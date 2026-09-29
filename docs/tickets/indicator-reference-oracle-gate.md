---
id: indicator-reference-oracle-gate
status: 設計待ち
priority: 未設定
opened: 2026-09-15
closed: null
related: [improve-targeted-run]
backfilled: true
source_section: 未完了
---
# [indicator-reference-oracle-gate]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[indicator-reference-oracle-gate]** (2026-09-15 起票、設計待ち): indicator には strategy の backtest / フロアに相当する正しさの門が無い (check_source + 自作 self-test + outputs のみ)。初期セットの参照実装 (T6a の独立 oracle と同じ作り) をハーネス側に持ち、同名・同 params の候補は参照値と ±1e-6 一致を gate で要求する。宣言外の新規指標は人間承認のみ。[improve-targeted-run] と同束で設計

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

