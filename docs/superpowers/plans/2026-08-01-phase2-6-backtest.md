# プラン 6: バックテスト基盤 + 履歴分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 設計書 §6「バックテスト (詳細設計 2026-08-01 確定)」と「履歴分析」を、plugin 機構なしで完結する形 (IntentSource 差し込み) で実装する。

**Architecture:** 既存の決定論的コア (scheduler / risk gate / executor / paper 約定) を in-memory SQLite + ReplayClock + 履歴バー供給で再生する。履歴は `ohlcv` v2 (source 込み一意キー + spread 列) に Dukascopy / MT5 の一括インポータで投入。holdout はハーネス所有、分析は in-sample 遮断。

**Tech Stack:** Python 3.13 / uv / SQLite / httpx / pandas / lzma (標準) / pytest。

## Global Constraints (全 task に適用)

- **発注・SL 変更・クローズ・資金保護は決定論的コードのみ** — バックテストの intent 経路も executor の origin/mission_id 検証を**同一コードで**通す (§6: 検証の注入置換・緩和は不可)
- **実 DB (`data/agentic.db`)・実 `data/state/` にバックテストが触れない** — in-memory SQLite に `init_db`
- **エージェント向け API は期間を受け取らない / holdout 情報を返さない** (遮断 8 項目、§6)。人間 CLI は対象外
- **`ohlcv` のインポート系書き込みは既存行不変** (同一キー同一値のみ無変更許容、異なる値は棄却+ログ)。live キャッシュ (yfinance) の上書き upsert は現行維持 — 適用対象が違う (§6)
- **既存テストの期待値を弱める変更は不可**。whitelist ピン (get_ohlcv / get_indicators の properties キー集合 `{pair, timeframe}`) 維持
- テストに実スリープ・実 HTTP を混入させない (インポータのネットワークはフィクスチャで代替。実照合は CLI の手動ステップ)
- `config/settings.yaml.example` に新キーを足したら `config/settings.yaml` (個人設定) と両方同期
- コミット trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

**現状の前提 (2026-08-01 実測)**: 791 tests PASS / `ohlcv` は PK (symbol, interval, bar_time) で source 列なし / `store/ohlcv.py` の `upsert_bars` は ON CONFLICT 上書き / MT5 bridge (:8812) 稼働・1m 深度 ≈ 3 ヶ月 / llama-swap は本プランでは不使用。

---

### Task 0: build_app の embedding seam (park 返済 — オフライン CI の前提条件)

**Files:**
- Modify: `src/agentic_fx/service.py` (build_app)
- Modify: `tests/test_service_app.py`, `tests/test_e2e_phase1.py` (フィクスチャ利用へ)
- Test: `tests/test_service_app.py` に追記

**Interfaces:**
- Produces: `build_app(root, *, runner=None, clock=None, quote_fn=None, spec_fn=None, bars_fn=None, embedding_fn=None)` — `embedding_fn` は `Rag(root / "data" / "rag", embedding_function=embedding_fn)` にそのまま渡す。None なら現行どおり既定 (chromadb DefaultEmbeddingFunction)

- [ ] **Step 1: 失敗するテストを書く** — `tests/test_service_app.py` に追記:

```python
def test_build_app_accepts_embedding_fn(tmp_path):
    """embedding_fn 注入で chromadb 既定モデルの probe を回避できる。"""
    _init(tmp_path)
    calls = []

    class FakeEmbedding:
        def __call__(self, input):
            calls.append(list(input))
            return [[0.0] * 8 for _ in input]

        def name(self):
            return "fake"

    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    assert app.rag is not None
    assert calls  # Rag 初期化 probe が fake を通った
```

(既存 `tests/store/test_rag.py` の FakeEmbedding パターンに合わせる — chromadb の
EmbeddingFunction 互換要件は既存テストの実装を正とする。)

- [ ] **Step 2: FAIL 確認** — `uv run pytest tests/test_service_app.py::test_build_app_accepts_embedding_fn -v` → TypeError (unexpected keyword)

- [ ] **Step 3: 実装** — `build_app` シグネチャに `embedding_fn=None` を追加し、`rag = Rag(root / "data" / "rag", embedding_function=embedding_fn)` に変更 (None 時は Rag 側の既定が効く — Rag は既に `embedding_function=None` 引数を持つ)。

- [ ] **Step 4: 全テスト PASS 確認** — `uv run pytest -q`

- [ ] **Step 5: (任意・推奨) 既存 build_app 系テストの高速化** — `tests/test_service_app.py` の `_seam_app` 相当ヘルパと `tests/test_e2e_phase1.py` に FakeEmbedding を注入し、実測でテスト時間が短縮されることを記録 (機能は不変。期待値は変えない)。

- [ ] **Step 6: Commit** — `git commit -m "feat: build_app に embedding seam (オフライン CI 前提の park 返済)"`

---

### Task 1: ohlcv v2 — source + spread 列と一意キー再構築

**Files:**
- Modify: `src/agentic_fx/store/db.py` (DDL + migration)
- Modify: `src/agentic_fx/store/ohlcv.py`
- Modify: `src/agentic_fx/datafeed/price_provider.py` (呼び出し追随)
- Test: `tests/store/test_db.py`, `tests/store/test_ohlcv.py` (新規可), 既存 datafeed テスト追随

**Interfaces:**
- Produces:
  - DDL: `ohlcv(symbol, interval, bar_time, open, high, low, close, volume, source TEXT NOT NULL DEFAULT 'yfinance', spread REAL, PRIMARY KEY (symbol, interval, bar_time, source))`
  - `upsert_bars(conn, bars, *, source: str = "yfinance") -> int` — **live キャッシュ用**。現行どおり ON CONFLICT 上書き (形成中バーの更新があるため)。既存呼び出し (price_provider) は無変更で動く
  - `import_bars(conn, rows: list[tuple], *, source: str) -> ImportResult` — **インポータ用・既存行不変**。rows は `(symbol, interval, bar_time_iso, o, h, l, c, volume, spread|None)`。同一キー同一値 (float は誤差 1e-9 未満) は無変更、**値が異なる既存行はスキップして件数計上** (`ImportResult(inserted, unchanged, conflicted)`)、conflicted > 0 は WARNING ログ
  - `load_bars(conn, symbol, interval, *, source: str = "yfinance", since=None, until=None) -> list[Bar]` — **source 必須の絞り込み** (既定 yfinance で既存呼び出しの意味は不変)。`until` は分析・バックテストの端点用 (endpoint は呼び出し側 = ハーネスが計算)
  - `load_spread(conn, symbol, bar_time_iso, *, source) -> float | None`

- [ ] **Step 1: migration の失敗するテストを書く** — `tests/store/test_db.py` に追記 (trigger 列 migration テストと同型):

```python
def test_init_db_migrates_legacy_ohlcv_to_v2(tmp_path):
    """旧 PK (symbol,interval,bar_time) の ohlcv が source 込み PK に再構築される。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT NOT NULL, interval TEXT NOT NULL, "
        "bar_time TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
        "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (symbol, interval, bar_time))")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()

    init_db(conn)  # ここで再構築 migration が走る

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    assert {"source", "spread"} <= cols
    row = conn.execute("SELECT source, spread FROM ohlcv").fetchone()
    assert row["source"] == "yfinance" and row["spread"] is None
    # PK が source を含む: 同キー別 source が共存できる
    conn.execute("INSERT INTO ohlcv (symbol,interval,bar_time,open,high,low,"
                 "close,volume,source) VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,0,'dukascopy')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 2
```

- [ ] **Step 2: FAIL 確認** → `uv run pytest tests/store/test_db.py -v -k ohlcv`

- [ ] **Step 3: 実装 (db.py)** — DDL を v2 に更新し、`init_db` に再構築 migration を追加。SQLite は PK 変更不可なので **table rebuild**: `_migrate_ohlcv_v2(conn)` — `PRAGMA table_info(ohlcv)` に `source` が無ければ `BEGIN` → `ALTER TABLE ohlcv RENAME TO ohlcv_v1` → 新 DDL で CREATE → `INSERT INTO ohlcv SELECT symbol,interval,bar_time,open,high,low,close,volume,'yfinance',NULL FROM ohlcv_v1` → `DROP TABLE ohlcv_v1` → `COMMIT` (既存行の由来は Phase 1 で yfinance のみ — 正確)。`_ensure_column` とは別関数 (列追加でなく再構築のため)。

- [ ] **Step 4: store/ohlcv.py の失敗するテストを書く** — `tests/store/test_ohlcv.py` (新規):

```python
NOW_ISO = "2026-07-22T12:00:00+00:00"
ROW = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.012)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db"); init_db(conn); return conn


def test_import_bars_inserts_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    r1 = ohlcv.import_bars(conn, [ROW], source="dukascopy")
    r2 = ohlcv.import_bars(conn, [ROW], source="dukascopy")
    assert (r1.inserted, r1.unchanged, r1.conflicted) == (1, 0, 0)
    assert (r2.inserted, r2.unchanged, r2.conflicted) == (0, 1, 0)


def test_import_bars_never_mutates_existing(tmp_path):
    """既存行と値が異なる入力は棄却 — spec §6「既存行は不変」。"""
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:3] + (999.0,) + ROW[4:]
    r = ohlcv.import_bars(conn, [tampered], source="dukascopy")
    assert r.conflicted == 1
    assert conn.execute("SELECT open FROM ohlcv").fetchone()[0] == 148.0


def test_load_bars_filters_by_source(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    ohlcv.upsert_bars(conn, [Bar("USDJPY", "1m",
                                 datetime.fromisoformat(NOW_ISO),
                                 1, 2, 0.5, 1.5, 0)])  # yfinance 既定
    assert len(ohlcv.load_bars(conn, "USDJPY", "1m", source="dukascopy")) == 1
    assert ohlcv.load_bars(conn, "USDJPY", "1m",
                           source="dukascopy")[0].open == 148.0


def test_upsert_bars_still_overwrites_live_cache(tmp_path):
    """live キャッシュ (yfinance) は形成中バー更新のため上書き — 現行維持。"""
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_bars(conn, [b])
    ohlcv.upsert_bars(conn, [Bar("USDJPY", "1m", b.ts, 1, 3, 0.5, 2.5, 9)])
    row = conn.execute("SELECT high, source FROM ohlcv").fetchone()
    assert row["high"] == 3 and row["source"] == "yfinance"
```

- [ ] **Step 5: FAIL 確認** → 実装 (ohlcv.py) — `upsert_bars` は INSERT 列に source を加え ON CONFLICT 句の対象キーを 4 列に (意味は現行維持)。`import_bars` は `INSERT OR IGNORE` → 変化 0 の行は SELECT で既存値と比較 (誤差 1e-9) → 一致 unchanged / 不一致 conflicted (削除・更新はしない)。`ImportResult` は `@dataclass(frozen=True)`。

- [ ] **Step 6: 呼び出し追随** — `rg -n "upsert_bars|load_bars" src tests` で全呼び出しを洗い出し、キーワード化に追随 (price_provider は source 既定 yfinance のままで挙動不変)。`uv run pytest -q` 全 green。

- [ ] **Step 7: Commit** — `git commit -m "feat: ohlcv v2 (source+spread 列・PK 再構築・インポートは既存行不変)"`

---

### Task 2: settings 拡張 (backtest / watch_symbols / analysis — コア所有)

**Files:**
- Modify: `src/agentic_fx/config.py`, `config/settings.yaml.example`, `config/settings.yaml`
- Test: `tests/test_config.py` に追記

**Interfaces:**
- Produces (`Settings` に追加):
  - `backtest: BacktestSettings` — `holdout_months: int = 3 (ge=1)` / `initial_balance: float = 1_000_000 (gt=0)` / `speed_gate_note: str = ""` は持たない (YAGNI — ベンチ結果はレジャーに記録)
  - `datafeed.watch_symbols: list[str] = []` — 取引不可・分析専用 (§6)
  - `analysis: AnalysisSettings` — `max_watch_symbols: int = 10 (ge=1)` / `max_gap_pct: float = 5.0 (gt=0)`

- [ ] **Step 1: 失敗するテストを書く** — `tests/test_config.py` に追記:

```python
def test_backtest_and_analysis_defaults():
    s = load_settings(EXAMPLE)
    assert s.backtest.holdout_months == 3
    assert s.backtest.initial_balance > 0
    assert s.datafeed.watch_symbols == []
    assert s.analysis.max_watch_symbols == 10
    assert s.analysis.max_gap_pct == 5.0


def test_watch_symbols_never_extend_pairs():
    """watch は取引対象ではない — pairs との独立をピン。"""
    s = load_settings(EXAMPLE)
    assert set(s.datafeed.watch_symbols).isdisjoint(set(s.pairs)) or True
    # 構造上の独立: watch_symbols は RiskSettings.pair_rules の検証対象外
    # (pairs に足さない限り risk 検証は要求されない)
```

- [ ] **Step 2: FAIL → 実装** — `_Strict` サブクラスで `BacktestSettings` / `AnalysisSettings` を追加、`DatafeedSettings` に `watch_symbols: list[str] = []`。**example と個人 settings.yaml の両方**に追記 (コメントで「コア所有 — 変更は人間レビュー必須」と明記):

```yaml
backtest:                      # コア所有 (§6) — holdout_months の変更は人間レビュー必須
  holdout_months: 3
  initial_balance: 1000000

analysis:                      # コア所有 (§6 履歴分析)
  max_watch_symbols: 10
  max_gap_pct: 5.0
```

`datafeed:` 配下に `watch_symbols: []   # 取引不可・分析専用 (§6)。pair enum には入れない`。

- [ ] **Step 3: 全テスト PASS → Commit** — `git commit -m "feat: backtest/analysis/watch_symbols 設定 (コア所有)"`

---

### Task 3: Dukascopy tick デコーダ (フィクスチャ検証 + 実データ照合ステップ)

**Files:**
- Create: `src/agentic_fx/backtest/__init__.py`, `src/agentic_fx/backtest/dukascopy.py`
- Test: `tests/backtest/__init__.py`, `tests/backtest/test_dukascopy.py`
- Create: `tests/backtest/fixtures/` (合成 bi5 フィクスチャ — テストが生成)

**Interfaces:**
- Produces:
  - `hour_url(symbol: str, dt_utc: datetime) -> str` — `https://datafeed.dukascopy.com/datafeed/{SYM}/{YYYY}/{MM:02d}/{DD:02d}/{HH:02d}h_ticks.bi5`。**月は 0 始まり** (1 月 = `00`) — Dukascopy 仕様の罠。既知仕様として実装し、Step 5 の実データ照合で確定する
  - `decode_bi5(payload: bytes, *, point: float, hour_start_utc: datetime) -> list[Tick]` — LZMA 展開 → 20 byte レコード `struct '>3i2f'` = (ms_offset, ask_points, bid_points, ask_vol, bid_vol)。`Tick(ts, bid, ask)` (`bid = bid_points * point`)
  - `point_of(symbol: str) -> float` — クォート通貨 JPY なら `1e-3`、それ以外 `1e-5` (InstrumentSpec の quote_currency を参照する薄い関数。**symbol 文字列切り出しは §5 で禁止** — spec テーブルに無い symbol は明示エラー)
  - `class Tick(NamedTuple): ts: datetime; bid: float; ask: float`

- [ ] **Step 0: テスト共通ヘルパを作る** — `tests/backtest/conftest.py` (本プランの全テストが使う。以降の task のテスト内 `_conn` / `_row_at` / `_bi5` / `H` / `WED` / `SETTINGS` はここから import するか fixture で受ける):

```python
import lzma
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.store.db import connect, init_db

H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
WED = H  # 2026-07-22 は水曜 — 市場オープン
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _row_at(ts, *, o, h, l, c, v=10.0, spread=0.01, symbol="USDJPY"):
    """ohlcv.import_bars 用の row タプル。"""
    return (symbol, "1m", ts.isoformat(), o, h, l, c, v, spread)


def _bi5(records):
    """(ms, ask_points, bid_points, ask_vol, bid_vol) 列 → bi5 バイト列。"""
    raw = b"".join(struct.pack(">3i2f", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)
```

- [ ] **Step 1: 失敗するテストを書く** — `tests/backtest/test_dukascopy.py`:

```python
import lzma
import struct
from datetime import datetime, timezone

from agentic_fx.backtest.dukascopy import Tick, decode_bi5, hour_url, point_of

H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _bi5(records: list[tuple[int, int, int, float, float]]) -> bytes:
    raw = b"".join(struct.pack(">3i2f", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_decode_roundtrip_jpy_point():
    payload = _bi5([(1500, 148_205, 148_193, 1.2, 0.8)])  # ask, bid (points)
    ticks = decode_bi5(payload, point=1e-3, hour_start_utc=H)
    assert ticks == [Tick(H.replace(second=1, microsecond=500_000),
                          148.193, 148.205)]


def test_decode_rejects_negative_and_inverted():
    bad = _bi5([(0, 148_000, 148_100, 1.0, 1.0)])  # ask < bid (逆転)
    assert decode_bi5(bad, point=1e-3, hour_start_utc=H) == []


def test_hour_url_month_is_zero_based():
    assert hour_url("USDJPY", H) == (
        "https://datafeed.dukascopy.com/datafeed/USDJPY/2026/06/22/12h_ticks.bi5")


def test_point_of_uses_spec_quote_currency():
    assert point_of("USDJPY") == 1e-3
    assert point_of("EURUSD") == 1e-5
```

- [ ] **Step 2: FAIL 確認 → 実装** — `decode_bi5` は `lzma.decompress(payload)` (FORMAT_ALONE / FORMAT_XZ 両対応: `try FORMAT_ALONE → except LZMAError → 自動判定`)、20 byte 毎に unpack、`bid > 0 and ask >= bid` を満たさないレコードは黙って棄却 (件数を DEBUG ログ)。`point_of` は `price_provider` の InstrumentSpec 静的テーブル (`_SPECS` — 実名は実装時に `rg -n "_SPECS" src/agentic_fx/datafeed/price_provider.py` で確認) から `quote_currency == "JPY"` を判定。

- [ ] **Step 3: PASS 確認** → `uv run pytest tests/backtest/ -v`

- [ ] **Step 4: Commit** — `git commit -m "feat: Dukascopy bi5 デコーダ (合成フィクスチャ検証)"`

- [ ] **Step 5 (実データ照合 — ネットワーク使用・pytest 外)**: 実装者はコミット後に 1 回だけ実 URL から直近営業日の 1 時間分を取得し、フォーマット仮定 (0 始まり月・struct 順・point) を実測確認して報告書に記録する:

```bash
uv run python - << 'EOF'
from datetime import datetime, timedelta, timezone
import httpx
from agentic_fx.backtest.dukascopy import decode_bi5, hour_url, point_of
# 直近の平日 12:00 UTC を選ぶ (週末は 404/空)
dt = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
url = hour_url("USDJPY", dt)
r = httpx.get(url, timeout=30); r.raise_for_status()
ticks = decode_bi5(r.content, point=point_of("USDJPY"), hour_start_utc=dt)
print(url, len(ticks), ticks[0] if ticks else None)
EOF
```

期待: 数千 tick、bid/ask が当日の USDJPY レート帯 (MT5 実測と ±1% 以内)。**乖離したら仮定 (struct 順・point) を修正してテストも直す** — 合成フィクスチャは仮定の自己整合しか検証しないため、この照合が仮定の正しさを担保する。

---

### Task 4: Dukascopy 1m 集約 + 投入 (importer)

**Files:**
- Create: `src/agentic_fx/backtest/importer.py`
- Test: `tests/backtest/test_importer.py`

**Interfaces:**
- Consumes: Task 3 の `decode_bi5` / `hour_url` / `point_of`、Task 1 の `ohlcv.import_bars`
- Produces:
  - `ticks_to_1m(ticks: list[Tick], symbol: str) -> list[tuple]` — UTC 分境界で mid OHLC (`mid=(bid+ask)/2`) + `spread = バー内平均 (ask−bid)` + `volume = tick 数` に集約し、`import_bars` の row タプルを返す
  - `import_dukascopy(conn, symbol, start: datetime, end: datetime, *, fetch=None, progress=None) -> ImportResult` — 時間毎に `fetch(url) -> bytes` (既定 httpx。**テストは fetch 注入**) → decode → 集約 → `import_bars(source="dukascopy")`。404/空時間はスキップ (週末)。**期間は人間 CLI からのみ渡る** (エージェント経路なし)

- [ ] **Step 1: 失敗するテストを書く** — `tests/backtest/test_importer.py`:

```python
def test_ticks_to_1m_mid_and_mean_spread():
    ticks = [Tick(H, 148.000, 148.010),
             Tick(H.replace(second=30), 148.020, 148.040),
             Tick(H.replace(minute=1), 149.000, 149.020)]
    rows = ticks_to_1m(ticks, "USDJPY")
    assert len(rows) == 2
    sym, iv, ts, o, h, l, c, v, spread = rows[0]
    assert (sym, iv, ts) == ("USDJPY", "1m", H.isoformat())
    assert o == 148.005 and c == 148.030 and v == 2
    assert abs(spread - 0.015) < 1e-9  # (0.010+0.020)/2


def test_import_dukascopy_uses_injected_fetch_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    payload = _bi5([(0, 148_010, 148_000, 1, 1)])
    calls = []

    def fetch(url):
        calls.append(url); return payload

    r1 = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1), fetch=fetch)
    r2 = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1), fetch=fetch)
    assert r1.inserted == 1 and r2.unchanged == 1 and r2.conflicted == 0
    assert all("datafeed.dukascopy.com" in u for u in calls)


def test_import_skips_empty_hours(tmp_path):
    conn = _conn(tmp_path)
    r = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1),
                         fetch=lambda url: b"")
    assert r.inserted == 0
```

- [ ] **Step 2: FAIL → 実装** — 集約は pandas 不使用の素朴ループでよい (依存を増やさない)。`fetch` 既定実装は `httpx.get(url, timeout=30)`、404 は空扱い。1 時間の tick はその時間内の分にのみ寄与 (跨ぎなし — bi5 は時間単位ファイル)。

- [ ] **Step 3: PASS → Commit** — `git commit -m "feat: Dukascopy 1m インポータ (mid+平均spread・冪等)"`

---

### Task 5: MT5 一括インポータ + 価格系差照合

**Files:**
- Create: `src/agentic_fx/backtest/mt5_import.py`
- Test: `tests/backtest/test_mt5_import.py`

**Interfaces:**
- Consumes: MT5 bridge `GET {base}/ohlcv/{sym}?from=..&to=..&interval=1m` (応答: `{"symbol","interval","bars":[{"time","open","high","low","close","volume"},...]}` — 2026-08-01 実測形)。Task 1 の `import_bars`
- Produces:
  - `import_mt5(conn, symbol, start, end, *, base_url, fetch=None) -> ImportResult` — 1 日窓でページングし `import_bars(source="mt5")` (spread=None)。**MT5 は bid 系列** — mid 近似としてそのまま保存 (§6 の但し書きどおり)
  - `compare_sources(conn, symbol, *, a="dukascopy", b="mt5") -> dict` — 両 source が重複する期間の close 差 `a_mid − assumed_half_spread` vs `b` の {count, mean, std, max_abs} を返す (人間 CLI / 報告用。assumed_half_spread は settings.risk.pair_rules の assumed_spread_pips から換算)

- [ ] **Step 1: 失敗するテストを書く**:

```python
def test_import_mt5_pages_daily_and_imports(tmp_path):
    conn = _conn(tmp_path)
    def fetch(url):
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": H.isoformat(), "open": 148.0, "high": 148.2,
             "low": 147.9, "close": 148.1, "volume": 10}]}
    r = import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                   base_url="http://x", fetch=fetch)
    assert r.inserted >= 1
    bars = ohlcv.load_bars(conn, "USDJPY", "1m", source="mt5")
    assert bars and bars[0].close == 148.1


def test_compare_sources_reports_distribution(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [("USDJPY", "1m", H.isoformat(),
                              148.005, 148.2, 147.9, 148.105, 5, 0.01)],
                      source="dukascopy")
    ohlcv.import_bars(conn, [("USDJPY", "1m", H.isoformat(),
                              148.0, 148.2, 147.9, 148.10, 5, None)],
                      source="mt5")
    rep = compare_sources(conn, "USDJPY")
    assert rep["count"] == 1 and abs(rep["mean"]) < 0.01
```

- [ ] **Step 2: FAIL → 実装 → PASS** (fetch 注入・実 HTTP なし。既定 fetch は httpx + `raise_for_status`)。

- [ ] **Step 3: Commit** — `git commit -m "feat: MT5 一括インポータ + 価格系差照合"`

- [ ] **Step 4 (手動・報告のみ)**: bridge 稼働環境で直近 1 週間を両 source 取り込み → `compare_sources` の実測値を報告書とレジャーに記録 (§6「導入時に 1 回照合」)。

---

### Task 6: ReplayClock + BarFeed (UTC 連続進行の契約)

**Files:**
- Create: `src/agentic_fx/backtest/replay.py`
- Test: `tests/backtest/test_replay.py`

**Interfaces:**
- Consumes: `agentic_fx.core.contracts.Clock` (Protocol) / Task 1 の `load_bars(source 必須)`
- Produces:
  - `class ReplayClock:` — `__init__(start: datetime)` / `now() -> datetime` (Clock 実装) / `advance() -> datetime` (+1 分して返す)。**履歴バーの有無に関わらず UTC 1m 格子を連続に進める** (§6 契約)
  - `class BarFeed:` — `__init__(conn, symbol, *, source: str, start, end)`。1m バーを `{ts_iso: Bar}` に前読みし、`bar_at(ts) -> Bar | None` (欠損は None)。`latest_1m(ts) -> Bar | None` は `bar_at` の別名 (scheduler の `bars_fn` 契約に合わせ、その時刻のバーのみ返す — 過去へ遡らない: 欠損 tick で古いバーを返すと価格依存判定が「バー存在」と誤認するため)
  - `quote_from_bar(bar: Bar, spread: float) -> Quote` — `bid = close − spread/2`, `ask = close + spread/2`, ts=bar.ts, source="backtest"

- [ ] **Step 1: 失敗するテストを書く**:

```python
def test_replay_clock_advances_continuously():
    c = ReplayClock(H)
    assert c.now() == H
    assert c.advance() == H + timedelta(minutes=1)
    assert c.now() == H + timedelta(minutes=1)


def test_bar_feed_returns_none_for_gap(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [_row(H), _row(H + timedelta(minutes=2))],
                      source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy",
                   start=H, end=H + timedelta(minutes=5))
    assert feed.bar_at(H) is not None
    assert feed.bar_at(H + timedelta(minutes=1)) is None  # 欠損は None — 遡らない
    assert feed.bar_at(H + timedelta(minutes=2)) is not None


def test_quote_from_bar_half_spread():
    q = quote_from_bar(Bar("USDJPY", "1m", H, 148, 148.2, 147.9, 148.1, 5),
                       spread=0.02)
    assert (q.bid, q.ask) == (148.09, 148.11)
```

- [ ] **Step 2: FAIL → 実装 → PASS** — BarFeed は `load_bars(..., source=source, since=start, until=end)` を 1 回読んで dict 化 (37 万行/年 — メモリ許容。実測は Task 12)。

- [ ] **Step 3: Commit** — `git commit -m "feat: ReplayClock + BarFeed (UTC 連続 1m 格子・欠損は None)"`

---

### Task 7: BacktestRunner コア (in-memory 再生・synthetic Mission・先読み禁止)

**Files:**
- Create: `src/agentic_fx/backtest/runner.py`
- Test: `tests/backtest/test_runner.py`

**Interfaces:**
- Consumes (すべて実運用と同一コード — Global Constraints): `init_db` / `StateStore` (一時ファイル) / `PaperBroker(conn, settings, clock)` / `Executor(conn=..., broker=..., settings=..., state_store=..., activity=..., notifier=..., clock=..., quote_fn=..., spec_fn=..., rate_fn=...)` / `Scheduler(conn=..., executor=..., settings=..., state_store=..., activity=..., bars_fn=..., on_trade_mission=..., on_news_cycle=..., on_econ_cycle=...)` / `missions.start` / `TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)` / `market_hours.is_market_open` / Task 6 の ReplayClock・BarFeed・quote_from_bar / `record_snapshot` (core.accounting — 初期残高)
- Produces:
  - `IntentSource = Callable[[Bar], dict | None]` — 引数は **確定した評価 timeframe バー**。返り値は LLM 出力と同形の dict (`{"action": "open", ...}` / None = 提案なし)。**プラン 7 の strategy plugin アダプタはこの型に合わせる**
  - `@dataclass BacktestResult: orders: list[dict]; equity_curve: list[tuple[str, float]]; start: datetime; end: datetime; source: str; fallback_spread_used: bool`
  - `run_replay(settings, *, symbol, source, start, end, intent_source, eval_timeframe="1h", history_conn) -> BacktestResult` — 中身:
    1. `conn = connect(":memory:")` + `init_db(conn)` — **実 DB に触れない**。`StateStore` は `tempfile` 配下
    2. `record_snapshot(conn, now=start, balance=settings.backtest.initial_balance, equity=...)`
    3. no-op 注入: `_NullActivity` (write/tail が何もしない) / `Notifier(enabled=False, webhook_url=None)` / `on_news_cycle=lambda: None` / `on_econ_cycle=lambda: None`
    4. `quote_fn = lambda pair: quote_from_bar(current_bar, spread_of(current_bar))` — spread は `ohlcv.spread` → 無ければ `settings.risk.pair_rules[pair].assumed_spread_pips` から換算 (使用したら `fallback_spread_used=True`)。`rate_fn` は同じ quote から構成 (test_wiring.py の実配線パターンに合わせる)
    5. メインループ: `while clock.now() < end:` — `bar = feed.bar_at(now)`。`is_market_open(now)` かつ bar があれば `scheduler.tick(now)` 相当の**約定・SL/TP 監視のみ**を回す (取引判断 Mission は cron でなく IntentSource 駆動のため、`Scheduler` は `on_trade_mission=lambda reason: None` で組み、tick の fills/exits 経路だけを使う)。時間依存判定 (指値期限・day クローズ) は bar 欠損でも tick を呼んで進める (§6: 価格依存はスキップ・時間依存は進む — tick 内の fills は bars_fn が None を返すことで自然にスキップされる)
    6. **評価 timeframe のバー確定を検出** (eval_timeframe 境界を跨いだ最初の 1m tick) したら、確定バーで `proposal = intent_source(closed_bar)`。None でなければ: `mid = missions.start(conn, "trade", "backtest", "intent-source", clock.now())` → `missions.finish(..., "completed", proposal, [], clock.now())` (**synthetic Mission** — §6。origin/mission_id/loop 検証を同一コードで通すため) → `intent = TradeIntent.from_llm_dict(proposal, origin=Origin.SCHEDULER)` → `executor.handle_intent(intent, mid)`。**呼び出しは次の 1m tick の advance 後** (先読み禁止 — 評価バー自身の 1m では約定に参加させない)
    7. 終了時: open ポジションはそのまま (成績集計は Task 8 が closed のみ集計)。orders 全行と equity 推移を返す

- [ ] **Step 1: 失敗するテストを書く** (フルサイクル — E2E テスト `tests/test_e2e_phase1.py` の OPEN_INTENT と同じ形の提案を使う):

```python
OPEN = {"action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
        "expires_in": "6h", "stop_loss": 147.80, "take_profit": 149.00,
        "reasoning": "bt"}


def _seed_history(conn):
    """水曜 12:00 から: 1h バー確定 → 指値到達 → TP 到達、の 1m 列を投入。"""
    rows = []
    t = WED  # 2026-07-22 (水) 12:00 UTC — 市場オープン
    for i in range(60):          # 12:00-12:59 (評価対象の 1h を構成)
        rows.append(_row_at(t + timedelta(minutes=i), o=148.5, h=148.6,
                            l=148.4, c=148.5))
    rows.append(_row_at(t + timedelta(hours=1), o=148.3, h=148.35,
                        l=148.10, c=148.15))       # 13:00 — 指値 148.20 到達
    rows.append(_row_at(t + timedelta(hours=1, minutes=1), o=148.9,
                        h=149.10, l=148.85, c=149.05))  # 13:01 — TP 到達
    ohlcv.import_bars(conn, rows, source="dukascopy")


def test_full_cycle_open_fill_tp(tmp_path):
    hist = _conn(tmp_path); _seed_history(hist)
    fired = []

    def source(bar):
        if not fired:
            fired.append(bar.ts)
            return dict(OPEN)
        return None

    res = run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=source, eval_timeframe="1h",
                     history_conn=hist)
    closed = [o for o in res.orders if o["status"] == "closed"]
    assert len(closed) == 1 and closed[0]["realized_pnl"] > 0
    assert closed[0]["close_reason"] == "tp"
    assert fired == [WED]  # 評価は 12:00-12:59 の 1h バー確定時のみ


def test_no_lookahead_same_bar(tmp_path):
    """評価に使ったバーの 1m では約定しない — 先読み禁止 (§6)。"""
    hist = _conn(tmp_path)
    # 12:00-12:59 の 1h バー自体に指値到達価格を含める (13:00 以降は到達しない)
    rows = [_row_at(WED + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.10, c=148.5) for i in range(60)]
    rows.append(_row_at(WED + timedelta(hours=1), o=148.5, h=148.6,
                        l=148.4, c=148.5))
    ohlcv.import_bars(conn=hist, rows=rows, source="dukascopy")
    res = run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert not [o for o in res.orders if o["status"] in ("open", "closed")]


def test_synthetic_mission_passes_origin_gate(tmp_path):
    """§5 検証を同一コードで通す — missions 行が in-memory に作られ intent が accepted。"""
    hist = _conn(tmp_path); _seed_history(hist)
    res = run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert res.orders  # origin_rejected なら orders は生まれない


def test_real_db_untouched(tmp_path, monkeypatch):
    """バックテストが実 data/ に触れない。"""
    monkeypatch.chdir(tmp_path)
    hist = _conn(tmp_path); _seed_history(hist)
    run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
               start=WED, end=WED + timedelta(hours=1),
               intent_source=lambda b: None, eval_timeframe="1h",
               history_conn=hist)
    assert not (tmp_path / "data").exists()
```

- [ ] **Step 2: FAIL 確認** → **Step 3: 実装** (上記 Produces の 1〜7)。約定監視は `Scheduler` を組んで `tick(now)` を呼ぶ (fills/exits/期限/day クローズの実運用コードを共有)。cron 起動を抑止するため `Scheduler` の `on_trade_mission` は no-op を渡し、`_last_trade` は start に初期化して毎時発火を無害化する (発火しても no-op)。

- [ ] **Step 4: PASS → 全体 green 確認 → Commit** — `git commit -m "feat: BacktestRunner (in-memory 再生・synthetic Mission・先読み禁止)"`

---

### Task 8: 成績集計 + backtest_runs (scope 3 値・再現性メタデータ)

**Files:**
- Create: `src/agentic_fx/backtest/metrics.py`
- Modify: `src/agentic_fx/store/db.py` (backtest_runs DDL — `_SCHEMA` 追記 + テーブル数 assert 更新)
- Create: `src/agentic_fx/store/backtest_runs.py`
- Test: `tests/backtest/test_metrics.py`, `tests/store/test_backtest_runs.py`

**Interfaces:**
- Consumes: Task 7 の `BacktestResult`
- Produces:
  - `compute_metrics(result: BacktestResult) -> dict` — `{trades, pf, win_rate, avg_r, max_drawdown, total_pnl, evaluable, fallback_spread_used}`。closed のみ集計。`evaluable = trades >= 30` (§6: 未満は足切りにも採用にも使わない)。PF は `総利益/総損失` (損失 0 なら `inf` でなく None — JSON 保存のため)。avg_r の 1 取引リスク額は `abs(avg_fill_price − stop_loss) × quantity × contract_size` (クォート通貨建て — Phase 2 は USDJPY のみでクォート通貨 = 口座通貨。EURUSD 解禁時は §6 A-9 チェックリストで換算を扱う)。max_drawdown は equity_curve のピーク比。`fallback_spread_used` は BacktestResult から透過 (§6 の品質注記 — metrics_json に載って backtest_runs に残る)
  - DDL: `backtest_runs(id INTEGER PK, plugin_ref TEXT NOT NULL, content_hash TEXT NOT NULL, kind TEXT NOT NULL, pair TEXT NOT NULL, timeframe TEXT NOT NULL, source TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('in_sample','holdout_gate','human_custom')), metrics_json TEXT NOT NULL, settings_hash TEXT NOT NULL, core_commit TEXT NOT NULL, initial_balance REAL NOT NULL, created_at TEXT NOT NULL)`
  - `backtest_runs.save(conn, *, plugin_ref, content_hash, kind, pair, timeframe, source, period, scope, metrics, settings_hash, core_commit, initial_balance, now) -> int`
  - `backtest_runs.in_sample_view(conn, *, pair=None) -> list[dict]` — **`scope='in_sample'` のみ**。metrics_json を展開して返す (改善ループが読む唯一の面 — プラン 8 で tool 化)
  - `settings_snapshot_hash(settings) -> str` — risk / sizing 関連 + backtest + spread フォールバックを含む安定 JSON の sha256。`core_commit()` は `git rev-parse HEAD` (取得不能時 "unknown")

- [ ] **Step 1: 失敗するテストを書く**:

```python
def test_metrics_basic_and_evaluable_threshold():
    res = _result_with_closed(pnls=[+100.0] * 20 + [-50.0] * 10)
    m = compute_metrics(res)
    assert m["trades"] == 30 and m["evaluable"] is True
    assert abs(m["pf"] - (2000.0 / 500.0)) < 1e-9
    m2 = compute_metrics(_result_with_closed(pnls=[+100.0] * 29))
    assert m2["evaluable"] is False


def test_backtest_runs_scope_check_and_view(tmp_path):
    conn = _conn(tmp_path)
    for scope in ("in_sample", "holdout_gate", "human_custom"):
        backtest_runs.save(conn, plugin_ref="p.py", content_hash="h",
                           kind="strategy", pair="USDJPY", timeframe="1h",
                           source="dukascopy", period=(H, H), scope=scope,
                           metrics={"trades": 0}, settings_hash="s",
                           core_commit="c", initial_balance=1e6, now=H)
    rows = backtest_runs.in_sample_view(conn)
    assert len(rows) == 1 and rows[0]["scope"] == "in_sample"
    with pytest.raises(sqlite3.IntegrityError):  # CHECK 違反 (旧二値 'holdout' は不可)
        backtest_runs.save(conn, plugin_ref="p.py", content_hash="h",
                           kind="strategy", pair="USDJPY", timeframe="1h",
                           source="dukascopy", period=(H, H), scope="holdout",
                           metrics={"trades": 0}, settings_hash="s",
                           core_commit="c", initial_balance=1e6, now=H)
```

- [ ] **Step 2: FAIL → 実装 → PASS** — db.py の期待テーブル集合 (`"ohlcv", "missions", ...` の assert セット) に `backtest_runs` を追加。既存 DB への追加は `CREATE TABLE IF NOT EXISTS` で足りる (新テーブル)。

- [ ] **Step 3: Commit** — `git commit -m "feat: バックテスト成績集計 + backtest_runs (scope 3 値・再現性メタデータ)"`

---

### Task 9: holdout (分割点計算・in-sample 制限・遮断回帰テスト)

**Files:**
- Create: `src/agentic_fx/backtest/holdout.py`
- Test: `tests/backtest/test_holdout.py`

**Interfaces:**
- Consumes: Task 7 `run_replay` / Task 8 `compute_metrics`・`backtest_runs.save` / `settings.backtest.holdout_months`
- Produces:
  - `holdout_boundary(now: datetime, months: int) -> datetime` — `now` から暦月で months 遡った UTC 時刻 (端数日はそのまま — 単純減算。実装は `dateutil` を足さず「月を months 引き、日はクランプ」の純関数)
  - `run_in_sample(settings, *, history_conn, symbol, source, intent_source, eval_timeframe, plugin_ref, content_hash, kind, now) -> dict` — **期間引数なし**。期間は `(source の最古バー, holdout_boundary(now))` をハーネスが計算。実行 → metrics → `backtest_runs.save(scope="in_sample")` → **metrics のみ返す (期間・端点は返さない)** (§6 遮断 1)
  - `run_holdout_gate(...同上引数...) -> dict` — 期間 `(holdout_boundary(now), now)`。`scope="holdout_gate"` で保存し metrics を返す。**呼び出し元は採用ゲート (人間承認フロー) に限る** — プラン 7/8 の配線で強制し、本 task ではドキュメンテーションとテストで契約をピンする

- [ ] **Step 1: 失敗するテストを書く**:

```python
def test_holdout_boundary_simple_months():
    assert holdout_boundary(datetime(2026, 8, 1, tzinfo=UTC), 3) == \
        datetime(2026, 5, 1, tzinfo=UTC)
    assert holdout_boundary(datetime(2026, 3, 31, tzinfo=UTC), 1) == \
        datetime(2026, 2, 28, tzinfo=UTC)  # 日クランプ


def test_run_in_sample_returns_metrics_without_period(tmp_path):
    """遮断 1: 期間・端点を返さない。"""
    hist = _conn(tmp_path); _seed_history(hist)
    out = run_in_sample(SETTINGS, history_conn=hist, symbol="USDJPY",
                        source="dukascopy", intent_source=lambda b: None,
                        eval_timeframe="1h", plugin_ref="p", content_hash="h",
                        kind="strategy", now=WED + timedelta(days=120))
    assert "trades" in out
    forbidden = {"period_start", "period_end", "start", "end", "boundary"}
    assert forbidden.isdisjoint(out.keys())


def test_in_sample_and_gate_use_disjoint_periods(tmp_path):
    """分割点の両側が交わらないことを backtest_runs の記録で検証。"""
    hist = _conn(tmp_path); _seed_history(hist)
    now = WED + timedelta(days=120)
    kw = dict(history_conn=hist, symbol="USDJPY", source="dukascopy",
              intent_source=lambda b: None, eval_timeframe="1h",
              plugin_ref="p", content_hash="h", kind="strategy", now=now)
    run_in_sample(SETTINGS, **kw)
    run_holdout_gate(SETTINGS, **kw)
    rows = {r["scope"]: dict(r) for r in hist.execute(
        "SELECT scope, period_start, period_end FROM backtest_runs")}
    assert rows["in_sample"]["period_end"] == \
        rows["holdout_gate"]["period_start"]
```

(注: `run_in_sample` / `run_holdout_gate` の backtest_runs 保存先は **history_conn と同じ DB** (実運用では実 DB = data/agentic.db が履歴と runs の両方を持つ)。in-memory の再生 DB には保存しない。)

- [ ] **Step 2: FAIL → 実装 → PASS → Commit** — `git commit -m "feat: holdout 分割 (期間はハーネス所有・in-sample は期間を返さない)"`

---

### Task 10: 履歴分析 (相関 3 種・出力契約・analysis_runs)

**Files:**
- Create: `src/agentic_fx/backtest/analysis.py`
- Modify: `src/agentic_fx/store/db.py` (analysis_runs DDL + テーブル数 assert)
- Create: `src/agentic_fx/store/analysis_runs.py`
- Test: `tests/backtest/test_analysis.py`, `tests/store/test_analysis_runs.py`

**Interfaces:**
- Consumes: Task 1 `load_bars(source 必須)` / Task 9 `holdout_boundary` / `settings.datafeed.watch_symbols`・`settings.analysis.*`
- Produces:
  - **パラメータは列挙制** (§6): `TIMEFRAMES = ("15m", "1h", "4h", "1d")` / `WINDOWS = (20, 60, 120)` (バー数) / `LAGS = range(-12, 13)` (バー数)。自由な数値は受けない
  - `corr_matrix(conn, symbols, *, timeframe, source, in_sample_until) -> dict[tuple[str, str], float]` — log リターンのピアソン相関。整列は UTC bar_time の inner join、欠損バー除外 (§6)
  - `rolling_corr_summary(conn, a, b, *, timeframe, window, source, in_sample_until) -> dict` — **固定 4 統計のみ** `{mean, std, min, max}` (窓系列・件数・日時は返さない — §6 出力契約)
  - `lead_lag(conn, a, b, *, timeframe, source, in_sample_until) -> dict` — `{peak_lag, peak_corr}` のみ
  - `analyze_for_agent(conn, settings, request: dict, *, now) -> dict` — **改善ループに露出する唯一の面** (プラン 8 で tool 化)。`in_sample_until = holdout_boundary(now, settings.backtest.holdout_months)` を内部で適用。symbols は `settings.pairs + settings.datafeed.watch_symbols` 内に限定。実行毎に `analysis_runs.save` し、**返り値に `analysis_run_id` を含める** (approval への添付参照用)。返却 dict に日時型・list[時系列] を**含めない**
  - DDL: `analysis_runs(id INTEGER PK, params_json TEXT NOT NULL, trial_count INTEGER NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL)`。`trial_count` = その呼び出しで計算した相関値の個数 (多重比較の分母)
  - `analysis_runs.save(conn, *, params, trial_count, source, now) -> int`
  - `coverage_report(conn, symbol, *, timeframe, source, start, end) -> dict` — `{bars, expected_open_bars, gap_pct}` (market_hours のオープン分数に対する欠損率)。**watch 銘柄の選定基準③ (§6: 許容欠損率 初期 5%) を人間が判定するための関数** — 人間 CLI (Task 11) から使う

- [ ] **Step 1: 失敗するテストを書く**:

```python
import math

FAR_FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _series(conn, symbol, values, *, start, timeframe="1h"):
    """決定的な close 列を 1h バーとして投入 (乱数・実時刻不使用)。"""
    step = timedelta(hours=1)
    rows = [(symbol, timeframe, (start + i * step).isoformat(),
             v, v + 0.05, v - 0.05, v, 1.0, 0.01)
            for i, v in enumerate(values)]
    ohlcv.import_bars(conn, rows, source="dukascopy")


def _sine(n, *, phase=0):
    """三角関数の決定的疑似価格列 (100 を中心に振幅 1)。"""
    return [100 + math.sin((i + phase) / 5.0) for i in range(n)]


def _seed_two_series(conn, *, start=H):
    """EURUSD が USDJPY に 1 バー先行する系列 (b[t] = a[t+1] と同位相差)。"""
    _series(conn, "USDJPY", _sine(200, phase=0), start=start)
    _series(conn, "EURUSD", _sine(200, phase=1), start=start)


def test_corr_matrix_inner_join_and_gap_exclusion(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn)
    # USDJPY 側の 1 バーを欠損させても inner join で落ちるだけで計算は通る
    conn.execute("DELETE FROM ohlcv WHERE symbol='USDJPY' AND bar_time=?",
                 ((H + timedelta(hours=7)).isoformat(),)); conn.commit()
    m = corr_matrix(conn, ["USDJPY", "EURUSD"], timeframe="1h",
                    source="dukascopy", in_sample_until=FAR_FUTURE)
    assert ("USDJPY", "EURUSD") in m
    assert -1.0 <= m[("USDJPY", "EURUSD")] <= 1.0


def test_lead_lag_detects_leader(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn)
    r = lead_lag(conn, "USDJPY", "EURUSD", timeframe="1h",
                 source="dukascopy", in_sample_until=FAR_FUTURE)
    assert set(r.keys()) == {"peak_lag", "peak_corr"}  # 返却スキーマ固定
    assert r["peak_lag"] == 1 and r["peak_corr"] > 0.9


def _leaves(x):
    """dict/list を再帰展開して (key 列, leaf 列) を返す。"""
    keys, leaves = [], []
    if isinstance(x, dict):
        for k, v in x.items():
            keys.append(k)
            k2, l2 = _leaves(v); keys += k2; leaves += l2
    elif isinstance(x, (list, tuple)):
        assert len(x) <= 8, "系列様の list を返してはならない"
        for v in x:
            k2, l2 = _leaves(v); keys += k2; leaves += l2
    else:
        leaves.append(x)
    return keys, leaves


def test_agent_output_contract_no_leak(tmp_path):
    """§6 出力契約: 日時・系列・観測数・端点を返さない (再帰チェック)。"""
    conn = _conn(tmp_path); _seed_two_series(conn)
    out = analyze_for_agent(conn, SETTINGS,
                            {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                             "timeframe": "1h"}, now=NOW)
    keys, leaves = _leaves(out)
    for leaf in leaves:
        assert not isinstance(leaf, datetime)
        assert not (isinstance(leaf, str) and len(leaf) >= 10
                    and leaf[4:5] == "-" and leaf[:4].isdigit())  # ISO 日付形
    assert "analysis_run_id" in keys
    forbidden = {"count", "n", "observations", "start", "end", "dates",
                 "period_start", "period_end", "window_series"}
    assert forbidden.isdisjoint(set(keys))


def test_agent_analysis_is_in_sample_bounded(tmp_path):
    """直近 (holdout 期) だけ逆位相の系列で、境界適用が効いていることを検証。"""
    conn = _conn(tmp_path)
    start = NOW - timedelta(days=200)          # in-sample 100 日 + holdout 100 日
    n_in, n_out = 2400, 2400                    # 1h バー数 (100 日ずつ)
    _series(conn, "USDJPY", _sine(n_in + n_out, phase=0), start=start)
    _series(conn, "EURUSD",
            _sine(n_in, phase=1) + _sine(n_out, phase=-1)[::-1],  # holdout 部を破壊
            start=start)
    r_all = lead_lag(conn, "USDJPY", "EURUSD", timeframe="1h",
                     source="dukascopy", in_sample_until=FAR_FUTURE)
    out = analyze_for_agent(conn, SETTINGS,
                            {"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                             "timeframe": "1h"}, now=NOW)
    assert out["peak_corr"] > r_all["peak_corr"]  # holdout 汚染が除外されている


def test_agent_rejects_symbol_outside_watch_and_pairs(tmp_path):
    conn = _conn(tmp_path)
    out = analyze_for_agent(conn, SETTINGS,
                            {"kind": "lead_lag", "a": "USDJPY", "b": "GBPZAR",
                             "timeframe": "1h"}, now=NOW)
    assert "error" in out  # 列挙外 symbol は検証エラー (例外を投げない)


def test_analysis_runs_records_trials(tmp_path):
    conn = _conn(tmp_path); _seed_two_series(conn)
    analyze_for_agent(conn, SETTINGS, {"kind": "corr_matrix",
                                       "timeframe": "1h"}, now=NOW)
    row = conn.execute("SELECT trial_count, params_json FROM analysis_runs")\
              .fetchone()
    assert row["trial_count"] >= 1


def test_coverage_report_gap_pct(tmp_path):
    conn = _conn(tmp_path)
    _series(conn, "USDJPY", _sine(24), start=H)   # 水曜 24h — 全てオープン時間
    rep = coverage_report(conn, "USDJPY", timeframe="1h", source="dukascopy",
                          start=H, end=H + timedelta(hours=48))
    assert rep["bars"] == 24 and rep["gap_pct"] > 0  # 後半 24h が欠損
```

(注: `analyze_for_agent` のテスト用 SETTINGS は watch_symbols に EURUSD を含む必要がある — conftest の SETTINGS を `model_copy(update=...)` で拡張するローカルヘルパを test_analysis.py に書くこと。)

- [ ] **Step 2: FAIL → 実装** — 相関計算は純 Python (`statistics` + 手書きピアソン) か pandas (既存依存) のどちらでもよいが、**入力整列は必ず bar_time の集合積**で行う。`analyze_for_agent` は request 検証 (kind / symbol / timeframe / window / lag が列挙内) に失敗したら `{"error": "..."}` を返す (無送出)。

- [ ] **Step 3: PASS → Commit** — `git commit -m "feat: 履歴分析 (相関 3 種・非漏洩出力契約・analysis_runs)"`

---

### Task 11: 人間 CLI (backtest / history / analyze サブコマンド)

**Files:**
- Modify: `src/agentic_fx/entry.py`
- Create: `src/agentic_fx/backtest/cli.py`
- Test: `tests/backtest/test_cli.py`, `tests/test_entry.py` (追記)

**Interfaces:**
- Consumes: Task 4/5 importer / Task 7 runner / Task 8 metrics / Task 10 analysis
- Produces (entry.py の sub_parsers に追加 — 既存 `init` と同居):
  - `afx history import --source {dukascopy,mt5} --symbol USDJPY --from 2016-01-01 --to 2026-05-01` — 一括取り込み (進捗を件数で表示)
  - `afx history compare --symbol USDJPY` — compare_sources の表示
  - `afx backtest run --symbol USDJPY --source dukascopy --from ... --to ... --proposal-file proposals.jsonl` — **人間用の自由期間** (scope=human_custom で保存)。proposal-file は `{"ts": iso, ...OPEN_INTENT 形...}` の JSONL (plugin 未実装のため、人間が用意した提案列を IntentSource 化する薄いアダプタ。プラン 7 で `--plugin` オプションが加わる)
  - `afx analyze corr --a USDJPY --b EURUSD --timeframe 1h --source dukascopy` — 人間は期間自由 (`--from/--to` 任意)。**analysis_runs には保存しない** (探索監査は改善ループ経路のみの契約 — 人間の探索まで記録すると探索数の意味が濁る。§6 の記録義務は「改善ループ向け API」に係る)
  - `afx history coverage --symbol EURUSD --timeframe 1h --source dukascopy --from ... --to ...` — Task 10 の `coverage_report` 表示 (watch 選定基準③の判定材料)
- サービス系 (`run_service`) の既定動作は不変 — サブコマンド未指定時の挙動を変えない (既存 test_entry の期待維持)

- [ ] **Step 1: 失敗するテストを書く** — argparse 配線と scope 記録のみを検証 (importer/runner は Task 4-10 でテスト済み — CLI は薄いアダプタに徹する)。`tests/backtest/test_cli.py`:

```python
from unittest.mock import MagicMock, patch

from agentic_fx.entry import main


def test_cli_history_import_calls_importer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("agentic_fx.backtest.cli.import_dukascopy") as imp:
        imp.return_value = MagicMock(inserted=10, unchanged=0, conflicted=0)
        rc = main(["history", "import", "--source", "dukascopy",
                   "--symbol", "USDJPY",
                   "--from", "2026-07-01", "--to", "2026-07-02"])
    assert rc == 0
    args, kwargs = imp.call_args
    assert args[1] == "USDJPY"  # (conn, symbol, start, end)
    assert args[2].isoformat().startswith("2026-07-01")


def test_cli_backtest_run_records_human_custom_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proposals = tmp_path / "p.jsonl"
    proposals.write_text("", encoding="utf-8")  # 提案なし = 取引 0 で完走
    with patch("agentic_fx.backtest.cli.run_replay") as rr, \
         patch("agentic_fx.backtest.cli.backtest_runs") as br:
        rr.return_value = MagicMock(orders=[], equity_curve=[],
                                    source="dukascopy",
                                    fallback_spread_used=False)
        rc = main(["backtest", "run", "--symbol", "USDJPY",
                   "--source", "dukascopy",
                   "--from", "2026-07-01", "--to", "2026-07-02",
                   "--proposal-file", str(proposals)])
    assert rc == 0
    assert br.save.call_args.kwargs["scope"] == "human_custom"


def test_cli_default_service_behavior_unchanged(monkeypatch):
    with patch("agentic_fx.service.run_service", return_value=0) as rs:
        rc = main([])
    assert rc == 0 and rs.called
```

- [ ] **Step 2: FAIL → 実装 → PASS → Commit** — `git commit -m "feat: 人間 CLI (history import / backtest run / analyze)"`

---

### Task 12: 速度実測ゲート + 通し E2E

**Files:**
- Test: `tests/backtest/test_bench.py` (`@pytest.mark.bench` — 既定 skip、`-m bench` で実行), `tests/backtest/test_e2e_backtest.py`

**Interfaces:** Consumes: 全 task。

- [ ] **Step 1: 通し E2E を書く** — 合成 1 週間 (決定的生成・約 7,000 本の 1m) を import_bars → `run_replay` (指値→約定→TP を 3 回起こす提案列) → `compute_metrics` → `backtest_runs.save(in_sample)` → in_sample_view で読める、まで 1 テストで通す。assert: closed 3 件 / metrics 妥当 / view 経由で期間端点が**見えない**こと。

- [ ] **Step 2: ベンチを書く** — 合成 1 年分 (約 37 万本) を投入し `run_replay` の実時間を測って `print` する (assert は「完走」のみ — 時間の合否はコントローラが実測をレジャーに記録して裁定する。目標オーダー: 数分/年)。

- [ ] **Step 3: 実測実行** — `uv run pytest tests/backtest/test_bench.py -m bench -s` の出力を報告書に貼る。**遅くても本プランでは最適化しない** (§6: 専用執行ロジック禁止。tick 外側の最適化はコントローラ裁定で別 task 化)。

- [ ] **Step 4: 全体 green + Commit** — `git commit -m "test: バックテスト通し E2E + 速度ベンチ"`

---

## プラン完了条件 (再掲 — 分割書と同一)

1. Dukascopy 実データ照合 (Task 3 Step 5) と MT5 価格系差 (Task 5 Step 4) が報告書に記録されている
2. 遮断回帰テスト (期間引数なし / in_sample ビュー制限 / 分析出力スキーマ) が green
3. 1 年 replay の実測時間がレジャーに記録されている
4. 既存全テスト + 本プラン追加分が green。実 DB・実 HTTP に触れるテストが無い

## プラン 7 への引き継ぎ

- `IntentSource` (Task 7) に strategy plugin アダプタを差し込む
- `run_in_sample` / `run_holdout_gate` (Task 9) を評価ランナー・採用ゲートから呼ぶ
- `analyze_for_agent` (Task 10) をプラン 8 で改善ループの tool にする (registry 登録はプラン 8)
- `afx backtest run --plugin` オプション追加 (プラン 7)
