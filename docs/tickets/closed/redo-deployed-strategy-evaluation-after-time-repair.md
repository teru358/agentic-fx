---
id: redo-deployed-strategy-evaluation-after-time-repair
title: 時刻修復後の履歴で配備中 strategy の in_sample / holdout をやり直し承認の根拠を差し替える
status: 是正済
priority: 中
kind: 保守
opened: 2026-11-07
closed: 2026-11-07
related: [market-hours-fixed-21utc-ignores-dst, mt5-bridge-applies-current-offset-to-all-history]
---
# [redo-deployed-strategy-evaluation-after-time-repair] 時刻修復後の履歴で配備中 strategy の in_sample / holdout をやり直し承認の根拠を差し替える

**状態**: 是正済 / **優先**: 中 / **種別**: 保守

## 現象

DST 設計 J2(c) の測定 (2026-11-07、tmp/review-dst/j2c-report.md): 配備中 strategy 6 本 (USDJPY 1h) を修復前 / 後の履歴で backtest すると、冬を含む期間で asym_shock_breakout PF 0.96→1.05、close_pressure_followthrough 0.91→1.01、rsi_bb_reversion_200 1.06→0.93 など差が大きい。承認の根拠 (backtest_runs / candidate_archives の in_sample・holdout) は誤ラベルの履歴で算出されている。利用者裁定 (2026-11-07): (a) 6 本すべてやり直して根拠を差し替える。再評価と承認の根拠の差し替えの経路は現行に無いので設計が要る。rsi_bb は修復後 PF < 1.0 (床) で、やり直し後に配備継続を判断する

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-07: 2026-11-07 配備 (main 990630c5)。afx plugin reevaluate --all-deployed で配備中 6 本を修復後の履歴で再評価し、承認の依頼 6 件を作成。利用者が 4 本を承認 (根拠を差し替え)、bb_ema_breakout (holdout PF 0.98) と rsi_bb_reversion_200 (holdout 2 trades) を却下し、却下と一体で配備を停止 (disable_symlink op done、plugin_disabled 通知)

## 経緯

- 2026-11-07: 起票。
- 2026-11-07: 状態: 実装中 — 設計 C2.1 を 2026-11-07 に利用者承認 (spec 2026-11-07-redo-deployed-strategy-evaluation-design.md)。T1 → T2 → T3 → T4
- 2026-11-07: 是正内容 — 2026-11-07 配備 (main 990630c5)。afx plugin reevaluate --all-deployed で配備中 6 本を修復後の履歴で再評価し、承認の依頼 6 件を作成。利用者が 4 本を承認 (根拠を差し替え)、bb_ema_breakout (holdout PF 0.98) と rsi_bb_reversion_200 (holdout 2 trades) を却下し、却下と一体で配備を停止 (disable_symlink op done、plugin_disabled 通知)
