"""Shared helpers for the multi-process producer / deploy-operation tests."""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.switch import plugin_lock_path
from agentic_fx.plugin.resolve import ResolvedIndicator, ResolvedIndicatorSet
from tests.plugin.deploy_world import World
from tests.plugin.test_signal_producer import H, SETTINGS, SOURCE, _seed_flat

HERE = Path(__file__).parent
FIRST_TICK = H + timedelta(hours=1, seconds=31)
NEXT_TICK = FIRST_TICK + timedelta(hours=1)


def wait_for(path: Path | str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not Path(path).exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"barrier never appeared: {path}")
        time.sleep(0.01)


def probe_other_process(root: Path, names) -> dict[str, bool]:
    """Ask a separate process whether it can take each name lock right now."""
    out = subprocess.run(
        [sys.executable, str(HERE / "_lock_probe.py"), str(root), *names],
        capture_output=True, text=True, timeout=60, check=True,
        stdin=subprocess.DEVNULL)
    return json.loads(out.stdout)


def lock_is_free(root: Path, name: str) -> bool:
    path = plugin_lock_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True
    finally:
        handle.close()


class Holder:
    """A separate process that owns the given name locks until released."""

    def __init__(self, root: Path, prefix: Path, names) -> None:
        self.prefix = str(prefix)
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "_lock_holder.py"), str(root),
             self.prefix, *names], stdin=subprocess.DEVNULL)
        try:
            wait_for(self.prefix + ".held")
        except BaseException:
            self.proc.kill()
            self.proc.wait()
            raise

    def release(self) -> None:
        Path(self.prefix + ".release").write_text("release")
        self.proc.wait(timeout=30)
        assert self.proc.returncode == 0

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class DisableChild:
    """``disable_plugin`` running in a separate process."""

    def __init__(self, db_path: Path, root: Path, name: str, *, pause: str,
                 force: bool, prefix: Path) -> None:
        self.prefix = str(prefix)
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "_disable_worker.py"), str(db_path),
             str(root), name, pause, "1" if force else "0", self.prefix],
            stdin=subprocess.DEVNULL)

    def wait_reached(self) -> None:
        try:
            wait_for(self.prefix + ".reached")
        except BaseException:
            self.close()
            raise

    def go(self) -> None:
        Path(self.prefix + ".go").write_text("go")

    def finish(self) -> str:
        self.proc.wait(timeout=60)
        assert self.proc.returncode == 0
        return Path(self.prefix + ".result").read_text()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def run_disable(db_path: Path, root: Path, name: str, *, force: bool,
                prefix: Path) -> str:
    child = DisableChild(db_path, root, name, pause="none", force=force,
                         prefix=prefix)
    try:
        return child.finish()
    finally:
        child.close()


class Activity:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def write(self, _category, event, summary, ref_id=None):
        self.events.append((event, summary))

    def names(self) -> list[str]:
        return [event for event, _ in self.events]


class Sandbox:
    """Producer evaluation stand-in that can run a callback during evaluation."""

    def __init__(self, result: dict, on_call=None) -> None:
        self.result = result
        self.on_call = on_call
        self.calls: list[dict] = []

    def __call__(self, meta, payload, *, settings):
        self.calls.append(payload)
        if self.on_call is not None:
            self.on_call()
        return dict(self.result)


class ConcWorld(World):
    """A live, approved strategy ``s`` (optionally depending on indicator ``a``)."""

    def __init__(self, tmp_path: Path, *, indicator: str | None = None) -> None:
        super().__init__(tmp_path)
        self.db_path = tmp_path / "agentic.db"
        self.strategy_dir = self.version("s", "s", kind="strategy", indicator=indicator)
        self.approve("s", self.strategy_dir)
        self.link("s", self.strategy_dir)
        self.indicator_dir = None
        if indicator is not None:
            self.indicator_dir = self.version(indicator, "i")
            self.approve(indicator, self.indicator_dir)
            self.link(indicator, self.indicator_dir)
        _seed_flat(self.conn, H - timedelta(hours=3), 8 * 60 + 1)
        self.indicator_name = indicator

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="s", kind="strategy", path=self.strategy_dir, params={},
            timeframe="1h", pairs=("USDJPY",), max_bars=2,
            content_hash=self.content(self.strategy_dir),
            artifact_hash=self.strategy_dir.name)

    def resolved(self, *, content_hash: str | None = None) -> dict:
        meta = self.meta()
        items = ()
        if self.indicator_name is not None:
            items = (ResolvedIndicator(
                alias="fast", plugin_name=self.indicator_name,
                plugin_py=self.root / self.indicator_name / "plugin.py",
                content_hash=content_hash or (
                    self.content(self.indicator_dir) if self.indicator_dir else "f" * 64),
                params={}, max_bars=2, outputs=("value",), pinned=False),)
        return {(meta.name, meta.content_hash): ResolvedIndicatorSet(
            inventory_root=self.root, items=items, all_pinned=False)}

    def evaluate(self, producer, now, sandbox, activity):
        meta = self.meta()
        return producer.evaluate_due_plugins(
            self.conn, plugins=[meta], now=now, source=SOURCE,
            sandbox_run=sandbox, settings=SETTINGS,
            resolved_by_identity=self.resolved(), activity=activity,
            plugins_root=self.root)

    def signal_rows(self) -> list[tuple[str, str]]:
        return [(r["plugin"], r["status"]) for r in self.conn.execute(
            "SELECT plugin, status FROM signals ORDER BY id")]

    def pending(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status='pending'").fetchone()[0]

    def total(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
