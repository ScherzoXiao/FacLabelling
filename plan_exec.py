# -*- coding: utf-8 -*-
"""层 3 **参照执行器**（P-C）：plan + 目标页 → 合规的 `annotation_result`。

## 它证明什么

接缝要成立，必须能拿出一个**只吃 plan** 的执行器——不读 `learned_*.json`、
不读 `profile_store`、不读 `data/layout_contracts/*.json`。本模块就是那个样本：

    spec      = spec_from_plan(plan)        # L0 的全部规则，来自 plan.split_spec
    contract  = contract_facade(plan)       # anchor_page 需要的最小契约门面
    records   = RS.records_of(units, spec)
    entries   = PA.anchor_page(records, stem, contract)

除此之外不再读任何规则来源。**所以这个执行器是可替换的**：换掉它，plan 不变。

## 零 token

只实现降级链里的 `rule` 与 `single_line` 两级（都不调模型）。`l1` / `l2` 是
生产路径（`preannotate_gen`）的层级，参照实现故意不含——模型的接入不是接缝的
验证对象，**接缝验证的是"plan 够不够用"**。

## 版式覆盖（P0-3b，2026-09-13）

`plan` 会声明某些页的版式族"只有单页证据"（`page_model.regions[].covered == False`）。
本执行器据此**不按主族骨架切分**，直接降级为整页一条兜底（见 `run_page` 的注释：
硬切会产伪命中，人工复核成本远高于一次 API 调用）。这个决定写进产物的
`result["layout"]`（`{covered, policy, cluster, n_regions, known}`），
`seam.validate` 会据此告警 —— **执行器做了什么，必须留在产物里能被追到**。

用法：
    python -u plan_exec.py --profile <pid> [--stem <stem>] [--out data/results]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import seam as SEAM                                              # noqa: E402

DEFAULT_PLAN_DIR = ROOT / "data" / "plans"
DEFAULT_RESULT_DIR = ROOT / "data" / "results"
DEFAULT_EXCLUSIONS_PATH = ROOT / "data" / "page_exclusions.json"
EXECUTOR_NAME = "plan_exec(reference, zero-token)"


# ============================================================
# 排除带消费（P5，2026-09-26）：人工裁决的「非本页内容」区
# ============================================================
def load_page_exclusions(path=None) -> dict:
    """`page_exclusions.json` → dict（读不到 / 解析不动 → `{}`，零影响）。

    文件由人工在标注台登记（键 = image_name 含 .png 后缀），本层**只读**。
    """
    p = Path(path) if path else DEFAULT_EXCLUSIONS_PATH
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def zones_of(exclusions: dict, stem: str) -> List[List[float]]:
    """某页的排除带矩形 `[[x0,y0,x1,y1], …]`；无登记 / 框非法 → `[]`。"""
    e = exclusions.get(f"{stem}.png") or exclusions.get(stem) or {}
    out: List[List[float]] = []
    for z in (e.get("zones") or []):
        b = (z or {}).get("box")
        try:
            r = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
        except (TypeError, ValueError, IndexError):
            continue
        if r[2] > r[0] and r[3] > r[1]:
            out.append(r)
    return out


def _exclusion_split(units: Sequence[dict], texts: Sequence[str],
                     zones: Sequence[Sequence[float]],
                     ) -> Tuple[List[str], List[Tuple[int, int]]]:
    """逐行把**落进排除带的字符**剔掉 → `(各行保留文本, 流坐标排除区间)`。

    竖排（h≥w，本项目主态）字自上而下排；横排自左而右。第 i 字的中心点
    落入任一排除带 → 剔除。字符级而非行级是必须的：排除带常只覆盖一列的
    **下半截**（跨页延续），整行剔会把本页内容一起丢掉（6期_0002 实测）。
    坐标插值按 `texts`（去空白后的流文本）做，与 `input.text_stream` 同一口径。
    """
    kept_lines: List[str] = []
    excl: List[Tuple[int, int]] = []
    off = 0
    for u, t in zip(units, texts):
        b = u.get("box")
        keep: List[str] = []
        if b and t:
            x0, y0 = float(b[0]), float(b[1])
            x1, y1 = float(b[2]), float(b[3])
            w, h = x1 - x0, y1 - y0
            n = len(t)
            vert = h >= w
            span = max(h, w)
            for i, ch in enumerate(t):
                mid = (i + 0.5) * span / n
                cx = ((x0 + x1) / 2.0) if vert else (x0 + mid)
                cy = (y0 + mid) if vert else ((y0 + y1) / 2.0)
                if any(zx0 <= cx <= zx1 and zy0 <= cy <= zy1
                       for zx0, zy0, zx1, zy1 in zones):
                    excl.append((off + i, off + i + 1))
                else:
                    keep.append(ch)
        kept_lines.append("".join(keep))
        off += len(t)
    return kept_lines, excl


# ============================================================
# 规格外化：**仅凭 plan** 重建 L0 的 spec
# ============================================================
def spec_from_plan(plan: dict) -> dict:
    """**仅凭 plan.json** 重建 L0 的 spec —— 不读 learned、不读档案、不读源码常量。

    这就是「自足」的可执行定义（P-B 的 A2/A3 判据即基于此）。本函数是**唯一实现**：
    探针与验收脚本一律从这里导入，不得各自复制一份。
    """
    ss = plan["split_spec"]
    return {
        "attrs": ss["attrs"],
        "anchor_attr": ss["anchor_attr"],
        "person_attr_set": set(ss["person_attr_set"]),
        "name_max_len": ss["name_max_len"],
        "body_min_len": ss["body_min_len"],
        "name_len": ss["name_len"],
        "page_constants": ss["page_constants"],
        "anchors": {a: [(re.compile(x["re"]), x["mode"]) for x in lst]
                    for a, lst in (ss["anchors"] or {}).items()},
        "person_slots": [tuple(p) for p in (ss.get("person_slots") or [])],
        "titles_re": ss.get("titles_re"),
        "required": ss.get("required"),
        "profile_attrs": ss.get("profile_attrs"),
        "sources": ss.get("sources"),
    }


def contract_facade(plan: dict) -> dict:
    """plan → `preannotate.anchor_page` 需要的最小契约门面。

    只填它真正读的四个位置（S.record_template / G.char_metrics / known_attrs /
    profile_id），其余一律不造——门面越窄，越能暴露"执行器其实还需要别的东西"。
    """
    pm = plan.get("page_model") or {}
    return {
        "profile_id": plan.get("profile_id"),
        "S": {"record_template": (plan.get("record") or {}).get("template") or []},
        "G": {"char_metrics": pm.get("char_metrics") or {}},
        "known_attrs": [a.get("name") for a in (plan.get("attributes") or [])
                        if a.get("name")],
    }


# ============================================================
# 页集合：**跑哪些页** —— 全项目唯一解析（WP-3，2026-09-15）
# ============================================================
def resolve_stems(plan: dict, *, stem: str = "",
                  pages: Optional[Sequence[str]] = None,
                  project: str = "") -> Tuple[List[str], str]:
    """`(页名列表, page_source)` —— **全项目唯一实现**。

    ## 为什么必须有这一处（WP-3）

    此前命令面的页来源只有两个**极值**，中间是空的：

    | 输入 | 实际跑的页 |
    |---|---|
    | `--stem X` | 1 页 |
    | 什么都不给 | `plan.source.pages` = **编译方案时的那几页金标准页** |

    "跑这批新页"**没有参数承载**。而 SKILL.md 第 4 步的原文是「不给 `--stem` 则跑
    全页」—— 在新材料的语境里，"全页"必然被读成"这批页"。后果不是报错，而是
    **rc 0、有产物、校验通过，而对象根本不是新页**（实测：跑出来的就是 4 页
    `0000_官报_1908年4期…` 金标准页）。语义错位比跑不动更危险
    —— 方案 §0.2 纲领第 4 条，优先级高于功能缺失。

    ⇒ 本函数的第一职责不是"能跑"，而是**让"跑的是谁"成为可查证的显式值**：
    返回值里的 `page_source` 同时进产物与人读输出。

    ## 优先级

    `pages` > `project` > `stem` > `plan.source.pages`（越具体越优先）

    `pages` 的元素可以是**页名**，也可以是**目录**（= 跑该目录里的全部页）。
    目录取页复用 `page_triage._stems_from_inbox`、栏目取页复用
    `data_io.get_project_image_stems` —— 与 `triage` **同源**，不另造实现（§60）。

    :returns: `page_source` ∈ {`"explicit"`, `"project:<id>"`, `"plan.source.pages"`}
    """
    if pages:
        out: List[str] = []
        for item in pages:
            p = Path(str(item))
            if p.is_dir():                      # 给目录 = 跑目录里的全部页
                import page_triage as PT
                out += list(PT._stems_from_inbox(p))
            else:
                out.append(p.stem)              # 容忍 "x.png" 与 "x"
        seen: set = set()
        uniq: List[str] = []
        for s in out:
            if s and s not in seen:
                seen.add(s)
                uniq.append(s)
        return uniq, "explicit"
    if project:
        import data_io as DIO
        return ([Path(s).stem for s in DIO.get_project_image_stems(project)],
                f"project:{project}")
    if stem:
        return [str(stem)], "explicit"
    return ([str(s) for s in ((plan.get("source") or {}).get("pages") or [])],
            "plan.source.pages")


# ============================================================
# 单页执行
# ============================================================
def _line_offsets(texts: Sequence[str]) -> List[int]:
    """各行的起始字符偏移（用于把流位置映回行 → 区）。"""
    offs = [0]
    for t in texts:
        offs.append(offs[-1] + len(t))
    return offs


def _region_role_at(y: float, page_h: float, regions: Sequence[dict]) -> str:
    if not page_h or not regions:
        return ""
    r = y / page_h
    for reg in regions:
        lo, hi = (reg.get("lo", (reg.get("rel") or [0, 1])[0]),
                  (reg.get("hi", (reg.get("rel") or [0, 1])[1])))
        if lo <= r < hi:
            return str(reg.get("role") or "")
    return ""


def run_page(plan: dict, stem: str, *,
             structured_dir=None, outbox_dir=None,
             page_source: Optional[str] = None,
             exclusions_path=None) -> Tuple[dict, List[dict]]:
    """plan + 页 → `(annotation_result, 实际使用的行列表)`。

    行列表**一并返回**是刻意的：校验时必须拿"执行器真正消费的那份输入"去核对
    （否则等于让执行器自证）。

    `page_source`（WP-3，2026-09-15）：这一页是**谁指定的** ——
    `"explicit"` / `"project:<id>"` / `"plan.source.pages"`。给了就写进产物，
    于是"跑错对象"从**不可见**变成**可在产物里追**（纲领第 4 条）。
    不给则该字段缺席（老调用方行为逐字不变）。

    `exclusions_path`（P5，2026-09-26）：人工排除带文件路径；默认
    `data/page_exclusions.json`。该页无登记 → 行为与不消费**逐字节一致**。
    """
    import preannotate as PA
    import rule_split as RS

    spec = spec_from_plan(plan)
    facade = contract_facade(plan)

    # ★ 版式覆盖判定（P0-3b，2026-09-13）—— plan 说这页所属的版式族有没有跨页证据。
    #   ⚠ **唯一读口**是 `seam.plan_page_layout`，本模块不得另解析 plan 的 page_model。
    #
    #   为什么 covered=False 就不切：那套骨架（记录起点判据 / 锚词 / 版式带）是从
    #   **例题那 3 页**归纳出来的。把它套到别的题材上，不会"失败退出"，而是**硬切伪命中**
    #   （实测把「译书院」「房租」、人名当成公司名切成记录）。伪命中会倒进待核工作台，
    #   人工复核成本 ≫ 一次 API 调用 —— 故此处**连试都不试**，直接降级为整页一条兜底：
    #   不产伪记录，同时不丢字（守恒判据照常满足）。
    #
    #   `known=False`（老 plan / 外部 plan 没有 page_model）→ 无从判断，按默认路径走，
    #   并在产物里如实写 `known: false`，不假装知道。
    lay = SEAM.plan_page_layout(plan, stem)
    layout_blocked = bool(lay.get("known")) and lay.get("covered") is False
    lay_out = dict(lay)
    lay_out["policy"] = "page_fallback" if layout_blocked else "family_params"

    # 与 anchor_page 内部同参数取行（它自己会再取一次，参数一致则结果一致）
    all_lines = PA.page_lines(stem, structured_dir, outbox_dir)
    units = [u for u in all_lines if (u.get("text") or "").strip()]
    texts = [SEAM.strip_ws(str(u.get("text") or "")) for u in units]
    stream = "".join(texts)

    # ★ 排除带消费（P5）：带内字符**不参与属性分配**——切分/取值只看
    #   `alloc_units`（带内字符已剔）；声明流 `stream` 仍含全部文本，
    #   故校验器的 stream 对账不受影响，带内文本走 `unassigned_text`
    #   以 `exclusion_zone` 声明（正当理由，不算缺口也不算幻觉）。
    #   该页无登记 → `alloc_units` 与 `units` 同一对象，逐字节零改动。
    excl_zones = zones_of(load_page_exclusions(exclusions_path), stem)
    alloc_units, excl_spans = units, []
    if excl_zones:
        kept, excl_spans = _exclusion_split(units, texts, excl_zones)
        alloc_units = [dict(u, text=k) for u, k in zip(units, kept)]
    alloc_texts = texts if not excl_zones else \
        [SEAM.strip_ws(str(u.get("text") or "")) for u in alloc_units]
    alloc_stream = "".join(alloc_texts)

    pid = str(plan.get("profile_id") or "")
    result = SEAM.new_result(
        pid, stem, tier="rule",
        executor={"name": EXECUTOR_NAME, "kind": "reference",
                  "note": "仅实现 rule / single_line（零 token）"},
        plan_ref={"_schema_version": plan.get("_schema_version"),
                  "built_at": plan.get("built_at")},
        layout=lay_out,
    )
    result["input"] = {"text_stream": stream, "n_chars": len(stream),
                       "n_lines": len(units),
                       # 声明消费的是哪条行流 —— 校验器据此**自己去读**同一来源
                       # （不让执行器自证）。参照实现走生产路径 page_lines。
                       "line_source": {"kind": "preannotate.page_lines"}}
    # ★ WP-3：把"这一页是谁指定的"写进产物 —— 产物自此**自己声明**它是什么。
    if page_source:
        result["page_source"] = str(page_source)
    # 页尺寸必须取**原图真实尺寸**，不能从框的极值推：官报的列框只到 y≈1591，
    # 而真实页高 1806 —— 用 max(box) 会把页脚边距整段算进内容区，令
    # `record_in_unannotated` 全域误报（本模块首版就这么错过）。
    #
    # ★ 2026-09-15 WP-2a：找图的目录口径**不再在这里拼** —— 一律走
    #   `config.image_path_of`（全项目唯一解析口，OUTBOX → INBOX 两处查）。
    #   此前这里是 `Path(outbox_dir or DEFAULT_OUTBOX) / f"{stem}.png"`：
    #   图落 `inbox/`（命令面 `import` 的默认落位）时就取不到 ⇒ 静默退回框极值。
    #   实测（控制实验，唯一变量 = 图位置）页宽 953→887（−6.9%）、页高 1787→1643
    #   （−8.1%），而 rc 仍为 0 —— **这段注释警告过的坑，被路径口径重新引入了**。
    import layout_contract as LC
    import config as CONFIG
    img = CONFIG.image_path_of(stem, given=outbox_dir)
    # ⚠ 解析不到时**仍对"原路径"调一次 `LC.page_size`**（同 `preannotate.page_lines`）：
    #   保持"页尺寸 = 对某条路径读文件头"这一形状（测试可 monkeypatch 注入），
    #   且图真不在时结果同为 None（`page_size` 打不开就返回 None），不引入猜测。
    size = LC.page_size(
        img if img is not None else (Path(outbox_dir or PA.DEFAULT_OUTBOX) / f"{stem}.png"))
    page_w = float(size[0]) if size else 0.0
    page_h = float(size[1]) if size else 0.0
    if not size:
        page_h = max((float(u["box"][3]) for u in units if u.get("box")), default=0.0)
        page_w = max((float(u["box"][2]) for u in units if u.get("box")), default=0.0)
    result["page"] = {"w": page_w, "h": page_h, "source": "image" if size else "boxes"}

    if not units:
        result["tier"] = "single_line"
        result["tier_trace"] = [{"tier": "rule", "ok": False, "reason": "no_lines"},
                                {"tier": "single_line", "ok": True,
                                 "reason": "无行可读，空结果（不静默：显式声明）"}]
        result["unassigned_text"] = []
        return result, units

    records, details, segs = [], [], []
    if layout_blocked:
        # ★ 版式未覆盖 → **不跑 L0 骨架切分**（连试都不试：试了也只是把主族骨架硬套）。
        #   记账照写：`rule` 这一级的 ok=False 并给出**为什么没试** —— 与"试了但读不出"
        #   是两件事，不能混成同一个 reason（前者是已知不该用，后者是能力不足）。
        trace = [{"tier": "rule", "ok": False,
                  "reason": "版式未覆盖（所属族只有单页证据）→ 未尝试骨架切分"}]
    else:
        records, details, segs = RS.records_of(alloc_units, spec)
        trace = [{"tier": "rule", "ok": bool(records),
                  "reason": "" if records else "零产出（未找到记录起点行）"}]

    if records:
        entries = (PA.anchor_page(records, stem, facade,
                                  structured_dir=structured_dir,
                                  outbox_dir=outbox_dir) or {}).get("entries") or []
    else:
        # 整页兜底一条（plan 降级链的 single_line 档）——同样只吃剔除后的行
        seg = {"name": "", "name_index": 0, "end_index": len(alloc_units),
               "units": alloc_units, "body_text": "",
               "text": alloc_stream}
        rec, _det = RS.extract_attrs(seg, spec, stream)
        rec = {k: v for k, v in rec.items() if v}
        records, segs = [rec], [seg]
        entries = (PA.anchor_page(records, stem, facade,
                                  structured_dir=structured_dir,
                                  outbox_dir=outbox_dir) or {}).get("entries") or []
        result["tier"] = "single_line"
        trace.append({"tier": "single_line", "ok": True,
                      "reason": ("版式未覆盖 → 整页一条兜底（不产伪命中；产出需人工复核）"
                                 if layout_blocked else
                                 "整页一条保底（plan 允许；低置信占位）")})
    result["tier_trace"] = trace

    # ---- entries → records（按 evidence.record_index 归组）----
    by_rec: Dict[int, List[dict]] = {}
    for e in entries:
        ev = e.get("evidence") or {}
        ri = int(ev.get("record_index") or 0)
        li = ev.get("line_index")
        a = {"attr": str(e.get("attr") or ""), "text": str(e.get("text") or ""),
             "confidence": e.get("confidence")}
        if e.get("box"):
            a["box"] = [float(v) for v in e["box"]]
        if e.get("boxes"):
            a["boxes"] = [[float(v) for v in b] for b in e["boxes"]]
        if li is not None:
            nseg = int(ev.get("n_segments") or 1)
            a["lines"] = list(range(int(li), int(li) + max(1, nseg)))
        by_rec.setdefault(ri, []).append(a)

    for k, seg in enumerate(segs):
        lo, hi = int(seg["name_index"]), int(seg["end_index"])
        boxes: List[List[float]] = []
        for a in by_rec.get(k) or []:
            if a.get("box"):
                boxes.append(a["box"])
        rec_box = None
        if boxes:
            rec_box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes)]
        SEAM.add_record(result, lines=list(range(lo, hi)),
                        attrs=by_rec.get(k) or [], box=rec_box)

    # ---- 未覆盖文本显式声明（**绝不静默丢字**）----
    pieces = [SEAM.norm_locate(a["text"])
              for r in result["records"] for a in r["attrs"] if a.get("text")]
    spans, _bh, _ms = SEAM.find_spans(SEAM.norm_locate(stream), pieces)
    offs = _line_offsets(texts)
    regions = []
    for r in (plan.get("page_model") or {}).get("regions") or []:
        if r.get("stem") == stem:
            regions = r.get("regions") or []
            break
    unassigned: List[dict] = []
    consts = [SEAM.norm_locate(str(c)) for c in (spec.get("page_constants") or []) if c]
    for a, b, txt in SEAM.gaps_of(SEAM.norm_locate(stream), spans):
        # 位置 → 行 → 该行的 y 中心 → 版式区（定声明理由）
        li = _line_at(offs, a, len(texts))
        y = (float(units[li]["box"][1]) + float(units[li]["box"][3])) / 2.0 \
            if li < len(units) and units[li].get("box") else 0.0
        role = _region_role_at(y, page_h, regions)
        # ★ 理由判定顺序（P0-2）：**排除带优先**（P5）——「这段根本不是本页内容」
        #   是最具体的声明；其次**页面常量** —— 它是"这段文本本来就不该是属性值"，
        #   比"落在页眉/页脚区"更具体、更可追（常量来自金标准归纳的 `表头` 标注）。
        #   原先只有 header/footer 两个位置性理由，而实测的版心碎片落在正文带正中
        #   （rel≈0.60），两个位置性理由都套不上 —— 只能含糊成 footer。
        if any(s0 < b and e0 > a for s0, e0 in excl_spans):
            reason = "exclusion_zone"
        elif consts and any(c and c in SEAM.norm_locate(txt) for c in consts):
            reason = "page_constant"
        elif role.startswith("unannotated"):
            reason = "unannotated_region"
        elif li == 0:
            reason = "header"
        elif li >= len(texts) - 1:
            reason = "footer"
        else:
            reason = "no_attribute"
        unassigned.append({"text": txt[:200], "lines": [li], "reason": reason,
                           "n_chars": b - a})
    result["unassigned_text"] = unassigned
    if excl_zones:
        # ★ 执行器做了什么必须留在产物里能被追到（P0-3b 同纪律）：
        #   消费了哪个文件、几条带、剔了多少字 —— 审计与复算的依据。
        result["exclusions"] = {
            "file": (Path(exclusions_path).name if exclusions_path
                     else DEFAULT_EXCLUSIONS_PATH.name),
            "n_zones": len(excl_zones),
            "n_chars": sum(e - s for s, e in excl_spans),
        }
    return result, units


def _line_at(offs: Sequence[int], pos: int, n_lines: int) -> int:
    """offs 为各行起始偏移（长度 n_lines+1，递增）→ 返回 pos 所在行下标。"""
    import bisect
    i = bisect.bisect_right(list(offs), pos) - 1
    return max(0, min(n_lines - 1, i))


# ============================================================
# CLI
# ============================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="层 3 参照执行器（plan → 结果）")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--stem", default="", help="只跑这一页（单页快捷方式）")
    ap.add_argument("--pages", nargs="+", default=None,
                    help="跑哪些页：页名（可多个）或**目录**（跑目录里全部页）"
                         " —— 跑新材料的正确姿势")
    ap.add_argument("--project", default="",
                    help="按栏目取页（项目 id）；与 `chronicles triage --project` 同源")
    ap.add_argument("--plan-dir", default=str(DEFAULT_PLAN_DIR))
    ap.add_argument("--out", default=str(DEFAULT_RESULT_DIR),
                    help="产物**目录**（不是文件名）")
    ap.add_argument("--outbox", default=None,
                    help="页图目录；默认按 OUTBOX→INBOX 两处查（config.image_path_of）")
    ap.add_argument("--structured-dir", default=None, help="默认 data/structured")
    ap.add_argument("--exclusions", default=None,
                    help="人工排除带文件（默认 data/page_exclusions.json；"
                         "供测试隔离可传别的路径）")
    ap.add_argument("--no-verify", action="store_true", help="不跑内建自校验")
    args = ap.parse_args(argv)

    pj = Path(args.plan_dir) / f"plan_{args.profile}.json"
    plan = json.loads(pj.read_text(encoding="utf-8"))
    stems, page_source = resolve_stems(plan, stem=args.stem, pages=args.pages,
                                       project=args.project)
    print(f"[plan_exec] plan={pj.name}  页数={len(stems)}  页来源={page_source}")
    if page_source == "plan.source.pages":
        # ★ WP-3：把最容易被误读的那句说清楚。原文档写的是「不给 --stem 则跑全页」，
        #   而它跑的是**方案编译时的那几页**（金标准页），不是"这批新页"。
        print(f"[plan_exec] ⚠ 未指定页来源 → 跑的是**方案编译时的那 {len(stems)} 页**"
              f"（不是新页）；要跑新页用 `--project <栏目id>` 或 `--pages <目录>`")
    if not stems:
        print("[plan_exec] 页集合为空 —— 缺料（没有可跑的页）")
        return 3
    n_ok = n_bad = 0
    for stem in stems:
        try:
            res, lines = run_page(plan, stem, structured_dir=args.structured_dir,
                                  outbox_dir=args.outbox, page_source=page_source,
                                  exclusions_path=args.exclusions)
        except Exception as e:                                  # pragma: no cover
            print(f"  {stem:<36} 执行失败：{type(e).__name__}: {e}")
            n_bad += 1
            continue
        if not res["records"]:
            print(f"  {stem:<36} 跳过（无行）")
            continue
        p = SEAM.save_result(res, args.out)
        line = (f"  {stem:<36} tier={res['tier']:<11} 记录 {len(res['records']):<3} "
                f"值 {sum(len(r['attrs']) for r in res['records']):<4} "
                f"未声明 {len(res['unassigned_text'])} 段")
        if args.no_verify:
            print(line)
            n_ok += 1
            continue
        rep = SEAM.validate(res, plan=plan,
                            page_size=(res["page"]["w"], res["page"]["h"]))
        print(line + f"  校验 {'✓' if rep['ok'] else '✗'}")
        if not rep["ok"]:
            for e in rep["errors"]:
                print(f"        [错] {e['code']}: {e['msg']}")
            n_bad += 1
        else:
            n_ok += 1
        print(f"        落盘 {p.name}  覆盖度 {rep['metrics'].get('attr_coverage')}")
    print(f"\n[plan_exec] 合规 {n_ok} / 不合规 {n_bad}")
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
