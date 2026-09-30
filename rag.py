"""P-K7 阶段 2/4：RAG 检索模块（v5.1 / 阶段 4 混合检索版）

设计要点：
- **RAG 检索源**：data/structured/<image>.json 的 L2_lines 拼接全文（**绕过 P-K1 切片 bug**）
- 零外部依赖（仅 stdlib + numpy）：BM25 / TF-IDF 纯 Python 实现，jieba 用可降级到字符 n-gram
- 引用脚注：白名单正则防 XSS
- 支持 scope：current_project / all_projects / selected_images
- L1→L2 按需扩展（命中 section 后可拉具体行）
- 阶段 4：BM25 + TF-IDF cosine 混合检索（hybrid_search）
- 阶段 4：corpus 缓存（避免每次 query 都重新 tokenize 全部文档）

公开 API（被 app.py 路由调用）：
- load_image_fulltext(image_stem) -> str
- load_all_image_fulltexts(scope: str, project_id: str = "") -> dict[image_stem, str]
- bm25_search(query: str, corpus: dict, top_k: int = 5) -> list[dict]
- tfidf_search(query: str, corpus: dict, top_k: int = 5) -> list[dict]
- hybrid_search(query: str, corpus: dict, top_k: int = 5, alpha: float = 0.5) -> list[dict]
- build_rag_prompt(query: str, retrieved: list[dict], history: list[dict] = None) -> list[dict]
- expand_l2_on_demand(card_id: str, image_stem: str, top_n: int = 3) -> list[dict]
- get_or_build_corpus_cache(scope, ...) -> dict  # 阶段 4 缓存加速
"""
from __future__ import annotations

import json
import re
import sys
import time
import difflib
import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Optional

import zhconv  # 繁简归一（已在 venv 装好）

import corrected_text  # ✅ P-K9 阶段 0b：校正感知读取（口径同 unit_builder）

# 日志走统一 logger，禁止 print
log = logging.getLogger("local_chronicles_ocr")

# ✅ PyInstaller 冻结模式兼容
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()
else:
    _BASE = Path(__file__).parent.resolve()

# 顶层常量
DEFAULT_DATA_DIR = _BASE / "data" / "structured"
DEFAULT_KNOWLEDGE_DIR = _BASE / "knowledge_cards"

# ✅ M23 清理：原 CARD_ID_PATTERN + extract_card_ids_from_text()（Q-R14 从 LLM 文本
# 反解析引用）零生产调用——现行引用机制为检索侧构造 references + M16 空 quotes 闸，
# 不再从输出文本提取。删除连带其专属测试（test_p_knowledge_chat_rag Test10/11/14 等）。

# === 阶段 4：corpus 缓存（备份纪律之外的运行时缓存）===
# 结构：{cache_key: {"stems": [...], "tokenized": [[toks]], "tfidf": [dict], "norms": [float], "built_at": float, "corpus_hash": str}}
_CORPUS_CACHE: dict = {}
_CORPUS_CACHE_MAXSIZE = 8  # 最多缓存 8 个 scope 的 corpus（LRU 简化版）
_CORPUS_CACHE_TTL = 300.0   # 5 分钟内复用（避免长时间过期数据）

# === 模块级 RLock 保护（项目编码规范：业务异常走自定义类，但模块级锁 stdlib 即可）===
_lock = threading.RLock()


# ============================================
# 1. load_image_fulltext — 加载 L2_lines 拼接全文
# ============================================
def _default_corrected_map(data_dir: Path) -> dict:
    """structured 目录 → 主工作簿"真校对"修正表（缺失/层级不符 → 空表）。

    ✅ P-K9 阶段 0b（2026-09-10）：校正感知读取的口径入口在 corrected_text，
    此处只做"从 structured 目录反推主工作簿"。临时目录（测试注入）不含
    output.xlsx → 空表 → 行为与改造前完全一致。
    """
    return corrected_text.map_for_structured_dir(data_dir)


def load_image_fulltext(
    image_stem: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    corrected_map: Optional[dict] = None,
) -> str:
    """加载单张图的 L2_lines 拼接全文（按 col_index, row_index 排序）。

    ✅ P-K9 阶段 0b（2026-09-10）：**校正感知**。此前这里直读 structured 原文，
    即最初 OCR 输出（手写材料首过正确率有限），等于把未校正结果当权威语料喂给
    问答。现逐行套用人工校对（口径同 unit_builder：真校对 + safe_corrected
    粒度防护），未校对的图行为不变。

    Args:
        image_stem: 图片 stem（无扩展名），如 "clipboard_样例"
        data_dir: data/structured 目录
        corrected_map: 注入的修正表（测试用）。None → 按 data_dir 反推主工作簿。

    Returns:
        拼接后的完整文本（繁简归一 + 空白归一）
        文件不存在则返 ""
    """
    json_path = data_dir / f"{image_stem}.json"
    if not json_path.exists():
        return ""
    with open(json_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    l2 = d.get("L2_lines", [])
    if not isinstance(l2, list):
        return ""
    cmap = _default_corrected_map(data_dir) if corrected_map is None else corrected_map
    items = []
    for line in l2:
        if isinstance(line, dict):
            col = line.get("col_index", 0)
            row = line.get("row_index", 0)
            text, _flag = corrected_text.apply_correction(line, cmap)
            items.append((col, row, text))
    items.sort(key=lambda x: (x[0], x[1]))
    return _normalize_simplify("".join(t for _, _, t in items))


# ============================================
# 2. load_all_image_fulltexts — 按 scope 加载
# ============================================
def load_all_image_fulltexts(
    scope: str = "current_project",
    project_id: str = "",
    project_name: str = "",
    selected_images: Optional[list[str]] = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR,
) -> dict[str, str]:
    """按 scope 加载一组 image 全文。

    Args:
        scope: "current_project" / "all_projects" / "selected_images"
        project_id: 当前项目 id（如"<栏目id>"）— current_project 时必填
        project_name: 当前项目名（v5.1：作为 knowledge_cards 子目录名）
        selected_images: 选中的 image_stem 列表（selected_images 时必填）
        data_dir / knowledge_dir: 数据源

    Returns:
        {image_stem: 全文} 字典
    """
    with _lock:
        stems = _resolve_scope_stems(scope, project_id, project_name,
                                     selected_images, data_dir, knowledge_dir)

        out: dict[str, str] = {}
        for stem in stems:
            txt = load_image_fulltext(stem, data_dir)
            if txt:
                out[stem] = txt
        return out


def _data_root(data_dir: Path) -> Path:
    """栏目归属与元数据所在的数据根目录。

    rag 的 data_dir 约定是 ``data/structured/``（见 DEFAULT_DATA_DIR），而
    ``project_assignments.json`` / ``projects.json`` 位于其上一级；
    若调用方传入的已是数据根目录，则原样返回。
    """
    d = Path(data_dir)
    return d.parent if d.name == "structured" else d


def _project_id_by_name(project_name: str, data_dir: Path) -> str:
    """按栏目名反查项目 id（data/projects.json）。找不到返回空串。"""
    if not project_name:
        return ""
    try:
        items = json.loads((_data_root(data_dir) / "projects.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(items, list):
        return ""
    for p in items:
        if isinstance(p, dict) and p.get("name") == project_name:
            return str(p.get("id") or "")
    return ""


def _stems_for_project(project_id: str, project_name: str, data_dir: Path) -> list[str]:
    """取某栏目的图片 stem 列表。

    ✅ 2026-09-10 修复：权威来源改为 ``data/project_assignments.json``
    （图片名 → 栏目 id 映射），实时反映栏目归属、不依赖任何导出动作。

    原先走 ``knowledge_cards/<栏目>/_index.json`` 的 ``sections`` 字段，但该索引
    实际只写 ``card_count`` 汇总（没有 sections 数组），必然解析出 0 张 →
    调用方回落全库扫描，栏目级检索从未真正生效。改为 assignments 后同时解决
    "必须先人工导出知识卡片，问答才检索得到"的体验断点。
    """
    pid = project_id or _project_id_by_name(project_name, data_dir)
    if not pid:
        return []
    try:
        assign = json.loads(
            (_data_root(data_dir) / "project_assignments.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(assign, dict):
        return []
    return sorted(Path(k).stem for k, v in assign.items() if v == pid)


def _stems_from_knowledge_index(dir_name: str, knowledge_dir: Path) -> list[str]:
    """（兼容路径）从历史知识卡片索引的 sections 数组提取 image stem。"""
    if not dir_name:
        return []
    kc_index = knowledge_dir / dir_name / "_index.json"
    if not kc_index.exists():
        return []
    try:
        with open(kc_index, "r", encoding="utf-8") as f:
            idx = json.load(f)
        stems: list[str] = []
        for entry in idx.get("sections", []) or []:
            cid = entry.get("card_id", "")
            m = re.match(r"section_(.+?)_col_\d+$", cid)
            if m:
                stems.append(m.group(1))
        return list(dict.fromkeys(stems))
    except (json.JSONDecodeError, OSError):
        return []


def _resolve_scope_stems(
    scope: str,
    project_id: str = "",
    project_name: str = "",
    selected_images: Optional[list[str]] = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR,
) -> list[str]:
    """按 scope 解析出 image_stem 列表（v2 M3 从 load_all_image_fulltexts 抽出复用）。"""
    stems: list[str] = []
    if scope == "current_project":
        # ① 权威来源：栏目归属表（实时，无需任何导出）
        stems = _stems_for_project(project_id, project_name, data_dir)
        # ② 兼容历史：知识卡片索引的 sections 数组
        if not stems:
            stems = _stems_from_knowledge_index(project_name or project_id, knowledge_dir)
        # ③ 兜底：全库结构化数据
        if not stems:
            stems = _all_stems_in_data(data_dir)
    elif scope == "all_projects":
        stems = _all_stems_in_data(data_dir)
    elif scope == "selected_images":
        stems = list(selected_images or [])
    else:
        raise ValueError(f"未知 scope: {scope!r}")
    return stems


def _all_stems_in_data(data_dir: Path) -> list[str]:
    """扫 data/structured/ 下所有 json 文件名作为 stem 列表"""
    if not data_dir.exists():
        return []
    return [p.stem for p in data_dir.glob("*.json") if p.stem != "repair_report"]


# ============================================
# 3. 工具函数
# ============================================
def _normalize_simplify(s: str) -> str:
    """空白符 + 中文标点归一 + 繁体→简体

    阶段 4-B 修复：去中英文常见标点（避免 fuzzy_match 时标点差异导致不匹配）
    阶段 4-B 二次修复（2026-08-20）：只去中文标点 + 英文标点；保留 . - _ 等（避免破坏"1.5B"等）
    """
    if s is None:
        return ""
    s = re.sub(r"\s+", "", s)
    # 去标点：中文标点（避免破坏"1.5B"等数字字母组合中的 . - _）
    s = re.sub(r"[，。！？、；：·\u3000\"'""''《》（）()【】\[\]\\\/!?;]", "", s)  # noqa
    return zhconv.convert(s, "zh-cn")


def _tokenize(s: str) -> list[str]:
    """简化的中文分词：
    - 2-gram 滑窗
    - 单字（CJK 1 字也算 token）
    - 字母+数字组合 token（如 "DeepSeekV4" / "1.5B" 完整保留）
    - 繁简归一
    """
    s_norm = _normalize_simplify(s)
    toks: list[str] = []
    for i in range(len(s_norm) - 1):
        toks.append(s_norm[i:i + 2])
    for c in s_norm:
        if "\u4e00" <= c <= "\u9fff":
            toks.append(c)
    # 字母+数字组合（含可选小数点）：匹配 "DeepSeekV4" / "1.5B" / "abc" / "123"
    for m in re.finditer(r"[a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)*", s_norm):
        toks.append(m.group(0))
    return toks


# ============================================
# 4. bm25_search — 纯 Python BM25 实现（零依赖）
# ============================================
class _BM25:
    """经典 BM25（Okapi BM25）实现。

    Ref: https://en.wikipedia.org/wiki/Okapi_BM25
    """

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = corpus
        self.doc_lens = [len(d) for d in corpus]
        self.avgdl = sum(self.doc_lens) / max(1, len(corpus))
        self.df: dict[str, int] = Counter()  # 词项文档频
        self.tf: list[Counter] = []  # 每篇文档的词频
        for doc in corpus:
            tf = Counter(doc)
            self.tf.append(tf)
            for term in set(doc):
                self.df[term] += 1
        self.N = len(corpus)

    def score(self, query_tokens: list[str], doc_idx: int) -> float:
        tf = self.tf[doc_idx]
        dl = self.doc_lens[doc_idx]
        s = 0.0
        for term in query_tokens:
            if term not in tf:
                continue
            f = tf[term]
            n = self.df[term]
            # IDF
            idf = max(0.0, ((self.N - n + 0.5) / (n + 0.5)) * (1 if self.N > 0 else 0))
            # BM25 公式
            denom = f + self.k1 * (1 - self.b + self.b * dl / max(1, self.avgdl))
            s += idf * f * (self.k1 + 1) / max(1e-9, denom)
        return s


def bm25_search(
    query: str,
    corpus: dict[str, str],
    top_k: int = 5,
) -> list[dict]:
    """BM25 检索。

    Args:
        query: 自然语言查询
        corpus: {image_stem: 全文}
        top_k: 返回前 K 个结果

    Returns:
        [{"image_stem": ..., "score": ..., "preview": ...}, ...] 按 score 降序
    """
    if not corpus:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    # 构建 BM25 语料
    docs_tokens: list[list[str]] = [_tokenize(text) for text in corpus.values()]
    stems = list(corpus.keys())
    bm = _BM25(docs_tokens)
    scores: list[tuple[int, float]] = []
    for i in range(len(stems)):
        s = bm.score(query_tokens, i)
        if s > 0:
            scores.append((i, s))
    # 降序
    scores.sort(key=lambda x: -x[1])
    out: list[dict] = []
    for i, s in scores[:top_k]:
        stem = stems[i]
        full_text = corpus[stem]
        preview = full_text[:120] + ("..." if len(full_text) > 120 else "")
        out.append({
            "image_stem": stem,
            "score": round(s, 4),
            "preview": preview,
            "full_text": full_text,  # 阶段 2 暂返回全文；阶段 4 优化时只返 top_n 行
        })
    return out


# ============================================
# 5. build_rag_prompt — 构造 RAG prompt
# ============================================
def build_rag_prompt(
    query: str,
    retrieved: list[dict],
    history: Optional[list[dict]] = None,
    system_prompt: Optional[str] = None,
) -> list[dict]:
    """构造 RAG prompt（OpenAI 风格 messages 列表）。

    Args:
        query: 用户当前问题
        retrieved: bm25_search() 返回的检索结果
        history: 多轮对话历史（默认 None）
        system_prompt: 自定义系统提示（默认中文档案助手）

    Returns:
        messages: [{"role": "system"|"user"|"assistant", "content": ...}, ...]
    """
    if system_prompt is None:
        system_prompt = (
            "你是档案资料助手，基于以下检索到的知识卡片内容回答用户问题。\n"
            "如果检索内容与问题无关，请回答「未找到相关信息」，**不要编造内容**。\n"
            "回答末尾标注引用：参考 [<card_id>]，card_id 形如 section_<image_stem>_col_<col_index>。\n"
            "引用示例：参考 [section_clipboard_样例_col_0]\n"
            "检索内容来自 OCR，未经人工校对的字句可能有识别错误；"
            "涉及精确字句（人名、机构名、年份、金额数字）时，请提示用户核对原图。"
        )

    # 拼检索内容摘要
    if retrieved:
        ref_lines: list[str] = []
        for i, item in enumerate(retrieved, 1):
            stem = item.get("image_stem", "")
            preview = item.get("preview", "")
            score = item.get("score", 0)
            # 阶段 4：如果有 bm25_score/tfidf_score 则显示明细
            bm = item.get("bm25_score")
            tf = item.get("tfidf_score")
            score_label = (
                f"(BM25={bm}, TF-IDF={tf}, hybrid={score})"
                if bm is not None and tf is not None
                else f"(BM25 score={score})"
            )
            # ✅ v2 M3：unit 检索结果自带 card_id（unit_<stem>_<seq>）；
            # doc 兜底/旧语料回退合成 section_<stem>_col_0
            card_id = item.get("card_id") or f"section_{stem}_col_0"
            ref_lines.append(
                f"[{i}] 来源图片: {stem}  {score_label}\n"
                f"    参考 [{card_id}]\n"
                f"    内容摘要: {preview}"
            )
        refs_text = "\n\n".join(ref_lines)
        user_content = f"问题: {query}\n\n参考材料:\n{refs_text}"
    else:
        user_content = f"问题: {query}\n\n（未找到相关参考材料）"

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    # 追加历史
    # P-K7 阶段 5：删 history[-10:] 硬截断，让调用方（app.py /api/chat）控制滚动窗口
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_content})
    return messages


# ============================================
# 6. expand_l2_on_demand — L1 命中后拉 L2 行（v5.1 强化）
# ============================================
def expand_l2_on_demand(
    card_id: str,
    image_stem: str,
    top_n: int = 3,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> list[dict]:
    """命中 L1 section 后，按需拉取该 image 的 L2 行（最多 top_n 条）。

    Args:
        card_id: 形如 "section_<stem>_col_<n>"
        image_stem: 用于定位 data/structured/<stem>.json
        top_n: 返回行数

    Returns:
        [{"line_id": int, "text": str, "xlsx_id": int, "col_index": int, "row_index": int}, ...]
    """
    json_path = data_dir / f"{image_stem}.json"
    if not json_path.exists():
        return []
    with open(json_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    l2 = d.get("L2_lines", [])
    if not isinstance(l2, list):
        return []
    # card_id 中的 col_index 提取
    m = re.match(r"section_.+?_col_(\d+)$", card_id)
    if m:
        col_filter = int(m.group(1))
        filtered = [ln for ln in l2 if isinstance(ln, dict) and ln.get("col_index") == col_filter]
    else:
        filtered = list(l2)
    # 按 row_index 排序
    filtered.sort(key=lambda ln: (ln.get("col_index", 0), ln.get("row_index", 0)) if isinstance(ln, dict) else (0, 0))
    out: list[dict] = []
    for ln in filtered[:top_n]:
        if isinstance(ln, dict):
            out.append({
                "line_id": ln.get("line_id", 0),
                "xlsx_id": ln.get("xlsx_id", 0),
                "col_index": ln.get("col_index", 0),
                "row_index": ln.get("row_index", 0),
                "text": ln.get("text", ""),
            })
    return out


# ============================================
# 7. 阶段 4：TF-IDF cosine 检索（numpy 加速）
# ============================================
def _build_tfidf_vectors(
    docs_tokens: list[list[str]],
) -> tuple[list[dict[str, float]], list[float], dict[str, float]]:
    """构建 TF-IDF 向量（稀疏 dict 表示）+ L2 范数 + IDF 表。

    公式：
        tf(t, d) = count(t, d) / sum(count(*, d))    # 归一化
        idf(t)   = log((N + 1) / (df(t) + 1)) + 1    # sklearn 风格平滑
        tfidf(t, d) = tf(t, d) * idf(t)

    Returns:
        vectors: 每篇文档的 {token: tfidf} 稀疏字典
        norms:   每篇文档的 L2 范数
        idf:     {token: idf} 全局表（供 query 用）
    """
    if not docs_tokens:
        return [], [], {}
    N = len(docs_tokens)
    # 1) tf
    tf_list: list[Counter] = []
    df: Counter = Counter()
    for toks in docs_tokens:
        c = Counter(toks)
        tf_list.append(c)
        for term in c:
            df[term] += 1
    # 2) idf (sklearn smooth: log((N+1)/(df+1)) + 1)
    idf = {term: (1.0 + __import__("math").log((N + 1) / (cnt + 1))) for term, cnt in df.items()}
    # 3) tfidf + norm
    vectors: list[dict[str, float]] = []
    norms: list[float] = []
    for c in tf_list:
        total = sum(c.values()) or 1
        vec: dict[str, float] = {}
        s = 0.0
        for term, cnt in c.items():
            v = (cnt / total) * idf.get(term, 0.0)
            vec[term] = v
            s += v * v
        vectors.append(vec)
        norms.append(__import__("math").sqrt(s))
    return vectors, norms, idf


def _tfidf_cosine(
    query_tokens: list[str],
    vectors: list[dict[str, float]],
    norms: list[float],
    idf: dict[str, float],
) -> list[float]:
    """计算 query 与每个 doc 的 cosine 相似度。

    Returns:
        每个 doc 的 cosine 分数（[0, 1] 之间）
    """
    # query 向量
    qc = Counter(query_tokens)
    q_total = sum(qc.values()) or 1
    q_vec: dict[str, float] = {}
    for term, cnt in qc.items():
        v = (cnt / q_total) * idf.get(term, 0.0)
        if v:
            q_vec[term] = v
    q_norm_sq = sum(v * v for v in q_vec.values())
    q_norm = __import__("math").sqrt(q_norm_sq)
    if q_norm == 0:
        return [0.0] * len(vectors)
    out: list[float] = []
    for vec, d_norm in zip(vectors, norms):
        if d_norm == 0:
            out.append(0.0)
            continue
        # dot product (only over shared terms)
        if len(q_vec) <= len(vec):
            dot = sum(q_vec[t] * vec.get(t, 0.0) for t in q_vec)
        else:
            dot = sum(t_v * vec.get(t, 0.0) for t, t_v in vec.items() if t in q_vec)
        out.append(dot / (q_norm * d_norm))
    return out


def tfidf_search(
    query: str,
    corpus: dict[str, str],
    top_k: int = 5,
) -> list[dict]:
    """TF-IDF cosine 检索。

    Args:
        query: 自然语言查询
        corpus: {image_stem: 全文}
        top_k: 返回前 K 个结果

    Returns:
        [{"image_stem": ..., "score": ..., "preview": ...}, ...] 按 score 降序
    """
    if not corpus:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    docs_tokens: list[list[str]] = [_tokenize(text) for text in corpus.values()]
    stems = list(corpus.keys())
    vectors, norms, idf = _build_tfidf_vectors(docs_tokens)
    scores = _tfidf_cosine(query_tokens, vectors, norms, idf)
    pairs = [(i, s) for i, s in enumerate(scores) if s > 0]
    pairs.sort(key=lambda x: -x[1])
    out: list[dict] = []
    for i, s in pairs[:top_k]:
        stem = stems[i]
        full_text = corpus[stem]
        preview = full_text[:120] + ("..." if len(full_text) > 120 else "")
        out.append({
            "image_stem": stem,
            "score": round(s, 4),
            "preview": preview,
            "full_text": full_text,
        })
    return out


# ============================================
# 9. 阶段 4：混合检索（BM25 + TF-IDF cosine）
# ============================================
def _minmax_normalize(scores: list[float]) -> list[float]:
    """min-max 归一化到 [0, 1]；全 0 时返全 0。"""
    if not scores:
        return []
    mn, mx = min(scores), max(scores)
    if mx == mn:
        return [0.0] * len(scores)
    return [(s - mn) / (mx - mn) for s in scores]


def hybrid_search(
    query: str,
    corpus: dict[str, str],
    top_k: int = 5,
    alpha: float = 0.5,
    candidate_multiplier: int = 3,
) -> list[dict]:
    """BM25 + TF-IDF cosine 混合检索（线性加权）。

    设计：
        - BM25 擅长精确词频 + 长度归一（exact_match 类问题）
        - TF-IDF cosine 擅长文档级区分（semantic 类问题）
        - 混合 score = α * bm25_norm + (1 - α) * tfidf_cosine
        - 候选：各取 top_k * candidate_multiplier，合并去重
        - 最终：取 top_k

    Args:
        query: 自然语言查询
        corpus: {image_stem: 全文}
        top_k: 返回前 K 个结果
        alpha: BM25 权重（0~1；默认 0.5 = 等权）
        candidate_multiplier: 候选倍率（保证混合后仍有 ≥top_k 候选）

    Returns:
        [{"image_stem": ..., "score": ..., "preview": ..., "bm25_score": ..., "tfidf_score": ..., "hybrid_score": ...}, ...]
        按 hybrid_score 降序
    """
    if not corpus:
        return []
    if not (0.0 <= alpha <= 1.0):
        raise ValueError(f"alpha 必须在 [0, 1]；当前 {alpha!r}")

    cand_k = max(top_k, top_k * candidate_multiplier)
    bm25_res = bm25_search(query, corpus, top_k=cand_k)
    tfidf_res = tfidf_search(query, corpus, top_k=cand_k)

    # 用 image_stem 作 key 合并
    bm25_map: dict[str, float] = {r["image_stem"]: r["score"] for r in bm25_res}
    tfidf_map: dict[str, float] = {r["image_stem"]: r["score"] for r in tfidf_res}
    all_stems = list(dict.fromkeys(list(bm25_map.keys()) + list(tfidf_map.keys())))
    if not all_stems:
        return []

    # 归一化 BM25（min-max → [0, 1]）
    bm25_raw = [bm25_map.get(s, 0.0) for s in all_stems]
    bm25_norm = _minmax_normalize(bm25_raw)
    # TF-IDF cosine 本身就是 [0, 1]，直接用
    tfidf_raw = [tfidf_map.get(s, 0.0) for s in all_stems]

    # 混合
    hybrid_scores = [alpha * b + (1.0 - alpha) * t for b, t in zip(bm25_norm, tfidf_raw)]

    # 按 hybrid 排序
    indexed = sorted(enumerate(all_stems), key=lambda x: -hybrid_scores[x[0]])
    out: list[dict] = []
    for idx, stem in indexed[:top_k]:
        hs = hybrid_scores[idx]
        if hs <= 0:
            continue
        full_text = corpus[stem]
        preview = full_text[:120] + ("..." if len(full_text) > 120 else "")
        out.append({
            "image_stem": stem,
            "score": round(hs, 4),                       # 混合分（前端用）
            "bm25_score": round(bm25_map.get(stem, 0.0), 4),
            "tfidf_score": round(tfidf_map.get(stem, 0.0), 4),
            "preview": preview,
            "full_text": full_text,
        })
    return out


# ============================================
# 10. 阶段 4：corpus 缓存（避免每次 query 都重新 tokenize）
# ============================================
def _make_cache_key(
    scope: str,
    project_id: str,
    project_name: str,
    selected_images: Optional[list[str]],
) -> str:
    """生成 corpus 缓存 key。"""
    sel = sorted(selected_images) if selected_images else []
    return f"{scope}|{project_id}|{project_name}|{'|'.join(sel)}"


def get_cached_corpus_artifacts(
    scope: str,
    project_id: str = "",
    project_name: str = "",
    selected_images: Optional[list[str]] = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR,
) -> tuple[list[str], dict[str, str], list[list[str]], list[dict], list[float], dict]:
    """加载（缓存命中则直接用）corpus 及其 tokenize / tfidf artifacts。

    Returns:
        stems:        list[str]   — image_stem 列表
        corpus:       dict        — {image_stem: 全文}
        docs_tokens:  list[list]  — 每篇文档的 token 列表
        vectors:      list[dict]  — TF-IDF 稀疏向量
        norms:        list[float] — L2 范数
        idf:          dict        — 全局 IDF 表
    """
    with _lock:
        cache_key = _make_cache_key(scope, project_id, project_name, selected_images)
        now = time.time()
        cached = _CORPUS_CACHE.get(cache_key)
        if cached and (now - cached.get("built_at", 0)) < _CORPUS_CACHE_TTL:
            return (
                cached["stems"],
                cached["corpus"],
                cached["docs_tokens"],
                cached["tfidf_vectors"],
                cached["norms"],
                cached["idf"],
            )
        # 重建
        corpus = load_all_image_fulltexts(
            scope=scope,
            project_id=project_id,
            project_name=project_name,
            selected_images=selected_images,
            data_dir=data_dir,
            knowledge_dir=knowledge_dir,
        )
        stems = list(corpus.keys())
        docs_tokens: list[list[str]] = [_tokenize(text) for text in corpus.values()]
        vectors, norms, idf = _build_tfidf_vectors(docs_tokens)
        # LRU 简化：超 maxsize 删最旧
        if len(_CORPUS_CACHE) >= _CORPUS_CACHE_MAXSIZE:
            oldest_key = min(_CORPUS_CACHE.keys(), key=lambda k: _CORPUS_CACHE[k].get("built_at", 0))
            _CORPUS_CACHE.pop(oldest_key, None)
        _CORPUS_CACHE[cache_key] = {
            "stems": stems,
            "corpus": corpus,
            "docs_tokens": docs_tokens,
            "tfidf_vectors": vectors,
            "norms": norms,
            "idf": idf,
            "built_at": now,
        }
        return stems, corpus, docs_tokens, vectors, norms, idf


def clear_corpus_cache() -> int:
    """清空 corpus 缓存（用于测试或文件大规模变动时手动失效）。"""
    with _lock:
        n = len(_CORPUS_CACHE)
        _CORPUS_CACHE.clear()
        return n


# ============================================
# 10.5 v2 M3（2026-08-31 知识库重构）：unit 语料 + 字段倒排索引 + 三路检索 + 硬拒答
# ============================================
DEFAULT_UNITS_DIR = _BASE / "data" / "units"

# 硬拒答阈值：TF-IDF cosine 是绝对分（[0,1]，不受 min-max 归一影响）。
# 低于它 = 语料中根本没有与查询共享的有效词项 → 拒答，不再喂 LLM 自由发挥
# （直接修负样本"未找到"问题；阈值以 eval 负样本 top 分数为基准标定）
HARD_REFUSAL_TFIDF_THRESHOLD = 0.06

# 字段命中 boost：查询文本包含某 unit 的字段值（归一化后）→ 该 unit 加分。
# 直接修 table_lookup（户名/金额/科年精确查找）问题
FIELD_BOOST_WEIGHT = 0.35
FIELD_BOOST_CAP = 3          # 最多计入 3 个字段命中
FIELD_MIN_VALUE_LEN = 2      # 字段值最短长度（1 字值 boost 无区分度）

# 规则版查询扩展（文白/口语同义，只追加检索词，不改写原问题、不传给 LLM）。
# 阶段 4（M6）会加 LLM 改写；这里是它的降级路径
_QUERY_SYNONYMS: dict[str, list[str]] = {
    "进士": ["登第", "科年", "举人"],
    "举人": ["登第", "科年"],
    "登第": ["进士", "举人"],
    "多少钱": ["金额", "銀", "兩"],
    "金额": ["銀", "兩"],
    "投资": ["投资课", "股份", "入股"],
    "公司": ["局", "厂", "堂"],
    "户名": ["堂", "记"],
    "籍贯": ["县", "人"],
}


def _fields_to_text(fields: dict) -> str:
    """把 unit.fields 拼成可检索文本（字段名一并入 token，利于"户名/金额"类查询）。"""
    parts: list[str] = []
    for k, v in (fields or {}).items():
        if v is None or v == "":
            continue
        parts.append(f"{k}：{v}")
    return "；".join(parts)


def load_unit_corpus_for_stems(
    stems: list[str],
    units_dir: Path = DEFAULT_UNITS_DIR,
) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """unit 级语料（路①）：每 verified 语义单元一篇文档。

    Args:
        stems: image_stem 列表（scope 解析结果）
        units_dir: data/units 目录

    Returns:
        corpus: {card_id: text}，card_id = ``unit_<stem>_<seq:04d>``；
            文本 = unit 正文 + 字段序列化。
            某 stem 无 units payload 时 fallback 整图全文（card_id = image_stem，
            下游合成 section_<stem>_col_0，与旧行为一致）。
        field_index: {card_id: [(field_key, raw_value), ...]}（路②字段倒排索引原料）
    """
    corpus: dict[str, str] = {}
    field_index: dict[str, list[tuple[str, str]]] = {}
    units_dir = Path(units_dir)
    for stem in stems:
        payload = None
        path = units_dir / f"{stem}.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"[rag] units payload 读失败 {stem}: {e}")
        if not payload or not isinstance(payload.get("units"), list):
            # 路①兜底：unit_builder 未跑的图退回整图全文
            # ✅ P-K9 阶段 0b 修复：原实现忽略 units_dir 注入、恒读默认 structured
            #    目录（测试注入目录时读到真实项目数据）；现按 units_dir 同根推导。
            struct_dir = units_dir.parent / "structured"
            txt = load_image_fulltext(stem, struct_dir)
            if txt:
                corpus[stem] = txt
            continue
        for u in payload["units"]:
            if not isinstance(u, dict):
                continue
            # 只收 verified 单元（needs_review 文本不进 RAG，防未验证内容污染答案）
            if (u.get("verify", {}) or {}).get("status") != "verified":
                continue
            uid = u.get("unit_id", "")
            text = (u.get("text", "") or "").strip()
            if not uid or not text:
                continue
            seq = uid.rsplit("_", 1)[-1]
            card_id = f"unit_{stem}_{seq}"
            fields = u.get("fields", {}) or {}
            ftext = _fields_to_text(fields)
            corpus[card_id] = _normalize_simplify(f"{text}\n{ftext}" if ftext else text)
            fvs = [(str(k), str(v)) for k, v in fields.items()
                   if v is not None and str(v).strip()]
            if fvs:
                field_index[card_id] = fvs
    return corpus, field_index


# ============================================
# 7-C. 人工标注金标准语料（P-K9 阶段 0d，2026-09-10）
# ============================================
# 背景（专项审计发现）：manual_annotations/*.jsonl 是标注页逐字段的人工金标准，
# 此前**只服务** gold_eval / few-shot / 训练导出，问答侧一个字读不到——问答只能
# 吃未校正的 OCR 原文。用户最高质量的人工投入被浪费在问答之外，且同一批材料
# 存在"两套事实来源"（校对台进 RAG、标注页不进）。本段把金标准接进检索语料。

MANUAL_ANNOTATIONS_DIRNAME = "manual_annotations"

# 金标准卡片 id 前缀（与 unit_ 并列，供下游识别来源与优先级）
GOLD_CARD_PREFIX = "gold_"


def manual_annotations_dir(units_dir: Path = DEFAULT_UNITS_DIR) -> Path:
    """由 units_dir（``data/units``）反推项目根下的 ``manual_annotations``。

    测试注入临时目录时，反推出的路径通常不存在 → 返回空语料，行为与改造前一致
    （绝不落到真实项目数据，沿用本文件的目录注入隔离纪律）。
    """
    return Path(units_dir).parent.parent / MANUAL_ANNOTATIONS_DIRNAME


def _stem_of_manual_row(row: dict, fallback: str) -> str:
    """标注行的 image_stem：优先用行内 image_name（权威），否则用文件名回退。"""
    name = str(row.get("image_name") or "").strip()
    if name:
        return Path(name).stem
    return fallback


def load_manual_annotation_corpus_for_stems(
    stems: list[str],
    units_dir: Path = DEFAULT_UNITS_DIR,
) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """人工标注（标注页金标准）→ 语料卡。

    粒度：**每张图一张卡**（card_id = ``gold_<stem>_0001``）。标注行是逐字段
    片段（attr + text + box），不成篇；按图聚合后既能被 BM25/TF-IDF 正常竞争，
    又不至于把同一条记录拆碎。文本保留**文件追加顺序**（= 用户标注顺序，近似
    其阅读顺序），逐行 ``属性：文本``。

    繁简归一与 unit 语料一致（``_normalize_simplify``）：查询侧是简体，语料不
    归一会导致繁体标注命中率下降；引文校验也拿同一份归一后文本比对，无额外风险。

    Args:
        stems: scope 解析出的 image_stem 列表（只收本范围内的图）
        units_dir: data/units（用于反推 manual_annotations 位置）

    Returns:
        (corpus, field_index)：field_index 供字段 boost 用（attr 名与值都参与）。
        目录缺失 / 无匹配 / 读失败 → 空表（静默，不影响主检索）。
    """
    corpus: dict[str, str] = {}
    field_index: dict[str, list[tuple[str, str]]] = {}
    ann_dir = manual_annotations_dir(units_dir)
    if not ann_dir.is_dir() or not stems:
        return corpus, field_index
    wanted = set(stems)
    want_list = list(stems)
    for jf in sorted(ann_dir.glob("*.jsonl")):
        raw_name = jf.name[: -len(".jsonl")] if jf.name.endswith(".jsonl") else jf.name
        fname_fallback = Path(raw_name).stem
        rows: list[dict] = []
        stem = ""
        try:
            for line in jf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if not stem:
                    stem = _stem_of_manual_row(obj, fname_fallback)
                rows.append(obj)
        except OSError as e:
            log.warning(f"[rag] 人工标注读失败 {jf.name}: {e}")
            continue
        if not stem:
            stem = fname_fallback
        # 文件内 image_name 可能与文件名不一致（重命名历史）：按行内 stem 再校正一次
        stems_in_file = {_stem_of_manual_row(r, fname_fallback) for r in rows if r}
        stem = next((s for s in want_list if s in stems_in_file), stem)
        if stem not in wanted or not rows:
            continue
        lines_out: list[str] = []
        fvs: list[tuple[str, str]] = []
        for r in rows:
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            attr = str(r.get("attr") or "").strip()
            lines_out.append(f"{attr}：{text}" if attr else text)
            fvs.append((attr or "文本", text))
        if not lines_out:
            continue
        card_id = f"{GOLD_CARD_PREFIX}{stem}_0001"
        # 存**可读原文**而非 _normalize_simplify 后的压缩串：归一化会把空白与
        # 「：」一并抹掉，50 个字段会粘成一长串（字段边界全失、引文无法逐字摘录）。
        # 检索与校验层各自内部归一（_tokenize / fuzzy_match 均调 _normalize_simplify），
        # 因此这里保留「属性：文本」换行格式对命中率无损，对模型可读性更好。
        corpus[card_id] = "\n".join(lines_out)
        if fvs:
            field_index[card_id] = fvs
    return corpus, field_index


def load_content_types(
    stems: list[str],
    units_dir: Path = DEFAULT_UNITS_DIR,
) -> dict[str, str]:
    """M11：读取各 stem 的 units payload content_type（prose/roster/ledger/...）。

    供 prose 多粒度融合使用：仅 prose 图的 doc 全文参与候选竞争。
    读失败/无 payload 的 stem 不入表。
    """
    out: dict[str, str] = {}
    units_dir = Path(units_dir)
    for stem in stems:
        path = units_dir / f"{stem}.json"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[rag] units payload 读失败 {stem}: {e}")
            continue
        ct = payload.get("content_type")
        if ct:
            out[stem] = str(ct)
    return out


def get_cached_unit_artifacts(
    scope: str,
    project_id: str = "",
    project_name: str = "",
    selected_images: Optional[list[str]] = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR,
    units_dir: Path = DEFAULT_UNITS_DIR,
) -> dict:
    """v2 M3 缓存入口：unit 语料 + 字段索引 + doc 兜底语料（仍走 _CORPUS_CACHE）。

    Returns:
        {"stems", "corpus", "field_index", "doc_corpus", "prose_stems",
         "docs_tokens", "tfidf_vectors", "norms", "idf"}
    """
    with _lock:
        cache_key = "units|" + _make_cache_key(scope, project_id, project_name, selected_images)
        now = time.time()
        cached = _CORPUS_CACHE.get(cache_key)
        if cached and (now - cached.get("built_at", 0)) < _CORPUS_CACHE_TTL:
            return {k: cached[k] for k in (
                "stems", "corpus", "field_index", "doc_corpus", "prose_stems",
                "docs_tokens", "tfidf_vectors", "norms", "idf")}
        stems = _resolve_scope_stems(scope, project_id, project_name,
                                     selected_images, data_dir, knowledge_dir)
        corpus, field_index = load_unit_corpus_for_stems(stems, units_dir)
        # ✅ P-K9 阶段 0d（2026-09-10）：并入人工标注金标准语料。
        # 金标准与 unit 卡同池竞争（BM25/TF-IDF 正常排序），prompt 侧标注为
        # 人工金标准供模型区分可信度；目录缺失时为空表，行为与改造前一致。
        gold_corpus, gold_fields = load_manual_annotation_corpus_for_stems(stems, units_dir)
        if gold_corpus:
            corpus.update(gold_corpus)
            field_index.update(gold_fields)
        # M11：prose stem 集合（这些图的 doc 全文参与多粒度融合）
        content_types = load_content_types(stems, units_dir)
        prose_stems = {s for s, ct in content_types.items() if ct == "prose"}
        # 路③兜底语料：整图全文（按 stem 一篇）
        doc_corpus: dict[str, str] = {}
        for s in stems:
            txt = load_image_fulltext(s, data_dir)
            if txt:
                doc_corpus[s] = txt
        docs_tokens: list[list[str]] = [_tokenize(t) for t in corpus.values()]
        vectors, norms, idf = _build_tfidf_vectors(docs_tokens)
        if len(_CORPUS_CACHE) >= _CORPUS_CACHE_MAXSIZE:
            oldest_key = min(_CORPUS_CACHE.keys(), key=lambda k: _CORPUS_CACHE[k].get("built_at", 0))
            _CORPUS_CACHE.pop(oldest_key, None)
        entry = {
            "stems": stems,
            "corpus": corpus,
            "field_index": field_index,
            "doc_corpus": doc_corpus,
            "prose_stems": prose_stems,
            "docs_tokens": docs_tokens,
            "tfidf_vectors": vectors,
            "norms": norms,
            "idf": idf,
            "built_at": now,
        }
        _CORPUS_CACHE[cache_key] = entry
        return {k: entry[k] for k in (
            "stems", "corpus", "field_index", "doc_corpus", "prose_stems",
            "docs_tokens", "tfidf_vectors", "norms", "idf")}


def preprocess_query_rule(query: str) -> str:
    """规则版查询预处理（M3）：同义扩展 + 保留原查询。

    只用于检索（BM25/字段匹配）；原查询原样传给 LLM prompt。
    LLM 改写版本在阶段 4（M6）接入，此处为其降级路径。
    """
    q = (query or "").strip()
    if not q:
        return ""
    q_norm = _normalize_simplify(q)
    additions: list[str] = []
    for term, syns in _QUERY_SYNONYMS.items():
        t = _normalize_simplify(term)
        if t and t in q_norm:
            additions.extend(syns)
    additions = [s for s in dict.fromkeys(additions)
                 if _normalize_simplify(s) and _normalize_simplify(s) not in q_norm]
    return f"{q} {' '.join(additions)}".strip() if additions else q


# ============================================
# 10-M6. LLM 查询改写（规则版 preprocess_query_rule 的升级路径；失败降级规则版）
# ============================================
QUERY_REWRITE_SYSTEM_PROMPT = (
    "你是古籍语料检索查询改写器。把用户问题改写成适合检索的关键词组合。\n"
    "规则：\n"
    "1. 保留全部实体：人名、户名、机构、年号、地名、官职、金额（繁体原文照抄，不要转成简体）。\n"
    "2. 做文白/同义扩展，例如：进士→科年 登第 举人；存钱→存 規元 銀兩；官员→知县 職官；田赋→征銀 錢糧。\n"
    "3. 输出 1-3 行，每行一个改写式；不要解释、不要编号。\n"
    "4. 问题本身已是好的检索式时，原样输出即可。"
)
_QUERY_REWRITE_CACHE: dict[str, str] = {}
_QUERY_REWRITE_CACHE_MAXSIZE = 256


def preprocess_query_llm(query: str, ask_fn) -> str:
    """LLM 查询改写（M6）：实体保留 + 文白同义扩展；任何失败降级为规则版。

    Args:
        query: 原始用户问题
        ask_fn: callable(query:str)->str，由调用方注入 LLM 客户端
                （rag.py 不直接依赖 llm_client，避免循环导入与单测耦合）
    Returns:
        组合检索串（原问题 + 改写扩展，供 BM25/TF-IDF/字段索引使用）；
        ask_fn 异常/空输出时 = 规则版 preprocess_query_rule 结果
    """
    q = (query or "").strip()
    if not q or ask_fn is None:
        return preprocess_query_rule(q)
    cache_key = _normalize_simplify(q)
    cached = _QUERY_REWRITE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    combined = ""
    try:
        raw = (ask_fn(q) or "").strip()
        if raw:
            lines = []
            for ln in raw.splitlines():
                ln = re.sub(r"^\s*\d*\s*[.、)）]?\s*", "", ln.strip())
                ln = ln.strip("*-—·")
                if ln and len(ln) <= 120:
                    lines.append(ln)
            rewritten = " ".join(dict.fromkeys(lines))[:400]
            if rewritten and _normalize_simplify(rewritten) != cache_key:
                combined = f"{q} {rewritten}".strip()
    except Exception as e:
        log.warning(f"[rag] LLM 查询改写失败（降级规则版）: {e}")
    if not combined:
        combined = preprocess_query_rule(q)
    if len(_QUERY_REWRITE_CACHE) >= _QUERY_REWRITE_CACHE_MAXSIZE:
        _QUERY_REWRITE_CACHE.pop(next(iter(_QUERY_REWRITE_CACHE)))
    _QUERY_REWRITE_CACHE[cache_key] = combined
    return combined


def _field_boost_map(
    query: str,
    field_index: dict[str, list[tuple[str, str]]],
) -> dict[str, float]:
    """路②：字段精确索引 → {card_id: boost}。

    查询（归一化）包含某 unit 的字段值 → 该 unit + FIELD_BOOST_WEIGHT × 命中数。
    """
    q_norm = _normalize_simplify(query)
    if not q_norm or not field_index:
        return {}
    boost: dict[str, float] = {}
    for cid, fvs in field_index.items():
        n = 0
        for _k, v in fvs:
            v_norm = _normalize_simplify(v)
            if len(v_norm) >= FIELD_MIN_VALUE_LEN and v_norm in q_norm:
                n += 1
                if n >= FIELD_BOOST_CAP:
                    break
        if n:
            boost[cid] = FIELD_BOOST_WEIGHT * n
    return boost


# ============================================
# 10-M7. 本地语义召回（bge-small-zh ONNX；模型缺失/加载失败 → 整体降级纯词法）
# ============================================
# 方案 §2.6 预授权路径：semantic/table_lookup 实测不达标后启用本地小模型。
# 设计纪律：
#   - 不引入向量数据库/外部 embedding 服务：本地 ONNX（onnxruntime + tokenizers，均已在 venv）
#   - 模型文件缺失或加载失败 → _get_embedder 返回 None，检索行为与 M6 版完全一致
#   - 语料向量缓存 keyed by 语料指纹（键集+长度），与 _CORPUS_CACHE 同等生命周期
SEMANTIC_MODEL_DIR = _BASE / "models" / "bge-small-zh-v1.5-onnx"
SEMANTIC_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："  # bge v1.5 s2p 官方指令前缀
SEMANTIC_MAX_TOKENS = 256        # unit 卡文本都很短，512 上限内截断
SEMANTIC_WEIGHT = 0.35           # 排序混合：final = lexical + W * cos_sim
SEMANTIC_RESCUE_SIM = 0.55       # 词法拒答边缘的语义救援阈值
_SEMANTIC_EMBEDDER = None
_SEMANTIC_LOAD_FAILED = False    # 只试装一次，避免每请求重复加载失败
_SEM_CORPUS_CACHE: dict = {}     # {corpus_fingerprint: np.ndarray}
_SEM_CORPUS_CACHE_MAXSIZE = 4


class _OnnxEmbedder:
    """bge-small-zh-v1.5 ONNX 推理封装（CLS 池化 + L2 归一化，CPU）。"""

    def __init__(self, model_dir: Path):
        import numpy as np
        from tokenizers import Tokenizer
        import onnxruntime as ort

        self._np = np
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=SEMANTIC_MAX_TOKENS)
        # ✅ M7 修正：batch 内文本长度必须一致，否则 np.array 拼接炸
        # （"setting an array element with a sequence ... inhomogeneous shape"）
        self.tokenizer.enable_padding()  # 默认 pad_to_longest，attention_mask 自动处理
        self.session = ort.InferenceSession(
            str(model_dir / "onnx" / "model_quantized.onnx"),
            providers=["CPUExecutionProvider"],
        )
        input_names = {i.name for i in self.session.get_inputs()}
        self._need_token_type = "token_type_ids" in input_names

    def encode(self, texts: list[str]):
        """→ (n, dim) L2 归一化向量（float32）。"""
        np = self._np
        enc = self.tokenizer.encode_batch(list(texts))
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if self._need_token_type:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]  # (n, seq, dim) last_hidden_state
        cls = hidden[:, 0, :]                      # bge：CLS 池化
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return (cls / norm).astype(np.float32)


def _get_embedder():
    """惰性加载语义模型；任何失败只记一次并永久降级（返回 None）。"""
    global _SEMANTIC_EMBEDDER, _SEMANTIC_LOAD_FAILED
    if _SEMANTIC_EMBEDDER is not None:
        return _SEMANTIC_EMBEDDER
    if _SEMANTIC_LOAD_FAILED:
        return None
    try:
        if not (SEMANTIC_MODEL_DIR / "onnx" / "model_quantized.onnx").exists():
            raise FileNotFoundError(f"语义模型缺失: {SEMANTIC_MODEL_DIR}")
        _SEMANTIC_EMBEDDER = _OnnxEmbedder(SEMANTIC_MODEL_DIR)
        log.info("[rag] 语义召回模型已加载: bge-small-zh-v1.5 (onnx int8)")
        return _SEMANTIC_EMBEDDER
    except Exception as e:
        _SEMANTIC_LOAD_FAILED = True
        log.warning(f"[rag] 语义模型加载失败（降级纯词法检索）: {e}")
        return None


def _corpus_fingerprint(corpus: dict) -> str:
    import hashlib
    h = hashlib.sha1()
    for k in sorted(corpus):
        h.update(f"{k}\x1f{len(corpus[k])}\x1e".encode("utf-8"))
    return h.hexdigest()


def _corpus_embeddings(corpus: dict):
    """语料向量（带指纹缓存）；embedder 不可用时返回 (None, keys, None)。"""
    import numpy as np
    emb = _get_embedder()
    if emb is None or not corpus:
        return None, [], None
    keys = sorted(corpus)
    fp = _corpus_fingerprint(corpus)
    with _lock:
        matrix = _SEM_CORPUS_CACHE.get(fp)
    if matrix is None:
        matrix = emb.encode([corpus[k] for k in keys])
        with _lock:
            if len(_SEM_CORPUS_CACHE) >= _SEM_CORPUS_CACHE_MAXSIZE:
                _SEM_CORPUS_CACHE.pop(next(iter(_SEM_CORPUS_CACHE)))
            _SEM_CORPUS_CACHE[fp] = matrix
    return emb, keys, matrix


def semantic_sims(query: str, corpus: dict):
    """查询↔语料余弦相似度 map（M7）。

    Returns:
        {card_id: cos_sim}；模型不可用/语料为空/推理异常时返回 None
        （调用方按纯词法处理——语义路任何故障都不得拖垮主检索）。
    """
    import numpy as np
    if not (query or "").strip() or not corpus:
        return None
    try:
        emb, keys, matrix = _corpus_embeddings(corpus)
        if emb is None:
            return None
        qv = emb.encode([SEMANTIC_QUERY_PREFIX + _normalize_simplify(query)])[0]
        sims = matrix @ qv
        return {k: float(s) for k, s in zip(keys, sims)}
    except Exception as e:
        log.warning(f"[rag] 语义相似度计算失败（降级纯词法）: {e}")
        return None


def hybrid_search_v2(
    query: str,
    corpus: dict[str, str],
    top_k: int = 5,
    alpha: float = 0.5,
    field_index: Optional[dict] = None,
    candidate_multiplier: int = 3,
    sem_sims: Optional[dict[str, float]] = None,
) -> list[dict]:
    """路①+②：unit hybrid 检索 + 字段 boost 重排（v2 主检索）。

    结果项新增 card_id（= 语料 key）与 field_boost 字段；
    image_stem 字段保持为语料 key（unit 卡 id 或 stem），下游兼容。

    M7：传入 sem_sims（semantic_sims 的结果）时，按
    final = lexical + SEMANTIC_WEIGHT × cos_sim 融合重排；
    各结果项记录 sem_sim 字段（无语义结果时为 None）。
    """
    cand_k = max(top_k, top_k * candidate_multiplier)
    cands = hybrid_search(query, corpus, top_k=cand_k, alpha=alpha,
                          candidate_multiplier=candidate_multiplier)
    # 字段 boost 用预处理后的查询（同义词扩展后能命中"堂/记"等简称）
    boost = _field_boost_map(preprocess_query_rule(query), field_index or {})
    for r in cands:
        cid = r.get("image_stem", "")
        r["card_id"] = cid
        # ✅ v2 修正：image_stem 还原为真实图片 stem（unit_<stem>_<seq> → <stem>），
        # 前端显示 / 日志聚合用；card_id 保留语料 key 供脚注白名单与 eval 适配。
        # ✅ P-K9 阶段 0d：前缀扩到 (unit|gold)（金标准卡）；量词改贪婪——
        # 非贪婪在 stem 本身以 _dddd 结尾时（如「材料名…_0001」）会少剥一段，
        #    导致 image_stem 显示被截断。贪婪取到"最后一个 _dddd 之前"，正确。
        m = re.match(r"(?:unit|gold)_(.+)_\d{4}$", cid)
        if m:
            r["image_stem"] = m.group(1)
        r["field_boost"] = boost.get(cid, 0.0)
        r["score"] = round(r.get("score", 0.0) + boost.get(cid, 0.0), 4)
        if sem_sims:
            # M7：语义融合 final = lexical + SEMANTIC_WEIGHT × cos_sim
            sim = sem_sims.get(cid, 0.0)
            r["sem_sim"] = round(sim, 4)
            r["score"] = round(r["score"] + SEMANTIC_WEIGHT * sim, 4)
        else:
            r["sem_sim"] = None
    cands.sort(key=lambda x: -x["score"])
    return cands[:top_k]


def _fuse_prose_docs(
    query: str,
    retrieved: list[dict],
    doc_corpus: dict[str, str],
    prose_stems: Optional[set],
    top_k: int,
    alpha: float,
    use_semantic: bool,
    refusal_threshold: float,
) -> list[dict]:
    """M11：prose 图 doc 全文候选与 unit 结果同池竞争。

    仅处理 prose_stems 中的图；unit 结果已覆盖的 stem 不重复并入；
    doc tfidf 达拒答阈值才算可信候选。融合后按 score 排序截断 top_k。
    任何异常都降级返回原 retrieved（融合失败不拖垮主检索）。
    """
    try:
        if not prose_stems or not doc_corpus or not retrieved:
            return retrieved
        covered = {r.get("image_stem", "") for r in retrieved}
        prose_docs = {s: t for s, t in doc_corpus.items()
                      if s in prose_stems and s not in covered}
        if not prose_docs:
            return retrieved
        doc_res = hybrid_search(query, prose_docs, top_k=3, alpha=alpha)
        doc_sem = semantic_sims(query, prose_docs) if use_semantic else None
        fused: list[dict] = []
        for r in doc_res:
            stem = r.get("image_stem", "")
            if (r.get("tfidf_score", 0.0) or 0.0) < refusal_threshold:
                continue
            r["card_id"] = f"section_{stem}_col_0"
            r["granularity"] = "doc"
            r["sem_sim"] = None
            if doc_sem and stem in doc_sem:
                sim = doc_sem[stem]
                r["sem_sim"] = round(sim, 4)
                r["score"] = round(r.get("score", 0.0) + SEMANTIC_WEIGHT * sim, 4)
            fused.append(r)
        if not fused:
            return retrieved
        merged = retrieved + fused
        merged.sort(key=lambda x: -x.get("score", 0.0))
        return merged[:max(top_k, len(retrieved))]
    except Exception as e:
        log.warning(f"[rag] prose doc 融合失败（降级纯 unit 结果）: {e}")
        return retrieved


def search_with_fallback(
    query: str,
    corpus: dict[str, str],
    doc_corpus: dict[str, str],
    top_k: int = 5,
    alpha: float = 0.5,
    field_index: Optional[dict] = None,
    refusal_threshold: float = HARD_REFUSAL_TFIDF_THRESHOLD,
    use_semantic: bool = True,
    prose_stems: Optional[set] = None,
) -> tuple[list[dict], bool, str]:
    """v2 三路检索主入口（M3 + M7 语义救援 + M11 prose 多粒度融合）。

    ① unit hybrid + 字段 boost（+ M7 语义分数融合）；
    ②（合并在 ① 里的字段重排）；
    ②-B M11 prose 多粒度融合：prose 图的逐行 unit 碎片检索弱（叙事类查询
       词面歧义大），其 doc 全文与 unit 候选同池竞争——全文 tfidf 达拒答阈值
       且 stem 未被 unit 结果覆盖时，作为 section_<stem>_col_0 候选并入排序；
    ③ unit 级不可信（top TF-IDF < refusal_threshold）→ doc 整图兜底；
    ④ doc 也不可信，但语义救援命中（max sim ≥ SEMANTIC_RESCUE_SIM）
       → 返回 lexical unit 结果，route="semantic"（不让误拒吞掉口语化改述查询）；
    ⑤ 都不可信 → hard_refusal=True（调用方应走"未找到"模板，不让 LLM 自由发挥）。

    use_semantic=False 时完全退回 M6 行为（不加载模型、不加语义分、不救援）。

    Returns:
        (retrieved, hard_refusal, route)
        route ∈ {"unit", "doc", "semantic", "refused"}
    """
    sem_sims = None
    if use_semantic and corpus:
        sem_sims = semantic_sims(query, corpus)
    retrieved = hybrid_search_v2(query, corpus, top_k=top_k, alpha=alpha,
                                 field_index=field_index, sem_sims=sem_sims)
    top_tfidf = retrieved[0].get("tfidf_score", 0.0) if retrieved else 0.0
    if retrieved and top_tfidf >= refusal_threshold:
        for r in retrieved:
            r["granularity"] = "unit"
        # === M11：prose 多粒度融合（仅 unit 路置信时）===
        retrieved = _fuse_prose_docs(
            query, retrieved, doc_corpus, prose_stems,
            top_k=top_k, alpha=alpha, use_semantic=use_semantic,
            refusal_threshold=refusal_threshold,
        )
        return retrieved, False, "unit"
    # 路③：doc 整图兜底
    if doc_corpus:
        doc_res = hybrid_search(query, doc_corpus, top_k=top_k, alpha=alpha)
        doc_top = doc_res[0].get("tfidf_score", 0.0) if doc_res else 0.0
        if doc_res and doc_top >= refusal_threshold:
            for r in doc_res:
                r["card_id"] = f"section_{r.get('image_stem', '')}_col_0"
                r["granularity"] = "doc"
            return doc_res, False, "doc"
    # 路④：M7 语义救援——词面完全 miss 但向量相似度足够高的口语化改述。
    # 注意：此时词法候选可能为空（hybrid_search 过滤零分结果），
    # 因此直接按语义相似度对全语料排序构造结果，不依赖 retrieved。
    if sem_sims:
        ranked = sorted(sem_sims.items(), key=lambda kv: -kv[1])[:top_k]
        if ranked and ranked[0][1] >= SEMANTIC_RESCUE_SIM:
            res = []
            for cid, sim in ranked:
                full_text = corpus.get(cid, "")
                # ✅ P-K9 阶段 0d：前缀扩到 (unit|gold)（金标准卡）；量词改贪婪——
                # 非贪婪在 stem 本身以 _dddd 结尾时（如「材料名…_0001」）会少剥一段，
                #    导致 image_stem 显示被截断。贪婪取到"最后一个 _dddd 之前"，正确。
                m = re.match(r"(?:unit|gold)_(.+)_\d{4}$", cid)
                res.append({
                    "image_stem": m.group(1) if m else cid,
                    "card_id": cid,
                    "score": round(sim, 4),          # 纯语义路由下 score=cos_sim
                    "sem_sim": round(sim, 4),
                    "bm25_score": 0.0,
                    "tfidf_score": 0.0,
                    "preview": full_text[:120] + ("..." if len(full_text) > 120 else ""),
                    "full_text": full_text,
                    "granularity": "unit",
                    "semantic_rescue": True,
                })
            return res, False, "semantic"
    return retrieved, True, "refused"


# ============================================
# 10-B. M9：聚合意图检测 + 确定性金额排序检索
# ============================================
# 背景：eval Q069-071（"这本账簿里金额最大的一笔账是多少，是哪一户的？"）
# 与最大金额单元零词面重叠、语义相似度低于救援阈值 → 聚合类查询检索必然落空。
# 聚合是确定性的数值计算问题，不该交给向量检索——检测到意图后直接扫
# field_index 的"金额归一"字段排序，构造 top_k 检索结果，交给结构化 RAG
# prompt 让 LLM 基于真实单元作答（引用与编造防护照常生效）。

_AGG_MAX_RE = re.compile(r"最大|最多|最高")
_AGG_MIN_RE = re.compile(r"最小|最低|最少")
_AGG_AMOUNT_RE = re.compile(r"金额|金額|錢|钱|款|一笔|一筆|账|賬")


def detect_aggregate_intent(query: str) -> Optional[str]:
    """检测金额聚合意图。返回 "max" / "min" / None。

    判据（宁缺勿滥，防误劫持普通查询）：极值词（最大/最小等）与金额语境词
    （金额/钱/款/一笔/账等）同时出现才触发。
    """
    if not query:
        return None
    q = _normalize_simplify(query)
    if not _AGG_AMOUNT_RE.search(q):
        return None
    if _AGG_MAX_RE.search(q):
        return "max"
    if _AGG_MIN_RE.search(q):
        return "min"
    return None


def aggregate_amount_search(
    op: str,
    corpus: dict[str, str],
    field_index: Optional[dict],
    top_k: int = 5,
) -> list[dict]:
    """按"金额归一"字段对 unit 语料做确定性排序，返回 top_k 检索结果。

    Args:
        op: "max" 或 "min"（detect_aggregate_intent 的返回值）。
        corpus: {card_id: text}（get_cached_unit_artifacts 的语料）。
        field_index: {card_id: [(field_key, raw_value), ...]}。

    Returns:
        与 search_with_fallback 结果项同构的列表（card_id / image_stem /
        score / preview / full_text / granularity="unit"），额外带
        agg_amount（数值）与 agg_op 字段；无金额单元时返回空列表。
    """
    if op not in ("max", "min"):
        return []
    cands: list[tuple[float, str]] = []
    for cid, fvs in (field_index or {}).items():
        if cid not in corpus:
            continue
        amt: Optional[float] = None
        for k, v in fvs:
            if k == "金额归一":
                try:
                    amt = float(str(v).replace(",", ""))
                except (TypeError, ValueError):
                    amt = None
                break
        if amt is not None:
            cands.append((amt, cid))
    if not cands:
        return []
    # 降序取 max，升序取 min；同额并列按 (金额, card_id) 稳定排序，
    # 保证同一账簿多通道摄取的重复单元都能进 top_k（eval Q069-071 依赖此行为）
    cands.sort(key=lambda t: (-t[0], t[1]) if op == "max" else (t[0], t[1]))
    results: list[dict] = []
    for amt, cid in cands[:max(1, top_k)]:
        full_text = corpus.get(cid, "")
        # ✅ P-K9 阶段 0d：前缀扩到 (unit|gold)（金标准卡）；量词改贪婪——
        # 非贪婪在 stem 本身以 _dddd 结尾时（如「材料名…_0001」）会少剥一段，
        #    导致 image_stem 显示被截断。贪婪取到"最后一个 _dddd 之前"，正确。
        m = re.match(r"(?:unit|gold)_(.+)_\d{4}$", cid)
        results.append({
            "image_stem": m.group(1) if m else cid,
            "card_id": cid,
            "score": round(amt, 4),
            "agg_amount": amt,
            "agg_op": op,
            "sem_sim": None,
            "bm25_score": 0.0,
            "tfidf_score": 0.0,
            "preview": full_text[:120] + ("..." if len(full_text) > 120 else ""),
            "full_text": full_text,
            "granularity": "unit",
            "aggregate": True,
        })
    return results


# ============================================
# 11. 阶段 4-B：代码化引用真值校验（防 LLM 幻觉）
# ============================================
# 灵感来自《深入理解 AI Agent》§5.2.2 "代码作为业务规则的约束"：
# "所有政策事实一律从数据库读取，绝不采信模型自报的值"
# 这里"数据库"=检索到的原文 fulltext；"模型自报"=LLM 在 answer 里 claim 的引用 quotes。
DEFAULT_FUZZY_THRESHOLD = 0.85  # difflib.SequenceMatcher.ratio 阈值；<0.85 视为"可能编造"


def fuzzy_match(
    needle: str,
    haystack: str,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[bool, float]:
    """模糊匹配 needle 是否在 haystack 中真实存在（容许小差异如标点/空白）。

    策略：
    1) 严格子串包含 → True, 1.0
    2) 否则用 difflib 在 haystack 上找最相似的窗口（窗口长 = len(needle) * 1.2），
       算 SequenceMatcher.ratio；>= threshold 视为命中。
    3) 否则 False, ratio。

    耗时：haystack 通常 < 500 字符，windowing 扫一遍 < 1ms；远快于 n-gram 全量比对。

    Returns:
        (matched: bool, score: float)
    """
    if not needle or not haystack:
        return False, 0.0
    needle_n = _normalize_simplify(needle)
    haystack_n = _normalize_simplify(haystack)
    if not needle_n or not haystack_n:
        return False, 0.0
    # 1) 严格子串
    if needle_n in haystack_n:
        return True, 1.0
    # 2) 窗口滑动
    n_len = len(needle_n)
    if n_len > len(haystack_n):
        # needle 比 haystack 长，单独比
        ratio = difflib.SequenceMatcher(None, needle_n, haystack_n).ratio()
        return ratio >= threshold, ratio
    win_size = max(n_len, int(n_len * 1.2))
    step = max(1, n_len // 4)  # 步长 = needle 长度 1/4（足够密 + 不太慢）
    best = 0.0
    for i in range(0, len(haystack_n) - win_size + 1, step):
        window = haystack_n[i:i + win_size]
        r = difflib.SequenceMatcher(None, needle_n, window).ratio()
        if r > best:
            best = r
            if best >= threshold:
                return True, best
    # 窗口没扫到的剩余尾部
    if len(haystack_n) > win_size:
        tail = haystack_n[-win_size:]
        r = difflib.SequenceMatcher(None, needle_n, tail).ratio()
        if r > best:
            best = r
    return best >= threshold, best


def _strip_card_id_suffix(quote: str) -> str:
    """剥掉 quote 末尾可能附带的 [card_id] 引用脚注（避免影响 fuzzy match）"""
    # 移除 "参考 [xxx]" / "[xxx]" 形式（中文 card_id 模式；v2 M3 含 unit_、阶段 0d 含 gold_）
    s = re.sub(r"\s*参考\s*\[[^\]]*(?:section|unit|gold)_[^\]]+\]\s*\.?\s*$", "", quote)
    s = re.sub(r"\s*\[[^\]]*(?:section|unit|gold)_[^\]]+\]\s*\.?\s*$", "", s)
    return s.strip()


def verify_relevance(
    quotes: list[str],
    fulltext: str,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> dict:
    """验证 LLM 报的 relevant_quotes 是否在原文中真实存在。

    Args:
        quotes: LLM 在 JSON 输出里 claim 的"相关引用"列表
        fulltext: 检索到的原文（data/structured/<stem>.json L2_lines 拼接）
        threshold: fuzzy match 阈值

    Returns:
        {
            "claimed": int,            # LLM 报的引用数
            "verified": int,           # 通过 fuzzy match 的引用数
            "fabricated": int,         # 未通过（可能编造）的引用数
            "fabrication_rate": float, # fabricated / claimed
            "details": [
                {"quote": "...", "matched": bool, "score": float}, ...
            ],
            "verified_text": str,      # 通过验证的 quote 拼接（供下游答案生成用）
        }
    """
    if not quotes:
        return {
            "claimed": 0, "verified": 0, "fabricated": 0,
            "fabrication_rate": 0.0, "details": [], "verified_text": "",
        }
    details: list[dict] = []
    verified_texts: list[str] = []
    for q in quotes:
        if not isinstance(q, str):
            q = str(q)
        # 先剥掉 LLM 可能附在 quote 末尾的引用脚注（防误判 fabricated）
        clean_q = _strip_card_id_suffix(q)
        matched, score = fuzzy_match(clean_q, fulltext, threshold=threshold)
        details.append({
            "quote": q,
            "clean_quote": clean_q,
            "matched": matched,
            "score": round(score, 4),
        })
        if matched:
            verified_texts.append(clean_q)
    verified = len(verified_texts)
    fabricated = len(quotes) - verified
    return {
        "claimed": len(quotes),
        "verified": verified,
        "fabricated": fabricated,
        "fabrication_rate": fabricated / max(1, len(quotes)),
        "details": details,
        "verified_text": "\n".join(verified_texts),
    }


# 阶段 4-B 用的 JSON system prompt（强制 LLM 输出结构化）
STRUCTURED_SYSTEM_PROMPT = (
    "你是档案资料助手，必须严格基于检索片段回答，禁止编造。\n"
    "请按以下 JSON 格式输出（仅输出 JSON，不要其他文字）：\n"
    "{\n"
    '  "relevant_quotes": ["<逐字摘录检索片段中含问题关键词或与问题相关的原文，1-3 段>"],\n'
    '  "answer": "<基于上述引用的简洁回答，使用现代汉语>",\n'
    '  "confidence": <0-1 的浮点数，0=完全不确定，1=完全基于原文>\n'
    "}\n"
    "硬性规则：\n"
    "1. relevant_quotes 必须是检索片段中**逐字存在**的原文，不允许改写/概括/补充。\n"
    "2. **relevant_quotes 内不要添加 [card_id] 引用脚注**——引用脚注只在 answer 末尾用一次。\n"
    "3. **宽松摘录**：只要检索片段中出现问题的关键词（如人名、地名、年号），就该段算'相关'，必须摘录；\n"
    "   即使该段不能直接回答问题（如只提到关键词但未解释），也要摘出来供用户参考。\n"
    "4. 只有当所有检索片段都**完全不包含**问题中任何关键词时，relevant_quotes 才留空 []，\n"
    "   answer 写'未找到相关信息'。\n"
    "5. answer 必须只基于 relevant_quotes，禁止添加 quotes 之外的信息。\n"
    "6. **answer 必须从 relevant_quotes 中直接引用关键事实**（人名、年号、数字等），不要只说'原文提到'或'未明确说明'。\n"
    "   例如：quote 含'天启七年邓璜'，问题问'天启年间知县'，answer 应直接说'天启七年（1627）有邓璜'。\n"
    "7. confidence < 0.5 时，answer 可以以'根据有限信息，'开头，但**仍要列出 quotes 中的具体内容**。\n"
    "8. answer 末尾用一次：'参考 [<card_id>]'（card_id 形如 section_<stem>_col_<col_index>）。\n"
    "9. **负面问题必须明确拒答**：若 quotes 只是出现了与问题关键词相同的字眼（人名/年号/地名等），\n"
    " 但**不含问题所问的实际内容**（如问'田赋与清代相比的变化'，片段只有'某卷第八册姓名'这类\n"
    "   人名罗列；问'战后赔偿条款'，片段只有碰巧同姓的人名），answer 必须以'未找到相关信息'开头，\n"
    "   再用一句话说明片段实际内容是什么。禁止把'仅出现关键词'包装成部分回答。\n"
    "10. **识别可靠性**：检索片段来自 OCR，未经人工校对的字句可能有识别错误。"
    "涉及精确字句（人名、机构名、年份、金额数字）时，answer 末尾补一句"
    "'以上为未校对 OCR 结果，请核对原图'；引用较多时以 confidence 反映不确定度。\n"
    "11. **人工标注金标准优先**：标注为「人工标注·金标准」的片段是研究者逐字校对过的"
    "权威文本，**优先级高于任何 OCR 片段**。同一事实若两者冲突，一律以金标准为准，"
    "且不要对其添加'未校对'提示。金标准片段同样可被 relevant_quotes 逐字摘录。"
)


def build_structured_rag_prompt(
    query: str,
    retrieved: list[dict],
    history: Optional[list[dict]] = None,
) -> list[dict]:
    """构造"结构化 JSON 输出"版 RAG prompt。

    与 build_rag_prompt 区别：强制 LLM 输出 JSON {relevant_quotes, answer, confidence}。
    下游用 verify_relevance 验证 quotes 真值，把 fabrication 率展示给用户。
    """
    if retrieved:
        ref_lines: list[str] = []
        for i, item in enumerate(retrieved, 1):
            stem = item.get("image_stem", "")
            preview = item.get("preview", "")
            # ✅ v2 M3：unit 检索结果自带 card_id
            card_id = item.get("card_id") or f"section_{stem}_col_0"
            # ✅ P-K9 阶段 0d：人工标注金标准片段显式标注，供模型区分可信度
            gold_tag = "  【人工标注·金标准】" if str(card_id).startswith(GOLD_CARD_PREFIX) else ""
            ref_lines.append(
                f"[{i}] 来源图片: {stem}{gold_tag}\n"
                f"    引用编号 [{card_id}]\n"
                f"    全文（用于摘录相关原文）：\n{preview}\n"
                f"    (以上全文是真实的；你必须在 answer 中只引用这里出现过的内容)"
            )
        refs_text = "\n\n".join(ref_lines)
        user_content = (
            f"问题: {query}\n\n"
            f"参考材料（每条含全文，必须基于此回答）：\n{refs_text}\n\n"
            f"任务：\n"
            f"1. 在每条参考材料中，找出**包含问题关键词**（如人名/地名/事件/数字）的句子。\n"
            f"2. 把这些句子**逐字**写入 relevant_quotes（1-3 段即可；不要多于此数）。\n"
            f"3. 即使这些句子不能直接回答问题（如只提到关键词但未解释），也要摘出来——让用户能自己看原文。\n"
            f"4. answer 必须基于 quotes；如果 quotes 中有解释性信息就综合回答；\n"
            f"   如果 quotes 只是'提到关键词'而没有直接答案，answer 说'根据检索到的片段，<quote 内容>，但未提供<用户问题的具体方面>'。"
        )
    else:
        user_content = f"问题: {query}\n\n（未找到相关参考材料，relevant_quotes=[]，answer='未找到相关信息'）"

    messages: list[dict] = [
        {"role": "system", "content": STRUCTURED_SYSTEM_PROMPT},
    ]
    if history:
        messages.extend(history)  # P-K7 阶段 5：删硬截断，让调用方控
    messages.append({"role": "user", "content": user_content})
    return messages


# ============================================
# 12. 阶段 4-B-C：两阶段生成（先抽 quote，再基于 verified quotes 自由生成）
# ============================================
# 灵感：AI-Agents-in-Depth §5.2.2 末尾"合并校验与执行"
# - 阶段 1：LLM 输出结构化 JSON（relevant_quotes + 简单 answer）
# - 服务端 verify_relevance 验证 quotes 真值
# - 阶段 2：把 verified quotes 喂给 LLM，让它"基于已验证的事实"自由生成详细答案
# 这样 LLM 既不会编造（用代码约束），又可以自由发挥（基于真值）
ANSWER_FROM_QUOTES_SYSTEM_PROMPT = (
    "你是档案资料助手。基于已通过真值校验的原文片段（**已被系统验证确实来自检索材料**），"
    "生成对用户问题的详细、准确回答。\n"
    "硬性规则：\n"
    "1. answer 必须只基于 verified_quotes 提供的具体信息（如人名、年号、数字）。\n"
    "2. **必须把 verified_quotes 中的关键信息（人名/数据/年份）直接列出**，不要只说'原文提到'。\n"
    "3. 如果 verified_quotes 不包含问题的核心信息（如只能看到关键词但没解释），answer 说"
    "'根据检索到的片段，<quote 摘要>，但未提供<用户具体问题>的答案'。\n"
    "4. 不要编造 verified_quotes 之外的内容。\n"
    "5. 末尾用 '参考 [<card_id>]' 标注引用来源（card_id 形如 section_<stem>_col_<col_index>）。"
)


def build_answer_from_quotes_prompt(
    query: str,
    verified_quotes: list[str],
    card_ids: list[str],
    history: Optional[list[dict]] = None,
) -> list[dict]:
    """构造"阶段 2"prompt：基于已验证 quotes 让 LLM 自由生成答案。

    Args:
        query: 用户原始问题
        verified_quotes: 已通过 fuzzy match 验证的引用片段列表
        card_ids: 对应每个 verified quote 的 card_id（同一长度）
        history: 多轮对话历史

    Returns:
        messages: [{"role": ..., "content": ...}, ...]
    """
    if verified_quotes:
        ref_lines: list[str] = []
        for i, (q, cid) in enumerate(zip(verified_quotes, card_ids), 1):
            ref_lines.append(f"[{i}] {q}（来源：{cid}）")
        refs_text = "\n".join(ref_lines)
        user_content = (
            f"问题: {query}\n\n"
            f"已验证的原文片段（**这些内容已被系统验证确实来自检索材料，可放心引用**）：\n{refs_text}\n\n"
            f"任务：基于以上 verified 片段，生成对问题的详细回答。\n"
            f"要求：\n"
            f"- **必须列出** verified 片段中的具体信息（人名、年号、数字等）\n"
            f"- 不要只说'原文提到'或'未明确说明'\n"
            f"- 如果信息不足，诚实说明"
        )
    else:
        user_content = f"问题: {query}\n\n（已验证片段为空，说明检索内容与问题无关）"

    messages: list[dict] = [
        {"role": "system", "content": ANSWER_FROM_QUOTES_SYSTEM_PROMPT},
    ]
    if history:
        messages.extend(history)  # P-K7 阶段 5：删硬截断，让调用方控
    messages.append({"role": "user", "content": user_content})
    return messages


def parse_structured_response(raw_text: str) -> dict:
    """从 LLM 原始输出解析结构化 JSON。

    兼容：
    1) 纯 JSON
    2) markdown ```json ... ``` 包裹
    3) 前后有非 JSON 文字

    Returns:
        dict（至少含 relevant_quotes, answer, confidence；失败时 answer=raw_text, confidence=0）
    """
    if not raw_text:
        return {"relevant_quotes": [], "answer": "", "confidence": 0.0, "_parse_ok": False}
    text = raw_text.strip()
    # 提取 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    # 找最外层 {...}
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        text = text[first_brace:last_brace + 1]
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # ✅ M28：LLM 输出瑕疵容错——实测（一次批量评测的 Q083/Q084）deepseek
        # 偶发在 JSON 尾部多打一个闭合括号（"confidence": 0.0}\n}）导致整段 parse
        # 失败。后果：负样本时 raw JSON 直接呈现给用户；正样本时被 M16 空 quotes
        # 闸误杀成"未找到"。修法：用 raw_decode 提取第一个**完整** JSON 对象，
        # 忽略尾部多余文字/括号（对前后有非 JSON 文字的旧兼容路径同样生效）。
        try:
            start = text.find("{")
            if start != -1:
                obj, _end = json.JSONDecoder().raw_decode(text[start:])
            else:
                raise ValueError("no opening brace")
        except (json.JSONDecodeError, ValueError):
            # 解析失败：当纯文本处理
            return {
                "relevant_quotes": [],
                "answer": raw_text.strip(),
                "confidence": 0.0,
                "_parse_ok": False,
                "_parse_error": "JSON parse failed",
            }
    # 字段类型归一
    quotes = obj.get("relevant_quotes", [])
    if not isinstance(quotes, list):
        quotes = []
    answer = obj.get("answer", "")
    if not isinstance(answer, str):
        answer = str(answer)
    try:
        conf = float(obj.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {
        "relevant_quotes": quotes,
        "answer": answer,
        "confidence": conf,
        "_parse_ok": True,
    }

