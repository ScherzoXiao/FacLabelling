# -*- coding: utf-8 -*-
"""S4 待核工作台数据层（复杂版面方案 §4.5，2026-09-06）。

消费 divergence_sampler 落盘的对账报告
（data/divergence/run_<ts>/<stem>.json，reconciliation.review_items =
[{field, value, reason, rule, source}]，source ∈ rule/numeric/confidence），
拍平为跨 run 的待核队列；状态外挂存储（不动原始报告文件）。

设计参照（X-AnyLabeling 质检闭环吸收，用户 2026-09-06 拍板）：
- score 置信度随行展示（field_confidence，最低置信度优先排序，方案 §4.5）；
- checked 三态：pending（待核）/ checked（人工通过）/ fixed（已修正）；
- 错例回流：fixed 且带修正值 → data/fewshot/corrections.jsonl
  （按 profile+field 分桶，去重、每桶上限 30，方案 §4.5 few-shot 池雏形）。

纪律：本模块只做队列管理与状态回写，不做归纳；原始报告只读。
"""
from __future__ import annotations

import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger("review_store")

import data_io   # 原子写 / 文件名净化的单一实现（方案 §10.4 批 1）
import feedback  # 回流池的桶键口径（写入与读取同源，避免静默分叉）

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录（postbuild 联接真实数据）
else:
    _BASE = Path(__file__).parent.resolve()
BASE_DIR = _BASE
DEFAULT_RUNS_DIR = BASE_DIR / "data" / "divergence"
REVIEW_DIR = BASE_DIR / "data" / "review"
DEFAULT_STATUS_PATH = REVIEW_DIR / "status.json"
FEWSHOT_PATH = BASE_DIR / "data" / "fewshot" / "corrections.jsonl"
FEWSHOT_BUCKET_CAP = 30   # 方案 §4.5：每桶上限约 30 例

VALID_ACTIONS = ("checked", "fixed", "reset")


# ============================================
# 报告扫描 → 队列拍平
# ============================================
def iter_run_reports(runs_dir: Path) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """扫描 run_<ts>/<stem>.json 报告（summary.json 跳过；损坏文件告警跳过）。

    yields (run_name, stem, report)；run_name = 目录名去掉 "run_" 前缀
    （与 sampler 的时间戳命名对应，key 更短）。
    """
    for run_dir in sorted(Path(runs_dir).glob("run_*")):
        if not run_dir.is_dir():
            continue
        run_name = run_dir.name[4:]   # "run_<ts>" → "<ts>"
        for rep_path in sorted(run_dir.glob("*.json")):
            if rep_path.name == "summary.json":
                continue
            try:
                rep = json.loads(rep_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("[review_store] 报告读取失败 %s: %s",
                            rep_path.name, e)
                continue
            if not isinstance(rep, dict):
                continue
            # 解析失败的报告不进队列（read 阶段全败 → 全字段空值假待核，
            # 2026-09-06 实测：被中断的死 run 污染队列，重跑才有核的意义）
            pm = ((rep.get("read") or {}).get("parse_meta") or {})
            if pm.get("parse_ok") is False:
                log.info("[review_store] 跳过解析失败报告 %s/%s",
                         run_name, rep_path.stem)
                continue
            yield run_name, rep_path.stem, rep


def collect_items(runs_dir: Path = DEFAULT_RUNS_DIR,
                  status: Optional[Dict[str, Dict[str, Any]]] = None
                  ) -> List[Dict[str, Any]]:
    """拍平全部报告的 review_items 为待核队列。

    排序（方案 §4.5）：待核优先，其后按置信度升序（最低置信度在前）、
    stem、field 稳定排序。confidence 缺失视为 1.0（排最后段）。
    """
    status = status or {}
    items: List[Dict[str, Any]] = []
    for run, stem, rep in iter_run_reports(runs_dir):
        rec = rep.get("reconciliation") or {}
        review = rec.get("review_items") or []
        if not isinstance(review, list):
            continue
        conf_map = rec.get("field_confidence") or {}
        profile_id = str(rep.get("profile_id", ""))
        image = Path(str(rep.get("image", f"{stem}.png"))).name
        for it in review:
            if not isinstance(it, dict) or not it.get("field"):
                continue
            field = str(it["field"])
            key = f"{run}::{stem}::{field}"
            st = status.get(key, {})
            items.append({
                "key": key, "run": run, "stem": stem, "image": image,
                "profile_id": profile_id,
                "field": field,
                "value": str(it.get("value", "")),
                "reason": str(it.get("reason", "")),
                "rule": str(it.get("rule", "")),
                "source": str(it.get("source", "rule")),
                "record": str(it.get("record", "")),
                "confidence": (float(conf_map[field])
                               if isinstance(conf_map.get(field), (int, float))
                               else None),
                "page_confidence": (float(rec["page_confidence"])
                                    if isinstance(rec.get("page_confidence"),
                                                  (int, float)) else None),
                "status": str(st.get("status", "pending")),
                "fix": str(st.get("fix", "")),
                "note": str(st.get("note", "")),
                "ts": str(st.get("ts", "")),
            })
    items.sort(key=lambda x: (
        0 if x["status"] == "pending" else 1,
        x["confidence"] if x["confidence"] is not None else 1.0,
        x["stem"], x["field"]))
    return items


def find_item(runs_dir: Path, key: str,
              status: Optional[Dict[str, Dict[str, Any]]] = None
              ) -> Optional[Dict[str, Any]]:
    """按 key 精确查找单条（update 前校验 + few-shot 回流取数）。"""
    for it in collect_items(runs_dir, status=status):
        if it["key"] == key:
            return it
    return None


# ============================================
# 状态存储（外挂 JSON，原子写）
# ============================================
def load_status(path: Path = DEFAULT_STATUS_PATH) -> Dict[str, Dict[str, Any]]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_status(status: Dict[str, Dict[str, Any]],
                path: Path = DEFAULT_STATUS_PATH) -> None:
    """原子写（**单一实现** `data_io.atomic_write_text`：tmp + fsync + os.replace）。"""
    data_io.atomic_write_json(Path(path), status, indent=1)


def update_item(key: str, action: str, fix: str = "", note: str = "",
                runs_dir: Path = DEFAULT_RUNS_DIR,
                status_path: Path = DEFAULT_STATUS_PATH,
                fewshot_path: Path = FEWSHOT_PATH) -> Dict[str, Any]:
    """更新单条状态。action ∈ checked / fixed / reset。

    fixed 且带修正值 → 错例回流 few-shot 池（append_fewshot）。
    返回 {"ok": True, "item": {...}} 或 {"ok": False, "error": "..."}。
    """
    if action not in VALID_ACTIONS:
        return {"ok": False, "error": f"非法 action（{VALID_ACTIONS}）"}

    status = load_status(status_path)
    now = datetime.now().isoformat(timespec="seconds")

    if action == "reset":
        status.pop(key, None)
        save_status(status, status_path)
        return {"ok": True, "item": {"key": key, "status": "pending"}}

    item = find_item(runs_dir, key, status=status)
    if not item:
        return {"ok": False, "error": f"待核项不存在: {key}"}

    if action == "checked":
        entry = {"status": "checked", "fix": "", "note": note, "ts": now}
    else:   # fixed
        fix = fix.strip()
        if not fix:
            return {"ok": False, "error": "修正值不能为空（修正即回流样本）"}
        entry = {"status": "fixed", "fix": fix, "note": note, "ts": now}
        if fix != item["value"]:
            append_fewshot({
                "profile_id": item["profile_id"], "stem": item["stem"],
                "field": item["field"], "wrong": item["value"],
                "right": fix, "rule": item["rule"], "run": item["run"],
                "ts": now,
            }, path=fewshot_path)
    status[key] = entry
    save_status(status, status_path)
    return {"ok": True, "item": {"key": key, **entry}}


# ============================================
# 错例回流 few-shot 池（§4.5 雏形）
# ============================================
def append_fewshot(entry: Dict[str, Any],
                   path: Path = FEWSHOT_PATH,
                   bucket_cap: int = FEWSHOT_BUCKET_CAP) -> bool:
    """追加修正样本；同桶（profile+field）内 wrong→right 重复跳过；
    桶满（bucket_cap）则挤掉最旧一条。返回是否实际写入。

    **原子写**（批 1 纪律）：原为裸 `open(path, "w")` 覆盖写 —— 崩溃会留半截文件，
    而这条路径是**持续追加**的池子，半截即等于此前积累全废。
    桶键口径转发 `feedback.bucket_key`（**单一实现**：写入与读取同源，
    避免"按一套分桶写、按另一套分桶读"的静默分叉）。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lines: List[Dict[str, Any]] = []
    if Path(path).exists():
        for ln in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                lines.append(json.loads(ln))
            except json.JSONDecodeError:
                continue

    bucket_key = feedback.bucket_key(entry)
    dup = any(feedback.bucket_key(e) == bucket_key
              and e.get("wrong") == entry.get("wrong")
              and e.get("right") == entry.get("right") for e in lines)
    if dup:
        return False
    bucket = [e for e in lines if feedback.bucket_key(e) == bucket_key]
    if len(bucket) >= bucket_cap:
        # 挤掉桶内最旧一条（列表序 = 追加序）
        for i, e in enumerate(lines):
            if feedback.bucket_key(e) == bucket_key:
                del lines[i]
                break

    lines.append(entry)
    data_io.atomic_write_jsonl(path, lines)
    return True
