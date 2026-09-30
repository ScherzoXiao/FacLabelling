# -*- coding: utf-8 -*-
"""import_cli —— 图片导入的命令面（技能包 · 用户 2026-09-14 第 2 条）。

用户原话：

    「之前的图片，用户是有两个渠道去进行导入的，有剪贴板监听的渠道，还有批量导入的渠道。
    现在改成了技能包之后，这个图片导入的入口也需要做出相应的改动。」

两个原渠道在技能包里的对应关系：

| 原渠道 | 原形态 | 技能包里的位置 |
|---|---|---|
| 剪贴板监听 `clipboard_watcher.py` | **常驻进程**（截完图自动落 `inbox/`） | **保持原样** —— 它监听的是操作系统事件，不是「一次调用」，天生不属于命令面 |
| 批量导入 `/api/batch_import` | GUI 异步任务（复制 + 等 OCR + 归栏目） | **本模块** —— 只承担「入料」这一步 |

★ **为什么要拆开**：GUI 的批量导入把三件事捆在一个后台任务里，其中「等 OCR 完成」
  依赖 **常驻的 watchdog/Flask 在跑**。而命令面的纪律是「一次调用必须自己跑完」
  （设计稿 §4.2 非交互），**不能依赖别的进程** ⇒ 入料与识别拆成两步：

      chronicles import …   外部图像 → `inbox/`        （本模块）
      chronicles ocr    …   `inbox/` → `structured/`     （`ocr_cli`，走 PaddleOCR-VL 批量档）

  栏目归属**暂不在命令面**（设计稿 §8 风险表：「接口只暴露**能力**，不暴露**流程**」）。

★ **不重造任何一环**（`CLAUDE.md` §60）：
  · 扫描 → `batch_import.scan_paths`（含 PDF 识别与 unsupported 计数）
  · 去重 → `batch_import.pixel_hash`（**像素级**，与 GUI 批量导入同一口径）
  · 命名 → `image_naming.sanitize_stem` + `unique_name`
           （**保留用户原文件名**，冲突时 `_2`/`_3` 递增避让，跨 `inbox/`+`outbox/` 查重）
  所以命令面导入进去的图，与界面导入进去的图**在系统里长得一模一样**。

退出码
    0   成功（**含「全是重复、一张都没复制」** —— 那次导入没白跑，材料本来就在）
    3   缺料（没有可导入的图像：路径不存在 / 目录里没有图像）
    1   内部错误（有文件读/写失败）
    64  用法错误

用法
    python import_cli.py --images "<材料目录>" --dry-run --json
    python import_cli.py --images "<材料目录>" --json
    python chronicles.py import --images "<材料目录>"
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 退出码契约：正本在 `cli_contract`（2026-09-24 收敛）----
# ★ 判定点仍是本模块的 `import_exit_code` —— 收敛的是**词表**，不是判定。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_MISSING,           # noqa: E402
                          EXIT_USAGE, EXIT_MEANING, Parser as _Parser)    # noqa: E402


def _dirs(to: str):
    """返回 `(inbox, outbox, target)`。**目录口径取自 `config`**（单一来源）。"""
    import config
    inbox = Path(config.INBOX)
    outbox = Path(config.OUTBOX)
    target = outbox if str(to or "inbox").lower() == "outbox" else inbox
    return inbox, outbox, target


def _seed_known_from_target(target: Path, known: set) -> Dict[str, Any]:
    """把**落位目录**已有图像的像素 hash 灌进 `known` —— ★ 让导入幂等。

    **为什么必须有这一步**：`seen` 只保证「同一批里不重复」，而**重跑**同一份
    材料时它每次都是空集 ⇒ 会再复制一份（靠 `unique_name` 改名成 `x_2`），
    于是「agent 重试」会**静默造出重复页** —— 违反设计稿 §4.2「幂等」要求。

    **口径**（用户 2026-09-15 裁定）：**只扫落位目录**，不扫 inbox↔outbox 的
    另一侧；跨目录的同内容仍由 `unique_name` 改名避让，不在本函数职责内。

    **只读**：只算 hash，不动任何文件。复用 `batch_import.pixel_hash` 与
    `config.IMAGE_EXTS`（与扫描/命名同源，不另造实现）。
    """
    out: Dict[str, Any] = {"n_dir_files": 0, "n_hashed": 0,
                           "n_read_errors": 0, "seconds": 0.0}
    t0 = time.time()
    try:
        if not target.is_dir():
            return out
        import batch_import as BI
        from config import IMAGE_EXTS
        for p in sorted(target.glob("*")):
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            out["n_dir_files"] += 1
            try:
                h = BI.pixel_hash(p.read_bytes()) or ""
            except OSError:
                out["n_read_errors"] += 1
                continue
            if h:
                known.add(h)
                out["n_hashed"] += 1
    except OSError:
        pass
    finally:
        out["seconds"] = round(time.time() - t0, 3)
    return out


def _run_import_impl(*, images, to="inbox", recursive=True,
                     dry_run=False, limit=0, pdf_dpi=None) -> Dict[str, Any]:
    res: Dict[str, Any] = {
        "ok": False, "dry_run": bool(dry_run), "to": str(_dirs(to)[2]),
        "n_scanned": 0, "n_images": 0, "n_pdf_skipped": 0, "n_unsupported": 0,
        # ★ 2026-09-15 WP-1：PDF 从「一律跳过」改为「渲染成页图后入库」。
        #   `n_pdf_skipped` 的语义随之**收窄**为「**因缺依赖而跳过**」——
        #   它不再是常态读数（旧行为里所有 PDF 都记在这里）。
        "n_pdf_pages": 0, "n_pdf_rendered": 0, "n_pdf_failed": 0, "n_pdf_no_dep": 0,
        "pdf_dpi": 0,
        "n_planned": 0, "n_copied": 0, "n_skipped_dup": 0, "n_failed": 0,
        # ★ 幂等读数：落位目录已有的那批（跳过原因与「批内重复」分开）
        "n_existing": 0, "n_skipped_existing": 0, "dedup_seconds": 0.0,
        "dedup_scope": "",
        "n_renamed": 0, "scan_summary": {}, "planned": [], "errors": [],
    }

    import batch_import as BI

    sc = BI.scan_paths([str(p) for p in (images or [])], recursive=bool(recursive))
    items = list(sc.get("items") or [])
    summ = dict(sc.get("summary") or {})
    res["scan_summary"] = summ
    res["n_scanned"] = len(items)
    res["n_images"] = int(summ.get("images") or 0)
    res["n_pdf_pages"] = int(summ.get("pdf_pages") or 0)
    res["n_unsupported"] = int(summ.get("unsupported") or 0)

    if not items:
        res["error"] = "没有可导入的文件（路径不存在 / 目录里没有图像或 PDF）"
        return res

    img_items = [i for i in items if i.get("type") == "image"]
    pdf_items = [i for i in items if i.get("type") == "pdf"]
    # ★★ WP-1（用户 2026-09-15 第 1 条「无法批量直接导入 pdf」）：
    #   渲染能力**早就在生产里跑着**（GUI 批量导入用它），只是被 `app.py` 独占。
    #   本命令接上**同一份实现**（`batch_import.iter_pdf_pages` / `pdf_page_count` /
    #   `pixel_hash` + `image_naming.unique_base_for_pages`），**零新造**（§60）。
    #   于是技能包导入的 PDF 页图，与界面导入的**在系统里长得一模一样**。
    if not img_items and not pdf_items:
        res["error"] = "没有可导入的图像或 PDF"
        return res

    inbox, outbox, target = _dirs(to)
    res["to"] = str(target)

    import image_naming as IN

    seen: set = set()          # 批内去重（像素 hash）
    planned: set = set()       # 批内已分配目标名（防同名互相覆盖）
    plan: List[Dict[str, Any]] = []

    # ★★ 幂等基线：先把**落位目录已有图像**的 hash 灌进 `known`。
    #    没有这一步，重跑同一份材料会再复制一份（改名成 `x_2`）——
    #    「重试」静默造重复页，正是用户 2026-09-15 裁定的缺陷。
    known: set = set()
    seed = _seed_known_from_target(target, known)
    res["n_existing"] = seed["n_hashed"]
    res["dedup_seconds"] = seed["seconds"]
    res["dedup_scope"] = str(target)
    for it in img_items:
        p = Path(it["path"])
        try:
            data = p.read_bytes()
        except OSError as e:
            res["errors"].append(f"{it['name']}: 读取失败 {e}")
            continue
        h = BI.pixel_hash(data) or ""
        if h and h in seen:                 # 本批内部重复
            res["n_skipped_dup"] += 1
            continue
        if h and h in known:                # ★ 材料已在落位目录里（幂等）
            res["n_skipped_existing"] += 1
            continue
        seen.add(h)
        stem = IN.sanitize_stem(Path(it["name"]).stem)
        new_name = IN.unique_name(inbox, outbox, f"{stem}{it['ext']}",
                                  extra_taken=planned)
        planned.add(new_name)
        plan.append({
            "src": str(p), "name": new_name, "source_name": it["name"],
            "hash": h, "size": int(it.get("size") or 0),
            "renamed": new_name != it["name"],
        })

    # ------------------------------------------------------------------
    # ② PDF：逐页渲染 → 与图像**走同一套**去重 / 命名 / 落位
    # ------------------------------------------------------------------
    # ★ dpi 与 GUI **同一口径**（`app.py:7160-7163`）：缺省
    #   `batch_import.DEFAULT_PDF_DPI`(200) → 钳制 `72..400`。**不另定一套**。
    # ★ 为什么把实际用的 dpi 回报出来：它是**OCR 质量的第一决定因素**
    #   （本轮实测：同一页 200dpi 953×1787 vs 既有读数 ~320dpi 1525×2858，
    #   像素量只有 39%），绝不能让它成为一张暗牌。
    #   ⚠ 技能包文档目前**没规定**该用多少（`SKILL.md` 里 "dpi" 出现 0 次），
    #     项目内三个数字并存（GUI 200 / `scripts/render_pdf.py` 320 / 设计稿 350）
    #     → WP-5c 会在文档里补齐推荐值与理由。
    dpi = int(pdf_dpi) if pdf_dpi else int(BI.DEFAULT_PDF_DPI)
    dpi = max(72, min(400, dpi))
    res["pdf_dpi"] = dpi
    if pdf_items:
        if not BI.pdf_available():
            # ★ **不静默跳过**：缺依赖是「本机环境缺件」，与「材料不在」是两回事，
            #   必须给出 rc≠0 + 可执行的补救。旧行为只回一句"请先转成页图再导入"
            #   —— 那句话对 agent 等于死路（命令面无任何 PDF 入口）。
            res["n_pdf_no_dep"] = len(pdf_items)
            res["n_pdf_skipped"] = len(pdf_items)
            for it in pdf_items:
                res["errors"].append(
                    f"{it['name']}: 缺 PyMuPDF（fitz），无法把 PDF 渲染成页图")
        else:
            for it in pdf_items:
                stem0 = IN.sanitize_stem(Path(it["name"]).stem)
                # base 级冲突预解析：盘上/同批已有 `原名_0001` → 整组递增
                # （`原名_2_0001`…），保证同一 PDF 的页名连续。**同一实现**。
                base = IN.unique_base_for_pages(inbox, outbox, stem0, ".png",
                                                extra_taken=planned)
                try:
                    for page_no, png in enumerate(
                            BI.iter_pdf_pages(Path(it["path"]), dpi=dpi), start=1):
                        h = BI.pixel_hash(png) or ""
                        if h and h in seen:                  # 本批内部重复
                            res["n_skipped_dup"] += 1
                            continue
                        if h and h in known:                 # ★ 幂等：材料已在落位目录
                            res["n_skipped_existing"] += 1
                            continue
                        seen.add(h)
                        new_name = f"{base}_{page_no:04d}.png"   # 4 位 → 字典序 = 页序
                        planned.add(new_name)
                        plan.append({
                            "src": None, "name": new_name,
                            "source_name": f"{it['name']}#p{page_no}",
                            "hash": h, "size": len(png), "renamed": True,
                            "kind": "pdf_page", "png_bytes": png,
                        })
                        res["n_pdf_rendered"] += 1
                except Exception as e:      # noqa: BLE001 —— 逐份隔离，不拖垮整批
                    res["n_pdf_failed"] += 1
                    res["errors"].append(f"{it['name']}: PDF 渲染失败 {e}")

    if limit and limit > 0:
        plan = plan[:limit]
    # ⚠ `res` 会被 `json.dumps` 序列化 ⇒ **渲染字节不能进读数**（只有 PDF 页在内存里
    #   带 `png_bytes`）。剥掉后再登记 `planned`；`plan` 自身留着 bytes 供写盘用。
    res["planned"] = [{k: v for k, v in e.items() if k != "png_bytes"} for e in plan]
    res["n_planned"] = len(plan)
    res["n_renamed"] = sum(1 for e in plan if e.get("renamed"))

    if dry_run:
        res["ok"] = True
        return res

    target.mkdir(parents=True, exist_ok=True)
    for e in plan:
        try:
            if e.get("png_bytes") is not None:      # PDF 页：渲染结果直接落盘
                (target / e["name"]).write_bytes(e["png_bytes"])
            else:                                    # 图像：原件复制（与 GUI 同口径）
                shutil.copy2(e["src"], target / e["name"])
            res["n_copied"] += 1
        except OSError as ex:
            res["n_failed"] += 1
            res["errors"].append(f"{e['name']}: 复制失败 {ex}")

    # ★ 「全是重复、一张都没复制」**不是失败** —— 材料本来就在系统里，
    #   这正是去重生效的表现（与 triage 的「一页都没读到 = 缺料」是两回事：
    #   那里是没读到，这里是读到了且判定无需再导）。
    res["ok"] = True
    return res


def run_import(**kwargs) -> Dict[str, Any]:
    """跑一遍导入，返回结果 + `exit_code`（**唯一判定点**，见 `import_exit_code`）。

    ★ 副作用期把 stdout 整体改道：第三方库会**直接往 stdout 写**（实测 PyMuPDF 的
      `warning: The fitz API is deprecated…`），那会**污染 `--json` 的输出** ——
      agent 拿去 `json.loads` 会直接炸。噪音一律转去 stderr（不丢），stdout 只留 JSON。
      与 `chronicles._run_entry` 是同一手法。
    """
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        res = _run_import_impl(**kwargs)
    stray = buf.getvalue()
    if stray.strip():
        sys.stderr.write(stray)
    res["exit_code"] = import_exit_code(res)
    return res


def import_exit_code(result: Dict[str, Any]) -> int:
    """★ 判定点只有这一处 —— `main` 与 `chronicles.cmd_import` 都只取用、不重判。"""
    if not isinstance(result, dict) or not result.get("ok"):
        return EXIT_MISSING
    if int(result.get("n_failed") or 0) > 0:            # 有文件没写成功
        return EXIT_INTERNAL
    # ★ WP-1：PDF 渲染失败 / 缺依赖 ⇒ **有材料没进系统**，同属"有文件没导入成功"。
    #   归 1（内部错误）而非 3（缺料）：材料在、路径对，卡点在本机环境/渲染能力，
    #   两者的下一步动作不同（补依赖 vs 补文件），不能合并（§4.3 同一条纪律）。
    if int(result.get("n_pdf_failed") or 0) > 0:
        return EXIT_INTERNAL
    if int(result.get("n_pdf_no_dep") or 0) > 0:
        return EXIT_INTERNAL
    return EXIT_OK


def advice_for(result: Dict[str, Any]) -> str:
    # ⚠ 判定顺序有意为之：**先判成败、再判干跑**。
    #   旧实现把 `dry_run` 放在最前，于是 rc 3（什么都没扫到）时 next 仍是
    #   「去掉 --dry-run 即真导入」—— agent 会照做、然后撞同一面墙。
    #   这与 `_fail()` 的纪律一致：报错必须给**方向正确的**下一步。
    if not result.get("ok"):
        return ("没有可导入的图像或 PDF —— 确认 --images 的路径。"
                + (f"（{result.get('error')}）" if result.get("error") else ""))
    # ---- WP-1：PDF 相关（含"本机缺件"与"渲染失败"两种，动作不同）----
    if int(result.get("n_pdf_no_dep") or 0) > 0:
        return (f"有 {int(result['n_pdf_no_dep'])} 份 PDF 没能渲染 —— 本机缺 PyMuPDF（fitz）。"
                "装上依赖后重跑同一命令即可（已导入的页会按像素 hash 幂等跳过，不会重复）："
                "`python -m pip install PyMuPDF`。")
    if int(result.get("n_pdf_failed") or 0) > 0:
        return f"有 {int(result['n_pdf_failed'])} 份 PDF 渲染失败 —— 见 errors 逐份原因。"
    if int(result.get("n_failed") or 0) > 0:
        return f"有 {int(result['n_failed'])} 个文件没导入成功 —— 见 errors。"
    if result.get("dry_run"):
        n_pdf = int(result.get("n_pdf_rendered") or 0)
        return ("这是干跑：去掉 --dry-run 即真导入。"
                + (f"（其中 {n_pdf} 页将由 PDF 渲染产出，dpi={result.get('pdf_dpi')}）"
                   if n_pdf else ""))
    n_dup = int(result.get("n_skipped_dup") or 0)
    n_ext = int(result.get("n_skipped_existing") or 0)
    if int(result.get("n_copied") or 0) == 0 and (n_dup or n_ext):
        why = []
        if n_ext:
            why.append(f"{n_ext} 张在 {result.get('to')} 里已有同图")
        if n_dup:
            why.append(f"{n_dup} 张批内重复")
        return ("全部都是重复的（" + "；".join(why) +
                "），未再复制 —— 材料已在系统里，可直接 `chronicles ocr`。")
    return (f"已导入 {result.get('n_copied')} 张到 {result.get('to')} —— "
            f"下一步 `chronicles ocr --images {result.get('to')}`。")


def format_human(result: Dict[str, Any]) -> str:
    rc = int(result.get("exit_code", import_exit_code(result)))
    L: List[str] = []
    L.append("=" * 64)
    L.append(f"图片导入 · 退出码 {rc}（{EXIT_MEANING.get(rc, rc)}）")
    L.append("=" * 64)
    n_pdf_files = int((result.get("scan_summary") or {}).get("pdfs") or 0)
    n_pdf_pages = int(result.get("n_pdf_pages") or 0)
    n_pdf_rendered = int(result.get("n_pdf_rendered") or 0)
    pdf_desc = f"PDF {n_pdf_files} 份"
    if n_pdf_pages:
        pdf_desc += f"（{n_pdf_pages} 页）"
    L.append(f"  扫描到      : {result.get('n_scanned')} 个文件"
             f"（图像 {result.get('n_images')} / {pdf_desc} / "
             f"不支持 {result.get('n_unsupported')}）")
    if n_pdf_files:
        # ★ dpi 必须显式可见：它是 OCR 质量的第一决定因素，不能是一张暗牌。
        L.append(f"  PDF 渲染    : 产出 {n_pdf_rendered} 页 · dpi={result.get('pdf_dpi')}"
                 + (f" · 失败 {result.get('n_pdf_failed')} 份"
                    if result.get("n_pdf_failed") else "")
                 + (f" · 缺依赖 {result.get('n_pdf_no_dep')} 份"
                    if result.get("n_pdf_no_dep") else ""))
    L.append(f"  待导入      : {result.get('n_planned')} 张"
             f"（其中改名 {result.get('n_renamed')} 张）")
    L.append(f"  跳过(重复)  : {result.get('n_skipped_dup')} 张")
    if result.get("n_skipped_existing"):
        L.append(f"  跳过(已在库): {result.get('n_skipped_existing')} 张"
                 f"（{result.get('dedup_scope')} 已有同图"
                 f" {result.get('n_existing')} 张 / 查重 {result.get('dedup_seconds')}s）")
    L.append(f"  已复制      : {result.get('n_copied')} 张 → {result.get('to')}")
    if result.get("n_failed"):
        L.append(f"  失败        : {result.get('n_failed')}")
    if result.get("dry_run"):
        L.append("  （干跑：未复制任何文件）")
    errs = result.get("errors") or []
    if errs:
        L.append(f"  --- 明细（最多 10 条 / 共 {len(errs)} 条）---")
        for e in errs[:10]:
            L.append(f"    · {e}")
    L.append(f"  → {advice_for(result)}")
    return "\n".join(L)


# ============================================================
# 入口
# ============================================================
# （`_Parser` = `cli_contract.Parser`，已在文件头随词表一并 import。）


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="import_cli",
        description="图片导入：外部图像 / PDF → inbox/（图像保留原名；"
                    "PDF 逐页渲染为 `原名_NNNN.png`；像素级去重；冲突自动避让）",
        epilog="退出码：0 成功 · 3 缺料 · 1 内部错误（含 PDF 渲染失败 / 缺依赖）· 64 用法错误",
    )
    ap.add_argument("--images", nargs="+", required=True,
                    help="图像或 PDF 的目录（可递归）或文件，可给多个")
    ap.add_argument("--to", choices=["inbox", "outbox"], default="inbox",
                    help="落位目录（默认 inbox，与剪贴板通道一致）")
    ap.add_argument("--limit", type=int, default=0, help="只导前 N 张（0 = 全部）")
    ap.add_argument("--pdf-dpi", type=int, default=None,
                    help="PDF 渲染 dpi（默认 200，钳制 72..400 —— 与界面批量导入"
                         "同口径）。★ 这是 OCR 质量的第一决定因素：官报类竖排小字"
                         "材料宜用 300+（见 SKILL.md 步骤 A-3）")
    ap.add_argument("--no-recursive", action="store_true",
                    help="目录不递归（默认递归，含子目录）")
    ap.add_argument("--dry-run", action="store_true", help="只列将导入什么，不复制")
    ap.add_argument("--json", action="store_true", dest="as_json")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(list(argv) if argv is not None else None)
    r = run_import(images=a.images, to=a.to, recursive=not a.no_recursive,
                   dry_run=a.dry_run, limit=a.limit, pdf_dpi=a.pdf_dpi)
    rc = int(r.get("exit_code", EXIT_INTERNAL))
    if not a.as_json:
        print(format_human(r))
        return rc
    payload = dict(r)
    payload["advice"] = advice_for(r)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(format_human(r), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
