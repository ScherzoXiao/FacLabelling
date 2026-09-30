# -*- coding: utf-8 -*-
"""S1 轨道 A：VLM 直读模块（复杂版面方案 §4.2，阶段二 2a 前置，2026-09-04）。

本地/线上多模态模型读整页图 → 行 JSON 候选。本模块只做三件事：
    1. prepare_image      分辨率档位（PIL 缩略，控视觉 token——2B CPU 推理
                          全页原图 2060 视觉 token / 265s，缩档直接省时间）
    2. build_reading_prompt  版面先验注入（档案属性集 → prompt 条件化，
                          这是"档案驱动识别"发生的位置，§4.2 S0/S1 衔接）
    3. parse_records      json_repair 风格容错解析（本地轻实现，零依赖——
                          方案 §4.4(b) 明确不引入 json_repair 包）

图像传参：Ollama 原生 /api/chat 的 message.images（base64）——经
OllamaClient.chat() 的 messages **原样透传**（_do_chat 不清洗 messages），
llm_client.py 零改动。线上多模态档（2b）接入时另做适配层。

2B 已知缺陷对策（§4.2）：temperature 默认 0.6（贪心采样无限循环规避）；
timeout 默认 600s（CPU 全页直读实测 265s，须留余量）。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

log = logging.getLogger("vlm_reader")

# 分辨率档位（最长边像素；大字印刷 1400 ≈ 150-200dpi 可读档，
# 部署冒烟原图 1895×1806 → 2060 视觉 token，缩档预期显著降 token/耗时）
DEFAULT_MAX_SIDE = 1400
# 2B 温度下限（贪心循环规避，§4.2）；线上档不受此约束
DEFAULT_TEMPERATURE = 0.6
DEFAULT_TIMEOUT = 600


# ============================================
# 1. 图像准备（分辨率档位）
# ============================================
def prepare_image(image_path: str, max_side: int = DEFAULT_MAX_SIDE) -> Tuple[str, Dict[str, Any]]:
    """读图 → 等比缩到最长边 max_side → PNG base64。

    returns (b64_str, meta)：meta 含原始/输出尺寸（观测视觉 token 与档位效果）。
    缩略只降采样、不放大（小图原样转码）；PNG 无损（史料文字保真优先）。
    """
    with Image.open(image_path) as im:
        w, h = im.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            im = im.resize((round(w * scale), round(h * scale)),
                           Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="PNG")
        out_w, out_h = im.size
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, {"orig": [w, h], "sent": [out_w, out_h],
                 "max_side": max_side,
                 "b64_kb": round(len(b64) / 1024, 1)}


# ============================================
# 2. 直读 prompt（版面先验注入）
# ============================================
def build_reading_prompt(profile: Dict[str, Any],
                         contract: str = "line_json") -> str:
    """档案属性集 → 直读 prompt（"条件化"发生的位置，§4.2）。

    profile: profile_store 档案 dict（name / description / attrs=[{name,desc}]）
    contract: 目前仅 line_json（{"records":[...]}）；表格 HTML 契约 2a 迭代再开
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

    return f"""你是历史文献整理助手。请读取整页图像，输出结构化记录。

本页所属文献类型档案：{name}
{('档案说明：' + desc) if desc else ''}

页面通常包含以下属性（理解版面时参照，属性顺序即常见版面顺序）：
{attr_block}

输出要求（严格遵守）：
1. 只输出一个 JSON 对象，不要输出任何解释文字或代码围栏
2. 格式：{{"records": [{{"属性名": "值", ...}}, ...]}}
3. records 按页面阅读顺序排列（竖排文字按右→左列序），每条记录一个对象
4. 属性名必须与上面清单完全一致；该记录缺失的属性填 ""
5. 数字、金额、纪年逐字照录原文，禁止换算或改写
6. 残缺/无法辨认的字用 □ 占位，不要猜字
7. 跨页截断处理：若本页末尾某条记录未完、延续到下一页，照常输出该记录已有
   属性，并在该记录中加 "跨页":"接下页"；若本页开头是上一页末条记录的延续，
   将延续内容输出为一条记录，只填本页可见的属性，并加 "跨页":"承上页"。
   无跨页情况的记录不要加该键"""


# ============================================
# 2b. 整页转写 prompt（方案 A 阶段 1：只转写不结构化，2026-09-06）
# ============================================
def build_transcribe_prompt(profile: Dict[str, Any]) -> str:
    """整页逐字转写 prompt（方案 §4.7(f) 两阶段：2B 强项=转录，
    结构化外包给文本 LLM——彭系统实证两阶段覆盖=端到端 2 倍）。"""
    name = profile.get("name", "未命名档案")
    desc = profile.get("description", "")
    return f"""你是历史文献整理助手。请对整页图像做逐字转写。

本页所属文献：{name}
{('文献说明：' + desc) if desc else ''}

转写要求（严格遵守）：
1. 只输出一个 JSON 对象，不要输出任何解释文字或代码围栏
2. 格式：{{"page_text": "整页转写文本"}}
3. 竖排文字按右→左列序、列内自上而下转写；列与列之间用换行分隔
4. 数字、金额、纪年逐字照录原文，禁止换算或改写
5. 残缺/无法辨认的字用 □ 占位，不要猜字
6. 页眉、页脚与表格内全部文字都要转写，不要遗漏、不要概括、不要添加"""


# ============================================
# 3. 容错解析（json_repair 风格，本地轻实现）
# ============================================
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _light_repair(s: str) -> str:
    """轻量修复：全角引号/冒号/逗号 → 半角；去对象尾逗号。不追求完备。"""
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace("：", ":").replace("，", ",")
    s = re.sub(r",\s*([}\]])", r"\1", s)      # 尾逗号
    return s


def _find_outer_json(s: str) -> Optional[str]:
    """第一个 { 到与之配对的 }（计数配对，容忍字符串内花括号粗略处理）。"""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None   # 括号不配对 → 交给调用方判失败


def parse_records(raw_text: str) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """模型原始输出 → (records, parse_meta)。

    容错梯度：原样 loads → 剥围栏 → 提取最外层 JSON → 轻修复重试。
    parse_ok=False 时 records 为空，raw_text 保留供人工/重试（§4.4(b)
    失败仅重试出错字段——那是 S3 层的事，本层只报告）。
    """
    meta: Dict[str, Any] = {"parse_ok": False, "strategy": None,
                            "raw_len": len(raw_text or "")}
    if not raw_text or not raw_text.strip():
        return [], meta

    candidates = [raw_text.strip()]
    fence = _FENCE_RE.search(raw_text)
    if fence:
        candidates.append(fence.group(1).strip())
    outer = _find_outer_json(raw_text)
    if outer:
        candidates.append(outer)
    if fence:
        outer2 = _find_outer_json(fence.group(1))
        if outer2:
            candidates.append(outer2)
    candidates += [_light_repair(c) for c in list(candidates)]

    for i, cand in enumerate(candidates):
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        records = obj.get("records") if isinstance(obj, dict) else obj
        if isinstance(records, list):
            clean = [r for r in records if isinstance(r, dict)]
            # 顶层附加字段透传（如对账用 page_text 整页转写，1c/2a 消费）
            extras = ({k: v for k, v in obj.items() if k != "records"}
                      if isinstance(obj, dict) else {})
            meta.update(parse_ok=True, strategy=f"strategy_{i}",
                        n_records=len(clean),
                        n_dropped=len(records) - len(clean),
                        top_extras=extras)
            return clean, meta
        if isinstance(obj, dict) and obj:
            # 无 records 键的非空顶层对象（transcribe 模式 {"page_text":...}）：
            # 记 parse 成功、records 恒空，顶层键走 extras（方案 A 阶段 1）
            meta.update(parse_ok=True, strategy="extras_only",
                        n_records=0, n_dropped=0, top_extras=dict(obj))
            return [], meta

    meta["strategy"] = "all_failed"
    return [], meta


# ============================================
# 4. 直读入口
# ============================================
def read_page(client: Any, image_path: str, profile: Dict[str, Any],
              max_side: int = DEFAULT_MAX_SIDE,
              temperature: float = DEFAULT_TEMPERATURE,
              timeout: int = DEFAULT_TIMEOUT,
              max_tokens: int = 4096,
              extra_hint: str = "",
              response_format: Optional[Dict[str, str]] = None,
              mode: str = "records") -> Dict[str, Any]:
    """单页直读：缩略 → prompt（属性先验）→ messages(images 透传) → 解析。

    client: OllamaClient（本地 2B）或具备同签名 chat() 的线上适配对象。
    response_format: {"type":"json_object"} → Ollama JSON 模式（首航实测：
    多公司整页直读不约束 JSON 时尾段复读致 4096 截断、解析失败——
    2a 首航 2026-09-05 结论，本地 2B 一律建议开启）。
    mode: "records"（默认，结构化直读）| "transcribe"（方案 A 阶段 1，
    整页转写——page_text 在 parse_meta.top_extras，records 恒空）。
    returns {records, raw_text, parse_meta, image_meta, elapsed_s}
    """
    import time
    b64, img_meta = prepare_image(image_path, max_side=max_side)
    prompt = (build_transcribe_prompt(profile) if mode == "transcribe"
              else build_reading_prompt(profile))
    if extra_hint:
        prompt += "\n\n补充提示：" + extra_hint
    messages = [{
        "role": "user",
        "content": prompt,
        "images": [b64],          # Ollama 原生透传（OllamaClient 不清洗）
    }]
    t0 = time.time()
    chunks = client.chat(messages, temperature=temperature,
                         timeout=timeout, max_tokens=max_tokens,
                         response_format=response_format)
    raw = "".join(chunks)
    elapsed = round(time.time() - t0, 1)
    records, parse_meta = parse_records(raw)
    log.info("[vlm_reader] %s elapsed=%ss parse_ok=%s n=%s",
             Path(image_path).name, elapsed, parse_meta["parse_ok"],
             parse_meta.get("n_records", 0))
    return {"records": records, "raw_text": raw, "parse_meta": parse_meta,
            "image_meta": img_meta, "elapsed_s": elapsed}
