---
id: normal-protection-evaluates-latest-bar-only
title: 通常の SL/TP・指値の判定が最新の 1m 足しか見ない
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-21
closed: null
related: [datafeed-rollover-band-expected-gap, datafeed-pending-auto-release-when-flat]
---
# [normal-protection-evaluates-latest-bar-only] 通常の SL/TP・指値の判定が最新の 1m 足しか見ない

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

scheduler の通常保護 (`_process_limit_fills` / `_process_exits`) は、tick ごとに最新の確定 1m 足だけを鮮度 5 分以内で評価する (`src/agentic_fx/core/scheduler.py:44`, `:942-949`)。データが健全な ready 中でも、次の足は SL/TP・指値の判定を受けない。
- 同じ tick に 2 本の新着足が入ったときの古い方 (tick 周期の揺れで約 4 時間に 1 回と推定)。
- 鮮度 5 分を過ぎて届いた足。
- 金曜最後の 1m 足 (閉場中は取得しないので日曜に一括で入り、鮮度切れで捨てられる。週末をまたぐ建玉で判定されない)。

paper では SL/TP の取り逃しになる (live では broker 側の保護注文なので影響が違う)。

## 原因

## 処置案・裁定

時刻順に新着足を評価する案は、datafeed-rollover-band-expected-gap の設計レビューで検討した (注文の作成前の足での約定、同足 TP 見送りの再評価、部分成功、約定足時刻の永続化などの論点が出た。`tmp/` の記録ではなくこのチケットに論点を残す: 注文ごとの適用開始足、約定に使った足時刻の永続化、pair 内の部分失敗、鮮度切れの足の扱い)。資金保護の判定経路の変更なので設計 → レビュー → 利用者承認で進める。

## 修正内容

## 経緯

- 2026-11-21: 起票 (欠けた足の設計レビューで判明。範囲外として分離)。
