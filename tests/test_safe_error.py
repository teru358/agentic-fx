"""`_safe_error.safe_error_text` の 3 分岐すべてにピンを打つ。

この関数は price_provider / news_collector / econ_calendar の**唯一の
秘密抑止点**なので、分岐ごとに直接テストする (修正ラウンド 1: I-3)。
一本化以前は `httpx.HTTPError` 分岐 (ConnectError 等で本文を一切出さない層)
を検証するテストがリポジトリ全体に無く、この行を
`f"{type(e).__name__}: {e}"` に変える改変が全緑のまま生き残っていた。
`_SECRET_RE` は `apikey=` 形式しか伏字にしないため、その改変で漏れるのは
**URL / ホスト名そのもの** — 伏字パターンでは止まらない。
"""
from __future__ import annotations

import httpx

from agentic_fx._safe_error import safe_error_text

URL = "https://api.example.com/v1/quote?symbol=USDJPY&apikey=SECRET_KEY_123"


def test_http_status_error_keeps_status_and_drops_url():
    req = httpx.Request("GET", URL)
    e = httpx.HTTPStatusError(f"Client error '401 Unauthorized' for url '{URL}'",
                              request=req, response=httpx.Response(401, request=req))
    text = safe_error_text(e)
    assert text == "HTTPStatusError: HTTP 401"   # status は診断に要るので残す
    assert "api.example.com" not in text
    assert "SECRET_KEY_123" not in text


def test_transport_error_yields_type_name_only():
    """ConnectError 等は **型名だけ**。message に URL が載る実装がある。"""
    e = httpx.ConnectError(f"[Errno -2] Name or service not known: {URL}")
    assert isinstance(e, httpx.HTTPError) and not isinstance(
        e, httpx.HTTPStatusError)          # HTTPError 分岐に入ることの確認
    text = safe_error_text(e)
    assert text == "ConnectError"
    assert "api.example.com" not in text
    assert "SECRET_KEY_123" not in text
    assert "Errno" not in text


def test_unsupported_protocol_yields_type_name_only():
    """URL を message に含む別の HTTPError サブクラスでも同じ。"""
    e = httpx.UnsupportedProtocol(
        f"Request URL is missing an 'http://' or 'https://' protocol: {URL}")
    text = safe_error_text(e)
    assert text == "UnsupportedProtocol"
    assert "example.com" not in text


def test_other_exceptions_keep_their_message():
    """自前の例外は URL を含まないので情報量を落とさない。"""
    assert safe_error_text(ValueError("no usable country")) == \
        "ValueError: no usable country"


def test_secret_query_params_are_masked_in_any_branch():
    """URL 抑止を擦り抜けた経路でも apikey= 形式は伏字にする (多層防御)。"""
    text = safe_error_text(RuntimeError(f"failed calling {URL}"))
    assert "SECRET_KEY_123" not in text
    assert "apikey=***" in text
    # token= / secret= / api-key= も同じ扱い
    assert "***" in safe_error_text(RuntimeError("auth token=abcdef"))
    assert "***" in safe_error_text(RuntimeError("x api-key=abcdef"))
