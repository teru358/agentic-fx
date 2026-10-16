---
id: news-source-cli-and-429-backoff
title: afx news (list/add/enable/disable/test) と 429 連続時のバックオフ
status: 設計待ち
priority: 中
opened: 2026-10-15
closed: null
related: [news-source-route-omitted]
---
# [news-source-cli-and-429-backoff] afx news (list/add/enable/disable/test) と 429 連続時のバックオフ

**状態**: 設計待ち / **優先**: 中

## 現象

ニュース源を操作する利用者向けコマンドが無く (store/news_sources の add/list/set_enabled だけ)、源の追加・無効化は DB を直接触るしかない。yahoo-finance-topstories は 2026-09-02 から毎周期 429 (Retry-After なし、UA を付けると 404 = 源が止まった) で累計 1,170 回失敗し続け、収集器は何もせず 30 分ごとに再試行する。設計 (2026-10-15 ユーザー承認): (1) 最上位サブコマンド `afx news` を afx plugin と同じ流儀 (daemon を経由せず直接 DB、activity log に news_source_added / enabled / disabled) で追加: `list` (名前・有効・URL・直近の成否と件数を activity から)、`add <name> <url> [--fetcher feed|web]` (追加前に 1 回取得して件数と要約の有無を表示、0 件なら確認)、`enable|disable <name>`、`test <name>` (1 回取得して件数・要約の有無・HTTP 状態、DB は変えない)。反映は次の収集周期 (最長 30 分) と表示。bot / web からは操作しない (源の追加は人間の運用操作、2026-09-07 裁定)。(2) 収集器: Retry-After の無い 429 / 5xx が連続 N 回 (3) で指数バックオフ (30 分 → 1 時間 → … 上限 1 日)、バックオフ中は news_source_skipped を期限ごとに 1 回、status の未完手順に「源 <name> が連続で失敗」を出す。(3) 受入: 本機の yahoo を afx news disable で止め (コマンド処理の実機確認に使う)、activity に news_source_disabled が出て次周期から失敗が消えること。代替源の追加はユーザーの運用判断 (候補の実測表は別に出す)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-15: 起票。
- 2026-10-16: 優先: 中 — 吸収: news-fetch-no-user-agent-yahoo-429 の「ニュース取得が User-Agent を付けない (python-httpx 既定)」も直す。収集器の取得に固定の UA (製品名/版 + repo URL) を付ける。yahoo は 2026-10-15 の実測で UA 付きでも 404 (源が止まった) なので UA だけでは直らない
