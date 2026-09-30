# -*- coding: utf-8 -*-
"""方案 A 阶段 2：文本 LLM 记录拆分（2026-09-06，P0-1）。

输入阶段 1 的整页转写 page_text（vlm_reader mode="transcribe" 产物），
由线上文本 LLM（DeepSeek 小调用量）按档案契约拆分为结构化 records。

设计依据（彭系统实证 + 四页金标准实测）：
- 2B 直读结构化耦合单次推理 → 契约退化（属性名/值 通用对、跨页全缺），
  字段召回 0.36；文本 LLM 契约遵循远强于 2B，拆分只做"切分+归位"，
  不做"认字"——两阶段各用所长
- 属性值照录转写原文（保持原字形，不做繁简转换）——繁简对齐由
  对账/评估侧折叠（reconciler._norm_chars / gold_eval._sim）

纪律：拆分不归纳（宁缺勿猜——转写里没有的信息不许编）；全 mock 可测。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import vlm_reader as vr

log = logging.getLogger("split_records")

DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT = 180
# thinking 模型 reasoning 预算不可控（effort=low 在密集页实测仍耗 8194
# tokens），content JSON 本身约 2-4k —— max_tokens 须留足双份
#
# 2026-09-11 取证上调 16384 → 32768：探针实测 0009 / 0003 两页**都在**
# budget=16384 上 `finish_reason=length` 截断，扩档到 32768 才成功（2/2）。
# 截断不是"差一点"，而是**整次调用白付**（reasoning 吃满、content 为空），
# 所以宁可一开始就给够：cap 只是上限，按实际生成量计费，抬高 cap 不额外花钱，
# 反而省掉那次注定失败的付费调用。65536 的上限继续兜极密集页。
DEFAULT_MAX_TOKENS = 32768
# 截断后的扩档上限（不设死上限会无限烧额度；DeepSeek 系 output 上限 64K）
MAX_TOKENS_CEILING = 65536


def _render_rules_block(rules: Optional[Dict[str, Any]]) -> str:
    """已学规则 → 提示词注入段（`rules` 为空 → 空串，prompt 逐字不变）。

    **为什么这段存在**（数据飞轮第二段的消费口，2026-09-11）：
    用户对「金标准页」的定位是**例题**——教会系统"这一类图该怎么读"，
    而不是逐页核对。例题里学到的规则必须**参与识别**（事前），
    而不是只做事后校验，否则飞轮的主轴是断的。
    """
    if not rules:
        return ""
    lines: List[str] = []
    order = rules.get("attr_order") or []
    if order:
        lines.append("- 记录属性顺序（阅读序）：" + " → ".join(order))
    if rules.get("anchor"):
        lines.append(f"- 记录边界：出现「{rules['anchor']}」即为**一条新记录的开头**"
                     f"（一条记录可跨若干竖列，边界由锚属性标记）")
    if rules.get("required"):
        lines.append("- 每条记录**至少**应含这些属性（缺失填 \"\"，但键不能少）："
                     + "、".join(rules["required"]))
    if rules.get("page_constants"):
        lines.append("- 本页页眉/栏目名（**不属任何记录**，不要拆进 records）："
                     + "、".join(f"「{c}」" for c in rules["page_constants"]))
    if rules.get("continuation"):
        lines.append("- 跨框续接：" + str(rules["continuation"]))
        for ex in (rules.get("continuation_examples") or [])[:4]:
            lines.append(f"    例：{ex}")
    ex_map = rules.get("value_examples") or {}
    if ex_map:
        lines.append("- 值示例（示意**完整值**的样子，不是要求照抄）：")
        for a, vs in list(ex_map.items())[:10]:
            if vs:
                lines.append(f"    {a}：" + "／".join(vs))
    for h in (rules.get("pattern_hints") or []):
        lines.append(f"- 形制线索：{h}")
    if not lines:
        return ""
    return ("\n已从既往**例题**（用户标注的少量样本）中学到的规则——"
            "与上文冲突时**以本节为准**：\n" + "\n".join(lines) + "\n")


def build_split_prompt(profile: Dict[str, Any], page_text: str,
                       rules: Optional[Dict[str, Any]] = None) -> str:
    """档案契约 + 转写文本 → 拆分 prompt（条件化发生的位置）。

    `rules`（飞轮学到的规则，见 `rule_learn.as_prompt_rules`）为空时
    输出与既有版本**逐字相同**（接通类改动的头号判据：零影响）。
    """
    attrs = profile.get("attrs", [])
    attr_lines = []
    for a in attrs:
        if isinstance(a, str):
            a = {"name": a, "desc": ""}
        line = f"- {a['name']}"
        if a.get("desc"):
            line += f"：{a['desc']}"
        attr_lines.append(line)
    attr_block = "\n".join(attr_lines) if attr_lines else "- （档案未定义属性）"
    name = profile.get("name", "未命名档案")
    desc = profile.get("description", "")
    rules_block = _render_rules_block(rules)

    return f"""你是历史文献结构化助手。下面是一页文献的整页转写文本，请把它拆分为结构化记录。

文献：{name}
{('说明：' + desc) if desc else ''}

本文献的记录包含以下属性（属性顺序即常见行文顺序）：
{attr_block}

拆分规则（严格遵守）：
1. 只输出一个 JSON 对象，不要输出任何解释文字或代码围栏
2. 格式：{{"records": [{{"属性名": "值", ...}}, ...]}}，按阅读顺序排列
3. 属性名必须与上面清单完全一致；该条缺失的属性填 ""
4. 属性值逐字照录转写文本原文（保持原字形，禁止繁简转换、换算或改写）
5. 转写文本里没有的信息填 ""，禁止编造；无法确定归属的内容宁可不拆
6. 页眉页脚、栏目名等非记录内容不要拆进 records
7. 跨页截断：某条记录未完、延续到下一页 → 该记录加 "跨页":"接下页"；
   开头是上一页末条的延续 → 拆成一条只填本页可见属性，加 "跨页":"承上页"；
   无跨页情况不要加该键
{rules_block}
整页转写文本：
<<<PAGE_TEXT
{page_text}
PAGE_TEXT>>>"""


def _is_length_truncation(exc: Exception) -> bool:
    """异常是否为「max_tokens 被 reasoning 吃满」——可扩档重试，非故障。"""
    msg = str(exc)
    return "finish_reason=length" in msg or "max_tokens 截断" in msg


def split_page(client: Any, page_text: str, profile: Dict[str, Any],
               temperature: float = DEFAULT_TEMPERATURE,
               timeout: int = DEFAULT_TIMEOUT,
               max_tokens: int = DEFAULT_MAX_TOKENS,
               retries: int = 2,
               rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """转写文本 → records（复用 vlm_reader.parse_records 容错解析）。

    批处理可靠性（2026-09-06 四页实跑教训）：
    - 一律非流式（stream=False）——本地代理掐 >60s 的 SSE 长连接，
      流式中途静默断流 content 为空且无异常（0002 空/0003 截断实证）；
    - 空输出重试 retries 次（瞬时故障），仍空则抛错——宁报错不静默
      产 0 记录页（0 记录页会伪装成"没拆出东西"污染对账与待核队列）。

    **2026-09-11 批量实跑教训**：`max_tokens` 截断不是"瞬时故障"——
    同一页连试两次会**稳定复现**（reasoning 吃满 16383/16382，content 空），
    重试同样的预算必然再失败，于是整批在第 4 页抛错中断。
    故截断**单独扩档**：预算 ×2（上限 `MAX_TOKENS_CEILING`），
    **不消耗** `retries` 的故障重试次数。

    `rules`：飞轮从例题学到的规则（`rule_learn.as_prompt_rules` 产物）→
    注入 prompt，让规则**参与识别**而非只做事后校验；None → 与既有行为逐字相同。

    returns {records, raw_text, parse_meta, elapsed_s, attempts, max_tokens_used}
    """
    prompt = build_split_prompt(profile, page_text, rules)
    messages = [{"role": "user", "content": prompt}]
    t0 = time.time()
    last_err: Optional[Exception] = None
    budget = max_tokens
    attempt = 0
    fails = 0
    while fails < retries:
        attempt += 1
        try:
            chunks = client.chat(messages, temperature=temperature,
                                 timeout=timeout, max_tokens=budget,
                                 stream=False)
            raw = "".join(chunks)
        except TypeError:
            # 客户端不支持 stream 参数（如非 OpenAI 兼容实现）→ 退回默认
            chunks = client.chat(messages, temperature=temperature,
                                 timeout=timeout, max_tokens=budget)
            raw = "".join(chunks)
        except Exception as e:          # 瞬时网络/限流故障 → 重试（截断除外）
            last_err = e
            if _is_length_truncation(e) and budget < MAX_TOKENS_CEILING:
                budget = min(budget * 2, MAX_TOKENS_CEILING)
                log.warning("[split_records] max_tokens 截断 → 扩档至 %d 重试", budget)
                continue                # 不计入 fails：这是预算不足，不是故障
            fails += 1
            log.warning("[split_records] 第 %d 次调用失败: %s", attempt, e)
            continue
        if raw.strip():
            elapsed = round(time.time() - t0, 1)
            records, parse_meta = vr.parse_records(raw)
            log.info("[split_records] elapsed=%ss attempts=%d budget=%d parse_ok=%s n=%s",
                     elapsed, attempt, budget, parse_meta["parse_ok"],
                     parse_meta.get("n_records", 0))
            return {"records": records, "raw_text": raw,
                    "parse_meta": parse_meta, "elapsed_s": elapsed,
                    "attempts": attempt, "max_tokens_used": budget}
        last_err = RuntimeError("空输出（content 为空）")
        fails += 1
        log.warning("[split_records] 第 %d 次调用空输出", attempt)
    raise ValueError(f"拆分调用 {attempt} 次均失败: {last_err}")


def extract_page_text(read_out: Dict[str, Any]) -> Optional[str]:
    """阶段 1 read_page(mode="transcribe") 产物 → page_text（缺失返 None）。"""
    extras = (read_out.get("parse_meta") or {}).get("top_extras") or {}
    pt = extras.get("page_text")
    return pt if isinstance(pt, str) and pt.strip() else None
