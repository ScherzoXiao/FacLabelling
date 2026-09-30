# -*- coding: utf-8 -*-
"""页级分诊（preflight）：**在启动飞轮之前**回答「这一批页像本档案吗？」

## 它解决什么问题

飞轮两个入口都要**逐页付真实代价**：
- `POST /api/flywheel/run` → 本地视觉模型逐页直读（本机无 CUDA，每页 1–5 分钟）；
- `POST /api/preannotations/generate` → `auto` 档读不出的页会兜底线上模型（按页计费）。

用户把**别的题材的页**放进同一栏目（例如把账册/公函混进企业注册名录），
整轮飞轮就低效甚至无意义 —— 而且往往**跑完之后**才发现。
本模块把这件事提前到「按下按钮之前」，回答只需**毫秒级**。

## 判据：锚词命中数（实测取证，2026-09-12）

| 判据 | 名录族 26 页 | 非名录 26 页 | 结论 |
|---|---|---|---|
| `anchor_hits` | **9 – 73** | **0 – 1** | ✅ 干净分离 |
| `anchor_per_100c` | 4.93 – 14.74 | 0.00 – 0.62 | ✅ 8× 分离 |
| `n_lines` / `short_ratio` | 12–21 行 / 0.06–0.47 | **5–140 行 / 0–0.79** | ❌ **重叠** |

**为什么不用版面几何**：某张剪贴板导入页有 140 行、短行率 0.755 ——
从几何形态看比样例页还「像名录」。几何判据在这里是**误导性**的。

**为什么不用 L0 的 `fill_rate`**（它分离度也好）：要跑 L0、要落产物。
本模块只读、不写、不跑切分 ⇒ 零 token、零产物、零副作用。

阈值 `MIN_ANCHOR_HITS = 3` **由数据推**：名录下界 9、非名录上界 1，两侧各留 ≥3× 余量。

## 取文本必须与管线同口径（实测教训）

`page_lines`（`preannotate`）是管线**唯一**的取行入口，本模块直接复用 ——
**不另造快路径**。实测：直接用 `structured` 的 `L2_lines` 原始文本，会把 2 页判反
（`l2_reconstructed` 存量页在列流重建时经过 `LC.normalize_text` 折叠 + 按列重组），
即「分诊说行、管线读不出」。同一口径 > 少 0.1 ms。

速度不是问题：`page_lines` 实测 **≈0.4 ms/页**（52 页共 23 ms）。

## 读法纪律

- `anchor_hits` 是**必要信号、不是充分信号**：它只回答「这页有没有本档案的属性锚词」，
  **不回答**「切得对不对」——那是 L0 `fill_rate` 的活，属**跑批之后**的 QA。
- **不可测 ≠ 放错**：无文本（尚未 OCR / OCR 未产出正文）、档案无可用锚词 ⇒
  `unmeasurable`，**绝不判 `suspect`**（与 `qa_metrics` 的「不可测 ≠ 对位率为 0」同一条纪律）。
- `suspect` 是**建议复核**，不是判决：本模块只提醒，不阻断（是否放行由调用方与用户决定）。

## 命令面（技能包第 1 步，2026-09-14）

```
python page_triage.py --profile <id> --inbox <dir> [--json] [--allow-suspect]
python page_triage.py --profile <id> --project <id>
```

⚠ **`--profile` 是必需的**：分诊判据 = 锚词命中数，而锚词来自**档案**
（`rule_split.build_spec(rule_learn.load(pid), profile)`）。所以本模块回答的是
「这批页像不像**这个档案**」，**不是**「这批页像不像名录」这种绝对问题。
设计稿 §4.2 的草图 `triage --inbox <dir>` 少了这一项，以本文件为准。

⚠ **「不阻断」指的是内核，不是命令面** —— 两者默认相反，别当成矛盾：

| 面 | 默认 | 为什么 |
|---|---|---|
| 内核 `triage_pages` / `preflight_gate` | 只提醒（`verdict=warn`），`blocked` 由调用方决定 | 内核**不替调用方做产品决定** |
| 命令面 `page_triage.py` / `chronicles triage` | **面外即拒答（退出码 2）** | 设计稿 §7 第 1 步判据；面外硬跑 = 花钱买垃圾 |

`--allow-suspect` 是两者的接口：显式放行 → 退出码 0，**但清单仍照出 suspect**
（放行不等于看不见）。这与 `preflight_gate(allow_suspect=...)` 是**同一个语义**。

退出码 **0 全部面内 / 2 面外拒答 / 3 缺料 / 1 内部错误 / 64 用法错误**，
唯一判定点在 `triage_exit_code()`（`chronicles triage` 复用它，不重判）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from rule_split import norm_stream

# ---- 阈值：由数据推（见模块 docstring 的分离度表）----
MIN_ANCHOR_HITS = 3        # 名录 [9,73] vs 非名录 [0,1] → 取 3（两侧各 ≥3× 余量）
MIN_CHARS = 20             # 少于此字符数无判断依据（不判 suspect，判 unmeasurable）

VERDICT_OK = "ok"
VERDICT_SUSPECT = "suspect"
VERDICT_UNMEASURABLE = "unmeasurable"

READ_DISCIPLINE = (
    "anchor_hits 是必要信号、不是充分信号：只答「这页有没有本档案的属性锚词」，"
    "不答「切得对不对」（那是跑批后 L0 fill_rate 的事）。"
    "不可测 ≠ 放错：无文本或档案无锚词 ⇒ unmeasurable，不判 suspect。"
    "suspect 是建议复核、不是判决 —— 本模块只提醒，不阻断。"
)


# ============================================================
# 判据
# ============================================================
def count_anchors(text: str, spec: dict) -> Dict[str, object]:
    """页文本 → 锚词命中数。

    `spec` 来自 `rule_split.build_spec`（**单一实现**，不另造锚词表）。
    归一化用 `rule_split.norm_stream`（定位口径：繁简折叠 + 去空白，与原文 1:1 等长）——
    这里只做**计数**，取值不经此路，故不违反「取值保原字形」。
    """
    anchors = (spec or {}).get("anchors") or {}
    ns = norm_stream(text or "")
    hits, types = 0, 0
    per_attr: Dict[str, int] = {}
    for attr, rx_list in anchors.items():
        n = 0
        for rx, _lbl in rx_list:
            try:
                n += len(rx.findall(ns))
            except re.error:                      # pragma: no cover - 防御性
                continue
        if n:
            types += 1
            per_attr[attr] = n
        hits += n
    return {"hits": hits, "types": types, "n_chars_norm": len(ns),
            "per_attr": per_attr}


# ============================================================
# 单页 / 整批
# ============================================================
def triage_page(stem: str, spec: dict, *,
                structured_dir: Optional[Path] = None,
                outbox_dir: Optional[Path] = None,
                min_anchor_hits: int = MIN_ANCHOR_HITS,
                min_chars: int = MIN_CHARS,
                has_anchors: bool = True) -> dict:
    """单页分诊。只读，不写任何文件。"""
    import preannotate as PA

    rec: dict = {"stem": stem}
    if not has_anchors:
        # 档案本身没有可用锚词 → 整批都不可判（不是页的问题）
        rec.update({"verdict": VERDICT_UNMEASURABLE,
                    "reason": "档案没有可用属性锚词，无法分诊",
                    "anchor_hits": None, "anchor_types": None,
                    "anchor_per_100c": None, "n_lines": None, "n_chars": None})
        return rec

    try:
        lines = PA.page_lines(stem, structured_dir, outbox_dir)
    except Exception as e:                        # 读取异常不算「放错」
        rec.update({"verdict": VERDICT_UNMEASURABLE,
                    "reason": f"读取失败: {e}",
                    "anchor_hits": None, "anchor_types": None,
                    "anchor_per_100c": None, "n_lines": None, "n_chars": None})
        return rec

    texts = [(l or {}).get("text") or "" for l in lines]
    texts = [t for t in texts if t.strip()]
    text = "".join(texts)
    n_chars = len(text)

    if n_chars < min_chars:
        rec.update({"verdict": VERDICT_UNMEASURABLE,
                    "reason": ("没有可判文本（可能尚未 OCR，或 OCR 未产出正文）"
                               if n_chars == 0 else
                               f"文本过短（{n_chars} 字 < {min_chars}），不足以判断"),
                    "anchor_hits": None, "anchor_types": None,
                    "anchor_per_100c": None, "n_lines": len(texts),
                    "n_chars": n_chars})
        return rec

    c = count_anchors(text, spec)
    hits = int(c["hits"])
    verdict = VERDICT_OK if hits >= min_anchor_hits else VERDICT_SUSPECT
    rec.update({
        "verdict": verdict,
        "reason": (f"锚词命中 {hits} 次（≥{min_anchor_hits}）"
                   if verdict == VERDICT_OK else
                   f"锚词命中仅 {hits} 次（<{min_anchor_hits}）—— 可能不属于本档案"),
        "anchor_hits": hits,
        "anchor_types": int(c["types"]),
        "anchor_per_100c": round(hits * 100 / max(1, int(c["n_chars_norm"])), 3),
        "n_lines": len(texts),
        "n_chars": n_chars,
        "per_attr": c["per_attr"],
    })
    return rec


def triage_pages(stems: Iterable[str], profile_id: str, *,
                 structured_dir: Optional[Path] = None,
                 outbox_dir: Optional[Path] = None,
                 min_anchor_hits: int = MIN_ANCHOR_HITS,
                 min_chars: int = MIN_CHARS) -> dict:
    """整批分诊。零 token、零产物、只读。

    返回 `{ok, profile_id, n_pages, n_ok, n_suspect, n_unmeasurable, verdict,
            suspects, unmeasurable, pages, thresholds, elapsed_ms, advice, _read}`。
    """
    import rule_learn as RL
    import rule_split as RS
    from profile_store import get_profile

    t0 = time.perf_counter()
    stems = [str(s) for s in stems if str(s).strip()]
    profile = get_profile(profile_id)
    if not profile:
        return {"ok": False, "error": f"档案不存在: {profile_id}",
                "profile_id": profile_id, "pages": []}

    spec = RS.build_spec(RL.load(profile_id), profile)
    has_anchors = bool(spec.get("anchors"))

    pages: List[dict] = []
    for stem in stems:
        pages.append(triage_page(stem, spec,
                                 structured_dir=structured_dir,
                                 outbox_dir=outbox_dir,
                                 min_anchor_hits=min_anchor_hits,
                                 min_chars=min_chars,
                                 has_anchors=has_anchors))

    suspects = [p for p in pages if p["verdict"] == VERDICT_SUSPECT]
    unmeas = [p for p in pages if p["verdict"] == VERDICT_UNMEASURABLE]
    n_ok = len(pages) - len(suspects) - len(unmeas)

    if not has_anchors:
        verdict = "unknown"
        advice = ("该档案没有可用的属性锚词，无法分诊；"
                  "请先完善档案属性（或先做例题让系统归纳锚词）。")
    elif suspects:
        verdict = "warn"
        lo = min((p["anchor_hits"] or 0) for p in suspects)
        hi = max((p["anchor_hits"] or 0) for p in suspects)
        advice = (f"这 {len(suspects)} 页看起来不属于本档案"
                  f"（锚词命中 {lo}–{hi} 次；本档案的页通常 ≥{min_anchor_hits} 次）。"
                  "建议先把它们移到对应栏目，再启动飞轮。")
    elif unmeas:
        verdict = "warn"
        advice = (f"这 {len(unmeas)} 页没有可判文本（尚未 OCR 或未产出正文），"
                  "分诊对它们不生效。")
    else:
        verdict = "ok"
        advice = f"全部 {len(pages)} 页都像本档案，可以放心启动飞轮。"

    return {
        "ok": True,
        "profile_id": profile_id,
        "n_pages": len(pages),
        "n_ok": n_ok,
        "n_suspect": len(suspects),
        "n_unmeasurable": len(unmeas),
        "verdict": verdict,
        "suspects": suspects,
        "unmeasurable": unmeas,
        "pages": pages,
        "thresholds": {"min_anchor_hits": min_anchor_hits,
                       "min_chars": min_chars},
        "anchors_available": has_anchors,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "advice": advice,
        "_read": READ_DISCIPLINE,
    }


def triage_project(project_id: str, profile_id: str, **kw) -> dict:
    """按**栏目**取页再分诊（飞轮 / 预标注两个入口都从栏目取页）。"""
    import data_io

    stems = [Path(s).stem for s in data_io.get_project_image_stems(project_id)]
    out = triage_pages(stems, profile_id, **kw)
    out["project_id"] = project_id
    return out


# ============================================================
# preflight 闸门（供 app.py 的两个昂贵入口复用）
# ============================================================
def preflight_gate(stems: Iterable[str], profile_id: str, *,
                   allow_suspect: bool = False, **kw) -> dict:
    """跑批前的闸门。

    `allow_suspect=False`（默认）且存在 `suspect` → `blocked=True`，
    调用方应回 409 并把 `triage` 交给用户确认；用户确认后带
    `allow_suspect=True` 再发一次即可放行。

    **只在「真看着不像」时挡**：`unmeasurable` 不挡（不可测 ≠ 放错）；
    分诊自身失败也不挡（分诊是增值，不是关卡）。
    """
    t = triage_pages(stems, profile_id, **kw)
    if not t.get("ok"):
        return {"blocked": False, "triage": t, "reason": t.get("error") or ""}
    blocked = bool(t["n_suspect"]) and not allow_suspect
    return {"blocked": blocked, "triage": t,
            "reason": t["advice"] if blocked else ""}


def filter_ok(stems: Iterable[str], triage: dict) -> List[str]:
    """从分诊结果里挑出「像本档案」的页（用户选「只处理匹配页」时用）。"""
    bad = {p["stem"] for p in (triage.get("suspects") or [])}
    return [s for s in stems if s not in bad]


# ============================================================
# CLI（技能包命令面 · 第 1 步）—— 退出码契约的**唯一实现**
# ============================================================
# 退出码与 `chronicles.py` 的契约同形（设计稿 §4.2）：
#   0   全部面内（可以继续）
#   2   **面外拒答**（有 suspect）—— 这是**正确行为**，不是错误
#   3   缺料（档案不存在 / 无页可判 / 全部 unmeasurable 待 OCR）
#   1   内部错误
#   64  用法错误
#
# ⚠ 为什么必须有 64：argparse 的默认用法错误码**恰好是 2**，会与「面外拒答」撞车。
#   二者下一步动作完全相反（改参数 vs 换材料），混在一起 agent 就无法自纠。
#   这与 `chronicles.py` 是**同一条约定**（不是重复逻辑）。
#
# ⚠ 退出码**只在本模块判定一次**（`triage_exit_code`）：`chronicles triage` 取用
#   同一个函数，绝不重判 —— 否则同一批页会有两个码（§60 单一实现纪律）。
# 词表（常量 + EXIT_MEANING）正本在 `cli_contract`（2026-09-24 收敛，此前 10 份抄写）。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_OUT_OF_SCOPE,      # noqa: E402
                          EXIT_MISSING, EXIT_USAGE, EXIT_MEANING)         # noqa: E402

# 视为「一页」的图像后缀（stem 与 structured 产物对齐；改了这里要同步改文档）
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"})


def _stems_from_inbox(inbox) -> List[str]:
    """目录 → 页 stem（只取图像，按名排序）。目录不存在/非目录 → `[]`（由调用方判缺料）。"""
    d = Path(inbox)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def triage_exit_code(result: dict) -> int:
    """分诊结果 → 退出码（**唯一实现**；`chronicles triage` 复用，不另判）。

    ★ 三种「非成功」必须分清，且**只留一条 MISSING 出口**：
      - **输入为空**（inbox 空 / 目录不存在）→ 缺料 3，**不是「全部通过」**。
        内核在 `stems=[]` 时给 `verdict="ok"`、`advice="全部 0 页都像本档案"`
        —— 它假设调用方已经给了页。照抄它会让 agent 把「一个页都没读到」
        当成「检查通过」，这是最危险的假成功形态。
      - 全部 `unmeasurable` → 缺料 3（不可测 ≠ 放错，但也不能说通过；下一步是先 OCR）。
      - 档案无可用锚词 → 缺料 3（不是页的问题，是档案还没建好）。

    后两种与「输入为空」在**行为上等价**（下一步动作都是"去补东西"），
    故**合并成同一条出口**：凡「没有一页面内、也没有一页面外」即缺料。

    ★ 为什么要合并（本函数的第一版踩过）：第一版另写了
      `if n_pages <= 0: return EXIT_MISSING` 作为「空输入」的显式提前判断，
      结果它与末尾兜底**完全等价** —— 证伪时拆掉任一条、另一条照样挡，
      变异测试**抓不住**。互相掩护的冗余 = **不可验代码**，故收敛为单点判断。
      行为一字未变（六种输入逐一核对过），但此后每一行都可被证伪。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return EXIT_MISSING
    if not result.get("anchors_available", True):
        return EXIT_MISSING
    if int(result.get("n_suspect") or 0) > 0:
        return EXIT_OUT_OF_SCOPE       # 面外是最具体的结论，优先
    if int(result.get("n_ok") or 0) > 0:
        return EXIT_OK
    # 没有面内页、也没有面外页 = 一页都没读到 / 读到了但全不可测 → 缺料
    return EXIT_MISSING


def run_triage(*, profile: str, inbox=None, project_id=None,
               structured_dir=None, outbox_dir=None,
               allow_suspect: bool = False) -> dict:
    """**分诊的唯一实现**：参数 → 结果字典（含 `exit_code`）。

    两个 CLI 都只**取用**它：`main()` 打印后返回 `exit_code`；
    `chronicles triage` 组装统一 JSON 后返回**同一个**值 ⇒ 退出码只有一处判定。

    `allow_suspect` 只改**退出码**（显式放行 = 调用方已确认要处理面外页），
    **不改分诊读数**（`suspects` 清单照出 —— 放行不等于看不见）。
    """
    if inbox:
        stems = _stems_from_inbox(inbox)
        source: Dict[str, object] = {"kind": "inbox", "inbox": str(inbox)}
    elif project_id:
        import data_io
        stems = [Path(s).stem for s in data_io.get_project_image_stems(project_id)]
        source = {"kind": "project", "project": project_id}
    else:
        return {"ok": False, "exit_code": EXIT_MISSING,
                "error": "缺少页来源：给 --inbox <dir> 或 --project <id>",
                "profile_id": profile, "pages": [], "n_pages": 0,
                "source": {"kind": None}}

    kw: Dict[str, object] = {}
    if structured_dir:
        kw["structured_dir"] = Path(structured_dir)
    if outbox_dir:
        kw["outbox_dir"] = Path(outbox_dir)

    if project_id and not inbox:
        t = dict(triage_project(project_id, profile, **kw))
    else:
        t = dict(triage_pages(stems, profile, **kw))

    t["source"] = source
    t["allow_suspect"] = bool(allow_suspect)
    if allow_suspect and t.get("ok") and int(t.get("n_suspect") or 0) > 0:
        t["exit_code"] = EXIT_OK
        t["override"] = "allow_suspect"
    else:
        t["exit_code"] = triage_exit_code(t)
    return t


def advice_for(result: dict) -> str:
    """「下一步怎么办」——报错可执行（设计稿 §4.2）。"""
    rc = int(result.get("exit_code", EXIT_INTERNAL))
    pid = result.get("profile_id") or "<profile>"
    if rc == EXIT_OK:
        if result.get("allow_suspect") and result.get("override"):
            return (f"已显式放行（清单里仍有 {result.get('n_suspect')} 页面外）。"
                    f"继续：`chronicles facts --profile {pid}`")
        return f"继续：`chronicles facts --profile {pid}`"
    if rc == EXIT_OUT_OF_SCOPE:
        return ("把这批页移出本栏目（它们不像这个档案），或确认要处理时加 `--allow-suspect` 显式放行；"
                "若这些页本身就该是另一类材料，先为它建独立档案再分诊。")
    if rc == EXIT_MISSING:
        if not result.get("ok"):
            return (f"核对档案 id（见 data/collection_profiles/），或确认页来源"
                    f"（--inbox 目录是否存在、里面是否有图像）。")
        if not result.get("anchors_available", True):
            return "该档案还没有可用锚词：先在界面里补档案属性、或做几页例题让系统归纳锚词。"
        if int(result.get("n_pages") or 0) <= 0:
            return "一个页都没读到：核对 --inbox 目录（是否有 .png/.jpg 等图像）或 --project 的栏目成员。"
        return "这些页还没有可判文本（未 OCR / OCR 未产出正文）：先跑 OCR 通道再分诊。"
    return "内部错误：看 stderr 详情；这属不该发生的情况。"


class _Parser(argparse.ArgumentParser):
    """用法错误 → 64（argparse 默认 2 会与「面外拒答」撞车）。与 chronicles 同一约定。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"\npage_triage: 用法错误：{message}\n")
        raise SystemExit(EXIT_USAGE)


def format_human(t: dict) -> str:
    out: List[str] = []
    out.append("=" * 62)
    out.append("页级分诊（只读 · 零 token · 零产物）")
    out.append("=" * 62)
    if not t.get("ok"):
        out.append(f"  !! {t.get('error')}")
        out.append(f"  → {advice_for(t)}")
        return "\n".join(out)
    src = t.get("source") or {}
    out.append(f"  档案      : {t.get('profile_id')}")
    out.append(f"  页来源    : {src.get('kind')} "
               f"{src.get('inbox') or src.get('project') or ''}")
    out.append(f"  判据      : anchor_hits ≥ {t['thresholds']['min_anchor_hits']}"
               f"（本页属性锚词命中数）")
    out.append(f"  读数      : {t['n_pages']} 页 = 面内 {t['n_ok']}"
               f" + 面外 {t['n_suspect']} + 不可测 {t['n_unmeasurable']}"
               f"  ({t.get('elapsed_ms')} ms)")
    out.append(f"  结论      : {t.get('verdict')}")
    out.append("")
    out.append(f"  {t.get('advice')}")
    sus = t.get("suspects") or []
    if sus:
        out.append("")
        out.append(f"  面外页（{len(sus)}）：")
        for p in sus[:15]:
            out.append(f"     {p['stem'][:56]:<58} hits={p.get('anchor_hits')}")
        if len(sus) > 15:
            out.append(f"     ... 另 {len(sus) - 15} 页")
    unm = t.get("unmeasurable") or []
    if unm:
        out.append("")
        out.append(f"  不可测页（{len(unm)}，不判 suspect —— 不可测 ≠ 放错）：")
        for p in unm[:8]:
            out.append(f"     {p['stem'][:56]:<58} {p.get('reason')}")
        if len(unm) > 8:
            out.append(f"     ... 另 {len(unm) - 8} 页")
    out.append("")
    out.append(f"  → {advice_for(t)}")
    out.append(f"  [退出码 {t.get('exit_code')} "
               f"{EXIT_MEANING.get(int(t.get('exit_code') or 0), '')}]")
    out.append(f"  {READ_DISCIPLINE}")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="page_triage",
        description="页级分诊：判断一批页是否像**指定档案**（只读、零 token、零产物）",
        epilog="退出码：0 全部面内 · 2 面外拒答 · 3 缺料 · 1 内部错误 · 64 用法错误",
    )
    ap.add_argument("--profile", required=True,
                    help="档案 id（prof_...）—— 锚词来源，分诊判据由它决定")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inbox", default=None, help="待分诊的页目录（扫图像后缀）")
    g.add_argument("--project", dest="project_id", default=None, help="按栏目取页（项目 id）")
    ap.add_argument("--structured-dir", default=None, help="默认 data/structured")
    ap.add_argument("--outbox", default=None, help="默认 <项目根>/outbox")
    ap.add_argument("--allow-suspect", action="store_true",
                    help="显式放行面外页（退出码 0；清单仍照出 suspect）")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="结构化输出到 stdout（人读格式改道 stderr）")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(list(argv) if argv is not None else None)
    t = run_triage(profile=a.profile, inbox=a.inbox, project_id=a.project_id,
                   structured_dir=a.structured_dir, outbox_dir=a.outbox,
                   allow_suspect=a.allow_suspect)
    rc = int(t.get("exit_code", EXIT_INTERNAL))
    if a.as_json:
        print(json.dumps(t, ensure_ascii=False, indent=2))
        print(format_human(t), file=sys.stderr)
    else:
        print(format_human(t))
    return rc


if __name__ == "__main__":
    sys.exit(main())
