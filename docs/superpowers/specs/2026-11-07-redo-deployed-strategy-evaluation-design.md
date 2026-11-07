# 配備中 strategy の評価を修復後の履歴でやり直す — 設計 C2.1

チケット: `redo-deployed-strategy-evaluation-after-time-repair` (保守 / 中)。関連: DST 設計 `2026-11-02-utc-time-rules-dst-design.md` §8 J2 (c)。

## 0. 目的と非目標

**目的。** 履歴の時刻修復 (2026-11-03 `time_repair_dst_v1`) より前の履歴で評価・承認された、配備中の strategy の in_sample / holdout を、今の履歴でやり直す。結果を人間が見て、配備を続けるか (承認) やめるか (却下) を決める。利用者の裁定 (2026-11-07): 配備中 6 本すべてをやり直して、承認の根拠を差し替える。

**非目標。** 修復前の `backtest_runs` / `candidate_archives` / 承認済みの行の payload を書き換えない (記録として残す)。評価の規則 (期間・床・gate) は変えない。indicator は対象外 (indicator gate は合成 fixture を読み、履歴に依らない)。再評価は改善の試行に数えない。

### 0.1 前提の再検討 (裁定済み)

- **J1 (裁定済み)**: 根拠の差し替えは **新しい承認の依頼** で表す。`apply_decision` は status / reason / decided_* を更新するが payload は承認時のまま保持し (`store/approvals.py:166-`)、`ops/service.py:977-` の詳細は `payload_sha256` を照合する。承認済みの行の payload は書き換えない。
- **J2 (裁定済み、2026-11-07 利用者)**: 再評価の却下は **配備停止と一体**。既存の disable と同じ処理 (pending / claimed signal の整理、symlink を外す、配備解除の通知) を行う。shadow の観測と open position の保護は続け、強制決済しない。lock 済みの helper に分けて再入 deadlock を避ける (`plugin/deploy_ops.py:3-6`)。
- 配備中の版の再実行は既にある: 共有の gate `evaluate_strategy_adoption_gate` (`plugin/strategy_gate.py:393`) を配備中の版の meta / resolved で呼べば、submit / bless / improve と同じ期間規則・床・記録の形で評価できる。gate は `resolved` を再解決しない (`:406-409`)。
- 承認状態は 1 か所では決まらない。`approved_plugins` は「`(name, content_hash)` の最新の approved / rejected」で版を入れる (`tools/plugin_loader.py:113-170`、pending / expired / invalidated は数えない) が、取引入口の `source_state` は最新決定をその場で読む (`plugin/source_state.py:42-44`)。§3.1 で状態ごとに分ける。

## 1. 現物の確認 (file:line、`src/agentic_fx/` 基準)

| 事項 | 位置 |
|---|---|
| 共有の gate (in_sample → 床 → holdout → 床、`floor_mode`、`record_fn`)。評価不能の早期 return | `plugin/strategy_gate.py:393`、床 `:118-170`、dataset 未設定・参照履歴不足 `:480-503`、trades 不足 `:608-614` |
| 期間規則 (in_sample は最古の足〜 `now − holdout_months`、holdout はその後〜 `now`、`now` は足に正規化) | `backtest/holdout.py:~125-216`、正規化 `:193-213` |
| 記録 `backtest_runs` (`mission_outcome` に CHECK は無い、保存 API も値を制限しない) | `store/db.py:620-636`、`store/backtest_runs.py:131-159` |
| bless の流れ (gate を warn、`record_fn` で溜め、承認の行と gate の行を 1 tx) | `plugin/switch.py:2354-`、`_write_gate_rows` `:1183-1198`、tx `:2450-2458` |
| 承認の入口 (候補 locator の解決、未知 origin は拒否、epoch の CAS、版作成・git・journal) | `plugin/switch.py:1825-1870`、`resolve_candidate_dir` `:864-871`、`:1625-1674`、`_finalize_decision` `:1565-1589` |
| 後発承認で pending を陳腐化する判定 (別 content_hash の新しい approved) | `plugin/switch.py:1546-1562`、適用 `:1871-1883` |
| 却下の入口 (決定と staging 掃除だけ) | `plugin/switch.py:2224-2282`、shell の activity `commands.py:208-209`、ops の `approval_state_changed` `ops/service.py:1972-1975` |
| 承認の結果の語彙 / ops が成功とみなす語彙 | `plugin/switch.py:48-51` `APPROVAL_OUTCOMES`、`ops/service.py:144` `_DEPLOYED` |
| disable (lock 済みで決定 / intent / signal 整理を 1 tx、その後 symlink を外し通知) | `plugin/deploy_disable.py:108-223`、`deploy_ops.record_intent` `:299-334`、`try_notify` `:351-372`、`acquire_op_locks` `:248-273` |
| signal の整理 (pending は abandoned、claimed は op に結ぶだけ) | `store/signals.py:679-706` |
| 取引入口 (signal 投入・OPEN 最終確認で最新決定と live を読む) | `plugin/source_state.py:30-60`、`plugin/signal_producer.py:574-582`、`loops/trade_loop.py:599-608` |
| 稼働中の集合の取り直し (運用日境界か deploy op 表の変化) | `signal_admission.py:141-174`、`service.py:1767-1778` |
| 最新決定 / 最新承認 | `store/approvals.py:102-110` / `:112-119` |
| 起動ごとに locator を欠く plugin pending を invalidated にする | `store/db.py:2080-2130` |
| 承認の中身 (pending の候補は staging / human のみ、他は `UNSAFE_SOURCE`) | `ops/change.py:39-62` |
| plugin の承認に期限は無い (`expires_at=None`) | `plugin/approval.py:539`、`plugin/switch.py:1484,2452,2479` |
| 修復は instance lock を取り、履歴の書換え・pending 承認の invalidated・`data_migrations` を 1 tx | `backtest/time_repair.py:622-628,660-720`、`core/history_epoch.py:7-10` |
| daemon の plugin lock 待ちは既定 2 秒 | `config.py:600`、`plugin/signal_producer.py:572` |

human 経路 (submit / bless) の承認の payload には `in_sample` / `holdout` の key が無く、`metrics` (in_sample) だけを持つ。holdout は `backtest_runs` にだけある (`switch.py:1432-1498`)。

## 2. 利用者から見た動き

1. 利用者が `afx plugin reevaluate --all-deployed` (または `<name>`) を打つ。daemon が動いていてよい。
2. 配備中の strategy ごとに、今の履歴で in_sample と holdout を回す (gate は warn)。評価は数分かかるが、その間も取引は止まらない (§3.2)。結果は承認の依頼 (pending) として出る。評価できなかった版は依頼を作らず、理由を表示する。
3. 通知 (Discord の承認カード・web・`afx ctl approval`) は今の承認の依頼と同じ経路で出る。詳細には「再評価: 前回の承認 #id → 今回」の数値、評価時点の違い、コード変更なしが並ぶ。
4. **承認** すると、その版の最新の承認が新しい行になり、根拠が差し替わる。配備は何も変わらない。
5. **却下** すると、その場で配備を止める (`afx plugin disable` と同じ)。新しい signal と新規注文は止まり、symlink が外れ、「配備解除」が通知される。持っている position は保護を続け、強制決済しない。
6. 依頼が pending の間は配備が続く。期限は無い (plugin の承認と同じ)。依頼の後に配備が変わった (別の版への切替・取消・停止) 場合、その依頼は古くなり、承認も却下も効かずに無効になる。もう一度 reevaluate を打つ。

## 3. 規則

### 3.1 状態と反映時点

| 状態 | 正本 | 承認したとき | 却下したとき |
|---|---|---|---|
| 承認取消 | その版の最新決定 (`approval_requests`) | 決定 tx の commit で新しい approved | 決定 tx の commit で rejected |
| 取引入口 (signal 投入・OPEN 最終確認) | `source_state` (lock 下で最新決定と live を読む) | 変化なし | commit 直後から `strategy_not_approved`。claimed は OPEN 最終確認で `source_inactive` で捨てる |
| pending の signal | `signals` | 変化なし | 決定と同じ tx で abandoned、claimed は op に結ぶ (disable と同じ) |
| FS の live | `plugins/<name>` の symlink | 変化なし | 決定 tx の直後、同じ lock の中で外す。失敗は op を `needs_retry` にし、`afx plugin disable <name>` で続きを行う |
| 稼働中 inventory | `DeployedStrategies` | 変化なし | deploy op 表が変わるので次の tick で取り直す |
| 通知 | activity / ops event | 既存の `approval_state_changed` / shell の activity | 決定で `approval_state_changed`、symlink を外した後に `plugin_disabled` (`try_notify`)。回復は既存 disable の契約に合わせる: 通知を試みる前に中断した op は回復経路で出る。試みた後の配送失敗は保証しない (`try_notify` は失敗しても試行済みにし、回復は試行済みの行を飛ばす、`plugin/deploy_ops.py:361-371,399`) |

symlink が外れる前に「配備解除済み」とは表示しない (op の phase をそのまま出す)。

### 3.2 対象と入力の固定 (指摘 4)

1. **対象**: `plugins/<name>` が `.versions/<name>/<artifact_hash>` を指す正規形 symlink で、その版の最新決定が approved の kind = strategy。`--all-deployed` は名前順の全部、`<name>` は 1 本。plain・indicator・未承認は飛ばして理由を出す。
2. **snapshot (短い lock)**: guard (共有) → strategy と依存 indicator の名前 lock (`_plugin_locks`、名前順) を取り、その中で次を読んで固定する。
   - `approved_plugins` の inventory から meta (`content_hash` / `artifact_hash` / `pairs` / `timeframe` / 版 dir の path) と同じ inventory の `resolved[(name, content_hash)]`。live の canonical symlink、版 dir の実 hash、依存 indicator の live / 承認を検証する。
   - 基準の決定: 最新決定の id (approved であること) と、最新承認の id (= 前回の承認)。
   - **配備世代**: その名前の switch journal の最大 op_id と deploy op の最大 op_id の組 (A→B→A を区別する)。
   - `history_epoch`。
3. **評価は lock の外**: daemon の lock 待ち (既定 2 秒) を数分止めないため。再生には live symlink を辿らず、固定した版 dir の meta を使う。`now` はコマンドの開始時に 1 回読み、全 strategy で共通にする。settings も開始時に 1 回読む。
4. **保存 (短い lock + 短い tx)**: 同じ lock を取り直し、2 の全項目を再検証する。違えば結果を捨て、`changed_during_evaluation` を出す (行は書かない)。tx の中で `history_epoch` をもう一度確かめる。修復は guard を排他で取り (`backtest/time_repair.py:632`)、instance lock の下で履歴と epoch を 1 tx で commit する (`:660-720`) ので、開始時と保存 tx 内で epoch が同じなら評価中に修復は commit されていない。

### 3.3 評価不能と失敗 (指摘 8)

- **評価不能** (`evaluable=False`: trades 不足・dataset 未設定・参照履歴不足): 依頼を作らない。activity `reevaluation_unevaluable` (名前・理由) は全ケースで残す。gate の行は生成済みの分だけ、§3.2-4 と同じ lock と再検証の後に `mission_outcome="reevaluation"` で短い tx に残す。trades 不足は in_sample を回した後なので行が残り、dataset 未設定・参照履歴不足は再生前に戻る (`plugin/strategy_gate.py:480-503`) ので行は無い。配備は変えない。
- **失敗** (worker 例外・中断・lock timeout): 行を書かない。activity `reevaluation_failed`。配備は変えない。
- **`--all-deployed`**: 1 本ずつ独立 (snapshot → 評価 → 保存)。1 本の失敗で止めず、最後に名前ごとの結果 (`created #id` / `existing #id` / `unevaluable` / `failed` / `changed_during_evaluation` / `skipped`) を出す。`unevaluable` の行には理由と、止めたいときのコマンド `afx plugin disable <name>` を並べる。`created` と `existing` 以外が 1 本でもあれば終了コードは非 0。再実行は同じ鍵の pending がある名前を評価せずに飛ばし、他は回し直す。

### 3.4 依頼の行 (指摘 3・5・6)

- `approvals_store.create(kind="plugin")` の pending。payload: `name` / `kind` / `content_hash` / `artifact_hash`、`candidate_origin: "reevaluation"`、`candidate_path: "plugins/.versions/<name>/<artifact_hash>"` (起動時の locator 検査 `store/db.py:2080-` を通す)、`in_sample` / `holdout` (pair ごと)、`run_ids` (scope と pair ごとの `backtest_runs` の id)、`history_epoch`、`eval_now`、`reevaluates_approval_id`、`basis_decision_id`、`deploy_generation`、`previous`、`reason: "history_time_repaired"`、`floor_warning` / `floor_detail` / `profitability_floor`、`indicator_deps`、`eval_source` / `base_interval` / `eval_timeframe` / `live_source` (表示の reader が submit / bless と同じ形で読める)。`eval_now` は共通の `now` を `base_interval` の格子へ切り捨てた値 (gate が実際に使う値、`backtest/holdout.py:87-92`)。
- **識別子を継承しない**: `mission_id` / `backlog_id` / `candidate_archive_id` は持たない。`apply_decision` の backlog 更新は no-op。台帳の settle は `apply_decision` の中で `candidate_origin == "reevaluation"` を見て呼ばない (呼ぶと `store/approvals.py:25-29` が warning を書く)。
- gate の行 (`mission_outcome="reevaluation"`) と pending を同じ tx に書く。行を先に書いて id を得てから payload に入れる。
- **一意性**: 鍵 = `(name, content_hash, artifact_hash, history_epoch, basis_decision_id, deploy_generation)`。1 つの名前に pending の再評価は高々 1 件。snapshot で同じ鍵の pending があれば評価を飛ばし、保存 tx の中でも同名の pending 再評価を読み直して、同じ鍵なら作らず既存の id を返す。鍵が違えば古い方を `invalidated` (`reevaluation_superseded`) にしてから作る。修復の計画は pending の strategy 承認を `history_time_repaired` で invalidated にする (`backtest/time_repair.py:530-540`) ので、再評価の pending もこれに乗る (計画が拾う条件は test で確かめる)。
- **`previous`** (前回の承認 = `reevaluates_approval_id`): in_sample は前回の payload の値 (improve 経路 `in_sample`、human 経路 `metrics`)。holdout は payload にあればそれ、無ければ `backtest_runs` から `scope=holdout_gate`・`variant=candidate`・`mission_outcome='approval'`・同じ content_hash / pair / `eval_source` / `base_interval`・`created_at` が前回の承認の作成時刻以下 (≤、文字列でなく時刻として比べる。bless は gate の行と承認を同じ tx で書き、gate の行の `created_at` は同じ `now` を格子へ切り捨てた値なので承認の作成時刻以下になる) の行が pair ごとに **ちょうど 1 行** のときだけ採る (その run id を `previous` に記録)。それ以外は「不明」。値ごとに出所 (payload / run #id / 不明) を持つ。

### 3.5 承認の処理 (指摘 2・3)

公開入口 `approve_candidate` は共用し、`candidate_origin == "reevaluation"` を locator の解決より前に専用の内部経路へ振る。`resolve_candidate_dir` は未知 origin を拒否したまま (他の経路を開けない)。専用経路は次の順。

1. lock: guard (共有) → strategy と payload の依存 indicator の名前 lock → DB。
2. lock 内で行を読み直し、kind と payload digest (`expected_payload_sha256`)、pending を確かめる。`candidate_path` が `name` / `artifact_hash` からの正規形と一致すること。
3. `history_epoch` が今と違えば `history_epoch_changed` (pending のまま、既存と同じ)。未完の switch journal / deploy op があれば既存と同じく拒否 (pending のまま)。
4. **鮮度**: live の canonical target が payload の版、版 dir の実 content / artifact hash、依存 indicator の live と承認、最新決定 id = `basis_decision_id` (approved)、配備世代 = `deploy_generation`。一つでも違えば行を `invalidated` (reason `reevaluation_stale`) にし、結果は既存の語 `outcome="invalidated"` + `reason="reevaluation_stale"` で返す (新しい語 `stale` は作らない。`invalidated` は ops の成功集合に既にある、`ops/service.py:144`)。決定には数えないので、取消済みの版が復活しない。
5. 短い tx で、tx の中で `history_epoch` をもう一度確かめてから `apply_decision(approved, reason="reevaluation_confirmed")`。版作成・git の版記録・switch journal・symlink 操作はしない (`_finalize_decision` は使わない)。
6. **監査記録**: 承認の行 (decided_by / decided_at / reason) と activity `reevaluation_approved` (approval id、name、content_hash、artifact_hash、`reevaluates_approval_id`)。結果の語彙 (`APPROVAL_OUTCOMES`) に `reevaluation_confirmed` を足し、ops の成功集合 `_DEPLOYED` にも入れる。`retry_approval` は `approve_candidate` を素通しする (`plugin/switch.py:2202-2222`) ので、`retry_approval` の側で `candidate_origin == "reevaluation"` を見て拒否する (pending のまま。retry する journal が無い)。ops の決定 worker は reject 以外 (approve と retry) を共通に `approve_candidate` へ渡している (`ops/service.py:1943`) ので、`kind == "retry"` を `retry_approval` へ振り分ける。
7. 後発承認の判定 `_is_superseded` (`plugin/switch.py:1557-1562`) の一致条件に `other_payload.get("candidate_origin") != "reevaluation"` を足す。逐語では `if (other_payload.get("name") == name and other_payload.get("content_hash") != content_hash and other_payload.get("candidate_origin") != "reevaluation"): return True`。再評価の承認は版を変えないので、同名の別候補の pending を無効にしない。

### 3.6 却下の処理 = 配備停止 (J2、指摘 1・7)

`reject_candidate` も同じく専用の内部経路へ振る。**digest**: 再評価の却下は digest を要求しない (今の reject と同じ。ops は reject で digest を見ず `ops/service.py:1915`、`reject_candidate` も `expected_payload_sha256=None` `plugin/switch.py:2265`)。古い内容での誤停止は鮮度照合 (basis / 世代 / live / epoch) で防ぐ。

1. lock と、digest を除く §3.5-2〜3 の確認 (epoch が違えば `history_epoch_changed`、pending のまま)。
2. 鮮度が違えば §3.5-4 と同じく `invalidated` (reason `reevaluation_stale`)、配備は変えない (別の版を止めない)。
3. 鮮度が合えば 1 つの `BEGIN IMMEDIATE` で、tx の中で `history_epoch` をもう一度確かめてから、`apply_decision(rejected, reason=<人間の理由>)`、`disable_symlink` の deploy op intent (subject = 評価した live、取り下げる承認 = `reevaluates_approval_id`、revoke はしない: 却下そのものが最新決定を rejected にする)、その identity の pending / claimed signal の整理、を書いて commit。
4. 同じ lock の中で、live が intent の from と一致すれば symlink を外して op を `done`、違えば `foreign`、unlink 失敗は `needs_retry`。各 phase で `try_notify`。
5. `record_intent` は自前で tx を開くので、tx を開かない lock 済み helper (intent + signal 整理) に分け、disable と再評価の却下の両方から呼ぶ。lock の中から `disable_plugin` / `approve_candidate` を呼ばない。
6. 台帳 (`candidate_archives`) は触らない (disable と同じ)。shadow の観測 (`signal_shadow`、`store/db.py:198`、signal 整理の対象外) と open position の保護 (SL・決済処理) は続ける。
7. **結果の形**: 今の `reject_candidate` は bool を返し、ops は結果を固定で作り (`ops/service.py:1930-1942`)、shell は無条件に activity `rejected` を書く (`commands.py:208-209`)。専用経路は `ApprovalOutcome` (outcome / status / op_id / reason に `deploy_phase` を足す) を返し、ops の結果・event・shell の activity・Discord の文言はすべてこの結果から作る。却下の 5 形:

| 形 | outcome / status / deploy_phase | ops | 文言 (shell / ctl / web / Discord) |
|---|---|---|---|
| 停止完了 | `rejected` / rejected / done | 成功、event `state=rejected`・`deploy_phase=done` | 却下し、配備を停止しました |
| unlink 失敗 | `rejected` / rejected / needs_retry | 成功、`deploy_phase=needs_retry` | 却下しました。取引は止まりましたが symlink を外せていません。`afx plugin disable <name>` で完了してください |
| live が想定外 | `rejected` / rejected / foreign | 成功、`deploy_phase=foreign` | 却下しました。取引は止まりましたが plugins/<name> が想定と違うため外していません。確認してください |
| 古い依頼 | `invalidated` / invalidated / — (reason `reevaluation_stale`) | 成功、event `state=invalidated` | 依頼が古いため無効にしました。配備は変えていません。再評価をやり直してください |
| 履歴が変わった | `history_epoch_changed` / pending / — | `INVALID_STATE` (承認と同じ) | 履歴が変わったため決定していません |

`rejected` を `APPROVAL_OUTCOMES` と ops の成功集合に足す。ops は通常の reject (bool) と再評価の reject (`ApprovalOutcome`) を区別し、通常の reject の `False → INVALID_STATE` (`ops/service.py:1938-1939`) は今のまま保つ。activity は outcome ごとに `reevaluation_rejected` (phase 付き) / `reevaluation_invalidated` を書き、`rejected` を無条件には書かない。Discord の reason は `None` → `""` で `approval_requests.reason` が空になるのは許容する。

### 3.7 reader ごとの扱い (指摘 5)

| reader | 扱い | 理由 |
|---|---|---|
| `in_sample_view` (改善ループの成績面) | 対象外。条件は `(mission_outcome IS NULL OR mission_outcome <> 'reevaluation')` (NULL の行を落とさない) | 再評価は改善の試行ではない。改善ループの入力を変えない |
| `latest_in_sample_metrics` (get_signals の成績添付) | 承認済み再評価だけ。name は signal 行の `plugin` 列 (`tools/signal_tools.py:153,166`) を新しい引数で渡し、`(name, content_hash)` の最新承認が再評価ならその `run_ids` の pair の行、そうでなければ今の SQL | 根拠の差し替え (J1) を取引判断に見せる数値にも及ぼす |
| `find_matching_approved_metrics` (質検査) | 再評価全件を母集団に入れる | 修復後の履歴で回る候補の複製は修復後の数値でしか一致しない。既存も承認の status を問わない |
| `baseline_monitor.monitor_summary` | 対象外 (`compare_mode="off"` で baseline 行を書かない) | — |
| `family_attempt_projection` / 台帳 | 対象外 (台帳に行を作らない) | 試行に数えない |
| `ops/change.py` (承認の中身) | 再評価は `candidate_path == f"plugins/.versions/{name}/{artifact_hash}"` の等値検査の後に `version_source(name, artifact_hash)`、要約は「コード変更なし」 | 今は `UNSAFE_SOURCE` で詳細が出ない |
| `deploy_deps._pending_pins` / version GC / rollback の決定集合 | 今のまま (依存 pin・`artifact_hash` を持つ) | pending の間 indicator を pin し、承認後は版 dir を守る |
| `scripts/reevaluate_approvals.py` | 今のまま (`in_sample` / `holdout` の形で読める) | — |

### 3.8 表示

承認の詳細 (ctl / shell / web / 承認カード) の見出しに「再評価 (前回の承認 #id)」、pair ごとに in_sample / holdout の pf / trades / avg_r / max_drawdown を前回 → 今回で並べる。前回の値は出所を付け、不明は「不明」。評価時点 (前回の承認日時 / 今回の `eval_now`) を利用者 tz の表記で示し、期間が違う比較だと明記する。期間の端点は出さない。

**却下の確認と終端文言**: 再評価の依頼では、Reject の確認と結果に「配備停止」を明記する。Discord は確認の文 (`afx-ops/afx_ops/gate/service.py:258` の "Confirm this decision.") を再評価の reject のとき「Reject すると strategy <name> の配備を停止します (position は保護を続けます)」にし、終端文言 (`gate/render.py:368` の「が却下されました」) は §3.6-7 の表の文言にする。ctl / shell / web の reject の確認と結果も同じ。bot が見分けられるよう、ops の承認詳細と `approval_state_changed` の event に `candidate_origin` と `deploy_phase` を載せる。

## 4. 配線表

| 場所 | 配線 |
|---|---|
| `plugin/switch.py` | `reevaluate_deployed` (§3.2〜3.4)、`approve_candidate` / `reject_candidate` / `retry_approval` の origin 振り分けと専用経路 (§3.5・3.6)、`ApprovalOutcome.deploy_phase`、`APPROVAL_OUTCOMES`、`_is_superseded` |
| `plugin/deploy_ops.py` / `plugin/deploy_disable.py` | tx を開かない intent + signal 整理の helper、disable からも使う |
| `store/backtest_runs.py` | §3.7 の reader の絞り (`in_sample_view`、`latest_in_sample_metrics`、`find_matching_approved_metrics`) |
| `store/approvals.py` | `apply_decision` で再評価の台帳 settle を呼ばない |
| `tools/signal_tools.py` | `latest_in_sample_metrics` に name を渡す |
| `ops/service.py` / `ops/change.py` / `commands.py` / `ops/client.py` | 却下の結果を `ApprovalOutcome` から作る、成功集合、event の `candidate_origin` / `deploy_phase`、承認の中身、shell の activity、§3.8 の表示 |
| `afx-ops` の承認カード・確認文・終端文言・web | §3.8 の表示と配備停止の明記 |
| `backtest/cli.py` / `cli_surface.py` | `afx plugin reevaluate [--all-deployed \| <name>]` (最上位レーン、`Label.CHANGE`、confirm)、§3.3 の結果表と終了コード |
| `docs/operations/` | 修復の後に再評価を回す手順 |

`candidate_origin` / `candidate_path` を読む箇所は実装時に全数 grep で数える。

## 5. 受入条件

1. 配備中 6 本の複製で `--all-deployed` を回すと 6 件の pending が出て、各 payload の in_sample / holdout が同じ `now` の gate の結果と一致し、`run_ids` がその行を指す。`previous` は出所付きで、特定できない値は不明。2 回目は評価せず pending を増やさない。epoch が変わった後の実行は古い pending を invalidated にして作り直す。
2. 評価不能の版は依頼を作らず、activity が残り、配備は変わらない。trades 不足のケースでは生成済みの `reevaluation` の行が残り、再生前の評価不能 (dataset 未設定・参照履歴不足) のケースでは行が無い。2 ケースを別の test で確かめる。1 本の失敗で他の版は止まらず、終了コードは非 0。
3. 評価中に artifact 改変・依存 pin 変更・epoch 変更・切替が起きると `changed_during_evaluation` で何も書かない。保存 tx の失敗は gate の行も pending も残さない。
4. 評価中も daemon の signal 投入が lock timeout にならない。
5. 承認すると最新承認が新しい行になり、symlink・inventory・版 dir・git・journal は変わらない。詳細に前回 → 今回が出る。承認後、get_signals の添付成績が再評価の値になる。同名の別候補の pending は承認できる (`_is_superseded` の filter を外す変異で落ちる test)。
6. 却下すると、commit 直後から新しい signal と OPEN が止まり、pending signal は abandoned、symlink が外れ、op は done、`plugin_disabled` が通知され、次の tick で inventory から外れる。既存の shadow は観測を続け、open position は保護処理を続け、強制決済されない。unlink 失敗は `needs_retry` で `afx plugin disable` で完了できる。通知を試みる前に中断した op は回復経路で `plugin_disabled` が出る。試みた後の配送失敗は再送しない (既存 disable と同じ)。
7. 依頼の後に切替・取消・停止・A→B→A・別の承認があると、承認も却下も `outcome=invalidated`・reason `reevaluation_stale` で行が invalidated になり、配備は変わらない (取消済みの版が復活しない)。epoch の変更は 2 つに分けて確かめる。(a) pending を残したまま epoch だけ変えた fixture: 承認・却下は `history_epoch_changed` で pending のまま。(b) 実際の修復の統合試験: 修復が pending の再評価を invalidated にしてから epoch を更新する (`backtest/time_repair.py:711-717`) ので、修復後の承認・却下は既に決定済みとして拒否され、追加の副作用 (決定・deploy op・signal・FS の変化) が無い。
8. 再起動しても pending の再評価は invalidated にならない。ops の承認詳細が `UNSAFE_SOURCE` にならない。
9. 修復前の `backtest_runs` / `candidate_archives` / 承認済みの行の payload は変わらない。`in_sample_view` に再評価の行が出ず、`mission_outcome` が NULL の行は出る。
10. 却下の 5 形 (§3.6-7) が ops の結果と event (`state` / `deploy_phase`)、shell の activity、ctl / web / Discord の文言にその形どおり出る。古い依頼の却下で「却下」と表示されない。
11. Discord で再評価の依頼の Reject を押すと、確認の文に「配備を停止します」が出て、終端文言が §3.6-7 の表どおりになる。ctl / shell / web も同じ。
12. bless で承認された版 (gate の行と承認が同じ tx、行の `created_at` は格子へ切り捨てた `now`) の再評価で、`previous` の holdout が `backtest_runs` の run id から復元される。同じ版を 2 回 bless した複製では「不明」になる。
13. 実時刻の例で `eval_now` と表示を pin する: 開始 `2026-11-08 03:17:42Z`、`base_interval=1m` なら `eval_now = 2026-11-08T03:17:00Z`、利用者 tz `Asia/Tokyo` の表示は `2026-11-08 12:17:00 +09:00`。
14. 段 0 の変異スイープの対象: 鮮度照合の各項目 (live / 版 dir hash / 依存 pin / `basis_decision_id` / `deploy_generation` / tx 内の epoch)、一意性の鍵の各要素、`_is_superseded` の filter、approve / reject / retry の origin 振り分け、`in_sample_view` の NULL 条件、台帳 settle の分岐。
15. origin=reevaluation の行が既存の plain reject (bool の経路) を通らない (振り分けを外す変異で、symlink が残ることを test が捕まえる)。
16. 再評価の行への retry は、`retry_approval` の直接呼び出しでも ops 経由 (ctl / API / shell) でも拒否され、pending・最新の決定・FS が変わらない (ops の `retry` の振り分けを外す変異で落ちる)。通常の reject は `False` のとき今までどおり `INVALID_STATE` になる。

## 6. 残余

- 再評価は修復が起きるたびに利用者が打つ (自動では回さない)。`history_epoch` が変わったことを status で知らせるかは別件。
- 評価は 1 つの read snapshot ではない。修復は epoch で検出するが、評価中の MT5 復旧 backfill などで `now` 以前の足が増えると結果に混ざりうる (submit / bless と同じ)。
- 評価不能の版は古い根拠のまま配備が続く (§8 要判断)。
- 却下で止めた版の台帳は approved のまま (disable と同じ)。改善ループの族別試行には反映しない。
- 配備世代は名前ごとの journal / deploy op の最大 op_id なので、live でない版の revoke (deploy op 行を作る `store/plugin_deploy_ops.py:60` `insert_done_revoke`) でも進み、live が変わっていなくても依頼が古くなる。安全側に倒れる (やり直せば済む) ので許容する。

## 7. task 分割 (案)

| task | 内容 | 担当 |
|---|---|---|
| T1 | 承認 / 却下 / retry の専用経路、ops の `kind == "retry"` を `retry_approval` へ振り分け、disable の helper 分割、結果の形と語彙 (通常 reject の bool は保つ)、ops の結果・event・中身、shell の activity | sonnet (plugin の lock と実 FS の test が sandbox で回らない) |
| T2 | `reevaluate_deployed`・CLI・保存・reader の絞り | sonnet |
| T3 | 表示と文言 (ctl / shell / web / afx-ops の承認カード・確認文・終端文言)。T1 の結果の形に依存 | sonnet |
| T4 | 本番で `afx plugin reevaluate --all-deployed`、評価中に daemon が止まらないことを実測 (§5-4)、利用者が承認 / 却下 | 指揮者 + 利用者 |

順序は T1 → T2 → T3。pending を作るコード (T2) は、専用の却下経路 (T1) より先に本番へ入れない。先に入ると再評価の行の却下が既存の reject を通って plain rejected になり、symlink が残り、signal の整理も `plugin_disabled` も出ない (J2 違反)。§5-15 で pin する。

## 8. 裁定

### 裁定済み
- **J1** (2026-11-07): 新しい承認の依頼で根拠を差し替える。承認済みの行の payload は書き換えない。
- **J2** (2026-11-07 利用者): 再評価の却下は配備停止と一体。既存の disable と同じ処理、shadow と open position の保護は続け、強制決済しない。lock 済みの helper に分ける。

### 要判断
- **評価不能になった配備中の版をどうするか**: T4 で実際に評価不能が出たときだけ問う (6 本とも評価できれば問わない)。それまでは既定案 (依頼を作らず配備を続け、理由を表示・記録する) で動き、結果表の `unevaluable` の行に `afx plugin disable <name>` を並べて利用者が決められるようにする。裁定が要る理由: 評価不能の版では「根拠を差し替える」が果たせず、古い根拠のまま取引を続けてよいかは運用方針で、技術では決まらない。

### 技術上の決定
- 評価は共有の gate を `floor_mode="warn"`、`compare_mode="off"` で呼ぶ。lock は snapshot と保存の 2 回だけ短く取る。
- 古い依頼 (配備世代・最新決定が変わった) は承認・却下とも決定せず、既存の語 `invalidated` + reason `reevaluation_stale` で終える。
- 再評価の却下は digest を要求せず、鮮度照合で誤停止を防ぐ。
- 修復前の記録は書き換えない。gate の行は `mission_outcome="reevaluation"` で残し、reader ごとに §3.7 で扱う。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-07 | C0 | 初版 | DST J2(c) の裁定 (a) | — |
| 2026-11-07 | C1 | 状態と反映時点、入力の固定と lock 外の評価、専用の承認 / 却下経路、鮮度と一意性、reader ごとの扱い、`previous` の特定、評価不能と部分成功を規定。却下を disable 一体に。J1/J2 を裁定済みに、評価不能の扱いを要判断に | sol 1 周目 (Important 8 + Minor 1)、J2 の利用者裁定 | — |
| 2026-11-07 | C2 | 却下の結果の形 (5 形) と ops / shell / event / Discord の配線、digest の扱い、Discord の確認文・終端文言に配備停止、`_is_superseded` の除外条件を逐語に、get_signals の name の出所、`stale` を `invalidated` + reason に、retry の拒否、台帳 settle の分岐、`in_sample_view` の NULL 条件、`previous` の時刻条件 ≤、`live_source`、配備世代の過剰 stale を残余に、task 順序 T1 (専用経路) → T2、受入条件 10〜15、評価不能の要判断を T4 で出たときだけに | Fable 2 周目 (Important 4 + Minor 8) | — |
| 2026-11-07 | C2.1 | ops の retry を `retry_approval` へ振り分け (T1、§5-16)、通常 reject の `False → INVALID_STATE` を保つ、通知の回復保証を既存 disable の契約に限定、評価不能の行は生成済みの分だけ (2 ケースを別 test)、epoch 変更の受入条件を fixture と実修復に分ける | sol 閉鎖確認 (Important 1 + Minor 3) | — |
