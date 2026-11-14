# 評価用履歴の表示と import 衝突継続 — 設計 C2.2

日付: 2026-11-14。対象: `status-history-hidden-while-daemon-runs` / `history-import-stops-at-first-conflict`。
状態: C2.2。2026-11-14 利用者承認 (C2.1)、遮断器の既知除外を技術判断で追加。実装中。関連原本仕様・コードの変更は task とする。
基準: 設計レビュー opus 1 周目と terra の閉鎖確認 2 回 (非公開の記録)。参照: 段1a `docs/superpowers/specs/2026-11-11-backtest-history-freshness-guard-design.md`、同期の設計 (段 2・3、レビュー中)、評価区間の設計 (レビュー中)。

## 1. 動作目線

### 1.1 目的と技術判断 A・B・C

稼働中も主 pair の評価履歴の不足理由を確認できるようにする。孤立した異値を保持・記録し、その後の正常な足を取り込む。ただし系列全体のずれを新しい範囲へ拡散しない。

| 裁定 | 採用と理由 | 他案の扱い |
|---|---|---|
| A | **A1**。最上位 `afx status` の対象を限定した locked 経路へ評価履歴を追加する。daemon memory を必要としないローカル観測は最上位 CLI に置くという配置原則に従う | A2/A3 を本件では採らない。ctl/web への展開は同期段2・3の責務とし、共通 DTO は本件が先行して作る |
| B | **B2**。参照表示の正本は `watch_symbols`。literal 参照は readable (pairs ∪ watch_symbols) の外を resolver が拒むため、主 pair を除いた B1 は B2 に含まれる | B1 は不要な plugin 探索依存を残す。B3 は対象を増やさず依存だけを増やす |
| C | **C1 + 事前遮断器**。既存値を保持し、孤立した衝突を記録して非衝突行を続行する | C2 は後続未取込を直さない。C3 の自動上書きは既存の利用者裁定に反する |

A・B・C は技術判断として確定し、利用者への問いにしない。段1aが段2へ送った「衝突の永続記録・衝突後続行」は **histbugs が前倒しして置き換える**。置換操作、定期同期、通知、ctl/web 実装は同期側に残す。

**2026-11-11 の利用者裁定を維持する。主 pair の不足で止まるのは採用判定と承認だけであり、売買・資金保護・既存配備の動作を止めない。遮断器が履歴を stale/gap のまま残しても、この範囲を拡張しない。** 参照の疑いは見せるが、現在の主 pair guard の停止条件にしない。評価区間C2の参照可否判定とは別である。

### 1.2 `afx status` の表示と lock 保持者

既存 cli-surface spec `docs/superpowers/specs/2026-10-23-cli-surface-design.md` §6.1 / AC-18 の「locked 中は DB を読まず6行」を、次の表で明示的に置換する。locked は daemon 稼働の証明ではない。

| 観測した保持者 | 履歴読取 | 表示 |
|---|---|---|
| marker なしの `locked-by: unknown` | 設定・評価履歴専用 readonly 接続だけを許す | 既存 locked 情報 + 表示tz付き評価履歴 + NOTE + 既存 next |
| `setup` | 読まない | 従来の6行を完全維持 |
| `enroll:bot/web/notify`、未知/破損 enroll、`enroll:*+setup` | 読まない | 従来の6行を完全維持 |
| 将来追加される、上記 unknown 以外の保持者 | 読まない (既定拒否) | 保持者別の既存表示。履歴のために詳細 reader を呼ばない |
| free | 従来の offline 集計に同じ履歴 DTO を組み込む | 主判定・参照・案内を同じ renderer で表示 |

unknown には marker を書かない keys、post_start、repair 等も含まれ得る。daemon と断定して分岐しない。probe 後に instance lock を保持・再取得せず、unknown 判定後に保持者が変わっても readonly・fail-soft で扱う。設定中に見える可能性はあるが、不完全な DB を初期化して補わない。

unknown 経路は設定を一度読み、now を一度固定し、履歴専用接続を一度開き、終了時に必ず閉じる。`collect_status` 全体、state file、activity log、pending、plugin loader、systemctl は呼ばない。token の内容も読まない。keys の有無に応じた既存 next (ctl または journal 案内)、NOTE、rc=0 を保持する。表示tzを取得できなければ UTC と明示し、設定不明の履歴を ready にしない。

unknown の DB 接続は **URI `mode=ro` を強制し、sidecar の有無にかかわらず `immutable` を付けない**。接続 helper に明示的な強制引数を追加し、locked 呼出しが必ず渡す。DB新規作成、migration、journal mode 変更、rw fallback は禁止。sidecar がなく開けなければ `database_unavailable`。主 DB への論理的・物理的書込みはしないが SQLite の WAL/SHM sidecar の変化は許容する。

履歴が読めなくても locked 情報は表示する。stdout に固定理由を出し、stderr は空のまま。例外本文・例外型・技術log は追加しない。`settings_unavailable` / `database_unavailable` / `history_check_unavailable` を区別し、参照のみ失敗なら `reference_query_failed` を使う。SQLite busy wait は接続1回で最大1秒、retry なし。この1秒は全 query の実行時間の上限を意味しない。

### 1.3 主判定、参照、次の操作

主 pair の ready/stale/gap/unavailable、dataset、検査時刻、holdout範囲、鮮度・充足・日別欠損、適用 no-bars の観測という段1aの意味を維持する。設定が読めなければ参照節だけでなくブロック全体を unavailable とする。eval_source が none なら not configured と表示する。

参照節の見出しは **「holdout 区間の 0 本日 (評価に使えるかは eval_window の表示を見る)」** とする。設定順に重複除去した `watch_symbols` から主 pair を除く。空なら `no reference watch symbols`。主 pair のどれかに足がある完了 UTC 日のうち参照が0本の日だけを列挙する。正当な商品固有休場も含み得る「疑い」であり、ready や評価可能を意味しない。参照の薄い日の検出・再試行は同期側で扱い、このブロックに追加しない。

1銘柄の query/日付変換失敗は当該銘柄だけを unavailable にする。比較元の主 pair 有足日の読取が失敗したら参照節全体を unavailable にし、すでに得た主 verdict を消さない。主判定の read transaction と参照観測は別 snapshot であり得る。単一 command 全体が原子的でないという NOTE を維持する。

| 現在の主判定 | 履歴ブロック内の案内 |
|---|---|
| stale / gap | 不足 pair・範囲の確認、`afx history import`、その後 `afx status` |
| unavailable | 固定理由に沿う設定/DB/no-bars の確認と status 再実行。一律 import は案内しない |
| ready | `next:` なし。参照の0本日があっても同じ |
| not configured | 設定状態を表示。一律 import/再評価は案内しない |

参照0本日には「商品休場を含む疑い。評価可否は eval_window 参照」という情報1行だけを添え、参照 import を `next:` にしない。再評価は status の現在の充足から推測しない。gate/reevaluation の具体的結果・依頼記録から案内する。既存の pending 記録から件数を出すことは将来可能だが、今回 unknown 経路では pending を読まない。履歴ブロック以外の既存 `next:` は維持する。

### 1.4 import の見え方

正常な既存行は変更しない。同値は unchanged、異値は conflicted とし、遮断されなければ全範囲の非衝突行を挿入する。MT5 の最初の衝突日窓での停止をなくす。Dukascopy も同じ結果契約とし、衝突完走時の rc が **0 から3へ変わる**。

各操作・系列の全取得・検証と事前判定を終えてから batch 書込みする。遮断されたgenerationは履歴・衝突表とも書込み0件。再開前の確定prefixは維持し別表示する。他の正常銘柄の処理は進める。遮断時は永続衝突が保存されたと表示しない。

通常の衝突は新規・既知を分け、時刻順に最大20件 (§2.3) を人間向け stderr に旧新の OHLC/volume/spread、source、symbol、interval、bar_time、epoch、conflict id、state とともに表示する。完走結果には `processed=[start,effective_end)` と requested/effective_end、inserted/unchanged/conflicted を出す。gap があれば処理済みと充足済みを混同しない。`remaining_due_to_conflict=none` は出さない。

遮断時は `conflict_circuit_open`、重複行数 `N_overlap`、`N_test`、`C_new / C_known`、新規割合と閾値、時刻順先頭5件の旧新値、`omitted=(C_new+C_known)-shown`、`persisted=false` を出す。全件の永続保存のために遮断を迂回して再実行しない。人間が取り直して比較する手順を案内する。

## 2. 仕組み

### 2.1 現状と根拠の扱い

現コードで確認した箇所は `status.py::main/render_locked`、`status_history.py::collect/_reference_symbols/_reference_lines`、`store/db.py::connect_observer_readonly/values_match`、`store/ohlcv.py::import_history_bars`、`core/history_epoch.py`、`backtest/time_repair.py::apply_plan`、`tests/test_status_locks.py`。locked は即return、参照節はplugin探索とcatch-all、nextは無条件である。現 observer helper は WAL/SHM のどちらも無いと `mode=ro&immutable=1` に分岐する。C0の「常にmode=ro」は不正確だった。

MT5 は取得・検証後、衝突を含む最初の日窓まで確定し例外で止まる (窓内の行ループは続行する)。Dukascopy は時間単位の確定後も続けるが詳細を捨てる。共通 core と time repair が現在の runtime 履歴 writer であり、将来 live-confirmed の writer も下記の共有契約に従わせる。

C0が挙げた status 計測 (主1系列・3か月・5m) の中央値約111msは過去の限定条件の観測であり、本改訂では再計測していない。pair/watch 数増加、checkpoint 遅延、並行 writer の待ち時間の上限を保証する値ではない。CIに壁時計の固定値を追加せず、配備前の実測対象にする。

当時の `references: unavailable` の直接例外は未確認。literal参照0件という復元条件が正しければ参照SQL/日時変換には到達せず、候補は approved_plugins / literal_reference_coordinates 等の探索・解決側に絞られる。resolve_indicator_deps が処理済みの IndicatorResolutionError 以外の例外も候補。同じ実行の `pending steps: unavailable` と rc=1 の有無が追加の判別材料となる。C0の「SQLも候補」をこの条件下では撤回する。

C0の「配備4 strategy版」と評価区間C2の「strategy名2件」は単位が異なる。名前数と版数を混ぜない。当時の gitignore 対象 plugins 資材をどう復元したかのmanifest・hashは本改訂では未確認であり、4版の復元再現性も未確認とする。DB backupだけでは資材を復元した証拠にならない。例外の断定はしないまま、不要なplugin探索を除く修正は成立する。

### 2.2 構造化結果と評価区間C2の接点

`status_history.collect` は描画済み文字列tupleを返す形をやめ、次の人間専用 DTO を返す。日時は UTC の型付き値、列挙は安定順、固定理由は enum とし、renderer が表示tzへ変換する。CLIで verdict や閾値を再計算しない。

| object | 必須内容・責務 |
|---|---|
| `HistoryStatus` | `observed_at`, `evaluation_history`, `history_sync`, `eval_window` の別スロット。後二者の本件未収集は `not_collected` とし、成功や空結果に偽装しない |
| `evaluation_history` | `state(ready/stale/gap/unavailable/not_configured)`, `reason_code`, dataset、checked_at、holdout_start/end、guard/calendar版、主pair別の現行診断、no-bars適用情報、`references`、`next_actions` |
| `references` | `kind=holdout_zero_bar_days`, `state`, 比較区間、観測時刻、銘柄ごとの `state/reason_code/zero_bar_dates`。ready/evaluableを持たせない |
| `history_sync` | 同期側が永続run/seriesの観測を格納。現在guardから同期成功を推測しない |
| `eval_window` | C2が最後のrun記録を格納。run_id・基準時刻・pair/q/scope・S・除外座標と原因・delay・配備例外等はC2が所有。記録がない場合は `not_evaluated`。その場の再計算は別の `next_run_estimate` と明示 |

θや in-sample 実効始点・参照可否をここで再定義しない。watch health とC2のU診断 (self座標も含む) は別。DTOの現行 guard の要約から過去run可否を生成しない。histbugsは評価履歴スロットと拡張枠を作り、C2と同期側が自分のスロットを充填する。段2・3のctl/webはこの共通DTOを使い、独自判定しない。DTOは人間CLI/API用であり、agent tool/promptへ公開しない。

### 2.3 事前遮断器と取得単位

判定単位は固定された取得操作の1系列 `(source, interval, symbol)` と、その要求区間集合 (admission generation) とする。日窓やbatchで分母をリセットしない。同期の recent と独立した古いgap日窓は別操作としてよいが、閾値を通すための分割は禁止する。複数gapを一操作で取得するなら全窓を一括判定する。要求を確定済みの実効終端へ切り詰め、全窓を取得・検証し、同一bar keyを正規化・重複除去する。同一keyで入力内の値が異なれば応答不正として書込み0件で失敗する。

**C2.2 の判定契約 (同期 r2 I-4 の正本)**。`values_match` による分類後、同じ mutation lease 下で現在の系列・bar key・history_epoch・existing_digest・incoming_digest が完全一致する `open / kept` を探す。その再観測だけが `C_known`。値・epoch・旧新いずれかのdigestが変われば `C_new`、`replaced / superseded` は既知扱いしない。許容差だけの差は従来どおり unchanged であり、digest比較を先行させない。

`N_overlap = unchanged + C_new + C_known`、遮断用の **`N_test = unchanged + C_new`** とする。新規行はどちらにも入れず、既知異値で割合を希釈しない。監査上の conflicted は `C_new + C_known`。通過したときは既知も seen_count/last_seen_at/last_origin/last_run_id を通常のbatch内で更新し、人間表示を続ける。id/first_seen/初出origin/runと kept の判断は維持し、kept を open に戻さない。遮断・検査不足のgenerationは既知のseenも更新しない。

**初期固定規則: `C_new > 20 OR (C_new >= 2 AND 100*C_new > N_test)` なら遮断する。** `N_test=0` の割合は算出不能と表示する。1%ちょうどは通す。新規1件だけの孤立衝突は割合条件に掛けない。小 N でも割合判定を外さず、新規2件の高率異値は系統的ずれの疑いとして遮断する。`N_test < 200` なら件数だけを見るという r2 の例示案は採らない。

件数20は新規異値の遮断閾値である。通常表示上限20件・遮断表示上限5件とは独立した定数とする。既知異値が多数でも件数遮断せず、表示は `C_new/C_known` の総数と omitted を添える。表示省略は監査upsertの省略を意味しない。この初期閾値は保守的な設計値であり、実分布から推定済みの統計閾値ではない。変更は理由・規則版・境界testを伴う設計改訂とし、利用者の強制続行optionは追加しない。

**検査尾の判定契約も本節が所有する。** 同期は追記範囲と同時に、同じ系列・epochの既存区間を検査尾として事前固定し取得する。初期契約は直前2開場時間相当の確定格子を基本とし、足幅wで `K = ceil(2時間/w)` を計算する (1m=120本、5m=24本、15m=8本)。時間をそのまま120本と読み替えない。暦/no-barsで除外した格子、新規行、欠落応答を実際の重複数に数えない。実重複 `N_overlap >= K` かつ `N_test >= 2` を比較可能性の最低条件とする。既知異値は比較尾の存在には数えるが、遮断の分母には入れない。Kは系列正当性の証明ではなく初期の検査量である。

基本尾に既存行が足りない場合は、取得結果を見る**前**にDBの既存key・暦・固定規則から選んだ検査用既存区間を同じgenerationへ追加できる。追加区間・上限・全要求数をadmission前に固定する。結果のC_newや割合を見て正常行を後から足し、通るまで窓を広げる行為は禁止。新規2件の高率異値を見た後の追加取得は別の調査であり、その遮断を解除しない。日窓/batch単位の分割で判定をリセットしない。検査行と書込対象のkeyは重複除去し、seenは一観測一回とする。

同期のrecent・catchup・gap修復は、実重複が最低条件を満たさなければ **`deferred(reason=inspection_insufficient)`「検査不足」** と明示し、当該generationの履歴/監査0件、cursorを進めない。既知だけでN_test=0の場合も同じ。既に遮断条件に当たるなら検査不足へ格下げせず遮断とする。元々比較元のない初回手動importは `comparison_unavailable` を明示した導入として許すが、同期や比較可能な手動再実行の迂回路にはしない。手動もC_newの割合判定を外さず、同じ入力・epoch・digestの再実行では既知のseen更新と表示が続く。

N=0や新規異値1件だけでは系列ずれを検出できない。bridge の time rule 検査 (`time_rule_mismatch/time_rule_unverified`) は維持する。毎時の1hずれfixtureには足幅に応じた実重複尾を含め、日次を待たず遮断する。通常分布・検査尾の検出性能は実機未確認である。

**MT5/Dukascopy共に、一操作の一系列内の全要求窓を取得・検証してから判定・書込みする。** 複数系列は設定の安定順で系列ごとにstage→判定→確定する。全系列一括stage/一括rollbackはしない。取得失敗0件は当該操作・系列の範囲だけであり、先行系列の確定を戻さない。失敗後も独立した他系列を続け、全体rcは§3へ集約する。Dukascopyの逐次HTTP取得・rate limitは維持するが、C0のhour単位network partial案は撤回する。一操作・系列の全範囲の0件遮断は、すでにhourを確定する方式では実現できないためである。検証済み行をメモリまたは権限0600の一時spoolへ保持し、上限MAX_IMPORT_ROWS (現行100万) を厳守する。spoolに認証情報やraw応答を保存せず、操作完了・失敗・中断で削除する。通常cycle間は§2.5の寿命内で保持する。容量・取得・decode・検証失敗は当該操作・系列の履歴/衝突0件。sourceごとの別衝突実装を作らない。

事前読取とbatchの間に別の履歴writerが追加してN/Cが変わる競合を防ぐため、全取得後に **共通のhistory mutation用advisory lease** を取り、preflightから全batch終了まで保持する。SQLite write transactionを長時間保持する方式ではない。instance lockによる取得経路制限とは別の排他である。共通coreの全取込・time repair・将来置換/live writerがこのleaseを通る。履歴writer同士のみ直列化し、schedulerの通常取引DB書込みとstatus readerは対象外。repairはinstance/deploy lockの後、mutation leaseの後にDB transaction。histbugs単独のimportはinstance/deploy lockを取らず、mutation leaseの後にbatch DB lockを取る。**同期段2からはhistsync C2 §1.2/T2でimportの取得前instance lockを追加し稼働中取得を拒否する**。その段ではinstance lock→取得 (mutation leaseなし)→mutation lease→batch DB lockの順とし、deploy lockは追加しない。待たせず取得不能なら `database_busy` で書込み0件とする。段3もmutation lease→WriterAdmission→BEGINの順で逆順取得を禁止する。これにより同じseriesの判定後に新しい衝突が突然増え、途中から遮断になって「0件」を破ることを防ぐ。直接SQL等の契約外writerまで保証しない。

### 2.4 最終形の衝突表と同一性

表名は **`history_conflicts`**。`history_import_conflicts` は作らない。所有者は `store/ohlcv.py::import_history_bars` を含む共有coreと、その `store/history_conflicts.py` helper。importer/syncごとに記録を実装しない。

| 列 | 型・制約・意味 |
|---|---|
| `id` | INTEGER PRIMARY KEY。人間の指定用でありdigestの代替ではない |
| `source, interval, symbol, bar_time` | TEXT NOT NULL。履歴keyと同じ正規化、bar_timeはUTC ISOの統一表記 |
| `history_epoch` | TEXT NOT NULL。観測時の `core.history_epoch.history_epoch(conn)` |
| `existing_json, incoming_json` | TEXT NOT NULL。検証済みOHLC/volume/spreadのcanonical JSON |
| `existing_digest, incoming_digest` | TEXT NOT NULL。各JSONのUTF-8 SHA-256 |
| `origin, last_origin` | TEXT NOT NULL、CHECK `import/sync/live_compare`。初出のoriginは不変、last_originは再観測で更新 |
| `state` | TEXT NOT NULL DEFAULT `open`、CHECK `open/kept/replaced/superseded` |
| `first_seen_at, last_seen_at` | TEXT NOT NULL、注入したUTC clock。最初は同値 |
| `seen_count` | INTEGER NOT NULL DEFAULT 1、CHECK >=1 |
| `first_run_id, last_run_id` | nullable TEXT。手動importはNULL、同期導入後もFKは張らない。参照先runの最小identityをtombstoneとして保持し論理参照を保つ (SQLite ALTERでのFK後付けを前提にしない) |
| `resolved_at, resolved_by, resolution_action, reason_ref, revision_id` | nullable。人間操作の監査用予約列。actionは `keep_existing/replace_with_incoming/supersede` の固定語、価格や自由理由はagentに渡さない |

UNIQUE は **`(source, interval, symbol, bar_time, history_epoch, existing_digest, incoming_digest)`**。同epoch内ではr1の6成分案と同じ同一性であり、epochを加えるのはrepair前の記録が修復後の別足を指さないため (M10)。r1の6成分だけの案はこの点で修正採用する。値の組が変われば別記録、epochが変わっても別記録。originはkeyに入れず、手動と同期の同一観測をまとめる。

同一性はDB制約で保証し、アプリの先行検索だけに頼らない。`INSERT ... ON CONFLICT (上記UNIQUE列) DO UPDATE` により `last_seen_at=max(既存,観測時刻)`, `seen_count=seen_count+1`, `last_origin`, `last_run_id` だけを更新する。first列、state、旧新JSON、解決列は変更しない。runなしの再観測ならlast_run_idはNULL (「最後の観測」のrunが無いことを表す)。同一入力keyは操作内で1回に正規化済みであり、seenは確定した観測1回につき1増える。keptは再openしない。replaced/supersededも自動で再openしない。新epochの新記録はopenになり得る。

数値の判定は **2段** とする。

1. 履歴へのunchanged/conflicted判定は全6成分に現行 `db.values_match` を適用する。双方Noneは同値、片方Noneは異値、数値は `abs(a-b) < 1e-9`。衝突がなければ記録を作らない。
2. 衝突記録だけをcanonical JSONのdigest完全一致でまとめる。固定key順 `open,high,low,close,volume,spread`、空白なし、数値はfloatへ正規化したPython repr、Noneはnull、負のゼロは0.0へ統一。非有限数は検証で拒否する。量子化しない。`10`と`10.0`は同一digest。

許容差内の値だけの差なら第1段でunchangedになり、別の衝突記録は作らない。他成分が異値で **両入力とも衝突である場合**、incomingの一成分が1e-10違えば第2段のdigestは異なり別記録になる。この割り切りを固定し、「許容差内なら必ず同じ記録」と誤記しない。微小揺れが実運用で起きないという断定はしない。

fresh DDL、既存DBの `init_db` による冪等導入、旧DB fixture、`TABLE_NAMES` 完全一致、2回目no-opを同時に変更する。DBのschema versionは新設しない。現DBはCREATE IF NOT EXISTS / _ensure_column / data_migrations方式であり、settings schema_versionとは別である。C0の表は未導入を前提とするが、実装時に存在を検出した場合は放置して二重表運用せず明示移行を設計する。

### 2.5 batch transaction・途中失敗・再実行

取得/検証/遮断判定ではSQLite write transactionを開かない。通過した系列をbar_time順に最大5,000入力行、または経過50msを目安に、短い `BEGIN IMMEDIATE` batchへ分割する。各batch内で履歴INSERTと対応する衝突upsertを一体で確定する。記録/commit失敗なら **そのbatch全体** をrollbackし、確定済みbatchは残す。全期間1transactionや日窓ごとの外側transactionで囲まない。inner SAVEPOINTだけで分割したつもりにしない。

50msはtransaction開始からcommit完了までの運用目標。行ごとの単調時計で早めに分割しても、最後のSQLite呼出し/fsyncまでハード上限にできるとは主張しない。行数を実測で縮めて50ms目標を満たす。実装前に保証できる実測最大値は未確認。手動importも `busy_timeout=0` とし、busy時はtransaction外で短く譲り、操作内累積250msの待機予算で打ち切る。daemon側はこれにWriterAdmissionを加え、DB作業250ms/cycle後は新batchを始めず、100ms以上譲って同じrunの次cycleで続行する。取得済みstageと判定を通常cycle間で捨てない。mutation leaseは当該generationのpreflightから終了まで保持し、WriterAdmissionとDB transactionはbatchごとに解放する。network中はどのleaseも持たない。

結果には確定済みの `inserted/unchanged/conflicted`、最後に確定処理したbar_time (`last_committed_bar_time`、挿入だけでなく同値・衝突も含む)、処理済みprefixの排他的終端、`remaining=[resume_from,effective_end)` を出す。未確定batchを成功件数に加えない。先頭batch失敗ならfailed、先行batchが確定していればpartial。空応答の窓も処理範囲の進捗と行watermarkを区別する。複数系列なら進捗は系列別とし、全体で一つの時刻に潰さない。

案内は **元の同じ範囲を最初から再実行** を既定にする。確定済み新規行はunchangedになり、衝突記録は同じidを再観測しseenだけ増える。first_seen_at・stateは維持する。失敗batchの記録は残らず、再実行で初めて記録する。履歴と衝突の同一性について冪等であり、観測回数・last_seen_atは意図的に増える。取得結果やepochが変われば同じ記録になる保証はない。gapはDBを再検査して判断し、processedだけで解消としない。

### 2.5a 同期の分割再開・stage・cursor (terra I1)

一runは複数の取得操作・cycleを持つ。取得操作開始時に `operation_id, generation, dataset, epoch, requested_ranges, T, E` を固定する。stageは正規化済み行のみ、最大100万行、作成から最大10分。memoryまたは0600 spoolを使い、認証情報/raw応答は持たない。全取得完了前のcursorは進めない。stage期限切れ・process終了時は削除し、古いstageを次processで再利用しない。

取得中または反映中に stage が期限 (10 分) に達したら、未確定の batch を rollback して stage を破棄し、確定済み prefix と確定 cursor はそのまま保ち、mutation lease を解放する。operation の結果は `stage_expired` (技術失敗、rc は §3 の技術失敗と同じ) とし、残りの範囲は次の cycle で新しい generation として再取得・再判定する (prefix は新しい N/C に含めない)。受入: 期限到達を fake clock で注入し、prefix が残る・lease が解放される・次 cycle が suffix だけを取る・旧 stage を再利用しない、を確かめる。

同じgenerationの通常cycle間はstage・N/C・分類を維持する。mutation lease下でreadonly分類を一度済ませ、unchanged行は書込みSQLを省く。新規行と衝突だけをbatchへ渡す。INSERT競合時はrowcount=0の行だけ再比較し、契約外変更ならbatchをrollbackして技術失敗とする。全unchangedでも進捗を確定する。分類済みsuffixを先頭から再処理しない。

確定進捗は `operation_id/generation, committed_prefix_end, next_cursor, last_committed_bar_time` を系列別に持つ。prefixは行番号ではなく要求区間集合の時刻順prefixで、空応答窓も含む。cursorは最初の未確定範囲の先頭、残りは複数区間なら区間一覧とする。同期では履歴+監査+この進捗を同じbatchで確定し、失敗batchのcursorは進めない。readonlyで判明したunchangedだけのprefixも短い進捗transactionを必要とする。run/series終端の失敗はbatch進捗から復元する。

長時間lease占有を防ぐため反映開始後60秒で新batchを止める。正常予算終了は `deferred(reason=apply_budget)` とし、確定prefixを保存してstageを削除、leaseを解放する。stop/異常終了はinterruptedである。次runは **残りだけを新generationとして再取得・検証し、現在DB/epochに対して再preflight** する。N_test/C_new/C_knownは新generation入力と現在DBの分類から求める。確定済みprefixは書込対象へ戻さず、前回新規行を検査尾の比較母集団にも加えて希釈しない。必要な検査尾は元操作開始前から存在したkeyを候補として事前固定し、現epoch/値で再取得・比較する。足りなければ検査不足とし、前回prefixを都合よく追加しない。前generationのN/Cを流用もしない。残りが高率になれば新generationで遮断し、そのsuffixのみ0件、既確定prefixは維持し明示する。元操作全体0件だったかのように表示しない。epoch変更時も同じく残りを新generationへ送り、旧判定を使わない。

これは日窓ごとの恣意的な閾値リセットではなく、確定prefixと未確定suffixの境界でのみ認める再開規則である。日窓取得予算は操作開始前に範囲を固定するための予算であり、取得途中の未検証prefixを書いて進めてはならない。次runの新しいrecent終端も別操作とし、古いsuffixをround-robinで進めて飢餓を防ぐ。

人間のimport再実行は元範囲全体からでよい (§2.5)。これは新しい独立観測でありN/Cを再計算する。同じ行/id/stateを保つ冪等性と、初回と同じ遮断結果になる保証は別である。自動再開は保存cursorを使い、元範囲の反復で分母を増やさない。

### 2.6 time repair・明示置換・epoch

現在の `history_epoch()` は直近time_repair migration名または `none`。通常の非上書き追加・衝突観測・keep-existingではepochを変えない。衝突表の追加だけでも変えない。statusの新表示で既存承認を失効させない。

time repairはmutation leaseを取り、同じ履歴変更transactionで **移動元と移動先のkey集合の和集合** に属するopen記録を `superseded` にする。区間近似で無関係な銘柄やsourceまで閉じない。旧bar_timeや旧epochを修復後へ書き換えない。kept等の閉じた記録は過去判断として維持する。履歴変更・migration marker・supersedeを一括commitし、どれかの失敗で全部rollbackする。既に適用済みrepairのno-op再実行はepochや記録を変えない。

将来の人間置換は同期側がdaemon停止、instance lock、backup、reason、epoch+既存digestのCASを実装する。選択した記録をreplaced、同じ履歴keyの他のopen記録をsupersededにし、履歴変更・history_revisions追記・解決監査を同transactionで確定する。CAS失敗は0件。閉じた記録は消さない。keep-existingはkeptにし、epochは不変。

同期側が `history_revisions` を導入したら、空の間は現行epoch文字列を完全維持し、revisionがあるときだけ `(現行time_repair token, 最新revision id)` を曖昧さのないversion付きcanonical表現でepochへ含める。置換後は旧証跡とのepoch照合を不一致にし、再評価を要求する。epochは無効化印でありsnapshot/as-ofではない。修復後の通常importによる穴埋めで既存repairの `_post_state_matches` が不一致となりrc=5になる可能性は従来からあり、新設の衝突rcと混同しない。

### 2.7 既存行が誤っている可能性

11-03 13:45の衝突では、11-11の確定判定修正以前に形成途中の末尾が保存された可能性がある。これは仮説であり、既存値を正値と断定する根拠もincomingを正値と断定する根拠もない。r1には別の主系列の同時刻がlive cache集約と一致する反証と、当該参照には比較cacheが無いとの報告がある。本改訂では実DBを再照合していない。

人間はbridgeから当該barをread-onlyで再取得し、保存した旧新値・確定条件・修復前後のepoch・時刻を並べ、必要なら既存live cache集約とも比較する。DBへ試し書きせず、置換は同期側の明示操作へ送る。結果を修正前importの末尾監査の優先度判断に使う。衝突表と人間表示は判断材料であり、既存行保持の裁定は変えない。価格、個人の設定値、口座値は本設計へ転記しない。

## 3. 失敗時・結果とrc

import/syncの語彙は次を共有し、別名のpartial成功を作らない。

| result | rc | 確定の意味 |
|---|---:|---|
| `ok` | 0 | 範囲処理完了、衝突・既知gapなし |
| `completed_with_gaps` | 3 | 範囲処理完了、再検査で主pairのopen gap、または今回新たに生じた参照の疑いが残る。既知の商品休場疑いだけでは発行しない。importが充足を検査しない場合は発行しない |
| `completed_with_conflicts` | 3 | 範囲処理完了、衝突あり。gapもあれば別flagで併記 |
| `conflict_circuit_open` | 4 | 遮断generationの履歴/衝突書込み0件。旧prefixや他系列の確定を別途表示し、完走成功と呼ばない |
| `partial` | 1 | DB/取得等の失敗で一部系列/batchのみ確定。原因コード、進捗、再実行範囲を表示 |
| `failed` | 1 | 書込み開始前または先頭batchで失敗、確定0件 |
| `deferred` | 0 | 同期の閉場/予定待ち/予算による後送り。同期runにdeferred_reasonと未処理を必ず残す |
| `invalid_arguments` | 2 | 引数不正、書込み0件 |

混在時の優先順位: invalid_argumentsはI/O前、開始後の技術失敗 (partial/failed) > circuit_open > completed_with_conflicts > completed_with_gaps > deferred > ok。deferredと確定済みbatchの併存は同期の予定された分割だけに使い、DB busyの失敗をdeferredで隠さない。rc=4はhistory import/syncの遮断専用であり、repair-time固有rcは変更しない。

| 事象 | 影響と表示 |
|---|---|
| unknown statusの設定/DB/主履歴検査失敗 | locked情報維持、履歴だけ固定理由付きunavailable、rc=0、stderr空 |
| setup/enroll等のlocked | 設定・DB読取なし、従来6行とrc=0 |
| 参照1銘柄の失敗 | 当該銘柄だけunavailable、主判定・他参照を維持 |
| 全取得・decode・検証・spool失敗 | 当該操作・系列の履歴/衝突0件。旧prefixや他操作/系列に確定済みがあれば全体partial、確定なしならfailed |
| 事前遮断 | 対象generation0件、旧prefix維持を別表示、rc=4、上位5件の一時表示のみ。取引処理へ停止を伝播させない |
| 記録/INSERT/commit失敗 | 当該batch rollback、先行batch維持、failed/partial rc=1 |
| writer lease/busy予算切れ | 新たなbatchを始めず、確定の有無でfailed/partial rc=1、原因database_busy |
| 衝突完走 | 非衝突行と全衝突記録をbatchごとに確定、rc=3 |
| repair/置換の監査失敗 | その履歴変更transaction全体rollback。旧epochと記録の整合を維持 |

配備順は新schema/core/表示を導入→daemon再起動→manual import。稼働中旧daemonの下で新importだけ先行させない。runbookと公開変更履歴にDukascopyのrc変更、取得失敗0件への変更、部分確定時の再実行を明記する。Dukascopy実機疎通はr1時点で接続元の制限が報告されており、本改訂でも未確認。偽fetchの受入と実機の確認を混同しない。

## 4. test・受入

以下は実装時の必須確認であり、本設計改訂で実装・実行済みとはしない。

| # | fixture/操作 | 必須結果 |
|---|---|---|
| S1 | 別processのinstance lock + 保持者全種 + 鍵有無 | §1.2表どおり。setup/enroll/未知新保持者はreader禁止・6行維持、unknownだけ設定/履歴reader許可。他source・token内容は禁止 |
| S2 | unknown + sidecarなし、接続直後writerがWALを開始 | URIにimmutableなし、mode=ro強制。読めなければdatabase_unavailable、rw fallbackなし |
| S3 | 未commit WAL writerとstatus並行 | committed値だけ表示、writer transaction/主DBを変更しない。busyは1秒wait設定、retryなし、全出口close |
| S4 | 同一now・datasetでfree/unknownのready/stale/gap/unavailable | 同じ履歴DTO/ブロック、unknownはtz明示・rc=0・stderr空。設定不明はブロック全体unavailable |
| S5 | watchあり/pluginなし、watch空、主との重複、参照1銘柄失敗、比較主query失敗 | plugin loader不使用、空文言訂正、銘柄単位のfail-soft、主query失敗時は参照節全体だけunavailable |
| S6 | ready+参照0本日、stale/gap、unavailable | §1.3の案内。参照のみのnextなし、再評価の常時案内なし。商品休場を欠損断定しない |
| S7 | DTO、最後のrunなし/あり、次runの見込み | スロットと出所を分離、未収集/未評価を成功にしない。C2のθ・可否を再計算しない |
| S8 | 例外へ秘密canary、status stdout/stderr | 固定理由のみ、本文・例外型・秘密を出さない。ctl/web/APIの本件未変更を確認 |
| H1 | MT5最初の日に1衝突、複数後続窓、Dukascopy同等fixture | 既存不変、非衝突全行と監査確定、completed_with_conflicts rc=3、両source同じcore |
| H2 | 十分な重複を持つ全行1hずれ + 新規終端/穴 | 遮断、対象系列の履歴/衝突とも0件、rc=4、上位5件/omitted/persisted=false |
| H3 | C_new=0/N_test=0、1/1、2/199・200、20/2000、21/100000 | 手動の数値閾値は順に通過・通過・遮断/通過・通過・件数遮断。同期は別途検査尾の最低条件必須。日窓/batch分割でN/Cをリセットしない |
| H4 | 時間窓重複・入力内異値duplicate、全取得の最終窓失敗 | 重複のC水増しなし、異値duplicate拒否、両source取得失敗0件、spool削除 |
| H5 | 同epoch/値の再取込、手動と同期の再観測、kept再観測 | inserted=0、id/first_seen/state不変、seen+1、origin不変/last_origin更新。UNIQUE競合でも二重記録なし |
| H6 | 10/10.0、None、負ゼロ、非有限数、許容差境界、1e-10の差 | 正規化一致、検証拒否、abs差=1e-9は異値。微差だけはunchanged。別成分で両方衝突なら微差で別digest |
| H7 | 2batch目の記録upsert/commit失敗 + 元範囲再実行 | 1batch目維持、2batch目全rollback、partial rc=1、確定watermark/remaining正確、再実行で重複行・記録なし |
| H13 | 250msで数cycle、60秒終了・再起動・stage期限切れ・epoch変更 | cycle間は同じN/C、cursor単調増加、未検証窓の確定なし。再開はsuffixだけ再取得しprefix新規行でNを増やさない。全unchangedでも進行、後方系列も完了 |
| H14 | 系列A確定後にBの最後の取得窓失敗、C正常 | Bだけ履歴/監査0件、A/C維持、全体partial rc=1。先行確定なしならfailed。遮断同時なら技術失敗優先 |
| H8 | 複数系列で遮断/技術失敗/完走/後送り混在 | §3優先順位、系列別確定件数とremaining、gapとconflictを別flagで表示 |
| H9 | preflight直後に別import/repair、lease失敗 | 競合writerは譲る。N/C判定後に変更なし、遮断対象に先行batchなし、lock順序逆転なし |
| H10 | repairの移動元/先/無関係key、監査失敗、no-op | 対象openのみsuperseded、旧epoch/key維持、失敗全rollback、no-op無変更。置換は同期側で同等CAS受入 |
| H11 | fresh DB /旧fixture /init_db2回 /TABLE_NAMES | 最終表・UNIQUE・CHECK・nullable予約列一致、2回目no-op、epoch不変、schema_version新設なし |
| H12 | 現行CLIのrequested/effective_end・closed-bar・不正引数 | 既存確定条件/上限維持、rc=2、不要な固定remaining文字列なし |
| H15 | 既知kept 2件の15m gap日、必要な固定検査尾を事前取得 | 新規異値0なら遮断せずgap補完。既知2件のseen+1・kept維持・表示継続。2時間=8格子、実重複も検証 |
| H16 | 同一手動importの再実行、既知異値多数、表示上限超過 | 同じid/state、seen更新、C_known表示・omitted明示。C_knownだけで20件遮断しない |
| H17 | 新規2件/小N、既知多数+新規2件 | 1%超なら遮断。既知で分母希釈しない。結果後の窓拡張禁止、事前固定の検査区間のみ使用 |
| H18 | 毎時recentの1hずれ (1m/5m/15m)、尾の応答欠落 | 当該generation0件で遮断。比較不十分なら検査不足、日次まで盲目的追記しない |
| H19 | 同bar keyのepoch変更、existing/incoming digestの各変更、replaced/superseded | 各々新規として判定、旧kept判断を流用しない。完全一致open/keptのみ既知 |

性能受入: WAL・1m・最大watch/pair・5,000入力行batchで、共有coreの取込とscheduler writerを受入専用harnessで並走させ (同期段2以後のCLI importはinstance lockで稼働中拒否を別途確認し、本番daemon下で迂回しない)、、BEGIN〜commitの最大/p95、schedulerの待ち、status全体とcheckpoint遅延を測る。batch50ms目標を超えたら行数を下げ再測定し、目標を緩めて合格させない。mutation leaseを持つがDB write lockを解放しているbatch間にschedulerが進むことも確認する。結果は人間専用の配備記録へ残し、未知の最大時間を「短い」とだけ書かない。C2のgate snapshotによるWAL保持も併せて観測する。

## 5. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-13 | C0 | locked表示、watch正本、衝突続行と記録を提案 | 運用中の不可視化と後続未取込を解消 | — (元設計、commit未確認) |
| 2026-11-14 | C1 | A1/B2/C1を技術確定、unknown限定・強制ro、遮断器、最終表/epoch/rc/batch/DTO、全指摘の裁定と文書task | opus r1と利用者の本改訂指示。Dukascopy衝突rc=0→3、取得失敗は全取得後書込みへ変更 | — (本改訂、未commit) |
| 2026-11-14 | C2 | 分割再開generation・suffix分母・stage寿命・cursor、系列別取得原子性、空参照文言、同期C1整合 | terra閉鎖確認 Important 2 / Minor 2 | — (本改訂、未commit) |
| 2026-11-14 | C2.1 | §2.5a に stage の期限到達時の処理 (未確定 batch の rollback、prefix と cursor の維持、lease 解放、`stage_expired`、次 cycle で suffix を新 generation で再取得) と受入を追加 | terra の C2 閉鎖確認 Minor 1 | — |
| 2026-11-14 | C2.2 | C_new/C_known・N_test、表示と遮断の分離、小N維持、足幅別検査尾と検査不足、段1 T3正本、rc/FK整合 | 同期r2 I-4を利用者指定のsol推奨で閉鎖、m12/m14/m18追随 | — (未commit) |

## 6. 短い task 表

本改訂では下記の対象を編集しない。実装担当がコード・test・原本仕様を同じ変更として揃える。**段1の T3 (実装中) は本 C2.2 を正本にする。** C2.1以前のC/N分類・表示上限との結合を実装済みでも、本節と§2.3へ追随する。

| 順 | task / 対象 | 完了条件 |
|---|---|---|
| T1 | locked契約: `status.py`, `store/db.py`, `tests/test_status_locks.py`, observer関連test、cli-surface spec §6.1/golden/AC-18、`docs/operations/first-run-acceptance.md:172` | unknownのみ履歴読取・ro強制、他保持者6行維持、timezoneと固定理由、helper docstringも整合 |
| T2 | DTO/参照/案内: `status_history.py` と関連test、段1a §7、`docs/operations/reevaluate-deployed-after-history-repair.md` §7手順3、同期C2 §1.3/§2.8、評価区間C2 §2.5との接点 | watch正本、見出し、空文言/期待値、条件付きnext、再評価は具体的結果から案内、共通スロットの所有権を明示 |
| T3 | schema/core: `store/db.py`, `store/ohlcv.py`, 新 `store/history_conflicts.py`, schema/旧DB fixture/`TABLE_NAMES` test | 最終形、二段判定、epoch付きUNIQUE/upsert、closed state維持、agent隔離 |
| T4 | import両source/CLI: `backtest/mt5_import.py`, Dukascopy `importer.py`, `cli.py`, import test、共通mutation lease | 操作・系列別全取得→遮断→batch、rc/進捗/再実行・stage寿命・generation/cursor、例外契約変更。`ImportConflictError` docstring/全利用箇所、mt5末尾「1回の短いtransaction」コメント、ohlcvの古い件数だけ/窓分割前提コメントを更新または廃止 |
| T5 | repair: `backtest/time_repair.py`, `core/history_epoch.py`との契約、修復test | 同じlease、移動元/先のsupersede原子性、既存epoch/no-op/backupの意味を維持 |
| T6 | 原本の前倒し整合: 段1a概要/§2.3/§7/受入表/段の表/変更履歴、同期分は C1 で反映済み、C2/C2.2との実装時照合だけ (run/series status・rc・原子性・受入を含む) | 停止・部分commit維持と段2後送りを本件が置換。同期の二重所有宣言、partial rc=3、全locked読取、表/keyの食い違いを訂正 |
| T7 | runbook/公開変更履歴/性能受入 | 再起動後import、Dukascopy rcと取得原子性変更、失敗時再実行、閾値N/Cとwrite lock実測、§4受入を確認してから完了判断 |

## 付録A. opus r1 の指摘ごとの採否と理由

### A.1 Important 全7件

| 指摘 | 採否 | 理由・反映先 |
|---|---|---|
| I1 lockedの既存契約 | 採用 | §1.2/T1。unknownだけ例外を許し、setup/enroll/将来保持者は従来どおり。cli-surface/AC-18/golden/test/first-runを一括更新 |
| I2 immutable | 採用 | §1.2/S2。sidecarのない起動前窓でもimmutable禁止。失敗はunavailableへ |
| I3 最終書込み契約 | 修正採用 | §2.4/§3。最終名・state/origin/二段比較/UNIQUE/upsert/rcを本件で確定。epochをUNIQUEへ追加。解決列もnullableで最初から備えるので二度の中間schemaを作らない |
| I4 transaction | 採用 | §2.5/H7。batch原子性、確定prefix、再実行。50msは実測目標とし未測定ハード保証とは書かない |
| I5 遮断器 | 採用 | §2.3。1%超かつ2件以上、または20件超。全取得とwriter leaseで系列0件を守る。採用判定・承認だけが止まる裁定内 |
| I6 参照の評価可能との混同 | 採用 | §1.3/§2.2。指定見出し・共通DTO、θ二重定義なし、C2の最後のrunを別スロットへ |
| I7 段1aの前倒し | 採用 | §1.1/T6。本件が置換責務を持ち同期の重複宣言を訂正する |

### A.2 Minor 全14件

| 指摘 | 採否 | 理由・反映先 |
|---|---|---|
| M1 再評価の文書 | 採用 | §1.3/T2。statusの常時案内を消すとともに段1a/runbookを更新。履歴が埋まっただけで再評価必須と推測しない |
| M2 参照nextの常態化 | 採用 | §1.3。商品休場もあり、情報行だけにする |
| M3 発生源と復元根拠 | 採用 | §2.1。literal0件ならSQLへ到達しない。名前/版数の単位を分離し、plugins復元根拠は未確認と明記 |
| M4 DBのschema version | 採用 | §2.4/T3。fresh DDL/旧fixture/TABLE_NAMES/2回目no-op。settings版と混同しない |
| M5 remaining固定文字列 | 採用 | §1.4/§2.5。完走processedと失敗時remainingを使い分ける |
| M6 例外型の出力先 | 採用 | §1.2/S8。stderrもlogも追加せず、例外型も出さない |
| M7 settingsなしの到達性 | 採用 | §1.3。ブロック全体がunavailable、参照だけと書かない |
| M8 lockedのtz | 採用 | §1.2。履歴の表示tz明示、設定失敗はUTC表記 |
| M9 toleranceとdigest | 修正採用 | §2.4/H6。微差だけならunchanged。別成分で衝突の場合に限り微差で別記録という二段性を厳密化。「実際に揺れない」との断定は根拠未確認のため採らない |
| M10 repair/置換 | 採用 | §2.6。観測epoch、元/先keyのopen supersede、同transaction、epoch入りUNIQUE |
| M11 Dukascopy rc/core | 採用 | §1.4/§3/T7。rc0→3を公開変更として記載、専用記録は作らず偽fetchで受入。実機未確認を区別 |
| M12 配備順 | 採用 | §3/T7。再起動してからimport |
| M13 ctl/web/DTO/snapshot | 採用 | §2.2。段1はA1、段2・3は共通DTOを展開。主と参照snapshot差をNOTEの範囲として明示 |
| M14 既存値誤りの仮説 | 仮説として採用 | §2.7。旧新記録とread-only再取得で人が判断。正誤未確認、自動置換しない |

### A.3 その他の提案の採否

A/B/Cの「利用者へ問わず技術で決める」は採用 (§1.1)。本番DBのreadonly/sidecar例外/instance lock非保持は採用 (§1.2)。checkpointが約110msだけ遅れるという上限解釈は採らない。過去のquery実測はcheckpoint/並行writerの上限を証明しないため、§4で測る。

過剰設計への提案は、通常衝突の詳細表示と固定remaining削除を採用。C2.2では既知異値の増加を許すため、表示件数の上限と省略数を別途設ける。遮断時は大量となるため先頭5件の上限だけ残す。永続表を同期側へ先送りする代案は不採用。本件で最終契約を確定する利用者指示があり、共有coreの監査を最初から必要とするため。Dukascopy専用記録も不採用。同じcoreで足りる。

C0のDukascopy逐次確定維持は不採用。全期間の遮断で0件にできないため、§2.3の全取得stageに変更。r1のUNIQUE6列案はepochを加えて修正採用、解決列後付け案は初期nullable列へ変更して最終形を固定した。

## 付録B. 文書とコードの食い違い12件の扱い

| r1番号 | 訂正・実装時の更新先 |
|---|---|
| 1 | C0の常にmode=roを撤回、§2.1に現行immutable分岐、T1に強制roとhelper docstring |
| 2 | cli-surface §6.1/AC-18を§1.2の保持者別契約へ、T1でgolden/testも更新 |
| 3 | first-run-acceptance:172をunknown例外に更新、T1 |
| 4 | 段1aの停止/部分commit/後送りを本件が置換、T6で概要・§2.3・受入・段表・履歴 |
| 5 | 段1a §7とreevaluate runbook §7手順3の常時案内を具体的結果に基づく案内へ、T2 |
| 6 | 段1a §7の「daemon停止中も」をfree/unknown/その他保持者の明示へ、T6 |
| 7 | DB schema versionという記載を撤回、§2.4/T3 |
| 8 | literal0件時のSQL/日時変換説を候補から除外、§2.1 |
| 9 | strategy名数と版数の単位を分離、復元素材manifestなしの確定主張を撤回、§2.1。C2と整合させる際も数を未確認のまま同一視しない |
| 10 | 同期C0の表/key/state/rc/batch/展開時期を本件の最終契約へ統一、T6と付録C |
| 11 | mt5_importの「最後に1回の短いtransaction」、ImportConflictError docstringと呼出し、ohlcvの旧コメントを更新/廃止、T4 |
| 12 | status_historyのnone declaredをno reference watch symbolsへ、関連test期待値も更新、T2 |

## 付録C. 同期段2・3が守る契約

| 項目 | 同期側の必須対応 |
|---|---|
| 所有権 | 衝突永続化/続行はhistbugsが前倒しする。同期C0 §1.2とtask表の二重所有を削り本設計参照へ |
| 記録先 | `history_conflicts`を共有し、別表・sync専用digest/helperを作らない |
| 同一性 | epochを含む7列UNIQUE、二段比較、upsert更新列、closed state非再openを厳守 |
| 起源/run | origin=sync/live_compareを使う。手動NULL runを許す。初出origin/runは変えずlast観測だけ更新 |
| 遮断 | §2.3/2.5aの操作・系列・generation全体を取得・検証・stageし事前判定。通常cycle間はN/C固定、次runのsuffixのみ再取得・再判定。遮断generation0件、旧prefixは別表示。日窓/batchで恣意的に薄めない |
| concurrency | 全履歴writerはmutation leaseに参加し、lease→WriterAdmission→BEGINの順。network中のDB lock禁止。leaseなしでcoreを迂回しない |
| transaction | 履歴+監査+同期cursorを同batch確定。5,000行/50ms目標・実測、busy_timeout=0、250ms/cycleでyield。同runはstage維持、60秒終了/中断の後はsuffix再取得・再preflight |
| 結果 | §3のrc表と優先順位へ揃える。完走衝突/gapをpartial rc=3と呼ばない。HTTP成功をgap解消とみなさない |
| 人の解決 | kept/replaced/supersededのstateと監査列を使う。replaceはepoch/digest CAS、backup、reason、history_revisionsと同transaction。keepでepochを変えない |
| epoch | revisions空では現行token維持、revision後にversion付きtokenへ。repair移動元/先のsupersedeと同じ意味。古い承認だけを失効照合し売買を止めない |
| 表示 | 保持者別locked契約を継承。ctl/webは共通DTO、主guard/参照の疑い/同期結果/C2の最後のrunと次run見込みを分離。同期C0の「全lockedに履歴」をそのまま実装しない |
| 機密境界 | 価格・時刻・digest・自由理由・run診断は人間専用。agent prompt/toolや改善の記録投影に新規露出しない |
| 保証限界 | epoch/処理範囲は行snapshotの代替ではない。C2のgate内snapshotは維持し、将来の完全再現性とは区別 |

## 付録D. 未確認と完了条件の境界

当時の直接例外型、plugins復元manifestと名前/版数の対応、当該参照衝突の旧新どちらが正しいか、通常時のN/C分布、Dukascopyの実機疎通、最大構成でのwrite lock/scheduler待ち/DTO性能は未確認。本改訂で本番DB・bridgeへアクセスせず、コード編集・test実行・デプロイも行わない。これらを設計が実装済みであるとの主張に使わない。設計書の存在・全指摘の対応・文書構造を検査し、実装の完了条件は§4とtask表に残す。

## 付録E. terra閉鎖確認の全指摘の裁定

| 指摘 | 採否 | 理由・反映 |
|---|---|---|
| I1 分割再開の分母・prefix・cursor・stage | 採用 | §2.5/2.5a/H13。cycleと次runを区別、同generationはlease下でN/C固定。次runはsuffix再取得・再判定、prefixを分母へ戻さない。stage10分・反映60秒、進捗はbatch原子的 |
| I2 複数系列取得失敗の原子性/rc | 採用 | §2.3/§3/H14。操作・系列ごとの全取得、0件は当該系列。既確定があればpartial、なければfailed、技術失敗優先 |
| M1 空参照文言 | 採用 | §1.3/T2。主pair除外後の空集合を no reference watch symbols とする。主のみ設定済みのfixtureも必要 |
| M2 同期の更新対象 | 採用 | T6/付録C。同期C1のstatus・失敗・rc・受入表まで一括照合し、系列全体rollbackや衝突partialの旧文を残さない |

同期側の衝突・書込み正本は本C2.2である。付録CのC0参照は旧文書の更新箇所を指すもので、旧契約の存続を意味しない。live_compareの値を人の操作で履歴へ入れることも禁止する。originだけでは再観測の由来を判定できないため、将来その記録を作るならincomingのbridge由来検証を別に必須とする。同期C1ではlive比較自体を導入しない。

## 付録F. 同期 opus r2 と C2.2 の裁定

| 指摘 | 採否 | 理由・反映 |
|---|---|---|
| I-4 毎時の盲目・既知の反復遮断 | 修正採用・本書で閉鎖 | §2.3/H15〜H19。C_new/C_knownを分離しN_testから既知を除外。通過時のseen/表示とkeptを維持。同期は尾の供給責務だけを持つ |
| I-4 小Nの割合免除/後付け窓拡張案 | 不採用 | 新規2件の高率ずれを見逃す。比較区間は事前固定し、検査不足は明示する。遮断後の正常行追加で通さない |
| m12 rc=3常態化 | 修正採用 | §3。主open gap・今回新規参照疑いに限定し、既知疑いは独立表示。衝突完走rc=3は維持 |
| m14 run FK | 採用 | §2.4。後付けFKを約束せず、同期のtombstone保持と一致 |
| I-3 稼働中import | 同期段2で採用 | §2.3/§4。histbugs単独の現状と段2の取得前instance lock導入を区別し、性能はcore harnessで検証 |
| m18 T6循環 | 採用 | §6。同期C1反映済みとして実装照合へ。段1 T3の正本をC2.2へ更新 |

旧節・付録の N/C はC2.2では N_test/C_new に読み替える。既知の監査数は C_known として別に維持する。再開suffixへ検査尾を付ける場合も確定prefixの新規行を分母へ再投入しない (§2.5a)。本改訂は文書だけであり、受入fixture・実測は未実行。
