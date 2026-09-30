---
name: chronicles-pipeline
description: '把一批同格式扫描古籍页（当前唯一服务对象 = 《商务官报》）做成结构化名录：导入 → 自动归属 → 后台 OCR → 人标 2–3 张金标准页 → 学规则（★停点：人确认）→ 分诊 → 编译方案 → 执行 → 汇总量化指标 → 人裁全量通过；未过则 sample 随机抽样复核，循环至通过。全程走 chronicles 命令面（退出码 0/2/3/4/1/64；面外拒答 2 与缺料 3 都是正常结果，不是失败）。触发词：古籍批量结构化、名录整理、半自动标注、金标准页、批量标注、分诊 triage、跑批 exec、profile learn、profile scan、材料画像、规则变更 diff、profile override、随机抽样 sample、全量通过、验收自动标注、draft 桥、verify 校验、adjudicate、逐条裁决、采纳 驳回 撤销。'
title: "单一格式材料的半自动标注主流程：导入 → 人标金标准 → 学规则（人确认）→ 跑批 → 汇总指标 → 全量通过判定"
summary: "把「少量金标准页 → 可机械检验的判据 → 同批全样本」跑成一条命令链。核心纪律五条：①**单格式唯一口径**——栏目与档案由一次性初始化固定（见 §1.1 实例表），任何会话不得再建线（建线只在新环境附录里，一次性）；②面外必须拒答（rc 2 是正确行为）；③「0 页」判缺料（3），绝不判成功——把「一页没读到」当「检查通过」是最危险的假成功形态；④**agent 画不出框**——金标准页只能「系统提框 + 人定属性 + 机器落盘」，人干活的地方是本地界面（`ui --serve`）；⑤**learn 之后是硬停点**：规则 diff 必须汇报给人确认，确认前不得跑批。验收以量化读数交人裁决（eval-iou 三口径 + verify + 置信分层），未过则 `sample` 随机抽 2–3 页复核，循环直至人判全量通过。含退出码六档表（用法错误 64 必须与拒答 2 分开）、三层责任划分（提议=agent / 裁决=内核 / 定案=人）、人面闭环（draft → 界面裁决 → 下一轮 learn）。"
agent_created: true
read_when:
  - 有一批同格式扫描页（图像 + 已 OCR 的文本），要批量产出结构化名录 / 条目
  - 用户给了一批新材料的地址，要走「导入 → 标金标准 → 学规则 → 跑批 → 验收」全流程
  - 跑批结束，需要汇总量化指标、判断这批自动标注能否全量通过
  - 需要从跑批产物里随机抽样几页交人复核（`sample`）
  - 要判断「这批材料属不属于当前档案」——拿不准该不该继续花钱跑 OCR / 跑批
  - 命令返回了非零退出码，需要判断「该改参数 / 该补输入 / 该换材料 / 该报障」
  - 用户说「古籍批量结构化」「名录整理」「半自动标注」「金标准页」「分诊」「跑批」「随机抽样」「全量通过」
---

# 单一格式材料的半自动标注主流程

## 0. 一句话

> 把「从**少量金标准页**推导出**可机械检验的判据**，并对**同批全样本**执行」这件事，
> 跑成一条 agent 可直接调用的命令链。

**卖的是判据，不是程序。** 所以本流程的重点不是"跑得快"，而是"**能机械判错、面外会拒答**"。

**七步主流程**（子命令全部在命令面上；栏目与档案初始化一次后固定，日常会话不建线）：

```
① 入场      import → project assign（预置栏目，不问名）→ ocr（后台跑，首批就绪即并行）
② 识别人    ui --serve → 人标 2–3 张金标准页
③ 学规则    profile scan（全池页无标注通读 → 材料画像，跑批前一次）→ profile learn（消费画像先验）→ **profile rules 出规则说明书**（含材料画像节）→ 汇报 diff + 说明书 → ★停点：人审规则 / profile override
④ 跑批      triage → facts → plan → exec → draft
⑤ 汇总      eval-iou 三口径 + verify + 置信分层 → 成文汇报
⑥ 裁决      人判「全量通过 / 不通过」；未过 → sample 随机抽 2–3 页 → 人改
⑦ 循环      改过的页 = 新金标准页 → 回到 ③，直至人判全量通过 ★★
```

**命令面之外只剩三件**：①**人**给名字 / 模板文件 / 材料路径 / 属性判定；②**人**在界面上
**验收自动标注**（逐条采纳 / 驳回 —— 这是飞轮的闭合处，见 §2 步骤 A-8）；③剪贴板监听
（操作系统事件驱动的常驻进程，天生不属于"一次调用"）。

⚠⚠ **别再把「没有任何一步必须回到图形界面」当作卖点**（2026-09-16 用户实测反馈）：
跑批确实不必回界面，但**飞轮的闭合必须回界面** —— 自动标注的产物要有人**在图上**看、
**逐条**采纳或驳回，才能成为下一轮的金标准页。`draft`（A-8）就是为此而加：它把层 3 产物
桥成界面认得的草稿形态。缺了它，"agent 自动标注 → 人验收 → 变金标准 → 再 learn"
这一环是**断的**，整个管线退化成一次性流程。

---

## 1. 先把两条边界钉死（否则后面全错）

### 1.1 只服务一类材料（单格式唯一口径，2026-09-26 定）

本 skill 的服务对象是**一套格式相同的材料**：同一批纸、同一套印格、同一套表头
（当前 = 《商务官报》）。**多格式泛用已从设计目标中撤下**（2026-09-24 用户裁定）：
单格式流程尚未跑熟之前，多套契约的维护成本曾把本项目拖入失控，不再重犯。

| 预置实例 | id | 职责 |
|---|---|---|
| 栏目 | `<栏目id>`（显示名由人给；本部署作「商务官报」） | 全部页图的唯一归属 |
| 档案 | `<pid>`（属性表；本部署对应「公司注册」类页面） | 唯一的属性表与规则载体 |

⇒ **任何会话不得再执行 `project new` / `project template` / `profile new`**——
建线命令只存在于文末附录「一次性初始化」（新环境装好后跑一次）。归属判断不需要挑选：
只有一套契约，面外材料靠分诊拒答（rc 2）。

### 1.2 起点是**人给的金标准页**，不是"从零开始"

这是最容易误判的一点。三层责任是分开的，**权限自下而上收**：

| 层 | 谁 | 能做什么 | 不能做什么 |
|---|---|---|---|
| **提议** | agent（你） | 提读数、提框候选、提契约修正建议 | **不得声明契约已生效**、**不得替人画框** |
| **裁决** | 确定性内核 | 几何重建、坐标、列序、算术、契约校验 | 不判"语义对不对" |
| **定案** | **人** | 定属性、确认 / 修订契约 | — |

⇒ **命令面不替人做三件事**：**给定属性语义**（`annotate` 只提框，属性由人定）、
**给定材料**（模板文件 / 材料目录 / 栏目名都是人给的参数）、
**★ 提供画面**（人必须在**图上**看框、定框、改框 —— 这是界面的事，命令面做不到；
2026-09-16 实测反馈：把标注只做成 `add --line N`，等于让人在看不见图的情况下指路）。

### 1.3 输入前提（缺哪样补哪样；★ 标 ⚠ 的那一行**必须靠界面**，命令面只是降级通道）

| 前提 | 存在哪 | 谁产生 | 命令面入口 |
|---|---|---|---|
| **栏目**（归属 + 模板样式） | `data/projects.json` | **初始化一次** | 附录初始化（日常不跑） |
| **档案**（属性表） | `data/collection_profiles/<pid>.json` | **初始化一次** | 附录初始化（日常不跑） |
| 页图像（**含 PDF 渲染**） | `inbox/` · `outbox/` | 用户给目录或 PDF | ✅ `import`（PDF 逐页渲染，见 A-3） |
| 页 → 栏目归属 | `data/project_assignments.json` | 人给范围 | ✅ `project assign`（**缺它 `triage --project` 取空**） |
| 页文本 | `data/structured/<stem>.json` | OCR 通道 | ✅ `ocr` |
| **OCR 凭证**（PaddleOCR-VL token） | 环境变量 `PADDLEOCR_VL_TOKEN`（优先）或项目根 `ocr_backend_config.json` 的 `paddleocr_vl_token` 字段 | **用户/环境**（★ skill 包不含凭证，agent 只向用户要，不写死） | 无命令口——`ocr --dry-run` 不验凭证；真跑 rc 1 报「引擎构造失败」= 凭证缺/错 |
| **金标准页标注** | `manual_annotations/<stem>.png.jsonl` | **人在界面上画框 + 定属性** | ⚠ `annotate`（**只是降级通道**；正解是界面，见 A-6 末段） |
| **可裁决的自动标注草稿** | `data/preannotations/<stem>.ai.jsonl` | `exec` 跑出产物 → **`draft` 桥成草稿** | ✅ `draft` ★★ **飞轮闭合处**（见 A-8） |
| **裁决结论**（采纳/驳回/撤销） | 状态 `data/adjudication/status.json`；**采纳的行落 `manual_annotations/`** | **人** —— 界面点，或经命令面**指定序号** | ✅ `adjudicate` ★★ 飞轮第四段（见 A-9） |
| **锚词 / 分诊判据** | `rules_data/learned_<pid>.json` | **由金标准页学出来** | ✅ `profile learn` ★★（每轮报**变更 diff**，见 A-7） |
| **材料画像（全语料通读）** | `rules_data/corpus_<pid>.json` | `data/structured/` 池页（**无标注**，确定性统计零语义） | ✅ `profile scan`（`learn` 消费：df≥0.9 常量并入、0.4–0.9 仅报告；**diagnostics.top20 无论过线与否都落盘**——「最高才 0.2」这类事实必须可见） |
| **人工覆盖层**（pin 属性序 / 强制必填） | `rules_data/overrides_<pid>.json` | **人**（声明，**不是**统计出来的） | ✅ `profile override`（`learn` 只**读**它，见 A-7 末） |
| 契约 | `data/layout_contracts/` | 由金标准页编译 | ✅ 包含在 `facts` / `plan` 里 |

★★ **`profile learn` 这一行是整条链里最容易被漏掉的**：它不产出"看起来有用"的产物，
但**所有分诊判据都从它来**。漏了它，`triage` 会给你一整批 `unmeasurable`（rc 3）而你不知道为什么。
判据链条（实证）：

```
manual_annotations/*.jsonl
  → rule_learn.learn_and_save            → rules_data/learned_<pid>.json
  → rule_split.build_spec(learned, profile)  → spec.anchors
  → page_triage.count_anchors            → 分诊判据
```

★ **档案里没有 `anchors` 字段** —— 锚词是**学出来的**，不是档案里写着的。
所以：**冷启动的第一批金标准页只能由人直接指定**（那时还没有规则，分诊挑不了）；
`triage` 是**有规则之后**才用来筛后续大批量的。

★ 若档案不存在，`triage` 会判**缺料（退出码 3）**——这不是 bug，是在告诉你"该建档案了"。

---

## 2. 主流程

全部命令来自**同一个入口** `chronicles.py`。加 `--json` 时 stdout 是**纯 JSON**（可直接 parse），
人读信息走 stderr。

### 步骤 A（材料入场：从地址到"可标注 / 可跑批"；栏目与档案初始化一次后固定，只管材料本身）

```bash
# A-0. 系统自检（每轮会话开头跑一次；只读、零成本）
python chronicles.py gold --json
#    确认：唯一档案在位、金标准页数、栏目 × 图数正常。
#    栏目与档案：见 §1.1 实例表（本部署初始化时填入）。
#    两者查无 ⇒ 本环境没初始化过 → 走文末附录「一次性初始化」（只跑一次），
#    **日常会话禁建线**（§1.1）。

# A-3. 导入材料（外部**图像或 PDF** → inbox/；保留原名，像素级去重，冲突自动 _2 避让）
python chronicles.py import --images "<材料目录>" --json
#    ★ PDF 会被**逐页渲染**成 `原文件名_NNNN.png`（页码 4 位）——与界面批量导入**同一实现**，
#      所以命令面导入的 PDF 页图，与界面导入的在系统里长得一模一样。
python chronicles.py import --images "<材料目录>" --pdf-dpi 300 --json
#    ★★ 渲染 dpi = **OCR 质量的第一决定因素**，且**必须显式选**（默认 200，钳制 72..400）。
#       实测同一页：200 dpi → 953×1787；320 dpi → 1525×2858（像素量差 2.5 倍）。
#       官报这类**竖排小字**材料建议 **300+**；普通印刷体 200 够用。
#       返回 JSON 的 `pdf_dpi` 会回报本次实际用的值 —— **别让它成为一张暗牌**。
#    rc 0 → 成功（★「全是重复、一张都没复制」也是 0 —— 去重生效不是错误）
#    rc 3 → 没读到图像也没读到 PDF（路径不对 / 目录空了）
#    rc 1 → PDF 渲染失败，或**本机缺 PyMuPDF（fitz）**——见 errors；装依赖后重跑即可
#           （已导入的页按像素 hash 幂等跳过，不会重复）
python chronicles.py import --images "<材料目录>" --dry-run --json   # 先干跑看一眼
#    ★ 图会**留在 inbox/**，命令面**不会**把它搬去 outbox/ —— 这是正常的（执行器两处都查）。
#      ⚠ 别用整个大目录递归导入：目录里若混着已有的页图子目录（如 `_ocr/**`），
#        它们会被一并当成新素材、改名后抹掉期号/页序信息。**精确给文件或先干跑核对**。

# A-4. 归栏目（栏目**已初始化**，不问人；★ 缺这一步 `triage --project` 一页都取不到）
python chronicles.py project assign --project <栏目id> --images inbox --json
#    rc 3 → 一张都找不到（路径写错 / 还没 import）★ 不校验就写归属会造出**悬空 assignment**
python chronicles.py project images --project <栏目id> --json       # 核对归属

# A-5. OCR —— **后台起跑，首批就绪即并行**（★ 七步流程第 ① 步的时序编排，2026-09-26 定）
#    `ocr` 本身同步、幂等：增量落盘、重跑只补缺页 ⇒ **放到后台执行是安全的**。
#    agent 的正确姿势：把下面这条命令放后台跑，**不等它结束**；
#    用 `next` / `gold --json` 轮询已就绪页数，首批页（≥2–3 张）一就绪
#    就进入步骤 A-6（起服务标金标准页）—— OCR 与标注并行，人不用等全批。
python chronicles.py ocr --images inbox --json
#    ★ 页实际在哪个目录就给哪个：示例用 inbox/；若页图在 outbox/（界面导入 / 已跑过的库常见），
#      用 --images outbox。目录里没页 → rc 3（缺料，不是失败），用 `next --json` 看页都在哪。
#    ★ 第一次真跑前确认凭证就位（§1.3「OCR 凭证」行）：环境变量 PADDLEOCR_VL_TOKEN
#      或根目录 ocr_backend_config.json；凭证缺/错 → rc 1「OCR 引擎构造失败」。
#    默认走批量档（批量提交张数 = config.OCR_BATCH_PREFETCH，默认 100），无需手动调；
#    JSON 里的 engine / batch 字段就是**本次实际用的粒度**，想确认就去看它。
#    rc 0 → 成功（含部分页失败也会在 JSON 的 items 里逐页标出）

# A-6. 给一页做金标准（人的工作，但框由系统提）
python chronicles.py annotate lines --image <页名>.png --json
#    返回该页 OCR 行：编号 + 文本 + 框。把它给人看，问"第 N 行是什么属性"。
python chronicles.py annotate add --image <页名>.png --line <N> --attr "<属性名>" --json
#    人定了属性 → 机器落盘（source: manual）→ manual_annotations/<页名>.png.jsonl
python chronicles.py annotate list   --image <页名>.png --json    # 看看已标了什么
python chronicles.py annotate remove --image <页名>.png --index 2 --json

# A-6b. 材料画像（跑批前一次；`learn` 消费它，跳过也能跑但画像先验缺席）
python chronicles.py profile scan --profile <档案id> --rules-dir <目录，默认 rules_data/> --json
#    全语料页**无标注通读**（确定性统计，零 LLM，不依赖金标准）
#    rc 0 → 落盘 rules_data/corpus_<档案id>.json（报 页数 / 常量候选数 / 异常页数）
#    ★ 产物是「**画像先验**」不是规则：df≥0.9 的常量候选 learn 时自动并入页常量、
#      0.4–0.9 仅列出待人审、diagnostics.top20 无论过线与否都落盘（事实必须可见）。
#    ★ 若需求方把「OCR 后的自训练」说成「得到规则」——改口径：无标注阶段只能得到**画像**；
#      **规则只能由金标准页 learn 出来**（下一步 A-7）。画像 → learn 消费画像，这才是实际链条。

# A-7. ★★ 学规则（**最容易被漏的一步**：不跑它，anchors 为空，triage 一律 rc 3）
python chronicles.py profile learn --profile <档案id> --json
#    rc 0 → 落盘 rules_data/learned_<档案id>.json（返回里报了骨架与锚属性）
#    rc 3 → 本档案名下一页金标准都没有。**两种成因要分清**（隔离后它们是两回事）：
#        · 行标在**别的档案**名下 → 改归属（`chronicles gold` 看分布）
#        · 行**没有档案标签**（未归档）→ 多半是标注时没绑定档案
python chronicles.py profile show  --profile <档案id> --with-gold --json   # 核对"已学规则"现状
python chronicles.py profile learn --profile <档案id> --dry-run --json     # 只算不落

# A-7b. 金标准普查：我有哪些档案/项目、金标准做到哪一步（只读、零 token）
python chronicles.py gold --json
#    给：档案 × 页/行 + 未归档 + 同页多档案 + 项目 × 图数/已OCR/待标注
#    ★ 与 `annotate` 的分工：那边是**给人标注用的入口**，这边是**给 agent 看的读数**
#    ★ 「未归档」= 行上没有 `profile` 标签 —— 它在**任何**档案下都会被排除（见 §5.4）
```

★★ **`annotate` 的边界（别越界）**：金标准页的一行是 `{box, text, attr, …}`，其中
`box` 是**原图像素坐标**。agent 读得懂文本、判得了属性，但**画不出框** ⇒
agent 的正确动作是**列出候选让人挑**，不是自己定框。
框的来源只有两处，**不许第三条**：`--line N`（取 OCR 行几何）或 `--box x1,y1,x2,y2`
（= 界面里画框的等价物）。两处都给 → 报错；都不给 → 报错（**没有"猜一个框"这条路**）。

★★ **但人真正干活的地方是界面，不是这里**（2026-09-16 实测反馈，用户原话：「技能包版本并没有
为用户提供标注工具…用户没有办法直接通过当前的技能包管线创造并提供金标准页」）：

`annotate add --line N` 要求人说出行号，而**人看不到图、也看不到框** —— 竖排古籍里"一行"常是
整列的一段，靠文本猜行号**极易错位**。⇒ 本组命令的正确定位是「**agent 可调的最小原子动作**」
（界面不可用时的降级通道），**不是**给人用的标注工具。**别把它当作"标注这一环已经齐了"的证据。**

人标注的正确入口是**本地界面的标注页**（`/annotate/<页名>`）：**拖拽即画框**、单击框选中编辑、
属性下拉 + **Alt+数字快选**；页面已有"预标注草稿"区时，还能**逐条采纳/改写 text,box,attr**，
采纳即落 `manual_annotations/`（`source=ai_verified`）。
⇒ **把图给人看 + 让人在图上定框/定属性**，是标注这一环不可省略的一步；命令面自己做不到这件事。

★★ **（2026-09-16 已补）** 这两者原本**不通**：`exec` 的产物落在 `data/results/<stem>.result.json`，
而界面草稿裁决区读的是 `data/preannotations/<stem>.ai.jsonl` ⇒ "agent 自动标注 → 人验收改动 →
变金标准页 → 下一轮 learn"这一环是断的。**现已由 `draft` 打通**，完整用法见下方 **A-8**。

★ **`--profile` 可以给名字**（不必先 `list` 拿 id）：先按 id 查、查不到再按名称查。

★ **每轮 `learn` 都会报「规则变更 diff」**（2026-09-16 新增）：改了什么一目了然 ——
属性序（增 / 删 / **换位**）、必填集合、页常量、值示例涉及哪些属性、取值枚举、标题候选、
跨列续接，外加**输入规模**（多少页 / 多少行）。★ **先看输入规模那一行**：规则报"没变"
有可能是"根本没读到料"，两者在旧版本里现象一样。
空 diff 也会明确打一行「规则**没有变化**」，**不会**静默什么都不输出；`--dry-run` 同样给 diff。

★★ **learn 之后是流程的硬停点（七步流程第 ③ 步末，2026-09-26 定；09-27 升级为「审规则书」）**：
learn 后先跑 **`chronicles profile rules --profile <档案>`** 生成**识别规则说明书**
（结构规则 / 页面规则 / 提取判据 / 几何规则 / 人工层现状五节，每条带来源与改法），
连同规则变更 diff 一起**汇报给人**。人审的是**规则本身**（"这就是系统读每页的方式"），
不是指标：等人**确认，或用 `profile override` 修改后再确认**（页常量增删：
`override --page-const-add/--page-const-remove`；属性序/必填：`--attr … --position/--required`）。
**人确认之前不得进入跑批**（triage / facts / plan / exec）。
**人确认后，agent 把确认登记落盘**（2026-09-30 新增，scene ⑥「用户对规则裁决」的可核验形态）：

```bash
python chronicles.py profile confirm --profile <档案id> --note "<人话结论>" --json
#    rc 0 → 落 rules_data/confirmed_<档案id>.json（绑 learned_<pid>.json 的 sha256）
#    rc 3 → 还没 learn（确认态没有对象可绑）。
#    之后 `profile rules` 头部会显示确认态；learn 重跑整份重写 learned ⇒ hash 必变
#    ⇒ 说明书自动显示「规则已变，需重审」——旧确认不会假装仍然有效。
#    ★ 登记口**不是机器闸**：triage/exec 不读它拦人，拦人的仍是本条纪律。
```

理由：规则是从几页例题统计出来的，人知道自己领域里哪条统计结论不对（A-7c 正是为此而设）；
带着未确认的规则跑全量批，烧掉的是真金白银的 OCR 额度，产出一整批要返工的产物。
汇报形态：**规则说明书全文** + diff 原文 + 一句人话结论（「规则是否可用」的建议），**同意权在人**。

### 步骤 A-7c：规则不够贴时 —— 人工覆盖层 `profile override`（2026-09-16 新增）

`learn` 是**统计**：属性序按 support 排、必填按 `support >= 0.8` 判。例题只有几页时，
统计结论会与人的领域知识冲突（某属性在 2 页里恰好缺一次 ⇒ support=0.67 ⇒ 判成 optional，
而人知道它必有）。这时**不要**去改金标准页"凑"统计（那是拿数据迁就方法），
用覆盖层把人的判断**另存一处**：

```bash
python chronicles.py profile override --profile <pid> --show --json        # 看当前覆盖层
python chronicles.py profile override --profile <pid> --attr 公司名 --position 1 --json  # pin 属性序
python chronicles.py profile override --profile <pid> --attr 公司名 --required --json    # 强制必填
python chronicles.py profile override --profile <pid> --attr 公司名 --optional --json    # 取消必填
python chronicles.py profile override --profile <pid> --clear --json       # 清空（配 --attr 只清一条）
#    rc 4 → 属性不在学出的属性序里 / position 越界 / --required 与 --optional 同给 / --clear 与设置项同给
#    rc 3 → 这个档案还没学过（先跑 A-7）
```

★★ **为什么必须是两个文件**（`learned_<pid>.json` 与 `overrides_<pid>.json`）：
前者每次 `learn` **整份重写**（机器所有），后者**只有人能改**（人所有）。若把覆盖混进统计产物，
下一轮就会**把人的判断当成统计结论**引用 —— 这正是「AI 的判断不得以库内既有结论的身份被引用」
那条定则要防的。⇒ 叠加后产物里能**分辨**来源：`_meta.overrides_applied` 逐条带
`source:"human"` 与 `from` 原值；被拒绝的条目进 `_meta.overrides_skipped`（**绝不静默丢弃**）。

★ **每条覆盖必须自带 `source: "human"`** —— 缺了就不生效（fail-closed，且是**逐条**判定：
一条坏覆盖不会让整份失效）。这是刻意的：让"机器写的"进不来这个文件。
★ **pin 只做重排，不新增属性**：属性不在学出的序里 ⇒ 当场 rc 4（而不是悄悄插进去）。

### 步骤 A-8（闭环）：把自动标注产物桥成可裁决的草稿 —— 下一轮的起点

**为什么单独列一步**：`exec` 产出的 `data/results/<stem>.result.json` 是**机器格式**，
界面裁决区不认它。人想"看一眼 agent 标得对不对、顺手改两处"，需要的是
`data/preannotations/<stem>.ai.jsonl` 那种**逐条带 OCR 原文与状态**的草稿。
`draft` 就是这两者之间唯一的那道桥。

```bash
# 跑完 exec 之后（页来源参数与 exec 完全一致，别都不给）
python chronicles.py draft --profile <pid> --project <栏目id> --json
python chronicles.py draft --profile <pid> --stem <页名> --json        # 单页
python chronicles.py draft --profile <pid> --pages "<页目录>" --json
#    rc 0 → 产物已桥成草稿；返回里每页给 status，并给出**人该点开的界面地址**
#    rc 3 → 那几页还没有 exec 产物（先跑 exec）
#    rc 4 → **对账不过**：从产物重建出来的条目与产物本身不一致 ⇒ **拒绝落盘**。
#           这是保护，不是故障 —— 说明产物与当前方案/契约已不同源。
python chronicles.py draft --profile <pid> --stem <页名> --dry-run --json   # 先看会写什么
python chronicles.py draft --profile <pid> --stem <页名> --overwrite --json # 确要替换已有草稿
```

**它做了两件事，都不是"重新生成标注"**（§60 单一实现）：

1. **调回同一个 `PA.anchor_page`** —— `exec` 内部用的就是它。喂给它从产物**原样重建**的
   `records`（空记录留空 dict 占位以保 `record_index` 对齐），于是重新拿到完整的
   `row_uid` / `evidence.ocr_text` / `segments`。这三样在产物里被**有损投影**掉了，
   而它们正是裁决界面「**禁止盲签**」（红线二）与裁决状态定位的依据。
2. **逐条对账**：重建结果与产物**逐字段**比对（attr / text / box / confidence / row_uid），
   任一条不符即 **rc 4 拒绝落盘**。⇒ **你看到的草稿一定与你手里的产物同源**。

**默认不覆盖 —— 这是纪律，不是疏漏**：那几页若**已经有草稿**，命令**不动它**，
状态记为 `existing_differs`（**rc 仍是 0，不是失败**）并报出差异。理由："有草稿"意味着
**那页本来就可以裁决**，本命令的职责是"让每一页产物都能被人看到"，不是"非要用我的版本替换你的"。
确要替换时显式加 `--overwrite` —— **被覆盖的原文自动归档**到 `_archive/draft_superseded/`
（每页返回的 `backup` 字段给出归档路径；**归档失败则拒绝覆盖**）。
`n_created` 与 `n_replaced` **分开计数** —— 别把"替换"读成"新写"。

**草稿落到哪**：`data/preannotations/<stem>.ai.jsonl` —— 与界面「点生成预标注」**同一落点、
同一套键**。所以跑完 `draft`，人直接在界面上干活：

```
/annotate/<页名>          ← 人或 agent 都从返回里拿得到这个地址
  人在那里：看框、看 OCR 原文、逐条采纳 / 改写 text,box,attr / 驳回
  采纳即落 manual_annotations/<页名>.png.jsonl（source=ai_verified）
  ⇒ 这就是**下一轮的金标准页**
```

⇒ **飞轮闭合**：`exec` → **`draft`** → 人在界面裁决 → `manual_annotations/` →
`profile learn` → 下一轮。

★ **`draft` 不是"再跑一遍标注"**：它不改任何识别结果，只做「格式转换 + 一致性对账」。
识别结果本身由 `exec` 决定 ⇒ **产物不对，桥不会把它变对**，只会如实让你看到产物。

⚠ **裁决的触发者只能是人**（红线二「确认即固化」，禁止盲签 —— 所以界面必须并排显示
OCR 原文）。agent 能做的是**把草稿备好、把地址给人**；**它不会自行决定哪条该过**。

★ **2026-09-16 增补**：裁决现在**命令面上也能做**了（见步骤 A-9）—— 但边界不变：
`accept/reject/reset` **只执行人指定的序号**，命令面**不猜**该裁哪条；
`batch --confidences` 是"机器的判断"，由**数据层的四道安全门**拦
（2026-09-17 由两道扩为四道，判据同时由"几何来源标签"换成"档案级独立回验读数"）。
⇒ 从"只能由人手点"放宽为"必须由人**指定**"：**决定权始终不在 agent 手里**。
★ 同理：`adjudicate validation` **只登记人给出的读数**，它自己**不做任何度量**
（度量属离线对评工具，零 API）—— 命令面不替人判"够不够准"。

⚠ **一个已知的口径冲突（未统一，先报）**：`draft` 走 `plan_exec.contract_facade(plan)` 的
`char_metrics`，而界面「点生成」走 `layout_contract.build_contract` ⇒ 同一页由两条通道产出的
`confidence` 可能不同（`low` vs `medium/high`）。表现为某页 `existing_differs` 但**差异只在置信度**。
改哪一边都会动到已有产物的读数，属待裁定的口径决定。

### 步骤 A-9：裁决 —— 命令面入口（2026-09-16 新增）

**为什么补这一步**：裁决此前**只有界面**能触发。技能包里 agent 能跑 `draft` 造出草稿，
却没有任何命令面把它裁决掉 ⇒ 飞轮第四段（裁决 → 回流改规则）在命令行上是**断的**。

```bash
# ① 看这页有什么可裁、以及"批量采纳能不能用"
python chronicles.py adjudicate list --stem <页名> --json
#   ↑ 与界面**同一个取数口**（adjudicate.page_state）：草稿 + 裁决状态 +
#     批量准入 + 操作次数估算，一次给全

# ② 逐条裁决（★ 序号由**人**给 —— 命令面不猜该裁哪条）
python chronicles.py adjudicate accept --stem <页名> --idx 3 --json
python chronicles.py adjudicate accept --stem <页名> --idx 3 \
    --text "改后的文本" --box 12,34,220,58 --attr 公司名 --json   # ⇔ 界面的「改后采纳」
python chronicles.py adjudicate reject --stem <页名> --idx 3 --json
python chronicles.py adjudicate reset  --stem <页名> --idx 3 --json   # 撤销采纳（幂等）

# ③ 批量（★ 两种触发方式语义**不同**，别当同义词用）
python chronicles.py adjudicate batch --stem <页名> --indices 1,3,7 --json
#   ↑ **人的判断**：人已逐条点过 ⇒ 免安全门
python chronicles.py adjudicate batch --stem <页名> --confidences high --json
#   ↑ **机器的判断**：按置信层 ⇒ 必须过四道安全门，否则 rc 2 拒绝

# ④ 档案级回验读数（★ 批量采纳的**唯一放行依据**；2026-09-17 新增）
python chronicles.py adjudicate validation --profile-id <prof_…> --json
#   ↑ 只查询：这个档案登记过回验读数没有（没有 ⇒ 批量采纳对它一律停用）
python chronicles.py adjudicate validation --profile-id <prof_…> \
    --verdict pass --metric value_aligned_iou50 --value 0.62 \
    --n-boxes 46 --pages 0001,0002,0003 --json
#   ↑ 登记。★ 粒度是**档案** —— 它不是"给这一页放行"，是解除整个档案的封禁；
#     所以它**不接受 --stem**（给了就会把粒度误解回页级）。
#   ★ `--verdict` 只有 pass / fail 两个值（**拿不准就是 fail**）；只给 --profile-id
#     不带 --verdict = 只查询、不写盘（顺手登记会把"看一眼"变成"改状态"）。

# ⑤ ★ 那个 `--value` 从哪来？（2026-09-21 新增）`eval-iou` 是它的**唯一产出者**
python chronicles.py eval-iou --profile-id <prof_…> --json
#   ↑ **留一交叉验证**（零 API）：每折留出一页学契约、再在该页上测
#     ⇒ 测试页**未参与该折学习** ⇒ 独立性**不需要新标注页**就能满足（红线一自动成立）
#   rc 3 = 缺料（**不凑数出个假读数**）：无契约 / 无 records / 无金标准 / 入选源页 < 3
#   ★ 读三个数、别只看一个 —— 它们把"读数低"拆成两个**互斥**的失败源：
#       值匹配 `value_match_rate`   语义对齐（值读出没有 / 属性名对不对）
#       纯几何 `geometry_iou50`     值已配上后，框对上没有
#       端到端 `value_aligned_iou50`  **登记就用这个**（分母 = 全部有效金标准框）
#   ⚠ 分母一律取**金标准侧** ⇒ 预测没产出的框算不中（fail-closed）。
#     旧口径（分母在预测侧、虚高）实测同页 0.750 vs 0.227 —— **不得用于准入**。
#   ★ `--oracle` = 用金标准值当 records，得**上界对照**（隔离 VLM 值质量）。
#     它用来判断**主瓶颈在哪**，**不是读数**，别拿去登记。
python chronicles.py eval-iou --profile-id <prof_…> --stem <页名> --json
#   ↑ 只看一页（诊断；准入读数必须是**档案级** = 上面那条）
```

**退出码**（与全局同表）：

| rc | 含义 |
|---|---|
| 0 | 成功 |
| **2** | **面外拒答 = `batch` 安全门拦下**（版式漂移 / 无几何层 / 无原图 / 档案未回验或未达标）—— **这是正确行为，别去"修"**；`batch_block_reason` 已经给的是人话 |
| 3 | 缺料：这一页**没有草稿** ⇒ 先跑 `draft`（步骤 A-8）；或该**档案没有回验记录**（`adjudicate validation` 查询） |
| 4 | 校验不过：`--idx` 越界 / `--box` 格式错 / 文本为空 / `--verdict` 不在 pass\|fail |
| 1 | 内部错误：登记读数时归档旧记录或写盘失败（**不是**你参数写错了 —— 去查目录权限） |
| 64 | 用法错误（如 `--indices` 与 `--confidences` 同给，或 `--value` 不是数字） |

★★ **安全门不许绕**（红线二落在**数据层**，不在界面层）：按置信层批量采纳要过**四道**检查，
**全部 fail-closed**：① 版式漂移挂起 ② 无几何层（`L1_blocks` 缺失）③ 无原图
（人点进去没图可核）④ **该档案有独立回验读数且 `pass`**。

★★ **判据已换（2026-09-17，用户裁）：不再是「几何来源 = `pipeline`」**。
实测它只是"几何可信"的**必要条件**：同为 `pipeline`，值对齐命中率 **0.800**
（`manual_9a7e82dd`）vs **0.118**（官报 0001），差 6.8 倍 ⇒ "放行即可信"这一半从未成立。
现在 `geometry_source` **只作参考信息**（JSON 里照给，人读视图标了"**非判据**"）。
⇒ **「没测过」不等于「偏离小」**：没有回验记录就一律不放行，且必须把"为什么"与
"怎么解锁"一并给出。准入粒度是**档案**（不是页、不是期次 —— 期次是行政划分）。
命令面**没有** `--allow-untrusted` 之类后门（有护栏用例钉着）。
★ **登记一个 `pass` 之后要记得它随时可以改判 `fail`** —— 那不是一次性开关。

★ `reset` 的特别之处：它按 `ai_ref` 精确清残留，**不需要草稿还在** ⇒ 草稿已删也能撤销。

### 七步主流程（跑批段）

```bash
# ── 0. 前置：确认档案、金标准页、页都在（下面每条命令内部会自检）
#    档案：data/collection_profiles/<pid>.json      ← 步骤 A-2
#    金标准页：manual_annotations/<stem>.png.jsonl  ← 步骤 A-6
#    锚词：rules_data/learned_<pid>.json           ← 步骤 A-7 ★ 漏了这个后面全白跑
#    页文本：data/structured/<stem>.json            ← 步骤 A-5（没 OCR 过的页是 unmeasurable）

# ── 1. 分诊：这批页像不像这个档案？ ★ 先分诊，再花钱
python chronicles.py triage --profile <pid> --inbox <页目录> --json
#    或按栏目取页（★ 需要先 A-4 归栏目）：
python chronicles.py triage --profile <pid> --project <project_id> --json
#      rc 0 → 面内，继续第 2 步
#      rc 2 → 面外，停（见 §3：换材料 / 换档案 / 显式放行）
#      rc 3 → 缺料，按 JSON 里的 next 字段补输入

# ── 2. 层 1：金标准 → 观测（只记录观测，不推断规则）
python chronicles.py facts --profile <pid> --json
#    产物 data/plan/facts_<pid>.json   ⚠ 目录是单数 plan

# ── 3. 层 2：观测 → 识别与标注方案（核心，"提契约"在这一步）
python chronicles.py plan --profile <pid> --json
#    产物 data/plans/plan_<pid>.{json,md}   ⚠ 目录是复数 plans

# ── 4. 层 3：方案 → 产物   ★★ 跑"这批新材料"的正确姿势是 --project / --pages
python chronicles.py exec --profile <pid> --project <栏目id> --json
#    按**栏目**取页跑 —— 与第 1 步 `triage --project` **同源**。这才是"跑这批新页"。
python chronicles.py exec --profile <pid> --pages "<页目录>" --json
#    或直接点名：--pages 可给**目录**（跑目录里全部页）或若干页名
python chronicles.py exec --profile <pid> --stem <stem> --json        # 单页
#
# ⚠⚠ **三者都不给** → 跑的是 `plan.source.pages` = **方案编译时的那几页金标准页**，
#     **不是你的新页**。而它 rc 0、有产物、校验也通过 —— 极易被误读成"跑完了"。
#     （旧版本文档把它写成"跑全页"，实测跑出来的是 3 个月前的 4 页金标准页。）
#     ⇒ 产物里的 `page_source` 字段与人读输出**都会显式声明跑的是哪一批**：
#       `explicit` / `project:<id>` / `plan.source.pages`。**读它，别猜。**
#
# 产物 data/results/<stem>.result.json
#   ⚠ `--out` 是**目录**（不是文件名）—— 传文件名会静默建出一个同名目录。
#   返回 JSON 逐页交代：`n_pages` / `n_written` / `n_skipped`；
#   没有产物的页会在 `skipped[]` 里给出 `reason`（最常见 `no_structured` = 该页还没 OCR）。

# ── 4.5 ★★ 桥成人可裁决的草稿（飞轮闭合处；不做这步，跑批成果就停在机器格式里）
python chronicles.py draft --profile <pid> --project <栏目id> --json
#    页来源参数与 exec **完全一致**（--project / --pages / --stem 三选一，别都不给）
#    返回里每页给 status 与**人该点开的界面地址**；默认**不覆盖**已有草稿（见 A-8）
python chronicles.py draft --profile <pid> --project <栏目id> --overwrite --json   # 确要替换时
#    rc 4 = 重建与产物对不上 ⇒ **拒绝落盘**（保护：产物与当前方案已不同源）

# ── 5. 自校验（**这一步必须做**：产出合不合格由它判）
python chronicles.py verify --result data/results/<stem>.result.json \
                            --plan data/plans/plan_<pid>.json --json

# ── 6.（可选，给人看）方案结构图
python chronicles.py map --profile <pid> --svg <输出路径.svg>
```

**关于第 3 步「提契约」**：契约不是手写的，而是**从金标准页编译出来的**——
`facts`（观测：横带结构、行覆盖、双源交叉）→ `plan`（推断：阅读序 / 记录起点 / 属性锚词 / 不变量）。
两层拆开的意义是**解读的准确性可以单独检验**，不必等端到端效果出来才知道。

### 2.1 验收步（七步流程第 ⑤⑥⑦ 步）：汇总量化指标 → 人裁「全量通过」→ 未过则抽样循环

第 5 步 verify 跑完**不等于流程结束**——产物要交给人裁「这批能不能全量通过」：

```bash
# ① 汇总量化指标（三样材料齐全，组合成**一份**汇报）：
#    · verify 的合规判定（上一步）
#    · eval-iou 档案级三口径（A-9 ⑤；登记口径 = value_aligned_iou50）
#    · 产物置信分层（exec / draft 返回与产物里的 high / medium / low 各多少条）
# ② 汇报里给**结论建议**（通过 / 不通过 + 依据读数），**决定权在人**。

# ③ 人判「不通过」→ 随机抽样复核（2026-09-26 新增）：
python chronicles.py sample --n 3 --json
#    从产物池随机抽 n 页（**排除已有人工金标准的页**；池 < n 时全给并如实报）。
#    seed 缺省现场生成并**回报**在输出里 ⇒ 同 seed 可复现、可审计。
#    rc 0 → 抽到了；rc 3 → 池空（先跑 exec；若 n_excluded_gold = 产物总数，
#            本批已全量人工验证过，没有可抽的页）。
# ④ 逐页桥草稿 + 交人：
python chronicles.py draft --profile <pid> --stem <页名> --json
python chronicles.py ui --serve --stem <页名> --json
# ⑤ 人在界面改完（采纳即落 manual_annotations/）→ 回到 A-7 learn → 第二轮跑批。
#    循环直至人判「全量通过」（§0 七步图）。
```

★ **为什么抽样不做置信分层**：抽样的目的是**估计整批通过率**，
按置信度挑页会系统性偏向低置信页、把估计拉低。要定向复核低置信页时直接按页名点名
（`draft --stem`），那是另一件事，别和随机抽样混在一起。

★ **需求方要「上一轮效果最差的 3 页」时怎么办**（2026-09-30 审计裁定：**维持随机，不加排序口**）：
无标签页的「效果」本质不可测，只有代理信号——产物逐条 `confidence`、
`data/preannotations/<stem>.l0.json` 页级自检、exec 信封逐页 `qa_flags`。把这些做成
「按效果排序取最差」的命令口，会与上面「抽样不做置信分层」的纪律**正面冲突**（两套口径打架），
且 QA flags 消费口尚在取证（PENDING R3）。⇒ 现行口径：**仍用随机 `sample` 选页**，
代理信号只作**阅读优先级提示**（抽中页的 `picked_qa` / L0 读数已随输出回报）；
用户指名要某页就 `draft --stem <页名>` 定向，不改变抽样本身。

### 2.2 落点：默认写生产位置，试跑要重定向

`facts` / `plan` / `exec` 的**默认落点就是系统正在用的位置**（§4 表）。
⇒ 原地重跑 = **覆盖现有产物**（幂等，内容一致，但时间戳会变）。

**试跑 / 验收时**把中间产物引到别处，并把下游指回去：

```bash
python chronicles.py facts --profile <pid> --out <tmp>/plan --json
python chronicles.py plan  --profile <pid> --out <tmp>/plans --facts-dir <tmp>/plan --json
python chronicles.py exec  --profile <pid> --stem <stem> --plan-dir <tmp>/plans --out <tmp>/results --json
python chronicles.py verify --result <tmp>/results/<stem>.result.json --plan <tmp>/plans/plan_<pid>.json --json
python chronicles.py map   --plan <tmp>/plans/plan_<pid>.json --svg <tmp>/map.svg
```

★ **`--facts-dir` / `--plan-dir` / `--plan` 就是为这种重定向准备的**：
一旦上游换了目录，下游必须显式指回去，否则它会去默认位置找（找得到就静默用旧的，找不到就判缺料）。
`map` 同理：产物不在默认位置时用 `--plan` 指定，别用 `--profile`（后者取默认目录里最新的那份）。

★ 建线四步（`project` / `profile` / `import` / `ocr` / `annotate`）在**验收/试跑**时也要重定向：

```bash
python chronicles.py profile  new   --name <名> --attrs 甲 乙 --profiles-dir <tmp>/profiles --json
python chronicles.py profile  learn --profile <pid> --profiles-dir <tmp>/profiles \
                                    --gold <tmp>/gold --rules-dir <tmp>/rules --json
python chronicles.py project  new   --name <名> --json
python chronicles.py project  template --project <id> --file <模板> --profiles-dir <tmp>/profiles --json
```
`--profiles-dir` **务必带上**：漏了它，命令面会往**真实** `data/collection_profiles/` 写垃圾档案
（这个坑在真实金标准目录上踩过一次，见 `CLAUDE.md` §74.8）。

---

## 3. 退出码 = 自纠依据（agent 靠它判断下一步，不靠读自然语言）

| 码 | 含义 | 你的下一步 |
|---|---|---|
| **0** | 成功 | 继续下一步 |
| **2** | **面外拒答（正确行为，不是错误）** | **换材料 / 换档案 / 建新契约**；或确认要硬跑时加 `--allow-suspect` |
| **3** | **缺料** | **去补输入**（JSON 里 `next` 字段写明补什么） |
| **4** | 校验不过 | 查产物 / 改规则 / **改参数**（模板解析不出表头、档案属性超上限等） |
| **1** | 内部错误 | 需人看（stderr 有栈） |
| **64** | **用法错误** | **改参数** |

★★ **`2` 与 `3` 必须分开**：前者是"这类材料不该用这套契约"（**该换东西**），
后者是"该给的没给齐"（**该补东西**）。合并成一个非零码会让 agent **无法自纠**。

★★ **`64` 必须与 `2` 分开**：多数命令行框架的默认用法错误码**恰好是 2**，
不改就会与「面外拒答」撞车——而两者的下一步动作**完全相反**（改参数 vs 换材料）。
本命令面已把用法错误统一改投 `64`。

★ **`3` 与 `4` 也必须分开**：`3` 的下一步是**补材料**，`4` 的下一步是**改参数**。
例：「档案名/栏目名为空」判 **4**（改参数），「模板文件不存在」判 **3**（补材料）。

★ **`--allow-suspect` 的语义是"放行"，不是"看不见"**：它把退出码改成 0，
但**清单里仍照出被拒的条目**。放行 = "我知道它面外、仍要处理"。且它**不覆盖缺料**。

### 3.1 别只看码 —— JSON 里有 `next` 字段（"下一步怎么办"）

每个子命令的 `--json` 输出都带 `next`：**成功时给下一步命令，失败时给出路**。
退出码决定"停不停"，`next` 决定"停在哪、往哪走"。实测原文：

| 情形 | rc | `next`（实测） |
|---|---|---|
| 面内 | 0 | `继续：chronicles facts --profile <pid>` |
| 面外 | 2 | `把这批页移出本栏目（它们不像这个档案），或确认要处理时加 --allow-suspect 显式放行；若这些页本身就该是另一类材料，先为它建独立档案再分诊。` |
| 缺料 | 3 | `一个页都没读到：核对 --inbox 目录（是否有 .png/.jpg 等图像）或 --project 的栏目成员。` |
| 未学规则 | 3 | `先用 chronicles ocr 补页文本，再 chronicles annotate lines/add 标注至少 1 页，然后回来 profile learn。` |

---

## 4. 产物落点（★ 单复数不同，写反会静默生出空目录）

| 子命令 | 产物 | 目录 |
|---|---|---|
| `project new` | 追加一条栏目 | `data/projects.json` |
| `project template` | 栏目样式（含模板原文截断）+ 档案 | `data/projects.json` + `data/collection_profiles/` |
| `project assign` | 图 → 栏目归属 | `data/project_assignments.json` |
| `profile new` | `<profile_id>.json` | `data/collection_profiles/` |
| `profile learn` | `learned_<pid>.json` ★ 锚词 | `rules_data/` |
| `import` | 图像副本（保留原名）/ PDF 页图（`原名_NNNN.png`） | `inbox/`（或 `--to outbox`） |
| `ocr` | `<stem>.json` | `data/structured/` |
| `annotate add` | `<页名>.png.jsonl`（`box` 用**原图像素坐标**） | `manual_annotations/` |
| `facts` | `facts_<pid>.json` | `data/plan/` ⚠ **单数** |
| `plan` | `plan_<pid>.json` + `.md` | `data/plans/` ⚠ **复数** |
| `exec` | `<stem>.result.json` | `data/results/` |
| `draft` | `<stem>.ai.jsonl`（**与界面预标注同落点**；覆盖前原文归档到 `_archive/draft_superseded/`） | `data/preannotations/` |
| `sample` | 只读选择、不落盘（抽中页名与 seed 回报在输出里） | — |
| `verify` | 只判不写 | — |
| `map` | `.svg` / IR | `--svg` 指定 |

---

## 5. 边界：本流程明确**不承诺**什么

### 5.1 面外必须拒答，而且是默认行为

内核那层（供界面调用的）**只提醒不阻断**，把决定权交给调用方；
命令面这层**默认拒答（rc 2）**。两者默认相反是**故意的**——命令面需要给 agent 一个能自纠的判决。
**别把"默认拒答"当成 bug 改掉。**

### 5.2 「0 页」不是「全部通过」

内核在输入为空时会返回"全部 ok"（它假设调用方已经给了页）。
命令面已把这种情况定死为**缺料（3）**，三处同一条纪律：

| 子命令 | 「一个都没有」的语义 |
|---|---|
| `triage` / `ocr` | **缺料 3**（去补料） |
| `profile learn` | 本档案名下一行都没有 → **缺料 3**（去标页）★ 判成功的话你会一路跑到 triage 才知不对。**隔离后有两种成因**：行标在别的档案名下 / 行没标签（未归档）—— 报错里会分开说 |
| `project assign` | 「一张图都找不到」→ **缺料 3**（去 import） |
| `import` | 「全是重复、一张都没复制」→ **成功 0**（去重生效**不是**错误） |

### 5.3 （已补）OCR 现在在本命令面上

~~完整端到端还差一环：OCR 不在本命令面上~~ —— **2026-09-14 晚已补上**：
`chronicles ocr` 已接入（见 §2 步骤 A-5），「原始图像 → structured → facts → plan → exec →
verify」全程可在命令面走完，不再需要另一个通道做"输入准备"。

未 OCR 的新页在分诊里是 `unmeasurable`（既不算通过也不算拒答）—— 那是**缺料**，
先跑 `ocr` 再分诊，**别把 `unmeasurable` 当成功**（同 §5.2）。

⚠ 批量档里的 `submit_interval = 0.25 s` 是**承重**常量（避开 429 / 12002），
**不要**为了"更快"绕过它，也不要裸并发。粒度已由 `config.OCR_BATCH_PREFETCH` 调好，
想看本次实际用了多少，读返回 JSON 的 `engine` / `batch` 字段。

**★★ 三条实测事实（2026-09-16，10 页官报，全链 `elapsed_ms=528890.9`）——先读再判断"是不是慢得不正常"：**

1. **耗时的来源是服务端排队，不是本地。** 总耗时 = **max(单页)** 而非 sum(单页)：
   预取并发会让已完成页在预取完成的一瞬间**同时**落盘（日志里 `命中预取结果 … +0.0s`）。
   本地侧开销可忽略 —— 图像解码 0.237 s/页 + 几何重建 0.04 s/页 + 提交间隔 2.25 s + 限流退避 1.1 s，
   **10 页合计 < 30 s**。⇒ **不要**为"跑得慢"去改本地参数或加并发，那只会撞限流。
   判据：看 stderr 里 `预取完成 batch=… 成功 N / 失败 M（Xs, 并发 K）` 那一行的 `X` —— 它就是服务端侧的真耗时。
2. **★ `code=10010`「任务提交队列已满」会让你丢页，且被报成 `rc 1 内部错误`。**
   提交重试（`OCR_SUBMIT_RETRY=3` + `BACKOFF=0.6`）只退避 ≈4 s 就放弃 ⇒ **该页永久失败**，
   但**这是外部容量状况，不是你的代码坏了**。
   **正确处置：等一会儿重跑同一条命令** —— 已完成页会落盘、不受影响（`import`/`ocr` 全程幂等），
   只有缺的那几页会重投。**别去翻代码，也别改 `ocr` 参数试图"修"它。**
3. **服务端按图像内容缓存**（实测：同一张图重跑 2.8 s，从未成功过的新图 194 s）。
   ⇒ 跨批次重跑同一批材料**近乎免费**；反之**首次跑新内容才是真花钱**。
   ⚠ 本项目侧的 `_cache` 是**进程内** dict（key = 路径 + size + mtime_ns）⇒ **跨进程/跨会话一定会重新提交**，
   不要指望本地缓存省额度。

### 5.4 ★★ 金标准按**档案**隔离（2026-09-16）

**每个档案只读自己那一份金标准。** 别指望「目录是全局的所以都能用」——

| 概念 | 是什么 | 决定什么 |
|---|---|---|
| **档案**（`profile`，`prof_*`） | 文献**类型** | ★★ **决定隔离**：`facts` / `learn` 只读行上 `profile == 本档案` 的行 |
| **栏目**（`profile` 之外的 `project`，`*_xxxxxx`） | 一批材料的**主题**归类 | 只决定**组织与视图**（哪些页放一起、进度看哪一批），**不决定隔离** |

三条要知道的事：

1. **行上的 `profile` 标签才是隔离依据。** 界面在**未绑定档案**时保存的标注行**不带标签**
   ⇒ 成为「**未归档**」⇒ **在任何档案下都会被排除**（包括不属于「全部」）。
   这是**设计**：无标签的行没法归给谁。`chronicles gold` 会把未归档的页/行数印出来。
2. **别把「本档案读不到」当成产品坏了。** 先跑 `chronicles gold` 看两件事：
   那些行是**标在别的档案名下**，还是**根本没标签**。前者改归属，后者先绑定档案再重新保存。
3. **隔离只约束「推断口」。** `/api/seam_subjects`（让人挑例题的入口）与 `gold` 的
   项目侧计数**刻意不按档案过滤** —— 它们不替人做判断。**别去"修"它们。**

★ **单格式前提下（2026-09-26）本机只有一个档案与一个栏目**（§1.1 预置表）。
隔离机制照常生效——它是数据正确性的保障，与档案个数无关；多档案时学到的判据
（错归属的行会被静默排除）在单档案下同样是防呆的一部分。

### 5.5 单字准确率不是本流程的目标

本流程保证的是「**产物能被机械校验、缺口会被显式声明、面外会被拒答**」。
它不保证每个字都对——**不静默丢字**（缺口写进 `unassigned_text`）比"看起来读对了"重要。

### 5.6 （已补）档案现在能从命令面建 —— 但**别忘了 `learn`**

~~档案仍只能从界面建~~ —— **2026-09-15 已补上**：`chronicles project template`
（传模板 → 挂样式 + 建档案）与 `chronicles profile new`（直接给属性名）都在命令面上，
`profile add-attr` 可追加属性。

★ **要打开标注页**：`chronicles ui --serve`（§5.7）—— 2026-09-16 起已获授权起服务。

⚠ **仍要按人机分工办**：**属性名是人的判断**（哪些列是"公司名"、哪些是"资本额"），
不是 agent 猜的。agent 可以做的是：**读模板文件、把表头原样列给人确认**，
或**从金标准页的 OCR 行里提候选给人挑**。

★★ **最容易漏的不是建档，而是建档之后的 `profile learn`**（§1.3）：
标注完金标准页 **≠** `triage` 能用。**没有 `learn` ⇒ `anchors` 为空 ⇒ 整批 `unmeasurable`（rc 3）。**
`profile show --profile <pid>` 会报"已学规则"的现状，不确定就先看它。

### 5.7 ★★ 「起服务」已获授权：`chronicles ui [--serve]`（2026-09-16 裁定修订）

~~本命令面的纪律是「给地址，不替人起服务」~~ —— **该裁定已作废**。用户原话：

> 同意加 `chronicles ui [--serve]`，且老裁定已经明显阻碍到了用户对产品的顺利体验，
> 我决定修改该裁定，**允许在运行管线中的必要节点起服务**。

```
chronicles ui                        # 只报地址 + 状态（**缺省仍不起**）
chronicles ui --serve                # 起服务（**已在跑则复用，不重复起**）
chronicles ui --serve --stem <页名>   # 起服务 + 直出该页的标注地址
chronicles ui --json                 # 信封（server_up / url / annotate_urls）
```

★★ **统一裁定（2026-09-30）**：本通道**只有**技能包自有交互面
（`skill/chronicles-app/annotate_server.py`，**端口 5001**）。桌面壳的后端
（app.py，5000）由桌面壳自己拉起/重启——本通道**不再代拉、不再探测它**，
本机与分发包**行为完全一致**：没有「本机落壳、分发落 skill」的分叉。
（2026-09-16 批次 C 的 `--surface auto|app|skill` 开关随本裁定移除；
它修掉的「分发断点」由"恒 skill 面"永久保住。）

★ **探测 / 复用语义**：起前探 5001，已在跑就复用；**壳的 5000 开不开与本通道
无关**（两边可并存：壳 = 完整工作台，5001 = 标注 / 验收页）。
★ 缺省不 `--serve` 是刻意的：不让「顺手起服务」变成新的默认副作用。
★ `--open-browser` 缺省**关** —— 本机默认浏览器是联想浏览器，agent 起服务时不该弹窗。
  判据只有一条 —— **`app.py` 在不在**。
★ 探测**先于**选面：已在跑就复用，**根本不看形态**（避免与壳起的那个面打架）。
★ app 面自动带 `--max-backfill 0` —— **起界面 ≠ 批处理入口**：
  启动时那次 inbox 扫描在 serve **之前同步跑**，10 张≈100 s，会顶掉 30 s 的等待窗，
  表现成"服务起不来"（假失败）。要回填就显式跑 `chronicles ocr`。

★ **与 `next` 的分工**：`next` = 看下一步在哪（按页分类，只探测）；
★ **桌面壳不跟 `--max-backfill 0` 这条走，是故意的**：壳是**产品**，续跑中断的 inbox
  是它 2026-08-19 特意加的行为（`app.py` 的 argparse 默认 10）；`ui --serve` 是
  **agent 起界面**，才需要 0。壳那边已把值**显式写出**（`desktop_shell/main.js` 的
  `BACKEND_ARGS`）⇒ **两面值不同、理由各注一处**；"口径一致"指参数面显式化，不是抄值。

`ui` = 把界面交到人手里（地址 / 起服务 / 某一页的直达 URL）。

---

## 6. 排错

| 现象 | 先查什么 |
|---|---|
| `rc 2` 但你确信材料是对的 | 档案的锚词覆盖是否够（判据 = 本页锚词命中数下限）。**若这批材料本是另一类 → 该建独立档案**，别硬放行 |
| `rc 2` 且是手写/异体材料 | 大概率是**该类材料的契约还没建**（现有档案可能只覆盖印刷名录）——需要先建档案 + 学规则 |
| `rc 3` 且 JSON 说"没有读到页" | 核对 `--inbox` 目录 / `--project` 的栏目成员 |
| `rc 3` 且 `triage` 报 `unmeasurable` | **多半是漏了 `profile learn`**（锚词为空）—— 先 `profile show` 看"已学规则" |
| `rc 3` 且是 `triage --project` 取空 | **多半是漏了 `project assign`**：`import` **不写归属**（静默断裂）—— 见 §2 步骤 A-4 |
| `rc 4` | 参数/材料**格式**问题：空名、模板解析不出表头、属性数超上限 —— **改参数或改模板** |
| `rc 64` | 参数写错。检查 `--profile` 是否漏（**它是必需的**：判据来自档案，不存在"像不像名录"这种无档案的绝对问法） |
| `import` 报 `rc 3` | 路径里**既没有图像也没有 PDF**（路径写错 / 目录空了） |
| `import` 报 `rc 1` 且 errors 提到 PyMuPDF / fitz | 本机缺 `fitz` ⇒ `python -m pip install PyMuPDF` 后重跑 —— **幂等**，已导入的页不会重复 |
| `import` 把一堆不相干的图也导进来了 | 目录**递归**扫的。已混入的图会被改名（抹掉期号/页序）⇒ 先 `--dry-run` 核对，或精确给文件 |
| `exec` 产出的不是我这批新页 | **没给页来源** ⇒ 跑的是 `plan.source.pages`（方案编译页）。改用 `--project` / `--pages`；读产物 `page_source` 可查证 |
| `exec` 报 `n_skipped > 0` | 那些页**还没有 OCR 正文**（`reason=no_structured`）⇒ 先 `ocr`。**不是失败** |
| `exec` 的 `--out` 建出一个怪目录 | `--out` 是**目录**不是文件名 —— 应传 `data/results` 而不是 `xxx.result.json` |
| `draft` 报 `rc 4`（对账不过） | **拒绝落盘**：从产物重建的条目 ≠ 产物本身 ⇒ 产物与当前方案/契约已不同源。**先重跑 `exec`**，别绕过这道闸 |
| `draft` 某页 `existing_differs`（**rc 仍是 0**） | **不是失败**：那页本来就有草稿，默认**不动它**。看 `diff_summary` 判差异；确要替换加 `--overwrite`（原文自动归档）。**若差异只在置信度** → §2 A-8 末段的口径冲突 |
| 界面上看不到 agent 刚跑出来的自动标注 | **忘了跑 `draft`** —— `exec` 的产物是机器格式，界面裁决区不认它。先桥一次（§2 A-8） |
| `import` 说"全是重复、未再复制"（`rc 0`） | **这不是错误**：材料已在系统里，去重生效了。直接 `ocr` 即可 |
| `import` 重跑同一批，第二次 `n_copied = 0` | ★ **幂等生效，不是失败**：读 `n_skipped_existing`（落位目录**已有同图**）与 `n_skipped_dup`（**批内**重复）—— 这是**两个不同信号**（前者 = 材料本来就在，后者 = 这批自己重了）。⇒ **重试安全**，不会造出 `x_2` 副本。★ 查重范围**只覆盖落位目录**（`dedup_scope` 会报出来） |
| `annotate add` 报 `rc 3` | 该页**没有 OCR 行** ⇒ 先跑 `ocr`。这是缺料，不是参数错 |
| `annotate add` 报 `rc 64` | 参数问题：`--line` 越界 / `--box` 不是 4 个数 / 给了 `--profile` 但格式不对 |
| `annotate` 提框时发现行数不对 | 该页的 `structured` 可能来自旧版 OCR —— 重跑 `ocr` 再提框 |
| `profile learn` 报 `rc 3` | 库里没有金标准页 ⇒ 先 `annotate add` 至少 1 页 |
| `project assign` 报 `rc 3` | 图在 `inbox/`·`outbox/` 里都找不到 ⇒ 先 `import`（或核对你给的名字是不是 basename） |
| `verify` 报 `layout_uncovered` 之类告警 | 是**告警不是错误**——表示该页产出需人工复核。合规判定不受影响 |
| `sample` 报 `rc 3` | 产物池空：先 `exec`。若 `n_excluded_gold` 等于产物总数 ⇒ 本批已全量人工验证过，没有可抽的页 |
| `ocr` 报 `rc 1`，errors 里提到 `code=10010` / 「任务提交队列已满」 | **不是 bug**：服务端**提交队列满**（外部容量）。**等一会儿重跑同一命令即可**，已完成页正常落盘不受影响（§5.3 第 2 条）。**别改参数、别翻代码** |
| `ocr` 跑得慢（10 页要几分钟），怀疑哪里浪费了 | 服务端排队，不是本地。读 stderr 的 `预取完成 batch=… （Xs, 并发 K）`：那个 `X` 就是服务端真耗时；本地总开销 < 30 s（§5.3 第 1 条） |
| `ocr` 重跑同一批材料，第二次明显快很多 | **服务端按内容缓存**（§5.3 第 3 条），不是本地缓存生效 —— 跨进程重跑仍会重新提交 |
| 端到端跑通 ≠ 可交付 | 判据是"**换一份材料**（不是同一文件）还能重跑通"，见 §7 |

---

## 7. 验收基准（怎么算"真的跑通了"）

**一次成功不算数**，至少满足：

0. **前置齐备**：预置栏目与档案在位（`gold --json` 自检，§2 A-0）+
   **锚词（`profile learn`）** + 金标准页（界面标注）+ 页文本（`ocr`）+ 归属（`project assign`）
   —— 缺任何一样，后面的"通过"都不作数
   （`unmeasurable` 与缺料 3 **都不是通过**）；
1. **换一份材料**（同类但不是同一文件）重跑，仍能走完六步 —— **这才是及格线**；
2. 面外材料能被**拒答**（rc 2），而不是给一份空清单硬跑；
3. `verify` 对新产物判**合规**（rc 0）；
4. 产物的**缺口声明**（`unassigned_text`）非空时，能指认到具体是页眉/页脚/无属性文字；
5. ★★ **飞轮闭合**：`draft` 之后能**逐条裁决**这批产物（采纳 / 改写 / 驳回）——
   在界面上点，或用命令面 `adjudicate accept --idx N`（§2 A-9）；采纳的那条真落进
   `manual_annotations/`（`source=ai_verified`），并对下一轮 `profile learn` 的 **diff**
   有可见影响 —— **这一条不算数，「这不是一次性流程」就不算成立**（§2 A-8 / A-9）；
6. ★ **验收环闭合**（2026-09-26）：跑批后 `sample` 能从产物池抽出**未验证**的页、
   **排除**已有人工金标准的页、同 seed 复现同一组页名 ——
   「汇总指标 → 人裁全量通过 → 未过则抽样复核 → 第二轮」这条环有真实入口（§2.1）。

---

## 8. 另一条通道：MCP（stdio）

同一个命令面已暴露为 MCP server：`skill/chronicles-mcp/server.py`（**纯标准库、零依赖**）。
下面的全部流程、退出码、产物落点**一字不变**，只是换成工具调用。

| 事项 | 形态 |
|---|---|
| 工具数 | **39 个（2026-09-30 实测）= 命令面叶子一一对应**（`profile`/`project`/`annotate`/`adjudicate` 四组按**动作**展开，因为按顶层展开会撞参数名）。★ 工具表**由 `argparse` 现推**（`server.py` 里**没有**手写登记表）⇒ 命令面加一个叶子就自动多一个工具（`confirm` 即如此，2026-09-30）；**别去 server.py 里「补登记」**，那会造出第二份真相 |
| 工具名 | `chronicles_<命令路径>`，连字符转下划线（`profile add-attr` → `chronicles_profile_add_attr`） |
| 参数名 | CLI 长选项转下划线（`--add-attr` → `add_attr`） |
| schema | 由 `argparse` **现算**，不是手抄的 ⇒ 与 CLI 不会漂移（已用真实 `--help` 逐项证伪） |

★★ **不要用 `isError` 判断下一步** —— 它是「调用本身坏了」，只有两类：

| 退出码 | `isError` | 含义 |
|---|---|---|
| 0 / 2 / 3 / 4 | **false** | 都是**正常结果**：成功 / 面外拒答 / 缺料 / 校验不过 |
| 1 / 64 | true | 内部错误 / 用法错误 |

⇒ 判断依据看 **`structuredContent.exit`**（以及 `result.advice` / `result.next`），
与命令行里读退出码**同一套语义**（§3）。这也正是这套命令面能经 MCP 交给任意客户端的原因。

---

## 附录：一次性初始化（新环境装好后执行一次；日常会话禁再执行）

单格式前提（§1.1）下，正常会话**不建线**——栏目与档案已初始化并固定。
只有**全新环境**（A-0 `gold --json` 查无档案）才走本附录，且**只走一次**：

```bash
# 1. 建栏目（人给名字；本项目 = 商务官报）
python chronicles.py project new --name "商务官报" --json
#    rc 0 → 建好（★ 幂等：同名已存在则复用，不建第二个；要另立用 --force-new）

# 2. 传汇总模板 → 同时挂样式 + 建档案（人给模板文件；属性名来自模板表头）
python chronicles.py project template --project <栏目id> --file "<汇总模板.xlsx>" --json
#    模板首个有效行的各列名 → 档案属性。同 label 重传 = "改一改再传"（沿用原档案、合并属性）
#    rc 4 → 模板解析不出表头（改模板）；rc 3 → 栏目/文件不存在（补输入）
#    ★ 也可以先手工建档、以后再挂模板：
# python chronicles.py profile new --name "<档案名>" --attrs 公司名 注册人 资本额 --json

# 3. 回到 §2 步骤 A-0 自检确认在位，再进入主流程。
```

⚠ **属性名是人的判断**（哪些列是"公司名"、哪些是"资本额"），agent 可做的只是
读模板文件、把表头原样列给人确认。⚠ **验收/试跑时**建线命令也要重定向
（`--profiles-dir` 等，见 §2.2）——漏了它会往**真实**档案目录写垃圾档案
（这个坑在真实金标准目录上踩过一次，`CLAUDE.md` §74.8）。

---

## 参考

- `chronicles.py --help` / `chronicles.py <子命令> --help` —— 每个参数的权威说明（**读代码确认，别信二手文档**）
- `page_triage.py` —— 分诊的独立入口（同源同码；不带 `chronicles` 前缀时用它）
- `ocr_cli.py` / `import_cli.py` / `manual_annotate.py` —— 三个入口的独立实现
- `profile_cli.py` / `project_cli.py` —— 建线与归属的独立实现
  （`chronicles profile/project/ocr/import/annotate` **都只是转调**，逻辑都在这些模块里；
  退出码判定点也在各自模块内，**包装层不重判**）
- `draft_bridge.py` —— **人面桥**的实现（`chronicles draft` 只是转调，判定点在它内部）。
  它**不重跑识别**：调回 `plan_exec.contract_facade` + `preannotate.anchor_page` 同一实现，
  再与产物逐条对账；`draft_exit_code()` 是它唯一的判定点。
- `examples/external_agent_demo.py` —— 「执行层可被外部实现替换」的完整示例：
  不依赖本项目任何模块，只凭 `plan_<pid>.json` 就能产出合规结果并接受 `verify` 校验。
  **若你要自己实现第 4 步**（而不是调 `exec`），照它做。
