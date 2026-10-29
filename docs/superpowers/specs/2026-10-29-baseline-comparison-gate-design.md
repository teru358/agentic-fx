# 同名の戦略候補は配備中の版と同条件で backtest し、改善していなければ gate で却下する — 設計 C0

**版:** v0.3.1 C0 (sol 2 周目の全件反映、指揮者承認前の清書)
**チケット:** `same-name-candidate-must-beat-deployed-baseline` (中)。`baseline-replay-unimplemented` (2026-09-12、同じ未実装を指す) はこの束で閉じる。
**正本の関係:** 収益性の床は `2026-09-12-profitability-floor-design.md`、遮断 8 の規律は同 §4、通知は `2026-10-25-improve-discord-notice-design.md`、族別要約は `2026-10-24-improve-feedback-structure-design.md`。いずれも変えず、gate に 1 段足す。

## 0. 目的と非目標

**目的:** 配備中の同名 strategy がある候補 (更新) を、同じ mission の中で配備中の版と同条件に並べて backtest し、上回らなければ承認に回さず gate で却下する。比較値は承認画面にも添付する。全体設計 `2026-07-25` §441 の「baseline との比較値を承認依頼に添付」の実装でもある。

**運用形:** 設定 `improve.baseline_gate: shadow | enforce` (既定は §8 J6)。shadow は比較を記録 (DB・親専有レポート・承認画面) するが却下しない。enforce で却下する。mode は mission 開始時に settings から snapshot し、mission 内で不変 (§3.7)。

**非目標:** 絶対の床 (pf・avg_r・取引数・holdout 評価可能) の変更 / 新名の候補の扱い / 再ロックの扱い / 人間回廊 (`submit` / `bless --from _human`) の判定 / 配備後の前方成績の評価 (`weekly-improve-rebacktests-deployed-strategies`) / 配備中の版の自動停止。

### 0.1 前提の再検討

| チケットの前提 | 確かめたこと | 結論 |
|---|---|---|
| 保存済みの成績と比べれば足りる | 実 DB の配備中 `asym_shock_breakout` (`15a66f12`、mission 2128) は in_sample 期間末 2026-07-26・holdout 64 取引、後継候補 (`01fd0352`、mission 2517) は末 2026-07-29・59 取引。境界は `now` で毎日動くので、保存済みと今日の候補は期間が違う。`variant='baseline'` の行は 0 件 | 保存値との比較は成立しない。同じ `now`・同じ設定で配備中の版を回し直す (ticket b を維持) |
| 同じ mission 内で再 backtest するしかない | 代替は (i) 期間を凍結して保存 (境界が動く以上、人工的な期間を新設することになる)、(ii) 前方成績 (週次評価の領分で、取引数が足りるのは 4〜12 週後) | 同 mission 内の再生が唯一の最小案。週次評価とは目的が違う (下記) |
| weekly-rebacktest と重複する | 同じ履歴の再 backtest は決定論的で新情報が無い、と同チケットが自ら裁定済み。あちらは配備後の前方区間、こちらは候補と同じ区間で並べる比較 | 目的は別。共有できるのは「配備中の版を指定の段で再生して行を残す」関数だけ。依存は作らない |
| 同名更新 = 改善のはず | `plugin-naming-lifecycle-policy` は「同一仮説の改良は同名、意味が変わるものは新名」。足 (`timeframe`) や `pairs` は候補が自分で変えられる。比べられないからと床のみで通すと、変えるだけで gate を回避できる (sol 1 周目 Critical) | 足・pair を変えた同名更新は比較不能ではなく不正 (`same_name_contract_changed`、§3.4)。意味が変わる変更は新名で提出する |
| 配備中の版を基準にするのは中立 | 配備中の版は過去の holdout を通って選ばれたので、期待値は楽観に偏る (勝者の呪い)。直近配備版の旧 holdout と新 holdout は約 87/90 日重なり、偏りはほぼ解けない | 候補に不利な保守側の基準。体制変化に追随する更新は gate で通らず、人間の `bless --from _human` が残る (§6) |
| 「上回る」は測れる | holdout は 3 か月で 60 取引前後 (`evaluable` は 30 以上)。pf の差は雑音と区別できない | gate は **両指標 (pf・avg_r) の点推定の優越を要求する保守的な ratchet** で、優位の証明でも「明らかな劣後だけの排除」でもない。60 取引前後では雑音による偽棄却が大きく、それを意図的に受容する。勝者の呪いは窓の重なりでほぼ残る。このため shadow 段階を置き、通過率を測ってから enforce にする (§3.7、AC 9)。厚みは承認画面の比較値と人間の判断に残す |
| 予算に入る | `max_backtests_per_candidate` は agent の `run_backtest` RPC の呼び出し上限 (`improve_rpc_tools.py`)。commit 時の gate の backtest (候補の in_sample / holdout) は数えていない | 配備中の版の分も同様に数えない (§3.6) |


**実例 (2026-10-29、旧版を同じ条件で手動 backtest)**: `asym_shock_breakout` の承認 #39 → #73 は、承認依頼の保存値では holdout pf 1.048 → 1.071 と改善に見えたが、同じ期間で回し直すと旧版と新版の結果が全桁一致した (in_sample 714 取引 pf 1.0194、holdout 61 取引 pf 1.0535)。新版が足した EMA20 filter は挙動を変えておらず、保存値の差は評価日のずれによるデータの動きだった。一方 `close_pressure_followthrough` #71 → #72 は同条件でも holdout pf 1.145 → 1.209、avg_r 0.066 → 0.106 (取引 38 → 93、max_dd 2.3% → 5.7%) で改善が残った。保存値同士の比較では前者を見抜けず、同一 mission 内の再生が要ることの根拠とする。

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

**却下されたとき (enforce)。** 週次・手動単発・手動 repeat の mission で候補が床を通り、同名の配備中の版を上回らなかった場合:

- mission は `completed`、承認依頼は作らない。`data/improve_reports/improve-<日付>-<mission>.md` に `reason: not_improved` と節「Baseline comparison detail」が残る。節は段・pair ごとの候補と配備中の版の 8 指標と判定、配備中の版の hash 12 桁。親専有で agent は読めない。
- activity: `IMPROVE gate_failed mission=<id> reason=not_improved` と、比較を行った全 mission に `IMPROVE baseline_compare mission=<id> plugin=<name> mode=shadow|enforce result=improved|not_improved|unavailable detail=<固定語> candidate=<hash 先頭 12 桁> base=<hash 先頭 12 桁>`。数値は書かない。hash は数値でなく遮断 8 に抵触しない。
- backlog の `last_result` / `origin_outcome` は他の非提出と同じ `observation:not_submitted` / `not_submitted`。Discord 終了通知: 見出し「配備中の版を上回らず見送り」、本文は固定 1 行 (数値なし、§8 J5 は裁定済み)。
- **shadow のとき** は比較を同じ形で記録し (行・レポート節・承認画面・activity の `mode=shadow`)、`not_improved` でも却下せず従来どおり承認へ進める。

**同名更新として不正なとき。** 候補の `timeframe` か `pairs` が配備中の版と違う場合は、mode に関わらず、床より常に優先して `same_name_contract_changed` で fail closed (床未達でも同じ outcome。candidate の backtest 行は 0 行)。agent 面は `observation:not_submitted` に畳む。親専有レポートに「意味が変わる変更は新しい名前で提出する」と書く (`plugin-naming-lifecycle-policy` と整合)。

**agent に見えるもの。** 変わらない。段・pair・数値・差分・「baseline」の語は注入面、RPC 返却、mission 出力に出さない (遮断 8)。prompt の床の節に 1 文だけ足す: 「同名で更新する候補は、配備中の版を上回ることが採用の条件である」(足・pair を変えない旨は書かない: 語彙を増やさない)。

**承認画面。** 通ったとき、payload の `baseline` に比較値が付く (§4)。CLI・対話シェル・web は既存の in_sample / holdout の表の下に「配備中の版 (同条件で再評価)」の同形の表と判定 (`改善` / `比較不能 (理由)`、shadow では `未改善 (観測のみ)` も) を出す。

## 3. 規則

### 3.1 対象

次をすべて満たす候補に比較を課す: 改善 mission の commit gate (週次・手動単発・手動 repeat、同じ `_run_strategy_gate`) / kind=strategy / 配備中の同名 strategy が `ctx.inventory` に在る / 再ロック (`is_relock_transition`) でない / 候補が床を通った。比較は床の **後**: 床で落ちる候補は従来どおり `unprofitable` で、配備中の版を回さない。契約検査 (§3.4) は床の前で、床より常に優先する。

### 3.2 比較条件

配備中の版は、候補と同じ gate 呼び出しの中で、同じ `now`・`settings`・`dataset`・eval source・holdout 分割 (`run_in_sample` / `run_holdout_gate` が期間を所有)・pair で回す。保存済みの行は使わない。pair 集合は候補と配備中の版で **完全一致** (§3.4)。配備中の版の identity は mission 開始時の `ctx.inventory` から取り、payload に `base_content_hash` として載せる。

### 3.3 改善の判定 (保守的な ratchet)

pair p、段 s ごとに純関数 `compare_metrics(b, c)` で `improved(s, p)` を決める。`pf is None` (損失ゼロ) は +∞、両方 None は等しい (改善でない)。推奨 (§8 J1):

- 決定段は holdout。全 pair で、pf と avg_r が共に配備中の版を **厳密に** 上回る (差の下限 0)。avg_r は損益幅を含む期待値。
- in_sample は記録・添付のみで判定に使わない。取引数は判定に使わない。ただし両側の holdout が `evaluable` (30 取引以上) の pair だけが比較対象で、いずれか 1 pair でも非 evaluable なら `unavailable`。
- 性質: 点推定の優越を両指標に要求するため、60 取引前後では雑音による偽棄却が大きい。意図的な受容。窓が約 87/90 日重なるので勝者の呪いもほぼ残る。有意性は主張しない。

### 3.4 契約検査と比較不能

**同名更新として不正 (候補が制御できる)。** `timeframe` の不一致、または pair 集合の不一致 (部分集合・上位集合を含む完全一致でない場合) は、`same_name_contract_changed` で fail closed (`mission_outcome` もこの固定語、agent 面は `not_submitted`)。部分積集合で強い pair を外して勝つ回避を塞ぐ。

**比較不能 `unavailable` (候補が制御できない)。** (a) 配備中の版の holdout が非 evaluable (標本不足)、(b) 配備中の版の再生不能 (`baseline_unhealthy`)。holdout に比較可能 pair が無いとき in_sample への繰り上げはしない (agent が最適化した窓のため)。扱い (§8 J2 の推奨): 床のみで通し、activity `result=unavailable detail=<固定語>` と payload の `verdict=unavailable` + `reason` を残す。

**再生失敗の対応表。** 現物の `SandboxError.code` を全数列挙する (`src/agentic_fx/plugin/sandbox.py` の `code=` 生成箇所と既定値): `plugin_error` (884・919・1013・1071・1324 行ほか)、`crashed` (1021・1059・1088・1500・1548・1726・1743 行ほか)、`protocol_error` (1554 行)、`sandbox_unavailable` (1051 行)、`timeout` (1505 行)、`cpu_limit` (1748 行。worker が SIGKILL され CPU 秒が上限付近のとき動的に生成される実装済み code)、既定値 `backtest_failed` (129 行のコンストラクタ既定。AST 検査・import・hash・入力と戻り値の検証など、code を明示しない全 raise)。

| code | 区分 | 扱い |
|---|---|---|
| `sandbox_unavailable` | 環境起因 | mission を retryable failed (既存の staging 保持経路)。比較結果は作らない |
| `plugin_error` / `crashed` / `protocol_error` | baseline artifact 起因 | `baseline_unhealthy` → `unavailable` (J2) |
| `backtest_failed` (既定 code) | baseline artifact 起因 (AST・hash・入力・戻り値の検証) | `baseline_unhealthy` → `unavailable` |
| `timeout` / `cpu_limit` | baseline の資源超過 | `baseline_unhealthy` → `unavailable` |
| 上記以外 (未知) | 不明 | fail closed: mission を failed (比較を捏造しない) |

対応表は上の 7 code で閉じる。新しい code が sandbox に足されると未知として fail closed になり、表の更新が要る。

**途中行の切り戻し (全破棄の境界)。** baseline 再生の前に `gate_rows` の長さを checkpoint する。再生が失敗 (上表で `unavailable` または failed) したら、その位置以降の `variant=baseline` 行だけを切り戻す。候補の floor 行 (checkpoint より前) は保持し、candidate は完全な pair × 段が残る。理由: 一部 pair だけの baseline 行は「同条件の対」(AC 1) を満たさず、片側だけの行は読み手に誤った比較を見せる。`gate_rows` は evaluator がメモリ上で積み、終端 tx が `_persist_gate_rows` で書く。baseline 行・候補行・approval payload の `comparison.verdict=unavailable`・mission 終端は同じ終端 tx で保存し、payload の `unavailable` のうち再生失敗 (`baseline_unhealthy` / `gate_timeout` / mission failed) は baseline 行 0 行と対応し、再生に成功したが baseline holdout が非 evaluable (標本不足) の場合は完全な baseline 行を保持したまま `verdict=unavailable` と対応する (AC 6 はこの二つを分けて検査する)。

### 3.5 対象外

再ロックは比較しない (`is_relock_transition`)。新名の候補は従来どおり `no_strategy` 行を書き床のみ。人間回廊 (`submit_candidate` / `bless_candidate`) は `compare_mode="off"`: 人間が自分の判断で上書きする経路を残す。

### 3.6 backtest 予算と壁時計

`max_backtests_per_candidate` (既定 6) は agent の RPC 回数で、gate の再生は数えず、config の検証も変えない (§8 J3)。追加の CPU は mission あたり pair 数 × (in_sample 約 20 秒 + holdout 約 2 秒)。

**壁時計の上限は二分する。** (1) 候補の既存 floor は従来どおり完了または失敗させ、壁時計で打ち切らない (打ち切ると絶対床を迂回できるため)。候補を含む総上限は設けない。(2) 設定 `improve.baseline_replay_wall_clock_max_sec` は **床通過直後に開始** し、配備中の版の再生だけに掛かる。既定 300 秒 (最悪 pair 数 6 × (in_sample + holdout の実測 約 22 秒) × 2 = 264 を丸めた値。pair 数の上限は実装時に settings から確定し、T1 の壁時計実測で見直す)。超過は再生を取消し `baseline_unhealthy` (reason `gate_timeout`) として `unavailable` に倒す (§3.4 の切り戻しを適用し、床のみで通す)。候補の floor 実行中は timer が動かないので、床の途中で `unavailable` になることはない。

**repeat の期限。** 2 回目以降の mission は `remaining >= mission_timeout_sec + baseline_replay_wall_clock_max_sec + cleanup_margin` (cleanup_margin は 60 秒) のときだけ始め、満たさなければ `deadline` で終える。1 回目は現行どおり期限を見ない (ctl-improve-repeat 設計 §3.1 の裁定)。この条件は同設計 v0.5.3 §3.1 の改訂を要する (§7、改訂は指揮者)。

### 3.7 shadow から enforce へ

既定 shadow (§8 J6) で、`not_improved` も却下せず記録する。**mode は mission 開始時に settings から snapshot し、mission 内で不変**。設定の反映契機は daemon 再起動 (現行の settings 読み込み。ホットリロードは別チケット `settings-hot-reload-safe-keys`)。repeat は daemon 再起動で中断されるので、実質 1 job 内でも不変。activity の各行に `mode` を載せる。enforce への切替前に次を測る (AC 9): 比較件数、通過率、pf と avg_r の差分分布、窓重複率。

**窓重複率の定義。** 配備中の版が採用されたときの holdout 期間 (その承認 payload / `backtest_runs` から引く) と、今回の再生 holdout 期間の、日数の重複率。同一 mission 内の候補・配備中の再生は同じ期間なので、その対は測らない。採用時の期間が取得不能なら `null` とし集計から除外する。偽棄却が許容を超えるなら J1 の定義 (C: 差の下限を正に) を再裁定する。

## 4. 配線

| 場所 | 変更 |
|---|---|
| `strategy_gate.evaluate_strategy_adoption_gate` | 引数 `compare_mode: "off"\|"shadow"\|"enforce"` (既定 `off`)。契約検査 (§3.4) を床の前に。床の後・`baseline_row` を作る前に、`has_approved_baseline` かつ mode が `shadow` / `enforce` のとき配備中の版を pair ごとに in_sample → holdout で回す。行は `record_fn` へ、`plugin_ref=plugins/<name>`、`content_hash=<配備中>`、`variant="baseline"`、`ref_plugin_ref` / `ref_content_hash=<候補の content_hash>` で渡す。verdict に `comparison` と `not_improved` を足す (shadow では `not_improved` は却下に使わない)。`cpu_samples` / `evaluation_sink` は配備中の版も通す (scope `baseline_in_sample` / `baseline_holdout`)。床通過直後に `baseline_replay_wall_clock_max_sec` の timer を開始し、再生だけを打ち切る (§3.6)。再生前に `gate_rows` の長さを checkpoint、失敗時は baseline 行だけ切り戻す (§3.4) |
| `improve_loop._run_strategy_gate` / `commit` | mode を mission 開始時の settings snapshot から渡す。再ロックなら `off`。`enforce` かつ `not_improved` は `_finalize_gate_failed(reason="not_improved", mission_outcome="not_improved", candidate_outcome="gate_failed", not_submitted=True, notice=NOTICE_NOT_IMPROVED, ...)`。`same_name_contract_changed` も同形 (固定語)。`SandboxError` は §3.4 の表 (7 code + 未知) で振り分ける。通ったとき `gate_metrics["baseline"]` に比較値を載せる |
| payload `baseline` | 既存の `ref_plugin_ref` / `variant` に加え `base_content_hash` (配備中の版) と `comparison`。旧承認行は `comparison` 無しとして扱う |
| `backtest_runs` | `variant='baseline'` 行は候補の `mission_outcome` と同じ値で保存。`ref_content_hash` は候補の `content_hash`。`candidate_archives.outcome_class` は `gate_failed`、DDL は変えない |
| 通知・承認詳細 | `improve_notice_codes.py` に `NOTICE_NOT_IMPROVED` と `same_name_contract_changed` の固定語。閉じた辞書の消費側 (`ops/events.py`、`ops/service.py`、`afx-ops` の `improve_render` / `improve_events`) を grep 全数で同期。承認詳細 (`approval_detail`、CLI、shell、web `DETAIL_FIELDS`) が比較表を出す |

**payload の JSON schema (逐語)。**

```
comparison = {
  "mode": "shadow|enforce",
  "verdict": "improved|not_improved|unavailable",
  "reason": "<固定語>",
  "stages": {
    "holdout":   {"<pair>": {"candidate": {<8 指標>}, "deployed": {<8 指標>}, "verdict": "improved|not_improved|unavailable"}},
    "in_sample": {"<pair>": {"candidate": {<8 指標>}, "deployed": {<8 指標>}, "verdict": "improved|not_improved|unavailable"}}
  }
}
```

**監視集計器 (T3、親専有レポート) の出力 schema。** 入力は親専有 DB (`backtest_runs` の baseline 行と candidate 行、approval payload の `comparison`)。数値は親専有レポートにだけ出し、activity と Discord には出さない。

```
{"strategy": "<name>", "attempts": <int>, "by_candidate_hash": [{"candidate": "<12 桁>", "base": "<12 桁>", "verdict": "...", "monotone_run": <int>}],
 "not_submitted_share": <float>, "unavailable_rate": <float>, "window_overlap": {"mean": <float|null>, "n": <int>}}
```

`monotone_run` は同じ strategy・同じ配備中の版に対する候補系列で、指標が単調に動き続けた長さ (遮断 8 の独立例外の監視、§6)。

**identity の役割。** payload の `base_content_hash` は「比較に使った配備中の版」の identity。DB の baseline 行の `ref_content_hash` は「比較された候補」の identity。`approval-base-identity-cas` は配備の identity を守る (承認時の CAS)、本書は比較の identity を残すだけで、CAS を代替しない。CAS が未実装の間は、比較の `base_content_hash` を承認詳細に出し、承認時に配備中の hash と食い違えば表示で警告する (§6)。

**変えないもの:** 床の規則・閾値・順序 / `floor_mode` と人間回廊 / 遮断 8 の注入面と `last_result` の語彙 / `candidate_archives` の DDL / 質検査 / `max_backtests_per_candidate` / `NOTICE_COMPARISON_BASE_UNAVAILABLE` (別物) / repeat の失敗規則: `not_improved` は連続失敗に数えず backoff も付けない (AC 7 で pin)。

## 5. 受入条件

1. **同条件の証拠 (SQL)。** 同名更新の mission で、`plugin_ref='plugins/<name>' AND variant='candidate'` かつ scope / pair が一致する候補行と、`variant='baseline'` 行が pair × 段で対になり、`created_at`・`period_start` / `period_end`・`source` / `base_interval` / `timeframe`・`settings_hash`・`core_commit` が一致する (`plugins/_staging/...` の重複行は対象外)。baseline の `ref_content_hash` から候補行が一意に引け、`content_hash` は配備中の版。
2. **同じ指標で却下 (enforce)。** 同一コードの候補で `gate_failed reason=not_improved`、承認依頼は増えず、`last_result='observation:not_submitted'`、`improve_run_notices` が `(gate_failed, not_improved)`、レポートに節が在る。`candidate_archives.outcome_class='gate_failed'` と baseline 行の `mission_outcome='not_improved'` が完全一致して観測される。
3. **`compare_metrics` の境界表。** pf None / 双方 None / 片方の指標だけ等値 / 複数 pair の一つだけ不合格 / 非 evaluable。変異: `>` を `>=`、`and` を `or`、None を 0 扱い、がそれぞれ落ちる。上回る合成候補は承認依頼ができ、`comparison.verdict='improved'`、`stages.holdout` が schema どおり。
4. **床が先・契約が最優先。** 床で落ちる候補は `unprofitable`、baseline 行 0。契約変更かつ床未達の候補は `same_name_contract_changed` (床より優先)、candidate の backtest 行も 0 行。
5. **対象外。** 再ロック・新名・人間回廊は baseline 行が増えない。
6. **契約と比較不能。** `timeframe` または pair 集合を変えた同名候補は `same_name_contract_changed`、承認依頼なし、agent 面は `not_submitted`。baseline の holdout が 30 取引未満は床のみで通り `verdict=unavailable`。`SandboxError` は列挙 literal (§3.4 の 7 code) に加え、**既定 code (`backtest_failed`、AST 違反の baseline artifact で実生成) と CPU 上限 (`cpu_limit`、実 worker の SIGKILL で実生成)** を通し、表どおりに振り分ける (parametrize)。未知 code は failed。再生失敗時は baseline 0 行・candidate は完全な pair × 段で、payload `comparison.verdict=unavailable` と対応する。
7. **3 入口の mission 通し。** 週次・手動単発・手動 repeat のそれぞれで、同名更新の mission が実 worker / 実 DB で AC 1・2 を満たす。repeat では `not_improved` が連続失敗数を増やさず backoff なしで次の mission に進む。
8. **遮断 8 と監視。** 注入面 (prompt 全文、`last_result`、`origin_outcome`、族別要約、RPC 返却、mission 出力) に数値 canary・段名・`baseline`・`not_improved`・`same_name_contract_changed` が現れない。Discord と activity にも数値が出ない (hash 12 桁と `verdict` は固定語の行に出る)。変異: 床の前に回すと 4 が落ちる、行の variant を `candidate` にすると 1 が落ちる。監視: 集計器 (§4 の出力 schema) が親専有 DB から strategy / hash ごとの試行回数、単調な候補系列、`not_submitted` の集中を計算し、activity の hash 付き行と件数が一致する。
9. **shadow・mode 固定・壁時計。** shadow では `not_improved` でも承認依頼ができ、行・レポート・activity (`mode=shadow`) は enforce と同形。**shadow で始めた mission は途中で settings が変わっても enforce にならない** (mission 開始時 snapshot、反映は daemon 再起動)。enforce 切替前の測定値 (通過率・差分分布・窓重複率、取得不能は `null` で除外) が集計できる。壁時計: baseline の再生が `baseline_replay_wall_clock_max_sec` を超えると取消され `gate_timeout` で `unavailable` (床のみ)、**候補の floor 実行中は timeout が `unavailable` にならない** (timer は床通過後に開始)。repeat は 2 回目以降、`remaining < mission_timeout_sec + baseline_replay_wall_clock_max_sec + 60 秒` なら次の mission を始めず `deadline` で終わり、1 回目は期限を見ない (実時刻の例で pin、60 秒 repeat を含む)。
10. **予算。** 候補の `backtest_call_count` と `max_backtests_per_candidate` の消費は比較の有無で変わらない。
11. **表示の各面。** CLI (`afx ctl approval show`)・対話 shell・web の各面で、candidate / deployed の 8 指標・verdict・`base_content_hash` が出る。旧 payload (`comparison` 無し) は各面で後方互換 (表を出さず落ちない)。Discord は固定見出しのみ (数値なし)。閉じた辞書 (`improve_notice_codes.py`) の全消費側 (`ops/events.py`、`ops/service.py`、`afx-ops` の `improve_render` / `improve_events`、web `DETAIL_FIELDS`) を grep で列挙し、新語が全てに在ることを test で確かめる。

## 6. 残余

- 統計: block bootstrap 等で劣後側の信頼境界を使う案は入れない (残余)。両指標の点推定の優越は偽棄却が大きい。shadow の測定で見て、必要なら J1 を C に再裁定する。
- 遮断 8 の独立例外 (§8 J4): 相対 oracle は絶対床より強い。固定された配備中の版の未知の holdout 水準を、候補を単調に変えて推定できる経路。再評価の契機は AC 8 の監視指標 (比較試行回数、単調系列、`not_submitted` の集中)。
- 体制変化への追随: 配備中の holdout が良い間は適応更新が通らない。人間は `bless --from _human` で上書きする。
- `approval-base-identity-cas` との関係: 比較中に配備が変わると、候補は古い版に勝っただけになりうる。本書は `base_content_hash` を残して表示で警告するにとどめ、挿入直前・承認時の再照合 (CAS) は同チケットの実装に依存する (未確認: 現況)。
- `candidate_archives.outcome_class` が `gate_failed` に畳まれ、比較不合格は archive 上で他と区別できない。区別が要るなら 14 値の CHECK の table rebuild (`_migrate_candidate_archives` の前例)。
- replay の決定論は `holdout` / `run_replay` の契約に依る。T1 で同じ候補を 2 回回す test で確かめる。
- 周辺: `weekly-improve-rebacktests-deployed-strategies` が再生関数を共有するかはあちらの設計時に決める。

## 7. 変更一覧と task 分割

| task | 内容 | 担当 | 行数 (src / test) |
|---|---|---|---|
| T1 | `strategy_gate`: `compare_metrics` / 契約検査 / 配備中の版の再生 / `compare_mode` 3 値 / 再生失敗の対応表 / 壁時計上限 / sink。境界表・決定論・変異の test | codex (terra) | 約 200 / 350 |
| T2 | `improve_loop`: 分岐、`_finalize_gate_failed`、レポート節、payload `baseline`、activity 固定語 (hash 12 桁・`mode`)、prompt の 1 文、`improve.baseline_gate` / `baseline_replay_wall_clock_max_sec` の settings (example と migration)、mode の mission 開始時 snapshot、repeat の残り時間規則 (実装) | codex (terra) | 約 150 / 250 |
| T3 | 通知固定語と消費側の全数同期、承認詳細の比較表 (service・CLI・shell・web)、監視集計器 (親専有レポート、§4 の schema) | codex (terra) | 約 90 / 150 |
| T4 | 実 worker・実 DB 相当の結合試験 (3 入口の通し、AC 1〜11、canary) と変異。4 群に分ける: 入口配線 (AC 1・2・7)、永続化 (AC 4〜6)、表示 (AC 8・11)、期限・mode (AC 9・10) | sonnet | / 約 250 |

依存は T1 → T2 → T3 → T4。timer の開始点・checkpoint・例外分類は T1 が所有する。ctl-improve-repeat 設計 v0.5.3 §3.1 の改訂 (repeat の期限条件、§3.6) は指揮者が行う。レビューは codex 1 周目 → `/code-review high`。注入面の漏洩検査は最終 diff にローカル LLM 3 本を使う候補。

## 8. 裁定 (利用者判断が要るもの)

技術で決まるものは含めない。J5 は 2026-10-25 の裁定 (数値は Discord に出さない) で固定済みのため本節から外した (§2)。

**J1 改善の定義と保守的 ratchet の採否。** 本規則は優位の証明ではなく、両指標の点推定の優越を要求する保守的な ratchet で、60 取引前後では偽棄却が大きく、勝者の呪いは窓の重なりでほぼ残る。これを理解したうえで採用するか。推奨: 採用し、holdout の pf と avg_r が共に厳密に上回る (差の下限 0)、in_sample は添付のみ。根拠: in_sample は agent が最適化した窓。avg_r も損益幅を含む期待値なので 2 指標で足りる。代案 B = in_sample の悪化なしも要求 (`15a66f12` → `01fd0352` は in_sample pf 1.0229 → 1.0165 で落ちうる)、C = 差の下限を正に (基準値の根拠が無く実測要)。利用者判断の理由: 厳しいほど改善が止まる。

**J2 baseline の標本不足・再生不能のとき。** (足・pair の変更は §3.4 で不正と確定、ここには入らない。) 推奨: 床のみで通し、承認画面に「比較不能」と理由を出す。再生不能は artifact 不良と環境故障を §3.4 の表で分類済み (環境起因は failed)。根拠: 候補が制御できない事象で、却下すると最も改善が要る plugin の更新を止める。人間承認が後に控える。代案: 却下 (fail closed)。

**J3 baseline 再生の壁時計上限。** `max_backtests_per_candidate` は agent RPC 回数で、gate の再生を含めない (既存契約)。推奨: 配備中の版の再生だけに `baseline_replay_wall_clock_max_sec` を設ける、既定 300 秒 (式は §3.6、T1 の実測で見直す)。候補の床は打ち切らず、総上限も置かない。利用者判断の理由: 本番ペーパー機の負荷と mission の所要の受容。

**J4 遮断 8 の独立例外。** 「絶対床より強い相対 oracle」として独立に記録する。推奨: 監視条件 (hash と親専有 DB の集計器による比較試行回数、単調系列、`not_submitted` の集中、AC 8) つきで受容。理由: 安全 pin の例外の追加で、床の 1 bit の同型ではない。

**J6 shadow の期間。** 既定を shadow にして何週間観測してから enforce に切り替えるか。推奨: 既定 shadow。自動で enforce にはせず、最低 paired 比較件数・対象 strategy 数・unavailable 率 (閾値は実測後に利用者が決める) を満たしたうえで、2 週間 (週次 2 周 + 手動分) を目安に人間が settings で切り替える (反映は daemon 再起動)。J1 の推奨も、2 週間だけでは標本数を保証しない点を踏まえる。理由: 偽棄却の大きさは実測前に決められない。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-29 | v0.1 C0 | 初版 (指揮者承認前の清書) | チケット `same-name-candidate-must-beat-deployed-baseline` の設計化 | |
| 2026-10-29 | v0.2 C0 | sol 1 周目の全件反映: 契約変更の不正化・shadow 段階・SandboxError 対応表・payload schema・壁時計上限・3 入口 AC・J4 独立化・J5 外し・J6 新設 | sol 1 周目 (Critical 1 / Important 8 / Minor 2) | |
| 2026-10-29 | v0.3 C0 | sol 2 周目の全件反映: 壁時計を baseline 再生専用に二分 (候補の床は打ち切らない)・repeat 期限式・SandboxError 全 code 閉じ (`backtest_failed`、`cpu_limit` 実装済み)・checkpoint 切り戻し・mode の mission 開始時 snapshot・activity に hash と集計器 schema・表示面の AC 11・窓重複率の再定義・契約検査の優先・J3 / J6 文言 | sol 2 周目 (Critical 1 / Important 7 / Minor 1) | |
| 2026-10-29 | v0.3.1 C0 | `unavailable` の 2 種 (再生失敗 = baseline 0 行 / 非 evaluable = 完全な baseline 行を保持) を §3.4 と AC 6 で分ける | 設計レビュー 3 周目 (sol、Important 1) | |
