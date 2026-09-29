---
id: analyze-corr-rpc-double-unwrap
status: 是正済
priority: 未設定
opened: 2026-09-09
closed: 2026-09-09
related: []
backfilled: true
source_section: 未完了
---
# [analyze-corr-rpc-double-unwrap]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[analyze-corr-rpc-double-unwrap] 是正済み 2026-09-09** `f432dbf` (子が `{"request": …}` を送る + 全 RPC 種別の契約テスト) → codex Important 2 + Minor 4 `7e0fc16` → ローカル pin `bdfb71e`。3517 passed。実機での成功応答は run9 でも呼び出し 0 で未確認 (旧: 重大、run8 欠陥 A) `afx_analyze_corr` は本番経路で 100% 失敗 (backend 非依存)。子 `improve_rpc_tools.analyze_corr(request)` は `request` の中身を RPC に送り、親 `build_ledger_wrapped_rpc_handlers` は `func(**args)` で再 splat → `analyze_corr(query=…)` TypeError。`run_backtest` はフラット dict を送るので偶然一致。親 wrapper を通すテストは 0 件 (verify-integration 型)。是正: 子が `{"request": request}` を送る + 子→RPC→親 wrapper を跨ぐ契約テスト。[OC-qwen3.8-toolcall] の「モデルの引数生成が壊れている」見立てを訂正 (m20 も同根の可能性)

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

