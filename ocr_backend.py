"""OCR 后端抽象 + 工厂 + 云端 OCR API 实现。

架构（2026-08-18）：
- `OCRBackend` 抽象基类：定义 recognize(image_path) / is_ready 接口
- `BaiduOCRBackend`：百度云 OCR 实现（古籍专用模型）
- `PaddleOCRVLBackend`：飞桨 PaddleOCR-VL（星河社区，异步 API）

✅ 2026-08-20：本地 PaddleOCR 引擎（ocr_engine.py）已彻底移除，
应用仅保留云端后端（baidu / paddleocr_vl）。

用户启用百度 OCR：
1. 申请百度 AI 开放平台账号：https://ai.baidu.com/tech/ocr
2. 创建"通用文字识别（高精度版）"或"古籍识别"应用，获得 API_KEY + SECRET_KEY
3. 设置环境变量（推荐）：
       setx BAIDU_OCR_API_KEY=your_key
       setx BAIDU_OCR_SECRET_KEY=your_secret
   或在 config.py 中填入（不推荐，会进 git）
4. 改 OCR_BACKEND = "baidu"（默认是 "paddleocr_vl"）
5. 重启 app.py —— 自动启用百度 OCR

注意事项：
- 百度 OCR 按调用次数收费（古籍模型 0.04 元/次，有免费额度）
- access_token 30 天有效，自动缓存到过期前 5 分钟刷新
- 百度 OCR 返回按行结构（无列分隔），col_index 固定 0

P0-2 安全修复（2026-08-19）：
- 单一事实来源：VALID_BACKENDS 是后端白名单的唯一真相
- app.py / 其他模块不得硬编码后端列表，必须从这里导入
- 新增后端时同步加到这里，校验会自动通过
"""
import sys
import os
import json
import base64
import time
import uuid
import random
import threading
import urllib.request
import urllib.parse
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import Dict, List, Optional


# ✅ 2026-08-19 P0-2：后端白名单单一事实来源
# 添加新后端：在这里加一个 name，并实现对应的 backend 类
# ✅ 2026-08-20：已移除 "paddleocr"（本地引擎废弃，应用只用云端后端）
VALID_BACKENDS = {
    "baidu",          # 百度智能云 OCR
    "paddleocr_vl",   # 飞桨 PaddleOCR-VL（星河社区）
}

# ✅ P0-2（2026-08-20）：PaddleOCR-VL HTTP 连接复用
#   裸 requests.post/get 每张图都重建 DNS/TCP/TLS 连接；批量识别时浪费明显。
#   模块级懒加载 Session + 连接池，提交/轮询/下载三个请求点共用。
_HTTP_SESSION: Optional["requests.Session"] = None
_HTTP_SESSION_LOCK = threading.Lock()


def _get_http_session() -> "requests.Session":
    """懒加载模块级 Session（线程安全，避免 import 时副作用）。"""
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        with _HTTP_SESSION_LOCK:
            if _HTTP_SESSION is None:
                import requests
                from requests.adapters import HTTPAdapter
                session = requests.Session()
                adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                _HTTP_SESSION = session
    return _HTTP_SESSION


# ✅ M22 清理：原 make_record 零引用（线上路径用 build_result_from_blocks），已删。


class OCRBackend(ABC):
    """OCR 后端抽象基类。"""

    name: str = "base"

    @abstractmethod
    def recognize(self, image_path: str) -> Dict:
        """识别一张图，返回 {"text": str, "lines": list[dict], "columns": list[list[dict]]}。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """后端是否就绪（引擎已加载 / token 已获取）。"""
        raise NotImplementedError

    @property
    def init_error(self) -> Optional[Exception]:
        """初始化失败的错误（None 表示无错）。子类可重写。"""
        return None

    def warmup(self) -> None:
        """可选：预热（让 is_ready 提前变 True，避免首次 OCR 卡顿）。子类可重写。"""
        pass


class BaiduOCRBackend(OCRBackend):
    """百度智能云 OCR 后端。

    支持模型（按需选用）：
    - "ancient"：古籍/繁体专用模型（推荐，竖排支持好；需单独申请权限）
    - "accurate_basic"：高精度版
    - "general_basic"：标准版
    - "table"：表格识别（单独 endpoint）

    文档：https://ai.baidu.com/ai-doc/OCR/
    """

    name = "baidu"

    ACCESS_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
    OCR_URL_TEMPLATE = "https://aip.baidubce.com/rest/2.0/ocr/v1/{model}"
    DEFAULT_MODEL = "accurate_basic"  # 默认高精度版（古籍模型需单独开通）

    def __init__(self, api_key: str, secret_key: str, model: Optional[str] = None, timeout: float = 30.0):
        if not api_key or not secret_key:
            raise ValueError(
                "百度 OCR API Key / Secret Key 未配置。\n"
                "  1. 申请：https://ai.baidu.com/tech/ocr\n"
                "  2. 设置环境变量：BAIDU_OCR_API_KEY + BAIDU_OCR_SECRET_KEY\n"
                "  或在 config.py 中填入"
            )
        self.api_key = api_key
        self.secret_key = secret_key
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout
        # access_token 缓存
        self._token: Optional[str] = None
        self._token_expires: float = 0
        self._init_lock = threading.Lock()
        self._init_done: bool = False
        self._init_error: Optional[Exception] = None

    @property
    def is_ready(self) -> bool:
        return self._init_done and self._token is not None

    @property
    def init_error(self) -> Optional[Exception]:
        return self._init_error

    def warmup(self) -> None:
        """预热：提前获取 access_token，让 dashboard "加载中" banner 提前消失。"""
        try:
            self._get_access_token()
            print(f"[*] 百度 OCR access_token 已就绪（model={self.model}）")
        except Exception as e:
            print(f"  [警告] 百度 OCR 预热失败: {e}")

    def _get_access_token(self) -> str:
        """获取 access_token，缓存到过期前 5 分钟。"""
        with self._init_lock:
            if self._token and time.time() < self._token_expires - 300:
                return self._token
            params = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.secret_key,
            })
            url = f"{self.ACCESS_TOKEN_URL}?{params}"
            req = urllib.request.Request(url, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if "access_token" in data:
                    self._token = data["access_token"]
                    # 百度默认 30 天（2592000 秒），我们提前 5 分钟刷新
                    self._token_expires = time.time() + int(data.get("expires_in", 2592000))
                    self._init_done = True
                    return self._token
                else:
                    err = data.get("error", "?")
                    err_desc = data.get("error_description", "?")
                    raise RuntimeError(f"百度 access_token 失败: {err} - {err_desc}")
            except Exception as e:
                self._init_error = e
                self._init_done = False
                raise

    def recognize(self, image_path: str) -> Dict:
        """调用百度 OCR 识别一张图。

        返回：统一 schema 的 dict：
          {
            "text": "全部文字 \\n 拼接",
            "lines": [{"box": [[x,y]...4 点], "text": str, "confidence": float, "col_index": 0, "row_index": int}],
            "columns": [[lines...]]   # 百度按行返回，单列结构
          }
        """
        import logging
        import time as _t
        # ✅ 2026-08-18：用跟 app.py 同一个 logger（local_chronicles_ocr），否则 handler 不通
        log = logging.getLogger("local_chronicles_ocr")
        token = self._get_access_token()
        with open(image_path, "rb") as f:
            img_data = f.read()
        img_b64 = base64.b64encode(img_data).decode("utf-8")
        url = self.OCR_URL_TEMPLATE.format(model=self.model)
        url = f"{url}?access_token={urllib.parse.quote(token)}"
        body = urllib.parse.urlencode({"image": img_b64}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        t0 = _t.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log.error(f"[百度 OCR] HTTP 调用失败: {e}（model={self.model}）")
            raise RuntimeError(f"百度 OCR HTTP 请求失败: {e}")
        elapsed = _t.time() - t0
        if "error_code" in data:
            log.error(f"[百度 OCR] ❌ 错误 {data['error_code']}: {data.get('error_msg','?')}（model={self.model}, {elapsed:.2f}s）")
            raise RuntimeError(f"百度 OCR 错误 [{data['error_code']}]: {data.get('error_msg','?')}")
        n = len(data.get("words_result", []))
        log.info(f"[百度 OCR] ✓ {n} words（model={self.model}, {elapsed:.2f}s, image={os.path.basename(image_path)}）")
        return self._parse_baidu_result(data, image_path)

    def _parse_baidu_result(self, data: Dict, image_path: str) -> Dict:
        """百度 OCR 结果 → 我们的统一格式。

        百度返回结构（按 model 略有差异，accurate_basic）：
          {
            "words_result": [
              {"words": "文字", "location": {"left": x, "top": y, "width": w, "height": h},
               "probability": {"average": 0.95, "min": 0.9, "var": 0.001}},
              ...
            ],
            "words_result_num": 行数
          }
        """
        words = data.get("words_result", [])
        if not words:
            return {"text": "", "lines": [], "columns": []}
        # ✅ 2026-08-18 调试：若 0 框，记原始响应用排查
        n_no_loc = 0
        sample_keys = None
        lines = []
        text_parts = []
        for i, item in enumerate(words):
            text = item.get("words", "")
            loc = item.get("location", {}) or {}
            x = float(loc.get("left", 0))
            y = float(loc.get("top", 0))
            w = float(loc.get("width", 0))
            h = float(loc.get("height", 0))
            if not loc:
                n_no_loc += 1
                if sample_keys is None:
                    sample_keys = list(item.keys())
            # 4 顶点 polygon（顺时针：左上 → 右上 → 右下 → 左下）
            box = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
            # 置信度
            prob = item.get("probability", {}) or {}
            conf = float(prob.get("average", 0.95)) if prob else 0.95
            lines.append({
                "box": box,
                "text": text,
                "confidence": conf,
                "col_index": 0,  # 百度按行返回，没列分隔
                "row_index": i,
            })
            text_parts.append(text)
        if n_no_loc > 0:
            # 调试输出（不阻断流程）：记到 logs
            import logging
            logging.warning(
                f"[百度 OCR 调试] {len(words)} 个词中 {n_no_loc} 个无 location 字段。"
                f"  words_result[0] 字段: {sample_keys}"
            )
            # 同时打印到 stdout（用户能从 start_err.log 看到）
            print(f"  [百度 OCR 调试] {len(words)} 词中 {n_no_loc} 无 location 字段，"
                  f"words_result[0] keys={sample_keys}, image={image_path}")
        return {
            "text": "\n".join(text_parts),
            "lines": lines,
            "columns": [lines] if lines else [],  # 单列结构
        }


def get_backend(name: str, **kwargs) -> OCRBackend:
    """工厂：根据后端名称返回实例。

    name: "baidu" | "paddleocr_vl"
    kwargs:
      - baidu: api_key, secret_key, model, timeout
      - paddleocr_vl: token, job_url, poll_interval, max_wait, batch_prefetch,
        batch_max, batch_parallel, submit_retry, submit_backoff
    """
    name = (name or "").strip().lower()
    if name in ("", "baidu", "baidu_ocr", "百度"):
        return BaiduOCRBackend(
            api_key=kwargs.get("api_key", ""),
            secret_key=kwargs.get("secret_key", ""),
            model=kwargs.get("model"),
            timeout=kwargs.get("timeout", 30.0),
        )
    if name in ("paddleocr_vl", "paddleocr-vl", "paddleocr_app", "aistudio"):
        # ✅ 2026-09-14 P-OPT-6：显式传了就透传；没传则交给 `__init__` 的 `_UNSET`
        #    哨兵去 config.py 取 —— **默认值单点**，这里不再写死 5.0 / 600.0。
        return PaddleOCRVLBackend(
            token=kwargs.get("token", ""),
            job_url=kwargs.get("job_url", PaddleOCRVLBackend.JOB_URL),
            model=kwargs.get("model", PaddleOCRVLBackend.MODEL),
            poll_interval=kwargs.get("poll_interval", _UNSET),
            max_wait=kwargs.get("max_wait", _UNSET),
            batch_prefetch=kwargs.get("batch_prefetch", _UNSET),
            batch_max=kwargs.get("batch_max", _UNSET),
            batch_parallel=kwargs.get("batch_parallel", _UNSET),
            submit_retry=kwargs.get("submit_retry", _UNSET),
            submit_backoff=kwargs.get("submit_backoff", _UNSET),
        )
    raise ValueError(f"未知 OCR 后端: {name!r}（可选: 'baidu', 'paddleocr_vl'）")


def _mask_token(token: str) -> str:
    """Token 脱敏（写 log / 错误信息时用，避免 token 泄露）。"""
    if not token or len(token) < 8:
        return "***"
    return token[:4] + "***" + token[-4:]


# ---------- P0（2026-09-10）：块内几何重建（像素投影切列） ----------
#
# 背景：PaddleOCR-VL 只返回段级 block_bbox（一个块一个框 + 整段文字）。旧实现
# 把块高按 "\n" 段数均分来"造"行框——对横排块正确，但竖排密集版面的块内含多
# 个竖排列，均分后得到的是横跨整块的条带（实测官报 0001：17 个"行"的 x 范围
# 全部是 [795,1578]，实为 17 段文字被横切）。后果：标注页"OCR 参考框"在这类
# 页面点不中，人工只能逐一拖框。
#
# 修法：块内多列时改用像素垂直投影切列（列是物理事实，与语言/字体无关），再按
# "段文本字符数 → 列内 y 累积"定位。金标准实测：列切分正确率 181/189 = 95.8%
# （@0.7 严格阈值下不变），几何完全失效时为 0%。设计见
# 《半自动标注通用方案_20260910.md》§2 G 层。

def _load_gray(image):
    """图像源 → 灰度 ndarray；不支持/失败 → None（调用方退回旧行为）。"""
    if image is None:
        return None
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None
    try:
        if isinstance(image, np.ndarray):
            return image if image.ndim == 2 else None
        if isinstance(image, Image.Image):
            return np.array(image.convert("L"))
        if isinstance(image, (str, os.PathLike)):
            with Image.open(image) as im:
                return np.array(im.convert("L"))
    except Exception:
        return None
    return None


def _project_bands(gray, bbox, ink_ratio: float = 0.06, min_w: int = 12,
                   expand: bool = True) -> list:
    """像素垂直投影切列带。

    - 先剔除"贯通横线"行（墨迹占比 > 0.8），否则表格线会让所有列连成一片；
    - 按投影谷（墨迹 < 峰值 × ink_ratio）切分，带宽 < min_w 的碎片丢弃；
    - expand：列带外延到相邻间隙中点，得到列的真实宽度。墨迹密集区比真列
      窄约 40%（官报 0001：36.8px vs 真实 65px），不外延会系统性低估框宽。
    """
    import numpy as np
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    x1 = max(0, x1); y1 = max(0, y1)
    x2 = min(gray.shape[1], x2); y2 = min(gray.shape[0], y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return []
    dark = gray[y1:y2, x1:x2] < 128
    if not dark.any():
        return []
    row_ink = dark.sum(axis=1) / max(1, dark.shape[1])
    keep = row_ink < 0.8
    if keep.sum() < 4:
        keep = np.ones_like(keep)
    prof = dark[keep].sum(axis=0).astype(float)
    if prof.max() <= 0:
        return []
    thr = prof.max() * ink_ratio
    bands = []
    run = None
    for i, v in enumerate(prof):
        if v > thr:
            if run is None:
                run = i
        else:
            if run is not None:
                if i - run >= min_w:
                    bands.append((x1 + run, x1 + i))
                run = None
    if run is not None and len(prof) - run >= min_w:
        bands.append((x1 + run, x1 + len(prof)))
    if expand and len(bands) >= 2:
        out = []
        for i, (a, b) in enumerate(bands):
            w = b - a
            if i == 0:
                left = a - min(w, max(0, a - x1))
            else:
                left = (bands[i - 1][1] + a) / 2.0
            if i == len(bands) - 1:
                right = b + min(w, max(0, x2 - b))
            else:
                right = (b + bands[i + 1][0]) / 2.0
            out.append((left, right))
        bands = out
    return bands


def _band_ink_y(gray, band, bbox):
    """列带内的墨迹 y 范围 → (top, bot)；无墨迹 → None。"""
    import numpy as np
    bx1 = max(0, int(band[0])); bx2 = min(gray.shape[1], int(band[1]))
    by1 = max(0, int(bbox[1])); by2 = min(gray.shape[0], int(bbox[3]))
    if bx2 <= bx1 or by2 <= by1:
        return None
    sub = gray[by1:by2, bx1:bx2] < 128
    rows = np.where(sub.any(axis=1))[0]
    if len(rows) == 0:
        return None
    return (by1 + int(rows[0]), by1 + int(rows[-1]) + 1)


def _rebuild_block_entries(segs: list, bbox, gray, rtl: bool = True):
    """退化块（块内多列）→ 列级行框列表；无法安全重建 → None。

    - 列序 = 阅读序（竖排右→左）；
    - 列容量 cap = 列内墨迹高 / 字高（字高 ≈ 列宽 × 0.92）；
    - **每列至少承载一段**（placed == 0 不退出）——否则容量低估会把某段推到相邻
      列，造成语义错位（实测官报 0002「總號」曾被排到下一列之后）；
    - 容量仍不足的剩余段追加到最后一列（**绝不丢字**，也就不必整块退回）。
    """
    bands = _project_bands(gray, bbox)
    if len(bands) < 2:
        return None
    order = sorted(bands, key=lambda b: b[0], reverse=rtl)
    out = []
    si = 0
    for band in order:
        if si >= len(segs):
            break
        cell = max(8.0, (band[1] - band[0]) * 0.92)
        iy = _band_ink_y(gray, band, bbox)
        if iy is None:
            continue
        top, bot = iy
        cap = max(1, int(round((bot - top) / cell)))
        used = 0
        placed = 0
        while si < len(segs) and (used < cap or placed == 0):
            seg = segs[si]
            if not seg:
                si += 1
                continue
            y_a = top + used * cell
            y_b = min(bot, y_a + len(seg) * cell)
            out.append({
                "box": [[band[0], y_a], [band[1], y_a], [band[1], y_b], [band[0], y_b]],
                "text": seg,
                "confidence": None,
                "geometry": "projection",   # 标记来源，便于回溯/统计
            })
            used += len(seg)
            si += 1
            placed += 1
    if si < len(segs) and order:
        last = order[-1]
        cell = max(8.0, (last[1] - last[0]) * 0.92)
        top, bot = _band_ink_y(gray, last, bbox) or (bbox[1], bbox[3])
        k = 0
        for seg in segs[si:]:
            if not seg:
                continue
            y_a = min(bot, top + k * cell)
            y_b = min(bot, y_a + len(seg) * cell)
            out.append({
                "box": [[last[0], y_a], [last[1], y_a], [last[1], y_b], [last[0], y_b]],
                "text": seg,
                "confidence": None,
                "geometry": "projection",
            })
            k += 1
    return out or None


# ---------- P0b（2026-09-10）：HTML 表格块 → 竖排流布局 ----------
#
# 背景：PaddleOCR-VL 对"竖排多列"版面（报纸公告栏）返回的是 HTML <table>，
# 而不是纯文本——tr = 同一水平带的块，rowspan = 块跨越多个行带。旧实现只做
# `text.split("\n")`，对这类块只能拿到 1 段 → **整页塌成 1 行**（官报 0003 是
# 402 字 1 行；0002_0001 是 2108 字 2 行），标注页参考框完全不可用。
#
# 由 0003 的 64 条人工金标准 + 像素实证归纳出的版面模型：
#   1. 页面有横向框线（上框 / 中框 / 下框），把纵向切成「标题区 + 正文区」
#   2. 列网格贯穿上下——标题区的公司名与正文区的列共用同一 x 网格
#   3. 阅读流在正文区内按列流动（竖排右→左，列内自上而下，到列底换左列顶）
#   4. 标题（公司名）是列的"上部文字"，位于正文区之外，独立成框
#
# 实测（0003，64 条金标准）：列切分正确率 64/64 = 100%、文本零丢失、
# 值中心 y 落框率 93.8%。设计见《半自动标注通用方案_20260910.md》§6 P0b。

def _dilate_rect(mask, kh: int = 1, kw: int = 1):
    """矩形结构元膨胀（numpy 实现，避免引入 cv2 依赖）。"""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    out = mask
    if kh > 1:
        pad = (kh - 1) // 2
        p = np.pad(out, ((pad, pad), (0, 0)))
        out = sliding_window_view(p, kh, axis=0).any(axis=-1)
    if kw > 1:
        pad = (kw - 1) // 2
        p = np.pad(out, ((0, 0), (pad, pad)))
        out = sliding_window_view(p, kw, axis=1).any(axis=-1)
    return out


class _TableHTMLParser(HTMLParser):
    """<table> → [[cell, ...], ...]；cell = {text, rowspan, colspan}。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td":
            def _int(v, d=1):
                try:
                    return max(1, int(v))
                except Exception:
                    return d
            self._cell = {"text": "", "rowspan": _int(a.get("rowspan")),
                          "colspan": _int(a.get("colspan"))}
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += "\n"

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._cell["text"] = self._cell["text"].strip()
            if self._row is None:
                self._row = []
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def parse_html_table(text: str):
    """块文本 → 非空单元格列表（文档序）；非表格 / 解析失败 → None。"""
    if not text or "<t" not in text.lower():
        return None
    p = _TableHTMLParser()
    try:
        p.feed(text)
    except Exception:
        return None
    cells = []
    for r, row in enumerate(p.rows):
        for c, cell in enumerate(row):
            if cell["text"]:
                cells.append({"row": r, "col": c, "text": cell["text"],
                              "rowspan": cell["rowspan"],
                              "colspan": cell["colspan"]})
    return cells or None


def _html_page_regions(gray, bbox, k: int = 25, hline: float = 0.7):
    """检测「标题区 / 正文区」→ dict | None。

    正文区判据：滑动窗口内墨迹行像素数远高于标题区（标题区只有几个公司名）。
    横线行（墨迹占满整宽）不参与判定。
    """
    import numpy as np
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    blk = (gray[y1:y2, x1:x2] < 128)
    H, W = blk.shape
    if H < 4 * k or W < 8:
        return None
    rowcnt = blk.sum(axis=1).astype(float)
    hlines = np.where(rowcnt / max(1, W) > hline)[0]
    rowcnt[hlines] = 0.0
    smooth = np.convolve(rowcnt, np.ones(k) / k, mode="same")
    if smooth.max() <= 0:
        return None
    body = smooth > smooth.max() * 0.35
    best = (0, 0, 0)
    i = 0
    while i < H:
        if body[i]:
            j = i
            while j < H and body[j]:
                j += 1
            if j - i > best[0]:
                best = (j - i, i, j)
            i = j
        else:
            i += 1
    if best[0] < 20:
        return None
    return {"body_top": y1 + best[1], "body_bot": y1 + best[2],
            "title": (y1, y1 + best[1]), "hlines": [y1 + int(v) for v in hlines]}


def _html_band_ink_y(gray, band, ylo, yhi, hline: float = 0.85, pad_ratio: float = 1.0):
    """列带内墨迹 y 范围（剔贯通横线行）→ (top, bot) | None。

    "横线行"判据必须**相对列带之外**取：横线的定义是墨迹延续到本带之外，而不是
    本带内占有率超标。早期实现用 `带内 ink/带宽 > 0.8`，在窄带（带内几乎只有本列
    的字）下会把**每一个字行**都判成横线 → 整带无墨迹 → 返回 None → 调用方退化成
    整幅高的框。改为：把带左右各外扩一个带宽，若外扩带内该行仍近乎满墨，才算横线。
    实测（官报 4 页）本改动与旧口径结果完全一致（0003 金标准 x 落列 64/64、
    值中心 y 落框 60/64 不变），仅消除了窄带下的退化路径。
    """
    import numpy as np
    bx1 = max(0, int(band[0])); bx2 = min(gray.shape[1], int(band[1]))
    by1 = max(0, int(ylo)); by2 = min(gray.shape[0], int(yhi))
    if bx2 - bx1 < 3 or by2 - by1 < 3:
        return None
    sub = gray[by1:by2, bx1:bx2] < 128
    pad = int((bx2 - bx1) * pad_ratio)
    ex1 = max(0, bx1 - pad); ex2 = min(gray.shape[1], bx2 + pad)
    ext = gray[by1:by2, ex1:ex2] < 128
    keep = ext.sum(axis=1) / max(1, ext.shape[1]) < hline
    if keep.sum() < 3:
        keep = np.ones_like(keep)
    idx = np.where(keep)[0]
    rows = np.where(sub[keep].any(axis=1))[0]
    if len(rows) == 0:
        return None
    return (by1 + int(idx[rows[0]]), by1 + int(idx[rows[-1]]) + 1)


def _html_title_blocks(gray, title_region, bbox, min_w: int = 8, gap: int = 14):
    """标题区内的墨迹 x 块（右→左）→ [(x1,y1,x2,y2)]。"""
    import numpy as np
    y_lo, y_hi = title_region
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    y_lo = max(y1, int(y_lo)); y_hi = min(y2, int(y_hi))
    if y_hi - y_lo < 12:
        return []
    sub = (gray[y_lo:y_hi, x1:x2] < 128)
    keep = sub.sum(axis=1) / max(1, sub.shape[1]) < 0.6
    if keep.sum() < 4:
        return []
    body = sub[keep]
    colink = body.sum(axis=0) / max(1, body.shape[0])
    prof = body.sum(axis=0).astype(float).copy()
    prof[colink > 0.85] = 0.0          # 剔贯通竖线（页面边框会被误判成标题块）
    if prof.max() <= 0:
        return []
    thr = prof.max() * 0.05
    raw, run = [], None
    for i, v in enumerate(prof):
        if v > thr:
            if run is None:
                run = i
        else:
            if run is not None:
                raw.append((run, i))
                run = None
    if run is not None:
        raw.append((run, len(prof)))
    merged = []
    for a, b in raw:
        if merged and a - merged[-1][1] < gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    idx = np.where(keep)[0]
    out = []
    for a, b in merged:
        if b - a < min_w:
            continue
        ys = np.where(body[:, a:b].any(axis=1))[0]
        if len(ys) == 0:
            continue
        out.append((x1 + a, x1 + b, y_lo + int(idx[ys[0]]),
                    y_lo + int(idx[ys[-1]]) + 1))
    # 剔页面边框（块贴住 bbox 左右边界 → 是框线，不是标题文字）
    edge = 20
    out = [t for t in out if (t[0] - x1) > edge and (x2 - t[1]) > edge]
    out.sort(key=lambda t: -t[0])
    return out


def _html_body_columns(gray, bbox, body_top, body_bot, ratio: float = 0.25,
                       min_w: int = 18, merge_gap: int = 6):
    """正文区切列 → [(wide_x1, wide_x2, narrow_x1, narrow_x2, yt, yb)]（右→左）。

    竖排切列的关键：先 3×3 膨胀修复笔画断裂，再**沿 y 膨胀**让同列的字连成竖条，
    此时 x 投影的峰谷才分明（纯投影因字错位 + 扫描歪斜，谷只有峰值 5%，阈值法
    会把 3 列并成 1）。阈值取峰值 25%，剔宽度 < 18px 的碎片。
    narrow 是墨迹带（宽 ≈ 字宽），wide 外延到相邻间隙中点（≈ 列宽）——**测墨迹
    必须用 narrow**，用 wide 会吞进邻列的字，使每列墨迹高都变成整幅高。
    """
    import numpy as np
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    yt, yb = int(body_top), int(body_bot)
    sub = (gray[yt:yb, x1:x2] < 128).astype(np.uint8)
    H, W = sub.shape
    if H < 20 or W < 20:
        return []
    sub[sub.sum(axis=1) / max(1, W) > 0.7] = 0            # 剔贯通横线
    sub[:, sub.sum(axis=0) / max(1, H) > 0.85] = 0        # 剔贯通竖线（页面边框）
    b = _dilate_rect(sub.astype(bool), 3, 3)
    b = _dilate_rect(b, 17, 1)
    prof = b.sum(axis=0).astype(float)
    if prof.max() <= 0:
        return []
    thr = prof.max() * ratio
    raw, run = [], None
    for i, v in enumerate(prof):
        if v > thr:
            if run is None:
                run = i
        else:
            if run is not None:
                if i - run >= min_w:
                    raw.append((run, i))
                run = None
    if run is not None and len(prof) - run >= min_w:
        raw.append((run, len(prof)))
    merged = []
    for a, b2 in raw:
        if merged and a - merged[-1][1] < merge_gap:
            merged[-1] = (merged[-1][0], b2)
        else:
            merged.append((a, b2))
    if not merged:
        return []
    out = []
    for i, (a, b2) in enumerate(merged):
        left = a - min(b2 - a, a) if i == 0 else (merged[i - 1][1] + a) / 2.0
        right = b2 + min(b2 - a, W - b2) if i == len(merged) - 1 \
            else (b2 + merged[i + 1][0]) / 2.0
        out.append((x1 + left, x1 + right, x1 + a, x1 + b2, yt, yb))
    out.sort(key=lambda t: -t[0])
    return out


def _html_pick_titles(cells, tblocks):
    """挑出与标题块一一对应的格：使「块高 / 文本长度」离散度最小。

    标题是同一字号的方块大字 → 字高（块高/字数）应一致；正文长段会给出极小的
    伪字高而被方差筛掉（并设字高物理下限 32px）。n 取 min(标题块数, 格数-1)，
    保证至少留一段作正文流。
    """
    import itertools
    n_t = min(len(tblocks), len(cells) - 1)
    if n_t <= 0:
        return []
    hs = [tb[3] - tb[2] for tb in tblocks[:n_t]]
    best = None
    for combo in itertools.combinations(range(len(cells)), n_t):
        ls = [len(cells[i]["text"]) for i in combo]
        if any(v <= 0 for v in ls):
            continue
        ratios = [h / v for h, v in zip(hs, ls)]
        mean = sum(ratios) / len(ratios)
        if not (32.0 < mean < 220.0):
            continue
        cv = (sum((r - mean) ** 2 for r in ratios) / len(ratios)) ** 0.5 / mean
        if best is None or cv < best[0]:
            best = (cv, combo)
    return list(best[1]) if best else []


def _fit_cell(heights, n_chars, lo: float = 12.0, hi: float = 160.0):
    """求字格高，使 sum(round(h/cell)) 最接近 n_chars（总字数守恒）。"""
    if n_chars <= 0 or not heights:
        return 38.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        s = sum(max(1, int(round(h / mid))) for h in heights)
        if s > n_chars:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _layout_html_block(gray, bbox, cells):
    """HTML 表格块 → entries（按阅读序：列自右向左，列标题紧邻其列正文）。

    失败 → None（调用方退回旧行为）。
    """
    reg = _html_page_regions(gray, bbox)
    if not reg:
        return None
    cols = _html_body_columns(gray, bbox, reg["body_top"], reg["body_bot"])
    tblocks = _html_title_blocks(gray, reg["title"], bbox)
    idx = _html_pick_titles(cells, tblocks)
    tblocks = tblocks[:len(idx)]

    # 标题块按 x 就近挂到某一列（标题与正文共用列网格）
    def _emit(tb, text, geom):
        return {
            "box": [[tb[0], tb[2]], [tb[1], tb[2]], [tb[1], tb[3]], [tb[0], tb[3]]],
            "text": text,
            "confidence": None,
            "geometry": geom,
        }

    title_of = {}          # 列下标 → 标题 entry
    orphan = []
    for tb, ci in zip(tblocks, idx):
        ent = _emit(tb, cells[ci]["text"], "html-title")
        if not cols:
            orphan.append(ent)
            continue
        cx = (tb[0] + tb[1]) / 2.0
        k = min(range(len(cols)),
                key=lambda i: 0 if cols[i][0] <= cx <= cols[i][1]
                else min(abs(cx - cols[i][0]), abs(cx - cols[i][1])))
        title_of.setdefault(k, ent)

    entries = list(orphan)
    skip = set(idx)
    stream = "".join(c["text"] for k, c in enumerate(cells) if k not in skip)
    if not stream or not cols:
        return entries or None

    ink = [_html_band_ink_y(gray, (c[2], c[3]), c[4], c[5]) for c in cols]
    heights = [((iy[1] - iy[0]) if iy else (c[5] - c[4]))
               for iy, c in zip(ink, cols)]
    cell = _fit_cell(heights, len(stream))
    pos = 0
    last = None                    # (entry, cell, top, bot, 已装字数)
    for k, ((wx1, wx2, _nx1, _nx2, cy1, cy2), iy) in enumerate(zip(cols, ink)):
        if k in title_of:
            entries.append(title_of[k])
        if pos >= len(stream):
            continue
        top, bot = iy if iy else (cy1, cy2)
        cap = max(1, int(round((bot - top) / cell)))
        take = stream[pos:pos + cap]
        if not take:
            continue
        yb2 = min(bot, top + len(take) * cell)
        entries.append({
            "box": [[wx1, top], [wx2, top], [wx2, yb2], [wx1, yb2]],
            "text": take,
            "confidence": None,
            "geometry": "html-column",
        })
        last = (len(entries) - 1, wx1, wx2, top, bot)
        pos += len(take)
    # 字高拟合的舍入误差可能使末列少装若干字 → 余量并入末列（**绝不丢字**）
    if pos < len(stream) and last is not None:
        li, lx1, lx2, ltop, lbot = last
        e = entries[li]
        e["text"] += stream[pos:]
        yb2 = min(lbot, ltop + len(e["text"]) * cell)
        e["box"] = [[lx1, ltop], [lx2, ltop], [lx2, yb2], [lx1, yb2]]
    return entries or None


# ---------------------------------------------------------------------------
# P1（2026-09-10）块层留痕 + 装配（assemble_result）
#
# 背景：P0/P0b 的几何修正在内存里完成，**块级 box 与段切分未落盘**——离开本次
# 运行就无法复算框（structured 只存 7 字段白名单的 L2_lines）。P1 把块层显式
# 产出（`build_result_from_blocks` 返回 `blocks` + `block_order`），由
# `structured_writer` 落成 `L1_blocks`；下游凭 L1_blocks 调 `assemble_result`
# 即可**不依赖图像与 OCR 重跑**复算出 lines/columns。
#
# 装配（列聚类 + 输出顺序）必须**只有一份实现**，否则复算与在线结果会漂移。
# ---------------------------------------------------------------------------

ENTRY_ORDER = "entry"       # 输出保持 entry（段）顺序 —— 几何已修，顺序不动
COLUMN_ORDER = "column"     # 输出按列序重排 —— 修正 PaddleOCR 的自然阅读序


def _block_record(block_id, bbox, text, split_rule, entries) -> dict:
    """块层记录：块框 + 原始块文本 + 切分规则 + 段切分结果（P1 落盘用）。"""
    x1, y1, x2, y2 = [float(v) for v in bbox]
    return {
        "block_id": block_id,
        "box": [x1, y1, x2, y2],
        "text": text,
        "split_rule": split_rule,
        "entries": entries,      # 与 block_entries 同一批对象（whitelist 在落盘侧）
    }


def assemble_result(block_entries: list, text_parts: list,
                    entry_order: bool, log=None) -> dict:
    """第 2 步：按 x_center 聚类重建列 + 决定输出顺序（**唯一装配实现**）。

    entry_order=True  → 输出保持 entry 顺序（页内含 P0a/P0b 重建块时）
    entry_order=False → 按列序重排（P-OPT-1 旧行为，修正 PaddleOCR 阅读序）

    在线路径（`build_result_from_blocks`）与离线复算
    （`structured_writer.rebuild_lines_from_blocks`）都走这里。
    """
    if not block_entries:
        return {"text": "", "lines": [], "columns": []}

    columns = None
    try:
        from column_detector import find_columns_from_ocr_boxes
        columns = find_columns_from_ocr_boxes(block_entries)
    except Exception as e:
        if log is not None:
            log.warning(f"[PaddleOCR-VL] 列聚类失败（回退单列）: {e}")

    if not columns:
        # 聚类失败 / cv2 缺失 → 单列结构（与旧行为一致，至少不丢数据）
        all_lines = []
        for i, entry in enumerate(block_entries):
            entry["col_index"] = 0
            entry["row_index"] = i
            all_lines.append(entry)
        return {
            "text": "\n".join(text_parts),
            "lines": all_lines,
            "columns": [all_lines],
        }

    # 列结构成立：先按聚类结果给每行赋 col_index（单一来源）
    col_of = {}
    for col_idx, col in enumerate(columns):
        for line in col:
            col_of[id(line)] = col_idx

    if entry_order:
        # 页内含"块内几何重建 / HTML 竖排流布局"的块时，输出顺序保持 entry 本身
        # 的顺序（几何已修，顺序不动）。
        # · P0a：退化块在旧行为下各行 x 全同 → 聚成 1 列 → 列内按 y 排序恰好
        #   等于段序，故保持段序 = 与旧行为文本顺序**完全一致**（零回归）；而按
        #   列序重排会在列分配偶有偏差时拆开语义相邻的段（实测官报 0002
        #   「總號」被排到下一列之后）。
        # · P0b：_layout_html_block 已按阅读序（列自右向左、标题紧邻其列）产出。
        all_lines = []
        counters = {}
        for e in block_entries:
            ci = col_of.get(id(e), 0)
            r = counters.get(ci, 0)
            counters[ci] = r + 1
            line_out = dict(e)
            line_out["col_index"] = ci
            line_out["row_index"] = r
            all_lines.append(line_out)
        return {
            "text": "\n".join(l["text"] for l in all_lines),
            "lines": all_lines,
            "columns": columns,
        }

    # 无重建：保持 P-OPT-1 行为 —— 按列序重排
    all_lines = []
    ordered_texts = []
    for col_idx, col in enumerate(columns):
        for row_idx, line in enumerate(col):
            line_out = dict(line)
            line_out["col_index"] = col_idx
            line_out["row_index"] = row_idx
            all_lines.append(line_out)
            ordered_texts.append(line_out["text"])
    return {
        "text": "\n".join(ordered_texts),
        "lines": all_lines,
        "columns": columns,
    }


def build_result_from_blocks(blocks: list, log, image=None) -> dict:
    """P-OPT-1 block 模式解析：块级 entry + 列聚类重建。

    输入: blocks = [(block_content: str, block_bbox: [x1,y1,x2,y2]), ...]
    输出: {"text": str, "lines": [...], "columns": [...],
           "blocks": [块层记录], "block_order": "entry"|"column"}

    P0（2026-09-10）新增 image 参数：可选图像源（路径 / PIL.Image / 灰度 ndarray）。
      提供时，块内多列（投影列带 ≥ 2）且段数 ≥ 2 的退化块改走像素投影切列
      重建几何（见 _rebuild_block_entries）；未提供、重建失败、或块非退化
      → 完全退回旧行为（均分高度），保证向后兼容与"绝不丢字"。
    P0b（2026-09-10）新增 HTML 表格块路径：块文本含 <table>/<tr>/<td> 时改走
      竖排流布局（见 _layout_html_block），否则旧行为只能拿到 1 段 → 整页塌成
      1 行。失败仍退回旧行为。
    P1（2026-09-10）新增 `blocks` / `block_order` 两个返回键（块层留痕）：
      记录每块的原始文本、块框、split_rule（single / even-split /
      projection-columns / html-table）与段切分结果，供 structured_writer 落成
      `L1_blocks`；下游凭该层 + `assemble_result` 可离线复算框。
    """
    # ---- 第 1 步：块级 entry ----
    gray = _load_gray(image)
    block_entries = []
    text_parts = []
    block_records = []          # ✅ P1：块层留痕（block box + 段切分 + split_rule）
    rebuilt_blocks = 0
    html_blocks = 0
    for text, (x1, y1, x2, y2) in blocks:
        if gray is not None:
            cells = parse_html_table(text)
            if cells:
                entries = _layout_html_block(gray, (x1, y1, x2, y2), cells)
                if entries:
                    block_entries.extend(entries)
                    text_parts.extend(e["text"] for e in entries)
                    block_records.append(_block_record(
                        len(block_records), (x1, y1, x2, y2), text,
                        "html-table", entries))
                    html_blocks += 1
                    continue
        sub_texts = [t.strip() for t in text.split("\n") if t.strip()]
        if not sub_texts:
            continue
        if gray is not None and len(sub_texts) >= 2:
            entries = _rebuild_block_entries(sub_texts, (x1, y1, x2, y2), gray)
            if entries:
                block_entries.extend(entries)
                text_parts.extend(e["text"] for e in entries)
                block_records.append(_block_record(
                    len(block_records), (x1, y1, x2, y2), text,
                    "projection-columns", entries))
                rebuilt_blocks += 1
                continue
        # 旧行为：块内按段数均分高度（横排块正确；竖排多列块退化为横条带）
        n = len(sub_texts)
        sub_h = (y2 - y1) / n
        new_entries = []
        for j, sub in enumerate(sub_texts):
            sy1 = y1 + j * sub_h
            sy2 = y1 + (j + 1) * sub_h
            new_entries.append({
                "box": [[x1, sy1], [x2, sy1], [x2, sy2], [x1, sy2]],
                "text": sub,           # 完整行文本，不再拆字
                "confidence": None,    # VL 无行级置信度
            })
            text_parts.append(sub)
        block_entries.extend(new_entries)
        block_records.append(_block_record(
            len(block_records), (x1, y1, x2, y2), text,
            "single" if n == 1 else "even-split", new_entries))
    if html_blocks:
        log.info(f"[PaddleOCR-VL] HTML 表格块竖排流布局: {html_blocks} 个块")
    if rebuilt_blocks:
        log.info(f"[PaddleOCR-VL] 块内几何重建: {rebuilt_blocks} 个多列块改用投影切列")

    res = assemble_result(block_entries, text_parts,
                          bool(rebuilt_blocks or html_blocks), log)
    # ✅ P1：块层随结果返回（供 structured_writer 落盘为 L1_blocks）
    res["blocks"] = block_records
    res["block_order"] = ENTRY_ORDER if (rebuilt_blocks or html_blocks) else COLUMN_ORDER
    return res



# ══════════════════════════════════════════════════════════════
# ✅ 2026-09-14 P-OPT-6：批量提交（预取）
# ══════════════════════════════════════════════════════════════
# 动机（实测，见 `2026-09-14.md` §Q1/§Q2）：旧路径「一图一 job、串行等」
#   300 页 ≈ 3.3 小时，其中服务端识别 23.5 s/页 —— 是 **Σ 而不是 max**，
#   因为任务根本没被并行提交。官方 API 本就支持批量语义：
#     * `batchId` **客户端自定义**（「用户自定义传入，形式自订」）
#     * 同 batchId 最多 100 条 job（超限错误码 10009）
#     * 提交队列满 / 频率过高 = 10010 / 12002(429)
#   ⚠ `file` / `fileUrl` 二选一 ⇒ **一个请求仍只能一个文件**。
#     所以「批量」= N 次提交 + 统一收结果，收益在「让服务端并行」这一句上。
#
# 改法（**不替换 recognize，而是给它加一个前置**）：
#   `prefetch(paths)` 在后台线程并发提交 + 并发轮询；逐张的 `recognize()`
#   命中在途条目时**等结果**而不是重复提交 ⇒ 总耗时 ΣBᵢ → max(Bᵢ)。
#   预取完全失败时 `recognize()` 原样走逐张路径 —— **零感知退化**。
_UNSET = object()


class _PrefetchEntry:
    """一张图的预取在途状态（提交后由后台线程填充）。

    这是「批量」生效的关键：`recognize()` 命中它时**等待**而不是重复提交 ——
    所有 job 早已在服务端并行跑，逐张处理只是按顺序收结果。
    """

    __slots__ = ("path", "job_id", "parsed", "error", "event")

    def __init__(self, path: str):
        self.path = path
        self.job_id: str = ""
        self.parsed: Optional[dict] = None
        self.error: str = ""
        self.event = threading.Event()


class PaddleOCRVLBackend(OCRBackend):
    """PaddleOCR-VL 星河社区 API（每日 3000-20000 页免费额度）。

    流程（异步）：
    1. multipart POST 提交任务（含图片 + model），拿 jobId
    2. 轮询 GET /{jobId} 看 state（pending → running → done/failed）
    3. done 时拿 resultUrl.jsonUrl 下载 JSONL
    4. 解析 JSONL：每行一个 result，含 layoutParsingResults[].prunedResult.parsing_res_list

    关键差异（vs 百度 OCR）：
    - 百度 OCR：字级 location（每个字一个 box）
    - PaddleOCR-VL：段级 block_bbox（每段一个 box + 整段文字）
    - 对竖排县志：每段 ≈ 一列，content 1-6 字
    - ✅ P-OPT-1（2026-08-19）：默认 block 模式保留块级文本并重建列结构；
      旧的"按字数均分 bbox 宽度拆字"保留为 char 回退模式
      （配置 ocr_backend_config.json 的 "vl_parse_mode"）

    鉴权：Authorization: bearer {TOKEN}
    """
    name = "paddleocr_vl"
    # 星河社区 PaddleOCR-VL API 端点（每个用户专属子域名；用户从 aistudio 申请拿到）
    JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    MODEL = "PaddleOCR-VL-1.6"

    def __init__(
        self,
        token: str,
        job_url: str = None,
        model: str = None,
        poll_interval: float = _UNSET,
        max_wait: float = _UNSET,
        batch_prefetch: int = _UNSET,
        batch_max: int = _UNSET,
        batch_parallel: int = _UNSET,
        submit_retry: int = _UNSET,
        submit_backoff: float = _UNSET,
        cache_ttl: float = _UNSET,
        cache_max: int = _UNSET,
    ):
        if not token:
            raise ValueError("PaddleOCR-VL token 必填（去 aistudio.baidu.com/paddleocr/task 申请）")
        # ✅ 2026-09-14 P-OPT-6：调参默认值**单点在 config.py**（此处不写死数字），
        #    显式传参仍可覆盖（测试 / 标定用）。用 `_UNSET` 哨兵而非数字默认，
        #    是为了避免「config 改一处、这里还得再改一处」的经典漂移。
        from config import (
            OCR_POLL_INTERVAL, OCR_MAX_WAIT, OCR_BATCH_PREFETCH, OCR_BATCH_MAX,
            OCR_BATCH_PARALLEL, OCR_SUBMIT_RETRY, OCR_SUBMIT_BACKOFF,
        )
        self.token = token
        self.job_url = job_url or self.JOB_URL
        self.model = model or self.MODEL
        self.poll_interval = float(OCR_POLL_INTERVAL if poll_interval is _UNSET else poll_interval)
        self.max_wait = float(OCR_MAX_WAIT if max_wait is _UNSET else max_wait)
        # 批量预取：0 = 关闭（默认）。>0 = 单次预取张数上限（官方同 batchId ≤100）
        self.batch_prefetch = int(OCR_BATCH_PREFETCH if batch_prefetch is _UNSET else batch_prefetch)
        self.batch_max = max(1, int(OCR_BATCH_MAX if batch_max is _UNSET else batch_max))
        # ✅ P-OPT-7：预取并发度 + 提交重试（标定实测 429 真的会发生）
        self.batch_parallel = max(
            1, int(OCR_BATCH_PARALLEL if batch_parallel is _UNSET else batch_parallel))
        self.submit_retry = max(
            0, int(OCR_SUBMIT_RETRY if submit_retry is _UNSET else submit_retry))
        self.submit_backoff = max(
            0.0, float(OCR_SUBMIT_BACKOFF if submit_backoff is _UNSET else submit_backoff))
        # 结果缓存：预取产出的落点（TTL 秒；0 = 不缓存，等于关闭预取的收益）
        self.cache_ttl = float(1800.0 if cache_ttl is _UNSET else cache_ttl)
        self.cache_max = max(16, int(512 if cache_max is _UNSET else cache_max))
        self._cache: Dict[tuple, tuple] = {}                 # key -> (expire_ts, parsed)
        self._pending: Dict[tuple, _PrefetchEntry] = {}      # key -> 在途条目
        self._cache_lock = threading.Lock()
        # 预取提交节流（秒）：官方有提交队列上限（10010）与频率限制（12002），
        # 并发提交时用小间隔摊平，避免整批被拒。
        self.submit_interval = 0.25
        self._token_mask = _mask_token(token)

    @property
    def is_ready(self) -> bool:
        return True  # 不需要预热（无 engine load）

    @property
    def init_error(self):
        return None

    def warmup(self) -> None:
        # 不做实际预热（异步 API 每次才建连）；只 log 一下
        import logging
        log = logging.getLogger("local_chronicles_ocr")
        log.info(f"[*] PaddleOCR-VL backend 就绪（model={self.model}, job_url={self.job_url}, token={self._token_mask}）")

    # ✅ P-OPT-7（2026-09-14 标定实测）：**频率类**错误 → 退避重试。
    #   证据：零节流并发提交 6 张，1 张被拒，返回
    #   `HTTP 429 / {"code": 12002, "msg": "请求频率过高，请稍后重试"}`。
    #   官方错误码表另有 10010（提交队列已满）。这两类都是**暂时性**的 ——
    #   不重试就会让那一张退回逐张路径，在批量场景下等于把并行的收益退回去。
    #   退避带抖动：整批同时撞车时，若退避时长相同会在下一个刻度再次一起撞。
    _RETRYABLE_HTTP = (429, 503)
    _RETRYABLE_CODES = (12002, 10010)

    def _submit_job(self, image_path: str, batch_id: str = "") -> str:
        """multipart POST 提交任务，返回 jobId。频率类错误自动退避重试。

        ✅ 2026-09-14 P-OPT-6：`batch_id` 是**客户端自定义**的分组名（官方原文
        「用户自定义传入，形式自订」）。带上它才能用批量查询端点统一收结果。
        逐张路径传空串 ⇒ 请求体与 P-OPT-6 之前**逐字节一致**。

        ✅ P-OPT-7：HTTP 429/503 或 body code 12002/10010 ⇒ 指数退避重试
        （`submit_retry` 次，首次 `submit_backoff` 秒，乘 2，带 ±25% 抖动）。
        **其它错误一律不重试**（重试凭证错误只是白等）。
        重试耗尽后抛 RuntimeError，错误串**不含 token**。
        """
        import logging
        log = logging.getLogger("local_chronicles_ocr")
        optional_payload = {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }
        attempt = 0
        while True:
            with open(image_path, "rb") as f:
                files = {"file": (os.path.basename(image_path), f, "application/octet-stream")}
                data = {
                    "model": self.model,
                    "optionalPayload": json.dumps(optional_payload, ensure_ascii=False),
                }
                if batch_id:
                    data["batchId"] = batch_id
                headers = {"Authorization": f"bearer {self.token}"}
                resp = _get_http_session().post(
                    self.job_url, headers=headers, data=data, files=files, timeout=30
                )
            # ✅ 错误信息不打印 token
            body = None
            try:
                body = resp.json()
            except Exception:
                body = None
            code = body.get("code") if isinstance(body, dict) else None
            if resp.status_code == 200 and code == 0 and "data" in (body or {}):
                return body["data"]["jobId"]
            detail = f"HTTP {resp.status_code}, body={resp.text[:300]}"
            retryable = (resp.status_code in self._RETRYABLE_HTTP
                         or code in self._RETRYABLE_CODES)
            if retryable and attempt < self.submit_retry:
                delay = self.submit_backoff * (2 ** attempt) * (0.75 + 0.5 * random.random())
                log.warning(
                    f"[PaddleOCR-VL] 提交被限流（{detail[:80]}），"
                    f"{delay:.1f}s 后重试 {attempt + 1}/{self.submit_retry}"
                )
                time.sleep(delay)
                attempt += 1
                continue
            raise RuntimeError(f"PaddleOCR-VL 提交失败: {detail}")


    def _poll_job(self, job_id: str) -> dict:
        """轮询任务状态，返回 done 时的 resultUrl.jsonUrl。"""
        import requests
        import time as _t
        waited = 0.0
        while waited < self.max_wait:
            _t.sleep(self.poll_interval)
            waited += self.poll_interval
            try:
                resp = _get_http_session().get(
                    f"{self.job_url}/{job_id}",
                    headers={"Authorization": f"bearer {self.token}"},
                    timeout=30,
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
                if body.get("code") != 0 or "data" not in body:
                    continue
                state = body["data"].get("state")
                if state == "done":
                    return body["data"]
                elif state == "failed":
                    err = body["data"].get("errorMsg") or "?"
                    raise RuntimeError(f"PaddleOCR-VL 任务失败: {err}")
                # pending / running → 继续轮询
            except requests.RequestException:
                continue
        raise RuntimeError(f"PaddleOCR-VL 任务超时（>{self.max_wait}s）")

    # ✅ P-OPT-1（2026-08-19）：VL 解析模式，读 ocr_backend_config.json
    #   "block"（默认）：保留 VL 块级文本 + 块内按行均分 bbox 高度，
    #                    再按 x_center 聚类重建竖排列结构
    #   "char"（旧行为）：拆字 + 宽度均分（回退开关，盒子退化的旧行为）
    @staticmethod
    def _get_vl_parse_mode() -> str:
        try:
            cfg_path = os.path.join(
                str(Path(sys.executable).parent.resolve() if getattr(sys, "frozen", False)
                    else Path(__file__).parent),
                "ocr_backend_config.json")
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            mode = (cfg.get("vl_parse_mode") or "block").strip().lower()
            return mode if mode in ("block", "char") else "block"
        except Exception:
            return "block"

    def _parse_jsonl_char_mode(self, blocks: list, log) -> dict:
        """旧行为：拆字（保留作 fallback，与 2026-08-19 之前的输出完全一致）。"""
        all_lines = []
        text_parts = []
        for text, bbox in blocks:
            chars = list(text.replace("\n", ""))
            n = len(chars)
            if n == 0:
                continue
            x1, y1, x2, y2 = bbox
            width = (x2 - x1) / n
            for i, ch in enumerate(chars):
                cx1 = x1 + i * width
                cx2 = x1 + (i + 1) * width
                char_box = [[cx1, y1], [cx2, y1], [cx2, y2], [cx1, y2]]
                all_lines.append({
                    "box": char_box,
                    "text": ch,
                    "confidence": None,
                    "col_index": 0,
                    "row_index": len(all_lines),
                })
                text_parts.append(ch)
        return {
            "text": "".join(text_parts),
            "lines": all_lines,
            "columns": [all_lines],
        }

    def _parse_jsonl(self, jsonl_text: str, log, image=None) -> dict:
        """解析 JSONL（每行一个 result），返回统一结构。

        P-OPT-1（2026-08-19）重写：
        - 旧版把 VL 返回的块级 block_content 拆成单字符并按宽度均分 bbox
          → 竖排文本拆错轴 + 文本结构被销毁（total_lines == total_chars）
        - 新版（block 模式，默认）：
          1. 保留块级文本；块内含 \\n 的多列块改用像素投影切列重建几何（无图像源时退回按行数均分高度）
          2. 用 column_detector.find_columns_from_ocr_boxes 按 x_center 聚类
             重建列结构（竖排：每列一个块簇；横排：聚成一列按 y 排序）
          3. block 模式产出 0 行时自动回退 char 模式（绝不产出空结果）
        - 配置：ocr_backend_config.json 的 "vl_parse_mode" ("block"|"char")

        P-OPT-5（2026-08-20）：核心逻辑提取到模块级 build_result_from_blocks，
        供 repair_structured.py 离线修复历史碎片化数据复用（同一条代码路径）。
        """
        # ---- 第 1 步：抽出所有 (block_content, block_bbox) ----
        blocks = []
        for line_num, line in enumerate(jsonl_text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                line_data = json.loads(line)
            except Exception as e:
                log.warning(f"[PaddleOCR-VL] 解析 JSONL 第 {line_num} 行失败: {e}")
                continue
            result = line_data.get("result", {})
            for page in result.get("layoutParsingResults", []):
                pr = page.get("prunedResult", {})
                for block in pr.get("parsing_res_list", []):
                    text = block.get("block_content") or ""
                    bbox = block.get("block_bbox")  # [x1, y1, x2, y2]
                    if not text or not bbox or len(bbox) != 4:
                        continue
                    blocks.append((text, [float(v) for v in bbox]))

        mode = self._get_vl_parse_mode()
        if mode == "char":
            return self._parse_jsonl_char_mode(blocks, log)
        result = build_result_from_blocks(blocks, log, image=image)
        if not result["lines"]:
            # 回退：block 模式产出 0 行（异常 JSONL）→ 用旧行为兜底
            log.warning("[PaddleOCR-VL] block 模式解析出 0 行，回退 char 模式")
            return self._parse_jsonl_char_mode(blocks, log)
        return result

    def recognize(self, image_path: str) -> Dict:
        """识别一张图，调用 PaddleOCR-VL 异步 API，拆段级 box 为字级。

        ✅ 2026-09-14 P-OPT-6：**签名与逐张语义未变**（watchdog 单图路径零影响）。
        新增的只是入口处两问：
          ① 命中预取缓存 → 直接返回（**不重复计费**）；
          ② 命中预取在途 → **等它**（job 早已在服务端并行跑，不重复提交）。
        两问皆未命中 ⇒ 原样走逐张路径 `_recognize_one`。未开启预取 / 预取失败时，
        行为与 P-OPT-6 之前**完全一致**。
        """
        import logging
        log = logging.getLogger("local_chronicles_ocr")
        key = self._cache_key(image_path)
        # ① 缓存命中（预取已产出）
        hit = self._cache_get(key)
        if hit is not None:
            log.info(f"[PaddleOCR-VL] ⚡ 命中预取结果（{os.path.basename(image_path)}）")
            return hit
        # ② 预取在途 → 等结果，而不是再提交一次
        ent = self._pending_get(key)
        if ent is not None:
            if ent.event.wait(self.max_wait) and ent.parsed is not None:
                self._cache_put(key, ent.parsed)
                self._pending_drop(key)
                log.info(f"[PaddleOCR-VL] ⚡ 预取已就绪（{os.path.basename(image_path)}）")
                return ent.parsed
            # 等待超时 / 该张失败 → 落到逐张路径自己提交（**绝不静默丢页**）
            log.warning(
                f"[PaddleOCR-VL] 预取未就绪（{os.path.basename(image_path)}，"
                f"err={ent.error or 'timeout'}），回退逐张提交")
            self._pending_drop(key)
        return self._recognize_one(image_path)

    def _recognize_one(self, image_path: str) -> Dict:
        """逐张路径：提交 → 轮询 → 下载 → 解析。

        = P-OPT-6 之前 `recognize()` 的主体，**逐字保留**（只改名）。
        """
        import logging
        import time as _t
        log = logging.getLogger("local_chronicles_ocr")
        t0 = _t.time()
        # 1. 提交
        try:
            job_id = self._submit_job(image_path)
            log.info(f"[PaddleOCR-VL] job submitted: {job_id} (image={os.path.basename(image_path)})")
        except Exception as e:
            log.error(f"[PaddleOCR-VL] ❌ 提交失败: {e}")
            raise
        # 2. 轮询
        try:
            result = self._poll_job(job_id)
        except Exception as e:
            log.error(f"[PaddleOCR-VL] ❌ 轮询失败: {e}")
            raise
        jsonl_url = result.get("resultUrl", {}).get("jsonUrl")
        if not jsonl_url:
            raise RuntimeError("PaddleOCR-VL 结果无 jsonUrl")
        # 3. 下载 JSONL
        try:
            r = _get_http_session().get(jsonl_url, timeout=60)
            r.raise_for_status()
            jsonl_text = r.text
        except Exception as e:
            log.error(f"[PaddleOCR-VL] ❌ 下载结果失败: {e}")
            raise RuntimeError(f"PaddleOCR-VL 下载结果失败: {e}")
        # 4. 解析
        parsed = self._parse_jsonl(jsonl_text, log, image=image_path)
        # 2026-09-11：原始返回随结果带出（**仅多一个键，读数方零感知**）。
        # 用途：下游做「解析前后零丢字」核对（红线：绝不丢字）——
        # 没有原始块就无法判断"少了的字"是解析丢了还是模型没给。
        parsed["raw_jsonl"] = jsonl_text
        elapsed = _t.time() - t0
        log.info(
            f"[PaddleOCR-VL] ✓ {len(parsed['lines'])} boxes "
            f"（model={self.model}, {elapsed:.1f}s, image={os.path.basename(image_path)}）"
        )
        return parsed

    # ══════════════════════════════════════════════════════════
    # P-OPT-6：预取（批量提交）—— 缓存 / 在途 / 后台编排
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _cache_key(image_path: str) -> tuple:
        """缓存键 = **内容指纹**（绝对路径 + size + mtime_ns），不是纯路径。

        为什么不用纯路径：同名文件被覆盖后仍会命中旧结果。
        为什么不读文件算 hash：多一次全文件 IO；而「同路径 + 同 size + 同 mtime_ns」
        实际不可碰撞，且 `copy2` / `write_bytes` 都会刷新 mtime ——
        这正是想要的「换了内容就换 key」。
        """
        try:
            st = os.stat(image_path)
            return (os.path.abspath(image_path), int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            return (os.path.abspath(image_path), -1, -1)

    def _cache_get(self, key: tuple) -> Optional[Dict]:
        if self.cache_ttl <= 0:
            return None
        with self._cache_lock:
            row = self._cache.get(key)
            if not row:
                return None
            expire, parsed = row
            if expire < time.time():
                self._cache.pop(key, None)
                return None
            return parsed

    def _cache_put(self, key: tuple, parsed: Dict) -> None:
        if self.cache_ttl <= 0 or parsed is None:
            return
        with self._cache_lock:
            # 极简 LRU：超上限时按插入序淘汰最旧的 1/4（dict 保序）
            if len(self._cache) >= self.cache_max:
                for k in list(self._cache.keys())[: max(1, self.cache_max // 4)]:
                    self._cache.pop(k, None)
            self._cache[key] = (time.time() + self.cache_ttl, parsed)

    def _pending_get(self, key: tuple) -> Optional[_PrefetchEntry]:
        with self._cache_lock:
            return self._pending.get(key)

    def _pending_drop(self, key: tuple) -> None:
        with self._cache_lock:
            self._pending.pop(key, None)

    def clear_cache(self) -> None:
        """清空结果缓存（用户要强制重跑同一张图时用）。"""
        with self._cache_lock:
            self._cache.clear()

    def prefetch(self, image_paths: List[str], *, max_parallel: int = None,
                 batch_id: str = "", block: bool = False) -> int:
        """把一批图**预先提交**出去，让服务端并行跑。返回登记成功的张数。

        语义：
        - 提交动作在**后台线程**里（默认不阻塞调用方）；`block=True` 则等待完成（测试用）。
        - 已缓存 / 已在途的图**不重复提交**（幂等：重复调用安全）。
        - 单张失败只标记该张，其余照常；**全部失败 = 逐张路径零感知退化**。
        - `batch_prefetch <= 0`（默认）时**什么都不做**，返回 0 ——
          这就是「默认关闭」的全部含义，不影响任何既有行为。

        为什么这样设计而不是替换 `recognize`：`recognize` 是 watchdog 单图路径的
        唯一入口，它必须保持「给一张图、拿一份结果」的直白语义。预取只做**前置**，
        把「什么时候提交」与「怎么拿结果」解耦。

        `max_parallel=None` ⇒ 用 `self.batch_parallel`（来自 config.py / json）。
        """
        if max_parallel is None:
            max_parallel = self.batch_parallel
        paths = [p for p in (image_paths or []) if p]
        if self.batch_prefetch <= 0 or not paths:
            return 0
        paths = paths[: self.batch_prefetch]
        bid = batch_id or f"local-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        entries: List[tuple] = []
        with self._cache_lock:
            for p in paths:
                key = self._cache_key(p)
                if key in self._pending or key in self._cache:
                    continue          # 已在途 / 已有结果 → 不重复提交
                ent = _PrefetchEntry(p)
                self._pending[key] = ent
                entries.append((key, ent))
        if not entries:
            return 0
        th = threading.Thread(
            target=self._prefetch_worker, args=(entries, bid, max_parallel),
            name=f"ocr-prefetch-{bid}", daemon=True,
        )
        th.start()
        if block:
            th.join()
        return len(entries)

    def _prefetch_worker(self, entries: List[tuple], batch_id: str, max_parallel: int) -> None:
        """并发提交 → 并发轮询 → 下载解析 → 写缓存。**单张失败不拖累其余。**"""
        import logging
        from concurrent.futures import ThreadPoolExecutor, as_completed
        log = logging.getLogger("local_chronicles_ocr")
        t0 = time.time()
        n_ok = 0
        n_fail = 0

        def _one(idx: int, _key: tuple, ent: _PrefetchEntry) -> bool:
            try:
                # 官方约束：同一 batchId 最多 100 条 job（10009）→ 按序切批
                bid = f"{batch_id}-{idx // max(1, self.batch_max)}"
                ent.job_id = self._submit_job(ent.path, batch_id=bid)
                result = self._poll_job(ent.job_id)
                jsonl_url = (result.get("resultUrl") or {}).get("jsonUrl")
                if not jsonl_url:
                    raise RuntimeError("结果无 jsonUrl")
                r = _get_http_session().get(jsonl_url, timeout=60)
                r.raise_for_status()
                parsed = self._parse_jsonl(r.text, log, image=ent.path)
                parsed["raw_jsonl"] = r.text          # 与逐张路径同口径（零丢字核对要用）
                ent.parsed = parsed
                return True
            except Exception as e:
                ent.error = f"{type(e).__name__}: {e}"
                return False
            finally:
                ent.event.set()                       # 无论成败都放行等待者

        try:
            with ThreadPoolExecutor(max_workers=max(1, max_parallel),
                                    thread_name_prefix="ocr-prefetch") as ex:
                futs = []
                for i, (key, ent) in enumerate(entries):
                    if i:
                        # 节流：规避提交队列上限（10010）与频率限制（12002）
                        time.sleep(self.submit_interval)
                    futs.append(ex.submit(_one, i, key, ent))
                for f in as_completed(futs):
                    try:
                        if f.result():
                            n_ok += 1
                        else:
                            n_fail += 1
                    except Exception:
                        n_fail += 1
        except Exception as e:
            # 编排本身炸了 → 把所有未完成的条目放行（recognize 会自己提交）
            log.warning(f"[PaddleOCR-VL] 预取编排异常，未完成项全部回退逐张: {e}")
            for _key, ent in entries:
                if not ent.event.is_set():
                    ent.error = f"prefetch_orchestrator: {e}"
                    ent.event.set()

        # 成功的：写缓存 + 摘在途标记。失败的：**保留在途条目**，
        # 让 recognize 读到 error 后自行回退（只回退一次，随后条目被摘除）。
        for key, ent in entries:
            if ent.parsed is not None:
                self._cache_put(key, ent.parsed)
                self._pending_drop(key)
        log.info(
            f"[PaddleOCR-VL] 预取完成 batch={batch_id} 成功 {n_ok} / 失败 {n_fail} "
            f"（{time.time() - t0:.1f}s, 并发 {max_parallel}）"
        )
