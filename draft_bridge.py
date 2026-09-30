# -*- coding: utf-8 -*-
"""draft_bridge —— 层 3 产物 → 裁决草稿（技能包「人面」缺的那一段，2026-09-16）。

## 它补的是哪个洞

用户 2026-09-16 实测提出的两个断点，指向同一个空洞：

  ① **人看不到 agent 标了什么** —— `chronicles exec` 的产物躺在
     `data/results/<stem>.result.json`，而它**没有任何消费方**：
     `/annotate` 与 `/review` 都读不到它。
  ② **人因此改不了、也就变不出新一轮金标准页** —— 裁决面读的是
     `data/preannotations/<stem>.ai.jsonl`，而那目录**只有界面点「生成」才写**
     （`preannotate_gen.generate`）。飞轮于是断在「层 3 产物 → 裁决草稿」这一格。

本模块就是那座桥：把**已落盘、已过 `seam.validate` 的产物**投影成裁决草稿，
人于是能在 `/annotate` 里看到 agent 的标注、逐条 accept/reject/改写，
采纳即落 `manual_annotations/`（`source="ai_verified"`）—— 飞轮重新转起来。

## 为什么不是「重新生成一遍草稿」

`preannotate_gen.generate()` 也能写草稿，但它的输入是「重读 `structured/` 再切分」，
与本桥的输入**不是同一份东西**。这里的关键在于：**人要验收的必须就是那份已验收的产物**。
若桥自己重跑一遍切分，`verify` 报绿的产物与人看到的东西就可能各说各话
—— 那是「验收对象被偷换」，比没有桥更坏。

## 实现口径：补回被投影丢掉的那一半

`plan_exec.run_page` 内部**本来就调了 `PA.anchor_page()`**（与
`preannotate_gen.one_page` 同一个函数），只是在把它压成产物时做了**有损投影**：

    entry(attr/text/box/boxes/confidence/row_uid/evidence/segments…)
      └─▶ attrs[] = {attr, text, confidence, box, boxes, lines}

丢掉的是 `row_uid` / `evidence` / `segments` —— 其中 `evidence.ocr_text`
正是裁决界面「**禁止盲签**」（红线二：必须能并排看到 OCR 原文）的落点，
`row_uid` 是裁决状态键（M3：跨重跑稳定）与「已采纳行」定位的依据。

⇒ 补法只有一条：**调回同一个 `PA.anchor_page`**（§60 单一实现），喂给它
从产物**原样重建**的 `records`（`{属性: 值}`，含空记录占位以保 `record_index` 对齐），
并复用执行器**同一份契约门面** `plan_exec.contract_facade(plan)`。
参数全同 ⇒ 产出逐字段等于当初那一次。

## 对账（本模块最要紧的一步）

补完之后**逐条与产物对账**（`attr / text / confidence / box / boxes / record_index`）。
任一条不一致 ⇒ **不落盘**并报出差异（退出码 4）。

理由：草稿是「人要裁决的那一份」，它必须**可证等价**于「已验收的那一份」。
这条对账就是那个证明。它同时兜住一类真实故障：产物落盘之后
`structured/` 或页图被改动过 —— 此时重算出来的几何已不是产物里那个，
两份都不该悄悄胜出，**必须报错让人来判断**。

## 纪律

- **不覆盖既有草稿**（除非 `--overwrite`）：草稿可能来自界面那条通道，
  两条通道谁覆盖谁都不该是默认行为。
- **覆盖前必留归档**（`--overwrite` 时）：覆盖是**本模块唯一的破坏性动作**，
  归档是它唯一的可逆通道；归档失败 ⇒ **拒绝覆盖**（fail closed），
  见 `_archive_existing`。归档位置由 `drafts_dir` 派生（沙箱演练自动跟着走沙箱）。
- **幂等**：同产物重复跑 → `unchanged`（按 `attr/text/box/confidence/row_uid`
  判等，不比 `ts`，因为 `ts` 每次都变而它不承载语义）。
- 落盘一律走 `preannotate.save_drafts`（原子写 + fsync，唯一实现）。
- **「新建」与「替换」分开报**：两者风险不同（前者安全、后者可能顶掉更好的那份），
  把 `n_written` 一个数当两者之和报出去，等于把破坏性动作藏进"新写"这个词里
  （2026-09-16 实测踩到：f7 报"新写 9 页"，实为替换 9 页）。
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

LOG = logging.getLogger("draft_bridge")

DEFAULT_RESULTS_DIR = ROOT / "data" / "results"
DEFAULT_DRAFTS_DIR = ROOT / "data" / "preannotations"
DEFAULT_PLANS_DIR = ROOT / "data" / "plans"
RESULT_SUFFIX = ".result.json"

#: 对账字段 —— 只取「产物里存了的、且人裁决直接依据的」那一组。
#: `row_uid` / `evidence` 不在其中：产物根本没存，无从对账（它们是桥补出来的，
#: 由 `anchor_page` 唯一决定，见模块 docstring）。
AGREE_TEXT_FIELDS = ("attr", "text", "confidence")
#: 框比较容差。产物里的坐标是 JSON 往返过的 float，逐位应相等；留极小容差
#: 只为不吃浮点表示法的亏，**不是**给几何漂移开后门（0.000001 px 无意义）。
BOX_TOL = 1e-6


# ============================================================
# 读入
# ============================================================
def plan_path(profile_id: str, plans_dir=None) -> Path:
    return Path(plans_dir or DEFAULT_PLANS_DIR) / f"plan_{profile_id}.json"


def load_plan(profile_id: str, plans_dir=None) -> dict:
    """读方案。**没有方案就没有契约门面**，而契约是锚定的依据 → 让调用方去处理缺料。"""
    p = plan_path(profile_id, plans_dir)
    if not p.exists():
        raise FileNotFoundError(str(p))
    d = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise ValueError(f"方案不是对象：{p}")
    return d


def result_path(stem: str, results_dir=None) -> Path:
    return Path(results_dir or DEFAULT_RESULTS_DIR) / f"{stem}{RESULT_SUFFIX}"


def load_result(stem: str, results_dir=None) -> Optional[dict]:
    p = result_path(stem, results_dir)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        LOG.warning("[draft_bridge] 产物读取失败 %s: %s", p.name, e)
        return None
    return d if isinstance(d, dict) else None


def resolve_stems(profile_id: str, plan: dict, *, stem: str = "",
                  pages: Optional[Sequence[str]] = None, project: str = "",
                  results_dir=None) -> Tuple[List[str], str]:
    """`(页名列表, page_source)`。

    显式姿势（`--stem` / `--pages` / `--project`）**复用 `plan_exec.resolve_stems`**
    —— 页集合解析全项目只此一处，本模块不另写一份（§60）。

    默认姿势是本桥特有的：**扫产物目录**（`data/results/*.result.json` 里
    `profile_id` 等于本档案的那些）。**刻意不沿用** `plan_exec` 的默认
    （`plan.source.pages` = 方案编译时的金标准页）—— 桥的对象是「已经跑出来的
    这批新页的产物」，不是金标准页。
    """
    if stem or pages or project:
        import plan_exec as PE
        return PE.resolve_stems(plan, stem=stem, pages=pages, project=project)

    d = Path(results_dir or DEFAULT_RESULTS_DIR)
    stems: List[str] = []
    if d.is_dir():
        for p in sorted(d.glob(f"*{RESULT_SUFFIX}")):
            r = load_result(p.name[: -len(RESULT_SUFFIX)], results_dir)
            if r is None:
                continue
            if str(r.get("profile_id") or "") != str(profile_id or ""):
                continue
            stems.append(r.get("stem") or p.name[: -len(RESULT_SUFFIX)])
    return stems, f"results_dir:{profile_id}"


# ============================================================
# 产物 → records（anchor_page 的输入）
# ============================================================
def records_from_result(result: dict) -> List[dict]:
    """产物 `records[].attrs[]` → `[{属性名: 值}]`（`anchor_page` 的 records）。

    ★ **空记录必须留占位**（空 dict）：`evidence.record_index` = 记录在
      `records` 里的**序号**。产物里 `records` 与执行器的 `segs` 一一对应
      （`plan_exec` 对每个 seg 都 `add_record`，值为空的记录也在），
      若重建时把空记录滤掉，序号整体前移 ⇒ 对账必挂、分组也会错。

    ★ **`跨页` 不重建**：`anchor_page` 遍历键时本就排除它
      （`k not in ("跨页",)`），`_record_cuts` 也算长度时排除 —— 两边一致。
    """
    out: List[dict] = []
    for rec in (result.get("records") or []):
        d: Dict[str, str] = {}
        if isinstance(rec, dict):
            for a in (rec.get("attrs") or []):
                if not isinstance(a, dict):
                    continue
                name = str(a.get("attr") or "")
                if name and name != "跨页":
                    d[name] = str(a.get("text") or "")
        out.append(d)
    return out


def _flatten_expected(result: dict) -> List[dict]:
    """产物里的条目按**同一顺序**摊平（每条附上它所在记录序号）。"""
    out: List[dict] = []
    for ridx, rec in enumerate(result.get("records") or []):
        if not isinstance(rec, dict):
            continue
        for a in (rec.get("attrs") or []):
            if isinstance(a, dict):
                out.append({"record_index": ridx, **a})
    return out


# ============================================================
# 补回 row_uid / evidence / segments
# ============================================================
def build_entries(stem: str, result: dict, plan: dict, *,
                  structured_dir=None, outbox_dir=None,
                  image_name: Optional[str] = None) -> Tuple[List[dict], dict]:
    """重建条目：**调回 `PA.anchor_page`**（与执行器同一个函数、同一份门面）。

    `image_name` 缺省时由调用方按真实图名给出（见 `bridge_stem`）；不给则
    `anchor_page` 沿用 `f"{stem}.png"`（与执行器逐字一致）。
    """
    import preannotate as PA
    import plan_exec as PE

    records = records_from_result(result)
    facade = PE.contract_facade(plan)
    pid = str(result.get("profile_id") or plan.get("profile_id") or "")
    if pid:
        facade["profile_id"] = pid
    r = PA.anchor_page(records, stem, facade,
                       structured_dir=structured_dir, outbox_dir=outbox_dir,
                       image_name=image_name)
    return list(r.get("entries") or []), {"n_records": len(records)}


def apply_rules(entries: List[dict], records: List[dict], profile_id: str) -> dict:
    """疑错层（飞轮第四段的入口）—— 与界面通道**同源**：
    `preannotate_gen.rule_flags` + `apply_flags`（§60，不另写一份）。

    ★★ **必须在对账之后调用**：命中 SUSPECT 会把条目的 `confidence` 下调为 `low`
      并挂 `evidence.rule`，而对账是拿 `confidence` 与**产物**比 —— 先挂规则会让
      对账假挂（产物里没有规则层，执行器不跑规则）。
      顺序错了就会把"完全一致的页面"报成"对不上"。

    ★ 为什么桥要做这件事：界面（`annotate.html`）会渲染 `evidence.rule`
      为「规则疑错」标签，而 `anchor_page` **不产生**该字段（那是
      `preannotate_gen` 的 `apply_flags` 干的）。不补这一层，产物来源的草稿
      就看不到"系统自己怀疑这里读错"—— 那正是飞轮把人引向待核页面的信号。
      补上之后，两条通道在草稿层的**字段面**等价。

    失败一律降级（返回 `rules_applied=False`）—— 疑错是增益，不是必需，
    **不能因为它挂了就让人看不到产物**。
    """
    try:
        import preannotate_gen as PG
        from profile_store import get_profile
        prof = get_profile(profile_id)
        if not prof:
            return {"rules_applied": False, "reason": "profile_not_found"}
        flags = PG.rule_flags(prof, records)
        n = PG.apply_flags(entries, flags) if flags else 0
        return {"rules_applied": True, "n_flagged_records": len(flags), "n_suspect": n}
    except Exception as e:                                 # pragma: no cover
        LOG.warning("[draft_bridge] 规则层不可用（降级）: %s", e)
        return {"rules_applied": False, "reason": f"{type(e).__name__}: {e}"}


# ============================================================
# 对账
# ============================================================
def _box_diff(a, b, tol: float = BOX_TOL) -> Optional[str]:
    if (a is None) != (b is None):
        return "box 有无不一致"
    if a is None:
        return None
    if len(a) != len(b):
        return "box 维度不一致"
    if any(abs(float(u) - float(v)) > tol for u, v in zip(a, b)):
        return "box 坐标不一致"
    return None


def reconcile(entries: List[dict], expected: List[dict],
              *, tol: float = BOX_TOL, limit: int = 20) -> List[dict]:
    """重建条目 vs 产物条目，逐条对账。返回差异清单（空 = 完全一致）。"""
    diffs: List[dict] = []
    if len(entries) != len(expected):
        diffs.append({"kind": "count", "entries": len(entries), "result": len(expected)})
        return diffs
    for i, (e, x) in enumerate(zip(entries, expected)):
        ri_e = int(((e.get("evidence") or {}).get("record_index")) or 0)
        ri_x = int(x.get("record_index") or 0)
        if ri_e != ri_x:
            diffs.append({"kind": "record_index", "i": i, "attr": x.get("attr"),
                          "entries": ri_e, "result": ri_x})
        for f in AGREE_TEXT_FIELDS:
            if str(e.get(f) or "") != str(x.get(f) or ""):
                diffs.append({"kind": f, "i": i, "attr": x.get("attr"),
                              "entries": e.get(f), "result": x.get(f)})
        msg = _box_diff(e.get("box"), x.get("box"), tol)
        if msg:
            diffs.append({"kind": "box", "i": i, "attr": x.get("attr"),
                          "entries": e.get("box"), "result": x.get("box"),
                          "why": msg})
        eb, xb = e.get("boxes"), x.get("boxes")
        if len(eb or []) != len(xb or []):
            diffs.append({"kind": "boxes", "i": i, "attr": x.get("attr"),
                          "entries": len(eb or []), "result": len(xb or []),
                          "why": "分段数不一致"})
        else:
            for k, (u, v) in enumerate(zip(eb or [], xb or [])):
                m2 = _box_diff(u, v, tol)
                if m2:
                    diffs.append({"kind": "boxes", "i": i, "attr": x.get("attr"),
                                  "seg": k, "why": m2})
        if len(diffs) >= limit:
            break
    return diffs


# ============================================================
# 草稿等值判定（幂等 / 冲突）
# ============================================================
def _fingerprint(entries: Sequence[dict]) -> List[tuple]:
    """条目的**语义指纹** —— 不含 `ts`（每次都变、不承载语义），
    含 `row_uid`（M3：跨重跑稳定的几何标识）。"""
    out: List[tuple] = []
    for e in entries or []:
        out.append((str(e.get("attr") or ""), str(e.get("text") or ""),
                    tuple(round(float(v), 4) for v in (e.get("box") or [])),
                    str(e.get("confidence") or ""),
                    str(e.get("row_uid") or "")))
    return out


def compare_fingerprints(old: Sequence[dict], new: Sequence[dict]) -> dict:
    """两版草稿的**粗粒度差异摘要** —— 让 `existing_differs` 不是一句无解的谜。

    为什么需要它（2026-09-16 实测）：既有草稿与产物不一致有三种完全不同的成因 ——
      ① 只有 `confidence` 不同（两条通道用了不同契约源的 `char_metrics`）；
      ② 条目集合不同（既有草稿来自**另一次**生成）；
      ③ 既有草稿**更多**（界面通道有 L1/L2 兜底，比零 token 执行器读得更全）。
    三者对人的处置完全不同（①可忽略 / ②该换 / ③**千万别换**），
    所以摘要要能分辨它们，而不是只报一个"不一致"。
    """
    fo = [tuple(x[:2]) for x in _fingerprint(old)]        # (attr, text) —— 内容身份
    fn = [tuple(x[:2]) for x in _fingerprint(new)]
    so, sn = set(fo), set(fn)
    conf_o = {x[:2]: x[3] for x in _fingerprint(old)}
    conf_n = {x[:2]: x[3] for x in _fingerprint(new)}
    box_o = {x[:2]: x[2] for x in _fingerprint(old)}
    box_n = {x[:2]: x[2] for x in _fingerprint(new)}
    return {
        "n_old": len(old), "n_new": len(new),
        "only_in_draft": len(so - sn),                     # 旧草稿独有 → ③ 的征兆
        "only_in_result": len(sn - so),                    # 产物独有 → ② 的征兆
        "confidence_differs": sum(1 for k in (so & sn)
                                  if conf_o.get(k) != conf_n.get(k)),
        "box_differs": sum(1 for k in (so & sn)
                           if box_o.get(k) != box_n.get(k)),
        "sample_only_in_draft": [list(k) for k in list(so - sn)[:3]],
        "sample_confidence_pairs": [
            {"attr": k[0], "text": k[1], "draft": conf_o.get(k), "result": conf_n.get(k)}
            for k in list(so & sn) if conf_o.get(k) != conf_n.get(k)][:3],
    }


def draft_path(stem: str, drafts_dir=None) -> Path:
    """草稿文件路径 —— **与写入侧 `preannotate.save_drafts` 同一口径**（`data_io.safe_name`）。

    ★ 为什么必须有这个函数：本模块原先把"读既有草稿"写成裸 `f"{stem}.ai.jsonl"`，
      而写入侧走 `safe_name(stem)`。两者对**含空格/特殊字符**的 stem 会指向
      **不同文件** ⇒ 读不到既有草稿 → 判定为"新建" → 直接写到 safe 名上，
      **静默顶掉一份真实存在的草稿**（正是"补缺不覆盖"这道纪律要防的事，
      却从路径口径这一侧漏了过去）。本项目的 stem 恰好净化前后同名，
      所以它一直是**潜伏**的；换成真实文件名带空格就会发作。
    """
    import data_io
    return Path(drafts_dir or DEFAULT_DRAFTS_DIR) / f"{data_io.safe_name(stem)}.ai.jsonl"


def load_draft_entries(stem: str, drafts_dir=None) -> List[dict]:
    """读既有草稿（**只读**；口径与 `adjudicate.load_drafts` 同源，但不需要 idx）。"""
    p = draft_path(stem, drafts_dir)
    if not p.exists():
        return []
    out: List[dict] = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
    except OSError as e:                                   # pragma: no cover
        LOG.warning("[draft_bridge] 既有草稿读取失败 %s: %s", p.name, e)
    return out


#: 被覆盖草稿的归档目录名（相对 `drafts_dir` 所在项目的 `_archive/`）。
DRAFT_ARCHIVE_NAME = "draft_superseded"


def draft_archive_dir(drafts_dir=None) -> Path:
    """归档落点的**默认**推导 = 由 `drafts_dir` 派生，不写死 `ROOT/_archive`。

    推导假设项目布局 `<项目根>/data/<草稿目录>`（本项目的真实布局）：
    `data/preannotations` → `<项目根>/_archive/<DRAFT_ARCHIVE_NAME>/`。
    这样演练把 `drafts_dir` 指到沙箱时，归档**自动跟着进沙箱**，
    不会因为"归档"这个看似无害的副作用而往真实 `_archive/` 里丢文件。

    ⚠ 浅层目录（如 `tmp/drafts`）会推到 `tmp/../_archive` —— 落在调用方沙箱**外**。
      故所有入口都接受显式 `archive_dir=`；**测试/演练一律显式给**，
      不要依赖推导（推导只为「人直接跑 CLI」这一条路径服务）。
    """
    d = Path(drafts_dir or DEFAULT_DRAFTS_DIR).resolve()
    return d.parent.parent / "_archive" / DRAFT_ARCHIVE_NAME


def _archive_existing(stem: str, drafts_dir=None,
                      archive_dir=None) -> Optional[Path]:
    """把**即将被覆盖**的草稿留一份副本；无原文 → `None`。

    ★ **归档不成即拒绝覆盖**（fail closed）：`--overwrite` 是人显式要求的
      **破坏性**动作，归档是它唯一的可逆通道。归档失败还照覆盖，
      等于把人明确要求的"可回退"悄悄降级成不可回退 —— 那比不覆盖更坏。

    ★ 副本**校验后才返回**（大小一致），不留半截归档。
    """
    src = draft_path(stem, drafts_dir)
    if not src.exists():
        return None
    dst_dir = Path(archive_dir) if archive_dir else draft_archive_dir(drafts_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{Path(src.name).stem}.{ts}"
    dst = dst_dir / f"{base}.bak"
    # ★ 同一秒内对同一页归档两次（连跑两次命令就会）⇒ 不能撞名。
    #   撞名会让后一次**覆盖掉前一次的归档**，而被顶掉的那份恰是更早、
    #   更原始的版本 —— 归档的全部意义就是留住它。故顺延编号。
    n = 1
    while dst.exists():
        n += 1
        dst = dst_dir / f"{base}.{n}.bak"
    shutil.copy2(src, dst)
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        raise OSError(f"归档校验失败（副本不完整）：{dst}")
    return dst


# ============================================================
# 单页 / 批量
# ============================================================
def bridge_stem(stem: str, plan: dict, *, results_dir=None, drafts_dir=None,
                structured_dir=None, outbox_dir=None, archive_dir=None,
                overwrite: bool = False, dry_run: bool = False) -> dict:
    """一页：产物 → 草稿。**不抛异常**（逐页隔离，落 `status` + `error`）。"""
    rec: Dict[str, Any] = {"stem": stem}
    import config as CONFIG

    res = load_result(stem, results_dir)
    if res is None:
        rec["status"] = "no_result"
        rec["error"] = f"没有产物：{result_path(stem, results_dir)}"
        rec["next"] = "先跑 `chronicles exec --profile <档案> --pages <页目录>`。"
        return rec

    rec["result_file"] = str(result_path(stem, results_dir))
    rec["tier"] = res.get("tier")
    rec["page_source"] = res.get("page_source")

    # 页图名以**真实文件**为准：执行器固定写 `<stem>.png`，但 `import` 保留原名
    # （可能是 .jpg）—— 照抄 `<stem>.png` 会让 `/annotate/<image_name>` 直接 404，
    # 而这条链的尽头就是「人打不开页面」。图真找不到时退回旧口径并如实标记。
    img = CONFIG.image_path_of(stem, given=outbox_dir)
    image_name = Path(img).name if img is not None else f"{stem}.png"
    rec["image_name"] = image_name
    rec["image_found"] = img is not None

    try:
        entries, st = build_entries(stem, res, plan,
                                    structured_dir=structured_dir,
                                    outbox_dir=outbox_dir,
                                    image_name=image_name)
    except Exception as e:                                 # 逐页隔离
        rec["status"] = "error"
        rec["error"] = f"锚定重建失败：{type(e).__name__}: {e}"
        return rec

    expected = _flatten_expected(res)
    rec["n_expected"] = len(expected)
    rec["n_entries"] = len(entries)
    rec["n_records"] = st.get("n_records")
    if not entries:
        rec["status"] = "empty"
        rec["error"] = "产物里没有条目（该页 0 值）—— 无可裁决内容"
        return rec

    # ★ 对账必须在挂规则层**之前**（见 `apply_rules` docstring）
    diffs = reconcile(entries, expected)
    rec["n_diffs"] = len(diffs)
    if diffs:
        rec["status"] = "mismatch"
        rec["diffs"] = diffs
        rec["error"] = ("重建条目与产物对不上 —— 已**拒绝落盘**"
                        "（产物落盘后 structured/ 或页图被改动过？）")
        rec["next"] = ("核对差异；若确属产物过期，重跑 `chronicles exec` 后再桥。"
                       "两份都不该悄悄胜出，故此处不猜。")
        return rec

    # 疑错层（对账通过之后）—— 会给命中 SUSPECT 的条目挂 `evidence.rule` 并下调置信
    rec.update(apply_rules(entries, records_from_result(res),
                           str(res.get("profile_id") or plan.get("profile_id") or "")))

    # ★★ 默认「**补缺不覆盖**」（2026-09-16 实测裁定）。
    #   理由不是我保守，而是实测发现**既有草稿可能比产物更好**：
    #   界面那条通道（`preannotate_gen`）有 L1/L2 兜底，实测在
    # 在一页金标准页上比零 token 执行器**多读出一条公司名**。
    #   若默认以产物为准覆盖，等于**静默销毁更好的那份**。
    #   反过来，既有草稿也可能是**上一次生成**留下的陈旧版本（实测 24 条 vs 10 条，
    #   内容几乎无关）—— 那种又确实该换。**两种情形从文件本身分不出来**，
    #   所以默认两边都不动，只如实报出差异摘要，把决定权交回人（`--overwrite`）。
    old = load_draft_entries(stem, drafts_dir)
    if old:
        rec["existing_draft"] = str(draft_path(stem, drafts_dir))
        rec["diff_summary"] = compare_fingerprints(old, entries)
        if _fingerprint(old) == _fingerprint(entries):
            rec["status"] = "unchanged"
            rec["n_entries"] = len(entries)
            return rec
        if not overwrite:
            rec["status"] = "existing_differs"
            d = rec["diff_summary"]
            rec["note"] = (
                f"已有草稿与产物不一致（草稿 {d['n_old']} 条 / 产物 {d['n_new']} 条；"
                f"草稿独有 {d['only_in_draft']}、产物独有 {d['only_in_result']}、"
                f"仅置信度不同 {d['confidence_differs']}）—— **默认不覆盖**。")
            rec["next"] = ("草稿独有 > 0 说明它可能来自带 L1/L2 兜底的界面通道（更全）→ "
                           "**先看一眼再决定**；确认要用产物版本替换，加 `--overwrite`。")
            return rec

    if dry_run:
        rec["status"] = "dry_run"
        return rec

    # ★ 覆盖前先归档（`old` 非空 = 这次落盘是**替换**，不是新建）。
    #   归档失败 ⇒ **不覆盖**，如实报 error。见 `_archive_existing`。
    backup: Optional[Path] = None
    if old:
        try:
            backup = _archive_existing(stem, drafts_dir, archive_dir)
        except Exception as e:
            rec["status"] = "error"
            rec["error"] = f"覆盖前归档失败，已放弃覆盖（原草稿未动）：{type(e).__name__}: {e}"
            rec["next"] = ("检查草稿目录同级的 `_archive/` 是否可写，再重试；"
                           "只想先看差异可用 `--dry-run`。")
            return rec

    try:
        import preannotate as PA
        p = PA.save_drafts(entries, stem, drafts_dir=drafts_dir)
    except Exception as e:
        rec["status"] = "error"
        rec["error"] = f"草稿落盘失败：{type(e).__name__}: {e}"
        return rec
    rec["status"] = "written"
    rec["draft_file"] = str(p)
    # ★ 「新建」与「替换」必须分开表达：两者风险不同，混成一个 `written`
    #   会把破坏性动作藏进"新写"这个词里（2026-09-16 实测踩到）。
    rec["overwritten"] = bool(old)
    if backup is not None:
        rec["backup"] = str(backup)
    return rec


def bridge_stems(stems: Sequence[str], plan: dict, *, profile_id: str = "",
                 results_dir=None, drafts_dir=None, structured_dir=None,
                 outbox_dir=None, archive_dir=None, overwrite: bool = False,
                 dry_run: bool = False) -> dict:
    pages = [bridge_stem(s, plan, results_dir=results_dir, drafts_dir=drafts_dir,
                         structured_dir=structured_dir, outbox_dir=outbox_dir,
                         archive_dir=archive_dir,
                         overwrite=overwrite, dry_run=dry_run)
             for s in (stems or [])]
    from collections import Counter
    cnt = Counter(p.get("status") for p in pages)
    written = [p for p in pages if p.get("status") == "written"]
    out = {
        "ok": not any(p.get("status") in ("no_result", "mismatch",
                                          "empty", "error") for p in pages),
        "profile_id": profile_id,
        "n_pages": len(pages),
        # ★ `n_written` = 落盘了的页数（含替换）；`n_created` / `n_replaced` 把它拆开。
        #   风险不对称：新建不会伤到任何既有物，替换会顶掉一份既有草稿。
        #   报数的人要能一眼分开，否则「新写 9 页」会掩盖「替换了 9 页」。
        "n_written": cnt.get("written", 0),
        "n_created": sum(1 for p in written if not p.get("overwritten")),
        "n_replaced": sum(1 for p in written if p.get("overwritten")),
        "n_unchanged": cnt.get("unchanged", 0),
        # ★ 有草稿但与产物不一致 —— **不是失败**：那一页本来就可裁决，
        #   只是裁决的不是本产物的版本。计数单独给，别混进 n_skipped 里被忽略。
        "n_existing_differs": cnt.get("existing_differs", 0),
        "n_skipped": cnt.get("no_result", 0) + cnt.get("empty", 0),
        "status": dict(cnt),
        "pages": pages,
        "drafts_dir": str(Path(drafts_dir or DEFAULT_DRAFTS_DIR)),
    }
    out["exit_code"] = draft_exit_code(out)
    return out


# ============================================================
# 退出码（**唯一判定点** —— `draft_exit_code`，chronicles 侧不重判）
# 词表正本在 `cli_contract`（2026-09-24 收敛）；语境文案在 `draft_exit_code`
# 的 docstring 与人读输出里，不再各抄一份 EXIT_MEANING。
# ============================================================
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_MISSING,             # noqa: E402
                          EXIT_INVALID, EXIT_MEANING)                       # noqa: E402


def draft_exit_code(res: dict) -> int:
    """与全项目六档契约同族。

    ★ 「一页产物都没有」不是成功 —— 那是**缺料 3**（下一步是去跑 exec），
      而「对账不过」是**校验不过 4**（下一步是查差异 / 重跑 exec）。
      两者下一步动作相反，绝不能并成一个码。

    ★ `existing_differs` **不算失败**：本命令的职责是「让每一页产物都可裁决」，
      而那一页**已经有草稿**（来自另一条通道）⇒ 职责已达成。它只在
      `pages[].note` / `diff_summary` 里被如实报出，供人决定要不要 `--overwrite`。
    """
    pages = res.get("pages") or []
    if not pages:
        return EXIT_MISSING
    st = {p.get("status") for p in pages}
    if st == {"no_result"}:
        return EXIT_MISSING
    if "mismatch" in st:
        return EXIT_INVALID
    if st & {"no_result", "empty", "error"}:
        return EXIT_INTERNAL
    return EXIT_OK


def advice_for(res: dict) -> str:
    pages = res.get("pages") or []
    if not pages:
        return ("没有找到产物 —— 先跑 `chronicles exec --profile <档案> --pages <页目录>`，"
                "或核对 `--results-dir`。")
    n = res.get("n_written", 0)
    same = res.get("n_unchanged", 0)
    dif = res.get("n_existing_differs", 0)
    cre = res.get("n_created", n)
    rep = res.get("n_replaced", 0)
    # 「新建」与「替换」分开说：替换是本命令唯一的破坏性动作，必须显式可见。
    what = f"新建 {cre} 页"
    if rep:
        what += (f" / **替换既有草稿 {rep} 页**（原文已归档，见各页 `backup`）")
    tail = (f"⚠ 另有 {dif} 页已有草稿但与产物不一致（默认保留）—— 逐页见 `diff_summary`，"
            f"「草稿独有 > 0」的多半是带 L1/L2 兜底的界面版（更全），**别急着覆盖**。"
            if dif else "")
    if rep:
        tail += (f"⚠ 本次替换了 {rep} 页既有草稿 —— 若其中有带 L1/L2 兜底的界面版，"
                 f"请到 `_archive/{DRAFT_ARCHIVE_NAME}/` 取回（`backup` 字段给了路径）。")
    if res.get("exit_code") == EXIT_OK:
        return (f"草稿就绪（{what} / 与既有草稿一致 {same} 页）→ "
                f"去界面逐条裁决：/annotate/<页图名>；采纳即落 manual_annotations/。" + tail)
    bad = [p for p in pages if p.get("status") not in
           ("written", "unchanged", "existing_differs")]
    return (f"有 {len(bad)} 页没桥成（{', '.join(sorted({p.get('status') for p in bad}))}）"
            f"—— 逐页原因见 pages[].error。" + tail)


def format_human(res: dict) -> str:
    rc = int(res.get("exit_code", draft_exit_code(res)))
    L: List[str] = []
    L.append("=" * 64)
    L.append(f"draft_bridge · 产物 → 裁决草稿 · 退出码 {rc}（{EXIT_MEANING.get(rc, rc)}）")
    L.append("=" * 64)
    L.append(f"  档案        : {res.get('profile_id')}")
    L.append(f"  草稿目录    : {res.get('drafts_dir')}")
    L.append(f"  页数        : {res.get('n_pages')}  "
             f"（新建 {res.get('n_created')} / 替换既有 {res.get('n_replaced')} / "
             f"一致 {res.get('n_unchanged')} / "
             f"有草稿但不一致 {res.get('n_existing_differs')}）")
    for p in (res.get("pages") or []):
        mark = {"written": "✓", "unchanged": "=", "existing_differs": "≠",
                "dry_run": "·"}.get(p.get("status"), "✗")
        tag = p.get("status") or ""
        if tag == "written" and p.get("overwritten"):
            tag = "written·替换"
        L.append(f"    {mark} {p.get('stem')}  [{tag}] "
                 f"条目 {p.get('n_entries', 0)}/{p.get('n_expected', 0)}  "
                 f"{p.get('error') or ''}")
        if p.get("backup"):
            L.append(f"        · 原文已归档 {p['backup']}")
        d = p.get("diff_summary")
        if d:
            L.append(f"        · 草稿 {d['n_old']} 条 / 产物 {d['n_new']} 条  "
                     f"草稿独有 {d['only_in_draft']} 产物独有 {d['only_in_result']} "
                     f"仅置信度不同 {d['confidence_differs']} 框不同 {d['box_differs']}")
        for x in (p.get("diffs") or [])[:3]:
            L.append(f"        · 对账差异 {x}")
    L.append(f"  → {advice_for(res)}")
    return "\n".join(L)


# ============================================================
# 人面入口（exec / draft 共用）
# ============================================================
def base_url() -> str:
    """标注面的基址。默认取 `config.SKILL_FLASK_PORT`（**5001**），可用环境变量覆盖。

    ★ **2026-09-30 统一裁定**：命令面（`next` / `ui` / 本模块的地址生成）**只认
      技能包自有交互面**（5001）——与桌面壳后端（app.py，5000）完全解耦：
      本机与分发包行为一致，壳开不开都不影响这里的地址。
      环境变量 `CHRONICLES_BASE_URL` 仍可整体覆盖。
    ★ 服务实际在哪个端口由 `ui_cli.live_url()` 探测得出（探 5001）；
      起服务由唯一实现 `ui_cli.serve` 负责。
    """
    env = (__import__("os").environ.get("CHRONICLES_BASE_URL") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        import config as CONFIG
        return f"http://{CONFIG.FLASK_HOST}:{CONFIG.SKILL_FLASK_PORT}"
    except Exception:                                      # pragma: no cover
        return "http://127.0.0.1:5001"


def annotate_url(stem: str, outbox_dir=None, base: Optional[str] = None) -> str:
    """该页的**裁决入口** URL（`/annotate/<页图名>`）。

    图名以真实文件为准（同 `bridge_stem`）—— 页图是 `.jpg` 时给 `.png` 会 404。
    """
    import config as CONFIG
    img = CONFIG.image_path_of(stem, given=outbox_dir)
    name = Path(img).name if img is not None else f"{stem}.png"
    return f"{base or base_url()}/annotate/{name}"


def annotate_urls(stems: Sequence[str], outbox_dir=None, base: Optional[str] = None,
                  limit: int = 20) -> List[str]:
    b = base or base_url()
    return [annotate_url(s, outbox_dir=outbox_dir, base=b)
            for s in list(stems or [])[:limit]]


# ============================================================
# 抽样（2026-09-26 SK-2）：跑批后随机抽 n 页交人复核
# ============================================================
def gold_exists(stem: str, *, gold_dir=None, outbox_dir=None) -> bool:
    """该页是否已有金标准标注。

    文件名口径与标注写入侧**同一实现**（`adjudicate.manual_path`，
    挂在**图名**上）；图名以真实文件为准（同 `bridge_stem`，`.jpg` 给 `.png` 会错位）。
    """
    import adjudicate as A
    import config as CONFIG
    img = CONFIG.image_path_of(stem, given=outbox_dir)
    image_name = Path(img).name if img is not None else f"{stem}.png"
    return A.manual_path(image_name, gold_dir).exists()


def sample_stems(*, n: int = 3, seed: Optional[int] = None,
                 results_dir=None, gold_dir=None, outbox_dir=None) -> dict:
    """从跑批产物池随机抽 n 页（七步流程第 7 步的抽样口）。

    池 = `results_dir` 里**有产物**的页；**排除已有金标准**的页 ——
    复核名额不花在人已验证过的页上（金标准页自己的产物也因此天然出局）。

    ★ seed 缺省时现场生成并**如实回报**：同一次抽选由此可复现、可审计；
      汇报里带着 seed，人对抽选有异议可以换 seed 重抽或核验。
    ★ 抽选纯随机、**不做置信分层**：分层会把样本引向低置信页，
      估计出的整批通过率会系统性偏低 —— 抽样的目的是估计，分层留给人主动要求。
    ★ 池 < n 时把池全给（`n_picked < n` 如实报）；"缺料"只有一种：
      一页可抽的都没有 ⇒ 退出码 3（下一步：先跑 `exec`，或本批已全量人工验证）。
    """
    import os
    import random

    pool: List[str] = []
    excluded: List[str] = []
    d = Path(results_dir or DEFAULT_RESULTS_DIR)
    if d.is_dir():
        for p in sorted(d.glob(f"*{RESULT_SUFFIX}")):
            r = load_result(p.name[: -len(RESULT_SUFFIX)], results_dir)
            if r is None:
                continue
            stem = str(r.get("stem") or p.name[: -len(RESULT_SUFFIX)])
            if gold_exists(stem, gold_dir=gold_dir, outbox_dir=outbox_dir):
                excluded.append(stem)
            else:
                pool.append(stem)
    eff_seed = int.from_bytes(os.urandom(4), "big") if seed is None else int(seed)
    picked = sorted(random.Random(eff_seed).sample(sorted(pool), min(n, len(pool))),
                    ) if pool else []
    out: Dict[str, Any] = {
        "ok": bool(picked),
        "n_requested": int(n),
        "n_picked": len(picked),
        "pool_size": len(pool),
        "n_excluded_gold": len(excluded),
        "excluded_gold": sorted(excluded),
        "picked": picked,
        "seed": eff_seed,
        "results_dir": str(d),
    }
    out["exit_code"] = sample_exit_code(out)
    return out


def sample_exit_code(res: dict) -> int:
    """唯一判定点（chronicles 侧不重判）。抽样是纯选择，只涉及两档：
    0 = 抽到了；3 = 池空 —— 下一步去跑 `exec`，或本批已全量人工验证。"""
    return EXIT_OK if res.get("picked") else EXIT_MISSING
