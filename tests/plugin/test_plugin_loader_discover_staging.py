"""discover の `_`/`.` 除外・名前正規形・symlink 追従・artifact_hash
(プラン10 Task 5、設計書 §2.3)。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agentic_fx.plugin.loader import discover, PluginMeta

INDICATOR_PY = "def compute(df, params):\n    return {'v': 1.0}\n"
CONFIG_YAML = "kind: indicator\n"
TEST_PY = "def test_x():\n    pass\n"


def _write_plugin_files(d: Path, *, plugin_py=INDICATOR_PY,
                        config_yaml=CONFIG_YAML, test_py=TEST_PY) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(config_yaml)
    (d / "test_plugin.py").write_text(test_py)


def _artifact_hash(plugin_py: str, config_yaml: str, test_py: str) -> str:
    return hashlib.sha256(
        b"plugin.py\0" + plugin_py.encode() + b"\0config.yaml\0" +
        config_yaml.encode() + b"\0test_plugin.py\0" + test_py.encode()
    ).hexdigest()


def test_discover_skips_underscore_prefixed_dirs(tmp_path):
    """(着手前検証 Blocking 4 修正) 現行 `discover` は `plugins_dir.iterdir()`
    の**直下**しか見ない — 旧稿は `_staging/m-001/cand/` (2 階層下) に
    3 ファイルを置いており、現行コードでも `_staging` 直下には 3 ファイル
    が無い (`missing [...] — skipping` で偶然 skip) ため新設フィルタを
    一切 pin していなかった。3 ファイルを `_staging` **直下**に置き、
    新設フィルタが無ければ検出されてしまう (= 現行コードなら拾われる)
    配置にする。"""
    plugins_dir = tmp_path / "plugins"
    _write_plugin_files(plugins_dir / "_staging")
    metas = discover(plugins_dir)
    assert metas == []


def test_discover_skips_dot_prefixed_dirs(tmp_path):
    """(着手前検証 Blocking 4 修正) 同上 — `.versions` 直下に 3 ファイルを
    置く (旧稿の `foo/deadbeef` という 2 階層下ではなく)。"""
    plugins_dir = tmp_path / "plugins"
    _write_plugin_files(plugins_dir / ".versions")
    metas = discover(plugins_dir)
    assert metas == []


def test_discover_does_not_skip_a_normally_named_dir_with_the_same_files(tmp_path):
    """(着手前検証 Blocking 4、対照テスト) 上記 2 本と全く同じ 3 ファイルを
    正規形の名前 `staging_like` に置くと 1 件検出される — フィルタが
    「名前の先頭文字」だけを見ていて、ファイル内容やディレクトリ深さでは
    ないことを固定する。"""
    plugins_dir = tmp_path / "plugins"
    _write_plugin_files(plugins_dir / "staging_like")
    metas = discover(plugins_dir)
    assert [m.name for m in metas] == ["staging_like"]


def test_discover_rejects_non_canonical_name(tmp_path):
    """`Foo-bar` のような大文字・ハイフンを含む名前は skip される。"""
    plugins_dir = tmp_path / "plugins"
    _write_plugin_files(plugins_dir / "Foo-bar")
    metas = discover(plugins_dir)
    assert metas == []


def test_discover_accepts_canonical_name(tmp_path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin_files(plugins_dir / "sma_cross_v2")
    metas = discover(plugins_dir)
    assert [m.name for m in metas] == ["sma_cross_v2"]


def test_discover_computes_artifact_hash_for_plain_dir(tmp_path):
    plugins_dir = tmp_path / "plugins"
    _write_plugin_files(plugins_dir / "ind")
    metas = discover(plugins_dir)
    expected = _artifact_hash(INDICATOR_PY, CONFIG_YAML, TEST_PY)
    assert metas[0].artifact_hash == expected


def test_discover_follows_canonical_symlink_and_fixes_path_to_version_dir(tmp_path):
    """live symlink `plugins/<name>` → `.versions/<name>/<artifact_hash>` を
    追従し、`PluginMeta.path` を版ディレクトリの実体に固定する
    (resolve() は使わない — リンク先文字列を字句検証)。

    **(着手前検証 Blocking 5 修正)** 版ディレクトリ名は **実際の
    artifact_hash** (`_artifact_hash(INDICATOR_PY, CONFIG_YAML,
    TEST_PY)`) にする — 旧稿は `"a"*64` という任意値を使っており、
    discover 実装 (Step 3) はディレクトリ名と実計算 hash の不一致を
    reject するため、正常系のはずのこのテストが必ず `metas == []` に
    落ちて `test_discover_rejects_directory_name_artifact_hash_mismatch`
    (`"f"*64` で同じ `[]` を期待) と矛盾していた。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    real_hash = _artifact_hash(INDICATOR_PY, CONFIG_YAML, TEST_PY)
    version_dir = plugins_dir / ".versions" / "ind" / real_hash
    _write_plugin_files(version_dir)
    (plugins_dir / "ind").symlink_to(
        Path(".versions") / "ind" / real_hash, target_is_directory=True)
    metas = discover(plugins_dir)
    assert len(metas) == 1
    assert metas[0].name == "ind"
    assert metas[0].path.resolve() == version_dir.resolve()


def test_discover_rejects_symlink_pointing_outside_versions_dir(tmp_path):
    """字句検査: リンク先が `.versions/<同じ name>/<hash>` の正規形に
    一致しない symlink は reject される (`../../etc/passwd` 等の逃避)。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    outside = tmp_path / "outside"
    _write_plugin_files(outside)
    (plugins_dir / "ind").symlink_to(outside, target_is_directory=True)
    metas = discover(plugins_dir)
    assert metas == []


def test_discover_rejects_symlink_with_mismatched_name_in_target(tmp_path):
    """リンク先の `<name>` 成分が symlink 自身の名前と食い違う
    (`plugins/ind` → `.versions/other/<hash>`)。**(着手前検証 Blocking 5
    確認)** 版ディレクトリ名 `"b"*64` は実 artifact_hash と一致しない
    任意値のままでよい — このテストが reject を検出する理由は「name
    不一致」であり、hash 照合まで到達する前に落ちるため hash の正誤は
    無関係 (`test_discover_follows_canonical_symlink_and_fixes_path_to_
    version_dir` とは異なり、ここでは意図的に区別している)。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    version_dir = plugins_dir / ".versions" / "other" / ("b" * 64)
    _write_plugin_files(version_dir)
    (plugins_dir / "ind").symlink_to(
        Path(".versions") / "other" / ("b" * 64), target_is_directory=True)
    metas = discover(plugins_dir)
    assert metas == []


def test_discover_rejects_symlink_whose_target_name_differs_even_when_hash_is_correct(
        tmp_path):
    """(実装時追加、変異表は下限 — mutation-ledger-task5.md 5-F M4 参照)
    `test_discover_rejects_symlink_with_mismatched_name_in_target` は版
    ディレクトリ名を `"b"*64`(実 hash と不一致)にしているため、`<name>`
    一致検査を削除する変異 (M4) を注入しても hash 照合が独立に reject して
    しまい、当該テストは生存判定できない (実測確認済み)。ここでは
    版ディレクトリ名を**正しい** artifact_hash にし、`<name>` 成分だけを
    symlink 自身の名前と食い違わせる — hash 照合を通過させ、`<name>` 一致
    検査だけを唯一の reject 根拠にする (Blocking 11 が M3→M6 に施した
    修正と同型)。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    real_hash = _artifact_hash(INDICATOR_PY, CONFIG_YAML, TEST_PY)
    version_dir = plugins_dir / ".versions" / "other" / real_hash
    _write_plugin_files(version_dir)
    (plugins_dir / "ind").symlink_to(
        Path(".versions") / "other" / real_hash, target_is_directory=True)
    metas = discover(plugins_dir)
    assert metas == []


def test_discover_rejects_directory_name_artifact_hash_mismatch(tmp_path):
    """版ディレクトリ名 (= artifact_hash) と実計算が不一致なら拒否
    (in-place 編集の検出)。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    wrong_hash = "f" * 64
    version_dir = plugins_dir / ".versions" / "ind" / wrong_hash
    _write_plugin_files(version_dir)
    (plugins_dir / "ind").symlink_to(
        Path(".versions") / "ind" / wrong_hash, target_is_directory=True)
    metas = discover(plugins_dir)
    assert metas == []


def test_discover_plain_dir_still_works_unchanged(tmp_path):
    """既存プレーン plugin (symlink でない) は挙動不変 — 回帰 pin。"""
    plugins_dir = tmp_path / "plugins"
    _write_plugin_files(plugins_dir / "legacy_plain")
    metas = discover(plugins_dir)
    assert len(metas) == 1
    assert metas[0].path == plugins_dir / "legacy_plain"


def test_discover_rejects_symlink_with_valid_hash_name_pointing_outside_versions_dir(
        tmp_path):
    """(着手前検証 Blocking 11) 設計書 §2.3 が明示的に禁じる形 —
    「リンク先が正規形かどうかを `resolve()` の結果で判定する」実装への
    劣化 (変異 M6) を殺す。`test_discover_rejects_symlink_pointing_
    outside_versions_dir` は `outside` という**絶対パス**を使うため、
    symlink 追従後 `entry.is_symlink()` は True だが hash 照合
    (`resolved.name == "outside"` が実 hash と不一致) の方で reject
    される — 字句検証を削除しても hash 照合が偶然カバーしてしまい、
    M6 は生存したまま (実測で確認済み)。ここでは **`.versions` の外だが
    正しい 64hex artifact_hash という名前を持つディレクトリ**
    (`plugins/elsewhere/<正しい hash>/`) を用意し、`plugins/ind` から
    `../elsewhere/<同じ hash>` へ symlink する — hash 照合は通ってしまう
    ため、字句検証 (`pattern.match(target)`) だけが reject の唯一の
    根拠になる。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    real_hash = _artifact_hash(INDICATOR_PY, CONFIG_YAML, TEST_PY)
    elsewhere_dir = plugins_dir / "elsewhere" / real_hash
    _write_plugin_files(elsewhere_dir)
    (plugins_dir / "ind").symlink_to(
        Path("..") / "plugins" / "elsewhere" / real_hash, target_is_directory=True)
    metas = discover(plugins_dir)
    assert metas == []
