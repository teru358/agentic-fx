---
id: weekly-plugin-inventory-notice
title: 週次の棚卸し: 未使用の指標と成績の悪い戦略を停止の提案として通知する
status: 実装中
priority: 中
kind: 機能
opened: 2026-10-29
closed: null
related: [plugin-naming-lifecycle-policy, weekly-improve-rebacktests-deployed-strategies]
---
# [weekly-plugin-inventory-notice] 週次の棚卸し: 未使用の指標と成績の悪い戦略を停止の提案として通知する

**状態**: 実装中 / **優先**: 中 / **種別**: 機能

## 現象

配備中の指標 50 本のうち配備中の戦略が参照するのは 9 本 (2026-10-29)。改善 mission が指標を提出し承認されるたびに増え、使われないものが残る。方針 (ユーザー 2026-10-29): 週次改善の冒頭で、未使用の指標 (配備中の戦略の config.yaml が参照しないもの) と成績の悪い戦略 (配備後の前方成績、weekly-improve-rebacktests-deployed-strategies の結果) を列挙し、Discord に「停止の提案」として通知する。停止・引退は人間が afx の既存操作 (disable / retire) で行い、自動では停止しない。削除はしない。停止済みの名前は改善 prompt に出して作り直しを抑止する。決めること: 未使用の定義 (参照なしが何週続いたら)、成績の悪さの閾値 (床と同じ PF 1.0 / avg_r か、親の成績との比較か)、通知の文面 (数値は 10/25 の裁定の語彙)、`afx ctl` から提案一覧を読む面。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-29: 起票。
- 2026-11-20: 状態: 実装中 — 設計 docs/superpowers/specs/2026-11-19-weekly-deployed-strategy-review-design.md C1 を利用者承認 (§6 は 6 件とも推奨どおり)。3 チケットを 1 設計の 3 段 (指標の棚卸しの読取面 / 戦略別の前方成績 = 機械採点 / 週次の確定・判定・停止の提案の通知) で実装する
- 2026-11-20: 状態: 実装中 — 段 1 (棚卸しの読取面: usage_review・afx plugin list --unused・web の使われ方の列) を main に merge 0d8fff89、2026-11-20 18:52 に afx と afx-ops@web/bot を再起動。本番で未使用 2 本 (停止の提案: 可)、直近 8 週の戦略候補 531 件。週次の通知は段 3
