---
id: code-impact-map
title: コードから読み取る現状の影響地図 (機械索引・契約表・領域頁) を docs/ に置く
status: 設計待ち
priority: 高
kind: 保守
opened: 2026-11-14
closed: null
related: [auto-killswitch-latch-emits-no-event, bot-gateway-disconnect-is-silent]
---
# [code-impact-map] コードから読み取る現状の影響地図 (機械索引・契約表・領域頁) を docs/ に置く

**状態**: 設計待ち / **優先**: 高 / **種別**: 保守

## 現象

コードが約 12 万行に育ち、チケット単位の実装では全体への影響が見えにくい。2026-11 には guard_state_changed を一部の経路だけが出す、shell が StateStore を直接変えて ops lane を迂回する、docstring と実動作が食い違う、が続いた。基本設計は 10 月から更新が止まり、個別の設計書は変更単位の履歴で現状を表さない。現状の正本として、(1) script で作り直せる機械索引 (DB 表・event code・state file・config key ごとの書き手と読み手、daemon と afx-ops の境界)、(2) 状態遷移 × 入口 × 必要な副作用の契約表 (観測事実 / 守る契約 / 既知の逸脱)、(3) 領域ごとの頁、(4) 鮮度の管理 (最終検証 commit と依存先、要確認頁の列挙)、(5) 設計・実装・レビューの工程への組み込み、を docs/ に置く。まず kill switch と guard state の領域で試し、手で作った正解表で抽出の正確さを測ってから全域へ広げる。契約が変わるチケットは同じ commit で該当頁を直す。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。
