# 週次の稼働中戦略の評価と棚卸し — 設計 C1

**状態:** C1 (2026-11-20 利用者承認)。§6 の問い 6 件は推奨どおりに裁定

チケット: `weekly-improve-rebacktests-deployed-strategies`、`strategy-attribution-and-shadow-evaluation`、`weekly-plugin-inventory-notice` (3 件を束ね、3 段で実装する)。
関連: `improve-daily-params-weekly-algorithm`、`per-strategy-virtual-books`、`plugin-naming-lifecycle-policy`。

パスの略記: `S/` = `src/agentic_fx/`、`A/` = `afx-ops/afx_ops/`、`W/` = `tmp/wt/webcharts/src/agentic_fx/` (branch `webcharts`、未統合)。根拠はコードの file:line で示す。

## 0. 何が起きているか

- signal の採否と機械採点は DB にあるが、配備後の戦略別集計を表示する経路が無い。
- 配備中の指標は増え、使われないものが残る。web で 1 本ずつ参照元を読めるが、一覧として拾えない。
- 戦略停止を促す定期通知が無い。

既存経路は付録に示す。shadow は live の 1 分足を使い、評価履歴の同期・鮮度管理は変えない。

## 1. 何が変わるか (動作)

### 1.1 利用者が受け取るもの

1. **いつでも (web・CLI)**: plugin 一覧で未使用の指標と条件が読め、絞れる。戦略別画面では、実取引の隣に「採否できた signal の、採否時点からの仮想成績」(以下、機械採点)、採否、穴の件数が並ぶ。「実約定を含まず楽観側に出ます」「採否前に abandoned となった signal は含みません」を固定表示する。
2. **週に 1 回**: 取引週の終わりから通常の確定猶予を経て、親専有の週次 report ができる。停止候補・要確認・新規の停止提案可能な指標があれば Discord に 1 通届く (§6 問 6 の推奨)。劣化・失敗・打切り・回復は §2.5 に従い通知する。標本不足だけなら report のみだが、現世代が 12 週に達しても N < 30 なら要確認として通知する。初回の未使用一覧は report のみ。
3. **人間の判断**: 戦略の停止は `afx plugin disable <name>`、履歴での確認は `afx plugin reevaluate <name>`。指標は disable / retire。自動停止・削除はせず、提案を放置しても取引は変わらない。

判定は符号と信頼性の 2 軸とする (§2.4)。要確認は床超えだけを保留し、停止候補は理由を併記して残す。床超えは機械採点上の意味に限り、実運用の収益性を保証しない。

### 1.2 改善 agent が受け取るもの

停止名・kind だけを例外として加える案とする (§2.8、§6 問 4)。

発注・決済・資金保護、採否・採点・採用 gate、既存の承認・停止操作と送信先は変えない。

## 2. 仕組み

### 2.1 段の分け方と依存

棚卸し・機械採点・週次確定と通知の 3 段とする (§5)。

### 2.2 指標の棚卸し (段 1)

live の指標で、次のすべてを満たすものを未使用とする。

1. 配備中の戦略の config から参照されない (既存 dependents が空。`S/plugin/read_model.py:170,405-409`)。
2. 保留中の承認候補から参照されない (`S/plugin/deploy_deps.py:28`)。
3. 直近 K 週に作られた**戦略候補の依存 plugin として登場しない**。`candidate_archives.family_key, created_at` (`S/store/db.py:635-646`) を棚卸し専用の読み手で構造的に読み、legacy / v2 の strategy の依存名だけを調べる。pair の所属・字句文法は依存名と独立に扱う。既存 parser はこれらを検査するため、そのまま流用しない (`S/plugin/logic_identity.py:211-214,345-372,386-397`)。指標自身の再提出・部分文字列の一致は利用に数えない。
4. 現配備世代の配備から K 週以上たっている。

K の推奨は 8 (§6 問 3)。条件 3・4 の基準は週次では評価時刻、web・CLI では読取時刻。どれか 1 条件で使用中と決まれば、他の条件の判定不能を問わない。NULL・読めない key は行を除外し、理由別件数を report に出す。読めない key に live 指標名が独立した字句として含まれる場合、その指標行だけ判定不能にする。残る配備時刻不明も行単位の判定不能。これらは棚卸し全体の判定不能や劣化にしない。棚卸し全体が判定不能になるのは scan 失敗・journal 読取失敗だけ。休止も暦週に含め、候補生成 0 件と明示する。時刻は §2.3 から投影する (`S/plugin/read_model.py:106-123`)。

未使用と停止提案可能を分ける。停止提案可能は「未使用かつ現版の出所が staging と確認できる指標」だけとする。出所の語彙は `S/plugin/read_model.py:63-65,274-281`。reevaluation は元の版の出所を継ぎ、確認できなければ出所不明とする。human・出所不明は未使用でも「提案対象外」を付ける。除外は名前全体でなく現版に適用する。

read model の行に `usage_review` (未使用 / 判定不能、条件、出所、停止提案可否) を加える (`S/plugin/read_model.py:573`)。web は列と絞り込み (`A/web/plugin_pages.py:185-196`)、CLI は `afx plugin list --unused`。段 1 は読取時の集計で保存しない。

操作 API・CLI が読む read model に追加する。改善の承認済み一覧 (`inventory`) は変えず、`usage_review` は渡さない (`S/loops/improve_loop.py:973-986`)。

### 2.3 戦略別の前方成績 (段 2)

**対象と世代。** live の `(name, content_hash, 配備世代)`。世代は `(source ∈ {journal, deploy_op}, op_id)` とし、実際に配備を変えた成功操作だけで進める。journal の `kind IN ('approve','bless') AND phase='decided' AND switch_required=1` と deploy ops の `op_kind=rollback_symlink AND phase=done` から、現在の版に至る最後の遷移と updated_at を取る (`S/store/db.py:508-520,535-552`、`S/plugin/switch.py:1767-1778,2558-2559`)。棚卸しも同じ投影を使う。失敗・取消・revoke・同じ版の bless (`switch_required=0`) では進めない。版を替える bless は再開始する。既存 deploy_generation は全 phase の最大操作を取るため流用しない (`S/plugin/switch.py:2000-2007`)。版変更・A→B→A は再開始し、signal の hash と発生時刻で限定する。配備時刻不明は判定不能。

**窓。** 読取時点から 12 週前まで、現世代の開始以後に限る。採否時刻 `signal_decisions.decided_at` で窓に入れる。週次は読取時点の代わりに週末時刻を使う。実取引の名前単位の行とは集計範囲が違うので世代開始を注記する。

| 系列 (合算しない) | 内容 | 単位 |
|---|---|---|
| 機械採点・全採否 | 現世代の shadow の数値終端 sl / tp / day_close。accept と reject の両方。採否できた signal の、採否時点からの仮想成績 | R |
| 機械採点・採否別 | 同じ母集団を採否で分ける | R |
| 実取引 | チャートの戦略行 (`W/ops/performance_view.py:868-879`) | 口座通貨・R |

**R。** `pnl_pips ÷ (|entry_price − stop_loss| ÷ pip_size)`。SL は signal の payload、他は shadow 行。分母が 0 以下・非有限・SL 欠落は R 不明として除外する。SL 幅の偏りを避けるため pips 合算を使わない。backtest の avg_r (`S/backtest/metrics.py:89-97`) とは無条件に一致しない。

**表示。** N (R 既知)、勝 / 負 / 分、平均 R、PF (R の利益合計 ÷ 損失合計)。損失が無ければ PF は計算不能として表示する。判定は平均 R だけを使う。母数 5 未満は「–」、5〜29 は参考、30 以上は通常表示。30 件 (`S/backtest/metrics.py:20,142`)・28 日は運用上の最低条件で、有意差・独立性の根拠ではない。

率の隣に abandoned (理由別。`S/store/signals.py:477`)、no_fill、data_insufficient、未終端、R 不明を出す。採否別件数、accept のうち risk gate 通過数、signal に対する採否到達率、未終端の最古の経過時間も出す。集中度は採点のある取引日数と日別最大件数の割合だけとする。到達率は同じ発生期間の signal に対する decision ありの割合。現世代内の直近 4 週と前 4 週を表示で比較し、空の期間は比較不能とする。自動判定には使わない。

**経路。** 純関数と読取専用接続で集計する。pips だけを返す `period_summary` (`S/store/signal_shadow.py:316`) を置き換える。web の列を足し、親専有の日次 report の shadow 節も戦略別にする (`S/loops/econ_signal_report.py:552`)。

### 2.4 週次の確定と判定 (段 3)

**境界と確定時刻。** NY 取引日で月〜金の暦週の最後の取引日を market_hours の暦から決める。開場の観測を待たず、平日の休場で週を割らない (`S/core/market_hours.py:16,25-43`)。週の印は `trading_date(close_at − 1 分)` (`S/core/scheduler.py:486-488`)。窓終端はこの close に固定し、確定は close + FINALIZE_GRACE (1 日) 以後。未終端の期限は signal ごとの `day_close_deadline(t0) + FINALIZE_GRACE`、指値は 2 段目の期限を使う (`S/store/ohlcv_slices.py:47`、`S/core/econ_slice_worker.py:774-799`)。期限超えが残っても理由・経過時間を残して確定する。

**起動と再試行。** 独自 tick と専用 alert_state key / prefix を登録し (`S/store/alert_state.py:18-29,64-67`)、起動時・tick で運用開始週から猶予到来済みの週を暦から列挙する。運用開始週は機能の初回起動時に直近の猶予到来済み週として保存する。週記録のない未完了週を、窓終端から 14 日以内は古い順に専用 thread 1 本で処理する。試行回数は「対象週 × UTC 日」で最大 3 回。投入前に alert_state へ回数・一意な試行 ID・実行状態をまとめて確定保存する (`S/store/alert_state.py:46-54`)。例外終了も記録する。起動時は監視・再投入の前に、前プロセスの実行中試行を「中断」に更新する。試行 ID ごとに一度だけ更新し、回数は再加算しない。週が完了確定済みなら失敗扱いせず、§2.5 の回復側へ進む。試行 key は週 × UTC 日の prefix とし、窓終端から 14 日超で消す。同じ週の実行を重ねず、中断回収・初回投入・監視の順に進める。上限到達週は翌日に回し、後続週は処理できる。窓終端から 14 日超の未完了週は打切り記録を確定し、以後再試行しない。日次 hook (`S/core/scheduler.py:450-468,508-530`) は変えず、mission に依らず発注 tick を塞がない。

**保存と状態。** 既存表は変えず、週の記録と集計 2 表を 1 transaction で確定する。処理例外は全体を rollback して再計算し、計算不能として返せる結果は劣化で確定する。

| 記録 | 内容 |
|---|---|
| 週の記録 | 一意な週、窓終端、確定時刻、評価時刻、対象の名前・hash・世代、規則版、確定状態 (完了 / 打切り)、評価状態 (正常 / 劣化 / 未評価)、固定の劣化理由と件数、棚卸し状態 |
| strategy_review_weekly | 週・名前・hash・世代で一意。最初の採否、N、勝敗、R の利益・損失合計、穴・採否・集中度、短期表示、符号と理由。金額なし |
| plugin_inventory_weekly | 週・名前、未使用 / 判定不能、現版の出所、条件、停止提案可否。棚卸しが正常な直前の完了週と比べ、未使用への移行を新規とする。比較元なしは初回一覧で新規扱いしない。初回は指標の通知件数・名前も抽出しない |

確定状態と評価状態は独立に扱う。完了でも scan・journal 読取失敗や戦略の集計不能・配備時刻不明があれば劣化とし、対象と理由を保存して通知する。正常な空集合・標本不足は劣化にしない。統合前の実取引は「未提供」で劣化にせず、統合後の読取失敗は劣化として系列を分ける。打切りは未評価・集計なしとし、対象週と期間超過を report に残す。

戦略対象は窓終端時点の配備記録から、§2.3 の遷移と `disable_symlink done`・`retire_plain done` も使って復元し (`S/store/db.py:535-552`)、signal・採否・abandoned は各時刻で窓終端の as-of に切る。shadow は窓内の採否に属し評価時刻までに終端したものを採点する (`S/store/db.py:183,208`、`S/store/signals.py:477`)。棚卸しは実行時の live scan・保留候補から作り、評価時刻を完了記録に書く。scan・journal 読取失敗は棚卸し全体を判定不能とし、新規の抽出・次週の棚卸し比較元から外す。実取引は読取時の値と注記する。

**判定の方針案 (§6 問 2)。** 主窓は現世代内の最大 12 週、短期 4 週は件数・平均 R の表示だけ。符号の軸は次の表を上から評価する。

| 条件 | 符号 |
|---|---|
| 配備時刻不明・集計不能 | 判定不能 |
| N < 30、または現世代の最初の採否から 28 日未満 | 標本不足 |
| 平均 R < −0.05 | 停止候補 |
| 平均 R の絶対値 ≤ 0.05 | ゼロ近傍 |
| 平均 R > 0.05 | 床超え候補 |

信頼性の要確認理由は「窓終端までの直近 2 週に採否のある signal が 0 件」「評価時刻が期限を超えた未終端」「現世代が 12 週に達しても N < 30」の 3 つ。符号に関わらず併記し、理由別件数と穴の内訳を出す。表示判定は符号を基本に、ゼロ近傍は要確認、床超え候補は理由があれば要確認、無ければ「機械採点上は床超え」とする。通知用の要確認本数はゼロ近傍または理由ありの戦略数で、停止候補との重複を許す。

承認時 holdout は判定に使わず、重なる窓を独立の確認としない。

**report と操作。** `data/improve_reports/` の専用 dir に既存どおり owner-only で書く (`S/loops/econ_signal_report.py:779`)。確定・評価状態と理由、判定、3 系列、穴・到達率・集中度、承認時の成績、未使用と停止済みの一覧を載せる。初回一覧は report だけに出し、人間由来・出所不明の行には「提案対象外」を付ける。書込み失敗は同じ確定から再生成する。停止は既存操作で行い、次の対象から外れる。

### 2.5 通知 (段 3)

- event は `weekly_review_ready`。週は実在日を検証した `ref=weekly:YYYY-MM-DD` に置く。fields は固定語と非負整数 (state=ready / degraded / failed / aborted、停止候補・要確認・停止提案可能・新規の停止提案可能の本数、評価状態、棚卸し状態 (正常 / 判定不能)、固定の劣化理由別件数) のみ。failed / aborted は未完了 / 期間超過の固定理由だけで、本数を省く。名前・成績の数値は入れない。専用の文法検査を置く (`S/ops/events.py:54,85`)。
- ready / degraded の本文は必ず `GET /v1/review/weekly?week=YYYY-MM-DD` で指定週を読む。応答の週が違う・取得不能なら event の状態・固定理由・本数だけに縮退し、劣化の見出しを残す。failed / aborted は詳細を取得せず、週と未完了 / 打切りの固定文だけを出す。詳細は保持しない。
- 中断回収・初回投入後と tick で、確定時刻が直近 14 日以内の週を回収する。完了・正常は通常条件で ready、完了・劣化は必ず degraded、打切りは必ず aborted とする。failed 発行済みの週は、完了すれば通常条件に関わらず回復の ready を 1 回出し、劣化なら degraded も出す。`NOT EXISTS (code, ref, state)` で未発行を選び、emit_best_effort で出す。event 行を発行記録とする既存の方式に従う (`S/ops/events.py:199-211`、`S/ops/service.py:1430-1445`)。
- 未完了の failed は、窓終端から 3 日超・14 日以内で、かつ「その週の試行に例外終了または中断がある」または「その日の試行上限に到達」の場合に 1 回出す。未試行・完了確定済みの週は対象外。回収期間後は report・web・CLI で読む。
- bot は既存の event 配送・cursor 保存・送信 retry 経路 (`A/bot/adapter.py:871-892,971-975`、`A/gate/service.py:153-167`) に接続する。送信失敗では cursor を進めない。成功直後・cursor 保存前の停止による重複は許容する。初回接続は既存どおり末尾からで、過去週は web・CLI・report で読む (`A/gate/state.py:149-172`)。
- 本文の成績は **pf / avg_r / 件数 / 評価不能** の 4 項目だけ。勝率・DD・金額・閾値は載せず、機械採点と明記する。既存通知の本文 2000 文字上限、escape、mention 無効、低優先段落からの省略を継承する (`A/gate/render.py:41,77,88,234`、`A/gate/improve_render.py:4-6`)。
- 通常は停止候補 ≥1 または要確認 ≥1 または新規の停止提案可能 ≥1 で通知する (§6 問 6)。指標の通知件数は未使用かつ現版の出所が staging の行だけ、名前はそのうち新規だけを最大 5 件抽出し、残りは本数にする。human・出所不明は件数にも名前にも入れない。初回一覧は report のみで、通知条件・本文に入れない。failed / aborted に成績・名前は付けない。
- OPS_EVENT_SPECS に既定 True で登録する (`A/gate/render.py:470-477,511-513`)。ops_notices は default / all だけで、止めるには bot 自体を止める (`A/config.py:42,81`)。設定・送信先を増やさず、webhook も同じ fields だけとする。

### 2.6 CLI・API・web

読取 CLI は daemon 不要の `afx plugin list --unused` と `afx plugin review [--week <日付>]`。API は status.read の `GET /v1/review/weekly` (直近 / 指定週)、plugins DTO の `usage_review`、戦略別の機械採点。web は未使用の絞り込み・戦略別の穴・週次表、bot は指定週の本文を読む。
help は `cli_surface.py` から生成する。現在の判定は現配備の name・hash・世代に一致する確定だけから取り、不一致は `-`。指定過去週は当時の対象と表示する。web の停止操作は作らない。

### 2.7 誰に何を見せるか

| 情報 | 人間 | Discord | event・webhook | 改善 agent・他の agent |
|---|---|---|---|---|
| 機械採点 | 全指標 | 4 項目だけ | 出さない | 出さない |
| 採否・穴・到達率・集中度 | 出す | 出さない | 出さない | 出さない |
| 実取引の戦略別成績 | 出す | 出さない | 出さない | 出さない |
| 週次判定・評価状態 | 出す | 停止候補・要確認・劣化・失敗・打切り・回復 | 本数・固定理由・状態だけ | 出さない |
| 未使用の指標 | 名前・条件・提案可否、初回一覧は report | 停止提案可能の本数・新規名だけ | 同じ対象の本数・状態だけ | 出さない |
| 停止済みの名前 | 出す | 出さない | 既存 plugin_disabled のまま | 例外採用時、改善だけ名前・kind |
| 止めた理由・経路、承認・却下の reason | 既存の面 | 出さない | 新規には出さない | 出さない |
| 承認時の成績 | report | 出さない | 出さない | 出さない |

週次表・report は data/ 配下に置き、agent の入力・reflection・RAG・取引 prompt に結ばない。改善 worker は Landlock で data/ を読めない (`S/core/landlock.py:208-227`、`S/mission_worker.py:365-368`)。採点は LLM の reasoning を読まない。

停止名は前方成績に基づく人間の判断の写しとして情報を運ぶ例外である (§6 問 4)。既存の実取引全体集計は §6 問 5 の選択であり、3 段の着手を止めない。情報経路の検証で既存集計に期待する内容だけがその選択で決まる。

### 2.8 停止済みの名前の掲示 (独立)

成功した live scan と完了済みの配備記録を照合し、過去に配備済みで今 live ではない名前だけを出す。scan・journal の読取失敗、未完了の switch / deploy 操作、版不明を停止と誤認しない (`S/plugin/read_model.py:86-134`、`S/store/db.py:518-527,549`)。

kind は検証済み保存版の config から取得し、親側で `{name, kind}` だけに投影する。journal の kind は approve / bless であり plugin の kind ではない (`S/store/db.py:510`)。actor・reason・成績・disable の時刻は context に渡さない。既存の名前文法 (`S/plugin/logic_identity.py:206`) を通し、kind=strategy を先に、各 kind 内で名前の昇順に並べ、50 件・4 KiB に収める。戦略が省略される場合だけ固定文で知らせる。

§6 問 4 の例外を採る場合に限り掲示する。固定文は「停止済みの同名 plugin を自動で再配備しないでください」。同じ仮説の別名候補までは禁じない。人間の rollback で live に戻れば一覧から外れる。週次確定を待たずに実装できる。

## 3. 失敗時・運用

再試行・打切りは §2.4、通知と回復は §2.5 に従う。古い確定には日付と注意を出す。確定済み週は変更せず、表が無ければ未確定を返す。

## 4. 検証と受入

### 追加する検証

1. **棚卸し**: 4 条件と使用中優先、出所・reevaluation の継承、保留候補、legacy / v2、pair 設定に依存しない依存抽出、指標の再提出、休止、評価 / 読取時刻での K 境界。NULL・読めない key の理由別除外件数、字句一致・時刻不明による行単位の判定不能、scan・journal 読取失敗による全体の判定不能を分ける。human・出所不明は提案対象外で通知に含めない。
2. **集計**: R 不明・穴、PF・平均 R、世代の source 分離・A→B→A。版変更の bless は再開始、同じ版の bless・失敗・取消・reverted・needs_retry・foreign・revoke は再開始しない。窓境界・集中度 2 項目。
3. **判定**: 30 件・28 日・±0.05、符号と要確認の全組合せ。signal があっても採否 0 件、12 週経過して N < 30 は符号に関わらず通知し、停止候補も残す。到達率低下・比較不能・短期の負だけでは判定を変えず、配備 30 日でも床超え可能。異なる hash・世代は `-`。
4. **確定と起動**: 猶予、週末 as-of と評価時刻、翌週混入防止、全体 rollback、空集合・標本不足・行単位判定不能と劣化の分離。再試行中の版変更・disable・retire を含む週末対象復元。夏冬時間・平日休場、古い週順の回復、週 × UTC 日の上限・再起動継承、14 日境界・打切り・再試行停止。統合前の実取引は「未提供」で劣化にしない。
5. **通知**: 未試行の初回起動・長期停止直後には failed を出さず、例外終了・中断・上限到達と 3 日・14 日の境界、打切りで aborted を検証する。回数保存後・投入前と、投入後・週の commit 前にプロセスを終了し、翌 UTC 日に再起動する fixture で、中断回収の二重計数なし、3 日超の未完了で failed が 1 回、その後の完了で ready、劣化なら degraded も回収されることを確かめる。停止候補 0 でも劣化は degraded。event 前後・送信失敗・cursor 保存前の停止、未発行回収と 14 日境界、指定週一致、許容重複・初回 cursor、未知 event、fields・ref、本文 4 項目・escape・mention 無効・上限、初回一覧の非通知、名前 5 件・通知不要週。
6. **情報経路**: 週次表・停止 reason・report の canary が改善 context・prompt に出ず、読取経路も無いこと。report dir は guarded data dir の子孫。既存全体集計は問 5 に応じて検証。停止名は名前・kind のみで、scan 失敗・操作途中を除き、strategy 優先の上限と省略文を守る。
7. **CLI**: help の記録、daemon なしの読取、指定週・未確定。

### 受入

1. web で未使用に絞り、3 本の参照元・戦略候補への登場を読取だけで照合する。disable / retire は隔離した fixture で確かめ、本番での停止は条件にしない。
2. 戦略別画面の件数を読取専用 DB と照合し、穴・到達率・集中度・世代を確認する。
3. 実週では通常猶予後の report に初回一覧が出て、一覧単独では通知されないことを確認する。fixture では通知要・不要週、劣化・打切りと未発行回収、翌週を越す再試行、提案対象外の除外、指定週・4 項目・未終端を確かめる。
4. 改善 prompt に週次の数値・判定語がなく、停止名は名前・kind だけであることを確認する。

## 5. 実装の分け方

| 段 | 内容 | 主な場所 | 依存 |
|---|---|---|---|
| 1 | 棚卸し・usage_review・DTO・web・CLI | `S/plugin/read_model.py`、`A/web/plugin_pages.py`、`S/backtest/cli.py`、`S/cli_surface.py` | なし |
| 2 | R・穴・採否の集計、period_summary の置換 | `S/store/signal_shadow.py`、集計 module | なし |
| 2 | 日次 report の戦略別化 | `S/loops/econ_signal_report.py` | 集計だけ |
| 2 | 成績応答・web 列 | `W/ops/performance_view.py` (統合後は S/)、`A/web/` | 集計とチャートの統合 |
| 3 | 週の状態・集計表・判定・原子的保存・report | `S/store/db.py`、新 module | 両集計。実取引系列はチャート統合待ちで、それまでは「未提供」(劣化にしない) |
| 3 | 独自 tick・alert_state key、暦週列挙・週別再試行・打切り・状態監視 | `S/core/scheduler.py`、`S/service.py` | 週次保存 |
| 3 | 指定週の API・CLI・web | `S/ops/`、`S/cli_surface.py`、`A/web/` | 週次保存 |
| 3 | event・未発行回収・bot 配送と本文 | `S/ops/events.py`、`A/gate/` | 週次保存と指定週 API |
| 独立 | 停止名の投影と掲示 | `S/loops/improve_context.py`、`S/loops/prompts/improve_mission.md` | 週次判定への依存なし |

## 6. 利用者に問う事項 (2026-11-20 裁定: 6 件とも推奨どおり)

| # | 問い | 推奨 | 理由 |
|---|---|---|---|
| 1 | 主源。(a) 採否できた signal の機械採点。(b) 実取引 | (a)、正は機械採点上の床超えまで | 実取引は採否・枠・裁量決済、機械採点は欠測・費用の偏りを持つ。両方を並べる |
| 2 | 窓・最低条件・線・正の意味。(a) 最大 12 週、30 件・28 日、±0.05 は要確認、短期 4 週と到達率比較は表示だけ、12 週経過して N < 30 は要確認、正は機械採点上の床超えまで。(b) holdout からの低下で別設計 | (a) | 統計的保証ではない。直近 2 週の採否 0 件・期限超え未終端で床超えを保留する。低頻度・欠測で 12 週たっても 30 件に届かない戦略が無音にならないよう、符号に関わらず要確認として通知する |
| 3 | 未使用。(a) K=8 週、現版が人間由来なら提案除外。(b) K=4 週。(c) live 戦略の参照なしだけ | (a) | 試行期間を取る。休止も暦週に含め明示し、現版の出所不明も除外 |
| 4 | 停止名の掲示。(a) 前方成績に基づく人間の判断の写しとして例外を許す。(b) agent には掲示しない | (a)、例外と記録し名前・kind に限定 | 停止行為も前方情報を運ぶ。同名の自動再配備の抑止に限り、時刻・理由は渡さない |
| 5 | 既存の 90 日の実取引全体成績。(a) 例外として残す。(b) 件数・hold 率だけにする | (a)、前方情報の例外と記録 | 勝率・PF・ペア別損益が既に渡る。3 段の着手は止めず、既存集計の検証の期待値を決める |
| 6 | 通常の通知頻度。(a) 内容のある週だけ。(b) 毎週必ず 1 通 | (a) | 停止候補・要確認は続く限り再掲。劣化・失敗・打切り・回復は §2.5 に従い必ず通知する。初回一覧は report のみ |

## 7. 残余・やらないこと

- **欠測**: abandoned の採点は採否の正規形と shadow の外部キーに触れるため行わない。原因是正・前方 backtest は別課題とする。
- **枠・実帳簿・命名・保有上限**: 関連チケットで扱う。機械採点が正というだけで着手条件を満たしたとは扱わない。仮説の禁止は別の人間方針とする。
- **採点の偏りと誤差**: spread は採否時の 1 点、swap・手数料なし、同時保有の制約なし。30 件・28 日でも独立性・符号の安定は保証しない。停止前には reevaluate で履歴も確かめる手順を運用文書に書く。
- **将来の方向語**: 入れるなら「前方成績による適応を例外として許す」と記録して別設計する。低頻度だから安全とは扱わない。
- **人間による数値の転記**: 防げない。holdout の数値を方針に書かない運用上の注記の対象に週次の数値を足す。

## 付録. 文書とコードの食い違い

| 文書の前提 | 現状・根拠 |
|---|---|
| 帰属・採点は未整備 | decision が signal と建玉を結び shadow を作るが、採否前の abandoned は含まれない (`S/store/db.py:175-208`、`S/store/signal_shadow.py:121-137`) |
| entry はバー終値・固定費用、exit は戦略の規則 | 採否後の足 open / 指値、quote / 仮定 spread、SL / TP / day close。exit_mode は levels のみ (`S/core/paper_fills.py:201-245`、`S/store/signal_shadow.py:80-108`、`S/plugin/loader.py:291-293`) |
| 戦略別表示を ctl status に追加、親 report は既に戦略別 | 現在の shadow は採否種別 × 状態。実取引の戦略行はチャートが提供 (`S/loops/econ_signal_report.py:325-334,552-585`、`W/ops/performance_view.py:868-879`) |
| 配備時の成績しか無い・任意期間も再生可能 | reevaluate と同条件再生はあるが、期間の入口は in_sample / holdout のみ (`S/cli_surface.py:315`、`S/plugin/strategy_gate.py:86-89,226`、`S/backtest/holdout.py:180,216`) |
| 族の表に成績を渡す | 族の入力に成績は無い (`S/loops/improve_context.py:259-266`、`S/loops/prompts/improve_mission.md:31-35`)。方向語は §7 |
| 前方の数値は agent に無い | 90 日の実取引全体集計は既存入力 (`S/loops/improve_context.py:146-179`、§6 問 5) |
| 週次 mission の冒頭なら週 1 回 | mission は手動・繰り返しでも走り、cadence は weekly / daily (`S/loops/improve_loop.py:906`、`S/config.py:460`) |
| 指標・参照・戦略の本数を固定値とする | 本数は live scan と依存から数える (`S/plugin/read_model.py:374-409`)。停止操作は `afx plugin` (`S/cli_surface.py:307-322`) |
| 停止名は改善 prompt に表示済み | 現在は項目が無い (`S/loops/improve_context.py:710-733`、`S/loops/prompts/improve_mission.md:37-42`) |
| period_summary は人間面で使用中 | src に呼出し元が無い (`S/store/signal_shadow.py:316`、`tests/store/test_signal_shadow.py:334`) |
| bless は世代を進めない | 同じ版だけが切替不要。版変更は世代を進める (`S/plugin/switch.py:2558-2559`) |
| 既存 parser で依存名をそのまま抽出 | pair 文法・所属の検査があり、棚卸し専用の読取りが必要 (`S/plugin/logic_identity.py:211-214,345-347,386-395`) |
| 新しい欄名は inventory | 改善の承認済み一覧に同名がある。追加は usage_review (`S/loops/improve_loop.py:973-986`) |
| 実取引の戦略系列は提供済み | `W/ops/performance_view.py:868-879` は未統合で、`S/ops/performance_view.py` は無い |
| 配備対象は approve・rollback だけで復元 | disable_symlink・retire_plain の完了も必要 (`S/store/db.py:535-552`) |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-19 | C0 | 初版 (下書き) | 3 チケットを束ねて設計する | — |
| 2026-11-19 | C0.1 | 判定・週次確定・通知・棚卸しと情報経路を明確化し、3 段に整理 | 設計レビュー 1 周目の指摘を反映 | |
| 2026-11-19 | C0.2 | 判定を 2 軸化し、配備世代・棚卸し・失敗通知を整理、保存と起動を簡素化 | 設計レビュー 2 周目の指摘を反映 | |
| 2026-11-20 | C0.3 | 評価状態・提案対象・週別再試行と通知を明確化し、重複記述を整理 | 設計レビュー 3 周目の指摘を反映 | |
| 2026-11-20 | C0.4 | 配備世代・依存抽出・標本不足・通知を是正し、重複と旧条件を整理 | 設計レビュー 4 周目の指摘を反映 | |
| 2026-11-20 | C0.5 | 中断試行の永続化・起動時回収と失敗・回復通知を明確化し、重複を削減 | 設計レビュー 5 周目の指摘を反映 | |
| 2026-11-20 | C1 | §6 を裁定済みにする | 利用者承認 (6 件とも推奨どおり)。レビューは 6 周 (sol → opus → sol → opus → sol → sol) で収束 | |
