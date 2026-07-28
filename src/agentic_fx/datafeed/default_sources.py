"""素の clone で動く基本ニュースソース (設計書 §6)。

**到達性・日付フォーマットの既知の限界**: この作業環境にはネットワークが
無く、各 URL への実接続確認・実際の RSS 応答 (特に日付欄のタイムゾーン
表記) の確認ができていない。特に CST (中国標準時、+8h) を「US Central
(-6h)」と誤読する feedparser の既知の罠 (datafeed/fetchers.py 参照) が
無いかは、以下の 3 件について未確認のまま採用している:

- yahoo-finance-topstories: 米国のサービスであり中国標準時を名乗る理由が
  薄いと推測されるが、実際のフィード応答は未確認。
- nhk-keizai: 日本の公共放送。発行日時は "+0900" 等の数値オフセットで
  出るものと推測される (数値オフセットなら略号辞書を経由せず
  `_unrecognized_tz_abbrev` の対象にもならない) が、実データでは未確認。
  なお "cat5.xml" が実際に経済カテゴリかどうかも未確認 — NHK のカテゴリ
  番号割当を裏付ける資料にアクセスできていない。
- fxstreet-news: 国際的な金融メディアで中国標準時を名乗る理由は薄いと
  推測されるが未確認。

いずれも「確認できない」であって「問題なし」ではない。

**重要な注意 (CST は無音)**: fetchers.py の `_unrecognized_tz_abbrev` 警告は
「未知の略号」だけを検知する仕組みであり、`CST`/`CDT` は feedparser の
辞書に既に「US Central (-6h)」として登録されているため、中国標準時
(+8h) の意味で `CST` を出すソースがあっても **警告は一切出ない** まま
14 時間ずれる (Task 5 のレビューで判明した既知の罠、本ファイル冒頭参照)。
したがって技術ログの警告有無はこの問題の検知手段にならない。確認する
には実際のフィード応答の生の日付文字列 (pubDate 等) を目視するしかなく、
それにはネットワーク接続が要る — この環境では実施できていない。
"""
DEFAULT_SOURCES: list[dict] = [
    {"name": "yahoo-finance-topstories", "fetcher": "feed",
     "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "nhk-keizai", "fetcher": "feed",
     "url": "https://www3.nhk.or.jp/rss/news/cat5.xml"},
    {"name": "fxstreet-news", "fetcher": "feed",
     "url": "https://www.fxstreet.com/rss/news"},
]
