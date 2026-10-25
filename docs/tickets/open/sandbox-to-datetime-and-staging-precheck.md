---
id: sandbox-to-datetime-and-staging-precheck
title: 改善 sandbox: to_datetime 拒否と test_plugin.py の事前検査欠如
status: 設計待ち
priority: 中
opened: 2026-10-25
closed: null
related: [improve-mission-indicator-creation-path]
kind: 不具合
---
# [sandbox-to-datetime-and-staging-precheck] 改善 sandbox: to_datetime 拒否と test_plugin.py の事前検査欠如

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

mission 2037 は backtest 3 回 ok の後、最終 gate で test_plugin.py の pd.to_datetime が「attribute to_datetime is not allowed」で拒否された。sandbox.check_source の to_ 接頭辞禁止の例外 (_TO_ALLOWED) に純粋な型変換 to_datetime / to_timedelta / to_numeric が無い。構造的に (1) mission 中の backtest は plugin.py しか検査せず test_plugin.py の AST 検査は mission 終了後の最終 gate が初回、(2) write_staging_file に事前検査が無い、(3) 次 mission へ渡る last_result は gate_failed の語頭のみで理由が届かない、(4) improve_mission.md に denylist の説明が無い。過去同種 3 件 (import typing 拒否 2、to_datetime 1)。是正案: _TO_ALLOWED に 3 名を追加、typing の import 許可を検討、write_staging_file 直後に check_source を走らせ拒否理由 (静的規則文、漏洩なし) をツール結果で返す、prompt に denylist 要約を 1 段落。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与
