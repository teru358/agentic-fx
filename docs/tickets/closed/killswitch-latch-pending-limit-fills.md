---
id: killswitch-latch-pending-limit-fills
title: kill switch のラッチ中も未約定の指値が約定する
status: 是正済
priority: 中
kind: 不具合
opened: 2026-11-16
closed: 2026-11-17
related: [killswitch-latched-activity-repeats]
---
# [killswitch-latch-pending-limit-fills] kill switch のラッチ中も未約定の指値が約定する

**状態**: 是正済 / **優先**: 中 / **種別**: 不具合

## 現象

ラッチは新規の open を risk gate で止めるが、既に出ている未約定の指値 (PENDING_FILL) は scheduler の約定判定で kill switch を見ずに約定する (約定を止めるのは指標回避帯の _suppress_fill だけ、core/scheduler.py:1599)。指値の予約リスクは発注時に max_total_risk_pct に数えているので上限は超えないが、新規停止後に建玉が増える。設計 §5 の「kill switch は新規停止」と食い違う。処置案: ラッチ時に未約定の指値を取り消すか、ラッチ中は約定させない (エクスポージャー縮小方向・決定論的)。replay の kill switch 規約との整合も要確認

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-17: 開場 tick で state を一回の非 blocking 試行で読み、ラッチ中 (marker を含む) は全 pending を close_reason kill_switch で取消して約定を止め、DD 閾値以上は約定だけ止める (取消・ラッチ書込みなし)。state 読取不能・lock 競合は約定だけ止める。DD 判定は risk gate と共有の accounting.drawdown_kill_reached。停止・復旧・DD 停止の遷移を activity に記録し、afx-ops の取消理由表示に kill_switch を追加。clear→latched の writer の全数を AST で pin。設計 docs/superpowers/specs/2026-11-16-killswitch-latch-pending-limits-design.md (C0.2)。影響地図の頁を同 commit で更新 (F174–F184、K-LATCH-WRITERS、K-PENDING-STOP、D12–D14、E79–E90)。レビューは設計 3 周、実装 codex 1 周目・/code-review high・閉鎖確認 2 回。root と afx-ops の判定は既知の実 CLI の 1 件を除き緑

## 経緯

- 2026-11-16: 起票。
- 2026-11-16: 状態: 実装中 — 設計 C0.2 (docs/superpowers/specs/2026-11-16-killswitch-latch-pending-limits-design.md) を 2026-11-16 に利用者承認。案 A: ラッチ中は開場 tick ごとに全 pending を取消し約定停止、DD 閾値以上は約定だけ停止、state 読取不能・lock 競合は約定だけ停止
- 2026-11-17: 是正内容 — 開場 tick で state を一回の非 blocking 試行で読み、ラッチ中 (marker を含む) は全 pending を close_reason kill_switch で取消して約定を止め、DD 閾値以上は約定だけ止める (取消・ラッチ書込みなし)。state 読取不能・lock 競合は約定だけ止める。DD 判定は risk gate と共有の accounting.drawdown_kill_reached。停止・復旧・DD 停止の遷移を activity に記録し、afx-ops の取消理由表示に kill_switch を追加。clear→latched の writer の全数を AST で pin。設計 docs/superpowers/specs/2026-11-16-killswitch-latch-pending-limits-design.md (C0.2)。影響地図の頁を同 commit で更新 (F174–F184、K-LATCH-WRITERS、K-PENDING-STOP、D12–D14、E79–E90)。レビューは設計 3 周、実装 codex 1 周目・/code-review high・閉鎖確認 2 回。root と afx-ops の判定は既知の実 CLI の 1 件を除き緑
