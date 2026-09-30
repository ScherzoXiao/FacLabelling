"""项目（专项栏目）数据访问层。

设计：
- projects.json：所有项目元信息（id, name, description, color, tags, ocr_backend, created_at）
- project_assignments.json：image_name → project_id 映射
- active_project.json：当前激活项目 ID（inbox 新图自动归这里）

为什么不分子目录：
- outbox 保持 flat，避免改 watchdog + excel_writer 路径逻辑
- 用 project_assignments.json 索引，零迁移成本
- 未来 RAG 直接按 project_id 过滤即可
"""
import sys
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional


# ========== 路径 ==========
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录（postbuild 联接真实数据）
else:
    _BASE = Path(__file__).parent.resolve()
DATA_DIR = _BASE / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROJECTS_FILE = DATA_DIR / "projects.json"
ASSIGNMENTS_FILE = DATA_DIR / "project_assignments.json"
ACTIVE_PROJECT_FILE = DATA_DIR / "active_project.json"

_lock = threading.RLock()  # 项目 IO 全局锁


# ========== 工具函数 ==========
_now_seq = 0  # 2026-08-19：保证 created_at 在毫秒内也单调递增（防 time.time() 同值）
_now_lock = threading.Lock()


def _now_iso() -> str:
    # 2026-08-19：加微秒精度 + 进程内单调递增序号
    # 让 created_at 在秒内/毫秒内也能严格区分（stable sort 不能保证"最新"）
    global _now_seq
    with _now_lock:
        _now_seq += 1
        seq = _now_seq
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{seq:06d}"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ========== 原子写 / 文件命名（**单一实现**，app / preannotate / adjudicate 共用） ==========
# 2026-09-11（方案 §10.4 批 1，对照 X-AnyLabeling `LabelFile.save`）：
# 此前「原子写」与「文件名净化」在本仓库各有平行实现——前者 review_store / adjudicate /
# drift / batch_import 各写一遍（preannotate 漏做），后者 app.py 内联 8 处 + adjudicate 1 处。
# 现统一到此模块；调用方一律引用，不得再就地实现。

def atomic_write_text(path: Path, text: str, fsync: bool = True) -> Path:
    """原子写文本：同目录临时文件 → flush → fsync → `os.replace`。

    **「全部成功才替换」**：写入过程任何异常都不触碰目标文件（旧内容完整保留）。
    `fsync=True` 保证掉电后不会出现「改名成功但内容为空/半截」的文件。
    临时文件与目标**同目录**，避免跨卷 `os.replace` 失败。
    """
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def atomic_write_bytes(path: Path, data: bytes, fsync: bool = True) -> Path:
    """原子写**原始字节**（同 `atomic_write_text` 的语义：临时文件 → fsync → `os.replace`）。

    为什么需要它（而不是"读成文本再写回去"）：备份必须**逐字节等于**原文件。
    文本往返会在两处改变字节 —— ① git 在工作区检出的 CRLF（`read_text` 的通用换行
    翻译会把它折成 `\\n`）；② 任何 re-serialize（`json.dumps` 的浮点/转义写法）都无法
    保证与原字节一致。而"先备份"的全部价值就在于能原样回滚。
    """
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def atomic_write_json(path: Path, data, indent: int = 2) -> Path:
    """原子写 JSON（UTF-8 不转义）。`indent` 可调，便于调用方保持既有文件字节形态。"""
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


def atomic_write_jsonl(path: Path, rows) -> Path:
    """原子写 JSONL：**先把全部分行渲染进内存**，再一次性落盘。

    行渲染期间抛错（如对象不可序列化）→ 目标文件保持旧版本，绝不出现半截草稿。
    """
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return atomic_write_text(path, text)


def _write_json_atomic(path: Path, data) -> None:
    """原子写：写到 .tmp + os.replace（保留旧签名，内部转调 `atomic_write_json`）。"""
    atomic_write_json(path, data)


def safe_name(name) -> str:
    """文件名净化（**单一实现**）。保留字母数字与 `.` `_` `-`，其余替换为 `_`。

    口径与历史逐字一致（`"".join(c if c.isalnum() or c in "._-" else "_" ...)`），
    保证既有 jsonl 文件名不变（向后兼容）。
    """
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(name or ""))


def name_collisions(names) -> List[dict]:
    """找出**净化后同名**的不同原名 —— 静默串页风险，必须**可见不静默**。

    场景：批量导入保留原文件名，`a b.png` 与 `a_b.png` 净化后同名，
    会落到同一个 `<safe>.jsonl`（后写覆盖先写）。

    返回 `[{"safe_name": ..., "image_names": [...]}, ...]`（每组 ≥2 个**不同**原名）；
    组内按原名排序，组间按净化名排序（确定性输出）。无碰撞 → `[]`。
    """
    groups: Dict[str, List[str]] = {}
    for n in names:
        groups.setdefault(safe_name(n), []).append(str(n))
    return [{"safe_name": k, "image_names": sorted(set(v))}
            for k, v in sorted(groups.items()) if len(set(v)) > 1]


def _slugify(name: str) -> str:
    """把项目名转成 ID（用于 file/key 索引）：保留中文 + ASCII，去掉特殊字符。"""
    import re
    s = re.sub(r'[\\/:*?"<>|]+', '', name).strip()
    return s or "unnamed"


# ========== 项目 CRUD ==========
def list_projects() -> List[dict]:
    """列出所有项目，按 created_at 倒序。"""
    with _lock:
        projects = _read_json(PROJECTS_FILE, [])
        if not isinstance(projects, list):
            projects = []
    # 按 created_at 倒序
    projects.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return projects


def get_project(project_id: str) -> Optional[dict]:
    """按 ID 查项目。"""
    for p in list_projects():
        if p.get("id") == project_id:
            return p
    return None


def create_project(name: str, description: str = "", color: str = "#4A7FCB",
                   tags: List[str] = None, ocr_backend: str = None) -> dict:
    """新建项目。"""
    with _lock:
        projects = _read_json(PROJECTS_FILE, [])
        if not isinstance(projects, list):
            projects = []
        # ID 用 name slug（用户友好）+ 短 uuid（避免重名）
        slug = _slugify(name)
        suffix = uuid.uuid4().hex[:6]
        new_id = f"{slug}_{suffix}"
        project = {
            "id": new_id,
            "name": name,
            "description": description,
            "color": color,
            "tags": tags or [],
            "ocr_backend": ocr_backend,  # None = 跟随全局默认
            "created_at": _now_iso(),
            "image_count": 0,  # OCR 完时自动更新
            "ocr_record_count": 0,
        }
        projects.append(project)
        _write_json_atomic(PROJECTS_FILE, projects)
        return project


def update_project(project_id: str, **kwargs) -> Optional[dict]:
    """更新项目字段（name, description, color, tags, ocr_backend, summary_template,
    summary_templates）。

    `summary_templates` 是 2026-09-12 起的**多模板样式**列表（`summary_template`
    保留为"最近一次"，供学术导出等既有消费口使用）。
    """
    allowed = {"name", "description", "color", "tags", "ocr_backend",
               "summary_template", "summary_templates"}
    with _lock:
        projects = _read_json(PROJECTS_FILE, [])
        if not isinstance(projects, list):
            return None
        for p in projects:
            if p.get("id") == project_id:
                for k, v in kwargs.items():
                    if k in allowed:
                        p[k] = v
                _write_json_atomic(PROJECTS_FILE, projects)
                return p
        return None


def delete_project(project_id: str) -> bool:
    """删除项目（不影响图片和 OCR 记录，只解除关联）。"""
    with _lock:
        projects = _read_json(PROJECTS_FILE, [])
        if not isinstance(projects, list):
            return False
        new_projects = [p for p in projects if p.get("id") != project_id]
        if len(new_projects) == len(projects):
            return False
        _write_json_atomic(PROJECTS_FILE, new_projects)
        # 清理 assignments 里该项目
        assignments = _read_json(ASSIGNMENTS_FILE, {})
        if not isinstance(assignments, dict):
            assignments = {}
        for img_name in list(assignments.keys()):
            if assignments[img_name] == project_id:
                del assignments[img_name]
        _write_json_atomic(ASSIGNMENTS_FILE, assignments)
        # 如果是 active project，清掉
        if get_active_project() == project_id:
            set_active_project(None)
        return True


# ========== Active Project（截图自动归这里） ==========
def get_active_project() -> Optional[str]:
    """当前激活项目 ID。

    默认规则（2026-08-19 重构）：
    1. active_project.json 显式设置了一个 ID，且该项目存在 → 返回它
    2. 没设置 / 设置了已删除的项目 / JSON 文件缺失 → fallback 到最新项目
    3. 一个项目都没有 → 返回 None

    为什么要 fallback 到最新：
    - 用户描述"截图存入一个专栏项目后"——希望新截图自动归到最新的项目
    - 显式"清空"按钮（PUT null）仍然允许（fallback 不影响"清空"语义，
      因为清空意味着没激活，下一张图按 fallback 进最新项目也很合理）
    """
    active_id, _ = get_active_project_with_source()
    return active_id


def get_active_project_with_source() -> tuple:
    """返回 (active_project_id, was_explicit) — 后者用于前端区分显示。

    was_explicit = True：用户显式设的
    was_explicit = False：fallback 到最新项目（用户没设过）
    """
    with _lock:
        data = _read_json(ACTIVE_PROJECT_FILE, {})
        active_id = None
        if isinstance(data, dict):
            active_id = data.get("active_project_id")
        # 1) 显式 active 且项目存在
        if active_id:
            projects = _read_json(PROJECTS_FILE, [])
            if isinstance(projects, list) and any(p.get("id") == active_id for p in projects):
                return active_id, True
        # 2) Fallback：最新项目
        projects = _read_json(PROJECTS_FILE, [])
        if isinstance(projects, list) and projects:
            projects_sorted = sorted(
                projects, key=lambda p: p.get("created_at", ""), reverse=True
            )
            return projects_sorted[0].get("id"), False
        return None, False


def set_active_project(project_id: Optional[str]) -> None:
    """设置当前激活项目。None = 清空（inbox 新图不自动归类）。"""
    with _lock:
        _write_json_atomic(ACTIVE_PROJECT_FILE, {
            "active_project_id": project_id,
            "updated_at": _now_iso(),
        })


# ========== 图 → 项目分配 ==========
def get_project_image_stems(project_id: str, data_dir: Optional[Path] = None) -> List[str]:
    """取项目图片白名单（image_name 的 stem，排序返回）。

    M23 统一实现：unit_builder / reassembler / exporter 原各自持有等价函数，
    现委托到此。data_dir 缺省用模块级 DATA_DIR（测试可 monkeypatch）；
    读取失败时返回 []（与历史行为一致：按全部图片处理）。
    """
    if not project_id:
        return []
    base = Path(data_dir) if data_dir else DATA_DIR
    try:
        assign = json.loads((base / "project_assignments.json").read_text(encoding="utf-8"))
        if not isinstance(assign, dict):
            return []
        return sorted(Path(k).stem for k, v in assign.items() if v == project_id)
    except Exception:
        return []


def get_all_assignments() -> Dict[str, str]:
    """所有分配：{image_name: project_id}"""
    with _lock:
        assignments = _read_json(ASSIGNMENTS_FILE, {})
        if not isinstance(assignments, dict):
            return {}
        return dict(assignments)


def get_image_project(image_name: str) -> Optional[str]:
    """查某张图属于哪个项目。"""
    with _lock:
        assignments = _read_json(ASSIGNMENTS_FILE, {})
        if isinstance(assignments, dict):
            return assignments.get(image_name)
        return None


def assign_image(image_name: str, project_id: str) -> None:
    """分配一张图到项目。"""
    with _lock:
        assignments = _read_json(ASSIGNMENTS_FILE, {})
        if not isinstance(assignments, dict):
            assignments = {}
        assignments[image_name] = project_id
        _write_json_atomic(ASSIGNMENTS_FILE, assignments)
        # 更新项目的 image_count
        _update_project_counts(project_id)


def assign_images_batch(image_names: List[str], project_id: str) -> int:
    """批量分配，返回成功数。"""
    with _lock:
        assignments = _read_json(ASSIGNMENTS_FILE, {})
        if not isinstance(assignments, dict):
            assignments = {}
        n = 0
        for name in image_names:
            assignments[name] = project_id
            n += 1
        _write_json_atomic(ASSIGNMENTS_FILE, assignments)
        _update_project_counts(project_id)
        return n


def unassign_image(image_name: str) -> None:
    """解除图的分配（回到未分类）。"""
    with _lock:
        assignments = _read_json(ASSIGNMENTS_FILE, {})
        if isinstance(assignments, dict) and image_name in assignments:
            old_project = assignments[image_name]
            del assignments[image_name]
            _write_json_atomic(ASSIGNMENTS_FILE, assignments)
            _update_project_counts(old_project)


def list_project_images(project_id: str) -> List[str]:
    """项目下所有图（按 image_name 排序）。"""
    with _lock:
        assignments = _read_json(ASSIGNMENTS_FILE, {})
        if not isinstance(assignments, dict):
            return []
        return sorted([k for k, v in assignments.items() if v == project_id])


def stem_to_project(assignments: Optional[Dict[str, str]] = None,
                    data_dir: Optional[Path] = None) -> Dict[str, str]:
    """`{图名: 项目id}` → `{stem: 项目id}`（**按 stem 归一**）。

    ★ 为什么必须归一到 stem（2026-09-16）：金标准侧只有 stem（标注文件名是
      `<图名>.jsonl`，`stem_of_gold_file` 要剥两层），而归属表用的是**完整
      image_name**（带扩展名）。不归一就会**静默漏归属** —— 换扩展名（`.jpg`）
      或改名后缀之后，"这个档案属于哪个项目"会凭空变空，且不报错。
    ★ 为什么收成单一实现：原先 `app.py` 的 `/api/seam_subjects` 内联了同样逻辑，
      命令面再写一遍就是两份口径（`CLAUDE.md` §60）。同键取**先出现的**（保序）。
    """
    if assignments is None:
        assignments = get_all_assignments() or {}
    out: Dict[str, str] = {}
    for img_name, proj_id in (assignments or {}).items():
        stem = Path(str(img_name)).stem
        if stem and proj_id:
            out.setdefault(stem, str(proj_id))
    return out


def _update_project_counts(project_id: str) -> None:
    """重新统计项目的 image_count / ocr_record_count（从 xlsx 取真实数）。"""
    try:
        from excel_writer import ExcelWriter
        from config import OUTPUT_XLSX
        import os
        if not os.path.exists(OUTPUT_XLSX):
            return
        ew = ExcelWriter(OUTPUT_XLSX)
        all_records = ew.list_records()
        project_images = set(list_project_images(project_id))
        project_records = [r for r in all_records if r.get("image_name") in project_images]
        with _lock:
            projects = _read_json(PROJECTS_FILE, [])
            if not isinstance(projects, list):
                return
            for p in projects:
                if p.get("id") == project_id:
                    p["image_count"] = len(project_images)
                    p["ocr_record_count"] = len(project_records)
            _write_json_atomic(PROJECTS_FILE, projects)
    except Exception:
        pass  # 统计失败不影响主流程
