---
id: ops-bot-initial-start-replays-all-events
title: ops bot が初回起動で daemon の全 event 履歴をチャンネルに投稿する
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: [ops-bot-display-residuals]
---
# [ops-bot-initial-start-replays-all-events] ops bot が初回起動で daemon の全 event 履歴をチャンネルに投稿する

**状態**: 設計待ち / **優先**: 低

## 現象

afx-ops@bot の初回起動 (2026-10-11 17:23) で event stream を cursor 0 から読み、過去の approve / improve の event 5 件を全部 Discord に投稿した。履歴が長い instance では初回 enable がチャンネルを埋める。初回は cursor を末尾 (または直近 N 件) から始めるか、起動時の一括投稿に件数上限を置く。snapshot に cursor を持つなら初回だけの規則で足りる。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。
