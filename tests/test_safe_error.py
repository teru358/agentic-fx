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

from agentic_fx._safe_error import safe_error_text, safe_text

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
    """URL 抑止を擦り抜けた経路でも apikey= 形式は伏字にする (多層防御)。

    codex C-I3 での変更: 以前はここで `RuntimeError(f"failed calling {URL}")`
    を渡して `apikey=***` を期待していたが、URL 置換層の追加で URL 全体が
    `<url>` になるため、その期待は**より強い保証に置き換わった** (欠陥を
    固定していたテストではない)。URL を含まない形の秘密 —
    ログの手組み文字列やヘッダ表示など、スキーム付き URL の外で
    `token=` が現れる経路 — は URL 層では止まらないので、この層は残す。
    """
    assert "***" in safe_error_text(RuntimeError("auth token=abcdef"))
    assert "abcdef" not in safe_error_text(RuntimeError("auth token=abcdef"))
    assert "***" in safe_error_text(RuntimeError("x api-key=abcdef"))
    assert "***" in safe_error_text(RuntimeError("x apikey=abcdef"))
    assert "***" in safe_error_text(RuntimeError("x secret=abcdef"))


# --- codex C-I3: 非 httpx 例外の URL 素通しを塞ぐ ------------------------

def test_url_is_redacted_in_non_httpx_exception():
    """docstring の契約 (「URL を出さない」) は else 分岐で破れていた。

    else 分岐は `f"{type(e).__name__}: {e}"` なので、httpx 例外を自前例外に
    ラップした経路 (`raise DataUnhealthy(f"... {e}")` や
    `RuntimeError(str(httpx_err))`) では **ホスト名・パス・クエリのキー名**が
    そのまま残る。`_SECRET_RE` は `apikey=<値>` の値しか伏せないため、
    ホストもパスも止まらない。Phase 3 で broker が MT5 bridge (httpx) に
    なると、この経路が activity / Discord 通知に URL を載せる。
    """
    text = safe_error_text(RuntimeError("failed https://host.example/p?apikey=x"))
    assert "host.example" not in text     # ホストが残らない
    assert "/p" not in text               # パスが残らない
    assert "apikey" not in text           # クエリのキー名も残らない
    assert text.startswith("RuntimeError: ")   # 型名は診断に要るので残す
    assert "failed" in text                    # 非 URL 部分の情報も残す
    assert "<url>" in text


def test_url_redaction_applies_to_every_branch_and_keeps_surrounding_text():
    """置換は全分岐の**出口**で効く (else 分岐だけの対症療法にしない)。"""
    # 自前例外 (DataUnhealthy 相当) — 前後の文脈は残る
    text = safe_error_text(ValueError(f"quote fetch failed: {URL} (retry 2)"))
    assert "api.example.com" not in text
    assert "SECRET_KEY_123" not in text
    assert text == "ValueError: quote fetch failed: <url> (retry 2)"
    # http:// も同じ
    assert safe_error_text(RuntimeError("x http://h/p y")) == \
        "RuntimeError: x <url> y"
    # 引用符で囲まれた URL は引用符を残す (可読性を落とさない)
    assert safe_error_text(RuntimeError("for url 'https://h/p'")) == \
        "RuntimeError: for url '<url>'"


def test_url_redaction_does_not_touch_non_url_text():
    """URL 以外の文字列は落とさない (診断情報を失わない)。"""
    assert safe_error_text(ValueError("no usable country: JP")) == \
        "ValueError: no usable country: JP"


def test_safe_text_redacts_urls_and_secrets_in_plain_strings():
    """例外でない文字列にも同じ抑止をかけられること。

    外部由来のテキストは例外だけではない — broker が返す
    `BrokerResult.message` は scheduler が activity にそのまま書いており
    (`reconcile_pending`)、Phase 3 の MT5 bridge がエラー本文に自分の
    エンドポイントを載せればそこから漏れる。抑止層を 1 箇所に保つため、
    文字列版を公開する (safe_error_text はこれを内部で使う)。"""
    assert safe_text(f"upstream said: {URL}") == "upstream said: <url>"
    assert safe_text("plain message") == "plain message"
    assert "***" in safe_text("token=abcdef")
