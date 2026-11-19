# 週次の稼働中戦略の評価と棚卸し — 設計 C0.1

チケット: `weekly-improve-rebacktests-deployed-strategies`、`strategy-attribution-and-shadow-evaluation`、`weekly-plugin-inventory-notice` (3 件を束ね、3 段で実装する)。
関連: `improve-daily-params-weekly-algorithm`、`per-strategy-virtual-books`、`plugin-naming-lifecycle-policy`。

パスの略記: `S/` = `src/agentic_fx/`、`A/` = `afx-ops/afx_ops/`、`W/` = `tmp/wt/webcharts/src/agentic_fx/` (branch `webcharts`、未統合)。DB の件数は 2026-11-19 に本番 DB を読取専用で確認した値。

## 0. 何が起きているか

- 配備後の動きを戦略ごとに見る面が無い。signal の採否と機械採点は DB にあるが、戦略別に集計して出す経路が無い。
- 配備中の指標は増え、使われないものが残る。web で 1 本ずつ参照元を読めるが、一覧として拾えない。
- 戦略を止める判断は人間がするが、その判断を促す定期の知らせが無い。

### 0.1 今のコードで確かめた前提

| 前提 | コード・DB | 帰結 |
|---|---|---|
| 帰属 | `signal_decisions(signal_id UNIQUE, trade_intent_id)` が signal の plugin・hash と建玉を結ぶ。trigger は `signal:<plugin>` (`S/store/db.py:175`、`S/store/signal_decisions.py:92-112`、`S/ops/trade_events.py:184-188`) | 帰属の列は足さない |
| 機械採点 | decision 行から shadow を作る (`S/store/signal_shadow.py:121-137`)。accept 51・reject 6、shadow 57 件。採否前に abandoned となる signal は含まれない。配備中の 1 本では 125 件中 55 件が abandoned | 全 signal の成績とは呼ばず、穴と採否到達率を併記する |
| 採点規則 | 観測開始は採否時刻以後の UTC 分境界 (`S/store/signal_shadow.py:99-108`)。entry は成行の最初の 1 分足 open か指値到達、exit は SL / TP / day close。spread は quote か仮定値 (`S/store/signal_shadow.py:80-96`、`S/core/paper_fills.py:201-245`、設計 2026-11-01 §3.4) | 規則は変えない。戦略の exit_mode は levels のみ (`S/plugin/loader.py:291-293`) |
| 人間の表示 | 実取引の戦略行はチャートの変更が提供する (`W/ops/performance_view.py:867`)。日次 report の shadow は採否種別 × 状態 (`S/loops/econ_signal_report.py:325-334,552-585`) | 実取引を作り直さず、機械採点を並べる |
| 再評価 | `afx plugin reevaluate` と同名候補との同条件の再生がある (`S/cli_surface.py:315`、`S/plugin/strategy_gate.py:86-89,226`) | 最終判断は既存の操作を使う |
| 主源 | 実取引は戦略だけでなく採否・枠・裁量決済を含む。機械採点は採否前の欠測と費用の偏りを持つ | 主源は §6 問 1 で選ぶ |
| 前方 backtest | 期間の入口は run_in_sample / run_holdout_gate のみ (`S/backtest/holdout.py:180,216`) | 任意の前方区間の再生は別起票 |
| 改善入力 | 既に 90 日の実取引の勝率・PF・ペア別損益と件数を渡す (`S/loops/improve_context.py:146-179`、`S/loops/prompts/improve_mission.md:10-18`) | 「shadow を足さない」と「前方情報を全面遮断」は別。既存入力は §6 問 5 |
| 起動 | mission は手動・繰り返しでも走り (`S/loops/improve_loop.py:906`)、cadence は weekly / daily (`S/config.py:460`) | 週次確定を mission と切り離す |
| 棚卸し | 配備中の指標 94 本、戦略 2 本、参照する指標 3 本 (`plugins/` の symlink と plugin の config) | 参照なしだけで不要としない |
| 操作の置き場 | disable / retire は最上位 `afx plugin` (`S/cli_surface.py:307-310`) | 保存結果の読取も同じ場所に置く |
| 通知 | 開始通知は mission ごとで、event は固定語と整数の投影 (`A/gate/improve_render.py:691`、`S/ops/events.py:54,85`) | 週次は専用 event |
| データ | shadow は live の 1 分足を使い、評価履歴の同期とは別 (設計 2026-11-01 §3.5-2) | 履歴の鮮度の守りは変えない |

## 1. 何が変わるか (動作)

### 1.1 利用者が受け取るもの

1. **いつでも (web・CLI)**: plugin 一覧で未使用の指標と条件が読め、絞れる。戦略別画面では、実取引の隣に「採否できた signal の、採否時点からの仮想成績」(以下、機械採点)、採否、穴の件数が並ぶ。「実約定を含まず楽観側に出ます」「採否前に abandoned となった signal は含みません」を固定表示する。
2. **週に 1 回**: 取引週の終わりから通常の確定猶予を経て、親専有の週次 report ができる。停止候補か新しい未使用の指標があれば Discord に 1 通届く。最初の数週の「標本不足」と件数は report に出るが、通知は保証しない。
3. **人間の判断**: 戦略の停止は `afx plugin disable <name>`、履歴での確認は `afx plugin reevaluate <name>`。指標は disable / retire。自動停止・削除はせず、提案を放置しても取引は変わらない。

| 判定 | 意味 |
|---|---|
| 標本不足 | 現配備世代で採点 30 件未満、または最初の採否から 28 日未満 |
| 要確認 | ゼロ近傍、未終端の滞留、採否到達率の悪化などがあり、符号を判断材料にできない |
| 機械採点上は床超え | 最低条件を満たし、欠測の問題が無く、平均 R がゼロ近傍を超えて正。実運用の収益性は保証しない |
| 停止候補 | 最低条件を満たし、平均 R がゼロ近傍より負。調査・停止検討の材料 |

### 1.2 改善 agent が受け取るもの

週次の数値も判定語も渡さない。停止済みの同名 plugin の自動再配備を避けるため、名前と kind だけの一覧を独立に加える (§2.8)。仮説そのものの禁止には使わない。

### 1.3 変わらないもの

発注・SL・決済・資金保護、採否の契約、shadow の採点規則、採用 gate と床、承認、disable / retire / reevaluate の動作。外向きの送信先も増やさない。

## 2. 仕組み

### 2.1 段の分け方と依存

| 段 | 内容 | 依存 | 単独の価値 |
|---|---|---|---|
| 1 | 指標の棚卸し | なし | 未使用の指標を一覧で拾える |
| 2 | 戦略別の機械採点 | 集計は依存なし。web 表示だけチャートの変更の統合後 | 仮想成績と欠測を読める |
| 3 | 週次確定・判定・停止提案 | 段 1・2 の集計 | 見に行かなくても知らせが来る |

棚卸しは成績を要らず先に出せる。3 件を束ねる理由は起動・保存・通知・人間の操作が共通だからである。停止名の掲示は週次判定に依存しない独立の変更とする。

### 2.2 指標の棚卸し (段 1)

live の指標で、次のすべてを満たすものを未使用とする。

1. 配備中の戦略の config から参照されない (既存 dependents が空。`S/plugin/read_model.py:170,405-409`)。
2. 保留中の承認候補から参照されない (`S/plugin/deploy_deps.py:28`)。
3. 直近 K 週に作られた**戦略候補の依存 plugin として登場しない**。`candidate_archives.family_key, created_at` (`S/store/db.py:635-646`) を既存 `parse_family_key` (`S/plugin/logic_identity.py:329`) で構造的に読み、legacy / v2 の strategy の依存名だけを調べる。指標自身の再提出・部分文字列の一致は利用に数えない。
4. 現配備世代の配備から K 週以上たっている。

K の推奨は 8 (§6 問 3)。対象期間に NULL・不正 key があり依存を確かめられなければ「判定不能」とし、未使用とは確定しない。改善休止も暦の K 週に含むが、候補生成 0 件と明示する。配備時刻不明も判定不能。時刻は完了済みの switch / rollback の記録から取り、時刻なしの既存 journal 入力 (`S/plugin/read_model.py:117-123`) に投影を足す (`S/store/db.py:508-520,535-552`)。

現版に対応する承認済み依頼の `candidate_origin=human` なら印を付け、停止提案から除外する。名前全体を永久に除外しない。現版の出所不明も提案から除外する。

read model の行に `inventory` (未使用 / 判定不能、満たした条件) を加える (`S/plugin/read_model.py:573`)。web は列と絞り込み (`A/web/plugin_pages.py:185-196`)、CLI は `afx plugin list --unused`。段 1 は読む時点の集計で保存を持たない。並べ替えと最終登場月の表示は後回しにする。

この read model は操作 API と plugin CLI が読む (`S/ops/plugins_view.py:58-59`、`S/backtest/plugin_deploy_cli.py:113`)。改善は別の一覧 (`S/loops/improve_loop.py:985`) を使い、inventory を渡さない。

### 2.3 戦略別の前方成績 (段 2)

**対象と世代。** 今 live の `(name, content_hash, 配備世代)`。世代は既存 `deploy_generation` と同じ区別を使う (`S/plugin/switch.py:2000-2007`)。版変更・A→B→A の rollback とも現世代の配備時刻から再開始し、同 hash の過去採点は使わない。signal の hash と発生時刻で世代を限定する。配備時刻が不明なら判定不能とする。

**窓。** 読取時点から 12 週前まで、現世代の開始以後に限る。採否時刻 `signal_decisions.decided_at` で窓に入れる。週次は読取時点の代わりに週末時刻を使う。実取引の名前単位の行とは集計範囲が違うので世代開始を注記する。

| 系列 (合算しない) | 内容 | 単位 |
|---|---|---|
| 機械採点・全採否 | 現世代の shadow の数値終端 sl / tp / day_close。accept と reject の両方。採否できた signal の、採否時点からの仮想成績 | R |
| 機械採点・採否別 | 同じ母集団を採否で分ける。accept と reject の平均 R の差も表示だけ | R |
| 実取引 | チャートの戦略行 (`W/ops/performance_view.py:867`) | 口座通貨・R |

**R。** `pnl_pips ÷ (|entry_price − stop_loss| ÷ pip_size)`。SL は signal の payload、他は shadow 行。分母が 0 以下・非有限・SL 欠落は R 不明として率から除外する。lot を持たない採点を pips で合算すると広い SL が重くなるため R にする。backtest の金額による avg_r (`S/backtest/metrics.py:89-97`) と無条件には一致しない。

**表示。** N (R 既知)、勝 / 負 / 分、平均 R、PF (R の利益合計 ÷ 損失合計)。損失が無ければ PF は計算不能として表示する。判定は平均 R だけを使う。母数 5 未満は「–」、5〜29 は参考、30 以上は通常表示。既存の 30 件 (`S/backtest/metrics.py:20,142`) は表示上の床で、有意差・独立性の根拠ではない。30 件・28 日は運用上の最低条件に留める。

率の隣に abandoned (理由別。`S/store/signals.py:477`)、no_fill、data_insufficient、未終端、R 不明を出す。採否別件数、accept のうち risk gate 通過数、signal に対する採否到達率、未終端の最古の経過時間も出す。採点のある取引日数、日別最大件数の割合、ペア・売買方向・時間帯別件数を併記し、同じ日・同じ局面への集中を人間が確かめられるようにする。

**経路。** 純関数と読取専用接続で集計する。pips だけを返す `period_summary` (`S/store/signal_shadow.py:316`、src の呼出し元なし) を置き換える。web の列を足し、親専有の日次 report の shadow 節も戦略別にする (`S/loops/econ_signal_report.py:552`)。

### 2.4 週次の確定と判定 (段 3)

**境界と確定時刻。** 週末は次の開場まで 24 時間以上空く close、週の印はその close の NY の取引日。窓の終端と確定実行時刻は別にする。確定は shadow の通常の 1 日の猶予 (`S/store/ohlcv_slices.py:47`、`S/core/econ_slice_worker.py:774-796`) 後。遅延実行でも窓は週末に固定し、翌週の採否を入れない。残る未終端と経過時間は確定に残す。web の現在集計と週次固定値の時刻を分けて表示する。

**起動。** 日次 report hook (`S/core/scheduler.py:450`、登録 `S/service.py:1939`) は callback 1 本 (`S/core/scheduler.py:468`) なので、複数 callback の記録を分離する。日次は通常どおり呼び、週次は猶予到来を tick で確認して専用 thread に積む。猶予前に完了扱いにしない。失敗は既存同様に次の tick、同日最大 3 回 (`S/core/scheduler.py:453-466`)。翌日と起動時にも未完了を確認する。新規作成は直近の対象週だけとし、作成中の週と確定済み通知の回収は捨てない。mission の有無・成否・回数に依らず、発注 tick を塞がない。

**保存。** 既存表は変えず、週単位の記録と集計 2 表を追加する。

- 週単位の記録: 週を一意とし、窓終端・評価時刻・対象の名前、hash、配備世代・規則版を開始時に固定する。対象は週末時点の配備記録から復元する。再試行で対象・時刻を入れ替えない。評価時刻までに終端した shadow だけを採点する。穴・到達率・棚卸しの読取入力も開始時の同じ snapshot から必要列だけ固定保存し、再試行時に現在の状態へ差し替えない。
- `strategy_review_weekly`: 週・名前・hash・世代で一意。最初の採否、N、勝敗、R の利益・損失合計、穴・採否・集中度、表示用短期集計、判定と理由。金額は保存しない。実取引の列は読取時の集計と注記する。
- `plugin_inventory_weekly`: 週・名前、未使用 / 判定不能、現版の出所、満たした条件。新しい未使用は直前の正常な確定との差とし、比較元なしなら初回一覧を新規とする。

両集計と完了記録を **1 transaction** で確定する。片方の例外は両方 rollback し、完了にしない。正常な空集合は完了・0 件。scan 失敗は完了・棚卸し判定不能として区別し、新しい未使用は通知しない。判定不能の週は次週の比較元にしない。

**判定の方針案 (§6 問 2)。**

1. 主窓は週末までの 12 週。4 週の短期窓は report の件数・平均 R の表示だけに使い、自動判定に使わない。
2. 通常猶予を超えた未終端が残る、または採否到達率が悪化なら `要確認`。到達率は同じ発生期間の signal に対する decision ありの割合。直近 4 週が前の 4 週より低い、比較期間が空、直近の signal が無い場合も要確認。古い解決済み 30 件が正でも肯定側に進めない。
3. N < 30、または現世代の最初の採否から 28 日未満は `標本不足`。
4. ゼロ近傍は `要確認`。推奨幅は平均 R の絶対値 0.05 以下。統計的な保証を表す幅ではない。
5. 幅より負なら `停止候補`。非正は調査・停止検討の材料とし、ゼロ近傍は先に要確認とする。採用の床 (`config/settings.yaml.example:117-118`) と近い線でも、採用の正当性の逆判定には使わない。
6. 幅より正なら `機械採点上は床超え`。実運用の収益性や別機能の着手条件の証明には使わない。

前週の判定語を読まない。重なる週の窓を独立した確認とは扱わない。承認時の holdout は report に並べるだけで判定に使わない。

**report と操作。** `data/improve_reports/` の専用 dir に既存どおり owner-only で書く (`S/loops/econ_signal_report.py:779`)。判定、3 系列、穴・到達率・集中度、承認時の成績、未使用と停止済みの一覧を載せる。書込み失敗は同じ確定から再生成する。提案の採用・却下ボタンは作らず、停止は既存操作。止めれば次の対象から外れる。

### 2.5 通知 (段 3)

- event は `weekly_review_ready`。週は実在日を検証した `ref=weekly:YYYY-MM-DD` に置く。fields は固定語と非負整数 (停止候補・未使用・新規未使用の本数、棚卸し状態) のみ。名前・成績の数値は入れない。専用の文法検査を置く (`S/ops/events.py:54,85`)。
- bot は必ず `GET /v1/review/weekly?week=YYYY-MM-DD` で指定週を読む。応答の週が違う・取得不能なら見出しと本数だけに縮退する。詳細は保持しない。
- 起動時と tick で確定済み・event 未発行を回収する。既存の未通知回収 (`S/ops/events.py:199-211`) 同様、code・ref を確認する。event INSERT と週の発行済み記録を同一 transaction にする。既存 emit は内部で commit する (`S/ops/events.py:150-159`) ので、そのまま使わず専用保存を加える。
- bot は既存の event 配送・cursor 保存・送信 retry 経路 (`A/bot/adapter.py:871-892,971-975`、`A/gate/service.py:153-167`) に接続する。送信失敗では cursor を進めない。成功直後・cursor 保存前の停止による重複は許容する。初回接続は既存どおり末尾からで、過去週は web・CLI・report で読む (`A/gate/state.py:149-172`)。
- 本文の成績は **pf / avg_r / 件数 / 評価不能** の 4 項目だけ。勝率・DD・金額・閾値は載せず、機械採点と明記する。既存通知の本文 2000 文字上限、escape、mention 無効、低優先段落からの省略を継承する (`A/gate/render.py:41,77,88,234`、`A/gate/improve_render.py:4-6`)。
- 停止候補 0・新規未使用 0 なら出さず、週の記録を通知不要とする (§6 問 6)。初期の件数表示は report の話である。
- on / off は既存 `ops_notices` (`A/config.py:81`)。設定・送信先を増やさず、webhook にも固定語と本数だけを渡す。

本文例 (架空):

```text
[週次] 停止の提案 2026-11-21
戦略 (機械採点、直近 12 週)
  <name>: pf 0.82 avg_r -0.11 採点 34 件 — 停止候補
未使用の指標 14 本 (新規 2: <name>, <name>)
確認: afx plugin review / web の plugin 一覧
```

### 2.6 CLI・API・web

| 面 | 追加 |
|---|---|
| CLI (daemon 不要、読取) | `afx plugin list --unused`、`afx plugin review [--week <日付>]` |
| API (status.read) | `GET /v1/review/weekly` (直近と指定週)、plugins DTO の inventory、戦略別の機械採点 |
| web | 未使用の列と絞り込み、戦略別の列と穴、週次確定の表 |
| bot | 専用 event の指定週の本文 |

help は `cli_surface.py` の記録から生成する。web からの停止操作は作らない。現在の判定欄は現配備の name・hash・世代に一致する確定だけから取り、不一致は `-`。過去週を明示指定した画面は当時の対象と表示する。

### 2.7 誰に何を見せるか

| 情報 | 人間 | Discord | event・webhook | 改善 agent・他の agent |
|---|---|---|---|---|
| 機械採点 | 全指標 | 4 項目だけ | 出さない | 出さない |
| 採否・穴・到達率・集中度 | 出す | 出さない | 出さない | 出さない |
| 実取引の戦略別成績 | 出す | 出さない | 出さない | 出さない |
| 週次判定 | 出す | 停止候補だけ | 本数だけ | 出さない |
| 未使用の指標 | 名前・条件 | 本数・新規の名前 | 本数・状態だけ | 出さない |
| 停止済みの名前 | 出す | 出さない | 既存 plugin_disabled のまま | 改善だけ名前・kind |
| 止めた理由・経路、承認・却下の reason | 既存の面 | 出さない | 新規には出さない | 出さない |
| 承認時の成績 | report | 出さない | 出さない | 出さない |

週次表・親 report・判定語を agent の入力に結ばず、worker に DB と report の path を渡さない。reflection・RAG・取引 prompt にも足さない。機械採点自体も LLM の reasoning を読まない。

既存の実取引全体集計は別問題である。§6 問 5 を先に裁定し、現状を残すなら前方情報の明示的な例外として記録する。「shadow を追加しない」だけで前方情報全般の遮断を宣言しない。将来の方向語の検討も、その裁定後に行う。

### 2.8 停止済みの名前の掲示 (独立)

成功した live scan と完了済みの配備記録を照合し、過去に配備済みで今 live ではない名前だけを出す。scan・journal の読取失敗、未完了の switch / deploy 操作、版不明を停止と誤認しない (`S/plugin/read_model.py:86-134`、`S/store/db.py:518-527,549`)。

kind は検証済み保存版の config から取得し、親側で `{name, kind}` だけに投影する。journal の kind は approve / bless であり plugin の kind ではない (`S/store/db.py:510`)。actor・reason・成績は context に渡さない。既存の名前文法 (`S/plugin/logic_identity.py:206`) を通し、昇順で 50 件・4 KiB、超過は固定文で省略する。

固定文は「停止済みの同名 plugin を自動で再配備しないでください」。同じ仮説の別名候補までは禁じない。人間の rollback で live に戻れば一覧から外れる。週次確定を待たずに実装できる。

## 3. 失敗時・運用

| 事象 | 動き |
|---|---|
| 集計例外・DB busy | 集計と完了を rollback。固定した対象・評価時刻で次の tick に再試行、同日最大 3 回。翌日・起動時にも未完了を確認 |
| report 書込み失敗 | 確定から再生成。通知は指定週の API を読む |
| 確定後・event 前に停止 | 未発行の確定を回収 |
| bot の詳細取得失敗 | 指定週の見出し・本数に縮退。送信失敗なら retry |
| 確定が古い | web・CLI は日付と注意を出す。現配備と一致しなければ判定は `-` |
| worker 滞留・到達率悪化 | 古い採点が正でも要確認。穴と経過時間を出す |
| 版変更・rollback | 世代ごとに再開始。以前の確定は保存し、現在の判定に流用しない |
| 週の途中で停止 | 週末の対象から外す。既に対象を固定した週は書き換えない |
| scan 失敗 | 棚卸し判定不能。正常な空集合と区別 |
| 実取引の集計失敗 | 実取引の列だけ読取不能。機械採点と混ぜない |
| 新しい表が無い | 未確定を返す。表は既存の追加の慣行で作る |

## 4. 検証と受入

### 追加する検証

1. **棚卸し**: 条件の組合せ、現版の人間由来・出所不明、保留候補、legacy / v2 の戦略依存、指標自身の再提出、NULL・不正 key、休止、K の境界、配備時刻不明、scan 失敗。
2. **集計**: R 不明と穴の除外、PF と平均 R の符号、版・世代の分離、A→B→A、窓の境界、取引日数・集中度。
3. **判定**: 30 件・28 日・ゼロ近傍の境界、主窓が正でも直近 0 件・採否停止・滞留・到達率悪化は要確認。短期が負でも自動判定を変えない。前週の語に依存せず、現配備と異なる hash・世代の確定は `-`。
4. **確定と起動**: 通常の shadow 遅延・滞留・再起動を組み合わせ、猶予前に完了しない、窓終端が動かず翌週が混ざらないこと。片方の保存後の例外、空集合と判定不能、再試行中の配備変更、夏冬時間・休場週、callback 別の記録と同日最大 3 回。
5. **通知**: 確定直後・event 前、event 保存中、送信失敗、送信成功直後・cursor 保存前に障害を入れる。未発行回収、週の一致、許容する重複、初回 cursor、未知 event、固定 fields と ref、本文 4 項目・escape・mention 無効・上限・省略、通知不要週。
6. **情報経路**: 週次表、停止 reason、親 report に専用 canary を入れ、改善 context・描画後 prompt に出ないことを確認する。読取関数の呼出しを監視し、依存経路自体が無いことも検証する。既存全体集計の例外は別に検証し、偶然一致する数値の文字列検索だけに頼らない。停止名は名前・kind 以外を受け取れず、scan 失敗・操作途中では出ないこと。
7. **CLI**: help の記録、daemon なしの読取、指定週・未確定。

### 受入

1. web で未使用に絞り、3 本の参照元・戦略候補への登場を読取だけで照合する。disable / retire は隔離した fixture で確かめ、本番での停止は条件にしない。
2. 戦略別画面の件数を読取専用 DB と照合し、穴・到達率・集中度・世代を確認する。
3. 通常猶予後に report ができ、内容のある週だけ Discord に届く。指定週・4 項目・未終端の明示を確認する。
4. 改善 prompt に週次の数値・判定語がなく、停止名は名前・kind だけであることを確認する。

## 5. 実装の分け方

| 段 | 内容 | 主な場所 | 依存 |
|---|---|---|---|
| 1 | 未使用の判定・read model・DTO | `S/plugin/read_model.py` | なし |
| 1 | web 列・絞り込みと CLI | `A/web/plugin_pages.py`、`S/backtest/cli.py`、`S/cli_surface.py` | 棚卸し集計 |
| 2 | R・穴・採否の集計、period_summary の置換 | `S/store/signal_shadow.py`、集計 module | なし |
| 2 | 成績応答・web 列・日次 report | `S/ops/performance_view.py`、`A/web/`、`S/loops/econ_signal_report.py` | 集計とチャートの変更の統合。統合待ちはこの項目だけ |
| 3 | 週の記録・集計表・判定・原子的保存・report | `S/store/db.py`、新 module | 両集計。scheduler から独立に検証可能 |
| 3 | callback 別記録、猶予後の起動・再試行 | `S/core/scheduler.py`、`S/service.py` | 週次保存 |
| 3 | 指定週の API・CLI・web | `S/ops/`、`S/cli_surface.py`、`A/web/` | 週次保存 |
| 3 | event・未発行回収・bot 配送と本文 | `S/ops/events.py`、`A/gate/` | 週次保存と指定週 API |
| 独立 | 停止名の投影と掲示 | `S/loops/improve_context.py`、`S/loops/prompts/improve_mission.md` | 週次判定への依存なし |

## 6. 利用者に問う事項

| # | 問い | 推奨 | 理由 |
|---|---|---|---|
| 1 | 主源。(a) 採否できた signal の機械採点。(b) 実取引 | (a)、正は機械採点上は床超えまで | 実取引は採否・枠・裁量決済を含み、機械採点は欠測・費用の偏りを持つ。両方を並べ、非正を調査・停止検討に使う |
| 2 | 窓・最低条件・線・正の意味。(a) 12 週、30 件・28 日、平均 R の ±0.05 は要確認、短期 4 週は表示だけ、正は機械採点上は床超えまで。(b) 承認時 holdout からの低下を基準に別設計 | (a) | 運用上の条件で統計的保証ではない。滞留・到達率悪化も要確認。集中度を示し、承認時の値は表示だけ |
| 3 | 未使用。(a) K=8 週、現版が人間由来なら提案除外。(b) K=4 週。(c) live 戦略の参照なしだけ | (a) | 材料の試行期間を取る。改善休止も暦週に含むが休止と明示。現版の出所不明も除外 |
| 4 | 将来の方向語。(a) 前方成績による適応を例外として許す別設計。(b) 人間向けだけ | (b) | 週次の語も holdout 区間の情報で、候補選択を変える。今回の実装には入れず、将来も問 5 の裁定を先にする |
| 5 | 既存の 90 日の実取引全体成績。(a) 明示的な例外として残す。(b) 件数・hold 率だけにする | (a)、前方情報の例外と記録することが条件 | 勝率・PF・ペア別損益と件数が既に渡る。shadow を足さないことと全面遮断は別。(b) は別途変更する |
| 6 | 通知頻度。(a) 内容のある週だけ。(b) 毎週必ず 1 通 | (a) | 停止候補は続く限り毎週再掲する。新しい未使用も無い週の空通知を減らす。report は毎週残る |

技術で決めたもの: 帰属と採点規則を変えず、評価を mission と分離する。R で数え、配備世代で再開始する。対象・評価時刻を固定し、集計と完了を原子的に保存する。通知は指定週を読む。前方 backtest は期間の入口と遮断を別設計するため別起票とする (`S/backtest/holdout.py:180,216`)。窓・閾値・正の意味は技術決定でなく上の方針案である。

## 7. 残余・やらないこと

- **abandoned の採点**: 採否の正規形と shadow の外部キーに触れるため行わない。欠測の原因の是正は別課題。
- **自動停止・削除**: 行わない。
- **前方 backtest**: 別起票。機械採点で欠測を埋めたことにはしない。
- **戦略ごとの枠・実帳簿**: 関連チケットのまま。機械採点が正というだけで着手条件を満たしたとは扱わない。
- **改善の日次化**: 週次確定は mission と独立なので影響しない。
- **命名・保有上限**: 関連チケットのまま。停止名は同名の自動再配備だけを抑止し、仮説の禁止は別の人間方針とする。
- **採点の偏りと誤差**: spread は採否時の 1 点、swap・手数料なし、同時保有の制約なし。30 件・28 日でも独立性・符号の安定は保証しない。停止前には reevaluate で履歴も確かめる手順を運用文書に書く。
- **将来の方向語**: 入れるなら「前方成績による適応を例外として許す」と記録して別設計する。低頻度だから安全とは扱わない。
- **人間による数値の転記**: 防げない。holdout の数値を方針に書かない運用上の注記の対象に週次の数値を足す。

## 付録 A. 文書とコードの食い違い

### A.1 チケットとコード

| 文書の記述 | コード・DB | 位置 |
|---|---|---|
| `strategy-attribution-and-shadow-evaluation`: orders / TradeIntent に発注元の戦略・signal の帰属が無い | signal 由来の建玉は decision 行で signal (plugin・版) に結ばれ、mission の trigger も `signal:<plugin>` を持つ | `S/store/db.py:175`、`S/store/signal_decisions.py:92-112`、`S/ops/trade_events.py:184-188` |
| 同: 影の帳簿は設計待ち | accept / reject の shadow は稼働中 (57 件) | `S/store/signal_shadow.py:121`、`S/store/db.py:198` |
| 同: LLM が採用しなかった signal も含む全 signal を採点 | reject は含むが、abandoned は decision が無く採点されない | `S/store/signal_shadow.py:129-137` |
| 同: entry = バー終値、固定コスト、exit = 戦略の規則か SL / TP | entry = 採否後の最初の 1 分足 open か指値到達、spread は quote 由来か仮定値、exit = SL / TP / day close | `S/store/signal_shadow.py:27-35,80-108`、`S/core/paper_fills.py:201-245` |
| 同: exit 規則が無い戦略の定義を決める | `exit_mode` は `levels` だけ | `S/plugin/loader.py:291-293` |
| 同: 戦略別の表示を `afx ctl status` に | status の shadow は採否種別 × 状態で、戦略別ではない。戦略別の実取引は web の段が出す | `S/loops/econ_signal_report.py:325-334`、`W/ops/performance_view.py:867` |
| `weekly-improve-rebacktests-deployed-strategies`: 稼働中の戦略は配備時の成績しか持たない | 再評価の操作と、同名候補との同条件の再生がある | `S/cli_surface.py:315`、`S/plugin/strategy_gate.py:86-89,226` |
| 同 (2 つ目の処置案の節): 族の表に「稼働中の最新成績」を載せ、agent に in_sample 相当の数値を渡す | 同じ票の 10-25 の裁定 (語だけ) と食い違う。族の表は 5 列固定で成績を持たない | `S/loops/improve_context.py:259-266`、`S/loops/prompts/improve_mission.md:31-33`、設計 2026-10-24 §1.2-5 |
| 同: agent に前方区間の数値を見せない | 直近 90 日の実取引の勝率・PF・ペア別を prompt に渡している | `S/loops/improve_context.py:131-182`、`S/loops/prompts/improve_mission.md:10-18` |
| 同: 週次 mission の冒頭で走らせる | mission は手動・繰り返しでも走る。週 1 回の保証は無い | `S/loops/improve_loop.py:906` |
| 同: `baseline-replay-unimplemented` と実装を共有できるか | 期間の入口は 2 つで任意の区間を取らない。共有できるのは再生の core まで | `S/backtest/holdout.py:180,216` |
| `weekly-plugin-inventory-notice`: 指標 50 本のうち参照 9 本 | 指標 94 本、戦略 2 本、参照 3 本 | `plugins/` 直下 (symlink 96) |
| 同: 提案一覧を `afx ctl` から読む | 保存済みの結果の読取は daemon を要らない。disable / retire は `afx plugin` | `S/cli_surface.py:307-322`、`docs/operations/ops-api-2026-10-09.md` 冒頭 |
| 同: 停止済みの名前は改善 prompt に出す | 今は出ていない (改善 context と prompt に停止・引退の項目が無い) | `S/loops/improve_context.py:710-733`、`S/loops/prompts/improve_mission.md:37-42` |
| `improve-daily-params-weekly-algorithm`: cadence が 1 種類 | `schedule.improve` は `weekly` と `daily` を受ける | `S/config.py:460`、`config/settings.yaml.example:79-80` |
| `redo-deployed-strategy-evaluation-after-time-repair`: 配備中 6 本 | 今は 2 本 (11-07 に 2 本、11-13 に 2 本を disable) | `plugin_deploy_ops` |

### A.2 設計とコード

| 文書の記述 | コード | 位置 |
|---|---|---|
| 設計 2026-11-01 §2.1: 親専有レポートで strategy × decision × actor / code と shadow 成績を見る | 日次 report の decisions は種別の合計、shadow は採否種別 × 状態で、戦略別が無い (abandoned も集計は戦略別を返すが合計だけを書く。`S/store/signals.py:477,531`) | `S/loops/econ_signal_report.py:297-307,325-334,552-585` |
| 同 (人間面用の期間集計) | `period_summary` は src に呼び出し元が無い (test だけ) | `S/store/signal_shadow.py:316`、`tests/store/test_signal_shadow.py:321` |

### A.3 文書の方針と主源選択のトレードオフ

| 一方 | 他方 |
|---|---|
| チケット 10-25 の裁定: agent に族別の方向の語を渡す | 設計 2026-11-01 §3.6: 改善 agent に decision / shadow を出さない |
| 同上 | 設計 2026-10-24 §1.2-5、§2.6: 族の表は成績の良し悪しを意味しない |
| 主源の選択: チケット 10-25 は実取引を主源とする | 実取引は採否・枠・裁量決済を含む。機械採点は採否できた signal だけで費用の偏りもある。文書の矛盾ではなく、測る対象の違い (§6 問 1) |

## 付録 B. 確かめていないこと

- 指標への `afx plugin disable` / `retire` を本番で実行したことは無い (操作と依存の検査のコードがあることだけを見た。`S/plugin/deploy_disable.py:123-131`)。本番での停止を受入条件にせず、隔離した fixture で検証する。
- 日次 report の hook を複数 callback にしたとき、既存の記録 (`alert_state` の 1 key) の読み替えが要るかは実装で確かめる (`S/core/scheduler.py:508-530`)。
- 取引週の最後の close を「次の開場まで 24 時間以上」で決める規則が、祝日の休場の週で意図どおりになるかは test で確かめる。
- 判定の最低条件を満たすまでの週数は戦略ごとの採否頻度で変わる。経過週数だけでは判定可能とはしない。
- bot の未知 event の扱いは通知の検証で確かめる。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-19 | C0 | 初版 (下書き) | 3 チケットを束ねて設計する | — |
| 2026-11-19 | C0.1 | 判定・週次確定・通知・棚卸しと情報経路を明確化し、3 段に整理 | 設計レビュー 1 周目の指摘を反映 | |
