# ops event 通知の運用

bot の `ops.toml` では、任意 key `ops_notices` で ops event 通知を選べます。key がない旧設定は `default` で起動し、不正な値は起動時に拒否されます。

| key | 値 | 既定 | 意味 |
|---|---|---|---|
| `ops_notices` | `default` / `all` | `default` | `default` は承認状態、guard、サービス健全性、および失敗・取消・停止の job だけを投稿します。`all` は job の完了・受付・実行中、操作要求の終了、event stream health も投稿します。投稿しない既知 event も cursor は進みます。 |

```toml
ops_notices = "all"
```

既定で出さない監査 event は、web の活動ページと `afx ctl events` で確認できます。通知には principal や決定者を表示しません。
