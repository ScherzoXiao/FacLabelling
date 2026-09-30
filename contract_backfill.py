# -*- coding: utf-8 -*-
"""契约回迁通道：**plan → 契约**（P-C 补丁）。

## 这条路补的是什么

三层契约的裁定是：

  裁定 2 —— 带结构属「版面约定类」信息 → **契约是权威**；
  裁定 3 —— 先让 plan 承接（层 2 交付物），契约滞后。

两条并存时有个后果：**plan 里新长出来的版面常数，契约里查不到**。
于是「契约 = 版面约定的权威」这句话，在 plan 已经产出了稳定版面参数、
而契约还没有的时候——**不成立**。本模块就是补这条路。

实测样本（样例 3 页主族）：plan 里 `page_model.layout_clusters[0]`
有 3 条 `kind="rule"` 的结构线，`rel` = 0.1625 / 0.323 / 0.8848，
跨 3 页 `support` = 1.0、`rel_spread` ≤ 0.0061。契约 `G` 里没有任何对应字段。

## 三条纪律（沿用本项目既有的）

  ① **只增不改**：只写 `G.layout_bands`（**新增**键），不碰 `G.regions`。
     两者语义不同，别合并——
       · `G.regions.annot_y_range` = 「标注区在页面哪一段」（金标准直接归纳，绝对像素）
       · `G.layout_bands`          = 「版式结构线在哪、跨页多稳」（plan 回迁，相对页高）
     合并会把「观测到的界线」和「归纳出的区间」混成一锅，将来谁也说不清
     哪来的。**宁可两个键并存。**
  ② **人类确认**：默认 dry-run，必须 `--apply` / `--rebuild` 才落盘。
  ③ **不静默丢失**：契约重建（`layout_contract.build_contract`）会把
     `G.layout_bands` 一起丢掉——它是从标注归纳出来的，归纳不出回迁内容。
     故 `layout_contract.save_contract` 对纳入 `ADDITIVE_G_KEYS` 的键**默认保留**，
     撤销走本模块的 `--revoke`（显式）。见 `layout_contract.preserve_additive`。

## 门槛不是拍的

来源是 plan 自己算好的稳定度字段（`layout_boundaries_meta` + 每条的
`support` / `n_pages_hit` / `rel_spread` / `weak`），本模块**只做门槛判定，
不重新测量**——plan 是层 2 的唯一接口物，回迁不该绕过它去翻原图。

  默认门槛：`kind == "rule"`、`n_pages_hit ≥ 2`（多页验证）、
  `support ≥ 0.8`、`rel_spread ≤ 0.01`。被拒的**也全部列出来**（带理由），
  不做无声筛选。

## 回迁之后谁来读

`plan_build` 编译下一版 plan 时，会把新归纳的结构线**与契约回迁带对位**
（`page_model.layout_boundaries_contract`），给每条线标上
`in_contract` / `dev_px`。这是「契约当权威」的最小可落地形态：
**先观测偏差，不急着反向约束**——同一期实测偏差为 0 才敢让契约覆盖 plan。

用法：
    python contract_backfill.py                       # dry-run，只看会加什么
    python contract_backfill.py --apply               # 落盘（只增不改）
    python contract_backfill.py --rebuild             # 重建契约并保留回迁带
    python contract_backfill.py --revoke              # 显式撤销回迁带
    python contract_backfill.py --json                # 机器可读（验收脚本用）
"""

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import data_io

log = logging.getLogger("contract_backfill")

ROOT = Path(__file__).resolve().parent
DEFAULT_PLANS_DIR = ROOT / "data" / "plans"
DEFAULT_CONTRACTS_DIR = ROOT / "data" / "layout_contracts"
DEFAULT_PROFILE_ID = ""   # 由调用方显式传入（如 --profile-id）；不内置任何档案 id

#: `G` 层里**由回迁写入**的键（相对页高语义）。`save_contract` 按 `ADDITIVE_G_KEYS`
#: 保留——这些键不是从标注归纳来的，重建必然丢。
BANDS_KEY = "layout_bands"
BANDS_SCHEMA = "layout_bands/1"

#: 默认门槛
MIN_PAGES = 2            # 多页验证：只在 1 页出现的线不迁（可能是个体差异）
MIN_SUPPORT = 0.8        # 命中页占样本页的比例
MAX_SPREAD = 0.01        # 跨页 rel 极差（约 18px @1800px 页高）
STRUCTURAL_KINDS = ("rule",)   # 只迁**印刷在版面上**的横线；空白带随内容长度浮动

#: 已存在判定容差（rel 差 ≤ 此值算同一条线）
MATCH_TOL = 0.005


# ============================================================ 读入

def load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_of(path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def pick_plan(plans_dir=None, profile_id: str = DEFAULT_PROFILE_ID) -> Optional[Path]:
    """`data/plans/plan_<pid>.json`；不存在 → 该目录里最新的 plan_*.json。"""
    d = Path(plans_dir or DEFAULT_PLANS_DIR)
    p = d / f"plan_{profile_id}.json"
    if p.exists():
        return p
    cand = sorted(d.glob("plan_*.json"), key=lambda x: x.stat().st_mtime)
    return cand[-1] if cand else None


def contract_path(contracts_dir=None, profile_id: str = DEFAULT_PROFILE_ID) -> Path:
    d = Path(contracts_dir or DEFAULT_CONTRACTS_DIR)
    return d / f"layout_{profile_id}.json"


# ============================================================ 候选提取

def clusters_of(plan: dict) -> List[dict]:
    return ((plan or {}).get("page_model") or {}).get("layout_clusters") or []


def patterns_of(plan: dict) -> List[dict]:
    """全部版式族（`layout_clusters`），无该字段 → 由 `layout_boundaries` 兜底合成一族。"""
    cs = clusters_of(plan)
    if cs:
        return cs
    pm = (plan or {}).get("page_model") or {}
    lines = pm.get("layout_boundaries") or []
    if not lines:
        return []
    meta = pm.get("layout_boundaries_meta") or {}
    return [{"id": 0, "is_primary": True, "pages": [],
             "n_pages": meta.get("n_pages"), "ref_page_h": meta.get("ref_page_h"),
             "layout_boundaries": lines}]


def candidates(plan: dict,
               *,
               min_pages: int = MIN_PAGES,
               min_support: float = MIN_SUPPORT,
               max_spread: float = MAX_SPREAD,
               kinds: Tuple[str, ...] = STRUCTURAL_KINDS,
               pattern: str = "primary") -> Tuple[List[dict], List[dict], dict]:
    """从 plan 提取回迁候选。

    `pattern`：`"primary"`（默认，只取主族）/ `"all"`（全部版式族）/ 整数族 id。

    返回 `(accepted, rejected, meta)`；每条都带 `verdict` / `reason`，
    **被拒的不丢**——静默筛选会让"为什么没迁"变成不可追的问题。
    """
    pats = patterns_of(plan)
    chosen: List[dict] = []
    if pattern == "all":
        chosen = pats
    elif pattern == "primary":
        chosen = [c for c in pats if c.get("is_primary")] or pats[:1]
    else:
        chosen = [c for c in pats if int(c.get("id", -1)) == int(pattern)]

    accepted: List[dict] = []
    rejected: List[dict] = []
    for c in chosen:
        n_pages = c.get("n_pages") or len(c.get("pages") or []) or 0
        for b in c.get("layout_boundaries") or []:
            it = dict(b)
            it["pattern_id"] = c.get("id")
            it["pattern_pages"] = list(c.get("pages") or [])
            it["pattern_n_pages"] = n_pages
            it["pattern_ref_page_h"] = c.get("ref_page_h")
            it["verdict"], it["reason"] = _verdict(it, min_pages, min_support,
                                                   max_spread, kinds)
            (accepted if it["verdict"] == "accept" else rejected).append(it)
    meta = {
        "pattern": pattern,
        "patterns_considered": [c.get("id") for c in chosen],
        "thresholds": {"min_pages": min_pages, "min_support": min_support,
                       "max_spread": max_spread, "kinds": list(kinds)},
        "n_accepted": len(accepted),
        "n_rejected": len(rejected),
        "plan_built_at": (plan or {}).get("built_at"),
        "plan_profile_id": (plan or {}).get("profile_id"),
    }
    accepted.sort(key=lambda x: x["rel"])
    rejected.sort(key=lambda x: x["rel"])
    return accepted, rejected, meta


def _verdict(b: dict, min_pages: int, min_support: float,
             max_spread: float, kinds: Tuple[str, ...]) -> Tuple[str, str]:
    """单条结构线的回迁判定。返回 `(verdict, reason)`。"""
    kind = str(b.get("kind"))
    if kinds and kind not in kinds:
        return "reject", f"kind_not_structural（{kind} 不在 {list(kinds)}）"
    hit = int(b.get("n_pages_hit") or 0)
    if hit < min_pages:
        return "reject", f"too_few_pages（{hit} < {min_pages}）"
    sup = b.get("support")
    if sup is None or float(sup) < min_support:
        return "reject", f"low_support（{sup} < {min_support}）"
    sp = b.get("rel_spread")
    if sp is None or float(sp) > max_spread:
        return "reject", f"unstable_spread（{sp} > {max_spread}）"
    weak = int(b.get("weak") or 0)
    if weak >= hit:
        # 全部命中都是「短横线」——多半是文字行误检，不是版式线
        return "reject", f"weak_evidence（{weak}/{hit} 命中为短横线）"
    note = "accept"
    if weak:
        note = f"accept（含 {weak} 条短横线，未占比）"
    return "accept", note


# ============================================================ 落点读写

def bands_of(contract: dict, page_h: Optional[float] = None) -> List[dict]:
    """读回迁带（给下游用的唯一读取口）。

    `page_h` 给定则附带 `px` —— 该页上的绝对像素位置。
    契约没有回迁带 → `[]`（**不是 None**，下游无需判空分支）。
    """
    blk = ((contract or {}).get("G") or {}).get(BANDS_KEY) or {}
    out: List[dict] = []
    for b in blk.get("bands") or []:
        it = dict(b)
        if page_h:
            it["px"] = round(float(b.get("rel", 0)) * float(page_h), 1)
        out.append(it)
    return out


def bands_meta(contract: dict) -> dict:
    return dict((((contract or {}).get("G") or {}).get(BANDS_KEY) or {}))


def _find(bands: List[dict], rel: float, tol: float = MATCH_TOL) -> Optional[dict]:
    best, bd = None, tol
    for b in bands:
        d = abs(float(b.get("rel", 0)) - float(rel))
        if d <= bd:
            best, bd = b, d
    return best


def plan_changes(contract: dict, accepted: List[dict],
                 tol: float = MATCH_TOL) -> Dict[str, List[dict]]:
    """dry-run 的差分：`add` / `present` / `stale`。

    `stale` = 契约里有、本版 plan 没验出来的带 —— **不自动删**，
    只报出来（可能是新周期样本不足，也可能是真漂移；两者处理方式不同，
    机器不替人决定）。
    """
    cur = bands_of(contract)
    add, present = [], []
    for a in accepted:
        m = _find(cur, a["rel"], tol)
        if m is None:
            add.append(a)
        else:
            present.append({"plan": a, "contract": m})
    stale = [c for c in cur if _find(accepted, c["rel"], tol) is None]
    return {"add": add, "present": present, "stale": stale}


def merge_bands(contract: dict, accepted: List[dict], *,
                plan_ref: Optional[dict] = None,
                ref_page_h: Optional[float] = None,
                ts: Optional[str] = None,
                tol: float = MATCH_TOL) -> Tuple[dict, dict]:
    """把 `accepted` 并进 `contract["G"]["layout_bands"]`（**不改原对象之外的键**）。

    已存在的带保留其原始 `backfilled_at` 与 `first_seen_plan`（**只增不改**：
    再次 `--apply` 是幂等的，不会把历史证据刷掉）。
    """
    ts = ts or datetime.now().isoformat(timespec="seconds")
    new = json.loads(json.dumps(contract, ensure_ascii=False))     # 深拷贝，防就地污染
    g = new.get("G")
    if not isinstance(g, dict):
        # 契约 G 层缺/为空（无标注样本）——新增键即可，不假装归纳出了别的东西
        g = {}
        new["G"] = g
    blk = g.get(BANDS_KEY)
    if not isinstance(blk, dict):
        blk = {}
        g[BANDS_KEY] = blk
    old = list(blk.get("bands") or [])

    kept: List[dict] = []
    added: List[dict] = []
    for a in accepted:
        m = _find(old, a["rel"], tol)
        if m is not None:
            kept.append(m)                       # 原样保留，连 backfilled_at 一起
            continue
        row = {
            "rel": round(float(a["rel"]), 4),
            "rel_spread": a.get("rel_spread"),
            "px_at_ref_page_h": a.get("px_at_ref_page_h"),
            "kind": a.get("kind"),
            "support": a.get("support"),
            "n_pages_hit": a.get("n_pages_hit"),
            "pages": list(a.get("pages") or []),
            "evidence": {
                "source": "plan.page_model.layout_clusters",
                "pattern_id": a.get("pattern_id"),
                "pattern_n_pages": a.get("pattern_n_pages"),
                "pattern_ref_page_h": a.get("pattern_ref_page_h"),
                "in_gold_any": a.get("in_gold_any"),
                "weak": a.get("weak"),
                "verdict_note": a.get("reason"),
            },
            "first_seen_plan": (plan_ref or {}).get("built_at"),
            "backfilled_at": ts,
        }
        kept.append(row)
        added.append(row)

    kept.sort(key=lambda x: float(x.get("rel", 0)))
    # 「多版确认」的证据链：下一次 `--apply`（换了一版 plan）追加一条。
    # 这条链是**将来**让契约反向约束 plan 的唯一依据（同一版重复 apply 不算确认）。
    confs = list(blk.get("confirmations") or [])
    ref_built = (plan_ref or {}).get("built_at")
    if ref_built and (not confs or confs[-1].get("plan_built_at") != ref_built):
        confs.append({"plan_built_at": ref_built, "at": ts,
                      "n_bands": len(kept)})
        confs = confs[-20:]
    blk.update({
        "_schema": BANDS_SCHEMA,
        "ref_page_h": ref_page_h if ref_page_h is not None else blk.get("ref_page_h"),
        "semantics": "相对页高的版式结构线（印刷横线）；绝对像素 = rel × 本页高",
        "authority": "plan（层 2）回迁；契约 G.regions 仍是标注区权威，两者语义不同",
        "rule": "只增不改：撤销用 `python contract_backfill.py --revoke`",
        "from_plan": blk.get("from_plan") or plan_ref,
        "confirmations": confs,
        "backfilled_at": blk.get("backfilled_at") or ts,
        "updated_at": ts,
        "bands": kept,
    })
    rep = {"added": added,
           "kept": [b for b in kept if not any(b is a for a in added)],
           "n_bands": len(kept),
           "n_confirmations": len(confs),
           "backfilled_at": blk["backfilled_at"],
           "ref_page_h": blk.get("ref_page_h")}
    return new, rep


def revoke_bands(contract: dict, rel: Optional[float] = None,
                 tol: float = MATCH_TOL, ts: Optional[str] = None) -> Tuple[dict, dict]:
    """撤销回迁带。`rel` 为空 → 整块移除。**显式动作**，不是 `--apply` 的反面。"""
    ts = ts or datetime.now().isoformat(timespec="seconds")
    new = json.loads(json.dumps(contract, ensure_ascii=False))
    g = new.get("G") or {}
    blk = g.get(BANDS_KEY) or {}
    if not blk:
        return new, {"removed": [], "n_bands": 0, "note": "契约本无回迁带"}
    if rel is None:
        g.pop(BANDS_KEY, None)
        return new, {"removed": [b.get("rel") for b in (blk.get("bands") or [])],
                     "n_bands": 0, "note": "整块移除"}
    hit = _find(blk.get("bands") or [], float(rel), tol)
    if hit is None:
        return new, {"removed": [], "n_bands": len(blk.get("bands") or []),
                     "note": f"未找到 rel≈{rel}（容差 {tol}）"}
    blk["bands"] = [b for b in blk["bands"] if b is not hit]
    blk["updated_at"] = ts
    return new, {"removed": [hit.get("rel")], "n_bands": len(blk["bands"]), "note": "已移除"}


def write_contract(path, contract: dict) -> Path:
    """原子写契约（M7 纪律）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return data_io.atomic_write_json(p, contract)


# ============================================================ 编排

def plan_ref_of(plan_path, plan: dict) -> dict:
    return {"path": str(Path(plan_path).name) if plan_path else None,
            "built_at": (plan or {}).get("built_at"),
            "profile_id": (plan or {}).get("profile_id"),
            "sha256": sha256_of(plan_path) if plan_path else None}


def build_report(plan_path, contract_path_, *, pattern: str = "primary",
                 min_pages: int = MIN_PAGES, min_support: float = MIN_SUPPORT,
                 max_spread: float = MAX_SPREAD,
                 kinds: Tuple[str, ...] = STRUCTURAL_KINDS,
                 mode: str = "dry-run",
                 apply: bool = False,
                 revoke_rel: Optional[float] = None,
                 rebuild: bool = False,
                 contracts_dir=None,
                 profile: Optional[dict] = None) -> dict:
    """一条龙：读 plan + 契约 → 提候选 → 差分/合并 → （`apply`）落盘。

    `contract_path_` 为 None 且 `rebuild=True` → 走 `layout_contract.build_contract`
    重建（重建会自动保留既有回迁带，见 `preserve_additive`）。
    """
    plan = load_json(plan_path) if plan_path and Path(plan_path).exists() else {}
    cpath = Path(contract_path_) if contract_path_ else None
    rebuilt_from = None

    if rebuild and cpath is not None:
        import layout_contract as LC
        old = load_json(cpath) if cpath.exists() else {}
        pid = plan.get("profile_id") or (old.get("profile_id")) or DEFAULT_PROFILE_ID
        fresh = LC.induce_contract(pid)
        fresh, preserved = LC.preserve_additive(fresh, old)
        contract = fresh
        rebuilt_from = {"preserved": preserved}
    else:
        contract = load_json(cpath) if (cpath and cpath.exists()) else {}

    if not contract:
        return {"mode": mode, "ok": False,
                "error": f"契约不存在或为空: {cpath}（试 --rebuild）"}

    pid_plan = plan.get("profile_id")
    pid_c = contract.get("profile_id")
    if pid_plan and pid_c and pid_plan != pid_c:
        return {"mode": mode, "ok": False,
                "error": f"plan/契约 profile_id 不一致：{pid_plan} ≠ {pid_c}"}

    accepted, rejected, cmeta = candidates(
        plan, min_pages=min_pages, min_support=min_support,
        max_spread=max_spread, kinds=kinds, pattern=pattern)
    ref_h = ((plan.get("page_model") or {}).get("layout_boundaries_meta") or {}
             ).get("ref_page_h") or (accepted[0].get("pattern_ref_page_h")
                                     if accepted else None)
    diff = plan_changes(contract, accepted)
    out = {"mode": mode, "ok": True,
           "plan": plan_ref_of(plan_path, plan),
           "contract": {"path": str(cpath) if cpath else None,
                        "profile_id": pid_c,
                        "path_exists": bool(cpath and cpath.exists())},
           "candidates": cmeta,
           "ref_page_h": ref_h,
           "accepted": accepted,
           "rejected": rejected,
           "diff": {"add": diff["add"], "present": diff["present"],
                    "stale": diff["stale"]},
           "rebuilt_from": rebuilt_from,
           "written": None}

    if revoke_rel is not None or mode == "revoke":
        new, rep = revoke_bands(contract, revoke_rel)
        out["revoke"] = rep
        if apply:
            out["written"] = str(write_contract(cpath, new))
        return out

    new, rep = merge_bands(contract, accepted,
                           plan_ref=out["plan"], ref_page_h=ref_h)
    out["merge"] = rep
    if apply:
        out["written"] = str(write_contract(cpath, new))
    return out


# ============================================================ 打印

def format_report(rep: dict) -> str:
    L: List[str] = []
    w = L.append
    if not rep.get("ok"):
        return f"❌ {rep.get('error')}"
    L.append(f"== 契约回迁 [{rep['mode']}] ==")
    pl = rep.get("plan") or {}
    w(f"plan      : {pl.get('path')}  built_at={pl.get('built_at')}")
    w(f"契约      : {rep['contract'].get('path')}")
    th = (rep.get("candidates") or {}).get("thresholds") or {}
    w(f"门槛      : kind∈{th.get('kinds')}  n_pages_hit≥{th.get('min_pages')}  "
      f"support≥{th.get('min_support')}  rel_spread≤{th.get('max_spread')}")
    if rep.get("rebuilt_from"):
        pv = rep["rebuilt_from"].get("preserved") or {}
        w(f"重建      : 保留既有回迁带 {pv.get('kept')} 条")
    w("")
    w(f"-- 候选：接受 {len(rep.get('accepted') or [])} / "
      f"拒绝 {len(rep.get('rejected') or [])} --")
    for b in rep.get("accepted") or []:
        w(f"  ✔ rel={b['rel']:<8} spread={b.get('rel_spread')}  "
          f"support={b.get('support')}  hit={b.get('n_pages_hit')}  "
          f"kind={b.get('kind')}  [{b.get('reason')}]")
    for b in rep.get("rejected") or []:
        w(f"  ✘ rel={b['rel']:<8} ← {b.get('reason')}")
    d = rep.get("diff") or {}
    w("")
    w(f"-- 差分：新增 {len(d.get('add') or [])} / 已有 {len(d.get('present') or [])} "
      f"/ 契约有而 plan 无（stale）{len(d.get('stale') or [])} --")
    for b in d.get("add") or []:
        w(f"  + rel={b['rel']}")
    for h in d.get("present") or []:
        w(f"  = rel={h['contract'].get('rel')}（已在契约）")
    for b in d.get("stale") or []:
        w(f"  ? rel={b.get('rel')} ← 契约有、本版 plan 未复现（**不自动删**，人工判断）")
    if rep.get("revoke"):
        w("")
        w(f"-- 撤销：{rep['revoke']} --")
    w("")
    if rep.get("written"):
        w(f"✅ 已原子写：{rep['written']}")
    else:
        w("（dry-run：未落盘。加 --apply 落盘）")
    return "\n".join(L)


def _main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="契约回迁通道（plan → 契约 G.layout_bands）")
    ap.add_argument("--plan", default=None, help="plan 路径；缺省取 data/plans 最新")
    ap.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    ap.add_argument("--plans-dir", default=None)
    ap.add_argument("--contracts-dir", default=None)
    ap.add_argument("--pattern", default="primary",
                    help="primary（默认）/ all / 族 id")
    ap.add_argument("--min-pages", type=int, default=MIN_PAGES)
    ap.add_argument("--min-support", type=float, default=MIN_SUPPORT)
    ap.add_argument("--max-spread", type=float, default=MAX_SPREAD)
    ap.add_argument("--kinds", default=",".join(STRUCTURAL_KINDS),
                    help="纳入的结构线 kind（逗号分隔；空串 = 不限）")
    ap.add_argument("--apply", action="store_true", help="落盘（默认 dry-run）")
    ap.add_argument("--rebuild", action="store_true",
                    help="先按金标准重建契约，再回迁（重建会保留既有回迁带）")
    ap.add_argument("--revoke", nargs="?", const="", default=None,
                    help="撤销回迁带：不带值 = 整块移除；带 rel = 只移除那条")
    ap.add_argument("--json", action="store_true", help="输出机器可读报告")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    plan_path = Path(a.plan) if a.plan else pick_plan(a.plans_dir, a.profile_id)
    cpath = contract_path(a.contracts_dir, a.profile_id)
    kinds = tuple(k.strip() for k in (a.kinds or "").split(",") if k.strip())
    revoke_rel = None
    if a.revoke is not None and a.revoke != "":
        revoke_rel = float(a.revoke)

    rep = build_report(
        plan_path, cpath,
        pattern=a.pattern, min_pages=a.min_pages, min_support=a.min_support,
        max_spread=a.max_spread, kinds=kinds,
        mode=("revoke" if a.revoke is not None else
              ("rebuild" if a.rebuild else "dry-run")),
        apply=bool(a.apply), revoke_rel=revoke_rel, rebuild=bool(a.rebuild),
        contracts_dir=a.contracts_dir,
    )
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(format_report(rep))
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
