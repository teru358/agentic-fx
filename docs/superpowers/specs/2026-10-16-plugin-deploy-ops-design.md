# 配備済み plugin の deploy ops 設計書

**版:** v1.1
**対象:** `deployed-plugin-has-no-disable-or-rollback`。symlink retire を統合し、`live-signal-eval-blocks-protection-tick` の `core_lock` 保持部分を部分解消する。

## 0. 目的、範囲、決定

配備済み plugin に、人間が実行する disable / rollback / retire / revoke と読み取り専用 versions を加える。正本は `plugin_deploy_ops` であり、既存の switch journal は置き換えない。詳細な既存 switch 操作の安全化は [switch-ops-hardening v1.10](2026-09-19-switch-ops-hardening-design.md)、操作 API の境界は [ops-api design](2026-10-04-ops-api-design.md) を参照する。

| 項目 | 決定 |
|---|---|
| 到達経路 | 最上位 `afx plugin` の人間 CLI のみ。操作 API、`afx ctl`、bot、improve tool には出さない。 |
| 対象 | disable / rollback は正規 symlink 配備のみ。legacy plain dir は retire のみ。 |
| 実行中 daemon | 外す向きは次の signal INSERT から有効、追加・rollback 先の有効化は restart 待ち。 |
| 記録 | `plugin_deploy_ops` と `plugin_deploy_op_signals`。承認行は更新せず、revoke は rejected 行を追記する。 |
| 版 dir | 自動削除しない。symlink は unlink する。保全と手動削除は runbook の責任。 |
| 通知 | activity は通知試行であり exactly-once を保証しない。状態の正本は deploy-op 表。 |
| 障害範囲 | process crash を対象にし、電源断耐久性・directory fsync は対象外。 |

## 1. 用語と非目標

| 用語 | 意味 |
|---|---|
| live | `plugins/<name>` の現在の symlink、plain dir、または不在。 |
| subject | 外す版。rollback では現在 live の版、revoke では revoke 対象版。 |
| target | rollback で置く検証済み版。 |
| blocking/open | `intent` または `needs_retry/syscall_failed`。制限操作が未完である状態。 |
| closed | `done`、`foreign`、または `needs_retry/verify_failed`。 |
| foreign | 第三者の live 変更を検出したため、reconcile が FS を触らず閉じた状態。 |

新しい配備経路、hot reload、版 dir の自動削除、保有注文の扱い、電源断耐久性は本設計の範囲外である。CLOSE / CANCEL / HOLD と scheduler の SL/TP・drawdown kill switch は新しい source lock や拒否の対象にしない。
## 2. データモデル

### 2.1 `plugin_deploy_ops` (新規表)

切替ジャーナルと同じ規律 (journal-first、name flock 内、reconcile で閉じる) で動く別表。既存ジャーナルの消費者は変えない。**この表が配備操作の状態の正本**で、`afx ctl status` の `plugin_deploy_ops` 節、`afx ops status <feature>`、`versions` はここから読む (activity ログは読まない)。

| 列 | 内容 |
|---|---|
| `op_id` | INTEGER PRIMARY KEY |
| `op_kind` | NOT NULL, CHECK IN (`disable_symlink`, `rollback_symlink`, `retire_plain`, `revoke`) |
| `name` | NOT NULL。plugin 名 (正規形) |
| `from_kind` | NOT NULL, CHECK IN (`symlink`, `plain`, `absent`, `none`)。操作開始時の live の種類。`none` は revoke 専用 (FS を触らない) |
| `to_kind` | NOT NULL, CHECK IN (`absent`, `symlink`, `retired`, `none`)。disable = `absent`、rollback = `symlink`、retire_plain = `retired`、revoke = `none` |
| `from_target` | from が symlink のとき readlink の生文字列。それ以外 NULL |
| `to_target` | rollback の戻り先 `.versions/<name>/<hash>`。それ以外 NULL |
| `retired_path` | retire_plain の退避先 (`_retired/<name>-<ts>-<op_id>`)。それ以外 NULL |
| `subject_content_hash` / `subject_artifact_hash` | 外される版 (revoke では revoke する版) の identity (symlink は版 dir から、plain は discover から記録する。plain で得られなければ NULL) |
| `target_content_hash` / `target_artifact_hash` | rollback の戻り先の identity。それ以外 NULL |
| `approval_id` | 外される版の最新 approved 行の id (nullable。`revoke=1` の行と `op_kind=revoke` では NOT NULL) |
| `revoke` | NOT NULL, CHECK IN (0, 1)。同 transaction で `rejected` 行を追記したか |
| `revoke_row_id` | 追記した `rejected` 行の id。`revoke=1` のとき NOT NULL、それ以外 NULL |
| `force` | NOT NULL, CHECK IN (0, 1)。依存 strategy の cascade を伴うか |
| `phase` | NOT NULL, CHECK IN (`intent`, `done`, `needs_retry`, `foreign`) |
| `fail_reason` | CHECK IN (`verify_failed`, `syscall_failed`)。`phase='needs_retry'` のとき NOT NULL、それ以外 NULL |
| `notify_attempted` | NOT NULL, CHECK IN (0, 1)。通知対象 phase の後に activity の**書込を試みた**か。成否は不明で、配送を保証しない |
| `actor` / `created_at` / `updated_at` | NOT NULL。`human_cli`、tz-aware UTC (`updated_at` は §2.4 の単調規則) |

**cross-column CHECK (DDL に固定する)**。列の組が op_kind と食い違う行は INSERT / UPDATE で拒否される。

| op_kind | 固定する列の関係 |
|---|---|
| `disable_symlink` | `from_kind='symlink'` AND `to_kind='absent'` AND `from_target IS NOT NULL` AND `to_target IS NULL` AND `retired_path IS NULL` AND `target_*` が NULL AND `subject_*` が NOT NULL |
| `rollback_symlink` | `to_kind='symlink'` AND `to_target IS NOT NULL` AND `target_*` が NOT NULL AND `retired_path IS NULL` AND `from_kind IN ('symlink','absent')`。`from_kind='symlink'` ⇔ `from_target` / `subject_*` が NOT NULL。`from_kind='absent'` なら `revoke=0` |
| `retire_plain` | `from_kind='plain'` AND `to_kind='retired'` AND `retired_path IS NOT NULL` AND `from_target` / `to_target` / `target_*` が NULL AND `revoke=0` |
| `revoke` | `from_kind='none'` AND `to_kind='none'` AND `from_target` / `to_target` / `retired_path` / `target_*` が NULL AND `subject_*` と `approval_id` が NOT NULL AND `revoke=1` AND `force=0` AND `phase='done'` |
| 共通 | `revoke=1` ⇔ `revoke_row_id IS NOT NULL`。`phase='needs_retry'` ⇔ `fail_reason IS NOT NULL`。`fail_reason='syscall_failed'` は `op_kind != 'revoke'`。`fail_reason='verify_failed'` は `op_kind='rollback_symlink'` に限る。`revoke=1` ⇒ `approval_id IS NOT NULL`。`phase='intent'` ⇒ `notify_attempted=0`。`needs_retry/syscall_failed` から同じ `syscall_failed` 以外 (`done` / `foreign` / `verify_failed` / `intent` を含む) へ遷移するときは、通知を再試行できるよう `notify_attempted` を 0 に戻す。`intent` への巻き戻しは再実行の取り込み (`match_or_reject`)、終端 phase への遷移は終端判定 (`settle_op`) が行う |

- **immutable intent 署名**: `intent` 作成後に変わらない列の組 = (`op_kind`, `name`, 対象 identity = `subject_*`, `to_target`, `revoke`, `force`)。CLI 再実行の引き継ぎ判定に使う (§6.4)。署名列は phase 遷移以外で UPDATE しない。
- **共通述語 (この 1 組だけを使う)**:
  - `blocking/open` = `phase='intent'` OR (`phase='needs_retry'` AND `fail_reason='syscall_failed'`)。制限が未完で、live が「外すと決めた版」のままのこともある。
  - `closed` = `phase='done'` OR `phase='foreign'` OR (`phase='needs_retry'` AND `fail_reason='verify_failed'`)。
  - `syscall_failed` は「同署名の再実行を待つ安定した状態」で、op の終端とは呼ばない (`phase` が `needs_retry` でも op は閉じていない)。「終端」と書くのは `closed` の 3 通りだけ。通知窓 (§7.0) の対象は `phase` が `done` / `needs_retry` (両 fail_reason) / `foreign` の行で「通知対象 phase」と呼ぶ。
  - 使う箇所 (全て同じ定義を呼び、複製しない): 部分 UNIQUE、producer の再検証 (§5.1)、OPEN ゲート (§5.2)、approve / bless / retry の入口と交差 name 検査 (§2.2)、deploy op 開始の検査 (§3 の 3)、reconcile の対象選択 (§6)、CLI 再実行の取り込み (§6.4)、`versions` の表示 (blocking/open と closed を分けて出す) と `afx ctl status` の表示 (`open` = blocking/open、`attention` = `needs_retry/verify_failed` と、注意が要る `foreign`、読めないときは `unavailable`。`done` 全件は載せない)。実装は store 層に SQL 断片 `OPEN_PHASE_SQL` と Python 関数 `is_open(row)` を 1 組置き、全消費者がそれを呼ぶ。以降の表で単に「open」と略す箇所はすべて `blocking/open` を指す。
- **name ごとに blocking/open は高々 1 本**: `CREATE UNIQUE INDEX ... ON plugin_deploy_ops(name) WHERE phase='intent' OR (phase='needs_retry' AND fail_reason='syscall_failed')` (`OPEN_PHASE_SQL` と同じ式)。blocking/open の間は producer の再検証・OPEN のゲート・approve / bless / retry・他の deploy op がその name を扱わない (§2.2, §5)。
- `needs_retry`: 完了できずに止まった状態。`fail_reason` で 2 種に分ける。
  - `verify_failed`: 制限は完了した。rollback の戻り先が再検証に落ちて前進できず、source を unlink 済みで live は不在 (disable と同じ安全側)。**closed (blocking/open ではない)**。人間は原因を直して新規の `rollback --to` か `disable` を打つ。
  - `syscall_failed`: FS の syscall (unlink / rename / symlink swap 等、EACCES・EXDEV など) が失敗して制限が**未完**。live は旧版のままのこともある。**blocking/open のまま**で、producer の見送りと OPEN のゲートは続く。権限・mount を直してから**同署名の CLI を明示再実行**すると続きから完遂する (reconcile も起動ごとに 1 回試みる)。全 op_kind (revoke を除く) に適用する。
  - 遷移時に activity ERROR `plugin_deploy_op_needs_retry` を出す。
- `foreign`: 第三者が live を触った。reconcile は live に触れず ERROR を出して閉じる。name のロックは解放される。制限の DB 効果は残る。回復は種類別の手順 (§10 の 3c) で、専用コマンドは足さない。
- 新規表のため DB 専用 migration は不要（`_SCHEMA` の `CREATE TABLE IF NOT EXISTS` で既存 DB に追加する）。表名を `TABLE_NAMES` に足す (現物 30 表 → 32 表)。ただし settings は schema v3 へ移行し、v2 から `worker.plugin_lock_timeout_sec=2` と `worker.reconcile_lock_timeout_sec=30` を補う。既存値は保持し、`worker` 節が無い、又は mapping でない場合は schema marker だけを v3 にする。

付随表 `plugin_deploy_op_signals(op_id, signal_id, status_at_link, PRIMARY KEY(op_id, signal_id))` と `CREATE INDEX plugin_deploy_op_signals_signal_id ON plugin_deploy_op_signals(signal_id)`。`status_at_link` は CHECK IN (`pending`, `claimed`)。`op_id` は `plugin_deploy_ops` の行 (revoke 単独は `op_kind=revoke` の行) に結ぶ。

- 意図の記録 transaction で、対象 identity に属する **pending と claimed の両方**を記録する。**signals の status を変えるのは pending (→ `abandoned`) だけ**で、claimed は status を触らない (今 LLM に注入されている最中の可能性があるため。止めるのは §5.2 のゲート)。
- `retire_plain` は intent に plain の content hash を記録し、CLI 再実行と reconcile を含む FS 操作の直前に discover を再実行して記録値と比較する。不一致なら rename せず `foreign` に閉じる。記録 hash が NULL (discover 不能) の plain は identity 比較を行わず、plugin 名で退役する。identity を discover できない壊れた legacy こそ retire が要る場面であり、NULL を foreign に倒すと退役できなくなるため。cascade は元々 plugin 名単位なので影響しない。
- 用途: `get_signals` / `claim_oldest` / `pending_exists` の除外 (§5.3)、監査。signals 表の CHECK を作り直さずに「配備操作で放棄・無効化された」ことを区別できる。
- **ゲートで拒否した claimed の結び先**: 意図の記録時に claimed として記録済みの signal は、既に `op_id` を持つ (そのまま)。付随表に無い signal (op に由来しない無効化、例: `foreign` 後の第三者の変更) がゲートで拒否された場合は付随表に追記せず、`trade_intents.gate_result` (`signal_source_inactive`) を正本とし、`get_signals` での見え方は通常の abandoned と同じ。結び先を捏造しない。

### 2.2 切替ジャーナルとの共存

- IV-9: name flock の内側で両表を見る。切替ジャーナルに非終端があれば deploy ops を開始せず、deploy ops に `blocking/open` (`intent` と `needs_retry/syscall_failed`、§2.1) があれば approve / bless / retry / retire を開始しない。後者のため `approve_candidate` / `bless_candidate` / `retry_approval` の入口に「blocking/open の deploy op なら `UnresolvedJournalError` 相当で拒否」を足す。
- **検査の範囲は交差 name** (IV-16): approve / bless / retry は candidate 自身の名前に加え、**candidate の全依存 indicator 名** (payload の `indicator_deps` と config.yaml の参照) について blocking/open の deploy op を検査する。deploy op の開始は、対象名と**全 dependent strategy 名**について open switch journal を検査する。crash 後は lock が解放されているため、通常実行中の lock 競合だけでは A (indicator) の disable intent が残ったまま S (A に依存する strategy) を配備できてしまうのを、この検査が塞ぐ。
- reconcile は name ごとに deploy ops を先に閉じ、次に切替ジャーナルを見る。両方の非終端が同時に存在した場合 (不変条件違反) は何も触らず ERROR。

### 2.3 revoke の追記行 (`approval_requests`)

承認行は UPDATE しない。管理対象は `(name, content_hash)` の最新決定で決まるので、同 key に後から `rejected` 行が入れば loader 側のコード変更なしで除外される。最新決定の正本は `approvals` store の latest-decision helper 群 (`latest_decision_status` / `latest_decision_for_identity` / `latest_approved_for_identity`) であり、`versions`、revoke、rollback、source gate などの呼び出し側は規則を複製しない。

| 列 | 値 |
|---|---|
| kind | `plugin` |
| status | `rejected` (CAS を通さず直接 INSERT。`pending` を経ない) |
| payload | `name`, `content_hash`, `artifact_hash`, `revokes_approval_id` のみ。`candidate_*` / `backlog_id` は持たない (backlog への波及を防ぐ) |
| decided_by | `human_cli` |
| decided_at / created_at | 操作時刻 (tz-aware UTC)。**同 key の既存の最新決定の `decided_at` より厳密に後**になるよう `max(now, 最新決定の時刻 + 1µs)` を使う（最新決定は `decided_at, id` 順で畳む）。`created_at` は NOT NULL なので同値を入れる |
| reason | 固定文言 `revoked_by_operator` のみ。自由記述を受けない |

決定規則:

| 項目 | 規則 |
|---|---|
| 対象版 | `(name, content_hash)`。`artifact_hash` は同 key の最新 approved 行の payload から取る。`--version` は content hash の小文字 hex 前方一致だけを受け付け、12 桁以上かつ一意に決まらなければ拒否する。版 dir 名 (artifact hash) は受け付けない |
| 元 approval id | 同 key の最新 approved 行の id。最新決定が既に `rejected` なら何も追記せず rc=0 (冪等) |
| 現 live の版 | `revoke` 単独操作では**拒否** (producer が走り続けるため)。`disable --revoke` / `rollback --revoke` で外す操作と同時に行う |

復活は同内容を `afx plugin bless` し直すと、より新しい approved 行が入る (意図的な再承認のみ)。`approval <id>` 詳細表示は欠落キーを `get()` で扱うので revoke payload でも例外にならない見込みだが (`commands.py`)、AC-28 で pin する。

### 2.4 統合 read model `plugin_live_history`

「直前の実 live」を決める表示専用の関数 (表ではなく SELECT)。

| 出所 | 条件 | イベント |
|---|---|---|
| 切替ジャーナル | `phase='decided'` かつ `switch_required=1` | `kind` (approve / bless)、`from=old_target` (`old_kind='absent'` なら absent)、`to=new_target`、時刻=`updated_at` |
| `plugin_deploy_ops` | `phase='done'` かつ `op_kind IN ('disable_symlink','rollback_symlink','retire_plain')` (`revoke` 行は FS を動かさないのでイベントを作らない) | `op_kind`、`from`、`to`、時刻=`updated_at` |
| `plugin_deploy_ops` | `phase='needs_retry'` かつ `fail_reason='verify_failed'` かつ `from_kind='symlink'` (reconcile が source を unlink した)。`syscall_failed` は live を動かしていないのでイベントを作らない | `op_kind`、`from`、**実効 `to=absent`** (表の `to_target` ではなく、実 FS の結果)。`from_kind='absent'` の `needs_retry` はイベントを作らない |

- 順序: name ごとに生時刻昇順、同時刻は (出所, id)。読み取り側で `実効時刻 = max(生時刻, 直前の実効時刻 + 1µs)` と畳んで単調にする。
- 書込み側: `plugin_deploy_ops` の `updated_at` は name flock 内で `max(now, その name の両表の最新イベント時刻 + 1µs)`。**既存の切替ジャーナル (`set_phase()` が `now` をそのまま書く) は触らない**。journal 側の時計逆行は読み取りの畳みで吸収し、吸収しきれない順序誤りは次の検証で必ず安全側に倒れる:
- 現 live の検証: 最新イベントの実効 `to` が FS の live と一致しなければ「履歴と live が不一致」として既定の戻り先を出さず、`--to` 必須にする。
- `afx plugin versions` の「経歴」欄もこの read model から出す。

## 3. 操作の振る舞い

共通の手順 (disable / rollback / retire / revoke に共通。誰が何をするか):

1. 人間が repo root で打つ。権限は「その shell の uid が DB と plugins dir に書けること」(bless と同じ)。approver 鍵は使わない。TTY があれば結果の予告を見せて y/N、`--yes` で省略。`--dry-run` は予告まで。
2. 依存 strategy を仮列挙 (§4)、**対象名 + 依存名を名前昇順で `_plugin_locks` で取得**する。lock 内で再列挙し、集合が仮列挙の部分集合でなければ lock を放して再試行 (3 回で拒否)。flock は非再入なので、内部から `approve_candidate` 等を呼ばない。

予告は `Expectation` を返す。本実行は lock 取得後、intent の記録と FS 操作の前に、live の種類と `readlink` の生文字列、subject identity、`to_target`、cascade 集合、open op id、`--revoke` の subject に対する最新承認決定 (`approval_id`, `status`) を再照合する。1 つでも変われば `state_changed` として無変更で終了する。人間が y/N を選ぶ間は lock を保持しない。daemon を止めないためである。

`--dry-run` も foreign と退避先衝突を含め本実行と同じ refusal を返す。予告だけが通ることはない。各操作の lock 待ちは 30 秒、TTY では待機を通知し、timeout は `lock_timeout`（rc 1）とする。予告と本実行がともに lock を取るため、合計待機は最大およそ 60 秒である。
3. lock 内で検査する。1 つでも落ちたら何も変えずに終了する (fail closed)。
4. **意図の記録**を 1 transaction で commit する (lock 内): `plugin_deploy_ops` に `phase=intent`、`--revoke` の `rejected` 行、cascade の pending `abandoned` 化と、`plugin_deploy_op_signals` への pending / claimed の記録。これらは制限する向きの効果だけで、FS より先に確定してよい (IV-10)。
5. FS を動かす。
6. `phase=done` にして commit。その後 activity を出し (`ActivityLog.write` は失敗を握るので成否を見ない)、`notify_attempted=1` にする。

### 3.1 disable `afx plugin disable <name> [--revoke] [--force] [--yes] [--dry-run]`

検査 (手順 3): 名前が正規形 / 切替ジャーナル (対象 + 全 dependent 名) と deploy ops に `blocking/open` が無い (あれば §6.4 の「再実行の取り込み」を除き拒否) / `plugins/<name>` が symlink で字句が正規形 (plain なら「`afx plugin retire` を使う」と案内、不在なら「既に disable 済み」で rc=0) / 依存 strategy が 1 件以上なら `--force` が無い限り拒否 / `--revoke` のとき subject に最新 approved 行が無ければ拒否 (`revoke=1` ⇒ `approval_id` NOT NULL の CHECK と一致)。

`--revoke` で live 不在のときは no-op にせず、「最後に完了した disable 系 op の外した版」を `--version` 候補として表示し、`revoke` 独立操作へ誘導する (rc=2)。

意図の記録: `op_kind=disable_symlink, from_kind=symlink, from_target=<readlink 生文字列>, to_kind=absent`、subject identity は版 dir から得る。cascade の対象 (§4.3) は subject の pending と、`--force` で解決不能になる依存 strategy の pending。付随表への記録はそれらの pending + claimed。

FS: `is_symlink()` を再確認して `unlink` (dir は触らない)。完了表示は `filesystem_switched=yes runtime_deactivation=effective_on_next_signal_insert`。

### 3.2 rollback `afx plugin rollback <name> [--to <hash>] [--revoke] [--force] [--yes] [--dry-run]`

戻り先の決め方:

| 状況 | 既定の戻り先 | `--to` |
|---|---|---|
| 最新の live-changing event (§2.4) が approve / bless で、live と実効 `to` が一致 | そのイベントの `from` | 版 dir の hash (前方一致可、一意でなければ拒否) |
| そのイベントの `from` が absent (初回配備) | 無し。「戻り先が無い。disable を使う」と案内して拒否 | 指定すれば可 |
| 最新イベントが rollback / disable、または live 不在、または履歴と live が不一致 | **無し (`--to` 必須)**。disable した版が黙って復活しない | 必須 |
| live が plain dir | 拒否 (retire を使う) | — |

戻り先の検証 (すべて lock の内側。1 つでも落ちたら変更なし。IV-8):

1. 戻り先 `.versions/<name>/<hash>` が実在し、dir 名が正規形。
2. その版の最新決定が approved (`approvals` store の latest-decision helper 群を呼び、規則を複製しない)。revoke 済み・未承認は拒否。
3. `_version_dir_hashes_ok(dir, content_hash, artifact_hash)` (`switch.py`) が真。
4. `loader._discover_one(dir, name)` が non-None で、kind が現配備と同じ (live 不在のときは直近の done op の subject と同じ kind)。
5. 依存の検算 (§4.2): 戻り先を置いた状態で、(a) **戻り先自身が strategy なら自分の依存が現 inventory で `resolve_indicator_deps(..., pin_mode="require")` により解決できる**、(b) 依存 strategy が `require` で解決できる。(a) の不成立は `--force` でも許さない (`--force` は「他 strategy を止める許可」であり、戻り先自身の依存不整合は対象外)。(b) の解決不能は列挙し、`--force` が無ければ拒否。

意図の記録: `op_kind=rollback_symlink, from_kind=<symlink|absent>, to_kind=symlink, to_target, target identity`。subject は外される版 (live 不在から始めるときは subject なし)。`--revoke` の対象は**外される版 (subject)**。live 不在のときの `--revoke` は拒否し、`revoke --version` を使わせる。

FS: temp symlink `.{name}.link-d{op_id}` を作り `_atomic_symlink_swap`。完了表示は `filesystem_switched=yes runtime_activation=pending_restart`。`rollback 完了` とだけ表示することを禁止する (AC で pin)。

### 3.3 retire `afx plugin retire <name> [--force] [--yes] [--dry-run]`

入口は 1 つ。状態機械は live の種類で分ける。**disable / rollback は symlink 配備だけ、plain は retire のみ**。

| 観点 | symlink | plain dir (legacy) |
|---|---|---|
| 処理 | §3.1 の disable_symlink と**同じ状態機械** (`op_kind=disable_symlink`、`--revoke` なし) | `op_kind=retire_plain` (§6.3)。`os.rename(plugins/<name>, _retired/<name>-<ts>-<op_id>)` |
| 退避 | しない (unlink)。from_target を記録するだけ | 退避先を `retired_path` に記録。`_retired/` は plugins と同一 FS であることを意図の記録前に `st_dev` で確認 |
| 依存確認 | あり | あり (旧実装には無かった)。**plain の依存は config.yaml が参照する indicator 名で判定し、hash は照合しない**（`ResolvedIndicatorSet` に `artifact_hash` がなく、plain に版 dir もないため） |
| cascade | subject の identity で | plain は identity の値にかかわらず plugin 名単位で pending signal を全て放棄 |
| lock | `_plugin_locks` (旧 `retire` の自前 flock は廃止) | 同左 |

`afx plugin disable` は symlink 専用の明示的な名前として残す。`retire` が symlink に当たったら委譲ではなく disable と同じ関数を呼ぶ。activity は既存名 `plugin_retired` を plain で維持し、symlink は `plugin_disabled`。open の disable intent に対する symlink `retire` の再実行は、署名 (§2.1) が一致する (revoke=0、force 同値) ときだけ引き継ぎ、それ以外は拒否 (§6.4)。

### 3.4 `versions` `afx plugin versions <name>`

読み取り専用 (name lock を取らず、DB の行・schema・app が持つファイルを作らず変更しない。SQLite が reader のために置く `-wal` / `-shm` はこの契約の除外)。表示: live の版 (symlink の先 / plain / 不在)、`.versions/<name>/` 配下の版ごとの承認時刻・承認者・最新決定 (approved / rejected)・版 dir の整合 (`_version_dir_hashes_ok`)、§2.4 の経歴、open / `needs_retry` / `foreign` の deploy op (`plugin_deploy_ops` から)、依存 strategy。rollback の `--to` 候補をここから選ぶ。

### 3.5 revoke `afx plugin revoke <name> --version <content-hash-先頭一致> [--yes]`

FS は触らない DB 操作。name lock を取り (approve / bless / disable との直列化)、**1 transaction** で次を行う。

1. §2.3 の `rejected` 行を 1 行追記する。
2. `plugin_deploy_ops` に `op_kind=revoke, phase=done` の行を 1 つ作る (`intent` を経ない。`revoke_row_id` = 追記した行の id、subject identity、`approval_id`)。
3. 対象 identity `(name, content_hash)` の `pending` signal を `abandoned` にし、`pending` / `claimed` を `plugin_deploy_op_signals` に記録する (`abandon_pending_for_identities`)。claimed の status は触らない。

結び先の決定: `op_kind=revoke` 行に一本化する (推奨どおり)。理由は 3 つ。(a) 付随表の親が常に `plugin_deploy_ops` で、除外の anti-join・表示・監査が op_kind で分岐しない。(b) `rejected` 行の id を結び先にすると付随表の親が 2 種類になり、`approval_requests` の行に `op_id` 相当の意味を持たせる誘惑が生じる。(c) 通知窓 (`notify_attempted`) と `versions` の経歴を他 op と同じ規則で扱える。代償は、単独 revoke でも op 行が 1 行増えること。

監査の正本は `approval_requests` の `rejected` 行 (時刻・actor・固定理由 `revoked_by_operator`・`revokes_approval_id`)。`plugin_deploy_ops` の revoke 行は signals の結び先と通知窓のためで、監査の二重化ではない。

- 現 live の版は拒否 (producer が走り続けるため)。name に `blocking/open` の deploy op / 切替ジャーナルの非終端があれば拒否。
- 冪等: 最新決定が既に `rejected` なら何も追記せず rc=0 (新しい op 行も作らない)。
- 取り残しの対処: 旧版の pending が残っていても、除外 (§5.3) と §5.1 の再検証 (最新決定が rejected) と §5.2 のゲートが止める。単独 revoke は同 transaction でそれを abandon と付随表で明示する。
- activity `plugin_revoked name=… version=…` は通知試行 (§7.0)。DB への追記が正で、activity の成否は問わない (`afx plugin versions` が rejected を表示する)。

disable / rollback の `--revoke` は、subject の revoke を意図の記録 transaction に含めるだけの糖衣 (`revoke_row_id` を op 行に持つ)。live 不在のときは受け付けず `revoke --version` を案内する。

### 3.6 CLI outcome と refusal code

outcome は rc と code と文言の組に 1 対 1 で対応し、stderr の 1 行目は `エラー: …(code=<code>)`、次の安全な操作を続ける。`test_every_outcome_maps_to_one_rc_code_and_message` (outcome の表を parametrize で全数固定) と個別 test で次のとおり固定する。

| 群 | rc | code | 内容 | 固定する test |
|---|---|---|---|---|
| 完了・no-op・dry-run | 0 | なし (stderr は空) | 各 op の完了、`already_disabled` / `already_retired` / `already_revoked`、rollback の `already_live` (refusal だが rc 0)、全 op の `dry_run` | `test_every_outcome_maps_to_one_rc_code_and_message`。完了表示は `test_rollback_success_never_reports_a_bare_completion`、予告は `test_dry_run_shows_the_plan_and_changes_nothing`、no-op は `test_a_noop_needs_no_confirmation` |
| usage | 2 | `revoke_requires_version` | live 不在の `--revoke`。`revoke --version` を案内する | 上の全数 test と `test_disable_revoke_without_a_live_points_to_revoke_with_rc_2` |
| 業務拒否 | 1 | `dependents_blocked`, `dependents_unresolvable`, `foreign`, `needs_retry`, `retire_refused`, `target_verification_failed`, `bad_target`, `target_not_found`, `target_ambiguous`, `target_required`, `no_rollback_target`, `live_is_plain`, `signature_mismatch`, `deploy_op_blocked`, `unresolved_journal`, `lock_timeout`, `dependents_changed`, `invalid_request`, `state_changed` | 状態や入力が操作を許さない。rollback の source unlink 後に戻り先の検証が落ちた表示は `target_verification_failed` と `live_removed` を併記する。`retire_refused` は具体的な理由を載せる | 上の全数 test。個別に `test_revoke_of_the_live_version_is_refused_with_a_code` (`invalid_request`)、`test_revoke_prefix_that_matches_no_version_is_refused` (`target_not_found`)、`test_revoke_rejects_a_version_prefix_that_is_empty_short_or_not_lowercase_hex` (`bad_target`)、`test_a_rerun_with_a_different_signature_shows_the_op_and_the_wait_hint` (`signature_mismatch`)、`test_a_lock_held_past_the_deadline_is_a_lock_timeout_that_changes_nothing` (`lock_timeout`)、`test_versions_rejects_an_invalid_plugin_name_with_a_code` (`invalid_request`)。`state_changed` は予告後に前提が動いた各 test (`test_rollback_does_not_run_when_the_live_version_moved_after_the_preview` ほか) |
| 対話結果 | 1 | `confirmation_required`, `aborted` | 端末も `--yes` も無い、または確認で拒否・EOF・Ctrl-C。何も変えない | `test_without_a_terminal_or_yes_nothing_changes_and_confirmation_is_required`、`test_declining_the_terminal_prompt_aborts_without_changes`、`test_eof_at_the_prompt_is_a_refusal`、`test_ctrl_c_at_the_prompt_aborts_with_a_code`、`test_revoke_asks_first_and_records_the_version_only_after_yes` (revoke の `confirmation_required`) |
| OS / DB / 内部 / 設定エラー | 1 | `os_error`, `db_error`, `internal_error`, `config_error`, `db_missing` | OS の失敗、SQLite のエラー、想定外の例外、設定ファイルの不在・不正、DB 未初期化。`versions` は設定や DB が無いとき何も作らない | `os_error` / `db_error` / `internal_error` は全数 test。`config_error` は `test_versions_refuses_missing_or_invalid_settings_with_a_code`、`db_missing` は `test_versions_on_a_root_without_a_database_creates_nothing_and_says_so` |

## 4. 依存 strategy の列挙と cascade

### 4.1 入力集合

- 現在の承認済み inventory: `approved_plugins(conn, plugins_dir, settings=...)` の `inventory` を基準に、`phase1_metas` (第 1 相を通った meta) も走査する。`discover(plugins_root)` だけでは未承認・未 admit の live を区別できない。
- 依存の判定は strategy の `config.yaml` の indicator 参照 (`plugin: <name>`)。
- pending 承認: `approval_requests` の `status='pending'`, `kind='plugin'` の payload の `indicator_deps` が対象 name を pin しているものを**別枠**で表示のみ (人間が再ロックする既存運用に任せる)。

### 4.2 検算

差し替え inventory (対象 name を除去、または rollback 先の meta に置換した `ApprovedInventory`) に対し、各依存 strategy へ `resolve_indicator_deps(..., pin_mode="require")` を呼ぶ。失敗は `(strategy, alias, reason)` として列挙。成功する依存は表示のみ。副作用なし。`--dry-run` はここまで。rollback 先自身が strategy の場合は、戻り先の meta の依存も同じ関数で検算する (§3.2 の 5(a))。

### 4.3 `--force` と cascade

依存 strategy が列挙されたとき、`--force` が無ければ拒否 (strategy 名・alias・理由を stderr に出す)。`--force` を付けたときは次を同じ transaction (意図の記録) で行う。

| 効果 | 内容 |
|---|---|
| producer の停止 | 依存 strategy の symlink は残る。producer は §5.1 の lock 内再検証で、解決不能になった strategy の INSERT を止める |
| pending の cascade | subject と、解決不能になる依存 strategy の `(plugin, content_hash)` の `pending` signal を `abandoned` にし、`plugin_deploy_op_signals` に記録 |
| claimed | status は触らない。同 identity の claimed を付随表に記録する (`get_signals` / `claim_oldest` から除外)。止めるのは §5.2 のゲート |
| 表示 | 完了時に「これらの strategy は再起動後 `not_found` / `pin_mismatch` で管理対象から外れる」と出す |

cascade の SQL は `store/signals.py` の新関数 `abandon_pending_for_identities(conn, identities, op_id)` (identity の集合を渡す。pending を `abandoned` にし、pending / claimed の id を `op_id` で付随表に記録する。1 transaction)。

## 5. daemon 側の変更

### 5.0 lock 順序 (全経路、IV-18)

| 経路 | 取る lock | `core_lock` |
|---|---|---|
| `Scheduler.tick()` 本体 (exits・SL/TP・fills・cron・news / econ / cache hook) | name lock を**取らない** | 保持 (`_scheduler_tick_once` が保持して呼ぶ) |
| signal maintenance (遅延 callable。reclaim / expire / sandbox 評価 / 再検証 / INSERT) | INSERT 区間だけ name lock (名前昇順、deadline 付き)。DB は専用接続 `conn_signal` のみで `conn_core` に触れない | **保持しない** (`core_lock` 解放後に `_scheduler_tick_once` が実行) |
| trade loop の commit-pre (`gather_open_snapshot`) | なし | 保持しない |
| OPEN の commit-core | **name lock (昇順、commit-pre 完了後) → `core_lock`** の順。`open_from_snapshot` の戻りまで両方保持し、`core_lock` → name の逆順で放す | name lock の後に取る |
| CLOSE / CANCEL / HOLD、reflection、その他の `core_lock` 利用者 | name lock を取らない | 従来どおり |
| CLI (disable / rollback / retire / revoke / approve / bless / retry)、startup reconcile | name lock のみ (名前昇順、blocking + deadline) | 取らない |

規則: (1) `core_lock` を保持している間は name lock を取らない。name lock を保持したまま `core_lock` を待つ経路は OPEN だけで、「`core_lock` → name lock」の辺が存在しないので循環しない。(2) name lock は常に名前昇順。(3) OPEN の name lock 待ちは `core_lock` を持たずに行うため、SL/TP・drawdown kill switch を含む scheduler tick は止まらない。(4) test は `_plugin_locks` の呼び出しを計装し、`core_lock` を保持した状態での取得を失敗させる (AC-34)。(5) 遅延 callable は `conn_core` を受け取らず閉包にも入れない。`conn_core` は `core_lock` 保持中だけ使う既存契約のまま、signal maintenance は `conn_signal` だけを所有する (§5.1、IV-20)。


### 5.1 signal maintenance を `core_lock` の外へ + producer の lock 内再検証 + INSERT

**遅延化**: `Scheduler.tick()` は `on_signal_maintenance` を tick 内で同期実行しない。**接続修復 wrapper を毎 tick (state を問わず)** 遅延 callable として戻り値 `pending` に積む。wrapper は `conn_signal` の健全性確認・再生成・連続失敗 counter を担い、**実 maintenance** (`reclaim_expired` → `expire_stale` → producer 評価 → 通知 の全体、現行 `_run_signal_maintenance`) だけを、実行時に `state_fn()=='ready'` のときに限って呼ぶ。ready でない tick は接続の確認・再生成だけを行い、実 maintenance は走らない。`service._scheduler_tick_once()` が `core_lock` 解放後に他の hook と同じ `for hook in pending: hook()` で実行する（既存の遅延 hook と同じ実行形）。wrapper は実行時に `state_fn()` を評価し、従来の `_run_data_hook` と同じ fail-open (例外を握って activity に記録) で包む。callable が使う DB 接続は**専用書込み接続 `conn_signal` だけ**である (下の「専用接続」)。news / econ / cache maintenance は tick 内のまま変えない。

これでチケット `live-signal-eval-blocks-protection-tick` の「`core_lock` を signal 計算が保持する」部分が**部分解消**する (チケットは閉じない、§15): 評価と name lock 待ちの間、`core_lock` は他の利用者 (OPEN・CLOSE・次 tick の保護処理) に開いている。

**producer の INSERT**: 評価 (長い) は lock の外のまま、**INSERT 直前の短い区間だけ** name lock を取る。

1. 評価結果を得たら、strategy 名と、その `ResolvedIndicatorSet.items[].plugin_name` の全 indicator 名を集める（`ResolvedIndicatorSet` に `artifact_hash` はない）。
2. 名前昇順で `_plugin_locks(..., deadline=...)` (blocking flock、deadline 付き、`_bounded_lock_kwargs` に倣う) を取る。deadline 超過は専用の結果型 (例外) で返し、`_evaluate_bucket` はその bucket を**成功扱いにせず cursor を進めない**。catch-up は打ち切り、次 tick で再評価する (鮮度窓を越えた分は意図的に失効)。状態変化ごとに 1 回 activity `plugin_skipped_lock_timeout`。
3. lock 内で**全て**再検証する。1 つでも落ちたら INSERT せず skip し、活動は `plugin_skipped_not_live name=… reason=…` を状態変化ごとに 1 回。比較は共通関数 `live_identity(plugins_root, name)` (symlink は版 dir から `(kind, content_hash, artifact_hash)`、plain は fresh discover の `(kind, content_hash, None)`、不在は None) の結果で行う:
   - strategy 自身: symlink なら `(content_hash, artifact_hash)` が `meta` と一致。plain なら `content_hash` が `meta.content_hash` と一致。
   - 解決済みセットの全 indicator: symlink なら `content_hash` が `item.content_hash` と一致。**plain の indicator は存在 (kind=plain) だけを見る** (`artifact_hash` が無く、hash は照合しない、既存契約)。
   - strategy の `(name, content_hash)` の最新決定が approved (revoke 済みなら見送り)。
   - その name 群に `blocking/open` の `plugin_deploy_ops` (§2.1) が無い。
4. 同じ lock 内で `signals.add()` する (INSERT と commit)。lock を放すのはその後。

これで interleaving は 2 通りに限られる: INSERT が disable の意図の記録より前 → disable の cascade が pending を拾う。後 → 再検証が落ちて INSERT されない。lock 区間は lstat + readlink 数回と 1 INSERT で短い。signal 型 plugin (依存なし) も同じ経路を通る。

`plugins_root` と `conn` (= `conn_signal`) を渡す引数追加が要る。呼び出し元 `service.py` と単体テストの差し替え箇所は task 6 で全数 grep する。

**専用接続 `conn_signal` (必須仕様)**:

| 項目 | 内容 |
|---|---|
| 理由 | `conn_core` は `core_lock` 保持中だけ使う契約で、`check_same_thread=False` は共有を許すだけ（`conn_core` の既存契約）。遅延後の callable は scheduler thread が `core_lock` の外で動かすので、Mission thread の `core_lock` 下の transaction と同じ Connection で交錯し、他 thread の transaction を commit / rollback しうる |
| 所有 | `build_app` が `connect(root/"data"/"agentic.db")` で別 Connection を作り (WAL・`busy_timeout=5000` は `connect()` 既定)、`App.conn_signal` に持つ。使うのは遅延 callable (`reclaim_expired` / `expire_stale` / producer の cursor 更新と `signals.add` / 通知の DB 書込) だけ。Mission・trade loop・reflection・Scheduler 本体・ingest・コマンドは触らない |
| 渡さないもの | `conn_core` は callable に渡さず、閉包にも入れない。`_run_signal_maintenance(conn=conn_signal, ...)` |
| 競合 | 異なる Connection 間は SQLite の書込み直列化に任せ、単一 SQL 文の原子性 (claim / requeue / reclaim) は signals 層の契約のまま。`busy_timeout` (現物 5000ms のまま) を超えた書込み競合は例外になり、callable の fail-open (activity に記録、cursor 不進行、次 tick で再評価) に乗る。接続の後始末は次行の busy policy |
| busy policy | maintenance 境界 (遅延 callable の入口と出口) で、**全例外時に `conn_signal.rollback()`** する。再利用前に `conn_signal.in_transaction == False` を保証する。`rollback()` 自体が失敗した、または `in_transaction` が真のまま残る場合は、その接続を破棄して `connect()` で再生成する (旧接続は close を試み、失敗は握る)。再生成も失敗したら、**新しい degraded latch は作らず**、次の固定手順に従う。(a) 固定分類 `signal_conn_unavailable` を既存の `health_latch.record_failure` に記録する (status の `health` に出る)。(b) その tick の signal maintenance を skip し、cursor は進めず、部分書込みもしない。(c) 次 tick で再生成を再試行する。再生成と連続失敗 counter は wrapper が毎 tick 担うので、state が degraded / halted の tick でも再試行と回数更新は止まらない (ready でない間は skip する実 maintenance が元々無いので、その tick の失敗は「再生成の失敗」としてだけ数える)。(d) 連続 3 回失敗したら既存の fatal 経路 (`_record_fatal` + `stop_event.set`、systemd が再起動) に乗せる。再生成に成功したら連続回数を 0 に戻す (latch の記録は既存 latch の規則どおり残る)。OPEN ゲートは既存の `signal_source_active` と、consume 前の 2 述語 (snapshot の鮮度 + claim 元 signal の `bar_ts` 鮮度、§5.2 (i)) で足りる: maintenance が走らなければ新しい signal は生まれず、障害前に claim 済みの signal は consume 前に鮮度が再検査されるので、再生成失敗中に鮮度切れとなった signal から OPEN は成立しない。**新旧接続の交換規則**: 再生成は wrapper の中だけで行い、`App.conn_signal` と実 maintenance が参照する接続は常に同じ 1 本とする (wrapper と実 maintenance は毎回 `App.conn_signal` を読み、閉包に固定しない。交換後の呼出しは新接続を使い、旧接続を再利用しない)。`signals.add` 等は DML 後に明示 commit するため、BUSY / LOCKED を出した接続を rollback せずに次 tick で再利用すると、未完 transaction や遅延 commit を持ち越す。これを防ぐ規則 |
| shutdown | `App.close()` の所有資源に `("conn_signal", self.conn_signal.close)` を `conn_core` の**前**に足す。遅延 callable は scheduler thread 上で動くので、`_busy_resources_after_join` は scheduler が残存するとき `conn_signal` も busy に足し (`conn_core` と同条件)、残存中は閉じない |
| 起動時 | 起動時 1 回の `reclaim_expired` (`service.py`) は scheduler thread 開始前の単一 thread なので `conn_core` のまま |
| 観測 | AC-21 (接続が別であること、maintenance 中の Mission transaction と共有しないこと、`conn_core` を渡す変異が guarded-connection test で失敗すること、busy policy の 2 case、再生成失敗の連続回数 1〜2 / 3) |

### 5.2 commit-core: OPEN の source lock と決定論的ゲート

LLM には委ねない。対象は **`Action.OPEN` で claim 済み signal があるときだけ**（`Action` は 4 値。CLOSE / CANCEL / HOLD は lock も検査も無し。SL 変更 action は無く、SL/TP・drawdown kill switch は scheduler / executor の別経路で、lock を取らず不変)。

手順（`loops/trade_loop.py` の commit-pre から commit-core 周辺）:

1. パース → **commit-pre (`gather_open_snapshot`) を従来どおり lock なしで完了させる**。
2. commit-pre 完了後、`core_lock` に入る前に、claim 元 signal の strategy と、`resolved_by_identity`（daemon 起動時に固定した解決セット） の固定済み解決セットから得た全依存 indicator の name lock を**名前昇順**で deadline 付きで取る。この時点で `core_lock` を持たないので、lock 待ちで SL/TP 監視は止まらない (CLI は `core_lock` を取らないため循環しない、§5.0)。
3. deadline 超過は固定理由 `signal_source_lock_timeout` で fail closed (建てない)。consume せず `stopped_by_data_state` と同じ止め方 (gate 拒否の記録、claimed は finally の既存規則で `requeue`)。
4. `core_lock` 内、`record_and_validate_intent` の後・`signals.consume` の前に、注入 clock から **1 回だけ**取得した同一の `now` で 2 述語を両方評価する。両方 stale でも短絡しない。**signal stale を優先**し、signal が fresh のときだけ snapshot stale の requeue を適用する。
   - (i) **鮮度の 2 述語** (consume の前に両方を評価する):
     - (i-a) **snapshot の鮮度**: `open_from_snapshot` 内の既存判定を共通 helper (`snapshot_age_exceeded`。理由文字列・`reject_category="execution"` は不変) に抜き出し、executor と trade loop の両方がそれを呼ぶ。helper は**純関数** `(snapshot, now, max_age_sec) -> canonical rejection | None` で、境界は `age_sec > max_age_sec` (厳密な `>`、現物どおり)。**上で 1 回だけ取得した同じ注入 `now` を渡し、`datetime.now()` を使わない**。name lock と `core_lock` の待ちで stale になっていたら (第一判定)、**consume せず**に gate 拒否を記録して止める (新しい理由コードは足さない)。claimed は finally の既存規則 (`requeue`、現在の `requeue_count >= max_requeue` なら abandoned） に任せる。第一判定を通ったあと consume 後に `open_from_snapshot` 内の executor 側の判定 (第二判定) が拒否した場合は、**既存どおり signal は consumed のまま**で、requeue しない (第一判定の拒否だけが requeue)。
     - (i-b) **claim 元 signal の `bar_ts` 鮮度**: `claim_oldest` / `pending_exists` と同じ共有述語に、上で 1 回だけ取得した同じ注入 `now` を渡して claim 済みの signal に再適用する。claim 時にしか signal 自身の鮮度は検査されず、障害や lock 待ちで claim 後に鮮度切れとなりうるため (`_claimed_signal_is_stale` は outage が ready でないときの finally にしか効かず、`conn_signal` 障害では到達しない)。stale なら **OPEN を拒否** (consume せず gate 拒否、新しい理由コードは足さず、鮮度切れの既存分類を使う) し、signal は requeue でなく **abandoned (鮮度切れ)** にする。これは **claim eligibility / `expire_stale` と同じ境界で、一度観測した鮮度切れは terminal にする既存方針**であり、再 claim しても stale のままである。
   - (ii) 純関数 `signal_source_active(conn, plugins_root, plugin, content_hash)`: (a) `live_identity` の `content_hash` が claim 元と一致、(b) 当該 `(name, content_hash)` の最新決定が approved、(c) strategy なら依存 indicator も fresh な inventory で `require` 解決できる、(d) 当該 name 群に `blocking/open` の deploy op が無い。
5. 通れば consume → `open_from_snapshot()` を実行し、**戻りまで name lock と `core_lock` を保持**する。放すのは戻った後 (`core_lock` → name lock の順)。executor 内の鮮度判定は残り (多層)、consume と発注の間の微小な窓だけが残余になる。
6. (ii) が落ちたら `set_gate_result(accepted=False, reject_reason="signal_source_inactive", reject_category="signal")`、claimed は `abandoned` (再 claim させない)。reason は固定文言。

snapshot stale 時に **requeue** (abandon でない) とする理由 (signal 鮮度切れは上の (i-b) のとおり abandoned): (a) 原因は lock 競合というインフラ要因で、signal 自身の価値は失われていない。(b) abandon にすると同じ bar の機会を操作の競合で失う。(c) requeue は有界で、規則は「現在の `requeue_count >= max_requeue` なら abandoned」。同じ signal が LLM に投入される総回数は最大 `max_requeue + 1` 回 (既定 `max_requeue=2` なら 3 回、`config.py`)。最古 signal を再 claim するので競合が続けば新しい signal はその間 head-of-line で待つが、最大回数で必ず外れるので待ちは有界。(d) 本当に disable 済みなら、次の claim の前に除外 (§5.3) とゲート (ii) が止める。判定を二重実装しないため helper を共有し、consume 前に置く (現物は consume が `open_from_snapshot` の前にあり、stale 拒否のときに signal だけが consumed になる）。

| 項目 | 内容 |
|---|---|
| 直列化の帰結 | 確認 → CLI の disable 完走 → 発注、は起きない。disable が先なら OPEN が拒否され、OPEN が先なら disable の lock 取得が発注確定まで待つ (CLI は blocking flock で待つ、AC-24) |
| 位置づけ | Risk Gate は資金制約を見るもの。このゲートは「信号の根拠が配備されているか」を見る別の決定論ゲート。Risk Gate は変えない |
| 保持時間 | name lock の保持 = `core_lock` の待ち + `record_and_validate_intent` + gate + `open_from_snapshot` (executor の発注時間) + 戻り。**commit-pre の外部取得 (quote / spec / rate) は含まない**。deadline は lock 取得にだけ掛かり、保持中の発注時間には掛からない。実時間は task 6 で確認する |
| コスト | claim 後に fresh な inventory を引くのは OPEN intent あたり 1 回で、判断足ごとの頻度なら問題にならない |

### 5.3 `get_signals`・`claim_oldest`・`pending_exists` からの除外

共有の粒度を分ける。**全てが共有する最小の断片は「deploy-linked 除外 anti-join」**で、claim 可能性の条件 (status・鮮度) まで `recent()` に共有しない (`recent()` は status を問わず consumed / abandoned も LLM に見せる現物の挙動を変えない、既存契約)。

| 関数 | 条件 |
|---|---|
| `claim_oldest` | `status='pending'` + 鮮度 (既存 `_FRESH_CONDITION`) + anti-join を、`store/signals.py` の 1 つの「claim 可能」断片として共有 |
| `pending_exists` | `claim_oldest` と**同じ断片** (status・鮮度・anti-join)。引数に `now` と `freshness_bars` を足す。signal maintenance の遅延で同 tick の `signal_due_fn` が `expire_stale` より先に走るため、鮮度条件が無いと stale な pending だけで空の signal mission が起動する |
| `recent(exclude_deploy_linked=True)` | anti-join だけを追加。status・鮮度は足さない。鮮度切れで abandoned になった行の見え方は変わらない |

- `tools/signal_tools.py` の `get_signals` は `recent(exclude_deploy_linked=True)` を使い、`plugin_deploy_op_signals` に id がある行 (pending / claimed どちらで記録された行も) を LLM 入力から落とす。
- `claim_oldest` は付随表にある signal を claim しない (anti-join、`signal_id` index を使う)。linked signal は requeue、lease 回収、起動時 `recover_interrupted` のいずれでも `abandoned` で終端し、pending に戻さない。pending に戻すと anti-join により永久に不可視なゾンビ行になるためである。
- **linked な pending だけ、または stale な pending だけが残っているときは signal mission を起動しない** (claim 不能な signal で scheduler が毎 tick mission を起動し続けるのを防ぐ、AC-23)。
- `signals.recent()` / `claim_oldest` / `pending_exists` の呼び出し元 (`service.py` の `signal_due_fn` を含む) と差し替え箇所は task 6 で全数 grep する。

### 5.4 稼働中の安全の整理

| 観点 | 結果 |
|---|---|
| 外す向き (disable / rollback の source / retire / revoke) | 意図の記録の commit 後は producer が INSERT しない (lock 内再検証 + open op)。pending は cascade 済み。claimed は付随表で LLM から隠れ、OPEN は §5.2 の lock + ゲートが止める。linked pending は mission を起動しない (§5.3) |
| 追加向き (rollback 先・新配備) | daemon の `approved` に入らず、再起動まで評価されない (fail closed)。status 表示は request ごとの再読込により先に新しい状態を示しうる |
| trade LLM の indicator 参照 | 次の mission から新 live を見る。producer とは再起動まで食い違う (許容) |
| CLI が待つ時間 | producer の lock 区間は短く、CLI の待ちは通常数十 ms。OPEN 中は発注確定まで待つ。deadline 超過時は CLI がエラーで終わり、変更なし |
| scheduler の保護経路 | name lock を取らない。signal maintenance は `core_lock` の外で、専用接続 `conn_signal` を使う (`conn_core` を共有しない)。OPEN の name lock 待ちは `core_lock` を持たない |

### 5.5 `signal_max_id` による mission 境界

trade mission の submit 時に `core_lock` の下で `COALESCE(MAX(id), 0)` を採取し、その mission の `claim_oldest(max_id=...)` に渡す。`0` は claim 不可、`None` は上限なしの直接呼出し経路を表す。採取後に producer が INSERT した signal は次の mission まで claim されない。既存 id の signal が requeue された場合は、後続 mission の上限に含まれるときだけ再 claim 対象になる。



## 6. 状態機械と reconcile

reconcile (起動時、name ごとに lock 内) は deploy ops を切替ジャーナルより先に処理する。**制限する向きの操作は安全側へ完遂する。** 完遂できない場合は `needs_retry` (`verify_failed` は closed、`syscall_failed` は blocking/open のまま同署名の再実行待ち)。reconcile の lock も CLI と同じく対象名 + 全依存名を名前昇順で取り、`core_lock` は取らない。対象は `blocking/open` (§2.1) で、1 起動につき 1 回だけ試みる。FS の syscall が失敗したら、どの op_kind でも**その場で `needs_retry/syscall_failed` に収束**させる (reconcile の内側で再試行を繰り返さない)。

### 6.1 disable_symlink

| 観測 (live と op) | reconcile の動作 | 結果 |
|---|---|---|
| live == `from_target` の symlink | `is_symlink()` を再確認して unlink | `done` |
| live == `from_target` の symlink だが `unlink` が失敗 (EACCES 等) | live に触れない。activity ERROR | `needs_retry` (`syscall_failed`、blocking/open のまま) |
| live 不在 | — | `done` |
| 上記以外 (別 symlink・plain・別 target) | live に触らない。activity ERROR | `foreign` |

### 6.2 rollback_symlink

| 観測 | reconcile の動作 | 結果 |
|---|---|---|
| live == `to_target` | — | `done` |
| live == `from_target` または live 不在 (from_kind=absent) | 対象 strategy と全依存名を**再列挙し昇順 lock** した上で、戻り先を §3.2 の検証 1〜5 で**全て再実行**する (5(a) の `resolve_indicator_deps(..., pin_mode="require")` は op 行の `force` に関わらず必ず再実行。`force` が信用されるのは 5(b) の「他 strategy を止める許可」だけ)。通れば temp を作り直して swap | `done` |
| `from_kind=symlink` かつ live 不在 (source は unlink 済み。この reconcile が検証失敗後に unlink して phase 更新前に中断した場合と、第三者が unlink した場合は区別できない) | 区別せず、commit 済み intent の範囲でのみ安全側へ完遂する。上の行と同じく対象 strategy と全依存名を再列挙・昇順 lock し、検証 1〜5 を全て再実行する。通れば temp を作り直して swap、落ちれば live 不在のまま | `done` / `needs_retry` (`verify_failed`) |
| 上記で、再列挙の結果**新たに解決不能になった dependent** があった | FS を動かす前の追加制限 transaction で、その dependent の pending cascade と pending / claimed の付随表記録を補完してから続行する (op の `force=0` なら続行せず下の行へ) | 続行 or `needs_retry` |
| 検証が落ちた | live が `from_target` なら unlink (source を非 active にする)。不在ならそのまま。activity ERROR `plugin_deploy_op_needs_retry` | `needs_retry` |
| 戻し/swap の syscall (temp 作成・`rename`) が失敗 | live は `from` のまま。temp は残してよい。ERROR | `needs_retry` (`syscall_failed`、open のまま) |
| source の `unlink` (検証失敗時の非 active 化) が失敗 | live は `from` のまま (source は active)。ERROR。open が残るので producer・OPEN は止まり続ける | `needs_retry` (`syscall_failed`) |
| 上記以外 | live に触らない。ERROR | `foreign` |

再試行対象の stale temp symlink (`.{name}.link-d{op_id}`) は reconcile が作り直す前に unlink する。foreign で終端した temp は無害で保全対象ではない。

### 6.3 retire_plain

| 観測 | reconcile の動作 | 結果 |
|---|---|---|
| live が plain dir・`retired_path` 不在 | FS 操作直前に discover を再実行し、intent の plain content hash が非 NULL なら一致する場合だけ `os.rename(live, retired_path)`。記録 hash が NULL なら比較せず plugin 名で `os.rename` する | `done` |
| live が plain dir・intent の content hash (非 NULL) と不一致 | rename しない。activity ERROR | `foreign` |
| live 不在・`retired_path` 実在 | — | `done` |
| live 実在かつ `retired_path` 実在 (衝突) | 触らない。ERROR | `foreign` |
| live 不在かつ `retired_path` 不在 (消失) | ERROR | `foreign` |
| live が symlink に変わっていた | 触らない。ERROR | `foreign` |
| `rename` が EXDEV / EACCES 等で失敗 | live・退避先とも動かさない。activity ERROR | `needs_retry` (`syscall_failed`、open のまま。修復後に同署名の CLI 再実行で続きから) |

### 6.4 共通

- 通知対象 phase (`done` / `needs_retry` の両 fail_reason / `foreign`) への遷移の後に reconcile / CLI が activity を試み (`ActivityLog.write` は失敗を握る、既存契約)、`notify_attempted=1` にする。**配送保証は無い**。activity の欠落や重複は状態の正本 (`plugin_deploy_ops`) に影響しない。`needs_retry/syscall_failed` から同じ `syscall_failed` 以外の phase / reason へ遷移すると `notify_attempted=0` に戻り、その遷移ごとに再通知を負う (`intent` への巻き戻しは再実行の取り込み `match_or_reject`、終端 phase への遷移と再通知は終端判定 `settle_op` が担う)。同じ `syscall_failed` が継続する間は初回通知だけである。通知窓の crash は全通知対象 phase に共通で、§7.0 の 1 表で扱う。**通知対象 phase かつ `notify_attempted=0` の行は、startup reconcile と CLI 再実行 (状態としては no-op) の双方が通知を試みる**。
- reconcile 自身は各ステップを idempotent に書く。中断すると、§7 の「直前の状態」に戻るだけで、悪化しない。
- **CLI の再実行の取り込み**: 同 name・同 `op_kind` の `blocking/open` な op が存在し、**immutable intent 署名 (§2.1: 対象 identity、`to_target`、`revoke`、`force`) が再実行の指定と一致**し、live が from / to のいずれかなら、CLI は新規 op を作らず open op を引き継いで完遂する。open rollback の再実行で `--to` を省略した場合は、署名比較前に open 行の `to_target` を補って比較する。異なる `--to` は拒否する。(検査 3 を再実施。`syscall_failed` の場合は `intent` に戻して続きから。このとき `notify_attempted` を 0 に戻し、完遂後にまた通知を試みる)。**署名が不一致なら拒否**し、open op の `op_id` と署名の値を表示して、同じ command を再実行するか daemon を再起動して reconcile するよう案内する。別の `--to` で打ち直すのは、open op が終端化された後の新規 op。別 op_kind の open op があれば同じく拒否。`--revoke` / `--force` を再実行で付け足すことはできない。revoke を足したいなら `revoke --version`。

## 7. crash matrix

対象は **process crash (kill -9 相当)**。電源断の耐久性 (fsync) は対象外 (決定)。symlink swap の耐久性は既存の `_atomic_symlink_swap` (directory fsync 無し) と同等。fault injection は関数境界での例外送出と、syscall の失敗注入 (`rename` / `unlink` / `symlink` を monkeypatch) を実 FS + 実 SQLite で行う。T4b (全 matrix の統合、§13) の 1 か所で全点を持つ。

注: 各表の「revoke 有」は、`rejected` 行の追記が意図の記録 transaction に含まれることを指す (`revoke` 単独操作は 1 行 INSERT の 1 文でアトミック)。cascade の `abandoned` 化と付随表の記録も同 transaction。以下で「DB」は意図の記録の効果全体 (op 行 + revoke + cascade + 付随表) を指し、全体が all-or-nothing。

### 7.0 通知窓 (全通知対象 phase に共通)

通知対象 phase = `done` / `needs_retry` (両 fail_reason) / `foreign`、および op_kind = disable / rollback / retire / revoke の全てに次の表を適用する。各 op の表には通知窓の行を置かず、ここを参照する。通知は exactly-once を保証せず、N2 の重複と N3 の無音を許容し、いずれも状態の正本 (`plugin_deploy_ops`) に影響しない設計である (「二重送信にも無音化にも倒れない」設計ではない)。

| # | 中断点 | DB | 次の startup reconcile | CLI 再実行 (状態としては no-op、rc=0) |
|---|---|---|---|---|
| N1 | 通知対象 phase の commit 後・activity 前 | 通知対象 phase、`notify_attempted=0` | activity を試み `notify_attempted=1` | 同じく activity を試み `notify_attempted=1` |
| N2 | activity 追記後・`notify_attempted=1` commit 前 | 通知対象 phase、`notify_attempted=0` | activity を再度試みる (重複を許容) | 同左 |
| N3 | activity の書込自体が失敗 (`ActivityLog.write` が握る) | 通知対象 phase | `notify_attempted=1` になる。activity は欠落しうるが状態は変わらない | 同左 |
| N4 | `notify_attempted=1` 済み | 通知対象 phase | 何もしない | 何もしない |
| N5 | reconcile / CLI が通知の試行中に中断 | N1 または N2 と同じ | 同左 | 同左 |

revoke 単独 (`done` を作成と同時に commit) も N1〜N5 に従う。


### 7.1 disable_symlink

| # | 中断点 | DB | FS | 稼働中 producer | 起動時 reconcile | CLI 再実行の収束 |
|---|---|---|---|---|---|---|
| D1 | intent INSERT 前 | 不変 | live = from | 通常 | 対象なし | 通常どおり最初から |
| D2 | transaction 中 (commit 前) | 全て rollback | live = from | 通常 | 対象なし | D1 と同じ |
| D3 | commit 後・unlink 前 | intent + (revoke) + cascade + 付随表 | live = from | open op のため INSERT 見送り | unlink → done | 署名一致なら引き継ぎ unlink → done |
| D4 | unlink 後・done commit 前 | intent のまま | live 不在 | 同上 | live 不在 → done | 引き継いで done |
| D5 | done commit 後 | done | live 不在 | INSERT しない (live 検証) | §7.0 の N1〜N5 | §7.0 の N1〜N5 |
| D6 | reconcile が unlink 前に中断 | D3 と同じ | live = from | 同 D3 | 次回起動で D3 の処理 | D3 と同じ |
| D7 | reconcile が unlink 後・done 前に中断 | D4 と同じ | live 不在 | 同 D4 | 次回起動で D4 の処理 | D4 と同じ |
| D8 | 第三者が D3〜D4 の間に live を別 symlink へ | intent | live = 別 | 検証で落ちる | live 不触、ERROR → foreign | 取り込まず拒否 (foreign が閉じた後は通常の検査) |
| D9 | `unlink` が EACCES 等で失敗 (CLI 内、または reconcile 内) | `needs_retry/syscall_failed` (open のまま) | live = from | 見送り (open op) | 起動ごとに 1 回再試行。失敗が続けば `syscall_failed` のまま ERROR | 権限等を直した後、署名一致の CLI 再実行で `intent` に戻して unlink → done。失敗中の CLI は rc≠0 |
| D10 | D3 の後、再実行の指定が署名と不一致 (`--revoke` / `--force` の付け足し) | intent | live = from | 見送り | unlink → done | 拒否、open op の `op_id` と署名を表示。reconcile 後は新規 op として通常検査 |
| D11 | D3〜D4 の間に別 indicator の live が変わる (依存変化) | intent | live = from | 見送り | disable は依存に影響されず unlink → done | 同左 |
| D12 | D9 の後、修復せず daemon を再起動 | `needs_retry/syscall_failed` | live = from | 見送り (producer は INSERT しない。claimed は付随表で隠れる) | unlink を試み、成功すれば done、失敗なら同状態のまま | 同 D9 |

### 7.2 rollback_symlink (revoke の対象は外される版。target の再検証失敗でも revoke は戻さない)

| # | 中断点 | DB | FS | producer | reconcile | CLI 再実行の収束 |
|---|---|---|---|---|---|---|
| R1 | intent INSERT 前 | 不変 | live = from | 通常 | なし | 最初から |
| R2 | transaction 中 | 全て rollback | live = from | 通常 | なし | R1 と同じ |
| R3 | commit 後・temp 作成前 | intent + (revoke) + cascade + 付随表 | live = from (または不在) | 見送り | 再列挙・昇順 lock → 検証 1〜5 を再実行、ok なら temp 作成 + swap → done、落ちたら source unlink → `needs_retry` | 署名一致なら引き継いで同じ検証、ok なら完遂 |
| R4 | temp 作成後・rename 前 | 同 R3 | live = from、temp 残存 | 見送り | 再試行対象の stale temp を作り直す前に unlink してから R3 と同じ | 同左 |
| R5 | rename 後・done commit 前 | intent のまま | live = to | 見送り (open op)。to は `approved` に無く評価されない | live == to → done | 引き継いで done |
| R6 | done commit 後 | done | live = to | 評価されない | §7.0 の N1〜N5 | §7.0 の N1〜N5 |
| R7 | reconcile が R3/R4 内で中断 | R3 と同じ | temp 残存または live = from | 同左 | 次回起動で R3/R4 の処理 | 同左 |
| R8 | reconcile が rename 後・done 前に中断 | R5 と同じ | live = to | — | 次回起動で R5 | 同左 |
| R9 | reconcile が `needs_retry` 化の unlink 後・phase 更新前に中断 (`from_kind=symlink`、live 不在、`intent`) | intent | live 不在 | 見送り | §6.2 の専用行: 検証 1〜5 を再実行し、落ちれば `needs_retry/verify_failed` (live 不在のまま、event を出す)、通れば swap → `done`。`foreign` にはならない | `--to` 省略なら open 行の `to_target` で再検証し、別の `--to` は署名不一致で拒否。終端化後は新規 op として打ち直す |
| R10 | from_kind=absent で R3 | intent (`--revoke` は受け付けない) | live 不在 | 見送り | 検証 ok → link → done / 落ち → `needs_retry` (unlink 不要、read model の event は作らない) | `--to` 省略なら open 行の `to_target` を使う |
| R11 | 戻り先の版 dir が R3〜R4 の間に 1 byte 改変 | intent | live = from | 見送り | 検証 3 が落ち → source unlink → `needs_retry` | `--to` を別の版にして新規 op |
| R12 | 第三者が live を別 symlink へ | intent | live = 別 | — | 触らず ERROR → foreign | 取り込まず拒否 |
| R13 | intent 後に戻り先 strategy の依存 indicator の live が変わる (依存変化) | intent | live = from | 見送り | 5(a) が落ち → source unlink → `needs_retry` (`force` でも許さない) | 新規 op |
| R14 | intent 後に別 dependent が新たに解決不能 (op の `force=1`) | intent | live = from | 見送り | 追加制限 transaction で cascade + 付随表を補完してから swap → done | 引き継ぎ |
| R15 | 再実行の `--to` / `--force` / `--revoke` が署名と不一致 | intent | 不変 | 見送り | 通常の R3 処理 | 拒否、open op の `op_id` と署名を表示 |
| R16 | swap の syscall (temp 作成・`rename`) が EACCES / EXDEV 等で失敗 | `needs_retry/syscall_failed` (open のまま) | live = from、temp 残存 | 見送り | 起動ごとに 1 回再試行。失敗が続けば同状態のまま ERROR | 修復後、署名一致の CLI 再実行で続きから (検証 1〜5 を再実行) |
| R17 | `needs_retry/verify_failed` 遷移後 | needs_retry (verify_failed、closed) | live 不在 | — | §7.0 の N1〜N5 | §7.0 の N1〜N5 |
| R18 | 検証失敗 → source の `unlink` 自体が EACCES 等で失敗 | `needs_retry/syscall_failed` (open のまま。`verify_failed` にはしない: source がまだ live だから) | live = from (active のまま) | 見送り (open op なので producer は INSERT しない、OPEN はゲートで拒否) | 起動ごとに 1 回、検証から再実行して unlink を再試行 | 修復後、署名一致の CLI 再実行 → 検証 → 再び落ちれば unlink → `needs_retry/verify_failed` |
| R19 | 第三者 (または手作業) が R3〜R4 の間に source の symlink を unlink (`from_kind=symlink`、live 不在、`intent`) | intent | live 不在 | 見送り | R9 と同じ専用行。区別せず検証 1〜5 → 通れば swap → `done` / 落ちれば `needs_retry/verify_failed` (commit 済み intent の範囲でのみ完遂) | `--to` 省略なら open 行の `to_target` で、署名一致として同じ検証 |

### 7.3 retire_plain (revoke は付けない。cascade は plugin 名の pending 全て)

| # | 中断点 | DB | FS | producer | reconcile | CLI 再実行の収束 |
|---|---|---|---|---|---|---|
| P1 | intent INSERT 前 | 不変 | live = plain | 通常 | なし | 最初から |
| P2 | transaction 中 | 全て rollback | live = plain | 通常 | なし | P1 と同じ |
| P3 | commit 後・rename 前 | intent + cascade + 付随表 | live = plain、`retired_path` 不在 | open op で見送り | rename → done | 署名一致なら引き継いで rename → done |
| P4 | rename 後・done commit 前 | intent のまま | live 不在、`retired_path` 実在 | 見送り (live 不在) | done | 引き継いで done |
| P5 | done commit 後 | done | 同 P4 | — | §7.0 の N1〜N5 | §7.0 の N1〜N5 |
| P6 | reconcile が rename 前に中断 | P3 | P3 | — | 次回 P3 | 同左 |
| P7 | reconcile が rename 後・done 前に中断 | P4 | P4 | — | 次回 P4 | 同左 |
| P8 | 退避先が既に実在 (意図の記録の前) | 不変 | 変更なし | 通常 | なし | 拒否 (op_id を含む名前なので通常起きない。起きるなら第三者のファイル) |
| P9 | P3 の間に退避先が第三者に作られた | intent | live 実在 + `retired_path` 実在 | — | 触らず ERROR → foreign | 取り込まず拒否。dry-run も同じ refusal |
| P10 | `st_dev` 検査の後に `rename` が EXDEV / EACCES で失敗 (plain rename 失敗) | `needs_retry/syscall_failed` (open のまま。cascade 済み) | live = plain | open op で見送り | 起動ごとに 1 回再試行。失敗が続けば同状態のまま ERROR | 修復後、署名一致の CLI 再実行で続きから。`st_dev` 検査で通常の EXDEV は意図の記録前に拒否される |
| P11 | 意図の記録後に plain の依存 indicator が変わる (依存変化) | intent | live = plain | 見送り | retire は依存に影響されず rename → done | 同左 |
| P12 | P10 の後、退避先が第三者に作られた状態で再試行 | `needs_retry/syscall_failed` | live 実在 + `retired_path` 実在 | — | 触らず ERROR → foreign | 取り込まず拒否。dry-run も同じ refusal |

### 7.4 revoke 単独

1 transaction (`rejected` 行の追記 + `op_kind=revoke, phase=done` の op 行 + 対象 identity の pending の abandon + 付随表の記録) が all-or-nothing。crash は「全て無し」か「全て有り」のどちらかで、再実行は冪等 (最新決定が既に rejected なら rc=0、op 行は増えない)。FS は触らない。通知窓は §7.0。activity の欠落は許容し、状態は `approval_requests` の `rejected` 行と revoke 行が正。

### 7.5 producer 側

| # | 中断点 | 結果 |
|---|---|---|
| S1 | 評価後・lock 取得前 | 何も INSERT されない。次 tick で再評価 (cursor 未進行) |
| S2 | lock 内・INSERT 前 | lock は process 死で解放。INSERT なし |
| S3 | INSERT の commit 後 | 通常の signal。続く disable の cascade が pending を拾う |
| S4 | lock 取得待ち中に CLI が完了 | 再検証が落ち INSERT されない |
| S5 | lock 取得 deadline 超過 | 専用結果で INSERT 見送り、cursor 不進行、次 tick で再評価 |
| S6 | 遅延 callable の実行中に process が死ぬ | `core_lock` は既に解放済み。INSERT の commit 前なら (`conn_signal` の未 commit transaction は process 死で破棄され) 何も無く、cursor 未進行で次 tick が再評価する。commit 済みなら通常の signal |
| S7 | 遅延 callable の実行中に Mission thread が `core_lock` を取り `conn_core` で transaction を実行する | 接続は別 (`conn_signal` と `conn_core`) で、互いの transaction を commit / rollback しない。書込み競合は `busy_timeout` 内で待つか、超えれば callable の fail-open (cursor 不進行、次 tick で再評価) |

### 7.6 commit-core 側 (OPEN)

| # | 中断点 | 結果 |
|---|---|---|
| G1 | name lock 取得待ち中に CLI が intent を commit して完了 | lock 取得後に `signal_source_active` が落ちる → `signal_source_inactive` 拒否、claimed は abandoned |
| G2 | ゲート通過後・`open_from_snapshot` 中に CLI が disable を試みる | CLI の lock 取得は発注確定まで待つ。発注は通り、その後 disable が完了する |
| G3 | lock deadline 超過 | `signal_source_lock_timeout` で建てない。consume しない |
| G4a | daemon が OPEN 中、**consume 前** (signal は claimed) に crash、対応する running mission がある | lock は process 死で解放。intent は記録済みで既存の reconcile 規則に従う。起動時の `missions.recover_interrupted()` (`service.py`, `missions.py`) は running mission とその claimed を同一 transaction で処理する。linked signal は上限や `requeue_count` にかかわらず `abandoned` で終端する。付随表に結ばれた signal は `claim_oldest` が取らず `pending_exists` も真にならない (signal mission を起動しない、AC-23 case a) |
| G4b | daemon が OPEN 中、**consume 後** (`open_from_snapshot` の実行中、signal は consumed) に crash | signal は既に consumed で `recover_interrupted` の対象外 (戻すのは `status='claimed'` だけ)。pending には戻らず consumed のまま維持する。以後は既存の intent / order 回復だけに委ね、本設計は変更しない |
| G5 | 対応する mission が無い claimed (mission 回収の対象外) | 起動時 1 回の `reclaim_expired` (`service.py`) または maintenance の `reclaim_expired` は lease 切れの linked signal を `abandoned` で終端する。mission を起動しない (AC-23 case b) |
| G6a | claim 後、障害または lock 待ちで claim 元 signal の `bar_ts` 鮮度が切れる (`conn_signal` 再生成失敗中を含む) | `core_lock` 内、consume の前に共有述語 (注入 clock) が検出し、OPEN を gate 拒否 (consume せず、建てない)。signal は abandoned (鮮度切れ)、requeue しない (時間が戻らないため) |
| G6 | name lock / `core_lock` の待ちで commit-pre の snapshot が期限切れ | `core_lock` 内、consume の前に共通 helper が検出し、consume せず gate 拒否 (executor と同じ理由・category)。claimed は finally の `requeue` で pending に戻る (現物 `missions.py` 145 に合わせ、上限未満なら pending、`requeue_count >= max_requeue` に到達済みなら abandoned)。建てない。helper の拒否が consume 後の第二判定で起きた場合は consumed のまま (requeue しない) |
| G7 | OPEN が name lock を取ったまま `core_lock` を待っている間に scheduler tick が走る | tick は name lock を取らないので止まらない (SL/TP・kill switch 継続)。tick が終われば OPEN が `core_lock` を得る |

## 8. 状態遷移と不変条件

`plugin_deploy_ops.phase`: `intent` → `done` / `needs_retry` / `foreign`。op が closed になるのは `done` / `foreign` / `needs_retry/verify_failed` の 3 通りだけ。`needs_retry/syscall_failed` は blocking/open のまま同署名の再実行を待ち、再実行で `intent` に戻る (`notify_attempted` も 0 に戻す)。述語は §2.1 に 1 組だけ置く。revoke 単独だけは `intent` を経ず `done` で作る。

| 遷移 | 契機 | 条件 | 副作用 |
|---|---|---|---|
| (なし) → `intent` | 人間操作 (disable / rollback / retire) | 対象名 + 依存名の lock 内、切替ジャーナル (交差 name)・deploy ops とも open なし | 制限する向きの DB 効果 (revoke 行・cascade・付随表) を同 tx で確定 |
| (なし) → `done` | 人間操作 (revoke 単独) | name lock 内、open なし、現 live の版でない | rejected 行・pending の abandon・付随表を同 tx で確定 |
| `intent` → `done` | 操作自身 / CLI の引き継ぎ / reconcile | live が to (disable は不在、retire は退避先) | activity (通知試行) |
| `intent` → `needs_retry` (`verify_failed`) | reconcile (rollback の再検証失敗) | source を unlink できた後 | activity ERROR `plugin_deploy_op_needs_retry`、read model event (実効 to=absent) |
| `intent` → `needs_retry` (`syscall_failed`) | CLI / reconcile (FS の syscall 失敗) | live を動かせなかった | activity ERROR。blocking/open のまま |
| `needs_retry` (`syscall_failed`) → `intent` → closed | 署名一致の CLI 再実行 / reconcile | 修復後 | 通常の完遂 |
| `intent` → `foreign` | reconcile | live が from / to のどちらでもない | activity ERROR。live 不触 |

既存 IV-1〜IV-7 は変えない。追加:

| ID | 不変条件 |
|---|---|
| IV-8 | rollback の戻り先は、live を動かす瞬間に (a) 最新決定が approved、(b) 版 dir の中身が承認時の `(content_hash, artifact_hash)` と一致、(c) loader が受理する形、(d) 戻り先自身の依存が `require` で解決できる、の 4 つを満たす。reconcile でも同じ検証を再実行する |
| IV-9 | `blocking/open` (§2.1 の共通述語) は name ごとに高々 1 本 (部分 UNIQUE)。切替ジャーナルの非終端と同時には存在しない (name flock 内で両表を見る) |
| IV-10 | 制限する向きの DB 効果 (revoke・cascade・付随表) は live の FS 効果より先に commit する。live は「その時点で管理対象でない版」へ動かない |
| IV-11 | 承認行は UPDATE されない。変わるのは `rejected` 行の追記のみ。`gc_roots` は変えない (承認済み版は残る) |
| IV-12 | 呼び出し経路は人間 CLI のみ。改善 agent・操作 API・bot からは到達しない (§9) |
| IV-13 | signal の INSERT は、signal maintenance が `core_lock` の外で実行され (遅延 callable。DB は専用接続 `conn_signal` のみ)、strategy + 全依存 indicator の name lock を名前昇順で保持し、全 symlink / pin と `blocking/open` の deploy op を再検証した直後に同じ lock 内で行う |
| IV-14 | `Action.OPEN` で claim 済み signal を伴う intent は、commit-pre 完了後に strategy + 全依存 indicator の name lock を昇順で取り、生存確認から `open_from_snapshot` の戻りまで保持する。claim 元 `(plugin, content_hash)` と依存版が active でなければ決定論的ゲートで拒否される。consume の前に、同じ 1 回の注入 `now` で 2 述語を両方評価する: claim 元 signal の `bar_ts` 鮮度は `claim_oldest` / `pending_exists` と共有の述語で、stale なら consume せず signal を abandoned にする。これは **claim eligibility / `expire_stale` と同じ境界で、一度観測した鮮度切れは terminal にする既存方針**であり requeue しない。signal が fresh のときだけ、snapshot の鮮度を共通の純関数 helper (注入 clock、境界 `>`) で評価し、stale なら consume せず claimed を requeue する。両方 stale では signal stale を優先して abandoned にする。consume 後の executor 側の第二判定の拒否は consumed のまま。CLOSE / CANCEL / HOLD と scheduler の SL/TP・drawdown kill switch は lock も拒否も受けない |
| IV-15 | 制限する向きの deploy op は、commit 後に crash しても reconcile または CLI 再実行で、closed (`done` / `foreign` / `needs_retry/verify_failed`) か、再実行待ちの blocking/open (`needs_retry/syscall_failed`) のいずれかへ収束する。FS の syscall が失敗し続けても `needs_retry/syscall_failed` で止まり、blocking/open のままなので producer・OPEN は止まり続け、live が「外すと決めた版」のまま `abandoned` で閉じることは無い。修復後の同署名の再実行で続きから完遂する |
| IV-16 | approve / bless / retry は candidate の全依存名について open deploy op を検査し、deploy op は対象と全 dependent 名について open switch journal を検査する (交差 name) |
| IV-17 | 配備操作の状態の正本は `plugin_deploy_ops` (revoke 単独の監査は `approval_requests` の `rejected` 行が正本で、op 行は付随表の結び先と通知窓)。activity は通知試行で、欠落・重複しても状態は変わらない |
| IV-18 | lock 順序は §5.0 の表で固定: `core_lock` 保持中は name lock を取らない。OPEN だけが name lock → `core_lock`。CLI・reconcile は name lock のみ。name lock は名前昇順 |
| IV-19 | 単独 revoke を含む全ての制限 transaction で、対象 identity の pending / claimed は付随表に結ばれ、`claim_oldest` / `get_signals` / `pending_exists` から除外される。除外 anti-join は 1 つの SQL 断片を共有し、`claim_oldest` / `pending_exists` は status・鮮度もその断片で共有する (§5.3) |
| IV-20 | signal maintenance は専用書込み接続 `conn_signal` だけを使う。`conn_core` は `core_lock` 保持中だけ使う既存契約を保ち、遅延 callable に渡さない。`conn_signal` は `App` の所有資源で、`App.close()` は `conn_core` より前に閉じ、scheduler が残存するときは busy として閉じない。rollback 不能な `conn_signal` は破棄・再生成し (毎 tick 積まれる接続修復 wrapper の中だけ、wrapper と実 maintenance は毎回 `App.conn_signal` を読む。実 maintenance だけが `ready` でゲートされる)、再生成失敗は `signal_conn_unavailable` を `health_latch` に記録して tick を skip (cursor 不進行・部分書込みなし)、連続 3 回で既存の fatal 経路 (`_record_fatal` + `stop_event.set`) に乗せる |
| IV-21 | `blocking/open` / `closed` の述語は store 層の 1 組の定義 (`OPEN_PHASE_SQL` / `is_open`) を全消費者が呼ぶ。`syscall_failed` を終端とみなす分岐を持たない |

live を書き換える全経路の表 (既存 spec §4 末尾) に、`rollback` の `_atomic_symlink_swap` と `disable` の `unlink` の 2 行を追加し、既存の retire 行を `retire_plain` の `rename` を含む形に置き換える。すべて name lock の内側。lock 順序規約 (名前昇順、§5.0) は producer の INSERT lock (IV-13) と commit-core の source lock (IV-14) にも適用される。

## 9. 遮断と安全

- 呼び出し経路は人間 CLI のみ (IV-12)。実装は `plugin/switch.py` の関数 + `backtest/cli.py` の分岐に限る。
- **improve registry の pin**: `tools/mission_registry.py` の improve tool の完全な集合 (13 本: `web_search`, `fetch_article`, `list_staging`, `read_staging_file`, `write_staging_file`, `read_plugin_source`, `list_examples`, `read_example_plugin`, `run_plugin_tests`, `list_deployed_plugins`, `lock_staging_deps`, `run_backtest`, `analyze_corr`) を frozenset で test に固定する (部分文字列検査は廃止)。`improve_staging_tools.py` の `_ALLOWED_REL = {plugin.py, config.yaml, test_plugin.py}` (3 本) の完全一致も pin。
- **API の pin**: `ops/api_server.py` の `ENDPOINTS` (24 本、`ops/service.py` の `build_routes` から作る `ROUTES`) の `(method, path, handler, scope)` 完全集合と、`jobs` / `events` の action enum を frozenset で固定する。deploy ops を指す route / action が無いこと。
- **bot の pin**: bot に command 表は無い。`afx-ops/afx_ops/gate/types.py` の component custom-id の action 集合 `{approve, claim, confirm, cancel, reject, retry}` と、`gate/state.py` の決定 action 集合 `{approve, reject, retry}` を完全集合で pin する。`BotClient` は **`afx-ops/afx_ops/api_client.py` のクラス本体に直接定義された公開メソッド集合 `{events, approvals, approval, approve, reject, retry, poll, cancel}` (8 本。継承した `from_token`・`interrupt` 等は対象外)** を完全集合で pin し、対応する API path 集合 (events、approvals の一覧 / 詳細、approve / reject / retry、job の取得 / cancel の 8 系統) も固定する。disable / rollback / retire / revoke を含まない。
- **表名の pin**: `store/db.py` の `TABLE_NAMES` は現物 30 表で、本設計で `plugin_deploy_ops` と `plugin_deploy_op_signals` が加わり 32 表になる。表名集合の完全一致 test を更新する。`Action` は `{OPEN, CLOSE, CANCEL, HOLD}` の 4 値のまま (本設計は action を足さない)。
- **実隔離 worker での拒否 integration test**: Landlock + seccomp 下の改善 worker を実起動し (既存 `core/improve_canary.py` の実 worker → 隔離 CLI 経路を使う)、(a) `afx plugin disable` の subprocess 起動、(b) DB への書込、(c) `plugins/<name>` の unlink / rename / `plugins/.versions` の書換え、がすべて拒否される。fingerprint 非依存の形 (実行ファイルの照合に依存しない起動経路) で書く。CI は `isolation` job を追加し、先頭で kernel の Landlock ABI を測る。ABI 8 以上なら実隔離 integration を実行し、skip があれば fail とする。ABI 8 未満なら「kernel 不足」を明示して job を neutral にする。release の受入は ABI 8 の実機で手順書どおり手で実行する。
- 新しい文字列を `approval_requests.reason` / `improvement_backlog.last_result` に自由記述で書かない (固定文言のみ)。activity も固定書式 (自由記述なし)。
- 外向きリクエスト・新しい sink・新しい入力経路は増えない。
- fail closed: 検査が 1 つでも落ちたら FS も DB も変えない。意図の記録後の crash は §6 が収束させる。
- 残余: 同 uid の shell を持つ攻撃者は CLI を打てる (bless / retire と同じ前提で、人間の shell が信頼境界)。API に出さないので、漏れた approver 鍵で disable される妨害は起きない。

## 10. 鍵漏れ・誤承認の回復手順 (運用文書に書く 1 節)

`docs/operations/ops-api-2026-10-09.md` §7 を置き換える。同 §6 (鍵を止める手順) の続きとして、鍵を止めた後に plugin を回収する。

1. 前提: §6 の手順 1〜2 まで済ませる (daemon 停止、鍵の rotate / revoke)。停止中は資金保護を kill switch の latch に任せたまま、plugin だけを直す。
2. `afx plugin versions <name>` で、現在の版・各版の承認時刻・承認者 (`decided_by`)・revoke の有無・経歴・open / `needs_retry` / `foreign` の op を見る。疑わしい承認の時刻帯は鍵の漏洩時刻から割り出す。
3. 誤承認・漏洩鍵で承認された版がある name ごとに:
   - 前の版に戻せる: `afx plugin rollback <name> --to <hash> --revoke` (経歴に rollback / disable を挟むと `--to` 必須)。戻り先は `--dry-run` で確認する。
   - 前の版が無い、または疑わしい: `afx plugin disable <name> --revoke`。
   - 既に revoke なしで外してある: `afx plugin revoke <name> --version <content-hash-先頭一致>`（小文字 hex 12 桁以上）。
   - indicator なら依存 strategy が列挙される。意図して外すなら `--force` (依存 strategy の signal は止まり、pending は放棄される)。そうでなければ strategy 側を先に処置する。
   - legacy plain 配備は `afx plugin retire <name>` (disable / rollback の対象外)。
3b. open の op が残っているとき (`versions` / `afx ctl status` が示す): `needs_retry/syscall_failed` は権限・mount・ディスクの原因を直してから**同じ署名のコマンドを再実行**する (`--revoke` / `--force` / `--to` を変えない)。`needs_retry/verify_failed` は live が不在になっているので、原因を直した上で新規の `rollback --to` か `disable` を打つ。
3c. `foreign` (第三者が live を触った。op は閉じ、制限の DB 効果は残る) の回復は専用コマンドを使わず、`afx plugin versions <name>` で live の種類を確認して分ける:

| foreign の live | 手順 |
|---|---|
| 別 symlink、または不在 | 版 dir の整合 (`versions` の整合欄) と承認を確認し、検証済みの版を `afx plugin bless` / 承認経路 (`approve`) で入れ直す。承認されていない版は先に承認する |
| plain dir (現物の approve / bless は `legacy_plain_present` で保留に留め上書きしない) | 外部データを保全し、中身を確認したうえで必ず `afx plugin retire <name>` を経てから、bless / approve する |
| 不明・改変された版 (symlink の先が承認と合わない、版 dir の hash 不一致、出所不明の plain) | daemon を停止し、`plugins/` と `.versions/` を保全してから処置する。稼働中に触らない |
4. rollback 後の戻り先は daemon を再起動するまで反映されない。完了表示に `runtime_activation=pending_restart` が出たら、daemon の起動で有効になる。
5. 版 dir は消えない。調査したいときは `plugins/.versions/<name>/<hash>/` を読み取り専用のまま保全する (コピー先は `data/` 外)。**容量の観測** (`du` で `.versions` 総量を月次に確認)、**保全** (revoke した版は削除せず、必要なら tar で `data/` 外へ退避)、**手動削除するとき** (削除前に `afx plugin versions <name>` で当該版が live でも rollback 候補でもないことを確認し、削除後に再度 `versions` で整合表示が崩れていないことを確認する) を手順に書く。削除は人間が手で行う。
6. daemon を起動し、`afx ctl --as operator whoami`、`afx plugin versions <name>` の live と op の状態が意図どおりであることを確かめる。`afx ctl status` の `plugin_deploy_ops` 節は `open` / `attention`、読めないときは `unavailable` を示す。`afx ops status <feature>` は未完 op が 1 件以上のときだけ 1 行を示す。activity (`plugin_disabled` / `plugin_rolled_back`) は通知試行なので、欠けていても状態が正であれば問題としない。
7. 稼働中に誤承認に気付いたとき: 停止せず `afx plugin disable <name>` を打てる。次の signal INSERT から止まる (pending は放棄され、claimed は LLM から隠れ、OPEN は発注確定まで待ってゲートで止まる)。ただし新しい配備や rollback 先の有効化は再起動が要る。鍵漏れのときは必ず §6 の停止手順を先に行う。

## 11. 受入条件

AC は表の 1 行を 1 本と数え、枝番を持たない通し番号 AC-1〜AC-36 の 36 本。クラッシュ注入の AC-15 (D / R / P / N) と AC-16 (S / G) は別の行。

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | symlink 配備の disable 後、`plugins/<name>` が存在せず `.versions/<name>/<hash>` は残り、承認行は 1 行も UPDATE されていない (`--revoke` なしのとき追記も無い) | FS と `approval_requests` の before/after |
| AC-2 | disable が `plugin_deploy_ops` に `intent` → `done`、`notify_attempted=1`。activity `plugin_disabled` は **1 行以上** (重複許容、`notify_attempted=1` を伴う)。revoke 単独も同じ (`op_kind=revoke` の `done` 行、`notify_attempted=1`) | DB / log / status 出力 |
| AC-3 | **activity の書込失敗を注入**しても op は `done`、`notify_attempted=1`、`afx ctl status` の `plugin_deploy_ops` 節と `afx ops status <feature>` が `plugin_deploy_ops` から状態を返す。通知窓は §7.0 の N1〜N5 を disable / rollback / retire / revoke / needs_retry / foreign の全通知対象 phase で踏む | 書込失敗の注入、状態出力 |
| AC-4 | disable を 2 度打っても 2 度目は rc=0 で何も変わらない。`--revoke` 付きの 2 度目は rc=2 で `revoke --version` を案内 | rc と行数 |
| AC-5 | **直接 plugin**: 依存 strategy のない signal plugin の disable 後、pending は abandoned、`get_signals` に出ず、producer は INSERT しない | DB / tool 出力 |
| AC-6 | **依存 strategy**: indicator A の disable は `--force` 無しで拒否され、strategy 名・alias・理由が出る。FS・DB 不変。`--force` 付きなら A の disable と S の pending の cascade abandoned が同一 tx で commit され、S の producer は INSERT を止める (S の symlink は残る) | rc, stderr, DB, producer |
| AC-7 | rollback 既定: 最新イベントが approve / bless のとき `from` へ戻る。A→B 承認、B→A rollback の後の `rollback` は `--to` 無しで拒否される (rc=1)。disable 後も同じ。`needs_retry` 後は実効 `to=absent` のイベントとして扱われ `--to` 必須 | 3 世代 + 挟み履歴 fixture |
| AC-8 | 初回配備のみの name の rollback は「戻り先が無い」で拒否 (rc=1、変更なし) | rc |
| AC-9 | 戻り先の版 dir を 1 byte 書き換えると rollback が拒否される (IV-8 (b))。revoke 済み・未承認の hash も拒否 | 各 1 case |
| AC-10 | rollback 完了表示に `filesystem_switched=yes runtime_activation=pending_restart` が含まれ、「完了」単独の表示でない。rollback 後、daemon の producer は戻り先を再起動まで評価しない | stdout, producer |
| AC-11 | `--revoke` で `rejected` 行が 1 行追記され (`created_at`・`decided_at` が既存の最新決定より後)、その後の `approved_plugins` が当該 `(name, content_hash)` を除外する。再度同内容を `bless` すると復活する | loader の戻り値 |
| AC-12 | `revoke --version` 独立操作: 先に revoke なしで disable した版に後から revoke でき、冪等 (2 度目 rc=0、op 行は増えない)。現 live の版には拒否。**1 transaction で `rejected` 行・`op_kind=revoke` の `done` 行・対象 identity の pending の abandon・pending / claimed の付随表記録が揃い、以後その signal は `claim_oldest` / `get_signals` / `pending_exists` に出ない** (旧版から approve / bless で切り替えた後に旧版を revoke するケースで観測する)。途中失敗で一部だけ残らない | DB, rc, tool 出力 |
| AC-13 | cascade: subject と、`--force` で解決不能になる依存 strategy の pending だけが abandoned。claimed の status と他 plugin の signal は不変。`plugin_deploy_op_signals` に pending と claimed の id が `status_at_link` つきで残り、`signal_id` index が存在する | before/after |
| AC-14 | **決定的な固定 fixture** で、切替ジャーナルが非終端の name (対象 + 全 dependent 名) に deploy ops は拒否、deploy ops が open の name に approve / bless / retire / retry_approval は拒否。**交差 name ケース**: indicator A の disable intent が open のとき、A に依存する strategy S の approve / bless / retry も拒否。逆に S の open switch journal がある間の A の disable も拒否。open の disable intent に対する同署名の `retire` は拒否ではなく引き継ぎ (別 op_kind・署名不一致は拒否) | 双方向 + 交差 + 署名 |
| AC-15 | 配備操作の crash 注入 (D / R / P / N): §7.0 N1〜N5、§7.1 D1〜D12、§7.2 R1〜R19、§7.3 P1〜P12、§7.4 の各点で、reconcile 後と CLI 再実行後に表どおりの最終状態へ収束する (revoke 有無で DB 効果の全体が all-or-nothing)。**永続 syscall 失敗** (D9 / R16 / R18 / P10、`unlink` / `rename` / `symlink` の失敗注入) は `needs_retry/syscall_failed` (blocking/open) に収束し、producer が INSERT せず OPEN がゲートで拒否する。修復後の同署名の CLI 再実行 (署名不一致は拒否) で続きから完遂する。**R9 / R19** (`from_kind=symlink` + live 不在 + `intent`) は再検証の成否で `done` / `needs_retry/verify_failed` に収束し、`foreign` にならない。IV-15 の収束先と一致する | 全点を実 FS + 実 SQLite で |
| AC-16 | producer / commit-core の crash 注入 (S / G): §7.5 S1〜S7、§7.6 G1〜G7 の各点で表どおりの結果になる。G4a (`recover_interrupted`) と G5 (対応 mission が無い claimed の lease 回収) では linked signal が `abandoned` で終端し、`requeue_count` を増やさず、再 claim も mission 起動もしない。G4b (consume 後の crash) は consumed を維持し、pending に戻らず既存の intent / order 回復だけに委ねる。G6 は stale な snapshot が consume されず、claimed が requeue されることを観測する。**G6a は stale な claim 元 signal が consume されず abandoned になることを観測し、snapshot と signal が同時 stale の case では signal stale を優先して abandoned になることを観測する。**G4a と G4b は両境界を別 case で観測する | 実 SQLite + barrier |
| AC-17 | plain retire: 従来どおり `_retired/` へ退避し、依存確認 (config.yaml の名前判定、hash 照合なし)・記録・plugin 名の pending 放棄が付く。退避先は op_id を含み衝突しない。EXDEV は意図の記録前に拒否。crash 点は AC-15 の P 系 | FS / DB |
| AC-18 | symlink の retire は disable_symlink と同じ結果 (`op_kind=disable_symlink`) | DB |
| AC-19 | producer interleaving (barrier 付き multi-process test): 評価完了の直後・lock 取得の直前に disable を完走させると、INSERT されない。disable の意図の記録の直前に INSERT を完了させると、cascade が拾う。どちらの順でも disable 後に pending が残らない | multi-process test (既存 `tests/plugin/_flock_worker.py` 型) |
| AC-20 | 依存 indicator の interleaving: S の評価中に A を `--force` で disable すると、S の INSERT は再検証で落ちる。legacy plain の indicator に依存する正当な strategy は、indicator が live の間は INSERT が通る (hash 非照合) | multi-process test |
| AC-21 | signal maintenance は `core_lock` を保持しない (遅延 callable)。producer の lock 区間は評価を含まない。**barrier と注入した monotonic clock** で、(a) 評価中に別 process が name lock を取れる、(b) lock 取得 deadline 超過時は INSERT 見送り・cursor 不進行・次 tick で再評価、(c) activity が状態変化ごとに 1 回、を直接検査する (実時間待ちに依存しない)。signal maintenance の実行中に別 thread が `core_lock` を取れる。**専用接続**: 遅延 callable が使う接続は `App.conn_signal` で、`conn_core` と別の Connection である (同一性の比較)。maintenance の実行中に Mission thread が `core_lock` を取って `conn_core` で transaction を実行しても接続共有がなく、互いの transaction を commit / rollback しない。**busy policy は 2 case に分ける**: (1) `busy_timeout` 内の競合は両接続が独立に commit する、(2) `conn_core` が `BEGIN IMMEDIATE` を timeout 超過まで保持した場合、signal 側は固定分類で失敗し、rollback 済み (`in_transaction == False`)・cursor 不進行・部分書込みなし・次 tick で回復する。rollback 不能な接続の破棄・再生成を注入で観測する。**再生成失敗**: 1〜2 回目は maintenance の skip・`signal_conn_unavailable` の latch 記録 (status の `health` に出る)・cursor 不進行・部分書込みなしで、その間も SL/TP と drawdown kill switch は継続し、OPEN は既存ゲート (`signal_source_active` と鮮度) のまま、次 tick で再生成を再試行する。成功で連続回数が 0 に戻り latch の記録は残る。**tick 契約**: 接続修復 wrapper は state が ready / degraded / halted のいずれでも毎 tick 積まれて再生成と連続失敗 counter が進み、実 maintenance だけが ready のときに限り走る (degraded の tick で再生成が試され、実 maintenance は呼ばれないことを注入 state で観測する)。**接続の交換**: 交換後の呼出しは新しい `App.conn_signal` を使い、旧接続を再利用しない (旧接続の使用を検出する guard で pin)。**claimed signal の鮮度**: 障害前に claim 済み → 再生成失敗中に claim 元 signal の `bar_ts` 鮮度が切れる → OPEN 拒否 + signal は abandoned (requeue されない、注入 clock で試験)。3 回目の失敗で `_record_fatal` + `stop_event.set` が呼ばれ、復旧は再起動である。**guarded-connection test**: `conn_core` を「`core_lock` を保持した thread だけが使える」guard (tracked RLock の owner で判定) で包み、`conn_signal` も別の guard で包む。`conn_core` を callable に渡す変異は guard 違反で必ず失敗する。`App.close()` が `conn_signal` を `conn_core` より前に閉じ、scheduler 残存時は busy として閉じない | barrier + 注入 clock + guarded connection |
| AC-22 | commit-core: (a) claim 済み signal の元 plugin が disable / revoke / rollback で無効になった後の OPEN intent は `signal_source_inactive` で拒否され、claimed は abandoned。(b) **barrier 付き interleaving test**: ゲート通過直後に CLI の disable を競合させると、disable は `open_from_snapshot` の戻りまで待つ。disable が先に完了していれば OPEN は拒否される。(c) lock deadline 超過は `signal_source_lock_timeout` で建てず consume もしない。**signal 鮮度 (consume 前の 2 述語の 2 つ目)**: claim 元 signal の `bar_ts` 鮮度が切れていれば共有述語 (`claim_oldest` / `pending_exists` と同一、注入 clock) が consume の前に検出し、OPEN を拒否して signal を abandoned (鮮度切れ) にする。requeue しない。**snapshot と signal が同時 stale の case は、同じ 1 回の注入 `now` で両方を評価し、signal stale を優先して abandoned にする。** signal が fresh のときだけ snapshot stale の requeue を適用する。境界は共有述語どおりで、`datetime.now()` を使う変異は失敗する。**name lock / `core_lock` の待ちで snapshot が stale になったとき**、共通 helper が consume の前に検出し、consume せず gate 拒否 (executor と同じ理由・category)、claimed は `requeue` で pending に戻る (`requeue_count >= max_requeue` なら abandoned、未満なら pending)。helper は `open_from_snapshot` 内の判定と同じ純関数 `(snapshot, now, max_age_sec)` であることを関数の同一性と同じ境界値 (`age_sec == max_age_sec` は通り `>` で拒否) の test で pin し、**注入 clock (`self.clock.now()`) で試験する** (`datetime.now()` を使う変異は失敗する)。**第二判定 case**: 第一判定を通ったあと consume 後に executor 側の判定が拒否すると signal は consumed のまま (requeue されない)。**requeue 境界**: `max_requeue=0` (初回の拒否で abandoned、総 LLM 投入 1 回) と `max_requeue=2` (投入 3 回目の後に abandoned) で、現在の `requeue_count >= max_requeue` の規則どおりに pending / abandoned になる。(d) **CLOSE / CANCEL / HOLD は lock も拒否も受けず**、scheduler の SL/TP・drawdown kill switch 経路が name lock を取らないことを既存回帰 test で pin する。(e) **lock 順序は「一定時間で終了」でなく event 順序で観測する (barrier + tracked RLock)**: 共有の `core_lock` に owner を記録する tracked RLock を使い、実 `Scheduler` と OPEN で次の順を barrier で記録して assert する。1. OPEN が name lock を取得して `core_lock` 待ちに入る / 2. scheduler が `core_lock` を取得して SL/TP 処理地点を通過 / 3. scheduler が `core_lock` を解放 / 4. OPEN が `core_lock` を取得 / 5. 遅延 hook が `core_lock` を持たない状態で name 待ちに入る。SL/TP の完了と `_scheduler_tick_once` 全体の完了 (遅延 hook の同期実行を含む) は別の event として区別する。OPEN の保持範囲が commit-pre 完了後から `open_from_snapshot` の戻りまでであることも確認する | trade loop test、multi-process barrier、実 `Scheduler` + `_scheduler_tick_once`、tracked RLock |
| AC-23 | `get_signals` / `claim_oldest` / `pending_exists`: `plugin_deploy_op_signals` の行 (pending / claimed どちら由来も) は LLM に出ず claim もされない。**linked な pending だけが残るとき、および stale な pending だけが残るときも `pending_exists` は偽**で、signal mission を起動しない (遅延化で `expire_stale` より先に `signal_due_fn` が走る順序を再現して観測する)。除外 anti-join は 1 つの SQL 断片を共有し、`claim_oldest` / `pending_exists` は status・鮮度もその断片で共有する (条件の複製が無いこと)。`recent(exclude_deploy_linked=True)` は anti-join だけを足し、鮮度切れの abandoned・consumed の見え方は変わらない。付随表に無い gate 拒否 signal は通常の abandoned として見える。linked claimed は `recover_interrupted` と `reclaim_expired` のどちらでも `abandoned` で終端し、pending に戻らず mission を起動しない | tool 出力の before/after、scheduler の起動判定 |
| AC-24 | 他 process が name flock を保持している間、CLI は 30 秒待つ (TTY では待機を通知し、timeout は `lock_timeout` / rc 1)。lock を放すと完了する。依存名の lock も名前昇順。予告と本実行は別々に lock を取る | multi-process test |
| AC-25 | read model: 時計が逆行しても `updated_at` (deploy ops) と読み取りの実効時刻は単調。journal と deploy ops を跨ぐ並びが期待どおり。journal 側の逆行は触らずに吸収され、吸収しきれない場合は live 不一致で `--to` 必須に倒れる | 固定時刻 fixture |
| AC-26 | 遮断: improve tool 13 本の完全集合、`_ALLOWED_REL` の 3 本、API の `ROUTES` (24 endpoint) / action 完全集合、bot の custom-id action 集合 (6) と決定 action 集合 (3)、**`BotClient` のクラス本体に直接定義された公開メソッド集合 `{events, approvals, approval, approve, reject, retry, poll, cancel}` (継承属性は対象外)**、API path 集合、`TABLE_NAMES` (現物 30 表 + 本設計の 2 表 = 32)、`Action` 4 値がそれぞれ固定集合と一致し、deploy ops を含まない | 完全一致 test |
| AC-27 | 実隔離 worker (実 canary 経路、fingerprint 非依存の形) から CLI 起動・DB 書込・`plugins/` への書換えが拒否される。CI の `isolation` job は先頭で Landlock ABI を測り、ABI 8 以上なら実隔離 integration を実行し skip があれば fail、ABI 8 未満なら「kernel 不足」を明示して neutral とする。release の受入は ABI 8 の実機で手順書どおり手で実行する | integration |
| AC-28 | `approval <id>` 詳細表示が revoke 行 (`candidate_*` なし) で例外にならない | 表示 test |
| AC-29 | 運用文書 §7 が §10 の手順に置き換わり、文中に「未整備」が残らない | grep |
| AC-30 | `versions` が live・各版の承認 / revoke・整合・経歴・open / needs_retry / foreign の op を出す。name lock を取らず、DB の行・schema・app が持つファイルを作らず変更しない（SQLite reader の `-wal` / `-shm` は除外） | 出力 test |
| AC-31 | CLI 再実行の取り込み: 署名一致 (対象 identity・`to_target`・`revoke`・`force`) のときだけ open op を引き継ぎ、不一致は拒否して open op の `op_id` と署名を表示する。拒否時に FS・DB は不変 | 一致 / 各列の不一致 case |
| AC-32 | rollback の reconcile: 意図の記録後に戻り先 strategy の依存 indicator の live を変えると、`force=1` でも `needs_retry` になる (5(a))。新たに解決不能になった dependent があれば、FS の前に cascade と付随表が補完される | 注入 fixture |
| AC-33 | open 判定: `needs_retry/syscall_failed` の間は producer が INSERT せず OPEN のゲートが拒否し、approve / bless / retry と他の deploy op がその name を扱わない。`needs_retry/verify_failed` は blocking/open ではない。述語の定義は store 層の 1 組で、部分 UNIQUE・producer・OPEN・approve / bless / retry・deploy-op 開始・reconcile・status 表示の各消費者について `syscall_failed` の行で同じ判定を観測する。部分 UNIQUE が `intent` と `syscall_failed` を同時に 2 本持てないこと | 直 INSERT と操作の両方 |
| AC-34 | lock 順序: test 用の計装で、`core_lock` を保持している thread (tracked RLock の owner で判定) が `_plugin_locks` を呼ぶと失敗する。全経路 (scheduler tick・遅延 callable・OPEN・CLI・reconcile) を通して違反が無い。AC-22 (e) の event 順序と併せて、循環と SL/TP 遅延が無いことを示す | 計装つき統合 test |
| AC-35 | cross-column CHECK: §2.1 の表の各行に対し、op_kind と食い違う列の組 (disable に `to_target` あり、rollback に `to_target` なし、retire に `retired_path` なし、revoke に `revoke_row_id` なし / `phase != done`、`fail_reason` と `phase` の不一致、`from_kind='absent'` の rollback に `revoke=1`、`fail_reason='verify_failed'` で `op_kind != 'rollback_symlink'`、`revoke=1` で `approval_id IS NULL`、`phase='intent'` で `notify_attempted=1` 等) の INSERT / UPDATE が全て拒否される。`syscall_failed` から `intent` に戻す UPDATE は `notify_attempted=0` を伴わないと拒否される | 直 INSERT の CHECK 違反 case 列挙 |
| AC-36 | runbook (§10 3b / 3c) に open op の再実行と foreign の 3 種類 (symlink・不在 / plain / 不明・改変) の手順があり、文中に「専用コマンドは無い」と、plain の場合は `retire` を経ることが書かれている | grep |

AC-23 の観測には §5.5 の `signal_max_id` も含める。submit 時点の上限、`0` と `None`、requeue 済み既存 id の後続 mission での再 claim、採取後の producer INSERT が次 mission まで待つことを固定する。

## 12. 変更ファイル一覧

| ファイル | 変更 |
|---|---|
| `docs/superpowers/specs/2026-10-16-plugin-deploy-ops-design.md` (新規) | 本稿の清書。正本 |
| `docs/superpowers/specs/2026-09-19-switch-ops-hardening-design.md` | v1.10: 本設計 v1.1 への参照更新。詳細状態機械は複製しない |
| `store/db.py` | `plugin_deploy_ops` / `plugin_deploy_op_signals` の DDL (`_SCHEMA`)、`plugin_deploy_op_signals_signal_id` index、`TABLE_NAMES` に追加 |
| `store/plugin_deploy_ops.py` (新規) | 行 CRUD、blocking/open の共通述語 (`OPEN_PHASE_SQL` / `is_open`)、read model (`plugin_live_history`)、`live_identity` |
| `store/signals.py` | `abandon_pending_for_identities`、deploy-linked 除外 anti-join 断片、`claim_oldest` / `pending_exists` と claimed signal への再適用で共有する signal 鮮度述語、`claim_oldest` / `pending_exists` の「claim 可能」断片 (status・鮮度・anti-join、`pending_exists` に `now` / `freshness_bars` を追加)、`recent(exclude_deploy_linked=)` (anti-join のみ) |
| `store/approvals.py` | `append_revocation(...)` (固定文言、CAS を通さない追記専用、時刻規則) |
| `plugin/switch.py` | `disable_plugin` / `rollback_plugin` / `retire_plugin` の統合、`revoke_version`、依存列挙 (§4)、reconcile の deploy ops 処理、approve / bless / retry 入口の交差 name の open op 拒否 |
| `core/scheduler.py` | signal maintenance を tick 内の同期 hook から外し、接続修復 wrapper を **state を問わず毎 tick** 遅延 callable として `pending` に積む (improve hook と同形)。`ready` でのゲートは wrapper 内の実 maintenance 呼出しだけに移す。news / econ / cache hook は不変 |
| `service.py` | `on_signal_maintenance` を接続修復 wrapper + 遅延 callable として返す配線、**`App.conn_signal` の生成・保持・`App.close()` での順序 (`conn_core` の前)・`_busy_resources_after_join` への追加**、**`conn_signal` の再生成、連続失敗 counter、`health_latch.record_failure` (`signal_conn_unavailable`)、`_record_fatal`、`stop_event` (連続 3 回)**、`_scheduler_tick_once` が `core_lock` 解放後に実行 (既存の `for hook in pending`)、`plugins_root` / `conn` の受け渡し |
| `plugin/signal_producer.py` | lock 内再検証 + INSERT (§5.1)、lock timeout の専用結果と cursor 不進行 |
| `loops/trade_loop.py` | OPEN の source lock (commit-pre 完了後、`open_from_snapshot` の戻りまで) + 決定論的ゲート + consume 前の共有 signal 鮮度述語と snapshot 鮮度 helper。signal stale は abandoned、signal fresh 時だけ snapshot stale を requeue (§5.2) |
| `core/executor.py` | `open_from_snapshot` 内の snapshot 鮮度判定を共通 helper に抜き出す (判定・理由文字列・category は不変) |
| `tools/signal_tools.py` | `get_signals` の除外 (§5.3) |
| `backtest/cli.py` | `disable` / `rollback` / `revoke` / `versions` の parser と dispatch、`retire` の更新 |
| `config.py` / `setup/settings_migrate.py` / `config/settings.yaml.example` | settings schema v3 と、`worker.plugin_lock_timeout_sec=2`、`worker.reconcile_lock_timeout_sec=30` の v2→v3 移行 |
| `docs/operations/ops-api-2026-10-09.md` | §7 の置き換え (§10) |
| `docs/superpowers/specs/2026-10-04-ops-api-design.md` | §1.4 の「範囲外」注記に本設計への参照を 1 行 |

既存の `store/plugin_switch_journal.py` (`set_phase`) は変更しない (§2.4)。

## 13. task 表 (9 task、依存順)

規模は S / M / L / XL。順序は **`T1 → T2 → (T3, T4a, T6) → T5 → T4b → T7 → T8`**。T3 / T4a / T6 は T2 の後に並行できる (T3 と T4a は同じ `plugin/switch.py` を触るので worktree を分け、merge は T3 → T4a の順)。T5 は T3 と T4a の後 (symlink の retire は disable を呼ぶ)。**T4b (crash matrix の全点統合) は T3 / T4a / T5 / T6 がすべて終わった後**に限る。crash matrix (§7) の fault injection 骨格と全点の網羅は T4b に集約し、T3 / T4a / T5 / T6 は操作固有の正常系・拒否系だけを持つ (crash 点の重複を作らない)。

| task | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|
| T1 schema + store | 2 表の DDL・index・**cross-column CHECK (§2.1)**・CRUD、**blocking/open の共通述語 (`OPEN_PHASE_SQL` / `is_open`)**、read model (単調畳み・`needs_retry/verify_failed` の実効 event)、`live_identity` (symlink / plain)、`append_revocation`、`abandon_pending_for_identities` (pending / claimed 記録)、`signals` の共有 SQL 断片 (anti-join、`claim_oldest` / `pending_exists` の claim 可能断片、`recent(exclude_deploy_linked=)`)、`plugin_deploy_ops` の単調 `updated_at` helper | 部分 UNIQUE (blocking/open = `intent` または `syscall_failed`) を直 INSERT で破って拒否 / **CHECK 違反を列挙 (AC-35: op_kind ごとの列の食い違い、revoke の `phase`・`revoke_row_id`・`approval_id`、`fail_reason` と `phase`・`op_kind`、`intent` と `notify_attempted`、0/1 列と NOT NULL)** / 述語の単一定義 (AC-33 の store 側) / read model の単調性 / `append_revocation` の時刻が既存最新決定より後 / 付随表と `signal_id` index の使用 (EXPLAIN) / `pending_exists` が linked pending と stale pending を数えない / 新規表追加で既存 DB が開けること | L |
| T2 共通手順 + CLI 引き継ぎ部品 | lock 取得 (仮列挙 → 再列挙)、意図の記録 transaction の共通部品、immutable 署名の比較、交差 name の blocking/open 検査 (approve / bless / retry の入口)、通知試行の共通部品 | 署名の各列不一致で拒否 / 交差 name の拒否 (AC-14) / CLI の lock 待ち (AC-24) | M |
| T3 disable + signal invalidation | `disable_plugin`、依存列挙 (§4)、`--force` cascade、`revoke_version` (revoke 行 + `op_kind=revoke` の `done` 行 + pending の abandon + 付随表を 1 tx)、`--revoke` 糖衣 | AC-1, 4〜6, 11〜13 の正常系・拒否系 (直接 plugin と依存 strategy の両ケース、旧版 revoke 後に signal が除外されるケース)。変異: `_approved_hashes_by_name` を自前判定にすると revoke 後の復活 test が落ちる / revoke の付随表記録を外すと AC-12 が落ちる | L |
| T4a rollback + reconcile 実装 | 戻り先決定・検証 (IV-8、再検証を含む)、`rollback_plugin`、`--to`、`needs_retry` 収束 (`verify_failed` / `syscall_failed`)、完了表示、全 op_kind の reconcile (§6.1〜6.3。`from_kind=symlink` + live 不在の専用行を含む)、通知試行の回収 | AC-7〜10, 31, 32 の正常系・拒否系と各 reconcile 行の代表 case (3 世代 + 挟み履歴 fixture、target 1 byte 改変 / revoke 済み / 未承認 / kind 違い / 依存解決不能 / 依存変化)。全 crash 点の網羅は持たない | L |
| T6 scheduler 遅延化 + 専用接続 + producer lock + OPEN lock + ゲート | **`core/scheduler.py` と `service.py` で signal maintenance を遅延 callable に**、**`App.conn_signal` の生成・shutdown 順・busy 判定・毎 tick の接続修復 wrapper による再生成と失敗時の `signal_conn_unavailable` 記録 / tick skip / 連続 3 回での fatal 経路**、producer の lock 内再検証 + INSERT (lock timeout の専用結果、cursor 不進行)、`get_signals` / `claim_oldest` / `pending_exists` の共有除外、trade loop の source lock (commit-pre 完了後) + `signal_source_active`、consume 前の 2 述語を同じ 1 回の注入 `now` で評価する順序 (共有 signal 鮮度述語を claimed に再適用し stale は abandoned、signal fresh 時だけ executor と共有する snapshot helper で stale を requeue)、lock を `open_from_snapshot` の戻りまで保持、CLOSE / CANCEL / HOLD と SL/TP / kill switch 経路の非依存、lock 順序の計装 (AC-34) | AC-19〜23, 34。barrier 付き multi-process test、**実 `Scheduler` と OPEN の event 順序 test (AC-22 (e))**、注入 monotonic clock、**guarded-connection test と再生成失敗 1〜2 / 3 回の注入 (AC-21)**。signal の引数追加と `recent` / `claim_oldest` / `pending_exists` の差し替え箇所、`on_signal_maintenance` の呼び出し元を全数 grep。変異: `conn_core` を callable に渡す / shared signal freshness を claimed へ再適用しない / helper を consume の後ろに移す / `pending_exists` から鮮度を外す / OPEN の name lock を `core_lock` の後に取る、がそれぞれ落ちる | L |
| T5 retire 統合 | symlink = disable_symlink への委譲、plain の `retire_plain` 状態機械、`_plugin_locks` 化、依存確認、`st_dev` 検査 | AC-17, 18 の正常系・拒否系 (退避先衝突 / EXDEV / symlink に変わった live)。crash 点は T4b の P 系を呼ぶだけ | M |
| T4b crash matrix 全点統合 | §7.0 の N1〜N5、§7.1〜7.6 の D / R / P / S / G 全点を、T3 / T4a / T5 / T6 の実装の上で実 FS + 実 SQLite の fault injection (関数境界の例外 + `rename` / `unlink` / `symlink` の失敗注入) で網羅する | AC-3, 15, 16, 33 の収束側。変異: unlink を reconcile から外すと D3 が落ちる / `abandoned` に閉じる旧挙動に戻すと IV-15 が落ちる / 5(a) を `force` で免除すると AC-32 が落ちる / `syscall_failed` を blocking/open から外すと AC-33 が落ちる / `from_kind=symlink` + live 不在の行を `foreign` に落とすと R9 / R19 が落ちる / G6 snapshot または **G6a signal / 同時 stale** の consume 前検査を外すと対応 case が落ちる。規模が大きいので D / N / P と R / S / G の 2 束に分けて実行してよい | XL |
| T7 CLI + versions | 5 コマンドの parser・確認 (TTY / `--yes` / `--force` / `--dry-run`)、rc と stderr の文言 pin、`versions`、仮列挙 → 再列挙のリトライ、`afx ctl status` / `afx ops status <feature>` への op 状態表示 (`open` と `attention`、読めないときは `unavailable`) | AC-24, 25, 30。CLI の rc / 文言を pin。rollback 完了表示 | M |
| T8 文書 + 遮断 | 独立 spec の清書、既存 spec v1.10、運用文書 §7 置換 (§10 の 3b / 3c を含む)、遮断の完全集合 test (improve 13 本 / `_ALLOWED_REL` / API 24 endpoint / bot の 2 集合 / `BotClient` 直接定義 8 メソッド / `TABLE_NAMES` 32)、実隔離 worker integration (CI の `isolation` job が kernel の Landlock ABI を測り、ABI 8 以上なら skip があれば fail、ABI 8 未満なら「kernel 不足」を明示して neutral。release の受入は ABI 8 の実機で手順書どおり手で実行する。AC-27) | AC-26〜29, 36。spec と実装の番号 (IV / AC) の突き合わせ。integration は実 kernel を触る。Landlock ABI の skip 規約に従う | L |



## 14. 決定と後続チケット

| ID | 決定 |
|---|---|
| U1 | 操作は最上位 `afx plugin` CLI のみ。操作 API・`afx ctl`・bot に出さない。 |
| U2 | 反映は非対称とする。外す向きは次の signal INSERT から、rollback 先・新配備の有効化は restart 待ち。 |
| U3 | 版 dir は自動削除しない。容量観測、保全、手動削除時の整合確認は runbook の責任。 |
| 通知 | durable outbox は導入しない。activity は通知試行、状態の正本は `plugin_deploy_ops`。 |
| 電源断 | process crash のみ対象。directory fsync を追加しない。 |
| foreign | 専用回復コマンドを作らず、§10 の種類別 runbook で回復する。 |
| 接続 | signal maintenance は `conn_signal` だけを使い、`conn_core` を渡さない。 |
| 述語 | `blocking/open` と `closed` は §2.1 の一組の定義だけを全消費者が使う。 |
| stale snapshot | lock 待ちで stale になった claimed signal は consume せず requeue する。ただし同時に signal stale なら signal stale を優先して abandoned にする。 |
| 後続（低） | foreign で終端した rollback temp symlink を sweep ④の対象にするかは、別途判断する。指揮者が後続の作業項目を起票する。 |
| 後続（低） | 依存変化後の起動時エラー表示を追加する。 |
| 後続 | lock API を変更する機会に、process-global callback を `on_wait` 引数へ置き換える。 |

`live-signal-eval-blocks-protection-tick` は、signal 計算が `core_lock` を保持する部分のみ部分解消する。signal maintenance の別 worker 化、at-most-one / coalescing、失敗 plugin の backoff、tick 時間上限、shutdown を `signal-maintenance-offload-worker` に分離する。

## 15. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-18 | v1.1 | 実装済みの deploy 操作、状態遷移、status、隔離受入、運用手順 (手順の順序を含む) を正本化 | T1〜T7 の実装と crash matrix の実測で確定した事項の追記 | |
| 2026-10-16 | v1.0 | disable / rollback / retire / revoke の正本仕様を確定 | チケット `deployed-plugin-has-no-disable-or-rollback` | |
