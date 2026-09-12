# [gate-failed-ledger-discarded] 設計 v6 (実装仕様) — 最終出力に至らない mission の成果保全 (2026-09-09)

本書の位置づけ: チケット [gate-failed-ledger-discarded]、本体設計書 phase2-10 §3.4/§3.6/§4.1 を改訂 (`29f260e`)。

対象: main `bdfb71e`。v1 → v2: codex 1 周目 (`codex-design-review.md`) を反映。v2 → v3: codex 2 周目 (`codex-design-review-2.md`) を反映。v3 → v4: codex 3 周目 (`codex-design-review-3.md`) を反映。v4 → v5: codex 4 周目 (`codex-design-review-4.md`、Critical 2 / Major 3) を反映 — **L0/L2 を再構成: callback は記録だけ、archive の publish は slot スレッドの commit 相へ移す**。差分は §8。v5 → v6: codex 5 周目 (`codex-design-review-5.md`、**新規 Critical 0**、Major 3 / Minor 2) を反映、差分は §9。**実装に進む** (L2 は分離しない)。動機: run9 #67 は pf 1.387 / max_dd 4.03% の候補を abort 終端で台帳ごと破棄 (`tmp/a4-run9-20260909.md` 観測 B)。

## 0. 前提を疑う

| 通説 | 実測・再読 | 帰結 |
|---|---|---|
| 「失敗 mission で台帳 DISCARDED」(設計書 §3.4/§4.1) | 理由は監査 (期限内完了のみ) で、失敗 mission の backtest を捨てる積極的根拠は無い | 全終端で永続化 (L1) |
| 「監査規則『期限内完了のみ』は freeze で守られている」 | **守られていない** (codex Critical 1): RPC timeout は dispatcher (`worker_runner.py:300-307`) が判定するが、`ledger.record` は handler 完了時 (`improve_rpc_tools.py:125-131`) に別スレッドで走る。timeout 後・freeze 前に完了した handler の結果が台帳に載る。失敗 mission で捨てていたので隠れていた | **記録主体を dispatcher に移す**: handler は記録せず結果 (+ save_kwargs) を返し、dispatcher が `result_queue.get(timeout)` に**成功した直後**に `ledger.record`。timeout した呼び出しは記録されない (監査境界 = timeout 所有者) |
| 「終端時に staging をコピーすれば本体が残る」 | **残らない** (Critical 2): staging は backtest 後も上書き自由 (`write_staging_file`)。終端時のファイルは「測定された本体」でなく「最後に書かれた本体」。hash A→B と書き換えた候補では metrics と本体が対応しない | **backtest 成功の瞬間に snapshot**: 親 handler が `run_in_sample` 完了直後に staging の 3 ファイルの bytes (実行に使った同じ bytes = handler が読んだもの) を `plugins/_archive/<mission_id>/<artifact_hash>/` へ原子的に書く (temp → fsync → readonly → rename、`version_store` の流儀)。終端時のコピーは不要 |
| 「`backtest_runs` に行を足すだけでよい」 | 消費者 `latest_in_sample_metrics` (`get_signals` が live plugin の成績表示に使う) は `content_hash + pair + variant + source + base_interval` で最新 1 件を取る。失敗 mission の同 hash 行が承認済み plugin の表示を上書きしうる (Major 1) | 行に **`mission_outcome`** 列 (migration、既存行 NULL = 旧成功行) を足し、`latest_in_sample_metrics` は `mission_outcome IN ('approval') OR mission_outcome IS NULL` に絞る。Tier G 等の将来の消費者は outcome を明示して引く |
| 「DB 行と archive は命名規則で結べる」 | `plugin_ref` は削除される staging パス、hash 不一致時は結べない (Major 5) | **`candidate_archives` 表** (mission_id, name, content_hash, artifact_hash, archive_path, created_at)。backtest_runs 行とは (mission_id, content_hash) で join。archive のディレクトリ名 = artifact_hash |
| 「保全は終端 tx の外でよい」 | DB 行は終端と同じ tx でないと不整合 (v1 と同じ) | 終端 tx 内の SAVEPOINT。fail-soft は `Exception` 全般 (KeyError / TypeError / ValueError も) を隔離 (Major 3)。`ROLLBACK TO` の後は `RELEASE` |

## 1. 誰が何をどう扱うか

### L0 — 監査境界の是正 (予約は RPC 開始時、記録は期限内受理後、publish は commit 相)
- **前提を疑う (4 周目)**: 「dispatcher スレッドの callback で archive を publish する」限り、callback が停滞したまま slot が freeze する競合と、打ち切れない callback の矛盾は消えない (Critical 2 件)。**publish を callback から外し、slot スレッド (commit 相) に移す** — 可視 archive を作るのは常に ledger の entries を読む側なので「archive あり・行なし」は構造的に起きない。
- **予約は RPC 処理開始時** (Critical 1): dispatcher は handler を起動する**前**に `on_rpc_begin(name)` → `ledger.begin_accept()` (OPEN なら予約 +1、FROZEN なら False で RPC 自体を `{"error": "mission_finalizing"}` で拒否)。`result_queue.get` が期限内に返ったら `on_rpc_accepted(name, args, outcome)` → `record(...)` + `end_accept()`。timeout / handler error では `on_rpc_released(name)` → `end_accept()` のみ (記録しない)。受理点と予約の間の窓は無い。
- 予約は **generation 付き token** (`begin_accept()` が `(gen, token)` を返し、`end_accept(token)` は現 generation と一致するときだけ減算)。`freeze()` の打ち切りは generation を進めてカウンタを 0 にするので、遅延した `end_accept` は no-op (単純 decrement だと負値になる — codex 5 周目)。
- `record` は in-memory 追記だけ (ファイル I/O 無し) なので callback は停滞しない。`freeze()` が予約 0 を待つ上限 (`accept_drain_sec`、既定 30 s) は「handler が rpc_timeout 内で走行中に mission が終わった」場合にだけ効く。上限超過 = その handler の結果は捨て、tmp は startup sweep が回収。**打ち切っても在庫が壊れない** (publish していないから)。
- 二層戻り値 `RpcOutcome(public, private)` と `WorkerRunner` の callback 注入 (`on_rpc_begin` / `on_rpc_accepted` / `on_rpc_released`) は v3/v4 のまま。子 ledger は残す (`improve_rpc_tools.py` の「optional / worker は渡さない」を撤回、変更表を修正 — codex 4 周目 Major 3)。
- drain の shutdown への算入 (Major 1): `accept_drain_sec` を improve supervisor の join deadline に足し、service の graceful 判定は improve thread の生存も見る (既存は取引 supervisor のみ)。

### L1 — 台帳の全終端永続化
- 共通 predicate `accepted_entries(entries)` = `"error" not in result_summary`。**永続化・`trial_count`・`analysis_call_count`・`analysis_run_ids` の全部がこれを使う** (Major 2: 現状 payload は error entry も加算している)。
- 非成功 6 経路 + report `.part` 失敗 sentinel 経路 (`_prepare_report_if_applicable` :1655-1676、Major 4) の各終端 tx の内側に `SAVEPOINT ledger` → `_persist_ledger_rows(..., mission_outcome=<経路名>)` → `RELEASE`。`Exception` なら `ROLLBACK TO ledger; RELEASE ledger` + activity `ledger_persist_failed`、終端は続行。
- ledger 状態 (権威はこの行、codex 5 周目 Major 2): 外側 commit 成功 → `PERSISTED` / SAVEPOINT 巻き戻し・外側 commit 失敗 (= 保存の一時失敗) → **`PERSIST_FAILED`** (補償 2 回目で再試行可) / 予約打ち切り・CAS 負けなど再試行しない経路 → `DISCARDED`。FROZEN → {PERSISTED, PERSIST_FAILED, DISCARDED}、PERSIST_FAILED → {PERSISTED, DISCARDED}。`commit()` の finally が無条件に `mark_persisted` する現行 (:1629) は「保存結果」を見る形に直す。
- CAS を別終端者に負けた場合 (補償 tx も CAS 失敗) は保存されない — 「全終端」の保証から除外と明記。

### L2 — backtest 時点 snapshot (handler は tmp まで)、publish は commit 相 (slot スレッド)
- **段 1 (handler、dispatcher の daemon thread)**: v4 と同じ — 実行に使った bytes を `plugins/_archive/<mission_id>/.tmp-<artifact_hash>-<uuid>/` に書いて fsync、tmp パスと `artifact_hash` を `outcome.private` に載せる。rename しない。
- **段 2 (commit 相、slot スレッド、`_persist_ledger_rows` と同じ場所)**: freeze 後に `accepted_entries` を走査し、`archive_tmp` を持つ entry を `plugins/_archive/<mission_id>/<artifact_hash>/` へ `os.rename` (直行、`EEXIST` / `ENOTEMPTY` は先着扱いで自 tmp を chmod → 削除、他 errno は `archive_failed`)。成功したものだけ `candidate_archives` 行を同じ SAVEPOINT 内に書く。**timeout / 予約打ち切り / FROZEN 後の entry は台帳に無いので rename されない**。
- publish は DB 行と同じスレッド・同じ相なので順序は「rename → 行」。rename 成功後に SAVEPOINT / 外側 commit が失敗すると archive は残り行が無い → ledger は `PERSIST_FAILED`。**再試行の冪等性** (codex 5 周目 Major 1): 再試行時に tmp が無く final が存在するなら、final が通常ディレクトリで、格納 3 ファイルから再計算した `artifact_hash` がディレクトリ名と一致することを確認して「publish 済み」と扱い、`candidate_archives` 行だけを挿入する (`final.is_dir()` だけでは別内容・不完全 dir を正当化しうる)。tmp も final も無ければ `archive_failed`。再試行されなかった孤立 final (補償も失敗) は startup sweep が `candidate_archives` に無い dir として activity に出す (削除しない)。
- 同一候補名で A→B→A と書き換えても hash ごとに別ディレクトリで metrics と本体が 1:1。

### L3 — 人間向け導線
- system note の `last_result` 末尾に `best=<name>@<artifact_hash[:8]> pf=<x> dd=<y> archive=plugins/_archive/<mission>/<hash>`。best の順序 = **同一 hash 内で evaluable desc → trades desc → pf desc (None は最下) → max_drawdown asc**。
- `plugins/_archive/INDEX.md` に 1 mission 1 行を追記 (mission_id / status / 候補数 / best)。新しい report 種別は作らない (`improvement_runs.result` の CHECK と report_state 規約に触れない — Major 4)。
- output_invalid / commit 補償 / `.part` sentinel も同じ note・INDEX 経路を通す (全終端表 §3)。

### L4 — GC
- `plugin/switch.py::sweep_orphans` に `_archive` の branch を足す: **総バイト上限** (`improve.archive_max_bytes`、既定 200 MB) と **mission 数上限** (`improve.archive_max_missions`、既定 50) を併用、古い mission_id から削除。symlink は追わない、entry 単位で失敗隔離、readonly tree は chmod してから削除 (既存 sweep の流儀)。
- 順序 (codex Minor 2): **ファイル削除を確認してから**同じ entry の `candidate_archives.archive_path` を NULL に更新 (行は残す = metrics の来歴は消えない)。DB 更新に失敗した非 NULL 行は次回 sweep で「path 不存在」として再収束。`.tmp-*` の無条件回収は **runner 起動前の startup sweep に限定** (codex 3 周目 Minor 1: 起動時なら前プロセスの handler は存在しない)。runtime GC に転用するなら age cutoff が要る、と `sweep_orphans` の docstring に明記。

### v2 候補 (本束に入れない)
- model 向け `read_archived_candidate` (noop_copy_of の拡張が要る。codex Minor 1 で先送り妥当と判定)。
- [backtest-dedup-cache]: L1 の行 + `candidate_archives` + 履歴 revision。

## 2. 変更点
| 場所 | 変更 |
|---|---|
| `runners/worker_runner.py` | `on_rpc_begin` / `on_rpc_accepted` / `on_rpc_released` callback を注入 (begin は handler 起動前、accepted は `get` 成功直後、released は timeout / error)。子へは `outcome.public` のみ送る |
| `loops/improve_rpc_ledger.py` | `begin_accept()` / `end_accept()` (予約カウンタ)、`freeze()` が予約 0 まで待つ (上限付き)、`freeze_if_open()`、状態 `PERSIST_FAILED` を追加 |
| `core/improve_supervisor.py` / `service.py` | join deadline に `accept_drain_sec` を算入、graceful 判定に improve thread の生存 |
| `mission_worker.py` / `tools/mission_registry.py` | 子側 ledger は現状維持 (変更なし、契約テストも維持) — 変更表に明記 (codex 3 周目 Major 1) |
| `config.py` | `improve.accept_drain_sec` (既定 30) |
| `tools/improve_rpc_tools.py` | `RpcOutcome(public, private)`、親 wrapper を `build_rpc_handlers` (記録しない) に。子側 tooldef の `ledger` 契約は**無変更** |
| `loops/improve_loop.py` | `on_rpc_begin` / `on_rpc_accepted` (ledger.record のみ) / `on_rpc_released` / `accepted_entries` / `_persist_ledger_rows(mission_outcome=)` + **commit 相の archive publish (rename → candidate_archives、冪等再試行)** / 全終端の SAVEPOINT (§3) / `run_backtest_handler` の tmp snapshot / `compensate_commit_failure` (freeze_if_open → 永続化 → 状態遷移) / note の best / INDEX.md |
| `store/db.py` | migration: `backtest_runs.mission_outcome TEXT` / 新表 `candidate_archives` (fresh schema と `TABLE_NAMES`、`test_db` の 20 → 21 表) |
| `store/backtest_runs.py` | `save_harness_run(mission_outcome: str \| None = None)` (既定 None = 既存呼び出し無変更、ImproveLoop の台帳/gate 保存だけが明示)、`latest_in_sample_metrics` は `mission_outcome IS NULL OR = 'approval'` |
| `store/candidate_archives.py` (新規) | insert / list_by_mission / clear_path |
| `plugin/switch.py` | `sweep_orphans` の archive GC branch |
| `config.py` / example | `improve.archive_max_bytes` / `archive_max_missions` |
| 設計書 §3.4 / §4.1 | 監査境界 = dispatcher、全終端で永続化、DISCARDED は保存失敗 |

## 3. 全終端表 (各経路で何が起きるか)
| 経路 | ledger 行 (outcome) | archive | note / INDEX | staging | ledger 状態 |
|---|---|---|---|---|---|
| 成功 (approval) | あり (`approval`) | あり (commit 相で publish) | — | 残す | PERSISTED |
| report / observation | あり (`report` / `observation`) | あり | INDEX | 削除 | PERSISTED |
| gate_failed / loser | あり | あり | INDEX (report は既存) | 削除 | PERSISTED |
| output_invalid | あり (`output_invalid`) | あり | note 型 A/C・INDEX | 削除 | PERSISTED |
| failed / timeout / abort | あり (`failed`) | あり | note 型 A/B/C + best・INDEX | 削除 | PERSISTED |
| report `.part` 失敗 sentinel | あり (`report_failed`) | あり | INDEX | 削除 | PERSISTED |
| approval Tx-2 失敗 → 内部補償 `_compensate_tx2_failure` 成功 | 補償 tx 内で試みる (`commit_failed`) | あり | INDEX | 残す (既存) | PERSISTED / PERSIST_FAILED / DISCARDED |
| 内部補償も失敗 (mission 非終端のまま return) | 無し | あり | activity のみ | 残す | DISCARDED |
| `commit()` が例外で抜け supervisor が `compensate_commit_failure` | 補償入口で `ledger.freeze_if_open()` (冪等: OPEN なら freeze、それ以外は no-op — `commit()` が接続取得前に落ちると OPEN のまま来る、codex 3 周目 Major 3)。補償 tx 内で SAVEPOINT 永続化 (`commit_failed`) → `finish` の CAS → commit 成功で PERSISTED。**CAS 失敗は tx 全体 rollback、ledger の終端状態は変えない**。二重呼び出し (codex 4 周目 Major 2): ledger 状態を `PERSISTED` / `PERSIST_FAILED` (SAVEPOINT rollback = 再試行可) / `DISCARDED` (予約打ち切り等 = 再試行しない) に分け、2 回目は `PERSIST_FAILED` のときだけ DB の mission 状態を確認して再試行 (mission が running なら補償 tx 全体、既に terminal なら ledger 行のみを一意キー (mission_id, opaque_ref) で挿入)。`PERSISTED` は skip | あり | INDEX | **削除** (既存挙動、v2 表の誤りを訂正) | PERSISTED / DISCARDED |
| CAS を別終端者に負けた | 無し | あり (backtest 時) | — | — | DISCARDED |

## 4. 検証
- 単体: L0 (timeout 後・freeze 前に handler 完了 → 記録されない、期限内 → 記録される、両方 barrier で決定論的) / L1 (7 経路 × outcome、SAVEPOINT 失敗 (KeyError 注入) で行なし・終端完了・DISCARDED、外側失敗で PERSISTED にならない) / L2 (成功時に archive 作成、hash A→B→A で 3 dir、失敗 backtest では作らない、書込失敗で結果は返る、rename 前の中断で部分 dir が残らない) / `latest_in_sample_metrics` が failed 行を返さない (乗っ取りの再現 → 絞りで red) / `accepted_entries` が trial_count と行数を一致させる / GC (バイト・件数・symlink・部分 dir・malformed 名) / best の順序 (pf None、trades 同点)。
- 並行性: handler が走行中のまま freeze が来る (barrier) → freeze が予約完了を待ち、record された entry だけが commit 相で publish される / 上限超過で予約分が捨てられ activity に出る、遅延 end_accept は no-op (負値にならない) / rename 成功後に SAVEPOINT rollback → PERSIST_FAILED → 補償 2 回目で hash 再検証して行だけ挿入 / 同 hash の tmp 2 本が rename を競う (先着勝ち、後着は tmp 削除) / 接続 factory 失敗で OPEN のまま外部補償 → `freeze_if_open` で進む / `compensate_commit_failure` 二重呼び出しで永続化は 1 回 / startup sweep と tmp 作成の非同時 (接合テスト)。
- フィクスチャは run9 の transcript の save_kwargs 形。
- 段 0 変異: dispatcher の記録を handler に戻す / SAVEPOINT 除去 / outcome 絞り除去 / hash 照合除去 / rename 前の fsync 除去 / best 順序反転 / GC 上限 ±1 / accepted_entries を trial_count で使わない。
- 実機: A4 10 回目で失敗 mission 後に `backtest_runs (mission_outcome='failed')`、`candidate_archives`、`plugins/_archive/<N>/<hash>` が揃い、`get_signals` の deployed 成績が変わらない。

## 5. v1 → v2 差分 (codex 1 周目の裁定)
| 指摘 | 裁定 |
|---|---|
| C1 監査境界が freeze だけでは守られない | 採用: 記録主体を dispatcher (L0) |
| C2 終端時コピーでは測定本体を保全できない | 採用: backtest 時点 snapshot、hash 別 dir (L2) |
| M1 消費者が outcome で分離されていない | 採用: `mission_outcome` 列 + `latest_in_sample_metrics` の絞り |
| M2 error entry と trial_count の不整合 | 採用: `accepted_entries` を共有 |
| M3 SAVEPOINT の fail-soft と状態遷移 | 採用: Exception 全般、RELEASE、PERSISTED は commit 成功後のみ、DISCARDED の意味は不変 |
| M4 終端経路の列挙不足 / failed report | 採用: 全終端表 (§3)、新 report 種別は作らず note + INDEX |
| M5 DB 行と archive の対応 | 採用: `candidate_archives` 表 |
| M6 sweep の接合先 / 耐久化 | 採用: `sweep_orphans` branch、version_store 流儀 |
| Minor 1 model 向け tool の先送り | 維持 |
| Minor 2 保持上限と best | 採用: バイト + 件数、順序を明文化 |
| Minor 3 変異の次元 | §4 に反映 |

## 6. v2 → v3 差分 (codex 2 周目の裁定)
| 指摘 | 裁定 |
|---|---|
| C: `_strip_forbidden` で save_kwargs が落ち dispatcher に届かない | 採用: `RpcOutcome(public, private)` の二層契約。子側 ledger は冗長系にならないので削除 |
| M1: timeout した RPC の archive が後から公開される | 採用: 二段階 publish (handler は tmp まで、期限内受理時のみ rename)。tmp は sweep が回収 |
| M2: 補償 2 種の混同・外側補償の欠落 | 採用: §3 に 3 行、`compensate_commit_failure` は永続化 → discard の順に |
| M3: WorkerRunner が ledger を触る層違反 | 採用: `on_rpc_accepted` callback を注入、記録は ImproveLoop |
| Minor: `mission_outcome` 既定 None / TABLE_NAMES | 採用 |
| Minor: GC の削除順序 | 採用: fs 削除確認 → DB NULL、失敗行は次回再収束 |

## 7. v3 → v4 差分 (codex 3 周目の裁定) と実装の切り分け
| 指摘 | 裁定 |
|---|---|
| C: callback (publish → record) と freeze の競合で「archive あり・行なし」 | 採用: ledger に accept 予約、`freeze()` が予約完了を待つ (上限 30 s) |
| M1: 子 ledger 削除は前周の趣旨の取り違え、変更表の欠落 | 採用: 子 ledger は残す、mission_worker / mission_registry は無変更と明記 |
| M2: 同 hash publish の競合規約 | 採用: `os.rename` 直行、EEXIST/ENOTEMPTY のみ先着扱い (version_store 流儀) |
| M3: 外部補償入口で ledger が OPEN の場合・二重呼び出し | 採用: `freeze_if_open()`、2 回目は永続化しない、CAS 失敗は tx rollback |
| Minor: `.tmp-*` 回収は startup sweep 限定 / 検証項目 | 採用 |

**Critical の履歴**: v1 (終端コピー) → v2 (payload 欠落) → v3 (freeze 競合)。3 周とも L0/L2 (RPC 受理と archive publish) の層で、各周で観点が異なる。メモリの規則「3 周連続 Critical の項目は外す」に照らすと、この層を外す案 = 「L1 (全終端で台帳を永続化、metrics のみ) だけを本束にし、archive (L2) と記録主体の移動 (L0) を別束にする」。ただし L0 は監査規則の欠陥そのもの (失敗 mission の行を保存し始めると期限超過行が混入する) なので L1 と切り離せない。**推奨: 4 周目を回して L0/L2 に新規 Critical が出なければ実装、出れば L2 (archive) を別束に分離して L0+L1 だけ先行**。

**task の切れ目 (v6、codex 5 周目 Major 3 で再分割)**: **T1** = L0 + L2 前半 (RpcOutcome / callback 3 種と WorkerRunner 注入 / 予約 token と freeze / handler の tmp snapshot / `accept_drain_sec` と shutdown 算入) → **T2** = migration + store (`mission_outcome` / `candidate_archives` / `latest_in_sample_metrics` の絞り / TABLE_NAMES) → **T3** = L1 + L2 後半 (accepted_entries / 全終端 SAVEPOINT / commit 相 publish と冪等再試行 / 補償 2 種と `PERSIST_FAILED`) → **T4** = L3 + L4 (note / INDEX / GC)。T1 と T2 は並列可、T3 は両方に依存、T4 は T3 に依存。

## 8. v4 → v5 差分 (codex 4 周目の裁定)
| 指摘 | 裁定 |
|---|---|
| C1: 受理点と予約取得が原子的でない | 採用: 予約は RPC 開始時 (`on_rpc_begin`)、timeout/error で解放。FROZEN 後の RPC は `mission_finalizing` で拒否 |
| C2: 30 s 打ち切りで実行中 callback を取り消せず「archive あり・行なし」 | **前提を変更**: callback は in-memory 記録だけ。archive の publish (rename) を commit 相 (slot スレッド) に移し、台帳にある entry だけを publish。打ち切りは在庫を壊さない |
| M1: drain が shutdown 判定に未接続 | 採用: supervisor の join deadline に算入、service の graceful 判定に improve thread |
| M2: 2 回目の補償を ledger 状態だけで抑止 | 採用: `PERSIST_FAILED` を再試行可として分離、DB の mission 状態を確認して経路を選ぶ |
| M3: 子 ledger の記述矛盾 | 採用: 変更表を修正 (子側契約は無変更) |
| 分離判断 | L2 は「handler は tmp まで」+「publish は commit 相」に単純化したので**本束に残す**。ただし 5 周目で L0/L2 に Critical が出たら L2 を別束にする (§7 の条件を維持) |

## 9. v5 → v6 差分 (codex 5 周目の裁定) — 実装仕様として確定
| 指摘 | 裁定 |
|---|---|
| 新規 Critical 0 (L0/L2 の在庫は壊れない) | **実装に進む。L2 は分離しない** |
| M1: rename 成功後の rollback を `PERSIST_FAILED` で再試行すると ENOENT | 採用: tmp 無し + final あり → hash 再検証して行だけ挿入 |
| M2: `PERSIST_FAILED` の遷移が §1 と §8 で矛盾 | 採用: §1 を権威に修正 (保存失敗 = PERSIST_FAILED、打ち切り/CAS 負け = DISCARDED) |
| M3: task 分割が v5 と不整合 | 採用: T1 (L0 + tmp snapshot) ∥ T2 (migration) → T3 (全終端 + publish + 補償) → T4 |
| Minor 1: v4 の残記述 | 採用: 変更表・検証・終端表を修正 |
| Minor 2: `mission_finalizing` が streak で abort に至る | 許容 (finalizing 中の反復呼び出しを止める挙動として妥当) |
| 実装条件: 予約 token / generation | 採用 (§L0) |

## 変更履歴

| 日付 | 版 | 変更 | 理由 (レビュー指摘 / 実機観測 / 裁定) | commit |
|---|---|---|---|---|
| 2026-09-09 | v1 | 起案。動機 = run9 #67 (pf 1.387 / max_dd 4.03%) が abort 終端で台帳ごと破棄された観測 | 実機観測 `tmp/a4-run9-20260909.md` 観測 B | - |
| 2026-09-09 | v2 | C1 監査境界 (記録主体を dispatcher へ) / C2 終端時コピーでは本体を保全できない (backtest 時点 snapshot へ) 他 M1〜M6・Minor 1〜3 を反映 | codex 1 周目 Critical 2 / Major 6 / Minor 3 (`codex-design-review.md`) | - |
| 2026-09-09 | v3 | `RpcOutcome(public, private)` 二層契約 / 二段階 publish (tmp→期限内受理時のみ rename) / 補償 2 種の切り分け / `on_rpc_accepted` callback 注入 | codex 2 周目 Critical 1 / Major 3 (`codex-design-review-2.md`) | - |
| 2026-09-09 | v4 | callback (publish→record) と freeze の競合を是正 (accept 予約 + drain 付き freeze) / 子 ledger 残置の明記 / `os.rename` 直行の競合規約 / `freeze_if_open()` | codex 3 周目 Critical 1 / Major 3 (`codex-design-review-3.md`) | - |
| 2026-09-10 | v5 | L0/L2 再構成: 予約を RPC 開始時に前倒し / archive publish を callback から commit 相 (slot スレッド) へ移し、打ち切りが在庫を壊さない構造に変更 | codex 4 周目 Critical 2 / Major 3 (`codex-design-review-4.md`) | - |
| 2026-09-11 | v6 | 実装仕様として確定 (新規 Critical 0)。PERSIST_FAILED 再試行時の hash 再検証によるべき等性 / `PERSIST_FAILED` 遷移の権威を §1 に統一 / task 分割 T1〜T4 の再分割 | codex 5 周目 Major 3 / Minor 2 (`codex-design-review-5.md`) | - |
| 2026-09-10 | 実装 T1 | 台帳の受理境界を dispatcher へ移す (callback 3 種・予約 token/generation・drain 付き freeze・handler の tmp snapshot) | v6 §L0・§L2 前半の実装 | `696ea27` |
| 2026-09-10 | 実装 T2 | `backtest_runs.mission_outcome` 列・`candidate_archives` 表 (fresh DDL) を追加、`latest_in_sample_metrics` を outcome で絞り込み、設計書 §3.4/§3.6/§4.1 を v6 へ改訂 | v6 §L1・§2 (migration/store) の実装 | `29f260e` |
| 2026-09-10 | 実装 T3 | 全終端で台帳を永続化 (SAVEPOINT ledger、非成功 6 経路 + `.part` sentinel)・`accepted_entries` 共有・commit 相の archive publish と冪等再試行・ledger 終端状態遷移・`ImproveSupervisor.is_alive` の graceful 判定算入 | v6 §L1・§L2 後半の実装 (codex 実装、ChatGPT 上限で最終報告前に停止 + 指揮者是正 1 件) | `df6220f` |
| 2026-09-10 | 実装 T4 | `best_candidate` 順序・system note の `best=` 追記・`plugins/_archive/INDEX.md` (1 mission 1 行)・archive GC (バイト/mission 数上限)・`improve.archive_max_bytes`/`archive_max_missions` | v6 §L3・§L4 の実装 | `1a9d4f1` |
| 2026-09-10 | 是正 | callback 例外時の受理境界を fail-closed に (`on_rpc_begin`/`on_rpc_accepted` 例外時の release フォールバック、前処理を finally 内へ) | codex 1 周目レビュー Important (`tmp/review-20260910-ledger/codex-r1.md`) | `3dbc704` |
| 2026-09-11 | 是正 | INDEX.md 追記のプロセス内 lock 直列化 (二重行防止) / archive GC の DB 更新失敗を rollback + `sweep_archive_db_failed` に | codex 1 周目レビュー (T3+T4) Important 2 件 (`tmp/review-20260910-ledger/codex-r1-t3t4.md`) | `c0ddb15` |
| 2026-09-11 | 是正 | 親ゲート行への `mission_outcome` 必須化 / `_rpc_worker` の `result_queue` 束縛修正 / `analyze_corr` の private 全量化 / tmp snapshot は OPEN のときのみ / chmod ツリー復元の統合 / GC 報告の統一 / `Thread.start` 失敗時の予約解放 / 外側 commit 後処理の `_settle_ledger_after_commit` への統合、他 | `/code-review high` 2 周目 10 件 (`tmp/review-20260910-ledger/code-review-r2.md`) | `10f4d8c` |
| 2026-09-11 | 是正 | state 読み取りと snapshot 作成の check-then-act 競合是正 (`rpc_abandoned` フラグ、snapshot 前後の再確認) | codex 2 周目 Important (`tmp/review-20260910-ledger/codex-r2.md`) | `c566598` |
| 2026-09-11 | 是正 | `_publish_archives` の `os.rename` が既存の空 dir へ無警告成功する経路を是正 (rename 前に final を mkdir して `FileExistsError` へ正規化) | sonnet 3 周目 Minor (`tmp/review-20260910-ledger/sonnet-r3.md`) | `3151377` |
| 2026-09-12 | 実機観測 | A4 10〜13 回目: approval 経路の永続化は OK。report 経路で INDEX 追記・`latest_in_sample_metrics` の遮断・`ledger_entry_skipped_error` (error 応答の台帳スキップ) を初観測。失敗終端 (failed/timeout/output_invalid) の永続化は未観測 (持ち越し) | 実機観測 `tmp/a4-run11-claude-20260912.md` | - |
| 2026-09-12 | 実機 (A4 14 回目) | 失敗終端 (`tool_budget_abort:max_tool_calls`, codex #77) で F1〜F7 充足: `mission_outcome='failed'` 行 / `candidate_archives` + `plugins/_archive/77/` / `INDEX.md` +1 行 (status=failed, best=) / 型 B note の `last_result` に `best=rsi_adx_range@8d5cedfd pf=0.141 dd=0.083 archive=…` / 遮断句が failed 行を除外 / staging 削除・archive 残置。全終端表の 2 経路目 (failed) を実機確認 (`tmp/a4-run14-failed-codex-20260912.md`) | — |
