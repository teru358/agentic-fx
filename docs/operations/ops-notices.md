# ops event 通知の運用

bot の `ops.toml` では、任意 key `ops_notices` で ops event 通知を選べます。key がない旧設定は `default` で起動し、不正な値は起動時に拒否されます。

| key | 値 | 既定 | 意味 |
|---|---|---|---|
| `ops_notices` | `default` / `all` | `default` | `default` は承認状態、guard、サービス健全性、および失敗・取消・停止の job だけを投稿します。`all` は job の完了・受付・実行中、操作要求の終了、event stream health も投稿します。event stream health は汎用の「イベント … が記録されました。」という文面です。投稿しない既知 event も cursor は進みます。 |

```toml
ops_notices = "all"
```

既定で出さない監査 event は、web の活動ページと `afx ctl events` で確認できます。通知には principal や決定者を表示しません。

## 改善 mission の通知

改善 mission ごとに、bot が同じ channel へ 2 通投稿します (mention は付きません)。開始時に 1 通、終了時に 1 通です。

開始は `[改善] 開始 #<id> (手動)` と backend / model の 2 行です。週次の起動は `(週次)` と出ます。

終了は 1 行目が結果の見出し、続いて本文行、最終行が `所要 <m分ss秒> / tool <n> 回` です。結果ごとの見え方は次のとおりです。

```text
[改善] 提出なし (agent の申告) #2040
agent の申告: (agent が書いた観察文。300 字まで)
所要 4分49秒 / tool 11 回

[改善] 床未達で見送り #2043 (unprofitable)
落ちた段: holdout
holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)
所要 4分21秒 / tool 10 回

[改善] 失敗 #2046 (commit\_crashed)
所要 6分45秒 / tool -
```

「agent の申告」は agent の自己申告で、システムの判定ではありません。床未達の数値行は、落ちた段の、落ちた pair だけです (通った段の数値は出ません)。失敗の理由は固定語だけで、例外の文面は出ません。候補を提出した場合は `plugin <name> (<kind>) / 承認 #<id>` の行が付き、承認の中身は承認通知が担います。

### 設定

| 場所 | key | 既定 | 意味 |
|---|---|---|---|
| bot の `ops.toml` | `improve_notices` | `all` | `all` は開始と終了、`results` は終了のみ、`off` は投稿しません。bot の再起動で反映されます。 |
| `config/settings.yaml` | `notify.improve_events` | `true` | `false` で daemon が改善 mission の event (`improve_run_changed`) を作りません (rollback 用)。daemon の再起動で反映されます。 |

notify (webhook) へ配送するかは `ops.toml` の `deliver_improve_events` (既定 `false`) です。初回の正常 poll は、配備前の event を配送しません。

### 配備順

afx-ops (bot / notify) を先に配って再起動し、その後に daemon を再起動します。逆順にすると、新しい event を読んだ旧 bot / notify が止まります。daemon の再起動は mission の実行中を避けてください。実行中の mission は再起動で中断され、`interrupted` として通知されます。rollback は `notify.improve_events: false` にして daemon を再起動し、bot / notify が追いついてから afx-ops を戻します。

### 注意

- holdout の数値は人間にだけ届きます。`afx ctl policy add` で directives.md に書いたり、backlog の文に書き写したりしないでください。書いた内容は agent の次回プロンプトに流れ、holdout を agent に見せない遮断 8 の外から数値が入ります。
- 観察文は agent の自己申告です。内容を鵜呑みにして方針へ転記しないでください。

### 障害時

- 詳細を取得できないとき (要求全体で 3 秒、404 / 5xx / 接続失敗など)、投稿は止まらず簡略形になります。終了は見出しと、`tool_calls` が分かれば `tool <n> 回`、最終行に `(詳細を取得できませんでした)` だけです。開始は 1 行目だけです。観察文・数値・所要は出ません。
- 本文が 2000 字を超えるときは、見出しと `(本文を省略)` だけを投稿します。
- daemon が event を保存できなかったときは、技術ログに `improve_notice_emit_failed mission=<id> state=<started|finished>` が 1 行残ります。終了の event は、起動時と scheduler の tick ごと (約 60 秒) の掃き出しが、終端の記録を元に作り直します。掃き出しの対象は 7 日以内に終端した mission で、`notify.improve_events: false` の間の分は戻した後に 7 日分までまとめて出ます。開始の通知は作り直しません。重複して届いた event は bot が ID で落とします。
