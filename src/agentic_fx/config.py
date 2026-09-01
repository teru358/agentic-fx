"""settings.yaml のロードと検証 (1 ファイル、3 分割しない — 設計書 §12)。"""
from __future__ import annotations

import logging
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel, ConfigDict, Field, ValidationError, field_validator,
    model_validator,
)

_log = logging.getLogger("agentic_fx.config")

# round2 裁定B (2026-08-29): drawdown_kill_pct > この値は「到達可能性は
# あるが常識的な運用値ではない」ことの起動時 WARN しきい値。上限
# (le=100、下記 RiskSettings) と役割が違う — le=100 は「equity>=0 の全域で
# ラッチ可能」という穴塞ぎ (到達可能性の保証)、20 超 WARN は妥当性の可視化。
_DRAWDOWN_KILL_PCT_WARN_THRESHOLD = 20


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
    # round2 #1 是正 (裁定B、2026-08-29): 上限が無いと `9999`/`1e9` 等の
    # 到達不能な値を `Settings.model_validate` が受理し、drawdown kill
    # switch (CLAUDE.md 絶対制約) が de facto 無効化できてしまう
    # (probe 実測)。`accounting.drawdown_pct` は `max(0, (hwm-equity)/hwm*100)`
    # で equity<0 のとき 100 を超えうるため equity>=0 の全域を境界にする
    # には `le=100` が要る (equity=0 で drawdown_pct==100.0)。
    drawdown_kill_pct: float = Field(gt=0, le=100)
    limit_deviation_pct: float = Field(gt=0)
    limit_expiry_max_h: float = Field(gt=0, le=24)
    max_slippage_pct: float = Field(gt=0)
    # NY 現地時間。市場クローズ (NY 金 17:00) からの逆算で指定する
    # (America/New_York は DST を跨ぐため、UTC 固定だと季節でずれる)
    friday_swing_cutoff_ny: str = "14:00"
    commission_per_lot: float = Field(ge=0)
    pair_rules: dict[str, PairRule]


class ClaudeCliSettings(_Strict):
    bin: str = "claude"
    credentials_file: str = "~/.claude/.credentials.json"


class CodexCliSettings(_Strict):
    bin: str
    provider: str = "chatgpt"
    auth_file: str = "~/.codex/auth.json"

    @field_validator("provider")
    @classmethod
    def _chatgpt_only(cls, value: str) -> str:
        if value != "chatgpt":
            raise ValueError(
                "codex は namespace tools のため llama.cpp と不成立 "
                "(2026-08-30 裁定)。ローカルは opencode backend を使う")
        return value


class OpencodeCliSettings(_Strict):
    bin: str = "~/.opencode/bin/opencode"
    context_limit: int = 0


class RunnerChoice(_Strict):
    backend: str = Field(pattern="^(local|claude|codex|opencode)$")
    model: str


class RunnerSettings(_Strict):
    trade: RunnerChoice
    improve: RunnerChoice
    claude: ClaudeCliSettings = Field(default_factory=ClaudeCliSettings)
    codex: CodexCliSettings
    opencode: OpencodeCliSettings = Field(default_factory=OpencodeCliSettings)
    cli_terminate_grace_sec: float = Field(gt=0, default=10.0)

    @model_validator(mode="after")
    def _trade_backend_not_codex(self) -> "RunnerSettings":
        if self.trade.backend in ("codex", "opencode"):
            raise ValueError(
                f"runner.trade.backend={self.trade.backend!r} is not allowed "
                "(CLI backend cannot drop shell; trade worker has no Landlock)")
        return self

    @model_validator(mode="after")
    def _opencode_context_limit_required(self) -> "RunnerSettings":
        if (self.improve.backend == "opencode"
                and self.opencode.context_limit <= 0):
            raise ValueError(
                "runner.opencode.context_limit must be >0 when "
                "runner.improve.backend='opencode' (set it equal to "
                "llama-swap --ctx-size for the model)")
        return self


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
    # 換算レート (ConversionRate) の skew 許容 (設計書 §5): ①各脚の鮮度は
    # freshness_max_min で検証済みなのでここでは見ない。②クロス2脚間の
    # 時刻差 ③判断内スナップショット全体 (reference_ts) との時刻差、の
    # 2 つを検証する専用の閾値。**freshness_max_min の使い回しは禁止**
    # (レビュー指摘 F1): reference_ts が判断開始時の now、各脚の quote が
    # 同じ clock の freshness_max_min 以内という構成では、2 つの検証に
    # 同じ値を使うと「脚が freshness の範囲内である」ことと「skew が
    # 閾値以内である」ことが数学的に同値になり、skew 超過が構造的に
    # 発火しなくなる (fail closed が空文になる)。freshness_max_min より
    # 十分小さい独立した値にすること — 検証 (`_skew_tighter_than_freshness`)
    # で強制する。
    conversion_skew_max_min: float = Field(gt=0)
    # 取引の時間軸は固定しない (設計書 §5)。扱う足と、判断が依存する足
    intervals: list[str] = Field(default_factory=lambda: ["1m", "1h"],
                                 min_length=1)
    primary_intervals: list[str] = Field(default_factory=lambda: ["1h"],
                                         min_length=1)
    # 取引不可・分析専用。pair enum には入らない (§6)
    watch_symbols: list[str] = Field(default_factory=list)
    # プラン 9 Task 16: ohlcv_cache の保持期間 (日)。起動時検証
    # (service.py:_validate_cache_retention — _validate_startup から呼ばれる)
    # が、構成済み intervals × **既知チェーン source 全部** の要求日数
    # (live_window_days) と突き合わせる。source を disable しても要求は
    # 緩まない — 後から有効化した瞬間にキャッシュフォールバックが黙って
    # 壊れるのを防ぐため、enabled に関わらず検証する。
    # ここでは正値検証のみ (interval との整合はキャッシュ窓計算
    # (cache_window) を import する必要があり、config.py を datafeed 層に
    # 依存させない方針を保つため service.py 側に置く)。
    cache_retention_days: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _check_intervals(self) -> "DatafeedSettings":
        # INTERVAL_MIN は関数内 import。モジュールトップだと
        # config → datafeed.sources → (yfinance/httpx) の重い依存が
        # 設定読み込みに巻き込まれ、循環 import の温床にもなる
        from agentic_fx.datafeed.sources import INTERVAL_MIN
        unknown = [i for i in self.intervals + self.primary_intervals
                   if i not in INTERVAL_MIN]
        if unknown:
            raise ValueError(f"unknown interval(s): {unknown}")
        if "1m" not in self.intervals:
            # ペーパー約定判定が 1 分足に依存する構造的要件
            raise ValueError("datafeed.intervals must include '1m'")
        missing = set(self.primary_intervals) - set(self.intervals)
        if missing:
            raise ValueError(
                f"primary_intervals must be a subset of intervals: {missing}")
        return self

    @model_validator(mode="after")
    def _skew_tighter_than_freshness(self) -> "DatafeedSettings":
        # レビュー指摘 F1: conversion_skew_max_min が freshness_max_min 以上
        # だと、通常の (処理が速い) 判断では skew 検証が原理的に発火しない
        # 設定に戻ってしまう。誤設定を起動時に弾く。
        if self.conversion_skew_max_min >= self.freshness_max_min:
            raise ValueError(
                "datafeed.conversion_skew_max_min must be < freshness_max_min "
                f"(got {self.conversion_skew_max_min} >= "
                f"{self.freshness_max_min}) — 同じか大きいと skew 検証が "
                "freshness 検証に埋没して発火しなくなる (設計書 §5)")
        return self


class ReflectionSettings(_Strict):
    max_attempts: int = Field(ge=1, default=2)


class AlertSettings(_Strict):
    consecutive_gate_reject: int = Field(ge=1, default=10)


class NewsSettings(_Strict):
    cleanup_hours: int = Field(gt=0)


class ResearchSettings(_Strict):
    max_searches: int = Field(ge=1, default=20)
    max_fetches: int = Field(ge=1, default=30)
    min_interval_sec: float = Field(gt=0, default=2.0)
    max_per_host: int = Field(ge=1, default=5)
    fetch_max_bytes: int = Field(ge=1, default=2_097_152)
    user_agent: str = "agentic-fx/0.1 (+https://github.com/agentic-fx/agentic-fx)"


class ImproveSettings(_Strict):
    parallel: int = Field(ge=1, le=4, default=1)
    mission_max_turns: int = Field(ge=1, default=200)
    mission_timeout_sec: float = Field(ge=60, default=3600)
    llama_swap_verified: bool = False
    max_new_backlog_per_mission: int = Field(ge=1, default=20)
    backtest_rpc_timeout_sec: float = Field(gt=0, default=600)
    research: ResearchSettings = Field(default_factory=ResearchSettings)


class ScheduleSettings(_Strict):
    trade_interval_min: int = Field(ge=1)
    improve: str = Field(pattern="^(weekly|daily)$")
    improve_at: str = "Sat 03:00"

    # round2 #3 是正 (2026-08-29、verified-round2.md #3): `improve_at` に
    # 形式検証が無いと (probe 実測: `"Saturday 03:00"`/`""`/`"xx"`/
    # `"Sat 25:00"` を全て受理)、`ImproveSupervisor.tick` が毎 tick
    # `latest_scheduled_occurrence` → `ValueError` (scheduler.py) を投げ、
    # `scheduler_thread` の `except Exception: _log.exception("tick failed")`
    # に飲まれる — activity にも notifier にも出ないまま改善ループが
    # 恒久沈黙する (取引レーンは影響を受けない)。`improve` の値に応じて
    # 既存の `_DAILY_AT_RE`/`_WEEKLY_AT_RE` (scheduler.py — 正規表現を
    # ここに複製しない) で照合する。`fullmatch` を使う (M1 是正と同じ
    # 末尾改行の穴を新設しない — `$` は MULTILINE 無しでも文字列末尾の
    # 直前の改行にマッチしうるため `.match`/`$` だけでは不十分)。
    @model_validator(mode="after")
    def _check_improve_at(self) -> "ScheduleSettings":
        from agentic_fx.core.scheduler import _DAILY_AT_RE, _WEEKLY_AT_RE
        rx = _DAILY_AT_RE if self.improve == "daily" else _WEEKLY_AT_RE
        if not rx.fullmatch(self.improve_at):
            raise ValueError(
                f"invalid improve_at for cadence {self.improve!r}: "
                f"{self.improve_at!r}")
        return self


class LoggingSettings(_Strict):
    level: str = "INFO"


class ApiSettings(_Strict):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8420


class BacktestSettings(_Strict):
    holdout_months: int = Field(ge=1)
    initial_balance: float = Field(gt=0)


class AnalysisSettings(_Strict):
    max_watch_symbols: int = Field(ge=1)
    max_gap_pct: float = Field(gt=0)


class DiscordSettings(_Strict):
    enabled: bool = False


class PaperSettings(_Strict):
    starting_balance: float = Field(gt=0)


class PluginSettings(_Strict):
    """plugin サンドボックス (プラン 7 Task 2) の resource limit・IPC 設定。
    既定値のみで動く (`Settings.plugin` は default_factory を持つ) ため、
    このセクションを持たない既存 settings.yaml も無変更でロードできる。
    """
    # 1 回の call() (indicator/signal/strategy 呼び出し) の待ち上限。
    # セッション起動 (import plugin.py) の待ちには使わない (別枠の固定
    # 起動タイムアウトを sandbox.py 側に持つ — pandas 初回 import の遅延
    # と暴走 plugin を区別するため)。
    sandbox_timeout_sec: float = Field(gt=0, default=10.0)
    # RLIMIT_CPU — worker プロセス 1 個 (= 1 セッション) の寿命に対する
    # 累積 CPU 秒数上限。
    sandbox_session_cpu_sec: int = Field(ge=1, default=60)
    # RLIMIT_AS (仮想アドレス空間) の上限 MiB。
    sandbox_memory_mb: int = Field(ge=1, default=512)
    # 1 応答行の生バイト数上限。無制限バッファを避けるため読み取り中に
    # この上限を超えた時点で打ち切る。
    sandbox_output_max_bytes: int = Field(ge=1, default=1_048_576)
    # RLIMIT_NOFILE — worker プロセスが同時に開けるファイル記述子数の上限。
    # plugin コードは check_source の denylist によりファイルを開けない
    # ため、想定外の大量オープン (fd リーク) を検知する多層防御。
    sandbox_nofile: int = Field(ge=1, default=128)
    # RLIMIT_FSIZE (MiB) — 1 ファイルあたりの書き込みサイズ上限。plugin
    # コードは denylist により意図的な書き込みができないため、想定外の
    # 大量書き込みを小さく抑える多層防御。pytest サブプロセス (approval.py
    # の test_plugin.py 実行) にも同じ 2 値を流用する。
    sandbox_fsize_mb: int = Field(ge=1, default=8)
    # plugin に渡す DataFrame の末尾最大本数の上限 (config.yaml の
    # max_bars はこれ以下でなければならない — 照合は消費側の責務)。
    max_bars_limit: int = Field(ge=1, default=1000)
    # 本番運用 (producer) が signal/strategy plugin を評価するときのデータ
    # source。承認バックテスト (Task 6) は常に "dukascopy" を使うため、
    # 承認 payload の "live_source" にこの値を載せて「承認 source と本番
    # source の差異」を人間に見せる (プラン 7 Task 6, opus R2 I1)。
    producer_source: str = "yfinance"
    # signals テーブル (プラン 7 Task 7) の requeue 上限。この回数以上
    # requeue_count が溜まると reclaim/requeue で abandoned に終端する。
    signal_requeue_max: int = Field(ge=0, default=2)
    # claim の lease 時間 (分)。この分数だけ claimed のまま経過すると
    # reclaim_expired の回収対象になる。
    signal_lease_min: int = Field(gt=0, default=15)
    # 鮮度ゲート (D4): 宣言 timeframe の何バー分まで新鮮とみなすか。
    signal_freshness_bars: int = Field(ge=1, default=2)
    # signal トリガー取引判断 Mission の最短起動間隔 (分)。
    # missions.signals_rate_ok (プラン 7 Task 8) が missions.trigger LIKE
    # 'signal%' の行 (DB 永続カウンタそのもの) で判定する。
    signal_min_interval_min: int = Field(gt=0, default=10)
    # signal トリガー取引判断 Mission の日次上限 (trading_day_start 境界)。
    signal_daily_max: int = Field(ge=1, default=12)
    # get_signals ツール (プラン 7 Task 9) の lookback 上限 (時間)。
    # since_hours はこの値を超えてはならない (ToolDef スキーマの maximum
    # + 関数側クランプの二重防御 — 詳細は tools/signal_tools.py)。
    signals_max_lookback_hours: int = Field(ge=1, default=24)
    # gate pytest (プラン10 Task 6) の待ち上限。既存 approval.py の
    # `_PYTEST_TIMEOUT_SEC = 300.0` と同じ既定値を config 化する
    # (submit/bless の pytest 実行と改善ループの候補ゲートが共有)。
    pytest_timeout_sec: float = Field(gt=0, default=300.0)


class WorkerSettings(_Strict):
    """mission worker (プラン8) の壁時計監視・resource limit・IPC 設定。
    既定値のみで動く (`Settings.worker` は default_factory を持つ)。
    """
    # 子プロセス側 resource limit (§12 申し送り② — 実測に基づく確定値。
    # 上記実測手順のコメント参照)。
    child_as_mb: int = Field(ge=1, default=4096)
    child_nofile: int = Field(ge=1, default=128)
    child_fsize_mb: int = Field(ge=1, default=8)
    # 親側の preemption エスカレーション (設計書 §4.7)。
    worker_grace_sec: float = Field(gt=0, default=30.0)
    worker_terminate_grace_sec: float = Field(gt=0, default=10.0)
    worker_startup_timeout_sec: float = Field(gt=0, default=30.0)
    # Mission 累積 transcript 上限 (設計書 §4.3 codex M-3)。
    transcript_max_bytes: int = Field(ge=1, default=1_048_576)
    # tick 内データ hooks が内部で使う全ネットワーククライアントの
    # timeout 上限 (設計書 §3.2 codex I2-1 — wall-clock 保証ではない)。
    data_hook_timeout_sec: float = Field(gt=0, default=30.0)
    # RAG RPC の親側応答待ち上限 (設計書 §4.3/§4.4)。
    rpc_timeout_sec: float = Field(gt=0, default=15.0)
    # 停止シーケンスの join 上限 (設計書 §5)。
    shutdown_join_timeout_sec: float = Field(gt=0, default=30.0)
    # commit-pre で取得したスナップショットの許容鮮度 (秒)。commit-core
    # 開始時にこれを超えていれば発注拒否する (lock 内での再取得はしない
    # — 設計書 §3.1)。
    snapshot_max_age_sec: float = Field(gt=0, default=10.0)


class Settings(_Strict):
    # ログ・status 表示に使う (保存は常に UTC、市場境界は NY 固定で変更不可)
    display_timezone: str = "UTC"
    # 利用者の基準通貨。損益・リスク・エクイティの表現に使う (取引可能ペアを
    # 制限するものではない)。実取引では MT5 口座通貨と照合する (設計書 §5)
    account_currency: str = Field(default="JPY", pattern="^[A-Z]{3}$")
    pairs: list[str] = Field(min_length=1)
    risk: RiskSettings
    runner: RunnerSettings
    llama_swap: LlamaSwapSettings
    backtest: BacktestSettings
    datafeed: DatafeedSettings
    news: NewsSettings
    schedule: ScheduleSettings
    improve: ImproveSettings = Field(default_factory=ImproveSettings)
    logging: LoggingSettings
    api: ApiSettings
    discord: DiscordSettings
    analysis: AnalysisSettings
    paper: PaperSettings
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    reflection: ReflectionSettings = Field(default_factory=ReflectionSettings)
    alert: AlertSettings = Field(default_factory=AlertSettings)

    @field_validator("display_timezone")
    @classmethod
    def _valid_display_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"invalid display_timezone: {v!r} is not a "
                             f"known IANA timezone") from e
        return v

    @model_validator(mode="after")
    def _pair_rules_cover_pairs(self) -> "Settings":
        missing = [p for p in self.pairs if p not in self.risk.pair_rules]
        if missing:
            raise ValueError(f"risk.pair_rules missing for pairs: {missing}")
        return self

    @model_validator(mode="after")
    def _watch_symbols_capped(self) -> "Settings":
        if len(self.datafeed.watch_symbols) > self.analysis.max_watch_symbols:
            raise ValueError(
                f"datafeed.watch_symbols ({len(self.datafeed.watch_symbols)}) "
                f"exceeds analysis.max_watch_symbols ({self.analysis.max_watch_symbols})")
        return self

    @model_validator(mode="after")
    def _warn_high_drawdown_kill_pct(self) -> "Settings":
        # round2 #1 残余 (裁定B): `le=100` は到達可能性の保証止まりで、
        # `99.99` のような「実質無効化」に近い値は妥当。raise しない
        # (config で無効化不可の絶対制約は上限で担保済み、ここは可観測性)。
        if self.risk.drawdown_kill_pct > _DRAWDOWN_KILL_PCT_WARN_THRESHOLD:
            _log.warning(
                "risk.drawdown_kill_pct=%s is above the sanity threshold "
                "(%s%%) — the drawdown kill switch may rarely or never "
                "latch in practice",
                self.risk.drawdown_kill_pct, _DRAWDOWN_KILL_PCT_WARN_THRESHOLD)
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
