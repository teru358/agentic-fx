---
id: econ-event-actuals-and-reaction-stats
title: 指標の発表実績を取得し、種別ごとの価格反応を決定論的に集計する
status: 是正済
priority: 中
kind: 機能
opened: 2026-10-30
closed: 2026-11-07
related: [econ-event-hold-window-deterministic, llm-vs-strategy-signal-authority]
---
# [econ-event-actuals-and-reaction-stats] 指標の発表実績を取得し、種別ごとの価格反応を決定論的に集計する

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

econ_events は forecast / previous だけで actual が 1026 件中 0 件 (econ_calendar.py は発表前の予定だけ取り込み、発表後に実績を更新しない)。発表内容と、それを受けた価格推移が蓄積されていない。対策: (1) 発表後に actual を更新 (surprise = actual − forecast)、(2) イベント種別 (国 × 名前) ごとに直近 N 回の発表後 15m / 1h / 4h の値動き幅と surprise の符号との関係を ohlcv_cache の 1m 足から決定論的に集計 (新しい保存でなく派生集計)、(3) 使い道は回避帯の幅を種別ごとに実測で決めることと、tool で LLM に数値統計を渡すこと。LLM に過去の物語を読ませる形は採らない (CPI 年 12 回 / FOMC 年 8 回で標本が小さく過剰一般化する)。設計前にブレスト (ユーザー要望) と sol の前提確認

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-07: 反応統計 (T1〜T4) を main 8106f486 で配備 (2026-11-07)。econ slice worker の cycle 後処理が終端の event set を DB 再走査して反応値を 1 回だけ保存し、(通貨, 重要度) と (通貨, 名前) の束で median / p75 / n を集計する。値の無い event set は purge しない。trade の cron mission に get_econ_reactions (n 20 未満は insufficient のみ)、ctl status / web に反応統計の欄。反応統計を呼んだ非 signal mission の判断理由は振り返りに渡さない (intent の reasoning_source)。全範囲の判定は既知の失敗と main でも落ちるもの以外は緑。配備後の ctl status で未算定は起動直後の 32 から 0 に、束 99 (床 20 以上 3)。actual の取得は源に無く対象外

## 経緯

- 2026-10-30: 起票。
- 2026-10-30: 状態: 裁定待ち — ブレスト → sol 前提確認 → 設計 C0 の順 (3 件を一束で)
- 2026-11-01: 状態: 実装待ち — 設計 C0 v0.9 (docs/superpowers/specs/2026-11-01-econ-window-signal-authority-shadow-design.md) を 8 周のレビューで清書、J1〜J3 をユーザーが推奨どおり承認。3 件は束 1 (回避帯・採否契約・shadow、T1a〜T5) と束 2 (反応統計、T1〜T4) で実装。actual 取得は対象外 (源に無い)
- 2026-11-07: 状態: 実装待ち — 束 1 (slice の退避と 1m 規則、反応統計の後処理差込口) は 2026-11-07 に配備 (main c0aa9bb0)。反応統計の集計・tool・人間面は束 2 (T1〜T4)
- 2026-11-07: 状態: 実装中 — 束 2 着手。前提確認 (sol) で設計 v0.10.5 (反応値は DB 再走査の cycle 後処理、unrecoverable も 3 窓 unresolvable、値の無い event set は purge しない)。利用者が進め方を承認
- 2026-11-07: 是正内容 — 反応統計 (T1〜T4) を main 8106f486 で配備 (2026-11-07)。econ slice worker の cycle 後処理が終端の event set を DB 再走査して反応値を 1 回だけ保存し、(通貨, 重要度) と (通貨, 名前) の束で median / p75 / n を集計する。値の無い event set は purge しない。trade の cron mission に get_econ_reactions (n 20 未満は insufficient のみ)、ctl status / web に反応統計の欄。反応統計を呼んだ非 signal mission の判断理由は振り返りに渡さない (intent の reasoning_source)。全範囲の判定は既知の失敗と main でも落ちるもの以外は緑。配備後の ctl status で未算定は起動直後の 32 から 0 に、束 99 (床 20 以上 3)。actual の取得は源に無く対象外
