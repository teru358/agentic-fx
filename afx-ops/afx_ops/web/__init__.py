"""Loopback read-only web の transport 非依存の部品。"""

from afx_ops.web.request import Request, Response, handle_request, parse_http_request

__all__ = ("Request", "Response", "handle_request", "parse_http_request")
