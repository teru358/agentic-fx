# [decision-timeframe-config] 束 B 設計書 v1.0

作成日: 2026-09-22 JST。対象は A2-1 の後に導入する判断足（トレード足）の設定化である。これは実装計画ではなく、B-1 の受入可能な最小変更と、その後の B-2 を分ける設計である。根拠の裁定は `tmp/design-a2/RESUME.md` の「ユーザー裁定 18:45 / 19:05」、確定足・primary の前提は `docs/superpowers/specs/2026-09-21-closed-bars-and-required-window-design.md` §3.3–3.5 である。

## 要点

1. 新しい唯一の判断足キーは `datafeed.decision_timeframes: [1h]` とし、B-1 ではちょうど一要素だけを許す。既定も `[1h]` である。二要素以上は `[multi-decision-timeframes]` が未実装であることを理由に起動拒否する。
2. cron mission は固定 60 秒 tick を維持し、判断足の新しい**確定** watermark があるときだけ実行する。due は watermark のみで判定し、`timedelta(hours=1)` による eligibility は廃止する。確定は A2-1b が ingest commit 後に DB へ記録した watermark を読む。
3. B-1 は cron mission、設定検証、primary/health の判断足導出、LLM に渡す省略時既定足までに限る。signal 起動は独立のまま変更しない。context 足、catch-up の全面足対応、mission 予算の最適化、複数判断足は B-2 以降である。
4. 既存 settings を無変更で読んだときの正規化結果は `[1h]` であり、既定足と「1 確定足に 1 回」の意味は維持する。ただし wall-clock eligibility を廃止するため、実時刻上の最短 1 時間間隔は維持しない（互換差）。

## 1. 前提の検証

|現物で確認した事実|根拠|含意|
|---|---|---|
|判断 mission の next-run は `timedelta(hours=1)` で固定されている。|`src/agentic_fx/core/scheduler.py:297-299`|B-1 の置換点。分単位の tick 自体を 15 分へ変える変更ではない。|
|`schedule.trade_interval_min` は設定モデルの定義だけで、runtime の参照箇所がない。|`src/agentic_fx/config.py:290`、`rg -n 'trade_interval_min' src`（定義以外 0 件）|二つの周期の真実を残さず、廃止または導出専用にする。|
|`datafeed.primary_intervals` は healthcheck と validator だけが読む。|`src/agentic_fx/datafeed/price_provider.py:355-363`、`src/agentic_fx/config.py:178-188`|B-1 では判断足から導出する。明示値を許すなら一致検査が必要である。|
|strategy plugin はそれぞれ `config.yaml` の `timeframe` を持ち、producer は閉じた bucket のときだけ評価する。|`src/agentic_fx/plugin/signal_producer.py:205-237`|strategy の足と判断足は現在も別概念であり、B-1 で同一視してはならない。|
|signal 起動は scheduler の cron mission と独立した `signal_due_fn` 経由である。|`src/agentic_fx/core/scheduler.py` の `signal_due_fn` 呼出し（要実装時に call graph を再確認）|signal の周期を判断足に結合するのは B-2 の判断である。|
|health は primary interval の bar と鮮度を調べる。A2-1b の registry は health consumer を「判断足 1 本 + 鮮度」として登録する。|`src/agentic_fx/datafeed/price_provider.py:355-363`; A2-1 spec §3.4|B-1 の `primary_intervals` は独立した利用者入力ではない。|
|起動時 catch-up、horizon (`day`/`swing`)、金曜 cutoff、日次処理は既存 scheduler の別分岐である。|`src/agentic_fx/core/scheduler.py`（catch-up / horizon / Friday・daily 分岐）|これらが「毎時」を暗黙に仮定していないかは B-2 で個別に時刻モデル化する。B-1 は既存の起動時・日次・cutoff の意味を変えない。|
|LLM market tool の `timeframe` は現在は必須引数である。|`src/agentic_fx/tools/market_tools.py:47-56,99-115`|B-1 は schema の required から外して省略可能にし、未指定時の既定を判断足にして prompt にも明示する。|

最後の三行は「B-1 に含めないこと」の確認であり、15m 化しても日次締め・金曜締めを 15 分ごとに実行するという意味にはしない。`signal_due_fn` の正確な起点と日次分岐の行番号は、B-1 実装時の call graph spike で再確認する必要がある（この下書き時点では上記以外を推測しない）。指揮者の read-only 実測（2026-09-22、missions 2026-09-15〜）では mission 所要時間は local qwen3-coder が p50 1.3 分/p90・max 1.8 分、Claude Sonnet が p50 0.7 分/p90・max 1.3 分、local qwen3.8 が p50 2.1 分/p90 3.1 分/max 3.2 分であり、signal 起動 mission は 0 件だった。B1-0 ではこの実測を補う bar 側の集計を行う。

## 2. B-1 と B-2 の切り分け

|項目|区分|理由|
|---|---|---|
|`decision_timeframes` の list 化、既定 `[1h]`、一要素検証|B-1|15m を選べる最小の公開設定契約。list 形で将来を塞がない。|
|cron mission の due を判断足 watermark だけへ置換（60 秒 tick は維持）|B-1|現在の 1h eligibility を直接置換し、足境界駆動へ変えるため。|
|新しい確定判断 bar を cursor で一度だけ消費する due 判定|B-1|15m で同じ閉じた bar に複数 mission を出さない安全条件。|
|`trade_interval_min` の廃止または導出専用化|B-1|未配線でも将来の二重設定を防ぐ。|
|`primary_intervals` を判断足へ導出し、明示時は不一致拒否|B-1|A2-1b の health registry と整合させる。|
|配備 strategy の `timeframe` と判断足の不一致診断|B-1|15m 判断で 1h strategy を無自覚に使う事故を早く出す。|
|prompt と `get_ohlcv` の未指定足|B-1|LLM の意思決定根拠を選択した判断足へそろえる最小変更。|
|`context_timeframes`|B-2|参考情報の足は発注判断足と異なるため、必要本数・prompt 表現・tool payload の設計が別途必要。|
|signal 起動と cron mission の関係|B-1|signal 起動は制限せず現行の独立経路・`signal_min_interval_min` を維持する。cron と signal が同じ足で並ぶことは許容し、既存 supervisor が非並行で直列化する。全面的な関係設計は B-2。|
|起動時 catch-up を足ごとの「未消費確定 bar」へ一般化|B-2|再起動時に何本まで再実行するか、horizon/cutoff と合わせて裁定が必要。B-1 は従来 catch-up を維持する。|
|15m mission の wall-clock/LLM 予算、重複抑止、timeout 方針|B-2|local LLM の 2–3 分は収まり得るが、Claude/Codex の所要時間では次 due と重なり得る。|
|初回設定ウィザードの選択肢|B-2|設定 schema が確定してから UX を接続する。|
|orders/signals の timeframe tag を集合前提へ拡張|範囲外: `[multi-decision-timeframes]`|B-1 は単一値であり、データモデルの集合化は不要。|
|二本以上の decision timeframes の実行・優先・予算|範囲外: `[multi-decision-timeframes]`|list の形だけを先行させ、意味付けは将来束で行う。|
|`horizon` の再定義|B-2|`day`/`swing` は保有期限の概念で、判断足との換算規則を安易に導入しない。|

## 3. B-1 の設計

### 3.1 設定モデルと正規化

新しい `datafeed` 所属キーを次で正規化する。

```yaml
datafeed:
  decision_timeframes: [1h]  # default
```

許容 interval は datafeed が native/derived として扱える canonical interval だけで、文字列は既存 interval parser を通して canonical 化する。空 list は「判断足なし」ではなく起動拒否である。要素数が 2 以上なら、次のように値を含む可読なエラーにする。

```text
decision_timeframes は現在ちょうど1個必要です（指定: [15m, 1h]）。複数判断足は [multi-decision-timeframes] で対応予定です。
```

1 個のときの runtime 値を `decision_timeframe`（単数）として一箇所で提供する。public YAML を list に保ちつつ、B-1 の実装が `[0]` を散在させないためである。将来束ではこの accessor を集合 API に置換または併設する。

Settings root の `mode="before"` validator は raw input を見て、(1) `datafeed.decision_timeframes` の raw presence を判定し、(2) legacy `schedule.trade_interval_min` を判断足へ変換し、(3) raw `datafeed.primary_intervals` と導出値の一致を判定し、(4) 型検証へ渡す順で処理する。これにより既存の `primary_intervals ⊆ intervals` validator より先に導出を済ませる。Pydantic default 済みの値で raw 未記載を推測しない。

`schedule.trade_interval_min` は削除しない互換キーとする。raw にあれば判断足の分数との一致を要求し、不一致は起動拒否、一致なら一回だけ deprecation warning を出す。既存 `settings.yaml` と example は無改変で起動する。これは runtime 周期をこのキーで決める契約ではない。

`datafeed.primary_intervals` は runtime では `set(decision_timeframes)` から導出する。raw YAML に値があれば、canonical set が導出値と同じ場合だけ受理して一回だけ deprecation warning、不一致なら起動拒否する。順序差は等価、重複は設定エラーとする。これにより health が 1h を待つのに mission は 15m という無自覚な状態を作らない。B-1 の前提は A2-1b 完了後であり、health と due は ingest commit 後の DB reader だけを使う。A2-1a の取得経路へ B-1 を単独 backport しない。

### 3.2 cron の due 契約

各 scheduler tick は現行どおり 60 秒で、`now_utc` を一度取得する。判定対象は、同 tick の ingest commit 後に DB reader が返す pair ごとの `decision_timeframe` 最新確定 watermark である。

watermark の同一性は canonical UTC の `bar_time` のみで定義する。grace は closed bar の可視性判定であり、watermark の識別子には含めない。`latest_closed_watermark(pair, decision_interval) = None` の pair は `due=false` とする。

```text
due(pair) = latest_closed_watermark(pair, decision_interval) > cursor(pair, decision_interval)
due = any(due(pair) for pair in configured_pairs)
```

cursor は scheduler プロセス内の `(pair, decision_interval)` ごとの canonical UTC `bar_time` とする。いずれかの pair が未消費なら mission は全 pair を扱う。`try_submit` が受理した時だけ、`None` でない最新確定 watermark を持つ pair の cursor をその時点の値へ進める。`latest=None` の pair の cursor は更新しないため、後日最初の非 `None` watermark が現れたら due となる。実行中で `try_submit` が `None` のときは進めず次 tick で再試行する。その間に複数足が進めば、受理時に最新 watermark へ coalesce し中間足の mission は走らない。未更新 pair は同じ足を再び見るが、これは現行の全 pair mission と同じである。

再起動時は、開場中なら最新確定 watermark で一回だけ起動して baseline とし、閉場中なら baseline だけして起動しない。過去の bar 全件を追いかけない。

`timedelta(hours=1)` による eligibility は廃止し、60 秒 polling の実行権限は上の watermark due だけが握る。tick の遅延や DB commit 遅延により watermark が未観測なら、次 tick で同じ watermark を判定する。複数回の poll が同じ watermark に対して mission を発火してはならない。

この設計は「新しい確定足が出た後だけ」を守る。15m の boundary ちょうどに起動する保証はしない。ingest が DB へ commit した後の最初の tick が最短である。「前回から 1 時間」は廃止され、mission 時刻が足の境界駆動に変わることが互換差である。

### 3.3 strategy 足の不一致

採用案は **起動拒否ではなく一回の明示 warning** である。plugin strategy の `timeframe` は signal producer の評価 bucket を表す既存設定であり、判断足とは別概念である（§1）。全 strategy を 15m に変えずに、15m ごとの LLM mission が 1h signal を参照する構成は合理的である。拒否すると既存配備の移行を不必要に止める。

ただし warning は plugin 名、strategy timeframe、decision timeframe を含め、`signal は strategy timeframe（例: 1h）でしか更新されず、get_signals は status を問わず 24h 分を返すため、consumed / abandoned の行は再提案しない` と明記する。例: `strategy timeframe 1h differs from decision timeframe 15m (plugin=...); signals update only on strategy timeframe and historical consumed/abandoned rows may be shown repeatedly; do not propose them again`。同一なら出さない。B-2 で context/signal の役割と freshness を決めた後、必要なら strict mode を検討する。これは「一致しない strategy を判断足に自動変換する」機能ではない。

### 3.4 LLM へ渡す最小情報

mission prompt の固定 metadata に `decision_timeframe: <runtime の判断足の展開値 (既定 1h、設定 15m なら 15m)>` と、`signal は strategy の足（例: 1h）でしか更新されず、get_signals は status を問わず 24h 分を返す。consumed / abandoned の行は再提案しない` を追加する。市場 tool の `get_ohlcv` と `get_indicators` は `timeframe` を省略可能にし、schema の required から外す。未指定時は runtime `decision_timeframe` を使い、明示した interval は尊重する。tool 応答にも実際に使った interval を返す。これにより prompt の表示と tool の暗黙値がずれない。

これは context 足の自動添付ではない。LLM が 1h を必要とするなら B-1 でも tool 引数で明示可能だが、context の収集量・保持本数・prompt の表現は B-2 で決める。

## 4. B-2 の設計（概要）

B-2 は B-1 の `decision_timeframes` list と単数 accessor を入力として、次を設計する。

* `context_timeframes` を別キーとして導入し、判断/発注の足ではないことを prompt・tool payload・registry reason に明記する。primary interval/必要本数/保持を context にどう要求するかを決める。
* `signal_due_fn`、producer bucket、cron mission の三者を時系列図で整理する。signal は strategy 足の閉鎖で、mission は判断足の閉鎖であり、同一化するか購読するかを裁定する。
* restart catch-up を `(last consumed decision bar, latest closed decision bar)` で定義し、最大遡及数、金曜 cutoff、day/swing horizon と交差させる。
* 15m の非重複実行を保証する。mission 一本当たりの timeout、queue/skip/coalesce 方針、provider 別 LLM の最大実行時間を測定し、過負荷時の可観測性を加える。
* 初回設定ウィザード、orders/signals/activity の timeframe provenance を集合を許せる shape にする。ただし単一判断足の B-1 では schema を集合化しない。

B-1 は public key を list にし、`primary_intervals` の比較を set としているため、B-2 や `[multi-decision-timeframes]` が追加足を表現できる余地を残す。一方、runtime mission cursor、order/signal tags、予算は B-1 で単数のままであり、誤って「複数対応済み」と見せない。

## 5. 不変条件

1. runtime の判断足は B-1 では必ず一つであり、0 または 2 個以上では scheduler を開始しない。
2. 同じ `(process lifetime, pair, decision_interval, watermark)` に対して cron mission は高々一回である。いずれかの pair が未消費なら一つの mission を全 pair に対して起動する。
3. mission は同 tick の ingest commit 後に DB から読んだ確定 watermark を根拠に開始し、形成中 bar や外部取得結果を根拠にしない。
4. timer の起床回数と mission 実行回数を同一視しない。watermark の同一性は canonical UTC `bar_time` のみで、grace は可視性判定である。60 秒 tick の遅延/再 poll は同じ確定 watermark を重複消費しない。`latest=None` の pair は due=false かつ cursor 不変であり、cursor は `try_submit` 受理時だけ `None` でない pair の最新 watermark へ進める。
5. health/primary の判断 interval は正規化後の `decision_timeframes` と一致する。別の明示設定で暗黙に上書きしない。
6. strategy timeframe は判断 timeframe と独立であり、不一致を自動変換しない。診断はする。
7. legacy settings が `decision_timeframes` を持たなければ、正規化値 `[1h]`、周期上限 1h、LLM tool 既定 1h となる。
8. signal 起動は cron watermark due と独立であり、B-1 では `signal_min_interval_min` を含め変更しない。同じ足で cron と signal が並ぶことを許容し、supervisor が直列化する。
9. 再起動時は開場中の最新確定 watermark を一回だけ起動して baseline とし、閉場中は baseline のみとする。
10. day/swing、金曜 cutoff、日次処理の意味は B-1 単独では変えない。

## 6. AC と殺すテスト

すべて scheduler 時刻は fake clock にし、実 time / sleep / 外部 provider に依存しない。closed bar reader は `(bar_time, interval, grace)` を返す fake にする。

|ID|AC|担当 task|正例|逆変異（殺すべき実装）|
|---|---|---|---|---|
|AC-B1-01|設定の既定|B1-1|キーなしで `[1h]` に正規化され、interval は 60 分。|既定を `15m` に変える、または scalar `1h` を list と同じ扱いで黙認する。|
|AC-B1-02|単一要素制約|B1-1|`[15m]` で起動可能。|`[]` や `[15m, 1h]` を許す。後者のエラーに指定値と `[multi-decision-timeframes]` が無い。|
|AC-B1-03|legacy 互換|B1-1|既存 settings/example の `trade_interval_min: 60` と `primary_intervals:[1h]` は無改変で起動し、各 warning は一回。不一致 legacy 値は拒否。|legacy key を unknown-key として拒否する、または legacy 値で runtime 周期を変える。|
|AC-B1-04|primary 導出|B1-1,B1-4|`decision_timeframes:[15m]` で health の required interval は 15m。|`primary_intervals:[1h]` を併存して無言で受理する。等価 `[15m]` は warning 付き受理（互換を採る場合）。|
|AC-B1-05|watermark due|B1-2|A2-1b DB fixture の新しい確定 watermark で 1 回。`timedelta(hours=1)` がなくても同一 watermark は 100 tick で 1 回。|wall-clock eligibility を残す、または timer tick ごとに mission を出す。|
|AC-B1-06|grace と watermark|B1-2|grace 前は旧 watermark、grace 到達後は canonical UTC `bar_time` の新 watermark で 1 回だけ due、その後 100 tick は不発火。|grace 状態を watermark の識別子に含め、同じ bar を再消費する。|
|AC-B1-07|pair 集約|B1-2|二 pair の片方だけ新 watermark なら一 mission を全 pair に出し、受理時に全 pair cursor を各最新 watermark へ進める。|先頭 pair だけを読む、または未更新 pair を理由に全体を止める。|
|AC-B1-08|rollover 回復|B1-2|`latest=None` の pair は due=false、受理時も cursor 不変。後日最初の非 `None` watermark が現れたら due=true。|`None > cursor` を比較する、または `None` を cursor に書いて回復後の due を落とす。|
|AC-B1-09|再起動|B1-2|開場中再起動は最新確定 watermark で 1 回だけ、閉場・週末再起動は金曜最終足を起動せず baseline のみ、同時刻再起動も一回だけ。|cold cursor で週末の古い足を起動する、または開場中の初回起動を失う。|
|AC-B1-10|closed→open 遷移|B1-2|金曜閉場中に起動・baselineし、開場だけでは 0 回。最初の判断足 commit 直後に 1 回、その後同 watermark では 0 回。|金曜足を開場時に誤発火する、または再baselineして最初の判断足を落とす。|
|AC-B1-11|busy/coalesce|B1-2|`try_submit` が `None` なら cursor 不変で次 tick 再試行。busy 中に 2 足進めば、受理時は最新へ coalesce して中間 mission は 0。handoff 拒否時も cursor 不変。|submit 前に cursor を進める、または中間足を全て enqueue する。|
|AC-B1-12|signal 割り込み|B1-2|同じ判断足で独立 signal mission が起動しても cron watermark 契約を変えず、supervisor で直列化する。|signal 起動を判断足で制限する、または signal により cron cursor を進める。|
|AC-B1-13|strategy mismatch|B1-3|15m decision + 1h strategy は warning を一回出し、起動は継続。|不一致で strategy timeframe を書換える、または無理由に起動拒否する。|
|AC-B1-14|LLM defaults|B1-3|`timeframe` を省略した `get_ohlcv`/indicator と prompt metadata は runtime の判断足 (既定 `[1h]` の配備では 1h、`[15m]` では 15m の双方を確認)、ToolDef schema の required に timeframe がない。明示 1h は 1h。|tool schema だけ required のまま、tool は 1h のまま、または明示引数を上書きする。|
|AC-B1-15|既存分岐|B1-2|fake clock で日次/Friday の既存テスト結果が 1h default と同じ。|15m の cron に伴い daily 処理を四回実行する。|

追加の統合 AC は A2-1b の DB fixture に限定する。ingest が最新 closed 15m を commit した後にのみ DB watermark により health と mission がともに成立することを確認する。「既存 catch-up と同じ」の AC は置かない。対象の cron catch-up 実装は存在しないためである。

## 7. 設定キーと移行

`config/settings.yaml.example` は既存の `schedule.trade_interval_min` と `datafeed.primary_intervals` を無改変で残し、`datafeed.decision_timeframes: [1h]` と「現在は一要素のみ」のコメントを追加する。legacy 二キーは受理時に一回だけ deprecation warning を出す互換経路であり、runtime 値は `decision_timeframes` から導出する。

既存の実 `settings.yaml` はこの束で書換えない。キーが無いことを raw input の段階で検出して default `[1h]` を投入する。Pydantic の default 済み値だけで legacy/new の併存を判定しないこと（A2-1 primary 移行と同じ注意）を守る。

移行 matrix:

|raw `decision_timeframes`|raw legacy `trade_interval_min`|raw `primary_intervals`|結果|
|---|---|---|---|
|未記載|未記載|未記載|`[1h]`、watermark 駆動（60 秒 tick）。|
|`[15m]`|未記載|未記載|15m judgment/health、15m tool default。|
|`[15m]`|あり|任意|legacy 分数が 15 と一致すれば warning 付き受理、不一致は起動拒否。|
|`[15m]`|未記載|`[1h]`|不一致として起動拒否。|
|`[15m]`|未記載|`[15m]`|deprecation warning のうえ受理。|

互換差は mission の時刻である。旧 `trade_interval_min` は前回 handoff からの wall-clock eligibility を表していたが、B-1 では 60 秒 tick 上で新しい確定 watermark のみが cron due を決める。そのため mission は足の境界・ingest commit 駆動となり、「前回から 1 時間」を保証しない。legacy `trade_interval_min` は一致検査と warning だけに用い、due を変更しない。

## 8. 裁定済み事項

1. `schedule.trade_interval_min` は削除せず、raw の分数が判断足と一致する場合に一回の deprecation warning 付きで受理する。不一致は起動拒否する。
2. `datafeed.primary_intervals` の等値明示は一回の deprecation warning 付きで受理し、不一致は起動拒否する。
3. cursor は `try_submit` の handoff 受理時だけ全 pair の最新 watermark へ進める。実行成功を待たず、busy/拒否時は進めない。
4. B-1 の `decision_timeframe` は prompt/tool metadata までに止め、order/activity の永続 tag 集合化は `[multi-decision-timeframes]` に回す。
5. cron due は watermark のみ、scheduler tick は 60 秒のまま、signal 起動は独立のまま変更しない。

## 9. 影響ファイルと呼び出し元

想定変更先は `src/agentic_fx/config.py`、`config/settings.yaml.example`、`src/agentic_fx/core/scheduler.py`、`src/agentic_fx/service.py` の ingest→scheduler 配線、判断足 watermark を読む DB store reader、`src/agentic_fx/datafeed/price_provider.py`、`src/agentic_fx/tools/market_tools.py`、prompt 組立の実ファイル、および config/scheduler/store/provider/tool の対応テストである。strategy plugin の設定ファイルは B-1 で自動変更しない。

実装 spike で用いる探索コマンド（この文書の根拠を更新するため）は次である。

```bash
rg -n -C 4 'timedelta\(hours=1\)|trade_interval_min|decision_timeframes|primary_intervals|signal_due_fn|catch.?up|horizon|Friday|friday|daily' src/agentic_fx config
rg -n -C 4 'get_ohlcv|get_indicators|interval|timeframe|prompt' src/agentic_fx/tools src/agentic_fx
rg -n -C 4 'primary_intervals|health|fresh' src/agentic_fx/datafeed/price_provider.py src/agentic_fx/config.py
rg -n -C 4 'timeframe|bucket|due' src/agentic_fx/plugin/signal_producer.py src/agentic_fx/core/scheduler.py
```

特に置換の起点は `scheduler.py:297-299`、未配線 legacy key は `config.py:290`、primary health/validator はそれぞれ `price_provider.py:355-363` / `config.py:178-188` である。A2-1b を前提にする registry/ingest の接続点は A2-1 spec §3.3–3.5 を正とする。ingest の優先順（`1m → 判断足 → その他`）と、成功可能な判断足 key を有限 tick 内に probe する契約は **A2-1 spec v1.1 の契約に依存**する。

## 10. task 分割（B-1 のみ）

1. **B1-0: read-only spike / call graph 固定** — config raw-key 正規化、`try_submit` の受理/拒否、daily/Friday の接続、prompt builder、A2-1b の service ingest→scheduler 配線と DB store reader を file:line と最小 fake で確定する。15m bar の gap、rollover、週末 watermark を集計する。A2-1 spec v1.1 の契約に依存する ingest 優先順 `1m → 判断足 → その他` と、成功可能な判断足 key を有限 tick 内に probe する fair queue の方式・上限を確定する。外向き通信なし。
2. **B1-1: config contract** — `decision_timeframes`、単一要素 validator、単数 accessor、`primary_intervals` の導出/不一致、legacy key の採用裁定、example と unit tests。
3. **B1-2: scheduler due** — 1h 固定置換、closed-bar cursor、fake-clock boundary/duplicate/delay tests。catch-up/daily/cutoff の既存テストを回帰対象にする。
4. **B1-3: LLM/runtime integration** — prompt metadata、market tool default、strategy mismatch warning、tool/prompt/config integration tests。
5. **B1-4: A2-1b 接続検証** — 15m の registry health、ingest 完了前後、primary interval 導出を integration fake で検証する。A2-1b 未実装なら契約テストを先に置き、実接続は依存実装後に実行する。

**AC と task の対応:** AC-B1-01 (設定の既定) → B1-1 / AC-B1-02 (単一要素制約) → B1-1 / AC-B1-03 (legacy 互換) → B1-1 / AC-B1-04 (primary 導出) → B1-1,B1-4 / AC-B1-05 (watermark due) → B1-2 / AC-B1-06 (grace と watermark) → B1-2 / AC-B1-07 (pair 集約) → B1-2 / AC-B1-08 (rollover 回復) → B1-2 / AC-B1-09 (再起動) → B1-2 / AC-B1-10 (closed→open 遷移) → B1-2 / AC-B1-11 (busy/coalesce) → B1-2 / AC-B1-12 (signal 割り込み) → B1-2 / AC-B1-13 (strategy mismatch) → B1-3 / AC-B1-14 (LLM defaults) → B1-3 / AC-B1-15 (既存分岐) → B1-2

各 task は先行 task の API を使い、B-2 の context/signal/catch-up 再設計や集合 tag を混ぜない。B1-0 で A2-1b の ingest commit 後に DB から pair ごとの `latest closed decision bar` watermark を読めないと判明した場合は、B1-2 を開始せず A2-1b の不足として設計を戻す。

## 変更履歴

|日付|版|変更|理由|
|---|---|---|---|
|2026-09-22|v0.1|初稿。束 B を B-1（単一判断足の設定化）と B-2（周辺時刻モデル）に分離。|15m 試行を早く可能にしつつ、複数判断足・context・signal の意味を先取りしないため。|
|2026-09-22|v0.2|r1 指揮者裁定 12 件を全件反映。watermark due、pair cursor、legacy 互換、A2-1b DB reader、restart/coalesce、signal 注記、tool timeframe 省略可能化を確定。|裁定済み契約へ設計を一致させるため。|
|2026-09-22|v0.3|r2 裁定 5 件を全件反映。既定 1h の wall-clock 互換差、canonical UTC `bar_time` watermark、`None` pair の回復、A2-1 v1.1 の ingest fairness 依存、closed→open AC を確定。|watermark の一意消費、欠損からの回復、判断足の進捗保証を明文化するため。|
|2026-09-22|v0.4|r3 (codex terra、C0/I2) を反映。prompt metadata の判断足を runtime の展開値に、AC に ID (`AC-B1-01`〜`15`) と担当 task を付番し §10 に対応表。|受入確認の周で残った Important 2 件|
|2026-09-22|v1.0|spec として清書 (内容は下書き v0.4 と同一)。B-1 = いま入れる最小 (判断足 1 個の設定化 + 確定足駆動の cron)、B-2 = 残り|codex 設計レビュー 3 周 (sol C4/I7 → sol C0/I5 → terra C0/I2) で Critical 0。ユーザー承認待ち|
