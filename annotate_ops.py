# -*- coding: utf-8 -*-
"""annotate_ops —— 标注交互面的**领域操作**（框架无关的单一实现）。

★★ 为什么有这个模块（2026-09-16 · 技能包形态重构 批次 1）

    标注交互面（`/annotate/<页>` + 12 条 `/api/*`）里，有三段逻辑原先
    **只内联在 `app.py` 的 HTTP 处理函数里**，内核够不到：

    | 段 | 原落点 | 归宿 |
    |---|---|---|
    | 图名净化后的**同槽碰撞**上报 | `app._name_collisions` · L1743 | `name_collisions()` |
    | 漂移查询的**档案 id 逐级回落** | `app._resolve_profile_id` · L2119 | `resolve_profile_id()` |
    | 整份 jsonl **覆写**（编辑/删除后） | `app.api_rewrite_manual` 函数体 · L2374 | `rewrite_manual()` |

    技能包要拥有自己的交互面而**不 import app**；这三段若不下来，
    交互面只能照抄 → **第二份真相**（`CLAUDE.md` §60）。
    ⇒ 下沉为内核，两个宿主**转调同一实现**。

★ 本模块**不 import flask**（内核保持框架无关）。所有目录参数**由调用方传入**
  （不是模块全局）—— 这是本项目「受保护目录：可被测试 patch 的目录必须传下去」
  那条纪律（踩过 2 次）的直接落地。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["name_collisions", "resolve_profile_id", "rewrite_manual",
           "latest_image", "find_page_image",
           "parse_correction_payload", "save_correction",
           "note_path", "read_notes", "add_note"]


# ============================================================
# 1. 图名同槽碰撞（原 app._name_collisions）
# ============================================================
def name_collisions(image_name: str, scan_dir=None) -> List[str]:
    """该图名净化后是否与其他图**同槽**（共用同一个 jsonl）——M9 碰撞检测。

    背景（方案 §10.4 批 1，对照 X-AnyLabeling `_deduplicated_filename` /
    `_target_name_exists`）：批量导入**保留原文件名**，而 `data_io.safe_name`
    会把空格等替换为 `_`，于是 `a b.png` 与 `a_b.png` 落到同一个 `<safe>.jsonl`
    ——后写覆盖先写，且删一个会连带删另一个的标注。

    **只检测、只上报，不改名**：jsonl 名必须能由 image_name 单值推导
    （`safe_name` 是纯函数），改名会让"图名 → jsonl"不再可逆，制造第二处不一致。
    故此处把冲突**显式暴露**（日志 + 响应字段），与 P5 的 `source.excluded` 同旨
    ——可见不静默。

    返回：**同槽的其他文件名列表**（不含自身）；目录不可读 → `[]`。
    """
    import data_io
    d = Path(scan_dir) if scan_dir else Path(".")
    try:
        names = [p.name for p in d.iterdir() if p.is_file()]
    except OSError:
        return []
    key = data_io.safe_name(image_name)
    return sorted({n for n in names if n != image_name and data_io.safe_name(n) == key})


# ============================================================
# 2. 档案 id 逐级回落（原 app._resolve_profile_id）
# ============================================================
def resolve_profile_id(profile_id: Optional[str], image_name: str = "",
                       *, drafts_dir=None, contracts_dir=None) -> Optional[str]:
    """档案 id 解析：显式参数 → 该页草稿记录的档案 → 磁盘上唯一的契约。

    刻意做成"逐级回落"而非强制传参：漂移查询是**诊断性**入口，
    不该因为调用方没带参数就 400。
    """
    if profile_id:
        return str(profile_id).strip() or None
    if image_name:
        try:
            import adjudicate
            drafts = adjudicate.load_drafts(adjudicate.stem_of(image_name))
            if drafts:
                p = str(drafts[0].get("profile") or "").strip()
                if p:
                    return p
        except Exception:
            pass
    try:
        import layout_contract
        base = Path(contracts_dir) if contracts_dir else Path(layout_contract.DEFAULT_CONTRACTS_DIR)
        contracts = sorted(base.glob("layout_*.json"))
        if len(contracts) == 1:
            return contracts[0].name[len("layout_"):-len(".json")]
    except Exception:
        pass
    return None


# ============================================================
# 3. 整份 jsonl 覆写（原 app.api_rewrite_manual 函数体）
# ============================================================
def rewrite_manual(image_name: str, items: List[dict], manual_dir) -> Dict[str, Any]:
    """用 `items` **整份覆写**该页的标注 jsonl（编辑 / 删除后走这条）。

    ★ 逐字沿用原语义：每个 item 一行 `json.dumps(..., ensure_ascii=False)`，
      字段**原样透传**（`attr` / `profile` / `xp` 等新字段不得被吞）。

    ★ 一处**有意的实现升级**（2026-09-16）：原实现是裸 `open(w)` —— 先截断再写，
      中途崩 = 该页标注全没。本版改为 **同目录临时文件 + `os.replace` 原子替换**
      （与 `adjudicate._write_manual` 同口径）。成功路径的观感与产物**逐字节相同**；
      差别只在崩溃窗口。**未加归档**：那会改变产物面，留待裁定。

    返回 `{"ok": bool, "count": int, "path": str}`；失败 → `ok=False` + `error`。
    """
    import data_io
    out_file = Path(manual_dir) / ("%s.jsonl" % data_io.safe_name(image_name))
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "count": 0, "error": str(e), "path": str(out_file)}

    payload = "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items)
    tmp_path = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(out_file.parent), prefix=".rewrite_", suffix=".tmp")
        tmp_path = Path(tmp)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp_path, out_file)               # 原子替换
        tmp_path = None
    except OSError as e:
        return {"ok": False, "count": 0, "error": str(e), "path": str(out_file)}
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                os.remove(tmp_path)
            except OSError:                          # pragma: no cover
                pass
    return {"ok": True, "count": len(items), "path": str(out_file)}


# ============================================================
# 4. 最新图扫描（原 app.annotate_latest / annotate_server.r_page_latest 内联）
# ============================================================
def latest_image(dirs, exts) -> Optional[Path]:
    """按 mtime 找最新一张图像：目录按传入顺序即优先级；一张都没有 → `None`。

    `dirs` 里 `None` / 不存在的目录跳过；`exts` 是允许的后缀集合（小写比较）。
    同 mtime 时按路径倒序取（与原内联实现的 `sort(reverse=True)` 一致）。
    """
    cands: List[tuple] = []
    for d in dirs:
        if d is None or not Path(d).exists():
            continue
        try:
            entries = list(Path(d).iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            try:
                cands.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


# ============================================================
# 5. 页图两处查（OUTBOX → INBOX，逐处转调唯一解析口）
# ============================================================
def find_page_image(name, outbox, inbox) -> Optional[Path]:
    """页名 → 页图路径：按优先级两处查，每处都转调 `config.image_path_of`。

    逐处传 `given=` 是刻意的：不给 `given` 时 `image_path_of` 会搜索 config
    的全局 IMAGE_DIRS，交互面与测试 patch 过的目录会被绕开。
    """
    import config
    for d in (outbox, inbox):
        if d is None:
            continue
        p = config.image_path_of(name, given=d)
        if p is not None:
            return p
    return None


# ============================================================
# 6. OCR 校对回写（原 app.api_save / annotate_server.r_save 内联两连招）
# ============================================================
def parse_correction_payload(data):
    """校对请求体 → `(record_id, corrected)`；不合契约 → `ValueError`（文案即 400 响应）。"""
    if not isinstance(data, dict):
        raise ValueError("invalid JSON body")
    raw_id = data.get("id")
    corrected = data.get("corrected_text", "")
    if raw_id is None:
        raise ValueError("missing id")
    try:
        record_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError(f"id must be int, got {raw_id!r}") from None
    if not isinstance(corrected, str):
        raise ValueError("corrected_text must be string")
    return record_id, corrected


def save_correction(excel_writer, record_id: int, corrected: str) -> bool:
    """校对文本写回 xlsx；成功则标记 has_manual_correction（knowledge_card 读它）。"""
    ok = excel_writer.update_corrected(record_id, corrected)
    if ok:
        excel_writer.update_field(record_id, "has_manual_correction", True)
    return ok


# ============================================================
# 8. 页级备注（给 AI 的提醒通道；2026-09-26）
# ============================================================
#: 既有落盘格式（manual_annotations/notes/<image_name>.notes.jsonl，每行一行）：
#:   {"box": [x1,y1,x2,y2]?, "note": str, "ts": iso, "attr_hint"/"attr"?: str,
#:    "source"?: str}
#: 本模块只**追加**（受保护目录：不删、不重写既有行），读取容忍坏行。
def note_path(image_name: str, notes_dir) -> Path:
    """页名 → 备注文件路径（净化口径与 `adjudicate.manual_path` 同一实现）。"""
    import data_io
    return Path(notes_dir) / f"{data_io.safe_name(image_name)}.notes.jsonl"


def read_notes(image_name: str, notes_dir) -> List[dict]:
    """读某页备注列表；文件缺失 → 空列表（调用方零分支）。只读，绝不创建。"""
    p = note_path(image_name, notes_dir)
    if not p.exists():
        return []
    out: List[dict] = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                it = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(it, dict):
                out.append(it)
    except OSError as e:
        # 读失败按空处理但不静默 —— 备注是提醒通道，丢了人要能查到原因
        import logging
        logging.getLogger("annotate_ops").warning(
            "[notes] 备注读取失败 %s: %s", p.name, e)
    return out


def add_note(image_name: str, note: str, notes_dir, box=None,
             attr: str = "", source: str = "ui") -> Dict[str, Any]:
    """追加一条页级备注（**只追加**，文件不存在则创建目录与文件）。

    `box` 可选（附带当前选中条目的框，供 AI 定位是哪一条）；
    `attr` 可选（备注关联的属性名，写入 `attr_hint` 与既有文件口径一致）。
    """
    import data_io
    from datetime import datetime
    val = str(note or "").strip()
    if not val:
        return {"ok": False, "error": "note 必填（不能为空）"}
    rec: Dict[str, Any] = {
        "note": val,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": str(source or "ui"),
    }
    if box is not None:
        try:
            rec["box"] = [float(v) for v in box]
        except (TypeError, ValueError):
            return {"ok": False, "error": "box 必须是四数数组"}
    a = str(attr or "").strip()
    if a:
        rec["attr_hint"] = a
    d = Path(notes_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = note_path(image_name, d)
    # 追加语义：先渲染整行再一次性写（同 data_io.atomic_write_jsonl 的先渲染后落盘）
    data_io.atomic_write_text(
        p, (p.read_text(encoding="utf-8") if p.exists() else "") +
        json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(p), "note": rec}
