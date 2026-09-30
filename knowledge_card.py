"""知识卡片导出模块（地方志 OCR 项目 — K1 阶段）。

设计：
把 P2-6 输出的 L0/L1/L2 结构化 JSON 转成 RAG 友好的知识卡片 JSONL，
按"项目"聚合输出到 ``knowledge_cards/<project_name>/`` 目录。

三级粒度（按需导出）：
- L0 document 卡片：整图概览，每张图一张
- L1 section 卡片（legacy，v2 默认不再产出）：每列一张，含完整文本 + 质量分桶
- L2 line 卡片（精细）：每行一张，含 confidence + confidence_source
- L3 unit 卡片（✅ v2 主推，2026-08-31）：语义单元卡（名录条目/账目/段落），
  数据源 ``data/units/<image_stem>.json``（unit_builder.py 产出），
  每卡携带结构化 fields（户名/方向/币种/金额归一/科年/籍贯等）

v2 默认 granularity = ("unit", "document")；section 卡停产后仍保留构建函数
（显式传 granularity 含 "section" 可导出，兼容旧消费者）。

输入：
- ``data/structured/<image_stem>.json``  P2-6 L0/L1/L2
- ``data/units/<image_stem>.json``  unit_builder 语义单元（v2 新增）
- ``data/project_assignments.json``  图→项目 映射
- ``data/projects.json``  项目元信息
- ``manual_annotations/<image>.jsonl``  手动校对事实（用于 metadata.has_manual_correction）

输出：
- ``knowledge_cards/<project_name>/units.jsonl``
- ``knowledge_cards/<project_name>/sections.jsonl``
- ``knowledge_cards/<project_name>/lines.jsonl``
- ``knowledge_cards/<project_name>/documents.jsonl``
- ``knowledge_cards/<project_name>/_index.json``
- ``knowledge_cards/<project_name>/_stats.json``
- ``knowledge_cards/<project_name>/README.md``

约束：
- ``build_*`` 函数是纯函数（除 IO 之外，无副作用）
- ``export_*`` 才有 IO 副作用
- 模块级 ``threading.RLock`` 保护 knowledge_cards/ 目录写（V9 修复）
- 不引入新依赖（仅 pathlib / json / threading / logging）
- 路径用 ``pathlib.Path``，UTF-8 编码

注：
- 本模块不读取 ``xlsx``（约束 #8：xlsx 是人用的，agent 只读 JSON/JSONL）
- 同一张图可能属于多个项目（schema 允许），但实测均为单项目
- 如果一张图在 ``project_assignments.json`` 缺失，归到 ``_unassigned`` 项目（不丢弃数据）
"""
import sys
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

log = logging.getLogger("local_chronicles_ocr")


# ============== 模块级常量 ==============

# 知识卡片 schema 版本号
# 每次 schema 变更必须 +1，下游消费者应校验 schema_version
# 1.1.0（2026-08-19 P-OPT）：section 卡新增可选 content.text_row_major（表格行主序）；
#        document 卡新增可选 summary.text_row_major；export 跳过 possible_duplicate 图
# 1.2.0（2026-08-31 知识库重构 M2）：新增 unit 卡（L3 语义单元）；
#        默认 granularity 改为 ("unit", "document")，section 停产（仍可显式导出）
KNOWLEDGE_CARD_SCHEMA_VERSION = "1.2.0"

# L0 文档卡片的 first_200_chars 长度
DOCUMENT_PREVIEW_CHARS = 200

# README 章节预览长度
README_SECTION_PREVIEW_CHARS = 100

# 质量分桶 4 类（与 P2-5 / structured_writer 一致）
QUALITY_BUCKETS = {
    "low_quality": "low_quality_count",
    "suspected_garbage": "garbage_count",
    "layout_anomaly": "layout_anomaly_count",
    "possible_duplicate": "duplicate_count",
}

# 未分类项目 ID（v2.1 决策：有图但 assignments 缺失的图归到这里）
UNASSIGNED_PROJECT_ID = "_unassigned"
UNASSIGNED_PROJECT_NAME = "未分类"

# 模块级锁：保护 knowledge_cards/ 目录写
# 多个 export_project 并发调用时（V9 修复），RLock 防止目录操作竞态
_write_lock = threading.RLock()

# 模块级路径（基于模块所在目录推断项目根）
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录（postbuild 联接真实数据）
else:
    _BASE = Path(__file__).parent.resolve()
BASE_DIR = _BASE
DEFAULT_DATA_DIR = BASE_DIR / "data"
# ✅ M22 清理：原 DEFAULT_PROJECTS_FILE / DEFAULT_ASSIGNMENTS_FILE 零引用，已删。
DEFAULT_STRUCTURED_DIR = DEFAULT_DATA_DIR / "structured"
DEFAULT_UNITS_DIR = DEFAULT_DATA_DIR / "units"
DEFAULT_MANUAL_DIR = BASE_DIR / "manual_annotations"


# ============== 内部工具函数 ==============

def _image_stem(image_name: str) -> str:
    """从 'foo.png' / 'foo' 提取 stem 'foo'。"""
    return Path(image_name).stem


def _confidence_source(ocr_backend: str) -> str:
    """把 backend 名映射成 confidence_source 字段。

    V11 简化：直接用 ocr_backend 名（baidu / paddleocr_vl；旧数据可能带 "paddleocr"）。
    不再归一化（v2 的 normalized 字段已删除），下游 RAG 按需判断。
    """
    if not ocr_backend:
        return "unknown"
    return str(ocr_backend)


def _aggregate_line_qualities(l2_lines: list) -> Dict[str, int]:
    """统计 L2 lines 各类质量桶的计数。

    Returns:
        {"low_quality_count": int, "garbage_count": int,
         "layout_anomaly_count": int, "duplicate_count": int}
    """
    counts = {v: 0 for v in QUALITY_BUCKETS.values()}
    for line in l2_lines or []:
        q = line.get("quality", "")
        if q in QUALITY_BUCKETS:
            counts[QUALITY_BUCKETS[q]] += 1
    return counts


def _image_name_from_structured(structured_json: dict) -> str:
    """从 structured_json 提取 image_name（容错）。"""
    l0 = structured_json.get("L0_document", {}) or {}
    return l0.get("image_name", "")


def _structured_json_path(image_name: str) -> str:
    """structured JSON 路径（POSIX 风格，相对项目根）。

    例如 'data/structured/page_042.json'。
    """
    stem = _image_stem(image_name)
    rel = Path("data") / "structured" / f"{stem}.json"
    return rel.as_posix()


def _structured_json_path_for(image_name: str, structured_dir: Path) -> Path:
    """从 image_name 反查 structured JSON 绝对路径。"""
    stem = _image_stem(image_name)
    return Path(structured_dir) / f"{stem}.json"


def _section_full_text(l2_lines_in_section: list) -> str:
    """拼接一段的 L2 lines 文本（按 row_index 顺序）。"""
    sorted_lines = sorted(l2_lines_in_section or [], key=lambda l: l.get("row_index", 0))
    return "".join(l.get("text", "") for l in sorted_lines)


def _row_range_from_l2(l2_lines: list) -> list:
    """从 L2 lines 算 row_range [min, max]（防御性空）。"""
    if not l2_lines:
        return [0, 0]
    rows = [l.get("row_index", 0) for l in l2_lines]
    return [min(rows), max(rows)]


def _xlsx_id_range_from_l2(l2_lines: list, fallback=None) -> list:
    """从 L2 lines 算 xlsx_id_range [min, max]。fallback 用于空。"""
    if not l2_lines:
        return list(fallback) if fallback else [0, 0]
    ids = [l.get("xlsx_id", 0) for l in l2_lines]
    return [min(ids), max(ids)]


# ============== 1. build_document_cards（L0） ==============

def build_document_cards(
    structured_json: dict,
    project_info: dict,
    manual_annotations: Optional[Dict[str, int]] = None,
) -> List[dict]:
    """构建 L0 文档卡片（每张图一张，整图概览）。

    Args:
        structured_json: P2-6 输出的 L0/L1/L2 dict
        project_info: ``{"project_id": ..., "project_name": ...}``
        manual_annotations: ``{image_name: int}`` 键值对，标注 line 数；可空

    Returns:
        list[dict]，长度恒为 1（如果 L0_document 缺 image_name 则返回 []）
    """
    l0 = structured_json.get("L0_document", {}) or {}
    image_name = l0.get("image_name", "")
    if not image_name:
        log.warning("[knowledge_card] structured_json missing L0_document.image_name, skip document card")
        return []

    image_stem = _image_stem(image_name)
    l1_sections = structured_json.get("L1_sections", []) or []
    l2_lines = structured_json.get("L2_lines", []) or []

    # first_200_chars：拼接全图文本（不依赖 L1.preview，因为它被截断到 200 字）
    full_text = _section_full_text(l2_lines)
    if not full_text:
        # ✅ 2026-08-20 P0 修复：L2_lines 缺失/为空时（历史/损坏数据），
        # 回退用 L1 段的 first_text/preview 拼接，避免产出空 preview 卡
        full_text = "".join(
            (sec.get("first_text") or "").strip() or (sec.get("preview") or "").strip()
            for sec in l1_sections
        )
    first_200 = full_text[:DOCUMENT_PREVIEW_CHARS]
    if len(full_text) > DOCUMENT_PREVIEW_CHARS:
        first_200 += "…"

    # sections summary（每个 section 一行）
    section_summaries = []
    for sec in l1_sections:
        section_summaries.append({
            "section_id": sec.get("section_id", ""),
            "col_index": sec.get("col_index", 0),
            "line_count": sec.get("line_count", 0),
            "char_count": sec.get("char_count", 0),
            "preview": sec.get("preview", ""),
        })

    sjp = _structured_json_path(image_name)

    card = {
        "card_id": f"doc_{image_stem}",
        "card_type": "document",
        "schema_version": KNOWLEDGE_CARD_SCHEMA_VERSION,
        "project": {
            "id": project_info.get("project_id", ""),
            "name": project_info.get("project_name", ""),
        },
        "source": {
            "image_name": image_name,
            "image_stem": image_stem,
            "ocr_backend": l0.get("ocr_backend", ""),
            "ocr_model": l0.get("ocr_model", ""),
            "ocr_at": l0.get("ocr_at", ""),
            "structured_json_path": sjp,
        },
        "summary": {
            "total_lines": l0.get("total_lines", 0),
            "total_columns": l0.get("total_columns", 0),
            "total_chars": l0.get("total_chars", 0),
            "first_200_chars": first_200,
        },
        "quality": {
            "image_quality": l0.get("quality", ""),
        },
        "sections": section_summaries,
    }

    # ✅ P-OPT-2（2026-08-19）：表格图附行主序文本概览（可选字段）
    table = structured_json.get("table")
    if table and isinstance(table, dict) and table.get("text_row_major"):
        card["summary"]["text_row_major"] = table["text_row_major"]

    return [card]


# ============== 2. build_section_cards（L1） ==============

def build_section_cards(
    structured_json: dict,
    project_info: dict,
    manual_annotations: Optional[Dict[str, int]] = None,
    quality_data: Optional[dict] = None,
) -> List[dict]:
    """构建 L1 段卡片（每列一张，主推格式）。

    Args:
        structured_json: P2-6 输出的 L0/L1/L2 dict
        project_info: ``{"project_id": ..., "project_name": ...}``
        manual_annotations: ``{image_name: int}`` 标注 line 数
        quality_data: 留作扩展接口（保持函数签名稳定），当前未使用

    Returns:
        list[dict]，每段一卡
    """
    l0 = structured_json.get("L0_document", {}) or {}
    image_name = l0.get("image_name", "")
    if not image_name:
        return []

    image_stem = _image_stem(image_name)
    l1_sections = structured_json.get("L1_sections", []) or []
    l2_lines = structured_json.get("L2_lines", []) or []

    # 按 col_index 把 L2 lines 分组（每组内按 row_index 排序）
    l2_by_col: Dict[int, list] = {}
    for line in l2_lines:
        col = line.get("col_index", 0)
        l2_by_col.setdefault(col, []).append(line)
    for col in l2_by_col:
        l2_by_col[col].sort(key=lambda l: l.get("row_index", 0))

    sjp = _structured_json_path(image_name)
    img_quality = l0.get("quality", "")

    # manual annotation 计数（按 image_name 查）
    annot_count = 0
    has_correction = False
    if manual_annotations and image_name in manual_annotations:
        annot_count = int(manual_annotations[image_name] or 0)
        has_correction = annot_count > 0

    cards: List[dict] = []
    for sec in l1_sections:
        col_index = sec.get("col_index", 0)
        col_l2 = l2_by_col.get(col_index, [])

        # 完整文本：从 L2 重新拼接（不依赖 L1.preview 的截断）
        full_text = _section_full_text(col_l2)
        if not full_text:
            # ✅ 2026-08-20 P0 修复：该列 L2 行缺失/为空时，
            # 回退用 L1 段的 first_text/preview，避免产出空 content.text 卡
            full_text = (sec.get("first_text") or "").strip() or (sec.get("preview") or "").strip()
        line_qualities = _aggregate_line_qualities(col_l2)
        row_range = _row_range_from_l2(col_l2)
        # 优先用 L1.xlsx_id_range（已由 P2-6 算好），fallback 到从 L2 算
        xlsx_id_range = sec.get("xlsx_id_range") or _xlsx_id_range_from_l2(col_l2)

        card = {
            "card_id": f"section_{image_stem}_col_{col_index}",
            "card_type": "section",
            "schema_version": KNOWLEDGE_CARD_SCHEMA_VERSION,
            "project": {
                "id": project_info.get("project_id", ""),
                "name": project_info.get("project_name", ""),
            },
            "source": {
                "image_name": image_name,
                "image_stem": image_stem,
                "ocr_backend": l0.get("ocr_backend", ""),
                "ocr_model": l0.get("ocr_model", ""),
                "ocr_at": l0.get("ocr_at", ""),
                "col_index": col_index,
                "row_range": row_range,
                "xlsx_id_range": xlsx_id_range,
                "structured_json_path": sjp,
            },
            "content": {
                "text": full_text,
                "char_count": len(full_text),
                "line_count": len(col_l2),
            },
            "quality": {
                "image_quality": img_quality,
                "line_qualities": line_qualities,
            },
            "metadata": {
                "corrected_count": annot_count,
                "manual_annot_count": annot_count,
                "has_manual_correction": has_correction,
            },
        }

        # ✅ P-OPT-2（2026-08-19）：表格图附行主序文本（人名+朝代共现，RAG 检索友好）
        # content.text 保持列主序原文不变（保真优先）
        table = structured_json.get("table")
        if table and isinstance(table, dict) and table.get("text_row_major"):
            card["content"]["text_row_major"] = table["text_row_major"]

        cards.append(card)

    return cards


# ============== 3. build_line_cards（L2） ==============

def build_line_cards(
    structured_json: dict,
    project_info: dict,
) -> List[dict]:
    """构建 L2 行卡片（每行一张，精细检索）。

    Args:
        structured_json: P2-6 输出的 L0/L1/L2 dict
        project_info: ``{"project_id": ..., "project_name": ...}``

    Returns:
        list[dict]，每行一卡
    """
    l0 = structured_json.get("L0_document", {}) or {}
    image_name = l0.get("image_name", "")
    if not image_name:
        return []

    image_stem = _image_stem(image_name)
    l2_lines = structured_json.get("L2_lines", []) or []

    # confidence_source 来自 L0 ocr_backend（V11 简化：单一字段，不归一化）
    backend = l0.get("ocr_backend", "")
    csource = _confidence_source(backend)

    sjp = _structured_json_path(image_name)

    cards: List[dict] = []
    for line in l2_lines:
        col_index = line.get("col_index", 0)
        row_index = line.get("row_index", 0)
        confidence = line.get("confidence", None)  # None / 0-1 浮点

        card = {
            "card_id": f"line_{image_stem}_col_{col_index}_row_{row_index}",
            "card_type": "line",
            "schema_version": KNOWLEDGE_CARD_SCHEMA_VERSION,
            "project": {
                "id": project_info.get("project_id", ""),
                "name": project_info.get("project_name", ""),
            },
            "source": {
                "image_name": image_name,
                "image_stem": image_stem,
                "col_index": col_index,
                "row_index": row_index,
                "xlsx_id": line.get("xlsx_id", 0),
                "structured_json_path": sjp,
            },
            "content": {
                "text": line.get("text", ""),
                "confidence": confidence,
                "confidence_source": csource,
            },
            "quality": line.get("quality", ""),
        }
        cards.append(card)

    return cards


# ============== 3.5 build_unit_cards（L3 语义单元，v2 主推） ==============

def build_unit_cards(
    units_payload: dict,
    project_info: dict,
) -> List[dict]:
    """构建 L3 语义单元卡片（每单元一张，v2 主推粒度）。

    数据源是 unit_builder.py 的产出（``data/units/<image_stem>.json``），
    单元是内容逻辑单元（名录条目 / 账目 / 段落），携带结构化 fields。

    Args:
        units_payload: ``data/units/<image_stem>.json`` 反序列化后的 dict
        project_info: ``{"project_id": ..., "project_name": ...}``

    Returns:
        list[dict]，每语义单元一卡。
        needs_review 单元照常产出（card.quality.verify_status 标记），
        下游 RAG 应按 ``verify_status == "verified"`` 过滤，避免未验证文本污染答案。

    card_id 约定：``unit_<image_stem>_<seq>``（如 ``unit_foo_0001``），
    ``unit_`` 前缀沿用 v2 M3 约定（原 rag.CARD_ID_PATTERN 已随 M23 清理移除）。
    """
    image_stem = (units_payload.get("image_stem") or "").strip()
    if not image_stem:
        log.warning("[knowledge_card] units_payload missing image_stem, skip unit cards")
        return []
    image_name = units_payload.get("image_name") or f"{image_stem}.png"

    content_type = units_payload.get("content_type", "")
    payload_build_method = units_payload.get("build_method", "")
    source_text_hash = units_payload.get("source_text_hash", "")
    units = units_payload.get("units", []) or []

    cards: List[dict] = []
    for u in units:
        unit_id = u.get("unit_id", "")
        if not unit_id:
            continue
        # unit_id 形如 u_<stem>_<seq:04d>；card_id 直接复用 seq 段
        seq = unit_id.rsplit("_", 1)[-1]
        verify = u.get("verify", {}) or {}
        text = u.get("text", "")
        fields = u.get("fields", {}) or {}

        card = {
            "card_id": f"unit_{image_stem}_{seq}",
            "card_type": "unit",
            "schema_version": KNOWLEDGE_CARD_SCHEMA_VERSION,
            "project": {
                "id": project_info.get("project_id", ""),
                "name": project_info.get("project_name", ""),
            },
            "source": {
                "image_name": image_name,
                "image_stem": image_stem,
                "unit_id": unit_id,
                "unit_type": u.get("unit_type", ""),
                "content_type": content_type,
                "build_method": u.get("build_method", payload_build_method),
                "inherited": bool(u.get("inherited", False)),
                "line_ids": u.get("line_ids", []),
                "xlsx_id_range": u.get("xlsx_id_range", [0, 0]),
                "box": u.get("box", []),
                "source_text_hash": source_text_hash,
            },
            "content": {
                "text": text,
                "char_count": len(text),
                "fields": fields,
            },
            "quality": {
                "verify_status": verify.get("status", ""),
                "multiset": bool(verify.get("multiset", False)),
                "coverage": bool(verify.get("coverage", False)),
                "diff_total": int(verify.get("diff_total", 0) or 0),
            },
        }
        cards.append(card)

    return cards


def load_units_payload(units_dir: Union[str, Path], image_stem: str) -> Optional[dict]:
    """读 ``data/units/<image_stem>.json``，失败/缺文件返回 None（不抛错）。"""
    path = Path(units_dir) / f"{image_stem}.json"
    if not path.exists():
        return None
    return _load_json(path)


# ============== 4. 聚合入口 ==============

def build_all_cards(
    structured_json: dict,
    project_info: dict,
    granularity: Iterable[str] = ("section", "line", "document"),
    manual_annotations: Optional[Dict[str, int]] = None,
) -> Dict[str, List[dict]]:
    """按粒度批量构建卡片。

    Args:
        structured_json: P2-6 输出的 L0/L1/L2 dict
        project_info: ``{"project_id": ..., "project_name": ...}``
        granularity: 要产出的粒度（``"document"`` / ``"section"`` / ``"line"``）
        manual_annotations: ``{image_name: int}``

    Returns:
        ``{"document": [...], "section": [...], "line": [...]}``，未请求的粒度为空列表
    """
    out: Dict[str, List[dict]] = {"document": [], "section": [], "line": []}
    g = set(granularity or ())
    if "document" in g:
        out["document"] = build_document_cards(structured_json, project_info, manual_annotations)
    if "section" in g:
        out["section"] = build_section_cards(structured_json, project_info, manual_annotations)
    if "line" in g:
        out["line"] = build_line_cards(structured_json, project_info)
    return out


# ============== 5. IO 助手 ==============

def _load_json(path: Path) -> Optional[dict]:
    """读 JSON，失败返回 None（不抛错）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"[knowledge_card] load {path} failed: {e}")
        return None


def _load_projects(projects_file: Path) -> list:
    """读 projects.json，容错：缺文件/损坏/非 list → []。"""
    data = _load_json(projects_file)
    if not isinstance(data, list):
        return []
    return data


def _load_assignments(assignments_file: Path) -> dict:
    """读 project_assignments.json，容错：缺文件/损坏/非 dict → {}。"""
    data = _load_json(assignments_file)
    if not isinstance(data, dict):
        return {}
    return data


def _list_project_images(project_id: str, assignments: dict) -> List[str]:
    """从 assignments dict 取该项目的图列表（按 image_name 排序）。"""
    if not isinstance(assignments, dict):
        return []
    return sorted([k for k, v in assignments.items() if v == project_id])


def _load_manual_annotations(manual_dir: Path) -> Dict[str, int]:
    """扫描 manual_annotations/ 目录，返回 ``{image_name: line_count}``。

    命名约定：``<image_name>.jsonl``（如 ``foo.png.jsonl``）。
    容错：缺目录 → {}，文件读失败 → 计数 0。
    """
    out: Dict[str, int] = {}
    if not manual_dir or not Path(manual_dir).exists():
        return out
    for p in sorted(Path(manual_dir).glob("*.jsonl")):
        image_name = p.name[: -len(".jsonl")]  # 去掉 .jsonl 后缀
        try:
            with open(p, "r", encoding="utf-8") as f:
                n = sum(1 for line in f if line.strip())
        except Exception:
            n = 0
        out[image_name] = n
    return out


def _write_jsonl(path: Path, cards: list) -> None:
    """写 JSONL（UTF-8，一行一卡）。空列表也写出空文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for card in cards:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写：先写 ``.tmp``，再 rename（防崩溃留半截文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    tmp.replace(path)


# ============== 6. 辅助统计 / README ==============

def _compute_stats(all_cards: Dict[str, List[dict]]) -> dict:
    """RAG 系统用的细统计（分粒度 / quality / 后端）。"""
    section_cards = all_cards.get("section", [])
    line_cards = all_cards.get("line", [])

    # section 按 ocr_backend
    by_backend: Dict[str, int] = {}
    for c in section_cards:
        backend = c.get("source", {}).get("ocr_backend", "") or "(无)"
        by_backend[backend] = by_backend.get(backend, 0) + 1

    # section 按 image_quality
    by_image_quality: Dict[str, int] = {}
    for c in section_cards:
        q = c.get("quality", {}).get("image_quality", "") or "(无)"
        by_image_quality[q] = by_image_quality.get(q, 0) + 1

    # line 按 quality
    line_quality: Dict[str, int] = {}
    for c in line_cards:
        q = c.get("quality", "") or "(无)"
        line_quality[q] = line_quality.get(q, 0) + 1

    # line 按 confidence_source
    by_conf_source: Dict[str, int] = {}
    for c in line_cards:
        cs = c.get("content", {}).get("confidence_source", "") or "(无)"
        by_conf_source[cs] = by_conf_source.get(cs, 0) + 1

    # unit 卡统计（v2 M2）
    unit_cards = all_cards.get("unit", [])
    unit_by_type: Dict[str, int] = {}
    unit_by_build: Dict[str, int] = {}
    unit_by_status: Dict[str, int] = {}
    fields_coverage: Dict[str, int] = {}
    for c in unit_cards:
        ut = c.get("source", {}).get("unit_type", "") or "(无)"
        unit_by_type[ut] = unit_by_type.get(ut, 0) + 1
        bm = c.get("source", {}).get("build_method", "") or "(无)"
        unit_by_build[bm] = unit_by_build.get(bm, 0) + 1
        vs = c.get("quality", {}).get("verify_status", "") or "(无)"
        unit_by_status[vs] = unit_by_status.get(vs, 0) + 1
        for k in (c.get("content", {}).get("fields", {}) or {}):
            fields_coverage[k] = fields_coverage.get(k, 0) + 1

    return {
        "section_count_by_backend": by_backend,
        "section_count_by_image_quality": by_image_quality,
        "line_count_by_quality": line_quality,
        "line_count_by_confidence_source": by_conf_source,
        "unit_count_by_unit_type": unit_by_type,
        "unit_count_by_build_method": unit_by_build,
        "unit_count_by_verify_status": unit_by_status,
        "unit_fields_coverage": fields_coverage,
    }


def _generate_readme(project_name: str, all_cards: dict, index: dict) -> str:
    """生成人类可读的 README（人类 + RAG 工程师）。"""
    lines: List[str] = []
    lines.append(f"# 知识卡片：{project_name}")
    lines.append("")
    lines.append(f"- 导出时间：{index['exported_at']}")
    lines.append(f"- 项目 ID：`{index['project_id']}`")
    lines.append(f"- 图像数：{index['image_count']}")
    lines.append(f"- 字符总数：{index['total_chars']}")
    card_counts = index["card_count"]
    lines.append(
        f"- 卡片分布：unit={card_counts.get('unit', 0)}, "
        f"document={card_counts.get('document', 0)}, "
        f"section={card_counts.get('section', 0)}, "
        f"line={card_counts.get('line', 0)}"
    )
    lines.append("")
    lines.append("## 文件说明")
    lines.append("")
    lines.append("- `units.jsonl` — L3 语义单元卡片（v2 主推，名录条目/账目/段落，含结构化 fields）")
    lines.append("- `documents.jsonl` — L0 文档卡片（整图概览）")
    lines.append("- `sections.jsonl` — L1 段卡片（legacy，v2 默认停产）")
    lines.append("- `lines.jsonl` — L2 行卡片（精细，每行一张）")
    lines.append("- `_index.json` — 项目级清单")
    lines.append("- `_stats.json` — RAG 系统用的分布统计")
    lines.append("")
    lines.append("## 卡片预览（前 10 张 unit 卡片）")
    lines.append("")
    unit_cards = all_cards.get("unit", [])
    for card in unit_cards[:10]:
        cid = card.get("card_id", "")
        proj = card.get("project", {}).get("id", "")
        img = card.get("source", {}).get("image_name", "")
        utype = card.get("source", {}).get("unit_type", "")
        preview_text = card.get("content", {}).get("text", "")
        preview = preview_text[:README_SECTION_PREVIEW_CHARS]
        if len(preview_text) > README_SECTION_PREVIEW_CHARS:
            preview += "…"
        lines.append(f"### {cid}")
        lines.append(f"`{proj}` · `{img}` · unit_type={utype}")
        lines.append("")
        lines.append(f"> {preview}")
        lines.append("")
    return "\n".join(lines)


# ============== 7. export_project（IO 入口） ==============

def export_project(
    project_id: str,
    output_dir: Union[str, Path],
    granularity: Iterable[str] = ("unit", "document"),
    image_filter: Optional[Iterable[str]] = None,
    data_dir: Optional[Union[str, Path]] = None,
    manual_dir: Optional[Union[str, Path]] = None,
    include_duplicates: bool = False,
    units_dir: Optional[Union[str, Path]] = None,
) -> dict:
    """导出单项目的知识卡片。

    Args:
        project_id: 项目 ID（如 ``"行会研究_c8263e"``）。
            若不在 ``projects.json`` 中，回退使用 ``project_id`` 作为 ``project_name``。
        output_dir: 知识卡片输出根目录（自动创建 ``<project_name>/`` 子目录）
        granularity: 要导出的粒度。v2 默认 ``("unit", "document")``；
            ``section`` / ``line`` 仍支持（legacy，需显式传入）
        image_filter: 可选。显式覆盖默认从 assignments 取图的行为。
            传 ``None`` → 默认按 project_id 在 assignments 里查图。
            传 list（即使为空）→ 用 list 覆盖。
        data_dir: 注入 data 目录（测试用）。None 时用 ``data/``（项目根下）。
        manual_dir: 注入 manual_annotations 目录（测试用）。
        include_duplicates: ✅ P-OPT-3。True 时疑似重复图（quality ==
            possible_duplicate）也导出。默认 False（排除，防矛盾副本污染 RAG）。
        units_dir: 注入 data/units 目录（测试用）。None 时用 ``data/units/``。

    Returns:
        dict 形如
        ``{"project_id", "project_name", "output_dir",
        "card_count": {"unit","document","section","line"},
        "total_chars", "image_count", "exported_at", ...}``
    """
    output_dir = Path(output_dir)
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    manual_dir = Path(manual_dir) if manual_dir else DEFAULT_MANUAL_DIR
    units_dir = Path(units_dir) if units_dir else (data_dir / "units")

    projects_file = data_dir / "projects.json"
    assignments_file = data_dir / "project_assignments.json"
    structured_dir = data_dir / "structured"

    with _write_lock:
        projects = _load_projects(projects_file)
        assignments = _load_assignments(assignments_file)
        manual_annots = _load_manual_annotations(manual_dir)

        # 1) 找 project 名称
        project_name = project_id
        for p in projects:
            if p.get("id") == project_id:
                project_name = p.get("name", project_id)
                break

        # 2) 找项目下的图
        if image_filter is not None:
            # 显式覆盖（用于 _unassigned 模式 — orphan 图列表）
            images = sorted(set(image_filter))
        else:
            images = _list_project_images(project_id, assignments)

        # 3) 聚合卡片
        all_cards: Dict[str, List[dict]] = {
            "unit": [], "document": [], "section": [], "line": [],
        }
        project_info = {"project_id": project_id, "project_name": project_name}
        total_chars = 0
        skipped_duplicates: List[str] = []  # ✅ P-OPT-3：被排除的疑似重复图
        g = set(granularity or ())

        for image_name in images:
            image_stem = _image_stem(image_name)

            # —— P-OPT-3：疑似重复图整图排除（unit / structured 卡都不导出）——
            # 提前到循环开头（v2）：原来只挡 structured 卡，unit 卡也会被污染
            sj_path = _structured_json_path_for(image_name, structured_dir)
            sj = _load_json(sj_path) if sj_path.exists() else None
            img_quality = (sj.get("L0_document", {}) or {}).get("quality", "") if sj else ""
            if img_quality == "possible_duplicate" and not include_duplicates:
                dup_of = (sj.get("L0_document", {}) or {}).get("duplicate_of", "")
                skipped_duplicates.append(
                    f"{image_name}（duplicate_of={dup_of or '?'}）"
                )
                log.info(
                    f"[knowledge_card] skip possible_duplicate: {image_name}"
                    f" (duplicate_of={dup_of or '?'})"
                )
                continue

            # —— v2 M2：unit 卡（数据源 data/units/，不依赖 structured JSON）——
            if "unit" in g:
                payload = load_units_payload(units_dir, image_stem)
                if payload:
                    unit_cards = build_unit_cards(payload, project_info)
                    all_cards["unit"].extend(unit_cards)
                    for c in unit_cards:
                        total_chars += c.get("content", {}).get("char_count", 0)
                else:
                    log.warning(
                        f"[knowledge_card] {image_stem} → units payload not found"
                        f"（unit_builder 未跑？），unit 卡缺失"
                    )

            # —— L0/L1/L2 卡（依赖 structured JSON）——
            if not (g & {"document", "section", "line"}):
                continue
            if not sj_path.exists():
                log.warning(
                    f"[knowledge_card] {image_name} → {sj_path} not found, skip"
                )
                continue
            if not sj:
                continue

            built = build_all_cards(
                sj, project_info,
                granularity=granularity,
                manual_annotations=manual_annots,
            )
            for k in all_cards:
                if k == "unit":
                    continue  # unit 卡已在上面单独处理
                all_cards[k].extend(built.get(k, []))
            # 字符数（从 section 卡片取；unit 主导时已在上面累计，避免重复计数）
            if "unit" not in g:
                for c in built.get("section", []):
                    total_chars += c.get("content", {}).get("char_count", 0)

        # 4) 写文件
        project_dir = output_dir / project_name
        project_dir.mkdir(parents=True, exist_ok=True)

        # jsonl 文件名：unit→units, document→documents, section→sections, line→lines
        jsonl_names = {
            "unit": "units.jsonl",
            "document": "documents.jsonl",
            "section": "sections.jsonl",
            "line": "lines.jsonl",
        }
        for ctype, cards in all_cards.items():
            _write_jsonl(project_dir / jsonl_names[ctype], cards)

        # _index.json
        index = {
            "project_id": project_id,
            "project_name": project_name,
            "card_count": {k: len(v) for k, v in all_cards.items()},
            "total_chars": total_chars,
            "image_count": len(images),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            # ✅ P-OPT-3：疑似重复图排除记录（人工复核入口）
            "skipped_duplicates": skipped_duplicates,
            "include_duplicates": bool(include_duplicates),
        }
        _atomic_write_text(
            project_dir / "_index.json",
            json.dumps(index, ensure_ascii=False, indent=2),
        )

        # _stats.json
        stats = _compute_stats(all_cards)
        _atomic_write_text(
            project_dir / "_stats.json",
            json.dumps(stats, ensure_ascii=False, indent=2),
        )

        # README.md
        readme = _generate_readme(project_name, all_cards, index)
        _atomic_write_text(project_dir / "README.md", readme)

    return {
        "project_id": project_id,
        "project_name": project_name,
        "output_dir": str(project_dir),
        "card_count": index["card_count"],
        "total_chars": total_chars,
        "image_count": index["image_count"],
        "exported_at": index["exported_at"],
        "skipped_duplicates": skipped_duplicates,
    }


# ============== 8. export_all_projects（批量） ==============

def export_all_projects(
    output_dir: Union[str, Path],
    granularity: Iterable[str] = ("unit", "document"),
    data_dir: Optional[Union[str, Path]] = None,
    manual_dir: Optional[Union[str, Path]] = None,
) -> List[dict]:
    """批量导出所有项目的知识卡片。

    行为：
    1. 遍历 ``projects.json`` 里的所有项目，逐个 ``export_project``
    2. 扫描 ``data/structured/`` 找出所有有 L0/L1/L2 的图
    3. 与 ``project_assignments.json`` 里的 image_name 取差集
       → orphan 图（在 structured/ 但不在 assignments）归到 ``_unassigned`` 项目
    4. 返回每个项目的导出结果列表

    Args:
        output_dir: 知识卡片输出根目录
        granularity: 要导出的粒度（透传给 ``export_project``）
        data_dir: 注入 data 目录（测试用）
        manual_dir: 注入 manual_annotations 目录（测试用）

    Returns:
        list[dict]，每项同 ``export_project`` 返回值
    """
    output_dir = Path(output_dir)
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    manual_dir = Path(manual_dir) if manual_dir else DEFAULT_MANUAL_DIR

    projects = _load_projects(data_dir / "projects.json")
    assignments = _load_assignments(data_dir / "project_assignments.json")
    structured_dir = data_dir / "structured"

    # 收集所有有 structured JSON 的图名（从 L0_document.image_name）
    all_structured_images: set = set()
    if structured_dir.exists():
        for p in sorted(structured_dir.glob("*.json")):
            sj = _load_json(p)
            img = _image_name_from_structured(sj) if sj else ""
            if img:
                all_structured_images.add(img)

    # orphan 图：在 structured/ 有，但不在 assignments 里
    orphan_images = sorted(all_structured_images - set(assignments.keys()))

    results: List[dict] = []
    for p in projects:
        pid = p.get("id")
        if not pid:
            continue
        r = export_project(
            pid, output_dir,
            granularity=granularity,
            data_dir=data_dir,
            manual_dir=manual_dir,
        )
        results.append(r)

    # 导出 _unassigned（如果有 orphan 图）
    if orphan_images:
        r = export_project(
            UNASSIGNED_PROJECT_ID, output_dir,
            granularity=granularity,
            image_filter=orphan_images,
            data_dir=data_dir,
            manual_dir=manual_dir,
        )
        # 覆盖 project_name 为中文"未分类"，让目录名更友好
        r["project_name"] = UNASSIGNED_PROJECT_NAME
        results.append(r)

    return results
