"""loader.py の discover / content_hash テスト (プラン 7 Task 1)。

フィクスチャは tmp_path 上にフラットな plugin フォルダ (plugin.py /
config.yaml / test_plugin.py) を生成する。実 I/O は tmp_path のみ、実
sleep/HTTP/git/乱数/実時計は使わない。
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import pytest

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES
from agentic_fx.plugin import loader as loader_module
from agentic_fx.plugin.loader import PluginMeta, content_hash, discover

INDICATOR_PY = """
def compute(df, params):
    return {"rsi_14": 50.0}
"""

INDICATOR_CONFIG = """
kind: indicator
"""

SIGNAL_PY = """
def detect(df, params):
    return []
"""

SIGNAL_CONFIG = """
kind: signal
timeframe: 1h
pairs: [USDJPY]
"""

STRATEGY_PY = """
def evaluate(df, indicators, signals, params):
    return {"action": "hold", "rationale": "test"}
"""

STRATEGY_CONFIG = """
kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: levels
max_bars: 200
"""

TEST_PY = """
def test_placeholder():
    pass
"""


def _write_plugin(base: Path, folder_name: str, *, plugin_py: str,
                  config_yaml: str, test_py: str | None = TEST_PY,
                  plugin_filename: str = "plugin.py",
                  config_filename: str = "config.yaml",
                  test_filename: str = "test_plugin.py") -> Path:
    d = base / folder_name
    d.mkdir()
    (d / plugin_filename).write_text(plugin_py)
    (d / config_filename).write_text(config_yaml)
    if test_py is not None:
        (d / test_filename).write_text(test_py)
    return d


# ① content_hash の手計算照合 -------------------------------------------

def test_content_hash_matches_manual_recomputation(tmp_path):
    d = _write_plugin(tmp_path, "sma_cross", plugin_py=STRATEGY_PY,
                      config_yaml=STRATEGY_CONFIG)
    got = content_hash(d)

    plugin_bytes = (d / "plugin.py").read_bytes()
    config_bytes = (d / "config.yaml").read_bytes()
    expected = hashlib.sha256(
        b"plugin.py\0" + plugin_bytes + b"\0config.yaml\0" + config_bytes
    ).hexdigest()

    assert got == expected


# ② 1 バイト変更でハッシュ変化 -------------------------------------------

def test_content_hash_changes_on_one_byte_change(tmp_path):
    d = _write_plugin(tmp_path, "sma_cross", plugin_py=STRATEGY_PY,
                      config_yaml=STRATEGY_CONFIG)
    before = content_hash(d)

    (d / "plugin.py").write_text(STRATEGY_PY + " ")  # 1 バイト (空白) 追加
    after = content_hash(d)

    assert before != after


def test_content_hash_ignores_test_plugin_py(tmp_path):
    """test_plugin.py は content_hash の対象外 (Task 6 の別関心)。"""
    d = _write_plugin(tmp_path, "sma_cross", plugin_py=STRATEGY_PY,
                      config_yaml=STRATEGY_CONFIG)
    before = content_hash(d)
    (d / "test_plugin.py").write_text("changed")
    after = content_hash(d)
    assert before == after


# ③ 正常 plugin → PluginMeta ---------------------------------------------

def test_discover_normal_strategy_plugin(tmp_path):
    _write_plugin(tmp_path, "sma_cross", plugin_py=STRATEGY_PY,
                  config_yaml=STRATEGY_CONFIG)

    metas = discover(tmp_path)

    assert len(metas) == 1
    meta = metas[0]
    assert isinstance(meta, PluginMeta)
    assert meta.name == "sma_cross"
    assert meta.kind == "strategy"
    assert meta.pairs == ("USDJPY",)
    assert meta.timeframe == "1h"
    assert meta.max_bars == 200
    assert meta.params == {}
    assert meta.content_hash == content_hash(tmp_path / "sma_cross")
    # exit_mode は検証されるが PluginMeta には保持されない
    assert not hasattr(meta, "exit_mode")


def test_discover_normal_indicator_plugin_defaults(tmp_path):
    _write_plugin(tmp_path, "rsi_indicator", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)

    metas = discover(tmp_path)

    assert len(metas) == 1
    meta = metas[0]
    assert meta.kind == "indicator"
    assert meta.timeframe is None
    assert meta.pairs == ()
    assert meta.max_bars == 200  # 既定値


def test_discover_normal_signal_plugin(tmp_path):
    _write_plugin(tmp_path, "rsi_signal", plugin_py=SIGNAL_PY,
                  config_yaml=SIGNAL_CONFIG)

    metas = discover(tmp_path)

    assert len(metas) == 1
    assert metas[0].kind == "signal"
    assert metas[0].pairs == ("USDJPY",)


# ④ exit_mode: evaluate reject -------------------------------------------

def test_discover_rejects_exit_mode_evaluate(tmp_path, caplog):
    _write_plugin(tmp_path, "bad_strategy", plugin_py=STRATEGY_PY,
                  config_yaml="""
kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: evaluate
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []
    assert "bad_strategy" in caplog.text


def test_discover_rejects_strategy_missing_exit_mode(tmp_path, caplog):
    _write_plugin(tmp_path, "no_exit_mode", plugin_py=STRATEGY_PY,
                  config_yaml="""
kind: strategy
timeframe: 1h
pairs: [USDJPY]
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


# ⑤ timeframe: D5 列挙制 (15m 受理・5m reject) -----------------------------

def test_discover_accepts_15m_timeframe(tmp_path):
    assert "15m" in PLUGIN_TIMEFRAMES
    _write_plugin(tmp_path, "fast_signal", plugin_py=SIGNAL_PY,
                  config_yaml="""
kind: signal
timeframe: 15m
pairs: [USDJPY]
""")
    metas = discover(tmp_path)
    assert len(metas) == 1
    assert metas[0].timeframe == "15m"


def test_discover_rejects_5m_timeframe(tmp_path, caplog):
    assert "5m" not in PLUGIN_TIMEFRAMES
    _write_plugin(tmp_path, "too_fast_signal", plugin_py=SIGNAL_PY,
                  config_yaml="""
kind: signal
timeframe: 5m
pairs: [USDJPY]
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []
    assert "too_fast_signal" in caplog.text


# ⑥ kind 列挙外 / evaluate 欠落 / pairs 空 / max_bars 0 → reject ----------

def test_discover_rejects_unknown_kind(tmp_path, caplog):
    _write_plugin(tmp_path, "unknown_kind", plugin_py=STRATEGY_PY,
                  config_yaml="""
kind: predictor
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_missing_target_function(tmp_path, caplog):
    """kind=strategy だが evaluate(df, indicators, signals, params) が無い。"""
    _write_plugin(tmp_path, "no_evaluate", plugin_py="""
def compute(df, params):
    return {}
""", config_yaml=STRATEGY_CONFIG)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []
    assert "no_evaluate" in caplog.text


def test_discover_rejects_nested_function_not_top_level(tmp_path, caplog):
    """関数の内側にネストされた同名・同引数の関数はモジュール属性として
    呼び出せない (`module.evaluate` は存在しない) — トップレベル関数のみ
    を AST 検証の対象にする (fail-open 防止。引数名は本物と完全一致させ、
    「ネストだから弾かれた」ことだけを見るテストにする)。"""
    _write_plugin(tmp_path, "nested_evaluate", plugin_py="""
def _factory():
    def evaluate(df, indicators, signals, params):
        return {"action": "hold", "rationale": "x"}
    return evaluate
""", config_yaml=STRATEGY_CONFIG)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_isolates_non_utf8_plugin_py(tmp_path, caplog):
    """plugin.py が UTF-8 でデコードできない場合もそのフォルダのみ
    reject し、他フォルダの discover は継続する (フォルダ単位の隔離)。"""
    bad = tmp_path / "bad_encoding"
    bad.mkdir()
    (bad / "plugin.py").write_bytes(b"\xff\xfe garbage not utf8")
    (bad / "config.yaml").write_text(STRATEGY_CONFIG)
    (bad / "test_plugin.py").write_text(TEST_PY)

    _write_plugin(tmp_path, "good_indicator", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)

    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)

    assert len(metas) == 1
    assert metas[0].name == "good_indicator"


def test_discover_rejects_wrong_arg_names(tmp_path, caplog):
    """関数名は一致するが引数名が違う (exact arg-name match)。"""
    _write_plugin(tmp_path, "wrong_args", plugin_py="""
def evaluate(dataframe, indicators, signals, params):
    return {"action": "hold", "rationale": "x"}
""", config_yaml=STRATEGY_CONFIG)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_empty_pairs(tmp_path, caplog):
    _write_plugin(tmp_path, "empty_pairs", plugin_py=SIGNAL_PY,
                  config_yaml="""
kind: signal
timeframe: 1h
pairs: []
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_missing_pairs_for_signal(tmp_path, caplog):
    _write_plugin(tmp_path, "missing_pairs", plugin_py=SIGNAL_PY,
                  config_yaml="""
kind: signal
timeframe: 1h
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_max_bars_zero(tmp_path, caplog):
    _write_plugin(tmp_path, "zero_max_bars", plugin_py=STRATEGY_PY,
                  config_yaml="""
kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: levels
max_bars: 0
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_non_int_max_bars(tmp_path, caplog):
    _write_plugin(tmp_path, "float_max_bars", plugin_py=STRATEGY_PY,
                  config_yaml="""
kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: levels
max_bars: 200.5
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_bool_max_bars(tmp_path, caplog):
    _write_plugin(tmp_path, "bool_max_bars", plugin_py=STRATEGY_PY,
                  config_yaml="""
kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: levels
max_bars: true
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_unknown_config_keys(tmp_path, caplog):
    _write_plugin(tmp_path, "unknown_key", plugin_py=INDICATOR_PY,
                  config_yaml="""
kind: indicator
mystery: 1
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


# ⑦ 3 ファイル欠けは skip -------------------------------------------------

def test_discover_skips_folder_missing_test_file(tmp_path, caplog):
    _write_plugin(tmp_path, "no_test_file", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG, test_py=None)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []
    assert "no_test_file" in caplog.text


def test_discover_skips_folder_missing_config(tmp_path, caplog):
    d = tmp_path / "no_config"
    d.mkdir()
    (d / "plugin.py").write_text(INDICATOR_PY)
    (d / "test_plugin.py").write_text(TEST_PY)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_skips_folder_missing_plugin_py(tmp_path, caplog):
    d = tmp_path / "no_plugin_py"
    d.mkdir()
    (d / "config.yaml").write_text(INDICATOR_CONFIG)
    (d / "test_plugin.py").write_text(TEST_PY)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_ignores_non_directory_entries(tmp_path):
    (tmp_path / "README.txt").write_text("not a plugin")
    _write_plugin(tmp_path, "rsi_indicator", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)
    metas = discover(tmp_path)
    assert len(metas) == 1
    assert metas[0].name == "rsi_indicator"


def test_discover_rejects_invalid_yaml(tmp_path, caplog):
    _write_plugin(tmp_path, "bad_yaml", plugin_py=INDICATOR_PY,
                  config_yaml="kind: [unterminated")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_exit_mode_for_non_strategy_kind(tmp_path, caplog):
    """exit_mode は strategy 専用キー — indicator/signal での指定は fail closed。"""
    _write_plugin(tmp_path, "indicator_with_exit_mode", plugin_py=INDICATOR_PY,
                  config_yaml="""
kind: indicator
exit_mode: levels
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_indicator_accepts_valid_optional_timeframe(tmp_path):
    """indicator は timeframe を省略できるが、指定するなら列挙制に従う。"""
    _write_plugin(tmp_path, "tf_indicator", plugin_py=INDICATOR_PY,
                  config_yaml="""
kind: indicator
timeframe: 4h
""")
    metas = discover(tmp_path)
    assert len(metas) == 1
    assert metas[0].timeframe == "4h"


def test_discover_indicator_rejects_invalid_optional_timeframe(tmp_path, caplog):
    _write_plugin(tmp_path, "bad_tf_indicator", plugin_py=INDICATOR_PY,
                  config_yaml="""
kind: indicator
timeframe: 5m
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_multiple_plugins_independent_rejection(tmp_path):
    """1 フォルダの reject は他フォルダに波及しない。"""
    _write_plugin(tmp_path, "good_indicator", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)
    _write_plugin(tmp_path, "bad_kind", plugin_py=INDICATOR_PY,
                  config_yaml="kind: nonsense")

    metas = discover(tmp_path)

    assert len(metas) == 1
    assert metas[0].name == "good_indicator"


# --- レビュー fix round 1 (codex 指摘 C1-C10) -----------------------------

# C1: YAML 重複キーを reject -------------------------------------------

def test_discover_rejects_duplicate_yaml_keys(tmp_path, caplog):
    """`kind` を 2 回書いた config は reject される (last-wins の無言受理は
    人間レビューと discovery の見え方を食い違わせる承認迂回リスク)。"""
    _write_plugin(tmp_path, "dup_key", plugin_py=INDICATOR_PY, config_yaml="""
kind: strategy
kind: indicator
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []
    assert "dup_key" in caplog.text


# C2: デフォルト無し keyword-only 引数は fail-open --------------------------

def test_discover_rejects_required_kwonly_arg(tmp_path, caplog):
    """`evaluate(df, indicators, signals, params, *, secret)` はハーネスの
    位置引数のみの呼び出しで TypeError になる — reject。"""
    _write_plugin(tmp_path, "kwonly_required", plugin_py="""
def evaluate(df, indicators, signals, params, *, secret):
    return {"action": "hold", "rationale": "x"}
""", config_yaml=STRATEGY_CONFIG)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_accepts_kwonly_arg_with_default(tmp_path):
    """デフォルト付き kwonly は位置引数のみの呼び出しでも成立するので許容。"""
    _write_plugin(tmp_path, "kwonly_default", plugin_py="""
def evaluate(df, indicators, signals, params, *, mode="default"):
    return {"action": "hold", "rationale": "x"}
""", config_yaml=STRATEGY_CONFIG)
    metas = discover(tmp_path)
    assert len(metas) == 1


# C3: デコレータ付き契約関数は fail-open ------------------------------------

def test_discover_rejects_decorated_target_function(tmp_path, caplog):
    """デコレータはモジュール属性を非 callable な別物に差し替え得る —
    契約関数へのデコレータは一切許容しない。"""
    _write_plugin(tmp_path, "decorated_compute", plugin_py="""
def _noop(fn):
    return fn


@_noop
def compute(df, params):
    return {"rsi_14": 50.0}
""", config_yaml=INDICATOR_CONFIG)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


# C6: pairs の空文字列・空白文字列は reject ---------------------------------

def test_discover_rejects_blank_pair_entry(tmp_path, caplog):
    _write_plugin(tmp_path, "blank_pair", plugin_py=SIGNAL_PY, config_yaml="""
kind: signal
timeframe: 1h
pairs: ["   "]
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_rejects_empty_string_pair_entry(tmp_path, caplog):
    _write_plugin(tmp_path, "empty_string_pair", plugin_py=SIGNAL_PY,
                  config_yaml="""
kind: signal
timeframe: 1h
pairs: [""]
""")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


# C7: discover が読むファイルのサイズ上限 -----------------------------------

def test_discover_rejects_oversized_config(tmp_path, caplog, monkeypatch):
    """上限定数を monkeypatch して小さくし、テストを高速に保つ。"""
    monkeypatch.setattr(loader_module, "_MAX_FILE_BYTES", 32)
    oversized_config = INDICATOR_CONFIG + ("# padding" * 10)
    assert len(oversized_config.encode()) > 32
    _write_plugin(tmp_path, "oversized_config", plugin_py=INDICATOR_PY,
                  config_yaml=oversized_config)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []
    assert "oversized_config" in caplog.text


def test_discover_rejects_oversized_plugin_py(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(loader_module, "_MAX_FILE_BYTES", 32)
    oversized_py = INDICATOR_PY + ("\n# padding" * 10)
    assert len(oversized_py.encode()) > 32
    _write_plugin(tmp_path, "oversized_plugin_py", plugin_py=oversized_py,
                  config_yaml=INDICATOR_CONFIG)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []


def test_discover_accepts_file_within_size_limit(tmp_path, monkeypatch):
    """上限を大きく下げても、既定サイズのサンプルはそのまま通ることのピン。"""
    monkeypatch.setattr(loader_module, "_MAX_FILE_BYTES", 4096)
    _write_plugin(tmp_path, "small_indicator", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)
    metas = discover(tmp_path)
    assert len(metas) == 1


# C10: フォルダ単位隔離の完全化 (config 読み/content_hash の OSError) --------

def test_discover_isolates_unreadable_config_from_other_folders(tmp_path, caplog):
    """config.yaml の読み込みで OSError (権限拒否) が出ても、そのフォルダ
    だけ reject され、他フォルダの discovery は継続する。

    root 実行では chmod によるパーミッション拒否が効かないため skip する。
    """
    if os.geteuid() == 0:
        pytest.skip("root では chmod によるパーミッション拒否を再現できない")

    unreadable = _write_plugin(tmp_path, "unreadable_config",
                               plugin_py=INDICATOR_PY,
                               config_yaml=INDICATOR_CONFIG)
    config_path = unreadable / "config.yaml"
    os.chmod(config_path, 0)
    try:
        _write_plugin(tmp_path, "good_indicator", plugin_py=INDICATOR_PY,
                      config_yaml=INDICATOR_CONFIG)
        with caplog.at_level(logging.WARNING):
            metas = discover(tmp_path)
    finally:
        os.chmod(config_path, 0o644)

    assert len(metas) == 1
    assert metas[0].name == "good_indicator"
    assert "unreadable_config" in caplog.text


def test_discover_isolates_content_hash_oserror_from_other_folders(
        tmp_path, caplog, monkeypatch):
    """content_hash 算出時の OSError も discover 全体を落とさず、そのフォ
    ルダのみ reject する。

    content_hash は config/plugin.py 検証・AST 検証が両方通った**後**にし
    か呼ばれないため、ファイルシステム操作 (chmod 等) だけでこの経路を
    単独で再現するのは難しい (plugin.py を読めなくすると先に AST 検証側で
    reject されてしまう)。`content_hash` をフォルダ名で分岐する monkeypatch
    スタブに差し替え、`discover` の `except OSError` ラッパーそのものの
    契約 (どのフォルダで OSError が出ても隔離される) を直接検証する。
    """
    real_content_hash = loader_module.content_hash

    def _flaky_content_hash(plugin_dir):
        if plugin_dir.name == "hash_error":
            raise OSError("simulated I/O failure during content_hash")
        return real_content_hash(plugin_dir)

    monkeypatch.setattr(loader_module, "content_hash", _flaky_content_hash)

    _write_plugin(tmp_path, "hash_error", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)
    _write_plugin(tmp_path, "good_indicator", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)

    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)

    assert len(metas) == 1
    assert metas[0].name == "good_indicator"
    assert "hash_error" in caplog.text


# --- 最終レビュー F3 (codex Critical): 同梱 conftest.py の reject --------


def test_discover_rejects_bundled_conftest_py(tmp_path, caplog):
    """plugin フォルダ直下に `conftest.py` を同梱すると、submit 時の
    pytest がこれを AST 検査もハッシュ照合も受けずに自動ロードしてしまう
    (pytest の仕様: test_plugin.py と同じディレクトリの conftest.py は
    明示 import なしに読み込まれる)。discover の段階で reject し、
    approval フロー (承認バックテスト) にすら乗せない。"""
    d = _write_plugin(tmp_path, "with_conftest", plugin_py=INDICATOR_PY,
                      config_yaml=INDICATOR_CONFIG)
    (d / "conftest.py").write_text("import os\n")
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert metas == []
    assert "with_conftest" in caplog.text
    assert "conftest.py" in caplog.text


def test_discover_conftest_rejection_isolated_to_its_folder(tmp_path, caplog):
    """他フォルダの discovery を道連れにしない (loader.py の既存規律 —
    フォルダ単位の fail closed)。"""
    d = _write_plugin(tmp_path, "with_conftest", plugin_py=INDICATOR_PY,
                      config_yaml=INDICATOR_CONFIG)
    (d / "conftest.py").write_text("import os\n")
    _write_plugin(tmp_path, "good_indicator", plugin_py=INDICATOR_PY,
                  config_yaml=INDICATOR_CONFIG)
    with caplog.at_level(logging.WARNING):
        metas = discover(tmp_path)
    assert len(metas) == 1
    assert metas[0].name == "good_indicator"


def test_discover_ignores_pycache_directory(tmp_path):
    """`__pycache__` はディレクトリであり `.py` ファイルではないため、
    reject 対象にならない (規定 3 ファイル以外の**ファイル**判定は直下の
    ファイルのみを見る — サブディレクトリは走査しない)。"""
    d = _write_plugin(tmp_path, "with_pycache", plugin_py=INDICATOR_PY,
                      config_yaml=INDICATOR_CONFIG)
    pycache = d / "__pycache__"
    pycache.mkdir()
    (pycache / "plugin.cpython-313.pyc").write_bytes(b"\x00")
    metas = discover(tmp_path)
    assert len(metas) == 1
    assert metas[0].name == "with_pycache"


def test_discover_sample_plugins_directory_not_rejected():
    """既存サンプル (docs/examples/plugins/*) は 3 ファイル構成 (+ 実行時
    生成される __pycache__) のみで、この reject の影響を受けないこと。"""
    samples_dir = (Path(__file__).resolve().parents[2] / "docs" / "examples"
                  / "plugins")
    metas = discover(samples_dir)
    names = {m.name for m in metas}
    assert {"rsi_indicator", "sma_cross"} <= names


def test_discover_rejects_version_dir_with_mismatched_artifact_hash(tmp_path):
    """§2.3: discover は版ディレクトリ名 (= artifact_hash) と実計算の
    artifact_hash を照合し、不一致なら在版を拒否する (in-place 編集の検出)。
    """
    from agentic_fx.plugin import version_store

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    real_hash = version_store.artifact_hash_bytes(
        b"def compute(df, params):\n    return {}\n", b"kind: indicator\n",
        b"def test_x():\n    pass\n")
    version_dir = version_store.create_version_dir(
        plugins_dir, "sma", real_hash,
        plugin_py=b"def compute(df, params):\n    return {}\n",
        config_yaml=b"kind: indicator\n",
        test_plugin=b"def test_x():\n    pass\n", op_identity="1")
    # ディレクトリを不一致な hash 名へ rename (in-place 編集を模す)
    wrong_dir = version_dir.parent / ("f" * 64)
    version_dir.rename(wrong_dir)
    (plugins_dir / "sma").symlink_to(f".versions/sma/{'f' * 64}")

    metas = discover(plugins_dir)
    assert not any(m.name == "sma" for m in metas)


def test_discover_logs_symlink_target_not_a_directory_reason(tmp_path, caplog):
    """A9 (`stage0-bundle-B.md` Minor) 再実測: `_resolve_entity` の
    `if not version_dir.is_dir(): _reject(...)` を単純に削除しても、
    `discover` の直後の `REQUIRED_FILES` 欠落検査が同じ「discover から
    消える」という見かけの結果を返す (別経路で fail closed、§6.7 型) —
    単独再実測で SURVIVED を再確認した。この 2 つの経路は**診断メッセージ
    が異なる**ため (B1 と同じパターン: 結果でなくメッセージで区別する)、
    symlink target を「ディレクトリではない実ファイル」にして
    `_reject` の "is not a directory" という固有の理由文言が出ることを
    pin する — `is_dir()` 検査を落とす変異は `missing [...]` という別の
    警告文言に変わるため red になる。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    versions_dir = plugins_dir / ".versions" / "sma"
    versions_dir.mkdir(parents=True)
    fake_hash = "a" * 64
    # ディレクトリではなく通常ファイルを symlink target に置く
    (versions_dir / fake_hash).write_text("not a directory")
    (plugins_dir / "sma").symlink_to(f".versions/sma/{fake_hash}")

    with caplog.at_level(logging.WARNING):
        metas = discover(plugins_dir)
    assert not any(m.name == "sma" for m in metas)
    assert "is not a directory" in caplog.text, caplog.text


def test_symlink_target_regex_rejects_trailing_traversal_via_missing_anchor():
    """A10 (`stage0-bundle-B.md` Minor): `_SYMLINK_TARGET_RE_TMPL` の末尾
    `$` アンカーを落とすと、`.versions/<name>/<hash>` に続けて
    `/../..` のような追加パス要素を持つ symlink target も `.match()` を
    通ってしまう (`re.match` は先頭固定・末尾不問のため)。`discover` 経由
    の実プロセス/多層防御を介さず、正規表現そのものを直接 unit で pin
    する (単独再実測で確認した SURVIVED — `resolve()` 後の
    `artifact_hash` 照合という別の層は残るが、この字句検証の層自体は
    無検証だった)。"""
    import re

    from agentic_fx.plugin.loader import _SYMLINK_TARGET_RE_TMPL

    pattern = re.compile(_SYMLINK_TARGET_RE_TMPL.format(name=re.escape("sma")))
    valid = ".versions/sma/" + "a" * 64
    assert pattern.match(valid) is not None
    traversal = valid + "/../.."
    assert pattern.match(traversal) is None, (
        "末尾 $ アンカーが欠けており、追加のパス要素を持つ symlink "
        "target が字句検証を素通りしている")


def test_content_hash_delegates_to_version_store_content_hash_bytes(tmp_path):
    """`loader.content_hash(plugin_dir)` は `version_store.content_hash_bytes`
    の薄いラッパであり、同一 bytes に対して両 API が完全一致することを
    pin する (レビュー1周目 M2、設計書 §5.2)。"""
    from agentic_fx.plugin import loader, version_store

    plugin_dir = tmp_path / "myind"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_bytes(b"# plugin body\n")
    (plugin_dir / "config.yaml").write_bytes(b"kind: indicator\n")

    via_loader = loader.content_hash(plugin_dir)
    via_version_store = version_store.content_hash_bytes(
        (plugin_dir / "plugin.py").read_bytes(),
        (plugin_dir / "config.yaml").read_bytes())
    assert via_loader == via_version_store


def test_artifact_hash_bytes_delegates_to_version_store(tmp_path):
    """B-5 是正: loader.artifact_hash_bytes は version_store.artifact_hash_bytes
    の薄いラッパ (同一 bytes に対する両 API 一致 pin)。"""
    from agentic_fx.plugin import loader, version_store
    p, c, t = b"plugin body", b"kind: indicator\n", b"def test_x():\n    pass\n"
    assert loader.artifact_hash_bytes(p, c, t) == version_store.artifact_hash_bytes(p, c, t)
