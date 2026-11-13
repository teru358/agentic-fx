---
id: status-history-hidden-while-daemon-runs
title: daemon 稼働中は評価用履歴の状態を見る面が無い
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-13
closed: null
related: [live-confirmed-bars-into-history]
---
# [status-history-hidden-while-daemon-runs] daemon 稼働中は評価用履歴の状態を見る面が無い

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

afx status は daemon 稼働中 (instance locked) だと短い表示だけを返し evaluation history ブロックを出さない。afx ctl status にも同ブロックが無い。設計 (2026-11-11 backtest-history-freshness-guard §status) は daemon 停止中も出すとしており、稼働中に出せないのは食い違い。改善の採用・承認が history_stale で止まる理由を運用中に確かめられない。あわせて: (1) references 行が常に unavailable になる (2026-11-13、参照 4 銘柄を 12:05Z まで取り込んだ後も unavailable)、(2) verdict が ready でも next 行が import を案内する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-13: 起票。
