"""web の公開範囲の対話。enable と reconfigure が同じ質問と同じ前処理・probe の周回を使う。

質問の順は 2 port → 3 自機のみ / LAN → 4 待受 address → 5 誰が見られるか → 6 allow →
7 / 8 password。LAN の待受候補は `afx-ops interfaces` の結果 (`candidates`) だけを使い、
手入力の fallback は設けない。EOF と取消しは例外 (`EOFError` / `KeyboardInterrupt`) のまま
呼び出し側へ返し、ここでは何も書かない。
"""
from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentic_fx.ops_setup.feature_config import (UNRESTRICTED, InputError, InterfaceLookupFailed, is_loopback,
                                                 validate)
from agentic_fx.ops_setup.interfaces import Candidate
from agentic_fx.ops_setup.web_setup import (
    SecretPlan,
    WebSetupError,
    apply_target,
    auth_of,
    check_password,
    generate_password,
    web_secret_state,
)

DEFAULT_PORT = 8788
UNRESTRICTED_WARNING = ("LAN、VPN、port-forward 等を含め、このアドレスへ到達できる全 IPv4 source が"
                        "戦略の diff を読めます")
ONE_QUESTION = "この設定で有効化します。[1] このまま (既定) [2] 変更する"
MAX_PROBES = 3
_INVALID = "afx ops: setup_input_invalid"

# 質問の段
PORT, SCOPE, BIND, WHO = "port", "scope", "bind", "who"
FULL = frozenset({PORT, SCOPE, BIND, WHO})
# probe 失敗の reason ごとに再質問する段 (無関係な値は再質問しない)
RETRY_STEPS = {
    "config_invalid": frozenset({PORT, SCOPE, BIND, WHO}),
    "allow_required": frozenset({WHO}),
    "bind_not_local": frozenset({BIND}),
    "probe_bind_failed": frozenset({PORT, BIND}),
    "secret_missing": frozenset({WHO}),
    "secret_unexpected": frozenset({WHO}),
}
# 再質問せず、その試行を直ちに終える reason
STOP_REASONS = frozenset({"interface_lookup_failed", "secret_invalid", "config_dir_invalid"})


class IO(Protocol):
    def isatty(self) -> bool: ...
    def ask(self, label: str, default: str | None = None) -> str | None: ...
    def ask_secret(self, label: str) -> str: ...
    def stdout(self, text: str) -> None: ...
    def stderr(self, text: str) -> None: ...


@dataclass
class Session:
    root: Path
    io: IO
    probe: Callable[[], tuple[int, str]]
    candidates: Callable[[], Sequence[Candidate]]
    hook: Callable[[str], None] | None = None
    token_bytes: Callable[[int], bytes] = secrets.token_bytes
    before_probe: Callable[[], None] | None = None
    echo_each: bool = True
    probes: int = field(default=0, init=False)

    # --- 質問 -----------------------------------------------------------------------------

    def _invalid(self) -> None:
        self.io.stderr(_INVALID)

    def _choose(self, label: str, options: Sequence[str], default: int | None) -> int:
        text = label + " " + " ".join(f"[{number}] {option}" + (" (既定)" if number == default else "")
                                      for number, option in enumerate(options, 1))
        while True:
            answer = self.io.ask(text)
            if answer is None or answer == "":
                if default is not None:
                    return default
            elif answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer)
            self._invalid()

    def _ask_port(self, default: int) -> int:
        while True:
            answer = (self.io.ask("port", str(default)) or "").strip()
            if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= 65535 and str(int(answer)) == answer:
                return int(answer)
            self._invalid()

    def _ask_bind(self, current: str | None) -> str:
        try:
            found = list(self.candidates())
        except InterfaceLookupFailed:
            raise WebSetupError("interface_lookup_failed") from None
        if not found:
            raise WebSetupError("bind_not_local")
        default = None
        if len(found) == 1:
            default = 1
        elif current is not None:
            default = next((n for n, c in enumerate(found, 1) if c.address == current), None)
        options = [f"{c.address} ({c.scope})" for c in found]
        return found[self._choose("待受アドレス", options, default) - 1].address

    def _ask_allow(self, default: Sequence[str] | None) -> list[str]:
        shown = ", ".join(default) if default else None
        while True:
            answer = self.io.ask("許可する IPv4 / CIDR (カンマ区切り)", shown)
            items = [item.strip() for item in (answer or "").split(",")]
            if not answer or any(not item for item in items):
                self._invalid()
                continue
            try:
                allow = validate("web", {"port": 1, "allow": items})["allow"]
            except InputError:
                self._invalid()
                continue
            if UNRESTRICTED in allow:
                self._invalid()
                continue
            return allow

    def _confirm_unrestricted(self) -> bool:
        self.io.stdout(UNRESTRICTED_WARNING)
        answer = self.io.ask("この設定で公開してよい場合は yes と入力してください")
        return (answer or "").strip() == "yes"

    def _ask_secret(self, auth: str, live: str) -> SecretPlan | None:
        """password の選択。None は「認証方式を選び直す」。"""
        if auth == "basic":
            if live == "safe":
                choice = self._choose("password", ["既存の password を再利用する (表示しません)",
                                                    "新しく生成する", "自分で入力する"], 1)
                if choice == 1:
                    return SecretPlan("keep")
                generate = choice == 2
            else:
                generate = self._choose("password", ["生成する", "自分で入力する"], 1) == 1
            if generate:
                return SecretPlan("install", generate_password(self.token_bytes), generated=True)
            while True:
                try:
                    return SecretPlan("install", check_password(self.io.ask_secret("password (16〜128 文字)")))
                except WebSetupError:
                    self._invalid()
        if live == "safe":
            choice = self._choose("既存の password が残っています。認証なしにして削除します。",
                                  ["削除して認証なしにする", "認証方式を選び直す"], None)
            return SecretPlan("remove") if choice == 1 else None
        return SecretPlan("absent")

    def _ask_who(self, cur: dict[str, Any], live: str) -> SecretPlan:
        allow, auth = cur.get("allow"), auth_of(cur)
        if allow == [UNRESTRICTED]:
            default = 2 if auth == "basic" else 4
        elif allow:
            default = 3 if auth == "basic" else 1
        else:
            default = 1
        while True:
            who = self._choose("誰が見られるか", ["許可 IP だけ", "パスワード", "両方",
                                                  "この待受アドレスへ到達できる全 IPv4"], default)
            if who in (1, 3):
                limited = allow if allow and allow != [UNRESTRICTED] else None
                chosen = self._ask_allow(limited)
            else:
                chosen = [UNRESTRICTED]
                if who == 4 and not self._confirm_unrestricted():
                    continue
            new_auth = "basic" if who in (2, 3) else "none"
            plan = self._ask_secret(new_auth, live)
            if plan is None:
                continue
            cur["allow"], cur["auth"] = chosen, new_auth
            return plan

    def collect(self, defaults: dict[str, Any], live: str, steps: frozenset[str]) -> tuple[dict[str, Any], SecretPlan]:
        """目標の web 4 key と secret の計画を質問で決める。defaults は live の単独の文法を通る値。"""
        cur = dict(defaults)
        if WHO in steps and is_loopback(cur):
            # 自機のみの設定では認証を選べない。公開範囲から聞き直す。
            steps = steps | {SCOPE}
        if PORT in steps or not isinstance(cur.get("port"), int):
            port = cur.get("port")
            cur["port"] = self._ask_port(port if isinstance(port, int) else DEFAULT_PORT)
        while True:
            lan = not is_loopback(cur)
            if SCOPE in steps:
                chosen = self._choose("公開範囲", ["自機のみ", "LAN"], 2 if lan else 1) == 2
                if not chosen:
                    for name in ("allow", "auth") if not lan else ("bind", "allow", "auth"):
                        cur.pop(name, None)
                elif not lan:
                    cur.pop("bind", None)
                lan = chosen
            plan: SecretPlan | None = None
            if lan and (SCOPE in steps or BIND in steps):
                cur["bind"] = self._ask_bind(cur.get("bind"))
            if lan and (SCOPE in steps or WHO in steps):
                plan = self._ask_who(cur, live)
            elif not lan and (SCOPE in steps or WHO in steps):
                cur.pop("allow", None)
                cur.pop("auth", None)
                plan = self._ask_secret("none", live)
                if plan is None:
                    continue
            return self._normalize(cur), plan if plan is not None else self._carry(cur, live)

    @staticmethod
    def _carry(cur: dict[str, Any], live: str) -> SecretPlan:
        """secret を聞かない段の計画。live と目標の auth が整合していれば何もしない。"""
        if auth_of(cur) == "basic" and live == "safe":
            return SecretPlan("keep")
        if auth_of(cur) == "none" and live == "absent":
            return SecretPlan("absent")
        raise WebSetupError("setup_input_required", 2)

    @staticmethod
    def _normalize(cur: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"port": cur["port"]}
        if is_loopback(cur):
            if cur.get("bind", "127.0.0.1") != "127.0.0.1":
                result["bind"] = cur["bind"]
            return result
        if "bind" not in cur or "allow" not in cur:
            raise WebSetupError("setup_input_required", 2)
        result.update({"bind": cur["bind"], "allow": cur["allow"], "auth": auth_of(cur)})
        return result

    # --- 前処理と probe の周回 -------------------------------------------------------------

    def converge(self, values: dict[str, Any], plan: SecretPlan, *, interactive: bool,
                 show_password: Callable[[str], None],
                 reason_code: Callable[[object], str]) -> tuple[int, str] | None:
        """(a) secret → (b) config → probe を、成功するか打ち切るまで繰り返す。成功は None、失敗は (rc, reason)。

        再質問は変わった項目だけ (a) / (b) をやり直す。反映済みの password や削除は再実行しない。
        """
        while True:
            apply_target(self.root, values, plan, hook=self.hook, show_password=show_password, clean=False)
            if self.before_probe is not None:
                self.before_probe()
            self.probes += 1
            rc, raw = self.probe()
            if rc == 0:
                if self.hook is not None:
                    self.hook("probe_succeeded")
                return None
            reason = reason_code(raw)
            if self.echo_each:
                self.io.stderr(f"afx-ops: {reason}")
            if rc not in (71, 73) or reason in STOP_REASONS or not interactive or self.probes >= MAX_PROBES:
                return 1, reason
            steps = RETRY_STEPS.get(reason)
            if steps is None:
                return 1, reason
            if reason == "probe_bind_failed" and is_loopback(values):
                steps = frozenset({PORT})
            values, plan = self.collect(values, web_secret_state(self.root), steps)
