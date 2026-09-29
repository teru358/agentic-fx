---
id: first-run-setup
status: 設計待ち
priority: 未設定
opened: 2026-09-17
closed: null
related: [indicator-consumption-wiring, indicator-initial-set]
backfilled: true
source_section: 未完了
---
# [first-run-setup]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[first-run-setup] 方針 2026-09-17 (ユーザー)**: 初回起動は対話的な設定を必須にする (LLM 接続先 / トレード足 / 戦略作成のきっかけ / 戦略改善周期 等)。設定完了をもって user systemd service ファイルを設置する (設計書 §8「systemd unit 化はユーザーが明示、ドキュメントのみ」を改訂)。現行 `main.py init` (非対話・冪等) を置換/拡張。**着手は戦略調整 ([indicator-consumption-wiring] → [indicator-initial-set] → ペーパー到達) の後、詳細設計はその時点で**。README 空・runbook 消失の受け皿でもある

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

