# -*- coding: utf-8 -*-
"""S2 规则引擎层（复杂版面方案 §4.3，阶段一 1b，2026-09-04）。

《OCR 识别规则笔记》逐节代码化：全部纯函数、零 API 成本、纯 stdlib。
每个规则模块输出统一结果类型 RuleResult（三态）：
    PASS    值通过校验，原样保留
    FIXED   确定性修正完成，fixes 记录全部改动明细（双向：修正 + 报告）
    SUSPECT 进待核队列（S4 待核清单由此自动生成），值不被擅改

词表数据在 rules_data/*.json（数据与逻辑分离）：本版内置方案点名的种子条目，
《OCR 识别规则笔记》完整词表到位后按同结构扩充 JSON 即可，不改代码。
加载失败/缺失一律回退空规则并告警——规则层绝不阻断主流程。

对接：FieldSchema 由文献类型档案的属性集生成（profile_store，单一来源两处
使用：S3 LLM 抽取的输出契约 + 本层校验规则）。1c 对账器消费本模块。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("rule_engine")

RULES_DATA_DIR = Path(__file__).parent.resolve() / "rules_data"

STATUS_PASS = "PASS"
STATUS_FIXED = "FIXED"
STATUS_SUSPECT = "SUSPECT"


# ============================================
# 统一结果类型
# ============================================
@dataclass
class RuleResult:
    """单条规则对单值的处理结果。value 恒为处理后的值（SUSPECT 时 = 原值）。"""
    rule: str                     # 规则模块名，如 "CharNormalizer"
    status: str                   # PASS | FIXED | SUSPECT
    value: Any = None             # 处理后的值
    fixes: List[Dict[str, Any]] = field(default_factory=list)  # FIXED 明细
    reason: str = ""              # SUSPECT 原因 / FIXED 摘要

    @property
    def needs_review(self) -> bool:
        return self.status == STATUS_SUSPECT

    def as_dict(self) -> Dict[str, Any]:
        """JSON 序列化形式（报告落盘 / S4 待核清单消费）。"""
        return {"rule": self.rule, "status": self.status, "value": self.value,
                "fixes": self.fixes, "reason": self.reason}


def _result(rule: str, status: str, value: Any,
            fixes: Optional[List[dict]] = None, reason: str = "") -> RuleResult:
    return RuleResult(rule=rule, status=status, value=value,
                      fixes=fixes or [], reason=reason)


# ============================================
# rules_data 加载（缺失/损坏回退空规则，绝不阻断）
# ============================================
def load_rules_data(name: str, data_dir: Optional[Path] = None) -> dict:
    """读 rules_data/<name>.json；不存在或损坏 → {} 并告警。"""
    path = (data_dir or RULES_DATA_DIR) / f"{name}.json"
    if not path.exists():
        log.warning("[rule_engine] 词表数据缺失: %s（规则将按空表运行）", path.name)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[rule_engine] 词表数据损坏: %s（%s）", path.name, e)
        return {}


# ============================================
# 1. CharNormalizer —— OCR 误识字修正表
#    （确定性替换字典：修正 + 报告改动，双向留痕）
# ============================================
class CharNormalizer:
    """逐字符查表替换。表 {误识字: 正确字}（如 OCR 把原文「昇」识成「升」）。"""

    def __init__(self, table: Optional[Dict[str, str]] = None,
                 data_dir: Optional[Path] = None):
        data = load_rules_data("char_corrections", data_dir)
        self.table: Dict[str, str] = dict(table) if table is not None else {
            k: v for k, v in data.items() if not k.startswith("_")}

    def normalize(self, text: str) -> RuleResult:
        if not text:
            return _result("CharNormalizer", STATUS_PASS, text)
        out, fixes = [], []
        for i, ch in enumerate(text):
            fixed = self.table.get(ch)
            if fixed:
                out.append(fixed)
                fixes.append({"pos": i, "from": ch, "to": fixed})
            else:
                out.append(ch)
        if not fixes:
            return _result("CharNormalizer", STATUS_PASS, text)
        return _result("CharNormalizer", STATUS_FIXED, "".join(out), fixes,
                       reason=f"修正 {len(fixes)} 处误识字: "
                              + "、".join(f"{f['from']}→{f['to']}" for f in fixes))


# ============================================
# 2. TitleValidator —— 职衔体系（候选 vs 候补）
#    词表校验 + 近形字修正建议
# ============================================
def _edit_distance_le1(a: str, b: str) -> bool:
    """编辑距离是否 ≤1（小片段专用，O(n) 双指针）。"""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la   # 保证 a 短
    for i in range(lb):
        if a[:i] + a[i:] == b[:i] + b[i + 1:]:   # b 去掉一位 = a
            return True
    return False


# 疑似职衔起点：候选/候补（含繁体/异体）
_TITLE_HEAD_RE = re.compile(r"候[选選補补]")
_TITLE_MAX_LEN = 6   # 最长职衔窗口（候选/候补 + 4 字官名）


class TitleValidator:
    def __init__(self, titles: Optional[List[str]] = None,
                 data_dir: Optional[Path] = None):
        if titles is not None:
            self.titles = list(titles)
        else:
            data = load_rules_data("titles", data_dir)
            self.titles = list(data.get("titles", []))

    def validate(self, text: str) -> RuleResult:
        """「候选/候补」起点做多长度窗口切分：任一窗口在词表 = PASS；
        无命中时按词表项长度对齐找近形（编辑距离≤1）→ SUSPECT + 修正建议。
        （贪婪正则会把官名后的姓氏吞进 token，故必须多窗口切分。）"""
        if not text:
            return _result("TitleValidator", STATUS_PASS, text)
        suspects: List[str] = []
        for m in _TITLE_HEAD_RE.finditer(text):
            start = m.start()
            windows = [text[start:start + L]
                       for L in range(2, _TITLE_MAX_LEN + 1)
                       if start + L <= len(text)]
            if any(w in self.titles for w in windows):
                continue
            near = near_tok = None
            for t in self.titles:
                w = text[start:start + len(t)]
                if w and _edit_distance_le1(w, t):
                    near, near_tok = t, w
                    break
            if near:
                suspects.append(f"「{near_tok}」疑为「{near}」")
            else:
                shown = next((w for w in windows if len(w) >= 4),
                             windows[0] if windows else m.group(0))
                suspects.append(f"「{shown}」不在职衔词表")
        if not suspects:
            return _result("TitleValidator", STATUS_PASS, text)
        return _result("TitleValidator", STATUS_SUSPECT, text,
                       reason="；".join(suspects))


# ============================================
# 3. AddrSimplifier —— 地址粒度 v3
#    （保府县级与租界，去门牌街坊；基于研究侧
#    simplify_address.py 的规则概述重构入库）
# ============================================
# 门牌/街坊尾缀：街巷道路 + 门牌号 + 余尾（弄/号/楼等）
_DOOR_TAIL_RE = re.compile(
    r"[街巷道路][0-9〇〇零一二三四五六七八九十百]+号?.*$")


class AddrSimplifier:
    def __init__(self, concessions: Optional[List[str]] = None,
                 data_dir: Optional[Path] = None):
        if concessions is not None:
            self.concessions = list(concessions)
        else:
            data = load_rules_data("address_rules", data_dir)
            self.concessions = list(data.get("concession_keywords",
                                             ["租界"]))

    def simplify(self, addr: str) -> RuleResult:
        """地址粒度 v3：租界整段保留；有粒度字（省/府/县/州/厅）取到最后
        粒度字（去掉其后街坊门牌）；无粒度字则剥门牌尾缀；否则不动。"""
        if not addr:
            return _result("AddrSimplifier", STATUS_PASS, addr)
        if any(k in addr for k in self.concessions):
            return _result("AddrSimplifier", STATUS_PASS, addr,
                           reason="租界地址按 v3 规则整段保留")
        # 粒度字截断（最后出现的省/府/县/州/厅 之后）
        last = max((addr.rfind(c) for c in "省府县縣州厅廳"), default=-1)
        if last >= 0:
            base = addr[:last + 1]
            if base != addr:
                return _result("AddrSimplifier", STATUS_FIXED, base,
                               fixes=[{"from": addr, "to": base,
                                       "reason": "去街坊门牌（保县级）"}],
                               reason="地址粒度归一到县级")
            return _result("AddrSimplifier", STATUS_PASS, addr)
        stripped = _DOOR_TAIL_RE.sub("", addr).strip()
        if stripped and stripped != addr:
            return _result("AddrSimplifier", STATUS_FIXED, stripped,
                           fixes=[{"from": addr, "to": stripped,
                                   "reason": "去门牌尾缀"}],
                           reason="去门牌街坊尾缀")
        return _result("AddrSimplifier", STATUS_PASS, addr)


# ============================================
# 4. CurrencyParser —— 资本格式（只录总股额；官商合办分项）
#    中文金额解析迁移自 unit_builder §1（受控拷贝，规则化）；
#    「寧缺勿猜」纪律同源：缺数字的小数位按 0 计，解析不出 → None。
# ============================================
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_LOWUNIT = {"十": 10, "拾": 10, "百": 100, "千": 1000, "仟": 1000}
_CN_DEC = {"錢": 0.1, "钱": 0.1, "分": 0.01, "厘": 0.001, "毛": 0.001, "毫": 0.0001}
_AMOUNT_RUN_RE = re.compile(
    r"[一二三四五六七八九十百千萬万零兩两拾仟][一二三四五六七八九十百千萬万零兩两拾仟錢钱分厘毫]*")


def cn_amount_to_float(text: str) -> Optional[float]:
    """中文金额串 → 兩 为单位的 float（unit_builder 同名逻辑，受控拷贝）。

    "五十九萬七千三百十二兩九錢一分三厘" → 597312.913；
    "兩千" → 2000.0（兩后跟位值字符时视为数字 2）；解析不出 → None。
    """
    total = 0.0
    section = 0.0
    cur: Optional[int] = None
    for i, ch in enumerate(text):
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
        elif ch in _CN_LOWUNIT:
            section += (cur if cur is not None else 1) * _CN_LOWUNIT[ch]
            cur = None
        elif ch in ("萬", "万"):
            section += cur or 0
            total += section * 10000
            section = 0.0
            cur = None
        elif ch in ("兩", "两"):
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt in _CN_LOWUNIT:
                cur = 2
            else:
                total += section + (cur or 0)
                section = 0.0
                cur = None
        elif ch in _CN_DEC:
            if cur is not None:
                total += cur * _CN_DEC[ch]
                cur = None
    # 1b 适配修正：收尾残余并入（unit_builder 原版无此步——其金额串恒以
    # 兩/錢收尾不会触发；「兩千」这类位值收尾的串在此补结算 = 2000）
    total += section + (cur or 0)
    return total if total > 0 else None


def extract_amount(text: str) -> Tuple[str, Optional[float]]:
    """提取（金额原文, 金额归一）。取含銀衡单位的最长连续数字串。"""
    best, best_len = "", 0
    for m in _AMOUNT_RUN_RE.finditer(text):
        run = m.group(0)
        if ("兩" in run or "两" in run or "錢" in run or "钱" in run) \
                and len(run) > best_len:
            best, best_len = run, len(run)
    if not best:
        return "", None
    return best, cn_amount_to_float(best)


def extract_all_amounts(text: str) -> List[Tuple[str, Optional[float]]]:
    """提取语料中**全部**含銀衡单位的金额串（1c 数字回验用：
    抽取值需与语料中任一金额按数值等价比对，而非只对最长串）。"""
    out: List[Tuple[str, Optional[float]]] = []
    for m in _AMOUNT_RUN_RE.finditer(text):
        run = m.group(0)
        if ("兩" in run or "两" in run or "錢" in run or "钱" in run):
            out.append((run, cn_amount_to_float(run)))
    return out


# 官商合办特征（官股/商股/官商合办）
_JOINT_RE = re.compile(r"官商合[办辦]|官股|商股")


class CurrencyParser:
    """资本字段规则：文本含官商合办/官股/商股 → 分项解析（缺项 SUSPECT）；
    否则只录总股额；解析不出金额 → SUSPECT（宁缺勿猜）。"""

    def parse_capital(self, value: str) -> RuleResult:
        if not value:
            return _result("CurrencyParser", STATUS_PASS, value)
        if _JOINT_RE.search(value):
            official = self._segment(value, "official")
            merchant = self._segment(value, "merchant")
            off_amt = extract_amount(official)[1] if official else None
            mer_amt = extract_amount(merchant)[1] if merchant else None
            payload = {"official": off_amt, "merchant": mer_amt, "raw": value}
            if off_amt is None or mer_amt is None:
                missing = "官股" if off_amt is None else "商股"
                return _result("CurrencyParser", STATUS_SUSPECT, value,
                               reason=f"官商合办但{missing}金额未解析出（待核原文）")
            return _result("CurrencyParser", STATUS_PASS, payload,
                           reason="官商合办分项：官股+商股")
        raw, amount = extract_amount(value)
        if amount is None:
            return _result("CurrencyParser", STATUS_SUSPECT, value,
                           reason="未解析出中文金额（宁缺勿猜，进待核）")
        return _result("CurrencyParser", STATUS_PASS,
                       {"total": amount, "raw": raw}, reason=f"总股额 {raw}")

    @staticmethod
    def _segment(value: str, side: str) -> str:
        """切出官股/商股一侧文本片段（供金额提取）。
        优先「官股/商股」二字锚点（跳过「官商合办」词内部的官/商单字）；
        无股字锚点时退化为单字查找（跳过开头「官商」连写）。"""
        if side == "official":
            anchors = [("官股", "商股"), ("官", "商")]
        else:
            anchors = [("商股", "官股"), ("商", "官")]
        for a, other in anchors:
            idx = value.find(a)
            if idx < 0:
                continue
            nxt = value.find(other, idx + len(a))
            return value[idx:nxt] if nxt > idx else value[idx:]
        return ""


# ============================================
# 5. EnumConstraint —— 规范枚举同义词归一（公司性质用语表等）
#    枚举组结构: {"canonical": [...], "synonyms": {规范值: [同义词...]}}
# ============================================
class EnumConstraint:
    def __init__(self, canonical: List[str],
                 synonyms: Optional[Dict[str, List[str]]] = None):
        self.canonical = list(canonical)
        self._syn: Dict[str, str] = {}          # 同义词 → 规范值
        for norm, syns in (synonyms or {}).items():
            for s in syns:
                self._syn.setdefault(s, norm)   # 冲突保留首个并告警
                if self._syn[s] != norm:
                    log.warning("[rule_engine] 同义词「%s」冲突（%s/%s），保留首个",
                                s, self._syn[s], norm)

    def normalize(self, value: str) -> RuleResult:
        v = (value or "").strip()
        if not v:
            return _result("EnumConstraint", STATUS_PASS, value)
        if v in self.canonical:
            return _result("EnumConstraint", STATUS_PASS, value)
        norm = self._syn.get(v)
        if norm:
            return _result("EnumConstraint", STATUS_FIXED, norm,
                           fixes=[{"from": value, "to": norm,
                                   "reason": "同义词归一"}],
                           reason=f"归一为规范值「{norm}」")
        return _result("EnumConstraint", STATUS_SUSPECT, value,
                       reason=f"「{v}」不在规范枚举内")

    @classmethod
    def from_group(cls, group: dict) -> "EnumConstraint":
        return cls(group.get("canonical", []),
                   group.get("synonyms", {}))


def load_enum_groups(data_dir: Optional[Path] = None) -> Dict[str, EnumConstraint]:
    """读 enums.json 的枚举组表 → {组名: EnumConstraint}。"""
    data = load_rules_data("enums", data_dir)
    return {name: EnumConstraint.from_group(g)
            for name, g in data.items() if not name.startswith("_")
            and isinstance(g, dict)}


# ============================================
# 6. FieldSchema —— 输出契约（由档案属性集生成，单一来源两处使用）
#    pydantic 未引入（项目零依赖纪律），dataclass + 校验函数等效实现。
# ============================================
# 纪年正则（公共常量：FieldSchema 日期校验 + 1c 对账器数字回验共用）。
# 「元年」= 纪年特殊形态（第一年文言用法），须与数字年并列支持。
ERA_YEAR_RE = re.compile(
    r"(光绪|光緒|宣统|宣統|咸丰|咸豐|同治|乾隆|嘉庆|嘉慶|道光|康熙|雍正)"
    r"(?:[0-9〇零一二三四五六七八九十]+|元)年?")


# 属性名 → 规则绑定的启发式关键词（恒先跑 CharNormalizer）
_CURRENCY_HINTS = ("资本", "股银", "股銀", "股本", "金额", "金額")
_ADDRESS_HINTS = ("地址", "所在地", "住址")
_TITLE_HINTS = ("衔", "銜", "职衔", "官职", "官職")


@dataclass
class FieldDef:
    name: str
    desc: str = ""
    synonyms: List[str] = field(default_factory=list)
    required: bool = False
    rules: List[str] = field(default_factory=list)   # 绑定的规则模块序列

    # --- 三类内建校验（方案：空值/日期格式/枚举）---
    def check_empty(self, value: str) -> Optional[str]:
        if self.required and not (value or "").strip():
            return f"必填字段「{self.name}」为空"
        return None

    def check_date(self, value: str) -> Optional[str]:
        """日期格式：属性名含「时间/日期/年」时要求清代纪年（含元年）或公元年。"""
        if not any(k in self.name for k in ("时间", "時間", "日期", "年份", "年月")):
            return None
        v = (value or "").strip()
        if not v:
            return None
        if ERA_YEAR_RE.search(v):
            return None
        if re.search(r"(1[89][0-9]{2}|20[0-9]{2})年?", v):
            return None
        return f"「{self.name}」值「{v}」非纪年/公元年格式"


class FieldSchema:
    """档案属性集 → 输出契约 + 校验规则链（S3 LLM 输出与 S2 校验共用）。"""

    def __init__(self, fields: List[FieldDef],
                 enum_groups: Optional[Dict[str, EnumConstraint]] = None):
        self.fields = {f.name: f for f in fields}
        self.enum_groups = enum_groups or {}
        self.char_normalizer = CharNormalizer()
        self.title_validator = TitleValidator()
        self.addr_simplifier = AddrSimplifier()
        self.currency_parser = CurrencyParser()

    @classmethod
    def from_profile(cls, profile: Dict[str, Any],
                     data_dir: Optional[Path] = None) -> "FieldSchema":
        """profile_store 档案 dict（attrs=[{name,desc,synonyms}]）→ FieldSchema。"""
        fields: List[FieldDef] = []
        for attr in profile.get("attrs", []):
            if isinstance(attr, str):
                attr = {"name": attr}
            name = attr.get("name", "")
            if not name:
                continue
            fields.append(FieldDef(
                name=name, desc=attr.get("desc", ""),
                synonyms=list(attr.get("synonyms", [])),
                required=bool(attr.get("required", False)),
                rules=_bind_rules(name),
            ))
        return cls(fields, enum_groups=load_enum_groups(data_dir))

    def validate_record(self, record: Dict[str, str]) -> Dict[str, RuleResult]:
        """对一条抽取记录逐字段跑规则链：内建校验 + 绑定模块串联
        （CharNormalizer 恒先行，前级 FIXED 输出作为后级输入）。
        返回 {字段名: RuleResult}——S4 待核清单 = SUSPECT 项汇总。"""
        results: Dict[str, RuleResult] = {}
        for name, fdef in self.fields.items():
            value = record.get(name, "")
            # 1) 内建：空值 / 日期格式
            for checker in (fdef.check_empty, fdef.check_date):
                msg = checker(value)
                if msg:
                    results[name] = _result("FieldSchema", STATUS_SUSPECT,
                                            value, reason=msg)
                    break
            if name in results:
                continue
            # 2) 绑定规则链
            cur = value
            final: Optional[RuleResult] = None
            for rule_name in fdef.rules:
                rr = self._apply_rule(rule_name, cur)
                if rr.status == STATUS_FIXED:
                    cur = rr.value       # 修正值继续下级校验
                final = rr
            if final is None:
                final = _result("FieldSchema", STATUS_PASS, value)
            results[name] = final
        return results

    def _apply_rule(self, rule_name: str, value: str) -> RuleResult:
        if rule_name == "char":
            return self.char_normalizer.normalize(value)
        if rule_name == "title":
            return self.title_validator.validate(value)
        if rule_name == "address":
            return self.addr_simplifier.simplify(value)
        if rule_name == "currency":
            return self.currency_parser.parse_capital(value)
        if rule_name.startswith("enum:"):
            group = self.enum_groups.get(rule_name[5:])
            if group:
                return group.normalize(value)
        return _result("FieldSchema", STATUS_PASS, value)


def _bind_rules(attr_name: str) -> List[str]:
    """属性名启发式 → 规则链（char 恒先行）。enum:组名 按属性名精确匹配
    enums.json 的组（如「公司性质」），其余按关键词猜。"""
    rules = ["char"]
    for gname in _ENUM_GROUP_NAMES:
        if attr_name == gname or gname in attr_name:
            rules.append(f"enum:{gname}")
            return rules
    if any(k in attr_name for k in _CURRENCY_HINTS):
        rules.append("currency")
    elif any(k in attr_name for k in _ADDRESS_HINTS):
        rules.append("address")
    elif any(k in attr_name for k in _TITLE_HINTS):
        rules.append("title")
    return rules


# enums.json 的组名在 load 时才知道——绑定期查一次（空文件时为空表）
_ENUM_GROUP_NAMES: List[str] = []


def _refresh_enum_group_names(data_dir: Optional[Path] = None) -> None:
    global _ENUM_GROUP_NAMES
    data = load_rules_data("enums", data_dir)
    _ENUM_GROUP_NAMES = [k for k in data if not k.startswith("_")
                         and isinstance(data[k], dict)]


_refresh_enum_group_names()


# ============================================
# 管线便捷入口
# ============================================
def build_schema_from_profile(profile: Dict[str, Any],
                              data_dir: Optional[Path] = None) -> FieldSchema:
    """档案 dict → FieldSchema（1c 对账器 / S3 抽取校验共用入口）。"""
    _refresh_enum_group_names(data_dir)
    return FieldSchema.from_profile(profile, data_dir=data_dir)


def review_list(results: Dict[str, RuleResult]) -> List[Dict[str, str]]:
    """从 validate_record 结果提取 S4 待核清单行（按字段名排序稳定输出）。"""
    return [{"field": name, "value": str(rr.value), "reason": rr.reason,
             "rule": rr.rule}
            for name, rr in sorted(results.items()) if rr.needs_review]
