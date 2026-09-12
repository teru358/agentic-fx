# テスト衛生 + 小物 実装プラン v1 (設計書 = `2026-09-12-test-hygiene-design.md` 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) または superpowers:executing-plans で task ごとに実行すること。

**Goal:** 設計書 v1 の T1 (bootstrap probe の import 時束縛欠陥 + runner cwd 残骸の
session guard) / T2 (`plugins/.locks` 蓄積) / T3 (`missions.finished_at` の
論理時刻明記) / T4 (`_check_codex_subscription_expiry` のスキーマ不一致) /
T5 (`INDEX.md` 既存ファイルへのヘッダ後付け) を実装する。**設計を変えない** —
曖昧な箇所 (T2/T4/T5 の裁定候補選択) は各 task 冒頭の「ユーザー裁定待ち」に
列挙し、着手前に裁定を得る。

**Architecture:** 1 worktree・直列。T1 が本体で `mission_worker.py` /
`cli_runner.py` / `tests/conftest.py` を触る。T2〜T5 は小物で、T2 は
`plugin/switch.py`、T3 はドキュメントのみ、T4 は `service.py`、T5 は
`plugin/switch.py` (T2 と同じ `sweep_orphans` に相乗り) — **T2 と T5 は
同じ関数を触るため、裁定候補 1 を両方採用する場合は T2 → T5 の順に直列で
実装する** (T5 が T2 で追加した sweep 呼び出しの直後に相乗りする)。
決定論的コアの判定ロジック (Risk Gate・approval 決定) は全 task で不変。

**Tech Stack:** Python 3.13 / uv / pytest / sqlite3 (既存 schema への追加は
無し。ドキュメント変更のみの task を含む)

## Global Constraints

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p`
  (サブスク認証) のみ
- 発注・SL 変更・クローズ・資金保護は本プランの対象外
- `plugins/`・`config/`・`data/` はコミット対象外のまま (本プランは
  `src/` / `tests/` / `docs/` のみ変更)
- **T2・T4・T5 はユーザー裁定候補を両論併記した状態で着手前に裁定を得ること**
  (設計書の「推奨」を無断で確定させない)

## T1: bootstrap probe テストの import 時束縛欠陥 + runner cwd 残骸の session guard [bootstrap-probe-tests-mkdir-real-logs-dir]

**対応**: 設計書 §T1。本束の本体

**変更**:
- `src/agentic_fx/mission_worker.py`: `_TRANSCRIPT_DIR_DEFAULT as
  _MISSION_TRANSCRIPT_DIR` の値コピー import (76 行目) をやめ、
  `cli_runner` モジュールを import して `_bootstrap_improve_profile`
  (253 行目周辺) から `cli_runner._TRANSCRIPT_DIR_DEFAULT` を都度参照する
- `src/agentic_fx/runners/cli_runner.py`: `_TRANSCRIPT_DIR_DEFAULT`
  (43 行目) の初期化に環境変数 override を追加 (例:
  `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR` があればそちらを使う)
- `tests/test_mission_worker.py::_run_bootstrap_probe`: 子プロセスの
  `env` に上記環境変数を tmp_path 配下の値で設定して渡す
- `tests/conftest.py`: repo root 直下の untracked ファイル差分を
  session 前後で比較する新規 autouse session fixture を追加 (fail closed)

**執筆時の未決事項 (実装者が着手前に確認すること)**:
- (b) の repo root 残骸 (`mcp.json`/`schema.json`/`prompt.txt`) は本設計書
  執筆時点で**特定の pytest テストを 1 本に絞り込めなかった**
  (`tests/runners/test_claude_runner.py`/`test_codex_runner.py`/
  `test_cli_runner.py` は全数確認済みで workdir はすべて tmp_path 由来)。
  着手前に `find . -maxdepth 2 \( -name mcp.json -o -name schema.json
  -o -name prompt.txt \)` 等で repo root/worktree root の残骸有無を確認し、
  もし pytest 実行で再現する経路が見つかったら、そのテストの workdir 起点を
  tmp_path へ修正すること (本 task の変更点表には含めていない — 見つかった
  場合の追加修正として扱う)。見つからない場合は session guard の追加のみで
  完了とする

**完了条件**:
- [ ] pin: `_bootstrap_improve_profile` を同一プロセス内で直接呼ぶケース・
      `_run_bootstrap_probe` で別プロセス起動するケースの両方で、隔離用
      tmp_path 配下だけが mkdir され実 `logs/mission-transcripts/` に
      新規エントリが増えないこと
- [ ] 段0 変異 red: 値コピー import を元に戻す変異 / 環境変数 override を
      読まない変異
- [ ] **fresh worktree でフルスイートを実行し、実行前後で repo root 直下の
      untracked ファイルがゼロであること** (session guard 自体がこの条件を
      検査する — 手動確認としても 1 回実施する)
- [ ] フルスイート green

## T2: `plugins/.locks/<name>.lock` の蓄積回収 [plugin-locks-accumulate]

**対応**: 設計書 §T2

**ユーザー裁定待ち**: 設計書は「decide 直後 unlink」(候補1) と「startup
sweep 回収」(候補2、推奨) を両論併記している。**着手前にどちらを採用するか
(または両方か) の裁定を得ること**。以下は候補2 (startup sweep) を既定と
した変更点

**変更 (候補2 採用時)**:
- `src/agentic_fx/plugin/switch.py::sweep_orphans`: `.locks/*.lock` の
  うち、対応する pending approval_request/候補が存在しない名前のファイルを
  削除する分岐 (⑧) を追加

**完了条件**:
- [ ] pin: pending な approval が無い名前のロックファイルは sweep で消える
      / pending がある名前のロックファイルは残る / `.locks/` ディレクトリ
      自体が無い場合は何もしない
- [ ] 逆変異 red: 参照判定 (pending 突合) を外して無条件削除にする変異
- [ ] フルスイート green

## T3: `missions.finished_at` は論理終端時刻と明記 [missions-finished-at-is-logical]

**対応**: 設計書 §T3

**ユーザー裁定待ち**: 設計書は「論理終端時刻と明記 + activity の壁時計を
併記する運用」(候補1、推奨) と「commit 直前に取り直す」(候補2、Clock 配線
変更が要る) を両論併記している。**着手前にどちらを採用するか裁定を得ること**

**変更 (候補1 採用時、ドキュメントのみ)**:
- `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md`
  または `docs/superpowers/specs/2026-09-10-ledger-preserve-design.md` の
  `missions.finished_at` 説明箇所に、論理終端時刻である旨と実測時は
  `activity` テーブルを使う旨を追記

**完了条件 (候補1 採用時)**:
- [ ] 該当設計書に追記されていること (pin/逆変異/フルスイートは対象外 —
      ドキュメントのみの変更)

**完了条件 (候補2 採用時、参考)**:
- [ ] pin: `finished_at` が `_run_strategy_gate` 呼び出し後の壁時計に
      一致すること
- [ ] 既存の `FixedClock` 前提の pin が壊れないこと (Clock 配線変更の
      影響範囲を洗い出した上で着手)
- [ ] フルスイート green

## T4: `_check_codex_subscription_expiry` のスキーマ不一致 [codex-subscription-expiry-check-never-fires]

**対応**: 設計書 §T4

**ユーザー裁定待ち**: 設計書は「実キー (`tokens.id_token` の JWT `exp`
クレーム) から是正できるか確認した上で是正」(候補1) と「検査を撤去」
(候補2) を両論併記している。**着手前に、まず `tokens.id_token` の payload
(署名部分は読まない・デコードのみ) に `exp` クレームがあるか、それが
「サブスク期限」を指すものかを確認し、その結果を踏まえてどちらを採用するか
裁定を得ること**

**変更 (候補1・is exp が使える場合)**:
- `src/agentic_fx/service.py::_check_codex_subscription_expiry`:
  `chatgpt_subscription_active_until` の代わりに `tokens.id_token` の
  JWT `exp` クレームを見る形に書き換え

**変更 (候補2・撤去の場合)**:
- `src/agentic_fx/service.py`: `_check_codex_subscription_expiry` の
  定義と呼び出し (386 行目) を削除。対応するテストも削除

**完了条件 (候補1)**:
- [ ] pin: `exp` がある/ない両方の auth.json フィクスチャで正しく
      警告/非警告になる / `exp` が過去日時のフィクスチャで警告が出る
- [ ] フルスイート green

**完了条件 (候補2)**:
- [ ] 呼び出し削除・関数削除・対応テスト削除が揃っていること
- [ ] フルスイート green

## T5: `plugins/_archive/INDEX.md` 既存ファイルへのヘッダ後付け [archive-index-naming] の残り

**対応**: 設計書 §T5。**T2 (候補2 採用時) の後**に着手 — 同じ
`sweep_orphans` に相乗りするため

**ユーザー裁定待ち**: 設計書は「startup sweep でヘッダを 1 回だけ補う」
(候補1) と「据え置き」(候補2、推奨) を両論併記している。**着手前にどちらを
採用するか裁定を得ること**

**変更 (候補1 採用時)**:
- `src/agentic_fx/plugin/switch.py::sweep_orphans`: `INDEX.md` の先頭が
  ヘッダ文言と一致しなければ、一時ファイル書き込み→rename でヘッダを
  先頭に挿入する処理を追加

**完了条件 (候補1 採用時)**:
- [ ] pin: ヘッダ無し既存ファイル → sweep 後にヘッダ付き / 既にヘッダ付き
      ファイル → 二重挿入されない / ファイルが無い場合は何もしない
- [ ] 逆変異 red: ヘッダ一致判定を外す (毎起動で二重挿入される事故)
- [ ] フルスイート green

**完了条件 (候補2・据え置きの場合)**:
- [ ] 変更なし。設計書 §T5 の裁定記録のみで完了とする

## レビュー段

1. **段0 (指揮者の変異スイープ)** — レビュー前に必ず回す
2. **1 周目**: codex + ローカル LLM 3 本 (枠ゼロ、並列可)
3. **must-fix (Critical/Important) が 1 周目で 0 件なら、2 周目 (`/code-review
   high`) を省略してよい** — 提案であり、**実際に省略するかは裁定は
   ユーザーが行う** (`/code-review high` は指揮者から起動できないため)
4. 1 周目で must-fix が出た場合は 2 周目 (`/code-review high` + codex +
   ローカル 3 本) を実施する。有償 2 本 (`/code-review` と sonnet) は
   並列にしない
5. **3 周目 (must-fix のみ)**: ブリーフ付き sonnet。2 周目まで実施した
   場合のみ、その結果に応じて要否を判断する

## 完了条件 (束全体)

- [ ] T1〜T5 (裁定確定済みの選択肢) がすべて実装完了
- [ ] pin / 逆変異が各 task の完了条件どおり揃っている
- [ ] **fresh worktree でフルスイートを実行し、残骸ゼロ** (T1 の
      session guard が green で通ること、かつ手動で repo root
      直下を目視確認すること)
- [ ] レビュー段の完了 (2 周目省略の場合はその旨を進捗表に明記)

## 進捗表

| task | 状態 | commit | 備考 |
|---|---|---|---|
| T1 bootstrap probe + cwd 残骸 | 未着手 | - | 本体。(b) の再現条件特定を含む |
| T2 `.locks` 蓄積回収 | 未着手 (裁定待ち) | - | 候補1/候補2 の選択が要る |
| T3 `finished_at` 論理時刻明記 | 未着手 (裁定待ち) | - | 候補1/候補2 の選択が要る |
| T4 codex auth.json スキーマ不一致 | 未着手 (裁定待ち) | - | `exp` クレーム確認が先 |
| T5 INDEX.md ヘッダ後付け | 未着手 (裁定待ち、T2 後) | - | 候補1/候補2 の選択が要る |
| レビュー段0 | 未着手 | - | |
| レビュー1周目 | 未着手 | - | |
| レビュー2周目 (省略可否含め裁定) | 未着手 | - | |
| fresh worktree フルスイート (残骸ゼロ確認) | 未着手 | - | |


> 指揮者補足 (2026-09-12): T1(b) の残骸 `prompt.txt` の中身は 1 byte `p` = `tests/runners` の `_mission()` 既定 prompt (r2b 以前) と一致するので **pytest 由来で確定**。`workdir=` が tmp_path でも、`CliRunner` を `workdir` 省略で構築する経路か、`Path.cwd()` を既定にする fixture を疑う (`grep -rn "CliRunner(\|ClaudeRunner(\|CodexRunner(" tests | grep -v workdir`)。実装者は「テスト実行後に repo root の untracked が増えない」session guard を先に入れ、guard が落ちるテストを犯人として特定する。


> 指揮者裁定 (2026-09-12 17:00、既定選択): T2 = startup sweep で `.locks/` を回収 (flock/unlink の TOCTOU を避ける) / T3 = 文書化 (`finished_at` は論理終端時刻、壁時計は activity) のみ、コード変更なし / T4 = 実 auth.json (codex 0.150.1) に期限キーが存在しないため検査を撤去 (WARNING の空振りを止める。期限は codex 側が 401 で返すのに任せ、`cli_stderr_fatal` に `usage limit` / `401` パターンを足す) / T5 = 据え置き (文書に明記済)。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案。T1 (本体) / T2〜T5 (小物) の task 分割、T2/T4/T5 にユーザー裁定待ちを明記、完了条件に fresh worktree フルスイート残骸ゼロを規定、レビュー段に「1周目 must-fix 0 件なら2周目省略」の提案を明記 (裁定はユーザー) | 設計書 `2026-09-12-test-hygiene-design.md` v1 を実装プランへ写す | - |
| 2026-09-12 | v1.1 | 指揮者裁定を追記 (T2 startup sweep / T3 文書 / T4 撤去 + stderr パターン / T5 据え置き)、補足 (`prompt.txt`=`p` は pytest 由来、session guard 先行) | 実 auth.json のキー確認と残骸の中身から既定を確定 | f39bed5 |
| 2026-09-12 | v1.2 | 実装完了 (sonnet)。T1(a) 遅延参照 + env `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR` / T1(b) session guard、犯人 = `test_mission_worker_protocol._drive_main` の chdir 漏れ (ブリーフの「runner テスト」想定は外れ) / T2 ⑧ / T4 撤去 + 4 パターン / T3,T5 文書。worker_runner への env 伝播は R10-① pin と衝突し撤回 (テスト側 monkeypatch)。逆変異 4/4 RED (run_mut.py overlay は `__file__` 相対の既定値で偽陰性 → 手動プロトコル)。fresh worktree フルスイート 3766 passed、残骸ゼロ、`logs/` 未生成 | 実装報告 `tmp/th/impl-report.md` | a6cef16 |
| 2026-09-12 | v1.3 | 検収是正 C1 `"401"` 部分一致 → `"401 Unauthorized"` / `"Unauthorized"` (ブリーフ側の欠陥) / C2 `.locks` sweep の payload 解析を行単位隔離 (`sweep_locks_payload_corrupt`) / C3 guard は増加のみ fail (`_new_untracked`、pin `tests/test_conftest_guards.py`)。指揮者が T2 flock 逆変異を独立再現 (RED) | 削除行と部分一致の検収 | 3413fe9 |
