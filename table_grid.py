# -*- coding: utf-8 -*-
"""表格网格重建（P-OPT-2，2026-08-19）。

背景：
PaddleOCR-VL 对表格图片（如职官志官员名册）返回的小块 ≈ 单元格内容
（人名/年份/出处各一块）。块级解析（P-OPT-1）后这些块已按 x_center
聚成表格列、按 y 排序，但阅读顺序仍是"列主序"（竖排逻辑），对 RAG
不友好——"天启年间的知县是谁"这类查询需要"行主序"共现（朝代+人名）。

本模块（离线后处理，纯函数，不依赖 OpenCV）：
- detect_table(l2_lines) -> bool      表格启发式判断
- build_grid(l2_lines) -> dict|None   重建 N 列 × M 行网格
- detect_and_build(l2_lines) -> dict|None  组合入口（structured_writer 调用）

输出 schema（写入 structured JSON 的可选 "table" 字段）：
{
  "column_count": int,
  "row_count": int,
  "columns": [col_x_mean, ...],          # 左→右
  "rows": [[cell_text, ...], ...],       # 每行按列左→右；缺格用 ""
  "text_row_major": "单元格 · 单元格\n单元格 · 单元格"   # RAG 检索用
}

设计约束（P-OPT 方案 §3.5）：
- 不做表格线检测（Hough）——块坐标已够用
- 不做表头语义识别——"哪列是姓名哪列是年份"交给 LLM
- 散文图必须 0 误判（启发式阈值保守）
"""
from typing import List, Optional

# ---- 启发式阈值（保守：宁可漏判表格，不可误判散文）----
MIN_TABLE_LINES = 16        # 块数 < 16 不可能是表格（散文图 ~10 块）
MIN_TABLE_COLS = 4          # 列数 < 4 不算表格（竖排散文也有 8-10 "列"，
                            # 但散文块是长文本，靠下面的字数阈值排除）
MAX_MEDIAN_CHARS = 6        # 单元格文本中位数 > 6 字 → 散文（整句成块）
MAX_MEDIAN_ASPECT = 3.0     # 块高/宽 中位数 > 3 → 竖排散文列（高瘦条）
ROW_OVERLAP_RATIO = 0.5     # 跨列块 y 区间重叠 > 50% 视为同一表格行


def _box_metrics(line: dict) -> tuple:
    """返回 (x_center, y_center, w, h, text_len)。box 为 4 点多边形。"""
    box = line.get("box") or []
    if len(box) != 4:
        return (0.0, 0.0, 0.0, 0.0, 0)
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    return (
        (min(xs) + max(xs)) / 2,
        (min(ys) + max(ys)) / 2,
        w,
        h,
        len(line.get("text", "")),
    )


def _median(vals: List[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2


def detect_table(l2_lines: List[dict]) -> bool:
    """表格启发式：块多 + 列多 + 单元格短 + 块形近方。

    散文图（竖排古籍）块少（每列 1-3 块）、块是长文本（>10 字）、
    块高瘦（高/宽 >> 3），三个条件全部与表格相反，不会误判。
    """
    if not l2_lines or len(l2_lines) < MIN_TABLE_LINES:
        return False
    metrics = [_box_metrics(l) for l in l2_lines]
    col_ids = {l.get("col_index", 0) for l in l2_lines}
    if len(col_ids) < MIN_TABLE_COLS:
        return False
    median_chars = _median([m[4] for m in metrics])
    if median_chars > MAX_MEDIAN_CHARS:
        return False
    aspects = []
    for _, _, w, h, _ in metrics:
        if w > 0:
            aspects.append(h / w)
    if _median(aspects) > MAX_MEDIAN_ASPECT:
        return False
    return True


def build_grid(l2_lines: List[dict]) -> Optional[dict]:
    """重建表格网格（假设调用方已用 detect_table 确认是表格）。

    步骤：
    1. 按 col_index 分组 → 表格列；每列内按 y 排序
    2. 列按 x_center 升序（左→右）
    3. 跨列行合并：把所有块按 y_center 排序，贪心分组——
       块与当前行的 y 区间重叠 > ROW_OVERLAP_RATIO 则同行
    4. 每行内按列序填格子；行内缺列用 ""
    """
    if not l2_lines:
        return None

    # 1) 列分组
    by_col = {}
    for l in l2_lines:
        by_col.setdefault(l.get("col_index", 0), []).append(l)
    col_ids = sorted(by_col.keys(),
                     key=lambda c: _box_metrics(by_col[c][0])[0])  # 左→右
    col_lines = {c: sorted(by_col[c], key=lambda l: _box_metrics(l)[1])
                 for c in col_ids}
    columns_x = [ _median([_box_metrics(l)[0] for l in col_lines[c]]) for c in col_ids ]

    # 2) 收集全部块的 y 区间
    spans = []  # (y_center, y_min, y_max, col_index, line)
    for c in col_ids:
        for l in col_lines[c]:
            _, yc, _, _, _ = _box_metrics(l)
            box = l.get("box") or []
            ys = [p[1] for p in box] if len(box) == 4 else [yc, yc]
            spans.append((yc, min(ys), max(ys), c, l))
    spans.sort(key=lambda s: s[0])

    # 3) 贪心行分组
    rows = []          # 每项: {"ymin", "ymax", "cells": {col_id: text}}
    for yc, ymin, ymax, c, l in spans:
        placed = False
        for row in rows:
            # y 区间重叠率（相对较短的那个 span）
            overlap = min(row["ymax"], ymax) - max(row["ymin"], ymin)
            shorter = min(row["ymax"] - row["ymin"], ymax - ymin) or 1.0
            if overlap / shorter > ROW_OVERLAP_RATIO:
                row["ymax"] = max(row["ymax"], ymax)
                row["ymin"] = min(row["ymin"], ymin)
                row["cells"].setdefault(c, l.get("text", ""))
                placed = True
                break
        if not placed:
            rows.append({"ymin": ymin, "ymax": ymax, "cells": {c: l.get("text", "")}})

    # 4) 行内按列序展开（缺格 ""）
    grid_rows = []
    for row in rows:
        grid_rows.append([row["cells"].get(c, "") for c in col_ids])
    # 空行过滤（理论上不会出现）
    grid_rows = [r for r in grid_rows if any(cell for cell in r)]

    if not grid_rows:
        return None

    text_row_major = "\n".join(
        " · ".join(cell for cell in r if cell) for r in grid_rows
    )
    return {
        "column_count": len(col_ids),
        "row_count": len(grid_rows),
        "columns": [round(x, 1) for x in columns_x],
        "rows": grid_rows,
        "text_row_major": text_row_major,
    }


def detect_and_build(l2_lines: List[dict]) -> Optional[dict]:
    """组合入口：是表格才重建，否则 None。"""
    if not detect_table(l2_lines):
        return None
    return build_grid(l2_lines)
