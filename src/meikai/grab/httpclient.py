"""带浏览器 TLS 指纹的异步 HTTP 客户端。"""

import os

from curl_cffi.requests import AsyncSession

_DEFAULT_IMPERSONATE = "chrome131_android"


def impersonate() -> str:
    """返回当前要模拟的浏览器指纹，可用环境变量 MEIKAI_IMPERSONATE 覆盖。"""
    return os.environ.get("MEIKAI_IMPERSONATE", _DEFAULT_IMPERSONATE).strip() or _DEFAULT_IMPERSONATE


def http_client(timeout: float = 30) -> AsyncSession:
    """返回带移动 Chrome TLS/JA3/HTTP2 指纹的异步客户端。

    headers 完全由调用方控制（default_headers=False），只借底层 TLS 指纹。
    """
    return AsyncSession(
        impersonate=impersonate(),
        default_headers=False,
        timeout=timeout,
    )

client = http_client  # 向后兼容别名
