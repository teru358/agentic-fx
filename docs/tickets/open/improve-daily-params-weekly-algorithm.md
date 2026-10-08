---
id: improve-daily-params-weekly-algorithm
title: 改善を日次のパラメータ調整と週次のアルゴリズム改善に分ける
status: 設計待ち
priority: 中
opened: 2026-10-03
closed: null
related: [first-run-setup]
---
# [improve-daily-params-weekly-algorithm] 改善を日次のパラメータ調整と週次のアルゴリズム改善に分ける

**状態**: 設計待ち / **優先**: 中

## 現象

改善 loop は週 1 本 (Sat 03:00 JST) の LLM mission だけで、不採用が続くと何も進まない。候補探索は backtest なのでライブ 1 週間分を待つ理由が無い。一方で取引中 (15m 判断) に重い改善は走らせにくい。現行の Sat 03:00 JST は NY 金曜引け (JST 土 06:00〜07:00) より前で取引中に走っている (2026-10-03 ユーザー指摘)。

## 原因

cadence が 1 種類で、軽い調整 (params) と重い改善 (ロジック) を同じ口で扱っている。

## 処置案・裁定

日次 = 採用済み/候補の config params を決定論 sweep で backtest (LLM 不要、取引の薄い時間帯、小さい CPU 上限、保護 tick を塞がない)。出力は結果表 + params 変更の候補 (人間承認は現行どおり、週末にまとめて)。週次 = 今の LLM mission をアルゴリズム改善に特化、閉場後 (Sat 08:00 JST 以降) に動かし timeout と本数を増やす。設計事項: params の調整可能範囲の宣言方法、日次結果の週次への受け渡し (holdout 漏洩の規律と整合)、日次候補の上限と承認画面の束ね方。先行して improve_at を閉場後へ (設定 1 行)。

## 修正内容

## 経緯

- 2026-10-03: 起票。
- 2026-10-03: 関連: [baseline-replay-unimplemented] — 2026-10-03: この環境の settings は Sat 08:00 に変更済。製品既定 (config.py の Sat 03:00、settings.yaml.example) は取引中なので、設計時に閉場後の既定へ変える。
- 2026-10-03: 関連: [first-run-setup] — 2026-10-03: improve_at は display_timezone で解釈されるので他の標準時でも動くが、「閉場後」の時刻は標準時ごとに違う (JST Sat 08:00 = London Sat 00:00 頃 = NY Fri 19:00 頃)。製品既定は固定文字列でなく市場基準 (NY 閉場の N 時間後を表示 TZ に換算) も選べる形にし、ウィザードで提案する。
