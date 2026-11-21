"""status の data: 停止理由と次の操作 (daemon の API)。"""
from __future__ import annotations

from types import SimpleNamespace

from agentic_fx.ops.contracts import ShellCaller
from tests.ops.test_service_api_wiring import _app
from tests.test_service_app import NOW, _init

NEW_KEYS = {"phase", "next_action", "unprocessed_positions", "resume_last_result",
            "resume_last_result_at"}


def _seed(app, **cols):
    app.outage._ensure_row(NOW)
    sets = ", ".join(f"{k}=?" for k in cols)
    app.outage.conn.execute(f"UPDATE datafeed_outage_state SET {sets} WHERE id=1",
                            tuple(cols.values()))
    app.outage.conn.commit()


def _data(app):
    return app.ops.status(ShellCaller.SHELL)["data"]


def test_status_data_keeps_the_state_and_adds_the_guidance_keys(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        data = _data(app)
        assert data["state"] == "ready"
        assert NEW_KEYS <= set(data)
        assert (data["phase"], data["next_action"]) == ("ready", "none")
        assert data["unprocessed_positions"] == "unknown"  # ready は数えない
        assert data["resume_last_result"] is None and data["resume_last_result_at"] is None
    finally:
        app.close()


def test_a_degraded_row_with_the_awaiting_mark_asks_for_a_resume(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        _seed(app, state="degraded", epoch=5, recovered_notified_epoch=5,
              resume_last_result="rejected_watermark", resume_last_result_at=NOW.isoformat())
        data = _data(app)
        assert data["state"] == "degraded"
        assert (data["phase"], data["next_action"]) == ("resume_rejected", "restore_source")
        assert data["resume_last_result"] == "rejected_watermark"
        assert data["resume_last_result_at"] == NOW.isoformat()
        _seed(app, resume_last_result=None, resume_last_result_at=None)
        data = _data(app)
        assert (data["phase"], data["next_action"]) == ("awaiting_resume", "resume")
        _seed(app, resume_requested_at=NOW.isoformat())
        assert (_data(app)["phase"], _data(app)["next_action"]) == ("resume_requested", "wait_tick")
    finally:
        app.close()


def test_the_card_state_and_the_guidance_come_from_the_same_read(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        _seed(app, state="degraded", epoch=5)
        # 別の読み取りが ready を返しても、data の state は案内と同じ行から作る
        app.outage.state_from = lambda conn: "ready"
        data = _data(app)
        assert data["state"] == "degraded"
        assert (data["phase"], data["next_action"]) == ("stopped", "restore_source")
    finally:
        app.close()


def test_a_guidance_read_failure_is_unknown_and_keeps_the_state_read_once(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    reads = []

    class _Outage:
        def state_from(self, conn):
            reads.append(1)
            return "ready"

    try:
        fn = app.ops._status_provider
        cells = dict(zip(fn.__code__.co_freevars, fn.__closure__))
        original = cells["outage"].cell_contents
        cells["outage"].cell_contents = _Outage()
        try:
            data = app.ops.status(ShellCaller.SHELL)["data"]
        finally:
            cells["outage"].cell_contents = original
        assert len(reads) == 1
        assert data["state"] == "ready"
        assert (data["phase"], data["next_action"]) == ("unknown", "check_status")
        assert data["unprocessed_positions"] == "unknown"
        from agentic_fx.datafeed.outage import unknown_guidance
        assert {k: v for k, v in data.items() if k != "state"} == unknown_guidance()
    finally:
        app.close()


def test_the_guidance_does_not_touch_the_scheduler_connection(tmp_path, monkeypatch):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        _seed(app, state="degraded", epoch=2, recovered_notified_epoch=2)
        expected = _data(app)
        with monkeypatch.context() as scoped:
            scoped.setattr(app.outage, "conn", SimpleNamespace(
                execute=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("outage.conn used"))))
            assert _data(app) == expected
    finally:
        app.close()
