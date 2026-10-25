from agentic_fx.setup.pending_facts import collect_pending_facts
from agentic_fx.store import plugin_switch_journal
from agentic_fx.store.db import connect_readonly
from tests.test_service_app import NOW, _init
from tests.ops.test_service_api_wiring import _app


def test_collection_accepts_none_settings_and_connection_without_sql(tmp_path):
    class Clock:
        def now(self):
            return NOW

    facts = collect_pending_facts(tmp_path, None, None, deployed_plugins=(), outage=None, clock=Clock(),
                                  service_active=None)
    assert facts.settings is None
    assert facts.history_counts == {}
    assert facts.required_env_keys == ()
    assert facts.env_present_keys == frozenset()
    assert facts.approval_ids == ()
    assert facts.service_active is None


def test_collection_preserves_explicit_service_active_values(tmp_path):
    class Clock:
        def now(self):
            return NOW

    for value in (True, False, None):
        facts = collect_pending_facts(tmp_path, None, None, deployed_plugins=(), outage=None, clock=Clock(),
                                      service_active=value)
        assert facts.service_active is value


def test_collection_limits_history_pairs_and_journal_ids(tmp_path, caplog, monkeypatch):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        app.settings.backtest.eval_source = "mt5"
        app.settings.pairs = [f"P{number:03d}" for number in range(50)]
        for approval_id in range(100):
            plugin_switch_journal.insert(
                app.conn_shell, kind="approve", approval_id=approval_id, name=f"n{approval_id}",
                old_kind="absent", old_target=None, temp_path="t", new_target="n",
                switch_required=True, actor="test", now=NOW)
        import logging
        monkeypatch.setattr(logging.getLogger("agentic_fx"), "propagate", True)
        caplog.set_level("INFO", logger="agentic_fx.setup.pending_facts")
        conn = connect_readonly(tmp_path / "data" / "agentic.db")
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            facts = collect_pending_facts(
                tmp_path, app.settings, conn, deployed_plugins=app.approved_plugins,
                outage=app.outage, clock=app.clock)
        finally:
            conn.close()
        history_queries = [sql for sql in statements if "ohlcv_history" in sql]
        assert len(history_queries) == 32
        assert len(facts.history_counts) == 32
        assert caplog.text.count("stalled approvals exceed") == 1
        assert facts.approval_ids == tuple(range(99, 67, -1))
    finally:
        app.close()


def test_supplied_landlock_availability_is_used_without_probing(tmp_path, monkeypatch):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        def probe():
            raise AssertionError("must not probe")
        monkeypatch.setattr("agentic_fx.setup.pending_facts.landlock.is_available", probe)
        conn = connect_readonly(tmp_path / "data" / "agentic.db")
        try:
            facts = collect_pending_facts(
                tmp_path, app.settings, conn, deployed_plugins=app.approved_plugins,
                outage=app.outage, clock=app.clock, landlock_available=False)
        finally:
            conn.close()
        assert facts.landlock_available is False
    finally:
        app.close()


def test_a_failing_landlock_probe_is_unknown_not_unavailable(tmp_path, monkeypatch):
    from agentic_fx.setup import pending_facts as module
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        def boom():
            raise OSError("no libc")
        monkeypatch.setattr(module.landlock, "is_available", boom)
        conn = connect_readonly(tmp_path / "data" / "agentic.db")
        try:
            facts = collect_pending_facts(tmp_path, app.settings, conn, deployed_plugins=(),
                                          outage=app.outage, clock=app.clock)
        finally:
            conn.close()
        assert facts.landlock_available is None
    finally:
        app.close()


def test_env_key_presence_combines_dotenv_and_process_environment(tmp_path, monkeypatch):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        app.settings.datafeed.primary = "mt5"
        monkeypatch.delenv("MT5_BRIDGE_API_KEY", raising=False)
        (tmp_path / ".env").write_text("MT5_BRIDGE_API_KEY=not-read\n", encoding="utf-8")
        conn = connect_readonly(tmp_path / "data" / "agentic.db")
        try:
            facts = collect_pending_facts(tmp_path, app.settings, conn, deployed_plugins=(),
                                          outage=app.outage, clock=app.clock)
        finally:
            conn.close()
        assert facts.env_present_keys == frozenset({"MT5_BRIDGE_API_KEY"})

        (tmp_path / ".env").unlink()
        conn = connect_readonly(tmp_path / "data" / "agentic.db")
        try:
            facts = collect_pending_facts(tmp_path, app.settings, conn, deployed_plugins=(),
                                          outage=app.outage, clock=app.clock)
        finally:
            conn.close()
        assert facts.env_present_keys == frozenset()

        monkeypatch.setenv("MT5_BRIDGE_API_KEY", "present")
        conn = connect_readonly(tmp_path / "data" / "agentic.db")
        try:
            facts = collect_pending_facts(tmp_path, app.settings, conn, deployed_plugins=(),
                                          outage=app.outage, clock=app.clock)
        finally:
            conn.close()
        assert facts.env_present_keys == frozenset({"MT5_BRIDGE_API_KEY"})
    finally:
        app.close()
