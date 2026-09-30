"""结构化 JSON 输出（RAG 准备，OpenViking L0/L1/L2 范式）。

设计目标：
把每张图的 OCR 结果按三级粒度输出成 JSON，给 RAG 阶段使用：

- L0_document：文件级（image_name / OCR 后端 / 时间 / 总行数 / 质量标记等）
- L1_blocks：块级（✅ P1 2026-09-10；每块的原始文本 / 块框 / split_rule / 段切分）
- L1_sections：列级（每列 = 一段，按 col_index 聚合；含 char_count / preview）
- L2_lines：行级（每个 OCR box group = 一行；含 text / confidence / box 坐标 / 行级 quality）

为什么多一层 L1_blocks？
- 旧实现只落 L2_lines（7 字段白名单），**块级 box 与段切分丢失** → 离开当次运行就
  无法复算框（P0/P0b 的几何修正在内存里完成，落盘时只剩结果）
- 有了 L1_blocks，任一页可**仅凭 structured JSON**（不需图像、不需重跑 OCR）经
  `rebuild_lines_from_blocks()` 复算出 lines / columns —— 这是 P1 的验收标准
- L1_blocks 是**追加键**：缺失时下游应回退为"只读 L2_lines"（向前兼容）

为什么三级？
- 朴素 RAG：把整张图 OCR 文本塞一段 → 检索精度差、丢失版面信息
- L0/L1/L2 RAG：按需取粒度
  - "找提到 X 府的段落" → L1（列级，有版面上下文）
  - "第 3 列第 5 行是什么" → L2（行级，精准定位）
  - "这张图是哪天识别的" → L0（元信息）

文件位置：data/structured/<image_stem>.json
（如 data/structured/test_p2_5_quality.json）

写入策略：每张图写一个文件，与 xlsx 写入互不干扰。
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("local_chronicles_ocr")


# L0/L1/L2 JSON schema 版本号
# 每次 schema 变更必须 +1，下游消费者应校验 _schema_version
# 1.1.0（2026-09-10 P1）：新增 L1_blocks（块层：块框 + 原文 + split_rule + 段切分）
SCHEMA_VERSION = "1.1.0"

# L1 section 的 preview 长度
L1_PREVIEW_CHARS = 200

# ✅ M22 清理：原 L2_PREVIEW_CHARS 零引用，已删。

# ✅ P1：会使输出顺序保持 entry 顺序的 split_rule（与 ocr_backend.build_result_from_blocks
# 的 `rebuilt_blocks or html_blocks` 判据一一对应；仅在 block_order 缺失时作兜底推断）
ENTRY_ORDER_RULES = ("projection-columns", "html-table")


def build_l0_document(
    image_name: str,
    ocr_backend: str,
    ocr_model: str,
    columns: list,
    lines: list,
    quality: str,
    xlsx_id_range: tuple,
    finished_at: Optional[float] = None,
    duplicate_of: str = "",
) -> dict:
    """构建 L0 文件级元信息。

    参数:
        image_name: 原图文件名（含扩展名）
        ocr_backend: 后端名（paddleocr_vl / baidu；旧数据可能带已移除的 paddleocr）
        ocr_model: 模型名（PaddleOCR-VL-1.6 / accurate / ...）
        columns: result["columns"]（列结构）
        lines: result["lines"]（行结构）
        quality: 整图 OCR 质量标签（来自 P2-5 validator）
        xlsx_id_range: (start_id, end_id) 在 xlsx 中的 ID 范围（含两端）
        finished_at: 结束时间（Unix epoch 秒），None = now()
        duplicate_of: ✅ P-OPT-3 正本文件名（quality=possible_duplicate 时非空）
    """
    total_chars = sum(len(line.get("text", "")) for line in lines)
    doc = {
        "image_name": image_name,
        "ocr_backend": ocr_backend,
        "ocr_model": ocr_model,
        "ocr_at": datetime.fromtimestamp(
            finished_at if finished_at is not None else datetime.now().timestamp()
        ).isoformat(timespec="seconds"),
        "total_lines": len(lines),
        "total_columns": len(columns),
        "total_chars": total_chars,
        "quality": quality or "",
        "xlsx_id_range": [xlsx_id_range[0], xlsx_id_range[1]]
            if xlsx_id_range and len(xlsx_id_range) == 2 else [0, 0],
    }
    # ✅ P-OPT-3：possible_duplicate 时记录正本指向（不占正常文档的 schema）
    if duplicate_of:
        doc["duplicate_of"] = duplicate_of
    return doc


def build_l1_sections(
    lines: list,
    xlsx_id_start: int,
) -> list:
    """构建 L1 列级 section 列表。

    按 (col_index, row_index) 排序后，按 col_index 聚合。
    每个 section:
      - section_id: "col_<col_index>"
      - col_index: 原始列号
      - line_count / char_count: 统计
      - line_id_range: [start_line_id, end_line_id] (L2 索引)
      - xlsx_id_range: [start_xlsx_id, end_xlsx_id]
      - first_text: 第一节文字（首行）= section 主题的快速预览
      - preview: 前 L1_PREVIEW_CHARS 字的拼接（用全图前 N 字）

    返回: list[dict]，按 col_index 升序
    """
    if not lines:
        return []

    # 按 (col_index, row_index) 排序（防御性：万一输入未排序）
    sorted_lines = sorted(lines, key=lambda l: (l.get("col_index", 0), l.get("row_index", 0)))

    # 按 col_index 分组
    groups: dict[int, list] = {}
    for line in sorted_lines:
        col = line.get("col_index", 0)
        groups.setdefault(col, []).append(line)

    sections = []
    line_cursor = 0  # L2 line 索引（不是 xlsx id）
    for col_index in sorted(groups.keys()):
        col_lines = groups[col_index]
        line_id_start = line_cursor
        line_id_end = line_cursor + len(col_lines) - 1
        xlsx_ids = [
            xlsx_id_start + i
            for i in range(len(sorted_lines))
            if sorted_lines[i].get("col_index") == col_index
        ]
        # char_count + preview
        full_text = "".join(l.get("text", "") for l in col_lines)
        # preview：前 L1_PREVIEW_CHARS 字
        preview = full_text[:L1_PREVIEW_CHARS]
        if len(full_text) > L1_PREVIEW_CHARS:
            preview += "…"
        first_text = col_lines[0].get("text", "") if col_lines else ""

        sections.append({
            "section_id": f"col_{col_index}",
            "col_index": col_index,
            "line_count": len(col_lines),
            "char_count": len(full_text),
            "line_id_range": [line_id_start, line_id_end],
            "xlsx_id_range": [xlsx_ids[0], xlsx_ids[-1]] if xlsx_ids else [0, 0],
            "first_text": first_text,
            "preview": preview,
        })
        line_cursor = line_id_end + 1

    return sections


def build_l2_lines(
    lines: list,
    xlsx_id_start: int,
    quality_map: dict,
) -> list:
    """构建 L2 行级 line 列表。

    每个 line:
      - line_id: L2 索引（0-based，按 col+row 排序后）
      - xlsx_id: 对应 xlsx 行 ID
      - col_index / row_index
      - text
      - confidence (None = VL 后端不提供)
      - box: 框坐标 [[x1,y1], [x2,y2], ...]
      - quality: 行级 quality tag（来自 P2-5）

    返回: list[dict]，按 (col_index, row_index) 升序
    """
    if not lines:
        return []
    sorted_lines = sorted(lines, key=lambda l: (l.get("col_index", 0), l.get("row_index", 0)))
    out = []
    for i, line in enumerate(sorted_lines):
        out.append({
            "line_id": i,
            "xlsx_id": xlsx_id_start + i,
            "col_index": line.get("col_index", 0),
            "row_index": line.get("row_index", 0),
            "text": line.get("text", ""),
            "confidence": line.get("confidence"),  # None = VL 后端
            "box": line.get("box", []),
            "quality": quality_map.get(i, "") if isinstance(quality_map, dict) else "",
        })
    return out


def build_l1_blocks(blocks: list, order: str = "", source: str = "pipeline") -> Optional[dict]:
    """✅ P1（2026-09-10）：构建 L1_blocks 块层（白名单，与 build_l2_lines 同思路）。

    参数:
        blocks: `ocr_backend.build_result_from_blocks` 返回的 `blocks`
                （每项 = block_id / box / text / split_rule / entries）
        order:  "entry" | "column" —— 复算时的输出顺序规则。空则由 split_rule 兜底推断
        source: "pipeline" = OCR 管线原始块层；"l2_reconstructed" = 由 L2_lines 回填
                （**块分组不可复原**，仅保证复算等价）

    返回: dict | None（无有效块时 None → 调用方不写该键，保持向前兼容）

    结构:
        {"order": "entry"|"column", "source": ..., "blocks": [
            {"block_id": 0, "box": [x1,y1,x2,y2], "text": 原始块文本,
             "split_rule": "single|even-split|projection-columns|html-table",
             "entries": [{"box": [...], "text": ..., "geometry": ...}]}]}
    """
    if not blocks:
        return None
    out = []
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        box = b.get("box") or []
        if len(box) != 4:
            continue
        entries = []
        for e in b.get("entries") or []:
            if not isinstance(e, dict):
                continue
            ebox = e.get("box") or []
            if not ebox:
                continue
            rec = {"box": ebox, "text": e.get("text", "")}
            if e.get("geometry"):
                rec["geometry"] = e["geometry"]
            entries.append(rec)
        out.append({
            "block_id": b.get("block_id", i),
            "box": [float(v) for v in box],
            "text": b.get("text", ""),
            "split_rule": b.get("split_rule", ""),
            "entries": entries,
        })
    if not out:
        return None
    if not order:
        order = "entry" if any(b["split_rule"] in ENTRY_ORDER_RULES for b in out) else "column"
    return {"order": order, "source": source, "blocks": out}


def rebuild_lines_from_blocks(l1_blocks: Optional[dict]) -> Optional[dict]:
    """✅ P1：仅凭 L1_blocks 复算 lines/columns（不依赖图像、不重跑 OCR）。

    走 `ocr_backend.assemble_result` —— **与在线路径同一份装配实现**，
    保证"复算结果 == 在线结果"不会因两处逻辑漂移。

    返回: {"text", "lines", "columns"} | None（无块层时 None）
    """
    if not l1_blocks or not l1_blocks.get("blocks"):
        return None
    import ocr_backend as _ob            # 惰性导入：避免结构化消费方被 OCR 依赖拖累

    entries, text_parts = [], []
    for b in l1_blocks["blocks"]:
        for e in b.get("entries") or []:
            rec = {"box": e.get("box"), "text": e.get("text", ""), "confidence": None}
            if e.get("geometry"):
                rec["geometry"] = e["geometry"]
            entries.append(rec)
            text_parts.append(rec["text"])
    if not entries:
        return None
    return _ob.assemble_result(entries, text_parts,
                               entry_order=(l1_blocks.get("order") == "entry"))


def rebuild_from_document(doc: Optional[dict]) -> Optional[dict]:
    """✅ P1：从整份 structured JSON 复算框（无 L1_blocks 时返回 None）。"""
    if not isinstance(doc, dict):
        return None
    return rebuild_lines_from_blocks(doc.get("L1_blocks"))


class StructuredWriter:
    """结构化 JSON 写入器。

    路径：data/structured/<image_stem>.json
    （一个图一个 JSON，与 xlsx 互不干扰）

    线程安全：用 self._write_lock 保护文件写入（防多图同时写同一文件）
    """

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    def _path_for(self, image_name: str) -> Path:
        """image.png → output_dir/image.json"""
        stem = Path(image_name).stem
        return self.output_dir / f"{stem}.json"

    def save(
        self,
        image_name: str,
        ocr_backend: str,
        ocr_model: str,
        columns: list,
        lines: list,
        quality: str,
        xlsx_id_start: int,
        quality_map: dict,
        duplicate_of: str = "",
        blocks: Optional[list] = None,
        block_order: str = "",
    ) -> bool:
        """写一张图的 L0/L1/L2 JSON（+ ✅ P1 的 L1_blocks）。

        duplicate_of: ✅ P-OPT-3 正本文件名（quality=possible_duplicate 时传入）
        blocks / block_order: ✅ P1 块层（`recognize()` 返回的 result["blocks"] /
            result["block_order"]）。缺失（如 char 模式、旧调用方）→ 不写 L1_blocks，
            保持 schema 向前兼容。

        返回: True = 成功，False = 失败（已 log 错误）
        """
        if not lines:
            log.warning(f"[structured] no lines for {image_name}, skip")
            return False

        with self._write_lock:
            try:
                l0 = build_l0_document(
                    image_name=image_name,
                    ocr_backend=ocr_backend,
                    ocr_model=ocr_model,
                    columns=columns,
                    lines=lines,
                    quality=quality,
                    xlsx_id_range=(xlsx_id_start, xlsx_id_start + len(lines) - 1),
                    duplicate_of=duplicate_of,
                )
                l1 = build_l1_sections(lines, xlsx_id_start)
                l2 = build_l2_lines(lines, xlsx_id_start, quality_map)

                payload = {
                    "_schema_version": SCHEMA_VERSION,
                    "L0_document": l0,
                    "L1_sections": l1,
                    "L2_lines": l2,
                }

                # ✅ P1（2026-09-10）：块层落盘 —— 使任一页可仅凭 structured 复算框
                l1b = build_l1_blocks(blocks, order=block_order)
                if l1b:
                    payload["L1_blocks"] = l1b

                # ✅ P-OPT-2（2026-08-19）：表格图附加重建网格（行主序文本供 RAG）
                # 纯本地计算，无 IO；散文图 detect_table 返回 False，不附加字段
                try:
                    from table_grid import detect_and_build
                    table = detect_and_build(lines)
                    if table:
                        payload["table"] = table
                        log.info(
                            f"[structured] table grid rebuilt for {image_name}: "
                            f"{table['row_count']} rows x {table['column_count']} cols"
                        )
                except Exception as e:
                    log.warning(f"[structured] table_grid failed for {image_name}: {e}")

                path = self._path_for(image_name)
                # 原子写：先写 .tmp，再 rename（防崩溃留半截文件）
                tmp_path = path.with_suffix(".json.tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                tmp_path.replace(path)
                log.info(f"[structured] wrote {path.name} "
                         f"({len(l2)} lines / {len(l1)} cols / {l0['total_chars']} chars"
                         f"{' / ' + str(len(l1b['blocks'])) + ' blocks' if l1b else ''})")
                return True
            except Exception as e:
                import traceback
                log.error(f"[structured] save failed for {image_name}: {e}")
                log.error(traceback.format_exc())
                return False

    def load(self, image_name: str) -> Optional[dict]:
        """读一张图的 L0/L1/L2 JSON。

        返回: dict / None（文件不存在时返回 None）
        """
        path = self._path_for(image_name)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"[structured] load failed for {image_name}: {e}")
            return None

    def list_available(self) -> list:
        """列出所有可用的结构化 JSON 文件。

        返回: list[dict]，每项含 image_name / path / size / mtime / schema_version
        """
        if not self.output_dir.exists():
            return []
        results = []
        for p in sorted(self.output_dir.glob("*.json")):
            try:
                stat = p.stat()
                # 尝试读 schema_version（不需要全 load）
                schema_version = ""
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        head = f.read(2048)  # _schema_version 在文件顶部
                    import re
                    m = re.search(r'"_schema_version"\s*:\s*"([^"]+)"', head)
                    if m:
                        schema_version = m.group(1)
                except Exception:
                    pass
                results.append({
                    "image_name": p.stem,  # 去掉 .json
                    "path": str(p),
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "schema_version": schema_version,
                })
            except Exception as e:
                log.warning(f"[structured] list: skip {p.name}: {e}")
        return results
