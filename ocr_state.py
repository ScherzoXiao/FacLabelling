"""OCR 状态管理（内存 dict + 持久化到 data/ocr_state.json）。

每个 image 的状态：
  - "unknown": 还没 OCR 过（默认）
  - "running": OCR 正在识别
  - "done": OCR 完成
  - "failed": OCR 失败

字段：
  status: str
  count: int            已识别 box 数
  total_estimated: int  预估总数（百度云 API 没回 → 用上次的量 + 0；运行时为 0）
  started_at: float     epoch
  finished_at: float    epoch（完成/失败时填）
  error: str            失败时的错误信息
  backend: str          "baidu" / "paddleocr_vl"（旧记录可能含已移除的 "paddleocr"）
  model: str            "accurate" / "ancient" / "PaddleOCR-VL-1.6"

线程安全：所有读写都加 self._lock。
持久化：✅ 2026-08-19 P2-4
- 每次状态变更写 data/ocr_state.json（原子写：tmp + os.replace）
- 服务启动时 restore() 恢复未完成的任务（status=running 的标记为 failed）
- running 状态不持久化（重启后默认 failed 触发重新 OCR）
"""
import sys
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional


# ✅ 2026-08-19 P2-4：状态文件路径
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录（postbuild 联接真实数据）
else:
    _BASE = Path(__file__).parent.resolve()
DEFAULT_STATE_FILE = _BASE / "data" / "ocr_state.json"


class OcrState:
    """单实例 OCR 状态机（process 内全局共享）。"""

    def __init__(self, state_file: Optional[Path] = None):
        self._states: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()  # 文件写串行化
        self._state_file = state_file or DEFAULT_STATE_FILE
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        # 启动时恢复
        self.restore()

    # ========== 持久化（2026-08-19 P2-4）==========

    def _persist(self):
        """把当前状态写盘（线程安全，原子写）。调用方已持 self._lock。"""
        # 简化：只持久化 done/failed 状态（running 状态不持久化，重启后会过期）
        # 这避免重启时大量 running 状态污染
        persistable = {
            k: v for k, v in self._states.items()
            if v.get("status") in ("done", "failed") and v.get("finished_at")
        }
        try:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(persistable, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_file)
        except Exception as e:
            # 持久化失败不应阻塞业务
            import logging
            logging.getLogger("local_chronicles_ocr").warning(
                f"[ocr_state] 持久化失败: {e}"
            )

    def restore(self):
        """服务启动时调用。从 data/ocr_state.json 恢复 done/failed 状态。

        running 状态会自然丢失（OCR 进程死了，但状态还在内存）—— 不会从盘恢复。
        """
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            with self._lock:
                self._states.update(data)
            import logging
            logging.getLogger("local_chronicles_ocr").info(
                f"[ocr_state] 恢复 {len(data)} 条历史状态（done/failed）"
            )
        except Exception as e:
            import logging
            logging.getLogger("local_chronicles_ocr").warning(
                f"[ocr_state] 恢复失败: {e}"
            )

    # ========== 状态变更 ==========

    def register(self, image_name: str, backend: str = "", model: str = ""):
        """标记某张图开始 OCR（覆盖之前的状态）。"""
        with self._lock:
            self._states[image_name] = {
                "status": "running",
                "count": 0,
                "total_estimated": 0,
                "started_at": time.time(),
                "finished_at": None,
                "error": None,
                "backend": backend,
                "model": model,
            }
            # running 状态不持久化（避免重启时污染）

    def update(self, image_name: str, count: int = None, total_estimated: int = None):
        """更新运行中进度（运行时多次调用）。"""
        with self._lock:
            s = self._states.get(image_name)
            if not s or s["status"] != "running":
                return
            if count is not None:
                s["count"] = count
            if total_estimated is not None:
                s["total_estimated"] = total_estimated

    def done(self, image_name: str, count: int):
        """标记完成。"""
        with self._lock:
            s = self._states.get(image_name)
            if not s:
                s = self._states[image_name] = {}
            s["status"] = "done"
            s["count"] = count
            s["total_estimated"] = count
            s["finished_at"] = time.time()
            s["error"] = None
            self._persist()

    def failed(self, image_name: str, error: str):
        """标记失败。"""
        with self._lock:
            s = self._states.get(image_name)
            if not s:
                s = self._states[image_name] = {}
            s["status"] = "failed"
            s["finished_at"] = time.time()
            s["error"] = error
            self._persist()

    def get(self, image_name: str) -> dict:
        """读某张图的状态（不存在返回 unknown 默认值）。"""
        with self._lock:
            s = self._states.get(image_name)
            if not s:
                return {
                    "status": "unknown",
                    "count": 0,
                    "total_estimated": 0,
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "backend": "",
                    "model": "",
                }
            return dict(s)  # 浅拷贝

    def all_running(self) -> list[str]:
        """返回所有正在 OCR 的图（dashboard 用）。"""
        with self._lock:
            return [k for k, v in self._states.items() if v["status"] == "running"]

    def cleanup_finished(self, max_age_seconds: float = 3600.0):
        """清理过期的 done/failed 状态（避免 dict 无限增长）。默认 1 小时。"""
        with self._lock:
            now = time.time()
            drop = []
            for k, v in self._states.items():
                if v["status"] in ("done", "failed") and v["finished_at"]:
                    if now - v["finished_at"] > max_age_seconds:
                        drop.append(k)
            for k in drop:
                del self._states[k]
            if drop:
                self._persist()  # ✅ 2026-08-19 P2-4：清理后也持久化


# ========== 模块级单例 ==========
ocr_state = OcrState()
