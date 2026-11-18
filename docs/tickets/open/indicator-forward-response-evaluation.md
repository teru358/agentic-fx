---
id: indicator-forward-response-evaluation
title: 指標の計算値を数学的な計算にかけ、通貨ペアの傾向を改善 agent に見せる分析の道具
status: 裁定待ち
priority: 中
kind: 機能
opened: 2026-11-18
closed: null
related: [multi-input-indicator-relations, strategy-attribution-and-shadow-evaluation, weekly-plugin-inventory-notice, improve-context-family-attempt-summary, reference-symbols-as-indicator-inputs, web-ui-charts-equity-indicators, multi-timeframe-indicator-deps, improve-objective-filtered-signal-quality]
---
# [indicator-forward-response-evaluation] 指標とその変換が将来の値動きとどう関係するかを測り、改善 agent に返す

**状態**: 裁定待ち / **優先**: 中 / **種別**: 機能

## 現象

改善ループは「仮説 → 戦略 → backtest の売買成績」で 1 回に 1 仮説しか確かめられず、探索の効率が悪い。指標は検証なしで作れるため、使い道の無い指標が増える。指標そのものを評価して「どの方向が有望か」を返す手段が無い (10-24 の裁定で、改善ループに欠けていると確認した情報)。

要望 (2026-11-18 利用者): 指標を backtest 期間にかけて値を返し、期間内の返し値の比較・差・微分・積分・しきい値越えなどで新しい検証をしたい。主な利用者は改善 agent (戦略改善の機能として)。人間向けの表示は確認用にあってよい。

方針の案 (設計前、指揮者):
- 測るのは「時点 t の指標 (と変換) と、t から h 本先の値動きの関係」。分位ごとの h 本先の平均値動きと勝率、しきい値越え直後の値動き (平均・最大順行・最大逆行・件数)、順位相関の月ごとの安定性。
- 変換は任意コードにせず、決まった演算の組合せ (差・変化率・区間の累積・標準化・しきい値越え・2 系列の交差)。再現と試行数の計数のため。
- 偶然の当たりへの対策: 評価期間を探索用と確認用に固定で分ける、試行数を記録して合格の基準に反映する、確認用の結果を agent に見せない (既存の遮断の規律と同じ)。
- 履歴は USDJPY 5m が約 18 か月、参照 5 銘柄が 2025-07 から。

順序: web の改修の後に設計 (C0 → レビュー → 承認)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-18: 起票。
- 2026-11-18: 題名: 指標の計算値の系列を加工し、将来の値動きで評価して改善 agent に返す — 利用者の意図の補足 (11-18): チャートに描かれる EMA 等の計算値そのものを、backtest 期間の時系列として扱い、差・微分・積分・比較などの処理をかけたい。仕組みは 2 層: (1) 指標の値の系列を作り、決まった演算で加工する層 (要望の中心)、(2) 加工した系列を、その後の値動きと照らして良し悪しを測る層。系列そのもの (5m 18 か月で約 11 万点) は agent に渡さず、agent は演算の組合せを指定し、計算はコード側で行って要約だけを返す。確認用の期間の系列・結果は agent に見せない
- 2026-11-18: 関連: [multi-input-indicator-relations, strategy-attribution-and-shadow-evaluation, weekly-plugin-inventory-notice, improve-context-family-attempt-summary, reference-symbols-as-indicator-inputs, web-ui-charts-equity-indicators, multi-timeframe-indicator-deps] — 方針案のレビュー (11-18、fable と sol、どちらも「このままでは設計に進まない」)。共通: (1) 系列は本番と同じ tail(max_bars) の意味で作る (全期間の一括計算は先読み・初期値で値が変わり、欠陥のある指標を高く評価する)。(2) 固定の確認用期間は、実行時点から 3 か月の動く holdout 境界と両立しない。in-sample の内側に暦日で凍結し、agent に見せる範囲と次周に残すもの (成績の順序の持越し) を新しい裁定として決める。「既存の遮断と同じ」は誤り。(3) 評価の合格は戦略作成の関門にせず根拠の記録にする。床・承認・run_backtest は変えない。(4) 第 1 版は配備済み指標・同じ銘柄・1h 判断・列挙した見方・区分の応答。式の言語・積分・多入力は外す。(5) 新しい層を作らず、既存の analyze_for_agent に kind を 1 つ足す。fable のみ: 本番 DB の集計で、10-22 以降 in-sample 評価 515 組の 30% が床を通る一方、holdout 73 組の 81% が 3 か月で 30 取引に届かず評価不能。候補が落ちる主因は holdout の標本不足で、探索効率ではない。概算では 1h 単一指標の効果は spread と同じ桁。作る前に人間側で配備済み 50 指標 × 固定の見方を一括で 1 回だけ試算 (費用控除・多重比較補正後に残る組を数える) を第 0 段にする。sol のみ: 買いは ask→bid・売りは bid→ask で費用、ブロック再標本化、累積の試行予算を mission で戻さない、事前登録と凍結。報告: tmp/review-indeval/draft-r1-{fable,sol}.md (非公開)。

指揮者が追加した考慮事項: (a) 目的に直結するのは「配備済み戦略のシグナル発火時に、指標の状態が勝ち負けを分けるか」(10-07 の目標、signal_shadow が材料)。(b) 方向以外の使い道 (取引回避の条件・SL/TP の幅・決済の時機) は h 本先の値動きで測れない。(c) 取引判断 LLM に見せる指標の選別にも使える。(d) 取引できない時間 (経済指標の回避帯・週末の閉場前後・kill switch 中) を評価から除く。日をまたぐ h ではスワップ、paper は slippage を模していない。(e) plugin のコードは本番と同じ隔離 worker で動かす。50 指標 × 約 8,000 判断点の再計算に cache と、取引 tick を止めない実行枠が要る。(f) 指標の版・params・データの版 (取込み直し・衝突の解消・時刻規則の訂正) が変わったら結果を無効にする。(g) 似た指標 (EMA の期間違いなど) は既存の重複 gate の類似度で群にし、試行数も群で数える。(h) 別の足の指標の整列規則、相場の局面別の安定性 (局面で分ければ試行数も増える)。(i) 人間の確認は web のチャートに系列・しきい値越えを重ねて描く (web-ui-charts-equity-indicators)。(j) 成否の指標と、やめる基準を先に決める (例: 第 0 段で残る組が 0 なら人間向けの棚卸し報告に縮める)
- 2026-11-18: 状態: 裁定待ち — 考慮事項ごとの対処案 (11-18、fable と sol、報告 tmp/review-indeval/considerations-r1-{fable,sol}.md 非公開)。一致: 設計の前に第 0 段 (人間側で 1 回の一括試算、配備済み指標 × 列挙した見方 × h、USDJPY 1h、隔離 worker・閉場中・DB 読取専用、費用は ask/bid と spread 欠損を捨てる、slippage は感度) を置き、合否の基準を結果を見る前に凍結する。第 1 版は既存 analyze_for_agent の新しい kind で、区分の応答・向きの宣言・累積の試行台帳 (mission で戻さない、鍵は指標の hash・params・見方・h・データの版)。次周には試行済みの印だけを渡す。評価は関門にせず承認画面の根拠に出す。signal_shadow の診断は人間向けの後続 (50 行・9 日で標本が足りない)。相違: 確認用の期間 (sol は第 1 版で新設しない、fable は in-sample の内側を暦日で凍結し第 0 段の集団の符号の再現と人間向けの表示だけに使う)、多重比較 (sol は週ブロック再標本化 + Holm、fable は値動きを日単位でずらした帰無分布と残存数の比較)。fable が実測で見つけた事実: in-sample の区間に経済指標の履歴が無い (econ_events は 2026-08-23 から)、backtest の replay は経済指標の回避帯を適用しない、スワップはどこでも計算していない (paper の orders も全件 0)
- 2026-11-18: 関連: [multi-input-indicator-relations, strategy-attribution-and-shadow-evaluation, weekly-plugin-inventory-notice, improve-context-family-attempt-summary, reference-symbols-as-indicator-inputs, web-ui-charts-equity-indicators, multi-timeframe-indicator-deps, improve-objective-filtered-signal-quality] — 利用者の方針 (11-18): テストは最低限の関門で、テストで利益を出せないものは本番で厳しい (必要条件として扱う)。経済指標の回避帯は運用者の選択で許可もできるので、backtest が回避帯を適用しないことは欠陥として扱わない。スワップは考慮しない。spread は backtest と同じく履歴の spread で残す (外すとテストが最低線として働かない)。slippage など、それ以上の費用の作り込みはしない
- 2026-11-18: 題名: 指標の計算値を数学的な計算にかけ、通貨ペアの傾向を改善 agent に見せる分析の道具 — 利用者が目的を明言 (11-18): 「目的は戦略改善でできることを増やす。指標をチャートに通したときに返す値をモデルに渡して通貨ペアの傾向を見える化する。返す値を数学的計算に投げられるようにする」。中心は当たりを判定する仕組みではなく、改善 agent が使う分析の道具。将来の値動きとの関係の評価は機能の 1 つ。指揮者の確認中: (1) 「モデル」は改善 agent か (取引判断 LLM にも渡すか)、(2) 第 0 段の一括試算を前提から外し、道具として設計に進むか。道具でも残る制約: 本番と同じ tail(max_bars) の意味で系列を作る、agent には要約を返す (計算はコード側)、見せるのは in-sample だけで holdout は隠し試行を記録する
