---
id: datafeed-rollover-band-expected-gap
title: 日次ロールオーバー帯の 1 分足の無 tick を停止とみなすか
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-11-21
closed: null
related: [datafeed-outage-discord-notice]
---
# [datafeed-rollover-band-expected-gap] 日次ロールオーバー帯の 1 分足の無 tick を停止とみなすか

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

epoch 52〜66 の 15 件中 14 件が 22:0x〜22:5x UTC の USDJPY 1m の 1 分停滞 (activity.log)。flat なら restricted → 自動復帰するが、建玉を持ち越すと即 degraded・人の resume 待ちになり、11-19 22:40Z からは約 23 時間取引判断が止まった。価格源の取引時間と取得記録から通常の無 tick 帯を確かめ、その帯だけ期待足の例外にする案 (設計 docs/superpowers/specs/2026-11-21-datafeed-outage-notice-design.md の付録の I1(a))。時間帯だけで実障害を見逃す危険があるので、例外の範囲と建玉保有時の扱いは利用者が裁定する。裁定までは今の停止条件を維持する

## 原因

## 処置案・裁定

- 2026-11-21 利用者裁定: 時間帯だけを例外にする案は採らない。欠けはどの時間帯でも起こりうるが、ロールオーバー帯で目立つという前提で、時間帯に依存しない方針を出し直す。

## 修正内容

## 経緯

- 2026-11-21: 起票。
- 2026-11-21: 利用者が時間帯の例外案を退け、時間帯に依存しない方針の再提出を求めた。
