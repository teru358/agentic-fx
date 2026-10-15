---
id: wizard-stage9-summary-in-user-terms
title: 段 9 の要約は設定キーでなく利用者の言葉で、差分は d で開く
status: 実装中
priority: 中
opened: 2026-10-14
closed: null
related: [first-run-setup, wizard-stage8-ask-daemon-not-api]
---
# [wizard-stage9-summary-in-user-terms] 段 9 の要約は設定キーでなく利用者の言葉で、差分は d で開く

**状態**: 実装中 / **優先**: 中

## 現象

段 9 の要約が settings.yaml のキー (datafeed.mt5.bridge_url: … → …) と内部用語 (確定 1 / 確定 2、planned) で、利用者には読めない。要約は利用者が答えた言葉 (価格源 / 取引判断と改善の backend とモデル / 判断足 / backtest の基底足と履歴源 / 改善周期と次回と最初の 1 本 / 操作 API = 常駐運用 / 作るもの / このあと尋ねること) で出し、問いは「この内容で書き込みます [Y/n]」。settings.yaml の生の差分は `d` で開ける (再実行や手編集する人向け) 形に残す。未確認の項目は固定語でなく文 (「bridge の接続は未確認」)。2026-10-14 stream-svr 受入 1 回目のユーザー指摘

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。
- 2026-10-15: 状態: 実装中 — 束 fr-fix2 (spec v1.8、ブリーフ tmp/review-20261011-sc/fr-fix2-1.md / fr-fix2-2.md)
