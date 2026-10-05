"""plugin の 2 ファイルを上限つきで読む共有部品。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.core import plugin_files
from agentic_fx.plugin import loader, version_store, worker_isolation


def _plugin_dir(base: Path, plugin: bytes = b"x = 1\n", config: bytes = b"kind: signal\n") -> Path:
    d = base / "p"
    d.mkdir()
    (d / "plugin.py").write_bytes(plugin)
    (d / "config.yaml").write_bytes(config)
    return d


def test_module_import_does_not_load_numpy_or_pandas():
    code = ("import sys; import agentic_fx.core.plugin_files; "
            "print(int('numpy' in sys.modules), int('pandas' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.split()
    assert out == ["0", "0"]


def test_limit_has_one_origin_for_the_parent_loader_and_the_worker():
    assert loader._MAX_FILE_BYTES == plugin_files.MAX_PLUGIN_FILE_BYTES == 1_048_576
    # worker_isolation は値を再定義せず core.plugin_files の定数を import している
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(worker_isolation))
    imported = [a for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and n.module == "agentic_fx.core.plugin_files" for a in n.names]
    assert any(a.name == "MAX_PLUGIN_FILE_BYTES" for a in imported)
    literals = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
                and n.value == plugin_files.MAX_PLUGIN_FILE_BYTES]
    assert not literals
    assert worker_isolation.MAX_PLUGIN_FILE_BYTES == plugin_files.MAX_PLUGIN_FILE_BYTES
    # content_hash の式は core.plugin_files が持ち、版ストアはそれを参照する
    assert plugin_files.content_hash_bytes is version_store.content_hash_bytes
    assert plugin_files.content_hash_bytes.__module__ == "agentic_fx.core.plugin_files"


def test_core_plugin_files_does_not_import_the_plugin_package():
    code = ("import sys; import agentic_fx.core.plugin_files; "
            "print(int(any(m.startswith('agentic_fx.plugin') for m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.split()
    assert out == ["0"]


def test_content_hash_is_unchanged_for_files_within_the_limit(tmp_path):
    d = _plugin_dir(tmp_path, b"a" * plugin_files.MAX_PLUGIN_FILE_BYTES)
    expected = version_store.content_hash_bytes(
        (d / "plugin.py").read_bytes(), (d / "config.yaml").read_bytes())
    assert loader.content_hash(d) == expected
    assert plugin_files.content_hash_of_dir(d) == expected


@pytest.mark.parametrize("name", ["plugin.py", "config.yaml"])
def test_one_byte_over_the_limit_is_file_too_large(tmp_path, name):
    d = _plugin_dir(tmp_path)
    (d / name).write_bytes(b"a" * (plugin_files.MAX_PLUGIN_FILE_BYTES + 1))
    with pytest.raises(plugin_files.PluginFileTooLarge) as info:
        loader.content_hash(d)
    assert info.value.reason == "file_too_large"
    assert isinstance(info.value, ValueError)


def test_fifo_in_place_of_a_plugin_file_does_not_block(tmp_path):
    d = _plugin_dir(tmp_path)
    (d / "config.yaml").unlink()
    os.mkfifo(d / "config.yaml")
    with pytest.raises(OSError, match="not a regular file"):
        loader.content_hash(d)


def test_missing_file_is_still_an_os_error(tmp_path):
    d = _plugin_dir(tmp_path)
    (d / "plugin.py").unlink()
    with pytest.raises(FileNotFoundError):
        loader.content_hash(d)


_RSS_PROBE = """
import json, resource, sys
from agentic_fx.core import plugin_files
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
try:
    plugin_files.content_hash_of_dir(sys.argv[1])
    outcome = "ok"
except plugin_files.PluginFileTooLarge:
    outcome = "file_too_large"
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"outcome": outcome, "growth_kib": after - before}))
"""


def test_parent_hash_memory_does_not_grow_with_the_file_size(tmp_path):
    """上限を大きく超えるファイル (疎ファイル 512 MiB) でも、読むのは上限 + 1 bytes だけ。"""
    d = _plugin_dir(tmp_path)
    with open(d / "plugin.py", "wb") as f:
        f.truncate(512 * 1024 * 1024)
    out = subprocess.run([sys.executable, "-c", _RSS_PROBE, str(d)], capture_output=True,
                         text=True, check=True, timeout=60).stdout
    probe = json.loads(out)
    assert probe["outcome"] == "file_too_large"
    assert probe["growth_kib"] < 16 * 1024
