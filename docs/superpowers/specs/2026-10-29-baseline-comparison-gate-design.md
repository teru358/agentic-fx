# 同名の戦略候補は配備中の版と同条件で backtest し、改善していなければ gate で却下する — 設計 C0

**版:** v0.1 C0 (指揮者承認前の清書)
**チケット:** `same-name-candidate-must-beat-deployed-baseline` (中)。`baseline-replay-unimplemented` (2026-09-12、同じ未実装を指す) はこの束で閉じる。
**正本の関係:** 収益性の床は `2026-09-12-profitability-floor-design.md`、遮断 8 の規律は同 §4、通知は `2026-10-25-improve-discord-notice-design.md`、族別要約は `2026-10-24-improve-feedback-structure-design.md`。いずれも変えず、gate に 1 段足す。

## 0. 目的と非目標

**目的:** 配備中の同名 strategy がある候補 (更新) を、同じ mission の中で配備中の版と同条件に並べて backtest し、上回らなければ承認に回さず gate で却下する。比較値は承認画面にも添付する。全体設計 `2026-07-25` §441 の「baseline との比較値を承認依頼に添付」の実装でもある。

**非目標:** 絶対の床 (pf・avg_r・取引数・holdout 評価可能) の変更 / 新名の候補の扱い / 再ロックの扱い / 人間回廊 (`submit` / `bless --from _human`) の判定 / 配備後の前方成績の評価 (`weekly-improve-rebacktests-deployed-strategies`) / 配備中の版の自動停止。

### 0.1 前提の再検討

| チケットの前提 | 確かめたこと | 結論 |
|---|---|---|
| 保存済みの成績と比べれば足りる | 実 DB の配備中 `asym_shock_breakout` (`15a66f12`、mission 2128) は in_sample 期間末 2026-07-26・holdout 64 取引、後継候補 (`01fd0352`、mission 2517) は末 2026-07-29・59 取引。境界は `now` で毎日動くので、保存済みと今日の候補は期間が違う。`variant='baseline'` の行は 0 件 | 保存値との比較は成立しない。同じ `now`・同じ設定で配備中の版を回し直す (ticket b を維持) |
| 同じ mission 内で再 backtest するしかない | 代替は (i) 期間を凍結して保存 (境界が動く以上、人工的な期間を新設することになる)、(ii) 前方成績 (週次評価の領分で、取引数が足りるのは 4〜12 週後) | 同 mission 内の再生が唯一の最小案。週次評価とは目的が違う (下記) |
| weekly-rebacktest と重複する | 同じ履歴の再 backtest は決定論的で新情報が無い、と同チケットが自ら裁定済み。あちらは配備後の前方区間、こちらは候補と同じ区間で並べる比較 | 目的は別。共有できるのは「配備中の版を指定の段で再生して行を残す」関数だけ。依存は作らない |
| 同名更新 = 改善のはず | `plugin-naming-lifecycle-policy` は「同一仮説の改良は同名、意味が変わるものは新名」。gate は意味の違いを見ないので、足や pair を変えた同名更新はここで比べようがない | 比べられない更新は床のみに倒す (§3.4)。新名への誘導は命名方針チケットの領分 |
| 配備中の版を基準にするのは中立 | 配備中の版は過去の holdout を通って選ばれたので、期待値は楽観に偏る (勝者の呪い)。holdout 窓は毎日動くので偏りは部分的に解ける | 候補に不利な保守側の基準。体制変化に追随する更新は gate で通らず、人間の `bless --from _human` が残る (§6) |
| 「上回る」は測れる | holdout は 3 か月で 60 取引前後 (`evaluable` は 30 以上)。pf の差は雑音と区別できない | 差は雑音の域を出ない。gate は「明らかに劣る・同じ」を落とす棘 (ratchet) であり、優位の証明ではない。厚みは承認画面の比較値と人間の判断に残す |
| 予算に入る | `max_backtests_per_candidate` は agent の `run_backtest` RPC の呼び出し上限 (`improve_rpc_tools.py`)。commit 時の gate の backtest (候補の in_sample / holdout) は数えていない | 配備中の版の分も同様に数えない (§3.6) |

## 1. 現物の確認 (コード)

| 項目 | 現物 |
|---|---|
| 同名判定 | `strategy_gate.py` は `inventory.inventory.metas` の同名 strategy の有無を `has_approved_baseline` とする。pin 破れで配備から外れた版は inventory に無く、baseline にならない |
| baseline の中身 | 有るときは `{"ref_plugin_ref","variant":"baseline"}` を返すだけで再生しない。無いときは `no_strategy` の定数行を in_sample に書く |
| 床 | `_check_profitability_floor` を in_sample (不合格なら enforce で holdout を回さず終了)、holdout の順に 2 段。`floor_mode="warn"` は人間回廊 (`bless`) だけ |
| 失敗の処理 | `commit()` の strategy 分岐が `unprofitable` を `_finalize_gate_failed(not_submitted=True, mission_outcome="unprofitable", notice=NOTICE_UNPROFITABLE)` へ流す。agent 面の `last_result` は `observation:not_submitted` |
| 行の保存 | gate は `record_fn=gate_rows.append` に行を積み、終端 tx が `_persist_gate_rows(mission_outcome=...)` で書く。`save_harness_run` は `variant` (`baseline` 可) と `ref_plugin_ref` / `ref_content_hash` を既に受ける。読み手 (`latest_in_sample_metrics`、質検査) は `variant='candidate'` で絞るので baseline 行は混ざらない |
| 再生に要る材料 | 配備中の meta は `ctx.inventory.inventory.by_name(name)`、解決済み依存は `ctx.inventory.resolved[(name, content_hash)]` (`plugin_loader.py` の記述)。候補と同じ `build_intent_source` / `run_in_sample` / `run_holdout_gate` を使える |
| 再ロック | `noop_gate.find_noop_copy` と質検査が `is_relock_transition(candidate_dir, deployed_dir, inventory)` を共有の例外にしている |
| 実測 | worker の CPU は in_sample 約 18〜23 秒、holdout 約 2 秒 (pair ごと、activity `backtest_cpu`、2026-10-29 mission 2517 / 2518 / 2520)。壁時計は未確認 |

## 2. 利用者から見た動き

**却下されたとき。** 週次・手動の mission で候補が床を通り、同名の配備中の版を上回らなかった場合:

- mission は `completed`、承認依頼は作らない。`data/improve_reports/improve-<日付>-<mission>.md` に `reason: not_improved` と節「Baseline comparison detail」が残る。節は段 (in_sample / holdout) と pair ごとの候補と配備中の版の 8 指標、判定の列 (`improved` / `not_improved` / `skipped`)、配備中の版の hash 12 桁。親専有で agent は読めない。
- activity: `IMPROVE gate_failed mission=<id> reason=not_improved` と、比較を行った全 mission に `IMPROVE baseline_compare mission=<id> plugin=<name> result=improved|not_improved|skipped detail=<固定語>`。数値は書かない。
- backlog の `last_result` は他の非提出と同じ `observation:not_submitted`、`origin_outcome` も `not_submitted`。
- Discord の終了通知: 見出し「配備中の版を上回らず見送り」、本文は固定の 1 行「同じ期間・同じ設定で再評価した配備中の版と比べ、改善を確認できなかった」。数値は載せない (§8 J5)。

**agent に見えるもの。** 変わらない。失敗は `observation:not_submitted`、族別要約でも `not_submitted` (床・取引数不足と同じ畳み方、`improve_context._OUTCOME_PROJECTION`)。段・pair・数値・差分・「baseline」の語は注入面、RPC 返却、mission 出力のどこにも出さない (遮断 8)。prompt の床の節に 1 文だけ足す: 「同名で更新する候補は、配備中の版を上回ることが採用の条件である」。段・指標・閾値は書かない。

**承認画面。** 通ったとき、承認依頼の payload `baseline` に比較値が付く (§4)。CLI・対話シェル・web の承認詳細は、既存の in_sample / holdout の表の下に「配備中の版 (同条件で再評価)」の同形の表と、判定 (`改善` / `比較不能 (理由)`) を出す。人間は差の大きさを見て承認を決める。

## 3. 規則

### 3.1 対象

次をすべて満たす候補に比較を課す: 経路が改善 mission の commit gate (週次・手動単発・手動繰り返しの区別なし、同じ `_run_strategy_gate`) / kind=strategy / 配備中の同名 strategy が `ctx.inventory` に在る / 再ロック (`is_relock_transition`) でない / 候補が床を通った。比較は床の **後**: 床で落ちる候補は従来どおり `unprofitable` で、配備中の版を回さない (費用を払わず、床の理由も変わらない)。

### 3.2 比較条件

配備中の版は、候補と同じ gate 呼び出しの中で、同じ `now`・`settings`・`dataset` (period、source、base interval)・eval source・holdout 分割 (`run_in_sample` / `run_holdout_gate` が期間を所有する)・pair で回す。保存済みの行は使わない。比較する pair は、候補の宣言 pair と配備中の版の宣言 pair の積集合。足 (`timeframe`) が違う、または積集合が空のときは比較しない (§3.4)。配備中の版の identity は mission 開始時の `ctx.inventory` から取る (比較中に配備が動く場合は承認側の CAS が受ける、§6)。

### 3.3 改善の判定 (候補の形)

pair p、段 s ごとに候補の指標 `c` と配備中の版の指標 `b` から `improved(s, p)` を決める純関数 `compare_metrics(b, c)` を 1 つ置く。`pf is None` (損失ゼロ) は +∞ として順序づけ、両方 None は等しい (改善でない)。推奨する形 (§8 J1):

- 決定段は holdout。比較できる全 pair で、pf と avg_r が共に配備中の版を **厳密に** 上回る (差の下限 0)。
- in_sample は比較値を記録・添付するが判定に使わない。
- 取引数は判定に使わない (選別して減るのは許す)。ただし両側の holdout が `evaluable` (30 取引以上) の pair だけが比較対象。

holdout がどの pair も比較できないとき、in_sample (合計取引 30 以上の側) を決定段に繰り上げる。どの段も比較できなければ §3.4。

### 3.4 比較できないとき

比較不能の種類: 配備中の版の holdout / in_sample が評価不能 (取引数不足) / 足の不一致 / pair の積集合が空 / 配備中の版の再生が plugin 起因で失敗 (`SandboxError` の `plugin_error` 系。語彙は実装時に確認)。扱いの推奨 (§8 J2): **床のみで通し、`baseline_compare result=skipped detail=<固定語>` を activity に、payload の比較に `status=unavailable` と理由を残す**。`sandbox_unavailable` は環境の失敗なので既存の経路 (staging を残し mission を failed) に流す。

### 3.5 対象外

再ロック (pin だけの更新) は比較しない (`noop_gate` と同じ `is_relock_transition` を使う)。新名の候補 (配備中の同名なし) は従来どおり `no_strategy` との比較行を書き、床のみ。人間回廊 (`switch.py` の `submit_candidate` / `bless_candidate`) は `compare_mode="off"` で従来のまま: 人間が自分の判断で上書きする経路を残す。

### 3.6 backtest 予算

候補ごとの上限 `max_backtests_per_candidate` (既定 6) は agent の tool 呼び出しの上限で、gate の再生は数えていない。配備中の版の再生も数えず、config の検証 (`>= len(pairs)`) も変えない (推奨、§8 J3)。追加費用は mission あたり、比較 pair 数 × (in_sample 約 20 秒 + holdout 約 2 秒) の worker CPU。床で落ちる候補にはかからない。

## 4. 配線

| 場所 | 変更 |
|---|---|
| `strategy_gate.evaluate_strategy_adoption_gate` | 引数 `compare_mode: "enforce"\|"off"` (既定 `off`)。床の判定の後・`baseline_row` を作る前に、`has_approved_baseline` かつ `enforce` かつ床を通ったときだけ配備中の版を pair ごとに in_sample → holdout の順で回す。行は `record_fn` へ、候補の行と同じ identity で `plugin_ref=plugins/<name>`、`content_hash=<配備中>`、`variant="baseline"`、`ref_plugin_ref` / `ref_content_hash=<候補>` に差し替えて渡す (`no_strategy` の複製と同じ流儀)。`StrategyGateVerdict` に `comparison: dict \| None` と `not_improved: bool` を足す。`cpu_samples` と `evaluation_sink` は配備中の版の評価も通す (scope は `baseline_in_sample` / `baseline_holdout`) |
| `improve_loop._run_strategy_gate` | `compare_mode="enforce"` を渡す (`floor_mode` と同じく呼び出し元が明示)。再ロック判定は `_deployed_dir_for` と同じ `is_relock_transition` を使い、再ロックなら `off` |
| `improve_loop.commit` | `floor_reason` の分岐の次に `not_improved` 分岐。`_finalize_gate_failed(reason="not_improved", mission_outcome="not_improved", candidate_outcome="gate_failed", not_submitted=True, notice=NOTICE_NOT_IMPROVED, report_detail=_render_comparison_report_detail(...))`。報告の見出しは引数化 (床は従来のまま)。通ったときは `gate_metrics["baseline"]` に比較値 (下) を載せる |
| payload `baseline` | 既存の `ref_plugin_ref` / `variant` を残し、`ref_content_hash` と `comparison: {status: "improved"\|"unavailable", detail, stages: {in_sample: {pair: 8 指標}, holdout: {...}}}` を足す。旧い承認行は `comparison` を持たず、表示側は無しとして扱う |
| `backtest_runs` | `variant='baseline'` の行が、候補の `mission_outcome` と同じ値 (`approval` / `not_improved`) で保存される。`candidate_archives.outcome_class` は既存の `gate_failed` を使い、DDL は変えない (§6) |
| 通知の固定語 | `improve_notice_codes.py` に `NOTICE_NOT_IMPROVED = ("gate_failed", "not_improved")` を追加し、専用の見出しを持つ集合に入れる。閉じた辞書の消費側 (`ops/events.py`、`ops/service.py`、`afx-ops` の `improve_render` / `improve_events`) を grep 全数で同期する。`floor` の数値行は `unprofitable` のときだけ描く既存条件のまま |
| 承認詳細 | `ops/service.approval_detail` が `payload.baseline` の比較値を返し、CLI の formatter・対話シェル・web の `DETAIL_FIELDS` が既存の in_sample / holdout と同じ描画で出す |

**変えないもの:** 床の規則と閾値・順序 / `floor_mode` と人間回廊 / 遮断 8 の注入面と `last_result` の語彙 / `candidate_archives` の DDL / 質検査 (`duplicate_metrics` 等。比較の後に従来どおり走る) / `max_backtests_per_candidate` と validator / config のキー (新設なし) / `NOTICE_COMPARISON_BASE_UNAVAILABLE` (質検査の語。別物) / 週次 cadence・repeat の失敗規則 (`not_improved` は `gate_failed` と同じく失敗に数えない mission の終端)。

## 5. 受入条件

観測可能な形で (実 DB の例は 2026-10-29 時点):

1. **同条件の証拠。** 同名更新の mission で `backtest_runs` に、同じ `mission_id` の `variant='baseline'` 行が pair ごとに in_sample と holdout の 2 行でき、`period_start` / `period_end` / `source` / `base_interval` / `timeframe` / `settings_hash` / `core_commit` が候補の行と一致する。`content_hash` は配備中の版、`ref_content_hash` は候補。例: `asym_shock_breakout` 更新なら `15a66f12` の行が `01fd0352` の行と同じ期間 (2025-05-06〜、境界は mission の `now - 3 か月`) で並ぶ。
2. **同じ指標で却下。** 候補が配備中の版と同一コード (同じ metrics) のとき `gate_failed reason=not_improved` が activity に出て、承認依頼は増えず、`improvement_backlog.last_result='observation:not_submitted'`、`improve_run_notices` が `(gate_failed, not_improved)`、レポートに節「Baseline comparison detail」が在る。
3. **上回る候補は通る。** holdout の pf と avg_r が共に上回る合成候補で承認依頼ができ、`payload.baseline.comparison.status='improved'`、`stages.holdout` に両側の 8 指標が入る。承認詳細 (CLI・web) に比較の表が出る。
4. **床が先。** 床で落ちる同名候補は従来の `unprofitable` で、`variant='baseline'` の行は 0 行。
5. **対象外。** 再ロックだけの更新・新名・人間回廊では `baseline` 行が増えない (新名は従来の `no_strategy`)。
6. **比較不能。** 配備中の版の holdout が 30 取引未満の構成で床のみで通り、`baseline_compare result=skipped` が出て `comparison.status='unavailable'`。
7. **遮断 8。** 注入面 (prompt 全文、`last_result`、`origin_outcome`、族別要約、RPC 返却、mission 出力) に、数値 canary・段名・`baseline`・`not_improved` が現れない。Discord と activity にも数値が出ない。変異: 判定を `>=` にすると 2. が落ちる、床の前に回すと 4. が落ちる、行の variant を `candidate` にすると質検査が baseline 行を拾って 1. が落ちる。
8. **予算。** 候補の `backtest_call_count` と `max_backtests_per_candidate` の消費は、比較の有無で変わらない。

## 6. 残余

- 1 bit の拡張: 承認 / 非承認の結果が配備中の版との差の 1 bit を運ぶ (§8 J4)。再評価の契機は、改善 loop が同名更新で holdout 寄りに体系的に収束する挙動の観測 (床の R5 と同じ)。
- 体制変化への追随: 配備中の holdout が良い間は、同じ仮説の適応更新は通らない。人間が `bless --from _human` で上書きする (比較は走らない)。
- 雑音: 60 取引前後の pf 差は有意でない。厳密な `>` は雑音で通ることがある。有意性の検定は入れない。
- 比較中に配備が変わる場合: 基準は mission 開始時の inventory。承認時の食い違いは `approval-base-identity-cas` の領分 (未確認: 実装の現況)。
- `candidate_archives.outcome_class` が `gate_failed` に畳まれ、比較不合格は archive 上で他の gate 不合格と区別できない (族別要約は元から区別しない)。区別が要るなら 14 値の CHECK の table rebuild (`_migrate_candidate_archives` の前例) が要る。
- commit gate の壁時計: 配備中の版の再生で gate の所要がおよそ 2 倍。gate や repeat の時間上限との相互作用は未確認。
- 比較条件の同一性は同じ `now` を使う前提。replay の決定論は `holdout` / `run_replay` の契約に依り、同一入力での再現は未確認 (T1 で同じ候補を 2 回回す test で確かめる)。
- 周辺: `weekly-improve-rebacktests-deployed-strategies` が再生関数を共有するかは、あちらの設計時に決める。

## 7. 変更一覧と task 分割

| task | 内容 | 担当 | 行数 (src / test) |
|---|---|---|---|
| T1 | `strategy_gate`: `compare_metrics` (純関数) / 配備中の版の再生 / `compare_mode` / verdict の拡張 / `cpu_samples` と sink。決定論・同条件・各比較不能・変異の test | codex (terra) | 約 150 / 300 |
| T2 | `improve_loop`: `not_improved` 分岐、`_finalize_gate_failed` の見出し引数化、`_render_comparison_report_detail`、payload `baseline`、activity、prompt の 1 文、再ロックと `off` の振り分け | codex (terra) | 約 90 / 200 |
| T3 | `improve_notice_codes` と消費側の全数同期 (`ops/events.py`、`ops/service.py`、`afx-ops` の render / events)、承認詳細の比較表 (service・CLI・shell・web)、Discord の固定見出し | codex (terra) | 約 90 / 150 |
| T4 | 実 worker・実 DB 相当の結合試験 (同名更新の mission 通し、AC 1〜8、遮断 8 の canary、床が先) と変異 | sonnet | / 約 200 |

T1 → T2 → T3、T4 は T2 後。レビューは codex 1 周目 → `/code-review high`。境界は遮断 8 と資金に触れないが、注入面の漏洩検査は最終 diff にローカル LLM 3 本を使う候補。

## 8. 裁定 (利用者判断が要るもの)

技術で決まるものは含めない。以下は採用基準の厳しさ・停止の許容・安全 pin の拡張で、製品方針に属する。

**J1 改善の定義。** どの段・どの指標をどれだけ上回れば「改善」か。推奨: holdout の pf と avg_r が共に厳密に上回る (差の下限 0)、in_sample は添付のみ。根拠: in_sample は agent が最適化した窓で、そこでの優位は汎化を示さない。holdout は期間が毎日動き、agent は数値を見られない。pf だけだと損益の大きさを見落とし、avg_r だけだと勝率偏重を見落とす。代案: B = in_sample が悪化していないことも要求 (実例: `15a66f12` → `01fd0352` は保存値で in_sample pf 1.0229 → 1.0165、holdout 1.0476 → 1.0709。B なら 3 日分の窓の違いだけで落ちうる)。C = 差の下限を pf +0.05 など正にする (雑音の通過を減らすが、基準値は根拠が無く実測を要する)。なぜ利用者判断か: 厳しいほど改善が止まり、緩いほど雑音が通る。値は運用実績が無い。

**J2 配備中の版が評価不能のとき。** 推奨: 床のみで通し、承認画面に「比較不能」と理由を出す。根拠: 評価不能 (取引数不足・足の不一致・再生失敗) は候補の責任でなく、却下すると最も改善が要る plugin の更新を止める。人間承認が後に控える。代案: 却下 (fail closed)。なぜ利用者判断か: 「承認に回さない」を守ると改善が止まり、止めないと承認に回る。

**J3 予算。** 推奨: 候補ごとの `max_backtests_per_candidate` に含めない。根拠: これは agent の試行回数の上限で、gate の再生は元から数えていない。追加は mission あたり比較 pair 数 × 約 22 秒の CPU と、gate の所要の増加。代案: gate の再生にも別の上限を置く。なぜ利用者判断か: 所要と CPU の受容 (本番ペーパー機の負荷) は運用判断。

**J4 遮断 8 のただし書きの拡張。** 床のただし書きは「baseline との差分は不可」で、集約 1 bit (`unprofitable`) だけを許した。本設計は agent 面に新しい語を出さないが、承認 / 非承認という結果が配備中の版との差の 1 bit を運ぶ。反復すれば、配備中の版の holdout の水準を二分探索で推定できる原理的な経路になる。推奨: 受容し、床のただし書きと同じ脅威モデルとして追記する。根拠: 方針 (自動却下) の帰結で、床の 1 bit と同様、agent の観測は「承認か非提出か」のままで語彙は増えない。再評価の契機は §6。なぜ利用者判断か: 安全 pin の例外の追加。

**J5 Discord に比較の数値を出すか。** 推奨: 出さない (固定見出しと固定 1 行のみ)。数値は親専有レポートと、通ったときの承認画面にある。根拠: 2026-10-25 の裁定は床未達の 4 項目だけを許可しており、配備中の版の holdout 数値は新しい露出になる。運用者が方針に書き写す数値を増やさない。代案: 床と同じ形 (落ちた段・pair の 4 項目) で候補と配備中の版を並べる。なぜ利用者判断か: 2026-10-25 の裁定の延長。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-29 | v0.1 C0 | 初版 (指揮者承認前の清書) | チケット `same-name-candidate-must-beat-deployed-baseline` の設計化 | |
