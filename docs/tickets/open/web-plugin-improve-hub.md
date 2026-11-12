---
id: web-plugin-improve-hub
title: 同梱 web に plugin と改善の画面群を作る (一覧・依存・承認・改善の実行)
status: 実装中
priority: 高
kind: 機能
opened: 2026-11-08
closed: null
related: [improve-run-summary-for-humans, web-ui-charts-equity-indicators, web-https-for-writes, improve-runner-choice, improve-backtest-untradable-pair-and-skip-readability, web-write-recover-idle-interruption]
---
# [web-plugin-improve-hub] 同梱 web に plugin と改善の画面群を作る (一覧・依存・承認・改善の実行)

**状態**: 実装中 / **優先**: 高 / **種別**: 機能

## 現象

web の承認一覧は pending を並べるだけで、plugin の配備状態・strategy と indicator の依存・改善の様子が見えない。承認と改善の実行は Discord と CLI でしかできない (2026-11-08 利用者要件)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-08: 起票。
- 2026-11-08: 関連: [improve-run-summary-for-humans, web-ui-charts-equity-indicators, web-https-for-writes, improve-runner-choice] — 段 A (読む) を main に merge (8e122365): plugin の read model (純関数、CLI afx plugin list と共有)、依存の列挙、有用性 (提出件数・配備成功件数)、GET /v1/plugins・/v1/plugins/{name}、web の /plugins・/plugins/{name} とナビ Plugins。レビュー codex 1 周目・/code-review high・閉鎖確認 2 回。残り: /improve の画面 (要約の束の後)、段 B1 (loopback の書込み、実装前レビュー済み、設計 C2.3)
- 2026-11-09: 関連: [improve-run-summary-for-humans, web-ui-charts-equity-indicators, web-https-for-writes, improve-runner-choice, improve-backtest-untradable-pair-and-skip-readability] — 段 B1 の 1・2 段目を main に merge (360d6e6e): afx-ops の鍵の manifest 互換 (知らない principal を形を確かめて無視)、ops.toml の write (既定 false)、起動時の検査 (write × auth=none・非 loopback を落とす)、Host・Basic・Origin・token の検査、確認 record の状態機械、厳密な POST の parse、却下の直前の照合。本番の起動にはまだ配線しない (write=true でも読取専用)。レビュー codex 3 回 (Important 2・Minor 1 を是正)。残り: 3 段目 (daemon の ops-web-write・IMPROVE_RUN・actor の表)、4 段目 (afx ops web enable --write と鍵の切替、本体の feature_config.py と reconfigure の write 対応)、/improve の画面
- 2026-11-12: 関連: [improve-run-summary-for-humans, web-ui-charts-equity-indicators, web-https-for-writes, improve-runner-choice, improve-backtest-untradable-pair-and-skip-readability, web-write-recover-idle-interruption] — 段 B1 の 3 段目 (daemon の ops-web-write・IMPROVE_RUN・actor の表・書込み client) と 4 段目 (afx-ops の配線と実隔離の結合、本体の enable --write / --no-write と鍵の切替、write と鍵の整合を check_write_key_consistency に一本化) を main へ merge。レビューは sol 複数周・/code-review high (8 件是正)・sol 閉鎖 (Minor 1 は文書で是正)。判定は afx-ops 4681 緑、本体 tests/ を 3 束 (5222 / 10313 / 5565) 緑。残余は web-write-recover-idle-interruption。残り: /improve 画面。
