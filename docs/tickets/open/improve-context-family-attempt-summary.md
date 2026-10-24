---
id: improve-context-family-attempt-summary
title: 改善 prompt に仮説族ごとの試行回数と結果区分を渡す
status: 設計待ち
priority: 高
opened: 2026-10-24
closed: null
related: [improve-objective-filtered-signal-quality, improve-new-candidate-metric-reference-set-freeze]
---
# [improve-context-family-attempt-summary] 改善 prompt に仮説族ごとの試行回数と結果区分を渡す

**状態**: 設計待ち / **優先**: 高

## 現象

改善ループは試行済み idea の本文と固定語彙の last_result、配備済み族しか受け取らず、候補 archive (46 件・24 族) の族別の消費状況と結果区分 (not_submitted / rejected_by_human / approved / revoked) を知らない。agent は未試行の族を順に消費するだけで、24 族消費して床を越えたものがゼロという事実も伝わらない。成績 (PF 等) は遮断 8 のとおり渡さず、族 key・試行回数・結果区分・最終試行日だけを渡す (第 1 段)。第 2 段 (in-sample 成績の族別順序尺度) は holdout 漏洩との境界を別に設計する。2026-10-24 ユーザー承認 (着手順 1)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-24: 起票。
