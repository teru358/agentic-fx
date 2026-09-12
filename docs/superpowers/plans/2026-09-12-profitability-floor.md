# 収益性フロア + reject reason 漏洩 実装プラン v1 (設計書 = `2026-09-12-profitability-floor-design.md` 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (推奨) または superpowers:executing-plans で task ごとに実行すること。

**Goal:** 設計書 v1 の T0 (人間 reject reason のプロンプト漏洩の是正) / T1 (収益性フロアを
決定論ゲートに 2 段で課す) / T2 (RPC 返却ヒント + プロンプト規律) / T3 (config 3 キー) を
実装する。**設計を変えない** — ユーザー裁定 U1〜U5 は設計書 §2 に確定記録済みなので
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
- `src/agentic_fx/tools/improve_rpc_tools.py` / `loops/prompts/improve_mission.md` (T2)
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
  「holdout」の語を**絶対に混ぜない** (設計書 §4)。数値は親専有レポートと
  `backtest_runs` にだけ出す

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
- `tests/integration/test_improve_forbidden_regression.py`: F5-1 / F5-3 / F5-4 / F5-5 を追加
  (⑧ の節へ。**レンダ済みプロンプト全文** (`_render_improve_mission_prompt` の戻り値) を
  対象に `assert "holdout" not in rendered` 等の形で書く — DB 行の assert で代替しない)

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
- `config/settings.yaml.example` (`:95-97` の `improve.gate`) に 3 キーをコメント付きで追記
- `config/settings.yaml` (gitignore) に同じ 3 キーを追記 (**必ず両方**)

**接合面**: `ImproveGateSettings` は `_Strict` 継承なので未知キーは拒否される →
`settings.yaml` 側だけに書くと起動しない / `config.py` 側だけだと既定値で動く。
**両方同期** が完了条件。

**執筆時の申し送り**: `settings_snapshot_hash` (`store/backtest_runs.py:315-320`) は
`{"risk":…, "backtest":…}` しか含まないため `improve.gate` を変えても
`backtest_runs.settings_hash` は変わらない。**ここを直さない** (既存の全 hash が変わり
§A の dedup 母集団と既存行の identity に波及する)。代わりに T1 で
「適用した閾値をレポートと activity に書く」で監査痕跡を作る。

**完了条件**:
- [ ] pin F8-1: 既定値 `min_pf == 1.0` / `require_positive_avg_r is True` /
      `require_holdout_evaluable is False`
- [ ] pin F8-2: `settings.yaml.example` と `ImproveGateSettings` のキー集合一致
      (既存の同期テストがあればそれに乗る。無ければ新設)
- [ ] pin F8-3: `min_pf` に負値 → `ValidationError`
- [ ] 段0 変異 red: 既定 `min_pf` を 0.0 にする変異 → F8-1 が落ちる
- [ ] フルスイート green

---

## T1: 収益性フロアを決定論ゲートに課す [profitability-floor]

**対応**: 設計書 §3 / §7。本束の本体。**task 内分散**: Step 1-1〜1-3 (ゲート本体) と
Step 1-5 (テスト転写) を並列執筆してよいが、統合 + red/green 実行は 1 レーン直列。

### Step 1-1: 判定関数 (`_check_profitability_floor`)

**変更**:
- `src/agentic_fx/plugin/strategy_gate.py` に新設:
  ```
  def _check_profitability_floor(per_pair: dict[str, dict], *, settings,
                                 scope: str) -> tuple[str, str]:
      """戻り値 (label, detail)。label は "" (合格) か "unprofitable" (固定)。
      detail は人間向けレポート専用の全数値文字列 — label と混ぜない。"""
  ```
- 判定は設計書 §3「判定規則 (逐語)」のとおり。**pair ごとに判定し 1 pair でも
  不合格なら候補全体が不合格**。`scope` は `"in_sample"` / `"holdout"`

**落とし穴 (設計書 §0 副次)**:
- `pf is None` (`gross_loss == 0`) は **PASS**。素で `pf < min_pf` を書くと `TypeError`
- `trades == 0` の pair は `avg_r is None` なので**判定から除外** (`continue`)
- `scope == "holdout"` かつ `metrics["evaluable"]` が false のときは、
  `require_holdout_evaluable` が True でなければ**判定から除外** (R8、指揮者既定)
- `pf == min_pf` は PASS (`<` であって `<=` ではない)。`avg_r == 0.0` は FAIL

### Step 1-2: verdict への載せ方と 2 段構成

**変更**:
- `StrategyGateVerdict` に `floor_reason: str = ""` / `floor_detail: str = ""` を追加
- `evaluate_strategy_adoption_gate`:
  - in-sample ループ → `total_trades` 判定 (既存) の**直後**に in_sample 段を呼ぶ。
    不合格なら `StrategyGateVerdict(evaluable=True, floor_reason="unprofitable",
    floor_detail=…, candidate_metrics=per_pair)` を即 return —
    **holdout ループに入らない** (F1-8)
  - holdout ループの `run_holdout(...)` の**返り値を pair ごとに捕捉**する
    (`holdout_per_pair[pair] = run_holdout(...)`。現行は戻り値を捨てている
    `strategy_gate.py:160-171`)。ループ後に holdout 段を呼び、不合格なら同じ形の
    verdict を return (`floor_detail` に in_sample + holdout の両方を文字列化)
  - 合格時は従来どおり baseline / no_strategy の分岐へ進む (既存挙動を変えない)

**接合面の注意**: **`evaluable` を流用しない**。`evaluable=False` は R8 の「標本不足」
意味論を 2 箇所が読んでいる (`plugin/approval.py:216-221` が
`"strategy not evaluable (insufficient trades)"` で raise、
`loops/improve_loop.py:2179` が `observation_reason` を `insufficient_trades:` として
扱う)。フロアは**独立したフィールド**で表す。

### Step 1-3: 改善ループ側の再ルート

**変更**:
- `src/agentic_fx/loops/improve_loop.py` の `if not strategy_verdict.evaluable:`
  (`:2179`) ブロックの**直後**に:
  ```
  if strategy_verdict.floor_reason:
      self._finalize_gate_failed(
          conn, ctx=ctx, backlog_id=selection.backlog_id,
          reason=strategy_verdict.floor_reason,      # = "unprofitable" 固定
          now=now, gate_rows=tuple(gate_rows), tool_calls=tool_calls,
          mission_outcome="unprofitable",
          report_detail=strategy_verdict.floor_detail)
      return
  ```
- `_finalize_gate_failed` (`:2689`) に 2 引数を追加:
  - `mission_outcome: str = "gate_failed"` — `_persist_gate_rows` の 2 箇所
    (`:2739`, `:2769`) へ転送。**既定値は現行値なので既存 3 呼び出しの挙動は不変**
  - `report_detail: str = ""` — `body_md` の後段に全数値を書く
- `body_md` に入れるもの (設計書 §3「ラベルと数値の分離」/ F4-4):
  in_sample と holdout の pair ごとの
  `trades / pf / win_rate / avg_r / max_drawdown / total_pnl / kill_switch_latches / evaluable`、
  落ちた段、落ちた pair、**適用した閾値** (`min_pf=… require_positive_avg_r=… require_holdout_evaluable=…`)
- `activity` の `gate_failed` 行: `reason=` は引数 `reason` のまま (= `unprofitable` 固定)。
  適用閾値を同じ行に足す (`report_detail` の全数値は activity には書かない)

**接合面の注意**: `report_detail` は **`last_result` には一切流さない**。
`last_result` は `reason` 逐語 = `unprofitable` のみ (F2-7 / F5-2 の pin が守る)。

### Step 1-4: 人間 corridor (U2)

**変更**:
- `src/agentic_fx/plugin/approval.py::run_kind_gate` (`:177` 付近):
  - 引数 `floor_mode: Literal["enforce", "warn"] = "enforce"` を追加
  - 戻り値を `(metrics, evaluable)` → `(metrics, evaluable, floor_warning: str)` に拡張
    (`floor_warning` は `""` か `"unprofitable"`)
  - `verdict.floor_reason` が立っていたら: enforce → `ValueError(f"plugin {meta.name!r}: unprofitable")`、
    warn → 続行して `floor_warning` に載せる。`floor_detail` も 4 要素目として返す
- `src/agentic_fx/plugin/approval.py::_validate_strategy` (`:118-158`):
  **in_sample 段のみ**のフロアを追加 (この legacy corridor は holdout を回さない)。
  `total_trades`/`evaluable` の算出直後に
  `_check_profitability_floor(per_pair_metrics, settings=settings, scope="in_sample")`
  を呼び、立ったら `ValueError(f"plugin {meta.name!r}: unprofitable")`。
  **holdout を呼び始めないこと** (F6-6)
- `src/agentic_fx/plugin/switch.py::_run_full_gate` (`:738`):
  `floor_mode` 引数を足して `run_kind_gate` へ転送。戻り値を
  `(meta, after_content, after_artifact, metrics, evaluable, floor_warning, floor_detail)`
  へ拡張 (呼び出し 2 箇所 `:818`, `:1470` を追随)
- `src/agentic_fx/plugin/switch.py::submit_candidate` (`:805`): `floor_mode="enforce"` (既定)。
  戻り値の要素数変更に追随するだけ
- `src/agentic_fx/plugin/switch.py::bless_candidate` (`:1440` 付近):
  `floor_mode="warn"`。`activity: "ActivityLog | None" = None` 引数を追加
  (既存 `reconcile` 系と同じパターン — `switch.py:122,140`)。`floor_warning` が
  立っていたら payload に `"floor_warning": "unprofitable"` と
  `"floor_detail": <全数値>` を足し、`activity is not None` なら
  `activity.write(Category.APPROVAL, "bless_floor_warning", f"name={name} unprofitable")`
- `src/agentic_fx/backtest/cli.py::_plugin_bless` (`:523-540`):
  `ActivityLog(root / "logs" / "activity.log")` を構築して
  `bless_candidate(..., activity=activity)` に渡す (`cli.py:556` に既存の構築例)。
  `floor_warning` が立っていたら stderr に
  「警告: この候補は収益性フロア (pf<1 または avg_r<=0) を満たしません。
  人間裁定で承認申請を作成しました。詳細は `afx> approval <id>`」を出す。
  **終了コードは 0 のまま** (bless は成功している)

**接合面の注意**: `bless_candidate` の戻り値は現行 `int` (approval_id)。
`floor_warning` を呼び出し元へ返す必要があるので、**戻り値を変えるか**
`(approval_id, floor_warning)` のタプルにするか、**payload 経由で CLI が読み直すか**
の 3 択。`bless_candidate` の呼び出し元は `cli.py:533` の 1 箇所のみ
(`grep -rn "bless_candidate" src/ tests/` で全数確認してから決める) —
**タプル化を既定**とし、テストの追随を含めて実装する。

### Step 1-5: テスト

**新規**: `tests/plugin/test_strategy_gate_floor.py` (判定関数 + verdict、F1/F2/F3)、
`tests/loops/test_improve_loop_floor.py` (再ルートと副作用、F4)、
`tests/plugin/test_switch_floor_modes.py` (U2、F6)。
**既存への追加**: `tests/integration/test_improve_forbidden_regression.py` (F5-2/F5-6)。

設計書 §8 の F1〜F8 を逐語で pin にする。**フィクスチャは実 transcript / 実 metrics 形状から**
作る (実 DB の値を使ってよい — 設計書 §1.1/§1.2 に approval #6〜#10 の実測値がある。
手書きの偽形状を作らない)。

**完了条件**:
- [ ] F1-1〜F1-8 (in_sample 段の境界 + holdout を回さない)
- [ ] F2-1〜F2-7 (holdout 段。特に **F2-4 = `evaluable=false` は通す** と
      **F2-7 = in_sample 段と holdout 段のラベルが文字列として同一**)
- [ ] F3-1 / F3-2 (多 pair)
- [ ] F4-1〜F4-8 (再ルート後の台帳 / backlog / approval / レポート / INDEX / staging /
      既存 3 呼び出しの後方互換 / §A dedup 母集団への非混入)
- [ ] F5-2 / F5-6 (遮断 8: ラベルとヒントに holdout 由来文字列が無い)
- [ ] F6-1〜F6-6 (submit は raise / bless は警告のみ / legacy corridor は in_sample のみ)
- [ ] 段0 変異 red (最優先 4 件): `pf is None` を fail 側に倒す / in_sample 段を
      holdout の後に置く / holdout の `evaluable` を見ずに判定する /
      ラベルに holdout pf を入れる
- [ ] 逆変異 red: holdout の返り値を捨てる (現行実装に戻す) → F2-1 が落ちる
- [ ] フルスイート green

---

## T2: 予算内で作り直させる [floor-feedback-in-prompt]

**対応**: 設計書 §6 T2。T1 の後 (同じ判定関数を再利用する)

**変更**:
- `src/agentic_fx/tools/improve_rpc_tools.py::run_backtest`: `remaining_budget` を足す
  箇所 (`:157-162`) の隣で、`_strip_forbidden` 済み response に
  `submission_blocked` を足す:
  `{"reason": "unprofitable", "detail": "pf=… avg_r=… — この成績では親ゲートが承認申請を出さず observation になります"}`。
  **in_sample 段を通るときはキーを足さない**。判定は T1 の
  `_check_profitability_floor(…, scope="in_sample")` を**同じ関数で**呼ぶ
  (閾値をここにハードコードしない)。**holdout を一切参照しない** (遮断 8)
- `src/agentic_fx/loops/prompts/improve_mission.md` 規律 4 の末尾に設計書 §6 T2 の
  文言ブロックを逐語で追記
- 同 規律 3 の「`observation` として理由を残し」に
  「**試したパラメータと得られた pf / avg_r を具体的に**」を足す

**接合面の注意**: `run_backtest` handler の戻り値は `_RpcToolResult` (dict サブクラス、
`save_kwargs` 属性付き)。`submission_blocked` は `_strip_forbidden` の**後**に足す
(`remaining_budget` と同じ位置) — 前に足すと禁止キー濾過の対象になりうる。
`response` に足したキーは**台帳には入らない** (`ledger.record` は strip 前の `result` を
渡している `:150-154`) ので台帳の痩せは起きない。

**予算との整合 (変更不要の確認)**: `max_backtests_per_candidate=6` は候補ごと
(`counters.backtest_calls[name]`)。パラメータを変えた候補を別名で書けば枠は別。
`max_writes=30` / `max_tool_calls=300` の範囲で 3〜4 候補 × backtest 数回は収まる。
**追加 config は作らない**。

**完了条件**:
- [ ] F7-1: `submission_blocked` が in_sample 段不合格のときだけ現れる
- [ ] F7-2: 判定が T1 と同じ関数を呼ぶ (`min_pf` を変えたテストが効く)
- [ ] F7-3: プロンプト文言 pin (`pf >= 1.0` と `unprofitable` の語)
- [ ] F5-6: `submission_blocked` に `"holdout"` の語・holdout の数値・期間端点が無い
      (既存 pin `"period_start" not in json.dumps(out)` を拡張)
- [ ] 段0 変異 red: 常に `submission_blocked` を付ける変異 / RPC 側に閾値を
      ハードコードする変異
- [ ] フルスイート green

---

## レビュー段

1. **段0 (指揮者の変異スイープ)** — レビュー前に必ず回す。最優先 4 件 =
   F1-4 (`pf is None`) / F1-8 (in_sample 段で holdout を回さない) /
   F2-4 (holdout `evaluable=false` を通す) / F5-2 (ラベルの holdout 漏洩)
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
- 固定文言ラベルが**どの経路からも**数値を漏らさないか (`floor_detail` の流れを全数追跡)
- `_finalize_gate_failed` の `mission_outcome` 引数化が既存 3 呼び出しの挙動を変えていないか
- `run_kind_gate` / `_run_full_gate` / `bless_candidate` の戻り値拡張が
  呼び出し元全数に追随しているか (`grep` で全数確認した証跡を添える)
- legacy corridor (`approval.submit_plugin`) が holdout を呼び始めていないか

## 完了条件 (束全体)

- [ ] T0 / T3 / T1 / T2 がすべて実装完了
- [ ] pin / 逆変異が各 task の完了条件どおり揃っている
- [ ] **設計書 §8 の F1〜F8 が全て pin として存在する** (下限なので追加は可)
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
| T1 収益性フロア (2 段) | 未着手 | - | 本体。Step 1-4 の戻り値拡張が広い |
| T2 RPC ヒント + プロンプト | 未着手 | - | T1 の判定関数を再利用 |
| レビュー段0 (変異スイープ) | 未着手 | - | 最優先 4 件 |
| レビュー1周目 (codex + ローカル3) | 未着手 | - | ブリーフにラベル規律を含める |
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

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案。T0 (reject reason 固定文言化 + `_approval_detail` の reason 表示 + 2026-08-16 設計書 §7.1-1 ⑧ へのただし書き追記) / T3 (config 3 キー) / T1 (収益性フロア 2 段、`_finalize_gate_failed` の `mission_outcome`/`report_detail` 引数化、`run_kind_gate`/`_run_full_gate`/`bless_candidate` の戻り値拡張、legacy corridor は in_sample のみ) / T2 (RPC `submission_blocked` + プロンプト規律) の順序と Step 分割、接合面、完了条件、レビュー段 (ブリーフにラベル規律必須)、プラン規約 (実 DB 不可触・ラベル固定文言規律) | 設計書 `2026-09-12-profitability-floor-design.md` v1 を実装プランへ写す。ユーザー裁定 U1〜U5 (2026-09-12) と指揮者既定選択を反映 | - |
