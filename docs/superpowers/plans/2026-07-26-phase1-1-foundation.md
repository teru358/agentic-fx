# Phase 1 プラン 1: 基盤 + 共有契約 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agentic-fx の土台 — 共有契約 (型)、設定、ログ 2 軸、SQLite 11 テーブル + CRUD、状態ストア、`main.py init` + 起動ガード、CI — を TDD で構築する。

**Architecture:** レイヤー型 src レイアウト (設計書 §11)。本プランは `core/contracts.py` (全プラン共有の型)、`config.py`、`logging_setup.py` / `activity.py`、`store/` (永続化 CRUD のみ — 状態遷移ルールはプラン 2)、`entry.py` + `service.py` (init と起動ガード) を作る。LLM 関連は一切登場しない。

**Tech Stack:** Python 3.13 / uv / pydantic v2 / PyYAML / python-dotenv / sqlite3 (stdlib) / pytest

## Global Constraints (設計書より)

- パッケージ管理は uv (`uv add <pkg>` / `uv run pytest`)。pip を直接使わない
- TDD: failing test → 実装 → green → commit を厳守
- `core/` は LLM 関連を一切 import しない (設計書 §11)
- `config/settings.yaml` は gitignore 済み。**`config/settings.yaml.example` のみコミットし、新規キー追加時は必ず両方を同期**
- `data/`, `logs/`, `.env`, `CLAUDE.md`, `plugins/` はコミット対象外 (gitignore 済み)
- kill switch は config で無効化不可 → **Settings に無効化キーを設けない** (存在しないことがガード)
- 稼働モード・autopilot は settings.yaml でなく `data/state/` に保存 (設計書 §3)
- orders の状態名は設計書 §12 の遷移図の全状態を最初から enum に固定する (実装は後続プラン)
- コミットメッセージ末尾: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

**本プランの責務境界 (codex 評価で確定):**
- store/ は**永続化 CRUD のみ**。状態遷移の許可判定・risk 計算はプラン 2
- init は**基盤部分のみ** (設定生成・DB 初期化・state=learning・ログ・起動ガード)。価格ソース接続確認はプラン 3、llama-swap 接続確認はプラン 5 で追加
- Phase 2/3 専用テーブル (improvement_*, approval_requests, news_sources) はスキーマ + 基本 CRUD まで

---

### Task 1: プロジェクト骨格 + 依存 + CI

**Files:**
- Modify: `pyproject.toml`
- Create: `src/agentic_fx/__init__.py`, `src/agentic_fx/core/__init__.py`, `src/agentic_fx/store/__init__.py`
- Create: `tests/__init__.py`, `tests/test_smoke.py`
- Create: `.github/workflows/ci.yml`
- Create: `config/.gitkeep`, `policy/.gitkeep`

**Interfaces:**
- Produces: パッケージ `agentic_fx` が import 可能。`uv run pytest` が動く。CI が PR で pytest を実行

- [ ] **Step 1: 依存を追加**

```bash
uv add pydantic pyyaml python-dotenv
uv add --dev pytest
```

- [ ] **Step 2: pyproject.toml を src レイアウトに変更**

`pyproject.toml` に追記 (既存の `[project]` は維持):

```toml
[project.scripts]
afx = "agentic_fx.entry:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/agentic_fx"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: 失敗するスモークテストを書く**

`tests/test_smoke.py`:

```python
def test_package_importable():
    import agentic_fx  # noqa: F401
```

- [ ] **Step 4: テストが失敗することを確認**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (ModuleNotFoundError: agentic_fx)

- [ ] **Step 5: パッケージ骨格を作成**

```bash
mkdir -p src/agentic_fx/core src/agentic_fx/store
touch src/agentic_fx/__init__.py src/agentic_fx/core/__init__.py src/agentic_fx/store/__init__.py tests/__init__.py
mkdir -p config policy && touch config/.gitkeep policy/.gitkeep
uv sync
```

- [ ] **Step 6: テストが通ることを確認**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 7: CI を作成**

`.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.13"
      - run: uv sync --dev
      - run: uv run pytest -v
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src tests .github config policy
git commit -m "chore: src レイアウト骨格 + 依存 + CI"
```

---

### Task 2: 共有契約 (core/contracts.py)

全プランが依存する型。**このタスクで固定した名前・型を後続プランは変更なしで使う。**

**Files:**
- Create: `src/agentic_fx/core/contracts.py`
- Test: `tests/core/test_contracts.py` (+ `tests/core/__init__.py`)

**Interfaces:**
- Produces:
  - StrEnum: `Action(open|close|cancel|hold)`, `Direction(long|short)`, `EntryType(market|limit)`, `Horizon(day|swing)`, `Origin(scheduler|ask)`, `Mode(learning|trading)`
  - StrEnum `OrderStatus`: `approval_pending, submitting, submitted, pending_fill, protection_pending, open, closing, closed, cancelling, cancelled, cancel_unknown, close_unknown, submit_unknown, rejected, expired, invalidated` (設計書 §12 遷移図の全状態)
  - frozen dataclass: `Quote(symbol, bid, ask, ts, source)`, `Bar(symbol, interval, ts, open, high, low, close, volume)`, `AccountState(balance, equity, currency, ts, source)`, `InstrumentSpec(symbol, pip_size, min_lot, max_lot, lot_step, contract_size)`, `BrokerResult(status: Literal["ok","rejected","unknown"], broker_order_id, broker_position_id, message)`
  - frozen dataclass `TradeIntent(action, origin, pair, direction, entry_type, horizon, order_id, limit_price, expires_in_h, stop_loss, take_profit, confidence, reasoning)` + classmethod `TradeIntent.from_llm_dict(d: dict, origin: Origin) -> TradeIntent` (構造検証。セマンティック検証は risk gate = プラン 2)
  - `IntentParseError(ValueError)`
  - `Clock` Protocol: `now() -> datetime` (UTC aware)。テスト用 `FixedClock(dt)`

- [ ] **Step 1: 失敗するテストを書く**

`tests/core/test_contracts.py`:

```python
from datetime import datetime, timezone

import pytest

from agentic_fx.core.contracts import (
    Action, Direction, EntryType, Horizon, IntentParseError,
    Origin, OrderStatus, FixedClock, TradeIntent,
)


def _open_dict(**over):
    d = {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "day",
        "limit_price": 148.20, "expires_in": "4h",
        "stop_loss": 147.80, "take_profit": 149.00,
        "confidence": 0.7, "reasoning": "test",
    }
    d.update(over)
    return d


def test_from_llm_dict_open_limit():
    it = TradeIntent.from_llm_dict(_open_dict(), origin=Origin.SCHEDULER)
    assert it.action is Action.OPEN
    assert it.direction is Direction.LONG
    assert it.entry_type is EntryType.LIMIT
    assert it.horizon is Horizon.DAY
    assert it.expires_in_h == 4.0
    assert it.origin is Origin.SCHEDULER


def test_open_requires_stop_loss():
    with pytest.raises(IntentParseError, match="stop_loss"):
        TradeIntent.from_llm_dict(_open_dict(stop_loss=None), origin=Origin.SCHEDULER)


def test_open_requires_horizon():
    d = _open_dict()
    del d["horizon"]
    with pytest.raises(IntentParseError, match="horizon"):
        TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)


def test_limit_requires_limit_price_and_expiry():
    with pytest.raises(IntentParseError, match="limit_price"):
        TradeIntent.from_llm_dict(_open_dict(limit_price=None), origin=Origin.SCHEDULER)


def test_market_rejects_limit_fields():
    d = _open_dict(entry_type="market")
    with pytest.raises(IntentParseError, match="limit"):
        TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)


def test_close_requires_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "close"}, origin=Origin.SCHEDULER)


def test_hold_minimal():
    it = TradeIntent.from_llm_dict({"action": "hold", "reasoning": "様子見"},
                                   origin=Origin.SCHEDULER)
    assert it.action is Action.HOLD


def test_unknown_action_rejected():
    with pytest.raises(IntentParseError, match="action"):
        TradeIntent.from_llm_dict({"action": "buy"}, origin=Origin.SCHEDULER)


def test_nan_price_rejected():
    with pytest.raises(IntentParseError, match="finite"):
        TradeIntent.from_llm_dict(_open_dict(stop_loss=float("nan")),
                                  origin=Origin.SCHEDULER)


def test_order_status_has_all_states():
    names = {s.value for s in OrderStatus}
    assert {"approval_pending", "submitting", "submitted", "pending_fill",
            "protection_pending", "open", "closing", "closed", "cancelling",
            "cancelled", "cancel_unknown", "close_unknown", "submit_unknown",
            "rejected", "expired", "invalidated"} == names


def test_fixed_clock():
    dt = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    assert FixedClock(dt).now() == dt
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/core/test_contracts.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/core/contracts.py`:

```python
"""全レイヤー共有の契約型。LLM 出力の構造検証まで担当 (セマンティック検証は risk_gate)。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol


class Action(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    CANCEL = "cancel"
    HOLD = "hold"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class EntryType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class Horizon(StrEnum):
    DAY = "day"
    SWING = "swing"


class Origin(StrEnum):
    SCHEDULER = "scheduler"
    ASK = "ask"


class Mode(StrEnum):
    LEARNING = "learning"
    TRADING = "trading"


class OrderStatus(StrEnum):
    APPROVAL_PENDING = "approval_pending"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    PENDING_FILL = "pending_fill"
    PROTECTION_PENDING = "protection_pending"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    CANCEL_UNKNOWN = "cancel_unknown"
    CLOSE_UNKNOWN = "close_unknown"
    SUBMIT_UNKNOWN = "submit_unknown"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: datetime
    source: str


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    interval: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class AccountState:
    balance: float
    equity: float
    currency: str
    ts: datetime
    source: str


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    pip_size: float
    min_lot: float
    max_lot: float
    lot_step: float
    contract_size: float


@dataclass(frozen=True, slots=True)
class BrokerResult:
    status: Literal["ok", "rejected", "unknown"]
    broker_order_id: str | None = None
    broker_position_id: str | None = None
    message: str = ""


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class FixedClock:
    fixed: datetime

    def now(self) -> datetime:
        return self.fixed


class IntentParseError(ValueError):
    pass


_EXPIRES_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*h$")


def _finite(d: dict, key: str, required: bool) -> float | None:
    v = d.get(key)
    if v is None:
        if required:
            raise IntentParseError(f"{key} is required")
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise IntentParseError(f"{key} must be a number") from None
    if not math.isfinite(f):
        raise IntentParseError(f"{key} must be finite")
    return f


def _enum(cls, d: dict, key: str):
    v = d.get(key)
    if v is None:
        raise IntentParseError(f"{key} is required")
    try:
        return cls(v)
    except ValueError:
        raise IntentParseError(
            f"invalid {key}: {v!r} (allowed: {[m.value for m in cls]})") from None


@dataclass(frozen=True, slots=True)
class TradeIntent:
    action: Action
    origin: Origin
    reasoning: str = ""
    pair: str | None = None
    direction: Direction | None = None
    entry_type: EntryType | None = None
    horizon: Horizon | None = None
    order_id: int | None = None
    limit_price: float | None = None
    expires_in_h: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float | None = None

    @classmethod
    def from_llm_dict(cls, d: dict, *, origin: Origin) -> "TradeIntent":
        if not isinstance(d, dict):
            raise IntentParseError("intent must be an object")
        action = _enum(Action, d, "action")
        reasoning = str(d.get("reasoning", ""))

        if action in (Action.CLOSE, Action.CANCEL):
            order_id = d.get("order_id")
            if not isinstance(order_id, int):
                raise IntentParseError(f"{action.value} requires integer order_id")
            return cls(action=action, origin=origin, order_id=order_id,
                       reasoning=reasoning)

        if action is Action.HOLD:
            return cls(action=action, origin=origin, reasoning=reasoning)

        # action == open
        pair = d.get("pair")
        if not pair or not isinstance(pair, str):
            raise IntentParseError("open requires pair")
        direction = _enum(Direction, d, "direction")
        entry_type = _enum(EntryType, d, "entry_type")
        horizon = _enum(Horizon, d, "horizon")
        stop_loss = _finite(d, "stop_loss", required=True)
        take_profit = _finite(d, "take_profit", required=False)
        confidence = _finite(d, "confidence", required=False)

        limit_price = _finite(d, "limit_price", required=entry_type is EntryType.LIMIT)
        expires_in_h: float | None = None
        if entry_type is EntryType.LIMIT:
            raw = d.get("expires_in")
            if raw is None:
                raise IntentParseError("limit requires expires_in")
            m = _EXPIRES_RE.match(str(raw).strip())
            if not m:
                raise IntentParseError(f"expires_in must look like '4h', got {raw!r}")
            expires_in_h = float(m.group(1))
        else:
            if d.get("limit_price") is not None or d.get("expires_in") is not None:
                raise IntentParseError("market must not carry limit fields")
            limit_price = None

        return cls(action=action, origin=origin, pair=pair, direction=direction,
                   entry_type=entry_type, horizon=horizon, limit_price=limit_price,
                   expires_in_h=expires_in_h, stop_loss=stop_loss,
                   take_profit=take_profit, confidence=confidence,
                   reasoning=reasoning)
```

`tests/core/__init__.py` を空で作成。

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/core/test_contracts.py -v`
Expected: PASS (全件)

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/core/contracts.py tests/core
git commit -m "feat: 共有契約 (TradeIntent 構造検証・OrderStatus 全状態・Clock)"
```

---

### Task 3: 設定 (config.py + settings.yaml.example)

**Files:**
- Create: `src/agentic_fx/config.py`
- Create: `config/settings.yaml.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: pydantic モデル `Settings` (下記構造)、`load_settings(path: Path) -> Settings`、`ConfigError(Exception)`。`.env` は `load_settings` 内で `dotenv.load_dotenv()` 済み
- 注意: **kill switch の有効/無効キーは存在しない** (無効化不可の構造的担保)。閾値 `drawdown_kill_pct` のみ設定可

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_config.py`:

```python
from pathlib import Path

import pytest

from agentic_fx.config import ConfigError, Settings, load_settings

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example"


def test_example_file_loads():
    s = load_settings(EXAMPLE)
    assert isinstance(s, Settings)
    assert s.pairs == ["USDJPY", "EURUSD"]
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
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: example と実装を書く**

`config/settings.yaml.example`:

```yaml
# agentic-fx 設定 (このファイルを settings.yaml にコピーして編集。新キー追加時は両方を同期)
pairs: [USDJPY, EURUSD]

risk:
  rr_min: 1.5                 # 最小リスク報酬比 (spread 込み)
  risk_per_trade_pct: 0.5     # 1 取引リスク上限 (% of equity)
  swing_risk_factor: 0.5      # horizon=swing のリスク率係数 (常時半減)
  max_positions: 2            # 未約定指値も枠にカウント
  max_total_risk_pct: 1.5     # 最大総エクスポージャー: 全ポジション + 未約定指値の予約リスク合算 (% of equity)
  max_leverage: 10            # 必要証拠金ベースのレバレッジ上限
  daily_loss_limit_pct: 1.0   # 日初エクイティ比
  drawdown_kill_pct: 2.0      # HWM 比。kill switch 自体の無効化キーは存在しない
  limit_deviation_pct: 0.5    # 指値の現値乖離上限
  limit_expiry_max_h: 24
  max_slippage_pct: 0.1
  friday_swing_cutoff_utc: "18:00"  # 金曜この時刻以降の新規 swing 建て禁止
  commission_per_lot: 0.0     # 往復手数料 (口座通貨建て、sizing のリスク額に算入)
  pair_rules:                 # ペア毎の SL 距離制約 + 想定 spread (設計書 §5: SL 距離は pair 毎 config)
    USDJPY: {sl_distance_min_pips: 5, sl_distance_max_pips: 200, assumed_spread_pips: 1.0}
    EURUSD: {sl_distance_min_pips: 5, sl_distance_max_pips: 200, assumed_spread_pips: 1.0}

runner:
  trade:   {backend: local, model: qwen3.6-35b}
  improve: {backend: local, model: qwen3.6-35b}

llama_swap:
  base_url: "http://localhost:8080/v1"
  timeout_sec: 300
  max_turns: 16

datafeed:
  yfinance:   {enabled: true}
  mt5:        {enabled: false, bridge_url: "http://localhost:8812"}
  twelvedata: {enabled: false}
  freshness_max_min: 20       # これより古い quote/bar は不健全扱い

news:
  cleanup_hours: 48

schedule:
  trade_interval_min: 60
  improve: weekly             # weekly | daily

logging:
  level: INFO

api:
  enabled: false
  host: 127.0.0.1
  port: 8420

discord:
  enabled: false              # webhook 通知 (URL は .env の DISCORD_WEBHOOK_URL)
```

`src/agentic_fx/config.py`:

```python
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
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/config.py config/settings.yaml.example tests/test_config.py
git commit -m "feat: 設定ロード + 検証 (kill switch 無効化キーなしの構造的担保)"
```

---

### Task 4: 技術ログ (logging_setup.py)

**Files:**
- Create: `src/agentic_fx/logging_setup.py`
- Test: `tests/test_logging_setup.py`

**Interfaces:**
- Produces: `setup_technical_logging(log_dir: Path, level: str = "INFO") -> logging.Logger` — `agentic_fx` 名前空間の logger を `logs/agentic.log` (RotatingFileHandler 10MB × 5 世代) に接続。**stdout ハンドラは付けない** (設計書 §13: CLI 通常出力に混ぜない)。多重呼び出しでハンドラを増やさない
- **journald 経路の契約 (実装はプラン 5)**: 設計書 §13 の「+ journald」は、`--daemon` 起動時のみ stderr への StreamHandler を追加し、systemd が journald へ取り込む構成で実現する。TTY (対話シェル) モードはファイルのみ — プラン 5 で `setup_technical_logging(log_dir, level, daemon=False)` の `daemon` パラメータとして拡張する

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_logging_setup.py`:

```python
import logging

from agentic_fx.logging_setup import setup_technical_logging


def test_writes_to_file_only(tmp_path):
    logger = setup_technical_logging(tmp_path, level="INFO")
    logger.info("hello technical")
    logger.debug("should be filtered")
    text = (tmp_path / "agentic.log").read_text(encoding="utf-8")
    assert "hello technical" in text
    assert "should be filtered" not in text
    assert not any(isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)
                   for h in logger.handlers)


def test_idempotent_setup(tmp_path):
    l1 = setup_technical_logging(tmp_path)
    l2 = setup_technical_logging(tmp_path)
    assert l1 is l2
    assert len(l2.handlers) == 1


def test_reinit_with_different_dir_switches_file(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    setup_technical_logging(dir_a).info("to-a")
    logger = setup_technical_logging(dir_b)
    logger.info("to-b")
    assert len(logger.handlers) == 1
    assert "to-b" not in (dir_a / "agentic.log").read_text(encoding="utf-8")
    assert "to-b" in (dir_b / "agentic.log").read_text(encoding="utf-8")
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_logging_setup.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/logging_setup.py`:

```python
"""技術ログ (severity 軸)。activity ログとは完全分離 — 設計書 §13。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_technical_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    target = (log_dir / "agentic.log").resolve()
    logger = logging.getLogger("agentic_fx")
    logger.setLevel(level.upper())
    logger.propagate = False
    # 同一パスなら既存 handler を再利用、異なるパスなら close して差し替える
    for h in list(logger.handlers):
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == target:
            return logger
        logger.removeHandler(h)
        h.close()
    handler = RotatingFileHandler(target, maxBytes=10 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    return logger
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_logging_setup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/logging_setup.py tests/test_logging_setup.py
git commit -m "feat: 技術ログ (RotatingFileHandler、stdout 非混入)"
```

---

### Task 5: activity ログ (activity.py)

**Files:**
- Create: `src/agentic_fx/activity.py`
- Test: `tests/test_activity.py`

**Interfaces:**
- Produces: StrEnum `Category(NEWS|TECH|AGGREGATE|TRADE|IMPROVE|APPROVAL|SYSTEM)`; class `ActivityLog(path: Path)` — `.write(category: Category, event: str, summary: str, ref_id: str | None = None) -> None` (1 行 = `ISO8601\tCATEGORY\tevent\tsummary\tref_id`)、`.tail(n: int = 20, category: Category | None = None) -> list[str]` (pull 型リーダ、シェルの `activity` コマンドが使用)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_activity.py`:

```python
from agentic_fx.activity import ActivityLog, Category


def test_write_format(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    log.write(Category.TRADE, "order_opened", "USDJPY long 0.10lot", ref_id="42")
    line = (tmp_path / "activity.log").read_text(encoding="utf-8").strip()
    parts = line.split("\t")
    assert parts[1] == "TRADE"
    assert parts[2] == "order_opened"
    assert parts[3] == "USDJPY long 0.10lot"
    assert parts[4] == "42"


def test_tail_with_category_filter(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    for i in range(5):
        log.write(Category.NEWS, "fetched", f"n{i}")
    log.write(Category.TRADE, "order_opened", "t1")
    assert len(log.tail(3)) == 3
    trades = log.tail(10, category=Category.TRADE)
    assert len(trades) == 1 and "t1" in trades[0]


def test_tail_empty_file(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    assert log.tail(5) == []


def test_summary_newlines_sanitized(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    log.write(Category.SYSTEM, "boot", "line1\nline2")
    assert len((tmp_path / "activity.log").read_text().strip().splitlines()) == 1
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_activity.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/activity.py`:

```python
"""activity ログ (カテゴリ軸)。ユーザーが読む行動記録 — 設計書 §13。"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class Category(StrEnum):
    NEWS = "NEWS"
    TECH = "TECH"
    AGGREGATE = "AGGREGATE"
    TRADE = "TRADE"
    IMPROVE = "IMPROVE"
    APPROVAL = "APPROVAL"
    SYSTEM = "SYSTEM"


class ActivityLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, category: Category, event: str, summary: str,
              ref_id: str | None = None) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        clean = " ".join(summary.split())
        line = "\t".join([ts, category.value, event, clean, ref_id or "-"])
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def tail(self, n: int = 20, category: Category | None = None) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if category is not None:
            lines = [l for l in lines
                     if l.split("\t", 2)[1:2] == [category.value]]
        return lines[-n:]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_activity.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/activity.py tests/test_activity.py
git commit -m "feat: activity ログ (7 カテゴリ・pull 型 tail)"
```

---

### Task 6: SQLite スキーマ (store/db.py)

**Files:**
- Create: `src/agentic_fx/store/db.py`
- Test: `tests/store/test_db.py` (+ `tests/store/__init__.py`)

**Interfaces:**
- Produces: `connect(db_path: Path) -> sqlite3.Connection` (Row factory / WAL / foreign_keys ON)、`init_db(conn) -> None` (冪等)、定数 `TABLE_NAMES: frozenset[str]` (11 テーブル)

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_db.py`:

```python
from agentic_fx.store.db import TABLE_NAMES, connect, init_db

EXPECTED = {
    "ohlcv", "missions", "trade_intents", "orders", "reflections",
    "account_snapshots", "improvement_backlog", "improvement_runs",
    "econ_events", "approval_requests", "news_sources",
}


def test_init_creates_all_11_tables(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    assert {r["name"] for r in rows} == EXPECTED
    assert TABLE_NAMES == frozenset(EXPECTED)


def test_init_is_idempotent(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    init_db(conn)  # 二回目でも例外なし


def test_foreign_keys_enabled(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_orders_has_lifecycle_columns(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    assert {"client_order_id", "quantity", "filled_quantity",
            "remaining_quantity", "avg_fill_price", "broker_order_id",
            "broker_position_id", "broker_synced_at", "horizon",
            "status"} <= cols
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/store/test_db.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/store/db.py`:

```python
"""SQLite 接続 + 11 テーブルスキーマ — 設計書 §12。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (symbol, interval, bar_time)
);
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,            -- trade | improve | ask | reflection
  runner TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  output_json TEXT, transcript_json TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS trade_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL REFERENCES missions(id),
  payload_json TEXT NOT NULL,
  gate_result TEXT,              -- accepted | rejected (NULL = 未判定)
  reject_reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER REFERENCES trade_intents(id),
  approval_id INTEGER REFERENCES approval_requests(id),
  client_order_id TEXT UNIQUE,
  pair TEXT NOT NULL, direction TEXT NOT NULL,
  entry_type TEXT NOT NULL, horizon TEXT NOT NULL,
  status TEXT NOT NULL,
  quantity REAL, filled_quantity REAL DEFAULT 0,
  remaining_quantity REAL, avg_fill_price REAL,
  requested_price REAL, close_price REAL,
  stop_loss REAL, take_profit REAL,
  fees_swap REAL DEFAULT 0, realized_pnl REAL, close_reason TEXT,
  broker_order_id TEXT, broker_position_id TEXT, broker_synced_at TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  filled_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS reflections (
  order_id INTEGER PRIMARY KEY REFERENCES orders(id),
  content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, balance REAL NOT NULL, equity REAL NOT NULL,
  hwm REAL NOT NULL, cashflow REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'paper'
);
CREATE TABLE IF NOT EXISTS improvement_backlog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea TEXT NOT NULL,
  source TEXT NOT NULL,          -- user | agent | research
  status TEXT NOT NULL DEFAULT 'open',  -- open | selected | done | rejected
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS improvement_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backlog_id INTEGER REFERENCES improvement_backlog(id),
  result TEXT,                   -- pr | approval | report
  pr_url TEXT, approval_id INTEGER, report_path TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS econ_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, country TEXT NOT NULL, name TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 0,
  actual TEXT, forecast TEXT, previous TEXT,
  UNIQUE (ts, country, name)
);
CREATE TABLE IF NOT EXISTS approval_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,            -- tech_plugin | news_source | live_trade
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  -- pending | approved | rejected | expired | invalidated
  reason TEXT, decided_by TEXT, decided_at TEXT,
  message_id TEXT, expires_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  fetcher TEXT NOT NULL,         -- feed | web
  url TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 0,
  added_by TEXT NOT NULL,        -- user | agent
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
"""

TABLE_NAMES = frozenset({
    "ohlcv", "missions", "trade_intents", "orders", "reflections",
    "account_snapshots", "improvement_backlog", "improvement_runs",
    "econ_events", "approval_requests", "news_sources",
})


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/store/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/db.py tests/store
git commit -m "feat: SQLite 11 テーブルスキーマ (orders は全ライフサイクルカラム)"
```

---

### Task 7: 取引系 store (orders / intents / missions)

**永続化 CRUD のみ。状態遷移の許可判定はプラン 2 の責務** — ここでは任意の status 更新を許す代わりに、更新は必ず `updated_at` を刻む。

**Files:**
- Create: `src/agentic_fx/store/orders.py`, `src/agentic_fx/store/intents.py`, `src/agentic_fx/store/missions.py`
- Test: `tests/store/test_orders.py`, `tests/store/test_intents.py`, `tests/store/test_missions.py`

**Interfaces:**
- Produces:
  - `orders.insert(conn, *, pair, direction, entry_type, horizon, status, now, **optional) -> int` / `orders.get(conn, order_id) -> dict | None` / `orders.update_fields(conn, order_id, *, now, **fields) -> None` / `orders.list_by_status(conn, *statuses) -> list[dict]`
  - `intents.insert(conn, mission_id, payload: dict, now) -> int` / `intents.set_gate_result(conn, intent_id, accepted: bool, reject_reason: str | None) -> None` / `intents.get(conn, intent_id) -> dict | None`
  - `missions.start(conn, loop: str, runner: str, model: str, now) -> int` / `missions.finish(conn, mission_id, status: str, output: dict | None, transcript: list, now) -> None` / `missions.recent(conn, n: int) -> list[dict]`
  - `now` はすべて timezone-aware `datetime` (呼び出し側が Clock から渡す — テスト容易性のため内部で now() しない)

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_orders.py`:

```python
from datetime import datetime, timezone

from agentic_fx.core.contracts import OrderStatus
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_insert_and_get(tmp_path):
    c = _conn(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="limit",
                        horizon="day", status=OrderStatus.PENDING_FILL, now=NOW,
                        quantity=0.1, stop_loss=147.8, requested_price=148.2)
    row = orders.get(c, oid)
    assert row["pair"] == "USDJPY"
    assert row["status"] == "pending_fill"
    assert row["quantity"] == 0.1
    assert orders.get(c, 9999) is None


def test_update_fields_touches_updated_at(tmp_path):
    c = _conn(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="swing", status=OrderStatus.SUBMITTING, now=NOW)
    later = datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc)
    orders.update_fields(c, oid, now=later, status=OrderStatus.OPEN,
                         avg_fill_price=148.25, filled_quantity=0.1)
    row = orders.get(c, oid)
    assert row["status"] == "open"
    assert row["updated_at"] == later.isoformat()


def test_list_by_status(tmp_path):
    c = _conn(tmp_path)
    orders.insert(c, pair="USDJPY", direction="long", entry_type="limit",
                  horizon="day", status=OrderStatus.PENDING_FILL, now=NOW)
    orders.insert(c, pair="EURUSD", direction="short", entry_type="market",
                  horizon="day", status=OrderStatus.OPEN, now=NOW)
    rows = orders.list_by_status(c, OrderStatus.PENDING_FILL, OrderStatus.OPEN)
    assert len(rows) == 2
    assert len(orders.list_by_status(c, OrderStatus.CLOSED)) == 0
```

`tests/store/test_intents.py`:

```python
from datetime import datetime, timezone

from agentic_fx.store import intents, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_insert_and_gate_result(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    mid = missions.start(c, "trade", "local", "qwen3.6-35b", NOW)
    iid = intents.insert(c, mid, {"action": "hold", "reasoning": "様子見"}, NOW)
    intents.set_gate_result(c, iid, accepted=False, reject_reason="RR below 1.5")
    row = intents.get(c, iid)
    assert row["gate_result"] == "rejected"
    assert row["reject_reason"] == "RR below 1.5"
    assert '"hold"' in row["payload_json"]
```

`tests/store/test_missions.py`:

```python
import json
from datetime import datetime, timezone

from agentic_fx.store import missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_start_finish_recent(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    mid = missions.start(c, "trade", "local", "qwen3.6-35b", NOW)
    missions.finish(c, mid, "completed", {"action": "hold"},
                    [{"role": "assistant", "content": "..."}], NOW)
    rows = missions.recent(c, 5)
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert json.loads(rows[0]["output_json"]) == {"action": "hold"}
    assert json.loads(rows[0]["transcript_json"])[0]["role"] == "assistant"
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/store -v`
Expected: 新規 3 ファイルが FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/store/orders.py`:

```python
"""orders 永続化 CRUD。状態遷移の許可判定は core (プラン 2) の責務。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

_OPTIONAL = {
    "intent_id", "approval_id", "client_order_id", "quantity",
    "filled_quantity", "remaining_quantity", "avg_fill_price",
    "requested_price", "close_price", "stop_loss", "take_profit",
    "fees_swap", "realized_pnl", "close_reason", "broker_order_id",
    "broker_position_id", "broker_synced_at", "expires_at",
    "filled_at", "closed_at",
}


def insert(conn: sqlite3.Connection, *, pair: str, direction: str,
           entry_type: str, horizon: str, status: str,
           now: datetime, **optional) -> int:
    unknown = set(optional) - _OPTIONAL
    if unknown:
        raise ValueError(f"unknown order fields: {unknown}")
    cols = ["pair", "direction", "entry_type", "horizon", "status",
            "created_at", "updated_at", *optional]
    vals = [pair, direction, entry_type, horizon, str(status),
            now.isoformat(), now.isoformat(), *optional.values()]
    q = f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join('?' * len(vals))})"
    cur = conn.execute(q, vals)
    conn.commit()
    return cur.lastrowid


def get(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def update_fields(conn: sqlite3.Connection, order_id: int, *,
                  now: datetime, **fields) -> None:
    unknown = set(fields) - _OPTIONAL - {"status"}
    if unknown:
        raise ValueError(f"unknown order fields: {unknown}")
    fields = {k: (str(v) if k == "status" else v) for k, v in fields.items()}
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    conn.execute(f"UPDATE orders SET {sets} WHERE id=?",
                 [*fields.values(), now.isoformat(), order_id])
    conn.commit()


def list_by_status(conn: sqlite3.Connection, *statuses) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY id",
        [str(s) for s in statuses]).fetchall()
    return [dict(r) for r in rows]
```

`src/agentic_fx/store/intents.py`:

```python
from __future__ import annotations

import json
import sqlite3
from datetime import datetime


def insert(conn: sqlite3.Connection, mission_id: int, payload: dict,
           now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO trade_intents (mission_id, payload_json, created_at) "
        "VALUES (?,?,?)",
        (mission_id, json.dumps(payload, ensure_ascii=False), now.isoformat()))
    conn.commit()
    return cur.lastrowid


def set_gate_result(conn: sqlite3.Connection, intent_id: int, *,
                    accepted: bool, reject_reason: str | None) -> None:
    conn.execute(
        "UPDATE trade_intents SET gate_result=?, reject_reason=? WHERE id=?",
        ("accepted" if accepted else "rejected", reject_reason, intent_id))
    conn.commit()


def get(conn: sqlite3.Connection, intent_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM trade_intents WHERE id=?",
                       (intent_id,)).fetchone()
    return dict(row) if row else None
```

`src/agentic_fx/store/missions.py`:

```python
from __future__ import annotations

import json
import sqlite3
from datetime import datetime


def start(conn: sqlite3.Connection, loop: str, runner: str, model: str,
          now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES (?,?,?,'running',?)", (loop, runner, model, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def finish(conn: sqlite3.Connection, mission_id: int, status: str,
           output: dict | None, transcript: list, now: datetime) -> None:
    conn.execute(
        "UPDATE missions SET status=?, output_json=?, transcript_json=?, "
        "finished_at=? WHERE id=?",
        (status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         json.dumps(transcript, ensure_ascii=False),
         now.isoformat(), mission_id))
    conn.commit()


def recent(conn: sqlite3.Connection, n: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM missions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/store -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/orders.py src/agentic_fx/store/intents.py \
  src/agentic_fx/store/missions.py tests/store
git commit -m "feat: 取引系 store CRUD (orders/intents/missions — 永続化のみ)"
```

---

### Task 8: 運用系 store (approvals / news_sources / backlog / improve_runs)

**Files:**
- Create: `src/agentic_fx/store/approvals.py`, `src/agentic_fx/store/news_sources.py`, `src/agentic_fx/store/backlog.py`, `src/agentic_fx/store/improve_runs.py`
- Test: `tests/store/test_approvals.py`, `tests/store/test_news_sources.py`, `tests/store/test_backlog.py`

**Interfaces:**
- Produces:
  - `approvals.create(conn, kind: str, payload: dict, now, expires_at: datetime | None = None) -> int` / `approvals.decide(conn, approval_id, *, status, decided_by, now, reason=None) -> None` (**pending 以外への decide は `AlreadyDecidedError`** — 冪等 409 相当、設計書 §7) / `approvals.pending(conn, kind=None) -> list[dict]` / `approvals.expire_due(conn, now) -> int` / `approvals.set_message_id(conn, approval_id, message_id) -> None` / 例外 `AlreadyDecidedError(Exception)`
  - `news_sources.add(conn, *, name, fetcher, url, added_by, now, enabled=False) -> int` / `news_sources.list_enabled(conn) -> list[dict]` / `news_sources.list_all(conn) -> list[dict]` / `news_sources.set_enabled(conn, source_id, enabled: bool) -> None` / 重複 URL は `sqlite3.IntegrityError`
  - `backlog.add(conn, idea: str, source: str, now) -> int` / `backlog.list_open(conn) -> list[dict]` / `backlog.set_status(conn, backlog_id, status: str, now) -> None`
  - `improve_runs.start(conn, backlog_id: int | None, now) -> int` / `improve_runs.finish(conn, run_id, *, result, now, pr_url=None, approval_id=None, report_path=None) -> None`

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_approvals.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import approvals
from agentic_fx.store.approvals import AlreadyDecidedError
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_create_and_decide(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "tech_plugin", {"path": "plugins/tech/rsi"}, NOW)
    assert len(approvals.pending(c)) == 1
    approvals.decide(c, aid, status="approved", decided_by="shell", now=NOW)
    assert approvals.pending(c) == []


def test_double_decide_rejected(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "news_source", {"url": "https://x"}, NOW)
    approvals.decide(c, aid, status="rejected", decided_by="shell", now=NOW,
                     reason="低品質")
    with pytest.raises(AlreadyDecidedError):
        approvals.decide(c, aid, status="approved", decided_by="shell", now=NOW)


def test_expire_due(tmp_path):
    c = _conn(tmp_path)
    approvals.create(c, "live_trade", {"pair": "USDJPY"}, NOW,
                     expires_at=NOW + timedelta(minutes=15))
    n = approvals.expire_due(c, NOW + timedelta(minutes=16))
    assert n == 1
    assert approvals.pending(c) == []


def test_pending_filter_by_kind(tmp_path):
    c = _conn(tmp_path)
    approvals.create(c, "tech_plugin", {}, NOW)
    approvals.create(c, "news_source", {}, NOW)
    assert len(approvals.pending(c, kind="tech_plugin")) == 1
```

`tests/store/test_news_sources.py`:

```python
from datetime import datetime, timezone

import pytest
import sqlite3

from agentic_fx.store import news_sources
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_add_and_enable(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    sid = news_sources.add(c, name="reuters-fx", fetcher="feed",
                           url="https://example.com/rss", added_by="user", now=NOW)
    assert news_sources.list_enabled(c) == []
    news_sources.set_enabled(c, sid, True)
    assert news_sources.list_enabled(c)[0]["name"] == "reuters-fx"
    with pytest.raises(sqlite3.IntegrityError):
        news_sources.add(c, name="dup", fetcher="feed",
                         url="https://example.com/rss", added_by="user", now=NOW)
```

`tests/store/test_backlog.py`:

```python
from datetime import datetime, timezone

from agentic_fx.store import backlog, improve_runs
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_backlog_and_run(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    bid = backlog.add(c, "ATR ベースの SL 幅", "user", NOW)
    assert backlog.list_open(c)[0]["idea"].startswith("ATR")
    backlog.set_status(c, bid, "selected", NOW)
    assert backlog.list_open(c) == []
    rid = improve_runs.start(c, bid, NOW)
    improve_runs.finish(c, rid, result="report", now=NOW,
                        report_path="reports/improve-2026-07-26.md")
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/store -v`
Expected: 新規テストが FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/store/approvals.py`:

```python
"""approval_requests CRUD。決定は冪等 (pending 以外への decide は拒否) — 設計書 §7。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime


class AlreadyDecidedError(Exception):
    pass


def create(conn: sqlite3.Connection, kind: str, payload: dict, now: datetime,
           expires_at: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, expires_at, created_at) "
        "VALUES (?,?,?,?)",
        (kind, json.dumps(payload, ensure_ascii=False),
         expires_at.isoformat() if expires_at else None, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def decide(conn: sqlite3.Connection, approval_id: int, *, status: str,
           decided_by: str, now: datetime, reason: str | None = None) -> None:
    cur = conn.execute(
        "UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, reason=? "
        "WHERE id=? AND status='pending'",
        (status, decided_by, now.isoformat(), reason, approval_id))
    conn.commit()
    if cur.rowcount == 0:
        raise AlreadyDecidedError(f"approval {approval_id} is not pending")


def pending(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    q = "SELECT * FROM approval_requests WHERE status='pending'"
    args: list = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def expire_due(conn: sqlite3.Connection, now: datetime) -> int:
    cur = conn.execute(
        "UPDATE approval_requests SET status='expired' "
        "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at < ?",
        (now.isoformat(),))
    conn.commit()
    return cur.rowcount


def set_message_id(conn: sqlite3.Connection, approval_id: int,
                   message_id: str) -> None:
    conn.execute("UPDATE approval_requests SET message_id=? WHERE id=?",
                 (message_id, approval_id))
    conn.commit()
```

`src/agentic_fx/store/news_sources.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime


def add(conn: sqlite3.Connection, *, name: str, fetcher: str, url: str,
        added_by: str, now: datetime, enabled: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO news_sources (name, fetcher, url, enabled, added_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (name, fetcher, url, int(enabled), added_by, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM news_sources ORDER BY id")]


def list_enabled(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM news_sources WHERE enabled=1 ORDER BY id")]


def set_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> None:
    conn.execute("UPDATE news_sources SET enabled=? WHERE id=?",
                 (int(enabled), source_id))
    conn.commit()
```

`src/agentic_fx/store/backlog.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime


def add(conn: sqlite3.Connection, idea: str, source: str, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO improvement_backlog (idea, source, created_at, updated_at) "
        "VALUES (?,?,?,?)", (idea, source, now.isoformat(), now.isoformat()))
    conn.commit()
    return cur.lastrowid


def list_open(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM improvement_backlog WHERE status='open' ORDER BY id")]


def set_status(conn: sqlite3.Connection, backlog_id: int, status: str,
               now: datetime) -> None:
    conn.execute(
        "UPDATE improvement_backlog SET status=?, updated_at=? WHERE id=?",
        (status, now.isoformat(), backlog_id))
    conn.commit()
```

`src/agentic_fx/store/improve_runs.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime


def start(conn: sqlite3.Connection, backlog_id: int | None, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO improvement_runs (backlog_id, started_at) VALUES (?,?)",
        (backlog_id, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def finish(conn: sqlite3.Connection, run_id: int, *, result: str, now: datetime,
           pr_url: str | None = None, approval_id: int | None = None,
           report_path: str | None = None) -> None:
    conn.execute(
        "UPDATE improvement_runs SET result=?, pr_url=?, approval_id=?, "
        "report_path=?, finished_at=? WHERE id=?",
        (result, pr_url, approval_id, report_path, now.isoformat(), run_id))
    conn.commit()
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/store -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/approvals.py src/agentic_fx/store/news_sources.py \
  src/agentic_fx/store/backlog.py src/agentic_fx/store/improve_runs.py tests/store
git commit -m "feat: 運用系 store CRUD (approvals 冪等決定・news_sources・backlog)"
```

---

### Task 9: 記録系 store (reflections / snapshots / econ_events / ohlcv)

**Files:**
- Create: `src/agentic_fx/store/reflections.py`, `src/agentic_fx/store/snapshots.py`, `src/agentic_fx/store/econ_events.py`, `src/agentic_fx/store/ohlcv.py`
- Test: `tests/store/test_records.py`

**Interfaces:**
- Produces:
  - `reflections.save(conn, order_id: int, content: str, now) -> None` (UPSERT) / `reflections.get(conn, order_id) -> dict | None` / `reflections.recent(conn, n: int) -> list[dict]`
  - `snapshots.add(conn, *, ts, balance, equity, hwm, cashflow=0.0, source="paper") -> int` / `snapshots.latest(conn) -> dict | None` (HWM の計算はプラン 2 — ここは保存のみ)
  - `econ_events.upsert(conn, *, ts, country, name, importance, actual=None, forecast=None, previous=None) -> None` / `econ_events.upcoming(conn, now, hours: int) -> list[dict]`
  - `ohlcv.upsert_bars(conn, bars: list[Bar]) -> int` / `ohlcv.load_bars(conn, symbol, interval, since: datetime | None = None) -> list[Bar]` (contracts.Bar を返す)

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_records.py`:

```python
from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar, OrderStatus
from agentic_fx.store import econ_events, ohlcv, orders, reflections, snapshots
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_reflection_upsert(tmp_path):
    c = _conn(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="day", status=OrderStatus.CLOSED, now=NOW)
    reflections.save(c, oid, "v1", NOW)
    reflections.save(c, oid, "v2", NOW)
    assert reflections.get(c, oid)["content"] == "v2"
    assert len(reflections.recent(c, 5)) == 1


def test_snapshots_latest(tmp_path):
    c = _conn(tmp_path)
    snapshots.add(c, ts=NOW, balance=10000, equity=10000, hwm=10000)
    snapshots.add(c, ts=NOW + timedelta(hours=1), balance=10000,
                  equity=10100, hwm=10100)
    assert snapshots.latest(c)["equity"] == 10100


def test_econ_upsert_and_upcoming(tmp_path):
    c = _conn(tmp_path)
    ts = NOW + timedelta(hours=3)
    econ_events.upsert(c, ts=ts, country="US", name="CPI", importance=3)
    econ_events.upsert(c, ts=ts, country="US", name="CPI", importance=3,
                       actual="3.1%")  # 上書き
    rows = econ_events.upcoming(c, NOW, hours=24)
    assert len(rows) == 1 and rows[0]["actual"] == "3.1%"
    assert econ_events.upcoming(c, NOW + timedelta(days=2), hours=24) == []


def test_ohlcv_roundtrip(tmp_path):
    c = _conn(tmp_path)
    bars = [Bar("USDJPY", "1h", NOW + timedelta(hours=i),
                148.0, 148.5, 147.9, 148.2, 1000) for i in range(3)]
    assert ohlcv.upsert_bars(c, bars) == 3
    ohlcv.upsert_bars(c, bars)  # 冪等
    loaded = ohlcv.load_bars(c, "USDJPY", "1h")
    assert len(loaded) == 3
    assert loaded[0].close == 148.2
    assert len(ohlcv.load_bars(c, "USDJPY", "1h",
                               since=NOW + timedelta(hours=2))) == 1
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/store/test_records.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/store/reflections.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime


def save(conn: sqlite3.Connection, order_id: int, content: str,
         now: datetime) -> None:
    conn.execute(
        "INSERT INTO reflections (order_id, content, created_at) VALUES (?,?,?) "
        "ON CONFLICT(order_id) DO UPDATE SET content=excluded.content",
        (order_id, content, now.isoformat()))
    conn.commit()


def get(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM reflections WHERE order_id=?",
                       (order_id,)).fetchone()
    return dict(row) if row else None


def recent(conn: sqlite3.Connection, n: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?", (n,))]
```

`src/agentic_fx/store/snapshots.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime


def add(conn: sqlite3.Connection, *, ts: datetime, balance: float, equity: float,
        hwm: float, cashflow: float = 0.0, source: str = "paper") -> int:
    cur = conn.execute(
        "INSERT INTO account_snapshots (ts, balance, equity, hwm, cashflow, source) "
        "VALUES (?,?,?,?,?,?)",
        (ts.isoformat(), balance, equity, hwm, cashflow, source))
    conn.commit()
    return cur.lastrowid


def latest(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
```

`src/agentic_fx/store/econ_events.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


def upsert(conn: sqlite3.Connection, *, ts: datetime, country: str, name: str,
           importance: int, actual: str | None = None,
           forecast: str | None = None, previous: str | None = None) -> None:
    conn.execute(
        "INSERT INTO econ_events (ts, country, name, importance, actual, "
        "forecast, previous) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(ts, country, name) DO UPDATE SET importance=excluded.importance, "
        "actual=excluded.actual, forecast=excluded.forecast, previous=excluded.previous",
        (ts.isoformat(), country, name, importance, actual, forecast, previous))
    conn.commit()


def upcoming(conn: sqlite3.Connection, now: datetime, hours: int) -> list[dict]:
    end = now + timedelta(hours=hours)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM econ_events WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (now.isoformat(), end.isoformat()))]
```

`src/agentic_fx/store/ohlcv.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime

from agentic_fx.core.contracts import Bar


def upsert_bars(conn: sqlite3.Connection, bars: list[Bar]) -> int:
    conn.executemany(
        "INSERT INTO ohlcv (symbol, interval, bar_time, open, high, low, close, "
        "volume) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, bar_time) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(b.symbol, b.interval, b.ts.isoformat(), b.open, b.high, b.low,
          b.close, b.volume) for b in bars])
    conn.commit()
    return len(bars)


def load_bars(conn: sqlite3.Connection, symbol: str, interval: str,
              since: datetime | None = None) -> list[Bar]:
    q = "SELECT * FROM ohlcv WHERE symbol=? AND interval=?"
    args: list = [symbol, interval]
    if since is not None:
        q += " AND bar_time >= ?"
        args.append(since.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/store -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/reflections.py src/agentic_fx/store/snapshots.py \
  src/agentic_fx/store/econ_events.py src/agentic_fx/store/ohlcv.py tests/store
git commit -m "feat: 記録系 store CRUD (reflections/snapshots/econ/ohlcv)"
```

---

### Task 10: 状態ストア (store/state.py — mode / autopilot)

**Files:**
- Create: `src/agentic_fx/store/state.py`
- Test: `tests/store/test_state.py`

**Interfaces:**
- Produces: dataclass `AppState(initialized: bool, mode: Mode, autopilot: bool, kill_switch_latched: bool)`; class `StateStore(path: Path)` — `.load() -> AppState` (ファイル無しならデフォルト: `initialized=False, mode=learning, autopilot=False, kill_switch_latched=False`)、`.save(state: AppState) -> None` (**atomic write: tmp + os.replace** — 設計書 §10 StateStore 踏襲)、`.update(**changes) -> AppState`
- **settings.yaml にはモード・autopilot を置かない** (設計書 §3 の構造的担保はこの分離で実現)

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_state.py`:

```python
import json

from agentic_fx.core.contracts import Mode
from agentic_fx.store.state import AppState, StateStore


def test_defaults_when_missing(tmp_path):
    s = StateStore(tmp_path / "state" / "app_state.json").load()
    assert s == AppState(initialized=False, mode=Mode.LEARNING,
                         autopilot=False, kill_switch_latched=False)


def test_update_roundtrip(tmp_path):
    store = StateStore(tmp_path / "app_state.json")
    store.update(initialized=True)
    s2 = StateStore(tmp_path / "app_state.json").load()
    assert s2.initialized is True
    assert s2.mode is Mode.LEARNING


def test_atomic_write_no_partial_file(tmp_path):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    store.update(initialized=True, autopilot=True)
    data = json.loads(path.read_text())
    assert data["autopilot"] is True
    assert not list(tmp_path.glob("*.tmp"))


def test_unknown_field_rejected(tmp_path):
    store = StateStore(tmp_path / "app_state.json")
    try:
        store.update(no_such_field=1)
        assert False, "should raise"
    except TypeError:
        pass
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/store/test_state.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/store/state.py`:

```python
"""稼働モード・autopilot 等の状態。config でなくここに置く (設計書 §3 の構造的担保)。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from agentic_fx.core.contracts import Mode


@dataclass(frozen=True, slots=True)
class AppState:
    initialized: bool = False
    mode: Mode = Mode.LEARNING
    autopilot: bool = False
    kill_switch_latched: bool = False


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> AppState:
        if not self._path.exists():
            return AppState()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return AppState(initialized=bool(raw["initialized"]),
                        mode=Mode(raw["mode"]),
                        autopilot=bool(raw["autopilot"]),
                        kill_switch_latched=bool(raw["kill_switch_latched"]))

    def save(self, state: AppState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(state)
        d["mode"] = state.mode.value
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self._path)

    def update(self, **changes) -> AppState:
        valid = {f.name for f in fields(AppState)}
        unknown = set(changes) - valid
        if unknown:
            raise TypeError(f"unknown state fields: {unknown}")
        state = replace(self.load(), **changes)
        self.save(state)
        return state
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/store/test_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/state.py tests/store/test_state.py
git commit -m "feat: 状態ストア (mode/autopilot、atomic write、config 非依存)"
```

---

### Task 11: init + 起動ガード (entry.py / service.py / main.py)

**Files:**
- Create: `src/agentic_fx/entry.py` (argparse エントリ)、`src/agentic_fx/service.py` (init 実行と起動ガード)
- Modify: `main.py` (uv init スタブを置き換え)
- Test: `tests/test_init_and_guard.py`

**Interfaces:**
- Produces:
  - `service.run_init(root: Path) -> int` — ①`config/settings.yaml` が無ければ example をコピー (**既存ファイルは決して上書きしない** — 冪等) ②`data/` `logs/` 作成 ③DB 初期化 (11 テーブル) ④state: **現在 mode が learning (または state 未作成) の場合のみ `initialized=True, mode=learning, autopilot=False` を明示保存。現在 mode=trading の場合は mode/autopilot を変更せず `initialized=True` のみ更新し警告を表示** — learning への切替はモード遷移ガード (§3、Phase 3 実装) を通る `mode` コマンドだけの仕事であり、init をガード迂回路にしない ⑤activity SYSTEM に `init_completed` を実際の mode で記録。戻り値は exit code (0 成功)
  - `service.ensure_initialized(root: Path) -> None` — 未 init なら **`SystemExit(2)`** with メッセージ「`uv run main.py init` を先に実行」
  - `service.run_service(root: Path) -> int` — 本プランでは**起動ガード通過後に「サービス本体はプラン 5 で実装」と表示して exit 0** するスタブ (ガード自体は本物)
  - `entry.main(argv: list[str] | None = None) -> int` — `init` / 引数なし (= run_service)。`[project.scripts] afx` と `main.py` の両方から呼ばれる。init は非対話・冪等 (プロンプトなし。対話的な接続確認はプラン 3/5 で追加)
  - パス規約: `root` 直下に `config/` `data/` `logs/` `policy/`。DB は `data/agentic.db`、state は `data/state/app_state.json`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_init_and_guard.py`:

```python
import pytest

from agentic_fx.entry import main as entry_main
from agentic_fx.service import ensure_initialized, run_init
from agentic_fx.store.db import connect
from agentic_fx.store.state import StateStore


def _example(root):
    (root / "config").mkdir(parents=True)
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (root / "config" / "settings.yaml.example").write_text(src)


def test_guard_blocks_before_init(tmp_path):
    _example(tmp_path)
    with pytest.raises(SystemExit) as e:
        ensure_initialized(tmp_path)
    assert e.value.code == 2


def test_init_creates_everything(tmp_path):
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    assert (tmp_path / "config" / "settings.yaml").exists()
    assert (tmp_path / "data" / "agentic.db").exists()
    conn = connect(tmp_path / "data" / "agentic.db")
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "orders" in tables
    state = StateStore(tmp_path / "data" / "state" / "app_state.json").load()
    assert state.initialized is True
    assert state.mode.value == "learning"
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "init_completed" in act
    ensure_initialized(tmp_path)  # ガード通過 (例外なし)


def test_init_is_idempotent(tmp_path):
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    marker = tmp_path / "config" / "settings.yaml"
    marker.write_text(marker.read_text() + "\n# user edit\n")
    assert run_init(tmp_path) == 0
    assert "# user edit" in marker.read_text()  # 既存 settings.yaml を上書きしない


def test_reinit_in_trading_keeps_mode(tmp_path):
    # init をモード遷移ガード (§3) の迂回路にしない: trading 中は mode/autopilot 不変
    from agentic_fx.core.contracts import Mode
    _example(tmp_path)
    run_init(tmp_path)
    store = StateStore(tmp_path / "data" / "state" / "app_state.json")
    store.update(mode=Mode.TRADING, autopilot=True)
    run_init(tmp_path)
    s = store.load()
    assert s.mode is Mode.TRADING
    assert s.autopilot is True
    assert s.initialized is True


def test_entry_init_subcommand(tmp_path, monkeypatch):
    _example(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert entry_main(["init"]) == 0


def test_entry_default_requires_init(tmp_path, monkeypatch):
    _example(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        entry_main([])
    assert e.value.code == 2
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_init_and_guard.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/service.py`:

```python
"""起動シーケンス: init ウィザードと起動ガード — 設計書 §8。
本プランのスコープは基盤部分のみ (価格ソース接続確認はプラン 3、llama-swap 確認はプラン 5 で追加)。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Mode
from agentic_fx.logging_setup import setup_technical_logging
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore


def _state_store(root: Path) -> StateStore:
    return StateStore(root / "data" / "state" / "app_state.json")


def ensure_initialized(root: Path) -> None:
    if not _state_store(root).load().initialized:
        print("初期化が完了していません。先に `uv run main.py init` を実行してください。",
              file=sys.stderr)
        raise SystemExit(2)


def run_init(root: Path) -> int:
    cfg_dir = root / "config"
    example = cfg_dir / "settings.yaml.example"
    target = cfg_dir / "settings.yaml"
    if not example.exists():
        print(f"settings.yaml.example が見つかりません: {example}", file=sys.stderr)
        return 1
    if not target.exists():
        shutil.copy(example, target)
        print(f"作成: {target} (必要に応じて編集してください)")
    settings = load_settings(target)  # 検証を兼ねる

    (root / "logs").mkdir(parents=True, exist_ok=True)
    setup_technical_logging(root / "logs", settings.logging.level)

    conn = connect(root / "data" / "agentic.db")
    init_db(conn)

    # learning への切替はモード遷移ガード (§3) を通る mode コマンドのみ。
    # init はガードの迂回路にしない: trading 中は mode/autopilot に触れない。
    store = _state_store(root)
    if store.load().mode is Mode.TRADING:
        state = store.update(initialized=True)
        print("警告: 現在 trading モードです。init は mode/autopilot を変更しません "
              "(切替は mode コマンドを使用)。")
    else:
        state = store.update(initialized=True, mode=Mode.LEARNING,
                             autopilot=False)

    ActivityLog(root / "logs" / "activity.log").write(
        Category.SYSTEM, "init_completed",
        f"pairs={settings.pairs} mode={state.mode.value} "
        f"autopilot={'on' if state.autopilot else 'off'}")
    print("初期化が完了しました。`uv run main.py` でサービスを起動できます。")
    return 0


def run_service(root: Path) -> int:
    ensure_initialized(root)
    # サービス本体 (scheduler / 対話シェル) はプラン 5 で実装する。
    print("起動ガードを通過しました。サービス本体は Phase 1 プラン 5 で実装されます。")
    return 0
```

`src/agentic_fx/entry.py`:

```python
"""main.py と [project.scripts] afx の共通エントリ。"""
from __future__ import annotations

import argparse
from pathlib import Path

from agentic_fx import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afx", description="agentic-fx")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="初期設定 (非対話・冪等。既存 settings.yaml は上書きしない)")
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "init":
        return service.run_init(root)
    return service.run_service(root)


if __name__ == "__main__":
    raise SystemExit(main())
```

`main.py` (全置換):

```python
from agentic_fx.entry import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_init_and_guard.py -v`
Expected: PASS

- [ ] **Step 5: 手動確認**

```bash
uv run main.py            # → exit 2 (init 未完了メッセージ)
uv run main.py init       # → 初期化完了
uv run main.py            # → ガード通過メッセージ
git status                # data/ logs/ config/settings.yaml が untracked に出ないこと (gitignore 確認)
```

- [ ] **Step 6: Commit**

```bash
git add src/agentic_fx/entry.py src/agentic_fx/service.py main.py \
  tests/test_init_and_guard.py
git commit -m "feat: init ウィザード (基盤部分) + 起動ガード"
```

---

### Task 12: 仕上げ (全体テスト + セルフレビュー)

- [ ] **Step 1: 全テスト実行**

Run: `uv run pytest -v`
Expected: 全件 PASS

- [ ] **Step 2: セルフレビューチェック**

- 設計書 §12 の 11 テーブルすべてに CRUD が存在するか (ohlcv 含む)
- `OrderStatus` が設計書 §12 遷移図の 16 状態と一致するか
- settings.yaml.example のキーと `Settings` モデルが 1:1 か
- `core/` から LLM 関連 import がないか: `grep -rn "openai\|anthropic\|claude\|llama" src/agentic_fx/core/` が空
- kill switch 無効化キーが存在しないか: `grep -rn "kill" config/settings.yaml.example` が閾値のみ

- [ ] **Step 3: 最終 Commit (残差分があれば)**

```bash
git add -A ':!data' ':!logs' ':!config/settings.yaml'
git commit -m "chore: プラン 1 完了 — 基盤 + 共有契約"
```

---

## プラン 2 への引き継ぎ事項

- 状態遷移の許可判定は `core/` に新設する (store は今後も無検証 CRUD のまま)
- `TradeIntent` / `Quote` / `InstrumentSpec` / `AccountState` / `Clock` は contracts.py のものを使う。risk gate / sizing はこれらを入力に取る純関数として実装する
- `snapshots.add` に渡す `hwm` の計算 (入出金調整済み HWM) はプラン 2 の責務
- `orders.update_fields` の呼び出し元はプラン 2 の executor / 状態機械のみとする
- プラン 5: `setup_technical_logging` に `daemon: bool = False` を追加し、--daemon 時のみ stderr StreamHandler (journald 経路)。モード遷移ガード (§3) は Phase 3 プランで `mode` コマンドとして実装 (init は今後もガードを迂回しない)
