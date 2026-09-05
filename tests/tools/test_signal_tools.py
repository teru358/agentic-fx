"""get_signals ツールのテスト (プラン 7 Task 9)。

①since_hours クランプ ②strategy 行への metrics + 注記添付 ③signal 行
には両キーとも付かない ⑤bool 拒否 + pair 検証 + payload 破損 fail-open。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.store import backtest_runs, signals
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import signal_tools

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _add_signal(conn, *, hours_ago: float, kind: str = "signal",
                content_hash: str = "h1", pair: str = "USDJPY",
                timeframe: str = "1h", payload: dict | None = None):
    return signals.add(
        conn, plugin="sig1", content_hash=content_hash, pair=pair,
        timeframe=timeframe,
        bar_ts=(NOW - timedelta(hours=hours_ago)).isoformat(),
        kind=kind, payload=payload if payload is not None else {"x": 1},
        now=NOW)


def _tool(conn, settings=SETTINGS, clock=None):
    tools = signal_tools.build(conn, settings, clock or FixedClock(NOW))
    assert len(tools) == 1 and tools[0].name == "get_signals"
    return tools[0]


# ---- ①since_hours=100 クランプ ---------------------------------------------

def test_since_hours_clamped_to_configured_max(tmp_path):
    """max_lookback=24h の設定で since_hours=100 を渡しても、24h より古い
    行 (-30h) は含まれず、24h 以内の行 (-10h) だけが返る (クランプ実証)。"""
    conn = _conn(tmp_path)
    assert SETTINGS.plugin.signals_max_lookback_hours == 24  # 前提
    _add_signal(conn, hours_ago=30, content_hash="old")
    _add_signal(conn, hours_ago=10, content_hash="recent")
    tool = _tool(conn)
    out = tool.func(pair="USDJPY", since_hours=100)
    hashes = {r["content_hash"] for r in out}
    assert hashes == {"recent"}


def test_since_hours_clamp_uses_configured_max_not_hardcoded_24(tmp_path):
    """B (advisor 指摘): 他のテストは max=24 (既定値) 固定なので「設定値へ
    クランプ」と「24 決め打ち」を区別できない。max_lookback_hours=6 に
    変更した設定でも、その値へクランプされることを確認する。"""
    conn = _conn(tmp_path)
    settings6 = SETTINGS.model_copy(deep=True)
    settings6.plugin.signals_max_lookback_hours = 6
    _add_signal(conn, hours_ago=10, content_hash="old")  # 6h 超なので除外
    _add_signal(conn, hours_ago=3, content_hash="recent")  # 6h 以内
    tool = _tool(conn, settings=settings6)
    since_schema = tool.parameters["properties"]["since_hours"]
    assert since_schema["maximum"] == 6  # スキーマも設定値に追従する
    out = tool.func(pair="USDJPY", since_hours=100)
    hashes = {r["content_hash"] for r in out}
    assert hashes == {"recent"}


# ---- ②strategy 行に metrics + 注記 -------------------------------------------

def test_strategy_row_gets_in_sample_metrics_and_annotation(tmp_path):
    conn = _conn(tmp_path)
    sid = _add_signal(conn, hours_ago=1, kind="strategy",
                      content_hash="strat1")
    assert sid is not None
    backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p.py", content_hash="strat1",
        kind="strategy", pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
        period=(NOW, NOW), metrics={"trades": 40, "pf": 1.3},
        settings_hash="s", core_commit="c", initial_balance=1e6, now=NOW)
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    assert len(out) == 1
    row = out[0]
    assert row["in_sample_metrics"] == {"trades": 40, "pf": 1.3}
    assert row["note"] == "バックテスト成績は実運用成績の予測値ではない"


def test_strategy_row_uses_dataset_derived_source_and_base_interval(
        tmp_path):
    """段階 2 レビュー是正 c4b-1: `latest_in_sample_metrics` へ渡す
    `source`/`base_interval` が `settings.backtest.dataset()` 由来 (=
    settings.backtest.eval_source/base_interval) であることをピンする。
    既定 ("dukascopy"/"1m") とは異なる ("mt5"/"5m") 設定で backtest_runs 行
    を保存し、それが取れることを確認する — ハードコード ("dukascopy"/"1m"
    固定) への退行ならこの行は見つからず metrics は None になる。
    """
    conn = _conn(tmp_path)
    settings_5m = SETTINGS.model_copy(deep=True)
    settings_5m.backtest.eval_source = "mt5"
    settings_5m.backtest.base_interval = "5m"
    sid = _add_signal(conn, hours_ago=1, kind="strategy",
                      content_hash="strat_5m")
    assert sid is not None
    backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p.py", content_hash="strat_5m",
        kind="strategy", pair="USDJPY", timeframe="1h", source="mt5",
        base_interval="5m",
        period=(NOW, NOW), metrics={"trades": 40, "pf": 1.3},
        settings_hash="s", core_commit="c", initial_balance=1e6, now=NOW)
    tool = _tool(conn, settings=settings_5m)
    out = tool.func(pair="USDJPY")
    assert len(out) == 1
    assert out[0]["in_sample_metrics"] == {"trades": 40, "pf": 1.3}


def test_strategy_row_without_backtest_run_gets_none_metrics_but_note(tmp_path):
    """未計測 (backtest_runs に対応行なし) でも note は必ず付き、
    in_sample_metrics は明示的に None。"""
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=1, kind="strategy", content_hash="unmeasured")
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    assert len(out) == 1
    assert out[0]["in_sample_metrics"] is None
    assert out[0]["note"] == "バックテスト成績は実運用成績の予測値ではない"


# ---- ③signal 行には両キーとも付かない ----------------------------------------

def test_signal_row_has_no_metrics_or_note_keys(tmp_path):
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=1, kind="signal", content_hash="sig1")
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    assert len(out) == 1
    assert "in_sample_metrics" not in out[0]
    assert "note" not in out[0]


# ---- ⑤bool 拒否 --------------------------------------------------------------

def test_since_hours_bool_rejected(tmp_path):
    conn = _conn(tmp_path)
    tool = _tool(conn)
    with pytest.raises(ValueError, match="since_hours"):
        tool.func(pair="USDJPY", since_hours=True)


def test_since_hours_non_int_rejected(tmp_path):
    conn = _conn(tmp_path)
    tool = _tool(conn)
    with pytest.raises(ValueError, match="since_hours"):
        tool.func(pair="USDJPY", since_hours="24")


def test_since_hours_below_minimum_clamped_to_one(tmp_path):
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=0.5, content_hash="fresh")
    tool = _tool(conn)
    out = tool.func(pair="USDJPY", since_hours=-5)
    # クランプ後は 1h なので 0.5h 前の行は含まれる
    assert len(out) == 1


# ---- pair 検証 (settings.pairs 外) -------------------------------------------

def test_pair_not_in_settings_rejected(tmp_path):
    conn = _conn(tmp_path)
    tool = _tool(conn)
    with pytest.raises(ValueError, match="pair"):
        tool.func(pair="EURUSD")  # settings.yaml.example の pairs は [USDJPY]


# ---- payload 破損は fail-open (行 skip) --------------------------------------

def test_corrupt_payload_json_skips_row_fail_open(tmp_path):
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=1, content_hash="good")
    _add_signal(conn, hours_ago=1, content_hash="bad")
    conn.execute("UPDATE signals SET payload_json='not json' "
                "WHERE content_hash='bad'")
    conn.commit()
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    hashes = {r["content_hash"] for r in out}
    assert hashes == {"good"}


# ---- payload は decode 済み dict で返る ---------------------------------------

def test_payload_returned_as_decoded_dict(tmp_path):
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=1, content_hash="p1",
               payload={"direction": "long", "strength": 0.7})
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    assert out[0]["payload"] == {"direction": "long", "strength": 0.7}


# ---- ToolDef スキーマの minimum/maximum -------------------------------------

def test_schema_has_minimum_and_maximum(tmp_path):
    conn = _conn(tmp_path)
    tool = _tool(conn)
    since_schema = tool.parameters["properties"]["since_hours"]
    assert since_schema["minimum"] == 1
    assert since_schema["maximum"] == SETTINGS.plugin.signals_max_lookback_hours


# ---- fix round 1 F2 (Important, sonnet): 多 pair strategy の成績が pair 別に
# 正しく帰属される (content_hash はコード由来で pair 非依存のため、pair
# 絞りが無いと「最後に評価された pair」の成績が誤って付く)。

def test_strategy_metrics_scoped_by_own_pair_not_other_pair(tmp_path):
    conn = _conn(tmp_path)
    settings2 = SETTINGS.model_copy(deep=True)
    settings2.pairs = ["USDJPY", "EURUSD"]
    _add_signal(conn, hours_ago=1, kind="strategy", content_hash="multi",
               pair="USDJPY")
    _add_signal(conn, hours_ago=1, kind="strategy", content_hash="multi",
               pair="EURUSD")
    kw = dict(plugin_ref="p.py", content_hash="multi", kind="strategy",
              timeframe="1h", source="dukascopy", base_interval="1m", period=(NOW, NOW),
              settings_hash="s", core_commit="c", initial_balance=1e6, now=NOW)
    # EURUSD を後から保存する (id が大きい) — pair 絞りが無いと id 降順で
    # USDJPY 側の呼び出しにも EURUSD の成績が誤って付く。
    backtest_runs.save_harness_run(
        conn, scope="in_sample", pair="USDJPY", metrics={"pf": 1.1}, **kw)
    backtest_runs.save_harness_run(
        conn, scope="in_sample", pair="EURUSD", metrics={"pf": 2.2}, **kw)
    tool = _tool(conn, settings=settings2)
    usdjpy_out = tool.func(pair="USDJPY")
    eurusd_out = tool.func(pair="EURUSD")
    assert usdjpy_out[0]["in_sample_metrics"] == {"pf": 1.1}
    assert eurusd_out[0]["in_sample_metrics"] == {"pf": 2.2}


# ---- fix round 1 F3 (Important, sonnet+codex): status 露出 + projection ピン --

def test_signal_row_status_present_and_exact_key_set(tmp_path):
    """(a) status キーが返る。(b) projection を dict(row) にする変異
    (claimed_by_mission_id 等の内部列の全露出) を検出するため、キー集合を
    厳密に assert する。"""
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=1, kind="signal", content_hash="sig1")
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    assert out[0]["status"] == "pending"
    assert set(out[0].keys()) == {
        "id", "plugin", "content_hash", "pair", "timeframe", "bar_ts",
        "kind", "status", "payload"}


def test_strategy_row_status_present_and_exact_key_set(tmp_path):
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=1, kind="strategy", content_hash="strat1")
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    assert out[0]["status"] == "pending"
    assert set(out[0].keys()) == {
        "id", "plugin", "content_hash", "pair", "timeframe", "bar_ts",
        "kind", "status", "payload", "in_sample_metrics", "note"}


# ---- fix round 1 F4 (Important, codex): metrics_json 破損の fail-open -------

def test_malformed_backtest_metrics_json_does_not_break_other_rows(tmp_path):
    """1 件の strategy 行の backtest_runs.metrics_json が壊れていても、
    get_signals 全体が例外で落ちず、他の正常な行 (signal 行) は普通に
    返る。壊れた行自体は in_sample_metrics=None + note 付きで返る
    (fail-open)。"""
    conn = _conn(tmp_path)
    _add_signal(conn, hours_ago=1, kind="signal", content_hash="good_sig")
    _add_signal(conn, hours_ago=1, kind="strategy", content_hash="broken")
    backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="p.py", content_hash="broken",
        kind="strategy", pair="USDJPY", timeframe="1h", source="dukascopy", base_interval="1m",
        period=(NOW, NOW), metrics={"trades": 1}, settings_hash="s",
        core_commit="c", initial_balance=1e6, now=NOW)
    conn.execute("UPDATE backtest_runs SET metrics_json='not json' "
                "WHERE content_hash='broken'")
    conn.commit()
    tool = _tool(conn)
    out = tool.func(pair="USDJPY")
    by_hash = {r["content_hash"]: r for r in out}
    assert set(by_hash) == {"good_sig", "broken"}
    assert by_hash["broken"]["in_sample_metrics"] is None
    assert by_hash["broken"]["note"] == "バックテスト成績は実運用成績の予測値ではない"


# ---- Task 3: description f-string 化 -----------------------------------------

def test_get_signals_description_reflects_default_lookback(tmp_path, monkeypatch):
    """get_signals tool の description が _DEFAULT_SINCE_HOURS の実値を
    反映する (硬コードされた 24h ではなく f-string から動的に参照する)。
    リテラル値への逆変異を検出するため、_DEFAULT_SINCE_HOURS を動的に変更して
    description が追従することを検証する (codex I-3)。"""
    conn = _conn(tmp_path)

    # 元の値で動作確認
    tool_original = _tool(conn)
    assert f"{signal_tools._DEFAULT_SINCE_HOURS}h" in tool_original.description
    assert "24h" in tool_original.description  # 元の既定値

    # _DEFAULT_SINCE_HOURS を 37 に変更し、description が新しい値を反映することを確認
    monkeypatch.setattr(signal_tools, "_DEFAULT_SINCE_HOURS", 37)
    tool_mutated = _tool(conn)
    assert "37h" in tool_mutated.description, \
        f"description should contain '37h' when _DEFAULT_SINCE_HOURS=37, got: {tool_mutated.description}"
    assert "24h" not in tool_mutated.description, \
        f"description should not contain '24h' when _DEFAULT_SINCE_HOURS=37, got: {tool_mutated.description}"
