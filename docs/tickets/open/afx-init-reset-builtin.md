---
id: afx-init-reset-builtin
title: afx init --reset (内蔵のリセット) を追加する
status: 設計待ち
priority: 低
opened: 2026-10-12
closed: null
related: [first-run-setup]
---
# [afx-init-reset-builtin] afx init --reset (内蔵のリセット) を追加する

**状態**: 設計待ち / **優先**: 低

## 現象

first-run spec v1.0 §4.5 はリセットを独立スクリプト scripts/first-run-reset.sh (venv / settings が壊れていても動く) に置いた。内蔵版は後回し。追加するなら所有判定 (instance_id、ops.toml の instance_root、unit の WorkingDirectory) の規則をスクリプトと 1 箇所に共通化し、二重確認と非端末の規律を同じにする。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-12: 起票。
