---
id: gate-admission-helper
status: 設計待ち
priority: 未設定
opened: 2026-09-06
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [gate-admission-helper]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[gate-admission-helper]** (2026-09-06、設計): submit/bless/improve の 3 経路が gate チェックリストを各自再導出し drift (max_bars_limit 3 重、kind=signal 漏れ)。`switch._run_full_gate` 相当の共通 admission helper に集約する設計。 **裁定 2026-09-06: 段階 4 完了後に着手** (改善ループ実走の拒否理由分布を見てから設計)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

