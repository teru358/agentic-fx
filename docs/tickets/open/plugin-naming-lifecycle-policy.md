---
id: plugin-naming-lifecycle-policy
title: plugin の命名・引退・保有上限の方針 (同名更新か新名作成か)
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-10-27
closed: null
related: [approval-base-identity-cas, indicator-update-dependent-relock-flow]
---
# [plugin-naming-lifecycle-policy] plugin の命名・引退・保有上限の方針 (同名更新か新名作成か)

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

改善で戦略・指標を同名で更新するか新名で作るかの方針が無い。利用者から「新しく作るほうが影響が少なく版管理の必要性が薄れる」との提起 (2026-10-27)。sol の評価: 新名を原則にするのは不向き (duplicate_logic / family_redirect が同名更新へ誘導、全 admitted strategy が signal 評価対象で増殖は runtime・検証量を増やす)。推奨は「同一仮説・互換修正は同名更新、意味・outputs・依存契約が変わるもの・並行比較するものは新名」。新名方式でも hash 固定と版管理は要る (承認 identity・signal 監査・rollback が依存)。引退 (retire) と inventory 上限も含めて決める。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。
