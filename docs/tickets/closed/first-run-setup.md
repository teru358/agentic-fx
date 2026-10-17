---
id: first-run-setup
title: 初回起動の対話設定と service 設置が無い
status: 是正済
priority: 中
opened: 2026-09-17
closed: 2026-10-17
related: [indicator-consumption-wiring, indicator-initial-set]
backfilled: true
source_section: 未完了
---
# [first-run-setup]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[first-run-setup] 方針 2026-09-17 (ユーザー)**: 初回起動は対話的な設定を必須にする (LLM 接続先 / トレード足 / 戦略作成のきっかけ / 戦略改善周期 等)。設定完了をもって user systemd service ファイルを設置する (設計書 §8「systemd unit 化はユーザーが明示、ドキュメントのみ」を改訂)。現行 `main.py init` (非対話・冪等) を置換/拡張。**着手は戦略調整 ([indicator-consumption-wiring] → [indicator-initial-set] → ペーパー到達) の後、詳細設計はその時点で**。README 空・runbook 消失の受け皿でもある

## 修正内容

- 2026-10-17: 受入 3 回目を Dunia (Ubuntu 26.04.1 / kernel 7.0 / Landlock ABI 8、clean clone、main-claude 台本) で完走 (driver rc=0、記録 tmp/review-20261011-sc/reports/acceptance-r3/)。段 0〜14 すべて通過、unit / linger / enable / 起動 / improve isolation admitted=yes を実機確認。唯一の所見 (初回起動の確認が埋め込みモデル取得中に timeout して失敗表示) は別チケット first-start-check-times-out-on-model-download (中) へ。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 初回起動の対話設定と service 設置が無い — 仕分け (2026-10-02、現物で成立を確認): 着手は戦略調整・ペーパー到達の後、詳細設計はその時点。 根拠: 初回起動の対話ウィザードは未実装。方針は 2026-09-17 のユーザー指示、着手は戦略調整後。
- 2026-10-02: 優先: 中 — ユーザー方針 (2026-10-02): 優先度 高 のチケットが片付いたら、次の束として初期設定ウィザードに進む。着手条件を「戦略のペーパー到達の後」から変更
- 2026-10-12: 状態: 実装中 — 2026-10-12: 設計書 v1.0 承認 (docs/superpowers/specs/2026-10-12-first-run-wizard-design.md)。実装順 T10 → T1 → T3 → T2 → T4 → T13 → T11 → T5b → T7a → T7b → T12 → T5a → T6 → T15a/b → T8 → T14。
- 2026-10-16: 16 task は main 投入済み (fr-fix1 / fr-fix2 含む)。tickets skill に「受入待ち」の状態は無いので status は実装中のまま。残りは stream-svr の Ubuntu 26.04 再インストール後の受入 3 回目のみで、完走で是正済にする。
- 2026-10-17: 是正内容 — 受入 3 回目を Dunia (Ubuntu 26.04.1 / kernel 7.0 / Landlock ABI 8、clean clone、main-claude 台本) で完走 (driver rc=0、記録 tmp/review-20261011-sc/reports/acceptance-r3/)。段 0〜14 すべて通過、unit / linger / enable / 起動 / improve isolation admitted=yes を実機確認。唯一の所見 (初回起動の確認が埋め込みモデル取得中に timeout して失敗表示) は別チケット first-start-check-times-out-on-model-download (中) へ。
