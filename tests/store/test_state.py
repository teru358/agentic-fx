import json

from agentic_fx.core.contracts import Mode
from agentic_fx.store.state import AppState, StateStore


def test_defaults_when_missing(tmp_path):
    s = StateStore(tmp_path / "state" / "app_state.json").load()
    assert s == AppState(initialized=False, mode=Mode.LEARNING,
                         autopilot=False, kill_switch_latched=False)


def test_update_roundtrip(tmp_path):
    store = StateStore(tmp_path / "app_state.json")
    store.update(initialized=True)
    s2 = StateStore(tmp_path / "app_state.json").load()
    assert s2.initialized is True
    assert s2.mode is Mode.LEARNING


def test_atomic_write_no_partial_file(tmp_path):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    store.update(initialized=True, autopilot=True)
    data = json.loads(path.read_text())
    assert data["autopilot"] is True
    assert not list(tmp_path.glob("*.tmp"))


def test_unknown_field_rejected(tmp_path):
    store = StateStore(tmp_path / "app_state.json")
    try:
        store.update(no_such_field=1)
        assert False, "should raise"
    except TypeError:
        pass
