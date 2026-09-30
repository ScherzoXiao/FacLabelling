# -*- coding: utf-8 -*-
"""零 API 几何对评 —— **度量口径的唯一实现**（B2，2026-09-21）。

## 为什么必须有这个模块

P5 的第 ④ 道门（批量采纳安全门）判据是「档案级**独立回验读数**」，而
`adjudicate.VALIDATION_METRICS = ("value_aligned_iou50", "gold_iou50", "manual")`
此前**只有名字、没有实现**（`adjudicate.py` 原文：*只登记名字，不在此实现度量
——度量实现属离线对评工具*）。本模块就是那个"离线对评工具"。

⇒ 没有它，`data/validation/` 永远空着，第 ④ 道门是一道**死门**。

## 三条口径（★★ 分母一律取**金标准侧**，不在预测侧筛）

设金标准有 `N` 个"有效框"。★ `N` 的**定义**见下节「分母口径」——
默认 `N` = **有属性名**的条目数（对称分母 B，2026-09-21 裁定）。对每个框 `q`：

| 量 | 定义 | 含义 |
|---|---|---|
| `value_match_rate` | 「存在预测条目，其值文本与 `q` 的值同」的框数 / `N` | **语义对齐率** |
| `geometry_iou50` | 在上一条已成立的框里，「存在预测框 IoU≥0.5」的比例 | **条件几何质量** |
| `value_aligned_iou50` | 两条同时成立的框数 / `N` | **端到端框级命中率** |

三者关系：`value_aligned_iou50 ≈ value_match_rate × geometry_iou50`。
★ 这个分解是本模块存在的主要理由 —— 它把「读数低」归因到**两个互斥的失败源**
（值没读出来 / 框没对上），而旧口径把两者混成一个数。

### 与旧口径的差别（★ 会改历史读数的含义）

`record_aligned_iou50`（对照用，`score_record_aligned` 实现）的分母是
**预测侧条目数** ⇒ 预测没产出的条目**根本不进分母** ⇒ **系统性虚高**。
实测：同一页 `金标准页` 旧口径 **0.750**、
值对齐口径 **0.227**（差 3.3 倍）。

⇒ **准入语境必须用值对齐口径**：与项目既有的 fail-closed 纪律一致
（"没测过"≠"没问题"；"没读出来"必须算不中）。

### ★★ 分母口径：`N` 只算**有属性名**的条目（2026-09-21 用户裁定改 B）

证据：3 折留出页 145 条里 **45 条无 `attr`**，
其中 **33 条是「同一属性的相邻段 / 标签」**（与某个有 `attr` 的金标准框**同列、
y 间隙 0–6 px**，如 `分销处在…` 是 `总公司地址` 的第二段），10 条是**版面结构**
（报头 `样例材料` / 栏目 / 期号）。而产品（`anchor_page`）按**属性**输出、
人工标注按**视觉行**切 ⇒ 把"段"计入分母是**两侧粒度不一致**（审计 C 那一类标尺失真），
会给读数加 **31% 死区**（产品原理上不可能命中）。

| 口径 | 分母 | 实测（3 折） | 用途 |
|---|---|---|---|
| **B 对称** | 有属性名的条目 | `15/100 = 0.1500` | **主口径**（本模块默认，进 `data/validation/`） |
| A 含无属性名 | 全部标注条目 | `16/145 = 0.1103` | **附注**（`metrics_all_entries`），保住「段覆盖」维可见 |

★ A 口径**不废弃**，但**不是准入读数** —— 并列报粒度是本模块的硬纪律
（同名指标只报一个数，必被读成"退步/进步"）。
⚠ 换口径改变历史读数的含义 ⇒ **必须同批重登记 `data/validation/`**（2026-09-21 已做）。
⚠ `DENOM_SYMMETRIC` 是**单点开关**；`PASS_HINT_END_TO_END` 是**建议线**（人读视图只读它）。

## 零 API

全部输入来自已落盘产物：`data/structured`（几何层）+ `manual_annotations`（金标准）
+ `data/divergence/*`（VLM records，可换 oracle 上界对照）。不发起任何网络调用。
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("align_eval")

_BASE = Path(__file__).resolve().parent
if str(_BASE) not in sys.path:                     # 允许被 `_probe/` 下的脚本直接跑
    sys.path.insert(0, str(_BASE))

import layout_contract as LC                        # noqa: E402

DEFAULT_ANN_DIR = _BASE / "manual_annotations"
DEFAULT_STRUCTURED_DIR = _BASE / "data" / "structured"
DEFAULT_OUTBOX = _BASE / "outbox"
DEFAULT_DIVERGENCE_DIR = _BASE / "data" / "divergence"
DEFAULT_CONTRACTS_DIR = _BASE / "data" / "layout_contracts"

SCHEMA_VERSION = 1
#: IoU 命中阈值。★ 0.5 是**口径的一部分**，不随页而调 —— 调它等于改历史读数的含义。
IOU_THRESHOLD = 0.5
DEFAULT_ANCHOR = "公司名"

#: 报告出的度量名（与 `adjudicate.VALIDATION_METRICS` 取交集的那个必须逐字一致）
METRIC_END_TO_END = "value_aligned_iou50"
METRIC_VALUE_MATCH = "value_match_rate"
METRIC_GEOMETRY = "geometry_iou50"
#: 对照口径。**不得用于准入**（分母在预测侧 ⇒ 虚高，见模块 docstring）
METRIC_LEGACY = "record_aligned_iou50"

#: ★★ **分母口径**（2026-09-21 用户裁定改 B）
#: - 主口径 = **只算有属性名**的金标准条目（**对称分母**）。
#:   理由：3 折 145 条里 **45 条无 `attr`**，其中 **33 条是「同一属性的相邻段」**
#:   （与有 `attr` 的框**同列、y 间隙 0–6 px**），10 条是版面结构（报头/栏目/期号）。
#:   产品（`anchor_page`）按**属性**输出，标注按**视觉行**切 ⇒
#:   把"段"计进分母是**两侧粒度不一致**（审计 C），不是产品缺陷。
#:   量级：A 16/145 = **0.1103** → B 15/100 = **0.1500**。
#: ⚠ 改这一处会**改变历史读数的含义** ⇒ 必须同批重登记 `data/validation/`。
DENOM_SYMMETRIC = True
#: **附注口径（A）**：分母含无属性名条目 —— 由 `eval_page` **并列报出**，
#: 以保住「属性**段覆盖**」这一维可见（skill：同名指标必须**并列报粒度**）。
#: ★ 它**不是**准入读数，不参与 `data/validation/`。
METRIC_END_TO_END_A = "value_aligned_iou50_allentries"
#: 「建议通过」的端到端线。**人读视图只读它、自己不写数**（单一真相）。
#: ⚠ 它只影响**建议文案**；真正的准入是 `data/validation/` 上的 `verdict`（由人定的）。
PASS_HINT_END_TO_END = 0.5


# ============================================================
# 几何基元（唯一实现）
# ============================================================
def rects(boxes) -> List[List[float]]:
    """框列表 → 合法矩形列表（非法框丢弃，不猜）。"""
    return [r for r in (LC.rect_of_box(b) for b in (boxes or [])) if r]


def iou(a, b) -> float:
    """两矩形 IoU；任一不可用 → 0.0。本项目唯一的 IoU 实现。"""
    if not a or not b:
        return 0.0
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def best_iou(preds: Sequence, golds: Sequence) -> float:
    """一侧多框时的**逐金标准框取最优**（多框条目的并集矩形会虚增面积，不可用）。"""
    return max((iou(p, q) for q in golds for p in preds), default=0.0)


# ============================================================
# 两侧摊平：统一成 [(attr, norm_text, [rect])]
# ============================================================
def gold_boxes(stem: str, ann_dir: Optional[Path] = None,
               *, drop_anchorless: bool = False, drop_empty: bool = DENOM_SYMMETRIC,
               anchor: str = DEFAULT_ANCHOR) -> List[Tuple[str, str, List[List[float]]]]:
    """金标准 → 摊平三元组。只保留**文本与框都取得到**的条目。

    ★★ **分母口径的唯一实现**（2026-09-21 用户裁定改 B）：
    - `drop_empty=True`（默认，随 `DENOM_SYMMETRIC`）= **B 口径**：只算**有属性名**的条目。
      实测 45/145 无 `attr` 条目中 **33 条是「同一属性的相邻段」**（同列、y 间隙 0–6 px）、
      10 条是版面结构（报头/栏目/期号）⇒ 产品按**属性**输出 ⇒ 计入分母是**两侧粒度不一致**。
    - `drop_empty=False` = **A 口径**（历史读数 `0.1103` 由此产生）—— 由 `eval_page`
      作为**附注**并列报出（`metrics_all_entries`），**不是**准入读数。
    - `drop_anchorless=True` 是**另一件事**（只留锚属性，供记录切分对照），与上面两个正交。
    """
    from annotation_groups import aggregate

    p = Path(ann_dir or DEFAULT_ANN_DIR) / f"{stem}.png.jsonl"
    if not p.exists():
        return []
    try:
        rows = [json.loads(x) for x in
                p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[align_eval] 金标准读取失败 %s: %s", p.name, e)
        return []
    out: List[Tuple[str, str, List[List[float]]]] = []
    for e in aggregate(rows):
        a = str(e.get("attr") or "")
        if drop_empty and not a:
            continue
        if drop_anchorless and a != anchor:
            continue
        t = LC.normalize_text(e.get("text") or "")
        bs = rects(e.get("boxes") or ([e["box"]] if e.get("box") else []))
        if t and bs:
            out.append((a, t, bs))
    return out


def pred_boxes(entries: Sequence[dict]) -> List[Tuple[str, str, List[List[float]]]]:
    """预测条目 → 摊平三元组（口径与 `gold_boxes` 逐字相同）。"""
    out: List[Tuple[str, str, List[List[float]]]] = []
    for e in (entries or []):
        if not isinstance(e, dict):
            continue
        t = LC.normalize_text(e.get("text") or "")
        bs = rects(e.get("boxes") or ([e["box"]] if e.get("box") else []))
        if t and bs:
            out.append((str(e.get("attr") or ""), t, bs))
    return out


# ============================================================
# 打分
# ============================================================
def score(pred: Sequence[Tuple[str, str, List[List[float]]]],
          gold: Sequence[Tuple[str, str, List[List[float]]]],
          *, iou_t: float = IOU_THRESHOLD) -> Dict[str, Any]:
    """三口径读数 + 按属性分解 + 未命中值清单（诊断用，不参与判定）。"""
    by_val: Dict[str, List[List[float]]] = {}
    for _a, t, bs in pred:
        by_val.setdefault(t, []).extend(bs)

    n = len(gold)
    n_match = n_geo = 0
    miss_val: Dict[str, int] = {}
    miss_geo: Dict[str, int] = {}
    per_attr: Dict[str, Dict[str, int]] = {}
    for a, t, gbs in gold:
        d = per_attr.setdefault(a, {"n": 0, "match": 0, "geo": 0})
        d["n"] += 1
        pb = by_val.get(t)
        if not pb:
            miss_val[t[:12]] = miss_val.get(t[:12], 0) + 1
            continue
        n_match += 1
        d["match"] += 1
        if best_iou(pb, gbs) >= iou_t:
            n_geo += 1
            d["geo"] += 1
        else:
            miss_geo[a] = miss_geo.get(a, 0) + 1
    return {
        "n_gold": n,
        "n_value_match": n_match,
        "n_iou_hit": n_geo,
        METRIC_VALUE_MATCH: round(n_match / n, 4) if n else None,
        METRIC_GEOMETRY: round(n_geo / n_match, 4) if n_match else None,
        METRIC_END_TO_END: round(n_geo / n, 4) if n else None,
        "iou_threshold": iou_t,
        "miss_value_top": sorted(miss_val.items(), key=lambda kv: -kv[1])[:6],
        "miss_geometry_by_attr": sorted(miss_geo.items(), key=lambda kv: -kv[1])[:6],
        "per_attr": {k: {**v,
                         METRIC_VALUE_MATCH: (round(v["match"] / v["n"], 3) if v["n"] else None),
                         METRIC_GEOMETRY: (round(v["geo"] / v["match"], 3)
                                           if v["match"] else None)}
                     for k, v in sorted(per_attr.items(), key=lambda kv: -kv[1]["n"])},
    }


def score_record_aligned(entries: Sequence[dict],
                         gold: Sequence[Tuple[str, str, List[List[float]]]],
                         *, anchor: str = DEFAULT_ANCHOR,
                         iou_t: float = IOU_THRESHOLD) -> Dict[str, Any]:
    """**对照口径**（记录序号对齐）。分母在预测侧 ⇒ 虚高。**不得用于准入**。

    保留它的唯一理由是让"换口径带来的差异"可复算（老报告里的读数用它，
    不保留就无法解释历史数字）。
    """
    grecs: List[Dict[str, List[Tuple[str, List[List[float]]]]]] = []
    cur: Dict[str, List[Tuple[str, List[List[float]]]]] = {}
    for a, t, bs in gold:
        if a == anchor and cur:
            grecs.append(cur)
            cur = {}
        cur.setdefault(a, []).append((t, bs))
    if cur:
        grecs.append(cur)
    grecs = [r for r in grecs if anchor in r]

    precs: Dict[int, List[Tuple[str, str, List[List[float]]]]] = {}
    for e in (entries or []):
        if not isinstance(e, dict):
            continue
        ri = int(((e.get("evidence") or {}).get("record_index")) or 0)
        t = LC.normalize_text(e.get("text") or "")
        bs = rects(e.get("boxes") or ([e["box"]] if e.get("box") else []))
        if t and bs:
            precs.setdefault(ri, []).append((str(e.get("attr") or ""), t, bs))
    tot = hit = 0
    for ri, items in precs.items():
        grec = grecs[ri] if ri < len(grecs) else {}
        for attr, _t, pbs in items:
            # ★ 必须**按属性取**（`grec.get(attr)`）：金标准一条记录里含多个属性，
            #   若不按 attr 过滤，一条预测会被拿去和记录里每个属性比 → 分母虚增。
            for _gt, gbs in grec.get(attr, []):
                tot += 1
                if best_iou(pbs, gbs) >= iou_t:
                    hit += 1
    return {"n": tot, "hit": hit,
            METRIC_LEGACY: round(hit / tot, 4) if tot else None}


# ============================================================
# records 来源（零 API）
# ============================================================
def find_run(stem: str, divergence_dir: Optional[Path] = None) -> Optional[Path]:
    """含该页产物的**最新** run 目录；无 → None。"""
    d = Path(divergence_dir or DEFAULT_DIVERGENCE_DIR)
    if not d.is_dir():
        return None
    c = sorted((x for x in d.iterdir()
                if x.is_dir() and (x / f"{stem}.json").exists()),
               key=lambda x: x.name, reverse=True)
    return c[0] if c else None


def resolve_records(stem: str, *, run_dir=None, oracle: bool = False,
                    divergence_dir=None, ann_dir=None,
                    anchor: str = DEFAULT_ANCHOR) -> Tuple[Optional[List[dict]], str]:
    """`(records, source)`。`source` ∈ `run:<名>` / `oracle` / `""`（都没取到）。

    ★ `oracle` = 用**金标准的值**当 records ⇒ **上界对照，不是读数**
      （它隔离了 VLM 值质量，只留几何）。报告里必须显式标注，不得当成绩。
    """
    import preannotate as PA

    if oracle:
        return oracle_records(stem, ann_dir, anchor), "oracle"
    rd = Path(run_dir) if run_dir else find_run(stem, divergence_dir)
    if rd is not None:
        try:
            recs = PA.load_run_records(rd, stem)
        except Exception as e:                      # noqa: BLE001
            log.warning("[align_eval] run 读取失败 %s: %s", rd, e)
            recs = None
        if recs:
            return recs, f"run:{rd.name}"
    return None, ""


def oracle_records(stem: str, ann_dir: Optional[Path] = None,
                   anchor: str = DEFAULT_ANCHOR) -> List[dict]:
    """金标准 → `anchor_page` 的 records（按锚属性切分）。

    ★ **上界对照，不是读数**（只走 `--oracle`；`resolve_records` 会把 source 标成 `oracle`）。

    ★★ 2026-09-21 修两处**有损**：

    ① **同属性多段被覆盖**。原实现 `cur[a] = text` 只留**最后一段** ⇒ 实测 3 处
       （`公司类别 '股分有限'→'公司'`、`主营业务 '从事机'→'器磨面'`、
       `总公司地址 '设无锡'→'西门外'`），**前段的字被丢掉**（拼起来才是完整值，
       如 `股分有限公司`）。现改为**按阅读序拼接**。

    ② **丢弃不含锚属性的记录**。原实现末尾 `[r for r in recs if anchor in r]`
       ⇒ 实测丢 2 条，其中 0003 那条含 **7 个属性**（第二家公司没标「公司名」）。
       `anchor_page` **不要求记录含锚属性**（它遍历每条记录的每个键）⇒ 保留。

    ⇒ 两处都让"oracle 上界"**系统性低估**，从而**误导排程**：
      曾据此得出「主瓶颈是定位与切框」的错误结论（§89.4 已更正）。
    """
    from annotation_groups import aggregate

    p = Path(ann_dir or DEFAULT_ANN_DIR) / f"{stem}.png.jsonl"
    if not p.exists():
        return []
    try:
        rows = [json.loads(x) for x in
                p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (json.JSONDecodeError, OSError):
        return []
    recs: List[dict] = []
    cur: Dict[str, str] = {}
    for e in aggregate(rows):
        a = str(e.get("attr") or "")
        if a == anchor and cur and any(cur.values()):
            recs.append(cur)
            cur = {}
        if a:
            # ★ 同属性多段**按阅读序拼接**（不是覆盖）—— 见 docstring ①
            cur[a] = cur.get(a, "") + str(e.get("text") or "")
    if cur:
        recs.append(cur)
    # ★ 不再按锚属性过滤（见 docstring ②）：无锚记录里的值同样是"值完美"的一部分
    return recs


# ============================================================
# 单页 / 留一交叉验证
# ============================================================
def eval_page(stem: str, records: Sequence[dict], contract: dict, *,
              structured_dir=None, outbox_dir=None, ann_dir=None,
              image_name: Optional[str] = None,
              iou_t: float = IOU_THRESHOLD) -> Dict[str, Any]:
    """页 + records + 契约 → 跑几何装配 → 三口径读数。**不落盘**。

    ★ 调的是 `preannotate.anchor_page`（与生产同一条路径），不另写一份装配实现。
    """
    import preannotate as PA

    if not records:
        return {"stem": stem, "ok": False, "reason": "no_records"}
    if not contract:
        return {"stem": stem, "ok": False, "reason": "no_contract"}
    try:
        r = PA.anchor_page(list(records), stem, contract,
                           structured_dir=structured_dir, outbox_dir=outbox_dir,
                           image_name=image_name)
    except Exception as e:                          # noqa: BLE001
        log.warning("[align_eval] anchor_page 失败 %s: %s", stem, e)
        return {"stem": stem, "ok": False, "reason": f"{type(e).__name__}: {e}"}
    entries = list(r.get("entries") or [])
    pred = pred_boxes(entries)
    g = gold_boxes(stem, ann_dir)                       # ★ 主口径 B（对称分母）
    if not g:
        return {"stem": stem, "ok": False, "reason": "no_gold",
                "n_entries": len(entries)}
    # ★ 附注口径 A（含无属性名条目）：**并列报出**，不是准入读数。
    #   不复用 s —— 省一次遍历换不来可读性，且"两次调用同一函数"本身就是自证。
    s = score(pred, g, iou_t=iou_t)
    s_alt = score(pred, gold_boxes(stem, ann_dir, drop_empty=False), iou_t=iou_t)
    s2 = score_record_aligned(entries, g, iou_t=iou_t)
    return {"stem": stem, "ok": True, "n_entries": len(entries),
            "n_records": len(records), "metrics": s, "metrics_all_entries": s_alt,
            "legacy": s2, "stats": r.get("stats") or {}}


def _agg(folds: Sequence[Dict[str, Any]], key: str, *,
         bucket: str = "metrics") -> Optional[float]:
    """档案级聚合 = **按金标准框数加权的比**，不是各页读数取平均。

    `bucket` 选择用哪本读数（`metrics` = 主口径 B；`metrics_all_entries` = 附注口径 A）。
    """
    fs = [f for f in folds if f.get("ok") and isinstance(f.get(bucket), dict)]
    n = sum(f[bucket]["n_gold"] for f in fs)
    if not n:
        return None
    if key in (METRIC_END_TO_END, METRIC_END_TO_END_A):
        return round(sum(f[bucket]["n_iou_hit"] for f in fs) / n, 4)
    if key == METRIC_VALUE_MATCH:
        return round(sum(f[bucket]["n_value_match"] for f in fs) / n, 4)
    return None  # geometry_iou50 的分母是 n_value_match，不能跨页直接加


def loo(profile_id: str, *, ann_dir=None, structured_dir=None, outbox_dir=None,
        divergence_dir=None, contracts_dir=None, oracle: bool = False,
        stems: Optional[Sequence[str]] = None,
        iou_t: float = IOU_THRESHOLD) -> Dict[str, Any]:
    """留一交叉验证：每次**留出一页**学契约，在该页上测 ⇒ 测试页未参与学习。

    ★ 为什么这条路径**天然满足红线一**（源同则校无效）：`induce_contract(pages=)`
      显式传入训练页，测试页既不在 `source.pages` 里、也不参与任何归纳步骤。
      ⇒ 不需要新标注页即可得到**独立**读数。

    ★ 训练页集合**只从 `select_source_pages` 的入选页里取** —— 契约源页的资格判据
      归 `layout_contract` 管（B1 解耦：归一不得改它）。
    """
    allp = LC.collect_pages(profile_id, ann_dir, outbox_dir)
    if not allp:
        return {"ok": False, "error": f"该档案没有任何已标注页：{profile_id}"}
    kept, dropped = LC.select_source_pages(allp)
    if len(kept) < 3:
        return {"ok": False,
                "error": f"入选源页不足（{len(kept)} < 3）→ 交叉验证无意义",
                "kept": [p["stem"] for p in kept],
                "excluded": [d["stem"] for d in dropped]}
    by_stem = {p["stem"]: p for p in allp}

    base = LC.induce_contract(profile_id, ann_dir=ann_dir,
                              structured_dir=structured_dir, outbox_dir=outbox_dir)
    pool = [s for s in (stems or [p["stem"] for p in kept]) if s in by_stem]
    folds: List[Dict[str, Any]] = []
    for hold in pool:
        train = [by_stem[s] for s in pool if s != hold]
        if len(train) < 2:
            folds.append({"holdout": hold, "ok": False,
                          "reason": f"训练页不足（{len(train)}）"})
            continue
        c = LC.induce_contract(profile_id, pages=train, ann_dir=ann_dir,
                               structured_dir=structured_dir, outbox_dir=outbox_dir)
        recs, src = resolve_records(hold, oracle=oracle, divergence_dir=divergence_dir,
                                    ann_dir=ann_dir)
        r = eval_page(hold, recs, c, structured_dir=structured_dir,
                      outbox_dir=outbox_dir, ann_dir=ann_dir, iou_t=iou_t)
        r.update({"holdout": hold, "n_train": len(train),
                  "train": [p["stem"] for p in train],
                  "records_source": src,
                  "contract_confidence": c.get("confidence")})
        folds.append(r)

    ok_folds = [f for f in folds if f.get("ok")]
    # 基线（全契约）同页读数：用于判断"留出"本身带来了多大变化
    base_pages: List[Dict[str, Any]] = []
    for hold in pool:
        recs, src = resolve_records(hold, oracle=oracle, divergence_dir=divergence_dir,
                                    ann_dir=ann_dir)
        b = eval_page(hold, recs, base, structured_dir=structured_dir,
                      outbox_dir=outbox_dir, ann_dir=ann_dir, iou_t=iou_t)
        b["records_source"] = src
        base_pages.append(b)
    base_ok = [b for b in base_pages if b.get("ok")]

    profile_reading = {
        METRIC_END_TO_END: _agg(ok_folds, METRIC_END_TO_END),
        METRIC_VALUE_MATCH: _agg(ok_folds, METRIC_VALUE_MATCH),
        "n_gold": sum(f["metrics"]["n_gold"] for f in ok_folds),
        "n_iou_hit": sum(f["metrics"]["n_iou_hit"] for f in ok_folds),
        # ★ 附注口径 A（并列报粒度）：分母含无属性名条目 —— **不是**准入读数
        METRIC_END_TO_END_A: _agg(ok_folds, METRIC_END_TO_END_A,
                                  bucket="metrics_all_entries"),
        "n_gold_all_entries": sum(
            (f.get("metrics_all_entries") or {}).get("n_gold", 0) for f in ok_folds),
        "n_folds": len(ok_folds),
    }
    delta = None
    if base_ok and ok_folds:
        a, b = _agg(ok_folds, METRIC_END_TO_END), _agg(base_ok, METRIC_END_TO_END)
        if a is not None and b is not None:
            delta = round(a - b, 4)
    return {
        "ok": True,
        "profile_id": profile_id,
        "oracle": bool(oracle),
        "iou_threshold": iou_t,
        "kept": [p["stem"] for p in kept],
        "excluded": [{"stem": d["stem"], "reason": d.get("reason")} for d in dropped],
        "folds": folds,
        "baseline_pages": base_pages,
        "fold_reading": profile_reading,
        "baseline_reading": {
            METRIC_END_TO_END: _agg(base_ok, METRIC_END_TO_END),
            METRIC_VALUE_MATCH: _agg(base_ok, METRIC_VALUE_MATCH),
            METRIC_END_TO_END_A: _agg(base_ok, METRIC_END_TO_END_A,
                                      bucket="metrics_all_entries"),
            "n_gold": sum(f["metrics"]["n_gold"] for f in base_ok),
        },
        "loo_minus_baseline": delta,
    }


# ============================================================
# 人读视图
# ============================================================
def format_report(r: Dict[str, Any]) -> str:
    L: List[str] = ["=" * 70]
    if not r.get("ok"):
        L.append("几何对评  : **失败** —— %s" % r.get("error"))
        return "\n".join(L)
    L.append("档案      : %s" % r["profile_id"])
    L.append("口径      : 分母 = **有属性名的金标准条目**（对称分母，`DENOM_SYMMETRIC`）；"
             "IoU 阈值 %s%s"
             % (r["iou_threshold"], "（★ oracle 上界，非读数）" if r.get("oracle") else ""))
    L.append("入选源页  : %s" % ", ".join(s[-4:] for s in r.get("kept") or []))
    if r.get("excluded"):
        L.append("被剔（留出）: %s"
                 % ", ".join("%s(%s)" % (d["stem"][-12:], d.get("reason"))
                             for d in r["excluded"]))
    L.append("-" * 70)
    L.append("  折  训练  值匹配   纯几何   端到端   来源")
    for f in r.get("folds") or []:
        label = str(f.get("holdout") or f.get("stem") or "-")[-4:]
        if not f.get("ok"):
            L.append("  %-6s ----  %s" % (label, f.get("reason")))
            continue
        m = f["metrics"]
        L.append("  %-6s %-5d %-8s %-8s %-8s %s" % (
            label, f.get("n_train"),
            m[METRIC_VALUE_MATCH], m[METRIC_GEOMETRY], m[METRIC_END_TO_END],
            f.get("records_source") or "-"))
    R = r.get("fold_reading") or {}
    B = r.get("baseline_reading") or {}
    L.append("-" * 70)
    L.append("**LOO 档案读数**（%s 折 / %s 框）" % (R.get("n_folds"), R.get("n_gold")))
    L.append("  值匹配    : %s" % R.get(METRIC_VALUE_MATCH))
    L.append("  **端到端**  : %s" % R.get(METRIC_END_TO_END))
    L.append("基线（全契约）: 值匹配 %s / 端到端 %s"
             % (B.get(METRIC_VALUE_MATCH), B.get(METRIC_END_TO_END)))
    L.append("  [附注 A 口径] 端到端 : %s（分母 %s 含无属性名条目 —— **非准入读数**）"
             % (R.get(METRIC_END_TO_END_A), R.get("n_gold_all_entries")))
    if r.get("loo_minus_baseline") is not None:
        L.append("LOO − 基线    : %+.4f" % r["loo_minus_baseline"])
    L.append("")
    _e2e = R.get(METRIC_END_TO_END)
    L.append("登记（第 ④ 道门的解锁动作）：")
    L.append("  chronicles adjudicate validation --profile-id %s \\" % r["profile_id"])
    L.append("      --verdict %s --metric %s --value %s --pages %s"
             % ("pass" if (_e2e or 0) >= PASS_HINT_END_TO_END else "fail",
                METRIC_END_TO_END, _e2e,
                ",".join(str(p)[-4:] for p in (r.get("kept") or []))))
    L.append("  （`pass/fail` 此处按**建议线** `PASS_HINT_END_TO_END = %s` 给；"
             "同批须把判定依据写进 `--note`——真正的准入 `verdict` 由**人**定）"
             % PASS_HINT_END_TO_END)
    return "\n".join(L)


# ============================================================
# CLI（`chronicles eval-iou` 的**唯一实现**；命令面只转调）
# ============================================================
# 退出码词表正本在 `cli_contract`（2026-09-24 收敛）；判定在本模块 `run()` 内。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_MISSING,             # noqa: E402
                          EXIT_INVALID, EXIT_MEANING)                       # noqa: E402


def format_page(p: Dict[str, Any]) -> str:
    """单页读数（人读）。**只显示，不判定**。"""
    if not p.get("ok"):
        return ("[align_eval] 缺料：%s\n  → %s"
                % (p.get("error") or p.get("reason"), p.get("next") or "核对输入。"))
    m = p.get("metrics") or {}
    ma = p.get("metrics_all_entries") or {}
    lg = p.get("legacy") or {}
    L = ["=" * 70,
         "页        : %s%s" % (p.get("stem"),
                               "（★ oracle 上界，非读数）" if p.get("oracle") else ""),
         "records   : %s（%s 条）→ 条目 %s"
         % (p.get("records_source") or "-", p.get("n_records"), p.get("n_entries")),
         "金标准框  : %s（有属性名；主口径分母）" % m.get("n_gold"),
         "-" * 70,
         "  值匹配    : %s  （%s/%s）" % (m.get(METRIC_VALUE_MATCH),
                                         m.get("n_value_match"), m.get("n_gold")),
         "  纯几何    : %s  （值已配上的框里，IoU≥%s 的比例）"
         % (m.get(METRIC_GEOMETRY), m.get("iou_threshold")),
         "  **端到端** : %s  （分母 = 有属性名的金标准条目）" % m.get(METRIC_END_TO_END),
         "  [附注 A]  : %s  （分母 %s 含无属性名条目 —— **非准入读数**）"
         % (ma.get(METRIC_END_TO_END), ma.get("n_gold")),
         "  （对照）旧口径 : %s —— 分母在预测侧 ⇒ 虚高，**不得用于准入**"
         % lg.get(METRIC_LEGACY)]
    if m.get("miss_value_top"):
        L.append("  值未预测出  : %s" % m["miss_value_top"])
    if m.get("miss_geometry_by_attr"):
        L.append("  框没对上的属性: %s" % m["miss_geometry_by_attr"])
    return "\n".join(L)


def format_human(p: Dict[str, Any]) -> str:
    """人读视图唯一分发口（命令面与独立 CLI 共用一份）。"""
    if p.get("action") == "page":
        return format_page(p)
    return format_report(p)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="零 API 几何对评（值对齐三口径）")
    ap.add_argument("--profile-id", required=True)
    ap.add_argument("--stem", default="", help="只评这一页（不给则做 LOO-CV）")
    ap.add_argument("--oracle", action="store_true",
                    help="★ 用金标准值当 records（**上界对照，不是读数**）")
    ap.add_argument("--run-dir", default=None, help="指定 VLM records 所在 run")
    ap.add_argument("--ann-dir", default=None)
    ap.add_argument("--structured-dir", default=None)
    ap.add_argument("--outbox", default=None)
    ap.add_argument("--divergence-dir", default=None)
    ap.add_argument("--contracts-dir", default=None)
    ap.add_argument("--iou", type=float, default=IOU_THRESHOLD)
    ap.add_argument("--json", action="store_true", dest="as_json")
    return ap


def run(argv: Optional[Sequence[str]] = None) -> Tuple[int, Dict[str, Any]]:
    """编排入口 → `(exit_code, payload)`。

    ★ **所有判定都在这里**（`chronicles eval-iou` 只做「拿参数 → 调本函数 → 组织输出」）。
      命令面因此不可能造出第二份"什么算通过"的真相。
    """
    a = build_parser().parse_args(argv)
    ann = Path(a.ann_dir) if a.ann_dir else None
    st = Path(a.structured_dir) if a.structured_dir else None
    ob = Path(a.outbox) if a.outbox else None
    dv = Path(a.divergence_dir) if a.divergence_dir else None
    ct = Path(a.contracts_dir) if a.contracts_dir else None

    if a.stem:
        c = LC.load_contract(a.profile_id, ct)
        if not c:
            return EXIT_MISSING, {
                "ok": False, "action": "page", "profile_id": a.profile_id,
                "error": f"契约不存在：{a.profile_id}",
                "next": "先跑 `chronicles plan --profile <档案id>` 生成契约。"}
        recs, src = resolve_records(a.stem, run_dir=a.run_dir, oracle=a.oracle,
                                    divergence_dir=dv, ann_dir=ann)
        r = eval_page(a.stem, recs, c, structured_dir=st, outbox_dir=ob,
                      ann_dir=ann, iou_t=a.iou)
        r["records_source"] = src
        r["profile_id"] = a.profile_id
        r["oracle"] = bool(a.oracle)
        r["iou_threshold"] = a.iou
        rc = EXIT_OK if r.get("ok") else EXIT_MISSING
        if not r.get("ok"):
            r["next"] = ("先产出 records（跑一次 OCR / `chronicles exec`），"
                         "或用 `--oracle` 取上界对照。"
                         if r.get("reason") == "no_records" else
                         "该页没有金标准（manual_annotations/<stem>.png.jsonl）"
                         "—— 回验需要真值。")
        return rc, {"ok": bool(r.get("ok")), "action": "page",
                    "schema_version": SCHEMA_VERSION, **r}

    r = loo(a.profile_id, ann_dir=ann, structured_dir=st, outbox_dir=ob,
            divergence_dir=dv, contracts_dir=ct, oracle=a.oracle, iou_t=a.iou)
    if not r.get("ok"):
        return EXIT_MISSING, {"ok": False, "action": "loo",
                              "profile_id": a.profile_id,
                              "error": r.get("error"),
                              "next": ("交叉验证要求该档案有 ≥3 页**入选源页**"
                                       "（`layout_contract.select_source_pages` 的判据）。"
                                       "源页不够时先攒标注页。")}
    return EXIT_OK, {"ok": True, "action": "loo",
                     "schema_version": SCHEMA_VERSION, **r}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """独立 CLI（`chronicles eval-iou` 转调的是 `run`，不是本函数）。"""
    raw = list(argv if argv is not None else sys.argv[1:])
    as_json = "--json" in raw
    rc, payload = run([x for x in raw if x != "--json"])
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_human(payload))
    return rc


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
