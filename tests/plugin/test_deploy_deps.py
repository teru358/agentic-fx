from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentic_fx.plugin import deploy_deps
from agentic_fx.plugin.loader import IndicatorRef, PluginMeta
from agentic_fx.plugin.resolve import ApprovedInventory
from agentic_fx.store import db as db_store


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
