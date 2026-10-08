---
id: research-user-agent-claims-unverified-url
title: 研究用取得の User-Agent が実在未確認の URL を名乗る
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
---
# [research-user-agent-claims-unverified-url] 研究用取得の User-Agent が実在未確認の URL を名乗る

**状態**: 設計待ち / **優先**: 中

## 現象

improve.research.user_agent の既定 (config.py:319 と example) が、実在を確認していない URL を名乗っている。外向きリクエストの責任の所在が示せない。

## 原因

## 処置案・裁定

公開リポジトリの実 URL に揃える。ニュース取得の User-Agent (ticket news-fetch-no-user-agent-yahoo-429) と同じ値を共有する。

## 修正内容

## 経緯

- 2026-10-02: 起票。
