from __future__ import annotations

import json

from agentic_fx.backtest.plugin_deploy_cli import plugin_list
from agentic_fx.store.db import connect, init_db


def test_plugin_list_combines_live_pending_and_open_rollback_without_writing(
        tmp_path, capsys):
    root = tmp_path
    plugins = root / "plugins"
    live = plugins / "momentum"
    live.mkdir(parents=True)
    (live / "plugin.py").write_text(
        "def evaluate(df, indicators, signals, params):\n    return {'action': 'hold'}\n")
    (live / "config.yaml").write_text(
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n")
    (live / "test_plugin.py").write_text("def test_plugin():\n    pass\n")
    db = root / "data" / "agentic.db"
    conn = connect(db)
    init_db(conn)
    pending = "8b0e6d5a2c91" + "0" * 52
    target = "3f9a1c2b7d4e" + "1" * 52
    conn.execute("INSERT INTO approval_requests (kind,payload_json,status,created_at) VALUES (?,?,?,?)",
                 ("plugin", json.dumps({"name": "momentum", "content_hash": pending}),
                  "pending", "2026-10-23T00:00:00+00:00"))
    conn.execute("INSERT INTO plugin_deploy_ops (op_id,op_kind,name,from_kind,to_kind,"
                 "from_target,to_target,retired_path,subject_content_hash,subject_artifact_hash,"
                 "target_content_hash,target_artifact_hash,approval_id,revoke,revoke_row_id,force,"
                 "phase,fail_reason,notify_attempted,actor,created_at,updated_at) "
                 "VALUES (17,'rollback_symlink','momentum','absent','symlink',NULL,?,NULL,NULL,NULL,?,"
                 "?,NULL,0,NULL,0,'intent',NULL,0,'test','2026-10-23T00:00:00+00:00','2026-10-23T00:00:00+00:00')",
                 (".versions/momentum/x", target, "f" * 64))
    conn.commit()
    conn.close()
    before = db.read_bytes()

    assert plugin_list(root) == 0

    out = capsys.readouterr().out
    assert "source live: ok" in out
    assert "source pending: ok" in out
    assert "source journal: ok" in out
    assert "momentum  strategy" in out
    assert pending[:12] in out
    assert f"17:rollback:{target[:12]}" in out
    assert db.read_bytes() == before
