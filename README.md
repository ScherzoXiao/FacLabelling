# FacLabelling — 古籍文献半自动标注技能包

> 命令行工具名沿用 `chronicles`（单一入口 `chronicles.py`，39 个 MCP 工具前缀
> `chronicles_*`）——工具名是 agent 消费口径，保持稳定；项目展示名 FacLabelling
> 用于仓库与文档。

把「少量金标准页 → 可机械检验的识别规则 → 同批全样本自动标注」跑成一条命令链。
面向个人研究者的复杂版面（竖排古籍名录类）史料整理，单格式前提下可审计、可回滚。

核心流程（十步）：

```
告知待处理文献地址 + PaddleOCR-VL token
  → 批量 OCR（并发预取，服务端排队）
  → 材料画像 scan（全语料零标注统计）
  → 人工标注少量金标准页（技能包自带标注页）
  → learn：从例题学规则 → rules 说明书汇报给人 → 人确认（profile confirm）
  → 分诊 triage → 记录抽取 facts → 方案 plan → 执行 exec → 校验 verify
  → 草稿桥 draft → 界面人裁（annotate 页）
  → 硬指标 eval-iou（留页验证）→ 抽样 sample → 循环至全量验收
```

## 目录结构

```
chronicles.py                  # 单一命令入口（agent 命令面，39 个 MCP 工具）
skill/
  chronicles-pipeline/SKILL.md # agent 使用手册（流程纪律与命令序列）
  chronicles-app/              # 技能包自有交互面（标注/裁决页，独立于任何桌面应用）
  chronicles-mcp/server.py     # MCP 服务器（零依赖，stdio JSON-RPC）
templates/ static/             # 标注页模板与样式（包豪斯：1px 描边、无圆角）
rules_data/ data/              # 运行时目录：规则与档案由使用者自建（本仓库不附带任何数据）
scripts/make_mcp_config.py     # 生成本机 MCP 客户端配置
```

> **本仓库只含代码与文档，不含任何语料、档案、规则或标注数据。**
> 首次使用请先建栏目与档案（见下），再用自己的材料跑流程。

## 安装

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt # Linux/macOS: venv/bin/pip
```

Python ≥ 3.11。

## 配置 OCR 凭证

批量 OCR 走 PaddleOCR-VL（星河社区）。在**项目根**新建 `ocr_backend_config.json`：

```json
{
  "backend": "paddleocr_vl",
  "paddleocr_vl_token": "<你的 token>",
  "paddleocr_vl_job_url": "<星河社区 job url>"
}
```

此文件已被 `.gitignore` 排除，**不要提交**。token 也可用环境变量
`PADDLEOCR_VL_TOKEN` / `PADDLEOCR_VL_JOB_URL` 代替。

## 三分钟跑通

```bash
# 1. 看有哪些命令（agent 消费同一入口）
python chronicles.py --help

# 2. 建栏目（归属范围）与档案（属性表）—— 用你自己的材料名
python chronicles.py project new --name "<栏目名>" --json
python chronicles.py profile new --name "<档案名>" --json

# 3. 批量 OCR：图片放 inbox/ 后
python chronicles.py ocr --images inbox/

# 4. 材料画像 → 学规则 → 说明书   （<pid> = 第 2 步返回的档案 id）
python chronicles.py profile scan  --profile <pid>
python chronicles.py profile learn --profile <pid>
python chronicles.py profile rules --profile <pid>

# 5. 人审说明书后确认（绑 learned sha256；learn 重跑即失效需重审）
python chronicles.py profile confirm --profile <pid> --note "<你的结论>"

# 6. 起标注/裁决页（技能包自有交互面，端口 5001）
python chronicles.py ui --serve
# 打开 http://127.0.0.1:5001/annotate/<页名>.png
```

档案（属性表）就是「这类文献有哪些字段」的定义；`rules_data/` 与 `data/collection_profiles/`
会在你跑流程时自动生成。想直接从表头批量建属性集，用标注页的「新建档案 → 从模板表头导入」。

## 让 agent 用（MCP）

```bash
python scripts/make_mcp_config.py --write
```

把生成的 `mcp.generated.json` 内容合入你的 MCP 客户端配置即可——agent 将获得
39 个 `chronicles_*` 工具。agent 使用手册见 `skill/chronicles-pipeline/SKILL.md`。

## 纪律（重要）

- **本仓库不含任何研究数据**：语料、档案（`data/collection_profiles/`）、规则与画像
  （`rules_data/`）、金标准（`manual_annotations/`）与一切运行时产物均在 `.gitignore` 内，
  由使用者自建、自管。
- `profile confirm` 是**登记口不是机器闸**——「人确认前不得跑批」靠 agent 纪律执行。
- 默认所有破坏性动作（覆盖、删除）都会先归档；`--overwrite` 是唯一的覆盖通道。
