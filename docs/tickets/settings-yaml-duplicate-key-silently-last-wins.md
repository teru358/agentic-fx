---
id: settings-yaml-duplicate-key-silently-last-wins
title: settings.yaml の重複キーは黙って後の値が効く
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
---
# [settings-yaml-duplicate-key-silently-last-wins] settings.yaml の重複キーは黙って後の値が効く

**状態**: 設計待ち / **優先**: 中

## 現象

pyyaml の safe_load は重複キーを後勝ちにする。手編集で同じキーを 2 回書くと、見ている行と効いている値がずれる (2026-10-02 実測)。

## 原因

## 処置案・裁定

起動時の検証で重複キーを検出して拒否し、行番号を示す。初期設定ウィザードの事前検査と同じ部品を使う。

## 修正内容

## 経緯

- 2026-10-02: 起票。
