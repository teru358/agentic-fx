"""Validation and one-shot news retrieval shared by news source callers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
import httpx

from agentic_fx._retry_after import retry_after_from
from agentic_fx.datafeed.fetchers import fetch_feed, fetch_web


class NewsInputError(ValueError):
    """Input is invalid; text is deliberately independent of user input."""


class ProbeFailure(RuntimeError):
    """A retrieval failed with a safe, fixed reason."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    count: int
    with_body: int


@dataclass(frozen=True, slots=True)
class FailureClass:
    reason: str
    kind: str
    retry_after: timedelta | None


def validate_name(name: str) -> None:
    import re
    if not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name) is None:
        raise NewsInputError("名前の形式が正しくありません")


def validate_url(url: str) -> None:
    if not isinstance(url, str) or len(url) > 2048:
        raise NewsInputError("URL の形式が正しくありません")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise NewsInputError("URL の形式が正しくありません") from None
    if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname
            or parts.username is not None or "#" in url):
        raise NewsInputError("URL の形式が正しくありません")


def normalize_url(url: str) -> str:
    """Normalize scheme/host/default port while preserving path and query spelling."""
    validate_url(url)
    p = urlsplit(url)
    host = p.hostname.lower()  # type: ignore[union-attr]
    if ":" in host:
        host = f"[{host}]"
    try:
        port = p.port
    except ValueError:
        raise NewsInputError("URL の形式が正しくありません") from None
    if port is not None and not ((p.scheme.lower() == "http" and port == 80) or
                                 (p.scheme.lower() == "https" and port == 443)):
        host += f":{port}"
    out = urlunsplit((p.scheme.lower(), host, p.path, p.query, ""))
    return out + "?" if url.endswith("?") and not out.endswith("?") else out


def fold_url(url: str) -> str:
    p = urlsplit(normalize_url(url))
    return f"{p.scheme}://{p.netloc}/…"


def read_url_from_stdin(stream, *, getpass_fn) -> str:
    raw = getpass_fn("") if stream.isatty() else stream.readline(2049)
    if not isinstance(raw, str) or len(raw) >= 2049 or (not stream.isatty() and stream.readline(1)):
        raise NewsInputError("標準入力の URL が正しくありません")
    value = raw.strip()
    if not value:
        raise NewsInputError("標準入力の URL が正しくありません")
    return value


def classify_failure(exc: BaseException, now: datetime) -> FailureClass:
    wait = retry_after_from(exc, now)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return FailureClass(f"HTTP {code}", "refusal" if wait else "failure", wait)
    if isinstance(exc, httpx.TimeoutException):
        return FailureClass("タイムアウト", "failure", None)
    if isinstance(exc, httpx.TransportError):
        return FailureClass("接続できません", "failure", None)
    return FailureClass("取得できません", "failure", None)


def probe(fetcher: str, url: str, name: str, *, timeout_sec: float) -> ProbeResult:
    """Fetch exactly once and return article/body counts; failures expose fixed text only."""
    try:
        fn = {"feed": fetch_feed, "web": fetch_web}.get(fetcher)
        if fn is None:
            raise NewsInputError("取得方式が正しくありません")
        articles = fn(url, name, timeout_sec=timeout_sec)
        return ProbeResult(len(articles), sum(bool(a.body) for a in articles))
    except NewsInputError:
        raise
    except BaseException as exc:
        reason = classify_failure(exc, datetime.now().astimezone()).reason
        raise ProbeFailure(reason) from None
