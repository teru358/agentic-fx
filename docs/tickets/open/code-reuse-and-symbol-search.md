---
id: code-reuse-and-symbol-search
title: コードの重複防止と source 検索の軽量化の方式を決める
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-17
closed: null
related: [code-impact-map]
---
# [code-reuse-and-symbol-search] コードの重複防止と source 検索の軽量化の方式を決める

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

コードが約 12.8 万行に育ち、既存関数の再利用漏れ (重複) と、agent が source を読む量の多さが課題。手書きの影響地図 (code-impact-map) は費用に見合わず中止した (2026-11-17)。方針の候補 (未決定、様子見): (1) module ごとの公開関数の signature と docstring 1 行目の目録を script で自動生成し、設計 C0 と実装 brief で既存関数を引く入口にする。(2) AST の正規化と hash による重複の検出器を判定時に報告だけで回し、既存の重複も棚卸しする。(3) LSP (pyright) か tree-sitter による symbol 単位の検索道具を agent に使わせる (外部道具の選定が要る)。手で保守する文書は作らない。1 + 2 を先に、3 は不足が分かってから。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-17: 起票。
