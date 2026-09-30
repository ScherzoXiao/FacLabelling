# -*- coding: utf-8 -*-
"""api_auth —— HTTP 写操作的 token 门控（**框架无关**的单一实现）。

★★ 为什么有这个模块（2026-09-16 · 技能包形态重构 批次 1）

    同一份 token 语义原先**只存在于 `app.py` 里**（`get_or_create_api_token` +
    `require_auth`，约 45 行）。而技能包要拥有**自己的**交互面
    （`skill/chronicles-app/surface.py`），它**不得 import app**
    （`app.py` = 7458 行桌面应用层；`__import__` 会把 `rag` / `chat_*` /
    `reassembler` / `batch_import` 一并拖进来 —— 实测见 `_probe/m3.txt`）。
    若在交互面里照抄一份 → **第二份真相**（`CLAUDE.md` §60）。

    ⇒ 正确做法 = 把这份语义**下沉为内核**：两个消费者（桌面版 `app.py`、
    技能包交互面）**转调同一实现**。

★ 框架无关：本模块**不 import flask**（保持内核「零框架依赖」这条性质）。
  Flask 装饰器由 `flask_require_auth()` 工厂在**调用时**延迟导入并构造 ——
  于是「哪些方法放行 / 未初始化返 503 / 常量时间比较 / 401 文案」这些 **语义**
  只有一份，各宿主只留 2 行框架胶水（`require_auth = flask_require_auth(...)`）。

★ 契约（2026-09-16 逐字沿用 `app.py` 原语义，未改）：

    | 条件 | 结果 |
    |---|---|
    | `GET` / `HEAD` / `OPTIONS` | 放行（浏览器首次取 token 时还没有 token） |
    | 服务未初始化（token 为 `None`） | **503** `service_not_initialized` |
    | 带 `Authorization: Bearer <t>` 或 `X-API-Token: <t>` 且匹配 | 放行 |
    | 其余 | **401** `unauthorized` |

  安全性另有 `@app.before_request` 的 Host 头校验保证（非 localhost 到不了这里）。
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

__all__ = [
    "SAFE_METHODS",
    "token_file_for",
    "get_or_create_api_token",
    "extract_token",
    "gate",
    "token_payload",
    "flask_require_auth",
    "check_host",
]

#: 不需要 token 的方法（读操作 + 跨域 preflight）
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


# ============================================================
# Host 白名单（P0-3 防 DNS 重绑定；原 app._check_host 内联，2026-09-24 下沉）
# ============================================================
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_HOST_DENY_BODY = {"success": False, "error": "forbidden",
                   "message": "只接受 localhost 请求"}


def check_host(request_host) -> Optional[Tuple[Dict[str, Any], int]]:
    """Host 头白名单：本地形态 → `None`（放行）；其余 → `(响应体, 403)`。

    端口与 IPv6 方括号都剥掉再比：`127.0.0.1:5000` / `[::1]:8080` / `::1`。
    旧内联实现 `split(":")[0]` 把 `[::1]:8080` 切成 `[`、裸 `::1` 切成空串
    —— 文档契约写明放行 ::1，实现却拒绝，此处按契约修正（两份复抄同病）。
    """
    host = str(request_host or "").strip().lower()
    if host.startswith("["):
        host = host[1:host.index("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:                   # host:port 才剥端口；裸 IPv6 保留
        host = host.split(":", 1)[0]
    if host in _LOCAL_HOSTS:
        return None
    return (dict(_HOST_DENY_BODY), 403)


def token_file_for(project_root) -> Path:
    """项目根 → token 落点（`data/.api_token`，已 gitignore，权限 0600）。"""
    return Path(project_root) / "data" / ".api_token"


# ============================================================
# 取用（唯一实现；原先内联在 app.get_or_create_api_token）
# ============================================================
def get_or_create_api_token(token_file, logger=None) -> str:
    """已存在则读旧 token（**保持浏览器 session 不变**），否则生成新的。

    重启服务不重置。想强制重置：删掉该文件后重启。

    ★ `token_file` 必须由调用方传入（不是模块全局）—— 这样测试可以指向
      临时目录，且两个宿主（桌面版 / 技能包交互面）能各自决定落点。
    """
    p = Path(token_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            tok = p.read_text(encoding="utf-8").strip()
        except OSError:
            tok = ""
        if tok and len(tok) >= 32:
            return tok
    tok = secrets.token_hex(32)                      # 64 字符
    p.write_text(tok, encoding="utf-8")
    try:
        # Windows 上 os.chmod 只能改"只读"位；要 0600 需要额外操作（这里尽力）
        import os
        os.chmod(p, 0o600)
    except Exception:                                # pragma: no cover
        pass
    if logger is not None:
        logger.info("[auth] 生成新 API token: %s", p)
    return tok


def extract_token(headers: Mapping[str, str]) -> str:
    """从请求头取 token：优先 `Authorization: Bearer`，其次 `X-API-Token`。

    后者是给 curl 调试用的（少写一个前缀）。
    """
    auth = ""
    x_token = ""
    try:
        auth = headers.get("Authorization", "") or ""
        x_token = headers.get("X-API-Token", "") or ""
    except Exception:                                # pragma: no cover
        return ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    if x_token:
        return x_token.strip()
    return ""


# ============================================================
# 判定（唯一实现；纯函数，不碰框架、不碰全局）
# ============================================================
def gate(method: str, headers: Mapping[str, str],
         expected: Optional[str]) -> Optional[Dict[str, Any]]:
    """**唯一的判定点**。返回 `None` = 放行；否则返回 `{"status": .., "body": {..}}`。

    纯函数：同样的 (method, headers, expected) 恒得同一结论 —— 两个宿主
    不可能判出不同结果（这正是"转调不复制"要保住的东西）。
    """
    if str(method).upper() in SAFE_METHODS:
        return None
    if not expected:
        # 服务未初始化（token 未生成）：**拒绝**写操作，不静默放行
        return {"status": 503, "body": {
            "success": False,
            "error": "service_not_initialized",
            "message": "服务尚未初始化，请稍后重试",
        }}
    token = extract_token(headers)
    # 常量时间比较，防计时攻击
    if not token or not secrets.compare_digest(token, str(expected)):
        return {"status": 401, "body": {
            "success": False,
            "error": "unauthorized",
            "message": "需要 Bearer token（GET /api/auth_token 获取）",
        }}
    return None


def token_payload(expected: Optional[str]) -> Tuple[Dict[str, Any], int]:
    """`GET /api/auth_token` 的响应体 + 状态码（唯一实现）。"""
    if not expected:
        return {"success": False, "error": "service_not_initialized"}, 503
    return {
        "success": True,
        "token": expected,
        "message": ("把这个 token 存到 sessionStorage，所有后续 fetch 加 "
                    "'Authorization: Bearer <token>' 头"),
    }, 200


# ============================================================
# 框架胶水（**唯一一份**；flask 延迟导入 ⇒ 本模块仍框架无关）
# ============================================================
def flask_require_auth(get_token: Callable[[], Optional[str]],
                       on_deny: Optional[Callable[[int, str, str], None]] = None):
    """构造一个 Flask 装饰器；`get_token()` 由宿主提供（读它自己的模块全局）。

    ★ 为什么是**工厂**而不是直接一个装饰器：桌面版的 token 是模块全局
      `app.API_TOKEN`，且在 `main()` 里才赋值 ⇒ 必须**每次请求现取**，
      不能在建装饰器时取值（否则永远锁死在 `None`）。

    ★ 用法（宿主侧唯一一行）::

        require_auth = api_auth.flask_require_auth(lambda: API_TOKEN)
    """
    from functools import wraps                     # 标准库，非框架
    from flask import jsonify, request              # ★ 延迟导入：本模块 import 时零框架

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            denied = gate(request.method, request.headers, get_token())
            if denied is not None:
                if on_deny is not None:
                    try:
                        on_deny(denied["status"], request.method, request.path)
                    except Exception:                # pragma: no cover
                        pass
                return jsonify(denied["body"]), denied["status"]
            return f(*args, **kwargs)
        return decorated
    return decorator
