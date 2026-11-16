---
id: killswitch-latch-pending-limit-fills
title: kill switch のラッチ中も未約定の指値が約定する
status: 実装中
priority: 中
kind: 不具合
opened: 2026-11-16
closed: null
related: [killswitch-latched-activity-repeats]
---
# [killswitch-latch-pending-limit-fills] kill switch のラッチ中も未約定の指値が約定する

**状態**: 実装中 / **優先**: 中 / **種別**: 不具合

## 現象

ラッチは新規の open を risk gate で止めるが、既に出ている未約定の指値 (PENDING_FILL) は scheduler の約定判定で kill switch を見ずに約定する (約定を止めるのは指標回避帯の _suppress_fill だけ、core/scheduler.py:1599)。指値の予約リスクは発注時に max_total_risk_pct に数えているので上限は超えないが、新規停止後に建玉が増える。設計 §5 の「kill switch は新規停止」と食い違う。処置案: ラッチ時に未約定の指値を取り消すか、ラッチ中は約定させない (エクスポージャー縮小方向・決定論的)。replay の kill switch 規約との整合も要確認

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-16: 起票。
- 2026-11-16: 状態: 実装中 — 設計 C0.2 (docs/superpowers/specs/2026-11-16-killswitch-latch-pending-limits-design.md) を 2026-11-16 に利用者承認。案 A: ラッチ中は開場 tick ごとに全 pending を取消し約定停止、DD 閾値以上は約定だけ停止、state 読取不能・lock 競合は約定だけ停止
