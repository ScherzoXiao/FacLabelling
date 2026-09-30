# -*- coding: utf-8 -*-
"""ui_cli —— 界面通道：**给地址 / 起服务**（技能包 G1 落地 · 2026-09-16）。

★★ 裁定变更（用户 2026-09-16 二次裁定，**旧裁定作废**）：
  旧裁定「给地址，不替人起服务」（同日早些时候的裁定②）**已废除**。原话：

  > ①G1：同意加 `chronicles ui [--serve]`，且老裁定已经明显阻碍到了用户对产品的
  > 顺利体验，我决定修改该裁定，**允许在运行管线中的必要节点起服务**。

  ⇒ 由「只给地址」改为「**默认仍只给地址；显式 `--serve` 才起**」。
  默认不改变旧行为，这是为了不让"顺手起服务"变成新的默认副作用。

★ 旧裁定里**仍然有效的那一半**（不随之作废）——**起之前必须先探测、能复用就复用**：
  桌面壳（Electron）可能已经拉起后端；会话再起一个会造成**双实例**，
  并把壳的「重启本地服务」按钮顶成 attach 模式而失效
  （见 skill `persistent-service-in-workbuddy-shell` §4.8：**壳开着就直接用它**）。

★★ 形态开关（2026-09-16 批次 C）：本模块**不复制任何 Flask 逻辑** ——
  「起服务」= 以子进程拉起**某个已有的入口脚本**，本模块只做三件事：
  **探测、拉进程、等端口通**。可拉的面有两个：

      app    `<项目根>/app.py`                                    ← 完整应用（桌面壳的后端）
      skill  `<项目根>/skill/chronicles-app/annotate_server.py`   ← 技能包自有交互面

  缺省 `auto`：**app.py 在就用它、不在就落到 skill 面**。这样
  ①开发机上行为与今天相同；②分发包里**没有 `app.py`** 也不会报错 —— 那正是
  「技能包分发时的真断点」（批次 B 遗留）。
  ★ **不做环境探测**（不按端口 / 进程名 / 命令行猜形态）：猜测会在"两个面都能起"
  时给出不确定答案，而它决定的是**起哪个后端** —— 猜错是静默起错。

退出码
    0   成功（含「已在跑，直接复用」）
    1   起服务失败（子进程早退 / 超时端口未通）
    64  用法错误

用法
    python ui_cli.py --json                         # 只报地址 + 状态
    python ui_cli.py --serve --json                 # 起服务（已跑则复用）
    python ui_cli.py --serve --stem <页名> --json    # 起服务 + 报该页的标注地址
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 退出码契约：正本在 `cli_contract`（2026-09-24 收敛）----
# ★ ui 面只会出 0 / 1 / 64：起服务失败判 1（内部错误）不是 3（缺料）——下一步动作不同。
#   判定点仍是本模块的 `ui_exit_code` —— 收敛的是**词表**，不是判定。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_USAGE,               # noqa: E402
                          EXIT_MEANING, Parser as _Parser,                  # noqa: E402
                          capture_stray_stdout)                             # noqa: E402

SERVE_LOG_NAME = "_flask_serve.log"     # 子进程 stdout/stderr 落这里
POLL_INTERVAL = 0.4

# ---- 交互面（2026-09-30 统一裁定：chronicles 通道**只有**技能包自有面）----
# ★ 2026-09-16 批次 C 的 `--surface auto|app|skill` 三态开关已随裁定移除：
#   「本机落壳、分发落 skill」的分叉正是执行混乱的来源。壳的后端（app.py，5000）
#   由桌面壳自己拉起/重启；本通道恒拉 skill/chronicles-app/annotate_server.py
#   （端口 5001），本机与分发包行为完全一致。
SURFACE_AUTO = "auto"                   # 兼容旧签名：auto = skill（不再看 app.py）
SURFACE_SKILL = "skill"                 # 技能包自有交互面（唯一形态）
SURFACE_CHOICES = (SURFACE_AUTO, SURFACE_SKILL)
SKILL_SURFACE_REL = Path("skill") / "chronicles-app" / "annotate_server.py"
SKILL_PORT = 5001
SKILL_BASE_URL = "http://127.0.0.1:5001"


# ============================================================
# 探测（转调既有实现，不另写一份 TCP 逻辑）
# ============================================================
def base_url() -> str:
    """技能包标注面基址（**5001**，与壳的 5000 无关）。`CHRONICLES_BASE_URL` 可覆盖。"""
    env = (os.environ.get("CHRONICLES_BASE_URL") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        import config as CONFIG
        return f"http://{CONFIG.FLASK_HOST}:{CONFIG.SKILL_FLASK_PORT}"
    except Exception:                                        # pragma: no cover
        return SKILL_BASE_URL


def candidate_urls(base: Optional[str] = None) -> List[str]:
    """探测候选：技能包面**单端口**（5001）。

    ★ 旧版会连壳的 5000 一起探（那时 auto 可能落到壳面）；统一裁定后本通道
      与壳无关——壳在不在跑、跑在哪个端口，都不是这里的事。
    """
    out: List[str] = []
    b = base or base_url()
    if b and b not in out:
        out.append(b)
    return out


def is_up(base: Optional[str] = None, timeout: float = 0.6) -> bool:
    """任一候选地址通即算在跑。**转调** `next_step.server_up`（探测的唯一实现）。"""
    import next_step as NS
    for u in candidate_urls(base):
        try:
            if NS.server_up(u, timeout=timeout):
                return True
        except Exception:                                    # pragma: no cover
            continue
    return False


def live_url(base: Optional[str] = None, timeout: float = 0.6) -> str:
    """在跑的那个地址；没通 → 返回技能包基址（供报地址用）。"""
    import next_step as NS
    urls = candidate_urls(base)
    for u in urls:
        try:
            if NS.server_up(u, timeout=timeout):
                return u
        except Exception:                                    # pragma: no cover
            continue
    return urls[0] if urls else SKILL_BASE_URL


# ============================================================
# 形态：要拉哪个面（2026-09-30 统一裁定：只有技能包自有面）
# ============================================================
def script_for(surface: str = SURFACE_SKILL) -> tuple[Path, str]:
    """形态 → (要拉起的入口脚本, 实际形态)。

    ★★ **只有技能包自有面**：传 `auto` / `skill` 都落到
      `skill/chronicles-app/annotate_server.py`；传 `app` ⇒ **ValueError**
      （壳由桌面壳自己管理，本通道不再代拉——这就是「执行混乱」的根源，
      2026-09-30 裁定移除）。
    """
    if surface not in (SURFACE_AUTO, SURFACE_SKILL):
        raise ValueError(
            f"未知形态：{surface!r}（本通道只有技能包自有面；"
            f"app 壳由桌面壳自己管理，不再经 chronicles 拉起）")
    return ROOT / SKILL_SURFACE_REL, SURFACE_SKILL


def launch_argv(script: Path, *, root: Optional[Path] = None,
                open_browser: bool = False) -> List[str]:
    """拼子进程命令行（**唯一实现**，§60）。

    · 必带 `--root <项目根>`：自有面不假定自己在项目根下（分发可换机器）。
    · 必带 `--port 5001`（`config.SKILL_FLASK_PORT`）：**固定端口**，
      不读壳的 FLASK_PORT——本机壳开着（5000）也不冲突，探测/复用/地址
      生成全以 5001 为准。
    · 浏览器开关是 `--open` flag（缺省不开）。
    """
    return [sys.executable, str(script),
            "--root", str(root or ROOT),
            "--port", str(SKILL_PORT)] + (["--open"] if open_browser else [])


# ============================================================
# 起服务
# ============================================================
def _port_of(url: str) -> int:
    h = url.split("//", 1)[-1].split("/", 1)[0]
    _, _, p = h.partition(":")
    return int(p or "80")


def _log_tail(path: Path, n: int = 12) -> List[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln.rstrip() for ln in lines if ln.strip()][-n:]


def serve(*, wait: float = 30.0, open_browser: bool = False,
          base: Optional[str] = None, log_path: Optional[Path] = None,
          surface: str = SURFACE_SKILL) -> Dict[str, Any]:
    """起标注页服务（技能包自有面，端口 5001）。**已在跑则直接复用**（不起第二个）。

    `open_browser=False`（缺省）会设 `OPEN_BROWSER=0` —— 自动拉起浏览器在多数环境下不可靠，
      由 agent 起服务时不该弹窗；地址交给调用方转达（既有约定）。
    ★ 探测**先于**一切：5001 已在跑就复用。壳的 5000 与本通道无关——
      两边可并存（壳=完整工作台，5001=标注/验收页），互不顶掉。
    """
    res: Dict[str, Any] = {"started": False, "reused": False, "ok": False,
                           "pid": None, "url": base_url(), "log": None,
                           "surface": None, "surface_requested": surface,
                           "errors": []}

    # ★ 第一步恒为探测 —— 复用优先（5001 已有技能包面就直接用）
    if is_up(base):
        res.update(ok=True, reused=True, already_up=True, url=live_url(base),
                   surface=script_for(surface)[1],
                   next="服务已在运行，**未重复启动**。")
        return res

    log = Path(log_path or (ROOT / SERVE_LOG_NAME))
    res["log"] = str(log)

    env = dict(os.environ)
    env["OPEN_BROWSER"] = "1" if open_browser else "0"
    env["PYTHONIOENCODING"] = "utf-8"

    flags = 0
    if os.name == "nt":                                      # pragma: no cover
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    try:
        logf = open(log, "ab")
    except OSError as e:
        res["error"] = f"无法打开日志文件 {log}：{e}"
        return res

    # ---- 形态 → 脚本（放在 Popen 之前：脚本不存在时不必先付出一次 spawn 尝试）----
    try:
        script, actual = script_for(surface)
    except ValueError as e:
        logf.close()
        res["error"] = str(e)
        return res
    res["surface"] = actual
    if not script.exists():
        logf.close()
        res["error"] = (f"技能包交互面脚本不存在：{script}"
                        f"（分发包必须带 skill/ 目录；缺它标注页起不来）")
        return res
    argv = launch_argv(script, root=ROOT, open_browser=open_browser)
    res["script"] = str(script)
    res["argv"] = list(argv)

    try:
        # ★ 拉起入口脚本自身（不复制 Flask 逻辑）；cwd=项目根，它自己算相对路径
        proc = subprocess.Popen(
            argv,
            cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=flags)
    except Exception as e:                                   # noqa: BLE001
        logf.close()
        res["error"] = f"{type(e).__name__}: {e}"
        return res

    res["pid"] = proc.pid

    # 等端口通；同时盯子进程是否早退（早退必须立刻报，别干等满 wait）
    urls = candidate_urls(base)
    deadline = time.time() + max(1.0, float(wait))
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            try:
                logf.close()
            except Exception:                                # pragma: no cover
                pass
            res["exit_code_of_child"] = rc
            res["error"] = (f"服务进程启动后立即退出（rc={rc}）—— 多半是端口被占或"
                            f"依赖缺失，见日志：{log}")
            res["log_tail"] = _log_tail(log)
            return res
        if is_up(base):
            res.update(ok=True, started=True, url=live_url(base),
                       next="服务已起。地址直接用浏览器打开即可。")
            return res
        time.sleep(POLL_INTERVAL)

    try:
        logf.close()
    except Exception:                                        # pragma: no cover
        pass
    res["error"] = (f"等了 {wait:.0f}s 端口仍未通（探测过：{', '.join(urls)}）—— "
                    f"进程还在（pid={proc.pid}），见日志：{log}")
    res["log_tail"] = _log_tail(log)
    return res


# ============================================================
# 动作
# ============================================================
def _do_ui(*, serve_flag: bool = False, wait: float = 30.0,
           open_browser: bool = False, stem: str = "", stems=None,
           surface: str = SURFACE_SKILL) -> Dict[str, Any]:
    """`ui` 的唯一动作：报地址（可选起服务），并给出**具体页**的标注地址。

    `surface` 仅存兼容签名（统一裁定后只有 skill 面），照旧解析并回报——
    人得能看见「用哪个面、哪个端口」，不让它当暗牌。
    """
    res: Dict[str, Any] = {"ok": False, "action": "ui", "serve": bool(serve_flag),
                           "url": base_url(), "server_up": False,
                           "surface": None, "surface_requested": surface,
                           "surface_script": None, "annotate_urls": [], "errors": []}

    # 形态先落地（不起服务时也要报）——非法的 surface 在命令行层已被 choices 拦住，
    # 这里兜的是程序内调用（测试 / 别的模块 import 本模块直接用）。
    try:
        _script, res["surface"] = script_for(surface)
        res["surface_script"] = str(_script)
    except ValueError as e:
        res["errors"].append(str(e))
        return res

    if serve_flag:
        s = serve(wait=wait, open_browser=open_browser, surface=surface)
        res.update({k: v for k, v in s.items()
                    if k in ("started", "reused", "pid", "log", "error", "log_tail",
                             "exit_code_of_child", "surface", "script", "argv")})
        if not s.get("ok"):
            res["errors"].append(s.get("error") or "起服务失败")
            return res
        res["url"] = s.get("url") or res["url"]
        res["server_up"] = True
    else:
        # 只探测、不起 —— 但把"怎么起"显式告诉人（否则人会以为坏了）
        up = is_up()
        res["server_up"] = up
        if up:
            res["url"] = live_url()
            res["next"] = "服务已在运行；要某页的标注地址见 annotate_urls。"
        else:
            res["next"] = ("服务**没在运行**。要起它：`chronicles ui --serve`"
                           "（或按 G1 新裁定，在管线必要节点加 `--serve`）。")

    # 具体页的标注地址（给了 --stem 才出）—— 转调 draft_bridge，不自己拼 URL
    want: List[str] = []
    if stem:
        want.append(str(stem))
    want.extend([str(s) for s in (stems or [])])
    if want:
        try:
            import draft_bridge as DB
            b = res["url"]
            for s in want[:20]:
                res["annotate_urls"].append(DB.annotate_url(s, base=b))
        except Exception as e:                               # noqa: BLE001
            res["errors"].append(f"生成标注地址失败：{type(e).__name__}: {e}")
    res["ok"] = True
    return res


def ui_exit_code(result: Dict[str, Any]) -> int:
    """★ 判定点只有这一处 —— `main` 与 `chronicles.cmd_ui` 都只取用、不重判。

    「起服务失败」判 **1（内部错误）**、不是 3：缺料是"去补输入"，而这里是
    "环境/端口有问题"，下一步动作是**看日志**，不是去导材料。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return EXIT_INTERNAL
    return EXIT_OK


def advice_for(result: Dict[str, Any]) -> str:
    if result.get("next"):
        return str(result["next"])
    if result.get("error"):
        return str(result["error"])
    if result.get("ok"):
        return "完成。"
    return "未知错误"


def format_human(result: Dict[str, Any]) -> str:
    """人读视图。★ 显示层不参与判定：任何字段缺失都只降级、不得抛。"""
    rc = int(result.get("exit_code", ui_exit_code(result)))
    L: List[str] = []
    L.append("=" * 64)
    L.append(f"界面 · {result.get('action')} · 退出码 {rc}（{EXIT_MEANING.get(rc, rc)}）")
    L.append("=" * 64)
    L.append(f"  地址      : {result.get('url')}")
    L.append(f"  服务状态  : {'在运行' if result.get('server_up') else '未运行'}")
    if result.get("surface"):
        L.append(f"  交互面    : {result['surface']}"
                 f"（技能包自有面 · 端口 {SKILL_PORT} · 与桌面壳无关）")
    if result.get("reused"):
        L.append("  （已在运行，**未重复启动**）")
    if result.get("started"):
        L.append(f"  已启动    : pid={result.get('pid')}  日志={result.get('log')}")
    for u in (result.get("annotate_urls") or [])[:20]:
        L.append(f"    · {u}")
    for ln in (result.get("log_tail") or [])[:12]:
        L.append(f"    | {ln}")
    L.append(f"  → {advice_for(result)}")
    return "\n".join(L)


# ============================================================
# 入口
# ============================================================
# （`_Parser` = `cli_contract.Parser`，已在文件头随词表一并 import。）


def add_arguments(p: argparse.ArgumentParser) -> None:
    """把 `ui` 的参数挂上去。

    ★ **单一来源**：`chronicles.py` 与本模块的 `build_parser` 都调它 ——
      参数名与帮助文本不会在两处漂移。
    """
    p.add_argument("--serve", action="store_true", dest="serve_flag",
                   help="起标注页服务（★ 2026-09-16 新裁定；已在跑则复用，不重复起）")
    p.add_argument("--wait", type=float, default=30.0,
                   help="起服务后等端口通的秒数（默认 30）")
    p.add_argument("--open-browser", action="store_true",
                   help="起服务后顺带打开浏览器（默认不开）")
    p.add_argument("--stem", default="", help="顺带给出该页的标注地址")
    p.add_argument("--stems", nargs="+", default=None, help="顺带给出多页的标注地址")
    p.add_argument("--json", action="store_true", dest="as_json")


def kwargs_from_args(a: argparse.Namespace) -> Dict[str, Any]:
    """Namespace → `_do_ui` 的 kwargs。★ **单一来源**：`chronicles.py` 也调它（§60）。"""
    return {"serve_flag": a.serve_flag, "wait": a.wait,
            "open_browser": a.open_browser,
            "stem": a.stem, "stems": a.stems}


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="ui_cli",
        description="界面通道：给标注页地址；--serve 起服务（已在跑则复用）",
        epilog="退出码：0 成功 · 1 起服务失败 · 64 用法错误",
    )
    add_arguments(ap)
    ap.set_defaults(func=None)
    return ap


def run_ui(action: str = "ui", **kwargs) -> Dict[str, Any]:
    """跑一个动作，返回结果 + `exit_code`（唯一判定点）。"""
    if action != "ui":
        r: Dict[str, Any] = {"ok": False, "action": action,
                             "error": f"未知动作：{action}"}
        r["exit_code"] = EXIT_USAGE
        return r
    r = capture_stray_stdout(_do_ui, **kwargs)
    r.setdefault("action", "ui")
    r["exit_code"] = ui_exit_code(r)
    return r


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(list(argv) if argv is not None else None)
    r = run_ui("ui", **kwargs_from_args(a))
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
