# -*- coding: utf-8 -*-
"""P4 草稿裁决数据层（半自动标注通用方案 §5 P4，2026-09-10）。

消费 P3 产出的草稿层 `data/preannotations/<stem>.ai.jsonl`（**只读**），
把人工的「通过 / 修正 / 忽略」裁决落成三重物理分层：

    ai          草稿层    data/preannotations/<stem>.ai.jsonl      ← 本模块永不写入
    ai_verified 确认层    manual_annotations/<image_name>.jsonl    ← accept 写入
    manual      人工层    同一文件（由 annotate 页从零标注写入）

**裁决状态外挂**到 `data/adjudication/status.json`
（key = `<stem>::uid:<指纹>|<attr>@r<记录号>`；无指纹的旧数据仍认 `<stem>::<idx>`），
与 `review_store` 的 `data/review/status.json` 同一模式：原始文件只读、状态外挂、
原子写。选这个位置而非草稿同目录，是为守住红线二的字面要求
——「`data/preannotations/` 只放 ai 层」，草稿目录里不进任何裁决痕迹。

**键的稳定性（M3 修订，2026-09-16）**：两个问题分两步解决。
① `idx` 是草稿 jsonl 的**行号**（阅读序位次），阅读序一变（批 2 把旧 `//50`
  分箱换成无量纲列聚类）旧 `idx` 键就会指向别人 ⇒ 引入几何指纹 `uid`。
② 但本语料是**竖排古籍**，一「行」常是整列一段、一列锚定**多个**属性 ⇒
  `uid`（= 条目**首行**指纹）被同列兄弟条目**共用**：36 页 727 条实测
  **554 条（76.2%）**与他条共键。后果不止显示错 —— `reset` 会删掉兄弟条目
  在受保护目录里的行。⇒ 键再收成**三元组** `(uid, attr, record_index)`：
  实测 727 键 / **0 碰撞**。据此键**不再与阅读序无关**（原设计正为此才引入 uid），
  但那本就是假目标：阅读序一变草稿整体就变了，任何键都指不准；而串页是天天发生的。
  详见 `entry_identity` / `_key` / `_status_get`。

红线落实
--------
- **红线二（确认即固化）**：`ai → ai_verified` 只能由人工动作触发，**绝不自动回流**。
  未经确认的草稿不得进入 `manual_annotations/`。accept 时把草稿溯源写进
  `ai_ref{stem, idx, seg, uid, attr, record, confidence, edited}`——
  `annotation_groups.aggregate()` 的键白名单会丢弃它，故对下游零污染，
  但 reset 可凭它精确定位并物理移除（`uid`+`attr`+`record` 三者全中才算它，
  见 `_matches_ai_ref`：只凭 uid 会误删同列兄弟的行）。
- **绝不丢字**：多段草稿（跨列续框）按各段框高比例切分文本（最大余数法，总和守恒），
  以 `gid` 组多行落库，与 `aggregate()` 的多段语义一一对应；拼接结果恒等于原值。
- **可见不可藏的折叠（M5，2026-09-11）**：多段条目在裁决面板默认折叠为**一条**，
  展开可见**每一段**（段文本 / 段框 / 段标识 / 该段 OCR 行原文）。切分走
  `layout_contract.split_by_boxes`（唯一实现，与落库同源）→ **面板预览 == 采纳后写入**。
  注意：**折叠只改变展示粒度，绝不隐藏内容** —— 值始终在 `text` 里、各段皆可展开。
  （对照 `X-AnyLabeling._link_grouped_text_blocks` 的 `hidden_in_panel`：其前提是
  「同组恰一个非空」，本语料金标准 17 个多行组**全部不满足**（组内每行都有文本），
  照搬即隐藏真实内容，故只借用"代表段"的**交互形态**，不借用其"隐藏"语义。）
- **向后兼容**：草稿文件缺失 → 返回空列表；manual 目录不存在 → 按需创建；
  `ai_ref` 缺失的旧行不受任何影响（本模块只碰自己写的行）。

记录顺序纪律
------------
采纳的行**按阅读序插入**（右上→左下；与 `preannotate` 的分箱口径一致），而非简单
追加到文件尾——`layout_contract._split_by_anchor` 依赖「行序 = 阅读流」来切记录，
尾部追加会把采纳内容全部挤到最后一"记录"里。已有行的相对顺序不被改动。

批量采纳安全门（2026-09-10 增补 → **2026-09-17 换判据**，用户批准）
------------------------------------------------------------------
把「按置信层批量采纳」与「按 idx 批量采纳」**分开对待**：

| 触发方式 | 实质 | 准入 |
|---|---|---|
| `confidences=["high"]` | 把**机器的分层**当作人的判断 | 该页有几何层、有原图，且该**档案**已有独立回验读数**且达标** |
| `indices=[...]` | 人已逐条指定 | 放行（人对点过的条目负责） |

**为什么换掉「几何来源标签」判据**：

1. `L1_blocks.source == "pipeline"` 只是"几何可信"的**必要条件**，不是充分条件。
   同为 `pipeline`：`金标准页` 值对齐命中率 **0.800**，样例页 0001 只有 **0.118**
   —— 差 6.8 倍。原两分法只有前一半成立：26 页 `l2_reconstructed` **全部拦对（0 漏）**，
   但"放行即可信"这一半不成立。
2. **「没测过」不等于「偏离小」**。按来源标签放行，等价于**未验证即放行**——
   比逐条裁决更危险（后者至少人看过）。故改为 **fail-closed**：
   **没有回验记录 = 不放行**，且必须把"为什么"与"怎么解锁"一并告知。
3. 准入粒度 = **档案（`profile`）**，不是页、也不是期次。实测：**期次是行政划分**
   （5 期 drift 的成因只是"半页扫描 925×1798"vs"整页 1884×1795"，**规则无差异**）；
   而档案在系统里本就是"一套规则"的实体（`profile_store`：属性集 + 样本页），
   且契约 `layout_prof_*.json` 本就档案级、本就由 **3 页金标准**学出。
   ⇒「标 3 页解锁整个档案」**不是新增门槛，是契约构建的既有要求**，成本仍 O(1)。

红线二的实质是"固化必须由人的判断触发"，故拦截放在**数据层**
（`batch_adjudicate` 直接拒绝），前端禁用只是提示；`page_state` 同时下发
`batch_safe` 与 `batch_block_reason`，让界面能**说明原因**而不是只灰掉按钮。

⚠ `geometry_source` 仍随判定一并返回，但**已降级为参考信息、不再是判据**。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import layout_contract as LC
import data_io

log = logging.getLogger("adjudicate")

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录
else:
    _BASE = Path(__file__).parent.resolve()
BASE_DIR = _BASE

DEFAULT_DRAFTS_DIR = BASE_DIR / "data" / "preannotations"
DEFAULT_STATUS_DIR = BASE_DIR / "data" / "adjudication"
DEFAULT_STATUS_PATH = DEFAULT_STATUS_DIR / "status.json"
DEFAULT_MANUAL_DIR = BASE_DIR / "manual_annotations"
DEFAULT_ARCHIVE_DIR = BASE_DIR / "_archive"
DEFAULT_STRUCTURED_DIR = BASE_DIR / "data" / "structured"
#: 档案级「独立回验读数」目录（`batch_gate` 的唯一放行依据）。
#: 一档案一文件：`data/validation/<profile_id>.json`。见 `write_validation`。
DEFAULT_VALIDATION_DIR = BASE_DIR / "data" / "validation"

VALID_ACTIONS = ("accept", "reject", "reset")
VALID_CONFIDENCES = ("high", "medium", "low")
SOURCE_AI_VERIFIED = "ai_verified"
GID_PREFIX = "aiv"          # 合法 gid（字母数字，见 annotation_groups.GID_RE）
MAX_ATTR_NAME = 60          # 与 profile_store.MAX_ATTR_NAME 同量级
# 阅读序不再用固定像素分箱（旧 `RO_BIN_W = 50`）：改用 `LC.same_column` 的
# 无量纲三守卫（重叠比/宽度比/跨度比），与 P3 生成侧列聚类同源——见 §35 c)。

#: ⚠ **2026-09-17 起不再作准入判据**，只作参考信息随判定下发。
#: 历史（2026-09-10）：`l2_reconstructed`（存量回填）只是把旧合成框换个壳，
#: 高置信层在其上实测精确率仅 0.152。**但实测证明 `pipeline` 只是必要条件**
#: （同为 pipeline，值对齐命中率 0.800 vs 0.118）⇒ 判据换成档案级回验读数，
#: 见模块 docstring「批量采纳安全门」。
TRUSTED_GEOMETRY_SOURCES = ("pipeline",)
UNTRUSTED_SOURCE_NOTE = "l2_reconstructed"

#: 回验记录 schema 版本（`data/validation/<pid>.json`）
VALIDATION_SCHEMA = 1
#: 允许的度量口径名。**只登记名字，不在此实现度量** —— 实现见
#: `align_eval.score()`（分母一律取**金标准侧**的三个口径）。
#: ★★ 同一个口径名在两个模块里出现 ⇒ 必须**互相指认**，否则判据会漂成两份
#:   （本项目反复防的"第二份真相"）。机械保证：`tests/test_align_eval.py` 断言
#:   `align_eval.METRIC_END_TO_END in VALIDATION_METRICS`。
#: ★ `gold_iou50` 是**历史名、无实现**（早期报告里的口径）——登记它不会被拒
#:   （这里只管名字合法不合法），但没有任何工具能产出它 ⇒ 命令面的 help 已标明。
VALIDATION_METRICS = ("value_aligned_iou50", "gold_iou50", "manual")



# ============================================
# 路径与命名（与 app.py / layout_contract 同口径，避免平行实现）
# ============================================
def safe_name(name: str) -> str:
    """文件名净化——**单一实现**在 `data_io.safe_name`（方案 §10.4 批 1，M9）。

    保留本函数作为转发口（既有调用方与测试零改动）。
    """
    return data_io.safe_name(name)


def stem_of(image_name: str) -> str:
    """image_name → 草稿用 stem（单一来源：`layout_contract.stem_of`）。"""
    return LC.stem_of(str(image_name or ""))


def entry_identity(entry: dict) -> tuple:
    """草稿条目 → **裁决身份三元组** `(uid, attr, record_index)`（M3 修订，2026-09-16）。

    ## 为什么必须三元组（实测数据推翻了原设计假设）

    M3 原设计用 `row_uid`（= 条目**首行**的几何指纹）当条目的稳定标识。
    但本语料是**竖排古籍**：一「行」常是**整列一段**，同一列会锚定**多个**属性
    ⇒ 多条条目拿到**同一个 uid**。在 36 页 727 条真实草稿上实测：

    | 键的构成 | 键数 | 残留碰撞组 | 组内条目 |
    |---|---|---|---|
    | `uid` | 344 | 171 | **554（76.2%）** |
    | `uid + attr` | 709 | 18 | 36（5.0%） |
    | `uid + attr + record_index` | **727** | **0** | **0** |

    ⇒ 只用 uid 时，一次裁决会被记到**同列的兄弟条目**头上。后果不是显示问题：
    `batch_adjudicate` 会把它们当成"已裁决"跳过（**静默永不裁决**），
    而 `reset` 按 uid 定位会**删掉兄弟条目在 `manual_annotations/` 里的行**
    （受保护目录的数据丢失）。

    ⇒ 三元组是**能确定归属的最小组合**。三项分别为：条目从哪一列起（uid）、
      是什么属性（attr）、属于哪一条记录（record_index）。
    """
    ev = entry.get("evidence") or {}
    return (str(entry.get("row_uid") or "").strip(),
            str(entry.get("attr") or "").strip(),
            int(ev.get("record_index") or 0))


def _key(stem: str, idx: int, uid: Optional[str] = None,
         attr: Optional[str] = None, record=None) -> str:
    """裁决状态键。

    **M3 修订（2026-09-16）**：`uid` 单独**不足以**标识条目（实测 76.2% 的条目
    与兄弟共用 uid，见 `entry_identity`）⇒ 键改为 `stem::uid:<uid>|<attr>@r<记录号>`。

    与原设计的取舍：`record_index` 随切分变化，故键**不再完全与阅读序无关**
    （原设计正是为了这个才引入 uid）。但那是个假目标 —— 阅读序一变，草稿内容
    整体就变了，任何键都不可能还指得准；而**键串页**（76% 的条目被兄弟的裁决
    带上）是天天发生的真实故障。两害相权取其轻。

    分层：
      - `uid` 非空 且 给了 attr/record → 三元组键（新，唯一）；
      - `uid` 非空 而无 attr/record → **旧格式** `stem::uid:<uid>`（仅供迁移读取）；
      - `uid` 为空 → `stem::<idx>`（迁移前的老草稿，行为逐字不变）。
    """
    u = str(uid or "").strip()
    if not u:
        return f"{stem}::{int(idx)}"
    if attr is None and record is None:
        return f"{stem}::uid:{u}"
    return f"{stem}::uid:{u}|{str(attr or '').strip()}@r{int(record or 0)}"


def _status_get(status: Dict[str, Dict[str, Any]], stem: str,
                idx: int, uid: Optional[str] = None,
                attr: Optional[str] = None, record=None
                ) -> Optional[Dict[str, Any]]:
    """读状态条目：**三元组键优先 → 旧 idx 键兜底**。

    ★ **刻意不再读「仅 uid」的旧格式键**（当调用方给了 attr/record 时）：
      那正是串页的来源 —— 回退读它等于把 76% 的碰撞重新引回来。
      存量影响为零：`data/adjudication/status.json` 实测 **0 个键**
      （2026-09-16 取证），`manual_annotations/` 里带 `ai_ref.uid` 的行也是 **0**。
      此类键只可能在"有旧版裁决记录"时出现，而那种情况下降级为
      pending（**宁可重问，不可错认**）——裁决是可重复的动作，误认不是。
    """
    if uid:
        ent = status.get(_key(stem, idx, uid, attr, record))
        if ent is not None:
            return ent
    return status.get(_key(stem, idx))


# ============================================
# 草稿层（只读）
# ============================================
def draft_path(stem: str, drafts_dir=None) -> Path:
    return Path(drafts_dir or DEFAULT_DRAFTS_DIR) / f"{safe_name(stem)}.ai.jsonl"


def load_drafts(stem: str, drafts_dir=None) -> List[dict]:
    """读草稿层，逐行附 `idx`（= **原始行号**，坏行也占位，保证标识稳定）。

    草稿层只读；文件缺失/损坏 → 返回空列表（向后兼容，调用方零分支）。
    """
    p = draft_path(stem, drafts_dir)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        log.warning("[adjudicate] 草稿读取失败 %s: %s", p.name, e)
        return []
    out: List[dict] = []
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        e = dict(e)
        e["idx"] = i
        out.append(e)
    return out


def list_draft_stems(drafts_dir=None) -> List[str]:
    """所有含草稿的 stem（按名排序）——供概览/页清单使用。"""
    d = Path(drafts_dir or DEFAULT_DRAFTS_DIR)
    if not d.is_dir():
        return []
    return sorted(p.name[:-len(".ai.jsonl")] for p in d.glob("*.ai.jsonl"))


# ============================================
# 裁决状态（外挂 JSON，原子写——同 review_store）
# ============================================
def load_status(path=None) -> Dict[str, Dict[str, Any]]:
    try:
        d = json.loads(Path(path or DEFAULT_STATUS_PATH).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_status(status: Dict[str, Dict[str, Any]], path=None) -> None:
    """原子写（**单一实现** `data_io`：tmp + fsync + os.replace）。"""
    data_io.atomic_write_json(Path(path or DEFAULT_STATUS_PATH), status, indent=1)


# ============================================
# 人工层读写
# ============================================
def manual_path(image_name: str, manual_dir=None) -> Path:
    return Path(manual_dir or DEFAULT_MANUAL_DIR) / f"{safe_name(image_name)}.jsonl"


def read_manual(image_name: str, manual_dir=None) -> List[dict]:
    p = manual_path(image_name, manual_dir)
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
        log.warning("[adjudicate] 人工标注读取失败 %s: %s", p.name, e)
    return out


def _matches_ai_ref(item: dict, stem: str, idx: int,
                    uid: Optional[str] = None, attr: Optional[str] = None,
                    record=None) -> bool:
    """行是否来自本页第 idx 条草稿（撤销/更新的定位依据）。

    **M3（方案 §10.3）**：`ai_ref` 记 `uid`（几何指纹，跨重跑稳定）后，
    优先按 `uid` 命中——这样**列分箱/阅读序变化导致 idx 漂移时，
    已采纳的行仍能被正确定位**（旧行为会挂到别人身上）。
    `uid` 缺任一侧 → 退回 (stem, idx) 旧口径（既有数据零迁移）。

    ★★ **M3 修订（2026-09-16）**：`uid` 命中**不足以**认领一行 —— 竖排古籍
      一列会锚定多个属性，同列兄弟条目共用 uid（实测 76.2% 的条目如此）。
      只按 uid 认领 ⇒ `reset` 会**删掉兄弟条目已采纳的行**（受保护目录丢数据）。
      ⇒ 行里带了 `attr`/`record` 的（本次修订之后写的），必须**三者全中**才算它；
        三者中缺一，或行是修订前写的（无这两个键）→ 才退回"uid 命中即算"的旧口径。
    """
    ref = item.get("ai_ref")
    if not isinstance(ref, dict):
        return False
    if str(ref.get("stem", "")) != stem:
        return False
    ru = str(ref.get("uid") or "")
    if uid and ru and ru == str(uid):
        if "attr" in ref or "record" in ref:
            if attr is None and record is None:
                return True                    # 调用方没给 → 旧口径
            same_a = str(ref.get("attr") or "").strip() == str(attr or "").strip()
            try:
                same_r = int(ref.get("record") or 0) == int(record or 0)
            except (TypeError, ValueError):
                same_r = False
            return same_a and same_r
        return True                            # 修订前写的行 → 旧口径
    try:
        return int(ref.get("idx", -1)) == int(idx)
    except (TypeError, ValueError):
        return False


def _insert_by_reading_order(items: List[dict], rows: List[dict]) -> List[dict]:
    """把 rows 按阅读序（**RTL 竖版：列右→左，列内上→下**）插入 items，
    **不改动 items 相对顺序**。

    多段行（同一 gid 组）作为一个块整体插入，保证组内行连续
    —— `aggregate()` 按行序拼接组内文本。

    **参照系是 `items` 自身**（不再是硬编码 `RO_BIN_W=50` 的像素分箱）：
    同列与否用**无量纲**的三守卫判据（`LC.same_column`，重叠比/宽度比/跨度比），
    与 P3 生成侧的列聚类**同一份口径**。理由同 §35 c)：列距随文献而异，
    固定分箱在列密/列疏两端都会失效。
    """
    if not rows:
        return items
    nb = rows[0].get("box")
    nr = LC.rect_of_box(nb)
    if nr is None:
        return items + list(rows)                     # 新行无几何 → 追加（不猜位置）
    ncx = (nr[0] + nr[2]) / 2.0
    pos = len(items)
    for i, it in enumerate(items):
        r = LC.rect_of_box(it.get("box"))
        if r is None:
            continue
        if LC.same_column(r, nr):                     # 同列 → 比较上下
            if r[1] > nr[1]:
                pos = i
                break
            continue
        if (r[0] + r[2]) / 2.0 < ncx:                 # 该列在新行左边 → 新行在其前
            pos = i
            break
    return items[:pos] + list(rows) + items[pos:]


def _write_manual(image_name: str, items: List[dict], manual_dir=None,
                  archive_dir=None) -> Path:
    """原子写回人工层（**先备份**）。文件原有内容一律先落 `_archive/`。"""
    p = manual_path(image_name, manual_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            ad = Path(archive_dir or DEFAULT_ARCHIVE_DIR) / "manual_edits"
            ad.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(p, ad / f"{p.name}.{ts}.bak")
        except OSError as e:
            log.warning("[adjudicate] 备份失败（继续写入）%s: %s", p.name, e)
    data_io.atomic_write_jsonl(p, items)
    return p


# ✅ 2026-09-14（技能包）：公开为 `write_manual` —— 人工标注命令面
# （`manual_annotate.remove_annotations`）要复用这份「原子写 + 先落 _archive」，
# 按 §60 单一实现纪律提公开而非复制。旧名保留，既有调用点零变化。
write_manual = _write_manual


# ============================================
# 文本按框高切分（多段落库；拼接恒等于原值）
# ============================================
def _split_by_weights(text: str, weights: List[float]) -> List[str]:
    """最大余数法按权重切分文本。**总和守恒**（拼回 == 原文，绝不丢字）。

    **单一实现已收敛**（M5）：算法本体在 `layout_contract.split_text`。该规则原与
    `preannotate._partition` 同口径但两份实现；M5 让裁决面板也要显示**逐段切分**
    （"展开可见各段"），若面板自算一遍就会与落库分叉 —— 故统一到 LC，此处转发。
    """
    return LC.split_text(text, weights)


# ============================================
# 草稿 → 人工层行（红线二：确认即固化）
# ============================================
def segments_of(entry: dict) -> List[dict]:
    """条目的**分段视图**（M5）：裁决面板「折叠 + 展开可见各段」的数据契约。

    - 草稿自带 `segments`（P3 生成侧落的，最全 —— 含逐段 OCR **行原文**）→ 原样返回；
    - 旧草稿（M5 之前生成，无该键）→ **即时派生**，界面不必分两套口径读。

    派生与落库**严格同源**：逐段文本走 `LC.split_by_boxes`（与 `build_rows` 同一实现），
    文本取 `.strip()`（`build_rows` 落库口径）；段标识按序取 `evidence.row_uids`。
    无框 → `[]`（界面据此不显示折叠，也绝不因此隐藏任何值 —— 值仍在 `text` 里）。
    """
    segs = entry.get("segments")
    if isinstance(segs, list) and segs:
        return segs
    boxes = [b for b in (entry.get("boxes") or []) if b]
    if not boxes:
        b = entry.get("box")
        boxes = [b] if b else []
    if not boxes:
        return []
    val = str(entry.get("text") or "").strip()
    _ws, keep = LC.segment_weights(boxes)
    parts = LC.split_by_boxes(val, boxes)
    if len(parts) != len(keep):                 # 退化 → 全部给末段（绝不丢字）
        parts = [""] * (len(keep) - 1) + [val] if keep else []
    uids = [str(u) for u in ((entry.get("evidence") or {}).get("row_uids") or [])]
    out: List[dict] = []
    for pos, (k, part) in enumerate(zip(keep, parts)):
        box = LC.rect_of_box(boxes[k])
        out.append({
            "seg": pos,
            "row_uid": uids[k] if k < len(uids) else "",
            "box": [float(v) for v in box] if box else [],
            "text": part,
            "n_chars": len(part),
            "ocr_line": "",                      # 旧草稿未落行原文 → 留空，界面不显示
        })
    return out


def build_rows(entry: dict, stem: str, image_name: str, idx: int,
               text: Optional[str] = None, box=None, attr: Optional[str] = None,
               ts: Optional[str] = None) -> Tuple[List[dict], bool]:
    """草稿条目 + 人工覆盖 → manual 行列表（多段 → gid 组多行）。

    返回 `(rows, edited)`；`rows` 为空表示校验失败（调用方据此报错）。
    `edited=True` 表示人工修改过值/框/属性 —— 即一条「AI 提议错、人对」的
    天然错例，为后续 few-shot 回流预留（本模块只标记，不回流）。
    """
    val = text if text is not None else entry.get("text", "")
    val = str(val or "").strip()
    if not val:
        return [], False
    a = (attr if attr is not None else entry.get("attr", "")) or ""
    a = str(a).strip()
    if len(a) > MAX_ATTR_NAME:
        return [], False

    boxes: List[list] = []
    if box is not None:
        r = LC.rect_of_box(box)          # 人工重画框 → 单段（覆盖草稿分段）
        if r is None:
            return [], False
        boxes = [[float(v) for v in r]]
    else:
        for b in (entry.get("boxes") or []):
            r = LC.rect_of_box(b)
            if r is not None:
                boxes.append([float(v) for v in r])
        if not boxes:
            r = LC.rect_of_box(entry.get("box"))
            if r is not None:
                boxes.append([float(v) for v in r])
    if not boxes:
        return [], False

    orig_text = str(entry.get("text") or "").strip()
    edited = bool(val != orig_text or a != str(entry.get("attr") or "").strip()
                  or box is not None)

    if len(boxes) == 1:
        parts, gid = [val], ""
    else:
        # M5：走 `LC.split_by_boxes`（**唯一**切分实现）——与 `anchor_page` 给面板
        # 预览的逐段文本必然逐字一致，这是"预览 == 落库"机械判据的落点。
        parts = LC.split_by_boxes(val, boxes)
        if len(parts) != len(boxes):          # 退化兜底：绝不丢字，全部给末段
            parts = [""] * (len(boxes) - 1) + [val]
        gid = f"{GID_PREFIX}{int(idx):03d}"

    ts = ts or datetime.now().isoformat(timespec="seconds")
    conf = str(entry.get("confidence") or "")
    # M3 修订：`ai_ref` 带上**身份三元组**的另外两项（attr / record）——
    # 只记 uid 时，同列兄弟条目的行会被"顺带"认领（reset 会误删）。见 `entry_identity`。
    _u0, _a0, _r0 = entry_identity(entry)
    rows: List[dict] = []
    for i, (b, part) in enumerate(zip(boxes, parts)):
        row: Dict[str, Any] = {
            "image_name": image_name,
            "box": [int(round(v)) for v in b],
            "text": part,
            "ts": ts,
            "source": SOURCE_AI_VERIFIED,
            "ai_ref": {"stem": stem, "idx": int(idx), "seg": i,
                       "uid": _u0, "attr": _a0, "record": _r0,
                       "confidence": conf, "edited": edited},
        }
        if a:
            row["attr"] = a
        prof = str(entry.get("profile") or "").strip()
        if prof:
            row["profile"] = prof
        if gid:
            row["gid"] = gid
        rows.append(row)
    return rows, edited


# ============================================
# 裁决主入口
# ============================================
def _apply(image_name: str, stem: str, remove_idxs: Sequence[int],
           rows: List[dict], manual_dir, archive_dir,
           uid_by_idx: Optional[Dict[int, Sequence]] = None) -> int:
    """删旧（同 stem+idx，或同 **uid+attr+record** 三元组）→ 按阅读序插入新行 → 一次原子写。

    `uid_by_idx`: `{草稿 idx: (uid, attr, record_index)}`（`entry_identity` 的产物）。
    ⚠ 传 `None`/缺项 = 旧口径（仅按 uid 或 idx 认领）—— 那是**会误删兄弟行**的旧行为，
      新调用方一律传三元组。
    """
    items = read_manual(image_name, manual_dir)
    before = len(items)
    kept = [it for it in items
            if not any(_matches_ai_ref(it, stem, i,
                                       *(uid_by_idx or {}).get(i, (None, None, None)))
                       for i in remove_idxs)]
    new = _insert_by_reading_order(kept, rows)
    if new != items:
        _write_manual(image_name, new, manual_dir, archive_dir)
    return len(new) - before


def adjudicate(stem: str, image_name: str, idx: int, action: str,
               text: Optional[str] = None, box=None, attr: Optional[str] = None,
               drafts_dir=None, status_path=None, manual_dir=None,
               archive_dir=None) -> Dict[str, Any]:
    """单条草稿裁决。action ∈ accept / reject / reset。

    - accept：草稿（可带人工覆盖的 text/box/attr）落 `manual_annotations/`，
      `source="ai_verified"`；**幂等**（重复采纳 = 先删旧行再写新行）。
    - reject：仅记状态，草稿原样留痕（不改任何标注文件）。
    - reset ：按 `ai_ref` 精确移除已采纳的行 + 清状态；找不到行也返回 ok。
    """
    stem = stem_of(stem)
    if action not in VALID_ACTIONS:
        return {"ok": False, "error": f"非法 action（{VALID_ACTIONS}）"}
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return {"ok": False, "error": "idx 必须是整数"}

    status = load_status(status_path)
    # M3 修订：身份 = (uid, attr, record_index) 三元组（uid 单独不足以区分同列兄弟）。
    # 草稿不在 → 空三元组 ⇒ 退回 (stem, idx) 旧口径。
    _e0 = next((e for e in load_drafts(stem, drafts_dir) if e["idx"] == idx), None)
    ident = entry_identity(_e0) if _e0 else ("", "", 0)
    uid = ident[0] or None
    key = _key(stem, idx, *ident)

    # ---- reset：不需要草稿仍在 ----
    if action == "reset":
        image_name = image_name or f"{stem}.png"
        before = len(read_manual(image_name, manual_dir))
        kept = [it for it in read_manual(image_name, manual_dir)
                if not _matches_ai_ref(it, stem, idx, *ident)]
        if len(kept) != before:
            _write_manual(image_name, kept, manual_dir, archive_dir)
        status.pop(key, None)
        # 顺带清掉**同一 uid 的另外两种键形态**，避免残留：
        #   `_key(stem, idx)`      = 迁移前老草稿写的 idx 键
        #   `_key(stem, idx, uid)` = M3 首版写的"仅 uid"键（无 attr/record）
        # 后者在前向路径上**不再被读**（见 `_status_get`：宁可重问不可错认），
        # 但留着会让"这页到底裁没裁过"读起来自相矛盾，且是我方 diagnostic 的噪音源。
        status.pop(_key(stem, idx), None)
        if uid:
            status.pop(_key(stem, idx, uid), None)
        save_status(status, status_path)
        return {"ok": True, "action": "reset", "stem": stem, "idx": idx,
                "removed": before - len(kept), "status": "pending"}

    entry = next((e for e in load_drafts(stem, drafts_dir) if e["idx"] == idx), None)
    if entry is None:
        return {"ok": False, "error": f"草稿条目不存在: {stem}#{idx}"}

    now = datetime.now().isoformat(timespec="seconds")
    image_name = image_name or str(entry.get("image_name") or f"{stem}.png")

    if action == "reject":
        status[key] = {"action": "reject", "ts": now, "uid": str(uid or ""),
                       "attr": ident[1], "record": ident[2],
                       "confidence": str(entry.get("confidence") or "")}
        save_status(status, status_path)
        return {"ok": True, "action": "reject", "stem": stem, "idx": idx,
                "status": "rejected"}

    rows, edited = build_rows(entry, stem, image_name, idx,
                              text=text, box=box, attr=attr, ts=now)
    if not rows:
        return {"ok": False, "error": "采纳失败：文本为空 / 无有效框 / 属性超长"}

    _apply(image_name, stem, [idx], rows, manual_dir, archive_dir,
           uid_by_idx={idx: ident} if uid else None)
    status[key] = {"action": "accept", "ts": now, "n_rows": len(rows),
                   "edited": edited, "uid": str(uid or ""),
                   "attr": ident[1], "record": ident[2],
                   "confidence": str(entry.get("confidence") or "")}
    save_status(status, status_path)
    return {"ok": True, "action": "accept", "stem": stem, "idx": idx,
            "image_name": image_name, "rows": rows, "edited": edited,
            "status": "accepted"}


# ============================================
# 预标注条目改属性（人显式修正 AI 标错的属性名，2026-09-26）
# ============================================
#: 改属性前归档目录名（相对 archive_dir）。结果产物与草稿层的原值都落这里，
#: 归档失败 ⇒ 拒绝改（fail closed，与 `write_validation` 同一条纪律）。
REATTR_ARCHIVE_NAME = "preattr_edits"


def _archive_one(p: Path, archive_dir, tag: str) -> Path:
    """单文件改前归档：`<archive_dir>/<tag>/<名>.<ts>.bak`；同一秒重名则顺延编号。"""
    if not p.exists():
        raise OSError(f"待归档文件不存在: {p}")
    ad = Path(archive_dir) / tag
    ad.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dst = ad / f"{p.name}.{ts}.bak"
    n = 1
    while dst.exists():
        n += 1
        dst = ad / f"{p.name}.{ts}.{n}.bak"
    shutil.copy2(p, dst)
    if not dst.exists() or dst.stat().st_size != p.stat().st_size:
        raise OSError(f"归档校验失败（副本不完整）：{dst}")
    return dst


def reassign_attr(stem: str, image_name: str, new_attr: str,
                  idx: Optional[int] = None, old_attr: Optional[str] = None,
                  uid: Optional[str] = None, record: Optional[int] = None,
                  drafts_dir=None, status_path=None, results_dir=None,
                  archive_dir=None) -> Dict[str, Any]:
    """把一条预标注条目的属性名从 old 改为 new（**人显式**动作，2026-09-26）。

    ## 改哪里（两处落点 + 一处清理）

    1. **结果产物** `data/results/<stem>.result.json`（draft_bridge 的输入源，
       飞轮重桥时以此为真）—— `records[record].attrs[]` 里 `attr == old` 的那条；
    2. **草稿层** `data/preannotations/<stem>.ai.jsonl`（裁决界面正在显示的那份）
       —— 只改定位到的那一行，其余行**原字节保留**（含坏行与空行）；
       注：本模块其余函数对草稿层只读，此处是**唯一**的写口，且写前必归档；
    3. **裁决状态** `data/adjudication/status.json` —— 判定状态键 =
       `(uid, attr, record_index)`（见 `entry_identity`），attr 变了旧键就指不到
       这条条目 ⇒ 旧键清掉（三种键形态都清，同 `reset`），**新键不预设**
       （条目回到 pending，由人重新裁决）。

    ## 安全阀

    - 旧状态为 **accept** 时拒绝：已采纳的行在 `manual_annotations/` 里带着
      旧 `ai_ref.attr`，改属性会让新采纳删不掉旧行（`_matches_ai_ref` 三者全中
      才认领）⇒ 金标准出现重复行。请先「撤回」再改属性。
    - 产物里同一记录下 `old_attr` 出现多条 → 拒绝（定位歧义，不猜）。
    - 产物 / 草稿**任一归档失败 → 两处都不写**。

    `error_kind`: `"invalid"`（参数不合法）/ `"missing"`（草稿或产物不存在，
    命令面映射退出码 3）/ `"ambiguous"` / `"conflict"`（已采纳，先撤回）。
    """
    stem = stem_of(stem)
    a_new = str(new_attr or "").strip()
    if not a_new:
        return {"ok": False, "error_kind": "invalid", "error": "new_attr 必填"}
    if len(a_new) > MAX_ATTR_NAME:
        return {"ok": False, "error_kind": "invalid",
                "error": f"属性名过长（> {MAX_ATTR_NAME}）"}

    drafts = load_drafts(stem, drafts_dir)
    if not drafts:
        return {"ok": False, "error_kind": "missing", "error": f"无草稿: {stem}"}

    # ---- 定位草稿条目：idx 优先，否则按身份三元组匹配 ----
    entry = None
    if idx is not None:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return {"ok": False, "error_kind": "invalid", "error": "idx 必须是整数"}
        entry = next((e for e in drafts if e["idx"] == idx), None)
        if entry is None:
            return {"ok": False, "error_kind": "missing",
                    "error": f"草稿条目不存在: {stem}#{idx}"}
    else:
        u = str(uid or "").strip()
        if not u:
            return {"ok": False, "error_kind": "invalid",
                    "error": "idx 与 uid 至少给一个（条目定位依据）"}
        ident0 = (u, str(old_attr or "").strip(), int(record or 0))
        hits = [e for e in drafts if entry_identity(e) == ident0]
        if not hits:
            return {"ok": False, "error_kind": "missing",
                    "error": f"草稿条目不存在: {stem} uid={u} attr={ident0[1]!r} "
                             f"record={ident0[2]}"}
        entry = hits[0]
        idx = entry["idx"]

    ident = entry_identity(entry)                 # (uid, attr, record_index)
    a_old = ident[1] if old_attr is None else str(old_attr).strip()
    if not a_old:
        return {"ok": False, "error_kind": "invalid",
                "error": "old_attr 必填（该条目当前属性为空，无可改之名）"}
    if a_old != str(entry.get("attr") or "").strip():
        return {"ok": False, "error_kind": "invalid",
                "error": f"old_attr 与草稿不符（草稿当前为 {entry.get('attr')!r}）"}
    if a_old == a_new:
        return {"ok": False, "error_kind": "invalid",
                "error": "new_attr 与 old_attr 相同，无需修改"}

    # ---- 旧状态检查：accept 时拒绝（防止金标准重复行）----
    status = load_status(status_path)
    st_old = _status_get(status, stem, idx, *ident) or {}
    if str(st_old.get("action") or "") == "accept":
        return {"ok": False, "error_kind": "conflict",
                "error": "该条目已采纳（accept）——请先「撤回」再改属性，"
                         "否则金标准里的旧行将无法被新采纳替换"}

    # ---- 改结果产物 ----
    try:
        import draft_bridge as DB
    except Exception as e:                         # pragma: no cover
        return {"ok": False, "error_kind": "internal",
                "error": f"draft_bridge 不可用: {type(e).__name__}: {e}"}
    rpath = DB.result_path(stem, results_dir)
    if not rpath.exists():
        return {"ok": False, "error_kind": "missing",
                "error": f"结果产物不存在: {rpath}"}
    try:
        result = json.loads(rpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "error_kind": "internal",
                "error": f"产物读取失败: {e}"}
    rec_i = ident[2]
    records = result.get("records") or []
    if not (0 <= rec_i < len(records)) or not isinstance(records[rec_i], dict):
        return {"ok": False, "error_kind": "missing",
                "error": f"产物里没有第 {rec_i} 号记录"}
    attrs = records[rec_i].get("attrs") or []
    hit = [a for a in attrs if isinstance(a, dict)
           and str(a.get("attr") or "").strip() == a_old]
    if len(hit) > 1:
        return {"ok": False, "error_kind": "ambiguous",
                "error": f"产物第 {rec_i} 号记录里属性 {a_old!r} 有 {len(hit)} 条，"
                         "定位歧义，拒绝修改"}
    if not hit:
        return {"ok": False, "error_kind": "missing",
                "error": f"产物第 {rec_i} 号记录里没有属性 {a_old!r}"}

    # ---- 归档（两处都成功才动手写）----
    dpath = draft_path(stem, drafts_dir)
    try:
        arc_r = _archive_one(rpath, archive_dir, REATTR_ARCHIVE_NAME)
        arc_d = _archive_one(dpath, archive_dir, REATTR_ARCHIVE_NAME)
    except OSError as e:
        return {"ok": False, "error_kind": "internal",
                "error": f"改前归档失败，两处均未写入：{e}"}

    hit[0]["attr"] = a_new
    data_io.atomic_write_json(rpath, result,
                              indent=1)   # 与既有产物缩进口径一致（读回不受影响）

    # ---- 改草稿层：只换定位行，其余行原字节保留 ----
    lines = dpath.read_text(encoding="utf-8").splitlines()
    new_entry = dict(entry)
    new_entry["attr"] = a_new
    lines[idx] = json.dumps(new_entry, ensure_ascii=False)
    data_io.atomic_write_text(dpath, "\n".join(lines) + "\n")

    # ---- 清旧状态键（三元组 / 仅 idx / 仅 uid 三种形态，同 reset）----
    status.pop(_key(stem, idx, *ident), None)
    status.pop(_key(stem, idx), None)
    if ident[0]:
        status.pop(_key(stem, idx, ident[0]), None)
    save_status(status, status_path)
    return {"ok": True, "stem": stem, "idx": idx, "record": rec_i,
            "old_attr": a_old, "new_attr": a_new, "uid": ident[0],
            "status": "pending", "archived": [str(arc_r), str(arc_d)],
            "result_file": str(rpath), "draft_file": str(dpath)}


# ============================================
# 批量采纳安全门（2026-09-10 立 → 2026-09-17 换判据，用户批准）
# ============================================
# 原则：**"相信 high"是机器的判断，"指定 idx"是人的判断**。
#   - 按置信层批量采纳 = 把机器分层当作人的判断 → 仅当**该档案已被独立回验且达标**
#     （且该页有原图可核对）才允许；
#   - 按显式 indices 批量 = 人已逐条点过 → 放行（人对自己点过的条目负责）。
# 换判据的理由见模块 docstring「批量采纳安全门」：来源标签只是必要条件，
# 「没测过」也不等于「偏离小」⇒ 改成 fail-closed 的**档案级回验读数**。
# 红线二的实质是"固化必须由人的判断触发"，故此处不是 UI 层的禁用，
# 而是**数据层拒绝**（前端禁用只是提示）。
def geometry_source(stem: str, structured_dir=None) -> Optional[str]:
    """页几何来源 = `L1_blocks.source`。无块层 / 读不到 / 无该键 → None。"""
    p = Path(structured_dir or DEFAULT_STRUCTURED_DIR) / f"{safe_name(stem_of(stem))}.json"
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    src = (doc.get("L1_blocks") or {}).get("source")
    return str(src) if src else None


def has_page_image(stem: str, image_dir=None) -> bool:
    """该页是否**有原图可核对**（P4，2026-09-17）。

    为什么它是一道独立的门：实测 21 页"有几何、无原图" —— 这类页在裁决界面上
    **人点进去没有图可看**，只能盲签。既然后果是"人无法履行核对职责"，
    就不该允许批量采纳（批量恰恰是把"人核对"这一步也省掉的路径）。

    解析口径复用 `config.image_path_of`（全项目解析页图的唯一入口），
    传入 `image_dir` 时只搜该目录（测试隔离）。
    **取不到配置模块时返回 False**（fail-closed：无法证明有图 ⇒ 当作没有）。
    """
    try:
        import config as CONFIG
    except Exception:                                  # pragma: no cover
        return False
    try:
        return CONFIG.image_path_of(stem_of(stem), given=image_dir) is not None
    except Exception:                                  # pragma: no cover
        return False


def drift_suspension(stem: str, profile_id: Optional[str] = None,
                     drift_dir=None) -> Optional[dict]:
    """P5 挂起状态（**只读 P5 的判定缓存，不在裁决路径上重算图像**）。

    无 `profile_id`（草稿未记档案）/ 无缓存 / P5 模块不可用 → None（不据此拦截）。
    刻意不在此处调用 `drift.verdict_for`：那要解整页图像，而本函数在每次取数时
    都会被调用——判定应由 P5 的批扫描（或显式扫描）产出。
    """
    if not profile_id:
        return None
    try:
        import drift as DR
    except Exception:                                  # pragma: no cover
        return None
    v = (DR.load_state(drift_dir).get("profiles", {})
         .get(profile_id, {}).get("verdicts", {}).get(DR.LC.stem_of(stem)))
    if not v or v.get("verdict") != DR.V_DRIFT:
        return None
    return {"verdict": v.get("verdict"), "conformance": v.get("conformance"),
            "failed": v.get("failed") or [], "reasons": v.get("reasons") or [],
            "ts": v.get("ts")}


def validation_path(profile_id: str, validation_dir=None) -> Path:
    """档案 → 回验记录文件 `data/validation/<profile_id>.json`。"""
    return Path(validation_dir or DEFAULT_VALIDATION_DIR) / f"{safe_name(profile_id)}.json"


def load_validation(profile_id: Optional[str], validation_dir=None) -> Optional[dict]:
    """读某档案的回验记录；无档案 / 无文件 / 坏 JSON / 非 dict → `None`。"""
    if not profile_id:
        return None
    p = validation_path(profile_id, validation_dir)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return d if isinstance(d, dict) else None


def write_validation(profile_id: str, verdict: str, metric: Optional[str] = None,
                     value: Optional[float] = None, n_boxes: Optional[int] = None,
                     pages: Optional[Sequence[str]] = None, note: str = "",
                     validation_dir=None, archive_dir=None, ts: Optional[str] = None
                     ) -> Dict[str, Any]:
    """登记某档案的**独立回验读数**（`batch_gate` 的唯一放行依据）。

    为什么是"登记"而不是"计算"：本函数**不实现任何度量**——度量属离线对评工具
    （零 API、作用于已落盘产物 + 金标准）。数据层只负责"把人的判断记下来并据此
    放行"，与红线二一致：**决定权不在 agent 手里，也不在本模块里**。

    `verdict` 只接受 `pass` / `fail`（**没有第三种**：拿不准就是 fail）。
    覆盖写旧记录前先归档到 `_archive/validation_<pid>.<ts>.bak`——
    归档失败即拒绝写入（与 `profile_cli --overwrite` 同一条纪律）。

    ★ 失败返回带 `error_kind`（`"invalid"` / `"io"`）——**供命令面映射退出码**
      （4 校验不过 / 1 内部错误）而**不必去匹配错误字符串**。同 `batch_gate` 的
      `blocked` 一脉：底层给出**机器可判别**的档位，面只做转达。
      域判定仍**只此一处**（命令面不得自己再判一次 `verdict` 合法性 = 第二份真相）。
    """
    v = str(verdict or "").strip().lower()
    if v not in ("pass", "fail"):
        return {"ok": False, "error_kind": "invalid",
                "error": f"verdict 只能是 pass/fail，收到 {verdict!r}"}
    if not profile_id:
        return {"ok": False, "error_kind": "invalid",
                "error": "缺 profile_id：回验读数是**档案级**的"}
    now = ts or datetime.now().isoformat(timespec="seconds")
    p = validation_path(profile_id, validation_dir)
    doc = {
        "_schema_version": VALIDATION_SCHEMA,
        "profile_id": str(profile_id),
        "verdict": v,
        "metric": str(metric) if metric else "manual",
        "value": (float(value) if value is not None else None),
        "n_boxes": (int(n_boxes) if n_boxes is not None else None),
        "pages": [str(x) for x in (pages or [])],
        "note": str(note or ""),
        "measured_at": now,
    }
    archived = ""
    if p.exists():
        arc_dir = Path(archive_dir or DEFAULT_ARCHIVE_DIR)
        try:
            arc_dir.mkdir(parents=True, exist_ok=True)
            dst = arc_dir / f"{p.stem}.validation_{time.strftime('%Y%m%d_%H%M%S')}.bak"
            shutil.copy2(p, dst)
            archived = str(dst)
        except OSError as e:
            return {"ok": False, "error_kind": "io",
                    "error": f"归档旧记录失败，已拒绝写入：{e}"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        return {"ok": False, "error_kind": "io", "error": f"写入失败：{e}"}
    return {"ok": True, "path": str(p), "profile_id": doc["profile_id"],
            "verdict": v, "archived": archived, "doc": doc}


def batch_gate(stem: str, structured_dir=None, profile_id: Optional[str] = None,
               drift_dir=None, image_dir=None, validation_dir=None) -> Dict[str, Any]:
    """批量采纳（按置信层）的准入判定。**四道检查，全部 fail-closed**。

    1. **P5 挂起**（版式漂移）：该页版式与档案契约不符 → 契约参数本就不可用，先拦；
    2. **无几何层**：`L1_blocks` 缺失 ⇒ 草稿框只是兜底产物（**事实缺失**，
       与"几何质量好坏"是两回事——后者归第 4 道门管）；
    3. **无原图**：人点进去没有图可核 ⇒ 不允许走"连核对也省掉"的批量路径（P4）；
    4. **档案级独立回验**：无记录 ⇒ 未验证不放行；有记录但非 `pass` ⇒ 拒绝。

    返回 `{batch_safe, blocked, geometry_source, batch_block_reason, validation}`。
    `geometry_source` **只作参考信息**（不再区分 `pipeline` / `l2_reconstructed`
    ——那是"质量优劣"，已交给回验读数；见模块 docstring）；
    `validation` 为**该档案的回验记录摘要**（无档案 / 无记录为 `None`）——
    ★ 它在**任何一道门拦下时都照常给出**：那是"档案登记了什么"的**事实陈述**，
      不是第 4 道门的产物。若只在第 4 道门里读，被前 3 道拦下的页会显示成
      "回验无记录" —— 把**没查**讲成**没有**，界面就会报错原因（2026-09-17 修）。
    前端据此禁用按钮并**显示原因**（不能只灰掉不说为什么——用户会以为是 bug）。
    """
    stem = stem_of(stem)
    src = geometry_source(stem, structured_dir)

    # ★ 先取回验记录（与哪道门拦下无关），见 docstring。
    rec = load_validation(profile_id, validation_dir)
    brief = None
    if rec is not None:
        brief = {"verdict": rec.get("verdict"), "metric": rec.get("metric"),
                 "value": rec.get("value"), "n_boxes": rec.get("n_boxes"),
                 "pages": rec.get("pages") or [], "measured_at": rec.get("measured_at")}

    # 1) 版式漂移挂起
    susp = drift_suspension(stem, profile_id, drift_dir)
    if susp:
        detail = "；".join(susp["reasons"][:2]) or "符合度不足"
        return {"batch_safe": False, "blocked": "drift_suspended",
                "geometry_source": src, "validation": brief,
                "batch_block_reason":
                    f"本页版式与档案契约不符（符合度 {susp['conformance']}）→ 已挂起，"
                    f"批量采纳停用，请逐条裁决或在版式页登记 variant。{detail}",
                "drift": susp}

    # 2) 无几何层 → 不放行（**事实**缺失，不是质量判断）
    if src is None:
        return {"batch_safe": False, "blocked": "no_geometry",
                "geometry_source": None, "validation": brief,
                "batch_block_reason":
                    "本页**没有几何层**（`L1_blocks` 缺失）→ 草稿框只能是兜底产物，"
                    "不是管线在像素上定过的 → 批量采纳已停用，请逐条裁决。"}

    # 3) 无原图 → 不放行（人无法履行核对职责）
    if not has_page_image(stem, image_dir):
        return {"batch_safe": False, "blocked": "no_image",
                "geometry_source": src, "validation": brief,
                "batch_block_reason":
                    "本页**没有原图**（outbox/ 与 inbox/ 均未找到）→ 界面上无图可核，"
                    "批量采纳已停用。请先把原图放回 outbox/，或逐条裁决。"}

    # 4) 档案级独立回验读数（fail-closed）
    if rec is None:
        who = f"档案 {profile_id}" if profile_id else "本页所属档案"
        return {"batch_safe": False, "blocked": "unverified_profile",
                "geometry_source": src, "validation": None,
                "batch_block_reason":
                    f"{who}**尚未做过独立回验**（`data/validation/` 无记录）→ "
                    f"「没测过」不等于「偏离小」，故不放行批量采纳。"
                    f"解锁：标注 3 页金标准并跑一次离线对评，再登记读数。"}
    if str(rec.get("verdict")) != "pass":
        return {"batch_safe": False, "blocked": "profile_not_passed",
                "geometry_source": src, "validation": brief,
                "batch_block_reason":
                    f"本页所属档案的回验结论为 **{rec.get('verdict')!r}**"
                    f"（{rec.get('metric')}={rec.get('value')}，"
                    f"{rec.get('measured_at')}）→ 未达标，批量采纳停用。"
                    f"请先修几何 / 修规则后重新回验，或逐条裁决。"}
    return {"batch_safe": True, "blocked": None, "geometry_source": src,
            "validation": brief, "batch_block_reason": ""}


def batch_adjudicate(stem: str, image_name: str, action: str = "accept",
                     confidences: Sequence[str] = ("high",), indices=None,
                     drafts_dir=None, status_path=None, manual_dir=None,
                     archive_dir=None, structured_dir=None, drift_dir=None,
                     image_dir=None, validation_dir=None,
                     allow_untrusted: bool = False) -> Dict[str, Any]:
    """批量裁决（如「一键采纳全部 high」）。**单次重写文件**，不做 N 次 IO。

    `indices` 显式给定则优先（忽略 confidences）；否则取置信层匹配且当前
    状态为 pending 的全部条目（已裁决的不重复处理）。

    **安全门**（`indices` 为空即"按置信层"）：① P5 挂起（版式漂移）或
    ② 几何来源不可信时**拒绝执行**并返回 `error`（见模块内「批量采纳安全门」段）。
    `allow_untrusted=True` 仅供测试/离线批处理显式放行，**不要**接到界面上。
    """
    stem = stem_of(stem)
    if action not in ("accept", "reject"):
        return {"ok": False, "error_kind": "usage",
                "error": "批量仅支持 accept / reject（reset 请逐条）"}
    drafts = load_drafts(stem, drafts_dir)
    if indices is None and not allow_untrusted:
        prof = str((drafts[0].get("profile") if drafts else "") or "") or None
        gate = batch_gate(stem, structured_dir, profile_id=prof, drift_dir=drift_dir,
                          image_dir=image_dir, validation_dir=validation_dir)
        if not gate["batch_safe"]:
            return {"ok": False, "error": gate["batch_block_reason"], **gate}
    if not drafts:
        return {"ok": False, "error_kind": "missing", "error": f"无草稿: {stem}"}
    status = load_status(status_path)

    if indices is not None:
        want = {int(i) for i in indices}
        picked = [e for e in drafts if e["idx"] in want]
    else:
        confs = {str(c) for c in (confidences or [])}
        picked = [e for e in drafts
                  if str(e.get("confidence") or "") in confs
                  and _status_get(status, stem, e["idx"], *entry_identity(e)) is None]
    if not picked:
        return {"ok": True, "action": action, "stem": stem, "n": 0,
                "indices": [], "skipped": len(drafts)}

    now = datetime.now().isoformat(timespec="seconds")
    image_name = image_name or str(picked[0].get("image_name") or f"{stem}.png")
    idxs = [e["idx"] for e in picked]
    accepted, edited_n, bad = [], 0, []
    rows_all: List[dict] = []
    for e in picked:
        rows, edited = build_rows(e, stem, image_name, e["idx"], ts=now)
        if not rows:
            bad.append(e["idx"])
            continue
        rows_all.extend(rows)
        accepted.append(e["idx"])
        edited_n += 1 if edited else 0
        _ident = entry_identity(e)
        status[_key(stem, e["idx"], *_ident)] = {
            "action": action, "ts": now, "n_rows": len(rows), "edited": edited,
            "uid": _ident[0], "attr": _ident[1], "record": _ident[2],
            "confidence": str(e.get("confidence") or ""), "batch": True}

    if action == "accept" and rows_all:
        _apply(image_name, stem, accepted, rows_all, manual_dir, archive_dir,
               uid_by_idx={e["idx"]: entry_identity(e)
                           for e in picked if e.get("row_uid")})
    save_status(status, status_path)
    return {"ok": True, "action": action, "stem": stem, "n": len(accepted),
            "indices": accepted, "edited": edited_n, "failed": bad}


# ============================================
# 页面状态（裁决界面唯一取数口）
# ============================================
def page_state(stem: str, image_name: Optional[str] = None,
               drafts_dir=None, status_path=None, manual_dir=None,
               structured_dir=None, drift_dir=None,
               image_dir=None, validation_dir=None) -> Dict[str, Any]:
    """草稿 + 裁决状态 + 交叉校验后的逐条视图 + 批量准入判定。

    **状态与事实自洽**：状态记 accepted 但人工层已无对应行（用户在 annotate 页
    删掉了）→ 视为 pending，避免「幽灵已采纳」。
    """
    stem = stem_of(stem)
    image_name = image_name or f"{stem}.png"
    drafts = load_drafts(stem, drafts_dir)
    status = load_status(status_path)
    manual = read_manual(image_name, manual_dir)
    # 「幽灵状态」交叉校验的判活集合。
    # **M3 修订（2026-09-16）**：判活必须与写入侧用同一把钥匙（三元组）。
    # 实测（36 页 727 条）：只按 uid 判活 → 344 键 / 171 碰撞组 / 554 条（76.2%）
    # 与兄弟条目共键 ⇒ 把同列兄弟的已采纳行算到自己头上（**假活**，幽灵不抹）；
    # 只按 idx 判活 → 阅读序漂移后把真实存在的行误判成幽灵（**假幽灵**，状态被抹）。
    # 故：
    #   `live_ids` = 修订后写的行，键为 `(stem, uid, attr, record)` —— 与 `_key` 同构；
    #   `live_idx` = **无 uid** 的旧条目兜底（那种条目的状态键也是 `<stem>::<idx>`，
    #                见 `_key`：uid 为空时三元组退化成 idx，故两边一致）。
    #
    # ★ 刻意**不**收「修订前的仅 uid 行」：那种行无法判断属于哪个兄弟条目，
    #   收进来等于把 76.2% 的碰撞重新引回判活。代价是"旧行的状态会显示成 pending"，
    #   而那是**可自愈**的：重采一次即可（采纳幂等，`_apply` 会凭旧 `ai_ref` 先删旧行）。
    #   存量代价实测为**零**（`manual_annotations/` 带 `ai_ref.uid` 的行 = 0）。
    live_idx, live_ids = set(), set()
    for it in manual:
        ref = it.get("ai_ref")
        if not isinstance(ref, dict):
            continue
        st_ = str(ref.get("stem", ""))
        live_idx.add((st_, int(ref.get("idx", -1))))
        u_ = str(ref.get("uid") or "")
        if u_ and ("attr" in ref or "record" in ref):
            live_ids.add((st_, u_, str(ref.get("attr") or "").strip(),
                          int(ref.get("record") or 0)))

    entries: List[dict] = []
    by_conf: Dict[str, int] = {c: 0 for c in VALID_CONFIDENCES}
    n_acc = n_rej = 0
    for e in drafts:
        conf = str(e.get("confidence") or "low")
        if conf not in by_conf:
            by_conf[conf] = 0
        by_conf[conf] += 1
        uid_e = str(e.get("row_uid") or "")
        # 与写入侧同一把钥匙：三元组（uid/attr/record_index）
        _ident_e = entry_identity(e)
        st = _status_get(status, stem, e["idx"], *_ident_e) or {}
        act = str(st.get("action") or "")
        is_live = (bool(uid_e) and (stem, uid_e, _ident_e[1], _ident_e[2]) in live_ids) or \
                  (stem, int(e["idx"])) in live_idx
        if act == "accept" and not is_live:
            act = ""                       # 幽灵状态 → 回落 pending
        if act == "accept":
            n_acc += 1
        elif act == "reject":
            n_rej += 1
        item = dict(e)
        item["status"] = {"accept": "accepted", "reject": "rejected"}.get(act, "pending")
        item["edited"] = bool(st.get("edited"))
        item["decided_at"] = str(st.get("ts", ""))
        # M5：给界面一个**恒非空**的分段视图（多段条目 → 可折叠展开逐段核对）。
        # 生成侧已落则原样透传，旧草稿即时派生 —— 界面不必分两种口径。
        item["segments"] = segments_of(item)
        entries.append(item)
    out = {
        "success": True, "stem": stem, "image_name": image_name,
        "has_draft": bool(drafts), "entries": entries,
        "stats": {"total": len(drafts), "accepted": n_acc, "rejected": n_rej,
                  "pending": len(drafts) - n_acc - n_rej, "by_confidence": by_conf,
                  "manual_rows": len(manual)},
    }
    # 把 P4 的验收口径（操作次数下降率）随取数一起给出——界面直接显示，
    # 不自作二次计算（度量口径单一来源，见 §34 d）。
    out["estimate"] = estimate_ops(out)
    # 批量采纳准入（两道安全门）：界面据此禁用按钮并显示原因；后端也会独立拒绝。
    prof = str((drafts[0].get("profile") if drafts else "") or "") or None
    gate = batch_gate(stem, structured_dir, profile_id=prof, drift_dir=drift_dir,
                      image_dir=image_dir, validation_dir=validation_dir)
    out.update(gate)
    # P5 挂起详情（即便未被拦也告知，便于界面提示"本页已挂起"）
    out["drift"] = gate.get("drift") or drift_suspension(stem, prof, drift_dir)
    return out


# ============================================
# 人工操作次数度量（方案 §6 P4 验收：较现状下降 ≥ 80%）
# ============================================
#: 全手工标注一条属性的操作次数：拖框 + 选属性 + 输文本 + Enter。
MANUAL_OPS_PER_ITEM = 4
#: 采纳一条草稿：单击「通过」。
ACCEPT_OPS = 1
#: 修正后采纳：单击「修正」+ 确认（输入框已预填草稿值，仅改错字）。
AMEND_OPS = 2


def estimate_ops(state: Dict[str, Any],
                 amend_rate: Optional[float] = None) -> Dict[str, Any]:
    """建模估算单页人工操作次数（现状 vs 草稿），两种交互策略分别给出。

    ⚠️ 这是**建模估算**而非真人计时：以「每条属性的必要交互次数」为单位，
    全手工 4 次/条（拖框+选属性+输入+确认），采纳 1 次、修正后采纳 2 次。

    - `draft_ops_each` 逐条点击「通过」；
    - `draft_ops_batch` **先一键采纳全部 high（记 1 次点击）**，其余逐条。

    方案 §6 的验收口径取 `drop_batch`（推荐工作流）。实测：样例页 0001（27 条，
    high 18）逐条 27 次 → 降 75%，**不足 80%**；批量 10 次 → 降 90.7% ✔。
    即「一键采纳 high」不是锦上添花，而是达标的前提。

    `amend_rate` 缺省按已裁决条目的 `edited` 实测比例；无已裁决数据时退化为
    「全部直通」（0.0），故该值应视为**乐观下界**。
    """
    stats = state.get("stats") or {}
    total = int(stats.get("total") or 0)
    if total <= 0:
        return {"items": 0, "high": 0, "manual_ops": 0, "draft_ops_each": 0,
                "draft_ops_batch": 0, "drop_each": 0.0, "drop_batch": 0.0}
    if amend_rate is None:
        decided = [e for e in state.get("entries", [])
                   if e.get("status") == "accepted"]
        amend_rate = (sum(1 for e in decided if e.get("edited")) / len(decided)
                      if decided else 0.0)
    amend_rate = max(0.0, min(1.0, float(amend_rate)))
    n_high = int((stats.get("by_confidence") or {}).get("high") or 0)
    per_item = (1 - amend_rate) * ACCEPT_OPS + amend_rate * AMEND_OPS
    base = total * MANUAL_OPS_PER_ITEM
    each = total * per_item
    batch = (ACCEPT_OPS if n_high else 0) + max(0, total - n_high) * per_item
    return {
        "items": total, "high": n_high,
        "amend_rate": round(amend_rate, 4),
        "manual_ops": base,
        "draft_ops_each": round(each, 2),
        "draft_ops_batch": round(batch, 2),
        "saved_batch": round(base - batch, 2),
        "drop_each": round((base - each) / base, 4) if base else 0.0,
        "drop_batch": round((base - batch) / base, 4) if base else 0.0,
    }
