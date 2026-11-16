---
id: history-audit-work-root-guarded-misreported
title: 非配備版の監査で work_root を隔離の保護領域に置くと理由が worker_failed になる
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-15
closed: null
related: [live-confirmed-bars-into-history]
---
# [history-audit-work-root-guarded-misreported] 非配備版の監査で work_root を隔離の保護領域に置くと理由が worker_failed になる

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

audit_non_deployed_version の work_root を data/ など改善隔離の保護領域 (data/・config/・logs/・~/.config) の下に置くと、隔離 worker の allowlist が allowlist_guarded で拒まれ、7 件とも worker_failed と出る (2026-11-14 の段 1b 1 回目)。置き場の誤りだと分からない。直し方の案: 監査の開始前に work_root を検査して理由付きで止める。運用手順 docs/operations/reevaluate-deployed-after-history-repair.md §8 に置き場の条件 (保護領域と plugins/ の外、例 tmp/ の下) を書く。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-15: 起票。
