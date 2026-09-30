# -*- coding: utf-8 -*-
"""金标准普查（命令面 `chronicles gold`）—— 「我有哪些档案/项目、金标准做到哪一步」。

★ 为什么需要它（用户 2026-09-16 两条指令）
-------------------------------------------------------------------
1. 「**保证**每个类型的项目只能使用自己的金标准」——隔离落地后，必须让人**看得见**
   「哪些行被排除了、为什么」。否则页数从 7 变 4 只会被当成 bug，而不是隔离生效
   （本项目一贯的纪律：**可见不静默**）。
2. 「需要给用户一个可视化界面：自己有哪些项目、**项目的金标准页制作进程**如何」
   —— 这份读数就是那个界面的**数据面**。

★ 定位：**只读聚合**，不新增任何判据
-------------------------------------------------------------------
  · 隔离过滤 → `rule_learn.filter_by_profile`（唯一落点）
  · 按档案归集 → `rule_learn.gold_census`（唯一实现）
  · 图 → 项目 → `data_io.stem_to_project`（唯一实现）
  · 该页有没有 OCR → `next_step.structured_path`（唯一落点）
⇒ 若日后做界面，界面必须是这份读数的**投影**，不能另算一遍。

★ 只读：绝不写 `manual_annotations/`（受保护目录）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import data_io
import next_step as NS
import profile_store
import rule_learn as RL

# 词表正本在 `cli_contract`（2026-09-24 收敛）；本命令只读，只有 0 一档正常出口。
from cli_contract import EXIT_OK, EXIT_MEANING                              # noqa: E402


def _gold_dir_of(gold_dir=None) -> Path:
    """缺省取 `rule_learn.DEFAULT_GOLD_DIR` —— **调用时**读，便于测试隔离。"""
    return Path(gold_dir) if gold_dir else Path(RL.DEFAULT_GOLD_DIR)


def run_gold(*, gold_dir=None, profiles_dir=None, structured_dir=None) -> Dict[str, Any]:
    """普查。返回**纯数据**（人面渲染交给 `format_human`）。

    ⚠ 2026-09-24 清理：曾有的 `outbox_dir` / `inbox_dir` 形参无任何消费者（死参数），
    已删 —— 三个调用方都只传 `gold_dir`（`structured_dir` 真实用于 OCR 计数，保留）。
    """
    gdir = _gold_dir_of(gold_dir)
    census = RL.gold_census(gdir)

    # ---- 档案侧：合并"答案库里有行"与"档案表里有条目" ----
    try:
        profs = profile_store.list_profiles(profiles_dir) if profiles_dir \
            else profile_store.list_profiles()
    except Exception:                                        # noqa: BLE001
        profs = []
    by_id = {p.get("profile_id"): p for p in profs}
    rows = []
    for pid, g in (census.get("by_profile") or {}).items():
        prof = by_id.get(pid) or {}
        rows.append({
            "profile_id": pid,
            "name": prof.get("name") or "",
            "n_attrs": len(prof.get("attrs") or []),
            "n_gold_pages": g["n_pages"],
            "n_gold_rows": g["n_rows"],
            "pages": list(g["stems"]),
            "learned": bool(RL.load(pid)),
            "registered": pid in by_id,
        })
    # 有档案、但一页金标准都没有的 —— **也要列**，那正是"还没开始"的那一套
    for pid, prof in by_id.items():
        if pid and pid not in (census.get("by_profile") or {}):
            rows.append({
                "profile_id": pid,
                "name": prof.get("name") or "",
                "n_attrs": len(prof.get("attrs") or []),
                "n_gold_pages": 0, "n_gold_rows": 0, "pages": [],
                "learned": bool(RL.load(pid)), "registered": True,
            })
    rows.sort(key=lambda r: (-r["n_gold_pages"], r["name"] or r["profile_id"]))

    # ---- 项目侧：图 / 有 OCR / 有金标准 ----
    try:
        projects = data_io.list_projects() or []
    except Exception:                                        # noqa: BLE001
        projects = []
    try:
        assignments = data_io.get_all_assignments() or {}
    except Exception:                                        # noqa: BLE001
        assignments = {}
    stem_to_proj = data_io.stem_to_project(assignments)

    # ★ 项目侧的「有金标准」= **组织视角、不限档案**：一个项目里"有几页已经有人
    #   标过了"就是这几页，不管标在哪套档案名下。这不是隔离的例外 ——
    #   隔离约束的是**推断口**（我读哪些行），不是**视图**（我总数是多少）。
    #   ⚠ 但它**不能**反过来当推断的输入（`facts` / `learn` 一律走档案过滤）。
    gold_stems = set()
    for _pid, g in (census.get("by_profile") or {}).items():
        gold_stems.update(g["stems"])

    proj_rows: List[Dict[str, Any]] = []
    for p in projects:
        pid = p.get("id") or ""
        try:
            stems = data_io.get_project_image_stems(pid) or []
        except Exception:                                    # noqa: BLE001
            stems = []
        n_gold = sum(1 for s in stems if s in gold_stems)
        n_ocr = 0
        for s in stems:
            try:
                if NS.structured_path(s, structured_dir).exists():
                    n_ocr += 1
            except Exception:                                # noqa: BLE001
                pass
        proj_rows.append({
            "id": pid, "name": p.get("name") or pid,
            "n_images": len(stems), "n_gold_pages": n_gold, "n_ocr": n_ocr,
            # 「待标注」= 已有 OCR、还没人标 —— 这才是人真正要看的那个数
            "n_pending_annotate": max(0, n_ocr - n_gold),
        })
    proj_rows.sort(key=lambda r: (-r["n_gold_pages"], -r["n_images"], r["name"]))

    return {
        "ok": True, "exit": EXIT_OK, "subcommand": "gold",
        "gold_dir": str(gdir),
        "total": {"n_pages": census["n_pages"], "n_rows": census["n_rows"]},
        "profiles": rows,
        "unassigned": census["unassigned"],
        "mixed": census["mixed"],
        "projects": proj_rows,
        "unassigned_stems": sorted(set(gold_stems) - set(stem_to_proj)),
    }


def next_hint(res: Dict[str, Any]) -> str:
    """一句话：现在该干什么。"""
    rows = res.get("profiles") or []
    empty = [r for r in rows if r["n_gold_pages"] == 0 and r["registered"]]
    projs = res.get("projects") or []
    pend = sum(r["n_pending_annotate"] for r in projs)
    if not rows:
        return "库里一页金标准都没有 —— 先 `chronicles import` 导图，再标第一页。"
    if empty:
        e = empty[0]
        return (f"档案「{e['name'] or e['profile_id']}」还没有一页金标准 —— "
                f"它是冷启动的起点：`chronicles next` 看哪一页该标。")
    if pend:
        return f"还有 {pend} 页已 OCR、等人标注 —— `chronicles next` 给出入口。"
    return "各档案都有金标准了 —— 可以 `chronicles learn` 重学规则后跑新页。"


def format_human(res: Dict[str, Any]) -> str:
    """人读表。★ 显示层**不参与判定**（抛异常不得改变退出码，`chronicles._mirror` 收口）。"""
    L: List[str] = []
    t = res.get("total") or {}
    L.append(f"金标准库  {res.get('gold_dir')} ｜ {t.get('n_pages', 0)} 页 / "
             f"{t.get('n_rows', 0)} 行")
    L.append("")
    L.append("档案（文献类型 = 隔离单位；推断只读本档案的行）")
    L.append(f"  {'profile_id':32s} {'名称':16s} {'属性':>4s} {'金标准页':>7s} "
             f"{'行数':>5s}  已学规则")
    rows = res.get("profiles") or []
    if not rows:
        L.append("  （一个都没有）")
    for r in rows:
        L.append(f"  {r['profile_id']:32s} {(r['name'] or '(无名)'):16s} "
                 f"{r['n_attrs']:4d} {r['n_gold_pages']:7d} {r['n_gold_rows']:5d}"
                 f"  {'✓' if r['learned'] else '—'}")
    un = res.get("unassigned") or {}
    if un.get("n_pages"):
        L.append(f"  ── 未归档（没有 profile 标签；**推断时一律排除**）："
                 f"{un['n_pages']} 页 / {un['n_rows']} 行")
        for s in (un.get("stems") or [])[:10]:
            L.append(f"       {s}")
    mixed = res.get("mixed") or []
    if mixed:
        L.append(f"  ── ⚠ 同页多档案（自相矛盾，请核对）：{len(mixed)} 页")
        for m in mixed[:10]:
            L.append(f"       {m['stem']}  {m['profiles']}")
    L.append("")
    L.append("栏目（项目 = 组织单位；**不**决定隔离）")
    L.append(f"  {'id':28s} {'名称':14s} {'图数':>4s} {'有金标准':>7s} "
             f"{'已OCR':>5s} {'待标注':>5s}")
    projs = res.get("projects") or []
    if not projs:
        L.append("  （一个都没有）")
    for r in projs:
        L.append(f"  {r['id']:28s} {r['name']:14s} {r['n_images']:4d} "
                 f"{r['n_gold_pages']:7d} {r['n_ocr']:5d} {r['n_pending_annotate']:5d}")
    L.append("")
    L.append("→ " + next_hint(res))
    return "\n".join(L)


def _main(argv: Optional[List[str]] = None) -> int:          # pragma: no cover
    import argparse
    import json
    ap = argparse.ArgumentParser(description="金标准普查")
    ap.add_argument("--gold", dest="gold_dir", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(list(argv) if argv is not None else None)
    res = run_gold(gold_dir=a.gold_dir)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(format_human(res))
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(_main())
