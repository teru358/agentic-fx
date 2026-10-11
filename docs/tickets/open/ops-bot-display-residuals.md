---
id: ops-bot-display-residuals
title: ops bot の表示まわりの残余 (CAS 不一致後の終端 edit が 1 回のみ、承認単位 Lock の表が増え続ける)
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: []
---
# [ops-bot-display-residuals] ops bot の表示まわりの残余 (CAS 不一致後の終端 edit が 1 回のみ、承認単位 Lock の表が増え続ける)

**状態**: 設計待ち / **優先**: 低

## 現象

afx-ops T6 bot の codex 6 周目で残った Low 2 件。(1) 再投稿の CAS 不一致後に送った投稿を NO_LONGER_ACTIVE に edit する試みは 1 回だけで、Discord の一時失敗で View が見えたまま残る (route は登録しないので gate は決定を拒み、安全性には関わらない)。(2) `_display_locks` は承認が終わっても entry を消さず、稼働時間に比例して増える (1 件あたり小さく件数は承認数どまり)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。
