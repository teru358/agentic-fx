# 改善 mission の開始・終了通知 (Discord) 設計書

**版:** v0.1 (C0 初版)

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
3. **自由記述を載せる経路は event にできない。** event の値は `[A-Za-z0-9_.:+-]` の 64 字以内に固定されている (`_SAFE_VALUE`)。日本語の観察文も数値表も入らない。かつ event は DB に残り、notify の webhook (外部送信) でも読まれる。したがって **event は「起きた」という事実と固定語だけ運び、本文の材料は bot が API で取りに行く** (承認の plugin 名補完と同じ型)。
4. **開始通知の要否。** 週次は 1 周あたり少数、手動は本人が起動する。開始通知の価値は「無人の週次が動き出した」ことの把握と、終了通知が来ないときの切り分けに限られる。ユーザー依頼なので実装するが、bot 側で `results` に絞れる設定を付ける (§7 裁定 1)。
5. 手動 improve は既存の `job_state_changed` (job_kind=improve) も出す。既定で job 成功は非表示、失敗は表示される。mission の例外終了では「job 失敗」と「改善失敗」の 2 通になり得る。許容する (§7 確認事項 3)。

## 1. 用語

- **mission**: 改善 loop の 1 回の実行 (`missions.id`)。手動 (`afx ctl improve`) と週次 (wave の slot) がある。
- **結果の 5 種** (`result_code`):
  - `submitted`: 承認依頼を出した (`approval_requested`)。
  - `observation`: **agent の申告**で提出をやめた (`mission_observation`。文は agent が書いた)。
  - `gate_failed`: **システムの判定**で見送った (`gate_failed`。床未達 `unprofitable`、取引数不足、評価不能、sandbox 違反などの固定語)。
  - `failed`: mission が失敗した (`mission_failed`、`output_invalid`、sandbox 利用不能)。
  - `report`: report を公開して終えた (`report_published`)。
- **reason_code**: `gate_failed` と `failed` の固定語 (小文字英数と `_`、32 字以内)。agent や例外の文面を含まない。
- **親専有レポート**: `data/improve_reports/improve-<日付>-<mission>.md`。holdout の数値を含み、agent には見せない (遮断 8)。

## 2. 利用者から見た動き

mission の開始時に 1 通、終了時に 1 通、同じ channel に投稿される (mention は付けない)。取引通知と同じ「1 行目に要点、続く行に補足」の形。

### 2.1 開始

```text
[改善] 開始 #2043 (手動)
backend codex / gpt-5.6-luna
```

`(手動)` は `afx ctl improve`、`(週次)` は wave の slot。backend / model は daemon の mission 行から取り、bot が表示用に escape する。課題は載せない (§0.1)。

### 2.2 終了 (結果ごと)

共通: 1 行目は結果、最終行は `所要 <m分s秒> / tool <n> 回`。tool 回数が無ければ省く。所要は mission の開始・終了時刻の差。以下は 2026-10-25 の実 mission での例。

**observation** (#2040、agent の申告):

```text
[改善] 提出なし (agent の申告) #2040
agent の申告: session_ranges によるロンドン・NY開始足のレンジ外ブレイク後回帰を実装し、固定出口と短縮出口でUSDJPY 1hを検証した。どちらも不採算判定となり、提出しなかった。次回は時間帯回帰以外の条件、または別の市場仮説を優先する。
所要 4分50秒 / tool 11 回
```

申告は 300 字で切って「…」を付ける。この文は agent が書いたもので、システムの判定ではないことをラベルで示す。重複・系統見送りで `observation:not_submitted` の mission は「agent の申告なし (重複または系統の見送り)」の固定文にする。

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

数値は落ちた段の、落ちた pair の行だけ (pair は最大 3 本、超えたら「ほか n 本」)。pf と avg_r は小数 2 桁、avg_r は符号付き。`evaluable=false` は「評価不能」、true は付けない。段が in_sample だけで落ちたときは holdout の行は出さない (レポートが holdout を持たないため)。#2043 のように pf が 1 を超えても評価不能で落ちることがあるので、「評価不能」を落ちた理由として必ず並べる。レポートが無い・読めない・形式が想定外のときは数値行を省き、固定語だけにする。床未達以外の `gate_failed` (取引数不足、`loader_rejected` など) も固定語だけ:

```text
[改善] 見送り #2044 (insufficient_trades)
所要 2分10秒
```

**failed** (#2046。実際はディスク quota 超過による commit 失敗):

```text
[改善] 失敗 #2046 (commit_crashed)
所要 6分46秒 / tool 12 回
```

例外の文面 (`Disk quota exceeded` など) は出さない。理由語だけ。

**submitted** (例。実例は承認 0 件のため構成):

```text
[改善] 候補を提出 #2050
plugin <name> (strategy) / 承認 #26
所要 5分30秒 / tool 14 回
```

承認の中身は既存の承認 message が担う。ここは重ねない。**report** は `[改善] レポートを公開 #<id>` に所要を添える (数値なし)。

### 2.3 設定と見えない場合

- bot の `ops.toml` に `improve_notices = "all" | "results" | "off"` を足す (既定 `all`、`results` は終了のみ)。
- 詳細の取得に失敗・3 秒で期限切れのときは、観察文と数値を省いた固定語の通知で投稿を続ける (承認の plugin 名補完と同じ)。
- 初回の正常 poll は取引通知と同じく、配備前の event を配送しない。

## 3. 秘密と遮断

通知は人間だけに届く経路である。agent への戻り経路を持たないことを、データの流れで示す。

```text
mission 終端 ──(daemon)──▶ ops_events: improve_run_changed
                              fields = state / trigger / result_code / reason_code / tool_calls
                              (固定語と整数だけ。観察文・数値・URL は入らない)
bot ──events.read──▶ event を受ける
bot ──GET /v1/improve/runs/{id}──▶ daemon が DB と親専有レポートから組み立てて返す (APPROVALS_DETAIL scope)
bot ──escape・切詰め──▶ Discord に投稿。本文はどこにも保存しない (bot の cursor snapshot は無変更、ログに本文を出さない)
```

要点:

1. **holdout の数値は Discord にだけ出る。** activity の reason、backlog の `last_result`、`idea`、次回プロンプトには、従来どおり流さない。`_finalize_gate_failed` の `last_result` / activity 行は変更しない (固定語と閾値 3 値だけ)。数値は event の fields にも入れない。event は notify の webhook でも読まれ得るため。
2. **API の応答は、承認詳細が既に返している情報と同じ水準。** `approval_detail` は `in_sample` / `holdout` と agent の申告を APPROVALS_DETAIL scope に返している。新 endpoint は同じ scope を使う (bot / web / operator / approver のみ、notify は読めない)。agent 側の鍵はなく、mission の sandbox から daemon へ到達する経路は無い。
3. **agent 由来の文字列は 1 項目 (観察文) だけ。** 保存済みの `last_result` (`safe_text` で URL・鍵様の文字列を抑止済み) から取り、daemon 側でも `display_text` 相当で制御文字・改行を畳み、bot 側で `escape_discord` (整形文字、mention、コードフェンス、URL の autolink を無効化) をかけ、300 字で切る。plugin 名は loader の文法 (`^[a-z][a-z0-9_]{0,63}$`) を通ったものだけ。agent 由来の URL・鍵・口座は、保存時と表示時の二重の抑止で通さない。
4. **mention は付けない。** 取引通知と同じ。本文由来の mention も escape で無効化する。
5. **失敗の理由は固定語。** `result.reason` や例外の文面 (OS のエラー文など) は通知に出さない。
6. **人間が通知の数値を `afx ctl policy add` や backlog に書き写すのは、人間の判断。** 書いた内容は従来の directive の規律に従う。通知はこれを防げないので、運用文書に「holdout の数値を方針に書かない」と注記する。

## 4. 配線

### 4.1 daemon

- **新 event code `improve_run_changed`** (fields: `state` = `started` / `finished`、`trigger` = `manual` / `scheduled`、`result_code`、`reason_code`、`tool_calls`。ref は `improve:<mission_id>`)。`ops/events.py` の `FIELDS` と `_REF` に足す。値は `_SAFE_VALUE` の範囲で、取引 event のような独自 grammar は要らない。
- **出す人は supervisor 1 か所。** 手動 (`submit_manual`) と週次 (`_launch_slot`) の両方で、`prepare()` 成功直後に `started`、`commit()` 後 (例外・補償を含む) に `finished` を出す。`ImproveLoop` は終端の各 site (activity を書く 8 か所) で `ctx` に結果と理由語を 1 行ずつ記録し、supervisor がそれを読んで 1 回だけ出す。記録が無いまま終わった mission は `failed` / `unknown` として出す。commit 後に出すので、通知の失敗や遅れが DB の終端に影響しない。
- **event の保存は best-effort。** 失敗しても mission は止まらない (`emit_best_effort`)。再送しない。at-least-once の重複は bot が event ID で落とす。improve は低頻度なので取引 event のような専用 thread は持たず、改善 loop の thread で別接続から書く。
- **新 endpoint `GET /v1/improve/runs/{mission_id}`** (scope は APPROVALS_DETAIL、読み取り、監査行なし)。返すのは mission 行 (backend / model / 開始・終了時刻)、起動契機、結果、観察文 (backlog 行 `last_result` が `observation:` で始まるときだけ。それ以外は null)、承認 (id / plugin 名 / kind)、床未達の数値 (親専有レポートの `failed_stage` と落ちた段の pair 別行)。レポートは `improvement_runs.report_path` が `data/improve_reports` 配下であることを `O_NOFOLLOW` で確かめて読む。数値の取り出しは `_render_floor_report_detail` と同じ module に parser を置き、render → parse の往復を test で固定する。
- **設定 `notify.improve_events`** (bool、既定 true)。rollback 用に生成を止められる。変更後は daemon の再起動が必要。

### 4.2 afx-ops

- bot: `improve_run_changed` の template (§2) と、`improve_notices` の設定。詳細の取得は承認の plugin 名補完と同じ型 (3 秒、失敗時は名前なし)。
- notify: 未知 code で止まらないよう、新 code を allowlist に足し、`deliver_improve_events` (既定 false、cursor は進める) を取引 event と同じ型で足す。webhook に送る場合も fields の固定語だけ。
- web: 無変更 (event 一覧が未知 code で落ちないことだけ確認する)。

### 4.3 配備順と再起動

取引通知と同じ。**afx-ops (bot / notify) を先に配って再起動 → daemon を再起動して emitter を有効にする。** 逆順だと、新 code を読んだ旧 bot / notify が停止する。daemon の再起動は mission の実行中を避ける (走行中の mission は再起動で中断される)。rollback は `notify.improve_events: false` → daemon 再起動 → bot / notify が追いついてから afx-ops を戻す。`improve_notices` は bot の再起動で反映される。

## 5. 受入

| AC | 内容 |
|---|---|
| AC-1 | 5 結果 × 本文の完全一致。実 mission (#2040 / #2043 / #2045 / #2046) のレポートと DB 行を fixture にし、bot の描画結果を §2 の文面と 1 文字単位で比べる。submitted と report は合成 fixture |
| AC-2 | 床未達の数値: 段 (in_sample のみ / holdout まで)、pair 3 本超、`evaluable` の真偽、pf が null、レポート欠落・symlink・形式不正・配下外 path で、数値行が出る / 省かれる / 固定語だけになる境界を押さえる |
| AC-3 | canary: レポートに URL・鍵様の文字列・目印の数値 (例 `7.7777`) を入れて mission を終端させ、activity.log、backlog (`last_result` / `idea` / `origin_outcome`)、次回 mission のプロンプト全文、`ops_events` の全行、監査・idempotency 表、bot の snapshot とログ、notify の送信 payload のいずれにも出ないこと。Discord 側 (描画結果) にだけ出ること |
| AC-4 | 観察文: 300 字ちょうど / 超過、改行・制御文字、`**` `@everyone` `<@id>` コードフェンス、URL を含む文が、1 行・無効化・切詰めで描画される。`not_submitted` は固定文 |
| AC-5 | failed の reason_code に例外文面 (OS エラー、パス、`result.reason`) が入らない。`gate_failed:max_bars_limit:<exc>` のような値は先頭語だけになる |
| AC-6 | bot が落ちている・Discord が 5xx・詳細取得が期限切れ・event 保存が失敗のいずれでも、mission は通常どおり終端し、DB の終端状態と activity が変わらない。詳細取得失敗は固定語の通知で投稿が続く |
| AC-7 | 手動と週次の両方で started / finished が 1 回ずつ出る。補償経路 (commit 例外、launch 例外) でも finished が 1 回出る。重複 event は bot が 1 通に落とす |
| AC-8 | 配備順: 新 code を含む event が先にあっても、afx-ops 配備済みの bot / notify は止まらない。`notify.improve_events: false` で event が生成されない |
| AC-9 | `improve_notices` の 3 値で投稿が変わる。値の変更は bot の再起動で反映される (再起動なしでは変わらないことを明記し、反映の可否を test で固定する)。daemon 側は `notify.improve_events` の変更に再起動が要る |
| AC-10 | 新 endpoint は notify と agent の経路から呼べず、APPROVALS_DETAIL を持つ principal だけが読める。応答に鍵・パス・内部例外文面が出ない |

変異スイープの対象 (最低限): 観察文の escape 除去、300 字上限の除去、レポート path 検査の除去、数値の event fields への混入、`last_result` / activity への数値混入、reason_code の先頭語切り出し除去、started の二重発行、commit 例外時の finished 欠落。

## 6. 変更一覧と task 分割

純 Python のみ。実 kernel・systemd は触らない。

| task | 範囲 | 規模 | 実装 |
|---|---|---|---|
| T1 daemon | `ops/events.py` (新 code)、`improve_loop.py` (終端 site で ctx に記録、floor report の parser)、`core/improve_supervisor.py` (started / finished の発行)、`ops/service.py` (新 endpoint)、`config.py` / `settings.yaml.example` (`notify.improve_events`)、`contracts` の endpoint 表、`cli_surface.py` の記載確認、AC-2・3・5・6・7・10 | 約 350 行 + test 450 行 | terra |
| T2 afx-ops | `gate/render.py` (template)、`config.py` (`improve_notices`、`deliver_improve_events`)、bot の詳細取得、notify の allowlist、`ops_setup` writer の key 保持、AC-1・4・8・9 | 約 250 行 + test 350 行 | terra |

T1 と T2 は event の fields を契約に並行できる。T2 の fixture は T1 が出す実レポートから作る。文書 `docs/operations/ops-notices.md` に節を足す (配備順、設定、注記)。

## 7. 裁定が要る点

技術で決まるものは推奨で決める。ユーザー裁定が要るのは次の 2 点。

1. **開始通知の要否と既定。** 推奨: 依頼どおり実装し、既定 `all`、`improve_notices = "results"` で終了のみに絞れる。根拠: 週次は無人なので開始の可視化に意味があり、頻度は少数で通知疲れにならない。要らなければ実装の差は設定 1 値で済む。
2. **床未達の数値の粒度。** 推奨: 落ちた段・落ちた pair の pf / avg_r / 取引数 / 評価不能 (依頼どおり)。in_sample の数値、win_rate、max_drawdown、total_pnl は載せない。根拠: 「なぜ落ちたか」は pf・avg_r・評価可否で足り、載せる項目を増やすほど運用者の方針への書き写しが増える。holdout の数値を人間が見ることは、遮断 8 (agent に見せない) の範囲外だが、§3 の 6 の注記を運用文書に入れる前提。

以下は推奨で決めた (裁定不要)。観察文の上限は 300 字 (依頼どおり、切詰めは表示のみで保存は不変)。plugin 名は submitted のみ。課題 (idea) は載せない (`improve-run-summary-for-humans` の範囲)。

## 8. 確認事項 (既存コードとの食い違い・未確認)

1. 週次の slot が 1 周あたり何本走るか (wave の M) は設定から読み取れていない。通知量の見積りは実機で確かめる。
2. `improvement_runs.report_path` が floor 以外の `gate_failed` でも常に埋まるか。埋まらない場合は §2.2 のとおり固定語だけになる。
3. 手動 improve の mission 例外終了で `job_state_changed` (失敗) と `improve_run_changed` (failed) が重なる。bot 側で improve の job 失敗を抑止するかは T2 着手時に決める (推奨: 抑止しない)。
4. 新 endpoint が GET として監査行を書かないこと (`approval_detail` と同じ)。T1 で確認する。
5. `missions` の trigger 列は improve では NULL。手動 / 週次の区別は ctx の `slot_key` の有無で行う (supervisor の 2 経路が別々に呼ぶため、経路で決まる)。
6. mission 実行中の daemon 再起動で `finished` が出ない場合の回復 (起動時の孤児 mission 回収が `mission_failed` を書く経路があるか) は未確認。無ければ通知は出ないまま許容する (通知は best-effort)。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 | 初版 | チケット `improve-start-and-result-to-discord` の設計 | |
