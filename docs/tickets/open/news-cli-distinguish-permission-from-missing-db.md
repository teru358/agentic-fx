---
id: news-cli-distinguish-permission-from-missing-db
title: afx news が権限不足・パス不備も「DB がありません」と出す
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-10-25
closed: null
related: []
---
# [news-cli-distinguish-permission-from-missing-db] afx news が権限不足・パス不備も「DB がありません」と出す

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

afx news の add / enable / disable は、DB を開く操作の失敗を FileNotFoundError に写して「ニュース源を読めません (DB がありません)。」と出す (spec 2026-10-25-news-source-cli-design の §4 (2) の設計)。権限不足やパスの不備でも同じ文になり、原因が読み取れない。文面を権限エラーなどと分岐するか検討する。実装レビューの裁定で起票。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
