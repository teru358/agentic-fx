"""稼働モード・autopilot 等の状態。config でなくここに置く (設計書 §3 の構造的担保)。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from agentic_fx.core.contracts import Mode


class StateError(Exception):
    """state.json が資金安全上信頼できない内容だった場合に送出する。

    fail closed: 握りつぶしてデフォルトを返さず、サービスの起動を止める。
    """


@dataclass(frozen=True, slots=True)
class AppState:
    initialized: bool = False
    mode: Mode = Mode.LEARNING
    autopilot: bool = False
    kill_switch_latched: bool = False


_BOOL_FIELDS = ("initialized", "autopilot", "kill_switch_latched")
_REQUIRED_KEYS = ("initialized", "mode", "autopilot", "kill_switch_latched")


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> AppState:
        if not self._path.exists():
            return AppState()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise StateError(
                f"state file must contain a JSON object, got {type(raw).__name__}")

        missing = [k for k in _REQUIRED_KEYS if k not in raw]
        if missing:
            raise StateError(f"state file missing required keys: {missing}")

        for key in _BOOL_FIELDS:
            v = raw[key]
            if type(v) is not bool:
                raise StateError(
                    f"state field {key!r} must be a JSON boolean, "
                    f"got {v!r} ({type(v).__name__})")

        mode_raw = raw["mode"]
        try:
            mode = Mode(mode_raw)
        except ValueError:
            raise StateError(
                f"state field 'mode' has invalid value {mode_raw!r} "
                f"(allowed: {[m.value for m in Mode]})") from None

        return AppState(initialized=raw["initialized"],
                        mode=mode,
                        autopilot=raw["autopilot"],
                        kill_switch_latched=raw["kill_switch_latched"])

    def save(self, state: AppState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(state)
        d["mode"] = state.mode.value
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self._path)

    def update(self, **changes) -> AppState:
        valid = {f.name for f in fields(AppState)}
        unknown = set(changes) - valid
        if unknown:
            raise TypeError(f"unknown state fields: {unknown}")
        state = replace(self.load(), **changes)
        self.save(state)
        return state
