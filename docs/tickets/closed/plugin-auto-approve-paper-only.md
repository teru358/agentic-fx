---
id: plugin-auto-approve-paper-only
title: plugin 自動承認モード (ペーパー限定の検討メモ)
status: 見送り
priority: 低
kind: 機能
opened: 2026-10-27
closed: 2026-10-29
related: [approval-base-identity-cas, plugin-naming-lifecycle-policy]
---
# [plugin-auto-approve-paper-only] plugin 自動承認モード (ペーパー限定の検討メモ)

**状態**: 見送り / **優先**: 低 / **種別**: 機能

## 現象

ユーザー提起 (2026-10-27): 改善候補の plugin を自動承認するモード。現行の絶対制約「採用は人間承認必須」と全体設計 §604 (autopilot 中は plugin 承認を API から外す) と衝突するため、一般の自動承認は不可。検討するならペーパー限定で、少なくとも: 自動承認版に印を付け取引モード切替時は人間の再承認を必須、自動承認の成績を autopilot on 判断の実績と分ける、approval-base-identity-cas を先に直す、Discord に必ず通知し即取り消し可。懸念: gate は成績しか見ず、繰り返しで試行が増えると偶然 floor を越える候補が増える。代替: Discord から 1 操作で承認・承認待ちのまとめ表示で手間を減らす。着手前に sol 確認と絶対制約の緩和についてユーザー裁定が要る。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。
- 2026-10-29: 見送り理由 — 採用は人間承認必須の絶対制約と全体設計 §604 に衝突する検討メモ
