# afx の direct exec user unit 運用

`afx init` は `~/.config/systemd/user/afx.service` を設置します。unit は `WorkingDirectory=<root>` を指定し、`main.py --daemon` を direct exec します。対話シェルや screen は使いません。

ログアウト後も動かすには user linger が必要です。`afx init` の案内に従って確認・有効化し、状態は `loginctl show-user "$USER" -p Linger` で確認します。

日常操作:

```bash
systemctl --user status afx
systemctl --user restart afx
journalctl --user -u afx
afx ctl status
```

承認、方針、改善の操作は `afx ctl` を入口にします。bot / web / notify は `afx ops <feature> enable` で有効化します。詳細は [操作 API と afx ctl の運用](ops-api-2026-10-09.md) を参照してください。

起動ガードは、ops の有効化が途中なら rc 75、初期設定の確定途中なら rc 76 でサービスを停止します。marker を残した操作を同じ入口から再実行して収束させてください。

停止または再設置するときは `systemctl --user stop afx` の後に `afx init` を再実行します。初回設定を作り直すには `scripts/first-run-reset.sh` を使います。
