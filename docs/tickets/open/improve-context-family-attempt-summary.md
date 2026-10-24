---
id: improve-context-family-attempt-summary
title: 改善 prompt に仮説族ごとの試行回数と結果区分を渡す
status: 実装中
priority: 高
opened: 2026-10-24
closed: null
related: [improve-objective-filtered-signal-quality, improve-new-candidate-metric-reference-set-freeze]
---
# [improve-context-family-attempt-summary] 改善 prompt に仮説族ごとの試行回数と結果区分を渡す

**状態**: 実装中 / **優先**: 高

## 現象

改善ループは試行済み idea の本文と固定語彙の last_result、配備済み族しか受け取らず、候補 archive (46 件・24 族) の族別の消費状況と結果区分 (not_submitted / rejected_by_human / approved / revoked) を知らない。agent は未試行の族を順に消費するだけで、24 族消費して床を越えたものがゼロという事実も伝わらない。成績 (PF 等) は遮断 8 のとおり渡さず、族 key・試行回数・結果区分・最終試行日だけを渡す (第 1 段)。第 2 段 (in-sample 成績の族別順序尺度) は holdout 漏洩との境界を別に設計する。2026-10-24 ユーザー承認 (着手順 1)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-24: 起票。
- 2026-10-24: 状態: 実装待ち — 設計書 docs/superpowers/specs/2026-10-24-improve-feedback-structure-design.md v0.3a で収束 (C0 + 2 周 + 閉鎖確認)、2026-10-24 ユーザー承認。T1 (中) が本チケット
- 2026-10-24: 状態: 実装中 — T1 (family key の一本化と文法、archive の 3 列投影、outcome と候補名の投影、族別集計と bounded render、prompt の「仮説族の試行状況」節) を実装。codex r1 High 2 / Medium 1、/code-review high 10 件 (2 件却下・吸収)、閉鎖確認 2 周を是正して merge。設計書 v0.3b。判定 loops+plugin+store+tools+commands+service 4596 green。残り T2 (get_signals 不在の文面と allowlist 観測) は improve-deployed-signal-lookup-gap
