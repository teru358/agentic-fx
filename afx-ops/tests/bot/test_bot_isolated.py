"""隔離済みの実 process で bot を回す: 実 UDS の偽 API daemon、loopback TLS の偽 Discord REST と
偽 gateway、UDP DNS、実 state dir。外部の Discord には繋がない。

この host の 443 には本物の待受があり、非特権では 443 を bind できないので、Discord 向けの
接続は loopback の試験 port で観測する (resolver の答えに試験 port を載せ、Landlock の connect
許可も試験 port だけにする)。443 だけが connect できることは固定 plan の canary
(test_canary の test_tcp_connect_is_limited_to_443_by_the_fixed_plan) が受け持つ。
"""
from __future__ import annotations

import errno
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for extra in (HERE.parent / "isolation", HERE.parent / "notify"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from _afx_isolation_support import (  # noqa: E402
    Interactive,
    layout_of,
    make_root,
    needs_landlock,
    needs_x86_64,
)
from _bot_fixtures import (  # noqa: E402
    CHANNEL,
    DIGEST_7,
    DIGEST_8,
    GUILD,
    OUTSIDER,
    ROLE,
    TOKEN,
    USER_A,
    USER_B,
    BotDaemon,
    event,
    wait_for,
)
from _fake_discord import BOT_ID, FakeDiscord  # noqa: E402
from _notify_fixtures import HOST, Dns, make_pki, needs_openssl  # noqa: E402

pytestmark = [needs_x86_64, needs_landlock, needs_openssl]

EPERM, EACCES = errno.EPERM, errno.EACCES
DEFER_LIMIT = 3.0
NO_LONGER_ACTIVE = "Approval is no longer active."


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return make_pki(tmp_path_factory.mktemp("pki"))


@pytest.fixture
def root(tmp_path) -> Path:
    root = make_root(tmp_path)
    token = root / "bot" / "secret" / "discord.token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    return root


@pytest.fixture
def daemon(root, tmp_path):
    made = BotDaemon(layout_of(root).socket_path, layout_of(root).data, tmp_path)
    yield made
    made.close()


@pytest.fixture
def discord_fake(pki):
    cert, key = pki.leaf("good")
    fake = FakeDiscord(cert, key, host=HOST)
    fake.token = TOKEN
    fake.channel_guild[CHANNEL] = GUILD
    yield fake
    fake.close()


@pytest.fixture
def dns():
    server = Dns(["127.0.0.1"])
    yield server
    server.close()


class Bot:
    """隔離済み bot の 1 回の起動。"""

    def __init__(self, root, pki, fake, dns, env, prefix=(), bot_config=None, **params) -> None:
        spec = {"feature": "bot", "root": str(root), "op": "bot_runtime",
                "wait": {"interval": 0.05, "limit": 10},
                "bot": {"allowlist_user_ids": [USER_A, USER_B], "mention_role_id": ROLE,
                        "mention_user_ids": [USER_A], **(bot_config or {})},
                "params": {"ca_pem": pki.ca_pem, "port": fake.port, "dns_port": dns.port,
                           "host": HOST, "connect_ports": [fake.port], **params}}
        self.proc = Interactive(spec, env, prefix=prefix)
        self.proc.wait_line("BOT-RUNNING")

    def stop(self):
        self.proc.send("stop")
        got = self.proc.finish(timeout=30)
        assert got.rc == 0, got.stderr[-3000:]
        return got

    def wait_exit(self):
        # stdin を閉じると止まる合図になるので、自分で終わるのを待ってから回収する
        self.proc.proc.wait(timeout=30)
        return self.proc.finish(timeout=30)

    def kill(self) -> None:
        self.proc.kill()


@pytest.fixture
def bots():
    made: list[Bot] = []
    yield made
    for bot in made:
        bot.kill()


def _start(bots, *args, **params) -> Bot:
    bot = Bot(*args, **params)
    bots.append(bot)
    return bot


def _snapshot(root) -> dict | None:
    path = root / "bot" / "state" / "bot.snapshot"
    return json.loads(path.read_text()) if path.exists() else None


def _seed_started(root) -> None:
    """event cursor の初期化が済んだ (cursor 0 から読み始める) snapshot を置く。"""
    path = root / "bot" / "state" / "bot.snapshot"
    path.write_text(json.dumps({"version": 5, "cursor": 0, "high_watermark": 0,
                                "event_cursor_initialized": True, "approvals": []},
                               sort_keys=True))
    path.chmod(0o600)


def _record(root, approval_id) -> dict:
    return next(r for r in _snapshot(root)["approvals"] if r["approval_id"] == approval_id)


def _approval_post(fake, approval_id, digest):
    prefix = f"afx-approval:{approval_id}:{digest}:"
    for message in list(fake.messages.values()):
        if not message.ephemeral and any(str(e.get("footer", {}).get("text")).startswith(prefix)
                                         for e in message.embeds):
            return message
    return None


def _ids(message) -> dict[str, str]:
    """投稿の component の label → custom_id。"""
    return {c["label"]: c["custom_id"] for row in message.components for c in row["components"]}


def _click(fake, message, label, user, **where):
    return fake.click(custom_id=_ids(message)[label], message_id=message.id, user_id=user,
                      guild_id=where.get("guild", GUILD), channel_id=where.get("channel", CHANNEL))


def _followup(fake, interaction_id):
    return wait_for(lambda: fake.followups_for(interaction_id))[0]


def _confirm_message(fake, interaction_id):
    """claim に返った確認 (ephemeral) message。"""
    call = _followup(fake, interaction_id)
    assert call.body.get("components"), call.body
    return next(m for m in fake.messages.values() if m.ephemeral
                and m.components == call.body["components"])


def _defer_delay(fake, interaction_id) -> float:
    call = wait_for(lambda: fake.callback_for(interaction_id))
    assert call.body["type"] == 5 and call.body["data"]["flags"] == 64, call.body
    return call.t - fake.dispatched[interaction_id]


def _start_ready(bots, root, pki, fake, dns, env, daemon, **params) -> Bot:
    bot = _start(bots, root, pki, fake, dns, env, **params)

    def ready():
        if bot.proc.proc.poll() is not None:
            got = bot.wait_exit()
            raise AssertionError(f"bot ended: rc={got.rc} out={got.stdout[-3000:]!r} "
                                 f"err={got.stderr[-3000:]}")
        return fake.connected() and any(k == "identify" for k, _ in fake.gateway_log)
    wait_for(ready)
    return bot


def test_startup_posts_pending_approvals_before_the_gateway_and_denies_network_misuse(
        root, pki, daemon, discord_fake, dns, child_env, bots):
    fake = discord_fake
    daemon.set(approvals={"7": {"status": "pending", "payload_sha256": DIGEST_7}})
    key = str(root / "keys" / "ops-bot.token")
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon,
                       after=[{"op": "bind", "params": {"host": "127.0.0.1"}},
                              {"op": "listen"},
                              {"op": "read_file", "params": {"path": key}}])
    message = wait_for(lambda: _approval_post(fake, 7, DIGEST_7))
    calls = fake.snapshot_calls()
    identify = next(t for k, t in fake.gateway_log if k == "identify")
    login = next(c for c in calls if c.path == "/users/@me")
    post = next(c for c in calls if c.method == "POST" and c.path == f"/channels/{CHANNEL}/messages")
    # login → 承認の投稿 → gateway 接続 の順 (reconcile が済んでから gateway に繋ぐ)
    assert login.t < post.t < identify
    # 投稿は gate の primary View (approve 起点・reject・retry) と固定識別子の footer を持つ
    assert sorted(_ids(message)) == ["Approve", "Reject", "Retry"]
    assert all(cid.startswith("afx:v1:") and cid.endswith(":7") for cid in _ids(message).values())
    # 判断材料の自由文 (daemon の detail にある) は投稿に写さない
    assert "free text" not in message.content and "claim text" not in message.content
    record = _record(root, 7)
    assert (record["state"], record["message_id"], record["display_generation"]) == (
        "fresh", message.id, 0)
    # 決定は 1 件も送っていない。daemon への要求は全部 actor なし (決定の経路以外)
    assert daemon.decisions() == []
    assert all(r["actor"] is None for r in daemon.requests())
    got = bot.stop()
    result = got.result
    assert result["rc"] == 0
    # 隔離後の bot は新しい bind / listen ができず、鍵 dir も読めない
    assert result["after"][0][-2:] == [-1, EPERM]
    assert result["after"][1][-2:] == [-1, EPERM]
    assert result["after"][2] == [-1, EACCES]
    assert TOKEN not in got.stdout + got.stderr


def _pending(*ids) -> dict:
    digests = {7: DIGEST_7, 8: DIGEST_8, 9: "9" * 64}
    return {str(i): {"status": "pending", "payload_sha256": digests[i]} for i in ids}


def _digest(approval_id) -> str:
    return {7: DIGEST_7, 8: DIGEST_8, 9: "9" * 64}[approval_id]


def _posts(fake, *ids):
    return {i: wait_for(lambda i=i: _approval_post(fake, i, _digest(i))) for i in ids}


def _assert_quick_defer(fake, interaction_id, limit=DEFER_LIMIT) -> float:
    delay = _defer_delay(fake, interaction_id)
    assert 0 <= delay < limit, delay
    return delay


_RETIRED_SUMMARY = re.compile(r"承認 #\d+\n\n状態: (承認済み|却下|失効)\n\n旧形式のため要約なし")


def _retired(fake, message) -> bool:
    """View が外れ、本文が終端の表示 (決定結果が取れれば状態、取れなければ固定の代替文)。"""
    return message.components == [] and (
        message.content == NO_LONGER_ACTIVE or _RETIRED_SUMMARY.fullmatch(message.content) is not None)


def _primary_edits(fake, message, since=0):
    """承認の投稿に primary View を付け直した bot token の edit。"""
    return [c for c in fake.snapshot_calls()[since:] if c.method == "PATCH"
            and c.path == f"/channels/{CHANNEL}/messages/{message.id}"
            and sorted(x["label"] for row in c.body.get("components", [])
                       for x in row["components"]) == ["Approve", "Reject", "Retry"]]


def test_rejected_interactions_never_send_a_decision(root, pki, daemon, discord_fake, dns,
                                                     child_env, bots):
    fake = discord_fake
    daemon.set(approvals=_pending(7, 8, 9))
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    posts = _posts(fake, 7, 8, 9)
    m7, m8, m9 = posts[7], posts[8], posts[9]

    # allowlist 外の利用者: 記録は変わらない
    i = _click(fake, m7, "Approve", OUTSIDER)
    _assert_quick_defer(fake, i)
    assert "not allowed" in _followup(fake, i).body["content"]
    assert _record(root, 7)["state"] == "fresh"
    # View の dispatch 表に無い改変 custom_id と未知の approval ID: gate が無変更で拒否する
    for custom_id in ("afx:v1:claim:7x", "afx:v1:claim:999", "afx:v2:claim:7"):
        i = fake.click(custom_id=custom_id, message_id=m7.id, user_id=USER_A, guild_id=GUILD,
                       channel_id=CHANNEL)
        _assert_quick_defer(fake, i)
        _followup(fake, i)
        assert _record(root, 7)["state"] == "fresh"
    # A が claim、B の最初のボタンは claim 済みで拒否 (記録は不変)、B の confirm は無効化
    claim = _click(fake, m7, "Approve", USER_A)
    _assert_quick_defer(fake, claim)
    confirm_message = _confirm_message(fake, claim)
    record = _record(root, 7)
    assert (record["state"], record["actor"], record["action"]) == (
        "claimed", f"discord:{USER_A}", "approve")
    i = _click(fake, m7, "Reject", USER_B)
    _assert_quick_defer(fake, i)
    assert "already claimed" in _followup(fake, i).body["content"]
    assert _record(root, 7)["state"] == "claimed"
    mark = len(fake.snapshot_calls())
    i = _click(fake, confirm_message, "Confirm", USER_B)
    _assert_quick_defer(fake, i)
    _followup(fake, i)
    # 他人の confirm は claim も世代も動かさず、投稿の表示も更新しない
    record = _record(root, 7)
    assert (record["state"], record["actor"], record["display_generation"]) == (
        "claimed", f"discord:{USER_A}", 0)
    assert not _primary_edits(fake, m7, mark)
    # claim 者本人の cancel で解けて、同じ世代の primary View から別の利用者が claim できる
    i = _click(fake, confirm_message, "Cancel", USER_A)
    _assert_quick_defer(fake, i)
    _followup(fake, i)
    record = _record(root, 7)
    assert (record["state"], record["actor"], record["display_generation"]) == ("fresh", None, 0)
    # claim を経ない confirm (primary の投稿に無い component) と、別 channel・別 guild からの
    # 操作: 解く claim が無い fresh の record は変えない
    i = fake.click(custom_id="afx:v1:confirm:8", message_id=m8.id, user_id=USER_A,
                   guild_id=GUILD, channel_id=CHANNEL)
    _assert_quick_defer(fake, i)
    _followup(fake, i)
    i = _click(fake, m9, "Approve", USER_A, channel=CHANNEL + 1)
    _assert_quick_defer(fake, i)
    _followup(fake, i)
    for approval_id in (8, 9):
        record = _record(root, approval_id)
        assert (record["state"], record["display_generation"]) == ("fresh", 0)
    claim = _click(fake, m7, "Reject", USER_B)
    _assert_quick_defer(fake, claim)
    _confirm_message(fake, claim)
    record = _record(root, 7)
    assert (record["state"], record["actor"], record["action"]) == (
        "claimed", f"discord:{USER_B}", "reject")
    bot.stop()
    # どの拒否でも daemon に決定は届かず、承認の再読取り (決定の前段) にも進まない
    assert daemon.decisions() == []
    assert not [r for r in daemon.requests() if r["path"].startswith("/v1/jobs/")]


def test_confirm_defers_before_the_slow_detail_and_the_loop_keeps_answering(
        root, pki, daemon, discord_fake, dns, child_env, bots):
    fake = discord_fake
    daemon.set(approvals=_pending(7, 8))
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    posts = _posts(fake, 7, 8)
    claim = _click(fake, posts[8], "Approve", USER_A)
    confirm_message = _confirm_message(fake, claim)
    daemon.set(detail_delay=4)
    confirm = _click(fake, confirm_message, "Confirm", USER_A)
    defer = wait_for(lambda: fake.callback_for(confirm))
    assert defer.t - fake.dispatched[confirm] < DEFER_LIMIT
    # worker thread が daemon の遅い応答を待つ間も、event loop は別の interaction に即 defer する
    detail = wait_for(lambda: [r for r in daemon.requests()
                               if r["method"] == "GET" and r["path"] == "/v1/approvals/8"
                               and r["t"] > fake.dispatched[confirm]])[0]
    other = _click(fake, posts[7], "Approve", OUTSIDER)
    other_defer = wait_for(lambda: fake.callback_for(other))
    assert other_defer.t - fake.dispatched[other] < 1.0
    assert other_defer.t < detail["t"] + 4
    # approval の再読取りは defer の後 (defer の前に API を呼ばない)
    assert detail["t"] > defer.t
    _followup(fake, confirm)
    bot.stop()


def test_confirmed_decision_and_job_poll_carry_the_same_actor(root, pki, daemon, discord_fake,
                                                             dns, child_env, bots):
    fake = discord_fake
    daemon.set(approvals=_pending(8), decide={"status": 202, "job_id": "b" * 32, "settles": True},
               job={"state": "done"})
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    message = _posts(fake, 8)[8]
    claim = _click(fake, message, "Approve", USER_A)
    confirm = _click(fake, _confirm_message(fake, claim), "Confirm", USER_A)
    _assert_quick_defer(fake, confirm)
    wait_for(lambda: daemon.decisions())
    decisions = daemon.decisions()
    assert [(d["path"], d["actor"]) for d in decisions] == [
        ("/v1/approvals/8/approve", f"discord:{USER_A}")]
    jobs = wait_for(lambda: [r for r in daemon.requests() if r["path"].startswith("/v1/jobs/")])
    assert [(j["path"], j["actor"]) for j in jobs] == [(f"/v1/jobs/{'b' * 32}", f"discord:{USER_A}")]
    wait_for(lambda: _retired(fake, message))
    # 終端の表示を外せた record は snapshot から消える (再起動のたびに edit し直さない)
    wait_for(lambda: _snapshot(root)["approvals"] == [])
    bot.stop()
    size = (root / "bot" / "state" / "bot.snapshot").stat().st_size
    mark = len(fake.snapshot_calls())
    again = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    again.stop()
    # 正本で pending でない承認は adopt も再投稿もしない。終端の投稿への edit も 0 回
    assert not [c for c in fake.snapshot_calls()[mark:] if c.path.startswith("/channels/")
                and c.method in {"PATCH", "POST"}]
    assert _snapshot(root)["approvals"] == []
    assert (root / "bot" / "state" / "bot.snapshot").stat().st_size == size


def test_restart_invalidates_open_claims_edits_then_adopts_before_the_gateway(
        root, pki, daemon, discord_fake, dns, child_env, bots):
    fake = discord_fake
    daemon.set(approvals=_pending(7, 8))
    first = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    posts = _posts(fake, 7, 8)
    claim = _click(fake, posts[7], "Approve", USER_A)
    old_confirm = _confirm_message(fake, claim)
    assert _record(root, 7)["state"] == "claimed"
    first.stop()
    fake.gateway_log.clear()
    mark = len(fake.snapshot_calls())

    second = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    identify = next(t for k, t in fake.gateway_log if k == "identify")
    calls = fake.snapshot_calls()[mark:]
    # 再起動前の確認は全部失効して世代を進めた fresh に戻り、その投稿は adopt して View を付け直す
    record = _record(root, 7)
    assert (record["state"], record["actor"], record["display_generation"]) == ("fresh", None, 1)
    edit7 = next(c for c in calls if c.method == "PATCH"
                 and c.path == f"/channels/{CHANNEL}/messages/{posts[7].id}")
    assert sorted(c["label"] for row in edit7.body["components"] for c in row["components"]) == [
        "Approve", "Reject", "Retry"]
    # adopt の edit で表示が新しい世代に揃ったので、表示更新の記録は残らない
    assert _record(root, 7)["edit_pending"] is False
    # 未終端の投稿は footer・author・channel を照合して adopt し、View を付け直す (repost しない)
    fetch8 = next(c for c in calls if c.method == "GET"
                  and c.path == f"/channels/{CHANNEL}/messages/{posts[8].id}")
    adopt8 = next(c for c in calls if c.method == "PATCH"
                  and c.path == f"/channels/{CHANNEL}/messages/{posts[8].id}")
    assert sorted(c["label"] for row in adopt8.body["components"] for c in row["components"]) == [
        "Approve", "Reject", "Retry"]
    assert not [c for c in calls if c.method == "POST" and c.path.startswith("/channels/")]
    assert edit7.t < identify and fetch8.t < adopt8.t < identify
    assert _record(root, 8)["state"] == "fresh"
    assert _record(root, 8)["message_id"] == posts[8].id
    # 起動直後の旧 message の click と、再起動前の確認 message の click: 決定は送らない
    i = fake.click(custom_id="afx:v1:claim:7", message_id=posts[7].id, user_id=USER_A,
                   guild_id=GUILD, channel_id=CHANNEL)
    _assert_quick_defer(fake, i)
    _followup(fake, i)
    i = _click(fake, old_confirm, "Confirm", USER_A)
    _assert_quick_defer(fake, i)
    _followup(fake, i)
    assert _record(root, 7)["state"] == "fresh"
    second.stop()
    assert daemon.decisions() == []


def test_crash_after_posting_an_approval_adopts_the_post_instead_of_reposting(
        root, pki, daemon, discord_fake, dns, child_env, bots):
    fake = discord_fake
    daemon.set(approvals=_pending(7))
    crashed = _start(bots, root, pki, fake, dns, child_env, exit_before_record=True)
    got = crashed.wait_exit()
    assert got.rc == 87, got.stderr[-2000:]
    first = _approval_post(fake, 7, DIGEST_7)
    assert first is not None
    assert _snapshot(root)["approvals"] == []
    # 投稿の後・対応の確定の前に止まった。再起動は Discord 上の自分の投稿を固定識別子で探す
    fake.gateway_log.clear()
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    record = wait_for(lambda: next((r for r in _snapshot(root)["approvals"]
                                    if r["approval_id"] == 7), None))
    assert record["message_id"] == first.id
    assert len(fake.channel_posts(CHANNEL)) == 1
    wait_for(lambda: sorted(_ids(first)) == ["Approve", "Reject", "Retry"])
    bot.stop()


def test_events_are_posted_with_configured_mentions_at_least_once_across_a_crash(
        root, pki, daemon, discord_fake, dns, child_env, bots):
    fake = discord_fake
    _seed_started(root)
    daemon.set(events=[event(1)])
    crashed = _start(bots, root, pki, fake, dns, child_env, exit_before_commit=True)
    got = crashed.wait_exit()
    assert got.rc == 86, got.stderr[-2000:]
    assert len(fake.channel_posts(CHANNEL)) == 1
    snap = _snapshot(root)
    assert (snap["cursor"], snap["high_watermark"]) == (0, 1)
    # 投稿の後・cursor の確定の前に止まった event は、再起動後にもう一度投稿する (at-least-once)
    daemon.set(approvals=_pending(7))
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    wait_for(lambda: _snapshot(root)["cursor"] == 1)
    posts = [c for c in fake.channel_posts(CHANNEL) if not c.body.get("embeds")]
    assert len(posts) == 2
    for post in posts:
        assert post.body["content"] == (f"<@&{ROLE}> <@{USER_A}> job_state_changed: "
                                        "job_kind=ask, result_code=ok, state=done")
        assert post.body["allowed_mentions"] == {"parse": [], "roles": [ROLE],
                                                 "users": [USER_A]}
    # 承認の決着 event: 正本を読み直し、pending でなければ record を終端にして View を外す
    message = _posts(fake, 7)[7]
    daemon.set(events=[event(1), event(2, code="approval_state_changed", approval_id=7,
                                       state="approved")],
               approvals={"7": {"status": "approved", "payload_sha256": DIGEST_7}})
    wait_for(lambda: _snapshot(root)["cursor"] == 2)
    wait_for(lambda: _retired(fake, message))
    wait_for(lambda: _snapshot(root)["approvals"] == [])
    # decided_by は投稿に写さない
    assert all("operator" not in (c.body or {}).get("content", "")
               for c in fake.channel_posts(CHANNEL))
    bot.stop()


def test_ac25_a_first_start_over_an_existing_history_posts_nothing_and_then_follows_new_events(
        root, pki, daemon, discord_fake, dns, child_env, bots):
    fake = discord_fake
    daemon.set(events=[event(1), event(2), event(3, code="approval_state_changed",
                                                 approval_id=9, state="approved")])
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    wait_for(lambda: (_snapshot(root) or {}).get("event_cursor_initialized") is True)
    snap = _snapshot(root)
    assert (snap["version"], snap["cursor"], snap["high_watermark"]) == (5, 3, 3)
    assert fake.channel_posts(CHANNEL) == []
    daemon.set(events=[event(1), event(2), event(3, code="approval_state_changed",
                                                 approval_id=9, state="approved"), event(4)])
    wait_for(lambda: _snapshot(root)["cursor"] == 4)
    assert len(fake.channel_posts(CHANNEL)) == 1
    bot.stop()


def _trade_order(event_id: int, state: str) -> dict:
    return {"id": event_id, "ts": "2026-10-20T00:00:00+00:00", "code": "order_state_changed",
            "ref": "order:12",
            "fields": {"state": state, "pair": "USDJPY", "side": "long", "entry_type": "limit",
                       "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": "150.9",
                       "origin_code": "cron", "mode": "paper", "state_mode": None,
                       "reason_code": None}}


def _trade_close(event_id: int) -> dict:
    return {"id": event_id, "ts": "2026-10-20T00:00:00+00:00", "code": "position_closed",
            "ref": "order:12",
            "fields": {"pair": "USDJPY", "side": "long", "qty": "0.1", "entry_price": "150.123",
                       "close_price": "150.9", "pnl": "7770.00", "ccy": "JPY", "pnl_r": "2.40",
                       "held_sec": 11520, "reason_code": "tp", "origin_code": "cron",
                       "mode": "paper", "state_mode": None}}


def test_ac24_trade_settings_read_before_isolation_apply_in_the_isolated_bot(
        root, pki, daemon, discord_fake, dns, child_env, bots):
    fake = discord_fake
    _seed_started(root)
    daemon.set(events=[_trade_order(1, "placed"), _trade_order(2, "filled"), _trade_close(3),
                       event(4)])
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon,
                       bot_config={"trade_notices": "results", "trade_amounts": "relative"})
    wait_for(lambda: _snapshot(root)["cursor"] == 4)
    posts = [c for c in fake.channel_posts(CHANNEL) if not c.body.get("embeds")]
    contents = [c.body["content"] for c in posts]
    assert len(contents) == 3
    assert contents[0].startswith("[ペーパー] 約定しました #12 USDJPY 買い")
    assert contents[1].startswith("[ペーパー] 決済しました #12 USDJPY 買い (利確到達)")
    assert "lot" not in contents[0] + contents[1] and "7,770" not in contents[1]
    # 取引通知は設定済みの mention を付けない。承認以外の既存の event は従来どおり付ける
    for post in posts[:2]:
        assert post.body["allowed_mentions"] == {"parse": []}
        assert "<@" not in post.body["content"]
    assert posts[2].body["allowed_mentions"] == {"parse": [], "roles": [ROLE], "users": [USER_A]}
    bot.stop()


def test_gateway_drop_resumes_and_interactions_still_defer(root, pki, daemon, discord_fake, dns,
                                                           child_env, bots):
    fake = discord_fake
    daemon.set(approvals=_pending(7))
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon)
    message = _posts(fake, 7)[7]
    fake.drop()
    wait_for(lambda: any(k == "resume" for k, _ in fake.gateway_log) and fake.connected())
    i = _click(fake, message, "Approve", USER_A)
    _assert_quick_defer(fake, i)
    _confirm_message(fake, i)
    assert _record(root, 7)["state"] == "claimed"
    fake.drop()
    wait_for(lambda: [k for k, _ in fake.gateway_log].count("resume") == 2 and fake.connected())
    bot.stop()
    assert daemon.decisions() == []


_THREAD_FLAGS = ("CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM|"
                 "CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID")


def _fds(lines, prefix) -> set[str]:
    return {m.group(1) for line in lines if line.startswith(prefix)
            for m in [re.search(r"= (\d+)$", line)] if m}


@pytest.mark.skipif(shutil.which("strace") is None, reason="strace が無い")
def test_bot_network_and_thread_syscalls_under_the_bot_profile(root, pki, daemon, discord_fake,
                                                                dns, child_env, bots, tmp_path):
    # 隔離後は PR_SET_DUMPABLE=0 で strace が引数の中身を読めないので、syscall 名・flags・
    # fd・address 長・戻り値で見る
    fake = discord_fake
    daemon.set(approvals=_pending(7))
    trace = tmp_path / "bot.trace"
    bot = _start_ready(bots, root, pki, fake, dns, child_env, daemon,
                       prefix=("strace", "-f", "-qq", "-e", "trace=%network,clone,clone3",
                               "-o", str(trace)))
    message = _posts(fake, 7)[7]
    fake.drop()
    wait_for(lambda: any(k == "resume" for k, _ in fake.gateway_log) and fake.connected())
    i = _click(fake, message, "Approve", USER_A)
    _assert_quick_defer(fake, i)
    _confirm_message(fake, i)
    bot.stop()
    lines = [re.sub(r"^\d+ +", "", line) for line in trace.read_text().splitlines()]
    tcp = _fds(lines, "socket(AF_INET, SOCK_STREAM")
    udp = _fds(lines, "socket(AF_INET, SOCK_DGRAM")
    # REST・gateway・再接続の gateway が TCP connect で、名前解決が宛先付き UDP で通る
    connects = [line for line in lines
                if re.match(rf"connect\(({'|'.join(tcp)}), 0x[0-9a-f]+, 16\) += (0|-1 EINPROGRESS)",
                            line)]
    assert len(connects) >= 3, connects
    assert udp and any(re.match(rf"sendto\(({'|'.join(udp)}), 0x[0-9a-f]+, \d+, 0, "
                                r"0x[0-9a-f]+, 16\) += \d+", line) for line in lines)
    # 業務処理の thread (gate の worker・heartbeat・停止待ち) は thread flags 完全一致の clone だけ
    clones = [line for line in lines if line.startswith("clone(")]
    assert clones and all(f"flags={_THREAD_FLAGS}" in line and not re.search(r"= -1", line)
                          for line in clones), clones
    assert all("ENOSYS" in line for line in lines if line.startswith("clone3("))
    # listener を作らない: bind / listen / accept は現れない
    assert not [line for line in lines if re.match(r"(bind|listen|accept4?)\(", line)]
    denied = [line for line in lines if re.search(r"= -1 E(PERM|ACCES|AFNOSUPPORT)\b", line)]
    assert denied == []
