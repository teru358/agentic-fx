"""MCP spool の境界値と異常系を実 file system で固定する。

`test_mcp_spool.py` の観測を補い、上限のちょうど境界、group に開いた mode、
id 形式でない `req-`、handler の例外、stop と rename の起床を見る。
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from agentic_fx.tools import mcp_spool
from agentic_fx.tools import mcp_spool_client as client
from tests.tools.test_mcp_spool import (  # noqa: F401 — fixture の取り込み
    _BASE, _call_req, _events, _make, _place, _read_resp, _wait,
    _wait_rejected, dispatchers,
)


def _executed(d, rid) -> bool:
    return rid in d.ledger.tool_executed_ids()


# --- 要求 file の mode ---------------------------------------------------

def test_group_readable_request_is_rejected(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    rid = _place(d.spool_dir, json.dumps(_call_req()).encode(), mode=0o640)
    assert _wait_rejected(d, rid) == "not_private_mode"
    assert calls == []


# --- JSON の上限の境界 ---------------------------------------------------

def _with_pad(pad: str) -> bytes:
    return (_BASE + ', "pad": ' + pad + "}").encode()


def test_json_depth_limit_boundary(tmp_path, dispatchers):
    """最上位の object を深さ 1 と数え、`pad` の list の入れ子 k 段の最深は
    k + 1。上限ちょうどは実行し、1 段超えると拒否する。"""
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    k_ok = mcp_spool.MAX_JSON_DEPTH - 1
    ok = _place(d.spool_dir, _with_pad("[" * k_ok + "]" * k_ok))
    _wait(lambda: _executed(d, ok), what="request at the depth limit")
    k_over = mcp_spool.MAX_JSON_DEPTH
    over = _place(d.spool_dir, _with_pad("[" * k_over + "]" * k_over))
    assert _wait_rejected(d, over) == "json_too_deep"
    assert calls == [1]


def test_json_key_limit_boundary(tmp_path, dispatchers):
    """key は要求全体で数える (`_BASE` の 7 個 + `pad` の 1 個 + pad の中)。
    上限ちょうどは実行し、1 個超えると拒否する。"""
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    n_ok = mcp_spool.MAX_JSON_KEYS - 8

    def pad(n: int) -> str:
        return "{" + ", ".join(f'"k{i}": 0' for i in range(n)) + "}"

    ok = _place(d.spool_dir, _with_pad(pad(n_ok)))
    _wait(lambda: _executed(d, ok), what="request at the key limit")
    over = _place(d.spool_dir, _with_pad(pad(n_ok + 1)))
    assert _wait_rejected(d, over) == "json_too_many_keys"
    assert calls == [1]


def test_json_nesting_beyond_decoder_recursion_is_rejected(tmp_path, dispatchers):
    """decoder 自身が RecursionError を出す深さ (10 万段) でも拒否として
    扱い、dispatcher は次の要求を処理する。"""
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    deep = _place(d.spool_dir, _with_pad("[" * 100_000 + "]" * 100_000))
    assert _wait_rejected(d, deep) == "json_too_deep"
    nxt = _place(d.spool_dir, json.dumps(_call_req(x=8)).encode())
    _wait(lambda: _executed(d, nxt), what="request after the deep one")
    assert calls == [8]


def test_control_character_in_json_string_is_rejected(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    rid = _place(d.spool_dir, _with_pad('"a\x01b"'))
    assert _wait_rejected(d, rid) == "not_json"
    assert calls == []


# --- spool root の nonce ---------------------------------------------------

@pytest.mark.parametrize("nonce", ["a" * 31, "A" * 32, "g" * 32])
def test_spool_root_rejects_short_or_non_hex_nonce(tmp_path, nonce):
    """workdir が在っても、128 bit の小文字 hex でない nonce では作らない。"""
    with pytest.raises(mcp_spool.SpoolCreateFailed, match="spool_create_failed"):
        mcp_spool.SpoolRoot.create(tmp_path, nonce)
    assert not (tmp_path / "afx-spool" / nonce).exists()


# --- scan の対象 -----------------------------------------------------------

def test_request_name_that_is_not_a_request_id_is_not_processed(
        tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    bad_name = "req-" + "g" * client.ID_HEX_LEN
    tmp = d.spool_dir / ("tmp-" + "g" * client.ID_HEX_LEN)
    tmp.write_bytes(json.dumps(_call_req(x=99)).encode())
    tmp.chmod(0o600)
    os.rename(tmp, d.spool_dir / bad_name)
    good = _place(d.spool_dir, json.dumps(_call_req(x=1)).encode())
    _wait(lambda: _executed(d, good), what="valid request")
    time.sleep(1.5)  # 周期 rescan を 1 回以上通す
    assert (d.spool_dir / bad_name).exists()
    assert calls == [1]


# --- handler の例外 --------------------------------------------------------

def test_handler_exception_before_response_returns_internal_error(
        tmp_path, dispatchers):
    """`params` が object でない tools/call は handler の中で例外になる。
    shim が待ち続けないよう -32603 を返し、次の要求も処理する。"""
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    rid = _place(d.spool_dir, json.dumps({
        "jsonrpc": "2.0", "id": 41, "method": "tools/call",
        "params": [1]}).encode())
    _wait(lambda: (d.spool_dir / ("resp-" + rid)).exists(), what="error response")
    resp = _read_resp(d.spool_dir, rid)
    assert resp["id"] == 41
    assert resp["error"]["code"] == -32603
    assert [e.detail for e in _events(d, rid, "handler_error")] == ["AttributeError"]
    nxt = _place(d.spool_dir, json.dumps(_call_req(x=2)).encode())
    _wait(lambda: _executed(d, nxt), what="request after handler error")
    assert calls == [2]


def test_exception_after_response_keeps_the_single_response(
        tmp_path, dispatchers):
    """応答を publish した後に handler が例外を出しても (after_send の失敗)、
    応答は最初の 1 回のまま、dispatcher は次の要求を処理する。"""
    calls: list = []

    def failing_after_send() -> None:
        raise RuntimeError("after_send failed")

    d = _make(dispatchers, tmp_path, calls=calls, after_send=failing_after_send)
    rid = _place(d.spool_dir, json.dumps(_call_req(rpc_id=5, x=3)).encode())
    _wait(lambda: _events(d, rid, "handler_error"), what="handler error")
    resp = _read_resp(d.spool_dir, rid)
    assert resp["id"] == 5 and "result" in resp
    assert _events(d, rid, "response_write_failed") == []
    assert not (d.spool_dir / ("rtmp-" + rid)).exists()
    nxt = _place(d.spool_dir, json.dumps(_call_req(x=4)).encode())
    _wait(lambda: _executed(d, nxt), what="request after after_send failure")
    _wait(lambda: (d.spool_dir / ("resp-" + nxt)).exists(), what="next response")
    assert calls == [3, 4]


# --- 起床 ------------------------------------------------------------------

def test_stop_wakes_the_dispatcher_without_waiting_for_rescan(
        tmp_path, dispatchers):
    d = _make(dispatchers, tmp_path, rescan_interval_sec=30)
    time.sleep(0.2)  # 起動時の scan を済ませて待ちに入れる
    started = time.monotonic()
    assert d.stop(timeout_sec=2) is True
    assert time.monotonic() - started < 2
    assert not d.thread.is_alive()


def test_rename_into_spool_wakes_the_dispatcher(tmp_path, dispatchers):
    """shim の publish は `tmp-` の作成と `req-` への rename の 2 段。
    rename の event だけで起床し、周期 rescan (ここでは 30 秒) を待たない。"""
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls, rescan_interval_sec=30)
    time.sleep(0.2)
    rid = client.new_request_id()
    dirfd = os.open(d.spool_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("tmp-" + rid, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     0o600, dir_fd=dirfd)
        os.write(fd, json.dumps(_call_req(x=6)).encode())
        os.close(fd)
        time.sleep(0.3)  # tmp- の作成で起きた scan を済ませる
        client.renameat2_noreplace(dirfd, "tmp-" + rid, "req-" + rid)
    finally:
        os.close(dirfd)
    _wait(lambda: _executed(d, rid), timeout=3, what="wake by rename")
    assert calls == [6]


# --- client の応答待ち ------------------------------------------------------

def test_client_call_times_out_without_a_dispatcher(tmp_path):
    root = mcp_spool.SpoolRoot.create(tmp_path, client.new_request_id())
    outcome: list = []

    def run() -> None:
        try:
            client.call(root.path, _call_req(), timeout_sec=0.3)
            outcome.append("returned")
        except TimeoutError:
            outcome.append("timeout")

    t = threading.Thread(target=run, daemon=True)
    try:
        t.start()
        t.join(3)
        assert outcome == ["timeout"]
    finally:
        root.close()
