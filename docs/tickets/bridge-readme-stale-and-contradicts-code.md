---
id: bridge-readme-stale-and-contradicts-code
title: bridge の README が現物と逆 (発注 endpoint は未実装と記載)
status: 実装中
priority: 高
opened: 2026-10-02
closed: null
related: []
---
# [bridge-readme-stale-and-contradicts-code] bridge の README が現物と逆 (発注 endpoint は未実装と記載)

**状態**: 実装中 / **優先**: 高

## 現象

mt5_bridge/README.md は「発注 endpoint は意図的に未実装」と書くが現物には発注系 endpoint がある。手順は finance 時代のまま (git clone <finance-repo-url>、feature/mt5-integration、設定キー mt5_bridge: は agentic-fx では未知キーで起動拒否)。.env.example と README に作者の口座の種類・ブローカー名・実測環境名が残る。

## 原因

## 処置案・裁定

README を agentic-fx 用に書き直す (配置して afx とつなげるまで。Wine と MT5 本体の導入は扱わない)。発注系 endpoint と DRY_RUN、待受と API キーを正しく説明する。作者固有の記述を除く。初期設定ウィザードの案内文と同じ手順にする。

## 修正内容

## 経緯

- 2026-10-02: 起票。
- 2026-10-03: 状態: 実装中 — 2026-10-03 着手 (並行実装、Landlock 設計の収束待ちの間)。
