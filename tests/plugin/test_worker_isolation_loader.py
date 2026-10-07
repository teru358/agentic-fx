"""source-only loader (`worker_isolation.load_plugin_module`) を隔離済みの実子 process で確かめる。

子は隔離段を通した後に、検査済み dir fd から plugin を読む。親 (pytest) は子が隔離を
終えた後にファイルを書き換え・差し替えて、loader の分類を見る。
"""
from __future__ import annotations

import importlib.util
import marshal
import os
import sysconfig
import time
from pathlib import Path

import pytest

from agentic_fx.plugin import loader, version_store
from agentic_fx.plugin import worker_isolation as wi

from ._worker_isolation_support import (
    REPO,
    SIGSYS,
    cfg_for,
    copy_plugin,
    handshake,
    needs_sandbox,
    run_child,
    write_plugin,
)

_EVAL = """
import numpy as np
import pandas as pd
from agentic_fx.core.plugin_contract import validate_indicator_result
n = 260
i = np.arange(n)
close = 150.0 + 2.0 * np.sin(i * 0.21) + 0.6 * np.sin(i * 0.047) + 0.01 * i
idx = pd.date_range("2026-01-05", periods=n, freq="h", tz="UTC")
df = pd.DataFrame({"open": close, "high": close + 0.05, "low": close - 0.05, "close": close,
                   "volume": 1.0}, index=idx)
params = {"oversold": 30, "overbought": 70, "stop_loss_pips": 30, "take_profit_pips": 60,
          "pip_size": 0.01}
decisions = []
for end in range(30, n + 1):
    sub = df.iloc[:end]
    dep = sub.tail(200).copy(deep=True)
    v = validate_indicator_result(ind.compute(dep, {"period": 14}), df_index=dep.index,
                                  outputs=("rsi",))
    inds = {"rsi": {k: (s.reindex(sub.index) if isinstance(s, pd.Series) else s)
                    for k, s in v.items()}}
    decisions.append(strat.evaluate(sub, inds, None, params))
out["decisions"] = decisions
out["actions"] = sorted({d["action"] for d in decisions})
"""

_LOAD_ISOLATED = ("strat = wi.load_plugin_module(iso.main)\n"
                  "ind = wi.load_plugin_module(iso.indicators[0])\n")

_LOAD_PLAIN = """
import importlib.util
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
strat = _load("plugin", cfg["main_dir"] + "/plugin.py")
ind = _load("indicator_rsi", cfg["handshake"]["indicators"][0]["plugin_py"])
"""


def _strategy_layout(root: Path) -> tuple[Path, Path]:
    strat = copy_plugin("rsi_pullback", root)
    ind = copy_plugin("rsi_indicator", root)
    return strat, ind


def _pyc_snapshot(root: Path) -> set[str]:
    return {str(p) for p in root.rglob("*.pyc")} | {str(p) for p in root.rglob("__pycache__")}


# --- examples の完走 ---------------------------------------------------------------

@needs_sandbox
def test_examples_strategy_with_indicator_complete_and_match_the_unisolated_run(tmp_path):
    strat, ind = _strategy_layout(tmp_path)
    hs = handshake(strat, (("rsi", ind),))
    venv = Path(sysconfig.get_paths()["purelib"]).resolve()
    venv_before = _pyc_snapshot(venv / "numpy") | _pyc_snapshot(venv / "pandas")

    # -B を付けずに起こす: bytecode を書かないのは worker 自身の設定による
    isolated = run_child(cfg_for(strat, hs, post=_LOAD_ISOLATED + _EVAL), write_bytecode=True)
    plain = run_child(cfg_for(strat, hs, isolate=False, post=_LOAD_PLAIN + _EVAL))

    assert isolated.rc == 0, isolated.stderr
    assert isolated.out["decisions"] == plain.out["decisions"]
    assert len(isolated.out["decisions"]) == 231
    assert "open" in isolated.out["actions"]
    assert not list(strat.rglob("__pycache__")) and not list(ind.rglob("__pycache__"))
    assert _pyc_snapshot(venv / "numpy") | _pyc_snapshot(venv / "pandas") == venv_before


@needs_sandbox
def test_indicator_alone_loads_and_computes_under_isolation(tmp_path):
    main = copy_plugin("rsi", tmp_path)
    post = ("import pandas as pd, numpy as np\n"
            "from agentic_fx.core.plugin_contract import validate_indicator_result\n"
            "m = wi.load_plugin_module(iso.main)\n"
            "idx = pd.date_range('2026-01-05', periods=60, freq='h', tz='UTC')\n"
            "c = 150 + np.sin(np.arange(60) * 0.3)\n"
            "df = pd.DataFrame({'open': c, 'high': c, 'low': c, 'close': c, 'volume': 1.0},"
            " index=idx)\n"
            "v = validate_indicator_result(m.compute(df, {'period': 14}), df_index=idx,"
            " outputs=('rsi',))\n"
            "out['valid'] = int(v['rsi'].notna().sum())\n")
    res = run_child(cfg_for(main, post=post), write_bytecode=True)
    assert res.out["valid"] == 60 - 14
    assert not list(main.rglob("__pycache__"))


# --- #40: bytecode cache の無い環境 ---------------------------------------------------

_SNAPSHOT_PREFIX = """
import os as _os
emit({"phase": "prefix", "files": sorted(_os.path.join(d, f)
      for d, _s, fs in _os.walk(_os.environ["PYTHONPYCACHEPREFIX"]) for f in fs)})
"""


@needs_sandbox
def test_without_any_bytecode_cache_the_isolated_worker_completes_and_writes_none(tmp_path):
    strat, ind = _strategy_layout(tmp_path / "plugins")
    hs = handshake(strat, (("rsi", ind),))
    prefix = tmp_path / "pycache_prefix"
    prefix.mkdir()
    # 空の pycache prefix: 全 module が cache miss になり、書けるなら書こうとする
    env = {"PYTHONPYCACHEPREFIX": str(prefix)}
    res = run_child(cfg_for(strat, hs, mid=_SNAPSHOT_PREFIX, post=_LOAD_ISOLATED + _EVAL),
                    write_bytecode=True, env=env)
    assert res.rc == 0, res.stderr
    assert "open" in res.out["actions"]
    after_prepare = res.phase("prefix")["files"]
    now = sorted(os.path.join(d, f) for d, _s, fs in os.walk(prefix) for f in fs)
    assert now == after_prepare   # prepare 以後に bytecode は 1 つも書かれていない
    assert not list((tmp_path / "plugins").rglob("__pycache__"))


@needs_sandbox
def test_writing_bytecode_under_isolation_kills_the_worker(tmp_path):
    # 対照: worker の設定を外し、親の -B も無い起動では遅延 import の mkdir で SIGSYS 死する
    strat, ind = _strategy_layout(tmp_path / "plugins")
    prefix = tmp_path / "pycache_prefix"
    prefix.mkdir()
    res = run_child(cfg_for(strat, handshake(strat, (("rsi", ind),)),
                            mid="sys.dont_write_bytecode = False\n"),
                    write_bytecode=True, env={"PYTHONPYCACHEPREFIX": str(prefix)})
    assert res.rc == -SIGSYS
    assert res.phase("isolated") is None


# --- pyc を読まない -----------------------------------------------------------------

def _plant_fake_pyc(plugin_dir: Path, fake_source: str) -> Path:
    src = plugin_dir / "plugin.py"
    st = src.stat()
    code = compile(fake_source, str(src), "exec")
    pyc = Path(importlib.util.cache_from_source(str(src)))
    pyc.parent.mkdir(exist_ok=True)
    data = bytearray(importlib.util.MAGIC_NUMBER)
    data += (0).to_bytes(4, "little")
    data += (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
    data += (st.st_size & 0xFFFFFFFF).to_bytes(4, "little")
    data += marshal.dumps(code)
    pyc.write_bytes(bytes(data))
    return pyc


@needs_sandbox
def test_planted_pyc_is_ignored_and_the_source_bytes_run(tmp_path):
    main = write_plugin(tmp_path / "p", 'MARK = "source"\n')
    _plant_fake_pyc(main, 'MARK = "pyc"\n')
    post_iso = "out['mark'] = wi.load_plugin_module(iso.main).MARK\n"
    post_plain = ("import importlib.util\n"
                  "spec = importlib.util.spec_from_file_location('plugin',"
                  " cfg['main_dir'] + '/plugin.py')\n"
                  "m = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n"
                  "out['mark'] = m.MARK\n")
    # 対照: importlib の file loader は置かれた pyc を使う
    assert run_child(cfg_for(main, isolate=False, post=post_plain)).out["mark"] == "pyc"
    assert run_child(cfg_for(main, post=post_iso)).out["mark"] == "source"


# --- module の同一性 -----------------------------------------------------------------

@needs_sandbox
def test_module_names_spec_and_loader_attributes(tmp_path):
    strat = copy_plugin("rsi_pullback", tmp_path)
    ind_a = copy_plugin("rsi_indicator", tmp_path, as_name="ind_a")
    ind_b = copy_plugin("rsi_indicator", tmp_path, as_name="ind_b")
    post = """
mods = [wi.load_plugin_module(r) for r in iso.records()[:3]]
out["mods"] = [{
    "name": m.__name__, "spec_name": m.__spec__.name, "origin": m.__spec__.origin,
    "file": m.__file__, "has_location": m.__spec__.has_location,
    "loader_none": m.__loader__ is None and m.__spec__.loader is None,
    "package": m.__package__, "cached": m.__cached__, "in_sys_modules": m.__name__ in sys.modules,
    "has_compute_or_evaluate": hasattr(m, "compute") or hasattr(m, "evaluate"),
} for m in mods]
out["distinct"] = len({id(m) for m in mods})
probe = wi.load_plugin_module(iso.indicators[2])
out["annotation_is_the_class"] = probe.f.__annotations__["x"] is int
"""
    # worker_isolation 自身の `from __future__ import annotations` を plugin へ持ち込まない
    ann = write_plugin(tmp_path / "ann", "def f(x: int):\n    return x\n")
    res = run_child(cfg_for(strat, handshake(strat, (("a", ind_a), ("b", ind_b), ("ann", ann))),
                            post=post))
    assert res.out["annotation_is_the_class"] is True
    mods = res.out["mods"]
    assert [m["name"] for m in mods] == ["plugin", "indicator_a", "indicator_b"]
    for m, d in zip(mods, (strat, ind_a, ind_b)):
        path = str(d.resolve() / "plugin.py")
        assert m == {"name": m["name"], "spec_name": m["name"], "origin": path, "file": path,
                     "has_location": True, "loader_none": True, "package": "",
                     "cached": None, "in_sys_modules": False, "has_compute_or_evaluate": True}
    assert res.out["distinct"] == 3


# --- 失敗の分類 ----------------------------------------------------------------------

_LOAD_REPORT = """
try:
    m = wi.load_plugin_module(iso.main)
    out["result"] = "loaded"
except wi.PluginLoadError as e:
    import traceback as _tb
    out["result"] = e.reason
    out["tb"] = "".join(_tb.format_exception(e))
import builtins
out["executed"] = hasattr(builtins, "AFX_TOP_LEVEL_RAN")
"""

_RAN = "import builtins\nbuiltins.AFX_TOP_LEVEL_RAN = True\n"


@needs_sandbox
def test_hash_mismatch_is_rejected_without_executing_the_plugin(tmp_path):
    main = write_plugin(tmp_path / "p", _RAN)
    res = run_child(cfg_for(main, handshake(main, content_hash="0" * 64), post=_LOAD_REPORT))
    assert (res.out["result"], res.out["executed"]) == ("content_hash_mismatch", False)


@needs_sandbox
def test_undecodable_source_is_a_plugin_error(tmp_path):
    main = write_plugin(tmp_path / "p", _RAN.encode() + b"x = '\xff\xfe'\n")
    res = run_child(cfg_for(main, post=_LOAD_REPORT))
    assert (res.out["result"], res.out["executed"]) == ("decode_failed", False)


@needs_sandbox
def test_top_level_exception_is_exec_failed_and_shows_the_executed_line(tmp_path):
    main = write_plugin(tmp_path / "p", "x = 1\nraise ValueError('top')  # AFX-TOP-LINE\n")
    res = run_child(cfg_for(main, post=_LOAD_REPORT))
    assert res.out["result"] == "exec_failed"
    assert "raise ValueError('top')  # AFX-TOP-LINE" in res.out["tb"]
    assert str(main.resolve() / "plugin.py") in res.out["tb"]


_CALL_TB = """
m = wi.load_plugin_module(iso.main)
{drop}
wait_parent()
try:
    m.f()
except ValueError:
    import traceback as _tb
    out["tb"] = _tb.format_exc()
"""


def _rewrite_in_place(path: Path, old: bytes, new: bytes) -> int:
    ino = path.stat().st_ino
    data = path.read_bytes().replace(old, new)
    with open(path, "r+b") as f:
        f.write(data)
        f.truncate()
    assert path.stat().st_ino == ino
    return ino


@needs_sandbox
@pytest.mark.parametrize("drop_linecache", [False, True])
def test_call_traceback_shows_the_executed_bytes_after_an_in_place_rewrite(tmp_path,
                                                                            drop_linecache):
    main = write_plugin(tmp_path / "p", "def f():\n    raise ValueError('x')  # AFX-MARK-A\n")
    path = main / "plugin.py"
    drop = ("import linecache\nlinecache.cache.pop(iso.main.real_dir + '/plugin.py')\n"
            if drop_linecache else "")
    res = run_child(cfg_for(main, post=_CALL_TB.format(drop=drop)),
                    interact=lambda pid, first: _rewrite_in_place(path, b"AFX-MARK-A",
                                                                  b"AFX-MARK-B") and None)
    tb = res.out["tb"]
    if drop_linecache:
        # 対照: 登録を外すと traceback はディスクを読み直し、書き換え後の行を出す
        assert "AFX-MARK-B" in tb
    else:
        assert "AFX-MARK-A" in tb and "AFX-MARK-B" not in tb


_LOAD_AFTER_PARENT = """
wait_parent()
import time as _t
t0 = _t.monotonic()
try:
    wi.load_plugin_module(iso.main)
    out["result"] = "loaded"
except wi.PluginLoadError as e:
    out["result"] = e.reason
out["elapsed"] = _t.monotonic() - t0
import builtins
out["executed"] = hasattr(builtins, "AFX_TOP_LEVEL_RAN")
"""


@needs_sandbox
def test_name_replaced_by_rename_after_the_rules_is_not_readable(tmp_path):
    main = write_plugin(tmp_path / "p", _RAN)
    path = main / "plugin.py"

    def swap(pid, first):
        tmp = main / "plugin.py.new"
        tmp.write_bytes(path.read_bytes())   # 中身は同じ。別 inode に差し替える
        os.replace(tmp, path)

    res = run_child(cfg_for(main, post=_LOAD_AFTER_PARENT), interact=swap)
    assert (res.out["result"], res.out["executed"]) == ("open_failed", False)


@needs_sandbox
def test_same_inode_growth_beyond_the_limit_is_file_too_large_quickly(tmp_path):
    main = write_plugin(tmp_path / "p", _RAN)
    path = main / "plugin.py"

    def grow(pid, first):
        with open(path, "ab") as f:
            f.write(b"#" * (wi.MAX_PLUGIN_FILE_BYTES + 100_000) + b"\n")

    res = run_child(cfg_for(main, post=_LOAD_AFTER_PARENT), interact=grow)
    assert (res.out["result"], res.out["executed"]) == ("file_too_large", False)
    assert res.out["elapsed"] < 1.0


# --- 有界 reader (隔離不要の単体) -----------------------------------------------------

def _dir_fd(d: Path) -> int:
    return os.open(d, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)


@pytest.mark.parametrize("size, expected", [(wi.MAX_PLUGIN_FILE_BYTES, None),
                                            (wi.MAX_PLUGIN_FILE_BYTES + 1, "file_too_large")])
def test_bounded_reader_accepts_the_limit_and_rejects_one_more_byte(tmp_path, size, expected):
    (tmp_path / "plugin.py").write_bytes(b"#" * size)
    fd = _dir_fd(tmp_path)
    try:
        if expected is None:
            assert len(wi.read_plugin_file_bounded(fd, "plugin.py")) == size
        else:
            with pytest.raises(wi.PluginLoadError) as ei:
                wi.read_plugin_file_bounded(fd, "plugin.py")
            assert ei.value.reason == expected
    finally:
        os.close(fd)


def test_bounded_reader_rejects_symlink_fifo_and_missing(tmp_path):
    (tmp_path / "real.py").write_text("x = 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    os.mkfifo(tmp_path / "fifo.py")
    fd = _dir_fd(tmp_path)
    try:
        for name, reason in (("link.py", "not_regular_file"), ("fifo.py", "not_regular_file"),
                             ("missing.py", "open_failed")):
            with pytest.raises(wi.PluginLoadError) as ei:
                wi.read_plugin_file_bounded(fd, name)
            assert ei.value.reason == reason, name
    finally:
        os.close(fd)


def test_bounded_reader_stops_reading_at_the_limit_plus_one(tmp_path, monkeypatch):
    # 上限を大きく超えるファイルでも、読む量は上限 + 1 で止まる (fstat の size を使わない)
    (tmp_path / "plugin.py").write_bytes(b"#" * (wi.MAX_PLUGIN_FILE_BYTES * 4))
    fd = _dir_fd(tmp_path)
    reads = []
    real_read = os.read

    def counting_read(f, n):
        b = real_read(f, n)
        reads.append(len(b))
        return b

    monkeypatch.setattr(os, "read", counting_read)
    try:
        with pytest.raises(wi.PluginLoadError):
            wi.read_plugin_file_bounded(fd, "plugin.py")
    finally:
        monkeypatch.undo()
        os.close(fd)
    assert sum(reads) == wi.MAX_PLUGIN_FILE_BYTES + 1


def test_content_hash_at_matches_the_parent_content_hash(tmp_path):
    d = copy_plugin("rsi_pullback", tmp_path)
    fd = _dir_fd(d)
    try:
        assert wi.content_hash_at(fd) == loader.content_hash(d)
        assert wi.content_hash_at(fd) == version_store.content_hash_bytes(
            (d / "plugin.py").read_bytes(), (d / "config.yaml").read_bytes())
    finally:
        os.close(fd)


# --- Landlock: 選んだ 2 ファイルだけ読める ----------------------------------------------

_FS_PROBE = """
def probe(path, listing=False):
    try:
        if listing:
            os.listdir(path)
        else:
            os.close(os.open(path, os.O_RDONLY | os.O_CLOEXEC))
        return "ok"
    except OSError as e:
        return e.errno
out["files"] = {p: probe(p) for p in cfg["probe_files"]}
out["dirs"] = {p: probe(p, True) for p in cfg["probe_dirs"]}
"""


def _fs_probe_targets(selected: list[Path], sibling: Path) -> tuple[dict, dict]:
    files, dirs = {}, {}
    for d in selected:
        for name in ("plugin.py", "config.yaml"):
            files[str(d.resolve() / name)] = "ok"
        for name in ("test_plugin.py", "extra_payload.bin", "__pycache__/plugin.cpython-313.pyc"):
            files[str(d.resolve() / name)] = 13
        dirs[str(d.resolve())] = 13
    files[str(sibling.resolve() / "plugin.py")] = 13
    dirs[str(sibling.resolve())] = 13
    from tests.conftest import REAL_HOME
    home = REAL_HOME.resolve()
    for p in (home, home / ".config", REPO, REPO / "src", REPO / "docs", Path("/tmp"),
              Path("/etc"), REPO / "data", REPO / "config", REPO / "logs"):
        if p.is_dir():
            dirs[str(p)] = 13
    for p in (Path("/etc/passwd"), REPO / "pyproject.toml", Path("/proc/self/environ")):
        if p.exists():
            files[str(p)] = 13
    return files, dirs


def _prepare_extras(d: Path) -> None:
    (d / "extra_payload.bin").write_bytes(b"payload")
    (d / "test_plugin.py").write_text("x = 1\n")
    _plant_fake_pyc(d, "x = 2\n")


@needs_sandbox
@pytest.mark.parametrize("layout", ["strategy_with_indicator", "indicator_alone"])
def test_only_the_selected_plugin_files_are_readable(tmp_path, layout):
    root = tmp_path / "plugins"
    sibling = copy_plugin("sma", root)
    if layout == "strategy_with_indicator":
        strat, ind = _strategy_layout(root)
        selected, hs, main = [strat, ind], handshake(strat, (("rsi", ind),)), strat
    else:
        main = copy_plugin("rsi", root)
        selected, hs = [main], handshake(main)
    for d in selected:
        _prepare_extras(d)
    files, dirs = _fs_probe_targets(selected, sibling)
    res = run_child(cfg_for(main, hs, post=_FS_PROBE, probe_files=list(files),
                            probe_dirs=list(dirs)))
    assert res.out["files"] == files
    assert res.out["dirs"] == dirs


@needs_sandbox
def test_read_dir_rule_on_the_plugin_dir_would_expose_the_extra_payload(tmp_path):
    # 対照: plugin dir に READ_DIR (と READ_FILE) を戻すと、追加 payload と一覧が読めてしまう
    main = copy_plugin("rsi", tmp_path / "plugins")
    _prepare_extras(main)
    mid = """
from pathlib import Path
from agentic_fx.core import landlock
_orig = landlock.build_allowlist
def _with_dir_rule(*a, **k):
    allow = _orig(*a, **k)
    for real, fd in allow.plugin_dir_fds.items():
        allow.rules.append(landlock.Rule("plugin", Path(real), fd, landlock._READ_ONLY_ACCESS))
    return allow
landlock.build_allowlist = _with_dir_rule
"""
    d = str(main.resolve())
    res = run_child(cfg_for(main, mid=mid, post=_FS_PROBE,
                            probe_files=[d + "/extra_payload.bin"], probe_dirs=[d]))
    assert res.out["files"] == {d + "/extra_payload.bin": "ok"}
    assert res.out["dirs"] == {d: "ok"}
