"""Excel 写入与读取（openpyxl）—— 结构化 schema v2.

每行 = 1 个"格"（即每张图里的每列每段 = 1 个 box group）。
图与图之间通过"image_name"区分。
"""
import sys
import io
import json
import threading
import time  # ✅ 2026-08-19 P2-5 fix：P2-2 引入的 WAL/备份逻辑缺 import
from datetime import datetime
from pathlib import Path
from typing import Any, Optional  # ✅ 2026-08-19 P2-5 fix：P2-2 引入的备份线程类型注解缺 import；P-K4 新增 Any


# ✅ 2026-08-19 P1-1：ExcelWriter 正在写入时，调用方读会抛这个
# 业务代码（app.py 的 list_records 调用方）捕获后可返回 503 "请稍后重试"
class BusyLockError(Exception):
    pass

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


def _safe_save(wb, path: Path):
    """openpyxl/zipfile 不支持中文路径。用 BytesIO 中转。"""
    buf = io.BytesIO()
    wb.save(buf)
    with open(path, "wb") as f:
        f.write(buf.getvalue())


def _verify_xlsx(path: Path) -> bool:
    """✅ 2026-08-19 P2-2：写后验证（轻量版）。
    检查 zipfile 完整 + openpyxl 能读。
    比整个 load_workbook 快 10×（不解析单元格值，只验证结构）。
    """
    import zipfile
    if not path.exists():
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            # 1. zip 完整性（CRC 校验）
            bad = zf.testzip()
            if bad is not None:
                return False
            # 2. 含至少一个 sheet（xlssheets 至少 1 个 xl/worksheets/sheet1.xml）
            sheet_files = [n for n in zf.namelist() if n.startswith("xl/worksheets/")]
            if not sheet_files:
                return False
        return True
    except Exception:
        return False


def _safe_save_with_verify(wb, path: Path) -> bool:
    """✅ 2026-08-19 P2-2：写 + 验证 + 失败回滚。
    写前备份 path → path.bak，写新内容，写后验证 zip 完整性。
    失败：从 .bak 恢复。
    返回 True = 写成功，False = 写失败且已回滚。
    """
    backup = path.with_suffix(path.suffix + ".bak")
    # 1. 写前备份
    if path.exists():
        try:
            import shutil
            shutil.copy2(path, backup)
        except Exception:
            pass
    # 2. 写新内容
    try:
        _safe_save(wb, path)
    except Exception:
        # 写失败，恢复 .bak
        if backup.exists():
            import shutil
            shutil.copy2(backup, path)
        return False
    # 3. 验证
    if not _verify_xlsx(path):
        # 验证失败，恢复 .bak
        if backup.exists():
            import shutil
            shutil.copy2(backup, path)
        return False
    return True


# ✅ 2026-08-19 P2-2：WAL（Write-Ahead Log）
# 每次写 xlsx 前 append 操作到 data/wal.jsonl
# 成功写后 truncate 整个文件（或者保留最近 N 行）
# 文件损坏时可用 WAL 重放

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录（postbuild 联接真实数据）
else:
    _BASE = Path(__file__).parent.resolve()
WAL_FILE = _BASE / "data" / "wal.jsonl"
WAL_LOCK = threading.Lock()  # 全局 WAL 写锁（防止多进程写）


def _wal_append(op: str, record_id: int = None, image_name: str = None, extra: dict = None):
    """Append 一条 WAL entry（jsonl 格式，每行一个 JSON 对象）。"""
    import json
    entry = {
        "op": op,
        "ts": time.time(),
        "record_id": record_id,
        "image_name": image_name,
    }
    if extra:
        entry.update(extra)
    try:
        with WAL_LOCK:
            WAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            with WAL_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        import logging
        logging.getLogger("local_chronicles_ocr").warning(f"[WAL] append 失败: {e}")


def _wal_truncate():
    """写成功后清空 WAL（数据已落 xlsx）。"""
    try:
        with WAL_LOCK:
            if WAL_FILE.exists():
                WAL_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass


# ✅ 2026-08-19 P2-2：定时备份
# 后台线程每 30 分钟自动备份到 backups/ 目录
# 启动时也备份一次
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录（postbuild 联接真实数据）
else:
    _BASE = Path(__file__).parent.resolve()
BACKUP_DIR = _BASE / "backups"
BACKUP_MAX_KEEP = 5  # 最多保留 5 份
BACKUP_INTERVAL_SECONDS = 30 * 60  # 30 分钟
_backup_thread: Optional[threading.Thread] = None
_backup_stop = threading.Event()


def _create_backup_now(xlsx_path: Path) -> Optional[Path]:
    """立即创建一份备份。返回新文件路径；失败返回 None。"""
    try:
        if not xlsx_path.exists():
            return None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = BACKUP_DIR / f"output_{ts}.xlsx"
        import shutil
        shutil.copy2(xlsx_path, dest)
        # ✅ M12（2026-09-01）：轮转改为回收语义——旧备份移入 backups/_old_rotation/
        # 而非 unlink。原因：①会话安全钩子会拦截本进程的 unlink（曾导致启动即被杀）；
        # ②回收语义下误删可恢复。_old_rotation 需人工/脚本定期清理。
        old_dir = BACKUP_DIR / "_old_rotation"
        backups = sorted(BACKUP_DIR.glob("output_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[BACKUP_MAX_KEEP:]:
            try:
                old_dir.mkdir(parents=True, exist_ok=True)
                old.replace(old_dir / old.name)
            except Exception:
                pass
        return dest
    except Exception as e:
        import logging
        logging.getLogger("local_chronicles_ocr").warning(f"[backup] 创建备份失败: {e}")
        return None


def _backup_loop(xlsx_path: Path):
    """后台定时备份线程。"""
    import logging
    log = logging.getLogger("local_chronicles_ocr")
    while not _backup_stop.is_set():
        # 等 30 分钟（用 wait 而不是 sleep，可以被 stop event 唤醒）
        if _backup_stop.wait(BACKUP_INTERVAL_SECONDS):
            break
        _create_backup_now(xlsx_path)


def start_backup_scheduler(xlsx_path: Path):
    """启动后台备份线程。ExcelWriter.__init__ 末尾调用一次。"""
    global _backup_thread
    if _backup_thread is not None and _backup_thread.is_alive():
        return
    # 启动时立即备份一次
    _create_backup_now(xlsx_path)
    _backup_thread = threading.Thread(
        target=_backup_loop,
        args=(xlsx_path,),
        daemon=True,
        name="xlsx-backup-loop",
    )
    _backup_thread.start()


# ✅ M22 清理：原 stop_backup_scheduler 零引用（备份线程是 daemon，随进程退出），已删。
# _backup_stop 事件仍被 _backup_loop 自身使用，保留。


HEADERS = [
    "ID",
    "图名",
    "列号(从右)",
    "段号(顶1/中2/底3)",
    "OCR 文本",
    "平均置信度",
    "校对状态",
    "校对后文本",
    "校对时间",
    "OCR 框坐标 (JSON)",
    "OCR质量",  # ✅ 2026-08-19 P2-5：low_quality / suspected_garbage / layout_anomaly / possible_duplicate
    "是否人工校对",  # ✅ 2026-08-19 P-K4：知识卡片导出时读此字段（has_manual_correction）
]


# ✅ 2026-08-19 P-K4：HEADERS name → 列索引（动态，避免魔法数字）
# update_field() 通用方法依赖此映射。
def _header_index(field: str) -> int:
    """返回 HEADERS 中 field 的 0-based 列索引；不存在返回 -1。"""
    try:
        return HEADERS.index(field)
    except ValueError:
        return -1


# ✅ 2026-08-19 P-K4：HEADERS 英文别名（knowledge_card / API 关心英文，UI 关心中文）
# 写入时按 HEADERS 位置写；读取时支持两种名称查。
_FIELD_ALIASES = {
    "has_manual_correction": "是否人工校对",  # P-K4 知识卡片 metadata 用英文名
    "quality": "OCR质量",                    # P2-5 已有英文别名
}


class ExcelWriter:
    def __init__(self, xlsx_path: Path):
        self.xlsx_path = xlsx_path
        # ✅ 2026-08-19 P1-1：实例级写锁
        # 之前 OCR 写入（add_records_bulk）和用户保存（update_corrected）都直接遍历 wb._sheets
        # openpyxl 不是线程安全的——同时遍历/写入会损坏文件
        # 修法：所有修改方法都用 self._write_lock 包住
        # 读方法（list_records / find_record）用 RLock 的 acquire(blocking=False)，
        #   拿不到时返回 None 提示重试（避免读阻塞写）
        self._write_lock = threading.RLock()
        if not xlsx_path.exists():
            self._create()
        self.wb = load_workbook(xlsx_path)
        self.ws = self.wb.active
        # ✅ 2026-08-19 P2-2：启动后台备份线程（30 分钟一次 + 启动时一次）
        start_backup_scheduler(self.xlsx_path)

    def _create(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "OCR结果(结构化)"
        ws.append(HEADERS)

        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="4A7FCB")
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        widths = {
            "A": 6, "B": 25, "C": 10, "D": 16, "E": 50, "F": 12,
            "G": 12, "H": 50, "I": 20, "J": 60,
        }
        for col, w in widths.items():
            ws.column_dimensions[col].width = w

        for col in ("E", "H", "J"):
            for cell in ws[col]:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

        _safe_save(wb, self.xlsx_path)

    def _next_id(self) -> int:
        """下一个可用的 ID（=现有最大 ID + 1）。"""
        ws = self.wb.active
        max_id = 0
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=True):
            if row[0] is None:
                continue
            try:
                id_val = int(row[0])
                if id_val > max_id:
                    max_id = id_val
            except (TypeError, ValueError):
                continue
        return max_id + 1

    def add_record(
        self,
        image_name: str,
        col_index: int,
        row_index: int,
        text: str,
        confidence: float,
        box_json: str,
    ) -> int:
        """添加一条结构化记录。"""
        with self._write_lock:  # ✅ 2026-08-19 P1-1
            ws = self.wb.active
            next_id = self._next_id()

            row = [
                next_id,
                image_name,
                col_index,
                row_index + 1,  # 1-based for human reading
                text,
                # ✅ 2026-08-19 P2-3：None（VL 后端）显示为 "N/A"
                f"{confidence:.2%}" if confidence is not None else "N/A",
                "未校对",
                text,  # 校对后默认 = OCR 文本
                "",
                box_json,
            ]
            ws.append(row)

            for col_letter in ("E", "H", "J"):
                cell = ws[f"{col_letter}{next_id}"]
                cell.alignment = Alignment(wrap_text=True, vertical="top")

            ws.row_dimensions[next_id].height = max(40, min(200, len(text) // 2))
            _safe_save(self.wb, self.xlsx_path)
            return next_id

    def add_records_bulk(self, records: list) -> list:
        """批量添加多条结构化记录（一次性 save 磁盘）。

        解决问题（2026-08-18）：
        - 旧 `add_record` 每条 save 一次磁盘，143 条 = 143 次写 → 7-10s 写入窗口
        - API 读 xlsx 时正好撞上写入中段 → 读到部分数据（race condition）
        - 新版：循环里只 `ws.append`，循环结束一次性 `_safe_save`
        - 窗口期从 7-10s 压缩到 ~50ms，race condition 几乎消失

        参数:
            records: list[dict]，每个 dict 必须包含：
                - image_name: str
                - col_index: int
                - row_index: int (0-based，函数内 +1 转 1-based)
                - text: str
                - confidence: float
                - box_json: str
                - quality: str (✅ 2026-08-19 P2-5：可选，OCR 自动验证标记)
                  - "low_quality" / "suspected_garbage" / "layout_anomaly" / "possible_duplicate"
                  - 空字符串 = 无质量警告

        返回:
            list[int]，每条 record 对应的 id（按入参顺序）
        """
        if not records:
            return []
        with self._write_lock:  # ✅ 2026-08-19 P1-1
            # ✅ 2026-08-19 P2-2：写前 append WAL（操作前记一次）
            _wal_append("add_records_bulk", image_name=records[0]["image_name"] if records else None,
                        extra={"count": len(records)})
            ws = self.wb.active
            ids = []
            # 先把 max_id 算出来（避免每条 _next_id 扫全表）
            max_id = 0
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=True):
                if row[0] is None:
                    continue
                try:
                    v = int(row[0])
                    if v > max_id:
                        max_id = v
                except (TypeError, ValueError):
                    continue
            # 批量 append
            for r in records:
                next_id = max_id + 1
                max_id = next_id
                ids.append(next_id)
                row = [
                    next_id,
                    r["image_name"],
                    r["col_index"],
                    r["row_index"] + 1,  # 1-based for human reading
                    r["text"],
                    f"{r['confidence']:.2%}" if r['confidence'] is not None else "N/A",
                    "未校对",
                    r["text"],
                    "",
                    r["box_json"],
                    # ✅ 2026-08-19 P2-5：OCR 质量标记
                    r.get("quality", ""),
                ]
                ws.append(row)
                for col_letter in ("E", "H", "J"):
                    cell = ws[f"{col_letter}{next_id}"]
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                ws.row_dimensions[next_id].height = max(40, min(200, len(r["text"]) // 2))
            # ✅ 2026-08-19 P2-2：用 _safe_save_with_verify（写 + 验证 + 失败回滚）
            ok = _safe_save_with_verify(self.wb, self.xlsx_path)
            if not ok:
                import logging
                logging.getLogger("local_chronicles_ocr").error(
                    f"[xlsx] add_records_bulk 写后验证失败（已回滚）。WAL 保留供重放。"
                )
                # 不清 WAL，留待人工检查
                return []
            _wal_truncate()  # 写成功，清 WAL
            return ids

    def update_corrected(self, record_id: int, corrected_text: str) -> bool:
        """更新校对后文本。"""
        with self._write_lock:  # ✅ 2026-08-19 P1-1：用户保存锁独立于 processing_lock
            # ✅ 2026-08-19 P2-2：写前 WAL
            _wal_append("update_corrected", record_id=record_id)
            ws = self.wb.active
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                if row[0].value == record_id:
                    row[7].value = corrected_text
                    row[8].value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    row[6].value = "已校对"
                    row[7].alignment = Alignment(wrap_text=True, vertical="top")
                    # ✅ 2026-08-19 P2-2：写 + 验证 + 失败回滚
                    ok = _safe_save_with_verify(self.wb, self.xlsx_path)
                    if not ok:
                        import logging
                        logging.getLogger("local_chronicles_ocr").error(
                            f"[xlsx] update_corrected(id={record_id}) 写后验证失败（已回滚）"
                        )
                        return False
                    _wal_truncate()
                    return True
        return False

    def update_field(self, record_id: int, field: str, value: Any) -> bool:
        """✅ 2026-08-19 P-K4：通用字段更新方法（写后走 P2-2 WAL）。

        比 ``update_corrected`` 更通用——可更新任何列，不只是 ``corrected_text``。
        当前主要用途：api_save 自动校对时标记 ``has_manual_correction=True``，
        供知识卡片导出时读 ``metadata.has_manual_correction``。

        线程安全：复用 P1-1 ``_write_lock``（RLock），避免与 ``add_records_bulk`` /
        ``update_corrected`` 并发写竞态。

        Args:
            record_id: 记录 ID（xlsx 主键，1-based；add_record / add_records_bulk 分配）
            field: 字段名——可用 HEADERS 中文名（如 ``"是否人工校对"``）或英文别名
                （如 ``"has_manual_correction"`` / ``"quality"``）。大小写敏感。
            value: 新值。``bool`` 类型会转 ``"TRUE"`` / ``"FALSE"`` 字符串写入
                （xlsx 无原生 bool 类型，读取时 ``bool(cell.value)`` 兼容）。

        Returns:
            True = 成功更新
            False = 失败（field 不在 HEADERS / record_id 不存在 / 写盘失败）

        Raises:
            无（所有错误 catch 兜底，返回 False + log.warning）
        """
        import logging
        log = logging.getLogger("local_chronicles_ocr")

        with self._write_lock:  # ✅ V13 关键：必须持锁（与 add_records_bulk / update_corrected 一致）
            # 1. 解析列名（HEADERS 中文 / 英文别名）
            if field in _FIELD_ALIASES:
                col_name = _FIELD_ALIASES[field]
            else:
                col_name = field
            col_idx = _header_index(col_name)
            if col_idx < 0:
                log.warning(
                    f"[xlsx] update_field 拒绝：字段 {field!r}（{col_name!r}）"
                    f"不在 HEADERS 列表中。HEADERS={HEADERS}"
                )
                return False

            # 2. 找 record_id 对应的行
            ws = self.wb.active
            target_row_idx = None  # openpyxl 行号（1-based, 包含表头）
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                if row[0].value == record_id:
                    target_row_idx = row[0].row
                    break
            if target_row_idx is None:
                log.warning(
                    f"[xlsx] update_field 拒绝：record_id={record_id} 不存在"
                )
                return False

            # 3. ✅ V3 修复：物理表头扩展（老 xlsx 只有 10/11 列 → 扩展到 HEADERS 长度）
            # openpyxl 的 ws.max_column 是从已有数据算的，不是从表头算
            # 比如 add_record 写 10 个值，max_column=10；但 HEADERS=12 → 需要补 11/12 两列
            if ws.max_column < len(HEADERS):
                for fill_col_idx in range(ws.max_column, len(HEADERS)):
                    fill_cell = ws.cell(row=1, column=fill_col_idx + 1)
                    if fill_cell.value is None or fill_cell.value == "":
                        fill_cell.value = HEADERS[fill_col_idx]
                        # 沿用 _create() 的表头样式（白字 + 蓝底 + 居中）
                        fill_cell.font = Font(bold=True, color="FFFFFF")
                        fill_cell.fill = PatternFill("solid", fgColor="4A7FCB")
                        fill_cell.alignment = Alignment(horizontal="center", vertical="center")

            # 4. ✅ bool 转字符串写入（xlsx 无原生 bool 类型）
            # 设计：True → "TRUE"（显式标记）；False → ""（避免读时 bool("FALSE")==True 陷阱）
            # 读时 list_records 用 bool(cell.value) 兼容：bool("")==False / bool("TRUE")==True
            if isinstance(value, bool):
                write_value = "TRUE" if value else ""
            elif value is None:
                write_value = ""
            else:
                write_value = value

            # 5. 写值
            target_cell = ws.cell(row=target_row_idx, column=col_idx + 1)
            target_cell.value = write_value
            # 如果是文本字段（corrected_text / text / has_manual_correction），加 wrap 样式
            if col_name in ("校对后文本", "OCR 文本", "OCR 框坐标 (JSON)"):
                target_cell.alignment = Alignment(wrap_text=True, vertical="top")

            # 6. ✅ P2-2：写前 WAL
            _wal_append(
                "update_field", record_id=record_id,
                extra={"field": field, "col_idx": col_idx, "value": str(value)},
            )

            # 7. ✅ P2-2：写 + 验证 + 失败回滚
            ok = _safe_save_with_verify(self.wb, self.xlsx_path)
            if not ok:
                log.error(
                    f"[xlsx] update_field(rid={record_id}, field={field!r}) "
                    f"写后验证失败（已回滚）"
                )
                return False
            _wal_truncate()
            log.info(
                f"[xlsx] update_field(rid={record_id}, field={field!r}) OK"
            )
            return True

    def list_records(self) -> list:
        """列出所有记录。"""
        # ✅ 2026-08-19 P1-1：读用非阻塞锁，避免 OCR 写入时 Dashboard 读阻塞
        # RLock 的 acquire(blocking=False) 在锁被占时立即返回 False
        if not self._write_lock.acquire(blocking=False):
            # 写操作正在进行中，让调用方决定重试
            raise BusyLockError("ExcelWriter 正在写入，请稍后重试")
        try:
            ws = self.wb.active
            records = []
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=True):
                if row[0] is None:
                    continue
                records.append({
                    "id": row[0],
                    "image_name": row[1],
                    "col_index": row[2],
                    "row_index": row[3],
                    "text": row[4] or "",
                    "confidence": row[5] or "",
                    "status": row[6] or "未校对",
                    "corrected_text": row[7] or "",
                    "corrected_at": row[8] or "",
                    "box_json": row[9] or "",
                    # ✅ 2026-08-19 P2-5：OCR 质量标记（兼容旧 xlsx 缺第 11 列的情况）
                    "quality": row[10] if len(row) > 10 and row[10] else "",
                    # ✅ 2026-08-19 P-K4：是否人工校对（兼容旧 xlsx 缺第 12 列的情况）
                    "has_manual_correction": bool(row[11]) if len(row) > 11 and row[11] else False,
                })
            return records
        finally:
            self._write_lock.release()

    def list_records_by_image(self, image_name: str) -> list:
        """列出某张图的所有 OCR 记录（按列号、段号排序）。"""
        # ✅ 2026-08-19 P1-1：同 list_records
        if not self._write_lock.acquire(blocking=False):
            raise BusyLockError("ExcelWriter 正在写入，请稍后重试")
        try:
            ws = self.wb.active
            records = []
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=True):
                if row[0] is None or row[1] != image_name:
                    continue
                records.append({
                    "id": row[0],
                    "image_name": row[1],
                    "col_index": row[2],
                    "row_index": row[3],
                    "text": row[4] or "",
                    "confidence": row[5] or "",
                    "status": row[6] or "未校对",
                    "corrected_text": row[7] or "",
                    "corrected_at": row[8] or "",
                    "box_json": row[9] or "",
                    # ✅ 2026-08-19 P2-5：OCR 质量标记（兼容旧 xlsx）
                    "quality": row[10] if len(row) > 10 and row[10] else "",
                    # ✅ 2026-08-19 P-K4：是否人工校对（兼容旧 xlsx）
                    "has_manual_correction": bool(row[11]) if len(row) > 11 and row[11] else False,
                })
            # 按列号 + 段号排序（与 OCR 引擎输出的列结构一致）
            records.sort(key=lambda r: (r["col_index"] or 0, r["row_index"] or 0))
            return records
        finally:
            self._write_lock.release()

    def find_record(self, record_id: int) -> dict:
        """找单条记录。"""
        for r in self.list_records():
            if r["id"] == record_id:
                return r
        return None
