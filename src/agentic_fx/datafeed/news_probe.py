"""One-shot news retrieval and input helpers shared by the news source callers.

Public contract:
- NewsInputError / validate_name / validate_url / normalize_url / fold_url
  (defined in news_url, re-exported here).
- read_url_from_stdin(stream, *, getpass_fn) -> str; NewsInputError on bad input.
- classify_failure(exc, now) -> FailureClass: the one classification used by the
  collector and by probe.  Never inspects anything but exception types, status
  codes and Retry-After, so no URL or message text can leak through it.
- probe(fetcher, url, name, *, timeout_sec) -> ProbeResult: exactly one
  retrieval.  Any retrieval exception becomes ProbeFailure carrying one of
  FIXED_REASONS and no chained exception.  An unknown fetcher raises
  NewsInputError.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from agentic_fx._retry_after import retry_after_from
from agentic_fx.datafeed import fetchers
from agentic_fx.datafeed.news_url import (  # noqa: F401 - re-exported
    MAX_URL_CHARS, NewsInputError, fold_url, normalize_url, validate_name, validate_url,
)

REASON_TIMEOUT = "タイムアウト"
REASON_CONNECT = "接続できません"
REASON_OTHER = "取得できません"
FIXED_REASONS = ("HTTP <code>", REASON_TIMEOUT, REASON_CONNECT, REASON_OTHER)


class ProbeFailure(RuntimeError):
    """A retrieval failed.  Only a fixed reason word is carried (see FIXED_REASONS)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProbeResult:
    count: int
    with_body: int


@dataclass(frozen=True, slots=True)
class FailureClass:
    reason: str
    kind: str  # "refusal" (positive Retry-After on 429/503) or "failure"
    retry_after: timedelta | None


def read_url_from_stdin(stream, *, getpass_fn) -> str:
    """Read one URL for the '-' argument.

    A tty goes through getpass_fn (nothing is echoed).  Otherwise only the first
    line is read, cut at 2049 characters, so a huge pipe is never slurped.  Raises
    NewsInputError for an empty value, a second line, or 2049 characters without
    a line end.  Surrounding whitespace and the line end are removed.
    """
    bad = NewsInputError("標準入力の URL が正しくありません")
    if stream.isatty():
        raw = getpass_fn("URL: ")
        if not isinstance(raw, str) or "\n" in raw.strip("\r\n") or len(raw.strip()) > MAX_URL_CHARS:
            raise bad
    else:
        raw = stream.readline(MAX_URL_CHARS + 1)
        if not isinstance(raw, str):
            raise bad
        content = raw.rstrip("\r\n")
        if len(content) > MAX_URL_CHARS or stream.readline(1):
            raise bad
    value = raw.strip()
    if not value:
        raise bad
    return value


def classify_failure(exc: BaseException, now: datetime) -> FailureClass:
    """Map a retrieval exception to a fixed reason word and a counting kind.

    kind is "refusal" only for 429/503 with a positive Retry-After; every other
    failure is "failure".  now must be timezone-aware.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        wait = retry_after_from(exc, now)
        return FailureClass(f"HTTP {exc.response.status_code}",
                            "refusal" if wait is not None else "failure", wait)
    if isinstance(exc, httpx.TimeoutException):
        return FailureClass(REASON_TIMEOUT, "failure", None)
    if isinstance(exc, httpx.TransportError):
        return FailureClass(REASON_CONNECT, "failure", None)
    return FailureClass(REASON_OTHER, "failure", None)


def probe(fetcher: str, url: str, name: str, *, timeout_sec: float) -> ProbeResult:
    """Fetch exactly once; return article and with-body counts.

    Raises NewsInputError for an unknown fetcher and ProbeFailure (fixed reason
    only, nothing chained) for any exception raised by the retrieval.
    """
    if fetcher == "feed":
        fn = fetchers.fetch_feed
    elif fetcher == "web":
        fn = fetchers.fetch_web
    else:
        raise NewsInputError("取得方式が正しくありません")
    reason: str | None = None
    try:
        articles = fn(url, name, timeout_sec=timeout_sec)
        result = ProbeResult(len(articles), sum(bool(a.body) for a in articles))
    except Exception as exc:  # noqa: BLE001 - every type folds into a fixed reason
        reason = classify_failure(exc, datetime.now(timezone.utc)).reason
    if reason is not None:
        raise ProbeFailure(reason)
    return result
