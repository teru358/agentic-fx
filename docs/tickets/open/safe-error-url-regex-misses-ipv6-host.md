---
id: safe-error-url-regex-misses-ipv6-host
title: _safe_error の URL 伏せが IPv6 host の URL を全く伏せない
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-10-25
closed: null
related: [news-source-cli-and-429-backoff]
---
# [safe-error-url-regex-misses-ipv6-host] _safe_error の URL 伏せが IPv6 host の URL を全く伏せない

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

`_safe_error.py` の `_URL_RE` は `[` `]` を URL 文字から除外しているため、`http://[2001:db8::1]:8080/path?key=…` のような IPv6 host の URL が one_line_error / safe_error_text で伏せられず、query に鍵があればそのまま表示・activity・通知へ出る。broker・bridge の経路にも及ぶ。afx news (spec 2026-10-25 v0.6) は regex に頼らない固定文の設計にしたので影響しないが、既存経路の穴として残る。是正案: `[`/`]` を host 部で許す regex に直し、IPv6 の fixture (bracket あり・port あり・query あり) を pin する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
