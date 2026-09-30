# -*- coding: utf-8 -*-
"""校对回流语料的**唯一读取与构造口**（闭合「金标准-数据飞轮」断链①，2026-09-11）。

## 为什么需要这个模块

2026-09-10 的飞轮审计发现四道断链，其中第一道是：
**`data/fewshot/corrections.jsonl` 只写不读** —— `review_store.append_fewshot`
把「模型读错、人工改对」的样本写进池子，但全项目**没有任何代码消费它**
（唯一"读"它的 `overview_stats` 只数行数，用于看板显示）。
后果：**人工每修一条，识别端一无所获** —— 这是全平台最大的价值泄漏。

本模块把「读池 → 选样 → 构造 prompt 片段 → 报告元数据」收敛为**一处实现**，
供 `two_stage`（主链路）与 `divergence_sampler`（采样器）共同消费。

## 三条硬约束（红线）

1. **绝不丢字**：本模块只影响 prompt 文本，不改任何识别结果或落盘内容。
2. **零回归**：池为空（或缺文件/坏行）时 `build_hint` 返回**逐字未改**的入参，
   prompt 与未接通时**完全相同** —— 这是可机械断言的判据，见
   `tests/test_feedback.py::TestZeroRegression`。
3. **漂移可见 + 逃生门**（§35 红线三）：注入了几条、来自哪些桶、池指纹是什么，
   一律通过 `hint_meta` 落进 run 产物；调用方可用 `feedback_entries=None`
   或 `enable_feedback=False` 完全关闭。

## 与 `review_store` 的分工

- **写入口径**在 `review_store.append_fewshot`（队列管理，本模块不写池）；
- **桶键口径**下沉到本模块的 `bucket_key`，`review_store` 转发 ——
  避免"写入按一套分桶、读取按另一套分桶"的静默分叉。

## 两个回流源（2026-09-11 增补第二源）

| 源 | 落点 | 产生方式 | 实测规模 |
|---|---|---|---|
| ① 待核队列更正 | `data/fewshot/corrections.jsonl` | `/review` 工作台「修正」 | 3 条 |
| ② **裁决采纳** | `manual_annotations/*.jsonl`（`source="ai_verified"`） | `/annotate` 裁决面板采纳 | **0 条**（截至 2026-09-11） |

两个源都是「模型读错 → 人工改对」，形状一致（`field/wrong/right/profile_id`），
故合并后走**同一套**选样与提示块构造。第二源的 `wrong` 需**回读草稿**
（`data/preannotations/<stem>.ai.jsonl`）取 AI 原值 —— 定位与分段一律复用
`adjudicate.segments_of`，与写入端同源。

**只取真正改过值的行**：`ai_ref.edited` 是条目级标记（多段条目各段共用），
逐行比较「草稿该段原值 vs 行文本」才能定位真正的更正；文本相同的行无信息。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("feedback")

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()
else:
    _BASE = Path(__file__).parent.resolve()
BASE_DIR = _BASE

DEFAULT_CORRECTIONS = BASE_DIR / "data" / "fewshot" / "corrections.jsonl"
DEFAULT_BUCKET_CAP = 30     # 与 review_store.FEWSHOT_BUCKET_CAP 同值（单桶存量上限）
DEFAULT_TOTAL_CAP = 12      # **prompt 注入上限**：防止提示块淹没版面先验
MAX_FIELD_LEN = 24          # 字段名截断（防畸形条目撑爆 prompt）
MAX_TEXT_LEN = 64           # 单条 wrong/right 截断

HINT_HEADER = "既往校对更正（同一文献类型的人工核对结果）"
HINT_FOOTER = ("以上仅供参照，一律以本页图像为准：不适用于本页的条目请忽略，"
               "无对应情形时按前述要求照录原文。")


# ============================================
# 池读取（容错：缺文件 / 坏行 / 非 dict 一律跳过，绝不抛）
# ============================================
def load_corrections(path: Optional[Path] = None) -> List[dict]:
    """读回流池；任何异常都退化为空列表（**绝不阻断识别链路**）。

    坏行**跳过单行**而非丢弃整个文件 —— 池子是持续追加的，
    一行残缺不该让此前所有积累失效。
    """
    p = Path(path or DEFAULT_CORRECTIONS)
    if not p.exists():
        return []
    out: List[dict] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("[feedback] 回流池不可读: %s（%s）", p, e)
        return []
    for i, ln in enumerate(text.splitlines(), 1):
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            log.warning("[feedback] 回流池第 %d 行解析失败，跳过", i)
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def bucket_key(entry: Dict[str, Any]) -> Tuple[str, str]:
    """桶键 = `(profile_id, field)`。**单一实现**，`review_store` 转发至此。"""
    return (str(entry.get("profile_id") or ""), str(entry.get("field") or ""))


def is_usable(entry: Dict[str, Any]) -> bool:
    """条目可用性：wrong/right 非空且**不相等**（相等即无信息量）。"""
    w = str(entry.get("wrong") or "").strip()
    r = str(entry.get("right") or "").strip()
    return bool(w and r and w != r)


def pool_fingerprint(entries: Sequence[Dict[str, Any]]) -> str:
    """池指纹（前 12 位 sha1）—— 用于"这次 run 用的是哪一版池"可追溯。"""
    h = hashlib.sha1()
    for e in entries:
        h.update(("|".join([str(e.get("profile_id") or ""),
                            str(e.get("field") or ""),
                            str(e.get("wrong") or ""),
                            str(e.get("right") or "")])).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:12]


# ============================================
# 选样：profile 过滤 → 桶内取最新 → 桶间轮转 → 总量封顶
# ============================================
def select_for_prompt(entries: Sequence[Dict[str, Any]],
                      profile_id: str = "",
                      fields: Optional[Iterable[str]] = None,
                      bucket_cap: int = DEFAULT_BUCKET_CAP,
                      total_cap: int = DEFAULT_TOTAL_CAP) -> List[dict]:
    """从池中挑出**本页适用**的条目。

    - `profile_id` 非空时**只取同档案**的条目（不同文献类型的更正不可互串）；
      条目 profile_id 为空视为"通用"→ 保留（兼容早期无档案字段的条目）。
    - `fields` 给出时只取**该页会出现的字段**（提高精度，避免无关条目干扰）；
      `fields=None` 表示不按字段过滤。
    - 桶内按追加序**取最新**（列表尾优先）—— 后写的更正通常更贴近当前版式。
    - 桶间**轮转**取样，保证每个桶都有代表；总数不超过 `total_cap`。
    """
    want_fields = {str(f) for f in fields} if fields is not None else None

    buckets: Dict[Tuple[str, str], List[dict]] = {}
    for e in entries:
        if not is_usable(e):
            continue
        pid = str(e.get("profile_id") or "")
        if profile_id and pid and pid != str(profile_id):
            continue
        if want_fields is not None and str(e.get("field") or "") not in want_fields:
            continue
        buckets.setdefault(bucket_key(e), []).append(e)

    picked: List[dict] = []
    for k in buckets:
        buckets[k] = buckets[k][-int(bucket_cap):]      # 桶内取最新
        buckets[k] = list(reversed(buckets[k]))         # 供轮转：新 → 旧

    # 轮转：每轮各桶取一条，直到取满 total_cap 或各桶取尽
    depth, keys = 0, sorted(buckets)
    while len(picked) < int(total_cap):
        added = False
        for k in keys:
            if depth < len(buckets[k]):
                picked.append(buckets[k][depth])
                added = True
                if len(picked) >= int(total_cap):
                    break
        if not added:
            break
        depth += 1
    return picked


# ============================================
# 提示块构造
# ============================================
def _clean(s: Any) -> str:
    """压平空白 + 去掉引号类字符 + 截断。防畸形条目破坏 prompt 结构。"""
    t = "".join((" " if ch in "\r\n\t" else ch) for ch in str(s or ""))
    t = t.replace('"', "").replace("`", "").replace("：", ":").strip()
    while "  " in t:
        t = t.replace("  ", " ")
    return t[:MAX_TEXT_LEN]


def corrections_hint(entries: Sequence[Dict[str, Any]]) -> str:
    """条目 → prompt 片段。**空输入返回空串**（调用方据此保持 prompt 逐字不变）。"""
    rows = [e for e in entries if is_usable(e)]
    if not rows:
        return ""
    lines = []
    for e in rows:
        field = _clean(e.get("field"))[:MAX_FIELD_LEN] or "（未标字段）"
        wrong = _clean(e.get("wrong"))
        right = _clean(e.get("right"))
        if not right:
            continue
        lines.append(f"- {field}: 常见误读「{wrong}」→ 应为「{right}」"
                     if wrong else f"- {field}: 应为「{right}」")
    if not lines:
        return ""
    return HINT_HEADER + "：\n" + "\n".join(lines) + "\n" + HINT_FOOTER


def hint_meta(entries: Sequence[Dict[str, Any]],
              pool_total: int = 0) -> Dict[str, Any]:
    """注入元数据（**可见性**：run 产物据此可审计本次用了什么）。"""
    rows = [e for e in entries if is_usable(e)]
    buckets = sorted({f"{p or '∅'}::{f or '∅'}" for p, f in map(bucket_key, rows)})
    return {"applied": bool(rows), "n": len(rows), "buckets": buckets,
            "pool_total": int(pool_total), "fingerprint": pool_fingerprint(rows)}


def build_hint(base_hint: str = "",
               entries: Optional[Sequence[Dict[str, Any]]] = None
               ) -> Tuple[str, Dict[str, Any]]:
    """`(hint, meta)`。**池空 → `base_hint` 逐字返回**（零回归的落点）。"""
    rows = [e for e in (entries or ()) if is_usable(e)]
    block = corrections_hint(rows)
    if not block:
        return base_hint, {"applied": False, "n": 0, "buckets": [],
                           "pool_total": len(entries or ()), "fingerprint": ""}
    composed = (base_hint.rstrip() + "\n\n" + block) if base_hint.strip() else block
    return composed, hint_meta(rows, pool_total=len(entries or ()))


def load_for_page(path: Optional[Path] = None,
                  profile: Optional[Dict[str, Any]] = None,
                  bucket_cap: int = DEFAULT_BUCKET_CAP,
                  total_cap: int = DEFAULT_TOTAL_CAP,
                  include_adopted: bool = True,
                  ann_dir: Optional[Path] = None,
                  drafts_dir: Optional[Path] = None
                  ) -> Tuple[List[dict], Dict[str, Any]]:
    """便捷入口：读**两个源** → 按档案选样 → `(entries, meta)`。

    调用方（`two_stage` / `divergence_sampler` 的入口）用它一次，逐页复用。

    **零回归**：`adopted` 源为空（当前实况）时，合并结果与只读池**逐元素相同**，
    因而 prompt 逐字不变；`meta` 只多一个 `sources` 键（可见性，不改文本）。
    """
    pool = load_corrections(path)
    adopted: List[dict] = []
    astats: Dict[str, Any] = dict(_EMPTY_ADOPTED_STATS)
    if include_adopted:
        res = load_adopted(ann_dir=ann_dir, drafts_dir=drafts_dir)
        adopted = res["entries"]
        astats = res["stats"]
    merged = list(pool) + list(adopted)

    pid = str((profile or {}).get("profile_id") or "")
    fields = [a.get("name") if isinstance(a, dict) else a
              for a in ((profile or {}).get("attrs") or [])]
    fields = [f for f in fields if f]
    picked = select_for_prompt(merged, profile_id=pid,
                               fields=fields or None,
                               bucket_cap=bucket_cap, total_cap=total_cap)
    meta = hint_meta(picked, pool_total=len(merged))
    meta["sources"] = {"pool": len(pool), "adopted": len(adopted),
                       "adopted_stats": astats}
    return picked, meta


# ============================================================================
# 第二回流源：裁决采纳记录（2026-09-11）
# ============================================================================
# 为什么需要第二个源：`/review` 工作台回流的是**对账队列**里的更正（量小：
# 实测 3 条）；而 `/annotate` 裁决面板上「AI 提议 → 人工改对」的固化行
# （`source="ai_verified"`，P4 红线二"确认即固化"）是**另一条独立的更正流**，
# 此前没有任何代码消费它。
#
# 与写入端同源：定位与分段一律复用 `adjudicate.segments_of`，不另造一份切分口径
# —— 否则"面板上看到的 AI 原值"与"回流进 prompt 的 AI 原值"会静默分叉。

DEFAULT_ANN_DIR = BASE_DIR / "manual_annotations"
DEFAULT_DRAFTS_DIR = BASE_DIR / "data" / "preannotations"
SOURCE_AI_VERIFIED = "ai_verified"

_EMPTY_ADOPTED_STATS: Dict[str, Any] = {
    "n_adopted_rows": 0,     # 人工层里 source=ai_verified 的行数
    "n_edited": 0,          # 其中 ai_ref.edited=True 的行数（条目级标记，粗）
    "n_changed": 0,         # **逐行比较后真正有文本变化**的行数（= 产出条目数）
    "n_same": 0,            # edited=True 但该段文本未变 → 无信息
    "n_unresolved": 0,      # 草稿缺失 / 定位不到 / 取不到原值
    "n_files": 0,           # 扫过的金标准文件数
    "stems": [],            # 涉及的页（去重，供排查）
    "missing_drafts": [],   # 缺草稿的页
}


def _read_jsonl(path: Path) -> List[dict]:
    """容错读 jsonl（缺文件 / 坏行均不抛）。"""
    if not path.exists():
        return []
    out: List[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            out.append(o)
    return out


def _find_draft_entry(rows: Sequence[dict], ref: Dict[str, Any]) -> Optional[dict]:
    """按 `ai_ref` 定位草稿条目：**uid 优先、idx 兜底**（与 `adjudicate` 同口径）。

    `uid` 是几何指纹（M3 稳定标识），列分箱/阅读序变化导致 idx 漂移时仍能命中；
    旧草稿无 `row_uid` → 退回 `idx`（既有数据零迁移）。
    """
    ru = str(ref.get("uid") or "")
    if ru:
        for r in rows:
            if str(r.get("row_uid") or "") == ru:
                return r
    try:
        i = int(ref.get("idx"))
    except (TypeError, ValueError):
        return None
    return rows[i] if 0 <= i < len(rows) else None


def _draft_seg_text(entry: dict, seg: int) -> Optional[str]:
    """草稿条目第 `seg` 段的 AI 原值（走 `adjudicate.segments_of`，**单一实现**）。

    单段条目 → `segments_of` 返回一段，即整值；多段 → 逐段切分与落库同源。
    取不到（无框 / 段号越界）→ None（调用方计入 unresolved，**绝不猜**）。
    """
    import adjudicate                     # 延迟 import：本模块在识别链路里被 import
    segs = adjudicate.segments_of(entry)
    for s in segs:
        if int(s.get("seg", -1)) == int(seg):
            return str(s.get("text") or "").strip()
    if not segs and int(seg) == 0:        # 无框 → 整值即第一段
        return str(entry.get("text") or "").strip()
    return None


def load_adopted(ann_dir: Optional[Path] = None,
                 drafts_dir: Optional[Path] = None) -> Dict[str, Any]:
    """从**裁决采纳记录**提取回流条目（第二源）。

    返回 `{"entries": [...], "stats": {...}}`，条目形状与池子条目一致
    （`field/wrong/right/profile_id`），另带 `origin="adopted"` 与来源定位
    （`stem/idx/seg`）便于审计。

    **只取真正改过值的行**：`ai_ref.edited` 是**条目级**标记（多段条目各段共用），
    它说"这条被编辑过"，但没说是哪一段变了。故逐行比较「草稿该段 AI 原值 vs 行文本」，
    文本相同的行**无信息**，跳过 —— 这比依赖 `edited` 更准。
    """
    ad = Path(ann_dir or DEFAULT_ANN_DIR)
    dd = Path(drafts_dir or DEFAULT_DRAFTS_DIR)
    stats = json.loads(json.dumps(_EMPTY_ADOPTED_STATS))   # 深拷贝
    entries: List[dict] = []
    if not ad.is_dir():
        return {"entries": entries, "stats": stats}

    drafts_cache: Dict[str, Optional[List[dict]]] = {}
    for p in sorted(ad.glob("*.jsonl")):
        stats["n_files"] += 1
        for row in _read_jsonl(p):
            if str(row.get("source") or "") != SOURCE_AI_VERIFIED:
                continue
            stats["n_adopted_rows"] += 1
            ref = row.get("ai_ref")
            if not isinstance(ref, dict):
                stats["n_unresolved"] += 1
                continue
            if ref.get("edited"):
                stats["n_edited"] += 1

            stem = str(ref.get("stem") or "")
            if not stem:
                stats["n_unresolved"] += 1
                continue
            if stem not in drafts_cache:
                drafts_cache[stem] = _read_jsonl(dd / f"{stem}.ai.jsonl")
                if not drafts_cache[stem]:
                    stats["missing_drafts"].append(stem)
            rows = drafts_cache[stem]
            if not rows:
                stats["n_unresolved"] += 1
                continue

            entry = _find_draft_entry(rows, ref)
            if entry is None:
                stats["n_unresolved"] += 1
                continue
            try:
                seg = int(ref.get("seg") or 0)
            except (TypeError, ValueError):
                seg = 0
            ai_text = _draft_seg_text(entry, seg)
            human = str(row.get("text") or "").strip()
            if ai_text is None:
                stats["n_unresolved"] += 1
                continue
            if not human or human == ai_text:
                stats["n_same"] += 1
                continue

            entries.append({
                "field": str(row.get("attr") or ""),
                "wrong": ai_text,
                "right": human,
                "profile_id": str(row.get("profile")
                                  or entry.get("profile") or ""),
                "origin": "adopted",
                "stem": stem, "idx": ref.get("idx"), "seg": seg,
                "confidence": str(ref.get("confidence") or ""),
            })
            stats["n_changed"] += 1
            if stem not in stats["stems"]:
                stats["stems"].append(stem)

    entries = [e for e in entries if is_usable(e)]
    stats["n_changed"] = len(entries)
    return {"entries": entries, "stats": stats}


# ============================================================================
# 断链②：`rules_data/*.json` 无程序化写入
# ============================================================================
# 设计立场（红线三：契约不可静默过拟合）：
# **只产出候选、不自动改规则表。** 规则表一旦被自动改写，识别行为会**静默变化**，
# 且变化无法归因到某次改动。故本模块把"该改什么"变成**可见的候选清单**
# （`data/feedback/rule_candidates.json`），由人确认后再显式落盘
# （`apply_char_candidates`，且只增不改人工种子、自动备份）。

CANDIDATES_PATH = BASE_DIR / "data" / "feedback" / "rule_candidates.json"
RULES_DATA_DIR = BASE_DIR / "rules_data"


def _single_char_substitution(wrong: str, right: str) -> Optional[Tuple[str, str]]:
    """等长且**恰好一处**不同 → `(误识字, 正确字)`；否则 None。

    严格限定为单字替换，是因为只有这种形态能安全表达为
    `char_corrections` 的 `{误:正}` 字典；其余形态（增删字、多字串、
    繁简整段归一）语义更复杂，必须人工判断，不入表。
    """
    if not wrong or not right or len(wrong) != len(right):
        return None
    diffs = [(a, b) for a, b in zip(wrong, right) if a != b]
    if len(diffs) != 1:
        return None
    return diffs[0]


def mine_rule_candidates(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """回流条目 → `rules_data` 候选（**只生成，不落盘**）。

    产出三类：
    - `char_candidates`：可入 `char_corrections` 的字形替换（带支持数/来源字段）；
    - `conflicts`：同一误识字指向**不同**正确字 → 必须人工裁决，不可自动合并；
    - `manual_review`：长度变化 / 多字差异等，语义复杂，只能人工看。
    """
    agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
    manual: List[Dict[str, Any]] = []
    for e in entries:
        if not is_usable(e):
            continue
        w = str(e.get("wrong") or "").strip()
        r = str(e.get("right") or "").strip()
        sub = _single_char_substitution(w, r)
        if not sub:
            manual.append({"field": e.get("field"), "wrong": w, "right": r,
                           "reason": ("长度不同（增删字/繁简归一）"
                                      if len(w) != len(r) else "多处不同")})
            continue
        k = sub
        node = agg.setdefault(k, {"from": sub[0], "to": sub[1], "n": 0,
                                  "fields": [], "examples": []})
        node["n"] += 1
        if e.get("field") and e["field"] not in node["fields"]:
            node["fields"].append(e["field"])
        if len(node["examples"]) < 3:
            node["examples"].append(f"{w}→{r}")

    by_from: Dict[str, List[Dict[str, Any]]] = {}
    for node in agg.values():
        by_from.setdefault(node["from"], []).append(node)
    conflicts, clean = [], []
    for ch, nodes in sorted(by_from.items()):
        if len(nodes) > 1:
            conflicts.append({"from": ch,
                              "targets": sorted(n["to"] for n in nodes),
                              "note": "同一误识字指向多个正确字，须人工裁决"})
        else:
            clean.append(nodes[0])
    clean.sort(key=lambda n: (-n["n"], n["from"]))
    return {"n_pool": len(entries), "char_candidates": clean,
            "conflicts": conflicts, "manual_review": manual,
            "pool_fingerprint": pool_fingerprint(list(entries))}


def write_candidate_report(candidates: Dict[str, Any],
                           path: Optional[Path] = None) -> Path:
    """候选清单落盘（**只写报告，不碰 rules_data**）。"""
    import data_io
    p = Path(path or CANDIDATES_PATH)
    payload = {"_meta": {"说明": "回溯自校对回流池的规则表候选；本文件只是建议，"
                                 "确认后才由 apply_char_candidates 写入 rules_data",
                         "生成纪律": "只增不改人工种子；冲突项必须人工裁决"},
               "generated_at": _now(), **candidates}
    return data_io.atomic_write_json(p, payload)


def apply_char_candidates(candidates: Sequence[Dict[str, Any]],
                          data_path: Optional[Path] = None,
                          archive_dir: Optional[Path] = None) -> Dict[str, Any]:
    """把**无冲突**的字形候选写入 `rules_data/char_corrections.json`。

    守卫（任一不满足即跳过该条，并计入返回值的 `skipped`）：
    1. **只增不改**：`from` 已存在于表 → 一律不动（人工种子优先，绝不覆盖）；
    2. 跳过 `_` 前缀的元数据键；
    3. 写入前**备份**原文件（`_archive/rules_data_backup_<ts>.json`）；
    4. **原子写**（`data_io` 单一实现）。

    返回 `{"added": [...], "skipped": [...], "backup": path|None}`。
    """
    import data_io
    from datetime import datetime as _dt
    src = Path(data_path or (RULES_DATA_DIR / "char_corrections.json"))
    table = {}
    if src.exists():
        try:
            table = json.loads(src.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            table = {}
    if not isinstance(table, dict):
        table = {}

    added, skipped = [], []
    for c in candidates:
        f, t = str(c.get("from") or ""), str(c.get("to") or "")
        if not f or not t or f.startswith("_"):
            skipped.append({"from": f, "to": t, "reason": "非法键"})
            continue
        if f in table:
            skipped.append({"from": f, "to": t,
                            "reason": "已存在（人工种子优先，不覆盖）"})
            continue
        table[f] = t
        added.append({"from": f, "to": t})

    backup = None
    if added:
        arch = Path(archive_dir or (BASE_DIR / "_archive"))
        arch.mkdir(parents=True, exist_ok=True)
        if src.exists():
            backup = arch / (f"rules_data_backup_"
                             f"{_dt.now().strftime('%Y%m%d_%H%M%S')}"
                             f"_char_corrections.json")
            backup.write_text(src.read_text(encoding="utf-8"),
                              encoding="utf-8")
        data_io.atomic_write_json(src, table)
    return {"added": added, "skipped": skipped, "backup": backup,
            "path": src if added else None}


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI：`report`（只生成候选）/ `apply`（显式写规则表）。

    默认合并**两个回流源**（池 + 裁决采纳）—— 规则候选该看到全部更正。
    """
    import argparse
    ap = argparse.ArgumentParser(
        description="校对回流：候选清单生成 / 显式落盘规则表")
    ap.add_argument("cmd", choices=["report", "apply"],
                    help="report=只生成候选清单；apply=把无冲突字形候选写入 rules_data")
    ap.add_argument("--corrections", default=None, help="回流池路径")
    ap.add_argument("--ann-dir", default=None,
                    help=f"人工层目录（第二源；默认 {DEFAULT_ANN_DIR}）")
    ap.add_argument("--drafts-dir", default=None,
                    help=f"预标注草稿目录（第二源取 AI 原值；默认 {DEFAULT_DRAFTS_DIR}）")
    ap.add_argument("--no-adopted", action="store_true",
                    help="只用池（不读裁决采纳记录）")
    ap.add_argument("--out", default=None, help="候选清单输出路径")
    ap.add_argument("--rules", default=None, help="char_corrections.json 路径")
    ap.add_argument("--yes", action="store_true",
                    help="apply 必须显式确认（否则只打印将要写入的内容）")
    args = ap.parse_args(argv)

    pool = load_corrections(args.corrections)
    adopted: List[dict] = []
    astats: Dict[str, Any] = dict(_EMPTY_ADOPTED_STATS)
    if not args.no_adopted:
        res = load_adopted(ann_dir=args.ann_dir, drafts_dir=args.drafts_dir)
        adopted, astats = res["entries"], res["stats"]
    entries = list(pool) + list(adopted)
    cand = mine_rule_candidates(entries)
    cand["sources"] = {"pool": len(pool), "adopted": len(adopted),
                       "adopted_stats": astats}

    print(f"回流语料 {len(entries)} 条（池 {len(pool)} + 裁决采纳 {len(adopted)}）"
          f" → 字形候选 {len(cand['char_candidates'])} "
          f"/ 冲突 {len(cand['conflicts'])} / 需人工 {len(cand['manual_review'])}")
    if not args.no_adopted:
        print(f"  采纳源明细：人工层 {astats['n_files']} 文件 / "
              f"ai_verified 行 {astats['n_adopted_rows']} / "
              f"edited 标记 {astats['n_edited']} / "
              f"实际改动 {astats['n_changed']} / "
              f"未变 {astats['n_same']} / 无法定位 {astats['n_unresolved']}")
        if astats["missing_drafts"]:
            print(f"  ⚠ 缺草稿（AI 原值取不到）：{astats['missing_drafts']}")
        if not adopted:
            print("  · 采纳源当前 0 条 —— 需先在裁决面板（/annotate）采纳，"
                  "采纳行才会成为更正样本")
    for c in cand["char_candidates"]:
        print(f"  · {c['from']} → {c['to']}  （{c['n']} 次 / {','.join(c['fields'])}）")
    for c in cand["conflicts"]:
        print(f"  ⚠ 冲突 {c['from']} → {c['targets']}")
    for m in cand["manual_review"]:
        print(f"  · 需人工 {m['field']}: {m['wrong']} → {m['right']}（{m['reason']}）")

    if args.cmd == "report":
        print(f"候选清单: {write_candidate_report(cand, Path(args.out) if args.out else None)}")
        print("（未改动 rules_data —— 确认后跑 `apply --yes`）")
        return 0

    if not args.yes:
        print("\n未加 --yes：仅预览。将写入的无冲突候选见上。")
        return 0
    res = apply_char_candidates(cand["char_candidates"],
                               Path(args.rules) if args.rules else None)
    print(f"\n写入 {len(res['added'])} 条 → {res['path']}")
    for s in res["skipped"]:
        print(f"  跳过 {s['from']}→{s['to']}：{s['reason']}")
    if res["backup"]:
        print(f"  备份: {res['backup']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
