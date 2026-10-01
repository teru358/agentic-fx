# [quiet-market-restricted-state] 設計書 v1.1

作成日: 2026-10-01。対象は、日次 rollover などで primary の 1m 足が短時間欠ける間の新規 entry 停止境界である。既存の outage/backfill 設計を置換するのではなく、flat 時の短い 1m stall に `restricted` を追加し、既存の `ready` / `degraded` / `backfilling`、backfill、replay の安全規則を補足する。

## 要点

1. 建玉・未約定注文が無い（flat）状態で通常の 1m stall を検出したら、global `restricted` に即時遷移し、全新規リスクを止める。restricted の absolute deadline を過ぎたら `degraded` にする。exposure がある stall、`empty`、`failed` は restricted を経ず即時 `degraded` とする。
2. `restricted` は新規 entry の猶予ではない。restricted に入った tick から OPEN、limit fill、cron/signal mission、signal producer を停止する。CLOSE/CANCEL と既存の安全側処理は継続する。
3. state、epoch、restricted 時刻、gap、exposure 判定は一つの SQLite transaction で確定する。activity は commit 後の best-effort 記録であり、DB state が正本である。
4. deadline 超過は `restricted` の全遷移セルで最優先である。閉場中に deadline は経過し、日曜の最初の healthy tick でも先に `degraded` へ遷移する。
5. flat + `auto_resume_when_flat=true` の deadline 昇格は human confirmation を立てず、健全 3 tick で既存の自動復帰により ready へ戻る。
6. 1m 欠落の品質 gate は signal producer に限る。判断 mission の read tool が 1m を直接読む、又は 1m まで cache fallback して集約する場合は、穴を含む実データをそのまま返し、欠落だけでは mission を止めない。

## 背景と実測

この設計の目的は 1m 足を連続させることではない。データを信用できない間に新規リスクを増やさず、古い価格を現在値として既存建玉に適用しないことである。synthetic candle、forward-fill、無 tick を価格不変と見なす処理は採らない。

|観測範囲|実測|設計への含意|
|---|---|---|
|USDJPY、60 日、開場中 1m|96 欠落分、54 run。全て 21 時台 UTC。最長は日曜開場直後の 18 分。|時刻帯 allow-list は採らない。30 分の bounded restricted は短い rollover gap を許容するが、無期限にはしない。|
|2026-09-29|欠落は 21:15、21:19–20、21:26–27 UTC。|回復のたびに ready へ戻る別 episode であり、一つの deadline 超過例ではない。|
|2026-09-13|21:00 の後に 18 分欠落。|日曜開場にも通常の stall 規則を適用する。|
|2026-09-30|20:45–21:50 UTC の 66 本に欠落なし。|低 tick 数、quote age、spread を outage 判定の入力に加えない。|

用語を次に固定する。

- **stall**: primary source の対象 `(pair, 1m)` watermark `w` に対し、`now > next_bar_confirmation(w, 1m, grace)` となる liveness 不確実性。
- **observed gap**: 後続の確定足を得た後、開場中の期待 1m 開始時刻が欠けていると確認した履歴品質の事実。過去へ遡って episode を開始しない。
- **empty**: 当 tick に対象 key の fetch は成功したが、確定バーが 0 本だったこと。
- **failed**: 当 tick の対象 key の取得が失敗したこと。`deferred` と not-attempted は healthy ではなく、healthy streak を進めない。
- **exposure**: OPEN、PENDING_FILL、SUBMITTING、unknown、closing/cancelling を含む、未解消の注文状態が一件以上あること。

## 守るべきものと現行の手段

|守るもの|現行の手段|本設計で維持・追加するもの|
|---|---|---|
|新規リスクを増やさない|non-ready state の scheduler/healthcheck/open gate|`restricted` も non-ready とし、五相 OPEN の最終 gate を補完する。|
|古い 1m 足で SL/TP を判定しない|fresh でない bar を走査しない|restricted を既存建玉保護の代替にしない。exposure 時は即 degraded にする。|
|episode を復旧・監査できる|global state/epoch と pair × interval × epoch gap|state/gap は原子的に確定し、activity は従属する記録とする。|
|信頼できない価格を作らない|native bar と既存の freshness 規則|穴の補完、価格不変、過去の order/mission の巻戻しをしない。|

## 候補比較と選定

|候補|停止と既存建玉の安全性|誤停止|結論|
|---|---|---|---|
|flat の stall のみ上限付き restricted|exposure、empty、failed は即 degraded。最初の stall から entry は止まる。|短い rollover gap で不要な degraded を避けられる。|**採用**|
|5m/判断足などで stall を判定|1m 停止を遅く検出し、1m freshness の保護を置換できない。|少ない。|不採用|
|無 tick を価格不変として走査|配信停止と区別できず、架空価格で判定する。|最小。|不採用|
|keyed outbox による activity 配送保証|現行 activity sink は書込失敗を呼出元へ返さず、key/dedupe 契約もない。|該当なし。|不採用|

## 動作仕様

### 状態

監視集合 `M` は、設定対象 pair ごとの RequirementRegistry hard 1m key である。primary source の watermark だけを live outage 判定に使う。global の `datafeed_outage_state` は次を永続化する。

- `state`: `ready`、`restricted`、`degraded`
- `epoch`
- `restricted_since`、`restricted_deadline_at`
- `ready_streak`
- `pending_human_confirmation`

`datafeed_outage_gap` は `(pair, interval, epoch)` ごとに保存する。最初の flat stall で epoch を一度だけ増やし、同 tick に stalled だった key の最小 expected を restricted 起点とする。`restricted_deadline_at = restricted_since + flat_stall_max_sec` とし、後発 stall は deadline を延長しない。restricted から degraded への昇格も epoch は増やさず、gap start を保存する。

tick 冒頭で UTC `now` を一度だけ採取し、observe、state transaction、scheduler、healthcheck へ同じ時刻を渡す。閉場中は観測、ready streak、遷移を進めない。ただし deadline は凍結しない。

### 遷移表

`healthy` は、全 `M` が non-stalled、当 tick に empty / failed / deferred / not-attempted がなく、当 episode で non-empty success を確認済みであることをいう。`H3` は healthy が連続 3 tick、`E` は exposure ありを表す。

**共通最優先条件:** 現在 state が `restricted` なら、すべてのセルで `now > restricted_deadline_at → degraded` を healthy/stalled/empty/failed の評価より先に適用する。

|現在|条件|次 state|epoch / pending|
|---|---|---|---|
|`ready`|flat + stalled|`restricted`|epoch+1、pending=0|
|`ready`|E、又は empty / failed|`degraded`|epoch+1。pending=1 は exposure、auto=false、又は既存人手確認要件がある場合だけ。|
|`ready`|healthy|`ready`|不変|
|`restricted`|`now > deadline`|`degraded`|flat + auto=true + 人手確認要件なしは pending=0、それ以外は pending=1|
|`restricted`|E、empty、failed|`degraded`|pending は上記規則で決める|
|`restricted`|flat + healthy H3 + auto=true + pending=0|`ready`|restricted 時刻を NULL|
|`restricted`|flat + healthy H3 + auto=false、又は pending=1|`degraded`|pending=1|
|`restricted`|その他|`restricted`|deadline は不変|
|`degraded`|flat + healthy H3 + auto=true + pending=0|`ready`|restricted 時刻を NULL|
|`degraded`|その他|`degraded`|不変|

flat の deadline 昇格、又は flat の empty/failed による direct degraded で `pending_human_confirmation=1` を立てないのは、`auto_resume_when_flat=true` かつ既存の人手確認要件が無いときだけである。`pending=1` は `auto_resume_when_flat=false`、exposure、又は既存仕様の人手確認要件で立てる。`pending=1` は automatic ready を妨げ、受理された `data resume` transaction だけが 0 にできる。再起動後は state/deadline/pending を保持し、in-memory healthy 証拠を捨てるため H3 を改めて数える。

### 発注と mission の gate

`restricted` と `degraded` の間は、limit fill、cron cursor の前進、cron/signal mission の start・claim、signal producer、新規 OPEN を停止する。CLOSE/CANCEL、day-close/retry-close、account/reconcile、fresh な足だけで行う既存建玉の SL/TP 走査は停止しない。

五相 OPEN は、開始時だけでなく次の各点で `ready` を再検査する。

1. core lock 内で intent を記録した直後、`signals.consume()` より前。
2. `open_from_snapshot()` の入口。
3. broker submit の直前。

non-ready なら OPEN intent は `risk_gate` 拒否し、submit しない。claim 済み OPEN signal は consume しない。fresh なら既存 `max_requeue` を適用して requeue し、上限到達又は鮮度切れなら abandoned にする。restricted 解除後は既存 `signal_min_interval_min` の rate window を経過して初めて再 claim できる。待機中に鮮度切れなら abandoned にする。CLOSE/CANCEL signal は restricted 中も consume・実行する。

### signal の扱い

品質 gate の結論は **signal producer に限定**する。producer は 15m/1h native 行を使い、4h/1d は closed 1h を集約する。1m 欠落専用の producer quality gate は追加しない。1h/15m producer が native 行だけを query することを回帰試験で固定する。

判断 mission の read tool は別扱いである。`decision_timeframes=[1m]`、context の 1m、又は 4h/1d cache fallback が 1m まで落ちて集約する場合、穴を含む実データをそのまま見せる。OHLCV を捏造・forward-fill せず、穴の存在だけでは mission を停止しない。1m fallback 集約が部分足を作り得る問題は `backtest-aggregates-partial-minute-bars` の範囲であり、本設計の範囲外である。

後続 bar で observed gap が判明したら `bar_gap_observed` を記録してよい。gap は監査情報であり、足の挿入、価格不変の根拠、過去の state/order/mission の巻戻しには使わない。

### 閉場と開場

閉場中は state、epoch、ready streak を変えない。deadline は wall-clock absolute である。金曜の flat restricted が閉場中に期限を過ぎた場合、日曜最初の tick が healthy でも `restricted → degraded` を先に行う。flat + auto=true + pending=0 なら、以後 3 healthy tick で ready に自動復帰する。

この週末 episode の activity 列は `datafeed_restricted` → `datafeed_degraded` → `datafeed_recovered_auto` である。停止時間は金曜の restricted 開始から日曜の H3 tick までとし、activity の成功有無ではなく DB state の遷移時刻で測る。

### 再起動

再起動後も state、epoch、restricted 時刻、deadline、pending、gap を DB から復元する。transaction 途中の crash では state と gap が同時に rollback される。commit 後 activity 書込み前の crash では activity が欠け得るが、新しい scheduler は確定済み state で動く。in-memory healthy streak は復元しない。

### 複数取引対象

同 tick に複数 key が stall した場合、最小 expected を restricted 起点にする。後発 stall では deadline を延長しない。一つでも設定対象 key が stall / empty / failed なら、全設定 pair の新規 entry を止める。ready への復帰には全 `M` の healthy を要する。設定外 symbol は対象外である。

## 設定キーと既定値

`config/settings.yaml.example` に、既存の `datafeed.outage` 設定を次の内容へ拡張する。ファイル本体は本設計では変更しない。

```yaml
datafeed:
  outage:
    flat_stall_max_sec: 1800  # flat の 1m stall を restricted に留める最大秒。0 で restricted を無効化し即 degraded
    ready_confirm_ticks: 3    # restricted/degraded から ready に必要な連続 healthy tick 数
    auto_resume_when_flat: true  # flat episode は連続 healthy tick で自動 ready。false は human confirmation を要する
```

`flat_stall_max_sec=0` は bounded restricted を無効にし、flat stall も既存相当の即 degraded とする。`ready_confirm_ticks` は 1 以上とする。

## activity

使用する event は `datafeed_restricted`、`datafeed_degraded`、`datafeed_recovered_auto`、`data_resume_accepted`、`bar_gap_observed` とする。遷移 event には epoch、reason、stalled key、expected、deadline を含める。

activity は state transaction の commit 後に best-effort で書く。activity sink は成功確認、idempotency key、sink 側 dedupe を提供しないため、keyed outbox、再配送、配送保証は採用しない。遷移を確定した tick に限り書くことで通常の state-machine 実行で同一遷移を重複記録しない。commit 後 activity 前の crash、及び activity の fail-soft write failure では記録が欠け得る。DB state と gap が監査・復旧の正本である。

## 不変条件

|ID|不変条件|
|---|---|
|IV-1|`state != ready` の間、新規 OPEN、limit fill、cron/signal mission、signal producer は実行しない。|
|IV-2|OPEN の authority は mission 開始時ではなく、consume 前、snapshot commit-core、broker submit 直前の `ready` 再検査である。|
|IV-3|exposure のある stall/empty/failed、及び flat の empty/failed は同 tick で degraded になる。|
|IV-4|`restricted` の全セルで deadline 超過が最優先である。deadline は後発 stall や閉場で延長・凍結しない。|
|IV-5|exposure 判定、state、epoch、restricted 時刻、gap は分割 commit しない。activity の失敗・欠落は確定済み state を戻さない。|
|IV-6|flat deadline 昇格は auto=true かつ人手確認要件なしなら pending を立てず、H3 の自動復帰を妨げない。|
|IV-7|判断 mission の read tool は穴のある native 1m 列を加工せず返し、欠落だけでは mission を止めない。|
|IV-8|observed gap は監査記録であり、synthetic candle、forward-fill、価格不変の根拠にしない。|
|IV-9|outage による OPEN signal の requeue も既存 `max_requeue` と鮮度規則を適用する。CLOSE/CANCEL は restricted 中も実行する。|

## 受入条件

fixture は実装時に `tests/fixtures/outage/` に置く。バー時刻は実データから固定し、poll 時刻、ingest report、exposure、crash 注入は test が作る。

|ファイル|銘柄|足|期間 (UTC)|本数|sha256 先頭|
|---|---|---|---|---:|---|
|`usdjpy-1m-20260929-rollover.json`|USDJPY|1m|2026-09-29 20:50–21:50|56|`b202be44e4b4e618`|
|`usdjpy-1m-20260913-sunday-open.json`|USDJPY|1m|2026-09-13 21:00–22:05|38|`f1f4217d0eb007a5`|
|`usdjpy-1m-20260930-no-gap.json`|USDJPY|1m|2026-09-30 20:45–21:50|66|`61bae06d85e8daea`|

|ID|条件|検証|
|---|---|---|
|AC-1|9/29 の実バーは三つの別 episode を作る。|episode 1: 21:17 restricted（deadline 21:46:30）→ 21:18/19/20 healthy → 21:20 ready。episode 2: 21:21 restricted（21:50:30）→ 21:22 は stalled のまま (21:19・21:20 の 2 本欠落) → 21:23/24/25 healthy → 21:25 ready。episode 3: 21:28 restricted（21:57:30）→ 21:29 は stalled のまま (21:26・21:27 の 2 本欠落) → 21:30/31/32 healthy → 21:32 ready。確定足は close の 1 分後の poll で見える前提 (60 秒 poll、grace 30 秒)。いずれも deadline 非到達、degraded なし。|
|AC-2|deadline 超過は fixture に依存しない合成 case で検証する。|watermark と restricted 起点を固定し、`now == deadline` は restricted、`now > deadline` は healthy でも degraded を assert。**合成** case と明記する。|
|AC-3|9/13 の日曜開場は三つの別 episode を作り、どれも bounded restricted から回復する。|episode 1: 21:03 restricted（deadline 21:32:30）→ 21:21/22/23 healthy → 21:23 ready。episode 2: 21:24 restricted（21:53:30）→ 21:35 ready。episode 3: 21:43 restricted（22:12:30）→ 21:49 ready。いずれも deadline 非到達、degraded なし。exposure 一件なら 21:03 同 tick で degraded。|
|AC-4|9/30 は遷移なし。|66 本の連続列で、低 tick 数を注入しても restricted/degraded/activity が発生しない。|
|AC-5|deadline 優先と週末自動復帰。|金曜 flat restricted が週末に期限切れ後、日曜最初の healthy tick でも degraded。flat+auto=true+pending=0 ではその後 H3 で ready。DB state から停止時間を測り、activity 列 `datafeed_restricted` → `datafeed_degraded` → `datafeed_recovered_auto` を確認する。|
|AC-6|empty/failed の全セル。|ready/restricted/degraded と exposure の有無の組で empty/failed を投入し、ready/restricted は同 tick degraded、degraded は維持。deferred/not-attempted が healthy streak を進めないことも assert。|
|AC-7|state/gap の原子性と activity の独立性。|state/gap SQL の途中で crash を注入し、双方が rollback され食い違わないことを assert。activity 書込みが例外又は fail-soft failure でも state は確定済みで scheduler が新 state で動くことを assert。commit 後 activity 前の crash では activity 欠落を許容する。|
|AC-8|五相 OPEN と outage requeue。|restricted 中の claimed OPEN は consume=0、submit=0、intent は `risk_gate` reject。fresh は既存 `max_requeue` を適用し requeue、上限到達又は stale は abandoned。restricted 解除 → `signal_min_interval_min` 経過 → fresh なら claim、先に stale なら abandoned。CLOSE/CANCEL は restricted 中も実行。|
|AC-9|判断 mission の 1m input は非加工。|`decision_timeframes=[1m]` と 1m context で、read tool が gap を含む native 1m 列を加工せず返すことを assert。4h/1d cache fallback が 1m まで落ちる列でも、欠落だけで mission を停止しないことを assert。|
|AC-10|producer の品質 gate は producer に限る。|1h/15m producer が native 1h/15m 行だけを query し、1m row を読まないことを assert。|
|AC-11|複数 key。|同 tick の複数 stall は最小 expected を restricted 起点にし、後発 stall が deadline を延長しない。一 key の異常で global restricted/degraded、設定外 symbol は無関係。|

## 範囲外と関連チケット

- `plugin-sees-live-spread`
- `risk-gate-rr-uses-configured-spread`
- `backtest-aggregates-partial-minute-bars`（1m fallback 集約の部分足）
- `outage-stalled-on-broker-daily-rollover-gap`
- quote age、spread、tick 数、時間帯 allow-list、銘柄別 local state/episode、bridge 改修、WebSocket 化、synthetic candle、forward-fill。

## task 分割と影響ファイル

|Task|単独で入る成果|主な影響ファイル|
|---|---|---|
|T1: state と fail-closed gate|`restricted`、absolute deadline、pending lifecycle、state/gap の一 transaction、commit 後 activity、prepare 例外の failed report、五相 OPEN の再検査と requeue。|`src/agentic_fx/config.py`、`config/settings.yaml.example`、`src/agentic_fx/datafeed/outage.py`、`src/agentic_fx/service.py`、`src/agentic_fx/store/db.py`、`src/agentic_fx/core/executor.py`、`src/agentic_fx/loops/trade_loop.py`、`src/agentic_fx/core/scheduler.py`、tests|
|T2: observed gap と data-input 契約|gap の監査、fixture pin、native producer 契約、1m read tool の非加工契約。|`src/agentic_fx/datafeed/health.py`、`src/agentic_fx/datafeed/outage.py`、`src/agentic_fx/plugin/signal_producer.py`、`src/agentic_fx/backtest/timeframes.py`、`src/agentic_fx/tools/market_tools.py`、tests/fixtures|
|T3: 運用・結合検証|週末、requeue rate window、fault injection、activity 欠落を含む acceptance。|state/command 層、tests/fixtures|

T1 は単独で、新規 OPEN の scheduler、同期、五相のすべてを non-ready で fail closed にする。T2/T3 は観測性と検証を加えるが、この停止境界を緩めない。

## 残るリスク

- flat な真の対象別停止は degraded 表示まで最大 30 分と polling 位相を要する。ただし entry は最初の stall から停止する。
- global AND のため、一対象の長時間問題が全対象の新規 entry を止める。
- PaperBroker 側の実時間 SL/TP は保証しない。exposure 時は即 degraded として可視化する。
- commit 後 activity 前の crash、又は activity sink の fail-soft failure で activity が欠け得る。DB state/gap が正本である。

## 既存 spec との関係

`2026-09-24-outage-stop-and-backfill-design.md` の次の節を、本設計が補足又は上書きする。

|既存節|本設計との関係|
|---|---|
|§3.1 データ状態|`ready` / `degraded` / `backfilling` を残し、flat の短い 1m stall 用に `restricted` を追加する。state/gap の atomic commit と activity の best-effort 性を明確化する。|
|§3.2 停止の境界表|`restricted` を non-ready gate に加え、五相 OPEN の consume 前・snapshot 入口・submit 直前を authority とする。|
|§3.4 休場との区別|閉場中に state は進めないが deadline は wall-clock で進む規則を補足する。|
|§4 不変条件|本設計の IV-1〜IV-9 を追加し、deadline 優先、activity の非保証、1m read tool の非加工を明記する。|
|§5 受入条件|本設計の AC-1〜AC-11 を追加し、実 fixture、合成 deadline、週末復帰、requeue を固定する。|
|§10/§11 影響ファイル・task|T1〜T3 の境界を追加する。既存の backfill/replay task は維持する。|

backfill、replay、既存建玉の連続性検査、`backfilling` 中の drain/handoff は既存 spec の責務として残る。本設計はそれらを再設計しない。

## 変更履歴

|日付|版|変更|理由|commit|
|---|---|---|---|---|
|2026-10-01|v1.0|flat 1m stall の bounded `restricted`、deadline 優先、state/gap transaction、best-effort activity、1m read-tool 契約、fixture acceptance を spec として確定。|設計レビュー 2 周の裁定。状態名は `ready` / `restricted` / `degraded` (形容詞 1 語で揃える、ユーザー裁定)。既定の上限 30 分・3 tick・取得失敗と empty は即 degraded も同裁定|`(this)`|
|2026-10-01|v1.1|受入条件 AC-1 の 9/29 の時刻を訂正 (episode 2 の ready は 21:25、episode 3 は 21:32)。AC-3 の 9/13 は 18 連続の後にも欠落があり 3 episode (21:03→21:23、21:24→21:35、21:43→21:49)。連続 2 本欠落の分だけ stalled が 1 tick 長い。|実装時に fixture を停滞式で再生して判明。仕様の動作は変えていない|`(this)`|
