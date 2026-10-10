"""鍵集合の `.ready` manifest v2 と停止中の ``afx keys enroll``。

- 旧形式 (固定の operator / approver) の `.ready` は、旧 ``initialize`` が書いた
  ものをそのまま保存した fixture で読取り互換を確かめる (手書きしない)
- v2 は登録済み principal の map。載っていない既知 principal は未登録で、API 全体は閉じない
- 未知 principal・鍵欠損・digest 不一致・準備状態は fail closed
- enroll は未登録 principal だけを generation 1 / active にし、他の鍵は内容も mtime も変えない
- enroll の各 crash 点 (process ごと os._exit) から、同じ enroll だけが完結できる
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal

from ._daemon import Daemon, activity, init_root, socket_path
from .test_real_daemon_integration import afx

FIXTURE_V1 = Path(__file__).with_name("fixtures") / "keys_manifest_v1.json"
OPS_BOT, OPS_WEB, OPS_NOTIFY = Principal.OPS_BOT, Principal.OPS_WEB, Principal.OPS_NOTIFY
OPERATOR, APPROVER = Principal.OPERATOR, Principal.APPROVER

# rotate と同じ hook 名 (準備 manifest → 鍵 file → ready manifest の各 3 点)
ENROLL_STEPS = ["preparing:written", "preparing:renamed", "preparing:dir_synced",
                "key:written", "key:renamed", "key:dir_synced",
                "ready:written", "ready:renamed", "ready:dir_synced"]
_NOT_STARTED = {"preparing:written"}
_COMMITTED = {"ready:renamed", "ready:dir_synced"}

_CRASH = ("import os, sys\n"
          "from pathlib import Path\n"
          "from agentic_fx.ops import keys\n"
          "from agentic_fx.ops.contracts import Principal\n"
          "action, principal, wanted = sys.argv[2], Principal(sys.argv[3]), sys.argv[4]\n"
          "def hook(step):\n"
          "    if step == wanted:\n"
          "        os._exit(17)\n"
          "getattr(keys, action)(Path(sys.argv[1]), principal, hook=hook)\n")


def _root(tmp_path: Path, name: str = "root") -> Path:
    root = tmp_path / name
    (root / "data").mkdir(parents=True)
    return root


def _install_v1(directory: Path) -> dict:
    """旧 initialize の出力を保存した fixture を鍵 dir に置く (mode は git が保たない)。"""
    fixture = json.loads(FIXTURE_V1.read_text())
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory, 0o700)
    files = {keys.READY_NAME: fixture["ready"]}
    files.update({f"{name}.token": text for name, text in fixture["tokens"].items()})
    for name, text in files.items():
        path = directory / name
        path.write_text(text)
        os.chmod(path, 0o600)
    return {name: text.strip() for name, text in fixture["tokens"].items()}


def _ready(directory: Path) -> dict:
    return json.loads((directory / keys.READY_NAME).read_text())


def _write_ready(directory: Path, ready: dict) -> None:
    path = directory / keys.READY_NAME
    path.write_text(json.dumps(ready))
    os.chmod(path, 0o600)


def _signature(directory: Path) -> dict:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}


def _code(call) -> str:
    with pytest.raises(keys.KeySetError) as err:
        call()
    return err.value.code


def _crash(directory: Path, action: str, principal: Principal, step: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CRASH, str(directory), action, principal.value, step],
        capture_output=True, text=True, timeout=60,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    assert result.returncode == 17, result.stderr


def _whoami(root: Path, token: str) -> int:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path(root))),
                      base_url="http://afx", timeout=15,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Length": "0"}) as client:
        return client.get("/v1/whoami").status_code


@pytest.fixture
def daemons():
    started: list[Daemon] = []
    yield started
    for daemon in started:
        daemon.kill()


# ---------------------------------------------------------------- AC-54

def test_ac54_v1_manifest_written_by_old_initialize_is_read_compatibly(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    tokens = _install_v1(directory)
    assert _ready(directory)["version"] == 1
    before = _signature(directory)
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(tokens["operator"]) is OPERATOR
    assert loaded.authenticate(tokens["approver"]) is APPROVER
    assert keys.read_token(directory, OPERATOR) == tokens["operator"]
    assert keys.read_token(directory, APPROVER) == tokens["approver"]
    # 旧形式に無い principal は未登録 (鍵欠損ではない)
    for principal in (OPS_BOT, OPS_WEB, OPS_NOTIFY):
        assert _code(lambda p=principal: keys.read_token(directory, p)) == "not_enrolled"
    # 起動時の補完は既存の .ready を書き換えない (v2 への書換えは鍵変更のときだけ)
    keys.ensure_initialized(directory, root=root)
    assert keys.initialize(directory, root=root) == []
    assert _signature(directory) == before


def test_ac54_v2_manifest_with_empty_principal_map_is_ready_invalid(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    _write_ready(directory, {"version": keys.READY_VERSION, "principals": {}})
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"
    assert _code(lambda: keys.read_token(directory, OPERATOR)) == "ready_invalid"


def test_ac54_empty_principal_map_keeps_the_real_daemon_closed(tmp_path, daemons):
    root = init_root(tmp_path / "root")
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    _write_ready(directory, {"version": keys.READY_VERSION, "principals": {}})
    daemon = Daemon(root)
    daemons.append(daemon)
    assert daemon.wait_first("api_start_failed", "api_started") == "api_start_failed"
    assert "ready_invalid" in activity(root)
    assert not socket_path(root).exists()
    assert daemon.stop() == 0


def test_ac54_v1_manifest_opens_the_real_daemon_and_keeps_the_file(tmp_path, daemons):
    root = init_root(tmp_path / "root")
    directory = keys.key_dir(root)
    tokens = _install_v1(directory)
    before = _signature(directory)
    daemon = Daemon(root)
    daemons.append(daemon)
    daemon.wait_listening()
    assert _whoami(root, tokens["operator"]) == 200
    assert _whoami(root, tokens["approver"]) == 200
    assert _whoami(root, "0" * 64) == 401
    assert daemon.stop() == 0
    assert _signature(directory) == before


def test_ac54_v1_manifest_requires_exactly_the_legacy_pair(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    _install_v1(directory)
    ready = _ready(directory)
    ready["principals"].pop("approver")
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"


def test_ac54_v1_manifest_with_a_newer_known_principal_is_refused(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    _install_v1(directory)
    ready = _ready(directory)
    ready["principals"]["ops-bot"] = dict(ready["principals"]["operator"])
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"


def test_ac54_fresh_init_writes_v2_with_only_the_initial_pair(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    created = keys.initialize(directory, root=root)
    assert set(created) == {OPERATOR, APPROVER}
    ready = _ready(directory)
    assert ready["version"] == 2
    assert set(ready["principals"]) == {"operator", "approver"}
    assert sorted(p.name for p in directory.iterdir()) == sorted(
        [keys.READY_NAME, "operator.token", "approver.token"])
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(keys.read_token(directory, OPERATOR)) is OPERATOR
    assert _code(lambda: keys.read_token(directory, OPS_BOT)) == "not_enrolled"


@pytest.mark.parametrize("version", [1, 2])
def test_ac54_unknown_principal_in_manifest_is_refused(tmp_path, version):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    ready = _ready(directory)
    ready["version"] = version
    ready["principals"]["ops-admin"] = dict(ready["principals"]["operator"])
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"
    assert _code(lambda: keys.read_token(directory, OPERATOR)) == "ready_invalid"
    assert _code(lambda: keys.enroll(directory, OPS_BOT)) == "ready_invalid"


@pytest.mark.parametrize("tamper", ["op_missing", "op_unknown", "generation_bool",
                                    "entry_not_object", "principals_list"])
def test_ac54_malformed_v2_entries_are_refused(tmp_path, tamper):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    ready = _ready(directory)
    if tamper == "op_missing":
        ready["principals"]["ops-bot"] = {"generation": 1, "state": "preparing"}
    elif tamper == "op_unknown":
        ready["principals"]["ops-bot"] = {"generation": 1, "state": "preparing",
                                          "op": "restore"}
    elif tamper == "generation_bool":
        ready["principals"]["operator"]["generation"] = True
    elif tamper == "entry_not_object":
        ready["principals"]["ops-bot"] = "active"
    else:
        ready["principals"] = [ready["principals"]]
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"


def test_ac54_enrolled_principal_with_missing_key_fails_closed(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_BOT)
    (directory / "ops-bot.token").unlink()
    assert _code(lambda: keys.load_keyset(directory)) == "key_missing"


def test_ac54_enrolled_principal_with_digest_mismatch_fails_closed(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_WEB)
    path = directory / "ops-web.token"
    path.write_text(keys.new_token() + "\n")
    os.chmod(path, 0o600)
    assert _code(lambda: keys.load_keyset(directory)) == "digest_mismatch"


def test_ac54_unfinished_enroll_keeps_the_real_daemon_closed(tmp_path, daemons):
    root = init_root(tmp_path / "root")
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    operator = keys.read_token(directory, OPERATOR)
    _crash(directory, "enroll", OPS_BOT, "key:renamed")
    daemon = Daemon(root)
    daemons.append(daemon)
    assert daemon.wait_first("api_start_failed", "api_started") == "api_start_failed"
    assert "ready_not_committed" in activity(root)
    assert not socket_path(root).exists()
    assert daemon.stop() == 0
    finished = afx(root, "keys", "enroll", "ops-bot")
    assert finished.returncode == 0, finished.stderr
    again = Daemon(root)
    daemons.append(again)
    again.wait_listening()
    assert _whoami(root, operator) == 200
    assert _whoami(root, keys.read_token(directory, OPS_BOT)) == 200
    assert again.stop() == 0


def _revoked_ops_bot(tmp_path: Path, root: Path | None = None) -> tuple[Path, str]:
    """ops-bot を enroll してから revoke し、鍵 dir と旧鍵の平文を返す。"""
    root = root or _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_BOT)
    old = keys.read_token(directory, OPS_BOT)
    keys.revoke(directory, OPS_BOT)
    return directory, old


def _put(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text)
    os.chmod(path, mode)


def test_ac54_revoked_principal_with_tombstone_loads_as_disabled(tmp_path):
    directory, old = _revoked_ops_bot(tmp_path)
    loaded = keys.load_keyset(directory)
    assert OPS_BOT in loaded.disabled
    assert loaded.authenticate(old) is None


def test_ac54_revoked_entry_with_the_old_key_left_in_place_fails_closed(tmp_path):
    directory, old = _revoked_ops_bot(tmp_path)
    _put(directory / "ops-bot.token", old + "\n")
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"


def test_ac54_manifest_flipped_to_revoked_over_an_active_key_fails_closed(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_BOT)
    ready = _ready(directory)
    entry = ready["principals"]["ops-bot"]
    ready["principals"]["ops-bot"] = {"generation": entry["generation"] + 1,
                                      "state": "revoked", "sha256": None}
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"


def test_ac54_revoked_entry_without_tombstone_fails_closed(tmp_path):
    directory, _old = _revoked_ops_bot(tmp_path)
    (directory / "ops-bot.token").unlink()
    assert _code(lambda: keys.load_keyset(directory)) == "key_missing"


@pytest.mark.parametrize("tamper", ["mode", "symlink", "empty"])
def test_ac54_revoked_entry_with_an_unverifiable_tombstone_fails_closed(tmp_path, tamper):
    directory, _old = _revoked_ops_bot(tmp_path)
    path = directory / "ops-bot.token"
    if tamper == "mode":
        os.chmod(path, 0o644)
    elif tamper == "symlink":
        target = tmp_path / "elsewhere"
        _put(target, path.read_text())
        path.unlink()
        path.symlink_to(target)
    else:
        _put(path, "")
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"


@pytest.mark.parametrize("tamper", ["sha256_missing", "sha256_digest", "generation_missing",
                                    "generation_str"])
def test_ac54_revoked_entry_schema_is_checked(tmp_path, tamper):
    directory, _old = _revoked_ops_bot(tmp_path)
    ready = _ready(directory)
    entry = ready["principals"]["ops-bot"]
    if tamper == "sha256_missing":
        del entry["sha256"]
    elif tamper == "sha256_digest":
        entry["sha256"] = "0" * 64
    elif tamper == "generation_missing":
        del entry["generation"]
    else:
        entry["generation"] = str(entry["generation"])
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"
    assert _code(lambda: keys.read_token(directory, OPERATOR)) == "ready_invalid"


@pytest.mark.parametrize("value", [None, 7, "", "ABC", "0" * 63])
def test_ac54_active_entry_sha256_must_be_a_hex_digest(tmp_path, value):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    ready = _ready(directory)
    ready["principals"]["approver"]["sha256"] = value
    _write_ready(directory, ready)
    assert _code(lambda: keys.load_keyset(directory)) == "ready_invalid"


@pytest.mark.parametrize("tamper", ["old_key", "tombstone_missing", "schema"])
def test_ac54_inconsistent_revoked_entry_keeps_the_real_daemon_closed(tmp_path, daemons,
                                                                     tamper):
    root = init_root(tmp_path / "root")
    directory, old = _revoked_ops_bot(tmp_path, root)
    path = directory / "ops-bot.token"
    if tamper == "old_key":
        _put(path, old + "\n")
        code = "ready_invalid"
    elif tamper == "tombstone_missing":
        path.unlink()
        code = "key_missing"
    else:
        ready = _ready(directory)
        del ready["principals"]["ops-bot"]["sha256"]
        _write_ready(directory, ready)
        code = "ready_invalid"
    daemon = Daemon(root)
    daemons.append(daemon)
    assert daemon.wait_first("api_start_failed", "api_started") == "api_start_failed"
    assert code in activity(root)
    assert not socket_path(root).exists()
    assert daemon.stop() == 0


# ---------------------------------------------------------------- AC-55

def test_ac55_enroll_creates_generation_one_and_leaves_other_keys_untouched(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    others = {name: sig for name, sig in _signature(directory).items()
              if name.endswith(".token")}
    entries = dict(_ready(directory)["principals"])
    keys.enroll(directory, OPS_BOT)
    after = _signature(directory)
    assert {name: after[name] for name in others} == others
    ready = _ready(directory)
    assert ready["version"] == 2
    assert {k: v for k, v in ready["principals"].items() if k != "ops-bot"} == entries
    token = keys.read_token(directory, OPS_BOT)
    assert ready["principals"]["ops-bot"] == {
        "generation": 1, "state": "active",
        "sha256": ready["principals"]["ops-bot"]["sha256"]}
    assert len(token) == 64
    assert oct((directory / "ops-bot.token").stat().st_mode & 0o777) == "0o600"
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(token) is OPS_BOT
    assert loaded.authenticate(keys.read_token(directory, OPERATOR)) is OPERATOR


def test_ac55_enroll_on_v1_manifest_writes_v2_and_keeps_legacy_entries(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    tokens = _install_v1(directory)
    legacy = dict(_ready(directory)["principals"])
    keys.enroll(directory, OPS_NOTIFY)
    ready = _ready(directory)
    assert ready["version"] == 2
    assert {k: v for k, v in ready["principals"].items() if k != "ops-notify"} == legacy
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(tokens["operator"]) is OPERATOR
    assert loaded.authenticate(tokens["approver"]) is APPROVER
    assert loaded.authenticate(keys.read_token(directory, OPS_NOTIFY)) is OPS_NOTIFY


@pytest.mark.parametrize("principal", [OPERATOR, OPS_BOT])
def test_ac55_enroll_of_an_active_principal_changes_nothing(tmp_path, principal):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_BOT)
    before = _signature(directory)
    keys.enroll(directory, principal)
    assert _signature(directory) == before


def test_ac55_enroll_on_v1_active_principal_does_not_rewrite_the_manifest(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    _install_v1(directory)
    before = _signature(directory)
    keys.enroll(directory, APPROVER)
    assert _signature(directory) == before
    assert _ready(directory)["version"] == 1


def test_ac55_enroll_of_a_revoked_principal_is_invalid_state(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.enroll(directory, OPS_BOT)
    keys.revoke(directory, OPS_BOT)
    before = _signature(directory)
    assert _code(lambda: keys.enroll(directory, OPS_BOT)) == "invalid_state"
    assert _signature(directory) == before
    # 復活は rotate
    keys.rotate(directory, OPS_BOT)
    assert keys.load_keyset(directory).authenticate(
        keys.read_token(directory, OPS_BOT)) is OPS_BOT


@pytest.mark.parametrize("action", ["rotate", "revoke"])
def test_ac55_rotate_or_revoke_of_an_unregistered_principal_is_not_enrolled(tmp_path, action):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    before = _signature(directory)
    assert _code(lambda: getattr(keys, action)(directory, OPS_WEB)) == "not_enrolled"
    assert _signature(directory) == before


def test_ac55_enroll_before_init_fails_without_creating_anything(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    assert _code(lambda: keys.enroll(directory, OPS_BOT)) == "not_initialized"
    assert not directory.exists()


_OTHER_COMMANDS = [("rotate", OPS_BOT), ("revoke", OPS_BOT), ("enroll", OPS_WEB),
                   ("rotate", OPERATOR), ("revoke", APPROVER)]


@pytest.mark.parametrize("step", ENROLL_STEPS)
def test_ac55_enroll_crash_points_only_the_same_enroll_completes(tmp_path, step):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    tokens = {p: keys.read_token(directory, p) for p in (OPERATOR, APPROVER)}
    legacy_files = {name: sig for name, sig in _signature(directory).items()
                    if name.endswith(".token")}
    legacy_entries = dict(_ready(directory)["principals"])

    _crash(directory, "enroll", OPS_BOT, step)

    def legacy_untouched() -> None:
        now = _signature(directory)
        assert {name: now[name] for name in legacy_files} == legacy_files
        entries = _ready(directory)["principals"]
        assert {k: entries[k] for k in legacy_entries} == legacy_entries

    legacy_untouched()
    entry = _ready(directory)["principals"].get("ops-bot")
    if step in _NOT_STARTED:
        # 準備 manifest がまだ commit されていない: 未登録のまま、API は旧集合で開ける
        assert entry is None
        loaded = keys.load_keyset(directory)
        assert loaded.authenticate(tokens[OPERATOR]) is OPERATOR
    elif step in _COMMITTED:
        assert entry["state"] == "active" and entry["generation"] == 1
        before = _signature(directory)
        keys.enroll(directory, OPS_BOT)
        assert _signature(directory) == before
    else:
        assert entry == {"generation": 1, "state": "preparing", "op": "enroll"}
        assert _code(lambda: keys.load_keyset(directory)) == "ready_not_committed"
        assert _code(lambda: keys.read_token(directory, OPS_BOT)) == "ready_not_committed"
        for action, principal in _OTHER_COMMANDS:
            before = _signature(directory)
            assert _code(lambda a=action, p=principal: getattr(keys, a)(directory, p)) \
                == "invalid_state", (action, principal)
            assert _signature(directory) == before, (action, principal)

    keys.enroll(directory, OPS_BOT)
    legacy_untouched()
    ready = _ready(directory)
    assert ready["version"] == 2
    assert ready["principals"]["ops-bot"]["state"] == "active"
    assert ready["principals"]["ops-bot"]["generation"] == 1
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(keys.read_token(directory, OPS_BOT)) is OPS_BOT
    assert loaded.authenticate(tokens[OPERATOR]) is OPERATOR
    assert loaded.authenticate(tokens[APPROVER]) is APPROVER


def test_ac55_afx_keys_enroll_command_runs_only_while_stopped(tmp_path):
    from agentic_fx.store.instance_lock import acquire_instance_lock
    root = init_root(tmp_path / "root")
    assert afx(root, "keys", "init").returncode == 0
    directory = keys.key_dir(root)
    before = _signature(directory)
    held = acquire_instance_lock(root / "data")
    try:
        running = afx(root, "keys", "enroll", "ops-bot")
    finally:
        held.close()
    assert running.returncode == 1
    assert _signature(directory) == before
    done = afx(root, "keys", "enroll", "ops-bot")
    assert done.returncode == 0, done.stderr
    assert "ops-bot" in done.stdout
    assert keys.read_token(directory, OPS_BOT) not in done.stdout + done.stderr
    assert _ready(directory)["principals"]["ops-bot"]["generation"] == 1
    again = afx(root, "keys", "enroll", "ops-bot")
    assert again.returncode == 0
    unknown = afx(root, "keys", "enroll", "ops-admin")
    assert unknown.returncode == 2


def test_ac55_afx_keys_enroll_revoked_principal_reports_invalid_state(tmp_path):
    root = init_root(tmp_path / "root")
    assert afx(root, "keys", "init").returncode == 0
    assert afx(root, "keys", "enroll", "ops-web").returncode == 0
    assert afx(root, "keys", "revoke", "ops-web").returncode == 0
    directory = keys.key_dir(root)
    before = _signature(directory)
    refused = afx(root, "keys", "enroll", "ops-web")
    assert refused.returncode == 1
    assert "invalid_state" in refused.stderr
    assert _signature(directory) == before


@pytest.mark.parametrize("action", ["rotate", "revoke"])
def test_ac55_afx_keys_rotate_or_revoke_of_an_unregistered_principal_asks_for_enroll(
        tmp_path, action):
    root = init_root(tmp_path / "root")
    assert afx(root, "keys", "init").returncode == 0
    before = _signature(keys.key_dir(root))
    refused = afx(root, "keys", action, "ops-web")
    assert refused.returncode == 1
    assert "not_enrolled" in refused.stderr
    assert "afx keys enroll ops-web" in refused.stderr
    assert "invalid_state" not in refused.stderr
    assert _signature(keys.key_dir(root)) == before
