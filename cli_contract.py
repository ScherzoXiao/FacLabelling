from __future__ import annotations

import argparse
import sys
from contextlib import redirect_stdout
from io import StringIO
from typing import Any, Callable, Dict

# ============================================================
# 命令面退出码契约 —— 唯一正本（2026-09-24 收敛）
# ============================================================
# 装什么 · 何时读：
# - 退出码常量 0/1/2/3/4/64 与 EXIT_MEANING 正本：任何模块需要退出码都从这里
#   import，不再各自抄一份。此前这套契约以抄写方式存在 8~10 份，文案改一处
#   其余不跟 —— 与 MCP server 当年手抄工具表是同一种病（MCP 侧已改现推，
#   生产端本次补上）。
# - Parser：argparse 默认用法错误码是 2，与「面外拒答 2」撞车 —— 两者的下一步
#   动作完全相反（改参数 vs 换材料）⇒ 统一改投 64。此前 5+ 个模块各抄一份
#   逐字相同的 error() override，是最容易被漏抄的一份。
# - capture_stray_stdout：json 模式下把底层实现误打进 stdout 的杂音改道 stderr，
#   保住 stdout 纯净（ocr_cli / ui_cli 同一手法）。chronicles._run_entry 是
#   argv-entry 变体（转调整个子命令），语义不同、不强并。
#
# 不改什么：
# - 码值与档位语义一字不变（0 成功 · 1 内部错误 · 2 面外拒答 · 3 缺料 ·
#   4 校验不过 · 64 用法错误）—— 本模块是收敛，不是重定义。
# - 各数据层仍保有各自的 *_exit_code 判定点（triage/ocr/import/draft/ui/adjudicate…），
#   本模块只提供它们共用的词表与基础设施，不替任何层做判定。

# ---- 退出码契约（正本）----
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_OUT_OF_SCOPE = 2
EXIT_MISSING = 3
EXIT_INVALID = 4
EXIT_USAGE = 64

EXIT_MEANING: Dict[int, str] = {
    EXIT_OK: "成功",
    EXIT_INTERNAL: "内部错误",
    EXIT_OUT_OF_SCOPE: "面外拒答（正确行为，非错误）",
    EXIT_MISSING: "缺料（待补输入）",
    EXIT_INVALID: "校验不过",
    EXIT_USAGE: "用法错误",
}


class Parser(argparse.ArgumentParser):
    """用法错误 → 64（改参数），不占 2（换材料）。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def capture_stray_stdout(impl: Callable[..., Dict[str, Any]], *args: Any,
                         **kwargs: Any) -> Dict[str, Any]:
    """跑 impl(*args, **kwargs)，把它误打进 stdout 的杂音转投 stderr。

    返回 impl 的结果 dict —— exit_code 由调用方写（判定点仍在各自数据层，
    本函数不做任何判定）。
    """
    buf = StringIO()
    with redirect_stdout(buf):
        res = impl(*args, **kwargs)
    stray = buf.getvalue()
    if stray.strip():
        sys.stderr.write(stray)
    return res
