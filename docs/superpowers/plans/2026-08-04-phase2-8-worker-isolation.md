# Phase 2 プラン 8: worker 隔離 + preemption + 起票返済 実装プラン (設計書 改訂 5 = a5c1dff 準拠)

**改訂履歴**: レビュー反映 1 回目 (2026-08-06)。参照: `.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/plan-review-adjudication.md` (裁定書)。sonnet 主査レビュー (`plan-review-sonnet.md`, CR-1〜6/IM-1〜10) + codex 敵対レビュー (`plan-review-codex.md`, P8-01〜07) + fork 指摘の反証検証 (`plan-review-fc-verdicts.md`) を統合し、CONFIRMED 判定分をこのプラン文書に反映した。修整は 1 回で打ち切り。Task 1〜11 分の反映ログ: `.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/fix-log-part1.md`。Task 12〜20 + Task 3 補遺 + Global Constraints 分の反映ログ: `.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/fix-log-part2.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mission (取引判断 loop / reflection / ask) を使い捨て子プロセス (`WorkerRunner` + `mission_worker`) に隔離し、preemption (SIGTERM→grace→SIGKILL) と「Mission 実行中も SL/TP 監視が止まらない」ロック粒度の再設計 (五相分解 + mission supervisor) を実装する。あわせてプラン 7 起票の B 束 (sandbox 増強・小口修正) と、プラン 5 レジャーの park 小口を返済する。

**Architecture:** `AgentRunner` 抽象に 3 つ目の実装 `WorkerRunner` を追加し、Mission 実行を「使い捨て子プロセス (`python -m agentic_fx.mission_worker`)」に隔離する。子は自前の読み取り専用 SQLite 接続 (`db.connect_readonly`) でツールを実行し、決定論部分 (claim/consume/Risk Gate/executor/finalize) は常に親が持つ。Mission 実行 (claim〜runner〜executor〜finalize) を **prepare → run → commit-pre → commit-core → commit-post** の五相へ再構成し、`core_lock` を保持するのは prepare と commit-core のみにする。scheduler の tick は「決定論ブロック (mark-to-market〜exits) を内部順序不変のまま先頭で実行 → データ hooks → Mission 起動判定 (`try_submit`)」に再編し、Mission 本体は単一スロットの `MissionSupervisor` スレッドが直列実行する。停止は「新規受付停止 → supervisor drain → scheduler join 先行 → worker 終了+supervisor join → 資源 close (逆順)」の状態機械として一意に定義し、実行主体は常に main スレッドにする。

**Tech Stack:** Python 3.12 / uv / pytest / sqlite3 / subprocess (`start_new_session=True` + `killpg`) / `ctypes` (Landlock syscall) / threading (`Lock`/`Event`/`Thread`/`Future` 相当の完了 queue)

**プラン規約 (マルチエージェント SDD — CLAUDE.md「実装体制」節に準拠。プラン 6/7 の「実装 sonnet + 全 task 停止」から変更):**
- 全体指揮 + 実装監督: opus (main セッション)。実装担当: haiku (機械的 task = Task 1-4, 9, 11, 17 目安) / codex (重量 task = Task 7, 10, 13-16, 19 目安)。レビュー: sonnet + codex 交差 (codex 実装分は sonnet 主査)。変異検証: haiku 並列 fan-out
- 大 task はテスト転写と実装転写を並列執筆し、統合 + red/green 実行は 1 レーン直列 (red 先行観測は統合役の実行順序で担保)。小 task は丸ごと 1 agent
- 依存の浅い task 束は worktree 並列。**ユーザーへの節目確認は task 単位ではなく束単位**
- **変異テストの前後で `__pycache__` を必ず削除する** (2026-08-07 Task 5 で実測した罠)。手順: `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +`。**受入条件の最終 `uv run pytest -q` も pyc 削除後に実行する**
  - 理由: Python は `.pyc` の再利用可否を「元ソースの mtime + サイズ」だけで判定する。変異が**同バイト数の書き換え** (`mode=ro`↔`mode=rw`、`==`↔`!=`、`>=`↔`<=` 等) で、かつ revert が**同一秒内**に起きると、`.py` を戻しても `.pyc` が更新されず、**以降のテストが変異したままのコードで走り続ける**
  - 実測: Task 5 の Mutation 2 (`mode=ro` → `mode=rw`) がこの条件を満たし、指揮者の検証で `test_connect_readonly_rejects_write` が偽の FAILED になった。`__pycache__` 削除後は 1442 全 green。`git diff` はクリーンなので**差分検査では絶対に検出できない**
  - レビュアーが変異検証をする場合も同じ。「revert 後に `git status`/`git diff` がクリーン」は pyc の健全性を保証しない
- **変異を注入したら、必ず該当行を `grep -n` / `sed -n` で表示し、意図した改変が入ったことを目視確認してから pytest を実行する** — 実装者・レビュアー・**指揮者のすべてに適用**。空振りした変異や意図と違う変異は「テストが緑 = 防御あり」という誤った確信を生む。実測: 指揮者が `deepcopy\((\w+)\)` の置換で `copy.deepcopy(msg)` を `copy.msg` に変えてしまい (= `AttributeError` で sink 全体を壊す変異)、「7 件落ちた = 保護が広く効いている」と誤読した。正しくは deepcopy 除去単独では 1 件しか落ちない
- **変異は 2 方向に広げる** (どちらも実測で穴が見つかった):
  - **同じ防御を複数の壊し方で** — 呼び出しを消す / 引数を消す / 値を None にする / 引数の中身を空にする / 条件を反転する。Task 5 の 2 周目で、実装者が「変異テスト済み」と報告した回帰テストが `"provider" in kwargs` とキーの存在しか見ておらず、**値を `None` にする変異 (= 元のバグの症状そのもの) を検出できなかった**
  - **防御の適用範囲全体に** — 防御が N 箇所に適用されるなら N 箇所すべてに当てる。Task 6 で `LocalRunner` の 6 つの append site を sink に集約したが、テストは 2 経路しか通しておらず**残り 4 経路の迂回変異が生存**した。**指揮者も 1 site で red を見て「集約は守られている」と誤判断している** — 1 site の red はその 1 site の防御しか示さない
- **各 step に書かれた変異リストは「下限」であって天井ではない** (Task 2 レビューで確定した運用規則)。実装者は「この task が守ろうとしている防御・性質」ごとに、それを外して red になるかを自分で 1 件ずつ確かめ、リストに無い変異を追加したら報告する。既存テストを弱める変更をする場合は、等価以上の代替ピンを同時に用意して報告する
  - 根拠 (実測): Task 2 の実装者はプラン記載の変異 5 件を全て実測して red を確認したが、リストに無かった `--noconftest` の多層防御は無防備で、削除しても `tests/plugin/` の 201 テストが green のままだった (sonnet 副査が実測検出)
- **各 task のレビューは 2 周構成** (2026-08-07 ユーザー裁定、Task 5 から適用)。1 周目 = codex 主査 + sonnet 副査の交差 → 指揮者裁定 → 修正ラウンド → **2 周目 = codex による差分限定レビュー** (修正コミットの diff のみ。Claude 週枠ゼロ、実測 20k 前後)。**2 周目で重大な問題が出た場合のみ sonnet でもクロスレビュー**する
  - **当面は 2 周目も codex + sonnet のクロスで実施する** (2026-08-07 ユーザー裁定)。Task 5/6 の実測 2 件では判断材料が足りないため、データを蓄積してから「常時クロス」か「条件付き」かを確定する。**各 task で 2 周目のコストと単独検出の有無をレジャーに記録すること**
  - 実測済み: Task 5 = codex 21.5k (I2/M1) + sonnet 63.4k (I3/M1)、**単独検出が両方向にあり**。Task 6 = codex 22.2k (I1) + sonnet 105.1k (I1/M2)、**Important は両者一致**で sonnet の追加は根本原因の深さと Minor 2 件。**「2 周目は軽い」は成り立たない** (Task 6 の sonnet は 1 周目の 143%)
  - 将来「条件付き」に移す場合の閾値案 (未確定): ①codex が **Critical** を出した ②**設計裁定・Global Constraints に関わる Important** を出した ③修正が**新たな製品挙動の変更**を持ち込んでいた
  - **1 周目・2 周目のクロスレビューと修正は、指揮者がユーザーの許可を得ずに実施してよい** (2026-08-08 ユーザー裁定)。節目確認はマージ前に一度だけ行う
  - **【2026-08-08 夜 確定体制】以降の task レビューは `sonnet` + `qwen3-coder-30b` + `KAT-Coder-V2.5-Dev` の 3 者**とする (ユーザー裁定)。codex は残 2% を**総合レビュー用に温存**し、task レビューには使わない
    - **較正の最終結果 (Task 10 の既知欠陥 5 件で採点)**: `qwen3-coder-30b` **5/5** (86s) / **`KAT-Coder-V2.5-Dev` Q4_K_M 4/5** (71s) / glm-4.7-flash 3/5 / gemma-4-31b 3/5 / qwen3.6-35b (alias 既定) 2/5 / **sonnet 1/5** (16 分)。gpt-oss-20b は think loop で失格
    - **KAT-Coder-V2.5-Dev は arch が `qwen35moe`** (現行 Qwen3.6-35B と同一) なので **KV = 80 KiB/token** がそのまま適用でき、**Q4_K_M (21.4GB) は ctx 64k で残 +5.6GB** と現行 Q5 (残 +2.2GB) より余裕がある
    - **ローカルは常に 1 本**ずつ実行する (llama-swap のロード競合)。**モデル比較は逐次**
    - **【2026-08-08 夜 確定】ローカルは今後も `qwen3-coder-30b` + `KAT-Coder-V2.5-Dev` の 2 本体制**とする (ユーザー裁定。コストゼロなので落とす理由がない)。**Task 11 の同一プロンプト直接比較で、KAT の指摘が qwen を完全に包含**した (qwen 9 件は全て KAT 8 件に含まれ、KAT のみ 3 件独自) ため、**KAT は下位互換ではない**
      - **KAT のみが出した観点**: ①原子性テストが **SQLite trigger 依存**で、失敗点が Python 側に変われば素通りする (Critical) ②ロック寿命を **`App.instance_lock` / `App.close()` の配線**として見た (Important) ③`max_requeue` の境界 (`>=` vs `>`) が片側しか踏まれていない (Minor)。①②は本プロジェクトの**再発欠陥クラス**そのもの
      - **qwen は重複が多い** (Task 11 では 9 件中 3 組が実質重複 → 6 件相当)。**KAT は重複ゼロ**。ただし qwen は誤検出が少ない
      - **共通の限界**: プロンプトに入れた材料の外は見えないので、指摘は「そうなっていたら壊れる」という**仮定形**で止まる。**裏取りは必ず指揮者が行う**
    - **ローカルの指摘はそのまま採用してはいけない。** Task 10 の 2 周目で `qwen3-coder-30b` が Important 2 件のうち **1 件を誤って指摘**した (下限 assert の水増しを懸念したが、変異時の実測値と閾値は 2 桁違い、修正案は逆に偽 fail を増やす内容だった)。**指揮者が変異で裏を取る前提**でのみ価値がある
    - **【2026-08-08 夕 codex 残 2%】**総合レビュー用に温存し、**以降の task レビューには使わない**。代わりに**ローカル LLM を常設の 3 人目**にする (較正実測の結果、下記)
    - **較正実測 (Task 10 の 1 周目、既知の生存変異 5 件で採点)**: **`qwen3-coder-30b-a3b-instruct` が 5/5 的中・誤検出ゼロ・86 秒・コストゼロ**。glm-4.7-flash と gemma-4-31b-it-qat が 3/5、alias 既定の qwen3.6-35b が 2/5、**sonnet は 1/5**。gpt-oss-20b は think loop で失格 (`reasoning_len=32603` に対し `content_len=1281`)
    - **役割分担 (実測に基づく)**: **機械的照合 (テストが防御を検出するか) = `qwen3-coder-30b`** / **文脈依存 (他 task の申し送り・設計整合・配線) = sonnet**。sonnet だけが「`tool_rpc_result` の seq は 2 から始まる」(Task 7 の申し送り)・「`RagUnavailable` が transcript に漏れる」(Task 9 の申し送り) を出せた — **ローカルには他 task の文脈が渡らないので構造的に不可能**
    - **ローカル LLM の実行方法**: Claude Code 経由は**不可** (起動時プロンプトだけで 66k トークン、モデル窓 64k を超過)。**直接 API** (`http://127.0.0.1:8080/v1/chat/completions`) に**指揮者が材料を絞って渡す** (実装 + テスト + 観点で 12k トークン)。**`reasoning_effort: "none"` が必須** (think loop 対策。無いと max_tokens 全部を思考に使い本文ゼロ) + Qwen 推奨サンプリング (`temperature 0.6 / top_p 0.95 / presence_penalty 1.0`)
    - **VRAM 制約 (実測)**: Arc B70 32GB。Qwen3.6-35B は 40 層 / KV ヘッド 2 / K=V=256 → **KV = 80 KiB/トークン**。ctx 64k で Q5 は 29.8 GiB (残 2.2)、**128k は Q5 -2.8 / Q4 +1.4 でいずれも実用外**。KV 量子化は SYCL で tg -41% のため不可
  - **【2026-08-08 codex 予算の逼迫による体制変更】codex の残容量が 4% (レビュー約 4 回分) になった。** 以降は codex を**全 task のレビューに使えない**。配分:
    - **codex を使う残り 4 回の内訳**: ①**総合レビュー** (プラン完了時、Task 20 後) に 1 回を必ず確保 ②残り 3 回を**資金保護に直結する最重量 task** に割り当てる — **Task 10 (WorkerRunner: preemption)** / **Task 15 (TradeLoop 五相: 資金保護の本体)** / **Task 19 (停止状態機械 + watchdog)** を第一候補とする。着手時に状況を見て指揮者が確定し、ユーザーへ報告する
    - **それ以外の task は sonnet 主査 + 指揮者検証**で回す。副査は置かない代わりに、**指揮者検証を実質的な第 2 レビュアーとして強化する** — Task 7〜9 の実測では、指揮者の変異検証が毎 task で実際に生存変異を検出しており (Task 8 で 2 件、Task 9 で 2 件)、レビュアー 1 名分の検出力に相当している
    - **交差の喪失は明示的なコストとして受け入れる**。実測では「codex と sonnet の Important は 8 task 連続で重複ゼロ〜わずか」であり、交差を省けば取り逃しが増える。**その分を総合レビューで拾う**前提で進める
  - **3 周目の要否は監視役 (ユーザー) が判断する** (2026-08-08 ユーザー裁定。旧規約「3 周目はやらない (打ち切り)」を改める)。指揮者は **2 周のレビューと修正を終えた時点で必ず停止**し、マージへ進まずに「3 周目が必要か」の判断材料を提示して指示を仰ぐ。判断材料には最低限これを含める: ①2 周目で何が見つかったか (特に**修正ラウンドが持ち込んだ欠陥**の有無) ②2 周目の修正の規模と性質 (挙動変更を含むか) ③未レビューのまま残る差分 ④変異テストの生存状況
    - 根拠: Task 7 の 2 周目で、**1 周目の修正が持ち込んだ新しい欠陥** (transport 失敗後の同一 seq 再送) が実際に見つかった。「修正ラウンドを誰もレビューしない」という穴は 1 段深いところにも同じ形で存在する — 打ち切り位置を指揮者が自動で決めるのは不適切
  - 根拠: 修正ラウンドの成果物を誰もレビューしていなかった。実際に Task 4 の修正後、指揮者が目視で「`ValueError` の直後に到達しない分岐が残る」欠陥を発見している (`cfd0bbd`)。プラン文書に対しては 2 周目を実施済み (Critical 0 / Important 3 を回収) で、コードに対してだけ非対称に省いていた
- **重大度が割れたら指揮者が再判定する** (2026-08-07 ユーザー裁定)。codex と sonnet の重大度評価は系統的にずれる — 実測: Task 2 の `--noconftest` 無防備は codex Important / sonnet Critical、Task 4 の生存変異 3 件は codex Critical / sonnet Important〜Minor と**逆方向にずれた**。両者の重大度は参考値とし、指揮者が現物 (設計書・コード) を確認して最終的な重大度と採否を決める。Task 3 の I-1 (設計書 §7 と突き合わせて「実装は正しい・文書が誤り」と裁定) がその実例
- エスカレーション: haiku 同一 task 2 回失敗 → codex/sonnet 再割当。割れた Critical は codex 反証要求 or ユーザー park
- レジャー: `.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/progress.md`

## 設計書からの委任事項 (writing-plans で確定させた 3 点 — §12 申し送り対応)

| # | 設計書の申し送り | 本プランでの確定 |
|---|---|---|
| 1 | N4-2: commit-pre/commit-core 間の exposure 増加時 fail-closed 分岐 + テスト | Task 15 (§「commit-core」節) で `required_currencies - set(snapshot.rates)` の非空チェックとして実装し、専用テストを置く。commit-pre は新設の `conn_supervisor` (lock 外の読取専用接続。§ Global Constraints 参照) で exposure pair 一覧を読む |
| 2 | rlimit 具体値の確定 + 通常起動の実測 | Task 7 で mission worker が実際に import する依存一式の VmPeak/VmRSS/fd 数を実測 (32 コア環境: VmPeak≈1.65GB, VmRSS≈123MB, fd=5) した上で `child_as_mb=4096`/`child_nofile=128`/`child_fsize_mb=8` を確定 (`WorkerSettings` として新設。Task 7 Step 7 に実測コマンドと結果を明記) |
| 3 | 分解書 (`2026-08-01-phase2-decomposition.md`) の「到達不能」表現への注記 | Task 18 の Step 1 で分解書 96/104 行付近に「到達不能 = 実行不能 (§4.6 の意味論。import 自体は Landlock のコードツリー読取許可により可能)」という 1 行注記を追記する |

## Global Constraints

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p` (サブスク認証) のみ。本プランは `ClaudeRunner` を実装しない (プラン 9) — `runner.trade.backend == "claude"` を worker 子側が検出した場合は `RuntimeError` で明示的に fail closed する (**Task 7** — `mission_worker.py` が子プロセス内で検出する。レビュー反映1回目 (裁定書 F-17/MN-1): 帰属記載の誤り (旧稿は「Task 10」と記載していたが、Task 10 は `WorkerRunner` 本体でありこの fail-closed 分岐の実装箇所ではない) を修正)
- **発注・SL 変更・クローズ・資金保護は LLM に委ねない。決定論的コードで強制する。** risk_gate.py / core/paper_broker.py の判定ロジックは本プランで **diff ゼロ** (§9 受入 7)。executor.py の変更は「判定ロジック不変・外部 I/O の取得位置のみ commit-pre へ移動」に限る
- **drawdown kill switch は config で無効化不可** — 本プランはこの経路 (`state.update(kill_switch_latched=True)`) に触れない
- 秘密情報は `.env` のみ。`config/settings.yaml` は gitignore、新キー追加時は `config/settings.yaml.example` と両方を同期する
- パッケージ管理 uv (`uv sync` / `uv run pytest` / `uv add`)。TDD (failing test → 実装 → green) を各 step で徹底する
- **接続契約 (本プラン新設 — 設計書 §3.1 の「conn_core は core_lock 保持中のみ触れる」を実装可能な形に具体化)**:
  - `app.conn_core` — **core_lock 保持中のみ**触れる (prepare 相・commit-core 相・scheduler tick)
  - `app.conn_shell` — シェルスレッド (`Commands`) 専用。本プランでは変更しない
  - `app.conn_supervisor` (本プラン新設) — **mission supervisor スレッドの lock 外相 (commit-pre) 専用の読取専用接続**。SQLite は WAL モードのため、書き込みトランザクション中でも別接続からの読み取りはブロックされない (`busy_timeout` はあるが、単発 SELECT が長時間ブロックされる想定はしない)。commit-pre はこの接続で「exposure pair 一覧」等のスナップショット読み取りのみ行い、書き込みは一切行わない
  - 子プロセス (`mission_worker`) は `db.connect_readonly` で開いた **専用の読み取り専用接続** を使う (親のいずれの接続とも別)
- **変異テスト (mutation testing) を各 task に含める**: 実装した分岐・条件・timeout 値を意図的に改変し、対応するテストが red になることを実装者・レビュアー双方が確認する (このプロジェクトの必須慣習 — 詳細は各 task の最終 step)
- テストに実スリープ (`time.sleep` での長待ち)・実 HTTP・実 git・乱数・実時刻を混入させない。**例外**: worker/E2E task の実 subprocess spawn・実 kill・実 rlimit・実 Landlock (これらは擬似できない対象そのものが検証対象のため) と、起動 timeout 等の実測で使う短時間 (数秒以内) の実スリープ
- コミットメッセージ末尾は `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (このプランの実行者は git commit を作成してよい — 親セッションが行うのはこのプラン**文書**の commit のみ)

## ファイル構成 (新設・変更の全体マップ)

```
src/agentic_fx/
├── mission_worker.py            # 子プロセスエントリ (Task 7,8) — python -m agentic_fx.mission_worker
├── core/
│   ├── landlock.py               # ctypes による Landlock syscall wrapper (Task 9)
│   ├── supervisor.py             # MissionSupervisor (Task 13, 15)
│   ├── scheduler.py               # tick 再編 (Task 12) — hooks 後段化 + on_signal_maintenance 統合
│   ├── executor.py                # snapshot 版 open エントリポイント追加 (Task 14)
│   └── ...
├── runners/
│   ├── worker_runner.py          # WorkerRunner (Task 10)
│   ├── local_runner.py           # sink 集約 + on_message コールバック (Task 6)
│   └── base.py                    # 変更なし (MissionResult 4 値契約は不変)
├── tools/
│   └── mission_registry.py       # build_mission_registry 抽出 (Task 5)
├── plugin/
│   ├── sandbox.py                  # pytest 隔離サブプロセス化 (Task 2)
│   └── worker.py                   # RLIMIT_NOFILE/FSIZE 追加 (Task 2)
├── store/
│   ├── db.py                       # connect_readonly (Task 5)
│   ├── missions.py                 # finish の CAS 化 + 起動時回収 (Task 11)
│   └── rag.py                      # 内部 lock + close() (Task 9)
├── loops/
│   ├── trade_loop.py               # 五相再構成 (Task 15)
│   └── reflection_cycle.py         # 五相再構成・共通化 (Task 16)
├── shell.py                        # readline 中断 seam (Task 17)
├── datafeed/
│   ├── fetchers.py                  # httpx timeout 注入 (Task 12)
│   └── econ_calendar.py             # httpx timeout 注入 (Task 12)
├── service.py                       # App.close 新設 + 全配線更新 (Task 19)
└── config.py                        # SupervisorSettings 新設 (Task 8, 12, 19 で追記)
config/settings.yaml.example          # 新設キー同期
tests/
├── core/test_supervisor.py, test_scheduler_tick_order.py, test_landlock.py
├── runners/test_worker_runner.py, test_mission_worker_protocol.py
├── loops/test_trade_loop_phases.py, test_reflection_cycle_phases.py
├── store/test_db_readonly.py, test_missions_cas.py, test_rag_lock.py
├── test_shell_interrupt.py
├── test_app_close.py
└── test_e2e_worker_isolation.py
```

依存方向は既存 (`loops → tools/core → store/datafeed`) を維持する。`mission_worker.py` はトップレベルモジュール (`plugin/worker.py` と対称の位置) — `src/agentic_fx/plugin/` 配下に置かないのは、plugin サンドボックスと mission 隔離が別の脅威モデル/権限境界を持つため (設計書 §4.5「ネットワーク毒入れはしない」— plugin worker とは異なる)。

---
### Task 1: 公開昇格 rename (`_parse_timeframe` / `_pair_param`)

冒頭 1 コミット (設計書 §7 / プラン 7 最終レビュー裁定どおり)。両関数は既に他モジュールから private 名のまま import されている (`strategy_adapter.py` が `runner._parse_timeframe` を、`signal_tools.py` が `market_tools._pair_param` を import) — 実質的に公開 API 化しているのに private 命名のままな状態を解消する。挙動は一切変えない (rename のみ)。

**Files:**
- Modify: `src/agentic_fx/backtest/runner.py:84`(定義)`,150`(呼び出し)
- Modify: `src/agentic_fx/plugin/strategy_adapter.py:18`(docstring 内 3 箇所)`,50`(import)`,93-96`(docstring + 呼び出し)
- Modify: `src/agentic_fx/tools/market_tools.py:18`(定義)`,94`(呼び出し)
- Modify: `src/agentic_fx/tools/signal_tools.py:124`(呼び出し)
- Modify: `src/agentic_fx/plugin/approval.py:88`(コメント内の関数名言及のみ)
- Test: 既存テスト (`tests/backtest/test_runner.py`, `tests/plugin/test_strategy_adapter.py`, `tests/tools/test_tool_impls.py`, `tests/tools/test_signal_tools.py`) は private 名を直接 import していないため変更不要 — 既存スイート green であることが本 task の受入条件

**Interfaces:**
- Produces: `agentic_fx.backtest.runner.parse_timeframe(tf: str) -> timedelta` (旧 `_parse_timeframe`)、`agentic_fx.tools.market_tools.pair_param(settings: Settings) -> dict` (旧 `_pair_param`)。シグネチャ・挙動は無変更、名前のみ変更
- Consumes: なし (rename のみ、新規依存なし)

- [ ] **Step 1: 既存テストが green であることを確認するベースライン取得**

```bash
uv run pytest tests/backtest/test_runner.py tests/plugin/test_strategy_adapter.py tests/tools/test_tool_impls.py tests/tools/test_signal_tools.py -q
```

Expected: 全 PASS (rename 前のベースライン)。

- [ ] **Step 2: `runner.py` の rename**

`src/agentic_fx/backtest/runner.py:84` を以下に変更 (`_parse_timeframe` → `parse_timeframe`):

```python
def parse_timeframe(tf: str) -> timedelta:
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"unsupported eval_timeframe: {tf!r}")
    n, unit = int(m.group(1)), m.group(2)
    if n == 0:
        # fix round 1 F7: "0m"/"0h" は正規表現には通るが tf=timedelta(0) と
        # なり、後段の `(now - _EPOCH) % tf` が ZeroDivisionError になる。
        raise ValueError(f"eval_timeframe must be > 0: {tf!r}")
    return timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
```

`runner.py:150` の呼び出し `tf = _parse_timeframe(eval_timeframe)` を `tf = parse_timeframe(eval_timeframe)` に変更する。

- [ ] **Step 3: `strategy_adapter.py` の呼び出し元更新**

`src/agentic_fx/plugin/strategy_adapter.py:50` の import を変更:

```python
from agentic_fx.backtest.runner import parse_timeframe
```

`strategy_adapter.py:96` の呼び出しを変更:

```python
        width = parse_timeframe(closed_bar.interval)
```

`strategy_adapter.py` のモジュール docstring (18 行目付近) とメソッド docstring (93-94 行目) 内の `runner._parse_timeframe`/`_parse_timeframe` という文言をすべて `runner.parse_timeframe`/`parse_timeframe` に置換する (コメントの整合性維持 — 挙動には影響しないが、次に読む人が private 名を探して迷わないようにする)。

- [ ] **Step 4: `market_tools.py` の rename**

`src/agentic_fx/tools/market_tools.py:18` を変更:

```python
def pair_param(settings: Settings) -> dict:
    """pair と timeframe の enum は設定から動的に作る (足・通貨ペアを固定しない方針)。"""
    return {"pair": {"enum": list(settings.pairs), "description": "e.g. USDJPY"},
            "timeframe": {"type": "string",
                          "enum": list(settings.datafeed.intervals)}}
```

`market_tools.py:94` の呼び出しを `pair_param = pair_param(settings)` ではなく変数名衝突を避けて次のように変更する (元コードは `pair_param = _pair_param(settings)` で変数名と関数名が偶然別だった — rename 後は関数名 `pair_param` と変数名が衝突するため、呼び出し側の変数名を `pair_schema` に変更する):

```python
    pair_schema = pair_param(settings)
    return [
        ToolDef("get_ohlcv", "OHLCV 価格データ (直近 100 本)",
                {"type": "object", "properties": pair_schema,
                 "required": ["pair", "timeframe"]}, get_ohlcv),
```

(以降 `market_tools.py` 内で `pair_param` 変数を参照している箇所があれば同じ変数名 `pair_schema` に統一すること — `build` 関数内を `grep -n "pair_param"` で確認し、変数参照をすべて `pair_schema` に置換する。)

- [ ] **Step 5: `signal_tools.py` の呼び出し元更新**

`src/agentic_fx/tools/signal_tools.py:124` を変更:

```python
    pair_schema = market_tools.pair_param(settings)["pair"]
```

- [ ] **Step 6: `approval.py` のコメント更新**

`src/agentic_fx/plugin/approval.py:88` 付近のコメント `runner._parse_timeframe が "1d" を受理しないため` を `runner.parse_timeframe が "1d" を受理しないため` に変更する (コードではなくコメントのみ)。

- [ ] **Step 7: 全体 green を確認**

```bash
uv run pytest -q
```

Expected: 全件 PASS (rename 前と同じテスト数・結果)。

- [ ] **Step 8: 変異テスト (rename の正しさをテストが検出できることの確認)**

`market_tools.py` の `pair_schema = pair_param(settings)` を一時的に `pair_schema = {}` に改変して `uv run pytest tests/tools/test_tool_impls.py -q` を実行し、`get_ohlcv`/`get_indicators` の schema 検証テストが red になることを確認する (既存テストが pair enum の中身を見ていることのピン)。確認後、改変を元に戻す。

- [ ] **Step 9: Commit**

```bash
git add src/agentic_fx/backtest/runner.py src/agentic_fx/plugin/strategy_adapter.py \
  src/agentic_fx/plugin/approval.py src/agentic_fx/tools/market_tools.py \
  src/agentic_fx/tools/signal_tools.py
git commit -m "$(cat <<'EOF'
refactor: _parse_timeframe/_pair_param を公開昇格 (rename のみ、挙動不変)

プラン7で既に private 名のままクロスモジュール import されていた
2 関数を公開名に揃える (設計書 §7)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 2: B 束 — plugin サンドボックス増強 (pytest 完全隔離 + RLIMIT_NOFILE/FSIZE + PluginSession スレッド安全性)

設計書 §7 表の 3 項目をまとめる (いずれも `plugin/` パッケージ内の小口修正)。

**現状確認**: `approval.py:_default_pytest_runner` (130-162 行) は既に `subprocess.Popen(start_new_session=True)` + timeout + `_kill_process_group` でプロセス隔離済みだが、①`env=` を渡していない (親の全環境変数 — `AFX_*` 等の秘密を含みうる — をそのまま継承) ②resource limit が一切無い ③ネットワーク毒入れが無い、の 3 点が未対応。`worker.py:_set_resource_limits` (77-91 行) は RLIMIT_CPU/RLIMIT_AS/RLIMIT_NPROC のみで RLIMIT_NOFILE/RLIMIT_FSIZE が無い。`sandbox.py:PluginSession` は単一スレッド前提(docstring 上も実装上も)だが、それを破る呼び出しを実行時に検出する assert が無い。

**設計判断 (writing-plans)**: pytest 実行専用のネットワーク毒入れは、`worker.py` の `_poison_network_modules()` を**そのまま再利用**する新設エントリモジュール `plugin/pytest_sandbox_entry.py` から呼ぶ (`python -m pytest` を直接 spawn するのをやめ、`python -m agentic_fx.plugin.pytest_sandbox_entry <test_plugin_path>` を spawn する)。resource limit は pytest サブプロセスにも plugin worker と同じ 2 設定キー (`sandbox_nofile`/`sandbox_fsize_mb`、新設) を使い回す — pytest 用に別キーを増やすと「B 束の小口項目」の規模を超える。

**Files:**
- Modify: `src/agentic_fx/plugin/worker.py:77-91` (`_set_resource_limits` に RLIMIT_NOFILE/RLIMIT_FSIZE 追加)
- Create: `src/agentic_fx/plugin/pytest_sandbox_entry.py` (pytest 実行専用エントリ — ネットワーク毒入れ後に `pytest.main()` を呼ぶ)
- Modify: `src/agentic_fx/plugin/approval.py:130-162` (`_default_pytest_runner` を `pytest_sandbox_entry` 経由の spawn + 最小 env + rlimit `preexec_fn` に変更)
- Modify: `src/agentic_fx/plugin/sandbox.py:288-307`(`PluginSession.__init__`)`,309-341`(`__enter__`)`,406-424`(`call`)`,383-386`(`close`) — owner thread assert
- Modify: `src/agentic_fx/config.py` (`PluginSettings` に `sandbox_nofile`/`sandbox_fsize_mb` 追加)
- Modify: `config/settings.yaml.example` (同期)
- Test: `tests/plugin/test_worker.py` (RLIMIT_NOFILE/FSIZE 追加テスト。**存在しないので新規作成する** — Step 1 の指示に従う。指揮者確認済み: `tests/plugin/` の実体は test_approval / test_loader / test_sandbox / test_signal_eval / test_signal_producer / test_strategy_adapter のみ), `tests/plugin/test_approval.py` (pytest_runner env/rlimit/poison テスト追記), `tests/plugin/test_sandbox_thread_safety.py` (新規)

**Interfaces:**
- Produces:
  - `worker._set_resource_limits(cpu_sec: int, memory_mb: int, nofile: int, fsize_mb: int) -> None` (シグネチャ拡張 — 呼び出し元 `worker.main()` の handshake dict に `nofile`/`fsize_mb` キーを追加。**破壊的変更**: `sandbox.py:PluginSession.__enter__` の handshake dict 構築箇所 (357-361 行) も同時に更新する)
  - `pytest_sandbox_entry.main() -> None` — `sys.argv[1]` (test_plugin.py の絶対パス文字列) を受け取り、`agentic_fx.plugin.worker._poison_network_modules()` を呼んでから `pytest.main(["-p", "no:cacheprovider", "--noconftest", "-q", sys.argv[1]])` の returncode で `SystemExit` する
  - `approval._pytest_rlimit_preexec(memory_mb: int, nofile: int, fsize_mb: int) -> Callable[[], None]` — `subprocess.Popen(preexec_fn=...)` に渡す純関数ファクトリ (単体テストで直接検証可能)
  - `PluginSession` に `self._owner_thread: int` (construction 時に `threading.get_ident()` で記録)。`call()`/`close()`/`__enter__()` の先頭で `self._check_owner_thread()` を呼び、不一致なら `RuntimeError` を送出する (`SandboxError` ではなく `RuntimeError` — スレッド越境はプログラミングエラーであり plugin コード起因の実行時エラーと区別する)

- [ ] **Step 1: 失敗するテストを書く (rlimit 拡張)**

`tests/plugin/test_worker.py` (新規、無ければ作成) に以下を追加する:

```python
"""worker.py の rlimit 拡張 (プラン 8 B 束) の単体テスト。"""
from __future__ import annotations

import resource

import pytest

from agentic_fx.plugin.worker import _set_resource_limits


def test_set_resource_limits_sets_nofile_and_fsize():
    """RLIMIT_NOFILE/RLIMIT_FSIZE が指定値ちょうどに設定される。

    実プロセスの現在の rlimit を破壊しないよう、テスト後に元へ戻す
    (RLIMIT_CPU/RLIMIT_AS は既存 test_sandbox.py の実測パターンに揃え、
    ここでは NOFILE/FSIZE のみを対象にする — CPU/AS を実際にこのテスト
    プロセスへ適用すると pytest 自体の実行を壊しかねないため、資源制限は
    別プロセス (test_sandbox.py の実 subprocess テスト) で検証し、ここは
    「setrlimit が正しい引数で呼ばれること」を fake 経由で検証する)。
    """
    calls: list[tuple[int, tuple[int, int]]] = []

    def fake_setrlimit(which, limits):
        calls.append((which, limits))

    import agentic_fx.plugin.worker as worker_mod
    orig = resource.setrlimit
    try:
        worker_mod.resource.setrlimit = fake_setrlimit  # type: ignore[attr-defined]
        _set_resource_limits(cpu_sec=60, memory_mb=512, nofile=128, fsize_mb=8)
    finally:
        worker_mod.resource.setrlimit = orig

    kinds = {which for which, _ in calls}
    assert resource.RLIMIT_NOFILE in kinds
    assert resource.RLIMIT_FSIZE in kinds
    nofile_limit = next(v for w, v in calls if w == resource.RLIMIT_NOFILE)
    assert nofile_limit == (128, 128)
    fsize_limit = next(v for w, v in calls if w == resource.RLIMIT_FSIZE)
    assert fsize_limit == (8 * 1024 * 1024, 8 * 1024 * 1024)
```

Note: `worker.py` は現在 `_set_resource_limits` 内で `import resource` (ローカル import、77 行目直後) をしている。テストで `worker_mod.resource` を monkeypatch できるようにするため、Step 3 の実装では **モジュールトップレベルで `import resource` に変更する** (ローカル import のままだと `worker_mod.resource` という属性が存在せず fake を差し込めない)。

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_worker.py -q
```

Expected: FAIL (`_set_resource_limits() got an unexpected keyword argument 'nofile'` または `ImportError`)。

- [ ] **Step 3: `worker.py` を実装**

`src/agentic_fx/plugin/worker.py` の import 節 (54-57 行目) に `import resource` を追加する (トップレベル化 — Step 1 の理由)。`_set_resource_limits` (77-91 行) を以下に置き換える:

```python
def _set_resource_limits(cpu_sec: int, memory_mb: int, nofile: int,
                          fsize_mb: int) -> None:
    cpu = int(cpu_sec)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    mem_bytes = int(memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    # プラン 8 B 束: worker は plugin.py の import と call() の応答書き込み
    # 以外にファイル記述子を要しない (stdin/stdout/stderr の 3 つ +
    # import 時の一時的な .so/.pyc オープン)。想定外の大量オープン
    # (fork bomb 的 fd リーク) を検知する上限として十分寛大な値を渡す
    # (呼び出し元が settings.plugin.sandbox_nofile を渡す — 既定 128)。
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))

    # RLIMIT_FSIZE: plugin コードは check_source の denylist
    # (open/to_*/read_* 等) により意図的なファイル書き込みができない —
    # ここでの上限は「想定外の書き込みを小さく抑える」多層防御 (呼び出し
    # 元が settings.plugin.sandbox_fsize_mb を渡す — 既定 8MB)。
    fsize_bytes = int(fsize_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))

    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (_NPROC_CAP, _NPROC_CAP))
    except (ValueError, OSError):
        # per-uid の既存使用量次第では失敗し得る (最終防衛線ではなく
        # ベストエフォートの追加防御 — CPU/AS の 2 軸が主防御)。
        pass
```

`worker.py:main()` (174-198 行付近) の `_set_resource_limits(handshake["cpu_sec"], handshake["memory_mb"])` 呼び出しを以下に変更する:

```python
        _set_resource_limits(handshake["cpu_sec"], handshake["memory_mb"],
                              handshake["nofile"], handshake["fsize_mb"])
```

`src/agentic_fx/plugin/sandbox.py:357-361` の handshake 構築を以下に変更する (`PluginSession.__enter__` 内):

```python
            handshake = {
                "cpu_sec": self._settings.sandbox_session_cpu_sec,
                "memory_mb": self._settings.sandbox_memory_mb,
                "nofile": self._settings.sandbox_nofile,
                "fsize_mb": self._settings.sandbox_fsize_mb,
                "kind": self._meta.kind,
            }
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_worker.py -q
```

Expected: PASS。既存 `tests/plugin/test_sandbox.py` の handshake 送出テスト (`import os` reject 等) が dict の追加キーで壊れていないことも合わせて確認する:

```bash
uv run pytest tests/plugin/test_sandbox.py -q
```

Expected: PASS (fixture が固定 dict で handshake を検証している場合は `nofile`/`fsize_mb` キーの追加を反映する)。

- [ ] **Step 5: `PluginSettings` に新設定キーを追加**

`src/agentic_fx/config.py` の `PluginSettings` クラス (170-213 行) に以下を追加する (`sandbox_output_max_bytes` の直後):

```python
    # RLIMIT_NOFILE — worker プロセスが同時に開けるファイル記述子数の上限。
    # plugin コードは check_source の denylist によりファイルを開けない
    # ため、想定外の大量オープン (fd リーク) を検知する多層防御。
    sandbox_nofile: int = Field(ge=1, default=128)
    # RLIMIT_FSIZE (MiB) — 1 ファイルあたりの書き込みサイズ上限。plugin
    # コードは denylist により意図的な書き込みができないため、想定外の
    # 大量書き込みを小さく抑える多層防御。pytest サブプロセス (approval.py
    # の test_plugin.py 実行) にも同じ 2 値を流用する。
    sandbox_fsize_mb: int = Field(ge=1, default=8)
```

`config/settings.yaml.example` の `plugin:` セクション (`sandbox_output_max_bytes` の直後) に同期する:

```yaml
  sandbox_nofile: 128           # RLIMIT_NOFILE: worker が同時に開けるファイル記述子数の上限
  sandbox_fsize_mb: 8           # RLIMIT_FSIZE (MiB): 1 ファイルあたりの書き込みサイズ上限。pytest サブプロセスにも流用
```

- [ ] **Step 6: 失敗するテストを書く (pytest サブプロセスの env/rlimit/poison)**

`tests/plugin/test_approval.py` に以下を追加する (既存の `pytest_runner` 注入テストと同じ fixture 構成を踏襲する — 実ファイルを開いて既存 import/fixture パターンを確認してから追記すること):

```python
def test_pytest_rlimit_preexec_sets_expected_limits(monkeypatch):
    """approval._pytest_rlimit_preexec が返す関数が正しい rlimit 呼び出しをする。"""
    import resource

    from agentic_fx.plugin import approval

    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        approval.resource, "setrlimit",
        lambda which, limits: calls.append((which, limits)))

    fn = approval._pytest_rlimit_preexec(memory_mb=256, nofile=64, fsize_mb=4)
    fn()

    kinds = {w: v for w, v in calls}
    assert kinds[resource.RLIMIT_AS] == (256 * 1024 * 1024, 256 * 1024 * 1024)
    assert kinds[resource.RLIMIT_NOFILE] == (64, 64)
    assert kinds[resource.RLIMIT_FSIZE] == (4 * 1024 * 1024, 4 * 1024 * 1024)


def test_default_pytest_runner_uses_minimal_env(monkeypatch, tmp_path):
    """_default_pytest_runner が subprocess.Popen に最小 env を渡す
    (AFX_* 等の秘密が子へ伝播しない)。"""
    from agentic_fx.plugin import approval

    captured: dict = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout):
            return "1 passed", ""

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["preexec_fn"] = kwargs.get("preexec_fn")
        return FakeProc()

    monkeypatch.setenv("AFX_SECRET_TOKEN", "must-not-leak")
    monkeypatch.setattr(approval.subprocess, "Popen", fake_popen)

    test_plugin_path = tmp_path / "test_plugin.py"
    test_plugin_path.write_text("def test_x():\n    assert True\n")
    approval._default_pytest_runner(test_plugin_path)

    assert "AFX_SECRET_TOKEN" not in captured["env"]
    assert captured["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert captured["args"][:3] == [
        approval.sys.executable, "-m", "agentic_fx.plugin.pytest_sandbox_entry"]
    assert captured["preexec_fn"] is not None
```

- [ ] **Step 7: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_approval.py -q -k "rlimit_preexec or minimal_env"
```

Expected: FAIL (`AttributeError: module 'agentic_fx.plugin.approval' has no attribute '_pytest_rlimit_preexec'` 等)。

- [ ] **Step 8: `pytest_sandbox_entry.py` を新規作成**

```python
"""test_plugin.py 実行専用のサブプロセスエントリ (プラン 8 B 束)。

approval.py の `_default_pytest_runner` が spawn する唯一の想定呼び出し元。
`python -m pytest` を直接 spawn するのをやめてこのモジュールを経由させる
理由: pytest がテストモジュール (test_plugin.py 経由で plugin.py) を
import する**前**にネットワーク毒入れを適用するため。resource limit
(RLIMIT_AS/NOFILE/FSIZE) は起動側 (approval.py の `preexec_fn`) が
fork 直後・exec 直前に設定済みの前提で、ここでは毒入れと pytest 起動のみ
行う。
"""
from __future__ import annotations

import sys


def main() -> None:
    from agentic_fx.plugin.worker import _poison_network_modules
    _poison_network_modules()

    import pytest

    args = ["-p", "no:cacheprovider", "--noconftest", "-q", *sys.argv[1:]]
    raise SystemExit(pytest.main(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: `approval.py` を実装**

`src/agentic_fx/plugin/approval.py` の import 節 (55-72 行) に `import resource` を追加する。`_default_pytest_runner` (130-162 行) を以下に置き換える:

```python
def _pytest_rlimit_preexec(memory_mb: int, nofile: int,
                            fsize_mb: int) -> Callable[[], None]:
    """`subprocess.Popen(preexec_fn=...)` に渡す純関数ファクトリ。

    fork 直後・exec 直前に子プロセス側で実行される (Unix 専用 API —
    本プロジェクトの動作環境は Linux 前提)。plugin worker (worker.py の
    `_set_resource_limits`) と同じ 2 値 (settings.plugin.sandbox_nofile/
    sandbox_fsize_mb) を pytest サブプロセスにも適用する。
    """
    def _fn() -> None:
        mem_bytes = int(memory_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))
        fsize_bytes = int(fsize_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    return _fn


def _default_pytest_runner(test_plugin_path: Path, *,
                            settings: "PluginSettings" | None = None,
                            ) -> dict[str, Any]:
    """既定の pytest 実行シーム。

    **1 plugin につき 1 サブプロセス** で実行する (サンプル test_plugin.py
    のモジュール名衝突回避)。`agentic_fx.plugin.pytest_sandbox_entry`
    経由で spawn する (ネットワーク毒入れを pytest のテスト収集より前に
    適用するため — モジュール docstring 参照)。**最小 env** (`sandbox.
    _build_env()` を再利用 — `AFX_*` 等の秘密を含む親 env を継承しない)
    + **resource limit** (`_pytest_rlimit_preexec`) を適用する。

    `settings` が None の場合は plugin サンドボックスの既定値
    (`PluginSettings()` のデフォルト) を使う — 呼び出し元 (`submit_plugin`)
    は実際の `settings.plugin` を渡す。

    timeout 発生時は `_kill_process_group` でプロセスグループごと回収する。
    """
    from agentic_fx.plugin.sandbox import _build_env
    eff_settings = settings if settings is not None else PluginSettings()
    env = _build_env()
    # FC-3 対応: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` はサードパーティ
    # プラグインの setuptools entry-point 自動読込のみを止める (pytest
    # 本体に同梱される builtin プラグインは対象外で、通常の収集・実行は
    # 引き続き機能する)。これが無いと `anyio` 等の entry-point プラグイン
    # が pytest.main() 実行中に (毒入れ済みの) `socket` を import しようと
    # して `ImportError` になり、poison 後は毎回 exit=1 で test_plugin.py
    # の承認が全滅する (実測で確認済み — poison を pytest 起動前に適用する
    # 設計上、entry-point プラグインの自動読込そのものを止める以外に
    # 安全な回避策が無い)。
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_fx.plugin.pytest_sandbox_entry",
         str(test_plugin_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True, env=env,
        preexec_fn=_pytest_rlimit_preexec(
            memory_mb=eff_settings.sandbox_memory_mb,
            nofile=eff_settings.sandbox_nofile,
            fsize_mb=eff_settings.sandbox_fsize_mb))
    try:
        stdout, stderr = proc.communicate(timeout=_PYTEST_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        raise ValueError(
            f"test_plugin.py timed out after {_PYTEST_TIMEOUT_SEC}s "
            f"({test_plugin_path})") from exc
    return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr}
```

`approval.py` の import 節に `from agentic_fx.config import PluginSettings` を追加する (`TYPE_CHECKING` ブロックに既にあるかを確認し、実行時に必要なため通常 import へ昇格すること — 既存の `TYPE_CHECKING` import と重複しないよう既存コードを確認してから編集する)。

`submit_plugin` (221-236 行) 内の `runner = pytest_runner if pytest_runner is not None else _default_pytest_runner` の呼び出し箇所 (268 行 `pytest_result = runner(test_plugin_path)`) を、`pytest_runner` が None の場合に settings を束縛したラムダを使うよう変更する:

```python
        runner = (pytest_runner if pytest_runner is not None
                  else lambda p: _default_pytest_runner(p, settings=settings.plugin))
        pytest_result = runner(test_plugin_path)
```

- [ ] **Step 10: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_approval.py -q
uv run pytest tests/plugin/ -q
```

Expected: 全件 PASS。

- [ ] **Step 11 (FC-3 対応): 毒入れ後に pytest が実際に起動できることを実 subprocess で確認する**

Step 6-10 のテストはすべて `subprocess.Popen` を fake に差し替えており、`_default_pytest_runner` が実際に spawn する `pytest_sandbox_entry` プロセスが**本当に collection まで到達して exit=0 を返すか**を一度も検証しない。`pytest.main(["--version"])` だけの確認では entry-point プラグイン (`anyio` 等) の自動読込は発火しないため不十分 — 実際に `-q <test_plugin.py>` で **収集・実行させる** ことを必須とする。`tests/plugin/test_approval.py` に以下を追加する:

```python
def test_default_pytest_runner_real_subprocess_exits_zero_after_poison(tmp_path):
    """モックなしで `_default_pytest_runner` を実行し、ネットワーク毒入れ後
    でも pytest が実際に収集・実行を完了して exit=0 を返すことを確認する
    (FC-3: poison 後に entry-point プラグインの socket import で exit=1
    になっていた回帰の実測ピン)。"""
    from agentic_fx.plugin import approval

    test_plugin_path = tmp_path / "test_plugin.py"
    test_plugin_path.write_text("def test_x():\n    assert True\n")

    result = approval._default_pytest_runner(test_plugin_path)

    assert result["returncode"] == 0, result["stdout"] + result["stderr"]
    assert "1 passed" in result["stdout"]
```

```bash
uv run pytest tests/plugin/test_approval.py -q -k real_subprocess_exits_zero
```

Expected (修正前の `env=_build_env()` のみの状態で先に実行した場合): FAIL (`returncode == 1`)。Step 9 の `env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"` を実装済みであれば PASS になる — 実装順序上は Step 9 の直後に置いているため、ここでは PASS を確認する意味で実行する (実装なしで先に本テストのみ追加した場合の red 確認は変異テスト Step で兼ねる)。

- [ ] **Step 12: 失敗するテストを書く (PluginSession スレッド安全性 assert)**

`tests/plugin/test_sandbox_thread_safety.py` を新規作成する:

```python
"""PluginSession の単一スレッド所有 assert (プラン 8 B 束)。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.config import PluginSettings
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import PluginSession, SandboxError


def _fake_meta(tmp_path) -> PluginMeta:
    (tmp_path / "plugin.py").write_text(
        "def compute(df, params):\n    return {}\n")
    from agentic_fx.plugin.loader import content_hash
    return PluginMeta(name="x", kind="indicator", path=tmp_path, params={},
                      timeframe=None, pairs=(), max_bars=200,
                      content_hash=content_hash(tmp_path))


def test_call_from_other_thread_raises_runtime_error(tmp_path):
    """`__enter__` を呼んだスレッド以外からの `call()` は RuntimeError
    (SandboxError ではない — プログラミングエラーと plugin 実行時エラーの
    区別)。実 subprocess は起動せず、owner thread チェックが __enter__
    完了前の早い段階 (spawn 前) で発火することを確認する — session が
    未起動 (`self._proc is None`) の状態でも境界チェックが機能すること
    のピン。
    """
    session = PluginSession(_fake_meta(tmp_path), settings=PluginSettings())
    session._owner_thread = threading.get_ident() + 999999  # 別スレッドを偽装

    with pytest.raises(RuntimeError, match="owner thread"):
        session.call({"df": None, "params": {}})
```

- [ ] **Step 13: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_sandbox_thread_safety.py -q
```

Expected: FAIL (`AttributeError: 'PluginSession' object has no attribute '_owner_thread'`)。

- [ ] **Step 14: `sandbox.py` を実装**

`src/agentic_fx/plugin/sandbox.py` の先頭 import 節に `import threading` を追加する。`PluginSession.__init__` (293-307 行) の末尾に以下を追加する:

```python
        # プラン 8 B 束: PluginSession は単一スレッド所有が前提
        # (全使用箇所が単一スレッド — ロックは追加しない)。construction
        # したスレッドを記録し、実行時 assert で境界越えを検出する。
        self._owner_thread = threading.get_ident()

    def _check_owner_thread(self) -> None:
        current = threading.get_ident()
        if current != self._owner_thread:
            raise RuntimeError(
                f"PluginSession used from a different thread than its "
                f"owner thread (owner={self._owner_thread}, "
                f"current={current}) — PluginSession is single-thread-owned")
```

`__enter__` (309 行) の docstring 直後、`try:` の直前に `self._check_owner_thread()` を追加する。`call()` (406 行) の docstring 直後、`if self._dead or self._proc is None:` の**前**に `self._check_owner_thread()` を追加する。`close()` (383 行) の先頭に `self._check_owner_thread()` を追加する (`proc = self._proc` の前)。

- [ ] **Step 15: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_sandbox_thread_safety.py -q
uv run pytest tests/plugin/ -q
```

Expected: 全件 PASS。

- [ ] **Step 16: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 17: 変異テスト**

以下の 4 箇所を個別に改変し、対応するテストが red になることを確認してから元に戻す:
1. `worker.py` の `resource.setrlimit(resource.RLIMIT_NOFILE, ...)` 行を削除 → `test_set_resource_limits_sets_nofile_and_fsize` が red
2. `approval.py:_default_pytest_runner` の `env=_build_env()` を削除 (env 未指定に戻す) → `test_default_pytest_runner_uses_minimal_env` が red
3. `sandbox.py:PluginSession.call` 内の `self._check_owner_thread()` 呼び出しを削除 → `test_call_from_other_thread_raises_runtime_error` が red
4. (FC-3) `approval.py:_default_pytest_runner` の `env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"` 行を削除 → `test_default_pytest_runner_uses_minimal_env` (env アサーション部分) と `test_default_pytest_runner_real_subprocess_exits_zero_after_poison` の両方が red (後者は環境に `anyio` 等の entry-point プラグインが入っている場合に限り red — 入っていない環境では poison 前から exit=0 のため red にならない点に注意。CI 環境の依存構成を確認し、red にならない場合は `uv run python -c "import importlib.metadata as m; [print(e) for e in m.entry_points(group='pytest11')]"` で entry-point プラグインの有無を確認した上でレビュアーに申し送る)

- [ ] **Step 18: Commit**

```bash
git add src/agentic_fx/plugin/worker.py src/agentic_fx/plugin/sandbox.py \
  src/agentic_fx/plugin/approval.py src/agentic_fx/plugin/pytest_sandbox_entry.py \
  src/agentic_fx/config.py config/settings.yaml.example \
  tests/plugin/test_worker.py tests/plugin/test_approval.py \
  tests/plugin/test_sandbox_thread_safety.py tests/plugin/test_sandbox.py
git commit -m "$(cat <<'EOF'
feat: plugin サンドボックス増強 (pytest 完全隔離 + RLIMIT_NOFILE/FSIZE + PluginSession スレッド安全性)

プラン7起票の B 束 3 項目 (設計書 §7)。test_plugin.py 実行を最小 env +
resource limit + ネットワーク毒入れ付きサブプロセスに、worker.py の
rlimit に NOFILE/FSIZE を追加し、PluginSession の単一スレッド所有を
実行時 assert で明文化する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 3: B 束 — 小口修正 6 項目 (maintenance 順序 / producer_source 検証 / strategy_adapter 対称化 / sqlite3.Error CLI 境界 / SQLite ≥3.35 assert / description f-string 化)

設計書 §7 表の残り 6 項目。いずれも影響範囲が 1〜数行の独立した小口修正であり、依存関係が無いため 1 task にまとめる (Task 2 で使い切った「サンドボックス増強」という単一テーマと違い、こちらはテーマ横断の雑多な小口 — レビュー粒度は項目ごとの diff で確認する)。

**Files:**
- Modify: `src/agentic_fx/service.py:369-382` (`on_signal_maintenance` の reclaim→expire 順序入替)
- Modify: `src/agentic_fx/service.py:208-223` (`_validate_startup` に producer_source 検証追加)
- Modify: `src/agentic_fx/store/ohlcv.py` (`KNOWN_OHLCV_SOURCES` 定数新設)
- Modify: `src/agentic_fx/plugin/strategy_adapter.py:74-87` (`PluginStrategyIntentSource.__init__` に pair 実在検証追加)
- Modify: `src/agentic_fx/backtest/cli.py:1-20`(import)`,461`(except 節)
- Modify: `src/agentic_fx/store/db.py:152-159` (`connect` に SQLite バージョン assert)
- Modify: `src/agentic_fx/tools/signal_tools.py` (description f-string 化)
- Test: `tests/core/test_scheduler_signal.py` または `tests/test_service_app.py` (maintenance 順序), `tests/test_service_app.py` (producer_source 検証), `tests/plugin/test_strategy_adapter.py` (pair 対称化), `tests/backtest/test_cli.py` (sqlite3.Error 境界), `tests/store/test_db.py` (SQLite バージョン assert), `tests/tools/test_signal_tools.py` (description)

**Interfaces:**
- Produces:
  - `store.ohlcv.KNOWN_OHLCV_SOURCES: frozenset[str]` = `{"yfinance", "mt5", "mt5-live", "twelvedata", "dukascopy"}` — ohlcv テーブルの `source` 列に実際に書き込まれる値の正規列挙 (`price_provider.py:_STORAGE_SOURCE` / `backtest/importer.py`/`mt5_import.py`/`analysis.py:ANALYSIS_SOURCE`/`plugin/approval.py:_EVAL_SOURCE` の実値を集約)
  - `service._validate_startup(settings)` の追加検証: `settings.plugin.producer_source not in KNOWN_OHLCV_SOURCES` なら `RuntimeError`
  - `strategy_adapter.PluginStrategyIntentSource.__init__` は `pair not in meta.pairs` で `ValueError` を送出する (fail closed — producer 側の「settings.pairs 外は warning + skip」と対称の検証だが、adapter は 1 インスタンス = 1 pair の明示的構築のため即座に `ValueError` で拒否する。producer のように複数 pair を反復して一部だけ諦める構造ではないため skip という選択肢が無い)
  - `db.connect()` は接続直後に `sqlite3.sqlite_version_info < (3, 35, 0)` なら `RuntimeError` (signals.py の `claim_oldest`/`requeue`/`reclaim_expired` が `RETURNING` 句 — SQLite 3.35.0 (2021-03-12) 以降が必須)
  - **(裁定書 F-16/IM-10 新設)** `service._run_signal_maintenance(*, conn, signal_producer, approved, settings, now: datetime) -> None` — `on_signal_maintenance` 閉包の実体を module レベル関数として抽出したもの。`build_app()` 全体を構築せずに単体テスト可能にする (実クロージャの順序を機械的に検証できない旧稿の欠陥の修正)

- [ ] **Step 1: 失敗するテストを書く (maintenance 順序)**

**(レビュー反映 1 回目 — 裁定書 F-16 / IM-10)**: 執筆者の当初案は `on_signal_maintenance` を**テスト内でローカルに再定義したフェイク閉包**に対して assert しており、`service.py` の実クロージャを一切呼ばない恒真テストだった (Step 2 が「このテストは `service.py` を変更せずとも green になる — 意図的」と明言していたこと自体が欠陥の自認)。**実クロージャを直接検証できるよう、`on_signal_maintenance` の本体を module レベル関数 `_run_signal_maintenance` に抽出**し (Step 3)、テストはその実関数を呼んで `agentic_fx.store.signals` の実モジュール関数を monkeypatch した状態で呼び出し順を記録する。

`tests/test_service_app.py` (無ければ実ファイルを `ls tests/*.py` で確認し、`build_app` の統合テストが既にある場所に追記する) に以下を追加する:

```python
def test_signal_maintenance_reclaims_before_expiring(monkeypatch):
    """reclaim_expired → expire_stale の順で呼ばれる (順序入替、codex M⑤)。
    reclaim で pending に戻った行が鮮度切れなら、同じ tick 内の
    expire_stale で abandoned という終端状態に落ちることを、
    呼び出し順の記録で確認する (**レビュー反映 2 回目 / Task 3 codex I-1**:
    旧稿は「1 tick 分だけ実行機会を得る」と書いていたが因果が逆。
    実際にはこの順序こそが同一 tick 内での終端化を起こす)。**(裁定書 F-16/IM-10)** `service.py` の
    実クロージャが呼ぶ module レベル関数 `_run_signal_maintenance` を
    直接呼び、`agentic_fx.store.signals` の実モジュール関数を
    monkeypatch する — テスト内の再定義フェイクに対して assert する
    恒真テストを避ける。
    """
    from datetime import datetime, timezone

    import agentic_fx.service as service_mod

    calls: list[str] = []

    def fake_reclaim(*a, **k):
        calls.append("reclaim")
        return []

    def fake_expire(*a, **k):
        calls.append("expire")
        return 0

    monkeypatch.setattr(service_mod.signals, "reclaim_expired", fake_reclaim)
    monkeypatch.setattr(service_mod.signals, "expire_stale", fake_expire)

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    fake_producer = object()  # evaluate_due_plugins は呼ばれない前提で
    # 属性アクセスされたら AttributeError で明示的に落ちるようにする
    # (順序検証の対象外だが、意図せず呼ばれた場合は検出したい)。

    class _NoOpProducer:
        def evaluate_due_plugins(self, **k):
            calls.append("producer")

    service_mod._run_signal_maintenance(
        conn=None, signal_producer=_NoOpProducer(), approved=[],
        settings=service_mod.load_settings(
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "config" / "settings.yaml.example"),
        now=now)

    assert calls == ["reclaim", "expire", "producer"]
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_service_app.py -q -k maintenance
```

Expected: FAIL (`AttributeError: module 'agentic_fx.service' has no attribute '_run_signal_maintenance'` — Step 3 で新設するまで存在しない)。

- [ ] **Step 3: `service.py` の `on_signal_maintenance` 順序を入替 + 実クロージャ検証可能化**

**(裁定書 F-16/IM-10)** `on_signal_maintenance` の本体を module レベル関数 `_run_signal_maintenance` として切り出し、`build_app` 内の閉包はこれを呼ぶだけにする — これにより Step 1 のテストが `build_app()` 全体を構築せずに実ロジックへ直接到達できる。`src/agentic_fx/service.py:369-382` を以下に置き換える (`reclaim_expired` を `expire_stale` より前に呼ぶ — codex M⑤。**レビュー反映 2 回目 / Task 3 codex I-1 で因果を訂正**: この順序の利得は「無駄な mission の実行の回避」ではなく、reclaim で pending に戻った鮮度切れ行が同一 tick 内で abandoned という終端状態に落ちること、および `expire_stale` の戻り値 (呼び出し側が通知件数に使う) の正確さである。鮮度ゲート有効時は `claim_oldest` の WHERE が `_FRESH_CONDITION` を含み、`_STALE_CONDITION` はその厳密な補集合なので、stale な pending が mission に claim されることは構造的にあり得ない):

```python
def _run_signal_maintenance(*, conn, signal_producer, approved, settings,
                            now: datetime) -> None:
    """`on_signal_maintenance` の実体 (裁定書 F-16/IM-10 — module レベル
    関数として抽出し、`build_app()` 全体を構築せずに単体テスト可能に
    する)。

    Task 7 申し送り → プラン 8 B 束で順序入替 (codex M⑤): lease 切れの
    claimed 行を先に reclaim_expired で pending へ戻し、その後に
    expire_stale で鮮度切れの pending を abandoned 化する。この順序に
    より、reclaim で pending に戻った行が鮮度切れなら同じ tick 内で
    abandoned という終端状態に落ちる。旧順序 (expire → reclaim) では、
    その行は expire の時点でまだ claimed のため対象外となり、鮮度切れで
    claim され得ない pending のまま次の maintenance まで居残った。
    なお鮮度ゲート有効時 (freshness_bars is not None) は claim_oldest の
    WHERE が _FRESH_CONDITION を含み、_STALE_CONDITION はその厳密な
    補集合であるため、stale な pending が mission に拾われることはない
    — 本順序の利得は「無駄な mission の実行の回避」ではなく、終端状態
    への即時収束と expire_stale の戻り値 (呼び出し側が通知件数に使う)
    の正確さである。呼び出し元 (Scheduler._run_data_hook) が fail-open
    で包む。
    """
    signals.reclaim_expired(conn, now=now,
                            lease_min=settings.plugin.signal_lease_min,
                            max_requeue=settings.plugin.signal_requeue_max)
    signals.expire_stale(conn, now=now,
                         freshness_bars=settings.plugin.signal_freshness_bars)
    signal_producer.evaluate_due_plugins(
        conn=conn, plugins=approved, now=now,
        source=settings.plugin.producer_source, settings=settings)
```

`build_app` 内の `on_signal_maintenance` 閉包定義を以下に置き換える:

```python
    def on_signal_maintenance(now: datetime) -> None:
        _run_signal_maintenance(conn=conn_core, signal_producer=signal_producer,
                                approved=approved, settings=settings, now=now)
```

- [ ] **Step 4: 失敗するテストを書く (producer_source 検証・SQLite バージョン assert・description f-string)**

`tests/test_service_app.py` に追加:

```python
def test_validate_startup_rejects_unknown_producer_source():
    from agentic_fx.service import _validate_startup
    from agentic_fx.config import load_settings
    from pathlib import Path

    settings = load_settings(
        Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
    settings = settings.model_copy(
        update={"plugin": settings.plugin.model_copy(
            update={"producer_source": "typo-source"})})
    with pytest.raises(RuntimeError, match="producer_source"):
        _validate_startup(settings)
```

`tests/store/test_db.py` に追加:

```python
def test_connect_rejects_old_sqlite_version(tmp_path, monkeypatch):
    import sqlite3

    import agentic_fx.store.db as db_mod
    monkeypatch.setattr(db_mod.sqlite3, "sqlite_version_info", (3, 34, 1))
    with pytest.raises(RuntimeError, match="3.35"):
        db_mod.connect(tmp_path / "x.db")
```

`tests/tools/test_signal_tools.py` に追加:

```python
def test_get_signals_description_reflects_default_lookback():
    from agentic_fx.tools import signal_tools

    tools = signal_tools.build(_conn_fixture(), _settings_fixture(), _clock_fixture())
    tool = next(t for t in tools if t.name == "get_signals")
    assert f"{signal_tools._DEFAULT_SINCE_HOURS}h" in tool.description
```

(既存の `test_signal_tools.py` の fixture 関数名 — `_conn_fixture`/`_settings_fixture`/`_clock_fixture` に相当するもの — を実ファイルを開いて確認し、既存の命名に合わせること。プレースホルダのまま使わない。)

- [ ] **Step 5: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_service_app.py tests/store/test_db.py tests/tools/test_signal_tools.py -q -k "producer_source or sqlite_version or default_lookback"
```

Expected: 3 件とも FAIL (`_validate_startup` は producer_source を見ていない / `connect` はバージョンを見ていない / `_DEFAULT_SINCE_HOURS` が存在しない)。

- [ ] **Step 6: 実装**

`src/agentic_fx/store/ohlcv.py` の先頭付近 (20 行目のコメントの直後) に追加:

```python
# プラン 8 B 束: ohlcv.source 列に実際に書き込まれる値の正規列挙
# (price_provider.py:_STORAGE_SOURCE / backtest/importer.py /
# backtest/mt5_import.py / backtest/analysis.py:ANALYSIS_SOURCE /
# plugin/approval.py:_EVAL_SOURCE の実値を集約)。起動時の
# producer_source typo 検出 (service.py:_validate_startup) が参照する。
KNOWN_OHLCV_SOURCES = frozenset(
    {"yfinance", "mt5", "mt5-live", "twelvedata", "dukascopy"})
```

`src/agentic_fx/service.py` の import 節に `from agentic_fx.store import ohlcv` を追加し (`store` パッケージから既に `approvals, missions, orders, signals` を import している行 45 に `ohlcv` を追加)、`_validate_startup` (208-223 行) 末尾に追加:

```python
    if settings.plugin.producer_source not in ohlcv.KNOWN_OHLCV_SOURCES:
        raise RuntimeError(
            f"settings.plugin.producer_source={settings.plugin.producer_source!r} "
            f"is not a known source (known: {sorted(ohlcv.KNOWN_OHLCV_SOURCES)})")
```

`src/agentic_fx/store/db.py:152-159` の `connect` を以下に置き換える:

```python
def connect(db_path: Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    # プラン 8 B 束: signals.py の claim_oldest/requeue/reclaim_expired は
    # RETURNING 句に依存する (SQLite 3.35.0 = 2021-03-12 以降)。古い
    # SQLite では RETURNING が構文エラーになり、失敗の意味が分かりにくい
    # (「claim できない」ではなく「SQL 構文エラー」として現れる) ため、
    # 接続確立時点で明示的に fail fast する。
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old (>= 3.35 required "
            "for signals.py RETURNING clauses)")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
```

`src/agentic_fx/tools/signal_tools.py` の `_ANNOTATION` 定数の近くに追加:

```python
# プラン 8 B 束: ToolDef description の「既定 Nh」が since_hours の実際の
# デフォルト値と独立に手打ちされ、乖離し得た (Task 9 deferred①)。
# 1 箇所の定数に統一し description は f-string で生成する。
_DEFAULT_SINCE_HOURS = 24
```

`get_signals` の内部関数シグネチャ `def get_signals(pair: str, since_hours: int = 24) -> list[dict]:` を `def get_signals(pair: str, since_hours: int = _DEFAULT_SINCE_HOURS) -> list[dict]:` に変更する。`ToolDef` の description 文字列 (128-130 行) を以下に置き換える:

```python
        ToolDef(
            "get_signals",
            "取引判断 loop 専用: 承認済み signal/strategy plugin の直近 "
            f"出力 (pair, 直近 since_hours 時間分・既定 {_DEFAULT_SINCE_HOURS}h)。"
            "strategy 行には in_sample バックテスト成績 (in_sample_metrics) "
            "と、実運用成績の予測値ではない旨の注記 (note) が付く",
```

- [ ] **Step 7: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_service_app.py tests/store/test_db.py tests/tools/test_signal_tools.py -q
uv run pytest -q
```

Expected: 全件 PASS。既存の `settings.yaml.example` の `producer_source: yfinance` は `KNOWN_OHLCV_SOURCES` に含まれるため既存起動テストは無変更で通ること、既存全 DB テストは実 SQLite が 3.35 以上 (開発環境の Python 標準 sqlite3 は概ね対応) であるため通ることを確認する。

- [ ] **Step 8: 失敗するテストを書く (strategy_adapter 対称化 + sqlite3.Error CLI 境界)**

`tests/plugin/test_strategy_adapter.py` に追加 (既存 fixture — `_fake_meta`/`_conn` 等 — を実ファイルで確認し流用する):

```python
def test_build_intent_source_rejects_pair_not_in_meta_pairs(tmp_path):
    """producer 側 (settings.pairs 外は warning+skip) と対称の検証:
    adapter は 1 インスタンス = 1 pair の明示的構築のため、meta.pairs に
    無い pair を渡されたら即座に ValueError (fail closed, Fable M-1)。"""
    meta = _fake_strategy_meta(tmp_path, pairs=("USDJPY",))  # 既存 fixture 名を確認して使う
    with pytest.raises(ValueError, match="pairs"):
        build_intent_source(meta, conn=_conn(tmp_path), pair="EURUSD",
                            source="dukascopy", settings=_settings())
```

`tests/backtest/test_cli.py` に追加:

```python
def test_dispatch_sqlite_error_returns_rc1_with_diagnostic(tmp_path, capsys, monkeypatch):
    """DB 層の sqlite3.Error が生の traceback ではなく診断メッセージ +
    rc=1 に正規化される (Task 6 deferred④)。"""
    import sqlite3

    from agentic_fx.backtest import cli

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "_history_coverage", boom)
    root = _init_root(tmp_path)  # 既存 fixture 名を確認して使う
    args = argparse.Namespace(command="history", history_command="coverage")
    rc = cli.dispatch(args, root)
    assert rc == 1
    assert "database is locked" in capsys.readouterr().err
```

- [ ] **Step 9: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_strategy_adapter.py tests/backtest/test_cli.py -q -k "not_in_meta_pairs or sqlite_error"
```

Expected: 両方 FAIL。

- [ ] **Step 10: 実装**

`src/agentic_fx/plugin/strategy_adapter.py` の `PluginStrategyIntentSource.__init__` (74-87 行) 冒頭、`self._meta = meta` の前に追加:

```python
        # プラン 8 B 束 (Fable M-1): producer 側 (settings.pairs 外は
        # warning + skip) と対称の検証。adapter は 1 インスタンス = 1 pair
        # の明示的構築であり、meta.pairs に無い pair は「呼び出し側の
        # 取り違え」であって producer のように複数 pair を反復して一部
        # だけ諦める構造ではないため、即座に拒否する (fail closed)。
        if pair not in meta.pairs:
            raise ValueError(
                f"pair {pair!r} is not in plugin {meta.name!r}'s declared "
                f"pairs {meta.pairs!r}")
```

`src/agentic_fx/backtest/cli.py` の import 節 (14-20 行付近) に `import sqlite3` を追加する。`dispatch` (437-461 行) の `except (ValueError, KeyError, OSError) as e:` を以下に変更する:

```python
    except (ValueError, KeyError, OSError, sqlite3.Error) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
```

- [ ] **Step 11: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_strategy_adapter.py tests/backtest/test_cli.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 12: 変異テスト**

以下を個別に改変し red を確認してから戻す:
1. **(裁定書 F-16/IM-10 反映)** `_run_signal_maintenance` 内の `signals.reclaim_expired(...)` と `signals.expire_stale(...)` の呼び出し順を入れ替える (expire→reclaim に戻す) → `test_signal_maintenance_reclaims_before_expiring` が red (`calls == ["expire", "reclaim", "producer"]` になり `assert calls == ["reclaim", "expire", "producer"]` に失敗する) — 旧稿はテスト内のローカルフェイク閉包に対して assert しており実クロージャの変異を検出できなかったが (「目視で確認」が最終防波堤だった)、`_run_signal_maintenance` への抽出後は module 関数の実引数順序を直接監視するため機械的に red になる
2. `service.py:_validate_startup` の producer_source チェックを削除 → `test_validate_startup_rejects_unknown_producer_source` が red
3. `db.py:connect` のバージョン assert を削除 → `test_connect_rejects_old_sqlite_version` が red
4. `strategy_adapter.py` の pair 検証を削除 → `test_build_intent_source_rejects_pair_not_in_meta_pairs` が red
5. `cli.py` の except タプルから `sqlite3.Error` を削除 → `test_dispatch_sqlite_error_returns_rc1_with_diagnostic` が red

- [ ] **Step 13: Commit**

```bash
git add src/agentic_fx/service.py src/agentic_fx/store/ohlcv.py src/agentic_fx/store/db.py \
  src/agentic_fx/plugin/strategy_adapter.py src/agentic_fx/backtest/cli.py \
  src/agentic_fx/tools/signal_tools.py \
  tests/test_service_app.py tests/store/test_db.py tests/tools/test_signal_tools.py \
  tests/plugin/test_strategy_adapter.py tests/backtest/test_cli.py
git commit -m "$(cat <<'EOF'
fix: B 束小口 6 項目 (maintenance順序/producer_source検証/adapter対称化/CLI境界/SQLite版数/description)

プラン7起票の B 束 (設計書 §7) のうち独立した小口修正をまとめて返済する。

レビュー反映1回目 (裁定書 F-16/IM-10): maintenance順序テストがテスト内
ローカルフェイクに対する恒真テストだった欠陥を修正。on_signal_maintenance
の本体を_run_signal_maintenanceとしてmodule関数へ抽出し、実クロージャを
monkeypatch経由で直接検証できるようにした。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 4: プラン 5 park 小口返済 (retry policy 明文化 / clock 配線 / provider ctor seam / policy OSError / watchdog 時刻源 / conftest 移動)

**park 一覧の出典**: `.superpowers/sdd/2026-07-26-phase1-5-loop-service/progress.md` 末尾「統合裁定 — fix wave」節。`codex I1`(retry policy) / `codex M2`(clock 配線) / `sonnet I-3`(provider ctor seam、Task 8 レビュー) / `fable M3`(policy OSError) / `fable M4`(watchdog 時刻源) / `fable M7`(conftest 移動) を返済する。`fable M5`(`_check_llama_swap` 文言) / `fable M6`(ask 失敗文言) は本 task の Step 1 で現状のメッセージ文面を確認した結果、既に具体的かつ明確 (`service.py:84-106` の 3 分岐別警告文、`trade_loop.py:76` の `"(Mission 失敗: internal_error)"`) であり追加修正の必要なしと判定する (triage — commit メッセージに明記)。**shell readline 中断は Task 17 で扱う** (§6 の停止実行主体の必須依存に昇格したため、監督/停止状態機械 task の直前に独立 task 化する)。

**Files:**
- Modify: `src/agentic_fx/core/scheduler.py:210-256` (`_trade_mission_due`/`tick` の docstring に retry policy 明文化)
- Modify: `src/agentic_fx/service.py:183-206`(`App` に `clock` フィールド追加)`,244-260`(`build_app` に `clock`/`provider` seam)`,448-478`(`_watchdog_tick` の時刻源)`,502-514`(`scheduler_thread` の clock 配線)
- Modify: `src/agentic_fx/loops/mission_watch.py:24-29` (`time_fn` プロパティ追加)
- Modify: `src/agentic_fx/policy.py:11-27` (`OSError` 捕捉に拡張)
- Rename: `tests/backtest/conftest.py` → `tests/backtest/factories.py` (13 箇所の import 更新)
- Test: `tests/core/test_scheduler.py` (retry policy pin), `tests/test_service_app.py` (clock 配線・provider seam・watchdog 時刻源), `tests/test_policy.py` (OSError)

**Interfaces:**
- Produces:
  - `App.clock: Clock` (新設フィールド。`build_app` が保持している `clock` 変数をそのまま格納する — 既存の `clock = clock or SystemClock()` 行の直後で使う)
  - `build_app(root, *, runner=None, clock=None, quote_fn=None, spec_fn=None, bars_fn=None, embedding_fn=None, provider=None)` — `provider: PriceProvider | None = None` (新設 kwarg)。**非 None の場合は内部での `PriceProvider(conn_core, settings, clock)` 構築と、それに続く `quote_fn`/`spec_fn`/`bars_fn` の bound-method 差し替え (285-296 行) を丸ごとスキップし、渡された `provider` インスタンスをそのまま使う** (呼び出し側が provider の全挙動を制御したい場合の直接注入 seam — 既存の `quote_fn`/`spec_fn`/`bars_fn` 個別注入とは併用不可・排他: **両方渡された場合は `ValueError` を送出する** — **レビュー反映 2 回目 / Task 4 codex Critical 1 + Important 1 で裁定変更**。旧稿は「`provider` を優先し `quote_fn` 等は無視する」と書いていたが、①Interfaces 節・「注意」節・逐語コードの 3 者が互いに矛盾しており実装者が独自解釈する余地を残した ②「黙って無視」は静かな失敗で、E2E テストが「注入が効かない」ことに気づけず debug を困難にする。fail closed で拒否し、契約を docstring とテストで固定する)
  - `MissionWatch.time_fn` — `self._time` を返す読み取り専用 property (新設)
  - `_watchdog_tick(app: App) -> None` の内部実装のみ変更 (シグネチャ不変) — `elapsed` の算出を `app.mission_watch.time_fn()` 経由にする
  - `Policy.tail`/`Policy.size_warning` は `FileNotFoundError` ではなく `OSError` を捕捉する (`FileNotFoundError` は `OSError` のサブクラスなので上位互換)

- [ ] **Step 1: 現状確認 (M5/M6 triage・変更なし)**

`src/agentic_fx/service.py:84-106` (`_check_llama_swap` の 3 分岐警告文) と `src/agentic_fx/loops/trade_loop.py:76` (`ask_once` の `"(Mission 失敗: internal_error)"`) を読み、いずれも状況別に具体的な文言であることを確認する。コード変更は行わない — Step 12 のコミットメッセージに triage 結果を記録する。

- [ ] **Step 2: 失敗するテストを書く (retry policy pin)**

`tests/core/test_scheduler.py` に以下を追加する (既存 `Env` fixture — ファイル冒頭で確認済み — をそのまま使う):

```python
def test_cron_deadline_advances_even_when_mission_callback_raises():
    """codex I1 (プラン5 park): on_trade_mission が例外を送出しても
    _last_cron_trade は前進する — 1 回/時の再試行間隔を意図的な設計として
    固定する (毎 tick 再試行すると障害時に LLM/notifier を連打するため
    安全側)。tick() 自体は on_trade_mission の例外を保護しない
    (呼び出し元 = service.py の scheduler_thread が広い try で包む) ため、
    この pin は tick 側の呼び出し順序 (締切前進 → on_trade_mission 呼び出し)
    が「前進してから呼ぶ」順であることを固定する。
    """
    env = Env()  # 既存 fixture 名を確認して使う (Env が無ければ既存の
                  # scheduler 構築ヘルパー名に合わせる)
    calls: list[str] = []

    def boom(reason):
        calls.append(reason)
        raise RuntimeError("mission callback failed")

    env.scheduler.on_trade_mission = boom
    first_call_time = WED
    with pytest.raises(RuntimeError):
        env.scheduler.tick(first_call_time)
    assert calls == ["cron"]
    # 締切は例外前に前進済み — 30 分後の tick では再起動しない
    assert env.scheduler._trade_mission_due(
        first_call_time + timedelta(minutes=30)) is None
    # 1 時間後には再試行される
    assert env.scheduler._trade_mission_due(
        first_call_time + timedelta(hours=1)) == "cron"
```

(既存の `Env` fixture のコンストラクタ引数・`WED`/`timedelta` の import が無ければファイル冒頭の既存 import に揃える。プレースホルダのまま使わず、実ファイルを読んでから書くこと。)

- [ ] **Step 3: テスト実行**

```bash
uv run pytest tests/core/test_scheduler.py -q -k retry_policy_or_cron_deadline
```

Expected: このテストは **現状の実装のまま PASS するはず** (`tick()` は `on_trade_mission` 呼び出し前に `_last_cron_trade = now` を代入済み — scheduler.py:215-217)。PASS しない場合は `tick()` の呼び出し順序が想定と異なる (退行) ため、実装側ではなくテストの前提を先に見直すこと。

- [ ] **Step 4: `scheduler.py` の docstring に retry policy を明文化**

`src/agentic_fx/core/scheduler.py:210-218` (`tick` 内、`reason = self._trade_mission_due(now)` の直前) に以下のコメントを追加する:

```python
        # プラン 8 park 返済 (codex I1, プラン 5 レジャー): on_trade_mission
        # が例外を送出しても _last_cron_trade は既に前進済み (下の
        # if reason == "cron": 行が先に走る) — これは意図的な設計であり
        # バグではない。毎 tick 再試行 (前進させない設計) は、Mission 起動
        # 自体が壊れている状況で LLM/notifier を毎分連打することになり、
        # 障害時により危険側に倒れる。1 時間ごとの再試行間隔を保つことで
        # 障害時の負荷を抑える (test_cron_deadline_advances_even_when_
        # mission_callback_raises がこの契約を固定する)。
        reason = self._trade_mission_due(now)
```

- [ ] **Step 5: 失敗するテストを書く (clock 配線・provider seam)**

`tests/test_service_app.py` に追加:

```python
def test_app_has_clock_field(tmp_path):
    from agentic_fx.core.contracts import FixedClock
    from datetime import datetime, timezone

    fixed = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    root = _init_root(tmp_path)  # 既存 fixture 名を確認して使う
    app = build_app(root, clock=fixed)
    assert app.clock is fixed


def test_build_app_provider_seam_bypasses_quote_fn_patch(tmp_path):
    """provider を直接注入した場合、quote_fn/spec_fn/bars_fn の
    bound-method 差し替えは行われない (provider が全挙動を持つ)。"""
    from agentic_fx.datafeed.price_provider import PriceProvider
    from agentic_fx.config import load_settings
    from agentic_fx.core.contracts import SystemClock

    root = _init_root(tmp_path)
    settings = load_settings(root / "config" / "settings.yaml")
    fake_provider = _FakeProvider()  # テスト用の最小 PriceProvider 互換 fake
    app = build_app(root, provider=fake_provider,
                    quote_fn=lambda pair: (_ for _ in ()).throw(
                        AssertionError("quote_fn should not be used")))
    assert app.provider is fake_provider
```

(`_FakeProvider`/`_init_root` は既存の `tests/test_service_app.py` (または `tests/test_e2e_phase1.py`) の fixture 命名規約に合わせて実装すること。無ければ最小限の `PriceProvider` サブクラス/duck-type を新規に書く。)

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_service_app.py -q -k "app_has_clock_field or provider_seam"
```

Expected: FAIL (`App` に `clock` 属性が無い / `build_app` に `provider` kwarg が無い)。

- [ ] **Step 7: `service.py` を実装 (clock フィールド + provider seam)**

`src/agentic_fx/service.py` の `App` dataclass (183-206 行) に `clock: object` フィールドを追加する (既存フィールドの型ヒントに揃えて `object` — 既存の `App` フィールドは型ヒントを厳密にしていないため踏襲する):

```python
@dataclass
class App:
    conn_core: object
    conn_shell: object
    settings: object
    state: object
    activity: object
    broker: object
    executor: object
    provider: object
    econ: object
    collector: object
    rag: object
    trade_loop: object
    reflection: object
    scheduler: object
    commands: object
    registry: object
    core_lock: threading.RLock
    mission_watch: MissionWatch
    notifier: object
    runner: object
    owns_runner: bool
    clock: object
```

（`instance_lock` フィールドは本 task では追加しない — 単一インスタンス保証 (FC-2) は Task 11 の担当であり、`App` dataclass への追加・`return App(...)` への配線は Task 11 側で完結させる。Task 4 の時点でここに追加すると `return App(...)` (直後) が未対応のまま `TypeError` になり、本 task 自身の Step 8 が失敗する。）

`build_app` のシグネチャ (244-246 行) に `provider: PriceProvider | None = None` を追加する:

```python
def build_app(root: Path, *, runner: AgentRunner | None = None,
              clock: Clock | None = None, quote_fn=None, spec_fn=None,
              bars_fn=None, embedding_fn=None,
              provider: PriceProvider | None = None) -> App:
```

docstring の `quote_fn / spec_fn / bars_fn / embedding_fn は E2E テストの注入点` の段落の後に追加:

```python
    `provider` (プラン 8 park 返済 — codex I-3): 非 None の場合、
    `PriceProvider` の内部構築と `quote_fn`/`spec_fn`/`bars_fn` の
    bound-method 差し替えを丸ごとスキップし、渡されたインスタンスを
    そのまま使う。`quote_fn`/`spec_fn`/`bars_fn` と併用した場合は
    `provider` が優先され、後者は無視される (provider が全挙動を握るため)。
```

`build_app` 本体 (268-297 行) の provider 構築部分を以下に置き換える:

```python
    if provider is not None:
        # プラン 8 park 返済: 呼び出し側が provider の全挙動を握る seam。
        # 個別注入との併用は fail closed で拒否する (レビュー反映 2 回目 —
        # 「黙って無視」は注入が効かないことに気づけない静かな失敗)。
        if quote_fn is not None or spec_fn is not None or bars_fn is not None:
            raise ValueError(
                "provider と quote_fn/spec_fn/bars_fn は併用できません "
                "(provider が全挙動を握る seam です)")
        quote_fn = provider.get_quote
        spec_fn = provider.spec
        bars_fn = provider.latest_1m_bar
    else:
        provider = PriceProvider(conn_core, settings, clock)
        # 【実装者への指示: 以下のコメント群は現行 service.py の該当箇所から
        #  **逐語で引き写し**、字下げを 1 段追加するだけにすること。この指示行
        #  自体は製品コードに転写しない (Task 4 レビューで、指示文がそのまま
        #  コードコメントとして残り、元の警告本文が失われる事故が起きた)。
        #  引き写す対象には fix round 1 F1 (将来 self.spec 等の自己呼び出しが
        #  追加されたとき注入がすり抜けるバグを無音で再発させるという警告) と
        #  F2 (self.get_bars は別メソッド名なので捕捉できず恒久的に注入対象外)
        #  の 2 つの警告を必ず含める。】
        if quote_fn is not None:
            provider.get_quote = quote_fn
        else:
            quote_fn = provider.get_quote
        if spec_fn is not None:
            provider.spec = spec_fn
        else:
            spec_fn = provider.spec
        if bars_fn is not None:
            provider.latest_1m_bar = bars_fn
        else:
            bars_fn = provider.latest_1m_bar
```

**注意 (実装者向け)**: `quote_fn`/`spec_fn`/`bars_fn` は provider 分岐の**外側**でも後続コード (`Executor`/`Scheduler` の構築、282-296 行の `rate_fn` クロージャ) から参照される。`provider is not None` 分岐では `quote_fn`/`spec_fn`/`bars_fn` がまだローカル変数として束縛されていない (呼び出し元が渡さなかった場合) ため、この分岐でも `quote_fn = quote_fn if quote_fn is not None else provider.get_quote` の要領で「未指定なら注入 provider の束縛メソッドを使う」形に揃えること (`spec_fn`/`bars_fn` も同様)。実装時に既存コードを読み、両分岐後に `quote_fn`/`spec_fn`/`bars_fn` が必ず non-None であることを確認してから次の行 (`rate_fn` 定義以降) へ進むこと。

`build_app` の `return App(...)` (415-422 行) に `clock=clock` を追加する:

```python
    return App(conn_core=conn_core, conn_shell=conn_shell, settings=settings,
               state=state, activity=activity, broker=broker,
               executor=executor, provider=provider, econ=econ,
               collector=collector, rag=rag, trade_loop=trade_loop,
               reflection=reflection, scheduler=scheduler, commands=commands,
               registry=registry, core_lock=core_lock,
               mission_watch=mission_watch, notifier=notifier,
               runner=runner, owns_runner=owns_runner, clock=clock)
```

- [ ] **Step 8: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_service_app.py -q
```

Expected: PASS。

- [ ] **Step 9: 失敗するテストを書く (scheduler_thread の clock 配線・watchdog 時刻源)**

`tests/test_service_app.py` に追加:

```python
def test_scheduler_thread_uses_app_clock(tmp_path, monkeypatch):
    """service.py の scheduler_thread が `datetime.now(timezone.utc)` を
    直接呼ばず `app.clock.now()` を使う (codex M2)。FixedClock を注入し、
    tick に渡された now がその固定値であることを確認する。"""
    from agentic_fx.core.contracts import FixedClock
    from datetime import datetime, timezone
    import threading

    fixed = FixedClock(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    root = _init_root(tmp_path)
    app = build_app(root, clock=fixed)
    seen: list = []
    app.scheduler.tick = lambda now: seen.append(now)

    stop_event = threading.Event()
    stop_event.set()  # 1 回だけ走らせてすぐ止める意図 — 実装の
                       # scheduler_thread のループ条件に合わせて
                       # 呼び出し元テストの構成 (run_service の
                       # _stop_event シームを使う) を確認して書くこと。
    # 実装方針: run_service(root, _stop_event=...) 経由でなく、
    # scheduler_thread のロジックを直接 exercise できるよう、Step 11 の
    # 実装では datetime.now(timezone.utc) の呼び出し箇所を
    # app.clock.now() に置換するのみであるため、このテストは
    # run_service の起動テスト (既存 test_service.py の
    # run_service_smoke 系) に 1 assertion を追記する形で実装してもよい。
    # 実装者は既存の run_service テスト fixture を確認し、
    # 最小改変で「tick に渡された now が FixedClock の値である」ことを
    # 検証できる形に書き換えること。


def test_watchdog_tick_uses_mission_watch_time_fn(monkeypatch):
    """_watchdog_tick の elapsed 算出が MissionWatch の time_fn 経由で
    行われる (fable M4) — 生の time.monotonic() を直接呼ばない。"""
    from agentic_fx.service import _watchdog_tick, App
    from agentic_fx.loops.mission_watch import MissionWatch

    fake_time = [1000.0]
    watch = MissionWatch(time_fn=lambda: fake_time[0])
    watch.begin(mission_id=1, loop="trade", timeout_sec=10.0)
    fake_time[0] = 1000.0 + 10.0 + 61.0  # timeout + grace(60) を超過

    calls: list[str] = []

    class FakeActivity:
        def write(self, *a, **k):
            calls.append("write")

    class FakeNotifier:
        def send(self, *a, **k):
            calls.append("send")

    app = App(conn_core=None, conn_shell=None, settings=None, state=None,
              activity=FakeActivity(), broker=None, executor=None,
              provider=None, econ=None, collector=None, rag=None,
              trade_loop=None, reflection=None, scheduler=None,
              commands=None, registry=None, core_lock=None,
              mission_watch=watch, notifier=FakeNotifier(), runner=None,
              owns_runner=False, clock=None)
    _watchdog_tick(app)
    assert calls == ["write", "send"]
```

- [ ] **Step 10: テスト実行して FAIL を確認、実装、PASS を確認**

```bash
uv run pytest tests/test_service_app.py -q -k "clock or watchdog_tick_uses"
```

Expected: FAIL。`src/agentic_fx/loops/mission_watch.py` の `MissionWatch` クラス (24-29 行) に以下のプロパティを追加する:

```python
    @property
    def time_fn(self):
        """テスト/watchdog が同じ時刻源を参照できるようにする公開アクセサ
        (プラン 8 park 返済 — fable M4)。"""
        return self._time
```

`src/agentic_fx/service.py:458` の `elapsed = time.monotonic() - entry.started` を以下に変更する:

```python
    elapsed = app.mission_watch.time_fn() - entry.started
```

`src/agentic_fx/service.py:511` (`scheduler_thread` 内) の `app.scheduler.tick(datetime.now(timezone.utc))` を以下に変更する:

```python
                    with app.core_lock:
                        app.scheduler.tick(app.clock.now())
```

再実行:

```bash
uv run pytest tests/test_service_app.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 11: `policy.py` の OSError 捕捉**

`src/agentic_fx/policy.py` の `tail`/`size_warning` (11-27 行) の `except FileNotFoundError:` を両方とも `except OSError:` に変更する (`FileNotFoundError` は `OSError` のサブクラスなので既存の「ファイルが無い場合は空文字/None」の挙動は不変のまま、権限エラー・I/O エラー等も同じフォールバックに含める)。

`tests/test_policy.py` (無ければ新規作成) に以下を追加する:

```python
def test_tail_returns_empty_on_permission_error(tmp_path, monkeypatch):
    from agentic_fx.policy import Policy

    p = tmp_path / "directives.md"
    p.write_text("x" * 100)
    policy = Policy(p)

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(type(p), "read_text", boom)
    assert policy.tail(10) == ""
    assert policy.size_warning() is None
```

```bash
uv run pytest tests/test_policy.py -q
```

Expected: PASS after the `except OSError:` change (verify FAIL first with the original `except FileNotFoundError:` by running the test before editing, per TDD discipline).

- [ ] **Step 12: `tests/backtest/conftest.py` を `factories.py` へ rename**

`conftest.py` という予約ファイル名を「pytest fixture の自動収集対象」ではなく単なる import 用ヘルパーモジュールとして使っている (`@pytest.fixture` は 1 つも無い — `grep -n "@pytest.fixture" tests/backtest/conftest.py` で確認済み) — 混乱を避けるため rename する (fable M7)。

```bash
git mv tests/backtest/conftest.py tests/backtest/factories.py
grep -rl "tests\.backtest\.conftest" tests/ | xargs sed -i 's/tests\.backtest\.conftest/tests.backtest.factories/g'
grep -rn "tests\.backtest\.conftest" tests/ || echo "OK: no remaining references"
```

`tests/backtest/test_cli.py:543,560` 付近のコメント「conftest の H」を「factories の H」に手動で置換する (`sed` はコード上の import 文だけを対象にしたため、コメント文中の言及は個別に確認して直す)。

```bash
uv run pytest tests/backtest/ tests/plugin/test_strategy_adapter.py tests/store/test_backtest_runs.py -q
```

Expected: 全件 PASS (import 経路の変更のみ、挙動は不変)。

- [ ] **Step 13: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 14: 変異テスト**

1. `scheduler.py` の `if reason == "cron": self._last_cron_trade = now` を `on_trade_mission(reason)` の**後**に移動する改変 → `test_cron_deadline_advances_even_when_mission_callback_raises` が red (前進しなくなる)
2. `policy.py` の `except OSError:` を `except FileNotFoundError:` に戻す → `test_tail_returns_empty_on_permission_error` が red
3. `service.py` の `app.scheduler.tick(app.clock.now())` を `app.scheduler.tick(datetime.now(timezone.utc))` に戻す → clock 配線テストが red
4. `_watchdog_tick` の `app.mission_watch.time_fn()` を `time.monotonic()` に戻す → `test_watchdog_tick_uses_mission_watch_time_fn` が red (fake_time を進めても検出されなくなる)

各改変後に対応するテストを実行して red を確認し、元に戻す。

- [ ] **Step 15: Commit**

```bash
git add src/agentic_fx/core/scheduler.py src/agentic_fx/service.py \
  src/agentic_fx/loops/mission_watch.py src/agentic_fx/policy.py \
  tests/core/test_scheduler.py tests/test_service_app.py tests/test_policy.py \
  tests/backtest/factories.py
git add -A tests/backtest/  # rename 検出のため
git commit -m "$(cat <<'EOF'
fix: プラン5 park小口返済 (retry policy明文化/clock配線/provider seam/policy OSError/watchdog時刻源/conftest移動)

プラン5レジャー統合裁定の park 一覧 (codex I1/M2, sonnet I-3, fable
M3/M4/M7) を返済する。fable M5 (_check_llama_swap 文言) / M6 (ask失敗
文言) は現状の文言が既に具体的であることを確認し、変更不要と判定した
(triage — 詳細は Task 4 Step 1)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 5: worker 基盤 (1) — `db.connect_readonly` + `build_mission_registry` 抽出

設計書 §3.4「RO 接続の実装」(codex I-7) と §4.2「registry の共有再構築」を実装する。この task は子プロセス本体 (Task 7) に先行する準備部品 — 親 (`_assert_tools_registered` 検証) と子 (実行時) が同一関数を共有するための抽出。

**現状確認**: `build_app` (service.py:325-337) はツール配線を直接インラインで行っている (`registry = ToolRegistry(); registry.register_all(market_tools.build(...)); ...`)。子プロセスは同じ配線をゼロから再構築する必要があるが、`provider`/`econ`/`broker` はいずれも `conn`+`settings`+`clock` (+`activity`) から素直に構築できる薄いラッパーであることを確認済み (`account_tools.build` が呼ぶ `PaperBroker.equity()` は純粋な SELECT、`market_tools.build` が呼ぶ `EconCalendar.upcoming()` は `self.activity` に触れない — grep で確認済み)。

**Files:**
- Modify: `src/agentic_fx/store/db.py` (`connect_readonly` 新設)
- Modify: `src/agentic_fx/datafeed/price_provider.py` (`PriceProvider.__init__` に `readonly: bool = False` — CR-4 対応、裁定書 F-5)
- Create: `src/agentic_fx/tools/mission_registry.py`
- Modify: `src/agentic_fx/service.py:325-337` (`build_app` を `build_mission_registry` 経由に置換)
- Test: `tests/store/test_db_readonly.py` (新規), `tests/tools/test_mission_registry.py` (新規), `tests/datafeed/test_price_provider.py` (readonly モードのテスト追記)

**Interfaces:**
- Produces:
  - `db.connect_readonly(db_path: Path) -> sqlite3.Connection` — `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)`。書き込み系 PRAGMA (`journal_mode`) は発行しない。`busy_timeout=5000` のみ設定。`db_path` が存在しなければ `FileNotFoundError` (RO 接続は稼働中サービスの既存 DB を前提とする)。既存 `connect()` と同じ SQLite バージョン assert (Task 3 で追加済みの `sqlite3.sqlite_version_info < (3, 35, 0)` チェック) をここにも適用する
  - `PriceProvider.__init__(conn, settings, clock, readonly: bool = False)` (CR-4 対応) — `readonly=True` のとき `get_bars`/`_derive` 内の `ohlcv.upsert_bars(...)` 呼び出しをスキップする (cache 読み取りは通常どおり行う)。RO 接続 (`connect_readonly`) と組み合わせて子プロセスで使う。RPC 経由で親に書込を委譲する設計は採らない (裁定書 F-5 — RPC 面を拡大しない)
  - `mission_registry.build_mission_registry(loop: str, conn: sqlite3.Connection, settings: Settings, clock: Clock, rag: Rag, *, activity: ActivityLog, indicator_plugins: list[PluginMeta] | None = None, sandbox_run=None, readonly: bool = False, provider: "PriceProvider | None" = None) -> ToolRegistry` — `econ`/`broker` は内部で新規構築する (呼び出し側から受け取らない — 親の既存インスタンスと子の使い捨てインスタンスを同じ関数で作れることが目的)。**`provider` は非 None ならそれを使い、None なら内部構築する** (**レビュー反映 2 回目 / Task 5 sonnet Important-1**: 旧稿は provider も常に内部構築していたため、Task 4 で導入した `build_app(provider=...)` の注入 seam が registry 経由の tool から黙って迂回され、fake provider を注入しても実 yfinance を叩いていた — 実測再現済み)。**`provider` の呼び出し規約 (2 周目レビューで訂正 — 旧稿の「親のテスト注入専用の seam」という表現は誤りだった。この表現が実装側の docstring に「本番環境では常に None」と転写され、実装と真逆の記述を生んだ)**: **親 (`build_app`) は本番・テストを問わず常に自身の `provider` を渡す** (親が保持するインスタンスと registry 経由の tool が同一 provider を使うことが目的 — 渡さないと tool が別インスタンスを構築し、注入 seam も cache 状態も分岐する)。**子プロセス (mission_worker) は `provider` を渡さず `readonly=True` で内部構築する**。`provider` と `readonly=True` の**同時指定は `ValueError` で拒否する** (親は provider を渡し readonly=False、子は provider なしで readonly=True — 併用は誤用であり、`build_app` 側の `provider`/`quote_fn` 併用ガードと対称にする)、`market_tools`/`news_tools`/`account_tools`/`reflection_tools`/`signal_tools` の全 `ToolDef` を登録した `ToolRegistry` を返す。**`loop` 引数は本プランでは配線を分岐しない** (常に同じ全ツール集合を構築する — どのツールを実際に Mission に見せるかは `Mission.tools` の呼び出し側リスト `_TRADE_TOOLS` が決める。`loop` は将来の improve 系 registry 分岐 (プラン 9) に向けた forward-compat 引数であることを docstring に明記する)。`readonly` はそのまま `PriceProvider(..., readonly=readonly)` に渡すだけ (Task 7 が子プロセス構築時に `readonly=True` を渡す)
- Consumes: `agentic_fx.tools.{market_tools,news_tools,account_tools,reflection_tools,signal_tools}` の既存 `build` 関数群 (シグネチャ不変)

- [ ] **Step 1: 失敗するテストを書く (`connect_readonly`)**

`tests/store/test_db_readonly.py` を新規作成:

```python
"""db.connect_readonly (プラン 8 worker 基盤 — codex I-7)。"""
from __future__ import annotations

import sqlite3

import pytest

from agentic_fx.store.db import connect, connect_readonly, init_db


def test_connect_readonly_can_read_existing_rows(tmp_path):
    db_path = tmp_path / "agentic.db"
    rw = connect(db_path)
    init_db(rw)
    rw.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES ('trade', 'local', 'x', 'running', '2026-08-04T00:00:00+00:00')")
    rw.commit()

    ro = connect_readonly(db_path)
    row = ro.execute("SELECT loop FROM missions").fetchone()
    assert row["loop"] == "trade"


def test_connect_readonly_rejects_write(tmp_path):
    db_path = tmp_path / "agentic.db"
    init_db(connect(db_path))
    ro = connect_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute(
            "INSERT INTO missions (loop, runner, model, status, started_at) "
            "VALUES ('trade', 'local', 'x', 'running', '2026-08-04T00:00:00+00:00')")


def test_connect_readonly_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect_readonly(tmp_path / "does-not-exist.db")
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_db_readonly.py -q
```

Expected: FAIL (`ImportError: cannot import name 'connect_readonly'`)。

- [ ] **Step 3: `db.py` に実装**

`src/agentic_fx/store/db.py` の `connect` 関数 (Task 3 で SQLite バージョン assert 済み) の直後に追加:

```python
(`src/agentic_fx/store/db.py` の import 節に `import urllib.parse` を追加すること — 下の URI encode で使う。)

def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """読み取り専用で SQLite に接続する (mission worker 子プロセス専用 —
    設計書 §3.4 codex I-7)。

    書き込み系 PRAGMA (journal_mode 等) は発行しない — 既に WAL で稼働中の
    親プロセスの DB を読むだけであり、モード変更は不要かつ RO 接続では
    そもそも失敗する。`-wal`/`-shm` ファイルは親プロセスが作成済み (稼働中
    サービスが前提) なので読み取り可能。

    `db_path` が存在しない場合は `FileNotFoundError` — RO 接続は「既に
    `init_db` 済みの DB」を前提とし、この関数自身はスキーマを作らない
    (子プロセスがスキーマを作る権限を持つべきではない)。
    """
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old (>= 3.35 required)")
    if not db_path.exists():
        raise FileNotFoundError(
            f"connect_readonly requires an already-initialized DB: {db_path}")
    # path 部だけを percent-encode する (**レビュー反映 2 回目 / Task 5 codex
    # I-1**: 無加工だとファイル名中の `?` が query 区切り、`#` が fragment
    # 区切りと解釈され、`question?.db` / `hash#.db` で**別 DB を開く**。
    # `db_path.exists()` は元の literal path に対して成功するため存在確認でも
    # 防げない — 実測で `no such table: missions` を再現済み)。
    # `resolve().as_uri()` は採らない: symlink を解決するため exists() チェックと
    # 実接続が別パスを見る不整合を別の形で再導入し、相対パスの意味も変わる。
    uri = f"file:{urllib.parse.quote(str(db_path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_db_readonly.py -q
```

Expected: PASS。

- [ ] **Step 5 (CR-4 対応): 失敗するテストを書く (`PriceProvider` readonly モード)**

`tests/datafeed/test_price_provider.py` に以下を追加する (既存 `_provider`/`_fresh_bars` fixture をそのまま使う — ファイルを確認してから追記すること):

```python
def test_readonly_provider_skips_bar_cache_write(tmp_path):
    """CR-4 対応 (裁定書 F-5): readonly=True で構築した PriceProvider は
    get_bars 成功時に ohlcv.upsert_bars を呼ばない — RO 接続下でも
    `sqlite3.OperationalError` にならないことの単体ピン。"""
    s = load_settings(EXAMPLE)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW), readonly=True)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        bars = p.get_bars("USDJPY", "1m", 1)
    assert len(bars) == 30
    assert ohlcv.load_bars(conn, "USDJPY", "1m", source="yfinance") == []


def test_readonly_provider_skips_derived_bar_cache_write(tmp_path):
    """readonly=True は _derive 経路 (base 足の保存) でも書込をスキップする。"""
    s = load_settings(EXAMPLE)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW), readonly=True)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars(interval="1h", n=100)):
        p.get_bars("USDJPY", "4h", 5)
    assert ohlcv.load_bars(conn, "USDJPY", "1h", source="yfinance") == []
```

```bash
uv run pytest tests/datafeed/test_price_provider.py -q -k readonly_provider
```

Expected: FAIL (`TypeError: PriceProvider.__init__() got an unexpected keyword argument 'readonly'`)。

- [ ] **Step 6 (CR-4 対応): `price_provider.py` に `readonly` を実装**

`src/agentic_fx/datafeed/price_provider.py` の `PriceProvider.__init__` (66-73 行) を以下に変更する:

```python
    def __init__(self, conn: sqlite3.Connection, settings: Settings,
                 clock: Clock, readonly: bool = False) -> None:
        self.conn = conn
        self.settings = settings
        self.clock = clock
        # CR-4 (裁定書 F-5): 子プロセス (mission_worker.py) は conn に
        # db.connect_readonly (mode=ro) を渡す。get_bars/_derive の
        # cache 書込 (ohlcv.upsert_bars) は RO 接続下で
        # sqlite3.OperationalError になるため、readonly=True のときは
        # 書込呼び出し自体をスキップする (RPC 経由の親委譲はしない —
        # RPC 面を拡大しない設計裁定)。
        self.readonly = readonly
        self._bars_source: dict[tuple[str, str], str] = {}
        self._bars_origin: dict[tuple[str, str], str] = {}
```

`get_bars` (132-182 行) 内の以下の書込呼び出し (155-156 行) を条件分岐に変更する:

```python
                    if not self.readonly:
                        ohlcv.upsert_bars(self.conn, bars,
                                          source=_storage_source(name))
```

`_derive` (305-334 行) 内の書込呼び出し (333 行) も同様に変更する:

```python
        if not self.readonly:
            ohlcv.upsert_bars(self.conn, raw, source=_storage_source(source))
        return self._resample(raw, pair, interval)
```

- [ ] **Step 7 (CR-4 対応): テスト実行して PASS を確認**

```bash
uv run pytest tests/datafeed/test_price_provider.py -q
```

Expected: 全件 PASS (既存の書込ありテスト (`test_get_bars_caches` 等) が `readonly` 既定値 `False` のままなので壊れないことを含む)。

- [ ] **Step 8: 失敗するテストを書く (`build_mission_registry`)**

`tests/tools/test_mission_registry.py` を新規作成 (`tests/tools/test_tool_impls.py` 等の既存 fixture 命名規約を確認してから書く):

```python
"""build_mission_registry (プラン 8 worker 基盤 — 設計書 §4.2)。"""
from __future__ import annotations

import json
from pathlib import Path

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from agentic_fx.tools.mission_registry import build_mission_registry
from datetime import datetime, timezone

SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _clock():
    return FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))


def test_build_mission_registry_registers_all_trade_tools(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=lambda texts: [[0.0] * 4 for _ in texts])
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag, activity=activity)

    names = set(registry.names())
    for expected in ("get_ohlcv", "get_indicators", "search_news",
                     "get_econ_calendar", "get_positions", "get_account",
                     "get_recent_reflections", "search_reflections",
                     "get_signals"):
        assert expected in names, f"{expected} missing from registry"


def test_build_mission_registry_econ_calendar_does_not_touch_activity(tmp_path):
    """econ.upcoming() (get_econ_calendar が呼ぶ) は self.activity に触れない
    — 子プロセスが activity=None 相当の最小構成で呼んでも安全なことの
    構造的な確認 (worker.py が構築するときの前提)。

    I7 対応: `ToolRegistry.execute` はツール内で送出された全例外を
    `json.dumps({"error": ...})` に変換して返す (`registry.py:63-73`)
    ため、`result is not None` は常に真になり恒真テストになっていた
    (exploding activity が実際に呼ばれて `AssertionError` が飛んでも、
    その例外は registry に握り潰されて緑のまま通ってしまう)。ここでは
    (a) `activity.write` を呼んだかどうかを例外ではなく明示カウンタで
    記録し、(b) `result` を JSON decode して `list` 型 (`get_econ_calendar`
    の正常戻り値の型) であることを確認する — decode 結果が
    `{"error": ...}` の dict なら型不一致で確実に落ちる。
    """
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=lambda texts: [[0.0] * 4 for _ in texts])

    class CountingActivity:
        def __init__(self) -> None:
            self.write_calls = 0

        def write(self, *a, **k):
            self.write_calls += 1

    activity = CountingActivity()
    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag, activity=activity)
    result = registry.execute("get_econ_calendar", {"days": 1}, ["get_econ_calendar"])

    parsed = json.loads(result)
    assert isinstance(parsed, list), f"expected list payload, got: {result}"
    assert activity.write_calls == 0


def test_build_mission_registry_readonly_skips_bar_cache_write(tmp_path):
    """CR-4 対応: `readonly=True` で構築した registry の `get_ohlcv` は
    RO 接続 (`connect_readonly`) の下でも `ohlcv.upsert_bars` の書込を
    スキップして成功する — 子プロセス (`connect_readonly` で開いた conn)
    が `get_ohlcv` を呼んでも `sqlite3.OperationalError: attempt to write
    a readonly database` にならないことの配線ピン。"""
    from unittest.mock import patch

    from agentic_fx.core.contracts import Bar
    from agentic_fx.datafeed import sources
    from agentic_fx.store import ohlcv
    from agentic_fx.store.db import connect_readonly

    db_path = tmp_path / "x.db"
    rw_conn = connect(db_path)
    init_db(rw_conn)
    rw_conn.commit()

    def _fresh_bars():
        from datetime import timedelta
        step = timedelta(minutes=1)
        start = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)
        return [Bar("USDJPY", "1m", start + step * i,
                    148.0, 148.1, 147.9, 148.05, 10) for i in range(30)]

    ro_conn = connect_readonly(db_path)
    rag = Rag(tmp_path / "rag", embedding_function=lambda texts: [[0.0] * 4 for _ in texts])
    registry = build_mission_registry(
        "trade", ro_conn, SETTINGS, _clock(), rag,
        activity=ActivityLog(tmp_path / "logs" / "activity.log"), readonly=True)

    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        result = registry.execute(
            "get_ohlcv", {"pair": "USDJPY", "timeframe": "1m"}, ["get_ohlcv"])

    parsed = json.loads(result)
    # get_ohlcv の正常戻り値は list[dict] (market_tools.py 45-49 行)。RO
    # 接続で書込が実際に走っていれば OperationalError が
    # `{"error": ...}` の dict に化けて型不一致で検出される。
    assert isinstance(parsed, list) and len(parsed) == 30, result
    # 念のため RW 接続からも cache が空のままであることを確認する
    # (write skip の直接証跡)。
    assert ohlcv.load_bars(rw_conn, "USDJPY", "1m", source="yfinance") == []
```

- [ ] **Step 9: テスト実行して FAIL を確認**

```bash
uv run pytest tests/tools/test_mission_registry.py -q
```

Expected: FAIL (`ModuleNotFoundError: No module named 'agentic_fx.tools.mission_registry'`)。

- [ ] **Step 10: `mission_registry.py` を新規作成**

```python
"""Mission ツール配線の単一の組み立て関数 (プラン 8 worker 基盤 — 設計書 §4.2)。

親 (build_app、起動時 _assert_tools_registered 検証) と子
(mission_worker.py、実行時) が**同一関数**を共有する — 配線の二重化を
防ぎ、「親で検証したものと子で動くものが同じ」を関数の同一性で担保する。

`provider`/`econ`/`broker` はこの関数の内部で新規構築する (呼び出し側の
既存インスタンスを受け取らない) — conn/settings/clock/activity から素直に
組み立てられる薄いラッパーであり、親の長寿命インスタンスと子の使い捨て
インスタンスを同じコードパスで作れることが本モジュールの目的そのもの。
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store.rag import Rag
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, reflection_tools, signal_tools,
)
from agentic_fx.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.plugin.loader import PluginMeta


def build_mission_registry(
        loop: str, conn: sqlite3.Connection, settings: "Settings",
        clock: Clock, rag: Rag, *, activity: ActivityLog,
        indicator_plugins: "list[PluginMeta] | None" = None,
        sandbox_run=None, readonly: bool = False,
        provider: "PriceProvider | None" = None) -> ToolRegistry:
    """`loop` は本プランでは配線を分岐しない (常に同じ全ツール集合を
    構築する) — forward-compat 引数。どのツールを実際に Mission に
    見せるかは呼び出し側の `Mission.tools` リスト (`_TRADE_TOOLS` 等) が
    決める。将来の improve 系 registry 分岐 (プラン 9) で `loop` を
    使い始める想定。

    `readonly` (CR-4 対応、裁定書 F-5): 子プロセス (`mission_worker.py`)
    は `conn` に `db.connect_readonly` (SQLite `mode=ro`) を渡すため、
    `PriceProvider.get_bars`/`_derive` が通常経路で行う `ohlcv.upsert_bars`
    キャッシュ書込は `sqlite3.OperationalError: attempt to write a
    readonly database` になる。`readonly=True` は `PriceProvider` を
    cache 書込スキップモードで構築する — 既存 cache は引き続き読むが、
    新規取得したバーの書込だけをスキップする。RPC 経由で親に書込を
    委譲する設計は採らない (設計裁定: RPC 面を拡大しない — 裁定書
    F-5)。cache は性能最適化であり、親の scheduler tick が継続的に
    cache を温めるため実害は限定的。
    """
    provider = PriceProvider(conn, settings, clock, readonly=readonly)
    econ = EconCalendar(conn, activity, clock)
    broker = PaperBroker(conn, settings, clock)

    registry = ToolRegistry()
    registry.register_all(market_tools.build(
        provider, econ, settings, indicator_plugins=indicator_plugins,
        sandbox_run=sandbox_run))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn, broker))
    registry.register_all(reflection_tools.build(conn, rag, settings.pairs))
    registry.register_all(signal_tools.build(conn, settings, clock))
    return registry
```

- [ ] **Step 11: テスト実行して PASS を確認**

```bash
uv run pytest tests/tools/test_mission_registry.py -q
```

Expected: PASS。

- [ ] **Step 12: `service.py` を `build_mission_registry` 経由に置換**

`src/agentic_fx/service.py` の import 節に `from agentic_fx.tools.mission_registry import build_mission_registry` を追加する。`build_app` (315-337 行) の以下のブロック:

```python
    plugins_dir = root / "plugins"
    approved = plugin_loader.approved_plugins(conn_core, plugins_dir)

    signal_producer = SignalProducer()

    registry = ToolRegistry()
    registry.register_all(market_tools.build(
        provider, econ, settings, indicator_plugins=approved))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn_core, broker))
    registry.register_all(reflection_tools.build(conn_core, rag, settings.pairs))
    registry.register_all(signal_tools.build(conn_core, settings, clock))
    _validate_startup(settings)
    _assert_tools_registered(registry, _TRADE_TOOLS)
```

を以下に置き換える:

```python
    plugins_dir = root / "plugins"
    approved = plugin_loader.approved_plugins(conn_core, plugins_dir)

    signal_producer = SignalProducer()

    # プラン 8 worker 基盤: 親 (ここ) と子 (mission_worker.py) が同一関数
    # (build_mission_registry) でツール配線を組み立てる。親は既に構築済みの
    # econ/broker を再利用せず、conn_core から独立に再構築する
    # (子との配線一致を関数の同一性だけで担保するため — 親の長寿命
    # インスタンスを別途 provider/econ/broker として保持している事実と
    # 矛盾しない: registry 内のツールクロージャは新しく作った
    # provider/econ/broker を束縛するが、これらは conn_core を共有する
    # ため実質的に同じ DB 状態を見る)。
    # provider だけは build_app が保持しているインスタンスを渡す
    # (レビュー反映 2 回目 / Task 5 sonnet Important-1 — `build_app(provider=...)`
    # の注入 seam が registry 経由の tool から迂回されるのを防ぐ)。
    registry = build_mission_registry(
        "trade", conn_core, settings, clock, rag, activity=activity,
        indicator_plugins=approved, provider=provider)
    _validate_startup(settings)
    _assert_tools_registered(registry, _TRADE_TOOLS)
```

`market_tools`/`news_tools`/`account_tools`/`reflection_tools` の import が `service.py` の他の箇所で使われていないことを `grep -n "market_tools\.\|news_tools\.\|account_tools\.\|reflection_tools\." src/agentic_fx/service.py` で確認し、未使用になった import (49-52 行の `from agentic_fx.tools import (...)`) から `market_tools, news_tools, account_tools, reflection_tools` を削除する (`signal_tools`/`plugin_loader` は他箇所で引き続き使用されているため残す — 実ファイルを見て要否を確認してから編集すること)。

- [ ] **Step 13: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS (`_assert_tools_registered` が引き続き通ることを含む)。

- [ ] **Step 14: 変異テスト**

1. `mission_registry.py` の `registry.register_all(signal_tools.build(...))` 行を削除 → `test_build_mission_registry_registers_all_trade_tools` が red (`get_signals` missing)
2. `db.py:connect_readonly` の `mode=ro` を `mode=rw` に改変 → `test_connect_readonly_rejects_write` が red (INSERT が成功してしまう)
3. (CR-4) `price_provider.py:get_bars` の `if not self.readonly:` ガードを外して常に `upsert_bars` を呼ぶよう改変 → `test_readonly_provider_skips_bar_cache_write` および `test_build_mission_registry_readonly_skips_bar_cache_write` が red
4. (I7) `mission_registry.py` の econ 配線をそのままに、テスト対象を `CountingActivity` の代わりに `activity.write` を実際に呼ぶダミー econ 実装に差し替えて `write_calls == 0` assertion が red になることを一度確認する (実装差し替えではなくテストの自己点検 — 恒真化していないことの確認)

- [ ] **Step 15: Commit**

```bash
git add src/agentic_fx/store/db.py src/agentic_fx/datafeed/price_provider.py \
  src/agentic_fx/tools/mission_registry.py \
  src/agentic_fx/service.py tests/store/test_db_readonly.py \
  tests/datafeed/test_price_provider.py tests/tools/test_mission_registry.py
git commit -m "$(cat <<'EOF'
feat: db.connect_readonly + build_mission_registry (worker 基盤の共有配線点)

親 (build_app) と子 (mission_worker.py, Task 7) が同一関数でツール配線を
組み立てられるようにする (設計書 §3.4/§4.2)。PriceProvider に readonly
モードを追加し、RO 接続下でも get_ohlcv がキャッシュ書込なしで成功する
ことを配線テストで固定する (レビュー CR-4)。econ_calendar 非依存テストの
恒真化も修正する (レビュー I7)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 6: worker 基盤 (2) — LocalRunner の transcript sink 集約

設計書 §4.3「部分 transcript の保存範囲」(codex I-6) を実装する。`LocalRunner` の messages への append を単一 sink 関数に集約し、sink が (子プロセス内で) `event` フレームを送出できるようにする。この task は WorkerRunner/mission_worker (Task 7,10) に先行する準備 — sink 自体はインプロセスでテストでき、subprocess 隔離とは独立に検証できる。

**現状確認**: `src/agentic_fx/runners/local_runner.py` の messages への append site は 6 箇所 (`run()` メソッド内): 50 行目 (初期 user prompt — リスト構築)、99 行目 (assistant message)、144 行目 (tool result)、173 行目 (非文字列 content 修復依頼)、190 行目 (JSON parse エラー修復依頼)、201 行目 (schema エラー修復依頼)。

**Files:**
- Modify: `src/agentic_fx/runners/local_runner.py:34-46`(`__init__`)`,48-62`(`run` 冒頭)`,99,144,173,190,201`(各 append site)
- Test: `tests/runners/test_local_runner.py` (既存ファイルに追記)

**Interfaces:**
- Produces:
  - `LocalRunner.__init__(self, *, base_url, model, registry, transport=None, time_fn=time.monotonic, on_message: Callable[[dict], None] | None = None)` — `on_message` 新設 kwarg (既定 None = 既存挙動と完全互換)
  - `LocalRunner._sink(self, messages: list[dict], msg: dict) -> None` — 新設 private メソッド。`messages.append(msg)` の後、`self._on_message` が None でなければ呼ぶ。**`on_message` が例外を送出しても `run()` を止めない** (try/except で握って技術ログに warning — sink は観測性の記録であり Mission 実行そのものを阻害してはならない、という既存の `ActivityLog.write` 契約と同じ設計判断)。**I6 (裁定書) 注記**: この fail-soft 契約は「`on_message` が失敗しても Mission は継続してよい」ことを意味するが、`on_message` 自身が**外部への逐次通し番号付き送出** (mission_worker.py の `event` フレーム — Task 7) を行う場合はこの契約と衝突する。`_sink` は失敗を検出できないため、seq を消費した後の送出失敗は「継続」すると欠番になり親の `SeqTracker` が後続フレームを `ProtocolError` として reject する。**この種の `on_message` 実装 (Task 7) は `_sink` の fail-soft に頼らず、自分の中で失敗を検出して即座にプロセスを終了させる (fail closed) 責務を負う** — `_sink` 自体はここでは変更しない (汎用の観測性契約を保つ)

- [ ] **Step 1: 失敗するテストを書く**

`tests/runners/test_local_runner.py` に以下を追加する (既存ファイルの fixture — fake httpx transport の構築パターン — を確認し、それに揃えて書く):

```python
def test_on_message_called_for_every_append_including_initial_prompt():
    """sink 集約 (プラン 8 worker 基盤 — codex I-6): 初期 user prompt を
    含む全 append site で on_message が呼ばれる。"""
    seen: list[dict] = []
    registry = ToolRegistry()  # 既存 import 済みの ToolRegistry を使う
    transport = _completed_transport()  # 既存の「1 ターンで completed
                                          # する」fake transport ヘルパーを
                                          # 実ファイルで確認して使う
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=transport, on_message=seen.append)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    runner.run(mission)

    assert seen[0] == {"role": "user", "content": "hello"}
    assert any(m.get("role") == "assistant" for m in seen)


def test_on_message_exception_does_not_break_run(monkeypatch):
    """on_message が例外を送出しても run() は completed を返す
    (sink はベストエフォートの観測性記録)。"""
    def boom(msg):
        raise RuntimeError("sink failed")

    registry = ToolRegistry()
    transport = _completed_transport()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=transport, on_message=boom)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    result = runner.run(mission)
    assert result.status == "completed"
```

(`_completed_transport()` は既存テストが「1 ターンで JSON completed を返す」fake httpx transport を構築するために使っているヘルパーの実名に置き換えること — `grep -n "def.*transport\|httpx.MockTransport" tests/runners/test_local_runner.py` で確認してから書く。)

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/runners/test_local_runner.py -q -k on_message
```

Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'on_message'`)。

- [ ] **Step 3: 実装**

`src/agentic_fx/runners/local_runner.py:34-46` の `__init__` を以下に変更する:

```python
class LocalRunner(AgentRunner):
    def __init__(self, *, base_url: str, model: str, registry: ToolRegistry,
                 transport: httpx.BaseTransport | None = None,
                 time_fn: Callable[[], float] = time.monotonic,
                 on_message: Callable[[dict], None] | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._registry = registry
        self._client = httpx.Client(transport=transport)
        self._time = time_fn
        # プラン 8 worker 基盤 (codex I-6): messages への全 append を単一
        # sink に集約する。on_message は子プロセス (mission_worker.py) が
        # event フレームを親へ送出するためのコールバック。既定 None は
        # 既存挙動 (sink 呼び出しなし) と完全互換。
        self._on_message = on_message

    def close(self) -> None:
        """Close the HTTP client connection."""
        self._client.close()

    def _sink(self, messages: list[dict], msg: dict) -> None:
        """messages への唯一の append 経路。on_message はベストエフォート
        (例外を run() に伝播させない — 観測性の記録が Mission 実行を
        阻害してはならない)。"""
        messages.append(msg)
        if self._on_message is not None:
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001 — 観測性記録は実行を止めない
                _log.warning("on_message callback raised", exc_info=True)
```

`run()` の 6 箇所を書き換える。まず 48-50 行目 (初期メッセージ構築):

```python
    def run(self, mission: Mission) -> MissionResult:
        deadline = self._time() + mission.timeout_sec
        messages: list[dict] = []
        self._sink(messages, {"role": "user", "content": mission.prompt})
```

99 行目 (`messages.append(normalized_msg)`) を:

```python
            self._sink(messages, normalized_msg)
```

144 行目 (`messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})`) を:

```python
                    self._sink(messages, {"role": "tool",
                                          "tool_call_id": tc["id"],
                                          "content": result})
```

173 行目 (非文字列 content 修復依頼) を:

```python
                messages.append({"role": "user",
                                 "content": f"出力を JSON として解釈できません "
```

の直前の `messages.append` 呼び出しを `self._sink(messages, {...})` に置換する (元のコード `messages.append({"role": "user", "content": f"出力を JSON として解釈できません (content is not a string)。JSON オブジェクトのみを出力してください。"})` をそのまま `self._sink(messages, {...})` の形に変える — 中身の dict リテラルは無変更)。190 行目 (parse エラー修復依頼) と 201 行目 (schema エラー修復依頼) も同様に `messages.append(` を `self._sink(messages, ` に置換する (dict リテラルの中身は無変更)。

**実装者への注意**: 6 箇所のうち 173/190/201 行目は同じパターン (`messages.append({"role": "user", "content": f"..."})` → `self._sink(messages, {"role": "user", "content": f"..."})`) の機械的な置換であり、`messages.append(` という文字列をこの 3 箇所と 99/144 行目の計 5 箇所で `self._sink(messages, ` に置換すれば足りる (初期メッセージの 1 箇所だけがリスト構築から変わるため個別に書いた)。置換漏れが無いことを `grep -n "messages.append" src/agentic_fx/runners/local_runner.py` で確認し、**ヒットが 0 件になること**を確認する。

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/runners/test_local_runner.py -q
uv run pytest -q
```

Expected: 全件 PASS (既存の全 LocalRunner テストが sink 経由でも同じ挙動を保つこと)。

- [ ] **Step 5: 変異テスト**

`_sink` 内の `messages.append(msg)` はそのまま (削除すると `run()` 自体が壊れ既存スイート全体が red になるため対象外)、`if self._on_message is not None:` の呼び出しを削除する改変を行い、`test_on_message_called_for_every_append_including_initial_prompt` が red になることを確認してから元に戻す。

- [ ] **Step 6: Commit**

```bash
git add src/agentic_fx/runners/local_runner.py tests/runners/test_local_runner.py
git commit -m "$(cat <<'EOF'
refactor: LocalRunner の messages append を単一 sink 関数に集約

設計書 §4.3 (codex I-6)。on_message コールバックで子プロセスが event
フレームを送出できるようにする準備 (WorkerRunner 本体は Task 10)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 7: worker 基盤 (3) — mission worker 子プロセス本体 (JSON 行プロトコル + handshake + rlimit + PDEATHSIG)

設計書 §4.1〜§4.5、§4.7 の子プロセス側を実装する。親側 (`WorkerRunner`) は Task 10。

**設計判断 (writing-plans — 設計書の「必要サブセット」を具体化)**:
- **handshake で渡す `settings` は `Settings.model_dump()` のフルダンプ**とする (「必要サブセットのみ」ではなく全体)。理由: settings.yaml に秘密情報は含まれない (CLAUDE.md 絶対制約 — 秘密は `.env` のみ) ため全体を渡しても安全であり、「どのキーが必要か」を都度洗い出す部分集合方式は将来のツール追加のたびに漏れる (fail-open の温床)。
  - **Task 10 への申し送り (2026-08-08 指揮者の着手前実測)**: 親側は `model_dump()` ではなく **`model_dump(mode="json")`** を使うこと。`write_frame` は素の `json.dumps` を呼ぶため、`Settings` に将来 `Path`/`datetime`/`Enum` 型のフィールドが 1 つでも入ると `TypeError` で handshake 送出が壊れる。実測 (2026-08-08 現在): 現行 `Settings` は素の `model_dump()` でも `json.dumps` 可能・`mode="json"` でも `Settings.model_validate` に round-trip 可能 — つまり今は**どちらでも動くため、退行しても Task 10 のテストでしか気づけない**。Task 10 で `mode="json"` を使い、「handshake payload が JSON シリアライズ可能であること」の回帰ピンを 1 本置く。
- **`runner` 設定は個別に渡さない** — `settings.runner.trade`/`settings.llama_swap.base_url` が既に `settings` 全体に含まれるため、trade profile の子は常にこれらから `LocalRunner` を組み立てる (`backend == "claude"` は Global Constraints のとおり `RuntimeError` で fail closed)。
- **`indicator_plugins`/`approved plugins` は子が自分の RO 接続 + `plugins_dir` から自分で `plugin_loader.approved_plugins()` を呼んで再構築する** (`PluginMeta` は `Path` を含み JSON で素直にシリアライズできないため、ワイヤに乗せず子が独立に再計算する — 親と子は同じ `plugins_dir`/DB を見るので結果は一致する)。
- **`transcript_max_bytes` (累積上限) は子には渡さない** — truncate 判定は親側 (`WorkerRunner`, Task 10) の責務にする。子は `event` フレームを無条件に送出し続け、親が受信側で打ち切る (パイプの背圧を避けるため、子は送出を止めない — 親は打ち切った後も読み続けてパイプを詰まらせない)。

**Files:**
- Create: `src/agentic_fx/core/mission_protocol.py` (フレーム型定義 + seq 検証 — 親子共有)
- Create: `src/agentic_fx/mission_worker.py` (子プロセスエントリ)
- Test: `tests/core/test_mission_protocol.py`, `tests/test_mission_worker_protocol.py` (インプロセス — FakeWorker 越しの単体テスト。実 subprocess 起動は Task 10 の WorkerRunner 統合テスト/E2E (Task 20) に譲る)

**Interfaces:**
- Produces:
  - `mission_protocol.ProtocolError(Exception)` — フレーム形式・seq 違反の単一表現
  - `mission_protocol.SeqTracker` — `__init__(self) -> None` (内部カウンタ 1 起点) / `check(self, seq: Any) -> None` (`seq != 期待値` で `ProtocolError`。次回期待値を +1 する)
  - `mission_protocol.write_frame(stream, frame: dict) -> None` / `mission_protocol.read_frame(stream) -> dict | None` (EOF で None)
  - `mission_protocol.FRAME_TYPES_PARENT_TO_CHILD = frozenset({"handshake", "tool_rpc_result"})` / `FRAME_TYPES_CHILD_TO_PARENT = frozenset({"ready", "event", "tool_rpc", "result"})`
  - `mission_worker.main() -> None` — `python -m agentic_fx.mission_worker` のエントリ。標準入力から handshake (1 行) を読み、bootstrap (PDEATHSIG+ppid 照合 → rlimit → registry 構築) → `ready` 送出 → `LocalRunner.run(mission)` を実行 (on_message は `event` フレーム送出) → `result` 送出、の順で動く
  - `mission_worker._set_pdeathsig(sig: int) -> None` — `ctypes` で `prctl(PR_SET_PDEATHSIG, sig)` を呼ぶ (単体テストで `ctypes.CDLL` を fake 差し替え可能にする)
  - `mission_worker._make_on_message(protocol_out, out_seq: SeqTracker) -> Callable[[dict], None]` (I6 対応) — `LocalRunner(on_message=...)` に渡すコールバックを組み立てる。`write_frame` が失敗したら `os._exit(1)` で即座にプロセスを終了する (fail closed) — `LocalRunner._sink` (Task 6) の fail-soft 契約に頼ると、write 失敗後も seq だけ消費されたまま実行が継続し、次の成功フレームが親の `SeqTracker` に欠番として reject される (レビュー I6)
  - `mission_worker._RagRpcProxy(write_frame_fn, read_frame_fn, out_seq: SeqTracker, in_seq: SeqTracker) -> object` — `search_news(query, n=5) -> list[dict]` / `search_reflections(query, n=5) -> list[dict]` を実装 (`news_tools.build`/`reflection_tools.build` の duck-type 契約のみを満たす — 全メソッドは持たない)。`tool_rpc` フレーム送出 → `tool_rpc_result` を同期ブロッキング待ち (同時 1 件のみ、設計書 §4.3)。**`out_seq` は `main()` が `event`/`ready`/`result` の送出に使うのと同一インスタンスを共有する** (CR-3 対応 — 独立した `SeqTracker` を持たせると親の受信側検証 `in_seq` と衝突し `search_news`/`search_reflections` を使う Mission が初回呼出しで必ず `ProtocolError` になっていた)。`in_seq` は親→子方向 (`tool_rpc_result`) 専用の受信検証トラッカーで、`main()` が `handshake` の検証にも同じインスタンスを使う (I2 対応 — 親→子方向は従来 type/seq 検証が皆無だった)

- [ ] **Step 1: 失敗するテストを書く (`mission_protocol.py`)**

`tests/core/test_mission_protocol.py` を新規作成:

```python
"""mission_protocol の seq 検証 + フレーム I/O (プラン8, 設計書 §4.3)。"""
from __future__ import annotations

import io

import pytest

from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, read_frame, write_frame,
)


def test_seq_tracker_accepts_monotonic_sequence():
    t = SeqTracker()
    t.check(1)
    t.check(2)
    t.check(3)


def test_seq_tracker_rejects_duplicate():
    t = SeqTracker()
    t.check(1)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(1)


def test_seq_tracker_rejects_gap():
    t = SeqTracker()
    t.check(1)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(3)


def test_seq_tracker_rejects_regression():
    t = SeqTracker()
    t.check(1)
    t.check(2)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(1)


def test_write_then_read_frame_roundtrip():
    buf = io.BytesIO()
    write_frame(buf, {"type": "ready", "seq": 1, "ok": True})
    buf.seek(0)
    frame = read_frame(buf)
    assert frame == {"type": "ready", "seq": 1, "ok": True}


def test_read_frame_returns_none_on_eof():
    buf = io.BytesIO(b"")
    assert read_frame(buf) is None
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_mission_protocol.py -q
```

Expected: FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `mission_protocol.py` を実装**

```python
"""Mission worker の JSON 行プロトコル共有定義 (プラン8, 設計書 §4.3)。

`mission_worker.py` (子) と `runners/worker_runner.py` (親, Task 10) の
両方が import する。ワイヤ契約を 2 箇所の docstring で同期する方式
(sandbox.py/worker.py) ではなく、seq 検証ロジックそのものを共有コードに
する — 正しさが資金保護 (Mission 実行の停止窓) に波及するため、実装の
乖離が起き得ない形にする。

フレーム型 (全フレームに `seq` を付す。方向別に 1 起点の単調増加 —
codex M2-1):
- 親→子: `handshake` (起動時 1 回) / `tool_rpc_result`
- 子→親: `ready` (起動応答) / `event` (transcript メッセージ 1 件) /
  `tool_rpc` (RPC 要求) / `result` (最終ステータス、正常終端で 1 回)
"""
from __future__ import annotations

import json
from typing import Any, BinaryIO


class ProtocolError(Exception):
    """フレーム形式・seq 違反など、プロトコル契約に反する入力全般の単一
    表現 (fail closed — sandbox.py の `SandboxError`/`_dead` 意味論と同じ:
    違反を検出したらセッションは即座に死んだものとして扱う)。"""


FRAME_TYPES_PARENT_TO_CHILD = frozenset({"handshake", "tool_rpc_result"})
FRAME_TYPES_CHILD_TO_PARENT = frozenset({"ready", "event", "tool_rpc", "result"})


class SeqTracker:
    """方向別の 1 起点単調増加 seq 検証 (codex M2-1)。重複・逆行・欠番は
    すべて `ProtocolError` (「次に来るべき値と一致しない」の一律判定 —
    重複/逆行/欠番を種類分けしない。呼び出し側は方向ごとに別インスタンス
    を持つこと (親→子と子→親は別カウンタ)。"""

    def __init__(self) -> None:
        self._expected = 1

    def check(self, seq: Any) -> None:
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ProtocolError(f"seq must be an int, got {seq!r}")
        if seq != self._expected:
            raise ProtocolError(
                f"seq out of order: expected {self._expected}, got {seq}")
        self._expected += 1


def encode_frame(frame: dict) -> bytes:
    """フレームを wire 表現 (JSON 1 行) に変換する。**ストリームには触れない**。

    レビュー 2 周目 (codex): 送出は「serialize 失敗 (wire 未接触 — 同じ seq で
    別フレームを送り直してよい)」と「transport 失敗 (`write`/`flush` の例外 —
    **配信の有無が確定できない**)」を区別しなければならない。両者を
    `write_frame` の中で一体にしていると、呼び出し側はどちらが起きたのか
    判定できず、部分書込み後の再送が wire 上に壊れた行を作る。
    `mission_worker._send_frame` はこの関数で先に serialize してから
    ストリームへ書く。

    `json.dumps` の `TypeError` は `ProtocolError` に**正規化しない**
    (レビュー 2 周目 codex/sonnet で確認した意図的な非対称)。`ProtocolError`
    は「**受け取った**入力がプロトコル契約に反する」ことの表現であり、
    こちらは「自分が送ろうとした値が JSON にならない」ローカルなプログラム
    不備 — 別の故障クラスなので同じ型に潰すと親の分岐が誤る。
    """
    return json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n"


def write_frame(stream: BinaryIO, frame: dict) -> None:
    stream.write(encode_frame(frame))
    stream.flush()


def read_frame(stream: BinaryIO) -> dict | None:
    line = stream.readline()
    if not line:
        return None
    try:
        frame = json.loads(line)
    except ValueError as exc:
        # レビュー 1 周目 (codex I-1 / sonnet C1): `ProtocolError` は
        # 「プロトコル契約に反する入力全般の単一表現」と定義されているのに、
        # 旧実装は `json.JSONDecodeError` (と不正 UTF-8 の
        # `UnicodeDecodeError` — どちらも `ValueError` の派生) を素通し
        # していた。親 (`WorkerRunner`, Task 10) が `except ProtocolError`
        # をセッション違反の統一経路として実装しても捕捉できず、起動失敗の
        # 分類・後始末が例外型に依存してしまう。
        raise ProtocolError(f"malformed frame line: {exc}") from exc
    if not isinstance(frame, dict):
        # 同上。JSON の配列・文字列・数値は「行としては妥当」だがフレーム
        # 契約には反する。型注釈上の `dict` を裏切ったまま返すと、受け手の
        # `.get()` が `AttributeError` になり単一表現が崩れる。
        raise ProtocolError(
            f"frame must be a JSON object, got {type(frame).__name__}")
    return frame
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_mission_protocol.py -q
```

Expected: PASS。

- [ ] **Step 5: 失敗するテストを書く (`mission_worker.py` — インプロセス FakeWorker テスト)**

`tests/test_mission_worker_protocol.py` を新規作成する。実 subprocess は spawn せず、`mission_worker` の各関数をインプロセスで直接呼んで検証する (E2E 実 spawn は Task 20):

```python
"""mission_worker.py の単体テスト (インプロセス — 実 subprocess は spawn しない)。"""
from __future__ import annotations

import ctypes
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx import mission_worker
from agentic_fx.config import load_settings
from agentic_fx.store import signals
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import signal_tools


def test_set_pdeathsig_calls_prctl_with_expected_args(monkeypatch):
    calls: list[tuple] = []

    class FakeLibc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(mission_worker.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    mission_worker._set_pdeathsig(15)  # SIGTERM
    assert calls == [(1, 15, 0, 0, 0)]  # PR_SET_PDEATHSIG=1


def test_set_pdeathsig_raises_oserror_on_failure(monkeypatch):
    class FakeLibc:
        def prctl(self, *args):
            return -1

    monkeypatch.setattr(mission_worker.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(mission_worker.ctypes, "get_errno", lambda: 1)
    with pytest.raises(OSError):
        mission_worker._set_pdeathsig(15)


def test_rag_rpc_proxy_search_news_round_trip():
    """tool_rpc → tool_rpc_result の同期往復。

    CR-3 対応の回帰ピン: `out_seq` は `main()` が `ready`/`event`/`result`
    の送出に使うのと**同一インスタンス**を渡す (このテストでは 1 度も
    他フレームを送出していないので `out_seq` の初期値は 1 のまま —
    `tool_rpc` の seq は 1 になる)。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    outbound = io.BytesIO()
    out_seq = SeqTracker()
    in_seq = SeqTracker()

    def write_fn(frame):
        outbound.write((json.dumps(frame) + "\n").encode())

    # fake 親: search_news の RPC 要求に対して固定結果を返す応答を用意する
    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": True,
                "result": [{"title": "t", "body": "b", "source_name": "s"}]}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, out_seq, in_seq)
    result = proxy.search_news("usdjpy", n=5)
    assert result == [{"title": "t", "body": "b", "source_name": "s"}]

    outbound.seek(0)
    sent = json.loads(outbound.getvalue().splitlines()[0])
    assert sent["type"] == "tool_rpc"
    assert sent["seq"] == 1
    assert sent["name"] == "search_news"
    assert sent["args"] == {"query": "usdjpy", "n": 5}


def test_rag_rpc_proxy_shares_out_seq_with_other_child_to_parent_frames():
    """CR-3 の直接回帰ピン: `main()` が `ready` (seq=1) を送出済みの状態を
    模して `out_seq` を 1 個進めてから `_RagRpcProxy` に渡すと、
    `tool_rpc` の seq は 2 になる (親の単一 `in_seq` — 全フレーム種別
    共通 — が期待する次の値と一致する)。独立した `SeqTracker` を渡すと
    ここが 1 に戻ってしまい、親側で `ProtocolError` になる。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    outbound = io.BytesIO()
    out_seq = SeqTracker()
    out_seq._expected = 2  # ready (seq=1) を送出済みの状態を模す
    in_seq = SeqTracker()

    def write_fn(frame):
        outbound.write((json.dumps(frame) + "\n").encode())

    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": True,
                "result": []}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, out_seq, in_seq)
    proxy.search_news("usdjpy")

    outbound.seek(0)
    sent = json.loads(outbound.getvalue().splitlines()[0])
    assert sent["seq"] == 2


def test_rag_rpc_proxy_propagates_error():
    from agentic_fx.core.mission_protocol import SeqTracker

    def write_fn(frame):
        pass

    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": False,
                "error": "rag unavailable"}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, SeqTracker(), SeqTracker())
    with pytest.raises(RuntimeError, match="rag unavailable"):
        proxy.search_news("q")


def test_rag_rpc_proxy_rejects_wrong_frame_type(): 
    """I2 対応: 親→子方向 (tool_rpc_result) の type 検証。"""
    from agentic_fx.core.mission_protocol import ProtocolError, SeqTracker

    def write_fn(frame):
        pass

    def read_fn():
        return {"type": "event", "seq": 1, "rpc_id": "1", "ok": True, "result": []}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, SeqTracker(), SeqTracker())
    with pytest.raises(ProtocolError, match="tool_rpc_result"):
        proxy.search_news("q")


def test_rag_rpc_proxy_rejects_seq_gap():
    """I2 対応: 親→子方向の seq 検証 (欠番/重複/逆行を一律 ProtocolError)。"""
    from agentic_fx.core.mission_protocol import ProtocolError, SeqTracker

    def write_fn(frame):
        pass

    def read_fn():
        return {"type": "tool_rpc_result", "seq": 5, "rpc_id": "1", "ok": True,
                "result": []}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, SeqTracker(), SeqTracker())
    with pytest.raises(ProtocolError, match="seq"):
        proxy.search_news("q")


def test_on_message_exits_process_on_write_failure(monkeypatch):
    """I6 対応: write_frame が失敗したら os._exit(1) で即座にプロセスを
    終了する。LocalRunner._sink の fail-soft (Task 6) に頼って継続すると
    out_seq だけが消費され、次の成功フレームが親の SeqTracker に欠番
    として reject される。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    exit_calls: list[int] = []
    monkeypatch.setattr(mission_worker.os, "_exit", exit_calls.append)

    class BrokenStream:
        def write(self, data):
            raise BrokenPipeError("broken pipe")

        def flush(self):
            pass

    out_seq = SeqTracker()
    on_message = mission_worker._make_on_message(BrokenStream(), out_seq)
    on_message({"role": "assistant", "content": "x"})

    assert exit_calls == [1]


def test_main_rejects_handshake_with_wrong_type(monkeypatch, tmp_path):
    """I2 対応: 子は handshake フレームの type/seq を検証してから bootstrap
    に進む — 不一致なら ready を送らず即終了する (fail closed)。"""
    import io as _io

    frame = json.dumps({"type": "event", "seq": 1}).encode() + b"\n"
    monkeypatch.setattr(mission_worker.sys, "stdin",
                        type("S", (), {"buffer": _io.BytesIO(frame)})())
    captured = _io.BytesIO()
    monkeypatch.setattr(mission_worker, "_protect_protocol_stdout",
                        lambda: captured)

    mission_worker.main()

    captured.seek(0)
    lines = captured.getvalue().splitlines()
    assert len(lines) == 1
    sent = json.loads(lines[0])
    assert sent["type"] == "ready"
    assert sent["ok"] is False


def test_main_rejects_handshake_with_wrong_seq(monkeypatch, tmp_path):
    """I2 対応 (2026-08-08 指揮者の着手前照合で追加): `main()` の
    `in_seq.check(handshake.get("seq"))` を単独で pin する。

    `test_main_rejects_handshake_with_wrong_type` は type 検証が先に
    `ProtocolError` を送出するため **seq 検証行に到達しない** — その行を
    削除しても green のままになる (Task 2 の `--noconftest` と同じ
    「防御はあるがテストが無い」型の穴)。本テストは `type` を正しい
    `"handshake"` にしたうえで `seq` だけを不正にし、seq 検証行だけを
    red で守る。"""
    import io as _io

    frame = json.dumps({"type": "handshake", "seq": 7}).encode() + b"\n"
    monkeypatch.setattr(mission_worker.sys, "stdin",
                        type("S", (), {"buffer": _io.BytesIO(frame)})())
    captured = _io.BytesIO()
    monkeypatch.setattr(mission_worker, "_protect_protocol_stdout",
                        lambda: captured)

    mission_worker.main()

    captured.seek(0)
    lines = captured.getvalue().splitlines()
    assert len(lines) == 1
    sent = json.loads(lines[0])
    assert sent["type"] == "ready"
    assert sent["ok"] is False
    assert "seq" in sent["error"]


def test_rag_rpc_proxy_in_seq_continues_after_handshake():
    """親→子方向の seq 連続性 pin (2026-08-08 指揮者の着手前照合で追加)。

    `in_seq` は `main()` が `handshake` (seq=1) の検証に使うのと**同一
    インスタンス**であるため、本番では**最初の `tool_rpc_result` は
    seq=2** でなければならない。他の RPC テストはいずれも新品の `in_seq`
    に `seq: 1` を食わせており、この連続性を pin していない (どちらの
    実装でも green になる)。Task 10 の親側実装者がそれらをワイヤ仕様と
    読むと最初の `tool_rpc_result` を seq=1 で送り、**RAG 検索を使う
    Mission が初回呼出しで必ず `ProtocolError` になる** — CR-3 の
    親→子方向の鏡像。本テストがその契約を固定する。"""
    from agentic_fx.core.mission_protocol import ProtocolError, SeqTracker

    def write_fn(frame):
        pass

    in_seq = SeqTracker()
    in_seq.check(1)  # main() が handshake (seq=1) を検証済みの状態

    proxy_ok = mission_worker._RagRpcProxy(
        write_fn,
        lambda: {"type": "tool_rpc_result", "seq": 2, "rpc_id": "1",
                 "ok": True, "result": []},
        SeqTracker(), in_seq)
    assert proxy_ok.search_news("q") == []

    in_seq2 = SeqTracker()
    in_seq2.check(1)
    proxy_ng = mission_worker._RagRpcProxy(
        write_fn,
        lambda: {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1",
                 "ok": True, "result": []},
        SeqTracker(), in_seq2)
    with pytest.raises(ProtocolError, match="seq"):
        proxy_ng.search_news("q")


_BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_build_clock_default_rejects_stale_signal_as_wall_clock_advances(
        monkeypatch, tmp_path):
    """(I4/R2-CX-01) `_build_clock()` の既定 (`SystemClock()`) は呼ぶたび
    に壁時計を再評価する — Mission 実行中に実時間が進むと、`get_signals`
    の鮮度窓の基準時刻も一緒に進み、窓の外に出た signal は除外され続ける
    (fail closed)。`SystemClock.now` を monkeypatch して「1 回目の呼び出し
    は handshake 直後・2 回目は 45 分後」の壁時計を模擬し、同一 signal が
    1 回目は含まれ 2 回目は除外されることを検証する。

    `_build_clock()` の戻り値を `FixedClock(...)` (handshake 時点で 1 回
    だけ `now()` を取得して固定) に変異させると、2 回目の呼び出しでも
    基準時刻が進まず signal が除外されなくなる (fail-open) — 本テストは
    その場合に red になる (Task 20 の E2E は資金保護用の FixedClock しか
    使わず worker 内時刻を進めるシナリオを持たないため、この退行を拾える
    のは本テストのみ)。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    settings = load_settings(
        Path(__file__).resolve().parents[1] / "config"
        / "settings.yaml.example")

    # signal は handshake 時点 (_BASE) の 30 分前に観測された市場イベント
    # (bar_ts 基準)。since_hours=1 (60分) の窓には handshake 直後は入るが、
    # 壁時計が 45 分進んだ 2 回目には 75 分前になり窓 (60分) の外に出る。
    signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(_BASE - timedelta(minutes=30)).isoformat(),
        kind="signal", payload={"x": 1}, now=_BASE)

    wall_clock_ticks = iter([_BASE, _BASE + timedelta(minutes=45)])
    monkeypatch.setattr(
        "agentic_fx.core.contracts.SystemClock.now",
        lambda self: next(wall_clock_ticks))

    # `_build_clock()` 自身を呼ぶ (monkeypatch しない) — I4 変異
    # (SystemClock() → FixedClock(...)) を検出する対象はこの呼び出し。
    clock = mission_worker._build_clock()
    get_signals = signal_tools.build(conn, settings, clock)[0].func

    out_immediately = get_signals(pair="USDJPY", since_hours=1)
    assert {r["content_hash"] for r in out_immediately} == {"h1"}

    out_45min_later = get_signals(pair="USDJPY", since_hours=1)
    assert out_45min_later == []
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_mission_worker_protocol.py -q
```

Expected: FAIL (`ModuleNotFoundError: No module named 'agentic_fx.mission_worker'`)。

- [ ] **Step 7: `WorkerSettings` を新設し、rlimit 具体値を実測で確定する (§12 申し送り②)**

**実測手順 (writing-plans で実施済み — 結果を以下に転記する)**: mission worker が実際に import する依存一式 (`tools/market_tools`, `tools/news_tools`, `tools/account_tools`, `tools/reflection_tools`, `tools/signal_tools`, `runners/local_runner`, `store/db`, `store/rag` (`build_mission_registry` が `Rag` 型ヒントのため `chromadb` を import する) を読み込んだ直後の仮想メモリ・fd 数を測定した:

```bash
uv run python -c "
import os
import agentic_fx.tools.market_tools, agentic_fx.tools.news_tools
import agentic_fx.tools.account_tools, agentic_fx.tools.reflection_tools
import agentic_fx.tools.signal_tools
import agentic_fx.runners.local_runner
import agentic_fx.store.db as db
import agentic_fx.store.rag as rag_mod
import httpx, jsonschema, pandas, numpy, chromadb
with open(f'/proc/{os.getpid()}/status') as f:
    for line in f:
        if line.startswith(('VmRSS','VmSize','VmPeak')):
            print(line.strip())
    fds = os.listdir(f'/proc/{os.getpid()}/fd')
    print('num_fds', len(fds))
"
```

実測結果 (32 コア環境): `VmPeak ≈ 1.65GB` / `VmRSS ≈ 123MB` / `num_fds = 6` (2026-08-08 に指揮者が着手前に再実測して確認: `VmPeak 1653820 kB` / `VmRSS 125872 kB` / `num_fds 6` — 旧稿の `num_fds = 5` は誤り。`child_nofile: 128` の確定値には影響しない)。VmSize が RSS よりはるかに大きいのは OpenBLAS/numpy がコア数に比例したスレッド分の仮想アドレス空間を事前確保するため (`plugin/sandbox.py` の `_SINGLE_THREAD_ENV` コメントと同じ現象 — mission worker は plugin worker と異なり `_SINGLE_THREAD_ENV` を適用しない: pandas の演算性能を落とさないため。かわりに **RLIMIT_AS を「数 GB」に寛大に取る** ことで対応する、という設計書 §4.5 の方針をこの実測が裏付ける)。

**確定値**: `child_as_mb: 4096` (実測 VmPeak 1.65GB の約 2.4 倍の余裕) / `child_nofile: 128` (実測 5 fd に対し DB 接続・複数データソースへの HTTP 接続を見込んだ余裕) / `child_fsize_mb: 8` (mission worker は正常経路でファイルを書かない — plugin サンドボックスと同じ「想定外書込の検知」目的の小さい上限)。

**IM-9 (裁定書 F-12〜F-16) 注記**: 上記実測は単一の 32 コア機での測定値である。OpenBLAS/numpy のスレッド用仮想アドレス予約はコア数に比例するため、より多コアな本番機では同じ `child_as_mb: 4096` でも `RLIMIT_AS` を超過し、mission worker が `MemoryError`/`ready: false` (rlimit 到達によるメモリ確保失敗) で起動できなくなり得る。本番デプロイ先のコア数が実測環境 (32 コア) を大きく上回る場合は、上記実測コマンドをデプロイ先で再実行して `VmPeak` を確認し、`child_as_mb` を `settings.yaml` (example ではなく実運用設定) で「実測 VmPeak の 2.4 倍以上」に個別調整すること。症状は mission worker が `ready` を送らず `worker_startup_timeout_sec` で timeout する、または `ready: false` に `MemoryError` 相当のエラーメッセージが載る形で現れる。

`src/agentic_fx/config.py` に `WorkerSettings` を新設し `Settings` に追加する (`PluginSettings` の定義の直後、`class Settings(_Strict):` の直前に挿入):

```python
class WorkerSettings(_Strict):
    """mission worker (プラン8) の壁時計監視・resource limit・IPC 設定。
    既定値のみで動く (`Settings.worker` は default_factory を持つ)。
    """
    # 子プロセス側 resource limit (§12 申し送り② — 実測に基づく確定値。
    # 上記実測手順のコメント参照)。
    child_as_mb: int = Field(ge=1, default=4096)
    child_nofile: int = Field(ge=1, default=128)
    child_fsize_mb: int = Field(ge=1, default=8)
    # 親側の preemption エスカレーション (設計書 §4.7)。
    worker_grace_sec: float = Field(gt=0, default=30.0)
    worker_terminate_grace_sec: float = Field(gt=0, default=10.0)
    worker_startup_timeout_sec: float = Field(gt=0, default=30.0)
    # Mission 累積 transcript 上限 (設計書 §4.3 codex M-3)。
    transcript_max_bytes: int = Field(ge=1, default=1_048_576)
    # tick 内データ hooks が内部で使う全ネットワーククライアントの
    # timeout 上限 (設計書 §3.2 codex I2-1 — wall-clock 保証ではない)。
    data_hook_timeout_sec: float = Field(gt=0, default=30.0)
    # RAG RPC の親側応答待ち上限 (設計書 §4.3/§4.4)。
    rpc_timeout_sec: float = Field(gt=0, default=15.0)
    # 停止シーケンスの join 上限 (設計書 §5)。
    shutdown_join_timeout_sec: float = Field(gt=0, default=30.0)
```

`Settings` クラスに `worker: WorkerSettings = Field(default_factory=WorkerSettings)` を追加する (`plugin: PluginSettings = Field(default_factory=PluginSettings)` の直後)。

`config/settings.yaml.example` に以下を追記する (`plugin:` セクションの直後):

```yaml
worker:                        # mission worker (プラン8) の resource limit・preemption・IPC 設定。省略可
  child_as_mb: 4096             # RLIMIT_AS (MiB): 実測 VmPeak 1.65GB (32コア環境) の約2.4倍の余裕。多コア機では要再実測 (IM-9)
  child_nofile: 128             # RLIMIT_NOFILE
  child_fsize_mb: 8             # RLIMIT_FSIZE (MiB): 正常経路ではファイルを書かない前提の小さい上限
  worker_grace_sec: 30          # runner soft deadline の外側マージン (壁時計監視)
  worker_terminate_grace_sec: 10  # SIGTERM 後 SIGKILL までの猶予
  worker_startup_timeout_sec: 30  # 子の import〜ready まで
  transcript_max_bytes: 1048576   # Mission 累積 transcript 上限 (超過は truncate marker)
  data_hook_timeout_sec: 30       # tick 内データ hooks の全ネットワーククライアント timeout 上限
  rpc_timeout_sec: 15             # RAG RPC の親側応答待ち上限
  shutdown_join_timeout_sec: 30   # 停止シーケンスの join 上限
```

```bash
uv run pytest tests/store/ -q -k config
uv run pytest -q
```

Expected: 全件 PASS (新設 settings セクションは既存 `settings.yaml` にも `default_factory` で無変更ロード可能)。

**`config/settings.yaml` の同期について (明示判断 — 2026-08-08 指揮者)**: Global Constraints は「新キー追加時は `settings.yaml` と `.example` を両方同期する」と定めるが、`WorkerSettings` は全フィールドが既定値付きで `Settings.worker` も `default_factory` を持つため、**gitignore 対象の `config/settings.yaml` へ `worker:` セクションを追記しなくてもロードは成功する**。実装者は `.example` のみ更新すればよい (これは省略ではなく、この task 限定の明示判断)。実運用値の調整 (IM-9 の多コア機での `child_as_mb` 再実測など) が必要になった時点で `settings.yaml` 側に追記する。

- [ ] **Step 8: `mission_worker.py` を実装**

```python
"""Mission worker 子プロセス本体 (プラン8) — python -m agentic_fx.mission_worker
として起動される。`runners/worker_runner.py` (親, Task 10) が spawn する唯一の
想定呼び出し元。

**起動順序は厳守** (plugin/worker.py と同じ規律):
1. handshake (最初の 1 行) を読む → **type/seq を検証する** (I2 対応:
   親→子方向専用の `in_seq` トラッカーで `type == "handshake"` かつ
   `seq == 1` を確認。不一致は `ProtocolError` — 以降の `try` 節に含めて
   fail closed で `ready: false` を返す)
2. `_set_pdeathsig` (親死亡時に OS が SIGTERM を配送) → 設定**直後**に
   `os.getppid()` を `expected_parent_pid` と照合し、不一致 (= 設定前に
   親が死んで再親付けされた) なら即終了する (設計書 §4.8 codex I2-4)
3. resource limit (RLIMIT_AS/NOFILE/FSIZE/CORE=0) を設定 — **設定失敗は
   fail closed** (worker_profile="trade" は全項目)。CPU 制限は付けない
   (Mission の消費は LLM 待ちの壁時計であり親の preemption が受け持つ —
   設計書 §4.5)
4. `build_mission_registry` でツール配線を組み立てる (RAG は `_RagRpcProxy`
   を注入。**`out_seq` を先に構築し、`_RagRpcProxy` と `on_message` の
   両方に同一インスタンスを共有させる** — CR-3 対応。独立した
   `SeqTracker` を `_RagRpcProxy` に持たせると、親側 `WorkerRunner` の
   `in_seq` (子→親の全フレーム種別を単一トラッカーで検証) と衝突し、
   RAG 検索ツールの初回呼出しで必ず `ProtocolError` になっていた)
5. `ready` を送出
6. `LocalRunner.run(mission)` を実行 (`on_message` が `event` フレームを
   送出)
7. `result` を送出して終了

**worker_profile="trade" 限定** (本プランのスコープ — improve profile は
Task 18 で bootstrap を拡張する)。`settings.runner.trade.backend` が
"local" 以外 (= "claude") の場合は `RuntimeError` で fail closed する
(ClaudeRunner は本プランでは実装しない — Global Constraints)。

**I4 対応 (実時計)**: 子は Mission 実行中の鮮度判定 (`get_signals` 等)
に `_build_clock()` (既定 `SystemClock()`) を使う。handshake 時刻で
`FixedClock` に固定すると、Mission 後半でも鮮度境界の基準時刻が進まず、
実時刻なら範囲外になったはずの古い signal が残り続ける fail-open になる
(fork レビュー I4)。テストは `mission_worker._build_clock` を
monkeypatch して `FixedClock` を注入できる (seam は残す)。
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, encode_frame, read_frame,
)

if TYPE_CHECKING:
    from agentic_fx.core.contracts import Clock

_PR_SET_PDEATHSIG = 1


def _build_clock() -> "Clock":
    """本番は実時計を使う (I4 対応)。テストはこの関数を monkeypatch して
    `FixedClock` を注入できる (seam)。"""
    from agentic_fx.core.contracts import SystemClock
    return SystemClock()


def _set_pdeathsig(sig: int) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_PDEATHSIG) failed: "
                             f"{os.strerror(errno)}")


def _set_resource_limits(*, as_mb: int, nofile: int, fsize_mb: int) -> None:
    import resource

    as_bytes = int(as_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))
    fsize_bytes = int(fsize_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class _RagRpcProxy:
    """`news_tools.build(rag)`/`reflection_tools.build(conn, rag, pairs)`
    が要求する duck-type 契約 (`search_news`/`search_reflections`) のみを
    実装する (chromadb PersistentClient は多プロセス同時アクセス非対応 —
    設計書 §4.4)。`tool_rpc` は常に同時 1 件以下、同期ブロッキング待ち
    (設計書 §4.3)。

    `out_seq` (CR-3 対応) — 子→親方向 (`ready`/`event`/`tool_rpc`/
    `result`) の全フレームは `main()` が保持する単一の `SeqTracker` を
    共有する。`_RagRpcProxy` が自前の独立した `SeqTracker` を持つと、
    親側 `WorkerRunner.in_seq` (子→親の全フレーム種別を単一トラッカーで
    検証 — 設計書 §4.3 codex M2-1) の期待値と衝突し、RAG 検索ツールを
    使う Mission が初回呼出しで必ず `ProtocolError` になっていた
    (レビュー CR-3)。

    `in_seq` (I2 対応) — 親→子方向 (`tool_rpc_result`) 専用の受信検証
    トラッカー。`main()` が `handshake` の検証にも同じインスタンスを
    使う (親→子方向は 1 起点で共通)。**この共有の帰結として、本番では
    親が送る最初の `tool_rpc_result` の seq は 2 になる** (seq=1 は
    handshake が消費済み) — Task 10 の親側実装はこれに合わせること。"""

    def __init__(self, write_fn: Callable[[dict], None],
                read_fn: Callable[[], dict | None],
                out_seq: SeqTracker, in_seq: SeqTracker) -> None:
        self._write = write_fn
        self._read = read_fn
        self._out_seq = out_seq
        self._in_seq = in_seq
        self._rpc_counter = 0

    def _call(self, name: str, args: dict) -> Any:
        self._rpc_counter += 1
        rpc_id = str(self._rpc_counter)
        # レビュー 1 周目 (codex I-2): seq は**送出が成功してから**進める。
        # 先に消費すると、write/flush が失敗したときにその seq が wire に
        # 出ないまま欠番になり、次に成功したフレームを親の `SeqTracker` が
        # reject する。
        seq = self._out_seq._expected  # noqa: SLF001 — 送出側は採番に使う
        self._write({"type": "tool_rpc", "seq": seq,
                     "rpc_id": rpc_id, "name": name, "args": args})
        self._out_seq._expected += 1  # noqa: SLF001
        response = self._read()
        if response is None:
            raise RuntimeError("parent closed the pipe while awaiting tool_rpc_result")
        # I2 対応: type/seq を検証してから中身を信用する。
        if response.get("type") != "tool_rpc_result":
            raise ProtocolError(
                f"expected tool_rpc_result, got {response.get('type')!r}")
        self._in_seq.check(response.get("seq"))
        if response.get("rpc_id") != rpc_id:
            raise RuntimeError(
                f"tool_rpc_result rpc_id mismatch: expected {rpc_id}, "
                f"got {response.get('rpc_id')!r}")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "rag rpc failed")))
        return response.get("result")

    def search_news(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_news", {"query": query, "n": n})

    def search_reflections(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_reflections", {"query": query, "n": n})


def _make_on_message(protocol_out: Any, out_seq: SeqTracker) -> Callable[[dict], None]:
    """`LocalRunner(on_message=...)` に渡すコールバックを組み立てる
    (I6 対応 — 独立関数に切り出してあるのは単体テストで `os._exit` を
    monkeypatch し、write 失敗時の fail-closed 経路を `main()` 全体を
    実行せずに検証するため)。"""
    def on_message(msg: dict) -> None:
        try:
            _send_frame(protocol_out, out_seq, {"type": "event", "message": msg})
        except Exception:  # noqa: BLE001 — I6 対応 (fail closed)
            # LocalRunner._sink (Task 6) は on_message の例外を握って
            # run() を継続する契約 — しかし event フレームを送れないまま
            # 実行を続けると、親は transcript の一部を永久に受け取れない
            # (レビュー I6)。fail-soft に「継続」させず、このプロセスを
            # 即座に終了する (親は EOF/予期しない終了として Mission を
            # 失敗させる — 既に壊れた状態で run() を続けても無意味)。
            #
            # レビュー 2 周目 (codex) 以降、transport 失敗は
            # `_write_frame_or_die` がプロセスごと落とすため、この節へ
            # 実際に到達するのは **serialize 失敗** (transcript の message
            # が JSON にならない) のとき。その場合 seq は未消費なので
            # 欠番にはならないが、送れなかった事実は変わらないので
            # 同じく fail closed にする。
            os._exit(1)
    return on_message


def _protect_protocol_stdout() -> Any:
    """JSON 行プロトコル専用の書き込み先を確保し、fd 1 (stdout) を fd 2
    (stderr) へ付け替えて返す (`plugin/worker.py:_protect_protocol_stdout`
    と同じパターン — import 済みライブラリの意図しない `print`/警告出力が
    プロトコルの JSON 1 行ストリームに混ざるのを防ぐ)。**何よりも先に**
    (`read_frame` より前) 呼ぶ — import 時の副作用による stdout 汚染も
    ここで防がれる対象に含める。
    """
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return protocol_out


def main() -> None:
    protocol_out = _protect_protocol_stdout()

    # I2 対応: 親→子方向 (handshake/tool_rpc_result) 専用の受信検証
    # トラッカー。handshake は常に seq=1 (親の唯一の起動時送出)。
    in_seq = SeqTracker()
    # CR-3 対応: 子→親方向の ready/event/tool_rpc/result は 1 起点の単一
    # カウンタを共有する (設計書 §4.3 codex M2-1、親側 WorkerRunner.in_seq
    # がそう検証する)。レビュー 1 周目 (codex I-2) で `main()` の先頭へ
    # 移動した — 外側 `except` からも採番できる必要があるため。
    out_seq = SeqTracker()
    ready_sent = False
    try:
        # レビュー 1 周目 (codex I-1): handshake の読み取りを `try` の**内側**
        # へ移した。旧実装は `try` の外で `read_frame` を呼んでいたため、
        # 不正 JSON の handshake が `ProtocolError` を素通しさせて traceback
        # で異常終了し、**`ready: ok=False` を一切返さなかった** (親からは
        # 起動 timeout と区別がつかない)。EOF (None) は従来どおり正常終了。
        handshake = read_frame(sys.stdin.buffer)
        if handshake is None:
            return
        if handshake.get("type") != "handshake":
            raise ProtocolError(
                f"expected handshake, got {handshake.get('type')!r}")
        in_seq.check(handshake.get("seq"))

        expected_parent_pid = handshake["expected_parent_pid"]
        _set_pdeathsig(signal.SIGTERM)
        if os.getppid() != expected_parent_pid:
            # 設計書 §4.8 codex I2-4: prctl 設定前に親が死んで再親付け
            # されたレース。ready を送らずに即終了する (親の起動 timeout
            # がこれを検出する)。
            return
        settings_dict = handshake["settings"]
        worker_profile = handshake["worker_profile"]
        if worker_profile != "trade":
            raise RuntimeError(
                f"unsupported worker_profile in this plan: {worker_profile!r}")
        _set_resource_limits(
            as_mb=settings_dict["worker"]["child_as_mb"],
            nofile=settings_dict["worker"]["child_nofile"],
            fsize_mb=settings_dict["worker"]["child_fsize_mb"])

        from agentic_fx.config import Settings
        settings = Settings.model_validate(settings_dict)
        if settings.runner.trade.backend != "local":
            raise RuntimeError(
                f"runner.trade.backend={settings.runner.trade.backend!r} is "
                "not supported by mission_worker in this plan (ClaudeRunner "
                "is Plan 9 scope) — fail closed")

        from agentic_fx.store.db import connect_readonly
        from agentic_fx.tools import plugin_loader
        from agentic_fx.tools.mission_registry import build_mission_registry
        from agentic_fx.activity import ActivityLog

        conn = connect_readonly(Path(handshake["db_path"]))
        # I4 対応: 本番は実時計を使う (handshake["now"] で FixedClock に
        # 固定すると、Mission 後半でも鮮度判定の基準時刻が進まず
        # get_signals 等の鮮度検証が fail-open になる — レビュー I4)。
        clock = _build_clock()
        # 子は自身の scratch workdir に activity.log を持つ (§4.6 のとおり
        # econ.upcoming() は self.activity に触れないため実際には書かれ
        # ないが、EconCalendar のコンストラクタ契約を満たすためだけに
        # 必要 — mission_registry.py の docstring 参照)。
        activity = ActivityLog(Path.cwd() / "activity.log")
        plugins_dir = (Path(handshake["plugins_dir"])
                       if handshake.get("plugins_dir") else None)
        approved = (plugin_loader.approved_plugins(conn, plugins_dir)
                   if plugins_dir is not None else [])

        # CR-3 対応: `main()` 冒頭で構築した out_seq を、_RagRpcProxy と
        # on_message (event フレーム送出) の両方に**同一インスタンス**で
        # 共有させる。
        registry = build_mission_registry(
            "trade", conn, settings, clock,
            _RagRpcProxy(
                lambda frame: _write_frame_or_die(protocol_out, frame),
                lambda: read_frame(sys.stdin.buffer),
                out_seq, in_seq),
            activity=activity, indicator_plugins=approved, readonly=True)

        from agentic_fx.runners.base import Mission
        from agentic_fx.runners.local_runner import LocalRunner

        mission = Mission(**handshake["mission"])

        on_message = _make_on_message(protocol_out, out_seq)

        runner = LocalRunner(
            base_url=settings.llama_swap.base_url,
            model=settings.runner.trade.model, registry=registry,
            on_message=on_message)

        _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
        ready_sent = True

        try:
            result = runner.run(mission)
            _send_frame(protocol_out, out_seq, {
                "type": "result",
                "status": result.status, "output": result.output})
        except Exception as exc:  # noqa: BLE001 — 必ず result を送る
            _send_frame(protocol_out, out_seq, {
                "type": "result", "status": "failed", "output": None,
                "error": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:  # noqa: BLE001 — ready 送出前の失敗も報告する
        try:
            # レビュー 1 周目 (codex I-2): 旧実装は無条件に
            # `{"type": "ready", "seq": 1, ...}` を送っていた。`ready`
            # (seq=1) を送出**済み**でこの節に到達する経路が実在する
            # (内側 except 自身の送出が失敗した場合) ため、親の
            # `SeqTracker` から見て seq=1 の**重複**になっていた。
            # 送出済みなら `result: failed` として報告する。
            if ready_sent:
                _send_frame(protocol_out, out_seq, {
                    "type": "result", "status": "failed", "output": None,
                    "error": f"{type(exc).__name__}: {exc}"})
            else:
                _send_frame(protocol_out, out_seq, {
                    "type": "ready", "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001 — パイプが壊れていれば諦める
            pass


def _write_frame_or_die(protocol_out: Any, frame: dict) -> None:
    """1 フレームを protocol stream へ書く。**transport 失敗なら即プロセス終了**。

    レビュー 2 周目 (codex): `write`/`flush` の例外は「フレームが wire に
    1 バイトも出ていない」ことを**保証しない**。部分書込みの後に失敗して
    いれば、同じ seq で別フレームを送り直すと `{"type":...` の途中に別の
    JSON が連結されて**行が壊れる**。flush がデータを相手へ渡した後に失敗
    したのなら、親は既に最初のフレームを受理しており、再送は**重複**に
    なる。どちらが起きたかは呼び出し側から判定できない。

    したがって transport 失敗は回復不能として扱い、**再送せずに子を即座に
    終了する** (fail closed)。親は EOF / 異常終了として Mission を失敗
    させる — これは `_make_on_message` (I6) が既に採っている方針と同じ。

    serialize 失敗 (`encode_frame` の `TypeError` 等) は**ここへ来る前に**
    送出され、ストリームには触れない。呼び出し側はその場合だけ同じ seq で
    別フレームを送り直してよい。
    """
    line = encode_frame(frame)  # serialize 失敗はここ (wire 未接触 → 再送可)
    try:
        protocol_out.write(line)
        protocol_out.flush()
    except Exception:  # noqa: BLE001 — 配信有無が不明なので回復を試みない
        os._exit(1)


def _send_frame(protocol_out: Any, out_seq: SeqTracker, frame: dict) -> None:
    """子→親のフレームを 1 件送出する (`seq` はここで採番して付す)。

    **カウンタは送出が成功してから進める** (レビュー 1 周目 codex I-2)。
    旧実装 (`_next_seq`) は serialize/write/flush より**前**に消費していた
    ため、送出が失敗するとその seq が wire に出ないまま欠番になり、次に
    成功したフレームを親の `SeqTracker` が `ProtocolError` として reject
    した。実測: `runner.run()` の戻り値が壊れていて result フレームの
    構築中に落ちると、wire 上は `ready(1)` の次が `result(3)` になった。

    「送出が成功していないなら seq を進めない」が安全なのは **serialize
    失敗のときだけ** — transport 失敗は `_write_frame_or_die` がプロセス
    ごと落とすため、そもそも呼び出し側に戻らない (レビュー 2 周目 codex)。

    `SeqTracker` を採番器として流用する意図は Task 7 本文のとおり
    (「次に来る/送り出すべき値」の意味が受信検証と送信採番で一致する)。
    """
    payload = dict(frame)
    payload["seq"] = out_seq._expected  # noqa: SLF001 — 送出側は採番に使う
    _write_frame_or_die(protocol_out, payload)
    out_seq._expected += 1  # noqa: SLF001


if __name__ == "__main__":
    main()
```

**実装者への注意**: `SeqTracker` は本来「受信側の検証」用に設計されているが、子の**送出側**でも「次に払い出すべき値」を同じフィールドから取り出す採番器として流用している (`_send_frame` と `_RagRpcProxy._call` の `# noqa: SLF001` コメントのとおり、意図的な private 属性アクセス。**旧稿にあった `_next_seq` / `_RagRpcProxy._seq_next` はレビュー 2 周目で廃止済み** — 採番は必ず送出成功後に進めるため、送出と不可分な位置に置いた)。別の採番専用クラスを新設しない判断 (writing-plans) — `SeqTracker` の内部カウンタの意味 (「次に来る/送り出すべき値」) が両用途で一致するため。Task 10 (`WorkerRunner`) 側は同じ `SeqTracker` を**受信検証**に使う (`check()` 経由) — 送出用と受信検証用を混同しないこと (子の out_seq は「自分がこれから送る値」を採番するだけで `check()` は呼ばない。親の受信側 `SeqTracker` が `check()` で検証する)。

- [ ] **Step 9: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_mission_worker_protocol.py -q
```

Expected: PASS。

- [ ] **Step 10: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS (mission_worker.py は他モジュールから import されないため既存スイートに影響なし)。

- [ ] **Step 11: 変異テスト**

1. `mission_protocol.SeqTracker.check` の `if seq != self._expected:` を `if False:` に改変 → `test_seq_tracker_rejects_duplicate`/`_gap`/`_regression` が red
2. `mission_worker._set_pdeathsig` 呼び出し後の `os.getppid() != expected_parent_pid` チェックを削除 (次 Step の実装から一時的に外す) → 対応する統合テストは Task 10 の E2E 側にあるため、ここでは `test_set_pdeathsig_calls_prctl_with_expected_args` に相当する fake 呼び出し引数アサートが red にならないことを逆に確認 (この変異はレビュー時に「Task 10 の統合テストで拾われる」ことをコメントで明記するに留める — Task 7 単体では観測できない設計上の限界)
3. `_RagRpcProxy._call` の `if not response.get("ok"):` を削除 → `test_rag_rpc_proxy_propagates_error` が red
4. (CR-3) `main()` の `_RagRpcProxy(...)` 呼び出しに渡す `out_seq` 引数を `SeqTracker()` (新規独立インスタンス) に差し替える → `test_rag_rpc_proxy_shares_out_seq_with_other_child_to_parent_frames` が red (`sent["seq"]` が 2 ではなく 1 に戻る)
5. (I2) `_RagRpcProxy._call` の `if response.get("type") != "tool_rpc_result":` チェックを削除 → `test_rag_rpc_proxy_rejects_wrong_frame_type` が red。`self._in_seq.check(response.get("seq"))` 行を削除 → `test_rag_rpc_proxy_rejects_seq_gap` が red
6. (I2) `main()` の `handshake.get("type") != "handshake"` チェックを削除 → `test_main_rejects_handshake_with_wrong_type` が red。**別途** `in_seq.check(handshake.get("seq"))` 行を削除 → `test_main_rejects_handshake_with_wrong_seq` が red
   - **2026-08-08 指揮者の着手前照合による訂正**: 旧稿は「`in_seq.check(...)` 削除 → `test_main_rejects_handshake_with_wrong_type` が red」と書いていたが**これは誤り**。同テストの入力は `type="event"` であり、`main()` は type を先に検証して `ProtocolError` を送出するため **seq 検証行には到達しない** — 削除しても green のままになる。結果として handshake の seq 検証が**無防備のまま出荷される** (Task 2 の `--noconftest` と同型の穴)。Step 5 に `test_main_rejects_handshake_with_wrong_seq` を追加してこの行を単独で pin した
7. (I4/R2-CX-01) `_build_clock()` の戻り値を `SystemClock()` から `FixedClock(SystemClock().now())` (handshake 相当で 1 回だけ固定) に改変 → `test_build_clock_default_rejects_stale_signal_as_wall_clock_advances` が red になる (2 回目の `get_signals` 呼び出しでも基準時刻が進まず `out_45min_later` が `{"h1"}` のままで `== []` の assert に失敗する)
8. (I6) `_make_on_message` 内の `try/except` を削除し `os._exit(1)` 呼び出しごと外す → `test_on_message_exits_process_on_write_failure` が red (`exit_calls` が空のまま、または `BrokenPipeError` が素通しで送出されテストがエラー終了する — いずれにせよ green にならない)
9. (親→子 seq 連続性) `_RagRpcProxy` に渡す `in_seq` を `main()` 共有のものから新規 `SeqTracker()` に差し替える → `test_rag_rpc_proxy_in_seq_continues_after_handshake` が red (handshake 消費後の期待値 2 が 1 に戻る)

**Step 11 の実測による修正 (2026-08-08 指揮者。実測記録は `.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/task07-mutation-report.md`)**:

プラン記載 9 件 + 自主追加 17 件を実測したところ、**16 件が生存**した。根本原因は 1 つ — **`main()` の bootstrap 本体に一切テストが無い**。Step 5 のテスト 12 本は `_RagRpcProxy`/`_make_on_message`/`_set_pdeathsig`/`_build_clock` の**単体**と、`main()` の**外側 `except` に落ちる 2 経路**しか通らない。

- **プラン記載の変異 4・6a は無効だった** (どちらも生存):
  - 変異 4 (と着手前に追加した変異 9) は `main()` の call site を変異させるが、対応テストは `_RagRpcProxy` を直接構築するため red にならない。**単体テストは契約を pin するが配線を pin しない**
  - 変異 6a (handshake type チェック削除) も生存。削除しても後続の `handshake["expected_parent_pid"]` が `KeyError` を投げ、外側 `except` が同じ `ready: ok=False` を返すため。テストがエラー**内容**を検証していなかった
- **変異 2 (ppid 照合) を「Task 7 単体では観測不能」と本文に書いたのは誤り**。`expected_parent_pid` を `os.getppid()+1` にして `main()` を駆動すれば「何も送らずに終了する」ことで pin できる
- 特に重大な生存: **`runner.trade.backend != "local"` の fail closed (Global Constraints の強制点) が無防備**。CR-3 (`out_seq` 共有)・CR-4 (`readonly=True`)・§12 申し送り② (rlimit 適用) の回帰も同様に無防備だった

**対応**: `tests/test_mission_worker_protocol.py` に **12 本を追加**し、既存 `test_main_rejects_handshake_with_wrong_type` に `assert "handshake" in sent["error"]` を追加した。追加テスト: `test_main_happy_path_emits_ready_event_result_in_one_seq_sequence` / `test_main_shares_out_seq_and_in_seq_with_rag_rpc_proxy` / `test_main_builds_registry_readonly_and_applies_resource_limits` / `test_main_reports_result_failed_when_runner_raises` / `test_main_exits_without_ready_when_reparented` / `test_main_rejects_unsupported_worker_profile` / `test_main_fails_closed_when_runner_backend_is_claude` / `test_set_resource_limits_sets_all_four_limits` / `test_seq_tracker_rejects_bool_as_seq` / `test_rag_rpc_proxy_rejects_rpc_id_mismatch` / `test_rag_rpc_proxy_raises_when_parent_closes_pipe` / `test_rag_rpc_proxy_advances_out_seq_across_successive_calls`。**生存 16 件 → 2 件**。

**残存 2 件は accepted-unpinned** (意図的に次 task へ送る): 追加C (`write_frame` の `flush()` 削除) と 追加O (`_protect_protocol_stdout` の `dup2` 削除)。どちらも**実プロセスでしか症状が出ない** (インプロセステストは `_protect_protocol_stdout` 自体を monkeypatch している)。**Task 10 の実 spawn 統合テスト / Task 20 の E2E で拾う**。

**後続 task への一般化**: `main()` 相当の「配線を組み立てる関数」は、構成要素の単体テストが全部緑でも無防備になる。Task 10 (`WorkerRunner`)・Task 13 (`MissionSupervisor`)・Task 19 (`build_app`/`App.close`) では、**単体テストとは別に「配線そのものを駆動して観測点を assert するテスト」を必ず 1 本以上置くこと**。

**レビュー 1 周目の反映 (2026-08-08。codex 主査 I3 件 + sonnet 副査 I4 件を全件採用。上記 Step 3 / Step 8 のコードブロックは反映後の実装と一致させてある)**:

1. **`read_frame` の例外を `ProtocolError` に正規化** — 旧稿は `json.JSONDecodeError` (と不正 UTF-8 の `UnicodeDecodeError`) を素通ししており、`ProtocolError` の「プロトコル契約に反する入力全般の**単一表現**」という定義が成立していなかった。非 JSON object も拒否する。**Task 10 の親側は `except ProtocolError` をセッション違反の統一経路として書いてよい**
2. **`main()` の handshake 読取を `try` の内側へ移動** — 旧稿は `try` の外で `read_frame` を呼んでいたため、不正 JSON の handshake で `ready: ok=False` を**一切返さず** traceback で異常終了していた (親からは起動 timeout と区別がつかない)
3. **`_next_seq` を廃し `_send_frame` を新設 — seq は送出成功後に進める** — 旧稿は serialize/write/flush の**前**に採番を消費していたため、送出が失敗するとその seq が wire に出ないまま**欠番**になり、次に成功したフレームを親の `SeqTracker` が reject した。`_RagRpcProxy._call` の `tool_rpc` 採番も同様に修正 (`_seq_next` は廃止)
4. **外側 `except` の `"seq": 1` ハードコードを廃止** — `ready` 送出**後**にこの節へ到達する経路が実在する (内側 `except` 自身の送出が失敗した場合) ため、親から見て seq=1 の**重複**になっていた。`ready_sent` フラグを見て `result: failed` へ切り替える
5. **accepted-unpinned としていた 2 件を Task 7 内で pin した** — `flush()` / `dup2` は `os.pipe()` で**実 subprocess を spawn せずに**観測できる。両レビュアーが一致して「後送は過度に保守的」と指摘し、指揮者の判断を撤回した。実 subprocess での「効能」検証は引き続き Task 10/20 で行う (二層)
6. **`_set_resource_limits` の fail closed を pin** (sonnet 単独検出) — docstring が「設定失敗は fail closed」と明記しているのに、`_drive_main` が常に成功する fake に差し替えていたため無防備だった

**Task 10 への申し送り (codex 主査)**: 子の `out_seq`/`in_seq` 共有は現状 `_expected` の内部状態観測で pin している。より堅いのは **wire レベルの往復** — fake runner が registry 内の RAG ツールを実行し、`ready(1) → tool_rpc(2) → result(3)` と handshake(1) 後の `tool_rpc_result(2)` を実際に往復させる統合テスト。Task 10 でこれを必須にすること。

**レビュー 2 周目の反映 (2026-08-08。差分限定レビュー。codex Important 1 + Minor 1 / sonnet Minor 3。重大度が割れたため指揮者が再判定し、codex の Important を採用)**:

1. **[codex Important] 「送出例外 ⇒ wire 未接触」は成立しない仮定だった** — 1 周目の修正 (`_send_frame`) は `write`/`flush` の例外を受けて**同じ seq で再送**していたが、`write`/`flush` の例外は「フレームが wire に 1 バイトも出ていない」ことを**保証しない**。部分書込みの後に失敗していれば再送は `{"type":...` の途中に別 JSON を連結して**行を壊す**。flush がデータを相手へ渡した後の失敗なら親は既に受理済みで**重複**になる。**指揮者が 1 周目の修正で持ち込んだ新しい欠陥**であり、2 周目 (= 修正ラウンドのレビュー) が捕まえた実例
   - **対応**: `mission_protocol.encode_frame()` を分離し、`mission_worker._write_frame_or_die()` を新設した。**serialize 失敗** (wire 未接触) は例外として上げて同じ seq での再送を許し、**transport 失敗** (配信有無が不明) は `os._exit(1)` で**再送せず即終了**する (fail closed — 親は EOF/異常終了として Mission を失敗させる。`_make_on_message` が既に採っていた方針と同じ)。`_RagRpcProxy` の `tool_rpc` 送出も同じ writer を通す
   - **テストの脆さも同時に指摘された**: `_FlakyStream` は失敗回に**書き込む前に** raise しており、実 I/O の曖昧性 (部分書込み) をモデル化していなかった。`partial_bytes` を追加し、「部分書込み後の失敗」「全バイト書込み後の flush 失敗」「serialize 失敗」の 3 経路を個別に pin し直した
2. **[sonnet 単独] `_RagRpcProxy._call` の「送出成功後に採番」が未 pin** — pre-increment に戻しても 1499 件が green だった (Task 6 と同型の append-site の穴)。回帰ピンを追加
3. **[sonnet 単独] `write_frame` の `TypeError` を `ProtocolError` に正規化しない非対称が未文書** — `encode_frame` の docstring に理由を明記 (`ProtocolError` は「**受け取った**入力が契約違反」の表現。こちらは「自分が送る値が JSON にならない」ローカルなプログラム不備で別の故障クラス)
4. **[codex Minor + sonnet 単独] 廃止済み helper への言及が散文に残っていた** — 上記「実装者への注意」と **Task 18 のスケッチコード** (`_next_seq` を使っていた) を現行 API に更新

最終: 変異 **40 件を再走して生存 1 件** — 生存した 1 件は `encode_frame` を `write_frame` にインライン戻しする**等価変異** (equivalent mutant: 出力がバイト単位で同一なので、原理的にどのテストでも区別できない)。`uv run pytest -q` = **1504 passed, 1 deselected** (ベースライン 1459 + 45)。

**レビュー 3 周目 (2026-08-08。監視役の判断で実施。codex 単独・差分限定)**: **指摘なし**。serialize/transport の責務分割に曖昧に混ざる経路はなく、`_make_on_message` の guard も (serialize 失敗という) 独立した責務が残っているため死んでいない、`_ExitCalled(BaseException)` は `main()` の内外の `except Exception` を両方通過するので exit 後のコードがテストでだけ実行されることはない、`_FlakyStream` の write 回数依存は assertion (先頭 2 行の type + 3 つ目が改行なし断片 + 再送なし) が意図した経路を同定している、と全て根拠つきで追認された。指揮者は結論の土台となる事実主張 (「Task 10 に EOF-before-result のテストが明記されている」) を現物照合し、一致を確認した。

**3 周目が拾った唯一の実質的成果**は上記 Task 10 への前提の明文化 (`test_worker_runner_child_eof_before_result_is_failed` の直下に転記済み)。**指摘ゼロだが「打ち切ってよい」という判断の根拠を得た**という点で、監視役が 3 周目を指示した意義はあった。

**未検査として明示された点** (codex): `BinaryIO.write()` の**短い正常 return** を検査していない。本番の `protocol_out` は blocking fd を `os.fdopen(..., "wb")` した `BufferedWriter` であり下層の partial write は処理されるため、今回の経路では欠陥ではないと判断された。**親側 (Task 10) が別種のストリームを使う場合はこの前提が崩れる**ので注意。

**変異 driver の落とし穴 (実測)**: 変異が `os._exit` を踏むと **pytest プロセスごと停止**し、driver からは「FAILED 行なし = 生存」に見える。`_write_frame_or_die` 関連の変異で実際に偽の生存を 1 件出した。**pytest の要約行に `passed`/`failed`/`error` が無ければ「異常終了 = 検出扱い」とする判定を driver に入れること**。あわせて、fail-closed 経路を持つコードのテストは `os._exit` を必ず monkeypatch する (「終了しないこと」の assert も pin になる)。

- [ ] **Step 12: Commit**

```bash
git add src/agentic_fx/core/mission_protocol.py src/agentic_fx/mission_worker.py \
  src/agentic_fx/config.py config/settings.yaml.example \
  tests/core/test_mission_protocol.py tests/test_mission_worker_protocol.py
git commit -m "$(cat <<'EOF'
feat: mission worker 子プロセス本体 (JSON行プロトコル + handshake + rlimit + PDEATHSIG)

設計書 §4.1-§4.5/§4.7 の子プロセス側 (worker_profile=trade 限定)。
親側 WorkerRunner は Task 10 で実装する。out_seq を _RagRpcProxy と
event 送出で共有し RAG ツール初回呼出しのセッション強制終了を修正
(レビュー CR-3)。親→子方向の type/seq 検証を追加し (レビュー I2)、
本番の鮮度判定を実時計に変更する (レビュー I4)。子の PriceProvider は
readonly=True で構築する (レビュー CR-4、Task 5 参照)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 8: worker 基盤 (4) — Landlock ctypes モジュール

設計書 §4.6「Landlock による FS 自己制限」を実装する。improve worker profile (Task 18) が使う独立した小モジュール — Landlock 自体は本 task で完結してテストでき、improve profile への配線は Task 18 に譲る。

**実機検証済み (writing-plans で実施。2026-08-08 に指揮者が着手前へ再実測 — kernel 7.0.0-**29**-generic で ABI バージョン 8、syscall 列は下記のとおり全て成功。旧稿の `7.0.0-28-generic` は当時の版数)**: 本環境 (x86_64) で syscall 番号・構造体レイアウトを実際に呼び出して検証した — `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` (syscall 444) が ABI バージョン 8 を返す (Landlock 利用可能)、`landlock_add_rule` (syscall 445) で `O_PATH` ディレクトリ fd に対する読み取り専用ルールを追加できる、`prctl(PR_SET_NO_NEW_PRIVS, 1)` → `landlock_restrict_self` (syscall 446) の順で自己制限を適用した後、許可ディレクトリ配下は `os.listdir` が成功し、`/tmp`・`/etc/hostname` は `PermissionError` (errno 13) になることを実測確認済み。**構造体レイアウトの注記 (2026-08-08 指揮者が実測。レビュアーはここを再導出しなくてよい)**: カーネルの `struct landlock_path_beneath_attr` は `__attribute__((packed))` で **12 バイト**だが、`ctypes.Structure` (packed 指定なし) では **16 バイト**になる。ただし**フィールドオフセットは 0 / 8 で一致**し (`__u64` の次に `__s32` は自然アライメントでも offset 8)、`landlock_add_rule` はサイズを引数に取らずカーネルが自分の 12 バイトを読むだけなので、**末尾パディングは無害**。`_pack_ = 1` を足す必要はない (足しても動くが挙動は変わらない)。

**本モジュールは x86_64 Linux 専用** (syscall 番号はアーキテクチャ依存 — aarch64 等では異なる番号になる。`platform.machine() != "x86_64"` は起動時に `LandlockUnavailable` として拒否する)。

**Files:**
- Create: `src/agentic_fx/core/landlock.py`
- Test: `tests/core/test_landlock.py`

**Interfaces:**
- Produces:
  - `landlock.LandlockUnavailable(Exception)` — カーネル非対応・syscall 失敗・非対応アーキテクチャの単一表現
  - `landlock.is_available() -> bool` — ABI バージョン問い合わせ (`LANDLOCK_CREATE_RULESET_VERSION` フラグでの `landlock_create_ruleset` 呼び出し) が 1 以上を返せば True。`platform.machine() != "x86_64"` なら無条件 False
  - `landlock.restrict_to(*, read_only_paths: list[Path], read_write_paths: list[Path]) -> None` — 呼び出しプロセス自身を Landlock で FS allowlist に制限する (**不可逆 — プロセス生涯にわたって有効**)。利用不能なら `LandlockUnavailable` を送出する

- [ ] **Step 1: 失敗するテストを書く (fake ctypes 経由の分岐ロジック検証)**

`tests/core/test_landlock.py` を新規作成する:

```python
"""Landlock ctypes wrapper (プラン8, 設計書 §4.6)。

fake ctypes.CDLL によるロジック検証 (このプロセス自身は制限しない —
テストプロセス全体が二度と /tmp 等に触れなくなると以後のテストが全滅
する) と、実 Landlock を使う統合テスト (別プロセスで実施) を分離する。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import LandlockUnavailable, is_available, restrict_to


def test_is_available_false_on_non_x86_64(monkeypatch):
    import agentic_fx.core.landlock as landlock_mod
    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "aarch64")
    assert is_available() is False


def test_restrict_to_raises_when_create_ruleset_fails(monkeypatch, tmp_path):
    import agentic_fx.core.landlock as landlock_mod

    class FakeLibc:
        """I5 対応: `syscall`/`prctl` を通常メソッド (`def ...(self, ...)`)
        として定義すると、`libc.syscall` はアクセスするたびにバインド
        メソッドオブジェクトを新規生成し、Python のバインドメソッドは
        任意属性の代入を許さない (`__dict__` を持たない) — 実装コードが
        行う `libc.syscall.restype = ctypes.c_long` がここで
        `AttributeError` になり、テスト対象コードに到達する前にテスト
        自体が壊れる (レビュー I5)。属性代入を許す関数オブジェクトを
        インスタンス属性として直接持たせることで、実 `ctypes` の関数
        ポインタオブジェクトと同じ「`.restype` を保持できる callable」
        という性質を fake でも再現する。
        """
        def __init__(self) -> None:
            self.syscall = lambda *args: -1  # landlock_create_ruleset 失敗
            self.prctl = lambda *args: 0

    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(landlock_mod.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(landlock_mod.ctypes, "get_errno", lambda: 38)  # ENOSYS
    with pytest.raises(LandlockUnavailable):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])


# --- 2026-08-08 指揮者が着手前に追加した失敗分岐ピン ----------------------
# 旧稿は `restrict_to` の 4 つの失敗分岐 (create_ruleset / add_rule / prctl /
# restrict_self) のうち **1 つしかテストしていなかった**。残り 3 つと
# `is_available` の ABI 問い合わせ失敗、および `finally` での fd close は
# 無防備で、削除しても全件 green のままになる (プラン 8 Task 7 で同型の穴が
# 26 件中 16 件生存した実測を踏まえた予防)。


class _ScriptedLibc:
    """`syscall` の戻り値を呼び出し順に台本化した fake。

    `syscall`/`prctl` は **必ずインスタンス属性の関数オブジェクト**にする
    (I5 — クラスメソッドだと実装側の `libc.syscall.restype = ...` が
    バインドメソッドへの属性代入になり `AttributeError` で落ちる)。
    """

    def __init__(self, *, syscall_results, prctl_result=0):
        self._results = list(syscall_results)
        self.syscall_numbers: list[int] = []
        self.prctl_args: list[tuple] = []

        def syscall(*args):
            n = args[0]
            self.syscall_numbers.append(getattr(n, "value", n))
            return self._results.pop(0) if self._results else 0

        def prctl(*args):
            self.prctl_args.append(args)
            return prctl_result

        self.syscall = syscall
        self.prctl = prctl


def _install(monkeypatch, libc):
    """`platform.machine`/`ctypes.CDLL`/`os.close` を差し替えて、閉じられた
    fd の一覧を返す。

    `os.close` を差し替えるのは必須 — 台本上の `ruleset_fd` は実在しない
    番号 (4242) であり、実 `os.close` を通すと `OSError(EBADF)` が
    `finally` の中で送出されて **本来送出されるはずの `LandlockUnavailable`
    を置き換えてしまう**。あわせて「`finally` で確実に閉じている」ことの
    ピンにもなる。
    """
    import agentic_fx.core.landlock as landlock_mod

    closed: list[int] = []
    real_close = os.close

    def fake_close(fd):
        closed.append(fd)
        if fd < 1000:   # os.open で得た実 fd だけ本当に閉じる
            real_close(fd)

    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(landlock_mod.ctypes, "CDLL", lambda *a, **k: libc)
    monkeypatch.setattr(landlock_mod.ctypes, "get_errno", lambda: 1)
    monkeypatch.setattr(landlock_mod.os, "close", fake_close)
    return closed


def test_is_available_false_when_abi_query_fails(monkeypatch):
    """カーネルが Landlock 非対応 (ENOSYS) なら False。x86_64 判定だけを
    見ていると、この分岐は削除しても検出できない。"""
    _install(monkeypatch, _ScriptedLibc(syscall_results=[-1]))
    assert is_available() is False


def test_restrict_to_raises_when_add_rule_fails(monkeypatch, tmp_path):
    libc = _ScriptedLibc(syscall_results=[8, 4242, -1])  # abi, ruleset_fd, add_rule
    closed = _install(monkeypatch, libc)
    with pytest.raises(LandlockUnavailable, match="landlock_add_rule"):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
    assert 4242 in closed          # ruleset_fd を finally で閉じている
    assert len(closed) == 2        # parent_fd も閉じている (fd リークなし)


def test_restrict_to_raises_when_no_new_privs_fails(monkeypatch, tmp_path):
    """`prctl(PR_SET_NO_NEW_PRIVS)` は `landlock_restrict_self` の前提条件。
    失敗を無視すると後段が EPERM になる (実測で確認済み — Step 7 参照)。"""
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0], prctl_result=-1)
    closed = _install(monkeypatch, libc)
    with pytest.raises(LandlockUnavailable, match="NO_NEW_PRIVS"):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
    assert 4242 in closed


def test_restrict_to_raises_when_restrict_self_fails(monkeypatch, tmp_path):
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, -1])
    closed = _install(monkeypatch, libc)
    with pytest.raises(LandlockUnavailable, match="landlock_restrict_self"):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
    assert 4242 in closed


def test_restrict_to_issues_syscalls_in_required_order(monkeypatch, tmp_path):
    """syscall の**順序**を pin する。`prctl(NO_NEW_PRIVS)` が
    `landlock_restrict_self` より後になるとカーネルが EPERM を返す
    (実測済み) — 順序は正しさの一部であって偶然ではない。"""
    libc = _ScriptedLibc(syscall_results=[8, 4242, 0, 0, 0])
    _install(monkeypatch, libc)
    ro = tmp_path / "ro"; ro.mkdir()
    rw = tmp_path / "rw"; rw.mkdir()
    restrict_to(read_only_paths=[ro], read_write_paths=[rw])

    # 444(abi) → 444(create) → 445(add ro) → 445(add rw) → 446(restrict)
    assert libc.syscall_numbers == [444, 444, 445, 445, 446]
    assert len(libc.prctl_args) == 1
    assert libc.prctl_args[0][0] == 38   # PR_SET_NO_NEW_PRIVS
```

(`is_available` 自体を fake する統合はここでは避け、`restrict_to` 内部で `is_available()` の判定に使う `platform.machine`/`ctypes.CDLL` を個別に monkeypatch する — `is_available` の実装が `ctypes.CDLL(None)` を直接呼ぶため、`restrict_to` からの呼び出しも同じ fake を経由する設計にする。**I5 対応の `FakeLibc` 実装に注意** — `syscall`/`prctl` はクラスメソッド (`def`) ではなく `__init__` 内でインスタンス属性として代入する関数オブジェクトにすること。クラスメソッドのままだと `libc.syscall.restype = ...` (実装コード, Step 3) がバインドメソッドの属性代入で `AttributeError` になり、Step 4 の PASS が成立しない。)

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_landlock.py -q
```

Expected: FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `landlock.py` を実装**

```python
"""Landlock (Linux kernel 5.13+, ABI v1) による FS 自己制限 — ctypes による
syscall 直叩き (プラン8, 設計書 §4.6)。improve worker profile (Task 18) が
`data/` への到達不能を OS レベルで強制するために使う。

**x86_64 Linux 専用** — landlock_* syscall 番号はアーキテクチャ依存であり、
本モジュールは x86_64 の番号のみをハードコードする。他アーキテクチャでは
`is_available()` が無条件 False を返す (fail closed — improve worker の
起動を拒否する)。

実機検証済み (writing-plans, x86_64 / kernel 7.0.0-28-generic):
`landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` が
ABI バージョン 8 を返す / `landlock_add_rule` で O_PATH ディレクトリ fd に
対する読み取り専用ルールを追加できる / `prctl(PR_SET_NO_NEW_PRIVS, 1)` →
`landlock_restrict_self` の順で自己制限後、許可ディレクトリ外への
アクセスが `PermissionError` になることを実測確認済み。
"""
from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path

# x86_64 の landlock syscall 番号 (Linux 5.13+)。
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12

# ABI v1 の全 handled_access_fs (ruleset attr に必須 — 「この ruleset が
# 判定対象とするアクセス種別」の宣言。0x1FFF)。
_ABI_V1_HANDLED_ACCESS_FS = (
    _ACCESS_FS_EXECUTE | _ACCESS_FS_WRITE_FILE | _ACCESS_FS_READ_FILE |
    _ACCESS_FS_READ_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE |
    _ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_REG |
    _ACCESS_FS_MAKE_SOCK | _ACCESS_FS_MAKE_FIFO | _ACCESS_FS_MAKE_BLOCK |
    _ACCESS_FS_MAKE_SYM
)

_READ_ONLY_ACCESS = _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR
_READ_WRITE_ACCESS = (
    _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR | _ACCESS_FS_WRITE_FILE |
    _ACCESS_FS_MAKE_REG | _ACCESS_FS_REMOVE_FILE)

_PR_SET_NO_NEW_PRIVS = 38


class LandlockUnavailable(Exception):
    """カーネル非対応・非対応アーキテクチャ・syscall 失敗の単一表現。"""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]


def is_available() -> bool:
    """Landlock ABI バージョンを問い合わせる。x86_64 以外は無条件 False。"""
    if platform.machine() != "x86_64":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), None,
        ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    return version >= 1


def restrict_to(*, read_only_paths: list[Path],
                read_write_paths: list[Path]) -> None:
    """呼び出しプロセスを Landlock で FS allowlist に制限する
    (**不可逆 — プロセス生涯にわたって有効**、以後の子プロセスにも継承
    される)。利用不能なら `LandlockUnavailable`。
    """
    if not is_available():
        raise LandlockUnavailable(
            "Landlock is not available (non-x86_64, or kernel ABI < 1)")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    ruleset_attr = _RulesetAttr(handled_access_fs=_ABI_V1_HANDLED_ACCESS_FS)
    ruleset_fd = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(ruleset_attr),
        ctypes.c_size_t(ctypes.sizeof(ruleset_attr)), ctypes.c_uint32(0))
    if ruleset_fd < 0:
        errno = ctypes.get_errno()
        raise LandlockUnavailable(
            f"landlock_create_ruleset failed: {os.strerror(errno)}")

    try:
        for path, access in (
                *((p, _READ_ONLY_ACCESS) for p in read_only_paths),
                *((p, _READ_WRITE_ACCESS) for p in read_write_paths)):
            parent_fd = os.open(str(path), os.O_PATH | os.O_DIRECTORY)
            try:
                rule_attr = _PathBeneathAttr(allowed_access=access,
                                             parent_fd=parent_fd)
                rc = libc.syscall(
                    ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset_fd),
                    ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                    ctypes.byref(rule_attr), ctypes.c_uint32(0))
                if rc != 0:
                    errno = ctypes.get_errno()
                    raise LandlockUnavailable(
                        f"landlock_add_rule failed for {path}: "
                        f"{os.strerror(errno)}")
            finally:
                os.close(parent_fd)

        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            errno = ctypes.get_errno()
            raise LandlockUnavailable(
                f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(errno)}")

        rc = libc.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
                          ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
        if rc != 0:
            errno = ctypes.get_errno()
            raise LandlockUnavailable(
                f"landlock_restrict_self failed: {os.strerror(errno)}")
    finally:
        os.close(ruleset_fd)
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_landlock.py -q
```

Expected: PASS。

- [ ] **Step 5: 実 Landlock 統合テスト (別プロセス — E2E 帯)**

このプロセス自身を Landlock で制限すると以後のテストが全滅するため、**別プロセスを spawn してその中で実 `restrict_to` を呼ぶ**。`tests/core/test_landlock.py` に追記:

**2026-08-08 指揮者による拡張**: 旧稿のスクリプトは `read_write_paths=[]` 固定で、**`_READ_WRITE_ACCESS` 定数を一度も通らなかった**。`_ACCESS_FS_MAKE_REG` を削っても、定数ごと `_READ_ONLY_ACCESS` に差し替えても全件 green のままになる — **Task 18 が依存する書き込み側の権限境界が丸ごと無防備**だった (Task 7 の「追加L」と同型)。1 つの子プロセスで 3 つの防御をまとめて検証する形に拡張する。

```python
_REAL_LANDLOCK_SCRIPT = textwrap.dedent("""
    import sys
    from pathlib import Path
    from agentic_fx.core.landlock import restrict_to

    ro_dir = Path(sys.argv[1])       # read_only_paths
    rw_dir = Path(sys.argv[2])       # read_write_paths
    blocked_dir = Path(sys.argv[3])  # どちらにも入れない

    restrict_to(read_only_paths=[ro_dir], read_write_paths=[rw_dir])

    # (1) read_only パスは読める
    assert sorted(p.name for p in ro_dir.iterdir()) == ["x.txt"], "ro not readable"

    # (2) read_only パスへは **書けない** (旧稿が見ていなかった防御)
    try:
        (ro_dir / "new.txt").write_text("x")
        print("FAIL: write succeeded on a read-only path")
        sys.exit(1)
    except PermissionError:
        pass

    # (3) read_write パスへは書ける (_READ_WRITE_ACCESS の唯一のピン)
    try:
        (rw_dir / "new.txt").write_text("x")
    except PermissionError:
        print("FAIL: write was denied on a read-write path")
        sys.exit(1)

    # (4) 列挙していないパスは読めない
    try:
        list(blocked_dir.iterdir())
        print("FAIL: blocked_dir was readable")
        sys.exit(1)
    except PermissionError:
        pass

    print("OK")
    sys.exit(0)
""")


def test_real_landlock_enforces_read_only_read_write_and_blocked(tmp_path):
    """実 Landlock (別プロセス) — カーネルが対応していなければ skip する。

    **skip したかどうかを呼び出し側で必ず確認すること** (Step 5 の実行ログ)。
    この 1 本が実カーネルの強制を触る唯一のテストであり、静かに skip される
    と偽の green になる。本環境 (x86_64 / kernel 7.0.0-29-generic / ABI 8)
    では実行されることを指揮者が実測確認済み。
    """
    from agentic_fx.core.landlock import is_available
    if not is_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "x.txt").write_text("ok")
    rw = tmp_path / "rw"
    rw.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _REAL_LANDLOCK_SCRIPT,
         str(ro), str(rw), str(blocked)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
```

```bash
uv run pytest tests/core/test_landlock.py -q
```

Expected: PASS (実環境で Landlock 利用可能なら実際に制限を検証、不能なら skip)。

- [ ] **Step 6: 全体 green**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```

Expected: 全件 PASS。**Step 5 の実 Landlock テストが `skip` ではなく実際に走ったことを `-rs` で確認すること** (本環境では走る — 静かな skip は、実カーネルの強制を触る唯一のテストが無効化されたまま緑になる形になる)。

- [ ] **Step 7: 変異テスト**

**2026-08-08 指揮者が着手前に実測して書き換えた** (旧稿は「直接的な red 化は難しい」「fake テストの限界」として 2 件を推論で諦めていたが、**どちらもスタンドアロンスクリプトで実測したところ検出可能だった**。Task 7 で「無効な変異」をそのまま出荷しかけた反省を踏まえ、推論ではなく実測で確定させる)。

**実測の前提** (指揮者が本環境で確認済み。実装者は再実測不要):
- `prctl(PR_SET_NO_NEW_PRIVS)` を**呼ばない**と、直後の `landlock_restrict_self` は **`rc != 0` を返す** (カーネルの前提条件)
- `landlock_restrict_self` の**呼び出しごと削除**すると、プロセスは**まったく制限されない** (許可外ディレクトリが読める)

変異リスト:

1. `restrict_to` の `libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)` **呼び出しを削除** → 直後の `landlock_restrict_self` が失敗し `LandlockUnavailable` が送出される → 子プロセスが非ゼロ終了 → `test_real_landlock_enforces_read_only_read_write_and_blocked` が **red** (実測確認済み)
2. `landlock_restrict_self` の **syscall 呼び出しごと削除** (`rc = 0` に置換) → 制限が一切掛からず `blocked_dir` が読める → 同テストが **red** (実測確認済み)
3. `restrict_to` の `if rc != 0:` (`landlock_restrict_self` の結果検査) の**削除単独**は、正常経路では `rc == 0` なので**等価変異** (equivalent mutant — 原理的に検出不能)。**変異 1 と組み合わせたときだけ**「失敗しているのに成功として返る」危険な形になる。**1 と 3 を同時に注入して red になること**を確認する (単独での生存は正しい挙動なので生存扱いにしない)
4. `is_available` の `return version >= 1` を `return False` に固定 → `restrict_to` が常に `LandlockUnavailable` → 上記実プロセステストが red
5. `is_available` の `return version >= 1` を `return True` に固定 → `test_is_available_false_on_non_x86_64` / `test_is_available_false_when_abi_query_fails` が red
6. `restrict_to` の `_READ_WRITE_ACCESS` を `_READ_ONLY_ACCESS` に差し替える → 実プロセステストの (3) が red (**旧稿のテストでは検出できなかった**)
7. `_READ_WRITE_ACCESS` から `_ACCESS_FS_MAKE_REG` を外す → 同上 (新規ファイル作成が拒否される)
8. `_READ_ONLY_ACCESS` に `_ACCESS_FS_WRITE_FILE` を足す → 実プロセステストの (2) が red (read-only パスへ書けてしまう)
9. `restrict_to` の `finally: os.close(ruleset_fd)` を削除 → `test_restrict_to_raises_when_add_rule_fails` 等の `assert 4242 in closed` が red
10. `_ABI_V1_HANDLED_ACCESS_FS` から `_ACCESS_FS_READ_DIR` を外す → その種別が ruleset の判定対象から外れ、`blocked_dir` の列挙が通ってしまう → 実プロセステストの (4) が red
11. (I5) Step 1 の `FakeLibc.__init__` を元の `def syscall(self, *args): ...` (クラスメソッド) 形式に戻す → `test_restrict_to_raises_when_create_ruleset_fails` が `AttributeError: 'method' object has no attribute 'restype'` で red (`pytest.raises(LandlockUnavailable)` の外側で例外が飛ぶため。実装コード側の `.restype` 代入は正しい仕様なので `landlock.py` は変更しない)
12. syscall 番号 `_SYS_LANDLOCK_ADD_RULE` を 445 → 446 に入れ替える → `test_restrict_to_issues_syscalls_in_required_order` が red

**変異リストは下限**。実装者は「この task が守ろうとしている防御」ごとに自分で 1 件ずつ確かめ、リストに無い変異を追加したら報告すること。**生存した変異は必ず報告する** (等価変異と判断した場合はその根拠も添える)。

- [ ] **Step 8: Commit**

```bash
git add src/agentic_fx/core/landlock.py tests/core/test_landlock.py
git commit -m "$(cat <<'EOF'
feat: Landlock ctypes wrapper (x86_64 syscall 直叩き、実機検証済み)

設計書 §4.6。improve worker profile (Task 18) の FS 自己制限に使う
独立モジュール。syscall 番号・構造体レイアウトは本環境で実測検証済み。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 9: worker 基盤 (5) — Rag のスレッド安全化 (内部 lock + close())

設計書 §4.4「RAG RPC と Rag のスレッド安全化」(codex I-8) を実装する。現行 `Rag` (`store/rag.py`) にはロックも `close()` も無く、scheduler tick の news 書込・reflection 書込・(Task 10 以降の) worker RPC 読取が同一インスタンスを共有することになる — chromadb のスレッド安全性は保証に頼れないため、全公開メソッドを内部 lock で直列化する。

**実機確認済み**: `chromadb.PersistentClient` (本プロジェクト pin バージョン 1.5.9) には `close()` メソッドが実在する (`dir(client)` で確認済み) — 「無ければ参照破棄のみで可」という設計書の想定より単純に実装できる。

**Files:**
- Modify: `src/agentic_fx/store/rag.py` (全体 — lock 追加 + `close()` 新設)
- Test: `tests/store/test_rag_lock.py` (新規)

**Interfaces:**
- Produces:
  - `rag.RagUnavailable(Exception)` — lock を `lock_timeout_sec` 以内に取得できなかった場合の単一表現。**呼び出し側の責務**: news collector / reflection 書込 / RPC dispatcher (Task 10) はこれを「RAG 一時不可」として fail soft (該当機能 skip) で受ける — `Rag` 自身は health ラッチへの記録を行わない (health ラッチは App 層の概念 — Task 19)
  - `Rag.__init__(self, data_dir: Path, embedding_function=None, *, lock_timeout_sec: float = 10.0)` — `lock_timeout_sec` 新設 kwarg (既定 10.0 秒。`build_app` は `settings.worker.rpc_timeout_sec` を渡す — RPC dispatcher がハングでリークして lock を保持し続けた場合でも、news collector 等の他経路が無期限にブロックしないための波及止め。設計書 §4.4)
  - `Rag.close() -> None` — `self._client.close()` を lock 保護下で呼ぶ。lock 取得に失敗すれば `RagUnavailable` を送出する (App.close (Task 19) が「使用中は close しない」所有権ルールに従ってこれを catch し、close をスキップして記録する)
  - 全既存公開メソッド (`add_news`/`search_news`/`count_news`/`cleanup_news`/`add_reflection`/`search_reflections`) のシグネチャ・戻り値・例外契約は不変。内部で lock を取るようになる点のみが変更

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_rag_lock.py` を新規作成:

```python
"""Rag のスレッド安全化 (プラン8, 設計書 §4.4 codex I-8)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.store.rag import Rag, RagUnavailable


def _fake_embedding(texts):
    return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


def test_search_news_raises_rag_unavailable_when_lock_held(tmp_path):
    """他スレッドが lock を保持し続けている間、search_news は
    lock_timeout_sec 経過で RagUnavailable を送出する (無期限ブロックしない)。"""
    rag = Rag(tmp_path / "rag", embedding_function=_fake_embedding,
             lock_timeout_sec=0.2)

    release = threading.Event()

    def hold_lock():
        with rag._lock:  # 内部実装への直接アクセス (テスト専用の白箱検証)
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    time.sleep(0.05)  # hold_lock が確実に lock を取ってから測る
    try:
        with pytest.raises(RagUnavailable):
            rag.search_news("query")
    finally:
        release.set()
        t.join(timeout=2.0)


def test_close_calls_chromadb_client_close(tmp_path, monkeypatch):
    rag = Rag(tmp_path / "rag", embedding_function=_fake_embedding)
    calls: list[str] = []
    monkeypatch.setattr(rag._client, "close", lambda: calls.append("closed"))
    rag.close()
    assert calls == ["closed"]


def test_normal_operations_still_serialize_correctly(tmp_path):
    """lock 導入後も既存の add_news/search_news/cleanup_news の挙動が
    不変であることの回帰確認 (既存 tests/store/test_rag.py が主だが、
    ここでも 1 本だけ通しで確認する)。"""
    from datetime import datetime, timezone

    rag = Rag(tmp_path / "rag", embedding_function=_fake_embedding)
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    added = rag.add_news(
        [{"url": "https://x/1", "title": "t", "body": "b",
         "source_name": "s", "published": None}], now)
    assert added == 1
    assert rag.count_news() == 1
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_rag_lock.py -q
```

Expected: FAIL (`AttributeError: 'Rag' object has no attribute '_lock'` / `ImportError: cannot import name 'RagUnavailable'`)。

- [ ] **Step 3: `rag.py` を実装**

`src/agentic_fx/store/rag.py` の import 節に `import threading` と `from contextlib import contextmanager` を追加する。`_require_utc` の直前に追加:

```python
class RagUnavailable(Exception):
    """RAG 内部 lock を `lock_timeout_sec` 以内に取得できなかった (一時的な
    不可 — 呼び出し側は fail soft (該当機能 skip) で受けること。設計書
    §4.4 codex I2-2)。"""
```

`Rag.__init__` を以下に置き換える:

```python
    def __init__(self, data_dir: Path, embedding_function=None, *,
                lock_timeout_sec: float = 10.0) -> None:
        # (既存の ef 構築・probe 呼び出しは無変更 — このコメント位置に
        # 既存の 4 行 (ef = ... / ef([...]) を維持する)
        ef = (embedding_function if embedding_function is not None
              else DefaultEmbeddingFunction())
        ef(["__afx_rag_init_probe__"])

        self._client = chromadb.PersistentClient(path=str(data_dir))
        self._news = self._client.get_or_create_collection(
            "news", embedding_function=ef)
        self._refl = self._client.get_or_create_collection(
            "reflections", embedding_function=ef)
        # プラン 8 worker 基盤 (codex I-8): 全公開メソッドを直列化する
        # 内部 lock。低頻度・短時間の呼び出しなので直列化のコストは
        # 無視できる。
        self._lock = threading.Lock()
        self._lock_timeout_sec = lock_timeout_sec

    @contextmanager
    def _locked(self):
        acquired = self._lock.acquire(timeout=self._lock_timeout_sec)
        if not acquired:
            raise RagUnavailable(
                f"RAG lock not acquired within {self._lock_timeout_sec}s "
                "(a caller is holding it — fail soft: skip this operation)")
        try:
            yield
        finally:
            self._lock.release()

    def close(self) -> None:
        """chromadb PersistentClient.close() を lock 保護下で呼ぶ
        (実機確認済み — 1.5.9 に実在する)。lock 取得に失敗すれば
        `RagUnavailable` (呼び出し側の App.close は「使用中は close しない」
        所有権ルールに従ってこれを catch し、close をスキップして記録
        すること — 設計書 §5)。
        """
        with self._locked():
            self._client.close()
```

既存の 6 公開メソッド (`add_news`/`search_news`/`count_news`/`cleanup_news`/`add_reflection`/`search_reflections`) を、それぞれ**メソッド本体全体を `with self._locked():` で包む**形に変更する。例 (`search_news`):

```python
    def search_news(self, query: str, n: int = 5) -> list[dict]:
        """意味検索で news を引く。body は元記事の本文 (embedding 用に
        連結した "title\\nbody" テキストではない — メタデータに別途持つ)。
        """
        with self._locked():
            count = self.count_news()
            if count == 0:
                return []
            res = self._news.query(query_texts=[query], n_results=min(n, count))
            return [{"url": m["url"], "title": m["title"], "body": m["body"],
                     "source_name": m["source_name"]}
                    for m in res["metadatas"][0]]
```

**注意 (実装者向け — 再入デッドロック回避)**: `search_news` は内部で `self.count_news()` を呼ぶ。`count_news` 自身も `with self._locked():` で包むと、`threading.Lock` は非再入 (再帰取得不可) のため**自スレッドからの二重取得でデッドロックする**。実装時は以下のいずれかを選ぶこと (writing-plans 推奨: 案 A):
- **案 A (推奨)**: `count_news` の**内部実装**を lock 無しの private ヘルパー `_count_news_unlocked(self) -> int` に切り出し、公開 `count_news` は `with self._locked(): return self._count_news_unlocked()` に、`search_news` 内部の呼び出しは `self._count_news_unlocked()` (lock を取らない版) に変更する
- 案 B: `threading.Lock` を `threading.RLock` に変更する (再入を許す) — ただし `_locked()` の timeout 意味論が「累積」ではなく「その時点でのブロック待ち」になる点に注意 (RLock の `acquire(timeout=...)` は同一スレッドからの再取得は即座に成功するため実害はない)

いずれを選んでも Step 4 のテストが通ることを確認すること。**本プランは案 A を正とする** (`RLock` は「取得元スレッドを問わない直列化」という lock の意図をぼかすため — 実装は明示的に `_count_news_unlocked` を切り出すこと)。

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_rag_lock.py -q
uv run pytest tests/store/ -k rag -q
uv run pytest -q
```

Expected: 全件 PASS (既存 `tests/store/test_rag.py` 相当のテストが lock 導入後も無変更で通ること)。

- [ ] **Step 5: `build_app` に `lock_timeout_sec` を配線**

`src/agentic_fx/service.py:375` (**2026-08-08 指揮者が実測して訂正 — 旧稿の `:304` は 71 行のドリフト**) の `rag = Rag(root / "data" / "rag", embedding_function=embedding_fn)` を以下に変更する (`settings.worker.rpc_timeout_sec` を配線 — 実際に使い始めるのは Task 10 の RPC dispatcher からだが、値の由来をここで確定しておく):

```python
    rag = Rag(root / "data" / "rag", embedding_function=embedding_fn,
             lock_timeout_sec=settings.worker.rpc_timeout_sec)
```

- [ ] **Step 6: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 7: 変異テスト**

1. `_locked()` の `if not acquired:` を削除 (timeout 検出を無効化) → `test_search_news_raises_rag_unavailable_when_lock_held` が red
2. `close()` から `with self._locked():` を外す → `test_close_calls_chromadb_client_close` 自体は red にならない可能性があるため (lock が無くても close は呼ばれる)、代わりに「lock 保持中に `close()` を呼ぶと `RagUnavailable` になる」ケースを追加確認する変異テストとして、`test_search_news_raises_rag_unavailable_when_lock_held` と同型のテストを `close()` に対しても書き、この変異で red になることを確認する (実装者は Step 1 のテストに `test_close_raises_rag_unavailable_when_lock_held` を追加してから本変異を実施すること)

**2026-08-08 指揮者の着手前照合による追加 (波及の pin — Task 7 の教訓「配線そのものを検証する」)**:

本 task は `RagUnavailable` という**新しい例外を既存の 5 つの呼び出し経路に注入する**。Interfaces 節は「呼び出し側の責務: news collector / reflection 書込 / RPC dispatcher は fail soft で受ける」と書いているが、**それを検証する step が無い**。指揮者が呼び出し側 5 箇所を実査した結果:

| 呼び出し元 | 現状 |
|---|---|
| `reflection_cycle.py:139` `add_reflection` | `try/except Exception` で捕捉・次回リトライ ✓ |
| `news_collector.py:107` `add_news` | ソース単位の `try/except Exception` の**内側** ✓ |
| **`news_collector.py:113` `cleanup_news`** | **try の外側** — `collect()` を貫通する |
| `news_tools.py:12` / `reflection_tools.py:15` | 素通し (LLM にツールエラーとして見える。許容) |

`cleanup_news` の貫通は `scheduler._run_data_hook` (fail-open の隔離ハンドラ、`scheduler.py:266`) が捕捉するため**実害はない**。ただしこれは**確かめて初めて分かる性質**であり、隔離が外れれば「RAG の lock timeout が資金保護経路 (tick) を落とす」ことになる。

3. **(波及の pin) `tests/store/test_rag_lock.py` に以下のテストを追加すること**: `Rag` の lock を保持したまま `Scheduler.tick()` を回し (既存 `tests/core/test_scheduler*.py` の fixture を流用)、**`RagUnavailable` が tick を貫通せず、`_process_exits` (資金保護) まで到達すること**を確認する。そのうえで `scheduler._run_data_hook` の `try/except` を外す変異を注入 → このテストが red になることを確認する
   - **狙い**: 「`RagUnavailable` は fail soft で受けられる」という Interfaces 節の主張を、宣言ではなく**実行で固定**する。単体の lock timeout テストはこの性質を守らない

**レビュー 1 周目の反映 (2026-08-08。codex 主査 I3/M1 + sonnet 副査 I3/M1。両者が独立に同じ 2 件の生存変異を実測)**:

1. **[両者一致・実測] `_locked()` を `acquire(timeout=0)` (try-lock 化) する変異が全 13 本を素通りして生存**していた。既存テストは「deadline より長く保持すれば `RagUnavailable`」しか見ておらず、**timeout が「実際の待ち」であることの正方向の契約が無かった**。単一 lock で全操作を直列化する設計では**短い競合は正常系**なので、0 秒化は可用性を実質的に変える → `test_lock_timeout_waits_and_succeeds_when_released_in_time` を追加
   - **指揮者の 1 回目の修正は race で素通りした** — 「holder が 0.05 秒保持 → main が呼ぶ」順序だと main が呼ぶ前に holder が解放してしまう。**決定論的なハンドシェイク** (holder が `holding` → main が `about_to_call` を立ててから呼ぶ → holder はそれを待ってから保持し続けて解放) に組み直して初めて red になった
2. **[両者一致・実測] `service.py` の `lock_timeout_sec=15.0` 直書き変異が生存**していた。配線テストが**既定値 (15.0) と一致することしか見ていなかった**ため、「設定を読まない」変異を検出できなかった → 設定 YAML に**既定値とも `Rag` 既定 (10.0) とも異なる 0.37** を書いてから `build_app` する形に変更
3. **[codex Minor] lock 保持テストが `sleep(0.05)` に依存**し holder の取得完了を同期していなかった (高負荷 CI で flaky になる) → 3 箇所すべて `Event` ハンドシェイクへ

最終: 変異 **16 件で生存 0**。`uv run pytest -q` = **1530 passed, 1 deselected** (ベースライン 1516 + 14)。

**[両者一致 Important — Task 9 では解かず申し送り] `RagUnavailable` の fail-soft 契約が正典・各 task 間で一意になっていない**:

- 設計書 §4.4 は lock timeout を「RAG 一時不可として fail soft (**該当機能 skip + latched health 記録**)」と定めるが、**Task 9 は health 記録を App 層 (Task 19) へ送り、Task 19 の health callback は Task 10 の `queue.Empty` (RPC 呼び出し自体のリーク) だけを記録する**。結果として **`RagUnavailable` が期限内に正常返却されたケースは、どの task でも latch されない**
- **tool 経路が内部エラー文字列を LLM の transcript に混入させる**: `ToolRegistry.execute` が例外文字列を `{"error": ...}` にし、`LocalRunner._sink` 経由で transcript に入って次 turn へ渡る。文言は `RAG lock not acquired within 0.2s (a caller is holding it — fail soft: skip this operation)` — `safe_error_text` は URL/秘密は落とすが**文言は落とさない**。モデルが一時的な lock 競合を「RAG の恒久故障」と解釈する余地がある。プラン Task 9 本文はこの素通しを「許容」と裁定していたが**検証していなかった**
- **各経路で記録の質が不揃い**: `news_collector` の `add_news` は**系統的な RAG 障害を単一ソースの失敗として activity に誤帰属**、`cleanup_news` は scheduler の generic system activity、`reflection_cycle` は技術ログのみで activity に出ない
- **[sonnet Minor] Task 12 が hooks を決定論ブロックの後段へ再編するまでの過渡状態では**、`NewsCollector.collect()` の per-source + cleanup が**それぞれ独立に `lock_timeout_sec` 待つ**ため、RAG が詰まると tick 完了 (= 資金保護 `_process_exits`) が最大 `(ソース数+1) × lock_timeout_sec` 遅延しうる
- **対応方針 (Task 10 / 12 / 19 とスペック改訂で一意化する)**: ①`RagUnavailable` を予期された一時不可として各境界で明示 catch する ②read tool は**空結果または安定した非内部的な unavailable 表現に正規化**し、raw な lock 所有状況を transcript に入れない ③全経路から共通の App-level health recorder へ到達させ、**`RagUnavailable` の正常返却も RPC leak とは別イベントとして latch** する ④実 `NewsCollector`/`reflection`/2 tool/Task 10 dispatcher の各経路に例外注入テストを置き「skip・後続継続・health 記録・raw error 非露出」を pin する

- [ ] **Step 8: Commit**

```bash
git add src/agentic_fx/store/rag.py src/agentic_fx/service.py tests/store/test_rag_lock.py
git commit -m "$(cat <<'EOF'
feat: Rag のスレッド安全化 (内部 lock + close())

設計書 §4.4 (codex I-8)。scheduler tick の news/reflection 書込と
worker RPC 読取 (Task 10) が同一 Rag インスタンスを共有しても安全にする。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 10: WorkerRunner 本体 (spawn + preemption + RPC dispatcher + transcript 上限)

設計書 §3.1「WorkerRunner seam」、§4.1(spawn/handshake)、§4.3(reader/dispatcher スレッド構成)、§4.7(preemption エスカレーション)、§4.8(finalize は呼び出し元、close 順序)、§8(新設定キーの実消費) を実装する。`AgentRunner` の 3 つ目の実装として `build_app` のデフォルト runner を差し替える — この task 単体で「Mission が子プロセスで動く」ところまで完成させる (`core_lock` 粒度の再設計は Task 13/15/16)。

**Files:**
- Create: `src/agentic_fx/runners/worker_runner.py`
- Modify: `src/agentic_fx/service.py:409-414`(`build_app` のデフォルト runner を `WorkerRunner` に)`,642-643`(shutdown の close 判定を `hasattr` ベースに一般化) — **2026-08-08 指揮者が実測して訂正。旧稿の `339-343`/`573-577` はそれぞれ約 70 行のドリフト**
- Modify: `tests/test_service_app.py:453-459` (`isinstance(app.runner, LocalRunner)` → `WorkerRunner` に更新)
- Test: `tests/runners/test_worker_runner.py` (新規 — FakeChild によるインプロセス単体テスト + 実 subprocess の最小 E2E 1 本)

**Interfaces:**
- Produces:
  - `WorkerRunner(AgentRunner)` — `__init__(self, *, root: Path, settings: Settings, clock: Clock, rag: Rag, worker_profile: str = "trade", on_rpc_leak: Callable[[], None] | None = None) -> None`。`run(self, mission: Mission) -> MissionResult` (継承契約どおり)。`close(self) -> None` (no-op — 各 Mission が自分の子プロセスを spawn/reap するため永続資源を持たない。`service.py` の shutdown 判定を `LocalRunner` 専用の isinstance から `hasattr(app.runner, "close")` へ一般化するための対称メソッド)
  - `on_rpc_leak` — RPC dispatcher が `rpc_timeout_sec` を超えてハングした RAG 呼び出しを検出したときに呼ばれる (設計書 §4.3 codex I3-1 — 「累積許容しない」の通知経路。呼び出し元 = App/health ラッチ配線は Task 19)
  - `worker_runner._mission_worker_env(worker_profile: str) -> dict[str, str]` (IM-3/P8-03 対応) — `plugin.sandbox._build_env()` の最小 env をベースに、`worker_profile == "trade"` のときだけ `TWELVEDATA_API_KEY`/`MT5_BRIDGE_API_KEY` を親環境から明示 allowlist で追加する。`improve` profile は資格情報を一切渡さない

**設計判断 (writing-plans)**:
- **cwd**: `tempfile.TemporaryDirectory(prefix="afx-mission-")` で Mission ごとに新規の空ディレクトリを作り `cwd=` に渡す。Mission 終了後 (成功・失敗・timeout いずれでも) `with` ブロックで自動削除する
- **env** (IM-3/P8-03 対応、裁定書 F-9): mission worker 専用の env builder `_mission_worker_env(worker_profile)` を新設する。`plugin/sandbox.py:_build_env()` (`AFX_*` 等の秘密を継承しない最小 env) をベースにしつつ、`plugin/sandbox.py` の env をそのまま流用すると `TWELVEDATA_API_KEY`/`MT5_BRIDGE_API_KEY` (データプロバイダ資格情報) が子に渡らず、MT5/TwelveData を有効化した構成で trade worker の市場データ取得が認証失敗する (レビュー IM-3/P8-03)。trade profile のときだけ、この 2 キーを親環境から明示 allowlist で追加する。`AFX_*`/`ANTHROPIC_*` は引き続き除外。**improve profile では資格情報も渡さない** (裁定書 F-9 — 遮断維持)。network poison (`_poison_network_modules`) はどちらの profile でも呼ばない (mission worker は信頼済みハーネスコードでネットワークが必要、という設計書 §4.5 の方針)
- **handshake の `now`** (レビュー反映 2 回目 R2-CX-01 — Task 7 の `_build_clock()` 実時計方針 (裁定書 I4) に統一): `clock.now()` を 1 回取得して handshake フレームに含めるが、これは**監査・記録用の情報**に留める (transcript/activity に「親がこの Mission を起動した時点の時刻」として残すだけ)。子内の鮮度判定・データ取得境界 (`PriceProvider` の quote/bar 鮮度検証、`signal_tools` の検索境界等) は `FixedClock(now)` ではなく Task 7 の `_build_clock()` が返す実時計 (`SystemClock()`) を使う — handshake の `now` から `FixedClock` を組み立てて Mission 全体の時計として配線してはならない。旧稿にあった「1 回の判断内で時刻を固定し親子で判断材料を矛盾させない」という意図は、鮮度判定を fail-open にする副作用 (停止した Mission が古いデータ/signal を新鮮と誤判定し続ける) の方が上回るため採用しない — 判断材料の一貫性より鮮度の fail-closed を優先する (裁定書 I4)。
- **RPC dispatcher のリーク検出** (FC-1 対応 — 裁定書の方針どおり `concurrent.futures.ThreadPoolExecutor` は使わない): RAG 呼び出しごとに使い捨ての `threading.Thread(daemon=True)` を spawn し、結果を `queue.Queue(maxsize=1)` 経由で受け取る。`queue.Queue.get(timeout=rpc_timeout_sec)` で打ち切る。`queue.Empty` はリークとして扱い `on_rpc_leak()` を呼ぶ (worker スレッドは回収できないまま残る — 設計書 §4.3 codex I3-1 の裁定どおり「別プロセス化はしない・累積許容もしない」は維持)。**`ThreadPoolExecutor` を使わない理由 (レビュー FC-1)**: `ThreadPoolExecutor` のワーカースレッドは Python の atexit ハンドラ (`concurrent.futures.thread._python_exit`) に登録され、`shutdown(wait=False)` を呼んでもリークして回収不能になったワーカースレッドの終了をインタプリタ終了時に待ち続け、プロセスの `sys.exit()`/非ゼロ終了そのものを無期限にブロックする (実測で再現確認済み — `ThreadPoolExecutor` に未 set の `Event.wait()` を submit して `shutdown(wait=False)` 後にプロセスを終えようとすると `timeout 3s` で rc=124)。**`daemon=True` の素の `threading.Thread` は interpreter 終了時に join されない** ため、リークしたスレッドが存在してもプロセスは正常に (非ゼロ) 終了できる。RAG 呼び出しごとに新規スレッドを spawn する設計変更により「1 回リークした後の後続 RPC も自然に timeout する」保証は `ThreadPoolExecutor(max_workers=1)` の飽和ではなく `Rag` 自身の内部 lock (Task 9, `lock_timeout_sec`) の飽和で担保される (後続呼び出しはリーク中の呼び出しが保持する lock の解放を待って `RagUnavailable`/timeout になる) — 追加のガード条件は不要

- [ ] **Step 1: 失敗するテストを書く (FakeChild によるインプロセス単体テスト)**

`tests/runners/test_worker_runner.py` を新規作成する。実 subprocess の代わりに `subprocess.Popen` を monkeypatch し、パイプの片側をテストコードが操作する FakeChild ヘルパーで検証する:

```python
"""WorkerRunner (プラン8, 設計書 §3.1/§4.1/§4.3/§4.7)。

FakeChild: 実 subprocess を spawn せず、os.pipe() で親子間パイプを模倣し、
別スレッドで「子のふり」をして handshake/ready/event/result を書く。
実 subprocess spawn の最小 E2E は本ファイル末尾の
test_real_subprocess_completes_mission_end_to_end のみ (他は全て
FakeChild 経由)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.mission_protocol import write_frame
from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from datetime import datetime, timezone

SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _root(tmp_path):
    import shutil
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example",
        root / "config" / "settings.yaml")
    init_db(connect(root / "data" / "agentic.db"))
    return root


def _rag(tmp_path):
    return Rag(tmp_path / "rag", embedding_function=lambda t: [[0.0] * 4 for _ in t])


def _mission():
    return Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                   max_turns=1, timeout_sec=5.0)


def test_mission_worker_env_includes_data_provider_credentials_for_trade(monkeypatch):
    """IM-3/P8-03 対応: trade profile は TWELVEDATA_API_KEY/
    MT5_BRIDGE_API_KEY を明示 allowlist で子に渡す。AFX_* は継承しない。"""
    from agentic_fx.runners.worker_runner import _mission_worker_env

    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-secret")
    monkeypatch.setenv("MT5_BRIDGE_API_KEY", "mt5-secret")
    monkeypatch.setenv("AFX_SOMETHING", "must-not-leak")

    env = _mission_worker_env("trade")

    assert env["TWELVEDATA_API_KEY"] == "td-secret"
    assert env["MT5_BRIDGE_API_KEY"] == "mt5-secret"
    assert "AFX_SOMETHING" not in env


def test_mission_worker_env_excludes_credentials_for_improve(monkeypatch):
    """improve profile では資格情報も渡さない (裁定書 F-9 — 遮断維持)。"""
    from agentic_fx.runners.worker_runner import _mission_worker_env

    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-secret")
    monkeypatch.setenv("MT5_BRIDGE_API_KEY", "mt5-secret")

    env = _mission_worker_env("improve")

    assert "TWELVEDATA_API_KEY" not in env
    assert "MT5_BRIDGE_API_KEY" not in env


def test_mission_worker_env_omits_unset_credentials(monkeypatch):
    """親環境にキーが無ければそもそも env に含めない (空文字を渡さない)。"""
    from agentic_fx.runners.worker_runner import _mission_worker_env

    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    monkeypatch.delenv("MT5_BRIDGE_API_KEY", raising=False)

    env = _mission_worker_env("trade")

    assert "TWELVEDATA_API_KEY" not in env
    assert "MT5_BRIDGE_API_KEY" not in env


class _FakeChildScript:
    """テストが `subprocess.Popen` の代わりに使う擬似子プロセス。実
    プロセスは起動しない — 親側 (WorkerRunner) が書く stdin をこのスレッドが
    読み、指定したフレーム列を stdout 側パイプへ書き込む。"""

    def __init__(self, frames_after_handshake, *, delay_before_result=0.0):
        self.frames = frames_after_handshake
        self.delay = delay_before_result
        self.pid = 999999  # WorkerRunner が expected_parent_pid の照合対象に
                            # しない値 (FakeChild は本物の os.getppid() 照合を
                            # 経由しない — インプロセステストのため)
        self.returncode = None
        self._killed = threading.Event()

    def poll(self):
        return None if not self._killed.is_set() else -9

    def wait(self, timeout=None):
        self._killed.wait(timeout)
        return -9 if self._killed.is_set() else None

    def kill_signal_received(self):
        self._killed.set()
```

(このフィクスチャ設計は WorkerRunner の実装詳細 — `subprocess.Popen` の戻り値をどこまで模倣する必要があるか — に強く依存する。**実装者は Step 3 で WorkerRunner を書いた後、その実装が `proc.stdin`/`proc.stdout`/`proc.poll()`/`os.killpg(proc.pid, ...)` のどれを呼ぶかを確定させ、それに合わせて `_FakeChildScript` を拡張すること**。`os.killpg` は実 pid 空間に対する OS 呼び出しであり FakeChild の偽 pid には効かないため、**WorkerRunner の kill 経路を `self._kill_fn: Callable[[subprocess.Popen], None]` としてテスト注入可能な形にする** (既定は `lambda proc: os.killpg(proc.pid, signal.SIGKILL)`) — これによりテストは `kill_fn` を差し替えて「kill が呼ばれたこと」を観測する。以下のテストコードはこの前提で書く:

```python
def test_worker_runner_completes_mission_via_pipes(tmp_path, monkeypatch):
    """正常系: handshake→ready→event×2→result の往復を検証する。"""
    r, w = os.pipe()   # 子→親 (子が書く側 = w、親が読む側 = r)
    r2, w2 = os.pipe()  # 親→子 (親が書く側 = w2、子が読む側 = r2)

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "event", "seq": 2,
                                "message": {"role": "user", "content": "hi"}})
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {"x": 1}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()  # 自プロセス pid — expected_parent_pid 照合は
                            # FakeChild 側では検証しない (実 subprocess の
                            # 責務。テストは配線だけを見る)
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            # I9 対応: `finally` 節の `_ensure_dead` → `_kill` は
            # `poll() is None` の間 `proc.wait(timeout=5)` を必ず呼ぶ。
            # `wait` を持たない FakeProc だと `AttributeError` になり
            # Step 1 のテストが (正常系であっても) finally で必ず壊れる
            # (レビュー I9)。
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    # I9 対応: FakeProc.pid はテストプロセス自身の pid (上記コメント参照)。
    # `_kill`/`_escalate_kill` は `os.killpg(proc.pid, signal.SIGKILL)` を
    # 呼ぶため、`os.killpg` を monkeypatch せずに実行すると
    # **テストプロセス自身が SIGKILL される** (レビュー I9)。fake で
    # 呼び出しを記録するだけにする。
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(wr_mod.os, "killpg",
                        lambda pid, sig: killpg_calls.append((pid, sig)))

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert result.output == {"x": 1}
    assert {"role": "user", "content": "hi"} in result.transcript
```

（この 1 本は WorkerRunner の I/O 配線を通しで確認する骨格テストであり、実装が `subprocess.Popen` をどう呼ぶか (`stdin`/`stdout` を `os.fdopen` した実ファイルオブジェクトとして扱うか) に依存する。**実装者は Step 3 を書いた後、この骨格に合わせてテストの `FakeProc` を調整すること** — 特に `stdin.write`/`stdout.readline` が実際に呼ばれる形と一致させる。**I9 対応: `FakeProc.wait()` と `os.killpg` の monkeypatch は必須**。`FakeProc.pid = os.getpid()` かつ `finally` 節が `poll() is None` なら無条件に `_kill(proc)` (→ `os.killpg` → `proc.wait(timeout=5)`) を呼ぶ実装であるため、この 2 つを欠かすとテスト実行そのものが `AttributeError` で壊れるか、最悪 `os.killpg` が実際に呼ばれてテストプロセス自身が SIGKILL される。）

追加で以下のケースをカバーする独立したテストを書く (骨格は上と同じ `FakeProc`/`subprocess.Popen` monkeypatch パターンを流用し、子スレッドの応答内容だけを変える。**I9 対応: `FakeProc` に `wait(self, timeout=None)` を必ず実装し、`os.killpg` を monkeypatch すること** — `FakeProc.pid` を `os.getpid()` にする場合は特に必須 (killpg を fake しないとテストプロセス自身に `SIGKILL` が飛ぶ)。`_FakeChildScript` (既存, 999999 という実在しない pid を使う) を流用する場合も `wait()` は既に実装済みだが `os.killpg` が `ProcessLookupError` を投げて `except` で握られることに変わりはないため、明示的に呼び出し引数を検証したいテスト (`test_worker_runner_mission_timeout_escalates_sigterm_then_sigkill` 等) は `os.killpg` を monkeypatch して呼出し引数を記録する方式に統一すること):

1. `test_worker_runner_startup_timeout_kills_child` — `ready` を送らないまま `worker_startup_timeout_sec` (settings を `model_copy` で短縮して注入) を超過させ、`kill_fn` が呼ばれ `status == "failed"` になることを確認
2. `test_worker_runner_mission_timeout_escalates_sigterm_then_sigkill` — `result` を送らないまま `mission.timeout_sec + worker_grace_sec` を超過させ、SIGTERM 相当の呼び出し (`terminate_fn`) → `worker_terminate_grace_sec` 経過後に `kill_fn` が呼ばれることを確認。`status == "timeout"`
3. `test_worker_runner_child_eof_before_result_is_failed` — `ready` 送出後、`result` を送らずに子スレッドがパイプを閉じる (EOF) → `status == "failed"`
   - **Task 7 が親に課した前提 (2026-08-08、Task 7 レビュー 3 周目 codex が明文化)**: 子は **transport 失敗 (`write`/`flush` の例外) 時に structured error frame を送れないため `os._exit(1)` で黙って死ぬ**。親が観測できる確実な事実は **EOF / プロセス終了だけ**。したがって Task 10 は以下を守ること —
     - `result` を正常受信する**前**の EOF は、終了コードが未取得でも、部分 JSON 行が残っていても **`failed`** とする
     - EOF を「正常完了」「空 transcript」「startup timeout」として扱わない。**`ready` 前なら起動失敗、`ready` 後なら Mission 失敗**として区別する
     - 部分行の JSON decode error / `ProtocolError` も `failed` とし、**同じ子への送信再開やフレーム再要求をしない**
     - `finally` で子を reap し dispatcher を停止・join する。子の `os._exit` に cleanup frame を期待しない
4. `test_worker_runner_protocol_violation_is_failed` — `event` フレームの `seq` を逆行させて送る → `status == "failed"` (fail closed — `mission_protocol.ProtocolError` を検出)
5. `test_worker_runner_transcript_truncates_at_cap` — `transcript_max_bytes` を小さく (例: 200 バイト) 設定し、大量の `event` を送る → `result.transcript` に truncate marker が 1 件だけ含まれ、以後の `event` が積まれていないことを確認
6. `test_worker_runner_rag_rpc_dispatches_to_rag_and_responds` — `tool_rpc` (`name="search_news"`) を送り、fake `Rag.search_news` が呼ばれて `tool_rpc_result` が子側パイプ (`r2`) から読めることを確認
7. `test_worker_runner_rag_rpc_leak_calls_on_rpc_leak` — fake `Rag.search_news` を `rpc_timeout_sec` より長くブロックする関数に差し替え、`on_rpc_leak` コールバックが呼ばれることを確認 (mission 自体は `result` フレームが届けば completed のまま終わってよい — リーク検出とミッション結果は独立)
8. (IM-7) `test_worker_runner_finally_joins_dispatcher_before_closing_stdin` — 下記の完全なコードを参照 (`threading.Thread` を monkeypatch して生成されたスレッドを捕捉し、`run()` 復帰時点で `afx-worker-dispatcher` という名前のスレッドが `is_alive() is False` であることを確認する — `finally` 節が `proc.stdin.close()` の前に dispatcher を join し終えていることの直接的な回帰ピン)
9. (FC-1) `test_worker_runner_leaked_rag_rpc_returns_promptly_with_daemon_thread` — `rag.search_news` を永久にブロックする関数に差し替え、`run()` が `rpc_timeout_sec` 程度の有限時間で復帰すること (ハングしないこと) と、リークした `afx-rag-rpc` スレッドが `daemon=True` のまま `threading.enumerate()` に残っていること (= `ThreadPoolExecutor` の atexit join 対象になっていないこと) を確認する。下記の完全なコードを参照
10. (FC-1) `test_leaked_daemon_thread_does_not_block_process_exit` — **実測統合テスト (裁定書必須項目)**。実 subprocess を spawn し、`afx-rag-rpc` と同じパターン (`threading.Thread(daemon=True, target=<永久にブロックする関数>)`) でスレッドをリークさせた直後に `sys.exit(7)` する小スクリプトを実行し、`subprocess.run(..., timeout=5)` が `TimeoutExpired` を送出せず `returncode == 7` で戻ることを確認する。下記の完全なコードを参照 (`ThreadPoolExecutor` 版であれば同じスクリプトが `timeout 5s` で `TimeoutExpired` になっていたことをコメントで明記する — 実際に手元の `uv run python -c "..."` でこの対比を実行し、修正前の再現も確認済み)

```python
def test_worker_runner_finally_joins_dispatcher_before_closing_stdin(tmp_path, monkeypatch):
    """IM-7 対応: finally 節は proc.stdin を close する前に dispatcher
    スレッドの join を完了させる (dispatcher が stdin_lock を保持して
    tool_rpc_result を書込中に close が競合するレースを防ぐ — 設計書
    §4.3「writer は 2 時点で排他」)。run() から戻った時点で dispatcher
    スレッドが確実に終了している (is_alive() is False)ことを直接検証する。
    """
    import agentic_fx.runners.worker_runner as wr_mod

    created_threads: list[threading.Thread] = []
    orig_thread_cls = wr_mod.threading.Thread

    def spying_thread(*args, **kwargs):
        t = orig_thread_cls(*args, **kwargs)
        created_threads.append(t)
        return t

    monkeypatch.setattr(wr_mod.threading, "Thread", spying_thread)

    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q", "n": 5}})
        json.loads(child_in.readline())  # tool_rpc_result
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    rag = _rag(tmp_path)
    rag.search_news = lambda query, n=5: []  # 即応答 (遅延自体は対象外)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock, rag=rag)
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    dispatcher_threads = [th for th in created_threads
                          if th.name == "afx-worker-dispatcher"]
    assert dispatcher_threads, "dispatcher thread was not created"
    assert all(not th.is_alive() for th in dispatcher_threads)
```

```python
def test_worker_runner_leaked_rag_rpc_returns_promptly_with_daemon_thread(
        tmp_path, monkeypatch):
    """FC-1 対応: RAG RPC がリークしても run() は rpc_timeout_sec 程度の
    有限時間で復帰し (ThreadPoolExecutor 版は atexit join でハングし
    得た)、生成されたワーカースレッドは daemon=True のまま残る (=
    ThreadPoolExecutor の atexit join 対象になっていない)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "tool_rpc", "seq": 2, "rpc_id": "1",
                                "name": "search_news",
                                "args": {"query": "q", "n": 5}})
        # tool_rpc_result (timeout 応答) を読んでから result を送る
        json.loads(child_in.readline())
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()
    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    rag = _rag(tmp_path)
    rag.search_news = lambda query, n=5: threading.Event().wait()  # 永久リーク
    settings = SETTINGS.model_copy(update={
        "worker": SETTINGS.worker.model_copy(update={"rpc_timeout_sec": 0.2})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock, rag=rag)
    t.start()
    started = time.monotonic()
    result = runner.run(_mission())
    elapsed = time.monotonic() - started
    t.join(timeout=2.0)

    assert result.status == "completed"
    # rpc_timeout_sec=0.2 に対して十分な余裕 (dispatcher.join の上限は
    # rpc_timeout_sec + 5.0 — ThreadPoolExecutor 版なら atexit 待ちで
    # 無期限にハングし得た経路)。
    assert elapsed < 5.0
    leaked = [th for th in threading.enumerate() if th.name == "afx-rag-rpc"]
    assert leaked, "expected a leaked afx-rag-rpc thread to still be present"
    assert all(th.daemon for th in leaked)
```

```python
def test_leaked_daemon_thread_does_not_block_process_exit():
    """FC-1 対応の実測統合テスト (裁定書必須項目)。実 subprocess で
    afx-rag-rpc と同じパターン (`threading.Thread(daemon=True,
    target=<永久ブロック>)`) を作った直後に `sys.exit(7)` するスクリプト
    を実行し、プロセスが実際に有限時間で終了できることを確認する。

    対比 (手元で実測確認済み — `ThreadPoolExecutor` 版の再現):
    `concurrent.futures.ThreadPoolExecutor(max_workers=1)` に `submit`
    した `threading.Event().wait()` を `shutdown(wait=False)` した後に
    `sys.exit(...)` する同型スクリプトは、atexit ハンドラ
    (`concurrent.futures.thread._python_exit`) がワーカースレッドの
    終了を待つため `subprocess.run(..., timeout=5)` が
    `TimeoutExpired` になる (rc=124 相当)。本テストは `daemon=True` の
    素の `threading.Thread` に置き換えたことで、このハングが解消される
    ことを実行結果で固定する。
    """
    script = (
        "import threading, sys\n"
        "threading.Thread(target=lambda: threading.Event().wait(), "
        "daemon=True, name='afx-rag-rpc').start()\n"
        "sys.exit(7)\n")
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 7
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/runners/test_worker_runner.py -q
```

Expected: 全件 FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `worker_runner.py` を実装**

```python
"""WorkerRunner — Mission 実行を preemption 可能な使い捨て子プロセスへ
隔離する AgentRunner 実装 (プラン8, 設計書 §3.1/§4.1/§4.3/§4.7)。

親→子の spawn/handshake、子→親の event/tool_rpc/result 受信、
timeout エスカレーション (SIGTERM→grace→SIGKILL) をすべてこのクラスの
`run()` 呼び出し 1 回に閉じる。preemption 後も `MissionResult(status=
"timeout")` を返し、既存の 4 終端契約 (completed/failed/timeout/
max_turns) に in-band で乗る (呼び出し元は他の AgentRunner 実装と
区別なく扱える)。
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from agentic_fx.core.contracts import Clock
from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, read_frame, write_frame,
)
from agentic_fx.plugin.sandbox import _build_env
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.store.rag import Rag

import logging

_log = logging.getLogger("agentic_fx.worker_runner")

# IM-3/P8-03 対応 (裁定書 F-9): trade profile の子だけに渡すデータ
# プロバイダ資格情報の明示 allowlist。
_DATA_PROVIDER_ENV_ALLOWLIST = ("TWELVEDATA_API_KEY", "MT5_BRIDGE_API_KEY")


def _mission_worker_env(worker_profile: str) -> dict[str, str]:
    """mission worker 専用の env builder (IM-3/P8-03 対応、裁定書 F-9)。

    `plugin/sandbox.py:_build_env()` (`PATH`/`PYTHONPATH`/
    `PYTHONSAFEPATH`/`_SINGLE_THREAD_ENV` のみの最小 env、`AFX_*` 等は
    継承しない) をそのまま mission worker にも流用すると、
    `price_provider.py`/`sources.py` が読む `TWELVEDATA_API_KEY`/
    `MT5_BRIDGE_API_KEY` が子に渡らず、MT5/TwelveData を有効化した
    構成で trade worker の市場データ取得ツールが恒常的に認証失敗する
    (レビュー IM-3/P8-03)。`worker_profile == "trade"` のときだけ、
    この 2 キーを親プロセスの環境から明示 allowlist で追加する。
    `AFX_*`/`ANTHROPIC_*` 等は引き続き除外 (継承しない)。improve
    profile では資格情報も渡さない (裁定書 F-9 — 遮断維持)。
    """
    env = _build_env()
    if worker_profile == "trade":
        for key in _DATA_PROVIDER_ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
    return env


class WorkerRunner(AgentRunner):
    def __init__(self, *, root: Path, settings, clock: Clock, rag: Rag,
                worker_profile: str = "trade",
                on_rpc_leak: Callable[[], None] | None = None) -> None:
        self._root = root
        self._settings = settings
        self._clock = clock
        self._rag = rag
        self._worker_profile = worker_profile
        self._on_rpc_leak = on_rpc_leak

    def close(self) -> None:
        """no-op — 各 Mission が自分の子プロセスを spawn/reap するため
        永続資源を持たない (LocalRunner との対称性のための空実装)。"""

    def run(self, mission: Mission) -> MissionResult:
        w = self._settings.worker
        with tempfile.TemporaryDirectory(prefix="afx-mission-") as workdir:
            proc = subprocess.Popen(
                [sys.executable, "-m", "agentic_fx.mission_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=workdir,
                env=_mission_worker_env(self._worker_profile),
                start_new_session=True)
            return self._run_with_child(proc, mission, w)

    # ---- 内部 -------------------------------------------------------

    def _run_with_child(self, proc, mission: Mission, w) -> MissionResult:
        stdin_lock = threading.Lock()
        in_seq = SeqTracker()  # 子→親方向の受信検証
        ready_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        done_queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)
        dispatch_queue: "queue.Queue[dict]" = queue.Queue()
        transcript: list[dict] = []
        state = {"bytes": 0, "truncated": False}

        def handle_event(frame: dict) -> None:
            if state["truncated"]:
                return
            msg = frame["message"]
            size = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            if state["bytes"] + size > w.transcript_max_bytes:
                transcript.append({"role": "system",
                                   "content": "[transcript truncated: "
                                              "exceeded transcript_max_bytes]"})
                state["truncated"] = True
                return
            transcript.append(msg)
            state["bytes"] += size

        def reader_loop() -> None:
            try:
                while True:
                    frame = read_frame(proc.stdout)
                    if frame is None:
                        done_queue.put(("eof", None))
                        return
                    try:
                        in_seq.check(frame.get("seq"))
                    except ProtocolError as e:
                        done_queue.put(("protocol_error", str(e)))
                        return
                    ftype = frame.get("type")
                    if ftype == "ready":
                        ready_queue.put(frame)
                    elif ftype == "event":
                        handle_event(frame)
                    elif ftype == "tool_rpc":
                        dispatch_queue.put(frame)
                    elif ftype == "result":
                        done_queue.put(("result", frame))
                        return
                    else:
                        done_queue.put(
                            ("protocol_error", f"unknown frame type {ftype!r}"))
                        return
            except Exception as e:  # noqa: BLE001 — reader は死なせない代わりに報告する
                done_queue.put(("error", str(e)))

        def dispatcher_loop() -> None:
            out_seq_holder = {"n": 0}
            while True:
                frame = dispatch_queue.get()
                if frame is None:  # shutdown 合図
                    return
                # FC-1 対応: concurrent.futures.ThreadPoolExecutor は使わない
                # (atexit ハンドラがワーカースレッドの終了を待つため、
                # リークしたまま残ると sys.exit()/非ゼロ終了そのものを
                # 無期限にブロックする — レビュー FC-1、実測で再現確認済み)。
                # RAG 呼び出しごとに使い捨ての daemon スレッドを spawn し、
                # 結果は queue.Queue 経由で受け取る。daemon スレッドは
                # interpreter 終了時に join されないため、リークしても
                # プロセスは正常に (非ゼロ) 終了できる。
                result_queue: "queue.Queue[tuple[bool, object]]" = (
                    queue.Queue(maxsize=1))

                def _rpc_worker(name=frame["name"], args=frame["args"]) -> None:
                    try:
                        result_queue.put((True, self._call_rag(name, args)))
                    except Exception as e:  # noqa: BLE001 — 子へ tool error として返す
                        result_queue.put((False, str(e)))

                threading.Thread(target=_rpc_worker, daemon=True,
                                 name="afx-rag-rpc").start()
                try:
                    ok, payload = result_queue.get(timeout=w.rpc_timeout_sec)
                    response = ({"ok": True, "result": payload} if ok
                               else {"ok": False, "error": payload})
                except queue.Empty:
                    _log.error("RAG RPC leaked past rpc_timeout_sec=%s "
                              "(name=%s) — this thread will never be "
                              "reclaimed but will not block process exit "
                              "(daemon thread — 设計書 §4.3 codex I3-1, "
                              "レビュー FC-1)",
                              w.rpc_timeout_sec, frame["name"])
                    if self._on_rpc_leak is not None:
                        try:
                            self._on_rpc_leak()
                        except Exception:  # noqa: BLE001
                            _log.exception("on_rpc_leak callback failed")
                    response = {"ok": False, "error": "rag rpc timed out"}
                out_seq_holder["n"] += 1
                try:
                    with stdin_lock:
                        write_frame(proc.stdin, {
                            "type": "tool_rpc_result",
                            "seq": out_seq_holder["n"] + 1,  # handshake=1 済み
                            "rpc_id": frame["rpc_id"], **response})
                except (BrokenPipeError, OSError):
                    return  # 子が既に死んでいる — 応答不能

        reader = threading.Thread(target=reader_loop, daemon=True,
                                  name="afx-worker-reader")
        dispatcher = threading.Thread(target=dispatcher_loop, daemon=True,
                                      name="afx-worker-dispatcher")
        reader.start()
        dispatcher.start()

        now = self._clock.now()
        handshake = {
            "type": "handshake", "seq": 1,
            "expected_parent_pid": os.getpid(),
            "db_path": (str(self._root / "data" / "agentic.db")
                       if self._worker_profile == "trade" else None),
            "plugins_dir": (str(self._root / "plugins")
                            if self._worker_profile == "trade" else None),
            "settings": self._settings.model_dump(),
            "mission": {"prompt": mission.prompt, "tools": mission.tools,
                       "output_schema": mission.output_schema,
                       "max_turns": mission.max_turns,
                       "timeout_sec": mission.timeout_sec},
            "worker_profile": self._worker_profile,
            "now": now.isoformat(),
        }
        with stdin_lock:
            write_frame(proc.stdin, handshake)

        status = "failed"
        output = None
        try:
            try:
                ready = ready_queue.get(timeout=w.worker_startup_timeout_sec)
                if not ready.get("ok", False):
                    status = "failed"
                    return MissionResult(status, None, transcript)
            except queue.Empty:
                self._escalate_kill(proc, w)
                return MissionResult("failed", None, transcript)

            deadline_budget = mission.timeout_sec + w.worker_grace_sec
            try:
                kind, payload = done_queue.get(timeout=deadline_budget)
            except queue.Empty:
                self._escalate_kill(proc, w)
                return MissionResult("timeout", None, transcript)

            if kind == "result":
                status = payload["status"]
                output = payload.get("output")
            else:  # eof / protocol_error / error — すべて failed に正規化
                status = "failed"
                output = None
            return MissionResult(status, output, transcript)
        finally:
            dispatch_queue.put(None)
            self._ensure_dead(proc, w)
            reader.join(timeout=5.0)
            # IM-7 対応: dispatcher は `with stdin_lock: write_frame(proc.stdin,
            # ...)` を実行し得る (tool_rpc_result 応答の送出中)。ここで
            # stdin_lock の外から proc.stdin.close() すると、dispatcher が
            # まさに書込中の瞬間と競合し得る (設計書 §4.3「writer は
            # 2 時点で排他」違反 — レビュー IM-7)。dispatcher を先に join
            # する (dispatcher 自身は `result_queue.get(timeout=
            # w.rpc_timeout_sec)` で必ず打ち切られるため — FC-1 対応の
            # daemon スレッドがリークしても、dispatcher 自体は有限時間で
            # `dispatch_queue.get()` に戻り、None sentinel を見て return
            # する — 有界待ち)。
            dispatcher.join(timeout=w.rpc_timeout_sec + 5.0)
            with stdin_lock:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.stdout.close()
            except OSError:
                pass

    def _call_rag(self, name: str, args: dict):
        method = getattr(self._rag, name)
        return method(**args)

    def _escalate_kill(self, proc, w) -> None:
        """SIGTERM → `worker_terminate_grace_sec` → SIGKILL (設計書 §4.7)。"""
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + w.worker_terminate_grace_sec
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            self._kill(proc)

    def _ensure_dead(self, proc, w) -> None:
        """finally 節: どの終了経路でも子が生きていれば確実に殺す。
        既に `_escalate_kill` が呼ばれていれば `proc.poll()` は None
        ではないため no-op。"""
        if proc.poll() is None:
            self._kill(proc)

    def _kill(self, proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
```

**実装者への注意**: 上記は骨格実装であり、Step 1 のテストを実際に流しながら以下を調整すること — ①`_FakeChildScript`/`FakeProc` が `os.killpg(proc.pid, ...)` を呼べない (FakeProc.pid が実プロセスでない) ケースをどう扱うか (テストでは `monkeypatch.setattr(wr_mod.os, "killpg", ...)` で `os.killpg` 自体を差し替え、呼び出し引数を記録する形にする) ②`out_seq_holder["n"] + 1` のオフセット (handshake が seq=1 を消費済みであることの整合) は実装しながら実際のフレーム列で検証すること ③`dispatcher_loop` の `dispatch_queue.put(None)` によるシャットダウン合図が、リーク中 (直近の RAG 呼び出しが `result_queue.get(timeout=w.rpc_timeout_sec)` で打ち切り待ち中) でも `dispatcher_loop` 自身のループ (`dispatch_queue.get()` 待ち) を正しく抜けられることを確認すること (dispatcher スレッド自体はリークしない — リークするのは FC-1 対応で spawn する使い捨て `afx-rag-rpc` daemon スレッドのみで、`ThreadPoolExecutor` は使わない)。

- [ ] **Step 4: テスト実行して PASS を確認 (段階的に)**

```bash
uv run pytest tests/runners/test_worker_runner.py -q
```

実装と FakeProc/monkeypatch の整合を取りながら 1 本ずつ green にすること。全件 PASS になるまで Step 3/Step 1 を往復してよい (TDD の red→green サイクルをこの 2 step 間で回す)。

- [ ] **Step 5: 実 subprocess の最小 E2E (1 本)**

`tests/runners/test_worker_runner.py` の末尾に、実際に `python -m agentic_fx.mission_worker` を spawn する 1 本を追加する (llama-swap への実 HTTP は発生させない — `Mission.tools=[]`・`output_schema` が単純な固定 JSON を要求する形にはできない (LocalRunner は実際に LLM へ問い合わせるため) ので、**このテストは「起動〜ready〜(接続先が存在せず timeout)〜preemption による終了」までを実証する** — 実 LLM 応答が無い環境でも決定論的に検証できる範囲に留める):

```python
def test_real_subprocess_starts_and_reports_ready_then_times_out(tmp_path):
    """実 subprocess を spawn する最小 E2E。llama-swap への接続先が存在
    しない (base_url を到達不能な値に上書き) ため LocalRunner 側は
    timeout する — 子プロセスの起動・handshake・ready 応答・preemption
    による終了までが実際に動くことを実証する (詳細なハング注入・
    kill 検証は Task 20 の E2E に譲る)。
    """
    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "llama_swap": SETTINGS.llama_swap.model_copy(
            update={"base_url": "http://127.0.0.1:1", "timeout_sec": 2}),
        "worker": SETTINGS.worker.model_copy(
            update={"worker_grace_sec": 2.0, "worker_terminate_grace_sec": 2.0,
                    "worker_startup_timeout_sec": 15.0})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path))
    mission = Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=2.0)
    result = runner.run(mission)
    assert result.status in ("timeout", "failed")
```

```bash
uv run pytest tests/runners/test_worker_runner.py -q -k real_subprocess
```

Expected: PASS (数秒で完了する — `worker_grace_sec`/`timeout_sec` を短く設定しているため)。

- [ ] **Step 6: `build_app` のデフォルト runner を差し替え**

`src/agentic_fx/service.py` の import 節に `from agentic_fx.runners.worker_runner import WorkerRunner` を追加する。`build_app` (339-343 行) を以下に置き換える:

```python
    owns_runner = runner is None
    if runner is None:
        runner = WorkerRunner(root=root, settings=settings, clock=clock,
                              rag=rag, worker_profile="trade")
```

`run_service` (573-577 行付近) の shutdown 判定を一般化する:

```python
            if app.owns_runner and hasattr(app.runner, "close"):
                app.runner.close()
```

- [ ] **Step 7: 既存テストの更新**

`tests/test_service_app.py:453-459` の `test_owns_runner_true_when_built_locally` を以下に更新する:

```python
def test_owns_runner_true_when_built_locally(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, clock=FixedClock(NOW))
    assert app.owns_runner is True
    assert isinstance(app.runner, WorkerRunner)
    app.runner.close()  # no-op — 対称性の確認
```

`tests/test_service_app.py` の import 節に `from agentic_fx.runners.worker_runner import WorkerRunner` を追加する (`LocalRunner` の import が他で使われていなければ削除、使われていれば残す — `grep -n "LocalRunner" tests/test_service_app.py` で確認)。

**2026-08-08 指揮者の着手前照合による追記 — 旧稿は更新対象を 1 箇所しか挙げていなかった**:

`grep -n "LocalRunner" tests/test_service_app.py` の実測結果は **3 箇所** (`:13` import / `:458` isinstance / **`:609`**)。3 つ目が問題になる:

```python
def test_run_service_closes_owned_runner_on_graceful_shutdown(tmp_path):
    """F4-②: owns_runner=True 相当 (`LocalRunner` の spec を持つ mock に
    差し替え)。graceful shutdown で close() が 1 回だけ呼ばれること。"""
    mock_runner = MagicMock(spec=LocalRunner)      # ← ここ
```

このテストは「所有する runner が graceful shutdown で close される」ことの唯一の pin である。本 task で `service.py:642` の判定を `isinstance(app.runner, LocalRunner)` から `hasattr(app.runner, "close")` へ**一般化**すると、`spec=LocalRunner` の mock は `close` を持つので**このテストは緑のまま通る** — つまり **`WorkerRunner` が close されることは一切検証されない**状態になる。Task 7〜9 で繰り返し出た「配線を検証していない」パターンそのものである。

**対応 (必須)**: `spec=LocalRunner` を **`spec=WorkerRunner`** に変更する。そのうえで、**`service.py:642` の `hasattr(app.runner, "close")` 判定を削除する変異を注入して、このテストが red になることを確認する** (Step 9 の変異リストに追加すること)。`WorkerRunner.close()` は no-op だが、「所有権があれば close を呼ぶ」という配線自体が防御であり、Task 19 の `App.close` がこの規約を引き継ぐ。

- [ ] **Step 8: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。**注意**: `build_app` を `runner=None` で呼ぶ既存テストが他にもあれば (`grep -rn "build_app(" tests/ | grep -v "runner="`)、それらは今後 `WorkerRunner` 経由になり実サブプロセスを spawn する可能性がある — 該当テストを洗い出し、実行時間が許容範囲か確認する。許容できないほど遅い/不安定なテストがあれば `runner=FakeRunner([...])` を明示的に渡すよう修正すること (テストの意図を変えない範囲で)。

- [ ] **Step 9: 変異テスト**

1. `_escalate_kill` の `os.killpg(proc.pid, signal.SIGTERM)` を削除 (SIGKILL だけ残す) → `test_worker_runner_mission_timeout_escalates_sigterm_then_sigkill` が red (SIGTERM が呼ばれない)
2. `handle_event` の `if state["bytes"] + size > w.transcript_max_bytes:` の閾値判定を削除 → `test_worker_runner_transcript_truncates_at_cap` が red
3. `dispatcher_loop` の `future.result(timeout=w.rpc_timeout_sec)` の `timeout=` を削除 (無期限待ちにする) → `test_worker_runner_rag_rpc_leak_calls_on_rpc_leak` が red (テストがタイムアウトするか `on_rpc_leak` が呼ばれない)
4. `in_seq.check(frame.get("seq"))` の呼び出しを削除 → `test_worker_runner_protocol_violation_is_failed` が red
5. (I9) `_kill` 内の `proc.wait(timeout=5)` 呼び出しを削除 → 直接 red 化するテストは無い (呼ばなくても既存アサーションは通る) ため、代わりに `test_worker_runner_completes_mission_via_pipes` の `FakeProc.wait` から `return -9` を削除し `raise AssertionError("wait must not be called")` に変える変異を行い、**現在の実装が `wait` を呼んでいること自体** を回帰確認する (呼ばれなければ変異前後でテスト結果が変わらず、レビュー時に「_kill が wait を呼ぶ」という設計意図がテストで担保されていないことが分かる — 担保されていなければ Step 3 の docstring 通りの実装になっているか目視で再確認すること)
6. (IM-3/P8-03) `_mission_worker_env` の `if worker_profile == "trade":` ブロックを削除 (資格情報 allowlist を無条件スキップ) → `test_mission_worker_env_includes_data_provider_credentials_for_trade` が red
7. (IM-7) `finally` 節の `dispatcher.join(timeout=w.rpc_timeout_sec + 5.0)` 行を削除 → `test_worker_runner_finally_joins_dispatcher_before_closing_stdin` が red (`dispatcher_threads` の `is_alive()` が `True` のまま残るケースが発生し得る — 環境によりタイミング依存で毎回 red にならない場合は、`dispatcher_loop` 内に `time.sleep(0.05)` を一時挿入してレースを顕在化させ、削除の効果を確認する旨をコメントで明記する)
8. (FC-1) `dispatcher_loop` 内の `threading.Thread(target=_rpc_worker, daemon=True, ...)` を `daemon=False` に改変 → `test_leaked_daemon_thread_does_not_block_process_exit` は同型スクリプトを直接実行するため本改変では red にならない (WorkerRunner 内部の変更だけでは検出できない設計上の限界) — 代わりに `test_worker_runner_leaked_rag_rpc_returns_promptly_with_daemon_thread` の `assert all(th.daemon for th in leaked)` が red になることを確認する

**2026-08-08 指揮者の着手前照合による追加変異**:

- **(配線 pin) `service.py` の shutdown 判定 `if app.owns_runner and hasattr(app.runner, "close"):` を削除する** → `test_run_service_closes_owned_runner_on_graceful_shutdown` が red になることを確認する。**Step 7 で mock の spec を `WorkerRunner` に変えていないと、この変異は生存する** (`spec=LocalRunner` のままだと `hasattr` 判定の一般化が検証されないため)

- [ ] **Step 9b (FC-1): 実 subprocess での対比実測を確認する**

```bash
uv run pytest tests/runners/test_worker_runner.py -q -k leaked_daemon_thread_does_not_block_process_exit
```

Expected: PASS。念のため `ThreadPoolExecutor` 版に戻した場合に red (`TimeoutExpired`) になることを、実装完了後に一度だけ手動で `concurrent.futures.ThreadPoolExecutor` を使った同型スクリプトに差し替えて確認し (恒久的にテストへは残さない — 対比確認のみ)、レビューコメントに実測結果を残す。

- [ ] **Step 10: Commit**

```bash
git add src/agentic_fx/runners/worker_runner.py src/agentic_fx/service.py \
  tests/runners/test_worker_runner.py tests/test_service_app.py
git commit -m "$(cat <<'EOF'
feat: WorkerRunner (使い捨て子プロセスへの Mission 隔離 + preemption + RPC dispatcher)

設計書 §3.1/§4.1/§4.3/§4.7。build_app のデフォルト runner を WorkerRunner
に差し替える (core_lock 粒度の再設計は Task 13/15/16)。RAG RPC dispatcher
は ThreadPoolExecutor をやめ daemon スレッド + Queue に置換し、リーク後も
プロセスが非ゼロ終了できることを実測で固定する (レビュー FC-1)。
finally 節は dispatcher を join してから stdin を close する (レビュー
IM-7)。mission worker 専用の env builder でデータプロバイダ資格情報を
trade profile にのみ明示 allowlist で渡す (レビュー IM-3/P8-03)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 11: `missions.finish` の CAS 化 + 起動時 running→interrupted 回収 (missions+signals 同一トランザクション)

設計書 §4.7「finalize 所有権と二重終端防止」(codex C-4) と §4.8「残留の回収と孤児対策」(codex C-5) を実装する。この task は Task 15/16 (五相再構成) の前提部品 — `missions.finish` の呼び出し元 (`trade_loop.py`/`reflection_cycle.py`) 自体の書き換えは Task 15/16 に譲る (戻り値の型を `None`→`bool` に変えても、既存の呼び出し元は戻り値を使っていないため無変更で動く — 呼び出し元が CAS の戻り値を見て activity 警告を出すよう更新するのは Task 15/16 の五相再構成と同時に行う)。

**Files:**
- Modify: `src/agentic_fx/store/missions.py` (`finish` を CAS 化、`recover_interrupted` 新設)
- Create: `src/agentic_fx/store/instance_lock.py` (FC-2 対応 — 単一インスタンス保証)
- Modify: `src/agentic_fx/service.py:462-466`(**2026-08-08 指揮者が実測して訂正 — 旧稿の `262-266` は約 200 行のドリフトで、無関係な docstring の途中を指していた**。実際の配線先は「起動時 reclaim 1 回」のコメントと `signals.reclaim_expired(conn_core, ...)` の箇所) に起動シーケンスの `recover_interrupted` を追加。FC-2: その**前**にプロセス排他 flock を取得、`App` dataclass (`instance_lock` フィールド追加)、`return App(...)` (`instance_lock=instance_lock` 追加)
- Test: `tests/store/test_missions_cas.py` (新規), `tests/store/test_instance_lock.py` (新規)

**Interfaces:**
- Produces:
  - `missions.finish(conn, mission_id: int, status: str, output: dict | None, transcript: list, now: datetime) -> bool` — **`WHERE id=? AND status='running'` の CAS**。戻り値 `True` = この呼び出しが終端を書いた、`False` = 既に終端済み (影響行数 0、上書きしない)。**呼び出し元は `False` のとき activity 警告を残し、この結果を上書きしないこと** (呼び出し元の更新は Task 15/16)
  - `missions.recover_interrupted(conn, *, now: datetime, max_requeue: int) -> dict` — `status='running'` の missions 行を全件 `'interrupted'` へ終端し、それらを claim していた `claimed` signals を**同一トランザクション**で requeue (上限超過は abandoned) する。戻り値 `{"missions_recovered": int, "signals_requeued": int, "signals_abandoned": int}`。**`'interrupted'` は DB 回収専用の状態値であり `MissionResult.status` の 4 値契約 (`runners/base.py:40`) には現れない** (codex M-1 — `missions` テーブルの `status` 列に CHECK 制約は無いためスキーマ変更不要)
  - `instance_lock.InstanceAlreadyRunning(Exception)` (FC-2 対応、裁定書) — 別プロセスが同じ DB ディレクトリの instance lock を既に保持している場合の単一表現
  - `instance_lock.acquire_instance_lock(db_dir: Path) -> IO` — `db_dir` (`root / "data"`) 直下の lock file に対する `fcntl.flock(LOCK_EX | LOCK_NB)`。取得できなければ `InstanceAlreadyRunning` を送出する (fail closed — 起動を中止する)。戻り値のファイルオブジェクトは **App の全寿命にわたって保持**すること (close/GC されると lock が解放される)。`recover_interrupted` は「稼働中の全 `running` mission」を無条件に対象とするため、二重起動があると先発の稼働中 Mission を後発が誤って `interrupted` 終端し claim 済み signal を横取りしかねない — この排他はそれを防ぐ (裁定書 FC-2)。**解放は Task 19 の `App.close()` で配線する** (本 task では `App` に `instance_lock` フィールドを追加して保持するところまでに留め、Task 19 本文は編集しない)

**2026-08-08 指揮者の着手前照合 (実測。実装者は再確認不要)**:

- `missions.finish` の呼び出し元は **5 箇所** — `loops/trade_loop.py:114,129,282` / `loops/reflection_cycle.py:106` / `backtest/runner.py:244`。**いずれも戻り値を使っていない**ので、`None` → `bool` への変更で既存呼び出し元は無変更で動く (プランの想定どおり)
- `App` dataclass は `service.py:184-207` (20 フィールド)。`instance_lock` は末尾に追加してよい
- 起動シーケンスの実際の並び: `Scheduler(...)` 構築 → **「起動時 reclaim 1 回」コメント + `signals.reclaim_expired(...)`** (462-466) → `Commands` 構築 → `return App(...)` (474-)
- ベースライン: `uv run pytest -q` = **1556 passed, 1 deselected**

- [ ] **Step 1: 失敗するテストを書く (`finish` の CAS 化)**

`tests/store/test_missions_cas.py` を新規作成:

```python
"""missions.finish の CAS 化 + 起動時回収 (プラン8, 設計書 §4.7/§4.8)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store import missions, signals
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    return conn


def test_finish_returns_true_on_first_call(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    assert missions.finish(conn, mid, "completed", {"a": 1}, [], NOW) is True
    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "completed"


def test_finish_returns_false_on_second_call_and_does_not_overwrite(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    assert missions.finish(conn, mid, "completed", {"a": 1}, [], NOW) is True
    assert missions.finish(conn, mid, "failed", None, [], NOW) is False
    row = conn.execute(
        "SELECT status, output_json FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "completed"  # 上書きされていない
    assert row["output_json"] == '{"a": 1}'
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q -k finish
```

Expected: FAIL (`assert None is True`)。

- [ ] **Step 3: `finish` を CAS 化**

`src/agentic_fx/store/missions.py:23-32` の `finish` を以下に置き換える:

```python
def finish(conn: sqlite3.Connection, mission_id: int, status: str,
           output: dict | None, transcript: list, now: datetime) -> bool:
    """missions 行を終端状態へ CAS 更新する (設計書 §4.7 codex C-4)。

    `WHERE status='running'` を満たさない (= 既に終端済み) 場合は影響行数
    0 のまま何もしない — 無条件 UPDATE による「後勝ち上書き」を構造的に
    封鎖する (finalize 所有権は commit 相の finally 一箇所のみ、という
    設計書 §4.7 の不変条件をこの CAS が実装レベルで強制する)。

    戻り値: `True` = この呼び出しが終端を書いた。`False` = 既に終端済み
    だった (二重終端防止) — **呼び出し元はこの場合 activity 警告を残し、
    この結果を上書きしたと誤認しないこと**。
    """
    cur = conn.execute(
        "UPDATE missions SET status=?, output_json=?, transcript_json=?, "
        "finished_at=? WHERE id=? AND status='running'",
        (status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         json.dumps(transcript, ensure_ascii=False),
         now.isoformat(), mission_id))
    conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q -k finish
uv run pytest -q
```

Expected: 全件 PASS (既存の `missions.finish` 呼び出し元は戻り値を使っていないため無変更で動く)。

- [ ] **Step 5: 失敗するテストを書く (`recover_interrupted`)**

`tests/store/test_missions_cas.py` に追加:

```python
def test_recover_interrupted_finalizes_running_missions(tmp_path):
    conn = _conn(tmp_path)
    mid1 = missions.start(conn, "trade", "local", "m", NOW)
    mid2 = missions.start(conn, "trade", "local", "m", NOW)
    missions.finish(conn, mid2, "completed", None, [], NOW)  # 既に終端済み

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["missions_recovered"] == 1
    row1 = conn.execute("SELECT status FROM missions WHERE id=?", (mid1,)).fetchone()
    assert row1["status"] == "interrupted"
    row2 = conn.execute("SELECT status FROM missions WHERE id=?", (mid2,)).fetchone()
    assert row2["status"] == "completed"  # 触られない


def test_recover_interrupted_requeues_claimed_signals_same_transaction(tmp_path):
    """running のまま残った mission が claim していた signal は、
    lease_min の経過を待たず同一トランザクションで requeue される
    (codex C-5 — 分離すると mission は終端済みなのに signal は lease 満了
    まで不可視、という不整合窓が生じる)。"""
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["signals_requeued"] == 1
    row = conn.execute(
        "SELECT status, claimed_by_mission_id FROM signals WHERE id=?",
        (claimed["id"],)).fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None


def test_recover_interrupted_abandons_signal_over_requeue_limit(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    conn.execute("UPDATE signals SET requeue_count=2 WHERE id=?", (claimed["id"],))
    conn.commit()

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["signals_abandoned"] == 1
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (claimed["id"],)).fetchone()
    assert row["status"] == "abandoned"


def test_recover_interrupted_is_atomic_no_partial_state_on_failure(tmp_path):
    """途中で例外が起きても missions/signals どちらも変更されない
    (単一トランザクション — codex C-5)。

    I11 対応: `sqlite3.Connection.execute` は C 拡張型の read-only
    属性であり `monkeypatch.setattr(conn, "execute", ...)` は
    `AttributeError: 'sqlite3.Connection' object attribute 'execute'
    is read-only` で失敗し、テストがそもそも実行できない (レビュー
    I11、実測で確認済み)。DB 側の決定論的な failure point として
    SQLite trigger を使う — `signals` の `status` を `'pending'` に
    更新する UPDATE (`recover_interrupted` の requeue 分岐) だけを
    確実に失敗させ、`recover_interrupted` 自身の `except BaseException:
    conn.rollback(); raise` 経路を実際に通す。
    """
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    conn.execute("""
        CREATE TRIGGER fail_on_signal_requeue
        BEFORE UPDATE OF status ON signals
        WHEN NEW.status = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'simulated failure');
        END;
    """)
    conn.commit()

    with pytest.raises(sqlite3.Error):
        missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "running"  # ロールバック済み (missions 側も巻き戻る)
    sig_row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (claimed["id"],)).fetchone()
    assert sig_row["status"] == "claimed"  # signals 側も巻き戻る
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q -k recover_interrupted
```

Expected: 全件 FAIL (`AttributeError: module 'agentic_fx.store.missions' has no attribute 'recover_interrupted'`)。

- [ ] **Step 7: `recover_interrupted` を実装**

`src/agentic_fx/store/missions.py` に追加 (`finish` の直後):

```python
def recover_interrupted(conn: sqlite3.Connection, *, now: datetime,
                        max_requeue: int) -> dict:
    """起動時回収 (設計書 §4.8 codex C-5): 前回停止時に `running` のまま
    残った missions 行を `'interrupted'` へ終端し、それらを claim して
    いた `claimed` signals を**同一トランザクション**で requeue (上限
    超過は abandoned) する。

    分離すると「mission は終端済みなのに signal は lease 満了 (最大 15
    分) まで不可視」の不整合窓が生じる — signal の `claimed_at` は直近
    (プロセス生存中の claim) であり得るため、通常の lease ベース回収
    (`signals.reclaim_expired`) では長時間拾われない。

    `'interrupted'` は DB 回収専用の状態値であり `MissionResult.status`
    の 4 値契約 (`runners/base.py:40`) には現れない — status 表示・集計
    はこの区別を明記すること (codex M-1)。

    signals の requeue/abandon 判定は `signals.py` の
    `_REQUEUE_STATUS_EXPR`/`_REQUEUE_COUNT_EXPR` と同じ規則 (現在の
    requeue_count が max_requeue 以上なら abandoned、未満なら pending +
    +1) を Python 側で再現する — `signals.reclaim_expired` を呼ぶと
    それ自身が `conn.commit()` するため、missions の更新と同一トランザ
    クションを構成できない (この関数専用に単一トランザクションで完結
    させる必要がある)。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        running_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM missions WHERE status='running'").fetchall()]
        for mid in running_ids:
            conn.execute(
                "UPDATE missions SET status='interrupted', finished_at=? "
                "WHERE id=?", (now.isoformat(), mid))

        signals_requeued = signals_abandoned = 0
        if running_ids:
            placeholders = ",".join("?" * len(running_ids))
            claimed_rows = conn.execute(
                "SELECT id, requeue_count FROM signals WHERE status='claimed' "
                f"AND claimed_by_mission_id IN ({placeholders})",
                running_ids).fetchall()
            for row in claimed_rows:
                if row["requeue_count"] >= max_requeue:
                    conn.execute(
                        "UPDATE signals SET status='abandoned', "
                        "claimed_by_mission_id=NULL, claimed_at=NULL "
                        "WHERE id=?", (row["id"],))
                    signals_abandoned += 1
                else:
                    conn.execute(
                        "UPDATE signals SET status='pending', "
                        "requeue_count=requeue_count+1, "
                        "claimed_by_mission_id=NULL, claimed_at=NULL "
                        "WHERE id=?", (row["id"],))
                    signals_requeued += 1
        conn.commit()
        return {"missions_recovered": len(running_ids),
                "signals_requeued": signals_requeued,
                "signals_abandoned": signals_abandoned}
    except BaseException:
        conn.rollback()
        raise
```

- [ ] **Step 8: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 9 (FC-2 対応): 失敗するテストを書く (`instance_lock`)**

`tests/store/test_instance_lock.py` を新規作成:

```python
"""単一インスタンス保証 (プラン8, 裁定書 FC-2)。"""
from __future__ import annotations

import pytest

from agentic_fx.store.instance_lock import (
    InstanceAlreadyRunning, acquire_instance_lock,
)


def test_acquire_instance_lock_succeeds_when_uncontended(tmp_path):
    fh = acquire_instance_lock(tmp_path / "data")
    assert fh is not None
    fh.close()


def test_acquire_instance_lock_raises_when_already_held(tmp_path):
    """同一プロセス内でも、先に取得した lock file オブジェクトを close
    せずに 2 回目を取得しようとすると失敗する (flock は open file
    description 単位 — 2 回目の open は別の file description になる)。"""
    db_dir = tmp_path / "data"
    first = acquire_instance_lock(db_dir)
    try:
        with pytest.raises(InstanceAlreadyRunning):
            acquire_instance_lock(db_dir)
    finally:
        first.close()


def test_acquire_instance_lock_succeeds_again_after_release(tmp_path):
    db_dir = tmp_path / "data"
    first = acquire_instance_lock(db_dir)
    first.close()  # 解放

    second = acquire_instance_lock(db_dir)
    second.close()
```

```bash
uv run pytest tests/store/test_instance_lock.py -q
```

Expected: FAIL (`ModuleNotFoundError: No module named 'agentic_fx.store.instance_lock'`)。

- [ ] **Step 10 (FC-2 対応): `instance_lock.py` を新規作成**

```python
"""単一インスタンス保証 (プラン8, 裁定書 FC-2)。

`missions.recover_interrupted` は起動時に `status='running'` の全 mission
を無条件に `'interrupted'` へ終端する。同じ DB ディレクトリに対する
二重起動があると、後発プロセスが先発の稼働中 Mission を誤って終端し、
claim 済み signal を横取り requeue しかねない (実 grep で確認済み —
起動経路にプロセス排他が一切無かった)。DB と同一ディレクトリの lock
file への `flock(LOCK_EX | LOCK_NB)` で単一インスタンスを強制する
(fail closed — 取得できなければ起動そのものを中止する)。
"""
from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO


class InstanceAlreadyRunning(Exception):
    """同じ DB ディレクトリに対する別プロセスが既に instance lock を
    保持している (flock 取得失敗)。"""


def acquire_instance_lock(db_dir: Path) -> IO:
    """`db_dir` 直下の `instance.lock` を排他 lock する。

    戻り値のファイルオブジェクトは **呼び出し元プロセスの生涯にわたって
    保持**すること — close (または GC で暗黙 close) されると lock が
    解放される。呼び出し元 (`build_app`) は `App.instance_lock` として
    保持し、`App.close()` (Task 19) で明示的に close して解放する。
    """
    db_dir.mkdir(parents=True, exist_ok=True)
    lock_path = db_dir / "instance.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise InstanceAlreadyRunning(
            f"another agentic-fx process already holds the instance lock "
            f"at {lock_path} — refusing to start (fail closed, FC-2)") from e
    return fh
```

- [ ] **Step 11 (FC-2 対応): テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_instance_lock.py -q
```

Expected: PASS。

- [ ] **Step 12: `build_app` の起動シーケンスに配線**

`src/agentic_fx/service.py` の `build_app` 内、`conn_core = connect(...)` / `init_db(conn_core)` の直前 (264-265 行) に、**FC-2 対応の instance lock 取得**を追加する。`settings` は既に 260 行目 (`settings = load_settings(root / "config" / "settings.yaml")`) で構築済みであり、`missions` は既に 45 行目 (`from agentic_fx.store import approvals, missions, orders, signals`) で import 済み — 新たな import・別名は不要でどちらもそのまま使う。`build_app` の import 節に `from agentic_fx.store.instance_lock import acquire_instance_lock` を追加する:

```python
    # FC-2 (裁定書): recover_interrupted は起動時に status='running' の
    # 全 mission を無条件に interrupted 化する。二重起動があると、後発
    # プロセスが先発の稼働中 Mission を誤って終端し claim 済み signal を
    # 横取り requeue しかねないため、DB 接続・回収より**前**にプロセス
    # 排他 flock を取得する。取得できなければ起動を中止する (fail
    # closed — InstanceAlreadyRunning は呼び出し元の run_service/CLI
    # エントリまで伝播させ、非ゼロ終了させる)。
    instance_lock = acquire_instance_lock(root / "data")

    conn_core = connect(root / "data" / "agentic.db")
    init_db(conn_core)
    # プラン 8 (codex C-5): 前回停止時に running のまま残った mission と、
    # それが claim していた signal を同一トランザクションで回収する。
    # 既存の signals.reclaim_expired (403-407 行付近、lease ベースの
    # 一般的な回収) より前に置く — running mission の signal は claimed_at
    # が直近であり得るため lease ベースでは長時間拾われない。
    missions.recover_interrupted(
        conn_core, now=clock.now(),
        max_requeue=settings.plugin.signal_requeue_max)
```

`App` dataclass (Task 4 で `clock: object` を追加済み) の末尾に `instance_lock: object` を追加する:

```python
@dataclass
class App:
    ...
    clock: object
    instance_lock: object
```

`build_app` の `return App(...)` に `instance_lock=instance_lock` を追加する (Task 4 で `clock=clock` を追加済みの箇所):

```python
    return App(conn_core=conn_core, conn_shell=conn_shell, settings=settings,
               state=state, activity=activity, broker=broker,
               executor=executor, provider=provider, econ=econ,
               collector=collector, rag=rag, trade_loop=trade_loop,
               reflection=reflection, scheduler=scheduler, commands=commands,
               registry=registry, core_lock=core_lock,
               mission_watch=mission_watch, notifier=notifier,
               runner=runner, owns_runner=owns_runner, clock=clock,
               instance_lock=instance_lock)
```

**解放は Task 19 で配線する** (`App.close()` が保持中のリソースを逆順 close する箇所に `instance_lock.close()` を追加する — 本 task では追加しない。Task 19 本文は編集しないこと)。

**副作用 (FC-2)**: 同一 `root` に対して `build_app` を複数回呼ぶ既存テスト (先に作った `App`/`instance_lock` を close せずに再度 `build_app(same_root)` する構成) があれば、2 回目が `InstanceAlreadyRunning` で失敗するようになる (意図どおりの回帰検出だが、既存テストの前提が壊れる)。この対処は次の Step 12.5 で正式なチェックボックス Step として行う (prose 注記に留めない — レビュー反映 2 回目 R2-SN-01)。

- [ ] **Step 12.5 (レビュー反映 2 回目 R2-SN-01): 二重 `build_app` する既存テストを修正する**

`grep -rn "build_app(" tests/` で洗い出した結果、同一 `root`/`tmp_path` に対して `close()` を挟まず `build_app` を 2 回呼ぶ既存テストは `tests/test_service_app.py::test_f1c_startup_reclaim_recovers_claimed_signal` の 1 件のみ (他の `build_app`/`_build_app` 呼び出しはすべて 1 テスト関数につき 1 回、または関数スコープの `tmp_path` フィクスチャで root が毎回別になる — 同種の二重 build_app は無いことを確認済み)。

`tests/test_service_app.py:373-387` を以下のように修正する — `app1` を明示的に close してから `app2` を build する (テストの意図 = 再起動時の claimed signal 回収、を保ったまま。`app1.instance_lock.close()` だけでなく `app1.close(busy_resources=frozenset())` 相当の完全な close が Task 19 実装後は望ましいが、本 task 時点では `App.close()` に `instance_lock` の解放がまだ配線されていない — Step 12 冒頭の「解放は Task 19 で配線する」のとおり。よって本 task では `app1.instance_lock.close()` を直接呼ぶ最小修正に留める):

```python
def test_f1c_startup_reclaim_recovers_claimed_signal(tmp_path):
    """F1(c): 停止時に claimed のまま残った signal 行が、次の build_app
    (= 次回起動) 直後、tick を待たずに pending へ回収されること。"""
    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app1 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())
    sid = signals_store.add(
        app1.conn_core, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="signal", payload={"direction": "long"}, now=NOW)
    lease_min = app1.settings.plugin.signal_lease_min
    old = NOW - timedelta(minutes=lease_min + 5)
    claimed = signals_store.claim_oldest(app1.conn_core, mission_id=999,
                                         now=old, freshness_bars=None)
    assert claimed is not None and claimed["id"] == sid  # 前提

    # FC-2 (プラン8): instance_lock (flock) は App の全寿命で保持される
    # ため、同一 root への 2 回目の build_app は 1 回目の instance_lock を
    # 解放してからでないと InstanceAlreadyRunning になる。「再起動」を
    # 模す以上、1 回目のプロセスが終了して lock を手放したことも模す
    # 必要がある (App.close() への instance_lock 配線は Task 19)。
    app1.instance_lock.close()

    # 「再起動」を模して同じ DB に対しもう一度 build_app する
    app2 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())

    row = app2.conn_core.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"  # 起動時 reclaim が回収した
```

```bash
uv run pytest tests/test_service_app.py -q -k test_f1c_startup_reclaim_recovers_claimed_signal
uv run pytest -q
```

Expected: 全件 PASS (`build_app` を使う既存テストが、`running` 状態の mission 行を残していない限り `recover_interrupted` は no-op で無影響。二重 `build_app` テストは本 Step で対応済み)。

- [ ] **Step 13: 変異テスト**

1. `finish` の `WHERE id=? AND status='running'` から `AND status='running'` を削除 (無条件 UPDATE に戻す) → `test_finish_returns_false_on_second_call_and_does_not_overwrite` が red
2. `recover_interrupted` の `except BaseException: conn.rollback(); raise` を削除 → `test_recover_interrupted_is_atomic_no_partial_state_on_failure` が red (部分的な状態変更が残る)
3. `recover_interrupted` の `if row["requeue_count"] >= max_requeue:` を `if False:` に改変 → `test_recover_interrupted_abandons_signal_over_requeue_limit` が red
4. (FC-2) `instance_lock.py` の `fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)` を no-op (`pass`) に改変 → `test_acquire_instance_lock_raises_when_already_held` が red

- [ ] **Step 14: Commit**

```bash
git add src/agentic_fx/store/missions.py src/agentic_fx/store/instance_lock.py \
  src/agentic_fx/service.py \
  tests/store/test_missions_cas.py tests/store/test_instance_lock.py \
  tests/test_service_app.py
git commit -m "$(cat <<'EOF'
feat: missions.finish の CAS 化 + 起動時 running→interrupted 同時回収 + 単一インスタンス保証

設計書 §4.7 (codex C-4) / §4.8 (codex C-5)。呼び出し元の活用は Task
15/16 の五相再構成で行う。起動時に DB ディレクトリの flock で単一
インスタンスを強制し、recover_interrupted より前に取得することで
二重起動による稼働中 Mission の誤終端を防ぐ (レビュー FC-2)。解放の
配線は Task 19 に委ねる。二重 build_app する既存テスト
(test_f1c_startup_reclaim_recovers_claimed_signal) を修正 (レビュー
反映 2 回目 R2-SN-01)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 12: tick 再編 (hooks を決定論ブロックの後段へ) + `data_hook_timeout_sec` の httpx 注入

設計書 §3.2 を実装する。「決定論ブロック (mark-to-market → account/予約再検証 → `fills_allowed` 判定 → `_process_limit_fills` → `_process_exits`) は内部順序を一切変えずそのまま先頭へ繰り上げ、データ hooks (news/econ/signal maintenance) のみを後段へ移す」。

**設計判断 (writing-plans — advisor 指摘を反映)**: 現行の hooks (news/econ) は `tick()` **冒頭** (`open_now` 判定より前) にあり、「市場が閉じていても hooks は毎 tick 走る」という cross-plan 修正① の意図を持つ。単純に「決定論ブロックの後ろ」へ移すと、決定論ブロックは `open_now` 判定の**内側**にしかない (市場閉鎖時は早期 `return` する) ため、hooks が閉場中に一切走らなくなり週末バグが再発する。**対応: hooks を `_run_hooks(now)` という 1 メソッドに切り出し、`tick()` の全ての return パス直前 (市場閉鎖時の return 直前・`_mark_to_market` 失敗時の return 直前・通常経路の Mission 起動判定の直前) で呼ぶ**。`on_signal_maintenance` (現在は開場ガード内・`_process_limit_fills` より前の独立した位置) も `_run_hooks` に統合する — 閉場中も signal 保守 (鮮度切れ pending の abandoned 化・claimed の lease 回収) が走るようになる点は、既存のちenkeck (news/econ が閉場中も走る) と対称な改善であり退行ではない。

**Files:**
**【着手前照合 2026-08-08 — 参照行を実測で修正した。以下が正】**

- Modify: `src/agentic_fx/core/scheduler.py:80-228` (`tick` 全体の再構成、`_run_hooks` 新設)。**`tick` は 80 行目から 228 行目まで** (次の `def _trade_mission_due` が 229 行)。既存 hooks は **104-111 行** (`open_now` 判定 = 113 行より前)、`on_signal_maintenance` は **194-196 行** (開場ガードの内側)。fail-open 隔離の `_run_data_hook` は **266 行**
- Modify: `src/agentic_fx/datafeed/fetchers.py:148`(`fetch_feed` の def)**`,171`(`feedparser.parse(url)` の実体 — プラン旧記載の 148-160 には無い)**`,200`(`fetch_web` の def)`,212`(`httpx.get(url, timeout=30, ...)`)
- Modify: `src/agentic_fx/datafeed/econ_calendar.py:98`(`fetch_ff_calendar` の def)`,112`(`httpx.get(_URL, timeout=30, ...)`)`,170`(`EconCalendar.__init__`)`,176`(`refresh`)。**プラン旧記載の `171-224` は `__init__`/`refresh` しか含まず、肝心の `fetch_ff_calendar` (98-112) を外していた**
- Modify: `src/agentic_fx/datafeed/news_collector.py:75`(`__init__`)`,82`(`collect`)
- Modify: `src/agentic_fx/service.py:396`(`EconCalendar(...)`)**`,399`(`NewsCollector(...)`)** — **Task 11 で `build_app` 全体を `try:` で囲んだためインデントが 1 段深くなり、約 93 行ドリフトした。かつ 2 つは連続しておらず間に他の構築が挟まる** (プラン旧記載の `303-305` は「連続 3 行」を前提にしていた)

**照合で確認できたこと**: `data_hook_timeout_sec` は **`config.py:242` (`gt=0, default=30.0`) と `config/settings.yaml.example:97` に既に存在する** — 本 task は**配線のみ**でスキーマ追加は不要。ただし**個人設定 `config/settings.yaml` には未記載**なので、example との同期として追記すること (既定値があるので動作はするが、規約上 2 ファイルは同期させる)
- Test: `tests/core/test_scheduler_tick_order.py` (新規 — tick 順序契約の回帰ピン), `tests/datafeed/test_fetchers.py`/`tests/datafeed/test_news_collector.py`/`tests/datafeed/test_econ_calendar.py` (timeout 配線)

**Interfaces:**
- Produces:
  - `Scheduler._run_hooks(self, now: datetime) -> None` — news/econ/signal_maintenance を `_run_data_hook` (既存の fail-open 隔離) 経由で呼ぶ。`tick()` の全 return パス直前で呼ばれる
  - `fetchers.fetch_feed(url: str, source_name: str, *, timeout_sec: float) -> list[Article]` — **`feedparser.parse(url)` から `httpx.get(url, timeout=timeout_sec, follow_redirects=True)` で取得したバイト列を `feedparser.parse(response.content)` に渡す形へ変更** (feedparser 自身に timeout 機構が無いため — httpx 経由に変えることで初めて `data_hook_timeout_sec` が効く)
  - `fetchers.fetch_web(url: str, source_name: str, *, timeout_sec: float) -> list[Article]` — 既存の `httpx.get(url, timeout=30, ...)` の `30` を `timeout_sec` に置換
  - `econ_calendar.fetch_ff_calendar(*, timeout_sec: float) -> CalendarFetch` — 既存の `httpx.get(_URL, timeout=30, ...)` の `30` を `timeout_sec` に置換
  - `NewsCollector.__init__(self, conn, rag, activity, clock, *, timeout_sec: float)` — 新設 kwarg (**キーワード必須 — 既定値を与えない**。配線し忘れの無音成立を防ぐ、既存の `on_econ_cycle` と同じ設計判断)
  - `EconCalendar.__init__(self, conn, activity, clock, *, timeout_sec: float)` — 同上

- [ ] **Step 1: 失敗するテストを書く (tick 順序契約の回帰ピン)**

`tests/core/test_scheduler_tick_order.py` を新規作成する (既存 `tests/core/test_scheduler.py` の `Env` fixture を再利用):

```python
"""tick 順序契約の回帰ピン (プラン8, 設計書 §3.2 codex C2-1/C3-1)。

決定論ブロック (mark-to-market → account/予約再検証 → fills_allowed →
fills → exits) の内部順序・processed-bar マーキング位置・fills_allowed
ゲートを固定する。hooks (news/econ/signal_maintenance) は市場開閉に
関わらず毎 tick 走ることも固定する。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.core.test_scheduler import Env, WED, SAT  # 既存 fixture を再利用


def test_hooks_run_even_when_market_closed():
    """cross-plan 修正① の再発防止: 市場閉鎖中も news/econ/signal
    maintenance は毎 tick 走る。"""
    env = Env()
    news_calls = []
    econ_calls = []
    maint_calls = []
    env.scheduler.on_news_cycle = lambda: news_calls.append(1)
    env.scheduler.on_econ_cycle = lambda: econ_calls.append(1)
    env.scheduler.on_signal_maintenance = lambda now: maint_calls.append(now)

    env.scheduler.tick(SAT)  # 市場閉鎖 (土曜)

    assert news_calls == [1]
    assert econ_calls == [1]
    assert maint_calls == [SAT]


def test_hooks_run_after_deterministic_block_when_market_open():
    """通常経路: hooks は fills_allowed ゲート・fills・exits の**後**に
    走る (fills_allowed の判定材料を hooks が汚染しないことの構造確認 —
    news/econ/signal_maintenance の呼び出し順を記録し、
    _process_limit_fills/_process_exits より後であることを確認する)。
    """
    env = Env()
    order: list[str] = []
    env.scheduler.on_news_cycle = lambda: order.append("news")
    orig_fills = env.scheduler._process_limit_fills
    env.scheduler._process_limit_fills = lambda now: (
        order.append("fills") or orig_fills(now))
    orig_exits = env.scheduler._process_exits
    env.scheduler._process_exits = lambda now, filled_ids: (
        order.append("exits") or orig_exits(now, filled_ids))

    env.scheduler.tick(WED)

    assert order.index("fills") < order.index("exits") < order.index("news")


def test_hooks_run_even_when_mark_to_market_regresses(monkeypatch):
    """_mark_to_market が時系列逆行で False を返して tick が早期 return
    しても hooks は走る。"""
    env = Env()
    news_calls = []
    env.scheduler.on_news_cycle = lambda: news_calls.append(1)
    env.scheduler._mark_to_market = lambda now: False

    env.scheduler.tick(WED)

    assert news_calls == [1]


def test_account_unknown_still_cancels_pending_before_hooks_run():
    """account 不明時の全 pending 取消 (fail closed) は hooks より先に
    確定していること — 決定論ブロックの内部順序が保存されている回帰
    確認 (既存 test_scheduler.py の account 系テストと同型だが、hooks
    再編後も同じ結論になることをこのファイルでも固定する)。
    """
    env = Env()  # account を意図的に欠損させる既存 fixture の使い方に
                  # 合わせて実装すること (既存 test_scheduler.py の該当
                  # テストの account snapshot 未記録パターンを踏襲)
    env.scheduler.tick(WED)
    # 既存 test_scheduler.py の account_unknown 系アサーションと同じ
    # 内容を実装者が転記する (このファイルの目的は「hooks 再編後も同じ
    # 結論になる」ことの確認であり、account 判定ロジック自体は Task 12
    # で変更しない)。
```

（`test_account_unknown_still_cancels_pending_before_hooks_run` は既存 `tests/core/test_scheduler.py` の該当テスト (account 欠損時の全 `pending_fill` 取消) を実ファイルで確認し、同じ assertion をここに転記すること — プレースホルダのまま残さない。）

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_scheduler_tick_order.py -q
```

Expected: `test_hooks_run_even_when_market_closed` と `test_hooks_run_even_when_mark_to_market_regresses` が FAIL (現行実装は市場閉鎖時/mark_to_market 失敗時に hooks を呼ばない)。`test_hooks_run_after_deterministic_block_when_market_open` は現行実装でも本来は FAIL のはず (hooks が **先頭** にあるため `news` が `fills`/`exits` より前に記録される) — 実際に FAIL することを確認する。

- [ ] **Step 3: `scheduler.py` の `tick` を再構成**

`src/agentic_fx/core/scheduler.py:80-218` の `tick` メソッド全体を以下に置き換える (既存のコメント群は意味が変わらない箇所はそのまま維持し、位置が変わった箇所のみ新しいコメントを付す):

```python
    def tick(self, now: datetime) -> None:
        """1 tick 分の決定論的処理。

        **呼び出し契約 (codex C-M5)**: 変更なし (既存 docstring をそのまま
        維持 — このメソッドの上の元 docstring 全文をここに残すこと)。

        **tick 再編 (プラン 8, 設計書 §3.2 codex C2-1/C3-1)**: 決定論
        ブロック (mark-to-market → account/予約再検証 → fills_allowed
        判定 → `_process_limit_fills` → `_process_exits`) は内部順序を
        一切変えずそのまま先頭で実行する。データ hooks (news/econ/signal
        maintenance) は `_run_hooks` に切り出し、**tick の全ての return
        パス直前** (市場閉鎖時・mark-to-market 失敗時・通常経路の Mission
        起動判定の直前) で呼ぶ — 市場閉鎖中も hooks が走ることを維持する
        (cross-plan 修正① の意図の保存。単純に「決定論ブロックの後ろ」に
        だけ置くと、決定論ブロックが `open_now` 判定の内側にしか無いため
        市場閉鎖中に hooks が一切走らなくなる)。
        """
        open_now = market_hours.is_market_open(now)
        if not open_now:
            # レビュー修正 4: _was_open はプロセスメモリのみに保持される。
            # サービスがオープン中に落ち、クローズ後に再起動すると最初の
            # tick で _was_open は None (True でも False でもない) になる。
            # 「True か不明(None)」ならクローズ移行処理を実行する
            # (learning モードなら _on_market_close が早期 return するので無害)。
            if self._was_open is not False:
                self._on_market_close(now)
            self._was_open = False
            self._run_hooks(now)
            return
        self._was_open = True

        # レビュー修正 3: record_snapshot が時系列逆行 (NTP 補正等) で
        # ValueError を送出した場合、mark-to-market 自体が信頼できないため、
        # この tick は安全側に全体スキップする。
        if not self._mark_to_market(now):
            self._run_hooks(now)
            return
        self._resolve_unknowns(now)
        self._expire_limits(now)
        # (以下、account/fills_allowed 判定・_maintain_reservations 呼び出し・
        # _force_close_day の既存コード — 元 133-186 行をそのまま維持する。
        # on_signal_maintenance のインライン呼び出し (元 194-196 行) は
        # ここから削除し _run_hooks へ統合する)
        account = accounting.current_account(self.conn, now)
        fills_allowed = account is not None and account[0] > 0
        if account is None:
            self._cancel_all_pending(
                now, reason="account_unknown",
                event="account_unknown_cancel_pending",
                why="口座 snapshot が陳腐化/欠損 — 総リスク再検証不能")
        elif account[0] <= 0:
            self._cancel_all_pending(
                now, reason="equity_nonpositive",
                event="equity_nonpositive_cancel_pending",
                why="equity<=0 (債務超過) — 予約を維持できない")
        else:
            try:
                if not self._maintain_reservations(now, account):
                    fills_allowed = False
            except Exception as e:  # noqa: BLE001 — 資金保護を止めない
                text = safe_error_text(e)
                self.activity.write(
                    Category.TRADE, "maintain_reservations_error",
                    f"{text} — この tick の予約維持処理を中断")
                _log.warning("maintain_reservations failed: %s", text)
                fills_allowed = False
        self._force_close_day(now)
        filled_ids = self._process_limit_fills(now) if fills_allowed else set()
        self._process_exits(now, filled_ids)

        # プラン 8 tick 再編: hooks は決定論ブロック (mark-to-market〜
        # exits) の**後**、Mission 起動判定の**前**。
        self._run_hooks(now)

        reason = self._trade_mission_due(now)
        if reason is not None:
            if reason == "cron":
                self._last_cron_trade = now
            self.on_trade_mission(reason)

    def _run_hooks(self, now: datetime) -> None:
        """データ hooks (news/econ/signal maintenance) — 決定論ブロック
        の後段で実行する (設計書 §3.2)。**tick() の全ての return パスの
        直前で呼ぶこと** — 市場閉鎖時・mark-to-market 失敗時・通常時の
        いずれでも hooks は毎 tick 走る (「hooks が週末しか走らない」旧
        欠陥 = cross-plan 修正① の再発防止)。
        """
        if self._last_news is None or now - self._last_news >= _NEWS_INTERVAL:
            self._last_news = now
            self._run_data_hook("news", self.on_news_cycle)
        if self._last_econ is None or now - self._last_econ >= _ECON_INTERVAL:
            self._last_econ = now
            self._run_data_hook("econ", self.on_econ_cycle)
        if self.on_signal_maintenance is not None:
            self._run_data_hook(
                "signal_maintenance", lambda: self.on_signal_maintenance(now))
```

**実装者への注意**: `account`/`fills_allowed` 判定ブロックの中身 (`_cancel_all_pending`/`_maintain_reservations`/`_force_close_day` 呼び出し) は元コードの 145-186 行を**逐語**維持すること (このプランに再掲した版は要約ではなく実際に貼り付けるべきコードそのもの — 元ファイルと突き合わせて 1 行も落ちていないことを確認する)。`_trade_mission_due`/`_run_data_hook`/`_fresh_bar` 等の他メソッドは無変更 (このタスクでは触らない)。

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_scheduler_tick_order.py -q
uv run pytest tests/core/test_scheduler.py -q
uv run pytest tests/core/test_scheduler_signal.py -q
uv run pytest -q
```

Expected: 全件 PASS。**注意**: `tests/core/test_scheduler_signal.py` (プラン 7 の signal maintenance 配線テスト) が「開場中のみ呼ばれる」という前提のアサーションを持っていれば、この task の変更 (閉場中も呼ばれるようになる) で FAIL する — その場合はテストの前提を修正する (仕様変更として明記: `on_signal_maintenance` は市場開閉に関わらず毎 tick 走るようになった)。

- [ ] **Step 5: 失敗するテストを書く (httpx timeout 注入)**

`tests/datafeed/test_fetchers.py` に追加 (既存 fixture — `httpx.MockTransport` 等 — を確認して揃える):

```python
def test_fetch_web_uses_injected_timeout(monkeypatch):
    captured = {}

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        raise RuntimeError("stop here — this test only checks the timeout arg")

    import agentic_fx.datafeed.fetchers as fetchers_mod
    monkeypatch.setattr(fetchers_mod.httpx, "get", fake_get)
    with pytest.raises(RuntimeError):
        fetchers_mod.fetch_web("http://x", "s", timeout_sec=7.5)
    assert captured["timeout"] == 7.5


def test_fetch_feed_uses_httpx_with_injected_timeout(monkeypatch):
    """feedparser.parse(url) から httpx 経由の取得に変わったことの確認。"""
    captured = {}

    class FakeResponse:
        content = b"<rss><channel></channel></rss>"

        def raise_for_status(self):
            pass

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        return FakeResponse()

    import agentic_fx.datafeed.fetchers as fetchers_mod
    monkeypatch.setattr(fetchers_mod.httpx, "get", fake_get)
    fetchers_mod.fetch_feed("http://x", "s", timeout_sec=7.5)
    assert captured["timeout"] == 7.5
```

`tests/datafeed/test_econ_calendar.py` に追加:

```python
def test_fetch_ff_calendar_uses_injected_timeout(monkeypatch):
    captured = {}

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        raise RuntimeError("stop here")

    import agentic_fx.datafeed.econ_calendar as econ_mod
    monkeypatch.setattr(econ_mod.httpx, "get", fake_get)
    with pytest.raises(RuntimeError):
        econ_mod.fetch_ff_calendar(timeout_sec=7.5)
    assert captured["timeout"] == 7.5
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/datafeed/test_fetchers.py tests/datafeed/test_econ_calendar.py -q -k injected_timeout
```

Expected: 全件 FAIL (`TypeError: fetch_web() got an unexpected keyword argument 'timeout_sec'` 等)。

- [ ] **Step 7: `fetchers.py`/`econ_calendar.py` を実装**

`src/agentic_fx/datafeed/fetchers.py:148` の `def fetch_feed(url: str, source_name: str) -> list[Article]:` を `def fetch_feed(url: str, source_name: str, *, timeout_sec: float) -> list[Article]:` に変更し、関数本体冒頭の `parsed = feedparser.parse(url)` を以下に置き換える:

```python
    response = httpx.get(url, timeout=timeout_sec, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
```

`fetch_web` (200 行) のシグネチャを `def fetch_web(url: str, source_name: str, *, timeout_sec: float) -> list[Article]:` に変更し、本体の `r = httpx.get(url, timeout=30, follow_redirects=True)` の `timeout=30` を `timeout=timeout_sec` に変更する。

`src/agentic_fx/datafeed/econ_calendar.py:171` の `def fetch_ff_calendar() -> CalendarFetch:` を `def fetch_ff_calendar(*, timeout_sec: float) -> CalendarFetch:` に変更し、`r = httpx.get(_URL, timeout=30, follow_redirects=True)` の `timeout=30` を `timeout=timeout_sec` に変更する。`EconCalendar.__init__` (170 行) に `timeout_sec: float` をキーワード必須で追加し `self.timeout_sec = timeout_sec` を保持、`refresh()` (176 行) 内の `fetched = fetch_ff_calendar()` を `fetched = fetch_ff_calendar(timeout_sec=self.timeout_sec)` に変更する。

- [ ] **Step 8: `news_collector.py` を実装**

`src/agentic_fx/datafeed/news_collector.py` の `NewsCollector.__init__` (74-79 行) に `timeout_sec: float` をキーワード必須で追加し `self.timeout_sec = timeout_sec` を保持する。`collect()` (82-114 行) 内の `articles = fetch(src["url"], src["name"])` を `articles = fetch(src["url"], src["name"], timeout_sec=self.timeout_sec)` に変更する。

- [ ] **Step 9: テスト実行して PASS を確認**

```bash
uv run pytest tests/datafeed/ -q
```

Expected: 全件 PASS (既存の `fetch_web`/`fetch_feed`/`fetch_ff_calendar`/`NewsCollector`/`EconCalendar` 呼び出しテストは `timeout_sec` キーワード必須化により **FAIL するはず** — 既存呼び出しへ `timeout_sec=<既存テストの妥当な値、例 10>` を追記して直すこと。これは意図した破壊的変更であり、テストの追従修正が本 step の主作業)。

- [ ] **Step 10: `service.py` に配線**

`src/agentic_fx/service.py:303-305` の `econ = EconCalendar(conn_core, activity, clock)` / `collector = NewsCollector(conn_core, rag, activity, clock)` を以下に変更する:

```python
    econ = EconCalendar(conn_core, activity, clock,
                        timeout_sec=settings.worker.data_hook_timeout_sec)
    rag = Rag(root / "data" / "rag", embedding_function=embedding_fn,
             lock_timeout_sec=settings.worker.rpc_timeout_sec)
    collector = NewsCollector(conn_core, rag, activity, clock,
                              timeout_sec=settings.worker.data_hook_timeout_sec)
```

(`rag = Rag(...)` の行は Task 9 Step 5 で既に `lock_timeout_sec` 配線済み — ここでは順序の参考として再掲したのみで変更不要。実装時は `econ`/`collector` の 2 行だけを変更すること。)

- [ ] **Step 11: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 12: 変異テスト**

1. `_run_hooks` の呼び出しを市場閉鎖 return パスから削除 → `test_hooks_run_even_when_market_closed` が red
2. `tick()` 内の `_run_hooks(now)` の呼び出し位置を `_process_limit_fills` の**前**に戻す → `test_hooks_run_after_deterministic_block_when_market_open` が red
3. `fetch_web` の `timeout=timeout_sec` を `timeout=30` に戻す → `test_fetch_web_uses_injected_timeout` が red
4. `fetch_feed` の `httpx.get` 呼び出しを削除し `feedparser.parse(url)` に戻す → `test_fetch_feed_uses_httpx_with_injected_timeout` が red

- [ ] **Step 13: Commit**

```bash
git add src/agentic_fx/core/scheduler.py src/agentic_fx/datafeed/fetchers.py \
  src/agentic_fx/datafeed/econ_calendar.py src/agentic_fx/datafeed/news_collector.py \
  src/agentic_fx/service.py tests/core/test_scheduler_tick_order.py \
  tests/datafeed/test_fetchers.py tests/datafeed/test_econ_calendar.py
git commit -m "$(cat <<'EOF'
refactor: tick 再編 (hooksを決定論ブロック後段へ) + data_hook_timeout_sec の httpx 注入

設計書 §3.2。決定論ブロックの内部順序は不変のまま先頭に、hooks は
tick の全 return パス直前 (_run_hooks) で毎 tick 実行する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 13: mission supervisor スケルトン (容量 1 `try_submit` + trade/reflection 連鎖 + ask 統一)

設計書 §3.3「supervisor とスロット意味論」を実装する。**本 task では `core_lock` の保持範囲を変えない** — trade/reflection/ask の実行は引き続き呼び出し全体を `core_lock` で包む (scheduler tick との直列性は現状維持)。これは意図的な段階分け: 本 task は「Mission 実行を scheduler スレッドから専用の supervisor スレッドへ移し、正しいキューイング意味論 (単一スロット・原子的 `try_submit`・ask の Future 化) を導入する」ことだけに集中し、「lock の保持範囲を commit-core だけに狭める」(= 「Mission 実行中も SL/TP 監視継続」の本体) は Task 15/16 に委ねる。この段階分けにより、両 task を独立してレビュー・検証できる (Task 13 単体では tick の資金保護窓は変わらない — 変わるのは「誰が Mission を呼ぶスレッドか」だけ)。

**Files:**

**【着手前照合 2026-08-08 — 参照行を実測で修正した。以下が正】**

- Create: `src/agentic_fx/core/supervisor.py` (既存なしを確認)
- Modify: `src/agentic_fx/core/scheduler.py:43`(`on_trade_mission` の型ヒント — `Callable[[str], None]`)**`,185-189`(`tick` 内の呼び出し規約変更)**
  - **Task 12 で `tick` を `try/finally` 化したので、`self.on_trade_mission(reason)` は `finally:` ブロックの外 (185-189 行) にある。** プラン旧記載の `210-217` は現在の `_trade_mission_due` の docstring あたりを指す
- Modify: `src/agentic_fx/service.py:186`(`class App` — `supervisor`/`conn_supervisor` 追加)`,240`(`_LockedAsk` 削除 → `_SupervisorAsk` 新設)**`,452`(`on_trade_mission` の定義)`,495`(`Commands` の `trade_loop=_LockedAsk(...)` 配線)`,497-505`(`return App(...)` 構築)`,606`(`scheduler_thread` — 変更なし、確認のみ)**
  - **Task 11 で `build_app` 全体を `try:` で囲んだため、配線箇所は約 90〜100 行ドリフトしている** (プラン旧記載 `357-414` / `415-422` / `502-514` はいずれも古い)。新しいコードは **`try:` の内側**に置くこと
- Test: `tests/core/test_supervisor.py` (新規、既存なしを確認)

**照合で確認できたこと**:
- `heartbeat_pump_interval_sec` は**モジュール定数 `_HEARTBEAT_PUMP_INTERVAL_SEC` + 既定引数**であり、`config.py` / `settings.yaml` への追加は不要
- `dispatch_ceiling_sec` は Task 19 (watchdog) 側の関心事で、本 task では `busy_since` を公開するだけ
- `conn_supervisor` を `connect_readonly` で構築する修正 (プランレビュー I1 / 裁定書 F-7) は**プラン本文に反映済み** — Step 9 の逐語コードは正しい

**Interfaces:**
- Produces:
  - `supervisor.MissionSupervisor(*, trade_fn: Callable[[str], object], reflection_fn: Callable[[], object], ask_fn: Callable[[str], str]) -> object`
    - `start(self) -> None` — 内部スレッド (daemon) を起動する
    - `try_submit(self, kind: str, **kwargs) -> concurrent.futures.Future | None` — **原子的契約**: 内部 lock 下で「実行中ジョブ + 予約済みジョブの有無」を判定し、空きがあれば受理して `Future` を返す、busy なら即 `None` を返す。判定と投入を分離しない (TOCTOU 封鎖)。`kind` は `"trade"` (`kwargs={"trigger": str}`) / `"reflection"` (本プランでは `"trade"` に内包され単独では公開しない) / `"ask"` (`kwargs={"question": str}`)
    - `shutdown(self, *, drain_exc: Exception) -> None` — 新規受付停止 + queue 内の**未着手**ジョブの pending Future を `drain_exc` で例外完了させる (**ブロックしない** — 実行中ジョブの完了待ちは呼び出し側の `join()` の責務)
    - `fail_pending(self, *, exc: Exception) -> None` — supervisor スレッド死亡時に watchdog (Task 19) が呼ぶ想定。queue 内の pending Future を `exc` で例外完了させる (`shutdown` と同じ内部実装を共有してよい)
    - `join(self, timeout: float | None = None) -> None` / `is_alive(self) -> bool`
    - `heartbeat: float` — `time.monotonic()` 由来の生存確認用属性 (ジョブ待ちループのたびに更新。**加えて (裁定書 F-3 / CR-1) `_dispatch` 実行中も内部の heartbeat ポンプ daemon スレッドが `heartbeat_pump_interval_sec` 間隔で touch し続ける** — trade+reflection 連鎖が数百秒に及んでも watchdog (Task 19) の `heartbeat_grace_sec` を誤って超過しない。watchdog (Task 19) が鮮度監視に使う)
    - `busy_since: float | None` — **(裁定書 F-3 / CR-1 advisor 指摘反映)** dispatch 開始時刻 (`time.monotonic()`)。非 busy 時は `None`。heartbeat ポンプ単体は「supervisor スレッドが生きている」ことしか示さず `_dispatch` 内部の genuine なデッドロックを見逃す (fail-open の穴) ため、Task 19 watchdog は `busy_since` と設定由来の上限 (`dispatch_ceiling_sec`) を突き合わせる独立した第二の軸でハングを検出する
  - `Scheduler.on_trade_mission: Callable[[str], bool]` — **戻り値の型が `None` → `bool` に変わる** (`True` = supervisor が受理、`False` = busy で拒否)。`tick()` の呼び出し側 (210-217 行) は戻り値を見て `reason == "cron"` かつ `True` のときだけ `_last_cron_trade` を前進させる (設計書 §3.3「cron の意味論」— busy 拒否は締切を維持し次 tick 以降で必ず再試行される)
  - `App.supervisor: MissionSupervisor` / `App.conn_supervisor: object` (新設フィールド。後者は Task 15 の commit-pre 相が使う読取専用の lock 外接続 — 本 task では構築するだけで未使用)

- [ ] **Step 1: 失敗するテストを書く**

`tests/core/test_supervisor.py` を新規作成:

```python
"""MissionSupervisor (プラン8, 設計書 §3.3)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.core.supervisor import MissionSupervisor


def _blocking_trade_fn(release: threading.Event):
    def fn(trigger):
        release.wait(5.0)
        return {"trigger": trigger}
    return fn


def test_try_submit_accepts_when_idle():
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    future = sup.try_submit("trade", trigger="cron")
    assert future is not None
    result = future.result(timeout=2.0)
    assert result == {"trade": {"t": "cron"}, "reflection_count": 0}


def test_try_submit_rejects_when_busy():
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    assert f1 is not None
    time.sleep(0.05)  # f1 が確実にディスパッチされてから 2 回目を試す

    f2 = sup.try_submit("trade", trigger="signal")
    assert f2 is None  # busy — 即座に拒否

    release.set()
    f1.result(timeout=2.0)


def test_try_submit_accepts_again_after_previous_completes():
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    f1.result(timeout=2.0)
    f2 = sup.try_submit("trade", trigger="cron")
    assert f2 is not None
    f2.result(timeout=2.0)


def test_trade_job_chains_reflection_after():
    calls: list[str] = []

    def trade_fn(trigger):
        calls.append("trade")
        return {"trigger": trigger}

    def reflection_fn():
        calls.append("reflection")
        return 2

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=reflection_fn,
                            ask_fn=lambda q: q)
    sup.start()
    future = sup.try_submit("trade", trigger="cron")
    result = future.result(timeout=2.0)
    assert calls == ["trade", "reflection"]
    assert result["reflection_count"] == 2


def test_ask_job_returns_via_future():
    sup = MissionSupervisor(trade_fn=lambda t: None, reflection_fn=lambda: 0,
                            ask_fn=lambda q: f"answer: {q}")
    sup.start()
    future = sup.try_submit("ask", question="usdjpy どう？")
    assert future.result(timeout=2.0) == "answer: usdjpy どう？"


def test_shutdown_fails_pending_future_without_blocking():
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")  # 即座にディスパッチされ実行中になる
    time.sleep(0.05)
    f2 = sup.try_submit("trade", trigger="cron")
    assert f2 is None  # busy のため queue には入っていない (実行中ジョブが 1 つあるのみ)

    # shutdown は "未着手" (queue 内で待機中) の Future を例外完了させる契約
    # だが、上のケースでは queue が空 (f1 は既にディスパッチ済み) のため
    # shutdown 呼び出し自体は何もしない — 実行中ジョブの終了は待たない
    # (ブロックしないことの確認)。
    start = time.monotonic()
    sup.shutdown(drain_exc=RuntimeError("shutting down"))
    assert time.monotonic() - start < 0.5  # ブロックしていない

    release.set()
    f1.result(timeout=2.0)  # 実行中ジョブは通常どおり完了する


def test_heartbeat_is_touched_during_long_dispatch():
    """裁定書 F-3 (CR-1) の回帰ピン: _dispatch が長時間ブロックしている
    間も heartbeat が touch され続ける (while ループ先頭だけでは更新
    されない — watchdog の heartbeat_grace_sec 誤検知を防ぐ)。real-sleep
    予算内に収めるため pump 間隔を 0.05s に短縮して注入する。"""
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q,
                            heartbeat_pump_interval_sec=0.05)
    sup.start()
    sup.try_submit("trade", trigger="cron")
    time.sleep(0.02)
    hb_before = sup.heartbeat
    time.sleep(0.2)  # dispatch はまだ release 待ちでブロック中
    hb_during = sup.heartbeat
    assert hb_during > hb_before  # ポンプが touch している

    release.set()


def test_busy_since_is_set_during_dispatch_and_cleared_after():
    """裁定書 F-3 (CR-1) advisor 指摘反映の回帰ピン: busy_since は
    dispatch 開始時に time.monotonic() を持ち、完了後は None に戻る —
    Task 19 watchdog の dispatch_ceiling_sec 判定の入力になる。"""
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    assert sup.busy_since is None
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    time.sleep(0.05)
    assert sup.busy_since is not None
    assert sup.busy_since <= time.monotonic()

    release.set()
    f1.result(timeout=2.0)
    time.sleep(0.05)  # finally 節が busy_since=None を反映するまで
    assert sup.busy_since is None
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_supervisor.py -q
```

Expected: 全件 FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `supervisor.py` を実装**

```python
"""MissionSupervisor — 容量 1 のジョブスロットで Mission 実行を単一スレッド
直列実行する (プラン8, 設計書 §3.3)。

直列性の保証は「supervisor スレッドが 1 本」という構造に置く。`try_submit`
は内部 lock の下で「実行中 + 予約済み」の有無を判定し受理/拒否を即座に
返す原子的契約 (TOCTOU 封鎖)。ジョブは 2 種:
`"trade"` (kwargs={"trigger"} — 完了直後に reflection バッチを自動連鎖) /
`"ask"` (kwargs={"question"} — Future で結果を返す)。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable

_log = logging.getLogger("agentic_fx.supervisor")


class MissionSupervisor:
    def __init__(self, *, trade_fn: Callable[[str], object],
                reflection_fn: Callable[[], object],
                ask_fn: Callable[[str], str],
                heartbeat_pump_interval_sec: float = _HEARTBEAT_PUMP_INTERVAL_SEC,
                ) -> None:
        self._trade_fn = trade_fn
        self._reflection_fn = reflection_fn
        self._ask_fn = ask_fn
        # 裁定書 F-3 (CR-1): テストが real-sleep 予算 (数秒以内) を守れる
        # よう、pump 間隔を注入可能にする (既定は本番用の定数)。
        self._heartbeat_pump_interval_sec = heartbeat_pump_interval_sec
        self._lock = threading.Lock()
        self._busy = False
        self._queue: "queue.Queue[tuple[str, dict, Future] | None]" = \
            queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.heartbeat: float = time.monotonic()
        # 裁定書 F-3 (CR-1) advisor 指摘反映: heartbeat ポンプは
        # 「supervisor スレッドが生きている」ことしか示さず、_dispatch が
        # 呼ぶ先 (broker.submit 等) が真にデッドロックした場合はポンプが
        # 動き続けてしまい heartbeat だけでは検出できない (fail-open の
        # 穴)。`busy_since` (dispatch 開始時刻、非 busy 時は None) を
        # 別属性として公開し、Task 19 watchdog がこれと設定由来の上限
        # (dispatch_ceiling_sec) を突き合わせて「heartbeat は新鮮だが
        # dispatch が上限を超えて戻ってこない」を独立に検出できるように
        # する (heartbeat 鮮度チェックと busy_since 上限チェックは別軸)。
        self.busy_since: float | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="afx-supervisor")
        self._thread.start()

    def try_submit(self, kind: str, **kwargs) -> Future | None:
        with self._lock:
            if self._busy:
                return None
            self._busy = True
            future: Future = Future()
            self._queue.put((kind, kwargs, future))
            return future

    def shutdown(self, *, drain_exc: Exception) -> None:
        """新規受付停止 + queue 内の未着手ジョブを例外完了させる (ブロック
        しない — 実行中ジョブの完了待ちは呼び出し側の join() の責務)。"""
        self._stop_event.set()
        self.fail_pending(exc=drain_exc)

    def fail_pending(self, *, exc: Exception) -> None:
        """queue 内 (まだディスパッチされていない) の pending Future を
        `exc` で例外完了させる。supervisor スレッド死亡時に watchdog
        (Task 19) が呼ぶ経路と shutdown の両方から共有される。"""
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return
        if item is not None:
            _, _, future = item
            if not future.done():
                future.set_exception(exc)
            with self._lock:
                self._busy = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 内部 -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.heartbeat = time.monotonic()
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                continue
            kind, kwargs, future = item
            # レビュー反映1回目 (裁定書 F-3 / CR-1): _dispatch は trade+
            # reflection 連鎖で数百秒に及びうる (mission.timeout_sec +
            # worker_grace_sec を trade/reflection 各回で消費しうる) —
            # while ループ先頭でしか heartbeat を touch しないと、watchdog
            # の heartbeat_grace_sec (Task 19) を通常の Mission サイクル
            # だけで超過し、誤って fatal 判定される。dispatch 実行中も
            # 別スレッドで heartbeat を touch し続ける「ポンプ」を回す。
            pump_stop = threading.Event()
            pump = threading.Thread(
                target=self._pump_heartbeat, args=(pump_stop,),
                daemon=True, name="afx-supervisor-heartbeat-pump")
            pump.start()
            self.busy_since = time.monotonic()
            try:
                result = self._dispatch(kind, kwargs)
                if not future.cancelled():
                    future.set_result(result)
            except Exception as e:  # noqa: BLE001 — supervisor スレッドを殺さない
                _log.exception("mission job %r failed", kind)
                if not future.cancelled():
                    future.set_exception(e)
            finally:
                pump_stop.set()
                pump.join(timeout=1.0)
                self.busy_since = None
                with self._lock:
                    self._busy = False

    def _pump_heartbeat(self, stop: threading.Event) -> None:
        """裁定書 F-3 (CR-1): `_dispatch` 実行中も `heartbeat` を
        `_HEARTBEAT_PUMP_INTERVAL_SEC` 間隔で touch し続ける。

        **このポンプ単体は fail-open の穴を持つ**: `pump.daemon=True` の
        スレッドは `_run` の親スレッド (supervisor スレッド) の生死とは
        独立に走り続けるため、`_dispatch` 内部 (`trade_fn`/`reflection_fn`
        が呼ぶ broker.submit・DB 書込等) が genuine にデッドロックしても
        ポンプは `heartbeat` を touch し続けてしまい、`heartbeat` の鮮度
        だけを見る監視では検出できない。**したがってこのポンプは
        `heartbeat_grace_sec` (Task 19) を小さく保つためだけに存在し、
        「supervisor が壊れていないか」の判定は `heartbeat` 鮮度チェック
        単独では完結させない** — Task 19 watchdog は `busy_since`
        (dispatch 開始時刻) と設定由来の `dispatch_ceiling_sec` を突き合わせる
        **独立した第二の軸**で「heartbeat は新鮮だが dispatch が上限を
        超えて戻ってこない」を検出する (Task 19 参照)。この 2 軸の組合せ
        で初めて「grace は Mission 所要時間に非依存」かつ「回復不能な
        ハングは必ず検出される」の両方が成立する。
        """
        while not stop.wait(self._heartbeat_pump_interval_sec):
            self.heartbeat = time.monotonic()

    def _dispatch(self, kind: str, kwargs: dict) -> object:
        if kind == "trade":
            trade_result = self._trade_fn(kwargs["trigger"])
            reflection_count = self._reflection_fn()
            return {"trade": trade_result,
                   "reflection_count": reflection_count}
        if kind == "ask":
            return self._ask_fn(kwargs["question"])
        raise ValueError(f"unknown job kind: {kind!r}")
```

モジュール冒頭の定数群 (`_log = logging.getLogger(...)` の直後) に追加する:

```python
# 裁定書 F-3 (CR-1): heartbeat ポンプの touch 間隔。Task 19 の
# heartbeat_grace_sec はこの値に対してのみ余裕 (目安 6 倍程度) を見ればよい。
_HEARTBEAT_PUMP_INTERVAL_SEC = 5.0
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_supervisor.py -q
```

Expected: 全件 PASS。

- [ ] **Step 5: 失敗するテストを書く (`scheduler.py` の戻り値契約変更)**

`tests/core/test_scheduler.py` に追加:

```python
def test_cron_deadline_only_advances_when_on_trade_mission_returns_true():
    """設計書 §3.3: cron 締切の前進は on_trade_mission (supervisor.
    try_submit の結果) が True (受理) のときだけ。busy (False) なら
    締切は維持され次 tick 以降で必ず再試行される。"""
    env = Env()
    env.scheduler.on_trade_mission = lambda reason: False  # busy を模す

    env.scheduler.tick(WED)
    # busy で拒否されたので締切は前進していない — 直後の tick でも
    # 再び "cron" が due になる
    assert env.scheduler._trade_mission_due(
        WED + timedelta(minutes=1)) == "cron"
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_scheduler.py -q -k only_advances_when
```

Expected: FAIL (現行実装は `on_trade_mission` の戻り値を見ずに `reason == "cron"` だけで前進させる)。

- [ ] **Step 7: `scheduler.py` を実装**

`src/agentic_fx/core/scheduler.py:43` の `on_trade_mission: Callable[[str], None]` を `on_trade_mission: Callable[[str], bool]` に変更する (型ヒントのみ)。`tick()` 内の該当ブロック (Task 12 で `_run_hooks` に再構成済みの版の末尾) を以下に変更する:

```python
        reason = self._trade_mission_due(now)
        if reason is not None:
            # プラン 8 (設計書 §3.3): cron 締切の前進は on_trade_mission
            # (supervisor.try_submit の結果) が受理 (True) のときだけ。
            # busy (False) なら締切は維持し、次 tick 以降で必ず再試行
            # させる (signal は claim 前なので取りこぼしなし)。
            accepted = self.on_trade_mission(reason)
            if accepted and reason == "cron":
                self._last_cron_trade = now
```

- [ ] **Step 8: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_scheduler.py -q
uv run pytest -q
```

**【2026-08-08 訂正 — プランの自己矛盾を実装時に検出】** 旧記載は「既存の `test_cron_deadline_advances_even_when_mission_callback_raises` (Task 4) は変更後も同じ挙動 (締切前進 → 例外伝播) を保つ」としていたが、**上の逐語コードと両立しない**。`accepted = self.on_trade_mission(reason)` が例外を送出すれば `if accepted and ...` に到達しないので、**締切は前進しない**。

**ユーザー裁定 (2026-08-08): 前進させない = 実装のままでよい。** 根拠 — プラン 5 で codex I1 を受けて「例外時も前進させる」と決めたのは「**毎 tick 再試行すると障害時に LLM/notifier を連打する**」からだった。しかし Task 13 以降 `on_trade_mission` は `supervisor.try_submit(...) is not None` を返すだけの**非ブロッキングな投入**であり、**Mission 本体は supervisor スレッドで走る**。例外が出るのは `try_submit` の内部エラーだけで、**LLM 呼び出しも notifier 送信も起きない** — 連打の懸念は前提ごと消えている。毎 tick 再試行してもログが出るだけ。

したがって既存テストは `test_cron_deadline_does_not_advance_when_mission_callback_raises` へ改名し、期待値を反転させる (実装者が実施済み)。

Expected: 全件 PASS。

- [ ] **Step 9: `service.py` を実装 (supervisor 配線 + `_SupervisorAsk`)**

`src/agentic_fx/service.py` の import 節に `from agentic_fx.core.supervisor import MissionSupervisor` を追加する。`_LockedAsk` クラス (232-241 行) を削除し、以下に置き換える:

```python
class _SupervisorAsk:
    """ask を supervisor 経由で実行する薄いラッパー (`_LockedAsk` の後継 —
    設計書 §3.3「ask の統一」。core_lock を直接掴まない)。"""

    def __init__(self, supervisor: MissionSupervisor,
                wait_timeout_sec: float) -> None:
        self._supervisor = supervisor
        self._wait_timeout_sec = wait_timeout_sec

    def ask_once(self, question: str) -> str:
        future = self._supervisor.try_submit("ask", question=question)
        if future is None:
            return ("(現在 Mission 実行中のため質問を受け付けられません。"
                    "しばらくして再試行してください)")
        try:
            return future.result(timeout=self._wait_timeout_sec)
        except TimeoutError:
            return "(Mission 失敗: ask がタイムアウトしました)"
        except Exception as e:  # noqa: BLE001 — 元の ask_once の service
            # boundary (trade_loop.ask_once 内) が normalize 済みの文字列を
            # 返す設計だが、supervisor 経由の Future.exception() 化で
            # 二重に例外化され得るため、ここでも最終防波堤を置く。
            return f"(Mission 失敗: {e})"
```

`build_app` 内、`core_lock = threading.RLock()` (357 行) の直後、`on_trade_mission` 定義 (359-366 行) を以下に置き換える:

```python
    core_lock = threading.RLock()

    # プラン 8 (段階分け — Task 13 では lock の保持範囲を変えない):
    # trade/reflection/ask の実行は引き続き呼び出し全体を core_lock で
    # 包む。scheduler tick との直列性を維持したまま、Mission 呼び出しの
    # 主体を scheduler スレッドから supervisor スレッドへ移す (ロック
    # 粒度の再設計は Task 15/16)。
    def _trade_fn(trigger: str):
        with core_lock:
            return trade_loop.run_once(trigger)

    def _reflection_fn():
        with core_lock:
            return reflection.run_pending()

    def _ask_fn(question: str) -> str:
        with core_lock:
            return trade_loop.ask_once(question)

    supervisor = MissionSupervisor(
        trade_fn=_trade_fn, reflection_fn=_reflection_fn, ask_fn=_ask_fn)

    def on_trade_mission(trigger: str) -> bool:
        # trigger は scheduler._trade_mission_due() が返した起動理由。
        # supervisor.try_submit が受理すれば True (scheduler 側が cron
        # 締切を前進させる判断材料になる — 設計書 §3.3)。
        return supervisor.try_submit("trade", trigger=trigger) is not None
```

`Commands` の構築 (411-414 行) の `trade_loop=_LockedAsk(trade_loop, core_lock)` を以下に置き換える (ask の wall-clock timeout = mission timeout + preemption 猶予 + マージン — 設計書 §3.3):

```python
    ask_wait_timeout_sec = (settings.llama_swap.timeout_sec
                            + settings.worker.worker_grace_sec
                            + settings.worker.worker_terminate_grace_sec + 10.0)
    commands = Commands(conn=conn_shell, state_store=state,
                        broker=shell_broker,
                        trade_loop=_SupervisorAsk(supervisor, ask_wait_timeout_sec),
                        activity=activity, log_dir=root / "logs", clock=clock)
```

`build_app` 内、`conn_shell = connect(root / "data" / "agentic.db")` (266 行) の直後に以下を追加する (Task 15 の commit-pre 相が使う lock 外の読取専用接続 — 本 task では変数を用意するだけで未使用。**独立した名前付きローカル変数として定義する** — `App(...)` の kwargs に直接 `connect_readonly(...)` を書かないこと。Task 15 が `TradeLoop(...)` 構築時にこの変数をそのまま参照するため)。

**(レビュー反映 1 回目 — 裁定書 F-7 / IM-1 / P8-02)**: Global Constraints は `conn_supervisor` を「読取専用接続」と規定するが、当初案は通常の書込可能 `connect(...)` を使っていた — 文書上の性質がコードで強制されない。Task 5 で新設した `db.connect_readonly` (URI `mode=ro`) を使う (`query_only` PRAGMA ではなく真の RO 接続 — 設計書 §3.4 と同一契約):

```python
    # プラン 8 (Task 13, レビュー反映1回目 IM-1/P8-02): commit-pre 相
    # (lock 外) が使う読取専用の lock 外接続。core_lock は取らない
    # (Task 15 で使用開始)。Global Constraints「読取専用接続」の性質を
    # connect_readonly (URI mode=ro) で構造的に強制する — query_only
    # PRAGMA は使わない (プロセス内の別接続からは無効化されうるため)。
    conn_supervisor = connect_readonly(root / "data" / "agentic.db")
```

`build_app` の import 節に `from agentic_fx.store.db import connect, connect_readonly, init_db` (既存の `connect, init_db` に `connect_readonly` を追加) を確認する。

`build_app` の `App(...)` 構築 (415-422 行) に `supervisor=supervisor, conn_supervisor=conn_supervisor` を追加する。`App` dataclass (183-206 行) に `supervisor: object` / `conn_supervisor: object` フィールドを追加する。

`run_service` (534-537 行付近、`th = threading.Thread(target=scheduler_thread, ...)` の前) に `app.supervisor.start()` を追加する:

```python
    app.supervisor.start()
    th = threading.Thread(target=scheduler_thread, daemon=True)
    th.start()
```

（supervisor の停止 (`shutdown`/`join`) は Task 19 の停止状態機械で配線する — 本 task では daemon スレッドとして起動するのみ。）

- [ ] **Step 10: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。**注意**: `tests/test_service_app.py`/`tests/loops/` 等で `_LockedAsk` を直接 import/参照しているテストがあれば `grep -rn "_LockedAsk" tests/` で洗い出し、`_SupervisorAsk` へ更新する。`Commands.trade_loop` (実体は `_SupervisorAsk`/`_LockedAsk`) の型を直接 assert しているテストがあれば同様に更新する。

**(レビュー反映 1 回目 — 裁定書 F-7 / IM-1 / P8-02) `tests/test_service_app.py` に追加**:

```python
def test_conn_supervisor_is_readonly(tmp_path):
    """Global Constraints: conn_supervisor は読取専用接続でなければ
    ならない (IM-1/P8-02) — 書込は OperationalError になる。"""
    app = build_app(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            app.conn_supervisor.execute(
                "INSERT INTO missions(loop, runner, model, status, "
                "started_at) VALUES ('trade', 'local', 'x', 'running', 'x')")
    finally:
        app.close()
```

（`sqlite3` の import が無ければファイル冒頭に追加する。`missions` は `store/db.py` の既存スキーマ — 書込を試みる対象テーブルは既存の任意のテーブルでよい。）

- [ ] **Step 11: 変異テスト**

1. `MissionSupervisor.try_submit` の `if self._busy:` チェックを削除 → `test_try_submit_rejects_when_busy` が red (2 つの trade job が両方受理されてしまう)
2. `_dispatch` の `reflection_result = self._reflection_fn()` 相当行 (`self._reflection_fn()` 呼び出し) を削除 → `test_trade_job_chains_reflection_after` が red
3. `scheduler.py` の `if accepted and reason == "cron":` を `if reason == "cron":` に戻す (busy でも前進させる) → `test_cron_deadline_only_advances_when_on_trade_mission_returns_true` が red
4. **(裁定書 F-7 追加)** `conn_supervisor = connect_readonly(...)` を `connect(...)` に戻す → `test_conn_supervisor_is_readonly` が red (書込が成功してしまう)
5. **(裁定書 F-3 追加)** `_run` の heartbeat ポンプ起動 (`pump.start()`) をコメントアウトする → `test_heartbeat_is_touched_during_long_dispatch` が red (`hb_during == hb_before`)
6. **(裁定書 F-3 advisor 指摘反映 追加)** `self.busy_since = time.monotonic()` の行を削除する → `test_busy_since_is_set_during_dispatch_and_cleared_after` が red (`sup.busy_since is None` のまま)

- [ ] **Step 12: Commit**

```bash
git add src/agentic_fx/core/supervisor.py src/agentic_fx/core/scheduler.py \
  src/agentic_fx/service.py tests/core/test_supervisor.py tests/core/test_scheduler.py
git commit -m "$(cat <<'EOF'
feat: mission supervisor スケルトン (容量1 try_submit + trade/reflection連鎖 + ask統一)

設計書 §3.3。lock の保持範囲は変えず (Task 15/16 で再設計)、Mission
呼び出しの主体を scheduler スレッドから専用の supervisor スレッドへ移す。

レビュー反映1回目 (裁定書 F-7 / IM-1 / P8-02): conn_supervisor を
connect_readonly (URI mode=ro) で構築し、Global Constraints の
「読取専用接続」をコードで強制する。

レビュー反映1回目 (裁定書 F-3 / CR-1): _dispatch 実行中 (trade+
reflection 連鎖で数百秒に及びうる) も heartbeat を touch し続ける
daemon ポンプスレッドを追加し、Task 19 watchdog の heartbeat_grace_sec
誤検知 (通常サイクルでサービス全体が自己停止する) を防ぐ。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 14: Executor snapshot API (commit-pre 外部取得 → commit-core 鮮度再検証 + N4-2 fail-closed 分岐)

設計書 §3.1「commit の 3 小相分割」と §12 申し送り①「N4-2」を実装する。**`handle_intent`/`_open`/`open_risk_and_notional` は完全に不変のまま残す** — バックテストエンジン (`backtest/runner.py:248`) と既存の `tests/core/test_executor.py` (数十本) が `handle_intent` を直接呼ぶ唯一の経路として使い続けるため。新設する `open_from_snapshot` はこれと**並行する別経路** (Task 15 で TradeLoop の Mission 経路だけが使う)。判定ロジック (`evaluate(intent, ctx, settings.risk)` 以降) は両経路で**完全に同一の共有コード**を通す (Global Constraints: risk_gate/kill_switch は diff ゼロ、executor は I/O 位置のみ移動)。

**スコープ決定 (レビュー反映 1 回目 — 裁定書 F-1 / CR-2 / P8-01)**: 執筆者の当初の独自裁定「snapshot API は OPEN のみ (CLOSE は各データソースの個別 timeout があるため相対的にリスクが小さい)」は**破棄する**。根拠が既定構成と食い違っていた — 実ファイル `sources.py` の `yf_quote`/`yf_bars` は `yfinance.download(...)` を **timeout 引数無し**で呼び (`mt5_quote`/`td_quote` の `timeout=10` と異なる)、`config/settings.yaml.example` の既定値は `yfinance.enabled: true` / `mt5.enabled: false` / `twelvedata.enabled: false` — **既定構成では yfinance が唯一有効な quote ソース**であり timeout が効かない。さらに `close_order` は `resolve_close_rate` 経由で `rate_fn` (換算レート取得、こちらも外部 I/O) も呼ぶため、CLOSE の未対策範囲は quote だけでなく換算レートにも及ぶ。設計書 §3.1 の commit-pre 契約 (Risk Gate/執行に要る全外部取得を lock 外で終える) は OPEN/CLOSE を区別しておらず、これが正である。

**本 task で CLOSE 用スナップショット API も追加する** (CANCEL は `broker.cancel` が DB 状態変更のみで外部 I/O を持たないため対象外・現状維持)。`Executor.close_order` (scheduler の SL/TP・day rollover 等が使う既存の lock 保持中 I/O 込み経路) 自体は**変更しない** — scheduler tick はもともと tick 全体で `core_lock` を保持する設計であり、この経路の I/O 位置移動は本 task のスコープ外 (CR-2/P8-01 が問題にしているのは Mission の commit-core からの呼び出しのみ)。`close_order` の「broker 成功後の pnl 計算・DB 遷移・activity 記録」部分を `_finish_close`/`_close_unknown` として抽出し、新設する `close_order_from_snapshot` (commit-core 専用、外部 I/O 不要) と共有させることで、判定・記録ロジックを 1 箇所に保ち diff ゼロに近い形で分岐させる。

**Files:**
- Modify: `src/agentic_fx/core/executor.py` (`_open` の末尾を `_evaluate_and_execute_open` へ抽出、`open_risk_and_notional_from_snapshot`/`ExecutionSnapshot`/`SnapshotCoverageError`/`gather_open_snapshot`/`open_from_snapshot` 新設。**追加 (裁定書 F-1): `close_order` の broker 成功後処理を `_finish_close`/`_close_unknown` へ抽出、`CloseSnapshot`/`gather_close_snapshot`/`close_order_from_snapshot` 新設**)
- Test: `tests/core/test_executor_snapshot.py` (新規)

**Interfaces:**
- Produces:
  - `executor.ExecutionSnapshot` (frozen dataclass): `quote: Quote, spec: InstrumentSpec, specs_by_pair: dict[str, InstrumentSpec], rates: dict[str, ConversionRate], captured_at: datetime` — `specs_by_pair`/`rates` は intent の pair **と commit-pre 時点の全 exposure pair** をカバーする
  - `executor.SnapshotCoverageError(Exception)` — commit-core 開始時点で必要な pair/通貨がスナップショットに無い (N4-2 — exposure が commit-pre 後に増えた) 場合の単一表現
  - `Executor.gather_open_snapshot(self, intent: TradeIntent, *, exposure_pairs: list[str]) -> ExecutionSnapshot` — **commit-pre 専用、core_lock 非保持で呼ぶ**。`exposure_pairs` は呼び出し元 (Task 15 の commit-pre 相) が `conn_supervisor` (lock 外の読取専用接続) から読んだ既存 exposure の pair 一覧
  - `executor.open_risk_and_notional_from_snapshot(conn, risk, snapshot: ExecutionSnapshot) -> tuple[float, float, int]` — `open_risk_and_notional` の DB-only 版。exposure 行の pair/通貨がスナップショットに無ければ `SnapshotCoverageError`
  - `Executor.open_from_snapshot(self, intent: TradeIntent, iid: int, snapshot: ExecutionSnapshot, *, max_snapshot_age_sec: float) -> dict` — **commit-core 専用、core_lock 保持中に呼ぶ**。①鮮度再検証 (`now - snapshot.captured_at > max_snapshot_age_sec` なら発注拒否、lock 内での再取得はしない) ②DB 状態読み直し (account/daily_start_equity/has_unresolved_unknown) ③`open_risk_and_notional_from_snapshot` (N4-2: `SnapshotCoverageError` も発注拒否) ④`GateContext` 確定 → `_evaluate_and_execute_open` へ委譲 (判定・執行ロジックは `_open` と完全共有)
  - `Executor._evaluate_and_execute_open(self, intent: TradeIntent, iid: int, ctx: GateContext) -> dict` (private — `_open`/`open_from_snapshot` の共有末尾。既存 `_open` の `result = evaluate(...)` 以降を**逐語**移動しただけで判定ロジックは 1 文字も変えない)
  - **(裁定書 F-1 / CR-2 / P8-01 追加)** `executor.CloseSnapshot` (frozen dataclass): `pair: str, price: float, spec: InstrumentSpec, rate: ConversionRate | None, rate_degraded: bool, captured_at: datetime`
    - **`pair` はレビュー 2 周目の追加 (2026-08-09)**。当初案には `pair` が無く、`close_order_from_snapshot` は `snapshot.price` / `snapshot.spec.contract_size` を**無検査**で使っていた。OPEN 経路は `risk_gate.py:51` が `intent.pair != ctx.quote.symbol or intent.pair != ctx.spec.symbol` で fail-closed するのに対し、**CLOSE 経路には等価な防御が無かった**。Task 15 は row を `conn_supervisor` から、intent の `order_id` を別途読むため取り違えは構造的に起こりうる。起きると**別銘柄の bid/ask でクローズし、別通貨のレートで `realized_pnl` を確定して DB に永久記録**する (例外も出ない)。`close_order_from_snapshot` の**先頭** (`S.CLOSING` へ遷移する前) で `snapshot.pair != row["pair"]` なら `SnapshotCoverageError` を送出し、`close_from_snapshot` が N4-2 分岐と同じ形で拒否に変換する
  - `Executor.gather_close_snapshot(self, row: dict) -> CloseSnapshot` — **commit-pre 専用、core_lock 非保持で呼ぶ**。`row` は呼び出し元 (Task 15) が `conn_supervisor` (lock 外の読取専用接続) から読んだ現在の order 行 (`pair`/`direction` を参照するだけ)。`quote_fn`(成行価格) → `spec_fn` → `resolve_close_rate`(`rate_fn` 経由) を 1 回で完了させる
  - `Executor.close_from_snapshot(self, intent: TradeIntent, iid: int, snapshot: CloseSnapshot | None, *, max_snapshot_age_sec: float) -> dict` — **commit-core 専用、core_lock 保持中に呼ぶ**。`_close` (369-384 行) と並行する別経路。①row 現況の再確認 ②`snapshot is None` なら lock 内取得せず拒否 ③鮮度再検証 ④`close_order_from_snapshot` へ委譲
  - `Executor.close_order_from_snapshot(self, row: dict, snapshot: CloseSnapshot, reason: str) -> OrderStatus` — `close_order` の commit-core 専用版。`spec_fn`/`resolve_close_rate` を一切呼ばない。`broker.close`(paper broker=DB書込、外部 I/O ではない) と DB 遷移のみ commit-core で行う。`_finish_close`/`_close_unknown` を `close_order` と共有 (判定・記録ロジック不変 — I/O 位置のみ移動)
  - `Executor._finish_close(self, row, price, contract_size, rate, degraded, reason, now) -> OrderStatus` / `Executor._close_unknown(self, row, now) -> OrderStatus` (private — `close_order`/`close_order_from_snapshot` の共有末尾。既存 `close_order` の broker 成功後処理を**逐語**移動しただけで判定ロジックは 1 文字も変えない)

- [ ] **Step 1: 失敗するテストを書く**

`tests/core/test_executor_snapshot.py` を新規作成する。

**現物照合済み (2026-08-09, 着手前検証)** — 既存 `tests/core/test_executor.py` の実際の姿は以下であり、
下記スケルトンはこれに合わせて**プレースホルダを除去済み**である。実装者はこれをほぼそのまま使えるが、
**必ず現物を読んでから**書くこと:

- ヘルパーは `_setup(tmp_path, broker=None, rate_fn=None) -> (conn, ex, state, mid)` の 1 本のみ。
  `_make_executor`/`_start_trade_mission`/`_insert_intent`/`_insert_open_order`/`_close_intent` は**存在しない** (本 Step で新規に書く)
- `_setup` は `quote_fn=lambda p: QUOTE` / `spec_fn=lambda p: SPEC` を**固定注入**する (`quote_fn`/`spec_fn` の
  差し替え引数は無い)。本 Step の新ファイルは `_setup` を import せず**独自の Executor 構築ヘルパーを持つ**
- `SPEC` は `InstrumentSpec(symbol=..., ...)`。**フィールド名は `symbol` であって `pair` ではない**
  (`contracts.py:103-115`)
- `_open_intent` の既定は `entry_type="limit"` → 結果は `"pending"`。`"opened"` を期待するなら
  `entry_type="market", limit_price=None, expires_in=None` を渡すこと
- 既存 `_rate_fn` は JPY 恒等 + USD→JPY のみ。EUR を使うなら拡張が要る

**テストスタブに「呼ばれたら raise」を使ってはならない** — `resolve_close_rate` (`executor.py:206-211`) と
`_evaluate_and_execute_open`/`close_order_from_snapshot` の broker 呼び出し (`executor.py:395-399`) は
どちらも `except Exception` で握り潰す。`AssertionError` も `Exception` なので**握り潰されてテストが緑になる**。
呼び出し検出は必ず**記録型スタブ** (list に append して正常値を返す) + `assert calls == []` で行う。

```python
"""Executor snapshot API (プラン8, 設計書 §3.1 / §12 申し送り①N4-2)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    ConversionRate, FixedClock, InstrumentSpec, Origin,
    OrderStatus as S, Quote, TradeIntent,
)
from agentic_fx.core.executor import (
    ExecutionSnapshot, Executor, SnapshotCoverageError, _intent_payload,
    open_risk_and_notional_from_snapshot,
)
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.store import intents as intents_store
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

# **pair ごとに異なる spec を返す** — 既存 test_executor.py の
# `spec_fn=lambda p: SPEC` (常に USDJPY spec) を流用すると、USDJPY だけで
# USD/JPY の両通貨が揃ってしまい、`gather_open_snapshot` の
# exposure_pairs 展開ループを丸ごと消しても `rates` の assert が緑になる。
# EURUSD に別の base/quote 通貨 (EUR/USD) を持たせて初めて
# 「EUR が rates に入るか」が展開ループを pin する assert になる。
SPECS = {
    "USDJPY": InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01,
                             contract_size=100_000,
                             base_currency="USD", quote_currency="JPY"),
    "EURUSD": InstrumentSpec(symbol="EURUSD", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01,
                             contract_size=100_000,
                             base_currency="EUR", quote_currency="USD"),
}
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


def _rate_fn(ccy, account_ccy, now):
    """JPY 恒等 / USD・EUR → JPY のみ供給する最小スタブ。"""
    if ccy == account_ccy:
        return ConversionRate(1.0, ccy, account_ccy, (now,))
    if account_ccy == "JPY" and ccy in ("USD", "EUR"):
        return ConversionRate(QUOTE.ask, ccy, "JPY", (now,))
    raise DataUnhealthy(f"no rate for {ccy}->{account_ccy}")


def _make_executor(tmp_path, *, quote_fn=None, spec_fn=None, rate_fn=None,
                   broker=None) -> Executor:
    """本ファイル専用の Executor 構築ヘルパー。

    既存 `tests/core/test_executor.py::_setup` は `quote_fn`/`spec_fn` を
    固定注入しており差し替えられないため、こちらを新設する。
    `tmp_path / "a"` のようなサブディレクトリを渡せるよう mkdir する。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    return Executor(
        conn=conn,
        broker=broker or PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        settings=SETTINGS, state_store=StateStore(tmp_path / "state.json"),
        activity=ActivityLog(tmp_path / "activity.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=FixedClock(NOW),
        quote_fn=quote_fn or (lambda p: QUOTE),
        spec_fn=spec_fn or (lambda p: SPECS[p]),
        rate_fn=rate_fn or _rate_fn)


def _start_trade_mission(conn) -> int:
    return missions.start(conn, "trade", "local", "m", NOW)


def _insert_intent(conn, mid: int, intent: TradeIntent) -> int:
    return intents_store.insert(conn, mid, _intent_payload(intent), NOW)


def _open_intent(pair="USDJPY", origin=Origin.SCHEDULER, **over) -> TradeIntent:
    """既定は **market** (即 OPEN になる) — 既存 test_executor.py の
    `_open_intent` は limit 既定 (結果は "pending") なので同名だが別物。"""
    d = {"action": "open", "pair": pair, "direction": "long",
         "entry_type": "market", "horizon": "day", "limit_price": None,
         "expires_in": None, "stop_loss": 148.00, "take_profit": 149.60,
         "reasoning": "t"}
    d.update(over)
    return TradeIntent.from_llm_dict(d, origin=origin)


def _close_intent(order_id: int) -> TradeIntent:
    return TradeIntent.from_llm_dict({"action": "close", "order_id": order_id},
                                     origin=Origin.SCHEDULER)


def _insert_open_order(conn, pair: str) -> dict:
    """Risk Gate を通さず OPEN の建玉行を直接作る (exposure の下ごしらえ
    専用)。gate 経由にすると EURUSD の pip_size/価格の組合せでサイズ計算が
    絡み、テストの意図 (exposure が存在すること) がぶれるため。"""
    oid = orders.insert(
        conn, pair=pair, direction="long", entry_type="market",
        horizon="day", status=S.OPEN, now=NOW,
        quantity=0.1, remaining_quantity=0.0, requested_price=148.20,
        avg_fill_price=148.20, filled_quantity=0.1, stop_loss=147.80,
        take_profit=149.00, filled_at=NOW.isoformat())
    return orders.get(conn, oid)


def test_gather_open_snapshot_covers_intent_pair_and_exposure_pairs(tmp_path):
    """gather_open_snapshot は intent.pair と exposure_pairs の両方の
    spec/通貨レートを含む。"""
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=["EURUSD"])

    assert "USDJPY" in snapshot.specs_by_pair
    assert "EURUSD" in snapshot.specs_by_pair
    assert "JPY" in snapshot.rates  # USDJPY の quote_currency
    assert "USD" in snapshot.rates  # USDJPY の base / EURUSD の quote
    # ↓ この 1 本だけが exposure_pairs 展開ループを pin する
    #   (EUR は EURUSD の base_currency からしか入らない)
    assert "EUR" in snapshot.rates


def test_open_from_snapshot_rejects_stale_snapshot(tmp_path):
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])
    stale_snapshot = snapshot.__class__(
        quote=snapshot.quote, spec=snapshot.spec,
        specs_by_pair=snapshot.specs_by_pair, rates=snapshot.rates,
        captured_at=snapshot.captured_at - timedelta(seconds=999))

    out = ex.open_from_snapshot(intent, iid, stale_snapshot,
                                max_snapshot_age_sec=5.0)
    assert out["result"] == "rejected"
    assert "stale" in out["reasons"][0]
    # 拒否は「発注しない」まで意味する — 行が 1 本も生まれていないこと
    assert orders.list_by_status(ex.conn, S.SUBMITTING, S.OPEN,
                                 S.PENDING_FILL) == []


def test_open_from_snapshot_rejects_when_exposure_grew_after_commit_pre(tmp_path):
    """N4-2: commit-pre と commit-core の間に新規 exposure が確定し、
    スナップショットに必要通貨が無い場合は lock 内取得せず intent 拒否。"""
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])  # EURUSD 未カバー

    # commit-pre 後・commit-core 前に EURUSD の建玉が確定した状況を模す
    _insert_open_order(ex.conn, pair="EURUSD")

    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert "snapshot" in out["reasons"][0].lower()


def test_open_from_snapshot_matches_handle_intent(tmp_path):
    """judgment ロジック不変の確認: 同じ intent/状況で handle_intent (ライブ
    経路) と open_from_snapshot (Mission 経路) が同じ結果になる。"""
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")  # 同一初期状態の別 DB
    intent = _open_intent(pair="USDJPY")   # market → "opened" を期待

    mid1 = _start_trade_mission(ex1.conn)
    out1 = ex1.handle_intent(intent, mid1)

    mid2 = _start_trade_mission(ex2.conn)
    iid2 = _insert_intent(ex2.conn, mid2, intent)
    snapshot = ex2.gather_open_snapshot(intent, exposure_pairs=[])
    out2 = ex2.open_from_snapshot(intent, iid2, snapshot,
                                  max_snapshot_age_sec=999.0)

    assert out1["result"] == out2["result"] == "opened"
    row1 = orders.get(ex1.conn, out1["order_id"])
    row2 = orders.get(ex2.conn, out2["order_id"])
    # サイズ・約定価格まで一致すること (「同じ result 文字列」だけでは
    # 判定ロジック共有の証明にならない)
    assert row1["quantity"] == row2["quantity"]
    assert row1["avg_fill_price"] == row2["avg_fill_price"]


def test_open_from_snapshot_matches_handle_intent_on_gate_rejection(tmp_path):
    """却下側も一致すること — kill switch ラッチ等の副作用込みで
    `_evaluate_and_execute_open` を両経路が共有していることの pin。"""
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")
    # gate が必ず落とす intent (stop_loss を極端に離してリスク超過にする)
    intent = _open_intent(pair="USDJPY", stop_loss=100.00)

    mid1 = _start_trade_mission(ex1.conn)
    out1 = ex1.handle_intent(intent, mid1)

    mid2 = _start_trade_mission(ex2.conn)
    iid2 = _insert_intent(ex2.conn, mid2, intent)
    snapshot = ex2.gather_open_snapshot(intent, exposure_pairs=[])
    out2 = ex2.open_from_snapshot(intent, iid2, snapshot,
                                  max_snapshot_age_sec=999.0)

    assert out1["result"] == out2["result"] == "rejected"
    assert out1["reasons"] == out2["reasons"]


def test_open_risk_and_notional_from_snapshot_raises_on_uncovered_pair(tmp_path):
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="EURUSD")
    empty_snapshot = ExecutionSnapshot(
        quote=None, spec=None, specs_by_pair={}, rates={},
        captured_at=datetime(2026, 8, 4, tzinfo=timezone.utc))
    with pytest.raises(SnapshotCoverageError):
        open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk,
                                             empty_snapshot)


def test_open_risk_and_notional_from_snapshot_raises_on_uncovered_currency(
        tmp_path):
    """spec はあるが通貨レートが欠けている場合も N4-2 として拒否する
    (`if spec is None` だけを見て通貨チェックを削っても落ちるように)。"""
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="EURUSD")
    snapshot = ExecutionSnapshot(
        quote=QUOTE, spec=SPECS["USDJPY"],
        specs_by_pair={"EURUSD": SPECS["EURUSD"]},
        rates={"USD": ConversionRate(1.0, "USD", "JPY", (NOW,))},  # EUR 欠落
        captured_at=NOW)
    with pytest.raises(SnapshotCoverageError):
        open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk,
                                             snapshot)


def test_open_risk_and_notional_from_snapshot_matches_live_version(tmp_path):
    """DB-only 版が既存 open_risk_and_notional と同じ数値を返すこと
    (集計ロジックの逐語移植の pin)。"""
    from agentic_fx.core.executor import open_risk_and_notional
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="USDJPY")
    _insert_open_order(ex.conn, pair="EURUSD")
    cycle_rate = ex.cycle_rate_fn(NOW)
    live = open_risk_and_notional(ex.conn, ex.spec_fn, ex.settings.risk,
                                  cycle_rate)
    intent = _open_intent(pair="USDJPY")
    snapshot = ex.gather_open_snapshot(intent,
                                       exposure_pairs=["USDJPY", "EURUSD"])
    snap = open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk,
                                                snapshot)
    assert live == snap


# ---- CLOSE snapshot (裁定書 F-1 / CR-2 / P8-01 — 独自裁定「OPEN のみ」を
# 破棄した反映) ---------------------------------------------------------

def test_gather_close_snapshot_captures_price_spec_and_rate(tmp_path):
    """gather_close_snapshot は quote_fn/spec_fn/resolve_close_rate を
    呼び、CloseSnapshot に price/spec/rate を確定する。"""
    ex = _make_executor(tmp_path)
    row = {"pair": "USDJPY", "direction": "long"}
    snapshot = ex.gather_close_snapshot(row)
    assert snapshot.price == QUOTE.bid       # long → bid
    assert snapshot.spec.symbol == "USDJPY"  # InstrumentSpec のフィールドは symbol
    assert snapshot.rate is not None
    assert snapshot.rate_degraded is False


def test_gather_close_snapshot_uses_ask_for_short(tmp_path):
    ex = _make_executor(tmp_path)
    snapshot = ex.gather_close_snapshot({"pair": "USDJPY",
                                         "direction": "short"})
    assert snapshot.price == QUOTE.ask


def test_close_order_from_snapshot_performs_no_external_io(tmp_path):
    """裁定書 F-1 の核心: close_order_from_snapshot は quote_fn/spec_fn/
    rate_fn を一切呼ばない (commit-core は取得済み値のみ使用)。

    **スタブは「呼ばれたら raise」ではなく「記録して正常値を返す」にする** —
    `resolve_close_rate` (executor.py:206-211) も broker 呼び出し
    (executor.py:395-399) も `except Exception` で握り潰すため、
    AssertionError を投げるスタブは静かに飲まれてテストが緑になる。
    """
    calls: list[str] = []

    def rec_quote_fn(pair):
        calls.append(f"quote_fn:{pair}")
        return QUOTE

    def rec_spec_fn(pair):
        calls.append(f"spec_fn:{pair}")
        return SPECS[pair]

    def rec_rate_fn(ccy, account_ccy, now):
        calls.append(f"rate_fn:{ccy}")
        return _rate_fn(ccy, account_ccy, now)

    # snapshot は commit-pre 相を模す健全な Executor で取得する
    healthy_ex = _make_executor(tmp_path / "pre")
    row = _insert_open_order(healthy_ex.conn, pair="USDJPY")
    snapshot = healthy_ex.gather_close_snapshot(row)

    # commit-core 相を模す Executor — 外部取得が起きたら calls に残る
    ex = _make_executor(tmp_path / "core", quote_fn=rec_quote_fn,
                        spec_fn=rec_spec_fn, rate_fn=rec_rate_fn)
    row2 = _insert_open_order(ex.conn, pair="USDJPY")
    ex.close_order_from_snapshot(row2, snapshot, reason="llm_close")

    assert calls == [], f"commit-core で外部取得が発生した: {calls}"
    assert orders.get(ex.conn, row2["id"])["status"] == S.CLOSED.value


def test_close_order_from_snapshot_matches_close_order_result(tmp_path):
    """判定・記録ロジック不変の確認: 同一 price/spec/rate を使えば
    close_order (lock保持中に自前で取得) と close_order_from_snapshot
    (取得済みスナップショットを使用) が同じ最終状態・pnl になる。"""
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")
    row1 = _insert_open_order(ex1.conn, pair="USDJPY")
    row2 = _insert_open_order(ex2.conn, pair="USDJPY")

    final1 = ex1.close_order(row1, price=QUOTE.bid, reason="llm_close")
    snapshot = ex2.gather_close_snapshot(row2)
    final2 = ex2.close_order_from_snapshot(row2, snapshot, reason="llm_close")

    assert final1 == final2 == S.CLOSED
    r1 = orders.get(ex1.conn, row1["id"])
    r2 = orders.get(ex2.conn, row2["id"])
    assert r1["realized_pnl"] == r2["realized_pnl"]
    assert r1["close_price"] == r2["close_price"]


def test_close_from_snapshot_rejects_stale_snapshot(tmp_path):
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_close_snapshot(row)
    stale = snapshot.__class__(
        price=snapshot.price, spec=snapshot.spec, rate=snapshot.rate,
        rate_degraded=snapshot.rate_degraded,
        captured_at=snapshot.captured_at - timedelta(seconds=999))

    out = ex.close_from_snapshot(intent, iid, stale, max_snapshot_age_sec=5.0)
    assert out["result"] == "rejected"
    assert "stale" in out["reasons"][0]
    # 拒否は「クローズしない」まで意味する
    assert orders.get(ex.conn, row["id"])["status"] == S.OPEN.value


def test_close_from_snapshot_rejects_when_snapshot_is_none(tmp_path):
    """裁定書 F-1: commit-pre 時点で row が未 OPEN (snapshot 取得をスキップ)
    だった場合、commit-core は lock 内で取得し直さず reject する。"""
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)

    out = ex.close_from_snapshot(intent, iid, None, max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert "snapshot" in out["reasons"][0].lower()
    assert orders.get(ex.conn, row["id"])["status"] == S.OPEN.value


def test_close_from_snapshot_closes_when_fresh(tmp_path):
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_close_snapshot(row)

    out = ex.close_from_snapshot(intent, iid, snapshot,
                                 max_snapshot_age_sec=999.0)
    assert out["result"] == "closed"
    assert orders.get(ex.conn, row["id"])["status"] == S.CLOSED.value
```

**実装者への注意 (着手前検証の結果)**:

- `orders.insert` の受け付けるキーワードは `store/orders.py` の `_OPTIONAL` 集合で決まる
  (未知キーは `ValueError`)。`_insert_open_order` の引数が弾かれたら `_OPTIONAL` を見て調整すること
- `_open_intent(stop_loss=100.00)` で本当に gate が落ちるかは実測で確認する。落ちなければ
  `test_open_from_snapshot_matches_handle_intent_on_gate_rejection` の intent を
  「確実に落ちる値」(例: 極端な数量になる SL 距離、または position cap 超過の下ごしらえ) に差し替える。
  **「落ちる想定だったが実は accepted だった」まま assert が通る形にしてはならない** —
  `out1["result"] == "rejected"` を必ず実測で確認する
- `TradeIntent.from_llm_dict` の必須キー・`expires_in` の受け付ける形は `contracts.py` の現物を見る

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_executor_snapshot.py -q
```

Expected: 全件 FAIL (`ImportError: cannot import name 'ExecutionSnapshot'`)。

- [ ] **Step 3: `executor.py` を実装**

**⚠ 本 Step のコードブロックは「構造」を示すものであり、逐語の貼り付け原稿ではない (2026-08-09 着手前検証)。**
プラン執筆時に**既存コメントが数箇所落ちている** — 下記ブロックを貼り付けると
「1 文字も変えない」と言いながら実際にはコメントを削除することになる。移動対象領域の
**既存コメントは 1 行残らず保存すること**:

| 現物の行 | 落ちているコメント |
|---|---|
| `executor.py:269-272` | `cycle_rate` を 1 判断内で固定する理由 (設計書 §5) |
| `executor.py:325-326` | broker 例外を「結果不明」扱いにする理由 (codex 2) |
| `executor.py:354` 行内 | `# paper: 保護は常に成功` |
| `executor.py:407-411` | クローズのレート degraded フォールバックの理由 (設計書 §5) |

**また `config/settings.yaml` / `.example` は本 task では触らない。** `snapshot_max_age_sec` の
config 化は Task 15 (`WorkerSettings`) の担当であり、Task 14 は `max_snapshot_age_sec` を
キーワード引数として受け取るだけ。**設定キー同期の規約は本 task には適用されない。**

import 節に `from dataclasses import dataclass` を追加する (現行 `executor.py` は `sqlite3`/`datetime`/`typing.Callable` のみ import しており `dataclass` が無い — 本 task で新設する `ExecutionSnapshot`/`SnapshotCoverageError`/`CloseSnapshot` はいずれも `@dataclass(frozen=True, slots=True)` を使うため必須)。

`open_risk_and_notional` (36-64 行) の直後に追加:

```python
@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """commit-pre 相が集めた Risk Gate 評価用の外部取得スナップショット
    (設計書 §3.1)。`captured_at` は commit-core の鮮度再検証が使う。"""
    quote: Quote
    spec: InstrumentSpec
    specs_by_pair: dict
    rates: dict
    captured_at: datetime


class SnapshotCoverageError(Exception):
    """commit-core 開始時点の exposure がスナップショットでカバーされて
    いない (設計書 §12 申し送り① N4-2 — commit-pre と commit-core の間に
    新規 exposure が確定した場合)。lock 内で再取得せず intent 拒否する。"""


def open_risk_and_notional_from_snapshot(
        conn: sqlite3.Connection, risk,
        snapshot: ExecutionSnapshot) -> tuple[float, float, int]:
    """`open_risk_and_notional` の DB-only 版 (commit-core 専用 — 外部
    I/O を一切行わない)。exposure 行の pair/通貨がスナップショットに
    無ければ `SnapshotCoverageError` (N4-2)。"""
    total_risk = total_notional = 0.0
    rows = orders.list_by_status(conn, *_EXPOSURE)
    for r in rows:
        pair = r["pair"]
        spec = snapshot.specs_by_pair.get(pair)
        if spec is None:
            raise SnapshotCoverageError(
                f"pair {pair!r} is not covered by the execution snapshot "
                "(exposure grew after commit-pre — N4-2)")
        if (spec.quote_currency not in snapshot.rates
                or spec.base_currency not in snapshot.rates):
            raise SnapshotCoverageError(
                f"currency for pair {pair!r} is not covered by the "
                "execution snapshot (exposure grew after commit-pre — N4-2)")
        rule = risk.pair_rules.get(pair)
        spread = rule.assumed_spread_pips * spec.pip_size if rule else 0.0
        entry = r["avg_fill_price"] or r["requested_price"] or 0.0
        qty = r["quantity"] or 0.0
        quote_rate = snapshot.rates[spec.quote_currency]
        total_risk += (abs(entry - (r["stop_loss"] or entry)) + spread) \
            * spec.contract_size * qty * quote_rate.value \
            + risk.commission_per_lot * qty
        base_rate = snapshot.rates[spec.base_currency]
        total_notional += qty * spec.contract_size * base_rate.value
    return total_risk, total_notional, len(rows)
```

`Executor` クラスに `gather_open_snapshot`/`open_from_snapshot`/`_evaluate_and_execute_open` を追加する。まず `_open` メソッド (**現物 256-365 行 — 照合済み**) を「外部取得部分」と「判定・執行部分」に分割する。**`result = evaluate(intent, ctx, self.settings.risk)` から末尾までを `_evaluate_and_execute_open` として切り出し** (中身は 1 文字も変えない — **296-365 行** (`result = evaluate(...)` は 296 行) をそのまま新メソッドへ移動する)、`_open` はその呼び出しに置き換える:

```python
    def _open(self, intent: TradeIntent, iid: int) -> dict:
        now = self.clock.now()
        quote = self.quote_fn(intent.pair)
        spec = self.spec_fn(intent.pair)
        account = accounting.current_account(self.conn, now)
        if account is None:
            reasons = ["no fresh account snapshot (fail closed)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        equity, hwm = account
        cycle_rate = self.cycle_rate_fn(now)
        try:
            risk_total, notional, count = open_risk_and_notional(
                self.conn, self.spec_fn, self.settings.risk, cycle_rate)
            quote_to_account = cycle_rate(spec.quote_currency)
            base_to_account = cycle_rate(spec.base_currency)
        except DataUnhealthy as e:
            reasons = [f"conversion rate unavailable: {safe_error_text(e)}"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        ctx = GateContext(
            quote=quote, spec=spec, equity=equity, hwm=hwm,
            daily_start_equity=accounting.daily_start_equity(self.conn, now),
            open_position_count=count, existing_risk_account=risk_total,
            existing_notional_account=notional,
            kill_switch_latched=self.state.load().kill_switch_latched,
            has_unresolved_unknown=has_unresolved_unknown(self.conn),
            account_currency=self.settings.account_currency,
            quote_to_account=quote_to_account,
            base_to_account=base_to_account,
            now=now)
        return self._evaluate_and_execute_open(intent, iid, ctx)

    def _evaluate_and_execute_open(self, intent: TradeIntent, iid: int,
                                   ctx: GateContext) -> dict:
        """`_open`/`open_from_snapshot` の共有末尾 (判定ロジック不変—
        Global Constraints: risk_gate は diff ゼロ)。既存 `_open` の
        `result = evaluate(...)` 以降を逐語移動しただけ。"""
        result = evaluate(intent, ctx, self.settings.risk)
        if not result.accepted:
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(result.reasons))
            if any("kill switch" in r and "latched" not in r
                   for r in result.reasons):
                self.state.update(kill_switch_latched=True)
                self.activity.write(Category.SYSTEM, "kill_switch_latched",
                                    "drawdown threshold hit — 新規停止 (解除は明示操作)")
            self.activity.write(Category.TRADE, "gate_rejected",
                                "; ".join(result.reasons)[:200], ref_id=str(iid))
            return {"result": "rejected", "order_id": None,
                    "reasons": result.reasons}

        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        is_market = intent.entry_type.value == "market"
        now = ctx.now
        oid = orders.insert(
            self.conn, pair=intent.pair, direction=intent.direction.value,
            entry_type=intent.entry_type.value, horizon=intent.horizon.value,
            status=S.SUBMITTING, now=now, intent_id=iid,
            client_order_id=f"afx-{iid}-{now.timestamp():.0f}",
            quantity=result.size.quantity,
            remaining_quantity=result.size.quantity,
            requested_price=result.entry_price,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit,
            expires_at=(now + timedelta(hours=intent.expires_in_h)).isoformat()
            if intent.expires_in_h else None)
        row = orders.get(self.conn, oid)
        try:
            br = self.broker.submit(row, entry_price=result.entry_price)
        except Exception as e:  # noqa: BLE001
            br = BrokerResult(status="unknown",
                              message=safe_error_text(e))
        if br.status == "rejected":
            transitions.transition(self.conn, oid, S.REJECTED, now)
            self.activity.write(Category.TRADE, "broker_rejected",
                                f"{intent.pair}", ref_id=str(oid))
            return {"result": "rejected", "order_id": oid,
                    "reasons": ["broker rejected"]}
        if br.status == "unknown":
            transitions.transition(self.conn, oid, S.SUBMIT_UNKNOWN, now)
            self.activity.write(Category.TRADE, "submit_unknown",
                                f"{intent.pair} — reconcile 待ち", ref_id=str(oid))
            self.notifier.send(f"[agentic-fx] 送信結果不明 #{oid} — "
                               "解決まで新規発注停止")
            return {"result": "unknown", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.SUBMITTED, now,
                               broker_order_id=br.broker_order_id,
                               broker_position_id=br.broker_position_id)
        if is_market:
            transitions.transition(self.conn, oid, S.PROTECTION_PENDING, now,
                                   avg_fill_price=result.entry_price,
                                   filled_quantity=result.size.quantity,
                                   remaining_quantity=0.0,
                                   filled_at=now.isoformat())
            transitions.transition(self.conn, oid, S.OPEN, now)
            self.activity.write(Category.TRADE, "order_opened",
                                f"{intent.pair} {intent.direction.value} "
                                f"{result.size.quantity}lot @{result.entry_price}",
                                ref_id=str(oid))
            return {"result": "opened", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.PENDING_FILL, now)
        self.activity.write(Category.TRADE, "limit_placed",
                            f"{intent.pair} {intent.direction.value} "
                            f"{result.size.quantity}lot @{result.entry_price}",
                            ref_id=str(oid))
        return {"result": "pending", "order_id": oid, "reasons": []}
```

**実装者への注意**: `_evaluate_and_execute_open` の `now = ctx.now` は元の `_open` にあった `now = self.clock.now()` (256 行) を `ctx.now` (既に `GateContext.now` に保存済みの同じ値) から取り直すことで、`open_from_snapshot` 側 (下記) が commit-core 開始時に確定した `now` と完全一致させる (2 回目の `clock.now()` 呼び出しによる微小なズレを避ける)。

`gather_open_snapshot`/`open_from_snapshot` を `Executor` クラスに追加する (`_open` の直後):

```python
    def gather_open_snapshot(self, intent: TradeIntent, *,
                             exposure_pairs: list[str]) -> ExecutionSnapshot:
        """commit-pre 相専用 (設計書 §3.1) — **core_lock を保持しない状態
        で呼ぶこと**。Risk Gate 評価に要る全外部取得 (quote + 全 exposure
        pair の instrument spec + 全 exposure 通貨の換算レート) を 1 回で
        完了させ、timestamp 付きスナップショットにする。

        `exposure_pairs` は呼び出し元 (commit-pre 相) が `conn_supervisor`
        (lock 外の読取専用接続) から読んだ既存 exposure の pair 一覧。
        """
        now = self.clock.now()
        quote = self.quote_fn(intent.pair)
        spec = self.spec_fn(intent.pair)
        cycle_rate = self.cycle_rate_fn(now)
        specs_by_pair: dict = {intent.pair: spec}
        currencies: set = {spec.quote_currency, spec.base_currency}
        for pair in exposure_pairs:
            pair_spec = self.spec_fn(pair)
            specs_by_pair[pair] = pair_spec
            currencies.add(pair_spec.quote_currency)
            currencies.add(pair_spec.base_currency)
        rates = {ccy: cycle_rate(ccy) for ccy in currencies}
        return ExecutionSnapshot(quote=quote, spec=spec,
                                 specs_by_pair=specs_by_pair, rates=rates,
                                 captured_at=now)

    def open_from_snapshot(self, intent: TradeIntent, iid: int,
                           snapshot: ExecutionSnapshot, *,
                           max_snapshot_age_sec: float) -> dict:
        """commit-core 相専用 (設計書 §3.1) — **core_lock 保持中に呼ぶ
        こと**。①スナップショットの鮮度再検証 (lock 内での再取得はしない)
        ②DB 状態を読み直して GateContext を確定 ③Risk Gate 判定・paper
        broker 執行は `_evaluate_and_execute_open` へ委譲 (`_open` と
        完全共有 — 判定ロジック不変)。
        """
        now = self.clock.now()
        age_sec = (now - snapshot.captured_at).total_seconds()
        if age_sec > max_snapshot_age_sec:
            reasons = [
                f"execution snapshot is stale ({age_sec:.1f}s > "
                f"{max_snapshot_age_sec}s) — rejecting rather than "
                "re-fetching while holding core_lock (設計書 §3.1)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        account = accounting.current_account(self.conn, now)
        if account is None:
            reasons = ["no fresh account snapshot (fail closed)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        equity, hwm = account

        try:
            risk_total, notional, count = open_risk_and_notional_from_snapshot(
                self.conn, self.settings.risk, snapshot)
        except SnapshotCoverageError as e:
            # N4-2: commit-pre と commit-core の間に新規 exposure が確定
            # した。lock 内で再取得せず intent 拒否 (次周期の判断へ送る)。
            reasons = [f"execution snapshot coverage error: {e}"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        spec = snapshot.specs_by_pair[intent.pair]
        ctx = GateContext(
            quote=snapshot.quote, spec=spec, equity=equity, hwm=hwm,
            daily_start_equity=accounting.daily_start_equity(self.conn, now),
            open_position_count=count, existing_risk_account=risk_total,
            existing_notional_account=notional,
            kill_switch_latched=self.state.load().kill_switch_latched,
            has_unresolved_unknown=has_unresolved_unknown(self.conn),
            account_currency=self.settings.account_currency,
            quote_to_account=snapshot.rates[spec.quote_currency],
            base_to_account=snapshot.rates[spec.base_currency],
            now=now)
        return self._evaluate_and_execute_open(intent, iid, ctx)
```

**(裁定書 F-1 / CR-2 / P8-01 追加) CLOSE 用スナップショット API を追加する**。まず既存 `close_order` (**現物 386-433 行 — 照合済み。369 行は `_close` であってプラン旧記載の「369-433」は誤り**) の「broker 成功後の pnl 計算・DB 遷移・activity 記録」部分を `_finish_close`/`_close_unknown` として抽出する (中身は逐語移動 — 判定・記録ロジックは 1 文字も変えない)。`close_order` 自体はこの 2 メソッドを呼ぶ形に変わるが、**scheduler が使う経路としての外部から見える挙動は完全不変** (`spec_fn`/`resolve_close_rate` は引き続き `close_order` の中で呼ぶ — scheduler tick は tick 全体で `core_lock` を保持する既存設計のままであり、本 task はここを変えない):

```python
    def _close_unknown(self, row: dict, now: datetime) -> S:
        """close_order/close_order_from_snapshot 共有 (broker 応答が
        'ok' でない場合の後処理)。"""
        transitions.transition(self.conn, row["id"], S.CLOSE_UNKNOWN, now)
        self.activity.write(Category.TRADE, "close_unknown",
                            f"{row['pair']} — reconcile 待ち",
                            ref_id=str(row["id"]))
        self.notifier.send(f"[agentic-fx] クローズ結果不明 #{row['id']}")
        return S.CLOSE_UNKNOWN

    def _finish_close(self, row: dict, price: float, contract_size: float,
                      rate: ConversionRate | None, degraded: bool,
                      reason: str, now: datetime) -> S:
        """close_order/close_order_from_snapshot 共有 (broker 成功後の
        pnl 計算・DB 遷移・activity 記録 — 既存 close_order の当該部分を
        **逐語**移動しただけで判定ロジックは 1 文字も変えない)。"""
        pnl = compute_pnl(
            row, price, contract_size=contract_size,
            commission_per_lot=self.settings.risk.commission_per_lot,
            quote_to_account_rate=rate.value) if rate is not None else None
        transitions.transition(self.conn, row["id"], S.CLOSED, now,
                               close_price=price, realized_pnl=pnl,
                               closed_at=now.isoformat())
        pnl_text = f"{pnl:.0f}" if pnl is not None else "degraded(unresolved)"
        self.activity.write(Category.TRADE, "order_closed",
                            f"{row['pair']} pnl={pnl_text} reason={reason}",
                            ref_id=str(row["id"]))
        if degraded:
            self.activity.write(
                Category.TRADE, "close_pnl_rate_degraded",
                f"{row['pair']}: 換算レート取得不能 — " + (
                    "最後の健全レートで計算 (次回同期で吸収)" if pnl is not None
                    else "realized_pnl 未確定 (次回同期で解消)"),
                ref_id=str(row["id"]))
            self.notifier.send(
                f"[agentic-fx] クローズ換算レート degraded #{row['id']}")
        return S.CLOSED

    def close_order(self, row: dict, price: float, reason: str) -> S:
        """裁量クローズ・SL/TP・強制クローズ共通の決定論的クローズ経路
        (scheduler の SL/TP・day rollover 等が使う — lock 保持中に自前で
        spec_fn/resolve_close_rate を呼ぶ既存動作は不変。**snapshot 版は
        close_order_from_snapshot** — Mission の commit-core から使う)。
        結果不明は closed 扱いにしない (設計書 §12)。snapshot (mark-to-
        market) は scheduler が記録する。戻り値は遷移後の状態
        (S.CLOSED / S.CLOSE_UNKNOWN) — cancel_order と対称。"""
        now = self.clock.now()
        spec = self.spec_fn(row["pair"])
        transitions.transition(self.conn, row["id"], S.CLOSING, now,
                               close_reason=reason)
        try:
            br = self.broker.close(row, price, reason)
        except Exception as e:  # noqa: BLE001 — 結果不明として扱う (codex 2)
            br = BrokerResult(status="unknown", message=safe_error_text(e))
        if br.status != "ok":
            return self._close_unknown(row, now)
        # 設計書 §5: クローズはレート欠損でも妨げない。現在レートが取れなければ
        # 最後に健全性検証を通ったレートへ degraded フォールバックする。
        rate, degraded = self.resolve_close_rate(spec.quote_currency, now)
        return self._finish_close(row, price, spec.contract_size, rate,
                                  degraded, reason, now)

    def gather_close_snapshot(self, row: dict) -> CloseSnapshot:
        """commit-pre 相専用 (裁定書 F-1 / CR-2 / P8-01) — **core_lock
        非保持で呼ぶこと**。CLOSE 実行に要る quote (成行価格) +
        instrument spec + 換算レートを 1 回で取得し timestamp 付き
        スナップショットにする。`row` は呼び出し元 (Task 15 の commit-pre
        相) が `conn_supervisor` (lock 外の読取専用接続) から読んだ現在の
        order 行 (`pair`/`direction` を参照するだけ)。"""
        now = self.clock.now()
        quote = self.quote_fn(row["pair"])
        price = quote.bid if row["direction"] == "long" else quote.ask
        spec = self.spec_fn(row["pair"])
        rate, degraded = self.resolve_close_rate(spec.quote_currency, now)
        return CloseSnapshot(price=price, spec=spec, rate=rate,
                             rate_degraded=degraded, captured_at=now)

    def close_order_from_snapshot(self, row: dict, snapshot: CloseSnapshot,
                                  reason: str) -> S:
        """close_order の commit-core 専用版 (裁定書 F-1 / CR-2 / P8-01)
        — `spec_fn`/`resolve_close_rate` を一切呼ばない (外部 I/O ゼロ)。
        price/spec/rate は commit-pre で取得済みの `CloseSnapshot` を使う
        (`broker.close` は paper broker の DB 書込であり外部 I/O ではない
        ため commit-core に残す)。`_finish_close`/`_close_unknown` を
        `close_order` と共有 — 判定・記録ロジックは `close_order` と完全
        共有 (I/O 位置のみ移動)。"""
        now = self.clock.now()
        transitions.transition(self.conn, row["id"], S.CLOSING, now,
                               close_reason=reason)
        try:
            br = self.broker.close(row, snapshot.price, reason)
        except Exception as e:  # noqa: BLE001
            br = BrokerResult(status="unknown", message=safe_error_text(e))
        if br.status != "ok":
            return self._close_unknown(row, now)
        return self._finish_close(row, snapshot.price,
                                  snapshot.spec.contract_size, snapshot.rate,
                                  snapshot.rate_degraded, reason, now)

    def close_from_snapshot(self, intent: TradeIntent, iid: int,
                            snapshot: "CloseSnapshot | None", *,
                            max_snapshot_age_sec: float) -> dict:
        """commit-core 相専用 (裁定書 F-1 / CR-2 / P8-01) — **core_lock
        保持中に呼ぶこと**。①DB 状態を読み直して row の現況を確定
        (commit-pre 後に状態が変わっていないか確認) ②`snapshot` が None
        (commit-pre 時点で row が OPEN でなく取得をスキップした場合、また
        は commit-pre 後に OPEN へ遷移した稀なケース) なら lock 内で
        取得し直さず reject する ③鮮度再検証 (lock 内での再取得はしない)
        ④`close_order_from_snapshot` へ委譲。"""
        now = self.clock.now()
        row = orders.get(self.conn, intent.order_id)
        if row is None or row["status"] != S.OPEN.value:
            reasons = [f"order {intent.order_id} is not open"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        if snapshot is None:
            reasons = [
                "close snapshot unavailable (order was not open at "
                "commit-pre time) — rejecting rather than re-fetching "
                "while holding core_lock (設計書 §3.1)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        age_sec = (now - snapshot.captured_at).total_seconds()
        if age_sec > max_snapshot_age_sec:
            reasons = [
                f"close snapshot is stale ({age_sec:.1f}s > "
                f"{max_snapshot_age_sec}s) — rejecting rather than "
                "re-fetching while holding core_lock (設計書 §3.1)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        final = self.close_order_from_snapshot(row, snapshot, reason="llm_close")
        result = "closed" if final == S.CLOSED else "unknown"
        return {"result": result, "order_id": row["id"], "reasons": []}
```

`open_risk_and_notional_from_snapshot` の直前 (`ExecutionSnapshot`/`SnapshotCoverageError` の直後) に `CloseSnapshot` を追加する:

```python
@dataclass(frozen=True, slots=True)
class CloseSnapshot:
    """commit-pre 相が集めた CLOSE 用の外部取得スナップショット (裁定書
    F-1 / CR-2 / P8-01)。`captured_at` は commit-core の鮮度再検証が使う。"""
    price: float
    spec: InstrumentSpec
    rate: ConversionRate | None
    rate_degraded: bool
    captured_at: datetime
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_executor_snapshot.py -q
uv run pytest tests/core/test_executor.py -q
uv run pytest -q
```

Expected: 全件 PASS。**`tests/core/test_executor.py` が 1 本も壊れていないこと** (`handle_intent`/`_open`/`close_order`/`cancel_order` の外部から見える挙動は完全不変) を必ず確認する — 壊れていれば `_evaluate_and_execute_open`/`_finish_close`/`_close_unknown` への切り出しで何かを取りこぼしている。

- [ ] **Step 5: 変異テスト**

1. `_evaluate_and_execute_open` の `if not result.accepted:` を削除 → `tests/core/test_executor.py` の gate 却下系テストが red (`_open`/`open_from_snapshot` 両方に波及することを確認)
2. `open_from_snapshot` の `if age_sec > max_snapshot_age_sec:` を削除 → `test_open_from_snapshot_rejects_stale_snapshot` が red
3. `open_risk_and_notional_from_snapshot` の `if spec is None:` チェックを削除 → `test_open_from_snapshot_rejects_when_exposure_grew_after_commit_pre`/`test_open_risk_and_notional_from_snapshot_raises_on_uncovered_pair` が red
3b. `open_risk_and_notional_from_snapshot` の**通貨カバレッジ**チェック (`if spec.quote_currency not in snapshot.rates ...`) だけを削除 (spec チェックは残す) → `test_open_risk_and_notional_from_snapshot_raises_on_uncovered_currency` が red
3c. `gather_open_snapshot` の `for pair in exposure_pairs:` ループ本体を空にする → `test_gather_open_snapshot_covers_intent_pair_and_exposure_pairs` の `"EUR" in snapshot.rates` が red (**pair 非依存の `spec_fn` を使うとここが緑のまま生存する** — 新テストファイルが `SPECS[p]` を使う理由)
4. **(裁定書 F-1 追加)** `close_order_from_snapshot` の `snapshot.rate, snapshot.rate_degraded` の使用を `self.resolve_close_rate(snapshot.spec.quote_currency, now)` の直接呼び出しに戻す (外部 I/O を再導入する変異) → `test_close_order_from_snapshot_performs_no_external_io` が red。
   **記録型スタブでなければこの変異は生存する** — `resolve_close_rate` は `except Exception` で全例外を握り潰すので、`rate_fn` に raise するスタブを入れても degraded 扱いになるだけでテストは緑のまま通る (2026-08-09 着手前検証で確認)
4b. `close_order_from_snapshot` の `snapshot.price` を `self.quote_fn(row["pair"]).bid` に戻す → 同テストが red (`quote_fn` の記録が残る)
5. `close_from_snapshot` の `if snapshot is None:` チェックを削除 → `test_close_from_snapshot_rejects_when_snapshot_is_none` が red
6. `close_from_snapshot` の `if age_sec > max_snapshot_age_sec:` を削除 → `test_close_from_snapshot_rejects_stale_snapshot` が red
7. `open_from_snapshot` / `close_from_snapshot` の拒否 return を「拒否するが処理は続行」に変える (return を消す) → 「拒否は発注/クローズしないことまで含む」を pin する assert (`orders.list_by_status(...) == []` / `status == open`) が red

**リストは下限。** この task が守ろうとしている性質は「**commit-core で外部 I/O が一切起きないこと**」と
「**判定ロジックが `_open`/`open_from_snapshot` で完全に共有されていること**」の 2 点である。
この 2 点を壊す変異を自分で追加し、red になるかを確かめよ。特に
「`_evaluate_and_execute_open` の中身を片方の経路にだけ effect のある形に変える」変異
(例: `intents_store.set_gate_result` の呼び出しを消す) が両経路のテストで red になるかを確認すること。
**リストに無い変異を追加したら、その内容と結果 (KILLED/SURVIVED) を必ず報告せよ。**

- [ ] **Step 6: Commit**

```bash
git add src/agentic_fx/core/executor.py tests/core/test_executor_snapshot.py
git commit -m "$(cat <<'EOF'
feat: Executor snapshot API (commit-pre外部取得 + commit-core鮮度再検証 + N4-2 fail-closed)

設計書 §3.1 / §12 申し送り①。handle_intent/_open は完全不変のまま
(バックテスト runner.py の既存経路)、open_from_snapshot を並行追加する。
判定ロジックは _evaluate_and_execute_open として両経路が共有する。

レビュー反映1回目 (裁定書 F-1 / CR-2 / P8-01): CLOSE 用の CloseSnapshot /
gather_close_snapshot / close_order_from_snapshot / close_from_snapshot も
追加。既定構成 (yfinance) では quote timeout が効かず CLOSE の外部 I/O が
commit-core (core_lock 保持中) に残ると SL/TP 監視全体が止まるため、
執筆者の独自裁定「snapshot API は OPEN のみ」を破棄し OPEN と同一契約で
commit-pre スナップショット化する。close_order (scheduler 用、既存動作
不変) と close_order_from_snapshot は _finish_close/_close_unknown を
共有し判定・記録ロジックは 1 文字も変えない。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 15: TradeLoop 五相再構成 (prepare/run/commit-pre/commit-core/commit-post) — 「Mission 実行中も SL/TP 監視継続」の本体

設計書 §3.1 の五相分解を `TradeLoop` に実装する。**この task が本プランの核心** — `core_lock` を保持するのは prepare (missions.start/signals.claim/prompt 構築) と commit-core (consume/Risk Gate/執行/finish) のみにし、`run` 相 (`WorkerRunner.run(mission)`) は lock を一切保持しない。これにより scheduler tick (SL/TP 監視) が Mission 実行中も core_lock を取得でき、資金保護が止まらなくなる。`ask` (`_ask_once_impl`) も同じ理由 (WorkerRunner 呼び出しが長時間ブロックしうる) で三相 (prepare/run/commit) に再構成する。

**設計上の注記 (writing-plans — Phase 1 の fail-closed 修正との関係)**: Phase 1 レジャーの「W1 (finish 失敗の fail-closed 化)」は「`missions.finish` が失敗しても completed 結果のまま intent 変換・execute へ進んでしまう」という**無警告の**バグを修正したものだった。本 task の設計書 §3.1 は `missions.finish(CAS)` を commit-core の**末尾** (paper broker 執行の後) に置く — 一見 W1 と逆行するように見えるが、以下 2 点により安全性は後退しない: ①`orders.insert`/`trade_intents` への記録は `missions.finish` と独立した書込みであり、finalize が失敗しても発注自体の監査証跡 (どの注文がどう約定したか) は失われない ②finalize 書込み自体が例外を出せば `_finalize_mission` が `mission_finalize_failed` を activity に**可視化して記録**し (W1 が塞いだ「無警告」の再発は無い)、mission 行は `running` のまま残るが次回起動時の `recover_interrupted` (Task 11) が `interrupted` へ確実に回収する。W1 が防いだのは「無警告のまま実行し続ける」ことであり、本 task はそれを維持したまま commit-core の末尾に finalize を置く設計書の順序をそのまま採用する。

**(Task 14 レビュー 1 周からの申し送り — 本 task で必ず回収すること) `notifier.send` は commit-core から到達可能なブロッキング・ネットワーク I/O である。**

`Notifier.send` (`core/notifier.py:16-26`) は `enabled` 時に **`urllib.request.urlopen(req, timeout=10)` を同期実行**する。Task 14 が commit-core 専用に新設した経路から、以下 3 箇所に到達できる (2026-08-09 指揮者が現物で確認):

| `executor.py` の行 | 経路 | 発火条件 |
|---|---|---|
| `_evaluate_and_execute_open` 内 | `open_from_snapshot` → 委譲 | `broker.submit` が例外/unknown |
| `_close_unknown` 内 | `close_order_from_snapshot` → 委譲 | `broker.close` が例外/非 ok |
| `_finish_close` の degraded 分岐 | `close_order_from_snapshot` → 委譲 | commit-pre で換算レート取得に失敗し `rate_degraded=True` |

**つまり Task 14 の「commit-core で外部 I/O ゼロ」は happy-path でのみ成立しており、障害時 (broker 例外・レート取得失敗) には最大 10 秒 × 呼び出し回数だけ `core_lock` を保持したままブロックしうる。** これは本プランの核心 (Mission 実行中も SL/TP 監視を止めない) を、よりによって**障害時に**裏切る。

Task 14 で直さなかった理由: `_finish_close`/`_close_unknown`/`_evaluate_and_execute_open` は `close_order`/`_open` (scheduler・バックテストが使う既存経路) と**共有**されており、ここを触ると Task 14 の絶対条件「既存経路の挙動は完全不変」に反するため。**commit-post 相を持つ本 task が正しい回収先である。**

回収方法 (本 task で実装すること): **commit-core 中の通知は送らずにキューへ積み、commit-post 相 (lock 解放後) でまとめて送る。** `Executor` に `notifier` を直接持たせるのをやめ、commit-core では「送るべき通知」をリストに溜めて返し、呼び出し元 (五相の commit-post) が送る形にする。既存経路 (`close_order` 等) は従来どおり即時送信でよい (scheduler tick は元から tick 全体で lock を持つ設計)。

**pin の張り方**: `Notifier(enabled=True)` かつ webhook URL をローカルの遅い stub に向けた Executor を作り、**commit-core 相の実行中に `notifier.send` が 1 度も呼ばれないこと**を記録型スタブで確認する。Task 14 のテストは `Notifier(enabled=False, ...)` 固定だったためこの経路を一度も踏んでいない (レビューで判明)。

---

## ⚠ 着手前検証の結果 (2026-08-09、指揮者が現物照合) — **必ず最初に読むこと**

### (1) 申し送り 3 件が「散文にはあるが Step のコードには無い」

上の申し送り 3 件は**理由と方針だけが書かれていて、Step 3/5 の逐語コードには一切反映されていない**。
それどころか **Step 5 のコードは、申し送りが「これではダメだ」と言っている形そのまま**である:

```python
except Exception as e:  # noqa: BLE001
    snapshot_error = e      # → commit-core で raise → 汎用 intent_execution_failed
```

Step をなぞるだけの実装者は **3 件とも no-op のまま出荷し、しかも「逸脱なし」と正直に報告する**。
本改訂で 3 件すべてを Step の中に具体化した (Step 3 / **Step 3.5 (新設)** / Step 5)。

### (2) 参照行のドリフト (Task 14 で `executor.py` が 487→801 行に変わったため)

| プラン旧記載 | 現物 | 備考 |
|---|---|---|
| `executor.py` `handle_intent` **215-252** | **285-322** | |
| `executor.py` `_close` **369-384** | **604-619** | |
| `executor.py` `_cancel` **461-473** | **775-787** | |
| `service.py` `_trade_fn`/`_ask_fn` **359-380** | **480-491** | |
| `service.py` `TradeLoop(...)` 構築 **348-352** | **464-471** | |
| `service.py` `core_lock = threading.RLock()` **357** | **473** | **順序関係は不変** — `core_lock` (473) は `trade_loop` 構築 (464) より**後**なので、移動の指示は依然として有効 |

**`trade_loop.py` と `tests/loops/test_trade_loop.py` の参照行は現物と一致している** (Task 14 で触っていないため)。

### (3) Step 5 のコードブロックは既存コメントを落としている

Task 14 と同じ問題。**移動対象領域の既存コメントは 1 行残らず保存すること。**
下記は現物にあって Step 5 のブロックから消えているもの:

| 現物の行 | 落ちているコメント |
|---|---|
| `trade_loop.py:101-103` | `②missions.start(trigger="signal")` — NULL 窓を作らない理由 |
| `trade_loop.py:108` | `③claim_oldest — 失敗なら LLM を起こさず即 finalize` |
| `trade_loop.py:117-127` | `fix round 1 F2 (codex)` — 曖昧窓と `reclaim_expired` の役割 |
| `trade_loop.py:138` | `④set_trigger ⑤プロンプトへシグナル行を注入` |
| `trade_loop.py:164-167` | `⑥consume/requeue の確定規則` |
| `trade_loop.py:186-188` | `finally` の requeue 規則 |

### (4) `_run_recorded` 削除で失われる挙動 — **ask 経路の扱いを明記する**

現物の `_run_recorded` (260-297) は `missions.finish` が失敗したとき
**`result` を `MissionResult("failed", ...)` に差し替える**。これにより呼び出し元は
completed 系の分岐に進まない (Phase 1 レジャー W1 の fail-closed)。
新設する `_finalize_mission` はこの差し替えを**行わない**。

- **trade 経路**: 上の「設計上の注記」が意図的な変更として扱っており、そのまま採用する
- **ask 経路**: `_finalize_mission` は `result.status != "completed"` の判定より**前**に呼ばれるため、
  finish が失敗しても**回答文字列がそのまま返る** (従来は `"(Mission 失敗: ...)"` だった)。
  **ask は読み取り専用で資金に影響しないため、この変更は許容する** —
  ただし `mission_finalize_failed` の activity 記録は残るので無警告にはならない。
  **この判断を実装者が独自に変えないこと。**

---

**Files:**
- Modify: `src/agentic_fx/core/executor.py` (`handle_intent` を `record_and_validate_intent` + dispatch に分割、`_close`→`close_intent`/`_cancel`→`cancel_intent` の公開昇格 + **拒否分岐の activity 対称化**。**追加 (Task 14 申し送り): commit-core 経路の通知を commit-post へ遅延させる — 機構は Step 3.5 で確定**)
- Modify: `src/agentic_fx/loops/trade_loop.py` (全体 — 五相再構成)
- Modify: `src/agentic_fx/service.py:480-491` (**現物照合済み — 旧記載 359-380 はドリフト**) (`_trade_fn`/`_ask_fn` の `with core_lock:` 除去 + `TradeLoop` construction に `core_lock`/`conn_supervisor` を渡す。**追加 (裁定書 F-6 / CR-5): `healthcheck_provider = PriceProvider(conn_supervisor, settings, clock, readonly=True)` を新設し `TradeLoop(provider=healthcheck_provider, ...)` に渡す**)
- Modify: `src/agentic_fx/config.py` (`WorkerSettings` に `snapshot_max_age_sec` 追加)
- Modify: `config/settings.yaml.example` (同期)
- Modify: `tests/loops/test_trade_loop.py:31-69` (`_loop` fixture に `core_lock`/`conn_supervisor` 追加。**現物と一致 — ドリフトなし**)
- Test: `tests/loops/test_trade_loop_phases.py` (新規 — lock 境界の直接検証。**追加 (Task 14 申し送り): commit-core 中に `notifier.send` が呼ばれないことの pin**)

**(Task 14 レビュー 2 周からの申し送り — 本 task で必ず回収すること) commit-pre の外部取得失敗が「記録済みの gate 拒否」にならない。**

`_open` (ライブ経路) はレート障害を `except DataUnhealthy` で捕まえ、**`intents_store.set_gate_result(accepted=False)` + `gate_rejected` activity** という**記録済みの拒否**に変換する。一方 `gather_open_snapshot` は `cycle_rate_fn(now)` の `DataUnhealthy` を捕まえず送出するだけで、この分岐が snapshot 経路のどこにも存在しない (2026-08-09 `/code-review` が検出、指揮者が現物照合)。

既定構成では yfinance が唯一有効な quote/rate ソースなので、**1 通貨のレート取得失敗 (または `cycle_rate_fn` の全体 skew 検証の発火) で commit-pre が例外を送出する経路は現実的に起こる。** そのとき本 task の設計では `snapshot_error` として commit-core へ持ち越し、広い `except` で `intent_execution_failed` にするだけなので、**`trade_intents.gate_result` は NULL のまま・`gate_rejected` activity も残らない** — ライブ経路との監査証跡の非対称が生じる。

Task 14 で直さなかった理由: 記録には `iid` が要り、`iid` を持つのは**呼び出し元 (= 本 task の commit-pre 相)** であって `gather_open_snapshot` ではない。**本 task で、commit-pre のスナップショット取得失敗を `_open` と同じ形の「記録済み gate 拒否」に変換すること。**

**(Task 14 レビュー 1 周からの申し送り — 本 task で併せて回収)** `close_from_snapshot` の拒否 3 分岐 (not-open / snapshot None / stale) は `set_gate_result` のみで **`activity.write` を書かない**。`open_from_snapshot` の拒否分岐がすべて `gate_rejected` を activity に記録するのと非対称である。既存 `_close` も同じ非対称を持つため Task 14 では**プラン記述通りに据え置いた** (直すと既存経路に波及するため)。本 task で `close_intent` 公開昇格を行う際に、**両経路の拒否分岐すべてに `activity.write(Category.TRADE, "gate_rejected", ...)` を入れて対称化すること。** 運用時に「なぜ LLM の close 指示が実行されなかったか」を activity ログだけで追えるようにする。

**Interfaces:**
- Produces:
  - `Executor.record_and_validate_intent(self, intent: TradeIntent, mission_id: int) -> tuple[int, dict | None]` — intent の DB 記録 (常に行う) + HOLD 短絡 + origin/loop 検証。`(iid, None)` なら呼び出し側が `open_from_snapshot`/`close_from_snapshot`/`cancel_intent` へ dispatch する。`(iid, result)` なら `result` がそのまま最終結果 (hold・origin_rejected・mission_rejected)。**`handle_intent` はこのメソッド + dispatch を連結しただけで挙動は完全不変** (バックテスト runner.py 経由の既存呼び出しに影響なし)
  - `Executor.close_intent`/`Executor.cancel_intent` — 旧 `_close`/`_cancel` の公開昇格 (rename のみ、挙動不変。`handle_intent` 内の呼び出しも新名に更新)。**`close_from_snapshot`/`gather_close_snapshot`/`close_order_from_snapshot` は Task 14 で追加済み — 本 task は TradeLoop からこれらを呼ぶ配線のみ行う**
  - `TradeLoop.__init__(self, *, conn, runner, settings, executor, provider, econ, policy, activity, notifier, clock, watch=None, core_lock: threading.RLock, conn_supervisor: sqlite3.Connection)` — **`core_lock`/`conn_supervisor` が新設必須 kwarg**。**(裁定書 F-6 / CR-5) `provider` は呼び出し元が `conn_supervisor` (RO) で構築した `PriceProvider(readonly=True)` を渡すこと** — `TradeLoop` 内部で `self.provider` を使うのは lock 外の `healthcheck` 呼び出しだけであり、書込可能な `conn_core` 版を渡すと healthcheck 内の `get_bars`→`upsert_bars` が無保護で `conn_core` を書き込む (Global Constraints 違反)
  - `TradeLoop._finalize_mission(self, mid: int, result: MissionResult) -> None` — **呼び出し元が `core_lock` を保持している前提**。`missions.finish` の CAS 化された呼び出し (書き込み例外は fail closed、CAS 失敗 = 二重終端は警告のみ)
  - `TradeLoop._read_exposure_pairs(self) -> list[str]` — commit-pre 専用。`conn_supervisor` (lock 外) から既存 exposure (`executor._EXPOSURE` の全状態) の pair 一覧を読む
  - `TradeLoop._read_close_row(self, order_id: int) -> dict | None` — **(裁定書 F-1 / CR-2 追加)** commit-pre 専用。`conn_supervisor` (lock 外) から CLOSE 対象の order 行を読む (`gather_close_snapshot` の入力)

- [ ] **Step 1: 失敗するテストを書く (lock 境界の直接検証)**

`tests/loops/test_trade_loop_phases.py` を新規作成する (`tests/loops/test_trade_loop.py` の `_loop` フィクスチャを import して使う — Step 8 で `_loop` を更新した後にこのテストを書くこと。TDD の都合上、先に Step 8 を実施してから本 Step に戻ってもよい):

```python
"""TradeLoop 五相再構成の lock 境界 (プラン8, 設計書 §3.1)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.runners.base import Mission, MissionResult
from tests.loops.test_trade_loop import _loop


class _SlowRunner:
    """runner.run() が core_lock 非保持で呼ばれることを検証する fake —
    run() の中で「別スレッドが core_lock を取得できるか」を確認する。"""

    def __init__(self, core_lock: threading.RLock, result: MissionResult) -> None:
        self._core_lock = core_lock
        self._result = result
        self.lock_was_free_during_run = False

    def run(self, mission: Mission) -> MissionResult:
        acquired = self._core_lock.acquire(blocking=False)
        if acquired:
            self.lock_was_free_during_run = True
            self._core_lock.release()
        return self._result


def test_run_once_does_not_hold_core_lock_during_runner_run(tmp_path):
    """設計書 §3.1: run 相 (runner.run) は core_lock を保持しない —
    別スレッド (ここでは runner.run 自身の中) が同じロックを取得できる
    ことで検証する。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    slow = _SlowRunner(loop._core_lock, MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, []))
    loop.runner = slow

    loop.run_once("cron")

    assert slow.lock_was_free_during_run is True


def test_scheduler_tick_can_acquire_lock_while_worker_runner_blocks(tmp_path):
    """統合的な確認: run_once を別スレッドで実行中、メインスレッドが
    core_lock を (scheduler tick が行うのと同じ形で) 取得できる。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    release = threading.Event()

    class BlockingRunner:
        def run(self, mission):
            release.wait(5.0)
            return MissionResult("completed",
                                 {"action": "hold", "reasoning": "x"}, [])

    loop.runner = BlockingRunner()
    t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
    t.start()
    time.sleep(0.1)  # run_once が prepare を終えて run 相に入るまで待つ

    acquired = loop._core_lock.acquire(timeout=1.0)
    assert acquired, "core_lock は run 相の間、他スレッドから取得できるはず"
    loop._core_lock.release()

    release.set()
    t.join(timeout=5.0)
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/loops/test_trade_loop_phases.py -q
```

Expected: FAIL (`TypeError: TradeLoop.__init__() missing ... 'core_lock'` 等 — `_loop` フィクスチャ更新前は別のエラーになりうる。Step 8 の fixture 更新を先に済ませてから本 Step を実施する運用でもよい)。

- [ ] **Step 3: `executor.py` の `record_and_validate_intent` + rename**

`src/agentic_fx/core/executor.py` の `handle_intent` (**現物 285-322 行 — 照合済み**) を以下に置き換える:

```python
    def record_and_validate_intent(self, intent: TradeIntent,
                                   mission_id: int) -> tuple[int, dict | None]:
        """intent の DB 記録 (常に行う) + HOLD 短絡 + origin/loop 検証
        (プラン8 五相再構成 — `handle_intent` から分割。判定ロジックは
        1 文字も変えていない)。

        戻り値: `(iid, None)` なら呼び出し側が open_from_snapshot/
        close_intent/cancel_intent へ dispatch する。`(iid, result)` なら
        `result` がそのまま最終結果 (hold・origin_rejected・
        mission_rejected のいずれか)。
        """
        now = self.clock.now()
        iid = intents_store.insert(self.conn, mission_id,
                                   _intent_payload(intent), now)
        if intent.action is Action.HOLD:
            self.activity.write(Category.AGGREGATE, "hold",
                                intent.reasoning[:120], ref_id=str(iid))
            return iid, {"result": "hold", "order_id": None, "reasons": []}

        if intent.origin is not Origin.SCHEDULER:
            reasons = ["origin rejected: only scheduler missions may trade"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(reasons))
            self.activity.write(Category.TRADE, "origin_rejected",
                                f"{intent.action.value} from {intent.origin.value}",
                                ref_id=str(iid))
            return iid, {"result": "rejected", "order_id": None, "reasons": reasons}

        loop = missions_store.loop_of(self.conn, mission_id)
        if loop != "trade":
            reasons = [f"mission rejected: loop={loop!r} is not a trade mission"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(reasons))
            self.activity.write(Category.TRADE, "mission_rejected",
                                f"{intent.action.value} from mission "
                                f"#{mission_id} (loop={loop!r})", ref_id=str(iid))
            return iid, {"result": "rejected", "order_id": None, "reasons": reasons}

        return iid, None

    def handle_intent(self, intent: TradeIntent, mission_id: int) -> dict:
        """既存の全経路 (バックテスト runner.py・tests/core/test_executor.py)
        向けの一体化エントリ。`record_and_validate_intent` + dispatch を
        連結しただけで挙動は完全不変。"""
        iid, early = self.record_and_validate_intent(intent, mission_id)
        if early is not None:
            return early
        if intent.action is Action.OPEN:
            return self._open(intent, iid)
        if intent.action is Action.CLOSE:
            return self.close_intent(intent, iid)
        return self.cancel_intent(intent, iid)
```

`_close` (**現物 604-619 行**) を `close_intent` に、`_cancel` (**現物 775-787 行**) を `cancel_intent` に rename する。`_close`/`_cancel` を呼んでいた他の箇所が無いことを `grep -n "self\._close(\|self\._cancel(" src/agentic_fx/core/executor.py` で確認する (`handle_intent` からの呼び出しは上で更新済み)。

**さらに (Task 14 レビュー 1 周からの申し送りの回収 — 散文だけで Step に無かった分):
拒否分岐の `activity.write` を対称化する。** 現状、`open_from_snapshot` の拒否分岐は
すべて `gate_rejected` を activity に書くのに、CLOSE/CANCEL 側は `set_gate_result` しか
書かない。運用時に「なぜ LLM の close 指示が実行されなかったか」が activity ログから
追えない。**以下 4 箇所すべてに追加する** (いずれも既存の `set_gate_result` の直後):

```python
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
```

| メソッド | 分岐 |
|---|---|
| `close_intent` (旧 `_close`) | `row is None or row["status"] != S.OPEN.value` |
| `cancel_intent` (旧 `_cancel`) | `row is None or row["status"] != S.PENDING_FILL.value` |
| `close_from_snapshot` | `snapshot is None` |
| `close_from_snapshot` | 鮮度切れ (stale) |

**`close_from_snapshot` の `SnapshotCoverageError` 捕捉分岐は Task 14 で既に
`activity.write` を持っている** — 重複して追加しないこと (現物を確認してから書く)。

**pin の張り方**: `tests/core/test_executor_snapshot.py` の既存の拒否テスト
(`test_close_from_snapshot_rejects_when_snapshot_is_none` /
`test_close_from_snapshot_rejects_stale_snapshot`) に
`assert any("gate_rejected" in l for l in ex.activity.tail(n=50, category=Category.TRADE))`
を足す。**追加後、`activity.write` の行を消す変異で red になることを実測すること。**

- [ ] **Step 3.5 (新設 — Task 14 申し送りの回収): commit-core の通知を commit-post へ遅延させる**

**機構は「Executor 側の遅延フラグ + drain」に確定する (2026-08-09 指揮者裁定)。**
戻り値で通知を返す案は採らない — `_finish_close`/`_close_unknown`/`_evaluate_and_execute_open` は
`close_order`/`_open` (scheduler・バックテスト経路) と**共有**されており、Task 14 が 3 周かけて
「逐語不変」として pin した契約を変えてしまうため。フラグ方式なら**共有メソッドのシグネチャは
1 文字も変わらず、scheduler 経路は既定で従来どおり即時送信**になる。

`Executor.__init__` に追加:

```python
        # commit-core 相 (core_lock 保持中) の通知を溜める先。None のとき
        # は即時送信 (scheduler・バックテスト経路の既定)。設計書 §3.1 /
        # Task 14 申し送り: Notifier.send は urlopen(timeout=10) の同期
        # 実行なので、commit-core で呼ぶと core_lock を握ったまま最大 10 秒
        # ブロックし、SL/TP 監視が止まる。
        self._deferred_notifications: list[str] | None = None
```

`Executor` にメソッドを追加:

```python
    def _notify(self, text: str) -> None:
        """通知の単一出口。遅延中なら溜めるだけ、そうでなければ即時送信。"""
        if self._deferred_notifications is not None:
            self._deferred_notifications.append(text)
            return
        self.notifier.send(text)

    @contextmanager
    def defer_notifications(self):
        """commit-core 相専用 — この中の通知は送らずに溜め、`with` を抜けた
        後に呼び出し元 (commit-post 相) が送る。

        **スレッド安全性の根拠**: `Executor` は Mission スレッドと scheduler
        スレッドで共有されるが、**commit-core も scheduler tick も
        `core_lock` を保持している間しか executor を触らない**
        (`service.py:592` の `with app.core_lock: app.scheduler.tick(...)`)。
        したがって遅延窓と tick は相互排他であり、scheduler の通知が
        Mission の遅延リストに紛れ込むことはない。
        **この不変条件が崩れると通知が別 Mission に付け替わる** ので、
        `core_lock` 非保持でこのコンテキストに入ってはならない。
        """
        assert self._deferred_notifications is None, \
            "defer_notifications is not re-entrant"
        pending: list[str] = []
        self._deferred_notifications = pending
        try:
            yield pending
        finally:
            self._deferred_notifications = None
```

import 節に `from contextlib import contextmanager` を追加する。

**既存の `self.notifier.send(...)` 4 箇所をすべて `self._notify(...)` に置き換える**
(現物で行番号を確認してから — 2026-08-09 時点は下表):

| 現物の行 | メソッド | 到達する commit-core 経路 |
|---|---|---|
| 420 | `_evaluate_and_execute_open` | `open_from_snapshot` |
| 573 | `_close_unknown` | `close_order_from_snapshot` |
| 600 | `_finish_close` (degraded 分岐) | `close_order_from_snapshot` |
| 772 | `cancel_order` | `cancel_intent` |

**`grep -c "self.notifier.send" src/agentic_fx/core/executor.py` が 0 になること**を確認する
(`self.notifier` は `_notify` の中だけで使われる)。

**pin の張り方** (Task 14 のテストは `Notifier(enabled=False)` 固定でこの経路を一度も
踏んでいなかった — レビューで判明):

`tests/core/test_executor_snapshot.py` に**記録型スタブ**で 1 本追加する。
「呼ばれたら raise」は `except Exception` に飲まれるので使わないこと:

```python
class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)
```

- **遅延中**: `with ex.defer_notifications() as pending:` の中で
  `close_order_from_snapshot` を broker 失敗させて呼び、
  **`notifier.sent == []` かつ `pending` が 1 件**であること
- **遅延外 (scheduler 経路の非退行)**: 同じ失敗を `close_order` (既存経路) で起こし、
  **`notifier.sent` が 1 件**であること (= 既定は即時送信のまま)

**変異**: `_notify` の遅延分岐 (`if self._deferred_notifications is not None:`) を
削除して常に即時送信に戻す → 上の 1 本目が red になることを実測する。

- [ ] **Step 4: `config.py`/`settings.yaml.example` に `snapshot_max_age_sec` を追加**

`src/agentic_fx/config.py` の `WorkerSettings` (Task 7 で新設済み) に追加:

```python
    # commit-pre で取得したスナップショットの許容鮮度 (秒)。commit-core
    # 開始時にこれを超えていれば発注拒否する (lock 内での再取得はしない
    # — 設計書 §3.1)。
    snapshot_max_age_sec: float = Field(gt=0, default=10.0)
```

`config/settings.yaml.example` の `worker:` セクションに追加:

```yaml
  snapshot_max_age_sec: 10        # commit-pre スナップショットの許容鮮度 (秒)。超過は発注拒否 (lock内再取得しない)
```

- [ ] **Step 5: `trade_loop.py` を五相再構成**

`src/agentic_fx/loops/trade_loop.py` の import 節に `import threading` と `import sqlite3` (既存) を確認し、`from agentic_fx.core.contracts import Action, OrderStatus as S` を追加する (既存 import は `Clock, IntentParseError, Origin, TradeIntent` のみなので `Action`/`OrderStatus as S` を足す — `Action.OPEN`/`Action.CLOSE` の分岐、および裁定書 F-1 (CR-2) の commit-pre CLOSE 分岐が `row["status"] == S.OPEN.value` を参照するために両方必要)。

`TradeLoop.__init__` (42-58 行) を以下に変更する:

```python
class TradeLoop:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 settings: Settings, executor: Executor,
                 provider: PriceProvider, econ: EconCalendar, policy: Policy,
                 activity: ActivityLog, notifier: Notifier,
                 clock: Clock, core_lock: threading.RLock,
                 conn_supervisor: sqlite3.Connection,
                 watch: MissionWatch | None = None) -> None:
        self.conn = conn
        self.runner = runner
        self.settings = settings
        self.executor = executor
        self.provider = provider
        self.econ = econ
        self.policy = policy
        self.activity = activity
        self.notifier = notifier
        self.clock = clock
        self._core_lock = core_lock
        self._conn_supervisor = conn_supervisor
        self.watch = watch if watch is not None else MissionWatch()
```

`_run_once_impl` (80-190 行 — **現物と一致**) を以下に置き換える。

**⚠ このブロックは構造の指示であり逐語の貼り付け原稿ではない。** 上の着手前検証 (3) に挙げた
既存コメント 6 箇所が落ちているので、**移動対象領域の既存コメントは 1 行残らず保存すること。**
置き換え後に `git diff` を読み、消えたコメントが無いことを自分で確認せよ:

```python
    def _run_once_impl(self, trigger: str = "cron") -> dict | None:
        """取引判断 Mission 1 回分 (プラン8 五相再構成 — 設計書 §3.1)。

        prepare (lock 保持) → run (lock 非保持) → commit-pre (lock 非保持:
        結果検証・intent 構築・snapshot 取得) → commit-core (lock 保持:
        consume・Risk Gate・執行・finish) → commit-post (lock 非保持:
        aggregate 記録)。`healthcheck` は prepare よりさらに前 (lock 外)
        — claim 済みで healthcheck 死亡 → requeue 漏れ、を構造的に防ぐ
        (既存の設計方針を維持)。**(裁定書 F-6 / CR-5) `self.provider` は
        `conn_supervisor` (RO 接続) で構築した `readonly=True` の
        `PriceProvider` — `healthcheck` 内部の `get_bars` が呼ぶ
        `ohlcv.upsert_bars` は `readonly=True` によりスキップされるため、
        lock 外のこの呼び出しが `conn_core` を無保護で書き込むことはない
        (Global Constraints 違反の解消)。**
        """
        try:
            self.provider.healthcheck(self.settings.pairs[0])
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        # ---- prepare (core_lock 保持) ----
        claimed: dict | None = None
        mid: int | None = None
        with self._core_lock:
            now = self.clock.now()
            if trigger == "signal":
                mid = missions.start(self.conn, "trade",
                                     self.settings.runner.trade.backend,
                                     self.settings.runner.trade.model, now,
                                     trigger="signal")
                try:
                    claimed = signals.claim_oldest(
                        self.conn, mission_id=mid, now=now,
                        freshness_bars=self.settings.plugin.signal_freshness_bars)
                    if claimed is None:
                        missions.finish(self.conn, mid, "skipped", None, [], now)
                        return None
                except Exception:
                    try:
                        missions.finish(self.conn, mid, "failed", None, [], now)
                    except Exception:  # noqa: BLE001 — 元の例外を握りつぶさない
                        _log.exception(
                            "failed to finalize mission %s after claim error", mid)
                    raise
            if claimed is not None:
                missions.set_trigger(self.conn, mid, f"signal:{claimed['plugin']}")
                prompt = (self._build_prompt(load_prompt("trade_mission"))
                         + self._format_signal_injection(claimed))
            else:
                prompt = self._build_prompt(load_prompt("trade_mission"))
            mission = self._build_mission(prompt)
            if mid is None:
                mid = missions.start(self.conn, "trade",
                                     self.settings.runner.trade.backend,
                                     self.settings.runner.trade.model, now,
                                     trigger=trigger)

        consumed = False
        try:
            # ---- run (core_lock 非保持) ----
            self.watch.begin(mid, "trade", mission.timeout_sec)
            try:
                result = self.runner.run(mission)
                if not isinstance(result, MissionResult):
                    result = MissionResult("failed", None, [])
            except Exception:  # noqa: BLE001 — runner 例外で周期を殺さない
                _log.exception("runner raised")
                result = MissionResult("failed", None, [])
            finally:
                self.watch.end(mid)

            # ---- commit-pre (core_lock 非保持) ----
            if result.status != "completed":
                with self._core_lock:
                    self._finalize_mission(mid, result)
                self.activity.write(Category.AGGREGATE, "mission_failed",
                                    f"runner status={result.status}",
                                    ref_id=str(mid))
                self.notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}")
                return None
            try:
                intent = TradeIntent.from_llm_dict(result.output,
                                                   origin=Origin.SCHEDULER)
            except IntentParseError as e:
                with self._core_lock:
                    self._finalize_mission(mid, result)
                self.activity.write(Category.AGGREGATE, "intent_parse_failed",
                                    str(e), ref_id=str(mid))
                return None

            open_snapshot = None
            close_snapshot = None
            snapshot_error: Exception | None = None
            if intent.action is Action.OPEN:
                exposure_pairs = self._read_exposure_pairs()
                try:
                    open_snapshot = self.executor.gather_open_snapshot(
                        intent, exposure_pairs=exposure_pairs)
                except Exception as e:  # noqa: BLE001 — commit-core で
                    # 執行失敗として扱う (fail closed、consume より前に
                    # 確定させておき commit-core 側の分岐を単純にする)。
                    snapshot_error = e
            elif intent.action is Action.CLOSE:
                # 裁定書 F-1 (CR-2/P8-01): CLOSE の quote/spec/close-rate も
                # commit-pre (lock 非保持) で取得する。row が commit-pre
                # 時点で OPEN でなければ snapshot 取得自体をスキップし
                # (無駄な外部 I/O を避ける)、commit-core の
                # close_from_snapshot が fresh な状態を読み直して reject
                # する (snapshot=None のときの契約 — lock 内では取得し
                # 直さない)。
                row = self._read_close_row(intent.order_id)
                if row is not None and row["status"] == S.OPEN.value:
                    try:
                        close_snapshot = self.executor.gather_close_snapshot(row)
                    except Exception as e:  # noqa: BLE001
                        snapshot_error = e

            # ---- commit-core (core_lock 保持) ----
            # **通知は commit-post まで遅延させる (Step 3.5)** — Notifier.send は
            # urlopen(timeout=10) の同期実行なので、ここで送ると core_lock を
            # 握ったまま最大 10 秒ブロックし SL/TP 監視が止まる。
            with self._core_lock, \
                    self.executor.defer_notifications() as deferred:
                if claimed is not None:
                    # ⑥consume/requeue の確定規則: パース成功の時点で
                    # consume する (プロンプトに実際に載せた Mission が
                    # 確定できる)。executor 実行後の requeue は二重発注
                    # ハザードになるため、ここで確定させる。
                    signals.consume(self.conn, claimed["id"], mission_id=mid,
                                    now=self.clock.now())
                    consumed = True
                try:
                    iid, early = self.executor.record_and_validate_intent(
                        intent, mid)
                    if snapshot_error is not None:
                        # **(Task 14 レビュー 2 周からの申し送りの回収)**
                        # commit-pre の外部取得失敗を、ライブ経路 (`_open` の
                        # `except DataUnhealthy`) と**同じ形の「記録済み gate
                        # 拒否」**に変換する。ここを汎用の
                        # `intent_execution_failed` にすると
                        # `trade_intents.gate_result` が NULL のまま残り、
                        # 「なぜ発注されなかったか」を DB から追えない。
                        #
                        # `record_and_validate_intent` を**先に**呼ぶのは
                        # `iid` を得るため (intent の記録自体は常に行う契約)。
                        # early (hold/origin/mission 拒否) が確定している
                        # ケースはそちらを優先する — snapshot は使わない。
                        if early is not None:
                            out = early
                        else:
                            reasons = ["execution snapshot unavailable: "
                                       + safe_error_text(snapshot_error)]
                            intents_store.set_gate_result(
                                self.conn, iid, accepted=False,
                                reject_reason=reasons[0])
                            self.activity.write(Category.TRADE, "gate_rejected",
                                                reasons[0], ref_id=str(iid))
                            out = {"result": "rejected", "order_id": None,
                                   "reasons": reasons}
                    elif early is not None:
                        out = early
                    elif intent.action is Action.OPEN:
                        out = self.executor.open_from_snapshot(
                            intent, iid, open_snapshot,
                            max_snapshot_age_sec=self.settings.worker.snapshot_max_age_sec)
                    elif intent.action is Action.CLOSE:
                        out = self.executor.close_from_snapshot(
                            intent, iid, close_snapshot,
                            max_snapshot_age_sec=self.settings.worker.snapshot_max_age_sec)
                    else:
                        out = self.executor.cancel_intent(intent, iid)
                except Exception as e:  # noqa: BLE001
                    _log.exception("executor intent handling raised")
                    self._finalize_mission(mid, result)
                    self.activity.write(Category.AGGREGATE,
                                        "intent_execution_failed",
                                        safe_error_text(e), ref_id=str(mid))
                    # **`self.notifier.send` をここで呼んではならない** —
                    # まだ core_lock 保持中であり、urlopen(timeout=10) で
                    # 最大 10 秒ブロックする。commit-post まで遅延させ、
                    # `return` せずに with を抜けてから送る (Step 3.5)。
                    deferred.append(
                        f"[agentic-fx] 注文処理失敗: {safe_error_text(e)}")
                    out = None
                else:
                    self._finalize_mission(mid, result)

            # ---- commit-post (core_lock 非保持) ----
            # commit-core で溜めた通知をここで送る (Step 3.5)。送信失敗で
            # 本流を殺さない — 通知は保守処理であり、発注結果は既に確定済み。
            # **失敗経路 (out is None) もここを必ず通る** — commit-core から
            # 直接 return すると通知が送られないまま捨てられる。
            for _text in deferred:
                try:
                    self.notifier.send(_text)
                except Exception:  # noqa: BLE001 — 通知失敗で本流を止めない
                    _log.exception("deferred notification failed")
            if out is None:
                return None
            self.activity.write(Category.AGGREGATE, "decision",
                                f"{intent.action.value} -> {out['result']}",
                                ref_id=str(mid))
            return out
        finally:
            # 例外時も claimed のまま残さない (lease 回収を待たず即 requeue)。
            # consume 済みならここでは何もしない (二重発注ハザードを避ける)。
            # 裁定書 F-6 (CR-5): _requeue_signal は signals.requeue 経由で
            # conn_core を書き込むため、core_lock を保持中に呼ぶ (Global
            # Constraints 違反の解消 — この finally はメソッド全体の
            # try に対するものであり、commit-core の with ブロックは
            # 例外伝播時点で既に解放済みなので RLock の再取得は安全)。
            if claimed is not None and not consumed:
                with self._core_lock:
                    self._requeue_signal(claimed)

    def _finalize_mission(self, mid: int, result: MissionResult) -> None:
        """missions.finish の CAS 化された呼び出し (**core_lock 保持中に
        呼ぶこと**)。設計書 §4.7 codex C-4: 二重終端は上書きせず警告のみ
        残す。書き込み自体の例外は fail closed (旧 `_run_recorded` の
        契約を維持)。"""
        try:
            finished = missions.finish(self.conn, mid, result.status,
                                       result.output, result.transcript,
                                       self.clock.now())
        except Exception:  # noqa: BLE001
            _log.exception("missions.finish failed for %s", mid)
            try:
                self.activity.write(Category.SYSTEM, "mission_finalize_failed",
                                    f"mid={mid}")
            except Exception:  # noqa: BLE001
                _log.exception("failed to record mission_finalize_failed")
            return
        if not finished:
            try:
                self.activity.write(
                    Category.SYSTEM, "mission_finalize_conflict",
                    f"mid={mid} (already finalized elsewhere)")
            except Exception:  # noqa: BLE001
                _log.exception("failed to record mission_finalize_conflict")

    def _read_exposure_pairs(self) -> list[str]:
        """commit-pre 専用: `conn_supervisor` (lock 外の読取専用接続) から
        既存 exposure (`executor._EXPOSURE` の全状態) の pair 一覧を読む。
        `gather_open_snapshot` の `exposure_pairs` に渡す。"""
        from agentic_fx.core.executor import _EXPOSURE
        from agentic_fx.store import orders as orders_store
        rows = orders_store.list_by_status(self._conn_supervisor, *_EXPOSURE)
        return sorted({r["pair"] for r in rows})

    def _read_close_row(self, order_id: int) -> dict | None:
        """commit-pre 専用 (裁定書 F-1 / CR-2): `conn_supervisor` (lock 外の
        読取専用接続) から CLOSE 対象の order 行を読む。`gather_close_snapshot`
        の入力にするだけで、commit-core は改めて `self.conn` (conn_core) から
        読み直す (commit-pre 後に状態が変わっていないかの再確認 — 設計書
        §3.1 の鮮度契約と同じ精神)。"""
        from agentic_fx.store import orders as orders_store
        return orders_store.get(self._conn_supervisor, order_id)
```

**実装者への注意**: `_build_mission`/`_format_signal_injection`/`_requeue_signal`/`_build_prompt`/`_safe_report_boundary_failure` (192-320 行付近の残りのメソッド群) は無変更のため掲載を省略する — 削除しないこと。`_run_recorded` メソッド (260-297 行) は本 task で `_finalize_mission` に置き換わり、`_run_once_impl`/`_ask_once_impl` (次 Step) の両方が独自に run+commit の相を持つため**削除する** (どこからも呼ばれなくなることを `grep -n "_run_recorded" src/agentic_fx/loops/trade_loop.py` で確認する)。

- [ ] **Step 6: `_ask_once_impl` を三相再構成**

`_ask_once_impl` (233-256 行) を以下に置き換える:

```python
    def _ask_once_impl(self, question: str) -> str:
        """ask Mission (プラン8 三相再構成 — trade と同じ理由: WorkerRunner
        呼び出しが長時間ブロックしうるため lock を保持しない)。"""
        with self._core_lock:
            now = self.clock.now()
            prompt = self._build_prompt(load_prompt("ask_mission")) \
                + f"\n\n## ユーザーの質問\n{question}"
            mission = Mission(
                prompt=prompt, tools=_TRADE_TOOLS,
                output_schema=ANSWER_SCHEMA,
                max_turns=self.settings.llama_swap.max_turns,
                timeout_sec=self.settings.llama_swap.timeout_sec)
            mid = missions.start(self.conn, "ask",
                                 self.settings.runner.trade.backend,
                                 self.settings.runner.trade.model, now)

        self.watch.begin(mid, "ask", mission.timeout_sec)
        try:
            result = self.runner.run(mission)
            if not isinstance(result, MissionResult):
                result = MissionResult("failed", None, [])
        except Exception:  # noqa: BLE001
            _log.exception("runner raised")
            result = MissionResult("failed", None, [])
        finally:
            self.watch.end(mid)

        with self._core_lock:
            self._finalize_mission(mid, result)

        if result.status != "completed":
            return f"(Mission 失敗: {result.status})"
        if not isinstance(result.output, dict):
            return "(Mission 失敗: completed)"
        answer = result.output.get("answer")
        if not isinstance(answer, str):
            return "(Mission 失敗: completed)"
        self.activity.write(Category.AGGREGATE, "ask_answered",
                            question[:80], ref_id=str(mid))
        return answer
```

- [ ] **Step 7: テスト実行して PASS を確認**

```bash
uv run pytest tests/loops/test_trade_loop_phases.py -q
```

Expected: まだ FAIL (`_loop` fixture 未更新 — 次 Step で更新する)。

- [ ] **Step 8: `tests/loops/test_trade_loop.py` の `_loop` fixture を更新**

`_loop` 関数 (31-69 行) の `TradeLoop(...)` 構築 (61-68 行) に `core_lock`/`conn_supervisor` を追加する:

```python
    loop = TradeLoop(
        conn=conn, runner=runner, settings=SETTINGS,
        executor=executor, provider=provider, econ=econ,
        policy=Policy(policy_path),
        activity=activity_log,
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=clock, core_lock=threading.RLock(), conn_supervisor=conn,
        watch=watch)
```

（`conn_supervisor=conn` — テストでは同一 DB への 2 本目の実接続を作らず既存 `conn` をそのまま渡してよい (単体テストの関心は exposure pairs 読み取りロジックであり、真の多接続並行性ではない)。ファイル冒頭の import 節に `import threading` を追加する。）

```bash
uv run pytest tests/loops/test_trade_loop.py -q
uv run pytest tests/loops/test_trade_loop_phases.py -q
uv run pytest tests/loops/test_trade_loop_signal.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 9: `service.py` を実装 (lock ラップ除去 + 配線更新)**

`build_app` 内、Task 13 で作った `_trade_fn`/`_ask_fn` (`with core_lock:` で包んでいた版) を以下に置き換える (`_reflection_fn` は **Task 16 まで `with core_lock:` を維持する** — ReflectionCycle はまだ五相再構成されていないため):

```python
    def _trade_fn(trigger: str):
        # プラン 8 (Task 15): TradeLoop 自身が prepare/commit-core で
        # core_lock を保持する五相構造になったため、ここでは lock を
        # 掴まない (二重取得は RLock で技術的には安全だが、run 相の
        # 間ずっと lock を保持したままになり Task 15 の目的を無効化する)。
        return trade_loop.run_once(trigger)

    def _reflection_fn():
        # ReflectionCycle は Task 16 で五相再構成するまで、呼び出し全体
        # を lock で包む現状維持。
        with core_lock:
            return reflection.run_pending()

    def _ask_fn(question: str) -> str:
        # プラン 8 (Task 15): TradeLoop.ask_once も三相構造になったため
        # ここでは lock を掴まない。
        return trade_loop.ask_once(question)
```

`TradeLoop(...)` の構築 (**現物 464-471 行**) に `core_lock=core_lock, conn_supervisor=conn_supervisor` を追加する (`conn_supervisor` は Task 13 で `App`/`build_app` に既に構築済み)。**(裁定書 F-6 / CR-5) `provider=provider` (書込可能な `conn_core` 版) を渡していた箇所を、`conn_supervisor` (RO) で構築した専用の `healthcheck_provider` に差し替える** — `provider` 変数自体は `market_tools.build(provider, ...)` (`_assert_tools_registered` の起動時検証用) や `Executor(quote_fn=...)` の束縛元として他所で使われ続けるため無変更のまま残し、`TradeLoop` にだけ別インスタンスを渡す (`TradeLoop.provider` は healthcheck 専用でこれ以外に使われないため、この差し替えの影響範囲は healthcheck だけに閉じる):

```python
    # プラン 8 (Task 15, レビュー反映1回目 裁定書 F-6/CR-5): TradeLoop の
    # provider は healthcheck 専用 (lock 外で呼ばれる) — 書込可能な
    # conn_core 版を渡すと healthcheck 内の get_bars が無保護で conn_core
    # を書き込む (Global Constraints 違反)。conn_supervisor (RO) +
    # readonly=True で構築した専用インスタンスを渡す。
    # 注意: readonly=True のため healthcheck はもう ohlcv キャッシュを
    # 温めない (get_bars/derive の upsert_bars がスキップされる) —
    # これは意図した挙動であり退行ではない。CR-4 と同じ理由でキャッシュの
    # 一次的な書き手は scheduler tick (mark-to-market 等、既存の conn_core
    # 版 provider) であり続けるため、healthcheck が書かなくてもキャッシュ
    # 鮮度は保たれる。
    healthcheck_provider = PriceProvider(conn_supervisor, settings, clock,
                                         readonly=True)
    trade_loop = TradeLoop(conn=conn_core, runner=runner, settings=settings,
                           executor=executor, provider=healthcheck_provider,
                           econ=econ, policy=policy, activity=activity,
                           notifier=notifier, clock=clock,
                           core_lock=core_lock, conn_supervisor=conn_supervisor,
                           watch=mission_watch)
```

**注意**: `conn_supervisor`/`core_lock` は `build_app` 内で `trade_loop = TradeLoop(...)` より**前**に定義されている必要がある。`conn_supervisor` は Task 13 で `conn_shell` 構築の直後 (かなり早い位置) に新設済みのため既に条件を満たすが、`core_lock = threading.RLock()` は**現物 473 行目** (= `trade_loop = TradeLoop(...)` の **464 行目**より**後**) にある — `core_lock` の定義を `trade_loop` 構築より前に移動すること (`grep -n "core_lock = threading\|trade_loop = TradeLoop" src/agentic_fx/service.py` で現状の行順を確認してから並べ替える)。`PriceProvider(..., readonly=True)` kwarg は Task 5 (CR-4 対応) で新設済み — 未実装のままここに到達している場合は Task 5 の適用漏れを疑うこと。

**(裁定書 F-1 / CR-2 / P8-01 追加) `tests/loops/test_trade_loop_phases.py` に CLOSE 版の lock-free 回帰テストを追加する** (Step 1 の `test_scheduler_tick_can_acquire_lock_while_worker_runner_blocks` の直後):

```python
def test_scheduler_tick_can_acquire_lock_while_close_quote_fetch_blocks(tmp_path):
    """裁定書 F-1 (CR-2/P8-01) の回帰ピン: CLOSE intent の quote 取得
    (gather_close_snapshot) は commit-pre (lock 非保持) で行われるため、
    quote_fn がブロックしていても scheduler tick は core_lock を取得できる
    (既定構成の yfinance には timeout が効かないため、この lock-free 化
    自体が安全性の担保になる)。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    order_id = orders_insert(  # tests/loops/test_trade_loop.py の
        # orders.insert を使う (import 済み想定 — 無ければ追加する)
        conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status="open", now=NOW, quantity=0.1,
        avg_fill_price=148.50)
    runner.results = [MissionResult(
        "completed", {"action": "close", "order_id": order_id,
                      "reasoning": "x"}, [])]

    release = threading.Event()

    def blocking_quote_fn(pair):
        release.wait(5.0)
        return QUOTE

    loop.executor.quote_fn = blocking_quote_fn
    t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
    t.start()
    time.sleep(0.1)  # commit-pre の gather_close_snapshot がブロック中

    acquired = loop._core_lock.acquire(timeout=1.0)
    assert acquired, ("core_lock は CLOSE の quote 取得中も他スレッドから"
                      "取得できるはず (commit-pre は lock 非保持)")
    loop._core_lock.release()

    release.set()
    t.join(timeout=5.0)
```

（`orders_insert`/`NOW`/`QUOTE` は `tests/loops/test_trade_loop.py` からの import、または同モジュールの `orders.insert`/`NOW`/`QUOTE` をそのまま使う。`runner.results` は `FakeRunner` の既存属性に合わせて実装者が調整すること — 同ファイルの `FakeRunner` 実装を確認してから書く。）

**(裁定書 F-6 / CR-5 追加) `tests/test_service_app.py` に追加**:

```python
def test_trade_loop_healthcheck_provider_is_readonly(tmp_path):
    """裁定書 F-6 (CR-5): TradeLoop.provider (healthcheck 専用) は
    conn_supervisor (RO) で構築されている — conn_core への書込可能な
    provider を healthcheck に使っていないことの配線確認。"""
    app = build_app(tmp_path)
    try:
        assert app.trade_loop.provider.conn is app.conn_supervisor
        assert app.trade_loop.provider.readonly is True
    finally:
        app.close()
```

**(裁定書 F-6 / CR-5 追加、レビュー反映 2 回目 R2-CX-02 で完成形に置換) `tests/loops/test_trade_loop.py` に追加**:

冒頭の import 節に `import threading` と `from agentic_fx.store import signals` を追加する (`tests/loops/test_trade_loop_signal.py` の `_add_signal` ヘルパー・requeue 誘発パターン — `MissionResult("timeout", None, [])` で runner 失敗 → requeue 経路 — に倣う。当ファイルには signal ヘルパーが無いため、下記テスト内にインライン展開する):

```python
def test_requeue_signal_happens_under_core_lock(tmp_path):
    """裁定書 F-6 (CR-5) / レビュー反映 2 回目 R2-CX-02: finally 節の
    `_requeue_signal` は core_lock 保持中に呼ばれる — 呼び出し中は他
    スレッドから core_lock を取得できないこと、かつ呼び出しがちょうど
    1 回であることを実際に検証する (骨格・恒真テストではない)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "timeout", None, [])])  # runner 失敗 → requeue 経路 (consume 前)
    sid = signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="signal",
        payload={"direction": "long", "strength": 0.7, "rationale": "up"},
        now=NOW)

    entered = threading.Event()
    proceed = threading.Event()
    calls: list[object] = []
    original_requeue_signal = loop._requeue_signal  # bound method (self 済み)

    def spy_requeue_signal(*args, **kwargs):
        calls.append(args)
        entered.set()
        # requeue 呼び出しの「最中」を維持したまま、別スレッドに
        # core_lock.acquire(blocking=False) を試させる猶予を作る。
        assert proceed.wait(5.0), "checker スレッドが確認を完了しなかった"
        return original_requeue_signal(*args, **kwargs)

    loop._requeue_signal = spy_requeue_signal

    t = threading.Thread(target=lambda: loop.run_once("signal"), daemon=True)
    t.start()
    assert entered.wait(5.0), "_requeue_signal が呼ばれなかった"

    # RLock は同一スレッドからの acquire(blocking=False) は常に成功して
    # しまう (再入可能) ため、必ず別スレッド (checker) から確認する。
    acquired: list[bool] = []
    checker = threading.Thread(
        target=lambda: acquired.append(
            loop._core_lock.acquire(blocking=False)))
    checker.start()
    checker.join(timeout=5.0)
    if acquired and acquired[0]:
        loop._core_lock.release()  # 誤って取れてしまった場合の後始末
    assert acquired == [False], (
        "_requeue_signal 実行中は他スレッドから core_lock を取得できない"
        "はず (finally 節が with self._core_lock: で包んでいることの検証)")

    proceed.set()
    t.join(timeout=5.0)
    assert not t.is_alive()

    assert len(calls) == 1  # requeue はちょうど 1 回だけ呼ばれる
    row = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id=?",
        (sid,)).fetchone()
    assert row["status"] == "pending"
    assert row["requeue_count"] == 1
```

- [ ] **Step 10: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 11: 変異テスト**

**リストは下限。** この task が守ろうとしている性質は「**run 相で core_lock を保持しない**」と
「**commit-core では保持する**」の 2 つ (対の不変条件)。この 2 点を壊す変異を自分で追加し、
red になるかを確かめよ。**リストに無い変異を追加したら、その内容と結果 (KILLED/SURVIVED) を
必ず報告せよ。** また **KILLED でも「落ちたテスト名が狙った防御のものか」を確認**すること
(件数だけ見ない — 別の理由で落ちているケースが実在する)。

1. `_run_once_impl` の `with self._core_lock:` (commit-core 相) を外して lock 非保持にする
   → **下記の `test_commit_core_holds_core_lock` が red になること。**

   **(2026-08-09 着手前検証で改訂)** 旧プランはここを「変異テストの限界」として Task 20 の
   E2E に丸投げしていた。**本 task の存在理由そのものが lock 境界である以上、最も殺したい
   変異を測れないまま先へ進めてはならない。** 同じファイル内の
   `test_requeue_signal_happens_under_core_lock` (Step 9) が**まさにこの形を実現している** —
   対象メソッドを spy で差し替えて呼び出しの「最中」に留め、**別スレッド (checker)** から
   `acquire(blocking=False)` を試して `False` を期待する。`connect(check_same_thread=False)`
   (`store/db.py:153`) なので別スレッドからの DB 利用も問題ない (現物確認済み)。

   `tests/loops/test_trade_loop_phases.py` に追加する:

   ```python
   def test_commit_core_holds_core_lock(tmp_path):
       """設計書 §3.1: commit-core 相 (consume/Risk Gate/執行/finish) は
       core_lock を保持したまま実行される。run 相が lock 非保持であることと
       対になる不変条件で、**こちらが崩れると DB 書込が無保護になる**。

       executor の呼び出しを spy で捕まえて「実行中」に留め、別スレッドから
       core_lock を取れないことを確認する (RLock は同一スレッドからは常に
       取れてしまうため、必ず別スレッドで確かめる)。
       """
       conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
           "completed", {"action": "hold", "reasoning": "x"}, [])])

       entered = threading.Event()
       proceed = threading.Event()
       original = loop.executor.record_and_validate_intent

       def spy(*args, **kwargs):
           entered.set()
           assert proceed.wait(5.0), "checker スレッドが確認を完了しなかった"
           return original(*args, **kwargs)

       loop.executor.record_and_validate_intent = spy
       t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
       t.start()
       assert entered.wait(5.0), "commit-core に到達しなかった"

       acquired: list[bool] = []
       checker = threading.Thread(
           target=lambda: acquired.append(
               loop._core_lock.acquire(blocking=False)))
       checker.start()
       checker.join(timeout=5.0)
       if acquired and acquired[0]:
           loop._core_lock.release()
       assert acquired == [False], (
           "commit-core 実行中は他スレッドから core_lock を取得できないはず")

       proceed.set()
       t.join(timeout=5.0)
       assert not t.is_alive()
   ```

   **注意**: `record_and_validate_intent` は HOLD でも呼ばれる (intent の記録は常に行う契約)
   ので、`{"action": "hold"}` の結果で commit-core に到達する。`runner.results` の与え方は
   `FakeRunner` の現物に合わせること。

   **これが green になれば、Task 20 の E2E は「唯一の防波堤」ではなくなる** —
   progress.md への「限界」の明記は不要になる。もし何らかの理由でこのテストが
   書けなかった場合のみ、旧プランどおり Task 20 への申し送りとして記録すること
   (**書けなかった理由を必ず報告すること**)。
2. `record_and_validate_intent` の `if intent.action is Action.HOLD:` を削除 → `tests/core/test_executor.py` の hold 系テストが red
3. `_read_exposure_pairs` が `executor._EXPOSURE` の代わりに `(S.OPEN,)` のみを使うよう改変 → 専用テストが無ければ `tests/core/test_executor_snapshot.py` の `test_open_from_snapshot_rejects_when_exposure_grew_after_commit_pre` 相当が (統合すれば) red になることを確認する
4. **(裁定書 F-1 追加)** commit-pre の CLOSE 分岐 (`elif intent.action is Action.CLOSE:` ブロック) を削除し、代わりに commit-core 内で毎回 `self.executor.gather_close_snapshot(row)` を呼ぶよう戻す (I/O をロック内に再導入する変異) → `test_scheduler_tick_can_acquire_lock_while_close_quote_fetch_blocks` が red (タイムアウトして `acquired is False`)
5. **(裁定書 F-6 追加)** `TradeLoop` 構築時の `provider=healthcheck_provider` を `provider=provider` (書込可能版) に戻す → `test_trade_loop_healthcheck_provider_is_readonly` が red
6. **(裁定書 F-6 追加、レビュー反映 2 回目 R2-CX-02 でテスト本体を完成させたことで実際に検出できるようになった)** finally 節の `with self._core_lock: self._requeue_signal(claimed)` を lock 無しの直接呼び出しに戻す → `test_requeue_signal_happens_under_core_lock` が red (checker スレッドの `loop._core_lock.acquire(blocking=False)` が `True` を返してしまい `acquired == [False]` の assert に失敗する)

- [ ] **Step 12: Commit**

```bash
git add src/agentic_fx/core/executor.py src/agentic_fx/loops/trade_loop.py \
  src/agentic_fx/service.py src/agentic_fx/config.py config/settings.yaml.example \
  tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py \
  tests/test_service_app.py
git commit -m "$(cat <<'EOF'
feat: TradeLoop 五相再構成 (prepare/run/commit-pre/commit-core/commit-post)

設計書 §3.1。run相 (WorkerRunner.run) はcore_lockを一切保持しない —
これによりMission実行中もscheduler tickがSL/TP監視のためlockを取得できる。
askも三相再構成 (同じ理由)。

レビュー反映1回目 (裁定書 F-1/CR-2/P8-01): CLOSE の quote/spec/rate も
commit-pre で取得しcommit-coreはclose_from_snapshotで取得済み値のみ使う。

レビュー反映1回目 (裁定書 F-6/CR-5): healthcheckをconn_supervisor(RO)+
readonly=Trueのprovider専用インスタンスに切り替え、finallyのrequeueを
core_lock保持中に移す (Global Constraints違反の解消)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 16: ReflectionCycle 五相再構成 + `finalize_mission` の共通化

設計書 §3.1「ReflectionCycle も同じ三相構造に再構成し、独自実装だった `_run_recorded` 相当を共通化する」を実装する。まず TradeLoop (Task 15) の `_finalize_mission` を共有モジュールへ抽出し (「共通化」の実体)、`TradeLoop` をそちらへ切り替えてから、`ReflectionCycle` を同じ関数を使う三相 (prepare/run/commit-core) + RAG 書込 (lock 不要 — Task 9 で `Rag` 自身が内部 lock を持つ) の構造に再構成する。

**Files:**
- Create: `src/agentic_fx/loops/mission_finalize.py`
- Modify: `src/agentic_fx/loops/trade_loop.py` (`_finalize_mission` を削除し共有関数の呼び出しに置換)
- Modify: `src/agentic_fx/loops/reflection_cycle.py` (全体 — 三相再構成)
- Modify: `src/agentic_fx/service.py:353-355`(`ReflectionCycle` construction に `core_lock` 追加)`,`(`_reflection_fn` の `with core_lock:` 除去)
- Modify: `tests/loops/test_reflection_cycle.py:21-30,100-110,228-237` (3 箇所の `ReflectionCycle(...)` construction に `core_lock` 追加)
- Test: `tests/loops/test_reflection_cycle_phases.py` (新規 — lock 境界の直接検証)

**Interfaces:**
- Produces:
  - `mission_finalize.finalize_mission(conn: sqlite3.Connection, activity: ActivityLog, clock: Clock, mid: int, result: MissionResult) -> bool` — `missions.finish` の CAS 化された呼び出し (**呼び出し元が `core_lock` を保持している前提**)。戻り値: `True` = 書込み試行が例外を出さなかった (CAS 受理・拒否いずれも)。`False` = 書込み自体が例外で失敗
  - `ReflectionCycle.__init__(self, *, conn, runner, rag, settings, activity, clock, core_lock: threading.RLock, watch=None)` — `core_lock` が新設必須 kwarg

- [ ] **Step 1: `mission_finalize.py` を新規作成**

```python
"""prepare/run/commit の相構造で TradeLoop/ReflectionCycle が共有する
missions.finish の CAS 呼び出しヘルパー (プラン8, 設計書 §3.1 —
「独自実装だった _run_recorded 相当を共通化する」)。
"""
from __future__ import annotations

import logging
import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions

_log = logging.getLogger("agentic_fx.loops.mission_finalize")


def finalize_mission(conn: sqlite3.Connection, activity: ActivityLog,
                     clock: Clock, mid: int, result: MissionResult) -> bool:
    """`missions.finish` の CAS 化された呼び出し (**呼び出し元が
    core_lock を保持している前提**)。設計書 §4.7 codex C-4: 二重終端は
    上書きせず警告のみ残す。書込み自体の例外は fail closed。

    戻り値: `True` = 書込み試行が例外を出さなかった (CAS 受理・拒否
    いずれも)。`False` = 書込み自体が例外で失敗。
    """
    try:
        finished = missions.finish(conn, mid, result.status, result.output,
                                   result.transcript, clock.now())
    except Exception:  # noqa: BLE001
        _log.exception("missions.finish failed for %s", mid)
        try:
            activity.write(Category.SYSTEM, "mission_finalize_failed",
                           f"mid={mid}")
        except Exception:  # noqa: BLE001
            _log.exception("failed to record mission_finalize_failed")
        return False
    if not finished:
        try:
            activity.write(Category.SYSTEM, "mission_finalize_conflict",
                           f"mid={mid} (already finalized elsewhere)")
        except Exception:  # noqa: BLE001
            _log.exception("failed to record mission_finalize_conflict")
    return True
```

- [ ] **Step 2: `trade_loop.py` を共有関数へ切り替え**

`src/agentic_fx/loops/trade_loop.py` の import 節に `from agentic_fx.loops.mission_finalize import finalize_mission` を追加する。`TradeLoop._finalize_mission` メソッド (Task 15 で新設) を**削除**し、本体内の 3 箇所の呼び出し (`self._finalize_mission(mid, result)`) をすべて `finalize_mission(self.conn, self.activity, self.clock, mid, result)` に置換する (`grep -n "_finalize_mission" src/agentic_fx/loops/trade_loop.py` で呼び出し箇所を洗い出し、メソッド定義も含めて過不足なく置換すること)。

```bash
uv run pytest tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py \
  tests/loops/test_trade_loop_signal.py -q
```

Expected: 全件 PASS (挙動は不変 — 関数の置き場所を変えただけ)。

- [ ] **Step 3: 失敗するテストを書く (ReflectionCycle の lock 境界)**

`tests/loops/test_reflection_cycle_phases.py` を新規作成する:

```python
"""ReflectionCycle 五相再構成の lock 境界 (プラン8, 設計書 §3.1)。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.runners.base import Mission, MissionResult
from tests.loops.test_reflection_cycle import _cycle, _closed_order


class _SlowRunner:
    def __init__(self, core_lock: threading.RLock, result: MissionResult) -> None:
        self._core_lock = core_lock
        self._result = result
        self.lock_was_free_during_run = False

    def run(self, mission: Mission) -> MissionResult:
        acquired = self._core_lock.acquire(blocking=False)
        if acquired:
            self.lock_was_free_during_run = True
            self._core_lock.release()
        return self._result


def test_reflect_one_does_not_hold_core_lock_during_runner_run(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [])
    slow = _SlowRunner(cyc._core_lock, MissionResult(
        "completed", {"content": "振り返り"}, []))
    cyc.runner = slow
    _closed_order(conn)

    cyc.run_pending()

    assert slow.lock_was_free_during_run is True
```

- [ ] **Step 4: テスト実行して FAIL を確認**

```bash
uv run pytest tests/loops/test_reflection_cycle_phases.py -q
```

Expected: FAIL (`TypeError: ReflectionCycle.__init__() missing ... 'core_lock'` または `_cycle` fixture 未更新のエラー)。

- [ ] **Step 5: `reflection_cycle.py` を実装**

`src/agentic_fx/loops/reflection_cycle.py` の import 節に `import threading` と `from agentic_fx.loops.mission_finalize import finalize_mission` を追加する。`ReflectionCycle.__init__` (25-35 行) を以下に変更する:

```python
class ReflectionCycle:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 rag: Rag, settings: Settings, activity: ActivityLog,
                 clock: Clock, core_lock: threading.RLock,
                 watch: MissionWatch | None = None) -> None:
        self.conn = conn
        self.runner = runner
        self.rag = rag
        self.settings = settings
        self.activity = activity
        self.clock = clock
        self._core_lock = core_lock
        self.watch = watch if watch is not None else MissionWatch()
```

`run_pending` (37-57 行) を以下に変更する (先頭の SELECT を lock 保持下に):

```python
    def run_pending(self, max_items: int = 3) -> int:
        """Reflect on closed orders without reflection. Per-item isolation.

        1 回の呼び出しで処理する件数を `max_items` に制限する — core_lock
        保持中の Mission 合成時間を抑え、SL/TP 監視の停止窓を制限するため
        (この docstring は既存のまま — プラン8 五相再構成後もこの制約の
        意図は変わらない。実際の lock 保持範囲は各相ごとに Task 15/16 の
        方針で細分化されている)。
        """
        with self._core_lock:
            rows = self.conn.execute(
                "SELECT o.* FROM orders o LEFT JOIN reflections r "
                "ON r.order_id = o.id WHERE o.status='closed' "
                "AND r.order_id IS NULL ORDER BY o.id LIMIT ?",
                (max_items,)).fetchall()
        created = 0
        for row in rows:
            try:
                if self._reflect_one(dict(row)):
                    created += 1
            except Exception:  # noqa: BLE001 — per-item isolation
                _log.exception("per-item reflection failed for order #%s",
                               row["id"])
        return created
```

`_reflect_one` (59-154 行) を以下に置き換える:

```python
    def _reflect_one(self, row: dict) -> bool:
        """Run reflection on one closed order (プラン8 三相再構成)。

        prepare (lock) → run (lock 非保持) → commit-core (lock: finalize)
        → commit-post (RAG 書込は lock 不要 — Rag 自身が内部 lock を持つ
        (Task 9)。SQLite 書込 (`reflections.save`) のみ lock 保持)。
        """
        with self._core_lock:
            now = self.clock.now()
            intent = None
            if row["intent_id"]:
                ir = self.conn.execute(
                    "SELECT payload_json FROM trade_intents WHERE id=?",
                    (row["intent_id"],)).fetchone()
                intent = json.loads(ir["payload_json"]) if ir else None

            prompt = (load_prompt("reflection") + "\n\n## トレード詳細\n"
                      + json.dumps({"order": {k: row[k] for k in (
                          "id", "pair", "direction", "horizon", "quantity",
                          "avg_fill_price", "close_price", "realized_pnl",
                          "close_reason")},
                          "entry_reasoning": (intent or {}).get("reasoning")},
                          ensure_ascii=False, indent=1))

            mission = Mission(
                prompt=prompt, tools=[], output_schema=_SCHEMA,
                max_turns=2,
                timeout_sec=self.settings.llama_swap.timeout_sec)

            mid = missions.start(
                self.conn, "reflection",
                self.settings.runner.trade.backend,
                self.settings.runner.trade.model, now)

        self.watch.begin(mid, "reflection", mission.timeout_sec)
        try:
            result = self.runner.run(mission)
            if not isinstance(result, MissionResult):
                result = MissionResult("failed", None, [])
        except Exception:  # noqa: BLE001
            _log.exception("reflection runner raised")
            result = MissionResult("failed", None, [])
        finally:
            self.watch.end(mid)

        with self._core_lock:
            finalized = finalize_mission(self.conn, self.activity, self.clock,
                                         mid, result)

        if result.status != "completed" or not finalized:
            # finish 失敗時は監査未確定 (missions 行が running のまま) なので
            # reflection も保存しない。SQLite マーカー (reflections 行) が
            # 無いので次周期の run_pending が同じ order を再試行する
            return False

        if not isinstance(result.output, dict):
            return False
        content = result.output.get("content")
        if not isinstance(content, str):
            return False

        # RAG → SQLite order (SQLite row is completion marker)。RAG 書込は
        # lock 不要 (Rag 自身が内部 lock を持つ — Task 9)。失敗時は SQLite
        # 行が無いので次回 run_pending が同じ order を再試行する (idempotent)。
        try:
            self.rag.add_reflection(row["id"], content, row["pair"])
        except Exception:  # noqa: BLE001
            _log.exception("rag.add_reflection failed for #%s — retry next run",
                           row["id"])
            return False

        with self._core_lock:
            reflections.save(self.conn, row["id"], content, now)
        try:
            self.activity.write(
                Category.AGGREGATE, "reflection_created",
                f"#{row['id']} {row['pair']}",
                ref_id=str(row["id"]))
        except Exception:  # noqa: BLE001
            _log.exception("activity write failed for reflection #%s", row["id"])
        return True
```

- [ ] **Step 6: テスト実行して PASS を確認**

```bash
uv run pytest tests/loops/test_reflection_cycle_phases.py -q
```

Expected: まだ FAIL (`_cycle` fixture 未更新 — 次 Step で更新)。

- [ ] **Step 7: `tests/loops/test_reflection_cycle.py` の 3 箇所を更新**

ファイル冒頭の import 節に `import threading` を追加する。`_cycle` (21-30 行) の `ReflectionCycle(...)` 構築に `core_lock=threading.RLock()` を追加する:

```python
    cyc = ReflectionCycle(
        conn=conn, runner=FakeRunner(results), rag=rag,
        settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"),
        clock=FixedClock(NOW), core_lock=threading.RLock(),
        watch=watch)
```

100-110 行・228-237 行の残り 2 箇所の `ReflectionCycle(...)` 構築にも同様に `core_lock=threading.RLock()` を追加する (`grep -n "ReflectionCycle(" tests/loops/test_reflection_cycle.py` で全箇所を洗い出し、漏れなく更新すること)。

```bash
uv run pytest tests/loops/test_reflection_cycle.py -q
uv run pytest tests/loops/test_reflection_cycle_phases.py -q
```

Expected: 全件 PASS。

- [ ] **Step 8: `service.py` を実装**

`build_app` 内の `reflection = ReflectionCycle(...)` (353-355 行付近) に `core_lock=core_lock` を追加する:

```python
    reflection = ReflectionCycle(conn=conn_core, runner=runner, rag=rag,
                                 settings=settings, activity=activity,
                                 clock=clock, core_lock=core_lock,
                                 watch=mission_watch)
```

**注意**: `core_lock` の定義位置が `reflection = ReflectionCycle(...)` より後ろにある場合 (Task 15 の並べ替え次第)、`core_lock = threading.RLock()` の定義を `trade_loop`/`reflection` の構築より前に移動すること (Task 15 Step 9 の注意と同じ配慮)。

Task 15 で作った `_reflection_fn` (`with core_lock: return reflection.run_pending()`) を以下に変更する (`ReflectionCycle` 自身が prepare/commit-core で lock を管理するようになったため):

```python
    def _reflection_fn():
        # プラン 8 (Task 16): ReflectionCycle 自身が prepare/commit-core で
        # core_lock を保持する三相構造になったため、ここでは lock を掴まない。
        return reflection.run_pending()
```

- [ ] **Step 9: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 10: 変異テスト**

1. `finalize_mission` の `if not finished:` ブロックを削除 → 専用テストが無ければ `tests/store/test_missions_cas.py` の CAS 系テストとは別に、`finalize_mission` 自身の直接テストを 1 本追加してから (`tests/loops/test_reflection_cycle_phases.py` へ追記) この変異で red になることを確認する
2. `_reflect_one` の `if result.status != "completed" or not finalized:` を `if result.status != "completed":` に改変 (finalized チェックを削除) → 専用テストとして「finalize が False を返すケースで reflection が保存されない」ことを確認するテストを追加してから確認する
3. `_reflect_one` 内の RAG 書込ブロックを `with self._core_lock:` で誤って包む改変を行い、`test_reflect_one_does_not_hold_core_lock_during_runner_run` が red に**ならない**ことを確認する — これは run 相 (RAG 書込ではなく runner.run) の lock 非保持を検証するテストであり、RAG 書込の lock 有無を直接検出しない。**この限界を progress.md に明記し、RAG 書込が誤って lock 保持下に入っていないことは Step 5 の実装コード diff レビューで確認する** (テストで機械的に検出できない設計判断の限界)

- [ ] **Step 11: Commit**

```bash
git add src/agentic_fx/loops/mission_finalize.py src/agentic_fx/loops/trade_loop.py \
  src/agentic_fx/loops/reflection_cycle.py src/agentic_fx/service.py \
  tests/loops/test_reflection_cycle.py tests/loops/test_reflection_cycle_phases.py
git commit -m "$(cat <<'EOF'
feat: ReflectionCycle 五相再構成 + finalize_mission の共通化

設計書 §3.1。TradeLoop の _finalize_mission を共有モジュールへ抽出し、
ReflectionCycle も同じ三相構造 (prepare/run/commit-core) に再構成する。
RAG 書込は Rag 自身の内部 lock (Task 9) に委ね、core_lock は取らない。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 17: shell readline 中断 seam (対話モードで `stop_event` が `main` を wake する唯一の経路)

設計書 §6「停止シーケンスの実行主体の一意化」(codex I3-2) が要求する必須依存。対話モードの `main` は `input()` でブロックしているため、`stop_event` が (Task 19 で) watchdog によってセットされても `input()` は自然には解けない。`select.select` によるポーリングへ置き換え、`stop_event` を定期的にチェックできるようにする。

**Files:**
- Modify: `src/agentic_fx/shell.py` (全体)
- Test: `tests/test_shell_interrupt.py` (新規)

**Interfaces:**
- Produces:
  - `shell.run_shell(commands: Commands, stop_event: threading.Event, *, input_fn=input, print_fn=print, stdin_stream=None, poll_interval: float = 0.5) -> None` — **`input_fn` が既定値 (`input` そのもの) のときのみ**、新設の割込み可能読み取り (`_read_line_interruptible`) を使う。`input_fn` が上書きされている場合 (既存テストの fake 注入) は従来どおり `input_fn(prompt)` を直接呼ぶ (後方互換)。`stdin_stream` は割込み可能読み取りが使う実ストリーム (既定 `None` = `sys.stdin`) — テストが `os.pipe()` 経由の fake ストリームを注入できる
  - `shell._read_line_interruptible(prompt: str, stop_event: threading.Event, *, stream, poll_interval: float) -> str | None` — `select.select([stream], [], [], poll_interval)` でポーリングし、`stop_event` が立てば `None` を返す (中断)。データが来れば 1 行読んで返す。EOF (空文字列読み取り) は `EOFError` を送出する。**(裁定書 F-15/IM-8)** `select` の前に `_stream_has_buffered_data(stream)` (`stream.buffer.peek(1)`) で Python 側バッファの残存を確認し、あれば select を経由せず直接読む — 複数行同時到着時の後続行滞留を防ぐ

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_shell_interrupt.py` を新規作成する:

```python
"""shell readline 中断 seam (プラン8, 設計書 §6 codex I3-2)。"""
from __future__ import annotations

import os
import threading
import time

import pytest

from agentic_fx.shell import run_shell


class _FakeCommands:
    def dispatch(self, line: str) -> str:
        return f"echo: {line}"


def test_stop_event_wakes_blocked_shell_promptly(tmp_path):
    """対話モードで stop_event が外部スレッドからセットされたとき、
    input() 相当のブロッキング読み取りが速やかに解ける
    (poll_interval を短く設定し、real select ベースのポーリングで
    実際に解けることを実測する)。
    """
    r_fd, w_fd = os.pipe()  # 読み取り側は「データが来ない」ダミー stdin
    stream = os.fdopen(r_fd, "r")
    stop_event = threading.Event()

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": lambda *_: None},
        daemon=True)
    t.start()
    time.sleep(0.1)  # run_shell がポーリングループに入るまで待つ

    start = time.monotonic()
    stop_event.set()
    t.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not t.is_alive(), "stop_event セット後、shell スレッドが終了していない"
    assert elapsed < 1.0  # poll_interval=0.05s に対して十分な余裕

    os.close(w_fd)
    stream.close()


def test_line_is_read_when_available(tmp_path):
    """通常経路: stdin にデータが来れば読み取ってコマンドとして処理する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": printed.append},
        daemon=True)
    t.start()
    time.sleep(0.1)
    writer.write("hello\n")
    writer.flush()
    time.sleep(0.2)

    stop_event.set()
    t.join(timeout=2.0)

    assert "echo: hello" in printed
    writer.close()
    stream.close()


def test_multiple_lines_arriving_together_are_not_stuck(tmp_path):
    """裁定書 F-15 (IM-8) の回帰ピン: ペースト等で複数行が 1 回の書込みで
    同時到着した場合、2 行目以降が「次の新規入力が来るまで」滞留せず、
    追加の書込みなしに両方処理されることを確認する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": printed.append},
        daemon=True)
    t.start()
    time.sleep(0.1)
    writer.write("hello\nworld\n")  # 2 行を 1 回の書込み・flush でまとめて送る
    writer.flush()
    time.sleep(0.3)  # 追加の書込みは一切行わない — この待ちだけで両方届くはず

    stop_event.set()
    t.join(timeout=2.0)

    assert "echo: hello" in printed
    assert "echo: world" in printed  # 旧稿はここが追加入力なしでは届かなかった
    writer.close()
    stream.close()
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_shell_interrupt.py -q
```

Expected: FAIL (`TypeError: run_shell() got an unexpected keyword argument 'stdin_stream'`)。

- [ ] **Step 3: `shell.py` を実装**

```python
"""対話シェル — 静かなプロンプト。ログは pull 型コマンドのみ (設計書 §8)。

readline 中断 seam (プラン8, 設計書 §6 codex I3-2): 対話モードの main は
`input()` でブロックするため、`stop_event` が別スレッド (watchdog) から
セットされても自然には解けない。`select.select` によるポーリングへ
置き換え、定期的に `stop_event` をチェックできるようにする — 対話モード
で main を wake する唯一の経路。
"""
from __future__ import annotations

import select
import sys
import threading

from agentic_fx.commands import Commands


def _stream_has_buffered_data(stream) -> bool:
    """裁定書 F-15 (IM-8): `select.select` は OS 側パイプ/ソケットの
    読み取り可能性しか見ない。Python の `TextIOWrapper`/`BufferedReader`
    は 1 回の `readline()` で OS から利用可能な分をまとめて先読みして
    内部バッファに溜め込むため、複数行が同時到着した場合 (ペースト等)、
    1 行目を `readline()` で消費した後、2 行目以降は OS 側に新規データが
    無いまま Python 側バッファに残る。次回の `select.select` はこれを
    検出できず (fd に新規データが来ていない)、2 行目が「次の何らかの
    新規入力が来るまで」滞留する。`stream.buffer.peek(1)` で Python 側
    バッファの残存を先にチェックし、あれば select を経由せず直接
    `readline()` する (バッファ済みなのでブロックしない)。
    """
    buf = getattr(stream, "buffer", None)
    if buf is None or not hasattr(buf, "peek"):
        return False
    try:
        return bool(buf.peek(1))
    except Exception:  # noqa: BLE001 — peek 不可なストリームは select 頼みにフォールバック
        return False


def _read_line_interruptible(prompt: str, stop_event: threading.Event, *,
                             stream, poll_interval: float) -> str | None:
    """`select.select` でストリームの読み取り可能性をポーリングする。
    `stop_event` が立てば即座に `None` を返す (中断 — EOF とは区別する)。
    データが来れば 1 行読んで (末尾改行を除いて) 返す。EOF は
    `EOFError` を送出する。**(裁定書 F-15/IM-8)** Python 側バッファに
    既に読み取り済みのデータが残っていれば (`_stream_has_buffered_data`)
    select を経由せず即座に `readline()` する — 複数行同時到着時の
    後続行滞留を防ぐ。
    """
    print(prompt, end="", flush=True)
    while not stop_event.is_set():
        if not _stream_has_buffered_data(stream):
            ready, _, _ = select.select([stream], [], [], poll_interval)
            if not ready:
                continue
        line = stream.readline()
        if line == "":
            raise EOFError()
        return line.rstrip("\n")
    return None


def run_shell(commands: Commands, stop_event: threading.Event, *,
              input_fn=input, print_fn=print, stdin_stream=None,
              poll_interval: float = 0.5) -> None:
    use_interruptible = input_fn is input
    stream = stdin_stream if stdin_stream is not None else sys.stdin
    while not stop_event.is_set():
        try:
            if use_interruptible:
                raw = _read_line_interruptible(
                    "afx> ", stop_event, stream=stream,
                    poll_interval=poll_interval)
                if raw is None:
                    return  # stop_event がポーリング中に立った (中断)
                line = raw.strip()
            else:
                line = input_fn("afx> ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            return
        if stop_event.is_set():
            return
        if not line:
            continue
        if line == "stop":
            stop_event.set()
            return
        try:
            print_fn(commands.dispatch(line))
        except KeyboardInterrupt:
            stop_event.set()
            return
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_shell_interrupt.py -q
```

Expected: PASS。

- [ ] **Step 5: 既存テストの確認**

```bash
uv run pytest -q -k shell
uv run pytest -q
```

Expected: 全件 PASS。既存の `run_shell` テスト (`input_fn=` を明示的に注入するもの) は `use_interruptible = input_fn is input` の判定により従来どおり `input_fn` を直接呼ぶ経路を通るため無変更で動くことを確認する。既存テストが存在する場所を `grep -rln "run_shell" tests/` で確認し、1 件も red が無いことを確認する。

- [ ] **Step 6: 変異テスト**

1. `_read_line_interruptible` の `while not stop_event.is_set():` を `while True:` に改変 (stop_event を見なくする) → `test_stop_event_wakes_blocked_shell_promptly` が red (timeout するか `t.is_alive()` が True のまま)
2. `run_shell` の `use_interruptible = input_fn is input` を `use_interruptible = False` に固定 → `test_stop_event_wakes_blocked_shell_promptly` が red (通常の `input_fn` 経路に落ちて `stdin_stream` が使われなくなる)
3. **(裁定書 F-15/IM-8 追加)** `_read_line_interruptible` の `if not _stream_has_buffered_data(stream):` 判定を削除し常に `select.select(...)` を経由するよう戻す → `test_multiple_lines_arriving_together_are_not_stuck` が red (`"echo: world"` が追加入力なしには届かない)

- [ ] **Step 7: Commit**

```bash
git add src/agentic_fx/shell.py tests/test_shell_interrupt.py
git commit -m "$(cat <<'EOF'
feat: shell readline 中断 seam (select ポーリングで stop_event を検知)

設計書 §6 codex I3-2。対話モードの main を停止シーケンス開始時に
確実に wake する唯一の経路。既存の input_fn 注入テストとは後方互換。

レビュー反映1回目 (裁定書 F-15/IM-8): select.select とバッファ付き
readline() の混用により複数行同時到着時に後続行が滞留する欠陥を修正。
stream.buffer.peek() でPython側バッファの残存を先にチェックする。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 18: improve worker profile の権限境界 (DB パス非提供 + Landlock + 起動拒否)

設計書 §4.6「worker profile と権限境界」を実装する。**改善ループ本体・improve registry の中身はプラン 9** — 本 task は「構造的到達不能」の 2 層防御 (接続情報の非提供 + Landlock による FS 自己制限) だけを `mission_worker.py` に実装する。§4.6 の「到達不能 = 実行不能」の意味論 (`run_holdout_gate` の import 自体は可能だが、データ到達 (DB 接続) が構造的に失敗する) を実測で固定する。

**Files:**
- Modify: `src/agentic_fx/mission_worker.py` (`worker_profile == "improve"` 分岐 + `_bootstrap_improve_profile` 新設)
- Modify: `docs/superpowers/plans/2026-08-01-phase2-decomposition.md` (§12 申し送り③ — 「到達不能」表現への注記追加)
- Test: `tests/test_improve_profile_isolation.py` (新規 — 実 subprocess による E2E 帯)

**Interfaces:**
- Produces:
  - `mission_worker._bootstrap_improve_profile() -> None` — 呼び出し時点の `Path.cwd()` (WorkerRunner が `cwd=` に渡した専用空 workdir) と `Path(__file__).resolve().parents[1]` (src/ コードツリー) を使い、`landlock.restrict_to(read_only_paths=[...], read_write_paths=[workdir])` を呼ぶ。**(裁定書 F-8/IM-2/P8-04) read_only_paths は `code_root` に加え `sys.prefix` (venv/site-packages) と `sys.base_prefix` (stdlib 実体) も含む** — この直後に `main()` が `pydantic`/`LocalRunner`/`ToolRegistry` を import するため、これらが無いと `PermissionError` で起動不能になる (実測確認済み — いずれも `data/` の祖先ではないため到達不能の意味論は保たれる)。**`landlock.is_available()` が False なら `RuntimeError` (fail closed — improve worker は起動拒否)**
  - `mission_worker.main()` の `worker_profile == "improve"` 分岐: `db_path`/`plugins_dir` を一切参照しない (handshake で `None` が渡ってくる前提 — `WorkerRunner` の Task 10 実装が既にこの条件分岐を持つ)。`_bootstrap_improve_profile()` → resource limit (trade と同じ `child_as_mb`/`child_nofile`/`child_fsize_mb`。ネットワーク毒入れはしない — 設計書 §4.5) → 空の `ToolRegistry()` (改善ループの実ツールセットはプラン 9) で `LocalRunner` を組み立てる

- [ ] **Step 1: 分解書への注記追加 (§12 申し送り③)**

`docs/superpowers/plans/2026-08-01-phase2-decomposition.md` の該当箇所 (`grep -n "run_holdout_gate\|構造的に到達不能" docs/superpowers/plans/2026-08-01-phase2-decomposition.md` で 96 行目・104 行目付近を特定する) に以下の注記を追加する (既存文言は削除せず、注記として追記する):

```markdown
> **注記 (プラン8, 設計書 §4.6)**: 「構造的に到達不能」は「実行不能」の意味論で読み替える —
> Landlock の allowlist はコードツリーの読取を許すため `run_holdout_gate` の**関数 import
> 自体は可能**。遮断の実体は**データ到達**にあり、`run_holdout_gate` は `history_conn`
> (履歴 DB 接続) を必須引数に取るため、improve worker は DB パス非提供 + Landlock の
> data/ 遮断により接続を構成できず、import できても**実行が必ず失敗する**
> (プラン8 Task 18 で実測検証)。
```

- [ ] **Step 2: 失敗するテストを書く (Landlock 不能時の起動拒否)**

`tests/test_improve_profile_isolation.py` を新規作成する:

```python
"""improve worker profile の権限境界 (プラン8, 設計書 §4.6)。"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import is_available as landlock_available


def test_bootstrap_improve_profile_raises_when_landlock_unavailable(monkeypatch, tmp_path):
    import agentic_fx.mission_worker as mw_mod

    monkeypatch.setattr(mw_mod.landlock, "is_available", lambda: False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="Landlock"):
        mw_mod._bootstrap_improve_profile()
```

- [ ] **Step 3: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_improve_profile_isolation.py -q -k landlock_unavailable
```

Expected: FAIL (`AttributeError: module 'agentic_fx.mission_worker' has no attribute '_bootstrap_improve_profile'`)。

- [ ] **Step 4: `mission_worker.py` を実装**

`src/agentic_fx/mission_worker.py` の import 節に `from agentic_fx.core import landlock` と `import sysconfig` を追加する (`sys`/`os` は既存 import 済み)。`_set_resource_limits` の直後に追加:

**(レビュー反映 1 回目 — 裁定書 F-8 / IM-2 / P8-04)**: 当初案は Landlock 適用対象を `src/` コードツリーのみにしていたが、`main()` の improve 分岐は `_bootstrap_improve_profile()` の**直後**に `agentic_fx.config` (→ `pydantic`)・`agentic_fx.runners.local_runner`・`agentic_fx.tools.registry` を import する。これらは通常 venv (`sys.prefix` — 本プロジェクトでは `.venv/`) の site-packages や、venv とは別の Python 本体展開先 (`sys.base_prefix` — stdlib) にあり、`src/` allowlist には含まれないため `PermissionError` で improve worker が起動前に必ず落ちる (実測: `uv run python -c "import sys, sysconfig; print(sys.prefix, sys.base_prefix)"` で `sys.prefix=.../.venv`、`sys.base_prefix=~/.local/share/uv/python/...` と `src/` の 3 者が全く別ディレクトリであることを確認済み)。加えて受入 probe (Step 6, FC-5) は import 成功を「blocked」と区別できず、この破損を緑で通していた。

**修正方針**: 必要モジュールを Landlock 適用**前**に import する案は「どのモジュールが必要か」を将来にわたって静的に列挙し続けねばならず保守コストが高いため、**実行に必要な read-only root を明示許可する**方式を採る。`sys.prefix` (venv — site-packages) と `sys.base_prefix` (stdlib の実体) を read-only allowlist に追加する。**両者とも `data/` の祖先ディレクトリではない** (`.venv` は `data/` と同じ repo root 直下の兄弟ディレクトリ、`sys.base_prefix` は repo 外の uv 管理 Python 展開先) ため、設計書 §4.6 の「`data/` の絶対パスアクセスを OS レベルで遮断する」意味論は保たれる (Step 6 の positive/negative 両プローブがこれを実測で固定する — `sys.prefix`/`data/` を誤って repo root ごと許可していないかは、そのプローブの `open_db`/`list_data_dir` が `blocked` のままであることで検出できる):

```python
def _bootstrap_improve_profile() -> None:
    """improve worker profile の bootstrap (プラン8, 設計書 §4.6)。

    **構造的到達不能の 2 層防御**: ①接続情報の非提供 (handshake に
    db_path/plugins_dir が含まれない — `main()` の improve 分岐がこれらを
    一切参照しない) ②Landlock による FS 自己制限 (コードツリー読取 +
    venv/stdlib 読取 (裁定書 F-8/IM-2/P8-04 — 実行に必要な依存解決のため) +
    専用 workdir 読書きのみ allowlist、`data/` は遮断)。

    呼び出し時点の `Path.cwd()` は WorkerRunner が `cwd=` に渡した専用空
    workdir (呼び出し元の責務 — このプロセス自身は検証しない)。

    **Landlock 利用不能な環境では improve worker は起動拒否 (fail
    closed)** — trade profile は Landlock を任意 (RO 接続が主防御) と
    するが、improve profile は Landlock が唯一の FS 境界であるため必須。
    """
    if not landlock.is_available():
        raise RuntimeError(
            "Landlock is not available on this kernel/architecture — "
            "improve worker profile refuses to start without it "
            "(fail closed, 設計書 §4.6)")
    code_root = Path(__file__).resolve().parents[1]  # src/ ディレクトリ
    workdir = Path.cwd()
    # 裁定書 F-8 (IM-2/P8-04): この直後に main() が pydantic/LocalRunner/
    # ToolRegistry を import する — venv (site-packages) と stdlib の実体
    # ディレクトリを読取許可しないと PermissionError で起動不能になる。
    # sys.prefix (venv) / sys.base_prefix (uv 管理 Python 本体) はいずれも
    # data/ の祖先ではない (実測確認済み — 上記コメント参照)。
    venv_root = Path(sys.prefix).resolve()
    stdlib_root = Path(sysconfig.get_paths()["stdlib"]).resolve()
    read_only = [code_root, venv_root, stdlib_root]
    if Path(sys.base_prefix).resolve() != venv_root:
        read_only.append(Path(sys.base_prefix).resolve())
    landlock.restrict_to(read_only_paths=read_only, read_write_paths=[workdir])
```

`main()` 内、`worker_profile != "trade"` を拒否していた既存の分岐 (Task 7 の実装) を以下に置き換える:

```python
        worker_profile = handshake["worker_profile"]
        if worker_profile == "improve":
            _bootstrap_improve_profile()
            _set_resource_limits(
                as_mb=settings_dict["worker"]["child_as_mb"],
                nofile=settings_dict["worker"]["child_nofile"],
                fsize_mb=settings_dict["worker"]["child_fsize_mb"])
            from agentic_fx.config import Settings
            settings = Settings.model_validate(settings_dict)
            if settings.runner.improve.backend != "local":
                raise RuntimeError(
                    f"runner.improve.backend={settings.runner.improve.backend!r} "
                    "is not supported by mission_worker in this plan "
                    "(ClaudeRunner is Plan 9 scope) — fail closed")
            from agentic_fx.runners.base import Mission
            from agentic_fx.runners.local_runner import LocalRunner
            from agentic_fx.tools.registry import ToolRegistry

            # improve の実ツールセットはプラン9 — 本プランでは空の
            # registry を渡す (Landlock 適用後は data/ 到達が構造的に
            # 不能であることが本 task の受入条件そのもの)。
            registry = ToolRegistry()
            mission = Mission(**handshake["mission"])
            out_seq = SeqTracker()

            # Task 7 のレビュー 2 周目で `_next_seq` は廃止された。送出は
            # 必ず `_send_frame` を通す (serialize 失敗と transport 失敗を
            # 区別し、採番は送出成功後にだけ進める)。improve profile も
            # 同じ規律に従うこと。
            on_message = _make_on_message(protocol_out, out_seq)

            runner = LocalRunner(
                base_url=settings.llama_swap.base_url,
                model=settings.runner.improve.model, registry=registry,
                on_message=on_message)

            _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
            try:
                result = runner.run(mission)
                _send_frame(protocol_out, out_seq, {
                    "type": "result",
                    "status": result.status, "output": result.output})
            except Exception as exc:  # noqa: BLE001
                _send_frame(protocol_out, out_seq, {
                    "type": "result", "status": "failed", "output": None,
                    "error": f"{type(exc).__name__}: {exc}"})
            return
        if worker_profile != "trade":
            raise RuntimeError(
                f"unsupported worker_profile in this plan: {worker_profile!r}")
```

**実装者への注意**: 上記は `main()` の既存 trade 分岐 (Task 7) の**直前**に挿入する — `worker_profile == "improve"` のケースは早期 `return` で完結させ、以降の trade 専用ロジック (`connect_readonly`・`plugin_loader`・`_RagRpcProxy` 等) に一切触れないことを保証する。`settings.runner.improve` は既存 `RunnerSettings.improve: RunnerChoice` (config.py) を参照する — 新設フィールドではない。

- [ ] **Step 5: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_improve_profile_isolation.py -q -k landlock_unavailable
```

Expected: PASS。

- [ ] **Step 6: 失敗するテストを書く (実 subprocess による到達不能性の実測)**

`tests/test_improve_profile_isolation.py` に追記する。実 `mission_worker.py` の handshake プロトコルを経由せず、`_bootstrap_improve_profile` を直接 subprocess 内で呼ぶ**軽量プローブ**で検証する (LLM への実接続を避けるため — 受入条件 §9-3 が要求するのは「isolation の実測」であり LLM ループの実行ではない)。

**(レビュー反映 1 回目 — 裁定書 FC-5)**: 旧稿のプローブは 2 つの欠陥を持っていた。①`holdout_data_reachable` の判定が `except Exception` で例外型を問わず「blocked」扱いにしており、IM-2 (Landlock allowlist 不備) 由来の意図しない `PermissionError` や、fixture 自体が仕込んだ「DB 内容を意図的に不正文字列にする」ことによる無関係な破損エラーも合格させてしまい**反証不能**だった (Landlock が全く機能していなくても、他の理由で例外さえ出れば緑になる)。②`run_holdout_gate` を実際には呼ばず import しただけで、受入条件 §9-3③ (`run_holdout_gate` を呼んでもデータ到達不能で失敗) を実測していなかった。

修正方針: (a) `data/agentic.db` を**実際に有効な SQLite ファイル**として seed する (壊れた内容だと「壊れているから失敗しただけ」と区別がつかない)。(b) 例外の型を実測で確認したうえで narrow に絞る — 実測 (`uv run python -c "..."` で本環境の Landlock 適用後に `sqlite3.connect` を試す) により、**`open()`/`os.listdir()` の生 OS エラーは `PermissionError` (errno 13) だが、`sqlite3.connect()` 経由の失敗は SQLite が内部で OS エラーを吸収し `sqlite3.OperationalError: unable to open database file` として再送出する** (`PermissionError` ではない — 本環境で `sqlite3.connect('/tmp/<0755 でない dir>/x.db')` を実行し実測確認済み)。したがって `holdout_data_reachable` の判定は `sqlite3.OperationalError` を明示的に検査し、メッセージが `"unable to open database file"` を含むことまで確認する (他の `OperationalError` — 例えば SQL 構文誤り等 — と取り違えない)。(c) `run_holdout_gate` の**import 成功を別 assertion に分離**する (import できることと実行できないことの両方を独立に固定する — import 失敗と実行時データ到達不能を混同しない)。(d) **positive control**: allowlist 内のコードツリー読取・専用 workdir 読書きが実際に成功することを確認し、「全部失敗しているのは Landlock ポリシーが機能しているからであって、何かが壊れて全滅しているのではない」ことを積極的に示す:

```python
_ISOLATION_PROBE_SCRIPT = textwrap.dedent("""
    import os, sqlite3, sys
    from pathlib import Path
    os.chdir(sys.argv[2])  # WorkerRunner が cwd= に渡す専用空 workdir を模す
    from agentic_fx.mission_worker import _bootstrap_improve_profile
    _bootstrap_improve_profile()

    data_dir = Path(sys.argv[1])
    workdir = Path(sys.argv[2])
    results = {}

    # 裁定書 FC-5 (b): run_holdout_gate の import 成功を別 assertion に
    # 分離する (import できることと実行できないことを混同しない)。
    try:
        from agentic_fx.backtest.holdout import run_holdout_gate  # noqa: F401
        results["holdout_import"] = "ok"
    except Exception as e:
        results["holdout_import"] = f"FAILED: {type(e).__name__}: {e}"

    # ①data/agentic.db 絶対パス open 失敗 — 生 OS エラーは PermissionError
    # (Task 8 で実測確認済みの errno 13)。
    try:
        open(str(data_dir / "agentic.db"))
        results["open_db"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["open_db"] = "blocked"
    except Exception as e:  # noqa: BLE001 — 想定外の例外型は区別して記録する
        results["open_db"] = f"UNEXPECTED_EXCEPTION_TYPE: {type(e).__name__}: {e}"

    # ②data/ 列挙失敗
    try:
        os.listdir(str(data_dir))
        results["list_data_dir"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["list_data_dir"] = "blocked"
    except Exception as e:  # noqa: BLE001
        results["list_data_dir"] = f"UNEXPECTED_EXCEPTION_TYPE: {type(e).__name__}: {e}"

    # ③run_holdout_gate を呼んでもデータ到達不能で失敗 — 実測により
    # sqlite3.connect() 経由の失敗は PermissionError ではなく
    # sqlite3.OperationalError("unable to open database file") である
    # ことを確認済み。ここを PermissionError で判定すると (FC-5 が指摘
    # した反証不能な広すぎる except と同じ穴になるため) 明示的に
    # OperationalError + メッセージ内容まで確認する。
    try:
        conn = sqlite3.connect(str(data_dir / "agentic.db"))
        conn.execute("SELECT 1")
        conn.close()
        results["holdout_data_reachable"] = "UNEXPECTED_SUCCESS"
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            results["holdout_data_reachable"] = f"blocked: {type(e).__name__}"
        else:
            results["holdout_data_reachable"] = (
                f"UNEXPECTED_OPERATIONAL_ERROR_MESSAGE: {e}")
    except Exception as e:  # noqa: BLE001 — 想定外の例外型は区別して記録する
        results["holdout_data_reachable"] = f"UNEXPECTED_EXCEPTION_TYPE: {type(e).__name__}: {e}"

    # 裁定書 FC-5 (d) positive control: allowlist 内は実際に成功する
    # ことを積極的に示す (全滅していないことの証明)。
    try:
        (workdir / "probe.txt").write_text("ok")
        (workdir / "probe.txt").read_text()
        results["workdir_readwrite"] = "ok"
    except Exception as e:  # noqa: BLE001
        results["workdir_readwrite"] = f"UNEXPECTED_FAILURE: {type(e).__name__}: {e}"
    try:
        import agentic_fx
        Path(agentic_fx.__file__).read_text(encoding="utf-8")
        results["code_tree_read"] = "ok"
    except Exception as e:  # noqa: BLE001
        results["code_tree_read"] = f"UNEXPECTED_FAILURE: {type(e).__name__}: {e}"

    print(results)
""")


def test_improve_profile_cannot_reach_data_dir(tmp_path):
    """受入条件 §9-3: improve profile の実 worker プロセス内から
    ①data/agentic.db 絶対パス open 失敗 ②data/ 列挙失敗
    ③run_holdout_gate (import 可能・データ到達不能で実行失敗) を実測する。
    positive control (workdir 読書き・コードツリー読取) が成功することも
    確認し、「Landlock ポリシーが機能しているから遮断される」ことを
    「何かが壊れて全滅している」ことと区別する (裁定書 FC-5)。
    """
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 裁定書 FC-5 (a): 壊れた内容ではなく実際に有効な SQLite DB を seed
    # する — 「壊れているから失敗しただけ」と区別できるようにする。
    seed_conn = __import__("sqlite3").connect(str(data_dir / "agentic.db"))
    seed_conn.execute("CREATE TABLE t (x INTEGER)")
    seed_conn.commit()
    seed_conn.close()
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE_SCRIPT, str(data_dir), str(workdir)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "'holdout_import': 'ok'" in result.stdout
    assert "'open_db': 'blocked'" in result.stdout
    assert "'list_data_dir': 'blocked'" in result.stdout
    assert "'holdout_data_reachable': 'blocked" in result.stdout
    assert "'workdir_readwrite': 'ok'" in result.stdout
    assert "'code_tree_read': 'ok'" in result.stdout
    assert "UNEXPECTED" not in result.stdout
```

- [ ] **Step 6.5 (裁定書 F-8 / IM-2 / P8-04 追加): 実 improve worker が `ready` まで到達することの回帰テスト**

`_ISOLATION_PROBE_SCRIPT` はプロセス起動失敗を検出できない (`_bootstrap_improve_profile` を直接呼ぶだけで、`main()` の handshake〜`ready` 送出フローを経由しない)。IM-2/P8-04 が指摘した実際の欠陥は「`main()` が Landlock 適用後に `pydantic`/`LocalRunner`/`ToolRegistry` を import して `PermissionError` で起動不能になる」ことであり、これは実際に `python -m agentic_fx.mission_worker` を子プロセスとして起動し handshake〜`ready` を実測しないと検出できない。`tests/test_improve_profile_isolation.py` に追加する:

```python
def test_real_improve_worker_reaches_ready(tmp_path):
    """裁定書 F-8 (IM-2/P8-04) の回帰ピン: improve profile の実 worker
    プロセスが handshake 後に PermissionError で落ちず `ready` フレーム
    まで到達する。`ready` は LLM への実接続 (runner.run) より前に送出
    されるため、llama-swap が起動していない CI 環境でも検証できる。
    """
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    from agentic_fx.config import load_settings
    from agentic_fx.core.mission_protocol import read_frame, write_frame

    settings_path = (Path(__file__).resolve().parents[1] / "config"
                     / "settings.yaml.example")
    settings = load_settings(settings_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_fx.mission_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(workdir), start_new_session=True)
    try:
        handshake = {
            "type": "handshake", "seq": 1,
            "expected_parent_pid": os.getpid(),
            "db_path": None, "plugins_dir": None,
            "settings": settings.model_dump(),
            "mission": {"prompt": "test", "tools": [],
                       "output_schema": {"type": "object"},
                       "max_turns": 1, "timeout_sec": 30},
            "worker_profile": "improve",
            "now": "2026-08-06T00:00:00+00:00",
        }
        write_frame(proc.stdin, handshake)
        frame = read_frame(proc.stdout)
        assert frame["type"] == "ready", (
            f"improve worker did not reach ready: {frame} "
            f"stderr={proc.stderr.read(4096) if proc.stderr else ''}")
        assert frame.get("ok") is True, frame
    finally:
        proc.kill()
        proc.wait(timeout=5.0)
```

（既存 `tests/runners/test_worker_runner.py`/Task 7 のプロトコルテストの実装パターン (`write_frame`/`read_frame` の使い方、`proc.stdin`/`proc.stdout` のバッファリング) に実装者が合わせて調整すること。settings.yaml.example の `runner.improve.backend` が `"local"` であることを前提とする — 既定値がこれと異なる場合は該当キーを上書きした settings で構築すること。）

- [ ] **Step 7: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_improve_profile_isolation.py -q
```

Expected: PASS (Landlock が利用可能な環境。本実行環境は実測済みのため通るはず — §8 Task 8 参照)。

- [ ] **Step 8: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 9: 変異テスト**

1. `_bootstrap_improve_profile` の `if not landlock.is_available():` チェックを削除 → `test_bootstrap_improve_profile_raises_when_landlock_unavailable` が red
2. `_bootstrap_improve_profile` の `read_only_paths=[code_root]` を `read_only_paths=[code_root, data_dir]` のように仮に `data/` を含めるよう改変 (実装ミスの模擬) → `test_improve_profile_cannot_reach_data_dir` が red (`open_db`/`list_data_dir` が `UNEXPECTED_SUCCESS` になる)
3. **(裁定書 F-8/IM-2 追加)** `read_only` から `venv_root`/`stdlib_root` を除去する (元の欠陥を再現する変異) → `test_real_improve_worker_reaches_ready` が red (`ready` フレームに到達せず EOF/`PermissionError` で終わる)
4. **(裁定書 FC-5 追加)** `holdout_data_reachable` の判定を `except sqlite3.OperationalError` から `except Exception` に戻す (広すぎる except の再導入) → 単体では `test_improve_profile_cannot_reach_data_dir` は依然 green のままになりうる (反証不能性そのものが FC-5 の指摘点) — このため変異テストの効果は主に「レビュー時に except の型が narrow であることをコードで確認する」ことに置く。加えて `results["holdout_data_reachable"] = "blocked"` を無条件に固定する変異 (実際には何も検証していない状態を模す) を追加し、これが `test_improve_profile_cannot_reach_data_dir` を red にしない (= このテストだけでは検出できない) ことを実装者自身が手元で確認し、コメントで明記する

- [ ] **Step 10: Commit**

```bash
git add src/agentic_fx/mission_worker.py docs/superpowers/plans/2026-08-01-phase2-decomposition.md \
  tests/test_improve_profile_isolation.py
git commit -m "$(cat <<'EOF'
feat: improve worker profile の権限境界 (DBパス非提供 + Landlock + 起動拒否)

設計書 §4.6。改善ループ本体・registry の中身はプラン9 — 本task は
「構造的到達不能」の2層防御 (接続情報非提供 + Landlock) のみを実装する。

レビュー反映1回目 (裁定書 F-8/IM-2/P8-04): Landlock allowlist に
sys.prefix(venv)/sys.base_prefix(stdlib) を追加。Landlock適用後の
main()がpydantic/LocalRunner/ToolRegistryを import する際の
PermissionError起動不能を解消。実WorkerRunnerがreadyまで到達する
回帰テストを追加。

レビュー反映1回目 (裁定書 FC-5): 到達不能性プローブの except Exception
を除去しsqlite3.OperationalErrorへnarrow化 (実測: sqlite3.connect失敗は
PermissionErrorではなくOperationalErrorとして観測されることを確認)。
run_holdout_gateのimport成功を別assertionに分離。positive control
(workdir読書き・コードツリー読取) を追加。DBファイルも実測阻害要因を
排除するため有効なSQLiteとしてseedする。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 19: スレッド監督 (watchdog 相互監視) + health ラッチ + 停止状態機械 (`App.close`)

設計書 §5 (停止状態機械) と §6 (スレッド監督・health ラッチ) を実装する。本プランで最後の統合 task — ここまでの全部品 (WorkerRunner・MissionSupervisor・Rag・shell readline seam) を停止シーケンスへ接続する。

**設計判断 (writing-plans)**:
- **`stop_event` は `build_app` の外 (`run_service`) で先に構築し、`build_app` へ渡す** — `WorkerRunner`/`Scheduler` が `stop_event` を必要とするため、`build_app` 内部で新規生成する既存パターンでは `run_service` 側の `_stop_event` テストシームと二重管理になる。`build_app(..., stop_event: threading.Event | None = None)` (既定 None なら内部で新規生成 — 既存の `clock` 引数と同じパターン)
- **health ラッチの検出経路**: `ActivityLog` に `on_write_failure: Callable[[Exception], None] | None = None` を追加する (「write は例外を送出しない」契約は不変 — 失敗時にコールバックを**追加で**呼ぶだけ)。`build_app` がこれを `HealthLatch.record_failure` に配線する
- **WorkerRunner の即時中断**: `stop_event` をコンストラクタで受け取り、`ready_queue`/`done_queue` の待ちを `queue.Queue.get(timeout=deadline)` の 1 回待ちから「短い間隔でポーリングしつつ `stop_event` も見る」形に変更する (停止シーケンス開始時に実行中 Mission への SIGTERM を即座に発行できるようにするため — 設計書 §5 手順 1「同時に実行中 worker へ SIGTERM を発行 (3 と並行開始)」)

**Files:**
- Create: `src/agentic_fx/core/health_latch.py`
- Modify: `src/agentic_fx/activity.py` (`on_write_failure` コールバック追加)
- Modify: `src/agentic_fx/commands.py` (`health_latch` 追加 + `_status` 表示)
- Modify: `src/agentic_fx/core/scheduler.py` (`stop_event` 対応 — hooks/Mission 起動判定のスキップ)
- Modify: `src/agentic_fx/runners/worker_runner.py` (`stop_event` 対応 — 即時中断)
- Modify: `src/agentic_fx/service.py` (全体 — `App.close`/`build_app`/`run_service` の統合)
- Test: `tests/core/test_health_latch.py`, `tests/test_app_close.py`, `tests/test_stop_sequence.py` (新規)

**Interfaces:**
- Produces:
  - `health_latch.HealthLatch` — `record_failure(self, reason: str) -> None` / `is_latched(self) -> bool` / `summary(self) -> list[str]`。**ラッチは解除しない** (プロセス再起動でのみクリア — インスタンスの生涯を通じて `_reasons` は増えるだけ)
  - `ActivityLog.__init__(self, path: Path, *, on_write_failure: Callable[[Exception], None] | None = None)` — 失敗時、技術ログ warning に加えて `on_write_failure(e)` を (例外を握って) 呼ぶ
  - `Commands.__init__(..., health_latch: HealthLatch)` — `_status()` の出力に `health_latch.is_latched()` が True なら `"health: LATCHED (<summary の先頭 3 件>)"` を追記する
  - `Scheduler.__init__(..., stop_event: threading.Event | None = None)` — 新設 kwarg (既定 None = 常時 hooks/Mission 起動判定を実行、既存テスト互換)。`tick()` の全 `_run_hooks` 呼び出しサイトと `_trade_mission_due` 呼び出しを `stop_event.is_set()` でガードする (決定論ブロックは常に実行)
  - `WorkerRunner.__init__(..., stop_event: threading.Event | None = None)` — 新設 kwarg。`ready_queue`/`done_queue` の待ちを `stop_event` も見るポーリングへ変更 (既定 None = 従来どおり単純 timeout 待ち)
  - `MissionSupervisor.heartbeat: float` (Task 13 で既存) — watchdog が鮮度監視に使う
  - `App.close(self, *, busy_resources: frozenset[str] = frozenset()) -> list[str]` — 停止状態機械の資源終端 (§5 手順 2〜6)。戻り値は「close をスキップした資源名のリスト」(空なら全て close 完了)。**(裁定書 FC-2 前半申し送り) `instance_lock` の close (flock 解放) もこの resources リストに含める** — Task 11 で `App.instance_lock` フィールドは追加済みだが解放は本 task に委ねられている
    - **(Task 13 レビュー 2 周からの申し送り — 本 task で必ず回収すること)** `MissionSupervisor` は **supervisor スレッドが例外で死んだときに誰も気づかない**。`fail_pending(exc=...)` は Task 13 で用意済みだが**呼び出し元が存在しない**ため、現状はスレッド死亡後の `try_submit` が永久に `None` を返し続ける (`_busy` が `True` で固着)。watchdog に **`supervisor.is_alive()` の監視 + 死亡時の `fail_pending` 呼び出し**を配線すること。Task 13 単体ではこの経路をテストできない (watchdog が無いため) ので、**本 task で「スレッドを殺す → pending Future が例外完了する → 以降 fail fast する」を通しで pin すること**
    - **(同上) `conn_supervisor` は Task 13 で `connect_readonly` により構築されたが誰も close していない。** `App.close()` の resources リストに含めること (`instance_lock` と同じ扱い)
    - **(Task 12 レビュー 2 周からの申し送り)** `Scheduler.tick` は本タスクで **`try/finally` 化**され、`finally` で `_run_hooks(now)` を呼ぶ。`finally` は **`BaseException` (`KeyboardInterrupt`/`SystemExit`) でも走る**ので、**停止処理の最中に news の HTTP 取得が走りうる**。本 task の停止状態機械で **`_run_hooks` に停止フラグのガードを入れること** (`stop_event` が立っていたら hooks を飛ばす)。現状は許容だが、停止時間の上限を壊す経路になる
    - **(Task 11 レビュー 2 周からの申し送り — E2E で回収すること)** Task 11 の検証は**すべて同一プロセス内の 2 接続**で行った。`instance_lock` が防ぐ本番の競合 (**別プロセスの二重起動**) と `recover_interrupted` の `BEGIN IMMEDIATE` が防ぐ**マルチプロセスの lock upgrade race** は、単体テストでは原理的に再現できていない。本 task の E2E で **`subprocess` による実プロセス 2 本の同時起動**を 1 本張ること。①後発が `InstanceAlreadyRunning` で非ゼロ終了する ②先発の `running` mission が壊れていない、の 2 点を見る (3 周目レビューではなく E2E で扱う、という 2026-08-08 の指揮者判断)
    - **(同上) `except BaseException` 内の `instance_lock.close()` は `try/except` で包んである** (解放の失敗が元の失敗原因を隠さないため — `close()` の例外が伝播すると元の例外は `__context__` に退避されるだけで、`except` 節や終了コード判定は新しい例外を見る)。`App.close()` 側で同じ形を組むときも**同じ扱い**にすること
  - `App.stop_event: threading.Event` / `App.health_latch: HealthLatch` (新設フィールド)。**(裁定書 F-3 / CR-1 advisor 指摘反映 追加) `App.watchdog_heartbeat: float`** — watchdog スレッドが 30 秒周期ループの毎回先頭で touch する。scheduler 側の相互監視 (CR-6) が鮮度チェックに使う。`build_app` は起動直後の誤 fatal を避けるため `time.monotonic()` で初期化する (0/None にしない)。**(裁定書 IM-4 / P8-06 追加) `App.fatal_reason: str | None`** — `_record_fatal` が設定する。存在すれば join 成否によらず終了コードは 1 (`_exit_code` 参照)
  - `service._watchdog_check(app: App, scheduler_thread_obj: threading.Thread, stop_event: threading.Event, *, heartbeat_grace_sec: float = 30.0, dispatch_ceiling_sec: float | None = None) -> None` — モジュールレベル関数 (`run_service` のクロージャに閉じ込めず単体テスト可能にする)。1 回分の生存・heartbeat 鮮度チェック。**(裁定書 F-3 / CR-1 advisor 指摘反映)** `heartbeat_grace_sec` は heartbeat ポンプ間隔 (Task 13, 既定 5.0s) に対してのみ余裕を見た小さい値 (既定 30.0s = 6 倍) に変更 — 旧既定 90.0s は「Mission サイクル全体を包含する」誤った前提に基づいていた。**さらに `busy_since`/`dispatch_ceiling_sec` による独立した第二の軸のチェックを追加** (heartbeat ポンプが「genuine なデッドロック」を隠蔽する fail-open の穴を塞ぐ — Task 13 参照)。`dispatch_ceiling_sec` が `None` なら `_default_dispatch_ceiling_sec(app)` で `app.settings` から算出する
  - `service._default_dispatch_ceiling_sec(app: App) -> float` — **(裁定書 F-3 / CR-1 advisor 指摘反映 新設)** `(llama_swap.timeout_sec + worker.worker_grace_sec + worker.worker_terminate_grace_sec) × 4 + 60.0` (4 = trade 1 回 + reflection バッチ最大 3 回、設計書 §3.3。+60s はスケジューリング揺らぎのマージン)。dispatch 全体の壁時計上限であり Mission 実行時間そのものには依存しない設定由来の定数
  - `service._check_watchdog_health(app: App, watchdog_thread_obj: threading.Thread, stop_event: threading.Event, *, wd_heartbeat_grace_sec: float = 90.0) -> None` — **(裁定書 F-2/F-3 / CR-6 / P8-05 新設)** scheduler 側から watchdog の生存・heartbeat 鮮度を確認する (設計書 §6「scheduler tick が watchdog の heartbeat を相互確認する」)。異常時は `_record_fatal` を呼ぶ
  - `service._record_fatal(app: App, stop_event: threading.Event, reason: str) -> None` — fatal event の記録 + 通知 + `stop_event.set()` (App.close は実行しない)。**(裁定書 IM-4 / P8-06 追加) `app.fatal_reason = reason` もここでラッチする** (未設定なら — 最初の fatal 理由を保持し、以後の呼び出しでは上書きしない)
  - `service._exit_code(app: App, scheduler_alive: bool, supervisor_alive: bool) -> int` — **(裁定書 IM-4 / P8-06 新設)** `app.fatal_reason is not None` なら join 成否に関係なく `1`。それ以外は `1 if (scheduler_alive or supervisor_alive) else 0` (現行の join タイムアウト判定を維持)。`run_service` の末尾はこの関数の戻り値をそのまま使う (単体テスト可能にするため `run_service` 内クロージャに閉じ込めない)
  - `service._busy_resources_after_join(scheduler_still_busy: bool, supervisor_still_busy: bool) -> frozenset[str]` — **(裁定書 F-6 / IM-6 新設)** `App.close(busy_resources=...)` に渡す集合の判定を純関数として抽出。supervisor still-busy なら `conn_core` に加え `conn_supervisor` も busy に含める

- [ ] **Step 1: 失敗するテストを書く (`HealthLatch`)**

`tests/core/test_health_latch.py` を新規作成:

```python
"""HealthLatch (プラン8, 設計書 §6)。"""
from __future__ import annotations

from agentic_fx.core.health_latch import HealthLatch


def test_starts_unlatched():
    latch = HealthLatch()
    assert latch.is_latched() is False
    assert latch.summary() == []


def test_record_failure_latches_and_never_unlatches():
    latch = HealthLatch()
    latch.record_failure("disk full")
    assert latch.is_latched() is True
    assert latch.summary() == ["disk full"]
    latch.record_failure("second failure")
    assert latch.summary() == ["disk full", "second failure"]
    # 解除する公開 API が存在しないこと自体がテスト (grep で確認 — reset
    # メソッドが無いことをレビューで固定する)
```

- [ ] **Step 2: テスト実行して FAIL を確認 → 実装 → PASS**

```bash
uv run pytest tests/core/test_health_latch.py -q
```

`src/agentic_fx/core/health_latch.py` を新規作成:

```python
"""App 全体の latched health 状態 (プラン8, 設計書 §6)。

activity 書き込み失敗 (ディスクフル等) を記録する。**ラッチは解除しない**
(プロセス再起動でのみクリア — 「一度でも記録が欠けた稼働」を人間が確実に
知るため)。
"""
from __future__ import annotations

import threading


class HealthLatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reasons: list[str] = []

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._reasons.append(reason)

    def is_latched(self) -> bool:
        with self._lock:
            return bool(self._reasons)

    def summary(self) -> list[str]:
        with self._lock:
            return list(self._reasons)
```

```bash
uv run pytest tests/core/test_health_latch.py -q
```

Expected: PASS。

- [ ] **Step 3: `activity.py` に `on_write_failure` を追加**

`tests/test_activity.py` (既存ファイルを確認) に以下を追加する:

```python
def test_on_write_failure_callback_invoked_on_write_error(tmp_path, monkeypatch):
    from agentic_fx.activity import ActivityLog, Category

    calls = []
    log = ActivityLog(tmp_path / "a.log", on_write_failure=calls.append)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(log._path), "open", boom)
    log.write(Category.SYSTEM, "x", "y")  # 例外を送出しない契約は不変
    assert len(calls) == 1
    assert isinstance(calls[0], OSError)
```

`src/agentic_fx/activity.py` の `ActivityLog.__init__` を以下に変更する:

```python
    def __init__(self, path: Path, *,
                 on_write_failure: Callable[[Exception], None] | None = None) -> None:
        self._path = path
        self._on_write_failure = on_write_failure
        path.parent.mkdir(parents=True, exist_ok=True)
```

`write` の `except Exception as e:` ブロック末尾 (`_log.warning(...)` の直後) に追加する:

```python
            if self._on_write_failure is not None:
                try:
                    self._on_write_failure(e)
                except Exception:  # noqa: BLE001 — write は送出しない契約を守る
                    _log.exception("on_write_failure callback failed")
```

import 節に `from typing import Callable` を追加する。

```bash
uv run pytest tests/test_activity.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 4: `commands.py` に `health_latch` を配線**

`Commands.__init__` (27-34 行) に `health_latch: HealthLatch` を追加し `self.health_latch = health_latch` を保持する。`_status` (86-95 行) の戻り文字列末尾に以下を追加する:

```python
    def _status(self) -> str:
        s = self.state.load()
        balance, equity = self.broker.equity()
        active = orders.list_by_status(self.conn, "open", "pending_fill",
                                       "protection_pending")
        recent = missions.recent(self.conn, 1)
        last = (f"{recent[0]['loop']}:{recent[0]['status']} "
                f"({recent[0]['started_at']})") if recent else "なし"
        health = (f"\nhealth: LATCHED ({'; '.join(self.health_latch.summary()[:3])})"
                  if self.health_latch.is_latched() else "")
        return (f"mode: {s.mode.value} / autopilot: "
                f"{'on' if s.autopilot else 'off'} / kill switch: "
                f"{'LATCHED' if s.kill_switch_latched else 'ok'}\n"
                f"残高: {balance:,.0f} / エクイティ: {equity:,.0f}\n"
                f"アクティブ orders: {len(active)}\n"
                f"直近 mission: {last}{health}")
```

`import` 節に `from agentic_fx.core.health_latch import HealthLatch` を追加する (型ヒント用)。`tests/test_commands.py` の `Commands(...)` 構築箇所すべてに `health_latch=HealthLatch()` を追加する (`grep -n "Commands(" tests/test_commands.py` で洗い出す)。

```bash
uv run pytest tests/test_commands.py -q
```

Expected: PASS (既存テストの `_status` 出力アサーションが完全一致比較をしていれば、latch 未発火時は `health` が空文字列のため無変更で通ることを確認する)。

- [ ] **Step 5: `scheduler.py` に `stop_event` を配線**

`Scheduler.__init__` (39-47 行) に `stop_event: threading.Event | None = None` を追加し `self._stop_event = stop_event` を保持する。import 節に `import threading` を追加する。`tick()` 内、`_run_hooks` の 3 呼び出しサイト (市場閉鎖時・mark-to-market 失敗時・通常経路) をそれぞれ以下の形にガードする:

```python
        if not self._stopping():
            self._run_hooks(now)
```

(市場閉鎖時・mark-to-market 失敗時の 2 箇所も同様に `if not self._stopping(): self._run_hooks(now)` へ変更する。)

通常経路の Mission 起動判定 (`reason = self._trade_mission_due(now)`) を以下に変更する:

```python
        reason = None if self._stopping() else self._trade_mission_due(now)
```

`_run_hooks` メソッドの直前に追加する:

```python
    def _stopping(self) -> bool:
        """設計書 §5 手順1: 新規受付停止 (stop_event セット後) は hooks・
        Mission 起動判定をスキップする。決定論ブロック (mark-to-market〜
        exits) は stop_event の有無に関わらず必ず実行する (最後の tick
        まで資金保護を続ける)。"""
        return self._stop_event is not None and self._stop_event.is_set()
```

```bash
uv run pytest tests/core/test_scheduler.py tests/core/test_scheduler_tick_order.py \
  tests/core/test_scheduler_signal.py -q
uv run pytest -q
```

Expected: 全件 PASS (`stop_event` 未指定の既存テストは `_stopping()` が常に False を返すため無変更で動く)。

- [ ] **Step 6: `worker_runner.py` に `stop_event` を配線**

`WorkerRunner.__init__` (Task 10) に `stop_event: threading.Event | None = None` を追加し `self._stop_event = stop_event` を保持する。`_run_with_child` 内の ready 待ち・result 待ちを以下のポーリング形式に変更する。

**(前半 (Task 1〜11) からの申し送り — 対応済み)** Task 19 執筆時点でこの Step の `finally` 節に Task 10 修正前の古いコード (`rpc_executor.shutdown(wait=False)` を含む — ThreadPoolExecutor 版の名残) が埋め込まれており、Task 10 の現行コード (FC-1 の daemon スレッド化 + IM-7 の `dispatcher.join` 優先 + `stdin_lock` 排他) と不整合だった。以下は Task 10 の現行 `_run_with_child` (4103-4153 行付近) の `finally` 節と**完全に同一**の内容に更新済み — `dispatch_queue.put(None)` → `_ensure_dead` → `reader.join` → **`dispatcher.join(timeout=w.rpc_timeout_sec + 5.0)`** (IM-7: dispatcher が `stdin_lock` 保持中に `proc.stdin.close()` と競合しないよう、close より先に join する) → `with stdin_lock: proc.stdin.close()` → `proc.stdout.close()`、の順序を厳守する (`rpc_executor`/`ThreadPoolExecutor` は本プランに存在しない — FC-1 対応で使い捨て daemon スレッドに置換済み):

```python
        status = "failed"
        output = None
        try:
            ready = self._wait_with_stop(
                ready_queue, deadline_sec=w.worker_startup_timeout_sec)
            if ready is None:
                self._escalate_kill(proc, w)
                return MissionResult("failed", None, transcript)
            if not ready.get("ok", False):
                status = "failed"
                return MissionResult(status, None, transcript)

            deadline_budget = mission.timeout_sec + w.worker_grace_sec
            done = self._wait_with_stop(done_queue, deadline_sec=deadline_budget)
            if done is None:
                self._escalate_kill(proc, w)
                return MissionResult("timeout", None, transcript)
            kind, payload = done

            if kind == "result":
                status = payload["status"]
                output = payload.get("output")
            else:
                status = "failed"
                output = None
            return MissionResult(status, output, transcript)
        finally:
            # Task 10 (FC-1 の daemon スレッド化 + IM-7 の join 順序) の
            # 現行 finally 節と同一 — rpc_executor/ThreadPoolExecutor は
            # 存在しない。dispatcher を stdin close より先に join する
            # (dispatcher が stdin_lock 保持中の書込と競合しないため)。
            dispatch_queue.put(None)
            self._ensure_dead(proc, w)
            reader.join(timeout=5.0)
            dispatcher.join(timeout=w.rpc_timeout_sec + 5.0)
            with stdin_lock:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.stdout.close()
            except OSError:
                pass

    def _wait_with_stop(self, q, *, deadline_sec: float, poll_interval: float = 0.2):
        """`stop_event` が立てば即座に None を返す (中断)。それ以外は
        `queue.Queue.get` を短い間隔でポーリングし、`deadline_sec` 経過
        したら None (timeout)、値が来ればそれを返す (`(kind, payload)`
        タプルまたは `try_submit` の frame dict そのもの)。"""
        deadline = time.monotonic() + deadline_sec
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return q.get(timeout=min(remaining, poll_interval))
            except queue.Empty:
                continue
```

**実装者への注意**: 上記は `_run_with_child` の該当ブロック (Task 10 で書いた `try: ready = ready_queue.get(timeout=...) ...` の部分) を丸ごと置き換える。`ready_queue.get`/`done_queue.get` の直接呼び出し箇所をすべて `self._wait_with_stop(...)` 経由に統一すること。`tests/runners/test_worker_runner.py` の既存テスト (Task 10) は `stop_event` を渡さない (`None`) ため、`poll_interval=0.2` 秒刻みのポーリングに変わっても実質的な待ち時間は不変 (timeout 検出の粒度が最大 0.2 秒粗くなるだけ) — 既存テストが timeout 値を厳密にアサートしていなければ無変更で通るはず。厳密な時間アサートがあれば `poll_interval` 分の許容誤差を追加すること。

```bash
uv run pytest tests/runners/test_worker_runner.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 7: 失敗するテストを書く (`App.close` の所有権ベース skip)**

`tests/test_app_close.py` を新規作成する:

```python
"""App.close の所有権ベース資源終端 (プラン8, 設計書 §5)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fx.service import build_app


def _init_root(tmp_path):
    # 既存の起動系テスト (tests/test_service.py 等) の init ヘルパーに
    # 合わせて実装すること (settings.yaml.example のコピー + init_db)。
    ...


def test_close_closes_all_resources_in_normal_case(tmp_path):
    root = _init_root(tmp_path)
    app = build_app(root)
    skipped = app.close()
    assert skipped == []


def test_close_skips_conn_core_when_scheduler_thread_marked_busy(tmp_path):
    """設計書 §5 手順6: scheduler スタック時の conn_core は close しない
    (使用中 close の未定義動作より fd リークを選ぶ)。App.close は「conn_core
    が使用中かどうか」を呼び出し元 (run_service の join タイムアウト判定)
    から伝えられる形にする — この単体テストでは `busy_resources` 引数
    (Step 実装で確定する実シグネチャに実装者が合わせて書き直すこと) で
    直接指定して skip されることを確認する。
    """
    root = _init_root(tmp_path)
    app = build_app(root)
    skipped = app.close(busy_resources={"conn_core"})
    assert "conn_core" in skipped
```

（`_init_root`/`App.close` の実引数形は Step 8 の実装確定後にこのテストへ反映すること — 先に書いたテストが実装と食い違えば実装者が両方を整合させる。）

- [ ] **Step 8: `service.py` を実装 (`App.close` + `build_app`/`run_service` 統合)**

`App` dataclass に `stop_event: threading.Event` / `health_latch: HealthLatch` フィールドを追加する。**(裁定書 F-3 / CR-1 advisor 指摘反映) `watchdog_heartbeat: float` フィールドも追加する** (watchdog スレッドが touch する — CR-6 の相互監視が使う)。**(裁定書 IM-4 / P8-06) `fatal_reason: str | None` フィールドも追加する** (`_record_fatal` がラッチする)。`build_app` のシグネチャに `stop_event: threading.Event | None = None` を追加し、冒頭 (`clock = clock or SystemClock()` の直後) で `stop_event = stop_event if stop_event is not None else threading.Event()` を確定させる。`activity = ActivityLog(...)` の構築 (263 行) を以下に変更する:

```python
    health_latch = HealthLatch()
    activity = ActivityLog(root / "logs" / "activity.log",
                           on_write_failure=lambda e: health_latch.record_failure(
                               f"activity write failed: {safe_error_text(e)}"))
    # 裁定書 F-3 (CR-1 advisor 指摘反映): watchdog_heartbeat は起動直後の
    # 誤 fatal を避けるため time.monotonic() で初期化する (0/None にしない
    # — scheduler 側の CR-6 相互監視チェックが watchdog スレッド起動前の
    # 一瞬に走っても stale 判定されないようにする)。
    watchdog_heartbeat = time.monotonic()
```

`owns_runner = runner is None` / `if runner is None: runner = WorkerRunner(...)` (Task 10) の**内側**の `WorkerRunner(...)` 呼び出しに `stop_event=stop_event, on_rpc_leak=_on_rpc_leak` を追加する — `if runner is None:` の条件分岐そのものは変更しない (テストが `build_app(root, runner=FakeRunner(...))` で注入するケースを壊さないため)。`_on_rpc_leak` はこのブロックの前で定義しておく:

```python
    def _on_rpc_leak() -> None:
        health_latch.record_failure("RAG RPC dispatcher leaked past rpc_timeout_sec "
                                    "(設計書 §4.3 codex I3-1)")
        stop_event.set()

    owns_runner = runner is None
    if runner is None:
        runner = WorkerRunner(root=root, settings=settings, clock=clock,
                              rag=rag, worker_profile="trade",
                              stop_event=stop_event, on_rpc_leak=_on_rpc_leak)
```

`Commands(...)` の構築に `health_latch=health_latch` を追加する。`App(...)` の構築に `stop_event=stop_event, health_latch=health_latch, watchdog_heartbeat=watchdog_heartbeat, fatal_reason=None` を追加する。

`App` クラスに `close` メソッドを追加する:

```python
    def close(self, *, busy_resources: frozenset[str] = frozenset()) -> list[str]:
        """停止状態機械の資源終端 (設計書 §5 手順 2〜6)。`busy_resources`
        は呼び出し元 (`run_service` の join タイムアウト判定) が「使用中
        と判断した資源名」を伝える — 該当資源は close をスキップする
        (使用中 close の未定義動作より fd リークを選ぶ)。戻り値は close を
        スキップした資源名のリスト (空なら全て close 完了)。

        close 順序は逆順 (runner → rag → conn_supervisor/conn_core/
        conn_shell → instance_lock)。**(裁定書 MN-2 修正) `Notifier` は
        `close()` を持たない (webhook 送信のみの薄いラッパー) ため、この
        リストにも docstring にも含めない** — 旧稿の docstring は
        「→ notifier」を謳っていたが実装の resources リストには元々
        含まれておらず、記述だけが不整合だった。close できるものはすべて
        close し、1 つの資源の close 失敗が他をブロックしないよう個別に
        隔離する。**(裁定書 FC-2 前半申し送り) `instance_lock` (flock を
        保持するファイルオブジェクト、Task 11) の解放もここで行う** —
        Task 11 では取得のみ行い解放を本 task に委ねていた。
        """
        skipped: list[str] = []
        resources = [
            ("runner", lambda: self.runner.close()
             if self.owns_runner and hasattr(self.runner, "close") else None),
            ("rag", lambda: self.rag.close()),
            ("conn_supervisor", lambda: self.conn_supervisor.close()),
            ("conn_core", lambda: self.conn_core.close()),
            ("conn_shell", lambda: self.conn_shell.close()),
            ("instance_lock", lambda: self.instance_lock.close()),
        ]
        for name, closer in resources:
            if name in busy_resources:
                skipped.append(name)
                continue
            try:
                closer()
            except Exception as e:  # noqa: BLE001 — 1 資源の失敗で他を止めない
                _log.warning("App.close: %s failed: %s", name, safe_error_text(e))
        return skipped
```

`_watchdog_tick` (既存、448-478 行) の直後にモジュールレベル関数を追加する (`run_service` 内のクロージャに閉じ込めず、単体テスト可能にするため — Step 10 の受入テストがこれらを直接呼ぶ)。**(裁定書 IM-4/P8-06, F-2/F-3/CR-6/P8-05, F-3/CR-1 advisor 指摘反映を統合)**:

```python
def _record_fatal(app: App, stop_event: threading.Event, reason: str) -> None:
    """codex C2-3: scheduler/supervisor/watchdog の回復不能死亡は資金保護の
    恒久停止に等しい — 対話モードでも即座に停止シーケンスを開始する。
    fatal event の記録 + 通知 + stop_event セットに加え、**(裁定書 IM-4/
    P8-06) `app.fatal_reason` をラッチする** — 未設定 (None) の場合のみ
    書き込む (最初の fatal 理由を保持し、以後の呼び出しで上書きしない)。
    App.close は実行しない (停止シーケンスの実行主体は常に main、設計書
    §6)。
    """
    if app.fatal_reason is None:
        app.fatal_reason = reason
    try:
        app.activity.write(Category.SYSTEM, "fatal_thread_death", reason)
    except Exception:  # noqa: BLE001
        _log.exception("failed to record fatal_thread_death")
    try:
        app.notifier.send(f"[agentic-fx] 致命的エラー: {reason} — 停止します")
    except Exception:  # noqa: BLE001
        _log.exception("failed to notify fatal_thread_death")
    stop_event.set()


def _default_dispatch_ceiling_sec(app: App) -> float:
    """裁定書 F-3 (CR-1 advisor 指摘反映): supervisor の 1 回の dispatch
    (trade 1 回 + reflection バッチ最大 3 回、設計書 §3.3) が壁時計上
    絶対に超えないはずの上限。WorkerRunner の preemption
    (`mission.timeout_sec + worker_grace_sec`、超過で SIGTERM →
    `worker_terminate_grace_sec` 後 SIGKILL) が Mission 単体の上限を保証
    するため、この式は Mission の実際の実行時間には依存しない設定由来の
    定数になる。+60s はスケジューリング揺らぎのマージン。"""
    w = app.settings.worker
    per_mission = (app.settings.llama_swap.timeout_sec
                   + w.worker_grace_sec + w.worker_terminate_grace_sec)
    return per_mission * 4 + 60.0  # 4 = trade 1 + reflection 最大 3


def _watchdog_check(app: App, scheduler_thread_obj: threading.Thread,
                    stop_event: threading.Event, *,
                    heartbeat_grace_sec: float = 30.0,
                    dispatch_ceiling_sec: float | None = None) -> None:
    """1 回分の watchdog チェック (設計書 §6)。scheduler/supervisor の
    生存・heartbeat 鮮度を見て、異常があれば `_record_fatal` を呼ぶ。
    呼び出し元 (`run_service` の `watchdog_thread`) が 30 秒周期のループを
    持つ — このテストで直接叩けるよう周期そのものはこの関数の外に置く。

    **(裁定書 F-3 / CR-1 advisor 指摘反映)** `heartbeat_grace_sec` の既定
    30.0s は Task 13 の heartbeat ポンプ間隔 (既定 5.0s) に対してのみ
    余裕を見た値 — 旧既定 90.0s は「Mission サイクル全体を包含する」
    誤った前提に基づいていた。**ただし heartbeat ポンプは supervisor
    スレッドが生きてさえいれば touch し続けるため、`_dispatch` 内部
    (broker.submit 等) が genuine にデッドロックした場合を heartbeat 鮮度
    だけでは検出できない (fail-open の穴)。これを塞ぐため
    `busy_since`/`dispatch_ceiling_sec` による独立した第二の軸を追加する**
    — heartbeat が新鮮でも `busy_since` からの経過が
    `dispatch_ceiling_sec` を超えていれば fatal とする。
    """
    if not scheduler_thread_obj.is_alive():
        _record_fatal(app, stop_event, "scheduler thread is dead")
        return
    if not app.supervisor.is_alive():
        _record_fatal(app, stop_event, "supervisor thread is dead")
        app.supervisor.fail_pending(exc=RuntimeError("supervisor thread died"))
        return
    if time.monotonic() - app.supervisor.heartbeat > heartbeat_grace_sec:
        _record_fatal(app, stop_event, "supervisor heartbeat stale")
        return
    busy_since = app.supervisor.busy_since
    ceiling = (dispatch_ceiling_sec if dispatch_ceiling_sec is not None
              else _default_dispatch_ceiling_sec(app))
    if busy_since is not None and time.monotonic() - busy_since > ceiling:
        _record_fatal(
            app, stop_event,
            f"supervisor dispatch exceeded ceiling ({ceiling:.0f}s) — "
            "heartbeat pump was fresh but dispatch did not return "
            "(裁定書 F-3 advisor 指摘反映)")
        app.supervisor.fail_pending(exc=RuntimeError("supervisor dispatch hung"))
        return
    _watchdog_tick(app)


def _check_watchdog_health(app: App, watchdog_thread_obj: threading.Thread,
                           stop_event: threading.Event, *,
                           wd_heartbeat_grace_sec: float = 90.0) -> None:
    """裁定書 F-2 (CR-6/P8-05): 設計書 §6「scheduler tick が watchdog の
    heartbeat を相互確認する」— watchdog 自身が (例外処理の想定漏れ等で)
    静かに停止した場合、これまでは scheduler/supervisor の死亡検出手段が
    完全に失われていた。scheduler 側から watchdog の生存・heartbeat 鮮度
    (30 秒周期ループに対し 90s = 3 倍の余裕) を確認し、異常なら
    `_record_fatal` を呼ぶ (同じ latch/停止経路に接続 — IM-4 の
    fatal_reason はこの呼び出しもカバーする)。
    """
    if not watchdog_thread_obj.is_alive():
        _record_fatal(app, stop_event, "watchdog thread is dead")
        return
    if time.monotonic() - app.watchdog_heartbeat > wd_heartbeat_grace_sec:
        _record_fatal(app, stop_event, "watchdog heartbeat stale")


def _busy_resources_after_join(scheduler_still_busy: bool,
                               supervisor_still_busy: bool) -> frozenset[str]:
    """裁定書 F-6 (IM-6): join タイムアウト時にどの接続を busy (close
    スキップ対象) とするかを判定する純関数 (単体テスト可能にするため
    `run_service` から抽出)。supervisor は commit-pre 相 (lock 非保持) で
    `conn_supervisor` を使うため、supervisor が join タイムアウトで
    still-busy なら `conn_supervisor` も busy に含める — 旧稿は
    `conn_core` のみを対象にしており `conn_supervisor` が無条件に close
    されうる欠陥があった。scheduler は `conn_supervisor` を使わない。
    """
    busy: set[str] = set()
    if scheduler_still_busy:
        busy.add("conn_core")
    if supervisor_still_busy:
        busy.add("conn_core")
        busy.add("conn_supervisor")
    return frozenset(busy)


def _exit_code(app: App, scheduler_alive: bool, supervisor_alive: bool) -> int:
    """裁定書 IM-4 (P8-06): 終了コード判定を `is_alive()` だけに頼らない。
    `app.fatal_reason` がラッチされていれば (回復不能死亡が一度でも検出
    されていれば)、その後 join が正常に完了していても非ゼロ終了する
    (設計書 §6「回復不能死亡は非ゼロ終了」)。daemon/対話の両モードで
    同じ判定を使う (モード分岐しない — この判定自体がモード非依存である
    ことが「両モードで停止する」の担保)。
    """
    if app.fatal_reason is not None:
        return 1
    return 1 if (scheduler_alive or supervisor_alive) else 0
```

**(裁定書 FC-4 — 前回レビューで判明した誤り)** `run_service` を以下の状態機械へ再構成する。**置換範囲は「既存の `stop_event = _stop_event if ...` 行から末尾まで」ではない** — 現行 `run_service` (`service.py:480-`) は `app = build_app(root)` (491 行) が `stop_event = _stop_event if ... ` (500 行) より**前**にある。指示どおり 500 行から末尾だけを置換すると、旧 `app = build_app(root)` (491 行、`stop_event` 未指定) が残ったまま、置換後コードの冒頭に新しい `app = build_app(root, stop_event=stop_event)` が追加され、**`build_app` が二重実行される** (DB 二重 open・instance lock 二重取得で 2 回目が `InstanceAlreadyRunning` を送出し起動不能になる)。**置換範囲を `app = build_app(root)` (491 行) から末尾までに繰り上げる** — `settings = load_settings(...)` (488 行)・`setup_technical_logging(...)` (489-490 行) は現行のまま残し (1 回だけ実行)、`app = build_app(root)` 以降 (491 行〜) を以下に差し替える:

```python
    stop_event = _stop_event if _stop_event is not None else threading.Event()
    app = build_app(root, stop_event=stop_event)

    warning = Policy(root / "policy" / "directives.md").size_warning()
    if warning:
        print(warning)
    print(build_splash(app))
    app.activity.write(Category.SYSTEM, "service_started", f"daemon={daemon}")

    scheduler_busy = threading.Event()  # tick 実行中フラグ (join timeout 判定用)

    def scheduler_thread() -> None:
        last = 0.0
        while not stop_event.is_set():
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                if stop_event.is_set():
                    break
                scheduler_busy.set()
                try:
                    with app.core_lock:
                        app.scheduler.tick(app.clock.now())
                except Exception:  # noqa: BLE001
                    _log.exception("tick failed")
                finally:
                    scheduler_busy.clear()
                # 裁定書 F-2 (CR-6/P8-05): scheduler tick 毎に watchdog
                # 自身の生存・heartbeat 鮮度を確認する (設計書 §6 の
                # 相互監視の scheduler→watchdog 方向)。tick の決定論
                # ブロックより後 (資金保護そのものはこのチェックに依存
                # しない) — wd は下で定義されるが、この関数はクロージャ
                # として遅延評価されるため定義順の問題はない。
                try:
                    _check_watchdog_health(app, wd, stop_event)
                except Exception:  # noqa: BLE001
                    _log.exception("watchdog health check failed")
            stop_event.wait(1)

    def watchdog_thread() -> None:
        """設計書 §6: scheduler/supervisor の heartbeat 鮮度・生存を
        30 秒周期で監視する。1 回分のチェックはモジュールレベル関数
        `_watchdog_check` に委譲する (クロージャに閉じ込めず単体テスト
        可能にするため — Task 19 の受入テストがこの関数を直接呼ぶ)。"""
        while not stop_event.is_set():
            # 裁定書 F-2/F-3 (CR-6): scheduler 側の相互監視が読む
            # app.watchdog_heartbeat をループ先頭で touch する。
            app.watchdog_heartbeat = time.monotonic()
            stop_event.wait(30)
            if stop_event.is_set():
                break
            app.watchdog_heartbeat = time.monotonic()
            try:
                _watchdog_check(app, th, stop_event)
            except Exception:  # noqa: BLE001 — スレッドを殺さない
                _log.exception("watchdog tick failed")

    if daemon:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    app.supervisor.start()
    th = threading.Thread(target=scheduler_thread, daemon=True)
    th.start()
    wd = threading.Thread(target=watchdog_thread, daemon=True)
    wd.start()

    try:
        if daemon:
            while not stop_event.is_set():
                try:
                    stop_event.wait(1)
                except KeyboardInterrupt:
                    stop_event.set()
        else:
            from agentic_fx.shell import run_shell
            run_shell(app.commands, stop_event)
            if stop_event.is_set() and not daemon:
                # readline 中断 seam 経由で watchdog の fatal 検出により
                # 起こされた場合、対話モードは shell へ警告を表示してから
                # 終了する (codex N4-1 — daemon は activity+Notifier のみ、
                # 対話はそれに加えて shell 表示)。
                print("警告: システムスレッドの異常を検出したため停止します。"
                      "詳細は activity ログを確認してください。")
    finally:
        # ---- 停止状態機械 (設計書 §5) — 実行主体は常に main ----
        stop_event.set()  # 手順1: 新規受付停止 (べき等 — 既にセット済みでも安全)
        app.supervisor.shutdown(
            drain_exc=RuntimeError("service shutting down"))  # 手順2
        th.join(timeout=30)  # 手順3: scheduler join を先に
        scheduler_still_busy = scheduler_busy.is_set() and th.is_alive()

        supervisor_busy = app.supervisor.is_alive()
        app.supervisor.join(timeout=app.settings.worker.shutdown_join_timeout_sec)  # 手順4
        supervisor_still_busy = supervisor_busy and app.supervisor.is_alive()

        wd.join(timeout=15)

        # 裁定書 F-6 (IM-6): busy 判定は _busy_resources_after_join に
        # 委譲する (conn_supervisor も対象に含める — 旧稿の欠陥修正)。
        skipped = app.close(
            busy_resources=_busy_resources_after_join(
                scheduler_still_busy, supervisor_still_busy))  # 手順5-6
        if skipped:
            app.activity.write(Category.SYSTEM, "close_skipped_resources",
                               f"{skipped} (join timeout — used-in-flight)")

        if th.is_alive() or app.supervisor.is_alive():
            app.activity.write(Category.SYSTEM, "service_stopped",
                               "shutdown_timeout (Mission 継続中の可能性)")
        else:
            app.activity.write(Category.SYSTEM, "service_stopped", "graceful")

    # 裁定書 IM-4 (P8-06): 終了コードは is_alive() だけでなく
    # app.fatal_reason (watchdog/scheduler 相互監視・supervisor dispatch
    # ceiling いずれかが検出した回復不能死亡) も考慮する。_exit_code は
    # join 成否に関係なく fatal_reason があれば 1 を返す。
    rc = _exit_code(app, th.is_alive(), app.supervisor.is_alive())
    if rc == 1:
        print("警告: 停止タイムアウト、または致命的なスレッド異常を検出しました。"
              "実行中の処理が残っている可能性があります。")
        return 1
    print("停止しました。")
    return 0
```

**実装者への注意**: 上記は設計の骨格であり、実装時に以下を確認・調整すること — ①`watchdog_thread` はクロージャの定義順序に注意 (`th` を参照する `watchdog_thread` は `th` が定義された**後**に実際に呼ばれるが、Python のクロージャは遅延束縛のため定義順は `th = threading.Thread(...)` より前でも動く — 既存コードと同じパターン) ②`app.runner.close()` は Task 10 で `WorkerRunner.close()` が no-op として存在するため `owns_runner`+`hasattr` 判定は `App.close` 内に移した (旧 573-577 行の判定はここに統合され `run_service` 側からは削除する) ③`_check_llama_swap`/`build_splash`/import 節の整合を最終確認すること。

- [ ] **Step 9: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_app_close.py -q
uv run pytest tests/test_service.py tests/test_service_app.py -q
uv run pytest -q
```

Expected: 全件 PASS。`tests/test_service.py`/`tests/test_service_app.py` に `run_service` の shutdown 経路を検証する既存テストがあれば (`grep -n "run_service" tests/test_service*.py`)、新しい停止状態機械の順序 (`th.join` → `supervisor.join` → `wd.join` → `app.close`) に合わせてアサーションを更新すること。

- [ ] **Step 10: 失敗するテストを書く (停止シーケンスの統合確認)**

`tests/test_stop_sequence.py` を新規作成する:

```python
"""停止状態機械の統合確認 (プラン8, 設計書 §5/§6)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.service import run_service


def test_run_service_stops_gracefully_with_pre_set_stop_event(tmp_path):
    """_stop_event を事前にセットして渡すと、run_service は即座に停止
    状態機械を完走する。**(裁定書 IM-4 / P8-06 — 変異耐性)** 新規に生成した
    健全な app には Mission も fatal も存在しないため、戻り値は
    `0` (graceful) に**厳密に一致する**はず — 旧稿の `rc in (0, 1)` は
    `_exit_code` の実装をどう壊しても通ってしまう曖昧な assert だった。"""
    # root の init は既存 tests/test_service.py の init ヘルパーに合わせる
    root = _init_root(tmp_path)  # 既存 fixture 名で置き換えること
    stop_event = threading.Event()
    stop_event.set()
    rc = run_service(root, daemon=True, _stop_event=stop_event)
    assert rc == 0


def test_exit_code_is_1_when_fatal_reason_latched_even_if_threads_joined(tmp_path):
    """裁定書 IM-4 (P8-06) の回帰ピン: fatal_reason がラッチされていれば、
    scheduler/supervisor が両方とも正常 join (is_alive()==False) しても
    終了コードは 1 になる — 旧稿は is_alive() だけを見ており、fatal 検出
    後に join が完了すると誤って 0 を返していた。"""
    from agentic_fx.service import _exit_code, build_app

    root = _init_root(tmp_path)
    app = build_app(root)
    try:
        app.fatal_reason = "supervisor thread is dead"
        assert _exit_code(app, scheduler_alive=False, supervisor_alive=False) == 1
    finally:
        app.close()


def test_exit_code_is_0_when_no_fatal_and_threads_joined(tmp_path):
    from agentic_fx.service import _exit_code, build_app

    root = _init_root(tmp_path)
    app = build_app(root)
    try:
        assert app.fatal_reason is None
        assert _exit_code(app, scheduler_alive=False, supervisor_alive=False) == 0
    finally:
        app.close()


def test_busy_resources_after_join_includes_conn_supervisor_when_supervisor_busy():
    """裁定書 F-6 (IM-6) の回帰ピン: supervisor still-busy なら
    conn_supervisor も busy に含める (commit-pre 相が lock 非保持で
    conn_supervisor を使うため)。"""
    from agentic_fx.service import _busy_resources_after_join

    busy = _busy_resources_after_join(
        scheduler_still_busy=False, supervisor_still_busy=True)
    assert busy == frozenset({"conn_core", "conn_supervisor"})

    busy2 = _busy_resources_after_join(
        scheduler_still_busy=True, supervisor_still_busy=False)
    assert busy2 == frozenset({"conn_core"})  # scheduler は conn_supervisor を使わない


def test_check_watchdog_health_records_fatal_when_watchdog_dead(tmp_path):
    """裁定書 F-2 (CR-6/P8-05) の回帰ピン: scheduler 側が watchdog の
    死亡を検出して fatal 経路に接続する (相互監視の scheduler→watchdog
    方向)。"""
    from agentic_fx.service import _check_watchdog_health, build_app

    root = _init_root(tmp_path)
    app = build_app(root)

    class DeadWatchdog:
        def is_alive(self) -> bool:
            return False

    _check_watchdog_health(app, DeadWatchdog(), app.stop_event)

    assert app.stop_event.is_set()
    assert app.fatal_reason is not None
    log_lines = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "watchdog thread is dead" in log_lines


def test_watchdog_check_detects_stale_dispatch_ceiling_even_with_fresh_heartbeat(tmp_path):
    """裁定書 F-3 (CR-1 advisor 指摘反映) の回帰ピン: heartbeat ポンプが
    touch し続けていても (heartbeat は新鮮)、busy_since からの経過が
    dispatch_ceiling_sec を超えていれば fatal になる — heartbeat 鮮度
    チェック単独では genuine なデッドロックを検出できない fail-open の
    穴を塞ぐテスト。"""
    from agentic_fx.service import _watchdog_check, build_app

    root = _init_root(tmp_path)
    app = build_app(root)

    class AliveThread:
        def is_alive(self) -> bool:
            return True

    class StuckSupervisor:
        heartbeat = time.monotonic()  # ポンプが touch し続けている想定
        busy_since = time.monotonic() - 9999.0  # 遥か昔に dispatch 開始

        def is_alive(self) -> bool:
            return True

        def fail_pending(self, *, exc: Exception) -> None:
            calls.append(exc)

    calls: list[Exception] = []
    app.supervisor = StuckSupervisor()

    _watchdog_check(app, AliveThread(), app.stop_event, dispatch_ceiling_sec=60.0)

    assert app.stop_event.is_set()
    assert app.fatal_reason is not None
    assert len(calls) == 1


def test_watchdog_check_records_fatal_and_stops_when_supervisor_dead(tmp_path):
    """設計書 codex C2-3: supervisor スレッド死亡は (daemon/対話の
    モードを問わず) 停止シーケンスを開始する — `_watchdog_check` は
    モード概念を持たない共通のチェック本体であり、`run_service` はどちら
    のモードでも同じ `_watchdog_check` を呼ぶ (Step 8 の `watchdog_thread`
    参照)。「両モードで停止する」は呼び出し経路が daemon/対話で分岐しない
    ことそのもので保証される — この 1 本で `_watchdog_check` 自体の挙動
    (supervisor 死亡検出 → fatal 記録 → stop_event セット → pending
    Future の例外完了) を直接検証する。
    """
    from agentic_fx.service import _watchdog_check, build_app

    root = _init_root(tmp_path)  # 既存 fixture 名で置き換えること
    app = build_app(root)
    stop_event = app.stop_event

    class DeadThread:
        def is_alive(self) -> bool:
            return True  # scheduler は生きている

    class DeadSupervisor:
        heartbeat = 0.0

        def is_alive(self) -> bool:
            return False  # supervisor が死んでいる

        def fail_pending(self, *, exc: Exception) -> None:
            calls.append(exc)

    calls: list[Exception] = []
    app.supervisor = DeadSupervisor()

    _watchdog_check(app, DeadThread(), stop_event)

    assert stop_event.is_set()
    assert len(calls) == 1
    assert isinstance(calls[0], RuntimeError)
    # activity に fatal_thread_death が記録されていること
    log_lines = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "fatal_thread_death" in log_lines
    assert "supervisor thread is dead" in log_lines


def test_watchdog_check_detects_scheduler_death_before_supervisor(tmp_path):
    """scheduler thread の死亡は supervisor より先に検査され、同じ
    fatal 経路 (stop_event セット) に落ちる。"""
    from agentic_fx.service import _watchdog_check, build_app

    root = _init_root(tmp_path)
    app = build_app(root)

    class DeadThread:
        def is_alive(self) -> bool:
            return False

    _watchdog_check(app, DeadThread(), app.stop_event)

    assert app.stop_event.is_set()
    log_lines = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "scheduler thread is dead" in log_lines
```

- [ ] **Step 11: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_stop_sequence.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 12: 変異テスト**

1. `HealthLatch` に `reset()` メソッドを追加する変異を行い、`_reasons` をクリアするコードを書いてから `test_record_failure_latches_and_never_unlatches` 相当が「reset 呼び出し後も summary が空にならない」ことを確認する専用テストを追加する (ラッチ不解除の意図的な破壊テスト)
2. `scheduler.py` の `_stopping()` の呼び出しを `_run_hooks` の 3 箇所から 1 箇所だけ残して削除 → 対応する `test_hooks_run_even_when_market_closed` 等 (Task 12/19 で stop_event を絡めたテストがあれば) が red になることを確認する
3. `_watchdog_check` の `if not app.supervisor.is_alive():` ブロックを削除 → `test_watchdog_check_records_fatal_and_stops_when_supervisor_dead` が red
4. `App.close` の `if name in busy_resources:` チェックを削除 → `test_close_skips_conn_core_when_scheduler_thread_marked_busy` が red
5. **(裁定書 IM-4 追加)** `_exit_code` の `if app.fatal_reason is not None: return 1` を削除 → `test_exit_code_is_1_when_fatal_reason_latched_even_if_threads_joined` が red
6. **(裁定書 F-6/IM-6 追加)** `_busy_resources_after_join` の `if supervisor_still_busy: busy.add("conn_supervisor")` 相当行を削除 → `test_busy_resources_after_join_includes_conn_supervisor_when_supervisor_busy` が red
7. **(裁定書 F-2/CR-6 追加)** `_check_watchdog_health` の `if not watchdog_thread_obj.is_alive():` ブロックを削除 → `test_check_watchdog_health_records_fatal_when_watchdog_dead` が red
8. **(裁定書 F-3/CR-1 advisor 指摘反映 追加)** `_watchdog_check` の `busy_since`/`dispatch_ceiling_sec` 判定ブロックを削除 → `test_watchdog_check_detects_stale_dispatch_ceiling_even_with_fresh_heartbeat` が red

- [ ] **Step 13: Commit**

```bash
git add src/agentic_fx/core/health_latch.py src/agentic_fx/activity.py \
  src/agentic_fx/commands.py src/agentic_fx/core/scheduler.py \
  src/agentic_fx/runners/worker_runner.py src/agentic_fx/service.py \
  tests/core/test_health_latch.py tests/test_app_close.py tests/test_stop_sequence.py \
  tests/test_activity.py tests/test_commands.py
git commit -m "$(cat <<'EOF'
feat: スレッド監督 (watchdog相互監視) + health latch + 停止状態機械 (App.close)

設計書 §5/§6。停止の実行主体を main に一意化し、scheduler join優先→
supervisor drain/join→App.closeの所有権ベース資源終端まで一気通貫で実装。
両モードでスレッド死亡時に停止 (codex C2-3)。

レビュー反映1回目 (裁定書 F-2/F-3/CR-6/P8-05): scheduler tick 毎に
watchdog自身の生存・heartbeat鮮度を確認する相互監視のscheduler→watchdog
方向を追加 (これまでwatchdog→scheduler/supervisor方向のみだった)。

レビュー反映1回目 (裁定書 F-3/CR-1 advisor指摘反映): heartbeatポンプ
単独はgenuineなデッドロックを隠蔽するfail-openの穴を持つため、
busy_since/dispatch_ceiling_secによる独立した第二の軸を追加。

レビュー反映1回目 (裁定書 IM-4/P8-06): App.fatal_reasonをラッチし、
_exit_codeがjoin成否によらず1を返すようにする。

レビュー反映1回目 (裁定書 F-6/IM-6): busy_resources判定にconn_supervisor
を追加 (supervisorのcommit-pre相はlock非保持でconn_supervisorを使う)。

レビュー反映1回目 (裁定書 MN-2): App.closeのdocstringからNotifier close
の誤記述を削除。

レビュー反映1回目 (裁定書 FC-2 前半申し送り): instance_lockの解放を
App.closeに配線。

レビュー反映1回目 (裁定書 FC-4): run_serviceの置換範囲の誤り (build_app
二重実行) を修正。

前半 (Task 10) からの申し送り対応: _run_with_childのfinally節の古い
コード (rpc_executor.shutdown) をTask 10現行 (FC-1のdaemonスレッド化 +
IM-7のjoin順序) に合わせて更新。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 20: E2E + 受入条件検証 (設計書 §9 の 8 項目)

設計書 §9 の受入条件を実測・レビューで確定する最終 task。Task 1〜19 で個別に検証済みの性質を、実 subprocess・複数コンポーネント間の統合で改めて実証する (単体テストの積み上げでは検出できない配線欠陥・タイミング窓を狙う)。

**Files:**
- Test: `tests/test_e2e_worker_isolation.py` (新規)
- (レビューのみ・コード変更なし) `risk_gate.py`/`paper_broker.py`/`transitions.py` の diff ゼロ確認

**受入条件 8 項目と対応**:

| # | 受入条件 | 検証方法 |
|---|---|---|
| 1 | ハング注入で kill | 本 task 新規 (実プロセスへの SIGTERM 無視 → SIGKILL エスカレーション実測) |
| 2 | 資金保護継続 (Mission 実行中の SL 到達 → クローズ) | 本 task 新規 (build_app の実 App + FakeRunner 差し替えで統合実証) |
| 3 | improve profile 到達不能 | Task 18 で実測済み (`tests/test_improve_profile_isolation.py`) — 本 task は全体スイートに含まれることの確認のみ |
| 4 | 終端の一意性 (CAS 二重終端拒否 + 起動時同時回収) | Task 11 で実測済み (`tests/store/test_missions_cas.py`) — 同上 |
| 5 | スレッド死亡 → 両モード停止 + App.close 全経路 + shutdown 時 pending Future 例外完了 | Task 19 で実測済み (`tests/test_stop_sequence.py`/`tests/test_app_close.py`) — 同上 |
| 6 | tick 順序契約の回帰ピン + commit-core 鮮度再検証 | Task 12/14 で実測済み (`tests/core/test_scheduler_tick_order.py`/`tests/core/test_executor_snapshot.py`) — 同上 |
| 7 | 決定論的コア diff ゼロ | 本 task でレビューコマンド実行 (下記 Step) |
| 8 | 既存 1404+ tests green | 本 task で全体実行 |

- [ ] **Step 1: 失敗するテストを書く (ハング注入 → SIGTERM 無視 → SIGKILL)**

`tests/test_e2e_worker_isolation.py` を新規作成する:

```python
"""プラン8 E2E — 設計書 §9 受入条件 1・2 (実 subprocess・実統合)。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.config import load_settings

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example"


def test_worker_runner_kills_process_that_ignores_sigterm(tmp_path):
    """受入条件 1: SIGTERM を無視するプロセスに対し、
    worker_terminate_grace_sec 経過後に SIGKILL が実際に効く
    (実プロセス — シェルの trap でSIGTERM を無視させる)。
    """
    from agentic_fx.runners.worker_runner import WorkerRunner
    from agentic_fx.config import WorkerSettings

    proc = subprocess.Popen(
        ["sh", "-c", "trap '' TERM; sleep 60"],
        start_new_session=True)
    w = WorkerSettings(worker_terminate_grace_sec=1.0)

    runner = WorkerRunner.__new__(WorkerRunner)  # コンストラクタを経由せず
                                                  # _escalate_kill/_kill の
                                                  # OS レベル機構だけを直接
                                                  # 検証する (mission worker
                                                  # プロトコル一式を経由しない
                                                  # 最小構成)
    start = time.monotonic()
    runner._escalate_kill(proc, w)
    elapsed = time.monotonic() - start

    assert proc.poll() is not None  # 実際に死んでいる
    assert elapsed < 5.0  # grace(1s) + kill 完了が数秒以内


def test_funds_protection_continues_during_blocked_mission(tmp_path):
    """受入条件 2 (**裁定書 F-11 / IM-5 / P8-07 反映**): Mission 実行中
    (WorkerRunner がブロック中) でも、scheduler tick の SL/TP 監視
    (_process_exits) が core_lock を取得できて実行される — **本番配線
    (scheduler.tick → on_trade_mission → MissionSupervisor.try_submit →
    _trade_fn → TradeLoop.run_once) を経由**して統合実証する。

    旧稿は `app.trade_loop.run_once("cron")` を別スレッドから直接呼んで
    おり、scheduler callback (`on_trade_mission`)・supervisor スレッド・
    ジョブ受理判定のいずれかが壊れていても検出できない欠陥があった
    (P8-07)。置き換えるのは `TradeLoop.runner` (WorkerRunner 相当) だけに
    留め、それより上位の scheduler/supervisor/trade_loop の配線はすべて
    実物を使う。
    """
    # 実装方針:
    # 1. build_app(root, clock=FixedClock(...)) で実 App を構築する
    #    (settings.yaml.example ベースの一時 root — 既存 E2E テストの
    #    init ヘルパーに合わせる)
    # 2. app.trade_loop.runner を「.run() 到達を Event で通知してから
    #    別の Event で解放されるまでブロックする」fake に差し替える
    #    (WorkerRunner 相当だけを差し替え、scheduler/supervisor/trade_loop
    #    自体は実物のまま)
    # 3. OPEN 済みの注文 (SL 到達済みのバー) を DB に用意する
    # 4. app.supervisor.start() → app.scheduler.tick(now) を呼び、
    #    scheduler.tick → on_trade_mission → try_submit → _trade_fn →
    #    run_once → runner.run() という本番経路で Mission を起動する。
    #    到達 Event を待って「1 回目の tick が実際に Mission を起動した」
    #    ことを確認する (scheduler→supervisor 配線そのものの検証)。
    # 5. Mission がブロックしたままの状態で、次 tick
    #    (app.scheduler.tick(now + 1分)) を呼び、SL クローズが実行される
    #    こと (orders テーブルの status が 'closed' になること) を確認する
    # 6. release Event をセットし、supervisor.shutdown()+join() で後始末する
    #
    # 既存の Env/fixture (tests/core/test_scheduler.py の SL 到達シナリオ) を
    # 実ファイルで確認して実装すること。
    raise NotImplementedError(
        "実装者が Step 1 完了後、既存 fixture を組み合わせて具体化すること")
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_e2e_worker_isolation.py -q
```

Expected: `test_worker_runner_kills_process_that_ignores_sigterm` は WorkerRunner の実装 (Task 10/19) が既に正しければ **この時点で PASS してよい** (新規実装ではなく既存機構の統合実証のため)。`test_funds_protection_continues_during_blocked_mission` は `NotImplementedError` で FAIL する。

- [ ] **Step 3: `test_funds_protection_continues_during_blocked_mission` を実装**

Step 1 のコメントに書いた方針に従い、以下の形へ具体化する (実ファイルの既存 fixture 名に実装者が合わせること — 下記は骨格)。**(裁定書 F-11 / IM-5 / P8-07)** 直接 `trade_loop.run_once(...)` を呼ぶのではなく、`app.supervisor.start()` → `app.scheduler.tick(...)` という本番配線を経由する:

```python
def test_funds_protection_continues_during_blocked_mission(tmp_path):
    import threading
    from datetime import datetime, timedelta, timezone

    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.core.contracts import OrderStatus as S
    from agentic_fx.runners.base import Mission, MissionResult
    from agentic_fx.service import build_app
    from agentic_fx.store import orders as orders_store

    root = _init_root(tmp_path)  # 既存 E2E テストの init ヘルパーに合わせる
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    clock = FixedClock(now)
    app = build_app(root, clock=clock)

    try:
        # SL 到達済みの OPEN ポジションを 1 件用意する (既存
        # test_scheduler.py の SL/TP テストパターンに合わせて具体的な
        # entry/SL/bar 価格を選ぶ)
        oid = _seed_open_position_with_reachable_sl(app.conn_core, now)

        # 裁定書 F-11 (IM-5/P8-07): 差し替えるのは TradeLoop.runner
        # (WorkerRunner 相当) だけ — scheduler/supervisor/trade_loop の
        # 配線は実物のまま本番経路を通す。
        reached = threading.Event()
        release = threading.Event()

        class BlockingRunner:
            def run(self, mission: Mission) -> MissionResult:
                reached.set()
                release.wait(10.0)
                return MissionResult("completed",
                                     {"action": "hold", "reasoning": "x"}, [])

        app.trade_loop.runner = BlockingRunner()

        # 本番経路で 1 回目の Mission を起動する: scheduler.tick →
        # on_trade_mission → MissionSupervisor.try_submit → _trade_fn →
        # TradeLoop.run_once → runner.run()。scheduler.tick 自体は
        # core_lock を保持したまま実行し即座に return する (supervisor へ
        # 委譲するだけで Mission 完了を待たない — Task 13/15 の設計どおり)。
        app.supervisor.start()
        with app.core_lock:
            app.scheduler.tick(now)
        assert reached.wait(5.0), (
            "1 回目の tick が本番配線経由で Mission を起動しなかった "
            "(scheduler→on_trade_mission→supervisor.try_submit の配線を疑う)")

        # Mission (BlockingRunner) がブロックしたままの状態で、次 tick が
        # SL 到達を実際にクローズできることを確認する (これが受入条件 2
        # の核心 — Mission 実行中も core_lock が scheduler tick に開放
        # されている)。**bounded acquire (timeout 付き) を使う** —
        # `with app.core_lock:` (無期限待ち) だと、変異テスト (lock 粒度を
        # 意図的に壊す Step 8-2) で Mission 完了までブロックしたまま
        # pytest 自体がハングし、失敗が「タイムアウトで検出される」の
        # ではなく「無限に終わらない」になってしまう。
        acquired = app.core_lock.acquire(timeout=3.0)
        assert acquired, (
            "Mission 実行中に core_lock を取得できなかった — "
            "commit-core のロック粒度が壊れている可能性 (受入条件 2 の核心)")
        try:
            app.scheduler.tick(now + timedelta(minutes=1))  # SL 到達バー
        finally:
            app.core_lock.release()

        row = orders_store.get(app.conn_core, oid)
        assert row["status"] == S.CLOSED.value

        release.set()
        app.supervisor.shutdown(drain_exc=RuntimeError("test cleanup"))
        app.supervisor.join(timeout=10.0)
    finally:
        app.close()
```

（`_init_root`/`_seed_open_position_with_reachable_sl` は既存の `tests/core/test_scheduler.py`/`tests/test_e2e_phase1.py` 等の E2E fixture パターンに実装者が合わせて書くこと — 具体的な SL 価格・バー価格の数値は既存 SL/TP テストの数値をそのまま転用してよい。`app.supervisor.shutdown(...)` は新規受付停止 + 未着手ジョブの drain のみ (ブロックしない) — 実行中だった 1 回目の Mission (既に `release.set()` 済みで完了間近) は `join(timeout=10.0)` が待つ。）

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_e2e_worker_isolation.py -q
```

Expected: 全件 PASS。

- [ ] **Step 5: 決定論的コア diff ゼロの確認 (受入条件 7)**

```bash
git diff main -- src/agentic_fx/core/risk_gate.py
git diff main -- src/agentic_fx/core/paper_broker.py
git diff main -- src/agentic_fx/core/transitions.py
```

Expected: 3 ファイルとも **diff なし** (`risk_gate.py`/`paper_broker.py`/`transitions.py` は本プランのどの task でも Modify 対象に含まれていない — Files 一覧を全 task 通して `grep -n "risk_gate.py\|paper_broker.py\|transitions.py"` で確認し、1 件もヒットしないことをこの Step で再確認する)。`executor.py` は変更対象だが、`git diff main -- src/agentic_fx/core/executor.py` を目視し、**`evaluate(intent, ctx, ...)` の呼び出しと GateContext 構築ロジックが `_open`/`open_from_snapshot` の両方で完全に同一であること** (Task 14/15 の `_evaluate_and_execute_open` への切り出しが判定ロジックを 1 文字も変えていないこと) をコードレビューで確認する。

**(裁定書 FC-6 追加) kill switch ラッチ書込は「目視」だけに頼らない — 機械検証を追加する**: `risk_gate.py`/`paper_broker.py`/`transitions.py` は「本プランで一切変更されない」ため `git diff` で機械判定できるが、kill switch ラッチの実書込 (`state.update(kill_switch_latched=True)` + `activity.write(Category.SYSTEM, "kill_switch_latched", ...)`) は `executor.py` の中 (Task 14 で `_evaluate_and_execute_open` へ移動済み) にあり、`executor.py` 自体は本プランで大きく変更されるため diff ゼロの対象にできない (旧稿はここを「目視」のみで済ませており、目視レビューが唯一の防波堤になっていた — 裁定書 FC-6)。以下を `tests/test_e2e_worker_isolation.py` に追加し、①ラッチ書込が `_open`/`open_from_snapshot` の**両方の経路**で確実に発火すること、②その判定ロジックのソース文字列を pin して無言の改変を検出できることの 2 点を機械的に固定する:

```python
def test_kill_switch_latch_fires_identically_via_open_and_open_from_snapshot(tmp_path):
    """裁定書 FC-6: kill switch latch 書込 (executor.py) は risk_gate/
    paper_broker/transitions の diff-zero 機械検証の対象外 (executor.py
    自体は大きく変更されるため) — この回帰テストで代替の機械検証とする。
    (a) _open (バックテスト/handle_intent 経由) と open_from_snapshot
    (Mission 経由) の両方で、kill switch 却下時に state.kill_switch_latched
    が True になり activity に kill_switch_latched が記録されることを
    確認する。(b) _evaluate_and_execute_open のソースから kill switch
    判定・書込の中核行が消えていないことを文字列 pin で確認する
    (inspect.getsource — 無言の改変を検出する)。
    """
    import inspect

    from agentic_fx.core.executor import Executor

    src = inspect.getsource(Executor._evaluate_and_execute_open)
    # 裁定書 FC-6: この 2 行が消える/条件が緩む変異を検出するための pin。
    assert '"kill switch" in r and "latched" not in r' in src
    assert "self.state.update(kill_switch_latched=True)" in src

    # (a) 振る舞いの回帰: 実装時に tests/core/test_executor.py の kill
    # switch 却下シナリオ (drawdown 閾値超過などで result.reasons に
    # "kill switch" を含む拒否理由が入るケース) を _open 経由・
    # open_from_snapshot 経由の両方で構築し、いずれも
    # state_store.load().kill_switch_latched is True になることを
    # 確認する形に具体化すること (既存 test_executor.py の kill switch
    # テストの fixture 構築パターンに合わせる)。
```

- [ ] **Step 5.5 (前半 Task 11 からの申し送り — 既存テスト監査、レビュー反映 2 回目 R2-SN-01 で役割を最終確認に限定): 二重 `build_app` の既存テスト横断監査**

Task 11 (FC-2, 前半修整 + レビュー反映 2 回目 R2-SN-01 の Step 12.5) が `build_app` にプロセス排他 flock (`acquire_instance_lock`) を追加した時点で、当時判明していた唯一の該当ケース (`tests/test_service_app.py::test_f1c_startup_reclaim_recovers_claimed_signal`) は Task 11 内で個別対処済み (`app1.instance_lock.close()` を挟んでから 2 回目の `build_app` を呼ぶよう修正し、Task 11 の `git add` にも含めている)。**本 Step は新規の個別対処ではなく、Task 12〜19 の実装で新たに二重 `build_app` パターンが混入していないかを最終統合時点で横断監査するだけ**:

```bash
grep -rn "build_app(" tests/ | grep -v "def \|#"
```

上記で洗い出した箇所のうち、同一 `tmp_path`/`root` に対して `build_app` を複数回呼んでいて、かつ 1 回目の `App` を `close()` (または `instance_lock.close()`) していないものが Task 11 対処分以外に無いことを確認する。万一見つかった場合のみ、以下のいずれかで個別修正する: ①1 回目の `App` を使い終わったら `app.close()` (Task 19 配線後) または `app.instance_lock.close()` してから 2 回目を呼ぶ ②2 回目が別の `root` (別ディレクトリ) を使うよう変更する。監査結果 (該当なし、または追加修正した箇所) を progress.md に記録する。

- [ ] **Step 6: 全体 green (受入条件 8)**

```bash
uv run pytest -q
```

Expected: 全件 PASS。プラン開始時点のテスト数 (1404) + 本プランで追加したテストがすべて green であることを確認し、実測件数を progress.md に記録する。

- [ ] **Step 7: 受入条件 3・4・5・6 の再確認 (既存 task の実測結果を集約)**

以下を個別に再実行し、全件 PASS することを確認してから progress.md に「受入条件 8 項目、全件 green」の実測記録を残す:

```bash
uv run pytest tests/test_improve_profile_isolation.py -q       # 受入 3
uv run pytest tests/store/test_missions_cas.py -q               # 受入 4
uv run pytest tests/test_stop_sequence.py tests/test_app_close.py -q  # 受入 5
uv run pytest tests/core/test_scheduler_tick_order.py tests/core/test_executor_snapshot.py -q  # 受入 6
```

- [ ] **Step 8: 変異テスト**

1. `WorkerRunner._kill` の `os.killpg(proc.pid, signal.SIGKILL)` を `os.killpg(proc.pid, signal.SIGTERM)` に改変 (SIGKILL を送らなくする) → `test_worker_runner_kills_process_that_ignores_sigterm` が red (`trap '' TERM` により無反応、`proc.poll()` が None のまま)
2. `TradeLoop._run_once_impl` の commit-core `with self._core_lock:` を run 相の周りまで拡張する変異 (意図的に lock 粒度を壊す) → `test_funds_protection_continues_during_blocked_mission` が red (2 回目の `app.core_lock.acquire(timeout=3.0)` が `False` を返す — bounded acquire なのでテスト自体はハングせず確実に red になる)
3. **(裁定書 FC-6 追加)** `_evaluate_and_execute_open` の `if any("kill switch" in r and "latched" not in r for r in result.reasons):` を `if False:` に改変 (ラッチが二度と発火しなくなる) → `test_kill_switch_latch_fires_identically_via_open_and_open_from_snapshot` が red (ソース pin の assert が最初に落ちる — さらに振る舞い側の assert も red になることを確認する)

- [ ] **Step 9: Commit**

```bash
git add tests/test_e2e_worker_isolation.py
git commit -m "$(cat <<'EOF'
test: プラン8 E2E + 受入条件検証 (設計書 §9 の8項目)

ハング注入→SIGTERM無視→SIGKILL の実プロセス実証、Mission実行中の
SL/TP監視継続の統合実証を新規追加。他6項目は各taskの実測を集約する。

レビュー反映1回目 (裁定書 F-11/IM-5/P8-07): 資金保護E2Eをtrade_loop.
run_once直呼びから、supervisor.start()+scheduler.tickによる本番配線
経由に変更。

レビュー反映1回目 (裁定書 FC-6): kill switchラッチ書込 (executor.py)
をdiff-zero機械検証の代替として、振る舞い回帰+ソースpinで機械検証する。

前半 (Task 11) からの申し送り対応: 同一rootへのbuild_app二重呼び出し
既存テストの監査ステップを追加。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

## プラン完了条件

1. worker 隔離が実プロセスで成立: ハング注入 → SIGTERM 無視時も SIGKILL → `timeout` finalize + 部分 transcript (Task 7/10/20)
2. Mission 実行中も SL/TP 監視が core_lock を取得できる (Task 13/15/16/20 で実証)
3. improve worker profile が `data/` へ構造的に到達不能 (import 可能・実行不能の意味論 — Task 18)
4. `missions.finish` の二重終端が CAS で拒否される + 起動時 `running`→`interrupted` と claimed signals の同時回収 (Task 11)
5. scheduler/supervisor いずれかの死亡で両モードとも停止シーケンス + 非ゼロ終了、`App.close` が所有権ベースで資源終端 (Task 19)
6. tick 順序契約 (決定論ブロック内部順序・fills_allowed ゲート・processed-bar マーキング位置) が回帰ピンで固定、commit-core の鮮度再検証・N4-2 fail-closed 分岐がテストで固定 (Task 12/14)
7. risk_gate.py/paper_broker.py/transitions.py が diff ゼロ、executor.py は判定ロジック不変・I/O 位置のみ移動 (Task 14/15/20)
8. 既存 + 追加分すべての pytest が green (Task 20)
9. プラン7起票の B 束 9 項目・プラン5 park 小口 6 項目がすべて返済済み (Task 2/3/4)

## プラン9 への引き継ぎ

- improve worker profile の registry 中身 (research_tools・改善ループ本体) はプラン9 — 本プランは Landlock + DB パス非提供の権限境界のみ実装した
- `runner.improve.backend == "claude"` は本プランでは `RuntimeError` で fail closed (ClaudeRunner 未実装) — プラン9 で `ClaudeRunner` 実装後にこの分岐を実装へ差し替える
- 遮断 8 項目の全経路統合回帰テストはプラン9 の blocking 受入条件 (分解書どおり) — 本プランは項目 2 (holdout 実行到達不能性) の worker 境界のみ提供する
- spec 小改訂束 (exit_mode② / signals UNIQUE 意図明文化 / approved+rejected 併存規則 / claimed_by FK / **close/cancel gate 論点**) はプラン9 前に実施 (分解書どおり、本プランでは着手しない)
- **[2026-08-08 ユーザー裁定] システムが生成した改善コードはリポジトリに含めない。** 理由: ①本体のベース更新と競合する ②各ユーザーで状況が異なる。これは設計書が既に `plugins/` を gitignore にしている根拠と同一であり、**既に plugin へ適用済みの原則を改善ループの出力全体へ一貫適用する**もの。**スペック改訂を上記 spec 小改訂束に合流させてプラン 9 着手前に実施する**
  - **設計書 §6「出力の 3 経路」の「コア改善 → リポジトリ内 git 管理 → PR + 人間承認」経路は廃止**する。残るのは `plugins/` (gitignore) と `news_sources` (SQLite) の 2 経路
  - **`gh` による PR 作成は不要**になる。GitHub を経由する自己改善の動線そのものが無くなる
  - 改善ループの出力先は **gitignore 済みのユーザー固有領域**に収める (`config/settings.yaml` / `data/` / `logs/` / `reports/` / `plugins/`)
  - 本体コード (plugin 機構・バックテスト基盤・news fetcher 自体) の改善は**提案レポート止まり**。受け皿は設計書に既にある (「分析レポート `reports/improve-YYYY-MM-DD.md` だけ残す」)
  - risk gate パラメータは**コードでなく設定値**なので `approval_requests` の値提案で扱える
  - **未確定**: Mission プロンプト (`prompts/`) の扱い (設計書は「コード外の .md」とするがディレクトリ未作成でリポジトリ内想定)
  - **発見の経緯**: Task 8 完了時に `EXECUTE` 権を詰める中で、**設計書 §6 許可ツール「リポジトリのファイル読み書き」と §4.6 Landlock allowlist「コードツリー読取 + 専用 workdir 読書き」が矛盾**していることが判明した (Task 18 の配線どおりだと improve worker はリポジトリに 1 バイトも書けず、コア改善 PR 経路が成立しない)。この矛盾の解として本裁定が出た
  - **Task 18 への影響**: リポジトリ (コードツリー) は `read_only_paths` のままでよく、**配線はほぼ正しかった**。ただし `plugins/` を `read_write_paths` に足す必要がある。**`EXECUTE` 権の論点も大幅に縮む** (`gh` が消えるため。`pytest` を worker 内で回す必要があるかはプラン 9 で再評価)

- **[2026-08-08 暫定案 — 未確定] 生成コード用の入れ子リポジトリ。** `plugins/` 配下を**親から独立した git リポジトリ**にして、改善ループの生成物に履歴とロールバックを持たせる案。**実装の中で状況が変わり得るため、いまは案の記録に留め、必要になった時点で詳細を詰める**。
  - **解決する既存の穴**: 現状は「**承認台帳はあるが原本が無い**」。`approval_requests(kind="plugin")` の `payload_json` に `name` + `content_hash` (= `plugin.py` と `config.yaml` のバイト列の sha256) が蓄積され、`plugin_loader` は現ファイルから再算出したハッシュが承認済みと一致する plugin だけをロードする (fail closed)。しかし**コードそのものの履歴は無く、上書きされると前の版は失われる**。改善ループが plugin を書き換えると「成績が悪化したので戻す」ができない
  - **得られるもの**: ①原本の保全とロールバック ②2 版目以降の人間レビューが**全文でなく diff** になる (設計書が `prompts/` を .md にした理由と同じ発想) ③親リポジトリは `/plugins/` を gitignore 済みなので**ベース更新と非干渉** ④ユーザーごとに独立 ⑤improve worker の書き込み先が「`plugins/` + 専用 workdir」に確定し Landlock の論点が閉じる
  - **設計上の要点 (この 3 点は方針として先に固定してよい)**:
    1. **submodule にしない。** submodule は親に gitlink をコミットするため、避けたい「本体リポジトリが生成物に結合する」状態に戻る。**単なる入れ子の独立リポジトリ**にする
    2. **commit は親 (決定論的コア) が打つ。** worker に `git` を実行させると `EXECUTE` 権・git バイナリ・設定・署名が allowlist に必要になり権限境界が広がる。**worker はファイルを書くだけ、承認された時点で親が commit** する。こうすると worker に exec 権が不要で、かつ**コミット履歴 = 承認済み状態の歴史**になり、却下された提案は履歴に入らない (台帳と原本の整合が自動で取れる)
    3. **失敗を資金保護に波及させない。** git 操作の失敗 (ディスク full・リポジトリ破損) が plugin 承認や取引処理を止めてはならない。**best-effort + activity ログ** (`ActivityLog` の「write は例外を送出しない」契約と同じ思想)
  - **未確定**: `init` ウィザードへの組み込み方 / git が無い環境でのデグレード動作 (履歴なしで継続 = 現状と同じ) / `reports/` を同じリポジトリに入れるか / バックアップ運用
  - **実装時期**: この仕組みの価値は**改善ループが plugin を上書きし始めてから**発生する。現状は人間が plugin を置くだけで実害が出ていないため、**プラン 9 (改善ループ本体) と同時**でよい。先に作ると使われない期間が長く、`init` フロー・承認フローとの結合部は改善ループの実装が固まってから決める方が手戻りが少ない

## 付録: 実装時照合リスト (未検証のレビュー指摘)

これらは未検証の指摘であり、該当 task の実装者・レビュアーが着手時に照合すること。事実と確認できない場合は無視してよい。

出典: `plan-review-sonnet.md` の「fork 報告」表 (tasks 5-9 担当 fork が発見、主査は詳細照合していない — 77 tool call・23 分の報告のうち件数が多く時間内に全件の再照合はできなかったもの)。裁定書 `plan-review-adjudication.md` の裁定 F-FR (「I14〜I29, I32〜I40 は未検証のまま適用しない。プラン末尾に『実装時照合リスト』として転記し、各 task の実装者・レビュアーが着手時に照合する」) に基づき転記する。

### 表の一行要約 (原文ママ)

| ID | Task | 一行要約 |
|---|---|---|
| I14〜I29 | 13, 15, 16, 19 | fork 報告多数 (finalize の finally 未配置、`try_submit` の stop_event 未考慮、`Scheduler(stop_event=)` 未配線、health ラッチの通知未実装、`build_app` 途中失敗時の cleanup 未実装 等) — 詳細は fork 出力を参照 (本ファイルには要約のみ転記) |
| I32〜I40 | 1-4, 18, 20 | 既存テストの破壊的変更未対応、受入条件の網羅性不足、worktree 並列予定 task 間のファイル衝突 (`signal_tools.py`) 等 |

### 展開済み個別項目 (主査が個別テーマとして書き留めたもの — 上表の範囲に含まれる)

**I14 [Important] Task 13/15 — finalize の finally 未配置**
該当: Task 15 `_run_once_impl`/`_ask_once_impl`。
指摘内容: 五相再構成後、`_finalize_mission` (missions.finish 呼び出し) が各分岐 (runner 失敗・parse 失敗・snapshot 失敗・executor 例外) ごとに個別に呼ばれており、「finalize は必ず一度だけ呼ばれる」という不変条件を `finally` で一元的に保証する構造になっていない。将来分岐が増えた際に finalize 呼び忘れが発生しうる (現状の分岐網羅は fork が実装コードを追った限りでは漏れは無いとされるが、保証の仕方が構造的でなく列挙的)。
fork の修正案: `try/finally` で `_finalize_mission` を一箇所に集約し、各分岐は `result`/`status` をセットして早期 return するだけにする。
**照合メモ**: 本修整担当が Task 15 を編集した現行版でも `_finalize_mission` は複数分岐 (result.status!=completed / IntentParseError / executor 例外 / 正常系) それぞれで個別に呼ばれている (逐語コード引用は Task 15 本文参照)。列挙的な構造は変わっていない — 着手時に分岐網羅の再確認と、`try/finally` への一元化が可能か (commit-core の `with self._core_lock:` ブロック内で `finally` を使う場合、lock 解放タイミングとの整合に注意) を検討すること。

**I17 [Important] Task 13 — `try_submit` が `stop_event` を考慮しない**
該当: Task 13 `MissionSupervisor.try_submit`。
指摘内容: `try_submit` は `self._busy` のみを見て受理判定するため、`shutdown()` で `stop_event` が立った後でも、`_busy` が False なら新規ジョブを queue に投入できてしまう (shutdown と try_submit の間に TOCTOU 的な窓がある)。「新規受付停止」を `stop_event` セットだけで実現している設計書 §5 手順1の想定と食い違う。
fork の修正案: `try_submit` の先頭で `self._stop_event.is_set()` を確認し、立っていれば即 `None` を返す。
**照合メモ**: 本修整担当は `MissionSupervisor.try_submit` を変更していない (現行版もまだ `self._stop_event.is_set()` チェックを持たない)。Task 19 で `stop_event` セット後の新規受付停止が要求されるため、着手時に実際に TOCTOU 窓が問題になるか (scheduler 側は `_stopping()` で `on_trade_mission` 呼び出し自体をスキップするため、shell の `ask` 経由だけが窓に該当しうる) を確認すること。

**I19 [Important] Task 19 — `Scheduler(stop_event=...)` が未配線**
該当: Task 19 の `build_app`/`Scheduler` 構築箇所。
指摘内容: 設計書 §5 手順1「新規受付停止: stop_event セット → scheduler は起動判定・hooks をスキップ」を実現するには `Scheduler` 側が `stop_event` を参照して `_trade_mission_due`/`_run_hooks` をスキップする必要があるが、`Scheduler.__init__` のシグネチャ・`build_app` での構築のいずれにも `stop_event` パラメータが配線されていない。結果として stop_event セット後も次 tick で通常どおり hooks/Mission 起動判定が走ってしまう。
fork の修正案: `Scheduler.__init__` に `stop_event: threading.Event | None = None` を追加し、`tick()` 冒頭で `stop_event.is_set()` なら hooks/Mission 起動をスキップして決定論ブロックのみ実行する分岐を追加する。
**照合メモ**: 本修整担当が編集した現行 Task 19 は既に `Scheduler.__init__(..., stop_event: threading.Event | None = None)` と `_stopping()` メソッドを実装済みであり (Step 5 参照)、この指摘は**現行プランでは解消済み**と見られる。着手時に `build_app` が実際に `Scheduler(..., stop_event=stop_event)` を渡していることを diff で確認すること。

**I22 [Important] Task 19 — health ラッチの通知経路が未実装**
該当: Task 19 `HealthLatch` 相当のクラス定義箇所。
指摘内容: 設計書 §6「health ラッチ: activity 書き込み失敗は latched health 状態に記録し、以後は別経路 (notifier + stderr) で警告」とあるが、`record_failure` の実装は内部記録のみで、`notifier.send(...)`/stderr 出力を伴う「別経路での警告」が実装されていない。`status` コマンドでの表示との整合も未確認。
fork の修正案: `HealthLatch.record_failure` に `notifier`/`stderr` への即時通知を追加する。`Commands.status` の出力に latch 内容を含める実装漏れが無いか確認する。
**照合メモ**: 本修整担当が編集した現行 Task 19 の `HealthLatch.record_failure` は `self._reasons.append(reason)` のみで、notifier/stderr への即時通知は実装していない (`Commands._status()` への表示は実装済み — pull 型)。設計書の「別経路 (notifier + stderr)」が push 型通知を要求しているのか `status` コマンドでの pull 表示で足りるのか、着手時に設計書と再照合すること。

**I25 [Important] Task 19 — `build_app` 途中失敗時の cleanup 未実装**
該当: Task 19 の `build_app` 全体。設計書 §5「`build_app` 途中失敗時は構築済み分のみ逆順 cleanup」。
指摘内容: `build_app` は多数のリソースを順に構築するが、途中の構築 (例: `Rag(...)` の chromadb 初期化) が例外を送出した場合に、それ以前に開いた `conn_core`/`conn_shell` 等を閉じる `try/except`/`finally` が実装コードに見当たらない。
fork の修正案: `build_app` 全体を `try/except` で包み、例外時はその時点までに構築済みのリソースリストを逆順 close してから re-raise する。
**照合メモ**: 本修整担当は `build_app` 全体を `try/except` で包む変更を行っていない (現行版も未対応のまま)。`App.close()` (Task 19 で新設) は「完成した `App` インスタンス」を前提に動くため、構築途中の部分的なリソース (ローカル変数のみで `App` に未到達) には使えない。着手時に「本プランのスコープ内で対応するか」「プラン9 に送るか」をユーザーに確認すること (裁定書に明示指示が無いため本修整では手を付けていない)。

**I32 [Important] Task 1-4 — 既存テストの破壊的変更未対応**
該当: Task 3/4 の複数 step (config.py 新設 kwarg のキーワード必須化箇所)。
指摘内容: `NewsCollector.__init__`/`EconCalendar.__init__` 等にキーワード必須の新設引数を追加する際、既存の呼び出し箇所 (本体コードだけでなく既存テストのフィクスチャ) 全てを洗い出して更新する指示が「全体 green を確認して失敗したら直す」という事後対応の書き方になっており、洗い出し漏れがあっても step の記述上は検出できる保証が弱い。
fork の修正案: 事前に `grep` で影響箇所を全列挙してから step 内に明記する形式に統一する。
**照合メモ**: 本修整担当のスコープ外 (Task 1-4 は担当外)。着手時に Task 3/4 の該当 step を確認し、事前 grep 洗い出しへの書き換えが必要か判断すること。

**I35 [Important] Task 20 — 受入条件の網羅性不足**
該当: Task 20 の受入条件対応表。
指摘内容: 設計書 §9 の受入条件 8 項目のうち複数が「他 task で実測済み — 本 task は全体スイートに含まれることの確認のみ」という記載になっており、Task 20 自身が独立に end-to-end で再現するテストを書いていない項目がある。
fork の修正案: Task 20 に、各受入条件について「他 task のテストを流用」ではなく「本番に近い組み立てで独立に再現する」テストを最低 1 本ずつ追加する。
**照合メモ**: 本修整担当は受入条件 2 (IM-5/P8-07) を本番配線経由に修正したが、受入条件 3・4・5・6 は依然「他 task の実測を集約」のままであり (Task 20 本文の表参照)、この指摘は**部分的にのみ解消**。着手時に残り 4 項目について本番配線での独立再現が必要かユーザーと合意すること (裁定書に明示指示が無いため本修整ではスコープ外とした)。

**I38 [Important] Task 20 — worktree 並列予定 task 間のファイル衝突 (`signal_tools.py`)**
該当: プラン規約の「依存の浅い task 束は worktree 並列」の運用と、Task 3 (`signal_tools.py` の description f-string 化) / Task 9 (`signal_tools.py` 経由の Rag RPC 配線に関連する変更) が同一ファイルを編集する可能性。
指摘内容: プラン本文には task 間の依存関係グラフや「同一ファイルを編集する task の組」が明示されておらず、実行時の判断に委ねられている。
fork の修正案: プラン冒頭に「同一ファイル編集 task の一覧」を明記し、worktree 並列の対象から除外する組を指定する。
**照合メモ**: 本修整担当は対応していない (プラン冒頭への一覧追加はスコープ外)。着手時に worktree 並列を計画する場合は `grep -rln "signal_tools.py" docs/superpowers/plans/2026-08-04-phase2-8-worker-isolation.md` 等で同一ファイル編集 task を都度洗い出すこと。

**T2-1 [制約メモ] Task 16/18 — `preexec_fn` はスレッド安全でない (Task 2 レビューで検出)**
該当: `plugin/approval.py` の `_default_pytest_runner` が新設した `preexec_fn` (rlimit 設定)。
内容: `subprocess.Popen(preexec_fn=...)` は原理的にスレッド安全でない (fork 後・exec 前の子で任意の Python コードを実行するため、他スレッドが保持していたロックがデッドロックし得る)。**現時点では plugin 承認が単一スレッドの CLI 経路からしか到達しないため実害はない** (sonnet 副査が確認、non-blocking 判定)。
**照合メモ**: 本プラン 8 は supervisor / watchdog / scheduler / RPC dispatcher の 4 スレッドを新設する。**plugin 承認・plugin 評価がスレッド文脈から到達可能になる task (ReflectionCycle = Task 16、improve profile = Task 18 が候補) の着手時に、この経路がマルチスレッドから呼ばれないことを確認すること。** 呼ばれるなら `preexec_fn` を捨てて子側 (`pytest_sandbox_entry`) の先頭で rlimit を設定する方式へ移す。

**T4-1 [引き継ぎ] Task 19/20 — `tests/test_service.py` の参照が 4 箇所残っている**
該当: 本プラン文書の 8638 / 9008 / 9012 / 9036 行付近 (Task 19 と Task 20 の記述)。
内容: `tests/test_service.py` は**存在しない** (実体は `tests/test_service_app.py`)。Task 2-4 の範囲 (734-1480 行) は 2026-08-07 に一括置換済みだが、この 4 箇所は `tests/test_service.py tests/test_service_app.py` の**併記**が含まれ機械置換すると重複になるため保留した。
**照合メモ**: Task 19/20 の着手時に、併記の有無を見て個別に修正すること。Task 1 ではこの種のパス誤りを実装者が黙って回避し、レビューで初めて発覚した。
