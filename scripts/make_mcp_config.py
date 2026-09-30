# -*- coding: utf-8 -*-
"""生成本机的 MCP 客户端配置（mcp.json 片段）。

解决「示例配置里两处路径要手改」的问题：本脚本按**当前机器**的实际
Python 解释器与技能包位置，直接产出可粘贴/可直接合并的配置。

用法（在本仓库任意位置运行均可）：
    python scripts/make_mcp_config.py             # 打印配置片段
    python scripts/make_mcp_config.py --write     # 写到 ./mcp.generated.json

产物字段说明：
- command / 第一个 arg：运行 chronicles-mcp/server.py 的 Python 解释器
- --root：你的**项目数据根**（inbox/、data/、rules_data/ 所在目录；
  默认 = 本仓库目录。若你把数据放在别处，传 --root <那个目录>）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "skill" / "chronicles-mcp" / "server.py"


def build_config(root: Path) -> dict:
    return {
        "mcpServers": {
            "faclabelling": {
                "command": sys.executable,
                "args": [str(SERVER), "--root", str(root)],
            }
        }
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None,
                    help="项目数据根（默认 = 本仓库目录）")
    ap.add_argument("--write", action="store_true",
                    help="把配置写到 ./mcp.generated.json（而不是只打印）")
    a = ap.parse_args()

    root = Path(a.root).resolve() if a.root else REPO_ROOT
    if not SERVER.exists():
        print(f"[错误] 找不到 {SERVER} —— 请确认在仓库目录内运行", file=sys.stderr)
        return 1
    if not (root / "chronicles.py").exists():
        print(f"[错误] --root {root} 下没有 chronicles.py —— root 应指向仓库根/"
              f"含数据的项目根", file=sys.stderr)
        return 2

    cfg = build_config(root)
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    if a.write:
        out = Path.cwd() / "mcp.generated.json"
        out.write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
