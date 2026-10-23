---
id: improve-new-candidate-metric-reference-set-freeze
title: 方向性指標の「新規候補」の参照集合を観測開始時点で凍結する
status: 設計待ち
priority: 中
opened: 2026-10-22
closed: null
related: [improve-objective-filtered-signal-quality]
---
# [improve-new-candidate-metric-reference-set-freeze] 方向性指標の「新規候補」の参照集合を観測開始時点で凍結する

**状態**: 設計待ち / **優先**: 中

## 現象

改善ループの質 spec §3.2 の方向性指標「新規候補 2 本以上」は、(ii) 新規仮説族を「現在の配備済み集合」に対して判定する。床を戻す際に配備済み戦略を disable (revoke) すると、その族は配備済み集合から消え、同族の再発見が「新規候補」に数えられる (内生性)。観測開始時点の配備済み族 + 同時に revoke した族を参照集合として凍結しないと、disable の裁定が指標を良く見せる。family は粗い代理で signal の実重複 (発火時刻の Jaccard) は測っていない点も同根。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-22: 起票。
- 2026-10-23: 優先: 中 — 2026-10-23 棚卸し: 10/24 の改善周から方向性指標の観測を始めるため中へ
