---
id: cross-cutting-invariant-review
title: 横断の不変条件の再確認 (資金保護・隔離・秘密・状態の整合)
status: 設計待ち
priority: 高
kind: 保守
opened: 2026-11-14
closed: null
related: [code-impact-map, killswitch-reset-hwm-rebase]
---
# [cross-cutting-invariant-review] 横断の不変条件の再確認 (資金保護・隔離・秘密・状態の整合)

**状態**: 設計待ち / **優先**: 高 / **種別**: 保守

## 現象

チケット単位の diff レビューでは、局所では正しく見える横断の欠陥が抜けた (2026-11 の kill switch 作動の無通知、bot の切断の無言、shell の ops lane 迂回、holdout 空のままの承認)。直近の実装にもレビューのたびに指摘が出ており、古い部分にも同じ型が残ると見る。全体の読み直しではなく、横断の契約 4 つに絞って入口ごとの副作用の契約表を作り、コードと照合する: (1) 資金保護 (発注・SL・close・kill switch・HWM)、(2) 隔離 (Landlock・seccomp・改善 loop の書込み範囲)、(3) 秘密 (鍵・token・口座・log・公開文書)、(4) 状態と削除の整合 (lane・lock・marker・DB の原子性・rmtree)。資金保護は opus と codex の 2 者が独立に確かめる。影響地図の契約表の作り方を流用する。着手は実装中の作業 (運用異常の通知、段 1b、同期の段 2・3、HWM の付け直し) が一段落した時点 (2026-11-14 利用者裁定)。遅くとも実口座 broker の導入と公開の前に行う。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。
