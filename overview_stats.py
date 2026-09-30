# -*- coding: utf-8 -*-
"""总览页"系统运作状态"统计聚合（2026-09-06 总览重构）。

总览页按四阶段工作流展示（摄取OCR → 归纳与金标准 → 数据飞轮 → 知识库），
本模块聚合跨模块的状态数字，供 /api/stats 的 flywheel 字段消费：

- 金标准（阶段②）：gold_pages / gold_rows
  （manual_annotations/*.jsonl 扫描）
  ⚠ `gold_attrs` 保留但**不再上屏**（2026-09-11）：它是跨栏目属性名**并集**，
    栏目增多时若属性名重叠，该数可原地不动甚至回落，当不了增长指标。
    阶段②改上「已归纳例题的栏目数 / 总栏目数」——见 `project_gold_summary`。
- 数据飞轮（阶段③）：待核三态 pending/checked/fixed
  （复用 review_store.collect_items）+ fewshot 回流样本数
  + runs（divergence 对账批次数）
- 知识库（阶段④）数字 api_stats 已有 knowledge_summary，不在此重复

缓存：loadStats 每 10s 轮询，扫描类统计 60s TTL；人工核完一条后
由 app 调 invalidate_cache() 保证下轮刷新即见。
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import review_store

TTL_S = 60.0
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def gold_summary(annot_dir: Path) -> Dict[str, int]:
    """金标准规模：页数（jsonl 文件数）/ 行数 / 属性种数（非空 attr 去重）。

    ⚠ `gold_attrs` 只做向后兼容保留，**界面不得再呈现**（理由见模块 docstring）。
    """
    pages = rows = 0
    attrs: set = set()
    annot_dir = Path(annot_dir)
    if annot_dir.exists():
        for jf in sorted(annot_dir.glob("*.jsonl")):
            pages += 1
            for line in jf.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows += 1
                if d.get("attr"):
                    attrs.add(str(d["attr"]))
    return {"gold_pages": pages, "gold_rows": rows, "gold_attrs": len(attrs)}


OCR_FREE_QUOTA_PER_DAY = 20000
"""OCR 轨（PaddleOCR-VL）供应商口径的每日免费额度（页）。免费轨可放心跑，故台账里
只报"用了多少"，不报钱；这个常量是**供应商声明值**，不是用户设置，改名/调额时改这里。"""


def cost_ledger(structured_dir: Optional[Path] = None,
                preann_dir: Optional[Path] = None,
                now: Optional[float] = None) -> Dict[str, Any]:
    """**成本台账**（存量口径）：两条线上通道各自消耗了多少。

    与「评测台」的分工（2026-09-11 重构）：
      - 评测台 = **单次 run 口径**（"这一趟花了多少"）；
      - 本函数 = **存量口径**（"这一段时间累计花了多少"）。两者不重复。

    指标（只 stat + 读小 JSON，零 API、零 LLM，可随 /api/stats 轮询）：
      - `ocr_today`           今日落盘的 `structured/*.json` 张数（免费轨用量）
      - `ocr_quota_per_day`   免费额度上限（常量，供界面算占比）
      - `llm_cost_month_cny`  本月切分旁证里记到的 `l1_cost_cny` 合计（付费轨用量）
      - `llm_pages_month`     有花钱记录的页数

    ⚠ 口径诚实说明：`llm_cost_month_cny` 只覆盖**切分**（L1/L2）记账，不含对话/其它
    LLM 调用——本系统只有切分会把花费落到旁证里。界面须如实标注，不得冒充"总花费"。
    """
    ts = time.time() if now is None else float(now)
    today = datetime.date.fromtimestamp(ts)
    sd = Path(structured_dir or (Path(__file__).resolve().parent / "data" / "structured"))
    pd = Path(preann_dir or (Path(__file__).resolve().parent / "data" / "preannotations"))

    ocr_today = 0
    if sd.exists():
        for p in sd.glob("*.json"):
            try:
                if datetime.date.fromtimestamp(p.stat().st_mtime) == today:
                    ocr_today += 1
            except OSError:
                continue

    cost = 0.0
    pages = 0
    if pd.exists():
        for p in pd.glob("*.l0.json"):
            try:
                d0 = datetime.date.fromtimestamp(p.stat().st_mtime)
                if (d0.year, d0.month) != (today.year, today.month):
                    continue
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            v = d.get("l1_cost_cny") if isinstance(d, dict) else None
            if isinstance(v, (int, float)) and v:
                cost += float(v)
                pages += 1

    return {
        "date": today.isoformat(),
        "ocr_today": ocr_today,
        "ocr_quota_per_day": OCR_FREE_QUOTA_PER_DAY,
        "llm_cost_month_cny": round(cost, 4),
        "llm_pages_month": pages,
    }


def project_gold_summary(annot_dir: Path,
                         project_ids: Optional[list] = None,
                         assignments: Optional[dict] = None) -> Dict[str, int]:
    """**栏目维度**的例题覆盖：回答"还有几个栏目的飞轮不成立"。

    动机（2026-09-11 用户指令）：总览阶段②原先上屏 `gold_attrs`（跨栏目属性
    并集），多栏目下没有呈现价值 —— 并集可以原地不动甚至回落。改上本口径：

      - `projects_total`          栏目总数
      - `projects_with_examples`  **已有例题**的栏目数（该栏目名下 ≥1 页金标准）
      - `pages_assigned`          例题页中已归属某栏目的页数
      - `pages_unassigned`        例题页中**未归属任何栏目**的页数（旧数据/测试图，
                                  属可操作项：要么归属，要么清掉）

    实现只看 `manual_annotations/*.jsonl` 的**文件名**（不读内容），
    因此**不进 60s 缓存** —— 栏目增删要即时反映，而每次 /api/stats（10s 轮询）
    的全部开销只是 2 次小 JSON 读 + 一次目录列举。零 API、零 LLM。
    """
    pids = {str(p) for p in (project_ids or []) if p}
    assign = assignments if isinstance(assignments, dict) else {}
    with_ex: set = set()
    n_assigned = n_unassigned = 0
    ad = Path(annot_dir)
    if ad.exists():
        for jf in ad.glob("*.jsonl"):
            # `0000_官报_..._0001.png.jsonl` → 去掉 ".jsonl" 得到图名（与
            # project_assignments 的键一致）；旧数据有丢后缀的（`img1.jsonl`），
            # 故再退回按 stem 查一次——**双查而非猜**，查不到才算未归属。
            name = jf.name[:-len(".jsonl")]
            pid = assign.get(name) or assign.get(Path(name).stem)
            if pid and pid in pids:
                with_ex.add(pid)
                n_assigned += 1
            else:
                n_unassigned += 1
    return {
        "projects_total": len(pids),
        "projects_with_examples": len(with_ex),
        "pages_assigned": n_assigned,
        "pages_unassigned": n_unassigned,
    }


def flywheel_summary(runs_dir: Optional[Path] = None,
                     fewshot_path: Optional[Path] = None,
                     annot_dir: Optional[Path] = None,
                     status_path: Optional[Path] = None) -> Dict[str, int]:
    """聚合四阶段工作流的状态数字（60s TTL 缓存）。

    status_path 必传（或用默认外挂文件）——collect_items 的 status
    参数缺省是空 dict，不传会把已核/已修正全读成 pending
    （2026-09-06 实测踩坑：首批 9 条核完后飞轮仍显示待核 9）。
    """
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < TTL_S:
        return dict(_CACHE["data"])

    runs_dir = Path(runs_dir or review_store.DEFAULT_RUNS_DIR)
    fewshot_path = Path(fewshot_path or review_store.FEWSHOT_PATH)
    annot_dir = Path(annot_dir or (review_store.BASE_DIR / "manual_annotations"))
    status_path = Path(status_path or review_store.DEFAULT_STATUS_PATH)

    status = review_store.load_status(status_path)
    status_count = {"pending": 0, "checked": 0, "fixed": 0}
    for it in review_store.collect_items(runs_dir=runs_dir, status=status):
        status_count[it["status"]] = status_count.get(it["status"], 0) + 1
    n_runs = (sum(1 for d in runs_dir.glob("run_*") if d.is_dir())
              if runs_dir.exists() else 0)
    fewshot = 0
    if fewshot_path.exists():
        fewshot = sum(1 for ln in fewshot_path.read_text(encoding="utf-8").splitlines()
                      if ln.strip())

    data = {**status_count, "runs": n_runs, "fewshot": fewshot,
            **gold_summary(annot_dir)}
    _CACHE["ts"] = now
    _CACHE["data"] = data
    return dict(data)


def invalidate_cache() -> None:
    """人工核准状态变化后调用（/api/review/update），下轮轮询即见新数。"""
    _CACHE["data"] = None
