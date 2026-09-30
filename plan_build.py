# -*- coding: utf-8 -*-
"""层 2「规则归纳 → 识别与标注方案」（plan_build）。

把三份资产收敛成**一份自足方案**：

    契约     data/layout_contracts/layout_<pid>.json   版面约定的**权威**
    learned  rules_data/learned_<pid>.json              从金标准学到的统计与候选
    facts    data/plan/facts_<pid>.json                 层 1 的观测

产物：

    data/plans/plan_<pid>.json   机器可读方案（层 3 的唯一输入）
    data/plans/plan_<pid>.md     同一方案的执行说明书（人可读、可改）

纪律（每条都是实测代价换来的，别省）：

  1. **单向编译**：契约 → plan，**绝不写回**。plan 是产物，不是真相源。
     契约仍是「版面约定类」信息的权威；plan 是「识别与标注方案类」信息的权威
     （含契约没有的：横带结构、记录起点判据、执行说明）。两类各自权威，
     重叠部分单向编译，不重叠部分互不干涉。
  2. **可复现 + 可对照**：refs 记三份输入的 sha256，并**落盘** `refs.*.stale`。
     ⚠ **两种「stale」不是一件事，别读成一句**：
       · 本模块落盘的 `refs.<k>.stale` 答的是「相对**盘上上一版 plan**，这份输入的
         sha256 变了没有」—— 落盘时刻就能算（要读旧文件），是层 2 自己的记账。
         **无上一版可比时是 `null`（"无从判定"），不是 `false`（"没变"）**。
       · 「**此刻**盘上的输入还对不对得上这份 plan」是另一个问题：它只能在读取现场
         重算（`seam_map._refs_status` 干的活：现盘哈希 vs refs 记录哈希）。
     前者是历史（这份 plan 是怎么来的），后者是现状（这份 plan 还能用吗）。
     **不可互相替代**：plan 编译出来的那一刻，"现状"必然一致，除非事后有人动了输入。
     落点 = `save()`（落盘动作的拥有点）；`build_plan()` 保持纯编译、不读盘。
  3. **不猜语义**：未标注区只记观测（`has_gold` / `attrs` / `role`），
     不硬起「标题区」这类名字——人是按需标注的，不会替你标出所有版面结构。
  4. **缺项降级**：契约缺 → 退 learned；两者都缺 → 该块标 `degraded` 并写原因，
     而不是静默省略（省略会让外部 agent 无从知道"这里本该有东西"）。

用法：

    python plan_build.py --profile prof_xxx
    python plan_build.py --profile prof_xxx --out data/plans
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import statistics as st
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import data_io

log = logging.getLogger("plan_build")

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "0.1.0"
KIND = "recognition_annotation_plan"

DEFAULT_FACTS_DIR = ROOT / "data" / "plan"
DEFAULT_CONTRACTS_DIR = ROOT / "data" / "layout_contracts"
DEFAULT_RULES_DIR = ROOT / "rules_data"
DEFAULT_OUT_DIR = ROOT / "data" / "plans"

# 跨页归并结构线的相对页高容差。
# 实测标定（官报三页）：真结构线跨页 rel 极差 ≤0.0061，
# 而 0002 有一条 len=1 的短横线误检距标题线仅 0.0095 —— 两者重叠，
# 故单靠 rel 一维聚类**分不开**；配合「簇内按页去重」才干净（见 merge_layout_boundaries）。
REL_TOL = 0.007
# 弱证据阈值：行数 ≤ 此值的「横线」很可能是文字行误检（真横线跨列，远长于此）。
WEAK_RULE_LEN = 2

# ★ 2026-09-13（P1-2）：一个版式族要"**可归纳**"所需的最少页数。
#   理由（这轮取证推翻了上一轮的建议）：实测 5 个族里 4 个只有 1 页 ——
#   **给只有 1 页证据的族生成一套方案，等于把那一页的金标准抄一遍**（in-sample 拟合），
#   它对任何别的页都没有预测力，却会让"版式参数来自跨页统计"这句话变成假话。
#   ★ 这是"分组 → 每组一套参数"这类设计的**通用上线前检查**：
#   先看最小分组规模；组内样本数 < 2 时，该分叉不产生泛化，只产生拟合。
#   业界参照：PP-StructureV2 的路由单位是**区域类型**、MinerU2.5 是**统一标签体系**——
#   两者都不按"页级版式"分叉出多套解析器。故单页族**不生成**族级方案，
#   只记为"这一页版式未经跨页验证"（`coverage = "single_page_evidence"`）。
MIN_FAMILY_PAGES = 2


def _sha256(path: Path) -> Optional[str]:
    """文件内容哈希；不存在 → None（不抛，缺件由上层降级）。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ============================================================ 横带：跨页归并

def merge_layout_boundaries(pages: List[dict],
                            tol: float = REL_TOL) -> Tuple[List[dict], dict]:
    """把各页的强分隔归并成**版式级结构线**。

    为什么不能只按 rel 一维聚类：真结构线跨页 rel 波动可达 0.006（约 11px @1800px），
    而单页可能出现 len=1 的短横线误检，它距真线只有 17px —— 波动 > 间距，
    贪心聚类必把它们并成一簇、把簇心拉偏。

    故分两步：
      1) 按 rel 贪心聚类（tol）
      2) **簇内按页去重**：同一页多条 → 只留最接近簇中位的那条，其余进 `extra`

    这样 0002 的 {0.1602, 0.1696} 只贡献 0.1602，簇心回到三页真值附近。
    `extra` 不丢弃 —— 它是「这页多出来的线」，对执行器是有用信号。
    """
    items: List[dict] = []
    for pg in pages:
        ss = (pg.get("band_source") or {}).get("strong_separators") or []
        h = (pg.get("image") or {}).get("h")
        for s in ss:
            items.append({"rel": float(s["rel"]), "stem": pg.get("stem"),
                          "h": h, "kind": s.get("kind"),
                          "len": s.get("len"), "in_gold": bool(s.get("in_gold"))})
    if not items:
        return [], {"tol": tol, "n_pages": len(pages), "n_lines": 0}

    items.sort(key=lambda x: x["rel"])
    clusters: List[List[dict]] = []
    for it in items:
        if clusters and it["rel"] - clusters[-1][-1]["rel"] <= tol:
            clusters[-1].append(it)
        else:
            clusters.append([it])

    page_hs = [i["h"] for i in items if i["h"]]
    ref_h = int(st.median(page_hs)) if page_hs else None
    n_pages = len(pages)
    lines: List[dict] = []

    for c in clusters:
        rels = sorted(x["rel"] for x in c)
        med = st.median(rels)
        # 簇内按页去重：同页多条只留最接近中位的那条
        keep: Dict[str, dict] = {}
        extra: List[dict] = []
        for x in c:
            cur = keep.get(x["stem"])
            if cur is None:
                keep[x["stem"]] = x
            else:
                better = x if abs(x["rel"] - med) < abs(cur["rel"] - med) else cur
                worse = cur if better is x else x
                keep[x["stem"]] = better
                extra.append(worse)
        kept = list(keep.values())
        krels = [x["rel"] for x in kept]
        rel = sum(krels) / len(krels)
        stems = sorted(str(x["stem"]) for x in kept)
        kinds: Dict[str, int] = {}
        for x in kept:
            kinds[str(x["kind"])] = kinds.get(str(x["kind"]), 0) + 1
        kind = max(kinds.items(), key=lambda kv: kv[1])[0] if kinds else None
        lines.append({
            "rel": round(rel, 4),
            "rel_spread": round(max(krels) - min(krels), 4),
            "px_at_ref_page_h": round(rel * ref_h, 1) if ref_h else None,
            "support": round(len(stems) / n_pages, 3) if n_pages else None,
            "n_pages_hit": len(stems),
            "kind": kind,
            "in_gold_any": any(x["in_gold"] for x in kept),
            "weak": sum(1 for x in kept if (x.get("len") or 0) <= WEAK_RULE_LEN),
            "pages": stems,
            "extra_same_page": [{"stem": x["stem"], "rel": round(x["rel"], 4),
                                 "len": x.get("len"), "kind": x.get("kind")}
                                for x in sorted(extra, key=lambda y: y["rel"])],
        })

    lines.sort(key=lambda x: x["rel"])
    return lines, {"tol": tol, "n_pages": n_pages, "n_lines": len(lines),
                   "ref_page_h": ref_h}


# ============================================================ 契约回迁带对位（P-C）

BAND_MATCH_TOL = 0.005      # 与 contract_backfill.MATCH_TOL 同口径


def contract_band_agreement(lines: List[dict], contract: dict,
                            ref_page_h: Optional[float] = None,
                            tol: float = BAND_MATCH_TOL) -> dict:
    """本版归纳的结构线 × 契约回迁带（`G.layout_bands`）对位。

    契约是**版面约定的权威**（裁定 2），而回迁带是**上一版 plan** 写回去的
    （`contract_backfill.py`）。这一层对位回答的是：**新一版 plan 有没有偏离
    已经确认过的版面约定**。

    ★ 只观测，不反向约束。理由是先有偏差数据、再谈约束 —— 反过来做等于用
    未经验证的先验直接改切分（本项目对"判据尺度"踩过两次坑）。连续多版
    偏差都在容差内，才够格让契约覆盖本版。

    读取口径单一：回迁带一律经 `contract_backfill.bands_of`（不在此重复解析）。
    """
    try:
        import contract_backfill as CB
        cbands = CB.bands_of(contract or {})
        meta = CB.bands_meta(contract or {})
    except Exception as e:                            # pragma: no cover
        return {"verdict": "unavailable", "reason": f"{type(e).__name__}: {e}"}
    n_plan = len(lines or [])
    if not cbands:
        return {"verdict": "no_contract_bands", "n_contract": 0,
                "n_matched": 0, "n_plan_only": n_plan,
                "source": "contract.G.layout_bands",
                "note": "本契约尚无回迁带；用 `python contract_backfill.py --apply` 回迁"}

    h = ref_page_h or meta.get("ref_page_h")
    matched: List[dict] = []
    plan_only: List[dict] = []
    used = set()
    for l in lines or []:
        best, bd = None, tol
        for j, cb in enumerate(cbands):
            if j in used:
                continue
            d = abs(float(l.get("rel", 0)) - float(cb.get("rel", 0)))
            if d <= bd:
                best, bd = j, d
        if best is None:
            plan_only.append({"rel": l.get("rel")})
            continue
        used.add(best)
        cb = cbands[best]
        dev_px = bd * float(h) if h else None
        matched.append({"rel_plan": l.get("rel"),
                        "rel_contract": cb.get("rel"),
                        "dev_rel": round(bd, 4),
                        "dev_px": round(dev_px, 1) if dev_px is not None else None})
    contract_only = [{"rel": c.get("rel")} for j, c in enumerate(cbands)
                     if j not in used]
    devs = [m["dev_rel"] for m in matched]
    if matched and not plan_only and not contract_only:
        verdict = "conforms"
    elif matched:
        verdict = "deviates"
    else:
        verdict = "unmatched"
    return {
        "verdict": verdict,
        "n_contract": len(cbands),
        "n_matched": len(matched),
        "n_plan_only": len(plan_only),
        "n_contract_only": len(contract_only),
        "max_dev_rel": round(max(devs), 4) if devs else None,
        "max_dev_px": round(max(devs) * float(h), 1) if (devs and h) else None,
        "matched": matched,
        "plan_only": plan_only,
        "contract_only": contract_only,
        "tol": tol,
        "ref_page_h": h,
        "contract_backfilled_at": meta.get("backfilled_at"),
        "source": "contract.G.layout_bands（由 contract_backfill 从上一版 plan 回迁）",
        "note": "只观测不约束：偏差不改本版切分。连续多版 conforms 之后，"
                "才谈用契约带覆盖 plan 带（先把权威验证出来）。",
    }


def _line_similarity(a: List[float], b: List[float], tol: float) -> float:
    """两条结构线序列的相似度（贪心容差匹配 / max 长度）。空 → 0。"""
    if not a or not b:
        return 0.0
    used = set()
    m = 0
    for x in a:
        best, bd = None, tol
        for j, y in enumerate(b):
            if j in used:
                continue
            d = abs(x - y)
            if d <= bd:
                bd, best = d, j
        if best is not None:
            used.add(best)
            m += 1
    return m / max(len(a), len(b))


def cluster_pages_by_layout(pages: List[dict],
                            tol: float = REL_TOL,
                            min_sim: float = 0.5) -> List[dict]:
    """按**结构线形态**把页分成版式族（单链聚类）。

    ★ 为什么必须分族：一个 profile 里的页未必同版式。实测 7 页里，
    官报三页结构线两两相似度 0.75–1.0，而「联想截图_*」三页两两相似度 0
    （它们只是文件名前缀相同）。混在一起归并 → 得到 14 条"结构线"，
    其中 9 条 support=1/7，全是单页噪声，执行器无从判断该信哪条。

    分族后：语义规则（record / attributes / lexicon）仍**跨族共享**
    （它们本就来自同一批金标准），几何版式（结构线）按族各表 ——
    这正好对应契约 G/S/A 的分层：G 是几何、S 是语义。

    ★ P1-2（2026-09-13）：分族之外还判**可归纳性**（`coverage`）。族是几何事实
    （页与页的结构线形态不同就是不同族），但"能不能当方案来源"是另一件事：
    **只有 1 页证据的族，给它配参数等于把那页金标准抄一遍**。故
    `n_pages >= MIN_FAMILY_PAGES` 才标 `inducible`，否则 `single_page_evidence`。
    """
    sig = []
    for p in pages:
        ss = (p.get("band_source") or {}).get("strong_separators") or []
        sig.append({"stem": p.get("stem"), "page": p,
                    "rels": sorted(float(s["rel"]) for s in ss),
                    "h": (p.get("image") or {}).get("h")})

    raw: List[List[dict]] = []
    for s in sig:
        for c in raw:
            if any(_line_similarity(s["rels"], m["rels"], tol) >= min_sim
                   for m in c):
                c.append(s)
                break
        else:
            raw.append([s])

    out: List[dict] = []
    for c in raw:
        lines, meta = merge_layout_boundaries([m["page"] for m in c], tol)
        nl = [len(m["rels"]) for m in c]
        out.append({
            "pages": [m["stem"] for m in c],
            "n_pages": len(c),
            "median_n_lines": int(st.median(nl)) if nl else 0,
            "ref_page_h": meta.get("ref_page_h"),
            "layout_boundaries": lines,
            "merge_meta": meta,
            # ★ P1-2（2026-09-13）：**族的可归纳性**在分族处一次定好 ——
            #   `coverage=inducible` 才是方案来源；`single_page_evidence` 只是
            #   "这一页版式未经跨页验证"的记录。判据放在这里（而不是调用方），
            #   是为了让"族"这个概念**只有一个出处**，任何调用方都拿到同一档判定。
            "min_pages": MIN_FAMILY_PAGES,
            "coverage": ("inducible" if len(c) >= MIN_FAMILY_PAGES
                         else "single_page_evidence"),
        })
    # 主族 = 页数最多；并列时取结构线最多的（证据更强的那个）
    out.sort(key=lambda x: (-x["n_pages"], -x["median_n_lines"]))
    for i, c in enumerate(out):
        c["id"] = i
        c["is_primary"] = (i == 0)
    return out


def page_regions(page: dict, lines: List[dict]) -> List[dict]:
    """用版式结构线把**整页**切成区（含无人标注的区）。

    ★ 这是与 `facts.bands` 的关键区别：`bands` 只切「金标准覆盖到的内容」
    （人没标就不切），而 selector 需要知道"这一页总共有几块、哪块没标注"。
    人标注的是人对这张图的认知，不是 AI 需要的认知 —— 所以分区要覆盖全页。

    `role` 只给**位置性**判断，不猜语义：
      unannotated_head / unannotated_tail / unannotated_mid / annotated
    """
    boxes = page.get("columns") or []
    edges = [0.0] + [float(l["rel"]) for l in lines] + [1.0]
    # 去重并保证严格递增
    clean: List[float] = []
    for e in edges:
        if not clean or e - clean[-1] > 1e-6:
            clean.append(e)

    h = (page.get("image") or {}).get("h")
    out: List[dict] = []
    for i in range(len(clean) - 1):
        a, b = clean[i], clean[i + 1]
        entry: Dict[str, Any] = {"id": len(out), "rel": [round(a, 4), round(b, 4)],
                                 "has_gold": False, "n_gold_boxes": 0,
                                 "attrs": [], "role": "unknown"}
        if h:
            entry["y_px"] = [round(a * h, 1), round(b * h, 1)]
            inside = [c for c in boxes
                      if c.get("y") and c["y"][0] < b * h and c["y"][1] > a * h]
            entry["has_gold"] = bool(inside)
            entry["n_gold_boxes"] = sum(int(c.get("n_boxes") or 0) for c in inside)
            attrs = []
            for c in inside:
                attrs.extend(x for x in (c.get("attr_seq") or []) if x)
            entry["attrs"] = sorted(set(attrs))
        # ⚠ 无页高 → has_gold 不可知 → role 保持 "unknown"。
        # 不能默认判成 unannotated_*：那是「确定没有真值」，与「判断不了」是两回事，
        # 混淆会让执行器以为该区必然无记录可抽。
        if h:
            if entry["has_gold"]:
                entry["role"] = "annotated"
            elif i == 0:
                entry["role"] = "unannotated_head"
            elif i == len(clean) - 2:
                entry["role"] = "unannotated_tail"
            else:
                entry["role"] = "unannotated_mid"
        out.append(entry)
    return out


# ============================================================ 方案组装

def _pick(*cands):
    """按序取第一个**非空**候选；全空时返回最后一个（视作默认值）。

    ⚠ 不能用 `return None` 收尾：`{}` / `[]` 是 falsy 但**是合法的默认值**——
    缺料时返回 None 会让下游 `.get()` 直接 AttributeError（实测踩过）。
    """
    for c in cands:
        if c:
            return c
    return cands[-1] if cands else None


def _jsonable(v):
    """set / tuple / 正则 → JSON 可序列化（外部 agent 拿到的是纯数据）。"""
    if isinstance(v, set):
        return sorted(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if hasattr(v, "pattern"):
        return {"re": v.pattern}
    return v


def build_split_spec(learned: dict, profile: dict) -> Tuple[dict, Optional[str]]:
    """把 L0 切分规格（`rule_split.build_spec`）**外化**成纯数据。

    ★ 这是「自足」的实质：spec 是 L0 的**全部规则**——锚词正则、人名槽位、
    长度阈值、页面常量，它们原本只存在于代码里。任何"只有看代码才知道"的
    信息都是方案层的缺陷（R2 判据）。外化后，外部 agent 不读源码也能复现
    L0 的切分。

    复用而非重写：直接调 `rule_split.build_spec`，不另造平行实现
    （否则两份规则会静默分叉）。
    """
    try:
        import rule_split as RS
        spec = RS.build_spec(learned or {}, profile or {})
    except Exception as e:                                  # pragma: no cover
        return {}, f"{type(e).__name__}: {e}"
    out: Dict[str, Any] = {}
    for k, v in spec.items():
        if k == "anchors":
            out["anchors"] = {
                attr: [{"re": rx.pattern, "mode": mode} for rx, mode in (rxs or [])]
                for attr, rxs in (v or {}).items()
            }
        elif k == "sources":
            out["sources"] = _jsonable(v)
        else:
            out[k] = _jsonable(v)
    out["_note"] = (
        "锚词由**属性名**推出（属性名本身就是语义标签），不是硬编码词表；"
        "`mode=self` → 命中的文本段即取值；`mode=after` → 取锚词之后的片段。"
        "空锚词表的属性（如记录首属性）由几何给出，不做文本抽取。")
    return out, None


def build_plan(profile_id: str,
               facts_dir: Optional[Path] = None,
               contracts_dir: Optional[Path] = None,
               rules_dir: Optional[Path] = None,
               profile: Optional[dict] = None) -> dict:
    """三层资产 → plan（不落盘）。缺件走降级，不抛。

    `profile` 显式传入则不再读档案（测试与离线编译用）。
    """
    facts_dir = Path(facts_dir or DEFAULT_FACTS_DIR)
    contracts_dir = Path(contracts_dir or DEFAULT_CONTRACTS_DIR)
    rules_dir = Path(rules_dir or DEFAULT_RULES_DIR)

    f_path = facts_dir / f"facts_{profile_id}.json"
    c_path = contracts_dir / f"layout_{profile_id}.json"
    l_path = rules_dir / f"learned_{profile_id}.json"

    facts = json.loads(f_path.read_text(encoding="utf-8")) if f_path.exists() else {}
    contract = json.loads(c_path.read_text(encoding="utf-8")) if c_path.exists() else {}
    learned = json.loads(l_path.read_text(encoding="utf-8")) if l_path.exists() else {}
    if not learned.get("skeleton"):
        learned = {} if not learned else learned      # 允许部分缺失

    degraded: List[dict] = []
    if not facts:
        degraded.append({"block": "facts", "reason": "缺少层 1 观测，先跑 gold_facts.py",
                         "fallback": "page_model 退化为契约 + 金标准"})
    if not contract:
        degraded.append({"block": "contract", "reason": "缺少版面契约",
                         "fallback": "record/anchoring 退化为 learned"})
    if not learned:
        degraded.append({"block": "learned", "reason": "缺少已学规则",
                         "fallback": "record 退化为契约骨架"})

    pages = facts.get("pages") or []
    usable = [p for p in pages if not p.get("excluded")]
    G = contract.get("G") or {}
    S = contract.get("S") or {}
    A = contract.get("A") or {}
    sk = learned.get("skeleton") or {}
    xp = facts.get("cross_page") or {}

    # ---- 横带：先分版式族，再族内归并（裁定 3：全量结构线入 plan）
    # ★ 2026-09-13（P1-2）：族的可归纳性由 `cluster_pages_by_layout` 一次判好
    #   （见该函数 `coverage` / `min_pages`）—— 此处不再重判，避免两处判据漂移。
    #   为什么不是"把单页族去掉"：那一页的**分区仍要用它自己的结构线**（`regions`
    #   逐页用自身族边界，见下），删掉族会让分区退回主族边界 → 切错。
    clusters = cluster_pages_by_layout(usable)
    n_inducible = sum(1 for c in clusters if c.get("coverage") == "inducible")

    bound_of: Dict[str, List[dict]] = {}
    cluster_of: Dict[str, int] = {}
    covered_of: Dict[str, bool] = {}
    for c in clusters:
        induc = (c.get("coverage") == "inducible")
        for s in c["pages"]:
            bound_of[s] = c["layout_boundaries"]
            cluster_of[s] = c["id"]
            covered_of[s] = induc
    lines = clusters[0]["layout_boundaries"] if clusters else []
    merge_meta = clusters[0]["merge_meta"] if clusters else {}
    # ---- 分区必须用**该页自己族**的结构线：异版式页套用主族边界会被切错
    # `covered`（P1-2 新增）：该页所属族是否有**跨页证据**。执行器与界面据此判断
    # "这一页的产出能不能信" —— 单页族的版式参数等于孤证。判据用的是 `coverage`，
    # 不是"是否主族"：两个各 2 页的族都算 covered（都经过了跨页验证）。
    regions = [{"stem": p.get("stem"),
                "cluster": cluster_of.get(p.get("stem")),
                "covered": bool(covered_of.get(p.get("stem"))),
                "regions": page_regions(p, bound_of.get(p.get("stem"), []))}
               for p in usable]
    # ---- 与契约**回迁带**对位（P-C：契约当权威的最小可用形态）
    band_agree = contract_band_agreement(lines, contract,
                                        ref_page_h=merge_meta.get("ref_page_h"))
    # `degraded` 的两条**不是同一件事**，各自成立：
    #   ① 有页的版式族只有单页证据 → 那几页不得按主族骨架切分（否则伪命中），
    #      执行器应降级为整页兜底（P0-3b 已把它写进 `execution.invariants`）；
    #   ② 有 ≥2 个**可归纳**的族 → 顶层结构线只能取主族（其余族见 layout_clusters）。
    # ⚠ ② 计数必须用 `n_inducible` 而不是 `len(clusters)`：单页族永远存在（异版式页
    #   天然各自成族），拿总族数当判据会让告警**永不消失** → 告警就没有信息量了。
    n_uncovered = sum(1 for p in usable if not covered_of.get(p.get("stem")))
    if n_uncovered:
        degraded.append({
            "block": "page_model.layout_clusters",
            "reason": (f"{len(usable)} 页里 {n_uncovered} 页的版式族只有单页证据"
                       f"（族数 {len(clusters)}，其中可归纳 {n_inducible}）"),
            "fallback": ("这些页的 `covered=False` → 不按主族骨架切分，降级为整页一条兜底"
                         "（不产伪命中、不丢字）；产出仍需人工复核。"
                         "见 `page_model.regions[].covered` 与 `execution.invariants`"),
        })
    if n_inducible > 1:
        degraded.append({
            "block": "page_model.layout_clusters",
            "reason": f"该 profile 内有 {n_inducible} 个可归纳的版式族，非单一版式",
            "fallback": "顶层 layout_boundaries 取主族；其余族见 layout_clusters",
        })

    # ---- 阅读序
    rd = (usable[0].get("reading") if usable else {}) or {}
    reading = {
        "order": _pick(G.get("reading_order"), rd.get("order"), "rtl_ttb"),
        "primary_axis": "column",
        "votes": _pick(rd.get("votes"), G.get("_reading_votes"), {}),
        # P-C 补：**顺序必须可复现**。只写 `rtl_ttb` 这个名字，外部 agent 是复现
        # 不出同款顺序的（实测：朴素「x 降序 + y 升序」在官报 0001 上恰好一致，
        # 但在 0003 与 manual 页就不一致）。故把**决策程序**整体外化。
        "algorithm": {
            "id": "rtl-column-cluster-v1",
            "steps": [
                "① 每个框取 rect [x0,y0,x1,y1]；非法框（取不出矩形）保持相对顺序排在最后。",
                "② 按 **x 中心升序**（左→右）遍历，贪心建**列簇**：每个框依次尝试并入"
                "已有簇（按簇的建立顺序），命中即并入并更新该簇的 x 区间；都不命中则"
                "以自己为**种子**新建一簇。种子一经建立**不再改变**（防逐列吞并）。",
                "③ 并入判据（三条守卫，全无量纲）："
                "a) 宽度比 —— min(框宽, 种子宽) / max(...) ≥ 0.50；"
                "b) 重叠比 —— 框与**簇当前 x 区间**的重叠宽 / min(框宽, 区间宽) ≥ 0.50；"
                "c) 跨度比 —— 框与种子并集宽 ≤ 1.60 × 种子宽。",
                "④ 列簇按 **x 中心降序**排序（右→左）。",
                "⑤ 簇内按 **y 升序**排序（上→下）。",
                "⑥ 顺序展开为索引序列。任一步异常 → 回退「x 中心降序 + y 升序」。",
            ],
            "constants": {"COL_OVERLAP_RATIO": 0.50, "COL_WIDTH_RATIO": 0.50,
                          "COL_SPAN_MAX": 1.60},
            "why_not_fixed_bin": "固定像素分箱（如 x_center // 50）在某一档必失效："
                                 "列太密会两列同箱、列太疏会一列跨箱。故一律无量纲。",
            "conformance": "一致性由 `seam.validate` 的 `stream_mismatch` 判据检查 —— "
                           "执行器自报的 input.text_stream 必须等于真实 OCR 行流；"
                           "不等即说明阅读序没复现，校验器会直接判错并指出字数差。",
        },
        "_src": "contract.G.reading_order 优先；缺则 facts.pages[0].reading；"
                "algorithm 外化自 layout_contract.order_rtl / column_clusters",
    }

    # ---- 页面模型
    colp = G.get("column_profile") or {}
    cm = G.get("char_metrics") or {}
    fcm = (usable[0].get("char_metrics") if usable else {}) or {}
    page_model = {
        "layout_boundaries": lines,
        "layout_boundaries_meta": merge_meta,
        "layout_boundaries_contract": band_agree,
        "layout_clusters": clusters,
        "layout_cluster_of": cluster_of,
        "regions": regions,
        "bands": ([{"stem": p.get("stem"), "bands": p.get("bands")} for p in usable]
                  if usable else []),
        "columns": {
            "n_columns": _pick(colp.get("n_columns"), None),
            "n_columns_samples": colp.get("n_columns_samples"),
            "col_gap_px": _pick(colp.get("col_gap_px"), None),
            "col_width_px": _pick(colp.get("col_width_px"), None),
            "method": _pick(colp.get("method"), "page-projection"),
            "reliability": ("high" if colp.get("col_gap_px") else "low"),
            "note": colp.get("note"),
        },
        "char_metrics": {
            "cell_h": _pick(cm.get("cell_h"), fcm.get("cell_h_median")),
            "cell_h_median": _pick(cm.get("cell_h_median"), fcm.get("cell_h_median")),
            "box_w": _pick(cm.get("box_w"), fcm.get("box_w_median")),
            "n_samples": _pick(cm.get("n_samples"), fcm.get("n_samples")),
            "source": "contract.G.char_metrics" if cm.get("cell_h") else "facts",
        },
        "page_features": (G.get("page_stats") or {}).get("features"),
        "annot_y_range": (G.get("regions") or {}).get("annot_y_range"),
        "_src": "契约 G（版面约定权威；facts 仅作补齐）",
    }

    # ---- L0 切分规格外化（自足性的关键：把代码内的规则搬进 plan）
    if profile is None:
        profile = {}
        try:
            from profile_store import get_profile
            profile = get_profile(profile_id) or {}
        except Exception as e:
            degraded.append({"block": "profile",
                             "reason": f"档案读取失败：{type(e).__name__}",
                             "fallback": "split_spec 仅由 learned 推，缺档案属性序"})
    split_spec, ss_err = build_split_spec(learned, profile)
    if ss_err:
        degraded.append({"block": "split_spec", "reason": ss_err,
                         "fallback": "外部 agent 需自备锚词（方案不自足）"})

    # ---- 记录形态
    tmpl = _pick(S.get("record_template"), sk.get("order"), [])
    support = _pick(S.get("support"), sk.get("support"), {})
    anchor = _pick(S.get("anchor"), sk.get("anchor"),
                   contract.get("diagnostics", {}).get("anchor"))
    card = _pick(S.get("cardinality"), sk.get("cardinality"), {})
    record = {
        "anchor": anchor,
        "template": tmpl,
        "support": support,
        "required": _pick(S.get("required"), sk.get("required"), []),
        "optional": _pick(S.get("optional"), sk.get("optional"), []),
        "cardinality": card,
        "n_records": _pick(S.get("n_records"), None),
        "page_level_attrs": _pick((contract.get("diagnostics") or {}).get("page_level_attrs"), []),
        "start_rule": {
            "geometric": "band_start | new_column_top | column_gap_ge(col_gap_px)",
            "textual": "short_line(2..8) & not_strong_anchor & next_is_body_attr",
            "priority": ["geometric", "textual"],
            "note": ("几何优先：记录起点几乎总落在列顶或带首；文本判据仅在几何不可用时兜底。"
                     "判据顺序本身是踩坑产物——判据宽窄错序会让后续属性整段丢失。"),
        },
        "continuation": learned.get("continuation"),
        "_src": "契约 S 优先（权威）；缺则 learned.skeleton",
    }

    # ---- 属性
    attrs: List[dict] = []
    prior = A.get("col_offset_prior") or {}
    enums_obs = (learned.get("observed_enums") or {})
    examples = learned.get("value_examples") or {}
    for name in (_pick(contract.get("known_attrs"), list(support.keys()), []) or []):
        pr = prior.get(name) or {}
        en = enums_obs.get(name) or {}
        attrs.append({
            "name": name,
            "support": support.get(name),
            "col_offset_from_anchor": {
                "median": pr.get("median_col_offset_from_anchor"),
                "p25": pr.get("p25"), "p75": pr.get("p75"), "n": pr.get("n"),
            },
            "observed_values": sorted((en.get("values") or {}).keys())[:12],
            "n_distinct_observed": en.get("n_distinct"),
            "examples": (examples.get(name) or [])[:4],
            "anchors": (split_spec.get("anchors") or {}).get(name, []),
            "anchor": anchor,
        })

    # ---- 定位参数
    anchoring = {
        "locate_rate": A.get("locate_rate"),
        "counts": A.get("counts"),
        "col_gap_used": A.get("col_gap_used"),
        "col_offset_prior": prior,
        "cell_h_used": _pick(cm.get("cell_h"), fcm.get("cell_h_median")),
        "char_self_consistency": A.get("char_self_consistency"),
        "_src": "契约 A",
    }

    # ---- 模式（正则/词表；外部 agent 可直接用）
    patterns = learned.get("patterns") or {}
    lexicon = {
        "patterns": patterns,
        "title_candidates": learned.get("title_candidates"),
        "page_constants": learned.get("page_constants"),
    }

    # ---- 验收口径（声明用哪些指标 + 声明**怎么测**；数值不进 plan —— 那是历史的活）
    qa = {
        "metrics": [
            "box_text_align：金标准框中心落入的生成框，文本是否符合（对位率）",
            "head_offset_px：记录首属性落位偏差",
            "conservation：**不静默丢字** —— 未映射成属性的文本必须显式声明（见 execution.invariants）",
            "layout_boundaries_support：目标页结构线能否复现 plan 里的 rel",
        ],
        "baseline": {
            # ⚠ 这是**契约快照的转抄**，不是当前实测值 —— 与 `measure_by` 口径不同。
            # 契约本身可能滞后于代码（§8.7：在盘契约归纳于 html-table /
            # projection-columns 分类器之前）。转抄值随契约刷新而变，**plan 编译一次
            # 就固化一次**；若在"试跑 → 回退"的窗口里编译过，会把临时值永久带进 plan
            # （6645aac 实例：plan 记 0.7396 / 契约回退后为 0.7812，同提交不自洽）。
            # 故此处只报"从哪个快照抄的"，**对位率的权威口径走 `measure_by`**。
            "contract_locate_rate": A.get("locate_rate"),
            "contract_locate_snapshot": {
                "src": "layout_contracts/layout_<pid>.json 的 A.locate_rate（转抄）",
                "contract_built_at": contract.get("built_at"),
                "contract_profile_id": contract.get("profile_id"),
                "stale_risk": "契约未刷新时此值滞后于当前代码；勿当『当前对位率』读。"
                              "权威口径见 measure_by。",
            },
            "agree_all_true": (xp.get("agree") or {}).get("all_true"),
            "attr_coverage_median": 0.699,
            "attr_coverage_note": "实测 7 页：属性值对流的覆盖度中位 0.699（min 0.383）。"
                                  "**这不是门槛而是基线** —— 属性值天然不含锚词。",
        },
        "measured_on": [p.get("stem") for p in usable],
        # ★ 实测入口（P-D）：plan 里**不写数值** —— 数值随产物变动，写进 plan 就成
        # 了第二份真相。这里只声明"谁能量它、按什么口径量"，数值由评测台落历史。
        "measure_by": {
            "impl": "qa_metrics.measure(profile_id)",
            "cli": "python qa_metrics.py --record",
            "api": "POST /api/eval/plan_qa",
            "curve": "GET /api/eval/plan_series",
            "read": "box_text_align 低须与 box_text_align_covered 合读："
                    "covered 高而 align 低 = 覆盖不够；covered 也低 = 框内容错。"
                    "**且必须并列 gen_per_gold** —— 对位率对框的粒度敏感。",
        },
        "_src": "契约 A + facts.cross_page；**实测值由 qa_metrics 落评测历史**（P-D）",
    }

    # ---- 归一化口径（P-C 补：外部 agent 最容易踩偏的一处）
    # L0 全程跑在**繁简折叠后的流**上（锚词表简繁双写；不折叠则命中率归零，踩过坑），
    # 但**取值必须从原文切** —— 项目纪律「禁繁简转换，保原字形」。
    # 不写明这一条，外部 agent 要么匹配不到、要么把字形改掉。
    text_norm = {
        "locate": "定位（找锚词、找值的位置）在**繁简折叠流**上做："
                  "逐字折叠到 zh-hans，并去空白。",
        "value": "取值从**原文**切 —— 保原字形，**禁繁简转换**。",
        "identity": "折叠必须**逐字 1:1**（长度不变），否则位置无法映射回原文。",
        "impl": "zhconv.convert(s, 'zh-cn')；环境缺 zhconv 时退化为原样比较"
                "（此时锚词表里的简繁双写候选各自生效）。",
        "why": "OCR 原文为繁体，而锚词表可能以简体登记 —— 不折叠则命中率归零（实测踩坑）。",
        "verified_by": "seam.validate 的 glyph_folded 警告：值若只在折叠流里匹配得到，"
                       "即说明执行器改了字形，违反 value 口径。",
    }

    # ---- 执行说明书（外部 agent 的唯一依据）
    execution = {
        "input": ["目标页原图（用于像素投影）",
                  "OCR 逐行文本流（每行含文本；框可选，缺框时按列投影重切）"],
        "steps": _execution_steps(reading, page_model, record, attrs, anchoring),
        "invariants": [
            "**不静默丢字**：产出的属性值 + unassigned_text 必须覆盖输入文本的每一个字符。"
            "注意 —— 单靠属性值**覆盖不到全文**（实测中位 0.699）：锚词本身不是值。"
            "故要求的是「未覆盖部分必须显式声明」，不是「属性值覆盖全文」。",
            "文本顺序不得改变（重排也算破坏）；值的字形必须来自原文（禁繁简转换）。",
            "无框区（unannotated_*）不产出记录，但其文本必须保留（计入 unassigned_text）。",
            # ★ P0-3b（2026-09-13）：这条是**方案层必须交代**的事 —— 在此之前 plan 只
            #   在 `page_model.regions[].covered` 里声明了症状，**没给处置**，外部 agent
            #   只能自己猜（猜的结果通常是"照切不误"，于是伪命中倒进待核队列）。
            "**版式未覆盖的页不得按主族骨架切分**：先查本页在 `page_model.regions[]` 的"
            "`covered`；为 `false` 时该页所属族没有跨页证据，硬套主族骨架会产**伪命中**"
            "（实测把「译书院」「房租」、人名当成公司名切成记录）—— 应降级为 `single_line`"
            "（整页一条兜底，不丢字），并在结果的 `layout.policy` 声明 `page_fallback`。",
        ],
        "on_failure": {
            "chain": ["rule", "l1", "l2", "single_line"],
            "meaning": {"rule": "几何 + 锚词直投，零模型调用",
                        "l1": "仅对空缺属性补值，绝不覆盖已有值",
                        "l2": "整页复述兜底（成本最高）",
                        "single_line": "整页一条，保底不丢字"},
            "rule": "逐级降级；每一级都必须满足 invariants",
            "trace": "每一级的尝试与结果记入结果的 `tier_trace`（跳级只告警，不判错）",
        },
        "verify": "系统只提供结果校验器（`seam.validate`：守恒 / 对位 / 降级合规），"
                  "不干预过程。",
    }

    plan = {
        "_schema_version": SCHEMA_VERSION,
        "_kind": KIND,
        "profile_id": profile_id,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "authority": {
            "contract": "「版面约定类」信息的权威（列网格 / 锚词 / 属性集 / 模板）。plan 单向编译自契约。",
            "plan": "「识别与标注方案类」信息的权威，含契约没有的横带结构、记录起点判据、执行说明。",
            "rule": "重叠部分以契约为准；不重叠部分各自权威。编译只读契约，绝不写回。",
        },
        "refs": {
            "contract": {"path": str(c_path), "sha256": _sha256(c_path),
                         "schema_version": contract.get("_schema_version"),
                         "built_at": contract.get("built_at"),
                         "present": c_path.exists()},
            "learned": {"path": str(l_path), "sha256": _sha256(l_path),
                        "present": l_path.exists()},
            "facts": {"path": str(f_path), "sha256": _sha256(f_path),
                      "schema_version": facts.get("_schema_version"),
                      "built_at": facts.get("built_at"),
                      "present": f_path.exists()},
        },
        "source": {
            "pages": [p.get("stem") for p in usable],
            "n_pages": len(usable),
            # `excluded` 自 P0-1 起含两类：`empty`（无框）与 `thin`（有框但证据不足）。
            # 故带上 `level` —— 只说"被排除"会让两类混成一个数字（Dingo 的"分级不二值"）。
            "excluded": [{"stem": p.get("stem"),
                          "level": (p.get("quality") or {}).get("level"),
                          "reason": p.get("reason")}
                         for p in pages if p.get("excluded")],
            "quality": (facts.get("source") or {}).get("quality"),
            "n_annotations": sum(int(p.get("n_annotations") or 0) for p in usable),
        },
        "reading": reading,
        "text_norm": text_norm,
        "page_model": page_model,
        "record": record,
        "attributes": attrs,
        "anchoring": anchoring,
        "split_spec": split_spec,
        "lexicon": lexicon,
        "qa": qa,
        "execution": execution,
        "fallbacks": ["rule", "l1", "l2", "single_line"],
        "degraded": degraded,
        "provenance": {
            "reading": "contract.G.reading_order（优先）→ facts.pages[*].reading",
            "page_model.layout_boundaries": "facts.pages[*].band_source.strong_separators 跨页归并（主族）",
            "page_model.layout_boundaries_contract": "contract.G.layout_bands —— 由 contract_backfill.py 从**上一版 plan** 回迁；本层只做对位观测（不反向约束）",
            "page_model.layout_clusters": ("按结构线形态单链聚类 → 版式族；族内归并"
                                           "（异版式页不可混算）；`coverage` 标可归纳性"
                                           f"（≥{MIN_FAMILY_PAGES} 页才算方案来源）"),
            "page_model.regions": ("版式结构线切分**整页**（含无人标注区）；用各页自身族的边界；"
                                   "`covered` = 该页所属族有跨页证据（单页族为 false）"),
            "page_model.columns": "contract.G.column_profile（版面约定权威）",
            "page_model.char_metrics": "contract.G.char_metrics → facts（补齐）",
            "record": "contract.S → learned.skeleton",
            "attributes": "contract.known_attrs + A.col_offset_prior + learned.observed_enums",
            "anchoring": "contract.A",
            "text_norm": "项目纪律（定位折叠流 / 取值保原字形）+ zhconv 实现口径",
            "split_spec": "rule_split.build_spec 外化（L0 全部规则：锚词/槽位/阈值/常量）",
            "lexicon": "learned.patterns / title_candidates / page_constants",
            "execution": "本层新作（规则的可执行转写）",
        },
    }
    return plan


def _execution_steps(reading, page_model, record, attrs, anchoring) -> List[str]:
    """把 plan 里的规则区转写成**可照做的步骤**（自足性的载体）。"""
    cols = page_model.get("columns") or {}
    n = cols.get("n_columns")
    gap = cols.get("col_gap_px")
    cm = page_model.get("char_metrics") or {}
    cell = cm.get("cell_h")
    order = reading.get("order")
    lines = page_model.get("layout_boundaries") or []
    nreg = max((len(r.get("regions") or []) for r in (page_model.get("regions") or [])),
               default=0)
    steps = [
        f"1. 定阅读序：主序为**列**（{order}）。列间自右向左，列内自上而下。"
        "这一步决定后面所有顺序，不得改变。",
        f"2. 分区定位：按 plan.page_model.layout_boundaries 的相对页高（rel × 本页高）"
        f"把整页划成 {nreg} 个区（报头 / 内容 / 页脚）。"
        "**结构线不是记录边界** —— 实测官报版式里那条横线在**记录内部**"
        "（列内自上而下是「大字公司名 → 横线 → 属性正文」），记录跨带是常态。"
        "分区只用于：① 不产出记录的无标注区（role=unannotated_*）判位；"
        "② 决定一段文本落在哪个区（供 selector 定位）。"
        "`layout_boundaries_contract` 记录了本版线与**契约已确认约定**的对位；"
        "判定非 `conforms` 时以本版 rel 为准，并在结果的 `warnings` 里报出来。",
        f"3. 切列：用列的像素水平投影把每个有内容区切成列"
        f"（参考列宽 {cols.get('col_width_px')}px、列间距 {gap}px，n_columns={n} 仅作量级参考，"
        "投影带常含页眉/边注噪声，**不要硬凑列数**）。"
        "注意：**一条记录会横跨多列**（用到几列取决于文本长度），"
        "故「列」不是记录的划分单位，记录起止只由第 4 步的起点判据决定。",
        f"4. 找记录起点（判据见 plan.record.start_rule）："
        f"优先级 = {record['start_rule']['priority']}。"
        f"几何优先 —— {record['start_rule']['geometric']}；"
        f"几何不可用时用文本兜底 —— {record['start_rule']['textual']}。"
        f"记录首属性 = {record.get('anchor')}。",
        f"5. 属性定位：在记录**内部**按顺序匹配各属性名的锚词（见 plan.attributes[].name 与 "
        "plan.lexicon.patterns）。锚词命中即取值；值可能跨行（见 record.continuation）。"
        "**定位在繁简折叠流上做，取值从原文切**（见 plan.text_norm）——"
        "不折叠会因原文是繁体而匹配不到，折叠后取值则破坏原字形。",
        f"6. 映射成框：用字格 cell_h={cell} px 与 anchoring.col_offset_prior 的列偏移，"
        "把每个值映射为选框。优先级：锚词位置 > 列偏移先验 > 均分。"
        "子框必须夹在**所属行框之内**（行框偏紧时改用该行自身字高，别用全局 cell_h 硬套）。",
        "7. 自检：**不静默丢字** —— 属性值 + unassigned_text 必须覆盖输入全部字符"
        "（属性值本身覆盖不到全文，缺口要显式声明）；值的字形必须来自原文；"
        "同一行不得被两条记录引用。不满足则降级重跑（见 on_failure.chain）。"
        "产出交给 `seam.validate` 校验（守恒 / 对位 / 降级合规）。",
    ]
    return steps


# ============================================================ 渲染 / 落盘

def render_md(plan: dict) -> str:
    """plan → 执行说明书（人可读、可改、可直接给外部 agent）。"""
    pm = plan.get("page_model") or {}
    rec = plan.get("record") or {}
    anc = plan.get("anchoring") or {}
    ex = plan.get("execution") or {}
    L: List[str] = []
    A = L.append

    def _d(v, dash: str = "—"):
        """空值显示为破折号——表格里裸写 `None` 会被误读成字符串。"""
        return dash if v is None else v

    def _js(v, dash: str = "—"):
        """JSON 内联渲染；空值 → 破折号。"""
        return json.dumps(v, ensure_ascii=False) if v else dash

    # 属性按**记录模板序**排（读表顺序与记录内出现顺序一致）；模板外的按原序
    _tpl_o = {a: i for i, a in enumerate(rec.get("template") or [])}
    _attrs = sorted((plan.get("attributes") or []),
                    key=lambda a: _tpl_o.get(a.get("name"), 999))

    A(f"# 识别与标注方案 · `{plan.get('profile_id')}`")
    A("")
    A(f"> schema `{plan.get('_schema_version')}` · 生成于 {plan.get('built_at')} · "
      f"编译自 {len((plan.get('source') or {}).get('pages') or [])} 页金标准")
    A("")
    A("本文档是**层 3（方案落地）的唯一依据**。它自足——不需要读任何项目源码；")
    A("若你需要用它驱动自己的 agent，照 `## 执行步骤` 做即可。")
    A("")

    # ---- ★ 被「显式提升」改写过的 plan（P-S3）：必须自己说清楚 ----
    # 为什么单起一节而不是塞进 provenance：这份 md 是要给外部 agent / 人照着做的。
    # 提升改的是 split_spec，而那个值本是 rule_split.build_spec 算出来的 ——
    # 下次重编译会把它**静默**覆盖。不写在显眼处，"这份方案到底是什么"就答不准。
    promo = plan.get("promotion") or {}
    if promo:
        pv = promo.get("variant") or {}
        pb = promo.get("backup") or {}
        ph = promo.get("history") or {}
        A("## ★本方案已被「显式提升」改写过（**不是纯编译产物**）")
        A("")
        A(f"- 提升于 **{promo.get('promoted_at')}**（原编译时间 {promo.get('compiled_built_at')}）")
        A(f"- 改动：`{promo.get('knob')}`（{promo.get('label')}）"
          f" {promo.get('base_value')} → {promo.get('value')}"
          + ("　**越出代码标定区间**" if promo.get("out_of_calibration") else ""))
        A(f"- 来自变体：`{pv.get('id')}`（{pv.get('path')}）")
        A(f"- 提升前版本已备份：`{pb.get('json')}`（**原字节**，可原样回滚）")
        A(f"- 事件记录：`{ph.get('path')}`　行 `{ph.get('row_id')}`")
        A("")
        if promo.get("recompile_warning"):
            A(f"> ⚠ **{promo['recompile_warning']}**")
            A(">")
        if promo.get("products_warning"):
            A(f"> ⚠ {promo['products_warning']}")
        A("")
        _ds = promo.get("delta_summary") or {}
        _tot = _ds.get("totals") or []
        if _tot:
            A("提升时实测的 Delta（现场重跑 base、同一执行器；**只报事实，不判断优劣**）：")
            A("")
            A("| 指标 | 提升前 | 提升后 | 变化 |")
            A("|---|---|---|---|")
            for _r in _tot:
                # ★ 变量名不许叫 `_d` —— 本函数上面已把 `_d` 定义成"空值显示破折号"的助手，
                #   再用 `_d = <int>` 会把它遮蔽掉，后面每一处 `_d(...)` 都变成
                #   `'int' object is not callable`（实测踩到：提升后的 plan 渲染 .md 直接抛）
                _dv = _r.get("delta")
                _tail = f"{_dv:+g}" if isinstance(_dv, (int, float)) and _dv else "—"
                A(f"| {_r.get('label')} | {_r.get('base')} | {_r.get('variant')} | {_tail} |")
            A("")

    A("## 权威与来源")
    A("")
    auth = plan.get("authority") or {}
    A(f"- 契约（权威）：{auth.get('contract', '')}")
    A(f"- 本方案：{auth.get('plan', '')}")
    A(f"- 冲突处理：{auth.get('rule', '')}")
    A("")
    refs = plan.get("refs") or {}
    A("| 输入 | 存在 | sha256（前 12 位）| 相对上一版 |")
    A("|---|---|---|---|")
    for k in ("contract", "learned", "facts"):
        r = refs.get(k) or {}
        h = (r.get("sha256") or "")[:12] or "—"
        sv = r.get("stale")
        # ⚠ 三态，必须显式判 `is True` / `is False`：写成 `if sv:` 会把
        #   None（无从判定）和 False（未变）混成同一支 —— 那是两种相反的结论。
        s_txt = "**变了**" if sv is True else ("未变" if sv is False else "—（无上一版）")
        A(f"| {k} | {'是' if r.get('present') else '**否（已降级）**'} | `{h}` | {s_txt} |")
    A("")
    # ⚠ 变量名加后缀，别用 `_pv` / `_ch` 这类短名 —— 本函数已经踩过一次
    #   「单字母助手被循环变量遮蔽」（`_d`，见 §51.6），同函数内短名就是地雷。
    _pvref = refs.get("_prev_plan") or {}
    if _pvref.get("present"):
        _pvchg = _pvref.get("changed") or []
        A(f"*注：「相对上一版」比的是盘上那一版 plan（编译于 `{_pvref.get('built_at')}`，"
          f"本次核对 `{refs.get('checked_at')}`）。"
          + (f"**本次编译输入变更：{' + '.join(_pvchg)}。**" if _pvchg else "三份输入均未变。")
          + "「**此刻**盘上输入是否还对得上本方案」由接缝图现场重算，是另一件事。*")
    elif refs:
        A("*注：「相对上一版」列 = **无从判定**（没有上一版 plan 可比，**不等于「没变」**），"
          "从下一次编译起才有值。「此刻盘上输入是否还对得上本方案」由接缝图现场重算，"
          "不受此影响。*")
    A("")

    if plan.get("degraded"):
        A("## 降级项（编译时缺料）")
        A("")
        for d in plan["degraded"]:
            A(f"- **{d.get('block')}**：{d.get('reason')} → {d.get('fallback')}")
        A("")

    A("## 怎么读（reading）")
    A("")
    rd = plan.get("reading") or {}
    A(f"- 阅读序：`{rd.get('order')}`；第一主轴 = **{rd.get('primary_axis')}**")
    A(f"- 投票证据：`{json.dumps(rd.get('votes'), ensure_ascii=False)}`")
    A("")
    alg = rd.get("algorithm") or {}
    if alg:
        A(f"### 阅读序怎么算（`{alg.get('id')}`）")
        A("")
        A("**顺序必须可复现**：只写 `rtl_ttb` 这个名字是复现不出来的"
          "（实测朴素「x 降序 + y 升序」在官报 0001 上恰好一致，0003 与 manual 页就不一致）。")
        A("故决策程序整体外化如下：")
        A("")
        for s in (alg.get("steps") or []):
            A(f"- {s}")
        A("")
        A(f"- 常量：`{json.dumps(alg.get('constants'), ensure_ascii=False)}`")
        A(f"- 为何不用固定分箱：{alg.get('why_not_fixed_bin')}")
        A(f"- 一致性怎么验：{alg.get('conformance')}")
        A("")

    tn = plan.get("text_norm") or {}
    if tn:
        A("## 文本怎么归一（text_norm）")
        A("")
        A(f"- **定位**（找锚词、找值的位置）：{tn.get('locate')}")
        A(f"- **取值**：{tn.get('value')}")
        A(f"- **1:1 约束**：{tn.get('identity')}")
        A(f"- 实现：{tn.get('impl')}")
        A(f"- 为什么：{tn.get('why')}")
        A("")
        A("> 这一节不是可选的：OCR 原文是繁体、锚词表以简体登记，**不折叠匹配不到**；"
          "而折叠后取值又**破坏原字形**。两边都要照做。")
        A("")

    A("## 页面上有什么（page_model）")
    A("")
    clusters = pm.get("layout_clusters") or []
    if clusters:
        A("### 版式族（同一 profile 内可能有多个版式）")
        A("")
        A(f"本方案共识别出 **{len(clusters)}** 个版式族。几何参数（结构线）**按族各表**；")
        A("语义规则（记录形态 / 属性 / 词表）**跨族共享**——它们来自同一批金标准。")
        A("给目标页定参数时，先看它属于哪一族。")
        A("")
        # ★ P1-2（2026-09-13）：族分两类 —— **可归纳**（≥ 最少页数，能当方案来源）
        #   与**单页证据**（只有一页，不足以跨页验证）。后者不生成族级参数：
        #   给只有 1 页证据的族配参数 = 把那页金标准抄一遍，对别的页零预测力。
        A(f"**族的可归纳性**：一个族至少要有 **{MIN_FAMILY_PAGES} 页**才当方案来源；")
        A("只有 1 页的族记为 `single_page_evidence`（版式未经跨页验证），"
          "其页面的版式参数沿用主族，产出请人工复核。")
        A("")
        A("| 族 | 主族 | 页数 | 可归纳性 | 页 | 中位结构线数 | 参考页高 |")
        A("|---|---|---|---|---|---|---|")
        for c in clusters:
            pages_s = "、".join(c["pages"])
            A(f"| {c['id']} | {'★' if c.get('is_primary') else ' '} | {c['n_pages']} "
              f"| `{c.get('coverage')}` | {pages_s} | {c['median_n_lines']} "
              f"| {c.get('ref_page_h')} |")
        A("")
        for c in clusters:
            head = "★ 主族" if c.get("is_primary") else f"族 {c['id']}"
            A(f"**{head}**（{c['n_pages']} 页）的结构线：")
            A("")
            if not c.get("layout_boundaries"):
                A("- （本族无足够强的结构线：页数或线数不足，几何上无可迁移参数）")
                A("")
                continue
            A("| rel | 跨页波动 | 命中 | 形态 | 在标注区内 | 弱证据 | 参考页高下 px |")
            A("|---|---|---|---|---|---|---|")
            for l in c["layout_boundaries"]:
                A(f"| {l['rel']} | ±{l['rel_spread']} | {l['n_pages_hit']}/{c['n_pages']} "
                  f"| {l['kind']} | {'是' if l['in_gold_any'] else '**否**'} | {l['weak']} "
                  f"| {l['px_at_ref_page_h']} |")
            A("")
        A("> **rel 是权威，px 只是参考页高下的示例**——目标页高不同时按 `rel × 本页高` 换算。")
        A("> `在标注区内 = 否` 的线仍是版式结构（如标题区边界），人通常不会标注它，")
        A("> 因为人对这张图的认知与执行器需要的认知不是同一件事。")
        A("> `弱证据` 列 = 该簇里形态过短（≤2 行）的成员数，>0 时建议结合 `rel_spread` 判断可信度。")
        A("")
    ba = pm.get("layout_boundaries_contract") or {}
    if ba.get("verdict") not in (None, "unavailable", "no_contract_bands"):
        A("### 与契约回迁带的对位（契约当权威的最小形态）")
        A("")
        A("契约 `G.layout_bands` 是**上一版 plan 回迁**过去的版面约定"
          "（`contract_backfill.py`）。下面这张表是「本版归纳 vs 已确认约定」的对位 ——"
          "**只观测，不改本版切分**：先把权威验证出来（连续多版都吻合），"
          "才谈用契约带覆盖 plan 带。")
        A("")
        A(f"- 判定 **{ba.get('verdict')}**：命中 {ba.get('n_matched')}/"
          f"{ba.get('n_contract')}，最大偏差 {ba.get('max_dev_px')} px"
          f"（容差 {ba.get('tol')} rel，参考页高 {ba.get('ref_page_h')}）")
        A(f"- plan 独有 {ba.get('n_plan_only')} 条 / 契约独有 "
          f"{ba.get('n_contract_only')} 条 —— 不为空即说明版式在漂，别当噪声")
        if ba.get("matched"):
            A("")
            A("| 本版 rel | 契约 rel | 偏差 rel | 偏差 px |")
            A("|---|---|---|---|")
            for m in ba["matched"]:
                A(f"| {m['rel_plan']} | {m['rel_contract']} | {m['dev_rel']} "
                  f"| {m['dev_px']} |")
        A("")
    regs = pm.get("regions") or []
    if regs:
        A("### 全页分区（含无人标注区）")
        A("")
        A("人只标自己关心的内容，不会标出全部版面结构；分区**覆盖整页**，")
        A("这样执行器才知道「这一页总共有几块、哪块没有真值」。")
        A("")
        for r0 in regs[:3]:
            A(f"- `{r0.get('stem')}`（版式族 {r0.get('cluster')}）")
            for rg in (r0.get("regions") or []):
                attrs = "、".join(rg.get("attrs") or []) or "—"
                A(f"  - 区 {rg['id']} rel={rg['rel']} role=`{rg['role']}` "
                  f"金标框 {rg.get('n_gold_boxes', 0)} 个；属性：{attrs}")
        if len(regs) > 3:
            A(f"  - …… 其余 {len(regs) - 3} 页见 plan.json")
        A("")
    cols = pm.get("columns") or {}
    A("### 列与字格")
    A("")
    A(f"- 列：n_columns={_d(cols.get('n_columns'))}（样本 {_d(cols.get('n_columns_samples'))}）、"
      f"列宽 {_d(cols.get('col_width_px'))}px、列间距 {_d(cols.get('col_gap_px'))}px、"
      f"方法 `{_d(cols.get('method'))}`、可靠性 **{_d(cols.get('reliability'))}**")
    cma = pm.get("char_metrics") or {}
    A(f"- 字格：cell_h={_d(cma.get('cell_h'))}px（中位 {_d(cma.get('cell_h_median'))}px）、"
      f"box_w={_d(cma.get('box_w'))}px、样本 {_d(cma.get('n_samples'))}")
    if cols.get("note"):
        A(f"- 注意：{cols['note']}")
    A("")

    A("## 一条记录长什么样（record）")
    A("")
    A(f"- 记录首属性（anchor）：**{rec.get('anchor')}**")
    A(f"- 模板（按序）：{' → '.join(rec.get('template') or [])}")
    A(f"- 必备：{'、'.join(rec.get('required') or []) or '—'}")
    A(f"- 可选：{'、'.join(rec.get('optional') or []) or '—'}")
    A(f"- 页级属性（不进记录）：{'、'.join(rec.get('page_level_attrs') or []) or '—'}")
    A(f"- 支撑度：`{_js(rec.get('support'))}`")
    A(f"- 条目长度分布：`{_js(rec.get('cardinality'))}`")
    A("")
    sr = rec.get("start_rule") or {}
    A("**记录起点判据**（顺序即优先级）：")
    A("")
    A(f"1. 几何：`{sr.get('geometric')}`")
    A(f"2. 文本：`{sr.get('textual')}`")
    A("")
    if sr.get("note"):
        A(f"> {sr['note']}")
        A("")

    A("## 属性怎么定（attributes）")
    A("")
    A("| 属性 | 支撑度 | 列偏移中位 | 观测取值数 | 示例 |")
    A("|---|---|---|---|---|")
    for a in _attrs:
        co = a.get("col_offset_from_anchor") or {}
        ex4 = "；".join((a.get("examples") or [])[:2]) or "—"
        A(f"| {a['name']} | {_d(a.get('support'))} | {_d(co.get('median'))} "
          f"| {_d(a.get('n_distinct_observed'))} | {ex4[:40]} |")
    A("")

    lex = plan.get("lexicon") or {}
    pat = lex.get("patterns") or {}
    if pat:
        A("### 可用模式与词表（execution 可直接使用）")
        A("")
        for k, v in pat.items():
            if k == "note":
                continue
            A(f"- `{k}`：`{json.dumps(v, ensure_ascii=False)[:160]}`")
        if pat.get("note"):
            A(f"- 说明：{pat['note']}")
        A("")
    if lex.get("title_candidates"):
        A(f"- 抬头候选词：{'、'.join(lex['title_candidates'][:10])}")
        A("")
    if lex.get("page_constants"):
        A(f"- 页面常量（每页必现，非记录内容）：{'、'.join(lex['page_constants'])}")
        A("")

    A("## 切分规则（split_spec）")
    A("")
    ss = plan.get("split_spec") or {}
    if not ss:
        A("**（缺失）** 编译时未能外化 L0 规格，外部执行器需自备锚词——此方案**不自足**。")
        A("")
    else:
        A("本段是 L0 确定性切分的**全部规则**，已从代码里外化出来——")
        A("照它执行**不需要读任何项目源码**。这正是「方案自足」的实质：")
        A("凡是不看代码就不出来的信息，都在这里。")
        A("")
        A(f"- 记录首属性：`{_d(ss.get('anchor_attr'))}`")
        slots = "；".join(" / ".join(p) for p in (ss.get("person_slots") or []))
        A(f"- 人名槽位（名字 ↔ 功名成对）：{slots or '—'}")
        A(f"- 长度阈值：name_len={_d(ss.get('name_len'))}、"
          f"name_max_len={_d(ss.get('name_max_len'))}、"
          f"body_min_len={_d(ss.get('body_min_len'))}")
        A(f"- 页面常量（每页必现、**不是**记录内容）："
          f"{'、'.join(ss.get('page_constants') or []) or '—'}")
        A("")
        A("### 属性锚词表")
        A("")
        A("`mode=self` → 命中的文本段本身就是值；`mode=after` → 取锚词**之后**的片段。")
        A("锚词由**属性名**推出（属性名即语义标签），不是硬编码词表。")
        A("**同一属性出现多行 = 多个候选锚词，任一命中即可**（按行序取先命中者）。")
        A("锚词为空的属性（如记录首属性）由几何给出，不做文本抽取。")
        A("")
        A("| 属性 | 锚词正则 | 模式 |")
        A("|---|---|---|")
        for a in _attrs:
            for an in (a.get("anchors") or []):
                re_s = (an.get("re") or "").replace("|", "\\|")
                A(f"| {a['name']} | `{re_s[:120]}` | {an.get('mode')} |")
        A("")
        if ss.get("titles_re"):
            A(f"- 抬头（功名）候选词表：`{ss['titles_re'][:240]}`")
            A("")
        if ss.get("_note"):
            A(f"> {ss['_note']}")
            A("")

    A("## 定位参数（anchoring）")
    A("")
    A(f"- 契约实测锚词定位率：**{_d(anc.get('locate_rate'))}**"
      f"（计数 `{json.dumps(anc.get('counts'), ensure_ascii=False)}`）")
    A(f"- 映射用字格：cell_h = {_d(anc.get('cell_h_used'))} px")
    A(f"- 列间距实测：{_d(anc.get('col_gap_used'))} px")
    A("")

    A("## 执行步骤（层 3 照此执行）")
    A("")
    A("**输入**：")
    for i in (ex.get("input") or []):
        A(f"- {i}")
    A("")
    for s in (ex.get("steps") or []):
        A(s)
        A("")
    A("**不变式（违反即失败）**：")
    for i in (ex.get("invariants") or []):
        A(f"- {i}")
    A("")
    of = ex.get("on_failure") or {}
    A(f"**失败降级**：{' → '.join(of.get('chain') or [])}")
    A("")
    for k, v in (of.get("meaning") or {}).items():
        A(f"- `{k}`：{v}")
    A("")
    A(f"**校验**：{ex.get('verify')}")
    A("")

    A("## 结果怎么交（annotation_result schema v0.1）")
    A("")
    A("执行器交活一律按 `seam.RESULT_FIELDS` 的格式（JSON 一份/页）。最小形状：")
    A("")
    A("```jsonc")
    A('{')
    A('  "_schema_version": "0.1.0", "_kind": "annotation_result",')
    A(f'  "profile_id": "{plan.get("profile_id")}", "stem": "<页>",')
    A('  "tier": "rule",                       // 必须 ∈ plan.fallbacks')
    A('  "tier_trace": [{"tier":"rule","ok":true,"reason":""}],')
    A('  "input": {"text_stream": "<消费的 OCR 文本流>", "n_chars": 0, "n_lines": 0},')
    A('  "page": {"w": 0, "h": 0},             // 原图真实尺寸（勿用框极值推）')
    A('  "records": [')
    A('    {"record_index": 0, "lines": [2,3,4],   // 引用的行下标（防重复占用）')
    A('     "attrs": [{"attr":"公司名","text":"...","box":[x0,y0,x1,y1],')
    A('                "lines":[2],"evidence":{}}]}')
    A('  ],')
    A('  "unassigned_text": [{"text":"...","reason":"header|footer|'
      'unannotated_region|no_attribute"}]')
    A('}')
    A("```")
    A("")
    A("校验：`python -u seam.py --result <file> --plan <plan.json> --lines`")
    A("（`--lines` 会读真实 OCR 行流做最强校验；不给则只核结果内部自洽。）")
    A("")

    A("## 验收口径（qa）")
    A("")
    qa = plan.get("qa") or {}
    for m in (qa.get("metrics") or []):
        A(f"- {m}")
    A("")
    A(f"基线：`{json.dumps(qa.get('baseline'), ensure_ascii=False)}`")
    A("")
    A("### 怎么测（本文件**不写数值**）")
    A("")
    mb = qa.get("measure_by") or {}
    if mb:
        A(f"- 实现：`{mb.get('impl')}`　CLI：`{mb.get('cli')}`")
        A(f"- 接口：`{mb.get('api')}`　曲线：`{mb.get('curve')}`")
        A("")
        A(f"> {mb.get('read')}")
        A("")
        A("数值随产物变动，写进本文件就成了第二份真相 —— 故只声明"
          "「谁能量它、按什么口径量」，数值由评测台落历史（`data/eval_reports/history.jsonl`，"
          "`kind=plan_qa`）。**改动算法后必须重跑预生成**，否则生成框没变、这一列也不会变"
          "（每次测量都记产物指纹 `gen` 以便核对）。")
    A("")
    A("---")
    A("")
    A("*本文件由 `plan_build.py` 生成；改规则请改契约或金标准后重编译，")
    A("不要直接编辑本文件——下次编译会覆盖。*")
    return "\n".join(L)


# ============================================================ refs 记账（相对上一版）
def load_prev_plan(out_dir: Optional[Path], profile_id: str) -> Tuple[Optional[dict], Optional[Path]]:
    """读**盘上上一版** plan，返回 `(doc|None, path|None)`。失败不抛（记账不该阻断编译）。

    ⚠ 必须**精确到 `plan_<pid>.json`，不许 glob**：`data/plans/` 下有两个子目录 ——
    `_history/`（P-S3 的备份）与 `variants/`（P-S2 的试算变体）。递归搜索会把
    备份/变体当成"上一版正式方案"，**不报错、只是错**（同 §51 的 `_history/` 防线）。
    改这里前先看 `tests/test_plan_promote.py` 里那条"源码里不许出现 rglob"的断言。
    """
    p = Path(out_dir or DEFAULT_OUT_DIR) / f"plan_{profile_id}.json"
    if not p.exists():
        return None, None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, p                      # 文件在但读不动 → 视作无可比对象，但路径留痕
    return (doc if isinstance(doc, dict) else None), p


def annotate_refs_stale(plan: dict, out_dir: Optional[Path] = None) -> dict:
    """**就地**给 `plan['refs']` 补 `stale`、`checked_at`、`_prev_plan`；返回摘要。

    语义见模块 docstring 第 2 条。**本函数只答一件事**：
        相对盘上那份 `plan_<pid>.json`，这三份输入的 sha256 变了没有？
    它**不答**「此刻现盘还对得上吗」—— 那是 `seam_map._refs_status` 的活（现场重算）。

    逐 key 取值：
        `True`  变了（sha256 不同，或 present 由有变无 / 由无变有）
        `False` 与上一版一致
        `None`  **无从判定**：没有上一版 plan，或上一版没有这一项
                （首次编译就属于这种；**不要读成"没变"**）

    幂等：同一份 plan 连调两次，除 `checked_at` 外结果相同。
    """
    d = Path(out_dir or DEFAULT_OUT_DIR)
    prev, prev_path = load_prev_plan(d, plan.get("profile_id") or "")
    prev_refs = (prev or {}).get("refs") or {}

    refs = plan.get("refs")
    if not isinstance(refs, dict):
        refs = plan["refs"] = {}

    changed: List[str] = []
    for k in ("contract", "learned", "facts"):
        r = refs.get(k)
        if not isinstance(r, dict):
            continue
        pr = prev_refs.get(k)
        pr = pr if isinstance(pr, dict) else None
        if prev is None or pr is None:
            r["stale"] = None
            continue
        moved = (r.get("sha256") != pr.get("sha256")) or \
                (bool(r.get("present")) != bool(pr.get("present")))
        r["stale"] = bool(moved)
        if moved:
            changed.append(k)

    now = datetime.now().isoformat(timespec="seconds")
    refs["checked_at"] = now
    refs["_prev_plan"] = {
        "path": str(prev_path) if prev_path else None,
        "present": prev is not None,
        "built_at": (prev or {}).get("built_at"),
        "changed": changed,
        "note": "本块 stale 的语义 = 相对**这一版** plan 的 sha256 有无变化；"
                "None = 无从判定（无上一版），不是「没变」。"
                "「此刻盘上输入是否还对得上」由 seam_map 现场重算，是另一件事。",
    }
    return {"prev_present": prev is not None, "changed": changed, "checked_at": now}


def save(plan: dict, out_dir: Optional[Path] = None) -> Tuple[Path, Path]:
    """原子写 plan.json + plan.md。

    落盘前**就地**补 `refs.*.stale`（相对盘上上一版 plan 的输入变化，见
    `annotate_refs_stale`）。这一步只能放在**落盘时刻** —— 它要读旧文件，
    而 `build_plan()` 的承诺是"纯编译、不读盘"。
    """
    d = Path(out_dir or DEFAULT_OUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    pid = plan["profile_id"]
    jp = d / f"plan_{pid}.json"
    mp = d / f"plan_{pid}.md"
    rep = annotate_refs_stale(plan, d)
    data_io.atomic_write_json(jp, plan)
    data_io.atomic_write_text(mp, render_md(plan))
    tail = "无上一版可比" if not rep["prev_present"] else (",".join(rep["changed"]) or "三份输入均未变")
    log.info("[plan_build] 方案已保存: %s / %s（相对上一版：%s）", jp.name, mp.name, tail)
    return jp, mp


def plan_exit_code(rc: int, plan) -> int:
    """唯一判定点：底层 rc==0 **且** 产物可解析 ⇒ 0，否则 1。

    `chronicles plan` 取用本函数，包装层不重判（同 `triage_exit_code` 模式）。
    `plan` 传包装层已读回的产物（None = 读不到 / 不是合法 JSON）。
    """
    return 0 if (rc == 0 and plan is not None) else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="层 2：金标准规则 → 识别与标注方案")
    ap.add_argument("--profile", required=True, help="profile_id（prof_...）")
    ap.add_argument("--out", default=None, help="输出目录（默认 data/plans）")
    ap.add_argument("--facts-dir", default=None)
    ap.add_argument("--contracts-dir", default=None)
    ap.add_argument("--rules-dir", default=None)
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    plan = build_plan(a.profile,
                      facts_dir=Path(a.facts_dir) if a.facts_dir else None,
                      contracts_dir=Path(a.contracts_dir) if a.contracts_dir else None,
                      rules_dir=Path(a.rules_dir) if a.rules_dir else None)
    jp, mp = save(plan, Path(a.out) if a.out else None)

    pm = plan["page_model"]
    print(f"[plan] {jp}")
    print(f"  页 {plan['source']['n_pages']} / 标注 {plan['source']['n_annotations']}")
    print(f"  阅读序 {plan['reading']['order']}  锚属性 {plan['record']['anchor']}")
    print(f"  模板 {' → '.join(plan['record']['template'][:6])}")
    print(f"  版式结构线 {len(pm['layout_boundaries'])} 条"
          f"（其中落在标注区内 {(sum(1 for l in pm['layout_boundaries'] if l['in_gold_any']))}）")
    for l in pm["layout_boundaries"]:
        print(f"    rel={l['rel']:<8} support={l['support']:<6} kind={l['kind']:<6}"
              f" in_gold={l['in_gold_any']}  weak={l['weak']}")
    print(f"  分区 {len(pm['regions'])} 页")
    ba = pm.get("layout_boundaries_contract") or {}
    if ba.get("verdict") not in (None, "no_contract_bands", "unavailable"):
        print(f"  契约回迁带对位 {ba.get('verdict')}："
              f"{ba.get('n_matched')}/{ba.get('n_contract')} 条命中，"
              f"最大偏差 {ba.get('max_dev_px')} px"
              f"（plan 独有 {ba.get('n_plan_only')} / 契约独有 {ba.get('n_contract_only')}）")

    refs = plan.get("refs") or {}
    _pvref = refs.get("_prev_plan") or {}
    if _pvref.get("present"):
        _pvchg = _pvref.get("changed") or []
        print(f"  相对上一版 plan：{('输入变更 ' + '+'.join(_pvchg)) if _pvchg else '三份输入均未变'}"
              f"（上一版编译于 {_pvref.get('built_at')}）")
    else:
        print("  相对上一版 plan：无上一版可比（该列下次编译起才有值）")
    print(f"  属性 {len(plan['attributes'])}  降级项 {len(plan['degraded'])}")
    if plan["degraded"]:
        for d in plan["degraded"]:
            print(f"    ! {d['block']}: {d['reason']}")
    print(f"  说明书 {mp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
