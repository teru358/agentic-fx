"""Web response に必ず付ける固定 header。"""
from __future__ import annotations

from types import MappingProxyType

# render.STYLE (埋め込む唯一の CSS) の SHA-256。CSP はこの style だけを許し、script は許さない
STYLE_HASH = "sha256-dIAw3Rfkv/SAUhYuTRQUAz5UAqeCbdU2MPWMfhMTwaA="

SECURITY_HEADERS = MappingProxyType({
    "Content-Security-Policy": ("default-src 'none'; style-src '" + STYLE_HASH + "'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
})


def response_headers(*, length: int, extra: dict[str, str] | None = None) -> dict[str, str]:
    """本文と同じ長さを示す header を、固定の安全 header と共に作る。"""
    headers = dict(SECURITY_HEADERS)
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Content-Length"] = str(length)
    if extra:
        headers.update(extra)
    return headers
