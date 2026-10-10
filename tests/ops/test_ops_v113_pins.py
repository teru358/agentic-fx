"""manifest v2・enroll の永続化手順・申告主体の検査層・autopilot 中の bot 決定を固定する。

- manifest の version は int の 1 / 2 だけ (bool や float は同値比較で通さない)
- revoked entry は digest を持たない (sha256 は null)
- enroll は無変更のとき False を返し、CLI も「登録済み」と表示する
- active entry の sha256 欠落は fail closed
- 新鍵は一時 file を fsync してから rename し、その後 dir を fsync、最後に `.ready`
- 申告主体の形式は ``discord:`` 接頭辞と ASCII 数字だけ (検査関数単体でも)
- service 層も ops-bot の申告欠落を断る (API 層の検査に頼らない)
- autopilot 中は ops-bot の決定も autopilot_restricted
"""
from __future__ import annotations

import os
import stat

import pytest

from agentic_fx.ops import contracts, keys
from agentic_fx.ops.contracts import ErrorCode, OpsError, Principal

from .conftest import digest_of
from .test_ac54_55_manifest_v2_enroll import _code, _crash, _ready, _root, _write_ready
from .test_ac56_57_ops_principals import ACTOR, H, api5  # noqa: F401 (fixture)

OPS_BOT, OPERATOR, APPROVER = Principal.OPS_BOT, Principal.OPERATOR, Principal.APPROVER


# ---------------------------------------------------------------- manifest

@pytest.mark.parametrize("version", [True, 1.0, 2.0])
def test_manifest_version_must_be_a_plain_int(tmp_path, version):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    ready = _ready(directory)
    ready["version"] = version
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"
    assert _code(lambda: keys.read_token(directory, OPERATOR)) == "ready_invalid"


def test_active_entry_without_sha256_fails_closed(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_BOT)
    ready = _ready(directory)
    del ready["principals"]["ops-bot"]["sha256"]
    _write_ready(directory, ready)
    with pytest.raises(keys.KeySetError):
        keys.load_keyset(directory)


def test_revoked_entry_carries_no_digest(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_BOT)
    generation = _ready(directory)["principals"]["ops-bot"]["generation"]
    keys.revoke(directory, OPS_BOT)
    assert _ready(directory)["principals"]["ops-bot"] == {
        "generation": generation + 1, "state": "revoked", "sha256": None}


# ---------------------------------------------------------------- enroll の永続化

def test_enroll_reports_whether_it_changed_anything(tmp_path, capsys):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    assert keys.run_keys_command(root, "init") == 0
    assert keys.enroll(directory, OPS_BOT) is True
    assert keys.enroll(directory, OPS_BOT) is False
    capsys.readouterr()
    assert keys.run_keys_command(root, "enroll", "ops-web") == 0
    assert "登録しました" in capsys.readouterr().out
    assert keys.run_keys_command(root, "enroll", "ops-web") == 0
    out = capsys.readouterr().out
    assert "登録済み" in out and "登録しました" not in out


def test_enroll_fsyncs_the_temporary_key_before_rename_then_the_dir(tmp_path, monkeypatch):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    events: list[tuple] = []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd):
        st = os.fstat(fd)
        events.append(("fsync", st.st_ino, stat.S_ISDIR(st.st_mode)))
        return real_fsync(fd)

    def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        ino = os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False).st_ino
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        events.append(("rename", ino, dst))

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    assert keys.enroll(directory, OPS_BOT) is True
    monkeypatch.undo()

    renames = [(i, e) for i, e in enumerate(events) if e[0] == "rename"]
    targets = [e[2] for _, e in renames]
    assert targets == [keys.READY_NAME, "ops-bot.token", keys.READY_NAME], events
    for index, (_, ino, _dst) in renames:
        # 置き換える内容は rename 前に fsync 済み
        assert ("fsync", ino, False) in events[:index], (index, events)
        # rename の後、次の rename より前に dir を fsync
        following = [e for e in events[index + 1:] if e[0] == "rename"]
        end = events.index(following[0]) if following else len(events)
        assert any(e[0] == "fsync" and e[2] for e in events[index + 1:end]), (index, events)


def test_enroll_crash_after_writing_the_temporary_key_leaves_no_key_file(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    _crash(directory, "enroll", OPS_BOT, "key:written")
    # 鍵 file は rename で初めて現れる (書きかけの名前付き file を残さない)
    assert not (directory / "ops-bot.token").exists()
    _crash(directory, "enroll", OPS_BOT, "key:renamed")
    assert (directory / "ops-bot.token").exists()
    assert _ready(directory)["principals"]["ops-bot"]["state"] == "preparing"


# ---------------------------------------------------------------- 申告主体

@pytest.mark.parametrize("value", ["123456789012345678", ":123456789012345678",
                                   "discord123456789012345678"])
def test_actor_without_the_discord_prefix_is_400(api5, value):  # noqa: F811
    bot = api5.client(OPS_BOT)
    response = bot.get("/v1/whoami", headers={H: value})
    assert response.status_code == 400, value
    assert response.json()["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("value", [
    "discord:" + "１" * 18,          # 全角数字
    "discord:" + "٠" * 18,           # アラビア・インド数字
    "discord:12345678901234567８",
])
def test_validate_asserted_actor_accepts_only_ascii_digits(value):
    with pytest.raises(OpsError) as err:
        contracts.validate_asserted_actor(OPS_BOT, value)
    assert err.value.code is ErrorCode.INVALID_ARGUMENT
    assert contracts.validate_asserted_actor(OPS_BOT, ACTOR) == ACTOR


def test_service_layer_requires_the_actor_for_ops_bot(api5):  # noqa: F811
    env = api5.env
    service = api5.service
    approval = env.make_plugin_approval("p1", 1.0)
    calls = [
        lambda: service.approve(OPS_BOT, approval, digest_of(env, approval)),
        lambda: service.reject(OPS_BOT, approval),
        lambda: service.retry(OPS_BOT, approval, digest_of(env, approval)),
        lambda: service.get_job(OPS_BOT, "0" * 32),
        lambda: service.cancel_job(OPS_BOT, "0" * 32),
    ]
    for call in calls:
        with pytest.raises(OpsError) as err:
            call()
        assert err.value.code is ErrorCode.INVALID_ARGUMENT
    assert env.conn.execute("SELECT COUNT(*) FROM ops_requests").fetchone()[0] == 0
    assert env.approval(approval)["status"] == "pending"
    # 申告できない principal は従来どおり actor 無しで通る
    assert service.whoami(OPERATOR)["asserted_actor"] is None


# ---------------------------------------------------------------- autopilot

def test_ops_bot_decision_is_autopilot_restricted(api5):  # noqa: F811
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    env.state.update(autopilot=True)
    bot = api5.client(OPS_BOT)
    reply = bot.post(f"/v1/approvals/{approval}/approve",
                     json={"payload_sha256": digest_of(env, approval)}, headers={H: ACTOR})
    assert reply.status_code == 403, reply.text
    assert reply.json()["error"]["code"] == "autopilot_restricted"
    assert env.approval(approval)["status"] == "pending"
