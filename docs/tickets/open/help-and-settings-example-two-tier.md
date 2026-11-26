---
id: help-and-settings-example-two-tier
title: help と設定例を「普段使う」と「全部・上級」の 2 段に分ける
status: 設計待ち
priority: 中
kind: 保守
opened: 2026-11-26
closed: null
related: [cli-surface-principle-and-reorg]
---
# [help-and-settings-example-two-tier] help と設定例を「普段使う」と「全部・上級」の 2 段に分ける

**状態**: 設計待ち / **優先**: 中 / **種別**: 保守

## 現象

CLI は 73 操作、settings example は末端 147 キーあり、運用者が把握しきれない。7 週の操作台帳で実績のある形は約 20、個人で変えた設定は 19。裁定 (2026-11-26、運用の全体整理 B): `afx -h` / `afx ctl -h` は自分で始める操作と通知時の操作の 10 前後だけ、`afx help all` で全部。操作は消さずに隠し、復旧用 (reconcile など) は該当状態の案内と全件 help から必ずたどれる。settings.yaml.example は「変えるもの」と「上級 (既定のまま)」に並べ替え、risk・保護の項目は変更が無くても上に置く。schema は変えない。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-26: 起票。
