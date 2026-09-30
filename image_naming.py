"""image_naming.py - 入图命名共享模块（2026-09-02 命名规范 v2）

背景（用户需求 2026-09-02）：
- 剪贴板/文件夹监听入图改名：`YYYYMMDD_NNNN_<hash8>.<ext>`
  例：2026-09-02 当天第 3 张截图 → `20260902_0003_a1b2c3d4.png`
  NNNN   = 当天进入顺序（4 位补零，从 0001 起，跨进程重启只增不减）
  hash8  = 像素级内容 hash 前 8 位（"系统编码"，同图跨天编码稳定）
- 批量导入：保留用户原文件名（sanitize 后）；PDF 多页 = `原文件名_NNNN`（页码 4 位）
  例：`样例材料_0012.png` = 原 PDF 第 12 页

设计要点：
- 当天序号不落计数器文件：每次首用时扫描 inbox/outbox 里 `YYYYMMDD_NNNN_`
  前缀文件取最大序号 +1（磁盘真实状态推算 → 天然跨进程/重启安全），
  进程内缓存避免每张图全目录扫描。
- watcher 主循环单线程取号；进程内 RLock 兜底线程安全。
- 存量旧格式文件不受影响；旧文件不重命名（P-K8 v1 文件保持原样）。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

# 非法文件名字符（与 image_rename.INVALID_FILENAME_CHARS 同规则）
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 当天序号前缀：`YYYYMMDD_NNNN_`（命名规范 v2 识别用）
DAILY_SEQ_PATTERN = re.compile(r"^(\d{8})_(\d{4})_")

_seq_lock = threading.Lock()
_seq_cache: dict[str, int] = {}  # date_str -> 当前最大序号（进程内缓存）


def sanitize_stem(name: str, max_len: int = 80) -> str:
    """清洗用户原文件名 stem：去非法字符/控制字符、压缩空白、去首尾空白与点、截断。

    保留中文与常规可读字符；清洗后为空 → 返回 "imported"。
    """
    s = _INVALID_CHARS.sub("", str(name))
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "imported"


def scan_daily_max_seq(inbox: Path, outbox: Path, date_str: str) -> int:
    """扫描 inbox/outbox 中 `YYYYMMDD_NNNN_` 前缀（date_str 匹配）的最大序号。无 → 0。"""
    max_seq = 0
    for d in (inbox, outbox):
        if not d.exists():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.is_file():
                continue
            m = DAILY_SEQ_PATTERN.match(p.name)
            if m and m.group(1) == date_str:
                try:
                    seq = int(m.group(2))
                except ValueError:
                    continue
                if seq > max_seq:
                    max_seq = seq
    return max_seq


def next_daily_seq(inbox: Path, outbox: Path, date_str: str) -> int:
    """取下一个当天序号（以磁盘真实状态推算，跨进程/重启安全）。

    注意：取号后若文件写入失败序号会空洞（如 0001、0003）——
    "进入顺序"允许极罕见空洞，不做回滚（避免把失败名再分配给内容不同的图）。
    """
    with _seq_lock:
        cached = _seq_cache.get(date_str)
        if cached is None:
            cached = scan_daily_max_seq(inbox, outbox, date_str)
        cached += 1
        _seq_cache[date_str] = cached
        return cached


def reset_daily_seq_cache() -> None:
    """清空进程内序号缓存（测试用 / 外部进程改动 inbox 后强制重扫）。"""
    with _seq_lock:
        _seq_cache.clear()


def make_daily_image_name(
    inbox: Path, outbox: Path, hash8: str, ext: str = ".png",
    now: datetime | None = None,
) -> str:
    """剪贴板/监听入图命名 v2：`YYYYMMDD_NNNN_<hash8>.<ext>`。

    hash8 非法（非 hex）时回退 "00000000"（防御，不抛错——保存流程不应因命名中断）。
    """
    dt = now or datetime.now()
    date_str = dt.strftime("%Y%m%d")
    seq = next_daily_seq(inbox, outbox, date_str)
    h = str(hash8).lower()
    if not re.fullmatch(r"[0-9a-f]{4,16}", h):
        h = "00000000"
    if not ext.startswith("."):
        ext = "." + ext
    return f"{date_str}_{seq:04d}_{h}{ext}"


def name_taken(inbox: Path, outbox: Path, name: str) -> bool:
    """目标名是否已被 inbox/outbox 任一文件占用。"""
    return (inbox / name).exists() or (outbox / name).exists()


def unique_name(
    inbox: Path, outbox: Path, name: str,
    extra_taken: set[str] | None = None, max_try: int = 9999,
) -> str:
    """目标名被占用时在 stem 后追加 `_2`、`_3` … 递增，直到可用。

    extra_taken：批内已分配但尚未落盘的目标名（防同一批导入的
    同名文件互相覆盖——unique_name 只查磁盘，查不到 plan 里的名字）。
    """
    taken = extra_taken if extra_taken else set()
    if name not in taken and not name_taken(inbox, outbox, name):
        return name
    stem = Path(name).stem
    ext = Path(name).suffix
    for i in range(2, max_try + 2):
        cand = f"{stem}_{i}{ext}"
        if cand not in taken and not name_taken(inbox, outbox, cand):
            return cand
    raise RuntimeError(f"无法为 {name} 生成可用文件名（尝试 {max_try} 次）")


def unique_base_for_pages(
    inbox: Path, outbox: Path, base: str, ext: str = ".png",
    extra_taken: set[str] | None = None, max_try: int = 9999,
) -> str:
    """PDF 多页命名选 base：确保 `base_0001<ext>` 起的页名整体可用。

    若 `base_0001.png` 已被占用（同名 PDF 重复导入且内容 hash 不同），
    追加 `_2`、`_3` … 得到新 base（如 `原名_2`），保证同 PDF 页名连续。
    extra_taken 语义同 unique_name（批内防撞）。
    """
    if not ext.startswith("."):
        ext = "." + ext
    taken = extra_taken if extra_taken else set()
    first_page = f"{base}_0001{ext}"
    if first_page not in taken and not name_taken(inbox, outbox, first_page):
        return base
    for i in range(2, max_try + 2):
        cand = f"{base}_{i}"
        first = f"{cand}_0001{ext}"
        if first not in taken and not name_taken(inbox, outbox, first):
            return cand
    raise RuntimeError(f"无法为 {base} 生成可用页名 base（尝试 {max_try} 次）")
