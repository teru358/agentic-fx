"""LocalRunner の ctx 超過検知・HTTP error envelope 解析 (プラン9 Task2)。

設計: docs/superpowers/specs/2026-08-10-context-overflow-diagnosis-design.md
検査点 1〜9 (§7 の表) + §4.1 の型検証要件 (表に無い追加検査)。
"""
import json

import httpx

from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolRegistry

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}},
          "required": ["action"]}

_CTX_ENVELOPE = {"error": {
    "code": 400,
    "message": "request (90010 tokens) exceeds the available context "
               "size (65536 tokens), try increasing it",
    "type": "exceed_context_size_error",
    "n_prompt_tokens": 90010, "n_ctx": 65536}}


def _mission(**over):
    d = dict(prompt="p", tools=[], output_schema=SCHEMA, max_turns=8,
             timeout_sec=30)
    d.update(over)
    return Mission(**d)


def _runner_for_json_body(status: int, body: dict, *,
                          model="qwen3.6-35b-a3b_Q4") -> LocalRunner:
    def handler(request):  # noqa: ANN001
        return httpx.Response(status, json=body)
    return LocalRunner(base_url="http://test/v1", model=model,
                       registry=ToolRegistry(),
                       transport=httpx.MockTransport(handler))


def _runner_for_raw_body(status: int, body: bytes, *,
                         model="m") -> LocalRunner:
    def handler(request):  # noqa: ANN001
        return httpx.Response(status, content=body)
    return LocalRunner(base_url="http://test/v1", model=model,
                       registry=ToolRegistry(),
                       transport=httpx.MockTransport(handler))


# ---- 検査点 1: error.type によるコンテキスト判定 ---------------------------

def test_ctx_overflow_detected_by_error_type():
    """type=exceed_context_size_error なら reason に ctx 専用文言が入る。"""
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert "context exceeded" in r.reason


def test_other_error_type_not_treated_as_ctx_overflow():
    """type がそれ以外なら、n_prompt_tokens/n_ctx 相当の値が同居していても
    ctx 専用文言にしない (「type 判定を無視して常に ctx 文言にする」変異の
    キラー — CP1 の陽性テストだけでは常時 ctx 文言化する変異を見逃す)。"""
    envelope = {"error": {"type": "invalid_request_error",
                          "message": "bad request",
                          "n_prompt_tokens": 90010, "n_ctx": 65536}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "context exceeded" not in r.reason


# ---- 検査点 2〜4: reason の構成要素 -----------------------------------------

def test_ctx_overflow_reason_includes_prompt_tokens():
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert "90010" in r.reason


def test_ctx_overflow_reason_includes_n_ctx():
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert "65536" in r.reason


def test_ctx_overflow_reason_includes_model():
    r = _runner_for_json_body(400, _CTX_ENVELOPE,
                              model="qwen3.6-35b-a3b_Q4").run(_mission())
    assert "qwen3.6-35b-a3b_Q4" in r.reason


# ---- 検査点 5: safe_text による安全化・単一行化・長さ上限 -------------------

def test_reason_is_single_line():
    envelope = {"error": {"type": "other",
                          "message": "line one\nline two\ttabbed"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "\n" not in r.reason
    assert "\t" not in r.reason


def test_reason_collapses_printable_horizontal_whitespace():
    """M5a: isprintable() だけでは除去できない空白を split/join が畳む。"""
    envelope = {"error": {"type": "other", "message": "alpha  beta"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert r.reason == "alpha beta"


def test_reason_strips_non_whitespace_nonprintable_character():
    """M5b: split/join だけでは除去できない NUL を isprintable が落とす。"""
    envelope = {"error": {"type": "other", "message": "alpha\x00beta"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    # ⚠️ `_normalize_reason` は非印字文字を**削除ではなく空白 1 文字へ置換**する
    # (`ch if ch.isprintable() else " ")。3 周目レビューで「削除」を前提にした
    # 期待値 "alphabeta" になっており、**正しい実装に対しても永久に red** だった。
    # 指揮者が実測: _normalize_reason("alpha\x00beta") == "alpha beta"
    assert r.reason == "alpha beta"


def test_reason_length_is_capped():
    envelope = {"error": {"type": "other", "message": "x" * 10_000}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert len(r.reason) <= 550


def test_reason_uses_safe_text_to_strip_urls():
    envelope = {"error": {"type": "other",
                          "message": "upstream call to "
                                     "http://internal.example/v1?apikey=SECRET123 failed"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "SECRET123" not in r.reason
    assert "http://internal.example" not in r.reason


# ---- 検査点 6: status は "failed" のまま ------------------------------------

def test_ctx_overflow_status_is_still_failed():
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert r.status == "failed"


# ---- 検査点 7: 非 400 の同一 envelope --------------------------------------

def test_non_400_status_with_same_envelope_is_also_parsed():
    """413 (Payload Too Large) でも同じ envelope なら診断が拾える —
    status code で分岐してはならない (codex I4)。"""
    r = _runner_for_json_body(413, _CTX_ENVELOPE).run(_mission())
    assert "context exceeded" in r.reason


# ---- 検査点 8: 本文が JSON でない / キー欠落 → 例外を出さず汎用文言 --------

def test_non_json_body_falls_back_to_generic_reason():
    r = _runner_for_raw_body(400, b"not json at all").run(_mission())
    assert r.status == "failed"
    assert "HTTP 400" in r.reason


def test_missing_error_key_falls_back_to_generic_reason():
    """JSON としては妥当だが `error` キーが空 — 例外を出さず汎用文言に
    降格する (CP8 の「キー欠落」側)。"""
    r = _runner_for_json_body(400, {"error": {}}).run(_mission())
    assert "HTTP 400" in r.reason


# ---- 検査点 9: 本文が上限超過 → 汎用文言に退避 ------------------------------

def test_oversized_body_falls_back_to_generic_reason():
    huge = json.dumps({"error": {"type": "exceed_context_size_error",
                                 "message": "x" * 200_000,
                                 "n_prompt_tokens": 1, "n_ctx": 2}}).encode()
    r = _runner_for_raw_body(400, huge).run(_mission())
    assert "HTTP 400" in r.reason
    assert "context exceeded" not in r.reason


# ---- 受入条件 5 の補強: 応答本文そのものは transcript に残らない -----------

def test_response_body_not_stored_in_transcript():
    """⚠️ 旧版の 2 本目は `assert "90010" not in serialized or "90010" in
    (r.reason or "")` という **恒真な assert** だった (2 周目のローカル LLM
    レビューで KAT が検出・指揮者が裏取り)。ctx 超過の `reason` には設計上
    必ず `90010` が入る (`test_ctx_overflow_reason_includes_prompt_tokens`
    がそれを要求している) ため **`or` の右辺が常に真**で、左辺が偽でも
    assert は通る = **構造的に失敗し得ない**。
    なお M15 (`messages` に応答本文を append する変異) 自体は 1 本目の
    `exceed_context_size_error` の assert が殺すので「検出不能」ではないが、
    **1 本目を弱めたり別の envelope に差し替えたりした瞬間に無防備になる**。
    `reason` 側の検査は別テストの担当なので、ここでは transcript だけを
    厳密に見る (1 検査目的 1 テスト)。"""
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    serialized = json.dumps(r.transcript)
    assert "exceed_context_size_error" not in serialized   # エラー種別が漏れない
    assert "90010" not in serialized                       # 本文由来の数値も漏れない


# ---- 追加検査 (spec 21 項目には無いが §4.1 の型検証要件): -----------------
# 「コンテキスト専用文言に使う値は str/bool を除く正整数に型を絞る。
#  不正な型なら汎用の error.message に降格する」

def test_ctx_prompt_tokens_wrong_type_downgrades_to_message():
    envelope = {"error": {"type": "exceed_context_size_error",
                          "message": "context is full, please retry",
                          "n_prompt_tokens": "90010",  # str — 不正
                          "n_ctx": 65536}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "context exceeded" not in r.reason
    assert "context is full, please retry" in r.reason


def test_ctx_n_ctx_bool_downgrades_to_message():
    envelope = {"error": {"type": "exceed_context_size_error",
                          "message": "ctx bool edge case",
                          "n_prompt_tokens": 1,
                          "n_ctx": True}}  # bool — 正整数として扱わない
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "context exceeded" not in r.reason
    assert "ctx bool edge case" in r.reason


# ---- 段 0 の変異スイープで検出した無防備な防御 (指揮者追加) ----------------
# 計画の変異リスト M1〜M15 では拾えなかった 2 つの退避経路。どちらも
# 「例外を出さず汎用文言に退避する」という spec §4.1 の中核要件そのもの
# なのに、フルスイート 1748 全緑のまま防御を削除できてしまった。

def test_connection_error_without_response_falls_back_to_generic_reason():
    """spec §4.1「接続エラー等 response を持たない HTTPError は従来文言の
    まま」。`_reason_from_http_error` の `isinstance(e, HTTPStatusError)`
    ガードを外すと `e.response` に触れて **AttributeError** が送出される
    (実測)。llama-swap 停止・ネットワーク断という最もありふれた障害の経路
    であり、ここで例外が漏れると Mission が診断不能なまま落ちる。"""
    def handler(request):  # noqa: ANN001
        raise httpx.ConnectError("connection refused")

    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=ToolRegistry(),
                         transport=httpx.MockTransport(handler))
    r = runner.run(_mission())
    assert r.status == "failed"
    assert r.reason is not None
    assert "ConnectError" in r.reason


def test_json_body_that_is_not_a_dict_falls_back_to_generic_reason():
    """検査点 8 の穴。本文が **valid JSON だが dict ではない** (配列・
    スカラー) ケース。`isinstance(payload, dict)` ガードを外すと
    `payload.get()` で **AttributeError: 'list' object has no attribute
    'get'** になる (実測)。リバースプロキシや互換層が配列を返す実在の形。"""
    def handler(request):  # noqa: ANN001
        return httpx.Response(400, content=b"[1,2,3]")

    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=ToolRegistry(),
                         transport=httpx.MockTransport(handler))
    r = runner.run(_mission())
    assert r.status == "failed"
    assert r.reason is not None
    assert "HTTPStatusError" in r.reason
