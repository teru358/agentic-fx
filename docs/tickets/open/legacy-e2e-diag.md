---
id: legacy-e2e-diag
title: codex 失敗の理由が stderr 最終行だけで、本当のエラーが読めない
status: 実装待ち
priority: 中
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-diag]

**状態**: 実装待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-diag] codex_runner の失敗 reason が stderr 最終行のみで、実エラー (stdout JSON events の turn.failed / error) を拾わない。PATH aliases WARNING 誤誘導の根 (c2 と 2026-08-30 で計 3 回誤誘導)。turn.failed message を reason に昇格すべき

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 中、題名: codex 失敗の理由が stderr 最終行だけで、本当のエラーが読めない — 仕分け (2026-10-02、現物で成立を確認): turn.failed の message を reason に昇格する処置が未実施。 根拠: src/agentic_fx/runners 配下に turn.failed を拾う処理が無い (grep 0 件)。
