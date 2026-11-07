-- init_db が e8131706 で作る fresh DB の sqlite_master (table / index / trigger)。
-- 回避帯・signal 採否の schema 追加前の DB を再現する入力。手で編集しない。

CREATE TABLE ohlcv_cache (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);

CREATE TABLE ohlcv_history (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL, spread REAL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);

CREATE TABLE data_migrations (
  name TEXT PRIMARY KEY, applied_at TEXT NOT NULL
);

CREATE TABLE missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,            -- trade | improve | ask | reflection
  runner TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  trigger TEXT,                  -- cron | signal:<plugin> (Phase 2)。loop='trade' 以外は NULL
  output_json TEXT, transcript_json TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);

CREATE TABLE trade_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL REFERENCES missions(id),
  payload_json TEXT NOT NULL,
  action TEXT,                   -- open | close | cancel | hold (NULL = 移行前の行)
  gate_result TEXT,              -- accepted | rejected (NULL = 未判定)
  reject_reason TEXT,
  reject_category TEXT,          -- risk_gate | origin | mission | execution | signal
  created_at TEXT NOT NULL,
  -- `action IS NULL` は移行前の行を対象外にする意図の明示である。
  -- **SQL の NULL 意味論により、この節が無くても legacy 行は通る**
  -- (`reject_category` が NULL のとき `NULL IN (...)` は NULL、
  --  SQLite は CHECK 結果が NULL の行を受理する — 着手前検証 2026-08-15 で
  --  実測)。したがって「この節を外す」変異は**原理的に殺せない**が、
  -- 契約の文書化として残す (Step 5 の変異表に「受容」と明記)。
  --
  -- **同じ NULL 意味論が rejected 枝にも効く (レビュー 1 周目 ローカル LLM
  -- 3 モデル一致、2026-08-16 修正)。** `reject_category IS NOT NULL` を
  -- 明示しないと `NULL IN (...)` が NULL を返し `TRUE AND NULL` も NULL
  -- になるため、SQLite は CHECK 結果 NULL の行を受理し
  -- `('rejected', NULL)` の組が素通りしてしまう。`action IS NULL` 節と
  -- 同じ三値意味論 (NULL は TRUE でも FALSE でもない) が原因であり、
  -- `IS NOT NULL` を足すと `FALSE AND NULL` = FALSE に確定して弾ける。
  CHECK (action IS NULL
         OR gate_result IS NULL
         OR (gate_result='accepted' AND reject_category IS NULL)
         OR (gate_result='rejected' AND reject_category IS NOT NULL
             AND reject_category IN
             ('risk_gate','origin','mission','execution','signal')))
);

CREATE TABLE orders (
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

CREATE TABLE reflections (
  order_id INTEGER PRIMARY KEY REFERENCES orders(id),
  content TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE reflection_attempts (
  order_id INTEGER PRIMARY KEY REFERENCES orders(id),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT NOT NULL,
  last_reason TEXT
);

CREATE TABLE alert_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, balance REAL NOT NULL, equity REAL NOT NULL,
  hwm REAL NOT NULL, cashflow REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'paper'
);

CREATE TABLE improvement_backlog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea TEXT NOT NULL,
  source TEXT NOT NULL,          -- user | agent | research
  status TEXT NOT NULL DEFAULT 'open',  -- open | selected | done | rejected
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
, attempts INTEGER NOT NULL DEFAULT 0, last_result TEXT, idea_norm TEXT, origin_mission_id INTEGER, origin_outcome TEXT);

CREATE TABLE improvement_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backlog_id INTEGER REFERENCES improvement_backlog(id),
  result TEXT CHECK (result IN ('approval','report')),
  approval_id INTEGER, report_path TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
, mission_id INTEGER REFERENCES missions(id), report_state TEXT NOT NULL DEFAULT 'none' CHECK(report_state IN ('none','prepared','published','failed')), trigger TEXT);

CREATE TABLE econ_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, country TEXT NOT NULL, name TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 0,
  actual TEXT, forecast TEXT, previous TEXT,
  UNIQUE (ts, country, name)
);

CREATE TABLE approval_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,            -- plugin (news_source 経路は 2026-09-07 廃止、tech_plugin/live_trade は将来枠)
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  -- pending | approved | rejected | expired | invalidated
  reason TEXT, decided_by TEXT, decided_at TEXT,
  message_id TEXT, expires_at TEXT, created_at TEXT NOT NULL
);

CREATE TABLE news_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  fetcher TEXT NOT NULL,         -- feed | web
  url TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 0,
  added_by TEXT NOT NULL,        -- user | agent
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE backtest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin_ref TEXT NOT NULL, content_hash TEXT NOT NULL, kind TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, source TEXT NOT NULL,
  base_interval TEXT NOT NULL DEFAULT '1m', params_json TEXT NOT NULL DEFAULT '{}',
  period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  scope TEXT NOT NULL
    CHECK(scope IN ('in_sample','holdout_gate','human_custom')),
  issued_by TEXT NOT NULL CHECK(issued_by IN ('harness','human_cli')),
  metrics_json TEXT NOT NULL, settings_hash TEXT NOT NULL,
  core_commit TEXT NOT NULL, initial_balance REAL NOT NULL,
  created_at TEXT NOT NULL,
  mission_id INTEGER,
  variant TEXT NOT NULL DEFAULT 'candidate'
    CHECK(variant IN ('candidate','baseline','no_strategy')),
  ref_plugin_ref TEXT, ref_content_hash TEXT,
  mission_outcome TEXT
);

CREATE TABLE analysis_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  params_json TEXT NOT NULL, trial_count INTEGER NOT NULL,
  source TEXT NOT NULL, created_at TEXT NOT NULL
, mission_id INTEGER);

CREATE TABLE candidate_archives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  artifact_hash TEXT NOT NULL,
  archive_path TEXT,
  pair TEXT,
  metrics_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  logic_hash TEXT,
  family_key TEXT,
  outcome_class TEXT NOT NULL DEFAULT 'not_submitted'
    CHECK (outcome_class IN ('not_submitted','pending','approved','rejected_by_human','expired','invalidated','revoked','unprofitable','insufficient_trades','gate_failed','noop_copy','duplicate_logic','duplicate_metrics','family_redirect')),
  quality_flags TEXT NOT NULL DEFAULT '[]',
  relation TEXT NOT NULL DEFAULT '[]',
  UNIQUE(mission_id, artifact_hash)
);

CREATE TABLE signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin TEXT NOT NULL, content_hash TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','claimed','consumed','abandoned')),
  claimed_by_mission_id INTEGER REFERENCES missions(id), claimed_at TEXT,
  requeue_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(plugin, content_hash, pair, timeframe, bar_ts)
);

CREATE TABLE plugin_deploy_ops (
  op_id INTEGER PRIMARY KEY,
  op_kind TEXT NOT NULL CHECK(op_kind IN
    ('disable_symlink','rollback_symlink','retire_plain','revoke')),
  name TEXT NOT NULL,
  from_kind TEXT NOT NULL CHECK(from_kind IN ('symlink','plain','absent','none')),
  to_kind TEXT NOT NULL CHECK(to_kind IN ('absent','symlink','retired','none')),
  from_target TEXT, to_target TEXT, retired_path TEXT,
  subject_content_hash TEXT, subject_artifact_hash TEXT,
  target_content_hash TEXT, target_artifact_hash TEXT,
  approval_id INTEGER,
  revoke INTEGER NOT NULL CHECK(revoke IN (0,1)),
  revoke_row_id INTEGER,
  force INTEGER NOT NULL CHECK(force IN (0,1)),
  phase TEXT NOT NULL CHECK(phase IN ('intent','done','needs_retry','foreign')),
  fail_reason TEXT CHECK(fail_reason IN ('verify_failed','syscall_failed')),
  notify_attempted INTEGER NOT NULL CHECK(notify_attempted IN (0,1)),
  actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK(
    (op_kind='disable_symlink' AND from_kind='symlink' AND to_kind='absent'
     AND from_target IS NOT NULL AND to_target IS NULL AND retired_path IS NULL
     AND subject_content_hash IS NOT NULL AND subject_artifact_hash IS NOT NULL
     AND target_content_hash IS NULL AND target_artifact_hash IS NULL)
    OR
    (op_kind='rollback_symlink' AND to_kind='symlink' AND to_target IS NOT NULL
     AND retired_path IS NULL AND target_content_hash IS NOT NULL
     AND target_artifact_hash IS NOT NULL AND from_kind IN ('symlink','absent')
     AND ((from_kind='symlink' AND from_target IS NOT NULL
           AND subject_content_hash IS NOT NULL AND subject_artifact_hash IS NOT NULL)
          OR (from_kind='absent' AND from_target IS NULL
              AND subject_content_hash IS NULL AND subject_artifact_hash IS NULL
              AND revoke=0)))
    OR
    (op_kind='retire_plain' AND from_kind='plain' AND to_kind='retired'
     AND retired_path IS NOT NULL AND from_target IS NULL AND to_target IS NULL
     AND target_content_hash IS NULL AND target_artifact_hash IS NULL AND revoke=0)
    OR
    (op_kind='revoke' AND from_kind='none' AND to_kind='none'
     AND from_target IS NULL AND to_target IS NULL AND retired_path IS NULL
     AND subject_content_hash IS NOT NULL AND subject_artifact_hash IS NOT NULL
     AND target_content_hash IS NULL AND target_artifact_hash IS NULL
     AND approval_id IS NOT NULL AND revoke=1 AND force=0 AND phase='done')
  ),
  CHECK((revoke=1) = (revoke_row_id IS NOT NULL)),
  CHECK((phase='needs_retry') = (fail_reason IS NOT NULL)),
  CHECK(fail_reason IS NULL OR fail_reason!='syscall_failed' OR op_kind!='revoke'),
  CHECK(fail_reason IS NULL OR fail_reason!='verify_failed' OR op_kind='rollback_symlink'),
  CHECK(revoke=0 OR approval_id IS NOT NULL),
  CHECK(phase!='intent' OR notify_attempted=0)
);

CREATE TABLE plugin_deploy_op_signals (
  op_id INTEGER NOT NULL REFERENCES plugin_deploy_ops(op_id),
  signal_id INTEGER NOT NULL REFERENCES signals(id),
  status_at_link TEXT NOT NULL CHECK(status_at_link IN ('pending','claimed')),
  PRIMARY KEY(op_id, signal_id)
);

CREATE TABLE datafeed_outage_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  confirmed INTEGER NOT NULL DEFAULT 0,
  entered_degraded_at TEXT,
  restricted_since TEXT,
  restricted_deadline_at TEXT,
  ready_streak INTEGER NOT NULL DEFAULT 0,
  pending_human_confirmation INTEGER NOT NULL DEFAULT 0,
  resume_requested_at TEXT,
  resume_acknowledge INTEGER NOT NULL DEFAULT 0,
  recovered_notified_epoch INTEGER,
  updated_at TEXT NOT NULL
);

CREATE TABLE datafeed_outage_gap (
  pair TEXT NOT NULL,
  interval TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  gap_start TEXT NOT NULL,
  replay_through TEXT,
  PRIMARY KEY (pair, interval, epoch)
);

CREATE TABLE cron_cursor (
  pair TEXT NOT NULL,
  interval TEXT NOT NULL,
  bar_time TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (pair, interval)
);

CREATE TABLE mission_decision_bars (
  mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  pair TEXT NOT NULL,
  interval TEXT NOT NULL,
  bar_time TEXT NOT NULL,
  PRIMARY KEY (mission_id, pair, interval)
);

CREATE TABLE improve_waves (
  period_key TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expected INTEGER NOT NULL
);

CREATE TABLE improve_wave_slots (
  wave_period_key TEXT NOT NULL REFERENCES improve_waves(period_key),
  k INTEGER NOT NULL,
  status TEXT NOT NULL
    CHECK(status IN ('reserved','claimed','running','done','failed')),
  mission_id INTEGER REFERENCES missions(id),
  spawn_attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(wave_period_key, k)
);

CREATE TABLE plugin_switch_journal (
  op_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('approve','bless')),
  approval_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  old_kind TEXT NOT NULL CHECK(old_kind IN ('absent','symlink')),
  old_target TEXT,
  temp_path TEXT NOT NULL,
  new_target TEXT NOT NULL,
  switch_required INTEGER NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN
    ('preparing','versioned','recorded','switched','decided','reverted')),
  actor TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE ops_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL UNIQUE,
  endpoint TEXT NOT NULL,
  authenticated_principal TEXT NOT NULL,
  asserted_actor TEXT,
  peer_pid INTEGER,
  peer_exe TEXT,
  idempotency_key TEXT,
  body_sha256 TEXT NOT NULL,
  target_ref TEXT,
  phase TEXT NOT NULL CHECK(phase IN ('accepted','succeeded','failed','outcome_unknown')),
  result_code TEXT,
  job_id TEXT,
  response_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE ops_idempotency (
  authenticated_principal TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('accepted','succeeded','failed','outcome_unknown')),
  accepted_request_id TEXT NOT NULL,
  response_json TEXT,
  expires_at TEXT NOT NULL,
  PRIMARY KEY(authenticated_principal, endpoint, idempotency_key)
);

CREATE TABLE ops_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  code TEXT NOT NULL,
  ref TEXT NOT NULL,
  fields_json TEXT NOT NULL
);

CREATE TABLE ops_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL UNIQUE,
  directive TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE improve_run_notices (
  mission_id INTEGER PRIMARY KEY REFERENCES missions(id),
  trigger TEXT NOT NULL,
  result_code TEXT NOT NULL,
  reason_code TEXT,
  tool_calls INTEGER,
  observation_text TEXT,
  approval_id INTEGER,
  created_at TEXT NOT NULL
);

CREATE INDEX ix_account_snapshots_ts_id
  ON account_snapshots(ts, id);

CREATE UNIQUE INDEX ix_plugin_deploy_ops_open_name
  ON plugin_deploy_ops(name)
  WHERE phase='intent' OR (phase='needs_retry' AND fail_reason='syscall_failed');

CREATE INDEX plugin_deploy_op_signals_signal_id
  ON plugin_deploy_op_signals(signal_id);

CREATE INDEX ix_ohlcv_cache_bar_time
  ON ohlcv_cache(bar_time);

CREATE UNIQUE INDEX ix_plugin_switch_journal_open_name
  ON plugin_switch_journal(name)
  WHERE phase NOT IN ('decided', 'reverted');

CREATE INDEX ix_ops_events_code_ref ON ops_events(code, ref);

CREATE UNIQUE INDEX ix_improvement_runs_mission_id
  ON improvement_runs(mission_id)
  WHERE mission_id IS NOT NULL;

CREATE TRIGGER ops_requests_no_update
BEFORE UPDATE ON ops_requests BEGIN SELECT RAISE(ABORT, 'ops_requests is append-only'); END;

CREATE TRIGGER ops_requests_no_delete
BEFORE DELETE ON ops_requests BEGIN SELECT RAISE(ABORT, 'ops_requests is append-only'); END;

CREATE TRIGGER improve_run_notices_no_update
BEFORE UPDATE ON improve_run_notices BEGIN SELECT RAISE(ABORT, 'improve_run_notices is append-only'); END;

CREATE TRIGGER improve_run_notices_no_delete
BEFORE DELETE ON improve_run_notices BEGIN SELECT RAISE(ABORT, 'improve_run_notices is append-only'); END;
