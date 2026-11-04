---
id: mt5-bridge-applies-current-offset-to-all-history
title: bridge がいまの offset を全履歴に当て、DST 切替前の足が 1 時間ずれる
status: 是正済
priority: 高
kind: 不具合
opened: 2026-11-02
closed: 2026-11-03
related: []
---
# [mt5-bridge-applies-current-offset-to-all-history] bridge がいまの offset を全履歴に当て、DST 切替前の足が 1 時間ずれる

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

MT5 サーバ時刻は NY+7h (夏 UTC+3 / 冬 UTC+2、2026-11-01 22:05 UTC に 10800→7200)。mt5_bridge は live に測った offset 1 つを copy_rates_range の全履歴に適用する (mt5_client.py:71-79 / :614-630) ため、切替前の足をいま取り直すと 1 時間遅れのラベルで返る (10/30 の最終足が cache 20:59 / MT5 21:59、OHLC 一致)。切替直後の最大 5 分は旧 offset のまま ingest し、11/01 21:01-21:04 の誤ラベル行が cache に残った (正しい 22:01-22:04 と同値の重複)。週末は閉場後の古い tick から異常 offset (-16200〜-43200) を採用 (bridge.log 2026-10-03)。処置: 足ごとにその時刻の規則 (America/New_York + 7h) で換算、測定 offset は規則の検証にだけ使い不一致は警告、週末の異常値を拒否。報告 tmp/review-notick/dst-probe.md

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-03: bridge 足ごと換算 da3a0fe9、履歴と cache の修復 bc932d85 (本番 apply 2026-11-03 13:20Z: 履歴 146,632 行 +1h、cache 削除 9 / 置換 2、backup と manifest あり)

## 経緯

- 2026-11-02: 起票。
- 2026-11-03: 設計 docs/superpowers/specs/2026-11-02-utc-time-rules-dst-design.md。bridge の足ごとの換算と規則検査は T3、履歴と cache の修復は T4 で、R1 には入らない。bridge 是正までは `afx history import` を走らせない。
- 2026-11-03: 状態: 実装中 — 2026-11-03: bridge 側を配備 (main da3a0fe9、Wine に tzdata 2026.6、bridge のみ再起動)。足ごとの NY 規則換算と規則検査、検査済みのときだけ /ohlcv /quote。実 bridge で 2026-10-30 最終足 20:55Z・2025-11-02 最初の足 22:00Z・2026-03-08 最初の足 21:00Z を確認。残りは保存済みの履歴と cache の修復 (T4、dry-run を利用者に見せてから apply)。土曜の 503 は週末に確認
- 2026-11-03: 是正内容 — bridge 足ごと換算 da3a0fe9、履歴と cache の修復 bc932d85 (本番 apply 2026-11-03 13:20Z: 履歴 146,632 行 +1h、cache 削除 9 / 置換 2、backup と manifest あり)
