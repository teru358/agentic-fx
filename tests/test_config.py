from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agentic_fx.config import ConfigError, Settings, load_settings

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
    assert s.datafeed.watch_symbols == []
    assert s.analysis.max_watch_symbols == 10
    assert s.analysis.max_gap_pct == 5.0


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
