"""webhook URL と HTTP 結果の、network library に依らない境界。"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


class DeliveryError(Exception):
    """配送設定又は HTTP 結果の固定分類。"""

    def __init__(self, reason: str, *, retryable: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        # 受け手が指示した待ち (秒)。読めた 429 の Retry-After だけが持つ
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class WebhookUrl:
    url: str
    host: str
    port: int


def validate_webhook_url(raw: bytes) -> WebhookUrl:
    """secret file の bytes を一度だけ安全な HTTPS 宛先へ解釈する。"""
    try:
        value = raw.decode("utf-8")
        parsed = urlsplit(value)
        port = parsed.port
    except (AttributeError, UnicodeDecodeError, ValueError):
        raise DeliveryError("url_invalid") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port not in (None, 443)):
        raise DeliveryError("url_invalid")
    return WebhookUrl(url=value, host=parsed.hostname, port=443)


def encode_json_object(payload: object) -> bytes:
    """HTTP body は object だけを JSON として送る。"""
    if type(payload) is not dict:
        raise DeliveryError("payload_invalid")
    try:
        return json.dumps(payload, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError):
        raise DeliveryError("payload_invalid") from None


def request_headers(body: bytes) -> Mapping[str, str]:
    return MappingProxyType({"Content-Type": "application/json",
                             "Content-Length": str(len(body))})


def parse_retry_after(value: object, now: datetime | None = None) -> float | None:
    """Retry-After (秒、又は HTTP-date) を待ち秒数にする。読めなければ None、過去は 0。"""
    if type(value) is not str:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (datetime.now(timezone.utc) if now is None else now)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def classify_status(status: object, retry_after: object = None, *,
                    now: datetime | None = None) -> DeliveryError | None:
    """2xx 以外を retry 規則用の固定語へ分類する。retry_after は 429 の header 値だけが使う。"""
    if type(status) is not int:
        return DeliveryError("response_invalid")
    if 200 <= status < 300:
        return None
    if 300 <= status < 400:
        return DeliveryError("redirect")
    if status == 429:
        return DeliveryError("http_429", retryable=True,
                             retry_after=parse_retry_after(retry_after, now))
    if 400 <= status < 500:
        return DeliveryError("http_4xx")
    if 500 <= status < 600:
        return DeliveryError("http_5xx", retryable=True)
    return DeliveryError("response_invalid")
