import pytest

from agentic_fx.entry import main as entry_main
from agentic_fx.service import ensure_initialized, run_init
from agentic_fx.store.db import connect
from agentic_fx.store.state import StateStore


def _example(root):
    (root / "config").mkdir(parents=True)
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (root / "config" / "settings.yaml.example").write_text(src)


def test_guard_blocks_before_init(tmp_path):
    _example(tmp_path)
    with pytest.raises(SystemExit) as e:
        ensure_initialized(tmp_path)
    assert e.value.code == 2


def test_init_creates_everything(tmp_path):
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    assert (tmp_path / "config" / "settings.yaml").exists()
    assert (tmp_path / "data" / "agentic.db").exists()
    conn = connect(tmp_path / "data" / "agentic.db")
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "orders" in tables
    state = StateStore(tmp_path / "data" / "state" / "app_state.json").load()
    assert state.initialized is True
    assert state.mode.value == "learning"
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "init_completed" in act
    ensure_initialized(tmp_path)  # ガード通過 (例外なし)


def test_init_is_idempotent(tmp_path):
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    marker = tmp_path / "config" / "settings.yaml"
    marker.write_text(marker.read_text() + "\n# user edit\n")
    assert run_init(tmp_path) == 0
    assert "# user edit" in marker.read_text()  # 既存 settings.yaml を上書きしない


def test_reinit_in_trading_keeps_mode(tmp_path):
    # init をモード遷移ガード (§3) の迂回路にしない: trading 中は mode/autopilot 不変
    from agentic_fx.core.contracts import Mode
    _example(tmp_path)
    run_init(tmp_path)
    store = StateStore(tmp_path / "data" / "state" / "app_state.json")
    store.update(mode=Mode.TRADING, autopilot=True)
    run_init(tmp_path)
    s = store.load()
    assert s.mode is Mode.TRADING
    assert s.autopilot is True
    assert s.initialized is True


def test_entry_init_subcommand(tmp_path, monkeypatch):
    _example(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert entry_main(["init"]) == 0


def test_entry_default_requires_init(tmp_path, monkeypatch):
    _example(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        entry_main([])
    assert e.value.code == 2
