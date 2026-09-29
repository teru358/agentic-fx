---
id: strategy-gate-data-error-crashes-commit
status: 実装中
priority: 未設定
opened: 2026-09-02
closed: null
related: [run-backtest-error-opaque, staging-snapshot-src-leak, staging-shell-leak]
backfilled: true
source_section: 未レビュー束
---
# [strategy-gate-data-error-crashes-commit]

**状態**: 実装中 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[strategy-gate-data-error-crashes-commit]/[run-backtest-error-opaque]/[staging-snapshot-src-leak]/[staging-shell-leak] **是正済 bc75ad6 2026-09-02、未レビュー** — gate 判定化 + 補償真因 (commit finally の無条件 mark_persisted) / 原因別 error + hint (ohlcv_history 実データ) / `_drop_staging_candidate` が候補しか消さず mission dir + `_snapshot_src/` (dr-x------) が空殻で残る問題 (34/40、approve 後 38 で実測) を os.walk 再帰 chmod で掃除 (codex の 1 階層版は実形状で偽緑 → 指揮者是正)。実装 codex + 指揮者検収 2 件上乗せ、変異 2 件 red 実測、フルスイート 3041 passed。A4 で採取した欠陥 (`tmp/a4-report-20260902.md`)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

