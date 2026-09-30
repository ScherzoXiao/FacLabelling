# -*- coding: utf-8 -*-
"""corpus_scan —— **材料画像**：learn 之前的无标注通读（2026-09-29）。

定位（设计稿 §二）：对 `data/structured/` 全部
池页做**确定性统计**（零 LLM 语义、零属性名先验），把「发现分布」从 3 页金标准
挪到全语料，产出画像供 `rule_learn.learn()` 消费与人审（rule_report 材料画像节）。

★ 三条纪律：
  1. **行读取单一实现**：本模块取页行**只经** `preannotate.page_lines()`（几何复活
     + 阅读序同源），不另写任何 structured 解析器（§60）。`L0_document.total_columns`
     是唯一的例外直读 —— 它是**页级元数据**而非行文本，`page_lines` 不透出该字段。
  2. **零先验**：形态分类只看字符形态（纪年词/数字词/单位字），不看属性名；
     档案归属由操作者声明（`profile_id` 只写进 `_meta`，不自动推断）——
     池页按纪律「未归档的行不属于任何档案」，选择口不约束。
  3. **纯函数 + 单独落盘**：`scan_corpus()` 只算不写；落盘走 `save_corpus()`
     （`data_io.atomic_write_json` 原子写），与 `rule_learn.learn / learn_and_save`
     同款分工。命令面（profile_cli._do_scan）负责参数 → 调用 → 退出码映射。

★ 归一化与匹配放宽（2026-09-29 补做指令）：
  - 常量候选在**归一化文本**上算：去空白与中英标点 + 最小简繁折叠
    （fold 表随 `_meta.fold_table` 落盘，可解释可复核）；
  - 整行 exact-match 之外，加**归一化行文本的 n-gram（3–6 字）页级文档频率**：
    `df_ratio = 含该 n-gram 页数 / n_pages`，`df_ratio ≥ 0.9` 的 n-gram 按
    前后缀重叠合并为**极大串**候选（候选带 `source`: exact / ngram）；
  - `diagnostics` 节报总行数、候选总数与 top20（含 ratio，无论是否过线）——
    画像的"发现"过程必须可见，不许只有过线结果。

★ 非页文件排除（判据写死）：structured 顶层 json 若**不含任何**页层键
  （`L2_lines` / `L1_blocks` / `L1_sections` / `L0_document`），判为系统产物
  （如实测 `repair_report.json`：只有 generated_at/total_files 等工具输出），
  **默认不进扫描**，名单记入 `_meta.excluded_nonpage`（可见不静默）。

OCR 错字率代理（设计稿允许缺席）：与金标准同名页的逐行对齐字符错误率 ——
**本期不做**，无金标准的页该字段缺席（不猜）。TODO(画像v2)：复用 align_eval
对齐后按页报 CER，仅对有金标准的页。
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import data_io
import string

# ============================================================
# 归一化：标点剥离 + 最小简繁折叠
# ============================================================
# 折叠方向：简→繁（本库 OCR 主态是繁体语料）。**最小表**：只收样例材料
# 实测高频差集（烟测 top 候选里「公司註册」与「公司註冊」并存即为直接证据）。
# ★ 单一实现（2026-09-29）：表体 = `rule_split.S2T_FOLD`，本模块与
#   `rule_learn._apply_corpus` 的折叠去重共用同一张表（不另造第二份）；
#   表内容随画像 `_meta.fold_table` 落盘 —— 判据可解释，不藏在代码里。
from rule_split import S2T_FOLD            # noqa: E402  与匹配层同源
_FOLD_TRANS = str.maketrans(S2T_FOLD)

# 中英标点（剥离口径：空白由 isspace 覆盖，含全角空格）
PUNCT_SET = frozenset(
    string.punctuation + "，。、；：？！「」『』（）《》〈〉…—·・“”‘’【】〔〕～～"
    "－＋＝＜＞／＼｜『』")


def normalize_line(text) -> str:
    """行文本归一化：简繁折叠 → 去空白与中英标点。"""
    t = str(text or "").translate(_FOLD_TRANS)
    return "".join(c for c in t if not c.isspace() and c not in PUNCT_SET)


# ============================================================
# 形态分类判据（零属性名先验；字符形态是语言常识、不是属性先验）
# ============================================================
# 纪年词（dateish 判据①）：常见年号 + 民国纪年。繁简都收（OCR 文本两态都有）。
ERA_WORDS = ("光緒", "光绪", "宣統", "宣统", "康熙", "乾隆", "嘉慶", "嘉庆",
             "道光", "咸豐", "咸丰", "同治", "順治", "顺治", "雍正",
             "民國", "民国", "明治", "大正", "昭和")
DATE_UNITS = ("年", "月", "日")            # dateish 判据②
# 钱额单位（money 判据②）：设计稿给「元/两/万/千/股」；本库 OCR 是繁体
# （实测「洋銀十萬圓」），故收 繁体同形字 萬/兩/圓/圆，否则该类在本语料恒为 0。
MONEY_UNITS = ("元", "圓", "圆", "两", "兩", "万", "萬", "千", "股")
NUM_CHARS = frozenset("0123456789零一二三四五六七八九十百千两兩")

CONST_MIN_LEN = 2     # 常量候选（exact）：归一化行文本字长下限
CONST_MAX_LEN = 30    # 常量候选（exact）：字长上限（设计稿 §二）

# n-gram 文档频率（2026-09-29 补做指令②）
NGRAM_MIN = 3          # n-gram 长度下限
NGRAM_MAX = 6          # n-gram 长度上限
NGRAM_DF_RATIO = 0.9   # 入候选线：df_ratio ≥ 0.9
NGRAM_MERGE_MAX = 30   # 极大串合并后的字长上限（与 exact 窗口同量级）

SHAPES = ("dateish", "money", "short", "body", "other")

THIN_RATIO = 0.4        # thin：页行数 < 全库行数 median × 0.4
OUTLIER_RATIO = 1.5     # outlier_len：页行长中位数 > 全库行长 p90 × 1.5

# 垃圾页信号 kana_heavy + 疑误字信号 kana_trace（P12，2026-09-30）：
# 假名字符（ぁ-ん ァ-ヶ）占全页字符比：
# > 0.3 → "kana_heavy"（整页垃圾，如 样例页_0001 = 74.0%，其余页无一 >5%，分离度完美）；
#   0 < ratio ≤ 0.3 → "kana_trace"（疑误字页）。
# ★材料级事实（用户 2026-09-30 裁定）：样例材料为纯中文语料，不含日文——
#   任何假名痕迹都是 OCR 误判（该认汉字的字被认成了假名），本身就是疑错信号，
#   不论占比多小。此语义对「纯中文语料」类材料成立；若未来材料本身含日文，
#   应由该档案显式声明豁免（与排除带同人裁逻辑，不自动推断）。
KANA_RATIO_FLAG = 0.3   # kana_heavy 触发阈值（>）；≤ 且 >0 触发 kana_trace


def kana_ratio(texts: List[str]) -> float:
    """假名率（P12 kana_heavy 读数）：假名字符（ぁ-ん ァ-ヶ）占全部字符比。

    口径与页级 chars 一致（strip 后非空行文本之和）；空文本 → 0.0。
    """
    total = sum(len(t) for t in texts)
    if not total:
        return 0.0
    n = 0
    for t in texts:
        for ch in t:
            if ("\u3041" <= ch <= "\u3096") or ("\u30a1" <= ch <= "\u30f6"):
                n += 1
    return n / total

# 非页文件判据（写死、可解释）：缺全部页层键 ⇒ 系统产物，不进扫描
PAGE_KEYS = ("L2_lines", "L1_blocks", "L1_sections", "L0_document")


def classify_shape(text: str) -> str:
    """逐行形态分类（设计稿 §二，判据顺序 dateish → money → short → body → other）。

    在 **strip 后原文**上判（形态先验与标点无关，保持与 qa/行长同口径）。
    `dateish` = 含纪年词且含「年/月/日」；`money` = 含数字词且含钱额单位；
    `short` = ≤6 字且非前两类；`body` = ≥12 字；其余（7–11 字未命中前两类）`other`。
    """
    t = (text or "").strip()
    if any(w in t for w in ERA_WORDS) and any(u in t for u in DATE_UNITS):
        return "dateish"
    if any(c in NUM_CHARS for c in t) and any(u in t for u in MONEY_UNITS):
        return "money"
    n = len(t)
    if n <= 6:
        return "short"
    if n >= 12:
        return "body"
    return "other"


# ============================================================
# 页清单 / 页元数据（CLI 预检与 scan 共用这一份 —— 单一实现）
# ============================================================
def list_pages(structured_dir, pages_glob: Optional[str] = None) -> List[Path]:
    """structured 目录下**待检**文件（顶层 `*.json`，`pages_glob` 可选过滤）。

    ⚠ 只做文件枚举，不做"是不是页"的判定（那在 `_read_page_meta`，要读内容）。
    """
    import fnmatch
    d = Path(structured_dir)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"))
    if pages_glob:
        files = [f for f in files if fnmatch.fnmatch(f.name, pages_glob)]
    return files


def _read_page_meta(page_json: Path) -> Tuple[Optional[int], bool]:
    """读页级元数据 → (total_columns, is_page)。

    `is_page` 判据（写死）：文档含任一页层键（`PAGE_KEYS`）。缺全部键 ⇒ 系统产物
    （如实测 `repair_report.json`）。读不动（坏 json / IO 错）⇒ 非页（不猜）。
    """
    try:
        doc = json.loads(page_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, False
    if not isinstance(doc, dict):
        return None, False
    v = (doc.get("L0_document") or {}).get("total_columns")
    try:
        cols = int(v) if v and int(v) > 0 else None
    except (TypeError, ValueError):
        cols = None
    return cols, any(k in doc for k in PAGE_KEYS)


def _pct(sorted_vals: List[float], q: float) -> float:
    """分位数（线性插值），空表 → 0.0。输入须已升序。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


# ============================================================
# n-gram 文档频率 → 极大串合并
# ============================================================
def _merge_maximal(grams: Set[str]) -> List[str]:
    """高 df n-gram → 极大串：前后缀重叠迭代合并，只留不被任何其他串包含的。

    ⚠ 调用方必须**逐行**喂 gram（同一行的 gram 合并结果必是该行子串）——
    若把全库 gram 混在一起闭包合并，会跨页串接出任何页面都不存在的假串，
    且集合爆炸（实测 200s+）。
    """
    s = {g for g in grams if len(g) <= NGRAM_MERGE_MAX}
    changed = True
    while changed:
        changed = False
        cur = sorted(s)                          # 排序迭代 → 结果确定
        for a in cur:
            for b in cur:
                if a == b:
                    continue
                for ov in range(min(len(a), len(b)), 0, -1):
                    if a[-ov:] == b[:ov]:
                        m = a + b[ov:]
                        if len(m) <= NGRAM_MERGE_MAX and m not in s:
                            s.add(m)
                            changed = True
                        break
    return sorted(t for t in s if not any(t != o and t in o for o in s))


# ============================================================
# 主函数
# ============================================================
def scan_corpus(structured_dir, profile_id: Optional[str] = None,
                pages_glob: Optional[str] = None,
                outbox_dir=None) -> Dict[str, Any]:
    """全语料通读 → 画像 dict（**纯函数，不落盘**；落盘走 `save_corpus`）。

    行文本一律经 `preannotate.page_lines()` 取（几何复活 + 阅读序同源）；
    每页统计行数/字数/行长中位数并打 QA flags；全库层面在**归一化文本**上
    统计常量候选（exact + ngram 双源）、形态占比、行长分布与列数分布。
    """
    import preannotate as PA

    d = Path(structured_dir)
    files = list_pages(d, pages_glob)

    # ---- 非页文件排除（判据见 `_read_page_meta`，名单可见）----
    # 列数在同一次读里一并取出（每文件只读一次，不重复 IO）
    pages: List[Path] = []
    excluded: List[str] = []
    cols_of: Dict[str, Optional[int]] = {}
    for f in files:
        nc, is_page = _read_page_meta(f)
        if is_page:
            pages.append(f)
            cols_of[f.stem] = nc
        else:
            excluded.append(f.name)
    n_pages = len(pages)

    # ---- 逐页：行读取（唯一实现）+ 行级统计 ----
    # 全库阈值（thin / outlier_len 的 median、p90）必须先于页级 flags 算出，
    # 故两段式：本段只收集，flags 在下一段统一打。
    qa_pages: List[Dict[str, Any]] = []
    all_lens: List[int] = []                     # 全库行长（非空行）
    shape_counts = {k: 0 for k in SHAPES}
    exact_hits: Dict[str, set] = {}              # 归一化整行 → 命中页 stem 集
    page_norms: Dict[str, List[str]] = {}        # stem → 归一化行文本列表
    page_grams: Dict[str, Set[str]] = {}         # stem → 该页 n-gram 集（3–6 字）
    col_vals: List[int] = []
    n_lines_total = 0
    for f in pages:
        stem = f.stem
        lines = PA.page_lines(stem, structured_dir=d, outbox_dir=outbox_dir)
        texts = [str(l.get("text") or "").strip() for l in lines]
        texts = [t for t in texts if t]          # 空白行不进行长/形态/常量统计
        lens = [len(t) for t in texts]
        all_lens.extend(lens)
        n_lines_total += len(texts)
        norms = [normalize_line(t) for t in texts]
        norms = [t for t in norms if t]
        page_norms[stem] = norms
        grams: Set[str] = set()
        for t in texts:
            shape_counts[classify_shape(t)] += 1
        for t in norms:
            if CONST_MIN_LEN <= len(t) <= CONST_MAX_LEN:
                exact_hits.setdefault(t, set()).add(stem)
            for n in range(NGRAM_MIN, min(NGRAM_MAX, len(t)) + 1):
                for i in range(len(t) - n + 1):
                    grams.add(t[i:i + n])
        page_grams[stem] = grams
        nc = cols_of.get(stem)
        if nc:
            col_vals.append(nc)
        qa_pages.append({"page": stem, "lines": len(lines), "chars": sum(lens),
                         "len_median": None, "flags": [], "_lens": lens})
        # kana_heavy 读数（P12）：ratio > 0 才落键——零假名页 schema 零变化
        kr = kana_ratio(texts)
        if kr > 0:
            qa_pages[-1]["kana_ratio"] = round(kr, 4)

    # ---- 全库阈值 → 逐页 QA flags（empty / thin / outlier_len）----
    lines_per_page = [p["lines"] for p in qa_pages]
    lines_median = float(statistics.median(lines_per_page)) if lines_per_page else 0.0
    lens_sorted = sorted(all_lens)
    len_median = _pct(lens_sorted, 0.5)
    len_p90 = _pct(lens_sorted, 0.9)
    thin_thresh = lines_median * THIN_RATIO
    outlier_thresh = len_p90 * OUTLIER_RATIO
    for p in qa_pages:
        flags: List[str] = []
        if p["lines"] == 0:
            flags.append("empty")
        else:
            if p["_lens"]:
                p["len_median"] = round(float(statistics.median(p["_lens"])), 2)
            if lines_median > 0 and p["lines"] < thin_thresh:
                flags.append("thin")
            if len_p90 > 0 and (p["len_median"] or 0.0) > outlier_thresh:
                flags.append("outlier_len")
            if p.get("kana_ratio", 0.0) > KANA_RATIO_FLAG:
                flags.append("kana_heavy")
            elif p.get("kana_ratio", 0.0) > 0.0:
                # 纯中文语料中任何假名痕迹 = OCR 误判信号（材料级裁定，见文件头注释）
                flags.append("kana_trace")
        p["flags"] = flags
        del p["_lens"]                           # 内部中间量不出 schema
    n_flagged = sum(1 for p in qa_pages if p["flags"])

    # ---- 常量候选（exact + ngram 双源，ratio 降序；tie: pages_hit 降、文本升）----
    cands: List[Dict[str, Any]] = []
    for t, stems in exact_hits.items():          # ① 整行 exact-match
        cands.append({"text": t, "pages_hit": len(stems),
                      "ratio": round(len(stems) / n_pages, 4), "source": "exact"})
    if n_pages:
        df: Dict[str, int] = {}                  # ② n-gram 页级文档频率
        for st in page_norms:
            for g in page_grams[st]:
                df[g] = df.get(g, 0) + 1
        high = {g for g, c in df.items() if c / n_pages >= NGRAM_DF_RATIO}
        # ≥0.9 的 gram **逐行**合并为极大串（同一行的合并结果必是该行子串，
        # 不产生跨行假串）；合并出的串再跨页计 df。
        max_hit: Dict[str, set] = {}
        for st, norms in page_norms.items():
            per: Set[str] = set()
            for t in norms:
                ln = len(t)
                hi = {t[i:i + n]
                      for n in range(NGRAM_MIN, min(NGRAM_MAX, ln) + 1)
                      for i in range(ln - n + 1)
                      if t[i:i + n] in high}
                if hi:
                    per.update(_merge_maximal(hi))
            for m in per:
                max_hit.setdefault(m, set()).add(st)
        for m, stems in max_hit.items():
            cands.append({"text": m, "pages_hit": len(stems),
                          "ratio": round(len(stems) / n_pages, 4), "source": "ngram"})
    cands.sort(key=lambda c: (-c["ratio"], -c["pages_hit"], c["text"]))

    total_lines = sum(shape_counts.values())
    shape_stats = {k: (round(shape_counts[k] / total_lines, 4) if total_lines else 0.0)
                   for k in SHAPES}

    col_counts: Dict[str, Any] = {}
    if col_vals:
        col_counts = {"median": round(float(statistics.median(col_vals)), 1),
                      "min": min(col_vals), "max": max(col_vals)}

    return {
        "_meta": {
            "说明": "材料画像：learn 前的全语料无标注通读（确定性统计，零 LLM、零属性名先验）",
            "生成时间": datetime.now().isoformat(timespec="seconds"),
            "source": str(d),
            "profile_id": profile_id,        # 操作者声明，不推断（设计稿 §二）
            "n_pages": n_pages,
            # 归一化判据落盘（可解释可复核，不藏在代码里）
            "fold_table": {k: S2T_FOLD[k] for k in sorted(S2T_FOLD)},
            "fold_table_source": "与 rule_split.S2T_FOLD 同源（单一实现）",
            "excluded_nonpage": excluded,    # 系统产物等非页文件（判据见模块 doc）
        },
        "qa": {"pages": qa_pages, "n_flagged": n_flagged},
        "constants_candidates": cands,
        "diagnostics": {                     # 画像发现过程可见（补做指令③）
            "n_lines_total": n_lines_total,
            "n_candidates": len(cands),
            "top20": cands[:20],             # 含 ratio，无论是否过线
        },
        "shape_stats": shape_stats,
        "line_len": {"median": round(len_median, 2), "p90": round(len_p90, 2)},
        "col_counts": col_counts,
        # TODO(画像v2)：OCR 错字率代理 —— 与金标准同名页复用 align_eval 逐行对齐
        # 算字符错误率；无金标准的页该字段缺席（设计稿 §二，本期不做）。
    }


# ============================================================
# 落盘（与 rule_learn.learn / learn_and_save 同款分工）
# ============================================================
def corpus_path(profile_id: str, rules_dir=None) -> Path:
    """画像路径：`rules_data/corpus_<pid>.json`（与 learned_/overrides_ 同目录同族）。"""
    import rule_learn as RL
    return Path(rules_dir or RL.RULES_DATA_DIR) / f"corpus_{profile_id}.json"


def save_corpus(report: Dict[str, Any], rules_dir=None) -> Path:
    """画像原子落盘（data_io 单一实现，崩溃不留半截）。"""
    pid = str((report.get("_meta") or {}).get("profile_id") or "")
    p = corpus_path(pid, rules_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data_io.atomic_write_json(p, report)
    return p
