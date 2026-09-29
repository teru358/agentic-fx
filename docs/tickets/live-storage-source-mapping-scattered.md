---
id: live-storage-source-mapping-scattered
status: 設計待ち
priority: 中
opened: 2026-09-28
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [live-storage-source-mapping-scattered]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[live-storage-source-mapping-scattered] 起票 2026-09-28 (中、整理)**: primary → ライブ保存名 (`mt5` → `mt5-live`) の写像が 8 箇所に散在 (service.py 611/792/1218/1232、ingest.py:27、scheduler.py:443、price_provider.py:48、store/ohlcv.py:77)。`store/ohlcv.py` に `live_storage_source(primary)` を 1 本置いて全数を寄せる (ライブ専用、`_table` の LIVE/IMPORT 分離は変えない)。astra 助言 2026-09-28

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

