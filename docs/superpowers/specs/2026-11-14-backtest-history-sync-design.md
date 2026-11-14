# 評価用履歴の同期 — 段2・段3 設計 C5

日付: 2026-11-14。C5。2026-11-14 利用者承認 (段 3 の経路 = A)。実装は評価用履歴の表示と import 衝突継続 (`docs/superpowers/specs/2026-11-14-history-status-and-import-conflicts-design.md`) の core の後、T2 から。
根拠: 設計レビュー opus 3 周と terra の閉鎖確認 (非公開の記録)。書込みと衝突の契約の正本は `docs/superpowers/specs/2026-11-14-history-status-and-import-conflicts-design.md` (C2.2)。評価区間の設計 (レビュー中) と settings v6 を共有する。

## 1. 動作目線

### 1.1 何が起きるか

初回の古い履歴はdaemon停止中に `afx history import` で主pairと参照を同じ開始日から導入し、以後のholdout内の更新と穴修復をsyncで扱う。段2のoffline同期対象は設定順で重複除去した `pairs + watch_symbols`。段3aの初期自動取得対象は主pair (`pairs`) だけで、参照 (`watch_symbols`のうち主pairと重複しない系列) は計測・再受入後の別taskとする。sourceはmt5、native基底足は `backtest.base_interval` (1m/5m/15m)。pluginの配備有無、live registry、`datafeed.intervals` から対象を推測しない。live cacheを履歴へ転記しない。

2026-11-13裁定により自動同期は既定有効、人間所有のconfigで無効化できる。C0 §9の問いは閉じた。無効化はdaemon再起動で反映し、手動同期・状態表示・guard観測は残る。`eval_source != mt5` は `not_applicable`、手動syncはI/O前にrc=2とする。設定の既定値は `auto_enabled=true, lookback_days=7` (2〜31)。settingsは評価区間C2の追加キーと共有するmarkerだけのv5→v6 migrationを1回行う。`_migrate_three_to_four`の前例とCLAUDE.mdの規約に従い、既存YAMLへキー行を挿入せず省略時のmodel既定を使う。旧fixtureとmarker以外byte一致・冪等testを必須とする。ここで示す数値は設計定数であり個人の設定値ではない。置き場は `backtest.history_sync`。`config/settings.yaml.example` に既定値と条件をコメント付きで載せる。自動既定有効は取得前提の免除ではない。I-2はAで裁定済み。基準機の受入と、各利用者の短い自己計測・毎要求監視の二層で有効化する (§2.4)。段2のみ、計測前、または不合格時は自動取得0でローカル観測と停止中手動同期を維持する。

3aは開場中に主pairを毎時UTC :40に更新し、next_closeが返す各閉場の前に二段のcatchupを行う。UTC日末前catchupは必須にせず、次の毎時recentで埋める。正常追随時の日末未取得尾は§2.5の式でguardの日別60分閾値内に抑える。障害時のgap解消は保証しない。日次後ろ窓・古いgapは停止中手動だけとし、3bは保留する。閉場中にbridgeへ要求しない。失敗しても売買、資金保護、既存配備を止めない。主pairの採用・承認の履歴条件は段1aが所有し、本件で条件・証跡照合を追加しない。参照の疑いは表示するが主guardの停止条件にしない。

同期成功はin-sampleや参照評価可能の証明ではない。評価区間C2の最後のrunの診断を別ブロックで表示する。自動同期はholdoutより古い範囲を作らず、古くなったgapはexpiredにして自動修復をやめる。

### 1.2 人間の操作

| 操作 | 動作 |
|---|---|
| `afx history sync [--rescan-holdout] [--until-done]` | daemon停止中の手動同期。銘柄・期間列挙不要。auto無効中も可能 |
| `afx ctl history sync [--rescan-holdout]` | daemon workerに即時要求。ops laneの長いjobにはしない。202とrequest id、重複は同じpending要求へ統合 |
| `afx ctl history selftest [--clear-stop]` | daemon workerの自己計測を明示要求するCHANGE。202とrequest id。`--clear-stop`だけが永続監視停止の解除申請。合格前に解除しない。通常計測はdaemonが初回/boot変更時に自動開始 (§2.4) |
| `afx history gaps list` | 現datasetのqueue・attempt・次回・expiredを人間向けに一覧。readonly |
| `afx history conflicts list/show <id>` | histbugs C2.2の記録を表示 |
| `afx history conflicts resolve <id> --action keep-existing --reason <TEXT>` | 既存維持の判断を記録 |
| `afx history conflicts resolve <id> --action replace-with-incoming --reason <TEXT>` | daemon停止・backup等の正本契約を満たす場合だけ明示置換 |

daemon稼働中に最上位のsyncを別processで実行しない。instance lockのnonblocking取得失敗時は **rc=1、固定理由 `instance_locked`**。保持者の識別は保証できず、unknownでもdaemonと断定しない。「daemonなどが稼働中」と案内する。段2・段3導入前は「daemonを止めて実行」、段3のctl導入後だけctlを案内する。setup/enroll/unknown保持者でも迂回しない。offline syncは操作中instance lockを保持しdaemon起動を防ぐ。別のhistory-sync flockは追加しない。同process内のworkerはsingle-flight mutexで一つに限り、interrupted回収はinstance lock所有下かつ旧ownerの終了確認後だけ行う。

稼働中の手動importは履歴mutation leaseで同期workerと競合するため、**importも取得前から終了までinstance lockを必須**とする (両sourceに統一)。取れなければrc=1・固定理由でbridge要求0、停止中の実行へ案内する。`--allow-while-running` の迂回optionは作らない。MT5ではbridge lockの占有も防ぐ。Dukascopyはbridgeを使わないが同じmutation lease制限を適用する。Dukascopyの長時間importにもdaemon停止が必要で、その間kill switch監視が止まる費用を保守案内に明記する。段2の導入条件に含める。

list/show/gaps listはfreeとmarkerなしunknownだけ限定readonlyのlocal laneで可、setup/enroll/将来保持者では読まない。keep-existingも段2では停止中instance lock下のlocal CHANGEとして実施し、稼働中のlocal CAS迂回は許さない。replaceも停止中のみ。ctl経由の解決操作は今回追加しない。

ctl syncは人間のCHANGE、opsのwrite scope・認証済みprincipalを使い、既存の書込み認可と同じ制限を受ける。具体的scope/principalの識別子は実装時にops API正本へ合わせ、無認証経路を作らない。`_ACCEPTED_202` と `cli_surface._record` に登録する。web書込みはloopbackまたはTLSの正本契約を適用する。pending要求統合はrescanを論理和にし、受付202と同期成功を分ける。request/run idで結果を確認する。auto無効中も明示要求は可だが3aの対象・小窓・計測/受入・自動停止条件は省略しない。初期ctlでも参照・日次・古いgapを取得する迂回は作らない。参照はT7受入後だけ同じ小窓へ追加できる。rescanはローカルqueue再構成までで、取得できない範囲は停止中手動へ案内する。

`--rescan-holdout` は現在holdoutの再検査とqueue再構成だけを指す。全期間の強制再取得・expiredの無条件復活ではない。`--until-done` は上限付きsubrunを連続させ、dueの取得作業が尽きるか、失敗・閉場・将来backoff・人の停止で終了する。gap全消失まで無限に回す指定ではない。手動CLIのdeferredは正本どおりrc=0もあり得るため、0件でもstdoutに固定理由・未処理範囲・cursor・nextを必ず出す。成功や充足と誤表示しない。

### 1.3 表示と次の操作

histbugs C2.2 §1.2の保持者別locked契約を継承する。最上位statusで履歴を読めるlockedはmarkerなしunknownだけ。setup/enroll/将来保持者では従来表示を維持する。強制mode=ro、immutable禁止、migration/rw fallback禁止。新表未導入は `history_sync: not initialized` とし、再起動による導入を案内する。

共通 `HistoryStatus` DTOの `observed_at / evaluation_history / history_sync / eval_window` をCLI・ctl・webで共有し、rendererで判定を再計算しない。

| ブロック | 出所・内容 |
|---|---|
| evaluation_history | 現在guardのcached観測。dataset、checked_at、ready/stale/gap/unavailable、holdout、主診断、no-bars、参照0本日。参照0本日はhistbugsの表示、薄い日は同期の疑いとして区別 |
| history_sync | 永続run/series・queue・series状態。設定上のenabledとeffective enabled、自己計測/受入状態・監視停止の固定理由と規則版、worker alive、last attempt/success、request/run/trigger、範囲、確定prefix/cursor、件数、固定error、next retry、open gap/conflict、expired日数、観測時刻 |
| eval_window | 評価区間C2が保存した最後のrunの記録。run id、基準時刻、dataset/pair/q/scope、S、除外座標・原因、delay、配備例外。self座標も含む。記録なしはnot_evaluated |

`eval_window` は評価区間C2の記録読取に限り、同期でU/θ/S/可否を再生成しない。明示再計算を将来出す場合は `next_run_estimate` として独立表示する。history_guardの鍵集合・GUARD_VERSION・承認照合を変更しない。unknownで許す表は評価履歴/no-bars/epochと同期の `history_sync_runs/series/series_state/gaps`、`history_conflicts` の集計、評価区間の確定記録 `backtest_runs.params` だけ。最小列をreadし、pending/承認payload・state file・activity・pluginは読まない。承認payloadしか出所がない項目はnot_collectedとし、後から確定記録の保存契約を揃えるまで推測しない。

`history_sync` には「対象はholdout内と直近。in-sampleは作らない」を添える。expiredは「holdout外、自動修復なし N日」の情報行だけで、nextにはしない。

| 観測 | next |
|---|---|
| ready、同期正常 | なし。参照の商品休場だけでimportを案内しない |
| auto disabled | configで有効化して再起動、または条件を満たすctl/offline手動sync |
| 計測前/容量不合格/監視停止 | 自動同期なし。停止中手動sync、初回/boot変更はdaemonが自動計測。監視停止は原因確認後 `afx ctl history selftest --clear-stop`。周期retryや再起動だけで永続停止を解除しない |
| 自己計測不能 | 未初期化cursor / recent_backlog_offline_required (24開場時間超) / inspection_insufficientをstatus・CLIに示し、「停止中の手動同期の後に再実行」。§2.4の復旧手順に従い、手動完了だけで受入・永続停止を解除しない |
| 完了確認待ち・slot none | history_completion_pending、同bootで完了証拠なし・後続取得0と表示。既存応答が続いても解除せず、§2.4の保守復旧へ |
| 旧bridge/計測protocol未対応 | bridgeを更新。前提成立後daemonの自動自己計測を待つ。取得0 |
| closed/deferred | 次の予定。importで迂回しない |
| bridge失敗 | 固定理由の確認、次回時刻、必要なら手動sync |
| open主gap | gaps listで範囲を確認。3a小窓外は自動修復予定なしと示し停止中手動syncへ。反復して埋まらない日は既存no-bars手順の確認。通常rescanは不要 |
| reference_suspected | 商品休場を含む疑いと表示。採用可能と解釈しない |
| conflict | list/show。replaceにはdaemon停止が必要。系統的ずれはrepair-time型の専用調査へ |
| C2の実際の診断で主/参照in-sample不足 | daemonを止め、主pairと参照を同じ開始日からimport。主だけ古く入れると参照が全て除外され得る旨を添える |

主guard readyやsync成功だけから再評価を常時要求しない。具体的gate/reevaluationの結果から案内する。全表示は人間専用。秘密、URL、header、自由例外は出さず、reason本文・価格・時刻・digest・診断のagent投影を新設しない。

## 2. 仕組み

### 2.1 所有権・採った方式

衝突後続行・記録を段1aから前倒しする所有者は **histbugs C2.2** である。本件はその共通coreを呼び、別の書込み定義を作らない。表 `history_conflicts` のDDL/key/UNIQUE/upsert、二段比較、state・origin・run列、遮断、batch原子性、rcと優先順位、冪等性、epoch/superseded、解決監査は [histbugs C2.2 §2.3〜3・付録C・E](2026-11-14-history-status-and-import-conflicts-design.md) が正本。本件の記録・受入もその契約を参照する。旧r1のepochなしkey、別run列名、系列全体rollback、衝突partialは採らない。

取得・検証と書込みを分離する方式を採用する。現import関数を銘柄ごとに呼ぶ方式は衝突後停止を再現するため不採用。欠損だけの取得は訂正を見逃すため停止中手動の後ろ窓と併用する。3aの自動日次後ろ窓は今回入れず、毎時recentの検査尾を維持する。専用workerは自分で時刻・dueを測り `stop_event.wait` で休む。schedulerや資金保護threadにdue判定を追加せず、network/scanをcore_lockやops laneで実行しない。

### 2.2 run、取得操作、cycle、再開

一runは固定T・dataset・設定・暦と複数の操作を持ち、一操作は一系列と固定区間集合を持つ。recentを一操作、古いgapはUTC日ごとの独立操作とする。recent内の全窓は取得検証完了後に書く。古いgapの失敗はrecentを巻き戻さない。複数系列は系列別にstage→判定→batchと進め、失敗後も他系列を続行する。

cycleはDB作業250msを上限に新batch開始を止め、100ms以上yieldして同runを続ける。stage・lease・分母・prefix・cursor・期限・process中断後のsuffix再取得/再判定はhistbugs C2.2 §2.5/2.5aへ従う。全unchangedでも進捗を進める。通常cycleで先頭や後ろ窓の取得からやり直さない。反映60秒終了のdeferredとDB失敗のpartialを区別する。日窓予算で選ばなかったqueue行は未取得であり、今回処理済みにしない。

分類はreadonly接続で先に行い、書込みは新規行・衝突・進捗だけ。daemonではmutation lease→WriterAdmission→BEGIN、offlineではinstance lock→取得→mutation lease→BEGINの順を守る (offlineにWriterAdmissionはない)。ネットワーク中にlease/DB lockを持たない。5000行・50msは正本の目標でありfsyncを含むハード上限とは主張しない。行数を実測で下げる。BUSYはecon sliceの前例どおり10/20/40/50msでtransaction外再試行、累積250msで技術失敗、次回1分後。同cycleの待機予算と次回予定を混同しない。

### 2.3 範囲・scan・数値予算

足幅w、段1aのgrace gを使い `E=min(requested_end, floor_grid(T-g-w,w))`。Bは評価区間C2と同じ `in_sample_until(T,holdout_months)`。全自動範囲を `B,E)` にclipし、scanもEで止める。E以後の余裕1本を偽gapにしない。Bをまたぐ日もB以前の行は書かない。

offlineのrecentの追記範囲は `max(B, min(last_processed_E, E-1日))` からE。初回・後ろ窓は `max(B,E-lookback_days)` からE。日窓上限を超えた未取得区間はkind=unprocessedでqueueへ保存する。

**3aは有界な確定prefixで追いつく。** `c=max(B,last_processed_E)` とし、開場時間を積算して進む `advance_open` を共通暦で定義する。追記範囲は `[c,F)`、`F=min(E,advance_open(c,2時間))` に取得前に固定する。c未初期化は停止中初回同期を案内しMAX(bar_time)から推測しない。E−cが24開場時間を超える場合だけ `recent_backlog_offline_required` で停止中手動へ送り、自動でcold範囲を広げない。24時間以内も受入済みwarm形状であることが前提。正当な空格子を含めFまで判定・反映完了したらcursorをFへ進め、未取得 `[F,E)` は次の予定runへ残す。応答に実欠損があればqueueへ残し、cursorを充足の証明にしない。通常1時間間隔に2開場時間まで進められるので、全取得成功のrunが続けば中断分は毎回最大1開場時間ずつ縮み、長くなり続けるall-or-deferを解消する。run内のgeneration/batch・60秒中断のprefixはhistbugs C2.2 §2.5aのままとし、未確定suffixを処理済みにしない。

**検査尾は事前に既存keyから選ぶ。** anchor=c、基本尾は直前2開場時間。DBの同系列・epochの[B,c)内にある暦/no-bars有効な既存keyを取得前に読み、基本尾にK本なければ古いkeyを近い順に追加し、K本を覆う連続区間を固定する。`K=ceil(2時間/w)` (120/24/8) は正本のまま、尾の探索/取得幅は最大3開場時間、native格子最大180本とする。基本尾内にK本あれば拡張しない。要求前に既存key不足が分かればinspection_insufficientで要求0・当該generationの履歴/監査0・cursor維持。全窓の応答検証後は同じmutation lease下で遮断条件を先に判定し、該当すれば実重複/分母不足でもconflict_circuit_openを優先する。遮断に当たらない実重複/分母不足だけinspection_insufficientとし、いずれも当該generationの履歴/監査0・cursor維持。結果を見た拡張や正常行追加は禁止する。取得後は同じmutation leaseで正本の実重複・N_test・C_new/C_knownを再検証する。

追記と尾の2要求を同じ操作/generationにまとめ、重複keyを除く。尾1本の欠けは上限内の古い既存keyで事前補完できる。欠け合計が1開場時間を超える等で固定尾を作れない場合は明示的に停止中手動へ案内し、検査量を緩めない。尾は追記対象でなくても同じ比較・監査契約を使う。中断した同じ操作のsuffix再開では、元の検査区間を維持し確定prefixの新規行を遮断分母へ再投入しない (§2.5a)。完走した操作の次に始める独立した毎時recentは新anchorに対して上記の固定尾を事前選択する。両者を混同してprefix新規行を再開時の検査本数へ足さない。

1要求の時間幅は壁時計でなく共通暦の開場時間とnative格子数で制限する。閉場をまたぐ連続HTTP区間を許し、閉場片を追加要求にしない。no-barsは重複に数えず、容量の格子上限を緩めるためにも使わない。金曜尾から日曜開場後まででもこの定義で数える。閉場をまたぐ要求の実際の占有・warm性はT5/A8で再受入し、足数だけから性能を推測しない。C3の120本/2h上限は尾の欠けと両立しないため**最大180格子/3開場時間に改訂**し、受入前に旧容量認定を流用しない。

recent/検査尾外の訂正検出時期は停止中手動次第で保証しない。既存値は自動上書きせず、主guardはDBを直接判定する。

Nをその経路の対象系列数、Dをlookback_daysとする。offlineはUTC日境界でpagingする。全日閉場のUTC日は要求/予算に数えない (日曜のrecentで金・土・日を2開場日窓として扱う)。要求は単一in-flight、開始間隔5秒以上 (失敗/retryも含む)。検査尾の別要求も予算へ含め、admission前に全要求数を数える。上限外なら操作を部分確定せずdeferredにする。

| run種別 | recent上限 (検査尾込み) | 古いgap上限 | bridge要求上限 |
|---|---:|---:|---:|
| offline手動subrun | D+1開場日窓/系列 | 全系列合計32日窓 (別日の検査尾も算入) | `(D+1)N+32` |
| 3a毎時recent/起動時の直近確認/各閉場前catchup | 追記<=2開場時間/120格子、尾<=3開場時間/180格子、各1要求 | 0 | 検査尾込み最大2要求/主系列、run合計2N |
| 3a retry/明示ctl | dueの主系列・同じ小窓操作のみ | 0 | 上と同じ上限のdue対象分 |

3aはwarmと確認済みの有界recentだけ。事前固定尾・追記が上限に収まらなければ `inspection_insufficient / capacity_not_accepted` でdeferredとし、大窓やcoldへ広げない。起動時もこの範囲のみで、日次・古いgap・unprocessedの自動処理は行わない。参照追加後も日次/gap自動化を同時に解禁しない。経路が使えず残った範囲は表示し停止中手動取得へ案内する。

手動until-doneはsubrunごとの数・間隔を守る。系列別cursorとround-robin開始位置を永続し、残りを先に扱う。3aのcatchup予約帯には通常recent/retryの新操作を始めず、既存操作は操作境界でdeferredとして譲る。主pair内のround-robinを維持する。参照追加taskでは主pair優先と要求総量を再受入する。

holdout全scanは起動時・日次・明示rescan・no-bars/holdout_months/dataset/history_epoch変更時だけ。通常scanは変化区間と新しく完了したUTC日、参照中央値の必要20有足日へ限定する。日別countをdataset/暦/no-bars版/epochでcacheし、追記した日と比較依存先だけ無効化する。repair/置換・設定変更では全無効化する。holdout全scanとCPU集計はchunk間でyield、1queryはSQLite progress handlerで単調時計1秒の中断予算、Python走査は10msごとにyield判定する。

### 2.4 bridgeの単一lock、受動計測と実行時停止

現bridgeはcopy_rates_range・quote・注文・closeを単一lockで直列化し、現live ingestもこの経路を使う。worker thread分離やHTTP timeoutでは進行中native呼出しを取消せない。3aで共有lock待ち0を保証しない。

**現行の事実訂正**: live ingestは全設定系列を毎tick呼ぶのではなく、`next_probe_at`がdueのkeyだけを呼ぶ (`ingest.py` L182–185)。fableのログ集計では1 tick最大3件であり、コード上の固定上限3件とは扱わない。timeoutは `prepare()` が `ingest_budget_sec` (既定10秒) の残量から渡す (参照はさらに制限、L164・213–214)。`sources.mt5_bars_range` の30秒は既定引数でありlive ingestの実効timeoutではない。件数と足数の比からlock占有時間を推測しない。

**bridgeの受動計測を先行**: lock取得箇所を `_timed_lock()` 相当のhelperで包み、既存`_lock`の型と差替えには依存しない。requestごとのmutable集計をContextVarで分離し、thread poolの再利用で漏らさない。`__new__`で作る既存testにもクラス既定値で対応する。単調時計で各取得の待ち/占有、回数・合計・最大を測る。担体はHTTP response headerに固定し、既存body・detail文字列・status codeはbyte単位で維持する。list応答/response_modelへfieldを足さない。

| header (protocol v1予定) | 契約 |
|---|---|
| `X-Bridge-Metrics-Version`, `X-Bridge-Boot-Id` | 版と起動ごとのopaque値。口座由来にしない |
| `X-Bridge-Lock-State` | measured / not_locked / unavailable。不正/欠落を0と解釈しない |
| `X-Bridge-Lock-Wait-Ms`, `X-Bridge-Lock-Hold-Ms`, `X-Bridge-Lock-Acquisitions` | 1要求内の全取得の合計待ち/合計占有/回数。各区間と最大は同じprotocolの`Wait-Max-Ms`/`Hold-Max-Ms` headerおよび構造化記録へ (正式名は`X-Bridge-Lock-Wait-Max-Ms`/`X-Bridge-Lock-Hold-Max-Ms`)。単要求上限は合計占有に適用 |
| `X-Bridge-Error-Code` | symbol_unavailable / history_copy_failed / route_not_found / time_rule_mismatch等の固定code。404の分類もheader、detailをobject化しない |
| `X-Bridge-History-Request-Id`, `X-Bridge-History-State` | 下記の履歴専用slotのIDとwaiting / holding / completed。未受付はnone。payloadなし |
| `X-Bridge-History-Wait-Ms`, `X-Bridge-History-Hold-Ms` | 完了slotの合計待ち/合計占有。完了前の値では認定しない |

成功/HTTPException失敗/401/403/middleware/未知routeを含む応答へ計測headerを付ける。失敗はHTTPException(headers=...)と外側の応答処理で欠落を防ぎ、既存detailを維持する。lockなしはnot_locked。認証拒否に履歴slotの詳細は返さず、認証済みの既存応答だけがslotを運ぶ。秘密、URL、raw header、IDは人間status/agentへ投影しない。エラー本文完全一致testを維持する。

**timeout後の帰属と完了を確認する最小手段**: daemonの履歴fetchはopaqueな要求IDを同名request headerで送り、bridgeが専用の単一slotへboot/ID/stateを登録する。slotはnative取得前から、全native呼出しと解放のfinally完了後まで追跡し、その後だけcompletedにする。HTTP切断でこの追跡を消さない。slotは次の履歴要求まで保持し、通常のtick/Mission/CLI要求が上書きしない。未完slotがある別IDの履歴要求はnative実行前に拒否する。同IDの再送もnativeを重複実行しない。HTTP timeoutが受付前に起きslotがnone/別IDなら完了の証拠にならず、同bootでは確認待ちを維持する。完了観測はlockを取らないsnapshotで、認証済みの**既存応答**のheaderへ付ける。専用probe/新endpoint/解除のためのpoll要求は追加しない。daemonは既存取引/health応答のside channelからbootとslotを照合する。request IDは送信前にbootと共に永続し、完了確認・監視停止・campaignの進捗もdataset×symbol状態とは別のworker全体control行としてhistory_sync_series_state内に予約した単一制御keyへ保存する。系列の成功や詳細GCでこの状態を消さない。この追跡は未実装。既存応答が来なければ確認待ちのまま取得0とし、証拠なしで代替を出さない。

この段階は新しい要求を出さず、金曜閉場を含む1週間の基準線を取る。endpoint別に加え固定callerラベルtick / mission / cli / unknownを集計し、個人識別値を記録しない。Mission子processの`/ohlcv`、`repair-time` plan (24窓+server-time)、`check/init`はrequest admission外の競合利用者として含める。保持者を断定せず、このCLI群の稼働中利用を基準線とtimeout帰属で計上する。endpoint別待ち/占有p95/最大、tick/quote/close遅延・timeout/ingest失敗、計測負荷・欠落を確認する。T5で停止中手動の追記120格子/尾最大180格子/閉場跨ぎを測り、占有上限・p95規則・追加遅延・自己計測時間と標本数を版付きで確定する。仮のp95 10ms/最大50msは採用しない。目標未確定なら3a取得0。

**自己計測lifecycle**: (1) 基準機のA8合格後、(2) 各環境でdaemonが初回とboot id変更・形状/設定/受入規則変更のたびに有界な受動→能動自己計測を自動開始する。取得前提が揃う場合の自動再計測に人の操作を要求しない。1つのboot/形状で1 campaign、試行上限・期間・標本数・再試行間隔はT5で固定し、上限終了後はcapacity_not_acceptedで待つ (無制限probeなし)。既存応答でboot変更を認識する。auto無効中の自動能動計測は0、明示selftestは可。能動部分は通常の3a操作として正本coreで足/監査/cursorを確定し、同じ2N予算・間隔・tick admission・閉場予約・毎要求監視を適用する。計測用の捨て取得/別経路は作らない。未合格時は能動自己計測以外の定期取得0。ただし未初期化cursor・24開場時間超の遅れ・事前固定尾不足では通常3a操作を作れず、campaign/clear-stopも進めない。statusとCLI結果に該当理由、未処理範囲と「停止中の手動同期の後に再実行」を出し、自動回復を保証しない。

`afx ctl history selftest` は同campaignへの明示CHANGEで、auto無効/自動campaign不合格時も使える。永続停止中は `--clear-stop` を明示しなければ取得を始めない。clear-stopは解除の申請であり、停止理由を維持したまま有界計測し、合格時にだけ監査付きで原子的解除する。未完要求はcompleted照合、または新bootを確認するまでselftestも0要求。boot変更自体は永続停止を解除しない。ctl sync/再起動/周期retryによる迂回は不可。自己計測の受付は202、結果と未合格理由をrequest idで追う。offlineの別selftestコマンドは追加しない。

上記の計測不能時は、未完nativeがないことを先に確認してdaemon停止→初回は主/参照同開始のimport→offline syncで固定した範囲を正本どおり判定・反映し、未初期化cursorをその確定終端で初期化する (MAX時刻の代用は禁止)。既存cursorも確定prefixだけ進める。遅れと固定尾の条件を再検査してからdaemonを再開し、明示selftestでcampaignを再実行する。永続停止があれば--clear-stopを明示し、合格時だけ解除する。importだけでcursor/容量認定を捏造せず、手動でも尾不足等が残れば同じ理由で停止を維持する。

**毎要求監視と状態**: 成功/失敗を問わず応答または同boot/IDの完了slotの合計占有を上限と比較する。超過は即 `history_lock_budget_exceeded` を永続し後続取得0・health failed。p95も版付き規則で監視する。完了応答の計測欠落/不正は `history_metrics_unavailable` で同じ永続停止。単要求超過の応答データは正当性の失敗ではないため、既にその操作の全取得が済み正本判定を通る場合に限り追加bridge要求なしでbatch反映を許す。未取得窓があれば当該generationは反映せず、先行prefixを維持する。

5秒の全体fetch deadlineを超えたら `history_completion_pending` として後続取得0、まずcompletedを待つ。待ち時間が支配的で自身の占有が上限内だったことを完了記録で確認できれば通常の5/15/30/60分backoffへ移し、**恒久停止にしない**。自身の占有超過は永続停止、完了後の計測欠落/不正も永続停止。完了が未確認の間はfailedの確認待ちであり、予算超過とは断定しない。boot変化で旧nativeが存続しない配備条件 (単一bridge process、boot idはprocess生存期間と一致、旧process終了後のみ新process公開) をT4で検証し、成立しなければboot変更による回復を認めない。新bootでは永続停止がなければ、§2.3の前提を満たす範囲で自動再計測する。

受付前timeoutでslotがnoneのまま同bootの既存応答が続く場合も、status/CLIはhistory_completion_pending・完了証拠なし・取得0・保守復旧が必要と示す (slotのID/raw headerは出さない)。時間経過・再送・daemonだけの再起動では解除しない。保守手順で履歴送信元を停止し、旧bridge process/nativeの終了を確認して新processを公開、既存応答で新bootを確認した後にcampaignを再開する。永続停止があれば明示clear-stopも必要。旧native終了の配備条件を証明できなければ取得0を維持し、手動同期にも迂回しない。専用slot/永続状態/配備条件を含む実装可能性と、この復旧の成立はT4/A7で検証する。より簡単で安全な代替は未確認。

短いhistory request admissionをtick開始と共有する。既存WriterAdmissionのtick印は観測専用。tick中は履歴を新規開始せず、tickは履歴をjoinしない。Mission/人CLIは調停外であり位相を離しても衝突0を保証しない。単一in-flight、開始間隔5秒、tick後送りは期限内1秒ごと。connect 1/read 3/write 1/pool 1秒も初期予算とし、全体5秒は別の単調時計deadlineで実装する。共有executorのkill/再loginはしない。workerは所有HTTP接続を期限で閉じ、stop後4秒join+1秒終了処理を検証する。daemon workerの有限終了とremote native完了を区別し、未完slotを永続したまま停止可能とする。再起動後も完了/boot確認まで新規取得0。HTTP終了をnative完了とみなさない。追跡と有限終了を実装できなければ3a不合格。

### 2.4a I-2: 利用者裁定済み (2026-11-14、A)

採択は[fable推奨A (非公開の記録)。初期3aは**主pairの毎時UTC :40 recentとnext_closeが返す各閉場前catchupだけ**。UTC日末前catchupは必須にしない。参照系列は計測を通して同じ経路へ足す別task。基準機の受入と各利用者の短い自己計測・実行時占有監視で、既定有効と環境差を両立する。

3b (履歴専用の2台目terminal) は保留。今回置くのは `datafeed.mt5.history_executor_url` (既定null、値を表示しない) と、このキー未設定時も含め **`bridge_url` へのfallback禁止** だけ。値を設定しても3bの自動起動/接続/取得を実装しない。3aは既存の共有bridge_urlを明示使用する経路でありfallbackではない。キー有無で3aの接続先・要求数は不変、設定時statusに「3b未実装・未対応」と値なしで表示する。URLのuserinfoを含め全体を非表示とし既存news URLの秘密保護方針に合わせる。能力契約・terminal識別・再login予算の詳細は今回の要件から外す (付録G)。

3aが基準機計測/環境自己計測で不合格、または実行時監視で停止したら、**自動同期なし (段2の停止中手動だけ)** と同じ取得状態へ自動的に戻る。第2terminalへ自動切替しない。I-2は再質問せず、測定による目標確定は技術判断で進める。受入の実施・成功は未確認。

### 2.5 各閉場前の予定と日末の保証境界

共通market_hoursへ休日/DST対応の `next_close` を追加しbridge側と対のfixtureを持つ。next_rolloverは流用しない。金曜に限らず、12/24・12/31のNY 17:00以後の平日閉場も含め、共通暦で開→閉になる全てのCに二段catchupを行う。2026年の両日は木曜。未知のbroker早仕舞いは確認済みと扱わない。

通常recentの位相は**毎時UTC :40**。正時直後の判断Mission取得から離し、日末専用catchupは作らない。正常追随時、23:40の固定Tから算出したEによる日末未取得尾上限は `20分 + g + 2w`。最大15mではg=30秒の設計既定で50分30秒となり60分未満。全設定で `20分+g+2w <= 60分` を受入し、さらに実完了日の欠損合計<=60分とguard全体の充足率条件を満たすことを確認する。位相だけで他の欠損・充足率不足まで免除しない。処理は日境界前に完了する必要があり、`W_prior + R*(5秒間隔+5秒deadline) + W_tick + W_apply < 20分` も受入する。backlog/障害中にはこの保証を適用しない。予約帯と重なる通常回はcatchupへ譲り、閉場から日境界の開場欠損は閉場側の式で検証する。

閉場前は **C−15分とC−Lの二段** とする。各段はrun開始Tを固定してEを算出し、Eを完了時刻で先へ動かさない。全取得応答はC−30秒より前、DB反映・再検査はCより前に完了する。開始が閉場前でも応答が閉場後ならbridgeの取得後検査が503になり得る。第1段は通常recentからの遅れを先に縮め、第2段が最終の追いつきを担う。両slotが一致した場合だけ統合し、通常recentとの重複を抑える。起動時に過去slotを全再生しない。

予約幅は `L = W_prior + R*(5秒間隔+5秒deadline) + W_tick + W_apply + 30秒`。主pair N本で **R=2N (run合計の検査尾込み最大要求数)**。W_applyは反映/再検査の実測上限 (初期60秒以内)。W_priorとW_tickも実測し未測定なら有効化しない。3aのdaemon内履歴writerはsingle-flight workerだけで、offline import/sync/repair/keep/replaceはinstance lockで排除されctlは同workerを通るため **W_lease=0は構造的不変条件**。Lから外し、不変条件をtestする。契約外writerによるlease取得不能は即database_busyとして扱い測定待ちにしない。`L<=15分` を受入し満たせなければ不合格。予約帯には通常recent/retry/selftestの新操作を始めず既存操作境界で譲る。catchupは1秒ごとに期限内admissionし、超過はcatchup_deadline_missed。

`L + 2w + g <= 60分` と実完了UTC日の未取得開場格子合計<=60分を両方受入する。足幅/尾/Rで満たせなければ自動同期なしへ戻し、閉場後のretryで救済できるとは扱わない。取得/反映/DB再検査後とUTC日境界でguard cacheを更新し、guard閾値を緩めない。

閉場中は取得0・failure増加なし。開場後w+gから試す。time rule測定はbridgeの既存terminal tickを使い、起動後60秒以上離れた2観測まではunverified。既存応答のboot idで再起動graceの起点を識別し、単なる通信再接続でリセットしない。boot単位と各開場起点の最初の5分は `time_rule_pending` deferred、以後 `time_rule_unverified` 障害とする。boot/openそれぞれの実起点を永続し、通信再接続/daemon再起動でgraceを無限更新しない。

**閉場直前はtickが疎で、測定有効期限300秒によりunverifiedになり得る。** 二段catchupを入れても解消したとは断言しない。基準機の実機受入は1週間以上、金曜閉場と日曜開場を各1回以上含め、二段の完了・疎なtick・測定期限・開場時2観測を確かめる。金曜が不合格なら週末誤停止の解消を約束せず自動同期なしへ戻る。UTC日末は:40の取得反映後、00:00 cache更新が翌runより先でも、正常fixtureでreadyが維持され通知がflapしないことを確認する。早仕舞いは該当機会の実測がなければ未確認。通信障害時のゼロgapも保証せずguardを偽readyにしない。

### 2.6 gap queue、expired、backoff

queue keyは `(source, interval, symbol, day_utc, kind)`、kindはprimary_exact/reference_suspected/unprocessed。unprocessedは未取得区間一覧を日ごとの行に持ち、参照の半分以上ある部分日も消さない。未処理範囲の完走とDB再検査後に通常gap/疑いへ移すかresolvedとする。連続区間をkeyにせず表示時だけ連続日をまとめる。主pairはno-barsを除いた `[B,E)` の欠損を観測するが、自動queueはguardのgap判定に効く日 (現行の60分超/充足条件) に限定する。60分以下の欠けをすべて常設queueへ積まない。小欠損はローカルの日次scanと停止中手動取得で再観測し情報表示する。queue登録は3aでの古いgap取得の許可を意味しない。実装前の人間専用DB分布測定は別途必要・未実施であり、閾値変更は受入前に固定する。DB再読取で全expectedが揃ったときだけresolved。HTTP成功や空/部分応答で閉じない。

参照は主pairに足のある完了UTC日で0本、または直前20有足日中央値の50%未満をsuspectedとする。20日不足なら0本だけ。50%ちょうどは対象外。中央値比較は整数/有理数で境界を固定する。条件が外れたDB再読取だけでresolved。商品固有休場を欠損と断定せず採用guardに入れない。acknowledged状態とack操作は作らない。

主/参照とも欠損数や疑いが改善しない取得3回で24h、さらに3回で7日へ延長する。改善すれば通常周期へ戻し、成功解消ならattemptsを0にする。no-bars変更は主expectedとqueueを再評価する。選定はdue行の銘柄round-robin、古い日優先でcursorを永続し、埋まらない先頭日が予算を独占しない。

各run開始時と各gap要求直前に現在のBを計算する。日終端 `<= B` はexpired、要求/再試行予算を使わず表示にretryを出さない。Bをまたぐ日はactiveだが取得・書込みともB以後へclipする。stage反映直前にBが動いた場合も古くなった未確定部分を破棄して残りを新generationへ回す。これでrun跨ぎのin-sample自動追加を防ぐ。

holdout_monthsを増やしてBが過去へ戻ったときはrescanで同じkeyをopenへ戻せる。通常rescanだけではexpiredを戻さない。古い期間は明示import。評価区間C2付録C-3はこの方針で本設計へ統合して閉じ、別起票不要とする。C2のgate内snapshotは維持し、別gate間/後日の再現性(C-2)を解決したとはしない。

backoff正本は系列状態行とgap行であり、最新series結果ではない。手動成功は解除できるが手動失敗はdaemonの待ちを短くしない。retry triggerはdue操作だけを試し、全対象障害の連続数は全対象を実際に試したrunでだけ更新する。

### 2.7 記録、保持、置換

| 表/記録 | 同期が所有する内容 |
|---|---|
| history_sync_runs | id/request id、owner、trigger(manual/startup/scheduled/preclose/retry)、running/正本のterminal結果/interrupted、T/E/B、dataset/暦/設定識別、開始終了、集計・原因・未処理。healthは別field。ローカル日次scanを取得runと混同せずday_end取得triggerは作らない |
| history_sync_series | run×operation×generation×symbol×range kind/range、正本結果・件数、確定prefix/cursor、error、時刻。空窓の処理範囲と行watermarkを区別 |
| history_sync_series_state | dataset×symbolのlast_processed_E、attempts/next_retry、round-robin位置、最終health。履歴結果表と分離した再開/backoff正本。自己計測の規則版/boot対応・監視停止理由/解除監査を保持し、全系列共通の停止を1系列の成功で解除しない |
| history_sync_gaps | §2.6 key、open/resolved/expired、missing count/疑い、初出/観測時刻、attempts、無改善数、next_retry、resolution、選定cursor |
| history_conflicts | histbugs C2.2が所有。本件DDLや独自digest/helperを作らず正本を参照 |
| history_revisions | histbugs C2.2 §2.6を実現する明示置換の改訂印。id、kind、対象key、conflict id、実行者、時刻、理由参照。自由理由は人間専用 |

first/last_run_idにFKは張らず、runのtombstoneで論理参照を保持する。runs/series詳細は90日保持し、日別run集計は永続。未完run・再開cursor・gap・系列状態・衝突/改訂から参照されるrunの最小identityは削除しない (tombstone)。first/last_run_idの参照を壊さない。衝突は解決済みも削除しない。価格・自由reasonを集計に複製しない。GCも短いadmission付きtransactionで行いtickへ譲る。

revisionsをconflict id最大値へ畳む案は不採用。衝突idは初出順で改訂順でなく、古いidを後から置換してもepochが変わる保証にならない。独立した単調revisionが必要である。epochは正本のversion付き合成tokenとしsnapshotではない。

live比較は段3から外し、二表統合の調査へ送る。既存/将来のorigin=live_compare記録へreplaceを許さない。originがimport/syncでもlast_origin=live_compareやincoming由来が不明なら置換不可。初出origin不変のupsertを由来証明と誤解せず、bridge由来の別観測・検証を必要とする。正本C2付録Eの禁止を守る。

keep/replaceのstate、CAS、backup、lease、supersededの集合、原子的監査は正本をそのまま使う。replace一件でglobal epochが変わるため全strategyの旧pending承認・rollbackが照合不一致になり得る。既存売買・配備を自動停止しない。置換にはdaemon停止が必要で、その間daemonのkill switch監視も止まる運用費用を表示し、通常の保守手順内で人が行う。大量衝突の一括resolveを追加せず、repair-time型の調査へ送る。

新表はinit_db/data_migrationsで冪等導入し、fresh/旧fixture/TABLE_NAMES/二回目no-opを揃える。DBに新schema versionは導入しない。settingsは別物としてv6 markerへ上げる (§1.1)。dataset切替時は旧queue/backoffを休止し現在datasetだけを通常表示、衝突/改訂の監査は維持する。

### 2.8 cache、health、event、停止

workerはrun反映後、起動時、UTC日境界、設定/no-bars/epoch変更時と毎時にevaluation_history cacheを更新する。guard正本と同じexpected_grid/判定関数を使い、一致testを持つ。日別count cacheは再取得選定用であってguardの別実装にしない。auto無効でもローカル観測workerは動き自動取得要求0、明示ctl要求は前提を満たせば可。/v1/statusはcacheをchecked_at付きで返し、毎要求のguard再計算をしない。未収集はnot_collected、期限超過はstale observation、計算timeoutはunavailableとして旧readyを新観測扱いしない。readonly CLIはhistbugsの限定readerを使い、daemonと同snapshotだったとは表示しない。

同期healthはguardと分ける。完了確認待ちはauto disabledでもfailedを維持する。auto disabled・非mt5・capacity_not_accepted・history_executor_unsupportedの前提未達は非適用 (DTO health=not_applicable)、history_syncの障害/回復eventを発生させない。計測中も前提未達扱いで、永続監視停止はauto disabled/boot変更で非適用へ隠さずfailedを維持する。3b未対応の情報行だけでは3a healthを変えない。成功ok、一部取得/DB失敗または全対象失敗1〜2回はdegraded、全対象取得不能3回でfailed、遮断・占有超過/計測欠落による永続監視停止・完了確認待ち・worker起動失敗/死亡は即failed。既知open衝突・休場疑いをhealthへ恒常的に混ぜない。新しい問題と既知問題は人間statusの固定理由・未読表示で区別する。通知componentは `history_sync` 一つの実際のhealth遷移だけとし、疑似degraded→okやerror family別componentは作らない。同じhealth中の別issueはstatusへ記録し、通知を擬制しない。

guard悪化/回復は独立 `evaluation_history` componentでready→ok、stale/gap→degraded、unavailable→failed。not_configuredは非適用。無効化設定と独立して通知する。単に同期を止めたため通知経路が消えない。guard証跡を再照合して既存承認を追加停止する機能ではない。

既存event `service_health_changed` のcomponent_code/health_codeだけを用いる。componentは_SAFE_VALUE内の設計固定tokenであり、既存に固定語一覧があるというC0の説明は撤回する。人間bot/notifyで新componentの意味が分かる描画・互換testを追加する。event fieldsにsymbol、時刻、件数、価格、URL、自由理由は入れない。

eventは専用接続・WriterAdmissionの短いtransactionでbest-effortに投影する。履歴mutation leaseの外で行い、EventStore.emitのcommitを履歴batch内に混ぜない。永続health/通知watermarkから遷移を判断し、同一遷移の再通知を抑える。投影失敗で履歴をrollbackしない。crash境界で厳密exactly-onceは保証せず既存event streamの意味に従う。手動runは同期health eventを出さず、次のdaemon観測で正本から判定する。

workerのstart失敗とthread死はalive/heartbeat監視でstatusへfailedを出しdaemon取引を継続する。stop後は新要求・新batchを始めない。同期HTTPは外部threadからcancelできるとは書かず、各窓間でstopを確認し、接続を所有workerが閉じる。§2.4のfetch 5秒・join 4秒・終了処理1秒の別予算を守り、§2.4のcompleted slot/boot照合前に代替取得を開始しない。共有executorは停止しない。running回収は§1.2の所有確認下でinterruptedとし、確定cursorから正本どおり再取得する。

## 3. 失敗時・運用

terminal結果とrcはhistbugs C2.2 §3の表・混在優先順位だけを使う。running/interruptedは実行管理状態であり成功rcではない。衝突完走をpartialと呼ばず、gapも別flagへ残す。HTTP完了と充足完了を区別する。ctl受付HTTP statusをこのrcに代用しない。

| 事象 | 影響・固定理由・次回 |
|---|---|
| 取得/最終窓decode不正 | 当該操作・系列のみ履歴/衝突0件。他系列/先行recentの確定維持。技術失敗は正本rc。失敗seriesはrollback後に別の短いtransactionで記録 |
| series失敗記録もBUSY | run集計に未記録series/固定原因を残す。runも書けなければ成功表示なし、次回owner回収でinterrupted、確定cursorを復元 |
| 接続/HTTP失敗/5xx | bridge_unavailable、5/15/30/60分。ただしtimeoutは§2.4の確認待ちと帰属判定を優先し、待ち支配なら完了確認後の通常backoff。別経路へfallbackしない |
| 構造化404 | bridgeのX-Bridge-Error-Code headerで分類しdetail文字列とstatus codeを維持。symbol_select失敗=symbol_unavailable (24h)、copy_rates_range失敗=history_copy_failed (主pairも5/15/30/60分)、未知route/protocol=history_executor_unsupported (取得停止)。未分類404はinvalid_responseとし銘柄不在と断定しない |
| deadline/cold | history_completion_pending。後続0、既存応答の完了slotで帰属し通常backoff/永続停止を分ける。未確認中はfailedだが占有超過と断定しない。共有executorのkill/再loginなし、coldは停止中offlineへ |
| 占有超過/計測欠落 | history_lock_budget_exceeded / history_metrics_unavailable。即時取得停止・health failedを永続し、`afx ctl history selftest --clear-stop`再合格まで解除しない |
| selftest/解除 | `afx ctl history selftest [--clear-stop]`、CHANGE/write認可/202。初回・boot変更の自動campaignも通常3aの前提が必要。未初期化cursor・24開場時間超・尾不足は理由と「停止中の手動同期の後に再実行」をstatus/CLIへ (§2.4)。未完要求は完了/検証済み新boot前に取得0、手動完了だけでは解除せずclear-stop合格時だけ監査付き解除 |
| 同bootでslot none継続 | history_completion_pending・完了証拠なし・取得0・保守復旧が必要と表示。既存応答/時間経過だけでは解除しない。送信元停止→旧process/native終了確認→新bootの既存応答確認→campaign再開 (§2.4)。条件未証明なら取得停止を維持 |
| 経路前提未達 | history_executor_unsupported / capacity_not_accepted。取得0・health非適用・通知なし・通信failure数増加なし。旧bridgeは更新、容量未合格は自動計測の状態/明示selftestを案内。I-2裁定待ちは廃止 |
| bridge未設定 | bridge_not_configured、要求0。設定確認までfailed。自動source変更なし |
| 401/403、time rule mismatch、schema不正 | bridge_auth/time_rule_mismatch/invalid_response、初回通知、以後60分 |
| open直後/再起動直後unverified | 5分grace内time_rule_pending、deferred。以後time_rule_unverified、5/15/30/60分 |
| 閉場/要求admission拒否 | market_closed/tick_active、deferred。失敗数増加なし、次の開場/空き機会へ |
| 事前遮断 | 取得・検証後に正本の遮断条件を満たせば検査不足より優先してconflict_circuit_open、当該generation0件、非永続の上位例示。24h backoff、即failed (通知は§2.8の実health遷移時だけ)。旧prefixがあれば別表示 |
| 検査不足 | 要求前に既存key不足なら要求0。応答後は遮断条件非該当時だけinspection_insufficient・deferred。両者とも当該generationの履歴/監査0・cursor維持、停止中手動を案内。後付け正常行追加は禁止。境界はA21 |
| 新規・同値・衝突 | 正本の分類と確定件数。非衝突行を続行、既存値不変。完走衝突は正本結果 |
| 空/部分応答 | 正当行だけ反映、DB再読取で未解消queueを残す。無改善backoff |
| DB BUSY/監査/commit失敗 | 正本batch全rollback、先行batch維持。250ms累積後database_busy、次回1分。通信failure数へ加えない |
| apply予算終了 | 正本の予定deferred、prefix/cursor明示。stageは破棄、次回suffixの新generation |
| stop/crash | committed prefix維持、owner確認後interrupted、stage再利用なし |
| status/cache/event失敗 | 対象だけunavailable/投影失敗。履歴結果・売買状態を変更しない |

導入順は段2 (histbugs C2.2 core→同期schema/offline CLI・import稼働中拒否・表示) →bridge受動計測/構造化404/boot id→新規要求なしで金曜閉場を含む1週間の基準線→停止中手動のwarm小窓計測とA8目標確定→主pairだけの3a最小形と基準機の金曜閉場/日曜開場受入→各環境の短いdaemonによる自動自己計測・実行時監視→参照追加の再受入とする。3bは保留。取得前提未達/不合格/監視停止なら観測と段2だけを維持する。wizardは主pair/参照を同じ開始日・設定base_intervalで初回import、継続は段に合うsyncを案内する (1銘柄/5m固定例をやめる)。

rollbackは新コードのうちにauto_enabled=falseへ変更して再起動し、workerの取得停止を確認する。旧コードへ戻す前にstrict settingsが未知キーを拒むため `backtest.history_sync` と `datafeed.mt5.history_executor_url` の新キーを取り除く。C2の追加キーも戻す版の許可集合に合わせ、markerをv5へ戻して旧parserで検証する (さらに古い版は対応markerへ)。旧コードへ戻した後は同期workerが無いため無効化設定は不要。DB新表は残す (旧init_dbはCREATE IF NOT EXISTSで未知表を消さないというr1の前提を互換fixtureで検証)。履歴・監査・revisionは削除しない。旧版に新同期CLIが残るとは案内しない。

## 4. test・受入

以下は実装時の必須条件。本改訂では実装test・実機計測を実行していない。fake/fixtureと実機受入を混同しない。

| ID | ケース | 固定する結果 |
|---|---|---|
| A1 | histbugs C2.2 H1〜H19、両source共通core | key/比較/state/epoch/rc/batchを再定義せず正本testを通す。同期origin/runも同じ記録へ集約 |
| A2 | 250ms複数cycle、全unchanged、後方銘柄、60秒終了、process再起動 | cursor単調増加、stage通常保持/終了削除、suffixだけ再取得・再判定。prefixが分母に入らず予算反復で飢餓なし |
| A3 | A確定→B最終窓不正→C成功、recent後に古いgap失敗 | B操作だけ0件、A/C/recent維持、技術失敗優先。series記録BUSYならrun集計、run失敗ならinterrupted回収 |
| A4 | 同期2回、kept再観測、未知由来/live_compare置換、CAS失敗 | 正本の冪等性。live値置換拒否。global epoch影響・監査原子性・no-op維持 |
| A5 | 1m/5m/15m × 夏冬 × 金曜/12-24/12-31閉場、UTC日末 | 通信・time rule・容量が受入条件内なら各閉場を含む完了日の欠損60分以下。閉場要求0、bridge/core暦とDSTをfixture固定。正常追随の:40→UTC日境界でready維持・通知遷移なし、`20分+g+2w<=60分`と充足率を確認。開場起点5分graceの2観測も検証。実機未検証の早仕舞いは保証しない |
| A6 | C−15分/C−L二段と通常recent/retry/tick重複 | catchup優先、L<=15分と応答/反映期限を満たす。過去slot全再生なし。疎なtickで300秒失効/unverifiedになるfixtureを持ち、実機の金曜で未解消なら合格にしない |
| A7 | fake待ち支配timeout/自身占有超過/完了header喪失、Mission/CLI競合、版の組合せ | 完了slot前は次要求0。待ち支配+占有内なら完了後通常backoff、自身超過/完了計測不正だけ永続停止。clear-stop合格時のみ解除、再起動/ctl sync迂回不可。旧daemon×新bridgeはbody/detail/status不変、新daemon×旧bridgeは前提未達0要求。`__new__` lock test互換、401/403/list/response_model/未知route header、thread pool集計漏れなし。超過応答が全窓揃った操作のみ追加要求なしで反映。キー有無で3a要求先/数不変、3b未実装表示。slot noneの受付前timeout→同bootで既存応答継続でも停止維持、daemon再起動/再送で解除不可。旧native終了を証明した保守再起動→既存応答の新boot→campaign再開、証明不能なら0要求をfixtureで確認。売買継続 |
| A8 | 受動基準線→目標確定→主pair3a基準機受入→各環境自己計測 | まず新規要求0で金曜閉場を含む1週間のendpoint別待ち/占有・既存遅延を測る。停止中手動で追記120格子/尾最大180格子・閉場跨ぎを測り、占有p95/単要求上限・tick/quote/close追加遅延・自己計測時間/標本数を技術判断で確定・版管理 (現時点未確定)。その後基準機で1週間以上、金曜閉場/日曜開場を各1回以上含め3a受入。要求予算、二段応答C−30秒前/反映C前、取引timeout/ingest失敗の増加なし、確定遅延目標を満たす。各利用者はdaemonの自動自己計測+毎要求監視で有効化。初回/boot変更ごとに自動campaign、無効中の自動能動0、明示CHANGE、clear-stop失敗維持/成功解除を検証。未初期化cursor/24開場時間超/検査尾不足では理由・手動案内をstatus/CLIへ表示し、campaign/clear-stopで迂回不可。daemon停止→初回import→offline syncで確定終端へcursor初期化/前進→遅れ・尾の前提確認→再開→明示selftest (永続停止時--clear-stop)→合格時だけ有効化をfixtureで検証。手動後も不足/不合格なら停止維持。超過/不合格は自動同期なし。参照追加は要求数/占有/遅延/閉場容量を別taskで再受入。3b/coldは対象外 |
| A9 | 主N系列、retry/ctl、offlineのD日/until-done、tick中 | §2.3の経路別上限・5秒間隔・単一in-flight。3aは参照/日次/古いgap要求0、未取得queueを処理済みにしない。全対象を試さないretryで全対象障害数を増やさない |
| A10 | 11系列×7日と上限31日×1m・unchanged主体、WAL並行scheduler | readonly先行分類、unchangedに行別write SQLなし。5000行/50ms実測と250ms/cycle、batch間tick進行。50ms超ならbatch縮小 |
| A11 | scan/cache、30秒web更新、no-bars/epoch/dataset変更、1秒超SQL | APIで再scanなし、checked_at/期限を表示。通常は変化範囲だけ、変更時全無効化、progress handler中断・接続close。Python集計中もtick遅延目標を守る |
| A12 | exact格子、E以後1本、参照0/49%/50%、比較20日未満 | E以後をqueueにしない。no-bars除外。HTTP200だけでresolveしない。参照は疑い、guard停止なし |
| A13 | 無改善3回/6回、manual失敗/成功、再起動、round-robin | 24h/7日、manual失敗で待ち短縮なし。attempt/cursor復元、古い先頭gapの独占なし |
| A14 | 日終端=B、Bまたぎ、run中B移動、holdout_months拡大 | expired要求/予算0、B以前へ自動追加なし。拡大時だけrescanで復活。expiredにretry/nextなし |
| A15 | auto無効中guard悪化、既知衝突継続、新しい取得障害、worker死 | guard別componentで通知、自動要求0/明示要求可。既知issueで同期health張付きを起こさず、新障害はstatus表示、反復抑制・回復。graceでoutageとの不要な二重障害通知なし |
| A16 | ctl/manual/startup同時、setup/enroll lock、stop中HTTP | 一owner/一worker、稼働中local sync/import/keep/replace禁止。ctl受付と完了を区別、生きたrunを回収しない。stopで所有接続close/有限join、共有executor killなし、再起動後もcompleted slot/boot照合まで代替取得0。次回interrupted |
| A17 | unknown/free/setup/enroll、新表なし、eval_window記録あり/なし | histbugs locked契約、not initialized、共通DTO三ブロック。unknownの許可表だけを読みpending/承認payloadを読まない。最後のrun/未評価/次run見込みが混ざらず、sync成功でもin-sample可としない |
| A18 | 新/旧設定、fresh/upgrade/no-op、中断migration、rollback | settings v5→v6 markerのみ (評価区間C2と1回共有)、旧fixtureのmarker以外byte一致/冪等・省略既定、TABLE_NAMES・schema一致、履歴不変、FKなしの論理参照/tombstone保持、strict旧設定互換。保持GCでrun参照/epochを壊さない |
| A19 | reason/秘密canaryを正常・例外・RPC・activity・staging・backtest記録・次promptへ注入 | agent投影・eventへ新情報なし。人間向け詳細とagent境界を検査。URL/秘密/自由例外はstatusにも出さない |
| A20 | CLI AST/help/登録、ops event bot/notify | history/ctlのgroup/lane/label・selftest/clear-stopをcli_surface._record/CHANGE/write認可/202登録へ追加。selftestはhistory syncと同じ認証principal・loopback/TLS条件、重複統合時はclear-stopを暗黙合成せず明示申請だけ監査。既存event schema互換と新component描画を確認 |
| A21 | kept 2件の15m gap・同手動import再実行・新規2件の小N・毎時recentの1hずれ・epoch/両digest変更 | histbugs C2.2 H15〜H19。検査尾を含む要求数、検査不足の0件/cursor維持、後付け拡張禁止を同期側でも確認。尾の欠け1本を各足幅/金曜尾で事前補完、3h中断は2h追記prefixの連続runで追いつく。24開場時間超/尾3h不足は明示offline。日曜初runは尾1+追記1=最大2要求、壁時計48h超でも開場格子上限内。既知だけのN_test不足はcursor維持。境界fixture (各wのK): 事前key=K−1で要求0、事前K以上→応答N_overlap=K−1/C_new=0はdeferred、同不足でC_new=2/N_test=2は遮断優先。N_overlap>=Kを満たしC_new=2/N_test=200 (1%ちょうど) は通過、199は遮断。N_test=0/1で非遮断ならdeferred。遮断/不足は履歴・既知seenを含む監査0/cursor維持 |

実測値は未確認。r1の過去DB読取0.124秒やhistbugsの限定測定を最大構成の上限に使わない。guard/cache全scan、C2の長いgate snapshotのWAL保持・checkpoint、scheduler DB待ちも同時測定する。

## 5. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-13 | C0 | 手動/定期sync、queue、衝突解決、表示を提案 | 衝突後停止と稼働中不可視化 | — (元設計、commit未確認) |
| 2026-11-13 | 裁定 | 自動既定有効・config無効化可能 | 利用者裁定、r1の記録に基づく | — |
| 2026-11-14 | C1 | histbugs C2.2参照、cycle/cursor、予定/外向き予算、取得隔離、expired、cache/通知/DTO、当時の版不変、ctl経由、全指摘裁定 | opus r1と利用者方向づけを整合。共有bridge lockではtick非待機を保証できないため隔離を追加 | — (未commit) |
| 2026-11-14 | C2 | r2全指摘の裁定、3a/3bと未裁定選択肢、能力/受入強制、停止中import、閉場完了期限/待ち予算、histbugs C2.2検査尾、Minorと食い違いの追随 | opus r2と利用者改訂指示 | — (未commit) |
| 2026-11-14 | C3 | I-2=A裁定済み。主pair毎時recent/金曜二段catchup、受動計測先行、A8目標を基準線後に確定、短い自己計測と毎要求占有監視・超過自動停止。3b詳細を保留し参照追加を分離、live ingest事実訂正 | 利用者確定裁定とfable推奨A。既定有効と環境差を二層で解く | — (旧版、未commit) |
| 2026-11-14 | C4 | r3全件裁定。有界prefix/既存key固定尾、開場時間予算、自己計測lifecycle/timeout帰属/完了slot、UTC :40、header互換、共有v6、運用案内 | opus r3と利用者方向づけ、経路A維持 | — (旧版、未commit) |
| 2026-11-14 | C5 | 取得前不足/取得後遮断優先、計測不能時の手動復旧とcursor初期化、slot none継続時の表示/復旧/実装可能性、T3正本追随条件を補足 | terra C4閉鎖確認 Important 2 / Minor 2、histbugs C2.2 §2.3との整合 | — (本改訂、未commit) |

## 6. 短い task 表

この表は実装予定であり、本改訂で実行/起票したtaskではない。本文・付録の旧T番号参照はC2までの履歴であり、実装順と完了条件はこの表を優先する。関連CLI/help/ops-api/runbookと原本の訂正は各taskと同時に行う。

| 順 | 段/task | 完了条件 |
|---|---|---|
| T1 | 2: histbugs core | histbugs C2.2の比較/監査/epoch/batch/rcを正本testで照合。別定義なし |
| T2 | 2: 同期schema・offline CLI・import稼働中拒否 | 進捗/queue/expired/backoff/保持、v6 marker migration共有/旧fixture byte一致/旧strict互換、有界prefix/固定尾/180格子、両sourceの取得前instance lock、停止中keep/replace、until-done予算、CLI登録。A1〜4/9〜14/16/18/21の段2分 |
| T3 | 2: 表示・運用原本 | locked契約・共通三DTO・条件付きnext、初回主参照同開始/設定足幅import、rollback。段1a §10の段名、評価区間C2の§2.3/test14を共有v6へ、§2.7参照を本C5へ追随、histbugs T6は実装照合。HB §1.3/status_history/wizardのstale/gap案内を「停止してimport」または段3のctlへ、HB §3配備順を「schema導入で再起動→daemon停止→manual import→再起動」へ、HB §2.3/T2の相互参照をC5へ更新。両正本を実装時に同時更新し、評価区間だけ/同期だけ/双方の独立導入順とv5/v6の互換testを揃える。A17/19/20など段2分を完了し段2を独立導入可能にする |
| T4 | bridge受動計測と基準線 | endpoint別lock待ち/占有を記録し既存body/detail/statusを維持してHTTP headerへ計測protocol/boot/errorを付加。履歴専用slotを追跡し既存応答でcompleted確認、boot単一process条件、__new__/thread pool互換を検証。専用slot/永続control状態と配備条件の実装可能性、受付前timeoutでnoneのまま同boot停止が続く表示・保守復旧をA7で検証し、成立しなければ3aを合格にしない。sourcesの受動観測hook/専用fetchのheader読取を追加し既存戻り値/例外型不変、呼出し元price_provider/ingest/mt5_import/time_repair/setup/checksを全数照合。新しい要求を出さず金曜閉場を含む1週間のlive ingest/取引基準線と計測負荷・欠落を確認 |
| T5 | A8目標確定 (技術判断) | T4基準線と停止中手動の追記120格子/尾最大180格子・閉場跨ぎ/検査尾を測定。占有p95/単要求上限、追加遅延、自己計測時間/標本数/試行上限と間隔、Lの実測上限を確定・版管理。不合格なら自動同期なしで段2を維持 |
| T6 | 3a最小形: 主pairのみ | tick外admission、単一in-flight、5秒間隔、UTC :40 recent・next_close各閉場のC−15分/C−L二段のみ、毎要求占有監視/永続自動停止/自動自己計測・明示selftest解除。timeout帰属と完了待ち、W_lease=0不変条件、日末式、開場grace。next_close/暦対test、cache/guard/event/ctl/web/worker停止を統合しA5〜8/15〜16/19〜20ほか該当全条件を検証。基準機で実機1週間以上、金曜閉場と日曜開場を各1回以上含め受入。日末前catchupなし、300秒失効が未解決なら合格にしない。各利用者はdaemon自動自己計測後に監視付き有効化 |
| T7 | 参照系列の3a追加 (別task) | T6合格後、同経路の小窓へ追加。要求総量・占有/待ち・取引遅延・主pair優先/閉場容量を再受入。受入まで参照自動取得0、日次/古いgapは解禁しない |
| 別チケット案 | 3b・live cache転記 | 付録G。今回実装/詳細設計しない。専用接続先キー既定nullとbridge_url fallback禁止のみ残す |

A10の11系列/31日offlineと最大構成のCPU/DB/WAL/checkpointは段2で必要な実測、3a初期の参照取得受入とは区別する。古いdocstring/コメント、wizard、cli-surface、ops-api、help/first-runは各導入段と食い違わないよう更新する。

## 付録A. opus r1全指摘の採否

付録A〜Gは旧版の裁定履歴であり、版不変・120本・金曜限定・終了確認の旧要件はC4本文と付録H/Iで置換する。以下はr1時点の裁定履歴 (当時のtask番号を含む)。隔離の一律必須・日末catchup・日次/gap自動化等はC4本文・§6・付録H/Iが優先し、現在の要件とは扱わない。

### Important 13件 (I0〜I12)

| 指摘 | 採否・理由・反映 |
|---|---|
| I0 週末/日末誤停止 | 採用。§2.5/A5〜6。暦によるcatchup・容量式、反映後観測。guard閾値は維持 |
| I1 bridge単一lock | 強化採用。§2.4/A7〜8。tick禁止/間隔だけでは進行中lockを解けないため独立executor必須。実現性は未確認の配備条件 |
| I2 unchanged/飢餓 | 採用。§2.2〜3/A2/10。readonly分類、同run cycle列、日次後ろ窓、永続cursor |
| I3 CPU/全scan/API | 採用。§2.3/2.8/A11。変化範囲・cache・query中断、tick実測 |
| I4 原子性 | 正本へ統合採用。§2.1〜2/§3/A3。操作・系列別取得とbatch確定。失敗記録は別transaction |
| I5 queue | 修正採用。§2.6。日key、DB確認、主参照backoff、ack削除。通常古いgap4窓/6hへ縮小、手動32窓/連続subrunで負荷と導入を両立 |
| I6 expired | 採用。§2.6/A14。C2 C-3を本設計で閉鎖。B跨ぎはclip、古い自動追加を防ぐ |
| I7 health分離/無効中通知 | 採用。§2.8/A15。guard別component、新issueと既知issueを分離。別原因悪化も通知 |
| I8 live比較/置換 | 採用。§2.7/A4。比較自体を段3外、live/不明由来のreplace拒否、originだけを証明にしない |
| I9 settings版/rollback | 採用。§1.1/§3/A18。版不変・model既定、旧strictへ戻す前に節削除 |
| I10 書込み正本 | 採用。§2.1。histbugs C2.2のepoch入り最終契約とrcを参照。遮断は24h/即通知。一括解決は範囲外 |
| I11 eval_window | 採用。§1.3/A17。C2記録の独立ブロック、同期でin-sample生成せず初回importを明示 |
| I12 daemon手動経路 | 推奨(a)採用。§1.2/A16。ctl worker起動、offlineはinstance lock、owner確認回収 |

### Minor 20件

| 指摘 | 採否・理由・反映 |
|---|---|
| M1 404/未設定 | 採用。§3、symbol_unavailable 24h、bridge_not_configured |
| M2 開場/再起動unverified | 採用。§2.5、5分graceと永続起点 |
| M3 cancel不能 | 採用。§2.4/2.8。全体5秒deadlineと所有worker/executorの終了、外部cancelを約束しない |
| M4 自己due/死の観測 | 採用。§2.1/2.8。schedulerに新due処理なし |
| M5 retry trigger | 採用。§2.6〜7。dueのみ、全対象試行時だけ連続数更新 |
| M6 保持 | 採用。§2.7。90日詳細・永続集計・参照identity/監査保持 |
| M7 置換の全体影響 | 採用。§2.7。global epochと停止中監視費用を明示 |
| M8 revisions統合 | 不採用。§2.7。古いconflict idの後日置換はmax idで改訂検出できない。正本のrevision順を維持 |
| M9 手動予算 | 採用。§1.2/2.3。until-doneを有界subrunの列にする |
| M10 next修正 | 採用。§1.3。自動queueへrescanを常用せずno-bars/停止費用を案内 |
| M11 CLI登録 | 採用。A20/T2/T6。_record/AST/ops-api |
| M12 E終端 | 採用。§2.3/A12。余裕1本を偽gapにしない |
| M13 event接続 | 採用。§2.8。専用admission、mutation lease外、履歴commitへ混ぜない |
| M14 暦二重 | 採用。§2.5/A5。bridge/core一致fixture、CALENDAR_VERSIONがbridgeまで覆うと誤記しない |
| M15 dataset切替 | 採用。§2.7/A11。旧dataset休止・現dataset表示 |
| M16 無効/非適用 | 採用。§1.1。非mt5手動rc2、再起動反映、無効中も結果表示 |
| M17 閉場必ず503 | 採用。§2.5。閉場前取得が必要な根拠 |
| M18 表未導入 | 採用。§1.3/A17。readonlyでnot initialized |
| M19 失敗series記録 | 採用。§3/A3。rollback後別transaction、記録不能を成功にしない |
| M20 reason遮断 | 採用。§1.3/A19。staging/記録/activity agent投影のcanary |

## 付録B. 食い違い20件の裁定と原本更新先

| r1番号 | 採否・訂正・実装task |
|---|---|
| 1 | 採用。閉場必ずunverifiedへ訂正、§2.5/T4 |
| 2 | 採用。系列全体rollback撤回、正本batchへ、§2.2/§3/T1 |
| 3 | 採用。guardをsync healthから分離、§2.8/T5 |
| 4 | 採用。live比較を外しreplace由来制限、§2.7/T2 |
| 5 | 採用。settings版を上げず省略既定、§1.1/T1 |
| 6 | 採用。旧strictへ戻す前に新節削除、§3/T3 |
| 7 | 採用。全locked読取を撤回、histbugs unknown限定、§1.3/T3 |
| 8 | 採用。段1a前倒し所有者はhistbugs、§2.1/T1 |
| 9 | 採用。表/key/rc/batch/展開時期はhistbugs C2.2、§2.1/T1 |
| 10 | 採用。段1bは非配備監査、ctl/web/中央値は段2/3。段1a §10原本をT6で修正 |
| 11 | 採用。eval_window第三ブロック、§1.3/T3/T5 |
| 12 | 採用。2026-11-13裁定済み、問いを削除、§1.1 |
| 13 | 採用。404失敗表追加、§3/T2 |
| 14 | 採用。_SAFE_VALUEは任意token、設計側固定語で運用、§2.8/T5 |
| 15 | 事実訂正を採用。prune_cacheはcacheだけをsource/interval別に削除しhistoryは消さない。新しい履歴GCを本件へ足さない。チケット原文は歴史として残し運用説明をT6で訂正 |
| 16 | 注意として採用。端末履歴なし説は11-13に衝突後停止へ訂正済みとのr1報告。旧経緯を書き換えずT6に注記。今回実DB未確認 |
| 17 | 採用。mt5_import module/import_mt5 docstringの1分固定を1m/5m/15mへ、T6 |
| 18 | 採用。最後1回の短transactionコメントを正本batchへ、histbugs T4/T6と同期T6 |
| 19 | 採用。setup wizardの初回/古い期間import・継続sync案内、§3/T3 |
| 20 | 採用。econ_slice_workerの250ms/50ms/timeout0を前例として明記。BUSYは10/20/40/50ms同cycle再試行と累積250ms終了、1分は次run、§2.2/T1 |

## 付録C. 未確認と保証境界

I-2の経路選択は§2.4aのAで裁定済み。bridgeのlock待ち/占有基準線、追記120格子/尾最大180格子・閉場跨ぎの取得時間、A8目標、自己計測の具体的期間/標本数、最大構成のtick/DB/CPU/WAL/checkpoint、直接例外・過去復元素材は未確認。bridge/core暦一致、HTTP全体deadline、停止時間、bot/notify新component描画も実装受入待ち。3aの性能改善・週末誤停止解消を実証したとはしない。3bの独立性・broker多重session等は保留チケットの未確認事項。

参照中央値は休場の正確な暦ではない。MT5が保持しない範囲は埋まらず、別source fallback、補間、評価区間の自動短縮は行わない。C2はgate内snapshotを所有し、run全体/後日の不変入力は別チケットのまま。二表統合・live転記・履歴保持削除は範囲外。本改訂で本番DB読取、MT5 bridge要求、コード編集、実装test、デプロイ、起票は行っていない。

## 付録D. opus r2 の指摘ごとの採否と理由

本付録はr2の裁定をC3で更新する。C3本文と§6が優先する。実装済みという意味ではない。

| r2 | 採否 | 理由・反映先 |
|---|---|---|
| I-1 配備条件の強制 | C3で修正採用 | §2.4/§3/T4〜6。計測対応bridgeを先行、基準機受入/各環境自己計測/毎要求監視で強制。旧bridge・不合格・超過停止では取得0。3bはキーとfallback禁止のみ |
| I-2 取得経路の分割 | **裁定済み A (2026-11-14)** | §2.4a。主pair毎時recent/金曜二段catchupだけで開始、日末前は必須にしない。受動計測→目標確定→短い自己計測/毎要求監視で有効化、超過停止。不合格は段2手動だけ。参照追加は別task、3b保留 |
| I-3 手動共有経路/段2の行き先 | 修正採用 | §1.2/T2。警告や危険時間帯だけの制限より、importもinstance lockで稼働中禁止に統一。段2は停止中、段3導入後のみctl案内 |
| I-4 遮断器と操作形状 | histbugsへ統合 | histbugs C2.2 §2.3/H15〜H19正本。同期は事前固定尾を供給し受入。小N割合免除は不採用。後付け正常行で希釈しない |
| I-5 閉場到達条件 | 採用 | §2.5/A5〜8/T6。T固定と応答完了期限を分離。待ち込みL、主pair優先、二段予約と通常recent/retry除外、next_closeと対test、実機測定 |
| m1 rc | 採用 | §1.2。lock競合rc=1、invalid_argumentsのrc=2を流用しない |
| m2 ctl | 採用 | §1.2/2.8。無効中の自動0/明示可、rescan和、write scope/principal、202登録/CHANGE、web transport条件 |
| m3 設定/最大構成 | 採用 | §1.1/2.4/A9〜10/T2。backtest.history_sync、example、専用URL、31日上限も計測 |
| m4 guard cache | 採用 | §2.8/A11。毎時/反映後/日境界/変更時、guard正本関数、間はchecked_atを表示 |
| m5 微小gapの常設queue | 修正採用 | §2.6。guardに効く日だけを自動queueへ、小欠けは表示/日次。実DB分布は未確認で測定taskを残す |
| m6 中断の部分日 | 採用 | §2.6。unprocessed種別と未取得区間を永続化、lookback外でも消さない (B外はexpired) |
| m7 閉場日の窓 | 採用 | §2.3。全日閉場を要求/予算から除外、日曜recentで金曜尾を遅らせない |
| m8 404 | 採用 | §3/T4。symbol/copy/routeを構造化固定codeで分離、主copy失敗は通常backoff。既存bridgeは未対応 |
| m9 deadline/再login | C3で限定 | §2.4/2.8。5秒fetch/4秒join/1秒終了処理は初期予算、native終了を確認し代替重複なし。共有executor kill/再loginなし。3b再login予算の詳細は保留 |
| m10 独立測定/再起動 | C3で限定 | §2.4/2.5。現bridgeの2観測、既存応答のboot idでgrace管理。3b独立測定の詳細は保留 |
| m11 通知過剰 | 採用 | §2.8/A15。同期health一componentの真の遷移のみ、疑似issue遷移やfamily乱立を削除。別issueはstatusへ |
| m12 rc=3 | 修正採用 | histbugs C2.2 §3を参照。主open gapか今回新規の参照疑い。既知疑いは表示を維持 |
| m13 locked読取 | 採用 | §1.3/A17。許可表を列挙、pending/承認payloadを読まない、読めない確定診断はnot_collected |
| m14 run FK | 採用 | §2.7/A18とhistbugs C2.2。FKは張らずtombstoneで論理参照保持 |
| m15 tick印 | 採用 | §2.4/T6。既存印は観測のまま、別request admission primitiveを新設 |
| m16 古いgap頻度 | C3で保留 | §2.3。3aの古いgap/日次自動取得は0。offline32窓/subrunを維持。旧4窓/6h案は今回実装しない |
| m17 他CLI/lock | 修正採用 | §1.2。readonly保持者条件、keepは停止中local、冗長flockを廃止しworker mutexに限定 |
| m18 他文書追随 | 採用 | T3。評価区間C2の版/参照更新を明記、histbugs T6はC2.2で訂正済み |

## 付録E. r2 文書とコードの食い違い13件

事実訂正は採用。12の製品選択はC3でAに裁定済み。コード/原本仕様は本改訂では編集せず、実装時に§6の対応taskで更新する。

| # | 訂正・理由・更新先 |
|---|---|
| 1 | mt5_importの404想定外docstringとbridgeのRuntimeError→404が相違。§3で分類、T4で構造化error、T3でdocstring更新 |
| 2 | syncのlock競合rc=2を撤回、現repairと同じrc=1へ。§1.2/T2 |
| 3 | writer_admissionのtick印は観測専用。§2.4/T6で別primitiveを新設 |
| 4 | next_closeは未実装。§2.5/T6へ新設・休日/DST・bridge対test |
| 5 | wizardの1銘柄/5m例とbase_interval既定1mが相違。§3/T3で主参照同開始・設定足幅の案内 |
| 6 | 評価区間C2の同期v6と版不変が相違。T3で§2.3/test14/参照表を更新 |
| 7 | histbugs T6の同期C1再改訂要求は循環。histbugs C2.2で反映済み/実装照合へ変更 |
| 8 | HealthResponseにboot idが無い。§2.4/T4で既存応答へ追加しgrace起点を識別。3b能力応答は設計しない |
| 9 | auto無効時の「要求0」と明示要求可の矛盾。§2.8で自動要求0に限定 |
| 10 | C2では6h共有枠へ統一したが、C3では古いgapの自動取得を保留。§2.3はoffline予算だけを維持 |
| 11 | 段2にctlが無い。§1.2で段3前は停止中案内 |
| 12 | live ingestも共有経路。共有一律不採用を撤回。§2.4aでA裁定済み、3a限定/3b保留。timeoutは10秒budget残量から、due keyのみ (最大3件はfableの観測値) と§2.4で訂正 |
| 13 | 現importはinstance lockを取らない。§1.2/T2で取得前lockを導入するまで、稼働中安全を保証しない |

## 付録F. C3 の未確認と作業境界 (旧版の記録)

I-2は裁定済みAであり未確認事項ではない。bridge受動計測/構造化404/boot id、短い自己計測、毎要求占有監視と自動停止、request admission/next_closeは未実装・未確認。live ingestのbudget残量timeout/due keyとtime rule期限300秒はコード読取で確認したが、fableの1 tick最大3件/要求数集計は再計測していない。占有時間・基準線・A8の確定目標・短い自己計測の具体的期間/標本数、金曜閉場直前unverifiedの実挙動と日曜開場、休日早仕舞い、最大構成のCPU/DB/WAL/停止予算は未確認。本文の確定前数値は初期設計予算であって本環境の測定値ではない。

本改訂はc2.mdとfable資料を元にc3.mdを作成しただけで、コードを編集せず、個人設定・本番DBを編集/読取していない。MT5 bridgeへ要求せず、実装test・デプロイ・起票は行っていない。秘密情報・個人設定値・口座値を記載しない。コードは事実照合のための読取だけである。

## 付録G. 保留する別チケット案 (未起票)

| 案 | 着手の契機・調査境界 |
|---|---|
| 3b: 履歴専用2台目terminal | 3aが計測で不合格、または古いgapの停止中手動修復が実際に負担となった場合に検討。今回は接続先キー `datafeed.mt5.history_executor_url` (既定null) と `bridge_url` fallback禁止だけを置く。能力契約・再login予算は詳細設計しない。独立session/資源/cold load/broker多重session許可と取引接続への影響は未確認 |
| live cacheからの履歴転記 | 3a不合格時の退路として優先度を再評価。由来・検証・完了足・time rule・訂正/衝突/epochの正本契約を別途調査する。金曜の最終足が常にcacheにあるとは保証せず、今回の同期で転記しない。二表統合も未決 |

いずれも自動fallbackではない。別案が実装・受入されるまでは、自動同期なし/停止中手動だけの状態を維持する。


## 付録H. opus r3 の採否と理由 (全19件)

採用は設計への反映であり実装/測定の完了ではない。数値は設計定数で個人設定・実測値ではない。

| 指摘 | 採否 | 理由・反映 |
|---|---|---|
| I-1 | 修正採用 | §2.3/A21/T2/T5。2開場時間ずつの確定prefixで遅れを縮める。既存keyを事前選択し正本K/N_test維持。欠けを許す尾は最大3開場時間/180格子へ改訂して再受入。Kを緩める正本変更案、閉場片を要求数から隠す案は不採用。要求数2Nを維持しHTTP区間の開場時間を定義した方が測定可能 |
| I-2 | 採用・具体化 | §1.2/2.4/3/A7〜8/A20/T4〜6。daemonが初回/boot変更で自動計測、永続停止だけ明示clear-stop再合格で解除。履歴専用完了slotを既存応答headerで受動確認。待ち支配のtimeoutは通常backoff、未確認は取得0、占有超過と混同しない。最後の任意request idだけの記録は他要求に上書きされるため不採用、専用slot保持を選択 |
| I-3 | 採用 | §1.1/2.5/A5/T6。UTC :40固定、日末上限20分+g+2wと反映期限・充足率を受入。Missionとの衝突0は主張しない |
| I-4 | 採用 | §2.4/3/A7/T4。計測/boot/error/完了記録はheaderのみ、body/detail/status維持。取得箇所helperとContextVar、__new__互換、daemon side channelと専用fetchの読取をtaskへ |
| I-5 | 採用 | §1.1/2.7/3/A18/T2〜3。規約とmarker-only v4前例を優先、評価区間C2と共有v6。rollback marker v5。旧版不変判断を撤回 |
| m1 | 採用 | §2.5/A5。next_closeの各開→閉遷移へ。12/24・12/31の平日閉場を含む |
| m2 | 採用 | §2.5/3/A5。bootと開場それぞれの実起点から5分、再接続による延長禁止 |
| m3 | 採用 | §1.2。保持者情報を持たないlockからdaemonを断定しない。instance_locked統一 |
| m4 | 採用 | §1.2/3。Dukascopyはmutation lease競合が理由。停止中のkill switch監視停止費用も案内 |
| m5 | 採用 (競合計上) | §2.4/T4。Mission、repair-time plan、check/initはadmission外と明記し基準線/timeout帰属へ含む。planへの新instance lockは今回は追加しない。完了slotで安全に次取得を抑える |
| m6 | 採用 | §2.2/2.5/T6。W_lease=0を構造とtestで示しLから除去、offlineはWriterAdmissionなし |
| m7 | 採用 | §2.5。R=2Nは主pair全体の検査尾込みrun最大要求数 |
| m8 | 採用 | §2.4a/A7。キー有無で3a接続先/数不変、3b未実装の値なし表示。未実装fallbackの空testを撤回しuserinfoを含むURL全体を出さない |
| m9 | 採用 | §1.3/2.8/3。無効/容量未受入/未対応はhealth非適用・通知なし、旧bridge更新をnextへ。永続停止を非適用へ隠さない |
| m10 | 採用 | §2.4/A7。超過応答でも全窓取得済み/正本通過の操作だけ追加要求なしで反映。未取得窓があれば反映しない |
| m11 | 採用 | §1.3/3/T3。status_history・wizard・HB next/配備順も更新予定に列挙。停止中importまたは条件付きctlへ |
| m12 | 採用 | §1.2/3。手動deferredは理由/範囲/nextをstdoutに必ず表示。HBのrc=0契約は変更しない |
| m13 | 採用 | §2.4/A8〜9。能動自己計測は通常操作として反映、同じ予算/予約帯/監視。捨て取得を作らない |
| m14 | 採用 | 冒頭/§2.1のリンクをworktree正本へ、HB側C2参照をC4へ追随するtaskをT3へ。原本自体は本改訂で未編集 |

## 付録I. r3 の文書とコードの食い違い14件

全件の事実訂正を採用。現行コードを実装済みC4とは扱わず、下記taskでコードと運用原本を揃える。本改訂では原本/コードは編集しない。

| # | 訂正と理由・反映先 |
|---|---|
| 1 | ingest timeout参照行を213〜214へ訂正。due key/残budgetの事実は維持 (§2.4) |
| 2 | list/response_modelと本文field追加の不整合をheaderへ解消。body完全一致互換 (§2.4/A7/T4) |
| 3 | detail文字列比較を維持し固定errorはheaderへ (§2.4/3/T4) |
| 4 | Mission子processの取得はtick admission外。caller別基準線・完了slotで帰属 (§2.4/T4) |
| 5 | lock保持者を断定できないためdaemon_runningを撤回、instance_locked (§1.2/T2) |
| 6 | Dukascopyはbridgeを使わずmutation lease競合で拒否 (§1.2/T2) |
| 7 | repair-time plan/check/initの要求は残る。稼働中競合として計上し待ちtimeoutを恒久停止にしない (§2.4/T4) |
| 8 | CLAUDE.md/_migrate_three_to_fourと整合する共有v6 marker-onlyへ (§1.1/A18/T2〜3) |
| 9 | market_hoursの平日休日閉場もnext_closeの対象 (§2.5/A5/T6) |
| 10 | bootだけでなく開場の5分graceを本文へ (§2.5/3/A5) |
| 11 | HB §1.3/status_historyの稼働中import案内を停止中/ctlに改訂 (T3) |
| 12 | wizardのctl/startup案内も同じ変更、固定1銘柄/5m例を設定足幅と主/参照へ (T3) |
| 13 | HB §3の配備順はschema用再起動→停止→manual import→再起動へ (T3) |
| 14 | HB正本リンクへ、HB §2.3のhistsync C2参照はC4へ更新予定 (T3) |

## 付録J. C4 の未確認と検証境界 (旧版の記録)

経路A・既定有効・正本の比較/rcは確定。180格子/3開場時間の尾、24開場時間までのwarm性、閉場跨ぎHTTP形状、受動header計測の負荷、履歴slot/bootの完了追跡、daemonの有限終了、A8占有上限/p95/遅延目標、自己計測の具体的期間/標本数/試行上限は未実装・未確認。T5で測定して規則版を固定するまで3aを有効化しない。疎なtickによる300秒失効、日曜2観測、早仕舞い、尾の欠損分布、最大構成のCPU/DB/WAL/checkpointも未確認。

本改訂で行うのはC3/r3/HB正本・関連規約/限定ソースの読取とC4文書の作成・静的照合だけ。コード/他spec/個人設定/DBは編集せず、本番DBは読まず、MT5 bridgeへ要求しない。実装test・実測・デプロイ・起票・commitは行わない。秘密情報・個人設定値・口座値を記載しない。付録の採用/taskは実装済みという意味ではない。

## 付録K. terra C4閉鎖確認の裁定とC5検証境界

全4件を設計上採用。実装完了・実機での閉鎖を意味しない。

| 指摘 | 裁定・反映 |
|---|---|
| Important 1 | §2.3/§3/A21。要求前の既存key不足は要求0、応答後は正本の遮断条件優先。K不足と1%境界をfixture指定 |
| Important 2 | §1.3/§2.4/§3/A8。計測不能理由と停止中手動後の再実行を表示。cursor初期化からcampaign再開・合格時解除まで受入化し、無条件回復は保証しない |
| Minor 1 | T3に既存の具体的更新先を維持しC5参照へ。両正本の同時更新・独立導入順・v5/v6互換testを補足。原本の追随自体は未実施 |
| Minor 2 | §1.3/§2.4/§3/A7/T4。同bootでslot none継続時の運用表示・保守復旧と実装可能性を検証対象化。安全な簡易代替は未確認 |

C5で実施したのはC4・terra閉鎖確認・histbugs C2.2 §2.3・評価区間C2の該当箇所の読取と文書差分の静的照合のみ。付録Jの性能・配備・自己計測の未確認事項は継続する。コード・他spec・個人設定・DBは編集せず、DB読取・MT5 bridge要求・実装test・実測・デプロイ・起票・commitは行っていない。秘密情報・個人設定値・口座値は記載しない。
