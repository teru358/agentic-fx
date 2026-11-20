---
id: account-snapshot-future-ts-treated-fresh
title: 時計が戻ると未来時刻の口座 snapshot が新鮮と扱われ続ける
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-19
closed: null
related: [web-ui-charts-equity-indicators]
---
# [account-snapshot-future-ts-treated-fresh] 時計が戻ると未来時刻の口座 snapshot が新鮮と扱われ続ける

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

`S/core/accounting.py` の `current_account` は `now − ts > 10 分` だけを陳腐化とみなし、ts が未来 (差が負) の行を新鮮とする。`record_snapshot` は `now < 最新の ts` の挿入を拒否するので、壁時計が大きく戻る (NTP の補正、手動の変更) と新しい snapshot が書けず、古い equity が時計が追いつくまで新鮮として DD 判定・約定停止の判定に使われる (fail open)。performance API の今の状態も同じ関数を使うので同じ値を出す。直す方向: 未来の ts を陳腐化 (fail closed) として扱い、時計の逆行を activity と通知に出す。daemon と表示を同じ規則で直す。2026-11-19 チャートの段の閉鎖確認で発見

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-19: 起票。
