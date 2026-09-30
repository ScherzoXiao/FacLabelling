# -*- coding: utf-8 -*-
"""识别与标注方案 · 层 1：金标准解读（2026-09-12）。

**为什么单独一层**（用户 2026-09-12 对「泛用性」的收窄）：

    「泛用性」不是要求整条管线准确识别各类型材料。**解读与规则归纳，应该尽可能
    设计一套完善的算法（内置）；而由这套算法产生的方案，其具体落地过程，
    可以做成基于用户 agent 可拆卸的。**

    故本模块 = 「解读」这一环，**只记录观测，不推断规则**（推断属层 2）。
    拆开的意义：解读是否准确可以**单独检验**（观测能否与原图对上），
    而不必等端到端效果出来才知道。产物 schema 见
    设计文档 §3。

**本层补的关键观测 = 横带结构（bands）**。既有 `layout_contract` 的 G 层有
`regions.title_y_range`，但**没有形式化为"页面的横带划分"**；而实测（同文档 §4）
表明：框位置错的主因不是像素算错，而是**块级模型缺"标题带 / 正文带"这一层结构**。

**双源交叉**（红线：源同则校无效）：
  - 源 1 = **图像行剖面**（主源：带间分隔在物理上真实存在，或为空白、或为横线）
  - 源 2 = **金标准框的行覆盖**（副源：反映"人认为哪些行有内容"）
两者独立；一致与否都写入产物，**不做静默合并**。

实测要点（2026-09-12，样例页 0001/0002/0003）：
  - 带间分隔是**浅色横线**（0001 y583-584 darkfrac 0.79–0.93），不是空白；
    所以"空白判据"会漏检 → 行分类必须三态 `content / sparse / rule`。
  - 0001 的带间**被一条"表头"框跨过**（`y[465,822]`），故金标准覆盖不断开；
    这类跨带框要**标记**而不是让它抹掉带结构（`spans_bands`）。

纪律：零 API、只读；图像缺失 → 降级为"仅金标准"，绝不阻断。
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import layout_contract as LC  # noqa: E402
import rule_learn as RL       # noqa: E402

log = logging.getLogger("gold_facts")

SCHEMA_VERSION = "0.1.0"
DEFAULT_OUT_DIR = ROOT / "data" / "plan"
OUTBOX_DIR = ROOT / "outbox"

MIN_BAND_GAP = 5        # 带间分隔的最小行数（小于此算字间空，不切带）
CONTENT_MIN = 0.06      # 行"有字"的墨迹占比下限
RULE_RUN_RATIO = 0.08   # 贯通横线判据：行内最长连续暗段 ≥ 此比例 × 采样宽
RULE_FRAC_MIN = 0.10    # 贯通横线判据：墨迹占比下限
CLOSE_MAX = 14          # 形态闭运算：≤ 此长度的**纯稀疏**缝算字间空，不算分隔
ATTACH_TOL = 2          # 框中心距带边界多近算"跨带"

# ---------------------------------------------------------------- 质量分级（P0-1，2026-09-13）
#
# **为什么需要**：本模块原先只有一条排除路径 —— `无可用标注框`。于是 1 条 / 2 条标注的页
# **照样进版式族归纳**，实测 7 页里 5 个"版式族"有 3 个由这类页定义：
# 一个只有 1 个框的"族"对任何别的页都没有预测力，却会污染跨页统计并让 degraded 恒告警。
#
# **判据为什么是两条轴**（几何 × 语义），而不是只数框数：
#   · 几何轴 —— 框太少就画不出列网格 / 带边界（版式族的原料）；
# · 语义轴 —— **有框不等于标了属性**。实测「某张截图页」有 29 个框，
#     但 jsonl 里**根本没有 `attr` 字段**（只画框、未标属性），它无法参与
#     锚词/属性集的归纳。只数框数会把它判成 ok —— 那正是这轮要修的东西。
#
# **阈值不是拟合出来的**（这点必须能自证）：阈值扫描显示
# 几何轴取 3–29、语义轴取 2–10 都得到**同一个可用页集合**。
#   取 8 / 2 是这两个区间里最好解释的值：
# 8 ≈ 名录式版面**一整条记录**的字段数下限（公司名/类别/资本/注册时间/… ；
# 实测样例三页为 51/49/64 框），2 = "属性"这个概念存在所需的最少种类数
#      （只有 1 种时无法把"标签"与"标题"区分开）。
QUALITY_MIN_BOXES = 8       # 几何轴：≥ 此框数才能定义列网格 / 带边界
QUALITY_MIN_ATTR_KINDS = 2  # 语义轴：≥ 此属性种类数才看得出"属性"这一层
QUALITY_LEVELS = ("ok", "thin", "empty")


def grade_quality(n_boxes: int, n_attr_kinds: int) -> str:
    """金标准页的质量三档。**判据只有这一处**（`excluded` 由它派生，不得另判）。

    - `ok`    ：两条轴都过 —— 可参与版式族归纳与规则提取
    - `thin`  ：有框但证据不足（过少、或一个属性都没标）—— **不进 usable**，观测仍留档
    - `empty` ：无可用框

    ★ 返回的是**分级**而不是布尔：Dingo（MinerU 的标注复核工具）的要点之一是
    "输出分级而非二值" —— 二值判完就只剩一句"不合格"，无从审计"为什么"。
    """
    if n_boxes <= 0:
        return "empty"
    if n_boxes < QUALITY_MIN_BOXES or n_attr_kinds < QUALITY_MIN_ATTR_KINDS:
        return "thin"
    return "ok"


def _quality_note(level: str, n_boxes: int, n_attr_kinds: int) -> str:
    """人话理由（跟 `level` 一起进 facts，供审计与界面显示）。"""
    if level == "empty":
        return "无可用标注框"
    if level == "thin":
        lack = []
        if n_boxes < QUALITY_MIN_BOXES:
            lack.append(f"框 {n_boxes} < {QUALITY_MIN_BOXES}")
        if n_attr_kinds < QUALITY_MIN_ATTR_KINDS:
            lack.append(f"属性种类 {n_attr_kinds} < {QUALITY_MIN_ATTR_KINDS}")
        return "证据不足以定义版式（" + "；".join(lack) + "）"
    return f"框 {n_boxes}、属性 {n_attr_kinds} 种 —— 可参与归纳"


# ---------------------------------------------------------------- 区间工具

def _cover(boxes: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not boxes:
        return []
    iv = sorted((min(a, b), max(a, b)) for a, b in boxes)
    out = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= out[-1][1] + 0.5:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(float(a), float(b)) for a, b in out]


def _gaps_of(cover: List[Tuple[float, float]], lo: float, hi: float,
             min_gap: float = MIN_BAND_GAP) -> List[Tuple[float, float]]:
    out, cur = [], lo
    for a, b in cover:
        if a - cur >= min_gap:
            out.append((float(cur), float(a)))
        cur = max(cur, b)
    if hi - cur >= min_gap:
        out.append((float(cur), float(hi)))
    return out


def _cluster_columns(xs: List[float], tol: float) -> List[Tuple[float, float]]:
    """一维聚类 → [(xmin, xmax)]，从右到左（RTL 阅读序）。"""
    if not xs:
        return []
    s = sorted(xs)
    groups = [[s[0], s[0]]]
    for v in s[1:]:
        if v - groups[-1][1] <= tol:
            groups[-1][1] = v
        else:
            groups.append([v, v])
    return [(float(a), float(b)) for a, b in sorted(groups, key=lambda g: -g[0])]


# ---------------------------------------------------------------- 图像行剖面

def row_profile(gray, x0: int, x1: int, y0: int, y1: int):
    """区域逐行统计 → (frac, maxrun, y0)。

    **为什么除墨迹占比外还要"行内最长连续暗段"**：带间分隔在官方档里是**浅色横线**
    （占页比仅 0.16–0.41），与文字行的墨迹占比区间重叠（实测 0001 y=577 文字行 0.064
    vs y=581 横线 0.268），单靠占比判不开。而横线的**连续段接近全宽**
    （0001 达 0.72W、0002 0.14W），文字行最长只到 **0.05W** —— 这个量区分得很干净。
    """
    import numpy as np
    x0 = max(0, int(x0)); x1 = min(gray.shape[1], int(x1))
    y0 = max(0, int(y0)); y1 = min(gray.shape[0], int(y1))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return [], [], y0
    dark = gray[y0:y1, x0:x1] < 128
    frac = dark.mean(axis=1)
    runs = np.zeros(len(frac), dtype=int)
    best = 0
    for i, row in enumerate(dark):
        cur = 0
        for v in row:
            cur = cur + 1 if v else 0
            if cur > best:
                best = cur
        runs[i] = best
        best = 0
    return [float(v) for v in frac], [int(v) for v in runs], y0


def classify_rows(frac: List[float], runs: List[int], width: int,
                  min_run: float = 0.0) -> List[str]:
    """行三态：`content` / `sparse` / `rule`。

    - `rule`：行内连续暗段够长（贯通横线），与墨迹占比无关；判据取
      `runs >= max(RULE_RUN_RATIO × 采样宽, min_run)` —— `min_run` 是**绝对尺度下限**：
      横线要跨越多列，其连续段必然**长于一个字的框宽**。
      坑：若采样区间只覆盖一个字的宽度（例如标注框远窄于版面），
      字身会把整条采样带填满而被误判成横线 → 必须有下限兜住。
    - `sparse`：字与字的缝隙、被噪声抬起的行 —— **单列出来**是关键，
      否则带间分隔会被这些行切碎。
    """
    out = []
    need = max(RULE_RUN_RATIO * max(1, width), min_run)
    for v, r in zip(frac, runs):
        if r >= need and v >= RULE_FRAC_MIN:
            out.append("rule")
        elif v >= CONTENT_MIN:
            out.append("content")
        else:
            out.append("sparse")
    return out


def _close_sparse(kinds: List[str], max_gap: int = CLOSE_MAX) -> List[str]:
    """形态闭运算：≤ max_gap 的短稀疏缝并入 content（两侧都非稀疏才算缝）。

    **不跨 rule**：横线是硬分隔，闭运算绝不能把它填掉（实测横线只有 3~4 行，
    若不设此限，一切带结构都会被淹没）。
    """
    out = list(kinds)
    i, n = 0, len(out)
    while i < n:
        if out[i] != "sparse":
            i += 1
            continue
        j = i
        while j + 1 < n and out[j + 1] == "sparse":
            j += 1
        left = out[i - 1] if i > 0 else None
        right = out[j + 1] if j + 1 < n else None
        if ((j - i + 1) <= max_gap
                and left in ("content", "rule") and right in ("content", "rule")):
            for k in range(i, j + 1):
                out[k] = "content"
        i = j + 1
    return out


def find_separators(kinds: List[str], y0: int,
                    min_len: int = MIN_BAND_GAP) -> List[dict]:
    """非 content 的连续行段 → 带间候选分隔（含形态）。"""
    out, i, n = [], 0, len(kinds)
    while i < n:
        if kinds[i] == "content":
            i += 1
            continue
        j = i
        while j + 1 < n and kinds[j + 1] != "content":
            j += 1
        seg = kinds[i:j + 1]
        rule_rows = [y0 + k for k, v in zip(range(i, j + 1), seg) if v == "rule"]
        # 含横线的段**不受最小长度限制**：横线是结构线，几行也算分隔
        # （实测样例页 0001 的带间横线只有 4 行，被长度判据丢掉过）
        if (j - i + 1) < min_len and not rule_rows:
            i = j + 1
            continue
        out.append({
            "y": [y0 + i, y0 + j + 1], "len": j - i + 1,
            "kind": "rule" if rule_rows else "blank",
            "rule_rows": [rule_rows[0], rule_rows[-1] + 1] if rule_rows else None,
        })
        i = j + 1
    return out


def _strong(seps: List[dict], page_h: int) -> List[dict]:
    """够格切带的分隔：含**横线**（结构性）或空白足够长。"""
    min_len = max(16, int(0.02 * page_h))
    return [s for s in seps if s["kind"] == "rule" or s["len"] >= min_len]


def boundary_of(sep: dict) -> float:
    """分隔 → 带边界。有横线就取横线中点（横线才是真正的结构线）。"""
    if sep.get("rule_rows"):
        a, b = sep["rule_rows"]
        return (a + b) / 2.0
    return (sep["y"][0] + sep["y"][1]) / 2.0


# ---------------------------------------------------------------- 金标准读入

def load_gold_boxes(ann_dir: Optional[Path] = None,
                    profile_id: Optional[str] = None) -> Dict[str, List[dict]]:
    """`manual_annotations/*.jsonl` → {stem: [原始行]}。

    与 `rule_learn.load_gold` 的差别：**保留 attr 为空的行**。它们是真值框，
    只是没归到属性上；解读阶段丢掉会让横带/列的观测失真
    （实测样例页 0001 的 51 条真值框里有 17 条 attr 为空）。

    ★ 2026-09-16 两处改动（「项目类别隔离」）：
      · **解析收归 `rule_learn.read_gold_raw`** —— 原先这里另写了一份 jsonl 解析，
        与 `load_gold` 各解析一遍。两处解析本身就有漂移风险，更关键的是
        **隔离过滤只要漏在任一处就等于没隔离**。
      · 新增 `profile_id`：非空 ⇒ **只读该档案的行**。「本档案的列宽 / 横带 /
        页边距」必须只由本档案的例题决定，否则换一套材料时先验直接是错的。
    """
    raw = RL.filter_by_profile(RL.read_gold_raw(ann_dir), profile_id)
    out: Dict[str, List[dict]] = {}
    for stem, rows in raw.items():
        keep = [r for r in rows if LC.rect_of_box(r.get("box"))]
        if keep:
            out[stem] = keep
    return out


# ---------------------------------------------------------------- 单页解读

def _boxes_of(rows: List[dict]) -> List[dict]:
    bs = []
    for r in rows:
        b = LC.rect_of_box(r.get("box"))
        if not b:
            continue
        y0, y1 = min(b[1], b[3]), max(b[1], b[3])
        x0, x1 = min(b[0], b[2]), max(b[0], b[2])
        bs.append({"attr": r.get("attr"), "text": (r.get("text") or "").strip(),
                   "y0": y0, "y1": y1, "cy": (y0 + y1) / 2.0,
                   "cx": (x0 + x1) / 2.0, "h": y1 - y0, "w": x1 - x0,
                   "x0": x0, "x1": x1})
    return bs


def read_page(stem: str, rows: List[dict], outbox_dir: Path) -> dict:
    boxes = _boxes_of(rows)
    if not boxes:
        return {"stem": stem, "excluded": True, "reason": "无可用标注框",
                "quality": {"level": "empty", "n_boxes": 0, "n_attr_kinds": 0,
                            "note": _quality_note("empty", 0, 0)}}

    # ---- 质量分级（P0-1）：`excluded` **由分级派生**，不另立判据 ——
    #   两处各判一次必然漂移（"逐页判据"与"归档判据"不一致是本项目踩过的坑）。
    #   ★ thin 页**照常算完所有观测**（列/带/字格），只是不进 `usable`：
    #     审计要能回答"为什么判它 thin"，靠的就是这些观测。
    n_attr_kinds = len({b["attr"] for b in boxes if b.get("attr")})
    level = grade_quality(len(boxes), n_attr_kinds)
    note = _quality_note(level, len(boxes), n_attr_kinds)
    fact: Dict[str, Any] = {
        "stem": stem,
        "excluded": level != "ok",
        "reason": None if level == "ok" else note,
        "quality": {"level": level, "n_boxes": len(boxes),
                    "n_attr_kinds": n_attr_kinds, "note": note},
        "n_annotations": len(boxes),
        "n_attr_missing": sum(1 for r in rows if not r.get("attr")),
        "image": None,
        "image_source": "none",
    }

    # ---- 阅读序（投票，与契约同口径）
    votes = Counter()
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if abs(a["cy"] - b["cy"]) <= 12 and abs(a["cx"] - b["cx"]) > 20:
                votes["anchor_dx_neg" if a["cx"] > b["cx"] else "anchor_dx_pos"] += 1
            if abs(a["cx"] - b["cx"]) <= 12 and abs(a["cy"] - b["cy"]) > 20:
                votes["same_attr_top_to_bottom"] += 1
    fact["reading"] = {
        "order": ("rtl_ttb" if votes.get("anchor_dx_neg", 0) >= votes.get("anchor_dx_pos", 0)
                  else "ltr_ttb"),
        "votes": dict(votes),
    }

    gold_lo = min(b["y0"] for b in boxes)
    gold_hi = max(b["y1"] for b in boxes)
    cov = _cover([(b["y0"], b["y1"]) for b in boxes])
    gold_gaps = _gaps_of(cov, gold_lo, gold_hi)

    # ---- 字格度量
    hs = sorted(b["h"] for b in boxes if b["h"] > 0)
    wsx = sorted(b["w"] for b in boxes if b["w"] > 0)
    cell_h = hs[len(hs) // 2] if hs else None
    fact["char_metrics"] = {"cell_h_median": cell_h,
                            "box_w_median": wsx[len(wsx) // 2] if wsx else None,
                            "n_samples": len(hs)}

    # ---- 列（金标准框 x 中心聚类）
    tol = max(6.0, (fact["char_metrics"]["box_w_median"] or 40.0) * 0.5)
    cols = _cluster_columns([b["cx"] for b in boxes], tol)
    col_facts = []
    for ci, (cxa, cxb) in enumerate(cols):
        inside = sorted([b for b in boxes if cxa - 1 <= b["cx"] <= cxb + 1],
                        key=lambda b: b["y0"])
        if not inside:
            continue
        col_facts.append({
            "id": ci,
            "x_center": [round(cxa, 1), round(cxb, 1)],
            "x_span": [round(min(b["x0"] for b in inside), 1),
                       round(max(b["x1"] for b in inside), 1)],
            "n_boxes": len(inside),
            "y": [round(inside[0]["y0"], 1), round(max(b["y1"] for b in inside), 1)],
            "head": {"text": inside[0]["text"][:24], "attr": inside[0]["attr"],
                     "y0": round(inside[0]["y0"], 1), "h": round(inside[0]["h"], 1)},
            # `str(None)` 会得到字符串 "None"（真值，过滤不掉）→ 必须先判空再转
            "attr_seq": [str(b["attr"]) for b in inside if b.get("attr")],
        })
    fact["columns"] = col_facts

    # ---- 横带：主源 = 图像分隔；副源 = 金标准覆盖
    bs: Dict[str, Any] = {
        "gold_gaps": [[round(a, 1), round(b, 1)] for a, b in gold_gaps],
        "gold_cover": [[round(a, 1), round(b, 1)] for a, b in cov],
        "image_separators": [],
        "strong_separators": [],
        "image_sampled_x": None,
        "primary": "image" if (outbox_dir / f"{stem}.png").exists() else "gold",
        "agree": None,
        "agree_detail": [],
    }
    fact["band_source"] = bs
    boundaries: List[float] = []

    img = outbox_dir / f"{stem}.png"
    if img.exists():
        try:
            import numpy as np
            from PIL import Image
            with Image.open(img) as im:
                gray = np.array(im.convert("L"))
            fact["image"] = {"w": int(gray.shape[1]), "h": int(gray.shape[0])}
            fact["image_source"] = "outbox"
            x0 = int(max(0, min(b["x0"] for b in boxes)))
            x1 = int(max(b["x1"] for b in boxes))
            bs["image_sampled_x"] = [x0, x1]
            frac, runs, fy0 = row_profile(gray, x0, x1,
                                          int(gold_lo) - 24, int(gold_hi) + 40)
            # 横线的绝对尺度下限 = 1.6 × 字框宽（横线跨多列，必长于一个字）
            min_run = 1.6 * (fact["char_metrics"]["box_w_median"] or 40.0)
            kinds = _close_sparse(classify_rows(frac, runs, x1 - x0, min_run))
            bs["rule_min_run"] = round(min_run, 1)
            seps = find_separators(kinds, fy0)
            bs["image_separators"] = seps
            # 强分隔 = 够格切带的（含横线，或空白足够长）。
            # ★ 分两个用途记录，别混：
            #   - band_boundaries：**页内切带**用 → 必须夹在金标准覆盖区间内，
            #     否则 edges 会与 gold_lo/gold_hi 交叠产生退化带。
            #   - strong_separators：**版式观测**用 → 全部保留，不夹取。
            # 这个区分是实测逼出来的：样例页 0002 的标题带顶线（rel≈0.17）被收、
            # 0001/0003 的同一根线被夹掉（三页同版式，gold_lo 差 0.5px 就翻面），
            # 导致跨页归纳时"同一条结构线只出现在一页"。观测不该有边缘敏感。
            strong = _strong(seps, gray.shape[0])
            bs["strong_separators"] = [
                {"boundary": round(boundary_of(s), 1),
                 "kind": s["kind"],
                 "len": s["len"],
                 "rel": round(boundary_of(s) / float(gray.shape[0]), 4),
                 "in_gold": bool(gold_lo < boundary_of(s) < gold_hi)}
                for s in strong
            ]
            for s in strong:
                if gold_lo < boundary_of(s) < gold_hi:
                    boundaries.append(boundary_of(s))
            # 一致判据：金标准空段与图像分隔**有交集**即算互相印证
            detail = []
            for ga, gb in gold_gaps:
                hit = [s for s in seps if s["y"][1] > ga and s["y"][0] < gb]
                detail.append({"gap": [round(ga, 1), round(gb, 1)],
                               "gap_mid": round((ga + gb) / 2, 1),
                               "image_sep": [s["y"] for s in hit],
                               "boundary": [round(boundary_of(s), 1) for s in hit],
                               "err_px": ([round(abs(boundary_of(hit[0]) - (ga + gb) / 2), 1)]
                                          if hit else None)})
            bs["agree_detail"] = detail
            bs["agree"] = (all(d["image_sep"] for d in detail) if detail else None)
        except Exception as e:                                   # pragma: no cover
            fact["image_source"] = f"error:{type(e).__name__}"

    if not boundaries:                       # 图像不可用 → 退回金标准覆盖断点
        boundaries = [(a + b) / 2.0 for a, b in gold_gaps]

    # ---- 用边界切带，并把金标准框挂到带上
    edges = [gold_lo] + sorted(boundaries) + [gold_hi]
    bands = []
    for i in range(len(edges) - 1):
        a, b = edges[i], edges[i + 1]
        if b - a < 4:
            continue
        inside = [x for x in boxes if a - ATTACH_TOL <= x["cy"] <= b + ATTACH_TOL]
        if not inside:
            continue
        spanning = [x for x in boxes
                    if x["y0"] < a - ATTACH_TOL < x["y1"] or x["y0"] < b + ATTACH_TOL < x["y1"]]
        bands.append({
            "id": len(bands),
            "y": [round(min(x["y0"] for x in inside), 1),
                  round(max(x["y1"] for x in inside), 1)],
            "y_envelope": [round(a, 1), round(b, 1)],
            "n_boxes": len(inside),
            "attrs": sorted({str(x["attr"]) for x in inside if x["attr"]}),
            "spans_bands": sorted({x["text"][:16] for x in spanning}),
        })
    fact["bands"] = bands
    fact["band_boundaries"] = [round(v, 1) for v in sorted(boundaries)]
    return fact


# ---------------------------------------------------------------- 归档

def build_facts(profile_id: str, ann_dir: Optional[Path] = None,
                outbox_dir: Optional[Path] = None) -> dict:
    ann_dir = ann_dir or RL.DEFAULT_GOLD_DIR
    outbox_dir = outbox_dir or OUTBOX_DIR
    # ★ 隔离（2026-09-16）：`profile_id` 原先**只用于输出文件名**，读的金标准是
    # 整个目录 —— 实测把档案名换成 `prof_FAKE_示例`，读到的仍是那 7 页。
    # ⇒ 换一套材料时，该批的列宽 / 横带 / 页边距会直接变成另一批材料的先验。
    #   现在 `profile_id` 真正参与读取：只有本档案的例题进观测。
    gold = load_gold_boxes(ann_dir, profile_id=profile_id)
    pages = [read_page(st, gold[st], outbox_dir) for st in sorted(gold)]
    usable = [p for p in pages if not p.get("excluded")]
    # 质量分档计数（P0-1）：让"有几页因证据不足被挡在归纳之外"成为**可读的读数**，
    # 而不是只能翻逐页 facts 才知道。`thin` 页仍留在 `pages` 里（观测不丢，供审计）。
    levels = Counter((p.get("quality") or {}).get("level") or "empty" for p in pages)
    return {
        "_schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source": {"ann_dir": str(ann_dir), "n_pages": len(pages),
                   "n_usable": len(usable),
                   # ★ 可见性：这次**吃进了什么、排除了什么**（隔离若不落读数，
                   #   下一个人只会看到"页数怎么少了"，无从归因）
                   "gold_scope": RL.gold_scope_of(
                       RL.read_gold_raw(ann_dir), profile_id),
                   "quality": {lv: int(levels.get(lv, 0)) for lv in QUALITY_LEVELS}},
        "pages": pages,
        "cross_page": _cross_page(usable),
    }


def _cross_page(pages: List[dict]) -> dict:
    """跨页稳定性：只有**稳定**的观测才有资格进方案（否则是过拟合单页）。"""
    import statistics as st
    band_counts = [len(p["bands"]) for p in pages]
    rel_bounds, gap_px = [], []
    for p in pages:
        h = (p.get("image") or {}).get("h")
        if h and len(p["bands"]) >= 2:
            for b in p["bands"][:-1]:
                rel_bounds.append(round(b["y_envelope"][1] / h, 3))
        for a, b in p["band_source"]["gold_gaps"]:
            gap_px.append(round(b - a, 1))
    agree_vals = [p["band_source"].get("agree") for p in pages
                  if p["band_source"].get("agree") is not None]
    return {
        "n_pages": len(pages),
        "band_count": {"values": band_counts,
                       "mode": (Counter(band_counts).most_common(1)[0][0]
                                if band_counts else None)},
        "band_boundary_rel_y": {
            "values": rel_bounds,
            "sd": round(st.pstdev(rel_bounds), 4) if len(rel_bounds) > 1 else None},
        "inter_band_gap_px": {"values": gap_px, "min": min(gap_px) if gap_px else None},
        "agree": {"evaluated": len(agree_vals),
                  "all_true": all(agree_vals) if agree_vals else None,
                  "values": agree_vals},
    }


def save(facts: dict, out_dir: Optional[Path] = None) -> Path:
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"facts_{facts['profile_id']}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def facts_exit_code(rc: int, facts) -> int:
    """唯一判定点：底层 rc==0 **且** 产物可解析 ⇒ 0，否则 1。

    `chronicles facts` 取用本函数，包装层不重判（同 `triage_exit_code` 模式）。
    `facts` 传包装层已读回的产物（None = 读不到 / 不是合法 JSON）。
    """
    return 0 if (rc == 0 and facts is not None) else 1


def _main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="层 1：金标准解读 → facts.json")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", default=None)
    # ★ 2026-09-16：对齐 `learn` 的既有选项名 `--gold`（同一件事用同一个名字）
    ap.add_argument("--gold", dest="gold_dir", default=None,
                    help="金标准目录（默认 manual_annotations/）")
    a = ap.parse_args(argv)
    facts = build_facts(a.profile, ann_dir=Path(a.gold_dir) if a.gold_dir else None)
    p = save(facts, Path(a.out) if a.out else None)
    cp = facts["cross_page"]
    print(f"[facts] {p}")
    q = facts["source"].get("quality") or {}
    print(f"  可用页 {facts['source']['n_usable']}/{facts['source']['n_pages']}"
          f"（ok {q.get('ok', 0)} / thin {q.get('thin', 0)} / empty {q.get('empty', 0)}）")
    gs = facts["source"].get("gold_scope") or {}
    if gs:
        # ★ 隔离必须**看得见**：排除掉的行数直接印在人面上，不藏进 JSON
        print(f"  金标准范围 [{gs.get('scope')}] 本档案 "
              f"{gs.get('n_pages_kept')} 页 / {gs.get('n_rows_kept')} 行"
              f" ｜ 排除他档案 {gs.get('n_rows_other_profile')} 行"
              f"（{gs.get('n_pages_other_profile')} 页）"
              f" ｜ 排除未归档 {gs.get('n_rows_unassigned')} 行"
              f"（{gs.get('n_pages_unassigned')} 页）")
    print(f"  横带数 {cp['band_count']}")
    print(f"  带边界相对页高 {cp['band_boundary_rel_y']}")
    print(f"  带间跨度 px {cp['inter_band_gap_px']}")
    print(f"  两源一致 {cp['agree']}")
    for pg in facts["pages"]:
        lv = (pg.get("quality") or {}).get("level") or "?"
        if pg.get("excluded"):
            # 逐页仍打印**为什么**被判 thin —— 分级的价值就在这一行（否则"不合格"不可审）
            print(f"  - {pg['stem'][-22:]:24s} [{lv}] 不进归纳：{pg.get('reason')}")
            continue
        bsrc = pg["band_source"]
        seps = ", ".join(f"[{s['y'][0]},{s['y'][1]}]{s['kind']}"
                         for s in bsrc["image_separators"])
        print(f"  - {pg['stem'][-22:]:24s} [{lv}] 框{pg['n_annotations']:3d}"
              f"(空属性{pg['n_attr_missing']:2d}) "
              f"列{len(pg['columns']):3d} 带边界{pg['band_boundaries']} 主源={bsrc['primary']}")
        print(f"      金标空段 {bsrc['gold_gaps']}  一致={bsrc['agree']}")
        print(f"      图像分隔 {seps}")
        for b in pg["bands"]:
            print(f"      带{b['id']} y{b['y_envelope']} 框{b['n_boxes']:3d} "
                  f"跨带框={b['spans_bands']} 属性={b['attrs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
