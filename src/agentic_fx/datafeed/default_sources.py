"""素の clone で動く基本ニュースソース (設計書 §6)。

**到達性・日付表記の実測 (2026-07-29、修正ラウンド 1)**: 3 件すべてに実接続し、
`feedparser.parse` の生の応答を確認した。結果:

| ソース | status | bozo | entries | 生の日付表記 | 本文 (summary) |
|---|---|---|---|---|---|
| yahoo-finance-topstories | 200 | False | 42 | `2026-07-28T03:33:00Z` | **無し (全件で空)** |
| nhk-keizai | 200 | False | 66 | `Wed, 29 Jul 2026 18:20:42 +0900` | 有り |
| fxstreet-news | 200 | False | 30 | `Wed, 29 Jul 2026 09:29:21 GMT` | 有り |

- **CST の罠 (Task 5 からの申し送り) は 3 件とも該当しない**。日付表記は
  ISO8601 の `Z` 終端 / 数値オフセット `+0900` / `GMT` の 3 種で、いずれも
  feedparser の略号辞書の曖昧性 (`CST` を US Central `-6h` と読む件) を
  踏まない。ただしこれは**この時点のスナップショット**であり、ソース側が
  日付表記を変えれば無音で壊れる性質は変わらない (下記「CST は無音」参照)。
- nhk-keizai の `cat5.xml` は**実測で経済カテゴリ**だった (株価・企業決算等)。
- **既知の欠点 — yahoo-finance-topstories は `summary` 要素を一切持たない**
  ため、`fetch_feed` が返す Article の `body` が全件で空になる。RAG に入る
  ドキュメントがタイトルだけになり、埋め込みの情報量も LLM が読める内容も
  他 2 件より大きく劣る。クラッシュはしないので Task 7 の範囲では採用を
  変えず、扱い (差し替え / fetcher="web" での本文取得 / 許容) は
  **ユーザー裁定に回す**。

**重要な注意 (CST は無音)**: fetchers.py の `_unrecognized_tz_abbrev` 警告は
「未知の略号」だけを検知する仕組みであり、`CST`/`CDT` は feedparser の
辞書に既に「US Central (-6h)」として登録されているため、中国標準時
(+8h) の意味で `CST` を出すソースがあっても **警告は一切出ない** まま
14 時間ずれる (Task 5 のレビューで判明した既知の罠)。したがって技術ログの
警告有無はこの問題の検知手段にならず、上表のような実応答の目視でしか
確認できない。ソースを追加・変更する際は同じ確認を行うこと。
"""
DEFAULT_SOURCES: list[dict] = [
    {"name": "yahoo-finance-topstories", "fetcher": "feed",
     "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "nhk-keizai", "fetcher": "feed",
     "url": "https://www3.nhk.or.jp/rss/news/cat5.xml"},
    {"name": "fxstreet-news", "fetcher": "feed",
     "url": "https://www.fxstreet.com/rss/news"},
]
