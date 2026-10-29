---
id: improve-repeat-progress-display
title: 繰り返し improve の何回目・経過時間を通知・jobs get・status に出す
status: 実装中
priority: 中
kind: 機能
opened: 2026-10-28
closed: null
related: [ctl-improve-repeat-duration-and-oneoff-comment]
---
# [improve-repeat-progress-display] 繰り返し improve の何回目・経過時間を通知・jobs get・status に出す

**状態**: 実装中 / **優先**: 中 / **種別**: 機能

## 現象

afx ctl improve -t / -n の繰り返し中、何回目の実施か・経過時間が Discord の開始・終了通知にも afx ctl status にも出ず、afx ctl jobs get の 1 行 (runs= deadline_at=) でしか分からない (2026-10-28 ユーザー)。方針 (ユーザー承認 2026-10-28): (1) 繰り返し job の mission の Discord 開始・終了通知に「繰り返し 2/10 回 · 5m/30m」の形を付ける (回数は実施回数 / 最大回数、時間は受理からの経過 / -t の期間、-n だけなら 24h)。(2) afx ctl jobs get を人が読める形 (実施 n 回 / 最大 N 回、経過、残り、実行中 mission)。(3) afx ctl status に走行中の繰り返し job の要約 1 行。improve の Discord 通知は daemon の不変 snapshot から本文を組む設計 (2026-10-25-improve-discord-notice-design.md) なので、繰り返しの文脈 (runs / max_runs / 経過秒 / 期間秒) を snapshot に固定語と整数で足し、afx-ops の描画と DTO を両 package で揃える。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-28: 起票。
- 2026-10-29: 状態: 実装中 — 2026-10-29: main d2c8bf76 で merge・配備。次の afx ctl improve -t / -n で Discord・jobs get・status の表示を確認して closed
- 2026-10-29: 状態: 実装中 — 2026-10-29 本機: jobs get / status の表示を確認。不具合: 終了後も「残り 23h56m」「実行中 mission #2498」と出る (終了後は残りを出さず「最後の mission」にすべき)。Discord の表示はユーザー確認待ち
