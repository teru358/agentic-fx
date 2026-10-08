---
id: legacy-l72
title: holdout の空期間分岐に穴があるとの記録
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-l72]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[L72] holdout の空期間分岐 (束 C 由来でない既存コードの穴)。tmp/review-bundleC/verified-round1.md 参照

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: holdout の空期間分岐に穴があるとの記録 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 現行コードは空期間で例外を出す。残る穴の具体は元レビュー資料で確認が要る。 根拠: holdout.py:186-190 で空期間を ValueError にしている。本文の「穴」が何を指すかは tmp/review-bundleC/verified-round1.md を見ないと決まらない。
