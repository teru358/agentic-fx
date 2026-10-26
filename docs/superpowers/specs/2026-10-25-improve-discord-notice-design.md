# 改善 mission の開始・終了通知 (Discord) 設計書

**版:** v0.8 (T1b 実装レビューの反映: outbox を tick ごとにも走らせ emit 失敗を固定語で記録、report の `failed` 列と複数段の読み方、DTO を `floor.stages[]` に)

**対象:** `improve-start-and-result-to-discord` (追加要望 2 点を含む)

**関連チケット:** `improve-run-summary-for-humans` (仮説・指標・床との差の要約は本件の範囲外)、`bot-event-notices-are-raw-log-lines` (是正済。bot の event 文面の型をそのまま使う)

**前提:** 操作 API (`2026-10-04-ops-api-design.md`)、同梱 bot / notify (`2026-10-09-afx-ops-design.md` §3.2・§3.4)、取引通知 (`2026-10-20-trade-notices-design.md`、新 event code を足す手順の先例)、承認の判断材料 (`2026-10-19-approval-content-design.md`、親が記録した facts と agent の申告を分けて見せる型)。


## 0. 目的と非目標

目的: 改善 mission の開始と終了を、人が読める日本語で Discord に出す。終了通知には、結果の種別ごとに「何が起きたか」を足す。agent が提出をやめた理由 (agent の申告)、システムが床で見送った理由 (落ちた段の数値)、失敗の理由語。

非目標:
- 仮説・使った指標・次回への申し送りの要約 (`improve-run-summary-for-humans`)。
- 通知経路の新設。既存の daemon → event → ops-bot → Discord に乗る。
- 通知を根拠にした daemon 側の判断。通知は出力専用で、daemon は読み戻さない。

### 0.1 前提の再検討 (チケットの案との差)

1. **「開始通知に課題 (backlog の idea) を載せる」は成立しない。** 課題は mission の終わりに agent が出力して初めて backlog 行へ紐づく (`_select_and_bind`)。開始時点で決まっているのは、起動契機・backend/model・mission id だけ。開始通知は課題を載せない。
2. **「job id」は載せない。** job id は操作 API の手動 improve にしか無く、週次の mission には無い。mission id を共通の識別子にする (手動の job 結果にも mission id は出る)。
3. **自由記述を載せる経路は event にできない。** event の値は `[A-Za-z0-9_.:+-]` の 64 字以内に固定されている (`_SAFE_VALUE`)。日本語の観察文も数値表も入らない。かつ event は DB に残り、notify の webhook (外部送信) でも読まれる。したがって **event は「起きた」という事実と固定語だけ運び、本文の材料は bot が API で取りに行く** (承認の plugin 名補完と同じ型)。ただし `_SAFE_VALUE` は `pf:7.7777` のような値も通すので、固定語であることは event の専用検査で保証する (§4.1)。
4. **開始通知の要否。** 週次は 1 周あたり少数、手動は本人が起動する。開始通知の価値は「無人の週次が動き出した」ことの把握と、終了通知が来ないときの切り分け (開始あり・終了なし = 異常) に限られる。ユーザー依頼なので実装するが、bot 側で `results` に絞れる設定を付ける (§7 裁定 1)。
5. 手動 improve は既存の `job_state_changed` (job_kind=improve) も出す。既定で job 成功は非表示、失敗は表示される。手動 mission の例外終了では「job 失敗」と「改善失敗」の 2 通になり得る。許容する (抑止しない)。週次は改善通知の 1 通だけ。

### 0.2 確定事項 (既存コードの確認済み。実装で再議論しない)

1. 週次の 1 wave の本数は `M = min(settings.improve.parallel, 空き slot 数)` (`parallel` は 1〜4、現設定は 1)。現設定では 1 周 1 本。
2. 床未達以外の `gate_failed` も同じ report outbox を通るので、正常なら `improvement_runs.report_path` が埋まる。書込み失敗・publish 失敗では `report_state='failed'` で path が空になる。このとき数値行は省く (§2.2)。
3. 手動 mission の例外終了で `job_state_changed` (失敗) と `improve_run_changed` が重なるのは許容する (§0.1-5)。bot 側で抑止しない。
4. 新 endpoint は `mutating=False` の読み取りで、`approval_detail` / `events_after` と同じく `ops_requests` の監査行を書かない。回帰 test で固定する。
5. `missions.trigger` は improve では NULL で、意味は変えない。手動 / 週次は `prepare()` が `slot_key` の有無 (None = 手動) で決め、Tx-0 で `improvement_runs.trigger` に記録する (§4.3)。再起動回収はこの列から復元する (`improve_wave_slots.mission_id` は回収と pre-ready 失敗で NULL に戻るので頼らない)。
6. 再起動回収 (`missions.recover_interrupted`) は存在する。running の mission を `interrupted` にし、unfinished の run と slot も回収するが、現行は activity も event も出さず、返すのは件数だけ。したがって通知が欠ける。§4.3 で回収が snapshot を書き、§4.4 の起動時の取りこぼし回収 (snapshot を outbox とする) が通知を出す。

## 1. 用語

- **mission**: 改善 loop の 1 回の実行 (`missions.id`)。手動 (`afx ctl improve`) と週次 (wave の slot) がある。
- **結果の 5 種** (`result_code`):
  - `submitted`: 承認依頼を出した (`approval_requested`)。
  - `observation`: **agent の申告**で提出をやめた (`mission_observation`。文は agent が書いた)。
  - `gate_failed`: **システムの判定**で見送った (床未達 `unprofitable`、取引数不足、評価不能など)。
  - `failed`: mission が失敗した (runner の失敗、`output_invalid`、sandbox 利用不能、commit 例外、再起動による中断など)。
  - `report`: report を公開して終えた (`report_published`、重複の見送り)。
- **reason_code**: 固定語の allowlist (§4.2 の表) の値だけ。agent や例外の文面、数値を含まない。
- **終端 snapshot** (`improve_run_notices`): mission を終端する transaction と同時に書く、通知用の不変な 1 行 (§4.3)。通知の正本。
- **親専有レポート**: `data/improve_reports/improve-<日付>-<mission>.md`。holdout の数値を含み、agent には見せない (遮断 8)。

## 2. 利用者から見た動き

mission の開始時に 1 通、終了時に 1 通、同じ channel に投稿される (mention は付けない)。取引通知と同じ「1 行目に要点、続く行に補足」の形。

### 2.1 開始

```text
[改善] 開始 #2043 (手動)
backend codex / gpt-5.6-luna
```

`(手動)` は `afx ctl improve`、`(週次)` は wave の slot。backend / model は daemon の mission 行から取り、bot が表示用に escape する。課題は載せない (§0.1)。開始通知は `prepare()` が完全に成功した mission にだけ出る。mission 行ができた後に準備で失敗した mission は、開始通知なしで終了通知だけが出る (§4.5)。

### 2.2 終了 (結果ごと)

構成: 1 行目は見出し (下の表)、続いて本文行 (表にあるものだけ)、最終行は `所要 <m分ss秒> / tool <n> 回`。以下の例は bot が送る content そのもので、変数部分 (観察文・reason_code・pair・backend / model・plugin 名) は `escape_discord` 後の文字列 (`_` は `\_` になり、Discord 上では `_` と表示される)。固定の語と数字は escape しない。

**所要:** 終端 snapshot の `created_at` から mission 行の `started_at` を引いた秒数 (daemon が整数秒 `elapsed_sec` にして返す)。snapshot は mission の終端 tx で書かれ、commit() の中で走る gate (in_sample / holdout の backtest) を含むので、「開始から結果が確定するまで」を表す。mission 行の `finished_at` は commit の開始時刻なので使わない (gate の分だけ短く出る)。小数秒は四捨五入 (0.5 は切り上げ)、`<m>分<ss>秒` (秒は 2 桁)、60 分以上は `<h>時間<mm>分<ss>秒`。`interrupted` (再起動回収) の `created_at` は再起動の時刻で停止していた時間を含むので、所要は省く。時刻が欠けるか終了が開始より前のときも省く。省いたときの最終行は `tool <n> 回` だけ、tool も出せなければ `所要 -`。

**tool 回数** は snapshot の `tool_calls`。null (記録が無い終端) のときは `tool -`。

**見出しと本文行の表** (bot が `(result_code, reason_code)` と `report_state` だけで引く。変数 `{id}` は mission id、`{reason}` は reason_code を escape したもの):

| result_code | reason_code | 見出し | 本文行 |
|---|---|---|---|
| submitted | null | `[改善] 候補を提出 #{id}` | `plugin <name> (<kind>) / 承認 #<approval_id>` (承認情報が取れないときは省く) |
| observation | null | `[改善] 提出なし (agent の申告) #{id}` | `agent の申告: <観察文>`。観察文が null か空白だけなら `agent の申告: (記載なし)` |
| observation | not_submitted | `[改善] 提出なし #{id}` | `agent の申告なし (重複または系統の見送り)` |
| observation | demoted_recovered | `[改善] 提出なし (降格) #{id}` | `resume 回収経由のため report は公開せず降格しました` |
| observation | risk_gate_unsupported | `[改善] 提出なし (risk_gate は未対応) #{id}` | なし |
| observation | duplicate_logic | `[改善] 見送り (既存候補と同じロジック) #{id}` | なし |
| observation | duplicate_metrics | `[改善] 見送り (既存候補と同じ成績) #{id}` | なし |
| observation | family_redirect | `[改善] 見送り (既存系統への誘導) #{id}` | なし |
| gate_failed | unprofitable | `[改善] 床未達で見送り #{id} (unprofitable)` | 数値行 (下記。取れなければ省く) |
| report | null | `[改善] レポートを公開 #{id}` | なし |
| report | duplicate_skipped | `[改善] レポートを公開 #{id} (duplicate_skipped)` | なし |
| report かつ `report_state=failed` | 任意 | `[改善] レポートの公開に失敗 #{id}` | なし |
| 上記以外の既知の result_code | 任意 | `[改善] <基本語> #{id} (<reason>)`。reason が null なら括弧なし。基本語: submitted=候補を提出、observation=提出なし、gate_failed=見送り、failed=失敗、report=レポートを公開 | なし |
| 未知の result_code | 任意 | `[改善] 終了 #{id}` | なし |

表の「上記以外」の行が、語の追加を後から許す口になる (daemon が新しい reason_code を足しても、afx-ops の表に無ければ括弧内にそのまま出る。§4.7)。`report_state=failed` は GET の応答の値で、snapshot には無い (§4.3)。

**観察文** (`observation / null` のとき) は `cut_escaped(value, 300)` (bot の既存関数) と同じ規則で出す。1 行化 → escape → escape 後の文字数で 300 字以内 (切るときは escape の単位を壊さず、末尾の「…」を含めて 300 字以内)。この文は agent が書いたもので、システムの判定ではないことを見出しの語で示す。`not_submitted` は agent の申告を出さない (last_result が中立な固定文言に倒される既存規則と揃える)。

**gate_failed、床未達の数値** (親専有レポートから。§4.6): 落ちた段 (`failed_stage:` 行の段名。表示は `in-sample` / `holdout`) と、その段の表のうち `failed_pairs:` に載った pair だけを 1 行ずつ出す。pair は最大 3 本、超えたら `ほか n 本`。形は `<段名>: <pair> pf <x:.2f> avg_r <x:+.2f> 取引 <n>` で、`evaluable=false` の行には末尾に ` (評価不能)`。pf と avg_r は Python の `format` の結果をそのまま使い (2 進の最近接偶数丸め)、`-0.00` は pf では `0.00`、avg_r では `+0.00` に正規化する。値が null のときは `pf -` / `avg_r -`。落ちた段が複数あるとき (`failed_stage: in_sample, holdout`) は、レポートの表の `failed` 列で段ごとの失敗 pair を復元し (§4.6)、`floor.stages[]` の順に段ごとの行を出す。#2043 のように pf が 1 を超えても評価不能で落ちることがあるので、「評価不能」を落ちた理由として必ず並べる。

in_sample の行を出す根拠: in_sample の成績は agent が `run_backtest` で既に見ている値で、in_sample で落ちた候補は holdout を回さないため遮断 8 の外にある。通った段の数値は出さない。

レポートが無い・`report_state` が `published` でない・読めない・形式が想定外のとき (§4.6 の検査のどれかに反する) は、数値行と「落ちた段」を省き、見出しだけにする。床未達以外の `gate_failed` (取引数不足、`loader_rejected` など) も見出しだけ。reason_code は固定語で、`insufficient_trades:16` のような数は持たない。

以下は 2026-10-25 の実 mission での例。所要は snapshot の `created_at` が DB にまだ無いため、終端 activity 行の時刻 (秒精度、`created_at` の代用として fixture に書く値) から規則で算出: #2040 は 06:47:26.891 → 06:52:16 で 289.109 秒、#2043 は 07:07:14.852 → 07:11:36 で 261.148 秒、#2044 は 07:14:55.598 → 07:21:36 で 400.402 秒、#2045 は 07:27:41.543 → 07:35:09 で 447.457 秒、#2046 は 07:56:53.528 → 08:03:39 で 405.472 秒。

**observation** (#2040、agent の申告):

```text
[改善] 提出なし (agent の申告) #2040
agent の申告: session\_ranges によるロンドン・NY開始足のレンジ外ブレイク後回帰を実装し、固定出口と短縮出口でUSDJPY 1hを検証した。どちらも不採算判定となり、提出しなかった。次回は時間帯回帰以外の条件、または別の市場仮説を優先する。
所要 4分49秒 / tool 11 回
```

**gate_failed、床未達** (#2043、#2045):

```text
[改善] 床未達で見送り #2043 (unprofitable)
落ちた段: holdout
holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)
所要 4分21秒 / tool 10 回

[改善] 床未達で見送り #2045 (unprofitable)
落ちた段: holdout
holdout: USDJPY pf 0.51 avg_r -0.28 取引 24 (評価不能)
所要 7分27秒 / tool 14 回
```

in_sample だけで落ちたとき (合成。AC-2):

```text
[改善] 床未達で見送り #2051 (unprofitable)
落ちた段: in-sample
in-sample: USDJPY pf 0.80 avg_r -0.10 取引 40
所要 3分05秒 / tool 12 回
```

**gate_failed、その他** (#2044):

```text
[改善] 見送り #2044 (insufficient\_trades)
所要 6分40秒 / tool 13 回
```

**failed** (#2046。実際はディスク quota 超過による commit 失敗):

```text
[改善] 失敗 #2046 (commit\_crashed)
所要 6分45秒 / tool -
```

例外の文面 (`Disk quota exceeded` など) は出さない。理由語だけ。#2046 の実データは tool 回数が DB にも activity にも残っていないので `tool -`。この実装以降の commit 例外では supervisor が `result.tool_calls` を補償に渡すので、回数が出る (§4.3)。

**submitted** (例。実例は承認 0 件のため構成):

```text
[改善] 候補を提出 #2050
plugin <name> (strategy) / 承認 #26
所要 5分30秒 / tool 14 回
```

承認の中身は既存の承認 message が担う。ここは重ねない。**report** は `[改善] レポートを公開 #<id>` (重複の見送りは `(duplicate_skipped)` 付き) に所要を添える (数値なし)。

**interrupted** (合成。所要を省き、再起動回収は tool 回数も持たないので最終行は `所要 -`):

```text
[改善] 失敗 #2052 (interrupted)
所要 -
```

### 2.3 設定

- bot の `ops.toml` に `improve_notices = "all" | "results" | "off"` を足す (既定 `all`、`results` は終了のみ)。
- 初回の正常 poll は取引通知と同じく、配備前の event を配送しない。

### 2.4 詳細を取得できないとき

詳細の取得は GET 1 回で、**3 秒は要求全体の monotonic deadline** (接続・送信・受信・本文読込の合計)。404 / 5xx / 接続失敗 / deadline 超過 / 応答の検査違反のいずれでも、投稿は止めず次の全文を出す (承認の plugin 名補完と同じ型)。event の `trigger` / `result_code` / `reason_code` / `tool_calls` だけから作る。

- 開始: 1 行目だけ。`[改善] 開始 #<id> (手動|週次)`。
- 終了 (5 結果とも): 1 行目は §2.2 の見出しの表を event の `result_code` / `reason_code` だけで引いたもの (`report_state` が分からないので report は「レポートを公開」のまま)、`tool_calls` が非 null なら 2 行目 `tool <n> 回`、最終行に `(詳細を取得できませんでした)`。観察文・数値・所要・plugin 名・backend は出さない。
  - 例 (observation): `[改善] 提出なし (agent の申告) #2040` / `tool 11 回` / `(詳細を取得できませんでした)`。

この全文は AC-1 の完全一致 test の対象 (5 結果 + 開始 × 取得失敗)。

## 3. 秘密と遮断

通知は人間だけに届く経路である。agent への戻り経路を持たないことを、データの流れで示す。

```text
mission 終端 tx ──(daemon, 同一 tx)──▶ improve_run_notices (終端 snapshot、不変)
              ──(commit 後, supervisor)──▶ ops_events: improve_run_changed
                              fields = state / trigger / result_code / reason_code / tool_calls
                              (専用検査を通る固定語と整数だけ。観察文・数値・URL は入らない)
bot ──events.read──▶ event を受ける
bot ──GET /v1/improve/runs/{id}──▶ daemon が snapshot + DB + 親専有レポートから組み立てて返す (APPROVALS_DETAIL scope)
bot ──escape・切詰め──▶ Discord に投稿。本文は worker のローカル値で、投稿後に捨てる
```

要点:

1. **holdout の数値は Discord にだけ出る。** activity の reason、backlog の `last_result`、`idea`、次回プロンプト、終端 snapshot には、従来どおり・新規とも流さない。`_finalize_gate_failed` の `last_result` / activity 行は変更しない (固定語と閾値 3 値だけ)。数値は event の fields にも入れない (専用検査が整数 1 項目 `tool_calls` 以外の数字を通さない)。event は notify の webhook でも読まれ得るため。数値は GET のたびに親専有レポートから読む。
2. **API の応答は、承認詳細が既に返している情報と同じ水準。** `approval_detail` は `in_sample` / `holdout` と agent の申告を APPROVALS_DETAIL scope に返している。新 endpoint は同じ scope を使う (bot / web / operator / approver のみ、notify は読めない)。agent 側の鍵はなく、mission の sandbox から daemon へ到達する経路は無い。
3. **agent 由来の文字列は 1 項目 (観察文) だけ。** snapshot の `observation_text` (保存時に `safe_text` で URL・鍵様の文字列を抑止済み) から取り、daemon 側でも `display_text` 相当で制御文字・改行を畳み、bot 側で `escape_discord` (整形文字、mention、コードフェンス、URL の autolink を無効化) をかけ、`cut_escaped(value, 300)` で切る。plugin 名は loader の文法 (`^[a-z][a-z0-9_]{0,63}$`) を通ったものだけ。agent 由来の URL・鍵・口座は、保存時と表示時の二重の抑止で通さない。
4. **mention は付けない。** improve 通知の `EventPlan` は `allow_mentions=False` (既存の型。設定済みの mention を付けず、送信に `discord.AllowedMentions.none()` を渡す)。本文由来の mention も escape で無効化する。
5. **失敗の理由は固定語。** `result.reason` や例外の文面 (OS のエラー文など) は通知にも snapshot にも出さない。
6. **終端 snapshot は agent から読めない。** snapshot 表は daemon 親プロセスの DB にあり、mission の sandbox・prompt builder・RPC handler・staging のどれからも参照されない。AC-3 で静的検査 (これらの module が表名を参照しない) と canary の両方で確かめる。snapshot の本文は mission の終端時に確定し、後から backlog の reopen や note 更新で変わらない。
7. **bot が取得した本文の所有期間。** GET の応答 (観察文・数値の DTO) は bot の worker のローカル値としてだけ持ち、`EventPlan.content` の生成後は state・retry 表・例外・ログへ渡さない。永続化するのは cursor と event ID だけ。再送時は GET から取り直す。
8. **人間が通知の数値を `afx ctl policy add` や backlog に書き写すのは、人間の判断。** 書いた内容は従来の directive の規律に従う。通知はこれを防げないので、運用文書に「holdout の数値を方針に書かない」と注記する。

## 4. 配線

### 4.1 event `improve_run_changed` と専用検査

- **新 event code `improve_run_changed`** (fields: `state` / `trigger` / `result_code` / `reason_code` / `tool_calls`。ref は `improve:<mission_id>`、`improve:[0-9]{1,18}`)。`ops/events.py` の `FIELDS` と `_REF` に足す。
- **専用の意味検査** (`validate_fields` に取引 event と同じく code 別の分岐を足す。既存の `_SAFE_VALUE` は変えない):
  - `state ∈ {started, finished}`、`trigger ∈ {manual, scheduled}`。
  - `started` のとき `result_code` / `reason_code` / `tool_calls` は null。
  - `finished` のとき `result_code` は 5 値のどれか、`reason_code` は §4.2 の許可対でその `result_code` に許された値 (または null が許される対での null)。
  - `tool_calls` は 0 以上 1,000,000 以下の整数 (bool 不可) か null。
  - これ以外 (`pf:7.7777`、`:` や数字を含む reason、表に無い語、負数) は `EventSchemaError`。
- allowlist と (result_code, reason_code) の許可対の定義は daemon 側の 1 か所 (`agentic_fx/improve_notice_codes.py`。トップレベルの純定数 module で、`store/` が `loops/` を import しない層を保つ) に置き、event の検査・終端 snapshot の書込み・各 site の定数がこれを使う。**afx-ops は agentic_fx を import しない独立 package なので、この定義は共有できない。** afx-ops 側の検証は文法だけを見る (§4.7)。

### 4.2 終端 site → (result_code, reason_code) の変換表

**各 site は分岐ごとの固定定数 (`improve_notice_codes` の `NoticeCode(result_code, reason_code)` 定数) を渡す。** 例外の文面、`last_result` / reason 文字列を split・prefix 判定して作らない。仕組みは次のとおり。

- 終端 helper のうち `_finalize_gate_failed`・`_compensate_prepare_failure` は必須のキーワード引数 `notice: NoticeCode` を持つ (既定値なし)。helper の内部で通知定数を選ばず、**呼び出し側が渡す**。`_finalize_report_or_observation` は `notice` ではなく **`choice` (observation の判定結果。`last_result`・観察文・`NoticeCode` の組) だけ**を必須引数に取り、通知は `choice.notice` から導出する (notice と choice の二重引数と不一致検査は持たない)。3 呼び出しの出所は次のとおり: (1) recovered report の降格 = 分岐自身の固定定数 `demoted_recovered`、(2) 質検査からの再ルート = `notice_for_quality(verdict.code)`、(3) 通常の report / observation = `artifact.type` と、report の場合は `proposal_kind` による分岐の定数 (observation は下の selector の結果。上記のどれでもない artifact は従来どおり `risk_gate_unsupported` の観察に倒し、mission を失敗させない)。`_compensate_tx2_failure`、`_finalize_success`、`_finalize_failed_mission`、`_finalize_output_invalid`、`_finalize_gate_sandbox_unavailable`、`_finalize_loser`、`_prepare_report_if_applicable` の OSError 分岐、`compensate_commit_failure`、`recover_interrupted` は自分の分岐の定数を直接持つ。
- 判定が下位の値に由来する 2 か所は、列挙された固定語を持つ値から定数に引く。`_PluginGateVerdict` と `_QualityVerdict` に **固定語のフィールド `code`** を足し、`_run_plugin_gate` の不合格 return 10 か所と質検査の各 return がそれぞれ定数の語を渡す。内部 wrapper (`_failed_before_pytest` / `_failed_after_pytest` / `_quality_rejection` / `_quality_unavailable`) も `code` を既定値なしの引数に取り、各意味分岐が固定の `code=` を渡す。`code` から `NoticeCode` へは `improve_notice_codes` の閉じた辞書 (`notice_for_plugin_gate(code)` / `notice_for_quality(code)`) で引く。文面 (`stdout_tail`、例外の文面) は従来どおり捨てる。
- `_finalize_failed_mission` は runner の status (`timeout` / `max_turns` / `failed`) の 3 値と、既存の `is_tool_budget_abort(result.reason)` (bool を返す既存の決定的な関数) で 4 定数のどれかを選ぶ。**優先順位は `timeout` → `max_turns` → (`failed` かつ tool 予算 abort) → その他の `failed`**。選べる語は閉じた集合で、文面は使わない。tool 予算 abort の suffix (URL・鍵・数値など) は snapshot にも event にも出ない (AC-5)。
- agent が出した observation (`artifact.type=observation`) は、既存の「reason が `family_redirect:` / `duplicate` で始まるときは last_result を中立な固定文言に倒す」判定 (`_finalize_report_or_observation`) の**結果をそのまま**使う。判定は **1 つの selector** に切り出して一度だけ行い、`last_result`・保存する観察文 (`observation_text`)・`NoticeCode` (倒すなら `not_submitted`、倒さないなら null で観察文を保存) を**同じ結果から同時に返す**。呼び出し (3) はこの結果を `choice=` に渡す。この判定は last_result の漏洩防止の既存規則であり、通知側が別に文字列を解釈するのではない。**この 1 か所を除いて文字列の prefix 判定は使わない。**

| 終端 site (分岐) | result_code | reason_code |
|---|---|---|
| 承認依頼を出した (`_finalize_success` の approval 経路) | `submitted` | null |
| agent の observation (マスクされない) | `observation` | null |
| agent の observation (既存のマスク判定で倒される) | `observation` | `not_submitted` |
| report を observation に降格 (resume 回収経由、`demoted_recovered`) | `observation` | `demoted_recovered` |
| `proposal_kind=risk_gate` の report (`unsupported_in_plan10:risk_gate`) | `observation` | `risk_gate_unsupported` |
| 質検査の降格 `_QualityVerdict.code` = `duplicate_logic` / `duplicate_metrics` / `family_redirect` | `observation` | 同名 |
| 質検査の拒否 `_QualityVerdict.code` = `comparison_base_unavailable` | `gate_failed` | 同名 |
| report 公開 (`report_published`) | `report` | null |
| 敗者経路 (選択 CAS に負け、重複の自動 report) | `report` | `duplicate_skipped` |
| report の本文書込み失敗 (`_prepare_report_if_applicable` と `_finalize_loser` の OSError 分岐、2 site) | `failed` | `report_write_failed` |
| 床未達 (`unprofitable`) | `gate_failed` | `unprofitable` |
| 取引数不足 (`unevaluable_kind=insufficient_trades`) | `gate_failed` | `insufficient_trades` |
| 履歴源未設定 (`unevaluable_kind=history_source_unconfigured`) | `gate_failed` | `history_source_unconfigured` |
| 履歴が取れない (`NoHistoryError`) | `gate_failed` | `backtest_data_unavailable` |
| plugin gate: 候補 snapshot の検査不合格 | `gate_failed` | `snapshot_rejected` |
| plugin gate: `check_source` 不合格 | `gate_failed` | `source_rejected` |
| plugin gate: loader が拒否 / gate 通過後の再 discover が None (2 site) | `gate_failed` | `loader_rejected` |
| plugin gate: `plugin.py` の構文エラー | `gate_failed` | `syntax_error` |
| plugin gate: 承認済みのコピー (`noop_copy`) | `gate_failed` | `noop_copy` |
| plugin gate: 自己 test 無し | `gate_failed` | `self_test_missing` |
| plugin gate: pytest の失敗 | `gate_failed` | `pytest_failed` |
| plugin gate: 収集された test が下限未満 | `gate_failed` | `self_test_too_thin` |
| plugin gate: pytest 後に候補が書き換わった | `gate_failed` | `self_test_mutated` |
| plugin gate: 隔離不能 (Landlock 不可の RuntimeError。backlog を observation で消費する) | `gate_failed` | `gate_sandbox_unavailable` |
| 候補の同一性が取れない (`logic_hash is None`。質検査より前に終端する。質検査側の防御枝 `candidate_identity_unavailable` に届いた場合も同じ語に引く) | `gate_failed` | `unverified` |
| `assert_max_bars_within_limit` の ValueError | `gate_failed` | `max_bars_limit` |
| `kind` が indicator / strategy 以外 | `gate_failed` | `kind_unsupported` |
| `outputs_required` | `gate_failed` | `outputs_required` |
| `IndicatorResolutionError` | `gate_failed` | `indicator_unresolved` |
| runner の status が `timeout` / `max_turns` | `failed` | `timeout` / `max_turns` |
| runner の status が `failed` (tool 予算 abort を除く) | `failed` | `runner_failed` |
| tool 予算 abort (`is_tool_budget_abort`) | `failed` | `tool_budget_abort` |
| `output_invalid` | `failed` | `output_invalid` |
| strategy gate の隔離不能 (`gate_worker_sandbox_unavailable`) | `failed` | `sandbox_unavailable` |
| Tx-2 の承認作成の失敗 (`_compensate_tx2_failure`。commit() は例外を握って return するので `compensate_commit_failure` は呼ばれない) | `failed` | `approval_tx_failed` |
| `commit()` の例外後の補償 (`compensate_commit_failure`) | `failed` | `commit_crashed` |
| `runner.run()` の例外後の補償 (`compensate_launch_failure` → `_compensate_prepare_failure`) | `failed` | `launch_crashed` |
| `prepare()` の Tx-0 後の失敗 (`prepare()` → `_compensate_prepare_failure`) | `failed` | `prepare_failed` |
| 再起動時回収 (`recover_interrupted`) | `failed` | `interrupted` |

`gate_failed` の受け皿 (`gate_other`、`plugin_gate`) は置かない。表に無い site は許可対に無いので、書込みも event も通らない (実行時には誤り、test では網羅検査が落とす)。語を足すときは、`improve_notice_codes` と表を変え、設計書の変更履歴に記録する。afx-ops の表に無い新語は括弧内に出るだけなので、afx-ops の配備順に縛られない (§4.7)。

**意図の注記 (表のうち、`missions.status` と通知の `result_code` が一致しない行):**

- `report_write_failed`: mission の status は `completed` (agent の仕事は終えており、取引は止めない) で終端するが、通知の観点では成果物 (report) が残らなかったので `failed`。同じ OSError 分岐でも `_finalize_gate_failed` のものは、gate の判定が事実なので gate の定数を保つ (レポートが無いので数値行は出ない。`report_state=failed`)。
- plugin gate の隔離不能は `gate_failed/gate_sandbox_unavailable`: 環境側の失敗だが、mission は completed で終端し backlog を observation で消費する (候補は再試行されない)。strategy gate の隔離不能 (`failed/sandbox_unavailable`、backlog を open に戻す) と通知の語を分けるのは、利用者に「再試行される」と読ませないため。backlog を open に戻す変更は本件の範囲外。
- `result_code` は通知の分類であり、`missions.status` / `improvement_runs.result` とは別の語彙である。

### 4.3 終端 snapshot (通知の正本)

- **表 `improve_run_notices`**: `mission_id` (PK、`missions(id)` 参照)、`trigger` (`manual` / `scheduled`。同じ tx で `improvement_runs.trigger` から読む)、`result_code`、`reason_code`、`tool_calls` (null 可)、`observation_text` (null 可。`safe_text` 済み。**保存時にコードポイント単位で 2,000 字に切り詰める。拒否はしない** — 通知の都合で終端 tx が失敗しないため)、`approval_id` (null 可)、`created_at`。
- **追加の作法**: `store/db.py` の schema に `CREATE TABLE IF NOT EXISTS` と、UPDATE / DELETE を `RAISE(ABORT)` する `CREATE TRIGGER IF NOT EXISTS` を足す (`ops_requests` の先例)。`improvement_runs.trigger` は `_ensure_column` で足す (既存行は NULL のまま)。DB に `schema_version` は無い。test は、新表の無い旧 DB に `init_db` をかけると表と trigger ができ、既存表の行が変わらないこと。
- **Tx-0 で `improvement_runs.trigger` を記録する** (`prepare()` が `slot_key` の有無で `manual` / `scheduled`)。回収が slot を片付けた後でも、pre-ready 失敗で slot が巻き戻った後でも、trigger が復元できる。`missions.trigger` の意味は変えない。列が NULL の run (導入前に始まった mission) は `manual` として扱う (配備は mission の実行中を避けるので、まれな一回限りの誤表示として許容)。
- 書込みの前に §4.1 と同じ許可対の検査を通す (違反は `ValueError` で終端 tx が失敗し、既定値で補うことはしない。AST test が全 site の網羅を保証するので、実行時に違反するのは実装の誤りだけで、その場合は補償が `commit_crashed` で終端する)。
- **mission を終端する同じ transaction で書く。** `missions.finish_improve_mission` に必須のキーワード引数 `notice` (`NoticeCode`、`tool_calls`、`observation_text` を持つ) を足し、`WHERE status='running'` の CAS が成功した (rowcount=1) ときだけ同じ tx で snapshot を挿入する。既に終端済みなら (補償の冪等な再実行) 何も書かない。呼び出し元が `notice` を渡さないとコードが書けない (引数に既定値を付けない)。
- **holdout 数値は持たない。** 床未達の数値は GET のたびに親専有レポートから読む (§4.6)。
- **`commit()` の契約は変えない** (戻りは `None`)。supervisor は commit / 補償の後に snapshot を **別の接続で読んで** event を作る。`ImproveRunContext` (frozen) に結果を書き足さない。`tool_calls` は `commit()` が `result.tool_calls` から snapshot に書く。補償 (`compensate_commit_failure` / `compensate_launch_failure`) には supervisor が `tool_calls` を引数で渡す (取れなければ null)。
- GET は snapshot を結果の正本にする。可変な `improvement_backlog.last_result` や activity は読まない。ただし**公開の成否は `improvement_runs.report_state`** を GET が併せて返す (`prepared → published / failed` と一方向にしか変わらない値で、可変 backlog とは性質が違う)。report の snapshot は publish (rename) の前に確定するので、publish が失敗すると snapshot は `(report, …)` のままだが、bot は `report_state=failed` を見て「レポートの公開に失敗」と出す (§2.2)。

### 4.4 daemon: 発行と起動時の取りこぼし回収

- **出す人は supervisor 1 か所。** 手動 (`submit_manual`) と週次 (`_launch_slot`) の両方で、`prepare()` が完全に成功した直後に `started`、`commit()` または補償の後に `finished` を出す。pre-ready 失敗で再試行する週次は、試行ごとに (mission ごとに) 開始・終了の組を出す (終端した試行の `finished` が先、再試行の `started` が後)。
- **supervisor は自前の接続と `EventStore` で書く。** `ImproveSupervisor` は ops service より前 (`service.py` の supervisor 構築) に作られるので ops service の event store は渡せない。`db_path` から supervisor 専用の接続と `EventStore(conn, wall_clock=clock.now)` を作り、emit は supervisor 内の lock で直列化する (週次は並列で終端し得る)。終端確認の読取りも同じ lock の下で同じ接続を使う (finished ごとに別接続を開閉しない)。
- **`finished` を出す前の確認** (補償自体が失敗して mission が `running` のまま残る場合に「終了」を誤表示しないため): 同じ接続で、`missions.status != 'running'` かつ `improvement_runs.finished_at IS NOT NULL` かつ snapshot の行がある、をすべて確かめる。満たさなければ event を出さず、技術ログに固定語 `improve_notice_skipped reason=not_terminal mission=<id>` だけを書く (未終端の mission は次回起動の回収が終端し、そこで通知される)。
- **event の保存は best-effort** (`emit_best_effort`)。失敗しても mission は止まらない。保存に失敗した (`emit_best_effort` が `None` を返した) ときは技術ログに固定語 `improve_notice_emit_failed mission=<id> state=<started|finished>` を 1 行書き (数値・URL・例外文面は書かない)、その接続を閉じて捨てる (次の emit が開き直す)。再送はこの場ではせず、次項の outbox が拾う。event の組立てで `EventSchemaError` になった場合も例外は握り、技術ログに固定語 `improve_notice_skipped reason=schema` を書く。at-least-once の重複は bot が event ID で落とす。commit 後に出すので、通知の失敗や遅れが DB の終端に影響しない。
- **snapshot を outbox として扱う回収。** 同じ関数を、daemon の起動時と、scheduler の tick ごと (60 秒周期) に走らせる。起動時は **ops service を構築して event store ができた直後・listener を開く前 (bot が読み始める前、supervisor の tick 開始前)**。tick ごとの実行は ops lane の中で行い (lane が空かなければその周期は飛ばす)、`notify.improve_events=false` では何もしない。回収は、`improve_run_notices` に行があり、同じ ref の `improve_run_changed` で `json_extract(fields_json,'$.state')='finished'` の行が `ops_events` に無い mission を探す。相関条件は **`code='improve_run_changed'`・同一 `ref`・`state='finished'` の 3 条件**で固定する (started は完了扱いにしない)。`ops_events (code, ref)` の index を schema に `CREATE INDEX IF NOT EXISTS` で足し、履歴が増えても全表走査にならないようにする。`created_at` が実行時刻から 7 日以内のものだけを (窓は SQL の `created_at >= ?` で絞る)、`created_at` の昇順に `finished` として 1 件ずつ best-effort で発行する。これで次が閉じる: (a) `emit_best_effort` の INSERT 失敗、(b) commit 後・発行前の落下、(c) 回収 (`service.py` の `recover_interrupted`) から ops service 構築までの起動処理の失敗、(d) 回収と発行の間の停止。再起動回収が書いた `(failed, interrupted)` もこの経路で通知される (**回収の戻り値を持ち回さない**)。`started` は補わない。7 日の上限は、`notify.improve_events=false` の期間の分が設定を戻した途端にまとめて出るのを抑える (上限内の分は出る)。起動時の回収は supervisor の tick 開始前なので通常経路と競合しない。tick ごとの回収は、通常経路の発行の直前に走ると同じ finished を二重に出し得る。重複は at-least-once として許す (bot が event ID で落とす)。回収側は NOT EXISTS で、すでに finished がある mission を二重に出さない。
- **再起動回収** (`missions.recover_interrupted`): running の improve mission を `interrupted` にし、同じ tx で snapshot `(failed, interrupted)` を書く (`trigger` は run 行の列から読む。slot の片付けより前に読む必要はない)。戻り値は従来どおり件数のまま。
- **設定 `notify.improve_events`** (bool、既定 true)。rollback 用に生成を止められる (通常経路の発行も起動時の回収も止まる)。変更後は daemon の再起動が必要。止めても snapshot は書く (通知用の記録は常に残す)。

### 4.5 `prepare()` の Tx-0 後の失敗

`prepare()` は Tx-0 で mission / run を作った後に workspace・prompt・runner を構築する。この後段で失敗すると内部補償 (`_compensate_prepare_failure`) が mission を終端する。補償は snapshot `(failed, prepare_failed)` を同じ tx で書く。後段の `except BaseException` は従来どおり **`BaseException` でも補償してから**再送出する (補償が成功すれば mission は terminal で、`prepare_failed` の snapshot が残る)。**`Exception` のときだけ** mission ID を持つ専用例外 `ImprovePrepareFailed(mission_id, slot_key)` (元の例外を `__cause__` に連結) に包んで送出し、`KeyboardInterrupt` / `SystemExit` などの `BaseException` は包まずに素通りさせる (shutdown の意味を変えない)。この場合も補償は済んでいるので snapshot はあり、supervisor までは例外が届かないため、`finished` event は次回起動の §4.4 の起動時の回収が出す。supervisor は `ImprovePrepareFailed` を受けて `finished` だけを発行したうえで、元の例外を再送出する (呼び出し側が見る例外は変わらない)。Tx-0 の前に失敗した場合は mission 行がなく、通知も出ない。**「started が出たのに mission 行がない」ことは起きない** (started は `prepare()` の完全成功後)。補償自体が失敗したときは mission が `running` のまま残り、§4.4 の確認で `finished` を出さず、次回起動の回収が通知する。

### 4.6 新 endpoint と親専有レポートの読み方

**新 endpoint `GET /v1/improve/runs/{mission_id}`** (scope は APPROVALS_DETAIL、読み取り、監査行なし)。返すのは、mission 行 (backend / model / 整数秒の `elapsed_sec`、終端前は null、`interrupted` は null)、snapshot (trigger / result_code / reason_code / tool_calls / observation_text、終端前は null)、`report_state` (`none` / `prepared` / `published` / `failed`)、承認 (snapshot の `approval_id` に対する id / plugin 名 / kind)、床未達の数値。mission が存在しない・improve でない場合だけ 404 (終端前の mission は snapshot が null の 200。開始通知がこれで backend / model を得る)。応答は DTO で、パス・内部例外文面・鍵を含まない。

**床未達の数値の読み方** (`result_code=gate_failed` かつ `reason_code=unprofitable` かつ `report_state=published` のときだけ読む):

1. 親が信頼できる `data/improve_reports` を dirfd として開く (symlink 不可)。名前は `improve-YYYY-MM-DD-<同一 mission_id>.md` の単一 basename だけ (`improvement_runs.report_path` の basename がこの正規形に一致するか確かめ、一致しなければ読まない)。
2. dirfd 基準の `openat(O_RDONLY | O_NOFOLLOW | O_CLOEXEC)` で開き、`fstat` が regular file であること、サイズが 64 KiB 以下であること、全体が厳密な UTF-8 であることを確かめる。
3. 見出し (`# Improve Mission <id> — gate failed`)、`## Profitability floor detail` 行、`failed_stage:` 行 (`in_sample` / `holdout` を `, ` 区切りで 1 つ以上。重複・未知の段・順序違いは形式不正)、`failed_pairs:` 行 (各 pair は `^[A-Z0-9_]{3,16}$`)、`### in_sample` の表 (必須)、`### holdout` の表 (`failed_stage` に `holdout` を含むときは必須、含まないときは有っても読まない) の列見出しを `_render_floor_report_detail` と同じ並びで厳密に照合する。表の末尾列は `failed` (`True` / `False`、その段でその pair が床を割ったか)。落ちた各段について `failed=True` の行だけを取り、どの落ちた段にも 1 行以上の失敗 pair があること、段ごとの `failed=True` の pair の和集合が `failed_pairs:` 行と一致することを確かめる。**旧形式** (`failed` 列の無い、既存の報告) は従来の規則で読む: `failed_stage` は 1 段だけを許し (複数段は形式不正)、その段の表のうち `failed_pairs:` に載った pair を取る。pair 行の trades は 0 以上の整数、`pf` / `avg_r` は有限の浮動小数か `None` (null 扱い)、`evaluable` は `True` / `False` だけ。**読み飛ばす行**: `reason:` 行、`thresholds:` 行、`No approval request…` の固定文、空行 (内容を値として使わない。見出し行と `## Profitability floor detail` 行の位置だけ確かめる)。
4. どれか 1 つでも反すれば、数値の部分全体を捨てる (部分的に出さない)。応答の数値欄は null になり、bot は見出しだけの通知にする (§2.2)。

parser は `_render_floor_report_detail` と同じ module に置き、render → parse の往復を test で固定する。

**応答 DTO (T1b 実装で確定。キー名・型・null の条件は afx-ops との契約)**

```json
{"id": 2043, "backend": "local", "model": "qwen3.6-35b", "elapsed_sec": 261,
 "snapshot": {"trigger": "scheduled", "result_code": "gate_failed", "reason_code": "unprofitable",
              "tool_calls": 10, "observation_text": null},
 "report_state": "published",
 "approval": null,
 "floor": {"stages": [{"stage": "holdout",
                      "pairs": [{"pair": "USDJPY", "trades": 13, "pf": 1.8458377992140615,
                                 "avg_r": 0.3029234868346611, "evaluable": false}]}]}}
```

| キー | 型 | null / 値の条件 |
|---|---|---|
| `id` | 整数 | 要求した mission id |
| `backend` / `model` | 文字列 | `missions.runner` / `missions.model` を `display_text` で 1 行に畳んだもの。終端前でも返る |
| `elapsed_sec` | 整数 または null | snapshot の `created_at` − `missions.started_at` をマイクロ秒の整数で引き、`(us + 500_000) // 1_000_000` (0.5 秒は切り上げ、float を使わない)。snapshot なし (終端前)、`(failed, interrupted)`、時刻のパース失敗、終了が開始より前のとき null。tz 無しの時刻は UTC とみなす |
| `snapshot` | オブジェクト または null | 終端前 (snapshot なし) は null |
| `snapshot.trigger` | `manual` / `scheduled` | |
| `snapshot.result_code` | 5 値のどれか | `submitted` / `observation` / `report` / `gate_failed` / `failed` |
| `snapshot.reason_code` | 文字列 または null | §4.2 の許可対に入る値、または null |
| `snapshot.tool_calls` | 整数 または null | 記録なしは null |
| `snapshot.observation_text` | 文字列 または null | `observation` のときだけ非 null になり得る。`display_text` で制御文字・改行を畳んだもの (上限 2,000 字の保存分をそのまま。切詰めは bot)。畳んだ結果が空なら空文字 `""` (bot は `(記載なし)` に倒す) |
| `report_state` | `none` / `prepared` / `published` / `failed` | run 行が無ければ `none` |
| `approval` | オブジェクト または null | snapshot の `approval_id` が null、または `approval_requests` に行が無いとき null |
| `approval.id` | 整数 | |
| `approval.plugin` | 文字列 または null | 承認 payload の `name` が plugin 名の文法 (`^[a-z][a-z0-9_]{0,63}$`) に一致するときだけ。取れなければ null |
| `approval.kind` | 文字列 または null | `approval_requests.kind` を `display_text` で畳んだもの。空なら null |
| `floor` | オブジェクト または null | `result_code=gate_failed` かつ `reason_code=unprofitable` かつ `report_state=published` で、親専有レポートを読んで形式検査に通ったときだけ非 null。それ以外 (読めない、形式不正、`improve_reports_dir` 未設定を含む) は null。理由は応答に出さない。技術ログには固定語 `improve_run floor unavailable mission=<id> reason=<missing / malformed / oversized / unsafe / unpublished>` だけを書く (path・数値・例外文面は書かない)。読取は ops lane (deadline 付き) の中で行う |
| `floor.stages` | 配列 | 落ちた段を `in_sample`、`holdout` の順に 1 要素以上 (旧形式は 1 要素) |
| `floor.stages[].stage` | `in_sample` / `holdout` | レポートの `failed_stage` に載った段 |
| `floor.stages[].pairs` | 配列 | その段の表のうち、その段で失敗した pair (`failed=True`、旧形式は `failed_pairs` に載った pair) を表の順に。件数の上限は設けない (3 本超でも全部返す。表示側が切る) |
| `floor.stages[].pairs[].pair` | 文字列 | `^[A-Z0-9_]{3,16}$` |
| `floor.stages[].pairs[].trades` | 整数 | 0 以上 |
| `floor.stages[].pairs[].pf` / `avg_r` | 浮動小数 または null | 表の値そのまま (丸めは表示側)。レポートの `None` は null。`nan` / `inf` / 空欄はレポートごと形式不正 |
| `floor.stages[].pairs[].evaluable` | 真偽値 | `True` / `False` だけ |

404 (`NOT_FOUND`) は mission が存在しない、または `missions.loop` が `improve` でないときだけ。応答にパス・例外の文面・鍵は含まれない。

### 4.7 afx-ops

afx-ops は `agentic_fx` を import しない独立 package で、event の検証は `notify/payload.py` に自前の写しを持つ。daemon の厳格な許可対 (§4.2) はそのまま残し、afx-ops 側は**文法だけを検証して、語の追加で止まらない**ようにする。

- **検証 (`notify/payload.py`、bot と notify が共有)**: `state` ∈ {started, finished}、`trigger` ∈ {manual, scheduled}、`result_code` は 5 値の列挙か null、`reason_code` は文法 `[a-z][a-z0-9_]{0,31}` か null、`tool_calls` は 0〜1,000,000 の整数 (bool 不可) か null。reason_code を列挙で照合しない (取引 event の型に倣うと、daemon が語を足した直後に未知の語で `PayloadError` となり、cursor が進まず全 event の配送が止まるため)。
- **表示ラベルの表**: §2.2 の見出しの表を `gate/render.py` に定数として置く (`web/render.py` の「daemon 側の定義と同じ」の先例)。表に無い `(result_code, reason_code)` は §2.2 の「上記以外」の行、未知の `result_code` は `[改善] 終了 #<id>` で描画する (止まらない)。未知の reason は escape して括弧内にそのまま出す。
- **語の一致の確認 (daemon → afx-ops の 1 方向)**: afx-ops は同じリポジトリ (`afx-ops/afx_ops/gate/render.py`) にあるので、daemon 側の test がそのファイルを読み (`ast` で定数のリテラルを評価する。import しない)、(1) afx-ops の表のキーがすべて daemon の許可対に入っていること、(2) daemon 側が「専用の見出しを持つ対」として宣言した集合 (`improve_notice_codes.PAIRS_WITH_DEDICATED_HEADING`) がすべて afx-ops の表にあることを確かめる。
- bot: `improve_run_changed` の template (§2)、`improve_notices` の設定、BotClient (`api_client.py`) の新しい GET method (承認の plugin 名補完と同じ型。要求全体の 3 秒 deadline、失敗時は §2.4 の全文)。`EventPlan.allow_mentions=False`。
- notify: 未知 code で止まらないよう、新 code を allowlist に足し、`deliver_improve_events` (既定 false、cursor は進める) を取引 event と同じ型で足す。webhook に送る場合も fields の固定語だけ。
- web: 無変更 (event 一覧が未知 code で落ちないことだけ確認する)。

### 4.8 配備順と再起動

取引通知と同じ。**afx-ops (bot / notify) を先に配って再起動 → daemon を再起動して emitter を有効にする。** 逆順だと、新 code を読んだ旧 bot / notify が停止する。以後は daemon が語を足しても afx-ops の再配備は要らない (§4.7 の文法検証。見出しは括弧内に出るだけで、専用の見出しを足したいときだけ afx-ops を更新する)。daemon の再起動は mission の実行中を避ける (走行中の mission は再起動で中断され、中断は §4.4 の回収で `interrupted` として通知される)。rollback は `notify.improve_events: false` → daemon 再起動 → bot / notify が追いついてから afx-ops を戻す。`improve_notices` は bot の再起動で反映される。

## 5. 受入

| AC | 内容 |
|---|---|
| AC-1 | 5 結果 + 開始の本文の完全一致。実 mission (#2040 / #2043 / #2044 / #2045 / #2046) のレポートと DB 行を fixture にし、bot の描画結果 (**escape 後の content** そのもの。`session\_ranges`、`insufficient\_trades`、`commit\_crashed`) を §2.2 の文面と 1 文字単位で比べる。snapshot の `created_at` は fixture で終端 activity 行の時刻 (#2040=06:52:16、#2043=07:11:36、#2044=07:21:36、#2045=07:35:09、#2046=08:03:39、すべて 2026-10-25 UTC、小数秒 0) に固定し、`started_at` は実 mission 行の値。所要は §2.2 の規則で #2040=4分49秒 / #2043=4分21秒 / #2044=6分40秒 / #2045=7分27秒 / #2046=6分45秒。tool 回数は #2040 / #2043 / #2044 / #2045 が activity の記録、#2046 は記録なしの `tool -` (snapshot の `tool_calls=null`)。submitted と report は合成 fixture。tool_calls が非 null の補償経路は生の `MissionResult` fixture で別に固定。§2.2 の表の全行 (observation の 3 つの本文行、`(記載なし)`、`interrupted` の `所要 -`、未知の reason の括弧、未知の result_code の `終了`) も同じ形で固定する。§2.4 の取得失敗の全文 (開始 + 5 結果 × 404 / 5xx / deadline 超過) も完全一致で固定する |
| AC-2 | 床未達の数値: 段 (`failed_stage` が in_sample → `in-sample` の行が出る / holdout → `holdout` の行が出る / **複数段 (`in_sample, holdout`) は `failed` 列から段ごとの失敗 pair を復元して段ごとに出す。`failed` 列の無い旧形式は 1 段だけ読み、複数段は形式不正で数値行と「落ちた段」を省く**)、pair 3 本超、`evaluable` の真偽、pf / avg_r が null (`pf -`)、`-0.00` の正規化 (pf=`0.00`、avg_r=`+0.00`)、丸め (`format` の結果)、§4.6 の検査の各項目 (レポート欠落・`report_state` が published でない・symlink・basename 不一致・配下外・65 KiB 超・UTF-8 不正・見出し / 列の不一致・`failed` 列の値不正・`failed` 列と `failed_pairs` の不一致・holdout 表の欠落・`nan` / `inf`・負の trades・形式不正) で、数値行が出る / 全体が省かれて見出しだけになる境界を押さえる。読み飛ばす行 (`reason:`、`thresholds:`) の内容が出力に影響しないこと。読取失敗の技術ログが固定語 (`mission=<id> reason=<固定語>`) だけであること。GET の読取が ops lane の deadline で打ち切られること |
| AC-3 | canary を 2 本に分ける。(a) **秘密 canary**: レポートと agent 出力に URL・鍵様の文字列を入れて mission を終端させ、activity.log、backlog (`last_result` / `idea` / `origin_outcome`)、次回 mission のプロンプト全文、`ops_events` の全行、終端 snapshot、監査・idempotency 表、daemon の技術ログ、API server のログ、bot の state / retry 構造 / ログ、notify の送信 payload、**Discord の描画結果**のいずれにも出ないこと。(b) **数値 canary**: 目印の数値 (例 `7.7777`) をレポートの holdout に入れ、上の全 sink に出ず、**Discord の描画結果にだけ**出ること。加えて静的検査で、sandbox・prompt builder・RPC handler・staging の module が `improve_run_notices` を参照しないことを確かめる |
| AC-4 | 観察文: 300 字ちょうど / 超過 (`cut_escaped(value, 300)` と同じく escape 後の 300 字以内、escape の単位の途中で切らない、「…」込み)、改行・制御文字、`**` `@everyone` `<@id>` コードフェンス、URL を含む文が、1 行・無効化・切詰めで描画される。`not_submitted` は固定文。観察文 null / 空白のみは `(記載なし)`。improve 通知の `EventPlan.allow_mentions` が False で、送信に `AllowedMentions.none()` が渡る (設定済み mention があっても付かない) |
| AC-5 | (a) event の専用検査 (§4.1): state / trigger / result_code / reason_code / tool_calls の各 allowlist。`reason_code` に `pf:7.7777` / `:` / 数字 / 例外文面 / 表に無い語 / 許可対でない組 / `gate_other` を入れると `EventSchemaError`、`tool_calls` に負数・bool・小数・文字列を入れても同様。(b) **網羅 (AST)**: 期待 site の一覧 (関数名 + 分岐) を test に持ち、`finish_improve_mission(` の呼び出しが一覧と過不足なく一致すること、各呼び出しの `notice=` が `NOTICE_*` 定数・`notice_for_*()` の閉じた辞書引き・終端 helper が受けた `notice` 引数・`choice.notice` のどれかであること、終端 helper のうち `_finalize_gate_failed`・`_compensate_prepare_failure` の全呼び出し式が `notice=` を、`_finalize_report_or_observation` の全呼び出し式が `choice=` だけを渡すこと (`_finalize_report_or_observation` の 3 呼び出しの出所は §4.2 のとおり)、`_PluginGateVerdict(passed=False…)` と `_QualityVerdict(outcome=…)` の全 return に加え、内部 wrapper (`_failed_before_pytest` / `_failed_after_pytest` / `_quality_rejection` / `_quality_unavailable`) の各意味分岐の呼び出し式が既定値なしの固定 `code=` を渡すこと。(c) 変換表 (§4.2) の全行について、site が表のとおりの (result_code, reason_code) を snapshot に書く。(d) `gate_failed:max_bars_limit:<exc>` / `commit_crashed:OSError:...` / `insufficient_trades:16` から例外文面と数が出ない。(e) `observation_text` は 2,001 字以上でも 2,000 字に切り詰めて保存され、終端が成功する。(f) 変異: tool 予算 abort の suffix と observation の prefix 判定の suffix に URL・鍵様の文字列・数値を入れても、snapshot と event に出ない。status の優先順位 (timeout → max_turns → failed+tool budget → その他 failed) を全組合せで固定する。質検査の `code` は 4 種 (`duplicate_logic` / `duplicate_metrics` / `family_redirect` / `comparison_base_unavailable`) の全行を実経路で試す。防御枝の `candidate_identity_unavailable` (`unverified` に引く) も実経路 test で snapshot 対を固定する |
| AC-6 | 因果を 2 つに分ける。(a) **daemon 側**: bot 停止・event の INSERT 失敗・`EventSchemaError` のいずれでも、mission の終端状態 (missions / improvement_runs / slot / snapshot) と activity が変わらない。(b) **afx-ops 側**: `channel.send` が 5xx のとき cursor を進めず、次の周回で同じ event を再送する (GET から取り直す)。詳細取得の失敗は §2.4 の全文で投稿が続く。両方を満たして「Discord の失敗が mission に戻らない」を示す |
| AC-7 | 手動と週次の両方で started / finished が 1 回ずつ出る。補償経路 (commit 例外、launch 例外、Tx-2 の承認作成失敗) でも finished が 1 回出る。**Tx-0 の後・`prepare()` の return 前の `Exception`** で finished だけが 1 回出て started は出ない (`ImprovePrepareFailed`、呼び出し側には元の例外が見える)。同じ位置の `KeyboardInterrupt` は包まれず素通りするが、補償は済み、mission は terminal で `(failed, prepare_failed)` の snapshot が書かれる (event は出ず、次回起動で 1 回出る)。補償自体が失敗して mission が `running` のままのとき finished が出ず、技術ログに固定語 (`not_terminal`) が残る。重複 event は bot が 1 通に落とす。supervisor は ops service を使わず、自前の接続と `EventStore` で書く |
| AC-8 | 再起動回収と起動時の取りこぼし回収 (outbox): running の improve mission を持つ DB で回収すると、snapshot `(failed, interrupted)` が書かれ、ops service 構築直後・listener を開く前に `finished` が 1 件出る。trigger は Tx-0 で記録した `improvement_runs.trigger` から復元され、`recover_stale_slots` で slot の `mission_id` が消えた後でも、pre-ready 失敗で slot が巻き戻った後でも週次のまま。**回収 commit の後・event 発行の前に daemon が落ちた場合、次回起動で 1 回出る。** 通常経路の `emit_best_effort` が INSERT 失敗した終端、commit 後・発行前に落ちた終端も同様に次回起動で 1 回出る。2 回目の起動では再通知されない (finished event が既にある)。emit が失敗した終端は、技術ログに固定語 `improve_notice_emit_failed` が 1 行出て、次の tick の回収で `finished` が 1 回だけ出る (さらに次の tick では出ない)。窓外の snapshot は SQL で除かれる。作成から 7 日を超えた snapshot は補わない。`notify.improve_events=false` では補わない。`started` は補わない |
| AC-9 | 配備順: 新 code を含む event が先にあっても、afx-ops 配備済みの bot / notify は止まらない。**未知の reason_code** (例 `zzz_new_word`) を持つ event を、afx-ops の検証が通し、`(zzz\_new\_word)` として描画する (bot / notify とも止まらず cursor が進む)。語の一致 (§4.7 の daemon 側 test) が通る。`notify.improve_events: false` で event が生成されない (snapshot は書かれる) |
| AC-10 | `improve_notices` の 3 値で投稿が変わる。値の変更は bot の再起動で反映される (再起動なしでは変わらないことを test で固定する)。daemon 側は `notify.improve_events` の変更に再起動が要る |
| AC-11 | 新 endpoint は notify と agent の経路から呼べず、APPROVALS_DETAIL を持つ principal だけが読める。応答に鍵・パス・内部例外文面が出ない。監査行を書かない。終端前の mission は snapshot が null の 200、存在しない / improve でない mission は 404。`report_state` を返す |
| AC-12 | 終端 snapshot の不変性と同一 tx: UPDATE / DELETE が拒否される。`finish_improve_mission` の CAS が失敗した (既に終端済み) 補償では snapshot が増えない。tx が rollback したとき snapshot も残らない。後から backlog の `last_result` を書き換えても GET の観察文が変わらない。**旧 DB に `init_db` をかけると、表・trigger・`improvement_runs.trigger` 列・`ops_events (code, ref)` の index ができ、既存表の行が変わらない** |
| AC-13 | report の公開失敗: publish (rename) が失敗して `report_state=failed` になった report の通知が `[改善] レポートの公開に失敗 #<id>` になる (snapshot は `(report, …)` のまま不変)。床未達の report が `report_state=failed` なら、数値行を出さず見出しだけになる |

変異スイープの対象 (最低限): 観察文の escape 除去、300 字上限の除去、レポート path / basename 検査の除去、レポートの byte 上限・`O_NOFOLLOW` の除去、`report_state=published` 条件の除去、数値の event fields への混入、`last_result` / activity / snapshot への数値混入、event 専用検査の各 allowlist の除去、reason_code を文面から切り出す実装への退行 (site 定数の置換)、snapshot を CAS の外で書く、`notice` 引数の既定値化、started の二重発行、commit 例外時の finished 欠落、`not_terminal` 確認の除去、起動時の回収 (outbox) の除去・7 日上限の除去・発行済み判定の除去、`allow_mentions` の除去、GET の deadline 除去、`-0.00` 正規化の除去、afx-ops の reason 検証を列挙に戻す退行。

## 6. 変更一覧と task 分割

純 Python のみ。実 kernel・systemd は触らない。

| task | 範囲 | 規模 | 実装 |
|---|---|---|---|
| T1a daemon 前半 | `improve_notice_codes` (allowlist・許可対・`NoticeCode` 定数・閉じた辞書・`PAIRS_WITH_DEDICATED_HEADING`)、DB schema (`improve_run_notices` + 不変 trigger + `improvement_runs.trigger`、旧 DB に `init_db` の test)、`store/missions.py` (`finish_improve_mission` の `notice` 引数、`recover_interrupted` の snapshot 書込み)、`improve_loop.py` (全終端 site の定数渡し、`_PluginGateVerdict` / `_QualityVerdict` の `code`、終端 helper の `notice` / `choice` 引数、`ImprovePrepareFailed`、Tx-0 の trigger、補償への `tool_calls`)、AC-5・12 の網羅 test、**既存 test の追従 (grep 一致 74 件、実 call 式 68 件。AST で数えた内訳は `finish_improve_mission(` 12、終端 helper / `recover_interrupted` 56 で、追従の基準はこの AST の件数)** | 約 550 行 + test 700 行 + 既存 test 追従 68 call 式 | terra |
| T1b daemon 後半 | `core/improve_supervisor.py` (自前接続と `EventStore`、started / finished の発行、`not_terminal` 確認)、起動時の取りこぼし回収 (本体は `ops/service.py`、**呼び出しは `src/agentic_fx/service.py` の `_build_ops_service()` 直後、`_recover_ops` / listener / supervisor の開始前**)、`ops_events (code, ref)` の index (`store/db.py` の schema、旧 DB の test)、`ops/events.py` (新 code と専用検査)、`ops/service.py` (新 endpoint、`report_state`)、floor report の parser、`config.py` / `settings.yaml.example` (`notify.improve_events`)、`contracts` の endpoint 表、`cli_surface.py` の記載確認、afx-ops の表との一致 test、AC-2・3・6a・7・8・9 (daemon 側)・11・13 | 約 450 行 + test 600 行 | terra |
| T2 afx-ops | `notify/payload.py` (`improve_run_changed` の文法検証)、`gate/render.py` (見出しの表・template・§2.4 の全文)、`config.py` (`improve_notices`、`deliver_improve_events`)、`api_client.py` の BotClient の新 GET method (要求全体の deadline)、bot の詳細取得と worker ローカル値の扱い、notify の allowlist、`ops_setup` writer の key 保持、AC-1・3 (bot / notify 側の sink)・4・6b・9・10 | 約 350 行 + test 450 行 | terra |

T1a → T1b は順序あり (T1b は T1a の定数と snapshot を使う)。T1b の「afx-ops の表との一致 test」は T2 の成果物 (`gate/render.py` の表) を読むので、T2 の merge 後の統合条件として確かめる (T1b 単独では緑にならない)。T2 は event の fields と GET の応答 DTO を契約に T1 と並行できる。T2 の fixture は T1 が出す実レポートから作る。文書 `docs/operations/ops-notices.md` に節を足す (配備順、設定、注記)。

## 7. 裁定が要る点

技術で決まるものは推奨で決める。ユーザー裁定が要った次の 2 点は、どちらも推奨で裁定済。

1. **開始通知の要否と既定。** **裁定済 (2026-10-25、ユーザー: 推奨で)。** 推奨: 依頼どおり実装し、既定 `all`、`improve_notices = "results"` で終了のみに絞れる。根拠: 週次は無人なので開始の可視化に意味があり、現設定 (`improve.parallel: 1`) では 1 周 1 本で通知疲れにならず、「開始あり・終了なし」が異常終了や停止を切り分ける唯一の手掛かりになる。要らなければ実装の差は設定 1 値で済む。
2. **床未達の数値の粒度。** **裁定済 (2026-10-25、ユーザー: 推奨で)。** 推奨: 落ちた段の、落ちた pair の行だけを出す (in_sample で落ちたなら in_sample の行、holdout で落ちたなら holdout の行)。項目は pf / avg_r / 取引数 / 評価不能 (依頼どおり)。通った段の数値、win_rate、max_drawdown、total_pnl、閾値は載せない。根拠: pf・avg_r・取引数・評価可否は床の判定そのものの入力なので「なぜ落ちたか」はこれで足り、それ以外を載せるほど運用者が方針へ書き写す数値が増える。in_sample の成績は agent が `run_backtest` で既に見ており、in_sample で落ちた候補は holdout を回さないので、in_sample の行は遮断 8 の外にある。holdout の数値を人間が見ることは、遮断 8 (agent に見せない) の範囲外だが、§3 の 8 の注記を運用文書に入れる前提。

以下は推奨で決めた (裁定不要)。観察文の上限は 300 字 (依頼どおり、切詰めは表示のみで保存は 2,000 字まで)。plugin 名は submitted のみ。課題 (idea) は載せない (`improve-run-summary-for-humans` の範囲)。

## 8. 確認事項 (既存コードとの食い違い・未確認)

無し。v0.1 の 6 件は既存コードの確認で確定し、§0.2 に移した。v0.3 で照合した終端 site の欠落 (`_compensate_tx2_failure`、`backtest_data_unavailable`、`history_source_unconfigured`、質検査 4 種、plugin gate 10 種) は §4.2 に反映済み。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 | 初版 | チケット `improve-start-and-result-to-discord` の設計 | |
| 2026-10-25 | v0.2 | event の専用意味検査と (result_code, reason_code) の変換表 (§4.1・§4.2)、mission 終端 tx と同時に書く不変な終端 snapshot を通知の正本に (§4.3)、再起動回収の通知 (§4.4)、`prepare()` 後段失敗の `ImprovePrepareFailed` (§4.5)、`finished` 前の終端確認 (§4.4)、取得失敗時の全文と要求全体の deadline (§2.4)、bot のローカル値の所有期間と canary の分離 (§3-7・AC-3)、report 読取の厳密規則と null 表示 (§4.6)、秒の丸め規則と実データに合わせた例 (§2.2・AC-1)、AC-6 の因果分割、task 範囲の拡張 (§6)、`cut_escaped` / `allow_mentions=False` の明記、§8 を確定事項 (§0.2) へ移動 | 設計レビュー 1 周目 (sol、Critical 2 / Important 10 / Minor 3 + §8 回答 6 件、全件採用。詳細は `tmp/review-discord/c0-r1-triage.md`) | |
| 2026-10-25 | v0.3 | 変換表を分岐ごとの定数に作り直し (`NoticeCode`、`_PluginGateVerdict` / `_QualityVerdict` の `code`、不足 site の追加、`gate_other` の廃止、AST 検査の対象変更。§4.2・AC-5)、afx-ops は文法検証 + 自前ラベル表で daemon の語の追加に縛られない (§4.7・§4.8)、所要を snapshot の `created_at` 基準に (§2.2)、見出し・本文行を 1 枚の表に (§2.2)、in_sample で落ちたら in_sample の行を出す (§2.2・§7-2)、`report_state` で公開失敗を表示 (§4.3・§4.6・AC-13)、snapshot を outbox とする起動時の取りこぼし回収と回収の戻り値の持ち回しの廃止 (§4.4・AC-8)、`improvement_runs.trigger` を Tx-0 で記録 (§4.3)、DB の追加作法 (`IF NOT EXISTS`・旧 DB の test)、`observation_text` の保存時切詰め、`ImprovePrepareFailed` は `Exception` のみ、supervisor の自前接続、数値の丸めと `-0.00`、読み飛ばす行の列挙 (§4.6)、実例を escape 後の content・新しい所要で再生成 (§2.2・AC-1)、T1 を T1a / T1b に分割し既存 test 74 か所の追従と `notify/payload.py` を見積りに追加 (§6) | 設計レビュー 2 周目 (opus、Critical 0 / Important 7 / Minor 8、全件採用。詳細は `tmp/review-discord/c0-r2-triage.md`) | |
| 2026-10-25 | v0.4 | `_finalize_report_or_observation` の 3 呼び出しの notice の出所と observation selector (§4.2)、質検査を 4 種に (到達不能の `candidate_identity_unavailable` 行を削除)、status の優先順位、wrapper の `code=` の AST 検査と suffix 変異 (AC-5)、`BaseException` も補償してから再送出 (§4.5・AC-7)、outbox の 3 条件と `ops_events (code, ref)` の index (§4.4)、`failed_stage` は 1 段のみ (§2.2・§4.6・AC-2)、T1b に `src/agentic_fx/service.py` と呼び出し位置・統合条件、T1a の件数を grep 74 / call 式 68 に (§6) | 設計レビュー 3 周目 (sol 閉鎖確認、詳細 `tmp/review-discord/c0-r3-triage.md`) | |
| 2026-10-25 | v0.5 | §7 の 2 件 (開始通知の要否と既定、床未達の数値の粒度) を裁定済に (推奨どおり) | ユーザー裁定 (2026-10-25、「推奨で」) | |
| 2026-10-25 | v0.6 | plugin gate の隔離不能を `gate_failed/gate_sandbox_unavailable` に (backlog を消費する実挙動と一致させる。strategy gate の `failed/sandbox_unavailable` と区別。§4.2)、`improve_notice_codes` を `agentic_fx/improve_notice_codes.py` に (§4.1)、`_finalize_report_or_observation` は `choice` のみを受け notice は `choice.notice` から導出、未知 artifact は `risk_gate_unsupported` に倒す (§4.2・AC-5)、質検査の防御枝 `candidate_identity_unavailable` を `unverified` に引く (§4.2・AC-5) | T1a 実装レビュー (/code-review high + codex r1、詳細 `tmp/review-discord/reports/t1a/t1a-rulings.md`) | |
| 2026-10-25 | v0.7 | GET `/v1/improve/runs/{id}` の応答 DTO (キー名・型・null の条件) を表で確定 (§4.6) | T1b 実装で DTO を確定 (T2 との契約) | |
| 2026-10-26 | v0.8 | §4.4: outbox を起動時に加えて scheduler の tick ごとにも走らせる、emit 失敗の固定語 `improve_notice_emit_failed` と接続の作り直し、終端確認は emit と同じ接続、7 日窓は SQL。§4.6 / §2.2: report の表に `failed` 列 (段ごと・pair ごと)、複数段を段ごとに読む、旧形式 (`failed` 列なし) は従来の 1 段規則、DTO を `floor.stages[]` に (`floor.stage` / `floor.pairs` は廃止)、警告の固定語に mission と reason、読取を ops lane 内に。§4.6 の DTO 例・AC-2・AC-8 を追従 | T1b 実装レビュー (/code-review high、詳細 `tmp/review-discord/reports/t1b/t1b-rulings.md`) | |
