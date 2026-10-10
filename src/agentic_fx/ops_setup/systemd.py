"""systemd user manager に対する小さい境界。"""
from __future__ import annotations

import subprocess
from collections.abc import Callable

from agentic_fx._safe_error import safe_text


_STDERR_TAIL_BYTES = 200
# is-active が止まったと答える状態。not-found の unit は rc 4 で inactive を返す (systemd 259 実測)。
_STOPPED_STATES = frozenset({"inactive", "failed", "unknown"})
# 起動を終えて動いている状態。activating はまだ起動 job の途中 (Type=notify の READY 前)。
_UP_STATES = frozenset({"active", "reloading", "refreshing"})
# 遷移中など、動いてもいないが止まりきってもいない状態。
_BETWEEN_STATES = frozenset({"activating", "deactivating", "maintenance"})


class SystemdError(RuntimeError):
    """systemd が要求された状態へ遷移できなかった。

    reason は固定語、detail は原因を読むための補足 (rc と stderr の末尾など)。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def _stderr_tail(stderr: str | None) -> str:
    # stage file と status に残るので、URL と秘密らしき値を先に伏せる。切ってから伏せると
    # "token=" の前置きだけが落ちて値が素のまま残り得る。
    # 末尾を byte で切ってから decode する。多 byte 文字の途中で切れても例外にしない。
    raw = safe_text(" ".join((stderr or "").split("\n")).strip()).encode()[-_STDERR_TAIL_BYTES:]
    return raw.decode(errors="ignore").strip()


Run = Callable[..., subprocess.CompletedProcess[str]]


class Systemd:
    """user manager の外部操作を、試験で差し替えられる一箇所に閉じる。"""

    def __init__(self, run: Run = subprocess.run) -> None:
        self._run = run

    def _call(self, *args: str) -> subprocess.CompletedProcess[str]:
        # systemctl が無い・実行できない環境でも、呼び出し側は SystemdError だけを扱えばよい。
        try:
            return self._run(["systemctl", "--user", *args], text=True, capture_output=True,
                             check=False)
        except OSError as error:
            raise SystemdError("unavailable", " ".join(f"{type(error).__name__}: {error}".split())) from error

    def _invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._call(*args)
        if result.returncode:
            raise SystemdError(args[0], f"rc={result.returncode} stderr={_stderr_tail(result.stderr)}")
        return result

    def stop(self, unit: str) -> None:
        self._invoke("stop", unit)

    def start(self, unit: str) -> None:
        self._invoke("start", unit)

    def ensure_active(self, unit: str) -> None:
        if not self.is_active(unit):
            self.start(unit)

    def _state(self, unit: str) -> str:
        """is-active の答えを状態名で返す。答えを得られなければ unavailable。

        bus 接続失敗なども非零終了になるので、rc だけで「止まった」と読まない。
        """
        result = self._call("is-active", unit)
        state = (result.stdout or "").strip()
        if state in _UP_STATES | _STOPPED_STATES | _BETWEEN_STATES:
            return state
        if not state and result.returncode in (0, 3):
            return "active" if result.returncode == 0 else "inactive"
        raise SystemdError("unavailable", f"rc={result.returncode} stderr={_stderr_tail(result.stderr)}")

    def is_active(self, unit: str) -> bool:
        """起動を終えて動いているときだけ True。activating は False (start が起動完了まで待つ)。"""
        return self._state(unit) in _UP_STATES

    def is_stopped(self, unit: str) -> bool:
        """止まっていると manager が答えたときだけ True。遷移中は止まったと扱わない。"""
        return self._state(unit) in _STOPPED_STATES

    def daemon_reload(self) -> None:
        self._invoke("daemon-reload")

    def show(self, unit: str, *props: str) -> dict[str, str]:
        result = self._invoke("show", unit, *[f"--property={prop}" for prop in props])
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return {prop: values.get(prop, "") for prop in props}
