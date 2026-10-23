---
id: signal-producer-hourly-warning-before-confirmation
status: 見送り
priority: 低
opened: 2026-09-29
closed: 2026-10-23
related: []
backfilled: true
source_section: 未完了
---
# [signal-producer-hourly-warning-before-confirmation]

**状態**: 見送り / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[signal-producer-hourly-warning-before-confirmation] 起票 2026-09-29 (低、ノイズ)**: producer が毎時 xx:00:07 の tick で「1h bucket not yet present」を WARNING で出す (足の確定は xx:00:30 なので次 tick で成功する正常な待ち)。確定前の 1 回は DEBUG/INFO に落とすか、確定時刻まで評価を遅らせる

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-23: 見送り理由 — signal-producer-evaluates-before-bar-arrives と同じ原因 (確定前評価)。そちらに統合
