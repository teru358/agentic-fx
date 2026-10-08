---
id: mission-abort-on-tool-budget
status: 是正済
priority: 高
opened: 2026-09-08
closed: 2026-09-09
related: [backtest-dedup-cache]
backfilled: true
source_section: 未完了
---
# [mission-abort-on-tool-budget]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[mission-abort-on-tool-budget] (高) 設計 v4 `tmp/design-mission-abort/design.md` (2026-09-08、codex 3 周 `codex-design-review{,-2,-3}.md`、**ユーザー承認 2026-09-08**: v4 実装 + 受入 probe 許可、Tier G 分離、`remaining_budget` を Tier C 補足で追加) → **実装 `27b7d77` (codex、段 0 18 変異、うち 1 件は -x flake の誤 red) → 1 周目: codex Important 1 `22e5555` / ローカル Y 9 `3145149` → 2 周目: codex Important 1 + Minor 1 `e076e45` / ローカル Y 1 `4eb220f`。本番欠陥 0。受入 probe YES (`probe-resume-mcp.md`: opencode 1.18.25 の resume は home config の mcp.afx.enabled=false を再読込、JSON 回収も成立)。`/code-review high` 完了 (`tmp/review-20260908-ma-r2/code-review-high.md`): 7 件中採用 7 → `7c6e049`。**本番欠陥 1 = local backend (既定) で abort が発火しない** (fire_if_pending の呼び出し元が dispatcher だけ。段 0・1 周目のテストは全て event を手で set していた) → LocalRunner に `after_tool_call` フック / abort 後の追撃回収 completed に reason 保持 + activity / prefix 定数化 / 閾値判定集約 / 旧契約 docstring・設計書 §1.6 改訂。3483 passed。→ **3 周目 sonnet 完了 (ユーザー指示) `1e32df9`**: Important 1 (activity 無テスト) + Minor 1 (端から端の E2E 無し) を採用、本番欠陥 0。3486 passed。**レビュー 3 周完了** → **A4 7 回目 実施 (2026-09-08、#63、`tmp/a4-run7-20260908.md`、20 分リミット = mission_timeout_sec 1200)**: Tier F の abort は**未検証** (壊れている証拠ではない) — 走行速度が run6 の 1/10 (1 呼び出し 67 s、trade #62 と冒頭 4 分 43 秒競合) で総呼び出し 17 に留まり予算枯渇に届かず timeout (19:01)。初観測 3 件: Tier A 発火 (consecutive 3) / **Tier B 発火かつ有効** (run4/5 の 26〜28 回ループを 1 回で断ち切り run_backtest へ) / resume 時の MCP 無効化 (`enabled` True→False、`could not disable` 0)。system note 型 B #41 が 1 件 (CP3 充足)、Traceback 0。**後日再実行**: mission_timeout_sec 3600 + trade mission と重ならない時間帯。予算を絞って 20 分で枯渇させる案 (max_backtests_per_candidate=2) は「run6 と同一条件」から外れるため裁定待ち****: 引き金 = 拒否 streak 2 本 (予算枯渇 / Tier B 順序違反、既定 10、リセットは成功 backtest・実行された self-test のみ) + 総呼び出し 300。終端 = `failed` + `reason=tool_budget_abort:…` (max_turns には畳まない)。dispatcher の `_call_lock` を execute+sendall に拡大し閾値 request の送信後に event → CliRunner poll loop (deadline 優先、`TerminationCause`) → 主実行のみ SIGTERM → resume 追撃は MCP 無効化 (contract probe が受入条件) → LocalRunner は tool 前後で確認 → claude improve allowlist を MCP のみに。Tier D' = 固定 idea 2 種 (backtest 0 回型 / 枯渇後未提出型)。**Tier G (backtest 重複排除) は 3 周連続 Critical で [backtest-dedup-cache] に分離**。mission の終了保証。総呼び出し `max_tool_calls` 到達で worker 内 dispatcher が event → `CliRunner._run_cli_process` を poll loop 化し主実行のみ SIGTERM → 既存追撃で最終 JSON 回収 → `MissionResult("aborted")`。仕様は design v4 §Tier F (3 周目是正込み: 追撃には event を渡さない / `termination_cause` で deadline 優先 / claude improve profile の allowlist を `mcp__afx__*` のみに / codex backend は `mission_timeout_sec` のみ = 許容済み)。A4 6 回目で Tier B/C の効果を見てから要否判断

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

