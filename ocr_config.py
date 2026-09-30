"""OCR 后端配置读取（2026-09-30 从 app.py 下沉）。

★ 为什么存在：`load_baidu_config` / `_cfg_num` / `OCR_BACKEND_CONFIG_FILE`
原是壳（app.py）的私有物，但 skill 通道（preannotate_gen.build_engine、ocr_cli）
同样需要「同源读一份 OCR 配置」⇒ 下沉为独立模块，壳与 skill 都从这里 import，
**单一真相**，杜绝两边各读一份配置漂移。

依赖边界：只依赖 config.py 的常量 + 标准库 + json 配置文件，
**不 import app / flask**——本模块必须可被无壳环境（skill 分发包）直接使用。
"""
import json
import os
from pathlib import Path

import config

_PROJECT_ROOT = Path(__file__).resolve().parent

# 百度 OCR 配置持久化文件（gitignore，不进版本控制）
OCR_BACKEND_CONFIG_FILE = _PROJECT_ROOT / "ocr_backend_config.json"


def _cfg_num(data: dict, key: str, default, cast):
    """从配置 dict 取一个数值；缺值 / 坏值一律回落 `default`（**绝不改默认**）。

    ✅ 2026-09-14 P-OPT-6：`ocr_backend_config.json` 是用户手改的，写入 `""`、
    字符串数字、甚至 `null` 都很常见 —— 解析失败只忽略该项，不让整体读取崩掉。
    """
    v = data.get(key)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def load_baidu_config() -> dict:
    """从 json 配置文件读百度 OCR key/model/backend（不返回 key 明文给前端）。
    优先级：json 文件 > 环境变量 > config.py 默认。

    ✅ 2026-08-20：兼容用户 setx 的变量名（Windows 环境变量大小写不敏感）：
    - PaddleOCR-VL token：PADDLEOCR_VL_TOKEN（= paddleocr_vl_token）
    - 百度 key：BAIDU_API_KEY / BAIDU_SECRET_KEY（= Baidu_api_key / Baidu_secret_key）
      同时保留 config.py 原用的 BAIDU_OCR_API_KEY / BAIDU_OCR_SECRET_KEY 写法。

    ✅ 2026-09-14 P-OPT-6：新增四项**吞吐调参**（`ocr_poll_interval` / `ocr_max_wait` /
    `ocr_batch_prefetch` / `ocr_batch_max`），默认值单点在 `config.py`，json 可覆盖。
    ✅ P-OPT-7：再加三项（`ocr_batch_parallel` / `ocr_submit_retry` /
    `ocr_submit_backoff`），同口径 —— 默认值在 `config.py`，json 可覆盖。
    """
    api_key = os.environ.get("BAIDU_OCR_API_KEY") or os.environ.get("BAIDU_API_KEY") or config.BAIDU_OCR_API_KEY
    secret_key = os.environ.get("BAIDU_OCR_SECRET_KEY") or os.environ.get("BAIDU_SECRET_KEY") or config.BAIDU_OCR_SECRET_KEY
    model = config.BAIDU_OCR_MODEL
    backend = config.OCR_BACKEND  # config.py 默认
    paddleocr_vl_token = os.environ.get("PADDLEOCR_VL_TOKEN", "")
    paddleocr_vl_job_url = os.environ.get("PADDLEOCR_VL_JOB_URL", "")
    # ✅ 2026-09-14 P-OPT-6：吞吐调参（默认值来自 config.py 单点）
    poll_interval = config.OCR_POLL_INTERVAL
    max_wait = config.OCR_MAX_WAIT
    batch_prefetch = config.OCR_BATCH_PREFETCH
    batch_max = config.OCR_BATCH_MAX
    batch_parallel = config.OCR_BATCH_PARALLEL
    submit_retry = config.OCR_SUBMIT_RETRY
    submit_backoff = config.OCR_SUBMIT_BACKOFF
    if OCR_BACKEND_CONFIG_FILE.exists():
        try:
            data = json.loads(OCR_BACKEND_CONFIG_FILE.read_text(encoding="utf-8"))
            api_key = data.get("api_key") or api_key
            secret_key = data.get("secret_key") or secret_key
            model = data.get("model") or model
            # ✅ 2026-08-18：后端选择也持久化（用户可在 dashboard 切换 baidu/paddleocr_vl）
            backend = data.get("backend") or backend
            # ✅ 2026-08-18：PaddleOCR-VL 星河社区 token + job_url
            paddleocr_vl_token = data.get("paddleocr_vl_token") or paddleocr_vl_token
            paddleocr_vl_job_url = data.get("paddleocr_vl_job_url") or paddleocr_vl_job_url
            # ✅ 2026-09-14 P-OPT-6：吞吐调参（缺项 / 坏值回落 config.py 默认）
            poll_interval = _cfg_num(data, "ocr_poll_interval", poll_interval, float)
            max_wait = _cfg_num(data, "ocr_max_wait", max_wait, float)
            batch_prefetch = _cfg_num(data, "ocr_batch_prefetch", batch_prefetch, int)
            batch_max = _cfg_num(data, "ocr_batch_max", batch_max, int)
            batch_parallel = _cfg_num(data, "ocr_batch_parallel", batch_parallel, int)
            submit_retry = _cfg_num(data, "ocr_submit_retry", submit_retry, int)
            submit_backoff = _cfg_num(data, "ocr_submit_backoff", submit_backoff, float)
        except Exception as e:
            print(f"  [警告] 读 {OCR_BACKEND_CONFIG_FILE} 失败: {e}")
    return {
        "api_key": api_key,
        "secret_key": secret_key,
        "model": model,
        "backend": backend,
        "paddleocr_vl_token": paddleocr_vl_token,
        "paddleocr_vl_job_url": paddleocr_vl_job_url,
        "ocr_poll_interval": poll_interval,
        "ocr_max_wait": max_wait,
        "ocr_batch_prefetch": batch_prefetch,
        "ocr_batch_max": batch_max,
        "ocr_batch_parallel": batch_parallel,
        "ocr_submit_retry": submit_retry,
        "ocr_submit_backoff": submit_backoff,
    }
