"""変更内容は派生物を保存せず、改善 agent へ戻る経路を作らない。"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.ops import client
from agentic_fx.ops.contracts import PRINCIPAL_SCOPES, ErrorCode, OpsError, Principal, Scope

from ._api import backlog_row
from ._change_fixtures import (approval_payload, insert_approval, make_live, make_staging,
                               make_version)
from .conftest import OPERATOR

NAME = "sma_cross"
OLD_CFG = "kind: strategy\nparams:\n  period: 14\n"
NEW_CFG = "kind: strategy\nparams:\n  period: 21\n"


def _canaries(tag: str) -> dict[str, str]:
    return {"plugin": f"PLUGINCANARY_{tag}_q7x2", "config": f"CFGCANARY_{tag}_k4m9",
            "summary": f"SUMMARYCANARY_{tag}_b8n3", "rationale": f"RATIONALECANARY_{tag}_z5v1",
            "reason": f"REASONCANARY_{tag}_w2j6", "metric": "7.31337"}


def _scenario(env, tag: str):
    """canary を plugin ファイル・config・申告・成績・却下理由の全てに入れた承認依頼。"""
    c = _canaries(tag)
    plugins = env.plugins_root
    old_hash = make_version(plugins, NAME, plugin_py="x = 1\n", config=OLD_CFG)
    make_live(plugins, NAME, old_hash)
    digest = make_staging(plugins, 7, NAME, plugin_py=f"x = 2  # {c['plugin']}\n",
                          config=NEW_CFG + f"  note: {c['config']}\n")
    payload = approval_payload(NAME, digest=digest)
    payload["agent_claims"] = {"summary": c["summary"], "selection_rationale": c["rationale"]}
    payload["in_sample"] = {"USDJPY": {"trades": 11, "pf": float(c["metric"]), "avg_r": 0.5}}
    approval_id = insert_approval(env, payload)
    backlog_id = backlog_row(env)
    env.conn.execute("UPDATE improvement_backlog SET last_result=? WHERE id=?",
                     (f"approval_pending:{approval_id}", backlog_id))
    env.conn.commit()
    return approval_id, backlog_id, c


def _row_snapshot(env, approval_id):
    return tuple(env.conn.execute(
        "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone())


def _all_tables_text(conn, skip=("approval_requests",)) -> str:
    chunks = []
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if table in skip:
            continue
        for row in conn.execute(f"SELECT * FROM {table}"):
            chunks.append(repr(tuple(row)))
    return "\n".join(chunks)


def _exercise(env):
    """変更内容を計算する全ての入口を通す。"""
    service = env.service()
    return service, service.approval_detail(OPERATOR, env_approval(env)), \
        service.approval_change(OPERATOR, env_approval(env))


def env_approval(env) -> int:
    return env.conn.execute("SELECT max(id) FROM approval_requests").fetchone()[0]


def test_ac15_approval_row_is_byte_identical_and_shape_is_unchanged(ops_env):
    approval_id, _, c = _scenario(ops_env, "ac15")
    columns_before = [r[1] for r in ops_env.conn.execute("PRAGMA table_info(approval_requests)")]
    payload_keys_before = set(json.loads(ops_env.approval(approval_id)["payload_json"]))
    row_before = _row_snapshot(ops_env, approval_id)
    service = ops_env.service()

    detail = service.approval_detail(OPERATOR, approval_id)
    service.approval_change(OPERATOR, approval_id)
    client.format_approval_detail(detail)

    assert _row_snapshot(ops_env, approval_id) == row_before
    assert [r[1] for r in ops_env.conn.execute("PRAGMA table_info(approval_requests)")] \
        == columns_before
    assert set(json.loads(ops_env.approval(approval_id)["payload_json"])) == payload_keys_before
    assert detail["payload_sha256"] == __import__("agentic_fx.plugin.switch", fromlist=["x"]) \
        .payload_sha256(row_before[2])
    assert "change" not in payload_keys_before and "diff" not in payload_keys_before


def test_ac15_derived_canaries_appear_in_no_table_and_last_result_is_untouched(ops_env):
    approval_id, backlog_id, c = _scenario(ops_env, "ac15b")
    derived = {k: v for k, v in c.items() if k in ("plugin", "config")}
    plugin_py = (ops_env.plugins_root / "_staging" / "7" / NAME / "plugin.py").read_text()
    config = (ops_env.plugins_root / "_staging" / "7" / NAME / "config.yaml").read_text()
    assert derived["plugin"] in plugin_py and derived["config"] in config  # 入力側に存在する
    service = ops_env.service()
    body = service.approval_change(OPERATOR, approval_id)
    service.approval_detail(OPERATOR, approval_id)
    assert derived["plugin"] in json.dumps(body)  # API の応答にはある (表示用)
    text = _all_tables_text(ops_env.conn)
    for name, value in derived.items():
        assert value not in text, name
        assert value not in ops_env.approval(approval_id)["payload_json"], name
    last = ops_env.conn.execute("SELECT last_result FROM improvement_backlog WHERE id=?",
                                (backlog_id,)).fetchone()[0]
    assert last == f"approval_pending:{approval_id}"


def test_ac16_generated_improve_prompt_carries_no_change_summary_or_claims(ops_env):
    from agentic_fx.loops.improve_context import build_improve_context
    from agentic_fx.loops.improve_loop import ImproveLoop

    approval_id, _, c = _scenario(ops_env, "ac16")
    service = ops_env.service()
    shown = json.dumps(service.approval_change(OPERATOR, approval_id)) \
        + json.dumps(service.approval_detail(OPERATOR, approval_id))
    for name in ("plugin", "config", "summary", "rationale", "metric"):
        assert c[name] in shown, name  # 表示側に実在するので、下の非流入が意味を持つ
    ctx_data = build_improve_context(
        ops_env.conn, settings=ops_env.settings, now=datetime(2026, 10, 5),
        root=ops_env.root, allowed_backlog_ids=None, inventory_view={"plugins": []})

    class Ctx:
        inventory_view = {"plugins": [], "pin_broken_strategies": []}
        baseline_gate_mode = "off"
        staging_dir = ops_env.root / "staging"
        source_snapshot_dir = ops_env.root / "source"

    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = ops_env.settings
    prompt = loop._render_improve_mission_prompt(ctx_data, ctx=Ctx())

    assert "improvement_history" not in prompt and len(prompt) > 500
    for name, value in c.items():
        assert value not in prompt, name
    assert "@@" not in prompt and "hunk" not in prompt


def test_ac27_canaries_reach_no_persistence_sink_log_or_file(ops_env, caplog):
    approval_id, backlog_id, c = _scenario(ops_env, "ac27")
    ops_env.conn.execute("UPDATE approval_requests SET reason=? WHERE id=?",
                         (c["reason"], approval_id))
    ops_env.conn.commit()
    plugin_text = (ops_env.plugins_root / "_staging" / "7" / NAME / "plugin.py").read_text()
    assert c["plugin"] in plugin_text
    assert c["summary"] in ops_env.approval(approval_id)["payload_json"]
    assert c["reason"] == ops_env.approval(approval_id)["reason"]
    service = ops_env.service()
    with caplog.at_level(logging.DEBUG):
        service.approval_detail(OPERATOR, approval_id)
        service.approval_change(OPERATOR, approval_id)
        client.format_approval_detail(service.approval_detail(OPERATOR, approval_id))
        service.events_after(OPERATOR, after=0, limit=100)
    sinks = {
        "tables": _all_tables_text(ops_env.conn),
        "events": json.dumps(service.events_after(OPERATOR, after=0, limit=500)),
        "log": caplog.text,
    }
    for path in ops_env.root.rglob("*"):
        # DB file は表ごとの検査で見る (承認依頼の既存保存を含むため file 単位では見ない)
        if (path.is_file() and ops_env.plugins_root not in path.parents
                and not path.name.startswith("agentic.db")):
            sinks[f"file:{path.name}"] = path.read_bytes().decode("utf-8", "replace")
    for sink, text in sinks.items():
        for name, value in c.items():
            if name in ("reason",) and sink == "tables":
                continue
            assert value not in text, (sink, name)
    last = ops_env.conn.execute("SELECT last_result FROM improvement_backlog WHERE id=?",
                                (backlog_id,)).fetchone()[0]
    assert last == f"approval_pending:{approval_id}"


def test_ac27_reason_is_only_in_the_existing_approval_column(ops_env):
    approval_id, _, c = _scenario(ops_env, "ac27r")
    ops_env.conn.execute("UPDATE approval_requests SET reason=? WHERE id=?",
                         (c["reason"], approval_id))
    ops_env.conn.commit()
    service = ops_env.service()
    body = json.dumps(service.approval_change(OPERATOR, approval_id))
    assert c["reason"] not in body
    assert c["reason"] not in _all_tables_text(ops_env.conn)


def test_ac27_exception_log_for_a_failing_computation_holds_only_a_fixed_code(
        ops_env, monkeypatch, caplog):
    from agentic_fx.ops import change as change_mod
    approval_id, _, c = _scenario(ops_env, "ac27x")

    def boom(**kwargs):
        raise RuntimeError(f"{c['plugin']} /abs/path/here")
    monkeypatch.setattr(change_mod, "compute_change", boom)
    with caplog.at_level(logging.DEBUG):
        ops_env.service().approval_change(OPERATOR, approval_id)
    assert c["plugin"] not in caplog.text and "/abs/path" not in caplog.text
    assert not any(r.exc_info for r in caplog.records)


def test_ac14_scope_is_approvals_detail_for_exactly_the_principals_holding_it(ops_env):
    approval_id, _, _ = _scenario(ops_env, "ac14")
    service = ops_env.service()
    for principal in Principal:
        allowed = Scope.APPROVALS_DETAIL in PRINCIPAL_SCOPES[principal]
        try:
            service.approval_change(principal, approval_id)
            outcome = None
        except OpsError as exc:
            outcome = exc.code
        assert (outcome is None) is allowed, principal
        if not allowed:
            assert outcome is ErrorCode.FORBIDDEN


def test_ac14_http_endpoint_returns_json_with_raw_ampersand(api):
    env = api.env
    plugins = env.plugins_root
    digest = make_staging(plugins, 7, NAME, plugin_py="a = b & c < d\n")
    approval_id = insert_approval(env, approval_payload(NAME, digest=digest))
    response = api.client(Principal.OPERATOR).get(f"/v1/approvals/{approval_id}/change")
    assert response.status_code == 200
    assert "a = b & c < d" in response.text and "&amp;" not in response.text
    data = response.json()["data"]
    assert data["id"] == approval_id and set(data) == {"id", "change", "claims", "diff"}
    missing = api.client(Principal.OPERATOR).get("/v1/approvals/99999/change")
    assert missing.status_code == 404
    assert api.client(Principal.OPERATOR).get(
        f"/v1/approvals/{approval_id}/change?x=1").status_code in (200, 400)
    assert api.client(None).get(f"/v1/approvals/{approval_id}/change").status_code == 401


def test_ac28_improve_registry_has_no_discord_transcript_approval_or_log_reader(tmp_path):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.config import load_settings
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.store.db import connect, init_db
    from agentic_fx.store.rag import Rag
    from agentic_fx.tools.mission_registry import build_mission_registry
    from tests.tools.test_mission_registry import SETTINGS, FakeEmbedding

    conn = connect(tmp_path / "x.db")
    init_db(conn)
    registry = build_mission_registry(
        "improve", conn, SETTINGS, FixedClock(datetime(2026, 8, 4, 12, 0)),
        Rag(tmp_path / "rag", embedding_function=FakeEmbedding()),
        activity=ActivityLog(tmp_path / "logs" / "activity.log"),
        staging_dir=tmp_path / "staging", source_snapshot_dir=tmp_path / "source",
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})
    for name in registry.names():
        for banned in ("discord", "transcript", "approval", "log", "activity", "change", "diff",
                       "ops", "message", "notify"):
            assert banned not in name.lower(), (name, banned)
    assert len(registry.names()) == 13
