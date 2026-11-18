---
id: web-approval-detail-budget-review
title: 承認詳細で日本語の長い申告があると params の差分と申告が切られる
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-11-18
closed: null
related: [web-css-design, web-layout-tabs]
---
# [web-approval-detail-budget-review] 承認詳細で日本語の長い申告があると params の差分と申告が切られる

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

承認詳細の diff 以外の予算 15 KiB (afx-ops/afx_ops/web/render.py:22-23、daemon 側 src/agentic_fx/ops/change_limits.py:36-37) は骨格 (STYLE を含む) を数える。日本語で最大長 (2,000 文字 × 2) の申告があると、CSS を変える前の今でも params の差分を全部省き、申告を 1,000 文字に切る (web-css-design の設計 C0.1 §2.5 の実測)。承認の判断材料が画面で欠ける。不足は約 8 KiB で、diff との振替 (2 KiB) では解けない。予算の配分か、申告の別表示 (別タブ・別ページ) を検討する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-18: 起票。
