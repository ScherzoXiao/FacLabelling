# -*- coding: utf-8 -*-
"""annotate_server —— 技能包形态的**人机交互面**（2026-09-16 重写）。

★★ 定位

    只服务「**人在页面上做标注**」这一件事，即用户场景的第 7–15 步：
    调出页面 → 画框 / 输文本 / 自由造属性 → 递交金标准页 →
    看规则 → 看自动标注结果 → 逐条裁决 → 回写下一轮金标准。

    **每条路由都转调内核模块的现有实现**（`CLAUDE.md` §60 单一实现）：
    本文件应当**零业务逻辑** —— 若这里出现一段"只有本面带才有的判断"，
    那就是 bug，不是特性。

★★ 与独立应用版（`app.py`）的关系 —— 三条边界

    | 面 | 独立应用版 app.py | 本文件（技能包交互面） |
    |---|---|---|
    | 规模 | 7458 行 / 117 路由 / 73 个顶层摄取函数 | 本文件；路由按**资源**分组 |
    | 摄取 | 剪贴板轮询 + 文件夹监听 + `InboxHandler` + OCR 校验 | **不带**：由 agent 触发 `chronicles import` / `ocr` |
    | 功能 | 聊天 / 导出 / 统计 / 飞轮编排 / 图片管理 | **不带**：只留标注所必需 |

    ⇒ 两个面**共用内核**、**不共享代码**：本文件**不 import `app`**
      （`app` 会把 `rag` / `chat_*` / `reassembler` / `batch_import` 一并拖进来）。

★★ 为什么交互面必须重写而不是搬（2026-09-16 实证）

    `app.py` 的标注端点本身只有 **425 行 = 5.7%**，18/18 全部委托核心模块、
    **零内联业务逻辑**（《技能包形态可行性论证》§1.3）。
    但它们**长在一个 7458 行的应用骨架里**：只要 `import app`，就同时得到
    摄取双通道、聊天、导出、RAG —— 技能包要的是那 5.7%，不是那 94.3%。

★ 形态边界（**明示、不静默**）

    少数端点在本形态下**有意少做**一段（见 `/api/save` 的 `degraded` 字段）。
    少做的地方一律**在响应里报出来**，不静默降级。

★ 前端契约（本面**必须逐字满足**，否则 `annotate.html` 要改）

    页面认的是 URL 与响应形状，不是哪个后端。故路由路径与响应结构
    **逐字沿用**（已在 `_probe/n7.txt` 逐条钉死），重写的是**代码组织**。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["bind_root", "create_app", "serve", "main", "ENDPOINTS", "default_root"]

log = logging.getLogger("chronicles.annotate_server")

# ============================================================
# 0. 内核句柄与路径（`bind_root()` 填充）
# ============================================================
#: 内核模块句柄。**只有** `bind_root()` 写它 —— 这样"根在哪"是一个可测的输入，
#: 而不是 import 期的副作用。
K: Any = None

# 路径常量**做成模块全局**（不是函数内局部），理由与 `app.py` 同：
# 测试靠 `monkeypatch.setattr(本模块, "MANUAL_ANNOT_DIR", tmp)` 做隔离，
# 路由**在调用时**读全局并把值**显式传**给内核 —— 不传就等于绕过那份 patch、
# 回落到真实金标准目录（这个坑在 app.py 上踩过一次，别重踩第二遍）。
PROJECT_ROOT: Optional[Path] = None
INBOX: Optional[Path] = None
OUTBOX: Optional[Path] = None
IMAGE_EXTS: set = set()
MANUAL_ANNOT_DIR: Optional[Path] = None
PROFILE_STORE_DIR: Optional[Path] = None
TEMPLATE_STORE_DIR: Optional[Path] = None
OUTPUT_XLSX: Optional[Path] = None
API_TOKEN_FILE: Optional[Path] = None
API_TOKEN: Optional[str] = None

# 草稿 / 裁决 / 归档 / 结构层目录：**显式传**给 adjudicate（写穿防线，2026-09-26）。
# 缺这组全局，`/api/preannotations/*` 三条路由会回落到 adjudicate 模块默认值（= 代码根）；
# 一旦绑到「数据根 ≠ 代码根」的部署（分发包 / 沙箱）就会写穿真实数据
# （P1 决定性实验附带发现；修法与 `_probe/p1_thin_host` 同款）。
DRAFTS_DIR: Optional[Path] = None      # 草稿层 data/preannotations
STATUS_PATH: Optional[Path] = None     # 裁决状态 data/adjudication/status.json
ARCHIVE_DIR: Optional[Path] = None     # 覆盖前归档 _archive
STRUCTURED_DIR: Optional[Path] = None  # 页几何 data/structured
DRIFT_DIR: Optional[Path] = None       # 版式漂移状态 data/drift
VALIDATION_DIR: Optional[Path] = None  # 档案级回验读数 data/validation
RESULTS_DIR: Optional[Path] = None     # 层 3 产物 data/results（reassign 的落点之一）
NOTES_DIR: Optional[Path] = None       # 页级备注 manual_annotations/notes（只追加）

PAGE_DIR: Optional[Path] = None       # 模板目录（页面资产）
STATIC_DIR: Optional[Path] = None     # 静态目录（bauhaus.css）

_excel_lock = threading.Lock()
_excel_writer = None                  # ExcelWriter 懒建（内核类）


def bind_root(root, *, page_dir=None, static_dir=None,
              init_token: bool = True) -> None:
    """把内核绑定到一个项目根：注入模块搜索路径 + 取常量 + 建 token。

    ★ 为什么是显式绑定而不是 import 期决定：技能包要能对**任意**项目根工作
      （换机器 / 分发时只改 `--root`），且测试能指向临时目录。
    """
    global K, PROJECT_ROOT, INBOX, OUTBOX, IMAGE_EXTS, MANUAL_ANNOT_DIR
    global PROFILE_STORE_DIR, TEMPLATE_STORE_DIR, OUTPUT_XLSX, API_TOKEN_FILE
    global API_TOKEN, PAGE_DIR, STATIC_DIR
    global DRAFTS_DIR, STATUS_PATH, ARCHIVE_DIR, STRUCTURED_DIR
    global DRIFT_DIR, VALIDATION_DIR
    global RESULTS_DIR, NOTES_DIR

    root = Path(root).resolve()
    if not (root / "config.py").exists():
        raise SystemExit(f"[annotate_server] 不像项目根（没有 config.py）: {root}")

    # 内核模块都在项目根下；插到 sys.path **最前**，避免被同名模块挡住
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    else:                                   # 已在里面但不在最前 → 提到最前
        sys.path.remove(str(root))
        sys.path.insert(0, str(root))

    import config                       # noqa: E402  （必须在 sys.path 就位后）
    import api_auth                     # noqa: E402
    import annotate_ops                 # noqa: E402
    import manual_annotate              # noqa: E402
    import adjudicate                   # noqa: E402
    import profile_store                # noqa: E402
    import template_store               # noqa: E402
    import data_io                      # noqa: E402
    import overview_stats               # noqa: E402
    import drift                        # noqa: E402
    import ocr_state                    # noqa: E402

    class _Kernel:                      # 轻量句柄容器（不是模块的再导出）
        pass

    k = _Kernel()
    for m in (config, api_auth, annotate_ops, manual_annotate, adjudicate,
              profile_store, template_store, data_io, overview_stats, drift,
              ocr_state):
        setattr(k, m.__name__.split(".")[-1], m)
    K = k

    PROJECT_ROOT = Path(config.BASE_DIR)
    INBOX = Path(config.INBOX)
    OUTBOX = Path(config.OUTBOX)
    IMAGE_EXTS = set(config.IMAGE_EXTS)
    MANUAL_ANNOT_DIR = Path(config.MANUAL_ANNOT_DIR)
    OUTPUT_XLSX = Path(config.OUTPUT_XLSX)
    PROFILE_STORE_DIR = Path(profile_store.DEFAULT_PROFILES_DIR)
    TEMPLATE_STORE_DIR = Path(template_store.DEFAULT_TEMPLATES_DIR)
    API_TOKEN_FILE = api_auth.token_file_for(PROJECT_ROOT)

    # 草稿 / 状态 / 归档 / 结构层：全部从数据根派生，随后**显式传**给 adjudicate
    #（写穿防线，见模块头「preannotations 路由」段注释）
    DRAFTS_DIR = PROJECT_ROOT / "data" / "preannotations"
    STATUS_PATH = PROJECT_ROOT / "data" / "adjudication" / "status.json"
    ARCHIVE_DIR = PROJECT_ROOT / "_archive"
    STRUCTURED_DIR = PROJECT_ROOT / "data" / "structured"
    DRIFT_DIR = PROJECT_ROOT / "data" / "drift"
    VALIDATION_DIR = PROJECT_ROOT / "data" / "validation"
    # reassign 要改的产物目录与页级备注目录：同样从数据根派生、路由里**显式传**
    RESULTS_DIR = PROJECT_ROOT / "data" / "results"
    NOTES_DIR = MANUAL_ANNOT_DIR / "notes"   # 随 MANUAL_ANNOT_DIR 被 patch（测试沙箱）

    # 页面与静态资产：默认取项目根下的 templates/ static/。
    # ★ 允许外部指定 ⇒ 将来技能包出自己的页面（场景专项）时，
    #   把 page_dir 指到技能包目录即可，**不必动项目根的模板**。
    PAGE_DIR = Path(page_dir) if page_dir else (PROJECT_ROOT / "templates")
    STATIC_DIR = Path(static_dir) if static_dir else (PROJECT_ROOT / "static")

    if init_token:
        API_TOKEN = api_auth.get_or_create_api_token(API_TOKEN_FILE, logger=log)
    log.info("[bind] root=%s  page_dir=%s", PROJECT_ROOT, PAGE_DIR)


def _kernel():
    if K is None:
        raise RuntimeError("bind_root() 尚未调用 —— 内核未绑定")
    return K


def _init_excel():
    """懒建 ExcelWriter（校对结果的存储层，内核类，非应用私有）。"""
    global _excel_writer
    with _excel_lock:
        if _excel_writer is None:
            from excel_writer import ExcelWriter
            _excel_writer = ExcelWriter(OUTPUT_XLSX)
    return _excel_writer


# ============================================================
# 1. 装配
# ============================================================
def create_app(*, root=None, page_dir=None, static_dir=None) -> Any:
    """构造 WSGI 应用。

    `root` 不给则要求已 `bind_root()` 过（测试两用：先绑临时根，再建 app）。
    """
    if root is not None or K is None:
        bind_root(root or PROJECT_ROOT, page_dir=page_dir, static_dir=static_dir)

    from flask import Flask, jsonify, request, send_from_directory, render_template

    app = Flask(__name__,
                template_folder=str(PAGE_DIR),
                static_folder=str(STATIC_DIR),
                static_url_path="/static")
    # 改页面不必重启（与 app.py 同）
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    # ---------- 安全：两道，顺序固定 ----------
    @app.before_request
    def _enforce_localhost():
        """P0-3：拒绝非本机回环地址的请求，防 DNS 重绑定。

        `/api/auth_token` 也走这道 —— 只有本地能拿到 token。
        白名单与响应体唯一实现在 `api_auth.check_host`。
        """
        r = _kernel().api_auth.check_host(request.host)
        if r is None:
            return None
        body, status = r
        log.warning("[host-check] 拒绝非本地请求 Host=%r remote=%s",
                    request.host, request.remote_addr)
        return jsonify(body), status

    @app.after_request
    def _no_cache_html(response):
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def _on_deny(status: int, method: str, path: str) -> None:
        log.warning("[auth] 拒绝 %s %s -> %s", method, path, status)

    # 门控：唯一一行框架胶水，语义全在 api_auth（常量时间比较 / 503 / 401 文案）
    require_auth = _kernel().api_auth.flask_require_auth(
        lambda: API_TOKEN, on_deny=_on_deny)

    # ========================================================
    # 资源 1：会话（token）
    # ========================================================
    @app.route("/api/auth_token", methods=["GET"])
    def r_token():
        """浏览器启动时取一次 token，之后所有写操作自动带上。"""
        body, status = _kernel().api_auth.token_payload(API_TOKEN)
        return jsonify(body), status

    # ========================================================
    # 资源 2：页面与原图
    # ========================================================
    @app.route("/annotate", strict_slashes=False)
    def r_page_latest():
        """不给页名 → 302 到最近一张可标注的图（outbox 优先，再 inbox，按 mtime）。"""
        latest = _kernel().annotate_ops.latest_image((OUTBOX, INBOX), IMAGE_EXTS)
        if latest is None:
            return ("<h3>没有可标注的图片</h3>"
                    "<p>先把材料放进 <code>inbox/</code>，或用命令面 "
                    "<code>chronicles import --images &lt;目录&gt;</code>。</p>",
                    404, {"Content-Type": "text/html; charset=utf-8"})
        from flask import redirect
        return redirect(f"/annotate/{latest.name}", code=302)

    @app.route("/annotate/<path:image_name>")
    def r_page(image_name):
        """标注页本体（页面资产来自 PAGE_DIR）。"""
        p = _kernel().annotate_ops.find_page_image(image_name, OUTBOX, INBOX)
        if p is None:
            return f"图片不存在: {image_name}", 404
        return render_template("annotate.html", image_name=p.name,
                               source_dir="outbox" if p.parent == Path(OUTBOX) else "inbox")

    @app.route("/annotate_raw/<path:image_name>")
    def r_raw(image_name):
        """页面要的原图字节（inbox / outbox 都可能）。"""
        p = _kernel().annotate_ops.find_page_image(image_name, OUTBOX, INBOX)
        if p is None:
            return "图片不存在", 404
        return send_from_directory(str(p.parent), p.name)

    # ========================================================
    # 资源 3：人工标注（= 金标准行）
    # ========================================================
    @app.route("/api/manual_annotations/<path:image_name>", methods=["GET"])
    def r_annot_list(image_name):
        """该页已有的人工标注（加载时回显）。

        ★ 返回结构**不含** `index`：那是命令面 `list_annotations` 专有的定位
          字段，界面不需要，加进来会动前端取数契约。
        """
        try:
            items = _kernel().manual_annotate.read_manual(image_name, MANUAL_ANNOT_DIR)
        except Exception as e:                      # noqa: BLE001
            return jsonify({"success": False, "error": str(e)}), 500
        return jsonify({"success": True, "items": items})

    @app.route("/api/manual_annotate", methods=["POST"])
    @require_auth
    def r_annot_add():
        """追加一条金标准行。

        ★ `manual_dir=MANUAL_ANNOT_DIR` **必须显式传** —— 见本文件顶部全局常量的说明。
        """
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "invalid JSON"}), 400

        lm = _kernel().manual_annotate
        image_name = str(data.get("image_name") or "")
        if image_name:
            coll = _kernel().annotate_ops.name_collisions(image_name, OUTBOX)
            if coll:
                log.warning("[manual_annot] 图名同槽: %s ↔ %s（共用 %s.jsonl）",
                            image_name, coll,
                            _kernel().data_io.safe_name(image_name))
        try:
            r = lm.add_annotation(
                image_name=image_name,
                box=data.get("box"),
                text=data.get("text", ""),
                attr=data.get("attr", ""),
                profile=data.get("profile", ""),
                note=data.get("note", ""),
                gid=data.get("gid", ""),
                xp=data.get("xp", ""),
                manual_dir=MANUAL_ANNOT_DIR,
            )
        except lm.ValidateError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        if not r.get("ok"):
            return jsonify({"success": False, "error": r.get("error")}), 500
        return jsonify({"success": True, "saved": r.get("saved")})

    @app.route("/api/rewrite_manual", methods=["POST"])
    @require_auth
    def r_annot_rewrite():
        """整份覆写（编辑 / 删除后用）。"""
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "invalid JSON"}), 400
        image_name = data.get("image_name")
        items = data.get("items")
        if not image_name or not isinstance(items, list):
            return jsonify({"success": False, "error": "image_name 和 items 必填"}), 400
        r = _kernel().annotate_ops.rewrite_manual(image_name, items, MANUAL_ANNOT_DIR)
        if not r.get("ok"):
            return jsonify({"success": False, "error": r.get("error")}), 500
        return jsonify({"success": True, "count": r.get("count")})

    # ========================================================
    # 资源 4：草稿（自动标注产物）与裁决
    # ========================================================
    @app.route("/api/preannotations/<path:image_name>", methods=["GET"])
    def r_draft_page(image_name):
        """某页的草稿层 + 裁决状态 + 统计（裁决界面唯一取数口）。

        `l0` 是页级自检旁证：**只读、可缺失、不参与任何判定**。
        """
        A = _kernel().adjudicate
        try:
            stem = A.stem_of(image_name)
            out = A.page_state(stem, image_name=image_name,
                               drafts_dir=DRAFTS_DIR, status_path=STATUS_PATH,
                               manual_dir=MANUAL_ANNOT_DIR, structured_dir=STRUCTURED_DIR,
                               drift_dir=DRIFT_DIR, image_dir=OUTBOX,
                               validation_dir=VALIDATION_DIR)
            try:
                import preannotate_gen as PG
                out["l0"] = PG.load_l0_meta(stem, DRAFTS_DIR)
            except Exception:                       # noqa: BLE001
                out["l0"] = None
            return jsonify(out)
        except Exception as e:                      # noqa: BLE001
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/preannotations/decide", methods=["POST"])
    @require_auth
    def r_draft_decide():
        """单条裁决：accept（可带人工覆盖的 text/box/attr）/ reject / reset。"""
        data = request.get_json(silent=True) or {}
        image_name = str(data.get("image_name", "")).strip()
        if not image_name:
            return jsonify({"success": False, "error": "image_name 必填"}), 400
        box = data.get("box")
        if box is not None and not isinstance(box, (list, tuple)):
            return jsonify({"success": False, "error": "box 必须是数组"}), 400
        A = _kernel().adjudicate
        r = A.adjudicate(
            A.stem_of(image_name), image_name, data.get("idx"),
            str(data.get("action", "")).strip(),
            text=(data.get("text") if isinstance(data.get("text"), str) else None),
            box=box,
            attr=(data.get("attr") if isinstance(data.get("attr"), str) else None),
            drafts_dir=DRAFTS_DIR, status_path=STATUS_PATH,
            manual_dir=MANUAL_ANNOT_DIR, archive_dir=ARCHIVE_DIR,
        )
        if r.get("ok"):
            _kernel().overview_stats.invalidate_cache()
        return jsonify(r), (200 if r.get("ok") else 400)

    @app.route("/api/preannotations/batch_decide", methods=["POST"])
    @require_auth
    def r_draft_batch_decide():
        """批量裁决：`confidences=["high"]` 一键采纳，或 `indices` 显式指定。"""
        data = request.get_json(silent=True) or {}
        image_name = str(data.get("image_name", "")).strip()
        if not image_name:
            return jsonify({"success": False, "error": "image_name 必填"}), 400
        confs = data.get("confidences")
        confs = confs if isinstance(confs, list) else ["high"]
        idxs = data.get("indices")
        if idxs is not None and not isinstance(idxs, list):
            return jsonify({"success": False, "error": "indices 必须是数组"}), 400
        A = _kernel().adjudicate
        r = A.batch_adjudicate(
            A.stem_of(image_name), image_name,
            action=str(data.get("action", "accept")).strip(),
            confidences=[str(c) for c in confs], indices=idxs,
            drafts_dir=DRAFTS_DIR, status_path=STATUS_PATH,
            manual_dir=MANUAL_ANNOT_DIR, archive_dir=ARCHIVE_DIR,
            structured_dir=STRUCTURED_DIR, drift_dir=DRIFT_DIR,
            image_dir=OUTBOX, validation_dir=VALIDATION_DIR)
        if r.get("ok"):
            _kernel().overview_stats.invalidate_cache()
        return jsonify(r), (200 if r.get("ok") else 400)

    @app.route("/api/preannotations/<path:image_name>/reassign", methods=["POST"])
    @require_auth
    def r_draft_reassign(image_name):
        """修改预标注条目的属性名（人显式修正 AI 标错的 attr）。

        body: {idx 或 uid, old_attr, new_attr, record?} —— 条目定位依据与
        裁决状态键同源（见 `adjudicate.entry_identity`）。目录参数**显式传**
        （DRAFTS_DIR / STATUS_PATH / ARCHIVE_DIR / RESULTS_DIR），不依赖内核默认值。
        """
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "invalid JSON"}), 400
        A = _kernel().adjudicate
        if not str(data.get("new_attr") or "").strip():
            return jsonify({"success": False, "error": "new_attr 必填"}), 400
        if data.get("idx") is None and not str(data.get("uid") or "").strip():
            return jsonify({"success": False, "error": "idx 与 uid 至少给一个"}), 400
        rec = data.get("record")
        if rec is not None and not isinstance(rec, int):
            return jsonify({"success": False, "error": "record 必须是整数"}), 400
        r = A.reassign_attr(
            A.stem_of(image_name), image_name,
            new_attr=str(data.get("new_attr") or ""),
            idx=data.get("idx"),
            old_attr=(data.get("old_attr") if isinstance(data.get("old_attr"), str)
                      else None),
            uid=data.get("uid"), record=rec,
            drafts_dir=DRAFTS_DIR, status_path=STATUS_PATH,
            results_dir=RESULTS_DIR, archive_dir=ARCHIVE_DIR,
        )
        if not r.get("ok"):
            kind = str(r.get("error_kind") or "")
            status = 404 if kind == "missing" else (409 if kind == "conflict" else 400)
            return jsonify({"success": False, "error": r.get("error"),
                            "error_kind": kind or None}), status
        return jsonify({"success": True, **r})

    # ========================================================
    # 资源 9：页级备注（给 AI 的提醒通道；只追加，不删改）
    # ========================================================
    @app.route("/api/notes/<path:image_name>", methods=["GET"])
    def r_notes_list(image_name):
        """该页已存备注（文件缺失 → 空列表，不报错）。"""
        try:
            items = _kernel().annotate_ops.read_notes(image_name, NOTES_DIR)
        except Exception as e:                      # noqa: BLE001
            return jsonify({"success": False, "error": str(e)}), 500
        return jsonify({"success": True, "notes": items})

    @app.route("/api/notes/<path:image_name>", methods=["POST"])
    @require_auth
    def r_notes_add(image_name):
        """追加一条页级备注（body: {note, box?, attr?}；note 必填）。"""
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "invalid JSON"}), 400
        if not str(data.get("note") or "").strip():
            return jsonify({"success": False, "error": "note 必填"}), 400
        box = data.get("box")
        if box is not None and not (isinstance(box, (list, tuple)) and len(box) == 4):
            return jsonify({"success": False, "error": "box 必须是四数数组"}), 400
        r = _kernel().annotate_ops.add_note(
            image_name, str(data.get("note") or ""), NOTES_DIR,
            box=box, attr=str(data.get("attr") or "") if data.get("attr") else "")
        if not r.get("ok"):
            return jsonify({"success": False, "error": r.get("error")}), 400
        return jsonify({"success": True, "note": r.get("note")})

    # ========================================================
    # 资源 5：OCR 记录（页面上的红虚线参考底图）
    # ========================================================
    @app.route("/api/ocr_records", methods=["GET"])
    def r_ocr_records():
        """`?image=<image_name>` → 该图 OCR 记录 + 进度 + 栏目归属。

        数据源与桌面版**同一处**（xlsx 校对层；`corrected_text.py` 说明它是
        校正结果的落点）—— 不另立第二套读取实现。
        """
        image_name = request.args.get("image")
        if not image_name:
            return jsonify({"success": False, "error": "image 必填"}), 400
        S = _kernel().ocr_state.ocr_state
        proj = _kernel().data_io.get_image_project(image_name)
        try:
            ew = _init_excel()
        except Exception as e:                      # noqa: BLE001
            log.warning("[ocr_records] ExcelWriter 不可用（不阻断标注）: %s", e)
            return jsonify({"success": True, "records": [],
                            "ocr_state": S.get(image_name),
                            "project_id": proj,
                            "note": "excel_unavailable"})
        from excel_writer import BusyLockError
        try:
            records = ew.list_records_by_image(image_name)
        except BusyLockError:
            return jsonify({"success": False, "error": "xlsx_busy",
                            "message": "OCR 正在写入，请 200ms 后重试",
                            "records": [], "ocr_state": S.get(image_name),
                            "project_id": proj}), 503
        except Exception as e:                      # noqa: BLE001
            return jsonify({"success": False, "error": str(e),
                            "ocr_state": S.get(image_name)}), 500
        return jsonify({"success": True, "records": records,
                        "ocr_state": S.get(image_name), "project_id": proj})

    # ========================================================
    # 资源 6：档案（= 隔离单位）与模板
    # ========================================================
    @app.route("/api/profiles", methods=["GET"])
    def r_profile_list():
        return jsonify({"success": True,
                        "profiles": _kernel().profile_store.list_profiles(PROFILE_STORE_DIR)})

    @app.route("/api/profiles", methods=["POST"])
    @require_auth
    def r_profile_create():
        data = request.get_json(silent=True) or {}
        try:
            prof = _kernel().profile_store.save_profile(data, PROFILE_STORE_DIR)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        return jsonify({"success": True, "profile": prof}), 201

    @app.route("/api/profiles/<profile_id>", methods=["GET"])
    def r_profile_get(profile_id):
        prof = _kernel().profile_store.get_profile(profile_id, PROFILE_STORE_DIR)
        if prof is None:
            return jsonify({"success": False, "error": f"档案不存在: {profile_id}"}), 404
        return jsonify({"success": True, "profile": prof})

    @app.route("/api/profiles/<profile_id>", methods=["DELETE"])
    @require_auth
    def r_profile_delete(profile_id):
        try:
            ok = _kernel().profile_store.delete_profile(profile_id, PROFILE_STORE_DIR)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        if not ok:
            return jsonify({"success": False, "error": "档案不存在"}), 404
        return jsonify({"success": True})

    @app.route("/api/profiles/<profile_id>/add_attr", methods=["POST"])
    @require_auth
    def r_profile_add_attr(profile_id):
        """页面「临时新建属性」→ 立刻写回档案（之后即刻可选）。"""
        data = request.get_json(silent=True) or {}
        try:
            prof = _kernel().profile_store.add_attr(
                profile_id, data.get("name", ""), data.get("desc", ""),
                PROFILE_STORE_DIR)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        if prof is None:
            return jsonify({"success": False, "error": f"档案不存在: {profile_id}"}), 404
        return jsonify({"success": True, "profile": prof})

    @app.route("/api/profiles/<profile_id>/import_template", methods=["POST"])
    @require_auth
    def r_profile_import_template(profile_id):
        """从模板表头一键导入属性集。"""
        data = request.get_json(silent=True) or {}
        template_id = (data.get("template_id") or "").strip()
        try:
            prof = _kernel().profile_store.import_attrs_from_template(
                profile_id, template_id,
                profiles_dir=PROFILE_STORE_DIR, templates_dir=TEMPLATE_STORE_DIR)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        if prof is None:
            return jsonify({"success": False, "error": f"档案不存在: {profile_id}"}), 404
        return jsonify({"success": True, "profile": prof})

    @app.route("/api/templates", methods=["GET"])
    def r_template_list():
        return jsonify({"success": True,
                        "templates": _kernel().template_store.list_templates()})

    # ========================================================
    # 资源 7：版式变体（漂移逃生门）
    # ========================================================
    @app.route("/api/drift/variant", methods=["POST"])
    @require_auth
    def r_drift_variant():
        """**人显式**登记 variant → 之后同类页不再判漂移。"""
        data = request.get_json(silent=True) or {}
        pid = _kernel().annotate_ops.resolve_profile_id(
            data.get("profile_id"), str(data.get("image_name") or ""))
        if not pid:
            return jsonify({"success": False, "error": "profile_id 必填"}), 400
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "name 必填（要人能看懂的名字）"}), 400
        stems = data.get("stems")
        if stems is not None and not isinstance(stems, list):
            return jsonify({"success": False, "error": "stems 必须是数组"}), 400
        feats = data.get("features")
        if feats is not None and not isinstance(feats, dict):
            return jsonify({"success": False, "error": "features 必须是对象"}), 400
        if not stems and not feats:
            return jsonify({"success": False, "error": "需给 stems 或 features 之一"}), 400
        r = _kernel().drift.register_variant(
            pid, name, note=str(data.get("note") or ""),
            stems=[str(s) for s in stems] if stems else None, features=feats)
        return jsonify(r), (200 if r.get("ok") else 400)

    # ========================================================
    # 资源 8：OCR 校对回写（页面上点红虚线框改字 → 存 xlsx）
    # ========================================================
    @app.route("/api/save", methods=["POST"])
    @require_auth
    def r_save():
        """把一条 OCR 记录的校正文本写回 xlsx（校对层）。

        ★ **本形态有意少做的部分**（在响应 `degraded` 里明示，不静默）：

          | 桌面版还会做 | 本形态 | 为什么 |
          |---|---|---|
          | 写 `train_data/_src/` 审计日志 | 不做 | 该日志的消费者是知识卡片链路（应用功能面） |
          | 后台重建 `data/units/` | 不做 | 同上；且默认 `use_llm=True` 会产生线上调用 |
          | 失效 RAG 语料缓存 | 不做 | 本形态无 RAG |

          ⇒ 校对**本身**没有少做：xlsx 落盘与 `corrected_text` 读取层完全一致。
        """
        data = request.get_json(force=True)
        try:
            record_id, corrected = _kernel().annotate_ops.parse_correction_payload(data)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400

        ew = _init_excel()
        ok = _kernel().annotate_ops.save_correction(ew, record_id, corrected)
        return jsonify({"success": bool(ok),
                        "degraded": ["audit_log", "units_rebuild", "rag_invalidate"]})

    # ========================================================
    # 端点表（供对账 / 自检；由 app.url_map 现推，不手写）
    # ========================================================
    app.config["CHRONICLES_SURFACE"] = "skill/chronicles-app/annotate_server.py"
    return app


def ENDPOINTS(app) -> List[str]:
    """返回 `METHOD PATH` 清单（**现推**，与真实注册表一致）。"""
    rows = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            rows.append(f"GET  {rule.rule}")
            continue
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        for m in methods:
            rows.append(f"{m:5s} {rule.rule}")
    return sorted(set(rows))


# ============================================================
# 2. CLI
# ============================================================
def serve(*, root, host=None, port=None, page_dir=None, open_browser=False):
    """前台起服务。

    ★★ 这里**故意不做 daemonize**（2026-09-16 实测）：`DETACHED_PROCESS` 逃不出
      托管 shell —— 命令一返回子进程就消失、端口 closed。⇒ **由宿主持有**：
      在会话里当长驻后台任务跑，或交给 `present_files` 的本地预览面板。

    ★ 顺序有讲究（2026-09-16 批次 C 修）：`create_app` **必须在前** ——
      它内部的 `bind_root()` 才把项目根插进 `sys.path`；以真子进程拉起时
      `sys.path[0]` 是**本文件所在目录**（`skill/chronicles-app/`），不是项目根，
      所以原先这里的 `import config` 会 `ModuleNotFoundError`。
      上轮自证走的是 `create_app + test_client`（同进程），**没暴露这一条**。
    """
    app = create_app(root=root, page_dir=page_dir)
    import config                                            # noqa: E402
    host = host or config.FLASK_HOST
    # ★ 缺省端口 = 技能包自有端口（SKILL_FLASK_PORT=5001），**不读壳的 FLASK_PORT**——
    #   与桌面壳完全解耦：壳在 5000 跑它的完整工作台，本面固定 5001，两边可并存。
    port = int(port or getattr(config, "SKILL_FLASK_PORT", 5001))
    if open_browser:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(
            f"http://{host}:{port}/annotate")).start()
    try:
        from waitress import serve as _wserve
        log.info("[serve] waitress %s:%s", host, port)
        _wserve(app, host=host, port=port, threads=8)
    except ImportError:
        log.info("[serve] flask dev server %s:%s", host, port)
        app.run(host=host, port=port, debug=False, threaded=True)


def default_root() -> Optional[Path]:
    """推断项目根；推不出来返回 `None`（**不猜一个本机路径**）。

    顺序：① 环境变量 `CHRONICLES_ROOT` ② 本文件上溯（`<root>/skill/chronicles-app/…`
    ⇒ `parents[2]`）且该处含 `config.py`。

    ★ 2026-09-16 批次 C：去掉原先硬编码的**本机项目路径**。理由两条——
      ① **分发纪律**：产物 / 代码里不得留本机私人信息（连注释里也不留）；
      ② 分发包里那条路径**根本不存在**，留着的实际效果是
         「报错信息把人指向另一台机器上的目录」。
    """
    env = (os.environ.get("CHRONICLES_ROOT") or "").strip()
    if env:
        return Path(env)
    try:
        cand = Path(__file__).resolve().parents[2]
        if (cand / "config.py").exists():
            return cand
    except Exception:                                        # pragma: no cover
        pass
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="annotate_server",
        description="技能包形态的标注交互面（只服务标注页；不 import app）")
    ap.add_argument("--root", default=None,
                    help="项目根（须含 config.py）；缺省取 $CHRONICLES_ROOT，"
                         "否则从本文件位置上溯")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--page-dir", default=None,
                    help="页面目录（默认 <root>/templates）")
    ap.add_argument("--open", action="store_true", help="起服务后开浏览器")
    ap.add_argument("--list-routes", action="store_true",
                    help="只打印端点表后退出（供对账 / 自检）")
    a = ap.parse_args(argv)

    root = Path(a.root).expanduser() if a.root else default_root()
    if root is None or not (root / "config.py").exists():
        sys.stderr.write(
            "[annotate_server] 找不到项目根（须含 config.py）：\n"
            "  用 --root <路径> 指定，或设环境变量 CHRONICLES_ROOT\n")
        return 64                                              # 用法错误（与命令面同表）

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.list_routes:
        app = create_app(root=root, page_dir=a.page_dir)
        print(f"# 交互面端点表（{app.config['CHRONICLES_SURFACE']}）")
        for row in ENDPOINTS(app):
            print("  " + row)
        return 0
    serve(root=root, host=a.host, port=a.port, page_dir=a.page_dir,
          open_browser=a.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
