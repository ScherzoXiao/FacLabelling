"""P1d（2026-09-04）：跨列续框——属性标注分组聚合（方案 §4.7 增补）。

背景（用户标注官报实测反馈）：竖排分栏版面中一个属性值常被列截断——
"公司名五个字，第一列底下俩字，第二列开头仨字"，单个矩形框无法从列底追到列头。

数据模型（逐行存储 + gid 归组，向后兼容）：
- 一条属性标注 = jsonl 中共享同一 gid 的相邻/任意行序列，每行一个 box + 该段文本；
- 行在文件中的顺序 = 拼接顺序 = 用户标注顺序（竖排右→左，用户按阅读顺序画段）；
- 无 gid 的行 = 传统单框标注，自成一条（旧 JSONL 零迁移）；
- 同组各行均带 attr/profile（每行自足），组内属性以首行为准、编辑时前端传播同步；
- 可选 xp 字段（P1d-x，2026-09-06）：跨页续接标记——"prev"=承上页（本段是
  上一页末条记录的延续）、"next"=接下页（本段所属记录延续到下一页）。
  组内传播同 attr；聚合取组内首个非空。下游跨页拼记录（金标准对照、
  few-shot 构建、批量合并）一律经本字段判断，不得另造约定。

聚合本模块是下游唯一入口：金标准对照（1d golden）、few-shot 注入（2a）、
模板归纳器（3a）消费属性标注一律走 aggregate()，不得各自重写分组逻辑。
"""
from __future__ import annotations

import re
import time
import uuid
from typing import List, Optional

# gid 白名单：与 annotate.html 前端 newGid() 同格式（g + base36 时间戳 + 4 位随机）。
# app.py /api/manual_annotate 引用本常量做校验（单一来源）。
GID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def new_gid() -> str:
    """生成新组 id（前端 newGid() 同构，脚本/测试用）。"""
    return f"g{format(int(time.time() * 1000), 'x')}{uuid.uuid4().hex[:4]}"


def aggregate(items: List[dict]) -> List[dict]:
    """标注行列表 → 聚合属性标注列表。

    输入：jsonl 行数组（dict），容忍缺字段/坏行（跳过非 dict 与缺 box 的行）。
    输出：每条 {gid, attr, profile, note, text, boxes, segments, ts, source}——
    - gid=None：无组行（含传统无属性框），单 box 自一条；
    - gid 组：text = 行序拼接，boxes = 行序 box 列表，segments = 段文本列表；
      attr/profile/note 取组内首个非空值（正常应各行一致，前端编辑已传播）；
    - 输出顺序 = 首次出现顺序（组按组首行位置，无组行按自身位置）。
    """
    out: List[dict] = []
    index: dict = {}  # gid -> 输出条目
    for it in items:
        if not isinstance(it, dict):
            continue
        box = it.get("box")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        gid = it.get("gid") or None
        text = str(it.get("text", ""))
        if gid is None:
            out.append({
                "gid": None,
                "attr": it.get("attr") or "",
                "profile": it.get("profile") or "",
                "note": it.get("note") or "",
                "xp": it.get("xp") or "",
                "text": text,
                "segments": [text],
                "boxes": [box],
                "ts": it.get("ts") or "",
                "source": it.get("source") or "",
            })
            continue
        entry = index.get(gid)
        if entry is None:
            entry = {
                "gid": gid,
                "attr": it.get("attr") or "",
                "profile": it.get("profile") or "",
                "note": it.get("note") or "",
                "xp": it.get("xp") or "",
                "text": text,
                "segments": [text],
                "boxes": [box],
                "ts": it.get("ts") or "",
                "source": it.get("source") or "",
            }
            index[gid] = entry
            out.append(entry)
            continue
        entry["text"] += text
        entry["segments"].append(text)
        entry["boxes"].append(box)
        for key in ("attr", "profile", "note", "xp"):
            if not entry[key] and it.get(key):
                entry[key] = it[key]
        if not entry["ts"] and it.get("ts"):
            entry["ts"] = it["ts"]
    return out


def attr_annotation_count(items: List[dict]) -> int:
    """属性标注条数（gid 组计 1，无组行各计 1）——annotate.html 头部计数同口径。"""
    return sum(1 for a in aggregate(items) if a["attr"])


def first_box(items: List[dict], gid: str) -> Optional[list]:
    """取某组首段 box（下游画示意框用）；组不存在 → None。"""
    for a in aggregate(items):
        if a["gid"] == gid:
            return a["boxes"][0]
    return None
