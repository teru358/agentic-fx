from pathlib import Path

import pytest

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
