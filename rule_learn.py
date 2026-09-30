# -*- coding: utf-8 -*-
"""数据飞轮 · 规则学习层（2026-09-11）。

**为什么需要这一层**（用户 2026-09-11 对「金标准页」的定位纠正）：

    金标准页不是一个用来逐张核对的标准答案——那是**没有必要的行为**
    （用户若能逐张做核对标注，就不需要 OCR 了）。它是**例题**：用户给少量
    标注，是要教会系统"这一类图该怎么读"，再由系统去读**其余绝大多数**。

    飞轮 = 金标准 → 提取规则 → 规则驱动全样本识别 → 疑错进待核
           → 用户裁决 → 裁决回流改规则 → 识别更准（回到第二段）

本模块负责其中的**第二段**：从金标准例题里提取**可执行的识别规则**。

**与既有模块的关系（绝不另造平行实现）**：

  - 几何/语义归纳（记录骨架、锚属性、列偏移先验、字格度量）**已存在**于
    `layout_contract.induce_contract` → `data/layout_contracts/layout_<pid>.json`。
    本模块**不重做**归纳，而是**读契约**并把其中的规律转成"识别时可用"的形式。
  - 词表/校验类规则（误识字表、职衔表、地址、金额、枚举）**已存在**于
    `rule_engine.py` + `rules_data/*.json`。本模块只产出**新词候选**，
    **不自动改写既有种子表**（红线三：候选而非自动改表）。
  - 本模块新增的是契约里没有的两族：**页面常量**（不属记录的表头/栏目行）与
    **值示例/形制线索**（供提示词注入）。

产物 `rules_data/learned_<profile_id>.json`，消费方是
`split_records.build_split_prompt(..., rules=...)`（规则参与识别）与
`preannotate_gen` 的规则校验（疑错进待核）。

纪律：零 API、纯 stdlib、全函数式；金标准缺失 → 空规则（绝不阻断主流程）。
"""
from __future__ import annotations

import copy
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import layout_contract as LC
import rule_split                 # 仅用其 S2T_FOLD 折叠表（单一实现，无环依赖）

log = logging.getLogger("rule_learn")

_BASE = (Path(__import__("sys").executable).parent.resolve()
         if getattr(__import__("sys"), "frozen", False)
         else Path(__file__).parent.resolve())

RULES_DATA_DIR = _BASE / "rules_data"
DEFAULT_GOLD_DIR = _BASE / "manual_annotations"
DEFAULT_CONTRACTS_DIR = _BASE / "data" / "layout_contracts"

MAX_VALUE_EXAMPLES = 4        # 每属性最多注入几个值示例（提示词预算）
MAX_EXAMPLE_CHARS = 24        # 单个值示例截断长度（防长地址挤爆提示词）
MAX_CONSTANTS = 6             # 页面常量上限
MIN_CONSTANT_PAGES = 2        # 页面常量至少出现在几页
CONT_MAX_LEN = 3              # 续接判据：同属性相邻框中较短者的长度上限


# ============================================
# 金标准读取 —— ★「项目类别隔离」的唯一落点（2026-09-16）
# ============================================
# 隔离轴 = **档案（profile）**，不是栏目。
#
# 为什么不是栏目（三条实测证据，2026-09-16）：
#   ① 金标准行自带 `profile` 字段：223 行里 189 行有，**全部是真实金标准**；
# 没有 `profile` 的 34 行 100% 是测试残留（`img1` / 截图类文件名）。
#   ② `data/projects.json` 的项目**没有"类型/档案"字段**（只有 id/name/color/tags…）
#      ⇒ 项目**声明不了**自己的文献类型，无从据此隔离。
# ③ 实况反例：金标准页归属的项目叫「**公司注册**」⇒ 项目名是**主题**、
# 不是**类型**。同名的项目里完全可以混进另一批材料。
# ⇒ 「隔离轴 = 项目」在实现上站不住；**行上的 `profile` 才是随行携带的真实类型标签**。
#
# ★ 分界（重要）：隔离约束的是**推断口**（`facts` / `learn` —— 它们会"自己决定读什么"），
#   不约束**选择口**（`/api/seam_subjects` 刻意列全库，因为那是让人**挑**用哪套例题）。
#   把隔离套到选择口上，等于取消"拿另一套例题跑本样式"这个正当入口。
PROFILE_FIELD = "profile"


def row_profile(row: dict) -> str:
    """标注行 → 所属档案 `profile_id`；空串 ⇒ **未归属任何档案**。"""
    return str((row or {}).get(PROFILE_FIELD) or "").strip()


def belongs_to_profile(row: dict, profile_id: Optional[str]) -> bool:
    """该行是否属于档案 `profile_id`。

    `profile_id` 为空/None ⇒ **不筛**（True）：那是"全局视图"的**显式**请求，
    调用方自己负责（`gold_census` 与选择口就是这么用的）。

    ★ 未归档的行（`profile` 为空）**不属于任何档案** —— 包括不属于"全部"。
      这是隔离的关键：否则测试残留会随每一次推断一起进观测。
    """
    pid = str(profile_id or "").strip()
    if not pid:
        return True
    return row_profile(row) == pid


def read_gold_raw(gold_dir: Optional[Path] = None) -> Dict[str, List[dict]]:
    """`manual_annotations/*.jsonl` → {stem: [原始行]}。**读金标准的唯一实现**。

    ★ 为什么单独立它（2026-09-16）：`load_gold`（只留 attr+text 的行）与
      `gold_facts.load_gold_boxes`（保留 attr 为空的行）原先**各自解析了一遍 jsonl**。
      两处解析 = 两处会漂移；更要命的是 —— **隔离过滤必须只挂一处**，
      否则今天堵住了推断口，明天新加的读取口又是漏的。
    """
    d = Path(gold_dir or DEFAULT_GOLD_DIR)
    if not d.is_dir():
        log.warning("[rule_learn] 金标准目录不存在: %s", d)
        return {}
    out: Dict[str, List[dict]] = {}
    for p in sorted(d.glob("*.jsonl")):
        rows: List[dict] = []
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("[rule_learn] 读取失败 %s: %s", p.name, e)
            continue
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                rows.append(r)
        if rows:
            out[stem_of_gold_file(p.name)] = rows
    return out


def filter_by_profile(raw: Dict[str, List[dict]],
                      profile_id: Optional[str]) -> Dict[str, List[dict]]:
    """按档案过滤 —— **隔离的唯一落点**（`load_gold` / `load_gold_boxes` 都从这过）。

    `profile_id` 为空 ⇒ 原样返回（不筛，显式的全局请求）。
    过滤后整页无行的 ⇒ 该页**不进结果**（少一页，不是少几行）。
    """
    pid = str(profile_id or "").strip()
    if not pid:
        return raw
    out: Dict[str, List[dict]] = {}
    for stem, rows in raw.items():
        keep = [r for r in rows if belongs_to_profile(r, pid)]
        if keep:
            out[stem] = keep
    return out


def load_gold(gold_dir: Optional[Path] = None,
              profile_id: Optional[str] = None) -> Dict[str, List[dict]]:
    """`manual_annotations/*.jsonl` → {stem: [行]}（只取有 attr/text 的行）。

    ★ `profile_id` 非空 ⇒ **只读该档案的行**。`learn` 走的就是这条 ⇒
      "换一套材料不会学到上一套的规则"由这里保证。

    **只读**：金标准是用户的劳动成果，本模块绝不改写（纪律）。
    """
    raw = filter_by_profile(read_gold_raw(gold_dir), profile_id)
    out: Dict[str, List[dict]] = {}
    for stem, rows in raw.items():
        keep = [r for r in rows
                if r.get("attr") and str(r.get("text") or "").strip()]
        if keep:
            out[stem] = keep
    return out


def gold_census(gold_dir: Optional[Path] = None) -> dict:
    """全库普查：**每个档案各有多少页/多少行** + 未归档 + 同页多档案。

    ★ 两个用途，都不需要第二份解析：
      ① **可见性** —— 报告"哪些行因不属于本档案而不会进推断"，让隔离**看得见**，
         而不是让人发现"页数怎么变少了"（本项目一贯的"可见不静默"）。
      ② **视图** —— 「我有哪些档案、它们的金标准做到哪一步」的数据面。
    """
    raw = read_gold_raw(gold_dir)
    by: Dict[str, dict] = {}
    unassigned = {"n_pages": 0, "n_rows": 0, "stems": []}
    mixed: List[dict] = []
    for stem, rows in sorted(raw.items()):
        per: Dict[str, int] = {}
        n_blank = 0
        for r in rows:
            pid = row_profile(r)
            if pid:
                per[pid] = per.get(pid, 0) + 1
            else:
                n_blank += 1
        if n_blank:
            unassigned["n_pages"] += 1
            unassigned["n_rows"] += n_blank
            unassigned["stems"].append(stem)
        # 同页出现两个档案 ⇒ 自相矛盾（只上报，不擅自改：可能是真的跨类型页）
        if len(per) > 1:
            mixed.append({"stem": stem, "profiles": sorted(per),
                          "rows": {k: per[k] for k in sorted(per)}})
        for pid, n in per.items():
            g = by.setdefault(pid, {"n_pages": 0, "n_rows": 0, "stems": []})
            g["n_pages"] += 1
            g["n_rows"] += n
            g["stems"].append(stem)
    return {
        "n_pages": len(raw),
        "n_rows": sum(len(v) for v in raw.values()),
        "by_profile": {k: by[k] for k in sorted(by)},
        "unassigned": unassigned,
        "mixed": mixed,
    }


def gold_scope_of(raw: Dict[str, List[dict]],
                  profile_id: Optional[str]) -> dict:
    """这次推断**吃进了什么、排除了什么** —— 给产物与界面读的可见性读数。

    没有它，"为什么这次只用了 3 页"就只剩一个变小的整数、无从归因；
    有它，页数变少这件事**在产物里自带解释**（本项目一贯的"可见不静默"）。
    """
    pid = str(profile_id or "").strip()
    n_own = n_foreign = n_blank = 0
    p_own = p_foreign = p_blank = 0
    for _stem, rows in raw.items():
        o = (sum(1 for r in rows if row_profile(r) == pid) if pid else len(rows))
        f = (sum(1 for r in rows
                 if row_profile(r) and row_profile(r) != pid) if pid else 0)
        b = sum(1 for r in rows if not row_profile(r))
        n_own += o
        n_foreign += f
        n_blank += b
        p_own += 1 if o else 0
        p_foreign += 1 if (not o and f) else 0
        p_blank += 1 if (not o and not f and b) else 0
    return {
        "profile_id": pid,
        "scope": "profile" if pid else "all",
        "n_rows_kept": n_own,
        "n_rows_other_profile": n_foreign,
        "n_rows_unassigned": n_blank,
        "n_pages_kept": p_own,
        "n_pages_other_profile": p_foreign,
        "n_pages_unassigned": p_blank,
    }


def r_stem(image_name: str) -> str:
    """图片名 → stem（去扩展名，供展示用）。"""
    return Path(str(image_name)).stem


def stem_of_gold_file(file_name: str) -> str:
    """标注文件名 → 页 stem。

    ⚠ **必须剥两层**：标注文件名是 `<image_name>.jsonl`，而 `image_name`
    自带扩展名（`xxx.png`）→ 单次 `.stem` 只得到 `xxx.png`，
    与其余模块用的 stem（`xxx`）对不上。同一坑在 `app.py` 的
    `skip_annotated` 也踩过（2026-09-11）。
    """
    return Path(Path(file_name).stem).stem


def _reading_order(rows: List[dict]) -> List[int]:
    """行 → 阅读序下标。

    几何齐 → 走 `layout_contract.order_rtl`（**单一实现入口**：无量纲三守卫，
    竖排 RTL / 横排 LTR 自适应）；几何缺失 → 保持原始顺序（不猜）。
    """
    boxes = [LC.rect_of_box(r.get("box")) for r in rows]
    if all(b is not None for b in boxes) and len(boxes) >= 2:
        return LC.order_rtl(boxes)
    return list(range(len(rows)))


# ============================================
# 五族规则提取
# ============================================
def _contract_of(profile_id: str, contracts_dir: Optional[Path] = None) -> dict:
    p = Path(contracts_dir or DEFAULT_CONTRACTS_DIR)
    return LC.load_contract(profile_id, p) or {}


def _skeleton_from_contract(contract: dict) -> dict:
    """记录骨架直接取契约的归纳结果（不重做归纳）。"""
    s = contract.get("S") or {}
    tmpl = [str(a) for a in (s.get("record_template") or [])]
    return {
        "order": tmpl,
        "support": {k: round(float(v), 3) for k, v in (s.get("support") or {}).items()},
        "required": [str(a) for a in (s.get("required") or [])],
        "optional": [str(a) for a in (s.get("optional") or [])],
        "anchor": str(s.get("anchor") or ""),
        "cardinality": s.get("cardinality") or {},
        "source": "layout_contract.S（induce_semantics 归纳）",
    }


def _skeleton_from_gold(gold: Dict[str, List[dict]], tmpl: List[str]) -> dict:
    """契约缺失时的兜底：直接从金标准投票属性序列（跨页一致度）。"""
    seqs: List[List[str]] = []
    for rows in gold.values():
        order = _reading_order(rows)
        seqs.append([rows[i]["attr"] for i in order])
    n = len(seqs)
    if not n:
        return {"order": tmpl, "support": {}, "required": [], "optional": [],
                "anchor": "", "cardinality": {},
                "source": "gold-first-order（契约缺失兜底）"}
    # ★ 保序去重（2026-09-16 修，实施 G2-② 时发现）：
    #   原写法是 `for a in set(s)` —— **set 无序** ⇒ `Counter` 的插入序随
    #   `PYTHONHASHSEED` 变 ⇒ 无契约（`tmpl` 为空）且 support 并列时，下面
    #   `sorted(..., key=lambda a: -support[a], tmpl.index(a) if a in tmpl else 99)`
    #   的 tie-break 退化成 set 迭代序 ⇒ **同一份金标准每次 learn 得到不同的
    #   属性序**（实测 6/6 全不同，且与金标准的书写序毫无关系）。
    #   后果不止"看着乱"：`as_prompt_rules()["attr_order"]` 每轮都在漂 ⇒
    #   提示词内容不稳；新一轮的规则变更 diff 会报满屏"换位"假噪音。
    #   改成 `dict.fromkeys` 保序去重后：**support 数值一个都不变**（每页每属性
    #   仍最多计一次），只是让 tie-break 有确定解 = 各页首次出现的顺序。
    counts = Counter(a for s in seqs for a in dict.fromkeys(s))
    support = {a: round(c / n, 3) for a, c in counts.items()}
    order = sorted(support, key=lambda a: (-support[a], tmpl.index(a) if a in tmpl else 99))
    return {"order": order, "support": support,
            "required": [a for a in order if support[a] >= 0.8],
            "optional": [a for a in order if 0.3 <= support[a] < 0.8],
            "anchor": count_anchor(seqs),
            "cardinality": {"min": min(len(s) for s in seqs),
                            "max": max(len(s) for s in seqs),
                            "mean": round(sum(len(s) for s in seqs) / n, 2)},
            "source": "gold-vote（契约缺失兜底）"}


def count_anchor(seqs: List[List[str]]) -> str:
    """锚属性 = 各页首属性里出现最多的那个（记录边界标记）。"""
    if not seqs:
        return ""
    c = Counter(s[0] for s in seqs if s)
    return c.most_common(1)[0][0] if c else ""


def extract_page_constants(gold: Dict[str, List[dict]],
                           skeleton_order: List[str]) -> List[str]:
    """页面常量 = 跨页**逐字相同**、且不属记录骨架的属性值（表头/栏目行）。

    这正是拆分规则第 6 条（页眉页脚不要拆进 records）的**可执行化** ——
    此前只靠模型自己判断，现在给出该文献的确切常量文本。
    """
    per_page: Dict[str, List[set]] = {}
    for rows in gold.values():
        for r in rows:
            a = str(r["attr"])
            if a in skeleton_order:
                continue
            per_page.setdefault(a, []).append({str(r["text"]).strip()})
    out: List[str] = []
    for a, sets in per_page.items():
        if len(sets) < MIN_CONSTANT_PAGES:
            continue
        common = set.intersection(*sets) if sets else set()
        for v in sorted(common):
            if v and v not in out:
                out.append(v)
    return out[:MAX_CONSTANTS]


def extract_value_examples(gold: Dict[str, List[dict]],
                           skeleton_order: List[str]) -> Dict[str, List[str]]:
    """逐属性取**代表值示例**（注入提示词，让模型照着判读口径读新页）。

    ⚠ **取长优先，不取高频优先**——实测教训：金标准是**逐框标注**的，
    短框碎片（「厂」「股」「司」「二日注册」）与完整值混在同一属性下；
    按频次取会把碎片当成"值长这样"教给模型（正好教反）。
    长值在本任务是完整值的可靠代理（碎片恒短）。
    """
    bag: Dict[str, List[str]] = {}
    for rows in gold.values():
        for r in rows:
            a = str(r["attr"])
            if a not in skeleton_order:
                continue
            v = str(r["text"]).strip()
            if 0 < len(v) <= MAX_EXAMPLE_CHARS:
                bag.setdefault(a, []).append(v)
    out: Dict[str, List[str]] = {}
    for a, vs in bag.items():
        cnt = Counter(vs)
        # 长优先 → 出现次数次之 → 字典序（确定性，便于比对）
        ranked = sorted(cnt, key=lambda v: (-len(v), -cnt[v], v))
        out[a] = ranked[:MAX_VALUE_EXAMPLES]
    return out


_PREFIX_STOP = set("的了在與与及和")


def extract_patterns(gold: Dict[str, List[dict]]) -> dict:
    """形制线索：纪年正则 + 资本单位字 + 地址引导词头。

    纪年直接复用 `rule_engine.ERA_YEAR_RE`（单一实现入口，不重写正则）；
    资本/地址则从金标准**实测**（不照搬假设）。
    """
    try:
        import rule_engine as RE
        era = RE.ERA_YEAR_RE.pattern
    except Exception:                                    # pragma: no cover
        era = ""
    cap_vals, addr_vals = [], []
    for rows in gold.values():
        for r in rows:
            a = str(r["attr"])
            v = str(r["text"]).strip()
            if any(k in a for k in ("资本", "資本", "股银", "股銀")):
                cap_vals.append(v)
            if "地址" in a:
                addr_vals.append(v)
    # 资本单位的**共现字**（出现在 ≥50% 资本值里的字）= 识别单位线索
    unit_chars: List[str] = []
    if cap_vals:
        cnt = Counter(ch for v in cap_vals for ch in set(v))
        thr = max(2, len(cap_vals) // 2)
        unit_chars = [ch for ch, c in cnt.most_common() if c >= thr and ch in "銀银兩两元股錢钱"]
    # 地址引导词头：2–4 字前缀，跨值重复出现
    heads: Counter = Counter()
    for v in addr_vals:
        for L in (2, 3, 4):
            if len(v) >= L and v[:L][0] not in _PREFIX_STOP:
                heads[v[:L]] += 1
    addr_heads = [h for h, c in heads.most_common(8) if c >= 2]
    return {"era_re": era, "capital_unit_chars": unit_chars,
            "addr_heads": addr_heads,
            "note": "era_re 复用 rule_engine.ERA_YEAR_RE；其余由金标准实测"}


def extract_observed_enums(gold: Dict[str, List[dict]],
                           skeleton_order: List[str],
                           max_card: int = 12) -> Dict[str, dict]:
    """低基数字段 → 观测值域（候选枚举，**不自动写入 enums.json**）。

    只报"金标准里出现过什么、各几次"，是否收为规范枚举由人定（红线三）。
    """
    bag: Dict[str, Counter] = {}
    for rows in gold.values():
        for r in rows:
            a = str(r["attr"])
            if a in skeleton_order:
                bag.setdefault(a, Counter())[str(r["text"]).strip()] += 1
    out: Dict[str, dict] = {}
    for a, c in bag.items():
        if 1 < len(c) <= max_card:
            out[a] = {"n_distinct": len(c),
                      "values": {v: n for v, n in c.most_common()}}
    return out


def extract_title_candidates(gold: Dict[str, List[dict]]) -> List[str]:
    """金标准里出现过的职衔值 → `titles.json` 的**扩充候选**（不自动写入）。"""
    cand: Counter = Counter()
    for rows in gold.values():
        for r in rows:
            if "功名" in str(r["attr"]) or "衔" in str(r["attr"]):
                v = str(r["text"]).strip()
                if v:
                    cand[v] += 1
    return [v for v, _ in cand.most_common()]


def extract_continuation(gold: Dict[str, List[dict]]) -> dict:
    """跨列/跨框续接：值跨多个行框 → 读的时候必须**按阅读序拼合**。

    **为什么必须形式化**：金标准自身就是逐框标注的（人工也一框一条），
    所以「裕昌机器缫丝」+「厂」、「股分洋银十万元」+「股」这类**同属性相邻短框**
    在例题里天然存在。若不定规则，例题本身都不可用（模型会把碎片当成值）。

    判据：阅读序相邻、属性相同、其一长度 ≤ `CONT_MAX_LEN` → 记为续接处。
    **拼合方向恒为阅读序方向**（实测反例：0001「股份有限公」+「司」碎片在后；
    0002「光绪」+「三十年八月…」碎片在前 —— 按"短的前置"拼会拼反）。
    """
    frag: Counter = Counter()
    examples: List[dict] = []
    total = 0
    for stem, rows in gold.items():
        order = _reading_order(rows)
        total += len(rows)
        for a, b in zip(order, order[1:]):
            ra, rb = rows[a], rows[b]
            if ra["attr"] != rb["attr"]:
                continue
            ta, tb = str(ra["text"]).strip(), str(rb["text"]).strip()
            if not ta or not tb or len(ta) > MAX_EXAMPLE_CHARS:
                continue
            if min(len(ta), len(tb)) <= CONT_MAX_LEN:
                frag[ra["attr"]] += 1
                if len(examples) < 8:
                    examples.append({
                        "page": r_stem(stem)[-4:], "attr": ra["attr"],
                        "first": ta, "second": tb,
                        "merged": ta + tb,
                    })
    by_attr = dict(frag.most_common(8))
    multi = Counter()
    for rows in gold.values():
        for r in rows:
            boxes = r.get("boxes")
            if isinstance(boxes, list) and len(boxes) >= 2:
                multi[str(r["attr"])] += 1
    return {
        "n_pairs": sum(frag.values()), "n_rows": total,
        "by_attr": by_attr, "examples": examples,
        "by_attr_boxes": dict(multi.most_common(8)),
        "rule": "同一属性的值若跨多个行框（阅读序相邻、其一为短碎片），"
                "按**阅读序**依次拼接；拼接方向恒为阅读序方向，与框的长短无关",
    }


# ============================================
# 裁决备注消费（P5，2026-09-26）：manual_annotations/notes/*.notes.jsonl
# ============================================
# **为什么**：用户裁决金标准页时留了备注（「规则总结时不要把报名纳入」「跨页内容
# 请考虑修正」）—— 这些是**可操作的规则意图**，此前 learn 完全不读，飞轮的
# 「裁决回流改规则」在这类备注上是断的。本段把断点接上（最小可行）：
#
#   exclude_masthead（报名列不纳入）→ 原文进 `page_constants`
#       （页面常量既被 `_looks_like_name` 拒作记录起点，也被取值前裁剪 ——
#         rule_split 的同一实现，不另造机制）；
#   cross_page（跨页内容）→ 执行层消费 `data/page_exclusions.json` 的排除带，
#       此处只**登记意图**（页 + 框 + 原话），让 learned 自带「为什么排除」的出处；
#   attr_hint（属性纠正提示）→ 原样登记进 `attr_hints`，供人读与提示词注入。
#
# 只读纪律：notes 与金标准一样是用户的劳动成果，本层绝不改写。
NOTES_DIR = DEFAULT_GOLD_DIR / "notes"


def read_notes(notes_dir: Optional[Path] = None) -> Dict[str, List[dict]]:
    """`notes/*.notes.jsonl` → {文件名: [备注行]}；目录缺失 → `{}`。"""
    d = Path(notes_dir or NOTES_DIR)
    if not d.is_dir():
        return {}
    out: Dict[str, List[dict]] = {}
    for p in sorted(d.glob("*.notes.jsonl")):
        rows: List[dict] = []
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and str(r.get("note") or "").strip():
                rows.append(r)
        if rows:
            out[p.name] = rows
    return out


def _note_clean_text(row: dict) -> str:
    """备注挂的原文（`样例材料（*AI看到…）`）→ `（` 前的干净文本。"""
    t = str(row.get("source_line_text") or "")
    for sep in ("（", "("):
        i = t.find(sep)
        if i > 0:
            t = t[:i]
    return t.strip()


def _note_kind(row: dict) -> str:
    """备注行 → 可操作意图类别；不可操作 → `""`（不计入消费）。"""
    n = str(row.get("note") or "")
    if "跨页" in n or "跨頁" in n:
        return "cross_page"
    if (any(k in n for k in ("报名", "報名", "报纸名", "報紙名"))
            and any(k in n for k in ("不要", "不是", "不纳", "不納"))):
        return "exclude_masthead"
    if str(row.get("attr_hint") or "").strip():
        return "attr_hint"
    return ""


def extract_note_rules(notes: Dict[str, List[dict]]) -> dict:
    """备注 → `learned["notes"]` 块。`n_consumed` = 可操作、已反映的条数。"""
    constants: List[str] = []
    cross_page: List[dict] = []
    hints: List[dict] = []
    n_total = n_consumed = 0
    for fname, rows in sorted(notes.items()):
        for row in rows:
            n_total += 1
            kind = _note_kind(row)
            if not kind:
                continue
            n_consumed += 1
            item = {"file": fname, "box": row.get("box"), "kind": kind,
                    "note": str(row.get("note") or "")[:120]}
            if kind == "exclude_masthead":
                t = _note_clean_text(row)
                item["text"] = t
                if t and t not in constants:
                    constants.append(t)
            elif kind == "attr_hint":
                item["hint"] = str(row.get("attr_hint") or "")
                hints.append(item)
            else:
                cross_page.append(item)
    return {"n_notes": n_total, "n_consumed": n_consumed,
            "source_dir": "manual_annotations/notes",
            "page_constants_extra": constants,
            "cross_page": cross_page, "attr_hints": hints}


# ============================================
# 学习 / 落盘 / 读取
# ============================================
def learn(profile_id: str, gold_dir: Optional[Path] = None,
          contracts_dir: Optional[Path] = None,
          notes_dir: Optional[Path] = None,
          data_dir: Optional[Path] = None) -> dict:
    """金标准 + 契约 + 裁决备注 → 可执行规则 dict。

    ★ 隔离（2026-09-16）：`gold_dir` 只决定**读哪个目录**，`profile_id` 决定
      **目录里哪些行算数**。两者缺一，"同一目录、多个档案"必然互相污染 ——
      实测过的后果是 `rules_data/learned_<pid>.json` 里混进别的档案的
      列宽 / 页边距 / 属性序，而这份文件正是 `triage` / `plan` 的输入。
    """
    gold = load_gold(gold_dir, profile_id=profile_id)
    contract = _contract_of(profile_id, contracts_dir)
    sk = _skeleton_from_contract(contract)
    if not sk["order"]:
        sk = _skeleton_from_gold(gold, [])
    n_rows = sum(len(v) for v in gold.values())
    # ★ 可见性：把"排除掉多少"一起落盘（否则下一个人只看 n_pages 变小，无从归因）
    _raw = read_gold_raw(gold_dir)
    # ★ 裁决备注（P5，2026-09-26）：报名类备注的原文并入页面常量 ——
    #   常量在 rule_split 是同一实现（拒作记录起点 + 取值前裁剪），不另造机制。
    note_rules = extract_note_rules(read_notes(notes_dir))
    page_constants = extract_page_constants(gold, sk["order"])
    for c in note_rules.get("page_constants_extra") or []:
        if c not in page_constants:
            page_constants.append(c)
    out = {
        "_meta": {
            "说明": "从金标准例题提取的可执行识别规则（飞轮第二段产物）。",
            "来源": "manual_annotations/*.jsonl + data/layout_contracts/ + notes/",
            "纪律": "本文件是**统计与候选**，不自动改写 rules_data 的种子表；"
                    "契约漂移须可见（漂移由 P5 负责），本层不做静默过拟合。",
            "生成时间": datetime.now().isoformat(timespec="seconds"),
        },
        "profile_id": profile_id,
        "gold_scope": gold_scope_of(_raw, profile_id),
        "n_pages": len(gold),
        "n_rows": n_rows,
        "pages": sorted(gold.keys()),
        "skeleton": sk,
        "page_constants": page_constants,
        "notes": note_rules,
        "value_examples": extract_value_examples(gold, sk["order"]),
        "patterns": extract_patterns(gold),
        "observed_enums": extract_observed_enums(gold, sk["order"]),
        "title_candidates": extract_title_candidates(gold),
        "continuation": extract_continuation(gold),
    }
    # ★ 材料画像消费（scan，2026-09-29）：画像存在时把全语料统计叠进来 ——
    #   高置信常量自动并入、中置信只做候选（人审）。
    #   ★ 零影响不变量：无 corpus 文件 ⇒ `load_corpus` 返 None ⇒ 下面整段跳过，
    #     输出与不集成时**字段级等价**（除 `_meta.生成时间`）——护栏测试断言。
    corpus = load_corpus(profile_id, data_dir)
    if corpus:
        _apply_corpus(out, corpus, corpus_path(profile_id, data_dir).name)
    return out


# ============================================
# 材料画像消费（scan · 2026-09-29）
# ============================================
# 数据流：corpus_scan.scan_corpus() → rules_data/corpus_<pid>.json（机器层）
#        → 本节把它叠进 learn() 产物（先验层）→ rule_report 渲染（人审层）。
#
# **为什么只读文件、不 import corpus_scan**：画像是一次性落盘的 JSON 产物，
#   learn 消费的是"上一步的结果"而非"上一步的过程" —— 读文件即可，模块不进
#   本文件的依赖图（rule_learn 保持单独可导入，corpus_scan 坏了也不连坐）。
#
# **三条去向（与 apply_overrides 的"三条互斥去向"同一分诊纪律）**：
#   ratio ≥ 0.9  → 自动并入 `page_constants`（追加、去重、不删既有），
#                  逐条记 `_meta.corpus_constants_added`（机器动的必须可分辨）；
#   0.4 ≤ ratio < 0.9 → 进 `learned["corpus"]["constants_candidates"]`，
#                  **仅报告**，人用 `--page-const-add` 采纳（AI 判断不入库为定论）;
#   ratio < 0.4  → 不够格，两处都不进（噪音）。
#
# **零影响不变量**：无 corpus 文件 ⇒ `load_corpus` 返 None ⇒ learn 输出与
#   旧版逐字节等价（`_meta.生成时间` 除外）。
CORPUS_ADD_RATIO = 0.9        # ≥ 此值：自动并入页常量
CORPUS_CAND_RATIO = 0.4       # ≥ 此值且 < 上者：进候选（仅报告）

# ★ 简繁折叠去重（2026-09-29，单一实现）：corpus 候选是**池页 OCR 文本**的
#   归一形态（繁体，如「公司註冊各案摘要」），金标准常量是**人工行文本**
#   （简体，如「公司注册各案摘要」）—— 同一语义常量两种字形，若按原样比较
#   会双双进 `page_constants`（重复且各自只匹配一半页）。去重比较一律过
#   `rule_split.S2T_FOLD`（与匹配层同一张表，不另造第二份）。
_S2T_TRANS = str.maketrans(rule_split.S2T_FOLD)


def corpus_path(profile_id: str, data_dir: Optional[Path] = None) -> Path:
    """材料画像路径（`rules_data/corpus_<pid>.json`），与 learned/overrides 同目录。"""
    return Path(data_dir or RULES_DATA_DIR) / f"corpus_{profile_id}.json"


def load_corpus(profile_id: str, data_dir: Optional[Path] = None) -> Optional[dict]:
    """读材料画像；不存在 / 读不动 → `None`（调用方据此走无画像路径，零影响）。"""
    p = corpus_path(profile_id, data_dir)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[rule_learn] 材料画像读取失败 %s: %s", p.name, e)
        return None
    return d if isinstance(d, dict) else None


def _apply_corpus(learned: dict, corpus: dict, scan_name: str) -> None:
    """画像 ⊕ 统计产物（就地：`learn()` 刚构建的 dict，无共享引用）。

    只动三处：`page_constants`（追加高置信常量）、`_meta.corpus_constants_added`
    （逐条留痕）、`learned["corpus"]`（画像摘要，报告层消费）。
    """
    meta = learned.setdefault("_meta", {})
    added: List[dict] = []
    candidates: List[dict] = []
    consts = list(learned.get("page_constants") or [])
    # 折叠规范形集合：同一语义常量的简/繁字形在此空间里同形（见上方 _S2T_TRANS 注）
    folded = {str(c).translate(_S2T_TRANS) for c in consts}
    for c in corpus.get("constants_candidates") or []:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        try:
            ratio = float(c.get("ratio") or 0.0)
        except (TypeError, ValueError):
            continue
        if not text:
            continue
        if ratio >= CORPUS_ADD_RATIO:
            key = text.translate(_S2T_TRANS)
            if key not in folded:       # 追加、去重（折叠后比较），不删既有
                consts.append(text)
                folded.add(key)
                added.append({"text": text, "ratio": ratio,
                              "pages_hit": c.get("pages_hit")})
        elif ratio >= CORPUS_CAND_RATIO:
            candidates.append({"text": text, "ratio": ratio,
                               "pages_hit": c.get("pages_hit")})
    if added:
        learned["page_constants"] = consts
        meta["corpus_constants_added"] = added
    qa = corpus.get("qa") if isinstance(corpus.get("qa"), dict) else {}
    learned["corpus"] = {
        "n_pages": (corpus.get("_meta") or {}).get("n_pages"),
        "shape_stats": corpus.get("shape_stats") or {},
        "qa_n_flagged": (qa or {}).get("n_flagged") or 0,
        "scan_path": scan_name,
        "constants_added": [a["text"] for a in added],
        "constants_candidates": candidates,
    }


def learned_path(profile_id: str, data_dir: Optional[Path] = None) -> Path:
    return Path(data_dir or RULES_DATA_DIR) / f"learned_{profile_id}.json"


def save(learned: dict, data_dir: Optional[Path] = None) -> Path:
    """原子写（复用 data_io 单一实现，崩溃不留半截）。"""
    import data_io
    p = learned_path(learned["profile_id"], data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data_io.atomic_write_json(p, learned)
    log.info("[rule_learn] 规则已保存: %s（骨架 %d 项 / 常量 %d / 示例 %d 属性）",
             p.name, len(learned["skeleton"]["order"]),
             len(learned["page_constants"]), len(learned["value_examples"]))
    return p


def load(profile_id: str, data_dir: Optional[Path] = None) -> dict:
    """读已学规则；不存在 → {}（调用方据此走无规则路径，零影响）。"""
    p = learned_path(profile_id, data_dir)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[rule_learn] 规则读取失败 %s: %s", p.name, e)
        return {}
    return d if isinstance(d, dict) and d.get("skeleton") else {}


# ============================================
# 人工覆盖层（G2-③，2026-09-16 用户裁决）
# ============================================
# **为什么需要这一层**：`learn()` 是**统计**产物 —— 属性序按 support 排序、必填
# 按 `support >= 0.8` 判定。但例题只有几页时，统计结论会与人的领域知识冲突
# （例：某属性在 2 页里恰好缺一次 ⇒ support=0.67 ⇒ 判成 optional，而人知道它必有）。
#
# **为什么不改 `learn()`**：那会让人工判断混进统计口径，"学出来的"与"人定的"
# 从此不可分辨 —— 与用户 2026-09-13 定的那条同源（AI 的判断不得入库为史料）。
# ⇒ 拆成两个文件、两个生命周期：
#
#     learned_<pid>.json    每次 learn 整份重写    ← 机器所有（统计）
#     overrides_<pid>.json  只有人能改            ← 人所有（声明）
#
# 叠加发生在 `learn_and_save`（`app.py` 的 `/api/rules/learn` 与命令面
# `profile learn` **共用这一个函数**）⇒ 覆盖层两条路都生效，**只有一份实现**。
#
# **可分辨纪律（本段的存在理由）**：产物里必须能看出哪条是学出来的、哪条是人定的。
# 故叠加结果不写回 `skeleton` 了事，而是另记在 `_meta.overrides_applied`
# （含 `from` 原值 + `source:"human"` + `ts`）；被拒绝的覆盖进
# `_meta.overrides_skipped`（**绝不静默丢弃**，且它是**持续状态**、留在产物里等人处理）；
# 已是目标态的进返回值里的 `noop`（**不落盘** —— 那只是本次运行的观察，落盘即噪音）。
#
# **零影响不变量**：没有 overrides 文件 ⇒ `learned_<pid>.json` 与改动前**逐字节
# 相同**（`apply_overrides` 原样返回入参），存量行为不变。
OVERRIDE_SOURCE = "human"


def overrides_path(profile_id: str, data_dir: Optional[Path] = None) -> Path:
    """人工覆盖层路径（`rules_data/overrides_<pid>.json`）。

    ★ 与 `learned_<pid>.json` **同目录、不同前缀**：同目录是为了一眼看全"规则
      由哪几份文件构成"；不同前缀是因为生命周期不同（前者每次重写、后者只在人改时动）。
    """
    return Path(data_dir or RULES_DATA_DIR) / f"overrides_{profile_id}.json"


def confirmed_path(profile_id: str, data_dir: Optional[Path] = None) -> Path:
    """规则确认态路径（`rules_data/confirmed_<pid>.json`，2026-09-30）。

    ★ rules_data 文件命名**单一来源**：learned / corpus / overrides / confirmed
      四份的前缀路径都在本模块（与 overrides 同理：同目录看全"规则由哪几份文件
      构成"）。读写只有 `profile_cli` 的 rules/confirm 两个动作 —— 登记口，非机器闸。
    """
    return Path(data_dir or RULES_DATA_DIR) / f"confirmed_{profile_id}.json"


def load_overrides(profile_id: str, data_dir: Optional[Path] = None) -> dict:
    """读覆盖层；不存在 / 读不动 → `{}`（调用方据此走无覆盖路径，零影响）。"""
    p = overrides_path(profile_id, data_dir)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[rule_learn] 覆盖层读取失败 %s: %s", p.name, e)
        return {}
    return d if isinstance(d, dict) else {}


def save_overrides(ov: dict, data_dir: Optional[Path] = None) -> Path:
    """原子写覆盖层（复用 `data_io` 单一实现）。**本函数只被命令面调用** ——
    `learn` 侧只读不写（否则 learn 会把自己的输出当人的判断再叠一次）。
    """
    import data_io
    pid = str((ov or {}).get("profile_id") or "")
    if not pid:
        raise ValueError("覆盖层缺 profile_id")
    p = overrides_path(pid, data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data_io.atomic_write_json(p, ov)
    log.info("[rule_learn] 覆盖层已保存: %s", p.name)
    return p


def apply_overrides(learned: dict, ov: Optional[dict],
                    data_dir: Optional[Path] = None) -> tuple:
    """统计产物 ⊕ 人工覆盖 → `(新 learned, applied, noop, skipped)`。

    **入参 `learned` 不被修改**（内部深拷贝）—— 它可能就是调用方手里那份
    `learn()` 的返回，就地改会让"统计产物"这个概念失去唯一性。

    支持的覆盖项（G2-③ 首批，用户 2026-09-16 裁决）：

    | 写法 | 语义 |
    |---|---|
    | `attrs.<属性>.position = N` | **pin 属性序**：把该属性钉在第 N 位（1 起，须已在学出的序里） |
    | `attrs.<属性>.required = true/false` | **强制必填 / 取消必填**（同步维护 `optional`） |

    **fail-closed（逐条、不清盘）**：每条覆盖必须自带 `source: "human"`；
    缺失 ⇒ 该条**不生效**并进 `skipped`（记原因）。选择"跳过而非抛错"是因为
    本函数在飞轮主链路上（`learn_and_save`），一条坏覆盖不该让学规则整体停摆；
    但**绝不静默** —— 跳过原因全部落进 `_meta.overrides_skipped`。

    **三条互斥去向**（避免"没生效"与"没变化"混为一谈）：
    `applied`（真的改了）/ `noop`（已是目标态）/ `skipped`（被拒绝或非法）。

    ★ `position` 只做**重排**、不做新增：属性不在 `skeleton.order` 里 ⇒ 跳过。
      理由：本批次只做"pin 属性序"，"新增一个学不出来的属性"是另一件事
      （它会连带要求锚点、support、值示例…），混进来会让这里变成第二套骨架生成器。
    """
    if not ov or not isinstance(ov, dict):
        return learned, [], [], []
    attrs = ov.get("attrs")
    has_pc = isinstance(ov.get("page_constants"), dict) and bool(ov.get("page_constants"))
    if not (isinstance(attrs, dict) and attrs) and not has_pc:
        return learned, [], [], []

    out = copy.deepcopy(learned)
    sk = out.get("skeleton") or {}
    order = list(sk.get("order") or [])
    required = list(sk.get("required") or [])
    optional = list(sk.get("optional") or [])
    applied: List[dict] = []
    noop: List[dict] = []
    skipped: List[dict] = []
    pins: List[tuple] = []

    # ---- 页常量 增 / 删（2026-09-27 第二批：人可以直接声明版面固定件）----
    if has_pc:
        pc_spec = ov["page_constants"]
        if pc_spec.get("source") != OVERRIDE_SOURCE:
            skipped.append({
                "field": "page_constants",
                "reason": f'缺 source:"{OVERRIDE_SOURCE}" —— 人的判断必须可与统计结论分辨',
                "got": pc_spec.get("source")})
        else:
            ts = str(pc_spec.get("ts") or "")
            consts = list(out.get("page_constants") or [])
            for w in (pc_spec.get("add") or []):
                w = str(w).strip()
                if not w:
                    continue
                if w in consts:
                    noop.append({"field": "page_constants", "value": w,
                                 "reason": "已在页常量里"})
                else:
                    consts.append(w)
                    applied.append({"field": "page_constants", "op": "add",
                                    "value": w, "source": OVERRIDE_SOURCE, "ts": ts})
            for w in (pc_spec.get("remove") or []):
                w = str(w).strip()
                if w in consts:
                    consts.remove(w)
                    applied.append({"field": "page_constants", "op": "remove",
                                    "value": w, "source": OVERRIDE_SOURCE, "ts": ts})
                else:
                    noop.append({"field": "page_constants", "value": w,
                                 "reason": "本来就不在页常量里"})
            if consts != list(out.get("page_constants") or []):
                out["page_constants"] = consts

    if not isinstance(attrs, dict):
        attrs = {}

    for attr, spec in attrs.items():
        attr = str(attr)
        if not isinstance(spec, dict):
            skipped.append({"attr": attr, "reason": "覆盖项不是对象（应为 {…}）"})
            continue
        if spec.get("source") != OVERRIDE_SOURCE:
            skipped.append({
                "attr": attr,
                "reason": f'缺 source:"{OVERRIDE_SOURCE}" —— 人的判断必须可与统计结论分辨',
                "got": spec.get("source")})
            continue

        ts = str(spec.get("ts") or "")

        # ---- pin 属性序 ----
        if "position" in spec:
            try:
                pos = int(spec["position"])
            except (TypeError, ValueError):
                skipped.append({"attr": attr, "reason": f"position 不是整数: {spec['position']!r}"})
                pos = None
            if pos is not None:
                if attr not in order:
                    skipped.append({"attr": attr, "reason": "属性不在学出的属性序里（pin 只做重排，不新增属性）"})
                elif not (1 <= pos <= len(order)):
                    skipped.append({"attr": attr, "reason": f"position={pos} 越界（合法范围 1..{len(order)}）"})
                elif order.index(attr) + 1 == pos:
                    noop.append({"attr": attr, "field": "position", "value": pos,
                                 "reason": "已在第 %d 位" % pos})
                else:
                    pins.append((attr, pos))
                    applied.append({"attr": attr, "field": "position", "value": pos,
                                    "from": order.index(attr) + 1,
                                    "source": OVERRIDE_SOURCE, "ts": ts})

        # ---- 强制 / 取消必填 ----
        if "required" in spec:
            want = bool(spec["required"])
            if want and attr not in required:
                if attr not in order:
                    skipped.append({"attr": attr, "reason": "属性不在学出的属性序里，无法设为必填"})
                else:
                    required.append(attr)
                    if attr in optional:
                        optional.remove(attr)
                    applied.append({"attr": attr, "field": "required", "value": True,
                                    "from": False, "source": OVERRIDE_SOURCE, "ts": ts})
            elif (not want) and attr in required:
                required.remove(attr)
                applied.append({"attr": attr, "field": "required", "value": False,
                                "from": True, "source": OVERRIDE_SOURCE, "ts": ts})
            else:
                noop.append({"attr": attr, "field": "required", "value": want,
                             "reason": "已是该状态"})

    # 重排：先摘下被 pin 的，再按 position 升序插回（未 pin 的相对序不变）
    if pins:
        pinned = {a for a, _ in pins}
        rest = [a for a in order if a not in pinned]
        for attr, pos in sorted(pins, key=lambda x: x[1]):
            rest.insert(max(0, min(pos - 1, len(rest))), attr)
        order = rest
        # 属性序动了 ⇒ 让 `required` 回到 order 的**子序列序**，避免两个列表互相矛盾。
        # （只在 pins 非空时做：没有 pin 就不该改动 `required` 的顺序。）
        _req = set(required)
        required = [a for a in order if a in _req]

    if not (applied or noop or skipped):
        return learned, [], [], []          # 全是空壳覆盖 → 视同没有

    sk["order"] = order
    sk["required"] = required
    sk["optional"] = [a for a in optional if a not in set(required)]
    out["skeleton"] = sk

    meta = out.setdefault("_meta", {})
    if applied:
        meta["overrides_applied"] = applied
    # ★ `noop` **刻意不落盘**：它是"本次运行没改动"的观察，每轮都会重复出现 ⇒
    #   落进去只会让产物膨胀成噪音。它仍走返回值，命令面照常显示（人看得到）。
    #   `skipped` 相反 —— "这条覆盖没生效"是个**持续状态**，必须留在产物里等人处理。
    if skipped:
        meta["overrides_skipped"] = skipped
    meta["overrides_file"] = overrides_path(str(out.get("profile_id") or ""), data_dir).name
    meta["overrides_note"] = ("以上条目来自人工覆盖层（source=human），"
                              "**不是**从金标准统计出来的 —— 复现请连 overrides 文件一起带走。")
    return out, applied, noop, skipped


def learn_and_save(profile_id: str, gold_dir: Optional[Path] = None,
                   contracts_dir: Optional[Path] = None,
                   data_dir: Optional[Path] = None,
                   notes_dir: Optional[Path] = None) -> dict:
    """学习 + 叠加人工覆盖 + 落盘（飞轮入口：金标准变了就重学一次）。

    ★ 叠加放在 `save` **之前**、`learn` **之后**：`learn()` 保持"纯统计"语义
      （它的产物是 overrides 的输入之一），落盘的才是"统计 ⊕ 人定"的最终态。
    """
    learned = learn(profile_id, gold_dir, contracts_dir, notes_dir=notes_dir,
                    data_dir=data_dir)
    _nr = learned.get("notes") or {}
    log.info("[rule_learn] 消费了 %d 条裁决备注（可操作 %d 条）",
             _nr.get("n_notes", 0), _nr.get("n_consumed", 0))
    if learned["n_rows"]:
        ov = load_overrides(profile_id, data_dir)
        if ov:
            learned, _ap, _np, _sk = apply_overrides(learned, ov, data_dir)
            if _sk:
                log.warning("[rule_learn] 覆盖层有 %d 条未生效（详见 _meta.overrides_skipped）: %s",
                            len(_sk), "; ".join(s.get("reason", "") for s in _sk[:3]))
        save(learned, data_dir)
    return learned


# ============================================
# 消费口：转成提示词可用的紧凑形式
# ============================================
def as_prompt_rules(learned: dict) -> dict:
    """学到的规则 → 提示词注入用的紧凑 dict（空规则 → {}）。

    只带**真正影响"怎么读"**的信息：属性顺序、必填、页眉常量、值示例、
    跨列续接、形制线索。不含统计噪声（support/cardinality 是人看的，不是模型看的）。
    """
    if not learned or not (learned.get("skeleton") or {}).get("order"):
        return {}
    sk = learned["skeleton"]
    out: Dict[str, Any] = {"attr_order": list(sk.get("order") or [])}
    if not out["attr_order"]:
        return {}
    if sk.get("required"):
        out["required"] = list(sk["required"])
    if sk.get("anchor"):
        out["anchor"] = sk["anchor"]
    if learned.get("page_constants"):
        out["page_constants"] = list(learned["page_constants"])
    if learned.get("value_examples"):
        out["value_examples"] = {k: list(v) for k, v in learned["value_examples"].items()}
    pat = learned.get("patterns") or {}
    hints: List[str] = []
    if pat.get("capital_unit_chars"):
        hints.append("资本值含銀衡单位字：" + "".join(pat["capital_unit_chars"]))
    if pat.get("addr_heads"):
        hints.append("地址常以「" + "」「".join(pat["addr_heads"][:4]) + "」起头")
    if hints:
        out["pattern_hints"] = hints
    cont = learned.get("continuation") or {}
    if cont.get("n_pairs"):
        out["continuation"] = cont.get("rule", "")
        if cont.get("examples"):
            out["continuation_examples"] = [
                f"{e['first']}｜{e['second']} → {e['merged']}"
                for e in cont["examples"][:4]]
    return out


def summary(learned: dict) -> dict:
    """给界面看的规则摘要（不含长值，便于一行展示）。"""
    if not learned:
        return {"ok": False, "reason": "尚未学习（无金标准或未运行）"}
    sk = learned.get("skeleton") or {}
    return {
        "ok": True, "learned_at": (learned.get("_meta") or {}).get("生成时间", ""),
        "n_pages": learned.get("n_pages"), "n_rows": learned.get("n_rows"),
        "skeleton": sk.get("order") or [], "anchor": sk.get("anchor", ""),
        "required": sk.get("required") or [],
        "n_constants": len(learned.get("page_constants") or []),
        "n_example_attrs": len(learned.get("value_examples") or {}),
        "n_title_candidates": len(learned.get("title_candidates") or []),
        "n_enum_attrs": len(learned.get("observed_enums") or {}),
        "n_continuations": (learned.get("continuation") or {}).get("n_pairs", 0),
        "n_notes_consumed": (learned.get("notes") or {}).get("n_consumed", 0),
    }


# ============================================
# 规则变更 diff（G2-②，2026-09-16）
# ============================================
# **为什么需要**：`save()` 是**整份覆盖** —— 学完一圈，上一圈的规则长什么样就
# 没人知道了。飞轮的每一圈都该"看得见"，否则 `triage` 的结果变了（页面从
# `measurable` 变成 `unmeasurable`、或反过来），无法归因到"规则变了"还是"料变了"。
#
# ★ 两条纪律：
#   ① **只读**：本函数不落盘、不改产物（改动只在 `learn` 那条路上）；
#   ② **空 diff 必须显式说"没变"**：否则分不清"没变化"与"命令没跑"。
def _diff_list(old_list, new_list) -> Dict[str, List[str]]:
    """两个字符串列表的增 / 删（忽略顺序）。"""
    o = [str(x) for x in (old_list or [])]
    n = [str(x) for x in (new_list or [])]
    so, sn = set(o), set(n)
    return {"added": [x for x in n if x not in so],
            "removed": [x for x in o if x not in sn]}


def _diff_order(old_order, new_order) -> Dict[str, Any]:
    """属性序的增 / 删 / **换位**（换位是"序"独有的一类变化）。"""
    d = _diff_list(old_order, new_order)
    oi = {str(a): i for i, a in enumerate(old_order or [])}
    ni = {str(a): i for i, a in enumerate(new_order or [])}
    moved = [{"attr": a, "from": oi[a] + 1, "to": ni[a] + 1}
             for a in ni if a in oi and oi[a] != ni[a]]
    moved.sort(key=lambda m: m["to"])
    d["moved"] = moved
    return d


def _diff_map_sets(old_map, new_map) -> Dict[str, Any]:
    """`{属性: [值…]}` 两边的**值集合**变化（只报属性名与增删值，不逐条比顺序）。"""
    om = old_map if isinstance(old_map, dict) else {}
    nm = new_map if isinstance(new_map, dict) else {}
    keys = {"added": sorted(set(nm) - set(om)), "removed": sorted(set(om) - set(nm))}
    changed = {}
    for k in sorted(set(om) & set(nm)):
        d = _diff_list(om[k], nm[k])
        if d["added"] or d["removed"]:
            changed[k] = d
    return {"keys": keys, "changed": changed}


def diff_learned(old: Optional[dict], new: Optional[dict]) -> dict:
    """两份 `learned` 的结构化差异（**纯只读**）。`old` 为空 = 首次学习。

    返回 `{first, changed, input, sections, lines}`：
      - `input`  输入规模（`n_pages` / `n_rows` / `gold_scope`）—— **必须先看这个**，
                 否则"规则没变"有可能只是"根本没读到料"（两者现象一样）；
      - `lines`  人读摘要，**空 diff 时也非空**（明确说"没变"）。
    """
    if not old:
        n = new or {}
        return {"first": True, "changed": True,
                "input": {"n_pages": [None, n.get("n_pages")],
                          "n_rows": [None, n.get("n_rows")]},
                "sections": {},
                "lines": [f"首次学习：{n.get('n_pages')} 页 / {n.get('n_rows')} 行 "
                          f"→ 骨架 {len((n.get('skeleton') or {}).get('order') or [])} 项"]}

    o = old or {}
    n = new or {}
    sections: Dict[str, Any] = {}

    order_d = _diff_order((o.get("skeleton") or {}).get("order"),
                          (n.get("skeleton") or {}).get("order"))
    if order_d["added"] or order_d["removed"] or order_d["moved"]:
        sections["skeleton.order"] = order_d

    req_d = _diff_list((o.get("skeleton") or {}).get("required"),
                       (n.get("skeleton") or {}).get("required"))
    if req_d["added"] or req_d["removed"]:
        sections["skeleton.required"] = req_d

    for key, label in (("page_constants", "page_constants"),
                       ("title_candidates", "title_candidates")):
        d = _diff_list(o.get(key), n.get(key))
        if d["added"] or d["removed"]:
            sections[label] = d

    ve = _diff_map_sets(o.get("value_examples"), n.get("value_examples"))
    if ve["keys"]["added"] or ve["keys"]["removed"]:
        sections["value_examples"] = ve["keys"]      # ★ 只报**键集**：值逐条报太吵

    oe = _diff_map_sets(o.get("observed_enums"), n.get("observed_enums"))
    if oe["keys"]["added"] or oe["keys"]["removed"] or oe["changed"]:
        sections["observed_enums"] = oe

    co, cn = (o.get("continuation") or {}), (n.get("continuation") or {})
    if (co.get("rule"), co.get("n_pairs")) != (cn.get("rule"), cn.get("n_pairs")):
        sections["continuation"] = {"added": [str(cn.get("rule") or "")],
                                    "removed": [str(co.get("rule") or "")],
                                    "n_pairs": [co.get("n_pairs"), cn.get("n_pairs")]}

    inp = {"n_pages": [o.get("n_pages"), n.get("n_pages")],
           "n_rows": [o.get("n_rows"), n.get("n_rows")]}
    gs_o = (o.get("gold_scope") or {}) if isinstance(o.get("gold_scope"), dict) else {}
    gs_n = (n.get("gold_scope") or {}) if isinstance(n.get("gold_scope"), dict) else {}
    gsd = _diff_list(sorted(gs_o), sorted(gs_n))
    if gsd["added"] or gsd["removed"]:
        inp["gold_scope"] = gsd

    changed = bool(sections)

    lines: List[str] = []
    lines.append(f"输入：{inp['n_pages'][0]} → {inp['n_pages'][1]} 页 / "
                 f"{inp['n_rows'][0]} → {inp['n_rows'][1]} 行")
    if not changed:
        lines.append("规则**没有变化**：属性序 / 必填 / 页常量 / 值示例 / 取值枚举 / "
                     "标题候选 / 跨列续接逐项相同。")
    else:
        if "skeleton.order" in sections:
            d = sections["skeleton.order"]
            bits = []
            if d["added"]:
                bits.append("新增 " + "/".join(d["added"]))
            if d["removed"]:
                bits.append("移除 " + "/".join(d["removed"]))
            if d["moved"]:
                bits.append("换位 " + "、".join(
                    f"{m['attr']}(第{m['from']}→第{m['to']}位)" for m in d["moved"]))
            lines.append("属性序：" + "；".join(bits))
        if "skeleton.required" in sections:
            d = sections["skeleton.required"]
            bits = []
            if d["added"]:
                bits.append("变为必填 " + "/".join(d["added"]))
            if d["removed"]:
                bits.append("不再必填 " + "/".join(d["removed"]))
            lines.append("必填集合：" + "；".join(bits))
        if "page_constants" in sections:
            d = sections["page_constants"]
            lines.append("页常量：+" + "、".join(d["added"] or ["（无）"]) +
                         " −" + "、".join(d["removed"] or ["（无）"]))
        if "value_examples" in sections:
            d = sections["value_examples"]
            lines.append("值示例涉及属性：+" + "、".join(d["added"] or ["（无）"]) +
                         " −" + "、".join(d["removed"] or ["（无）"]))
        if "observed_enums" in sections:
            d = sections["observed_enums"]
            lines.append(f"取值枚举：涉及属性 +{'、'.join(d['keys']['added']) or '（无）'}"
                         f" −{'、'.join(d['keys']['removed']) or '（无）'}；"
                         f"其中 {len(d['changed'])} 个属性取值集合有增减")
        if "title_candidates" in sections:
            d = sections["title_candidates"]
            lines.append("标题候选：+" + "、".join(d["added"] or ["（无）"]) +
                         " −" + "、".join(d["removed"] or ["（无）"]))
        if "continuation" in sections:
            lines.append(f"跨列续接规则变了：{sections['continuation']['n_pairs'][0]} → "
                         f"{sections['continuation']['n_pairs'][1]} 对")
    return {"first": False, "changed": changed, "input": inp,
            "sections": sections, "lines": lines}
