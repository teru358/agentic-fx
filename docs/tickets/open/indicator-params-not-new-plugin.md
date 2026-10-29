---
id: indicator-params-not-new-plugin
title: 既存の指標の params で表せる指標を新規として提出させない
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-29
closed: null
related: [plugin-naming-lifecycle-policy, weekly-plugin-inventory-notice]
---
# [indicator-params-not-new-plugin] 既存の指標の params で表せる指標を新規として提出させない

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

指標は params 付き (例 EMA の period) で、期間違いは同じ plugin で表せるのに、改善 mission は似た計算を別名の新しい指標として提出しうる。方針 (ユーザー 2026-10-29): 既存の指標の params で表せるものは新規の指標として提出させない。案: 改善 prompt に規律として明記 + gate で検査 (noop_gate / logic_identity の拡張: 候補 indicator の計算が既存 indicator + params の組で再現できるかを、同じ入力に対する出力の一致で判定)。決めること: 一致の判定法 (全出力の数値一致か、許容誤差か)、既存 indicator の params 探索範囲、却下の固定 reason と agent への伝え方 (family_redirect と同じ形)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-29: 起票。
- 2026-10-29: 優先: 中 — 2026-10-29 棚卸し: 2026-10-29 方針の一部。指標 50 本中 9 本しか参照されず、増殖を止める。plugin-naming 方針の確定に連動
