"""Shared on-disk fixtures for the rollback / reconcile tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.plugin import loader
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store import signals as signals_store

T0 = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class Activity:
    def __init__(self):
        self.events = []

    def write(self, category, event, summary):
        self.events.append((category, event, summary))


class World:
    def __init__(self, tmp_path: Path):
        self.db_path = tmp_path / "agentic.db"
        self.conn = db_store.connect(self.db_path)
        db_store.init_db(self.conn)
        self.root = tmp_path / "plugins"
        self.root.mkdir()

    def version(self, name: str, tag: str, *, kind: str = "indicator",
                indicator: str | None = None, pin: str | None = None) -> Path:
        staging = self.root / ".versions" / name / f"tmp-{tag}"
        staging.mkdir(parents=True)
        if kind == "indicator":
            source = f"# {tag}\ndef compute(df, params):\n    return {{'value': 1.0}}\n"
            config = "kind: indicator\noutputs: [value]\n"
        else:
            source = (f"# {tag}\ndef evaluate(df, indicators, signals, params):\n"
                      "    return {'action': 'hold'}\n")
            ref = ""
            if indicator:
                pin_text = f", pin: {pin}" if pin else ""
                ref = f"indicators:\n  fast: {{plugin: {indicator}{pin_text}}}\n"
            config = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
                      "exit_mode: levels\n" + ref)
        (staging / "plugin.py").write_text(source)
        (staging / "config.yaml").write_text(config)
        (staging / "test_plugin.py").write_text("def test_plugin():\n    pass\n")
        digest = loader.artifact_hash_bytes(*(p.read_bytes() for p in (
            staging / "plugin.py", staging / "config.yaml", staging / "test_plugin.py")))
        final = staging.with_name(digest)
        staging.rename(final)
        return final

    @staticmethod
    def content(version: Path) -> str:
        return loader.content_hash(version)

    def approve(self, name: str, version: Path, minute: int = 0) -> int:
        payload = {"name": name, "content_hash": loader.content_hash(version),
                   "artifact_hash": version.name}
        aid = approvals_store.create(self.conn, "plugin", payload, at(minute))
        approvals_store.apply_decision(self.conn, aid, "approved", decided_by="human",
                                       now=at(minute))
        return aid

    def target(self, name: str, version: Path | None) -> str | None:
        return None if version is None else f".versions/{name}/{version.name}"

    def journal(self, name: str, kind: str, old: Path | None, new: Path, minute: int,
                aid: int = 1) -> int:
        op_id = journal_store.insert(
            self.conn, kind=kind, approval_id=aid, name=name,
            old_kind="absent" if old is None else "symlink",
            old_target=self.target(name, old), temp_path="",
            new_target=self.target(name, new), switch_required=True, actor="human",
            now=at(minute))
        journal_store.set_phase(self.conn, op_id, "decided", now=at(minute))
        return op_id

    def link(self, name: str, version: Path) -> Path:
        live = self.root / name
        if live.is_symlink():
            live.unlink()
        live.symlink_to(self.target(name, version))
        return live

    def deploy(self, name: str, version: Path, previous: Path | None, minute: int) -> int:
        aid = self.approve(name, version, minute)
        self.journal(name, "approve", previous, version, minute, aid)
        self.link(name, version)
        return aid

    def three_generations(self, name: str = "sma"):
        a, b, c = (self.version(name, tag) for tag in ("a", "b", "c"))
        self.deploy(name, a, None, 1)
        self.deploy(name, b, a, 2)
        self.deploy(name, c, b, 3)
        return a, b, c

    def intent(self, name: str, *, op_kind: str = "rollback_symlink", phase: str = "intent",
               fail_reason: str | None = None, from_ver: Path | None = None,
               to_ver: Path | None = None, force: bool = False, revoke: bool = False,
               minute: int = 10, retired_path: str | None = None, from_plain: bool = False,
               notify: bool = False, subject_content_hash: str | None = None) -> int:
        row = dict(op_kind=op_kind, name=name, retired_path=retired_path,
                   approval_id=None, revoke=False, revoke_row_id=None, force=force,
                   actor="human_cli", now=at(minute), commit=True)
        if op_kind == "rollback_symlink":
            row.update(
                from_kind="symlink" if from_ver else "absent", to_kind="symlink",
                from_target=self.target(name, from_ver), to_target=self.target(name, to_ver),
                subject_content_hash=loader.content_hash(from_ver) if from_ver else None,
                subject_artifact_hash=from_ver.name if from_ver else None,
                target_content_hash=loader.content_hash(to_ver),
                target_artifact_hash=to_ver.name)
        elif op_kind == "disable_symlink":
            row.update(
                from_kind="symlink", to_kind="absent",
                from_target=self.target(name, from_ver), to_target=None,
                subject_content_hash=loader.content_hash(from_ver),
                subject_artifact_hash=from_ver.name,
                target_content_hash=None, target_artifact_hash=None)
        else:
            row.update(
                from_kind="plain", to_kind="retired", from_target=None, to_target=None,
                subject_content_hash=subject_content_hash, subject_artifact_hash=None,
                target_content_hash=None, target_artifact_hash=None)
        op_id = deploy_store.insert_intent(self.conn, **row)
        if phase != "intent":
            deploy_store.set_phase(self.conn, op_id, phase, now=at(minute),
                                   fail_reason=fail_reason, notify_attempted=notify)
        elif notify:
            deploy_store.mark_notify_attempted(self.conn, op_id)
        return op_id

    def signal(self, name: str, content: str, *, claimed: bool = False) -> int:
        signal_id = signals_store.add(
            self.conn, plugin=name, content_hash=content, pair="USDJPY", timeframe="1h",
            bar_ts=(at(1) if claimed else at(0)).isoformat(), kind="signal",
            payload={"direction": "long"}, now=at(0))
        if claimed:
            self.conn.execute("UPDATE signals SET status='claimed' WHERE id=?", (signal_id,))
            self.conn.commit()
        return signal_id

    def snapshot(self, name: str):
        live = self.root / name
        raw = None
        if live.is_symlink():
            raw = ("link", str(live.readlink()))
        elif live.exists():
            raw = ("dir",)
        return (raw, sorted(p.name for p in self.root.iterdir() if p.name != ".locks"),
                [tuple(r) for r in self.conn.execute("SELECT * FROM plugin_deploy_ops")],
                [tuple(r) for r in self.conn.execute("SELECT id,status FROM signals")],
                [tuple(r) for r in self.conn.execute(
                    "SELECT id,status FROM approval_requests")])
