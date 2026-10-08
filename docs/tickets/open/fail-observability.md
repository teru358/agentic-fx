---
id: fail-observability
title: 改善 mission 失敗の死因が記録されない問題は是正済
status: 設計待ち
priority: 低
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [fail-observability]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-observability]/[fail-observability] (同系) 手動発火 improve mission の失敗が missions.status 以外どこにも出ない (stderr 無し・activity IMPROVE 無し、mission #4 実測)。`_finalize_failed_mission` (improve_loop.py:1465〜) が失敗理由を一切残さない: activity.log 非出力 (他 finalize と非対称)・worker workdir を後始末で消す・worker_runner.py:427-434 が result フレームの `reason` のみ読み mission_worker.py:716-719 の `error` キーを捨てる。失敗 mission の死因分析が毎回 strace 頼みになる根因。是正候補: 失敗 reason の DB/activity 記録 + error キーの伝搬 (mission #14 実測、2026-08-30)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 改善 mission 失敗の死因が記録されない問題は是正済 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 仕分け (2026-10-02): 主要部分は是正済とみられるが本文の一部を追跡できていない — 失敗 mission の reason を activity に記録 (18f5fc4)。worker の error キー伝搬は未確認。 根拠: 18f5fc4。improve_loop.py:2944 _finalize_failed_mission が activity に mission_failed と reason を書く。
