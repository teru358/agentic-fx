---
id: ops-bot-exits-not-lock-holder
title: afx-ops bot が稼働中に not_lock_holder で終了し systemd が再起動する
status: 設計待ち
priority: 中
opened: 2026-10-22
closed: null
related: [ops-bot-initial-start-replays-all-events]
---
# [ops-bot-exits-not-lock-holder] afx-ops bot が稼働中に not_lock_holder で終了し systemd が再起動する

**状態**: 設計待ち / **優先**: 中

## 現象

afx-ops@bot が daemon 稼働中に `afx-ops: not_lock_holder` (exit 3) で終了し、systemd の Restart で 5 秒後に戻る。2026-10-15 23:17、10-16 19:01、10-21 19:28 の 3 回 (起動から 30 分〜21 時間後)。同時刻の daemon ログに異常なし。判定箇所は afx_ops/server_auth.py の `lock_holders(...) != {pid}` で、instance lock の保持者が daemon 1 個に確定できなかった瞬間がある。仮説: daemon の子 process が lock の fd を継承して保持者に見える、または別の afx コマンドが一時的に lock を取る。自己回復はするが、再起動のたびに通知の遅延と restart counter の増加が起きる。起動直後の `discord_unavailable` (network 未起動) は別事象。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-22: 起票。
