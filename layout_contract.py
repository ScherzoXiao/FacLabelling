"""P2（2026-09-10）：批级版面契约归纳器（Batch Layout Contract）。

定位见《半自动标注通用方案_20260910.md》§2/§3 阶段①：**人工 O(1) 建契约**——
从 1–3 页已人工标注页**自动归纳**三层契约参数，人工只做确认与微调；
产物落 `data/layout_contracts/layout_<profile_id>.json`，供 P3 预标注消费。

三层（每层有独立的**可观测校验信号**，这是"半自动"能客观门控的前提）：

- **G 层 · 几何**：阅读序 / 列参数 / 字高基准 / 切行规则 / 区域
- **S 层 · 语义**：记录模板（属性出现序列）/ 基数 / 必现选填 / 页级属性
- **A 层 · 锚定**：定位率 / 属性→相对横向位置先验 / 字长-几何自洽

**归纳算法本身格式无关**（投影切列、成对定序、字长自洽），只有**参数**是批特定的
——这正是"格式相近"从限制变成资产的地方（方案 §4）。

## 关键算法：S 层记录切分（本模块的核心）

金标准是**扁平的 (attr, text, box) 列表**，没有记录边界。切分难点：
属性序未知 ⟹ 无法用"序回退"切记录；而序又只能用记录内统计归纳 ⟹ 循环依赖。

解法 = **锚属性切分**（打破循环）：

1. `_valid_rows`：剔除 `xp`（跨页续接）标记行——它们是上/下页记录的残段，
   不属于本页任何**新**记录；
2. `_pick_anchor`：锚 = 记录起始属性。版面证据：记录从列的**顶部**起写
   ⟹ 锚是所有候选属性中**平均框顶 y 最小**者（官报实测恒为「公司名」）；
3. `_split_by_anchor`：遇锚属性即开新记录，锚之前的行归 `head`（前导残段）；
4. `_topo_order`：**同一记录内**统计属性对"谁在谁前"的胜场，多数决建有向图，
   Kahn 拓扑定序。只看同记录内的对 ⟹ 天然免疫**跨记录边界**噪声与
   **属性缺失**（某记录缺某属性不会把它的位置提前）；
5. `support` → 主体属性（≥ `min_rec_sup`）与页级/罕见属性（低于阈值，如页眉"表头"）。

**为什么不用 Borda 分数排序**：分数是全局补偿量，局部偏好可能成环而分数看不出来；
拓扑排序直接暴露环（环内属性按首现序兜底并记入 `cyclic` 诊断）。
官报实测：留一法序单调率 Borda 0.86/0.82/0.81 → 拓扑 **1.00/0.96/0.89**。

## 纪律

- 消费属性标注一律走 `annotation_groups.aggregate()`（P1d 唯一入口），不另造分组逻辑；
- 本模块**只读**既有数据，只写 `data/layout_contracts/`（新增落盘产物）；
- 数据路径走 `_BASE = frozen ? sys.executable.parent : __file__.parent`；
- 归纳失败/样本不足 → 返回 `confidence=low` 并给 `diagnostics`，**不抛异常静默降级**。
"""
from __future__ import annotations

import heapq
import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("local_chronicles_ocr")

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录
else:
    _BASE = Path(__file__).parent.resolve()

DEFAULT_ANN_DIR = _BASE / "manual_annotations"
DEFAULT_STRUCTURED_DIR = _BASE / "data" / "structured"
DEFAULT_CONTRACTS_DIR = _BASE / "data" / "layout_contracts"
DEFAULT_OUTBOX = _BASE / "outbox"

SCHEMA_VERSION = "1.1.0"          # 1.1.0：G 层新增 page_stats（像素级页度量，供 P5 漂移检测）

# ---- 阈值（全部可在契约里覆写，见 §4「批内一次校准的变量」）----
MIN_RECORD_SUPPORT = 0.5   # 记录模板主体属性的最低出现率
MIN_ANCHOR_COUNT = 2       # 锚属性最少出现次数
COL_PROFILE_RATIO = 0.25   # 整页投影切列的阈值（沿用 ocr_backend P0a）
CHAR_MIN, CHAR_MAX = 12.0, 220.0   # 字高物理上下限（px）
INK_LEVEL = 128            # 二值化阈值（墨迹 < INK_LEVEL）
ADV_MIN, ADV_MAX = 18, 80  # 字符推进量合理区间（像素）
RULE_RATIO = 0.90          # 贯通横线判据：行墨迹占满列带的比例
CORE_FRAC = 0.25           # 列带中央取样比例（排除邻列字形渗入边缘）

try:                                            # 繁简折叠（定位用）；缺失则退化为原样比较
    from zhconv import convert as _zh_convert   # type: ignore
except Exception:                               # pragma: no cover
    def _zh_convert(s: str, _t: str = "zh-cn") -> str:
        return s


# ============================ 基础工具 ============================

def normalize_text(s: str) -> str:
    """定位用归一：繁简折叠 + 去空白（逐字可定位的判据口径）。"""
    if not s:
        return ""
    return "".join(ch for ch in _zh_convert(str(s), "zh-cn") if not ch.isspace())


def rect_of_box(box) -> Optional[Tuple[float, float, float, float]]:
    """box → (x1, y1, x2, y2)。兼容两种形式：

    - 扁平 `[x1, y1, x2, y2]`（`annotation_groups.aggregate` 的标注框）
    - 四边形 `[[x, y], ...]`（structured L2_lines 的 OCR 框）

    **非法/空输入 → None**（不抛异常）：本函数被 P3 直接用于消费 OCR 行框，
    单条脏框不得让整页崩掉。调用方**必须**容错。
    """
    try:
        if isinstance(box, (list, tuple)) and len(box) == 4 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in box):
            return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        pts = list(box)
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
    except (TypeError, IndexError, ValueError, KeyError):
        return None
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def first_rect(entry: dict) -> Optional[Tuple[float, float, float, float]]:
    """条目的首个矩形：优先 `boxes[0]`（`aggregate()` 产物），
    兼容仅含扁平 `box` 的行（旧标注/手写行）——向后兼容口径。"""
    boxes = entry.get("boxes") or []
    if boxes:
        r = rect_of_box(boxes[0])
        if r:
            return r
    return rect_of_box(entry.get("box"))


def stem_of(image_name: str) -> str:
    """标注 image_name（含 .png）→ structured 用的 stem（去扩展名）。"""
    return image_name[:-4] if image_name.lower().endswith(".png") else image_name


def sanitize_bbox(box, w: float, h: float) -> Optional[list]:
    """M1（方案 §10.1）：把框**夹回图内**并纠正 x/y 顺序 —— 返回 `[x1,y1,x2,y2]`。

    - 坐标裁到 `[0, w] × [0, h]`；若 x1>x2（或 y1>y2）先交换；
    - 夹取后宽或高 ≤ 0（整框在图外）→ **None**（调用方应丢弃该框）；
    - `w`/`h` 非正或框非法 → None（不做无参照系的猜测性裁剪）。

    为什么必须做：上游坐标可能来自**另一分辨率**的参照系（例如按原图尺寸切分后
    在缩略页上复用），越界框不会报错，只会让下游「列内字符偏移」静默错位——
    这正是 §10.1 标注的**静默错位源**。裁剪是最后一道防线，不是缩放替代。
    """
    r = rect_of_box(box)
    if not r:
        return None
    try:
        W, H = float(w), float(h)
    except (TypeError, ValueError):
        return None
    if not (W > 0 and H > 0):
        return None
    x1, x2 = sorted((r[0], r[2]))
    y1, y2 = sorted((r[1], r[3]))
    x1, y1 = max(0.0, x1), max(0.0, y1)
    x2, y2 = min(W, x2), min(H, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def page_size(image_path) -> Optional[Tuple[int, int]]:
    """读图像尺寸 `(w, h)`（**只读文件头**，不解码像素）；失败 → None。

    供 `sanitize_bbox` 取参照系；独立于 `page_stats`（后者需要整幅灰度）。
    """
    try:
        from PIL import Image
        with Image.open(str(image_path)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


def _named_of(rec: List[dict], skip=frozenset()) -> List[str]:
    """记录 → 具名属性序列（去重保序，剔除 skip）。"""
    out: List[str] = []
    for e in rec:
        a = e.get("attr")
        if a and a not in skip and a not in out:
            out.append(a)
    return out


def _mode_binned(values: List[float], bin_w: float = 2.0) -> Optional[float]:
    """分箱众数（抗长尾，比中位数更稳）——字高基准用。"""
    if not values:
        return None
    bins = Counter(int(v // bin_w) for v in values)
    k = bins.most_common(1)[0][0]
    inbin = [v for v in values if int(v // bin_w) == k]
    return statistics.mean(inbin)


def _valid_rows(entries: List[dict]) -> List[dict]:
    """剔除跨页续接残段（xp 标记行）——它们不属于本页新记录。"""
    return [e for e in entries if e.get("xp") not in ("prev", "next")]


# ============================ 阅读序基元（RTL 竖版优先，M2） ============================
# 场景前提（2026-09-11 用户明确）：本系统绝大比例用于批量处理**古籍**，故
# **从右到左的竖版排版占绝对主导** —— 列是阅读序第一主轴（右→左），列内自上而下。
#
# 为什么不用固定像素分箱：旧口径 `x_center // 50` 在官报上恰好可用，但列距随文献而异
# （官报实测 `col_gap_px + col_width_px ≈ 87px`，而窄版古籍可低至 20–30px）。
# 任何固定分箱都会在某一档失效（列太密 → 两列同箱、按 y 交错；列太疏 → 一列跨箱、
# 退化为箱序 = 错误阅读序）。故改用**无量纲**判据（重叠比 / 宽度比 / 跨度比）。
COL_OVERLAP_RATIO = 0.50   # 同列判据：x 区间重叠 ≥ 较窄者宽度的该比例
COL_WIDTH_RATIO = 0.50     # 同列守卫：两者宽度差异不得大于（防整幅横条带吞并列）
COL_SPAN_MAX = 1.60        # 同列守卫：簇的 x 跨度 ≤ 种子宽的该倍数（防逐列吞并）


def same_column(a, b) -> bool:
    """两个框是否**同列**（无量纲三守卫；见模块内说明）。非法框 → False。"""
    ra, rb = rect_of_box(a), rect_of_box(b)
    if not ra or not rb:
        return False
    wa, wb = max(1e-6, ra[2] - ra[0]), max(1e-6, rb[2] - rb[0])
    if min(wa, wb) / max(wa, wb) < COL_WIDTH_RATIO:                  # 守卫 2
        return False
    ow = min(ra[2], rb[2]) - max(ra[0], rb[0])
    if ow <= 0 or ow / min(wa, wb) < COL_OVERLAP_RATIO:               # 守卫 1
        return False
    return (max(ra[2], rb[2]) - min(ra[0], rb[0])) <= COL_SPAN_MAX * min(wa, wb)  # 守卫 3


def column_clusters(boxes: List) -> List[List[int]]:
    """把框按 x 区间重叠聚成**列**，返回簇（每簇 = 索引列表，簇内保持入参顺序）。

    遍历顺序按 x 中心升序（左→右）建簇；**不在此处决定阅读序**（由 `order_rtl` 定）。
    宽度/跨度守卫均以**种子框**为基准（簇一旦建立，种子不变）——
    防"逐列吞并"把整页并成一簇（那会退化成纯 y 排序 = 完全错误的阅读序）。
    """
    order = sorted(range(len(boxes)),
                   key=lambda i: ((rect_of_box(boxes[i]) or (0, 0, 0, 0))[0]
                                  + (rect_of_box(boxes[i]) or (0, 0, 0, 0))[2],
                                  (rect_of_box(boxes[i]) or (0, 0, 0, 0))[1]))
    clusters: List[dict] = []
    for i in order:
        r = rect_of_box(boxes[i])
        if not r:
            clusters.append({"seed": None, "idx": [i]})       # 非法框独占一簇（不被吞）
            continue
        placed = False
        for c in clusters:
            if c["seed"] is None:
                continue
            seed = c["seed"]
            if min(r[2] - r[0], seed[2] - seed[0]) / max(
                    1e-6, max(r[2] - r[0], seed[2] - seed[0])) < COL_WIDTH_RATIO:
                continue
            if (max(r[2], seed[2]) - min(r[0], seed[0])) > COL_SPAN_MAX * (seed[2] - seed[0]):
                continue
            ux1, ux2 = c["ux1"], c["ux2"]
            ow = min(r[2], ux2) - max(r[0], ux1)
            if ow <= 0 or ow / max(1e-6, min(r[2] - r[0], ux2 - ux1)) < COL_OVERLAP_RATIO:
                continue
            c["ux1"], c["ux2"] = min(ux1, r[0]), max(ux2, r[2])
            c["idx"].append(i)
            placed = True
            break
        if not placed:
            clusters.append({"seed": r, "ux1": r[0], "ux2": r[2], "idx": [i]})
    return [c["idx"] for c in clusters]


def order_rtl(boxes: List) -> List[int]:
    """RTL 竖版阅读序 → **索引序列**（列右→左，列内上→下）。

    与 `sorted()` 的区别：**不就地排序**，返回索引映射（M2）——
    调用方据此把结果映回原数组（对照 `X-AnyLabeling` 的 `sorted_boxes` 返回
    `(boxes, sort_indices)`）。非法框（无 rect）排在最后，保持相对顺序。
    """
    n = len(boxes)
    if n <= 1:
        return list(range(n))
    rects = [rect_of_box(b) for b in boxes]
    valid = [i for i in range(n) if rects[i]]
    invalid = [i for i in range(n) if not rects[i]]
    try:
        clusters = [c for c in column_clusters(boxes) if c]
        covered = sum(len(c) for c in clusters)
        if covered != n:
            raise ValueError("簇未覆盖全部框")
        clusters.sort(key=lambda c: -max(
            ((rects[i][0] + rects[i][2]) / 2.0) if rects[i] else -1e18 for i in c))
        out: List[int] = []
        for c in clusters:
            out.extend(sorted(c, key=lambda i: (rects[i][1] if rects[i] else 0.0)))
        return out
    except Exception:                       # 回退：按 x 中心降序 + y 升序（仍是 RTL）
        return sorted(valid, key=lambda i: (-(rects[i][0] + rects[i][2]) / 2.0, rects[i][1])) + invalid


def row_uid(line: dict, index: int) -> str:
    """行/段**稳定标识**（M3，方案 §10.3）。

    几何存在 → **几何指纹**（`box_x1_y1_x2_y2`）：与阅读序、列分箱、重跑次数无关，
    同一条记录重跑后仍是同一 id。无几何 → 位置索引兜底 `idx_{n}`。

    为什么需要：现用 `(stem, idx)` 引用，而 idx 是**排序后的位置**——列分箱一变，
    同一条记录就换 id，人工裁决记录会**挂到别人身上**（静默错挂）。
    """
    b = rect_of_box((line or {}).get("box"))
    if b:
        return "box_%d_%d_%d_%d" % (int(round(b[0])), int(round(b[1])),
                                    int(round(b[2])), int(round(b[3])))
    return "idx_%d" % (int(index) + 1)


def unique_uid(uid: str, seen) -> str:
    """碰撞消解：重名 → `<uid>__dup2`、`__dup3`…（并登记进 `seen`，**原地生效**）。

    对照 `X-AnyLabeling` 的 `build_unique_block_key(..., seen_keys)`。
    """
    if uid not in seen:
        seen.add(uid)
        return uid
    n = 2
    while f"{uid}__dup{n}" in seen:
        n += 1
    out = f"{uid}__dup{n}"
    seen.add(out)
    return out


# ==================== 文本分段基元（M5：多段续接的单一切分实现） ====================
#
# 「一个逻辑值由多段续接承载」是本语料的**常态**（人工标注实测 12–20%，RTL 竖版
# 尤甚：一列写到底就接下一列）。切分规则 = **最大余数法**按各段权重分配字符数，
# 且要求**总和严格守恒**（拼回 == 原值，绝不丢字）。
#
# 该规则此前有**两份实现**：`preannotate._partition`（返回各段字数，给列流分字用）
# 与 `adjudicate._split_by_weights`（返回切片，给裁决落库用）。口径相同但各自演化，
# 一处改了另一处不会跟着改。M5 需要第三处消费者（裁决面板的**分段预览**，
# 即"展开可见各段"必须与落库结果逐字一致）—— 三处消费者是收敛的红线，故统一到此处，
# 上两处改为**转发**，名字保留以免触碰既有调用点（同 M9 的 `safe_name` 手法）。

def apportion_counts(total: int, weights: List[float]) -> List[int]:
    """最大余数法：把 `total` 按 `weights` 分给各段，**总和严格 = total**。

    - `weights` 为空 / 权重和 ≤ 0 / `total ≤ 0` → 全 0（调用方据此跳过）；
    - 余数并列 → 按下标升序优先（`(-rem, i)` 显式定序，不依赖排序稳定性）。

    这是本仓**唯一**的字符分配实现；`preannotate._partition` 转发到它。
    """
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    s = float(sum(weights))
    if s <= 0:
        return [0] * n
    raw = [total * float(w) / s for w in weights]
    base = [int(x) if x > 0 else 0 for x in raw]     # 负数权重 → 下限保护（不为负）
    rem = int(total) - sum(base)
    if rem > 0:
        frac = [raw[i] - base[i] for i in range(n)]
        order = sorted(range(n), key=lambda i: (-frac[i], i))
        for k in range(rem):
            base[order[k % len(order)]] += 1
    return base


def split_text(text: str, weights: List[float]) -> List[str]:
    """按权重切分文本 → 各段切片（拼接恒等于 `text`，**绝不丢字**）。

    段数 ≤ 1 → `[text]`（含 `text` 为空）：与旧 `_split_by_weights` 的退化口径一致。
    权重全为 0 / 非法（`apportion_counts` 退化为全 0）时，**兜底把全部文本给末段**
    ——宁可"分段不匀"也不能静默丢字；此路径在正常口径下不可达
    （权重恒为 `max(1.0, 框高)` > 0，故真实数据不受影响）。
    """
    if len(weights) <= 1:
        return [text]
    counts = apportion_counts(len(text), weights)
    if sum(counts) != len(text):
        counts = [0] * len(weights)
        counts[-1] = len(text)
    out, pos = [], 0
    for c in counts:
        out.append(text[pos:pos + c])
        pos += c
    return out


def segment_weights(boxes) -> Tuple[List[float], List[int]]:
    """段框 → `(权重, 有效下标)`；权重 = 段框高 `max(1.0, y2-y1)`。

    **权重口径的唯一来源**：与 `adjudicate.build_rows` 落库时的取值逐字一致
    （那里写的是 `max(1.0, b[3] - b[1])`）。非法框（`rect_of_box` → None）被剔除，
    并返回其**原始下标**，让调用方能把切片映射回原 `boxes` 数组。
    """
    ws: List[float] = []
    keep: List[int] = []
    for i, b in enumerate(boxes or []):
        r = rect_of_box(b)
        if r is None:
            continue
        ws.append(max(1.0, r[3] - r[1]))
        keep.append(i)
    return ws, keep


def split_by_boxes(text: str, boxes) -> List[str]:
    """按**段框高**比例切分文本 → 切片列表，与 `segment_weights` 的有效下标对齐。

    单一实现；`adjudicate` 落库与 `preannotate` 草稿预览、`adjudicate.page_state`
    的旧草稿派生都走这里 —— 三处结果必然逐字相同（M5 的机械判据）。
    """
    ws, _keep = segment_weights(boxes)
    return split_text(text, ws)


# ============================ 采集已标注页 ============================

def collect_pages(profile_id: str,
                  ann_dir: Optional[Path] = None,
                  outbox_dir: Optional[Path] = None) -> List[dict]:
    """按档案 id 采集已标注页 → [{"stem", "image_name", "entries", "img_w", "img_h"}]。

    `img_w/img_h` 取自 outbox 原图（读不到则 None）——`select_source_pages` 用它
    识别**裁切页**。只读尺寸，不解码像素。
    """
    from annotation_groups import aggregate

    ann_dir = Path(ann_dir or DEFAULT_ANN_DIR)
    outbox = Path(outbox_dir or DEFAULT_OUTBOX)
    if not ann_dir.exists():
        return []
    out: List[dict] = []
    for p in sorted(ann_dir.glob("*.jsonl")):
        try:
            raw = [json.loads(line) for line in
                   p.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[layout_contract] 标注读取失败 {p.name}: {e}")
            continue
        rows = [r for r in raw if isinstance(r, dict) and r.get("profile") == profile_id]
        if not rows:
            continue
        image_name = p.name[:-len(".jsonl")]
        stem = stem_of(image_name)
        w = h = None
        try:
            from PIL import Image
            ip = outbox / f"{stem}.png"
            if ip.exists():
                w, h = Image.open(ip).size
        except Exception:                             # pragma: no cover
            pass
        out.append({"stem": stem, "image_name": image_name,
                    "entries": aggregate(rows), "img_w": w, "img_h": h})
    return out


# ---- 契约源页资格（红线三：漂移可见，不得静默混入异质页） ----
CROP_RATIO = 0.60   # 页宽（或页高）低于同批中位数的该比例 → 判为裁切/截取页


def select_source_pages(pages: List[dict],
                        min_ratio: float = CROP_RATIO) -> Tuple[List[dict], List[dict]]:
    """从同档案的已标注页中挑出**整页**作契约源 → (入选, 被剔)。

    实测教训（2026-09-11）：官报批中混入一页 956×1781 的**单列裁切页**（宽为中位
    1895 的 50%），其记录被裁切边缘截断 → 支持度/记录数统计被带偏，`S.confidence`
    由 `high` 直落 `low`。裁切页的信息是**局部的**，不能作记录级统计的样本。
    被剔页连同理由一起写进契约 `source.excluded`（可见、可回溯，不静默丢弃）。
    """
    ok = [p for p in pages if p.get("img_w") and p.get("img_h")]
    if len(ok) < 3:                                   # 样本太少不做裁切判定
        return pages, []
    mw = statistics.median(p["img_w"] for p in ok)
    mh = statistics.median(p["img_h"] for p in ok)
    kept, dropped = [], []
    for p in pages:
        w, h = p.get("img_w"), p.get("img_h")
        if not w or not h:
            kept.append(p)                            # 无尺寸信息 → 不据此剔除（存疑即留）
            continue
        if w < min_ratio * mw or h < min_ratio * mh:
            dropped.append({"stem": p["stem"], "img_w": w, "img_h": h,
                            "reason": "crop",
                            "detail": f"页尺寸 {w}x{h} 低于同批中位数 "
                                      f"{mw:.0f}x{mh:.0f} 的 {min_ratio:.0%} → 判为裁切/截取页"})
        else:
            kept.append(p)
    return kept, dropped


# ============================ S 层 · 语义契约 ============================

def _pick_anchor(valid: List[dict]) -> Optional[str]:
    """锚 = 记录起始属性：平均框顶 y 最小者（记录从列顶起写），出现 ≥ MIN_ANCHOR_COUNT。

    无几何的行不参与定锚（`矩形缺失 → 跳过`）：锚的判据是**位置**，无位置即无证据。
    """
    top_y, cnt = defaultdict(list), Counter()
    for e in valid:
        a = e.get("attr")
        r = first_rect(e) if a else None
        if not a or r is None:
            continue
        cnt[a] += 1
        top_y[a].append(r[1])
    cands = [a for a in cnt if cnt[a] >= MIN_ANCHOR_COUNT]
    if not cands:
        return None
    return min(cands, key=lambda a: (statistics.mean(top_y[a]), -cnt[a]))


def _split_by_anchor(valid: List[dict], anchor: str) -> Tuple[List[List[dict]], List[dict]]:
    """遇锚即开新记录；锚之前的行归 head（前导残段）。"""
    recs: List[List[dict]] = []
    cur: List[dict] = []
    head: List[dict] = []
    for e in valid:
        if e.get("attr") == anchor:
            if cur:
                recs.append(cur)
            cur = [e]
        elif cur:
            cur.append(e)
        else:
            head.append(e)
    if cur:
        recs.append(cur)
    return recs, head


def _topo_order(recs: List[List[dict]], skip, seed: Dict[str, int]) -> Tuple[List[str], List]:
    """同记录内成对胜场 → 有向图 → Kahn 拓扑定序（tie 用首现序）。

    返回 (order, cyclic)：cyclic 为拓扑后仍余下的成环属性（按首现序追加）。
    """
    win, attrs = Counter(), set()
    for r in recs:
        named = _named_of(r, skip)
        attrs.update(named)
        for i, a in enumerate(named):
            for b in named[i + 1:]:
                win[(a, b)] += 1
    adj: Dict[str, set] = defaultdict(set)
    indeg = Counter({a: 0 for a in attrs})
    for a in attrs:
        for b in attrs:
            if a != b and win[(a, b)] > win[(b, a)]:
                adj[a].add(b)
    for a in adj:
        for b in adj[a]:
            indeg[b] += 1
    heap = [(seed.get(a, 999), a) for a in attrs if indeg[a] == 0]
    heapq.heapify(heap)
    order: List[str] = []
    while heap:
        _, a = heapq.heappop(heap)
        order.append(a)
        for b in sorted(adj[a], key=lambda x: seed.get(x, 999)):
            indeg[b] -= 1
            if indeg[b] == 0:
                heapq.heappush(heap, (seed.get(b, 999), b))
    rest = sorted((a for a in attrs if a not in order), key=lambda x: seed.get(x, 999))
    return order + rest, rest


def induce_semantics(pages: List[dict],
                     min_rec_sup: float = MIN_RECORD_SUPPORT) -> dict:
    """S 层：跨页归纳记录模板 / 基数 / 必现选填 / 页级属性。"""
    all_recs: List[List[dict]] = []
    per_page: List[dict] = []
    anchors: List[str] = []
    seed: Dict[str, int] = {}
    cyclic: List[str] = []

    for pg in pages:
        valid = _valid_rows(pg["entries"])
        for e in valid:                                  # 首现序做 tie-break 种子
            a = e.get("attr")
            if a and a not in seed:
                seed[a] = len(seed)
        anchor = _pick_anchor(valid)
        if anchor is None:
            per_page.append({"stem": pg["stem"], "anchor": None,
                             "n_records": 0, "n_head": 0})
            continue
        recs, head = _split_by_anchor(valid, anchor)
        recs = [r for r in recs if _named_of(r)]        # 剔空记录（页脚等）
        anchors.append(anchor)
        all_recs.extend(recs)
        per_page.append({"stem": pg["stem"], "anchor": anchor,
                         "n_records": len(recs), "n_head": len(head),
                         "record_sizes": [len(_named_of(r)) for r in recs]})

    if not all_recs:
        return {"record_template": [], "support": {}, "required": [], "optional": [],
                "rare": [], "cardinality": {}, "n_records": 0, "anchor": None,
                "page_level": [], "per_page": per_page, "cyclic": [],
                "min_record_support": min_rec_sup, "confidence": "none"}

    n = len(all_recs)
    cnt = Counter()
    for r in all_recs:
        for a in _named_of(r):
            cnt[a] += 1
    page_level = sorted(a for a, c in cnt.items() if c / n < min_rec_sup)
    order, cyclic = _topo_order(all_recs, frozenset(page_level), seed)
    support = {a: round(cnt[a] / n, 3) for a in order}
    sizes = [len(_named_of(r, frozenset(page_level))) for r in all_recs]
    sizes_sorted = sorted(sizes)
    ns = len(sizes_sorted)
    anchor = Counter(anchors).most_common(1)[0][0] if anchors else None

    conf = "high"
    if anchor is None or len(set(anchors)) > 1 or n < 3:
        conf = "low"
    elif cyclic:
        conf = "medium"
    return {
        "record_template": order,
        "support": support,
        "required": [a for a in order if cnt[a] / n >= 0.8],
        "optional": [a for a in order if 0.5 <= cnt[a] / n < 0.8],
        "rare": [a for a in order if cnt[a] / n < 0.5],
        "cardinality": {"mean": round(statistics.mean(sizes), 2),
                        "min": min(sizes), "max": max(sizes),
                        "p10": sizes_sorted[max(0, int(ns * 0.10) - 1)],
                        "p90": sizes_sorted[min(ns - 1, int(ns * 0.90))]},
        "n_records": n,
        "anchor": anchor,
        "page_level": page_level,
        "per_page": per_page,
        "cyclic": cyclic,
        "min_record_support": min_rec_sup,
        "confidence": conf,
    }


# ============================ G 层 · 几何契约 ============================

def _page_gray(image_path: Path):
    """图像 → 灰度 ndarray（失败返回 None，调用方需容错）。"""
    try:
        from ocr_backend import _load_gray
        return _load_gray(str(image_path))
    except Exception as e:                            # pragma: no cover
        log.warning(f"[layout_contract] 图像读取失败 {image_path.name}: {e}")
        return None


def _page_column_bands(gray, ylo: float, yhi: float, ratio: float = COL_PROFILE_RATIO):
    """**限定在标注纵向区间内**的整页投影切列（复用 ocr_backend P0a 实现）→ [(x1, x2)]。

    限定 y 区间是为了把页眉/页脚/页面边框排除在列网格之外——整页投影会把它们
    算成额外"列"（实测 0001 得到 26 列，而真实约 12 列）。
    """
    try:
        from ocr_backend import _project_bands
        bands = _project_bands(gray, (0.0, float(ylo), float(gray.shape[1]), float(yhi)))
        out = [(float(b[0]), float(b[1])) for b in bands] if bands else []
        return out if 2 <= len(out) <= 40 else []      # 合理性门（否则视为失败）
    except Exception as e:                            # pragma: no cover
        log.warning(f"[layout_contract] 投影切列失败: {e}")
        return []


# ============ 像素级页度量基元（P2 G 层 / P3 列流重建 / P5 漂移检测 共用） ============
# 纪律：**同一物理量只有一处实现**。P3 的列流重建与 P5 的漂移打分都必须调用
# 这里的函数，不得各写一份「自己那版」的像素测量（曾因两套口径产生过偏差）。

def ink_ratio(gray, ylo: float, yhi: float) -> float:
    """限定 y 区间内的墨迹占比（`< INK_LEVEL` 的像素比例）。"""
    import numpy as np
    y1 = max(0, int(ylo)); y2 = min(gray.shape[0], int(yhi))
    if y2 <= y1:
        return 0.0
    sub = np.asarray(gray[y1:y2])
    return float((sub < INK_LEVEL).mean()) if sub.size else 0.0


def band_text_y(gray, band, bbox, min_ink: float = 0.30,
                min_run: int = 8) -> Optional[Tuple[int, int]]:
    """列带内**排除贯通横线**后的文字 y 范围 → (top, bot)。

    - **横线判据**必须用**整带**：贯通横线占满带宽（比值 ~1.0），而竖排单字行
      只占 ~0.55。⚠️ 不能用窄带判——窄带里文字行本身就占满带宽。
    - **文字行判据**用**中央区**（去掉两侧 `CORE_FRAC`）：相邻列字形会渗入本带
      边缘（0001「北京工藝商局」就渗进右邻列顶边），只看中央区可排除。
    - 首/尾必须落在**连续墨迹段**（≤2px 缝隙算同段、长度 ≥ `min_run`）上，
      否则是横线底边抗锯齿的单行残影。
    """
    import numpy as np
    bx1 = max(0, int(band[0])); bx2 = min(gray.shape[1], int(band[1]))
    by1 = max(0, int(bbox[1])); by2 = min(gray.shape[0], int(bbox[3]))
    if bx2 <= bx1 or by2 <= by1:
        return None
    bw = bx2 - bx1
    pad = int(bw * CORE_FRAC)
    cx1, cx2 = bx1 + pad, bx2 - pad
    if cx2 - cx1 < 4:
        cx1, cx2 = bx1, bx2
    wide = gray[by1:by2, bx1:bx2] < INK_LEVEL
    core_m = gray[by1:by2, cx1:cx2] < INK_LEVEL
    if not core_m.any():
        return None
    rule = (wide.sum(axis=1) / float(bw)) >= RULE_RATIO
    ink = (core_m.sum(axis=1) / float(cx2 - cx1)) >= min_ink
    keep = np.where(ink & ~rule)[0]
    if len(keep) == 0:
        return None
    runs, s, p = [], int(keep[0]), int(keep[0])
    for v in keep[1:]:
        v = int(v)
        if v - p <= 2:
            p = v
        else:
            runs.append((s, p)); s = p = v
    runs.append((s, p))
    runs = [r for r in runs if r[1] - r[0] + 1 >= min_run]
    if not runs:
        return None
    return (by1 + runs[0][0], by1 + runs[-1][1] + 1)


def advance_estimate(gray, band, bbox) -> Optional[float]:
    """列内字符推进量（像素）——行墨迹剖面的**自相关主峰**。

    竖排文字在 y 方向近似周期信号，周期 = 字符推进量。实测 0001 三列分别给出
    37 / 36 / 36px（峰值 0.24–0.51，显著），与契约 `cell_h≈36.6` 一致。
    ⚠️ 不能再用「列宽 × 0.92」估计：官报列宽 63–76px 而真实推进量 36–38px，
    该口径偏大 ~1.7 倍，会让每列只装得下一半的字（`_rebuild_block_entries`
    的历史口径即此，是存量页几何退化的直接原因之一）。
    ⚠️ **倍频歧义**：主峰可能是真周期的 2 倍或 1/2 倍（官报同批内实测同时出现
    36 与 18），比对时容差必须覆盖该歧义（见 `drift.SD_FLOOR_REL`）。
    """
    import numpy as np
    bx1 = max(0, int(band[0])); bx2 = min(gray.shape[1], int(band[1]))
    by1 = max(0, int(bbox[1])); by2 = min(gray.shape[0], int(bbox[3]))
    if bx2 - bx1 < 8 or by2 - by1 < 4 * ADV_MIN:
        return None
    prof = (gray[by1:by2, bx1:bx2] < INK_LEVEL).sum(axis=1).astype(float)
    prof -= prof.mean()
    if not prof.any():
        return None
    ac = np.correlate(prof, prof, "full")[len(prof) - 1:]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    seg = ac[ADV_MIN:ADV_MAX]
    if not len(seg):
        return None
    k = int(seg.argmax()) + ADV_MIN
    return float(k) if seg.max() >= 0.15 else None


def page_stats(gray, ylo: float, yhi: float) -> dict:
    """一页的**像素级**版式度量（无需人工标注、无需 OCR 文本，摄取即可算）。

    字段：长宽比 / 墨迹比 / 列数 / 列间距 / 列宽 / 字符推进量。
    P5 漂移检测把逐页结果与契约 `G.page_stats.features` 的分布比对。
    """
    import numpy as np
    h, w = int(gray.shape[0]), int(gray.shape[1])
    y1 = max(0, min(int(ylo), h - 1)); y2 = max(y1 + 1, min(int(yhi), h))
    out: Dict[str, Any] = {"w": w, "h": h, "aspect": round(w / h, 4) if h else None,
                           "ink_ratio": round(ink_ratio(gray, y1, y2), 4),
                           "band_y": [y1, y2]}
    bands = _page_column_bands(gray, y1, y2)
    out["n_columns"] = len(bands) if bands else None
    if bands:
        cs = [(b[0] + b[1]) / 2 for b in bands]
        gaps = [c2 - c1 for c1, c2 in zip(cs, cs[1:]) if c2 - c1 > 0]
        ws = [b[1] - b[0] for b in bands]
        out["col_gap_px"] = round(float(statistics.median(gaps)), 1) if gaps else None
        out["col_width_px"] = round(float(statistics.median(ws)), 1) if ws else None
    else:
        out["col_gap_px"] = None
        out["col_width_px"] = None
    adv = advance_estimate(gray, (0, w), (0, y1, w, y2))
    out["adv_px"] = adv
    if adv:
        prof = (gray[y1:y2] < INK_LEVEL).sum(axis=1).astype(float)
        prof -= prof.mean()
        if prof.any():
            ac = np.correlate(prof, prof, "full")[len(prof) - 1:]
            if ac[0] > 0:
                ac = ac / ac[0]
                lo = int(max(ADV_MIN, adv - 1)); hi = int(min(ADV_MAX, adv + 2))
                if hi > lo:
                    out["adv_peak"] = round(float(ac[lo:hi].max()), 3)
    return out


# ---- 供 P5 比对的特征清单（顺序 = 报告顺序；'aspect' 等为无量纲量）----
DRIFT_FEATURES = ("aspect", "ink_ratio", "n_columns", "col_gap_px",
                  "col_width_px", "adv_px")

SD_FLOOR_MULT = 1.5        # 逐特征 sd 下限 = max(MULT × 同发布物实测 rel_sd, MIN)
SD_FLOOR_MIN = 0.01        # 下限的绝对地板（1%）
SD_FLOOR_SCAN_MAX = 60     # 同发布物扫描页数上限（控契约构建耗时）


def _feature_summary(values: List[float]) -> Optional[dict]:
    """一组取值的分布摘要（n<2 时 sd 无意义 → 记 None，由 P5 用下限兜底）。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return {"mean": round(statistics.mean(vals), 4),
            "sd": round(statistics.pstdev(vals), 4) if len(vals) >= 2 else None,
            "min": round(min(vals), 4), "max": round(max(vals), 4),
            "n": len(vals)}


def _publication_prefix(stem: str) -> str:
    """页 stem → 发布物前缀（去掉尾部 `_NNNN` 页序）。"""
    parts = str(stem).rsplit("_", 1)
    return parts[0] if len(parts) == 2 and parts[1].isdigit() else str(stem)


def _publication_sd_floors(pages: List[dict], outbox_dir: Path,
                           band: Optional[Tuple[float, float]]) -> dict:
    """在同发布物**全部页**上实测逐特征相对离散度 → 每特征 sd 下限。

    为什么必须逐特征：不同特征的**测量稳定性差两个数量级**——官报同期内
    `aspect` 的相对离散度仅 0.0004（同一台扫描仪、同一开本），而 `col_gap_px`
    达 0.23（首页带通栏标题会把列距拉开）。用统一下限必然两头不讨好：太小则
    跨页误报，太大则把"整页翻转/大幅拉伸"这类结构性变化也放过。
    """
    prefixes = {_publication_prefix(p["stem"]) for p in pages}
    if not prefixes or band is None:
        return {}
    acc: Dict[str, List[float]] = {}
    n = 0
    for p in sorted(outbox_dir.glob("*.png")):
        if n >= SD_FLOOR_SCAN_MAX:
            break
        if _publication_prefix(p.stem) not in prefixes:
            continue
        gray = _page_gray(p)
        if gray is None:
            continue
        try:
            st = page_stats(gray, band[0], band[1])
        except Exception:                             # pragma: no cover
            continue
        n += 1
        for k in DRIFT_FEATURES:
            if st.get(k) is not None:
                acc.setdefault(k, []).append(st[k])
    out = {}
    for k, vals in acc.items():
        if len(vals) < 2:
            continue
        mu = statistics.mean(vals)
        rel = (statistics.pstdev(vals) / abs(mu)) if mu else 0.0
        out[k] = {"sd_floor": round(max(SD_FLOOR_MULT * rel, SD_FLOOR_MIN), 4),
                  "rel_sd_measured": round(rel, 4), "n_scanned": len(vals)}
    out["_n_pages_scanned"] = n
    return out


def induce_geometry(pages: List[dict],
                    outbox_dir: Optional[Path] = None,
                    structured_dir: Optional[Path] = None) -> dict:
    """G 层：阅读序 / 列参数 / 字高基准 / 切行规则 / 区域。"""
    outbox_dir = Path(outbox_dir or DEFAULT_OUTBOX)
    structured_dir = Path(structured_dir or DEFAULT_STRUCTURED_DIR)

    dx_neg = dx_pos = vert = 0
    char_hs: List[float] = []
    box_ws: List[float] = []
    col_ann: List[int] = []
    col_gaps: List[float] = []
    col_widths: List[float] = []
    page_ys: List[Tuple[float, float]] = []
    title_ys: List[Tuple[float, float]] = []
    split_rules: Counter = Counter()
    ps_acc: Dict[str, List[float]] = {}      # 像素级页度量（按特征累积）
    ps_samples: List[dict] = []              # 逐页原始值（诊断用）
    ps_band: Optional[List[float]] = None

    for pg in pages:
        entries = _valid_rows(pg["entries"])
        pg_ys: List[Tuple[float, float]] = []
        # --- 阅读序：**锚属性在连续记录间的 x 位移**（记录级推进方向，最可靠） ---
        anchor = _pick_anchor(entries)
        recs, _ = _split_by_anchor(entries, anchor) if anchor else ([], [])
        anchor_x: List[float] = []
        for e in entries:
            r = first_rect(e) if anchor and e.get("attr") == anchor else None
            if r:
                anchor_x.append((r[0] + r[2]) / 2)
        for x1, x2 in zip(anchor_x, anchor_x[1:]):
            if x2 < x1 - 1:
                dx_neg += 1
            elif x2 > x1 + 1:
                dx_pos += 1
        # 列内推进方向（同一属性连续两段 = 续框，应自上而下）
        for attr_seen in {e.get("attr") for e in entries if e.get("attr")}:
            ys = [r[1] for r in (first_rect(e) for e in entries
                                 if e.get("attr") == attr_seen) if r]
            for y1, y2 in zip(ys, ys[1:]):
                if y2 > y1:
                    vert += 1
        # --- 字高 / 框宽（**按段配对**：gid 组的 text 是拼接全文，必须用 segments[i] 配 boxes[i]） ---
        for e in entries:
            boxes = e.get("boxes") or ([e["box"]] if e.get("box") else [])
            r0 = rect_of_box(boxes[0]) if boxes else None
            if not r0:
                continue
            x1, y1, x2, y2 = r0
            if e.get("attr") == "表头":
                title_ys.append((y1, y2))
            page_ys.append((y1, y2))
            pg_ys.append((y1, y2))
            segs = e.get("segments") or []
            for i, bx in enumerate(boxes):
                r4 = rect_of_box(bx)
                if not r4:
                    continue
                w, h = r4[2] - r4[0], r4[3] - r4[1]
                box_ws.append(w)
                seg = segs[i] if i < len(segs) else ""
                if h > w and len(seg) >= 2:
                    cell = h / len(seg)
                    if CHAR_MIN <= cell <= CHAR_MAX:
                        char_hs.append(cell)
        # --- 列参数：**限定标注纵向区间**的整页投影（需图像） ---
        img = Path(outbox_dir) / f"{pg['stem']}.png"
        if img.exists() and pg_ys:
            ylo = min(y for y, _ in pg_ys) - 10
            yhi = max(y for _, y in pg_ys) + 10
            gray = _page_gray(img)
            if gray is not None:
                bands = _page_column_bands(gray, ylo, yhi)
                if len(bands) >= 2:
                    col_ann.append(len(bands))
                    cs = [(b[0] + b[1]) / 2 for b in bands]
                    col_widths.extend(b[1] - b[0] for b in bands)
                    col_gaps.extend(abs(c2 - c1) for c1, c2 in zip(cs, cs[1:]))
                # --- 像素级页度量（P5 漂移检测的比对基准；与 P3 共用同一实现） ---
                try:
                    st = page_stats(gray, ylo, yhi)
                    ps_samples.append({"stem": pg["stem"], **st})
                    for k in DRIFT_FEATURES:
                        if st.get(k) is not None:
                            ps_acc.setdefault(k, []).append(st[k])
                except Exception as e:                # pragma: no cover
                    log.warning(f"[layout_contract] page_stats 失败 {pg['stem']}: {e}")
        # --- 切行规则（P1 落盘的 split_rule） ---
        sp = Path(structured_dir) / f"{pg['stem']}.json"
        if sp.exists():
            try:
                doc = json.loads(sp.read_text(encoding="utf-8"))
                for b in ((doc.get("L1_blocks") or {}).get("blocks") or []):
                    if b.get("split_rule"):
                        split_rules[b["split_rule"]] += 1
            except (json.JSONDecodeError, OSError):
                pass

    reading = ("rtl_ttb" if dx_neg > dx_pos else
               ("ltr_ttb" if dx_pos > dx_neg else "unknown"))
    ys = [y for pair in page_ys for y in pair]
    annot_band = [min(ys), max(ys)] if ys else None
    floors = _publication_sd_floors(pages, Path(outbox_dir), annot_band)
    ps_features = {}
    for k in DRIFT_FEATURES:
        s = _feature_summary(ps_acc.get(k, []))
        if s is None:
            ps_features[k] = None
            continue
        fl = floors.get(k) or {}
        ps_features[k] = {**s, "sd_floor": fl.get("sd_floor", SD_FLOOR_MIN),
                          "rel_sd_measured": fl.get("rel_sd_measured")}
    return {
        "reading_order": reading,
        "_reading_votes": {"anchor_dx_neg": dx_neg, "anchor_dx_pos": dx_pos,
                           "same_attr_top_to_bottom": vert},
        "column_profile": {
            "n_columns": int(statistics.median(col_ann)) if col_ann else None,
            "n_columns_samples": col_ann,
            "col_gap_px": round(statistics.median(col_gaps), 1) if col_gaps else None,
            "col_width_px": round(statistics.median(col_widths), 1) if col_widths else None,
            "method": "page-projection (y-restricted to annotated band)",
            "note": "n_columns 为投影带计数，含页眉/边注噪声，仅作参考；"
                    "col_gap_px / col_width_px 更可靠",
        },
        "char_metrics": {
            "cell_h": round(_mode_binned(char_hs), 2) if char_hs
                      else None,
            "cell_h_median": round(statistics.median(char_hs), 2) if char_hs else None,
            "box_w": round(statistics.median(box_ws), 1) if box_ws else None,
            "n_samples": len(char_hs),
        },
        "page_stats": {
            "features": ps_features,
            "samples": ps_samples,
            "band_y_range": annot_band,
            "n_publication_pages_scanned": floors.get("_n_pages_scanned", 0),
            "sd_floor_rule": f"max({SD_FLOOR_MULT} × 同发布物实测 rel_sd, {SD_FLOOR_MIN})",
            "note": "像素级页度量（无需标注/OCR 文本）。P5 漂移检测以此为比对基准："
                    "mean/sd 由**契约源页**给出（n<2 时 sd=None），sd_floor 由"
                    "**同发布物全部页**实测相对离散度给出（逐特征，差两个数量级）",
        },
        "line_split": sorted(split_rules) or None,
        "split_rule_counts": dict(split_rules),
        "regions": {
            "annot_y_range": [min(ys), max(ys)] if ys else None,
            "title_y_range": [min(t[0] for t in title_ys),
                              max(t[1] for t in title_ys)] if title_ys else None,
        },
    }


# ============================ A 层 · 锚定契约 ============================

def _page_text_lines(structured_dir: Path, stem: str) -> List[str]:
    """页 OCR 文本行（原始，未校正口径——与 P3 定位输入一致）。"""
    p = Path(structured_dir) / f"{stem}.json"
    if not p.exists():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [str(l.get("text") or "") for l in (doc.get("L2_lines") or [])]


def _norm_text_of(structured_dir: Path, stem: str) -> str:
    return "".join(normalize_text(t) for t in _page_text_lines(structured_dir, stem))


def induce_anchoring(pages: List[dict],
                     structured_dir: Optional[Path] = None,
                     geometry: Optional[dict] = None) -> dict:
    """A 层：值定位率 / 属性→相对列先验 / 字长-几何自洽。"""
    structured_dir = Path(structured_dir or DEFAULT_STRUCTURED_DIR)
    cell_h = ((geometry or {}).get("char_metrics") or {}).get("cell_h")
    cell_h = cell_h if isinstance(cell_h, (int, float)) and cell_h > 0 else None
    col_gap = (((geometry or {}).get("column_profile") or {}).get("col_gap_px")) or 65.0
    col_gap = float(col_gap) if col_gap else 65.0

    stat = Counter()                     # located / ambiguous / missing / no_text
    per_attr_off: Dict[str, List[float]] = defaultdict(list)
    self_cons: List[float] = []
    examples: List[dict] = []

    for pg in pages:
        page_text = _norm_text_of(structured_dir, pg["stem"])
        vals = _valid_rows(pg["entries"])
        # --- 定位率 / 字长-几何自洽（全部标注值） ---
        for e in vals:
            attr = e.get("attr")
            if not attr:
                continue
            txt = normalize_text(e.get("text"))
            if not txt:
                continue
            if not page_text:
                stat["no_text"] += 1
                hit_n = 0
            else:
                hit_n = page_text.count(txt)
                stat["located" if hit_n == 1 else
                     ("ambiguous" if hit_n > 1 else "missing")] += 1
            if hit_n >= 1 and len(examples) < 40:
                examples.append({"attr": attr, "text": str(e.get("text"))[:24],
                                 "hits": hit_n})
            r0 = first_rect(e)
            if cell_h and r0:
                segs = e.get("segments") or []
                for i, bx in enumerate(e.get("boxes") or
                                       ([e["box"]] if e.get("box") else [])):
                    r4 = rect_of_box(bx)
                    if not r4:
                        continue
                    if (r4[3] - r4[1]) <= (r4[2] - r4[0]):
                        continue                          # 只算竖排段
                    seg = segs[i] if i < len(segs) else ""
                    if len(seg) < 2:
                        continue
                    expect = cell_h * len(seg)
                    if expect > 0:
                        self_cons.append(abs((r4[3] - r4[1]) - expect) / expect)
        # --- 位置先验：**记录内**相对锚属性的列偏移（RTL 下应为负=更左） ---
        anchor = _pick_anchor(vals)
        if anchor:
            recs, _ = _split_by_anchor(vals, anchor)
            for r in recs:
                ax = [(_r[0] + _r[2]) / 2 for _r in
                      (first_rect(e) for e in r if e.get("attr") == anchor) if _r]
                if not ax:
                    continue
                axm = statistics.mean(ax)
                for e in r:
                    a = e.get("attr")
                    _r = first_rect(e) if a else None
                    if not _r:
                        continue
                    per_attr_off[a].append(((_r[0] + _r[2]) / 2 - axm) / col_gap)

    total = sum(stat.values())
    located = stat["located"]
    prior = {}
    for attr, vals in per_attr_off.items():
        vs = sorted(vals)
        n = len(vs)
        prior[attr] = {
            "median_col_offset_from_anchor": round(statistics.median(vs), 2),
            "p25": round(vs[max(0, int(n * 0.25) - 1)], 2),
            "p75": round(vs[min(n - 1, int(n * 0.75))], 2),
            "n": n,
        }
    return {
        "locate_rate": round(located / total, 4) if total else None,
        "counts": dict(stat),
        "n_values": total,
        "col_gap_used": col_gap,
        "col_offset_prior": prior,
        "char_self_consistency": {
            "median_rel_err": round(statistics.median(self_cons), 4) if self_cons else None,
            "n": len(self_cons),
            "cell_h_used": cell_h,
        },
        "examples": examples,
    }


# ============================ 契约装配 / 落盘 / 读回 ============================

def _profile_attr_names(profile_id: str) -> List[str]:
    """档案属性集（权威超集，P3 的 known_attrs 来源）。失败 → 空。"""
    try:
        import profile_store
        prof = profile_store.get_profile(profile_id) or {}
        return [a.get("name") for a in (prof.get("attrs") or []) if a.get("name")]
    except Exception as e:                            # pragma: no cover
        log.warning(f"[layout_contract] 档案读取失败 {profile_id}: {e}")
        return []


def induce_contract(profile_id: str,
                    pages: Optional[List[dict]] = None,
                    ann_dir: Optional[Path] = None,
                    structured_dir: Optional[Path] = None,
                    outbox_dir: Optional[Path] = None,
                    min_rec_sup: float = MIN_RECORD_SUPPORT,
                    crop_ratio: float = CROP_RATIO) -> dict:
    """归纳一份完整契约（G/S/A 三层 + 诊断）。样本不足 → confidence=low。

    `pages` 未给定则走 `collect_pages`（该档案全部已标注页），并经
    `select_source_pages` **剔除裁切页**——被剔页与理由写入 `source.excluded`
    （红线三：漂移可见，不得静默混入异质页）。
    """
    if pages is None:
        allp = collect_pages(profile_id, ann_dir, outbox_dir)
        pages, excluded = select_source_pages(allp, crop_ratio)
    else:
        pages, excluded = pages, []
    if not pages:
        return {"_schema_version": SCHEMA_VERSION,
                "profile_id": profile_id,
                "built_at": datetime.now().isoformat(timespec="seconds"),
                "source": {"pages": [], "n_pages": 0, "n_annotations": 0,
                           "excluded": excluded},
                "G": None, "S": None, "A": None,
                "confidence": "none",
                "diagnostics": {"reason": "该档案没有任何可用的人工标注页",
                                "excluded": excluded}}

    sem = induce_semantics(pages, min_rec_sup)
    geo = induce_geometry(pages, outbox_dir, structured_dir)
    anc = induce_anchoring(pages, structured_dir, geo)

    conf = sem.get("confidence", "low")
    if geo.get("column_profile", {}).get("n_columns") is None:
        conf = "low" if conf in ("none", "low") else "medium"
    return {
        "_schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "pages": [pg["stem"] for pg in pages],
            "n_pages": len(pages),
            "n_annotations": sum(len(pg["entries"]) for pg in pages),
            "excluded": excluded,
        },
        "G": geo,
        "S": sem,
        "A": anc,
        "known_attrs": _profile_attr_names(profile_id),
        "confidence": conf,
        "diagnostics": {
            "anchor": sem.get("anchor"),
            "anchor_conflict": len({p.get("anchor") for p in sem.get("per_page", [])}) > 1,
            "cyclic_attrs": sem.get("cyclic") or [],
            "page_level_attrs": sem.get("page_level") or [],
            "excluded_pages": len(excluded),
        },
    }


#: `G` 层里**不由标注归纳得出**、而由外部通道回迁写入的键。
#: `induce_contract` 是从人工标注归纳 G/S/A 的，归纳不出这些内容 ——
#: 若不搬回，一次例行重建就会把人工确认过的回迁内容**静默抹掉**。
#: 撤销必须是**显式**动作（结构性带：`contract_backfill.py --revoke`）。
ADDITIVE_G_KEYS = ("layout_bands",)


def preserve_additive(new: dict, old: Optional[dict]) -> Tuple[dict, dict]:
    """把旧契约里 `ADDITIVE_G_KEYS` 的内容并回新契约（**只增不改**）。

    返回 `(new, {"kept": n, "keys": [...], "skipped": [...]})`。旧契约缺该键、
    或新契约已显式写了同键 → 不动（保证调用方显式写入优先）。
    """
    report = {"kept": 0, "keys": [], "skipped": []}
    if not isinstance(new, dict) or not isinstance(old, dict):
        return new, report
    og = old.get("G") or {}
    if not isinstance(og, dict):
        og = {}
    # 新契约 `G` 缺/为空（例：该档案暂无标注样本 → `induce_contract` 出 `G=None`）：
    # 回迁内容是**独立于标注的证据**，不因标注暂时没长出来就丢；但留痕（见下 warning）。
    empty_g = not isinstance(new.get("G"), dict)
    ng = new.get("G")
    if empty_g:
        ng = {}
        new["G"] = ng
    for k in ADDITIVE_G_KEYS:
        v = og.get(k)
        if not v:
            continue
        if ng.get(k):
            report["skipped"].append(k)
            continue
        ng[k] = v
        report["keys"].append(k)
        report["kept"] += len(v.get("bands") or []) if isinstance(v, dict) else 1
    if report["keys"]:
        log.info(f"[layout_contract] 重建保留非归纳键 {report['keys']}"
                 f"（{report['kept']} 项）")
        if empty_g:
            log.warning("[layout_contract] 新契约 G 层为空（无标注样本？），"
                        f"仍保留非归纳键 {report['keys']} —— 它们是独立证据，"
                        "清掉需要显式撤销（contract_backfill --revoke）")
    return new, report


def save_contract(contract: dict,
                  contracts_dir: Optional[Path] = None,
                  *,
                  preserve: bool = True) -> Optional[Path]:
    """落盘 `layout_<profile_id>.json`；profile_id 非法 → None。

    `preserve=True`（默认）先 `preserve_additive`：把盘上旧契约里的非归纳键
    （`ADDITIVE_G_KEYS`）原样搬回。契约自 P-C 起承载**人工确认过的回迁内容**，
    重建时静默丢掉它正是本项目反复防的那类缺陷。

    写入走 `data_io.atomic_write_json`（M7）：契约现在带有人工确认内容，
    半截文件不可接受。
    """
    pid = contract.get("profile_id") or ""
    if not pid.startswith("prof_"):
        return None
    d = Path(contracts_dir or DEFAULT_CONTRACTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"layout_{pid}.json"
    if preserve and p.exists():
        old = None
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = None
        contract, _rep = preserve_additive(contract, old)
    import data_io
    data_io.atomic_write_json(p, contract)
    log.info(f"[layout_contract] 契约已保存: {p.name}")
    return p


def load_contract(profile_id: str,
                  contracts_dir: Optional[Path] = None) -> Optional[dict]:
    """读回契约；不存在 → None。"""
    p = Path(contracts_dir or DEFAULT_CONTRACTS_DIR) / f"layout_{profile_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[layout_contract] 契约读取失败 {p.name}: {e}")
        return None


def build_contract(profile_id: str, save: bool = True, **kw) -> Tuple[dict, Optional[Path]]:
    """归纳 + （默认）落盘。"""
    c = induce_contract(profile_id, **kw)
    return c, (save_contract(c, kw.get("contracts_dir")) if save else None)


# ============================ 校验（契约自检 / 留出页评分） ============================

def score_pages(contract: dict, pages: List[dict]) -> dict:
    """用契约给**任意页**打分（P2 验收 + 未来 P5 漂移检测共用同一实现）。

    - `attr_cov`     属性覆盖率：页内属性落在（模板主体 ∪ 档案属性集）的比例
    - `order_mono`   序单调率：**仅模板主体属性**参与的相邻对，在模板序上递增的比例
    - `order_mono_all` 同口径但含模板外属性（罕见属性会被判逆序，指标更严）
    - `record_fit`   实际记录大小落在契约 cardinality 区间的比例
    """
    sem = contract.get("S") or {}
    tmpl = sem.get("record_template") or []
    extra = [a for a in (contract.get("known_attrs") or []) if a not in tmpl]
    idx = {a: i for i, a in enumerate(tmpl)}
    for j, a in enumerate(extra):
        idx.setdefault(a, len(tmpl) + j)

    tot = hit = pairs = ok = pairs_all = ok_all = 0
    sizes: List[int] = []
    for pg in pages:
        valid = _valid_rows(pg["entries"])
        anchor = sem.get("anchor") or _pick_anchor(valid)
        if not anchor:
            continue
        recs, _ = _split_by_anchor(valid, anchor)
        for r in recs:
            named = _named_of(r)
            sizes.append(len(named))
            for a in named:
                tot += 1
                if a in idx:
                    hit += 1
            for (a1, a2) in zip(named, named[1:]):
                pairs_all += 1
                v1, v2 = idx.get(a1), idx.get(a2)
                if v1 is not None and v2 is not None and v2 > v1:
                    ok_all += 1
                if a1 in tmpl and a2 in tmpl:
                    pairs += 1
                    if idx[a2] > idx[a1]:
                        ok += 1
    card = sem.get("cardinality") or {}
    fit = 0
    if sizes and card:
        lo = card.get("p10", card.get("min", 0))
        hi = card.get("p90", card.get("max", 0))
        fit = sum(1 for s in sizes if lo <= s <= hi) / len(sizes)
    return {
        "attr_cov": round(hit / tot, 4) if tot else None,
        "order_mono": round(ok / pairs, 4) if pairs else None,
        "order_mono_all": round(ok_all / pairs_all, 4) if pairs_all else None,
        "record_fit": round(fit, 4) if sizes else None,
        "n_values": tot, "n_pairs": pairs, "n_records": len(sizes),
    }


if __name__ == "__main__":                        # 手工跑：python layout_contract.py <profile_id> [stem1,stem2,...]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not _args:
        sys.exit("用法：python layout_contract.py <profile_id> [stem1,stem2,...]")
    pid = _args[0]
    _pages = collect_pages(pid)
    if len(_args) > 1 and _args[1]:
        _want = set(_args[1].split(","))
        _pages = [p for p in _pages if p["stem"] in _want]
        print(f"限定页: {_want}")
    c = induce_contract(pid, pages=_pages)
    path = None if "--no-save" in sys.argv else save_contract(c)
    print(json.dumps({k: c[k] for k in ("profile_id", "source", "confidence", "diagnostics")},
                     ensure_ascii=False, indent=2))
    print(f"G: {json.dumps(c.get('G'), ensure_ascii=False)[:600]}")
    print(f"S.template: {(c.get('S') or {}).get('record_template')}")
    print(f"A.locate_rate: {(c.get('A') or {}).get('locate_rate')}")
    print(f"\n契约文件: {path}")
