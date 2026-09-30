"""阅读序重排器（Phase 1，2026-08-20；Phase 1.6 内容类型感知，2026-08-20）。

背景：PaddleOCR-VL 按栏（列）扫描输出，学术导出的行级 xlsx 对人类读者不友好；
且输入图异构（竖排古籍 / 横排手写书信 / 名单表格 / 混合版面），
单一确定性算法无法覆盖全部版面。本模块引入"规则兜底 + LLM 补位"双轨重排。

策略（双轨 + 内容类型感知）：
- 版面分类器 classify_layout()：
    - vertical   框占满页高且横向不宽 → 竖排（栏右→左、栏内上→下）→ 规则重排
    - horizontal 框占满页宽 → 横排（行上→下、行内左→右）→ 规则重排
    - ambiguous  手写潦草 / 名单 / 混合版面 → LLM 重排
- 内容类型分类器 classify_content_type() + llm_classify_content_type()（Phase 1.6）：
    先判断图片里是什么**类型的信息**，再决定怎么导出可读文本：
    - prose / letter / document → 阅读序重排（现有双轨）
    - roster（名录/名单：进士录、职官表、题名碑等）→ 条目化重组 llm_reassemble_roster()
      （LLM 把 VL 逐字/短词拆行的零散字词按语义聚合成「人名＋科年/籍贯/官职」条目；
      硬约束字符集合不变 + line_ids 全覆盖，复用 verify_multiset/_verify_coverage 校验）
    规则先给候选（可解释、零成本），LLM 做最终裁决（用户核心诉求：AI 判断图片信息类型）。
- LLM 重排（llm_client 接入 deepseek-v4-flash）：硬约束"只重排、不改字、不补字、不删字"，
  response_format=json_object 强制结构化输出。
- 分块重排 llm_reassemble_chunked()：大图按栏排序后切 ≤12 行/块，逐块重排再合并。
  实测（2026-08-20）：未分块时 V4-Flash thinking 模型的 reasoning 会吃掉全部 max_tokens
  预算（reasoning 甚至随预算膨胀），导致 content 空串/截断；分块 + max_tokens=16384 +
  temperature=0 后 10 行与 79 行图均全量通过字符多集校验（diff=0）。
- 名录条目化重组 llm_reassemble_roster()（Phase 1.6 实测，2026-08-20）：roster 聚合任务
  （在大量 1-3 字短词间建立长距离语义联系）比 prose 重排更易触发 reasoning 膨胀——
  28/30+ 次调用中绝大多数 max_tokens=16384 全被 reasoning 吃光、content 空串，仅约 1/5
  概率收敛。有效方案：strong 指引 prompt（"按 id 升序只读一遍快速分组、不反复检查归属、
  相邻 2-4 字可组人名直接成条、不做古籍考据只做分组"）+ max_tokens=32768 + timeout=300
  （实测 24 行 169s 成功输出 8 个可读条目）。此方案已固化到 ROSTER_SYSTEM_PROMPT 与
  llm_reassemble_roster 默认参数。
- 字符多集校验 verify_multiset()：重排前后字符集合必须逐字一致；
  不一致（模型动了字）→ 段标记 needs_review，不进学术导出的已验证视图。
- 片段覆盖校验 _verify_coverage()：LLM 输出的 line_ids 集合必须 == 输入片段编号集合；
  对"上同/同上"这类重复文字，字符多集可能漏检，line_id 全局唯一可精确发现遗漏/重复。
- L4 落盘（data/reassembled/<stem>.json）：缓存 + 出处（方法/模型/提示词版本/时间/来源 line_ids）。

设计边界：
- canonical（data/structured/*.json）只读不写；本模块产物是衍生视图。
- 校勘优先：行文本优先取主工作簿 corrected_text（人工修正），无则用 OCR 原文。
- 学术纪律：LLM 输出若与输入字符集不一致，一律降级为 needs_review，绝不静默入库。

CLI 用法：
    python reassembler.py --project-id 测试用归类栏目_0e8aba
    python reassembler.py --image-stems a b --force-llm
    python reassembler.py --xlsx output.xlsx   # 带人工修正优先
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import template_store  # M8b：用户样本模板（few-shot 注入）

log = logging.getLogger("reassembler")

SCHEMA_VERSION = 1
PROMPT_VERSION = 1

# 默认目录（与 exporter.py / rag.py 对齐）
DEFAULT_STRUCTURED_DIR = Path("data/structured")
DEFAULT_OUT_DIR = Path("data/reassembled")
DEFAULT_LLM_CONFIG = Path("data/llm_config.json")

# LLM 硬约束提示词（学术数据纪律：只重排，绝不改字）
REASSEMBLE_SYSTEM_PROMPT = (
    "你是古籍/文书版面阅读序重排助手。系统会给你 OCR 按栏（列）扫描输出的文字片段，"
    "每个片段带编号、栏序、行序和像素坐标。\n"
    "任务：把这些片段重排成人类可读的阅读顺序。竖排古籍：栏从右往左、栏内自上而下；"
    "横排文书：行自上而下、行内从左往右；名单表格：按表格行列自然顺序。\n"
    "硬性约束（学术数据，必须逐字遵守）：\n"
    "1. **只允许改变顺序，绝不允许增、删、改、补任何字符**；繁简、异体字保持原样，标点原样保留。\n"
    "2. 若某片段内部字序需要颠倒（如一行内倒置），可以颠倒该片段内的字符顺序，"
    "但整页字符集合仍必须与输入完全一致。\n"
    "3. 每个片段编号必须且只能被使用一次：line_ids 必须完整列出构成该段的全部片段编号，"
    "不得遗漏、不得重复、不得编造。\n"
    "4. 不要猜测、不要修正疑似 OCR 错字、不要添加任何输入中没有的字。\n"
    "5. 只输出 JSON，不要任何其他文字：\n"
    '{"segments": [{"text": "<重排后的连续文本>", "line_ids": [<构成该段的输入片段编号>]}]}\n'
    "6. segments 数量 1-8 段，按阅读顺序排列；段内文本应为完整通顺的语句。"
)

# 内容类型全集（Phase 1.6：AI 先判断图片信息类型，再决定重组/导出方式）
CONTENT_TYPES = ("prose", "roster", "table", "letter", "document", "mixed", "ambiguous")

# 信函套语（规则初判 letter 用；命中 ≥2 且页规模不大 → letter）
_LETTER_MARKS = (
    "敬啟", "敬启", "台鑒", "台鉴", "鈞鑒", "钧鉴", "頓首", "顿首",
    "此致", "再拜", "先生", "足下", "大鑒", "大鉴", "啟者", "启者",
    "遙寄", "遥寄", "拜讀", "拜读", "久仰", "玉照", "謹啟", "谨启",
    "閣下", "阁下", "鑒察", "鉴察",
)

# 名录条目化重组允许"输出多出"的格式分隔标点（模型按示例格式补的逗号等，
# 属可读性增强而非改字；只放宽 extra、不放宽 missing——删实义字仍判失败）
ROSTER_FORMAT_CHARS = {"，", ",", "、", "。", "．", "."}

# 名录（roster）条目化重组提示词（Phase 1.6）：
# 与 prose 重排的本质区别——prose 只重排"片段顺序"，roster 允许 LLM 把零散字词
# 跨片段聚合成"条目"（人名+科年/籍贯/官职），但仍遵守字符集合逐字不变 + line_ids 全覆盖。
ROSTER_SYSTEM_PROMPT = (
    "你是古籍名录/名单整理助手。原图是名录（如进士录、职官表、人名册、题名碑），"
    "OCR 按列扫描后把内容拆成了零散的字词片段，每个片段带编号、栏序、行序和坐标。\n"
    "任务：把这些零散字词重组成**可读的名录条目**，例如「人名＋科年／籍贯／官职」。\n"
    "硬性约束（学术数据，必须逐字遵守）：\n"
    "1. **只允许改变字符的先后顺序与分组，绝不允许增、删、改、补任何字符**；繁简、异体字保持原样。\n"
    "2. 片段内部的字序可以按需调整（如人名应连写），但整页字符集合必须与输入完全一致。\n"
    "3. 每个片段编号必须且只能被使用一次：line_ids 必须完整列出构成该条目的全部片段编号，"
    "不得遗漏、不得重复、不得编造。\n"
    "4. 无法确定归属的字词不要凭空配对：宁可单独成条目或并入相邻条目，也不要猜测归属。\n"
    "5. 只输出 JSON，不要任何其他文字：\n"
    '{"segments": [{"text": "<一条完整的名录条目>", "line_ids": [<构成该条目的片段编号>]}]}\n'
    "6. segments 按原图阅读顺序排列；每条目应通顺可读（如「何守中，嘉靖甲子科进士」）。\n"
    "执行要点（务必遵守，直接决定成败）：\n"
    "a. 按 id 升序只读一遍，边读边直接分组，不要反复回头检查某个字词归属哪里；\n"
    "b. 相邻 2-4 个字能组成人名就直接成条（如「劉」「子」「鑠」→「劉子鑠」），"
    "后续短词（科年/籍贯/官职）就近并入该条；\n"
    "c. 不要分析古籍背景、不做考据，只做机械分组，一次输出完毕；\n"
    "d. 不要添加输入中没有的标点符号（示例中的逗号只是展示条目结构）；"
    "如确需分隔，用空格或直接连写；\n"
    "e. 每个片段编号必须恰好出现一次，宁可保守分组也不要编造归属。"
)

# 内容类型分类提示词（Phase 1.6：用户核心诉求——AI 先判断图片信息类型）
CLASSIFY_SYSTEM_PROMPT = (
    "你是古籍/历史文献内容类型分类助手。系统会给你一张 OCR 图片的文字统计特征与样本。\n"
    "任务：判断这张图片的内容属于什么类型，从以下选择一个：\n"
    "- prose：连贯文章/叙事文本/志文/记叙（成句成段、语句通顺）\n"
    "- roster：名录/名单（进士录、职官表、人名册、题名碑等；条目=人名+科年/籍贯/官职）\n"
    "- table：表格（行列规整的数据表）\n"
    "- letter：信札/书信（含称谓、问候、落款等信函格式）\n"
    "- document：文书/公文/告示（有固定文首文尾格式）\n"
    "- mixed：混合类型\n"
    "- ambiguous：无法判断\n"
    "只输出 JSON，不要任何其他文字："
    '{"content_type": "<类型>", "reason": "<一句话理由>"}'
)

_LAYOUT_HINTS = {
    "vertical": "版面为竖排（文字框占满页高）：栏从右往左，栏内自上而下。",
    "horizontal": "版面为横排（文字框占满页宽）：行自上而下，行内从左往右。",
    "ambiguous": "版面类型不明确（可能是手写、表格或混合版面）：请根据坐标推断自然阅读顺序。",
}


# ============================================
# 1. box 工具
# ============================================
def box_of(line: dict) -> Optional[Tuple[float, float, float, float]]:
    """L2 行 box（4 点 [[x,y]x4]）→ (x0, y0, x1, y1)；无效返 None。"""
    box = line.get("box")
    if not isinstance(box, list) or len(box) < 2:
        return None
    try:
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        return (min(xs), min(ys), max(xs), max(ys))
    except (TypeError, ValueError, IndexError):
        return None


def _text_of(line: dict, corrected_map: Optional[Dict[int, str]] = None) -> str:
    """行文本：人工修正优先（主工作簿 corrected_text），拒绝/无修正则用 OCR 原文。

    修正经 safe_corrected 粒度校验（L2 与主工作簿行粒度可能不一致，防止 xlsx_id 错位
    把别行的修正拼进本行——2026-08-20 实测 12 图 L2 行数与主工作簿行数全部不一致）。

    ✅ P-K9 阶段 0d（2026-09-10）口径统一：safe_corrected 判定行粒度错位而**拒绝**时，
    旧实现返回空串，等于静默抹掉该行文本。推演影响面（按内容类型）：
      * ledger：detect_ledger 靠"行含銀衡单位字/收付方向字"计数，抹行会让账簿页
        跌回 prose 路径被切碎；split_ledger_entries 以"含金额行"flush 账目，
        抹掉金额行会导致条目丢失或粘连；
      * roster：该行不进 LLM payload，人名/科年静默消失；
      * prose：整段少字，且"字数"类回答随之偏小。
    更隐蔽的是**静默性**：字符多集校验的两侧（input_texts 与 segment text）同源于
    本函数，一起少字仍判 matched → 单元照样标 verified，问题不暴露。
    校正被拒的语义本就是"错位、不可安全套用"，正确动作是**保留原文**（等价于该行
    未校对），与 corrected_text.apply_correction 的口径一致——不因校验失败而删内容。
    """
    raw = str(line.get("text", ""))
    if corrected_map:
        xid = line.get("xlsx_id")
        if xid is not None:
            try:
                mapped = corrected_map.get(int(xid))
            except (TypeError, ValueError):
                mapped = None
            if mapped:
                from exporter import safe_corrected
                safe = safe_corrected(raw, str(mapped))
                if safe:
                    return safe
                # safe_corrected 返回空串 = 判定错位、拒绝套用 → 回落原文（绝不抹行）
    return raw


# ============================================
# 2. 版面分类器
# ============================================
def classify_layout(lines: List[dict]) -> str:
    """判定版面类型：vertical / horizontal / ambiguous。

    判据（基于 box 几何）：
    - vertical：≥60% 的框高 ≥ 页高一半（竖栏），且 <30% 的框宽 ≥ 页宽一半
    - horizontal：≥60% 的框宽 ≥ 页宽一半（整行），且 <30% 的框高 ≥ 页高一半
    - 其余（表格小格、潦草手写、混合）→ ambiguous
    """
    boxes = [b for b in (box_of(ln) for ln in lines if isinstance(ln, dict)) if b]
    if not boxes:
        return "ambiguous"
    W = max(b[2] for b in boxes)
    H = max(b[3] for b in boxes)
    if W <= 0 or H <= 0:
        return "ambiguous"
    n = len(boxes)
    tall = sum(1 for b in boxes if (b[3] - b[1]) >= 0.5 * H)
    wide = sum(1 for b in boxes if (b[2] - b[0]) >= 0.5 * W)
    if tall / n >= 0.6 and wide / n < 0.3:
        return "vertical"
    if wide / n >= 0.6 and tall / n < 0.3:
        return "horizontal"
    return "ambiguous"


# ============================================
# 2b. 内容类型分类器（Phase 1.6）
#     规则先给候选（可解释、零成本），LLM 做最终裁决
# ============================================
def _text_stats(lines: List[dict]) -> dict:
    """L2 行文本统计特征（分类器输入）。"""
    texts = [str(ln.get("text", "")) for ln in lines if isinstance(ln, dict)]
    n = len(texts)
    if n == 0:
        return {"n": 0, "avg_len": 0.0, "short_ratio": 0.0, "nian_rows": 0, "joined": "", "sample": []}
    lens = [len(t) for t in texts]
    return {
        "n": n,
        "avg_len": sum(lens) / n,
        "short_ratio": sum(1 for L in lens if L <= 3) / n,
        "nian_rows": sum(1 for t in texts if "年" in t),
        "joined": "".join(texts),
        "sample": texts[:12],
    }


def classify_content_type(lines: List[dict]) -> str:
    """规则初判内容类型：prose / roster / letter / ambiguous（+table 由 LLM 细分）。

    判据（基于文本统计，2026-08-20 实测标定）：
    - letter：信函套语命中 ≥2 且页规模不大（≤40 行、行长 ≥6）——信札
    - roster：行数多且行短（≥30 行、平均 ≤4 字；或 ≥15 行、平均 ≤6 字且短行过半）
      ——VL 逐字/短词拆行的名录（实测 430 图 79 行×2 字、453 图 189 行×1.5 字）
    - prose：成句成段（≥8 行、平均 ≥8 字）——连贯文章/志文
    - 其余 → ambiguous（交 LLM 裁决，可能细分 table/mixed/document）
    """
    stats = _text_stats(lines)
    n, avg = stats["n"], stats["avg_len"]
    if n == 0:
        return "ambiguous"
    joined = stats["joined"]
    letter_hits = [w for w in _LETTER_MARKS if w in joined]
    if len(letter_hits) >= 2 and n <= 40 and avg >= 4:
        return "letter"
    if n >= 30 and avg <= 4:
        return "roster"
    if n >= 15 and avg <= 6 and stats["short_ratio"] >= 0.5:
        return "roster"
    if n >= 8 and avg >= 8:
        return "prose"
    return "ambiguous"


def build_classify_messages(lines: List[dict], image_name: str = "") -> List[dict]:
    """构造 LLM 内容类型分类 prompt（统计特征 + 文本样本，不传全文）。"""
    stats = _text_stats(lines)
    sample = "\n".join(f"[{i}] {t}" for i, t in enumerate(stats["sample"]))
    user_content = (
        f"图片：{image_name}\n"
        f"统计：行数={stats['n']}，平均行长={stats['avg_len']:.1f} 字，"
        f"短行(≤3字)占比={stats['short_ratio']:.0%}，含「年」行数={stats['nian_rows']}\n"
        f"文字样本（前 {len(stats['sample'])} 行）：\n{sample}\n\n"
        f"请判断内容类型并输出 JSON。"
    )
    return [
        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_classify_json(raw_text: str) -> dict:
    """解析 LLM 分类输出 JSON（兼容 ```json 包裹 / 前后杂文）。"""
    if not raw_text:
        return {"content_type": "ambiguous", "reason": "空响应", "_parse_ok": False}
    text = raw_text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        text = text[a:b + 1]
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"content_type": "ambiguous", "reason": "解析失败", "_parse_ok": False}
    ct = str(obj.get("content_type", "")).strip().lower()
    if ct not in CONTENT_TYPES:
        return {"content_type": "ambiguous", "reason": f"未知类型 {ct}", "_parse_ok": False}
    return {"content_type": ct, "reason": str(obj.get("reason", "")), "_parse_ok": True}


def llm_classify_content_type(
    client,
    lines: List[dict],
    image_name: str = "",
    timeout: int = 240,
) -> Tuple[str, str]:
    """LLM 内容类型裁决（deepseek-v4-flash）。返回 (content_type, reason)。

    - 只传统计特征 + 前 12 行样本（分类不需要全文，控制 payload 与 reasoning）
    - temperature=0 确定性输出；解析失败返回 ambiguous（调用方回落规则结果）
    """
    messages = build_classify_messages(lines, image_name)
    raw = "".join(client.chat(
        messages, stream=False, response_format={"type": "json_object"},
        max_tokens=2048, temperature=0.0, timeout=timeout,
    ))
    parsed = parse_classify_json(raw)
    return parsed["content_type"], parsed["reason"]


# ============================================
# 3. 确定性规则重排
# ============================================
def _segment(items: List[dict], corrected_map: Optional[Dict[int, str]], join_with: str = "") -> dict:
    """一组行 → 段（文本 + 来源 line_ids）。"""
    return {
        "text": join_with.join(_text_of(ln, corrected_map) for ln in items),
        "line_ids": [ln.get("line_id") for ln in items],
    }


def _reassemble_vertical(lines: List[dict], corrected_map: Optional[Dict[int, str]]) -> List[dict]:
    """竖排：栏右→左（x 中心降序），栏内上→下（y 中心升序）。

    无 box 退化路径：按 col_index（VL 扫描序，0=最右）分组、组内按 row_index。
    """
    if not any(box_of(ln) for ln in lines):
        cols: Dict[int, List[dict]] = {}
        for ln in lines:
            cols.setdefault(int(ln.get("col_index", 0)), []).append(ln)
        out = []
        for col in sorted(cols):
            items = sorted(cols[col], key=lambda ln: ln.get("row_index", 0))
            out.append(_segment(items, corrected_map))
        return out
    entries = []
    for ln in lines:
        b = box_of(ln)
        cx = (b[0] + b[2]) / 2
        cy = (b[1] + b[3]) / 2
        entries.append((cx, cy, ln))
    entries.sort(key=lambda t: (-t[0], t[1]))  # 右→左；同列上→下
    widths = [b[2] - b[0] for b in (box_of(ln) for ln in lines) if b]
    tol = 0.5 * (sum(widths) / len(widths)) if widths else 1.0
    groups: List[dict] = []
    for cx, cy, ln in entries:
        if groups and abs(groups[-1]["cx"] - cx) <= max(tol, 1.0):
            groups[-1]["items"].append((cy, ln))
        else:
            groups.append({"cx": cx, "items": [(cy, ln)]})
    out = []
    for g in groups:
        g["items"].sort(key=lambda t: t[0])
        out.append(_segment([ln for _, ln in g["items"]], corrected_map))
    return out


def _reassemble_horizontal(lines: List[dict], corrected_map: Optional[Dict[int, str]]) -> List[dict]:
    """横排：按 y 中心行带聚类（上→下），带内 x 中心升序（左→右）。"""
    entries = []
    for ln in lines:
        b = box_of(ln)
        if b:
            cx = (b[0] + b[2]) / 2
            cy = (b[1] + b[3]) / 2
        else:
            cx = float(ln.get("row_index", 0))
            cy = float(ln.get("row_index", 0))
        entries.append((cy, cx, ln))
    entries.sort(key=lambda t: (t[0], t[1]))
    heights = [b[3] - b[1] for b in (box_of(ln) for ln in lines) if b]
    tol = 0.6 * (sum(heights) / len(heights)) if heights else 1.0
    groups: List[dict] = []
    for cy, cx, ln in entries:
        if groups and abs(groups[-1]["cy"] - cy) <= max(tol, 1.0):
            groups[-1]["items"].append((cx, ln))
        else:
            groups.append({"cy": cy, "items": [(cx, ln)]})
    out = []
    for g in groups:
        g["items"].sort(key=lambda t: t[0])
        out.append(_segment([ln for _, ln in g["items"]], corrected_map, join_with=""))
    return out


def deterministic_reassemble(
    lines: List[dict],
    corrected_map: Optional[Dict[int, str]] = None,
    layout: str = "vertical",
) -> List[dict]:
    """确定性规则重排 → segments（带 seq/status）。"""
    corrected_map = corrected_map or {}
    if layout == "horizontal":
        segs = _reassemble_horizontal(lines, corrected_map)
    else:
        segs = _reassemble_vertical(lines, corrected_map)
    for i, s in enumerate(segs, start=1):
        s["seq"] = i
        s.setdefault("status", "ok")
    return segs


# ============================================
# 4. 字符多集校验（防幻觉的最后一道闸）
# ============================================
def _norm_chars(texts) -> str:
    """拼接并去除全部空白（换行/空格不属于"字"，不参与比对）；
    繁→简折叠（zhconv，缺库降级直通）——与 reconciler._norm_chars
    受控拷贝同步（2026-09-06 P0-2），两处语义必须一致。"""
    try:
        import zhconv
    except ImportError:            # 降级：不做繁简折叠
        zhconv = None
    parts = []
    for t in texts:
        s = "".join(t.split())
        if zhconv is not None and s:
            s = zhconv.convert(s, "zh-cn")
        parts.append(s)
    return "".join(parts)


def verify_multiset(input_texts: List[str], output_texts: List[str],
                    ignore_extra: Optional[set] = None) -> dict:
    """重排前后字符集合必须逐字一致。

    ignore_extra（roster 条目化重组专用）：允许"输出比输入多出"的格式标点集合
    （如人名与科年间的逗号——模型按示例格式补的分隔符属可读性增强，非改字）。
    只放宽 extra，绝不放宽 missing：模型删了实义字仍判失败。

    Returns:
        {"chars_in", "chars_out", "matched", "extra_chars", "missing_chars", "diff_total"}
        matched=False 表示模型增删改了字 → 该图必须 needs_review。
    """
    cin = _norm_chars(input_texts)
    cout = _norm_chars(output_texts)
    extra = Counter(cout) - Counter(cin)
    missing = Counter(cin) - Counter(cout)
    if ignore_extra and extra and not missing:
        extra = Counter({k: v for k, v in extra.items() if k not in ignore_extra})
    return {
        "chars_in": len(cin),
        "chars_out": len(cout),
        "matched": not extra and not missing,
        "extra_chars": dict(extra),
        "missing_chars": dict(missing),
        "diff_total": sum(extra.values()) + sum(missing.values()),
    }


# ============================================
# 5. LLM 重排（只重排不改字）
# ============================================
def build_reassemble_messages(
    lines_payload: List[dict],
    layout: str = "ambiguous",
    image_name: str = "",
) -> List[dict]:
    """构造 LLM 重排 prompt（OpenAI 风格 messages）。"""
    hint = _LAYOUT_HINTS.get(layout, _LAYOUT_HINTS["ambiguous"])
    payload_json = json.dumps(lines_payload, ensure_ascii=False, indent=1)
    user_content = (
        f"图片：{image_name}\n"
        f"版面提示：{hint}\n\n"
        f"OCR 片段（每段含编号 id、栏序、行序、像素坐标 box=[x0,y0,x1,y1]、文本）：\n"
        f"{payload_json}\n\n"
        f"任务：按上述硬约束重排为阅读顺序，输出 JSON。"
    )
    return [
        {"role": "system", "content": REASSEMBLE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_reassembly_json(raw_text: str) -> dict:
    """解析 LLM 输出 JSON（兼容 ```json 包裹 / 前后杂文）。

    Returns:
        {"segments": [{"text", "line_ids", "status"}], "_parse_ok": bool}
    """
    if not raw_text:
        return {"segments": [], "_parse_ok": False}
    text = raw_text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        text = text[a:b + 1]
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"segments": [], "_parse_ok": False}
    segs = obj.get("segments")
    if not isinstance(segs, list):
        return {"segments": [], "_parse_ok": False}
    out = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        ids = s.get("line_ids", [])
        if not isinstance(ids, list):
            ids = []
        out.append({
            "text": str(s.get("text", "")),
            "line_ids": [int(i) for i in ids if isinstance(i, (int, float))],
            "status": "ok",
        })
    return {"segments": out, "_parse_ok": True}


def llm_reassemble(
    client,
    lines: List[dict],
    corrected_map: Optional[Dict[int, str]] = None,
    layout: str = "ambiguous",
    image_name: str = "",
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: int = 240,
) -> Tuple[str, List[dict]]:
    """调用 LLM 重排（单块）。返回 (raw_response, messages)。

    默认走确定性输出（temperature=0）+ 大预算（max_tokens=8192）+ 长超时（240s）：
    V4-Flash 是 thinking 模型，reasoning 会吃预算，max_tokens 太小会把 content 截断为空，
    响应时间也会随 payload 增长（2026-08-20 实测 12 行块 reasoning 需 60-120s+）。
    """
    corrected_map = corrected_map or {}
    payload = []
    for ln in lines:
        b = box_of(ln)
        payload.append({
            "id": ln.get("line_id"),
            "col_index": ln.get("col_index", 0),
            "row_index": ln.get("row_index", 0),
            "box": [b[0], b[1], b[2], b[3]] if b else None,
            "text": _text_of(ln, corrected_map),
        })
    messages = build_reassemble_messages(payload, layout, image_name)
    raw = "".join(client.chat(
        messages, stream=False, response_format={"type": "json_object"},
        max_tokens=max_tokens or 8192,
        temperature=0.0 if temperature is None else temperature,
        timeout=timeout,
    ))
    return raw, messages


def _chunk_lines(lines: List[dict], max_lines: int = 12) -> List[List[dict]]:
    """按栏排序后切块，每块 ≤ max_lines 行（控制单次 LLM payload）。

    - 先按 col_index 排序（竖排古籍栏右→左的语义），栏内保持扫描行序
    - 再按行数切块：同一栏的连续行尽量留在同一块（模型在块内靠 box 坐标重排）
    - VL 扫描若每行 col_index 唯一（无栏结构），则退化为纯顺序切块
    """
    by_col: Dict[int, List[dict]] = {}
    for ln in lines:
        by_col.setdefault(int(ln.get("col_index", 0)), []).append(ln)
    ordered: List[dict] = []
    for col in sorted(by_col):
        ordered.extend(by_col[col])
    return [ordered[i:i + max_lines] for i in range(0, len(ordered), max_lines)]


def llm_reassemble_chunked(
    client,
    lines: List[dict],
    corrected_map: Optional[Dict[int, str]] = None,
    layout: str = "ambiguous",
    image_name: str = "",
    max_lines: int = 12,
    max_tokens: Optional[int] = None,
    timeout: int = 240,
) -> Tuple[List[dict], List[List[dict]]]:
    """分块 LLM 重排：大图拆成 ≤max_lines 行的小块逐块重排，再合并为整页 segments。

    - 每块独立调用 LLM + 独立 JSON 解析（line_ids 是全局编号，块间不冲突）
    - 任一块解析失败 → 抛 ValueError（调用方规则回退）
    - 返回 (segments, messages_per_chunk)，segments 的 seq 已连续编号

    背景（2026-08-20 实测）：未分块时大 payload 触发 V4-Flash reasoning 膨胀，
    全部预算被 reasoning 吃掉、content 空串；分块后每块 reasoning 收敛，输出完整。
    """
    corrected_map = corrected_map or {}
    chunks = _chunk_lines(lines, max_lines)
    if not chunks:
        return [], []
    segments: List[dict] = []
    messages_list: List[List[dict]] = []
    for chunk in chunks:
        raw, msgs = llm_reassemble(
            client, chunk, corrected_map, layout, image_name,
            max_tokens=max_tokens, timeout=timeout,
        )
        messages_list.append(msgs)
        parsed = parse_reassembly_json(raw)
        if not parsed["_parse_ok"] or not parsed["segments"]:
            raise ValueError(
                f"LLM 分块重排解析失败（块 {len(chunk)} 行 / raw {len(raw)} 字符）"
            )
        segments.extend(parsed["segments"])
    for i, s in enumerate(segments, start=1):
        s.setdefault("seq", i)
    return segments, messages_list


# ============================================
# 5b. 名录条目化重组（Phase 1.6：roster 专用）
# ============================================
def build_roster_messages(lines_payload: List[dict], image_name: str = "",
                          template_block: str = "") -> List[dict]:
    """构造 roster 条目化重组 prompt（OpenAI 风格 messages）。

    M8b：template_block 非空时（用户激活了样本模板）追加 few-shot 参考样例。
    """
    payload_json = json.dumps(lines_payload, ensure_ascii=False, indent=1)
    user_content = (
        f"图片：{image_name}\n"
        f"OCR 片段（每段含编号 id、栏序、行序、像素坐标 box=[x0,y0,x1,y1]、文本）：\n"
        f"{payload_json}\n\n"
        f"任务：按上述硬约束把零散字词重组成可读的名录条目，输出 JSON。"
        f"{template_block}"
    )
    return [
        {"role": "system", "content": ROSTER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _roster_payload(lines: List[dict], corrected_map: Optional[Dict[int, str]]) -> List[dict]:
    """L2 行 → LLM payload（与 llm_reassemble 同构，便于复用校验）。"""
    corrected_map = corrected_map or {}
    payload = []
    for ln in lines:
        b = box_of(ln)
        payload.append({
            "id": ln.get("line_id"),
            "col_index": ln.get("col_index", 0),
            "row_index": ln.get("row_index", 0),
            "box": [b[0], b[1], b[2], b[3]] if b else None,
            "text": _text_of(ln, corrected_map),
        })
    return payload


def llm_reassemble_roster(
    client,
    lines: List[dict],
    corrected_map: Optional[Dict[int, str]] = None,
    image_name: str = "",
    max_lines: int = 30,
    timeout: int = 300,
    template: Optional[dict] = None,
    max_workers: int = 3,
) -> List[dict]:
    """名录条目化重组：把 VL 逐字/短词拆行的零散字词聚合成可读条目。

    - 分块（≤max_lines 行/块）逐块重组再合并。实测（2026-08-20）：79 行一次调用触发
      V4-Flash reasoning 膨胀；24-30 行/块 + strong 指引 + max_tokens=32768 收敛。
    - 名录聚合任务比 prose 重排更易 reasoning 膨胀（短词间长距离组合搜索），
      max_tokens 必须给足 32768（16384 全被 reasoning 吃光、content 空串，~4/5 概率失败），
      ROSTER_SYSTEM_PROMPT 的"执行要点"（只读一遍快速分组）是收敛的关键指引。
    - M8b：template 非空（用户激活样本模板）→ 每块 prompt 注入参考样例
    - 每条目一段 text + line_ids（全局编号）；单块失败自动重试 1 次（瞬时错误常见），
      仍失败 → 抛 ValueError（调用方规则回退）
    - ✅ 2026-09-03 块级并发（性能优化 A）：各块调用互不依赖 → ThreadPoolExecutor
      max_workers 路并发（默认 3），结果按原块序合并（质量与串行完全等价，token 成本不变）
    - 字符集合不变 + line_ids 全覆盖由调用方 verify_multiset/_verify_coverage 把关
    """
    corrected_map = corrected_map or {}
    chunks = _chunk_lines(lines, max_lines)
    if not chunks:
        return []
    template_block = template_store.render_template_block(template)

    def _run_chunk(chunk: List[dict]) -> List[dict]:
        messages = build_roster_messages(_roster_payload(chunk, corrected_map),
                                         image_name, template_block=template_block)
        raw = ""
        for attempt in range(2):
            try:
                raw = "".join(client.chat(
                    messages, stream=False, response_format={"type": "json_object"},
                    max_tokens=32768, temperature=0.0, timeout=timeout,
                ))
                break
            except Exception as e:
                log.warning(f"[reassembler] roster 块调用失败（第 {attempt + 1} 次）: {e}")
                raw = ""
        parsed = parse_reassembly_json(raw)
        if not parsed["_parse_ok"] or not parsed["segments"]:
            raise ValueError(
                f"名录重组解析失败（块 {len(chunk)} 行 / raw {len(raw)} 字符: "
                f"{raw[:300]!r}）"
            )
        return parsed["segments"]

    if max_workers <= 1 or len(chunks) <= 1:
        per_chunk = [_run_chunk(c) for c in chunks]
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as ex:
            # ex.map 保持输入顺序 → 合并顺序与串行版本完全一致
            per_chunk = list(ex.map(_run_chunk, chunks))
    segments: List[dict] = [s for segs in per_chunk for s in segs]
    for i, s in enumerate(segments, start=1):
        s.setdefault("seq", i)
    return segments


def _verify_coverage(segments: List[dict], lines: List[dict]) -> Tuple[bool, List, List]:
    """line_ids 覆盖校验：LLM 声称使用的片段编号集合必须 == 输入片段编号集合。

    比字符多集更强：对"上同/同上"这类重复文字，字符多集可能漏检，
    但 line_id 全局唯一，任何遗漏/重复/编造都能精确发现。
    Returns: (ok, missing_ids, extra_ids)
    """
    used: set = set()
    for s in segments:
        for i in s.get("line_ids", []):
            used.add(int(i))
    expected = {int(ln.get("line_id")) for ln in lines if ln.get("line_id") is not None}
    missing = sorted(expected - used)
    extra = sorted(used - expected)
    return (not missing and not extra), missing, extra


# ============================================
# 6. 单图重排 + L4 落盘
# ============================================
# ============================================
# 6b. L4 结果缓存（2026-09-03 性能优化 D）
# ============================================
def _reassembly_cache_key(structured_path: Path, template: Optional[dict],
                          roster_max_lines: int) -> str:
    """缓存键：structured 数据指纹 + 模板身份 + 关键参数。

    - structured 文件 mtime_ns：重新 OCR 会改写该文件 → 自动失效
    - template_id：换样本模板（few-shot 变化）→ 失效
    - PROMPT_VERSION / SCHEMA_VERSION：提示词或结构升级 → 失效
    - roster_max_lines：分块参数影响结果 → 失效
    """
    mt = structured_path.stat().st_mtime_ns
    tid = (template or {}).get("template_id") or "none"
    return f"v1|{mt}|{PROMPT_VERSION}|{SCHEMA_VERSION}|{roster_max_lines}|{tid}"


def _load_cached_reassembly(out_path: Path, key: str, stem: str) -> Optional[dict]:
    """读取缓存 payload；键不匹配或缺 segments → None（视为未命中）。"""
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("_cache_key") != key:
        return None
    if payload.get("image_stem") != stem or not payload.get("segments"):
        return None
    return payload


def reassemble_image(
    stem: str,
    structured_dir: Path = DEFAULT_STRUCTURED_DIR,
    corrected_map: Optional[Dict[int, str]] = None,
    client=None,
    force_llm: bool = False,
    model_name: str = "",
    out_dir: Optional[Path] = None,
    roster_max_lines: int = 30,
    template: Optional[dict] = None,  # M14：显式指定样本模板（None=回退激活的 roster 模板）
) -> Optional[dict]:
    """重排单张图并落盘 L4（data/reassembled/<stem>.json）。

    Phase 1.6 内容类型感知：
    - 先判内容类型（规则候选 + LLM 裁决），再决定重组策略
    - roster（名录/名单）→ 条目化重组 llm_reassemble_roster（LLM 聚合字词成条目）
    - prose / letter / document / table / mixed → 现有阅读序重排（竖排/横排规则，ambiguous 走 LLM）
    - 字符多集校验失败 / line_ids 覆盖不完整 → 全部段 needs_review（不进已验证视图）
    """
def reassemble_image(
    stem: str,
    structured_dir: Path = DEFAULT_STRUCTURED_DIR,
    corrected_map: Optional[Dict[int, str]] = None,
    client=None,
    force_llm: bool = False,
    model_name: str = "",
    out_dir: Optional[Path] = None,
    roster_max_lines: int = 30,
    template: Optional[dict] = None,  # M14：显式指定样本模板（None=回退激活的 roster 模板）
    roster_workers: int = 3,          # 2026-09-03：roster 块级并发数（1=串行）
    use_cache: bool = True,           # 2026-09-03：L4 结果缓存（structured/模板/参数未变则直接复用）
) -> Optional[dict]:
    """重排单张图并落盘 L4（data/reassembled/<stem>.json）。

    Phase 1.6 内容类型感知：
    - 先判内容类型（规则候选 + LLM 裁决），再决定重组策略
    - roster（名录/名单）→ 条目化重组 llm_reassemble_roster（LLM 聚合字词成条目）
    - prose / letter / document / table / mixed → 现有阅读序重排（竖排/横排规则，ambiguous 走 LLM）
    - 字符多集校验失败 / line_ids 覆盖不完整 → 全部段 needs_review（不进已验证视图）
    - 2026-09-03 缓存：use_cache=True 且 out_dir 给定时，已落盘结果若 _cache_key
      匹配（structured 未变 + 模板未换 + 参数一致）直接返回，跳过分类与 LLM；
      force_llm=True 视为强制重排（绕过缓存）
    """
    corrected_map = corrected_map or {}
    path = structured_dir / f"{stem}.json"
    if not path.exists():
        log.warning(f"[reassembler] structured 不存在: {path}")
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    l0 = data.get("L0_document", {}) or {}
    l2 = data.get("L2_lines", []) or []
    if not l2:
        log.warning(f"[reassembler] 无 L2 行，跳过: {stem}")
        return None

    # 缓存键（out_dir 存在时才可缓存；force_llm = 强制重排，绕过）
    cache_key = ""
    if out_dir is not None and not force_llm:
        try:
            tmpl_eff = template if template is not None else template_store.get_active_template("roster")
        except Exception:
            tmpl_eff = None
        cache_key = _reassembly_cache_key(path, tmpl_eff, roster_max_lines)
        if use_cache:
            out_path = out_dir / f"{stem}.json"
            if out_path.exists():
                cached = _load_cached_reassembly(out_path, cache_key, stem)
                if cached is not None:
                    log.info(f"[reassembler] {stem}: L4 缓存命中，跳过重排")
                    return cached

    layout = classify_layout(l2)
    input_texts = [_text_of(ln, corrected_map) for ln in l2]
    image_name = l0.get("image_name", f"{stem}.png")

    # 内容类型判定：规则候选 + LLM 裁决（用户核心诉求：AI 先判断图片信息类型）
    content_type = classify_content_type(l2)
    classify_method = "rule"
    classify_reason = ""
    if client is not None:
        try:
            llm_ct, reason = llm_classify_content_type(client, l2, image_name)
            if llm_ct != "ambiguous":
                content_type = llm_ct
                classify_method = "llm"
                classify_reason = reason
            else:
                classify_reason = f"LLM 无法判定（{reason}），回落规则: {content_type}"
        except Exception as e:
            log.warning(f"[reassembler] LLM 内容分类失败，用规则结果: {stem} ({e})")

    segments: Optional[List[dict]] = None
    method = "rule"
    if content_type == "roster" and client is not None:
        # 名录：条目化重组（LLM 聚合零散字词成「人名+科年/籍贯/官职」条目）
        # M8b：激活的 roster 样本模板 → few-shot 注入每块 prompt
        # M14：调用方显式传 template 时优先（导出时用户自选），否则回退激活模板
        tmpl = template if template is not None else template_store.get_active_template("roster")
        try:
            segments = llm_reassemble_roster(
                client, l2, corrected_map, image_name, max_lines=roster_max_lines,
                template=tmpl, max_workers=roster_workers)
            method = "llm"
        except Exception as e:
            log.warning(f"[reassembler] 名录重组失败，规则回退: {stem} ({e})")
            segments = None
    else:
        # 连贯文本/信札/表格/混合/歧义：现有阅读序重排
        use_llm = force_llm or layout == "ambiguous"
        if use_llm and client is not None:
            try:
                segments, _ = llm_reassemble_chunked(client, l2, corrected_map, layout, image_name)
                method = "llm"
            except Exception as e:
                log.warning(f"[reassembler] LLM 调用失败，规则回退: {stem} ({e})")
                segments = None

    if segments is None:
        seg_layout = layout if layout in ("vertical", "horizontal") else "ambiguous"
        segments = deterministic_reassemble(l2, corrected_map, seg_layout)
        method = "rule" if seg_layout != "ambiguous" else "rule_fallback"
        if seg_layout == "ambiguous":
            for s in segments:
                s["status"] = "needs_review"

    # 段编号归一（LLM 解析路径的段来自 JSON，无 seq；chunked 已编号，setdefault 幂等）
    for i, s in enumerate(segments, start=1):
        s.setdefault("seq", i)

    output_texts = [s["text"] for s in segments]
    # roster 条目化重组允许模型按示例格式补分隔标点（逗号等），但不允许删字
    verification = verify_multiset(
        input_texts, output_texts,
        ignore_extra=ROSTER_FORMAT_CHARS if content_type == "roster" else None)
    review_flags = []  # 记录降级原因（写进日志/产物）
    if method == "llm":
        cov_ok, missing, extra = _verify_coverage(segments, l2)
        if not cov_ok:
            review_flags.append(f"line_ids 覆盖不完整: missing={missing[:8]} extra={extra[:8]}")
    if not verification["matched"]:
        review_flags.append(f"字符多集差异 {verification['diff_total']} 字")
    if review_flags:
        log.warning(f"[reassembler] {stem}: " + "; ".join(review_flags) + " → 全部 needs_review")
        for s in segments:
            s["status"] = "needs_review"

    payload = {
        "_schema_version": SCHEMA_VERSION,
        "image_stem": stem,
        "image_name": image_name,
        "layout": layout,
        "content_type": content_type,
        "classify_method": classify_method,
        "classify_reason": classify_reason,
        "classify_model": model_name if classify_method == "llm" else "",
        "method": method,
        "reassembled_at": datetime.now().isoformat(timespec="seconds"),
        "llm_model": model_name if method == "llm" else "",
        "prompt_version": PROMPT_VERSION if method == "llm" else 0,
        "verification": verification,
        "segments": segments,
    }
    if cache_key:
        payload["_cache_key"] = cache_key
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{stem}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"[reassembler] {stem}: layout={layout} type={content_type} method={method} "
             f"segments={len(segments)} verified={verification['matched']}")
    return payload


# ============================================
# 7. 项目级重排 + CLI
# ============================================
def _project_image_stems(project_id: str, data_dir: Path = Path("data")) -> List[str]:
    """从 data/project_assignments.json 取项目图片白名单（按 stem）。

    ✅ M23：核心逻辑委托 data_io.get_project_image_stems 统一实现（消三胞胎重复）。
    """
    from data_io import get_project_image_stems
    return get_project_image_stems(project_id, data_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR 阅读序重排器（Phase 1）")
    parser.add_argument("--project-id", default="", help="项目 ID（自动取该项目图片白名单）")
    parser.add_argument("--image-stems", nargs="*", default=None, help="图片白名单（优先于 --project-id；缺省=全部）")
    parser.add_argument("--structured-dir", default=str(DEFAULT_STRUCTURED_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--xlsx", default="", help="主工作簿路径（提供则重排优先用人工修正）")
    parser.add_argument("--llm-config", default=str(DEFAULT_LLM_CONFIG), help="llm_config.json 路径")
    parser.add_argument("--force-llm", action="store_true", help="全部走 LLM 重排（调试用）")
    parser.add_argument("--roster-max-lines", type=int, default=30,
                        help="名录条目化重组的分块行数（默认 30；实测 79 行一次调用会触发 "
                             "reasoning 膨胀，必须分块；调大减少调用次数但单块风险上升）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structured_dir = Path(args.structured_dir)
    if not structured_dir.is_dir():
        log.error(f"structured 目录不存在: {structured_dir}")
        return 2

    stems = args.image_stems
    if stems is None and args.project_id:
        stems = _project_image_stems(args.project_id)
        log.info(f"项目 {args.project_id} 图片白名单: {len(stems)} 张")
    elif stems is None:
        stems = sorted(p.stem for p in structured_dir.glob("*.json") if not p.name.startswith("_"))
    if not stems:
        log.warning("无图片可重排")
        return 1

    corrected = {}
    if args.xlsx:
        from exporter import corrected_map_from_xlsx
        corrected = corrected_map_from_xlsx(Path(args.xlsx))
        log.info(f"主工作簿修正列: {len(corrected)} 条非空修正")

    client = None
    model_name = ""
    try:
        from llm_client import get_or_create_client, load_config
        cfg = load_config(Path(args.llm_config))
        client = get_or_create_client(cfg)
        model_name = client.model_name
        log.info(f"LLM 可用: {model_name}")
    except Exception as e:
        log.warning(f"LLM 不可用（{e}）——歧义版面将规则回退 + needs_review")

    out_dir = Path(args.out_dir)
    n_llm = 0
    n_rule = 0
    for stem in stems:
        payload = reassemble_image(
            stem, structured_dir, corrected,
            client=client, force_llm=args.force_llm,
            model_name=model_name, out_dir=out_dir,
            roster_max_lines=args.roster_max_lines,
        )
        if payload is None:
            continue
        if payload["method"] == "llm":
            n_llm += 1
        else:
            n_rule += 1
    log.info(f"完成：共 {len(stems)} 张，LLM={n_llm}，规则={n_rule} → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
