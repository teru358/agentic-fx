---
id: indicator-first-seeding
title: 初期 indicator セットを人間が用意する方針 (配備済)
status: 是正済
priority: 未設定
opened: 2026-09-12
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [indicator-first-seeding]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[indicator-first-seeding] (設計待ち、ユーザー方針 2026-09-12) 初期 indicator plugin セットを人間が用意し (MACD / ボリンジャー / ATR / ADX 等)、改善ループは「手元の指標で戦略を作る」「不足指標を起票・作成する」の 2 層で回す。`policy/directives.md` の文言はユーザーが検討中

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。標準指標の初期セットを配備した。directives.md の文言はユーザーが検討する。 根拠: plugins/ に macd・bollinger・atr・adx・stochastic・ichimoku・rsi・sma・ema が配備済み。indicator-initial-set 70bcd2d。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 初期 indicator セットを人間が用意する方針 (配備済) — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。標準指標の初期セットを配備した。directives.md の文言はユーザーが検討する。 根拠: plugins/ に macd・bollinger・atr・adx・stochastic・ichimoku・rsi・sma・ema が配備済み。indicator-initial-set 70bcd2d。
