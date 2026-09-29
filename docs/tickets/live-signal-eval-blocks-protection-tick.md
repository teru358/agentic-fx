---
id: live-signal-eval-blocks-protection-tick
status: 設計待ち
priority: 高
opened: null
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [live-signal-eval-blocks-protection-tick]

**状態**: 設計待ち / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[live-signal-eval-blocks-protection-tick]** (重要度: 高、同上、未実測): signal maintenance は scheduler tick 内の同期 hook。例外は握るが、「完了はするが重い」plugin が多数あると資金保護の tick を遅らせる。watchdog は scheduler の生存だけを見ており busy 時間を見ない。束 B で wall 遅延を評価。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

