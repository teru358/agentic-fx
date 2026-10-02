---
id: first-run-empty-1m-starts-degraded
title: 初期化直後に 1m 足が空だと degraded から始まる (実機で確認)
status: 設計待ち
priority: 低
opened: 2026-10-02
closed: null
related: [outage-stalled-on-broker-daily-rollover-gap]
---
# [first-run-empty-1m-starts-degraded] 初期化直後に 1m 足が空だと degraded から始まる (実機で確認)

**状態**: 設計待ち / **優先**: 低

## 現象

成功だが空の応答は即 degraded という規則のため、新規に初期化した直後の最初の取得で 1m 足が空だと停止の状態から始まる。実際の価格源では取得範囲が既存の足と重なるので通常は空にならない見込みだが、初回起動の経路は実機で未確認。

## 原因

## 処置案・裁定

init 直後の初回起動を実機 (または実 bridge を使う結合テスト) で 1 回確認し、空になる場合は初回だけの扱いを決める。

## 修正内容

## 経緯

- 2026-10-02: 起票。
