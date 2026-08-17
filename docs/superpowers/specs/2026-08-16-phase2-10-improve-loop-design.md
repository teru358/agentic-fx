# Phase 2 プラン 10 設計書: ClaudeRunner + CodexRunner + 戦略改善 loop

- 日付: 2026-08-16 (初版)
- ステータス: **ユーザー承認済み (2026-08-16)、codex 設計レビュー 10 周 → R12 スコープ縮小反映 (2026-08-17)、11 周目 (縮小版の収束確認) 待ち**
- 準拠: 本体設計書 **改訂第 17 版** (`c2355e8`) / 分解書 `2026-08-01-phase2-decomposition.md` (2026-08-11 改訂) / プラン 9 設計書 `2026-08-11-phase2-9-foundation-design.md` §1 D6・§5
- 前提: プラン 9 完了 + テスト isolation 遮断 (main `410501f`, 2071 passed / 1 deselected)
- 実測入力: `.superpowers/sdd/plan10-design/probe-runner-feasibility-report.md` (2026-08-16、opus probe。以下「probe」)、`code-state-map.md` (同日のコード現状地図)

## 0. 本書の役割 — 何を設計し、何を設計しないか

プラン 10 は **改善ループを初めて動かす**プランである。プラン 6〜9 が積んだ部品 (バックテスト・holdout・plugin 機構・worker 隔離・improve profile の空殻・`improvement_backlog` / `improvement_runs` / `backtest_runs` / `analysis_runs`) を、**書き手 (LLM) と親側の受け取り**で結線する。加えて **AgentRunner を 3 実装 (local / claude / codex) にする**。

| 由来 | 設計状態 |
|---|---|
| 分解書「プラン 10」節 (Task 1〜9 + 裁定要事項 ①②③) | **本書が設計する** |
| プラン 9 設計書 §1 D6 (`plugins/` 入れ子 git) | codex 3〜6 周ぶんの指摘反映済み。**本書 §5 で候補置き場からの昇格に合わせて再収束**させる |
| プラン 9 設計書 §5 (codex 経路・claude 実測・実装順序) | 入力。**probe で覆った点は §0.3 に列挙** |
| 本体設計書 §4 (AgentRunner) | **本書 §1 で改訂内容を確定**する (裁定②に伴う) |
| 本体設計書 §6 (改善 loop・品質ゲート・遮断 8 項目・plugin 機構) | 正として参照。転記しない |

**設計しないもの (明記)**: **R12 で外した (a) `plugin rollback` / `plugin bless-version` / 既存プレーン版の adopt / `plugin_versions` 表 (b) risk_gate 提案の親側評価 (d) live の in-place 編集を伴う bless (設計テキストは §A、起票は §10) / (c) wave slot の再開機構は簡素化 (§3.1)** / news ソース提案の承認経路 (プラン 11 Task 6 へ) / REST・Discord からの承認 (プラン 11) / 外向きリクエスト予算の**全体**設計 (起票のまま。本書は改善ループの経路に閉じた予算だけ §6) / claude のローカル LLM 駆動 (起票 §10) / trade_intents 保持・Notifier 戻り値契約ほかプラン 9 §6 の起票 (§10 に引き継ぐ)。

### 0.1 ユーザー裁定 (2026-08-16、本書の前提)

| # | 裁定 | 根拠 |
|---|---|---|
| R1 | **設計より前に実測** (worker 隔離下で codex / claude が 1 ターン完走するか) を回した | 分解書の実装順序①。「起動できる」で測らない |
| R2 | **news ソース提案はプラン 10 に含めない** (改善ループの出力は plugin 経路 + 提案レポート) | 機械検証は外向き予算設計と不可分 / 人間の `news add` はプラン 11 |
| R3 (裁定②) | **システムにインストール済みの CLI (`claude` / `codex` コマンド) を直接駆動する。Python SDK (`claude-agent-sdk` / `openai-codex`) は採用しない** | SDK は両方とも CLI 本体を同梱 (venv +677MB・依存 +21・版ラグ 0.144 vs 0.147)。codex 側は SDK でもツール公開に外部 MCP が要り利点が小さい。SDK は env を制限できない (追加のみ)。ユーザーは `codex login` / `claude login` のためにどのみち CLI を入れる。**CLI 直叩きで両方完走を実測** (probe P1/P2/P8) |
| R4 (裁定③) | **improve worker 内で pytest を回してよい** (反復用)。**採否を決めるゲートの pytest は親が別プロセスで回し、そのプロセスも improve と同じ Landlock で囲う** | worker 内 pytest の要件は probe P7 で確定。親側ゲートは現行 `submit_plugin` が無隔離で回しており、agent が書いた test を親権限で実行する confused deputy になる |
| R5 | **3 backend (local / claude / codex) を全て実装。既定は local のまま (暫定)**。実装計画の実機 E2E (3 方式で plugin 1 本を green にできるか) の結果で既定を見直す | codex+llama-swap の plugin 実装能力は未測。codex 自前 sandbox は Landlock 下で使えない (probe §5-①) |
| R6 (案 A) | **improve worker は DB に書かない。Mission の DB 効果は `MissionResult.output` として返し、親が commit 相で決定論的に適用する** | 不変条件「`data/`・DB に到達できない」を構造のまま保つ / ゲートを親が output に対して強制できる / supervisor 三相と finalize 所有権にそのまま乗る |
| R7 | **改善 Mission は取引の待ち行列に並ばない。別レーンで最大 N 並行、取引を待たせず、取引のために中断もされない** | ユーザー指摘「1 回の中断/負けで悪い改善と判断できない」「複数 Mission を並行したい」 |
| R8 | **1 回の結果で課題を「悪い」と判断しない**。失敗・却下・標本不足は「観察 (要再挑戦)」として残し、試行回数と標本数を履歴に添える | 同上 |
| R9 | **claude → ローカル LLM は起票** (未実測・鍵契約・規約グレー・codex+llama-swap で代替可) | ユーザー了承 |
| R11 | **サブスク backend (claude / codex+chatgpt) は「agent 自身が scratch の OAuth 認証コピーを読み、shell+ネットワークで持ち出せる」というハーネス固有の残余リスクを受容する** (codex 4 周目 C1)。従量課金鍵が無い不変条件は保つが、サブスク認証トークンは agent から可読。fail closed にしない。**codex+llama_swap / local にはこの資格情報が無い** — provider=llama_swap は空の scratch `CODEX_HOME` で起動し OAuth をコピーしない (codex 5 周目 C1、§1.1-2)。恒久対処 (別 UID / credential broker / 短命トークン) は起票 | ユーザー裁定 2026-08-16 |
| R12 | **スコープ縮小 (2026-08-17、codex 10 周目の判定を受けて)**: (a) `plugin rollback` / `plugin bless-version` / 既存プレーン版の adopt / `plugin_versions` provenance 表 → 起票 (§A.1 に設計を保存)。本プランで版化・履歴記録するのは本プランのライフサイクルで approved になった artifact だけ。既存プレーン dir は初回切替時に `_retired/` へ退避 (人間所有) (b) risk_gate 提案の親側評価 → 起票 (§A.2)。`proposal_kind=risk_gate` は observation `unsupported_in_plan10` (c) wave slot の再開機構を簡素化: period は wave 行の存在で消費、再起動後の claimed/running は failed、`started_at`・全 failed wave の削除を撤去 (§A.3)。3-way 起動と単一終端ヘルパは維持 (d) live path (`plugins/<name>`) は編集対象外。人間の候補は `plugins/_human/<name>/` (`materialize` でコピー) から `submit\|bless --from _human` で改善ループと同じライフサイクル | 10 周で指摘の大半がこの 4 領域の crash 整合に集中し (件数 14→14→16→15→9→10→11→12→12→5)、codex は「4 領域を外せば設計レベルの Critical/Important 0」と判定。価値の発生時期が後 (承認済み版が複数溜まってから / 改善ループが安定してから) であり、不変条件 (data/ 不可視・人間承認必須・原本保全) は弱まらない |
| R10 | **claude 用 `/proc` 許可による同一 UID プロセスの env 読取 (codex 3 周目 C1) は、緩和策 3 点 + 実測付き選択肢で受容する**: ①trade worker の資格情報を env から handshake へ移し、どの子プロセスの初期 env にも秘密を置かない ②サービス起動時に自身の初期 env に秘密名パターンがあれば improve+claude を起動拒否 ③残余 (同一 UID 他プロセスの environ/cmdline) を明記。加えて非特権 PID+mount namespace を実装計画で実測し、動けば既定にして `/proc` を allowlist から外す | ユーザー裁定 2026-08-16 |

### 0.2 なぜ 3 方式か (ユーザーの動機、後で読む人のために)

plugin の自己改善ループを回すにあたり、**自前のローカル LLM ハーネス (LocalRunner の tool-calling loop) より、claude / codex の成熟したハーネス (ファイル編集・shell・schema 出力・エラー回復) の方が効率的**というのがユーザーの見立てである。両方使えるなら両方載せる。したがって**本線は ClaudeRunner (サブスク) と CodexRunner (サブスク / llama-swap provider)** であり、**LocalRunner の改善対応は「外部 CLI 無しでも動く最低線」**である。既定 `local` は clone+init で動くことを守るための暫定値で、本線を local と誤解してはならない。見込み順は claude(サブスク) ≥ codex(サブスク) > codex(ローカル) > 自前ループ、費用は逆順。

### 0.3 プラン 9 設計書・分解書からの差分/訂正 (probe と裁定による)

| 項目 | プラン 9 §5 / 分解書の記述 | 本書 |
|---|---|---|
| codex の防御 | 「codex は `Sandbox.workspace-write` + `cwd=plugins/` で SDK 層でも絞れる (Landlock は二重防御)」 | **不成立**。Landlock 下では codex 自前 sandbox (`bwrap`) が `PR_SET_NO_NEW_PRIVS` により起動できず、legacy landlock でも `execvp /bin/bash` が拒否される (probe §5-①)。**shell を使わせるなら `--dangerously-bypass-approvals-and-sandbox` 一択で、FS 境界は両 runner とも我々の Landlock 単独** |
| SDK 採用 | 「公式 Python SDK `openai-codex` … で AgentRunner 実装」「`claude-agent-sdk` を採用、subprocess + stdio MCP の自前実装はしない」 | **取り消し (R3)**。CLI 直駆動 + 最小 MCP stdio シム |
| 実装順序 | ①実測 → ②共通基盤 → ③Claude → ④Codex → ⑤同一不変条件検証 | ①は完了 (probe)。②〜⑤は §8 の task 束に写像 |
| 従量課金遮断 | 「Claude 側は子 env から `ANTHROPIC_API_KEY` を除去」 | **「除去」ではなく「最初から継承しない」** (env は allowlist で完全指定)。claude は鍵があるとサブスク認証より優先しハングする (probe P5)。codex は `auth_mode=chatgpt` なら鍵を無視したが契約は同じく非継承 |
| news 提案 | 分解書 Task 2/6 の許可ツールに「news_sources への追加提案」 | **プラン 11 へ (R2)** |
| plugins/ への書込 | 「`read_write_paths` に `plugins/` を追加」 | **`plugins/` 全体は渡さない**。候補置き場 `plugins/_staging/<mission_id>/` のみ (§2.3) |
| Landlock 拡張 | 「EXECUTE 権の与え方は Task 18 で裁定」(未裁定) | **`execute_paths` を新設** (§2.2)。対象は backend ごとの exec closure (python・ローダ・CLI・shell 系) |
| D6 の到達範囲 (R12) | 「plugin 履歴とロールバック」 | **本プランは履歴 (bare git) と版ストアの保管まで**。ロールバック操作・旧版 adopt は起票 (§A) — 戻すときは `materialize` → 編集 → `bless --from _human` で新規承認 |
| D6 入れ子 git の形 | 「`plugins/` 配下を親から独立した入れ子 git リポジトリ (ワークツリー付き)」 | **bare リポジトリ `plugins/.history.git`** (ワークツリー無し)。live の `plugins/<name>` は版ディレクトリへの symlink であり、git tree (通常ファイル) と型が合わないため、ワークツリーを持つと `git status` が常時 dirty になり `git restore` が live symlink を壊す (codex 2 周目 I9)。plumbing (専用 index + blob) は不変 |
| 書込先 (`reports/`) | 分解書「書き込み可能パスが `plugins/` と `reports/` に閉じている」 | **worker は `reports/` に書かない** (案 A で親だけが書く — §2.1/§4.2-6)。worker の書込先は候補置き場 + workdir + `/dev`。「`plugins/` と `reports/`」は**親を含むシステム全体**の出力先として引き続き正 |

---

## 1. Runner 層 — `CliRunner` 共通基盤 + `ClaudeRunner` / `CodexRunner`

### 1.1 構造

```
runners/base.py          Mission / MissionResult / AgentRunner  (不変。docstring の max_turns 節を §1.5 で更新)
runners/local_runner.py  LocalRunner (不変。improve では registry が非空になるだけ)
runners/cli_runner.py    CliRunner(AgentRunner)  ← 新規: 共通基盤 (抽象)
runners/claude_runner.py ClaudeRunner(CliRunner) ← 新規
runners/codex_runner.py  CodexRunner(CliRunner)  ← 新規
runners/factory.py       build_runner(profile, settings, registry, *, on_message, workdir) ← 新規: mission_worker から backend を選ぶ唯一の入口
```

`CliRunner` の責務 (両 CLI 共通):

1. **子プロセス起動 — 全体原則: multi-threaded なプロセス (service / mission_worker) では Python `preexec_fn` を使わない** (codex 2 周目 I8 / 3 周目 I7)。`fork` 直後の child が他スレッドのロックを継承して exec 前に止まる経路があるため、子の起動前処理が要るときは常に **単一スレッドの最小 launcher** (`sys.executable -c "<launcher>"`、`agentic_fx.runners.launcher` に共通実装) を exec し、その launcher が前処理をしてから `os.execv` する。CLI: `Popen([sys.executable, "-c", "<launcher>", <expected_parent_pid>, <絶対 argv...>], cwd=workdir, env=<完全指定>, stdin=<open('/dev/null', O_RDONLY)>, stdout=PIPE, stderr=PIPE, start_new_session=True)`。launcher は ①`prctl(PR_SET_PDEATHSIG, SIGKILL)` ②**直後に `os.getppid()` を expected_parent_pid と再照合し、不一致なら即終了** (設定前に親が死んで再親付けされた race — プラン 8 §4.8 と同じ) ③`os.execv(argv[0], argv)` — argv は**起動時検査が解決した絶対パス**のみ (§1.4、相対既定値を launcher で解決しない)。`start_new_session=True` は Popen 側で済ませ、launcher は setsid しない。python の EXECUTE は closure に在る (§2.2)。**launcher を選ぶ理由**: PDEATHSIG は child 側でしか設定できず、「dispatcher スレッド起動前に spawn する順序契約」は再試行や 2 本目の CLI 起動で破れやすい。gate pytest (§4.2-3d) も同じ launcher を使う (rlimit を launcher が設定してから gate worker へ execve)。**プロトコルへの追加フレーム (`seq` 付き)**: 親→子 **`go`** (§3.1 3-way 起動: worker は `ready` 送出後、親が slot を `running` に commit してから送る `go` を受けるまで**Mission もツールも一切実行しない**。`worker_startup_timeout_sec` 内に `go` が来なければ副作用ゼロで終了) / 子→親 **`cli_started`** `{"type":"cli_started","pgid":<CLI pgid>}` (CLI 起動直後) — 外側 `WorkerRunner` は worker が EOF/異常死したとき、finalize より前に**この pgid を killpg** して CLI 子孫を回収する (I9)。**`subprocess.DEVNULL` は使わない** — Landlock 下で `/dev` が ro だと Python は `/dev/null` を `O_RDWR` で開けず失敗する (probe §5-③)。`/dev` は §2 で rw にするが、`CliRunner` 単体テストは ro 環境でも動く形にしておく
2. **scratch home と認証コピー — コピーするのは Landlock 外の親 (C1)**: `WorkerRunner` (親プロセス、Landlock 外) が Mission workdir を **0700** で作り、その直下に `home/`・`tmp/`・`cfg/` を作り、**spawn より前に**認証ファイルを `cfg/` へコピーする — **claude: `~/.claude/.credentials.json` → `cfg/.credentials.json` / codex は `runner.codex.provider=chatgpt` のときだけ `~/.codex/auth.json` → `cfg/auth.json`**。**`provider=llama_swap` は空の scratch `CODEX_HOME` (auth.json 無し) で起動する** (codex 5 周目 C1 — 不要な ChatGPT トークンを agent の可読範囲に置かない)。CLI がカスタム provider に資格情報を要求する場合は provider 定義の `env_key` に**非秘密のプレースホルダ** (例 `LLAMASWAP_API_KEY=local`) を env で与える — OAuth ファイルは決して使わない。**auth 無しの codex+llama_swap 実 1 ターンは provider 固有の blocking 実測 (§7.2、Task 13)** — fake pin (auth をコピーしないこと) は全体 blocking (§7.1-2) だが、実ターンの成否は `provider=llama_swap` の可否だけを決める: **人間が実測合格後に `improve.llama_swap_verified: true` を設定するまで、起動時検査は `provider=llama_swap` を拒否する** (他 backend の有効化には影響しない、codex 6 周目 M2)。コピー前に原本を検査: 通常ファイル (`O_NOFOLLOW`)・所有者 == 実 uid・group/other に権限なし (mode `0600` 系)・サイズ ≤ 64 KiB。不合格は Mission failed (reason 安全化)。ソースの場所は `runner.codex.auth_file` / `runner.claude.credentials_file` (既定は上記。`~` 展開は親側)。**`CliRunner` (worker 内) は実 `~/.codex` / `~/.claude` を一切読まず、`workdir/cfg` だけを使う** — Landlock 適用後の worker からは原本が `EACCES` なので構造的にも読めない。**コピー先が更新されても元へ書き戻さない** (トークンリフレッシュが起きた実行では実ファイルが古くなる方向のドリフトが予想される — 起票 §10)。実ファイルは Landlock allowlist に**入れない** (probe P4: strace で実 `~/.codex` への openat 0 件)。実 HOME に関する情報は子に一切渡さない
3. **env の完全指定** (継承しない。`_mission_worker_env` の allowlist を拡張する形): `PATH=/usr/bin:/bin` (codex が shell snapshot で `/bin/bash` を起動する — probe §3) / `HOME=<workdir>/home` / `TMPDIR=<workdir>/tmp` / `CODEX_HOME=<workdir>/cfg` or `CLAUDE_CONFIG_DIR=<workdir>/cfg` / `PYTHONPATH` `PYTHONSAFEPATH` (既存)。値は親が workdir を作った後に確定するので **Popen の env に直接入れる** (handshake ではない)。**`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `AFX_*` / データ資格情報は構造的に存在しない**。pin テスト: 起動 argv と env をキャプチャする fake Popen で「鍵の名前を含む変数がゼロ」を assert
4. **timeout と子孫の所有**: CLI は **`start_new_session=True` で自分専用のセッション/pgid** に置き (`killpg` が mission_worker 自身に当たらないように)、launcher (項 1) が `prctl(PR_SET_PDEATHSIG, SIGKILL)` を設定する (worker が死ねば CLI も死ぬ)。`mission.timeout_sec` を壁時計で監視し、超過で CLI セッションへ SIGTERM → `runner.cli_terminate_grace_sec` (既定 10) → `os.killpg(cli_pgid, SIGKILL)` → `status="timeout"` の result を返す。mission_worker 既存の SIGTERM ハンドラ (transcript flush) も **終了前に CLI セッションを kill** する。**`CliRunner` の `finally` (completed を含む全終端)**: CLI の pgid へ SIGTERM → grace → SIGKILL → **pgid が空になるまで待ってから** `MissionResult` を返す (codex 9 周目 I2 — 完了後の生き残り孫を残さない)。外側の `cli_started` pgid kill (§3.1) は第 2 線、`WorkerRunner._escalate_kill` (worker の pgid への killpg) は不変。**残余リスク (§2.4・§10)**: `setsid()` を呼ぶ孫は CLI の pgid から逃げる。完全な包含には cgroup v2 が要る (起票)。probe P9 は同一 pgid + synthetic 連鎖の範囲でしか測っていない
5. **transcript**: CLI のストリーム出力 (claude `stream-json` の各行 / codex `--json` の各イベント) を `on_message` sink に流す → 既存の worker `event` フレームに乗る。1 行上限・累積上限は既存の transcript 上限に従う
6. **出力の正規化と検証**: 最終出力文字列を `response_parser.parse_json_output` (フェンス剥がし・`<think>` 除去) に通し、`jsonschema` で `output_schema` 検証。不適合は **`status="failed"`, reason=`"output_schema mismatch: <安全化済み要約>"`** (CLI runner では再出力リトライをしない — CLI 再起動 = 全コンテキスト再送で高価。llama-swap provider の schema 非強制 (probe P1②) はフェンス剥がしで吸収し、それでも駄目なら失敗として E2E で計測する)
7. **`reason` 安全化**: `LocalRunner._normalize_reason` と同じ規律 (単一行・上限 200 文字・秘密除去・外部応答本文を生で入れない)。CLI の stderr は**要約 (先頭行 + 終了コード) のみ**を reason に載せ、本文は transcript の `truncated_stderr` イベントに切り詰めて残す
8. **異常終了**: 非ゼロ終了・出力ファイル無し・JSON 破損 → `status="failed"` + reason。SIGKILL 後 → `status="timeout"`

### 1.2 ClaudeRunner

argv (probe P2 で完走した形):

```
<claude_bin> -p <prompt>
  --output-format stream-json --verbose
  --json-schema <output_schema JSON 文字列>
  --setting-sources "" --strict-mcp-config
  --mcp-config <workdir/mcp.json>            # §1.6 のシム定義 (改善 profile のみ)
  --allowedTools <集合>                        # §1.6
  --max-turns <mission.max_turns>
  --model <runner.<profile>.model>
```

- 最終出力は `result` イベントの `structured_output` (probe: `{"answer":4}` を回収)。無ければ最後の assistant text を §1.1-6 に通す
- `--setting-sources ""` でも CLI 組み込みの tools/slash_commands は残る (probe §4「限定つき」)。ユーザー設定由来でないと解釈するが、積極確認は未了 → 実装計画の実測項目 (組み込み以外が混入していないことを `init` イベントの MCP server 集合で pin — **許可集合は当該 profile の `afx` ちょうど 1 つ、未知 server は 0 件** (codex 4 周目 M2)。probe の `mcp_servers: []` は MCP config を載せない実測形の観測)
- `--bare` は OAuth を読まないため**使えない** (probe §4)
- **trade profile でも選択可** (`runner.trade.backend=claude`)。その場合 `--allowedTools` は MCP ツール (`mcp__afx__<name>`) のみで、`Bash`/`Write`/`Edit` 等は許可しない。trade worker は Landlock 無しで RO DB とデータ資格情報を持つため

### 1.3 CodexRunner

argv (probe P1①/P1②/rl_codex_noplugins で完走した形):

```
<codex_bin> exec <prompt>
  --json
  --output-schema <workdir/schema.json> -o <workdir/output.txt>
  --ignore-user-config
  --dangerously-bypass-approvals-and-sandbox      # §0.3 — 自前 sandbox は Landlock 下で使えない
  --disable plugins --disable remote_plugin --disable recommended_plugins
  --disable apps                                   # ← 効果は probe 未検証。本プラン Task 13 で実測・記録 (§7.2)。残る egress の停止手段は起票 (§10)
  -c mcp_servers.afx.command=<python> -c mcp_servers.afx.args=[…]   # §1.6
  [-c model_providers.llamaswap.base_url=<llama_swap.base_url> -c model_providers.llamaswap.wire_api=responses
   -c model_provider=llamaswap]                    # provider=llama_swap のとき
  -m <runner.improve.model>
```

- **`--disable plugins --disable remote_plugin --disable recommended_plugins` は必須** — 起動時に 11.8MB のプラグインカタログを外部取得し、本番 `RLIMIT_FSIZE=8MB` で `SIGXFSZ` 即死する (probe §5-②)。argv pin テストで固定
- `apps` feature は provider が llama-swap でも `https://chatgpt.com/backend-api/ps/mcp` を叩く (probe §5-②)。`--disable apps` で止まるかは**未検証**。**本プランでは Task 13 (実機 E2E) で `strace -e trace=connect` 相当で測定し結果を記録するところまで**を行う。止まらなかった場合の停止手段 (別 config key・proxy) は**起票** (§10) — argv pin は「未確認の egress を対処済みにする」ものではなく、既知の 3 つ (plugins/remote_plugin/recommended_plugins) の退行防止である
- 最終出力は `-o` ファイル。llama-swap provider ではフェンス付きで返ることが多い (probe P1②) → §1.1-6
- **improve 専用**。`runner.trade.backend=codex` は `Settings` の validator で拒否 (`ValueError`)。理由: codex は shell を外せない (probe §5 表: ツールの無効化はできない)。trade worker は Landlock 無し + `TWELVEDATA_API_KEY` 等を持つ
- provider `chatgpt` のとき `auth.json` の `chatgpt_subscription_active_until` を起動時検査で読み、期限が近い/過ぎていれば警告 (§1.4)。期限切れ実行は Mission failed で止まる — **local への自動フォールバックはしない** (設計書 §13 の既存裁定と同じ)

### 1.4 config と起動時検査

```yaml
runner:
  trade:   { backend: local, model: … }      # backend: local | claude   (codex は拒否)
  improve: { backend: local, model: … }      # backend: local | claude | codex
  claude:
    bin: claude                                # PATH 解決 → 起動時に絶対パス (realpath) へ正規化
    credentials_file: ~/.claude/.credentials.json
  codex:
    bin: /path/to/vendor/x86_64-unknown-linux-musl/bin/codex   # **vendor native バイナリを直指定** (node ラッパ .js は拒否)
    provider: chatgpt                          # chatgpt | llama_swap
    auth_file: ~/.codex/auth.json
  cli_terminate_grace_sec: 10.0
improve:
  parallel: 1                                 # 1..4
  mission_max_turns: 200                      # ge 1。Local/Claude に渡す。Codex は無視 (§1.5)
  mission_timeout_sec: 3600                   # ge 60。3 backend 共通の壁時計
  llama_swap_verified: false                  # `afx improve verify-backend` (検証専用入口、Task 13) の合格後に人間が true にする。false のまま通常入口は runner.codex.provider=llama_swap を起動拒否
  max_new_backlog_per_mission: 20
  backtest_rpc_timeout_sec: 600
  research: { max_searches: 20, max_fetches: 30, min_interval_sec: 2.0, max_per_host: 5, fetch_max_bytes: 2097152, user_agent: … }
schedule:
  improve: weekly                             # weekly | daily (既存)
  improve_at: "Sat 03:00"                     # weekly は "<weekday> HH:MM"、daily は "HH:MM"。表示 TZ
```

- `RunnerChoice.backend` の正規表現を `^(local|claude|codex)$` へ。`RunnerSettings` に `claude: ClaudeCliSettings` / `codex: CodexCliSettings` を追加 (`_Strict`)。`settings.yaml.example` を同期
- **起動時検査** (`service.build_app` の既存 `_check_llama_swap` の隣): 選択された backend について ①`bin` を `shutil.which` → `realpath` で**絶対パスへ正規化**し、実行可能な通常ファイルであること。**codex は ELF (vendor native) を要求し、node ラッパ (`codex.js` / shebang スクリプト) は拒否** — エラー文言に探し方 (`npm root -g` 配下 `@openai/codex/node_modules/@openai/codex-linux-x64/vendor/.../bin/codex`、または `codex --version` を打つラッパの中身) を書く (codex 3 周目 I8。node ラッパの closure は起票 §10) ②`<bin> --version` が 0 で返る (rlimit なし・親プロセス側で) ③認証ファイルが存在する (**claude と codex+chatgpt のみ**。codex+llama_swap は要求しない — §1.1-2) ④codex+chatgpt は `chatgpt_subscription_active_until` を読んで 7 日以内なら WARNING、過ぎていれば ERROR ⑤**improve+claude のとき、サービス自身の初期 env (`/proc/self/environ`) に秘密名パターン (`*_API_KEY` / `*TOKEN*` / `*SECRET*` / `*WEBHOOK*` / `ANTHROPIC_*` / `OPENAI_*`) の変数があれば起動拒否** — 理由: claude worker は `/proc` を読めるため親の初期 env を読み戻せる (R10)。文言は「秘密は exported env でなく `.env` に置くこと」(`python-dotenv` は `os.environ` に setenv するだけで `/proc/self/environ` には現れない)。**①②③⑤のいずれかを欠けば `build_app` は起動拒否 (fail closed)** — Landlock 不可時の improve 起動拒否と同じ扱い。backend=local の環境では一切走らない
- **子へ渡す argv は解決済みの絶対パスのみ**。exec closure (§2.2) は解決後のバイナリの realpath ディレクトリから作る

### 1.5 契約 — 揃えるもの・揃えないもの

| 項目 | 契約 |
|---|---|
| 終端 status | 4 値 (`completed` / `failed` / `timeout` / `max_turns`)。3 実装とも同一の契約テストスイートに合格する (**運用時に選ばれるのは 1 つ**。スイートは fake CLI スクリプト = 決められた JSON を吐く python で回し、実 LLM を呼ばない) |
| `reason` | spec ② §4.3 と同じ (安全化済み・単一行・上限・外部応答本文を生で入れない) |
| `output_schema` | 3 実装とも `jsonschema` で検証し、不適合は `failed` |
| `timeout_sec` | 壁時計。**常に優先** |
| **`max_turns`** | **意味は runner ごとに文書化し、揃えない**: Local = LLM 呼出回数 (現行)。Claude = CLI `--max-turns` (structured output は 1 ターンで終わらず `num_turns:3` — probe P2。改善 Mission の値は 200 以上を推奨し、`schedule`/prompt 側の既定を合わせる)。Codex = 上限なし → **`max_turns` 終端は到達不能**、timeout が唯一の上限 (docstring と契約テストで明示: codex は `max_turns` を渡しても無視する) |
| 課金鍵・個人設定 | 3 実装とも子 env に鍵が無い / claude・codex は scratch home のみ (§1.1) |
| **改善 Mission の値** (codex 3 周目 M3) | `improve.mission_max_turns` (既定 200、ge 1) / `improve.mission_timeout_sec` (既定 3600、ge 60) を `Mission.max_turns` / `Mission.timeout_sec` に写す。写像: **Local** = `max_turns` を LLM 呼出回数上限に、`timeout_sec` を壁時計に / **Claude** = `--max-turns <max_turns>`、`timeout_sec` を壁時計に / **Codex** = `max_turns` は無視 (到達不能を docstring)、`timeout_sec` のみ |

`runners/base.py` の docstring (`:17-28`) は「runner-neutral turn semantics」を上記の表へ改める。

### 1.6 ツール公開 — 3 backend で同一の registry

- registry は §3.4 の improve registry (`build_mission_registry("improve", …)`) 1 つ。**Local は in-process、claude / codex は MCP stdio シム経由**で同じ `ToolDef` を見る
- **MCP シム** `python -m agentic_fx.tools.mcp_shim <unix socket path>`: CLI が MCP サーバとして起動する子プロセス。**ツール本体は持たない** — MCP の `initialize` / `tools/list` / `tools/call` を JSON-RPC で受け、`workdir/afx.sock` (Unix ドメインソケット) 越しに mission_worker へ転送するだけ。mission_worker 側は専用スレッドで `tools/list` → `registry.openai_tools(allowed)` の変換、`tools/call` → `registry.execute(name, args, allowed)` を返す。**親への RPC が要るツール** (`run_backtest` / `analyze_corr`) は既存の `tool_rpc` パイプ (in-flight 1) に乗る — シム経由でも in-flight 1 は保たれる (CLI は tools/call を直列に呼ぶ想定だが、mission_worker 側で lock を取って直列化する)
- MCP ライブラリのサーバ側実装は使わない (依存を増やさない。stdio JSON-RPC 2.0 + MCP の 3 メソッドのみ)。プロトコル版は実装計画で CLI 2 種の要求を確認する
- staging ファイルツール (§3.4) の根は **handshake の `staging_dir`**、`read_plugin_source` の根は **handshake の `source_snapshot_dir`** (§2.2 I7/I6) から導く。シム・registry・prompt が同じ値を見る
- claude 定義: `--mcp-config workdir/mcp.json` = `{"mcpServers":{"afx":{"command":"<sys.executable>","args":["-m","agentic_fx.tools.mcp_shim","<sock>"],"env":{"PYTHONPATH":…}}}}`。codex 定義: `-c mcp_servers.afx.command=… -c mcp_servers.afx.args=[…]`
- **許可集合は profile ごとに固定**:
  - improve + claude: `--allowedTools "mcp__afx__*,Bash,Read,Write,Edit,Glob,Grep"` (CLI ネイティブのファイル/shell を許可。Landlock が唯一の線である以上、ツール層で絞っても形式的 — プラン 9 §5 D3 放棄裁定)
  - improve + codex: shell/ファイルは常にある + `mcp_servers.afx`
  - trade + claude: `--allowedTools "mcp__afx__*"` のみ。**`Bash`/`Write` が含まれないことを argv pin**
  - trade + local: 現行どおり

### 1.7 本体設計書 §4 の改訂点 (プラン 10 実装完了時に反映)

1. 「ClaudeRunner (Claude Agent SDK)」→「ClaudeRunner / CodexRunner (システム CLI 直駆動)」。SDK 不採用の根拠 (R3) を 1 段落
2. 「ツールレジストリを SDK の in-process MCP サーバとして公開」→「MCP stdio シム経由 (ツール本体は worker)」
3. 3 形態の自動変換 (function calling / MCP / 素の関数) は維持。MCP 形態が「シム経由」になる
4. CodexRunner の追加 (improve 専用・provider 2 択・`max_turns` 到達不能)
5. 「認証は `claude login`」→「認証は各 CLI のログイン (サブスク)。サービスは認証ファイルだけを Mission ごとの scratch home にコピーして使う」

**変異 (§1)**: env に `ANTHROPIC_API_KEY` を通す (→ pin が落ちる) / scratch home でなく実 `$HOME` を渡す / `--setting-sources ""` を落とす / codex の `--disable plugins…` を落とす (→ argv pin) / `--dangerously-bypass…` を落とす (→ shell 全滅、E2E で検出) / trade+claude の allowedTools に `Bash` を足す (→ pin) / `runner.trade.backend=codex` を通す (→ validator pin) / timeout 後に CLI セッションを killpg しない・completed で pgid を空にせず返す (→ 全終端で孫残留ゼロのテスト) / CLI を worker と同一 pgid で起動する (→ 内側 killpg が worker 自身を殺し result が返らない fake テスト) / **worker 側 (Landlock 下) で認証原本をコピーしようとする (→ `EACCES` で Mission failed になる実プロセス pin。親コピーが正)** / `preexec_fn` で PDEATHSIG を設定する (→ dispatcher スレッド稼働中の spawn テスト、実装計画で hang 再現) / `parse_json_output` を通さず生 JSON を `json.loads` (→ フェンス付き fake 出力で failed になる契約テスト) / reason に stderr 全文を入れる (→ 安全化契約テスト) / `max_turns` 超過で `completed` を返す (claude fake の `num_turns` 上限テスト)。

---

## 2. improve worker profile の拡張 — 権限境界と不変条件 (裁定①)

### 2.1 不変条件 (受入条件の核。§7.1-1 で実プロセスに対して測る)

improve worker プロセスとその**全子孫** (claude / codex CLI・MCP シム・pytest・shell) について:

1. `data/` 配下 (DB・履歴・RAG) に読み書きとも到達できない (`open` / `listdir` / `truncate` / `exec` すべて `EACCES`)
2. 書き込み可能パスが **①候補置き場 `<root>/plugins/_staging/<mission_id>/` ②workdir (scratch home・`TMPDIR` を含む) ③`/dev`** に閉じる。**`reports/`・`plugins/` 全体・リポジトリ本体・`config/`・`policy/` には書けない**。`reports/` は案 A (R6) により**親だけが書く** — worker が書ける場所に親が予測可能な名前で書き込む構造を作らない (codex 1 周目 C1: 所有境界の単純化)
3. 従量課金経路が無い (env に鍵が無い — §1.1)。**どの子プロセス (trade worker を含む) の初期 env にも秘密を置かない** — trade worker の資格情報は handshake で渡す (R10-①)。**明示的な例外 (R11)**: サブスク backend (claude / codex+chatgpt) では `workdir/cfg` の OAuth 認証コピーが agent (shell / Read) から**可読**である — CLI 自身がそれを読んで動く以上、同一 UID・同一 Landlock allowlist の中で agent だけから隠す手段が無い。「課金鍵は無い」は保ち、「サブスク認証トークンは agent から可読」を受容する (§2.4)
4. 個人設定を継承しない (scratch home / `--ignore-user-config` / `--setting-sources ""`)
5. **shell を許す = `/usr/bin` 配下の実行 (と読取拡大) を許す**、と明示する。改善 profile の claude / codex は shell を持つので、execute closure に `/usr/bin` (この環境では `/bin` → `/usr/bin` の symlink) が入る。読取範囲がその分広がることを不変条件 1 と両立させる (どちらも `data/` の祖先ではない)

**「シェルが無いこと」は条件にしない** (プラン 9 §5 の D3 放棄裁定)。

### 2.2 Landlock 配線の変更 (`core/landlock.py` + `mission_worker._bootstrap_improve_profile`)

- `restrict_to(*, read_only_paths, read_write_paths, execute_paths: list[Path] = ())` に **`execute_paths` を追加**。マスクは `_EXECUTE_ACCESS = _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR` (**自己充足** — 同一 inode に対する ro ルールとの併合に依存させない。probe の `landlock_probe.py` と同一)。`_HANDLED_ACCESS_FS` は不変 (EXECUTE は元から handled)
- **exec closure を backend ごとに明示する** (codex 1 周目 C4 — CLI 本体だけ許しても、CLI が起動する shell / shebang interpreter で `EACCES` になり主機能に到達しない):

  | 区分 | パス (realpath) | 根拠 |
  |---|---|---|
  | 共通 | `sys.prefix`, `sys.base_prefix` | worker 内 pytest・MCP シムの python |
  | 共通 | `/usr/lib`, `/usr/lib64` | 動的リンクのローダ (claude・python。`/usr/lib64` 単独では不十分 — probe §2.2) |
  | shell 系 (improve+claude / codex) | **`/usr/bin`** (`bash`, `env`, coreutils)。bootstrap 時に `/bin` が実ディレクトリなら `/bin` も追加 (この環境は symlink) | codex は shell snapshot と shell tool で `/bin/bash` を起動 (probe §3、P6 は `PATH=/usr/bin:/bin` で完走)。claude の `Bash` ツールも同じ |
  | claude | `<runner.claude.bin>` の realpath の親 (`~/.local/share/claude/versions`) | probe §2.2 |
  | codex | `<runner.codex.bin>` (vendor native、起動時検査で ELF を要求) の realpath の親 | probe §2.1 は vendor native 直指定で完走。node ラッパは受理しない (§1.4、起票 §10) |
  | local | 共通のみ (CLI・shell 系を入れない) | LocalRunner は subprocess を起こさない (worker 内 pytest のみ) |

  **起動時検査 (§1.4) が shebang を解決し、interpreter が closure に無ければ起動拒否 (fail closed)**。実装計画で closure を「1 要素 drop で何が壊れるか」の pin にする (実測して固定。推測で増やさない)
- **read_only 追加**: `/run/systemd/resolve` (外部 DNS。`/etc/resolv.conf` が symlink で Landlock は解決先で判定 — probe §2.1。存在するときのみ) / `/proc` (**claude のみ**。bun が panic して SIGABRT — probe §2.2)。**`/proc` 許可の代償 (codex 3 周目 C1、R10)**: 同一 UID の他プロセス (親サービスを含む) の `/proc/<pid>/environ` / `cmdline` を読める。緩和: ①どの子の初期 env にも秘密を置かない (trade 資格情報は handshake、§2.2 末尾) ②サービス自身の初期 env に秘密があれば improve+claude を起動拒否 (§1.4-⑤) ③残余は §2.4 に明記。**実装計画で非特権 PID+mount namespace (`unshare -Upfm --mount-proc` 相当) を claude launcher に実測し、この host で動けば既定にして `/proc` を allowlist から外す** (§7.2 実測項目・§10)。codex は `/proc` 不要 (probe) なので影響なし
- **`/dev` を read_only → read_write へ**。理由: `subprocess.DEVNULL`・bash のリダイレクト・pytest logging が `/dev/null` を書込オープンする (probe §5-③、P7)。**脅威分析**: rw マスクに `MAKE_CHAR` も `MAKE_SYM` も無い (現行 `_READ_WRITE_ACCESS`、`landlock.py:111-114`) のでデバイスノード・symlink の作成は不可。既存デバイスへの write は Unix パーミッション次第で `/dev/shm` (tmpfs) には書ける — `data/` 到達には寄与しない。`_ACCESS_FS_IOCTL_DEV` は従来どおり handled にしない (プラン 8 の判断を維持)
- **read_write 追加**: `<root>/plugins/_staging/<mission_id>/` **のみ**。`<root>/plugins/` 自体・`<root>/reports/` は入れない。**その値は handshake の新フィールド `staging_dir` (絶対パス) で 1 つだけ渡す** (codex 2 周目 I7): 親 `WorkerRunner` が 0700 で作成し、子は Landlock 適用前に ①dirfd で開き所有者 == uid・mode 0700・通常ディレクトリ ②パスが `<...>/plugins/_staging/<mission_id>/` の正規形に一致 (mission_id は handshake の値) を再検証してから、**Landlock の rw ルール・staging ツールの根 (§3.4)・prompt に埋めるパス (§3.2) の全てをこの 1 値から生成**する。root・DB パス・plugins_dir は従来どおり渡さない (`db_path=None, plugins_dir=None`)。**handshake の improve 用フィールドは `mission_id` / `staging_dir` / `source_snapshot_dir` の 3 つで、子は `staging_dir` のパス末尾が `mission_id` と一致することを相互照合する** (codex 3 周目 I5)。`source_snapshot_dir` = `workdir/source/` — 親が prepare で**承認済み plugin の 3 本 (稼働中 registry の固定 `PluginMeta.path` から、flock 下・hash 再照合) と `docs/examples/plugins/*` (`_examples/`) を読取専用コピー**した場所 (I6 / 4 周目 I8・M3)。worker は `plugins/` そのものを一切見ない (Landlock ro にも入れない — 他 Mission の staging・未承認版・`.history.git` を見せない)。workdir は既に ro/rw 対象なので追加ルール不要 (`source/` は親が 0500 で作る)
- **`_assert_allowlist_excludes_data_dir` を拡張**: 入力を `read_only + read_write + execute` の全部にする。加えて**静的 pin**: `read_write_paths` の集合が `{staging, workdir, /dev}` と**一致**し、`<root>` 配下は `plugins/_staging/<id>` 以外を含まないこと (テストは `_bootstrap_improve_profile` が組む allowlist を dry-run で取り出して assert)
- **rlimit**: `child_fsize_mb=8` は維持 (codex は `--disable plugins…` で 664KB が最大 — probe §5-⑧)。**claude を rlimit 下で実 1 ターン回すのは未測** → Task 13 の実測項目 (超えるなら improve のみ `child_fsize_mb` を上げる)。**`RLIMIT_NPROC` は mission worker に適用しない** (現行どおり。plugin sandbox のみ)
- **env 追加** (`_mission_worker_env` の improve 分岐): `HOME=<workdir>/home`, `TMPDIR=<workdir>/tmp`, `CODEX_HOME=<workdir>/cfg` または `CLAUDE_CONFIG_DIR=<workdir>/cfg`。**ディレクトリ作成と認証コピーは親が spawn 前に行う** (§1.1-2)。子は何も作らない
- **trade worker の資格情報を env から handshake へ移す (R10-①)**: 現行 `_mission_worker_env` は trade profile に `TWELVEDATA_API_KEY` / `MT5_BRIDGE_API_KEY` を env で渡す (`worker_runner.py:39-61`)。これを **handshake フレームの `credentials` フィールド (stdin)** に移し、`_mission_worker_env` はどの profile にも資格情報を渡さない。trade worker は handshake 読取後、datafeed コードが env を参照するなら `os.environ` に setenv する — **setenv した値は `/proc/<pid>/environ` (初期 env ブロック) には現れない**。pin: 全 spawn の env に `*_API_KEY` が無い (fake Popen で assert)。改善 worker は従来どおり資格情報ゼロ

### 2.3 候補置き場 (staging) の意味論

- worker が plugin を書ける唯一の場所は `plugins/_staging/<mission_id>/<name>/`。**稼働中の `plugins/` (live symlink・版・`.history.git`・他 Mission の staging) は worker から不可視** — 読めるのは親が prepare で作った承認済み 3 本の読取専用スナップショット (`source_snapshot_dir`、§2.2/§3.4) だけ (codex 4 周目 M1)
- 根拠: 承認済み plugin をその場で書き換えると content_hash が承認済みハッシュと不一致になり、**承認が下りるまでその plugin は `approved_plugins()` から消える** — 週次の改善が稼働中の指標を止める。staging なら承認までは旧版が生き続ける
- **plugin 名の正規形** (codex 1 周目 C2): `^[a-z][a-z0-9_]{0,63}$` — 単一パス成分。`.`・`..`・`/`・絶対パス・大文字・ハイフンを含まない。**出力 schema (§3.5) と親側 (§4.2-1) の両方で検証**する。候補パス `staging/<name>` と切替先 `plugins/<name>` の検査は **`resolve()` を使わない** (codex 2 周目 I1 — 承認済み plugin は symlink なので resolve すると `.versions/` 配下になり、二度と更新できなくなる): 字句上の単一成分検査 + それぞれの dirfd 基準の `lstat` / `openat(O_NOFOLLOW)` で、最終成分が **{不存在 / 通常ディレクトリ (プレーン、初回移行前) / 正規形の相対 symlink}** のどれかであることだけを見る。symlink の場合は**リンク先文字列**が `.versions/<name>/<artifact_hash>` の正規形 (`^\.versions/<同じ name>/[0-9a-f]{64}$`) に一致することを確認する (辿らない)。同じ正規形を `afx plugin bless` / `submit` の CLI と `plugin/loader.discover` にも適用する — **正規形に反する既存の plugin ディレクトリは discover が WARNING を出して skip する** (ロードされなくなる。移行は人間が rename)
- **版ストア `plugins/.versions/` は不変** (codex 9 周目 I3): 親が作成時にディレクトリ 0500 / ファイル 0400 にする。人間が編集する場所ではない (運用ドキュメントに明記)。**discover は版ディレクトリ名 (= `artifact_hash`) と実計算の `artifact_hash` を照合し、不一致なら その版を拒否 + activity ERROR** (in-place 編集の検出。reconcile の入口でも同じ照合)
- **人間が編集して承認したいとき (R12-(d))**: **live path `plugins/<name>` はプレーンでも symlink でも編集対象にしない**。`afx plugin materialize <name>` が live (版ディレクトリ、またはプレーン dir) を `plugins/_human/<name>/` へコピーする (既に在れば拒否。ディレクトリ 0700 / ファイル 0600。人間所有 — 自動削除しない)。編集後は `afx plugin submit --from _human <name>` (pending 承認申請) または `afx plugin bless --from _human <name>` (submit + 人間決定 approved) が**改善ループの候補と同じ経路** (§4.2-3/4 のゲート → pending 行 + 証跡 → §5 の版化・切替 → 決定) を通す。`bless <name>` (live を候補に取る形) は無い。`_human/` は `_` 先頭なので discover に列挙されず、worker の rw にも入らない。**本プラン以前の手作りプレーン plugin** も同じ: `materialize` → `_human` → `submit|bless --from _human`。初回の承認で live はプレーン dir から symlink に入れ替わり、旧 dir は `plugins/_retired/<name>-<ts>/` に退く (§5.1)
- **discovery は live symlink を辿った先の版ディレクトリを `PluginMeta.path` に固定し、`PluginMeta.artifact_hash` (3 本マニフェスト) も discover 時に計算して保持する** (codex 3 周目 I4 / 5 周目 M1 — source snapshot はこの `content_hash`/`artifact_hash` の両方と照合する): `discover` は `plugins/<name>` が正規形 symlink なら検証後に `.versions/<name>/<artifact_hash>` の実体パスを `PluginMeta.path` とする (プレーン dir はそのまま)。**稼働中のサービスは起動時に discover した版を再起動 (再読込) まで使い続ける** — approve による symlink 切替は**次回の起動/再読込にだけ**効く (hot reload はしない)。これにより切替中に実行中の plugin が壊れることも、`PluginMeta.path` が dangling になることも無い。pin: approve 後も起動済みサービスの sandbox が旧版パスを実行し続ける
- **`plugin/loader.discover` の列挙条件に「先頭が `_` または `.` のディレクトリを除外」を追加する** (codex 1 周目 M3: 現行は全子ディレクトリを列挙し、3 ファイル欠落で**偶然** skip されているだけ。`_staging/`・`.versions/`・`.locks/` (§5) を予約する)。`_reject_unexpected_py_files` の走査も同様。**Task 5 の受入 pin**
- ライフサイクル: 親が Mission 起動前に空 dir を作る → worker が書く → commit 相 (§4) でゲート (候補は**読取専用のスナップショット**として扱う) → 承認申請を出した候補は**決定まで残す** → 承認時に版ディレクトリ (`artifact_hash` キー) へコピー → git 記録 → symlink 切替 (§5、この順) → 決定 (approved / rejected / expired / invalidated) 時に削除。承認申請に至らなかった候補は commit 相の末尾で削除
- **孤児の掃除**: 起動時 reconcile (§5.3 の位置) で、`_staging/` 配下のうち「対応する pending の approval_request が無い」ものを削除する
- `.gitignore` の `/plugins/` はそのまま staging も覆う

### 2.4 残余リスク (明記)

- improve worker は任意コード実行 + ネットワーク (LLM・研究ツール) を持つ。plugin ソースの信頼モデルは従来どおり「人間承認まで信用しない」。egress は §6 のとおり**予算はツール契約であって安全保証ではない**
- `/dev/shm` への書込は可能 (§2.2)
- **プロセス包含は pgid ベース** (§1.1-4): `setsid()` を呼ぶ孫は CLI の pgid から逃げ、timeout / shutdown 後も生存し得る (LLM egress・staging 書込を継続)。完全な包含は cgroup v2 (systemd --user scope) が要る — **起票 (§10)**。`RLIMIT_NPROC` も掛けていない
- shell を許すため `/usr/bin` 配下の読取・実行が可能 (§2.1-5)
- **サブスク認証トークンの持ち出し (R11、codex 4 周目 C1)**: claude / codex+chatgpt では agent が `cat $CLAUDE_CONFIG_DIR/.credentials.json` / `cat $CODEX_HOME/auth.json` を実行でき、web 記事の prompt injection や誤作動で access/refresh token を shell/python の HTTP から送信できる。**受容 (fail closed にしない)** — このリスクは「成熟したハーネスをサブスクで使う」選択に固有で、機構で塞ぐには認証主体と任意コード実行主体を分ける (別 UID / credential broker / 短命トークン) 必要がある → 起票 §10。緩和: research 予算の advisory・prompt 規律・transcript の監査。**codex+llama_swap / local にはこの資格情報が無い** (llama_swap は auth.json をコピーしない — §1.1-2)
- **improve+claude は `/proc` を読める** (R10): 同一 UID の他プロセスの `environ` (初期 env) と `cmdline` を読める。Yama `ptrace_scope=1` が `mem` / `fd` を塞ぐ。緩和 ①② (§2.2) の下でも、**サービス以外の同一 UID プロセスが秘密を env に持っていれば読める** — 運用者は改善 worker と同じ UID で秘密を exported env に持つプロセスを走らせない。恒久対処は PID namespace (実測項目) か別 UID

**変異 (§2)**: `execute_paths` を `_assert_allowlist_excludes_data_dir` に渡さない (→ `data/` を execute に入れても通る、pin が落ちる) / `plugins/` 全体または `reports/` を rw に入れる (→ 静的 pin) / `/dev` を ro に戻す (→ subprocess.DEVNULL 経路の実測テスト) / `/usr/bin` を closure から落とす (→ shell 系 backend の 1 要素 drop pin) / staging を Mission id で分けず共有にする (→ 並行 Mission の衝突テスト) / `discover` が `_staging` / `.versions` を拾う (→ 承認前 plugin がロードされる pin) / 正規形に反する名前を discover が受ける (→ `Foo-bar` ディレクトリが skip される pin) / 孤児 reconcile を落とす (→ 起動時テスト) / `HOME` に実ホームを渡す (→ env pin)。

---

## 3. 改善 Mission — 起動・レーン・注入・道具・出力

### 3.1 起動とレーン (R7) — 週期 1 wave・並行 N・専用接続

- **起動契機と週期の CAS (codex 3 周目 I11 / 4 周目 I3・I4)**: `schedule.improve` (`weekly` / `daily`) と **`schedule.improve_at`** (`"<weekday> HH:MM"` = weekly / `"HH:MM"` = daily、表示 TZ `display_timezone`。既定 `"Sat 03:00"`)。scheduler tick は **`now` 以下で最新の scheduled occurrence** (weekly = 直近の該当曜日+時刻、daily = 直近の該当時刻。表示 TZ で計算) を求め、**その occurrence が属する period key** (weekly = occurrence の ISO 週 `YYYY-Www`、daily = occurrence の日付 `YYYY-MM-DD`) を CAS 対象にする — 「現在のカレンダー period」を先に選ばない。これにより**停止中に逃した period は起動後の最初の tick で 1 回だけ catch-up される** (最新の逃した occurrence のみ。それ以前は追わない)。**手動**: 対話シェル `improve` (M=1 の wave、**分担なし** = 全バックログを見る、period を消費しない、`improve_waves` に行を作らない)。どちらも missions 行 `loop='improve'`, `trigger` は NULL (設計書 §12 — trigger は trade 専用)
- **wave と slot (codex 4 周目 I4 / 5 周目 I1 / 6 周目 I1 / 7 周目 I1・I2、R12 で再開機構を簡素化)**: 新テーブル `improve_waves(period_key TEXT PRIMARY KEY, created_at TEXT NOT NULL, expected INTEGER NOT NULL)` と **`improve_wave_slots(wave_period_key TEXT NOT NULL REFERENCES improve_waves(period_key), k INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('reserved','claimed','running','done','failed')), mission_id INTEGER, spawn_attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(wave_period_key, k))`**。**slot は帳簿であって再開の単位ではない** (R12-(c))。手順: ①`M = min(improve.parallel, 空きスロット数)`。**M=0 なら何も書かない** (period 非消費、次 tick で再試行) ②**1 つの短い tx** で `INSERT OR IGNORE INTO improve_waves(period_key, created_at, expected) VALUES (?, ?, M)` (`rowcount=1` = 起動権) + **同じ tx で slot 行 `k=0..M-1` を `reserved` で INSERT** — **wave 行が存在した時点で period は消費済み** (実行 0 件でも次 tick で再 CAS しない。取りこぼした period は人間の `improve` で補う) ③**3-way 起動 (7 周目 I1)**: `reserved → claimed` (§4.1 Tx-0 と同じ tx で CAS `UPDATE … SET status='claimed', mission_id=?, spawn_attempts=spawn_attempts+1 WHERE … AND status='reserved'`) → worker spawn → worker `ready` → **親が短い tx で `claimed→running` を commit** → **親が `go` フレームを送る** → worker が Mission を開始 (`go` 前は agent/ツール実行ゼロ。`go` が来なければ副作用ゼロで終了)。`running` の commit と `go` の間で親が落ちても、worker は `go` を待って終了し、slot は `running` のまま → 起動時回収で `failed` (再実行しない) — 「実行済み Mission の再実行」は起きない ④**分担は負荷分散のヒント**: 親は prepare で**そのときの** `open|observation` 集合から `id % M == k` で `allowed_backlog_ids` を計算し RunContext に入れる (**永続化しない** — codex 7 周目 I3、簡素化側)。正しさは §4.1 Tx-1 の CAS が担う (敗者 → observation)。**手動 M=1 は全 id** ⑤**終端の直積 (7 周目 I2)** — 下表 ⑥**pre-ready の失敗** (spawn 失敗 / `ready` 前 crash・timeout) は**同一プロセス内でのみ** slot を `reserved` に戻して再 claim、**`spawn_attempts` は claim ごとに +1、失敗時に `< 2` なら `reserved` (初回 + 再試行 1 回)、それ以外は `failed`** ⑦**再起動後は再開しない**: `claimed` / `running` の slot で mission が interrupted になるものは全て `failed` (period は消費済みのまま)。**`started_at`・「全 failed の wave を削除して period を返す」規則は持たない** (簡素化) ⑧`submitted` は列で持たず `status IN ('running','done','failed')` の slot 数から導出 ⑨**手動 `improve` は wave/slot 行を作らない**

  | 事象 | slot | mission | run | 同一 tx か |
  |---|---|---|---|---|
  | Tx-0 (prepare) | reserved→claimed | INSERT (`running`) | INSERT (CREATED) | 1 tx |
  | spawn 失敗 / `ready` 前 crash・timeout (同一プロセス内) | claimed→reserved (`spawn_attempts` < 2 = 初回のみ) または failed (再試行後) | `failed` | FINISHED (`result=NULL`) | 1 tx |
  | `ready` 受信 | claimed→running | — | — | 短い tx、その後 `go` |
  | `ready` 後の全終端 (completed / failed / timeout / max_turns / 親の出力検査不合格 / Tx-2 / Tx-2 補償 / shutdown) | running→done (completed かつ Tx-2 成功) または failed | 終端 status | FINISHED | **1 つの短い tx** — 全経路が単一ヘルパ **`finish_improve_mission(conn, *, mission_id, run_id, slot_key\|None, mission_status, run_result, backlog_transition, commit=False)`** を通る (Tx-2 本体の末尾、または補償 tx)。手動 one-shot は `slot_key=None` (codex 8 周目 I2) |
  | 起動時: `claimed` または `running` で mission が interrupted | → failed (再実行しない) | interrupted | FINISHED、backlog `observation:interrupted` (BOUND なら) | `recover_interrupted` と同一 tx |
  | wave の全 slot が終端 | 残す (period 消費) | — | — | — |

- **別レーン**: 新設 `ImproveSupervisor` (`core/improve_supervisor.py`)。容量 `improve.parallel` (既定 1、上限 4)。**取引レーン (`MissionSupervisor`、容量 1) とは独立** — 改善実行中も取引 Mission は受理される。**preemption はしない**。実装は `MissionSupervisor` の一般化 (N スロット + `kind="improve"`) でも別クラスでもよいが、**取引レーンの直列性契約 (容量 1・原子的 try_submit) を変えない**ことを受入条件に置く
- **partition と担当集合**: 分担 `id % M == k` は**ヒント** (上記 ④)。親は出力の既存 `selected.backlog_id` がヒント集合外でも**拒否しない** — activity に `out_of_partition` を記録して Tx-1 の CAS に進む (勝てば正当、負ければ observation)。**新規 idea は集合と無関係**。発見・リサーチで新規に見つけた課題は自由に追加してよい (codex 3 周目 I10 の硬い拒否は 7 周目 I3 で撤回)
- **各 Mission は独立した improve worker** (WorkerRunner `worker_profile="improve"`、Mission ごとに構築し `run_context=` を渡す — §4 prepare) を持つ。workdir・staging・source snapshot・scratch home は Mission id 単位
- **SQLite 接続の所有 (codex 3 周目 I12 / 4 周目 I2)**: ①**slot スレッド**が prepare で `db.connect()` して所有し (write 接続)、prepare 短 tx / Tx-1 / Tx-2 だけに使い、`finally` で close ②**RPC dispatcher スレッド** (WorkerRunner 内) は Mission 専用の**別の読取専用接続** (`db.connect_readonly`) を**自スレッド内で**生成・close する。RunContext の `rpc_handlers` は接続でなく**接続ファクトリ**を受け取る。dispatcher は DB に書かない (永続化は台帳経由で slot スレッドの Tx-2) ③台帳 (`ImproveRpcLedger`、lock 付き) だけが両スレッドの橋。**接続をスレッド間で共有しない**。取引レーンの接続とも共有しない
- **重複解決の線形化点は §4.1 Tx-1 の backlog CAS** (先に `selected` を取った方が勝ち、後着は observation)
- **shutdown / join** (I4/I9): `ImproveSupervisor.shutdown()` は `stop_event` を立てて新規 submit を拒否し、走行中の N 本の WorkerRunner に stop を伝播 (`WorkerRunner` の既存 `stop_event` を共有 — 各 WorkerRunner が自分の worker を kill し、**`cli_started` で受け取った CLI pgid も killpg** してから `MissionResult` を返す) → 各 commit 相の `finally` が run と missions 行を終端 → `ImproveSupervisor.join(shutdown_join_timeout_sec)`。`run_service` の停止シーケンスは取引レーンの `supervisor.join` と**同じ位置で**改善レーンも join する (取引側の順序は変えない)。**worker が EOF/異常死したときも** WorkerRunner は finalize 前に CLI pgid を killpg する
- **LLM の競合**: backend=local で改善と取引が別 alias を使うと llama-swap がモデルを入れ替える (TTL・swap 遅延)。既定は同一 alias。設定で別 alias にした場合の遅延は受容 (警告を出す)。claude / codex は競合しない

### 3.2 注入コンテキスト (親が決定論的に集計してプロンプトに焼く)

`loops/improve_context.py` が生成し、`loops/prompts/improve_mission.md` (新規) のテンプレートに差し込む:

| 節 | 内容 | 出所 |
|---|---|---|
| 成績レポート | 直近 30/90 日の勝率・PF・ペア別・時間帯別・却下 intent の内訳 (reject_category 別)・hold 率 | `trade_intents` / `orders` (親の RO 集計) |
| 改善履歴 | 過去の `improvement_runs` (何を試し、approval / report / observation のどれで終わったか)。**各バックログ課題の試行回数と、strategy なら標本 (取引数)** を添える (R8) | `improvement_runs` / `improvement_backlog` / `backtest_runs` |
| 現行構成インベントリ | 組み込み + 承認済み plugin (kind・pairs・timeframe)、ニュースソース一覧、risk gate 現行値 | registry / `approved_plugins` / `news_sources` / settings |
| バックログ | open + observation の一覧 (担当 partition に印。手動 wave は印なし) | `improvement_backlog` |
| ユーザー方針 | `policy/directives.md` 末尾 4000 文字 (全 Mission 共通) | `Policy.tail` |
| 参照 | サンプル plugin (**親が prepare で `docs/examples/plugins/*` の 3 本ずつを `workdir/source/_examples/<name>/` へ読取専用コピー** — worker は repo の `docs/` を読めない、codex 4 周目 M3)・plugin 契約の要約・候補置き場のパス (`staging_dir`)・承認済み plugin の読取専用スナップショットのパス (`source_snapshot_dir`)・**plugin 名の正規形**・担当 backlog id 集合 | RunContext |
| 規律 | **ツール外の直接通信 (shell/python から HTTP) を禁じる** (§6 — 予算はツールでしか数えられない)。1 回の結果で課題を捨てないこと | prompt 定数 |

### 3.3 3 ステップ 1 Mission (設計書 §6 のまま)

1. **発見**: 注入内容から課題を特定
2. **リサーチ**: 研究ツールで外部知識を取り込む
3. **実施**: 1 件を選び、候補置き場に実装・自分でテストを回し、成果物を出力に載せる

### 3.4 道具 (improve registry — 取引 registry と分離)

`build_mission_registry("improve", …)` が返す集合。**取引 registry のツールは 1 つも含まない** (`get_ohlcv` / `get_signals` 等)。

| ツール | 実行場所 | 内容 | 制約 |
|---|---|---|---|
| `web_search(query, max_results)` | worker | ddgs (DuckDuckGo) 検索 | §6 の Mission 予算 (advisory) |
| `fetch_article(url)` | worker | 記事本文抽出 (`trafilatura`、既存依存。前身 article_fetcher 相当) | §6 の予算 / サイズ上限 / `data/` 不可視のまま |
| `list_staging()` / `read_staging_file(name, rel)` / `write_staging_file(name, rel, content)` | worker | 候補置き場のファイル操作。`name` は正規形 (§2.3)、`rel ∈ {plugin.py, config.yaml, test_plugin.py}` のみ。パス正規化して staging 外は拒否 | LocalRunner 用。claude/codex はネイティブでも同じ場所しか書けない (Landlock) |
| `read_plugin_source(name)` | worker | **親が prepare で作った読取専用スナップショット `workdir/source/<name>/`** を読む (改良の起点)。**コピー元は稼働中 registry が保持する固定 `PluginMeta.path` (版ディレクトリ実体) であって live symlink `plugins/<name>` ではない** — plugin `flock` 下で 3 本を 1 マニフェストとしてコピーし、コピー後に `content_hash`/`artifact_hash` を registry の値と再照合する (混成スナップショット・未 admit 版の混入を防ぐ、codex 4 周目 I8)。worker は `plugins/` 本体を見ない (3 周目 I6) | 読取のみ |
| `run_plugin_tests(name)` | worker | `python -m pytest -q -p no:logging <staging>/<name>/test_plugin.py` を subprocess (rlimit 継承・timeout `plugin.pytest_timeout_sec`) | 結果は**参考** — 親が改めて回す (§4) |
| `run_backtest(name, pair)` | **親 RPC** | `holdout.run_in_sample` を親が回し、**集計指標のみ** (取引数・PF・勝率・平均 R・DD。期間端点なし) を返す。**DB には書かない** — 呼出し (params・result・mission_id) は親の **Mission 内 RPC 台帳** (メモリ) に積まれ、commit 相で `backtest_runs` (`scope=in_sample, issued_by=harness`) として永続化される (§4.1 Tx-2、codex 1 周目 I1) | 遮断 1・2 |
| `analyze_corr(request)` | **親 RPC** | 既存 `backtest.analysis.analyze_for_agent` (列挙制パラメータ・固定個数の要約統計)。**`analysis_runs.save` は呼ばない** (`persist=False` 相当に改修) — 同じく台帳経由で commit 相に永続化 | 遮断 7 |
| **無いもの** | — | `get_signals` / 任意 SQL / `ohlcv_*` 直読 / holdout / `bless` / approval 発行 / backlog 書込 / news 提案 | 遮断 3・6・8、R2、R6 |

RPC は既存 `tool_rpc` フレーム (in-flight 1) に `run_backtest` / `analyze_corr` を追加する。**親側 dispatcher スレッドは自前の読取専用接続 (`connect_readonly`、Mission 専用) で handler を実行する** (§3.1 接続の所有)。dispatcher は `rpc_timeout_sec` を **RPC 種別ごと**に持つ (バックテストは数十秒〜数分。`improve.backtest_rpc_timeout_sec` 既定 600)。**RPC 台帳** (`ImproveRpcLedger`、Mission ごと・メモリ、lock 付き状態機械 `OPEN → FROZEN → PERSISTED | DISCARDED` — codex 2 周目 I4): dispatcher は**期限内に完了した**各呼出しの `{opaque_ref, kind, params, result_summary, trial_count}` を `OPEN` の間だけ追記する。`trial_count` は `analysis_runs.trial_count` と同じ意味 (= 計算した相関値の個数。lead-lag 1 呼出しでも 25 になり得る) で、`analyze_for_agent` が返す値をそのまま持つ。commit 相は先頭で台帳を**原子的に `FROZEN`** にしてから読む。**FROZEN 以後に届く遅延 RPC 完了は missions.status に関係なく拒否** (activity にカウント)。**RPC timeout (`rpc_timeout_sec` 超過) した呼出しは監査上「試行」に数えない** — その遅延結果は捨てる。監査に載るのは期限内完了分のみ (この規則を payload の説明文にも焼く)。台帳は成功 commit 後 `PERSISTED`、Mission 失敗/timeout で `DISCARDED`

### 3.5 出力 schema (`loops/summary.py` に `IMPROVE_OUTPUT_SCHEMA`)

```json
{
  "discoveries":  [{"idea": str, "source": "agent"|"research", "evidence": str}],   // 上限 §4.2-2
  "selected":     {"backlog_id": int|null, "idea": str},                             // 既存 id か新規
  "artifact":     {"type": "plugin", "name": "^[a-z][a-z0-9_]{0,63}$", "kind": "indicator"|"signal"|"strategy",
                   "self_test": "passed"|"failed"|"not_run", "summary": str}
               |  {"type": "report", "proposal_kind": "core"|"risk_gate"|"research", "title": str, "body_md": str}
                                      // risk_gate は本プランでは受理せず observation `unsupported_in_plan10` (R12-(b)、§4.2-6)。評価付きの受理は §A.2 / 起票
               |  {"type": "observation", "reason": str},
  "selection_rationale": str        // 分析 id・探索回数は agent に書かせない — 親が RPC 台帳から作る (codex 1 周目 I7)
}
```

`analysis_run_ids` / `trial_count` は**出力 schema に無い**。approval payload とレポートに載る値は親が台帳から生成する (§4.2-5)。agent が本文中に id を書いても無視される。

### 3.6 失敗の扱い

timeout / 出力不正 / worker 異常死 → **`finish_improve_mission` (§4.1) の 1 tx** で missions 行を該当 status で終端、**prepare で作った `improvement_runs` の行** (§4.1 run lifecycle) を `result=NULL` のまま `finished_at` で終端、scheduler wave なら slot を `failed` (新しい終端値は足さない — 既存 CHECK `('approval','report')` を維持し、失敗は missions 側で読む)、staging を削除、**RPC 台帳を `DISCARDED` (`backtest_runs` / `analysis_runs` は書かれない)**。**再試行はしない** (次の wave で自然に再実行。R8 により課題は消えない)。**プロセス crash (Tx-1 後) の回収**: 起動時の `missions.recover_interrupted` と同じ transaction で、interrupted になる improve Mission に結びついた run (`improvement_runs.mission_id`) の backlog `selected` 行を `observation` (`last_result='interrupted'`) へ戻す (§4.1 Tx-1 が run↔backlog↔mission を durable に結ぶ — codex 3 周目 I2)。

**変異 (§3)**: 改善を取引レーンに submit する (→ 取引受理テスト) / wave が M=1 しか起こさない (→ `parallel=4` で 4 partition が全て担当されるテスト) / `IMPROVE_FORBIDDEN` のツールが improve registry に居る (→ 既存 pin 拡張) / `run_backtest` が期間端点を返す (→ 返却 schema pin) / **RPC が `backtest_runs` に直接書く** (→ timeout Mission 後に行が残るテスト) / 台帳が finalize 後の遅延 RPC を受け付ける (→ 遅延注入テスト) / `write_staging_file` が `..` を通す (→ パス正規化テスト) / `run_plugin_tests` の結果でゲートを省く (§4 の変異) / 週次判定を落とす (→ scheduler pin) / shutdown が改善レーンを join しない (→ 停止時に worker 残留テスト)。

---

## 4. 親側 commit 相 — 出力の検証とゲート (`loops/improve_loop.py`)

`ImproveLoop` は `TradeLoop` と同じ prepare / run / commit の三相。**prepare** (core_lock 内・短時間、slot スレッド・専用 write 接続): **Tx-0 (1 つの tx): `missions.start(commit=False)` → `improvement_runs.start(mission_id, backlog_id=NULL, commit=False)` → wave slot の claim (§3.1、**scheduler 起動 wave (weekly / daily) のとき。手動 one-shot は除く**) → COMMIT** (run は Mission と同時・原子的に生まれる — codex 4 周目 I10 / 5 周目 I4) → workdir/staging (0700)/`source/` スナップショット (**稼働中 registry の固定 `PluginMeta.path` から** 3 本を plugin `flock` 下でコピーし hash 再照合 — §3.4、+ `docs/examples/plugins/*` を `source/_examples/` へ) を 0500 で作成 → 認証コピー (§1.1-2) → 注入コンテキスト生成 → **`ImproveRunContext` を構築** (immutable: `mission_id` / `run_id` / `staging_dir` / `source_snapshot_dir` / `allowed_backlog_ids` (ヒント、§3.1) / `ledger` (`ImproveRpcLedger`, OPEN) / `rpc_handlers` (`run_backtest` / `analyze_corr` の親側実装、**読取専用接続ファクトリ**を受け取り dispatcher スレッドで自前接続を開く — §3.1)) → その Mission 専用の `WorkerRunner(..., worker_profile="improve", run_context=ctx)` を構築 (codex 3 周目 I5。`Mission` は不変。handshake は ctx から `mission_id` / `staging_dir` / `source_snapshot_dir` を別フィールドで運び、子が相互照合する)。**run** (lock 外): `WorkerRunner.run(mission)`。**commit** (lock 外、専用 write 接続)。

### 4.1 transaction 設計 (codex 2 周目 C2 / 3 周目 I2・I3・I12 / 4 周目 I1・I10) — 長い仕事は transaction の外、DB 書込は短い 3 回、終端は 1 点

SQLite の writer は 1 本なので、**`BEGIN IMMEDIATE` を pytest / バックテスト越しに保持してはならない** (取引レーンが busy timeout を踏み R7 が破れる)。接続は slot ごとに専用 (§3.1)。commit 相の DB アクセスは次の短い transaction に閉じる:

- **Tx-0 (prepare、短い、唯一の読み方)**: `BEGIN IMMEDIATE` → **`missions.start(commit=False)`** (missions INSERT) → `improvement_runs.start(mission_id, backlog_id=NULL, commit=False)` (run INSERT) → (**scheduler 起動 wave (weekly / daily) のとき。手動 one-shot は除く**) `improve_wave_slots` の claim UPDATE → `COMMIT`。**run の生成時点は Mission と同じで原子的** — 出力不正・timeout・worker 異常死でも履歴 (run) が残り、Mission だけが commit されて run が無い状態は作らない。**`improvement_runs.mission_id` は新規行について一意** (`CREATE UNIQUE INDEX ... ON improvement_runs(mission_id) WHERE mission_id IS NOT NULL` — 既存移行行の NULL は許す)
- **Tx-1 (早い短い tx、選択の線形化 + 所有の bind)**: `BEGIN IMMEDIATE;` ①`discoveries` の INSERT (重複・上限は §4.2-2) ②`UPDATE improvement_backlog SET status='selected', attempts=attempts+1, updated_at=? WHERE id=? AND status IN ('open','observation')` — `rowcount=1` がこの Mission を唯一の勝者にする (I2) ③**`UPDATE improvement_runs SET backlog_id=? WHERE id=?`** (run ↔ backlog ↔ mission を durable に結ぶ) → `COMMIT`。新規 idea なら同じ tx で INSERT → UPDATE。`rowcount=0` (並行 Mission が先に取った / 人間が閉じた) → **敗者経路**: run は `backlog_id=NULL` のまま、ゲート作業を一切せず、レポート (「重複のため見送り」) と run finish だけを Tx-2 で書く
- **transaction 外**: 出力検査・スナップショット・AST・pytest・in-sample・holdout・レポート本文生成・レポートファイル作成。**親が回す `run_in_sample` / `run_holdout_gate` は non-committing 版**を使う — 既存 `_run_scope → save_harness_run(commit)` を **caller-owned sink (`record_fn`)** に差し替え、行は commit 相が Tx-2 で書く。`analyze_for_agent` も同様 (`persist=False` → 保存パラメータを返す)
- **Tx-2 (最後の短い tx — 終端の単一 commit point、codex 4 周目 I1)**: `BEGIN IMMEDIATE` → 台帳の `backtest_runs` / `analysis_runs` 行 → 親ゲートの `backtest_runs` 行 (in-sample / holdout_gate) → `approvals.create(commit=False)` → **`finish_improve_mission(..., mission_status='completed', slot_status='done', run_result=…, backlog_transition=…, commit=False)` (単一ヘルパ — 中で backlog 遷移 + `last_result`、`improvement_runs.finish`、`missions.finish` の CAS `WHERE id=? AND status='running'` (影響行 0 なら例外 → 全体ロールバック)、slot `running→done` を行う。個別 UPDATE は書かない — codex 9 周目 M1)** → `COMMIT`。**approval・run・backlog・mission の終端が 1 つの commit point** — Tx-2 直後〜mission 終端の間の crash 窓は消える。途中失敗は全部ロールバック (staging は残る) → **補償 tx** (短い、**`finish_improve_mission`** 経由): backlog を `observation` (`last_result='commit_failed'`)、run を `result=NULL, finished_at`、missions を `failed` (CAS)、**slot を `running→failed`** (scheduler wave のとき) で終端 (codex 8 周目 I2)。**`ready` 後の全終端 (Tx-2 成功・補償・出力検査不合格・timeout/failed/max_turns・shutdown) はこの 1 ヘルパだけを使う** — slot+mission+run+backlog を同一 tx で更新する唯一の経路
- **store helper に caller-owned transaction 版を用意する**: **`missions.start`** / `save_harness_run` / `approvals.create` / **`approvals.decide`** / **`approvals.expire_due` (行を 1 件ずつ列挙して処理する版)** / `improve_runs.start` / `improve_runs.finish` / `analysis_runs.save` / `backlog.set_status` / **`missions.finish`** の各々に `commit=False` (呼び出し側が transaction を持つ) 変種。現行の内部 `conn.commit()` はそのまま残し (他の呼び出し元は不変)、improve レーンと承認決定 (§4.3 の `apply_decision`) だけが変種を使う
- **report の公開状態 (codex 6 周目 I2)**: `improvement_runs` に `report_state TEXT NOT NULL DEFAULT 'none' CHECK(report_state IN ('none','prepared','published','failed'))` を `ensure_column` で追加。Tx-2 は `prepared` + 予定 `report_path`、COMMIT 後の公開 (rename) 成功で短い tx `published`、公開失敗で短い tx `failed` + `report_path=NULL` + backlog `done → observation(report_failed)`。**eventual な不変条件 (reconcile 完了後・静止状態): `published ⇔ 最終ファイルが存在**。遷移中は `prepared + 最終あり` を一時的に許す (codex 7 周目 M1)。起動時 reconcile: `prepared` かつ `.part` あり → 今公開して `published` / `prepared` かつ最終あり → `published` にする / `prepared` かつ両方無し → `failed` 補償 (`result=NULL`, `report_path=NULL`, backlog `report_failed`) / **`published` かつ最終ファイル無し → 同じ補償 (`report_state=failed`, `result=NULL`, `report_path=NULL`, backlog `done→observation(report_failed:missing)`, activity + 通知)** (codex 8 周目 I10) / 命名規則に一致するが**どの run からも参照されない**最終・一時ファイル → 削除。**耐久性要件: `.part` の `fsync` に加え、rename 後に `reports/.tmp` と `reports/` のディレクトリ fsync**
- **起動時回収 (I2/I1)**: `missions.recover_interrupted` と同じ transaction で、**`improvement_runs.finished_at IS NULL` かつその `mission_id` の Mission が interrupted になる run だけ**を対象に、run を `result=NULL, finished_at` で終端し、`backlog_id` が指す backlog が `selected` なら `observation` (`last_result='interrupted'`)、scheduler wave の slot は `running→failed` (= `finish_improve_mission` と同じ更新集合。§3.1 終端表)。**`finished_at` が入っているのに mission が `running` の run は Tx-2 の単一 commit point により存在しない** — これを起動時 assert (activity ERROR) で pin する

**run のライフサイクル (codex 4 周目 I10)**:

| 状態 | 入る契機 | 出る契機 |
|---|---|---|
| CREATED (`backlog_id=NULL, finished_at=NULL`) | Tx-0 (prepare) | Tx-1 で BOUND / 敗者・出力不正・timeout・worker 死で FINISHED |
| BOUND (`backlog_id` あり) | Tx-1 勝者 | Tx-2 で FINISHED |
| FINISHED (`finished_at` あり。`result ∈ {approval, report, NULL}`) | Tx-2 / 補償 tx / §3.6 の失敗終端 / 起動時回収 | 終端 |

**全ての終端経路 (pre-Tx-1 失敗・敗者・Tx-2 rollback 補償・timeout・worker 死・起動時回収) は同じ run を FINISHED にする** — dangling run (`finished_at IS NULL` で mission が終端済み) は作らない (pin)。

### 4.2 手順

0. **台帳の凍結**: `ctx.ledger` を `FROZEN` にする (これ以後の遅延 RPC 完了は拒否 — §3.4)
1. **出力検査** (tx 外): schema 検証 (runner 側でも済んでいるが親で再検証) / `selected.backlog_id` が実在する (**ヒント集合外なら activity `out_of_partition` を記録して続行** — 拒否しない、codex 7 周目 I3) / **`artifact.type=plugin` の分岐でのみ**: `artifact.name` が正規形 (§2.3)、`staging_dir/<name>` が dirfd + `lstat` で「通常ディレクトリ」、`plugins/<name>` が **{不存在 / プレーン dir / 正規形 symlink}** のどれか (§2.3、`resolve()` は使わない)。report / observation の artifact にはこれらの検査を掛けない (codex 7 周目 M2)。不合格 → Mission `failed`、staging 削除、台帳 `DISCARDED`、終わり
2. **バックログ追記 + 選択 + 所有 (Tx-1)**: `discoveries` を追加 (`source` = agent|research)。**正規化 (空白・大小文字) した `idea` の完全一致は重複として捨てる**。**上限 `improve.max_new_backlog_per_mission` (既定 20)** — 超過分は捨てて activity に件数を記録。続けて選択の CAS と run の `backlog_id` bind (§4.1)。敗者 → 敗者経路
3. **plugin ゲート (artifact.type=plugin、tx 外) — 候補は不変スナップショットとして扱う (codex 1 周目 C3)**。順に、どれか 1 つでも不合格なら**承認申請は出さず**、結果をレポートに残して backlog を `observation` へ:
   - a. **スナップショット検査**: `staging_dir/<name>/` 直下に**ちょうど 3 本の通常ファイル** (`plugin.py` / `config.yaml` / `test_plugin.py`)、サブディレクトリ・symlink・hardlink (`st_nlink==1`)・その他ファイル無し、各サイズ ≤ `_MAX_FILE_BYTES`。dirfd + `O_NOFOLLOW` で開く。`config.yaml` の `kind` 一致・`plugin/loader._validate_config` 相当
   - b. **2 つのハッシュを先に計算**: `content_hash` (H_before、既存定義 = `plugin.py` + `config.yaml`。**承認の実行時 identity**、定義不変) と **`artifact_hash`** (新設、`sha256(b"plugin.py\0"+p+b"\0config.yaml\0"+c+b"\0test_plugin.py\0"+t)` = 3 本全体。**版ディレクトリのキー**、codex 2 周目 I2)
   - c. `plugin/sandbox.check_source` を **`plugin.py` と `test_plugin.py` の両方**に (現行 `submit_plugin` と同じ)
   - d. **pytest を Landlock で囲った別プロセスで回す — 候補ディレクトリは read-only、起動は共通 launcher 経由**: 新ヘルパ `plugin/gate_pytest.py:run_gate_pytest(plugin_dir, *, settings) -> GateResult`。`Popen([sys.executable, "-c", "<launcher>", <expected_parent_pid>, <rlimit spec>, sys.executable, "-m", "agentic_fx.plugin.gate_pytest_worker", ...], cwd=<tmp workdir>, env=最小 + PYTHONPYCACHEPREFIX=<tmp>/pyc, start_new_session=True)` — **`preexec_fn` は使わない** (§1.1 の全体原則、codex 3 周目 I7)。launcher が rlimit (既存 `_pytest_rlimit_preexec` と同じ値) を設定して gate worker へ execve。**`PYTHONPYCACHEPREFIX` は interpreter 起動前に env で渡す** (codex 2 周目 M1。worker 冒頭で `sys.pycache_prefix` を assert)。gate worker は起動直後に **improve profile と同じ allowlist ファミリ** (`read_only` = code_root/venv/stdlib/`/usr/lib`/zoneinfo/`/etc` **+ 候補 plugin_dir (ro)**、`execute` = venv/base/`/usr/lib`/`/usr/lib64`、`read_write` = **tmp workdir + `/dev` のみ**) で `landlock.restrict_to` → `pytest.main(["-q", "-p", "no:logging", "-p", "no:cacheprovider", "--rootdir", <tmp>, str(plugin_dir)])`。timeout は `plugin.pytest_timeout_sec`。**Landlock 不可の環境では improve と同様 fail closed**。**`submit_plugin` / `bless` の `_default_pytest_runner` もこのヘルパに置き換える** (`plugin/approval.py:149` の無隔離実行を廃止)
   - e. **両ハッシュを再計算。H_after ≠ H_before または artifact_hash が変わっていれば不合格** (テストが自分の候補を書き換えた — 通常は ro で `EACCES` になるので**主 pin は「書込が EACCES・hash 不変・承認申請なし」**、hash 不一致は親側 fault injection の**副 pin** — codex 3 周目 M2)。以後の全工程は **この不変スナップショット** だけを入力にする
4. **戦略採用ゲート (kind=strategy のみ、tx 外)**: 親が `run_in_sample(..., record_fn=<sink>)` を各 pair で回す (worker の `run_backtest` 結果は使わない)。**評価可能性の閾値は既存 `backtest/metrics.py` の `EVALUABLE_MIN_TRADES` (=30) と既存の集計 (meta.pairs 合計)** — 新しい config key は作らない (codex 9 周目 I6)。**合計取引数 < 30 → 「評価不能」: 承認申請を出さず `observation` (悪いとは記録しない — R8)**。≥30 → `run_holdout_gate(..., record_fn=<sink>)` を回し、結果は **approval payload の添付のみ** (改善ループの読取ビューには載せない — 遮断 8)。**baseline の定義 (codex 8 周目 I8)**: (pair, timeframe) ごとに**現在 live で D4-approved の同名 strategy artifact** を同じ in-sample / holdout 期間で回した結果。**同名の承認済み strategy が無い (新規) ときは明示的な `no_strategy` baseline** (同期間・intent ゼロ → 取引 0) をその旨のラベル付きで記録する — **payload の baseline 行は決して null にしない**。**行の identity (codex 9 周目 I4)**: `backtest_runs` に `variant TEXT NOT NULL DEFAULT 'candidate' CHECK(variant IN ('candidate','baseline','no_strategy'))` と `ref_plugin_ref TEXT` / `ref_content_hash TEXT` (nullable、baseline が参照する候補) を `ensure_column` で追加。`no_strategy` 行は `plugin_ref='no_strategy:<name>'`, `content_hash=<候補の content_hash>`, `kind='strategy'`, `variant='no_strategy'` (実在 artifact を騙らずに NOT NULL を満たす)。**`latest_in_sample_metrics` は `variant='candidate'` に絞る** (挙動変更、pin)。payload は (pair, timeframe, variant) ごとに行 id を参照する。**submit / `submit|bless --from _human` の全経路に同じ規則**。`indicator` / `signal` はこのゲートを課さない (設計書 §6)。sink に積んだ行は Tx-2 で書く
5. **承認申請 (Tx-2 の一部)**: `approvals.create(kind="plugin", commit=False, payload={name, kind, staging_path, content_hash: H_before, artifact_hash, mission_id, backlog_id, in_sample: {...}, holdout: {...}|null, baseline: {...}|null, analysis_run_ids: <台帳→Tx-2 で解決した実 id>, trial_count: <台帳 entries の trial_count の総和 = 計算した相関値の個数>, analysis_call_count: <台帳の analyze_corr 呼出数>, backtest_call_count: <台帳の run_backtest 呼出数>, selection_rationale: <agent>, summary, audit_note: "RPC timeout した呼出しは数えていない"})`。**id・回数は agent 出力から取らない** (I7)。**`bless` は経由しない**
6. **レポート (親だけが書く)**: `artifact.type=report`、およびゲート不合格・評価不能・observation・敗者のとき、`reports/` に書く。**`proposal_kind=risk_gate` の report は本プランでは受理しない — observation `unsupported_in_plan10` にして report ファイルは書かない** (R12-(b)。親側の評価設計 (intent 単位 key・affected pairs/strategies・baseline/候補の in-sample + holdout 比較) は §A に保存し §10 に起票。本体設計書 §6「risk gate 提案の根拠にも同じ規律」を満たせないため、根拠なしの report を出さない側に倒す)。`core` / `research` の report は評価を要さない。
   **書き方 (codex 5 周目 I6 / 6 周目 I2 — 失敗 Mission の部分/孤児 report を残さず、DB が存在しない report を指さない)**: 本文は tx 外で **親専有の一時領域 `reports/.tmp/improve-<mission_id>.md.part`** に `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` (dirfd) で書き `fsync`。**Tx-2 は `improvement_runs.report_state='prepared'` + 予定の最終パス `report_path` を書く**。**最終名 `reports/improve-YYYY-MM-DD-<mission_id>.md` への `rename` (公開、`RENAME_NOREPLACE`) は COMMIT の後、最後の一手** → **`reports/.tmp` と `reports/` の両ディレクトリを fsync** → 成功で短い tx `report_state='published'`。公開失敗 (最終名が既に在る・I/O) → **短い補償 tx** で `report_state='failed'`、`result=NULL`、`report_path=NULL`、backlog **`done → observation` (`report_failed:<reason>`、§4.3 — この遷移は report_state の失敗からのみ許す)**、activity + 通知。**Tx-2 が rollback したら `.part` を削除**。**起動時 reconcile は §4.1 「report の公開状態」の状態表のとおり** (ここでは再掲しない — codex 9 周目 M3)。**eventual な不変条件 (reconcile 後): `published ⇔ 最終ファイルが存在`、遷移中の `prepared+最終` は許容**。`reports/` と `reports/.tmp/` は起動時に親が mkdir、gitignore 済み、**worker の rw には無い** (§2.1)。**agent の `body_md` は「提案本文」節に引用として入れる — 信用しない**
7. **`improvement_runs.finish` (Tx-2)**: 承認申請を出したら `result='approval'` + `approval_id` (+ レポートの一時ファイルが書けていれば `report_state='prepared'` + 予定 `report_path`)。承認申請なしで**レポートの一時ファイル作成に成功したときだけ** `result='report'` + `report_state='prepared'` + 予定 `report_path`。一時ファイル作成に失敗していたら `result=NULL`, `report_state='failed'`。**公開 (rename) が COMMIT 後に失敗したら補償 tx で `report_state='failed'`, `result=NULL`, `report_path=NULL`** (codex 2 周目 M2 / 5 周目 I6 / 6 周目 I2 — DB が存在しない report を指さない)
8. **backlog 遷移 (Tx-2、§4.3)** と `last_result` — `finish_improve_mission` の引数として渡す
9. **`finish_improve_mission(...)` を Tx-2 の末尾に置いて `COMMIT` → レポート公開 (rename) → 掃除**: 承認申請を出した staging は残す。それ以外は削除。台帳 `PERSISTED`。接続 close

**失敗の隔離**: commit 相の例外は improve レーンで握り、activity + 通知 (`improve_commit_failed`)。取引レーン・core_lock・資金保護には波及しない。missions 行・run・slot は必ず終端する (`finally` の補償 tx = `finish_improve_mission` — §4.1)。

### 4.3 バックログの状態機械 (codex 2 周目 I6 / 3 周目 I2・I3・M1)

status: `open | observation | selected | done | rejected` (`rejected` は人間が `backlog reject <id>` で閉じる用。`improvement_backlog.status` に CHECK は無い — `db.py:168-174` — ので migration 不要)。`attempts INTEGER NOT NULL DEFAULT 0` と `last_result TEXT` を `ensure_column` で追加 (冪等)。`list_open` = `open|observation`。**全遷移は決定と同じ transaction で `last_result` を更新する**。

| 現在 | 事象 | 次 | `last_result` |
|---|---|---|---|
| open / observation | Mission が選択 (Tx-1 CAS、`attempts+1`、run の `backlog_id` を bind) | selected | (変更なし) |
| selected | Tx-1 で敗者 (rowcount=0 — 遷移は起きない。敗者は自分の Mission の成果を観察に落とすだけ) | — | — |
| selected | レポートを書いて完了 (承認申請なし) | done | `report:<path>` |
| selected | **レポートの一時ファイル作成に失敗** (承認申請なし) | observation | `report_failed:<safe_reason>` |
| done | **公開 (rename) の失敗** — Tx-2 で `done` にした後の補償。**`improvement_runs.report_state` が `failed` に遷移するときだけ許す** (codex 6 周目 I2) | observation | `report_failed:<safe_reason>` |
| selected | ゲート不合格 / 評価不能 (<30、strategy 採用ゲート) / `proposal_kind=risk_gate` (本プラン未対応) / artifact=observation | observation | `gate_failed:<reason>` / `insufficient_trades:<n>` / `unsupported_in_plan10:risk_gate` / `observation:<reason>` |
| selected | 承認申請を発行 (pending) | selected (据え置き) | `approval_pending:<approval_id>` |
| selected | 承認 (approve、§5.1-7 の `apply_decision` と同じ tx) | done | `approved:<approval_id>` |
| selected | 却下 / 期限切れ / 失効 (approval 決定 tx で) | observation | `rejected:<reason>` / `expired` / `invalidated` |
| selected | Mission failed / timeout / Tx-2 失敗 | observation | `mission_failed:<status>` / `commit_failed` |
| selected | **プロセス crash 後の起動時回収** (`recover_interrupted` と同一 tx、run の `mission_id` 経由) | observation | `interrupted` |
| observation / open | 人間が `backlog reject <id>` | rejected | `human_rejected` |
| done / rejected | (終端。人間が `backlog reopen <id>` で open に戻せる) | open | `reopened` |

**approval の決定 API を 1 つにする (codex 3 周目 I3 / 4 周目 I5)**: `approvals.apply_decision(conn, approval_id, status, *, decided_by, now, reason=None)` — 1 transaction 内で ①approval 行の CAS (`UPDATE ... WHERE id=? AND status='pending'`、rowcount=0 → `AlreadyDecidedError`) ②`backlog.apply_approval_outcome` (payload の `backlog_id` を見て上表の遷移 + `last_result`) ③approve のときは §5.1 の手順 (版 → git → 切替 → 照合) を**先に**済ませてからこの API を呼ぶ (= decide が最後) ④approve のときは同 tx で switch ジャーナルを `decided` にする。**plugin approval の全 terminal decision (approve / reject / expire / invalidate / reconcile) は、DB CAS にも FS 副作用にも先立って同じ plugin `flock` (+ プロセス内 lock、§5.1-1) を取り、取得後に pending 状態・後発決定・**同名 plugin の未完 switch ジャーナル (§5.1 `plugin_switch_journal`、非終端 phase の行は name ごとに高々 1 件)** を再確認する — 未完があれば先に reconcile でそれを完了/巻き戻してから決定する (codex 5 周目 I2 / 6 周目 I4)。これで approve が切替まで進んだ瞬間に別 thread/process の reject が CAS に勝ち「DB は rejected・live は新版」になる経路を塞ぐ (rollback 操作は本プランに無い — R12)。`expire_due` は期限切れ行を 1 件ずつ列挙し、各行の plugin lock 下で同じ API を `status='expired'` で呼ぶ。シェル `approve/reject` / 起動時 reconcile / 将来 REST はすべてこの API を通る (直接 `decide` を呼ばない — pin)。

**変異 (§4)**: worker の `self_test="passed"` を信じて 3d を省く (→ 「worker が passed と言い、親ゲートで落ちる」テスト) / 3d を Landlock 無しで回す (→ ゲート子プロセスから `data/` を読む fake test が通ってしまう pin。killer = ゲート pytest 内で `data/agentic.db` を open して失敗することを assert) / 3d を `preexec_fn` で起動する (→ launcher 経由の argv pin) / **候補ディレクトリを rw で pytest に渡す (主 killer = `test_plugin.py` が `plugin.py` へ書こうとして `EACCES`・hash 不変・承認申請なし) / pytest 後に hash を取り直さない (副 killer = 親側 fault injection で hash を変え、承認申請が出ないこと)** / `PYTHONPYCACHEPREFIX` を起動後に設定する (→ `sys.pycache_prefix` assert) / スナップショット検査を落とす (→ symlink を置いた候補が通る pin) / `name` の正規形検査を落とす (→ `../../docs/examples/plugins/rsi_indicator` が弾かれる pin) / **partition 印 / ヒントを全件 assigned にする、または省略する (→ 負荷分散情報の欠落 = `out_of_partition` activity と担当印の pin。拒否はしない)** / **切替先を `resolve()` で検査する (→ 承認済み symlink plugin の再改善が failed になる pin)** / 選択 UPDATE を無条件にする (→ 2 接続同時選択で二重承認申請) / **Tx-0 の run INSERT を落とす (killer = Mission だけが生まれ run が無い状態が作れない — 出力不正で failed になった Mission にも run が在ること)** / **Tx-1 の `backlog_id` bind を落とす (killer = Tx-1 直後にプロセスを落とし、起動後に backlog が `observation:interrupted` になること)** / **Tx-1 を pytest 越しに保持する (killer = 2 接続同時 writer: ゲート中に取引レーンの `INSERT` が `busy_timeout` 内に通ること)** / **接続を slot 間で共有する (→ N=4 同時 Tx で例外/混線が無い pin)** / `save_harness_run` の内部 commit 版を親ゲートで呼ぶ (→ holdout 失敗後に in-sample 行だけ残る pin) / 30 未満を `rejected` にする (→ observation pin) / holdout 結果を Mission 出力や次回注入に含める (→ 遮断 8 pin) / **agent 申告の analysis id / trial_count を payload に載せる (→ 偽装テスト: 台帳 100 件・申告 1 件で payload が 100)** / **`trial_count` を呼出回数にする (→ lead-lag 1 呼出しで 25 になる pin)** / 台帳を凍結せずに読む (→ 遅延 RPC 注入で保存漏れ・追記競合) / バックログ上限を外す (→ 21 件投入テスト) / **report 失敗でも `result='report'` を書く (→ dangling path pin) / report 失敗で backlog が `selected` のまま (→ `report_failed` pin) / 公開 (rename) を Tx-2 より前に行う・`report_state` を書かない (killer = COMMIT 直後にプロセスを落とし、起動後に `published ⇔ 最終ファイル存在` が成立し `.part` が残らないこと) / `proposal_kind=risk_gate` を report として書く (→ `unsupported_in_plan10` observation・report ファイル無し pin) / slot を `ready` 前に `running` にする (killer = spawn 失敗後に slot が `reserved` に戻り同 wave で再 claim されること) / `go` を待たずに Mission を始める (killer = `running` commit 前に親を落とし、worker が副作用ゼロで終了すること) / 終端を slot・mission・run で別 tx にする (killer = 間で落として不一致が残らないこと)** / **approve/reject 後に backlog が `selected` のまま / `decide` を直接呼ぶ (→ `apply_decision` 経由 pin)** / **`missions.finish` を Tx-2 の外に置く (killer = Tx-2 commit 直後にプロセスを落とし、起動後に approval が pending のまま・backlog が `selected`・mission が finished で一致していること)** / **dispatcher が slot の write 接続を使う (→ `check_same_thread` 例外 or N=4 混線 pin)** / **source snapshot を live symlink から読む (killer = コピー中に symlink を切替え、3 本が単一版由来で hash が一致すること)** / **サンプル plugin を repo の `docs/` から直接読ませる (→ Landlock で EACCES、`source/_examples/` から読める pin)** / `bless` を呼ぶ (→ 承認申請が `pending` であることの pin) / レポートを `open(..., "w")` で書く (→ 事前に symlink を置いた fake で fail closed になる pin) / commit 相の例外で missions 行が終端しない (→ CAS finalize pin) / staging を削除しない (→ 掃除テスト)。

---

## 5. 承認時の履歴記録と昇格 (D6 の再収束) — 候補 → ゲート → pending 行 + 証跡 + ジャーナル → 版 + git → 切替 → 決定

**R12 で縮小した後の範囲**: 本節が扱うのは **改善ループの候補と人間の候補 (`_human`) が同じライフサイクルで承認され、不変の版ストアに保管され、live symlink を原子的に切り替えられ、bare git に履歴が残る**ことまで。**旧版へ戻す操作 (`plugin rollback` / `plugin bless-version`)・既存プレーン版の adopt・`plugin_versions` provenance 表・live の in-place 編集は本プランに無い** (§A に設計テキストを保存、§10 に起票)。戻したいときは人間が `materialize` → 編集 → `bless --from _human` で**新しい承認**を作る。

### 5.1 承認手順 (人間が `approve <id>` した瞬間。シェル / 起動時 reconcile / 将来の REST から。**scheduler スレッドでは決して実行しない**)

**順序の原則: 履歴への記録が先、本番への切替は最後に原子操作 1 回、DB 決定はさらにその後。** git がどう失敗しても稼働中の版は消えず、「live が無い瞬間」が存在しない。

**レイアウト**:
- 承認済み内容は **版ディレクトリ `plugins/.versions/<name>/<artifact_hash>/`** (3 本全体の hash がキー — 同じ code/config で test だけ違う版も区別する、codex 2 周目 I2)。approval の実行時 identity は従来どおり `content_hash` (2 本) で、payload は両方を持つ。**版ストアは不変** (親が 0500/0400 で作る。人間の編集場所ではない — §2.3)。**本プランで版ディレクトリを作るのは「本プランのライフサイクルで approved になった候補」だけ** — 既存のプレーン plugin を版化 (adopt) しない
- 稼働中の `plugins/<name>` は**版ディレクトリを指す相対 symlink** (`.versions/<name>/<artifact_hash>`)。**discovery は symlink を辿った先の版ディレクトリを `PluginMeta.path` に固定し、稼働中サービスは再起動まで同じ版を使い続ける** (§2.3、codex 3 周目 I4) — 切替は次回起動/再読込にだけ効く
- **既存のプレーンな `plugins/<name>/` (手作り、本プラン以前)** は自動移行しない。discover は両形 (プレーン / symlink) を読み、両者は共存できる。**同名の候補が初めて承認されたとき**、プレーン dir は `RENAME_EXCHANGE` で symlink と入れ替わり、退いたプレーン dir は **`plugins/_retired/<name>-<UTC タイムスタンプ>/` へ rename** される (人間所有。自動では削除しない。discover は `_` 先頭を列挙しない)。**旧版の版化・履歴記録は行わない** — 人間が必要なら `_retired` から `_human` へコピーして `bless --from _human` で新規承認する
- 履歴は **bare リポジトリ `plugins/.history.git`** (`git init --bare` を lazy に。**ワークツリーを持たない** — codex 2 周目 I9)。人間の閲覧は `git --git-dir=plugins/.history.git log|show`。**記録されるのは approved になった artifact だけ**
- `plugin/loader.discover` は先頭 `.`/`_` を除外 (§2.3) するので `.versions/`・`.locks/`・`.history.git/`・`_staging/`・`_human/`・`_retired/` は列挙されない

**候補の入口は 2 つ、ライフサイクルは 1 つ (codex 9 周目 I1)**: ①改善ループの候補 (`plugins/_staging/<mission_id>/<name>/`、§4 の commit 相がゲートを通し pending 承認申請を作る) ②人間の候補 (`plugins/_human/<name>/`、`afx plugin submit --from _human <name>` がゲートを通し pending を作る / `afx plugin bless --from _human <name>` = submit + 直後に人間決定 `approved`)。**どちらも「候補 → kind 別ゲート (§4.2-3・§4.2-4) → 1 つの短い tx で pending approval 行 + ゲート証跡行 (`backtest_runs`) → 決定 (承認) → 版 → git → 切替 → `apply_decision`」**を通る。live path (`plugins/<name>`) は**決して編集対象にしない** (プレーンでも symlink でも)。人間が承認済み plugin を改良するときは `afx plugin materialize <name>` (live の版ディレクトリまたはプレーン dir から `plugins/_human/<name>/` へコピー。既に在れば拒否。ディレクトリ 0700 / ファイル 0600。**自動削除しない** — 人間が消す) → 編集 → `submit|bless --from _human`。

1. **排他 + switch ジャーナルの確認**: **`flock` を `plugins/.locks/<name>.lock` に取得** (プロセス間 — サービスの approve / **reject / expire / invalidate** / 起動時 reconcile / 別プロセスの `afx plugin bless` が**全て**同じ lock を取る、codex 4 周目 I5) + プロセス内は plugin 名ごとの `threading.Lock` (ⓒ、順序は flock → thread lock)。lock 取得後に approval が `pending` であること・後発決定・**同名 plugin に未完の switch ジャーナルが無いこと**を再確認してから FS 副作用に進む (未完があれば先に reconcile — 下記)。異なる plugin の並行承認は 5.2 の CAS が守る
   **switch ジャーナル `plugin_switch_journal` (codex 5 周目 I2 / 6 周目 I3・I4 / 7 周目 I4・I5 — `flock` は crash を越えない)**: 新テーブル `plugin_switch_journal(op_id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('approve','bless')), approval_id INTEGER NOT NULL, name TEXT NOT NULL, old_kind TEXT NOT NULL CHECK(old_kind IN ('absent','symlink','plain')), old_target TEXT, retired_path TEXT, temp_path TEXT NOT NULL, new_target TEXT NOT NULL, switch_required INTEGER NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('preparing','versioned','recorded','switched','decided','reverted')), actor TEXT NOT NULL, updated_at TEXT NOT NULL)`。**行の INSERT が `op_id` を先に割り当てる (`phase='preparing'`、FS 効果ゼロの時点、codex 10 周目 I1)。`temp_path = plugins/.<name>.link-<op_id>`、`retired_path = plugins/_retired/<name>-<ts>` (old_kind=plain のときだけ) を最初の行に確定して書く** (codex 8 周目 I5)。**`approval_id` は常に既知** (approve は pending 行、bless は同 tx で作った pending 行 — 9 周目 I1)。**`switch_required` は INSERT 時に確定** (旧の正規 target == 新の正規 target なら 0 → FS 切替を省略し `recorded` の後そのまま決定へ。`switched` の復旧規則は `switch_required=1` の行にだけ適用 — codex 8 周目 I6) + **部分 UNIQUE index: 非終端 phase (`decided`/`reverted` 以外) の行は `name` ごとに高々 1 件**。**旧状態 (kind / target) は行の INSERT 前に確定する**。以後 **各 phase の書込は次の FS 効果より先、lock 下の短い tx**:

   | 操作 | 行の INSERT (`preparing`) 前に済ませること | phase 列 | 終端 |
   |---|---|---|---|
   | approve (改善ループ / `submit --from _human` の pending) | 旧状態の確定 (`plugins/<name>` が absent / symlink / plain のどれか、symlink なら target 文字列) | preparing → versioned (新版作成) → recorded (git) → switched (切替直前) → decided | `apply_decision(approved)` と同一 tx |
   | bless (`--from _human`) | 旧状態の確定 + **kind 別の全ゲート (§4.2-3・§4.2-4)** + **pending approval 行 + ゲート行 + ジャーナル行 (`preparing`) を同一 tx (FS 効果前)** | 同上 | `apply_decision(approved)` (approve と同一経路)、同一 tx |

   **収束規則**: ジャーナルが終端 ⇔ live symlink と DB (approval 決定) が一致。**`switched` で止まっていた行の復旧規則 (`switch_required=1` のみ)**: `live == new_target` → 完遂 (decide へ) / `live == old 状態` → 取消 (`reverted`) / どちらでもない → activity ERROR で人間待ち (触らない)。未完ジャーナルの扱い (同名の**全**操作 — approve/reject/expire/invalidate/bless — は lock 下でまずこれを収束させる): (a) **同じ操作の再試行 / 起動時 reconcile** は phase から再開して完了させる (hash がまだ一致し後発 reject が無ければ)。**再開前に参照物 (`new_target` の版・`old_target` の版・`retired_path`・temp link) の存在と hash を再検証**し、新版が欠損なら**保持している staging / `_human` 候補から再作成**、無理なら `reverted` で閉じて activity ERROR (I4) (b) **それ以外の操作が来た**とき (reject/expire/invalidate/別 hash の approve) は、reconcile が**切替を巻き戻す**: `old_kind='symlink'` → symlink を `old_target` へ 1 rename で戻す / **`old_kind='plain'` → `retired_path` のプレーン dir と temp symlink を再度 `RENAME_EXCHANGE` で入れ替えて戻す** (`retired_path` が無ければ ERROR で人間待ち) / `old_kind='absent'` → live symlink を除去 → `reverted` (activity `switch_reverted`) → その後に本来の操作を適用する。**`GC_ROOTS` (唯一の定義、他節はこの名で参照)** = **approved な approval 行の payload が指す `artifact_hash` の版** ∪ **live symlink の指す先** ∪ **非終端ジャーナルが参照する `new_target`・`old_target` の版・`temp_path`・`retired_path`** (codex 7 周目 I4 / 8 周目 I4)。**この集合に無い版ディレクトリは孤児として起動時 reconcile が削除する** (簡素な規則。pending の approval が指す staging は §2.3 の規則で残る)
2. **後発決定の確認 (ⓓ) — key は `(name, content_hash)`** (D4 と同じ。codex 4 周目 I9): **同じ `(name, content_hash)`** へのより新しい決定 (reject) があれば、この承認は失効 — `apply_decision(status='invalidated')` (§4.3。backlog も同 tx で `observation`)。**同名で content_hash が異なる approval 同士は独立** (C の reject は B の pending approve を失効させない)。plugin 名全体を revoke する操作は設けない (YAGNI)。再試行で復活させない (D4 の順序規則)
3. **ハッシュ再照合 (ⓐ)**: 候補 (staging / `_human`) を §4.2-3a と同じスナップショット検査に通し `content_hash` と `artifact_hash` を再計算、payload と一致しなければ承認は成立せず **pending のまま** + activity + 通知
4. **版ディレクトリの作成 (冪等)** (ジャーナル行 `preparing` を INSERT してから。完了後に `versioned`): `plugins/.versions/<name>/<artifact_hash>.tmp-<op_id>/` に 3 ファイルをコピー → 各ファイルとディレクトリを `fsync` → 0400/0500 に落として `rename` で `plugins/.versions/<name>/<artifact_hash>/` へ (既に同 artifact_hash の版があれば作らない)。失敗 → pending + 通知。**本番 symlink には触れていない**
5. **履歴への記録 (ⓑ) — 新版ディレクトリの 3 本から、`<name>/<file>` のパスで** (完了後に switch ジャーナル `recorded`): 下記 5.2 の blob-level plumbing。失敗 (git 不在 / detached / identity 無し / CAS 上限) → **pending のまま + 通知。本番には一切触れていない**
6. **切替 (原子操作 1 回、`switch_required=1` のときだけ)** (直前に switch ジャーナル `switched` を短い tx で書く — FS 効果より先): temp symlink `temp_path` = `plugins/.<name>.link-<op_id>` (→ `.versions/<name>/<artifact_hash>`) を作り、`plugins/<name>` の現在の形で分岐:
   - **不存在 / symlink**: `os.rename(temp, plugins/<name>)` (symlink → symlink の rename は原子的)
   - **プレーンなディレクトリ (本プラン以前の手作り plugin)**: `renameat2(AT_FDCWD, temp, AT_FDCWD, plugins/<name>, RENAME_EXCHANGE)` (ctypes syscall — `core/landlock.py` と同じ流儀。Linux 3.15+、同一 FS) で **temp symlink と live ディレクトリを 1 回で交換**する。交換後、temp パスに来た旧ディレクトリを **`retired_path` (`plugins/_retired/<name>-<ts>/`、ジャーナルに確定済み) へ `rename`** する (人間所有。自動削除しない)。**`RENAME_EXCHANGE` 非対応 (kernel/FS) は初回切替を fail closed** — その plugin の approve/bless は pending (理由 `exchange_unsupported`) に留める
   - 切替後 `content_hash(plugins/<name>)` (symlink を辿る) を再計算し payload と一致することを確認 (不一致 → 旧状態へ戻し pending)
7. **`apply_decision(status="approved")`** (§4.3 — approval 行 CAS + backlog `done` + **switch ジャーナル `decided`** を 1 tx で。決定は最後)。ここで初めて次回起動の `approved_plugins()` に載る
8. staging (改善ループの候補) を削除、lock 解放。`_human` 候補は削除しない (人間所有)

**却下・期限切れ・失効**: 同じ plugin `flock` 下で、同名の未完 switch ジャーナルがあれば先に巻き戻し (1 の (b)) → `apply_decision` (backlog を `observation`) + staging 削除。ジャーナルが無ければ `plugins/<name>`・版・履歴には触れない。

**bless (`afx plugin bless --from _human <name>`) — approve と同じライフサイクル: 候補 → ゲート → pending 行 + 証跡 → ジャーナル付き昇格 → 決定** (codex 9 周目 I1): 同じ `flock` を取り、同名の未完ジャーナルを収束 → 候補 `plugins/_human/<name>/` をスナップショット検査 → 両 hash → **kind 別の全ゲートを通常 submit と同じ経路で通す** (§4.2-3 のコードゲート = AST + Landlock pytest + hash 不変、**kind=strategy なら §4.2-4 の in-sample ≥ 30・固定 holdout・baseline 添付まで**。どれか欠ければ何も作らない — codex 7 周目 I7) → **FS 効果より前に 1 つの短い tx で**: pending の approval 行 (`kind=plugin`、payload はゲート行 id・baseline/holdout・settings hash を含む完全形、`candidate_path=_human/<name>`) + ゲートの `backtest_runs` 行 + switch ジャーナル行 (`preparing`、`approval_id` は**この時点で既知**) を作る → 版ディレクトリへコピー (4) → 履歴記録 (5) → 切替 (6。プレーンなら `RENAME_EXCHANGE`、symlink なら rename) → 照合 → **その pending 行に `apply_decision(approved, decided_by=human_cli)`** (7、approve と同一経路)。**live path や symlink 版を候補に取る bless は無い** (`bless <name>` は `plugins/<name>` がプレーン dir でも拒否し、`materialize` を案内する — live は編集対象外)。


### 5.2 記録手順 — bare リポジトリ + 専用 index + blob-level plumbing (プラン 9 D6 の確定形を、bare + 版ディレクトリ出所に合わせて組み替え)

- **初期化**: `plugins/.history.git` が無ければ親が `git init --bare` (lazy)。親リポジトリとは独立、gitlink は発生しない (`.gitignore` の `/plugins/` で親から見えない)。**ワークツリーは無い**ので `git add` の対象も `git status` の対象も存在しない
- **ポーセリン (`git add` / `git commit`) は使わない** — bare にはワークツリーが無く、そもそも `git commit -- <path>` はワークツリー内容を取り直すので検証後の書き換えを拾う (codex 4 周目 C1 / 3 周目 M1)。**blob を版ディレクトリのファイルから直接作り、専用 index に `<name>/<file>` のパス名で置く**

```
GIT_DIR=plugins/.history.git  (全コマンド共通。GIT_WORK_TREE は設定しない)
src  = plugins/.versions/<name>/<artifact_hash>/
ref  = git symbolic-ref HEAD        # unborn でも HEAD が指す ref 名は返る。init.defaultBranch に依存しない
old  = git rev-parse --verify <ref> # 失敗 = unborn (初回)
b_py = git hash-object -w <src>/plugin.py ; b_cfg = … config.yaml ; b_test = … test_plugin.py   # 3 blob を object DB へ
GIT_INDEX_FILE=<tmp> git read-tree <old>                # unborn なら read-tree --empty
GIT_INDEX_FILE=<tmp> git rm --cached -r -q --ignore-unmatch -- <name>/     # 旧版の同 prefix エントリを専用 index から外す
GIT_INDEX_FILE=<tmp> git update-index --add --cacheinfo 100644,<b_py>,<name>/plugin.py   (config.yaml / test_plugin.py も同様)
GIT_INDEX_FILE=<tmp> git cat-file blob :<name>/plugin.py / :<name>/config.yaml / :<name>/test_plugin.py → bytes → content_hash_bytes() / artifact_hash_bytes()   # 検証は index の blob に対して。payload と一致しなければ中止 (pending)
GIT_INDEX_FILE=<tmp> git write-tree     # → tree。unborn でなく tree == <old>^{tree} なら「変化なし」= commit を作らず成功
git commit-tree <tree> [-p <old>] -m "approve <name> content=<content_hash> artifact=<artifact_hash> (approval #id)"   # unborn なら -p なし
git update-ref <ref> <new> <old>        # CAS。unborn は <old> = 空文字。失敗したら頭からやり直し (有限回、超過は pending のまま)
```

- **`_staging/` `.versions/` を stage する経路が構造的に無い** (index に入るのは `update-index --cacheinfo` で明示した `<name>/<file>` の 3 本だけ)
- **`symbolic-ref` の失敗は終了コードで区別**: 1 = detached HEAD (人間が履歴操作中 → 待てば直る) / 128 = リポジトリ障害 (運用者の対処が要る)。どちらも pending だが activity と通知の理由を分ける
- **CAS 無しの `update-ref` は不可**。代替としてリポジトリ単位 lock で全 git 操作を直列化してもよい (実装はどちらでも)
- **hash の bytes 版**: `plugin/loader.content_hash(Path)` を `content_hash_bytes(plugin_py, config_yaml)` の薄いラッパにする (定義 `sha256(b"plugin.py\0"+p+b"\0config.yaml\0"+c)` は**不変**)。`artifact_hash_bytes(plugin_py, config_yaml, test_plugin)` を新設 (§4.2-3b)
- **commit identity はサービスが供給**: `GIT_AUTHOR_NAME/EMAIL` / `GIT_COMMITTER_NAME/EMAIL` = `agentic-fx <noreply@localhost>` を env で渡す (利用者の git 設定に依存させない。無いと `commit-tree` が失敗し永久 pending — codex 6 周目 I2)
- git サブプロセスの env は最小 (`PATH`, `HOME` は実ホームでよい — 親プロセス、Landlock 外)。`GIT_DIR` を明示し `GIT_WORK_TREE` を設定しない — 親リポジトリの `.git` を誤って触らない

### 5.3 git と SQLite は原子化できない — 収束性で担保 (codex I5)

- 「approved になっている plugin は必ず commit 済み」の**一方向**だけを不変条件にする。逆は保証しない
- **git の失敗・版作成の失敗は稼働中の版を決して壊さない** (どちらも切替に先行するため)。**切替は原子操作 1 回** (rename または exchange) なので「live が無い瞬間」は無い。起こりうる中間状態は次の 3 つで、いずれも pending に統一され、再試行で収束する:
  - **(a) 版ディレクトリ作成済み・未記録・pending**: 旧版無傷。再試行は同 artifact_hash の版が在るので作り直さず、git から続く。`.tmp-*` の残骸は reconcile が削除
  - **(b) 記録済み・未切替・pending**: 旧版無傷。再試行は git が tree 一致で no-op、切替をやり直す
  - **(c) 切替済み・pending** (`decide` の DB 書込だけ失敗 — 窓は極小): live は新版を指すが approved でないため**新版は load されず、旧版も load されない**。起動時 reconcile (`approved_plugins()` より前) と次の承認操作が拾って decide を完了させる。**人間が待てなければ、未完ジャーナルの reconcile が `switched` の復旧規則で旧状態へ revert する経路 (§5.1-1 (b)) を使う**
- **再試行の 3 契機**: ①起動時 reconcile ②次の承認操作 ③シェル `approval retry <id>`。**scheduler tick にぶら下げない**。再試行は手順を頭から流す (lock → ⓓ → ⓐ → 版 (冪等) → git (tree 一致なら no-op) → 切替 (既に新版を指していれば no-op) → `apply_decision`)
- **起動時 reconcile の位置**: `init_db` と中断 Mission の回収より後、**`approved_plugins()` より前** (後だとその起動では復旧した承認がロードされない — codex 4 周目 I3)。**順序は「ジャーナルの収束が先、孤児掃除が後」** (codex 7 周目 I4 — 再開材料を先に消さない): まず ⓪**未完の `plugin_switch_journal` (非終端 phase) を phase から完了または旧状態へ巻き戻して終端する** (5.1-1 の規則。参照物の存在と hash を再検証してから) → その後に ①§2.3 の孤児 staging 掃除 (未完ジャーナルが参照する staging は残す) ②`.versions/<name>/*.tmp-*` の削除 ③**`GC_ROOTS` (§5.1) に含まれない**版ディレクトリの削除 (orphan) ④**temp link `.<name>.link-*` の残骸削除 (`GC_ROOTS` の `temp_path` は除く)** ⑤`plugins/<name>` が dangling symlink なら activity ERROR (人間の操作を待つ。自動では向け直さない) ⑥`_retired/` `_human/` には触れない (人間所有) を行う。git 不在・reconcile 失敗はサービス起動を止めない (警告 + pending のまま)
- **git 不在**: 起動時検査で警告 (SQLite ≥3.35 assert と同じ扱い)。plugin 承認だけが成立せず、取引は動く


### 5.4 資金保護への非波及

承認スレッド (シェル / 将来 API) と起動シーケンスでのみ実行。**scheduler スレッドから git サブプロセスが呼ばれないこと**を回帰テストで固定 (呼ばれたら fail するシーム — D3' の通知と同じ形。「lock 内か」でなく「どのスレッドか」で検査)。`core_lock` は触らない。

**変異 (§5)**: 決定時ハッシュ再照合を削除 / 照合対象を index の blob から候補置き場のワークツリーへ戻す (TOCTOU 復活) / 専用 index をやめ `git add` + `git commit -- <path>` (**killer = 検証後に staging の plugin.py を書き換えてから commit させ、commit 内容が検証済みの内容であることを assert**) / **git 記録より先に切替する (killer = git 失敗を注入 (identity env 除去 / detached HEAD) して承認させ、`plugins/<name>` が旧版を指したまま `approved_plugins()` に残ることを assert)** / **プレーン初回切替を「旧を退避 → 新を置く」の 2 段 rename にする (killer = 1 段目の直後にプロセスを落とし、起動時に live が欠けている)** / **`RENAME_EXCHANGE` 非対応で 2 段にフォールバックする (→ fail closed pin)** / **退いたプレーン dir を削除する・`_retired` に置かない (killer = 初回切替後に `plugins/_retired/<name>-*/` に旧内容が残ること)** / **版キーを `content_hash` にする (killer = code/config 同一・test 違いの 2 候補を順に承認し、履歴に 2 つの test が別 commit で残り、版ストアに 2 版が在ること)** / **`flock` を落とす (killer = 別プロセスの bless と同時に走らせ、両者が互いの版を壊さないこと)** / **履歴を非 bare にしてワークツリーを持つ (killer = 承認後に `git status` が clean であること / `git restore .` 相当の操作で live symlink が実 dir に置換されないこと)** / 切替後の hash 再検証を落とす (killer = swap 中に版ディレクトリを差し替え、旧状態へ戻され pending のままであること) / 切替先を `resolve()` で検査する (§4 と共有) / `_staging/` `.versions/` が index に入る (→ 記録後の tree に無い pin) / 旧版の消えたファイルが index に残る (→ `git rm --cached` 落とし) / 空 commit 判定を `git diff --cached` に戻す / commit と decide の順序を入れ替える / commit 失敗で承認を成立させる / 後発 reject の確認を削除 / 起動時 reconcile を `approved_plugins()` の後に置く / reconcile が dangling symlink を勝手に向け直す (→ ERROR に留める pin) / git を scheduler スレッドから呼ぶ / CAS 無し update-ref (→ 並行承認で先発 commit が消える) / identity env を落とす (→ 空 git config 環境で永久 pending) / `fsync` を落とす (→ 実装計画の fault-injection、設計では要求のみ) / bless が Landlock ヘルパでなく `_default_pytest_runner` を使う (→ §4.2-3d の pin) / discover が symlink を辿らない (→ 承認直後に plugin が消える pin) / approve 時に backlog を `done` にしない (→ §4.3 pin) / **ジャーナル行を FS 効果の後に INSERT する・`op_id` を temp 名より後に採番する (killer = `preparing` 行が版作成より先に在り、`temp_path` がその `op_id` から導出されていること)** / **`switched` の復旧を「常に完遂」にする (killer = live が old のまま落ちた行が `reverted` で閉じ、live が変わらないこと)** / **ジャーナル参照物を `GC_ROOTS` に含めない・掃除をジャーナル収束より先に行う (killer = `versioned` で落として起動後に new 版が消えず再開できること)** / **bless が strategy ゲートを飛ばす (killer = 取引 10 件の strategy 候補が `bless --from _human` で承認されないこと)** / **reject/expire が plugin flock を取らない (killer = approve の切替直後に reject を差し込み、DB rejected と live 新版が食い違わないこと = reject が lock 待ちで approve 完了後に `AlreadyDecidedError` になる)** / **後発決定 key を name だけにする (killer = 同名別 hash の C を reject しても B の pending approve が生きていること)** / **switch ジャーナルを書かずに切替する (killer = 切替直後にプロセスを落とし、別プロセスの reject を先に走らせても、起動後に live が旧状態に戻り DB が rejected で一致すること)** / **`old_kind` を持たず NULL で plain と absent を混同する (killer = plain 初回切替の巻き戻しで `retired_path` の dir が exchange で戻り、absent では live が除去されること)** / **同名の未完ジャーナルを 2 件許す (killer = 別 hash の approve が先行 approve の切替を追い越さないこと)** / **bless が live path (`plugins/<name>`) を候補に取る (killer = プレーン live への `bless <name>` が拒否され `materialize` を案内すること)** / **materialize が既存 `_human/<name>` を上書きする (→ 拒否 pin)** / **版ストアのファイルを 0600 のまま置く (→ 0400/0500 pin、in-place 編集の discover 拒否と組)** / discovery が `PluginMeta.path` を symlink のまま持つ (→ approve 後に稼働中サービスの sandbox が旧版を実行し続ける pin)。


## 6. 外向きリクエストの予算 — 研究ツールの advisory 予算 (安全保証ではない)

**位置づけ (ユーザー裁定 2026-08-16、codex 1 周目 I5 を受けて)**: 本節の予算は **registry ツール (`web_search` / `fetch_article`) の契約**であり、**改善 worker からの外向き通信全体を律する安全保証ではない**。改善 profile は shell と python を持つ (§1.6・§2.1-5) ので、agent が `python -c` から直接 HTTP を投げれば予算は数えられない。これを塞ぐには network namespace か親の egress proxy への限定が要り、それは**外向きリクエスト予算の全体設計 (起票 §10)** の一部である。本プランでは:

- **prompt で禁じる** (§3.2 規律節): 「ネットワークアクセスはツール経由のみ。shell/python から直接 HTTP を投げない」。従わない agent を機構では止められないことを明記する
- **registry ツールの Mission 予算** (`improve.research` config、`_Strict`): `max_searches` (既定 20) / `max_fetches` (既定 30) / `min_interval_sec` (既定 2.0) / `max_per_host` (既定 5) / `fetch_max_bytes` (既定 2 MiB) / `user_agent` (既定 `agentic-fx/<version> (+https://github.com/<repo>)` — 素性を名乗る)。予算は worker 内のツール実装が数える (Mission = プロセス)。使い切ったらツールは `{"error": "budget exhausted"}` を返し、Mission は続く。**429 / 503 を受けたホストは同一 Mission 内で以後打ち切り** (再試行しない)。既定は未設定でも安全側で動く
- **CLI 自身の egress**: codex は `--disable plugins --disable remote_plugin --disable recommended_plugins` を argv pin (既知の 3 経路の退行防止)。`--disable apps` の効果は Task 13 で実測・記録 (§1.3、M2)。claude は `--setting-sources ""`。CLI が LLM エンドポイント以外へ出る通信をゼロにできる保証は無い — 観測して文書化する
- **ネットワーク遮断はしない** (プラン 8 §4.5 の裁定どおり。改善 worker は LLM と研究に外へ出る必要がある)

**変異 (§6 — ツール契約テストとして)**: 予算カウンタを落とす (→ 21 回目の検索が通る) / 429 で再試行する (→ fake サーバで 2 回目のリクエストが飛ぶ) / UA を空にする / `--disable plugins` を落とす (§1 の pin と共有)。**これらは安全保証の検証ではない**。

---

## 7. 受入条件

### 7.1 blocking (1〜7) — 全部が緑になるまで改善ループは有効化しない

`schedule.improve` の消費と `improve` コマンドの配線は Task 12 で行い、**Task 12 の gate は本節 1〜7 の逐語**である (8 は含まない)。

1. **遮断 8 項目の統合回帰** (`staging_dir` / `source_snapshot_dir` は handshake から導かれる — root/DB は渡さない。worker は `plugins/` 本体を読めない) (改善 worker の**実プロセス**に対して。**improve registry の task 直後に red で書き始める** — 最後の E2E に置かない): ①`data/agentic.db` の絶対パス open / `data/` 列挙が失敗 ②`run_holdout_gate` を呼んでもデータ到達不能で失敗 ③`ohlcv_history` / `ohlcv_cache` を直読するツールが registry に無い + DB パスが handshake に無い ④**書き込み可能パスが staging・workdir・`/dev` に閉じる** (`reports/`・リポジトリ本体・`plugins/<name>`・`plugins/.versions`・`plugins/_human`・`plugins/_retired`・`config/`・`policy/` への write が `EACCES`) ⑤plugin サンドボックスの入力 DataFrame はハーネスが与える (既存 pin 継続) ⑥`get_signals` を含む `IMPROVE_FORBIDDEN` + 取引 registry の全ツールが improve registry に**無い** ⑦`analyze_corr` / `run_backtest` の返却 schema に日時・期間端点・順序付き窓列・観測数が無い、**かつ RPC が DB に直接書かない** ⑧approval の結果として holdout の指標・baseline 差分・閾値別合否が Mission 出力・注入コンテキスト・RPC 返却のどこにも現れない、**かつ payload の analysis id / 回数は agent 出力からでなく RPC 台帳から来る**
2. **3 実装 (Local / Claude / Codex) が同一の契約テストスイートに合格** (fake CLI スクリプトで実 LLM を呼ばずに回す。運用で選ばれるのは 1 つ): 4 終端 / reason 安全化 / timeout 優先 (fake が sleep) → CLI セッションが killpg され worker 自身は生きて `timeout` を返す / schema 不適合 → failed / **子 env に鍵の名前がゼロ・scratch home のみ** / claude の allowedTools が profile で固定 / codex は trade で拒否 / `max_turns` の runner 別セマンティクス / **exec closure の 1 要素 drop pin** (shell 系 backend で `/usr/bin` を落とすと fake shell 起動が `EACCES`) / **子の argv が絶対パスのみ・codex に node ラッパを指すと起動拒否** / **全 spawn の env に `*_API_KEY` が無い (trade 資格情報は handshake)** / **サービス初期 env に秘密名があると improve+claude が起動拒否** / **launcher の expected-parent 再照合 + `cli_started` pgid で worker 異常死時に CLI が回収される** / **`improve.mission_max_turns/timeout_sec` の写像** (Codex は turns 無視) / **claude init の MCP server 集合が profile の `afx` ちょうど 1 つ・未知 0** / **サブスク認証コピーが agent から可読であることは受容 (R11、fail closed にしない) — 契約テストは「課金鍵が env に無い」に留める** / **codex+llama_swap は auth.json をコピーせず空の scratch `CODEX_HOME` で起動する** (fake pin、全体 blocking) + **`improve.llama_swap_verified` が false のとき通常入口は `provider=llama_swap` を起動拒否** (他 backend は影響なし。実ターンの実測は §7.2 の検証専用入口 `afx improve verify-backend`)
3. **ゲート pytest が柵の中で動き、候補は不変**: ゲート子プロセスから `data/agentic.db` を open する fake test が `EACCES` で失敗する / **主 pin: `test_plugin.py` が `plugin.py` へ書こうとすると `EACCES` (pytest 非ゼロ)、hash 不変、承認申請なし。副 pin: 親側 fault injection で hash を変えると承認申請が出ない** (codex 3 周目 M2) / symlink・余分ファイルを含む候補がスナップショット検査で落ちる / `sys.pycache_prefix` が tmp を指す / ゲートも launcher 経由 (`preexec_fn` 不使用) / `submit_plugin` と `bless` が同じヘルパを通る (無隔離の `_default_pytest_runner` が呼ばれない pin) / **transaction が pytest / バックテストを跨がない** (ゲート中に別接続の writer が `busy_timeout` 内に通る)
4. **D6 変異列** (§5) を全て殺す / **git サブプロセスが scheduler スレッドから呼ばれない**回帰テスト / 承認は git 記録成功後にしか `approved` にならない / **git 失敗を注入しても稼働中の旧版が消えない** (記録が切替に先行) / **切替は原子 (1 rename または 1 exchange)** (2 段化の killer、exchange 非対応は fail closed) / **初回切替で退いたプレーン dir が `_retired/` に残る** (削除されない) / **`flock` により別プロセス bless と競合しない** / **履歴は bare** (承認後に live symlink を壊す git 操作の面が無い) / **版キーは `artifact_hash`** (test 違いの版が区別される) / **承認済み symlink plugin を再改善できる** (`resolve()` 非依存) / **`PluginMeta.path` は版実体に固定され、approve は次回起動まで稼働中の plugin に影響しない** / **approval の全決定が `apply_decision` (1 tx: approval CAS + backlog 遷移 + ジャーナル終端) を通り、全 terminal decision が plugin flock を取る** / **後発決定 key は `(name, content_hash)`** / **`GC_ROOTS` (approved payload の版 ∪ live ∪ 未完ジャーナル参照物) に無い版だけが reconcile で消える** / **switch ジャーナル: 切替直後 crash → reject/別 hash approve が先に来ても live が旧状態 (symlink / `_retired` のプレーン / absent) へ戻り DB と一致、同名の未完は 1 件、`preparing` 行が FS 効果より先** / **版ストアは不変 (0500/0400)、in-place 編集は discover のディレクトリ名 hash 照合で拒否、live への bless は無い → `materialize` → `_human` → 候補経路** / **bless (`--from _human`) は approve と同じライフサイクル (ゲート → pending 行 + 証跡 → ジャーナル付き昇格 → `apply_decision`) で、通常 submit と同じ kind 別ゲートを通る** / **journal-first・sweep-last の起動順序と `GC_ROOTS` (単一定義)** / **`plugin rollback` / `bless-version` コマンドが存在しない (R12 pin)**
5. **改善レーンが取引レーンを塞がない**: 改善 Mission 実行中に `MissionSupervisor.try_submit("trade")` が受理される / 取引レーンの容量 1・直列性の既存 pin が不変 / **wave が `parallel=N` で N partition を全て担当する** / **backlog 選択の CAS** (2 接続同時で勝者 1) / **backlog 状態機械** (approve → done、reject/expire → observation、report → done / report_failed → observation、失敗 → observation、**crash 後の起動時回収 → observation:interrupted**) / **台帳の凍結** (FROZEN 後の遅延 RPC が拒否され、payload `trial_count` が実 trial の総和) / **ヒント集合外の選択は activity のみ (CAS が正)** / **1 period 1 wave** (`improve_waves` + slot 行を同一 tx で作成 = period 消費、最新 occurrence の period key、停止中に逃した period を起動後 1 回 catch-up、M=0 は行を作らない、slot `reserved→claimed` が Tx-0 と同一 tx・`ready` → `running` commit → `go` の 3-way、`go` 前は副作用ゼロ、起動時に `claimed`/`running` で interrupted は failed で再実行しない (period は消費済みのまま)、spawn は同一プロセス内で初回 + 再試行 1 回 (`spawn_attempts`)、`ready` 後の全終端は `finish_improve_mission` の 1 tx で slot+mission+run+backlog) / **接続の所有** (slot = write、dispatcher = 自前 read-only、共有なし) / **run lifecycle** (全終端経路で run が FINISHED、dangling run 無し、Tx-0 は missions+run+slot の 1 tx、`mission_id` 一意) / **Tx-2 が missions.finish を含む** (commit 直後 crash で approval/backlog/run/mission が一致) / **接続は slot 専用** (N=4 同時 Tx で混線無し) / shutdown が改善レーンの worker と CLI pgid を全て回収する / **認証コピーは親が spawn 前に行い、worker は原本を読まない**
6. **FakeRunner E2E**: 発見 → バックログ追加 (上限・重複) → 候補 → ゲート不合格でレポート止まり (承認申請なし) / ゲート合格で承認申請 (pending) → `approve` で 版 → git 記録 → symlink 切替 → approved (この順) / 30 未満 strategy が observation / 並行 2 Mission の重複選択が後着 observation / **`artifact.name` に `../` や絶対パスを返す fake が failed** / **レポート先に symlink を事前に置くと fail closed** / **timeout した Mission の `backtest_runs` / `analysis_runs` が残らない** / **レポート作成失敗時に `result=NULL`** / **`proposal_kind=risk_gate` の report は observation `unsupported_in_plan10` になり report ファイルが無い** / **strategy ゲートの baseline は live かつ D4-approved 同名 artifact、無ければ `no_strategy` 行 (null 無し)** / **report は `.tmp/*.part` → Tx-2 (`report_state=prepared`) → COMMIT 後に rename 公開 → `published`、公開失敗・`published` で最終欠損は `failed`+`result=NULL`+`done→observation`、rename 後にディレクトリ fsync、起動時 reconcile が `published ⇔ 最終存在` に収束させ孤児を消す** / **source snapshot と `_examples` は親コピーから読め、`plugins/`・`docs/` は EACCES**
7. `MissionResult.status` 4 値・決定論的コア (`risk_gate` / `paper_broker` / `transitions` / `executor` の判定) は diff ゼロ / 既存 2071 テストが壊れない / 新規 config キーは `settings.yaml.example` と同期 / migration (`improvement_backlog` の列追加) は空 DB・既存 DB で冪等 / **`discover` が `_`/`.` 先頭ディレクトリを列挙せず、正規形外の名前を skip する pin**

### 7.2 有効化後の実測 (8) — blocking ではない。既定見直しの材料

8. **実機 E2E (Task 13)**: 3 backend それぞれで「サンプル indicator plugin 1 本を候補置き場に実装し、親ゲートを通す」を実測。あわせて claude の rlimit 下 1 ターン / **`--disable apps` の egress (`strace -e trace=connect` 相当で記録)** / `--setting-sources ""` 下の init イベント / auth ローテーション有無 / exec closure の実測 (1 要素 drop) / **claude launcher の非特権 PID+mount namespace (`unshare -Upfm --mount-proc` 相当) が この host で動くか — 動けば既定にして `/proc` を allowlist から外す (R10)** / **auth 無し codex+llama_swap の実 1 ターン + サンプル plugin 候補 → 親ゲート E2E を、検証専用入口 `afx improve verify-backend` (scheduler・wave・backlog に一切触れない one-shot。`llama_swap_verified=false` のままでも動く唯一の経路。成功時に fingerprint を出力) で実行し、合格後に人間が `improve.llama_swap_verified: true` を設定して初めて通常入口が `provider=llama_swap` を受理する (codex 7 周目 I9)** を記録する。結果は §0.2 の既定見直しに使う

---

## 8. task 一覧・依存・並列束 (概略 — 詳細は実装計画で)

| 束 | # | task | 由来 | 依存 |
|---|---|---|---|---|
| **A** (runner) | 1 | 共通 launcher (`agentic_fx.runners.launcher`: PDEATHSIG + expected-parent 再照合 + 任意 rlimit + 絶対 argv execv) + `CliRunner` 共通基盤 (別セッション + timeout/killpg + `cli_started` フレーム) + factory + config schema (`^(local\|claude\|codex)$`, `runner.claude/codex`, `improve.mission_max_turns/timeout_sec`, trade=codex 拒否) + 起動時検査 (絶対パス正規化 / codex は ELF 要求 / --version / 認証 / 初期 env の秘密検査) + **`WorkerRunner` の親側 workdir 0700・`home/tmp/cfg/source` 作成・認証コピー (検査付き)・`run_context=` 受領・trade 資格情報の handshake 化 (env から除去)** | §1.1/1.4/§2.2 | — |
| A | 2 | `ClaudeRunner` + fake CLI 契約テスト | §1.2 | 1 |
| A | 3 | `CodexRunner` (provider 2 択) + fake CLI 契約テスト + argv pin | §1.3 | 1 |
| A | 4 | MCP stdio シム + mission_worker 側 dispatcher (Unix socket) | §1.6 | 1, 5 |
| **B** (柵) | 5 | `landlock.execute_paths` + backend 別 exec closure + `_bootstrap_improve_profile` 拡張 (`/dev` rw, `/proc` (claude), resolve, **handshake `mission_id`/`staging_dir`/`source_snapshot_dir` の受領・相互照合・rw 化**) + assert 拡張 + env 追加 + `/dev/null` O_RDONLY + **`discover` の `_`/`.` 除外と名前正規形 + symlink 追従 + `PluginMeta.path` を版実体に固定 + `PluginMeta.artifact_hash`** | §2 | — |
| B | 6 | Landlock ゲート pytest ヘルパ (launcher 経由・候補 ro・pyc prefix を env で・スナップショット検査・content/artifact hash before/after) + `submit_plugin` / `bless` の置換 | §4.2-3 | 1, 5 |
| **C** (registry) | 7 | improve registry (研究ツール + advisory 予算 / **`staging_dir` を根とする** staging ファイル / `run_plugin_tests` / RPC 2 種 **+ RPC 台帳 (状態機械)** + `analyze_for_agent`/`run_in_sample` の non-committing 版) + **遮断 8 項目の統合回帰を red で開始** | §3.4/§6 | 5 |
| C | 8 | バックログ拡張 (`observation` / attempts / last_result / **選択 CAS / 状態機械ヘルパ `apply_approval_outcome` / `approvals.apply_decision`** / `backlog reject\|reopen`) + `backtest_runs.variant/ref_plugin_ref/ref_content_hash` (`latest_in_sample_metrics` は candidate のみ) + `improvement_runs.mission_id` (部分 UNIQUE) + run lifecycle (Tx-0 = missions+run+slot の 1 tx / Tx-1 bind / 全終端で finish) + `improve_waves` + `improve_wave_slots` (reserved/claimed/running/done/failed、3-way 起動、終端直積表、再開なし) + **`finish_improve_mission` ヘルパ** + `improvement_runs.report_state` + `plugin_switch_journal` テーブル + 起動時回収 (interrupted → observation、`finished_at IS NULL` のみ、claimed/running+interrupted は failed、report_state の収束) + store helper の `commit=False` 変種 (`missions.start` / `decide` / `expire_due` / `missions.finish` 含む) + 注入コンテキスト生成 + prompt | §3.1/§3.2/§4.1/§4.3/§5.1 | — |
| **D** (loop) | 9 | `ImproveSupervisor` (N スロット・wave 状態機械・slot 専用 write 接続 / dispatcher 専用 RO 接続・shutdown/join + CLI pgid 回収) + scheduler の「最新 occurrence の period key」/ wave+slot 同一 tx 作成 / slot claim CAS + `ready`→`running`→`go` / spawn 初回 + 再試行 1 回 / partition ヒント再計算 / catch-up + `improve` / `improve add` / `backlog` / `policy add` コマンド (**`improve` の有効化配線は Task 12**) | §3.1 | 8 |
| D | 10 | `ImproveLoop` (三相 + `ImproveRunContext` + source snapshot (固定 `PluginMeta.path` から、`_examples` 込み) + Tx-0/Tx-1/Tx-2 (missions.finish 込み) + 補償 tx + commit 相ゲート + partition ヒント外の activity 記録 + `proposal_kind=risk_gate` の `unsupported_in_plan10` 化 + 台帳永続化 + 承認申請 + レポート (`.tmp/*.part` → Tx-2 `prepared` → COMMIT 後 rename 公開 → `published`、失敗時 `failed`+result=NULL+`done→observation`、起動時 収束) + backlog 遷移) | §4 | 6, 7, 8 |
| **E** (承認) | 11 | 版ディレクトリ (`artifact_hash`、不変 0500/0400) + bare 履歴 (blob-level plumbing) → symlink 切替 (rename / `RENAME_EXCHANGE`、退いたプレーン dir は `_retired/`) + 全 terminal decision の `flock` + **switch ジャーナル `plugin_switch_journal` (approve/bless 共通、`preparing` 先行、`op_id` 起点の `temp_path`/`retired_path`、`switch_required`、`old_kind`、name ごと未完 1 件、phase 表・復旧規則・`GC_ROOTS`・journal-first 起動順)** + **`plugin materialize` / `_human/` 候補経路 / `submit\|bless --from _human` のライフサイクル統一 (kind 別全ゲート → pending 行 + 証跡 + ジャーナルを FS 前 1 tx → 版/git/切替 → `apply_decision`)** + reconcile (孤児 staging・tmp 版・`GC_ROOTS` 外の版・temp link・未完ジャーナル・dangling) + **`approval retry` の handler と配線** + 全決定経路の `apply_decision` 化 (key `(name, content_hash)`) | §5 | 6, 7, 8 |
| **F** | 12 | FakeRunner E2E + 遮断 8 項目の完了 + **有効化配線 = `schedule.improve` の消費と `improve` コマンドの有効化のみ** (gate = §7.1 の 1〜7 逐語) | §7.1 | 1〜11 |
| F | 13 | **検証専用入口 `afx improve verify-backend`** + 実機 E2E (3 backend、§7.2 の実測項目、auth 無し llama_swap、`--disable apps` egress 記録) + 既定見直し提案 | §7.2 | 12 |

- **A-1〜3 / B-5 / C-8 は worktree 並列**可 (codex 4 周目 M4 / 5 周目 M2: **D-9 は C-8 の後、A-4 は A-1 と B-5 の merge 後** — どちらも並列集合から外す)。C-7 は B-5 の後。D-10 は B-6・C-7・C-8 の後。E-11 は B-6・C-7・C-8 の後 (A と並列可。codex 10 周目 M1: strategy bless の non-committing gate は C-7)。**F-12 は 1〜11 の後、F-13 は 12 の後**
- ファイル競合: `mission_worker.py` (A-4 / B-5) は**同一ファイル** — A-4 を B-5 の後に直列 / `worker_runner.py` (A-1 認証コピー / B-5 handshake `staging_dir`) は同一ファイル — マージ順に注意 / `plugin/approval.py` (B-6 / E-11) は順序依存 / `plugin/loader.py` (B-5 discover / E-11) は順序依存 / `store/db.py` は C-8 のみ / `service.py` (A-1 起動時検査 / D-9 / E-11 reconcile) はマージ順に注意

### 8.1 実装計画へ送る項目 (codex 各周の「実装計画へ送る項目」を要約)

> **R12 注記**: 以下のうち rollback / bless-version / adopt / `plugin_versions` / in-place bless / risk_gate 評価 / slot 再開 (`started_at`) に触れる項目は **【R12: §A へ】** を付す。該当項目は本プランの実装計画には写さず、§A の起票と一緒に後続プランへ送る (項目番号は履歴として保持)。

1. C1/C2/C3: dirfd 基準の `openat` / `O_NOFOLLOW|O_EXCL`、plugin 名 regex、3 本の不変マニフェスト、pytest 用 read-only スナップショットのヘルパと変異テストを具体化
2. C4: exec closure を claude native / codex vendor native の 2 形で採取し (nvm node ラッパは 3 周目 I8 で受理しない → 起票)、`/bin/bash`・`/usr/bin/env`・node・pytest python・動的ローダの 1 要素 drop テストを作る
3. I1: Mission ローカル RPC 台帳・commit 時の id 解決・failed/timeout 時破棄・RPC timeout 後のスレッド回収を protocol sequence と SQL transaction まで落とす
4. I2: backlog の条件付き UPDATE と、勝者だけが approval/report の副作用を出す transaction 境界を SQL 単位で書く
5. I3: wave id、M/k 予約、scheduler/manual 重複、部分 submit、shutdown 中の受付拒否を state machine と test matrix にする
6. I4: pgid ベースの所有 (別セッション + PDEATHSIG) を、SIGTERM 無視 CLI・CLI→bash 生存中の kill・service SIGTERM・N=4 同時停止の実プロセステストにする (`setsid()` 孫の逃避は起票側の cgroup で扱う — テストは「逃げる」事実の記録まで)
7. I5: advisory に縮めたので、受入条件と変異リストから安全保証の表現を外したことを実装計画でも維持 (proxy は起票)
8. I6: `flock` ファイル名・版ディレクトリ・fsync/rename 順・各 rename/commit/decide 直後の crash に対する起動時 reconcile テスト
9. I7: RPC 台帳から `analysis_run_ids` / trial count を生成する schema と、agent が id/count を偽装しても payload に反映されないテスト
10. 実測 task (claude fsize 8MB / `--disable apps` egress / claude init tools / auth rotation) + 全新規 `_Strict` config (`improve.parallel`、RPC timeout、research 予算、backlog 上限、`schedule.improve_at`、CLI grace 等) の `Settings`/example 同期 + **改善 N 並行中に取引 DB commit が busy timeout を踏まない負荷テスト**

**codex 2 周目から (要約)**:

11. C1/I7: 親が auth と `staging_dir` capability を準備する protocol sequence (mkdir 0700 → 通常ファイル/mode 検査 → auth copy → handshake → 子の再検証 → Landlock → runner 起動) をフィールド単位で書く
12. C2: 長時間処理を全て transaction 外へ出し、Tx-1 (CAS) と Tx-2 (台帳/ゲート行/approval/improvement_run/backlog 遷移) の SQL sequence を書く。全 store helper の `commit=False` 変種を列挙する
13. I4: `ImproveRpcLedger` の `OPEN/FROZEN/PERSISTED/DISCARDED`、in-flight counter、RPC timeout、Mission timeout、commit 同時発生の race matrix
14. I5: 台帳 entry `{opaque_ref, params, result, trial_count}` → 保存後の id 群と `sum(trial_count)` を payload へ解決。call count は別名
15. I1: plugin 名の字句検証、最終 symlink を辿らない dirfd API、許可する live 3 形 (absent / plain / canonical symlink) の test matrix
16. I2: `content_hash` と 3 本 `artifact_hash` の分離。同 code/config・異 test の 2 版が保存・commit・rollback できる pin 【R12: §A へ】
17. I3/I9: `RENAME_EXCHANGE` の ctypes 実装と非対応 FS の検出、crash point ごとの reconcile テスト、bare 履歴で `git status/restore` の面が無いことの pin
18. I6: backlog × approval の状態直積から許可遷移と `last_result/attempts` 更新 transaction の表
19. I8: launcher (`python -c` + PDEATHSIG + execv) の実プロセステスト: SIGTERM 無視 CLI・CLI→bash 生存中の kill・worker 親死・N=4 shutdown。`setsid()` 孫の逃避は記録のみ
20. M1/M2: gate worker の Popen env に pycache prefix を入れる pin、report file 作成成功だけを `report_path` の成立条件にする fault-injection
21. M3: Task 9/11/12 のコマンド所有と依存 (本書 §8 の表に反映済み) を実装計画でも維持
22. 継続実測 (claude fsize、apps egress、claude init tools、auth rotation)、exec closure 1 要素 drop、SQLite trade/improve 同時 writer 負荷、初回移行の各 crash 点を blocking/non-blocking の該当節へ逐語対応

**codex 3 周目から (要約)**:

23. C1/R10: 非特権 PID+mount namespace (`unshare -Upfm --mount-proc` 相当) の claude launcher を実測 — `--version`・実 1 ターン・`/proc/<parent>/environ` 拒否・DNS/LLM 到達を同時に pin。動けば既定化して `/proc` を外す。動かなければ緩和策 3 点で運用
24. `ImproveRunContext` の生成から破棄まで (missions 行 → staging/source snapshot → auth copy → ledger/RPC handlers → handshake → freeze/persist/discard) を sequence diagram とフィールド単位の protocol test に
25. Tx-1 で run/backlog owner を durable に結び、Tx-1 直後・pytest 中・backtest 中・Tx-2 直前の各 crash から起動時 `observation:interrupted` へ戻る migration/回収 test
26. `apply_decision` (approval CAS + backlog outcome) の caller-owned tx API、CAS 不一致、途中例外 rollback を SQL 単位で固定
27. slot 専用 SQLite 接続 (生成・close) と N=4 の Tx-1/Tx-2 + 取引 writer 同時実行で例外・busy timeout・rollback 混線が無い test
28. plain 初回移行の old/new artifact 版作成・bare commit (adopt)・exchange・旧 dir 照合/削除・decide の各 fault point と reconcile 期待状態の表 【R12: §A へ】
29. loader が canonical symlink 検証後に `PluginMeta.path` を版実体へ固定する pin と、approve/rollback 中も起動済みサービスが旧版を実行し続ける検証 【R12: §A へ】
30. 共通 launcher (expected-parent 再照合・PDEATHSIG・rlimit (gate)・絶対 argv・pgid 報告) の実プロセステスト: worker 親死・SIGKILL・CLI→bash 生存中・timeout・shutdown。`setsid()` 逃避は記録のみ
31. codex vendor native の executable graph fixture、closure 1 要素 drop、子 PATH 非依存。node ラッパは起票 (受理しない)
32. source snapshot と partition capability を親所有にし、他 Mission staging・未承認版を参照できない mutation test (担当外 backlog id は 7 周目 I3 で「activity のみ・CAS が正」に変更)
33. daily/weekly period key・表示 TZ・missed tick catch-up・M=0・partial submit・manual overlap・restart の schedule transition matrix と `improve_waves` CAS test
34. report write failure → `report_failed`、read-only mutation の主/副 pin、既存版ディレクトリの内容 hash 不一致、orphan report/version/temp link の fault injection、`improve.mission_max_turns/timeout_sec` を含む全 `_Strict` config と example 同期

**codex 4 周目から (要約)**:

35. R11 (C1): 受容の記録として、契約テストは「課金鍵が env に無い」に留め、scratch 認証コピーが読めることを**受容済み事実**として E2E で 1 回観測・記録する (fail closed にしない)
36. Tx-2 と `missions.finish` の線形化 SQL、Tx-2 commit 直前/直後・補償 tx の crash matrix
37. run lifecycle `CREATED→BOUND→FINISHED` を pre-Tx-1 failure / 敗者 / Tx-2 rollback / interrupted の全経路で dangling run 無しの test に
38. slot 所有 write 接続・dispatcher 所有 RO 接続・台帳 lock の ownership diagram と N=4 + 遅延 RPC test
39. scheduler は「最新 scheduled occurrence」を入力に period key を作り、daily/weekly・restart・跨 period・DST の transition matrix
40. `improve_waves` の reserved/running/completed、0/M・partial・crash recovery を SQL state machine に
41. approve/reject/expire/invalidate/reconcile/rollback の全入口が同じ plugin `flock` を通ること、CAS 敗者が live を変更しないことを multi-process test で固定 【R12: §A へ】
42. version GC root (→ 8 周目で `GC_ROOTS` に一本化: `plugin_versions` ∪ live symlink ∪ journal 参照物) を列挙し、旧 plain adopt 後の restart でも rollback 版が残る pin 【R12: §A へ】
43. risk-gate report の proposal schema、in-sample/holdout (OOS)/baseline 添付、評価不能時 observation を本体設計書 §6 と逐語対応 (5 周目 I3 で holdout を含める形に更新) 【R12: §A へ】
44. source snapshot は固定 `PluginMeta.path` だけから作り、copy 中 live exchange・3 本混成・未承認版混入を fault injection で殺す
45. D4 の key を `(name, content_hash)` に統一し、同名別 hash の approve/reject 並行 test
46. `docs/examples/plugins` は親 snapshot (`source/_examples/`) へコピーし、worker が repo の `docs/` を読めないまま sample を読める pin
47. Claude init event は許可 MCP server が exactly `afx`、未知 server 0 を実 CLI のフィールドに合わせて固定
48. Task graph `D-9 after C-8` / `F-12 after 1..11` / `F-13 after 12` を実装計画の依存表・同一ファイル競合順と一致させる
49. 実測継続: PID namespace、apps egress、Claude fsize/init/auth rotation、RENAME_EXCHANGE、exec closure drop、trade/improve writer 負荷、power-loss 時の version/git/live durability。ツール引数・syscall wrapper の選択はここで具体化

**codex 5 周目から (要約)**:

50. provider 別 credential matrix: `codex+llama_swap` は auth copy/auth 検査なし (空 `CODEX_HOME` + 非秘密 `env_key`) を fake + 実 1 ターンで固定。成立しなければ fail closed
51. wave/slot の durable 状態機械: reserve・claim・spawn 前後・crash・restart の全 fault point で「0 件なら再開・1 件以上なら消費」を検証
52. switch ジャーナル (5 周目 `plugin_promotions` → 6 周目 `plugin_switch_journal` に一般化) の DDL/phase と、切替直後 crash → reject/expire/reconcile の multi-process matrix。CAS 敗者は live を変えない
53. risk-gate report の in-sample/OOS/baseline sink と payload を本体設計書 §6 に逐語対応、holdout 派生値が worker/次回注入/RPC に出ない pin 【R12: §A へ】
54. Tx-0 = `missions.start(commit=False)` + run INSERT + slot claim の 1 tx、両 INSERT 間 crash/例外と `mission_id` 一意性の migration test
55. rollback target = D4 admission が approved の版のみ (adopted 無承認・後発 reject 済み・同名別 hash・既存 approved の matrix) 【R12: §A へ】
56. report の temp write/fsync/atomic publish (`RENAME_NOREPLACE`)/補償 unlink/起動時 orphan reconcile を fault-injection で固定
57. `PluginMeta` の固定 manifest (`path/content_hash/artifact_hash`) と snapshot copy 前後照合を型・loader test に
58. slot write 接続 / dispatcher RO 接続 / 台帳 lock / Tx-0/1/2 の thread ownership diagram と N=4 + 遅延 RPC + trade writer 負荷 test を維持
59. run lifecycle: pre-Tx-1 failure / 敗者 / Tx-2 rollback / interrupted の全経路で FINISHED・Mission と一対一・dangling 無し
60. Task graph `A-4 after A-1,B-5` / `D-9 after C-8` / `F-12 after 1..11` / `F-13 after 12` を同一ファイルの merge 順と一致させる
61. 既存 blocking mutation 群 (候補 ro、dirfd/name、bare git、`RENAME_EXCHANGE`、全 decision `flock`、固定 source snapshot、report 親専有) を crash matrix と分けずに転写
62. 実測継続 (PID namespace、apps egress、Claude fsize/init/auth rotation、RENAME_EXCHANGE、exec closure drop、writer 負荷、version/git/live/report の power-loss durability)。CLI 引数・MCP field・syscall wrapper はここで具体化

**codex 6 周目から (要約)**:

63. (89 に統合) wave slot `reserved/claimed/running/done/failed`: Tx-0 claim、`ready` → `running` commit → `go`、`started_at`、spawn 初回 + 再試行 1 回、crash、restart の各 fault point で「全 slot `started_at IS NULL` の wave だけ削除され period を消費しない・`ready` 後は再実行しない・`go` 前は副作用ゼロ」を固定 【R12: §A へ】
64. report の `report_state` outbox と、temp-only / DB-only / final-only / DB+final の起動時収束表。post-COMMIT 公開失敗時の `done→observation`、`result/report_path` clear を同表に
65. `plugin_switch_journal` の `old_kind` / old version identity / residue locator と、absent/symlink/plain × phase × approve/reject/expire/rollback/crash の matrix 【R12: §A へ】
66. 同名 plugin の未完 switch を 1 件に制限する部分 UNIQUE と、同名別 hash の approve/reject/rollback が先行ジャーナルを必ず収束させる multi-process test 【R12: §A へ】
67. adopted-only 版の fail closed 統一と、`plugin bless-version` (保管版の再ゲート → D4 approved decision) の API/CLI/test 【R12: §A へ】
68. rollback switch ジャーナルの intent-before-FS・rename・activity 同 tx・起動時 complete/revert の fault-injection。approval 行を変えない pin 【R12: §A へ】
69. risk-gate proposal の exact key/type/range/pair schema、`unsupported_param`、strategy×pair 単一再生、最低取引数の単位、`backtest_runs` identity を現行 holdout API/DB schema へ逐語対応 【R12: §A へ】
70. §4 変異の Tx-0 run create / Tx-1 bind 分離、auth 無し llama-swap の provider 固有 blocking (`improve.llama_swap_verified`) を task graph に明記
71. `plugin_versions` = 初回 provenance/GC root、switch ジャーナル/activity = rollback 履歴の所有分離。`INSERT OR IGNORE` で消える行を監査に使わない 【R12: §A へ】
72. Tx-0/1/2・Mission/run/slot 終端・slot write/dispatcher RO/台帳 lock の ownership diagram と N=4 + 遅延 RPC + trade writer 負荷 test へ転写
73. 既存 blocking mutation 群 (候補 ro、dirfd/name、bare git、`RENAME_EXCHANGE` fail closed、全 decision flock、固定 source snapshot、report 親専有、OOS 非露出) を新 crash matrix と同じ計画で維持
74. 実測継続 (auth 無し llama-swap、PID namespace、apps egress、Claude fsize/init/auth rotation、RENAME_EXCHANGE、exec closure drop、writer 負荷、power-loss durability)。CLI 引数・MCP field・SQL helper・syscall wrapper はここで具体化

**codex 7 周目から (要約)**:

75. worker startup を `ready / running-CAS / go` の sequence diagram と protocol test にし、`go` 前の tool/agent 実行ゼロを固定
76. wave/slot/Mission/run の全終端表 (pre-ready retry 枯渇、schema mismatch、4 status、Tx-2 補償、shutdown、restart、実行 0 件 wave の削除) を SQL transaction と fault point に
77. partition ヒントは非永続・再計算 (I3 簡素化側)。再 claim 時の重複は Tx-1 CAS が解く — hint 外選択の activity と敗者 observation を test に
78. switch ジャーナルの参照 path を `GC_ROOTS` に加え、journal-first / sweep-last の起動順序を version/temp/residue の crash matrix で固定 【R12: §A へ】
79. approve/bless/bless-version/rollback の phase × live 形 × DB decision/activity の収束表 (`preserved` 先行、`switched` の live==new/old 規則) を fault injection 【R12: §A へ】
80. adopted-only は直接 rollback 不可、`bless-version` の全ゲート + approved 決定後のみ可、を本文・mutation・blocking の同一文言でテストへ 【R12: §A へ】
81. `bless` / `bless-version` の kind 別ゲートを通常 submit と共有し、strategy は最低 30 取引・固定 holdout・baseline を欠けば approval/切替を作らない pin 【R12: §A へ】
82. risk proposal は `pair_rules.keys ⊆ proposal.pairs`、eligible cell、strategy ごとの最低取引数、deep-merge 全体 validation、baseline/candidate の settings hash 対応を exact schema/test に 【R12: §A へ】
83. `afx improve verify-backend` を production enable と分離し、false のまま one-shot E2E、成功後の人間による true 設定、通常入口の fail closed を固定
84. report publication は eventual invariant として temp-only / prepared+temp / prepared+final / published+final / published+missing / failed+residue の収束表と power-loss test 【R12: §A へ】
85. `artifact.type` ごとの親出力検査を schema test にし、report/observation が plugin-only の name/path 検査を通らないことを固定
86. Tx-0/1/2・slot write/dispatcher RO/台帳 lock・Mission/run 一対一を ownership diagram と N=4 + 遅延 RPC + trade writer 負荷 test に転写
87. 既存 blocking mutation 群を新しい slot/journal matrix と同じ計画で維持
88. 継続実測 (auth 無し llama-swap、PID namespace、apps egress、Claude fsize/init/auth rotation、RENAME_EXCHANGE、exec closure drop、writer 負荷、power-loss durability)。CLI 引数・MCP field・SQL helper・syscall wrapper はここで具体化

**codex 8 周目から (要約)**:

89. wave/slot/Mission/run の sequence と全終端表を、`ready/running/go`、`started_at`、pre-ready retry 枯渇、post-ready 4 status、shutdown、restart の SQL transaction/fault point に 【R12: §A へ】
90. `finish_improve_mission` が slot+mission+run+backlog を同一 tx で更新し、手動 one-shot だけ slot 無しになる ownership diagram と test
91. weekly/daily scheduled wave と manual one-shot の Tx-0 分岐、period key、DST/catch-up/M=0/N=4 の transition matrix
92. `GC_ROOTS` を単一 helper/query にし、journal-first/sweep-last を version/temp/residue/staging の crash matrix で固定 【R12: §A へ】
93. `op_id` 起点の temp/residue locator、`switch_required=0`、plain/symlink/absent × phase × live × DB decision/activity の表 【R12: §A へ】
94. rollback の exact artifact approval query を same content/different test・adopted・bless-version・後発 reject の fixture で固定 【R12: §A へ】
95. strategy baseline/candidate の identity、pair/timeframe、in-sample/holdout、settings_hash、新規 strategy の `no_strategy` baseline を payload 行へ一対一対応
96. risk proposal の global/pair-scoped 混在 schema、導出 `affected_pairs`、baseline/候補双方の strategy ごと 30 件、0 cell、deep-merge を exact test に 【R12: §A へ】
97. report outbox を temp-only / prepared+temp / prepared+final / published+final / published+missing / failed+residue の全状態表にし、補償 tx と directory fsync の fault test 【R12: §A へ】
98. Tx-0/1/2・slot write/dispatcher RO/台帳 lock・Mission/run 一対一を N=4 + 遅延 RPC + trade writer 負荷 test へ転写
99. 1・2 周目から維持する blocking mutation を新しい wave/journal matrix と同じ計画で維持
100. `afx improve verify-backend` の one-shot protocol、成功 fingerprint、false→人間 true、通常入口 fail closed を CLI/E2E に
101. 実測継続 (auth 無し llama_swap、PID namespace、apps egress、Claude fsize/init/auth rotation、RENAME_EXCHANGE、exec closure drop、writer 負荷、power-loss durability)
102. CLI の正確な引数、MCP protocol field、SQL helper、dirfd/openat、renameat2/fsync wrapper、fault-injection harness の選択は設計の粒度を超える — 意味論を変えない範囲で実装計画に具体化

**codex 9 周目から (要約)**:

103. bless / bless-version を「gate sink + pending approval + journal の先行 tx → version/git/switch → apply_decision」にした sequence と、各 commit/rename 直後 crash の recovery。version/link/residue temp は全て `op_id` 起点 【R12: §A へ】
104. `switch_required=0` は approve/bless=`recorded→decided`、rollback=`preserved→completed` とし、plain/symlink/absent × phase × live × DB の fault matrix 【R12: §A へ】
105. CLI の completed/failed/timeout/max_turns/worker EOF/shutdown の全終端で同一 pgid を空にする ownership test (`setsid()` 逃避は記録のみ)
106. 版ストア不変 pin と、symlink live を in-place 編集せず `materialize` → gate → bless する CLI/E2E。ディレクトリ名と実 `artifact_hash` の不一致は loader/rollback/reconcile の全入口で fail closed 【R12: §A へ】
107. rollback query を D4 latest content decision + exact artifact approved row で固定し、same content/different test・adopted-only・bless-version 後・後発 reject・切替済み pending を fixture 化 【R12: §A へ】
108. `backtest_runs` の candidate/baseline/no_strategy identity と payload の row-id 対応、settings hash、pair/timeframe、in-sample/holdout を migration と query 単位で
109. risk proposal の `affected_strategies` 非空 intersection、global/pair-only/mixed、無関係 pair strategy、baseline/candidate 双方の strategy ごと 30 を exact fixture に 【R12: §A へ】
110. `finish_improve_mission` を Tx-2 成功・補償・4 runner status・出力不正・shutdown・restart の唯一の terminal helper にし、`slot_key=None` は manual のみ、の SQL/fault matrix
111. spawn は初回 + retry 1 回、period 非消費は `all started_at IS NULL` のみ、と weekly/daily/manual/DST/catch-up の transition matrix 【R12: §A へ】
112. report outbox の全状態表 (temp-only / prepared+temp / prepared+final / published+final / published+missing / failed+residue) と補償 tx・両 directory fsync の fault test 【R12: §A へ】
113. 1・2 周目から維持する blocking mutation (reports 親専有、name/dirfd、候補 ro、exec closure、RPC freeze、backlog CAS、bare git、launcher、pycache prefix、task ownership) を wave/journal/baseline matrix と同じ計画で維持
114. 実測継続 (auth 無し llama_swap、PID namespace、apps egress、Claude fsize/init/auth rotation、RENAME_EXCHANGE、exec closure drop、writer 負荷、power-loss durability)
115. CLI 引数・MCP field・SQL helper・dirfd/openat・renameat2/fsync wrapper・fault-injection harness の選択は設計の粒度を超える — 確定意味論を変えない範囲で実装計画に具体化

## 9. 変えないもの

- 決定論的コアの判定ロジック / drawdown kill switch / 取引レーンの容量 1・直列性・preemption 契約
- `MissionResult` の 4 値 / `Mission` のフィールド / `LocalRunner` の tool-calling loop とリトライ規律
- trade profile の権限境界 (RO DB。Landlock 任意) — ClaudeRunner を trade で選んでも MCP ツールのみ。**資格情報の運搬だけ env → handshake に変わる** (R10-①、到達できる情報は同じ)
- `IMPROVE_FORBIDDEN` の pin / 遮断 8 項目の意味論 / holdout の所有 (ハーネス) / `backtest.holdout_months` はコア所有
- plugin 機構の契約 (3 ファイル・純関数・AST allowlist・サンドボックス実行・**`content_hash` の定義 (2 本、承認の実行時 identity)** — `artifact_hash` (3 本) は版ディレクトリのキーとして**新設**するが `content_hash` を置き換えない)
- 改善ループの出力先が gitignore 領域 (`plugins/`・`reports/`) に閉じること (2026-08-08 裁定)。**worker が書くのは `plugins/_staging/` のみ、`reports/` は親が書く**
- **人間の plugin 編集面**: 本プランでは `plugins/_human/<name>/` に統一する (live は編集対象外)。既存の手作りプレーン plugin は初回承認まで従来どおり動く (R12-(d))
- `RLIMIT_NPROC` を mission worker に掛けない現状 (変えない)
- `Notifier.send` の握り潰し (起票のまま)

## 10. 起票 (本プランでは直さない)

プラン 9 §6 から引き継ぎ: 外向きリクエスト予算の**全体**設計 (Global Constraints 化・共通出口・datafeed 4 経路・Discord) / `missions.failure_reason` / worker 診断タスク (result フレーム 5 箇所の `error` を親へ) / **improve モデルの `/models` 存在確認** (backend=local の improve が本プランで初めて実ツールを持つ — 実装計画で warn-only の扱いを決めてよい) / plugin `max_bars` × cache 保持期間 / SSE error event / `ohlcv_cache` の物理分離 / キャッシュ→履歴の昇格 / ヘッジ併存。プラン 9 束 D/E から: `Notifier.send` の成功戻り値契約 / `trade_intents` の保持・prune / `reject_category` の StrEnum 化 / db.py の table rebuild 骨格重複 / `_backup_before_migration` のログ文言。

本プランで新規:
- **【R12-(a)】`plugin rollback` / `plugin bless-version` / 既存プレーン版の adopt / `plugin_versions` provenance 表** — 設計テキストは §A.1 (9 周分の codex 指摘を反映済み: exact artifact の approved 行を要する rollback 述語、adopted-only の fail closed、`preserved` 先行、rollback の switch ジャーナル)。採用時期は「承認済み版が複数溜まり、戻す需要が出たとき」。それまでは `materialize` → 編集 → `bless --from _human` の新規承認で代替
- **【R12-(b)】risk_gate 提案の親側評価** — 設計テキストは §A.2 (intent 単位 key の strict schema、`affected_pairs`/`affected_strategies` の導出、baseline/候補の in-sample + holdout 比較、`backtest_runs.variant` の用途 identity)。プラン 10 では `proposal_kind=risk_gate` を `unsupported_in_plan10` observation にする。ポートフォリオ水準 key の評価 (下記) と一体で後続プランへ
- **【R12-(c)】wave slot の再開機構** — §A.3 (`started_at`・全 failed wave の削除で period を返す・claimed の同 period 再開)。プラン 10 は「wave 行 = period 消費・再開なし・取りこぼしは手動 `improve`」。週次の取りこぼしが実運用で問題になったら再訪
- **【R12-(d)】live path の in-place 編集を伴う bless** — 廃止のまま (再訪の予定なし)。人間の編集面は `_human/`
- **代替ローカルハーネス候補 (Qwen Code / Aider / OpenCode / Goose)** — 「ローカル LLM でハーネスを回す」目的は codex+llama-swap で満たす方針だが、実機 E2E (§7.2) で codex+llama-swap の plugin 実装力が不足と出たら、Qwen Code (qwen3.x 系の本家ハーネス、モデル相性) / Aider (弱いモデル向けの編集形式で成熟) / OpenCode / Goose を **4 番目の backend 候補として実測**する。§1 の `CliRunner` は「CLI 起動 → JSON 回収」の共通基盤なので追加コストは argv・出力形式・認証の差分に限られる。版・機能・ライセンスは採用検討時に実測で確認 (2026-08-16 ユーザー希望で起票)
- **claude → ローカル LLM 駆動** (R9): 要実測 (llama-swap が Anthropic 形式を受けるか) / 鍵契約の言い換え (「課金エンドポイントへの鍵は渡さない」) / 規約確認。E2E で codex+llama-swap が不足と分かったら再訪
- **codex `apps` egress の停止手段** — 検証・記録は本プラン Task 13 で行う (§1.3/§7.2)。`--disable apps` で止まらなかった場合の停止手段 (別 config key / proxy) はここに残る。argv pin は既知 3 経路の退行防止であり、この egress を「対処済み」にするものではない
- **codex の node ラッパ (nvm `codex.js`) 経由起動** — 起動時検査は vendor native を要求する (§1.4)。ラッパを受理するには wrapper/node/vendor native/同梱 helper の exec closure を blocking で実測する必要がある (codex 3 周目 I8)
- **risk gate 提案のポートフォリオ水準評価** — `max_positions` / `max_total_risk_pct` / `max_leverage` / `daily_loss_limit_pct` / `drawdown_kill_pct` 等は単一 strategy 再生では評価できず、strategy 集合の manifest hash・同時 intent の順序・複数 timeframe を持つポートフォリオ harness が要る。プラン 10 では `unsupported_param` (codex 6 周目 I7)
- **サブスク認証トークンの別主体化 (R11)** — 改善 worker と認証主体を分ける: 別 UID での改善 worker 実行 / credential broker (CLI からのトークン要求を親が代理) / 短命トークン。いずれも CLI 側の対応可否に依存する (要調査)。入るまでは §2.4 の受容
- **claude 用 `/proc` の恒久対処** — 非特権 PID+mount namespace は Task 13 で実測 (§7.2)。動かない場合の候補: 別 UID での改善 worker 実行 / seccomp。動くまでは R10 の緩和策 3 点で運用
- **改善 worker のプロセス包含 (cgroup v2)** — 現状は pgid ベース (§1.1-4、launcher 経由 PDEATHSIG)。`setsid()` を呼ぶ孫は逃げる。完全な包含は Mission ごとの cgroup v2 (systemd --user scope) + `cgroup.kill`。`RLIMIT_NPROC` の適用も併せて検討
- **改善 worker の egress を親の代理へ限定する (network namespace / egress proxy)** — 外向き予算の全体設計の一部。改善 worker からの AF_INET(6) を親の proxy 以外へ禁じ、LLM エンドポイントと研究 HTTP の双方で予算・レート・429 方針を強制する。これが入るまで §6 は advisory
- claude の本番 rlimit (`fsize 8MB`) 超過時の扱い — 実測は Task 13 (§7.2)。超えた場合の improve 別 `child_fsize_mb` はここに残る
- codex+llama-swap の schema 安定性 (フェンス付き応答。E2E で計測、`response_parser` で足りなければ再出力プロンプトを検討)
- 認証ファイルのコピー・ドリフト (トークンリフレッシュが scratch 側だけに落ちる)。長期運用で「コピーが古くなる」方向。対処候補: 実ファイルの mtime 監視 / 期限切れ時の明示エラー
- MCP シムのプロトコル版追随 (claude / codex が要求する MCP バージョン)
- `ImproveSupervisor` の heartbeat / watchdog 統合 (取引レーンの `busy_since` 相当を改善レーンにも持つか)
- 改善 Mission の transcript 保存量 (CLI のストリームは LocalRunner より多弁。`transcript_max_bytes` の improve 別設定)

## 11. レビュー方針

**設計レビューは codex 単独の反復**。新規設計を含む文書は 7 周を見込む (プラン 9 実績)。毎周「文書全体の自己矛盾の総ざらい」を依頼に含め、3 周目以降は指摘のあった節を通しで書き直す (パッチしない)。指摘がツールの実装詳細 (CLI の引数名・MCP のフィールド) に降りてきたら実装計画へ送る判断をレビュアーに併せて求める。実装計画のレビューは別途 (設計収束を計画の品質の根拠にしない)。

## 12. レビュー履歴

| 周 | 日付 | レビュアー | 結果 | 処置 |
|---|---|---|---|---|
| 1 | 2026-08-16 | codex (gpt-5.6-sol) | C4 / I7 / M3 = 14 件 | **全件採用** (`.superpowers/sdd/plan10-design/codex-round1.md`)。**C1 は事実誤り** — 「現行 rw マスクは `MAKE_SYM` を含む」は誤りで、`_READ_WRITE_ACCESS` (`landlock.py:111-114`) に `MAKE_SYM` は無く symlink 作成は Landlock で拒否される。**修正は別の理由 (所有境界の単純化 — 案 A で worker が `reports/` に書く必要が無い) で採用**し、親のレポート書込は `O_CREAT\|O_EXCL\|O_NOFOLLOW` とした。**I4 は部分採用** (ユーザー裁定 2026-08-16: 別セッション + PDEATHSIG + CLI セッション killpg。cgroup v2 は起票)。**I5 は advisory 裁定** (ユーザー裁定 2026-08-16: 研究ツール予算はツール契約、egress proxy は起票)。他 (C2 名前正規形 / C3 不変スナップショット + H_before/H_after / C4 exec closure / I1 RPC 台帳 / I2 backlog CAS / I3 wave / I6 版ディレクトリ + symlink 1 rename + flock / I7 台帳由来の id / M1 §7 分割 / M2 `--disable apps` の所在一本化 / M3 discover 除外を本プランで追加) は記述どおり採用。§2/§3/§4/§5/§6/§7/§8 を通しで書き直し |
| 2 | 2026-08-16 | codex (gpt-5.6-sol) | C2 / I9 / M3 = 14 件 (1 周目 14 件の閉鎖判定: 閉じた 10 / 部分 4) | **全件採用** (`.superpowers/sdd/plan10-design/codex-round2.md`)。C1 認証コピーを親 (Landlock 外) の spawn 前へ / C2 transaction を Tx-1 (CAS) + Tx-2 (最後の短い tx) に分け長時間処理を tx 外へ、store helper に `commit=False` 変種 / I1 切替先検査から `resolve()` を排し lstat + 3 形列挙 / I2 `artifact_hash` (3 本) を版キーに新設、`content_hash` 不変 / I3 プレーン初回移行を `RENAME_EXCHANGE` で原子化、非対応は fail closed / I4 台帳の状態機械 (FROZEN) + timeout 呼出しは数えない / I5 `trial_count` = 実 trial 総和、`analysis_call_count` を分離 / I6 backlog 状態機械 (表) / I7 handshake `staging_dir` 1 値 / I8 `preexec_fn` 廃止 → launcher / I9 履歴を bare `plugins/.history.git` / M1 pycache prefix を env で / M2 report 失敗時 `result=NULL` / M3 `approval retry` `plugin rollback` を Task 11 へ、Task 12 は有効化配線のみ。§4/§5 を通しで書き直し |
| 3 | 2026-08-16 | codex (gpt-5.6-sol) | C1 / I12 / M3 = 16 件 (2 周目 14 件の閉鎖判定: 閉じた 10 / 部分 4) | **全件採用**。**C1 (`/proc` による同一 UID env 読取) はユーザー裁定 (R10) で緩和策 3 点 + 実測付き選択肢として採用** (trade 資格情報を handshake へ / 起動時に自身の初期 env の秘密検査で improve+claude 起動拒否 / 残余を明記 / PID+mount namespace を Task 13 で実測し動けば既定)。I1 旧プレーン版の adopt / I2 Tx-1 に `improvement_runs.start` + `mission_id` 列 + 起動時回収 / I3 `apply_decision` 単一 API + `decide`/`expire_due` の `commit=False` / I4 `PluginMeta.path` を版実体に固定・hot reload 無し / I5 `ImproveRunContext` (`run_context=`) + handshake 3 フィールド相互照合 / I6 `read_plugin_source` は親のスナップショット / I7 gate も launcher・`preexec_fn` 全面禁止 / I8 絶対 argv・codex は vendor native 要求 (node ラッパは起票) / I9 launcher の expected-parent 再照合 + `cli_started` pgid / I10 `allowed_backlog_ids` を親が強制 / I11 `improve_waves` CAS・period key・catch-up・M=0 非消費 / I12 slot 専用接続 / M1 `report_failed` 遷移 / M2 主 pin/副 pin の分離 / M3 `improve.mission_max_turns/timeout_sec` + 写像表。§3.1 / §4 / §5.1 を通しで書き直し |
| 4 | 2026-08-16 | codex (gpt-5.6-sol) | C1 / I10 / M4 = 15 件 (3 周目 16 件の閉鎖判定: 閉じた 12 / 部分 4) | **全件採用**。**C1 (scratch OAuth コピーを agent が読み持ち出せる) はユーザー裁定 R11 でハーネス固有の残余リスクとして受容** (fail closed にしない。不変条件 3 の明示的例外・§2.4・起票)。I1 Tx-2 に `missions.finish` を含め終端を単一 commit point / I2 dispatcher は自前 RO 接続・RunContext は接続ファクトリ / I3 「最新 occurrence の period key」で逃した period を 1 回 catch-up / I4 `improve_waves` の reserved/running/completed + expected/submitted、`submitted=0` は起動時削除 / I5 全 terminal decision が plugin flock / I6 `plugin_versions` テーブル = GC root、adopt/rollback 行 / I7 report に `proposal_kind`、risk_gate は親の in-sample (≥30) + baseline 添付が無ければ observation、holdout 無し / I8 snapshot は固定 `PluginMeta.path` から flock 下でコピー + hash 再照合 / I9 後発決定 key `(name, content_hash)`、revoke は設けない / I10 run は Tx-0 (prepare) で生成、Tx-1 で bind、全終端で finish、状態表 / M1 §2.3 の「live 可読」を除去 / M2 init pin = `afx` ちょうど 1 つ / M3 `docs/examples/plugins` を `source/_examples/` へ親コピー / M4 D-9 は C-8 後、F-12 は 1〜11、F-13 は 12。§3.1 / §4 冒頭・4.1 を通しで書き直し |
| 5 | 2026-08-16 | codex (gpt-5.6-sol) | C1 / I6 / M2 = 9 件 | **全件採用**。C1 codex+llama_swap は auth.json をコピーせず空 `CODEX_HOME` + 非秘密 `env_key`、auth 無し実 1 ターンを blocking 実測 / I1 `improve_wave_slots` を wave 行と同一 tx で作成、slot claim CAS を Tx-0 と同一 tx、`submitted` は導出、reserved slot は同 period で再開 / I2 昇格ジャーナル `plugin_promotions` (phase 前書き、reject 前に巻き戻し、収束規則) / I3 risk_gate report は baseline/候補の in-sample + holdout OOS 比較、非露出 / I4 Tx-0 = `missions.start(commit=False)` + run + slot の 1 tx、`mission_id` 部分 UNIQUE / I5 rollback は D4 approved 版のみ・非 decision・`plugin_versions` は provenance / I6 report は `.tmp/*.part` → COMMIT 後 rename 公開、補償 tx、起動時 orphan 掃除 / M1 `PluginMeta.artifact_hash` / M2 A-4 依存 `1,5`、並列集合 {A-1..3, B-5, C-8}。§3.1 wave 節・§4.2-6・§5.1 を書き直し |
| 6 | 2026-08-16 | codex (gpt-5.6-sol) | C0 / I7 / M3 = 10 件 (由来: 前周修正 6 / 既存 1) | **全件採用**。I1 slot `reserved→claimed (Tx-0)→running (ready ack)→done|failed`、claimed は起動時 reserved へ戻し同 period 再開、spawn 失敗は 1 回再試行、ready 後は再実行しない / I2 `improvement_runs.report_state` (none/prepared/published/failed)、`published ⇔ 最終存在`、公開失敗で `done→observation` / I3・I4・I6 ジャーナルを `plugin_switch_journal` (approve/rollback 共通、`old_kind`/`old_artifact_hash`/`residue_path`、name ごと未完 1 件) に一般化、plain は adopted 旧版への symlink 復元、absent は live 除去 / I5 adopted-only は rollback 不可で統一、`plugin bless-version` を新設 / I7 risk_gate の許可 key を intent 単位に列挙、ポートフォリオ key は `unsupported_param`、strategy×pair 単一再生 / M1 §4 変異を Tx-0/Tx-1 に分離 / M2 auth 無し llama_swap は provider 固有 blocking (`improve.llama_swap_verified`) / M3 `plugin_versions.origin` 不変。§3.1 slot・§4.2-6・§5.1 ジャーナルを書き直し |
| 7 | 2026-08-16 | codex (gpt-5.6-sol) | C0 / I9 / M2 = 11 件 (由来: 前周修正 7 / 既存 2) | **全件採用** (I3 は簡素化側)。I1 `ready` → 親が slot `running` を commit → `go` → Mission 開始の 3-way、`go` 前は副作用ゼロ / I2 wave×slot×mission×run の終端直積表、`ready` 後の終端は 1 tx、全 slot failed で running/done 0 の wave は削除 = period 非消費 / I3 不変スナップショットを撤回 — partition は非永続のヒント、正しさは Tx-1 CAS、hint 外は activity のみ (3 周目 I10 の硬い拒否を撤回) / I4 GC root にジャーナル参照物、journal-first・sweep-last、再開前の存在+hash 再検証 / I5 操作別 phase 表、`preserved` を最初の行に、`switched` の復旧は live==new→完遂・live==old→取消 / I6 「旧版へ rollback できる」を全廃、bless-version 後のみ / I7 bless・bless-version は kind 別の全ゲート、rollback のみ再ゲート不要 / I8 pair-scoped key ⊆ proposal.pairs、cell = strategy × (meta.pairs ∩ proposal.pairs)、strategy ごと ≥30、deep-merge 全体検証 / I9 検証専用入口 `afx improve verify-backend` / M1 eventual invariant / M2 name 検査は plugin 分岐のみ。§3.1 slot・§5.1 ジャーナル・§5.3 順序を書き直し |
| 8 | 2026-08-16 | codex (gpt-5.6-sol) | C0 / I10 / M2 = 12 件 (由来: 前周修正 5 / 既存 5) | **全件採用**。I1 slot `started_at`、削除は「全 failed かつ全 started_at NULL」のみ / I2 `finish_improve_mission` 単一ヘルパ (slot+mission+run+backlog を 1 tx、補償 tx にも slot failed) / I3 「週次 wave のときだけ」→「scheduler 起動 wave (weekly/daily)、手動 one-shot は除く」 / I4 `GC_ROOTS` の単一定義 (journal 参照物を含む) / I5 `op_id` 起点の `temp_path`/`residue_path`、`approval_id` は bless 系で終端時に充填 / I6 `switch_required` を INSERT 時に確定 / I7 rollback 許可 = D4 approved + `artifact_hash` 一致の approved 行 / I8 strategy baseline = live かつ D4-approved 同名 artifact、無ければ `no_strategy` 行、null 無し、submit/bless/bless-version 共通 / I9 `affected_pairs` 導出、`pairs_mismatch`、baseline・候補とも strategy ごと ≥30 / I10 `published`+最終欠損の補償、ディレクトリ fsync / M1 partition 変異の文言 / M2 `plugin_versions.origin ∈ {approval, adopted}`、rollback は行を書かない。旧前提の残骸を §3.1/§4.1/§4.2/§5.1/§5.3/§7 で掃除 |
| 9 | 2026-08-16 | codex (gpt-5.6-sol) | C0 / I6 / M6 = 12 件 (ユーザー裁定でフルスコープ継続) | **全件採用**。I1 bless / bless-version を approve と同一ライフサイクル (ゲート → pending 行 + ゲート行 + ジャーナル行を FS 前の 1 tx → 版/git/切替 → `apply_decision`)、temp は全て `tmp-<op_id>` / I2 `CliRunner` の `finally` で全終端に pgid を空にしてから返す / I3 版ストア不変 (0500/0400)、ディレクトリ名 hash 照合、symlink live への bless 拒否 → `materialize` → `_human/` 候補経路 / I4 `backtest_runs.variant` (candidate/baseline/no_strategy) + `ref_*`、`no_strategy` の identity、`latest_in_sample_metrics` は candidate のみ / I5 `affected_strategies` の定義 (非空 intersection、空は評価不能、`unaffected` 列挙) / I6 架空の `backtest.min_trades` を撤去し既存 `EVALUABLE_MIN_TRADES` を明示 / M1 Tx-2 の末尾は `finish_improve_mission` 1 回 / M2 §5.3(c)・§7.1-4 の旧文を訂正 / M3 §4.2 の report reconcile 再掲を §4.1 参照に / M4 §3 の旧 partition 変異を削除 / M5 `spawn_attempts` = 初回 + 再試行 1 回に統一 / M6 §8.1-63 を 89 に統合。旧前提の残骸を再掃除 |
| 10 | 2026-08-16 | codex (gpt-5.6-sol) | C0 / I4 / M1 = 5 件 (由来: 前周修正 1 / 既存 3 / task 1)。**判定: 4 領域 ((a) rollback/bless-version/adopt (b) risk_gate 親評価 (c) slot 再開 (d) bless in-place) を起票/簡素化すれば設計レベル Critical/Important 0** | **ユーザー裁定 R12 (2026-08-17)**: (a)(b)(d) を起票 (§A に設計を保存)、(c) を簡素化。I1 (`op_id` 採番前の temp 名) は `preparing` phase の先行 INSERT で採用 / I2・I4 は (a)(d) の削除で消滅 / I3 (risk proposal の成績 identity) は (b) の削除で消滅 (`variant` は plugin admission 用だけ残す) / M1 E-11 の依存に C-7 を追加。§5 を縮小版で書き直し、§3.1 wave/slot・§4.2-6・§4.3・§7・§8・§9・§10 を整合。11 周目は縮小版の収束確認 |


---

## A. 起票済み設計 (R12 でプラン 10 から外した機能。実装しないが設計テキストを保存する — 後続プランの入力)

> **R12 (2026-08-16)**: codex 設計レビュー 10 周で (a) rollback / bless-version / 既存プレーン版の adopt / `plugin_versions` provenance 表、(b) risk_gate 提案の親側評価、(c) wave slot の再開機構、(d) bless の in-place 系 — の 4 領域が指摘の大半を生み続けたため、(a)(b)(d) をプラン 10 から外し (c) を簡素化した。codex 10 周目の判定: 「この 4 領域を起票/簡素化した場合、設計レベルの Critical/Important は 0 件」。以下は縮小前の設計テキストの**そのままの写し** (9 周目反映版、`2bab412`)。読むときは §5・§3.1・§4.2 の現行本文が正で、本節は起票の入力である。

### A.1 (a)(d) 縮小前の §5 全文 (rollback / bless-version / adopt / `plugin_versions` / in-place bless を含む)

> ## 5. 承認時の履歴記録と昇格 (D6 の再収束) — 記録が先、切替は原子 (1 rename または 1 exchange)
>
> ### 5.1 承認手順 (人間が `approve <id>` した瞬間。シェル / 起動時 reconcile / 将来の REST から。**scheduler スレッドでは決して実行しない**)
>
> **順序の原則: 履歴への記録が先、本番への切替は最後に原子操作 1 回、DB 決定はさらにその後。** git がどう失敗しても稼働中の旧版は消えず、「live が無い瞬間」が存在しない。
>
> **レイアウト**:
> - 承認済み内容は **版ディレクトリ `plugins/.versions/<name>/<artifact_hash>/`** (3 本全体の hash がキー — 同じ code/config で test だけ違う版も区別する、codex 2 周目 I2)。approval の実行時 identity は従来どおり `content_hash` (2 本) で、payload は両方を持つ
> - 稼働中の `plugins/<name>` は**版ディレクトリを指す相対 symlink** (`.versions/<name>/<artifact_hash>`)。ロールバック = symlink を別の版へ向け直す (`plugin rollback <name> <artifact_hash>`。**git checkout は使わない**。**対象は「その `(name, content_hash)` の最新決定が approved (D4 admission) で、かつ payload の `artifact_hash` が対象版の `artifact_hash` と一致する approved な approval 行が存在し、かつ版ディレクトリが在る」版だけ** (content 水準の承認だけでは足りない — 同 content で test 違いの版を区別する、codex 8 周目 I7)。**adopted のみで承認の無い版・後発 reject 済みの版・approval 行の artifact_hash が異なる版へは fail closed** (codex 5 周目 I5 / 6 周目 I5)。そこへ戻したい人間の唯一の経路は **`plugin bless-version <name> <artifact_hash>`**: 保管済み版ディレクトリに対して §4.2-3 のゲート (スナップショット検査・AST・Landlock pytest・hash) を再実行し、成立すれば版から payload を作った approval 行 (`kind=plugin`, `decided_by=human_cli`) を D4 approved 決定として作成し、同じ switch ジャーナル/切替機構で live を向ける — 以後その版は rollback 対象になる。**rollback は approval の terminal decision ではなく、監査付きの別操作**: 同じ plugin flock 下で switch ジャーナル (`kind='rollback'`) を先に書き、symlink を 1 rename で切り替え、activity `plugin_rollback` を同じ tx で残す。**`plugin_versions` は rollback で行を書かず、`origin` も更新しない (rollback 履歴は switch ジャーナルと activity だけが記録先、codex 6 周目 M3 / 8 周目 M2)**。**discovery は symlink を辿った先の版ディレクトリを `PluginMeta.path` に固定し、稼働中サービスは再起動まで同じ版を使い続ける** (§2.3、codex 3 周目 I4) — 切替は次回起動/再読込にだけ効く
> - 履歴は **bare リポジトリ `plugins/.history.git`** (`git init --bare` を lazy に。**ワークツリーを持たない** — codex 2 周目 I9)。人間の閲覧は `git --git-dir=plugins/.history.git log|show`
> - `plugin/loader.discover` は先頭 `.`/`_` を除外 (§2.3) するので `.versions/`・`.locks/`・`.history.git/` は列挙されない。プレーンなディレクトリ (手作り、初回移行前) と symlink 版は共存できる
>
> 1. **排他 + switch ジャーナルの確認**: **`flock` を `plugins/.locks/<name>.lock` に取得** (プロセス間 — サービスの approve / **reject / expire / invalidate** / 起動時 reconcile / `plugin rollback` / `plugin bless-version` / 別プロセスの `afx plugin bless` が**全て**同じ lock を取る、codex 4 周目 I5) + プロセス内は plugin 名ごとの `threading.Lock` (ⓒ、順序は flock → thread lock)。lock 取得後に approval が `pending` であること・後発決定・**同名 plugin に未完の switch ジャーナルが無いこと**を再確認してから FS 副作用に進む (未完があれば先に reconcile — 下記)。異なる plugin の並行承認は 5.2 の CAS が守る
>    **switch ジャーナル `plugin_switch_journal` (codex 5 周目 I2 / 6 周目 I3・I4・I6 / 7 周目 I4・I5 — `flock` は crash を越えない。approve / bless / bless-version / rollback の切替を 1 つの機構で記録する)**: 新テーブル `plugin_switch_journal(op_id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('approve','bless','bless_version','rollback')), approval_id INTEGER, name TEXT NOT NULL, old_kind TEXT NOT NULL CHECK(old_kind IN ('absent','symlink','plain')), old_target TEXT, old_artifact_hash TEXT, temp_path TEXT NOT NULL, residue_path TEXT, new_target TEXT NOT NULL, switch_required INTEGER NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('preserved','versioned','recorded','switched','decided','completed','reverted')), actor TEXT NOT NULL, updated_at TEXT NOT NULL)`。**行の INSERT が `op_id` を先に割り当て、`temp_path = plugins/.<name>.link-<op_id>`、plain のときの `residue_path` は exchange 後に同じパスへ来る旧 dir (= `temp_path` と同値) を最初の行に確定して書く** (codex 8 周目 I5)。**`approval_id` は nullable — approve / bless / bless-version は最初の行から既知 (bless 系は pending 行とジャーナル行を同じ tx で作る — 9 周目 I1)、rollback は NULL**。**`switch_required` は INSERT 時に確定** (旧の正規 target == 新の正規 target なら 0 → FS 切替を省略し、`recorded` の後そのまま終端 tx へ。`switched` の復旧規則は `switch_required=1` の行にだけ適用 — codex 8 周目 I6) + **部分 UNIQUE index: 非終端 phase (`decided`/`completed`/`reverted` 以外) の行は `name` ごとに高々 1 件**。**旧状態 (kind / target / artifact_hash) は最初のジャーナル行より前に確定し、plain なら旧版の保全 (5) を版ストア + git まで済ませてから、`phase='preserved'` で行を INSERT する** (I5)。以後 **各 phase の書込は次の FS 効果より先、lock 下の短い tx**:
>
>    | 操作 | 行の INSERT (`preserved`) 前に済ませること | phase 列 | 終端 |
>    |---|---|---|---|
>    | approve | 旧状態の確定 + (plain なら) 旧版保全 | preserved → versioned (新版作成) → recorded (git) → switched (切替直前) → decided | `apply_decision` と同一 tx |
>    | bless / bless-version | 旧状態の確定 (bless は旧=新内容、bless-version は保管版) + **kind 別の全ゲート (§4.2-3・§4.2-4)** + **pending approval 行 + ゲート行 + ジャーナル行を同一 tx (FS 効果前)** | 同上 | `apply_decision(approved)` (approve と同一経路)、同一 tx |
>    | rollback (D4 approved 済みの同一 artifact への切替のみ、ゲート不要) | 旧状態の確定 | preserved → switched → completed | activity と同一 tx |
>
>    **収束規則**: ジャーナルが終端 ⇔ live symlink と DB (approval 決定 / rollback 監査) が一致。**`switched` で止まっていた行の復旧規則 (approve・rollback 共通、`switch_required=1` のみ)**: `live == new_target` → 完遂 (approve は decide へ、rollback は activity を書いて `completed`) / `live == old 状態` → 取消 (`reverted`) / どちらでもない → activity ERROR で人間待ち (触らない)。未完ジャーナルの扱い (同名の**全**操作 — approve/reject/expire/invalidate/rollback/bless/bless-version — は lock 下でまずこれを収束させる): (a) **同じ操作の再試行 / 起動時 reconcile** は phase から再開して完了させる (hash がまだ一致し後発 reject が無ければ)。**再開前に参照物 (`new_target` の版・`old_target`/`old_artifact_hash` の版・`residue_path`・temp link) の存在と hash を再検証**し、欠損なら**保持している staging から再作成**、無理なら `reverted` で閉じて activity ERROR (I4) (b) **それ以外の操作が来た**とき (reject/expire/invalidate/別 hash の approve/rollback) は、reconcile が**切替を巻き戻す**: `old_kind='symlink'` → symlink を `old_target` へ 1 rename で戻す / **`old_kind='plain'` → 保全済み旧版 `.versions/<name>/<old_artifact_hash>` への正規 symlink を原子的に復元し、`residue_path` の残骸を掃除** / `old_kind='absent'` → live symlink を除去 → `reverted` (activity `switch_reverted`) → その後に本来の操作を適用する。**`GC_ROOTS` (唯一の定義、他節はこの名で参照)** = `plugin_versions` の行 ∪ live symlink の指す先 ∪ **非終端ジャーナルが参照する `new_target`・`old_artifact_hash` の旧版・`residue_path`・`temp_path`** (codex 7 周目 I4 / 8 周目 I4)
> 2. **後発決定の確認 (ⓓ) — key は `(name, content_hash)`** (D4 と同じ。codex 4 周目 I9): **同じ `(name, content_hash)`** へのより新しい決定 (reject) があれば、この承認は失効 — `apply_decision(status='invalidated')` (§4.3。backlog も同 tx で `observation`)。**同名で content_hash が異なる approval 同士は独立** (C の reject は B の pending approve を失効させない)。plugin 名全体を revoke する操作は設けない (YAGNI)。再試行で復活させない (D4 の順序規則)
> 3. **ハッシュ再照合 (ⓐ)**: 候補 (staging) を §4.2-3a と同じスナップショット検査に通し `content_hash` と `artifact_hash` を再計算、payload と一致しなければ承認は成立せず **pending のまま** + activity + 通知
> 4. **版ディレクトリの作成 (冪等)** (旧状態の確定と (plain なら) 5 の保全を**先に**済ませ、ジャーナル行を `preserved` で INSERT してから行う。完了後に `versioned`): `plugins/.versions/<name>/<artifact_hash>.tmp-<op_id>/` に 3 ファイルをコピー → 各ファイルとディレクトリを `fsync` → `rename` で `plugins/.versions/<name>/<artifact_hash>/` へ (既に同 artifact_hash の版があれば作らない)。失敗 → pending + 通知。**本番 symlink には触れていない**
> 5. **旧プレーン版の保全 (初回移行のときだけ、codex 3 周目 I1 / 4 周目 I6 / 7 周目 I5 — 手順上は 4 より先に実行する。ジャーナル行の INSERT より前)**: `plugins/<name>` が**プレーンなディレクトリ**なら、lock 下でその 3 本をスナップショット検査 → `artifact_hash_old` / `content_hash_old` を計算 → **`plugins/.versions/<name>/<artifact_hash_old>/` へコピー** (4 と同じ `tmp-<op_id>` → fsync → rename、冪等) → **bare 履歴へ「adopted pre-existing」として記録** (5.2 の plumbing、commit message に `adopted pre-existing content=<…> artifact=<artifact_hash_old>`。既に同 tree なら no-op) → **`plugin_versions` に `(name, artifact_hash_old, content_hash_old, origin='adopted', approval_id=NULL)` の行を短い tx で書く** (`GC_ROOTS` の一員 — 5.1 ジャーナル段落)。これで**旧プレーン版は版ストア・履歴・DB に保全される** (GC/履歴/復旧用)。**adopted のみでは rollback 対象にならない** — 戻したければ `plugin bless-version <name> <artifact_hash_old>` で再ゲート + 明示承認してから rollback/切替する (5.1 レイアウト、codex 6 周目 I5)
> 6. **履歴への記録 (ⓑ) — 新版ディレクトリの 3 本から、`<name>/<file>` のパスで** (完了後に switch ジャーナル `recorded`): 下記 5.2 の blob-level plumbing。失敗 (git 不在 / detached / identity 無し / CAS 上限) → **pending のまま + 通知。本番には一切触れていない**
> 7. **切替 (原子操作 1 回、`switch_required=1` のときだけ)** (直前に switch ジャーナル `switched` を短い tx で書く — FS 効果より先): temp symlink `temp_path` = `plugins/.<name>.link-<op_id>` (→ `.versions/<name>/<artifact_hash>`) を作り、`plugins/<name>` の現在の形で分岐:
>    - **不存在 / symlink**: `os.rename(temp, plugins/<name>)` (symlink → symlink の rename は原子的)
>    - **プレーンなディレクトリ (初回移行)**: `renameat2(AT_FDCWD, temp, AT_FDCWD, plugins/<name>, RENAME_EXCHANGE)` (ctypes syscall — `core/landlock.py` と同じ流儀。Linux 3.15+、同一 FS) で **temp symlink と live ディレクトリを 1 回で交換**する。交換後、temp パスに来た旧ディレクトリを **5 で作った `.versions/<name>/<artifact_hash_old>/` と照合し、一致すれば削除**、不一致なら残して activity ERROR。**`RENAME_EXCHANGE` 非対応 (kernel/FS) は初回移行を fail closed** — その plugin の approve/bless は pending (理由 `exchange_unsupported`) に留める
>    - 切替後 `content_hash(plugins/<name>)` (symlink を辿る) を再計算し payload と一致することを確認 (不一致 → 旧版へ向け直し pending)
> 8. **`apply_decision(status="approved")`** (§4.3 — approval 行 CAS + backlog `done` + **`plugin_versions` 行 `(name, artifact_hash, content_hash, origin='approval', approval_id)`** + **switch ジャーナル `decided`** を 1 tx で。決定は最後)。ここで初めて次回起動の `approved_plugins()` に載る
> 9. staging を削除、lock 解放
>
> **却下・期限切れ・失効**: 同じ plugin `flock` 下で、同名の未完 switch ジャーナルがあれば先に巻き戻し (1 の (b)) → `apply_decision` (backlog を `observation`) + staging 削除。ジャーナルが無ければ `plugins/<name>`・版・履歴には触れない。
>
> **版の台帳 `plugin_versions` (codex 4 周目 I6)**: 新テーブル `plugin_versions(name TEXT NOT NULL, artifact_hash TEXT NOT NULL, content_hash TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('approval','adopted')), approval_id INTEGER, created_at TEXT NOT NULL, PRIMARY KEY(name, artifact_hash))`。決定/adopt と**同じ tx** で書く。**rollback はこの表に行を書かない** (codex 8 周目 M2 — 履歴は switch ジャーナルと activity)。GC の root 集合は **`GC_ROOTS`** (§5.1 ジャーナル段落の定義) を参照する。**この表は保管/GC の provenance であって admission の根拠ではない** (codex 5 周目 I5) — `plugin rollback` の許可判定は D4 (最新決定が approved の `(name, content_hash)`) と版の存在で行い、**`origin` は最初の provenance のまま更新しない** (rollback 履歴は `plugin_switch_journal` と activity だけが記録先。`INSERT OR IGNORE` で消える行を監査に使わない — codex 6 周目 M3)。
>
> **bless (人間の候補を即時承認) / bless-version (保管版の再承認)** — **approve と同じライフサイクル: 候補 → ゲート → pending 行 + 証跡 → ジャーナル付き昇格 → 決定** (codex 9 周目 I1): 同じ `flock` を取り、同名の未完ジャーナルを収束 → 候補 (bless: `plugins/<name>` のプレーン dir または `plugins/_human/<name>/`。**symlink 版は拒否 → `materialize`** / bless-version: `.versions/<name>/<artifact_hash>`) をスナップショット検査 → 両 hash → **kind 別の全ゲートを通常 submit と同じ経路で通す** (§4.2-3 のコードゲート = AST + Landlock pytest + hash 不変、**kind=strategy なら §4.2-4 の in-sample ≥ 30・固定 holdout・baseline 添付まで**。どれか欠ければ何も作らない — codex 7 周目 I7) → **FS 効果より前に 1 つの短い tx で**: pending の approval 行 (`kind=plugin`、payload はゲート行 id・baseline/holdout・settings hash を含む完全形) + ゲートの `backtest_runs` 行 + switch ジャーナル行 (`preserved`、`approval_id` は**この時点で既知**) を作る → 旧状態の確定は tx 前に済ませておく (plain の旧版保全 5 は bless では旧=新内容なので不要) → 版ディレクトリへコピー (4、冪等、temp は `<artifact_hash>.tmp-<op_id>`) → 履歴記録 (6) → 切替 (7。プレーンなら `RENAME_EXCHANGE`、symlink なら rename) → 照合 → **その pending 行に `apply_decision(approved)`** (8、approve と同一経路)。**既に D4-approved の同一 artifact へ戻す rollback だけがゲート再実行を要しない**。**既存のプレーンな `plugins/<name>/` は起動時 reconcile では触らない** — 次の bless まではそのまま動く (discover は両形を読む)。
>
> ### 5.2 記録手順 — bare リポジトリ + 専用 index + blob-level plumbing (プラン 9 D6 の確定形を、bare + 版ディレクトリ出所に合わせて組み替え)
>
> - **初期化**: `plugins/.history.git` が無ければ親が `git init --bare` (lazy)。親リポジトリとは独立、gitlink は発生しない (`.gitignore` の `/plugins/` で親から見えない)。**ワークツリーは無い**ので `git add` の対象も `git status` の対象も存在しない
> - **ポーセリン (`git add` / `git commit`) は使わない** — bare にはワークツリーが無く、そもそも `git commit -- <path>` はワークツリー内容を取り直すので検証後の書き換えを拾う (codex 4 周目 C1 / 3 周目 M1)。**blob を版ディレクトリのファイルから直接作り、専用 index に `<name>/<file>` のパス名で置く**
>
> ```
> GIT_DIR=plugins/.history.git  (全コマンド共通。GIT_WORK_TREE は設定しない)
> src  = plugins/.versions/<name>/<artifact_hash>/
> ref  = git symbolic-ref HEAD        # unborn でも HEAD が指す ref 名は返る。init.defaultBranch に依存しない
> old  = git rev-parse --verify <ref> # 失敗 = unborn (初回)
> b_py = git hash-object -w <src>/plugin.py ; b_cfg = … config.yaml ; b_test = … test_plugin.py   # 3 blob を object DB へ
> GIT_INDEX_FILE=<tmp> git read-tree <old>                # unborn なら read-tree --empty
> GIT_INDEX_FILE=<tmp> git rm --cached -r -q --ignore-unmatch -- <name>/     # 旧版の同 prefix エントリを専用 index から外す
> GIT_INDEX_FILE=<tmp> git update-index --add --cacheinfo 100644,<b_py>,<name>/plugin.py   (config.yaml / test_plugin.py も同様)
> GIT_INDEX_FILE=<tmp> git cat-file blob :<name>/plugin.py / :<name>/config.yaml / :<name>/test_plugin.py → bytes → content_hash_bytes() / artifact_hash_bytes()   # 検証は index の blob に対して。payload と一致しなければ中止 (pending)
> GIT_INDEX_FILE=<tmp> git write-tree     # → tree。unborn でなく tree == <old>^{tree} なら「変化なし」= commit を作らず成功
> git commit-tree <tree> [-p <old>] -m "approve <name> content=<content_hash> artifact=<artifact_hash> (approval #id)"   # unborn なら -p なし
> git update-ref <ref> <new> <old>        # CAS。unborn は <old> = 空文字。失敗したら頭からやり直し (有限回、超過は pending のまま)
> ```
>
> - **`_staging/` `.versions/` を stage する経路が構造的に無い** (index に入るのは `update-index --cacheinfo` で明示した `<name>/<file>` の 3 本だけ)
> - **`symbolic-ref` の失敗は終了コードで区別**: 1 = detached HEAD (人間が履歴操作中 → 待てば直る) / 128 = リポジトリ障害 (運用者の対処が要る)。どちらも pending だが activity と通知の理由を分ける
> - **CAS 無しの `update-ref` は不可**。代替としてリポジトリ単位 lock で全 git 操作を直列化してもよい (実装はどちらでも)
> - **hash の bytes 版**: `plugin/loader.content_hash(Path)` を `content_hash_bytes(plugin_py, config_yaml)` の薄いラッパにする (定義 `sha256(b"plugin.py\0"+p+b"\0config.yaml\0"+c)` は**不変**)。`artifact_hash_bytes(plugin_py, config_yaml, test_plugin)` を新設 (§4.2-3b)
> - **commit identity はサービスが供給**: `GIT_AUTHOR_NAME/EMAIL` / `GIT_COMMITTER_NAME/EMAIL` = `agentic-fx <noreply@localhost>` を env で渡す (利用者の git 設定に依存させない。無いと `commit-tree` が失敗し永久 pending — codex 6 周目 I2)
> - git サブプロセスの env は最小 (`PATH`, `HOME` は実ホームでよい — 親プロセス、Landlock 外)。`GIT_DIR` を明示し `GIT_WORK_TREE` を設定しない — 親リポジトリの `.git` を誤って触らない
>
> ### 5.3 git と SQLite は原子化できない — 収束性で担保 (codex I5)
>
> - 「approved になっている plugin は必ず commit 済み」の**一方向**だけを不変条件にする。逆は保証しない
> - **git の失敗・版作成の失敗は稼働中の版を決して壊さない** (どちらも切替に先行するため)。**切替は原子操作 1 回** (rename または exchange) なので「live が無い瞬間」は無い。起こりうる中間状態は次の 3 つで、いずれも pending に統一され、再試行で収束する:
>   - **(a) 版ディレクトリ作成済み・未記録・pending**: 旧版無傷。再試行は同 artifact_hash の版が在るので作り直さず、git から続く。`.tmp-*` の残骸は reconcile が削除
>   - **(b) 記録済み・未切替・pending**: 旧版無傷。再試行は git が tree 一致で no-op、切替をやり直す
>   - **(c) 切替済み・pending** (`decide` の DB 書込だけ失敗 — 窓は極小): live は新版を指すが approved でないため**新版は load されず、旧版も load されない** (旧版は `.versions/<name>/<旧 artifact_hash>/` と履歴に残る)。起動時 reconcile (`approved_plugins()` より前) と次の承認操作が拾って decide を完了させる。**人間が待てなければ、未完ジャーナルの reconcile が `switched` の復旧規則で旧状態へ revert する経路 (§5.1-1 (b)) を使う** — `plugin rollback` は D4 approved + exact artifact の版にしか向けられないのでここでは使わない (codex 9 周目 M2)
> - **再試行の 3 契機**: ①起動時 reconcile ②次の承認操作 ③シェル `approval retry <id>`。**scheduler tick にぶら下げない**。再試行は手順を頭から流す (lock → ⓓ → ⓐ → 版 (冪等) → 旧版保全 (プレーンのときだけ、冪等) → git (tree 一致なら no-op) → 切替 (既に新版を指していれば no-op) → `apply_decision`)
> - **起動時 reconcile の位置**: `init_db` と中断 Mission の回収より後、**`approved_plugins()` より前** (後だとその起動では復旧した承認がロードされない — codex 4 周目 I3)。**順序は「ジャーナルの収束が先、孤児掃除が後」** (codex 7 周目 I4 — 再開材料を先に消さない): まず ⓪**未完の `plugin_switch_journal` (非終端 phase) を phase から完了または旧状態へ巻き戻して終端する** (5.1-1 の規則。参照物の存在と hash を再検証してから) → その後に ①§2.3 の孤児 staging 掃除 (未完ジャーナルが参照する staging は残す) ②`.versions/<name>/*.tmp-*` の削除 ③**`GC_ROOTS` (§5.1) に含まれない**版ディレクトリの削除 (orphan。approval payload だけを root にしない — プラン 10 以前の approval は `artifact_hash` を持たず、adopt した旧版が消える — codex 4 周目 I6) ④**temp link `.<name>.link-*` の残骸削除 (`GC_ROOTS` の `temp_path` は除く)** ⑤**exchange 後に temp パスへ来た旧プレーン dir の残骸** — その dir の `artifact_hash` を計算し、`.versions/<name>/<その hash>/` が無ければ **同じ adopt (5.1-5) を再開** (版作成 + 履歴記録) してから削除、既に版があれば一致確認して削除、不一致なら ERROR で残す ⑥(⓪で済み) ⑦`plugins/<name>` が dangling symlink なら activity ERROR (人間の操作を待つ。自動では向け直さない) を行う。git 不在・reconcile 失敗はサービス起動を止めない (警告 + pending のまま)
> - **git 不在**: 起動時検査で警告 (SQLite ≥3.35 assert と同じ扱い)。plugin 承認だけが成立せず、取引は動く
>
> ### 5.4 資金保護への非波及
>
> 承認スレッド (シェル / 将来 API) と起動シーケンスでのみ実行。**scheduler スレッドから git サブプロセスが呼ばれないこと**を回帰テストで固定 (呼ばれたら fail するシーム — D3' の通知と同じ形。「lock 内か」でなく「どのスレッドか」で検査)。`core_lock` は触らない。
>
> **変異 (§5)**: 決定時ハッシュ再照合を削除 / 照合対象を index の blob から候補置き場のワークツリーへ戻す (TOCTOU 復活) / 専用 index をやめ `git add` + `git commit -- <path>` (**killer = 検証後に staging の plugin.py を書き換えてから commit させ、commit 内容が検証済みの内容であることを assert**) / **git 記録より先に切替する (killer = git 失敗を注入 (identity env 除去 / detached HEAD) して承認させ、`plugins/<name>` が旧版を指したまま `approved_plugins()` に残ることを assert)** / **プレーン初回移行を「旧を退避 → 新を置く」の 2 段 rename にする (killer = 1 段目の直後にプロセスを落とし、起動時に live が欠けている)** / **`RENAME_EXCHANGE` 非対応で 2 段にフォールバックする (→ fail closed pin)** / **版キーを `content_hash` にする (killer = code/config 同一・test 違いの 2 候補を順に承認し、履歴に 2 つの test が別 commit で残り、両版に rollback できること)** / **`flock` を落とす (killer = 別プロセスの bless と同時に走らせ、両者が互いの版を壊さないこと)** / **履歴を非 bare にしてワークツリーを持つ (killer = 承認後に `git status` が clean であること / `git restore .` 相当の操作で live symlink が実 dir に置換されないこと)** / 切替後の hash 再検証を落とす (killer = swap 中に版ディレクトリを差し替え、旧版へ戻され pending のままであること) / 切替先を `resolve()` で検査する (§4 と共有) / `_staging/` `.versions/` が index に入る (→ 記録後の tree に無い pin) / 旧版の消えたファイルが index に残る (→ `git rm --cached` 落とし) / 空 commit 判定を `git diff --cached` に戻す / commit と decide の順序を入れ替える / commit 失敗で承認を成立させる / 後発 reject の確認を削除 / 起動時 reconcile を `approved_plugins()` の後に置く / reconcile が dangling symlink を勝手に向け直す (→ ERROR に留める pin) / git を scheduler スレッドから呼ぶ / CAS 無し update-ref (→ 並行承認で先発 commit が消える) / identity env を落とす (→ 空 git config 環境で永久 pending) / `fsync` を落とす (→ 実装計画の fault-injection、設計では要求のみ) / bless が Landlock ヘルパでなく `_default_pytest_runner` を使う (→ §4.2-3d の pin) / discover が symlink を辿らない (→ 承認直後に plugin が消える pin) / approve 時に backlog を `done` にしない (→ §4.3 pin) / **初回移行で旧プレーン版を保全せずに exchange する (killer = 初回 approve 後に旧版が `.versions` と履歴に在り、`plugin bless-version <name> <artifact_hash_old>` の全ゲート + approved 決定を経て切替できること。直接 rollback は fail closed で live 不変)** / **旧状態の確定・保全をジャーナル行の後に回す (killer = `preserved` 行の直後に落として old identity が復元できること)** / **`switched` の復旧を「常に完遂」にする (killer = live が old のまま落ちた行が `reverted` で閉じ、live が変わらないこと)** / **ジャーナル参照物を `GC_ROOTS` に含めない・掃除をジャーナル収束より先に行う (killer = `versioned` で落として起動後に new 版が消えず再開できること)** / **bless-version が strategy ゲートを飛ばす (killer = 取引 10 件の strategy 版が bless-version で承認されないこと)** / **adopt が `plugin_versions` 行を書かない (killer = 初回 approve → 再起動 → reconcile 後も旧版ディレクトリが残ること)** / **reject/expire が plugin flock を取らない (killer = approve の切替直後に reject を差し込み、DB rejected と live 新版が食い違わないこと = reject が lock 待ちで approve 完了後に `AlreadyDecidedError` になる)** / **後発決定 key を name だけにする (killer = 同名別 hash の C を reject しても B の pending approve が生きていること)** / **switch ジャーナルを書かずに切替する (killer = 切替直後にプロセスを落とし、別プロセスの reject を先に走らせても、起動後に live が旧状態に戻り DB が rejected で一致すること)** / **adopted のみの版へ rollback を許す (killer = 承認の無い版への rollback が fail closed で、live が変わらないこと。`bless-version` 後は成立すること)** / **rollback を `apply_decision` 経由にする (→ approval 行が変わらない pin)** / **rollback を switch ジャーナル無しで行う (killer = rename 直後に落とし、起動後に activity と live が一致すること)** / **`old_kind` を持たず NULL で plain と absent を混同する (killer = plain 初回移行の巻き戻しで adopted 旧版への symlink が復元され、absent では live が除去されること)** / **同名の未完ジャーナルを 2 件許す (killer = 別 hash の approve が先行 approve の切替を追い越さないこと)** / **rollback が `plugin_versions` に行を書く・`origin` を上書きする (→ provenance 不変 pin)** / discovery が `PluginMeta.path` を symlink のまま持つ (→ approve 後に稼働中サービスの sandbox が旧版を実行し続ける pin)。
>
> ---
>

### A.2 (b) 縮小前の §4.2-6 risk_gate 提案の親側評価

> 6. **レポート (親だけが書く)**: `artifact.type=report`、およびゲート不合格・評価不能・observation・敗者のとき、`reports/` に書く。**`proposal_kind=risk_gate` の report は harness 所有の評価が成立したときだけ report として成立する** (codex 4 周目 I7 / 5 周目 I3 / 6 周目 I7 — 本体設計書 §6「risk gate パラメータの提案は approval を出さないが、バックテスト必須・最低取引数・**out-of-sample 比較**の規律はレポートの根拠にも同様に適用」):
>    - **提案の能力境界 (strict schema)**: `risk_gate_proposal.params` に許す key は **intent 単位で risk gate / sizing が適用するもの**に限る — `rr_min` / `risk_per_trade_pct` / `swing_risk_factor` / `limit_deviation_pct` / `limit_expiry_max_h` / `max_slippage_pct` / `pair_rules.<pair>.{sl_distance_min_pips, sl_distance_max_pips, assumed_spread_pips}` (現行 `RiskSettings` / `PairRule`、`config.py:23-51`。値域は同じ validator)。**ポートフォリオ水準の key (`max_positions` / `max_total_risk_pct` / `max_leverage` / `daily_loss_limit_pct` / `drawdown_kill_pct` / `friday_swing_cutoff_ny` / `commission_per_lot`) は `unsupported`** — 単一 strategy の再生では評価できない (ポートフォリオ harness はプラン 10 に無い、起票 §10)。**未知 key・unsupported key・値域外 → observation `unsupported_param`** (report_failed ではない)。**`affected_pairs` を params から導出**: グローバル key (`rr_min` 等) が 1 つでもあれば `settings.pairs` 全部、`pair_rules.<pair>.*` だけなら参照された pair の集合。**`proposal.pairs` は `affected_pairs` と一致しなければならない** (不一致 → observation `pairs_mismatch`)。未知 key・unsupported key・値域外 → `unsupported_param` (codex 8 周目 I9)。override は現行 `RiskSettings` へ **deep-merge してから `RiskSettings`/`PairRule` の validator で全体検証** (不合格 → `unsupported_param`) (codex 7 周目 I8)
>    - **評価単位 = 承認済み strategy × pair の単一 strategy 再生**。**`affected_strategies = {meta | kind=strategy ∧ meta.pairs ∩ affected_pairs ≠ ∅}`** (codex 9 周目 I5)。空 → 評価不能 observation。cell = 各 affected strategy × (`meta.pairs ∩ affected_pairs`)。**affected strategy ごとに、baseline と候補の両方で** eligible pair の in-sample 合計取引数 ≥ 30 (`EVALUABLE_MIN_TRADES`) を要求 — **どちらか一方でも < 30 の strategy が 1 つでもあれば proposal 全体を `insufficient_trades` observation**。affected でない strategy はレポートに `unaffected` として列挙する (codex 7 周目 I8 / 8 周目 I9) (既存 `run_in_sample` / `run_holdout_gate` に、risk override を適用した settings スナップショットを渡すだけ — 新経路は作らない)。baseline (現行 risk 設定) と候補 (override) の両方で ①in-sample ②**ハーネス所有の固定 holdout** を回す。**全 affected strategy が baseline・候補とも ≥ 30 なら in-sample と holdout (OOS) の baseline/候補比較を strategy×pair ごとにレポートへ添付**して成立、そうでなければ「評価不能」→ report を書かず observation (`insufficient_trades`)。`affected_strategies` が空なら評価不能。行は Tx-2 で `backtest_runs` に**既存 identity (plugin_ref / content_hash / kind / pair / timeframe)** で保存 (`scope=in_sample` / `holdout_gate`, `issued_by=harness`)。**holdout の内容は worker 出力・RPC 返却・次回注入コンテキストのどこにも出さない** (遮断 8 — 親専有の report と監査行だけ)。`core` / `research` の report は評価を要さない

### A.3 (c) 簡素化前の §3.1 wave/slot 再開機構 (`started_at`・全 failed wave の削除・claimed の同 period 再開)

> - **wave と slot の durable 状態機械 (codex 4 周目 I4 / 5 周目 I1 / 6 周目 I1 / 7 周目 I1・I2)**: 新テーブル `improve_waves(period_key TEXT PRIMARY KEY, created_at TEXT NOT NULL, expected INTEGER NOT NULL)` と **`improve_wave_slots(wave_period_key TEXT NOT NULL REFERENCES improve_waves(period_key), k INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('reserved','claimed','running','done','failed')), mission_id INTEGER, spawn_attempts INTEGER NOT NULL DEFAULT 0, started_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(wave_period_key, k))`** (`started_at` は `claimed→running` の commit で埋まり、以後不変 — codex 8 周目 I1)。手順: ①`M = min(improve.parallel, 空きスロット数)`。**M=0 なら何も書かない** (period 非消費、次 tick で再試行) ②**1 つの短い tx** で `INSERT OR IGNORE INTO improve_waves(period_key, created_at, expected) VALUES (?, ?, M)` (`rowcount=1` = 起動権) + **同じ tx で slot 行 `k=0..M-1` を `reserved` で INSERT** ③**3-way 起動 (I1)**: `reserved → claimed` (§4.1 Tx-0 と同じ tx で CAS `UPDATE … SET status='claimed', mission_id=?, spawn_attempts=spawn_attempts+1 WHERE … AND status='reserved'`) → worker spawn → worker `ready` → **親が短い tx で `claimed→running` を commit** → **親が `go` フレームを送る** → worker が Mission を開始 (`go` 前は agent/ツール実行ゼロ。`go` が来なければ副作用ゼロで終了)。`running` の commit と `go` の間で親が落ちても、worker は `go` を待って終了し、slot は `running` のまま → 起動時回収で `failed` (再実行しない) — 「実行済み Mission の再実行」は起きない ④**分担は負荷分散のヒント**: 親は prepare で**そのときの** `open|observation` 集合から `id % M == k` で `allowed_backlog_ids` を計算し RunContext に入れる (**永続化しない**。再開した slot は再計算する — codex 7 周目 I3、簡素化側)。正しさは §4.1 Tx-1 の CAS が担う (敗者 → observation)。**手動 M=1 は全 id** ⑤**終端の直積 (I2)** — 下表 ⑥**部分 submit・実行 0 件の wave**: pre-ready の失敗は slot を `reserved` に戻して同 wave で再 claim、**`spawn_attempts` は claim ごとに +1、失敗時に `< 2` なら `reserved` (初回 + 再試行 1 回)、それ以外は `failed`**。**wave の全 slot が `failed` で、かつどの slot も `started_at` を持たない (= 一度も `running` になっていない) なら、その瞬間 (と起動時) に wave 行 + slot 行を削除 = period 非消費** (次 tick で再 CAS)。**1 つでも `started_at` を持つ slot があれば、全部が `failed` で終わっても wave は残る = period 消費** (codex 8 周目 I1) ⑦`submitted` は列で持たず `status IN ('running','done','failed')` の slot 数から導出 ⑧**手動 `improve` は wave/slot 行を作らない**
>
>   | 事象 | slot | mission | run | 同一 tx か |
>   |---|---|---|---|---|
>   | Tx-0 (prepare) | reserved→claimed | INSERT (`running`) | INSERT (CREATED) | 1 tx |
>   | spawn 失敗 / `ready` 前 crash・timeout | claimed→reserved (失敗時点で `spawn_attempts` < 2 = 初回のみ) または failed (再試行後) | `failed` | FINISHED (`result=NULL`) | 1 tx |
>   | `ready` 受信 | claimed→running | — | — | 短い tx、その後 `go` |
>   | `ready` 後の全終端 (completed / failed / timeout / max_turns / 親の出力検査不合格 / Tx-2 / Tx-2 補償 / shutdown) | running→done (completed かつ Tx-2 成功) または failed | 終端 status | FINISHED | **1 つの短い tx** — 全経路が単一ヘルパ **`finish_improve_mission(conn, *, mission_id, run_id, slot_key\|None, mission_status, run_result, backlog_transition, commit=False)`** を通る (Tx-2 本体の末尾、または補償 tx)。手動 one-shot は `slot_key=None` (codex 8 周目 I2) |
>   | 起動時: `claimed` で worker 無し | claimed→reserved → 同 period で再 claim | interrupted | FINISHED | `recover_interrupted` と同一 tx |
>   | 起動時: `running` で mission interrupted | running→failed (再実行しない) | interrupted | FINISHED、backlog `observation:interrupted` | 同一 tx |
>   | wave の全 slot `failed` かつ全 slot `started_at IS NULL` | wave + slot 削除 (period 非消費) | — | — | 短い tx (その瞬間 / 起動時) |
>   | wave の全 slot `failed` だが `started_at` を持つ slot がある | 残す (period 消費) | — | — | — |
>
