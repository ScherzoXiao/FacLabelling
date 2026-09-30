# -*- coding: utf-8 -*-
"""层 3 可视化：`seam_map` —— 「层1 观测 → 层2 方案 → 层3 执行器〔虚线框 = 支持自定义环节〕」
的类型化 IR + **确定性**渲染器。

★ P-S13 起，图上**没有虚线边**：四段关系一律实线。虚线的载体从「边」搬到「区域」——
  虚线框住的那块（声明了 `iface` 的节点）就是可自定义的环节，框下有一行注明。

解决什么问题
------------

本项目的三层架构里，「层 2 方案」是**唯一接口物**，层 3 可整体拆卸（换成用户自己的
agent）。但这条接缝此前**没有任何界面入口** —— `data/results/` 里那批产物是同一份 plan
被某个执行器跑出来的结果，却只躺在文件系统里。

★ 2026-09-13（用户裁定）：**执行层只画一个节点、产物只留一份。**
  此前画两个节点（`.result.json` / `.external.json`）是刻意并列"同一 plan、两个执行器"
  这个可替换性证据，但用户指出：**执行层作为可拆卸设计本来就不该有"默认"项，也就不该
  有二择** —— 执行层就是"方案被跑成了产物"，谁跑（系统自带的参照执行器，还是用户自己的
  agent）不改变它的身份。故产物统一落在 `data/results/<stem>.result.json`。
  "可替换"这件事并没有消失（接缝照样在那儿：它现在是**框住执行层的那条虚线框**，
  P-S13 之前是那条虚线边），只是不再用两个并列节点去表达。
  历史 `.external.json` 已 `mv` 进 `_archive/`（可逆，不是删除）。

参照物 `tt-a1i/archify` 的可搬之物是**方法论**（结构 → 类型化 IR → 确定性编译 → 可验证），
不是它的代码：

  1. **不引入 Node**：本分发包是纯 Python（PyInstaller）→ 用 Python 重实现「IR → 内联 SVG」。
  2. **不引外部资源**：不引 mermaid / d3 / 外部字体（承 §9.8「内联 SVG、零外部库」）。
  3. **视觉承包豪斯**：无圆角、无阴影、1px 描边、方形状态点，只取 `bauhaus.css` 的 token。

两条硬纪律（改动前先读）
------------------------

**① 点位与布线都在 IR 里，渲染器不做自动布局。**
   `render_svg` 不读任何文件、不 import 任何数据模块，只消费 IR 的 `canvas/nodes/edges`。
   这一条同时让渲染变成确定性的：同输入 → **逐字节同输出**（可写单测锁死）。

**② 渲染器不计算业务数字。**
   图上每个 `v` 都是 IR 构造器算好后写进去的字符串，且每个都带 `evidence.file + field`。
   渲染器只做几何派生（节点内文本按固定行高排布、边的正交折线），**不碰业务值**。
   单测守这一条：SVG 里出现的每个 facts 值都必须**逐字**来自 IR。

读法
----

    python seam_map.py --profile prof_xxx          # 打印 IR 摘要
    python seam_map.py --profile prof_xxx --svg out.svg

API 侧见 `app.py::/api/seam_map`（只读路由，无 auth）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("seam_map")

ROOT = Path(__file__).resolve().parent

SCHEMA_VERSION = "0.1.0"
KIND = "seam_map"

DEFAULT_PLANS_DIR = ROOT / "data" / "plans"
DEFAULT_FACTS_DIR = ROOT / "data" / "plan"
DEFAULT_RESULTS_DIR = ROOT / "data" / "results"
DEFAULT_GOLD_DIR = ROOT / "manual_annotations"   # 金标准行自带 profile 字段，可按档案过滤

# ---- 节点状态（颜色只表达状态，不表达评价） ----
ST_OK = "ok"              # 正常
ST_STALE = "stale"        # 与上游输入不一致（赭红）
ST_MISSING = "missing"    # 产物不存在（灰框，保留位置）
ST_DEGRADED = "degraded"  # 存在但降级

# ---- 边类型 ----
# ★ 2026-09-13（P-S13，用户圈注）：「方案执行层前面的箭头就不再需要用虚线了，和其他地方
#   一样用实线就行。」⇒ `detachable` **不再用线型说话**，它回到纯粹的**语义**标记：
#   说的是"接缝在哪"。而"这条接缝之后那一块可以被你换掉"由 `frames`（虚线框）承担 ——
#   虚线从此是**区域**的语言，不再是**线**的语言（一处说一件事；两处都说就成了噪声）。
EK_FLOW = "flow"
EK_DETACHABLE = "detachable"

# ---- 布线。IR 显式给定，渲染器照做，不做自动避让 ----
RT_H_DIRECT = "h_direct"    # 同一水平线上：出右边 → 直入左边
RT_H_ELBOW = "h_elbow"      # 水平肘形：出右边 → 水平到 lane → 垂直 → 水平入左边

# ---------------------------------------------------------------------------
# 布局常量（点位进 IR；渲染器只按 IR 画）
# ---------------------------------------------------------------------------
# ★ 为什么是**横向**布局：这一块要占满总览页主区（宽 > 高）。纵向图在 `width=100%`
#   下会等比放大成近千像素高；固定的 1120×300 既贴合主区宽高比，又让 10.5px 的字号
#   在任何屏宽下都保持一致（渲染器输出**固定像素尺寸**，不写 `width="100%"`）。
CANVAS_W = 1120
NODE_W = 180
NODE_WIDE = 200       # plan 节点略宽（标题最长）
NODE_MIN_H = 56
PAD_TOP = 9           # 节点内 label 基线偏移
LINE_H = 15           # 节点内 facts 行高
PAD_BOTTOM = 7
PAD_X = 11            # 节点内文本左边距
GUTTER = 25           # 画布四周留白 —— **以全部墨迹的外接框为准**（节点、虚线框、框下标注都算）

# ★★ 2026-09-13（P-S11，用户圈注）：「上下居中问题还是没有解决，上部分明显留出了大片空白。」
#   上一轮（P-S10）修的是 **CSS 的横向**（`.seam-canvas` 整宽块 → `width:fit-content`），
#   用户这次说的是**纵向** —— 两次圈注长得像，指的却是两个轴向。这是本次取证的第一条。
#
# 根因（逐行核对旧版备份）：
#   `ROW_Y = 111` 的原注释是「主轴（单行节点）的 y —— 与两个执行器的中轴对齐」。
#   那时执行层是**两个并列节点**（`exec_default` / `exec_external`），分别住在
#   `COL_Y = {exec_default: 36, exec_external: 186}`（各自高 56）→ 这对节点的纵向中点 = 139；
#   其余四个节点取 `ROW_Y = 111`，配 `NODE_MIN_H = 56` 时中心 = 111 + 28 = 139 —— **正好对上**。
#   ⇒ `111` 当时是个**有依据的中间量**，不是随手填的。
#
#   后来 P-S8 把两个执行器**合并成一个**（用户裁定"执行层不该有二择"），
#   那条"与两行中轴对齐"的前提**整体消失了**，而 `111` 原地留了下来。
#   于是单行布局被推到画布下半部：实测上留白 111px / 下留白 25px
#   逐行墨迹统计：109 / 8，比值 13.7），1120×227 的画布里近一半是空背景。
#   ⇒ 一般的形态：**一个常量的前提被撤掉了，而常量还在**。改内容的人不会去看布局常量。
#
#   正解：让布局**只认内容**。上留白 = `GUTTER`，下留白 = `GUTTER`，画布高度由内容外接框派生。
#
# ★★ 2026-09-13（P-S13，用户第二次就这张图给出的圈注）：「先把左下角虚线图例删除，
#   再像截图里那样，使用虚线方框框住执行层，虚线框下方注明『支持自定义环节』；
#   这时候方案执行层前面的箭头就不再需要用虚线了，和其他地方一样用实线就行。」
#   ⇒ 这一改把**虚线这个视觉载体从「边」搬到了「框」**上，于是留白这层也得换模型：
#     旧模型：内容 = 节点行；图例是行下方一条**另算的带**（`legend_band`）。
#     新模型：内容 = 节点 + 框 + 框下标注；留白 = 这三样**外接框**的四周各 `GUTTER`。
#   为什么非换不可：框比节点**高出 `FRAME_PAD`**。若节点行顶边仍钉在 `GUTTER`，
#   画布上边就只剩 `GUTTER - FRAME_PAD` —— 又一次纵向失衡（§62 那一课的重演）。
#   ⇒ 一般形态：**给某物加一圈外框时，留白要按框算，不能还按被框的东西算。**
ROW_Y = GUTTER        # 节点行顶边（**无框时**的值）；有框时由 build 再往下让 `FRAME_PAD`

# 可自定义区域（虚线框 + 框下标注）。★ 这几个数字**同源**给布局与渲染器用 ——
#   渲染器里不许再出现"框底边再加 10"这类与布局无关的字面量（§62 的教训：
#   两处各自"看着都挺合理"的字面量，改一处就会静默错位）。
FRAME_PAD = 9            # 框边 → 被框节点边（四周同值）
FRAME_LABEL_GAP = 13     # 框底边 → 标注文字**基线**
FRAME_LABEL_DESCENT = 3  # 标注基线 → 其下沿（11.5px 字的下沉量，向上取整）
FRAME_DASH = "6 4"       # 虚线样式（与「未产出」节点的 3 3 区分开：一眼不同族）
FRAME_LABEL = "支持自定义环节"
FRAME_ID = "custom_zone"  # 框的稳定 id（前端锚点 / 测试引用都不该靠"第几个框"）
# ★ 2026-09-13 重排：执行层由两个并列节点并成一个，四段间隙**均分**（各 40px）。
#   均分是刻意的 —— 原先 exec 列留了 72px 的肘形空间给分叉，合并后那段空白会让
#   「方案设计层 → 方案执行层」看起来比别的相邻关系更远（视觉在暗示一个不存在的分组）。
#
# ★★ 2026-09-13（P-S9b，用户圈注）：**同一行内所有节点等高**（行高由该行最"高"的节点决定）。
#   用户原话："seam 校验前面这个箭头还是弯折的，不美观，需要调整。"
#   折线的根因是**合并前的分叉残迹**（两个执行器一上一下才需要肘形），P-S8 合并后已无语义；
#   但直接"把折线掰直"会撞上另一条纪律 —— 要中心等高，就得让 `y` 随 `h` 变，而 `h` 随
#   facts 行数变 ⇒ **换档案时节点位置会跳**（实测空档案与有产物差 8px，被
#   `test_empty_profile_positions_identical` 当场抓到）。两条纪律单靠"按内容定高 + 竖直居中"
#   无法同时满足，故正解是**让高度本身不再是变量**：
#     `row_h = max(每个节点各自需要的高度)`，所有节点补足到 `row_h`，顶边一律 `ROW_Y`。
#   于是：中心 = `ROW_Y + row_h//2` **恒等** ⇒ 四条边都是水平直线；`y` 恒定 ⇒ 位置不跳。
#   代价是短节点多出几行空白 —— **不占额外画布**（画布高度本就由最高节点决定），
#   反而把原先的参差边收成一条整齐的列，更贴合包豪斯的网格语言。
COL_X = {             # 列 x
    "gold": 20,
    "facts": 240,
    "plan": 460,
    "exec": 700,
    "validate": 920,
}

# 节点 id → (kind 语义, 标题)
#
# ★ 2026-09-12 改标签：从"层号 + 内部术语"改成**用户工作流里的话**。
#   用户原话：「屏幕上无需显示那么多非专业用户看不懂的数字」「只需要让用户知道…
#   存在一个方案设计层…方案执行层根据接口物设计对应标注方案…最后抵达 seam 校验」。
#   故标签一律用「金标准页 / 规则提取 / 方案设计层 / 方案执行层 / seam 校验」；
#   层号退成副标签（`· 层 1`），因为它仍是文档与代码里的通用坐标，不能丢。
#   ★ 2026-09-13：`exec_default` / `exec_external` **合并**为单个 `exec`
#     （用户裁定"执行层不该有二择"；理由见模块 docstring）。
NODE_SPEC: Dict[str, Tuple[str, str]] = {
    "gold":          ("stage", "金标准页（例题）"),
    "facts":         ("layer1", "规则提取 · 层 1"),
    "plan":          ("layer2", "方案设计层 · 层 2"),
    "exec":          ("exec", "方案执行层"),
    "validate":      ("check", "seam 校验"),
}

# 拓扑：有向边。`detachable` 只有一条 —— 从 plan 分出去的那条（执行层可替换）。
#
# ★ 2026-09-13（P-S9b）：**四条边一律 `h_direct`**。全部节点共用一条竖直中轴
#   （见 `COL_X` 上方注释），故水平直线正好从 a 右边缘连到 b 左边缘，箭头落在 b 的中心高度上。
#   `RT_H_ELBOW` 仍保留在枚举里（渲染器的通用能力，供节点高度不齐的版式备用），但**本图不再使用** ——
#   IR 里的 `route` 必须描述**真实画法**：留一个"实际画成直线、却声明为肘形"的字段，就是让 IR 说谎。
EDGE_SPEC: List[Tuple[str, str, str, str, str]] = [
    # (from, to, kind, route, label)
    #
    # ★ P-S13：接缝那条边的 `label` 清空。它标的是**接缝的位置**（`kind` 已经说了），
    #   画出来是一条**实线**、与其余三条完全相同；"可自定义"这四个字现在住在**框下的标注**里。
    #   另外：渲染器**从来不画**边的 `label` —— 留一条"IR 有、图上没有"的文字字段，
    #   就是让 IR 说了句没画出来的话（与"`route` 必须描述真实画法"同源）。故清空。
    ("gold",          "facts",         EK_FLOW,       RT_H_DIRECT, ""),
    ("facts",         "plan",          EK_FLOW,       RT_H_DIRECT, ""),
    ("plan",          "exec",          EK_DETACHABLE, RT_H_DIRECT, ""),
    ("exec",          "validate",      EK_FLOW,       RT_H_DIRECT, ""),
]

# ★ 2026-09-13：**只有一种产物文件名**（用户裁定"执行层不该有二择"）。
#   系统自带的参照执行器（`plan_exec.py`）与用户自己的 agent 写的是**同一个文件**，
#   谁后写谁生效 —— 这正好是"可拆卸"的本义，也省掉了一条要用户记住的分支约定。
#
# ★★ 2026-09-13（P-S13）：把它从**节点 id 的字典**里提出来，单独成一个常量。
#   原先三处写着 `_IFACE_SUFFIX["exec"]` 来取"产物叫什么名字" —— 那是把两件事
#   绑在同一个字典上：
#     · `PRODUCT_SUFFIX`（这一份产物叫什么）—— 全系统只有一种，**与节点无关**；
#     · `_IFACE_SUFFIX`（**哪个**节点对外提供接入口）—— 现在的答案是 `exec`。
#   拆开之后，"没有可自定义区域"才是一个**真的存在的形状**（只清空后者即可），
#   否则 `row_y` 里那句 `else 0` 的分支永远没人走过。
PRODUCT_SUFFIX = ".result.json"

# 有接入口的节点 id → 它的产物后缀。**由 `PRODUCT_SUFFIX` 派生**，不另写一份字符串
# （写死两份迟早对不上：`_exec_scan` 扫的、glob 拼的、图上给用户看的必须是同一个）。
_IFACE_SUFFIX: Dict[str, str] = {
    "exec": PRODUCT_SUFFIX,
}

# 取值纪律：所有字符串都是**已格式化**的展示值（千分位 / 百分比在这里做完）。
# 渲染器不再加工，故图上不会出现与产物不一致的数字。


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def gold_scope(profile_id: str, ann_dir: Optional[Path] = None) -> Dict[str, int]:
    """该**档案**在金标准里的实际规模 → `{n_pages, n_rows, n_foreign}`。

    这是"我需要提供金标准页"那件事的真实读数：金标准行自带 `profile` 字段
    （`{attr, box, image_name, profile, source, text, ts}`），故按档案过滤是**原始事实**，
    不需要任何推断。

    - `n_pages`  ：有本档案标注行的页数
    - `n_rows`   ：这些页里属于本档案的**行数**（含 `attr` 为空但确是真值框的行 ——
      那是人工框出来的实体，不是噪声；`rule_learn.load_gold` 会丢掉它们，
      故此处不能用它来数"标了多少条"）
    - `n_foreign`：**有标注行、但没有一行属于本档案**的页数。它单独计数，是为了让
      「规则提取读的是全部金标准、不按档案过滤」这件事在图上**看得出来**，
      而不是两个数并排摆着让人猜为什么不一样。

    ⚠ 读金标准**只有一处实现**（`gold_facts.load_gold_boxes`），此处不再写第二份解析。
    """
    d = Path(ann_dir or DEFAULT_GOLD_DIR)
    n_pages = n_rows = n_foreign = 0
    try:
        import gold_facts
        boxes = gold_facts.load_gold_boxes(d)
    except Exception:  # noqa: BLE001 —— 金标准目录缺失/模块不可用都退化为"零"
        return {"n_pages": 0, "n_rows": 0, "n_foreign": 0}
    for _stem, rows in boxes.items():
        own = [r for r in rows if str(r.get("profile") or "") == profile_id]
        if own:
            n_pages += 1
            n_rows += len(own)
        else:
            n_foreign += 1
    return {"n_pages": n_pages, "n_rows": n_rows, "n_foreign": n_foreign}


def layout_coverage(plan: Optional[dict]) -> Dict[str, Any]:
    """plan → **版式覆盖读数**（P0-3，2026-09-13）。零重算：只做计数与集合运算。

    为什么这条要上界面
    ------------------
    `plan.degraded` 此前**只写在 JSON 里**，用户在总览页看不见。而它说的是一件
    **用户必须知道才能正确使用产出**的事 —— 异族页的产出不可靠。这不违背
    "撤体检表"的裁定：撤掉的三绿方块说的是**节点状态点已经在说的事**（信息冗余），
    这一条是新的、且读者要拿它做决定（"这页能不能信"）。

    ⚠ 判据一律读**结构化字段**，不解析 `plan.degraded[].reason` 的中文串
    ------------------------------------------------------------------
    那串中文是**给人看的措辞**，改一个标点就会让解析静默失效 —— 与「数字对了、
    说明文字是陈的」是同族病。故：

      · 有几个**可归纳**的族 ← `page_model.layout_clusters[].coverage`
      · 哪些页所属族无跨页证据 ← `page_model.regions[].covered`

    这两个字段都由 `plan_build` 一次判好（判据唯一出处在那里），本函数**只读**。

    返回
    ----
    `{n_families, n_inducible, n_pages, n_uncovered, uncovered}`；
    `uncovered` 是页 stem 的 **set**（供与产物名单求交），其余是计数。
    plan 缺失 / 老版 plan 没有 `page_model` 时全部退化为零 —— 不抛。
    """
    pm = (plan or {}).get("page_model") or {}
    clusters = pm.get("layout_clusters") or []
    regions = pm.get("regions") or []
    uncovered = {str(r.get("stem") or "") for r in regions
                 if not (r or {}).get("covered")}
    uncovered.discard("")
    return {
        "n_families": len(clusters),
        "n_inducible": sum(1 for c in clusters
                           if (c or {}).get("coverage") == "inducible"),
        "n_pages": len(regions),
        "n_uncovered": len(uncovered),
        "uncovered": uncovered,
    }


def _ev(rel: str, field: str = "") -> Dict[str, str]:
    """证据指针：图上每个数字都必须能追到「哪个文件的哪个字段」。"""
    return {"file": rel, "field": field}


def _rel(p: Any) -> str:
    """绝对路径 → **项目相对路径**（正斜杠）。

    ★ 图上出处必须是**真能打开**的路径。早先写死 `data/plans/plan_<pid>.json` 这种占位符，
    界面上「复制全部出处路径」复制出来的就是一个无效路径 —— 那等于把"可追溯"做成装饰。
    """
    s = str(p or "")
    if not s:
        return ""
    try:
        return str(Path(s).resolve().relative_to(ROOT)).replace("\\", "/")
    except Exception:
        return s.replace("\\", "/")


def _rel_dir(p: Any) -> str:
    """目录 → **项目相对路径**（带尾部 `/`）。

    ★ 出处必须指向**实际使用的位置**，不能写死 `data/plans/`：一旦调用方换了目录
    （测试夹具、多档案分仓），写死的路径就变成一个打不开的位置 —— 与占位符同一种病。
    """
    s = _rel(p)
    if not s:
        return ""
    return s if s.endswith("/") else s + "/"


def _disp_w(s: str) -> int:
    """字符串的显示宽度（半角 1 / 全角 2）。CJK 与全角标点按 2 计。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def _fit(v: Any, limit: int = 24) -> str:
    """把值压进节点宽度：按**显示宽度**截断，超出加省略号。

    节点内可用的值区约 158px（`NODE_W - 2*PAD_X`），10.5px 字号下 ≈24 个显示宽度单位。
    截断**只在展示层**：完整值由调用方放进 `full`，供详情面板与「一键复制路径」用
    （值太长时图上宁可省略号，也不要与左侧标签挤在一起 —— 后者会让两段文字**互相覆盖**，
    是"看不出损坏"的典型）。
    """
    s = str(v if v is not None else "—")
    if _disp_w(s) <= limit:
        return s
    out: List[str] = []
    w = 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > limit - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def _fit_facts(facts: Sequence[dict]) -> List[dict]:
    """统一把一组 facts 的展示值压宽（**单一入口**：所有节点都走这里）。

    完整值优先级：显式 `full` > `full_hint`（构造器给的"该值的完整形态"）> 被截断的原值。
    """
    out: List[dict] = []
    for it in facts or []:
        raw = str(it.get("v") if it.get("v") is not None else "—")
        shown = _fit(raw)
        item = {"k": it.get("k"), "v": shown, "evidence": it.get("evidence") or {}}
        full = it.get("full") or it.get("full_hint")
        if full and str(full) != shown:
            item["full"] = str(full)
        elif shown != raw:
            item["full"] = raw
        out.append(item)
    return out


def _merge_evidence(facts: Sequence[dict]) -> List[dict]:
    """把一个节点所有 facts 的 evidence 去重汇总（保序），供界面"一键复制路径"用。"""
    seen = set()
    out: List[dict] = []
    for f in facts or []:
        e = f.get("evidence") or {}
        key = (e.get("file"), e.get("field"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"file": e.get("file") or "", "field": e.get("field") or ""})
    return out


# ---------------------------------------------------------------------------
# 档案选择
# ---------------------------------------------------------------------------
def latest_plan_profile(plans_dir: Optional[Path] = None) -> Optional[str]:
    """挑一个档案：有 plan 的里面，取 plan 文件最新的那个。

    没有 plan 时返回 None（调用方据此画"未产出"图，而不是报错）。
    """
    d = Path(plans_dir or DEFAULT_PLANS_DIR)
    if not d.is_dir():
        return None
    best: Optional[Tuple[float, str]] = None
    for p in d.glob("plan_*.json"):
        pid = p.stem[len("plan_"):]
        if not pid:
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, pid)
    return best[1] if best else None


def plan_path_of(profile_id: str, plans_dir: Optional[Path] = None) -> Path:
    return Path(plans_dir or DEFAULT_PLANS_DIR) / f"plan_{profile_id}.json"


# ---------------------------------------------------------------------------
# 各节点的构造
# ---------------------------------------------------------------------------
def _node_facts_gold(scope: Dict[str, int], rel_gold: str,
                     decoupled: bool = False) -> Tuple[List[dict], str]:
    """① 金标准页 —— 该**样式**已有的例题规模（用户要动手提供的那件事）。

    ★ 2026-09-12 前后有别：此前这里读 `plan.source.n_pages`（= facts 收录的页数，
    而 facts 读金标准时**不按档案过滤**）。改成按档案过滤后，"我需要提供几页"
    与"方案实际学到几页"才是两个各自诚实、各自可追的数字。

    `decoupled` = 金标准那套**不是**本样式那套（用户在③里另选过）。此时**只加一行**
    把它说破 —— 图上宁可多这一行，也不能让读者以为"这几页例题就是这套样式的"。
    """
    n_pages = int(scope.get("n_pages") or 0)
    n_rows = int(scope.get("n_rows") or 0)
    rel = f"{rel_gold}*.jsonl"
    if n_pages <= 0:
        return ([{"k": "金标准", "v": "尚未提供",
                  "evidence": _ev(rel, "行内 profile 字段")}], ST_MISSING)
    out = [
        {"k": "例题页", "v": f"{n_pages} 页", "evidence": _ev(rel, "行内 profile 字段")},
        {"k": "标注条数", "v": f"{n_rows}", "evidence": _ev(rel, "行内 profile 字段")},
    ]
    if decoupled:
        out.append({"k": "版本", "v": "另选一套",
                    "evidence": _ev(rel, "行内 profile 字段（≠ 本样式档案）")})
    return out, ST_OK


def _node_facts_facts(facts_doc: Optional[dict], rel_facts: str,
                      rel_gold: str, n_foreign: int = 0) -> Tuple[List[dict], str]:
    """② 规则提取（层 1 观测）—— 层 2 的三份输入之一。

    `n_foreign` > 0 时**额外说一行**，因为那是图上唯一一处"两个页数看起来对不上"：
    规则提取读的是**全部**金标准，只有所选那套那几页才是它该学的。不说，
    用户就只会看到两个不一样的数字，然后开始怀疑哪个是错的。

    措辞用「**所选**金标准」而不是「本样式」：③可以另选一套金标准，
    那时"本样式"就不再是这几页的归属了。一个词同时成立的写法，才不需要两套文案。
    """
    if facts_doc is None:
        return ([{"k": "观测", "v": "未运行",
                  "evidence": _ev(rel_facts, "存在性")}], ST_MISSING)
    pages = (facts_doc or {}).get("pages")
    n = len(pages) if isinstance(pages, list) else 0
    out: List[dict] = [{"k": "观测页", "v": f"{n} 页", "evidence": _ev(rel_facts, "pages")}]
    if n_foreign:
        out.append({"k": "另有", "v": f"{n_foreign} 页未归所选金标准",
                    "evidence": _ev(rel_facts, "pages ⇄ 金标准行内 profile")})
    return out, ST_OK


def _refs_status(plan: dict, *, rel_plans_dir: str = "data/plans/") -> Tuple[List[dict], str]:
    """三份输入（契约 / learned / facts）的现况：**只有一行**，但状态是全的。

    返回 `([{k:"输入", v:"与方案一致"|"有变动（facts 变）"}], ok|stale)`。
    明细（哪份输入变了）写进值里；逐项路径不进节点 —— 节点宽度有限，逐项列出会与
    左侧标签重叠；完整路径在 click 后的详情面板（IR 的 `evidence` 已指向 `refs.*.sha256`）。

    ⚠ **本函数答的问题与 `plan_build` 落盘的 `refs.*.stale` 不是同一个**（2026-09-12 分工定案；
    此前这里写的是「plan_build 里 `stale` 从未落盘」，那个缺口当天已补）：
      · `plan_build` 落盘的是**历史**：相对**盘上上一版 plan**，哪份输入的 sha256 变过。
      · 本函数算的是**现状**：相对**此刻**盘上的输入文件，plan 里记的 sha256 还成立吗。
    两者都要有，谁也不能替谁 —— plan 编译出来的那一刻，"现状"必然一致；能问出
    「现在过期没有」的只有使用现场。故本模块**只读地**重算并比对，绝不去改层 2 的产物。
    """
    refs = plan.get("refs") or {}
    changed: List[str] = []
    missing: List[str] = []
    for key in ("contract", "learned", "facts"):
        r = refs.get(key) or {}
        path = r.get("path")
        recorded = r.get("sha256")
        if not r.get("present") or not path:
            missing.append(key)
            continue
        cur = _sha256(Path(path))
        if cur and recorded and cur != recorded:
            changed.append(key)
    state = ST_OK if (not changed and not missing) else ST_STALE
    # ★ 值是**人话**，不是 `3/3` 那种分数 —— 分子分母都是内部分类，对读者零信息。
    #   "哪份输入变了"才是读者要的，故写进值里（`facts` / `learned` 是真实的输入名）。
    v = "与方案一致"
    if changed and missing:
        v = f"有变动（{'+'.join(changed)} 变、{'/'.join(missing)} 缺）"
    elif changed:
        v = f"有变动（{'+'.join(changed)} 变）"
    elif missing:
        v = f"有变动（{'/'.join(missing)} 缺）"
    # 出处给**三份输入各自的真实路径**（不是 plan 自己的路径）—— 用户要核的正是它们
    ev = [_ev(_rel((refs.get(k) or {}).get("path")), f"refs.{k}.sha256")
          for k in ("contract", "learned", "facts")]
    ev = [e for e in ev if e["file"]]
    if not ev:
        ev = [_ev(rel_plans_dir, "refs")]
    return ([{"k": "输入", "v": v, "evidence": ev[0]}], state)


def _exec_scan(results_dir: Path, suffix: str) -> Tuple[List[str], List[dict]]:
    """扫 `data/results/`，返回 (页 stem 列表, 该执行器的结果文档列表)。"""
    d = Path(results_dir)
    if not d.is_dir():
        return [], []
    stems: List[str] = []
    docs: List[dict] = []
    for p in sorted(d.glob(f"*{suffix}")):
        stem = p.name[: -len(suffix)]
        doc = _load_json(p)
        stems.append(stem)
        docs.append({"path": p, "doc": doc})
    return stems, docs


def _exec_plan_ref_state(docs: List[dict], plan: Optional[dict]) -> Tuple[Optional[str], bool]:
    """产物的 `plan_ref.built_at` 与当前 plan 的 `built_at` 是否一致。

    ⚠ **弱判据，如实标注**：result 里只记了 plan 的 schema 与 built_at，**没有 sha256**，
    故只能在时间戳不同层判定"确定不是这一版"；时间戳相同**不能**证明同源。
    """
    if not plan or not docs:
        return None, False
    cur = plan.get("built_at")
    refs = set()
    for d in docs:
        doc = d.get("doc") or {}
        pr = doc.get("plan_ref") or {}
        b = pr.get("built_at")
        if b:
            refs.add(str(b))
    if not refs:
        return None, False
    if len(refs) == 1:
        only = next(iter(refs))
        if cur and only != cur:
            return only, True   # 确定 stale
        return only, False
    return f"{len(refs)} 个版本", True


def _node_facts_exec(stems: List[str], docs: List[dict],
                     plan: Optional[dict], suffix: str, rel_dir: str) -> Tuple[List[dict], str]:
    """执行层节点：**标了几页 + 出了几条记录**，以及依据的方案是不是旧的。

    ★ 2026-09-13：本函数原先被调**两次**（默认执行器 / 外部 agent 各一个并列节点）。
      用户裁定合并为**一个执行层节点**（"执行层不该有默认项，也就不该有二择"），
      故现在只调一次；口径与文案不变 —— 两个实现仍然共用这一套 facts 口径。

    2026-09-12 精简（用户：屏幕上无需那么多看不懂的数字）：删掉「字节」「基于 plan 的
    时间戳」「执行器 reference/external」三行 —— 前两个是内部量，第三个与节点标题重复。
    方案新旧改说人话（`依据 / 旧版方案`），且**只在真的旧时**才出现。
    """
    n = len(stems)
    rel = f"{rel_dir}*{suffix}"
    if n == 0:
        return ([{"k": "已标注", "v": "尚未开始",
                  "evidence": _ev(rel, "文件计数")}], ST_MISSING)
    out: List[dict] = [{"k": "已标注", "v": f"{n} 页",
                        "evidence": _ev(rel, "文件计数")}]
    # 产出的记录数（两执行器的差异在这里第一次并列可见）
    n_rec = None
    for d in docs:
        doc = d.get("doc") or {}
        recs = doc.get("records")
        if isinstance(recs, list):
            n_rec = (n_rec or 0) + len(recs)
    if n_rec is not None:
        out.append({"k": "记录", "v": f"{n_rec} 条", "evidence": _ev(rel, "records")})
    _ref_at, is_stale = _exec_plan_ref_state(docs, plan)
    if is_stale:
        # ⚠ 只给"旧了"这个**结论**，不给时间戳：时间戳是内部量，与读者的决策无关
        out.append({"k": "依据", "v": "旧版方案",
                    "evidence": _ev(rel, "plan_ref.built_at")})
    return out, (ST_STALE if is_stale else ST_OK)


def _exec_layout_facts(stems: List[str], docs: List[dict], cov: Dict[str, Any],
                       rel_glob: str, rel_plan: str) -> Tuple[List[dict], bool]:
    """执行层的**版式事实**：执行器实际用了哪套参数（P0-3b，2026-09-13）。

    ★ 数据来源**优先产物自己**（`result.layout.policy` / `.covered`）：
      这一层的身份是"执行",它该说的是"我做了什么"，而不是转述 plan 的判断。
      于是两层各说各的事实、互不重复：
        · 方案设计层 —— "我承认这几页的版式没有跨页证据"（读 plan）
        · 方案执行层 —— "我据此把它们降级成整页兜底了"（读产物）

    ⚠ 产物没声明时**退回 plan 的覆盖读数**并标注出处不同 —— 不假装产物声明过。
      这条退回必须留着：外部执行器（照 plan 自己写一个）完全可能不写 `layout`，
      那时界面不该显示"0 页降级"（那是假话），而该显示 plan 侧的读数。

    返回 `(facts 行列表, 是否降级)`。
    """
    n_fallback = n_declared = n_unc_decl = 0
    for d in docs:
        lay = ((d.get("doc") or {}).get("layout") or {})
        if not lay:
            continue
        n_declared += 1
        if lay.get("policy") == "page_fallback":
            n_fallback += 1
        if lay.get("covered") is False:
            n_unc_decl += 1
    if n_declared:
        out: List[dict] = []
        if n_fallback:
            out.append({"k": "降级", "v": f"{n_fallback} 页整页兜底",
                        "evidence": _ev(rel_glob, "layout.policy")})
        if n_unc_decl:
            out.append({"k": "勿直用", "v": f"{n_unc_decl} 页需人工复核",
                        "evidence": _ev(rel_glob, "layout.covered")})
        # 声明了但一条都没降级 → 这一层没话可说（**不写"0 页"**：屏幕上的零是噪声）
        return out, bool(n_fallback)
    n_unc = sum(1 for s in stems if s in (cov.get("uncovered") or set()))
    if n_unc:
        return ([{"k": "版式", "v": f"{n_unc} 页未覆盖",
                  "evidence": _ev(rel_plan, "page_model.regions[].covered")}], True)
    return [], False


def _node_facts_validate(report: Optional[dict], n_pages: int,
                         rel_glob: str) -> Tuple[List[dict], str]:
    """seam 校验节点 —— **现场跑**（只读纯计算），不是转抄 plan.qa 的旧值。"""
    if not report:
        return ([{"k": "校验", "v": "未运行",
                  "evidence": _ev(rel_glob, "seam.validate")}], ST_MISSING)
    ok = report.get("ok")
    n_err = int(report.get("n_errors") or 0)
    n_warn = int(report.get("n_warnings") or 0)
    n_ok = int(report.get("n_ok") or 0)
    # ★ 不写 `8/8 份` 这种 N/N 形态（2026-09-12 用户点名的"内部状态分数"）：
    #   同一个事实，说成一句人话即可 —— 全通过就说"全部通过"，没全通过才报出分子分母。
    if n_ok == n_pages and n_pages > 0:
        pass_v = f"{n_pages} 份全部通过"
    else:
        pass_v = f"{n_pages} 份里 {n_ok} 份通过"
    out = [
        {"k": "通过", "v": pass_v,
         # ⚠ 份数**必须跟着 n_pages 走**：此处曾写死「汇总 8 份」，产物增减后这行文字就成了假话
         "evidence": _ev(rel_glob, f"report.ok（汇总 {n_pages} 份）")},
        {"k": "硬错误", "v": f"{n_err} 条",
         "evidence": _ev(rel_glob, "report.errors")},
        {"k": "告警", "v": f"{n_warn} 条",
         "evidence": _ev(rel_glob, "report.warnings")},
    ]
    cov = report.get("coverage_median")
    if cov is not None:
        # `0.699` 是回归/评测口径的小数；上屏用百分数（同一数值换表示，**不做新计算**）
        out.append({"k": "原文覆盖", "v": f"{cov * 100:.0f}%",
                    "evidence": _ev(rel_glob, "report.metrics.coverage")})
    state = ST_OK if (n_err == 0 and ok is not False) else ST_STALE
    return out, state


# ---------------------------------------------------------------------------
# IR 构造
# ---------------------------------------------------------------------------
def build_seam_map(profile_id: Optional[str] = None, *,
                   gold_profile_id: Optional[str] = None,
                   plans_dir: Optional[Path] = None,
                   facts_dir: Optional[Path] = None,
                   results_dir: Optional[Path] = None,
                   gold_dir: Optional[Path] = None,
                   plan_path: Optional[Path] = None) -> dict:
    """构造 `seam_map` IR。**只读**，不写任何产物。

    Args:
        profile_id: 档案 id；缺省取 `data/plans/` 里 plan 最新的那个。
        gold_profile_id: **金标准按哪套算**；缺省跟随 `profile_id`。

            ★ 为什么要有它（用户 2026-09-12 追加）：一套样式按理要配一版新例题，
            但两版模板常常差别不大，用户可能想**沿用旧例题**去跑新样式。把金标准
            口径钉死在样式上，这种"样式 B + 例题 A"的组合就没有入口。故这里让两者
            **可以不同**，但**默认相同** —— 不选就是跟随，选了才解耦（零负担，不强加）。
        gold_dir: 金标准目录（默认 `manual_annotations/`）。本图只用它做一件事：
            数**指定那套金标准**有几页例题、几条标注 —— 金标准行自带 `profile` 字段，
            过滤是原始事实，不是推断。
        plan_path: **显式指定要画哪一份 plan**（§3「变体图」）。给了它就不再按
            `plans_dir/plan_<pid>.json` 找；`profile_id` 缺省时从该文件里读。

            ⚠ 它只换"**画哪份方案**"，不换口径：facts 与 results 仍按 `profile_id` 取 ——
            本图始终是**档案级**的。拿它画 `variants/` 里的变体 plan，就能把
            「同一批页、同一个执行器，换一份 split_spec 会怎样」摆在同一张图上。

    ★ **不接收任何全局统计**：本图是**档案级**的，每个数字都能追到该档案的某个文件字段。
      用户工作流四阶段的全局数字不进这张图（口径不同，混在一起就无法判定"这数字在说谁"）。
      —— 这里要防的是"**口径**混入"，不是"参数个数固定"：像 `plan_path`、`gold_dir`、
      `gold_profile_id` 这种**只换输入文件/目录、或无歧义地指定另一套档案级口径**的参数，
      是与这条纪律相容的（后者的口径仍在档案级，只是换了一个档案）。
    """
    plans_d = Path(plans_dir or DEFAULT_PLANS_DIR)
    facts_d = Path(facts_dir or DEFAULT_FACTS_DIR)
    results_d = Path(results_dir or DEFAULT_RESULTS_DIR)
    gold_d = Path(gold_dir or DEFAULT_GOLD_DIR)

    ppath: Optional[Path] = Path(plan_path) if plan_path else None
    pid = (profile_id or "").strip()
    if ppath is not None:
        # 显式给了文件 → 档案 id 可以直接从它里面读（调用方只需知道"画这一份"）
        if not pid:
            pid = str((_load_json(ppath) or {}).get("profile_id") or "").strip()
    else:
        pid = pid or latest_plan_profile(plans_d)
        ppath = plan_path_of(pid, plans_d) if pid else None
    plan = _load_json(ppath) if ppath else None
    if plan is not None and not pid:
        pid = str(plan.get("profile_id") or "").strip()
    fpath = facts_d / f"facts_{pid}.json" if pid else None
    facts_doc = _load_json(fpath) if fpath else None

    # ★ 出处一律由**实际使用的目录**推出，不写死 `data/...`：
    #   否则自定义目录（测试夹具、多档案分仓）下，图上出处会指向一个打不开的位置。
    rel_plans_dir = _rel_dir(plans_d)
    rel_facts_dir = _rel_dir(facts_d)
    rel_results_dir = _rel_dir(results_d)
    rel_gold_dir = _rel_dir(gold_d)
    rel_plan = _rel(ppath) if ppath else ""
    rel_facts = _rel(fpath) if fpath else ""

    # ★ 金标准按**档案**过滤（`manual_annotations` 每行自带 `profile`）——
    #   "我要提供几页例题"与"方案实际学到几页"从此是两个各自诚实、各自可追的数字。
    #   过滤用哪套金标准由 `gold_profile_id` 定（默认跟随 profile_id，可另行指定）。
    gpid = (gold_profile_id or "").strip() or pid
    decoupled = bool(gpid) and gpid != pid
    scope = gold_scope(gpid, gold_d) if gpid else {"n_pages": 0, "n_rows": 0, "n_foreign": 0}

    # ---- 各节点 facts ----
    n_gold = _node_facts_gold(scope, rel_gold_dir, decoupled=decoupled)
    n_facts = _node_facts_facts(facts_doc, rel_facts, rel_gold_dir,
                                n_foreign=int(scope.get("n_foreign") or 0))

    # ---- 版式覆盖读数（P0-3）：plan 缺省时全零，故这里无条件算一次 ----
    cov = layout_coverage(plan)

    if plan is None:
        # 没有 plan 文件 → 出处只能是**目录**（确实没有文件可指），不指一个不存在的路径
        # ★ 2026-09-13（用户）：原先这里另有一条浮动"提示条"说"还没编译出方案…"，
        #   提示条已整块撤除 —— 但"不静默"这条纪律不撤：把**下一步**写在**本节点自己身上**。
        #   判据：**提示该跟着它描述的那个节点走**，不该在页面别处另开一块。
        n_plan = ([{"k": "接口物", "v": "未产出",
                    "evidence": _ev(rel_plans_dir, "plan 文件存在性")},
                   {"k": "下一步", "v": "跑一次方案编译",
                    "evidence": _ev(rel_plans_dir, "plan_*.json 存在性")}],
                  ST_MISSING)
        ref_state = ST_MISSING
    else:
        ref_lines, ref_state = _refs_status(plan, rel_plans_dir=rel_plans_dir)
        pfacts = [{"k": "接口物", "v": "已产出",
                   "evidence": _ev(rel_plan, "_kind")}] + ref_lines
        # ★ P0-3（2026-09-13）：把 plan **自己声明**的版式覆盖事实搬上节点。
        #   两条各自独立，因为它们说的是两件事（与 `plan_build` 的两条 degraded 一一对应）：
        #     ① 有 ≥2 个**可归纳**的族 → 顶层结构线只编译了主族那套；
        #     ② 有页的族只有单页证据 → 那些页仍按主族参数执行，产出需人工复核。
        #   ⚠ 不解析 `plan.degraded[].reason` 的中文串（措辞一改就静默失效）——
        #     判定只读 `layout_clusters[].coverage` / `regions[].covered`。
        if cov["n_inducible"] > 1:
            pfacts.append({"k": "版式", "v": "多版式（仅主族已编译）",
                           "evidence": _ev(rel_plan, "page_model.layout_clusters[].coverage")})
        if cov["n_uncovered"]:
            pfacts.append({"k": "覆盖", "v": f"{cov['n_uncovered']} 页版式未覆盖",
                           "evidence": _ev(rel_plan, "page_model.regions[].covered")})
        # 状态：plan 自己声明了 `degraded` 就转降级。**stale 优先** ——
        # 输入已经变了是更紧的信号（"你依据的方案根本不是现在这版"），
        # 降级说的是"方案自身承认覆盖不全"，两件事叠加时先让用户去处理前者。
        if ref_state == ST_OK and (plan.get("degraded") or []):
            ref_state = ST_DEGRADED
        n_plan = (pfacts, ref_state)

    stems_e, docs_e = _exec_scan(results_d, PRODUCT_SUFFIX)
    ex_facts, ex_state = _node_facts_exec(stems_e, docs_e, plan,
                                          PRODUCT_SUFFIX, rel_results_dir)
    # ★ P0-3b（2026-09-13）：版式事实**优先读产物自己**（`result.layout`）——
    #   执行层的身份是"执行"，它该说的是"我做了什么"，不是转述 plan 的判断。
    #   产物没声明（外部执行器 / 老产物）才退回读 plan 的覆盖读数，且出处跟着换。
    _exec_glob = f"{rel_results_dir}*{PRODUCT_SUFFIX}"
    lay_facts, lay_degraded = _exec_layout_facts(stems_e, docs_e, cov,
                                                _exec_glob, rel_plan)
    ex_facts.extend(lay_facts)
    if lay_degraded and ex_state == ST_OK:
        ex_state = ST_DEGRADED
    n_exec = (ex_facts, ex_state)

    # ---- 现场校验（只读纯计算）----
    report: Dict[str, Any] = {}
    if plan is not None:
        report = _validate_results(docs_e, plan)

    n_validate = _node_facts_validate(report, len(docs_e),
                                      f"{rel_results_dir}*.json")

    fact_table = {
        "gold": n_gold,
        "facts": n_facts,
        "plan": n_plan,
        "exec": n_exec,
        "validate": n_validate,
    }

    # ---- 组装节点（点位来自布局表，高度按 facts 行数算）----
    #
    # ★ 2026-09-13（P-S9b）：分**两趟** —— 先把每节点"各自需要多高"算齐，取最大值作为
    #   **这一行的高度** `row_h`，第二趟所有节点都写成 `row_h`、顶边一律 `ROW_Y`。
    #   为什么必须两趟：行高取决于最高的那个节点（validate 通常 fact 最多），
    #   而高度要到遍历完才知道。为什么必须等高：见 `ROW_Y` 上方注释 ——
    #   "箭头要直"与"换档案位置不跳"这两条纪律，只有等高才能同时成立。
    # ★ P-S13：**可自定义区域 = 声明了「接入口」的节点**。
    #   判据只用一条**已经存在的事实**（`iface` 就是"你可以自己接手"的入口），
    #   刻意不另立一张"哪些节点可替换"的名单 —— 两张名单迟早漂移。
    #   也刻意**不写死 `exec`**：接缝将来若往下游挪、或可替换段变成两个节点，框跟着事实走。
    frame_ids: List[str] = [nid for nid in NODE_SPEC if nid in _IFACE_SUFFIX]

    row: List[dict] = []
    for nid, (kind, label) in NODE_SPEC.items():
        f, state = fact_table[nid]
        f = _fit_facts(f)          # ★ 统一压宽（单一入口）—— 防"值溢出节点框、与标签重叠"
        row.append({
            "id": nid, "kind": kind, "label": label,
            "x": COL_X[nid],
            "w": NODE_WIDE if nid == "plan" else NODE_W,
            # 该节点**自己**需要的高度（内容下限，不许溢出框外）；实际高度还要补到 row_h
            "need": max(NODE_MIN_H, PAD_TOP + LINE_H * (len(f) + 1) + PAD_BOTTOM),
            "state": state, "facts": f,
        })
    row_h = max(r["need"] for r in row)     # 一行一个高度：最"高"的那个节点说了算
    # ★ P-S13：有框时节点行整体下移 `FRAME_PAD` —— 让**框的顶边**（而不是节点顶边）
    #   落在 `GUTTER` 上。理由见 `ROW_Y` 上方注释：留白按外接框算。
    row_y = GUTTER + (FRAME_PAD if frame_ids else 0)

    nodes: List[dict] = []
    max_bottom = 0
    max_right = 0
    for r in row:
        nid, kind, label = r["id"], r["kind"], r["label"]
        f, state = r["facts"], r["state"]
        x, w = r["x"], r["w"]
        y, h = row_y, row_h    # ★ 等高 + 顶边固定（有框时已让位）⇒ 中心恒等，边自然拉直
        node = {
            "id": nid, "kind": kind, "label": label,
            "x": x, "y": y, "w": w, "h": h,
            "state": state,
            "facts": f,
            # 节点级证据汇总：去重后的 file+field 列表（每个 facts 值都能追到这里）
            "evidence": _merge_evidence(f),
        }
        # ★「方案执行层」是**唯一**带文件地址的节点（用户 2026-09-12 明确）：
        #   这张图的用处之一是让用户拿自己的 agent 接着做，那就必须给他
        #   ①接口物在哪 ②产物该写成什么文件。别的节点不给地址 —— 页面上常驻路径
        #   对非专业读者只是噪声（点节点后的详情面板另说）。
        #
        # ★ 2026-09-13（用户）：详情面板**只留这一块** —— 「数字出处」整块撤除（那串路径
        #   对普通读者没有动作可做），只保留"怎么接入、产物放哪"。故 title 用中性的
        #   「接入口」而不是「你的 agent 接入口」：能不能被替换是接缝的性质，不是节点的身份。
        if nid in _IFACE_SUFFIX:
            node["iface"] = {
                "title": "接入口",
                "paths": [
                    {"k": "接口物", "v": rel_plan or f"{rel_plans_dir}plan_*.json"},
                    {"k": "产物写到这里",
                     "v": f"{rel_results_dir}*{_IFACE_SUFFIX[nid]}"},
                ],
                "note": "执行这一层时读「接口物」，把结果写成上面那个文件名放进该目录 —— "
                        "系统自带的执行器与你自己写的 agent 用的是**同一个文件**，"
                        "回到本页即会自动纳入 seam 校验，不需要在系统里登记。",
            }
        nodes.append(node)
        max_bottom = max(max_bottom, y + h)
        max_right = max(max_right, x + w)

    # ---- 可自定义区域：虚线框（P-S13）----
    # 几何**全在这里算完**：框住 = 被覆盖节点的外接框 + 四周 `FRAME_PAD`。
    # 渲染器只落笔、不布局、不做算术（三条纪律里的第 ① 条）。
    frames: List[dict] = []
    if frame_ids:
        cover = [n for n in nodes if n["id"] in set(frame_ids)]
        fx = min(n["x"] for n in cover) - FRAME_PAD
        fy = min(n["y"] for n in cover) - FRAME_PAD
        fr = max(n["x"] + n["w"] for n in cover) + FRAME_PAD
        fb = max(n["y"] + n["h"] for n in cover) + FRAME_PAD
        frames.append({
            "id": FRAME_ID,
            "kind": "custom",          # 语义类别：这一块可以被换掉
            "label": FRAME_LABEL,
            "node_ids": [n["id"] for n in cover],
            "x": fx, "y": fy, "w": fr - fx, "h": fb - fy,
            # 标注**居中于框下**：左对齐框边只会与节点正文"差 2px 地几乎对齐"，
            # 那种 almost 比刻意偏移更难看；居中则明确读作这句框的题注。
            "label_x": (fx + fr) // 2,
            "label_y": fb + FRAME_LABEL_GAP,
        })
        max_right = max(max_right, fr)

    edges = [{"from": a, "to": b, "kind": k, "route": r, "label": lb}
             for (a, b, k, r, lb) in EDGE_SPEC]

    # ---- 体检表 / 提示条：2026-09-13（用户）**整块撤除** ----
    # 依据不是"数字是死的"（实测它们**确实是实时重算的** —— 换 profile 会变，见
    # 而是**信息冗余**：
    #   ① 三条体检项与**节点状态点说的是同一件事** ——
    #      `plan_present` ⇄ plan 节点 ok/missing（`test_empty_profile_does_not_crash` 已锁）；
    #      `refs_consistent` ⇄ plan 节点 stale（`test_refs_changed_is_visible_as_stale` 已锁）；
    #      `results_present` ⇄ exec 节点 ok/missing（`test_stale_products_are_visible` 已锁）。
    #      即三个绿方块对读者**零新增信息**。
    #   ② 提示条里的「另有 N 页未归所选金标准」**已经写在「规则提取 · 层 1」节点上**
    #      （同源同值，见 `_node_facts_facts`），余下的只是"会稀释规则/怎么消除"的解释文字。
    #   ③ 用户原话："就算可以实时变化，一样没有必要担负引起bug的风险加入进去。"
    # 保留的"不静默"：见上面 `n_plan` 的 `下一步` —— **提示跟着它描述的节点走**。

    # ★ P-S13（用户）：**左下角那条虚线图例删除**。
    #   它当初的职责是给"虚线边"下一个定义。现在虚线画在**框**上、框下又有就地标注，
    #   "图上唯一的虚线是什么"一眼可读 —— 再留一条图例，等于用一行小字解释一个
    #   已经不言自明的形状。撤内容时**连着为它算出来的空间一起撤**（§62 那一课的直接应用：
    #   只撤内容不撤空间，等于在图上留一个看不见的坑）。
    #
    # ---- 墨迹外接框：画布高度由**全部**墨迹派生（节点 + 框 + 框下标注）----
    ink_bottom = max_bottom
    for _fr in frames:
        ink_bottom = max(ink_bottom, _fr["y"] + _fr["h"],
                         _fr["label_y"] + FRAME_LABEL_DESCENT)

    return {
        "_schema_version": SCHEMA_VERSION,
        "_kind": KIND,
        "profile_id": pid,
        "gold_profile_id": gpid,
        "built_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        # ★ 画布 = **墨迹外接框** + 四周 `GUTTER`（上下同值 —— 竖直居中的唯一保证）。
        #   什么叫墨迹：节点、虚线框、框下标注 —— **全部**画出来的东西。
        #   旧公式 `max_bottom + 图例带 + GUTTER` 只认节点行，加一圈框就会把上边压薄
        #   （§62 的形态：留白跟着"某一类内容"算，而不是跟着"画出来的东西"算）。
        "canvas": {"w": max(CANVAS_W, max_right + 20),
                   "h": ink_bottom + GUTTER},
        "nodes": nodes,
        "edges": edges,
        "frames": frames,
        "counts": {
            "n_pages": len(stems_e),
        },
    }


def _validate_results(docs: List[dict], plan: dict) -> Dict[str, Any]:
    """对 results 里的每份产物跑 `seam.validate`（只读），汇总。**

    失败不抛 —— 图上一个节点画不出来，不该把整页拖垮（承"分诊是增值不是关卡"同族纪律）。
    """
    try:
        import seam  # noqa: WPS433 (延迟导入：只在真要校验时付这个成本)
    except ImportError as e:
        log.warning(f"[seam_map] seam 不可用: {e}")
        return {}
    n_ok = 0
    total = 0
    errors: List[dict] = []
    n_warnings = 0
    covs: List[float] = []
    for d in docs:
        doc = d.get("doc")
        if not doc:
            continue
        total += 1
        try:
            rep = seam.validate(doc, plan=plan)
        except Exception as e:  # noqa: BLE001
            errors.append({"where": d["path"].name, "code": "exception", "msg": str(e)[:160]})
            continue
        if rep.get("ok"):
            n_ok += 1
        errors.extend(rep.get("errors") or [])
        n_warnings += len(rep.get("warnings") or [])
        cov = (rep.get("metrics") or {}).get("coverage")
        if isinstance(cov, (int, float)):
            covs.append(float(cov))
    cov_med = None
    if covs:
        s = sorted(covs)
        cov_med = s[len(s) // 2]
    return {"ok": (total > 0 and n_ok == total), "n_ok": n_ok, "n_total": total,
            "n_errors": len(errors), "n_warnings": n_warnings,
            "errors": errors[:10], "coverage_median": cov_med}



# ---------------------------------------------------------------------------
# 渲染：IR → 内联 SVG（纯函数、确定性、零业务算术）
# ---------------------------------------------------------------------------
STATE_FILL = {
    ST_OK: "#FFFFFF",
    ST_STALE: "#FFFFFF",
    ST_MISSING: "#F4F3EF",
    ST_DEGRADED: "#FFFFFF",
}
STATE_STROKE = {
    ST_OK: "#1F1E1B",
    ST_STALE: "#B85C4A",
    ST_MISSING: "#E2E0DA",
    ST_DEGRADED: "#98958C",
}
STATE_DOT = {
    ST_OK: "#7D9B6A",
    ST_STALE: "#B85C4A",
    ST_MISSING: "#E2E0DA",
    ST_DEGRADED: "#98958C",
}
INK = "#1F1E1B"
MUTED = "#6F6C64"
SUBTLE = "#98958C"
BORDER = "#E2E0DA"
SURFACE = "#FFFFFF"
BG = "#F4F3EF"
ACCENT = "#B85C4A"
# ★ P-S13：可自定义区域的**框与框下标注**用同一个强调色 —— 它本来就是这个语义的颜色
#   （旧图上"可拆卸接缝"那条虚线边与图例用的也是它）。换色只改这一行。
FRAME_STROKE = ACCENT

FONT_SERIF = "'Noto Serif SC','Source Han Serif SC','Songti SC','SimSun',serif"
FONT_SANS = "'Inter','Noto Sans SC','Microsoft YaHei',-apple-system,sans-serif"

# ★ 2026-09-13（用户）：「这个图表中的文字，字体大小应该是一致的，不能出现字体大小
#   大小不一的情况」。原先节点标题 11.5、facts 与图例 10.5 两档混用 → 全图**只留一个字号**，
#   且它是**单一来源**（渲染器里不得再出现 `font-size` 字面量，有单测锁死）。
#   容量核算：节点内值区 = `NODE_W - 2*PAD_X` = 158px；`_fit` 的 24 个显示宽度单位
#   在 11.5px 下 ≈ 24 × (11.5/2) = 138px < 158px → 统一放大到 11.5 **不会溢出**（余 20px）。
FS = 11.5


def _esc(s: Any) -> str:
    """XML 转义。渲染器唯一对文本做的加工（不是业务加工）。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _cy(n: dict) -> int:
    """节点竖直中心（纯几何）。"""
    return n["y"] + n["h"] // 2


def _rx(n: dict) -> int:
    """节点右边缘（纯几何）。"""
    return n["x"] + n["w"]


def _lx(n: dict) -> int:
    """节点左边缘（纯几何）。"""
    return n["x"]


def _edge_path(a: dict, b: dict, route: str) -> str:
    """正交折线（横向）。**几何派生，不涉及业务值。**

    `h_direct`：出 a 右边缘 → 直入 b 左边缘（两者中线同高时即为一条水平直线）。
    `h_elbow`：出 a 右边缘 → 水平到 lane（两节点之间的中点）→ 垂直 → 入 b 左边缘。

    ★ 2026-09-13（P-S9b）：本图的布局**已把所有节点对齐到同一条竖直中轴**
    （同一行节点等高 + 顶边固定，见 `ROW_Y` 上方注释），故
    `_cy(a) == _cy(b)` 恒成立，即使声明 `h_elbow` 也会落到直线分支 —— 也就是说
    `h_elbow` 在本图**不再被任何边使用**（`EDGE_SPEC` 四条边全是 `h_direct`）。
    保留这个分支是因为它是渲染器的**通用能力**（节点高度不齐的版式仍需肘形）；
    但调用方若声明 `h_elbow` 却拿到直线，那是**版式已变**的信号，不是渲染器偷懒。
    """
    ax, ay = _rx(a), _cy(a)
    bx, by = _lx(b), _cy(b)
    if route == RT_H_ELBOW and ay != by:
        lane = (ax + bx) // 2
        return f"M {ax} {ay} H {lane} V {by} H {bx}"
    return f"M {ax} {ay} H {bx}"


def render_svg(ir: dict) -> str:
    """把 IR 渲染成自包含的内联 SVG 字符串。

    **确定性**：不读文件、不取时间、不用随机、不依赖 dict 迭代顺序（IR 全是 list）
    → 同一份 IR 渲染多次，输出逐字节相同（`tests/test_seam_map.py` 锁死）。
    **零外部请求**：无 `<image href>`、无外链字体、无 `@import`、无 `<style>`；
    唯一出现的 `http://` 是 SVG 命名空间声明（浏览器的固定常量，不是网络请求）。
    **固定像素尺寸**：不写 `width="100%"` —— 否则窄屏上 {FS}px 的字会被缩到看不清。
    """
    W = ir["canvas"]["w"]
    H = ir["canvas"]["h"]
    by_id = {n["id"]: n for n in ir["nodes"]}

    out: List[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" '
        f'aria-label="半自动标注方案结构图：金标准页（例题） → 规则提取 · 层 1'
        f' → 方案设计层 · 层 2 → 方案执行层 → seam 校验" '
        f'font-family="{FONT_SANS}">'
    )
    out.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="{BG}"/>')

    # ---- 可自定义区域：虚线框 + 框下标注（先画，让进入框的箭头压在框线上）----
    # ★ P-S13：虚线**只在框上**出现。框先于边绘制是刻意的 —— 那条进入框的箭头是"交接"
    #   这个动作，看着连续才像一次交接；框线在它下面断一小段，读者也不会把那段误读成
    #   "框里还有一个节点"。几何（含 `label_x` / `label_y`）全部来自 IR，渲染器不布局。
    for _fr in ir.get("frames") or []:
        out.append(f'<rect x="{_fr["x"]}" y="{_fr["y"]}" width="{_fr["w"]}" '
                   f'height="{_fr["h"]}" fill="none" stroke="{FRAME_STROKE}" '
                   f'stroke-width="1" stroke-dasharray="{FRAME_DASH}"/>')
        out.append(f'<text x="{_fr["label_x"]}" y="{_fr["label_y"]}" text-anchor="middle" '
                   f'font-size="{FS}" fill="{FRAME_STROKE}">{_esc(_fr["label"])}</text>')

    # ---- 边（压在框上、压在节点下面）----
    # ★ P-S13（用户）：「方案执行层前面的箭头就不再需要用虚线了，和其他地方一样用实线就行」
    #   ⇒ 四条边**一律实线**：渲染器里不再有"按 kind 选线型"的分支 —— 虚线是**区域**的语言。
    out.append(f'<g class="edges" fill="none" stroke="{INK}" stroke-width="1">')
    for e in ir["edges"]:
        a = by_id.get(e["from"])
        b = by_id.get(e["to"])
        if a is None or b is None:
            continue
        path = _edge_path(a, b, e.get("route") or RT_H_DIRECT)
        out.append(f'<path d="{path}"/>')
        # 箭头（几何）：在入点画一个 5px 的实心三角，表明方向
        bx, by = _lx(b), _cy(b)
        out.append(f'<path d="M {bx - 7} {by - 4} L {bx} {by} L {bx - 7} {by + 4} Z" '
                   f'fill="{INK}" stroke="none"/>')
    out.append('</g>')

    # ---- 节点 ----
    for n in ir["nodes"]:
        st = n.get("state") or ST_OK
        fill = STATE_FILL.get(st, SURFACE)
        stroke = STATE_STROKE.get(st, INK)
        dash = ' stroke-dasharray="3 3"' if st == ST_MISSING else ''
        x, y, w, h = n["x"], n["y"], n["w"], n["h"]
        # 显式带上节点 id：界面上「点哪个节点」不该依赖"文档顺序恰好等于 IR 顺序"这种隐式耦合
        # （将来只要有人给节点排序/分组绘制，顺序绑定就会**静默错位**，且看不出来）
        out.append(f'<g class="node" data-node-id="{_esc(n.get("id"))}">')
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" '
                   f'stroke="{stroke}" stroke-width="1"{dash}/>')
        # 方形状态点（包豪斯：几何状态点，不用圆）
        dot = STATE_DOT.get(st, INK)
        out.append(f'<rect x="{x + PAD_X}" y="{y + PAD_TOP + 1}" width="7" height="7" '
                   f'fill="{dot}"/>')
        out.append(f'<text x="{x + PAD_X + 12}" y="{y + PAD_TOP + 8}" font-size="{FS}" '
                   f'font-weight="600" fill="{INK}">{_esc(n.get("label"))}</text>')
        for i, f in enumerate(n.get("facts") or []):
            ty = y + PAD_TOP + LINE_H * (i + 1) + 8
            out.append(f'<text x="{x + PAD_X}" y="{ty}" font-size="{FS}" fill="{SUBTLE}">'
                       f'{_esc(f.get("k"))}</text>')
            out.append(f'<text x="{x + w - PAD_X}" y="{ty}" text-anchor="end" '
                       f'font-size="{FS}" fill="{MUTED}" font-family="{FONT_SERIF}">'
                       f'{_esc(f.get("v"))}</text>')
        out.append('</g>')

    out.append('</svg>')
    return "".join(out)


# ---------------------------------------------------------------------------
# CLI（便于人工核对与截图）
# ---------------------------------------------------------------------------
def map_exit_code(rc: int, ir, *, as_json: bool) -> int:
    """唯一判定点：底层 rc==0 **且**（要 IR 时 IR 可解析）⇒ 0，否则 1。

    `chronicles map` 取用本函数，包装层不重判。json 模式必须拿到 IR
    （那是信封的正文），非 json 模式 IR 为 None 不算失败。
    """
    return 0 if (rc == 0 and (ir is not None or not as_json)) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="seam_map：半自动标注方案结构图（IR + SVG）")
    ap.add_argument("--profile", default="", help="档案 id（缺省取最新 plan）")
    ap.add_argument("--svg", default="", help="把 SVG 写到该路径")
    ap.add_argument("--json", default="", help="把 IR 写到该路径")
    ap.add_argument("--plan", default="",
                    help="画**指定的 plan 文件**（如 `data/plans/variants/*.json` 变体）；"
                         "缺省画该档案的正式方案")
    ap.add_argument("--gold", default="", help="金标准目录（缺省 manual_annotations/）")
    ap.add_argument("--gold-profile", default="",
                    help="金标准按哪套算（缺省跟随 --profile）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    ir = build_seam_map(args.profile or None,
                        gold_profile_id=args.gold_profile or None,
                        gold_dir=Path(args.gold) if args.gold else None,
                        plan_path=Path(args.plan) if args.plan else None)
    print(f"profile = {ir['profile_id']}  gold = {ir['gold_profile_id']}  "
          f"canvas = {ir['canvas']}")
    for n in ir["nodes"]:
        print(f"  [{n['state']:<8}] {n['label']:<20} " +
              " | ".join(f"{f['k']}={f['v']}" for f in n["facts"]))
    # 落盘一律走项目唯一的原子写入口（不另造第二条写路径）。
    # 本模块**只读**的定位说的是"不写业务产物"；CLI 出图是人工核对用的导出。
    if args.json:
        import data_io
        data_io.atomic_write_text(Path(args.json), json.dumps(ir, ensure_ascii=False, indent=2))
        print(f"IR  -> {args.json}")
    if args.svg:
        import data_io
        data_io.atomic_write_text(Path(args.svg), render_svg(ir))
        print(f"SVG -> {args.svg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
