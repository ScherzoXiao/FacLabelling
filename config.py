"""项目配置 — 便携版（PyInstaller 兼容）。

项目根 = 脚本所在目录（开发模式）或 exe 所在目录（PyInstaller 冻结模式）。
不再硬编码 D:\\ 路径，支持任意位置解压运行。
"""
import os
import sys
from pathlib import Path

# ✅ PyInstaller 冻结模式检测
# frozen=True → PyInstaller 打包后的 exe 运行
# sys.executable → exe 文件路径，其 parent 即为安装目录
# 非 frozen → 开发模式，用脚本所在目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent.resolve()
else:
    BASE_DIR = Path(__file__).parent.resolve()

# 监听目录
INBOX = BASE_DIR / "inbox"

# 已处理目录
OUTBOX = BASE_DIR / "outbox"

# 失败目录：OCR 失败的图片移到这（不会永久堆在 INBOX）
FAILED_DIR = BASE_DIR / "failed"

# Excel 输出
OUTPUT_XLSX = BASE_DIR / "output.xlsx"

# 手动标注目录（全图拖拽画框+输入文字，绕过 OCR 直接产出训练数据）
MANUAL_ANNOT_DIR = BASE_DIR / "manual_annotations"

# ✅ M22 清理：原 VENV_DIR / MODELS_DIR 零引用，已删。

# ========== 界面版本号（2026-09-13 新增）==========
# 侧栏页脚显示的那个版本号 —— 它的唯一用途是"改版后硬刷页面，肉眼确认拿到的是新前端"。
# ★ 纪律：**只在改界面时 +1**，改后端逻辑不必动它。
# ★ 为什么放进 config 而不是写死在模板里：原先 dashboard / review / eval 三份页脚
#   各写一个版本号（且各自尾巴上挂着本批次变更说明），早已漂移到 v4.17 / v4.9 / v4.9 ——
#   三处同一事实、三个值。改为单点注入后升版只改这一行（`app.py` 的 context_processor
#   把它推给所有模板），漂移从结构上不可能再发生。
APP_VERSION = "v5.0"

# Flask
FLASK_HOST = "127.0.0.1"
# 端口 5000 可能被其他 v2 进程占着；用户通常经桌面快捷方式（launcher_visible 链路）启动，自动选空闲端口
FLASK_PORT = 5000
FLASK_PORT_FALLBACK = 5001  # 若 5000 占用，自动回退到 5001
# 技能包交互面（chronicles ui 通道）专用端口 —— 与壳的 5000 无关（2026-09-30 统一裁定：
# chronicles 通道只有 skill 面；壳的后端由桌面壳自己拉起/重启，两边可并存）。
SKILL_FLASK_PORT = 5001

# 支持的图片扩展名
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# ========== OCR 后端选择（2026-08-18 新增）==========
# ✅ 2026-08-20：本地 "paddleocr" 引擎已移除（应用放弃本地 OCR），
# 仅保留云端后端："paddleocr_vl"（默认，星河社区，需 token） / "baidu"（需 key）
OCR_BACKEND = "paddleocr_vl"

# 百度 OCR 配置（从环境变量优先读取，避免进 git）
# 申请地址：https://ai.baidu.com/tech/ocr
# 推荐用环境变量：setx BAIDU_OCR_API_KEY your_key
BAIDU_OCR_API_KEY = os.environ.get("BAIDU_OCR_API_KEY", "")
BAIDU_OCR_SECRET_KEY = os.environ.get("BAIDU_OCR_SECRET_KEY", "")
BAIDU_OCR_MODEL = os.environ.get("BAIDU_OCR_MODEL", "accurate_basic")  # 古籍模型需单独申请


# ========== PaddleOCR-VL 吞吐调参（2026-09-14 P-OPT-6 新增）==========
# ★ 全部可在 ocr_backend_config.json 里覆盖（同名字段，见 app.load_baidu_config），
#   不改代码即可调；这里只是默认值。
#
# poll_interval：官方是**异步任务**，客户端靠轮询取结果。旧值 5.0 s 的代价是
#   每页至少吃满一个刻度 —— 实测单页最短 5.4 s 紧贴刻度（服务端早已跑完，纯空等）。
#   收窄到 2.0 s 后平均空等从 ~2.5 s 降到 ~1.0 s。查询失败（含 429）在原实现里
#   本就是 `continue` 重试，故收窄不增加失败面。
OCR_POLL_INTERVAL = 2.0
# 单张任务最长等待（秒）。长尾实测 max 176.2 s，留足余量。
OCR_MAX_WAIT = 600.0
# 批量预取：批量导入时**先整批提交**（同一 batchId），watchdog 逐张处理时命中
#   已提交任务 → 服务端并行，总耗时 Σ→max。
#
# ✅ 2026-09-14 P-OPT-7 标定实测（12 页《官报》，同一 token，并行度 12）：
#   · 服务端**确实并发**：相位 A（串行）最大同时运行 job 恒为 **1**；
#     相位 B（批量）为 **7**，时间跨度 4.06 s vs Σ单 job 16.67 s → 压缩 4.10×。
#   · 端到端：A 串行 18.0 s / B 批量 4.1 s = **4.36×**（同为 12 张，12/12 成功）。
#   · 零节流并发提交 **会撞 HTTP 429 / code 12002**「请求频率过高」⇒
#     `submit_interval`（0.25 s）是**承重的**，不能删；另有退避重试兜底。
#   · 批量查询端点 `GET /jobs/batch/{batchId}` 实测可用（HTTP 200 / code 0；
#     `data.extractResult[]` 每项含 jobId / state / resultUrl / extractProgress）。
#   · 服务端耗时随负载波动极大（历史 p50 5.6 s、mean 23.5 s、max 176.2 s），
#     故 **N 越大、批量收益越大**（Σ→max）；上列 4.36× 是负载最轻时的下界。
#   0    = 关闭（行为与 P-OPT-6 之前完全一致）
#   >0   = 单次预取的最大张数（官方硬上限 100，见 OCR_BATCH_MAX）
OCR_BATCH_PREFETCH = 100
# 预取并发度（提交 + 轮询 + 下载的工作线程数）。标定实测 12 并发零失败；
#   取 8 留余量，避免与服务端队列上限（10010 / 12002）贴边。
OCR_BATCH_PARALLEL = 8
# 提交遇**频率类**错误（HTTP 429 / body code 12002 / 10010）时的重试次数与
#   首次退避秒数（指数退避 + 抖动）。标定实测该错误真的会发生；不重试就会让
#   那一张退回逐张路径（在批量场景下等于把并行的收益退回去）。
OCR_SUBMIT_RETRY = 3
OCR_SUBMIT_BACKOFF = 0.6
# 官方硬约束：同一 batchId 最多 100 条 job（超限错误码 10009）→ 超出自动切批。
OCR_BATCH_MAX = 100


# ========== 页图解析（2026-09-15 WP-2a · 全项目唯一解析口）==========
# ★ 为什么必须有这一处（这是本文件里唯一为「命令面」而加的东西）：
#
#   原系统的**真实链路**是常驻 watchdog 编排的 —— 剪贴板/批量导入把图丢进 `inbox/`，
#   后台 `process_image` 做完 OCR 后**把图搬到 `outbox/`**（`app.py:619` 注释原文：
#   「真实链路：process_image 把图从 inbox 移到 outbox 后…」）。于是下游（几何读取 /
#   管线取行 / 校验器读真值）一律**只去 `outbox/` 找图**，这是当时成立的假设。
#
#   技能包把 OCR 从 watchdog 里摘出来自己跑（**设计正确**：命令面不能依赖别的进程
#   还活着），但**搬运这一步没有接上** —— 而 `chronicles import` 的落位是 `inbox/`
#   （与剪贴板通道一致，用户 2026-09-15 裁定保持）。结果：几何读取取不到真值，
#   **静默**退回「框的极值」当页尺寸。控制实验实测（同页同方案，唯一变量 = 图位置）：
#
#       图在 inbox → page={"w":887,"h":1643,"source":"boxes"}   rc 0，无任何提示
#       图在 outbox→ page={"w":953,"h":1787,"source":"image"}   rc 0
#       真实原图   → 953×1787  ⇒ 静默降级把页宽少算 −6.9%、页高 −8.1%
#
# ★ 口径不是新定的，是**把全项目既有的惯例收成一处**：7 个模块早就是「两处查」——
#   `app.py` / `export_training_data.py` / `image_rename.py` / `project_cli.py` /
#   `batch_import.py` 一律 `for d in (OUTBOX, INBOX)`。此前**只有几何读取那一族**
#   （`preannotate.page_lines` / `plan_exec.run_page` / `seam._resolve_page_size`）
#   是单目录 —— 那不是设计选择，是遗漏。
#
# ★ 测试隔离点：要让某次调用**只搜指定目录**，不要改这里，用 `given=` 传参
#   （给了就只搜它）—— 显式参数优先，隔离面不会被悄悄放大。
IMAGE_DIRS = (OUTBOX, INBOX)      # 顺序即优先级（与全项目惯例一致）


def image_path_of(stem: str, given=None, exts=None):
    """页名（stem）→ 页图路径；两处都找不到 → `None`。

    **本函数是全项目解析页图的唯一入口**（`CLAUDE.md` §60）——
    `preannotate.page_lines` / `plan_exec.run_page` / `seam._resolve_page_size`
    一律调它，**不得各自拼路径**（此前各自拼路径正是"静默降级"的成因）。

    :param stem:  页名，**不带后缀**（如 `官报_1908年8期卷38-43页_0002`）；
                  若调用方已带图像后缀，按原名先找一次。
    :param given: 调用方**显式指定**的目录（CLI 的 `--outbox`、测试的 tmp 目录）。
                  **给了就只搜它** —— 显式参数优先（既有语义），且保证测试隔离
                  不会被"顺带搜真实 inbox"放大。
    :param exts:  后缀白名单，默认 `.png` 优先、其余按字典序（历史实现固定 `.png`，
                  故 `.png` 必须排第一，避免行为变化）。
    """
    if not stem:
        return None
    dirs = [Path(given)] if given else [Path(d) for d in IMAGE_DIRS]
    name = str(stem)
    sufs = list(exts) if exts else ([".png"] + sorted(IMAGE_EXTS - {".png"}))
    names = ([name] if Path(name).suffix.lower() in IMAGE_EXTS else []) + \
            [f"{name}{s}" for s in sufs]
    for d in dirs:
        for n in names:
            try:
                p = d / n
                if p.is_file():
                    return p
            except OSError:
                continue
    return None


