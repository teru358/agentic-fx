"""connect(): ターミナルが既に同じ口座でログイン済みなら login() を呼ばない。

OANDA の TOTP 必須化 (2026-10-31) 後、login() は新規認証として OTP を要求し得るため、
ターミナル側で済ませた認証に相乗りする (attach) のが唯一の無人経路。
"""
from __future__ import annotations

from types import SimpleNamespace

import mt5_client
from mt5_client import Mt5Client


class _FakeMt5:
    def __init__(self, current_login):
        self.calls: list[str] = []
        self._current_login = current_login

    def initialize(self):
        self.calls.append("initialize")
        return True

    def account_info(self):
        self.calls.append("account_info")
        if self._current_login is None:
            return None
        return SimpleNamespace(login=self._current_login)

    def login(self, login, password, server):
        self.calls.append(f"login:{login}")
        return True

    def last_error(self):
        return (0, "ok")

    def shutdown(self):
        self.calls.append("shutdown")


def _client(fake, monkeypatch):
    monkeypatch.setattr(mt5_client, "_import_mt5", lambda: fake)
    return Mt5Client(login=900190075, password="x", server="OANDA-Japan MT5 Live")


def test_attach_only_when_terminal_already_on_same_account(monkeypatch):
    fake = _FakeMt5(current_login=900190075)
    c = _client(fake, monkeypatch)
    c.connect()
    assert c.is_connected
    assert "login:900190075" not in fake.calls


def test_attach_when_terminal_login_is_str(monkeypatch):
    # account_info().login が str で返っても int 比較で attach 扱いにする (防御的)
    fake = _FakeMt5(current_login="900190075")
    c = _client(fake, monkeypatch)
    c.connect()
    assert c.is_connected
    assert "login:900190075" not in fake.calls


def test_login_when_terminal_on_other_or_no_account(monkeypatch):
    for current in (None, 123):
        fake = _FakeMt5(current_login=current)
        c = _client(fake, monkeypatch)
        c.connect()
        assert c.is_connected
        assert "login:900190075" in fake.calls
