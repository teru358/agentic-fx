---
id: ops-bot-exits-not-lock-holder
title: afx-ops bot が稼働中に not_lock_holder で終了し systemd が再起動する
status: 設計待ち
priority: 高
opened: 2026-10-22
closed: null
related: [ops-bot-initial-start-replays-all-events]
kind: 不具合
---
# [ops-bot-exits-not-lock-holder] afx-ops bot が稼働中に not_lock_holder で終了し systemd が再起動する

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

afx-ops@bot が daemon 稼働中に `afx-ops: not_lock_holder` (exit 3) で終了し、systemd の Restart で 5 秒後に戻る。2026-10-15 23:17、10-16 19:01、10-21 19:28 の 3 回 (起動から 30 分〜21 時間後)。同時刻の daemon ログに異常なし。判定箇所は afx_ops/server_auth.py の `lock_holders(...) != {pid}` で、instance lock の保持者が daemon 1 個に確定できなかった瞬間がある。仮説: daemon の子 process が lock の fd を継承して保持者に見える、または別の afx コマンドが一時的に lock を取る。自己回復はするが、再起動のたびに通知の遅延と restart counter の増加が起きる。起動直後の `discord_unavailable` (network 未起動) は別事象。

## 原因

`/proc/locks` は 1 回の read で 1 page (約 70 行) しか返さず、lock の取得・解放が重なる間は読む度に行が抜ける (seq_file の位置が行の入れ替わりでずれる)。保持者 (daemon) の行が抜けた読みが `lock_holders` を空にし `!= {pid}` で終了していた。実測: lock を 8 process で入れ替えながら安定した保持行を読み続けると 1.0〜9.6% の読みで行が抜ける。判定 (pytest) の最中に lock が増えて 1 page を超える。bot の終了時刻 11-13 以降 18 件中 17 件が judge の実行区間に重なる (11-21 は 11 件すべて)。

## 処置案・裁定

保持者が見えない読みは最大 8 回まで読み直す (1 回の抜け率 1 割未満なら 8 回続く確率は 1e-8 未満)。保持者を確かめられないときに通すことはしない。

## 修正内容

afx-ops `server_auth.lock_holders` / `check_lock_device` と ctl 側 `client.lock_holders` に読み直しを入れた。

## 経緯

- 2026-10-22: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与
- 2026-10-29: 優先: 高 — 2026-10-29 棚卸し: 承認通知・承認ボタンの経路 (afx-ops bot) が稼働中に落ち再起動を繰り返す。「承認の中身を見せる」の経路が不安定
