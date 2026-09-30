"""P-K7 LLM 客户端模块。

设计要点（v5.2）：
- 顶层 ``VALID_PROVIDERS`` 单一事实来源（Q14 + CLAUDE.md #2 对齐）
- api_key 优先级：环境变量（Q13 保留）＞ 本地密钥文件 data/llm_keys.json
  （v5.2 新增：分发场景 UI 写入，文件权限 0600，仅存 provider → key）
- 支持 OpenAI 兼容协议（DeepSeek / 火山 / OpenAI / Moonshot / Qwen-API /
  llama.cpp server / vLLM 等任意 base_url）
- 零外部依赖（仅用 stdlib urllib.request），不增加 requirements.txt
- 模块级 threading.RLock 保护 config 加载
- V4-Flash thinking 模型：自动分离 reasoning_content / content
- api_key 脱敏：前 4 后 4 字符（与 OCR 凭证一致，CLAUDE.md #1）

使用：
    from llm_client import get_llm_client, load_config, VALID_PROVIDERS

    config = load_config(Path("data/llm_config.json"))
    client = get_llm_client(config)
    chunks = list(client.chat([{"role": "user", "content": "你好"}], stream=False))
    print("".join(chunks))
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from llm_errors import (
    ProviderAPIError,
    ProviderNotConfigured,
    StreamInterrupted,
    RateLimited,
)


# === 顶层单一事实来源（Q14 + CLAUDE.md #2）===
VALID_PROVIDERS: frozenset[str] = frozenset({
    "openai_compatible",
    "ollama",
    "local_transformers",
})


# === Protocol 定义 ===
@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端统一接口（所有 provider 必须实现）"""
    def chat(
        self,
        messages: list[dict],
        stream: bool = True,
        response_format: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[str]:
        """对话接口。stream=True 返逐 chunk 迭代器，stream=False 返单 chunk 迭代器。

        Args:
            messages: OpenAI 风格 messages
            stream: True=SSE 流式 / False=非流式
            response_format: 可选，OpenAI 兼容；如 {"type": "json_object"} 强制 JSON 输出
            max_tokens: 单次调用覆盖构造时的 max_tokens（None=用默认）
            temperature: 单次调用覆盖构造时的 temperature（None=用默认）
            timeout: 单次调用覆盖构造时的 timeout（None=用默认）；
                thinking 模型长输入场景务必调大（默认 60s 容易被 reasoning 耗尽）
        """
        ...

    @property
    def model_name(self) -> str: ...

    @property
    def provider_name(self) -> str: ...


# === OpenAI 兼容协议客户端（核心）===
class OpenAICompatibleClient:
    """OpenAI 兼容协议客户端。

    适用：DeepSeek / 火山引擎 / OpenAI / Moonshot / Qwen-API /
    llama.cpp server / vLLM / Xinference 等任意 OpenAI 兼容端点。

    实测（2026-08-19）：
    - DeepSeek 官方 API base_url = https://api.deepseek.com/v1
    - 可用模型：deepseek-flash（DeepSeek V4.1 Flash，2026-09-10 起为官方主推；
      V4 Flash 与 V4 Pro 均已下线，旧模型名会被路由到 V4.1 Flash）
    - V4-Flash 是 thinking 模型：response 含 reasoning_content + content
      （本客户端只对外暴露 content，reasoning_content 静默丢弃，Q16）
    """

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,  # 阶段 4-B-C：配合 reasoning_effort="low" 把 reasoning 缩短；12k 浪费资源
        timeout: int = 60,
        reasoning_effort: str = "low",  # 阶段 4-B-C：V4 thinking 模型 reasoning 占比 95%；限制为 "low"
    ):
        self.base_url = base_url.rstrip("/")
        self._model_name = model_name
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        # 最近一次**非流式**调用的 usage（{prompt_tokens, completion_tokens, ...}）。
        # 流式调用不保证回传 usage，故不置位；调用方按需读取、缺省视为 None。
        self.last_usage: Optional[dict] = None

    def chat(
        self,
        messages: list[dict],
        stream: bool = True,
        response_format: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Iterator[str]:
        """调用 LLM。

        Args:
            messages: OpenAI 风格 messages（[{"role": ..., "content": ...}, ...]）
            stream: True=SSE 流式；False=单次返回
            response_format: 可选，OpenAI 兼容；如 {"type": "json_object"} 强制 JSON 输出
            max_tokens: 单次覆盖（None=用构造默认）；thinking 模型 reasoning 会吃预算，
                大输入务必调大，否则 content 可能被截断为空
            temperature: 单次覆盖（None=用构造默认）；确定性任务（如重排）建议 0
            timeout: 单次覆盖（None=用构造默认）；thinking 模型长输入建议调大

        Yields:
            stream=True 时逐 chunk 产出 content 字符串
            stream=False 时一次性产出完整 content

        Raises:
            ProviderAPIError: HTTP 错误 / finish_reason=length 且 content 为空
            RateLimited: 429 限流
        """
        body_dict = {
            "model": self.model_name,
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        # 阶段 4-B-C：V4 thinking 模型加 reasoning_effort="low" 限制 reasoning 长度
        # 实际效果：reasoning_tokens 从 3770 降到 132（实测 deepseek-flash）
        # 节省 95% reasoning 开销 + 显著降低 length 截断概率
        #
        # ✅ 2026-09-11（L1）：**允许单次覆盖**。实测 deepseek-flash 上
        # `reasoning_effort="none"` 可把 reasoning 彻底归零——同一提示词、
        # 同一答案，耗时 11.1s→0.6s、输出 2199→66 tokens（**6.3×**）。
        # "指路型"任务（只要求模型报行号/做映射）不需要长思考，默认可关；
        # 复杂生成任务仍走实例默认。传 None = 用实例值（向后兼容）。
        eff = self.reasoning_effort if reasoning_effort is None else reasoning_effort
        if eff:
            body_dict["reasoning_effort"] = eff
        if response_format:
            body_dict["response_format"] = response_format
        body = json.dumps(body_dict).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json" if not stream else "text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(
                req, timeout=self.timeout if timeout is None else timeout
            )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(f"上游 429 限流: {e.reason}")
            body_err = e.read().decode("utf-8", errors="replace")[:500]
            # ✅ M21 修复：必须把 HTTP 状态码传入 status_code。原先只传 message，
            # status_code 恒为 None → app 层 getattr(e, "status_code", 502) 拿到 None
            # （属性存在时 getattr 不用默认值）→ M18 /api/health 的 402/429 分支永不命中。
            raise ProviderAPIError(
                f"HTTP {e.code}: {e.reason} | {body_err}", status_code=e.code, body=body_err
            )
        except urllib.error.URLError as e:
            raise ProviderAPIError(f"URL 错误: {e.reason}")

        if stream:
            return self._stream_chunks(resp)
        return iter([self._nonstream_response(resp)])

    def chat_message(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str | dict | None = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
    ) -> dict:
        """非流式调用，返回完整 message 对象（含 tool_calls）。

        ✅ 2026-09-10（P-K9 阶段 0 工具层）：问答要支持函数调用，而 SSE 流式
        响应里的 tool_calls 分片解析复杂且易错，因此工具决策走一次非流式调用，
        拿到 tool_calls 后由调用方执行工具，再以流式产出最终回答。

        Returns:
            {"content": str, "tool_calls": [...], "finish_reason": str}

        Raises:
            ProviderAPIError / RateLimited：与 chat() 一致
        """
        body_dict: dict = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if self.reasoning_effort:
            body_dict["reasoning_effort"] = self.reasoning_effort
        if tools:
            body_dict["tools"] = tools
            body_dict["tool_choice"] = tool_choice or "auto"
        body = json.dumps(body_dict).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(
                req, timeout=self.timeout if timeout is None else timeout
            )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(f"上游 429 限流: {e.reason}")
            body_err = e.read().decode("utf-8", errors="replace")[:500]
            raise ProviderAPIError(
                f"HTTP {e.code}: {e.reason} | {body_err}",
                status_code=e.code, body=body_err,
            )
        except urllib.error.URLError as e:
            raise ProviderAPIError(f"URL 错误: {e.reason}")
        raw = resp.read().decode("utf-8")
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderAPIError(f"响应非 JSON: {raw[:300]}") from e
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        return {
            "content": msg.get("content") or "",
            "tool_calls": msg.get("tool_calls") or [],
            "finish_reason": choice.get("finish_reason"),
        }

    def _stream_chunks(self, resp) -> Iterator[str]:
        """SSE 流式解析：只对外 yield content 段（Q16 隐藏 thinking）。

        V4-Flash 响应形如：
          data: {"choices": [{"delta": {"reasoning_content": "..."}}]}
          data: {"choices": [{"delta": {"content": "..."}}]}
          ...
          data: [DONE]
        """
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in obj.get("choices", []):
                    delta = choice.get("delta", {})
                    # 只取 content；reasoning_content 静默丢弃
                    content = delta.get("content")
                    if content:
                        yield content
        except Exception as e:
            raise StreamInterrupted(f"流式中断: {e}") from e
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _nonstream_response(self, resp) -> str:
        raw = resp.read().decode("utf-8")
        d = json.loads(raw)
        # ✅ 2026-09-11（L1 成本取证）：把本次调用的 usage 留在实例上，供调用方
        # 统计真实 token 花费。此前 usage 被整包丢弃，"一页到底花了多少"只能靠估；
        # L1 的省钱结论必须能被**实测数字**证实，故在此留痕（只增不改，
        # 原返回值语义逐字不动）。
        try:
            self.last_usage = d.get("usage") or None
        except Exception:                                  # pragma: no cover
            self.last_usage = None
        choice = d.get("choices", [{}])[0]
        msg = choice.get("message", {})
        # 取 content；reasoning_content 静默丢弃
        content = msg.get("content", "")
        finish_reason = choice.get("finish_reason")
        # V4-Flash 是 thinking 模型：reasoning 会吃掉 max_tokens 预算，
        # 大输入下 content 可能被截断为空串。此时必须显式报错（原实现静默返回 ""，
        # 调用方会误以为模型正常返回了空内容——2026-08-20 reassembler 实测踩坑）。
        if finish_reason == "length" and not content:
            usage = d.get("usage", {})
            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
            raise ProviderAPIError(
                f"max_tokens 截断（finish_reason=length）：reasoning 消耗 {reasoning} tokens，"
                f"content 为空。请调大 max_tokens 或缩小输入。"
            )
        return content

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def model_name(self) -> str:
        return self._model_name

    def __repr__(self) -> str:
        return f"<OpenAICompatibleClient {self.base_url} model={self.model_name}>"


# === 本地 LLM 客户端（M13 2026-09-01 实施）===
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class _ThinkFilter:
    """流式 <think>…</think> 过滤器（状态机）。

    Qwen3/3.5 系思考模型经 Ollama /api/chat 输出时，思考内容以
    ``<think>…</think>`` 内联在 content 里。此过滤器逐 chunk 消费文本，
    只放行思考块之外的正文；块结束后的前导空白一并吞掉。
    未闭合的块（被 max_tokens 截断）视为"后续全是思考"，全部吞掉。
    """

    def __init__(self) -> None:
        self._pending = ""   # 可能含半个标签的尾巴
        self._in_think = False
        self._emitted_any = False

    def feed(self, text: str) -> str:
        """消费一个 chunk，返回可对外输出的部分。"""
        self._pending += text
        out = []
        while True:
            if self._in_think:
                idx = self._pending.find(_THINK_CLOSE)
                if idx < 0:
                    # 保留可能是半个闭合标签的尾巴（</think> 共 8 字符）
                    keep = len(_THINK_CLOSE) - 1
                    if len(self._pending) > keep:
                        self._pending = self._pending[-keep:]
                    break
                self._pending = self._pending[idx + len(_THINK_CLOSE):]
                self._in_think = False
            else:
                idx = self._pending.find(_THINK_OPEN)
                if idx < 0:
                    # 放行"确认不是半个开标签"的前缀（<think> 共 7 字符）
                    keep = len(_THINK_OPEN) - 1
                    if len(self._pending) <= keep:
                        break
                    safe = self._pending[: len(self._pending) - keep]
                    self._pending = self._pending[len(safe):]
                    if not self._emitted_any:
                        safe = safe.lstrip()
                    if safe:
                        out.append(safe)
                        self._emitted_any = True
                    break
                # 找到开标签：放行标签前正文（吞掉首个正文的前导空白）
                before = self._pending[:idx]
                if not self._emitted_any:
                    before = before.lstrip()
                if before:
                    out.append(before)
                    self._emitted_any = True
                self._pending = self._pending[idx + len(_THINK_OPEN):]
                self._in_think = True
        return "".join(out)

    def flush(self) -> str:
        """流结束：未在思考块中则放行尾巴。"""
        if self._in_think or not self._pending:
            return ""
        tail = self._pending
        if not self._emitted_any:
            tail = tail.lstrip()
        self._pending = ""
        return tail


class OllamaClient:
    """本地 Ollama 客户端（M13 2026-09-01 实施）。

    走原生 ``POST /api/chat``（ndjson 流式），不依赖 /v1 OpenAI 兼容层——
    原生端点对 ``format``/``options`` 的支持最稳。

    要点：
    - urlopen 在 chat() 调用时立即发生（连接期错误当次抛出），
      这是 RoutedChatClient 能在调用点安全降级的前提
    - Qwen 系思考模型的 <think>…</think> 内联内容被静默过滤（与
      OpenAICompatibleClient 丢弃 reasoning_content 的语义对齐）
    - response_format={"type":"json_object"} → Ollama ``format:"json"``
    - Ollama 未启动 / 模型未拉取 → ProviderAPIError（消息含排查提示）
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model_name: str = "qwen3.5:0.8b",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        timeout: int = 120,  # CPU 推理慢，默认放宽
        think: bool = False,  # M13：默认关思考（实测 0.8B 模型 11.5s→0.9s）
    ):
        self.base_url = base_url.rstrip("/")
        self._model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.think = think

    def chat(
        self,
        messages: list[dict],
        stream: bool = True,
        response_format: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[str]:
        body_dict = {
            "model": self.model_name,
            "messages": messages,
            "stream": stream,
            "think": self.think,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_predict": self.max_tokens if max_tokens is None else max_tokens,
            },
        }
        if response_format and response_format.get("type") == "json_object":
            body_dict["format"] = "json"
        try:
            return self._do_chat(body_dict, stream, timeout)
        except ProviderAPIError as e:
            # 旧版 Ollama / 不支持 thinking 的模型可能拒绝 "think" 字段：
            # 去掉该参数重试一次（仅当本次请求确实带了 think）
            if "think" in body_dict and ("think" in str(e).lower() or "400" in str(e)):
                body_dict = {k: v for k, v in body_dict.items() if k != "think"}
                return self._do_chat(body_dict, stream, timeout)
            raise

    def _do_chat(
        self,
        body_dict: dict,
        stream: bool,
        timeout: Optional[int],
    ) -> Iterator[str]:
        body = json.dumps(body_dict).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(
                req, timeout=self.timeout if timeout is None else timeout
            )
        except urllib.error.HTTPError as e:
            body_err = ""
            try:
                body_err = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            if e.code == 404:
                raise ProviderAPIError(
                    f"Ollama 模型不存在（请先 ollama pull）：{body_err or e.reason}",
                    status_code=e.code,
                )
            if e.code == 429:
                raise RateLimited(f"Ollama 429 限流: {e.reason}")
            raise ProviderAPIError(
                f"Ollama HTTP {e.code}: {e.reason} | {body_err}", status_code=e.code
            )
        except urllib.error.URLError as e:
            raise ProviderAPIError(
                f"Ollama 未启动或不可达（{self.base_url}）: {e.reason}"
            )
        except TimeoutError as e:
            raise ProviderAPIError(f"Ollama 推理超时: {e}")

        if stream:
            return self._stream_chunks(resp)
        return iter([self._nonstream_response(resp)])

    def _stream_chunks(self, resp) -> Iterator[str]:
        """ndjson 流式解析 + think 过滤。每行形如
        ``{"message": {"content": "..."}, "done": false}``。"""
        flt = _ThinkFilter()
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    raise ProviderAPIError(f"Ollama 流内错误: {obj['error']}")
                content = (obj.get("message") or {}).get("content")
                if content:
                    out = flt.feed(content)
                    if out:
                        yield out
                if obj.get("done"):
                    break
            tail = flt.flush()
            if tail:
                yield tail
        except StreamInterrupted:
            raise
        except ProviderAPIError:
            # ✅ M24：Ollama 流内应用层错误（line 448 主动抛出，如模型不存在）
            # 直接透传——不再被下方兜底包装成 StreamInterrupted（504 会掩盖
            # 真实错误，app.py SSE 的 except ProviderAPIError / M18 预警收不到）
            raise
        except Exception as e:
            raise StreamInterrupted(f"Ollama 流式中断: {e}") from e
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _nonstream_response(self, resp) -> str:
        raw = resp.read().decode("utf-8", errors="replace")
        d = json.loads(raw)
        if d.get("error"):
            raise ProviderAPIError(f"Ollama 错误: {d['error']}")
        content = (d.get("message") or {}).get("content", "")
        # 思考模型非流式：整块剔除 <think>…</think>（含未闭合）
        content = re.sub(
            r"<think>.*?(?:</think>|\Z)", "", content, flags=re.DOTALL
        )
        return content.lstrip()

    def ping(self, timeout: int = 3) -> list[str]:
        """探测 Ollama 服务并返回**真正驻留本机**的模型名列表。

        云端模型（name 以 ``:cloud`` 结尾）不算本地资源，排除在外。

        Raises:
            ProviderAPIError: 服务不可达
        """
        try:
            resp = urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=timeout)
        except (urllib.error.URLError, OSError) as e:
            raise ProviderAPIError(f"Ollama 未启动或不可达: {e}")
        try:
            d = json.loads(resp.read().decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return []
        return [
            m.get("name", "")
            for m in d.get("models", [])
            if m.get("name") and not m.get("name").endswith(":cloud")
        ]

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model_name

    def __repr__(self) -> str:
        return f"<OllamaClient {self.base_url} model={self.model_name}>"


class LocalTransformersClient:
    """本地 transformers 客户端（阶段 6 实施，本阶段占位）。

    用 transformers 库本地加载小模型（如 Qwen2-1.5B-Instruct）。
    """

    def __init__(self, model_path: str = "", device: str = "cpu"):
        self.model_path = model_path
        self.device = device

    def chat(self, messages: list[dict], stream: bool = True) -> Iterator[str]:
        raise NotImplementedError("LocalTransformersClient 待阶段 6 实施")

    @property
    def provider_name(self) -> str:
        return "local_transformers"


# === 配置管理（模块级 RLock 保护）===
_config_lock = threading.RLock()
_current_config: dict | None = None
_current_client: LLMClient | None = None


def load_config(config_path: Path, apply_env: bool = True) -> dict:
    """从 JSON 加载 llm config。文件不存在则返回默认配置（首次启动）。

    默认配置 provider=openai_compatible，model_name=deepseek-flash，
    api_key 不在 JSON 中（走环境变量 Q13）。

    ✅ M17（2026-09-01）：字段级环境变量覆盖（部署级配置外部化）。
    优先级：环境变量 > JSON 配置 > 默认值。api_key 另有独立链路
    （Q13：环境变量 > llm_keys.json），不在本表内。

    ✅ M24：apply_env=False 返回未经环境变量覆盖的原始配置——
    供"读-改-写"保存路径（POST /api/llm/config）使用，避免 env 覆盖值
    被固化写回 JSON（固化后重启即丢失 env 单一事实来源）。
    """
    with _config_lock:
        if not config_path.exists():
            base = _default_config()
        else:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                # 防御：即使 JSON 被改坏，也补全关键字段
                cfg.setdefault("provider", "openai_compatible")
                cfg.setdefault("base_url", "https://api.deepseek.com/v1")
                cfg.setdefault("model_name", "deepseek-flash")
                cfg.setdefault("temperature", 0.3)
                cfg.setdefault("max_tokens", 2048)
                base = cfg
            except (json.JSONDecodeError, OSError):
                base = _default_config()
        return _apply_env_overrides(base) if apply_env else base


# ✅ M17：部署级环境变量 → config 字段映射（全大写、OCR_LLM_ 前缀，避免撞名）
_LLM_ENV_OVERRIDES = {
    "provider": "OCR_LLM_PROVIDER",
    "base_url": "OCR_LLM_BASE_URL",
    "model_name": "OCR_LLM_MODEL",
    "temperature": "OCR_LLM_TEMPERATURE",
    "max_tokens": "OCR_LLM_MAX_TOKENS",
    "ollama_url": "OCR_LLM_OLLAMA_URL",
    "ollama_model": "OCR_LLM_OLLAMA_MODEL",
}


def _apply_env_overrides(cfg: dict) -> dict:
    """M17：用环境变量覆盖 config 字段（未设置/空值/非法数值时保持原值）。"""
    for field, env_name in _LLM_ENV_OVERRIDES.items():
        val = os.environ.get(env_name)
        if val is None or not val.strip():
            continue
        val = val.strip()
        try:
            if field == "temperature":
                cfg[field] = float(val)
            elif field == "max_tokens":
                cfg[field] = int(val)
            else:
                cfg[field] = val
        except ValueError:
            pass  # 非法数值（如 temperature=abc）忽略，保持 JSON/默认值
    return cfg


def save_config(config: dict, config_path: Path) -> None:
    """保存 config 到 JSON（atomic write：先写 .tmp 再 rename）。"""
    with _config_lock:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_suffix(config_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        tmp.replace(config_path)
        # 权限 0600（仅当前用户可读写）
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass  # Windows 上可能不支持


def _default_config() -> dict:
    return {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-flash",
        "temperature": 0.3,
        "max_tokens": 2048,
        "ollama_url": "http://localhost:11434",
        "ollama_model": "qwen3.5:0.8b",
        "local_model_path": "",
    }


# === API Key 管理（Q13：环境变量优先；v5.2 增加本地密钥文件兜底）===
_ENV_KEY_HINTS = {
    "openai_compatible": ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY"],
}

# 本地密钥文件（由 app.py 启动时 set_api_keys_file() 指向 data/llm_keys.json）。
# 只存 {provider: api_key}，不包含其他配置字段。
_API_KEYS_FILE: Path | None = None


def set_api_keys_file(path: Path | None) -> None:
    """设置本地密钥文件路径（app.py 启动时调用）。"""
    global _API_KEYS_FILE
    _API_KEYS_FILE = Path(path) if path else None


def load_api_keys(key_file: Path | None = None) -> dict:
    """读密钥文件。文件不存在/损坏时返回 {}。"""
    kf = key_file or _API_KEYS_FILE
    if kf is None or not kf.exists():
        return {}
    try:
        with open(kf, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_api_key(provider: str, api_key: str, key_file: Path | None = None) -> None:
    """保存 provider 的 api_key 到密钥文件（atomic write + 权限 0600）。

    传空串则删除该 provider 的密钥（等价 clear_api_key）。
    """
    kf = key_file or _API_KEYS_FILE
    if kf is None:
        raise ValueError("未设置密钥文件路径（set_api_keys_file）")
    keys = load_api_keys(kf)
    if api_key:
        keys[provider] = api_key.strip()
    else:
        keys.pop(provider, None)
    kf.parent.mkdir(parents=True, exist_ok=True)
    tmp = kf.with_suffix(kf.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)
    tmp.replace(kf)
    try:
        os.chmod(kf, 0o600)
    except OSError:
        pass  # Windows 上可能不支持


def clear_api_key(provider: str, key_file: Path | None = None) -> None:
    """删除 provider 的本地密钥。"""
    save_api_key(provider, "", key_file)


def get_api_key(provider: str, key_file: Path | None = None) -> str | None:
    """读 api_key：先环境变量（Q13），无则查本地密钥文件（v5.2）。

    Returns:
        找到的 api_key；都未设置则返 None
    """
    candidates = _ENV_KEY_HINTS.get(provider, [])
    for var in candidates:
        val = os.environ.get(var)
        if val:
            return val
    val = load_api_keys(key_file).get(provider)
    return val if val else None


def get_api_key_source(provider: str, key_file: Path | None = None) -> str | None:
    """返回当前 provider 的 api_key 来源（用于脱敏提示）。

    Returns:
        "env:VAR_NAME" 或 "file:llm_keys.json" 形式；未配置则返 None
    """
    candidates = _ENV_KEY_HINTS.get(provider, [])
    for var in candidates:
        if os.environ.get(var):
            return f"env:{var}"
    kf = key_file or _API_KEYS_FILE
    if load_api_keys(kf).get(provider):
        return f"file:{kf.name if kf else 'llm_keys.json'}"
    return None


# === 工厂（严格走 VALID_PROVIDERS 校验 Q14）===
def get_llm_client(config: dict) -> LLMClient:
    """根据 config 创建 LLMClient。

    Args:
        config: load_config() 返回的 dict

    Returns:
        LLMClient 实例

    Raises:
        ValueError: provider 不在 VALID_PROVIDERS 内（Q14 硬约束）
        ProviderNotConfigured: openai_compatible 缺 api_key
    """
    provider = config.get("provider", "openai_compatible")
    if provider not in VALID_PROVIDERS:
        raise ValueError(
            f"未知 provider: {provider!r}（必须在 {sorted(VALID_PROVIDERS)} 内）"
        )

    if provider == "openai_compatible":
        api_key = get_api_key("openai_compatible")
        if not api_key:
            raise ProviderNotConfigured(
                "未配置 API Key：请在知识卡片页点 ⚙ 设置填写，"
                "或设置环境变量 DEEPSEEK_API_KEY"
            )
        return OpenAICompatibleClient(
            base_url=config.get("base_url", "https://api.deepseek.com/v1"),
            model_name=config.get("model_name", "deepseek-flash"),
            api_key=api_key,
            temperature=float(config.get("temperature", 0.3)),
            max_tokens=int(config.get("max_tokens", 2048)),
            # 可由 data/llm_config.json 调（缺省 "low" = 原行为不变）。
            # 设 "none" 可彻底关掉 thinking —— 对 deepseek-flash 实测有效
            # （见 chat() 注释），适合"只做映射/抽取"的轻任务。
            reasoning_effort=str(config.get("reasoning_effort", "low")),
        )
    if provider == "ollama":
        return OllamaClient(
            base_url=config.get("ollama_url", "http://localhost:11434"),
            model_name=config.get("ollama_model", "qwen3.5:0.8b"),
            temperature=float(config.get("temperature", 0.3)),
            max_tokens=int(config.get("max_tokens", 2048)),
        )
    if provider == "local_transformers":
        return LocalTransformersClient(
            model_path=config.get("local_model_path", ""),
        )
    # 防御：未实现的 provider（理论上前面已拦截）
    raise ValueError(f"未实现的 provider: {provider}")


# ✅ M23 清理：原 mask_api_key() 生产零引用（app.py 已改用 get_api_key_source），
# 脱敏由 ocr_backend._mask_token 承担，已删（消 D6 重复）。


# === 模块级单例（懒加载，可重置）===
def get_or_create_client(config: dict) -> LLMClient:
    """获取或重建 LLM client 单例。config 变更时调用 reset_llm_client()。"""
    global _current_client
    with _config_lock:
        if _current_client is None:
            _current_client = get_llm_client(config)
        return _current_client


def reset_llm_client() -> None:
    """重置单例（POST /api/llm/config 后调用）。"""
    global _current_client
    with _config_lock:
        _current_client = None
