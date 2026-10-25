---
id: tracked-files-contain-author-home-path
title: 追跡ファイルに作者のホームの絶対パスが残る
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
kind: 不具合
---
# [tracked-files-contain-author-home-path] 追跡ファイルに作者のホームの絶対パスが残る

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

/home/teru358/... を含む追跡ファイルが 5 件 (tmp/design-first-run/inventory.md の C-8 に一覧)。

## 原因

## 処置案・裁定

相対パスか置き換え記号にする。

## 修正内容

## 経緯

- 2026-10-02: 起票。
- 2026-10-23: 優先: 中 — 2026-10-23 棚卸し: 追跡ファイルに /home/<user> が 6 件。公開前に要掃除なので中へ
- 2026-10-25: 種別: 不具合 — 種別を付与
