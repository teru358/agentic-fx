# 改善 mission の開始・終了通知 (Discord) 設計書

**版:** v0.2 (設計レビュー 1 周目の指摘を反映)

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
5. `missions.trigger` は improve では NULL。通常実行中の手動 / 週次は `ctx.slot_key` の有無で決まる (None = 手動)。再起動回収では ctx が無いので、`improve_wave_slots.mission_id` に行があれば週次、無ければ手動とする (§4.4)。
6. 再起動回収 (`missions.recover_interrupted`) は存在する。running の mission を `interrupted` にし、unfinished の run と slot も回収するが、現行は activity も event も出さず、返すのは件数だけ。したがって通知が欠ける。§4.4 で、回収した improve mission の通知を出す。

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

共通: 1 行目は結果、最終行は `所要 <m分s秒> / tool <n> 回`。

- **所要** は mission 行の開始・終了時刻の差 (daemon が整数秒 `elapsed_sec` にして返す)。小数秒は四捨五入 (0.5 は切り上げ) で整数秒にし、`<m>分<ss>秒` で出す (秒は 2 桁)。60 分以上は `<h>時間<mm>分<ss>秒`。時刻が欠けるか終了が開始より前のときは所要の項を省き、`tool` だけを出す。
- **tool 回数** は終端 snapshot の `tool_calls`。null (記録が無い終端) のときは `tool -` と出す。
- 両方とも出せない最終行は `所要 -`。

以下は 2026-10-25 の実 mission での例 (所要は DB の時刻差から上の規則で算出: #2040 は 289.41 秒、#2043 は 179.76 秒、#2044 は 278.92 秒、#2045 は 305.94 秒、#2046 は 406.14 秒)。

**observation** (#2040、agent の申告):

```text
[改善] 提出なし (agent の申告) #2040
agent の申告: session_ranges によるロンドン・NY開始足のレンジ外ブレイク後回帰を実装し、固定出口と短縮出口でUSDJPY 1hを検証した。どちらも不採算判定となり、提出しなかった。次回は時間帯回帰以外の条件、または別の市場仮説を優先する。
所要 4分49秒 / tool 11 回
```

申告は `cut_escaped(value, 300)` (bot の既存関数) と同じ規則で出す。1 行化 → escape → escape 後の文字数で 300 字以内 (切るときは escape の単位を壊さず、末尾の「…」を含めて 300 字以内)。この文は agent が書いたもので、システムの判定ではないことをラベルで示す。重複・系統見送りで reason_code が `not_submitted` の mission は「agent の申告なし (重複または系統の見送り)」の固定文にする。`demoted_recovered` / `duplicate_demoted` は申告文なしで 1 行目に `(降格)` を付ける。

**gate_failed、床未達** (#2043、#2045。数値は親専有レポートから):

```text
[改善] 床未達で見送り #2043 (unprofitable)
落ちた段: holdout
holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)
所要 3分00秒 / tool 10 回

[改善] 床未達で見送り #2045 (unprofitable)
落ちた段: holdout
holdout: USDJPY pf 0.51 avg_r -0.28 取引 24 (評価不能)
所要 5分06秒 / tool 14 回
```

数値は落ちた段の、落ちた pair の行だけ (pair は最大 3 本、超えたら「ほか n 本」)。pf と avg_r は小数 2 桁、avg_r は符号付き。値が null のときは `pf -` / `avg_r -` と出す。`evaluable=false` は「評価不能」、true は付けない。段が in_sample だけで落ちたときは holdout の行は出さない (レポートが holdout を持たないため)。#2043 のように pf が 1 を超えても評価不能で落ちることがあるので、「評価不能」を落ちた理由として必ず並べる。レポートが無い・読めない・形式が想定外のとき (§4.6 の検査のどれかに反する) は、数値行と「落ちた段」を省き、固定語だけにする。床未達以外の `gate_failed` (取引数不足、`loader_rejected` など) も固定語だけ。reason_code は先頭語だけで、`insufficient_trades:16` のような数は出さない (#2044 の実例):

```text
[改善] 見送り #2044 (insufficient_trades)
所要 4分39秒 / tool 13 回
```

**failed** (#2046。実際はディスク quota 超過による commit 失敗):

```text
[改善] 失敗 #2046 (commit_crashed)
所要 6分46秒 / tool -
```

例外の文面 (`Disk quota exceeded` など) は出さない。理由語だけ。#2046 の実データは tool 回数が DB にも activity にも残っていないので `tool -`。この実装以降の commit 例外では supervisor が `result.tool_calls` を補償に渡すので、回数が出る (§4.3)。

**submitted** (例。実例は承認 0 件のため構成):

```text
[改善] 候補を提出 #2050
plugin <name> (strategy) / 承認 #26
所要 5分30秒 / tool 14 回
```

承認の中身は既存の承認 message が担う。ここは重ねない。**report** は `[改善] レポートを公開 #<id>` (重複の見送りは `[改善] レポートを公開 #<id> (duplicate_skipped)`) に所要を添える (数値なし)。

### 2.3 設定

- bot の `ops.toml` に `improve_notices = "all" | "results" | "off"` を足す (既定 `all`、`results` は終了のみ)。
- 初回の正常 poll は取引通知と同じく、配備前の event を配送しない。

### 2.4 詳細を取得できないとき

詳細の取得は GET 1 回で、**3 秒は要求全体の monotonic deadline** (接続・送信・受信・本文読込の合計)。404 / 5xx / 接続失敗 / deadline 超過 / 応答の検査違反のいずれでも、投稿は止めず次の全文を出す (承認の plugin 名補完と同じ型)。event の `trigger` / `result_code` / `reason_code` / `tool_calls` だけから作る。

- 開始: 1 行目だけ。`[改善] 開始 #<id> (手動|週次)`。
- 終了 (5 結果とも): 1 行目は §2.2 と同じ見出し (結果ごとの語 + `#<id>` + reason_code があれば `(<reason>)`)、`tool_calls` が非 null なら 2 行目 `tool <n> 回`、最終行に `(詳細を取得できませんでした)`。観察文・数値・所要・plugin 名・backend は出さない。
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
  - `finished` のとき `result_code` は 5 値のどれか、`reason_code` は §4.2 の表でその `result_code` に許された値 (または null が許される結果での null)。
  - `tool_calls` は 0 以上 1,000,000 以下の整数 (bool 不可) か null。
  - これ以外 (`pf:7.7777`、`:` や数字を含む reason、表に無い語、負数) は `EventSchemaError`。
- allowlist と (result_code, reason_code) の許可対の定義は 1 か所 (`improve_notice_codes` module) に置き、event の検査・終端 snapshot の書込み・bot の template が同じ定義を使う。bot 側は独立した定義を持たず、未知の組は §2.4 の汎用見出しで描画する (止まらない)。

### 4.2 終端 site → (result_code, reason_code) の変換表

各 site は固定定数を渡す。例外の文面や reason 文字列を split して作らない。`finish_improve_mission` の呼び出し元と補償・回収の全数を AST test で表と突き合わせる (表に無い site があれば test が落ちる)。

| 終端 site | result_code | reason_code |
|---|---|---|
| 承認依頼を出した (`_finalize_success` の approval 経路) | `submitted` | null |
| 観察で終了 (`mission_observation`) | `observation` | null |
| 観察で終了、`observation:not_submitted` | `observation` | `not_submitted` |
| report を observation に降格 (resume 回収経由) | `observation` | `demoted_recovered` |
| 重複判定で observation に降格 (`_DuplicateDemotion`) | `observation` | `duplicate_demoted` |
| report 公開 (`report_published`) | `report` | null |
| 敗者経路 (選択 CAS に負け、重複の自動 report) | `report` | `duplicate_skipped` |
| 敗者経路 / report 書込み失敗 (`report_state='failed'`) | `failed` | `report_write_failed` |
| 床未達 (`unprofitable`) | `gate_failed` | `unprofitable` |
| 取引数不足 (`insufficient_trades:<n>`、数は捨てる) | `gate_failed` | `insufficient_trades` |
| plugin gate 不合格 (`gate_failed:<verdict.reason>`、文面は捨てる) | `gate_failed` | `plugin_gate` |
| `gate_failed:loader_rejected` | `gate_failed` | `loader_rejected` |
| `gate_failed:unverified` / 候補の同一性が取れない | `gate_failed` | `unverified` |
| `gate_failed:max_bars_limit:<exc>` (例外文は捨てる) | `gate_failed` | `max_bars_limit` |
| `gate_failed:kind_unsupported:<kind>` | `gate_failed` | `kind_unsupported` |
| `outputs_required` | `gate_failed` | `outputs_required` |
| `indicator_unresolved` | `gate_failed` | `indicator_unresolved` |
| 質検査の根拠が作れない (`comparison_base_unavailable`) | `gate_failed` | `comparison_base_unavailable` |
| 上のどれにも当たらない `gate_failed` (T1 で全 return を列挙して残る分) | `gate_failed` | `gate_other` |
| runner の status が `timeout` / `max_turns` | `failed` | `timeout` / `max_turns` |
| runner の status が `failed` (tool 予算 abort を除く) | `failed` | `runner_failed` |
| tool 予算 abort (`is_tool_budget_abort`) | `failed` | `tool_budget_abort` |
| `output_invalid` | `failed` | `output_invalid` |
| gate の隔離不能 (`gate_worker_sandbox_unavailable`) | `failed` | `sandbox_unavailable` |
| `commit()` の例外後の補償 (`compensate_commit_failure`) | `failed` | `commit_crashed` |
| `runner.run()` の例外後の補償 (`compensate_launch_failure`) | `failed` | `launch_crashed` |
| `prepare()` の Tx-0 後の失敗の補償 (`_compensate_prepare_failure` が呼ばれる準備側) | `failed` | `prepare_failed` |
| 再起動時回収 (`recover_interrupted`) | `failed` | `interrupted` |

`gate_other` と `plugin_gate` は「不明な文面を捨てて語だけ残す」受け皿で、実際の拒否理由を失う代わりに文面の漏れを構造的に防ぐ。受け皿に落ちた件数が多いときは語を足す (表の変更は設計書の変更履歴に記録する)。

### 4.3 終端 snapshot (通知の正本)

- **表 `improve_run_notices`**: `mission_id` (PK、`missions(id)` 参照)、`trigger` (`manual` / `scheduled`)、`result_code`、`reason_code`、`tool_calls` (null 可)、`observation_text` (null 可。`safe_text` 済み、上限 2,000 字)、`approval_id` (null 可)、`created_at`。DB の migration で追加する (旧 fixture と byte 一致の test を付ける)。UPDATE / DELETE を `RAISE(ABORT)` する trigger で不変にする。書込みの前に §4.1 と同じ許可対の検査を通す (違反は `ValueError` で終端 tx が失敗し、既定値で補うことはしない。AST test が全 site の網羅を保証するので、実行時に違反するのは実装の誤りだけで、その場合は補償が `commit_crashed` で終端する)。
- **mission を終端する同じ transaction で書く。** `missions.finish_improve_mission` に必須のキーワード引数 `notice` (上の 5 項目の値) を足し、`WHERE status='running'` の CAS が成功した (rowcount=1) ときだけ同じ tx で snapshot を挿入する。既に終端済みなら (補償の冪等な再実行) 何も書かない。呼び出し元が `notice` を渡さないとコードが書けない (引数に既定値を付けない)。
- **holdout 数値は持たない。** 床未達の数値は GET のたびに親専有レポートから読む (§4.6)。
- **`commit()` の契約は変えない** (戻りは `None`)。supervisor は commit / 補償の後に snapshot を **別の接続で読んで** event を作る。`ImproveRunContext` (frozen) に結果を書き足さない。`tool_calls` は `commit()` が `result.tool_calls` から snapshot に書く。補償 (`compensate_commit_failure` / `compensate_launch_failure`) には supervisor が `tool_calls` を引数で渡す (取れなければ null)。
- GET は snapshot だけを結果の正本にする。可変な `improvement_backlog.last_result` や activity は読まない。

### 4.4 daemon: 発行と回収

- **出す人は supervisor 1 か所。** 手動 (`submit_manual`) と週次 (`_launch_slot`) の両方で、`prepare()` が完全に成功した直後に `started`、`commit()` または補償の後に `finished` を出す。pre-ready 失敗で再試行する週次は、試行ごとに (mission ごとに) 開始・終了の組を出す (終端した試行の `finished` が先、再試行の `started` が後)。
- **`finished` を出す前の確認** (補償自体が失敗して mission が `running` のまま残る場合に「終了」を誤表示しないため): 別の接続で、`missions.status != 'running'` かつ `improvement_runs.finished_at IS NOT NULL` かつ snapshot の行がある、をすべて確かめる。満たさなければ event を出さず、技術ログに固定語 `improve_notice_skipped reason=not_terminal mission=<id>` だけを書く (未終端の mission は次回起動の回収が終端し、そこで通知される)。
- **event の保存は best-effort** (`emit_best_effort`)。失敗しても mission は止まらない。再送しない。event の組立てで `EventSchemaError` になった場合も例外は握り、技術ログに固定語 `improve_notice_skipped reason=schema` を書く。at-least-once の重複は bot が event ID で落とす。improve は低頻度なので専用 thread は持たず、改善 loop の thread で別接続から書く。commit 後に出すので、通知の失敗や遅れが DB の終端に影響しない。commit 後・発行前に daemon が落ちた場合の `finished` は出ない (許容。次回起動の回収は終端済みの mission を再通知しない)。
- **再起動回収**: `missions.recover_interrupted` が、回収した improve mission の `(mission_id, scheduled)` を戻り値に足し、回収と同じ tx で snapshot `(failed, interrupted)` を書く。`scheduled` は `improve_wave_slots.mission_id` の行の有無で復元する。回収の commit 後、**ops service 構築で event store ができた直後・bot が読み始める前** (supervisor の tick 開始前) に、`finished` を 1 件ずつ best-effort で発行する。回収は 1 回の起動で 1 度しか走らないので、回収した mission が再び通知されることはない。`started` は出さない (すでに出ている)。
- **設定 `notify.improve_events`** (bool、既定 true)。rollback 用に生成を止められる。変更後は daemon の再起動が必要。止めても snapshot は書く (通知用の記録は常に残す)。

### 4.5 `prepare()` の Tx-0 後の失敗

`prepare()` は Tx-0 で mission / run を作った後に workspace・prompt・runner を構築する。この後段で失敗すると内部補償 (`_compensate_prepare_failure`) が mission を終端する。補償は snapshot `(failed, prepare_failed)` を同じ tx で書く。`prepare()` は mission ID を持つ専用例外 `ImprovePrepareFailed(mission_id, slot_key)` (元の例外を `__cause__` に連結) を送出し、supervisor はこれを受けて `finished` だけを発行したうえで、元の例外を再送出する (呼び出し側が見る例外は変わらない)。Tx-0 の前に失敗した場合は mission 行がなく、通知も出ない。**「started が出たのに mission 行がない」ことは起きない** (started は `prepare()` の完全成功後)。補償自体が失敗したときは mission が `running` のまま残り、§4.4 の確認で `finished` を出さず、次回起動の回収が通知する。

### 4.6 新 endpoint と親専有レポートの読み方

**新 endpoint `GET /v1/improve/runs/{mission_id}`** (scope は APPROVALS_DETAIL、読み取り、監査行なし)。返すのは、mission 行 (backend / model / 整数秒の `elapsed_sec`、終端前は null)、snapshot (trigger / result_code / reason_code / tool_calls / observation_text、終端前は null)、承認 (snapshot の `approval_id` に対する id / plugin 名 / kind)、床未達の数値。mission が存在しない・improve でない場合だけ 404 (終端前の mission は snapshot が null の 200。開始通知がこれで backend / model を得る)。応答は DTO で、パス・内部例外文面・鍵を含まない。

**床未達の数値の読み方** (`result_code=gate_failed` かつ `reason_code=unprofitable` のときだけ読む):

1. 親が信頼できる `data/improve_reports` を dirfd として開く (symlink 不可)。名前は `improve-YYYY-MM-DD-<同一 mission_id>.md` の単一 basename だけ (`improvement_runs.report_path` の basename がこの正規形に一致するか確かめ、一致しなければ読まない)。
2. dirfd 基準の `openat(O_RDONLY | O_NOFOLLOW | O_CLOEXEC)` で開き、`fstat` が regular file であること、サイズが 64 KiB 以下であること、全体が厳密な UTF-8 であることを確かめる。
3. 見出し (`# Improve Mission <id> — gate failed`)、`failed_stage:` 行 (`in_sample` / `holdout` のコンマ区切り)、`failed_pairs:` 行 (各 pair は `^[A-Z0-9_]{3,16}$`)、`### in_sample` / `### holdout` の表の列見出しを `_render_floor_report_detail` と同じ並びで厳密に照合する。pair 行の trades は 0 以上の整数、`pf` / `avg_r` は有限の浮動小数か `None` (null 扱い)、`evaluable` は `True` / `False` だけ。
4. どれか 1 つでも反すれば、数値の部分全体を捨てる (部分的に出さない)。応答の数値欄は null になり、bot は固定語だけの通知にする (§2.2)。

parser は `_render_floor_report_detail` と同じ module に置き、render → parse の往復を test で固定する。

### 4.7 afx-ops

- bot: `improve_run_changed` の template (§2) と、`improve_notices` の設定。詳細の取得は BotClient (`api_client.py`) の新しい GET method で、承認の plugin 名補完と同じ型 (要求全体の 3 秒 deadline、失敗時は §2.4 の全文)。`EventPlan.allow_mentions=False`。
- notify: 未知 code で止まらないよう、新 code を allowlist に足し、`deliver_improve_events` (既定 false、cursor は進める) を取引 event と同じ型で足す。webhook に送る場合も fields の固定語だけ。
- web: 無変更 (event 一覧が未知 code で落ちないことだけ確認する)。

### 4.8 配備順と再起動

取引通知と同じ。**afx-ops (bot / notify) を先に配って再起動 → daemon を再起動して emitter を有効にする。** 逆順だと、新 code を読んだ旧 bot / notify が停止する。daemon の再起動は mission の実行中を避ける (走行中の mission は再起動で中断され、中断は §4.4 の回収で `interrupted` として通知される)。rollback は `notify.improve_events: false` → daemon 再起動 → bot / notify が追いついてから afx-ops を戻す。`improve_notices` は bot の再起動で反映される。

## 5. 受入

| AC | 内容 |
|---|---|
| AC-1 | 5 結果 + 開始の本文の完全一致。実 mission (#2040 / #2043 / #2044 / #2045 / #2046) のレポートと DB 行を fixture にし、bot の描画結果を §2 の文面と 1 文字単位で比べる (所要は §2.2 の丸め規則、#2040=4分49秒 / #2043=3分00秒 / #2044=4分39秒 / #2045=5分06秒 / #2046=6分46秒)。tool 回数は #2040 / #2043 / #2044 / #2045 が activity の記録、#2046 は記録なしの `tool -` (snapshot の `tool_calls=null`)。submitted と report は合成 fixture。tool_calls が非 null の補償経路は生の `MissionResult` fixture で別に固定。§2.4 の取得失敗の全文 (開始 + 5 結果 × 404 / 5xx / deadline 超過) も完全一致で固定する |
| AC-2 | 床未達の数値: 段 (in_sample のみ / holdout まで)、pair 3 本超、`evaluable` の真偽、pf / avg_r が null (`pf -`)、§4.6 の検査の各項目 (レポート欠落・symlink・basename 不一致・配下外・65 KiB 超・UTF-8 不正・見出し / 列の不一致・`nan` / `inf`・負の trades・形式不正) で、数値行が出る / 全体が省かれて固定語だけになる境界を押さえる |
| AC-3 | canary を 2 本に分ける。(a) **秘密 canary**: レポートと agent 出力に URL・鍵様の文字列を入れて mission を終端させ、activity.log、backlog (`last_result` / `idea` / `origin_outcome`)、次回 mission のプロンプト全文、`ops_events` の全行、終端 snapshot、監査・idempotency 表、daemon の技術ログ、API server のログ、bot の state / retry 構造 / ログ、notify の送信 payload、**Discord の描画結果**のいずれにも出ないこと。(b) **数値 canary**: 目印の数値 (例 `7.7777`) をレポートの holdout に入れ、上の全 sink に出ず、**Discord の描画結果にだけ**出ること。加えて静的検査で、sandbox・prompt builder・RPC handler・staging の module が `improve_run_notices` を参照しないことを確かめる |
| AC-4 | 観察文: 300 字ちょうど / 超過 (`cut_escaped(value, 300)` と同じく escape 後の 300 字以内、escape の単位の途中で切らない、「…」込み)、改行・制御文字、`**` `@everyone` `<@id>` コードフェンス、URL を含む文が、1 行・無効化・切詰めで描画される。`not_submitted` は固定文。improve 通知の `EventPlan.allow_mentions` が False で、送信に `AllowedMentions.none()` が渡る (設定済み mention があっても付かない) |
| AC-5 | event の専用検査 (§4.1): state / trigger / result_code / reason_code / tool_calls の各 allowlist。`reason_code` に `pf:7.7777` / `:` / 数字 / 例外文面 / 表に無い語 / 許可対でない組を入れると `EventSchemaError`、`tool_calls` に負数・bool・小数・文字列を入れても同様。変換表 (§4.2) の全行について、site が表のとおりの (result_code, reason_code) を snapshot に書く (AST で `finish_improve_mission` の全呼び出しと補償・回収の網羅を確かめる)。`gate_failed:max_bars_limit:<exc>` / `commit_crashed:OSError:...` / `insufficient_trades:16` から例外文面と数が出ない |
| AC-6 | 因果を 2 つに分ける。(a) **daemon 側**: bot 停止・event の INSERT 失敗・`EventSchemaError` のいずれでも、mission の終端状態 (missions / improvement_runs / slot / snapshot) と activity が変わらない。(b) **afx-ops 側**: `channel.send` が 5xx のとき cursor を進めず、次の周回で同じ event を再送する (GET から取り直す)。詳細取得の失敗は §2.4 の全文で投稿が続く。両方を満たして「Discord の失敗が mission に戻らない」を示す |
| AC-7 | 手動と週次の両方で started / finished が 1 回ずつ出る。補償経路 (commit 例外、launch 例外) でも finished が 1 回出る。**Tx-0 の後・`prepare()` の return 前の例外**で finished だけが 1 回出て started は出ない (`ImprovePrepareFailed`、呼び出し側には元の例外が見える)。補償自体が失敗して mission が `running` のままのとき finished が出ず、技術ログに固定語 (`not_terminal`) が残る。重複 event は bot が 1 通に落とす |
| AC-8 | 再起動回収: running の improve mission を持つ DB で回収すると、snapshot `(failed, interrupted)` と `(mission_id, scheduled)` が返り、event store 構築直後・bot の読み始め前に `finished` が 1 件ずつ出る。trigger は `improve_wave_slots.mission_id` の有無で週次 / 手動に復元される。回収の再実行（2 回目の起動）では再通知されない |
| AC-9 | 配備順: 新 code を含む event が先にあっても、afx-ops 配備済みの bot / notify は止まらない。`notify.improve_events: false` で event が生成されない (snapshot は書かれる) |
| AC-10 | `improve_notices` の 3 値で投稿が変わる。値の変更は bot の再起動で反映される (再起動なしでは変わらないことを test で固定する)。daemon 側は `notify.improve_events` の変更に再起動が要る |
| AC-11 | 新 endpoint は notify と agent の経路から呼べず、APPROVALS_DETAIL を持つ principal だけが読める。応答に鍵・パス・内部例外文面が出ない。監査行を書かない。終端前の mission は snapshot が null の 200、存在しない / improve でない mission は 404 |
| AC-12 | 終端 snapshot の不変性と同一 tx: UPDATE / DELETE が拒否される。`finish_improve_mission` の CAS が失敗した (既に終端済み) 補償では snapshot が増えない。tx が rollback したとき snapshot も残らない。後から backlog の `last_result` を書き換えても GET の観察文が変わらない |

変異スイープの対象 (最低限): 観察文の escape 除去、300 字上限の除去、レポート path / basename 検査の除去、レポートの byte 上限・`O_NOFOLLOW` の除去、数値の event fields への混入、`last_result` / activity / snapshot への数値混入、event 専用検査の各 allowlist の除去、reason_code を文面から切り出す実装への退行 (site 定数の置換)、snapshot を CAS の外で書く、`notice` 引数の既定値化、started の二重発行、commit 例外時の finished 欠落、`not_terminal` 確認の除去、再起動回収の通知の除去、`allow_mentions` の除去、GET の deadline 除去。

## 6. 変更一覧と task 分割

純 Python のみ。実 kernel・systemd は触らない。

| task | 範囲 | 規模 | 実装 |
|---|---|---|---|
| T1 daemon | DB migration (`improve_run_notices` + 不変 trigger + 旧 fixture / byte 一致 test)、`improve_notice_codes` (allowlist と許可対)、`store/missions.py` (`finish_improve_mission` の `notice` 引数、`recover_interrupted` の戻り値と snapshot 書込み)、`improve_loop.py` (全終端 site の定数渡し、`ImprovePrepareFailed`、補償への `tool_calls`、floor report の parser)、`core/improve_supervisor.py` (started / finished の発行と `not_terminal` 確認)、daemon 起動時の回収通知の配線 (`ops/service.py`、event store 構築直後)、`ops/events.py` (新 code と専用検査)、`ops/service.py` (新 endpoint)、`config.py` / `settings.yaml.example` (`notify.improve_events`)、`contracts` の endpoint 表、`cli_surface.py` の記載確認、AC-2・3・5・6a・7・8・9・11・12 | 約 650 行 + test 800 行 | terra |
| T2 afx-ops | `gate/render.py` (template と §2.4 の全文)、`config.py` (`improve_notices`、`deliver_improve_events`)、`api_client.py` の BotClient の新 GET method (要求全体の deadline)、bot の詳細取得と worker ローカル値の扱い、notify の allowlist、`ops_setup` writer の key 保持、AC-1・3 (bot / notify 側の sink)・4・6b・9・10 | 約 300 行 + test 400 行 | terra |

T1 と T2 は event の fields と GET の応答 DTO を契約に並行できる。T2 の fixture は T1 が出す実レポートから作る。文書 `docs/operations/ops-notices.md` に節を足す (配備順、設定、注記)。T1 の migration と全終端 site の変更は規模が大きいので、着手時に 2 束 (migration + store + 変換表、supervisor + 回収 + endpoint) に割ってよい。

## 7. 裁定が要る点

技術で決まるものは推奨で決める。ユーザー裁定が要るのは次の 2 点。

1. **開始通知の要否と既定。** 推奨: 依頼どおり実装し、既定 `all`、`improve_notices = "results"` で終了のみに絞れる。根拠: 週次は無人なので開始の可視化に意味があり、現設定 (`improve.parallel: 1`) では 1 周 1 本で通知疲れにならず、「開始あり・終了なし」が異常終了や停止を切り分ける唯一の手掛かりになる。要らなければ実装の差は設定 1 値で済む。
2. **床未達の数値の粒度。** 推奨: 落ちた段・落ちた pair の pf / avg_r / 取引数 / 評価不能 (依頼どおり)。in_sample の数値、win_rate、max_drawdown、total_pnl は載せない。根拠: pf・avg_r・取引数・評価可否は床の判定そのものの入力なので「なぜ落ちたか」はこれで足り、それ以外を載せるほど運用者が方針へ書き写す数値が増える。holdout の数値を人間が見ることは、遮断 8 (agent に見せない) の範囲外だが、§3 の 8 の注記を運用文書に入れる前提。

以下は推奨で決めた (裁定不要)。観察文の上限は 300 字 (依頼どおり、切詰めは表示のみで保存は不変)。plugin 名は submitted のみ。課題 (idea) は載せない (`improve-run-summary-for-humans` の範囲)。

## 8. 確認事項 (既存コードとの食い違い・未確認)

無し。v0.1 の 6 件は既存コードの確認で確定し、§0.2 に移した。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 | 初版 | チケット `improve-start-and-result-to-discord` の設計 | |
| 2026-10-25 | v0.2 | event の専用意味検査と (result_code, reason_code) の変換表 (§4.1・§4.2)、mission 終端 tx と同時に書く不変な終端 snapshot を通知の正本に (§4.3)、再起動回収の通知 (§4.4)、`prepare()` 後段失敗の `ImprovePrepareFailed` (§4.5)、`finished` 前の終端確認 (§4.4)、取得失敗時の全文と要求全体の deadline (§2.4)、bot のローカル値の所有期間と canary の分離 (§3-7・AC-3)、report 読取の厳密規則と null 表示 (§4.6)、秒の丸め規則と実データに合わせた例 (§2.2・AC-1)、AC-6 の因果分割、task 範囲の拡張 (§6)、`cut_escaped` / `allow_mentions=False` の明記、§8 を確定事項 (§0.2) へ移動 | 設計レビュー 1 周目 (sol、Critical 2 / Important 10 / Minor 3 + §8 回答 6 件、全件採用。詳細は `tmp/review-discord/c0-r1-triage.md`) | |
