---
id: activity-log-grows-without-rotation
title: activity ログが無期限に伸びる
status: 設計待ち
priority: 低
opened: 2026-10-04
closed: null
related: []
---
# [activity-log-grows-without-rotation] activity ログが無期限に伸びる

**状態**: 設計待ち / **優先**: 低

## 現象

logs/activity.log に rotation / retention が無い。読み取りは末尾から有界にできるが、ファイル自体は伸び続ける (操作 API 設計レビュー r1 F14、activity.py:56)。

## 原因

追記のみで rotation の仕組みが無い。

## 処置案・裁定

サイズか日付で rotation し、保持期間を設定に持つ。監査の正は DB 側 (ops_requests) なので activity は人向けの投影として切り捨ててよい。

## 修正内容

## 経緯

- 2026-10-04: 起票。
