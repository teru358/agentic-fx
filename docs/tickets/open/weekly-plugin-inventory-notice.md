---
id: weekly-plugin-inventory-notice
title: 週次の棚卸し: 未使用の指標と成績の悪い戦略を停止の提案として通知する
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-29
closed: null
related: [plugin-naming-lifecycle-policy, weekly-improve-rebacktests-deployed-strategies]
---
# [weekly-plugin-inventory-notice] 週次の棚卸し: 未使用の指標と成績の悪い戦略を停止の提案として通知する

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

配備中の指標 50 本のうち配備中の戦略が参照するのは 9 本 (2026-10-29)。改善 mission が指標を提出し承認されるたびに増え、使われないものが残る。方針 (ユーザー 2026-10-29): 週次改善の冒頭で、未使用の指標 (配備中の戦略の config.yaml が参照しないもの) と成績の悪い戦略 (配備後の前方成績、weekly-improve-rebacktests-deployed-strategies の結果) を列挙し、Discord に「停止の提案」として通知する。停止・引退は人間が afx の既存操作 (disable / retire) で行い、自動では停止しない。削除はしない。停止済みの名前は改善 prompt に出して作り直しを抑止する。決めること: 未使用の定義 (参照なしが何週続いたら)、成績の悪さの閾値 (床と同じ PF 1.0 / avg_r か、親の成績との比較か)、通知の文面 (数値は 10/25 の裁定の語彙)、`afx ctl` から提案一覧を読む面。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-29: 起票。
