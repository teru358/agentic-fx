---
id: live-confirmed-bars-into-history
status: 設計待ち
priority: 未設定
opened: 2026-09-21
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [live-confirmed-bars-into-history]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[live-confirmed-bars-into-history]** (2026-09-21 ユーザー論点 + codex 確認 `tmp/design-a2/unify-check.md`、A2-1 の後): ライブで集めた確定足をバックテスト履歴に不変で積み上げ、cache は形成中の最新値だけにする (二重保存の解消、MT5 再 import を不要に)。前提 = A2-1 が確定足の規則を import と揃えること。設計項目: live 収集行の source 名と HistoryDataset の許可 / **`prune_cache` が source を問わず消す — 履歴を消さない GC** / 確定後に源が値を訂正した場合 / `backtest_runs` にデータの版 (fingerprint / as-of) が無く、履歴が日々増えると同条件の再実行で結果が変わる / 物理的な 2 表 → 1 表統合 (約 20.8 万行、テスト 11 ファイル、3 本目の data migration) はその先。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

