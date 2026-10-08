---
id: legacy-t9b-go-not-sent-parent-runaway
title: go が送られない変異で親がスレッド暴走する
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-t9b-go-not-sent-parent-runaway]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[起票] t9b 変異「go を送らない」で親がスレッド暴走 (67 本・12 分超、変異下のみ) → 子が進まないときの親 timeout 経路を D-9 追補検収で実測

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: go が送られない変異で親がスレッド暴走する — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 変異下のみの現象、親の timeout 経路の実測が決め手。 根拠: tests/runners/test_worker_runner.py:3985 付近に go 不送信変異のバックストップ (3 秒) があるが、親側 timeout 経路の実測 (D-9 追補) は未確認。
