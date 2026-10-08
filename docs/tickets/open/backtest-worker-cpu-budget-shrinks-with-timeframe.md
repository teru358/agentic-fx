---
id: backtest-worker-cpu-budget-shrinks-with-timeframe
status: 設計待ち
priority: 高
opened: 2026-09-20
closed: null
related: [product-vision-grown-by-its-user]
backfilled: true
source_section: 未完了
---
# [backtest-worker-cpu-budget-shrinks-with-timeframe]

**状態**: 設計待ち / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[backtest-worker-cpu-budget-shrinks-with-timeframe] 起票 2026-09-20 (重要度: 高、案 A の実機 mission #87 で発覚、再現済 `tmp/plan-a/debug-87/report.md`)**: **再観測候補 2026-09-28: 9/25 18:00 UTC 頃の改善 mission で `holdout.run_in_sample` → `SandboxError: plugin worker exited unexpectedly (EOF)` の traceback 7 回 (`logs/agentic.log` 4450 行付近)、同型か要確認。** strategy の backtest は plugin worker 1 プロセスを再生全体で使い回すので、`plugin.sandbox_session_cpu_sec` (既定 60、RLIMIT_CPU = プロセス寿命の累積) が実質「1 バックテストの CPU 予算」になり、timeframe が細かいほど静かに足りなくなる (実測 1h/依存 2 本 = 9.9 秒、15m/依存 3 本 = 64 秒 → カーネルが SIGKILL)。しかも (T1) worker の stderr は DEVNULL・終了コードは `_kill()` で上書きされ死因がどこにも残らない、soft==hard なので SIGXCPU でなく SIGKILL (T2) agent には無情報の `backtest_failed` しか返らず同じ失敗を予算が尽きるまで繰り返す (T3) 恒久策 = N 評価ごとに worker を張り替えて予算を再生の長さから独立させる (T4) 失敗時も `backtest_cpu` を activity.log に。[[product-vision-grown-by-its-user]] の「失敗の理由が読める」に直結。暫定 = `plugin.sandbox_session_cpu_sec` を引き上げて再起動

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

