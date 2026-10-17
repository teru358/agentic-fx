from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentic_fx.plugin import deploy_deps
from agentic_fx.plugin.loader import IndicatorRef, PluginMeta
from agentic_fx.plugin.resolve import ApprovedInventory
from agentic_fx.store import db as db_store
from tests.plugin.conftest import SETTINGS


@dataclass(frozen=True)
class _Result:
    inventory: ApprovedInventory
    phase1_metas: tuple[PluginMeta, ...]


def _meta(name: str, kind: str, *, indicators=()) -> PluginMeta:
    return PluginMeta(name=name, kind=kind, path=Path("/plugins") / name,
                      params={}, timeframe="1h" if kind == "strategy" else None,
                      pairs=("USDJPY",) if kind == "strategy" else (), max_bars=10,
                      content_hash=f"{name}-content", artifact_hash=f"{name}-artifact",
                      indicators=indicators, outputs=("value",) if kind == "indicator" else None)


def test_enumerate_dependents_reports_strategy_broken_when_indicator_removed(
        monkeypatch, tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    indicator = _meta("a", "indicator")
    strategy = _meta("s", "strategy", indicators=(
        IndicatorRef(alias="fast", plugin="a", params={}, pin="a-content"),))
    result = _Result(ApprovedInventory(tmp_path, (indicator, strategy)), (indicator, strategy))
    monkeypatch.setattr(deploy_deps.tools_plugin_loader, "approved_plugins",
                        lambda *args, **kwargs: result)

    report = deploy_deps.enumerate_dependents(conn, tmp_path, "a", settings=object())

    assert report.unresolvable == (("s", "fast", "not_found"),)
    assert report.dependent_names == {"s"}


def test_enumerate_dependents_ignores_non_dict_pending_payloads(monkeypatch, tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    conn.execute(
        "INSERT INTO approval_requests (kind,payload_json,status,created_at) "
        "VALUES ('plugin','[]','pending','2026-10-16T00:00:00+00:00')")
    conn.commit()
    indicator = _meta("a", "indicator")
    result = _Result(ApprovedInventory(tmp_path, (indicator,)), (indicator,))
    monkeypatch.setattr(deploy_deps.tools_plugin_loader, "approved_plugins",
                        lambda *args, **kwargs: result)

    report = deploy_deps.enumerate_dependents(conn, tmp_path, "a", settings=object())

    assert report.unresolvable == () and report.pending_approvals == ()


def _patch_inventory(monkeypatch, tmp_path, metas, phase1=None):
    result = _Result(ApprovedInventory(tmp_path, tuple(metas)),
                     tuple(metas if phase1 is None else phase1))
    monkeypatch.setattr(deploy_deps.tools_plugin_loader, "approved_plugins",
                        lambda *args, **kwargs: result)


def _conn(tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    return conn


def test_enumerate_dependents_ignores_strategies_that_do_not_use_the_target(
        monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    unrelated = _meta("other", "strategy", indicators=(
        IndicatorRef(alias="fast", plugin="b", params={}, pin="b-content"),))
    _patch_inventory(monkeypatch, tmp_path, [_meta("a", "indicator"), unrelated])

    report = deploy_deps.enumerate_dependents(conn, tmp_path, "a", settings=object())

    assert report.unresolvable == ()
    assert report.resolvable == ()


def test_enumerate_dependents_only_looks_at_strategies(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    ref = IndicatorRef(alias="fast", plugin="a", params={}, pin="a-content")
    not_a_strategy = _meta("derived", "indicator", indicators=(ref,))
    _patch_inventory(monkeypatch, tmp_path, [_meta("a", "indicator"), not_a_strategy])

    report = deploy_deps.enumerate_dependents(conn, tmp_path, "a", settings=object())

    assert report.unresolvable == ()
    assert report.resolvable == ()


def test_enumerate_dependents_resolves_against_the_replacement_meta(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    indicator = _meta("a", "indicator")
    strategy = _meta("s", "strategy", indicators=(
        IndicatorRef(alias="fast", plugin="a", params={}, pin="a-content"),))
    _patch_inventory(monkeypatch, tmp_path, [indicator, strategy])

    report = deploy_deps.enumerate_dependents(
        conn, tmp_path, "a", settings=SETTINGS, replacement_meta=indicator)

    assert report.unresolvable == ()
    assert report.resolvable == (("s", "fast"),)


def test_pending_pins_lists_only_pending_rows_that_pin_the_target(monkeypatch, tmp_path):
    conn = _conn(tmp_path)
    pin_a = '{"indicator_deps": {"fast": {"plugin": "a"}}}'
    pin_b = '{"indicator_deps": {"fast": {"plugin": "b"}}}'
    for payload, status in ((pin_a, "pending"), (pin_a, "approved"), (pin_b, "pending")):
        conn.execute(
            "INSERT INTO approval_requests (kind,payload_json,status,created_at) "
            "VALUES ('plugin',?,?,'2026-10-16T00:00:00+00:00')", (payload, status))
    conn.commit()
    first_id = str(conn.execute(
        "SELECT id FROM approval_requests ORDER BY id LIMIT 1").fetchone()["id"])
    _patch_inventory(monkeypatch, tmp_path, [_meta("a", "indicator")])

    report = deploy_deps.enumerate_dependents(conn, tmp_path, "a", settings=object())

    assert report.pending_approvals == ((first_id, "fast"),)
