"""batch_import.py — 批量导入模块（2026-09-02）

场景：用户已准备好一文件夹的文献图片 / 几份 PDF，不想一张张截图。
功能：
1. 扫描一个文件夹（递归）或多份文件 → 可 OCR 材料清单（图片 + PDF）
2. PDF 用 PyMuPDF 逐页转 PNG（可选依赖，缺装时明确报错降级）
3. 复制到 inbox/（watchdog 自动触发 OCR，复用现有管道）：
   - 文件名遵循命名规范 v2（2026-09-02）：保留用户原文件名（sanitize + 冲突递增）；PDF 页 = 原文件名_NNNN
   - PDF 页时间戳逐秒递增 → 字典序 = 页序（与 P-K8 "时间在前"设计哲学一致）
   - 像素 hash 批内去重 + 持久化 hash 缓存（data/batch_imports/hashes.json，
     hash8 → image_name）跨批去重 —— 与导入时刻时间戳解耦：同一内容
     两次导入即使跨秒启动（文件名不同）也能识别为重复
4. 项目联动（用户核心诉求）：
   - **预写 assignments 在复制之前** —— process_image 的 auto-assign
     （active project fallback）检查 get_image_project() 非空即跳过，
     不会把批量图吸进"当前激活栏目"
   - project_mode: single（指定项目）/ subfolder（按一级子文件夹分组，
     同名项目复用、缺则自动创建）/ none（不分配，后续手动归类）
   - none 模式下批量图登记到豁免集合，process_image 也不会吸进 active
5. 知识库联动：
   - 批量任务在途时 app.py 的 _auto_export_knowledge_cards 跳过单图导出
     （避免 N 张图 = N 次全项目重导出风暴），登记 pending 项目
   - OCR 消化完成（全部离开 inbox）后统一导出涉及项目 + 失效 RAG 缓存

状态持久化：data/batch_imports/<task_id>.json（仿 export_tasks per-task 模式）。
上传临时目录：data/batch_import_uploads/<task_id>/（任务成功后清理，
启动时清理 24h 前的残留）。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

import image_naming
import data_io
from config import BASE_DIR, FAILED_DIR, IMAGE_EXTS, INBOX, OUTBOX

log = logging.getLogger("local_chronicles_ocr")

# ========== 可 OCR 材料定义 ==========
PDF_EXTS = frozenset({".pdf"})

# 任务状态目录 + 上传临时目录（模块级常量，测试可 monkeypatch）
BATCH_IMPORTS_DIR = BASE_DIR / "data" / "batch_imports"
UPLOADS_DIR = BASE_DIR / "data" / "batch_import_uploads"

# subfolder 模式自动建项目的轮转色板（包豪斯 8 色 token 风格）
PROJECT_COLOR_PALETTE = [
    "#B85C4A", "#4A7FCB", "#5A7D5A", "#8A6BB5",
    "#C99A3C", "#4AA5A0", "#7D6B5A", "#A54A6E",
]

# 默认参数
DEFAULT_PDF_DPI = 200          # A4 @200dpi ≈ 1654×2339，OCR 足够
DEFAULT_OCR_WAIT_TIMEOUT = 1800  # 等 watchdog 消化 inbox 的上限（秒）
OCR_POLL_INTERVAL = 2.0

VALID_PROJECT_MODES = frozenset({"single", "subfolder", "none"})

# ========== 任务状态（内存 + JSON 持久化，per-task 文件防多线程竞态）==========
_task_lock = threading.RLock()
_tasks: dict[str, dict] = {}

# ========== 批量感知状态（供 app.py 查询）==========
_inflight_lock = threading.Lock()
_ocr_batches_inflight = 0                 # 正在跑的批量导入任务数（>0 → 单图自动导出跳过）
_deferred_projects: set[str] = set()      # 批量期间被跳过的自动导出项目（任务结束统一导）
_batch_image_names: set[str] = set()      # 本进程内批量导入的图名（auto-assign 豁免）

# ========== OCR 批量预取钩子（✅ 2026-09-14 P-OPT-6）==========
# 设计：批量导入把整批图写进 inbox 后，**先整体提交** OCR 任务（让服务端并行跑），
# watchdog 逐张处理时命中「已提交」条目 ⇒ 总耗时 ΣBᵢ → max(Bᵢ)。
# 为什么不直接 import app：`app` 依赖本模块（循环导入）。改由 app.py 在 init_ocr 时
# **注入**（`set_prefetch_hook`）。未注入 / 钩子返回 0 ⇒ 行为与 P-OPT-6 之前完全一致。
_prefetch_hook = None                     # Optional[Callable[[List[str]], int]]


def set_prefetch_hook(fn) -> None:
    """注入 OCR 预取函数（签名 `fn(image_paths: List[str]) -> int`）。

    幂等：重复注入只用最后一个；传 `None` 可卸载（测试用）。
    """
    global _prefetch_hook
    _prefetch_hook = fn

# ========== 跨批去重：持久化 hash 缓存（hash8 → image_name）==========
# 设计动机：P-K8 文件名含导入时刻时间戳，同一内容两次导入跨秒启动会生成
# 不同名字，"同名即同内容"的旧假设失效 → 去重键必须是内容 hash 本身。
_hash_cache_lock = threading.RLock()
_hash_cache: Optional[dict[str, str]] = None   # 惰性单例（None = 未加载）


# =====================================================================
# 路径工具（测试 monkeypatch 友好：全部经过模块常量间接引用）
# =====================================================================
def _inbox() -> Path:
    return Path(INBOX)


def _outbox() -> Path:
    return Path(OUTBOX)


def _failed() -> Path:
    return Path(FAILED_DIR)


def _imports_dir() -> Path:
    return Path(BATCH_IMPORTS_DIR)


def _uploads_dir() -> Path:
    return Path(UPLOADS_DIR)


# =====================================================================
# 像素 hash（与 clipboard_watcher._pixel_hash 同思路：跨编码稳定）
# =====================================================================
def pixel_hash(data: bytes) -> Optional[str]:
    """计算像素级 hash（RGB 像素 + 宽高）。解析失败退回字节 MD5。"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        h = hashlib.md5()
        h.update(f"{img.width}x{img.height}".encode("ascii"))
        h.update(img.tobytes())
        return h.hexdigest()
    except Exception:
        return hashlib.md5(data).hexdigest()


# =====================================================================
# PDF 支持（PyMuPDF 可选依赖）
# =====================================================================
def pdf_available() -> bool:
    """PyMuPDF（fitz）是否已安装。"""
    try:
        import fitz  # noqa: F401
        return True
    except ImportError:
        return False


def pdf_page_count(pdf_path: Path) -> Optional[int]:
    """读 PDF 页数。失败 / 未装依赖返回 None。"""
    if not pdf_available():
        return None
    try:
        import fitz
        with fitz.open(str(pdf_path)) as doc:
            return doc.page_count
    except Exception as e:
        log.warning(f"[batch_import] 读 PDF 页数失败 {pdf_path.name}: {e}")
        return None


def iter_pdf_pages(pdf_path: Path, dpi: int = DEFAULT_PDF_DPI) -> Iterator[bytes]:
    """逐页渲染 PDF 为 PNG bytes（惰性生成器，大 PDF 不整本进内存）。"""
    import fitz
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=mat)
            yield pix.tobytes("png")


# =====================================================================
# 命名（2026-09-02 命名规范 v2：保留用户原文件名）
# =====================================================================
# ✅ v2（2026-09-02）：图片保留用户原文件名（sanitize + 冲突递增），
# PDF 多页 = `原文件名_NNNN`（页码 4 位，如 `1908年商务官报26期_0012.png`）。
# 旧 `_new_image_name`（P-K8 v1 时间戳命名）已移除；命名工具统一在 image_naming.py。


def _dedup_hash(data: bytes) -> str:
    """去重键 + 命名 hash（8hex）。"""
    return (pixel_hash(data) or hashlib.md5(data).hexdigest())[:8]


# =====================================================================
# 持久化 hash 缓存（跨批去重，与导入时刻时间戳解耦）
# =====================================================================
def _hash_cache_file() -> Path:
    return _imports_dir() / "hashes.json"


def _load_hash_cache() -> dict[str, str]:
    """惰性加载 hash 缓存（hash8 → 首次导入的 image_name）。文件缺失/损坏 → 空 dict。"""
    global _hash_cache
    with _hash_cache_lock:
        if _hash_cache is None:
            data: dict[str, str] = {}
            try:
                f = _hash_cache_file()
                if f.exists():
                    raw = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = {str(k): str(v) for k, v in raw.items()}
            except Exception as e:
                log.warning(f"[batch_import] hash 缓存加载失败（按空处理）: {e}")
            _hash_cache = data
    return _hash_cache


def _save_hash_cache(cache: dict[str, str]) -> None:
    """原子持久化（**单一实现** `data_io`：tmp + fsync + os.replace）。"""
    data_io.atomic_write_json(_hash_cache_file(), cache, indent=None)


def _hash_seen(h: str) -> Optional[str]:
    """hash 曾导入且对应文件仍在 inbox/outbox → 返回已存在的 image_name。

    记录指向的文件已被删除（如用户清理 outbox）→ 视为未见（允许重导）。
    """
    cache = _load_hash_cache()
    name = cache.get(h)
    if not name:
        return None
    if (_inbox() / name).exists() or (_outbox() / name).exists():
        return name
    return None


def _register_hashes(pairs: List[tuple[str, str]]) -> None:
    """复制成功后登记 hash8 → image_name（同 hash 重导时覆盖为最新名）。"""
    if not pairs:
        return
    with _hash_cache_lock:
        cache = _load_hash_cache()
        for h, name in pairs:
            cache[h] = name
        _save_hash_cache(cache)


# =====================================================================
# 扫描
# =====================================================================
def scan_paths(paths: List[str], recursive: bool = True) -> dict:
    """展开文件夹 / 文件路径 → 可 OCR 材料清单。

    Returns:
        {
          "items": [
            {"path": 绝对路径, "name": 文件名, "ext": 后缀, "size": int,
             "mtime": float, "type": "image"|"pdf",
             "subfolder": 一级子文件夹名（根级文件为 ""）,
             "pdf_pages": int|None, "pdf_ready": bool}
          ],
          "summary": {"images": n, "pdfs": n, "pdf_pages": n,
                      "unsupported": n, "total_estimated": n,
                      "pdf_supported": bool}
        }
    """
    items: list[dict] = []
    unsupported = 0
    roots: list[Path] = []

    for raw in paths:
        if not raw or not str(raw).strip():
            continue
        p = Path(str(raw).strip().strip('"'))
        if not p.exists():
            continue
        roots.append(p)

    def _add_file(p: Path, subfolder: str):
        nonlocal unsupported
        ext = p.suffix.lower()
        try:
            stat = p.stat()
        except OSError:
            return
        if ext in IMAGE_EXTS:
            items.append({
                "path": str(p), "name": p.name, "ext": ext,
                "size": stat.st_size, "mtime": stat.st_mtime,
                "type": "image", "subfolder": subfolder,
                "pdf_pages": None, "pdf_ready": True,
            })
        elif ext in PDF_EXTS:
            pages = pdf_page_count(p)
            items.append({
                "path": str(p), "name": p.name, "ext": ext,
                "size": stat.st_size, "mtime": stat.st_mtime,
                "type": "pdf", "subfolder": subfolder,
                "pdf_pages": pages,
                "pdf_ready": pdf_available(),
            })
        else:
            unsupported += 1

    for root in roots:
        if root.is_file():
            _add_file(root, "")
        elif root.is_dir():
            if recursive:
                # 全部图片 + PDF（含子目录），subfolder = 相对根的第一级目录名
                for p in sorted(root.rglob("*")):
                    if not p.is_file():
                        continue
                    if p.suffix.lower() not in IMAGE_EXTS and p.suffix.lower() not in PDF_EXTS:
                        # rglob 无法按后缀预过滤，计数在 _add_file 里做
                        unsupported += 1
                        continue
                    try:
                        rel = p.relative_to(root)
                    except ValueError:
                        continue
                    sub = rel.parts[0] if len(rel.parts) > 1 else ""
                    _add_file(p, sub)
            else:
                for p in sorted(root.iterdir()):
                    if p.is_file():
                        _add_file(p, "")

    # 排序：subfolder → name（用户预览稳定）
    items.sort(key=lambda x: (x["subfolder"], x["name"]))

    n_images = sum(1 for i in items if i["type"] == "image")
    n_pdfs = sum(1 for i in items if i["type"] == "pdf")
    pdf_pages_total = sum(i["pdf_pages"] or 0 for i in items if i["type"] == "pdf")
    return {
        "items": items,
        "summary": {
            "images": n_images,
            "pdfs": n_pdfs,
            "pdf_pages": pdf_pages_total,
            "unsupported": unsupported,
            "total_estimated": n_images + pdf_pages_total,
            "pdf_supported": pdf_available(),
        },
    }


# =====================================================================
# 任务状态管理
# =====================================================================
def _task_file(task_id: str) -> Path:
    return _imports_dir() / f"{task_id}.json"


def _save_task(task: dict) -> None:
    """per-task JSON 原子写（**单一实现** `data_io`；防多任务并发写同一文件竞态）。"""
    try:
        data_io.atomic_write_json(_task_file(task["task_id"]), task)
    except Exception as e:
        log.warning(f"[batch_import] 任务状态写盘失败 {task.get('task_id')}: {e}")


def _update_task(task_id: str, **fields) -> dict:
    """更新任务字段（内存 + 磁盘 + updated_at）。"""
    with _task_lock:
        task = _tasks.get(task_id)
        if task is None:
            # 磁盘恢复（服务重启后轮询旧任务）
            f = _task_file(task_id)
            if f.exists():
                try:
                    task = json.loads(f.read_text(encoding="utf-8"))
                    _tasks[task_id] = task
                except Exception:
                    return {}
            else:
                return {}
        task.update(fields)
        task["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _save_task(task)
        return dict(task)


def get_task(task_id: str) -> Optional[dict]:
    with _task_lock:
        task = _tasks.get(task_id)
        if task is not None:
            return dict(task)
    # 磁盘兜底
    f = _task_file(task_id)
    if f.exists():
        try:
            with _task_lock:
                task = json.loads(f.read_text(encoding="utf-8"))
                _tasks[task_id] = task
                return dict(task)
        except Exception:
            return None
    return None


def list_tasks(limit: int = 10) -> List[dict]:
    """最近 N 个任务（按 created_at 倒序，内存 + 磁盘合并）。"""
    merged: dict[str, dict] = {}
    if _imports_dir().exists():
        for f in _imports_dir().glob("*.json"):
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(t, dict) and t.get("task_id"):
                    merged[t["task_id"]] = t
            except Exception:
                continue
    with _task_lock:
        for tid, t in _tasks.items():
            merged[tid] = t
    tasks = sorted(merged.values(), key=lambda t: t.get("created_at", ""), reverse=True)
    return [dict(t) for t in tasks[:limit]]


# =====================================================================
# 批量感知接口（app.py 的 process_image / _auto_export_knowledge_cards 调用）
# =====================================================================
def is_ocr_batch_inflight() -> bool:
    """是否有批量导入任务在等 OCR 消化。True → 单图自动导出应跳过。"""
    with _inflight_lock:
        return _ocr_batches_inflight > 0


def defer_project_export(project_id: str) -> None:
    """批量期间被跳过的自动导出项目 → 登记，任务结束统一导出。"""
    if not project_id:
        return
    with _inflight_lock:
        _deferred_projects.add(project_id)


def pop_deferred_projects() -> set:
    """取走并清空 pending 项目集合（批量任务结束时调用）。"""
    with _inflight_lock:
        out = set(_deferred_projects)
        _deferred_projects.clear()
        return out


def register_batch_images(names: List[str]) -> None:
    """登记批量导入的图名（process_image 的 auto-assign 豁免依据）。"""
    with _inflight_lock:
        _batch_image_names.update(names)


def is_batch_image(image_name: str) -> bool:
    """该图是否来自批量导入（是 → 不吸进 active project）。"""
    with _inflight_lock:
        return image_name in _batch_image_names


# =====================================================================
# 启动清理（upload 临时目录 >24h 残留）
# =====================================================================
def cleanup_stale_uploads(max_age_hours: float = 24.0) -> int:
    """清理超龄上传临时目录，返回清理数。"""
    n = 0
    up = _uploads_dir()
    if not up.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    for d in up.iterdir():
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


# =====================================================================
# 项目归属解析
# =====================================================================
def _resolve_subfolder_projects(items: List[dict]) -> tuple[dict, list]:
    """subfolder 模式：一级子文件夹名 → 项目（同名复用，缺则创建）。

    Returns:
        (group_key → project_id 映射, 新建项目列表)
        根级文件（subfolder == ""）不在映射中 → 不分配。
    """
    import data_io
    groups = sorted({i["subfolder"] for i in items if i["subfolder"]})
    mapping: dict[str, str] = {}
    created: list[dict] = []
    existing = {p["name"]: p["id"] for p in data_io.list_projects()}
    for idx, g in enumerate(groups):
        if g in existing:
            mapping[g] = existing[g]
            continue
        proj = data_io.create_project(
            name=g,
            description=f"批量导入自动创建（子文件夹：{g}）",
            color=PROJECT_COLOR_PALETTE[idx % len(PROJECT_COLOR_PALETTE)],
        )
        mapping[g] = proj["id"]
        created.append(proj)
        existing[g] = proj["id"]
    return mapping, created


# =====================================================================
# 导入任务主体
# =====================================================================
def start_import_task(
    paths: List[str],
    project_mode: str = "single",
    project_id: Optional[str] = None,
    recursive: bool = True,
    pdf_dpi: Optional[int] = None,
    source: str = "folder",
    ocr_wait_timeout: Optional[float] = None,
    on_finished=None,
    task_id: Optional[str] = None,
) -> dict:
    """创建并启动批量导入后台任务。返回初始任务状态 dict。

    Args:
        paths: 文件夹 / 文件绝对路径列表（upload 模式 = 临时目录里的文件）
        project_mode: single | subfolder | none
        project_id: single 模式必填（已存在的项目 ID）
        recursive: 文件夹是否递归扫描
        pdf_dpi: PDF 渲染 DPI
        source: folder | upload（决定任务结束后是否清理源临时目录）
        ocr_wait_timeout: OCR 消化等待上限（秒）；None → 运行时读模块常量
            DEFAULT_OCR_WAIT_TIMEOUT（测试可 monkeypatch 后者）
        on_finished: 可选回调（task: dict）→ 测试同步等待用
        task_id: 预生成的任务 ID（upload 模式：app.py 先建临时目录存文件，
                 需要让任务 ID 与临时目录名一致）；缺省自动生成
    """
    if ocr_wait_timeout is None:
        ocr_wait_timeout = DEFAULT_OCR_WAIT_TIMEOUT
    if pdf_dpi is None:
        pdf_dpi = DEFAULT_PDF_DPI
    if project_mode not in VALID_PROJECT_MODES:
        raise ValueError(f"project_mode 必须是 {sorted(VALID_PROJECT_MODES)} 之一")
    if project_mode == "single":
        if not project_id:
            raise ValueError("single 模式必须指定 project_id")
        import data_io
        if not data_io.get_project(project_id):
            raise ValueError(f"项目不存在: {project_id}")

    if not task_id:
        task_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    task = {
        "task_id": task_id,
        "status": "scanning",
        "source": source,
        "project_mode": project_mode,
        "project_id": project_id if project_mode == "single" else None,
        "paths": [str(p) for p in paths],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "total": 0, "copied": 0,
        "skipped_duplicates": 0, "skipped_unsupported": 0,
        "image_names": [],
        "assignments": {},          # image_name → project_id（导入结果，供追溯）
        "projects_created": [],
        "ocr_done": 0, "ocr_failed": 0, "ocr_pending": 0,
        "exported_projects": [],
        "pdf_dpi": pdf_dpi,
        "errors": [],
    }
    with _task_lock:
        _tasks[task_id] = task
    _save_task(task)

    t = threading.Thread(
        target=_run_task,
        args=(task_id, paths, project_mode, project_id, recursive,
              pdf_dpi, source, ocr_wait_timeout, on_finished),
        name=f"batch-import-{task_id}",
        daemon=True,
    )
    t.start()
    return dict(task)


def _run_task(
    task_id: str,
    paths: List[str],
    project_mode: str,
    project_id: Optional[str],
    recursive: bool,
    pdf_dpi: int,
    source: str,
    ocr_wait_timeout: float,
    on_finished,
) -> None:
    """后台任务主体：scan → 预写 assignments → 复制 → 等 OCR → 统一导出。"""
    global _ocr_batches_inflight
    inbox = _inbox()
    outbox = _outbox()
    failed_dir = _failed()
    upload_root = _uploads_dir() / task_id
    try:
        # ---------- Phase 1: scanning ----------
        result = scan_paths(paths, recursive=recursive)
        items = result["items"]
        summary = result["summary"]
        _update_task(
            task_id,
            status="importing",
            total=summary["total_estimated"],
            skipped_unsupported=summary["unsupported"],
            scan_summary=summary,
        )

        if not items:
            _update_task(task_id, status="done", finished_at=datetime.now().isoformat(timespec="seconds"))
            return

        # PDF 未装依赖 → 明确报错（不静默吞）
        if not pdf_available():
            pdfs = [i for i in items if i["type"] == "pdf"]
            if pdfs:
                _update_task(
                    task_id,
                    status="failed",
                    errors=[f"发现 {len(pdfs)} 份 PDF，但未安装 PDF 转换组件（PyMuPDF）。"
                            f"请运行: pip install PyMuPDF 后重试"],
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
                return

        # ---------- Phase 2: 归属解析 + 生成计划 ----------
        # 2a. subfolder 模式：建/复用项目
        subfolder_map: dict[str, str] = {}
        created_projects: list[dict] = []
        if project_mode == "subfolder":
            subfolder_map, created_projects = _resolve_subfolder_projects(items)
            _update_task(task_id, projects_created=created_projects)

        # 2b. 逐文件生成目标（读 bytes 算 hash → 新名 → 项目归属）
        # ✅ 2026-09-02 命名 v2：保留用户原文件名（sanitize + 冲突递增避让）；
        # PDF 多页 = `原文件名_NNNN`（页码 4 位）。planned_names 防批内同名互相覆盖。
        plan: list[dict] = []          # [{new_name, src_path?, pdf_page?, project_id, kind}]
        seen_hashes: set[str] = set()  # 批内去重
        planned_names: set[str] = set()  # 批内已分配目标名（尚未落盘）
        errors: list[str] = []
        skipped_dup = 0

        for item in items:
            if item["type"] == "image":
                try:
                    data = Path(item["path"]).read_bytes()
                except Exception as e:
                    errors.append(f"读取失败 {item['name']}: {e}")
                    continue
                h = _dedup_hash(data)
                if h in seen_hashes:
                    skipped_dup += 1
                    continue
                # 跨批去重：持久化 hash 缓存（与文件名解耦）
                if _hash_seen(h):
                    skipped_dup += 1
                    continue
                orig_stem = image_naming.sanitize_stem(Path(item["name"]).stem)
                new_name = image_naming.unique_name(
                    inbox, outbox, f"{orig_stem}{item['ext']}",
                    extra_taken=planned_names,
                )
                seen_hashes.add(h)
                planned_names.add(new_name)
                plan.append({
                    "new_name": new_name, "kind": "image", "hash": h,
                    "src_path": item["path"], "pdf_page": None,
                    "project_id": _project_for(item, project_mode, project_id, subfolder_map),
                    "original_name": item["name"],
                })
            elif item["type"] == "pdf":
                # base 级冲突预解析：同批/盘上已有 `原名_0001` → 整组递增（原名_2_0001…）
                orig_stem = image_naming.sanitize_stem(Path(item["name"]).stem)
                base = image_naming.unique_base_for_pages(
                    inbox, outbox, orig_stem, ".png",
                    extra_taken=planned_names,
                )
                try:
                    for page_no, png in enumerate(iter_pdf_pages(Path(item["path"]), dpi=pdf_dpi), start=1):
                        h = _dedup_hash(png)
                        if h in seen_hashes:
                            skipped_dup += 1
                            continue
                        if _hash_seen(h):
                            skipped_dup += 1
                            continue
                        # 页名：`base_NNNN.png`（页码 4 位补零，字典序 = 页序）
                        new_name = f"{base}_{page_no:04d}.png"
                        seen_hashes.add(h)
                        planned_names.add(new_name)
                        plan.append({
                            "new_name": new_name, "kind": "pdf_page", "hash": h,
                            "src_path": item["path"], "pdf_page": page_no,
                            "png_bytes": png,
                            "project_id": _project_for(item, project_mode, project_id, subfolder_map),
                            "original_name": f"{item['name']}#p{page_no}",
                        })
                except Exception as e:
                    errors.append(f"PDF 转换失败 {item['name']}: {e}")

        if not plan:
            _update_task(
                task_id,
                status="done",
                skipped_duplicates=skipped_dup,
                errors=errors + (["没有可导入的材料"] if not items else []),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            return

        # ---------- Phase 3: 预写 assignments（先于复制！防 active 抢跑）----------
        assign_map = {p["new_name"]: p["project_id"] for p in plan if p["project_id"]}
        if assign_map:
            try:
                import data_io
                by_project: dict[str, list[str]] = {}
                for name, pid in assign_map.items():
                    by_project.setdefault(pid, []).append(name)
                for pid, names in by_project.items():
                    data_io.assign_images_batch(names, pid)
            except Exception as e:
                errors.append(f"项目预分配失败: {e}")
                log.warning(f"[batch_import] {task_id} assignments 预写失败: {e}")
                # 失败继续：图仍会导入，只是归属可能回落到手动分配

        # 批量图登记（auto-assign 豁免，无论模式——批量图的归属只由任务决定）
        register_batch_images([p["new_name"] for p in plan])

        # ---------- Phase 4: 复制 / 写入 inbox ----------
        inbox.mkdir(parents=True, exist_ok=True)
        image_names: list[str] = []
        copied = 0
        for p in plan:
            try:
                if p["kind"] == "pdf_page":
                    (inbox / p["new_name"]).write_bytes(p["png_bytes"])
                else:
                    shutil.copy2(p["src_path"], inbox / p["new_name"])
                image_names.append(p["new_name"])
                copied += 1
            except Exception as e:
                errors.append(f"写入 inbox 失败 {p['original_name']}: {e}")

        # 复制失败的部分回滚预分配（避免项目里挂不存在的图）
        if copied < len(plan):
            try:
                import data_io
                actual = set(image_names)
                for name in assign_map:
                    if name not in actual:
                        data_io.unassign_image(name)
            except Exception as e:
                log.warning(f"[batch_import] {task_id} 回滚孤儿 assignment 失败: {e}")

        # 登记持久化 hash 缓存（仅实际复制成功的，跨批去重与时间戳无关）
        try:
            ok_names = set(image_names)
            _register_hashes([(p["hash"], p["new_name"]) for p in plan
                              if p["new_name"] in ok_names])
        except Exception as e:
            log.warning(f"[batch_import] {task_id} hash 缓存登记失败（不影响本次导入）: {e}")

        _update_task(
            task_id,
            copied=copied,
            skipped_duplicates=skipped_dup,
            image_names=image_names,
            assignments={k: v for k, v in assign_map.items() if k in set(image_names)},
            errors=errors,
        )

        # ---------- Phase 4.5: OCR 批量预取（✅ P-OPT-6，可选）----------
        # 把整批 job 先提交出去（服务端并行），watchdog 逐张处理时命中「已提交」
        # ⇒ 总耗时 ΣBᵢ → max(Bᵢ)。**默认开启**（`OCR_BATCH_PREFETCH=100`，P-OPT-7 标定后）；
        #    设 0 即关闭（钩子直接返回 0，行为回到 P-OPT-6 之前）。
        # 纪律：预取失败**绝不**中断导入 —— watchdog 照常逐张提交，只是没享受到并行。
        if _prefetch_hook and copied and image_names:
            try:
                n_pf = _prefetch_hook([str(inbox / nm) for nm in image_names])
                if n_pf:
                    log.info(f"[batch_import] {task_id} OCR 预取登记 {n_pf} 张（服务端并行）")
            except Exception as e:
                log.warning(f"[batch_import] {task_id} OCR 预取失败（不影响导入）: {e}")

        # ---------- Phase 5: 等 watchdog 消化（批量中 → 单图自动导出节流）----------
        # inflight 计数器由本 try/finally 独占管理（外层 except 不再重复递减）
        with _inflight_lock:
            _ocr_batches_inflight += 1
        try:
            pending = _wait_ocr_done(task_id, image_names, inbox, outbox, failed_dir, ocr_wait_timeout)

            # ---------- Phase 6: 统一导出知识卡片 + 失效 RAG 缓存 ----------
            involved = {pid for pid in assign_map.values()}
            involved |= pop_deferred_projects()
            exported: list[str] = []
            if involved:
                _update_task(task_id, status="exporting")
                exported = _export_projects(task_id, involved)

            final_errors = (get_task(task_id) or {}).get("errors", []) or []
            # ★ 先清 upload 临时目录、再写 done（2026-09-24 修竞态）：
            #   轮询方以 status=="done" 作为"一切已结束"的信号 —— 清理若发生在
            #   done 写入之后，就存在"已 done 但临时目录还在"的窗口
            #   （test_upload_ok_starts_task 因此间歇变红）。
            #   失败路径不走这里：异常由 except 分支接管，临时目录保留供排查。
            if source == "upload":
                shutil.rmtree(upload_root, ignore_errors=True)
            _update_task(
                task_id,
                status="done",
                exported_projects=exported,
                errors=final_errors,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        finally:
            with _inflight_lock:
                _ocr_batches_inflight -= 1
    except Exception as e:
        log.exception(f"[batch_import] 任务 {task_id} 异常终止: {e}")
        _update_task(
            task_id,
            status="failed",
            errors=[f"{type(e).__name__}: {e}"],
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        with _inflight_lock:
            _ocr_batches_inflight -= 1
        _finish_callback(on_finished, task_id)


def _project_for(item: dict, project_mode: str, project_id: Optional[str],
                 subfolder_map: dict) -> Optional[str]:
    """解析单个材料的项目归属。"""
    if project_mode == "single":
        return project_id
    if project_mode == "subfolder":
        return subfolder_map.get(item["subfolder"])  # 根级文件 → None（不分配）
    return None  # none 模式


def _wait_ocr_done(task_id: str, image_names: List[str], inbox: Path,
                   outbox: Path, failed_dir: Path, timeout: float) -> int:
    """轮询本批图全部离开 inbox（→ outbox 成功 / failed 失败）。返回剩余 pending 数。"""
    deadline = time.time() + timeout
    last_counts = None
    while True:
        pending = [n for n in image_names if (inbox / n).exists()]
        done = sum(1 for n in image_names if (outbox / n).exists())
        failed = sum(1 for n in image_names if (failed_dir / n).exists())
        counts = (len(pending), done, failed)
        if counts != last_counts:
            _update_task(task_id, status="ocr_waiting",
                         ocr_done=done, ocr_failed=failed, ocr_pending=len(pending))
            last_counts = counts
        if not pending:
            return 0
        if time.time() >= deadline:
            _update_task(
                task_id,
                errors=(get_task(task_id) or {}).get("errors", [])
                + [f"等待 OCR 超时（{int(timeout)}s），仍有 {len(pending)} 张在 inbox"],
            )
            return len(pending)
        time.sleep(OCR_POLL_INTERVAL)


def _export_projects(task_id: str, project_ids: set) -> list:
    """同步导出涉及项目的知识卡片 + 失效 RAG corpus 缓存。失败记 errors 不 fail 任务。"""
    import knowledge_card
    from config import BASE_DIR as _base
    exported = []
    for pid in sorted(project_ids):
        try:
            knowledge_card.export_project(
                project_id=pid,
                output_dir=str(_base / "knowledge_cards"),
                granularity=["unit", "document"],
                data_dir=str(_base / "data"),
                manual_dir=str(_base / "manual_annotations"),
            )
            exported.append(pid)
            log.info(f"[batch_import] {task_id} 项目 {pid} 知识卡片已刷新")
        except Exception as e:
            _update_task(
                task_id,
                errors=(get_task(task_id) or {}).get("errors", [])
                + [f"知识卡片导出失败 {pid}: {e}"],
            )
            log.warning(f"[batch_import] {task_id} 导出 {pid} 失败: {e}")
    # 失效 RAG corpus 缓存（与 export API 行为一致）
    try:
        import rag as _rag
        cleared = _rag.clear_corpus_cache()
        log.info(f"[batch_import] {task_id} 已失效 RAG corpus 缓存（{cleared} 项）")
    except Exception:
        log.warning("[batch_import] 失效 RAG 缓存失败（非致命）", exc_info=True)
    return exported


def _finish_callback(on_finished, task_id: str) -> None:
    if on_finished is None:
        return
    try:
        on_finished(get_task(task_id))
    except Exception:
        log.warning("[batch_import] on_finished 回调异常（忽略）", exc_info=True)
