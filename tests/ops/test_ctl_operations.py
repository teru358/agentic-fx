"""`afx ctl` の各操作を実 UDS + 実 server で通す (端末確認の要らないもの)。"""
from __future__ import annotations

import concurrent.futures
import re

import pytest

from agentic_fx.ops import client, keys
from agentic_fx.store.instance_lock import acquire_instance_lock

from ._api import closed_order, make_api
from ._ctl import FakeServer, ctl_api, run_ctl  # noqa: F401


def test_status_is_formatted_like_the_shell(ctl_api):
    rc, out, err = run_ctl(ctl_api.env.root, ["status"])
    assert rc == 0, err
    assert out.splitlines()[0] == "mode: learning / autopilot: off / kill switch: ok"


def test_whoami_as_approver(ctl_api):
    rc, out, _ = run_ctl(ctl_api.env.root, ["--as", "approver", "whoami"])
    assert rc == 0
    assert "principal: approver" in out and "local_guard" in out


def test_activity_lines_are_sanitized(ctl_api):
    env = ctl_api.env
    env.activity_path.parent.mkdir(parents=True, exist_ok=True)
    env.activity_path.write_text("2026-10-05T00:00:00Z\tSYSTEM\tx\t\x1b[31mred\x1b[0m\n")
    rc, out, _ = run_ctl(env.root, ["activity", "5"])
    assert rc == 0
    assert "\x1b" not in out and "red" in out


def test_approval_list_and_detail_for_operator(ctl_api):
    env = ctl_api.env
    approval_id = env.make_plugin_approval()
    rc, out, _ = run_ctl(env.root, ["approval", "list"])
    assert rc == 0
    assert out.startswith(f"#{approval_id} kind=plugin name=sma ")
    rc, out, _ = run_ctl(env.root, ["approvals", "show", str(approval_id)])
    assert rc == 0
    assert f"approval #{approval_id} kind=plugin status=pending" in out
    assert "payload_sha256=" in out


def test_ask_polls_the_job_and_prints_the_answer(ctl_api):
    rc, out, err = run_ctl(ctl_api.env.root, ["ask", "今の相場は?"])
    assert rc == 0, err
    assert out.splitlines()[-1] == "answer"
    job_id = re.search(r"job ([0-9a-f]{32}) を受理しました", out).group(1)
    rc, out, _ = run_ctl(ctl_api.env.root, ["jobs", "get", job_id])
    assert rc == 0
    assert f"job {job_id} kind=ask state=done" in out


def test_ctrl_c_while_polling_leaves_the_accepted_job_running(ops_env):
    pending = concurrent.futures.Future()
    api_env = make_api(ops_env, service_pid=0,
                       service_kwargs={"ask_submitter": lambda question: pending})
    lock = acquire_instance_lock(ops_env.root / "data")

    def interrupt(_seconds):
        raise KeyboardInterrupt

    try:
        rc, out, _ = run_ctl(ops_env.root, ["ask", "q"], sleep=interrupt)
        assert rc == 130
        assert "受理済みで、この client を離れても続きます" in out
        job_id = re.search(r"jobs get ([0-9a-f]{32})", out).group(1)
        assert api_env.service.get_job(client.Principal.OPERATOR, job_id)["state"] == "running"
    finally:
        pending.set_result("late")
        api_env.server.stop(5.0)
        lock.close()


@pytest.mark.parametrize("state,hint", [("queued", True), ("running", False)])
def test_interrupt_notice_offers_cancel_only_while_queued(state, hint):
    text = client.interrupt_notice("a" * 32, state)
    assert ("jobs cancel" in text) is hint
    assert "jobs get" in text


def test_improve_runs_and_reports_the_mission(ctl_api):
    rc, out, err = run_ctl(ctl_api.env.root, ["improve"])
    assert rc == 0, err
    assert out.splitlines()[-1] == "improve mission #41 を起動しました"


def test_backlog_add_and_transition(ctl_api):
    env = ctl_api.env
    rc, out, _ = run_ctl(env.root, ["backlog", "add", "RSI の閾値を見直す"])
    assert rc == 0
    bid = int(re.search(r"backlog #(\d+) を追加しました: 「RSI の閾値を見直す」", out).group(1))
    rc, out, _ = run_ctl(env.root, ["backlog", "reject", str(bid)])
    assert (rc, out.strip()) == (0, f"backlog #{bid} を rejected にしました")
    rc, out, _ = run_ctl(env.root, ["backlog", "reopen", str(bid)])
    assert (rc, out.strip()) == (0, f"backlog #{bid} を open に戻しました")


def test_backlog_list_uses_updated_at_then_id_and_shows_last_result(ctl_api):
    env = ctl_api.env
    env.conn.execute(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm,last_result) "
        "VALUES ('older','user','open','2026-01-01T00:00:00+00:00',"
        "'2026-01-02T00:00:00+00:00','older',NULL)")
    env.conn.execute(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm,last_result) "
        "VALUES ('newer','system','note','2026-01-01T00:00:00+00:00',"
        "'2026-01-03T00:00:00+00:00','newer','result')")
    env.conn.commit()
    rc, out, err = run_ctl(env.root, ["backlog", "list", "--limit", "1"])
    assert rc == 0, err
    assert out.splitlines() == ["#2 note system 2026-01-03T00:00:00+00:00 newer", "  result"]


def test_backlog_list_empty_and_limit_boundaries(ctl_api):
    env = ctl_api.env
    assert run_ctl(env.root, ["backlog", "list"])[0:2] == (0, "backlog: 0 件\n")
    for limit in ("499", "500"):
        assert run_ctl(env.root, ["backlog", "list", "--limit", limit])[0] == 0
    rc, out, err = run_ctl(env.root, ["backlog", "list", "--limit", "501"])
    assert (rc, out) == (2, "") and "usage: afx ctl" in err


def test_policy_add_writes_the_directive(ctl_api):
    env = ctl_api.env
    rc, out, _ = run_ctl(env.root, ["policy", "add", "週末は建玉を持ち越さない"])
    assert (rc, out.strip()) == (0, "policy に追記しました")
    assert "週末は建玉を持ち越さない" in env.policy_path.read_text()


def test_reflect_retry_sends_the_seen_attempts(ctl_api):
    env = ctl_api.env
    order_id = closed_order(env)
    rc, out, err = run_ctl(env.root, ["reflect", "retry", str(order_id)])
    assert rc == 0, err
    assert out.strip() == f"order #{order_id} を reflection 再試行対象へ戻しました (台帳に試行記録なし)"


def test_events_resume_from_the_saved_cursor(ctl_api):
    env = ctl_api.env
    assert run_ctl(env.root, ["policy", "add", "a"])[0] == 0
    rc, out, _ = run_ctl(env.root, ["events"])
    assert rc == 0
    first = [line for line in out.splitlines() if line.startswith("#")]
    assert first and "ops_request_terminal" in first[0]
    rc, out, _ = run_ctl(env.root, ["events"])
    assert out.strip() == "(新しい event はありません)"
    assert run_ctl(env.root, ["policy", "add", "b"])[0] == 0
    rc, out, _ = run_ctl(env.root, ["events"])
    later = [line for line in out.splitlines() if line.startswith("#")]
    assert later and set(later).isdisjoint(first)
    rc, out, _ = run_ctl(env.root, ["events", "--after", "0"])
    assert [line for line in out.splitlines() if line.startswith("#")][:len(first)] == first


def test_lost_response_is_resent_once_with_the_same_key(ops_env, tmp_path):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    server = FakeServer(ops_env.root / "data" / "run" / "api.sock", tmp_path / "rec",
                        hold_lock_dir=ops_env.root / "data")
    try:
        rc, _, err = run_ctl(ops_env.root, ["policy", "add", "x"])
        received = server.connections(expected=2)
    finally:
        server.stop()
    assert rc == 3 and "応答を受け取れませんでした" in err
    assert len(received) == 2
    keys_sent = [re.search(rb"Idempotency-Key: ([0-9a-f]+)", data, re.I).group(1)
                 for data in received]
    assert keys_sent[0] == keys_sent[1]


def test_lost_response_without_key_is_not_resent(ops_env, tmp_path):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    server = FakeServer(ops_env.root / "data" / "run" / "api.sock", tmp_path / "rec",
                        hold_lock_dir=ops_env.root / "data")
    try:
        rc, _, _ = run_ctl(ops_env.root, ["backlog", "note", "1"])
        received = server.connections(expected=2, timeout=1.0)
    finally:
        server.stop()
    assert rc == 3
    assert len(received) == 1


def test_events_high_watermark_going_back_between_runs_keeps_the_cursor(ctl_api):
    env = ctl_api.env
    assert run_ctl(env.root, ["policy", "add", "a"])[0] == 0
    assert run_ctl(env.root, ["policy", "add", "b"])[0] == 0
    rc, out, _ = run_ctl(env.root, ["events"])
    assert rc == 0 and any(line.startswith("#") for line in out.splitlines())
    path = client._events_cursor_path(env.root)
    saved = client._load_cursor(path)
    assert saved > 0
    # DB を巻き戻した形: 前回の起動で見た high watermark より小さくなる。
    conn = env.connect()
    try:
        conn.execute("DELETE FROM ops_events")
        conn.commit()
    finally:
        conn.close()
    for _ in range(2):
        rc, out, err = run_ctl(env.root, ["events"])
        assert rc == 1
        assert "high watermark が後退しました" in err
        assert client._load_cursor(path) == saved


def test_events_cursor_file_with_position_only_is_read_as_the_position(ctl_api):
    env = ctl_api.env
    assert run_ctl(env.root, ["policy", "add", "a"])[0] == 0
    assert run_ctl(env.root, ["policy", "add", "b"])[0] == 0
    rc, out, _ = run_ctl(env.root, ["events", "--after", "0"])
    ids = [int(line[1:].split()[0]) for line in out.splitlines() if line.startswith("#")]
    assert len(ids) >= 2
    path = client._events_cursor_path(env.root)
    # --after の閲覧は cursor file を作らないので、ここで dir を用意する。
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(f"{ids[0]}\n", encoding="ascii")
    rc, out, _ = run_ctl(env.root, ["events"])
    assert rc == 0
    shown = [int(line[1:].split()[0]) for line in out.splitlines() if line.startswith("#")]
    assert shown and min(shown) == ids[1]


# ---------------------------------------------------------------- 引数の範囲

_OUT_OF_RANGE = [["log", "0"], ["log", "501"], ["activity", "501"],
                 ["activity", "1", "bogus"], ["approval", "list", "0"],
                 ["approval", "list", "201"], ["events", "--limit", "0"],
                 ["events", "--limit", "501"]]


@pytest.mark.parametrize("argv", _OUT_OF_RANGE, ids=lambda a: " ".join(a))
def test_out_of_range_argument_is_usage_before_any_key_or_connection(ops_env, tmp_path, argv):
    server = FakeServer(ops_env.root / "data" / "run" / "api.sock", tmp_path / "rec",
                        hold_lock_dir=ops_env.root / "data")
    try:
        rc, _, err = run_ctl(ops_env.root, argv)
        received = server.connections(expected=1, timeout=0.5)
    finally:
        server.stop()
    assert rc == 2
    assert "usage: afx ctl" in err
    assert received == []
    assert not keys.key_dir(ops_env.root).exists()


@pytest.mark.parametrize("argv, expected", [
    (["log", "1"], b"n=1"), (["log", "500"], b"n=500"),
    (["approval", "list", "200"], b"limit=200"),
    (["approval", "list", "150"], b"limit=150"),
    (["events", "--limit", "500"], b"limit=500"),
], ids=lambda v: " ".join(v) if isinstance(v, list) else None)
def test_boundary_argument_reaches_the_server_unchanged(ops_env, tmp_path, argv, expected):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    server = FakeServer(ops_env.root / "data" / "run" / "api.sock", tmp_path / "rec",
                        hold_lock_dir=ops_env.root / "data")
    try:
        run_ctl(ops_env.root, argv)
        received = server.connections(expected=1)
    finally:
        server.stop()
    request_line = received[0].split(b"\r\n", 1)[0]
    assert re.search(rb"[?&]" + expected + rb"(&| HTTP)", request_line), request_line


def _serve_accept_then_drop(sock_path, job_id):
    """1 本目の要求には受理を返し、2 本目 (job の GET) は応答せずに切る UDS server。"""
    import json
    import socket
    import threading

    sock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    sock_path.chmod(0o600)
    listener.listen(4)
    body = json.dumps({"ok": True, "data": {"job_id": job_id, "state": "queued"}}).encode()
    requests = []

    def serve():
        for index in range(2):
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                requests.append(data.split(b"\r\n", 1)[0])
                if index == 0:
                    conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: " + str(len(body)).encode()
                                 + b"\r\nConnection: close\r\n\r\n" + body)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread, requests


def test_connection_lost_while_polling_names_the_accepted_job(ops_env):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    job_id = "b" * 32
    lock = acquire_instance_lock(ops_env.root / "data")
    listener, thread, requests = _serve_accept_then_drop(
        ops_env.root / "data" / "run" / "api.sock", job_id)
    try:
        rc, out, err = run_ctl(ops_env.root, ["ask", "q"])
        thread.join(5)
    finally:
        listener.close()
        lock.close()
    assert [line.split()[0] for line in requests] == [b"POST", b"GET"]
    assert rc == 3
    assert "応答を受け取れませんでした" in err
    assert f"afx ctl jobs get {job_id}" in out
