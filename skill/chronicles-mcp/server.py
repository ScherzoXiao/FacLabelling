# -*- coding: utf-8 -*-
"""chronicles MCP server —— 把统一命令面暴露给任意 MCP 客户端（技能包第 4 步）。

设计依据：`技能包程序设计_20260914.md` §7 第 4 步
          判据 = **任意 MCP 客户端能发现并调用**。

三条不可退让的纪律
-------------------------------------------------------------------
1. **参数定义只有一份**（本项目 §60 单一实现）。
   本文件**不手写任何工具的参数表** —— 工具清单与 `inputSchema` 全部
   **从 `chronicles.build_parser()` 的 argparse 对象里推导**出来。
   ⇒ 命令面加一个选项，MCP 面**自动跟着变**；不存在"两处漂移"。

2. **纯包装**（同 `chronicles.py`）。
   `tools/call` 只做：参数 → argv → `chronicles.main(argv)` → 读回它**已经打出**的 JSON。
   不复制任何业务逻辑，不重判任何退出码。

3. **退出码语义原样保留**（这是本产品最核心的对外契约）。
   工具结果里**永远**带 `exit` 与 `exit_meaning`；`isError` 只表示
   「这次调用本身坏了」（**1 内部错误 / 64 用法错误**），
   **不含** `2 面外拒答`（那是**正确行为**）、也不含 `3 缺料`（那是"去补料"）。
   ⇒ 把 `2` 当失败上报，agent 就会去"修"一个本来正确的行为。

零依赖
-------------------------------------------------------------------
不用 `mcp` SDK（本项目与项目 venv 都没有装它，也不该为打包引入）：
stdio 传输就是**一行一条 JSON-RPC 2.0**，直接手写。
⇒ 整个技能包保持 `requirements.txt` 不变即可分发。

用法
-------------------------------------------------------------------
    python server.py                      # 说 MCP（stdio），给客户端用
    python server.py --list-tools         # 不打 MCP，只把工具清单打出来（人读/测试）
    python server.py --root <项目根目录>
    python server.py --timeout 900

客户端配置（例：Claude Desktop / 任意 MCP 宿主）
    {
      "mcpServers": {
        "faclabelling": {
          "command": "python",
          "args": ["<技能包目录>/skill/chronicles-mcp/server.py",
                   "--root", "<项目根>"]
        }
      }
    }
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SERVER_NAME = "chronicles"
SERVER_VERSION = "0.1.0"
SCHEMA_VERSION = "0.1.0"

# 支持的协议版本（按新→旧）。客户端报的版本在表内就回它，否则回我们的最新版。
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL = SUPPORTED_PROTOCOLS[0]

# 这些选项**不暴露**给 agent：
#   --json  由本服务**强制**加（协议要求 stdout 只走 JSON-RPC，JSON 必须被捕获）
#   --help  由 tools/list 取代
_HIDDEN_OPTS = {"--json", "--help", "-h"}

# 参数名保留字（MCP 的 arguments 是 JSON object）
_TOOL_NAME_SEP = "_"


# ============================================================
# 一、从 argparse 推导工具清单（唯一来源：chronicles.build_parser）
# ============================================================
_PY2JSON = {int: "integer", float: "number", str: "string", bool: "boolean"}


def _json_type(action: argparse.Action) -> Dict[str, Any]:
    """argparse 的 type → JSON Schema 片段。"""
    t = getattr(action, "type", None)
    if t is None or t is str:
        return {"type": "string"}
    jt = _PY2JSON.get(t)
    return {"type": jt} if jt else {"type": "string"}


def _arg_name(a: argparse.Action) -> str:
    """argparse 动作 → **MCP 参数名**。

    ★ 取**长选项名去 `--`**（`--project` → `project`），**不用 `a.dest`**。
      为什么：本项目有几处 `dest` 与选项名不同（`triage` 的 `--project` 写成
      `dest="project_id"`）。若用 `dest` 当参数名，agent 在 description 里看到 `--project`、
      传参却必须写 `project_id` —— 一次真实调用就会撞 `未知参数`。
      **MCP 参数名与 CLI 长选项名逐字一致**，agent 看帮助就会用，不需要第二套记忆。
    """
    longs = [o for o in a.option_strings if o.startswith("--")]
    flag = longs[0] if longs else a.option_strings[0]
    return flag.lstrip("-").replace("-", "_")


def _flag_of(a: argparse.Action) -> str:
    longs = [o for o in a.option_strings if o.startswith("--")]
    return longs[0] if longs else a.option_strings[0]


def _schema_for(parser: argparse.ArgumentParser
                ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """一个叶子 parser → (JSON Schema, {参数名: CLI 选项名})。"""
    props: Dict[str, Any] = {}
    required: List[str] = []
    argmap: Dict[str, str] = {}

    for a in parser._actions:  # noqa: SLF001 —— 就是要内省 argparse 本身
        if not a.option_strings:
            continue
        if set(a.option_strings) & _HIDDEN_OPTS:
            continue
        dest = _arg_name(a)
        argmap[dest] = _flag_of(a)
        frag: Dict[str, Any] = {}

        if isinstance(a, argparse._StoreTrueAction):
            frag = {"type": "boolean", "default": False}
        elif isinstance(a, argparse._StoreFalseAction):
            frag = {"type": "boolean", "default": True}
        else:
            if a.nargs in ("+", "*"):
                item = _json_type(a)
                item.pop("default", None)
                frag = {"type": "array", "items": item}
            else:
                frag = _json_type(a)
            if a.choices is not None:
                frag["enum"] = list(a.choices)
            if a.default is not None and not isinstance(a.default, (list, tuple)):
                frag["default"] = a.default

        help_txt = (a.help or "").strip()
        alias = " / ".join(a.option_strings)
        frag["description"] = f"{alias}  {help_txt}".strip()
        props[dest] = frag
        if a.required:
            required.append(dest)

    schema: Dict[str, Any] = {"type": "object", "properties": props}

    # 互斥组：required=True → 恰好给一个；否则 → 不许同时给
    for g in parser._mutually_exclusive_groups:  # noqa: SLF001
        names = [_arg_name(x) for x in g._group_actions]
        names = [n for n in names if n in props]
        if len(names) < 2:
            continue
        if g.required:
            schema.setdefault("oneOf", []).extend({"required": [n]} for n in names)
            for n in names:
                if n in required:
                    required.remove(n)
        else:
            schema.setdefault("allOf", []).append(
                {"not": {"required": names}})
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)

    return schema, argmap


def _leaf_help(parser: argparse.ArgumentParser, name: str) -> str:
    """子命令在自己父 parser 上登记的 help。"""
    for a in getattr(parser, "_actions", []):
        ch = getattr(a, "choices", None)
        if ch and name in ch and getattr(a, "_choices_actions", None):
            for ca in a._choices_actions:  # noqa: SLF001
                if ca.dest == name:
                    return ca.help or ""
    return ""


def walk_tools(parser: argparse.ArgumentParser) -> List[Dict[str, Any]]:
    """递归展开子命令 → 每个**叶子命令**一个 MCP 工具。

    为什么按叶子展开而不是按顶层：顶层那几个动作组（`profile`/`project`/`annotate`）
    内部的动作之间**参数名有重叠**（`--name` 在 `profile new` 与 `project new` 都存在），
    合并成一个工具会有歧义；按叶子展开 = 与命令面 **1:1 对应**，参数名不会撞。
    """
    tools: List[Dict[str, Any]] = []

    def rec(p: argparse.ArgumentParser, path: List[str], chain_desc: List[str]) -> None:
        sub_actions = [
            a for a in p._actions  # noqa: SLF001
            if not a.option_strings and getattr(a, "choices", None)
            and isinstance(getattr(a, "choices", None), dict)
        ]
        if not sub_actions:
            schema, argmap = _schema_for(p)
            # 工具名只用 `[A-Za-z0-9_]`（`add-attr` → `add_attr`）：
            # MCP 允许连字符，但统一成下划线后「工具名 ↔ 命令路径」的映射是无歧义的。
            tname = _TOOL_NAME_SEP.join(
                ["chronicles"] + [x.replace("-", "_") for x in path])
            desc = " · ".join(x for x in chain_desc if x) or (p.description or "")
            tools.append({
                "name": tname,
                "command": path,
                "description": desc.strip(),
                "inputSchema": schema,
                # ★ 内部用（不进协议）：参数名 → CLI 选项名，避免在 description 上反解
                "arg_map": argmap,
                # 本叶在自己父 parser 上登记的 help：下降时已逐层收进 chain_desc，
                # 末项即叶项（旧写法拿叶 parser 当父查，恒空 —— 2026-09-24 测试面抓出）
                "human_help": chain_desc[-1] if chain_desc else "",
            })
            return
        for sa in sub_actions:
            for name, sp in sa.choices.items():
                rec(sp, path + [name], chain_desc + [_leaf_help(p, name)])

    rec(parser, [], [])
    tools.sort(key=lambda t: t["name"])
    return tools


# ============================================================
# 二、把 MCP arguments 还原成 argv
# ============================================================
def args_to_argv(tool: Dict[str, Any], arguments: Dict[str, Any]) -> List[str]:
    """工具参数 → `chronicles` 的 argv。**只做名字映射与类型校验，不做语义加工**。

    ★ 类型校验不是洁癖：`--inbox` 是**单值字符串**，若调用方给了 list，
      `str(val)` 会把它变成 `"['D:\\\\...']"` 这种**看上去能跑、实际取空**的路径
      （实测踩到：面外分诊因此报成"一个页都没读到"）。⇒ 宁可当场报错。
    """
    argv: List[str] = list(tool["command"])
    props = tool["inputSchema"]["properties"]
    amap = tool["arg_map"]
    for dest, val in (arguments or {}).items():
        if dest not in props:
            raise ValueError(
                f"未知参数 {dest!r}；本工具的参数见 tools/list（可用 {sorted(props)}）")
        if val is None:
            continue
        frag = props[dest]
        flag = amap[dest]
        jtype = frag.get("type")
        if jtype == "boolean":
            if val:
                argv.append(flag)
            continue
        if jtype == "array":
            if not isinstance(val, (list, tuple)):
                raise ValueError(f"参数 {dest!r}（{flag}）应给数组，收到 {type(val).__name__}")
            if not val:
                continue
            argv.append(flag)
            argv.extend(str(x) for x in val)
            continue
        if isinstance(val, (list, dict)):
            raise ValueError(
                f"参数 {dest!r}（{flag}）应给单值 {jtype}，收到 {type(val).__name__}")
        argv.append(flag)
        argv.append(str(val))
    argv.append("--json")          # ★ 强制：stdout 必须是纯 JSON，才能被捕获
    return argv


# ============================================================
# 三、运行一次调用（唯一入口 = chronicles.main）
# ============================================================
class Runner:
    def __init__(self, root: Path, max_output_chars: int = 20000) -> None:
        self.root = root
        self.max_output_chars = max_output_chars
        self._chronicles: Any = None

    def chronicles(self) -> Any:
        if self._chronicles is None:
            if str(self.root) not in sys.path:
                sys.path.insert(0, str(self.root))
            self._chronicles = __import__("chronicles")
        return self._chronicles

    def call(self, tool: Dict[str, Any],
             arguments: Dict[str, Any]) -> Dict[str, Any]:
        C = self.chronicles()
        argv = args_to_argv(tool, arguments)

        out_buf, err_buf = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), \
                    contextlib.redirect_stderr(err_buf):
                rc = int(C.main(argv))
            crashed = None
        except SystemExit as e:                      # argparse 用法错误走这里
            rc = int(e.code) if isinstance(e.code, int) else 64
            crashed = None
        except BaseException:                        # noqa: BLE001
            rc = 1
            crashed = traceback.format_exc()

        raw = out_buf.getvalue()
        human = err_buf.getvalue()
        payload: Optional[Any] = None
        try:
            payload = json.loads(raw) if raw.strip() else None
        except Exception:                            # noqa: BLE001
            payload = None

        meaning = C.EXIT_MEANING.get(rc, str(rc))
        text = self._render(rc, meaning, argv, payload, raw, human, crashed)
        res: Dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {
                "exit": rc, "exit_meaning": meaning, "ok": rc == 0,
                "argv": argv, "result": payload,
                "stderr": human.strip() or None,
                "crash": crashed,
            },
            # ★ 只有「调用本身坏了」才是 MCP 错误；
            #   2 面外拒答 / 3 缺料 / 4 校验不过是**正常结果**，语义在 exit 里。
            "isError": rc in (C.EXIT_INTERNAL, C.EXIT_USAGE),
        }
        if crashed is None:
            res["structuredContent"].pop("crash")
        return res

    def _render(self, rc: int, meaning: str, argv: List[str],
                payload: Optional[Any], raw: str, human: str,
                crashed: Optional[str]) -> str:
        L = [f"exit={rc} ({meaning})", f"argv={' '.join(argv)}"]
        if isinstance(payload, dict):
            nxt = payload.get("next")
            if nxt:
                L.append(f"next: {nxt}")
            if payload.get("ok") is False and payload.get("error"):
                L.append(f"error: {payload['error']}")
        body = raw
        if len(body) > self.max_output_chars:
            keep = self.max_output_chars
            body = (body[:keep]
                    + f"\n…[截断：完整 JSON 共 {len(raw)} 字符；"
                      f"structuredContent.result 里是完整内容]")
        L.append("\n--- stdout (JSON) ---")
        L.append(body if body.strip() else "(空)")
        if human.strip():
            h = human.strip()
            if len(h) > 4000:
                h = h[:4000] + "\n…[截断]"
            L.append("\n--- stderr (人读) ---")
            L.append(h)
        if crashed:
            L.append("\n--- 内部异常 ---")
            L.append(crashed)
        return "\n".join(L)


# ============================================================
# 四、MCP stdio 传输（一行一条 JSON-RPC 2.0）
# ============================================================
def _resp(mid: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    e: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": e}


def _exit_legend(C: Any) -> str:
    """退出码一览 —— ★ 从 `chronicles.EXIT_MEANING` **现推**（§60 单一实现）。

    这里曾经**手抄**过一遍（"0 成功 / 2 面外拒答（正确行为，不是失败）/ …"）。
    值与 `EXIT_MEANING` 当时相同，看着无害，但它是**第二份真相**：
    `EXIT_MEANING` 改一处，agent 读到的仍是这里那句旧话，而且**没有任何红**
    —— 本服务把退出码语义当对外契约（见文件头第 3 条），抄一份等于把契约拆成两份。

    现推的成本是 0：`Server.__init__` 本来就拿得到 `chronicles` 模块。
    顺序按**重要性**排（先正常结果、后错误），不按码值。
    """
    order = ("EXIT_OK", "EXIT_OUT_OF_SCOPE", "EXIT_MISSING",
             "EXIT_INVALID", "EXIT_INTERNAL", "EXIT_USAGE")
    parts: List[str] = []
    for name in order:
        rc = getattr(C, name)          # 改了名就**当场炸**，不静默丢一档
        parts.append(f"{rc} {C.EXIT_MEANING.get(rc, rc)}")
    return " / ".join(parts)


class Server:
    def __init__(self, runner: Runner,
                 out: Optional[io.TextIOBase] = None) -> None:
        self.runner = runner
        C = runner.chronicles()
        self.exit_legend = _exit_legend(C)
        self.EXIT_OUT_OF_SCOPE = C.EXIT_OUT_OF_SCOPE
        self.tools = walk_tools(C.build_parser())
        self.by_name = {t["name"]: t for t in self.tools}
        # ★ 传输**必须**用进程真正的 stdout。
        #   工具执行期间 `sys.stdout` 会被临时改道到缓冲区，
        #   若这里也读 `sys.stdout`，命令的输出就会混进协议流。
        self.out = out if out is not None else sys.__stdout__

    # -- 协议基础 --
    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if method == "initialize":
            want = (params.get("clientInfo") or {}) if isinstance(params, dict) else {}
            proto = params.get("protocolVersion") if isinstance(params, dict) else None
            version = proto if proto in SUPPORTED_PROTOCOLS else PREFERRED_PROTOCOL
            return _resp(mid, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "chronicles = 「少量金标准页 → 可机械检验的判据 → 同类全样本」"
                    "的统一命令面。每个工具返回里**必定**带 exit 与 exit_meaning："
                    f"{self.exit_legend}。"
                    f"先 triage 再花钱；面外（{self.EXIT_OUT_OF_SCOPE}）不要硬跑。"
                    f"（clientInfo={want.get('name')}）"
                ),
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return _resp(mid, {})
        if method == "tools/list":
            return _resp(mid, {"tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t["inputSchema"]} for t in self.tools]})
        if method == "tools/call":
            name = params.get("name")
            t = self.by_name.get(name)
            if t is None:
                return _err(mid, -32602, f"未知工具：{name!r}",
                            {"available": sorted(self.by_name)})
            try:
                result = self.runner.call(t, params.get("arguments") or {})
            except ValueError as e:
                return _err(mid, -32602, str(e))
            return _resp(mid, result)
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            return _resp(mid, {key: []})
        if is_notification:
            return None
        return _err(mid, -32601, f"不支持的方法：{method!r}")

    def serve(self, inp: Optional[io.TextIOBase] = None) -> int:
        stream = inp if inp is not None else sys.stdin
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as e:                   # noqa: BLE001
                self._write(_err(None, -32700, f"JSON 解析失败：{e}"))
                continue
            try:
                reply = self.handle(msg)
            except Exception as e:                   # noqa: BLE001
                reply = _err(msg.get("id"), -32603, f"内部异常：{e}",
                             traceback.format_exc())
            if reply is not None:
                self._write(reply)
        return 0

    def _write(self, obj: Dict[str, Any]) -> None:
        self.out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.out.flush()


# ============================================================
# 入口
# ============================================================
def _default_root() -> Path:
    """默认项目根：环境变量 → 本文件的上两级（skill/chronicles-mcp/ → 项目根）。"""
    env = os.environ.get("CHRONICLES_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="chronicles-mcp",
        description="chronicles 统一命令面的 MCP server（stdio，零依赖）")
    ap.add_argument("--root", default=None,
                    help="项目根（含 chronicles.py）；默认取 CHRONICLES_ROOT 或本文件上两级")
    ap.add_argument("--list-tools", action="store_true",
                    help="不进入 MCP 循环，只打印工具清单（人读 / 测试用）")
    ap.add_argument("--max-output-chars", type=int, default=20000,
                    help="单条工具结果里内联 JSON 的上限（超出截断，完整值在 structuredContent）")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_argparser().parse_args(list(argv) if argv is not None else None)
    root = Path(a.root).expanduser().resolve() if a.root else _default_root()
    if not (root / "chronicles.py").exists():
        print(f"[chronicles-mcp] 找不到 chronicles.py：{root}", file=sys.stderr)
        return 3
    runner = Runner(root=root, max_output_chars=a.max_output_chars)

    if a.list_tools:
        tools = walk_tools(runner.chronicles().build_parser())
        print(json.dumps({
            "schema_version": SCHEMA_VERSION, "root": str(root),
            "n_tools": len(tools),
            "tools": [{"name": t["name"], "command": t["command"],
                       "description": t["description"],
                       "required": t["inputSchema"].get("required", []),
                       "n_params": len(t["inputSchema"]["properties"])}
                      for t in tools],
        }, ensure_ascii=False, indent=2))
        return 0

    return Server(runner).serve()


if __name__ == "__main__":
    sys.exit(main())
