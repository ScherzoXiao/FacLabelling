"""P3（2026-09-10）：预标注生成器（pre-annotation drafts）。

定位（阶段②）：把 VLM 的 `records`
（`{属性名: 值}`，已有 `vlm_reader.read_page(mode="records")`）经 **A 层锚定**
落到几何框上，产出**与人工标注同构**的草稿 jsonl（零迁移，`aggregate()` 可直接消费）。

## 锚定优先级（A 层契约的消费，按可靠性降序）

1. **文本可定位性**：值在页 OCR 行中逐字可定位（繁简折叠后）——整行相等 =
   `exact`（最强）；行内包含 = `contains`；跨行拼接 = `span`（跨列续框）；
2. **记录内顺序**：记录按阅读序排列，值只在**游标之后**的行里找 ⟹ 天然防止
   回头命中上一条记录的同名值（"周廷弼"这类跨记录重复值的关键）；
3. **字长-几何自洽**：值的母行「行高 / 行字数」≈ `cell_h`（偏差 ≤ 30% 记为自洽）
   ——⚠️ 必须用**母行**而非子框，理由见下节；
4. **干净列**：命中的每一行都必须是"列"（`h > w` 且宽 ≤ 契约 `box_w × 1.8`），
   而非残留的横条带——在横条带上做"列内字符偏移"必然错。

## 两条易错口径（伪证据 / 串行的防线）

- **字长自洽不得对子框做**：`_subbox` 的子框高 = `len(val) × cell_h`，是**由
  `cell_h` 正推**的，再拿去比「高 / 字数 ≈ cell_h」**恒等于 0** ——同义反复，
  不是独立证据（红线一：源同则校无效）。故用**行高 vs 行字数**（行高来自像素几何、
  字数来自 OCR 文本，两个独立估计量）作交叉校验（`_parent_fit`）。
- **游标两种语义**：命中「整行即该值」（`exact` 且单段）→ 该行已占满，
  游标**后移一位**，防下一条同名值回头命中同一行；行内 `contains` → 游标留在本行
  （一行可承载多个值，这是存量页的常态）。

## 置信度分层（**优先级，不是正确性保证**）

| 层 | 判据 |
|---|---|
| `high` | 定位 `exact`/`contains` **且** 干净列 **且** 模板命中 **且** 字长自洽 |
| `medium` | 定位 `exact`/`contains`/`span` **且** 干净列 **且** 模板命中 |
| `low` | 其余 |

⚠️ 层级只表达**证据强度**（供人工排优先级），**不等于正确性**：`high` 项的实测
框精度见下表，远未达到可直接采信的程度。

## 实测（2026-09-10，样例页 0001/0002/0003，金标准 69 条可比对条目）

严格口径（每个金标准框都要被某个预测框 IoU≥0.5 命中）：

| 层 | n | IoU≥0.5 | 框中心命中 |
|---|---|---|---|
| `high` | 46 | **0.152** | 0.239 |
| `medium` | 13 | 0.000 | 0.231 |
| `low` | 33 | 0.000 | 0.000 |

（上表为**已含**「母行字长自洽 + 游标排他」两处口径修正后的复测值——两处修正都是
**正确性修正而非提分手段**，对存量 3 页的指标无影响，指标变化见下。）

**远低于方案 §6 的验收线（高置信精确率 ≥ 0.95）。根因是几何而非值**：
把**人工标注值**替换 VLM 值当输入（oracle 上限对照）后指标几乎不变
（IoU≥0.5 0.076 → 0.090）——即瓶颈在"**存量页几何复活**"这一环：

- 存量 `L2_lines` 的框是**均分块高的合成几何**，真实几何只能靠像素重建；
- 列流重建能**准确恢复列**（x 带与人工列差 ≤12px），但**列容量**由列内墨迹高
  推算，受邻列字形渗入、贯通框线、以及"**每条记录从新列顶部开始、末列可只填一半**"
  三重影响，单列误差 ±1–3 字并沿列序累积；
- 结论：**存量页不适合作为 P3 的验收样本**；P3 的验收应在 **P1 之后新入库的页面**
  （`L1_blocks` 由管线写入、几何由 P0a/P0b 在像素定过）上做。

## 红线（方案 §7）

- **红线一：源同则校无效**——置信度**必须含独立于 VLM 的证据**（文本可定位 + 字长-几何
  自洽）。本模块的证据全部来自像素/OCR 文本，**不采信 VLM 的自述**。
- **红线二：确认即固化**——草稿 `source="ai"`；人工确认后才是 `ai_verified`；
  纯人工 `manual`。三者**物理分层**（`data/preannotations/` 只放 ai 层），严禁混淆。

## 页 OCR 行的来源（重要）

- **新页**（P1 之后）：`structured.L1_blocks` 且 `source != "l2_reconstructed"`
  → `rebuild_lines_from_blocks` 离线复算（几何由像素定过，直接可信）。
- **存量页**（P1 之前 52 张 + 回填页）：`L1_blocks.source == "l2_reconstructed"`
  或根本没有该键 → 走 `_column_flow_lines` 的**列流重建**（整页投影切列 +
  **记录锚定**字符流分配）。回填的块层不含新几何，用它反而更差，故显式分流。
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import layout_contract as LC
import data_io

log = logging.getLogger("local_chronicles_ocr")

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()
else:
    _BASE = Path(__file__).parent.resolve()

DEFAULT_STRUCTURED_DIR = _BASE / "data" / "structured"
DEFAULT_OUTBOX = _BASE / "outbox"
DEFAULT_CONTRACTS_DIR = _BASE / "data" / "layout_contracts"
DEFAULT_DRAFTS_DIR = _BASE / "data" / "preannotations"

CHAR_FIT_TOL = 0.30          # 字长-几何自洽容差（相对误差）
MAX_BOX_WIDTH = 400          # 行框宽度上限（滤掉整幅宽的退化行）
MAX_SPAN_LINES = 6           # 跨列续框：值最多跨的行数
PAD_HEAD_RATIO = 0.15        # _subbox 余量门控（P7，2026-09-26 落盘）：slack/h ≥ 此值
                             # 才「贴底」（OCR 文本短于框内墨迹 ⇒ 字符网格从框底往上排），
                             # 否则沿用居中均分。取实证窗口 [0.10, 0.20] 的中间值；
                             # 限度：正证据全部来自拟合页 0001/0002，外样本复验等
                             # sample→人裁产物（→ A2取证报告 §10 / PENDING P7）。

# ---- 像素级页度量：**不在此处实现**，一律引用 layout_contract（P2）同一份实现 ----
# 纪律：同一物理量只有一处实现（P3 列流重建 / P5 漂移检测共用），避免两套口径漂移。
ADV_MIN, ADV_MAX = LC.ADV_MIN, LC.ADV_MAX     # noqa: F811  （保留旧名，向后兼容）
RULE_RATIO, CORE_FRAC = LC.RULE_RATIO, LC.CORE_FRAC
_band_text_y = LC.band_text_y                 # noqa: E305  （旧私有名，测试与内部沿用）
_advance_estimate = LC.advance_estimate


# ============================ 存量页几何复活：列流重建 ============================
#
# 背景（0001/0002/0003 实证）：这类**竖排**页，`L2_lines` 的框全为合成几何，
# 但**文本顺序是真的**（条带序列 = 全页阅读序）。而版面物理模型是：
#
#   1. 文本区 = 若干**全局竖列**（x 带），整页像素投影可稳定得到（0001 得 12 列，
#      x 与人工列一致到 ±12px）；
#   2. 阅读流 = 列内自上而下、列序右→左；
#   3. 列内**字符推进量均一**（实测 36–38px，自相关主峰 36–37），故
#      **列容量 ∝ 列内墨迹高**；
#   4. 旧条带只是"阅读序上的连续文本块"，**边界与列边界无关**
#      → 必须把全页字符流在**字符级**切给各列（比例分配 + 最大余数，总和守恒）。
#
# 残差诚实说明：单列容量误差约 ±1–2 字（源于墨迹边缘与邻列渗入的测量噪声），
# 会沿列序累积。故本路径产出的框是**尽力而为**，不是金标准级几何——
# 这正是 P3 只能给"草稿 + 置信分层"、且高置信线须宁缺毋滥的原因。

def _l2_spans(doc: dict, tol: float = 5.0) -> List[dict]:
    """按 **x 跨度**聚类（容差 `tol`）。

    ⚠️ 不能只按 y 顺序做相邻合并：夹在中间、x 跨度不同的条带（如 0001 的竖排
    标题「公司註冊各案摘要」）会把 y 连续段一刀切断，导致同列群被拆成上下两块，
    投影的 y 区间变窄、列带退化成宽横条（实测过）。
    """
    rows = []
    for l in (doc.get("L2_lines") or []):
        if not l.get("text"):
            continue
        r = LC.rect_of_box(l.get("box") or [])
        if r:
            rows.append((r, str(l["text"])))
    spans: List[dict] = []
    for r, txt in sorted(rows, key=lambda t: (t[0][0], t[0][1])):
        for sp in spans:
            if abs(r[0] - sp["x1"]) <= tol and abs(r[2] - sp["x2"]) <= tol:
                sp["items"].append((r, txt)); break
        else:
            spans.append({"x1": r[0], "x2": r[2], "items": [(r, txt)]})
    return spans


def _l2_groups(doc: dict) -> List[List[Tuple[list, str]]]:
    """→ 块列表；每块 = 同 x 跨度、y 连续的条带序列（阅读序）。"""
    groups = []
    for sp in _l2_spans(doc):
        items = sorted(sp["items"], key=lambda t: t[0][1])
        cur: List[Tuple[list, str]] = []
        for r, txt in items:
            if cur and (r[1] - cur[-1][0][3]) > max(20.0, cur[-1][0][3] - cur[-1][0][1]):
                groups.append(cur); cur = []
            cur.append((r, txt))
        if cur:
            groups.append(cur)
    return groups


def _partition(total: int, weights: List[float]) -> List[int]:
    """按权重把 `total` 个字符分给各列（最大余数法，**总和严格 = total**）。

    **单一实现已收敛**（M5）：算法本体在 `layout_contract.apportion_counts`
    —— 该规则原有本函数与 `adjudicate._split_by_weights` 两份实现，M5 又需要
    第三处消费者（裁决面板分段预览，必须与落库逐字一致），故统一到 LC，此处转发。
    """
    return LC.apportion_counts(total, weights)


def _record_cuts(stream: str, records: List[dict], anchor: str = "公司名") -> List[int]:
    """字符流中每条记录的起点（字符偏移估计）。

    证据按可靠性降序：
    1. **锚属性精确位置**（记录有 `公司名` 时）——最准；
    2. **该记录首个可在流中定位的属性值**——记录起点必然在它之前（且通常只差
       一个锚属性的长度），误差 ~1 个属性 = 半列以内；
    3. **记录文本长度比例**——最弱（VLM 文本长度与 OCR 流差异可达 40 字，
       0001 记录0 VLM 共 113 字而流中实为 156 字）。

    调用方（`_column_flow_lines`）会再用**列容量累计边界**吸附一次：记录必从
    **新一列的顶部**开始，故切点只能落在列边界上。
    """
    recs = [r for r in records if isinstance(r, dict)]
    if len(recs) < 2:
        return []
    lens = [sum(len(LC.normalize_text(v) or "") for k, v in r.items() if k != "跨页")
            for r in recs]
    tot = sum(lens) or 1
    cuts, from_ = [], 0
    for i, r in enumerate(recs):
        est = None
        name = LC.normalize_text(r.get(anchor))
        if name:
            j = stream.find(name, from_)
            if j < 0:
                j = stream.find(name)
            if j >= 0:
                est = j
        if est is None:                              # ② 首个可定位的属性值
            for k, v in r.items():
                if k == "跨页":
                    continue
                nv = LC.normalize_text(v)
                if not nv:
                    continue
                j = stream.find(nv, from_)
                if j < 0:
                    j = stream.find(nv)
                if j >= 0:
                    est = j
                    break
        if est is None:                              # ③ 长度比例
            est = int(round(sum(lens[:i]) / tot * len(stream)))
        cuts.append(max(int(est), from_ if cuts else 0))
        from_ = max(cuts[-1], from_)
    return cuts


def _column_flow_lines(doc: dict, gray, records: Optional[List[dict]] = None,
                       label: str = "") -> List[dict]:
    """存量页 → 列级行框（阅读序）。失败/无图 → `[]`（调用方退回旧行为）。

    `records` 提供时走**记录锚定分配**（推荐）：按锚属性把字符流切成记录区间，
    每条记录**从新列顶部**开始逐列填充，最后一列可只填一半。缺 `records` 时退回
    全流比例分配（无记录信息的通用路径）。
    """
    if gray is None:
        return []
    try:
        import ocr_backend as OB
    except Exception:                                # pragma: no cover
        return []
    out: List[dict] = []
    for grp in _l2_groups(doc):
        rs = [g[0] for g in grp]
        bbox = (min(r[0] for r in rs), min(r[1] for r in rs),
                max(r[2] for r in rs), max(r[3] for r in rs))
        stream = "".join(LC.normalize_text(g[1]) for g in grp)
        if not stream:
            continue
        bands = sorted(OB._project_bands(gray, bbox), key=lambda b: -b[0])
        exts = [_band_text_y(gray, b, bbox) for b in bands] if len(bands) >= 2 else []
        ok = [i for i, e in enumerate(exts) if e]
        if len(ok) < 2:                              # 单列/投影失败 → 原框（可能是真几何）
            out.append({"text": stream, "box": [float(v) for v in bbox]})
            continue

        cols = [{"x": bands[i], "top": float(exts[i][0]),
                 "h": float(exts[i][1] - exts[i][0])} for i in ok]
        adv = len(stream) / max(1.0, sum(c["h"] for c in cols))   # 字符/像素
        # 一致性诊断：高度口径的推进量应与像素自相关主峰同量级（实测 36–38px）。
        # 偏差大 → 说明某列墨迹高被污染（邻列渗入/框线残留），该页几何可信度低。
        _advs = [a for a in (_advance_estimate(gray, b, bbox) for b in bands) if a]
        if _advs and abs(statistics.median(_advs) - 1.0 / adv) > 0.25 / adv:
            log.debug(f"[preannotate] {label} 列推进量口径分歧: "
                      f"高度口径 {1.0/adv:.1f}px vs 自相关 {statistics.median(_advs):.1f}px")
        for c in cols:
            c["cap"] = max(1, int(round(c["h"] * adv)))           # 列容量（字数）

        cuts = _record_cuts(stream, records) if records else []
        if len(cuts) >= 2:                      # 吸附到列容量累计边界（记录必从列顶开始）
            cums, acc = [], 0
            for c in cols:
                acc += c["cap"]; cums.append(acc)
            snapped, prev = [], -1
            for x in cuts:
                cand = min(range(len(cums)), key=lambda i: abs(cums[i] - x)) if x else -1
                v = cums[cand] if cand >= 0 else 0
                if v <= prev:                    # 保严格递增（同列不许两条记录起点）
                    v = next((cums[i] for i in range(len(cums)) if cums[i] > prev), cums[-1])
                snapped.append(v); prev = v
            cuts = snapped
        emitted = 0
        if len(cuts) >= 2:
            bounds = cuts + [len(stream)]
            pos, ci = 0, 0
            for r in range(len(bounds) - 1):
                a, b = max(bounds[r], pos), bounds[r + 1]
                while a < b and ci < len(cols):
                    n = min(cols[ci]["cap"], b - a)
                    _emit_col(out, cols[ci], stream, a, n)
                    a += n; ci += 1; emitted += 1
                if a < b and out:                    # 兜底：余文并进末列（绝不丢字）
                    out[-1]["text"] += stream[a:b]
            pos = ci
        if emitted == 0:                             # 未走记录锚定 → 全流比例分配
            caps = _partition(len(stream), [c["h"] for c in cols])
            pos = 0
            for c, n in zip(cols, caps):
                if n > 0:
                    _emit_col(out, c, stream, pos, n)
                    pos += n
            if pos < len(stream) and out:            # 兜底：绝不丢字
                out[-1]["text"] += stream[pos:]
    return out


def _emit_col(out: List[dict], col: dict, stream: str, start: int, n: int) -> None:
    """把 `stream[start:start+n]` 作为列 `col` 的一行（y 自列顶按固定推进量下推）。

    推进量取 `列墨迹高 / 列容量`（而非 `高/实际字数`）——这样**半填的列**（记录末列）
    其框也只覆盖实际上半部分，与人工框一致。
    """
    if n <= 0:
        return
    step = col["h"] / float(col["cap"])
    a, b = col["x"][0], col["x"][1]
    y1 = col["top"]
    out.append({"text": stream[start:start + n],
                "box": [float(a), y1, float(b), y1 + n * step]})


def page_lines(stem: str,
               structured_dir: Optional[Path] = None,
               outbox_dir: Optional[Path] = None,
               records: Optional[List[dict]] = None,
               return_index: bool = False):
    """页 OCR 行（经几何复活）→ `[{text, box:[x1,y1,x2,y2]}]`（阅读序）。

    `records` 仅用于**存量页**的列流重建（记录锚定分配）；无 `L1_blocks` 且无
    `records` 时退化为全流比例分配。

    **M1（方案 §10.1）**：出口处按**页面像素尺寸**把每个框夹回图内（`LC.sanitize_bbox`）。
    越界框不报错、只会让下游「列内字符偏移」静默错位，故在摄取期截断。
    取不到图尺寸（图缺失/损坏）→ **不裁剪**（无参照系时不做猜测性修改，向后兼容）。

    **M2**：`return_index=True` → 返回 `(行列表, 索引映射)`，`索引映射[k]` = 阅读序第 k
    位在原数组中的下标（排序不就地；对照 `X-AnyLabeling` 的 `sorted_boxes`）。
    """
    structured_dir = Path(structured_dir or DEFAULT_STRUCTURED_DIR)
    # ★ 2026-09-15 WP-2a：`outbox_dir` 的语义收窄为「调用方**显式指定**的图目录」。
    #   原先 `outbox_dir or DEFAULT_OUTBOX` 会把「没给」悄悄变成「给了 outbox/」，
    #   于是本函数**永远只搜 outbox/** —— 而命令面 `import` 的落位是 `inbox/`
    #   （用户 2026-09-15 裁定保持）。取不到图的后果是**两处静默**：
    #     ① 下行 `size` 为 None ⇒ 出口不裁剪越界框（列内字符偏移静默错位）；
    #     ② 列流重建拿不到灰度图 ⇒ 退回全流比例分配。
    #   现在：没给 ⇒ 交给 `config.image_path_of` 按全项目惯例两处查（OUTBOX → INBOX）。
    given_out = Path(outbox_dir) if outbox_dir else None
    p = structured_dir / f"{stem}.json"
    if not p.exists():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[preannotate] structured 读取失败 {stem}: {e}")
        return []

    raw: List[dict] = []
    l1 = doc.get("L1_blocks") or {}
    # `source="l2_reconstructed"`（存量回填）**不含任何新几何**——它只是把 L2_lines
    # 逐行包成块，复算结果 = 原合成框。用它会让列流重建失效、P3 精度反而下降，
    # 故仅当块层来自 OCR 管线（几何由像素定过）时才走复算路径。
    # （= 方案 §10.1 M6「已有几何 → 跳过检测只补值」的快路径；无块层的存量页才走重建。）
    import config as CONFIG
    # ★ WP-2a：找图**只此一处口径**（`config.image_path_of`）。显式给了目录就只搜它，
    #   没给则 OUTBOX → INBOX 两处查（全项目惯例）。返回 None ⇒ 真没有图 ——
    #   此时 size/gray 均为空，两个降级都是**既有设计**（无参照系不做猜测性修改），
    #   但从此不再是"图明明在 inbox 却被判成没有"。
    # ⚠ 取不到解析结果时**仍对"原路径"调一次 `LC.page_size`**（而不是直接跳过）：
    #   ① 保持"页尺寸 = 对某条路径读文件头"这一形状 ⇒ 测试可
    #      `monkeypatch(PA.LC, "page_size", ...)` 注入尺寸（改动本行时这个形状别丢）；
    #   ② 图真的不在时结果同为 `None`（`page_size` 打不开就返回 None），不引入猜测。
    img = CONFIG.image_path_of(stem, given=given_out)
    size = LC.page_size(
        img if img is not None else (Path(given_out or DEFAULT_OUTBOX) / f"{stem}.png"))
    if l1.get("blocks") and l1.get("source") != "l2_reconstructed":
        import structured_writer
        rep = structured_writer.rebuild_lines_from_blocks(l1) or {}
        raw = rep.get("lines") or []
    if not raw:                                      # 存量页（或复算空）→ 列流重建
        gray = None
        if img is not None:
            try:
                import ocr_backend
                gray = ocr_backend._load_gray(str(img))
            except Exception as e:                   # pragma: no cover
                log.warning(f"[preannotate] 灰度载入失败 {stem}: {e}")
        raw = _column_flow_lines(doc, gray, records, label=stem)
    if not raw:                                      # 最后兜底：L2 原框
        raw = [{"text": l.get("text"), "box": l.get("box")}
               for l in (doc.get("L2_lines") or []) if l.get("text")]

    lines: List[dict] = []
    out_of_page = 0
    for l in raw:
        r = LC.rect_of_box(l.get("box") or [])
        if not r:
            continue
        if size:
            s = LC.sanitize_bbox([r[0], r[1], r[2], r[3]], size[0], size[1])
            if s is None:                            # 整框在图外 → 丢弃（不猜几何）
                out_of_page += 1
                continue
            if (abs(s[0] - r[0]) > 0.5 or abs(s[1] - r[1]) > 0.5
                    or abs(s[2] - r[2]) > 0.5 or abs(s[3] - r[3]) > 0.5):
                out_of_page += 1                     # 越界被截断 → 计入（可见不静默）
            r = (s[0], s[1], s[2], s[3])
        lines.append({"text": str(l.get("text") or ""),
                      "box": [r[0], r[1], r[2], r[3]]})
    if out_of_page:
        log.warning(f"[preannotate] {stem} 有 {out_of_page} 个行框越出页面，已夹回/丢弃")
    order = _reading_order(lines)                    # M2：返回**索引映射**（不就地排序）
    ordered = [lines[i] for i in order]
    if return_index:
        return ordered, order
    return ordered


# ---- 阅读序（M2，方案 §10.1）：**单一实现下沉 layout_contract** ----
# 场景前提（用户 2026-09-11 明确）：本系统绝大比例用于批量处理**古籍**，故
# **从右到左的竖版排版占绝对主导**——列是阅读序第一主轴，列内自上而下。
# 无量纲列聚类（重叠比/宽度比/跨度比）替代旧 `x // 50` 硬编码分箱；此处只做转发，
# 判据与阈值一律以 `layout_contract` 为唯一来源（P2/P3/P5 同源纪律）。
COL_OVERLAP_RATIO = LC.COL_OVERLAP_RATIO
COL_WIDTH_RATIO = LC.COL_WIDTH_RATIO
COL_SPAN_MAX = LC.COL_SPAN_MAX
LEGACY_BIN_PX = 50.0       # 旧口径（硬编码 //50）——仅作**回退**保留


def _column_clusters(lines: List[dict]) -> List[List[int]]:
    """行 → 列簇（转发 `LC.column_clusters`，判据单一来源）。"""
    return LC.column_clusters([l.get("box") for l in lines])


def _reading_order(lines: List[dict], mode: str = "auto") -> List[int]:
    """阅读序 → **原索引序列**（M2：排序不就地，返回索引映射）。

    `mode="column"`（默认行为）走 **RTL 原生列聚类**：列按 x 中心**降序**
    （右→左），簇内按 y 升序（上→下）。`mode="bin"` 走旧 `//50` 分箱（回退用）。
    `mode="auto"`：先试列聚类，**异常时回退旧分箱**（向后兼容，失败不改行为）。
    """
    n = len(lines)
    if n <= 1:
        return list(range(n))
    if mode == "bin":
        return sorted(range(n), key=lambda i: (
            -int(((lines[i]["box"][0] + lines[i]["box"][2]) / 2.0) // LEGACY_BIN_PX),
            lines[i]["box"][1]))
    try:
        return LC.order_rtl([l.get("box") for l in lines])
    except Exception as e:                                    # pragma: no cover
        log.warning(f"[preannotate] 列聚类失败，回退旧分箱: {e}")
        return sorted(range(n), key=lambda i: (
            -int(((lines[i]["box"][0] + lines[i]["box"][2]) / 2.0) // LEGACY_BIN_PX),
            lines[i]["box"][1]))


# ============================ 锚定 ============================

def _locate(value_norm: str, lines_norm: List[str], start: int = 0
            ) -> Tuple[Optional[int], str]:
    """在 `lines_norm[start:]` 中定位值 → (行号, "exact"|"contains"|"none")。"""
    if not value_norm:
        return None, "none"
    for i in range(start, len(lines_norm)):
        if lines_norm[i] == value_norm:
            return i, "exact"
    for i in range(start, len(lines_norm)):
        if value_norm in lines_norm[i]:
            return i, "contains"
    return None, "none"


def _locate_segments(value_norm: str, lines_norm: List[str], start: int = 0
                     ) -> Tuple[str, List[Tuple[int, str]]]:
    """定位值 → (kind, [(行号, 该行承载的值片段)])，支持**跨列续框**。

    人工标注实测 12–20% 的条目是跨列续框（值的尾部落在前一列底、头部在下一列顶，
    如 0001「總號在」+「北京琉璃廠」）。单行定位对这类必然失败 → 必须支持
    **连续若干行拼接**后再按字符贡献切回各行。

    kind ∈ `exact`（整行即该值）/ `contains`（行内包含）/ `span`（跨行拼接）/
    `none`。
    """
    if not value_norm:
        return "none", []
    idx, kind = _locate(value_norm, lines_norm, start)
    if idx is not None:
        return kind, [(idx, value_norm)]
    # 跨行拼接（游标之后优先，找不到再全页）
    for lo in (start, 0):
        for i in range(lo, len(lines_norm)):
            acc, spans = "", []
            for j in range(i, min(i + MAX_SPAN_LINES, len(lines_norm))):
                acc += lines_norm[j]
                spans.append((j, lines_norm[j]))
                p = acc.find(value_norm)
                if p < 0:
                    continue
                segs, pos, consumed = [], 0, 0
                for jj, ln in spans:
                    a = max(0, p - pos)
                    b = min(len(ln), p + len(value_norm) - pos)
                    if b > a:
                        segs.append((jj, value_norm[consumed:consumed + (b - a)]))
                        consumed += b - a
                    pos += len(ln)
                    if consumed >= len(value_norm):
                        break
                if segs:
                    return "span", segs
    return "none", []


def _char_fit_ok(box, value_len: int, cell_h: Optional[float]) -> Optional[float]:
    """竖排字长-几何自洽 → 相对误差；横排/无基准 → None。"""
    if not cell_h or not box or value_len <= 0:
        return None
    w, h = box[2] - box[0], box[3] - box[1]
    if h <= w:                                        # 横排不适用
        return None
    expect = cell_h * value_len
    if expect <= 0:
        return None
    return abs(h - expect) / expect


def _parent_fit(segs: List[Tuple[int, str]], lines: List[dict],
                lines_norm: List[str], cell_h: Optional[float]) -> Optional[float]:
    """字长-几何自洽的**独立**口径：值所在**行**的「行高 vs 行字数」。

    ⚠️ **不得对子框做自洽检查**：`_subbox` 产出的子框高 = `len(val) × cell_h`
    （**由 `cell_h` 正推**），再拿它比「高 / 字数 ≈ cell_h」必然**恒等于 0**
    ——同义反复，不构成独立证据（红线一：源同则校无效）。行高与行字数则是两个
    **相互独立**的估计量（行高来自像素几何，字数来自 OCR 文本）的交叉校验。

    跨行（span）取首段所在行——校验的是该行几何是否与自身字数自洽，与值无关。
    """
    if not segs:
        return None
    j = segs[0][0]
    if not (0 <= j < len(lines)) or j >= len(lines_norm):
        return None
    return _char_fit_ok(lines[j]["box"], len(lines_norm[j]), cell_h)


def _subbox(line: dict, line_norm: str, val_norm: str,
            cell_h: Optional[float] = None, box_w: Optional[float] = None) -> Optional[list]:
    """**列内字符偏移 → 子框**（方案 §3 阶段② 第 3 步）。"""
    if not line_norm or not val_norm:
        return None
    pos = line_norm.find(val_norm)
    if pos < 0:
        return None
    b = line["box"]
    n = len(line_norm)
    if n <= 0:
        return None
    w, h = b[2] - b[0], b[3] - b[1]
    if h >= w:                                        # 竖排：沿 y 切
        cell = float(cell_h) if cell_h else h / n
        # `cell_h` 是**全页**字格估计（金标准实测中位），对"粗框"（整列一个框）
        # 是唯一可用的尺度；但当它在**这一行**放不下（`n × cell_h > h`，即行框偏紧）
        # 时，位置会整段落到父行之外——此时退回该行自身的平均字高。
        # （P-C 校验器实测：夹取前 y2 溢出到 1717/父行只到 1591；只看夹取又会
        #   退化成零高框 → 根因是尺度选错，不是没夹。）
        if n * cell > h:
            cell = h / n
        # 余量策略（P7 门控，2026-09-26 落盘）：粗框（slack/h ≥ PAD_HEAD_RATIO）贴底，
        # 紧框沿用居中均分。实证：端到端B 0.1500→0.3000、变差==0（i82/i83/i88）；
        # 既有断言不受影响（TestSubbox 两组 slack/h = 0 / 0.0625 均在门外）。
        slack = max(0.0, h - n * cell)
        pad = slack if (h > 0 and slack / h >= PAD_HEAD_RATIO) else slack / 2.0
        y1 = b[1] + pad + pos * cell
        y2 = y1 + len(val_norm) * cell
        # 兜底夹取：只**收窄**，不动文本、不动记录结构
        y1 = max(b[1], min(y1, b[3]))
        y2 = max(y1, min(y2, b[3]))
        cx = (b[0] + b[2]) / 2.0
        bw = float(box_w) if (box_w and box_w <= w) else w
        return [cx - bw / 2.0, y1, cx + bw / 2.0, y2]
    cell = w / n                                      # 横排：沿 x 切
    x1 = b[0] + pos * cell
    x2 = x1 + len(val_norm) * cell
    x1 = max(b[0], min(x1, b[2]))
    x2 = max(x1, min(x2, b[2]))
    return [x1, b[1], x2, b[3]]


def _union(boxes: List[list]) -> Optional[list]:
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def anchor_page(records: List[Dict[str, str]], stem: str, contract: dict,
                structured_dir=None, outbox_dir=None,
                image_name: Optional[str] = None) -> dict:
    """VLM records + 契约 → 草稿条目（与人工标注同构）。

    返回 `{"entries": [...], "stats": {...}}`；entries 含
    `attr / text / box / boxes / confidence / evidence`（外加与人工 jsonl 同名的
    `image_name / profile / source="ai" / ts`）。

    **多框**：值跨列续接时（人工标注实测占 12–20%）`boxes` 为多段、`box` 为其并集
    ——与 `annotation_groups.aggregate()` 的产物同构，下游零迁移。

    **M5 分段视图**：多段条目额外带 `segments`（逐段 `box / text / row_uid / ocr_line`），
    供裁决面板「折叠 + 展开可见各段」。逐段文本的切分**与 `adjudicate.build_rows`
    落库共用 `LC.split_by_boxes`**，故"面板看到的"恒等于"采纳后写入的"。
    单框条目不落该键（草稿字节与改动前一致）。
    """
    sem = contract.get("S") or {}
    geo = contract.get("G") or {}
    tmpl = sem.get("record_template") or []
    known = contract.get("known_attrs") or []
    pid = contract.get("profile_id") or ""
    cell_h = ((geo.get("char_metrics") or {}).get("cell_h"))
    box_w = ((geo.get("char_metrics") or {}).get("box_w"))
    lines = page_lines(stem, structured_dir, outbox_dir, records)
    lines_norm = [LC.normalize_text(l["text"]) for l in lines]
    # M3（方案 §10.3）：行**稳定标识**——几何指纹优先（跨重跑/跨分箱稳定），
    # `unique_uid` 消解重名（同坐标重复行）→ 裁决记录不会因列分箱变化而错挂。
    _seen_uid: set = set()
    line_uids = [LC.unique_uid(LC.row_uid(l, j), _seen_uid) for j, l in enumerate(lines)]
    ts = datetime.now().isoformat(timespec="seconds")
    if image_name is None:
        image_name = f"{stem}.png"

    entries: List[dict] = []
    cursor = 0
    stats = {"records": len(records), "values": 0, "anchored": 0,
             "exact": 0, "contains": 0, "span": 0, "none": 0,
             "high": 0, "medium": 0, "low": 0}

    for ridx, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        # 属性遍历顺序：先按模板序，再补 records 里模板外的键
        keys = [a for a in tmpl if a in rec] + [k for k in rec
                                                if k not in tmpl and k not in ("跨页",)]
        for attr in keys:
            raw = rec.get(attr)
            val = LC.normalize_text(raw)
            if not val:
                continue
            stats["values"] += 1
            kind, segs = _locate_segments(val, lines_norm, cursor)
            if kind == "none":                         # 游标之后找不到 → 全页回退一次
                kind, segs = _locate_segments(val, lines_norm, 0)

            boxes: List[list] = []
            if segs:
                whole_line = (kind == "exact" and len(segs) == 1)
                for j, part in segs:
                    if whole_line:                     # 整行即该值 → 直接用行框
                        boxes.append([float(v) for v in lines[j]["box"]])
                    else:                              # 行内包含/跨行 → 按字符偏移切子框
                        sb = _subbox(lines[j], lines_norm[j], part, cell_h, box_w)
                        boxes.append(sb if sb else [float(v) for v in lines[j]["box"]])
                cursor = segs[-1][0] + (1 if whole_line else 0)
                # 整行即该值 → 该行已被占满，后续值不可能再落在同一行 → 游标后移一位；
                # 否则保持**包含**语义（同一行可承载多个值），这是跨记录防重复的边界。
                stats[kind] += 1
                stats["anchored"] += 1
            else:
                stats["none"] += 1

            box = _union(boxes)
            fit = _parent_fit(segs, lines, lines_norm, cell_h)
            fit_ok = (fit is not None and fit <= CHAR_FIT_TOL)
            tmpl_hit = attr in tmpl
            # **干净列**判据：命中的每一行都必须是"列"而非残留横条带。
            # 存量 L2 的 box 常是 P0a 之前的**横向转置**产物（宽 ≈ 整幅），
            # 复算后仍可能有横条带残留；在横条带上做"列内字符偏移"必然错。
            def _is_col(j: int) -> bool:
                b = lines[j]["box"]
                w, h = b[2] - b[0], b[3] - b[1]
                return bool(h > w and (not box_w or w <= box_w * 1.8))

            clean = bool(segs) and all(_is_col(j) for j, _p in segs)
            line_w = (lines[segs[0][0]]["box"][2] - lines[segs[0][0]]["box"][0]) if segs else 0.0
            # 红线二（三视图）：裁决界面必须能**并排看到 OCR 原文**，禁止只显示草稿值
            # 让人点"确认"（那是盲签）。此处把命中行的**原始 OCR 文本**原样带出
            # （不做 normalize，保留可选字形差异供人工比对），零额外计算。
            ocr_text = "".join(lines[j]["text"] for j, _p in segs) if segs else ""
            if kind in ("exact", "contains") and clean and tmpl_hit and fit_ok:
                conf = "high"                          # 三源一致：定位/干净列/字长自洽
            elif kind in ("exact", "contains", "span") and clean and tmpl_hit:
                conf = "medium"
            else:
                conf = "low"
            stats[conf] += 1

            # M5（方案 §10.4）：**分段视图**（"面板折叠 + 展开可见各段"的数据面）。
            # 只对**多段条目**落这个键 —— 单框条目（绝大多数）的草稿字节不变，
            # 老读者零感知；`adjudicate.page_state` 会给旧草稿即时派生，界面拿到
            # 的统一契约恒非空。逐段文本用 `LC.split_by_boxes` 切分，**与
            # `adjudicate.build_rows` 落库走同一实现** → 面板预览必然等于实际写入。
            seg_meta = None
            if len(boxes) >= 2:
                seg_texts = LC.split_by_boxes(str(raw).strip(), boxes)
                if len(seg_texts) == len(boxes) and segs:
                    seg_meta = [
                        {
                            "seg": k,
                            "row_uid": line_uids[j],
                            "box": [float(v) for v in boxes[k]],
                            "text": seg_texts[k],
                            "n_chars": len(seg_texts[k]),
                            # 该段锚定到的**原始 OCR 行文本**（不 normalize）——
                            # 展开时人工可直接核对"这一段切得对不对"（防盲签）。
                            "ocr_line": lines[j]["text"],
                        }
                        for k, (j, _part) in enumerate(segs) if k < len(boxes)
                    ]
                    if len(seg_meta) != len(boxes):     # 段与框不 1:1 → 不落，交给派生
                        seg_meta = None

            entry = {
                "attr": attr,
                "text": str(raw),
                "box": box,
                "boxes": boxes,
                # M3：段稳定标识 = **首行**的几何指纹（段由首行锚定）；跨列续框时
                # 完整命中行标识见 evidence.row_uids。
                "row_uid": line_uids[segs[0][0]] if segs else None,
                "image_name": image_name,
                "profile": pid,
                "source": "ai",
                "ts": ts,
                "confidence": conf,
                "evidence": {
                    "locate": kind,
                    "line_index": segs[0][0] if segs else None,
                    "row_uids": [line_uids[j] for j, _p in segs] if segs else [],
                    "n_segments": len(segs),
                    "is_column": clean,
                    "line_width": round(line_w, 1),
                    "char_fit_rel_err": round(fit, 4) if fit is not None else None,
                    "template_hit": tmpl_hit,
                    "record_index": ridx,
                    "known_attr": attr in known,
                    "ocr_text": ocr_text,
                },
            }
            if seg_meta:
                entry["segments"] = seg_meta
            entries.append(entry)
    stats["lines"] = len(lines)
    return {"entries": entries, "stats": stats}


# ============================ 便捷入口 / 落盘 ============================

def load_contract(profile_id: str, contracts_dir=None) -> Optional[dict]:
    return LC.load_contract(profile_id, contracts_dir or DEFAULT_CONTRACTS_DIR)


def preannotate_stem(stem: str, records: List[Dict[str, str]], profile_id: str,
                     contracts_dir=None, **kw) -> dict:
    """按 profile_id 取契约 → 锚定 → 结果（契约缺失 → `error`）。"""
    c = load_contract(profile_id, contracts_dir)
    if c is None:
        return {"entries": [], "stats": {}, "error": f"契约不存在: {profile_id}"}
    return anchor_page(records, stem, c, **kw)


def save_drafts(entries: List[dict], stem: str,
                drafts_dir=None) -> Optional[Path]:
    """草稿落 `data/preannotations/<stem>.ai.jsonl`（**只放 ai 层**，红线二）。

    **M7（方案 §10.1）**：原子写 + `fsync` +「全部成功才替换」。此前是裸
    `open("w")` 覆盖写——崩溃会留半截草稿，重跑会**不可逆地**毁掉上一版
    （`review_store` / `adjudicate` / `drift` 都做了原子写，唯独此处漏了）。
    现在任一行渲染失败 → 目标文件保持旧内容不变。
    """
    d = Path(drafts_dir or DEFAULT_DRAFTS_DIR)
    p = d / f"{data_io.safe_name(stem)}.ai.jsonl"
    data_io.atomic_write_jsonl(p, entries)
    log.info(f"[preannotate] 草稿已保存: {p.name}（{len(entries)} 条）")
    return p


def load_run_records(run_dir: Path, stem: str) -> Optional[List[dict]]:
    """从 divergence run 产物读已落盘的 VLM records（离线复验用，零 API 调用）。"""
    p = Path(run_dir) / f"{stem}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    recs = (d.get("read") or {}).get("records")
    return recs if isinstance(recs, list) else None
