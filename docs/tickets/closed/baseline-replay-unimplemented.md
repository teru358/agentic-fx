---
id: baseline-replay-unimplemented
status: 是正済
priority: 中
opened: 2026-09-12
closed: 2026-10-31
related: [profitability-floor, same-name-candidate-must-beat-deployed-baseline]
backfilled: true
source_section: 未完了
kind: 不具合
---
# [baseline-replay-unimplemented]

**状態**: 是正済 / **優先**: 中 / **種別**: 不具合

## 現象・原因・処置案 (tickets.md からの移行、原文)

[baseline-replay-unimplemented] (中、設計書との乖離、2026-09-12 opus 調査) 設計書 2026-08-16 §4.2-4「(pair, timeframe) ごとに現在 live の D4-approved 同名 strategy を同じ in-sample/holdout 期間で回した結果」を baseline とする規定に対し、`strategy_gate.py` の `approved_row is not None` 分岐はマーカー dict を返すだけで再生しない。実 DB の `variant='baseline'` 行は 0 件。よって [profitability-floor] は絶対閾値で先に入れる (U4 裁定)。baseline 再生を実装するなら、同名 approved の artifact 取得・同期間再生・payload の baseline 行・相対閾値 (pf_candidate > pf_baseline) を設計

## 修正内容

- 2026-10-31: 配備中の版の同条件再生を strategy_gate に実装 (T1)、再生失敗の対応表・壁時計上限 300 秒・checkpoint 切り戻し。T4 で実 worker 実測 (再生約 14 秒)。2026-10-31 main 取り込み

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-25: 種別: 不具合 — 種別を付与
- 2026-10-29: 状態: 実装中 — 2026-10-29: 同名候補の配備版比較 gate (same-name-candidate-must-beat-deployed-baseline、設計 2026-10-29-baseline-comparison-gate-design.md) で閉じる
- 2026-10-29: 関連: [profitability-floor, same-name-candidate-must-beat-deployed-baseline] — 2026-10-29 棚卸し: 吸収の疑い。2026-10-29 追記で same-name 比較 gate 設計 (2026-10-29-baseline-comparison-gate-design.md) で閉じると明記済み。現コードに baseline 再生なし (strategy_gate は marker のみ)
- 2026-10-31: 是正内容 — 配備中の版の同条件再生を strategy_gate に実装 (T1)、再生失敗の対応表・壁時計上限 300 秒・checkpoint 切り戻し。T4 で実 worker 実測 (再生約 14 秒)。2026-10-31 main 取り込み
