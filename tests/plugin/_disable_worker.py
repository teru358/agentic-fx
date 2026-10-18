"""Child entry: run ``disable_plugin`` and optionally stop at a named point.

argv: db_path plugins_root name pause force barrier_prefix
``pause`` is ``none``, ``before_locks`` (first dependents enumeration, which runs
before any name lock is taken) or ``record_intent`` (name locks held, intent not
yet persisted).  At a pause point the child writes <prefix>.reached and waits for
<prefix>.go.  The outcome is written to <prefix>.result.
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agentic_fx.config import load_settings
from agentic_fx.plugin import deploy_disable, deploy_ops
from agentic_fx.store import db as db_store

NOW = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)


def _pause(prefix: str) -> None:
    Path(prefix + ".reached").write_text("reached")
    deadline = time.monotonic() + 60
    while not Path(prefix + ".go").exists():
        if time.monotonic() > deadline:
            raise SystemExit("go was never signalled")
        time.sleep(0.01)


def main() -> None:
    db_path, root, name, pause, force, prefix = sys.argv[1:7]
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    conn = db_store.connect(Path(db_path))

    if pause == "before_locks":
        real = deploy_disable.enumerate_dependents
        calls = []

        def paused(*args, **kwargs):
            if not calls:
                calls.append(1)
                _pause(prefix)
            return real(*args, **kwargs)

        deploy_disable.enumerate_dependents = paused
    elif pause == "record_intent":
        real_record = deploy_ops.record_intent

        def paused_record(*args, **kwargs):
            _pause(prefix)
            return real_record(*args, **kwargs)

        deploy_ops.record_intent = paused_record

    outcome = deploy_disable.disable_plugin(
        conn, Path(root), name, revoke=False, force=force == "1", now=NOW,
        settings=settings)
    Path(prefix + ".result").write_text(
        f"{outcome.outcome}:{','.join(outcome.cascade_strategies)}")


if __name__ == "__main__":
    main()
