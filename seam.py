# -*- coding: utf-8 -*-
"""层 3 接缝：**结果契约 + 结果校验器**（P-C）。

## 这个模块回答什么

层 2 产出 plan（唯一接口物）；层 3 把它落到具体页上。接缝的规矩是：
**系统不干预过程，只校验结果**。所以这里定义两样东西——

  ① `annotation_result` 的规范格式（schema v0.1）——任何执行器（含用户的 agent）
     都按它交活；
  ② `validate()` —— 只做**合规判定**，不做事后修补。

## 校验判据的依据（不是拍的，是实测推出来的）

离线探针在真实页上量过：**属性值对文本流的覆盖度中位仅 0.699、
最低 0.383**。原因是属性值天然不含锚词（锚词是定位标签，本身不是值）。
所以 plan 原先那句「产出必须覆盖输入文本的每一个字符」是**做不到的指令**（已改）。

于是守恒判据改成 **「绝不静默丢字」**：

  硬错（必须为 0）
    · 凭空生成 —— 值的文本在输入流里根本找不到（幻觉）
    · 引用越界 —— 声明的来源行下标超出范围
    · 重复占用 —— 同一行被两条不同记录引用；两条记录的行区间相交
    · 静默丢字 —— 有未覆盖的流区间，而 `unassigned_text` 没有声明它
    · 输入流被篡改 —— 自报 `input.text_stream` ≠ 真实行流（截断以掩盖丢字）
    · 框非法 / 越出页面
    · 记录**整体**落进无标注区（报头 / 页脚）
    · 降级档形态不符（`single_line` 必须恰好 1 条且覆盖 ≥0.95）

  度量 / 观测（进 QA，不是对错）
    · 覆盖度 —— 值覆盖了流的多少（中位 0.699 是当前基线，不是门槛）
    · 大缺口 —— 未覆盖段 ≥ `LARGE_GAP_CHARS` 字（实测 p90 = 20），进待核的信号
    · 模板序 ≠ 流序 —— 多人名族属性天然如此，只观测不判错
    · 字形被转换 —— 值只在折叠流里匹配得到，违反"保原字形"
    · 记录跨版式带 —— 竖排记录横贯整列高，跨水平带是常态

### 两条被**证伪**后撤掉的判据（留档，别再犯）

  ① **「记录跨版式区 = 错配」** —— 原图裁切证明该
     版式的横线在**记录内部**：列内自上而下是「大字公司名 → 横线 → 属性正文」。
     跨带是版式的定义特征；当成错配时四条记录**全中**。
  ② **「记录 x 跨度超列宽 = 跨列错配」** —— 实测一条记录本身就横跨多列
     （「北京工藝商局」属性 x 从 1311 排到 1566，跨 4 列）。竖排记录用几列取决于
     文本长度。**x 跨度不是错配信号。**

**教训**：判据的尺度必须来自**该版式的记录形态**，不能从"排版直觉"推。

## 另一个实测教训：归一化

L0 全程跑在**繁简折叠后的流**上（锚词表简繁双写；不折叠则命中率归零，踩过）。
但**取值必须从原文切**——项目纪律「禁繁简转换，保原字形」。

校验器的做法：两侧都用折叠流比对（于是执行器折没折都不影响判定），
**另加一条** `glyph_folded` 警告——若某值只在折叠流里找得到、在原文流里找不到，
说明执行器把字形改了，违反保原字形纪律。这条不误伤、又能抓住真偏离。

零第三方依赖（折叠取自 `layout_contract.normalize_text`，缺失则退化为仅去空白）。

用法：
    python -u seam.py --result data/results/<stem>.result.json [--plan <plan.json>]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "0.1.0"
KIND = "annotation_result"

# 降级链默认值（plan 里若给了以 plan 为准）
DEFAULT_CHAIN: Tuple[str, ...] = ("rule", "l1", "l2", "single_line")

# ---- 度量阈值（依据见模块 docstring；改这里必须同步改 probe_seam_pc 的结论）----
COVERAGE_WARN_FLOOR = 0.5      # 覆盖度低于此 → 警告（实测中位 0.699 / min 0.383）
LARGE_GAP_CHARS = 20           # 未覆盖段 ≥ 此长度 → 警告（实测 p90 = 20）
SINGLE_LINE_COVERAGE = 0.95    # single_line 档必须真覆盖全页

# 结果里「无框区文本」的合法声明理由（供人工审阅，不参与判定）
# `page_constant`（2026-09-13 增）：页面常量（篇名/版心）—— 已进 plan 的
# `split_spec.page_constants`（由金标准归纳），执行器在取值前会裁掉它，
# 被裁掉的原文仍要在 `unassigned_text` 里声明（文本零丢失）。
UNASSIGNED_REASONS = ("header", "footer", "unannotated_region",
                      "no_attribute", "page_constant", "decorative", "other",
                      "exclusion_zone")
# 有**正当理由**的未覆盖段：不算「疑似整段丢失」。页脚/页眉/无标注区本就该无值。
# `exclusion_zone`（P5，2026-09-26 增）：人工登记的排除带（跨页延续等非本页内容）
# —— 带内 OCR 文本在金标准中不存在是**正常的**，不算缺口。
EXCUSED_UNASSIGNED = ("header", "footer", "unannotated_region",
                      "page_constant", "decorative", "exclusion_zone")

# ---------------------------------------------------------------------------
# 版式应用策略（P0-3b，2026-09-13）—— 执行器**实际用的是哪套版式参数**
# ---------------------------------------------------------------------------
# 为什么需要这个字段：plan 会坦白「某页所属版式族只有单页证据」（`page_model.regions[].covered
# == False`），但在此之前**没有任何地方记录执行器拿到这条信息后怎么办了**。于是出现两种
# 都不可接受的局面：①执行器不知道，仍按主族骨架硬切 → 产出伪命中（`译书院`/`房租`/人名
# 当公司名）倒进待核工作台，人工成本 ≫ API 成本；②执行器知道却没说 → 下游看不出这几条
# 记录是"猜的"还是"读出来的"。
#
# ⇒ 执行器必须在产物里声明**它做了什么**（不是"plan 说了什么"）：
LAYOUT_POLICIES = {
    "family_params": "按所属版式族的参数执行（该族有跨页证据，正常路径）",
    "page_fallback": "该页所属族只有单页证据 → 版式参数无从归纳，降级为「整页一条」兜底"
                     "（不产伪命中、不丢字；产出仍需人工复核）",
}


# ============================================================
# 归一化（与定位口径同源；缺失 zhconv 时退化为仅去空白）
# ============================================================
try:                                                       # pragma: no cover
    from layout_contract import normalize_text as norm_locate
except Exception:                                          # pragma: no cover
    def norm_locate(s: str) -> str:                        # type: ignore
        return "".join(ch for ch in (s or "") if not ch.isspace())


def strip_ws(s: str) -> str:
    """去空白——「换行不算字」，比对前必须归一（与 `preannotate_gen.plain_text` 同口径）。"""
    return "".join((s or "").split())


# ============================================================
# 结果构造（执行器用的助手；手写结果也照此结构）
# ============================================================
def new_result(profile_id: str, stem: str, *,
               tier: str = "rule",
               executor: Optional[Dict[str, Any]] = None,
               plan_ref: Optional[Dict[str, Any]] = None,
               page: Optional[Dict[str, Any]] = None,
               layout: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """建一份空结果骨架。字段含义见 `RESULT_FIELDS`。"""
    return {
        "_schema_version": SCHEMA_VERSION,
        "_kind": KIND,
        "profile_id": profile_id,
        "stem": stem,
        "executor": executor or {"name": "unknown", "kind": "external"},
        "plan_ref": plan_ref or {},
        "tier": tier,
        "tier_trace": [{"tier": tier, "ok": True, "reason": ""}],
        "input": {"text_stream": "", "n_chars": 0, "n_lines": 0},
        "page": page or {},
        "layout": layout or {},
        "records": [],
        "unassigned_text": [],
    }


RESULT_FIELDS = {
    "profile_id": "档案 id，必须与 plan.profile_id 一致",
    "stem": "页标识（原图去扩展名）",
    "tier": "实际使用的降级层级，必须 ∈ plan.fallbacks",
    "tier_trace": "[{tier, ok, reason}] 逐级尝试记账（跳级只告警不判错）",
    "input.text_stream": "本执行器消费的 OCR 文本流（阅读序拼接）；系统会与真实行流核对",
    "records[].lines": "本条记录引用的**行下标区间**（守恒/防重复占用的锚）",
    "records[].attrs[].text": "属性值 —— 必须能在输入流中找到（禁凭空生成）",
    "records[].attrs[].lines": "该值读自哪几行（下标同 input 行序）",
    "unassigned_text[]": "未映射成属性的文本（必须显式声明，否则算静默丢字）",
}
# 可选字段：**不声明不判错**（老产物 / 简单执行器本该能交活），声明了才受判据约束。
RESULT_OPTIONAL_FIELDS = {
    "layout": "本页的版式归属与执行器**实际采用的策略**："
              "{covered, policy, cluster, n_regions}（见 `LAYOUT_POLICIES`）。"
              "由 `plan_page_layout` 读出 plan 的判定，`policy` 是执行器自己的选择 —— "
              "plan 说「未覆盖」而执行器仍按族切时，`validate` 会告警（不判错）。",
}



def add_record(result: Dict[str, Any], *, lines: Sequence[int],
               attrs: Sequence[Dict[str, Any]],
               box: Optional[Sequence[float]] = None,
               region: Optional[str] = None) -> Dict[str, Any]:
    """追加一条记录。`attrs` 每项至少含 `attr` 与 `text`。"""
    rec = {
        "record_index": len(result["records"]),
        "lines": [int(i) for i in lines],
        "attrs": [dict(a) for a in attrs],
    }
    if box:
        rec["box"] = [float(v) for v in box]
    if region:
        rec["region"] = region
    result["records"].append(rec)
    return rec


# ============================================================
# 覆盖核算（按字符偏移的**并集**——比"首尾孤儿"口径强）
# ============================================================
def find_spans(stream: str, pieces: Iterable[str]) -> Tuple[List[Tuple[int, int]], int, int]:
    """按**产出顺序**在 stream 里定位各片段 → `(并集区间, 回退命中数, 找不到数)`。

    游标只前进：命中则游标推到末尾（保序）。找不到时回退全流找一次——
    找到说明**顺序在流里对但产出序不符**（不算幻觉，另计）；仍找不到 =
    凭空生成。

    返回的区间已合并排序。**覆盖核算必须用并集**：早前实现只统计"首条记录之前 /
    末条之后"的孤儿，记录之间的空隙看不见（见模块 docstring）。
    """
    covered: List[Tuple[int, int]] = []
    cursor = 0
    back_hit = 0
    missing = 0
    for p in pieces:
        if not p:
            continue
        k = stream.find(p, cursor)
        if k >= 0:
            covered.append((k, k + len(p)))
            cursor = k + len(p)
            continue
        k2 = stream.find(p)
        if k2 >= 0:
            back_hit += 1
            covered.append((k2, k2 + len(p)))
        else:
            missing += 1
    covered.sort()
    merged: List[List[int]] = []
    for a, b in covered:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged], back_hit, missing


def gaps_of(stream: str, spans: Sequence[Tuple[int, int]]) -> List[Tuple[int, int, str]]:
    """stream 减去已覆盖区间 → `[(起, 止, 文本)]`。"""
    gaps: List[Tuple[int, int, str]] = []
    pos = 0
    for a, b in spans:
        if a > pos:
            gaps.append((pos, a, stream[pos:a]))
        pos = max(pos, b)
    if pos < len(stream):
        gaps.append((pos, len(stream), stream[pos:]))
    return gaps


# ============================================================
# 校验器
# ============================================================
LINE_SOURCE_KINDS = {
    "structured.L2_lines":
        "`data/structured/<stem>.json` 的 `L2_lines`，**列表序即 OCR 输出序**。"
        "原始、未经任何代码内几何重建 —— 任何执行器都能读到完全相同的输入。",
    "preannotate.page_lines":
        "`preannotate.page_lines(stem)`：系统经几何重建 + 阅读序排过的行流。"
        "参照实现走这条（它是生产路径）。",
}


def resolve_line_source(result: dict, *, root: Optional[Path] = None) -> Tuple[Optional[List[dict]], str]:
    """按结果声明的 `input.line_source` 取**真实**输入流 → `(行列表, 说明)`。

    为什么要声明来源：两条流**确实不同**（`L2_lines` 是 OCR 原始输出；
    `preannotate.page_lines` 会按 `L1_blocks` 重建几何并重排）。若不声明，
    校验器就无从知道该拿哪份去对账，只能拿执行器自报的流——那就等于让它自证。
    """
    src = (result.get("input") or {}).get("line_source") or {}
    kind = str(src.get("kind") or "")
    if kind not in LINE_SOURCE_KINDS:
        return None, f"未知或缺失的 line_source.kind={kind!r}（可选：{list(LINE_SOURCE_KINDS)}）"
    stem = str(result.get("stem") or "")
    r = Path(root or ROOT)
    if kind == "structured.L2_lines":
        p = Path(src.get("path") or (r / "data" / "structured" / f"{stem}.json"))
        try:
            doc = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:
            return None, f"读取 {p} 失败：{type(e).__name__}"
        out: List[dict] = []
        for l in (doc.get("L2_lines") or []):
            txt = str(l.get("text") or "")
            if not txt.strip():
                continue
            b = _quad_to_rect(l.get("box"))
            out.append({"text": txt, "box": b})
        return out, f"structured.L2_lines（{Path(p).name}，{len(out)} 行）"
    # preannotate.page_lines
    try:
        import preannotate as PA
        return [l for l in PA.page_lines(stem) if str(l.get("text") or "").strip()], \
               "preannotate.page_lines"
    except Exception as e:                                     # pragma: no cover
        return None, f"preannotate.page_lines 不可用：{type(e).__name__}"


def _quad_to_rect(box) -> List[float]:
    """四边形 `[[x,y]×4]` 或扁平 `[x0,y0,x1,y1]` → `[x0,y0,x1,y1]`。"""
    try:
        if isinstance(box, (list, tuple)) and len(box) == 4 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in box):
            x0, y0, x1, y1 = (float(v) for v in box)
            return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return [min(xs), min(ys), max(xs), max(ys)]
    except (TypeError, IndexError, ValueError, KeyError):
        return []


def _resolve_page_size(result: dict) -> Tuple[Optional[Tuple[float, float]], str]:
    """页尺寸真值优先取**原图**（不信执行器自报）。

    为什么必须自己取：执行器常用「框的极值」当页高，而该批的列框只到 y≈1591、
    真实页高 1806 —— 差 11.5% 足以把页脚区判错（实测外部示例 2/13 个框被误判
    落进无标注区）。与 `line_source` 同一条纪律：**真值由校验器自己读**。
    """
    stem = str(result.get("stem") or "")
    try:
        import layout_contract as LC
        # ★ 2026-09-15 WP-2a：真值目录口径统一走 `config.image_path_of`
        #   （全项目唯一解析口，OUTBOX → INBOX 两处查）。
        #   此前硬编码 `ROOT / "outbox"` —— 与被执行器的取图路径**完全相同**，
        #   于是"取不到真值"这件事对校验器与被检查对象是**同时发生**的：
        #   两边都退到同一份自报量，护栏结构上不可能发现图在 `inbox/`。
        #   纪律：**校验器读真值的路径必须独立于被检查对象的推导路径**。
        import config as CONFIG
        img = CONFIG.image_path_of(stem)
        # ⚠ 解析不到时退回**原路径**形状（同 `preannotate` / `plan_exec`）：
        #   保持可被 monkeypatch 注入，且图真不在时同为 None，不引入猜测。
        sz = LC.page_size(img if img is not None
                          else ROOT / "outbox" / f"{stem}.png")
        if sz:
            return (float(sz[0]), float(sz[1])), "image"
    except Exception:
        pass
    pg = result.get("page") or {}
    if pg.get("w") and pg.get("h"):
        return (float(pg["w"]), float(pg["h"])), f"self-declared({pg.get('source') or '?'})"
    return None, "unknown"


def _err(code: str, msg: str, where: str = "") -> Dict[str, str]:
    return {"code": code, "msg": msg, "where": where}


def plan_page_layout(plan: Optional[dict], stem: str) -> Dict[str, Any]:
    """plan → 该页的**版式归属与覆盖判定**（P0-3b，2026-09-13）。**唯一读口**。

    `plan_build` 已经判好（`page_model.regions[].covered` / `layout_clusters[].coverage`），
    本函数**只查表**不做判断 —— 执行器与校验器都从这里读，不得各自解析 plan。
    （判据唯一出处这条纪律：两处各写一遍 = 两处各错一遍。）

    返回值恒有 `known`：
      · `known=False` —— plan 缺 `page_model`，或该页根本不在 `regions[]` 里。
        老版 plan / 外部 plan 会走到这里：**不判错，也不假装知道**。
      · `known=True` —— 另带 `covered`（该族是否有跨页证据）、`cluster`、
        `coverage`（`inducible` / `single_page_evidence`）、`n_regions`。

    ⚠ 这里**不返回 `policy`**：用哪套参数是**执行器自己的决定**，plan 无权代言。
      执行器把 `plan_page_layout(...)` 的结果拷贝一份、填上自己的 `policy`
      放进 `result["layout"]` —— 于是产物回答的是"你实际做了什么"，而非"plan 说了什么"。
    """
    pm = (plan or {}).get("page_model") or {}
    row = None
    for r in pm.get("regions") or []:
        if str((r or {}).get("stem") or "") == str(stem):
            row = r
            break
    if row is None:
        return {"known": False, "covered": None, "policy": None,
                "cluster": None, "coverage": None, "n_regions": 0}
    cid = row.get("cluster")
    cov = None
    for c in pm.get("layout_clusters") or []:
        if (c or {}).get("id") == cid:
            cov = (c or {}).get("coverage")
            break
    return {
        "known": True,
        "covered": bool(row.get("covered")),
        "policy": None,
        "cluster": cid,
        "coverage": cov,
        "n_regions": len(row.get("regions") or []),
    }


def _plan_regions_for(plan: Optional[dict], stem: str) -> List[dict]:
    for r in ((plan or {}).get("page_model") or {}).get("regions") or []:
        if r.get("stem") == stem:
            return r.get("regions") or []
    return []


def _region_at(y: float, page_h: float, regions: Sequence[dict]) -> Optional[dict]:
    if not page_h or not regions:
        return None
    r = y / page_h
    for reg in regions:
        lo, hi = (reg.get("rel") or [0.0, 1.0])[:2]
        if lo <= r < hi:
            return reg
    return None


def verify_exit_code(rep: dict) -> int:
    """唯一判定点：`validate` 报告 `ok` ⇒ 0，否则 4（校验不过 —— 改规则/产物，不是补料）。

    `chronicles verify` 取用本函数，包装层不重判（同 `triage_exit_code` 模式）。
    """
    return 0 if rep.get("ok") else 4


def validate(result: dict, *, plan: Optional[dict] = None,
             lines: Optional[Sequence[dict]] = None,
             page_size: Optional[Tuple[float, float]] = None,
             resolve_source: bool = True) -> Dict[str, Any]:
    """校验一份层 3 产出 → `{ok, errors, warnings, metrics}`。

    `lines`：真实 OCR 行流 `[{text, box}]`（阅读序）。不给且 `resolve_source=True`
    时，按结果声明的 `input.line_source` **由校验器自己去读**（不让执行器自证）；
    给了则以调用方给的为准（最强）。

    `page_size` 为 `(w, h)`；不给时用 `result.page`，再不给则跳过页面边界判据。
    """
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    # ---------- E1 schema ----------
    if not isinstance(result, dict):
        return {"ok": False, "errors": [_err("schema", "结果不是对象")],
                "warnings": [], "metrics": {}}
    if result.get("_kind") != KIND:
        errors.append(_err("schema", f"_kind 应为 {KIND}，实为 {result.get('_kind')!r}"))
    sv = str(result.get("_schema_version") or "")
    if sv.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        errors.append(_err("schema", f"主版本不符：{sv} vs {SCHEMA_VERSION}"))
    for k in ("profile_id", "stem", "tier", "records", "input"):
        if k not in result:
            errors.append(_err("schema", f"缺字段 {k}"))
    if errors and any(e["code"] == "schema" for e in errors):
        return {"ok": False, "errors": errors, "warnings": warnings, "metrics": {}}

    tier = str(result.get("tier") or "")
    chain = tuple((plan or {}).get("fallbacks") or DEFAULT_CHAIN)

    # ---------- E2/E3/W 降级合规 ----------
    if tier not in chain:
        errors.append(_err("tier_unknown", f"tier={tier!r} 不在 plan.fallbacks={list(chain)}"))
    trace = result.get("tier_trace") or []
    if trace:
        seq = [t.get("tier") for t in trace]
        ok_seq = [t.get("ok") for t in trace]
        for t in seq:
            if t not in chain:
                errors.append(_err("tier_unknown", f"tier_trace 含未知层级 {t!r}"))
        # 尝试过的层级必须是 chain 的**子序列**（保序）
        idx = [chain.index(t) for t in seq if t in chain]
        if idx != sorted(idx):
            warnings.append(_err("tier_trace_order",
                                 f"tier_trace 未按降级链顺序：{seq} vs {list(chain)}"))
        if idx and (max(idx) - min(idx) + 1) != len(idx):
            warnings.append(_err("tier_skip", f"降级跳级：{seq}（链 {list(chain)}）"))
        # 最终 tier 应与 trace 里最后一个 ok=True 的层级一致
        done = [s for s, o in zip(seq, ok_seq) if o]
        if done and done[-1] != tier:
            errors.append(_err("tier_mismatch",
                               f"tier={tier!r} 与 tier_trace 最后一个成功层级 {done[-1]!r} 不符"))

    # ---------- 输入流 ----------
    inp = result.get("input") or {}
    declared = str(inp.get("text_stream") or "")
    # 归一化必须**整串同法**：zhconv 存在上下文相关的字符，逐行折叠与整串折叠
    # 结果可能不等（若两边各用一种就会产生假 `stream_mismatch`）。
    src_note = "调用方直接给了行流"
    if lines is None and resolve_source:
        lines, src_note = resolve_line_source(result)
        if lines is None:
            warnings.append(_err("line_source_unresolved",
                                 f"拿不到真实输入流（{src_note}）→ 本次只做结果内部自洽校验"))
    real_raw = real_norm = None
    if lines is not None:
        real_raw = "".join(strip_ws(str(l.get("text") or "")) for l in lines)
        real_norm = norm_locate(real_raw)
        if norm_locate(declared) != real_norm:
            errors.append(_err(
                "stream_mismatch",
                f"自报输入流与真实行流不符（自报 {len(norm_locate(declared))} 字 / "
                f"真实 {len(real_norm)} 字；来源 {src_note}）—— "
                f"不得截断输入以掩盖丢字，也不得自行重排顺序",
                "input.text_stream"))
    stream_n = norm_locate(declared)
    stream_r = real_raw if real_raw is not None else strip_ws(declared)

    # ---------- 逐记录收集 ----------
    records = result.get("records") or []
    pieces: List[str] = []
    raw_values: List[str] = []
    n_attrs = 0
    used_lines: Dict[int, int] = {}          # 行下标 → 记录号（查跨记录重复占用）
    spans_lines: List[Tuple[int, int, int]] = []   # (起, 止, 记录号)
    region_by_rec: Dict[int, List[str]] = {}

    for ri, rec in enumerate(records):
        if not isinstance(rec, dict):
            errors.append(_err("schema", f"records[{ri}] 不是对象"))
            continue
        rl = [int(i) for i in (rec.get("lines") or [])]
        if rl:
            spans_lines.append((min(rl), max(rl), ri))
            for i in rl:
                if not (0 <= i < (len(lines) if lines is not None else 10 ** 9)):
                    errors.append(_err("line_out_of_range",
                                       f"records[{ri}].lines 含越界下标 {i}", f"records[{ri}]"))
                    continue
                if i in used_lines and used_lines[i] != ri:
                    errors.append(_err(
                        "line_reused",
                        f"行 {i} 同时被记录 {used_lines[i]} 与 {ri} 引用（跨记录重复占用）",
                        f"records[{ri}]"))
                else:
                    used_lines[i] = ri
        elif lines is not None:
            warnings.append(_err("record_no_lines",
                                 f"records[{ri}] 未声明 lines → 该项无法核验重复占用",
                                 f"records[{ri}]"))
        for a in (rec.get("attrs") or []):
            if not isinstance(a, dict):
                continue
            txt = str(a.get("text") or "")
            if not txt.strip():
                continue
            n_attrs += 1
            pieces.append(norm_locate(txt))
            raw_values.append(strip_ws(txt))
            al = a.get("lines")
            if al:
                for i in al:
                    i = int(i)
                    if not (0 <= i < (len(lines) if lines is not None else 10 ** 9)):
                        errors.append(_err("line_out_of_range",
                                           f"属性「{a.get('attr')}」引用越界行 {i}",
                                           f"records[{ri}]"))
            elif lines is not None and not rl:
                warnings.append(_err("attr_no_lines",
                                     f"属性「{a.get('attr')}」未声明来源行",
                                     f"records[{ri}]"))

    # 记录区间相交
    spans_lines.sort()
    for (a0, a1, ari), (b0, b1, bri) in zip(spans_lines, spans_lines[1:]):
        if b0 <= a1 and ari != bri:
            errors.append(_err("record_overlap",
                               f"记录 {ari} 的行区间 [{a0},{a1}] 与记录 {bri} 的 [{b0},{b1}] 相交"))

    # ---------- 守恒：凭空生成 ----------
    spans, back_hit, missing = find_spans(stream_n, pieces)
    if missing:
        bad = [v for v in raw_values if norm_locate(v) not in stream_n][:3]
        errors.append(_err("fabricated",
                           f"{missing} 个属性值在输入流中找不到（凭空生成）。例：{bad}"))
    if back_hit:
        warnings.append(_err("value_disorder",
                             f"{back_hit} 个属性值在流中需回退才命中——"
                             f"多为「多人名族」属性（模板序 ≠ 流序），不判错"))

    # ---------- 保原字形（原文流里找不到、折叠流里找得到） ----------
    n_folded = 0
    if real_raw is not None:
        for v in raw_values:
            if v and v not in real_raw and norm_locate(v) in real_norm:
                n_folded += 1
        if n_folded:
            warnings.append(_err("glyph_folded",
                                 f"{n_folded} 个值的字形只在折叠流中匹配——"
                                 f"取值应从**原文**切（禁繁简转换，保原字形）"))

    # ---------- 守恒：静默丢字 ----------
    declared_un = result.get("unassigned_text") or []
    un_pieces: List[str] = []
    excused: List[Tuple[int, int]] = []      # 有正当理由的未覆盖段（区外/页眉/页脚）
    for u in declared_un:
        if isinstance(u, dict):
            t = str(u.get("text") or "")
            reason = str(u.get("reason") or "")
        else:
            t, reason = str(u or ""), ""
        if not t.strip():
            continue
        un_pieces.append(norm_locate(t))
        if reason in EXCUSED_UNASSIGNED:
            k = stream_n.find(norm_locate(t))
            if k >= 0:
                excused.append((k, k + len(norm_locate(t))))
    un_spans, un_back, un_missing = find_spans(stream_n, un_pieces)
    if un_missing:
        errors.append(_err("unassigned_fabricated",
                           f"{un_missing} 段 unassigned_text 在输入流中找不到"))
    merged_all: List[Tuple[int, int]] = []
    for a, b in sorted(list(spans) + list(un_spans)):
        if merged_all and a <= merged_all[-1][1]:
            merged_all[-1] = (merged_all[-1][0], max(merged_all[-1][1], b))
        else:
            merged_all.append((a, b))
    silent = gaps_of(stream_n, merged_all)
    silent_n = sum(b - a for a, b, _ in silent)
    if silent:
        big = sorted(silent, key=lambda g: -(g[1] - g[0]))[:2]
        errors.append(_err(
            "silent_gap",
            f"{silent_n} 字未覆盖且未在 unassigned_text 中声明（静默丢字）。"
            f"最大缺口：" + "；".join(f"[{a}:{b}] {t[:20]}" for a, b, t in big)))

    attr_cov = sum(b - a for a, b in spans) / len(stream_n) if stream_n else 0.0
    cov = sum(b - a for a, b in merged_all) / len(stream_n) if stream_n else 0.0
    # 覆盖度门槛看**属性值覆盖**（合计覆盖恒为 1.0 —— 缺口都被声明了，看不出问题）。
    if stream_n and attr_cov < COVERAGE_WARN_FLOOR:
        warnings.append(_err(
            "coverage_low",
            f"属性值覆盖度 {attr_cov:.3f} < {COVERAGE_WARN_FLOOR}（实测中位 0.699）"))
    # 大缺口 = 「值覆盖不到」且**理由不是区外/页眉/页脚**的段 —— 这才是"疑似整段丢失"。
    # （若把已声明为 footer 的段也算进来，页脚 5 字就会天天报警，失去分辨力。）
    susp_all: List[Tuple[int, int]] = []
    for a, b in sorted(list(spans) + list(excused)):
        if susp_all and a <= susp_all[-1][1]:
            susp_all[-1] = (susp_all[-1][0], max(susp_all[-1][1], b))
        else:
            susp_all.append((a, b))
    big_gaps = [(a, b, t) for a, b, t in gaps_of(stream_n, susp_all)
                if (b - a) >= LARGE_GAP_CHARS]
    if big_gaps:
        warnings.append(_err(
            "large_gap",
            f"{len(big_gaps)} 处未覆盖段 ≥{LARGE_GAP_CHARS} 字且无正当理由（疑似整段丢失）"
            f"→ 建议进待核。最大 {max((b - a) for a, b, _ in big_gaps)} 字；"
            f"例：{big_gaps[0][2][:24]}"))

    # ---------- 降级档形态 ----------
    # `single_line` = 「整页一条，保底不丢字」：要求**恰好 1 条**且该条的行区间
    # 覆盖整页。**不要求属性覆盖率** —— 它是从全页文本里抽属性，抽不到的部分
    # 走 unassigned_text 声明即可（早前用 `attr_cov ≥ 0.95` 当判据，把真实的
    # 兜底形态判成违规）。
    if tier == "single_line":
        rl = [int(i) for i in ((records[0] or {}).get("lines") or [])] if records else []
        span = (max(rl) - min(rl) + 1) if rl else 0
        need = int(0.9 * len(lines)) if lines is not None else 0
        if len(records) != 1:
            errors.append(_err("single_line_bad",
                               f"single_line 档必须恰好 1 条，实际 {len(records)} 条"))
        elif need and span < need:
            errors.append(_err("single_line_bad",
                               f"single_line 档的那一条必须覆盖整页："
                               f"行区间跨度 {span} < {need}（页共 {len(lines)} 行）"))

    # ---------- 版式应用策略（P0-3b） ----------
    # 只观测、不判错：**结果合规** 与 **结果可信** 是两件事。执行器明知版式未覆盖
    # 仍按族切，产出照样可以守恒（不丢字、不幻觉）—— 但它会产"伪命中"（把非目标
    # 题材的段落按主族骨架切出属性），那是人工要花掉一整轮复核成本的地方。
    # 故这里给**告警**：合规判定不拦它，但合规报告里必须看得到。
    # ⚠ 老产物没有 `layout` 字段 → `lay` 为空 → 一条判据都不触发（不追旧账）。
    lay = result.get("layout") or {}
    lay_cov = lay.get("covered")
    lay_pol = lay.get("policy")
    if lay:
        if lay_pol and lay_pol not in LAYOUT_POLICIES:
            warnings.append(_err("layout_policy_unknown",
                                 f"未知的版式策略 {lay_pol!r}（可选：{list(LAYOUT_POLICIES)}）"))
        if lay_cov is False and lay_pol != "page_fallback":
            warnings.append(_err(
                "layout_uncovered",
                f"该页所属版式族只有单页证据（参数沿用主族），但执行器策略是 {lay_pol!r}"
                f"（plan 的降级建议是 page_fallback = 整页兜底）—— 产出需人工复核"))
        if lay_cov is None and lay_pol == "page_fallback":
            warnings.append(_err("layout_uncovered",
                                 "声明整页兜底但未给 covered —— 声明不完整，无法追"))
        if lay_pol == "page_fallback" and tier != "single_line":
            warnings.append(_err(
                "layout_policy_mismatch",
                f"策略声明 page_fallback（整页兜底）但 tier={tier!r} —— "
                f"兜底档就是 single_line，两者必须一致"))

    # ---------- 对位 ----------
    size, size_src = _resolve_page_size(result) if not page_size else (page_size, "caller")
    if page_size is None and size is None:
        warnings.append(_err("page_size_unknown",
                             "取不到页尺寸（原图缺失且未自报）→ 跳过页面边界/分区判据"))
    pg_decl = result.get("page") or {}
    if (page_size is None and size and size_src == "image" and pg_decl.get("h")
            and abs(float(pg_decl["h"]) - size[1]) > 0.02 * size[1]):
        warnings.append(_err(
            "page_size_mismatch",
            f"自报页高 {pg_decl['h']} 与真实 {size[1]:.0f} 差 "
            f"{abs(float(pg_decl['h']) - size[1]) / size[1] * 100:.1f}% —— "
            f"分区/越界判据按**真实**尺寸算（自报尺寸偏小会把页脚误判成内容区）"))
    regions = _plan_regions_for(plan, str(result.get("stem") or ""))
    n_out_page = 0
    for ri, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        boxes: List[List[float]] = []
        for a in (rec.get("attrs") or []):
            if not isinstance(a, dict):
                continue
            for b in ([a.get("box")] if a.get("box") else []) + list(a.get("boxes") or []):
                bb = _as_box(b)
                if bb:
                    boxes.append(bb)
        if rec.get("box"):
            bb = _as_box(rec["box"])
            if bb:
                boxes.append(bb)
        for bb in boxes:
            x0, y0, x1, y1 = bb
            if x1 <= x0 or y1 <= y0:
                errors.append(_err("box_inverted", f"框非法 {bb}", f"records[{ri}]"))
            if size and (x0 < -0.5 or y0 < -0.5 or x1 > size[0] + 0.5 or y1 > size[1] + 0.5):
                n_out_page += 1
        if not boxes:
            continue
        # **不按 x 跨度判"跨列"**：实测金标准里一条记录本身就横跨多列
        # （「北京工藝商局」的属性 x 从 1311 排到 1566，跨 4 列）——竖排记录
        # 用到几列取决于文本长度，不是错配。故 x 跨度只作观测值进 metrics。
        region_by_rec.setdefault(ri, [])
        if regions and size:
            ycs = [(b[1] + b[3]) / 2.0 for b in boxes]
            roles = []
            for y in ycs:
                reg = _region_at(y, size[1], regions)
                roles.append(str((reg or {}).get("role") or ""))
            hit = {(_region_at(y, size[1], regions) or {}).get("id") for y in ycs}
            hit.discard(None)
            region_by_rec[ri] = sorted(str(h) for h in hit)
            # **跨区不判错**：实测该版式的一条记录 = 自右向左的若干列连续文本，
            # 列内自上而下先是大字公司名、再一条横线、再是属性正文——**横线在记录
            # 内部**。故记录跨越「名带 + 正文带」是版式的定义特征，不是错配。
            # （原图裁切证实；把它当错配时四条记录全中。）
            if len(hit) > 1:
                warnings.append(_err(
                    "record_spans_bands",
                    f"记录跨 {len(hit)} 个版式区（{sorted(hit)}）——竖排记录横贯整列高，"
                    f"跨水平带属常态，仅作观测",
                    f"records[{ri}]"))
            # 判据：只有**所有**框中心都落在无标注区，才算"记录整体跑进了报头/页脚"。
            # 单个框越界多半是框算式问题（另有 box_out_of_page 兜底），不该整条记录连坐。
            bad = [r for r in roles if r.startswith("unannotated")]
            if bad and len(bad) == len(roles):
                errors.append(_err(
                    "record_in_unannotated",
                    f"记录全部落在无标注区（role={bad[0]}）—— "
                    f"plan invariant 明说无框区不产记录",
                    f"records[{ri}]"))
            elif bad:
                warnings.append(_err(
                    "record_touches_unannotated",
                    f"{len(bad)}/{len(roles)} 个框中心落在无标注区（{bad[0]}）",
                    f"records[{ri}]"))
    if n_out_page:
        errors.append(_err("box_out_of_page", f"{n_out_page} 个框越出页边界"))

    metrics = {
        "n_records": len(records),
        "n_attrs": n_attrs,
        "n_lines": len(lines) if lines is not None else None,
        "line_source": src_note,
        "n_stream_chars": len(stream_n),
        "attr_coverage": round(attr_cov, 4),
        "coverage": round(cov, 4),
        "n_unassigned": len(un_pieces),
        "n_uncovered_chars": silent_n,
        "n_large_gaps": len(big_gaps),
        "n_value_disorder": back_hit,
        "n_glyph_folded": n_folded,
        "region_by_record": region_by_rec,
        "chain": list(chain),
        # 版式应用（P0-3b）：`None` = 该产物没声明（老版 / 简单执行器），不是"未覆盖"
        "layout_covered": lay_cov,
        "layout_policy": lay_pol,
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "metrics": metrics}


def _as_box(b) -> Optional[List[float]]:
    try:
        if isinstance(b, (list, tuple)) and len(b) == 4:
            x0, y0, x1, y1 = (float(v) for v in b)
            return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
    except (TypeError, ValueError):
        return None
    return None


# ============================================================
# 报告 / 读写
# ============================================================
def format_report(rep: Dict[str, Any], title: str = "结果校验") -> str:
    L: List[str] = []
    A = L.append
    A(f"=== {title} ===")
    A(f"判定：{'✓ 合规' if rep['ok'] else '✗ 不合规'}")
    m = rep.get("metrics") or {}
    if m:
        A(f"记录 {m.get('n_records')} 条 / 值 {m.get('n_attrs')} 个 / "
          f"流 {m.get('n_stream_chars')} 字")
        A(f"覆盖度 值={m.get('attr_coverage')} 合计={m.get('coverage')} "
          f"（未覆盖 {m.get('n_uncovered_chars')} 字，大缺口 {m.get('n_large_gaps')} 处）")
        if m.get("n_value_disorder"):
            A(f"模板序 ≠ 流序的值：{m['n_value_disorder']} 个（不判错）")
        if m.get("n_glyph_folded"):
            A(f"字形被转换的值：{m['n_glyph_folded']} 个")
    for e in rep.get("errors") or []:
        A(f"  [错] {e['code']:<22} {e['msg']}")
    for w in rep.get("warnings") or []:
        A(f"  [警] {w['code']:<22} {w['msg']}")
    return "\n".join(L)


def load_json(p) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def save_result(result: dict, out_dir=None, name: Optional[str] = None) -> Path:
    """原子写（与项目 `data_io.atomic_write_json` 同纪律）。"""
    out = Path(out_dir or (ROOT / "data" / "results"))
    out.mkdir(parents=True, exist_ok=True)
    p = out / (name or f"{result.get('stem')}.result.json")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def _main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="层 3 结果校验器")
    ap.add_argument("--result", required=True, help="结果 json 路径")
    ap.add_argument("--plan", default="", help="plan json 路径（给了才做 plan 对齐判据）")
    ap.add_argument("--no-source", action="store_true",
                    help="不去解析真实输入流（只核结果内部自洽，较弱）")
    args = ap.parse_args(argv)

    res = load_json(args.result)
    plan = load_json(args.plan) if args.plan else None
    # lines 不给 → validate 按结果声明的 `input.line_source` 自行解析真实输入流
    # （来源由校验器自己读，执行器无法自证）；`--no-source` 则退化为只核内部自洽。
    rep = validate(res, plan=plan, resolve_source=not args.no_source)
    print(format_report(rep, title=Path(args.result).name))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
