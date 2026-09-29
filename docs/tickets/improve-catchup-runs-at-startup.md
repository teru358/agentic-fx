---
id: improve-catchup-runs-at-startup
status: 設計待ち
priority: 未設定
opened: 2026-09-20
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [improve-catchup-runs-at-startup]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[improve-catchup-runs-at-startup] 起票 2026-09-20 (観測、要確認): サービス起動の直後に improve mission #85 が自動起動した (`schedule.improve_at: Sat 03:00` の取りこぼし分の catch-up と推定、未確認)。人間が backlog を整理・課題を投入する前に走るので、起動直後の 1 本は意図しない課題を選ぶ。仕様なら runbook に明記、そうでなければ catch-up の条件を見直す

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

