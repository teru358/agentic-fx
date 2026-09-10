from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.config import ImproveToolBudgetSettings


@pytest.mark.parametrize("values", [
    {"self_test_warn_after": 4, "max_self_test_runs": 3},
    {"max_self_tests_before_backtest": 4, "max_self_test_runs": 3},
])
def test_improve_tool_budget_rejects_threshold_above_run_limit(values):
    with pytest.raises(ValueError):
        ImproveToolBudgetSettings(**values)
from pydantic import ValidationError

from agentic_fx.config import BacktestSettings, ConfigError, Settings, load_settings
from agentic_fx.core.accounting import drawdown_pct

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example"


def test_example_file_loads():
    s = load_settings(EXAMPLE)
    assert isinstance(s, Settings)
    assert s.display_timezone == "Asia/Tokyo"
    assert s.pairs == ["USDJPY"]
    assert s.risk.rr_min == 1.5
    assert s.risk.risk_per_trade_pct == 0.5
    assert s.risk.swing_risk_factor == 0.5
    assert s.risk.max_total_risk_pct == 1.5
    assert s.risk.max_leverage == 10
    assert s.risk.pair_rules["USDJPY"].sl_distance_min_pips == 5
    assert s.risk.pair_rules["USDJPY"].assumed_spread_pips == 1.0
    assert s.runner.trade.backend == "local"
    assert s.datafeed.yfinance.enabled is True
    assert s.datafeed.mt5.enabled is False


def test_backtest_settings_eval_source_defaults_to_dukascopy():
    assert BacktestSettings(
        holdout_months=1, initial_balance=1.0).eval_source == "dukascopy"


def test_backtest_settings_constructs_default_history_dataset():
    settings = BacktestSettings(holdout_months=1, initial_balance=1.0)
    assert settings.base_interval == "1m"
    assert settings.dataset().as_dict() == {
        "source": "dukascopy", "base_interval": "1m"}


@pytest.mark.parametrize("field,value", [("eval_source", "bogus"),
                                           ("base_interval", "2m")])
def test_backtest_settings_delegates_invalid_dataset_to_validation(field, value):
    values = {"holdout_months": 1, "initial_balance": 1.0, field: value}
    with pytest.raises(ValidationError):
        BacktestSettings(**values)


def test_opencode_is_improve_only_and_codex_llama_swap_is_rejected():
    """opencode を trade に通す、または codex の llama_swap 封鎖を外す変異は
    shell 境界/namespace tools 非互換を再導入するため validator で pin する。"""
    raw = load_settings(EXAMPLE).model_dump()
    raw["runner"]["trade"]["backend"] = "opencode"
    with pytest.raises(ValidationError, match="opencode"):
        Settings.model_validate(raw)


@pytest.mark.parametrize(
    ("backend", "context_limit", "valid"),
    [("opencode", 0, False), ("opencode", 65536, True), ("local", 0, True)],
)
def test_opencode_context_limit_is_required_only_for_opencode(
        backend, context_limit, valid):
    raw = load_settings(EXAMPLE).model_dump()
    raw["runner"]["improve"]["backend"] = backend
    raw["runner"]["opencode"]["context_limit"] = context_limit
    if valid:
        assert Settings.model_validate(raw).runner.opencode.context_limit == context_limit
    else:
        with pytest.raises(ValidationError, match="context_limit must be >0"):
            Settings.model_validate(raw)
    raw = load_settings(EXAMPLE).model_dump()
    raw["runner"]["codex"]["provider"] = "llama_swap"
    with pytest.raises(ValidationError, match="namespace tools"):
        Settings.model_validate(raw)


def test_pair_without_rule_rejected(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["pairs"] = ["USDJPY", "GBPUSD"]  # GBPUSD の pair_rules がない
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="GBPUSD"):
        load_settings(p)


def test_sl_min_must_be_lt_max(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["pair_rules"]["USDJPY"]["sl_distance_min_pips"] = 300
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(p)


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_settings(Path("/nonexistent/settings.yaml"))


def test_invalid_yaml_key_raises(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("pairs: [USDJPY]\nrisk: {rr_min: -1}\n")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_kill_switch_cannot_be_disabled(tmp_path):
    # 全モデルを再帰的に走査し、kill switch を無効化するフィールドが存在しないこと (構造的担保)
    from pydantic import BaseModel

    seen: set[type] = set()

    def walk(model: type[BaseModel]):
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            assert not ("kill" in name and ("enable" in name or "disable" in name)), name
            ann = field.annotation
            for t in (ann, *getattr(ann, "__args__", ())):
                if isinstance(t, type) and issubclass(t, BaseModel):
                    walk(t)

    walk(Settings)

    # 無効化キーを混入させると ConfigError (extra="forbid")
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["kill_switch_enabled"] = False
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="kill_switch_enabled"):
        load_settings(p)


# round2 #1 是正 (裁定B、2026-08-29): `drawdown_kill_pct` に上限が無いと
# `9999`/`1e9` を受理し、drawdown kill switch (CLAUDE.md 絶対制約) が
# de facto 無効化できてしまう (probe 実測)。
def test_drawdown_kill_pct_rejects_unreachable_values(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["drawdown_kill_pct"] = 9999
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(p)


def test_drawdown_kill_pct_le_100_latches_at_equity_zero(tmp_path):
    # 上限値自体が「到達可能性の保証」であることを固定する — `le=1e9` への
    # 変異では `9999` 拒否テストだけでは殺せない (equity=0 が
    # drawdown_pct の最大到達値 100.0 であることまで見る)。
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["drawdown_kill_pct"] = 100.0
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    s = load_settings(p)
    assert s.risk.drawdown_kill_pct == 100.0
    assert drawdown_pct(equity=0.0, hwm=1000.0) == 100.0
    assert drawdown_pct(equity=0.0, hwm=1000.0) >= s.risk.drawdown_kill_pct

    # round2 最終是正 A5 (2026-08-29、verified-local-round2.md A5): `le=100`
    # の**値**が未 pin だった (`le=200` への変異が生存する)。100 を 1 でも
    # 超えたら拒否されることを直接固定する。
    raw["risk"]["drawdown_kill_pct"] = 100.1
    p2 = tmp_path / "s2.yaml"
    p2.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(p2)


def test_drawdown_kill_pct_above_threshold_warns_at_startup(tmp_path, caplog):
    import logging
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["drawdown_kill_pct"] = 99.0
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with caplog.at_level(logging.WARNING, logger="agentic_fx.config"):
        load_settings(p)
    assert any("drawdown_kill_pct" in r.message for r in caplog.records)


# round2 最終是正 A4 (2026-08-29、verified-local-round2.md A4): 上のテストは
# `99.0` (= どの変異 (`>`→`>=`、しきい値 20→21/20→98) でも WARN 側に落ちる
# 値) だけを流しているため、しきい値そのものが未 pin だった。境界 2 値
# (`20.0`=WARN 無し、`20.1`=WARN 有り) を直接固定する。
@pytest.mark.parametrize("value,expect_warn", [(20.0, False), (20.1, True)])
def test_drawdown_kill_pct_warn_threshold_boundary(tmp_path, caplog, value,
                                                    expect_warn):
    import logging
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["drawdown_kill_pct"] = value
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with caplog.at_level(logging.WARNING, logger="agentic_fx.config"):
        load_settings(p)
    assert any("drawdown_kill_pct" in r.message
              for r in caplog.records) is expect_warn


def test_unknown_top_level_key_rejected(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("pairs: [USDJPY]\ntypo_key: 1\n")
    with pytest.raises(ConfigError, match="typo_key"):
        load_settings(p)


def test_paper_settings():
    s = load_settings(EXAMPLE)
    assert s.paper.starting_balance == 1_000_000


def test_account_currency_default_example():
    s = load_settings(EXAMPLE)
    assert s.account_currency == "JPY"


@pytest.mark.parametrize("value", ["JPY", "USD"])
def test_account_currency_accepts_valid_iso4217_like_codes(tmp_path, value):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["account_currency"] = value
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    s = load_settings(p)
    assert s.account_currency == value


@pytest.mark.parametrize("value", ["jpy", "JPYY", ""])
def test_account_currency_rejects_invalid_codes(tmp_path, value):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["account_currency"] = value
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(p)


def test_display_timezone_defaults_to_utc(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del raw["display_timezone"]
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    s = load_settings(p)
    assert s.display_timezone == "UTC"


def _with_datafeed(tmp_path, **overrides):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["datafeed"].update(overrides)
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def _settings_with(**datafeed_overrides) -> Settings:
    """Example を読んで Settings.model_validate() で再検証する。
    model_copy は validation をスキップするため、Settings-level の
    model_validator を検証する必要があるときはこのヘルパを使う。"""
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["datafeed"].update(datafeed_overrides)
    return Settings.model_validate(raw)


def test_intervals_defaults_from_example():
    s = load_settings(EXAMPLE)
    assert "1m" in s.datafeed.intervals
    assert set(s.datafeed.primary_intervals) <= set(s.datafeed.intervals)


def test_intervals_must_include_1m(tmp_path):
    """1m はペーパー約定判定の構造的要件なので外せない。"""
    with pytest.raises(ConfigError, match="1m"):
        load_settings(_with_datafeed(tmp_path, intervals=["1h", "4h"],
                                     primary_intervals=["1h"]))


def test_primary_intervals_must_be_subset(tmp_path):
    with pytest.raises(ConfigError, match="subset"):
        load_settings(_with_datafeed(tmp_path, intervals=["1m", "1h"],
                                     primary_intervals=["4h"]))


def test_unknown_interval_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown interval"):
        load_settings(_with_datafeed(tmp_path, intervals=["1m", "3h"],
                                     primary_intervals=["1m"]))


def test_empty_primary_intervals_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(_with_datafeed(tmp_path, primary_intervals=[]))


# --- conversion_skew_max_min (レビュー指摘 F1: 換算 skew は freshness の
# 使い回しにしない専用キー) -------------------------------------------------

def test_conversion_skew_max_min_defaults_from_example():
    s = load_settings(EXAMPLE)
    assert s.datafeed.conversion_skew_max_min > 0
    # F1: 専用キーが freshness_max_min より厳しい (小さい) こと自体が
    # 「skew 検証が freshness に埋没しない」ための前提条件。
    assert s.datafeed.conversion_skew_max_min < s.datafeed.freshness_max_min


def test_conversion_skew_max_min_missing_rejected(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del raw["datafeed"]["conversion_skew_max_min"]
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(p)


def test_conversion_skew_max_min_nonpositive_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(_with_datafeed(tmp_path, conversion_skew_max_min=0.0))


def test_conversion_skew_max_min_equal_to_freshness_rejected(tmp_path):
    """F1 の再発防止: 専用キーを freshness_max_min と同値に (誤って) 設定
    すると、skew 検証が freshness 検証に埋没して発火しなくなる (レビュー
    実測の再現条件そのもの)。起動時に弾く。"""
    with pytest.raises(ConfigError, match="conversion_skew_max_min"):
        load_settings(_with_datafeed(tmp_path, conversion_skew_max_min=20.0,
                                     freshness_max_min=20.0))


def test_conversion_skew_max_min_larger_than_freshness_rejected(tmp_path):
    with pytest.raises(ConfigError, match="conversion_skew_max_min"):
        load_settings(_with_datafeed(tmp_path, conversion_skew_max_min=30.0,
                                     freshness_max_min=20.0))


def test_empty_pairs_rejected(tmp_path):
    """空の pairs を設定検証で弾く。

    init / trade_loop の fail closed は `settings.pairs[0]` を確認対象に
    するため、ここが通ると IndexError で init が例外死する。検証で弾かれる
    ことをテストで固定しておく (min_length=1 が消えたら落ちる)。
    """
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["pairs"] = []
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="pairs"):
        load_settings(p)


def test_invalid_display_timezone_rejected(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["display_timezone"] = "Not/A_Real_Zone"
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="not a known IANA timezone"):
        load_settings(p)


def test_plugin_signal_queue_settings_defaults_from_example():
    """プラン 7 Task 7: settings.yaml.example の値と PluginSettings 既定値の
    乖離を検出する (キー有無は extra="forbid" で検出できるが、値のドリフト
    は別に assert しないと見逃す)。"""
    s = load_settings(EXAMPLE)
    assert s.plugin.signal_requeue_max == 2
    assert s.plugin.signal_lease_min == 15
    assert s.plugin.signal_freshness_bars == 2
    # プラン 7 Task 8 追加分
    assert s.plugin.signal_min_interval_min == 10
    assert s.plugin.signal_daily_max == 12


def test_backtest_and_analysis_defaults():
    s = load_settings(EXAMPLE)
    assert s.backtest.holdout_months == 3
    assert s.backtest.initial_balance > 0
    assert s.backtest.eval_source == "dukascopy"
    assert s.datafeed.watch_symbols == []
    assert s.analysis.max_watch_symbols == 10
    assert s.analysis.max_gap_pct == 5.0


def test_backtest_eval_source_accepts_import_source():
    raw = load_settings(EXAMPLE).model_dump()
    raw["backtest"]["eval_source"] = "mt5"

    assert Settings.model_validate(raw).backtest.eval_source == "mt5"


@pytest.mark.parametrize("source", ["yfinance", "unknown"])
def test_backtest_eval_source_rejects_non_import_source(source):
    raw = load_settings(EXAMPLE).model_dump()
    raw["backtest"]["eval_source"] = source

    with pytest.raises(ValidationError, match=r"dukascopy.*mt5"):
        Settings.model_validate(raw)


def test_watch_symbols_never_extend_pairs():
    """watch は取引対象ではない — pair enum / risk 検証との独立を実 assert でピン。"""
    # watch_symbols を足した settings でも:
    s = _settings_with(watch_symbols=["XAUUSD"])
    # ① market_tools の pair enum は settings.pairs のみ (watch が混入しない)
    from agentic_fx.tools import market_tools
    from agentic_fx.tools.registry import ToolRegistry
    reg = ToolRegistry()
    reg.register_all(market_tools.build(MagicMock(), MagicMock(), s))
    schema = [t for t in reg.openai_tools(["get_ohlcv"])][0]
    enum = schema["function"]["parameters"]["properties"]["pair"]["enum"]
    assert enum == list(s.pairs) and "XAUUSD" not in enum
    # ② watch_symbols は risk.pair_rules の検証対象外 (load が通ること自体が証明)


def test_watch_symbols_capped_by_max():
    """選定基準⑤: 上限は設定バリデータで機械的に強制 (レビュー裁定)。"""
    with pytest.raises(ValidationError):
        _settings_with(watch_symbols=[f"SYM{i}" for i in range(11)])  # 11 > 10


def test_cache_retention_days_defaults_to_30():
    s = load_settings(EXAMPLE)
    assert s.datafeed.cache_retention_days == 30


def test_cache_retention_days_must_be_positive(tmp_path):
    with pytest.raises(ConfigError, match="cache_retention_days"):
        load_settings(_with_datafeed(tmp_path, cache_retention_days=0))


def test_reflection_and_alert_defaults_from_example():
    s = load_settings(EXAMPLE)
    assert s.reflection.max_attempts == 2
    assert s.alert.consecutive_gate_reject == 10


def test_reflection_max_attempts_must_be_at_least_one():
    """Task 15: `ge=1` を落とすと `max_attempts: 0` が通り、抽出条件
    `a.attempts < 0` により **どの order も一度も振り返られなくなる**
    (無効化キーが増える)。設計書 D1 は上限の下限を 1 に固定する。"""
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["reflection"] = {"max_attempts": 0}
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_alert_consecutive_gate_reject_must_be_at_least_one():
    """Task 17: `ge=1` を落とすと `consecutive_gate_reject: 0` が通り、
    却下が 1 本も無くても閾値を満たしてしまう (`count < 0` が常に偽) ため
    **区間ごとに必ず 1 通の誤通知**が出る。`reflection.max_attempts` 側と
    対称に下限を pin する (レビュー 1 周目 ローカル LLM)。"""
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["alert"] = {"consecutive_gate_reject": 0}
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


@pytest.mark.parametrize("section,key", [
    ("reflection", "max_attempts"), ("alert", "consecutive_gate_reject")])
def test_nested_unknown_key_rejected(section, key):
    """`ReflectionSettings` / `AlertSettings` の `_Strict` 継承の pin
    (レビュー 1 周目 ローカル LLM)。`test_unknown_top_level_key_rejected` は
    トップレベルしか見ないため、これらが `_Strict` を外しても生き残る。
    外れると **設定キーの打ち間違いが黙って既定値で動く**。"""
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw[section] = {key: 2, "typo_key": 1}
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_every_nested_settings_type_forbids_unknown_keys():
    """入れ子設定型の `_Strict` 継承の pin (レビュー 2 周目 ローカル LLM)。
    `test_nested_unknown_key_rejected` は reflection / alert の 2 セクション
    しか見ないため、**それ以外の設定型が `_Strict` を外しても** 全テストが
    緑のまま通る (実測: `DatafeedSettings` / `ScheduleSettings` はフルスイート
    2066 passed のまま生存)。外れると **設定キーの打ち間違いが黙って既定値で
    動く** — 打ち間違えた側は「設定した」と思い込む。"""
    import inspect

    from pydantic import BaseModel

    from agentic_fx import config as cfg

    types = [o for _, o in inspect.getmembers(cfg, inspect.isclass)
             if issubclass(o, BaseModel) and o.__module__ == cfg.__name__]
    assert len(types) >= 10, types
    loose = [t.__name__ for t in types
             if t.model_config.get("extra") != "forbid"]
    assert loose == [], f"未知キーを拒否しない設定型: {loose}"


def test_settings_yaml_example_has_cli_runner_settings():
    s = load_settings(EXAMPLE)
    assert s.runner.claude.bin == "claude"
    assert s.runner.claude.credentials_file == "~/.claude/.credentials.json"
    assert s.runner.codex.bin
    assert s.runner.codex.provider == "chatgpt"
    assert s.runner.opencode.bin == "~/.opencode/bin/opencode"
    assert s.runner.cli_terminate_grace_sec > 0


def test_settings_yaml_example_has_improve_settings():
    s = load_settings(EXAMPLE)
    assert s.improve.parallel >= 1
    assert s.improve.mission_max_turns >= 1
    assert s.improve.mission_timeout_sec >= 60
    assert s.improve.llama_swap_verified is False
    assert s.improve.max_new_backlog_per_mission >= 1
    assert s.improve.backtest_rpc_timeout_sec > 0
    assert s.improve.accept_drain_sec == 30.0
    assert s.improve.research.max_searches >= 1


def test_settings_yaml_example_has_schedule_improve_at():
    s = load_settings(EXAMPLE)
    assert s.schedule.improve_at


# round2 #3 是正 (2026-08-29、verified-round2.md #3): `schedule.improve_at`
# に形式検証が無いと ImproveSupervisor.tick が毎tick ValueError を投げ、
# scheduler_thread の except Exception: が飲んで改善ループが恒久沈黙する。
# 裁定E (round2 最終是正、2026-08-29): `config._check_improve_at` は
# `fullmatch` で末尾改行付き入力を拒否する (M1 是正) が、その規約自体は
# 未 pin だった。"Sat 03:00\n" は `.match()` なら通ってしまう値。
@pytest.mark.parametrize("value", ["Saturday 03:00", "03:00", "Sat 03:00\n"])
def test_improve_at_is_validated_against_cadence_weekly(tmp_path, value):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["schedule"]["improve"] = "weekly"
    raw["schedule"]["improve_at"] = value
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(p)


def test_improve_at_is_validated_against_cadence_daily(tmp_path):
    # cadence と組で見ることを固定する — 片方の正規表現だけ見る変異
    # (例えば常に _WEEKLY_AT_RE を使う) を殺す。"Sat 03:00" は
    # weekly の形式であって daily としては不正。
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["schedule"]["improve"] = "daily"
    raw["schedule"]["improve_at"] = "Sat 03:00"
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(p)


def test_improve_at_valid_values_pass_for_each_cadence(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["schedule"]["improve"] = "weekly"
    raw["schedule"]["improve_at"] = "Sat 03:00"
    p1 = tmp_path / "s1.yaml"
    p1.write_text(yaml.safe_dump(raw))
    assert load_settings(p1).schedule.improve_at == "Sat 03:00"

    raw["schedule"]["improve"] = "daily"
    raw["schedule"]["improve_at"] = "03:00"
    p2 = tmp_path / "s2.yaml"
    p2.write_text(yaml.safe_dump(raw))
    assert load_settings(p2).schedule.improve_at == "03:00"


def test_runner_choice_backend_accepts_codex():
    from agentic_fx.config import RunnerChoice
    assert RunnerChoice(backend="codex", model="m").backend == "codex"


def test_runner_choice_backend_rejects_unknown_value():
    from agentic_fx.config import RunnerChoice
    with pytest.raises(ValidationError):
        RunnerChoice(backend="bogus", model="m")


def test_trade_backend_codex_is_rejected(tmp_path):
    """trade.backend=codex は起動時に拒否する (§1.4: codex は shell を
    外せない。trade worker は Landlock 無し + データ資格情報を持つ)。"""
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["runner"]["trade"]["backend"] = "codex"
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="codex"):
        load_settings(p)


def test_improve_backend_codex_is_accepted(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["runner"]["improve"]["backend"] = "codex"
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(raw))
    s = load_settings(p)
    assert s.runner.improve.backend == "codex"


def test_codex_settings_extra_forbid():
    """既存 `_Strict` 継承規約 (未知キー拒否) が新規 config クラスにも
    適用されていることを pin する。"""
    from agentic_fx.config import CodexCliSettings
    with pytest.raises(ValidationError):
        CodexCliSettings(bin="/x/codex", unknown_key=1)


def test_improve_mission_timeout_sec_minimum_is_60():
    """Step 17 M5 killer: `ImproveSettings.mission_timeout_sec` は `ge=60`
    で下限を強制する (骨格 §1.4 の既定 3600 と `ge=60` は設計書 §1.4 逐語)。"""
    from agentic_fx.config import ImproveSettings
    with pytest.raises(ValidationError):
        ImproveSettings(mission_timeout_sec=59)


def test_improve_gate_default_requires_three_self_tests():
    """個人 YAML に gate 節が無くても安全な下限を維持する。"""
    from agentic_fx.config import ImproveSettings
    assert ImproveSettings().gate.min_test_functions == 3


def test_plugin_settings_pytest_timeout_sec_default():
    """6-A Step 1: pytest_timeout_sec の既定値が 300.0 であること。"""
    from agentic_fx.config import PluginSettings
    assert PluginSettings().pytest_timeout_sec == 300.0


def test_plugin_settings_pytest_timeout_sec_overridable():
    """6-A Step 1: pytest_timeout_sec がオーバーライド可能であること。"""
    from agentic_fx.config import PluginSettings
    assert PluginSettings(pytest_timeout_sec=60.0).pytest_timeout_sec == 60.0


def test_settings_yaml_example_has_pytest_timeout_sec():
    """6-A Step 1: settings.yaml.example に pytest_timeout_sec キーが含まれ、
    値が 300.0 であること。"""
    from agentic_fx.config import load_settings
    settings = load_settings(EXAMPLE)
    assert settings.plugin.pytest_timeout_sec == 300.0
