import json

import pytest

from agentic_fx.core.contracts import Mode
from agentic_fx.store.state import AppState, StateError, StateStore


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


def test_load_rejects_string_false_autopilot(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": True, "mode": "learning",
        "autopilot": "false", "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_string_false_initialized(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": "false", "mode": "learning",
        "autopilot": False, "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_bogus_mode(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": True, "mode": "bogus",
        "autopilot": False, "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_missing_key(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": True, "mode": "learning", "autopilot": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_int_for_bool_field(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": 1, "mode": "learning",
        "autopilot": False, "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()
