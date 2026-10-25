---
id: init-news-source-remove-on-revisit
title: afx init 段 3 の再入時に入力済みのニュース源を取り消せない
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-10-25
closed: null
related: []
---
# [init-news-source-remove-on-revisit] afx init 段 3 の再入時に入力済みのニュース源を取り消せない

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

afx init の段 3 (ニュース源) に再入すると、入力済みの源が表示されるが取り消せず、追加の入力しか受けられない (spec 2026-10-25-news-source-cli-design の §2.7 の設計どおり)。確定前に誤って入れた源は afx news disable で止められるが、wizard 内で取り消せると親切。UX の追加要望 (実装レビューの裁定で起票)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
