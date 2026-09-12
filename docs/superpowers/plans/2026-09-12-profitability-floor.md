# 収益性フロア + reject reason 漏洩 実装プラン v1.1 (設計書 = `2026-09-12-profitability-floor-design.md` v1.1 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (推奨) または superpowers:executing-plans で task ごとに実行すること。

**Goal:** 設計書 v1 の T0 (人間 reject reason のプロンプト漏洩の是正) / T1 (収益性フロアを
決定論ゲートに 2 段で課す) / T2 (RPC 返却ヒント + プロンプト規律) / T3 (config 3 キー) を
実装する。**設計を変えない** — ユーザー裁定 U1〜U5 と codex 設計レビュー 1 周目 18 件の
全件採用は設計書 v1.1 §2 に確定記録済みなので
「裁定待ち」は無い。設計書に無い判断が必要になったら実装を止めて指揮者へ申告すること
(設計書 §9 の R1 に該当する範囲は本束では触らない)。

**Architecture:** 1 worktree・直列。T0 → T3 → T1 → T2 の順 (T0 はフロアのラベル規律を
設計書 §4 のただし書きとして固定する前提作業。T3 の config がないと T1 の判定関数が
書けない。T2 は T1 の `_check_profitability_floor` を再利用するので最後)。

触るファイル:
- `src/agentic_fx/store/backlog.py` / `store/approvals.py` / `commands.py` (T0)
- `src/agentic_fx/config.py` / `config/settings.yaml.example` + `config/settings.yaml` (T3)
- `src/agentic_fx/plugin/strategy_gate.py` / `plugin/approval.py` / `plugin/switch.py` /
  `loops/improve_loop.py` / `backtest/cli.py` (T1)
- `src/agentic_fx/loops/improve_loop.py` (`_build_rpc_handlers`) / `loops/prompts/improve_mission.md` (T2)
- `tests/loops/test_floor_leak_guard.py` (**新規**、Landlock skip の無い漏洩 pin モジュール)
- `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` §7.1-1 ⑧ (T0、ただし書き追記)

決定論的コアの判定ロジックのうち **Risk Gate・発注・SL 変更・クローズ・資金保護は全 task で
不変**。本束が触るのは「採用判断 (approval を作るか)」だけ。

**Tech Stack:** Python 3.13 / uv / pytest / sqlite3。**schema 変更なし**
(`backtest_runs.mission_outcome` は `TEXT` で CHECK 制約が無いため新値 `unprofitable` に
migration 不要 — `store/db.py:299,406` で確認済)。

## Global Constraints

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p` (サブスク認証) のみ
- 発注・SL 変更・クローズ・資金保護は本プランの対象外 (触らない)
- **drawdown kill switch の無効化不可という制約とは別物** — 本束の config 3 キーは
  「採用判断の品質基準」であり緩められてよい (設計書 §6 T3)
- `config/settings.yaml` (gitignore) と `config/settings.yaml.example` は**必ず両方**同期する
- `plugins/`・`data/`・`logs/` はコミット対象外のまま (本プランは `src/` / `tests/` /
  `docs/` / `config/*.example` のみ変更)
- **実 DB (`data/agentic.db`) を書き換えないこと**。設計書 §1.3 / U5 の除染 SQL は
  **ユーザーが実行する** — 実装者は SQL を提示するだけ
- **ラベルの固定文言規律**: `improvement_backlog.last_result` に書くフロア不合格の文字列は
  `unprofitable` の**完全一致のみ**。pair 名・数値・段名 (in_sample/holdout)・
  「holdout」の語・段名を**絶対に混ぜない** (設計書 §4)。数値は親専有レポートと
  `backtest_runs` にだけ出す
- **`mission_outcome=None` の gate 行を新たに作らないこと** (設計書 §3 T1-f)。
  人間 corridor も含め、全ての gate 行に明示 outcome を付ける
- **tuple 拡張をしないこと** — 戻り値の拡張は named result 型 (`GateOutcome`) で行う。
  `bless_candidate` の戻り値は `int` のまま (tests 11 箇所を壊さない、設計書 §3 T1-d)

---

## T0: 人間 reject reason のプロンプト漏洩 [reject-reason-leak]

**対応**: 設計書 §5。本束の前提作業 (フロアのラベル規律を設計書に固定する)

### Step 0-1: 固定文言化

**変更**:
- `src/agentic_fx/store/backlog.py:111`: `"rejected": ("observation", "rejected:{reason}")`
  → `("observation", "rejected_by_human")`
- `src/agentic_fx/store/approvals.py::apply_decision` docstring: `rejected` 経路が
  `reason` を **backlog へは渡さない** (固定文言になる) ことを明記。引数 `reason` は
  `approval_requests.reason` 列へは従来どおり書かれる (削らない)

**接合面**: `backlog.apply_approval_outcome` の `"{reason}" in template` 分岐は固定文言側を
通るため**コード変更不要**。`approved` 経路 (`reason=str(approval_id)` → `approved:<id>`)
と `expired`/`invalidated` (定数) は一切変えない。

### Step 0-2: 人間向け導線

**変更**:
- `src/agentic_fx/commands.py::_approval_detail` (`:345-366`): 出力行に
  `reason=<approval_requests.reason または '-'>` / `decided_by` / `decided_at` を足す
  (`_archive_line` の直前)。`SELECT` を `kind, status, payload_json` から
  `kind, status, payload_json, reason, decided_by, decided_at` に拡張
- **同じ task で** payload の `floor_warning` / `floor_detail` / `profitability_floor` の
  表示行も足す (codex I4/I5 — CLI が「詳細は `afx> approval <id>`」と案内するのに
  読めない状態を作らない。T1 Step 1-8 と対になる。T1 より先に入れる場合はキー欠落時に
  行を出さない fail-soft で書く)

**理由 (必須項目)**: これが無いと、T0 の後に人間の却下理由がシェルから読めない
write-only 情報になる (現行 `_approval_detail` は `reason` を表示していない)。

### Step 0-3: 設計書へのただし書き追記

**変更**:
- `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` §7.1-1 の ⑧ の
  直後に、設計書 (本束) §4 の「ただし書き」ブロックを逐語で追記する。
  **内容を要約・言い換えしないこと** — 遮断 8 の解釈を変える追記なので逐語が正

### Step 0-4: テスト

**変更**:
- `tests/store/test_backlog.py:197`: パラメータ `("selected","rejected","observation","rejected:")`
  → `("selected","rejected","observation","rejected_by_human")` + assert を
  `startswith` から**完全一致**へ
- `tests/store/test_approvals.py:150`: `== "rejected:not useful"` → `== "rejected_by_human"`
- **`tests/loops/test_floor_leak_guard.py` (新規)**: F5-1 / F5-3 / F5-4 / F5-5 をここに置く。
  **`tests/integration/test_improve_forbidden_regression.py` には置かない** — 同ファイルは
  全体に Landlock skip marker (`:62-63`) があり、Landlock 非対応環境で純粋な
  store / rendering の漏洩 pin まで全 skip される (codex I12)。
  **レンダ済みプロンプト全文** (`_render_improve_mission_prompt` の戻り値) を対象に
  `assert "holdout" not in rendered` 等の形で書く — DB 行の assert で代替しない

**完了条件**:
- [ ] pin F5-1: 人間が `reject <id> "holdout pf 0.80 で負け"` した後、次 mission の
      レンダ済みプロンプト全文に `"holdout"` / `"0.80"` / 理由文字列が現れない
- [ ] pin F5-3: `approval_requests.reason` には理由が残っている
- [ ] pin F5-4: `afx> approval <id>` の出力に `reason=` が現れる
- [ ] pin F5-5: `_history_table` が `last_result` をレンダする唯一の経路
      (`_backlog_table` がレンダし始めたら落ちる assert)
- [ ] 段0 変異 red: `_OUTCOME_TABLE['rejected']` を `rejected:{reason}` に戻す →
      F5-1 が落ちる
- [ ] 設計書 §7.1-1 ⑧ への追記が入っている (逐語)
- [ ] **除染 SQL をユーザーへ提示** (実行はしない):
      `UPDATE improvement_backlog SET last_result='rejected_by_human' WHERE last_result LIKE 'rejected:%';`
- [ ] フルスイート green

---

## T3: 収益性フロアの閾値を config に出す [floor-config]

**対応**: 設計書 §6 T3。T1 より先に入れる (T1 の判定関数が参照する)

**変更**:
- `src/agentic_fx/config.py::ImproveGateSettings` (`:226-228`) に 3 キーを追加:
  - `min_pf: float = Field(ge=0.0, default=1.0)`
  - `require_positive_avg_r: bool = True`
  - `require_holdout_evaluable: bool = False`
- `src/agentic_fx/config.py::Settings` に **新規 `model_validator(mode="after")`**:
  `improve.tool_budget.max_backtests_per_candidate >= len(pairs)` (codex I10)。
  違反は `ValueError` (fail closed)。plugin の `pairs` は `Settings.pairs` の部分集合に
  制限されるので、この上限で「宣言全 pair を 1 回ずつ確認する枠」を保証できる。
  境界 (`==`) は通す
- `config/settings.yaml.example` (`:95-97` の `improve.gate`) に 3 キーをコメント付きで追記
- `config/settings.yaml` (gitignore) に同じ 3 キーを追記 (**必ず両方**)

**接合面**: `ImproveGateSettings` は `_Strict` 継承なので未知キーは拒否される →
`settings.yaml` 側だけに書くと起動しない / `config.py` 側だけだと既定値で動く。
**両方同期** が完了条件。

**執筆時の申し送り**: `settings_snapshot_hash` (`store/backtest_runs.py:315-320`) は
`{"risk":…, "backtest":…}` しか含まないため `improve.gate` を変えても
`backtest_runs.settings_hash` は変わらない。**ここを直さない** (既存の全 hash が変わり
§A の dedup 母集団と既存行の identity に波及する)。代わりに
**成功側 = approval payload の `profitability_floor` snapshot** (T1 Step 1-8、codex I3)、
**不合格側 = レポート本文 + activity** (T1 Step 1-3) で監査痕跡を作る。

**予算の根拠 (codex I11 による訂正)**: v1 の「パラメータを変えた候補を別名で書けば枠は別」
は**撤回**する — 候補ごと上限を名前変更で回避する運用を公式に勧める形になる。基本は
「**1 候補を最大 `max_backtests_per_candidate` 回まで編集して再試験する**」(同名候補の編集)。
複数候補を作る場合は mission 全体の `max_tool_calls=300` が律速で、別途の評価は範囲外。

**完了条件**:
- [ ] pin F8-1: 既定値 `min_pf == 1.0` / `require_positive_avg_r is True` /
      `require_holdout_evaluable is False`
- [ ] pin F8-2: `settings.yaml.example` と `ImproveGateSettings` のキー集合一致
      (既存の同期テストがあればそれに乗る。無ければ新設)
- [ ] pin F8-3: `min_pf` に負値 → `ValidationError`
- [ ] **pin F8-5: `max_backtests_per_candidate < len(pairs)` の settings が `ValidationError`**
      (境界 `==` は通る)
- [ ] pin F8-6: approval payload の `profitability_floor` が 3 生成箇所すべてに入り、
      非既定値 (`min_pf=1.5`) がそのまま載る (T1 Step 1-8 と対。T3 単独では
      config の読み出しまで)
- [ ] 段0 変異 red: 既定 `min_pf` を 0.0 にする変異 → F8-1 / validator を外す変異 → F8-5
- [ ] フルスイート green

---

## T1: 収益性フロアを決定論ゲートに課す [profitability-floor]

**対応**: 設計書 §3 / §7。本束の本体。**task 内分散**: Step 1-1〜1-3 (ゲート本体) と
Step 1-8 (テスト転写) を並列執筆してよいが、統合 + red/green 実行は 1 レーン直列。
Step 1-4〜1-7 (人間 corridor) は接合面が広いので 1 レーン直列。

### Step 1-1: 判定関数 (`_check_profitability_floor`)

**変更**: `src/agentic_fx/plugin/strategy_gate.py` に新設
```
def _check_profitability_floor(per_pair: dict[str, dict], *, settings,
                               scope: str) -> tuple[str, str]:
    """戻り値 (label, detail)。label は "" (合格) か "unprofitable" (固定)。
    detail は人間向けレポート専用の全数値文字列 — label と混ぜない。"""
```
判定は設計書 §3「判定規則 (逐語)」のとおり。**pair ごとに判定し 1 pair でも不合格なら
候補全体が不合格**。`scope` は `"in_sample"` / `"holdout"`。

**順序が契約 (設計書 §3 の ①→②→③)**:
1. `scope == "holdout"` かつ `require_holdout_evaluable` かつ `not evaluable` → **FAIL**
   (**`trades == 0` でも FAIL**。この判定を zero-trade shortcut の後に置くと設定名どおりの
   厳格化にならない — codex I9。pin F2-5(b))
2. `trades == 0` → `continue` (成績が無い pair)
3. `scope == "holdout"` かつ `not evaluable` → `continue` (既定: R8)
4. `pf is not None and pf < min_pf` → FAIL / `require_positive_avg_r and avg_r is not None
   and avg_r <= 0.0` → FAIL

**落とし穴**:
- `pf is None` (`gross_loss == 0`) は **PASS**。素で `pf < min_pf` を書くと `TypeError`
- `pf == min_pf` は PASS (`<`)。`avg_r == 0.0` は FAIL
- **`max_drawdown` / `kill_switch_latches` を判定式に入れない** (U3、pin F10-1)

### Step 1-2: verdict と `floor_mode` の 2 段構成

**変更**: `plugin/strategy_gate.py`
- `StrategyGateVerdict` に `floor_reason: str = ""` / `floor_detail: str = ""` を追加。
  **`evaluable` は流用しない** (`evaluable=False` の R8 意味論を `plugin/approval.py:216-221`
  と `loops/improve_loop.py:2179` が読んでいる)
- `evaluate_strategy_adoption_gate(..., floor_mode: Literal["enforce","warn"] = "enforce")`
- in-sample ループ → `total_trades` 判定 (既存) の**直後**に in_sample 段を呼ぶ
  - `floor_mode == "enforce"` で不合格 → `StrategyGateVerdict(evaluable=True,
    floor_reason="unprofitable", floor_detail=…, candidate_metrics=per_pair)` を即 return
    (**holdout ループに入らない**、pin F1-8)
  - `floor_mode == "warn"` で不合格 → **`floor_reason` を立てたまま holdout ループと
    baseline / no_strategy 分岐まで完走する** (codex C2、pin F6-3)。`floor_detail` が
    約束する「in_sample + holdout 全数値」はここで初めて生成できる
- holdout ループの `run_holdout(...)` の**返り値を pair ごとに捕捉**
  (`holdout_per_pair[pair] = run_holdout(...)`。現行は捨てている `:160-171`)。
  ループ後に holdout 段を呼び、不合格なら `floor_reason` を立てる
  (`warn` で in_sample も落ちていた場合は `floor_detail` に両段を含める)

### Step 1-3: 改善ループ側の再ルート + branch-local outcome

**変更**: `src/agentic_fx/loops/improve_loop.py`
- `if not strategy_verdict.evaluable:` (`:2179`) の**直後**に:
  ```
  if strategy_verdict.floor_reason:
      self._finalize_gate_failed(
          conn, ctx=ctx, backlog_id=selection.backlog_id,
          reason="unprofitable",                  # 固定文言
          now=now, gate_rows=tuple(gate_rows), tool_calls=tool_calls,
          mission_outcome="unprofitable",
          report_detail=strategy_verdict.floor_detail)
      return
  ```
  改善ループは常に `floor_mode="enforce"` (既定) で呼ぶ。
- `_finalize_gate_failed` (`:2689`) に 2 引数を追加し、**outcome を branch-local に 1 つ決めて
  3 箇所すべてへ渡す** (codex I6 — 現行は gate 行が引数、ledger が `"gate_failed"` 固定、
  settle が `"gate_failed"` 固定で分裂しうる):
  - `mission_outcome: str = "gate_failed"` / `report_detail: str = ""`
  - **正常分岐**: `outcome = mission_outcome` を
    `_persist_ledger_in_tx(mission_outcome=outcome)` (`:2764` 付近) /
    `_persist_gate_rows(mission_outcome=outcome)` (`:2769`) /
    `_settle_ledger_after_commit(outcome=outcome)` (`:2783`) の **3 箇所**へ
  - **report 作成失敗分岐** (`:2735-2739` 付近の OSError 経路): `outcome = "report_failed"`
    に固定し、**引数 `mission_outcome` で上書きしない** (pin F4-10)
  - 帰結: archive INDEX の `status` もフロア経路では `unprofitable` になる
- `report_detail` をレポート本文 (`body_md`) の後段に書く。入れるもの (pin F4-4):
  in_sample / holdout の pair ごとの
  `trades / pf / win_rate / avg_r / max_drawdown / total_pnl / kill_switch_latches / evaluable`、
  落ちた段、落ちた pair、**適用した閾値**
- `activity` の `gate_failed` 行: `reason=` は引数 `reason` (= `unprofitable` 固定) のまま +
  適用閾値を同じ行に足す。**`report_detail` の全数値は activity に書かない**

**接合面の注意**: `report_detail` は **`last_result` に一切流さない**
(`last_result` は `reason` 逐語 = `unprofitable` のみ。pin F2-7 / F5-2)。

### Step 1-4: named result 型 `GateOutcome` (codex I4/I5)

**変更**: `src/agentic_fx/plugin/approval.py`
```
@dataclass(frozen=True)
class GateOutcome:
    metrics: dict
    evaluable: bool
    floor_warning: str = ""            # "" | "unprofitable"
    floor_detail: str = ""
    gate_rows: tuple[dict, ...] = ()   # Step 1-6 の sink
```
- `run_kind_gate(..., floor_mode: Literal["enforce","warn"] = "enforce",
  record_fn: Callable[[dict], None] | None = None) -> GateOutcome`
  (戻り値を `tuple[dict, bool]` から変更。src の呼び出し元は `switch.py:779` のみ)
  - `enforce` で `verdict.floor_reason` → `ValueError(f"plugin {meta.name!r}: unprofitable
    (min_pf=… require_positive_avg_r=… require_holdout_evaluable=…)")`
  - `warn` → 続行し `floor_warning` / `floor_detail` に載せる
  - indicator / signal は `_validate_kind` に委譲 (`floor_warning=""`)
- `switch._run_full_gate(..., floor_mode="enforce")` の戻り値を
  **`(meta, after_content, after_artifact, outcome: GateOutcome)` の 4 要素**に固定
  (呼び出し元 `switch.py:818`, `:1470` を追随)。**tuple をこれ以上伸ばさない**

**接合面の注意**: v1 の spec/plan は要素数が食い違っていた (3 要素 / 4 要素 / 6 要素 / 7 要素)。
**4 要素 + named 型が唯一の契約**。

### Step 1-5: `bless_candidate` は `int` 戻りを維持 (codex I5)

**変更**: `src/agentic_fx/plugin/switch.py::bless_candidate`
- `floor_mode="warn"` で `_run_full_gate` を呼ぶ
- **戻り値は `int` (approval_id) のまま変えない**。新設キーワード
  `on_floor_warning: Callable[[str, str], None] | None = None` (引数 = `(label, detail)`) を
  追加し、`floor_warning` が立っていたら呼ぶ (`None` なら何もしない)
- payload に `"floor_warning": outcome.floor_warning` / `"floor_detail": outcome.floor_detail`
  / `"profitability_floor": {…}` を足す
- `activity: "ActivityLog | None" = None` 引数を追加 (既存 `reconcile` 系と同じパターン
  `switch.py:122,140`)。立っていたら
  `activity.write(Category.APPROVAL, "bless_floor_warning", f"name={name} unprofitable")`

**既存呼び出し元の全数 (codex I5 により訂正 — v1 の「1 箇所」は誤り)**:
- src: `backtest/cli.py:533` (1 箇所)
- tests: `tests/plugin/test_switch_paths.py:202,225,241,264,300,346,1201,1395,1456` (9)、
  `tests/plugin/test_approval_payload_common_contract.py:154` (1)、
  `tests/plugin/test_materialize_retire.py:212` (1) — 計 **11 箇所**

戻り値を `int` のまま保つので **これら 11 箇所は無変更で通る** (pin F6-7 がこれを固定する)。

**変更**: `src/agentic_fx/backtest/cli.py::_plugin_bless` (`:523-540`)
- `ActivityLog(root / "logs" / "activity.log")` を構築 (`cli.py:556` に既存例) して
  `bless_candidate(..., activity=activity, on_floor_warning=_warn)` に渡す
- `_warn(label, detail)` は stderr に settings からレンダした警告文を出す
  (「警告: この候補は収益性フロア (pf < {min_pf}[ または avg_r <= 0]) を満たしません。
  人間裁定で承認申請を作成しました。詳細は `afx> approval <id>`」)。
  **終了コードは 0 のまま**

### Step 1-6: 人間 corridor の gate 行に明示 outcome (codex I2)

**変更**: `src/agentic_fx/plugin/switch.py`
- `_run_full_gate` が `rows: list[dict] = []` を作り `run_kind_gate(..., record_fn=rows.append)`
  → `evaluate_strategy_adoption_gate(record_fn=…)` へ渡す (この引数は既存。`record_fn` が
  あれば `holdout._run_scope` (`holdout.py:155-160`) は即時 commit しない)。
  捕捉した行は `GateOutcome.gate_rows` で返す
- 新設 helper `_persist_human_gate_rows(conn, rows, *, mission_outcome, now, commit)`:
  `save_harness_run(conn, commit=False, mission_id=None, mission_outcome=…, **row)` を全行
- **保存の所有者は呼び出し元**:
  - `submit_candidate` / `bless_candidate`: `approvals_store.create` と**同じ tx** で
    `mission_outcome="approval"` で書く
  - `_run_full_gate` が `ValueError` を投げる分岐: **捕捉済みの行を短い専用 tx で保存して
    から raise**。フロア不合格 (enforce) = `unprofitable`、その他 (標本不足 / pytest /
    hash / snapshot) = `gate_failed`
- 規約表 (設計書 §3 T1-f と同一):

| 経路 | `mission_outcome` |
|---|---|
| `submit_candidate` 成功 | `approval` |
| `submit_candidate` フロア不合格 (enforce) | `unprofitable` |
| `submit_candidate` その他ゲート不合格 | `gate_failed` |
| `bless_candidate` 成功 (フロア合格 / 警告とも) | `approval` |

**目的**: `mission_outcome=None` の行を**新たに作らない** (`latest_in_sample_metrics` の
live 絞りへの混入を止める)。既存の NULL 行 4 件の遡及修正は範囲外 (設計書 §9 R4)。

### Step 1-7: legacy submit 回廊は strategy を拒否 (codex C1)

**変更**: `src/agentic_fx/plugin/approval.py::submit_plugin`
- 関数冒頭 (`test_plugin_bytes` 読み取りより前、検証ゲートの外) で:
  ```
  if meta.kind == "strategy":
      raise ValueError(
          f"plugin {meta.name!r}: この経路は strategy を受け付けません — "
          "`afx plugin materialize <name>` で候補を書き出し "
          "`afx plugin submit <name> --from _human` を使ってください "
          "(固定 holdout を含む共有ゲートを通すため)")
  ```
  **API 側で fail closed** にする (CLI だけの案内にしない)
- `backtest/cli.py::_plugin_submit` (`:507-520`) は既存 `except ValueError` でそのまま
  stderr に出る (コード変更不要。メッセージに `materialize` と `--from _human` が
  含まれることを pin F6-5 が固定)
- `_validate_kind` / `_validate_strategy` の strategy 分岐は**到達不能**になる。
  **削除しない** (tests が直接使う)。docstring に「`submit_plugin` は strategy を拒否する
  ため、この分岐は直接呼び出し (テスト) からのみ到達する」と明記。
  **この分岐にフロアは足さない** (v1 の「legacy corridor に in_sample 段だけ」は撤回)
- `activity.write(Category.APPROVAL, "submit_floor_rejected", …)` は
  `submit_candidate` のフロア不合格側に置く (legacy 拒否は approval 以前なので不要)

**既存テストへの影響**: `approval.submit_plugin` を strategy で呼ぶ既存テストがあれば
**xfail ではなく「拒否されること」を期待する形へ書き換える**。着手前に
`grep -rn "submit_plugin" tests/` で全数を出し、件数を実装報告に書くこと。

### Step 1-8: 閾値 snapshot (codex I3) と `_approval_detail`

**変更**:
- `"profitability_floor": {"min_pf": …, "require_positive_avg_r": …,
  "require_holdout_evaluable": …}` を **3 箇所**の payload に追加:
  `loops/improve_loop.py::_build_approval_payload` / `switch.py::submit_candidate` /
  `switch.py::bless_candidate`
- `commands.py::_approval_detail`: `floor_warning` / `floor_detail` / `profitability_floor`
  の表示行を追加 (T0 の `reason` / `decided_by` / `decided_at` と同じ task で入れる)

### Step 1-9: テスト

**新規**: `tests/plugin/test_strategy_gate_floor.py` (F1 / F2 / F3 / F9 / F10)、
`tests/loops/test_improve_loop_floor.py` (F4)、
`tests/plugin/test_switch_floor_modes.py` (F6)、
`tests/loops/test_floor_leak_guard.py` (F5 の純粋 store/rendering 部分 — **Landlock skip
marker を付けない**)。
**既存への追加**: `tests/integration/test_improve_forbidden_regression.py` は
実プロセス依存の pin だけ。

設計書 §8 の F1〜F10 を逐語で pin にする。**フィクスチャは実 metrics 形状から**作る
(設計書 §1.1/§1.2 に approval #6〜#10 の実測値がある。手書きの偽形状を作らない)。

**完了条件**:
- [ ] F1-1〜F1-8 (in_sample 段の境界。**F1-4 は `trades>0, pf=None, avg_r>0`**)
- [ ] F2-1〜F2-7 (holdout 段。**F2-4 = `evaluable=false` は通す** /
      **F2-5 は (a) trades=7 と (b) trades=0 の両方** / **F2-7 = 両段のラベルが文字列同一**)
- [ ] F3-1 / F3-2 (多 pair)
- [ ] F4-1〜F4-10 (**F4-9 = gate 行・ledger 行・settle が同一 outcome** /
      **F4-10 = report 失敗時は `report_failed` を上書きしない**)
- [ ] F5-2 / F5-4 (F5-2 は **`last_result` 完全一致 + holdout canary 数値の不在 +
      `floor_detail` の不在 + 段名断片の不在**。pair symbol の全文不在は**要求しない**)
- [ ] F6-1〜F6-8 (**F6-3 = warn bless でも holdout 呼び出しと baseline 添付がある** /
      **F6-5・F6-6 = legacy submit の strategy 拒否と「in_sample 合格・holdout 不合格が
      legacy 経路で approval にならない」** / **F6-7 = bless の戻り値 `int` 後方互換** /
      **F6-8 = 人間 corridor の gate 行に明示 outcome、`None` を作らない**)
- [ ] F9-1 / F9-2 (3 呼び出し元の spy pin と「helper を呼ばない」変異)
- [ ] F10-1 (DD / latches 極端値で verdict 不変)
- [ ] 段0 変異 red (最優先 6 件): `pf is None` を fail 側に倒す / enforce で in_sample 段を
      holdout の後に置く / holdout の `evaluable` を見ずに判定 / strict 判定を zero-trade
      shortcut の後に置く / ラベルに holdout pf を入れる / helper を呼ばず複製
- [ ] 逆変異 red: holdout の返り値を捨てる (現行実装) → F2-1 / `record_fn` を渡さず
      即時 commit に戻す → F6-8 / warn でも短絡する → F6-3
- [ ] `grep -rn "submit_plugin" tests/` の全数と対応結果を実装報告に書く
- [ ] フルスイート green

## T2: 予算内で作り直させる [floor-feedback-in-prompt]

**対応**: 設計書 §6 T2。T1 の後 (同じ判定関数を再利用する)

### Step 2-1: `submission_blocked` は親側 1 箇所で導出 (codex I7)

**変更**: `src/agentic_fx/loops/improve_loop.py::_build_rpc_handlers` の
`run_backtest_handler` (`:796-` の内側)
- `holdout.run_in_sample` の結果 metrics を得た直後に
  `label, detail = strategy_gate._check_profitability_floor({pair: metrics},
  settings=self._settings, scope="in_sample")` を呼び、`label` が立っていたら
  **agent へ返る dict** に
  `{"submission_blocked": {"reason": "unprofitable", "detail": "<settings からレンダした文>"}}`
  を足す。立っていなければ**キーを足さない**
- **`save_kwargs` には足さない** → 台帳の記録内容は変わらない
  (`improve_rpc_tools.py:150-154` が `save_kwargs` を読む契約)

**ここが唯一の導出点である理由 (codex I7 への回答)**: この handler は `self._settings` を
既に持っている。全ての経路 (`improve_loop.py:384` の in-process registry、`:1054` と
`tools/mission_registry.py:107` の RPC 越し子 registry) は `rpc_handlers["run_backtest"]`
を受け取るだけなので、**この 1 箇所を通らない経路は無い**。したがって
`build_improve_rpc_tooldefs` / `build_rpc_handlers` / `mission_registry` の
**シグネチャは変更しない** (gate settings を配線しない)。子側 tooldef の
`_strip_forbidden` は禁止キーのみを剥がすので `submission_blocked` はそのまま通り、
`remaining_budget` の付与 (`:157-162`) とも独立。

> 指揮者注 (v1.1): ブリーフ上の「builder / registry に gate settings を渡す」案は採らない。
> 親側 1 箇所導出で codex I7 の要求 (「どの境界で一度だけ導出するかを固定する」) は満たされ、
> 配線を増やすと導出点が 2 つになりうる。**この 1 点だけブリーフの文面と異なる** —
> 異論があれば実装前に指揮者へ差し戻すこと。

### Step 2-2: 文言は settings からレンダ (codex I8)

**変更**:
- `src/agentic_fx/loops/prompts/improve_mission.md` 規律 4 の末尾に **単一 placeholder**
  `{profitability_floor_rule}` を置く (`.format()` は条件分岐できないため、文そのものを
  親が組み立てて渡す)
- `src/agentic_fx/loops/improve_loop.py::_render_improve_mission_prompt` (`:653-686`) の
  `render_map` に `"profitability_floor_rule": self._floor_rule_text()` を追加。
  `_floor_rule_text()` は settings から組む:
  - `require_positive_avg_r=True`:
    「**`pf < {min_pf}` または `avg_r <= 0` の候補は提出しても承認申請になりません**
    (親の決定論ゲートが `unprofitable` として observation に落とします)。予算内で
    パラメータ・フィルタ・エントリ条件を変えて `run_backtest` をやり直し、
    `pf >= {min_pf}` かつ `avg_r > 0` を満たした候補だけを提出してください。
    予算を使い切っても満たせなければ**提出せず** `observation` として、試した
    パラメータ群とそれぞれの pf / avg_r を理由に書いてください。」
  - `require_positive_avg_r=False`: **avg_r の条件を文から省く**
- 規律 3 の「`observation` として理由を残し」に
  「**試したパラメータと得られた pf / avg_r を具体的に**」を足す
- 規律 4 に「**`config.yaml` の `pairs` に宣言した全 pair を最低 1 回 `run_backtest` で
  確認してから提出してください**」を足す (codex I10 — 候補は 1 pair の不合格で全体が落ちる)
- `submission_blocked.detail`、`cli.py::_plugin_bless` の警告文、`run_kind_gate` の
  `ValueError` メッセージも同じ settings 値を埋める

**完了条件**:
- [ ] F7-1: `submission_blocked` が in_sample 段不合格のときだけ現れる
- [ ] F7-2: 判定が T1 と同じ関数 (`min_pf` を変えたテストが効く)
- [ ] **F7-3: `min_pf=1.5` で文言に `1.5` が出る / `require_positive_avg_r=False` で
      avg_r 条件が文言から消える** (変異: 文言をハードコードに戻す → killer)
- [ ] F7-4: 導出が親 handler 1 箇所。`save_kwargs` / ledger 行に `submission_blocked` が
      混ざらない
- [ ] F7-5: プロンプト規律に「宣言全 pair を最低 1 回 backtest」が含まれる
- [ ] F5-6: `submission_blocked` に `"holdout"` の語・holdout の数値・期間端点が無い
      (既存 pin `"period_start" not in json.dumps(out)` を拡張)
- [ ] 段0 変異 red: 常に `submission_blocked` を付ける / RPC 側に閾値をハードコード /
      子側 tooldef で再導出する
- [ ] フルスイート green

## レビュー段

1. **段0 (指揮者の変異スイープ)** — レビュー前に必ず回す。最優先 6 件 =
   F1-4 (`trades>0, pf=None`) / F1-8 (enforce で holdout を回さない) /
   F2-4 (holdout `evaluable=false` を通す) / F2-5(b) (strict 判定が zero-trade より前) /
   F5-2 (ラベルの holdout 漏洩) / F9-2 (helper を呼ばず複製)
2. **1 周目**: codex + ローカル LLM 3 本 (枠ゼロ、並列可)。**ブリーフに
   「ラベルの固定文言規律」と「遮断 8 のただし書き」を必ず含める** —
   これを知らないレビュアーは「理由が分からないラベルは不親切」と
   逆方向の指摘を出す (材料の作り方が成績を決める)
3. **2 周目**: `/code-review high` + codex + ローカル 3 本。有償 2 本
   (`/code-review` と sonnet) は並列にしない。`/code-review high` は
   指揮者から起動できないのでユーザーに打ってもらう
4. **3 周目 (must-fix のみ)**: ブリーフ付き sonnet。2 周目の結果に応じて要否を判断
5. **レビューを投げる前に、メモリの `review-process` の「投げる前のチェックリスト」を
   読むこと**

**レビュアーに特に見てほしい軸** (ブリーフに書く):
- 固定文言ラベルが**どの経路からも**数値・段名を漏らさないか (`floor_detail` の流れを全数追跡)
- `_finalize_gate_failed` の branch-local outcome が gate 行・ledger 行・settle の 3 箇所で
  一致し、report 失敗分岐では `report_failed` を上書きしていないか (codex I6)
- `GateOutcome` への移行が呼び出し元全数に追随し、`bless_candidate` の `int` 戻りが
  維持されているか (tests 11 箇所が無変更で通ること、codex I5)
- 人間 corridor の gate 行に `mission_outcome=None` が残っていないか (codex I2)
- `warn` bless が holdout・baseline まで完走しているか (codex C2)
- legacy submit が strategy を拒否し、かつ `_validate_strategy` に holdout を
  呼び始めていないか (codex C1)
- `submission_blocked` の導出が親 handler 1 箇所に閉じているか (codex I7)
- 文言が settings からレンダされ、非既定値で食い違わないか (codex I8)

**1 周目の codex 設計レビュー (18 件) は全件採用済** — 同じ指摘が再掲された場合は
v1.1 の該当 Step を指して閉じる。**蒸し返しの裁定は不要**。

## 完了条件 (束全体)

- [ ] T0 / T3 / T1 / T2 がすべて実装完了
- [ ] pin / 逆変異が各 task の完了条件どおり揃っている
- [ ] **設計書 §8 の F1〜F10 が全て pin として存在する** (下限なので追加は可)
- [ ] **`mission_outcome=None` の gate 行を新たに作らない** (F6-8)
- [ ] **`bless_candidate` の既存 11 テスト呼び出しが無変更で通る** (F6-7)
- [ ] `config/settings.yaml` と `config/settings.yaml.example` が同期している
- [ ] **実 DB を書き換えていない** (`git status` と `data/` の mtime で確認)。
      U5 の除染 SQL はユーザーへ提示済み
- [ ] fresh worktree でフルスイート green (残骸ゼロ、空 `logs/` の残留も無い —
      test-hygiene 束 v1.6 の教訓)
- [ ] レビュー段の完了

## 進捗表

| task | 状態 | commit | 備考 |
|---|---|---|---|
| T0 reject reason 固定文言化 + approval detail + 設計書追記 | 未着手 | - | 前提作業。テスト波及は 2 本のみ |
| T3 config 3 キー | 未着手 | - | T1 より先。yaml 両方同期 |
| T1 収益性フロア (2 段) | 未着手 | - | 本体。Step 1-1〜1-3 (ゲート) / 1-4〜1-8 (人間 corridor・C1/C2/I2〜I5) |
| T2 RPC ヒント + プロンプト | 未着手 | - | 導出は親 handler 1 箇所。文言は settings レンダ |
| レビュー段0 (変異スイープ) | 未着手 | - | 最優先 4 件 |
| レビュー1周目 (codex 設計 18 件) | **完了・全件採用** | - | `tmp/design-profitability-floor/codex-design-r1.md` → spec/plan v1.1 |
| レビュー1周目 (実装後: codex + ローカル3) | 未着手 | - | ブリーフにラベル規律 + 18 件の決着を含める |
| レビュー2周目 (`/code-review high` + codex + ローカル3) | 未着手 | - | ユーザーが打つ |
| レビュー3周目 (sonnet、要否判断) | 未着手 | - | |
| fresh worktree フルスイート | 未着手 | - | 残骸ゼロ + `logs/` 未生成 |
| U5 除染 SQL (ユーザー実行) | 未着手 | - | 実装者は提示のみ |

> 指揮者裁定 (2026-09-12、ユーザー裁定 U1〜U5 + 既定選択): U1 = holdout もフロアに入れる・
> ラベルは `unprofitable` 固定 (数値もどちら側で落ちたかも書かない、人間向けレポートにだけ
> 全数値) / U2 = submit は課す (raise)・bless は警告のみ / U3 = `max_drawdown`・
> `kill_switch_latches` は門にしない (レポートには出す) / U4 = baseline 再生は別起票
> [baseline-replay-unimplemented] / U5 = #1/#3 除染はスクリプト更新済・ユーザー実行待ち。
> **既定選択 (裁定不要と判断)**: holdout の判定は holdout 行が `evaluable=true` のときだけ
> pf/avg_r を見る。`evaluable=false` (標本不足) は R8 どおりフロアを通す
> (`require_holdout_evaluable: bool = False` で厳格化可)。理由 = `holdout_months=3` の
> 現設定では低頻度戦略が構造的に 30 件に届かず (#10 は holdout trades 7)、
> holdout 標本不足を理由に落とすかは個別判断。

> 指揮者裁定 (2026-09-12、codex 設計レビュー 1 周目 = **全 18 件採用**、蒸し返さない):
> C1 = legacy submit (`approval.submit_plugin`) は kind=strategy を拒否し
> `materialize` → `submit --from _human` を案内 (共有ゲートへの統合・回廊廃止は別起票
> [legacy-submit-corridor-bypasses-gate]) / C2 = `floor_mode` を evaluator まで渡し
> `warn` では in_sample 不合格でも holdout・baseline 収集まで完走 / I1 = 遮断 8 の例外は
> 「人間 reject と同程度の露出」ではなく「反復可能な二値 oracle を受容する脅威モデル」として
> 2026-08-16 設計書 ⑧ に逐語追記、テスト保証は「数値・段名・pair・baseline 差分を漏らさない」
> に限定 / I2 = 非コミット sink + 人間回廊の明示 outcome / I3 = payload に
> `profitability_floor` snapshot / I4・I5 = `GateOutcome` dataclass、`bless_candidate` は
> `int` 戻り維持 + `on_floor_warning`、`_approval_detail` に floor 表示 /
> I6 = branch-local outcome 統一・`report_failed` 不上書き / I7 = `submission_blocked` は
> 親 handler 1 回導出 / I8 = 文言を settings レンダ / I9 = strict holdout 判定を
> zero-trade より前 / I10・I11 = 予算 validator + 全 pair backtest 規律、「別名で枠再取得」撤回 /
> I12 = 漏洩 pin を Landlock skip 無しモジュールへ / I13 = F5-2 の範囲限定 /
> I14 = 3 呼び出し元 spy pin / M1 = F1-4 の fixture / M2 = DD・latches 不変 pin。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案。T0 (reject reason 固定文言化 + `_approval_detail` の reason 表示 + 2026-08-16 設計書 §7.1-1 ⑧ へのただし書き追記) / T3 (config 3 キー) / T1 (収益性フロア 2 段、`_finalize_gate_failed` の `mission_outcome`/`report_detail` 引数化、`run_kind_gate`/`_run_full_gate`/`bless_candidate` の戻り値拡張、legacy corridor は in_sample のみ) / T2 (RPC `submission_blocked` + プロンプト規律) の順序と Step 分割、接合面、完了条件、レビュー段 (ブリーフにラベル規律必須)、プラン規約 (実 DB 不可触・ラベル固定文言規律) | 設計書 `2026-09-12-profitability-floor-design.md` v1 を実装プランへ写す。ユーザー裁定 U1〜U5 (2026-09-12) と指揮者既定選択を反映 | - |
| 2026-09-12 | v1.1 | codex 設計レビュー 1 周目 18 件を全件反映。T1 を Step 1-1〜1-9 へ再構成 (判定順序 ①strict holdout→②zero-trade→③R8、`floor_mode` を evaluator まで、branch-local outcome 統一、`GateOutcome` dataclass、`bless_candidate` は `int` 戻り + `on_floor_warning`、非コミット sink と人間回廊の明示 outcome 規約表、legacy submit の strategy 拒否、`profitability_floor` snapshot)。T2 を「親 handler 1 箇所導出 + settings からの文言レンダ」へ書き換え (builder/registry の配線は作らない)。T3 に `max_backtests_per_candidate >= len(pairs)` validator を追加し「別名で枠再取得」を撤回。T0 に漏洩 pin 用の新規モジュール `tests/loops/test_floor_leak_guard.py` (Landlock skip 無し) と `_approval_detail` の floor 表示を追加。完了条件・進捗表・レビュー段・裁定ブロックを更新 | codex 設計レビュー 1 周目 `tmp/design-profitability-floor/codex-design-r1.md` (gpt-5.6-sol、spec/plan v1 = 3ab0b06)、指揮者裁定 2026-09-12 (全件採用) | - |
