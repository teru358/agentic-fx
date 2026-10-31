---
id: news-store-duplicate-id-in-batch
title: 同一 feed 内の重複 URL で news の保存バッチが丸ごと失敗する (DuplicateIDError)
status: 実装待ち
priority: 高
kind: 不具合
opened: 2026-10-31
closed: null
related: []
---
# [news-store-duplicate-id-in-batch] 同一 feed 内の重複 URL で news の保存バッチが丸ごと失敗する (DuplicateIDError)

**状態**: 実装待ち / **優先**: 高 / **種別**: 不具合

## 現象

BBC business RSS (bbci) が同じ記事を 1 回の feed に 2 回含む (2026-10-31 実測: 50 件中一意 48、重複 2)。store/rag.py の add_news は URL (または rag_id) をそのまま ID にして chromadb に一括 upsert するため、バッチ内に同じ ID が 2 つあると DuplicateIDError でその源の全記事が保存されない。news_collector.py:97 が warning (news source bbci store failed (DuplicateIDError)) と activity NEWS news_store_failed bbci を出し、30 分ごとに再発 (2026-10-31 04:18 UTC〜連続)。afx news test は取得だけなので通る。健康記録は fetch 成功扱いで backoff はかからない。影響: bbci の記事が RAG に入らない。

## 原因

add_news がバッチ内 ID の一意化をしていない。chromadb の upsert は同一呼び出し内の重複 ID を拒否する。

## 処置案・裁定

処置案: add_news でバッチ内の ID を一意化する (同じ ID は後勝ちで 1 件に畳む、返り値は保存件数)。feed 側 (fetchers.fetch_feed) でなく保存側で畳むと web 源や将来の源にも効く。test: 同じ URL を 2 件含む articles で add_news が例外なく 1 件として保存される。実 feed の重複を写した fixture を 1 本。

## 修正内容

## 経緯

- 2026-10-31: 起票 (ログの DuplicateIDError をユーザーが発見、原因は指揮者が実 feed で確認)。
