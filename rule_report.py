from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import rule_learn as RL
import rule_split as RS


def _fmt_support(sk: dict) -> List[str]:
    out = []
    order = sk.get("order") or []
    support = sk.get("support") or {}
    required = set(sk.get("required") or [])
    optional = set(sk.get("optional") or [])
    for i, a in enumerate(order, 1):
        tag = "必填" if a in required else ("可选" if a in optional else "—")
        sup = support.get(a)
        sup_s = ("（出现率 %.0f%%）" % (sup * 100)) if isinstance(sup, (int, float)) else ""
        out.append("%d. **%s** — %s%s" % (i, a, tag, sup_s))
    return out


def _anchor_word_list() -> str:
    words = [w for w in RS._STRONG_ANCHOR_WORDS if isinstance(w, str)]
    return "、".join(words[:12]) + ("…" if len(words) > 12 else "")


def _regex_words(rx_text: str, limit: int = 8) -> str:
    """把「(?:a|b|c)」形态的词表正则转成人话（顿号列举）。"""
    s = rx_text.strip()
    if s.startswith("(?:") and s.endswith(")"):
        s = s[3:-1]
    parts = [p for p in s.split("|") if p and not p.startswith("(?")]
    return "、".join(parts[:limit]) + ("…" if len(parts) > limit else "")


def _override_line(it: dict) -> str:
    """覆盖条目 → 一行人话（2026-09-27 复盘修复：原 [:120] 硬截会把 JSON 切半截；
    已知形状转人话，未知形状**全文** JSON，绝不中途截断）。"""
    if not isinstance(it, dict):
        return str(it)
    f = it.get("field")
    if f == "page_constants":
        core = "页常量%s「%s」" % ("增" if it.get("op") == "add" else "删",
                                  it.get("value"))
    elif it.get("attr") and f == "position":
        core = "属性「%s」移到第 %s 位（原第 %s 位）" % (
            it["attr"], it.get("value"), it.get("from"))
    elif it.get("attr") and f == "required":
        core = "属性「%s」%s" % (it["attr"],
                                 "设为必填" if it.get("value") else "改为非必填")
    else:
        core = json.dumps(it, ensure_ascii=False)
    ts = str(it.get("ts") or "")
    return core + ("（人定 %s）" % ts[:10] if ts else "（人定）")


def _skip_line(it: dict) -> str:
    """skipped 条目 → 一行人话：谁 + 为什么。reason 缺失才回退完整 JSON。"""
    if not isinstance(it, dict):
        return str(it)
    who = ("属性「%s」" % it["attr"]) if it.get("attr") else (
        ("字段「%s」" % it["field"]) if it.get("field") else "条目")
    reason = str(it.get("reason") or json.dumps(it, ensure_ascii=False))
    return "%s：%s" % (who, reason)


def _hint_line(hint: dict) -> str:
    """裁决备注（attr_hint）→ 一行人话。note 在 learn 层已有 ≤120 上限。"""
    if not isinstance(hint, dict):
        return "属性纠正：%s" % hint
    tail = (" —— 备注原文：%s" % hint["note"]) if hint.get("note") else ""
    return "属性纠正（%s）：%s%s" % (hint.get("file") or "?",
                                     hint.get("hint") or "", tail)


_SHAPE_ZH = {"dateish": "日期类", "money": "金额类", "short": "短行",
             "body": "正文", "other": "其他"}


def _corpus_section(L: List[str], learned: dict, pid: str,
                    data_dir: Optional[Path]) -> None:
    """「材料画像（全语料通读）」节（scan · 2026-09-29）。

    ★ learned 里无 corpus 块 ⇒ 整节不出现（不渲染"（无）"空节）——
      画像缺席时说明书与改动前逐字节等价（零影响不变量的人审层镜像）。
    ★ 不带节号：「三/四/五」的编号被既有测试与读者引用锁死，新节插队不重排。
    """
    corpus = learned.get("corpus")
    if not isinstance(corpus, dict) or not corpus:
        return
    L.append("## 材料画像（全语料通读）")
    L.append("")
    n_pages = corpus.get("n_pages")
    L.append("- 通读页数：%s 页（来源：%s；全语料零标注统计，先于 learn）"
             % (n_pages if n_pages is not None else "?",
                corpus.get("scan_path") or "corpus_<pid>.json"))
    added = corpus.get("constants_added") or []
    if added:
        L.append("- 已自动并入页常量（corpus 来源，跨页出现率 ≥ %.0f%%）：%s"
                 % (RL.CORPUS_ADD_RATIO * 100, "、".join(added)))
    cands = corpus.get("constants_candidates") or []
    if cands:
        L.append("- 候选常量（**未自动采纳**，出现率 %.0f%%–%.0f%%）："
                 % (RL.CORPUS_CAND_RATIO * 100, RL.CORPUS_ADD_RATIO * 100))
        for c in cands:
            ratio = c.get("ratio")
            pct = ("%.0f%%" % (ratio * 100)) if isinstance(ratio, (int, float)) else "?"
            L.append("  - %s（出现于 %s 页面）" % (c.get("text"), pct))
    shape = corpus.get("shape_stats") or {}
    if shape:
        parts = []
        for k, v in sorted(shape.items(),
                           key=lambda kv: -(kv[1] if isinstance(kv[1], (int, float)) else 0)):
            zh = _SHAPE_ZH.get(k, k)
            parts.append(("%s %.0f%%" % (zh, v * 100))
                         if isinstance(v, (int, float)) else "%s ?" % zh)
        L.append("- 行形态占比（人话版）：" + "、".join(parts))
    n_flagged = corpus.get("qa_n_flagged") or 0
    flagged_lines: List[str] = []
    full = RL.load_corpus(pid, data_dir) or {}
    for p in ((full.get("qa") or {}).get("pages") or []):
        if not (p.get("flags") or []):
            continue
        cer = p.get("cer")
        cer_s = ("，错字率代理 %.0f%%" % (cer * 100)) if isinstance(cer, (int, float)) else ""
        flagged_lines.append("  - %s（%s 行 / 行长中位 %s）：%s%s"
                             % (p.get("page"), p.get("lines"), p.get("len_median"),
                                "、".join(p["flags"]), cer_s))
    if n_flagged:
        L.append("- ⚠ QA 异常页 %d 页：" % n_flagged)
        L.extend(flagged_lines or ["  - （明细读不到，见 corpus 文件）"])
    else:
        L.append("- QA 异常页：无")
    L.append("")


def _stop_words_list() -> str:
    """右界终结词 → 顿号列举（norm 空间去重后的规范形）。"""
    return "、".join(RS._VALUE_STOP_WORDS)


def _extraction_rows() -> List[tuple]:
    return [
        ("公司名（记录起点）",
         "长度 %d–%d 字；裸碎行（公司/有限公司/總號）判非起点；"
         "以「有限/有限公」结尾的短行豁免强锚词排除（OCR 拆开的名行前半）；"
         "命中强锚词（%s）排除；后继行长度 ≥ %d 字视为正文，"
         "或后一行为 ≤2 字后缀碎片（非裸碎行）且再下一行为正文"
         % (RS.NAME_MIN_LEN, RS.NAME_MAX_LEN_DEFAULT, _anchor_word_list(),
            RS.BODY_MIN_LEN_DEFAULT)),
        ("无锚记录体兜底开条",
         "正文行以纪年日期开头、日期后 ≤16 字内出现「创办」、且前一行为非起点行"
         " → 强制开条：公司名留空、整页置信降为低（open_rule=date_body_fallback "
         "留痕于切分明细）"),
        ("人名（注册人一/二/三/其他）",
         "长度默认 %d 字（%d–%d 界）；切块前剥除纪年/注册标记；"
         "日期构字占比 ≥ %.0f%% 的段整段放弃（防「光緒三十一年十二月」被切成人名）"
         % (RS.NAME_LEN_DEFAULT, RS.NAME_MIN_LEN, RS.NAME_MAX_LEN_DEFAULT,
            RS._DATEISH_REGION_RATIO * 100)),
        ("日期（创立时间/注册时间）",
         "纪年词（%s…）+ 年 + 月? + 日?；创立时间锚要求日期构字段**直达注册标记**"
         "（%s）——防把完整日期截短后偷锚；注册时间取值**剥离尾部注册标记**"
         "（「註冊」尾标是分隔符不是值，值不含尾标）"
         % ("、".join(RS._ERA_CHARS[:4]), _regex_words(RS._REG_MARK))),
        ("地址（总公司地址）",
         "开头词：%s；另配「辦事所在」直配形态（manual 页地址头，自带「在」）；"
         "取值遇终结词（%s）截断，防吞分销处/分号段"
         % (_regex_words(RS._ADDR_HEAD), _stop_words_list())),
        ("资本（注册资本）",
         "开头词：%s；头词命中检查前字——「官股銀／商股銀」里的「股銀」是更长"
         "单位词组合的内截命中，不作右界（防资本被切剩「股分官」）；"
         "取值遇终结词（%s）截断，防吞「每股…」「獨力」「爲」尾巴"
         % (_regex_words(RS._CAP_HEAD), _stop_words_list())),
        ("类别（公司类别）",
         "类别词：%s" % _regex_words(RS._CAT_WORD)),
    ]


def render(profile: dict, learned: dict, data_dir: Optional[Path] = None) -> str:
    """规则说明书（人可读 markdown）。数据源 = learned + overrides 现状 + 代码判据常量。"""
    pid = profile.get("profile_id", "")
    ov = RL.load_overrides(pid, data_dir)
    meta = learned.get("_meta") or {}
    applied = meta.get("overrides_applied") or []
    skipped = meta.get("overrides_skipped") or []
    sk = learned.get("skeleton") or {}
    consts = list(learned.get("page_constants") or [])
    notes = learned.get("notes") or {}
    L: List[str] = []
    L.append("# 识别规则说明书 — %s" % (profile.get("name") or pid))
    L.append("")
    L.append("- 生成时间：%s" % datetime.now().isoformat(timespec="seconds"))
    L.append("- 例题范围：%d 页 / %d 行（manual_annotations/ 金标准）"
             % (learned.get("n_pages") or 0, learned.get("n_rows") or 0))
    L.append("- 本文件由 `chronicles profile rules` 生成：**这是系统将从每页提取什么的"
             "完整声明**。你审的不是指标，是规则本身。")
    L.append("")
    L.append("## 一、结构规则（统计学习 → 可人工覆盖）")
    L.append("")
    L.extend(_fmt_support(sk))
    anchor = sk.get("anchor") or ""
    L.append("")
    L.append("- 锚属性：**%s**（分诊与记录切分都认它）" % anchor if anchor else "")
    L.append("- 属性基数：每条记录 %.1f 个值（%d–%d）"
             % ((sk.get("cardinality") or {}).get("mean", 0),
                (sk.get("cardinality") or {}).get("min", 0),
                (sk.get("cardinality") or {}).get("max", 0)))
    L.append("- ✏ 改法：`chronicles profile override --profile %s --attr <属性> "
             "--position N / --required / --optional`" % (profile.get("name") or pid))
    L.append("")
    L.append("## 二、页面规则")
    L.append("")
    if consts:
        corpus_added = {a.get("text")
                        for a in (meta.get("corpus_constants_added") or [])
                        if isinstance(a, dict)}
        L.append("页常量（这些文字是版面固定件，**不计入任何记录**）：")
        for c in consts:
            L.append("- %s%s" % (c, "（corpus 自动并入）" if c in corpus_added else ""))
    else:
        L.append("（无页常量）")
    extra = (notes.get("page_constants_extra") or []) if isinstance(notes, dict) else []
    if extra:
        L.append("- 其中来自你的备注：%s" % "、".join(extra))
    L.append("- ✏ 改法：`chronicles profile override --profile %s --page-const-add <词> "
             "／ --page-const-remove <词>`" % (profile.get("name") or pid))
    L.append("")
    _corpus_section(L, learned, pid, data_dir)
    L.append("## 三、提取判据（代码启发式 — 人审对象）")
    L.append("")
    L.append("| 属性 | 判据 |")
    L.append("|---|---|")
    for name, desc in _extraction_rows():
        L.append("| %s | %s |" % (name, desc))
    L.append("")
    L.append("单值上限 %d 字；判据本体在 `rule_split.py`（模块常量，每条都能指到行）。"
             % RS.MAX_VALUE_CHARS)
    L.append("**你不同意某条判据时**：在裁决页写备注（notes），learn 会把它登记进规则"
             "材料并在下一份说明书里可见。")
    L.append("")
    L.append("## 四、几何规则")
    L.append("")
    L.append("- 值在列内的子框定位：余量比 ≥ %.2f → 贴底；否则居中"
             "（`preannotate.PAD_HEAD_RATIO`；已知边界：列内留白大但文字居中的版式"
             "会错位，随裁决产物滚动复验）" % 0.15)
    L.append("")
    L.append("## 五、人工层现状")
    L.append("")
    L.append("- 覆盖层文件：%s"
             % ("已存在（%d 条已生效）" % len(applied) if applied else "尚未创建"))
    for it in applied:
        L.append("  - %s" % _override_line(it))
    if skipped:
        L.append("- ⚠ %d 条覆盖**未生效**（待人处理，绝不静默丢弃）：" % len(skipped))
        for it in skipped:
            L.append("  - %s" % _skip_line(it))
    n_notes = notes.get("n_notes") or 0
    n_used = notes.get("n_consumed") or 0
    L.append("- 裁决备注：%d 条读入，%d 条已消费进规则" % (n_notes, n_used))
    for hint in (notes.get("attr_hints") or []):
        L.append("  - %s" % _hint_line(hint))
    L.append("")
    L.append("## 审阅提示")
    L.append("")
    L.append("1. 逐节核对：**这就是系统读每一页的方式**；")
    L.append("2. 要改的结构项用 `profile override`（学出来的与您定的永远分文件可辨）；")
    L.append("3. 改完重跑 `profile learn` 覆盖即生效（`_meta.overrides_applied` 可核）；")
    L.append("4. 判据级意见（第三节）请用裁决页备注，不要直接改代码。")
    if isinstance(learned.get("corpus"), dict) and learned.get("corpus"):
        L.append("5. 材料画像的候选常量只是候选：认可的用 "
                 "`profile override --page-const-add <词>` 收编进页常量。")
    return "\n".join(L)


def build_report(profile_id: str, profiles_dir=None, rules_dir=None) -> Dict[str, Any]:
    """`profile rules` 动作本体：渲染说明书，返回结果 + 人读文本。"""
    import profile_cli as PC

    res: Dict[str, Any] = {"ok": False, "action": "rules", "profile_id": ""}
    p = PC.resolve_profile(profile_id, profiles_dir)
    if p is None:
        res["error"] = "档案不存在：%s" % profile_id
        return res
    pid = p["profile_id"]
    res["profile_id"] = pid
    learned_path = RL.learned_path(pid, rules_dir)
    if not learned_path.exists():
        res["error"] = ("尚无 learned_%s.json —— 先跑 `chronicles profile learn "
                        "--profile %s`" % (pid, profile_id))
        return res
    learned = json.loads(Path(learned_path).read_text(encoding="utf-8"))
    md = render(p, learned, rules_dir)
    res["ok"] = True
    res["report_md"] = md
    res["learned_path"] = str(learned_path)
    res["next"] = ("审阅以上规则；要改结构项用 `profile override`，改完重跑 "
                   "`profile learn` 生效后再跑批。")
    return res
