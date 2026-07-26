"""settings.yaml のロードと検証 (1 ファイル、3 分割しない — 設計書 §12)。"""
from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairRule(_Strict):
    sl_distance_min_pips: float = Field(gt=0)
    sl_distance_max_pips: float = Field(gt=0)
    assumed_spread_pips: float = Field(ge=0)

    @model_validator(mode="after")
    def _min_lt_max(self) -> "PairRule":
        if self.sl_distance_min_pips >= self.sl_distance_max_pips:
            raise ValueError("sl_distance_min_pips must be < sl_distance_max_pips")
        return self


class RiskSettings(_Strict):
    rr_min: float = Field(gt=0)
    risk_per_trade_pct: float = Field(gt=0, le=5)
    swing_risk_factor: float = Field(gt=0, le=1)
    max_positions: int = Field(ge=1)
    max_total_risk_pct: float = Field(gt=0)
    max_leverage: float = Field(gt=0)
    daily_loss_limit_pct: float = Field(gt=0)
    drawdown_kill_pct: float = Field(gt=0)
    limit_deviation_pct: float = Field(gt=0)
    limit_expiry_max_h: float = Field(gt=0, le=24)
    max_slippage_pct: float = Field(gt=0)
    friday_swing_cutoff_utc: str = "18:00"
    commission_per_lot: float = Field(ge=0)
    pair_rules: dict[str, PairRule]


class RunnerChoice(_Strict):
    backend: str = Field(pattern="^(local|claude)$")
    model: str


class RunnerSettings(_Strict):
    trade: RunnerChoice
    improve: RunnerChoice


class LlamaSwapSettings(_Strict):
    base_url: str
    timeout_sec: float = Field(gt=0)
    max_turns: int = Field(ge=1)


class SourceToggle(_Strict):
    enabled: bool = False
    bridge_url: str | None = None


class DatafeedSettings(_Strict):
    yfinance: SourceToggle
    mt5: SourceToggle
    twelvedata: SourceToggle
    freshness_max_min: float = Field(gt=0)


class NewsSettings(_Strict):
    cleanup_hours: int = Field(gt=0)


class ScheduleSettings(_Strict):
    trade_interval_min: int = Field(ge=1)
    improve: str = Field(pattern="^(weekly|daily)$")


class LoggingSettings(_Strict):
    level: str = "INFO"


class ApiSettings(_Strict):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8420


class DiscordSettings(_Strict):
    enabled: bool = False


class Settings(_Strict):
    pairs: list[str] = Field(min_length=1)
    risk: RiskSettings
    runner: RunnerSettings
    llama_swap: LlamaSwapSettings
    datafeed: DatafeedSettings
    news: NewsSettings
    schedule: ScheduleSettings
    logging: LoggingSettings
    api: ApiSettings
    discord: DiscordSettings

    @model_validator(mode="after")
    def _pair_rules_cover_pairs(self) -> "Settings":
        missing = [p for p in self.pairs if p not in self.risk.pair_rules]
        if missing:
            raise ValueError(f"risk.pair_rules missing for pairs: {missing}")
        return self


def load_settings(path: Path) -> Settings:
    load_dotenv()
    if not path.exists():
        raise ConfigError(f"settings file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Settings.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(str(e)) from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from e
