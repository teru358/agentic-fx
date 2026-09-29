---
id: baseline-replay-unimplemented
status: 設計待ち
priority: 中
opened: 2026-09-12
closed: null
related: [profitability-floor]
backfilled: true
source_section: 未完了
---
# [baseline-replay-unimplemented]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[baseline-replay-unimplemented] (中、設計書との乖離、2026-09-12 opus 調査) 設計書 2026-08-16 §4.2-4「(pair, timeframe) ごとに現在 live の D4-approved 同名 strategy を同じ in-sample/holdout 期間で回した結果」を baseline とする規定に対し、`strategy_gate.py` の `approved_row is not None` 分岐はマーカー dict を返すだけで再生しない。実 DB の `variant='baseline'` 行は 0 件。よって [profitability-floor] は絶対閾値で先に入れる (U4 裁定)。baseline 再生を実装するなら、同名 approved の artifact 取得・同期間再生・payload の baseline 行・相対閾値 (pf_candidate > pf_baseline) を設計

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

