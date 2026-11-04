---
id: market-hours-fixed-21utc-ignores-dst
title: market_hours の 21:00 UTC 固定が DST を無視し冬は境界が 1 時間ずれる
status: 是正済
priority: 高
kind: 不具合
opened: 2026-11-02
closed: 2026-11-04
related: []
---
# [market-hours-fixed-21utc-ignores-dst] market_hours の 21:00 UTC 固定が DST を無視し冬は境界が 1 時間ずれる

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

週の開場・閉場と日次ロールオーバーは broker サーバの 00:00 = NY 17:00 で、夏 21:00 UTC / 冬 22:00 UTC (2026-11-01 から冬、次は 2027-03-14)。core/market_hours.py は 21:00 UTC 固定 (:19 / :30 祝日ラベル +3h / :33 / :37 / :44-45 / :52-53) と plugin/canonical_fixture.py:30-38 で、冬の間は週の開場・閉場、取引日ラベル、祝日判定、horizon=day の強制決済、日次損失の区切り、日次 report が 1 時間ずれる。呼び出し元は scheduler / trade_loop / missions / accounting / signal_producer / ingest / outage / health / backtest など (報告 tmp/review-notick/dst-probe.md)。日曜 21:00-22:00 を開場中とみなし欠損扱い → 15m 取得失敗で hold 多発 (datafeed-no-tick-minutes-counted-as-gap の主因)。処置: 境界を America/New_York 17:00 として 1 関数で UTC に換算、全呼び出しをそれに寄せる。backtest の過去データ (取り込み時に固定 +3 換算) の扱いも設計で決める。期限: 次の金曜閉場 2026-11-06 まで

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-04: f3591e48 (R1: NY 17:00 tz 規則、稼働中の切替 T_c 2026-11-03T07:56:52Z)。11-03 は 21:00Z で日次が切り替わらず 22:00Z で切り替わり、過渡期間は 22:00Z で終了を DB で確認

## 経緯

- 2026-11-02: 起票。
- 2026-11-03: 状態: 実装中 — 実装済 (本番切替の配備待ち)。是正: 週の開場・閉場、取引日ラベル、祝日、day 強制決済、日次基準を NY 17:00 (America/New_York) の tz 規則 1 箇所から UTC に換算し、内部は UTC のまま。稼働中の切替は手順書 docs/operations/time-rule-cutover.md。設計 docs/superpowers/specs/2026-11-02-utc-time-rules-dst-design.md。close は本番切替の後
- 2026-11-04: 是正内容 — f3591e48 (R1: NY 17:00 tz 規則、稼働中の切替 T_c 2026-11-03T07:56:52Z)。11-03 は 21:00Z で日次が切り替わらず 22:00Z で切り替わり、過渡期間は 22:00Z で終了を DB で確認
