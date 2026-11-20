---
id: web-ui-charts-equity-indicators
title: 同梱 web に資金遷移グラフとチャート重ね表示
status: 是正済
priority: 低
opened: 2026-10-11
closed: 2026-11-20
related: []
---
# [web-ui-charts-equity-indicators] 同梱 web に資金遷移グラフとチャート重ね表示

**状態**: 是正済 / **優先**: 低 / **種別**: 機能

## 現象

同梱 web (ops-web) は当面最小構成 (素の HTML + 固定 CSS、読み取り専用、JS なし)。最終的には資金 (equity / balance) の遷移グラフと、価格チャートに指標・戦略シグナル・建玉を重ねた表示を提供したい (2026-10-11 ユーザー方針)。CSP と read-only principal の範囲で、server 側描画 (SVG) か限定的な JS かは設計時に決める。着手はウィザード後。

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-20: 資金推移と戦略ごとの成績の Performance 画面を足した (設計 docs/superpowers/specs/2026-11-19-web-charts-equity-strategy-design.md C1.3)。daemon に読取 API GET /v1/performance (資金推移の束・HWM の段・印・今の状態を daemon と同じ判定で、戦略別の帰属・指標・累積)、web に server SVG の図と表 (外部 JS なし、STYLE 3,967 byte)、Status と共通の状態の語の表。T1〜T4 は各 terra / code-review high / 閉鎖確認、frontend-design を物差しにした批評の 20 件も反映。main に merge 0a6ff1c9、2026-11-20 17:51 に afx と afx-ops@web/bot を再起動、利用者が本番の web で確認。範囲外の 2 つは web-decision-close-vs-hold-r-compare・web-price-chart-overlays、残余の資金保護の不具合は account-snapshot-future-ts-treated-fresh

## 経緯

- 2026-10-11: 起票。
- 2026-11-17: 状態: 設計待ち — 利用者の追加要望 (2026-11-17): 資金推移のページに戦略ごとの成績を表示したい。取引は判断 mission の起動理由 (missions.trigger) で戦略に結び付く: signal:<plugin> はその戦略のシグナルで起きた判断、cron は定期の判断で戦略に属さない。建てる判断は LLM が行うので、表示は「その戦略のシグナルで起きた判断の成績」と明記する。候補: 資金推移の線 (equity / balance / HWM、kill switch の発動と HWM の付け直しの印)、戦略 (起動理由) ごとの累積損益の線と表 (件数・勝率・PF・平均 R・決済理由の内訳)、判断による決済とそのまま SL/TP まで持った場合の R の比較。daemon の読取 API (戦略別集計と時系列) → web のページ → チャートの JS の順。web の順序は配置 → CSS → チャートの裁定 (2026-11-15) のうちチャートの段にあたる
- 2026-11-19: 状態: 実装中 — 設計 docs/superpowers/specs/2026-11-19-web-charts-equity-strategy-design.md C1 を利用者承認 (§6 は 6 件とも推奨どおり)。範囲は資金推移と戦略別の成績。比較と重ね表示は web-decision-close-vs-hold-r-compare・web-price-chart-overlays へ
- 2026-11-20: 状態: 実装中 — T1〜T4 実装・レビュー済み (branch webcharts)。残りは T5 の実ブラウザの受入
- 2026-11-20: 状態: 実装中 — main に merge 0a6ff1c9、afx と afx-ops@web/bot を再起動 (2026-11-20 17:51、DB backup data/agentic.db.bak-20261120-webcharts)。T5 は指揮者が headless Firefox で fixture の明・暗・390px・異常を確認済み、本番の web での利用者の確認待ち
- 2026-11-20: 是正内容 — 資金推移と戦略ごとの成績の Performance 画面を足した (設計 docs/superpowers/specs/2026-11-19-web-charts-equity-strategy-design.md C1.3)。daemon に読取 API GET /v1/performance (資金推移の束・HWM の段・印・今の状態を daemon と同じ判定で、戦略別の帰属・指標・累積)、web に server SVG の図と表 (外部 JS なし、STYLE 3,967 byte)、Status と共通の状態の語の表。T1〜T4 は各 terra / code-review high / 閉鎖確認、frontend-design を物差しにした批評の 20 件も反映。main に merge 0a6ff1c9、2026-11-20 17:51 に afx と afx-ops@web/bot を再起動、利用者が本番の web で確認。範囲外の 2 つは web-decision-close-vs-hold-r-compare・web-price-chart-overlays、残余の資金保護の不具合は account-snapshot-future-ts-treated-fresh
