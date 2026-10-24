"""web の公開範囲の試験で共有する偽物。実 home・実 DB・本番 unit には触れない。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from agentic_fx.ops.keys import Principal, initialize, key_dir
from agentic_fx.ops_setup.interfaces import Candidate
from agentic_fx.ops_setup.stages import OpsSetup
from agentic_fx.ops_setup.systemd import SystemdError
from agentic_fx.ops_setup.units import UnitPaths

LAN = "192.168.1.10"
LAN2 = "192.168.1.11"
IFNAME = "enp6s0"
CANDIDATES = (Candidate(LAN, 2, "global", IFNAME), Candidate(LAN2, 2, "global", IFNAME),
              Candidate("169.254.3.4", 3, "link", "wlp3s0"))
ONE_CANDIDATE = (Candidate(LAN, 2, "global", IFNAME),)

# 質問の label。既定の番号は label でなく ask の default (`ScriptIO.defaults`) に載る
SCOPE_Q = "公開範囲 [1] 自機のみ [2] LAN"
KEEP_Q = "この設定で有効化します。[1] このまま [2] 変更する"
BIND_EXPLANATION = "この PC のどの IP アドレスで待ち受けるかを選んでください（他の PC はこのアドレスにアクセスします）。"
ACCESS_Q = "アクセス制限 [1] 許可 IP [2] Basic 認証 [3] 両方 [4] なし (到達できる全 IPv4)"
KEEP_BASIC_Q = "現在の Basic 認証を [1] 維持する [2] 再設定する"
USER_Q = "ユーザー名"
PASSWORD_Q = "パスワード (空 Enter で自動生成)"
CONFIRM_Q = "もう一度"
WARNING = "LAN、VPN、port-forward 等を含め、このアドレスへ到達できる全 IPv4 source が戦略の diff を読めます"


@dataclass
class ScriptIO:
    """質問への回答を順に返す。EOF は `eof_after` 個の回答の後。全 IO の記録を残す。"""

    answers: list = field(default_factory=list)
    secret_answers: list = field(default_factory=list)
    tty: bool = True
    eof_after: int | None = None
    log: list = field(default_factory=list)
    defaults: list = field(default_factory=list)

    def _consumed(self) -> int:
        return sum(1 for kind, _ in self.log if kind in ("ask", "secret"))

    def isatty(self) -> bool:
        return self.tty

    def ask(self, label, default=None):
        if self.eof_after is not None and self._consumed() >= self.eof_after:
            self.log.append(("ask", label))
            raise EOFError
        self.log.append(("ask", label))
        self.defaults.append((label, default))
        value = self.answers.pop(0)
        return value or default

    def ask_raw(self, label, default=None):
        return self.ask(label, default)

    def ask_secret(self, label):
        if self.eof_after is not None and self._consumed() >= self.eof_after:
            self.log.append(("secret", label))
            raise EOFError
        self.log.append(("secret", label))
        return self.secret_answers.pop(0)

    def stdout(self, text):
        self.log.append(("out", text))

    def stderr(self, text):
        self.log.append(("err", text))

    # --- 参照 ---
    def of(self, kind: str) -> list[str]:
        return [text for k, text in self.log if k == kind]

    @property
    def asked(self) -> list[str]:
        return self.of("ask")

    @property
    def out(self) -> list[str]:
        return self.of("out")

    @property
    def err(self) -> list[str]:
        return self.of("err")

    def text(self) -> str:
        return "\n".join(f"{k}: {t}" for k, t in self.log)


class FakeSystemd:
    """systemctl 境界だけの偽物。unit 資材と鍵は実物を使う。呼出し列を全部記録する。"""

    def __init__(self, paths: UnitPaths) -> None:
        self.paths = paths
        self.active: set[str] = set()
        self.failed: set[str] = set()
        self.calls: list[tuple] = []
        self.afx_state = "active"
        self.afx_show_error = False
        self.need_reload: dict[str, str] = {}
        self.fail: dict[str, SystemdError] = {}
        self.fail_after: dict[str, SystemdError] = {}
        self.stuck: set[str] = set()
        self.start_limited: set[str] = set()
        self.on_call: dict[str, object] = {}
        self.afx_after_start_failure: str | None = None
        self.loaded = True
        self.results: dict[str, str] = {}
        self.wants_override: str | None = None

    # --- 内部 ---
    def _log(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        hook = self.on_call.get(name)
        if hook is not None:
            hook(self)
        if name in self.fail:
            raise self.fail[name]

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def for_unit(self, unit: str) -> list[str]:
        return [call[0] for call in self.calls if len(call) > 1 and call[1] == unit]

    # --- SystemdPort ---
    def stop(self, unit: str) -> None:
        self._log("stop", unit)
        if unit not in self.stuck:
            self.active.discard(unit)
            if unit == self.paths.afx_unit:
                self.afx_state = "inactive"

    def start(self, unit: str) -> None:
        self._log("start", unit)
        if unit in self.start_limited:
            self.failed.add(unit)
            raise SystemdError("start", "rc=1 start-limit")
        if unit == self.paths.afx_unit:
            self.afx_state = "active"
        elif self.afx_state not in ("active", "activating", "reloading"):
            if self.afx_after_start_failure is not None:
                self.afx_state = self.afx_after_start_failure
            raise SystemdError("start", "rc=1 requisite")
        self.failed.discard(unit)
        self.active.add(unit)

    def ensure_active(self, unit: str) -> None:
        self._log("ensure_active", unit)
        if unit not in self.active:
            self.active.add(unit)

    def reset_failed(self, unit: str) -> None:
        self._log("reset_failed", unit)
        self.start_limited.discard(unit)
        self.failed.discard(unit)

    def is_active(self, unit: str) -> bool:
        self._log("is_active", unit)
        return unit in self.active

    def is_stopped(self, unit: str) -> bool:
        self._log("is_stopped", unit)
        return unit not in self.active

    def daemon_reload(self) -> None:
        self._log("daemon_reload")

    def show(self, unit: str, *props: str) -> dict[str, str]:
        self._log("show", unit, *props)
        if unit == self.paths.afx_unit and self.afx_show_error and "ActiveState" in props:
            raise SystemdError("show", "rc=1")
        wants = ""
        dropin = self.paths.dropin_path
        if dropin.exists():
            wants = " ".join(part for line in dropin.read_text().splitlines() if line.startswith("Wants=")
                             for part in line[len("Wants="):].split())
        if unit == self.paths.afx_unit:
            active_state = self.afx_state
        else:
            active_state = "active" if unit in self.active else ("failed" if unit in self.failed else "inactive")
        values = {"LoadState": ("loaded" if (unit == self.paths.afx_unit or self.paths.template_path.exists())
                                and self.loaded else "not-found"),
                  "ActiveState": active_state,
                  "Result": self.results.get(unit, "success"),
                  "Wants": (wants if self.wants_override is None else self.wants_override)
                  if unit == self.paths.afx_unit else "",
                  "NeedDaemonReload": self.need_reload.get(unit, "no"),
                  "RestartPreventExitStatus": "75" if unit == self.paths.afx_unit else ""}
        return {prop: values[prop] for prop in props}


@dataclass
class Bundle:
    root: Path
    setup: OpsSetup
    systemd: FakeSystemd
    paths: UnitPaths
    xdg: Path

    @property
    def web_unit(self) -> str:
        return self.paths.feature_unit("web")

    @property
    def stage_file(self) -> Path:
        return self.root / "data" / "ops" / "web.stage.json"

    def snapshot(self) -> dict[str, bytes]:
        """鍵 dir・stage file・enroll marker の byte 内容 (reconfigure が変えてはいけないもの)。"""
        result: dict[str, bytes] = {}
        for base in (key_dir(self.root), self.root / "data" / "ops"):
            if base.exists():
                for path in sorted(base.rglob("*")):
                    if path.is_file() and path.name != "setup.lock":
                        result[str(path)] = path.read_bytes()
        return result


def make_bundle(tmp_path: Path, monkeypatch, *, enable_web: bool = True, access_check=lambda: None,
                web_values: dict | None = None, afx_active: bool = True) -> Bundle:
    """鍵初期化済みの instance。enable_web なら web を実際に enable して terminal `enabled` にする。"""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "instance"
    root.mkdir()
    initialize(key_dir(root), root=root)
    ops_exec = tmp_path / "bin" / "afx-ops"
    ops_exec.parent.mkdir()
    ops_exec.write_text("#!/bin/sh\n")
    ops_exec.chmod(0o755)
    paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx-test.service", ops_exec=str(ops_exec))
    systemd = FakeSystemd(paths)
    if afx_active:
        systemd.active.add(paths.afx_unit)
    setup = OpsSetup(root, systemd=systemd, paths=paths, ready=lambda _feature: True,
                     config_check=lambda _root, _feature: True, probe=lambda _exec, _feature: (0, ""),
                     access_check=access_check)
    from agentic_fx.ops_setup.feature_config import write_inputs
    write_inputs(root, "web", web_values or {"port": 8788})
    if enable_web:
        assert setup.enable("web").stage == "enabled"
        systemd.calls.clear()
    return Bundle(root, setup, systemd, paths, xdg / "agentic-fx" / "ops" / "web")


def write_fake_interfaces(path: Path, document: dict | None, *, rc: int = 0) -> str:
    """`afx-ops interfaces` の偽物 (実 process)。document の JSON を契約どおりに出す。"""
    payload = path.with_name(path.name + ".json")
    if document is not None:
        payload.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    path.write_text("#!/bin/sh\n" + (f"cat {payload}\n" if document is not None else "") + f"exit {rc}\n")
    path.chmod(0o755)
    return str(path)


def stage_file(root: Path, operation: str, stage: str) -> Path:
    path = root / "data" / "ops" / "web.stage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"feature": "web", "operation": operation, "stage": stage,
                                "principal": Principal.OPS_WEB.value, "registered": False,
                                "artifacts": [], "wants": [], "ops_active": False, "committed": False}))
    return path


class StubSetup:
    """`interactive_enable` が呼ぶ OpsSetup の最小の偽物 (enable だけ記録する)。"""

    def __init__(self, errors=()):
        from agentic_fx.ops_setup.stages import StageResult
        self.errors = list(errors)
        self.enabled: list[str] = []
        self._result = StageResult
        self.systemd = type("Systemd", (), {"show": lambda *_: {"ActiveState": "inactive"}})()
        self.paths = type("Paths", (), {"afx_unit": "afx.service", "ops_exec": "/bin/true"})()

    def enable(self, feature):
        self.enabled.append(feature)
        if self.errors:
            raise self.errors.pop(0)
        return self._result(feature, "enable", "enabled")


def tree(path: Path) -> dict[str, tuple[bytes | None, int]]:
    """path 配下の file の内容と mode (変更なしの確認用)。"""
    result: dict[str, tuple[bytes | None, int]] = {}
    if not path.exists():
        return result
    for item in sorted(path.rglob("*")):
        if item.is_symlink() or not item.is_file():
            result[str(item)] = (None, item.lstat().st_mode)
        else:
            result[str(item)] = (item.read_bytes(), item.stat().st_mode)
    return result


def effective_uid() -> int:
    return os.geteuid()
