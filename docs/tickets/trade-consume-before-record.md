---
id: trade-consume-before-record
title: 取引の消費が記録より先に起きる問題は是正済
status: 是正済
priority: 未設定
opened: 2026-09-05
closed: 2026-10-02
related: [notifier-under-core-lock-in-tick]
backfilled: true
source_section: 未レビュー束
---
# [trade-consume-before-record]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[trade-consume-before-record]/[notifier-under-core-lock-in-tick] 是正済 44f6244 → 段 0 f9c247a → 1 周目是正 `1bffa97` (tick 例外時の通知 drain / bak 別名 / pin 4) 2026-09-05。**小束クローズ** (2 周目省略の判断: Critical 0、/code-review 由来の是正束)

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。44f6244 と 1bffa97 で是正し小束クローズ済み。 根拠: 44f6244 (consume 失敗を記録済み gate 拒否に、通知を core_lock 外へ)、1bffa97 で 1 周目是正、小束クローズ。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 取引の消費が記録より先に起きる問題は是正済 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。44f6244 と 1bffa97 で是正し小束クローズ済み。 根拠: 44f6244 (consume 失敗を記録済み gate 拒否に、通知を core_lock 外へ)、1bffa97 で 1 周目是正、小束クローズ。
