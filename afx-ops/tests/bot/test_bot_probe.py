"""`afx-ops probe bot` の本体 (`probe.main`) を loopback の TLS の偽 Discord REST へ向けて回す。

probe は隔離しない短命 process。試験は同じ process の中で名前解決・CA・API の base URL だけを
試験用に差し替え (subprocess には差し替えの経路を作らない)、入力の読取り・要求・戻りは本物を通す。
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "notify") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "notify"))

from _fake_discord import FakeDiscord  # noqa: E402
from _notify_fixtures import HOST, Dns, make_pki, needs_openssl  # noqa: E402
from _notify_seams import test_ca_context, udp_resolver_factory  # noqa: E402
from afx_ops import probe  # noqa: E402
from afx_ops.bot import probe as bot_probe  # noqa: E402
from afx_ops.isolation.startup import feature_dirs  # noqa: E402

pytestmark = needs_openssl
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.probe-token-secret-part-0123456789"
GUILD, CHANNEL = 123456789012345678, 223456789012345678


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return make_pki(tmp_path_factory.mktemp("pki"))


@pytest.fixture
def servers():
    made = []

    def track(server):
        made.append(server)
        return server
    yield track
    for server in made:
        server.close()


def _install(home, token: str = TOKEN) -> None:
    dirs = feature_dirs("bot")
    dirs.config.mkdir(parents=True, mode=0o700)
    (dirs.config / "ops.toml").write_text(
        f'instance_root = "{home}/absent-root"\nkey_dir = "{home}/absent-keys"\n'
        f"guild_id = {GUILD}\nchannel_id = {CHANNEL}\n"
        "allowlist_user_ids = []\nmention_user_ids = []\n")
    (dirs.config / "ops.toml").chmod(0o600)
    dirs.secret.mkdir(mode=0o700)
    secret = dirs.secret / "discord.token"
    secret.write_text(token + "\n")
    secret.chmod(0o600)


def _fake(servers, pki, leaf="good") -> FakeDiscord:
    cert, key = pki.leaf(leaf)
    fake = servers(FakeDiscord(cert, key, host=HOST))
    fake.token = TOKEN
    fake.channel_guild[CHANNEL] = GUILD
    return fake


def _route(monkeypatch, pki, fake, dns) -> None:
    monkeypatch.setattr(bot_probe, "probe", functools.partial(
        bot_probe.probe, ssl_context=test_ca_context(pki.ca_pem),
        resolver_factory=udp_resolver_factory(dns_port=dns.port, port=fake.port),
        api_base=f"https://{HOST}/api/v10", timeout=5))


def _main(capsys) -> tuple[int, str]:
    rc = probe.main("bot")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert TOKEN not in captured.err and HOST not in captured.err
    return rc, captured.err


def test_probe_checks_identity_and_the_configured_channel_only(home, pki, servers,
                                                               monkeypatch, capsys):
    _install(home)
    fake = _fake(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    _route(monkeypatch, pki, fake, dns)
    assert _main(capsys) == (0, "")
    calls = [(c.method, c.path) for c in fake.snapshot_calls()]
    # identity と channel の 2 回だけ。何も投稿しない、gateway にも繋がない
    assert calls == [("GET", "/users/@me"), ("GET", f"/channels/{CHANNEL}")]
    assert not fake.connected()


@pytest.mark.parametrize("case,reason", [
    ("bad_token", "probe_unauthorized"),
    ("unknown_channel", "probe_channel_unreachable"),
    ("other_guild", "probe_guild_mismatch"),
    ("rogue_cert", "probe_tls_failed"),
    ("no_name", "probe_dns_failed"),
])
def test_probe_failures_are_fixed_reasons(case, reason, home, pki, servers, monkeypatch, capsys):
    _install(home)
    fake = _fake(servers, pki, leaf="rogue" if case == "rogue_cert" else "good")
    if case == "bad_token":
        fake.token = TOKEN + "x"
    elif case == "unknown_channel":
        fake.channel_guild.clear()
    elif case == "other_guild":
        fake.channel_guild[CHANNEL] = GUILD + 1
    dns = servers(Dns([None] if case == "no_name" else ["127.0.0.1"]))
    _route(monkeypatch, pki, fake, dns)
    assert _main(capsys) == (73, f"afx-ops: {reason}\n")
    assert all(c.method == "GET" for c in fake.snapshot_calls())


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c", TOKEN + " extra"])
def test_malformed_token_is_an_input_failure_without_network(token, home, pki, servers,
                                                              monkeypatch, capsys):
    _install(home, token)
    fake = _fake(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    _route(monkeypatch, pki, fake, dns)
    assert _main(capsys) == (71, "afx-ops: token_invalid\n")
    assert fake.snapshot_calls() == []
    assert dns.queries == []
