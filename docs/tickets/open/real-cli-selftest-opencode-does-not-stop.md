---
id: real-cli-selftest-opencode-does-not-stop
title: 実 CLI selftest の opencode が echo の後に止まらず tool を呼び続ける
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: []
---
# [real-cli-selftest-opencode-does-not-stop] 実 CLI selftest の opencode が echo の後に止まらず tool を呼び続ける

**状態**: 設計待ち / **優先**: 低

## 現象

seccomp T6 の実 CLI selftest (-m realbackend) で、opencode は最初の step で期待の {"echo": ...} を返した後も止まらず afx_web_search / afx_fetch_article を呼び続け、mission の上限まで走る (prompt は「tool を呼ぶな」だが MCP 経由で afx_* tool が見える)。判定基準 (echo の後に止まること、tool が見えること) を見直すか、selftest 用に tool を隠す。合わせて失敗理由が cleanup_deadline_received に化けて CLI の実際の結果 (schema 不一致) が見えない観測性の問題も扱う。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。
