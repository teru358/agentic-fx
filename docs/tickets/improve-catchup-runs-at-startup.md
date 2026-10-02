---
id: improve-catchup-runs-at-startup
title: 起動直後に改善 mission が自動起動する (catch-up と推定)
status: 裁定待ち
priority: 低
opened: 2026-09-20
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [improve-catchup-runs-at-startup]

**状態**: 裁定待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[improve-catchup-runs-at-startup] 起票 2026-09-20 (観測、要確認): サービス起動の直後に improve mission #85 が自動起動した (`schedule.improve_at: Sat 03:00` の取りこぼし分の catch-up と推定、未確認)。人間が backlog を整理・課題を投入する前に走るので、起動直後の 1 本は意図しない課題を選ぶ。仕様なら runbook に明記、そうでなければ catch-up の条件を見直す

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 裁定待ち、優先: 低、題名: 起動直後に改善 mission が自動起動する (catch-up と推定) — 仕分け (2026-10-02、現物で成立を確認): 仕様として runbook 明記か catch-up 条件の見直しかをユーザーが決める必要がある。 根拠: core/improve_supervisor.py:88-110 が period_key の wave を未作成なら即作成するため、取りこぼし分が起動直後に走る構造 (本文は推定のまま)。
