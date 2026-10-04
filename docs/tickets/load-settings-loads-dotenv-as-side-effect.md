---
id: load-settings-loads-dotenv-as-side-effect
title: 設定の検証が .env を環境に読み込む副作用を持つ
status: 実装待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
---
# [load-settings-loads-dotenv-as-side-effect] 設定の検証が .env を環境に読み込む副作用を持つ

**状態**: 実装待ち / **優先**: 中

## 現象

config.load_settings が load_dotenv() を引数なしで呼ぶ (config.py:742-743)。検証だけのつもりで呼んでも実 .env が環境変数に載る (2026-10-02 実測)。配布形 (非 editable) では .env を見つけない可能性もある (未確認)。

## 原因

## 処置案・裁定

.env の読み込みを起動経路の 1 箇所に寄せ、検証は副作用なしにする。.env の探索位置を明示する。

## 修正内容

## 経緯

- 2026-10-02: 起票。
- 2026-10-04: 状態: 実装待ち — 2026-10-04: 操作 API spec v1.3 (docs/superpowers/specs/2026-10-04-ops-api-design.md) の T1 の範囲に含めてユーザー承認。実装は plugin worker 隔離の main 投入と脱出の実測の後。
