---
id: news-fetch-no-user-agent-yahoo-429
title: ニュース取得が User-Agent を付けず Yahoo が毎回 429 を返す
status: 見送り
priority: 中
opened: 2026-10-02
closed: 2026-10-16
related: []
---
# [news-fetch-no-user-agent-yahoo-429] ニュース取得が User-Agent を付けず Yahoo が毎回 429 を返す

**状態**: 見送り / **優先**: 中

## 現象

yahoo-finance-topstories は登録の 1 秒後 (2026-09-02 10:40:51 UTC) から全試行が HTTP 429。30 分ごとに 1 日 48 回前後失敗し続けている。同じ IP からブラウザの User-Agent で取得すると 200 (2026-10-02 実測) なので IP の遮断ではない。nhk-keizai と fxstreet-news は失敗なし。

## 原因

news_collector は User-Agent を指定しておらず、既定の python-httpx が拒否されている見込み。失敗が続く源への取得間隔を広げる仕組みも無い (起動時のクールダウンは retry_after_sec を見るが、定期取得は 30 分固定)。

## 処置案・裁定

(1) 取得に識別できる User-Agent (リポジトリ名と連絡先) を付ける。ブラウザを装う UA は使わない。それで通らなければ Yahoo を既定のソースから外す (ソースの追加・無効化は人間の運用操作)。(2) 連続失敗した源は取得間隔を段階的に広げ、status で読めるようにする。(3) Yahoo の記事が 1 件でも保存されているかを確認する。

## 修正内容

## 経緯

- 2026-10-02: 起票。
- 2026-10-16: 見送り理由 — news-source-cli-and-429-backoff に吸収 (UA を名乗る取得、Retry-After なしの 429 連続時のバックオフ、afx news による源の無効化)。2026-10-15 の実測では UA 付きでも 404 で源自体が止まっている
