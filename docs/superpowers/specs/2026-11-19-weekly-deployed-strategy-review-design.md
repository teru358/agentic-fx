# 週次の稼働中戦略の評価と棚卸し — 設計 C0.3

チケット: `weekly-improve-rebacktests-deployed-strategies`、`strategy-attribution-and-shadow-evaluation`、`weekly-plugin-inventory-notice` (3 件を束ね、3 段で実装する)。
関連: `improve-daily-params-weekly-algorithm`、`per-strategy-virtual-books`、`plugin-naming-lifecycle-policy`。

パスの略記: `S/` = `src/agentic_fx/`、`A/` = `afx-ops/afx_ops/`、`W/` = `tmp/wt/webcharts/src/agentic_fx/` (branch `webcharts`、未統合)。根拠はコードの file:line で示す。

## 0. 何が起きているか

- 配備後の動きを戦略ごとに見る面が無い。signal の採否と機械採点は DB にあるが、戦略別に集計して出す経路が無い。
- 配備中の指標は増え、使われないものが残る。web で 1 本ずつ参照元を読めるが、一覧として拾えない。
- 戦略を止める判断は人間がするが、その判断を促す定期の知らせが無い。

### 0.1 今のコードで確かめた前提

| 前提 | コード | 帰結 |
|---|---|---|
| 帰属 | `signal_decisions(signal_id UNIQUE, trade_intent_id)` が signal の plugin・hash と建玉を結ぶ。trigger は `signal:<plugin>` (`S/store/db.py:175-183`、`S/store/signal_decisions.py:92-112`、`S/ops/trade_events.py:43,184-188`) | 帰属の列は足さない |
| 機械採点 | decision 行から shadow を作る (`S/store/signal_shadow.py:121-137`)。採否前に abandoned となる signal は含まれない | 全 signal の成績とは呼ばず、穴と採否到達率を併記する |
| 採点規則 | 観測開始は採否時刻以後の UTC 分境界 (`S/store/signal_shadow.py:99-108`)。entry は成行の最初の 1 分足 open か指値到達、exit は SL / TP / day close。spread は quote か仮定値 (`S/store/signal_shadow.py:80-96`、`S/core/paper_fills.py:201-245`、設計 2026-11-01 §3.4) | 規則は変えない。戦略の exit_mode は levels のみ (`S/plugin/loader.py:291-293`) |
| 人間の表示 | 実取引の戦略行はチャートの変更が提供する (`W/ops/performance_view.py:868-879`)。日次 report の shadow は採否種別 × 状態 (`S/loops/econ_signal_report.py:325-334,552-585`) | 実取引を作り直さず、機械採点を並べる |
| 再評価 | `afx plugin reevaluate` と同名候補との同条件の再生がある (`S/cli_surface.py:315`、`S/plugin/strategy_gate.py:86-89,226`) | 最終判断は既存の操作を使う |
| 主源 | 実取引は戦略だけでなく採否・枠・裁量決済を含む。機械採点は採否前の欠測と費用の偏りを持つ | 主源は §6 問 1 で選ぶ |
| 前方 backtest | 期間の入口は run_in_sample / run_holdout_gate のみ (`S/backtest/holdout.py:180,216`) | 任意の前方区間の再生は別起票 |
| 改善入力 | 既に 90 日の実取引の勝率・PF・ペア別損益と件数を渡す (`S/loops/improve_context.py:146-179`、`S/loops/prompts/improve_mission.md:10-18`) | 「shadow を足さない」と「前方情報を全面遮断」は別。既存入力は §6 問 5 |
| 起動 | mission は手動・繰り返しでも走り (`S/loops/improve_loop.py:906`)、cadence は weekly / daily (`S/config.py:460`) | 週次確定を mission と切り離す |
| 候補の記録 | 保存 API は NULL を許し、依存なしの legacy 戦略・非対応 kind・読取失敗は key を持たない (`S/store/candidate_archives.py:64-84`、`S/plugin/logic_identity.py:160-178,413-430`) | NULL 行は依存の根拠から外し件数を report に出す |
| 配備・出所 | journal の時刻は updated_at だけ (`S/store/db.py:508-520`)。出所は staging / human / reevaluation (`S/plugin/read_model.py:63-65`) | 完了した配備変更と元の版の出所を使う |
| 操作の置き場 | disable / retire は最上位 `afx plugin` (`S/cli_surface.py:307-310`) | 保存結果の読取も同じ場所に置く |
| 通知 | 開始通知は mission ごとで、event は固定語と整数の投影 (`A/gate/improve_render.py:691`、`S/ops/events.py:54,85`) | 週次は専用 event |
| データ | shadow は live の 1 分足を使い、評価履歴の同期とは別 (設計 2026-11-01 §3.5-2) | 履歴の鮮度の守りは変えない |

## 1. 何が変わるか (動作)

### 1.1 利用者が受け取るもの

1. **いつでも (web・CLI)**: plugin 一覧で未使用の指標と条件が読め、絞れる。戦略別画面では、実取引の隣に「採否できた signal の、採否時点からの仮想成績」(以下、機械採点)、採否、穴の件数が並ぶ。「実約定を含まず楽観側に出ます」「採否前に abandoned となった signal は含みません」を固定表示する。
2. **週に 1 回**: 取引週の終わりから通常の確定猶予を経て、親専有の週次 report ができる。停止候補・要確認・新規の停止提案可能な指標があれば Discord に 1 通届く (§6 問 6 の推奨)。劣化・3 日超の未完了・打切りは選択に関わらず通知する。標本不足と初回の未使用一覧は report に出し、単独では通知しない。
3. **人間の判断**: 戦略の停止は `afx plugin disable <name>`、履歴での確認は `afx plugin reevaluate <name>`。指標は disable / retire。自動停止・削除はせず、提案を放置しても取引は変わらない。

判定は符号と信頼性の 2 軸とする (§2.4)。要確認は床超えだけを保留し、停止候補は理由を併記して残す。床超えは機械採点上の意味に限り、実運用の収益性を保証しない。

### 1.2 改善 agent が受け取るもの

週次の数値も判定語も渡さない。停止済みの同名 plugin の自動再配備を避けるため、名前と kind だけの一覧を、前方成績に基づく人間の判断の写しという例外として加える案とする (§2.8、§6 問 4)。仮説そのものの禁止には使わない。

### 1.3 変わらないもの

発注・SL・決済・資金保護、採否の契約、shadow の採点規則、採用 gate と床、承認、disable / retire / reevaluate の動作。外向きの送信先も増やさない。

## 2. 仕組み

### 2.1 段の分け方と依存

段 1 は棚卸し、段 2 は戦略別の機械採点、段 3 は両集計を使う週次確定・通知とする。集計は独立に作れ、段 2 の web 表示だけチャートの統合を待つ。停止名の掲示は週次判定に依存しない。

### 2.2 指標の棚卸し (段 1)

live の指標で、次のすべてを満たすものを未使用とする。

1. 配備中の戦略の config から参照されない (既存 dependents が空。`S/plugin/read_model.py:170,405-409`)。
2. 保留中の承認候補から参照されない (`S/plugin/deploy_deps.py:28`)。
3. 直近 K 週に作られた**戦略候補の依存 plugin として登場しない**。`candidate_archives.family_key, created_at` (`S/store/db.py:635-646`) を既存 `parse_family_key` (`S/plugin/logic_identity.py:329`) で構造的に読み、legacy / v2 の strategy の依存名だけを調べる。指標自身の再提出・部分文字列の一致は利用に数えない。
4. 現配備世代の配備から K 週以上たっている。

K の推奨は 8 (§6 問 3)。NULL の family_key はその行を依存の根拠から外し、除外件数を report に出す。不正な非 NULL key は判定不能とする。改善休止も暦の K 週に含むが、候補生成 0 件と明示する。配備時刻不明も判定不能。時刻は §2.3 の完了した配備変更から取り、既存入力 (`S/plugin/read_model.py:106-123`) に時刻の投影を足す。

未使用と停止提案可能を分ける。停止提案可能は「未使用かつ現版の出所が staging と確認できる指標」だけとする。出所の語彙は `S/plugin/read_model.py:63-65,274-281`。reevaluation は元の版の出所を継ぎ、確認できなければ出所不明とする。human・出所不明は未使用でも「提案対象外」を付ける。除外は名前全体でなく現版に適用する。

read model の行に `inventory` (未使用 / 判定不能、条件、出所、停止提案可否) を加える (`S/plugin/read_model.py:573`)。web は列と絞り込み (`A/web/plugin_pages.py:185-196`)、CLI は `afx plugin list --unused`。段 1 は読取時の集計で保存しない。

この read model は操作 API と plugin CLI が読む (`S/ops/plugins_view.py:58-59`、`S/backtest/plugin_deploy_cli.py:113`)。改善は別の一覧 (`S/loops/improve_loop.py:985`) を使い、inventory を渡さない。

### 2.3 戦略別の前方成績 (段 2)

**対象と世代。** live の `(name, content_hash, 配備世代)`。世代は実際に配備を変えた成功操作だけで進める。journal の `kind=approve AND phase=decided AND switch_required=1` と deploy ops の `op_kind=rollback_symlink AND phase=done` から、現在の版に至る最後の遷移と updated_at を取る (`S/store/db.py:508-520,535-552`、`S/plugin/switch.py:1767-1778`)。棚卸しも同じ投影を使う。失敗・取消・bless・revoke では進めない。既存 deploy_generation は全 phase の最大操作を取るため流用しない (`S/plugin/switch.py:2000-2007`)。版変更・A→B→A は再開始し、signal の hash と発生時刻で限定する。配備時刻不明は判定不能。

**窓。** 読取時点から 12 週前まで、現世代の開始以後に限る。採否時刻 `signal_decisions.decided_at` で窓に入れる。週次は読取時点の代わりに週末時刻を使う。実取引の名前単位の行とは集計範囲が違うので世代開始を注記する。

| 系列 (合算しない) | 内容 | 単位 |
|---|---|---|
| 機械採点・全採否 | 現世代の shadow の数値終端 sl / tp / day_close。accept と reject の両方。採否できた signal の、採否時点からの仮想成績 | R |
| 機械採点・採否別 | 同じ母集団を採否で分ける | R |
| 実取引 | チャートの戦略行 (`W/ops/performance_view.py:868-879`) | 口座通貨・R |

**R。** `pnl_pips ÷ (|entry_price − stop_loss| ÷ pip_size)`。SL は signal の payload、他は shadow 行。分母が 0 以下・非有限・SL 欠落は R 不明として率から除外する。lot を持たない採点を pips で合算すると広い SL が重くなるため R にする。backtest の金額による avg_r (`S/backtest/metrics.py:89-97`) と無条件には一致しない。

**表示。** N (R 既知)、勝 / 負 / 分、平均 R、PF (R の利益合計 ÷ 損失合計)。損失が無ければ PF は計算不能として表示する。判定は平均 R だけを使う。母数 5 未満は「–」、5〜29 は参考、30 以上は通常表示。既存の 30 件 (`S/backtest/metrics.py:20,142`) は表示上の床で、有意差・独立性の根拠ではない。30 件・28 日は運用上の最低条件に留める。

率の隣に abandoned (理由別。`S/store/signals.py:477`)、no_fill、data_insufficient、未終端、R 不明を出す。採否別件数、accept のうち risk gate 通過数、signal に対する採否到達率、未終端の最古の経過時間も出す。集中度は採点のある取引日数と日別最大件数の割合だけとする。到達率は同じ発生期間の signal に対する decision ありの割合。現世代内の直近 4 週と前 4 週を表示で比較し、空の期間は比較不能とする。自動判定には使わない。

**経路。** 純関数と読取専用接続で集計する。pips だけを返す `period_summary` (`S/store/signal_shadow.py:316`) を置き換える。web の列を足し、親専有の日次 report の shadow 節も戦略別にする (`S/loops/econ_signal_report.py:552`)。

### 2.4 週次の確定と判定 (段 3)

**境界と確定時刻。** NY 取引日で月〜金の暦週の最後の取引日を market_hours の暦から決める。開場の観測を待たず、平日の休場で週を割らない (`S/core/market_hours.py:16,25-43`)。週の印は `trading_date(close_at − 1 分)` (`S/core/scheduler.py:486-488`)。窓終端はこの close に固定し、確定は close + FINALIZE_GRACE (1 日) 以後。未終端の期限は signal ごとの `day_close_deadline(t0) + FINALIZE_GRACE`、指値は 2 段目の期限を使う (`S/store/ohlcv_slices.py:47`、`S/core/econ_slice_worker.py:774-799`)。期限超えが残っても理由・経過時間を残して確定する。

**起動と再試行。** 独自の tick と alert_state key を使い、起動時・tick で運用開始週から猶予到来済みの週を暦から列挙する。運用開始週は機能の初回起動時に直近の猶予到来済み週として保存する。週の記録がないものを未完了とし、窓終端から 14 日以内は古い週から専用 thread 1 本で処理する。試行回数は「対象週 × UTC 日」で最大 3 回。投入前に alert_state に加算し、再起動でも引き継ぐ。同じ週の実行を重ねない。上限到達週は翌日に回し、後続週は処理できる。窓終端から 14 日超の未完了週は打切り記録を確定し、以後再試行しない。日次 hook (`S/core/scheduler.py:450-468,508-530`) は変えず、mission に依らず発注 tick を塞がない。

**保存と状態。** 既存表は変えず、週の記録と集計 2 表を 1 transaction で確定する。処理例外は全体を rollback して再計算し、計算不能として返せる結果は劣化で確定する。

| 記録 | 内容 |
|---|---|
| 週の記録 | 一意な週、窓終端、確定時刻、評価時刻、対象の名前・hash・世代、規則版、確定状態 (完了 / 打切り)、評価状態 (正常 / 劣化 / 未評価)、固定の劣化理由と件数、棚卸し状態 |
| strategy_review_weekly | 週・名前・hash・世代で一意。最初の採否、N、勝敗、R の利益・損失合計、穴・採否・集中度、短期表示、符号と理由。金額なし |
| plugin_inventory_weekly | 週・名前、未使用 / 判定不能、現版の出所、条件、停止提案可否。棚卸しが正常な直前の完了週と比べ、未使用への移行を新規とする。比較元なしは初回一覧で新規扱いしない。初回は指標の通知件数・名前も抽出しない |

確定状態と評価状態は独立に扱う。完了でも scan 失敗・不正 key・集計不能・配備時刻不明があれば劣化とし、対象の系列と理由を保存して通知する。正常な空集合・標本不足は劣化にしない。実取引だけ読取不能でも劣化理由を残し、機械採点と混ぜない。打切りは未評価・集計なしとし、対象週と期間超過を report に残して失敗通知する。

戦略対象は窓終端時点の配備記録から復元し、signal・採否・abandoned は各時刻で窓終端の as-of に切る。shadow は窓内の採否に属し評価時刻までに終端したものを採点する (`S/store/db.py:183,208`、`S/store/signals.py:477`)。棚卸しは実行時の live scan・保留候補から作り、評価時刻を完了記録に書く。scan 失敗は棚卸し判定不能とし、新規の抽出・次週の棚卸し比較元から外す。実取引は読取時の値と注記する。

**判定の方針案 (§6 問 2)。** 主窓は現世代内の最大 12 週、短期 4 週は件数・平均 R の表示だけ。符号の軸は次の表を上から評価する。

| 条件 | 符号 |
|---|---|
| 配備時刻不明・集計不能 | 判定不能 |
| N < 30、または現世代の最初の採否から 28 日未満 | 標本不足 |
| 平均 R < −0.05 | 停止候補 |
| 平均 R の絶対値 ≤ 0.05 | ゼロ近傍 |
| 平均 R > 0.05 | 床超え候補 |

信頼性の要確認理由は「窓終端までの直近 2 週に signal 無し」「評価時刻が期限を超えた未終端」の 2 つだけで、符号に関わらず併記する。表示判定は符号を基本に、ゼロ近傍は要確認、床超え候補は理由があれば要確認、無ければ「機械採点上は床超え」とする。通知用の要確認本数はゼロ近傍または理由ありの戦略数で、停止候補との重複を許す。

前週の語と承認時 holdout は判定に使わず、重なる窓を独立の確認としない。28 日は最低経過期間で、到達率比較のために 8 週を要求しない。

**report と操作。** `data/improve_reports/` の専用 dir に既存どおり owner-only で書く (`S/loops/econ_signal_report.py:779`)。確定・評価状態と理由、判定、3 系列、穴・到達率・集中度、承認時の成績、未使用と停止済みの一覧を載せる。初回一覧は report だけに出し、人間由来・出所不明の行には「提案対象外」を付ける。書込み失敗は同じ確定から再生成する。提案の採用・却下ボタンは作らず、停止は既存操作。止めれば次の対象から外れる。

### 2.5 通知 (段 3)

- event は `weekly_review_ready`。週は実在日を検証した `ref=weekly:YYYY-MM-DD` に置く。fields は固定語と非負整数 (state=ready / degraded / failed、停止候補・要確認・停止提案可能・新規の停止提案可能の本数、評価状態、棚卸し状態 (正常 / 判定不能)、固定の劣化理由別件数) のみ。failed は未完了 / 打切りの固定理由だけで、本数を省く。名前・成績の数値は入れない。専用の文法検査を置く (`S/ops/events.py:54,85`)。
- ready / degraded の本文は必ず `GET /v1/review/weekly?week=YYYY-MM-DD` で指定週を読む。応答の週が違う・取得不能なら event の状態・固定理由・本数だけに縮退し、劣化の見出しを残す。failed は詳細を取得せず、週と失敗の固定文だけを出す。詳細は保持しない。
- 起動時と tick で確定時刻が直近 14 日以内の通知要の週を回収する。完了・正常は通常条件で ready、完了・劣化は必ず degraded、打切りは必ず failed とする。`NOT EXISTS (code, ref, state)` で未発行を選び、emit_best_effort で出す。event 行を発行記録とする既存の方式に従う (`S/ops/events.py:199-211`、`S/ops/service.py:1430-1445`)。
- 未完了で窓終端から 3 日超・14 日以内の週も同じ監視で failed を 1 回発行する。打切り時は同じ failed の未発行分だけ回収する。後に完了すれば通知要の ready / degraded を別に出せる。回収期間を過ぎた記録は report・web・CLI で読む。
- bot は既存の event 配送・cursor 保存・送信 retry 経路 (`A/bot/adapter.py:871-892,971-975`、`A/gate/service.py:153-167`) に接続する。送信失敗では cursor を進めない。成功直後・cursor 保存前の停止による重複は許容する。初回接続は既存どおり末尾からで、過去週は web・CLI・report で読む (`A/gate/state.py:149-172`)。
- 本文の成績は **pf / avg_r / 件数 / 評価不能** の 4 項目だけ。勝率・DD・金額・閾値は載せず、機械採点と明記する。既存通知の本文 2000 文字上限、escape、mention 無効、低優先段落からの省略を継承する (`A/gate/render.py:41,77,88,234`、`A/gate/improve_render.py:4-6`)。
- 通常は停止候補 ≥1 または要確認 ≥1 または新規の停止提案可能 ≥1 で通知する (§6 問 6)。指標の通知件数は未使用かつ現版の出所が staging の行だけ、名前はそのうち新規だけを最大 5 件抽出し、残りは本数にする。human・出所不明は件数にも名前にも入れない。初回一覧は report のみで、通知条件・本文に入れない。failed に成績・名前は付けない。
- OPS_EVENT_SPECS に既定 True で登録する (`A/gate/render.py:470-477,511-513`)。ops_notices は default / all だけで、止めるには bot 自体を止める (`A/config.py:42,81`)。設定・送信先を増やさず、webhook も同じ fields だけとする。

本文例 (架空):

```text
[週次] 停止の提案 2026-11-20
戦略 (機械採点、現配備以後・最大 12 週)
  <name>: pf 0.82 avg_r -0.11 採点 34 件 — 停止候補
停止提案可能な指標 14 本 (新規 2: <name>, <name>)
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
| 週次判定・評価状態 | 出す | 停止候補・要確認・劣化・失敗 | 本数・固定理由・状態だけ | 出さない |
| 未使用の指標 | 名前・条件・提案可否、初回一覧は report | 停止提案可能の本数・新規名だけ | 同じ対象の本数・状態だけ | 出さない |
| 停止済みの名前 | 出す | 出さない | 既存 plugin_disabled のまま | 例外採用時、改善だけ名前・kind |
| 止めた理由・経路、承認・却下の reason | 既存の面 | 出さない | 新規には出さない | 出さない |
| 承認時の成績 | report | 出さない | 出さない | 出さない |

週次表・report は data/ 配下に置き、agent の入力・reflection・RAG・取引 prompt に結ばない。改善 worker は Landlock で data/ を読めない (`S/core/landlock.py:208-227`、`S/mission_worker.py:365-368`)。採点は LLM の reasoning を読まない。

停止名は前方成績に基づく人間の判断の写しとして情報を運ぶ例外である (§6 問 4)。既存の実取引全体集計は §6 問 5 の選択であり、3 段の着手を止めない。情報経路の検証で既存集計に期待する内容だけがその選択で決まる。

### 2.8 停止済みの名前の掲示 (独立)

成功した live scan と完了済みの配備記録を照合し、過去に配備済みで今 live ではない名前だけを出す。scan・journal の読取失敗、未完了の switch / deploy 操作、版不明を停止と誤認しない (`S/plugin/read_model.py:86-134`、`S/store/db.py:518-527,549`)。

kind は検証済み保存版の config から取得し、親側で `{name, kind}` だけに投影する。journal の kind は approve / bless であり plugin の kind ではない (`S/store/db.py:510`)。actor・reason・成績・disable の時刻は context に渡さない。既存の名前文法 (`S/plugin/logic_identity.py:206`) を通し、昇順で 50 件・4 KiB、超過は固定文で省略する。

§6 問 4 の例外を採る場合に限り掲示する。固定文は「停止済みの同名 plugin を自動で再配備しないでください」。同じ仮説の別名候補までは禁じない。人間の rollback で live に戻れば一覧から外れる。週次確定を待たずに実装できる。

## 3. 失敗時・運用

| 事象 | 動き |
|---|---|
| 集計例外・DB busy | 全体を rollback。§2.4 の週別再試行・14 日超の打切りに従い、3 日超から失敗通知 |
| report 書込み失敗 | 確定から再生成。通知は指定週の API を読む |
| 確定後・event 前に停止 | 未発行の確定を回収 |
| bot の詳細取得失敗 | 指定週の見出し・本数に縮退。送信失敗なら retry |
| 確定が古い | web・CLI は日付と注意を出す。現配備と一致しなければ判定は `-` |
| 期限超えの未終端・直近 2 週 signal 無し | 床超えを保留し、停止候補は残す。到達率低下だけでは自動判定を変えない |
| 版変更・rollback | 世代ごとに再開始。以前の確定は保存し、現在の判定に流用しない |
| 週の途中で停止 | 週末の対象から外す。確定済みの週は書き換えない |
| scan 失敗・集計不能・配備時刻不明 | 完了しても劣化。通常条件に関わらず通知 |
| 新しい表が無い | 未確定を返す。表は既存の追加の慣行で作る |

## 4. 検証と受入

### 追加する検証

1. **棚卸し**: 各条件、現版の出所・reevaluation の継承、保留候補、legacy / v2 の依存、指標の再提出、不正 key、休止、K の境界、時刻不明、scan 失敗。NULL 行の除外件数が report に出ること。human・出所不明が未使用でも通知件数・名前から除外され、report では提案対象外となること。
2. **集計**: R 不明と穴、PF と平均 R、世代分離・A→B→A。失敗・取消・bless・reverted・needs_retry・foreign・revoke で標本が再開始しないこと。窓境界・集中度 2 項目。
3. **判定**: 30 件・28 日・±0.05、判定不能、各符号と要確認理由の組合せ。負の主窓に滞留・直近 0 件が重なっても停止候補と通知を残す。到達率低下・比較不能・短期の負だけでは変わらず、配備 30 日でも床超え可能。前週語に依存せず、現配備と違う hash・世代は `-`。
4. **確定と起動**: 猶予前の完了禁止、週末 as-of と評価時刻、翌週混入防止、全体 rollback、正常な空集合・標本不足と劣化の分離、再試行中の配備変更から週末対象の復元。夏冬時間・平日休場、翌週を越す障害からの古い週順の回復、週 × UTC 日の試行上限と再起動時の継承、14 日の境界・打切り・再試行停止。
5. **通知**: event 前後・送信失敗・cursor 保存前の停止、14 日の回収境界、週の一致、許容重複・初回 cursor。3 日超の未完了・打切りの failed、停止候補等が 0 でも scan 失敗・集計不能・時刻不明の degraded と未発行回収、後の ready / degraded、未知 event、fields・ref、本文 4 項目・escape・mention 無効・上限、初回一覧だけでは通知も名前表示もしないこと、名前 5 件・通知不要週。
6. **情報経路**: 週次表・停止 reason・report の canary が改善 context・prompt に出ず、読取依存経路も無いこと。report dir が guarded data dir の子孫であること。既存全体集計は問 5 に応じて別に検証する。停止名は名前・kind 以外を受け取れず、scan 失敗・操作途中では出ないこと。
7. **CLI**: help の記録、daemon なしの読取、指定週・未確定。

### 受入

1. web で未使用に絞り、3 本の参照元・戦略候補への登場を読取だけで照合する。disable / retire は隔離した fixture で確かめ、本番での停止は条件にしない。
2. 戦略別画面の件数を読取専用 DB と照合し、穴・到達率・集中度・世代を確認する。
3. 実週では通常猶予後の report に初回一覧が出て、一覧単独では通知されないことを確認する。fixture では通知要・不要週、劣化・打切りと未発行回収、翌週を越す再試行、提案対象外の除外、指定週・4 項目・未終端を確かめる。
4. 改善 prompt に週次の数値・判定語がなく、停止名は名前・kind だけであることを確認する。

## 5. 実装の分け方

| 段 | 内容 | 主な場所 | 依存 |
|---|---|---|---|
| 1 | 未使用の判定・read model・DTO | `S/plugin/read_model.py` | なし |
| 1 | web 列・絞り込みと CLI | `A/web/plugin_pages.py`、`S/backtest/cli.py`、`S/cli_surface.py` | 棚卸し集計 |
| 2 | R・穴・採否の集計、period_summary の置換 | `S/store/signal_shadow.py`、集計 module | なし |
| 2 | 日次 report の戦略別化 | `S/loops/econ_signal_report.py` | 集計だけ |
| 2 | 成績応答・web 列 | `W/ops/performance_view.py` (統合後は S/)、`A/web/` | 集計とチャートの統合。この項目だけ統合待ち |
| 3 | 週の状態・集計表・判定・原子的保存・report | `S/store/db.py`、新 module | 両集計。scheduler から独立に検証可能 |
| 3 | 独自 tick・alert_state key、暦週列挙・週別再試行・打切り・状態監視 | `S/core/scheduler.py`、`S/service.py` | 週次保存 |
| 3 | 指定週の API・CLI・web | `S/ops/`、`S/cli_surface.py`、`A/web/` | 週次保存 |
| 3 | event・未発行回収・bot 配送と本文 | `S/ops/events.py`、`A/gate/` | 週次保存と指定週 API |
| 独立 | 停止名の投影と掲示 | `S/loops/improve_context.py`、`S/loops/prompts/improve_mission.md` | 週次判定への依存なし |

## 6. 利用者に問う事項

| # | 問い | 推奨 | 理由 |
|---|---|---|---|
| 1 | 主源。(a) 採否できた signal の機械採点。(b) 実取引 | (a)、正は機械採点上の床超えまで | 実取引は採否・枠・裁量決済、機械採点は欠測・費用の偏りを持つ。両方を並べる |
| 2 | 窓・最低条件・線・正の意味。(a) 最大 12 週、30 件・28 日、±0.05 は要確認、短期 4 週と到達率比較は表示だけ、正は機械採点上の床超えまで。(b) holdout からの低下で別設計 | (a) | 統計的保証ではない。比較のための 8 週は要求せず、直近 2 週 signal 無し・期限超え未終端で床超えを保留する |
| 3 | 未使用。(a) K=8 週、現版が人間由来なら提案除外。(b) K=4 週。(c) live 戦略の参照なしだけ | (a) | 試行期間を取る。休止も暦週に含め明示し、現版の出所不明も除外 |
| 4 | 停止名の掲示。(a) 前方成績に基づく人間の判断の写しとして例外を許す。(b) agent には掲示しない | (a)、例外と記録し名前・kind に限定 | 停止行為も前方情報を運ぶ。同名の自動再配備の抑止に限り、時刻・理由は渡さない |
| 5 | 既存の 90 日の実取引全体成績。(a) 例外として残す。(b) 件数・hold 率だけにする | (a)、前方情報の例外と記録 | 勝率・PF・ペア別損益が既に渡る。3 段の着手は止めず、既存集計の検証の期待値を決める |
| 6 | 通常の通知頻度。(a) 内容のある週だけ。(b) 毎週必ず 1 通 | (a) | 停止候補・要確認は続く限り再掲。劣化・3 日超の未完了・打切りは選択に関わらず通知する。初回一覧は report のみ |

技術で決めるもの: 帰属・採点規則の維持、mission との分離、R と成功した配備変更による世代、2 軸の評価順、NULL key の除外、原子的な確定、暦週列挙と週別の試行上限・打切り、指定週・14 日の通知回収、劣化・失敗通知。前方 backtest は期間の入口と遮断を別設計する (`S/backtest/holdout.py:180,216`)。窓・閾値・正の意味は上の方針案とする。

## 7. 残余・やらないこと

- **abandoned の採点**: 採否の正規形と shadow の外部キーに触れるため行わない。欠測の原因の是正は別課題。
- **前方 backtest**: 別起票。機械採点で欠測を埋めたことにはしない。
- **戦略ごとの枠・実帳簿**: 関連チケットのまま。機械採点が正というだけで着手条件を満たしたとは扱わない。
- **改善の日次化**: 週次確定は mission と独立なので影響しない。
- **命名・保有上限**: 関連チケットのまま。停止名は同名の自動再配備だけを抑止し、仮説の禁止は別の人間方針とする。
- **採点の偏りと誤差**: spread は採否時の 1 点、swap・手数料なし、同時保有の制約なし。30 件・28 日でも独立性・符号の安定は保証しない。停止前には reevaluate で履歴も確かめる手順を運用文書に書く。
- **人間による数値の転記**: 防げない。holdout の数値を方針に書かない運用上の注記の対象に週次の数値を足す。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-19 | C0 | 初版 (下書き) | 3 チケットを束ねて設計する | — |
| 2026-11-19 | C0.1 | 判定・週次確定・通知・棚卸しと情報経路を明確化し、3 段に整理 | 設計レビュー 1 周目の指摘を反映 | |
| 2026-11-19 | C0.2 | 判定を 2 軸化し、配備世代・棚卸し・失敗通知を整理、保存と起動を簡素化 | 設計レビュー 2 周目の指摘を反映 | |
| 2026-11-20 | C0.3 | 評価状態・提案対象・週別再試行と通知を明確化し、重複記述を整理 | 設計レビュー 3 周目の指摘を反映 | |
