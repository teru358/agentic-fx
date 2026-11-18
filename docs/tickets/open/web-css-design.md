---
id: web-css-design
title: 同梱 web の見た目を運用画面風に整え、ダークモードと内容の簡略化を入れる
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-11-18
closed: null
related: [web-layout-tabs, web-ui-charts-equity-indicators, web-plugin-improve-hub]
---
# [web-css-design] 同梱 web の見た目を運用画面風に整え、ダークモードと内容の簡略化を入れる

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

配置の段 (web-layout-tabs、11-16 完了) の次。今の CSS は afx-ops/afx_ops/web/render.py の STYLE (約 25 行、白地のみ) で、状態を一目で読みにくい。11-15 の裁定で、内容の削除・簡略化、ダークモード、ほかの表のスクロールはこの段で扱うと決めた。

利用者の方針 (2026-11-18): ダークモードは OS の設定に従う (prefers-color-scheme、切替ボタンなし、JS なし)。見た目は運用画面風 (要点を上に、状態を色の印で、表は詰めて縞模様、数字は桁を揃える)。削除・簡略化は指揮者が各画面を読んで候補を出し、利用者が選んだものだけを設計に入れる。

順序: 配置 → CSS (この段) → チャート (web-ui-charts-equity-indicators)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-18: 起票。
- 2026-11-18: 状態: 設計待ち — 設計 C0 の下書きを依頼 (2026-11-18)
