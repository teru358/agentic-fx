"""Landlock ゲート pytest (プラン10 Task 6、設計書 §4.2-3d、§8.1-10)。

測定 1 件 = 子プロセス 1 個 (Landlock 不可逆)。A-1 の
`agentic_fx.runners.launcher.build_launcher_argv` に依存する。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.landlock import is_available
from agentic_fx.plugin.gate_pytest import (
    CandidateSnapshotError, GateResult, check_candidate_snapshot, hashes_of,
    run_gate_pytest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "settings.yaml.example"

_PASSING_TEST = "def test_ok():\n    assert 1 == 1\n"
_FAILING_TEST = "def test_fail():\n    assert 1 == 2\n"


@pytest.fixture(scope="module")
def settings():
    return load_settings(_EXAMPLE)


def _skip_if_no_landlock():
    if not is_available():
        pytest.skip("Landlock not available on this kernel/architecture")


def _write_manifest(d: Path, *, plugin_py="p", config_yaml="c", test_py="t") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(config_yaml)
    (d / "test_plugin.py").write_text(test_py)


def _write_candidate(tmp_path: Path, *, test_py: str) -> Path:
    d = tmp_path / "candidate"
    d.mkdir()
    (d / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (d / "config.yaml").write_text("kind: indicator\n")
    (d / "test_plugin.py").write_text(test_py)
    return d


# 6-B′: 候補スナップショット検査 + content_hash/artifact_hash の pytest 前後照合


def test_check_candidate_snapshot_accepts_exact_three_files(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d)
    check_candidate_snapshot(d)  # raise しない


def test_check_candidate_snapshot_rejects_extra_file(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d)
    (d / "conftest.py").write_text("x")
    with pytest.raises(CandidateSnapshotError, match="unexpected"):
        check_candidate_snapshot(d)


def test_check_candidate_snapshot_rejects_subdirectory(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d)
    (d / "sub").mkdir()
    with pytest.raises(CandidateSnapshotError, match="unexpected"):
        check_candidate_snapshot(d)


def test_check_candidate_snapshot_rejects_symlink_member(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d)
    outside = tmp_path / "outside.py"; outside.write_text("evil")
    (d / "plugin.py").unlink()
    (d / "plugin.py").symlink_to(outside)
    with pytest.raises(CandidateSnapshotError, match="symlink"):
        check_candidate_snapshot(d)


def test_check_candidate_snapshot_rejects_symlinked_candidate_dir(tmp_path):
    """codex 1 周目是正 I2 (verified-codex-round1.md): 候補ディレクトリ
    自身が外部ディレクトリへの symlink の場合、`os.open(..., O_NOFOLLOW)`
    で追従を拒否すること。errno は環境依存 (ENOTDIR/ELOOP どちらもあり得る
    — O_DIRECTORY|O_NOFOLLOW をシンボリックリンクへ当てると実測では
    ENOTDIR) なので `match=` をそれに依存させない。"""
    outside = tmp_path / "outside"
    _write_manifest(outside)
    link = tmp_path / "cand_link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CandidateSnapshotError):
        check_candidate_snapshot(link)
    # 対照: 通常ディレクトリは受理される
    check_candidate_snapshot(outside)  # raise しない


def test_check_candidate_snapshot_rejects_hardlink_member(tmp_path):
    """`st_nlink == 1` の検査 — hardlink で候補外の実体を共有していないこと。"""
    d = tmp_path / "cand"; _write_manifest(d)
    outside = tmp_path / "shared.py"
    os_link_target = d / "plugin.py"
    os_link_target.unlink()
    outside.write_text("shared")
    os.link(outside, os_link_target)
    with pytest.raises(CandidateSnapshotError, match="nlink"):
        check_candidate_snapshot(d)


def test_check_candidate_snapshot_rejects_oversized_file(tmp_path, monkeypatch):
    import agentic_fx.plugin.gate_pytest as gate_mod
    monkeypatch.setattr(gate_mod, "_MAX_FILE_BYTES", 4)
    d = tmp_path / "cand"; _write_manifest(d, plugin_py="way too long")
    with pytest.raises(CandidateSnapshotError, match="exceeds size limit"):
        check_candidate_snapshot(d)


def test_check_candidate_snapshot_missing_file_is_rejected(tmp_path):
    d = tmp_path / "cand"; d.mkdir()
    (d / "plugin.py").write_text("p")
    (d / "config.yaml").write_text("c")
    with pytest.raises(CandidateSnapshotError, match="missing"):
        check_candidate_snapshot(d)


# <!-- precheck 2026-08-22: T6-B2 --> `check_candidate_snapshot` の無視
# リスト (`__pycache__/`, `*.pyc`, `.pytest_cache/`) — `submit_plugin` の
# 後段 (`_validate_kind` → `sandbox.PluginSession`) が候補ディレクトリを
# cwd に plugin.py を import すると `__pycache__` が残り、無視リストが
# 無いと以後その plugin の submit/bless が恒久的に失敗していた (検収
# Blocking B2)。


def test_check_candidate_snapshot_ignores_pycache_dir(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d)
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "plugin.cpython-313.pyc").write_bytes(b"\x00")
    check_candidate_snapshot(d)  # raise しない


def test_check_candidate_snapshot_ignores_pytest_cache_dir(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d)
    (d / ".pytest_cache").mkdir()
    check_candidate_snapshot(d)  # raise しない


def test_check_candidate_snapshot_ignores_top_level_pyc_file(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d)
    (d / "plugin.pyc").write_bytes(b"\x00")
    check_candidate_snapshot(d)  # raise しない


def test_check_candidate_snapshot_rejects_pycache_that_is_not_a_directory(tmp_path):
    """`__pycache__` という名前の**通常ファイル**は無視リスト対象では
    ない (ディレクトリであることを確認したうえでのみ無視する) — 無視
    リストを名前だけで判定する変異を殺す。"""
    d = tmp_path / "cand"; _write_manifest(d)
    (d / "__pycache__").write_text("not a directory")
    with pytest.raises(CandidateSnapshotError, match="unexpected"):
        check_candidate_snapshot(d)


def test_check_candidate_snapshot_still_rejects_unrelated_extra_file_alongside_pycache(
        tmp_path):
    """無視リストは無条件の免除ではない — `__pycache__` があっても、
    無視リスト外の余分ファイル (`conftest.py`) は依然拒否される。"""
    d = tmp_path / "cand"; _write_manifest(d)
    (d / "__pycache__").mkdir()
    (d / "conftest.py").write_text("x")
    with pytest.raises(CandidateSnapshotError, match="unexpected"):
        check_candidate_snapshot(d)


def test_check_candidate_snapshot_passes_after_plugin_session_execution(
        tmp_path, settings):
    """主 pin: `submit_plugin` の後段が使う `sandbox.PluginSession` を
    実際に起動すると、worker (`agentic_fx.plugin.worker._import_plugin`)
    の `importlib.util.spec_from_file_location` が候補ディレクトリに
    `__pycache__` を書く (`sandbox._build_env` は
    `PYTHONDONTWRITEBYTECODE`/`PYTHONPYCACHEPREFIX` を設定しないため)。
    是正前はこの後の再ゲートが `CandidateSnapshotError` で恒久的に
    失敗していた (検収 Blocking B2) — 是正後は通ることを確認する。"""
    from agentic_fx.plugin.loader import PluginMeta, content_hash as real_content_hash
    from agentic_fx.plugin.sandbox import PluginSession

    d = _write_candidate(tmp_path, test_py=_PASSING_TEST)
    check_candidate_snapshot(d)  # 実行前: 3 本ちょうど、通る

    meta = PluginMeta(name="ind", kind="indicator", path=d, params={},
                      timeframe=None, pairs=(), max_bars=200,
                      content_hash=real_content_hash(d))
    with PluginSession(meta, settings=settings.plugin):
        pass  # __enter__ が plugin.py を import させ __pycache__ を作る

    assert (d / "__pycache__").is_dir(), \
        "前提: PluginSession 実行後は __pycache__ が生成される"
    check_candidate_snapshot(d)  # 再ゲート: __pycache__ があっても通る (B2 是正)


def test_hashes_of_returns_content_and_artifact_hash(tmp_path):
    d = tmp_path / "cand"; _write_manifest(d, plugin_py="p", config_yaml="c", test_py="t")
    content_hash, artifact_hash = hashes_of(d)
    from agentic_fx.plugin.loader import content_hash as loader_content_hash
    from agentic_fx.plugin.loader import artifact_hash_bytes
    assert content_hash == loader_content_hash(d)
    assert artifact_hash == artifact_hash_bytes(b"p", b"c", b"t")


def test_run_gate_pytest_rejects_when_candidate_mutates_itself(tmp_path, settings):
    """主 pin: test_plugin.py が自分自身 (plugin.py) を書き換えようとする
    と ①候補は ro なので書込は EACCES で失敗する ②(fault injection 無しの
    通常経路では) hash も不変のまま — `run_gate_pytest` はこの状態を
    `passed=True` として返してよい (書込自体が拒否されているため、
    候補は無傷)。**副 pin は次のテストで別途、親側で hash を直接
    改ざんして不合格にする形を確認する**。"""
    _skip_if_no_landlock()
    d = _write_candidate(tmp_path, test_py=(
        "from pathlib import Path\n"
        "import pytest\n"
        "def test_self_mutation_denied():\n"
        "    with pytest.raises(PermissionError):\n"
        "        (Path(__file__).parent / 'plugin.py').write_text('OWNED')\n"))
    before_content, before_artifact = hashes_of(d)
    result = run_gate_pytest(d, settings=settings)
    after_content, after_artifact = hashes_of(d)
    assert result.passed is True, result.stdout_tail
    assert after_content == before_content
    assert after_artifact == before_artifact


def test_run_gate_pytest_checks_candidate_snapshot_before_spawning_pytest(
        tmp_path, settings, monkeypatch):
    """B6 (段 0 束 B 致命、`stage0-bundle-B.md`): `check_candidate_snapshot`
    は候補ディレクトリの完全性検査であり、これを pytest 実行 (収集を含む)
    の**前**に行うことが「候補のサブディレクトリに置かれた任意コードが
    収集時に import される」経路 (`loader._reject_unexpected_py_files` は
    直下のファイルのみを見るため) を止める唯一の防御になっている。

    既存の台帳 pin (6-B′ M7) は `grep -n "check_candidate_snapshot"` の
    **静的検査**であり、呼び出しの実行**順序**を入れ替える変異
    (`check_candidate_snapshot` の呼び出しを pytest 実行の**後**へ移す)
    に対して構造的に盲目 (呼び出しは残っているので grep は通る)。

    `check_candidate_snapshot` と `subprocess.Popen` の両方を、呼ばれた
    順に自分の名前を記録してから本体へ委譲するラッパで monkeypatch し、
    正常な候補に対して `run_gate_pytest` を 1 回走らせて
    `calls == ["check_candidate_snapshot", "Popen"]` であることを assert
    する。Landlock も FS 側チャネルも使わない、順序だけを見る pin
    (是正時の判断: report が「採ってはいけない案」として明示的に棄却した
    「候補の sub/conftest.py が痕跡を残さないことを assert する」形の
    振る舞い pin は使わない — 候補 dir は gate 内で ro、workdir は
    `TemporaryDirectory` で `with` を抜けると消え、永続する側チャネルが
    無いため原理的に効かない)。"""
    _skip_if_no_landlock()
    d = _write_candidate(tmp_path, test_py=_PASSING_TEST)

    import agentic_fx.plugin.gate_pytest as gate_mod

    calls: list[str] = []
    real_check = gate_mod.check_candidate_snapshot
    real_popen = gate_mod.subprocess.Popen

    def spy_check(plugin_dir):
        calls.append("check_candidate_snapshot")
        return real_check(plugin_dir)

    def spy_popen(*a, **kw):
        calls.append("Popen")
        return real_popen(*a, **kw)

    monkeypatch.setattr(gate_mod, "check_candidate_snapshot", spy_check)
    monkeypatch.setattr(gate_mod.subprocess, "Popen", spy_popen)

    result = run_gate_pytest(d, settings=settings)

    assert result.passed is True, result.stdout_tail
    assert calls == ["check_candidate_snapshot", "Popen"], (
        f"check_candidate_snapshot が pytest 実行 (Popen) より前に呼ばれて"
        f"いない (B6): {calls!r}")


def test_run_gate_pytest_fails_when_hash_changes_between_before_and_after(
        tmp_path, settings, monkeypatch):
    """副 pin (fault injection): 親側の hash 再計算そのものが機能して
    いることを、pytest 実行の**間**に候補ファイルを書き換える fake で
    確認する — 通常経路では候補は ro なので worker からは起きないが、
    「hash が変われば不合格にする」ロジック自体は独立して検証する。"""
    _skip_if_no_landlock()
    d = _write_candidate(tmp_path, test_py=_PASSING_TEST)

    import agentic_fx.plugin.gate_pytest as gate_mod
    real_hashes_of = gate_mod.hashes_of
    call_count = {"n": 0}

    def tampering_hashes_of(plugin_dir):
        call_count["n"] += 1
        if call_count["n"] == 2:  # after 呼び出しのタイミングで改ざんする
            (plugin_dir / "plugin.py").write_text("TAMPERED")
        return real_hashes_of(plugin_dir)

    monkeypatch.setattr(gate_mod, "hashes_of", tampering_hashes_of)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is False
    assert "hash" in result.stdout_tail.lower() or "content changed" in result.stdout_tail.lower()


def test_run_gate_pytest_passes_for_passing_test(tmp_path, settings):
    _skip_if_no_landlock()
    d = _write_candidate(tmp_path, test_py=_PASSING_TEST)
    result = run_gate_pytest(d, settings=settings)
    assert isinstance(result, GateResult)
    assert result.passed is True
    assert result.returncode == 0


def test_run_gate_pytest_fails_for_failing_test(tmp_path, settings):
    _skip_if_no_landlock()
    d = _write_candidate(tmp_path, test_py=_FAILING_TEST)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is False
    assert result.returncode != 0


def test_run_gate_pytest_candidate_dir_is_read_only(tmp_path, settings):
    """test_plugin.py が自分の候補ディレクトリへ書こうとすると EACCES —
    候補は read-only (§4.2-3e の主 pin)。"""
    _skip_if_no_landlock()
    write_attempt = (
        "from pathlib import Path\n"
        "import pytest\n"
        "def test_write_denied():\n"
        "    with pytest.raises(PermissionError):\n"
        "        (Path(__file__).parent / 'plugin.py').write_text('OWNED')\n")
    d = _write_candidate(tmp_path, test_py=write_attempt)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is True, result.stdout_tail


def test_run_gate_pytest_cannot_open_agentic_db(tmp_path, settings, monkeypatch):
    """ゲート子プロセスから `data/agentic.db` を開こうとすると EACCES
    (§8.1-10)。親が絶対パスを子へ明示的に渡し、それでも開けないことを
    確認する非対称設計 (申し送り⑨) — repo 直下の実 `data/agentic.db`
    ではなく、gate worker が受け取る argv 経由のパスを使う。"""
    _skip_if_no_landlock()
    # 実機 E2E (2026-08-30) で発覚した実データ破壊の是正: 旧実装は
    # `_REPO_ROOT / "data" / "agentic.db"` を存在チェックなしで
    # `write_bytes(b"test")` 上書きし finally で unlink していた —
    # **フルスイートを repo cwd で回すたびに実 DB が消える**。
    # テスト意図 (argv で明示的に渡された絶対パスでも gate worker からは
    # EACCES) は probe パスが gate allowlist 外でありさえすれば成立するので、
    # 隣の test_run_gate_pytest_cannot_read_unrelated_tmp_file と同じく
    # 使い捨ての外部ディレクトリに probe を置く。実 data/ には触れない。
    import tempfile
    probe_dir = tempfile.mkdtemp(prefix="afx-probe-db-")
    probe_db = Path(probe_dir) / "agentic.db"
    probe_db.write_bytes(b"test")
    try:
        check_db_access = (
            "def test_db_is_eacces():\n"
            f"    import pytest\n"
            f"    with pytest.raises((PermissionError, FileNotFoundError)):\n"
            f"        open({str(probe_db)!r}, 'rb')\n")
        d = _write_candidate(tmp_path, test_py=check_db_access)
        result = run_gate_pytest(d, settings=settings)
        assert result.passed is True, result.stdout_tail
    finally:
        import shutil as _shutil
        _shutil.rmtree(probe_dir, ignore_errors=True)


def test_run_gate_pytest_cannot_read_unrelated_tmp_file(tmp_path, settings):
    """`/tmp` 配下の gate workdir 以外のファイルは EACCES であること
    (2026-08-22 検収: 前任が read_only allowlist へ `/tmp` を丸ごと追加
    していた逸脱の撤回 pin)。`WorkerRunner` の workdir (認証コピーを含む)
    も `tempfile.TemporaryDirectory` = `/tmp` 配下なので、同 uid のゲート
    worker に `/tmp` を read させると他 Mission の資格情報が読めてしまう
    — gate worker は自分の gate workdir (`--basetemp`/`TMPDIR` で明示的に
    渡された領域) 以外の `/tmp` 配下に到達できないことを実プロセスで
    確認する。"""
    _skip_if_no_landlock()
    import tempfile
    outside_dir = tempfile.mkdtemp(prefix="afx-outside-gate-")
    outside_file = Path(outside_dir) / "secret.txt"
    outside_file.write_text("other mission's credentials")
    try:
        check_tmp_access = (
            "def test_unrelated_tmp_file_is_eacces():\n"
            f"    import pytest\n"
            f"    with pytest.raises((PermissionError, FileNotFoundError)):\n"
            f"        open({str(outside_file)!r}, 'rb')\n")
        d = _write_candidate(tmp_path, test_py=check_tmp_access)
        result = run_gate_pytest(d, settings=settings)
        assert result.passed is True, result.stdout_tail
    finally:
        outside_file.unlink(missing_ok=True)
        os.rmdir(outside_dir)


def test_run_gate_pytest_candidate_can_use_tmp_path_fixture(tmp_path, settings):
    """B2/3 (`stage0-bundle-B.md` Minor) 再実測メモ: 既存の
    `cannot_read_unrelated_tmp_file` は候補が「明示的に外部の tmp file
    を開く」経路のみを見ており、`TMPDIR`/`--basetemp` の配線そのもの
    (候補が pytest 標準の `tmp_path` フィクスチャを使う経路) を一度も
    踏んでいなかった。単独再実測 (`TMPDIR` の env 追加と `--basetemp`
    引数の両方を削る変異を注入): この pin も含めフルスイートは緑のまま
    ——**真の SURVIVED を再確認した**が、実測の結果 `TMPDIR`/`--basetemp`
    を落としても `/tmp`/`/var/tmp`/`/usr/tmp` は Landlock の read_write
    allowlist に無いため書込が EACCES になり、CPython
    `tempfile._get_default_tempdir()` は候補リストの最後の要素である
    `os.getcwd()` (= gate worker の `cwd=workdir`) へ自動的にフォール
    バックする — 結果として `TMPDIR`/`--basetemp` が無くても tmp_path は
    依然として gate workdir 配下に閉じ込められる (Landlock 自体が既に
    fail closed で、`stage0-bundle-B.md` の「docstring 過大主張」という
    評価どおり)。したがって本テストは退行防止としては有用だが、
    `TMPDIR`/`--basetemp` 削除変異を殺すテストとしては機能しない
    (この環境では経験的に kill 不能) — 追加の pin は見送り、この事実を
    記録するに留める (`measurement-is-environment-bound`)。"""
    _skip_if_no_landlock()
    use_tmp_path_fixture = (
        "def test_uses_tmp_path_fixture(tmp_path):\n"
        "    (tmp_path / 'x.txt').write_text('ok')\n"
        "    assert (tmp_path / 'x.txt').read_text() == 'ok'\n")
    d = _write_candidate(tmp_path, test_py=use_tmp_path_fixture)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is True, result.stdout_tail


def test_gate_pytest_worker_argv_pins_cacheprovider_and_rootdir():
    """B4a/B4b (`stage0-bundle-B.md` Minor): `-p no:cacheprovider` の削除
    (単独)、および削除 + `--rootdir` を `workdir` → `plugin_dir` に差し
    替える変異は、いずれも実プロセスの振る舞い pin ではフルスイート緑の
    まま生存する (候補 dir が ro のため `.cache` 書込は他の理由で
    fail closed し、`--rootdir` の変更も収集結果を変えない — 台帳 6-B
    M7 / 是正 C4 の「M7 は恒久的に unkillable」を裏取り済み)。振る舞いで
    殺せない「無いと壊れるが、あっても観測されない」ハードニングは、
    `stage0-bundle-B.md` §台帳へのフィードバック 6 が推奨するとおり
    argv の**静的 pin** (ソース文字列上に期待する引数が存在すること) に
    置き換える。"""
    worker_src = (
        Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
        / "plugin" / "gate_pytest_worker.py").read_text()
    call_start = worker_src.index("rc = pytest.main([")
    call_end = worker_src.index("])", call_start) + 2
    argv_literal = worker_src[call_start:call_end]
    assert '"-p", "no:cacheprovider"' in argv_literal, (
        "gate_pytest_worker.py の pytest.main argv から "
        "'-p', 'no:cacheprovider' が消えている")
    assert '"--rootdir", str(workdir)' in argv_literal, (
        "--rootdir の値が workdir でなくなっている "
        "(plugin_dir 等へのすり替えを検出)")


def test_run_gate_pytest_asserts_pycache_prefix(tmp_path, settings):
    """gate worker が起動直後に `sys.pycache_prefix` を assert する —
    `PYTHONPYCACHEPREFIX` を Popen env に置かない変異は red になる。"""
    _skip_if_no_landlock()
    check_pycache = (
        "import sys\n"
        "def test_pycache_prefix_is_set():\n"
        "    assert sys.pycache_prefix is not None\n")
    d = _write_candidate(tmp_path, test_py=check_pycache)
    result = run_gate_pytest(d, settings=settings)
    assert result.passed is True, result.stdout_tail


def test_run_gate_pytest_fails_closed_when_landlock_unavailable(tmp_path, settings, monkeypatch):
    import agentic_fx.plugin.gate_pytest as gate_mod
    monkeypatch.setattr(gate_mod, "is_available", lambda: False)
    d = _write_candidate(tmp_path, test_py=_PASSING_TEST)
    with pytest.raises(RuntimeError, match="[Ll]andlock"):
        run_gate_pytest(d, settings=settings)


def test_run_gate_pytest_times_out(tmp_path, settings):
    _skip_if_no_landlock()
    from agentic_fx.config import PluginSettings
    short_timeout_settings = settings.model_copy(
        update={"plugin": settings.plugin.model_copy(
            update={"pytest_timeout_sec": 0.5})})
    slow_test = "import time\ndef test_slow():\n    time.sleep(5)\n"
    d = _write_candidate(tmp_path, test_py=slow_test)
    result = run_gate_pytest(d, settings=short_timeout_settings)
    assert result.passed is False


def test_gate_pytest_does_not_use_preexec_fn():
    """M6 静的 pin: preexec_fn を使っていないこと。"""
    import inspect
    from agentic_fx.plugin import gate_pytest
    src = inspect.getsource(gate_pytest)
    assert "preexec_fn" not in src
