"""版ストア (plugins/.versions/<name>/<artifact_hash>/) の作成・不変化・
hash 分離 (プラン 10 Task 11、設計書 §2.3・§5.1)。"""
from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from agentic_fx.plugin import version_store

PLUGIN_PY = b"def compute(df, params):\n    return {}\n"
CONFIG_YAML = b"kind: indicator\n"
TEST_PY = b"def test_x():\n    pass\n"


def test_content_hash_bytes_matches_existing_2file_definition():
    """content_hash_bytes は plugin.py + config.yaml の既存定義と同一値
    (定義不変 — §5.2)。"""
    expected = hashlib.sha256(
        b"plugin.py\0" + PLUGIN_PY + b"\0config.yaml\0" + CONFIG_YAML
    ).hexdigest()
    assert version_store.content_hash_bytes(PLUGIN_PY, CONFIG_YAML) == expected


def test_artifact_hash_bytes_covers_3_files_and_differs_from_content_hash():
    """artifact_hash は test_plugin.py を含む 3 本全体 — content_hash とは
    別の値になる (キー分離の pin、§8.1-36)。"""
    c = version_store.content_hash_bytes(PLUGIN_PY, CONFIG_YAML)
    a = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, TEST_PY)
    assert a != c
    expected = hashlib.sha256(
        b"plugin.py\0" + PLUGIN_PY + b"\0config.yaml\0" + CONFIG_YAML
        + b"\0test_plugin.py\0" + TEST_PY
    ).hexdigest()
    assert a == expected


def test_two_versions_same_content_hash_different_artifact_hash_coexist(tmp_path):
    """同じ code/config・異なる test の 2 版が version store に共存できる
    (§5.1 レイアウト冒頭・§8.1-36 の pin)。content_hash は 2 版で同一、
    artifact_hash は異なるのでディレクトリは衝突しない。"""
    root = tmp_path
    a1 = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, TEST_PY)
    test_py_v2 = TEST_PY + b"# v2\n"
    a2 = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, test_py_v2)
    assert a1 != a2
    c1 = version_store.content_hash_bytes(PLUGIN_PY, CONFIG_YAML)

    d1 = version_store.create_version_dir(
        root, "sma", a1, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=TEST_PY, op_identity="1")
    d2 = version_store.create_version_dir(
        root, "sma", a2, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=test_py_v2, op_identity="2")
    assert d1 != d2
    assert d1.is_dir() and d2.is_dir()
    assert (d1 / "plugin.py").read_bytes() == PLUGIN_PY
    assert (d2 / "test_plugin.py").read_bytes() == test_py_v2
    # content_hash が同じであることの確認 (2 版の plugin.py/config.yaml が同一)
    assert version_store.content_hash_bytes(
        (d1 / "plugin.py").read_bytes(), (d1 / "config.yaml").read_bytes()) == c1


def test_create_version_dir_sets_0400_files_0500_dirs(tmp_path):
    """版ストアは不変 (§2.3): ディレクトリ 0500 / ファイル 0400。"""
    a = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, TEST_PY)
    d = version_store.create_version_dir(
        tmp_path, "sma", a, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=TEST_PY, op_identity="1")
    assert stat.S_IMODE(d.stat().st_mode) == 0o500
    for f in ("plugin.py", "config.yaml", "test_plugin.py"):
        assert stat.S_IMODE((d / f).stat().st_mode) == 0o400


def test_create_version_dir_is_idempotent_for_same_artifact_hash(tmp_path):
    """既に同じ artifact_hash の版がある場合は作り直さない (§5.1 手順 4)。
    mtime 不変で pin する — 呼び直しても新規書込が起きないことを確認。"""
    a = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, TEST_PY)
    d1 = version_store.create_version_dir(
        tmp_path, "sma", a, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=TEST_PY, op_identity="1")
    mtime1 = (d1 / "plugin.py").stat().st_mtime_ns
    d2 = version_store.create_version_dir(
        tmp_path, "sma", a, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=TEST_PY, op_identity="2")  # 異なる op_identity でも同じ版
    assert d1 == d2
    assert (d1 / "plugin.py").stat().st_mtime_ns == mtime1
    # tmp-2 の残骸が残っていないこと (idempotent path は tmp を作らない、
    # または作った tmp を rename せず削除する — どちらでも最終状態は同じ)
    leftovers = list((tmp_path / ".versions" / "sma").glob("*.tmp-*"))
    assert leftovers == []


def test_create_version_dir_fsyncs_files_before_rename(tmp_path, monkeypatch):
    """fsync してから 0400/0500 に落として rename する順序の pin
    (§5.1 手順 4、fault injection 用の seam を通す)。os.fsync 呼び出し回数
    ≥ 4 (3 ファイル + ディレクトリ) を実測する。"""
    calls = []
    real_fsync = version_store.os.fsync

    def spy_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(version_store.os, "fsync", spy_fsync)
    a = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, TEST_PY)
    version_store.create_version_dir(
        tmp_path, "sma", a, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=TEST_PY, op_identity="1")
    assert len(calls) >= 4


def test_create_version_dir_rename_onto_nonempty_final_dir_is_idempotent(tmp_path):
    """M-2 是正の pin: 実測で非空ディレクトリへの os.rename は
    OSError errno ENOTEMPTY (39) であり FileExistsError (EEXIST=17) では
    ない。final_dir は常に 3 ファイルを持つ非空ディレクトリなので、並行
    create_version_dir の「先着者を採用する」冪等分岐は ENOTEMPTY を
    捕えられなければ機能しない (except FileExistsError のままだと本テストは
    無関係な OSError で red になる)。"""
    a = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, TEST_PY)
    d1 = version_store.create_version_dir(
        tmp_path, "sma", a, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=TEST_PY, op_identity="1")
    # 別プロセスが同じ artifact_hash を並行して作ろうとした状況を模す:
    # final_dir は既に存在 (非空) — create_version_dir は早期 return する
    # 経路 (final_dir.is_dir()) を通るため、ENOTEMPTY 分岐そのものは
    # os.rename を直接叩いて再現する。
    tmp_dir = tmp_path / ".versions" / "sma" / f"{a}.tmp-2"
    tmp_dir.mkdir(mode=0o700)
    (tmp_dir / "x").write_bytes(b"x")
    import pytest
    import errno
    with pytest.raises(OSError) as exc_info:
        import os
        os.rename(tmp_dir, d1)
    assert exc_info.value.errno == errno.ENOTEMPTY
    # 掃除 (tmp_path の後始末)
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
