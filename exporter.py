"""学术研究结构化数据导出器（Phase 0，2026-08-20）。

目标：把 canonical 中间产物（data/structured/*.json 的 L0/L1/L2）展平为
**人类可读的学术研究视图**，导出 xlsx。

与知识卡片导出（AI 出口）的分工：
- 知识卡片（knowledge_card.py）：按 section/line 切块 → RAG 检索（AI 可读）
- 学术导出（本模块）：按 图序/栏序/行序 展平 + 坐标回溯 + 校勘三列（人类可读）

设计要点：
1. 数据源单一：只读 data/structured/*.json（L0/L1/L2），不直接改 Excel 主文件
2. 校勘三列：原文 text / 人工修正 corrected / 置信度 confidence 并排
   （corrected 从主工作簿按 xlsx_id 匹配，可选用 --xlsx 传入）
3. 坐标回溯：每行带 box 的 x/y 范围，可回溯到原书扫描件位置核对
4. 卷号钩子：volume_of(image_name) 默认从文件名提取「卷X」，Phase 1 可换项目配置

CLI 用法：
    python exporter.py --project-id 测试用归类栏目_0e8aba
    python exporter.py --structured-dir data/structured --out 学术导出.xlsx
    python exporter.py --project-id X --xlsx output.xlsx --out 带校勘列.xlsx
"""
import argparse
import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("academic_exporter")

# 学术导出列（顺序即表头顺序）
COLUMNS = [
    ("volume", "卷号"),
    ("image_order", "图序"),
    ("image_name", "图像文件"),
    ("col_index", "栏序"),
    ("row_index", "行序"),
    ("text", "原文"),
    ("corrected", "人工修正"),
    ("confidence", "置信度"),
    ("quality", "质量标签"),
    ("box_x", "x坐标范围"),
    ("box_y", "y坐标范围"),
    ("xlsx_id", "工作簿ID"),
    ("ocr_backend", "OCR后端"),
    ("ocr_model", "OCR模型"),
    ("ocr_at", "识别时间"),
]

# 阅读序文本 sheet（Phase 1：reassembler.py 的 L4 产出，人类可读视图；
# Phase 1.6：增加内容类型列——AI 先判断图片信息类型，roster 名录走条目化重组）
REASSEMBLY_COLUMNS = [
    ("image_order", "图序"),
    ("image_name", "图像文件"),
    ("content_type", "内容类型"),
    ("seq", "段序"),
    ("text", "阅读序文本"),
    ("line_ids", "来源线ID"),
    ("method", "重排方式"),
    ("llm_model", "模型"),
    ("status", "校验"),
    ("reassembled_at", "重排时间"),
]

# ============================================================
# M4 导出 v2（2026-09-01）：units（L3 语义单元层）驱动的类型化 sheet
# 数据源 data/units/<stem>.json；与 structured/reassembled 解耦，可独立重算
# ============================================================

# 名录 sheet：一条=一个 roster_entry unit，字段由 unit_builder 抽取
ROSTER_COLUMNS = [
    ("image_order", "图序"),
    ("image_name", "图像文件"),
    ("seq", "序号"),
    ("name", "姓名"),
    ("keyear", "科年"),
    ("identity", "身份"),
    ("note", "备注"),
    ("extra_fields", "其他字段"),
    ("text", "条目原文"),
    ("line_ids", "来源线ID"),
    ("xlsx_id_range", "xlsx_id范围"),
    ("status", "校验"),
]

# 账目 sheet：一条=一个 ledger_entry unit（金额归一为十進制兩值，可排序聚合）
LEDGER_COLUMNS = [
    ("image_order", "图序"),
    ("image_name", "图像文件"),
    ("seq", "序号"),
    ("account", "户名"),
    ("direction", "方向"),
    ("currency", "币种"),
    ("amount_raw", "金额原文"),
    ("amount_norm", "金额归一(兩)"),
    ("text", "条目原文"),
    ("line_ids", "来源线ID"),
    ("xlsx_id_range", "xlsx_id范围"),
    ("status", "校验"),
]

# 阅读版 sheet：prose/letter/document/table 的段落单元按阅读序
READING_COLUMNS = [
    ("image_order", "图序"),
    ("image_name", "图像文件"),
    ("content_type", "内容类型"),
    ("seq", "段序"),
    ("text", "段落文本"),
    ("line_ids", "来源线ID"),
    ("status", "校验"),
]

# 溯源 sheet：unit_id ↔ 图 ↔ box ↔ xlsx_id，串联 AI 答案脚注（unit_…）与扫描件坐标
PROVENANCE_COLUMNS = [
    ("unit_id", "unit_id"),
    ("image_name", "图像文件"),
    ("content_type", "内容类型"),
    ("unit_type", "单元类型"),
    ("box_x", "x坐标范围"),
    ("box_y", "y坐标范围"),
    ("xlsx_id_range", "xlsx_id范围"),
    ("build_method", "构建方式"),
    ("status", "校验"),
    ("rebuilt_at", "重建时间"),
]

# ✅ M22 清理：原 _XLSX_HEADERS（零引用，实际表头用 COLUMNS/ROSTER_COLUMNS 等），已删。


def volume_of(image_name: str) -> str:
    """从图片文件名提取卷号（钩子，Phase 1 可换项目级配置）。

    匹配「卷X」「卷之X」：卷[一二三四五六七八九十百0-9]+
    未命中返回 ""（调用方决定是否归入「未分卷」）。
    """
    m = re.search(r"卷(?:之)?([一二三四五六七八九十百\d]+)", image_name)
    return m.group(0) if m else ""


def load_structured_dir(structured_dir: Path) -> Dict[str, dict]:
    """读取目录下全部 structured JSON，返回 {image_stem: payload}。

    跳过 _backup / .tmp 文件；解析失败仅警告不中断。
    """
    result = {}
    for p in sorted(structured_dir.glob("*.json")):
        if p.name.startswith("_") or p.name.endswith(".tmp"):
            continue
        try:
            result[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"跳过损坏的 structured 文件 {p.name}: {e}")
    return result


def corrected_map_from_xlsx(xlsx_path: Path) -> Dict[int, str]:
    """读主工作簿，返回 {xlsx_id: corrected_text}（仅非空修正）。"""
    result = {}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            return result
        col_id = header.index("id") if "id" in header else 0
        col_corr = header.index("corrected_text") if "corrected_text" in header else 7
        for row in rows:
            rid = row[col_id]
            corr = row[col_corr]
            if isinstance(rid, (int, float)) and corr:
                result[int(rid)] = str(corr).strip()
    except Exception as e:
        log.warning(f"读取主工作簿修正列失败（继续无校勘导出）: {e}")
    return result


def safe_corrected(ocr_text: str, corrected: str) -> str:
    """粒度校验后的人工修正（防 xlsx_id 错位）。

    背景（2026-08-20 Phase 1 实测）：structured L2 与主工作簿的行粒度可能不一致
    （L2 是 VL 行模式整栏文本，主工作簿是 VL 列模式单字/短段行，但共享 xlsx_id 编号），
    直接按 xlsx_id 取 corrected 会把**别的行**的修正拼进本行，污染学术数据。
    错位特征（2026-08-20 修正后判据）：主工作簿单字/短段行拼到 L2 整栏文本行时，
    表现为**修正远短于原文**；而「修正比原文长」是合法的加字/补标点校对，必须放行。
    判据（保守：宁可漏接修正，不可错接修正）：
    - corrected 为空或与原文相同 → 原样返回
    - corrected 过短（<2 字）→ 视为错位特征，忽略
    - 修正远短于原文（n - m > 60%·n）→ 视为错位特征，忽略
    - 修正与原文等长或更长 → 放行（加字校对合法），仅由字符重叠兜底
    - 与原文字符集重叠 <50% → 忽略（人工校对通常保留多数原字）
    """
    if not corrected or corrected == ocr_text:
        return corrected or ""
    n, m = len(ocr_text), len(corrected)
    if n == 0 or m < 2:
        return ""
    if m < n and n - m > 0.6 * n:
        return ""
    c_in, c_out = Counter(ocr_text), Counter(corrected)
    overlap = sum((c_in & c_out).values())
    if overlap / n < 0.5:
        return ""
    return corrected


def _fmt_box(box):
    """box 4 点 [[x,y]x4] → 单值范围字符串（空则 ("", "")）。

    2026-08-20 修复：空 box 原返回 ""（字符串），调用处按二元组解包会
    ValueError——统一返回 ("", "")。
    """
    if not box or not isinstance(box, list) or len(box) < 2:
        return "", ""
    try:
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        return f"{min(xs):.1f}-{max(xs):.1f}", f"{min(ys):.1f}-{max(ys):.1f}"
    except Exception:
        return "", ""


def iter_academic_rows(
    structured_dir: Path,
    image_stems: Optional[List[str]] = None,
    corrected_map: Optional[Dict[int, str]] = None,
) -> List[dict]:
    """把 structured L0/L1/L2 展平为学术行（list[dict]，键见 COLUMNS）。

    - image_stems: 可选白名单（缺省 = 全部非 _ 前缀文件）
    - corrected_map: {xlsx_id: corrected_text}，缺省空（无校勘列）
    排序：图序（文件名序）→ 栏序 → 行序。
    """
    payloads = load_structured_dir(structured_dir)
    if image_stems is not None:
        want = set(image_stems)
        payloads = {k: v for k, v in payloads.items() if k in want}
    if not payloads:
        return []

    corrected_map = corrected_map or {}
    rows: List[dict] = []
    for order, (stem, data) in enumerate(sorted(payloads.items()), start=1):
        l0 = data.get("L0_document", {}) or {}
        l2 = data.get("L2_lines", []) or []
        image_name = l0.get("image_name") or f"{stem}.png"
        backend = l0.get("ocr_backend", "")
        model = l0.get("ocr_model", "")
        ocr_at = l0.get("ocr_at", "")
        l0_quality = l0.get("quality", "")
        vol = volume_of(image_name)
        for line in l2:
            box_x, box_y = _fmt_box(line.get("box"))
            confidence = line.get("confidence")
            rows.append({
                "volume": vol,
                "image_order": order,
                "image_name": image_name,
                "col_index": line.get("col_index", 0),
                "row_index": line.get("row_index", 0),
                "text": line.get("text", ""),
                "corrected": safe_corrected(
                    str(line.get("text", "")),
                    corrected_map.get(int(line["xlsx_id"]), "") if line.get("xlsx_id") is not None else "",
                )
                    if line.get("xlsx_id") is not None else "",
                "confidence": f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "",
                "quality": line.get("quality") or l0_quality,
                "box_x": box_x,
                "box_y": box_y,
                "xlsx_id": line.get("xlsx_id", ""),
                "ocr_backend": backend,
                "ocr_model": model,
                "ocr_at": ocr_at,
            })
    return rows


def _method_label(method: str, layout: str = "") -> str:
    """重排方式的中文标签（供阅读序文本 sheet 使用）。"""
    return {
        "llm": "LLM",
        "rule": "规则",
        "rule_fallback": "规则回退",
    }.get(method, method or layout)


def load_reassembled_dir(
    reassembled_dir: Path,
    image_stems: Optional[List[str]] = None,
) -> List[dict]:
    """读 data/reassembled/*.json（L4 阅读序），展平为每段一行的列表。

    - image_stems: 可选白名单（缺省 = 目录下全部非 _ 前缀文件）
    - 行键见 REASSEMBLY_COLUMNS；图序按白名单（或文件序）排序后编序
    """
    if not reassembled_dir.is_dir():
        return []
    stems = sorted(image_stems) if image_stems else None
    want = set(stems) if stems is not None else None
    rows: List[dict] = []
    for p in sorted(reassembled_dir.glob("*.json")):
        if p.name.startswith("_") or p.name.endswith(".tmp"):
            continue
        stem = p.stem
        if want is not None and stem not in want:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"跳过损坏的 reassembled 文件 {p.name}: {e}")
            continue
        segments = d.get("segments", []) or []
        order = stems.index(stem) + 1 if stems is not None else len(rows) + 1
        content_type = d.get("content_type", "")
        for s in segments:
            if not isinstance(s, dict):
                continue
            line_ids = s.get("line_ids") or []
            rows.append({
                "image_order": order,
                "image_name": d.get("image_name", f"{stem}.png"),
                "content_type": content_type,
                "seq": s.get("seq", 0),
                "text": s.get("text", ""),
                "line_ids": ",".join(str(i) for i in line_ids),
                "method": _method_label(d.get("method", ""), d.get("layout", "")),
                "llm_model": d.get("llm_model", ""),
                "status": "待复核" if s.get("status") == "needs_review" else "通过",
                "reassembled_at": d.get("reassembled_at", ""),
            })
    return rows


def load_units_dir(
    units_dir: Path,
    image_stems: Optional[List[str]] = None,
) -> List[dict]:
    """读 data/units/*.json（L3 语义单元层），按图序返回 payload 列表。

    - image_stems: 可选白名单（缺省 = 目录下全部非 _ 前缀文件）
    - 解析失败仅警告不中断；空 units / 非 dict payload 跳过
    """
    if not units_dir.is_dir():
        return []
    want = set(image_stems) if image_stems is not None else None
    payloads: List[dict] = []
    for p in sorted(units_dir.glob("*.json")):
        if p.name.startswith("_") or p.name.endswith(".tmp"):
            continue
        if want is not None and p.stem not in want:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"跳过损坏的 units 文件 {p.name}: {e}")
            continue
        if isinstance(d, dict) and d.get("units"):
            d.setdefault("image_stem", p.stem)
            payloads.append(d)
    return payloads


def _fmt_box_rect(box) -> tuple:
    """units 的 box（union 后 [x1,y1,x2,y2] 或 4 点列表）→ (x范围, y范围)。"""
    if not box or not isinstance(box, (list, tuple)) or len(box) < 4:
        return "", ""
    try:
        if all(isinstance(pt, (list, tuple)) and len(pt) >= 2 for pt in box):
            return _fmt_box(list(box))
        xs = [float(box[0]), float(box[2])]
        ys = [float(box[1]), float(box[3])]
        return f"{min(xs):.1f}-{max(xs):.1f}", f"{min(ys):.1f}-{max(ys):.1f}"
    except Exception:
        return "", ""


def _unit_status_label(unit: dict) -> str:
    """verify.status → 中文标签（与阅读序 sheet 的 通过/待复核 一致）。"""
    st = (unit.get("verify") or {}).get("status", "")
    return "待复核" if st == "needs_review" else "通过"


def _fmt_ids(v) -> str:
    """line_ids / xlsx_id_range → "a-b" / "a,b,c" 紧凑字符串。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        if len(v) == 2 and all(isinstance(x, int) for x in v):
            return f"{v[0]}-{v[1]}"  # xlsx_id_range 区间
        return ",".join(str(x) for x in v)
    return str(v)


def iter_roster_rows(unit_payloads: List[dict]) -> List[dict]:
    """roster_entry units → 名录 sheet 行（键见 ROSTER_COLUMNS）。"""
    rows: List[dict] = []
    known = ("姓名", "科年", "身份", "备注")
    for order, payload in enumerate(sorted(unit_payloads,
                                           key=lambda p: p.get("image_stem", "")), start=1):
        image_name = payload.get("image_name") or f"{payload.get('image_stem', '')}.png"
        for i, u in enumerate(payload.get("units", []), start=1):
            if u.get("unit_type") != "roster_entry":
                continue
            f = u.get("fields") or {}
            extra = {k: v for k, v in f.items() if k not in known and v}
            rows.append({
                "image_order": order,
                "image_name": image_name,
                "seq": i,
                "name": f.get("姓名", ""),
                "keyear": f.get("科年", ""),
                "identity": f.get("身份", ""),
                "note": f.get("备注", ""),
                "extra_fields": json.dumps(extra, ensure_ascii=False) if extra else "",
                "text": u.get("text", ""),
                "line_ids": _fmt_ids(u.get("line_ids")),
                "xlsx_id_range": _fmt_ids(u.get("xlsx_id_range")),
                "status": _unit_status_label(u),
            })
    return rows


def iter_ledger_rows(unit_payloads: List[dict]) -> List[dict]:
    """ledger_entry units → 账目 sheet 行（键见 LEDGER_COLUMNS）。

    金额归一保留 float（两位小数内精确到厘），供 xlsx 排序/聚合。
    """
    rows: List[dict] = []
    for order, payload in enumerate(sorted(unit_payloads,
                                           key=lambda p: p.get("image_stem", "")), start=1):
        image_name = payload.get("image_name") or f"{payload.get('image_stem', '')}.png"
        for i, u in enumerate(payload.get("units", []), start=1):
            if u.get("unit_type") != "ledger_entry":
                continue
            f = u.get("fields") or {}
            amt = f.get("金额归一")
            rows.append({
                "image_order": order,
                "image_name": image_name,
                "seq": i,
                "account": f.get("户名", ""),
                "direction": f.get("方向", ""),
                "currency": f.get("币种", ""),
                "amount_raw": f.get("金额原文", ""),
                "amount_norm": round(float(amt), 4) if isinstance(amt, (int, float)) else "",
                "text": u.get("text", ""),
                "line_ids": _fmt_ids(u.get("line_ids")),
                "xlsx_id_range": _fmt_ids(u.get("xlsx_id_range")),
                "status": _unit_status_label(u),
            })
    return rows


def iter_reading_rows(unit_payloads: List[dict]) -> List[dict]:
    """paragraph units（prose/letter/document/table）→ 阅读版 sheet 行。"""
    rows: List[dict] = []
    for order, payload in enumerate(sorted(unit_payloads,
                                           key=lambda p: p.get("image_stem", "")), start=1):
        image_name = payload.get("image_name") or f"{payload.get('image_stem', '')}.png"
        for i, u in enumerate(payload.get("units", []), start=1):
            if u.get("unit_type") != "paragraph":
                continue
            rows.append({
                "image_order": order,
                "image_name": image_name,
                "content_type": payload.get("content_type", ""),
                "seq": i,
                "text": u.get("text", ""),
                "line_ids": _fmt_ids(u.get("line_ids")),
                "status": _unit_status_label(u),
            })
    return rows


def iter_provenance_rows(unit_payloads: List[dict]) -> List[dict]:
    """全部 units → 溯源 sheet 行（AI 脚注 unit_… 回溯扫描件坐标的桥梁）。"""
    rows: List[dict] = []
    for order, payload in enumerate(sorted(unit_payloads,
                                           key=lambda p: p.get("image_stem", "")), start=1):
        image_name = payload.get("image_name") or f"{payload.get('image_stem', '')}.png"
        for u in payload.get("units", []):
            box_x, box_y = _fmt_box_rect(u.get("box"))
            rows.append({
                "unit_id": u.get("unit_id", ""),
                "image_name": image_name,
                "content_type": payload.get("content_type", ""),
                "unit_type": u.get("unit_type", ""),
                "box_x": box_x,
                "box_y": box_y,
                "xlsx_id_range": _fmt_ids(u.get("xlsx_id_range")),
                "build_method": payload.get("build_method", ""),
                "status": _unit_status_label(u),
                "rebuilt_at": u.get("rebuilt_at", "") or payload.get("built_at", ""),
            })
    return rows


def _append_sheet(wb, title: str, columns, rows: List[dict], widths: dict) -> None:
    """通用 sheet 写入（表头样式/列宽/冻结/筛选），与既有 sheet 风格一致。"""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(title)
    keys = [k for k, _ in columns]
    labels = [v for _, v in columns]
    ws.append(labels)
    for col_idx in range(1, len(labels) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="4F6228")
        c.alignment = Alignment(vertical="center")
    for row in rows:
        ws.append([row.get(k, "") for k in keys])
    for col_idx, key in enumerate(keys, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = widths.get(key, 12)
    if rows:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(labels))}{len(rows) + 1}"
    return ws


# ✅ M23 清理：原 export_units_xlsx()（M4 v2 独立导出入口）生产零调用——
# 主入口已是 export_academic_xlsx(unit_payloads=...)，其内部复用同一批
# iter_*_rows / _append_sheet；本函数仅测试引用，连同对应测试用例一并移除。


def export_academic_xlsx(
    rows: List[dict],
    out_path: Path,
    project_id: str = "",
    source_note: str = "",
    reassembled_rows: Optional[List[dict]] = None,
    unit_payloads: Optional[List[dict]] = None,
) -> Path:
    """写学术 xlsx：数据 sheet + 阅读序文本 sheet（可选）+ 类型化 units sheets（可选）+ 说明 sheet。

    M4 v2：unit_payloads（load_units_dir 产物）非空时追加 名录/账目/阅读版/溯源 sheet。
    返回 out_path。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "OCR学术数据"

    # 表头
    header_keys = [k for k, _ in COLUMNS]
    header_labels = [v for _, v in COLUMNS]
    ws.append(header_labels)
    for col_idx in range(1, len(header_labels) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="4F6228")
        c.alignment = Alignment(vertical="center")

    # 数据
    for row in rows:
        ws.append([row.get(k, "") for k in header_keys])

    # 列宽（文本列按内容自适应，上限 60）
    for col_idx, key in enumerate(header_keys, start=1):
        letter = get_column_letter(col_idx)
        if key == "text":
            width = 60
        elif key in ("image_name", "corrected"):
            width = 28
        elif key in ("box_x", "box_y"):
            width = 16
        else:
            width = 12
        ws.column_dimensions[letter].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(header_labels))}{len(rows) + 1}"

    # 阅读序文本 sheet（Phase 1：人类可读视图；缺省不创建，保持既有行为）
    if reassembled_rows:
        ws2 = wb.create_sheet("阅读序文本")
        r_keys = [k for k, _ in REASSEMBLY_COLUMNS]
        r_labels = [v for _, v in REASSEMBLY_COLUMNS]
        ws2.append(r_labels)
        for col_idx in range(1, len(r_labels) + 1):
            c = ws2.cell(row=1, column=col_idx)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="4F6228")
            c.alignment = Alignment(vertical="center")
        for row in reassembled_rows:
            ws2.append([row.get(k, "") for k in r_keys])
        for col_idx, key in enumerate(r_keys, start=1):
            letter = get_column_letter(col_idx)
            if key == "text":
                width = 80
            elif key in ("image_name", "line_ids"):
                width = 28
            else:
                width = 12
            ws2.column_dimensions[letter].width = width
        ws2.freeze_panes = "A2"
        ws2.auto_filter.ref = f"A1:{get_column_letter(len(r_labels))}{len(reassembled_rows) + 1}"

    # M4 v2：units 驱动的类型化 sheet（名录/账目/阅读版/溯源，空类型跳过）
    if unit_payloads:
        roster_rows = iter_roster_rows(unit_payloads)
        ledger_rows = iter_ledger_rows(unit_payloads)
        reading_rows = iter_reading_rows(unit_payloads)
        prov_rows = iter_provenance_rows(unit_payloads)
        common_w = {"image_name": 28, "text": 60, "line_ids": 16,
                    "xlsx_id_range": 12, "extra_fields": 20, "unit_id": 34}
        if roster_rows:
            _append_sheet(wb, "名录", ROSTER_COLUMNS, roster_rows,
                          {**common_w, "name": 14, "keyear": 12, "identity": 12})
        if ledger_rows:
            _append_sheet(wb, "账目", LEDGER_COLUMNS, ledger_rows,
                          {**common_w, "account": 16, "amount_raw": 22, "amount_norm": 13})
        if reading_rows:
            _append_sheet(wb, "阅读版", READING_COLUMNS, reading_rows, common_w)
        if prov_rows:
            _append_sheet(wb, "溯源", PROVENANCE_COLUMNS, prov_rows, common_w)

    # 说明 sheet（学术引用需要注明数据来源与生成参数）
    info = wb.create_sheet("说明")
    info.column_dimensions["A"].width = 24
    info.column_dimensions["B"].width = 60
    info.append(["项目", project_id])
    info.append(["生成时间", datetime.now().isoformat(timespec="seconds")])
    info.append(["数据行数", len(rows)])
    info.append(["数据来源", source_note or "data/structured/*.json（L0/L1/L2 canonical 模型）"])
    info.append(["校勘说明", "原文=OCR 原始输出；人工修正=主工作簿 corrected_text（如提供）；置信度为空表示后端未返回"])
    info.append(["坐标说明", "box 为原图像素坐标范围（x/y），可回溯原书扫描件核对"])
    if reassembled_rows:
        info.append(["阅读序文本", "由 reassembler.py 按内容类型感知重排生成：先由 AI 判定图片信息类型（prose 连贯文章/"
                                  "roster 名录/letter 信札/table 表格等），roster 名录做条目化重组，其余做阅读序重排；"
                                  "LLM 段仅改变顺序/分组、不改变字符；"
                                  "校验=待复核 表示字符多集校验未通过，引用前须人工核对"])
    if unit_payloads:
        info.append(["类型化 sheet", "名录/账目/阅读版 由 data/units（L3 语义单元层）生成：一条=一个内容逻辑单元"
                                     "（名录条目/账目/段落），字段由单元构建器抽取并经逐字校验；"
                                     "溯源 sheet 提供 unit_id ↔ 扫描件坐标 ↔ xlsx_id 的回溯链"])
        info.append(["金额说明", "账目 sheet 金额归一 = 中文数字金额折算为兩的十进制值"
                                 "（兩.錢分厘，如 6233兩2錢0分3厘 → 6233.203）"])
    info.append(["引用建议", "引用本数据请注明：项目名、生成时间、OCR 后端与模型（见数据列）"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    log.info(f"[exporter] 已导出 {len(rows)} 行 → {out_path}")
    return out_path


def _image_stems_for_project(project_id: str, data_dir: Path = None) -> List[str]:
    """从 data/project_assignments.json 取项目的图片白名单（按 stem）。

    ✅ M23：核心逻辑委托 data_io.get_project_image_stems 统一实现（消三胞胎重复）。
    """
    from data_io import get_project_image_stems
    return get_project_image_stems(project_id, data_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="学术研究结构化数据导出器（Phase 0）")
    parser.add_argument("--structured-dir", default="data/structured", help="canonical JSON 目录")
    parser.add_argument("--project-id", default="", help="项目 ID（自动取该项目图片白名单 + 写说明 sheet）")
    parser.add_argument("--image-stems", nargs="*", default=None, help="图片白名单（优先于 --project-id；缺省=全部）")
    parser.add_argument("--xlsx", default="", help="主工作簿路径（提供则带人工修正列）")
    parser.add_argument("--reassembled-dir", default="", help="L4 阅读序目录（提供则导出 Sheet2 阅读序文本）")
    parser.add_argument("--units-dir", default="", help="L3 语义单元目录（提供则追加 名录/账目/阅读版/溯源 sheets，M4 v2）")
    parser.add_argument("--out", default="", help="输出路径（缺省=academic_export_<时间戳>.xlsx）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    structured_dir = Path(args.structured_dir)
    if not structured_dir.is_dir():
        log.error(f"structured 目录不存在: {structured_dir}")
        return 2

    # 白名单优先级：--image-stems 显式 > --project-id 联动 > 全部
    image_stems = args.image_stems
    if image_stems is None and args.project_id:
        image_stems = _image_stems_for_project(args.project_id)
        if image_stems:
            log.info(f"项目 {args.project_id} 图片白名单: {len(image_stems)} 张")
        else:
            log.warning(f"项目 {args.project_id} 无图片映射，导出全部")

    corrected = {}
    if args.xlsx:
        corrected = corrected_map_from_xlsx(Path(args.xlsx))
        log.info(f"主工作簿修正列: {len(corrected)} 条非空修正")

    rows = iter_academic_rows(structured_dir, image_stems, corrected)
    if not rows:
        log.error("无可用 structured 数据（0 行），请检查目录/白名单")
        return 1

    reassembled_rows = []
    if args.reassembled_dir:
        reassembled_rows = load_reassembled_dir(Path(args.reassembled_dir), image_stems)
        log.info(f"阅读序文本 sheet: {len(reassembled_rows)} 段")

    unit_payloads = []
    if args.units_dir:
        unit_payloads = load_units_dir(Path(args.units_dir), image_stems)
        log.info(f"类型化 units sheets: {len(unit_payloads)} 图 / "
                 f"{sum(len(p.get('units', [])) for p in unit_payloads)} 单元")

    out = Path(args.out) if args.out else Path(f"academic_export_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    export_academic_xlsx(rows, out, project_id=args.project_id,
                         source_note=str(structured_dir),
                         reassembled_rows=reassembled_rows,
                         unit_payloads=unit_payloads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
