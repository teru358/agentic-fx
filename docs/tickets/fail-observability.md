---
id: fail-observability
status: 設計待ち
priority: 未設定
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [fail-observability]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-observability]/[fail-observability] (同系) 手動発火 improve mission の失敗が missions.status 以外どこにも出ない (stderr 無し・activity IMPROVE 無し、mission #4 実測)。`_finalize_failed_mission` (improve_loop.py:1465〜) が失敗理由を一切残さない: activity.log 非出力 (他 finalize と非対称)・worker workdir を後始末で消す・worker_runner.py:427-434 が result フレームの `reason` のみ読み mission_worker.py:716-719 の `error` キーを捨てる。失敗 mission の死因分析が毎回 strace 頼みになる根因。是正候補: 失敗 reason の DB/activity 記録 + error キーの伝搬 (mission #14 実測、2026-08-30)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

