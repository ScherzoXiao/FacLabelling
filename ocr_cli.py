# -*- coding: utf-8 -*-
"""ocr_cli —— OCR 环节的命令面（技能包落地 · 第 0 步补完，2026-09-14）。

**为什么单独一个模块**：`chronicles.py` 的定位是「纯包装既有 CLI」（转发 → 读回落盘产物 →
映射退出码），而 OCR 环节**没有可转发的既有 CLI**（`preannotate_gen` 是全链路编排，
不是 OCR 入口）。按 `page_triage.py` 的先例（模块自己长命令面），本模块承担这一环。

**本模块只做编排，不重造任何一环**（单一实现）：
    · 引擎构造   → `preannotate_gen.build_engine()`（与 `app.init_ocr()` 同源取配置）
    · 批量提交   → `ocr_backend.prefetch()`（P-OPT-6/7 已落地的能力，**不自造并发**）
    · 结构化落盘 → `preannotate_gen.write_structured()`（唯一实现）
    · 粒度默认值 → `config.py`（`OCR_BATCH_PREFETCH` / `OCR_BATCH_PARALLEL` / `OCR_BATCH_MAX`）

**粒度纪律**（用户 2026-09-14 明确「注意之前改造的粒度问题」）
    默认即走 P-OPT-7 的批量档（`OCR_BATCH_PREFETCH = 100`），并**把实际粒度读数写进结果**
    （prefetch / parallel / max / submit_interval / 实际批量提交张数）—— 让「并发多少、
    钱花在哪」可见。`submit_interval = 0.25 s` 是**承重的**（零节流并发提交实测会撞
    HTTP 429 / code 12002），故本模块**不提供**关闭它的开关，只提供整档关闭预取的
    `--no-prefetch`（退回逐张档，用于故障排查）。

**资源口径**（用户 2026-09-14）：PaddleOCR-VL **每天 20000 页免费、一般用不完** ⇒
本环节不设节约闸门；要计较快的是 DeepSeek 文本轨（本模块**完全不碰**它）。

退出码（与 `page_triage` 同契约）
    0   成功（全部页落盘）
    3   缺料（没有可跑的图像：目录不存在 / 目录里没有图像）
    1   内部错误（有页没做出来 —— OCR 失败 / 空行 / 落盘失败）
    64  用法错误

用法
    python ocr_cli.py --images inbox/ --json
    python ocr_cli.py --images "<材料目录>" --out data/structured_zhi --limit 5
    python chronicles.py ocr --images inbox/ --dry-run --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 退出码契约：正本在 `cli_contract`（2026-09-24 收敛，此前 10 份抄写）----
# ★ 判定点仍是本模块的 `ocr_exit_code`（唯一判定、双端取用）——收敛的是**词表**，不是判定。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_MISSING,           # noqa: E402
                          EXIT_USAGE, EXIT_MEANING, Parser as _Parser,    # noqa: E402
                          capture_stray_stdout)                           # noqa: E402
# ^ EXIT_USAGE 本模块不用，但 test_ocr_cli 以 `OC.EXIT_USAGE` 取词表（re-export），不可删。

# ---- 路径口径：与既有模块逐字一致，不另造（§60）----
DEFAULT_STRUCTURED_DIR = ROOT / "data" / "structured"
DEFAULT_OVERWRITE_ARCHIVE = ROOT / "_archive" / "ocr_overwrite"

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"})


# ============================================================
# 输入收集
# ============================================================
def _collect_images(sources: Sequence[str]) -> List[Path]:
    """把 `--images` 的取值解析成有序去重的图像路径表。

    目录 → 只扫**顶层**（与 `page_triage._stems_from_inbox` 同口径）、按名排序；
    显式给出的文件 → **不按后缀过滤**（用户指名要跑，跑不动会如实报错）。
    """
    out: List[Path] = []
    for s in sources or []:
        p = Path(s)
        if p.is_dir():
            out += sorted(q for q in p.iterdir()
                          if q.is_file() and q.suffix.lower() in IMAGE_SUFFIXES)
        elif p.is_file():
            out.append(p)
    seen: set = set()
    uniq: List[Path] = []
    for q in out:
        k = str(q.resolve()).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(q)
    return uniq


# ============================================================
# 主体
# ============================================================
def _run_ocr_impl(*, images: Sequence[str], out_dir=None, limit: int = 0,
                  prefetch: Optional[int] = None, parallel: Optional[int] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
    t0 = time.time()
    res: Dict[str, Any] = {
        "ok": False, "dry_run": bool(dry_run),
        "out_dir": str(out_dir or DEFAULT_STRUCTURED_DIR),
        "n_images": 0, "n_ok": 0, "n_failed": 0,
        "images": [], "items": [], "errors": [],
        "engine": {}, "batch": {},
    }

    paths = _collect_images(images)
    if limit and limit > 0:
        paths = paths[:limit]
    res["n_images"] = len(paths)
    res["images"] = [p.name for p in paths]

    if not paths:
        res["error"] = "没有可跑的图像（目录不存在 / 目录里没有图像文件）"
        res["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        return res

    if dry_run:
        res["ok"] = True
        res["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        return res

    import preannotate_gen as PG

    try:
        engine = PG.build_engine()
    except Exception as e:                       # 凭证 / 配置问题 → 说清怎么办
        res["error"] = (f"OCR 引擎构造失败：{type(e).__name__}: {e}"
                        "（检查 PaddleOCR-VL 凭证：环境变量 PADDLEOCR_VL_TOKEN，"
                        "或项目根 ocr_backend_config.json 的 paddleocr_vl_token 字段）")
        res["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        return res

    if prefetch is not None:
        engine.batch_prefetch = int(prefetch)
    if parallel is not None:
        engine.batch_parallel = max(1, int(parallel))
    res["engine"] = {
        "name": getattr(engine, "name", ""),
        "model": getattr(engine, "model", ""),
        "batch_prefetch": int(getattr(engine, "batch_prefetch", 0)),
        "batch_parallel": int(getattr(engine, "batch_parallel", 0)),
        "batch_max": int(getattr(engine, "batch_max", 0)),
        "submit_interval": float(getattr(engine, "submit_interval", 0.0)),
    }

    d = Path(out_dir) if out_dir else DEFAULT_STRUCTURED_DIR
    d.mkdir(parents=True, exist_ok=True)

    # ① 批量提交（P-OPT-6/7）：把「什么时候提交」与「怎么拿结果」解耦。
    #    `batch_prefetch <= 0` 时引擎内部直接返回 0 = 退回逐张档。
    n_pf = 0
    if res["engine"]["batch_prefetch"] > 0:
        try:
            n_pf = int(engine.prefetch([str(p) for p in paths]))
        except Exception as e:                   # 预取失败**不阻断**：recognize 会自己提交
            res["errors"].append(f"prefetch: {type(e).__name__}: {e}")
    res["batch"]["prefetched"] = n_pf

    # ② 逐张取结果（命中预取缓存即返回，不重复提交）→ ③ 结构化落盘
    for p in paths:
        rec: Dict[str, Any] = {"name": p.name, "stem": p.stem}
        try:
            r = engine.recognize(str(p))
            lines = r.get("lines") or []
            rec["n_lines"] = len(lines)
            if not lines:
                rec["error"] = "ocr_empty（该页无文本行）"
            else:
                ok = bool(PG.write_structured(p.stem, r, d,
                                              DEFAULT_OVERWRITE_ARCHIVE, engine))
                if not ok:
                    rec["error"] = "structured_save_failed（落盘失败）"
                else:
                    rec["written"] = f"{p.stem}.json"
                    rec["n_chars"] = sum(len(x.get("text") or "") for x in lines)
        except Exception as e:                   # 逐页隔离：单页失败不中断整批
            rec["error"] = f"{type(e).__name__}: {e}"
        if rec.get("error"):
            res["n_failed"] += 1
            res["errors"].append(f"{p.name}: {rec['error']}")
        else:
            res["n_ok"] += 1
        res["items"].append(rec)

    res["ok"] = True
    res["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    return res


def run_ocr(**kwargs) -> Dict[str, Any]:
    """跑一遍 OCR 落盘，返回结果 + `exit_code`（**唯一判定点**，见 `ocr_exit_code`）。

    ★ 副作用期把 stdout 整体改道：OCR 后端链路会 **import 第三方库到 stdout 写警告**
      （实测 PyMuPDF 的 deprecation warning），那会**污染 `--json` 的输出** ——
      agent 拿去 `json.loads` 会直接炸。噪音一律转去 stderr（不丢），stdout 只留 JSON。
      与 `chronicles._run_entry` 是同一手法。
    """
    res = capture_stray_stdout(_run_ocr_impl, **kwargs)
    res["exit_code"] = ocr_exit_code(res)
    return res


def ocr_exit_code(result: Dict[str, Any]) -> int:
    """OCR 结果的退出码。**判定点只有这一处** —— `main` 与 `chronicles.cmd_ocr` 都只取用。

    ★ 与 `triage` 同族的两条纪律：
      · **「一张都没读到」不是成功** —— 没有可跑的图像 = 缺料 3，绝不 0；
      · 有页没做出来 = 内部错误 1（不是 3）—— 料是齐的，是这一趟没跑成；
        两者的下一步动作相反：**补材料 vs 重跑/查凭证**。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return EXIT_MISSING
    if int(result.get("n_failed") or 0) > 0:
        return EXIT_INTERNAL
    return EXIT_OK


def advice_for(result: Dict[str, Any]) -> str:
    """报错要给「下一步怎么办」（§4.2 报错可执行）。"""
    if result.get("dry_run"):
        return "这是干跑：去掉 --dry-run 即真跑。"
    if not result.get("ok"):
        return ("没有可跑的图像 —— 确认 --images 的路径，或先把图像放进 inbox/。"
                + (f"（{result.get('error')}）" if result.get("error") else ""))
    n_failed = int(result.get("n_failed") or 0)
    if n_failed:
        return (f"有 {n_failed} 页没做出来 —— 逐页原因见 errors；"
                f"修好后重跑同一命令即可（已完成页会落盘，不受影响）。")
    return f"全部落盘到 {result.get('out_dir')} —— 下一步可用 triage 分诊，或直接进 facts。"


def format_human(result: Dict[str, Any]) -> str:
    rc = int(result.get("exit_code", ocr_exit_code(result)))
    L: List[str] = []
    L.append("=" * 64)
    L.append(f"OCR 命令面 · 退出码 {rc}（{EXIT_MEANING.get(rc, rc)}）")
    L.append("=" * 64)
    eng = result.get("engine") or {}
    if eng:
        L.append(f"  引擎        : {eng.get('name')} / {eng.get('model')}")
        L.append(f"  批量粒度    : prefetch={eng.get('batch_prefetch')} "
                 f"parallel={eng.get('batch_parallel')} max={eng.get('batch_max')} "
                 f"submit_interval={eng.get('submit_interval')}s")
        pf = (result.get("batch") or {}).get("prefetched")
        L.append(f"  批量提交    : {pf} 张")
    L.append(f"  输入        : {result.get('n_images')} 张")
    L.append(f"  落盘成功    : {result.get('n_ok')} 张")
    L.append(f"  失败        : {result.get('n_failed')} 张")
    L.append(f"  输出目录    : {result.get('out_dir')}")
    if result.get("dry_run"):
        L.append("  （干跑：未调用 API）")
    errs = result.get("errors") or []
    if errs:
        L.append(f"  --- 明细（最多 10 条 / 共 {len(errs)} 条）---")
        for e in errs[:10]:
            L.append(f"    · {e}")
    L.append(f"  用时        : {result.get('elapsed_ms')} ms")
    L.append(f"  → {advice_for(result)}")
    return "\n".join(L)


# ============================================================
# 入口
# ============================================================
# （`_Parser` = `cli_contract.Parser`，已在文件头随词表一并 import：
#   用法错误 2→64 的改写此前 5+ 个模块逐字抄写，现收口一份。）


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="ocr_cli",
        description="OCR 命令面：图像 → data/structured/*.json（走 PaddleOCR-VL 批量档）",
        epilog="退出码：0 成功 · 3 缺料 · 1 内部错误 · 64 用法错误",
    )
    ap.add_argument("--images", nargs="+", required=True,
                    help="图像目录（扫顶层）或图像文件，可给多个")
    ap.add_argument("--out", default=None,
                    help="结构化落盘目录（默认 data/structured）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 张（0 = 全部）")
    ap.add_argument("--prefetch", type=int, default=None,
                    help="覆盖批量提交张数（0 = 退回逐张档；默认取 config.OCR_BATCH_PREFETCH）")
    ap.add_argument("--no-prefetch", action="store_true",
                    help="等价于 --prefetch 0（故障排查用）")
    ap.add_argument("--parallel", type=int, default=None,
                    help="覆盖并发数（默认取 config.OCR_BATCH_PARALLEL）")
    ap.add_argument("--dry-run", action="store_true", help="只列将跑哪些图，不调用 API")
    ap.add_argument("--json", action="store_true", dest="as_json")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(list(argv) if argv is not None else None)
    pf = a.prefetch
    if a.no_prefetch:
        pf = 0
    r = run_ocr(images=a.images, out_dir=a.out, limit=a.limit,
                prefetch=pf, parallel=a.parallel, dry_run=a.dry_run)
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
