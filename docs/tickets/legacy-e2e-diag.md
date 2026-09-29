---
id: legacy-e2e-diag
status: 設計待ち
priority: 未設定
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-diag]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-diag] codex_runner の失敗 reason が stderr 最終行のみで、実エラー (stdout JSON events の turn.failed / error) を拾わない。PATH aliases WARNING 誤誘導の根 (c2 と 2026-08-30 で計 3 回誤誘導)。turn.failed message を reason に昇格すべき

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

