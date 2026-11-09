# 改善の backtest の除外を agent に読める形にする (improve-backtest-untradable-pair-and-skip-readability の B・C) 設計 C1

チケット: `docs/tickets/open/improve-backtest-untradable-pair-and-skip-readability.md`。部分 A (取引できない pair の入口拒否・gate の契約違反・prompt の取引 pair) は main に merge 済み (d610c035)。本書は残りの B (応答の読み方) と C (評価区間の始点) を扱う。

遮断の番号は原設計 `2026-07-25-agentic-fx-design.md` §6 の表を正とする: #1 = backtest の期間指定・端点を返さない (:447)、#7 = 履歴分析と :555 の出力契約 (観測数・件数で境界を推測させない)、#8 = holdout の派生情報 (:454)。コードの一部 (`improve_loop.py:242`、`improve_rpc_tools.py:69,98`) が期間・端点の非公開を「遮断7」と呼ぶのは旧表記。

## 0. 何が起きているか (事実)

- `run_backtest` の応答は `scope`・`pair`・`timeframe`・`source`・`metrics` (`eval_bars` を除く)・`trial_count`・`started` と、条件つきの `submission_blocked`・`skipped_bars_by_reason` (`src/agentic_fx/loops/improve_loop.py:237-270`)。
- `eval_bars` は意図して落としている。in-sample の長さを足数から推定させないため (`2026-09-12-profitability-floor-design.md:627`)。公開禁止の固定は `tests/loops/test_improve_loop_rpc_handlers.py:1152`。
- `skipped_bars_by_reason` の語は backtest では 4 語 (`plugin/strategy_adapter.py:184-234`、定義 `plugin/reference_frames.py:20-25`)。語の意味を agent に伝える文言は prompt にも応答にも無い (`improve_mission.md` に `skipped` の記述 0 件)。各語の判定条件:
  - `missing`: (a) 参照 symbol が `readable_symbols(settings)` に無い (:215)、(b) 判断時刻までに閉じた参照足を選べない (:220-227、選び方は `plugin/reference.py:138-146` の「最新の利用可能な確定足」)。
  - `age_exceeded`: 選んだ参照足が `max_age` より古い (:229)。原因の例は参照市場の休場・早期クローズ・欠損。
  - `indicator_unavailable`: worker が `indicator_unavailable` または `reference_unavailable` を返した (:184-188、`plugin/sandbox.py:688`)。原因の例は履歴先頭の warmup。strategy の呼出し後の失敗も含む。
  - `reference_window_exceeded_at_runtime`: 参照の窓が実行時の上限を超えた (:232-234)。
- 評価区間の始点は主 pair の最古足だけで決まる (`backtest/holdout.py:197`)。基準線の再生も同じ区間 (`plugin/strategy_gate.py:749-759`)。頻度の床の分母は adapter の skip より前に数える `eval_bars` (`backtest/runner.py:474-480`、`strategy_gate.py:154`)。
- 実例 (`tmp/inv-refgap/report.md`): USDJPY 主軸 + US500 参照で、先頭 952 足 (12.2%) が `missing`、休場の `age_exceeded` 約 78 足、warmup 1 足。agent は 0 取引の原因を参照に誤帰属した (実際の原因は部分 A で塞いだ)。

## 1. 何を作るか (動作)

### B1: 除外の語の意味を prompt に書く (この束で実装)

- `improve_mission.md` の `run_backtest` の説明に、`skipped_bars_by_reason` の 4 語を §0 の判定条件で説明する固定文言を足す。原因は例として書き、順位づけはしない。
- 共通の一文: 「除外された判断からは取引の提案を採用しません。0 取引や取引が少ないときは、除外・提案が出ていない・発注の前提を満たさない・未決済を区別して調べてください。」
- 応答の形は変えない (文言は静的で、期間の情報を含まない)。

### B2: 却下の内訳を応答に足す — 見送る

- 却下件数は、毎回必ず却下される open を返す候補で評価回数に近いカウンタになり、日付条件を変えた反復で境界の探索にも使える (遮断 #1・#7 の趣旨、`eval_bars` 非公開の裁定に反する)。件数を有無に減らしても、日付条件つきの候補による存在照会は残る。
- 部分 A で 0 取引の主因 (取引できない pair) は入口で止まるようになった。また backtest で実際に出る category は `risk_gate`・`execution` だけで (`origin`・`mission`・`econ_window` は通常の backtest の経路では出ず、`signal` は書込み箇所が無い)、内訳の情報量も小さい。
- 却下の内訳が要るなら、agent に見せない親側の診断 (運用ログ・要約) として別に設計する。

### B3: 足数・除外の割合は出さない

- 当初案「評価した足の数・除外の割合」は採らない。割合と既存の件数から `eval_bars` が逆算できる。

### C: 評価区間の始点 — 別チケットに分ける

- 始点を宣言した参照の最古足に合わせる案は、評価期間の契約の変更になる: 候補が参照の選び方で間接的に始点を選べる (遮断 #1 の期間所有)、頻度の床の分母 `eval_bars` が減って合否が変わる、基準線が自分の参照でさらに遅く始まれば端点が一致しても判断の機会は揃わない、4h・1d の参照は完全な bucket と確定足の規則で raw の最古足より後にしか使えない、`literal_reference_coordinates` は source・base_interval を持たない、履歴の追加・遡及取込で最古足が動き再評価で同じ始点が出ない。
- 候補ごとに後から切り詰めるのでなく、ハーネスが事前に固定する共通の評価区間 (raw 履歴の区間・判断できる区間・頻度の分母・候補と基準線の比較区間を分けて定める) として、新チケット `improve-eval-window-reference-availability` で設計する。

## 2. 残余

- 公開済みの `metrics.trades` と `skipped_bars_by_reason` の件数も、意図して作った候補 (毎足で建てて閉じる・参照を常に欠かす) で評価回数の下限の計測に使える。取引数は評価に不可欠なので外さない。本書では新しい件数を足さないことで、この面を広げない。

## 3. 受入

1. prompt に 4 語の判定条件 (`missing` の 2 条件、`indicator_unavailable` の 2 語の集約と呼出し後の失敗) と共通の一文がある。原因の順位づけの文が無い。
2. rendered prompt に日時が無い (既存の test のまま)。応答の形は変わらない (`test_improve_loop_rpc_handlers.py:299-354` の期待値は無変更)。
3. prompt の 4 語が `REFERENCE_REASONS` と `RUNTIME_WINDOW_REASON` のうち backtest の adapter が出す語の集合と一致することを test で固定する (語が増えたら test が落ちる)。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-09 | C1 | B1 を判定条件で書き直し原因の順位を削る、B2 を見送る (件数が評価回数のカウンタになる)、C を別チケットに分ける、遮断の番号を原設計の表に合わせる、§0 の引用を直す、残余を足す | sol 1 周目 (Critical 1 / Important 5 / Minor 1) | — |
| 2026-11-09 | C0 | 初版 | 部分 A の後の B・C の設計 | — |
