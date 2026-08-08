"""Landlock ctypes wrapper (プラン8, 設計書 §4.6)。

fake ctypes.CDLL によるロジック検証 (このプロセス自身は制限しない —
テストプロセス全体が二度と /tmp 等に触れなくなると以後のテストが全滅
する) と、実 Landlock を使う統合テスト (別プロセスで実施) を分離する。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import LandlockUnavailable, is_available, restrict_to


def test_is_available_false_on_non_x86_64(monkeypatch):
    import agentic_fx.core.landlock as landlock_mod
    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "aarch64")
    assert is_available() is False


def test_restrict_to_raises_when_create_ruleset_fails(monkeypatch, tmp_path):
    import agentic_fx.core.landlock as landlock_mod

    class FakeLibc:
        """I5 対応: `syscall`/`prctl` を通常メソッド (`def ...(self, ...)`)
        として定義すると、`libc.syscall` はアクセスするたびにバインド
        メソッドオブジェクトを新規生成し、Python のバインドメソッドは
        任意属性の代入を許さない (`__dict__` を持たない) — 実装コードが
        行う `libc.syscall.restype = ctypes.c_long` がここで
        `AttributeError` になり、テスト対象コードに到達する前にテスト
        自体が壊れる (レビュー I5)。属性代入を許す関数オブジェクトを
        インスタンス属性として直接持たせることで、実 `ctypes` の関数
        ポインタオブジェクトと同じ「`.restype` を保持できる callable」
        という性質を fake でも再現する。
        """
        def __init__(self) -> None:
            self.syscall = lambda *args: -1  # landlock_create_ruleset 失敗
            self.prctl = lambda *args: 0

    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(landlock_mod.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(landlock_mod.ctypes, "get_errno", lambda: 38)  # ENOSYS
    with pytest.raises(LandlockUnavailable):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])


# --- 2026-08-08 指揮者が着手前に追加した失敗分岐ピン ----------------------
# 旧稿は `restrict_to` の 4 つの失敗分岐 (create_ruleset / add_rule / prctl /
# restrict_self) のうち **1 つしかテストしていなかった**。残り 3 つと
# `is_available` の ABI 問い合わせ失敗、および `finally` での fd close は
# 無防備で、削除しても全件 green のままになる (プラン 8 Task 7 で同型の穴が
# 26 件中 16 件生存した実測を踏まえた予防)。


class _ScriptedLibc:
    """`syscall` の戻り値を呼び出し順に台本化した fake。

    `syscall`/`prctl` は **必ずインスタンス属性の関数オブジェクト**にする
    (I5 — クラスメソッドだと実装側の `libc.syscall.restype = ...` が
    バインドメソッドへの属性代入になり `AttributeError` で落ちる)。
    """

    def __init__(self, *, syscall_results, prctl_result=0):
        self._results = list(syscall_results)
        self.syscall_numbers: list[int] = []
        self.prctl_args: list[tuple] = []

        def syscall(*args):
            n = args[0]
            self.syscall_numbers.append(getattr(n, "value", n))
            return self._results.pop(0) if self._results else 0

        def prctl(*args):
            self.prctl_args.append(args)
            return prctl_result

        self.syscall = syscall
        self.prctl = prctl


def _install(monkeypatch, libc):
    """`platform.machine`/`ctypes.CDLL`/`os.close` を差し替えて、閉じられた
    fd の一覧を返す。

    `os.close` を差し替えるのは必須 — 台本上の `ruleset_fd` は実在しない
    番号 (4242) であり、実 `os.close` を通すと `OSError(EBADF)` が
    `finally` の中で送出されて **本来送出されるはずの `LandlockUnavailable`
    を置き換えてしまう**。あわせて「`finally` で確実に閉じている」ことの
    ピンにもなる。
    """
    import agentic_fx.core.landlock as landlock_mod

    closed: list[int] = []
    real_close = os.close

    def fake_close(fd):
        closed.append(fd)
        if fd < 1000:   # os.open で得た実 fd だけ本当に閉じる
            real_close(fd)

    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(landlock_mod.ctypes, "CDLL", lambda *a, **k: libc)
    monkeypatch.setattr(landlock_mod.ctypes, "get_errno", lambda: 1)
    monkeypatch.setattr(landlock_mod.os, "close", fake_close)
    return closed


def test_is_available_false_when_abi_query_fails(monkeypatch):
    """カーネルが Landlock 非対応 (ENOSYS) なら False。x86_64 判定だけを
    見ていると、この分岐は削除しても検出できない。"""
    _install(monkeypatch, _ScriptedLibc(syscall_results=[-1]))
    assert is_available() is False


def test_restrict_to_raises_when_add_rule_fails(monkeypatch, tmp_path):
    libc = _ScriptedLibc(syscall_results=[8, 4242, -1])  # abi, ruleset_fd, add_rule
    closed = _install(monkeypatch, libc)
    with pytest.raises(LandlockUnavailable, match="landlock_add_rule"):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
    assert 4242 in closed          # ruleset_fd を finally で閉じている
    assert len(closed) == 2        # parent_fd も閉じている (fd リークなし)


def test_restrict_to_raises_when_no_new_privs_fails(monkeypatch, tmp_path):
    """`prctl(PR_SET_NO_NEW_PRIVS)` は `landlock_restrict_self` の前提条件。
    失敗を無視すると後段が EPERM になる (実測で確認済み — Step 7 参照)。"""
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0], prctl_result=-1)
    closed = _install(monkeypatch, libc)
    with pytest.raises(LandlockUnavailable, match="NO_NEW_PRIVS"):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
    assert 4242 in closed


def test_restrict_to_raises_when_restrict_self_fails(monkeypatch, tmp_path):
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, -1])
    closed = _install(monkeypatch, libc)
    with pytest.raises(LandlockUnavailable, match="landlock_restrict_self"):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
    assert 4242 in closed


def test_restrict_to_issues_syscalls_in_required_order(monkeypatch, tmp_path):
    """syscall の**順序**を pin する。`prctl(NO_NEW_PRIVS)` が
    `landlock_restrict_self` より後になるとカーネルが EPERM を返す
    (実測済み) — 順序は正しさの一部であって偶然ではない。"""
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0, 0])
    _install(monkeypatch, libc)
    ro = tmp_path / "ro"; ro.mkdir()
    rw = tmp_path / "rw"; rw.mkdir()
    restrict_to(read_only_paths=[ro], read_write_paths=[rw])

    # 444(abi) → 444(create) → 445(add ro) → 445(add rw) → 446(restrict)
    assert libc.syscall_numbers == [444, 444, 445, 445, 446]
    assert len(libc.prctl_args) == 1
    assert libc.prctl_args[0][0] == 38   # PR_SET_NO_NEW_PRIVS


_REAL_LANDLOCK_SCRIPT = textwrap.dedent("""
    import sys
    from pathlib import Path
    from agentic_fx.core.landlock import restrict_to

    ro_dir = Path(sys.argv[1])       # read_only_paths
    rw_dir = Path(sys.argv[2])       # read_write_paths
    blocked_dir = Path(sys.argv[3])  # どちらにも入れない

    restrict_to(read_only_paths=[ro_dir], read_write_paths=[rw_dir])

    # (1) read_only パスは読める
    assert sorted(p.name for p in ro_dir.iterdir()) == ["x.txt"], "ro not readable"

    # (2) read_only パスへは **書けない** (旧稿が見ていなかった防御)
    try:
        (ro_dir / "new.txt").write_text("x")
        print("FAIL: write succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (3) read_write パスへは書ける (_READ_WRITE_ACCESS の唯一のピン)
    try:
        (rw_dir / "new.txt").write_text("x")
    except PermissionError:
        print("FAIL: write was denied on a read-write path")
        sys.exit(1)

    # (4) 列挙していないパスは読めない
    try:
        list(blocked_dir.iterdir())
        print("FAIL: blocked_dir was readable")
        sys.exit(1)
    except PermissionError:
        pass

    print("OK")
    sys.exit(0)
""")


def test_real_landlock_enforces_read_only_read_write_and_blocked(tmp_path):
    """実 Landlock (別プロセス) — カーネルが対応していなければ skip する。

    **skip したかどうかを呼び出し側で必ず確認すること** (Step 5 の実行ログ)。
    この 1 本が実カーネルの強制を触る唯一のテストであり、静かに skip される
    と偽の green になる。本環境 (x86_64 / kernel 7.0.0-29-generic / ABI 8)
    では実行されることを指揮者が実測確認済み。
    """
    from agentic_fx.core.landlock import is_available
    if not is_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "x.txt").write_text("ok")
    rw = tmp_path / "rw"
    rw.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _REAL_LANDLOCK_SCRIPT,
         str(ro), str(rw), str(blocked)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
