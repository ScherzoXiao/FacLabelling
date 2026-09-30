# -*- coding: utf-8 -*-
"""L1 指针式补全（2026-09-11）——把"复述整页"换成"只报行号"。

**成本根因**（§41 取证）：拆分层一页 completion ≈ 10k–16k tokens，而输入正文
仅 322 字 —— 钱全花在**让模型把正文重抄一遍**（输出是输入的 4 倍）。
成本 ∝ 需要生成的 token 数，所以唯一的治本是**别让它生成正文**。

**L1 的做法**：把 L0 已经切好的记录 + 按行编号的原文交给模型，让它**只回答
"某个属性该取第几行"**。值由本地按行号切片，模型一个字都不用抄。

    输入 ≈ 1.3k tokens（指令 + 记录摘要 + 编号文本）
    输出 ≈ 0.3k tokens（全是行号数字）
    vs L2 的 10k–16k → 每页 token 量约 1/8，**输出侧约 1/30**

分工（各用所长）：

    L0  `rule_split`   几何切分 + 锚词取值      零 token   精确率高、覆盖不全
    L1  本模块         只补 L0 **没对齐**的部分  极省       语义判断"值在哪一行"
    L2  `split_records` 兜底复述整页             贵         仅在 L1 也读不出时启用

纪律（与项目红线一致）：

  - **只填空、绝不覆盖** L0 已有的值（L0 精确率 0.953，不该被模型猜掉）
  - **模型不产出正文**：输出里只允许出现行号；值一律由本地从原文切片
  - 行号**当场校验**（越界 / 空值 / 页常量 / 超长 → 丢弃并计数，不静默）
  - 异常不外抛（逐页隔离）；全 mock 可测，单测绝不真实调用线上 API
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import rule_split as RS

log = logging.getLogger("split_l1")

MODE_REPAIR = "repair"          # L0 已切分 → 只补它留空的属性
MODE_FULL = "full"              # L0 未切分（整页兜底）→ 自己划记录

L1_TEMPERATURE = 0.0
L1_TIMEOUT = 120
# **关思考**：L1 是"指路型"任务（模型只需把属性映射到行号），不需要长推理。
# 实测 deepseek-flash：同一提示词、同一答案，reasoning 1872→0，
# 耗时 11.1s→0.6s，输出 2199→66 tokens（**6.3×**）。这是 L1 省钱的一半。
L1_REASONING_EFFORT = "none"
# 指针输出极小（一页通常 < 400 tokens）。4k 足够；真被截断会显式报错而非静默。
L1_MAX_TOKENS = 4096
L1_RETRIES = 1
# 单值硬上限：L0 是 80，这里放宽一档（L1 的语义判断可能合并相邻行），
# 再超就是"模型把一整段倒进来了"，属于异常，丢弃。
L1_MAX_VALUE_CHARS = 120
# 提示词里每行的展示上限（正文行实测 24–27 字，60 字足够且防超长块爆输入）
LINE_TEXT_MAX = 60
# 记录摘要里"已填值"的展示上限（只用来给模型定位，不需要全值）
_SHOW_VALUE_MAX = 24

# 成本参考（元 / 百万 tokens）。**仅作量级提示**——真实账单以服务商为准，
# 可用 data/llm_config.json 的 price_in_per_m / price_out_per_m 覆盖。
DEFAULT_PRICE_IN_PER_M = 2.0
DEFAULT_PRICE_OUT_PER_M = 8.0

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# ============================================================
# 提示词
# ============================================================
def fill_targets(spec: dict) -> List[str]:
    """L1 允许补的属性和类 = **L0 本来就可能填上的那些**。

    判据全部取自 L0 自身，不做任何新的语义发明：
      有确定锚词的属性（`spec["anchors"]`）+ 人名槽位（`spec["person_attr_set"]`）
      + 记录锚属性（`spec["anchor_attr"]`）。

    这样 L1 的补全空间与 L0 **完全重合**——它只负责"把 L0 没抓住的抓住"，
    不会去碰档案里那些**根本不属于记录**的标注项。实测教训（0001 页）：
    不设这道门时"待填"里会混进「备注」「表头」，模型被诱导着乱指行号，
    既浪费输出 token 又制造幻觉。
    """
    anchors = spec.get("anchors") or {}
    persons = spec.get("person_attr_set") or set()
    anchor_attr = spec.get("anchor_attr")
    return [str(a) for a in (spec.get("attrs") or [])
            if str(a) in anchors or str(a) in persons or str(a) == anchor_attr]


def _fmt_lines(lines: List[dict]) -> str:
    """按行编号的原文（**行号即阅读序**，竖排古籍：一行 = 一列）。

    超长行截断展示只为省输入 token —— 取值时仍用**完整原文**切片，
    所以截断不会污染结果，最多让模型少看到一点上下文。
    """
    out: List[str] = []
    for i, u in enumerate(lines):
        t = (u.get("text") or "").strip()
        if len(t) > LINE_TEXT_MAX:
            t = t[:LINE_TEXT_MAX] + "…"
        out.append(f"{i}｜{t}")
    return "\n".join(out)


def _fmt_record(i: int, rec: dict, det: Optional[dict], attrs: List[str]) -> str:
    """一条记录的摘要：行区间 + 已填（带值，供定位）+ 待填（属性名）。"""
    span = ""
    if det:
        s, e = det.get("name_index"), det.get("end_index")
        if isinstance(s, int) and isinstance(e, int):
            span = f"｜行 {s}–{max(s, e - 1)}"
    filled = [(k, str(v).strip()) for k, v in rec.items() if str(v or "").strip()]
    todo = [a for a in attrs if not str(rec.get(a) or "").strip()]
    show = "；".join(
        f"{a}=「{v[:_SHOW_VALUE_MAX]}{'…' if len(v) > _SHOW_VALUE_MAX else ''}」"
        for a, v in filled) or "（无）"
    return (f"记录 {i}{span}\n"
            f"  已填：{show}\n"
            f"  待填：{('、'.join(todo)) if todo else '（无）'}")


def build_l1_prompt(*, mode: str, lines: List[dict], records: List[dict],
                    details: Optional[List[dict]] = None,
                    spec: dict, profile: Optional[dict] = None) -> str:
    """组装 L1 提示词。**只读**：不修改任何入参。

    `mode=MODE_REPAIR` → 逐记录列"已填/待填"，只要模型补待填项的行号；
    `mode=MODE_FULL`   → 不给记录，要模型自己划记录并给出全部属性的行号。
    """
    attrs = fill_targets(spec)
    details = details or []
    attr_line = "、".join(attrs) if attrs else "（档案未定义属性）"
    consts = [str(c) for c in (spec.get("page_constants") or [])]
    const_line = "、".join(f"「{c}」" for c in consts) if consts else "（无）"

    common_tail = (
        "\n\n==== 输出格式（严格遵守）====\n"
        "只输出一个 JSON 对象；**不要**解释文字，**不要**代码围栏：\n"
        '{"records": [{"i": 0, "a": {"注册资本": [9, 10], "类别": [11]}}, ...]}\n\n'
        "- `i`：记录编号，必须与上文「记录 N」一致\n"
        "- `a`：该记录要补的属性 → **行号数组**（值横跨多行时按阅读顺序全部列出）\n"
        "- **只允许输出行号，绝对不要输出任何文本内容**\n"
        "- 行号必须是下面列出的编号之一；在给定行里找不到依据的属性，"
        "**不要**出现在 `a` 里（宁缺勿猜）\n"
        "- 页眉、栏目名、页码、页脚**不能**作为任何属性的取值\n"
        f"- 本页页眉/栏目名（不属于任何记录）：{const_line}\n"
        f"- 可补属性（属性名**只能**从下列取）：{attr_line}\n"
        "\n==== 按行编号的转写（行号｜文本）====\n"
    )

    if mode == MODE_FULL:
        head = (
            "你是历史文献结构化助手。下面把一页**竖排古籍**的转写文本按行编号列出。\n"
            "**一行 = 一列**，行号即阅读顺序。\n\n"
            "==== 任务 ====\n"
            "系统**未能**自动切分本页。请你依据下面的行**自己划出记录**，\n"
            "并把每条记录的属性映射到行号。\n"
            "- 记录按阅读顺序排列；每条记录**必须**含锚属性"
            f"「{spec.get('anchor_attr') or '公司名'}」\n"
            "- 一条记录可跨若干行；它的属性值就在这些行里\n"
            '- 确实没有记录时输出 {"records": []}\n'
        )
        return head + common_tail + _fmt_lines(lines)

    head = (
        "你是历史文献结构化助手。下面把一页**竖排古籍**的转写文本按行编号列出。\n"
        "**一行 = 一列**，行号即阅读顺序。\n\n"
        "==== 系统已自动切分出的记录 ====\n"
        "（\"已填\"是系统已从原文摘出的值，\"待填\"是系统没能定位的属性）\n"
    )
    body = "\n".join(
        _fmt_record(i, r, details[i] if i < len(details) else None, attrs)
        for i, r in enumerate(records))
    task = (
        "\n==== 任务 ====\n"
        "请你**只处理上面每个「待填」里的属性**：指出它的值应当取自下面哪几行。\n"
        "不要改动、也不要重复输出「已填」里的属性。\n"
    )
    return head + body + task + common_tail + _fmt_lines(lines)


# ============================================================
# 解析（结构层；语义校验留给 apply_l1）
# ============================================================
def _extract_json(raw: str) -> str:
    """从模型输出里取出 JSON 主体（容忍代码围栏 / 前后废话）。"""
    if not raw:
        return ""
    m = _FENCE_RE.search(raw)
    if m and m.group(1).strip():
        raw = m.group(1)
    s, e = raw.find("{"), raw.rfind("}")
    return raw[s:e + 1] if s >= 0 and e > s else ""


def _as_int(v: Any) -> Optional[int]:
    """宽容取整：int / "3" / 3.0 → 3；其余 → None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        t = v.strip()
        if re.fullmatch(r"-?\d+", t):
            return int(t)
    return None


def parse_l1(raw: str) -> dict:
    """模型输出 → `{records:[{i, a}], parse_ok, reason, n_raw}`（纯结构，不查语义）。

    `a` 的值统一归一成 `int` 列表；非法的单个条目在此**丢弃并计数**，
    不让它们往下游流（下游 `apply_l1` 再做属性名/行号范围/值质量校验）。
    """
    txt = _extract_json(raw)
    if not txt:
        return {"records": [], "parse_ok": False, "reason": "no_json",
                "n_raw": 0, "n_bad_index": 0}
    try:
        d = json.loads(txt)
    except json.JSONDecodeError as e:
        return {"records": [], "parse_ok": False,
                "reason": f"json_error: {e}", "n_raw": 0, "n_bad_index": 0}
    if not isinstance(d, dict):
        return {"records": [], "parse_ok": False, "reason": "not_object",
                "n_raw": 0, "n_bad_index": 0}
    raw_recs = d.get("records")
    if not isinstance(raw_recs, list):
        return {"records": [], "parse_ok": False, "reason": "no_records_key",
                "n_raw": 0, "n_bad_index": 0}

    out: List[dict] = []
    n_bad = 0
    for item in raw_recs:
        if not isinstance(item, dict):
            n_bad += 1
            continue
        amap = item.get("a")
        if not isinstance(amap, dict):
            n_bad += 1
            continue
        clean: Dict[str, List[int]] = {}
        for k, v in amap.items():
            if not isinstance(k, str) or not k.strip():
                n_bad += 1
                continue
            vals = v if isinstance(v, list) else [v]
            idxs: List[int] = []
            for x in vals:
                j = _as_int(x)
                if j is None:
                    n_bad += 1
                    continue
                idxs.append(j)
            if idxs:
                clean[k.strip()] = idxs
        if clean:
            out.append({"i": _as_int(item.get("i")), "a": clean})
    return {"records": out, "parse_ok": bool(out), "reason": "",
            "n_raw": len(raw_recs), "n_bad_index": n_bad}


# ============================================================
# 取值与合并（本地切片 —— 模型一个字都没抄）
# ============================================================
def _slice_value(lines: List[dict], idxs: List[int], spec: dict,
                 attr: Optional[str] = None, refine: bool = True
                 ) -> Tuple[Optional[str], str]:
    """行号 → 值（本地从**原文**切片）。返回 `(值, 拒绝原因)`。

    `refine=True` 时（默认）**收缩**：行号只用来定位，真正的取值交给
    `rule_split.extract_one` 那套锚词实现。理由是实测教训——竖排一行常同时
    装着日期/职衔/人名/地址，照搬整行会把「注册人二」填成一整行，
    属性精确从 0.953 掉到 0.827。收缩后取不到锚词就**拒绝**（宁可空着）。

    校验（全部当场做，拒绝即计数，不静默）：
      ① 行号必须在 `[0, len(lines))`
      ② 拼出的值非空
      ③ 值不是学到的页面常量（表头/栏目名不该变成属性值）
      ④ 长度不超过 `L1_MAX_VALUE_CHARS`
    """
    if not idxs:
        return None, "no_lines"
    for j in idxs:
        if not (0 <= j < len(lines)):
            return None, "line_out_of_range"
    picked = sorted(set(idxs))
    val = "".join((lines[j].get("text") or "").strip() for j in picked)
    if not val:
        return None, "empty_value"
    if refine and attr:
        tight = RS.extract_one(attr, val, spec)
        if not tight:
            return None, "no_anchor_in_region"
        val = tight
    if len(val) > L1_MAX_VALUE_CHARS:
        return None, "too_long"
    if RS.norm_stream(val) in set(spec.get("page_constants") or []):
        return None, "page_constant"
    return val, ""


def _order_record(rec: dict, spec: dict) -> dict:
    """按骨架属性序重排 + 必填键兜底 —— 与 `rule_split.records_of` 口径一致。"""
    out: Dict[str, Any] = {}
    for a in (spec.get("attrs") or []):
        v = rec.get(a)
        if v:
            out[a] = v
    for k, v in rec.items():                     # 档案外的键（如「跨页」）保留
        if v and k not in out:
            out[k] = v
    for a in (spec.get("required") or []):
        out.setdefault(a, "")
    return out


def apply_l1(records: List[dict], lines: List[dict], parsed: dict, *,
             spec: dict, mode: str,
             refine: bool = True) -> Tuple[List[dict], dict]:
    """把模型的"行号答案"落到记录上。返回 `(新记录, 统计)`。

    **只填空、绝不覆盖**：目标属性已有非空值 → 拒绝（L0 的值优先）。
    `refine=True`：行号只用于定位，取值交给 `rule_split.extract_one` 收缩
    （见 `_slice_value`）。
    `mode=MODE_FULL` 时 `records` 为空，模型的输出直接构成记录。
    """
    allowed = set(fill_targets(spec))
    st: Dict[str, Any] = {
        "mode": mode, "n_filled": 0, "n_rejected": 0,
        "attrs_filled": {}, "reject_reasons": {},
        "rejected": [], "chars_added": 0,
    }

    def _reject(i, attr, reason, idxs):
        st["n_rejected"] += 1
        st["reject_reasons"][reason] = st["reject_reasons"].get(reason, 0) + 1
        if len(st["rejected"]) < 30:
            st["rejected"].append({"i": i, "attr": attr, "reason": reason,
                                   "lines": idxs[:12]})

    if mode == MODE_FULL:
        out: List[dict] = []
        for item in parsed.get("records") or []:
            rec: Dict[str, str] = {}
            for attr, idxs in (item.get("a") or {}).items():
                if attr not in allowed:
                    _reject(None, attr, "unknown_attr", idxs)
                    continue
                val, why = _slice_value(lines, idxs, spec, attr=attr,
                                        refine=refine)
                if val is None:
                    _reject(None, attr, why, idxs)
                    continue
                rec[attr] = val
            if not rec:
                _reject(None, "", "empty_record", [])
                continue
            out.append(_order_record(rec, spec))
        st["n_filled"] = sum(1 for r in out for a in allowed if r.get(a))
        st["attrs_filled"] = dict(Counter(
            a for r in out for a in allowed if r.get(a)))
        return out, st

    out = [dict(r) for r in records]
    for item in parsed.get("records") or []:
        i = item.get("i")
        if i is None or not (0 <= i < len(out)):
            for attr, idxs in (item.get("a") or {}).items():
                _reject(i, attr, "record_index_invalid", idxs)
            continue
        tgt = out[i]
        for attr, idxs in (item.get("a") or {}).items():
            if attr not in allowed:
                _reject(i, attr, "unknown_attr", idxs)
                continue
            if str(tgt.get(attr) or "").strip():
                _reject(i, attr, "already_filled", idxs)
                continue
            val, why = _slice_value(lines, idxs, spec, attr=attr, refine=refine)
            if val is None:
                _reject(i, attr, why, idxs)
                continue
            tgt[attr] = val
            st["n_filled"] += 1
            st["chars_added"] += len(val)
            st["attrs_filled"][attr] = st["attrs_filled"].get(attr, 0) + 1
    return [_order_record(r, spec) for r in out], st


# ============================================================
# 成本（取证口径）
# ============================================================
def prices_from_config(cfg: Optional[dict]) -> Tuple[float, float]:
    """从 llm 配置读参考单价（元/百万 tokens）；缺失或非法 → 内置默认。"""
    cfg = cfg or {}

    def _f(key, dflt):
        try:
            return float(cfg.get(key, dflt))
        except (TypeError, ValueError):
            return dflt

    return _f("price_in_per_m", DEFAULT_PRICE_IN_PER_M), \
        _f("price_out_per_m", DEFAULT_PRICE_OUT_PER_M)


def estimate_cost(usage: Optional[dict], price_in: float,
                  price_out: float) -> Optional[float]:
    """token 用量 → 参考金额（元）。无用量数据 → None（不编造）。"""
    if not usage:
        return None
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if pt is None and ct is None:
        return None
    return round(((pt or 0) * price_in + (ct or 0) * price_out) / 1e6, 6)


# ============================================================
# 页级入口
# ============================================================
def _gaps(records: List[dict], spec: dict) -> int:
    """还没被填上的槽位数（0 → 无事可做，直接跳过、零调用）。"""
    attrs = fill_targets(spec)
    return sum(1 for r in records for a in attrs
               if not str(r.get(a) or "").strip())


def _chat_compat(client: Any, messages: List[dict], kw: dict) -> Any:
    """调 `client.chat`，对"参数不被支持"**逐级降级**（只在 TypeError 时降）。

    真实 OpenAI 兼容客户端支持全部参数；老实现 / 测试替身可能只认一部分。
    降级只发生在**参数绑定阶段**（请求尚未发出），所以不会产生额外费用；
    真正的网络/API 错误不在这里吞掉，交给调用方的逐页隔离处理。
    """
    tries: List[dict] = [
        dict(kw, stream=False),
        dict(kw),
        {k: kw.get(k) for k in ("temperature", "timeout", "max_tokens")},
        {},
    ]
    last: Optional[TypeError] = None
    for t in tries:
        try:
            return client.chat(messages, **t)
        except TypeError as e:
            last = e
    raise last if last is not None else RuntimeError("chat 调用失败")


def l1_page(client: Any, *, lines: List[dict], records: List[dict],
            details: Optional[List[dict]] = None,
            profile: Optional[dict] = None, learned: Optional[dict] = None,
            spec: Optional[dict] = None,
            timeout: int = L1_TIMEOUT, max_tokens: int = L1_MAX_TOKENS,
            retries: int = L1_RETRIES, force_full: bool = False,
            reasoning_effort: Optional[str] = None,
            price_in: Optional[float] = None,
            price_out: Optional[float] = None) -> Dict[str, Any]:
    """L1 单页：L0 记录 + 行 → 补全后的记录。**异常不外抛**（调用方逐页隔离）。

    `force_full=True`：即使 L0 交出了记录也不补空，而是**让模型自己划记录**
    （用于 L0 的"整页兜底一条"页——那种页看似有记录，实则切分是错的，
    补空救不了它，只能重划）。

    Returns:
        `{records, details, parse_meta, usage, cost_cny, elapsed_s, skipped}`
    """
    t0 = time.time()
    spec = spec or RS.build_spec(learned, profile or {})
    if force_full:
        records = []
    mode = MODE_REPAIR if records else MODE_FULL
    pi = DEFAULT_PRICE_IN_PER_M if price_in is None else price_in
    po = DEFAULT_PRICE_OUT_PER_M if price_out is None else price_out
    meta: Dict[str, Any] = {"mode": mode, "n_lines": len(lines),
                            "n_in": len(records), "parse_ok": False,
                            "forced_full": bool(force_full)}

    # 零调用快路：L0 已经把每个骨架槽位都填上了 → 没有任何"未对齐段"
    if mode == MODE_REPAIR and _gaps(records, spec) == 0:
        meta.update({"reason": "no_gap", "n_gap": 0})
        return {"records": records, "details": details or [], "parse_meta": meta,
                "usage": None, "cost_cny": None, "elapsed_s": 0.0,
                "skipped": "no_gap", "raw_text": ""}

    prompt = build_l1_prompt(mode=mode, lines=lines, records=records,
                             details=details, spec=spec, profile=profile)
    messages = [{"role": "user", "content": prompt}]
    raw = ""
    last_err: Optional[Exception] = None
    kw: Dict[str, Any] = {"temperature": L1_TEMPERATURE, "timeout": timeout,
                          "max_tokens": max_tokens}
    eff = L1_REASONING_EFFORT if reasoning_effort is None else reasoning_effort
    if eff:
        kw["reasoning_effort"] = eff
    for _ in range(max(1, retries)):
        try:
            raw = "".join(_chat_compat(client, messages, kw))
        except Exception as e:           # noqa: BLE001 —— 逐页隔离，异常转成返回值
            last_err = e
            continue
        if raw.strip():
            break

    usage = getattr(client, "last_usage", None)
    meta.update({"prompt_chars": len(prompt), "raw_chars": len(raw)})
    if not raw.strip():
        meta.update({"reason": f"empty_output: {last_err}" if last_err
                     else "empty_output"})
        return {"records": records if mode == MODE_REPAIR else [],
                "details": details or [], "parse_meta": meta,
                "usage": usage, "cost_cny": estimate_cost(usage, pi, po),
                "elapsed_s": round(time.time() - t0, 1), "skipped": "empty",
                "raw_text": raw}

    parsed = parse_l1(raw)
    new_records, st = apply_l1(records, lines, parsed, spec=spec, mode=mode)
    meta.update({"parse_ok": parsed["parse_ok"],
                 "parse_reason": parsed["reason"],
                 "n_raw_records": parsed["n_raw"],
                 "n_bad_index": parsed["n_bad_index"],
                 "n_out": len(new_records), "apply": st,
                 "n_gap": _gaps(records, spec) if mode == MODE_REPAIR else None})
    return {"records": new_records, "details": details or [],
            "parse_meta": meta, "usage": usage,
            "cost_cny": estimate_cost(usage, pi, po),
            "elapsed_s": round(time.time() - t0, 1), "skipped": None,
            "raw_text": raw}


def summary(meta: dict) -> str:
    """一行摘要（日志/界面用）。"""
    if not meta:
        return "L1 未运行"
    if meta.get("reason") == "no_gap":
        return "L1 指针补全：L0 已填满，跳过（零调用）"
    ap = meta.get("apply") or {}
    return ("L1 指针补全（%s）：补 %s 项、拒 %s 项；记录 %s → %s"
            % (meta.get("mode"), ap.get("n_filled", 0), ap.get("n_rejected", 0),
               meta.get("n_in"), meta.get("n_out")))
