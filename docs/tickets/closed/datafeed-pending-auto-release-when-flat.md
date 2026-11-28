---
id: datafeed-pending-auto-release-when-flat
title: 建玉が全部決済されデータが健全に戻ったら、人の再開なしで戻すか
status: 是正済
priority: 中
kind: 機能
opened: 2026-11-21
closed: 2026-11-28
related: [datafeed-outage-discord-notice]
---
# [datafeed-pending-auto-release-when-flat] 建玉が全部決済されデータが健全に戻ったら、人の再開なしで戻すか

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

データの停止中に建玉があると pending_human_confirmation が立ち、その建玉が SL/TP で決済されて flat になり、gap 以降の足が健全に揃っても人の resume まで止まり続ける (epoch 66: 11-20 00:56Z に SL で決済された後も約 22 時間停止)。未処理の対象が無く連続して健全を確かめられる場合に限り flat の自動解除を認める案 (設計 docs/superpowers/specs/2026-11-21-datafeed-outage-notice-design.md の付録の I1(b))。健全な足の取得は replay 完了の証明ではないので、未反映の処理が無いと判断する条件と手動解除を残す条件は利用者が裁定する。裁定までは pending を維持する

## 原因

## 処置案・裁定

- 2026-11-21 利用者裁定: 建玉が全部決済され、データが健全に戻ったら自動解除でよい。解除の条件 (未反映の処理が無いことの判定) は設計で詰める。datafeed-rollover-band-expected-gap と同じ状態機械の変更なので一つの設計にまとめるかを設計時に決める。
- 2026-11-21 利用者裁定: flat の自動解除の前に、停止 (non-ready) の epoch の間に届いた足を SL/TP・指値の接触検査にかけ、接触があれば自動解除せず人の確認を求める。

## 修正内容

- 2026-11-28: flat の通常停止は接触待ちが無く健全な tick が続けば自動で解除する (restricted は 30 分)。建玉ありと手動健全は緩めない。初回配備時の既存 epoch は移行確認 (人の ack) を求める。設計 C1

## 経緯

- 2026-11-21: 起票。
- 2026-11-21: 利用者が自動解除の方向を承認。
- 2026-11-21: 設計 docs/superpowers/specs/2026-11-21-datafeed-short-gap-and-flat-release-design.md C1 を利用者が承認。実装は datafeed-outage-discord-notice の merge の後。
- 2026-11-28: 是正内容 — flat の通常停止は接触待ちが無く健全な tick が続けば自動で解除する (restricted は 30 分)。建玉ありと手動健全は緩めない。初回配備時の既存 epoch は移行確認 (人の ack) を求める。設計 C1
