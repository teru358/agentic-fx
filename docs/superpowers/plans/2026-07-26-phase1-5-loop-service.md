# Phase 1 プラン 5: trade loop + service/shell 統合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 取引判断 loop・reflection・policy 注入・対話シェル・サービス配線を統合し、**Phase 1 完成 = 学習モードでのフル自走** (FakeRunner で LLM なし E2E 検証可能) を達成する。

**Architecture:** `loops/` に trade_loop / reflection_cycle / prompts (.md)、`policy.py`、`commands.py` + `shell.py`、`service.py` の本実装 (build_app 配線 + scheduler スレッド + graceful shutdown)。Mission 実行は**常にサービスプロセス内の単一 Mission スロット (threading.Lock)**。

**Tech Stack:** stdlib (threading, signal) / プラン 1〜4 の契約を消費

## Global Constraints (設計書より)

- **ask Mission は回答専用** — output_schema に TradeIntent を渡さない。TradeIntent を生成できるのは scheduler 起動の定期 Mission のみ (origin 検証は executor 済み) (設計書 §5)
- Mission プロンプト構成: ①固定システム指示 + policy 末尾 4000 文字 ②状態サマリ (決定論的生成) ③ワンショット (臨時時のみ) (設計書 §5)
- **fail closed**: 全価格ソース不健全なら Mission を実行しない (設計書 §5)
- hold を含む**全 Mission・全 intent・transcript を記録** (設計書 §12/§13)
- 対話シェルは静かに (ログを流さない)。ログは `log` / `activity` の pull 型 (設計書 §8)
- `--daemon` 時のみ技術ログを stderr にも出す (journald 経路、プラン 1 の引き継ぎ)
- `policy/directives.md` は読み込み + サイズ警告のみ (`policy add` コマンドは Phase 2) (設計書 §15)
- prompts はコード外の .md (設計書 §11)
- graceful shutdown: スケジューラ停止 → 実行中 Mission の完了待ち (タイムアウト付き) → 終了 (設計書 §8)
- kill switch ラッチの解除は人間の明示操作 — シェル `killswitch reset` (プラン 2 の引き継ぎ)
- コミットメッセージ末尾: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

**消費する契約** (変更禁止): プラン 2 `Executor.handle_intent / close_order`、`Scheduler(tick, on_trade_mission, on_news_cycle, bars_fn)`、プラン 3 `PriceProvider / NewsCollector / EconCalendar / Rag`、プラン 4 `Mission / MissionResult / AgentRunner / FakeRunner / LocalRunner / ToolRegistry / 各 build()`

---

### Task 1: DB 接続のスレッド対応 + prompts + policy.py

**Files:**
- Modify: `src/agentic_fx/store/db.py`
- Create: `src/agentic_fx/loops/__init__.py`, `src/agentic_fx/loops/prompts/trade_mission.md`, `src/agentic_fx/loops/prompts/ask_mission.md`, `src/agentic_fx/loops/prompts/reflection.md`
- Create: `src/agentic_fx/policy.py`
- Test: `tests/test_policy.py`, `tests/store/test_db.py` (追記)

**Interfaces:**
- Produces:
  - `db.connect(db_path, *, check_same_thread: bool = False)` — シェルスレッドと scheduler スレッドが同一 conn を共有するため。書き込みの直列化は Mission スロット + 各 CRUD の即時 commit + WAL で担保
  - `class Policy(path: Path)`: `.tail(chars: int = 4000) -> str` (ファイル無しは空文字)、`.size_warning(limit_chars: int = 16000) -> str | None` (超過時に警告文)
  - `load_prompt(name: str) -> str` — `loops/prompts/<name>.md` を読む (`loops/prompts.py` 内)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_policy.py`:

```python
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.policy import Policy


def test_tail_missing_file_empty(tmp_path):
    assert Policy(tmp_path / "directives.md").tail() == ""


def test_tail_returns_last_chars(tmp_path):
    p = tmp_path / "directives.md"
    p.write_text("A" * 5000, encoding="utf-8")
    assert len(Policy(p).tail(4000)) == 4000


def test_size_warning(tmp_path):
    p = tmp_path / "directives.md"
    p.write_text("A" * 20000, encoding="utf-8")
    assert Policy(p).size_warning() is not None
    p.write_text("short", encoding="utf-8")
    assert Policy(p).size_warning() is None


def test_prompts_exist():
    for name in ("trade_mission", "ask_mission", "reflection"):
        text = load_prompt(name)
        assert len(text) > 100
```

`tests/store/test_db.py` に追記:

```python
def test_connection_usable_across_threads(tmp_path):
    import threading
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    errors = []

    def use():
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert errors == []
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_policy.py tests/store/test_db.py -v`
Expected: 新規分 FAIL

- [ ] **Step 3: 実装**

`src/agentic_fx/store/db.py` の `connect` を変更:

```python
def connect(db_path: Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

`src/agentic_fx/policy.py`:

```python
"""policy/directives.md — 末尾 4000 文字注入 + サイズ警告 (設計書 §8)。追記は Phase 2。"""
from __future__ import annotations

from pathlib import Path


class Policy:
    def __init__(self, path: Path) -> None:
        self._path = path

    def tail(self, chars: int = 4000) -> str:
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")[-chars:]

    def size_warning(self, limit_chars: int = 16000) -> str | None:
        if self._path.exists() and \
                len(self._path.read_text(encoding="utf-8")) > limit_chars:
            return (f"警告: {self._path} が {limit_chars} 文字を超えています。"
                    "注入は末尾 4000 文字のみです。手動で整理してください。")
        return None
```

`src/agentic_fx/loops/prompts_loader.py`:

```python
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")
```

`src/agentic_fx/loops/prompts/trade_mission.md`:

```markdown
あなたは FX 取引判断エージェントです。1 時間毎に呼び出され、現在の市場状況を
ツールで調査して、次のいずれか 1 つを JSON で出力します。

## 判断の原則
- 情報軸は 2 つ: ニュース (search_news) とテクニカル (get_indicators / get_ohlcv)。
  必ず両方を確認してから判断すること
- 過去の振り返り (get_recent_reflections / search_reflections) から同じ失敗を
  繰り返さないこと
- 経済指標発表 (get_econ_calendar) の直前は新規エントリーを避けること
- 確信が持てないときは hold を選ぶこと。hold も立派な判断であり記録される
- 現在ポジション・未約定指値は get_positions で確認し、order_id を使って
  close (裁量クローズ) / cancel (指値取消) を提案できる

## 出力形式 (JSON のみ。説明文を JSON の外に書かない)
{"action": "open", "pair": "USDJPY", "direction": "long|short",
 "entry_type": "market|limit", "horizon": "day|swing",
 "limit_price": 148.20, "expires_in": "4h",
 "stop_loss": 147.80, "take_profit": 149.00,
 "confidence": 0.0-1.0, "reasoning": "判断根拠"}
または
{"action": "close", "order_id": 12, "reasoning": "..."}
{"action": "cancel", "order_id": 12, "reasoning": "..."}
{"action": "hold", "reasoning": "..."}

## 制約 (システム側で強制される)
- open には stop_loss / take_profit / horizon が必須
- 数量はあなたは決めない (システムが決定論的に算出する)
- リスク管理ルール (RR・SL 距離・ポジション数・損失上限) に違反する提案は
  自動的に却下される。reasoning に却下理由が返るので次回の判断に活かすこと
- horizon=day は当日クローズ前に強制決済、swing はリスク半分で数量が計算される
```

`src/agentic_fx/loops/prompts/ask_mission.md`:

```markdown
あなたは FX トレーディングシステムのアナリストです。ユーザーの質問に、
ツールで現在の市場状況・ポジション・ニュースを調査して日本語で答えてください。

重要: あなたはこの Mission では取引を実行できません。発注・クローズ・取消の
提案は次回の定期判断で扱われます。永続的な方針はユーザーが policy に記録します。

## 出力形式 (JSON のみ)
{"answer": "回答本文"}
```

`src/agentic_fx/loops/prompts/reflection.md`:

```markdown
クローズされたトレードの振り返りを書いてください。以下を簡潔に (200-400 字):
- 何を根拠にエントリーしたか (記録された reasoning から)
- 結果とその要因 (SL/TP/裁量/強制のどれで閉じたか、想定と何が違ったか)
- 次回に活かす教訓 (同じ局面で何を変えるか)

## 出力形式 (JSON のみ)
{"content": "振り返り本文"}
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_policy.py tests/store/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/db.py src/agentic_fx/policy.py \
  src/agentic_fx/loops tests/test_policy.py tests/store/test_db.py
git commit -m "feat: policy 注入・prompts (.md)・DB スレッド対応"
```

---

### Task 2: 状態サマリ + Mission スキーマ (loops/summary.py)

**Files:**
- Create: `src/agentic_fx/loops/summary.py`
- Test: `tests/loops/__init__.py`, `tests/loops/test_summary.py`

**Interfaces:**
- Produces:
  - `TRADE_INTENT_SCHEMA: dict` — `{"type": "object", "properties": {"action": {"enum": ["open", "close", "cancel", "hold"]}, ...}, "required": ["action", "reasoning"]}` (構造の最終防衛は `TradeIntent.from_llm_dict` — スキーマは LLM への再出力誘導用に緩めでよい)
  - `ANSWER_SCHEMA: dict` — `{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}`
  - `build_state_summary(conn, broker, econ, clock) -> str` — 設計書 §5 ②: 残高・エクイティ / 累計・日次 P&L / 現在ポジション + 未約定指値 (order_id・horizon 付き) / 直近 10 件のトレード 1 行要約 (ペア/方向/損益/クローズ理由) / 24h 以内の経済指標

- [ ] **Step 1: 失敗するテストを書く**

`tests/loops/test_summary.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops.summary import (
    ANSWER_SCHEMA, TRADE_INTENT_SCHEMA, build_state_summary,
)
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_schemas_validate():
    jsonschema.validate({"action": "hold", "reasoning": "wait"},
                        TRADE_INTENT_SCHEMA)
    jsonschema.validate({"answer": "回答"}, ANSWER_SCHEMA)
    try:
        jsonschema.validate({"action": "buy"}, TRADE_INTENT_SCHEMA)
        raise AssertionError("should fail")
    except jsonschema.ValidationError:
        pass


def test_summary_contains_positions_and_trades(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                  horizon="swing", status=OrderStatus.PENDING_FILL, now=NOW,
                  quantity=0.1, requested_price=148.2, stop_loss=147.8,
                  take_profit=149.0)
    oid = orders.insert(conn, pair="EURUSD", direction="short",
                        entry_type="market", horizon="day",
                        status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
                        realized_pnl=-1200.0, close_reason="sl")
    econ = MagicMock()
    econ.upcoming.return_value = [
        {"ts": "2026-07-22T19:30:00+00:00", "country": "USD",
         "name": "CPI", "importance": 3}]
    text = build_state_summary(conn, PaperBroker(conn, SETTINGS,
                                                 FixedClock(NOW)),
                               econ, FixedClock(NOW))
    assert "USDJPY" in text and "swing" in text      # 未約定指値 + horizon
    assert f"#{oid}" in text or str(oid) in text      # order_id 提示
    assert "sl" in text and "-1200" in text           # 直近トレード要約
    assert "CPI" in text                              # 経済指標
    assert "1,000,000" in text or "1000000" in text   # 残高
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/loops/test_summary.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/loops/summary.py`:

```python
"""状態サマリ (常時注入) と Mission 出力スキーマ — 設計書 §5。決定論的コードが生成。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from agentic_fx.core.contracts import Clock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.accounting import daily_start_equity
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.store import orders

TRADE_INTENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"enum": ["open", "close", "cancel", "hold"]},
        "pair": {"type": "string"},
        "direction": {"enum": ["long", "short"]},
        "entry_type": {"enum": ["market", "limit"]},
        "horizon": {"enum": ["day", "swing"]},
        "order_id": {"type": "integer"},
        "limit_price": {"type": ["number", "null"]},
        "expires_in": {"type": ["string", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "confidence": {"type": ["number", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
}

ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}

_ACTIVE = (OrderStatus.OPEN, OrderStatus.PENDING_FILL,
           OrderStatus.PROTECTION_PENDING)


def build_state_summary(conn: sqlite3.Connection, broker: PaperBroker,
                        econ: EconCalendar, clock: Clock) -> str:
    now = clock.now()
    balance, equity = broker.equity()
    day_start = daily_start_equity(conn, now)
    daily_pnl = (equity - day_start) if day_start else 0.0

    lines = ["## 現在の状態 (システム生成)",
             f"- 時刻: {now.isoformat()}",
             f"- 残高: {balance:,.0f} / エクイティ: {equity:,.0f} "
             f"/ 日次損益: {daily_pnl:+,.0f}"]

    active = orders.list_by_status(conn, *_ACTIVE)
    if active:
        lines.append("- ポジション/指値:")
        for r in active:
            entry = r["avg_fill_price"] or r["requested_price"]
            lines.append(
                f"  - #{r['id']} {r['pair']} {r['direction']} "
                f"{r['status']} {r['quantity']}lot @{entry} "
                f"SL={r['stop_loss']} TP={r['take_profit']} "
                f"horizon={r['horizon']}")
    else:
        lines.append("- ポジション/指値: なし")

    closed = conn.execute(
        "SELECT * FROM orders WHERE status='closed' "
        "ORDER BY closed_at DESC LIMIT 10").fetchall()
    if closed:
        lines.append("- 直近トレード:")
        for r in closed:
            lines.append(
                f"  - #{r['id']} {r['pair']} {r['direction']} "
                f"pnl={r['realized_pnl']:+,.0f} ({r['close_reason']})")

    events = econ.upcoming(hours=24)
    if events:
        lines.append("- 24h 以内の経済指標:")
        for e in events[:8]:
            stars = "★" * int(e["importance"] or 0)
            lines.append(f"  - {e['ts']} {e['country']} {e['name']} {stars}")
    return "\n".join(lines)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/loops/test_summary.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/loops/summary.py tests/loops
git commit -m "feat: 状態サマリ生成 + Mission 出力スキーマ (intent/answer 分離)"
```

---

### Task 3: 取引判断 loop (loops/trade_loop.py)

**Files:**
- Create: `src/agentic_fx/loops/trade_loop.py`
- Test: `tests/loops/test_trade_loop.py`

**Interfaces:**
- Produces: `class TradeLoop`:
  - `__init__(self, *, conn, runner: AgentRunner, settings: Settings, executor: Executor, provider: PriceProvider, econ: EconCalendar, policy: Policy, activity: ActivityLog, notifier: Notifier, clock: Clock)`
  - `run_once(self) -> dict | None` — 定期 Mission (scheduler の `on_trade_mission` に差し込む):
    1. **fail closed**: `provider.healthcheck(settings.pairs[0])` が `DataUnhealthy` → activity SYSTEM `data_unhealthy` + notifier + **Mission を実行せず None**
    2. prompt = `load_prompt("trade_mission")` + policy.tail(4000) + `build_state_summary(...)`
    3. `Mission(tools=[get_ohlcv, get_indicators, search_news, get_econ_calendar, get_positions, get_account, get_recent_reflections, search_reflections], output_schema=TRADE_INTENT_SCHEMA, max_turns/timeout=settings.llama_swap)`
    4. `missions.start(loop="trade")` → `runner.run` → **必ず `missions.finish`** (status・output・transcript)
    5. runner 失敗 (completed 以外) → activity AGGREGATE `mission_failed` + notifier + None
    6. `TradeIntent.from_llm_dict(output, origin=Origin.SCHEDULER)` — `IntentParseError` → missions は completed のまま activity AGGREGATE `intent_parse_failed` + None
    7. `executor.handle_intent(intent, mid)` → activity AGGREGATE `decision` (result 要約) → 戻り値
  - `ask_once(self, question: str) -> str` — 臨時 Mission (シェル/API の ask):
    - prompt = `load_prompt("ask_mission")` + policy + サマリ + `\n## ユーザーの質問\n{question}`
    - `output_schema=ANSWER_SCHEMA` (**TradeIntent 不可 — 発注経路遮断**)、loop="ask" で記録
    - 成功時 answer 文字列、失敗時はエラーメッセージ文字列を返す (シェルにそのまま表示)

- [ ] **Step 1: 失敗するテストを書く**

`tests/loops/test_trade_loop.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import FixedClock, InstrumentSpec, Quote
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.loops.trade_loop import TradeLoop
from agentic_fx.policy import Policy
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000)
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


def _loop(tmp_path, results, healthy=True):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    clock = FixedClock(NOW)
    broker = PaperBroker(conn, SETTINGS, clock)
    executor = Executor(conn=conn, broker=broker, settings=SETTINGS,
                        state_store=StateStore(tmp_path / "s.json"),
                        activity=ActivityLog(tmp_path / "a.log"), clock=clock,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPEC)
    provider = MagicMock()
    if healthy:
        provider.healthcheck.return_value = "yfinance"
    else:
        provider.healthcheck.side_effect = DataUnhealthy("all down")
    econ = MagicMock()
    econ.upcoming.return_value = []
    runner = FakeRunner(results)
    policy_path = tmp_path / "directives.md"
    policy_path.write_text("USDJPY は月末は控えめに", encoding="utf-8")
    loop = TradeLoop(conn=conn, runner=runner, settings=SETTINGS,
                     executor=executor, provider=provider, econ=econ,
                     policy=Policy(policy_path),
                     activity=ActivityLog(tmp_path / "a.log"),
                     notifier=Notifier(enabled=False, webhook_url=None),
                     clock=clock)
    return conn, loop, runner, tmp_path


def test_hold_mission_recorded(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "様子見"}, [])])
    out = loop.run_once()
    assert out["result"] == "hold"
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["loop"] == "trade" and m["status"] == "completed"
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 1
    # prompt に policy とサマリが注入されている
    prompt = runner.missions[0].prompt
    assert "月末は控えめに" in prompt
    assert "現在の状態" in prompt


def test_fail_closed_on_unhealthy_data(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [], healthy=False)
    assert loop.run_once() is None
    assert runner.missions == []  # Mission を実行していない
    act = (tp / "a.log").read_text(encoding="utf-8")
    assert "data_unhealthy" in act


def test_runner_failure_recorded(tmp_path):
    conn, loop, _, tp = _loop(tmp_path,
                              [MissionResult("timeout", None, [])])
    assert loop.run_once() is None
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["status"] == "timeout"
    assert "mission_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_unparsable_intent_recorded(tmp_path):
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "buy!"}, [])])
    assert loop.run_once() is None
    assert "intent_parse_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_open_intent_executes(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "test"}, [])])
    out = loop.run_once()
    assert out["result"] == "pending"


def test_ask_is_answer_only(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "現在は様子見が妥当です"}, [])])
    ans = loop.ask_once("今どう見てる？")
    assert "様子見" in ans
    # ask Mission のスキーマは answer 専用 (TradeIntent 不可)
    schema = runner.missions[0].output_schema
    assert "answer" in schema["properties"]
    assert "action" not in schema["properties"]
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["loop"] == "ask"
    # intent は生成されない
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 0
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/loops/test_trade_loop.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/loops/trade_loop.py`:

```python
"""取引判断 loop — Mission 組み立て・実行・記録・executor 連携 (設計書 §5)。"""
from __future__ import annotations

import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock, IntentParseError, Origin, TradeIntent
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.loops.summary import (
    ANSWER_SCHEMA, TRADE_INTENT_SCHEMA, build_state_summary,
)
from agentic_fx.policy import Policy
from agentic_fx.runners.base import AgentRunner, Mission
from agentic_fx.store import missions

_TRADE_TOOLS = ["get_ohlcv", "get_indicators", "search_news",
                "get_econ_calendar", "get_positions", "get_account",
                "get_recent_reflections", "search_reflections"]


class TradeLoop:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 settings: Settings, executor: Executor,
                 provider: PriceProvider, econ: EconCalendar, policy: Policy,
                 activity: ActivityLog, notifier: Notifier,
                 clock: Clock) -> None:
        self.conn = conn
        self.runner = runner
        self.settings = settings
        self.executor = executor
        self.provider = provider
        self.econ = econ
        self.policy = policy
        self.activity = activity
        self.notifier = notifier
        self.clock = clock

    # ---- 定期 Mission ---------------------------------------------------

    def run_once(self) -> dict | None:
        now = self.clock.now()
        try:
            self.provider.healthcheck(self.settings.pairs[0])
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        prompt = self._build_prompt(load_prompt("trade_mission"))
        mission = Mission(prompt=prompt, tools=_TRADE_TOOLS,
                          output_schema=TRADE_INTENT_SCHEMA,
                          max_turns=self.settings.llama_swap.max_turns,
                          timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "trade",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now)
        result = self.runner.run(mission)
        missions.finish(self.conn, mid, result.status, result.output,
                        result.transcript, self.clock.now())
        if result.status != "completed":
            self.activity.write(Category.AGGREGATE, "mission_failed",
                                f"runner status={result.status}",
                                ref_id=str(mid))
            self.notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}")
            return None
        try:
            intent = TradeIntent.from_llm_dict(result.output,
                                               origin=Origin.SCHEDULER)
        except IntentParseError as e:
            self.activity.write(Category.AGGREGATE, "intent_parse_failed",
                                str(e), ref_id=str(mid))
            return None
        out = self.executor.handle_intent(intent, mid)
        self.activity.write(Category.AGGREGATE, "decision",
                            f"{intent.action.value} -> {out['result']}",
                            ref_id=str(mid))
        return out

    # ---- 臨時 Mission (回答専用) ----------------------------------------

    def ask_once(self, question: str) -> str:
        now = self.clock.now()
        prompt = self._build_prompt(load_prompt("ask_mission")) \
            + f"\n\n## ユーザーの質問\n{question}"
        mission = Mission(prompt=prompt, tools=_TRADE_TOOLS,
                          output_schema=ANSWER_SCHEMA,
                          max_turns=self.settings.llama_swap.max_turns,
                          timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "ask",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now)
        result = self.runner.run(mission)
        missions.finish(self.conn, mid, result.status, result.output,
                        result.transcript, self.clock.now())
        if result.status != "completed":
            return f"(Mission 失敗: {result.status})"
        self.activity.write(Category.AGGREGATE, "ask_answered",
                            question[:80], ref_id=str(mid))
        return result.output["answer"]

    # ---- internal -------------------------------------------------------

    def _build_prompt(self, system: str) -> str:
        parts = [system]
        tail = self.policy.tail(4000)
        if tail:
            parts.append(f"## ユーザー方針 (policy)\n{tail}")
        parts.append(build_state_summary(self.conn, self.executor.broker,
                                         self.econ, self.clock))
        return "\n\n".join(parts)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/loops/test_trade_loop.py -v`
Expected: PASS (全 6 テスト)

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/loops/trade_loop.py tests/loops/test_trade_loop.py
git commit -m "feat: 取引判断 loop (fail closed・全記録・ask 回答専用)"
```

---

### Task 4: reflection cycle (loops/reflection_cycle.py)

**Files:**
- Create: `src/agentic_fx/loops/reflection_cycle.py`
- Test: `tests/loops/test_reflection_cycle.py`

**Interfaces:**
- Produces: `class ReflectionCycle`:
  - `__init__(self, *, conn, runner: AgentRunner, rag: Rag, settings: Settings, activity: ActivityLog, clock: Clock)`
  - `run_pending(self) -> int` — **reflection 未作成の closed orders** を検索し、各件: `load_prompt("reflection")` + トレード詳細 (order row + 対応 intent の reasoning) で Mission (`output_schema={"content": string}`、loop="reflection" で記録) → `reflections.save` + `rag.add_reflection` (**SQLite + ChromaDB 二重保存** — 設計書 §12)。runner 失敗はその件をスキップ (次回再試行)。作成件数を返す。scheduler の毎時 Mission 後に呼ぶ (プラン 5 の service 配線)

- [ ] **Step 1: 失敗するテストを書く**

`tests/loops/test_reflection_cycle.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store import orders, reflections
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _cycle(tmp_path, results):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    cyc = ReflectionCycle(conn=conn, runner=FakeRunner(results), rag=rag,
                          settings=SETTINGS,
                          activity=ActivityLog(tmp_path / "a.log"),
                          clock=FixedClock(NOW))
    return conn, rag, cyc


def _closed_order(conn):
    return orders.insert(conn, pair="USDJPY", direction="long",
                         entry_type="market", horizon="day",
                         status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
                         realized_pnl=-1500.0, close_reason="sl",
                         avg_fill_price=148.5, close_price=148.0)


def test_creates_reflection_for_closed(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "SL 幅が狭すぎた"}, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 1
    assert reflections.get(conn, oid)["content"] == "SL 幅が狭すぎた"
    rag.add_reflection.assert_called_once_with(oid, "SL 幅が狭すぎた", "USDJPY")


def test_skips_already_reflected(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    oid = _closed_order(conn)
    reflections.save(conn, oid, "既存", NOW)
    assert cyc.run_pending() == 0
    rag.add_reflection.assert_not_called()


def test_runner_failure_skips_for_retry(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [MissionResult("timeout", None, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None  # 次回再試行できる
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/loops/test_reflection_cycle.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/loops/reflection_cycle.py`:

```python
"""reflection cycle — クローズ済みトレードの振り返り生成 (SQLite + Chroma 二重保存)。"""
from __future__ import annotations

import json
import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.runners.base import AgentRunner, Mission
from agentic_fx.store import missions, reflections
from agentic_fx.store.rag import Rag

_SCHEMA = {"type": "object",
           "properties": {"content": {"type": "string"}},
           "required": ["content"]}


class ReflectionCycle:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 rag: Rag, settings: Settings, activity: ActivityLog,
                 clock: Clock) -> None:
        self.conn = conn
        self.runner = runner
        self.rag = rag
        self.settings = settings
        self.activity = activity
        self.clock = clock

    def run_pending(self) -> int:
        rows = self.conn.execute(
            "SELECT o.* FROM orders o LEFT JOIN reflections r "
            "ON r.order_id = o.id WHERE o.status='closed' "
            "AND r.order_id IS NULL ORDER BY o.id").fetchall()
        created = 0
        for row in rows:
            if self._reflect_one(dict(row)):
                created += 1
        return created

    def _reflect_one(self, row: dict) -> bool:
        now = self.clock.now()
        intent = None
        if row["intent_id"]:
            ir = self.conn.execute(
                "SELECT payload_json FROM trade_intents WHERE id=?",
                (row["intent_id"],)).fetchone()
            intent = json.loads(ir["payload_json"]) if ir else None
        prompt = (load_prompt("reflection") + "\n\n## トレード詳細\n"
                  + json.dumps({"order": {k: row[k] for k in (
                      "id", "pair", "direction", "horizon", "quantity",
                      "avg_fill_price", "close_price", "realized_pnl",
                      "close_reason")},
                      "entry_reasoning": (intent or {}).get("reasoning")},
                      ensure_ascii=False, indent=1))
        mission = Mission(prompt=prompt, tools=[], output_schema=_SCHEMA,
                          max_turns=2,
                          timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "reflection",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now)
        result = self.runner.run(mission)
        missions.finish(self.conn, mid, result.status, result.output,
                        result.transcript, self.clock.now())
        if result.status != "completed":
            return False
        content = result.output["content"]
        reflections.save(self.conn, row["id"], content, now)
        self.rag.add_reflection(row["id"], content, row["pair"])
        self.activity.write(Category.AGGREGATE, "reflection_created",
                            f"#{row['id']} {row['pair']}",
                            ref_id=str(row["id"]))
        return True
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/loops/test_reflection_cycle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/loops/reflection_cycle.py tests/loops/test_reflection_cycle.py
git commit -m "feat: reflection cycle (未作成のみ・二重保存・失敗は再試行)"
```

---

### Task 5: 操作コマンド (commands.py)

**Files:**
- Create: `src/agentic_fx/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Produces: `class Commands` (main.py シェルで使用。Phase 2 で client.py が同じ定義を API 経由で再利用):
  - `__init__(self, *, conn, state_store: StateStore, broker: PaperBroker, trade_loop: TradeLoop, activity: ActivityLog, log_dir: Path, clock: Clock)`
  - `dispatch(self, line: str) -> str` — 1 行をパースして実行し、表示文字列を返す。未知コマンドは help 文字列
  - Phase 1 コマンド: `status` / `log [n]` / `activity [n] [カテゴリ]` / `ask <質問...>` / `approve <id>` / `reject <id> [理由...]` / `killswitch reset` / `help`
  - `status`: mode・autopilot・kill switch・残高/エクイティ・アクティブ orders 数・直近 mission
  - `log`: `logs/agentic.log` の末尾 n 行 (デフォルト 20)
  - `activity`: `ActivityLog.tail(n, category)`
  - `approve/reject`: `approvals.decide` (`AlreadyDecidedError` はメッセージ表示)
  - `killswitch reset`: `state_store.update(kill_switch_latched=False)` + activity SYSTEM 記録 (人間の明示解除 — 設計書 §5)
  - Phase 2 予定コマンド (`policy add` / `improve` / `news` / `model` / `mode` / `autopilot`) は「Phase 2 で追加」と help に明記

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_commands.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _commands(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    state = StateStore(tmp_path / "s.json")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "agentic.log").write_text(
        "line1\nline2\nline3\n", encoding="utf-8")
    trade_loop = MagicMock()
    trade_loop.ask_once.return_value = "回答です"
    cmds = Commands(conn=conn, state_store=state,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=trade_loop, activity=activity,
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW))
    return conn, state, activity, cmds


def test_status(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("status")
    assert "learning" in out and "1,000,000" in out


def test_log_tail(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log 2")
    assert "line2" in out and "line1" not in out


def test_activity_filter(tmp_path):
    _, _, activity, cmds = _commands(tmp_path)
    activity.write(Category.TRADE, "order_opened", "x")
    activity.write(Category.NEWS, "collected", "y")
    out = cmds.dispatch("activity 10 TRADE")
    assert "order_opened" in out and "collected" not in out


def test_ask_delegates(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert "回答です" in cmds.dispatch("ask 今の相場は？")


def test_approve_reject(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    assert "approved" in cmds.dispatch(f"approve {aid}")
    out = cmds.dispatch(f"reject {aid} 理由")  # 二重決定
    assert "決定済み" in out or "already" in out.lower()


def test_killswitch_reset(tmp_path):
    _, state, _, cmds = _commands(tmp_path)
    state.update(kill_switch_latched=True)
    out = cmds.dispatch("killswitch reset")
    assert state.load().kill_switch_latched is False
    assert "解除" in out


def test_unknown_shows_help(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert "status" in cmds.dispatch("nonsense")
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_commands.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/commands.py`:

```python
"""操作コマンド定義 — main.py シェルと (Phase 2) client.py で共有 (設計書 §8)。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals, missions, orders
from agentic_fx.store.approvals import AlreadyDecidedError
from agentic_fx.store.state import StateStore

_HELP = """コマンド一覧:
  status                     残高・モード・kill switch・直近 mission
  log [n]                    技術ログの直近 n 行 (default 20)
  activity [n] [カテゴリ]     activity ログ (NEWS/TECH/AGGREGATE/TRADE/IMPROVE/APPROVAL/SYSTEM)
  ask <質問>                  臨時 Mission (回答専用 — 発注はしない)
  approve <id> / reject <id> [理由]   承認操作
  killswitch reset           kill switch ラッチの解除 (人間の明示操作)
  stop                       graceful shutdown (シェルのみ)
(Phase 2 で追加: policy add / improve / news / model / mode / autopilot)"""


class Commands:
    def __init__(self, *, conn: sqlite3.Connection, state_store: StateStore,
                 broker: PaperBroker, trade_loop, activity: ActivityLog,
                 log_dir: Path, clock: Clock) -> None:
        self.conn = conn
        self.state = state_store
        self.broker = broker
        self.trade_loop = trade_loop
        self.activity = activity
        self.log_dir = log_dir
        self.clock = clock

    def dispatch(self, line: str) -> str:
        parts = line.strip().split()
        if not parts:
            return ""
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "status":
                return self._status()
            if cmd == "log":
                return self._log(int(args[0]) if args else 20)
            if cmd == "activity":
                n = int(args[0]) if args else 20
                cat = Category(args[1].upper()) if len(args) > 1 else None
                return "\n".join(self.activity.tail(n, cat)) or "(なし)"
            if cmd == "ask":
                if not args:
                    return "usage: ask <質問>"
                return self.trade_loop.ask_once(" ".join(args))
            if cmd == "approve" and args:
                approvals.decide(self.conn, int(args[0]), status="approved",
                                 decided_by="shell", now=self.clock.now())
                self.activity.write(Category.APPROVAL, "approved",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} approved"
            if cmd == "reject" and args:
                reason = " ".join(args[1:]) or None
                approvals.decide(self.conn, int(args[0]), status="rejected",
                                 decided_by="shell", now=self.clock.now(),
                                 reason=reason)
                self.activity.write(Category.APPROVAL, "rejected",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} rejected"
            if cmd == "killswitch" and args and args[0] == "reset":
                self.state.update(kill_switch_latched=False)
                self.activity.write(Category.SYSTEM, "kill_switch_reset",
                                    "human explicit reset via shell")
                return "kill switch ラッチを解除しました"
        except AlreadyDecidedError:
            return "その approval は決定済みです"
        except (ValueError, KeyError) as e:
            return f"エラー: {e}\n{_HELP}"
        return _HELP

    def _status(self) -> str:
        s = self.state.load()
        balance, equity = self.broker.equity()
        active = orders.list_by_status(self.conn, "open", "pending_fill",
                                       "protection_pending")
        recent = missions.recent(self.conn, 1)
        last = (f"{recent[0]['loop']}:{recent[0]['status']} "
                f"({recent[0]['started_at']})") if recent else "なし"
        return (f"mode: {s.mode.value} / autopilot: "
                f"{'on' if s.autopilot else 'off'} / kill switch: "
                f"{'LATCHED' if s.kill_switch_latched else 'ok'}\n"
                f"残高: {balance:,.0f} / エクイティ: {equity:,.0f}\n"
                f"アクティブ orders: {len(active)}\n"
                f"直近 mission: {last}")

    def _log(self, n: int) -> str:
        path = self.log_dir / "agentic.log"
        if not path.exists():
            return "(ログなし)"
        return "\n".join(
            path.read_text(encoding="utf-8").splitlines()[-n:])
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/commands.py tests/test_commands.py
git commit -m "feat: 操作コマンド (status/log/activity/ask/approve/killswitch reset)"
```

---

### Task 6: 対話シェル (shell.py) + logging daemon 拡張

**Files:**
- Create: `src/agentic_fx/shell.py`
- Modify: `src/agentic_fx/logging_setup.py`
- Test: `tests/test_shell.py`, `tests/test_logging_setup.py` (追記)

**Interfaces:**
- Produces:
  - `run_shell(commands: Commands, stop_event: threading.Event, *, input_fn=input, print_fn=print) -> None` — プロンプト `afx> `。`stop` / EOF / Ctrl-C で `stop_event.set()` して抜ける。その他は `commands.dispatch` の戻りを print。**ログを勝手に流さない** (print するのはコマンド結果のみ)
  - `setup_technical_logging(log_dir, level="INFO", *, daemon: bool = False)` — `daemon=True` のとき stderr StreamHandler を追加 (systemd → journald 経路。プラン 1 の契約)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_shell.py`:

```python
import threading
from unittest.mock import MagicMock

from agentic_fx.shell import run_shell


def _run(lines):
    cmds = MagicMock()
    cmds.dispatch.return_value = "OK"
    stop = threading.Event()
    it = iter(lines)
    outputs = []

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    run_shell(cmds, stop, input_fn=fake_input, print_fn=outputs.append)
    return cmds, stop, outputs


def test_stop_sets_event():
    cmds, stop, _ = _run(["stop"])
    assert stop.is_set()
    cmds.dispatch.assert_not_called()


def test_eof_sets_event():
    _, stop, _ = _run([])
    assert stop.is_set()


def test_commands_dispatched_and_printed():
    cmds, _, outputs = _run(["status", "stop"])
    cmds.dispatch.assert_called_once_with("status")
    assert "OK" in outputs


def test_empty_line_ignored():
    cmds, _, _ = _run(["", "  ", "stop"])
    cmds.dispatch.assert_not_called()
```

`tests/test_logging_setup.py` に追記:

```python
def test_daemon_adds_stderr_handler(tmp_path):
    import logging
    logger = setup_technical_logging(tmp_path, daemon=True)
    kinds = [type(h).__name__ for h in logger.handlers]
    assert "RotatingFileHandler" in kinds
    assert "StreamHandler" in kinds
    # daemon=False に戻すと stderr handler は外れる
    logger2 = setup_technical_logging(tmp_path, daemon=False)
    assert [type(h).__name__ for h in logger2.handlers] == [
        "RotatingFileHandler"]
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_shell.py tests/test_logging_setup.py -v`
Expected: 新規分 FAIL

- [ ] **Step 3: 実装**

`src/agentic_fx/shell.py`:

```python
"""対話シェル — 静かなプロンプト。ログは pull 型コマンドのみ (設計書 §8)。"""
from __future__ import annotations

import threading

from agentic_fx.commands import Commands


def run_shell(commands: Commands, stop_event: threading.Event, *,
              input_fn=input, print_fn=print) -> None:
    while not stop_event.is_set():
        try:
            line = input_fn("afx> ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            return
        if not line:
            continue
        if line == "stop":
            stop_event.set()
            return
        print_fn(commands.dispatch(line))
```

`src/agentic_fx/logging_setup.py` — `setup_technical_logging` を置換:

```python
import sys


def setup_technical_logging(log_dir: Path, level: str = "INFO", *,
                            daemon: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    target = (log_dir / "agentic.log").resolve()
    logger = logging.getLogger("agentic_fx")
    logger.setLevel(level.upper())
    logger.propagate = False
    file_ok = False
    for h in list(logger.handlers):
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == target:
            file_ok = True
            continue
        logger.removeHandler(h)
        h.close()
    if not file_ok:
        handler = RotatingFileHandler(target, maxBytes=10 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    if daemon:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(stream)
    return logger
```

注: daemon=False 再呼び出しで StreamHandler が外れるのは「RotatingFileHandler + 同一パス以外は除去」のロジックによる。

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_shell.py tests/test_logging_setup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/shell.py src/agentic_fx/logging_setup.py \
  tests/test_shell.py tests/test_logging_setup.py
git commit -m "feat: 対話シェル + daemon 時の journald 経路 (stderr handler)"
```

---

### Task 7: サービス配線 (service.py 本実装 + entry 拡張)

**Files:**
- Modify: `src/agentic_fx/service.py`, `src/agentic_fx/entry.py`
- Test: `tests/test_service_app.py`

**Interfaces:**
- Produces:
  - `@dataclass App(conn, settings, state, activity, broker, executor, provider, econ, collector, rag, trade_loop, reflection, scheduler, commands, registry, mission_lock: threading.Lock)`
  - `build_app(root: Path, *, runner: AgentRunner | None = None, clock: Clock | None = None) -> App` — 全部品を配線して返す (テスト注入点: runner=FakeRunner / clock=FixedClock):
    - registry: market/news/account/reflection の build を register_all
    - runner が None なら `LocalRunner(base_url=settings.llama_swap.base_url, model=settings.runner.trade.model, registry=registry)`
    - `Scheduler(on_trade_mission=<mission_lock を取って trade_loop.run_once + reflection.run_pending>, on_news_cycle=collector.collect, bars_fn=provider.latest_1m_bar)`
    - executor の `quote_fn=provider.get_quote / spec_fn=provider.spec`
    - **equity 時価 snapshot**: `on_trade_mission` の先頭で `record_snapshot` (broker.equity ベース — Phase 1 近似、プラン 3 引き継ぎの注記どおり)
  - `run_service(root: Path, *, daemon: bool = False) -> int` — ensure_initialized → build_app → policy サイズ警告表示 → **スプラッシュ表示** → scheduler スレッド起動 (60 秒毎 `tick(now)`、stop_event を 1 秒粒度で監視) → TTY なら `run_shell`、daemon なら SIGTERM/SIGINT 待ち → **graceful shutdown** (stop_event → mission_lock 取得を最大 30 秒待って終了)
  - `build_splash(app) -> str` — mode・発注方式・対象ペア・runner/モデル・risk gate 現行値・承認待ち件数・未約定指値・直近成績。**項目は最小でよい (運用しながら調整 — 設計書 §8)**
  - entry: `--daemon` フラグ追加。非 TTY (`not sys.stdin.isatty()`) は自動で daemon 扱い
  - init 拡張: llama-swap 接続確認 (`GET {base_url}/models`、失敗は警告のみ)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_service_app.py`:

```python
import threading
from datetime import datetime, timezone

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, build_splash, run_init

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _init(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    from unittest.mock import patch
    with patch("agentic_fx.service.PriceProvider") as pp:
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)


def test_build_app_wires_everything(tmp_path):
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    assert app.trade_loop is not None
    assert app.scheduler is not None
    assert isinstance(app.mission_lock, type(threading.Lock()))
    # ツールが登録されている
    for name in ("get_ohlcv", "search_news", "get_positions",
                 "get_recent_reflections"):
        assert name in app.registry.names()


def test_splash_contains_key_fields(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    splash = build_splash(app)
    assert "learning" in splash
    assert "USDJPY" in splash
    assert "qwen" in splash  # runner モデル名


def test_on_trade_mission_runs_loop_and_reflection(tmp_path):
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from unittest.mock import patch
    with patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.on_trade_mission()
    assert len(fake.missions) >= 1  # trade mission が実行された
    rows = app.conn.execute("SELECT * FROM missions").fetchall()
    assert any(r["loop"] == "trade" for r in rows)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_service_app.py -v`
Expected: FAIL

- [ ] **Step 3: 実装**

`src/agentic_fx/service.py` に追加 (既存 `ensure_initialized` / `run_init` は維持し、以下を追記。import は先頭にまとめる):

```python
from __future__ import annotations

import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import os

from agentic_fx.commands import Commands
from agentic_fx.core.contracts import Clock
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.news_collector import NewsCollector
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.loops.trade_loop import TradeLoop
from agentic_fx.policy import Policy
from agentic_fx.runners.base import AgentRunner
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.store import approvals, orders
from agentic_fx.store.rag import Rag
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, reflection_tools,
)
from agentic_fx.tools.registry import ToolRegistry


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class App:
    conn: object
    settings: object
    state: object
    activity: object
    broker: object
    executor: object
    provider: object
    econ: object
    collector: object
    rag: object
    trade_loop: object
    reflection: object
    scheduler: object
    commands: object
    registry: object
    mission_lock: threading.Lock


def build_app(root: Path, *, runner: AgentRunner | None = None,
              clock: Clock | None = None) -> App:
    clock = clock or _SystemClock()
    settings = load_settings(root / "config" / "settings.yaml")
    state = _state_store(root)
    activity = ActivityLog(root / "logs" / "activity.log")
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)

    provider = PriceProvider(conn, settings, clock)
    econ = EconCalendar(conn, activity, clock)
    rag = Rag(root / "data" / "rag")
    collector = NewsCollector(conn, rag, activity, clock)
    broker = PaperBroker(conn, settings, clock)
    notifier = Notifier(enabled=settings.discord.enabled,
                        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"))
    executor = Executor(conn=conn, broker=broker, settings=settings,
                        state_store=state, activity=activity, clock=clock,
                        quote_fn=provider.get_quote, spec_fn=provider.spec)

    registry = ToolRegistry()
    registry.register_all(market_tools.build(provider, econ))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn, broker))
    registry.register_all(reflection_tools.build(conn, rag))

    if runner is None:
        runner = LocalRunner(base_url=settings.llama_swap.base_url,
                             model=settings.runner.trade.model,
                             registry=registry)

    policy = Policy(root / "policy" / "directives.md")
    trade_loop = TradeLoop(conn=conn, runner=runner, settings=settings,
                           executor=executor, provider=provider, econ=econ,
                           policy=policy, activity=activity,
                           notifier=notifier, clock=clock)
    reflection = ReflectionCycle(conn=conn, runner=runner, rag=rag,
                                 settings=settings, activity=activity,
                                 clock=clock)

    mission_lock = threading.Lock()

    def on_trade_mission() -> None:
        with mission_lock:
            balance, equity = broker.equity()
            from agentic_fx.core.accounting import record_snapshot
            record_snapshot(conn, now=clock.now(), balance=balance,
                            equity=equity)
            trade_loop.run_once()
            reflection.run_pending()

    scheduler = Scheduler(conn=conn, executor=executor, settings=settings,
                          state_store=state, activity=activity,
                          bars_fn=provider.latest_1m_bar,
                          on_trade_mission=on_trade_mission,
                          on_news_cycle=collector.collect)

    commands = Commands(conn=conn, state_store=state, broker=broker,
                        trade_loop=_LockedAsk(trade_loop, mission_lock),
                        activity=activity, log_dir=root / "logs", clock=clock)
    return App(conn=conn, settings=settings, state=state, activity=activity,
               broker=broker, executor=executor, provider=provider, econ=econ,
               collector=collector, rag=rag, trade_loop=trade_loop,
               reflection=reflection, scheduler=scheduler, commands=commands,
               registry=registry, mission_lock=mission_lock)


class _LockedAsk:
    """ask を Mission スロット (排他) 経由で実行する薄いラッパー。"""

    def __init__(self, trade_loop: TradeLoop, lock: threading.Lock) -> None:
        self._loop = trade_loop
        self._lock = lock

    def ask_once(self, question: str) -> str:
        with self._lock:
            return self._loop.ask_once(question)


def build_splash(app: App) -> str:
    s = app.state.load()
    balance, equity = app.broker.equity()
    pending = len(approvals.pending(app.conn))
    limits = len(orders.list_by_status(app.conn, "pending_fill"))
    # 項目構成は運用しながら調整 (設計書 §8) — 初期実装は最小
    return (
        "=== agentic-fx ===\n"
        f"mode: {s.mode.value} / autopilot: {'on' if s.autopilot else 'off'}"
        f" / kill switch: {'LATCHED' if s.kill_switch_latched else 'ok'}\n"
        f"pairs: {', '.join(app.settings.pairs)}\n"
        f"runner: {app.settings.runner.trade.backend}"
        f" ({app.settings.runner.trade.model})\n"
        f"risk: {app.settings.risk.risk_per_trade_pct}%/trade,"
        f" DD kill {app.settings.risk.drawdown_kill_pct}%,"
        f" daily {app.settings.risk.daily_loss_limit_pct}%\n"
        f"残高: {balance:,.0f} / 承認待ち: {pending} / 未約定指値: {limits}\n"
        "コマンドは help を参照。stop で終了。")


def run_service(root: Path, *, daemon: bool = False) -> int:
    ensure_initialized(root)
    settings = load_settings(root / "config" / "settings.yaml")
    setup_technical_logging(root / "logs", settings.logging.level,
                            daemon=daemon)
    app = build_app(root)

    warning = Policy(root / "policy" / "directives.md").size_warning()
    if warning:
        print(warning)
    print(build_splash(app))
    app.activity.write(Category.SYSTEM, "service_started",
                       f"daemon={daemon}")

    stop_event = threading.Event()

    def scheduler_thread() -> None:
        last = 0.0
        while not stop_event.is_set():
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                try:
                    app.scheduler.tick(datetime.now(timezone.utc))
                except Exception:  # noqa: BLE001
                    logging.getLogger("agentic_fx").exception("tick failed")
            stop_event.wait(1)

    th = threading.Thread(target=scheduler_thread, daemon=True)
    th.start()

    if daemon:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())
        while not stop_event.is_set():
            stop_event.wait(1)
    else:
        from agentic_fx.shell import run_shell
        run_shell(app.commands, stop_event)

    # graceful shutdown: 実行中 Mission の完了待ち (最大 30 秒)
    acquired = app.mission_lock.acquire(timeout=30)
    if acquired:
        app.mission_lock.release()
    th.join(timeout=5)
    app.activity.write(Category.SYSTEM, "service_stopped", "graceful")
    print("停止しました。")
    return 0
```

`src/agentic_fx/entry.py` — 置換:

```python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_fx import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afx", description="agentic-fx")
    parser.add_argument("--daemon", action="store_true",
                        help="systemd 用 (コマンド受付なし)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="初期設定 (非対話・冪等)")
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "init":
        return service.run_init(root)
    daemon = args.daemon or not sys.stdin.isatty()
    return service.run_service(root, daemon=daemon)
```

`run_init` 末尾 (価格ソース確認の後) に llama-swap 確認を追加:

```python
    try:
        import httpx
        httpx.get(f"{settings.llama_swap.base_url}/models", timeout=5)
        print("llama-swap OK")
    except Exception as e:  # noqa: BLE001
        print(f"警告: llama-swap に接続できません ({e})。"
              "取引判断 Mission は失敗として記録されます。")
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_service_app.py tests/test_init_and_guard.py -v`
Expected: PASS (init テストは llama-swap 警告分岐を含め成立 — 失敗するなら httpx を mock)

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/service.py src/agentic_fx/entry.py tests/test_service_app.py
git commit -m "feat: サービス配線 (build_app・スプラッシュ・scheduler スレッド・graceful shutdown)"
```

---

### Task 8: E2E — FakeRunner フル自走 (Phase 1 完成条件)

**Files:**
- Test: `tests/test_e2e_phase1.py`

- [ ] **Step 1: E2E テストを書く**

`tests/test_e2e_phase1.py`:

```python
"""Phase 1 完成条件: 学習モードで scheduler tick → Mission → intent →
ペーパー発注 → 約定 → reflection まで LLM なし (FakeRunner) で自走する。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, run_init

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

OPEN_INTENT = {"action": "open", "pair": "USDJPY", "direction": "long",
               "entry_type": "limit", "horizon": "day",
               "limit_price": 148.20, "expires_in": "6h",
               "stop_loss": 147.80, "take_profit": 149.00,
               "reasoning": "e2e"}


def test_phase1_full_cycle(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp:
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)

    fake = FakeRunner([
        MissionResult("completed", OPEN_INTENT, []),          # 定期判断
        MissionResult("completed", {"content": "振り返り"}, []),  # reflection
    ])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(WED))

    bars = {}
    quote = None
    with patch.object(app.provider, "healthcheck", return_value="test"), \
         patch.object(app.provider, "get_quote") as gq, \
         patch.object(app.provider, "spec") as sp, \
         patch.object(app.provider, "latest_1m_bar",
                      side_effect=lambda p: bars.get(p)):
        from agentic_fx.core.contracts import InstrumentSpec, Quote
        gq.return_value = Quote("USDJPY", 148.49, 148.51, WED, "test")
        sp.return_value = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01,
                                         100_000)
        # 注: executor は build_app 時点の provider.get_quote を束縛済みのため
        # patch.object が効くように app.executor 側も差し替える
        app.executor.quote_fn = gq
        app.executor.spec_fn = sp

        # tick 1: 毎時 Mission → 指値発注
        app.scheduler.tick(WED)
        rows = app.conn.execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "pending_fill"

        # tick 2: 約定バー → open
        bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
        app.scheduler.tick(WED + timedelta(minutes=1))
        assert app.conn.execute(
            "SELECT status FROM orders").fetchone()["status"] == "open"

        # tick 3: TP バー → closed
        bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.90, 149.10, 148.85,
                             149.05, 100)
        app.scheduler.tick(WED + timedelta(minutes=2))
        row = app.conn.execute("SELECT * FROM orders").fetchone()
        assert row["status"] == "closed" and row["realized_pnl"] > 0

        # tick 4 (1 時間後): 次の定期 Mission 前に reflection が生成される
        app.scheduler.tick(WED + timedelta(hours=1, minutes=1))
        refl = app.conn.execute("SELECT * FROM reflections").fetchall()
        assert len(refl) == 1

    # 監査痕跡
    missions = app.conn.execute("SELECT loop, status FROM missions").fetchall()
    assert {m["loop"] for m in missions} >= {"trade", "reflection"}
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    for ev in ("decision", "limit_placed", "limit_filled", "order_closed",
               "reflection_created"):
        assert ev in act
```

- [ ] **Step 2: テストを実行して green にする**

Run: `uv run pytest tests/test_e2e_phase1.py -v`
Expected: PASS (落ちる場合は配線を修正 — テストの意図を変えない)

- [ ] **Step 3: 全体テスト + セルフレビュー**

Run: `uv run pytest -v`
Expected: 全件 PASS

チェック (Phase 1 完成条件):
- 設計書 §15 Phase 1 の項目が全て揃ったか: 決定論的コア / LocalRunner / 取引判断 loop / 価格取得 (yfinance デフォルト) / main.py (スプラッシュ + シェル + init ガード + stop) / ログ 2 軸 / 最小ツールセット
- `uv run main.py init` → `uv run main.py` → `status` / `ask` / `stop` の手動確認
- ask から intent が執行されないこと (origin 検証テスト + ANSWER_SCHEMA)

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_phase1.py
git commit -m "test: Phase 1 E2E (FakeRunner フル自走 — 完成条件)"
```

---

## Phase 2 プランへの引き継ぎ事項

- `Commands` に Phase 2 コマンド (policy add / improve / news / model / mode / autopilot) を追加し、client.py は操作 API 経由で同じ dispatch を呼ぶ
- 操作 API (FastAPI) は `App` を共有し、`_LockedAsk` と同じ mission_lock を使う (Mission 実行は常にサービスプロセス内)
- ClaudeRunner は `AgentRunner` 実装として追加し、`settings.runner.<loop>.backend == "claude"` のとき build_app が選択する
- 改善 loop は `improve` 用の registry サブセット (research_tools + 書き込み系) を別途組む — 取引判断 loop の読み取り専用 registry に書き込み系を混ぜない
- ChromaDB の `Rag` は改善 loop でもそのまま使う。plugin_loader は `get_indicators` の合成点 (market_tools.build に承認済み plugin の結果を追加) として実装する
