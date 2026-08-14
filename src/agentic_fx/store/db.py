"""SQLite 接続 + 15 テーブルスキーマ (`_SCHEMA` が作る分。移行専用の旧
`ohlcv` は含まない) — 設計書 §12。"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote

_log = logging.getLogger("agentic_fx.store.db")

_OHLCV_V2_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'yfinance', spread REAL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);
"""

_OHLCV_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv_cache (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);
"""

_OHLCV_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv_history (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL, spread REAL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);
"""

_SCHEMA = _OHLCV_CACHE_DDL + _OHLCV_HISTORY_DDL + """
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,            -- trade | improve | ask | reflection
  runner TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  trigger TEXT,                  -- cron | signal:<plugin> (Phase 2)。loop='trade' 以外は NULL
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
-- Task 12 Fix Round 1 (ユーザー裁定): snapshots.latest() の
-- `ORDER BY ts DESC, id DESC LIMIT 1` が無索引で全表スキャンになり、tick
-- 毎に呼ばれる実運用・バックテスト双方で行数に対し劣化する (O(n^2) 実測 —
-- cProfile で latest() が実行時間の 92%)。ASC インデックスで十分 (SQLite
-- は逆方向スキャン対応のため DESC 指定は不要)。
CREATE INDEX IF NOT EXISTS ix_account_snapshots_ts_id
  ON account_snapshots(ts, id);
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
  kind TEXT NOT NULL,            -- tech_plugin | news_source | live_trade | plugin
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
CREATE TABLE IF NOT EXISTS backtest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin_ref TEXT NOT NULL, content_hash TEXT NOT NULL, kind TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, source TEXT NOT NULL,
  period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  scope TEXT NOT NULL
    CHECK(scope IN ('in_sample','holdout_gate','human_custom')),
  issued_by TEXT NOT NULL CHECK(issued_by IN ('harness','human_cli')),
  metrics_json TEXT NOT NULL, settings_hash TEXT NOT NULL,
  core_commit TEXT NOT NULL, initial_balance REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  params_json TEXT NOT NULL, trial_count INTEGER NOT NULL,
  source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin TEXT NOT NULL, content_hash TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','claimed','consumed','abandoned')),
  claimed_by_mission_id INTEGER, claimed_at TEXT,
  requeue_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(plugin, content_hash, pair, timeframe, bar_ts)
);
-- プラン 9 束 C (codex 指摘の裏取り): prune_cache (store/ohlcv.py) の
-- `WHERE bar_time < ?` は PK (symbol, interval, bar_time, source) の先頭列に
-- 当たらず、autoindex のカバリングスキャンになる。定常状態では cutoff を
-- 跨ぐ行が毎分 4 行程度しかなく LIMIT に到達しないため、毎 tick (60s、
-- core_lock 保持下) 索引全体を走り切る。実測 (対象ゼロ、commit 込み、warm
-- 中央値): 20 万行 8.8ms / 100 万行 35.9ms → 索引後はいずれも 0.0055ms。
-- 書き込み側の劣化は実運用経路 (per-tick 4 bars upsert) で 30→33µs のノイズ。
CREATE INDEX IF NOT EXISTS ix_ohlcv_cache_bar_time
  ON ohlcv_cache(bar_time);
"""

TABLE_NAMES = frozenset({
    "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
    "reflections", "account_snapshots", "improvement_backlog",
    "improvement_runs", "econ_events", "approval_requests", "news_sources",
    "backtest_runs", "analysis_runs", "signals",
})


def connect(db_path: Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    # プラン 8 B 束: signals.py の claim_oldest/requeue/reclaim_expired は
    # RETURNING 句に依存する (SQLite 3.35.0 = 2021-03-12 以降)。古い
    # SQLite では RETURNING が構文エラーになり、失敗の意味が分かりにくい
    # (「claim できない」ではなく「SQL 構文エラー」として現れる) ため、
    # 接続確立時点で明示的に fail fast する。
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old (>= 3.35 required "
            "for signals.py RETURNING clauses)")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """読み取り専用で SQLite に接続する (mission worker 子プロセス専用 —
    設計書 §3.4 codex I-7)。

    書き込み系 PRAGMA (journal_mode 等) は発行しない — 既に WAL で稼働中の
    親プロセスの DB を読むだけであり、モード変更は不要かつ RO 接続では
    そもそも失敗する。`-wal`/`-shm` ファイルは親プロセスが作成済み (稼働中
    サービスが前提) なので読み取り可能。

    `db_path` が存在しない場合は `FileNotFoundError` — RO 接続は「既に
    `init_db` 済みの DB」を前提とし、この関数自身はスキーマを作らない
    (子プロセスがスキーマを作る権限を持つべきではない)。
    """
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old (>= 3.35 required)")
    if not db_path.exists():
        raise FileNotFoundError(
            f"connect_readonly requires an already-initialized DB: {db_path}")
    # URI の path 部分を percent-encode して `?` / `#` を含むファイル名に対応する。
    # safe="/" は絶対パスの `/` を encode しない設定。
    encoded_path = quote(str(db_path), safe="/")
    uri = f"file:{encoded_path}?mode=ro"
    # isolation_level=None: autocommit mode — 読み取り専用なので buffering の
    # 必要がなく、即座にエラーを検出する (execute() で直ちに SQLite へ到達)。
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False,
                          isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """存在しない列を追加する (SQLite は ADD COLUMN IF NOT EXISTS を持たない)。

    init_db は CREATE TABLE IF NOT EXISTS なので、既存 DB のテーブル定義は
    更新されない。列追加は PRAGMA で検査して ALTER する必要がある。
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


_FLOAT_TOL = 1e-9


def _values_match(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < _FLOAT_TOL


def _backup_before_migration(conn: sqlite3.Connection, suffix: str) -> None:
    """移行が必要と判定された時だけ、移行前スナップショットを取る。
    `suffix` は移行の種類ごとに別ファイルにするための識別子
    (Task 16: v1→v2 移行と v2→split 移行を別バックアップにする)。"""
    main = next((r for r in conn.execute("PRAGMA database_list")
                if r["name"] == "main"), None)
    db_file = main["file"] if main is not None else ""
    if not db_file:
        return
    src_path = Path(db_file)
    bak_path = src_path.with_name(src_path.name + suffix)
    if bak_path.exists():
        return
    bak_conn = sqlite3.connect(bak_path)
    try:
        conn.backup(bak_conn)
    finally:
        bak_conn.close()
    _log.warning("ohlcv 移行前のバックアップを作成しました: %s", bak_path)


def _migrate_ohlcv_v2(conn: sqlite3.Connection) -> None:
    """ohlcv を v1 (PK: symbol,interval,bar_time) から v2 (source/spread 列・
    PK に source を含む) へ table rebuild する。

    SQLite は既存テーブルの PK を ALTER できないため rebuild が必要。
    `_ensure_column` (単純な列追加) とは別関数にしてある。

    トランザクション内で完結させる (BEGIN IMMEDIATE → RENAME → CREATE →
    INSERT..SELECT → DROP → COMMIT、例外時 ROLLBACK)。

    **F2 (fix round 1, codex Critical): 並行 migration の再検証**。
    呼び出し側 (init_db) の判定と、ここで実際に `BEGIN IMMEDIATE` の
    書き込みロックを取るまでの間に、別接続が先に移行を完了させている
    ことがある (2 プロセス/2 接続が同時に起動した場合等)。ロック取得後に
    もう一度「`source` 列あり かつ `ohlcv_v1` 無し (= 完全に移行済み)」を
    再検査し、真なら何もせず抜ける。再検査せずに再構築すると
    `INSERT..SELECT` が `source='yfinance'` に決め打ちしているため、既に
    複数 source が入っている v2 テーブルを壊す (dukascopy 等の一括
    インポート行が yfinance 名義に化ける)。

    途中失敗時の再開性:
    - RENAME 前に失敗 → ROLLBACK で v1 のまま。次回起動時は `source` 列なし
      と判定して最初からやり直す。
    - RENAME 後 (`ohlcv_v1` 存在) かつ v2 の `ohlcv` も存在する状態で失敗
      (DROP 前) → 次回起動時は `ohlcv` が既に v2 (source 列あり) なので
      「移行不要」に見えてしまう。それを防ぐため、呼び出し側 (init_db) は
      `source` 列の有無だけでなく `ohlcv_v1` の残存も見て、残っていれば
      INSERT..SELECT からやり直す (このテーブルは INSERT OR IGNORE で
      冪等)。

    **F3 (fix round 1, codex Critical): 再開の安全化**。上記の再開時、
    `INSERT OR IGNORE` は同一キーの行を無条件に無視する。無視された行が
    「本当に同じ値」なのか「v2 側に別の値が既に入っている (破損 / 想定外の
    書き込み)」なのかを区別せずに `ohlcv_v1` を DROP すると、後者を黙って
    捨てることになる。DROP の前に `ohlcv_v1` と `ohlcv` の同一キー行を
    JOIN して値を比較し、1 件でも不一致があれば `ohlcv_v1` を DROP せず
    例外を送出して停止する (バックアップからの復元と手動調査を促す)。
    `ohlcv_v1` が v1 形式 (source 列なし) なら合成 source は 'yfinance'
    固定、`ohlcv_v1` 自体が v2 形式で残っていた場合 (想定外だが F2 適用前の
    バイナリが作った残骸の可能性) は列をそのまま引き継いで比較する。

    実 DB 適用時のバックアップは `_backup_before_migration` (F8) が自動で
    取る (このバックアップは「移行前スナップショット」— F3 の例外時の
    手動復元先)。
    """
    _backup_before_migration(conn, ".bak-ohlcv-v2")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
        v1_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ohlcv_v1'").fetchone() is not None
        if "source" in cols and not v1_exists:
            # F2: ロック取得までの間に別接続が移行を完了させていた。
            conn.commit()
            return
        if not v1_exists:
            conn.execute("ALTER TABLE ohlcv RENAME TO ohlcv_v1")
        conn.execute(_OHLCV_V2_DDL)

        v1_cols = {r["name"] for r in
                  conn.execute("PRAGMA table_info(ohlcv_v1)")}
        if "source" in v1_cols:
            # 想定外の残骸 (v2 形式のまま ohlcv_v1 になっている) — 列を
            # そのまま引き継ぐ
            conn.execute(
                "INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, "
                "open, high, low, close, volume, source, spread) "
                "SELECT symbol, interval, bar_time, open, high, low, close, "
                "volume, source, spread FROM ohlcv_v1")
            join_source = "v2.source = v1.source"
            select_src = ("v1.source AS src_name, v1.spread AS spread1, "
                         "v2.spread AS spread2")
        else:
            conn.execute(
                "INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, "
                "open, high, low, close, volume, source, spread) "
                "SELECT symbol, interval, bar_time, open, high, low, close, "
                "volume, 'yfinance', NULL FROM ohlcv_v1")
            join_source = "v2.source = 'yfinance'"
            select_src = ("'yfinance' AS src_name, NULL AS spread1, "
                         "v2.spread AS spread2")

        # F3: 同一キー行の値一致を検証する (INSERT OR IGNORE で無視された
        # 行が「同じ値だから無視された」のか「破損データと衝突して無視
        # された」のかを区別する)。fresh migration (ohlcv_v1 が今回の
        # RENAME で作られた場合) では ohlcv は直前まで空なので、JOIN は
        # 常に「自分がいま入れた行」同士の比較になり無条件に一致する。
        rows = conn.execute(
            f"SELECT v1.symbol, v1.interval, v1.bar_time, {select_src}, "
            "v1.open AS open1, v2.open AS open2, "
            "v1.high AS high1, v2.high AS high2, "
            "v1.low AS low1, v2.low AS low2, "
            "v1.close AS close1, v2.close AS close2, "
            "v1.volume AS volume1, v2.volume AS volume2 "
            "FROM ohlcv_v1 v1 JOIN ohlcv v2 "
            "ON v2.symbol = v1.symbol AND v2.interval = v1.interval "
            f"AND v2.bar_time = v1.bar_time AND {join_source}"
        ).fetchall()
        for r in rows:
            ok = (_values_match(r["open1"], r["open2"])
                  and _values_match(r["high1"], r["high2"])
                  and _values_match(r["low1"], r["low2"])
                  and _values_match(r["close1"], r["close2"])
                  and _values_match(r["volume1"], r["volume2"])
                  and _values_match(r["spread1"], r["spread2"]))
            if not ok:
                raise RuntimeError(
                    "ohlcv migration: ohlcv_v1 と ohlcv (v2) で値が一致しない"
                    f"行があります ({r['symbol']}/{r['interval']}/"
                    f"{r['bar_time']}/{r['src_name']})。ohlcv_v1 は温存しま"
                    "した。data/agentic.db.bak-ohlcv-v2 (または手動バック"
                    "アップ) からの復元と手動調査が必要です。")

        conn.execute("DROP TABLE ohlcv_v1")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _migrate_ohlcv_split(conn: sqlite3.Connection) -> None:
    """v2 単一テーブル `ohlcv` (source 列あり) を `ohlcv_cache`/
    `ohlcv_history` へ分割する (プラン 9 Task 16。設計書 §12「キャッシュと
    履歴をテーブルで分ける」)。

    source が LIVE_SOURCES ならキャッシュへ、それ以外 (IMPORT_SOURCES を
    含む) は履歴へ — **未知 source は履歴側へ隔離する** (fail-safe。履歴
    側は削除しないため、判断を誤っても失われない。設計書 §12 移行方針)。

    `_migrate_ohlcv_v2` と同型の再開安全パターン: BEGIN IMMEDIATE →
    存在検査 (無ければ既に完了 済み — 何もせず抜ける) → INSERT OR IGNORE →
    値一致検証 (F3 相当 — 無視された行が「同じ値だから」か「破損/想定外
    書き込みと衝突したから」かを区別する) → DROP → COMMIT。失敗時 ROLLBACK。

    `LIVE_SOURCES` は store/ohlcv.py にある (関数内 import — db.py と
    ohlcv.py の間に将来循環 import が生じても壊れないようにする防御的
    パターン。config.py の INTERVAL_MIN 関数内 import と同じ流儀)。
    """
    from agentic_fx.store.ohlcv import LIVE_SOURCES

    _backup_before_migration(conn, ".bak-ohlcv-split")
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ohlcv'").fetchone() is not None
        if not exists:
            conn.commit()
            return
        # 移行先を自己完結で用意する。`init_db` は `_SCHEMA` を先に流すので
        # 通常は既にあるが、集合演算版は**両方のテーブルへ 1 文ずつ**流す
        # (旧実装は「その行に必要な側」しか触らなかった) ため、片側しか
        # 無い DB でも壊れないようにする。DDL は IF NOT EXISTS で冪等。
        # **`executescript` は使わない** — 実行前に暗黙の COMMIT を打つため、
        # 上で取った BEGIN IMMEDIATE の書き込みロックが落ちて、二重移行を
        # 防ぐ排他が無効になる。各定数は単一文なので `execute` で足りる。
        conn.execute(_OHLCV_CACHE_DDL)
        conn.execute(_OHLCV_HISTORY_DDL)

        # `_migrate_ohlcv_v2` と同じく**集合演算**で移す。旧実装は全行を
        # Python へ materialize し、1 行あたり 2 文 (INSERT + 検証 SELECT)
        # を発行していた。2 年分の 1m を 1 通貨ペアぶん持つ実 DB は ~75 万行
        # あり、BEGIN IMMEDIATE の書き込みロックを保持したまま 150 万文を
        # 流すことになる。移行はユーザーごとに 1 回きり・無人で走り、対象は
        # 最も大きくなりやすい DB なので、行数に依存しない形にする。
        live = sorted(LIVE_SOURCES)
        marks = ",".join("?" * len(live))
        conn.execute(
            "INSERT OR IGNORE INTO ohlcv_cache (symbol, interval, bar_time, "
            "open, high, low, close, volume, source) "
            "SELECT symbol, interval, bar_time, open, high, low, close, "
            f"volume, source FROM ohlcv WHERE source IN ({marks})", live)
        conn.execute(
            "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, "
            "open, high, low, close, volume, source, spread) "
            "SELECT symbol, interval, bar_time, open, high, low, close, "
            f"volume, source, spread FROM ohlcv WHERE source NOT IN ({marks})",
            live)

        # 値一致検証も JOIN で行い、**不一致の行だけ**を取り出す (通常は
        # 0 行なので行数に依存しない)。比較規則は `_values_match` と同じ —
        # 非 NULL 同士は絶対差が許容誤差未満なら一致、NULL 同士は一致、
        # 片側だけ NULL は不一致。
        num = " OR ".join(f"abs(o.{c} - t.{c}) >= {_FLOAT_TOL}"
                          for c in ("open", "high", "low", "close", "volume"))
        # 履歴側は spread も比較する。ohlcv_cache に spread 列は無いので
        # キャッシュ側は OHLCV 5 列のみ。5 列だけを見ると、spread だけ
        # 食い違う行が「一致」と判定されて旧 ohlcv が DROP され、旧 spread
        # が無警告で失われる (spread はバックテストのコスト計算に効く)。
        spread_mismatch = (
            "((o.spread IS NULL) <> (t.spread IS NULL) "
            "OR (o.spread IS NOT NULL AND t.spread IS NOT NULL "
            f"AND abs(o.spread - t.spread) >= {_FLOAT_TOL}))")
        bad = None
        for target, where, extra in (
                ("ohlcv_cache", f"o.source IN ({marks})", ""),
                ("ohlcv_history", f"o.source NOT IN ({marks})",
                 f" OR {spread_mismatch}")):
            bad = conn.execute(
                "SELECT o.symbol, o.interval, o.bar_time, o.source "
                f"FROM ohlcv o JOIN {target} t "
                "ON t.symbol = o.symbol AND t.interval = o.interval "
                "AND t.bar_time = o.bar_time AND t.source = o.source "
                f"WHERE {where} AND ({num}{extra}) LIMIT 1", live).fetchone()
            if bad is not None:
                break
        if bad is not None:
            raise RuntimeError(
                "ohlcv split migration: ohlcv と ohlcv_cache/ohlcv_history で"
                f"値が一致しない行があります ({bad['symbol']}/"
                f"{bad['interval']}/{bad['bar_time']}/{bad['source']})。ohlcv は"
                "温存しました。data/agentic.db.bak-ohlcv-split (または手動"
                "バックアップ) からの復元と手動調査が必要です。")

        conn.execute("DROP TABLE ohlcv")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _ohlcv_legacy_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='ohlcv'").fetchone() is not None


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    if _ohlcv_legacy_exists(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
        v1_leftover = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ohlcv_v1'").fetchone() is not None
        if "source" not in cols or v1_leftover:
            _migrate_ohlcv_v2(conn)     # v1 → v2 (既存、無変更)
        _migrate_ohlcv_split(conn)      # v2 → ohlcv_cache/ohlcv_history (新設)
    _ensure_column(conn, "missions", "trigger", "trigger TEXT")
    conn.commit()
