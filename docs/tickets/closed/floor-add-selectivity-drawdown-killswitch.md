---
id: floor-add-selectivity-drawdown-killswitch
title: 収益性フロアに選別度・max DD・kill switch 回数を加える
status: 是正済
priority: 中
kind: 機能
opened: 2026-11-01
closed: 2026-11-02
related: []
---
# [floor-add-selectivity-drawdown-killswitch] 収益性フロアに選別度・max DD・kill switch 回数を加える

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

us500_lagged_return_fx (承認 #84) はほぼ毎バー建てる戦略で in-sample pf 1.002 / avg_r 0.004 / max DD 26% / kill switch 55 回だが、床 (pf ≥ 1.0、avg_r > 0) を形式上通って承認依頼になった。床が「取引の選別度」と「運用上の危うさ」を見ていない。処置案: 床に (a) 建玉率 (trades / 評価バー数) の上限、(b) max DD の上限、(c) kill switch 発火回数の上限 (例 0 か 1)、(d) 最小 trades を加え、settings の improve.gate に任意キーで。既定は安全側。改善目標の裁定 (選別可能な勝ち筋、PF ≥ 1.0 を床に、2026-10-07) の延長として設計 C0 → ユーザー承認。改善 agent に出す理由は固定語 (数値は親専有)

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-02: 3ee97e18 (merge): max DD 既定 0.20 と取引頻度 0.25 を床に、eval_bars 計測、再実測 driver

## 経緯

- 2026-11-01: 起票。
- 2026-11-01: 状態: 実装中 — 設計 C0 = 収益性フロア設計 v1.6.2 §10 (terra → sol の 2 周、ユーザー承認 2026-11-01: max DD 既定 0.20 有効・設定可、取引頻度は eval_bars 計測後に閾値、kill switch と最小 trades は床に入れない、agent 面は unprofitable のまま)。T1 (eval_bars + manifest) → T2 (床)
- 2026-11-02: 是正内容 — 3ee97e18 (merge): max DD 既定 0.20 と取引頻度 0.25 を床に、eval_bars 計測、再実測 driver
