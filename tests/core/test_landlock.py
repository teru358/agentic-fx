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


def test_restrict_to_raises_when_landlock_is_unavailable(monkeypatch, tmp_path):
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
    # 2026-08-08 指揮者: **プラン記載のこのテストは名前どおりのことを検証
    # していなかった**。`FakeLibc.syscall` は全呼び出しに -1 を返すため、
    # `restrict_to` 冒頭の `is_available()` が False になってそこで送出され、
    # `landlock_create_ruleset` の失敗分岐には**到達していない** (`match=`
    # を足して実測発覚)。テスト名を実態に合わせ、create_ruleset の分岐は
    # 下の専用テストで別途 pin する。
    with pytest.raises(LandlockUnavailable, match="not available"):
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
        self.syscall_args: list[tuple] = []
        self.prctl_args: list[tuple] = []

        def syscall(*args):
            n = args[0]
            self.syscall_numbers.append(getattr(n, "value", n))
            # レビュー 2 周目 (codex + sonnet 一致): 定数を見るだけでは
            # 「その定数が実際に syscall へ渡されているか」を pin できない。
            # `ctypes.byref(x)` は `CArgObject` を返し、`._obj` で元の
            # 構造体を参照できる (CPython の実装詳細だがテスト専用)。
            self.syscall_args.append(
                tuple(getattr(a, "_obj", a) for a in args))
            if not self._results:
                # sonnet 副査 (1 周目): 台本切れで 0 (成功) を返すのは
                # fail-open。想定外の syscall が増えたときに黙って通す
                # harness になるため、明示的に落とす。
                raise AssertionError(
                    f"_ScriptedLibc: 台本にない syscall 呼び出し ({n})")
            return self._results.pop(0)

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


def test_restrict_to_raises_when_create_ruleset_fails(monkeypatch, tmp_path):
    """`landlock_create_ruleset` の失敗分岐 (2026-08-08 指揮者が追加)。

    ABI 問い合わせは成功させ (`is_available()` を通過させ)、**2 回目の
    syscall = ruleset 作成だけを失敗**させる。これが無いと
    `if ruleset_fd < 0:` を削除する変異が、後段の add_rule 失敗が投げる
    同じ `LandlockUnavailable` によって green のまま生存する (実測)。
    """
    libc = _ScriptedLibc(syscall_results=[8, -1])   # abi ok, create_ruleset 失敗
    _install(monkeypatch, libc)
    with pytest.raises(LandlockUnavailable, match="landlock_create_ruleset"):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
    assert libc.syscall_numbers == [444, 444]   # add_rule へ進んでいない


def test_is_available_false_when_abi_below_required(monkeypatch):
    """ABI 1/2 のカーネルでは `TRUNCATE` を強制できないので fail closed。

    「Landlock が使えるかどうか」ではなく「**本モジュールの要求水準で**
    使えるか」を返す (レビュー 1 周目 codex)。ABI >= 1 で True にすると、
    完全性を守れないカーネルで improve worker が起動してしまう。
    """
    _install(monkeypatch, _ScriptedLibc(syscall_results=[2]))
    assert is_available() is False
    _install(monkeypatch, _ScriptedLibc(syscall_results=[3]))
    assert is_available() is True


def test_create_ruleset_is_handed_the_truncate_bit(monkeypatch, tmp_path):
    """`landlock_create_ruleset` に**実際に渡される** `handled_access_fs` を
    pin する (レビュー 2 周目 codex + sonnet 一致)。

    旧版はモジュール定数 (`_HANDLED_ACCESS_FS` 等) を見るだけで、
    `restrict_to` がその定数を本当に syscall へ渡しているかは見ていな
    かった。**`_RulesetAttr(handled_access_fs=_ABI_V1_HANDLED_ACCESS_FS)`
    に戻す配線変異は単体テストを全て素通りした** (実測)。
    「実 Landlock テストが skip されても宣言漏れを検出する二層目」という
    旧 docstring の主張は成立していなかった。

    あわせて、各パスに渡される `allowed_access` (rule 側) も直接 pin する。
    """
    import agentic_fx.core.landlock as landlock_mod

    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0, 0])
    _install(monkeypatch, libc)
    ro = tmp_path / "ro"; ro.mkdir()
    rw = tmp_path / "rw"; rw.mkdir()
    restrict_to(read_only_paths=[ro], read_write_paths=[rw])

    # syscall 列: [444(abi), 444(create), 445(ro), 445(rw), 446]
    create_attr = libc.syscall_args[1][1]
    assert create_attr.handled_access_fs & landlock_mod._ACCESS_FS_TRUNCATE, \
        "create_ruleset に TRUNCATE が宣言されていない"
    assert create_attr.handled_access_fs == landlock_mod._HANDLED_ACCESS_FS

    ro_attr = libc.syscall_args[2][3]
    rw_attr = libc.syscall_args[3][3]
    # rule 側の allowed_access は必ず handled の部分集合でなければならない
    # (超えるとカーネルが EINVAL を返す)。
    for attr in (ro_attr, rw_attr):
        assert attr.allowed_access & ~create_attr.handled_access_fs == 0

    assert not (ro_attr.allowed_access & landlock_mod._ACCESS_FS_TRUNCATE)
    assert rw_attr.allowed_access & landlock_mod._ACCESS_FS_TRUNCATE
    assert ro_attr.allowed_access == landlock_mod._READ_ONLY_ACCESS
    assert rw_attr.allowed_access == landlock_mod._READ_WRITE_ACCESS


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


def test_restrict_to_rejects_non_directory_path(monkeypatch, tmp_path):
    """`os.O_DIRECTORY` の pin (2026-08-08 指揮者が追加)。

    ディレクトリでないパスを渡したら **その場で失敗する** こと。
    `O_DIRECTORY` を外すと `os.open` がファイルにも成功してしまい、
    ファイルの fd に対して path_beneath ルールを張るという意図と違う
    セマンティクスのまま**静かに通る** (実測: `O_DIRECTORY` 有りなら
    `NotADirectoryError` errno 20、無しなら fd が取れてしまう)。

    現状は `os.open` の `NotADirectoryError` がそのまま伝播する。
    `LandlockUnavailable` へ正規化するかは設計判断としてレビューに委ねる
    (`LandlockUnavailable` の定義は「カーネル非対応・非対応アーキテクチャ・
    syscall 失敗」であり、呼び出し側のプログラム誤りは別クラス)。
    """
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0])
    _install(monkeypatch, libc)
    a_file = tmp_path / "a.txt"
    a_file.write_text("x")
    with pytest.raises(NotADirectoryError):
        restrict_to(read_only_paths=[a_file], read_write_paths=[])


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


# 5-A: execute_paths テスト (プラン10 Task 5)

def test_create_ruleset_still_only_declares_truncate_and_execute_family(monkeypatch, tmp_path):
    """execute_paths 追加後も handled_access_fs 自体は不変
    (EXECUTE は元から ABI v1 に入っている — 新設は allowed_access 側のみ)。"""
    import agentic_fx.core.landlock as landlock_mod  # Minor 3: 既存テストの規律 (関数内 import) に合わせる
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0, 0, 0])
    _install(monkeypatch, libc)
    ro = tmp_path / "ro"; ro.mkdir()
    rw = tmp_path / "rw"; rw.mkdir()
    ex = tmp_path / "ex"; ex.mkdir()
    restrict_to(read_only_paths=[ro], read_write_paths=[rw], execute_paths=[ex])
    create_attr = libc.syscall_args[1][1]
    assert create_attr.handled_access_fs == landlock_mod._HANDLED_ACCESS_FS


def test_restrict_to_adds_a_rule_for_each_execute_path(monkeypatch, tmp_path):
    """execute_paths の各パスに対して landlock_add_rule (445) が 1 回ずつ
    追加で発行され、allowed_access が `_EXECUTE_ACCESS` と厳密一致する
    (multiplicity 1 の pin — execute_paths を渡しても ro/rw の呼出数が
    変わらないことも同時に見る)。"""
    import agentic_fx.core.landlock as landlock_mod
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0, 0, 0])
    _install(monkeypatch, libc)
    ro = tmp_path / "ro"; ro.mkdir()
    rw = tmp_path / "rw"; rw.mkdir()
    ex = tmp_path / "ex"; ex.mkdir()
    restrict_to(read_only_paths=[ro], read_write_paths=[rw], execute_paths=[ex])
    # 444(abi) 444(create) 445(ro) 445(rw) 445(execute) 446(restrict)
    assert libc.syscall_numbers == [444, 444, 445, 445, 445, 446]
    ex_attr = libc.syscall_args[4][3]
    assert ex_attr.allowed_access == landlock_mod._EXECUTE_ACCESS


def test_execute_access_mask_is_self_sufficient(monkeypatch, tmp_path):
    """§2.2: `_EXECUTE_ACCESS` は EXECUTE|READ_FILE|READ_DIR の**和**で、
    同一 inode に対する ro ルールとの併合に依存しない。execute_paths の
    値を `_ACCESS_FS_EXECUTE` 単独に弱める変異は、read_only にも
    同じパスを渡す既存テストでは検出できない — ここでは execute_paths
    のパスを read_only にも read_write にも一切含めない状態で
    allowed_access の READ_FILE/READ_DIR bit を直接検査する。"""
    import agentic_fx.core.landlock as landlock_mod
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0])
    _install(monkeypatch, libc)
    ex = tmp_path / "exonly"; ex.mkdir()
    restrict_to(read_only_paths=[], read_write_paths=[], execute_paths=[ex])
    ex_attr = libc.syscall_args[2][3]
    assert ex_attr.allowed_access & landlock_mod._ACCESS_FS_EXECUTE
    assert ex_attr.allowed_access & landlock_mod._ACCESS_FS_READ_FILE
    assert ex_attr.allowed_access & landlock_mod._ACCESS_FS_READ_DIR
    assert not (ex_attr.allowed_access & landlock_mod._ACCESS_FS_WRITE_FILE)


def test_read_write_access_still_excludes_make_char_and_make_sym(monkeypatch, tmp_path):
    """§2.2 の `/dev` rw 脅威分析はこの不在だけに乗っている
    (`_READ_WRITE_ACCESS` に `MAKE_CHAR`/`MAKE_SYM` が無いことでデバイス
    ノード・symlink の新規作成ができない)。5-D で `/dev` を read_write に
    昇格させる前に、この不在を直接 pin しておく — `_ACCESS_FS_MAKE_CHAR`/
    `_ACCESS_FS_MAKE_SYM` を足す変異は既存の等価性 assert
    (`test_create_ruleset_is_handed_the_truncate_bit`) でも検出できるが、
    その等価性 assert 自体が定数と一緒に動く変異 (定数側に足す) では
    落ちない — ここでは bit 単位で直接見る。"""
    import agentic_fx.core.landlock as landlock_mod
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0, 0])
    _install(monkeypatch, libc)
    ro = tmp_path / "ro"; ro.mkdir()
    rw = tmp_path / "rw"; rw.mkdir()
    restrict_to(read_only_paths=[ro], read_write_paths=[rw])
    rw_attr = libc.syscall_args[3][3]
    assert not (rw_attr.allowed_access & landlock_mod._ACCESS_FS_MAKE_CHAR)
    assert not (rw_attr.allowed_access & landlock_mod._ACCESS_FS_MAKE_SYM)


def test_execute_paths_default_is_empty(monkeypatch, tmp_path):
    """`execute_paths` を渡さない既存呼び出し (trade profile) が無変更
    のまま動く — 追加の add_rule 呼出しが発生しないことを pin する。"""
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0, 0])
    _install(monkeypatch, libc)
    ro = tmp_path / "ro"; ro.mkdir()
    rw = tmp_path / "rw"; rw.mkdir()
    restrict_to(read_only_paths=[ro], read_write_paths=[rw])
    assert libc.syscall_numbers == [444, 444, 445, 445, 446]


_REAL_LANDLOCK_SCRIPT = textwrap.dedent("""
    import os
    import sys
    from pathlib import Path
    from agentic_fx.core.landlock import restrict_to

    ro_dir = Path(sys.argv[1])       # read_only_paths
    rw_dir = Path(sys.argv[2])       # read_write_paths
    blocked_dir = Path(sys.argv[3])  # どちらにも入れない

    restrict_to(read_only_paths=[ro_dir], read_write_paths=[rw_dir])

    # (1) read_only パスは読める
    assert sorted(p.name for p in ro_dir.iterdir()) == ["empty", "x.txt"], \
        "ro not readable"

    # (2) read_only パスへは **新規作成できない** (_ACCESS_FS_MAKE_REG 非付与)
    try:
        (ro_dir / "new.txt").write_text("x")
        print("FAIL: create succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (2b) read_only パスの **既存ファイルも上書きできない**
    #      (_ACCESS_FS_WRITE_FILE 非付与)。2026-08-08 指揮者が追加 —
    #      (2) は新規作成しか試さないため MAKE_REG しか pin できず、
    #      `_READ_ONLY_ACCESS` に WRITE_FILE を足す変異が **生存した**
    #      (実測: 既存ファイルを上書きできてしまう = 権限境界が破れる)。
    #      「同じ防御を複数の壊し方で」— 作成と上書きは別の access bit。
    try:
        (ro_dir / "x.txt").write_text("OVERWRITTEN")
        print("FAIL: overwrite succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (2c) read_only パスへは **ディレクトリも作れない**
    #      (`_ABI_V1_HANDLED_ACCESS_FS` から MAKE_DIR が抜けると、その
    #      種別が ruleset の判定対象外になり素通しする)。
    try:
        (ro_dir / "subdir").mkdir()
        print("FAIL: mkdir succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (3) read_write パスへは **新規作成できる** (MAKE_REG + WRITE_FILE を
    #     同時に要求する — 各 bit の分離は (7b)/(7c) が担当する)
    try:
        (rw_dir / "new.txt").write_text("x")
    except PermissionError:
        print("FAIL: create was denied on a read-write path")
        sys.exit(1)

    # (3b) read_write パスの **既存ファイルも上書きできる**
    #      (_ACCESS_FS_WRITE_FILE — (3) は MAKE_REG しか pin しない)
    try:
        (rw_dir / "existing.txt").write_text("updated")
    except PermissionError:
        print("FAIL: overwrite was denied on a read-write path")
        sys.exit(1)

    # (4) 列挙していないパスは読めない
    try:
        list(blocked_dir.iterdir())
        print("FAIL: blocked_dir was readable")
        sys.exit(1)
    except PermissionError:
        pass

    # --- レビュー 1 周目 (codex Critical + sonnet Critical) の回帰ピン -------
    # (5) read_only / allowlist 外のファイルを **truncate で破壊できない**。
    #     `TRUNCATE` (ABI v3) を handled_access_fs に宣言していないと、
    #     Landlock は truncate を**判定対象外として素通し**する。実測では
    #     allowlist に一切列挙していない絶対パスのファイルまで 0 バイトに
    #     破壊できた (通常の write は拒否されるので気づきにくい)。
    try:
        os.truncate(ro_dir / "x.txt", 0)
        print("FAIL: truncate succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass
    try:
        os.truncate(blocked_dir / "victim.txt", 0)
        print("FAIL: truncate succeeded on an unlisted path")
        sys.exit(1)
    except PermissionError:
        pass

    # (6) read_write パスは truncate **できる** (handled に入れた以上、rw に
    #     明示付与しないと `open(..., "w")` = O_TRUNC が壊れる)。
    try:
        os.truncate(rw_dir / "existing.txt", 0)
    except PermissionError:
        print("FAIL: truncate was denied on a read-write path")
        sys.exit(1)

    # (7) READ_FILE の positive control (codex Important)。旧稿は
    #     `iterdir()` しか試しておらず、これは `READ_DIR` の検査であって
    #     `READ_FILE` の検査ではない。両マスクから READ_FILE を削っても
    #     red にならなかった。
    if (ro_dir / "x.txt").read_text() != "ok":
        print("FAIL: read-only file content mismatch")
        sys.exit(1)
    if (rw_dir / "new.txt").read_text() != "x":
        print("FAIL: read-write file content mismatch")
        sys.exit(1)

    # (7b) `WRITE_FILE` を `TRUNCATE` から**分離して** pin する。
    #      `Path.write_text()` は `O_TRUNC` を使うため、`TRUNCATE` を
    #      handled に入れた後は TRUNCATE 側で先に拒否され、
    #      **`_READ_ONLY_ACCESS` に WRITE_FILE を足す変異が隠蔽されて
    #      生存する** (レビュー 1 周目の修正直後に指揮者が実測した回帰)。
    #      `open(..., "r+")` = `O_RDWR` は O_TRUNC を伴わないので、
    #      WRITE_FILE だけを直接触れる。
    try:
        with open(ro_dir / "x.txt", "r+") as f:
            f.write("Z")
        print("FAIL: O_RDWR write succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass
    try:
        with open(rw_dir / "existing.txt", "r+") as f:
            f.write("Z")
    except PermissionError:
        print("FAIL: O_RDWR write was denied on a read-write path")
        sys.exit(1)

    # (7c) `MAKE_REG` を **単独で** pin する (レビュー 2 周目 sonnet)。
    #      `Path.write_text()` は MAKE_REG と WRITE_FILE を同時に要求する
    #      ため、(2) では MAKE_REG を分離できず、`_READ_ONLY_ACCESS` に
    #      MAKE_REG を足す変異が生存した。`O_CREAT|O_RDONLY` は MAKE_REG
    #      だけを要求する。
    try:
        fd = os.open(str(ro_dir / "created.txt"), os.O_CREAT | os.O_RDONLY, 0o644)
        os.close(fd)
        print("FAIL: O_CREAT succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (7d) `REMOVE_FILE` の否定側 pin (レビュー 2 周目 sonnet)。read-only
    #      パスのファイルを **削除** できないこと。1 周目の Critical と
    #      同じクラス — 削除できれば `data/agentic.db` を消せる。
    try:
        os.unlink(ro_dir / "x.txt")
        print("FAIL: unlink succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (7e) `REMOVE_DIR` の否定側 pin (レビュー 2 周目 codex + sonnet)。
    #      ro 配下の **既存の空ディレクトリ** を削除できないこと。
    #      (2c) の mkdir 拒否は MAKE_DIR の検査であって REMOVE_DIR では
    #      ないため、ro に REMOVE_DIR を足す変異が生存していた。
    try:
        os.rmdir(ro_dir / "empty")
        print("FAIL: rmdir succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (8) read_write は「専用 workdir」なのでディレクトリを作れる/消せる
    #     (codex Important — MAKE_DIR/REMOVE_DIR が無い領域は workdir として
    #     使えない)。read_only では作れない。
    #      どちらの bit が欠けたか分かるよう try を分ける (codex の指摘)。
    try:
        (rw_dir / "sub").mkdir()
    except PermissionError:
        print("FAIL: mkdir denied on a read-write path")
        sys.exit(1)
    try:
        (rw_dir / "sub").rmdir()
    except PermissionError:
        print("FAIL: rmdir denied on a read-write path")
        sys.exit(1)

    # (9) `REMOVE_FILE` の positive control (rw では削除できる)。
    try:
        os.unlink(rw_dir / "new.txt")
    except PermissionError:
        print("FAIL: unlink denied on a read-write path")
        sys.exit(1)

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
    (ro / "empty").mkdir()      # (7e) REMOVE_DIR の否定 pin 用
    rw = tmp_path / "rw"
    rw.mkdir()
    (rw / "existing.txt").write_text("before")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "victim.txt").write_text("destroy me")

    result = subprocess.run(
        [sys.executable, "-c", _REAL_LANDLOCK_SCRIPT,
         str(ro), str(rw), str(blocked)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# --- Task 5 Section 5-B: _assert_allowlist_excludes_data_dir -----

def test_assert_allowlist_excludes_data_dir_lives_in_landlock_module():
    from agentic_fx.core.landlock import _assert_allowlist_excludes_data_dir
    assert _assert_allowlist_excludes_data_dir is not None


def test_assert_allowlist_excludes_data_dir_passes_when_disjoint(tmp_path):
    from agentic_fx.core.landlock import _assert_allowlist_excludes_data_dir
    data_dir = tmp_path / "data"
    ok = tmp_path / "code"
    ok.mkdir()
    _assert_allowlist_excludes_data_dir([ok], guarded_data_dir=data_dir)  # raise しない


def test_assert_allowlist_excludes_data_dir_rejects_ancestor(tmp_path):
    from agentic_fx.core.landlock import _assert_allowlist_excludes_data_dir
    data_dir = tmp_path / "repo" / "data"
    with pytest.raises(RuntimeError, match="history data"):
        _assert_allowlist_excludes_data_dir([tmp_path / "repo"],
                                            guarded_data_dir=data_dir)


def test_assert_allowlist_excludes_data_dir_rejects_descendant(tmp_path):
    from agentic_fx.core.landlock import _assert_allowlist_excludes_data_dir
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    child = data_dir / "sub"
    child.mkdir()
    with pytest.raises(RuntimeError, match="history data"):
        _assert_allowlist_excludes_data_dir([child], guarded_data_dir=data_dir)


def test_assert_allowlist_excludes_data_dir_rejects_exact_match(tmp_path):
    from agentic_fx.core.landlock import _assert_allowlist_excludes_data_dir
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with pytest.raises(RuntimeError, match="history data"):
        _assert_allowlist_excludes_data_dir([data_dir], guarded_data_dir=data_dir)
