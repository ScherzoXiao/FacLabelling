# -*- coding: utf-8 -*-
"""project_cli —— **专项栏目**的命令面（技能包第 4 步 · 2026-09-15）。

用户 2026-09-15 的裁定：「同意补完三个以实现闭合」。

**补的是什么**（侦查实测的三处缺口）：

| 起始流程的这一步 | GUI 里的实现 | 本模块补的 |
|---|---|---|
| ① 建专项栏目 | `POST /api/projects` → `data_io.create_project` | `project new` |
| ② 传模板 → 建档案 | `POST /api/projects/<id>/summary_template`（**60 行编排内联在路由里**） | `project template` |
| ③ 导入图 + 归栏目 | `POST /api/batch_import` 的 `project_mode`（**导入时一次性决定**） | `project assign` |

★ **栏目 ≠ 档案**（两个存储、两个概念，见 `profile_cli` 的对照表）：
  栏目 = 图片归属 + 模板样式 + `ocr_backend`（`data/projects.json`）。

★★ **③ 为什么必须补**：`triage --project <id>` 走 `data_io.get_project_image_stems`，
   它读的是 `data/project_assignments.json`；而 `import_cli` **不写归属**
   （实测 grep 不到 assign/project 代码）⇒ **凡是用 `import` 进来的图，
   `--project` 一页都取不到**（只有 `--inbox` 通）。这是「看起来能跑、
   实际取空」的静默断裂，所以 `assign` 不是锦上添花，是闭合必需的一环。

★ **不重造任何一环**（`CLAUDE.md` §60）：
  · 建/列/查栏目 → `data_io.create_project` / `list_projects` / `get_project`
  · 归属 → `data_io.assign_images_batch` / `unassign_image` / `list_project_images`
  · 模板解析 → `summary_template.extract_text` + `extract_attr_headers`
  · 建档 → `profile_store.upsert_by_name`（本模块只编排，不复制）
  其中 `apply_template_style` 是**从 `app.py` 提公开的同一份编排** ——
  GUI 路由改为转调它，两边不会漂移。

退出码
    0   成功
    3   缺料（栏目不存在 / 模板文件不存在 / 图在 inbox·outbox 里都找不到）
    4   校验不过（模板解析不出表头）
    1   内部错误
    64  用法错误

用法
    python project_cli.py new --name 官報公司註冊 --json
    python project_cli.py list --json
    python project_cli.py assign --project 官報公司註冊_86e087 --images "outbox/*.png"
    python project_cli.py template --project <id> --file "<汇总模板.xlsx>"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 退出码契约：正本在 `cli_contract`（2026-09-24 收敛）----
# ★ 判定点仍在各 `project_*` 动作内 —— 收敛的是**词表**，不是判定。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_MISSING, EXIT_INVALID,  # noqa: E402
                          EXIT_USAGE, EXIT_MEANING, Parser as _Parser)         # noqa: E402

STYLE_TEXT_LIMIT = 4000      # 样式里存的模板原文上限（与 GUI 同口径）


class ProjectMissing(LookupError):
    """栏目不存在 → GUI 404 / CLI 缺料 3。"""


class TemplateError(ValueError):
    """模板不合格（解析不出表头等）→ GUI 400 / CLI 校验不过 4。"""


# ============================================================
# 栏目解析与常用读取
# ============================================================
def resolve_project(token: str) -> Optional[dict]:
    """`--project` 的值 → 栏目 dict。**先按 id 查，查不到再按名称查**。"""
    import data_io

    t = (str(token) if token is not None else "").strip()
    if not t:
        return None
    return data_io.get_project(t) or find_project_by_name(t)


def find_project_by_name(name: str) -> Optional[dict]:
    import data_io

    target = (str(name) if name is not None else "").strip()
    if not target:
        return None
    for p in data_io.list_projects():        # 已按 created_at 倒序
        if p.get("name") == target:
            return p
    return None


def _project_brief(p: dict) -> Dict[str, Any]:
    return {
        "id": p.get("id"), "name": p.get("name"),
        "description": p.get("description") or "",
        "image_count": p.get("image_count", 0),
        "n_templates": len((p.get("summary_templates") or [])),
        "created_at": p.get("created_at"),
    }


# ============================================================
# ★ 模板样式挂载（从 app.py 提公开的**同一份**编排）
# ============================================================
def apply_template_style(*, project_id: str, filename: str, tmp_path,
                         read_profiles_dir=None, upsert_fn=None,
                         ts: Optional[str] = None) -> Dict[str, Any]:
    """把一份模板文件挂到栏目上：解析表头 → upsert 档案 → 并入样式 → 写栏目。

    **这是原先内联在 `app.py` 的 `api_summary_template_upload` 里的那段编排**，
    2026-09-15 提到这里，好让 GUI 与命令面共用（§60）。行为逐字保持：

    - 样式**并入** `project.summary_templates`：同 label（文件名去扩展名）覆盖、
      新 label 追加；`summary_template`（单值）同步为"最近一次"。
    - 样式 ↔ 档案：同 label 沿用该样式已登记的 `profile_id`（"改一改再传"）；
      新 label 建独立档案（`<栏目名>｜<标签>`；本栏目第一套仍用裸栏目名）。
    - **档案 upsert 失败不阻塞主流程**：属性照存，`profile_error` 透出。

    ⚠ `read_profiles_dir` 缺省 `None` 是**刻意保持原行为**：原路由读档案时用
      模块全局 `PROFILE_STORE_DIR`（可被测试 patch），而 upsert 时**不传目录**
      （＝写入 profile_store 默认目录）。两者口径本就不一致，本次**只转调、
      不"顺手修正"** —— 改它就是改 GUI 行为。`upsert_fn` 同理：GUI 侧传入
      `app._upsert_profile_from_attrs`（保住既有测试的 patch 点），
      命令面走缺省 `profile_store.upsert_by_name`。

    出错：栏目不存在 → `ProjectMissing`；模板不合格 → `TemplateError`。
    """
    import summary_template as SM
    import profile_store as PS
    import data_io

    project = data_io.get_project(project_id)
    if project is None:
        raise ProjectMissing(project_id)

    text = SM.extract_text(tmp_path)                 # 后缀/解析问题 → ValueError
    attrs = SM.extract_attr_headers(text)
    if not attrs:
        raise TemplateError("未从模板中解析到表头属性（首个有效行应为各属性列名）")

    if upsert_fn is None:
        upsert_fn = PS.upsert_by_name

    # ---- 样式归属：同 label 沿用旧档案，新 label 建独立档案 ----
    styles = SM.styles_of(project)
    label = SM.style_label(filename)
    prev = SM.find_style(styles, label)
    profile = None
    profile_error = None
    if prev and prev.get("profile_id"):
        profile = PS.get_profile(prev["profile_id"], read_profiles_dir)
    if profile is not None:
        # 该 label 已有档案 → 直接合并属性（不新建、不改名）
        try:
            profile = upsert_fn(profile["name"], attrs)
        except Exception as e:                         # noqa: BLE001
            profile_error = f"{type(e).__name__}: {e}"
    else:
        name = SM.profile_name_for(project["name"], label, has_prev=bool(styles))
        try:
            profile = upsert_fn(name, attrs)
        except Exception as e:                         # noqa: BLE001
            profile_error = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"

    payload = {"label": label, "filename": filename, "attrs": attrs,
               "text": text[:STYLE_TEXT_LIMIT],
               "ts": ts or datetime.now().strftime("%Y-%m-%d %H:%M")}
    if profile:
        payload["profile_id"] = profile.get("profile_id")

    new_styles = SM.upsert_style(styles, payload)
    # 两个字段一起写：`summary_templates` 是唯一口径，`summary_template` 是旧消费口的兼容值
    updated = data_io.update_project(project_id, summary_templates=new_styles,
                                     summary_template=payload)
    if updated is None:
        # 栏目在上面已确认存在过 ⇒ 走到这里只可能是写盘失败。
        # 用 RuntimeError（不是 ProjectMissing）—— 前者 GUI 映射 500、后者 404，
        # 与原路由「项目更新失败 → 500」的语义保持一致。
        raise RuntimeError("项目更新失败")
    return {"ok": True, "project_id": project_id,
            "style": payload, "styles": new_styles,
            "profile": profile, "profile_error": profile_error,
            "attrs": attrs, "label": label}


# ============================================================
# 动作
# ============================================================
def _do_new(*, name, description="", color="#4A7FCB", tags=None,
            ocr_backend=None, force_new=False, dry_run=False) -> Dict[str, Any]:
    import data_io

    res: Dict[str, Any] = {"ok": False, "action": "new", "dry_run": bool(dry_run),
                           "name": str(name or "").strip(), "reused": False,
                           "project": None}
    n = res["name"]
    if not n:
        # ★ 空名判**校验不过(4)**、不是缺料(3)（§4.3：退出码要能区分下一步动作）。
        res["error"] = "栏目名不能为空"
        res["invalid"] = True
        return res

    # ★ 幂等：agent 重试不该建出第二个同名栏目（§4.2）。
    existing = find_project_by_name(n)
    if existing is not None and not force_new:
        res["ok"] = True
        res["reused"] = True
        res["project"] = existing
        res["next"] = "同名栏目已存在，**未改动**（要另立一个用 `--force-new`）。"
        return res

    if dry_run:
        res["ok"] = True
        res["next"] = ("干跑：将新建栏目。去掉 --dry-run 即真建。"
                       "★ 注意：新建**不自动激活** —— 激活只影响剪贴板通道的落位。")
        return res

    proj = data_io.create_project(name=n, description=description, color=color,
                                  tags=list(tags or []), ocr_backend=ocr_backend)
    res["ok"] = True
    res["project"] = proj
    res["next"] = (f"栏目已建：{proj['id']}。下一步传模板建档案："
                   f"`chronicles project template --project {proj['id']} "
                   f"--file <汇总模板>`；或直接 `chronicles import --images <材料目录>` "
                   f"再 `chronicles project assign --project {proj['id']} --images ...`。")
    return res


def _do_list() -> Dict[str, Any]:
    import data_io

    projs = data_io.list_projects()
    return {"ok": True, "action": "list", "n_projects": len(projs),
            "projects": [_project_brief(p) for p in projs],
            "next": "选一个栏目 → `chronicles triage --profile <pid> --project <栏目id>`。"}


def _do_show(*, project, with_images=False) -> Dict[str, Any]:
    import data_io

    p = resolve_project(project)
    if p is None:
        return {"ok": False, "action": "show",
                "error": f"栏目不存在：{project}（用 `project list` 看有哪些）"}
    out: Dict[str, Any] = {"ok": True, "action": "show", "project": p,
                           "brief": _project_brief(p),
                           "styles": [{"label": s.get("label"),
                                       "profile_id": s.get("profile_id", ""),
                                       "n_attrs": len(s.get("attrs") or [])}
                                      for s in (p.get("summary_templates") or [])]}
    if with_images:
        imgs = data_io.list_project_images(p["id"])
        out["images"] = imgs
        out["n_images"] = len(imgs)
    out["next"] = ("`chronicles triage --profile <档案id> --project "
                   f"{p['id']}` 分诊这一栏目的页。")
    return out


def _image_names_from_sources(sources) -> List[str]:
    """路径/图名 → image_name 列表（去重保序）。

    - **目录** → 扫**顶层**图像文件（后缀口径取自 `config.IMAGE_EXTS`，单一来源。
      只扫顶层：`import` 进来的图就在 `inbox/`·`outbox/` 顶层，递归会把
      归档副本也一起归栏目）
    - **文件路径 / 图名** → 取 basename（`assignments` 的 key 是 image_name，含扩展名）
    """
    import config

    exts = {str(e).lower() for e in config.IMAGE_EXTS}
    out: List[str] = []
    for s in (sources or []):
        p = Path(str(s).strip().strip('"'))
        if p.is_dir():
            cands = sorted(q.name for q in p.iterdir()
                           if q.is_file() and q.suffix.lower() in exts)
        else:
            cands = [p.name]
        for nm in cands:
            if nm and nm not in out:
                out.append(nm)
    return out


def _locate_images(names: List[str]):
    """把 image_name 分成「找得到」与「找不到」两拨（inbox / outbox 两处查）。"""
    import config

    where: Dict[str, Path] = {}
    for d in (Path(config.INBOX), Path(config.OUTBOX)):
        where[str(d)] = d
    found: List[str] = []
    missing: List[str] = []
    for nm in names:
        if any((d / nm).exists() for d in where.values()):
            found.append(nm)
        else:
            missing.append(nm)
    return found, missing, [str(d) for d in where.values()]


def _do_assign(*, project, images=None, names=None, unassign=False,
               dry_run=False) -> Dict[str, Any]:
    import data_io

    res: Dict[str, Any] = {"ok": False, "action": "assign", "dry_run": bool(dry_run),
                           "unassign": bool(unassign), "project": None,
                           "n_assigned": 0, "n_missing": 0,
                           "assigned": [], "missing": []}
    p = resolve_project(project)
    if p is None:
        res["error"] = f"栏目不存在：{project}（用 `project list` 看有哪些）"
        return res
    res["project"] = p["id"]

    src = list(images or []) + list(names or [])
    if not src:
        res["error"] = "没有给要归栏目的图（用 --images <路径…> 或 --names <图名…>）"
        return res
    target = _image_names_from_sources(src)

    if unassign:
        # 解除归属**不校验存在性**：图可能已从 inbox/outbox 移走（例如已归档），
        # 但归属记录仍要能清掉，否则会留下悬空归属。
        if dry_run:
            res.update(ok=True, n_assigned=len(target), assigned=target,
                       next=f"干跑：将解除 {len(target)} 张的归属。")
            return res
        for nm in target:
            data_io.unassign_image(nm)
        res.update(ok=True, n_assigned=len(target), assigned=target,
                   next=f"已解除 {len(target)} 张的归属（回到未分类）。")
        return res

    found, missing, looked_in = _locate_images(target)
    res["missing"] = missing
    res["n_missing"] = len(missing)

    # ★「一张都找不到」＝ 缺料（不是成功）：多半是路径写错，或图还没 import 进来。
    #   不校验就写归属会造出**悬空 assignment** —— 之后 `triage --project`
    #   取到空页，错误会以"分诊说缺料"的形态在更远处冒出来，更难查。
    if not found:
        res["error"] = (f"{len(target)} 张图在 {looked_in} 里都找不到 —— "
                        "先 `chronicles import --images <材料目录>` 再归栏目。")
        return res

    if dry_run:
        res.update(ok=True, n_assigned=len(found), assigned=found,
                   next=f"干跑：将把 {len(found)} 张归到 {p['id']}。")
        return res

    data_io.assign_images_batch(found, p["id"])
    res.update(ok=True, n_assigned=len(found), assigned=found)
    if missing:
        res["next"] = (f"已归栏目 {len(found)} 张（{len(missing)} 张没找到，已跳过）。"
                       "下一步 `chronicles triage --profile <档案id> "
                       f"--project {p['id']}`。")
    else:
        res["next"] = (f"已归栏目 {len(found)} 张。下一步 `chronicles triage "
                       f"--profile <档案id> --project {p['id']}`。")
    return res


def _do_images(*, project) -> Dict[str, Any]:
    import data_io

    p = resolve_project(project)
    if p is None:
        return {"ok": False, "action": "images",
                "error": f"栏目不存在：{project}"}
    imgs = data_io.list_project_images(p["id"])
    return {"ok": True, "action": "images", "project": p["id"],
            "n_images": len(imgs), "images": imgs,
            "next": ("空的话说明还没归栏目 —— `chronicles project assign "
                     f"--project {p['id']} --images <outbox 里的图>`。")}


def _do_template(*, project, file, dry_run=False,
                 profiles_dir=None) -> Dict[str, Any]:
    res: Dict[str, Any] = {"ok": False, "action": "template", "dry_run": bool(dry_run),
                           "project": None, "attrs": [], "profile": None,
                           "profile_error": None}
    p = resolve_project(project)
    if p is None:
        res["error"] = f"栏目不存在：{project}（用 `project list` 看有哪些）"
        return res
    res["project"] = p["id"]

    f = Path(str(file or "").strip().strip('"'))
    if not f.exists():
        res["error"] = f"模板文件不存在：{f}"
        return res

    if dry_run:
        import summary_template as SM

        try:
            text = SM.extract_text(f)
            attrs = SM.extract_attr_headers(text)
        except ValueError as e:
            res["error"] = f"模板解析失败：{e}"
            res["invalid"] = True
            return res
        if not attrs:
            res["error"] = "未从模板中解析到表头属性（首个有效行应为各属性列名）"
            res["invalid"] = True
            return res
        res.update(ok=True, attrs=attrs, label=SM.style_label(f.name),
                   next=f"干跑：将从该模板取到 {len(attrs)} 个属性并挂到栏目上。")
        return res

    # ★ 档案目录**可注入**（测试隔离用）。缺省 None = 走 `profile_store` 默认目录，
    #   这正是生产行为（命令面就是要建**真**档案）；但测试必须能把它指开 ——
    #   否则用例会往真实 `data/collection_profiles/` 写垃圾档案
    #   （这个坑 2026-09-14 已在金标准目录上踩过一次）。
    upsert_fn = None
    if profiles_dir is not None:
        from functools import partial

        import profile_store as PS
        upsert_fn = partial(PS.upsert_by_name, profiles_dir=profiles_dir)
    try:
        r = apply_template_style(project_id=p["id"], filename=f.name, tmp_path=f,
                                 read_profiles_dir=profiles_dir, upsert_fn=upsert_fn)
    except ProjectMissing as e:
        res["error"] = f"栏目不存在：{e}"
        return res
    except TemplateError as e:
        res["error"] = str(e)
        res["invalid"] = True
        return res
    except ValueError as e:
        res["error"] = f"模板解析失败：{e}"
        res["invalid"] = True
        return res
    except Exception as e:                                # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res

    res.update(ok=True, attrs=r["attrs"], label=r["label"],
               profile=r.get("profile"), profile_error=r.get("profile_error"),
               styles=r.get("styles"))
    pid = (r.get("profile") or {}).get("profile_id")
    if pid:
        res["next"] = (f"已挂模板（{len(r['attrs'])} 属性）并 upsert 档案 {pid}。"
                       f"下一步 `chronicles import` 导图 → `project assign` → "
                       f"`annotate` → `profile learn`。")
    else:
        res["next"] = ("模板已挂，但**档案没建成**（见 profile_error）。"
                       "可先用 `chronicles profile new --name <名> --attrs …` 手工建档。")
    return res


def run_project(action: str, **kwargs) -> Dict[str, Any]:
    """跑一个动作，返回结果 + `exit_code`（唯一判定点）。"""
    # CLI 从 argparse 拿到的是 str，而存储层要 Path（`list_profiles` 会调 `.exists()`）
    # —— 在**唯一入口**处统一归一。
    if kwargs.get("profiles_dir"):
        kwargs["profiles_dir"] = Path(kwargs["profiles_dir"])
    table = {
        "new": _do_new, "list": _do_list, "show": _do_show,
        "assign": _do_assign, "images": _do_images, "template": _do_template,
    }
    fn = table.get(action)
    if fn is None:
        r: Dict[str, Any] = {"ok": False, "action": action,
                             "error": f"未知动作：{action}"}
        r["exit_code"] = EXIT_USAGE
        return r
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        r = fn(**kwargs)
    stray = buf.getvalue()
    if stray.strip():
        sys.stderr.write(stray)
    r.setdefault("action", action)
    r["exit_code"] = project_exit_code(r)
    return r


def project_exit_code(result: Dict[str, Any]) -> int:
    """★ 判定点只有这一处 —— `main` 与 `chronicles.cmd_project` 都只取用、不重判。

    「一张图都找不到」必须是 **3（缺料）**：那是路径写错或图还没导进来，
    agent 该去补输入，而不是以为归属写好了。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return EXIT_INVALID if result.get("invalid") else EXIT_MISSING
    return EXIT_OK


def advice_for(result: Dict[str, Any]) -> str:
    if result.get("next"):
        return str(result["next"])
    if result.get("ok"):
        return "完成。"
    return str(result.get("error") or "未知错误")


def _proj_label(v: Any) -> str:
    """`result['project']` 取「可读标签」—— **两种形状都吃得下**。

    ★ 为什么必须有它：`new` / `show` 放的是**栏目 dict**，而
      `assign` / `template` / `images` 放的是**字符串 id**。
      2026-09-15 实测：`images` 落进 `format_human` 的 `else` 兜底分支被当 dict 用
      ⇒ `AttributeError: 'str' object has no attribute 'get'` ⇒ **人读视图整段丢失、
      退出码从 0 变 1**（`--json` 路径也中招，因为它打完 JSON 还要打人读镜像）。
    """
    if isinstance(v, dict):
        return "  ".join(x for x in (str(v.get("id") or ""), str(v.get("name") or "")) if x)
    return str(v or "")


def format_human(result: Dict[str, Any]) -> str:
    rc = int(result.get("exit_code", project_exit_code(result)))
    L: List[str] = []
    L.append("=" * 64)
    L.append(f"栏目 · {result.get('action')} · 退出码 {rc}（{EXIT_MEANING.get(rc, rc)}）")
    L.append("=" * 64)
    act = result.get("action")
    if act == "list":
        for p in result.get("projects") or []:
            L.append(f"  · {p['id']}  {p['name']}  （图 {p['image_count']} / "
                     f"模板 {p['n_templates']}）")
        L.append(f"  共 {result.get('n_projects')} 个栏目")
    elif act == "assign":
        L.append(f"  目标栏目  : {result.get('project')}")
        L.append(f"  已归属    : {result.get('n_assigned')} 张")
        if result.get("n_missing"):
            L.append(f"  没找到    : {result.get('n_missing')} 张")
        if result.get("assigned"):
            for nm in (result["assigned"] or [])[:10]:
                L.append(f"    · {nm}")
    elif act == "template":
        L.append(f"  目标栏目  : {result.get('project')}")
        L.append(f"  样式标签  : {result.get('label')}")
        L.append(f"  属性      : {' / '.join(result.get('attrs') or [])}")
        prof = result.get("profile") or {}
        if prof:
            L.append(f"  档案      : {prof.get('profile_id')}  {prof.get('name')}")
        if result.get("profile_error"):
            L.append(f"  ⚠ 档案未建 : {result['profile_error']}")
    elif act == "images":
        # ★ 本支的 `project` 是**字符串 id**（见 `_proj_label` 的说明）——
        #   不许并进下面的 `else`，那正是 2026-09-15 那个 rc 0→1 的缺陷。
        L.append(f"  栏目      : {_proj_label(result.get('project'))}")
        if result.get("n_images") is not None:
            L.append(f"  已归属    : {result.get('n_images')} 张")
        names = result.get("images") or []
        for nm in names[:10]:
            L.append(f"    · {nm}")
        if len(names) > 10:
            L.append(f"    …（其余 {len(names) - 10} 张略；要全量用 --json）")
    else:
        L.append(f"  栏目      : {_proj_label(result.get('project'))}")
        if result.get("reused"):
            L.append("  （同名已存在，已复用，未改动）")
        for s in result.get("styles") or []:
            L.append(f"    · 样式 {s.get('label')} → 档案 {s.get('profile_id') or '（无）'}")
        if result.get("dry_run"):
            L.append("  （干跑：未落盘）")
    L.append(f"  → {advice_for(result)}")
    return "\n".join(L)


# ============================================================
# 入口
# ============================================================
# （`_Parser` = `cli_contract.Parser`，已在文件头随词表一并 import。）


def add_subcommands(sub, func=None) -> None:
    """把全部动作挂到给定的 subparsers 对象上。

    ★ **单一来源**：`chronicles.py` 与本模块的 `build_parser` 都调它 ——
      参数名与帮助文本不会在两处漂移（`CLAUDE.md` §60）。
    `func` 给定 → 设 `func=<可调用>`（供 `chronicles` 统一分派）；
    缺省 → 设成**动作名字符串**（供本模块 `main` 自己分派）。
    """

    def _done(p, action):
        p.set_defaults(func=func if func is not None else action)

    def _json(p):
        p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("new", help="建专项栏目")
    p.add_argument("--name", required=True, help="栏目名")
    p.add_argument("--desc", default="", help="栏目描述")
    p.add_argument("--color", default="#4A7FCB", help="栏目色（界面用，不影响命令面）")
    p.add_argument("--tags", nargs="*", default=None, help="标签")
    p.add_argument("--ocr-backend", default=None, help="该栏目固定用哪个 OCR 通道（缺省跟随全局）")
    p.add_argument("--force-new", action="store_true",
                   help="同名栏目存在时仍另建一个（默认幂等复用）")
    p.add_argument("--dry-run", action="store_true", help="只算不落")
    _json(p)
    _done(p, "new")

    p = sub.add_parser("list", help="列出全部栏目")
    _json(p)
    _done(p, "list")

    p = sub.add_parser("show", help="看一个栏目（模板样式 + 归属）")
    p.add_argument("--project", required=True, help="栏目 id 或名称")
    p.add_argument("--with-images", action="store_true", help="附带列归属的图")
    _json(p)
    _done(p, "show")

    p = sub.add_parser("assign", help="归栏目（图 → 栏目；缺它 triage --project 取空）")
    p.add_argument("--project", required=True, help="栏目 id 或名称")
    p.add_argument("--images", nargs="+", default=None,
                   help="图路径或图名（目录则扫顶层图像；取 basename 作 image_name）")
    p.add_argument("--names", nargs="+", default=None, help="直接给 image_name")
    p.add_argument("--unassign", action="store_true", help="反过来：解除归属")
    p.add_argument("--dry-run", action="store_true", help="只算不落")
    _json(p)
    _done(p, "assign")

    p = sub.add_parser("images", help="列出栏目归属的图")
    p.add_argument("--project", required=True, help="栏目 id 或名称")
    _json(p)
    _done(p, "images")

    p = sub.add_parser("template", help="传汇总模板 → 挂样式 + 建档案（②的完整形态）")
    p.add_argument("--project", required=True, help="栏目 id 或名称")
    p.add_argument("--file", required=True, help="模板文件（.docx/.xlsx/.md）")
    p.add_argument("--profiles-dir", default=None, help="档案目录（测试隔离用）")
    p.add_argument("--dry-run", action="store_true", help="只解析，不挂不建")
    _json(p)
    _done(p, "template")


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="project_cli",
        description="专项栏目命令面：建栏目 / 传模板建档案 / 归栏目",
        epilog="退出码：0 成功 · 3 缺料 · 4 校验不过 · 1 内部错误 · 64 用法错误",
    )
    sub = ap.add_subparsers(dest="project_cmd", required=True, metavar="<action>")
    add_subcommands(sub)
    return ap


def kwargs_from_args(a: argparse.Namespace) -> Dict[str, Any]:
    """Namespace → `run_project` 的 kwargs。

    ★ **单一来源**：`chronicles.py` 也调它（§60）。
    """
    if a.project_cmd == "new":
        return {"name": a.name, "description": a.desc, "color": a.color,
                "tags": a.tags, "ocr_backend": a.ocr_backend,
                "force_new": a.force_new, "dry_run": a.dry_run}
    if a.project_cmd == "list":
        return {}
    if a.project_cmd == "show":
        return {"project": a.project, "with_images": a.with_images}
    if a.project_cmd == "assign":
        return {"project": a.project, "images": a.images, "names": a.names,
                "unassign": a.unassign, "dry_run": a.dry_run}
    if a.project_cmd == "images":
        return {"project": a.project}
    return {"project": a.project, "file": a.file, "dry_run": a.dry_run,
            "profiles_dir": getattr(a, "profiles_dir", None)}


# 兼容旧名
_kwargs_for = kwargs_from_args


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(list(argv) if argv is not None else None)
    r = run_project(a.func, **_kwargs_for(a))
    rc = int(r.get("exit_code", EXIT_INTERNAL))
    if not a.as_json:
        print(format_human(r))
        return rc
    payload = dict(r)
    payload["advice"] = advice_for(r)
    payload["exit_meaning"] = EXIT_MEANING.get(rc)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(format_human(r), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
