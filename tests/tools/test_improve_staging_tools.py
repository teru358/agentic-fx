"""improve registry の候補置き場ツール (設計書 §3.4/§2.3)。"""
from __future__ import annotations

import json

import pytest

from agentic_fx.tools.improve_staging_tools import build_improve_staging_tooldefs
from agentic_fx.tools.registry import ToolRegistry


def _build(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    tools = build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir)
    return {t.name: t for t in tools}, staging_dir, source_dir


def test_write_then_read_staging_file_roundtrips(tmp_path):
    tools, staging_dir, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name="rsi_v2", rel="plugin.py", content="x = 1\n")
    assert "error" not in out
    got = tools["read_staging_file"].func(name="rsi_v2", rel="plugin.py")
    assert got["content"] == "x = 1\n"
    assert (staging_dir / "rsi_v2" / "plugin.py").read_text() == "x = 1\n"


def test_list_staging_reports_names_and_files(tmp_path):
    tools, _, _ = _build(tmp_path)
    tools["write_staging_file"].func(name="a", rel="plugin.py", content="1")
    tools["write_staging_file"].func(name="a", rel="config.yaml", content="2")
    out = tools["list_staging"].func()
    assert out == {"candidates": [{"name": "a", "files": ["config.yaml", "plugin.py"]}]}


def test_write_staging_file_rejects_rel_outside_allowed_set(tmp_path):
    tools, _, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name="a", rel="not_allowed.txt", content="x")
    assert "error" in out


@pytest.mark.parametrize("evil_rel", ["../../etc/passwd", "/etc/passwd",
                                       "..\\..\\etc\\passwd"])
def test_write_staging_file_rejects_path_traversal_in_rel(tmp_path, evil_rel):
    tools, staging_dir, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name="a", rel=evil_rel, content="x")
    assert "error" in out
    assert not (staging_dir.parent / "etc").exists()


@pytest.mark.parametrize("evil_name", ["../a", "a/b", "A", "1a", ""])
def test_write_staging_file_rejects_non_canonical_name(tmp_path, evil_name):
    tools, _, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name=evil_name, rel="plugin.py", content="x")
    assert "error" in out


def test_read_plugin_source_reads_from_source_snapshot_dir_only(tmp_path):
    tools, _, source_dir = _build(tmp_path)
    (source_dir / "rsi_v1").mkdir()
    (source_dir / "rsi_v1" / "plugin.py").write_text("y = 2\n")
    out = tools["read_plugin_source"].func(name="rsi_v1")
    assert out["plugin.py"] == "y = 2\n"


def test_read_plugin_source_absent_name_returns_error_not_raise(tmp_path):
    tools, _, _ = _build(tmp_path)
    out = tools["read_plugin_source"].func(name="does_not_exist")
    assert "error" in out


def test_run_plugin_tests_reports_participant_result(tmp_path):
    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "ok_case").mkdir()
    (staging_dir / "ok_case" / "test_plugin.py").write_text(
        "def test_x():\n    assert 1 == 1\n")
    out = tools["run_plugin_tests"].func(name="ok_case")
    assert out["passed"] is True


def test_run_plugin_tests_reports_failure_without_raising(tmp_path):
    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "bad_case").mkdir()
    (staging_dir / "bad_case" / "test_plugin.py").write_text(
        "def test_x():\n    assert 1 == 2\n")
    out = tools["run_plugin_tests"].func(name="bad_case")
    assert out["passed"] is False


def test_staging_tools_return_single_layer_json_via_registry(tmp_path):
    """B1 (検収 Blocking): `ToolRegistry.execute` が `json.dumps(result, ...)`
    で自ら直列化する契約 (`registry.py`) なので、5 関数は native dict を
    返さなければならない。ここでは 5 種すべてを実 `ToolRegistry.execute`
    経由で叩き、agent が受け取る文字列が **1 段の JSON** であること
    (= `json.loads` した中身がすでに dict/list/str/... であり、それ自体が
    さらに JSON 文字列になっていないこと) を pin する。"""
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_dir = tmp_path / "source"; source_dir.mkdir()
    (source_dir / "rsi_v1").mkdir()
    (source_dir / "rsi_v1" / "plugin.py").write_text("y = 2\n")

    registry = ToolRegistry()
    registry.register_all(build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir))
    names = registry.names()

    calls = {
        "list_staging": {},
        "write_staging_file": {"name": "rsi_v2", "rel": "plugin.py", "content": "x = 1\n"},
        "read_staging_file": {"name": "rsi_v2", "rel": "plugin.py"},
        "read_plugin_source": {"name": "rsi_v1"},
        "run_plugin_tests": {"name": "does_not_exist"},
    }
    for name, args in calls.items():
        raw = registry.execute(name, args, names)
        loaded = json.loads(raw)
        assert not isinstance(loaded, str), (
            f"{name}: agent が受け取った値が二重エンコードされた JSON 文字列 "
            f"のまま (loaded={loaded!r})")


def test_write_staging_file_rejects_symlinked_candidate_dir(tmp_path):
    """7-B M3 (検収 問4): `_NAME_RE` が `.`/`/` を弾くため `name` への
    `../` は M1 で落ち M3 (`resolve()` 後の traversal 検査) には到達しない。
    有効な `name` だが実体が staging 外を指す symlink だけが M3 を殺せる。"""
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_dir = tmp_path / "source"; source_dir.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    # 有効な name (正規形を満たす) だが実体は staging の外を指す symlink
    (staging_dir / "evil").symlink_to(outside, target_is_directory=True)

    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir)}
    out = tools["write_staging_file"].func(
        name="evil", rel="plugin.py", content="PWNED")
    assert "error" in out, out
    assert not (outside / "plugin.py").exists(), "staging 外へ書き込まれた"
