"""Cluster OCR boxes into vertical columns for traditional Chinese texts.

Why this exists:
- PaddleOCR returns lines in a "natural reading order" it guesses
- For traditional Chinese vertical text, this guess is wrong (it reads L→R T→B)
- Clustering OCR boxes by x_center recovers the column structure
  (历史上有过"图像竖线检测"方案，已废弃，见 M22 清理注释)
"""
import logging
from typing import List, Dict

# ✅ 2026-08-19 阶段二 10：用 logging 替代 print
# 之前 [debug] print 直接刷到 stdout，污染生产输出
# 现在走 logging.getLogger("local_chronicles_ocr").debug()，可通过日志级别控制
log = logging.getLogger("local_chronicles_ocr")


# ✅ M22 清理（2026-09-01）：detect_column_dividers / assign_box_to_column /
# reorganize_ocr_output 零引用，已删。历史上"图像竖线检测"方案被
# find_columns_from_ocr_boxes（纯 OCR 框聚类）替代，保留前者只会拖累 cv2 依赖面。


def find_columns_from_ocr_boxes(ocr_lines: List[Dict], x_tolerance: int = 30) -> List[List[Dict]]:
    """Cluster OCR boxes into columns using x_center proximity.

    Strategy: greedy nearest-neighbor clustering. Boxes within x_tolerance
    of an existing column's mean x_center are merged into it.
    """
    if not ocr_lines:
        return []

    # Each column: [mean_x_center, [lines]]
    columns = []

    for line in ocr_lines:
        xs = [p[0] for p in line['box']]
        x_center = (min(xs) + max(xs)) / 2

        # Find closest existing column within tolerance
        best_idx = -1
        best_dist = float('inf')
        for i, (col_x, col_lines) in enumerate(columns):
            dist = abs(x_center - col_x)
            if dist < x_tolerance and dist < best_dist:
                best_idx = i
                best_dist = dist

        if best_idx >= 0:
            col_x, col_lines = columns[best_idx]
            col_lines.append(line)
            # Update mean
            all_x = [sum(p[0] for p in l['box']) / 4 for l in col_lines]
            columns[best_idx][0] = sum(all_x) / len(all_x)
        else:
            columns.append([x_center, [line]])

    # Sort each column top to bottom
    for col_x, col_lines in columns:
        col_lines.sort(key=lambda l: l['box'][0][1])

    # ✅ P-OPT-1（2026-08-19）：按列 x_center 降序排列（右→左，竖排阅读顺序）
    # 旧版 result.reverse() 依赖输入顺序（PaddleOCR 恰好按左→右返回才碰巧正确）；
    # VL 后端的块顺序不保证，改为显式按坐标排序，两个后端都确定正确。
    columns.sort(key=lambda c: c[0], reverse=True)
    result = [col_lines for _, col_lines in columns]
    return result

