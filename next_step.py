# -*- coding: utf-8 -*-
"""next_step —— 「现在该做什么、在哪做」的只读导航（2026-09-16）。

**为什么单独成模块**（用户 2026-09-16 的追问，原文）：

    「如果我现在换一组材料，换成县志材料，从一开始就没有金标准页，
      那我要在哪里创造金标准页？」

这句话点出了一个**入口缺口**：整套管线里唯一必须由人在界面上完成的那一步
（创造金标准页 / 验收草稿），在命令面里**没有任何东西指向它** ——
`ocr` 跑完之后该打开哪个页面、`exec` 之后草稿在哪看，全靠人自己知道。

本模块补的就是这一格：**只读地看一眼「每页卡在哪一步」，然后把入口地址给出来。**

★ 三条纪律
    ① **只读**：不落盘、不改任何状态。判定全部基于「文件在不在」，不做推断。
    ② **不主动起常驻服务**（用户 2026-09-16 裁定②）：只**探测**服务在不在，
       不在就说明怎么起 —— 起不起、什么时候起，由人决定。
    ③ **不另造口径**：URL 生成复用 `draft_bridge.base_url/annotate_url`；
       金标准路径复用 `adjudicate.manual_path`；文件名净化复用 `data_io.safe_name`
       （`CLAUDE.md` §60 单一实现）。

★ 判定的四档（互斥，按此优先级）
    needs_ocr          有图，但 `data/structured/<stem>.json` 还没有 → 去跑 `ocr`
    needs_annotate     有 OCR，没有草稿、也没有金标准       → **去界面标**（本模块的主用途）
    needs_adjudicate   有草稿（`data/preannotations/`）      → **去界面裁决**
    done               已有金标准（`manual_annotations/`）  → 该 `learn` 了

    ⚠ 这四档是「**下一步该干什么**」的口径，**不是质量口径** ——
      「有草稿」不等于「草稿对」，「有金标准」不等于「金标准全」。本模块不判对错。
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_GOLD_DIR = ROOT / "manual_annotations"
DEFAULT_DRAFTS_DIR = ROOT / "data" / "preannotations"
DEFAULT_STRUCTURED_DIR = ROOT / "data" / "structured"
DEFAULT_RESULTS_DIR = ROOT / "data" / "results"
DEFAULT_LEARNED_DIR = ROOT / "rules_data"

# ---- 四档状态（值即文案，直接可读）----
STEP_NEEDS_OCR = "needs_ocr"
STEP_NEEDS_ANNOTATE = "needs_annotate"
STEP_NEEDS_ADJUDICATE = "needs_adjudicate"
STEP_DONE = "done"

STEP_ORDER: Tuple[str, ...] = (
    STEP_NEEDS_OCR, STEP_NEEDS_ANNOTATE, STEP_NEEDS_ADJUDICATE, STEP_DONE)

STEP_LABEL = {
    STEP_NEEDS_OCR: "待 OCR",
    STEP_NEEDS_ANNOTATE: "待你标注",
    STEP_NEEDS_ADJUDICATE: "待你裁决草稿",
    STEP_DONE: "已有金标准",
}

# 该档对应的动作名（`next_action` 的取值）
ACTION_FOR_STEP = {
    STEP_NEEDS_OCR: "ocr",
    STEP_NEEDS_ANNOTATE: "annotate",
    STEP_NEEDS_ADJUDICATE: "adjudicate",
    STEP_DONE: "learn",
}

# 判定优先级：越靠前越"卡在前面"
_PRIORITY = (STEP_NEEDS_OCR, STEP_NEEDS_ANNOTATE,
             STEP_NEEDS_ADJUDICATE, STEP_DONE)

# 词表正本在 `cli_contract`（2026-09-24 收敛）；本模块只用 0/3 两档。
from cli_contract import EXIT_OK, EXIT_MISSING, EXIT_MEANING                # noqa: E402


# ============================================================
# 路径口径（全部转发既有单一实现，不另造）
# ============================================================
def gold_path(image_name: str, gold_dir=None) -> Path:
    """金标准页路径 —— 转发 `adjudicate.manual_path`（**与界面/命令面同一份口径**）。

    用 `image_name`（带扩展名）而非 stem：这是 `manual_path` 的既有签名，
    换算成 `manual_annotations/<safe_name>.jsonl` 的逻辑在那一边，这边不复制。
    """
    import adjudicate as A
    return Path(A.manual_path(image_name, gold_dir or DEFAULT_GOLD_DIR))


def draft_path(stem: str, drafts_dir=None) -> Path:
    """草稿路径 `data/preannotations/<safe_name(stem)>.ai.jsonl`。

    净化走 `data_io.safe_name` —— 与写入侧 `preannotate.save_drafts` 同一口径。
    """
    import data_io
    return Path(drafts_dir or DEFAULT_DRAFTS_DIR) / f"{data_io.safe_name(stem)}.ai.jsonl"


def structured_path(stem: str, structured_dir=None) -> Path:
    return Path(structured_dir or DEFAULT_STRUCTURED_DIR) / f"{stem}.json"


def image_path(stem: str, outbox_dir=None):
    """页图路径 —— 转发 `config.image_path_of`（OUTBOX→INBOX 两处查，单一实现）。"""
    import config as CONFIG
    return CONFIG.image_path_of(stem, given=outbox_dir)


# ============================================================
# 服务探测（**本函数只探测**；起服务在 `ui_cli`）
# ============================================================
def server_up(base: Optional[str] = None, timeout: float = 0.6) -> bool:
    """界面服务在不在？—— 纯 TCP 连一下，**本函数不做任何启动动作**。

    ★ 裁定变更（2026-09-16 同日修订）：旧裁定「给地址，不替人起服务」**已作废**。
      用户原话：「①G1：同意加 `chronicles ui [--serve]`，且老裁定已经明显阻碍到了
      用户对产品的顺利体验，我决定修改该裁定，**允许在运行管线中的必要节点起服务**。」

      ⇒ **起服务现在是被允许的**，但**唯一实现落点**是 `ui_cli.serve`
        （它拉起 `app.py` 本身，不复制 Flask 逻辑）。本函数**保持"纯探测"语义不变**：
        它的调用方用它判断"**要不要复用已在跑的那个**" —— 那正是最不该混进
        启动动作的地方（混进去就会在"检查状态"时顺手拉起第二个实例）。

    ⚠ 探测要**连候选端口一起探**（5000 与回落 5001）—— 只看主端口会把
      "跑在 5001 的服务"判成没起，进而重复起第二个。见 `ui_cli.candidate_urls`。
    """
    try:
        import draft_bridge as DB
        b = base or DB.base_url()
    except Exception:                                                # pragma: no cover
        b = base or "http://127.0.0.1:5000"
    try:
        hostport = b.split("//", 1)[-1].split("/", 1)[0]
        host, _, port = hostport.partition(":")
        if not port:
            port = "80"
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((host or "127.0.0.1", int(port)))
            return True
        finally:
            s.close()
    except Exception:                                                # pragma: no cover
        return False


# ============================================================
# 枚举页
# ============================================================
def _iter_images(outbox_dir=None, inbox_dir=None) -> List[Tuple[str, str]]:
    """`[(image_name, source)]` —— inbox + outbox，与 `app.py /api/all_images` 同口径。

    `source` 取 `"inbox"` / `"outbox"`，仅供人读时分辨；**判定不看它**。
    """
    import config as CONFIG
    exts = tuple(str(e).lower() for e in getattr(CONFIG, "IMAGE_EXTS", (".png", ".jpg")))
    out: List[Tuple[str, str]] = []
    seen = set()
    for d, tag in ((Path(inbox_dir or CONFIG.INBOX), "inbox"),
                   (Path(outbox_dir or CONFIG.OUTBOX), "outbox")):
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if p.name in seen:            # 同名同时在两处时，以先见的为准
                continue
            seen.add(p.name)
            out.append((p.name, tag))
    return out


def classify_page(image_name: str, *, gold_dir=None, drafts_dir=None,
                  structured_dir=None) -> str:
    """这一页卡在哪一步（四档之一）。**只看文件在不在，不判对错。**"""
    stem = Path(str(image_name)).stem
    if not structured_path(stem, structured_dir).exists():
        return STEP_NEEDS_OCR
    if gold_path(image_name, gold_dir).exists():
        return STEP_DONE
    if draft_path(stem, drafts_dir).exists():
        return STEP_NEEDS_ADJUDICATE
    return STEP_NEEDS_ANNOTATE


def collect(*, project_id: Optional[str] = None, stem: str = "",
            outbox_dir=None, inbox_dir=None, gold_dir=None, drafts_dir=None,
            structured_dir=None) -> List[Dict[str, Any]]:
    """要看的页 + 各页状态。`--stem` 给单页，`--project` 按栏目过滤，否则全部。"""
    if stem:
        img = image_path(stem, outbox_dir)
        if img is None:
            return []
        items = [(Path(str(img)).name, "outbox" if outbox_dir else "?")]
    else:
        items = _iter_images(outbox_dir, inbox_dir)

    if project_id:
        import data_io
        items = [(n, s) for (n, s) in items
                 if data_io.get_image_project(n) == project_id]

    pages: List[Dict[str, Any]] = []
    for name, src in items:
        st = classify_page(name, gold_dir=gold_dir, drafts_dir=drafts_dir,
                           structured_dir=structured_dir)
        pages.append({"image_name": name, "stem": Path(name).stem,
                      "source": src, "step": st})
    return pages


# ============================================================
# 唯一判定点
# ============================================================
def run_next(*, profile: str = "", project_id: Optional[str] = None,
             stem: str = "", limit: int = 20, gold_dir=None, drafts_dir=None,
             structured_dir=None, outbox_dir=None, inbox_dir=None,
             learned_dir=None, base: Optional[str] = None,
             probe_timeout: float = 0.6) -> Dict[str, Any]:
    """看一眼全局 → 「下一步做什么、去哪做」。

    ★ 判定点只有这一处：本函数给出 `exit_code`，显示层（`format_human`）不参与判定。
    """
    pages = collect(project_id=project_id, stem=stem, outbox_dir=outbox_dir,
                    inbox_dir=inbox_dir, gold_dir=gold_dir, drafts_dir=drafts_dir,
                    structured_dir=structured_dir)

    counts = {st: 0 for st in STEP_ORDER}
    for p in pages:
        counts[p["step"]] = counts.get(p["step"], 0) + 1

    # ★ 判定**只认"进过管线"的页**（有 OCR / 有草稿 / 有金标准）——
    #   `outbox/` 里躺着一批探测与冒烟残留（`_api_check.png`、`_baidu_real2.png`、
    #   `_verify_test.png`…），它们天然没有 structured。若把它们计入判定，
    #   `next` 会让人去"OCR 一堆测试图"，而真正的入口（那批已 OCR、等人标的页）
    #   反被藏在下面。实测（2026-09-16）：103 页里 26 页无 OCR，**全部是这类残留**；
    #   而 `inbox/` 的 25 张 100% 有 OCR。⇒ 计数照报，**但不参与判定**。
    n_no_ocr = counts.get(STEP_NEEDS_OCR, 0)
    active = [p for p in pages if p["step"] != STEP_NEEDS_OCR]

    scope = ("single" if stem else ("project" if project_id else "all"))
    source = {
        "scope": scope,
        "project": project_id or "",
        "stem": stem or "",
        "gold_dir": str(Path(gold_dir or DEFAULT_GOLD_DIR)),
        "drafts_dir": str(Path(drafts_dir or DEFAULT_DRAFTS_DIR)),
        "structured_dir": str(Path(structured_dir or DEFAULT_STRUCTURED_DIR)),
    }

    # 下一步 = 优先级最高的"还没做完"的那一档（在 active 里找）
    next_step = STEP_DONE
    for st in _PRIORITY:
        if st == STEP_NEEDS_OCR:
            continue
        if any(p["step"] == st for p in active):
            next_step = st
            break
    # 一页都没进管线，但确实有图 → 那才是该跑 OCR 的时候
    if not active and n_no_ocr:
        next_step = STEP_NEEDS_OCR
    next_action = ACTION_FOR_STEP[next_step]

    # ★ 该给地址的两档：待标注 / 待裁决 —— 都要人在界面上动手
    want_step = next_step if next_step in (STEP_NEEDS_ANNOTATE,
                                           STEP_NEEDS_ADJUDICATE) else ""
    urls: List[str] = []
    if want_step:
        try:
            import draft_bridge as DB
            b = base or DB.base_url()
            for p in [x for x in pages if x["step"] == want_step][:max(1, int(limit))]:
                urls.append(DB.annotate_url(p["stem"], outbox_dir=outbox_dir, base=b))
        except Exception:                                            # noqa: BLE001
            urls = []          # 指路信息是增益，坏了不改判定

    up = server_up(base) if want_step else False

    # 该不该重学（只在给了 profile 时判；判据 = 规则文件比金标准旧）
    n_gold = counts.get(STEP_DONE, 0)
    stale_learn = None
    if profile and n_gold:
        try:
            import rule_learn as RL
            lp = Path(learned_dir or DEFAULT_LEARNED_DIR) / f"learned_{profile}.json"
            if not lp.exists():
                stale_learn = True
            else:
                import os
                gd = Path(gold_dir or DEFAULT_GOLD_DIR)
                newest = max([f.stat().st_mtime for f in gd.glob("*.jsonl")] or [0])
                stale_learn = newest > lp.stat().st_mtime
        except Exception:                                            # noqa: BLE001
            stale_learn = None

    # 退出码：一页可看的图都没有 → 缺料（去 import）
    exit_code = EXIT_OK if pages else EXIT_MISSING

    return {
        "ok": exit_code == EXIT_OK,
        "exit_code": exit_code,
        "schema_version": "0.1.0",
        "source": source,
        "counts": {"n_pages": len(pages),
                   "n_needs_ocr": counts.get(STEP_NEEDS_OCR, 0),
                   "n_needs_annotate": counts.get(STEP_NEEDS_ANNOTATE, 0),
                   "n_needs_adjudicate": counts.get(STEP_NEEDS_ADJUDICATE, 0),
                   "n_done": counts.get(STEP_DONE, 0)},
        "next_step": next_step,
        "next_action": next_action,
        "urls": urls,
        "base_url": (urls[0].rsplit("/annotate/", 1)[0] if urls else ""),
        "server_up": up,
        "profile": profile,
        "learn_stale": stale_learn,
        "pages": {st: [p["image_name"] for p in pages if p["step"] == st]
                  for st in STEP_ORDER if counts.get(st)},
    }


# ============================================================
# 人读视图（**只负责显示**）
# ============================================================
def next_command(res: Dict[str, Any]) -> str:
    """给「照抄就能走」的下一步命令（拿不到 profile 时给带占位符的模板）。"""
    pid = res.get("profile") or "<profile>"
    act = res.get("next_action")
    if act == "ocr":
        return "chronicles ocr --images inbox"
    if act == "annotate":
        return ("标完这一页后：`chronicles facts --profile %s`" % pid)
    if act == "adjudicate":
        return ("（这一步只能在界面上做：逐条采纳 / 改写 / 驳回）"
                f" 处理完：chronicles profile learn --profile {pid}")
    if act == "learn":
        return f"chronicles profile learn --profile {pid}"
    return f"chronicles triage --profile {pid}"


def advice_for(res: Dict[str, Any]) -> str:
    """一句话说清「现在该干什么」。"""
    act = res.get("next_action")
    c = res.get("counts") or {}
    if not c.get("n_pages"):
        return ("一页图都没有 —— 先把材料导进来："
                "`chronicles import --images \"<材料目录或PDF>\" --pdf-dpi 320`。")
    if act == "ocr":
        return (f"扫描到的页**都还没进管线**（{c.get('n_needs_ocr')} 页无 OCR）—— "
                f"先 `chronicles ocr --images inbox`，再来。")
    if act == "annotate":
        tail = ("（界面已在运行，直接点开）" if res.get("server_up") else
                "⚠ 界面当前**没有在运行** —— 双击桌面「古籍文献识别管理系统」，"
                "或在 `desktop_shell/` 下 `npm start`；本命令**不会替你起服务**。")
        alt = ("\n     · 不想开界面：也可以在这条对话里标 —— "
               "`annotate lines` 列行 → 你定哪几行是哪个属性 → "
               "`annotate add --lines 1,2 --attr 公司名` 落盘。"
               if not res.get("server_up") else "")
        return (f"{c.get('n_needs_annotate')} 页已 OCR、还没人标 —— "
                f"**这是唯一必须由人做的一步**（系统提框，人定属性）。{tail}{alt}")
    if act == "adjudicate":
        tail = ("" if res.get("server_up") else
                "⚠ 界面当前**没有在运行** —— 双击桌面「古籍文献识别管理系统」。")
        return (f"{c.get('n_needs_adjudicate')} 页有草稿待裁决 —— "
                f"逐条看、采纳或改写，采纳的会变成下一轮的金标准页。{tail}")
    extra = ""
    if res.get("learn_stale"):
        extra = "（金标准比已学规则新 ⇒ 值得重学一次）"
    return (f"进过管线的页都已有人工标注（{c.get('n_done')} 页）—— "
            f"下一步是 `learn`，把金标准变成规则{extra}。")


def format_human(res: Dict[str, Any]) -> str:
    c = res.get("counts") or {}
    src = res.get("source") or {}
    scope_txt = {"single": f"单页 {src.get('stem')}",
                 "project": f"栏目 {src.get('project')}",
                 "all": "inbox/ + outbox/ 全部"}.get(src.get("scope"), "全部")
    L = ["=" * 64,
         "下一步 · 退出码 %s（%s）" % (
             res.get("exit_code"),
             "成功" if res.get("exit_code") == EXIT_OK else "缺料（待补输入）"),
         "=" * 64,
         f"  扫描范围 : {scope_txt}（有图 {c.get('n_pages')} 页）"]
    for st in STEP_ORDER:
        n = c.get("n_" + st, 0)
        if n:
            mark = "  ← 现在这一步" if st == res.get("next_step") else ""
            note = "（未进管线；含探测残留，不计入判定）" if st == STEP_NEEDS_OCR else ""
            L.append(f"  {STEP_LABEL[st]:<12s}: {n} 页{mark}{note}")
    L.append("")
    L.append(f"  {advice_for(res)}")
    for u in (res.get("urls") or [])[:10]:
        L.append(f"    ▸ {u}")
    if res.get("urls") and len(res["urls"]) > 10:
        L.append(f"    … 另有 {len(res['urls']) - 10} 页（用 --limit 调整）")
    L.append("")
    L.append(f"  → {next_command(res)}")
    return "\n".join(L)
