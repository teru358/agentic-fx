import threading
from unittest.mock import MagicMock

from agentic_fx.service import App


def _app():
    values = dict(conn_core=MagicMock(), conn_shell=MagicMock(), settings=None,
                  state=None, activity=None, broker=None, executor=None,
                  provider=None, econ=None, collector=None, rag=MagicMock(),
                  trade_loop=None, reflection=None, scheduler=None, commands=None,
                  registry=None, inventory_result=None, approved_plugins=None,
                  core_lock=None, mission_watch=None, notifier=None,
                  runner=MagicMock(), owns_runner=True, clock=None,
                  instance_lock=MagicMock(), supervisor=None,
                  improve_supervisor=None, conn_supervisor=MagicMock())
    return App(**values)


def test_close_skips_connections_still_in_use():
    app = _app()
    skipped = app.close(busy_resources=frozenset({"conn_core", "conn_supervisor"}))
    assert skipped == ["conn_supervisor", "conn_core"]
    app.conn_supervisor.close.assert_not_called()
    app.conn_core.close.assert_not_called()
    app.conn_shell.close.assert_called_once()
    app.instance_lock.close.assert_called_once()


def test_close_failure_isolated_per_resource():
    app = _app()
    app.rag.close.side_effect = OSError("broken")
    assert app.close() == []
    app.conn_supervisor.close.assert_called_once()
    app.conn_core.close.assert_called_once()
    app.conn_shell.close.assert_called_once()
    app.instance_lock.close.assert_called_once()


def test_close_releases_instance_lock():
    app = _app()
    app.close()
    app.instance_lock.close.assert_called_once()
