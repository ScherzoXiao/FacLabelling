"""P5（2026-09-11）：版式漂移检测 + 页面挂起 + variant 逃生门。

定位见《半自动标注通用方案_20260910.md》§3 阶段④与 §7 **红线三**：
**契约不可静默过拟合**——契约一旦生效，后续页若版式不合就必须**可见地**被拦下，
而不是被默默套用同一份参数。本模块只做三件事：

1. **符合度打分**：逐页把「像素级版式度量」与契约 `G.page_stats` 的分布比对；
2. **挂起**：判定漂移的页写入 `data/drift/state.json`，下游（草稿批量采纳）
   据此拒绝，直到人处理；
3. **variant 逃生门**：人若认定「这页确实是一种合法的另一种版式」，
   可**显式登记**为一个 variant；此后与该 variant 特征相符的页不再报漂移。

## 为什么是"像素级"而非"语义级"

漂移要在**摄取时**就能判——此时还没有 VLM 的 `records`，也没有人工标注。
故打分只用**独立于模型**的像素度量（红线一的精神：证据必须与它要校验的对象独立）：
长宽比 / 墨迹比 / 列数 / 列间距 / 列宽 / 字符推进量。
6 项全部复用 `layout_contract` 的同一份实现（`page_stats`），本模块**不另写估计器**。

## 阈值怎么来的（**不是拍脑袋**）

- 期望值 = 契约源页该项的 **mean**；容差 = `K_SIGMA × sd_eff`；
- `sd_eff = max(sd_measured, sd_floor)`。**逐特征的 `sd_floor` 在契约构建时实测**：
  取同发布物**全部页**（outbox 内同前缀，无需标注）算各特征的相对离散度，取
  `1.5 × rel_sd`（下限 1%）。**为什么必须逐特征**：同一批内 `aspect` 的相对离散度
  只有 **0.0004**（同一台扫描仪、同一开本），而 `col_gap_px` 高达 **0.23**（首页带
  通栏标题会拉开列距）—— 差两个数量级。用统一下限必然两头不讨好：太小则跨页误报，
  太大则把"整页翻转/大幅拉伸"这类结构性变化也放过。
- **页朝向**另设**分类判据**（`orientation`，关键项）：`aspect` 是否 < 1 必须与契约
  一致。整页旋转 90° 只把 aspect 变成倒数（1.05 → 0.95），数值容差稍宽就会漏检；
  "横排 vs 竖排"是结构性差异，用分类判据更可靠、报告也更好读。
- `SD_FLOOR_REL`（15%）只是**旧版契约的兜底**（契约没有 `sd_floor` 时才用）。

## 三个"不判漂移"的诚实边界

1. **有效检查数 < `MIN_CHECKS`** → `verdict="unknown"`（**宁缺毋滥**：证据不足时
   不冒充结论，也不挂起页面）；
2. **adv_px 的倍频歧义**：自相关主峰可能是真周期的 1/2 或 2 倍（官报同批内实测
   同时出现 36 与 18），故 `adv` 与 `{v, v/2, v*2}` 任一落带即算通过；
3. **等比缩放不判漂移**（如整页 0.5× 缩放）：版面**结构**未变，各量在同一像素
   空间里自洽。这是有意为之，不是漏检——由 `_probe/verify_p5.py` 作为
   **阴性对照**显式报告，不得含糊带过。
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if getattr(sys, "frozen", False):                      # PyInstaller onedir：数据在 exe 同级
    _BASE = Path(sys.executable).parent.resolve()
else:
    _BASE = Path(__file__).parent.resolve()

import layout_contract as LC                            # noqa: E402
import data_io                                          # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_DRIFT_DIR = _BASE / "data" / "drift"
DEFAULT_STATE_NAME = "state.json"
DEFAULT_OUTBOX = _BASE / "outbox"
DEFAULT_STRUCTURED_DIR = _BASE / "data" / "structured"

STATE_SCHEMA = "1.0.0"

# ---- 打分策略常数（依据见模块 docstring；由 _probe/verify_p5.py 扫描定点）----
SD_FLOOR_REL = 0.15      # sd 下限（相对 |mean|）**兜底值**：契约未给逐特征下限时用
K_SIGMA = 3.0            # 容许带 = mean ± sd_eff × K_SIGMA
MIN_CHECKS = 3           # 有效检查数下限；不足 → unknown（不判漂移）
DRIFT_MIN_PASS = 0.75    # 通过率低于此 → 判漂移
CRITICAL: Tuple[str, ...] = ("orientation", "aspect", "adv_px")   # 单条失败即判漂移
ADV_ALIAS = (1.0, 0.5, 2.0)   # adv 的倍频歧义候选

# ---- 判定取值 ----
V_OK = "ok"                  # 符合契约
V_OK_VARIANT = "ok_variant"  # 命中已登记的 variant
V_OK_MANUAL = "ok_manual"    # 人显式放行（resume）
V_DRIFT = "drift"            # 漂移 → 挂起
V_UNKNOWN = "unknown"        # 证据不足（不挂起）

# 空白/近空白页：无可比对内容 → 直接判漂移（契约无从适用）
MIN_INK = 0.01


# ============================ 状态存储（外挂 + 原子写） ============================

def state_path(drift_dir=None) -> Path:
    return Path(drift_dir or DEFAULT_DRIFT_DIR) / DEFAULT_STATE_NAME


def load_state(drift_dir=None) -> Dict[str, Any]:
    p = state_path(drift_dir)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"_schema_version": STATE_SCHEMA, "profiles": {}}
    if not isinstance(d, dict):
        return {"_schema_version": STATE_SCHEMA, "profiles": {}}
    d.setdefault("_schema_version", STATE_SCHEMA)
    d.setdefault("profiles", {})
    return d


def save_state(state: Dict[str, Any], drift_dir=None) -> Path:
    p = state_path(drift_dir)
    data_io.atomic_write_json(p, state, indent=1)
    return p


def _profile_block(state: Dict[str, Any], profile_id: str) -> Dict[str, Any]:
    return state.setdefault("profiles", {}).setdefault(
        profile_id, {"verdicts": {}, "variants": {}})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================ 逐特征打分 ============================

def feature_band(contract: dict, name: str,
                 sd_floor_rel: float = SD_FLOOR_REL,
                 k_sigma: float = K_SIGMA) -> Optional[dict]:
    """契约给出的容许带 → `{mean, sd, sd_eff, lo, hi, n}`；契约缺该特征 → None。

    `sd_eff = max(sd_measured, sd_floor)`，`sd_floor` **优先取契约的逐特征实测值**
    （`G.page_stats.features[k].sd_floor`，由同发布物全部页的实测相对离散度推出）；
    契约未给（旧版契约）才退回 `sd_floor_rel` 兜底。
    """
    feats = (((contract or {}).get("G") or {}).get("page_stats") or {}).get("features") or {}
    f = feats.get(name)
    if not f or f.get("mean") is None:
        return None
    mean = float(f["mean"])
    sd = f.get("sd")
    floor_rel = f.get("sd_floor")
    floor = (abs(mean) * float(floor_rel)) if floor_rel is not None \
        else abs(mean) * sd_floor_rel
    sd_eff = max(float(sd), floor) if sd is not None else floor
    return {"mean": mean, "sd": sd, "sd_eff": sd_eff, "n": f.get("n"),
            "sd_floor": round(floor, 4),
            "lo": mean - sd_eff * k_sigma, "hi": mean + sd_eff * k_sigma}


def _in_band(v: float, lo: float, hi: float) -> bool:
    if lo > hi:
        lo, hi = hi, lo
    return lo <= v <= hi


def check_orientation(contract: dict, feats: dict,
                      sd_floor_rel: float = SD_FLOOR_REL,
                      k_sigma: float = K_SIGMA) -> Optional[dict]:
    """**页朝向**检查（派生项）：页面是"高>宽"还是"宽>高"必须与契约一致。

    为什么不靠 `aspect` 的容差兜：整页旋转 90° 后 aspect 变为倒数（1.05 → 0.95），
    若容差略宽就会漏检——而"横排 vs 竖排"是**结构性**差异，用分类判据比用数值
    容差可靠得多，报告里也更好读（"页面朝向与契约不一致"）。
    """
    band = feature_band(contract, "aspect", sd_floor_rel, k_sigma)
    if band is None or feats.get("aspect") is None:
        return None
    want_portrait = band["mean"] < 1.0
    got_portrait = float(feats["aspect"]) < 1.0
    return {"name": "orientation",
            "value": "portrait" if got_portrait else "landscape",
            "expected": "portrait" if want_portrait else "landscape",
            "ok": bool(want_portrait == got_portrait), "critical": True,
            "note": f"aspect={feats['aspect']}（契约均值 {band['mean']}）"}


def check_feature(contract: dict, name: str, value,
                  sd_floor_rel: float = SD_FLOOR_REL,
                  k_sigma: float = K_SIGMA) -> Optional[dict]:
    """单特征检查 → `{name, value, lo, hi, ok, critical, note}`；无基准/无实测 → None。"""
    band = feature_band(contract, name, sd_floor_rel, k_sigma)
    if band is None or value is None:
        return None
    v = float(value)
    if name == "adv_px":
        # 倍频歧义：任一候选落带即通过
        ok = any(_in_band(v * m, band["lo"], band["hi"]) for m in ADV_ALIAS)
        cands = [round(v * m, 1) for m in ADV_ALIAS]
        note = f"候选 {cands}（倍频歧义）"
    else:
        ok = _in_band(v, band["lo"], band["hi"])
        note = ""
    return {"name": name, "value": round(v, 4),
            "expected": round(band["mean"], 4),
            "lo": round(band["lo"], 4), "hi": round(band["hi"], 4),
            "sd_eff": round(band["sd_eff"], 4), "sd_floor": band["sd_floor"],
            "n_source": band["n"],
            "ok": bool(ok), "critical": name in CRITICAL, "note": note}


# ============================ 页特征 / 打分 ============================

def band_y_range(contract: dict, structured_dir=None, stem: str = "") -> Optional[Tuple[float, float]]:
    """打分用的 y 区间 = 契约的 `page_stats.band_y_range`（与源页同口径）。

    契约缺该字段时退回 `G.regions.annot_y_range`；都没有 → None（调用方用整页）。
    """
    ps = ((contract or {}).get("G") or {}).get("page_stats") or {}
    r = ps.get("band_y_range")
    if not r:
        r = (((contract or {}).get("G") or {}).get("regions") or {}).get("annot_y_range")
    if not r:
        return None
    return (float(r[0]), float(r[1]))


def page_features(contract: dict, stem: str, outbox_dir=None,
                  structured_dir=None) -> dict:
    """逐页像素级特征（含图像尺寸与结构化文档的可用性）。图像缺失 → `available=False`。"""
    stem = LC.stem_of(stem)
    img = Path(outbox_dir or DEFAULT_OUTBOX) / f"{stem}.png"
    doc = Path(structured_dir or DEFAULT_STRUCTURED_DIR) / f"{stem}.json"
    out: Dict[str, Any] = {"stem": stem, "available": False, "has_structured": doc.exists()}
    if not img.exists():
        out["reason"] = "无原图（outbox 缺该页）"
        return out
    gray = LC._page_gray(img)
    if gray is None:
        out["reason"] = "原图读取失败"
        return out
    band = band_y_range(contract, structured_dir, stem)
    h = int(gray.shape[0])
    ylo, yhi = (band if band else (0.0, float(h)))
    st = LC.page_stats(gray, min(ylo, h - 1), min(yhi, h))
    out.update(st)
    out["available"] = True
    out["band_used"] = [min(ylo, h - 1), min(yhi, h)]
    return out


def score_page(contract: dict, feats: dict,
               variants: Optional[Dict[str, dict]] = None,
               k_sigma: float = K_SIGMA,
               sd_floor_rel: float = SD_FLOOR_REL,
               min_pass: float = DRIFT_MIN_PASS,
               min_checks: int = MIN_CHECKS) -> dict:
    """单页符合度打分 → 判定 + 逐项证据 + 人话理由。

    - `verdict ∈ {ok, drift, unknown}`（variant/manual 的放行由调用方在
      `scan`/`verdict_for` 层叠加，保持本函数纯粹）；
    - 空白页：`ink_ratio < MIN_INK` → 直接 `drift`（契约无从适用，
      且这是**最容易漏**的一类——它没有任何特征会"超带"，只是全 None）。
    """
    checks: List[dict] = []
    reasons: List[str] = []
    if not feats.get("available"):
        return {"verdict": V_UNKNOWN, "conformance": None, "checks": [],
                "reasons": [feats.get("reason") or "无可用图像特征"],
                "n_checks": 0}

    ori = check_orientation(contract, feats, sd_floor_rel, k_sigma)
    if ori:
        checks.append(ori)
    for name in LC.DRIFT_FEATURES:
        c = check_feature(contract, name, feats.get(name), sd_floor_rel, k_sigma)
        if c:
            checks.append(c)

    if feats.get("ink_ratio") is not None and feats["ink_ratio"] < MIN_INK:
        return {"verdict": V_DRIFT, "conformance": 0.0, "checks": checks,
                "reasons": [f"页面近全白（墨迹比 {feats['ink_ratio']:.4f} < {MIN_INK}）"
                            "→ 契约无从适用"],
                "n_checks": len(checks)}

    if len(checks) < min_checks:
        return {"verdict": V_UNKNOWN, "conformance": None, "checks": checks,
                "reasons": [f"有效检查仅 {len(checks)} 项（< {min_checks}）→ 证据不足，"
                            "不判漂移"], "n_checks": len(checks)}

    passed = [c for c in checks if c["ok"]]
    conformance = len(passed) / len(checks)
    failed = [c for c in checks if not c["ok"]]
    for c in failed:
        if "lo" in c:
            reasons.append(f"{c['name']}={c['value']} 越界 [{c['lo']}, {c['hi']}]"
                           + ("（关键项）" if c["critical"] else ""))
        else:                                          # orientation 等分类判据项
            reasons.append(f"{c['name']}={c['value']} 应为 {c['expected']}"
                           + ("（关键项）" if c["critical"] else ""))
    crit_fail = [c for c in failed if c["critical"]]
    drift = bool(crit_fail) or conformance < min_pass
    if not drift:
        reasons = []
    return {"verdict": V_DRIFT if drift else V_OK,
            "conformance": round(conformance, 4),
            "checks": checks, "failed": [c["name"] for c in failed],
            "critical_failed": [c["name"] for c in crit_fail],
            "reasons": reasons, "n_checks": len(checks)}


def match_variant(feats: dict, variants: Dict[str, dict],
                  contract: dict, k_sigma: float = K_SIGMA) -> Optional[dict]:
    """页面是否命中某个已登记 variant（对 variant 自身的离散度做带内判定）。

    variant 记录的是**一页**的特征，故 sd 缺失 → 用契约的逐特征 `sd_floor` 兜底
    （仍比统一 15% 更贴近该特征的实测稳定性）。
    """
    if not feats.get("available"):
        return None
    bands = {n: feature_band(contract, n, k_sigma=k_sigma)
             for n in LC.DRIFT_FEATURES}
    for vid, var in (variants or {}).items():
        vf = (var or {}).get("features") or {}
        if not vf:
            continue
        ok_all, n = True, 0
        for name, val in vf.items():
            if feats.get(name) is None or val is None:
                continue
            mean = float(val)
            b = bands.get(name) or {}
            floor = b.get("sd_floor")
            sd_eff = float(floor) if floor else abs(mean) * SD_FLOOR_REL
            v = float(feats[name])
            if name == "adv_px":
                ok = any(_in_band(v * m, mean - sd_eff * k_sigma, mean + sd_eff * k_sigma)
                         for m in ADV_ALIAS)
            else:
                ok = _in_band(v, mean - sd_eff * k_sigma, mean + sd_eff * k_sigma)
            n += 1
            if not ok:
                ok_all = False
                break
        # 朝向也必须一致（variant 的 aspect 同样隐含朝向）
        if ok_all and vf.get("aspect") is not None and feats.get("aspect") is not None:
            ok_all = (float(vf["aspect"]) < 1.0) == (float(feats["aspect"]) < 1.0)
        if ok_all and n >= 2:
            return {"variant_id": vid, "name": var.get("name") or vid, "n_checks": n}
    return None


# ============================ 扫描 / 判定入口 ============================

def _profile_contract(profile_id: str, contracts_dir=None) -> Optional[dict]:
    return LC.load_contract(profile_id, contracts_dir)


def _compute_verdict(contract: dict, stem: str, variants: Optional[Dict[str, dict]],
                     outbox_dir=None, structured_dir=None,
                     k_sigma: float = K_SIGMA) -> dict:
    """打分核心（**显式接收 variants**，不读磁盘）——便于"刚登记 variant 就重算"。"""
    feats = page_features(contract, stem, outbox_dir, structured_dir)
    res = score_page(contract, feats, k_sigma=k_sigma)
    mv = match_variant(feats, variants or {}, contract, k_sigma)
    if res["verdict"] == V_DRIFT and mv:
        res = {**res, "verdict": V_OK_VARIANT, "matched_variant": mv["variant_id"],
               "variant_name": mv["name"], "reasons": []}
    return {"verdict": res["verdict"], "conformance": res.get("conformance"),
            "failed": res.get("failed") or [], "reasons": res.get("reasons") or [],
            "checks": res.get("checks") or [], "n_checks": res.get("n_checks"),
            "suspended": res["verdict"] == V_DRIFT,
            "matched_variant": res.get("matched_variant"),
            "band_used": feats.get("band_used"), "ts": _now()}


def verdict_for(profile_id: str, stem: str, contract: Optional[dict] = None,
                outbox_dir=None, structured_dir=None, drift_dir=None,
                contracts_dir=None, use_cache: bool = True,
                k_sigma: float = K_SIGMA) -> dict:
    """取（或算）单页判定。**带缓存**：命中缓存不重算图像。

    返回 `{verdict, conformance, reasons, suspended, matched_variant, ...}`。
    契约不存在 → `unknown`（不挂起）。
    """
    stem = LC.stem_of(stem)
    state = load_state(drift_dir)
    block = _profile_block(state, profile_id)
    if use_cache and stem in block["verdicts"]:
        return dict(block["verdicts"][stem], cached=True)
    contract = contract or _profile_contract(profile_id, contracts_dir)
    if not contract or not contract.get("G"):
        return {"verdict": V_UNKNOWN, "conformance": None, "suspended": False,
                "reasons": [f"无契约（profile_id={profile_id}）→ 不判漂移"], "cached": False}
    out = {**_compute_verdict(contract, stem, block.get("variants"), outbox_dir,
                              structured_dir, k_sigma), "cached": False}
    return out


def scan(profile_id: str, stems: Optional[List[str]] = None,
         outbox_dir=None, structured_dir=None, drift_dir=None, contracts_dir=None,
         contract: Optional[dict] = None, k_sigma: float = K_SIGMA,
         persist: bool = True) -> dict:
    """批扫描并落盘判定。`stems` 缺省 = `discover_pages`（同发布物全部页）。

    缺省集合口径：契约 `source.pages` 里各页的**发布物前缀**（`序号_刊名_期卷`），
    这样"同一份报刊的后续页"能自动纳入，而不需要人工列页。
    """
    contract = contract or _profile_contract(profile_id, contracts_dir)
    if not contract or not contract.get("G"):
        return {"ok": False, "error": f"无契约: {profile_id}"}
    if stems is None:
        stems = discover_pages(contract, outbox_dir)
    state = load_state(drift_dir)
    block = _profile_block(state, profile_id)
    verdicts: Dict[str, dict] = {}
    n_drift = 0
    for st in stems:
        v = verdict_for(profile_id, st, contract=contract, outbox_dir=outbox_dir,
                        structured_dir=structured_dir, drift_dir=drift_dir,
                        use_cache=False, k_sigma=k_sigma)
        verdicts[LC.stem_of(st)] = v
        if v["verdict"] == V_DRIFT:
            n_drift += 1
    block["verdicts"] = verdicts
    block["scanned_at"] = _now()
    block["n_scanned"] = len(verdicts)
    if persist:
        save_state(state, drift_dir)
    return {"ok": True, "profile_id": profile_id, "n_scanned": len(verdicts),
            "n_drift": n_drift, "n_suspended": len(suspended_stems(profile_id, drift_dir)),
            "verdicts": verdicts, "state_path": str(state_path(drift_dir))}


def discover_pages(contract: dict, outbox_dir=None) -> List[str]:
    """由契约源页推出「同发布物」的页集合（用于批扫描的缺省口径）。

    发布物前缀 = 源页 stem 去掉尾部 `_NNNN` 页序；源页本身无页序时（裁切/单页
    样本）以整个 stem 为前缀。多个前缀取并集。
    """
    outbox = Path(outbox_dir or DEFAULT_OUTBOX)
    prefixes = set()
    for s in ((contract.get("source") or {}).get("pages") or []):
        parts = str(s).rsplit("_", 1)
        prefixes.add(parts[0] if len(parts) == 2 and parts[1].isdigit() else str(s))
    if not prefixes:
        return []
    out = []
    for p in sorted(outbox.glob("*.png")):
        for pre in prefixes:
            if p.stem == pre or p.stem.startswith(pre + "_"):
                out.append(p.stem)
                break
    return out


def suspended_stems(profile_id: str, drift_dir=None) -> List[str]:
    """当前被挂起的页（verdict=drift 且未被人放行）。"""
    block = _profile_block(load_state(drift_dir), profile_id)
    return sorted(s for s, v in (block.get("verdicts") or {}).items()
                  if v.get("verdict") == V_DRIFT)


# ============================ variant 逃生门 / 人工放行 ============================

def register_variant(profile_id: str, name: str, note: str = "",
                     stems: Optional[List[str]] = None, features: Optional[dict] = None,
                     contract: Optional[dict] = None, outbox_dir=None,
                     structured_dir=None, drift_dir=None, contracts_dir=None) -> dict:
    """**人显式**把一个（或一组）被挂起页登记为合法 variant → 之后同类页不再报警。

    红线二的精神：放行只能由人的判断触发，故本函数只由显式 API 调用，
    不在任何自动路径上被触发。登记后立即对已有判定**重算**（原挂起页转 `ok_variant`）。
    """
    state = load_state(drift_dir)
    block = _profile_block(state, profile_id)
    contract = contract or _profile_contract(profile_id, contracts_dir)
    if not contract or not contract.get("G"):
        return {"ok": False, "error": f"无契约: {profile_id}"}
    if features is None:
        if not stems:
            return {"ok": False, "error": "需给 stems 或 features 之一"}
        f = page_features(contract, stems[0], outbox_dir, structured_dir)
        if not f.get("available"):
            return {"ok": False, "error": f"取特征失败: {f.get('reason')}"}
        features = {k: f[k] for k in LC.DRIFT_FEATURES if f.get(k) is not None}
    vid = f"var_{len(block.get('variants') or {}) + 1:02d}"
    block.setdefault("variants", {})[vid] = {
        "name": name or vid, "note": note, "ts": _now(),
        "features": features, "from_stems": [LC.stem_of(s) for s in (stems or [])]}
    # 重算受影响页的判定（用**内存中**已更新 variants，不读回磁盘）
    touched = []
    for st in (stems or []):
        st = LC.stem_of(st)
        v = _compute_verdict(contract, st, block["variants"], outbox_dir, structured_dir)
        block["verdicts"][st] = v
        touched.append({"stem": st, "verdict": v["verdict"]})
    save_state(state, drift_dir)
    return {"ok": True, "variant_id": vid, "features": features, "touched": touched,
            "n_variants": len(block["variants"])}


def resume(profile_id: str, stem: str, note: str = "", drift_dir=None) -> dict:
    """人显式放行单页（不建立 variant）→ 判定改为 `ok_manual`，解除挂起。"""
    state = load_state(drift_dir)
    block = _profile_block(state, profile_id)
    stem = LC.stem_of(stem)
    v = dict(block["verdicts"].get(stem) or {})
    if not v:
        return {"ok": False, "error": f"该页无判定记录: {stem}"}
    v.update({"verdict": V_OK_MANUAL, "suspended": False, "reasons": [],
              "note": note, "ts": _now()})
    block["verdicts"][stem] = v
    save_state(state, drift_dir)
    return {"ok": True, "stem": stem, "verdict": v["verdict"]}


def clear(profile_id: str, drift_dir=None) -> dict:
    """清空该档案的判定与 variant（重新扫描前用；**不删文件**，只清状态）。"""
    state = load_state(drift_dir)
    state.setdefault("profiles", {})[profile_id] = {"verdicts": {}, "variants": {}}
    save_state(state, drift_dir)
    return {"ok": True, "profile_id": profile_id}
