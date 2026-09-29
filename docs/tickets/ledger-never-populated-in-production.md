---
id: ledger-never-populated-in-production
status: 実装中
priority: 未設定
opened: 2026-09-04
closed: null
related: [no-history-typed-exception]
backfilled: true
source_section: 未レビュー束
---
# [ledger-never-populated-in-production]

**状態**: 実装中 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[ledger-never-populated-in-production]/[no-history-typed-exception] **是正済 f67820d 2026-09-04 (2 周目 /code-review CR1 Critical + CR9)** — 親 ledger 付き wrapper (`build_ledger_wrapped_rpc_handlers`) を WorkerRunner に配線、e2e の手動 wrap を外し本番配線で pin (trial_count==1)。holdout.NoHistoryError で文字列契約廃止。旧: 親 ctx.ledger が本番で常に空、approval payload trial_count=0 固定 (実 DB #1〜#5)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

