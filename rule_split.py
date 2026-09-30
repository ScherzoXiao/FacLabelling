# -*- coding: utf-8 -*-
"""数据飞轮 · L0 确定性切分（模板 + 锚词 + 几何；**零 LLM、零 API**，2026-09-11）。

## 为什么需要这一层

线上文本模型拆分一页的代价实测为 **completion ≈ 10k–16k tokens**（thinking 的
reasoning 吃满 16384 才截断），而一页正文只有 **322 字**。钱花在哪里？花在
**让模型把正文重抄一遍**——输入 322 字，输出 JSON 1222–1423 字（≈ 输入的 4 倍）。

结论（实测取证）：**成本 ∝ 需要生成的 token 数**。要降一个数量级，
不是换更小的模型，而是**让模型别生成正文**。

L0 把这件事做到极致：**一个 token 都不生成**。

## 它凭什么能work（三条取证）

1. **切分的原料早就免费在手上**：线上 OCR 的 `block_content` 换行本身就是
   **行 = 竖列**（实测 27 字/列），我们的解析层把它拼回整段文字，再花钱让模型
   切回去。L0 直接用 `preannotate.page_lines`（已含阅读序与几何复活）。
2. **记录边界是几何的**：一条记录起于一个**短行**（公司名，4–8 字），
   正文行恒长（24–27 字）。判据无量纲：`len(行) ≤ name_max_len` 且
   **下一行是长行**（正文）且不含任何锚词。
3. **属性都有确定锚词**：`光緒X年X月X日`（创立/注册）、职衔表（功名）、
   `總號在/本廠在`（地址）、`股分/資本/規銀`（资本）、`從事`（主营）。
   锚词由**属性名**推出（属性名即语义标签），辅以金标准学到的职衔候选。

## 与既有模块的关系（绝不另造平行实现）

- **行/阅读序/几何** → `preannotate.page_lines`（M1 夹框 / M2 索引映射 /
  M3 稳定标识全在里面）。
- **锚定回几何** → `preannotate.anchor_page`。本模块**只产出 `records`
  （属性→值）**，与 `split_records.split_page` 的输出**同构**，
  故 `preannotate_gen` 可无缝替换拆分器；因为值都是原文的**逐字切片**，
  锚定必然是 `exact`/`contains`/`span`（不会 `none`）。
- **规则** → `rule_learn` 的产物（骨架序、锚属性、职衔候选、页面常量）。
  规则缺失也能跑（回落到默认阈值与内建锚词表），只是精度略低。

## 硬约束

- **绝不丢字**：值一律是页面字符流的**切片**（`stream[a:b]`），不重写、不拼造。
  页面级自检 `coverage`（被锚词覆盖的区间 / 全流）随 `parse_meta` 回传。
- **宁缺勿猜**：某属性没有可用锚词 → 留空，不猜。
- **原字形**：定位在繁简折叠后的归一文本上做，**取值从原文切**（保持原字形）。

## 已知短板（诚实记录）

- 表格页（`block_label="table"`）的单元格归属仍粗（服务返回的 HTML 表
  `rowspan` 一行装走整段正文），几何复活后的行序也可能与物理列序不一致
  （0003 实测：3 条记录只能稳定切出 2 条）。故 L0 对每页给
  `parse_meta.confidence`，低置信页在界面可见并优先送待核。
- `续招资本*` 这类"同名字段第二次出现"的槽位，目前只能靠
  `续招` 关键词区分，跨页续接时可能落空。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import layout_contract as LC

log = logging.getLogger("rule_split")

MODE_RULE = "rule"

# ============================================================
# 匹配层简繁折叠（S2T_FOLD，简→繁 20 字最小表）
# ============================================================
# ★ 单一实现：corpus_scan / rule_learn 的折叠去重都从本表 import（2026-09-29）。
# 方向 简→繁（本库 OCR 主态是繁体语料），在 `LC.normalize_text`（繁→简，
#   依赖 zhconv，缺失时退化为原样比较）**之后**追加 —— 两道折叠叠加后，这
#   20 字无论原文简繁、无论 zhconv 在不在，归一结果恒为繁体态：金标准简体
#   常量「公司注册各案摘要」与池页繁体「公司註冊各案摘要」在匹配层必然同形。
#   ★ 只折匹配层：归一串与原文 1:1 等长，取值/裁剪按坐标回原文，保原字形。
S2T_FOLD = {
    "务": "務", "报": "報", "册": "冊", "注": "註", "号": "號",
    "铁": "鐵", "银": "銀", "圆": "圓", "万": "萬", "两": "兩",
    "汉": "漢", "济": "濟", "芜": "蕪", "厂": "廠", "矿": "礦",
    "阳": "陽", "东": "東", "广": "廣", "苏": "蘇", "沪": "滬",
}
_S2T_TRANS = str.maketrans(S2T_FOLD)
# 实验台架对照臂开关：monkeypatch `_S2T_TRANS = str.maketrans({})` 即回退到
# 「不做 s2t 折叠」（LC 折叠仍在），用于折叠增益的对照实验。


# ============================================================
# 归一化：定位用（繁简折叠 + 去空白），取值仍从原文切
# ============================================================
def norm_stream(text: str) -> str:
    """逐字归一，**保证与原文 1:1 对齐**（值可原样切回原文）。

    `LC.normalize_text` 会去空白，整串调用会让位置与原文错位；
    故逐字折叠（带缓存），并对折叠后长度 ≠ 1 的字符走整串兜底。
    ★ LC 折叠后追加 `S2T_FOLD`（简→繁）—— 仍逐字、仍 1:1 等长，长度守恒
      兜底逻辑不变；归一空间里这 20 字的规范形 = 繁体态。
    """
    cache: Dict[str, str] = {}
    out: List[str] = []
    for ch in text:
        c = cache.get(ch)
        if c is None:
            c = (LC.normalize_text(ch) or ch).translate(_S2T_TRANS)
            cache[ch] = c
        out.append(c if len(c) == 1
                   else (LC.normalize_text(ch) or ch).translate(_S2T_TRANS))
    s = "".join(out)
    if len(s) != len(text):                      # 兜底：极端字符导致长度漂移
        s = ((LC.normalize_text(text) + text)[:len(text)]).translate(_S2T_TRANS)
    return s


# ---------------------------------------------------------------- 阈值
NAME_MIN_LEN = 2          # 公司名最短（1 字的「厂」是续接碎片，不是记录起点）
NAME_MAX_LEN_DEFAULT = 8  # 公司名上限（金标准实测 max=6，留 2 字余量）
BODY_MIN_LEN_DEFAULT = 8  # 「下一行是正文」的判据（正文行实测 24–27 字）
NAME_LEN_DEFAULT = 3      # 人名长度（金标准实测：周廷弼/叶成忠/张石君 均 3 字）
MAX_VALUE_CHARS = 80      # 单值上限（地址/主营可很长，但要有界）
# P12（2026-09-30）：CONT_LOOKAHEAD 死常量删除（R1 取证 :118 定义后从未使用）。
# 当年设想的是「起点判据⑤向后看 N 行找正文」，实际落地的放宽是**更窄**的
# 「下一行 ≤2 字后缀碎片 + 再下一行正文」（见 segment_records F3 注）——
# 留着一个语义不符的 6 行窗口常量只会误导读者，故删除留痕。

# 强锚词：行内出现即**不可能是公司名**（公司名不会含这些）
# ★ 字面量过 `norm_stream`（2026-09-29）：norm_stream 的归一空间被 S2T_FOLD
#   翻转成「20 字恒繁体态」后，锚词必须在**同一空间**书写才能命中 ——
#   例：正文「註冊」归一为「註冊」，字面量「注册」也须折成「註冊」。
#   同折叠还让繁简两态字面量自动并成一个规范形（總號/总号 → 总號）。
_STRONG_ANCHOR_WORDS = tuple(dict.fromkeys(norm_stream(w) for w in (
    "注册", "補註", "补注", "从事", "资本", "資本", "股本", "股分", "股份",
    "规银", "規銀", "股银", "股銀", "有限", "两合", "无限", "无限",
    "总号", "总公司", "總公司", "本厂", "本廠", "本行", "本局", "本公司",
    "分号", "分號", "分销处", "分銷處", "创办", "创辦", "发起", "發起",
    "摘要", "告白", "目录", "目錄",
)))
# 公司名后缀常量已删（P11，2026-09-29）：全库无引用的死常量（PENDING.md 已登记）。
_NUMERAL_CHARS = set("零〇一二三四五六七八九十百千万亿萬億两兩元圓圆角分第册冊年月日")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ERA_CHARS = ("光绪", "光緒", "宣统", "宣統", "咸丰", "咸豐", "同治", "乾隆",
              "嘉庆", "嘉慶", "道光", "康熙", "雍正")

# 日期锚（含月/日的可选形态：「正月」「闰四月」「初一日」「廿八日」）
_ERA = r"(?:%s)" % "|".join(_ERA_CHARS)
_YEAR = r"(?:[0-9〇零一二三四五六七八九十百]+|元)年"
_MON = r"(?:闰|閏)?(?:正|冬|腊|臘|[0-9〇零一二三四五六七八九十]{1,3})月"
_DAY = r"(?:[初廿][0-9〇零一二三四五六七八九十]{1,3}日|[0-9〇零一二三四五六七八九十]{1,3}日)"
_DATE = _ERA + _YEAR + r"(?:" + _MON + r")?(?:" + _DAY + r")?"
_DATE_PLAIN = _ERA + _YEAR            # 只有年（金标准里「光绪二十八年正月」= 有月）
_REG_MARK = norm_stream(r"(?:注册|補註|补注)")
# ★ 日期「未完结」续字（P5，2026-09-26）：创立时间匹配后若紧跟一段**日期构字
# 且这段构字直达注册标记**，说明该匹配是正则回退收缩的残段。实证（样例页_0002
#   用户裁决）：注册日期「光緒三十一年十二月二十九日註册」曾被收缩成
#   「光緒三十一年」，残余「十二月/二十九/日註册」被人名族切块错标成
#   注册人一/二/三 —— 同一日期语段拆给了四个属性。
#   ⚠ 不能一刀切「后随日期构字即截短」：金标准本身有「光緒四年四月」（年+月
#   截短形态，后随「三品銜…」）—— 一刀切会把创立时间逼去偷注册日期（0000_0002
#   首版回归实测）。判据见 `_anchors_for` 创立时间分支的 `anti` 组合。
_DATE_CONT = r"[〇零一二三四五六七八九十百初廿月日年正冬腊臘闰閏]"
# 月日片段（无纪年头）：月/日不入人名，从人名区剥除是安全的
_MON_DAY_FRAG = r"[〇零一二三四五六七八九十初廿]{1,3}月(?:[初廿〇零一二三四五六七八九十]{0,3}日)?"
# 人名区「日期性」占比上限：超过即整段不是裸人名列表（开办二十年暫停…类散文），
# 切块只会产出伪人名 —— 整段放弃（宁缺勿猜）。
_DATEISH_REGION_RATIO = 0.4
_ADDR_HEAD = norm_stream(
    r"(?:总号|總號|总公司|總公司|本厂|本廠|本行|本局|本公司|公司|"
    r"行号|行號|分号|分號|栈|棧)[在设設]")
# P11（2026-09-29，R2 因子 5）：manual 页地址头「辦事所在」零命中纯漏抽
# （金标准「办事所在江苏省江宁府句容县天王寺镇」）——该形态「所在」自带「在」，
# 不接 [在设設]，故单独成表并与 _ADDR_HEAD 拼接使用（rule_report 分两段人话展示）。
_ADDR_HEAD_EXTRA = norm_stream(r"辦事所在|办事所在")
_ADDR_HEAD_FULL = _ADDR_HEAD + "|" + _ADDR_HEAD_EXTRA
_CAP_HEAD = norm_stream(
    r"(?:股分|股份|股本|资本|資本|规银|規銀|股银|股銀|资银|資銀)")
# P11（2026-09-29，R2 因子 3）：资本头词「股銀」命中「官股銀／商股銀」内部时，
# 是更长单位词组合的**内部切片**，不得作为 after 取值右界——否则资本被切剩
# 「股分官」（样例页_0002 北洋煙草行16 实测：'股分官股銀二萬兩商股銀四萬五千'
# → pred 只剩 '股分官'）。判据：命中「股銀」且**前字**构成组合（官/商）→ 内截。
_CAP_INNER_PREV_CHARS = norm_stream("官商")
_CAP_INNER_TAIL = norm_stream("股銀")

# P11（2026-09-29，R2 根因 3）：after 模式取值右界终结词——遇之即截断。
# ★ 每个词都能指到 r2_probe_readout.txt 的过冲行证据，不许凭空加：
# 每股/每 —— '股分洋銀十萬圓每股洋銀一百圓'（样例页_0001 行4）、
# '股分規銀六十萬兩每股'（样例页_0001 行10）等全部「每股」过冲；
# 爲/为 —— '資本銀洋十四萬元爲'（样例页_0001 行15）、'資本銀洋十九萬元爲'、
#             '資本銀洋四萬元爲'（「爲」引出类别词前的过冲尾巴）；
#   獨力/独力 —— '資本銀一十萬元獨力'、'資本銀三萬元獨力'（5期 行1/行5）；
# 分銷處/分销处 —— '總號在北京琉璃廠分銷處在天津…'（样例页_0001 行3）、
# '總公司在天津閘口洋貨街分銷處在北京…'（样例页_0002 行15）；
# 分號/分号 —— '總公司在漳德府城外分號擬設懷慶府'（样例页_0003 行14）；
# 創始/创始 —— '公司在湖北漢口創始公司在江蘇上海'（样例页_0003 行9）；
# 另於/另于 —— '總號在上海另於錦州分設公司'（样例页_0001 行10）。
#   双写简繁两态：S2T_FOLD 只保 20 字（號→號 等），銷/創/獨/於 的折叠依赖
#   zhconv，缺失时两态在归一空间不同形——双写保证环境无关。
_VALUE_STOP_WORDS = tuple(dict.fromkeys(norm_stream(w) for w in (
    "每股", "每", "爲", "为", "獨力", "独力",
    "分銷處", "分销处", "分號", "分号", "創始", "创始", "另於", "另于",
)))
# ⚠ 类别词表**不含裸「股分/股份」**：实测教训——含了就会在
# 「续招股分洋银五万圆」处抢先命中（左先于右），把类别读成「股分」。
# 类别词必有「有限/两合/无限/公司」这类**组织形式**字，否则不成立。
_CAT_WORD = norm_stream(
    r"(?:股分有限公司|股份有限公司|有限股分公司|有限股份公司|"
    r"股分公司|两合公司|兩合公司|无限公司|無限公司|有限公司|有限)")
_BIZ_HEAD = norm_stream(r"(?:从事)")
_PERSON_TAIL = norm_stream(
    r"(?:为创办人|為創辦人|创办人|創辦人|创办|創辦|发起|發起)")

_PERSON_ATTR_RE = re.compile(
    r"^(?:其他|其它)?(?P<base>注册人|註冊人|创立人|創立人|创办人|創辦人|"
    r"发起人|發起人|代表人|经理人|經理人)(?P<ord>[一二三四五六七八九十])?"
    r"(?P<title>功名|职衔|職銜)?$")
_ORDINALS = "一二三四五六七八九十"


# ============================================================
# 页面件剔除：粘在正文里的**页面常量**（P0-2，2026-09-13）
# ============================================================
def strip_page_constants(text: str, spec: dict) -> Tuple[str, List[str]]:
    """裁掉**粘在正文里**的页面常量 → `(裁后文本, 被裁掉的原文片段)`。

    ★ 缺口在哪：本模块早有「页面常量」这个概念（判据 ④，`_looks_like_name`），
    但只做**整行精确匹配**（`n in spec["page_constants"]`）。而竖排古籍实测的
    真实失败形态是常量**粘在长行末尾**，精确匹配必然漏：

        实测样例页 0003：
        上游 VLM 把整页正文放在**一个** `<td rowspan="3">` 里（300 字），
        **版心**（篇名「公司註冊各案摘要」）被拼在正文串末尾 → 那一"行"成了
        `圓每股銀圓一百圓為股分有限公司從事引公司註冊各案摘要`，
        于是 `主营业务 = 「從事引公司註冊各案摘要」` —— 页面件混进了属性值。

    为什么**不能**改成"整行减去常量后再判名字"：那会让"整行就是常量"的那一行
    变成空串，反而有被当成公司名的风险。故**只在取值前裁**，行文本一律不动。

    为什么用**常量**而不是关键词黑名单：常量来自金标准（人在两页例题上
    把它标成 `表头` 属性），是**例题归纳出来的**，改一次金标准就跟着变；
    黑名单则是把人的判断写死在代码里。

    定位在 `norm_stream`（与原文 **1:1 等长**，**匹配层折叠简繁**：LC 繁→简 +
    `S2T_FOLD` 简→繁，这 20 字无论原文简繁、无论 zhconv 在否，归一恒同形）上做，
    裁剪坐标直接用于原文 —— **裁剪仍按坐标保原字形**：输出文本一律不动、
    不做繁简转换，`removed` 里是原文字形的逐字切片。

    ★ P0-2 案例（上面「公司註冊各案摘要」粘尾污染主营业务）—— **本改动修复**
      （2026-09-29）：修复前命中依赖运行环境装了 zhconv（靠 LC 繁→简折叠）；
      zhconv 缺失时 LC 退化为原样比较，简体常量在繁体页上永不命中、污染复现。
      S2T_FOLD 折叠后命中不再依赖 zhconv 可用性。
    """
    consts = [str(c) for c in (spec.get("page_constants") or []) if c]
    if not consts or not text:
        return text, []
    n = norm_stream(text)
    hits: List[Tuple[int, int]] = []
    for c in consts:
        start = 0
        while True:
            i = n.find(c, start)
            if i < 0:
                break
            hits.append((i, i + len(c)))
            start = i + len(c)
    if not hits:
        return text, []
    hits.sort()
    merged: List[List[int]] = []
    for a, b in hits:                            # 常量之间可能互相包含 → 先并集
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    parts: List[str] = []
    removed: List[str] = []
    prev = 0
    for a, b in merged:
        parts.append(text[prev:a])
        removed.append(text[a:b])
        prev = b
    parts.append(text[prev:])
    return "".join(parts), removed


# ============================================================
# 规则规格（属性名 → 锚词）
# ============================================================
def _anchors_for(attr: str, spec: dict) -> List[Tuple[re.Pattern, str]]:
    """属性名 → [(正则, 取值方式)]；取值方式 ∈ `self` / `after`。

    **为什么按属性名推锚词**：属性名本身就是用户给的语义标签（"注册时间"
    说明这个格子要装什么），这正是原先交给 LLM 判断的那件事。把它写成
    确定性规则，就没有"生成"了。
    """
    a = attr
    if _PERSON_ATTR_RE.match(a) or "跨页" in a or "表头" in a:
        # 人名**族**（注册人一/二/…）由 `extract_persons` 统一处理；
        # 但单值的「创立人/创办人」不是族（金标准 0002「由保定农务局设厂试办」），
        # 它们走锚词路径：发起主体由「由」引出。
        m = _PERSON_ATTR_RE.match(a)
        if (m and not m.group("ord") and not m.group("title")
                and m.group("base") in ("创立人", "創立人", "创办人", "創辦人",
                                        "发起人", "發起人")):
            return [(re.compile(r"(?:系由|係由|经由|經由|由)"), "after")]
        return []
    # ⚠ 判据顺序不可换：**「注册资本」里含子串「注册」** —— 若先判「注册」，
    # 注册资本会被当成注册时间去抓「光绪X年X月X日注册」，整条记录的游标
    # 一下跳到页尾，后面所有属性全部落空（2026-09-11 验收台实测）。
    if "续招" in a or "續招" in a:
        if "时间" in a or "日期" in a:
            # 续招时间/续招注册时间：日期 + 其后紧跟「续招」或「补注」
            return [(re.compile(_DATE + r"(?=.{0,6}?(?:续招|补注))"), "self")]
        return [(re.compile(r"(?:续招|續招)"), "after")]     # 续招额
    if "功名" in a or "职衔" in a or "職銜" in a or "衔" in a:
        rx = spec.get("titles_re")
        return [(re.compile(rx), "self")] if rx else []
    if "地址" in a or "所在地" in a or "地点" in a or "处所" in a:
        return [(re.compile(_ADDR_HEAD_FULL), "after")]
    if ("资本" in a or "資本" in a or "股本" in a or "股银" in a
            or "金额" in a or "資金" in a):
        return [(re.compile(_CAP_HEAD), "after")]
    if "类别" in a or "性质" in a or "種類" in a or "种类" in a:
        return [(re.compile(_CAT_WORD), "self")]
    if "主营" in a or "业务" in a or "经营" in a or "营业" in a:
        return [(re.compile(_BIZ_HEAD), "after")]
    if "时间" in a or "日期" in a or "年月" in a:
        if "注册" in a or "登记" in a or "註冊" in a:
            # 日与「注册」之间允许 ≤6 字的 OCR 噪声（实测：「八月十公牘公司註冊」
            # ——「日」被误识成「公牘公司」，紧邻假设会漏掉这条）
            return [(re.compile(_DATE + r".{0,6}?" + _REG_MARK), "self")]
        if "创立" in a or "创办" in a or "成立" in a or "开设" in a:
            # 创立时间：日期，且 ① **不是**注册日期（后 0–6 字内有注册标记）；
            # ② **不是注册日期的截短前缀**（匹配后紧跟一段日期构字、且这段构字
            # **直达**注册标记 → 本匹配是回退收缩的残段。P5 实测（样例页_0002）：
            #    「光緒三十一年十二月二十九日註册」曾被收缩成「光緒三十一年」，
            #    残余「十二月/二十九/日註册」被人名族切块错标成 注册人一/二/三）。
            #    ⚠ 不能用「后随日期构字」一刀切：金标准本身有「光緒四年四月」
            #    （年+月截短形态，后随「三品銜…」）—— 0000_0002 首版回归实测：
            #    一刀切会把创立时间逼去偷注册日期，注册时间反而落空。
            #    故只拦「构字段直达注册标记」，真创立日期后随「初九日」等不受影响。
            anti = (r"(?!.{0,6}?" + _REG_MARK + r")"
                    + r"(?!" + _DATE_CONT + r"{1,10}?" + _REG_MARK + r")")
            return [(re.compile(_DATE + anti), "self")]
        return [(re.compile(_DATE), "self"),
                (re.compile(_DATE_PLAIN), "self")]
    if "名称" in a or "公司名" in a or "商号" in a or "店名" in a:
        return []                                    # 记录起点，由几何给出
    if "创立人" in a or "创办人" in a or "发起人" in a:
        # 古文常见「由××局设厂试办」——发起主体由「由」引出
        return [(re.compile(r"(?:系由|係由|经由|經由|由)"), "after")]
    return []


def _build_titles_re(learned: dict, profile: dict) -> str:
    """职衔正则 = 金标准学到的候选（繁简折叠后）+ 内建种子。

    内建种子只放**结构性词头**（不针对单一档案）：候选/候补/试用/花翎/几品衔…
    """
    cand: List[str] = []
    for t in (learned.get("title_candidates") or []):
        t = norm_stream(str(t).strip())
        if 2 <= len(t) <= 8:
            cand.append(re.escape(t))
    seed = [
        r"翰林院侍读学士", r"侍读学士", r"候选道", r"候补道", r"候选知府",
        r"试用知县", r"候选县丞", r"通判衔", r"花翎游击", r"游击",
        r"三品衔", r"四品衔", r"五品衔", r"[三四五]品衔", r"[三四五]品顶戴",
        r"候补[知同通州]?[县縣]?", r"候选[知同通州]?[县縣]?", r"试用[知同通州]?[县縣]?",
        r"知府衔", r"知县衔", r"县丞", r"州同", r"州判", r"郎中", r"员外郎",
        r"主事", r"中书", r"道员", r"监生", r"生员", r"举人", r"进士",
    ]
    # 长候选优先（贪婪会让「候选道」吃掉「候选道周廷弼」的头，故按长度降序）；
    # **同长度再按字典序**，否则同长度的词保留 `set` 的哈希顺序 —— Python 默认
    # 哈希随机化，同一份 learned 在不同进程会编出不同的 titles_re；而正则
    # alternation 是**首次匹配优先**，顺序会影响匹配 → 切分结果跨进程不可复现。
    # （2026-09-12 P-B 验收的跨进程比对暴露：4 字候选顺序在两次编译中不同。）
    # 同长度词之间必无前缀包含关系，故字典序定序**不改变匹配行为**。
    cand = sorted(set(cand), key=lambda s: (-len(s), s))
    return "|".join(cand + seed)


def build_spec(learned: Optional[dict], profile: dict) -> dict:
    """已学规则 + 档案 → L0 切分规格（纯函数，零副作用）。"""
    learned = learned or {}
    sk = learned.get("skeleton") or {}
    order = [str(a) for a in (sk.get("order") or [])]
    prof_attrs = []
    for a in (profile.get("attrs") or []):
        prof_attrs.append(a["name"] if isinstance(a, dict) else str(a))
    attrs = order + [a for a in prof_attrs if a not in order]

    # 人名族：槽位按序数排（一→二→三→…→其他）
    slots: List[Tuple[str, str]] = []                # [(名字属性, 功名属性)]
    # 基名 = **槽位最多的那一族**（实测教训：按"最后一个匹配"取会把
    # 「创立人」（单值）误判成族名，导致整族 注册人一/二/三 全部落空）。
    _cnt: Dict[str, int] = {}
    for a in attrs:
        m = _PERSON_ATTR_RE.match(a)
        if m:
            _cnt[m.group("base")] = _cnt.get(m.group("base"), 0) + 1
    base = max(_cnt, key=lambda k: _cnt[k]) if _cnt else "注册人"

    def _slot_pick(ord_i: Optional[str]) -> Tuple[str, str]:
        """槽位属性名：**优先用档案里已有的原名**，缺失则按族生成。"""
        if ord_i is None:
            nm, tt = "其他" + base, "其他" + base + "功名"
        else:
            nm, tt = base + ord_i, base + ord_i + "功名"
        for a in attrs:
            m = _PERSON_ATTR_RE.match(a)
            if not m or m.group("base") != base:
                continue
            if (m.group("ord") or "") == (ord_i or "") and not m.group("title"):
                nm = a
            if (m.group("ord") or "") == (ord_i or "") and m.group("title"):
                tt = a
        return nm, tt

    # 序数槽位**只在档案确实定义了该序数时才开**——否则超过档案槽位数的人
    # 会被塞进不存在的「注册人四」，而档案其实是用「其他注册人」收容的
    # （金标准 0003 实测：4 个人 → 一/二/三 + 其他）。
    def _has_ordinal(o: str) -> bool:
        for a in attrs:
            m = _PERSON_ATTR_RE.match(a)
            if m and m.group("base") == base and (m.group("ord") or "") == o:
                return True
        return False

    for o in _ORDINALS[:4]:
        if _has_ordinal(o):
            slots.append(_slot_pick(o))
    slots.append(_slot_pick(None))

    # 阈值：优先由金标准实测推（取长优先的示例即完整值），无金标准 → 常数
    name_max = NAME_MAX_LEN_DEFAULT
    anchor_attr = str(sk.get("anchor") or "公司名")
    ex = (learned.get("value_examples") or {}).get(anchor_attr) or []
    lens = [len(norm_stream(str(v))) for v in ex if len(norm_stream(str(v))) >= NAME_MIN_LEN]
    if lens:
        name_max = max(NAME_MAX_LEN_DEFAULT, min(12, max(lens) + 2))
    p_lens = []
    for nm, _tt in slots[:3]:
        for v in ((learned.get("value_examples") or {}).get(nm) or []):
            n = len(norm_stream(str(v)))
            if n >= NAME_MIN_LEN:
                p_lens.append(n)
    name_len = int(round(sum(p_lens) / len(p_lens))) if p_lens else NAME_LEN_DEFAULT
    name_len = max(2, min(4, name_len))

    anchors: Dict[str, List[Tuple[re.Pattern, str]]] = {}
    person_attrs = {a for pair in slots for a in pair}
    for a in attrs:
        rx = _anchors_for(a, {"titles_re": _build_titles_re(learned, profile)})
        if rx:
            anchors[a] = rx

    return {
        "attrs": attrs,
        "anchor_attr": anchor_attr,
        "person_attr_set": person_attrs,
        "name_max_len": name_max,
        "body_min_len": BODY_MIN_LEN_DEFAULT,
        "name_len": name_len,
        "page_constants": [norm_stream(str(c)) for c in
                           (learned.get("page_constants") or [])],
        "anchors": anchors,
        "person_slots": slots,
        "titles_re": _build_titles_re(learned, profile),
        "profile_attrs": prof_attrs,
        "required": [str(a) for a in (sk.get("required") or [])],
        "sources": {
            "skeleton": "layout_contract.S（例题归纳）" if order else "内建（无契约）",
            "n_person_slots": len(slots),
        },
    }


# ============================================================
# 记录切分（几何：短行 = 记录起点）
# ============================================================
def _is_noise(t: str) -> bool:
    if len(t) < NAME_MIN_LEN:
        return True
    if not _CJK_RE.search(t):
        return True
    core = [ch for ch in t if _CJK_RE.match(ch)]
    return all(ch in _NUMERAL_CHARS for ch in core)


def _has_strong_anchor(t: str) -> bool:
    n = norm_stream(t)
    if re.search(_DATE, n):
        return True
    return any(w in n for w in _STRONG_ANCHOR_WORDS)


# P12（2026-09-30，R1 形态 F1）：**裸碎行**——公司名被 OCR 拆碎后剩下的
# 「公司」「有限公司」「總號」残段不是记录起点（8期实测：碎行「公司」过了
# 全部 5 判据，把粤东烟草整条撕碎，真名行落 orphan）。
# ★ 不动 `_STRONG_ANCHOR_WORDS` 共享词表：该表被名录页判定 / titles 正则 /
#   rule_report 说明书 / 页脚修剪（:545）多处消费，逐方判定后在**本判据内**
#   单独成表（segment 专用判据），共享表语义逐字节不变：
#   - 有限公司/總號：本就被 ②（子串命中「有限/总號」）排除，入表只为显式可读；
#   - 裸「公司」是唯一漏网形态（无任何锚词是其子串），F1 真正要杀的就是它。
_BARE_NAME_FRAGS = frozenset(dict.fromkeys(norm_stream(w) for w in (
    "公司", "有限公司", "總號", "總公司",
)))
# P12（2026-09-30，R1 形态 F2/F3）：被拆开的名行**前半**——
# 「粤東烟草有限」「福華紙烟有限」（以「有限」结尾）与「時益號有限公」
# （「有限公司」被拆成 有限公+司，R1 F2/F3 行证据 [0]/[5]/[17]）。
# 此类短行豁免判据②（有限作为公司名组织形式字出现在**名行尾部**，
# 与正文「爲股分有限公司從事…」的行中情形相反）；裸「有限」「有限公」
# 本身仍是碎行不豁免。表按 R1 行证据锁定，不许凭空扩。
_LIMIT_TAILS = tuple(norm_stream(w) for w in ("有限", "有限公"))


def _is_bare_frag(n: str) -> bool:
    return n in _BARE_NAME_FRAGS


def _is_limit_tail(n: str) -> bool:
    return n.endswith(_LIMIT_TAILS) and n not in _LIMIT_TAILS


def _looks_like_name(t: str, spec: dict) -> bool:
    n = norm_stream(t)
    if not (NAME_MIN_LEN <= len(n) <= spec["name_max_len"]):
        return False
    if _is_noise(n):
        return False
    if n in spec["page_constants"]:
        return False
    if _is_bare_frag(n):                         # P12 F1：裸碎行判非起点
        return False
    if _is_limit_tail(n):                        # P12 F2：名行前半，豁免②
        return True
    return not _has_strong_anchor(n)


# P12（2026-09-30，R1 形态 F4/页A）：**无锚记录体兜底开条**——名行物理缺失
# （preannotate 列流层根因，判据不可修）时，典当/织布类记录只剩
# 「日期+创办人+本典…」体。正文行「創辦」基本只出现在记录头一行内
# （R1 探针实测可覆盖页A 全部 3 条 + 8期 3 处，误开风险低），故：
# 正文行以完整纪年日期开头、日期后 ≤16 字内出现「创办」、且其前行不是
# 记录起点 → 强制开新记录（公司名留空 + 低置信留痕 `open_rule`）。
_FB_OPEN_MARK = norm_stream("创办")
_FB_OPEN_RE = re.compile("^" + _DATE + r".{0,16}?" + _FB_OPEN_MARK)
FB_OPEN_RULE = "date_body_fallback"


def segment_records(units: List[dict], spec: dict) -> List[dict]:
    """行序列（阅读序）→ 记录列表 `[{name, units, text}]`。

    判据（全部无量纲、可解释）：
      ① 该行长度落在 `[2, name_max_len]`（金标准实测公司名 3–6 字）；
      ② 该行**不含任何强锚词**（日期/注册/从事/资本/地址头/职衔…）；
        ★ P12 F2 豁免：以「有限/有限公」**结尾**的短行是被 OCR 拆开的
          名行前半（粤東烟草有限/福華紙烟有限/時益號有限公），豁免本判据；
      ③ 该行不是纯数字/标点（「十二」是日期续接，不是名字）；
      ④ 该行不是学到的页面常量（表头「公司注册各案摘要」）；
      ⑤ **紧跟其后的那一行必须是正文行**（≥ `body_min_len` 字），
         ★ P12 F3 放宽：或下一行是 ≤2 字的**后缀碎片**（且自身不是裸碎行）
         且再下一行是正文（「時益號有限公」+「司」+正文）。
         ⚠ 只放宽这一层，不能放宽成"后续若干行里有一个长行"——实测教训：
         页首碎片「飞龙仁幸」的反例恰是 F1 的「公司」碎行垫在中间（0002
         实测），F1 把裸「公司」判非起点后，它既当不成起点也当不成后缀
         碎片，放宽的前提自动解除（tests/test_p12_boundary.py 锁此因果）。
         正文行恒定长（24–27 字）与名字行（3–8 字）反差极大，这一条同时
         排除页脚（「戊申第四册」）与孤立续接碎片。
    """
    n = len(units)
    starts: List[int] = []
    f3_suffix_at: set = set()                        # F3 起点的后缀碎片行号（正文头 = 其后一行）
    for i, u in enumerate(units):
        if not _looks_like_name(u["text"], spec):
            continue
        if i + 1 >= n:
            continue                                 # 末尾孤立 → 页脚，不是记录
        if len(norm_stream(units[i + 1]["text"])) >= spec["body_min_len"]:
            starts.append(i)                         # 下一行是正文 → 记录起点
            continue
        # P12 F3：下一行 ≤2 字后缀碎片（非裸碎行）+ 再下一行是正文
        if (i + 2 < n
                and len(norm_stream(units[i + 1]["text"])) <= 2
                and not _is_bare_frag(norm_stream(units[i + 1]["text"]))
                and len(norm_stream(units[i + 2]["text"])) >= spec["body_min_len"]):
            starts.append(i)
            f3_suffix_at.add(i + 1)                  # 碎片行的下一行 = 本记录正文头
    # P12 F4：无锚记录体兜底开条（`^日期 ≤16字 创办` 且前行非起点）。
    # ★ 还要求**模式行之后有正文行**（与判据⑤同构）：记录体实测多行
    #   （24–27 字/行），孤悬页尾的模式行是页脚/残段（同⑤「末尾孤立 → 页脚」），
    #   开条只认「后面还有正文」的真记录体。升序扫描、边扫边并入起点集：
    #   兜底开出的条目本身是起点，紧随其后的同模式正文行「前行已是起点」
    #   → 不连开。
    forced: List[int] = []
    for i, u in enumerate(units):
        if i == 0 or i in starts:
            continue
        if (i - 1) in starts:
            continue                                 # 前行是起点 → 属上条的正文
        if (i - 1) in f3_suffix_at:
            continue                                 # 前行是 F3 碎片 → 属该名行的正文头
        if i + 1 >= n or len(norm_stream(units[i + 1]["text"])) < spec["body_min_len"]:
            continue                                 # 之后无正文 → 页脚残段，不开
        if _FB_OPEN_RE.match(norm_stream(u["text"] or "")):
            starts.append(i)
            forced.append(i)
    starts.sort()

    def _mk_rec(s: int, e: int) -> dict:
        body = units[s + 1:e]
        rec = {
            "name": "" if s in forced else units[s]["text"].strip(),
            "name_index": s,
            "end_index": e,
            "units": [units[s]] + body,
            "text": "".join((u["text"] or "").strip() for u in units[s:e]),
            "body_text": "".join((u["text"] or "").strip() for u in body),
        }
        if s in forced:                              # 低置信留痕（挂 seg/明细，不进属性 dict）
            rec["open_rule"] = FB_OPEN_RULE
        return rec

    recs = [_mk_rec(s, starts[k + 1] if k + 1 < len(starts) else n)
            for k, s in enumerate(starts)]
    # 末条记录的**尾随页脚**：短行且不含任何锚词（实测「戊申第四册」「十二」
    # 这类年号/册次）。不裁掉会被并进末条记录的正文，让"这条记录正文多少字"
    # 失真；含锚词的短行（如「十二日註冊」）是真续接，必须保留。
    if recs:
        last = recs[-1]
        while len(last["units"]) > 1:
            t = last["units"][-1]["text"].strip()
            n = norm_stream(t)
            if len(n) < spec["body_min_len"] and not _has_strong_anchor(n):
                last["trimmed"] = t + (last.get("trimmed") or "")
                last["units"].pop()
                continue
            break
        last["text"] = "".join((u["text"] or "").strip() for u in last["units"])
        last["body_text"] = "".join((u["text"] or "").strip()
                                    for u in last["units"][1:])
        last["end_index"] = last["name_index"] + len(last["units"])
    return recs


# ============================================================
# 属性抽取（模板序 + 锚词 + 顺序对齐）
# ============================================================
def _anchor_bounds(stream_n: str, spec: dict) -> List[int]:
    """全流中**任一锚词**的起点（供 `after` 模式定右界）。

    ★ P11（2026-09-29）：「股銀」命中「官股銀／商股銀」内部的内截命中被剔除，
      不作右界（否则 样例页_0002 资本被切剩「股分官」，见 `_CAP_HEAD` 注）。
    """
    pos: List[int] = []
    for rx_list in spec["anchors"].values():
        for rx, _mode in rx_list:
            pos.extend(m.start() for m in rx.finditer(stream_n))
    tr = spec.get("titles_re")
    if tr:
        pos.extend(m.start() for m in re.finditer(tr, stream_n))
    pos.extend(m.start() for m in re.finditer(_PERSON_TAIL, stream_n))
    return sorted(p for p in set(pos) if not _is_inner_cap_hit(stream_n, p))


def _is_inner_cap_hit(stream_n: str, p: int) -> bool:
    """位置 p 是否为「官股銀／商股銀」组合的内部「股銀」命中（内截，不作右界）。"""
    return (p > 0 and stream_n[p - 1] in _CAP_INNER_PREV_CHARS
            and stream_n.startswith(_CAP_INNER_TAIL, p))


_REG_TAIL_RE = re.compile(r"[註注册冊]{1,2}$")


# P12（2026-09-30，R1 形态 F6）：**记录头行内串道**——OCR 把上条尾+下条头
# 并进一行（样例页_0003 实测：溥利流头带燮昌注册时间残尾
# 「緒三十年十二月初二日註冊光緒三十年七月初一日試用知…」），下条记录的
# 属性扫描从流头开始，残尾里的「日期+註冊」会被注册时间锚抢先命中（张冠李戴）。
# 有界值级剥离：仅剥**流头** ≤16 字（残尾通常被截断变短）且以注册标记结尾、
# 其后**紧跟另一段纪年日期**的前缀——最后这个前瞻是自保护：本记录自己的
# 注册时间「…註冊」后面接的是正文（地址/职衔），不是日期，不会被误剥。
_HEAD_RESIDUE_RE = re.compile("^.{0,16}?" + _REG_MARK + "(?=" + _DATE + ")")


def _strip_head_residue(stream: str, stream_n: str) -> Tuple[str, str]:
    m = _HEAD_RESIDUE_RE.match(stream_n)
    if not m:
        return stream, stream_n
    k = m.end()                                      # norm 与原文 1:1 等长 → 坐标通用
    log.info("head residue stripped: %r", stream[:k])
    return stream[k:], stream_n[k:]


def _strip_reg_tail(val: str) -> str:
    """P11（2026-09-29，R1 模式①）：mode=self 抽取值剥离尾部注册标记。

    注册时间正则把「註冊」尾标吃进匹配区间（`.{0,6}?' + _REG_MARK`），
    尾标是**分隔符不是值**——pred 恒带「註冊」尾标是 fn9+fp9 的口径差主因。
    评测层（experiment.py）两侧同剥，fold4 的带尾 gold 仍命中。
    """
    return _REG_TAIL_RE.sub("", val)


def _value_stop_pos(stream_n: str, start: int) -> Optional[int]:
    """`start` 之后第一个右界终结词出现位（无则 None）。词表见 `_VALUE_STOP_WORDS`。"""
    best: Optional[int] = None
    for w in _VALUE_STOP_WORDS:
        i = stream_n.find(w, start)
        if i >= 0 and (best is None or i < best):
            best = i
    return best


def _take(stream_n: str, cursor: int, rx_list) -> Optional[Tuple[int, int, str, str]]:
    """在 `cursor` 之后取第一个锚词命中 `(start, end, token, mode)`。"""
    for rx, mode in rx_list:
        m = rx.search(stream_n, cursor)
        if m:
            return m.start(), m.end(), m.group(0), mode
    return None


def _bound_after(pos: int, bounds: List[int], end_of: int) -> int:
    for p in bounds:
        if p > pos:
            return p
    return end_of


def extract_persons(stream_n: str, spec: dict, taken: List[Tuple[int, int]],
                    stream: str = "") -> dict:
    """注册人一族（一/二/三/其他 + 各自功名）。

    机制（两条，均**通用**不特例）：
      A. **职衔驱动**：在职衔词表命中处，其后 `name_len` 字即人名
         （「翰林院侍读学士黄思永创办」→ 功名=翰林院侍读学士，名=黄思永）；
      B. **无人名职衔时按人名长度切**：`创办` 之前的那段连续汉字按
         `name_len`（由金标准值长度推，实测 3）切块
         （「张石君荣瑞馨荣宗敬荣德生创办」→ 4 人）。
    两种机制都以 **`创办` 锚位置** 为界——人名必然紧邻 `创办` 之前。

    ⚠ **定位在归一文本、取值从原文切**：人名/职衔必须保持原字形
    （实测教训：直接返回归一文本的切片，会把「張石君」写成「张石君」，
    违反"禁止繁简转换"的照录纪律）。
    """
    raw = stream if len(stream) == len(stream_n) else stream_n
    m = re.search(_PERSON_TAIL, stream_n)
    if not m:
        return {}, []
    head = m.start()
    lo = 0
    for s, e in taken:
        if e <= head and e > lo:
            lo = e
    region = stream_n[lo:head]
    region_raw = raw[lo:head]
    if not region.strip():
        return {}, []

    persons: List[Tuple[str, str]] = []              # [(功名, 人名)]
    title_rx = spec.get("titles_re")
    hits: List[Tuple[int, int]] = []
    if title_rx:
        hits = [(mm.start(), mm.end()) for mm in re.finditer(title_rx, region)]
    if hits:
        for i, (s, e) in enumerate(hits):
            stop = hits[i + 1][0] if i + 1 < len(hits) else len(region)
            nm = region_raw[e:min(stop, e + spec["name_len"] + 1)][:spec["name_len"]]
            if len(nm) >= NAME_MIN_LEN:
                persons.append((region_raw[s:e], nm))
        # 末位人名兜底：最后一个人**必然紧邻「创办」之前**。词表漏掉某个冷僻
        # 职衔（实测：「花翎游击」OCR 成「花餉游擊」→ 不在词表）时，
        # 该职衔+名字会整段留在尾部；取紧邻「创办」的 `name_len` 字兜住。
        last_end = (hits[-1][1] + spec["name_len"]) if hits else 0
        tail = region_raw[last_end:]
        if len(tail) >= spec["name_len"] + 1:
            nm = tail[-spec["name_len"]:]
            if _CJK_RE.search(nm):
                persons.append(("", nm))
    else:
        # ★ P5（2026-09-26）：切块前先剥**日期语段** —— 取证：
        #   51 份产物 8 对可疑切碎，全部是日期片被 `name_len` 切进 注册人一/二/三/其他，
        #   如「三十一」「年十二」「月十七」「日註冊」）。三道防线：
        #   ① 剥整段纪年（`_DATE`）+ 注册标记；② 剥月日片段（月/日不入人名，安全）；
        #   ③ 剥后日期构字占比仍过高 → 整段不是裸人名列表（「開辦二十年暫停…」类
        #      散文），切块只会产出伪人名 → 整段放弃（宁缺勿猜）。
        #   剥除在 `region`（归一坐标，与 `region_raw` 1:1 等长）上定位，取字仍从原文切。
        drop = [False] * len(region)
        for rx in (re.compile(_DATE), re.compile(_REG_MARK),
                   re.compile(_MON_DAY_FRAG)):
            for m in rx.finditer(region):
                for i in range(m.start(), m.end()):
                    drop[i] = True
        run = "".join(ch for i, ch in enumerate(region_raw)
                      if not drop[i] and _CJK_RE.match(ch))
        if run:
            n_date = sum(1 for c in run if c in _NUMERAL_CHARS)
            if n_date >= _DATEISH_REGION_RATIO * len(run):
                return {}, []
        chunk = spec["name_len"]
        # ④ 裸人名列表的**长度上限** = 槽位数 × 人名长（金标准实测 4 人 × 3 字）。
        #    超长即混有散文/残段（实测「開辦二十年暫停…添股復開張翼順…」整段
        #    被切成 8 个伪「人」）→ 整段放弃（宁缺勿猜）。
        if len(run) > chunk * len(spec["person_slots"]):
            return {}, []
        for i in range(0, len(run) - chunk + 1, chunk):
            nm = run[i:i + chunk]
            if all(c in _NUMERAL_CHARS for c in nm):
                continue                               # 纯日期构字块 = 残片，不是人名
            persons.append(("", nm))
    if not persons:
        return {}, []

    out: Dict[str, str] = {}
    slots = spec["person_slots"]
    for i, (title, nm) in enumerate(persons):
        if i >= len(slots):
            break
        nm_attr, tt_attr = slots[i]
        out[nm_attr] = nm
        if title:
            out[tt_attr] = title
    return out, [h[0] for h in persons]              # 第二个返回 = 人名片段起点（供取字）


def extract_attrs(rec: dict, spec: dict, stream: str) -> Tuple[Dict[str, str], dict]:
    """单条记录 → `{属性: 值}`（值 = 原文逐字切片）。"""
    stream_n = norm_stream(stream)
    # P12 F6：剥流头的前条残尾（若形态命中）；剥的是同一前缀，坐标仍 1:1。
    stream, stream_n = _strip_head_residue(stream, stream_n)
    bounds = _anchor_bounds(stream_n, spec)
    out: Dict[str, str] = {}
    detail: Dict[str, Any] = {"anchors": [], "empty": []}
    cursor = 0
    taken: List[Tuple[int, int]] = []
    pos = stream_n.find(norm_stream(rec["name"]))
    if pos >= 0:
        taken.append((pos, pos + len(norm_stream(rec["name"]))))
        cursor = max(cursor, pos + len(norm_stream(rec["name"])))

    if rec.get("name"):
        out[spec["anchor_attr"]] = rec["name"]

    for attr in spec["attrs"]:
        if attr == spec["anchor_attr"]:
            continue
        if attr in spec["person_attr_set"]:
            continue                                 # 人名族单独处理
        rx_list = spec["anchors"].get(attr)
        if not rx_list:
            detail["empty"].append(attr)
            continue
        got = _take(stream_n, cursor, rx_list)
        if not got:
            # 回退：本页行序偶有错位（表格页实测），游标之后找不到就全流再找一次，
            # 但不许落在别的属性已占用的区间里（防同段被两条属性重复认领）。
            got = _take(stream_n, 0, rx_list)
            if got and any(s0 <= got[0] < e0 for s0, e0 in taken):
                got = None
        if not got:
            detail["empty"].append(attr)
            continue
        s, e, tok, mode = got
        if mode == "self":
            # P11：值剥尾部注册标记（尾标是分隔符不是值）；end 仍含尾标——
            # 游标越过整段匹配，防创立时间回捞同段。
            val, end = _strip_reg_tail(stream[s:e]), e
        else:
            # `after` 取值**含锚词**：金标准即是如此（注册资本「股分洋银十万元」
            # 含「股分」、地址「总号在…」含「总号在」、主营「从事…」含「从事」），
            # 不含锚词会与金标准系统性错开、被误判为"没抽到"。
            b = _bound_after(e, bounds, len(stream_n))
            end = min(b, s + MAX_VALUE_CHARS)
            # P11：右界终结词截断（过冲尾巴「每股洋銀一百圓」「獨力」「爲」
            # 「分號擬設懷慶府」等 ~18 例，见 `_VALUE_STOP_WORDS` 证据注）。
            stop = _value_stop_pos(stream_n, e)
            if stop is not None:
                end = min(end, stop)
            val = stream[s:end]
        val = val.strip()
        if not val:
            detail["empty"].append(attr)
            continue
        out[attr] = val
        taken.append((s, end))
        cursor = max(cursor, end)
        detail["anchors"].append({"attr": attr, "token": tok, "mode": mode,
                                  "start": s, "value": val[:40]})

    persons, _frag = extract_persons(stream_n, spec, taken, stream)
    for k, v in persons.items():
        if v and not out.get(k):
            out[k] = v
            detail["anchors"].append({"attr": k, "token": "(人名)", "mode": "person",
                                      "start": -1, "value": v})
    return out, detail


def extract_one(attr: str, text: str, spec: dict) -> Optional[str]:
    """从**一小段文本**里精确切出某属性的值（L1 指针补全的"收缩器"）。

    为什么需要它：L1 的模型只回答"值在第几行"，而竖排古籍的**一行常把日期、
    职衔、人名、地址塞在一起**；照搬整行只能得到一段含糊的长文本
    （实测：把「注册人二」填成整行后，属性精确 0.953 → 0.827）。
    故分工是：**定位归模型，取值仍归这套锚词实现**——不另造第二套取值逻辑
    （纪律：单一实现入口）。

    取不到 → `None`：**宁可空着，也不塞一段含糊的长文本**。
    """
    if not text or not text.strip():
        return None
    if attr == spec.get("anchor_attr"):
        # 记录名（公司名）**本身就是值**，没有更细的锚词可收缩——
        # 直接照录（它在 L0 里来自几何分割，不是锚词抽取）。
        return text.strip() or None
    n = norm_stream(text)
    if attr in (spec.get("person_attr_set") or set()):
        try:
            persons, _frag = extract_persons(n, spec, [], text)
        except Exception:                                  # pragma: no cover
            return None
        return (persons.get(attr) or "").strip() or None
    rx_list = (spec.get("anchors") or {}).get(attr)
    if not rx_list:
        return None
    got = _take(n, 0, rx_list)
    if not got:
        return None
    s, e, _tok, mode = got
    if mode == "self":
        val = _strip_reg_tail(text[s:e])
    else:
        end = min(_bound_after(e, _anchor_bounds(n, spec), len(n)),
                  s + MAX_VALUE_CHARS)
        stop = _value_stop_pos(n, e)
        if stop is not None:
            end = min(end, stop)
        val = text[s:end]
    return val.strip() or None


def records_of(units: List[dict], spec: dict) -> Tuple[List[dict], List[dict], List[dict]]:
    """行序列 → `(records, details, segs)`。

    records 与 `split_records.split_page` 同构；segs 保留记录的行区间
    （供调用方核对"有没有哪些行没被任何记录覆盖"——绝不丢字的可见化）。
    """
    segs = segment_records(units, spec)
    records: List[dict] = []
    details: List[dict] = []
    for seg in segs:
        # ★ 取值前裁掉粘在正文里的页面常量（P0-2）。行文本本身不动 ——
        #   只影响"这条记录拿去抽属性的那段文本"，故记录边界与行索引全不受影响。
        text, cut = strip_page_constants(seg["text"], spec)
        rec, det = extract_attrs(seg, spec, text)
        rec = {k: v for k, v in rec.items() if v}
        for a in spec["required"]:                   # 必填缺失 → 显式空串（可见）
            rec.setdefault(a, "")
        det["name"] = seg["name"]
        if seg.get("open_rule"):
            # P12 F4：兜底开条的低置信留痕挂**明细**（不进属性 dict——
            # anchor_page 会把记录 dict 的每个键都当属性生成草稿条目，
            # 元数据键进去就会泄漏成伪属性；seam 信封 record 无元数据槽位，
            # 下游零改动，本键止于 L0 产物层）。
            det["open_rule"] = seg["open_rule"]
        det["text_chars"] = len(seg["text"])         # 原始行区间长度（未裁）
        if cut:
            # 裁掉了什么必须留痕（否则"值为什么变短了"无从审计）
            det["page_constants_cut"] = cut
            det["cut_chars"] = sum(len(c) for c in cut)
        det["n_units"] = len(seg["units"])
        # 行区间（**行号索引与 `units` 同一列表**）——L1 指针补全需要它把
        # "这条记录在第几行"讲给模型听，模型才能据此报行号。
        det["name_index"] = seg["name_index"]
        det["end_index"] = seg["end_index"]
        if rec:
            records.append(rec)
            details.append(det)
    return records, details, segs


# ============================================================
# 页级入口（与 split_records.split_page 同构）
# ============================================================
def split_page(stem: str, learned: Optional[dict], profile: dict, *,
               structured_dir=None, outbox_dir=None,
               units: Optional[List[dict]] = None) -> Dict[str, Any]:
    """页 stem → `{records, parse_meta, units, details}`（零 LLM 调用）。

    `units` 可外部传入（`preannotate_gen` 已有行时省一次读盘）。
    """
    spec = build_spec(learned, profile)
    if units is None:
        import preannotate as PA
        units = PA.page_lines(stem, structured_dir, outbox_dir)
    units = [u for u in units if (u.get("text") or "").strip()]
    if not units:
        return {"records": [], "details": [], "units": [],
                "parse_meta": {"parse_ok": False, "mode": MODE_RULE,
                               "reason": "no_lines", "n_records": 0}}
    records, details, segs = records_of(units, spec)

    # 兜底：连一个记录起点行都找不到（实测 0004 页 OCR 把公司名并进了正文列，
    # 全页没有短行）→ **不做硬失败**，退化为"整页一条"，属性照抽。
    # 理由：宁可给一份低置信草稿让人改，也不要让这一页在批处理里变成空白
    # （用户纪律：人工兜底永远保留，且失败必须可见）。
    fallback = ""
    if not records:
        seg = {"name": "", "name_index": 0, "end_index": len(units),
               "units": units, "body_text": "",
               "text": "".join((u["text"] or "").strip() for u in units)}
        # 兜底同样要裁页面常量（与 `records_of` 同一实现，不另写一份）
        fb_text, fb_cut = strip_page_constants(seg["text"], spec)
        rec, det = extract_attrs(seg, spec, fb_text)
        rec = {k: v for k, v in rec.items() if v}
        if fb_cut:
            det["page_constants_cut"] = fb_cut
            det["cut_chars"] = sum(len(c) for c in fb_cut)
        for a in spec["required"]:
            rec.setdefault(a, "")
        if rec:
            records, details, segs = [rec], [det], [seg]
            fallback = "整页一条（未找到记录起点行）"

    # 孤儿文本可见化（绝不丢字的**机械自证**）：首条记录之前 / 末条记录之后
    # 未被任何记录覆盖的行。实测 0003（表格页）行序错位时，最右一列会整段
    # 落在这里——不静默吞掉，而是回传字数与样文，界面/待核据此判断。
    orphan_head = orphan_tail = ""
    if segs:
        orphan_head = "".join((u["text"] or "").strip()
                              for u in units[:segs[0]["name_index"]])
        orphan_tail = ((segs[-1].get("trimmed") or "")
                       + "".join((u["text"] or "").strip()
                                 for u in units[segs[-1]["end_index"]:]))
    n_orphan = len(orphan_head) + len(orphan_tail)

    # P12 F5（2026-09-30，R1 形态 F5×2：样例页_0002 g0 / 样例页_0003 g0）：跨页接续
    # **可见化**——上页尾值（「二日注册」「续招…」）落到本页头时提示「接上页」。
    # ★ 呈现层只加提示：绝不自动合并、绝不写 page_exclusions.json（人裁专属）。
    orphan_head_hint = ""
    if orphan_head and len(orphan_head) <= 40:
        oh = norm_stream(orphan_head)
        if (re.search(_REG_MARK, oh) or re.search(_DATE + r"$", oh)
                or oh.startswith(norm_stream("续招"))):
            orphan_head_hint = ("疑似接上页记录（上页尾值落到本页头；"
                                "已按现状并入首条记录，未自动合并）")

    # 页级自检：骨架属性填充率（供界面标低置信，宁可见不静默）
    n_core = len(spec["attrs"]) or 1
    filled = sum(1 for r in records for a in spec["attrs"] if str(r.get(a) or "").strip())
    fill_rate = filled / (len(records) * n_core) if records else 0.0
    n_fb_open = sum(1 for d in details if d.get("open_rule"))
    if records and fill_rate >= 0.55 and not fallback and not n_fb_open:
        conf = "high"
    elif records and fill_rate >= 0.3 and not fallback and not n_fb_open:
        conf = "medium"
    else:
        conf = "low"
    meta = {
        "parse_ok": bool(records), "mode": MODE_RULE,
        "n_records": len(records), "n_units": len(units),
        "fill_rate": round(fill_rate, 4), "confidence": conf,
        "attrs": spec["attrs"], "anchor_attr": spec["anchor_attr"],
        "name_max_len": spec["name_max_len"], "name_len": spec["name_len"],
        "n_person_slots": len(spec["person_slots"]),
        "skeleton_source": spec["sources"]["skeleton"],
        "orphan_chars": n_orphan,
        "orphan_head": orphan_head[:60], "orphan_tail": orphan_tail[:60],
        "orphan_head_hint": orphan_head_hint,
        "n_fallback_openings": n_fb_open,
        "records_fallback": fallback,
        "reason": "" if records else "no_record_boundary",
    }
    if not records:
        meta["sample_units"] = [u["text"][:24] for u in units[:8]]
    return {"records": records, "details": details, "units": units,
            "parse_meta": meta}


def summary(meta: dict) -> str:
    """一行摘要（日志/界面用）。"""
    if not meta:
        return "L0 未运行"
    return ("L0 规则切分：记录 %s / 行 %s，骨架填充率 %s（%s）"
            % (meta.get("n_records"), meta.get("n_units"),
               meta.get("fill_rate"), meta.get("confidence")))
