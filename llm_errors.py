"""P-K7 LLM 异常类（v5.1）。

设计：自定义异常类传业务异常（CLAUDE.md 编码规范），不要直接用 Exception。
- ProviderNotConfigured: provider 缺 api_key 或 config
- ProviderAPIError: 上游 HTTP 错误（非 429）
- RateLimited: 429 限流
- StreamInterrupted: SSE 流式中断
"""
from __future__ import annotations


class LLMError(Exception):
    """所有 LLM 相关异常的基类。"""
    pass


class ProviderNotConfigured(LLMError):
    """Provider 缺 api_key 或 config（如环境变量未设置）。"""
    pass


class ProviderAPIError(LLMError):
    """上游 API 错误（4xx/5xx，非 429）。"""
    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RateLimited(LLMError):
    """上游 429 限流。"""
    def __init__(self, message: str = "上游 429 限流", retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class StreamInterrupted(LLMError):
    """SSE 流式响应被中断（网络断 / 服务端断流）。"""
    pass
