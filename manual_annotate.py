# -*- coding: utf-8 -*-
"""manual_annotate —— 人工标注层（金标准页）的读写单一实现（2026-09-14）。

**为什么单独成模块**（用户 2026-09-14 指出的缺口，原文）：

    「缺少了一个用户可以主动提供金标准页的入口，这个手动标注页面是在独立应用版本的
    界面中是有体现的，但是在技能包的版本中，这个功能应该也得加入……不然用户没有
    提供金标准页的渠道。」

人工标注的**写**原先只活在 Flask 路由函数体内（`app.py:api_manual_annotate`），
于是命令面（技能包）**根本够不到这条渠道**。本模块把这一层抽出来：
`app.py` 的两个路由改为**转调**（行为不变，全量回归兜底），
命令面（`chronicles annotate`）与界面**共用同一份实现**（单一实现）。

**金标准页是什么** —— `manual_annotations/<safe_name>.jsonl` 的一行：

    {"image_name": …, "box": [x1,y1,x2,y2], "text": …, "ts": …,
     "source": "manual", "attr": …, "profile": …, "note": …, "gid": …, "xp": …}

★ **`box` 是原图像素坐标 —— 这一点决定了「agent 不能替人标注」**：
  agent 读得懂文本、判得了属性，但它**画不出框**。所以命令面给的不是"让 agent 标注"，
  而是**「提框 + 由人定属性」**的通道：

      `page_lines()` 列出该页 OCR 行（编号 + 文本 + 框）
        → agent 转述给用户 → 用户定「第 N 行是某属性」
        → `add_annotation(line=N, attr=…)` 落盘

  这正是设计稿 §5.3「核心交互 = 裁决，不是标注」的**可执行形态**：
  系统/agent 提案，人定案，机器落盘。

★ **框的来源只有两处**（不许第三条）：
  ① `line=N` —— 取 `data/structured/<stem>.json` 的 OCR 行几何（**原生坐标**）；
  ② `box=[…]` —— 人显式给的坐标（= 界面里画框的等价物）。
  两者都给 → 歧义，报错；都不给 → 缺料，报错。**没有"猜一个框"这条路。**

★ **不重造任何一环**：
  · 路径   → `adjudicate.manual_path`（单一实现）
  · 读取   → `adjudicate.read_manual`（单一实现）
  · 整页写回 → `adjudicate.write_manual`（原子写 + 落 `_archive/`）
  · 页行   → `preannotate.page_lines`（几何复活 / 越界夹取 / 阅读序都在那一处）
  · 校验口径 → `profile_store` / `annotation_groups`（与界面同一份正则与上限）
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("manual_annotate")

__all__ = [
    "ValidateError", "MissingError",
    "manual_path", "read_manual", "list_annotations",
    "page_lines", "add_annotation", "remove_annotations",
]


class ValidateError(ValueError):
    """输入不合规。命令面据此回「用法错误 64」，界面据此回 400。"""


class MissingError(ValidateError):
    """**料不齐**（该页没有可用的 OCR 行）—— 与「参数写错」的下一步动作相反：
    前者要**去补材料**（先跑 `ocr`），后者要**改参数**。命令面据此回「缺料 3」。"""


# ============================================================
# 单一来源的转发（不另造）
# ============================================================
def manual_path(image_name: str, manual_dir=None) -> Path:
    import adjudicate as A
    return A.manual_path(image_name, manual_dir)


def read_manual(image_name: str, manual_dir=None) -> List[dict]:
    import adjudicate as A
    return A.read_manual(image_name, manual_dir)


def page_lines(image_name: str, *, structured_dir=None, outbox_dir=None) -> List[dict]:
    """该页 OCR 行（阅读序：`[{text, box:[x1,y1,x2,y2]}]`）—— agent 提框的**唯一材料**。

    转发 `preannotate.page_lines`：几何复活、越界夹取、RTL 阅读序都在那一处，
    这里只负责「按 image_name 找到 stem」。
    """
    import adjudicate as A
    import preannotate as PA
    stem = A.stem_of(image_name)
    return list(PA.page_lines(stem, structured_dir, outbox_dir) or [])


# ============================================================
# 读（带序号，便于按序号移除）
# ============================================================
def list_annotations(image_name: str, manual_dir=None) -> List[dict]:
    """该页已有的人工标注。每项多一个 `index`（**1 起**），供 `remove_annotations` 定位。"""
    out: List[dict] = []
    for i, it in enumerate(read_manual(image_name, manual_dir), 1):
        d = dict(it)
        d["index"] = i
        out.append(d)
    return out


# ============================================================
# 写：追加一条
# ============================================================
def _parse_line_spec(spec) -> List[int]:
    """`"1,2,5"` / `"1-3"` / `"1,3-5"` → `[1,2,5]` / `[1,2,3]` / `[1,3,4,5]`（去重、升序）。

    ★ 为什么需要它（2026-09-16 实测，用户「我要在哪里制作金标准页」那轮）：
      竖排古籍的一"行"= **整列的一段**，不是一条记录。实测样例材料
      `样例材料_0001.png`：

          行 1  x[819,925] y[ 300, 435]  時益號有限公     ← 标题被切成两格
          行 2  x[819,925] y[ 435, 570]  司
          行 3  x[868,929] y[ 581,1634]  光緒二十七年七月十四日郭玉泉創辦…  ← 高 1053 px

      ⇒ 单行 `--line` **既取不到跨格的标题**（"時益號有限公司"= 行 1+2），
        **也切不开长列**。而人在对话里能可靠给出的、且只需看一眼 OCR 文本
        就能给的判断，恰恰是「**哪几行合起来是一条**」。

      ⚠ 它**不解决**"在一列内部切出语义片段"（如从行 3 的 1053 px 里只取
        "光緒二十七年七月十四日"）——那是另一件事，别把它当万能切分器。
    """
    out: List[int] = []
    for part in str(spec or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:               # 用 [1:] 判区间，避开负号误判
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise ValueError(f"行号区间写法不对：{part!r}（应形如 3-5）")
            if hi < lo:
                raise ValueError(f"行号区间反了：{part!r}")
            out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                raise ValueError(f"行号必须是整数：{part!r}")
    if not out:
        raise ValueError("lines 是空的（要合并哪几行？）")
    return sorted(set(out))


def _pick_box(row: dict, pick: str) -> list:
    """在**一行内部**按文本切出一段的框 —— 人给文本，机器给框。

    ★ 为什么必须有它（2026-09-16，用户「用户应该如何制作金标准页」那轮）：
      `--line N` 给的是**整行**的框。而竖排古籍的一行 = 一整列的一段，
      实测 `样例材料_0001.png` 第 3 行高 **1053 px**
      （「光緒二十七年七月十四日郭玉泉創辦總號在香港大馬路分號設」，25 字）。
      金标准页要的是**语义片段**（"光緒二十七年七月十四日" ← 创立时间、
      "郭玉泉" ← 注册人一），不是整列。而**人在终端/对话里能可靠给出的是文本**，
      不是像素坐标 ⇒ 由文本反推框，人就不必"画框"。

    口径：**按字数比例在行的长轴上切**（竖排切 y、横排切 x）。

      ⚠ 这是**近似** —— 它假设字距均匀。竖排古籍近似成立，但它**不是**精确的
        排版测量；真要精确得走「列内水平投影找字缝」，那正是未修的 P1-1，
        别在这里另造第二份实现（§60）。⇒ 调用方应当把算出的框**从原图裁出来
        给人看一眼**再落盘（`annotate preview`），别让人盲签。
    """
    full = str(row.get("text") or "")
    n = len(full)
    if n == 0:
        raise MissingError("该 OCR 行没有文本，无法按文本定位（改用 --box 或 --line 整行）")
    p = str(pick or "")
    if not p:
        raise ValidateError("pick 是空的（要取该行里的哪一段？）")
    start = full.find(p)
    if start < 0:
        raise ValidateError(f"该行文本里找不到 {p!r}；该行是：{full[:40]}…")
    end = start + len(p)
    b = [float(v) for v in (row.get("box") or [])]
    if len(b) != 4:
        raise MissingError("该行缺几何（box），无法切分")
    x1, y1, x2, y2 = b
    if (y2 - y1) >= (x2 - x1):                    # 竖排：文本沿 y 排
        span = y2 - y1
        return [x1, y1 + span * start / n, x2, y1 + span * end / n]
    span = x2 - x1                                # 横排：文本沿 x 排
    return [x1 + span * start / n, y1, x1 + span * end / n, y2]


def _resolve_box(image_name: str, *, line, lines, box, text, pick,
                 structured_dir, outbox_dir) -> tuple:
    """定框：返回 `(box, text)`。**四档来源，且不许两处并用。**

    ① `line=N`     第 N **行**的框（= 该列那一段）
    ② `lines=spec` 多行的**并集框**（`"1,2"` / `"1-3"`）—— 竖排里一条记录常常跨列/跨格
    ③ `box=[…]`    人显式给的坐标（= 界面拖框的等价物）
    ④ `pick="…"`   **与 ① 合用**：只取该行里的这一个子串，按字数比例切出框
                   —— 这是「人在终端里给不出坐标，但给得出文本」的正解

    ⚠ ① ② 的粒度是 OCR 行：`lines` 能拼回被切碎的标题，但**切不开**一条 1000+ px 的
      长列；要切开就用 ④。三者的框都是**近似**，落盘前该给人看一眼（`preview`）。
    """
    given = [(n, v) for n, v in (("line", line), ("lines", lines), ("box", box))
             if v is not None]
    if len(given) > 1:
        raise ValidateError("框的来源只能有一处，收到 %d 处：%s（歧义）"
                            % (len(given), " + ".join(n for n, _ in given)))
    kind = given[0][0] if given else ""
    if kind in ("line", "lines"):
        rows = page_lines(image_name, structured_dir=structured_dir,
                          outbox_dir=outbox_dir)
        if not rows:
            raise MissingError(
                f"该页取不到 OCR 行：{image_name} —— 先跑 `chronicles ocr`，"
                f"或改用 box 显式给坐标")
        if kind == "line":
            try:
                n = int(line)
            except (TypeError, ValueError):
                raise ValidateError(f"line 必须是整数：{line!r}")
            if n < 1 or n > len(rows):
                raise ValidateError(f"line 越界：{n}（本页共 {len(rows)} 行，编号从 1 起）")
            idxs = [n]
        else:
            if pick:
                raise ValidateError("pick 只能与 --line 合用（一次只在**一行**里切）")
            try:
                idxs = _parse_line_spec(lines)
            except ValueError as e:
                raise ValidateError(str(e))
            bad = [n for n in idxs if n < 1 or n > len(rows)]
            if bad:
                raise ValidateError(
                    f"行号越界：{bad}（本页共 {len(rows)} 行，编号从 1 起）")
        sel = [rows[n - 1] for n in idxs]
        if pick:
            box = _pick_box(sel[0], pick)
            if not text:
                text = str(pick)
        else:
            boxes = [list(r.get("box") or []) for r in sel]
            if not all(len(b) == 4 for b in boxes):
                raise MissingError(f"该页有行缺几何（box），取不到框：{image_name}")
            # 并集框 —— 多行合成一个矩形；text 按**给的顺序**（已升序=阅读序）拼接
            box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                   max(b[2] for b in boxes), max(b[3] for b in boxes)]
            if not text:
                text = "".join(str(r.get("text") or "") for r in sel)
    elif pick:
        raise ValidateError("pick 只能与 --line 合用（--box 已经是显式坐标了）")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValidateError(
            "box 必须是 [x1,y1,x2,y2]（或用 line=N / lines=1,2 从 OCR 行取框）")
    try:
        box = [int(round(float(v))) for v in box]
    except (TypeError, ValueError):
        raise ValidateError(f"box 必须是 4 个数字：[x1,y1,x2,y2]，收到 {box!r}")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ValidateError("text 必须是字符串")
    return box, text


def _opt_str(value, label: str) -> str:
    """可选字段的**类型**口径：非字符串一律拒，**不做 `str()` 强转**。

    ⚠ 这是转调时踩过的坑（2026-09-14，全量回归抓出）：原 `app.py` 路由对
      `attr`/`profile`/`note`/`gid`/`xp` 逐个 `isinstance(…, str)` 检查 → 400；
      转调后若图省事写成 `str(v or "")`，界面的 **400 会变成 200**，
      并把坏值写进**金标准**（实测落盘 `"attr": "123"`，来源 `attr: 123`）。
      ⇒ 类型检查与格式检查**分层**：本函数管类型，`_validate_optional` 管格式。

    `None` 视为未提供（等价空串）；命令面（argparse）永远给字符串，
    所以这条只对界面 + 直调 API 生效 —— 但两处共用同一份口径。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidateError(f"{label} 必须是字符串")
    return value.strip()


def _validate_optional(*, attr: str, profile: str, gid: str, xp: str) -> None:
    """与界面**同一份**口径（正则/上限都取自单一来源模块）。"""
    if profile:
        import profile_store as PS
        if not PS.PROFILE_ID_RE.match(profile):
            raise ValidateError("非法 profile_id（须形如 prof_*）")
    if attr:
        import profile_store as PS
        if len(attr) > PS.MAX_ATTR_NAME:
            raise ValidateError(f"attr 超长（{len(attr)} > {PS.MAX_ATTR_NAME} 字符）")
    if gid:
        import annotation_groups as AG
        if not AG.GID_RE.match(gid):
            raise ValidateError("非法 gid（限字母数字_-，≤40 字符）")
    if xp and xp not in ("prev", "next"):
        raise ValidateError("xp 只能为空 / prev / next")


def annotate_exit_code(r: dict) -> int:
    """唯一判定点：add/remove 的结果 `ok` ⇒ 0，否则 1。

    `chronicles annotate add/remove` 取用本函数，包装层不重判。
    注意：缺料（该页无 OCR 行 → `MissingError`）与参数错（`ValidateError`）
    在命令面入口就被接住分投 3 / 64 —— 走到这里的失败都是数据层写盘层面的事。
    """
    return 0 if r.get("ok") else 1


def add_annotation(*, image_name: str, box=None, text: str = "",
                   attr: str = "", profile: str = "", note: str = "",
                   gid: str = "", xp: str = "", line=None, lines=None,
                   pick=None, structured_dir=None, outbox_dir=None,
                   manual_dir=None) -> Dict[str, Any]:
    """追加一条人工标注（`source: manual`）。

    返回 `{"ok": bool, "saved": dict|None, "file": str, "error": str}`。
    输入不合规 → 抛 `ValidateError`（命令面转成退出码 64）。

    空的可选字段**不落键**（旧行零迁移；与界面口径一致）。

    ★ 三种"框从哪来"（对话/终端形态下不必画框）：
      · `lines="1,2"` / `"1-3"`  多行**并集框**，文本按行序拼接 ——
        竖排一条记录常被切格（实测标题「時益號有限公司」= 行 1+2）
      · `line=N, pick="…"`       在**一行内部**按文本切出这一段 ——
        切得开 1000+ px 的长列（见 `_pick_box`；是近似，落盘前该给人看一眼）
      · `box=[x1,y1,x2,y2]`      显式坐标（= 界面拖框的等价物）
    """
    image_name = str(image_name or "").strip()
    if not image_name:
        raise ValidateError("image_name 必填（形如 0001.png）")

    box, text = _resolve_box(image_name, line=line, lines=lines, box=box,
                             text=text, pick=pick,
                             structured_dir=structured_dir, outbox_dir=outbox_dir)

    # ★ 先过**类型**关（非字符串直接拒，见 `_opt_str`），再做去空白 —— 顺序不能反，
    #   反了就等于用 `str()` 把坏值洗成合法值（界面 400 会静默变 200）。
    attr = _opt_str(attr, "attr/note")
    profile = _opt_str(profile, "profile")
    note = _opt_str(note, "attr/note")
    gid = _opt_str(gid, "gid")
    xp = _opt_str(xp, "xp")
    _validate_optional(attr=attr, profile=profile, gid=gid, xp=xp)

    sample: Dict[str, Any] = {
        "image_name": image_name,
        "box": list(box),
        "text": text.strip(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "manual",
    }
    for k, v in (("attr", attr), ("profile", profile), ("note", note),
                 ("gid", gid), ("xp", xp)):
        if v:
            sample[k] = v

    out_file = manual_path(image_name, manual_dir)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("[manual_annotate] 写入失败 %s: %s", out_file.name, e)
        return {"ok": False, "saved": None, "file": str(out_file), "error": str(e)}
    log.info("[manual_annotate] 追加 1 条 → %s（attr=%s）", out_file.name, attr or "-")
    return {"ok": True, "saved": sample, "file": str(out_file), "error": ""}


# ============================================================
# 写：按序号移除（整页写回）
# ============================================================
def remove_annotations(image_name: str, indices: Sequence[int], *,
                       manual_dir=None, archive_dir=None) -> Dict[str, Any]:
    """按序号（**1 起**）移除标注，**整页写回**。

    写回复用 `adjudicate.write_manual`（原子写 + 先落 `_archive/`）—— 不另造。
    """
    items = read_manual(image_name, manual_dir)
    n = len(items)
    drop = set()
    for i in indices or []:
        try:
            k = int(i)
        except (TypeError, ValueError):
            raise ValidateError(f"index 必须是整数：{i!r}")
        if k < 1 or k > n:
            raise ValidateError(f"index 越界：{k}（本页共 {n} 条，编号从 1 起）")
        drop.add(k - 1)
    if not drop:
        raise ValidateError("没有给 index（要移除哪几条？）")
    kept = [it for j, it in enumerate(items) if j not in drop]
    import adjudicate as A
    p = A.write_manual(image_name, kept, manual_dir, archive_dir)
    log.info("[manual_annotate] 移除 %d 条，余 %d 条 → %s", len(drop), len(kept), p.name)
    return {"ok": True, "removed": len(drop), "left": len(kept),
            "file": str(p), "error": ""}
