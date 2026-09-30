"""校正感知的行文本读取层（P-K9 阶段 0b，2026-09-10）。

背景（"正确性供给"漏口）：
    ``data/structured/<stem>.json`` 是 canonical 层，保存 OCR 原始输出，
    **永不改写**（见 knowledge_card / unit_builder 的数据流约定）。人工校对
    结果落在 ``output.xlsx`` 的「校对后文本」列，经 ``unit_builder`` 汇入
    ``data/units/``——但只有语义单元层与主检索吃到校正。其余消费面
    （chat_tools 工具层、rag 的 doc 兜底语料、知识卡片的行/段卡导出）此前
    **直读 structured 原文**，等于把首过正确率有限的识别结果当权威原文使用。

本模块给这些消费面提供**同一口径**的校正感知读取。

口径唯一（本项目"一个概念一处定义"纪律的延伸）：
- 「真校对」判定 = ``unit_builder.corrected_map_strict``
  （与 OCR 原文做差集，剔除以原文预填的 6413 条伪修正）
- 行粒度错位防护 = ``exporter.safe_corrected``
  （L2 整栏文本与主工作簿单字行共享 xlsx_id 编号，直接套用会串行；
   宁可漏接修正，不可错接修正）

与 ``reassembler._text_of`` 的一处**有意差异**：safe_corrected 判定为错位而
拒绝时，``_text_of`` 会返回空串（该行文本被抹掉，进而影响 units 的字符多集
校验），本模块则**保留原文**并标记 ``corrected=False``——对展示与检索而言，
"未应用校正"应等于原样返回，而非清空。此差异已记入专项审计待统一。

缓存：主工作簿修正表按 (路径, mtime_ns, size) 缓存，避免每次提问重读 7800+ 行。

零新依赖（stdlib + 项目既有模块），失败一律静默降级为"无校正"。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("local_chronicles_ocr")

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录
else:
    _BASE = Path(__file__).parent.resolve()

# 项目根下的主工作簿（校对结果所在）
DEFAULT_XLSX_NAME = "output.xlsx"

# 修正表缓存：{xlsx 绝对路径: {"key": (mtime_ns, size), "map": {...}}}
_MAP_CACHE: dict[str, dict[str, Any]] = {}


# ============================================
# 1. 主工作簿修正表（真校对）
# ============================================
def load_corrected_map(
    xlsx_path: Optional[Path | str] = None,
    *,
    use_cache: bool = True,
) -> dict[int, str]:
    """读主工作簿 → ``{xlsx_id: 校对后文本}``，仅含"真校对"。

    Args:
        xlsx_path: 主工作簿路径。None → 项目根 ``output.xlsx``。
        use_cache: 以 (mtime_ns, size) 为键缓存；文件被写即自动失效。

    Returns:
        dict[int, str]；文件缺失/损坏/无真校对 → ``{}``（不抛错）。
    """
    path = Path(xlsx_path) if xlsx_path else (_BASE / DEFAULT_XLSX_NAME)
    try:
        if not path.exists():
            return {}
        st = path.stat()
        ck = str(path.resolve())
        if use_cache:
            hit = _MAP_CACHE.get(ck)
            if hit and hit["key"] == (st.st_mtime_ns, st.st_size):
                return hit["map"]
    except OSError:
        return {}

    # 真校对判定唯一来源：unit_builder（惰性导入，避免模块加载期付出重依赖成本）
    try:
        from unit_builder import corrected_map_strict
        result = corrected_map_strict(path)
    except Exception as e:  # noqa: BLE001 - 读取失败一律降级为无校正
        log.warning("[corrected_text] 读取修正表失败（按无校勘处理）: %s", e)
        result = {}

    if not isinstance(result, dict):
        result = {}
    if use_cache:
        _MAP_CACHE[ck] = {"key": (st.st_mtime_ns, st.st_size), "map": result}
    return result


def clear_cache() -> int:
    """清空修正表缓存，返回清掉的条目数（测试与调试用）。"""
    n = len(_MAP_CACHE)
    _MAP_CACHE.clear()
    return n


def map_for_structured_dir(structured_dir: Optional[Path | str]) -> dict[int, str]:
    """从 ``.../data/structured`` 反推主工作簿（``.../output.xlsx``）。

    目录层级不符（如测试的临时目录）或文件不存在 → ``{}``，即"无校正"，
    保证注入式临时目录的行为与改造前完全一致。
    """
    if not structured_dir:
        return {}
    p = Path(structured_dir)
    if p.name != "structured":
        return {}
    return load_corrected_map(p.parent.parent / DEFAULT_XLSX_NAME)


# ============================================
# 2. 行级校正应用
# ============================================
def apply_correction(
    line: dict,
    corrected_map: Optional[dict[int, str]],
) -> tuple[str, bool]:
    """单行 → ``(生效文本, 是否经人工校正确认)``。

    未命中修正、或 safe_corrected 判定为行粒度错位 → 返回原文 + False。
    """
    raw = str(line.get("text", "") or "")
    if not corrected_map:
        return raw, False
    xid = line.get("xlsx_id")
    if xid is None:
        return raw, False
    try:
        mapped = corrected_map.get(int(xid))
    except (TypeError, ValueError):
        return raw, False
    if not mapped:
        return raw, False
    try:
        from exporter import safe_corrected
        safe = safe_corrected(raw, str(mapped))
    except Exception:  # noqa: BLE001 - 校验层异常不应影响读取
        return raw, False
    if safe and safe != raw:
        return safe, True
    return raw, False


def corrected_lines(
    structured: Optional[dict],
    corrected_map: Optional[dict[int, str]] = None,
) -> list[dict]:
    """structured JSON → L2 行列表（``text`` 已套用校正，附 ``corrected`` 标记）。

    每行是原 dict 的浅拷贝 + 覆写 ``text`` + 新增 ``corrected``，原 dict 不被修改。
    保持 ``L2_lines`` 原顺序（调用方若需阅读序请自行按 col/row 排序）。
    """
    if not isinstance(structured, dict):
        return []
    out: list[dict] = []
    for ln in structured.get("L2_lines") or []:
        if not isinstance(ln, dict):
            continue
        text, flag = apply_correction(ln, corrected_map)
        item = dict(ln)
        item["text"] = text
        item["corrected"] = flag
        out.append(item)
    return out


def line_stats(lines: Optional[list]) -> dict:
    """行级校正统计 → ``{"lines": n, "corrected": k, "corrected_ratio": r}``。"""
    lines = lines or []
    n = len(lines)
    k = sum(1 for ln in lines if isinstance(ln, dict) and ln.get("corrected"))
    return {
        "lines": n,
        "corrected": k,
        "corrected_ratio": round(k / n, 4) if n else 0.0,
    }


def load_lines_for_stem(
    stem: str,
    structured_dir: Path | str,
    corrected_map: Optional[dict[int, str]] = None,
) -> list[dict]:
    """便捷入口：按 stem 读 structured 并返回校正感知的行列表。

    ``corrected_map=None`` 时按 ``structured_dir`` 反推主工作簿；文件缺失即无校正。
    """
    import json

    if corrected_map is None:
        corrected_map = map_for_structured_dir(structured_dir)
    path = Path(structured_dir) / f"{stem}.json"
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("[corrected_text] structured 读失败 %s: %s", stem, e)
        return []
    return corrected_lines(d, corrected_map)
