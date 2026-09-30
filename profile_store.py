"""P1a（2026-09-04）：文献类型档案存储（collection profile store）。

背景见《复杂版面识别整理方案_20260903.md》§4.1/§4.7：批量识别任务必须绑定
一个"文献类型档案"——档案 = 属性集 + 属性标注样本页 +（可选）关联模板。
本模块只负责档案本体（属性集 CRUD + 从模板表头导入）；属性标注样本页
由既有 manual_annotations 体系承载（行内 profile/attr 字段引用档案 id）。

设计要点（与 template_store 同风格）：
- 存储：data/collection_profiles/prof_<id>.json（可读可手改）
- 属性项：{"name", "desc", "synonyms": []}；输入端也接受纯字符串（自动归一）
- 删除 → _trash/ 回收站子目录（可恢复，避开会话删除保护钩子）
- 从模板表头导入：解析 template.sample_input 的首个非注释 CSV 行
  （官报"公司注册表"模板 → 13+ 列属性，零手工）
- 日志走 logging.getLogger("local_chronicles_ocr")（CLAUDE.md §约束 #5）
"""
from __future__ import annotations

import sys
import csv
import io
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

log = __import__("logging").getLogger("local_chronicles_ocr")

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent.resolve()   # PyInstaller：exe 目录（postbuild 联接真实数据）
else:
    _BASE = Path(__file__).parent.resolve()
DEFAULT_PROFILES_DIR = _BASE / "data" / "collection_profiles"

PROFILE_ID_RE = re.compile(r"^prof_[A-Za-z0-9]{6,32}$")
TEMPLATE_ID_RE = re.compile(r"^tmpl_[A-Za-z0-9]{6,32}$")

MAX_ATTRS = 64                 # 单档案属性数上限
MAX_ATTR_NAME = 32             # 属性名长度上限（字符）
MAX_DESC_LEN = 500             # 属性语义说明长度上限
MAX_SYNONYMS = 8               # 同义词个数上限
MAX_PROFILE_NAME = 60          # 档案名长度上限
MAX_PROFILE_DESC = 2000        # 档案描述长度上限

_lock = threading.Lock()


# ---------- 内部工具 ----------

def _norm_text(value, field: str, max_len: int, *, allow_empty: bool = True) -> str:
    """str 化、去首尾空白、限长。allow_empty=False 时空值抛 ValueError。"""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    value = value.strip()
    if not allow_empty and not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > max_len:
        raise ValueError(f"{field} 超长（{len(value)} > {max_len} 字符）")
    return value


def _normalize_attrs(raw) -> List[dict]:
    """属性输入归一：接受 [str] / [{"name":…}] / 混合 → [{"name","desc","synonyms"}]。

    重名去重（保留首个）；顺序保持输入顺序。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attrs 必须是 list")
    out: List[dict] = []
    seen = set()
    for item in raw:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise ValueError("属性项必须是字符串或对象")
        name = _norm_text(item.get("name"), "属性名", MAX_ATTR_NAME, allow_empty=False)
        if name in seen:
            continue
        desc = _norm_text(item.get("desc"), "属性说明", MAX_DESC_LEN)
        syn_raw = item.get("synonyms") or []
        if not isinstance(syn_raw, list):
            raise ValueError("synonyms 必须是 list")
        synonyms: List[str] = []
        for s in syn_raw[:MAX_SYNONYMS]:
            s = _norm_text(s, "同义词", MAX_ATTR_NAME)
            if s and s != name and s not in synonyms:
                synonyms.append(s)
        seen.add(name)
        out.append({"name": name, "desc": desc, "synonyms": synonyms})
    if len(out) > MAX_ATTRS:
        raise ValueError(f"属性数超上限（{len(out)} > {MAX_ATTRS}）")
    return out


def parse_template_headers(sample_input: str) -> List[str]:
    """模板 sample_input → 表头属性名列表。

    规则：跳过 `#` 注释行（多 sheet / 结构说明），取首个非空非注释行按
    CSV 解析；单元格去空白、去空项、去重（合并单元格展开会产生相邻重复，
    如 ["公司名","注册人","注册人"] → ["公司名","注册人"]）。
    """
    for line in (sample_input or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cells = next(csv.reader(io.StringIO(line)))
        except (csv.Error, StopIteration):
            return []
        out: List[str] = []
        for c in cells:
            c = c.strip()
            if c and c not in out:
                out.append(c)
        return out
    return []


# ---------- CRUD ----------

def list_profiles(profiles_dir: Path = DEFAULT_PROFILES_DIR) -> List[dict]:
    """全部档案（按创建时间倒序）。目录不存在 → 空列表。"""
    if not profiles_dir.exists():
        return []
    out: List[dict] = []
    for p in sorted(Path(profiles_dir).glob("prof_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if PROFILE_ID_RE.match(data.get("profile_id", "")):
                out.append(data)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[profile_store] 档案读取失败 {p.name}: {e}")
    out.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return out


def get_profile(profile_id: str,
                profiles_dir: Path = DEFAULT_PROFILES_DIR) -> Optional[dict]:
    """按 id 取单个档案；不存在/非法 id → None。"""
    if not profile_id or not PROFILE_ID_RE.match(profile_id):
        return None
    p = Path(profiles_dir) / f"{profile_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[profile_store] 档案读取失败 {profile_id}: {e}")
        return None


def _new_profile_id() -> str:
    # uuid 后缀：同秒批量保存也唯一（沿 template_store 的踩坑经验）
    return (f"prof_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            f"{uuid.uuid4().hex[:8]}")


def save_profile(payload: dict,
                 profiles_dir: Path = DEFAULT_PROFILES_DIR) -> dict:
    """新建档案。payload: {name, description?, attrs?, template_id?}。

    attrs 接受 [str] / [{"name","desc","synonyms"}] 混合列表。
    template_id 可选（须形如 tmpl_*，仅做格式校验，不要求模板已存在）。
    """
    name = _norm_text(payload.get("name"), "档案名", MAX_PROFILE_NAME, allow_empty=False)
    description = _norm_text(payload.get("description"), "档案描述", MAX_PROFILE_DESC)
    attrs = _normalize_attrs(payload.get("attrs"))
    template_id = (payload.get("template_id") or "").strip()
    if template_id and not TEMPLATE_ID_RE.match(template_id):
        raise ValueError("非法 template_id")

    pid = _new_profile_id()
    prof = {
        "profile_id": pid,
        "name": name,
        "description": description,
        "attrs": attrs,
        "template_id": template_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    profiles_dir = Path(profiles_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        (profiles_dir / f"{pid}.json").write_text(
            json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"[profile_store] 档案已保存: {pid} ({name}, {len(attrs)} 属性)")
    return prof


def update_profile(profile_id: str, payload: dict,
                   profiles_dir: Path = DEFAULT_PROFILES_DIR) -> Optional[dict]:
    """更新档案（部分字段：只改 payload 里出现的键）。不存在 → None。"""
    if not profile_id or not PROFILE_ID_RE.match(profile_id):
        raise ValueError("非法 profile_id")
    p = Path(profiles_dir) / f"{profile_id}.json"
    if not p.exists():
        return None
    prof = json.loads(p.read_text(encoding="utf-8"))
    if "name" in payload:
        prof["name"] = _norm_text(payload.get("name"), "档案名",
                                  MAX_PROFILE_NAME, allow_empty=False)
    if "description" in payload:
        prof["description"] = _norm_text(payload.get("description"),
                                         "档案描述", MAX_PROFILE_DESC)
    if "attrs" in payload:
        prof["attrs"] = _normalize_attrs(payload.get("attrs"))
    if "template_id" in payload:
        template_id = (payload.get("template_id") or "").strip()
        if template_id and not TEMPLATE_ID_RE.match(template_id):
            raise ValueError("非法 template_id")
        prof["template_id"] = template_id
    with _lock:
        p.write_text(json.dumps(prof, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    log.info(f"[profile_store] 档案已更新: {profile_id}")
    return prof


def delete_profile(profile_id: str,
                   profiles_dir: Path = DEFAULT_PROFILES_DIR) -> bool:
    """删除档案 → 移入 _trash/（可恢复）。不存在 → False。"""
    if not profile_id or not PROFILE_ID_RE.match(profile_id):
        raise ValueError("非法 profile_id")
    p = Path(profiles_dir) / f"{profile_id}.json"
    if not p.exists():
        return False
    trash_dir = Path(profiles_dir) / "_trash"
    with _lock:
        trash_dir.mkdir(parents=True, exist_ok=True)
        p.replace(trash_dir / f"{profile_id}.json")  # rename 语义，同盘原子
    log.info(f"[profile_store] 档案已移入回收站: {profile_id}")
    return True


def add_attr(profile_id: str, attr_name: str, desc: str = "",
             profiles_dir: Path = DEFAULT_PROFILES_DIR) -> Optional[dict]:
    """追加单个属性（标注页"临时新建属性自动写回档案"用）。

    已存在同名属性 → 幂等返回（不动档案）；属性数满 → ValueError。
    档案不存在 → None。
    """
    prof = get_profile(profile_id, profiles_dir)
    if prof is None:
        return None
    attr_name = _norm_text(attr_name, "属性名", MAX_ATTR_NAME, allow_empty=False)
    if any(a["name"] == attr_name for a in prof["attrs"]):
        return prof  # 幂等
    if len(prof["attrs"]) >= MAX_ATTRS:
        raise ValueError(f"属性数已满（{MAX_ATTRS}）")
    desc = _norm_text(desc, "属性说明", MAX_DESC_LEN)
    prof["attrs"].append({"name": attr_name, "desc": desc, "synonyms": []})
    p = Path(profiles_dir) / f"{profile_id}.json"
    with _lock:
        p.write_text(json.dumps(prof, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    log.info(f"[profile_store] 属性已追加: {profile_id} += {attr_name}")
    return prof


def import_attrs_from_template(profile_id: str,
                               template_id: str,
                               profiles_dir: Path = DEFAULT_PROFILES_DIR,
                               templates_dir: Path = None) -> Optional[dict]:
    """从模板表头一键导入属性集（§4.7b：官报 13 列 → 13 属性）。

    - 解析 template.sample_input 首个非注释 CSV 行为属性名
    - 追加档案中尚不存在的属性（去重），并回写 template_id 关联
    - 模板不存在 / sample_input 无表头 → ValueError；档案不存在 → None
    返回更新后的档案。
    """
    prof = get_profile(profile_id, profiles_dir)
    if prof is None:
        return None
    if not template_id or not TEMPLATE_ID_RE.match(template_id):
        raise ValueError("非法 template_id")
    # 读模板（延迟 import 避免循环依赖；templates_dir 缺省用 template_store 默认）
    import template_store
    tmpl = template_store.get_template(
        template_id, templates_dir or template_store.DEFAULT_TEMPLATES_DIR)
    if tmpl is None:
        raise ValueError(f"模板不存在: {template_id}")
    headers = parse_template_headers(tmpl.get("sample_input", ""))
    if not headers:
        raise ValueError("模板 sample_input 中没有可解析的表头行")
    if len(headers) > MAX_ATTRS:
        raise ValueError(f"模板表头列数超上限（{len(headers)} > {MAX_ATTRS}）")

    existing = {a["name"] for a in prof["attrs"]}
    added = [h for h in headers if h not in existing]
    # 上限校验（含已有属性）
    if len(prof["attrs"]) + len(added) > MAX_ATTRS:
        raise ValueError(f"导入后属性数超上限（{len(prof['attrs']) + len(added)} > {MAX_ATTRS}）")
    prof["attrs"].extend({"name": h, "desc": "", "synonyms": []} for h in added)
    prof["template_id"] = template_id
    p = Path(profiles_dir) / f"{profile_id}.json"
    with _lock:
        p.write_text(json.dumps(prof, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    log.info(f"[profile_store] 模板表头已导入: {profile_id} ← {template_id}"
             f"（新增 {len(added)} / 共 {len(headers)} 列）")
    return prof


# ===========================================================================
# 按名称 upsert（2026-09-15 技能包第 4 步：从 app.py 提到存储层）
# ===========================================================================
# ★ 单一实现入口（CLAUDE.md §60）：这段编排原先**内联在 app.py**
#   的 `_upsert_profile_from_attrs` 里，命令面（`chronicles profile` /
#   `chronicles project template`）够不到它 —— 于是"传模板建档案"这条
#   渠道只有 GUI 有（用户 2026-09-14 指出的起始流程缺口）。
#   提到存储层后，GUI 与命令面共用同一份：改名（换 label）会另立档案，
#   同名（同 label 或同名栏目）则**合并属性**去重。
def find_by_name(name: str,
                 profiles_dir: Path = DEFAULT_PROFILES_DIR) -> Optional[dict]:
    """按**名称精确匹配**取档案（同名多个时取最新的一个）。无 → None。

    `list_profiles` 已按 created_at 倒序，故首个命中即最新。
    """
    target = (str(name) if name is not None else "").strip()
    if not target:
        return None
    for p in list_profiles(profiles_dir):
        if p.get("name") == target:
            return p
    return None


def upsert_by_name(name: str, attr_names,
                   profiles_dir: Path = None) -> dict:
    """按名称 upsert 档案：**存在则合并属性**（追加未有的），不存在则新建。

    返回档案 dict；属性数超上限等校验问题以 ValueError 抛出。
    `profiles_dir=None` → 用模块默认目录（GUI 侧原行为如此，见 §60 表）。
    """
    kw = {"profiles_dir": profiles_dir} if profiles_dir is not None else {}
    existing = (find_by_name(name, profiles_dir)
                if profiles_dir is not None else find_by_name(name))
    if existing is None:
        return save_profile({"name": name, "attrs": list(attr_names or [])}, **kw)
    merged = [a["name"] for a in existing.get("attrs", [])]
    for h in (attr_names or []):
        if h not in merged:
            merged.append(h)
    return update_profile(existing["profile_id"], {"attrs": merged}, **kw)
