"""Web response に必ず付ける固定 header。"""
from __future__ import annotations

from types import MappingProxyType

# render.STYLE (埋め込む唯一の CSS) の SHA-256。CSP はこの style だけを許し、script は許さない
STYLE_HASH = "sha256-LTD12P28Uy1nqEG8Ug154drZ3pH6F9EnG8flNemSejY="

_CSP = ("default-src 'none'; style-src '" + STYLE_HASH + "'; base-uri 'none'; "
        "form-action {action}; frame-ancestors 'none'")
WRITABLE_CSP = _CSP.format(action="'self'")

SECURITY_HEADERS = MappingProxyType({
    "Content-Security-Policy": _CSP.format(action="'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
})


def response_headers(*, length: int, extra: dict[str, str] | None = None,
                     writable: bool = False) -> dict[str, str]:
    """本文と同じ長さを示す header を、固定の安全 header と共に作る。

    `writable` (書込みが有効な web) のときだけ、form の送信先を同一 origin に緩める。
    """
    headers = dict(SECURITY_HEADERS)
    if writable:
        headers["Content-Security-Policy"] = WRITABLE_CSP
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Content-Length"] = str(length)
    if extra:
        headers.update(extra)
    return headers
