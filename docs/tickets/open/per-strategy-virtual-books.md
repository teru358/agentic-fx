---
id: per-strategy-virtual-books
title: 戦略ごとの実帳簿 (同一口座内の仮想分割、両建ては戦略間のみ)
status: 設計待ち
priority: 低
opened: 2026-10-16
closed: null
related: [strategy-attribution-and-shadow-evaluation, bridge-expose-margin-mode]
---
# [per-strategy-virtual-books] 戦略ごとの実帳簿 (同一口座内の仮想分割、両建ては戦略間のみ)

**状態**: 設計待ち / **優先**: 低

## 現象

戦略ごとに独立した実建玉・ロット枠・成績を持ちたい (2026-10-16 ユーザー意図: broker 口座は 1 つ、システム内だけで分割)。前提条件 (全部そろってから着手): (1) 口座が hedging モード (建玉ごとに broker 側 SL/TP を持てる。netting なら両建ては相殺されるだけなので不可)、(2) 両建ての規則 = 戦略間のみ許可 (足を問わない)、同一戦略内の逆向きと LLM 発の反対玉は拒否、(3) コストの明示 (両建て中は実露出ゼロでスプレッド 2 回 + 両側スワップ。実資金では戦略別成績の期待値 > コストが条件、ペーパーでは測定目的で許容)、(4) 資金保護は gross (max_positions は両脚を数える、DD は equity、両脚とも broker SL 必須)、(5) reconcile と executor が建玉をペアでなくチケットで識別する。改修: 建玉・注文への戦略 tag、Risk Gate の gross 判定と戦略別枠、reconcile のチケット単位化、両建て規則。着手条件: strategy-attribution-and-shadow-evaluation の成績で勝ち筋が 2 本以上、かつ margin_mode が hedging と確定

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-16: 起票。
