"""web の設定の期待 reason を機械生成する (文法の違反 class と公開範囲の全直積)。

期待値は仕様の 2 表を行の順に書き下した関数 (`g_*` と `c_expected`) だけから導く。
実装の関数は使わない。生成物は `fixtures/web_access_matrix.json` に置き、afx-ops の
3 入口 (config・probe・production) と本体の reader が同じ fixture を読む。

    python tests/_web_access_matrix.py --write   # fixture を更新する
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "web_access_matrix.json"
LIMITED = "192.168.1.0/24"

BASE = 'instance_root = "{ROOT}"\nkey_dir = "{KEYS}"\nport = {PORT}\n'


def _row(row_id: str, group: str, expected: str | None, **spec) -> dict:
    return {"id": row_id, "group": group, "expected": expected, **spec}


def _toml(extra: str = "", *, head: str = BASE) -> str:
    return head + extra


def g_rows() -> list[dict]:
    """第 1 層 (G1〜G7)。違反する class ごとに最終 reason を書き下す。"""
    rows: list[dict] = []
    ok = dict(bind="loopback", interfaces="na", secret="none")

    def add(row_id, expected, text, **extra):
        rows.append(_row(row_id, "G", expected, text=text, **{**ok, **extra}))

    rows.append(_row("G1-config-dir-missing", "G", "config_dir_missing", text=_toml(),
                     config_dir="missing", **ok))
    rows.append(_row("G2-config-dir-writable", "G", "config_dir_invalid", text=_toml(),
                     config_dir="writable", **ok))
    for name, kind, mode in (("symlink", "symlink", 0o600), ("directory", "directory", 0o700),
                             ("other-uid", "other_uid", 0o600), ("mode-0664", "file", 0o664),
                             ("mode-0666", "file", 0o666), ("mode-0620", "file", 0o620),
                             ("mode-0602", "file", 0o602)):
        add(f"G2-ops-toml-{name}", "config_dir_invalid", _toml(), ops_toml=kind, mode=mode)
    for mode in (0o600, 0o644, 0o640):
        add(f"G2-ops-toml-mode-{mode:04o}-accepted", None, _toml(), mode=mode)

    add("G3-ops-toml-absent", "config_invalid", _toml(), ops_toml="absent")
    add("G3-toml-syntax", "config_invalid", "port = = 1\n")
    add("G3-toml-duplicate-key", "config_invalid", _toml("port = 9\n"))
    rows.append(_row("G3-non-utf8", "G", "config_invalid", text="", bytes_hex="ff fe 00".replace(" ", ""),
                     **ok))
    add("G3a-instance-root-missing", "config_invalid",
        'key_dir = "{KEYS}"\nport = {PORT}\n')
    add("G3a-key-dir-missing", "config_invalid", 'instance_root = "{ROOT}"\nport = {PORT}\n')
    add("G3a-instance-root-not-string", "config_invalid",
        'instance_root = 1\nkey_dir = "{KEYS}"\nport = {PORT}\n')
    add("G3a-key-dir-not-string", "config_invalid",
        'instance_root = "{ROOT}"\nkey_dir = ["{KEYS}"]\nport = {PORT}\n')
    add("G3a-instance-root-relative", "config_invalid",
        'instance_root = "relative/root"\nkey_dir = "{KEYS}"\nport = {PORT}\n')
    add("G3a-key-dir-relative", "config_invalid",
        'instance_root = "{ROOT}"\nkey_dir = "relative/keys"\nport = {PORT}\n')
    add("G3a-other-instance", "config_invalid",
        'instance_root = "/srv/another-instance"\nkey_dir = "{KEYS}"\nport = {PORT}\n',
        body_only=True)
    add("G3b-unknown-key", "config_invalid", _toml('proxy = "x"\n'))
    add("G3b-trade-key", "config_invalid", _toml('trade_notices = "all"\n'))

    for name, value in (("missing", None), ("string", '"8080"'), ("bool", "true"), ("zero", "0"),
                        ("high", "65536"), ("negative", "-1"), ("float", "8080.0")):
        text = ('instance_root = "{ROOT}"\nkey_dir = "{KEYS}"\n'
                + ("" if value is None else f"port = {value}\n"))
        add(f"G4-port-{name}", "config_invalid", text)
    for name, value in (("int", "1"), ("bool", "true"), ("list", '["127.0.0.1"]'),
                        ("hostname", '"localhost"'), ("ipv6", '"::1"'),
                        ("mapped", '"::ffff:127.0.0.1"'), ("unspecified", '"0.0.0.0"'),
                        ("leading-space", '" 127.0.0.1"'), ("trailing-space", '"127.0.0.1 "'),
                        ("leading-zero", '"127.000.000.001"'), ("short", '"127.1"'),
                        ("cidr", '"127.0.0.1/8"'), ("empty", '""')):
        add(f"G5-bind-{name}", "config_invalid", _toml(f"bind = {value}\n"))
    for name, value in (("string", '"192.168.1.0/24"'), ("empty-list", "[]"),
                        ("empty-string", '[""]'), ("int-item", "[1]"), ("ipv6", '["fe80::1"]'),
                        ("mapped", '["::ffff:10.0.0.1"]'), ("space", '[" 10.0.0.1"]'),
                        ("host-bits", '["10.0.0.1/24"]'), ("leading-zero", '["010.0.0.1"]'),
                        ("duplicate", '["10.0.0.0/24", "10.0.0.0/24"]'),
                        ("duplicate-host", '["10.0.0.1", "10.0.0.1/32"]'),
                        ("mixed-unrestricted", '["0.0.0.0/0", "10.0.0.0/24"]'),
                        ("mixed-unrestricted-reversed", '["10.0.0.0/24", "0.0.0.0/0"]'),
                        ("unrestricted-twice", '["0.0.0.0/0", "0.0.0.0/0"]'),
                        ("prefix-33", '["10.0.0.0/33"]'), ("netmask", '["10.0.0.0/255.255.255.0"]')):
        add(f"G6-allow-{name}", "config_invalid", _toml(f"allow = {value}\n"))
    for name, value in (("capital", '"Basic"'), ("upper", '"NONE"'), ("digest", '"digest"'),
                        ("empty", '""'), ("int", "1"), ("bool", "true"), ("list", "[]")):
        add(f"G7-auth-{name}", "config_invalid", _toml(f"auth = {value}\n"))
    return rows


BINDS = ("loopback", "local", "absent", "failed")
ALLOWS = ("omitted", "limited", "unrestricted")
AUTHS = ("none", "basic")
SECRETS = ("none", "empty_dir", "safe", "unsafe")


def c_expected(bind: str, allow: str, auth: str, secret: str, artifact: str = "T1") -> str | None:
    """第 2 層の C1〜C8 を行の順に書き下したもの (T1 artifact)。"""
    nonloop = bind in ("local", "absent", "failed")
    secret_state = {"none": "none", "empty_dir": "none", "safe": "safe", "unsafe": "unsafe"}[secret]
    if nonloop and allow == "omitted":
        return "allow_required"                                          # C1
    if artifact == "T1" and allow == "unrestricted":
        return "unrestricted_not_supported"                              # C2
    if artifact == "T1" and allow in ("omitted", "limited") and auth == "basic":
        return "auth_not_supported"                                      # C3
    if bind == "failed" and allow in ("limited", "unrestricted"):
        return "interface_lookup_failed"                                 # C4
    if bind == "absent" and allow in ("limited", "unrestricted"):
        return "bind_not_local"                                          # C5
    if bind in ("loopback", "local") and secret_state == "unsafe":
        return "secret_invalid"                                          # C6
    if auth == "basic" and secret_state == "none":
        return "secret_missing"                                          # C7
    if auth == "none" and secret_state == "safe":
        return "secret_unexpected"                                       # C7
    return None                                                          # C8


def _c_text(bind: str, allow: str, auth: str) -> str:
    extra = ""
    if bind != "loopback":
        extra += 'bind = "{BIND}"\n'
    elif allow != "omitted" or auth != "none":
        extra += 'bind = "127.0.0.1"\n'
    if allow == "limited":
        extra += f'allow = ["{LIMITED}"]\n'
    elif allow == "unrestricted":
        extra += 'allow = ["0.0.0.0/0"]\n'
    if auth == "basic":
        extra += 'auth = "basic"\n'
    return _toml(extra)


def c_rows() -> list[dict]:
    rows = []
    for bind, allow, auth, secret in itertools.product(BINDS, ALLOWS, AUTHS, SECRETS):
        rows.append(_row(f"C-{bind}-{allow}-{auth}-{secret}", "C",
                         c_expected(bind, allow, auth, secret), text=_c_text(bind, allow, auth),
                         bind=bind, allow=allow, auth=auth, secret=secret,
                         interfaces={"loopback": "na", "local": "local", "absent": "absent",
                                     "failed": "failed"}[bind]))
    return rows


def rows() -> list[dict]:
    return g_rows() + c_rows()


def render() -> str:
    return json.dumps(rows(), indent=1, sort_keys=True, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(render(), encoding="utf-8")
        print(f"wrote {len(rows())} rows")
    else:
        print(render(), end="")
