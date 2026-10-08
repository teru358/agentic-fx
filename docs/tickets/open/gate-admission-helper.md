---
id: gate-admission-helper
title: gate 検査が 3 経路で重複し drift する
status: 設計待ち
priority: 低
opened: 2026-09-06
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [gate-admission-helper]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[gate-admission-helper]** (2026-09-06、設計): submit/bless/improve の 3 経路が gate チェックリストを各自再導出し drift (max_bars_limit 3 重、kind=signal 漏れ)。`switch._run_full_gate` 相当の共通 admission helper に集約する設計。 **裁定 2026-09-06: 段階 4 完了後に着手** (改善ループ実走の拒否理由分布を見てから設計)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: gate 検査が 3 経路で重複し drift する — 仕分け (2026-10-02、現物で成立を確認): 共通 admission helper に集約する設計を、改善実走の拒否分布を見てから着手。 根拠: 本文: submit/bless/improve の 3 経路で gate チェックの再導出が drift。裁定 2026-09-06 で段階 4 後着手、共通 helper の commit は無い。
