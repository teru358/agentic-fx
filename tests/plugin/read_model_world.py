"""plugin read model の test が使う、実際の配置 (symlink + .versions + 承認 payload) の組み立て。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.plugin import loader, version_store
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.resolve import ApprovedInventory, resolve_indicator_deps
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store.db import connect, init_db
from tests.backtest.factories import SETTINGS

START = datetime(2026, 11, 1, tzinfo=timezone.utc)

INDICATOR_PY = b"def compute(df, params):\n    return {}\n"
SIGNAL_PY = b"def detect(df, params):\n    return []\n"
STRATEGY_PY = (b"def evaluate(df, indicators, signals, params):\n"
               b"    return {'action': 'hold', 'rationale': 'x'}\n")
TEST_PY = b"def test_placeholder():\n    pass\n"

CODE = {"indicator": INDICATOR_PY, "signal": SIGNAL_PY, "strategy": STRATEGY_PY}
HEAD = {
    "indicator": "kind: indicator\noutputs: [v]\nparams:\n  period: 14\n",
    "signal": "kind: signal\ntimeframe: 1h\npairs: [USDJPY]\n",
    "strategy": "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\nmax_bars: 200\n",
}


class World:
    def __init__(self, root: Path, conn=None):
        self.root = root
        self.plugins = root / "plugins"
        self.plugins.mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(exist_ok=True)
        if conn is None:
            conn = connect(root / "data" / "agentic.db")
            init_db(conn)
        self.conn = conn
        self.tick = 0

    def now(self) -> datetime:
        self.tick += 1
        return START + timedelta(seconds=self.tick)

    # ---- FS ----
    def deploy(self, name: str, kind: str = "indicator", extra: str = "",
               *, plain: bool = False) -> PluginMeta:
        """実際の配置で plugin を置く。strategy の indicators は ``extra`` に書く。"""
        config = (HEAD[kind] + extra).encode()
        code = CODE[kind]
        artifact = version_store.artifact_hash_bytes(code, config, TEST_PY)
        if plain:
            target = self.plugins / name
            target.mkdir()
            (target / "plugin.py").write_bytes(code)
            (target / "config.yaml").write_bytes(config)
            (target / "test_plugin.py").write_bytes(TEST_PY)
        else:
            version_store.create_version_dir(
                self.plugins, name, artifact, plugin_py=code, config_yaml=config,
                test_plugin=TEST_PY, op_identity="world")
            os.symlink(f".versions/{name}/{artifact}", self.plugins / name)
        meta, reason = loader.discover_one_with_reason(self.plugins / name, name)
        assert reason is None, reason
        return meta

    def version_only(self, name: str, kind: str = "indicator", extra: str = "") -> str:
        config = (HEAD[kind] + extra).encode()
        artifact = version_store.artifact_hash_bytes(CODE[kind], config, TEST_PY)
        version_store.create_version_dir(
            self.plugins, name, artifact, plugin_py=CODE[kind], config_yaml=config,
            test_plugin=TEST_PY, op_identity="world")
        return artifact

    def undeploy(self, name: str) -> None:
        os.unlink(self.plugins / name)

    # ---- DB ----
    def request(self, meta: PluginMeta, *, status: str = "pending", origin: str = "staging",
                deps: object = "auto", kind: str | None = None, mission_id: int | None = 1,
                inventory: tuple[PluginMeta, ...] = (), extra: dict | None = None) -> int:
        """実際の submit / bless / 再評価の payload と同じ key を持つ承認依頼を作る。"""
        if deps == "auto":
            if meta.indicators:
                resolved = resolve_indicator_deps(
                    meta, ApprovedInventory(self.plugins.resolve(), inventory),
                    settings=SETTINGS, pin_mode="ignore")
                deps = resolved.pin_object()
            else:
                deps = {}
        payload = {
            "name": meta.name, "kind": kind or meta.kind,
            "candidate_origin": origin,
            "candidate_path": f"plugins/_staging/1/{meta.name}",
            "content_hash": meta.content_hash, "artifact_hash": meta.artifact_hash,
            "mission_id": mission_id,
        }
        if deps is not None:
            payload["indicator_deps"] = deps
        payload.update(extra or {})
        return self.raw_request(payload, status=status)

    def raw_request(self, payload: dict | str, *, status: str = "pending") -> int:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        created = self.now()
        cur = self.conn.execute(
            "INSERT INTO approval_requests (kind,payload_json,status,created_at) "
            "VALUES ('plugin',?, 'pending', ?)", (text, created.isoformat()))
        approval_id = cur.lastrowid
        if status != "pending":
            decided = self.now() if status in ("approved", "rejected") else None
            self.conn.execute(
                "UPDATE approval_requests SET status=?, decided_at=?, decided_by=? WHERE id=?",
                (status, decided.isoformat() if decided else None, "test", approval_id))
        self.conn.commit()
        return approval_id

    def journal(self, name: str, approval_id: int, *, phase: str = "decided",
                kind: str = "approve", new_target: str | None = None) -> int:
        target = new_target or f".versions/{name}/{'c' * 64}"
        cur = self.conn.execute(
            "INSERT INTO plugin_switch_journal (kind,approval_id,name,old_kind,old_target,"
            "temp_path,new_target,switch_required,phase,actor,updated_at) "
            "VALUES (?,?,?,'absent',NULL,'t',?,1,?,'test',?)",
            (kind, approval_id, name, target, phase, self.now().isoformat()))
        self.conn.commit()
        return cur.lastrowid

    def deploy_op(self, name: str, *, op_kind: str = "disable_symlink", phase: str = "done",
                  fail_reason: str | None = None, target: str | None = None) -> int:
        """実際の ``insert_intent`` で作り、phase だけ後から進める。"""
        from agentic_fx.store import plugin_deploy_ops as deploy_store
        digest = "a" * 64
        if op_kind == "disable_symlink":
            args = dict(from_kind="symlink", to_kind="absent",
                        from_target=f".versions/{name}/{digest}", to_target=None,
                        subject_content_hash=digest, subject_artifact_hash=digest,
                        target_content_hash=None, target_artifact_hash=None)
        else:
            args = dict(from_kind="absent", to_kind="symlink", from_target=None,
                        to_target=f".versions/{name}/{target or digest}",
                        subject_content_hash=None, subject_artifact_hash=None,
                        target_content_hash=target or digest,
                        target_artifact_hash=target or digest)
        op_id = deploy_store.insert_intent(
            self.conn, op_kind=op_kind, name=name, retired_path=None, approval_id=None,
            revoke=False, revoke_row_id=None, force=False, actor="test", now=self.now(),
            **args)
        if phase != "intent":
            self.conn.execute("UPDATE plugin_deploy_ops SET phase=?, fail_reason=? "
                              "WHERE op_id=?", (phase, fail_reason, op_id))
            self.conn.commit()
        return op_id


def by_name(model, name):
    row = model.by_name(name)
    assert row is not None, name
    return row
