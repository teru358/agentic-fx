# Phase 2 プラン 10 設計書: ClaudeRunner + CodexRunner + 戦略改善 loop

- 日付: 2026-08-16 (初版)
- ステータス: **ユーザー承認済み (2026-08-16)、codex 設計レビュー反復中**
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

**設計しないもの (明記)**: news ソース提案の承認経路 (プラン 11 Task 6 へ) / REST・Discord からの承認 (プラン 11) / 外向きリクエスト予算の**全体**設計 (起票のまま。本書は改善ループの経路に閉じた予算だけ §6) / claude のローカル LLM 駆動 (起票 §10) / trade_intents 保持・Notifier 戻り値契約ほかプラン 9 §6 の起票 (§10 に引き継ぐ)。

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

1. **子プロセス起動 (I8: multi-thread の worker で Python `preexec_fn` を使わない)**: mission_worker は MCP/RPC dispatcher スレッドを持つので、`fork` 直後の child が他スレッドのロックを継承して exec 前に止まる経路がある。したがって CLI は **最小 launcher** 経由で起動する: `Popen([sys.executable, "-c", "<launcher>"], cwd=workdir, env=<完全指定>, stdin=<open('/dev/null', O_RDONLY)>, stdout=PIPE, stderr=PIPE, start_new_session=True)`。launcher は単一スレッドの新 interpreter で `prctl(PR_SET_PDEATHSIG, SIGKILL)` → `os.execv(cli_argv)` を行うだけ (`start_new_session=True` は Popen 側で済ませ、launcher は setsid しない)。python の EXECUTE は closure に在る (§2.2)。**launcher を選ぶ理由**: PDEATHSIG は child 側でしか設定できず、「dispatcher スレッド起動前に spawn する順序契約」は再試行や 2 本目の CLI 起動で破れやすい。**`subprocess.DEVNULL` は使わない** — Landlock 下で `/dev` が ro だと Python は `/dev/null` を `O_RDWR` で開けず失敗する (probe §5-③)。`/dev` は §2 で rw にするが、`CliRunner` 単体テストは ro 環境でも動く形にしておく
2. **scratch home と認証コピー — コピーするのは Landlock 外の親 (C1)**: `WorkerRunner` (親プロセス、Landlock 外) が Mission workdir を **0700** で作り、その直下に `home/`・`tmp/`・`cfg/` を作り、**spawn より前に**認証ファイルを `cfg/` へコピーする — codex: `~/.codex/auth.json` → `cfg/auth.json` / claude: `~/.claude/.credentials.json` → `cfg/.credentials.json`。コピー前に原本を検査: 通常ファイル (`O_NOFOLLOW`)・所有者 == 実 uid・group/other に権限なし (mode `0600` 系)・サイズ ≤ 64 KiB。不合格は Mission failed (reason 安全化)。ソースの場所は `runner.codex.auth_file` / `runner.claude.credentials_file` (既定は上記。`~` 展開は親側)。**`CliRunner` (worker 内) は実 `~/.codex` / `~/.claude` を一切読まず、`workdir/cfg` だけを使う** — Landlock 適用後の worker からは原本が `EACCES` なので構造的にも読めない。**コピー先が更新されても元へ書き戻さない** (トークンリフレッシュが起きた実行では実ファイルが古くなる方向のドリフトが予想される — 起票 §10)。実ファイルは Landlock allowlist に**入れない** (probe P4: strace で実 `~/.codex` への openat 0 件)。実 HOME に関する情報は子に一切渡さない
3. **env の完全指定** (継承しない。`_mission_worker_env` の allowlist を拡張する形): `PATH=/usr/bin:/bin` (codex が shell snapshot で `/bin/bash` を起動する — probe §3) / `HOME=<workdir>/home` / `TMPDIR=<workdir>/tmp` / `CODEX_HOME=<workdir>/cfg` or `CLAUDE_CONFIG_DIR=<workdir>/cfg` / `PYTHONPATH` `PYTHONSAFEPATH` (既存)。値は親が workdir を作った後に確定するので **Popen の env に直接入れる** (handshake ではない)。**`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `AFX_*` / データ資格情報は構造的に存在しない**。pin テスト: 起動 argv と env をキャプチャする fake Popen で「鍵の名前を含む変数がゼロ」を assert
4. **timeout と子孫の所有**: CLI は **`start_new_session=True` で自分専用のセッション/pgid** に置き (`killpg` が mission_worker 自身に当たらないように)、launcher (項 1) が `prctl(PR_SET_PDEATHSIG, SIGKILL)` を設定する (worker が死ねば CLI も死ぬ)。`mission.timeout_sec` を壁時計で監視し、超過で CLI セッションへ SIGTERM → `runner.cli_terminate_grace_sec` (既定 10) → `os.killpg(cli_pgid, SIGKILL)` → `status="timeout"` の result を返す。mission_worker 既存の SIGTERM ハンドラ (transcript flush) も **終了前に CLI セッションを kill** する。外側 `WorkerRunner._escalate_kill` (worker の pgid への killpg) は不変。**残余リスク (§2.4・§10)**: `setsid()` を呼ぶ孫は CLI の pgid から逃げる。完全な包含には cgroup v2 が要る (起票)。probe P9 は同一 pgid + synthetic 連鎖の範囲でしか測っていない
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
- `--setting-sources ""` でも CLI 組み込みの tools/slash_commands は残る (probe §4「限定つき」)。ユーザー設定由来でないと解釈するが、積極確認は未了 → 実装計画の実測項目 (組み込み以外が混入していないことを `init` イベントの `mcp_servers: []` で pin)
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
    bin: claude                                # PATH 解決。絶対パス可
    credentials_file: ~/.claude/.credentials.json
  codex:
    bin: codex                                 # PATH 解決 (node ラッパ可) / vendor native 直指定可
    provider: chatgpt                          # chatgpt | llama_swap
    auth_file: ~/.codex/auth.json
  cli_terminate_grace_sec: 10.0
```

- `RunnerChoice.backend` の正規表現を `^(local|claude|codex)$` へ。`RunnerSettings` に `claude: ClaudeCliSettings` / `codex: CodexCliSettings` を追加 (`_Strict`)。`settings.yaml.example` を同期
- **起動時検査** (`service.build_app` の既存 `_check_llama_swap` の隣): 選択された backend について ①`bin` が解決でき実行可能 ②`<bin> --version` が 0 で返る (rlimit なし・親プロセス側で) ③認証ファイルが存在する ④codex+chatgpt は `chatgpt_subscription_active_until` を読んで 7 日以内なら WARNING、過ぎていれば ERROR。**①②③のいずれかを欠けば `build_app` は起動拒否 (fail closed)** — Landlock 不可時の improve 起動拒否と同じ扱い。backend=local の環境では一切走らない
- **`codex_bin` の既定は PATH の `codex`** (nvm の node ラッパ)。ラッパ経由だと node の EXECUTE も要るため、`_bootstrap_improve_profile` は `bin` を `shutil.which` で解決した**実体のディレクトリ**を execute allowlist に入れる (ラッパなら node のディレクトリも)。実装計画で「node ラッパ経由の完走」を実測する (probe は vendor native 直指定で測った)

### 1.5 契約 — 揃えるもの・揃えないもの

| 項目 | 契約 |
|---|---|
| 終端 status | 4 値 (`completed` / `failed` / `timeout` / `max_turns`)。3 実装とも同一の契約テストスイートに合格する (**運用時に選ばれるのは 1 つ**。スイートは fake CLI スクリプト = 決められた JSON を吐く python で回し、実 LLM を呼ばない) |
| `reason` | spec ② §4.3 と同じ (安全化済み・単一行・上限・外部応答本文を生で入れない) |
| `output_schema` | 3 実装とも `jsonschema` で検証し、不適合は `failed` |
| `timeout_sec` | 壁時計。**常に優先** |
| **`max_turns`** | **意味は runner ごとに文書化し、揃えない**: Local = LLM 呼出回数 (現行)。Claude = CLI `--max-turns` (structured output は 1 ターンで終わらず `num_turns:3` — probe P2。改善 Mission の値は 200 以上を推奨し、`schedule`/prompt 側の既定を合わせる)。Codex = 上限なし → **`max_turns` 終端は到達不能**、timeout が唯一の上限 (docstring と契約テストで明示: codex は `max_turns` を渡しても無視する) |
| 課金鍵・個人設定 | 3 実装とも子 env に鍵が無い / claude・codex は scratch home のみ (§1.1) |

`runners/base.py` の docstring (`:17-28`) は「runner-neutral turn semantics」を上記の表へ改める。

### 1.6 ツール公開 — 3 backend で同一の registry

- registry は §3.4 の improve registry (`build_mission_registry("improve", …)`) 1 つ。**Local は in-process、claude / codex は MCP stdio シム経由**で同じ `ToolDef` を見る
- **MCP シム** `python -m agentic_fx.tools.mcp_shim <unix socket path>`: CLI が MCP サーバとして起動する子プロセス。**ツール本体は持たない** — MCP の `initialize` / `tools/list` / `tools/call` を JSON-RPC で受け、`workdir/afx.sock` (Unix ドメインソケット) 越しに mission_worker へ転送するだけ。mission_worker 側は専用スレッドで `tools/list` → `registry.openai_tools(allowed)` の変換、`tools/call` → `registry.execute(name, args, allowed)` を返す。**親への RPC が要るツール** (`run_backtest` / `analyze_corr`) は既存の `tool_rpc` パイプ (in-flight 1) に乗る — シム経由でも in-flight 1 は保たれる (CLI は tools/call を直列に呼ぶ想定だが、mission_worker 側で lock を取って直列化する)
- MCP ライブラリのサーバ側実装は使わない (依存を増やさない。stdio JSON-RPC 2.0 + MCP の 3 メソッドのみ)。プロトコル版は実装計画で CLI 2 種の要求を確認する
- staging ファイルツール (§3.4) の根は **handshake の `staging_dir`** (§2.2 I7) から導く。シム・registry・prompt の 3 箇所が同じ値を見る
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

**変異 (§1)**: env に `ANTHROPIC_API_KEY` を通す (→ pin が落ちる) / scratch home でなく実 `$HOME` を渡す / `--setting-sources ""` を落とす / codex の `--disable plugins…` を落とす (→ argv pin) / `--dangerously-bypass…` を落とす (→ shell 全滅、E2E で検出) / trade+claude の allowedTools に `Bash` を足す (→ pin) / `runner.trade.backend=codex` を通す (→ validator pin) / timeout 後に CLI セッションを killpg しない (→ 孫残留テスト) / CLI を worker と同一 pgid で起動する (→ 内側 killpg が worker 自身を殺し result が返らない fake テスト) / **worker 側 (Landlock 下) で認証原本をコピーしようとする (→ `EACCES` で Mission failed になる実プロセス pin。親コピーが正)** / `preexec_fn` で PDEATHSIG を設定する (→ dispatcher スレッド稼働中の spawn テスト、実装計画で hang 再現) / `parse_json_output` を通さず生 JSON を `json.loads` (→ フェンス付き fake 出力で failed になる契約テスト) / reason に stderr 全文を入れる (→ 安全化契約テスト) / `max_turns` 超過で `completed` を返す (claude fake の `num_turns` 上限テスト)。

---

## 2. improve worker profile の拡張 — 権限境界と不変条件 (裁定①)

### 2.1 不変条件 (受入条件の核。§7.1-1 で実プロセスに対して測る)

improve worker プロセスとその**全子孫** (claude / codex CLI・MCP シム・pytest・shell) について:

1. `data/` 配下 (DB・履歴・RAG) に読み書きとも到達できない (`open` / `listdir` / `truncate` / `exec` すべて `EACCES`)
2. 書き込み可能パスが **①候補置き場 `<root>/plugins/_staging/<mission_id>/` ②workdir (scratch home・`TMPDIR` を含む) ③`/dev`** に閉じる。**`reports/`・`plugins/` 全体・リポジトリ本体・`config/`・`policy/` には書けない**。`reports/` は案 A (R6) により**親だけが書く** — worker が書ける場所に親が予測可能な名前で書き込む構造を作らない (codex 1 周目 C1: 所有境界の単純化)
3. 従量課金経路が無い (env に鍵が無い — §1.1)
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
  | codex | `<runner.codex.bin>` の realpath の親。**node ラッパ (nvm) なら node の realpath の親も** (shebang `/usr/bin/env node` は `/usr/bin` で解決) | probe §2.1 は vendor native 直指定で完走。ラッパ経由は Task 13 で実測 |
  | local | 共通のみ (CLI・shell 系を入れない) | LocalRunner は subprocess を起こさない (worker 内 pytest のみ) |

  **起動時検査 (§1.4) が shebang を解決し、interpreter が closure に無ければ起動拒否 (fail closed)**。実装計画で closure を「1 要素 drop で何が壊れるか」の pin にする (実測して固定。推測で増やさない)
- **read_only 追加**: `/run/systemd/resolve` (外部 DNS。`/etc/resolv.conf` が symlink で Landlock は解決先で判定 — probe §2.1。存在するときのみ) / `/proc` (claude のみ。bun が panic して SIGABRT — probe §2.2)
- **`/dev` を read_only → read_write へ**。理由: `subprocess.DEVNULL`・bash のリダイレクト・pytest logging が `/dev/null` を書込オープンする (probe §5-③、P7)。**脅威分析**: rw マスクに `MAKE_CHAR` も `MAKE_SYM` も無い (現行 `_READ_WRITE_ACCESS`、`landlock.py:111-114`) のでデバイスノード・symlink の作成は不可。既存デバイスへの write は Unix パーミッション次第で `/dev/shm` (tmpfs) には書ける — `data/` 到達には寄与しない。`_ACCESS_FS_IOCTL_DEV` は従来どおり handled にしない (プラン 8 の判断を維持)
- **read_write 追加**: `<root>/plugins/_staging/<mission_id>/` **のみ**。`<root>/plugins/` 自体・`<root>/reports/` は入れない。**その値は handshake の新フィールド `staging_dir` (絶対パス) で 1 つだけ渡す** (codex 2 周目 I7): 親 `WorkerRunner` が 0700 で作成し、子は Landlock 適用前に ①dirfd で開き所有者 == uid・mode 0700・通常ディレクトリ ②パスが `<...>/plugins/_staging/<mission_id>/` の正規形に一致 (mission_id は handshake の値) を再検証してから、**Landlock の rw ルール・staging ツールの根 (§3.4)・prompt に埋めるパス (§3.2) の全てをこの 1 値から生成**する。root・DB パス・plugins_dir は従来どおり渡さない (`db_path=None, plugins_dir=None`)
- **`_assert_allowlist_excludes_data_dir` を拡張**: 入力を `read_only + read_write + execute` の全部にする。加えて**静的 pin**: `read_write_paths` の集合が `{staging, workdir, /dev}` と**一致**し、`<root>` 配下は `plugins/_staging/<id>` 以外を含まないこと (テストは `_bootstrap_improve_profile` が組む allowlist を dry-run で取り出して assert)
- **rlimit**: `child_fsize_mb=8` は維持 (codex は `--disable plugins…` で 664KB が最大 — probe §5-⑧)。**claude を rlimit 下で実 1 ターン回すのは未測** → Task 13 の実測項目 (超えるなら improve のみ `child_fsize_mb` を上げる)。**`RLIMIT_NPROC` は mission worker に適用しない** (現行どおり。plugin sandbox のみ)
- **env 追加** (`_mission_worker_env` の improve 分岐): `HOME=<workdir>/home`, `TMPDIR=<workdir>/tmp`, `CODEX_HOME=<workdir>/cfg` または `CLAUDE_CONFIG_DIR=<workdir>/cfg`。**ディレクトリ作成と認証コピーは親が spawn 前に行う** (§1.1-2)。子は何も作らない

### 2.3 候補置き場 (staging) の意味論

- worker が plugin を書ける唯一の場所は `plugins/_staging/<mission_id>/<name>/`。**稼働中の `plugins/<name>/` は読めるが書けない**
- 根拠: 承認済み plugin をその場で書き換えると content_hash が承認済みハッシュと不一致になり、**承認が下りるまでその plugin は `approved_plugins()` から消える** — 週次の改善が稼働中の指標を止める。staging なら承認までは旧版が生き続ける
- **plugin 名の正規形** (codex 1 周目 C2): `^[a-z][a-z0-9_]{0,63}$` — 単一パス成分。`.`・`..`・`/`・絶対パス・大文字・ハイフンを含まない。**出力 schema (§3.5) と親側 (§4.2-1) の両方で検証**する。候補パス `staging/<name>` と切替先 `plugins/<name>` の検査は **`resolve()` を使わない** (codex 2 周目 I1 — 承認済み plugin は symlink なので resolve すると `.versions/` 配下になり、二度と更新できなくなる): 字句上の単一成分検査 + それぞれの dirfd 基準の `lstat` / `openat(O_NOFOLLOW)` で、最終成分が **{不存在 / 通常ディレクトリ (プレーン、初回移行前) / 正規形の相対 symlink}** のどれかであることだけを見る。symlink の場合は**リンク先文字列**が `.versions/<name>/<artifact_hash>` の正規形 (`^\.versions/<同じ name>/[0-9a-f]{64}$`) に一致することを確認する (辿らない)。同じ正規形を `afx plugin bless` / `submit` の CLI と `plugin/loader.discover` にも適用する — **正規形に反する既存の plugin ディレクトリは discover が WARNING を出して skip する** (ロードされなくなる。移行は人間が rename)
- **`plugin/loader.discover` の列挙条件に「先頭が `_` または `.` のディレクトリを除外」を追加する** (codex 1 周目 M3: 現行は全子ディレクトリを列挙し、3 ファイル欠落で**偶然** skip されているだけ。`_staging/`・`.versions/`・`.locks/` (§5) を予約する)。`_reject_unexpected_py_files` の走査も同様。**Task 5 の受入 pin**
- ライフサイクル: 親が Mission 起動前に空 dir を作る → worker が書く → commit 相 (§4) でゲート (候補は**読取専用のスナップショット**として扱う) → 承認申請を出した候補は**決定まで残す** → 承認時に版ディレクトリ (`artifact_hash` キー) へコピー → git 記録 → symlink 切替 (§5、この順) → 決定 (approved / rejected / expired / invalidated) 時に削除。承認申請に至らなかった候補は commit 相の末尾で削除
- **孤児の掃除**: 起動時 reconcile (§5.3 の位置) で、`_staging/` 配下のうち「対応する pending の approval_request が無い」ものを削除する
- `.gitignore` の `/plugins/` はそのまま staging も覆う

### 2.4 残余リスク (明記)

- improve worker は任意コード実行 + ネットワーク (LLM・研究ツール) を持つ。plugin ソースの信頼モデルは従来どおり「人間承認まで信用しない」。egress は §6 のとおり**予算はツール契約であって安全保証ではない**
- `/dev/shm` への書込は可能 (§2.2)
- **プロセス包含は pgid ベース** (§1.1-4): `setsid()` を呼ぶ孫は CLI の pgid から逃げ、timeout / shutdown 後も生存し得る (LLM egress・staging 書込を継続)。完全な包含は cgroup v2 (systemd --user scope) が要る — **起票 (§10)**。`RLIMIT_NPROC` も掛けていない
- shell を許すため `/usr/bin` 配下の読取・実行が可能 (§2.1-5)

**変異 (§2)**: `execute_paths` を `_assert_allowlist_excludes_data_dir` に渡さない (→ `data/` を execute に入れても通る、pin が落ちる) / `plugins/` 全体または `reports/` を rw に入れる (→ 静的 pin) / `/dev` を ro に戻す (→ subprocess.DEVNULL 経路の実測テスト) / `/usr/bin` を closure から落とす (→ shell 系 backend の 1 要素 drop pin) / staging を Mission id で分けず共有にする (→ 並行 Mission の衝突テスト) / `discover` が `_staging` / `.versions` を拾う (→ 承認前 plugin がロードされる pin) / 正規形に反する名前を discover が受ける (→ `Foo-bar` ディレクトリが skip される pin) / 孤児 reconcile を落とす (→ 起動時テスト) / `HOME` に実ホームを渡す (→ env pin)。

---

## 3. 改善 Mission — 起動・レーン・注入・道具・出力

### 3.1 起動とレーン (R7) — wave 意味論

- **起動契機**: `schedule.improve` (`weekly` / `daily`、既存 config で未消費だったキーをここで消費) — scheduler tick が `improve_due(now)` を判定し **wave を 1 つ**起こす。**手動**: 対話シェル `improve` (M=1 の wave、**分担なし** = 全バックログを見る)。どちらも missions 行 `loop='improve'`, `trigger` は NULL (設計書 §12 — trigger は trade 専用)
- **別レーン**: 新設 `ImproveSupervisor` (`core/improve_supervisor.py`)。容量 `improve.parallel` (既定 1、上限 4)。**取引レーン (`MissionSupervisor`、容量 1) とは独立** — 改善実行中も取引 Mission は受理される。**preemption はしない** (改善は取引のために中断されない)。実装は `MissionSupervisor` の一般化 (N スロット + `kind="improve"`) でも別クラスでもよいが、**取引レーンの直列性契約 (容量 1・原子的 try_submit) を変えない**ことを受入条件に置く
- **wave** (codex 1 周目 I3): due event ごとに ①`M = min(improve.parallel, 空きスロット数)` を原子的に決め (M=0 なら wave を起こさず activity に記録) ②`open|observation` の backlog id を**不変スナップショット**として取り ③partition `k=0..M-1` を一度ずつ予約し ④M 件の Mission を `k` 付きで submit する。分担は `id % M == k`。**発見・リサーチで新規に見つけた課題は自由に追加してよい**が、他 partition の課題は一覧に載せても「選ばないこと」と指示する。部分 submit 失敗 (spawn 失敗等) の partition は**この wave では働かない** (activity に記録)。次 wave は新しいスナップショットで再分割する。**手動 `improve` は M=1・分担なし**。手動と週次が重なった場合はスロットが埋まっている分だけ M が減るだけで、二重予約は起きない (partition は wave 内で閉じる)
- **各 Mission は独立した improve worker** (WorkerRunner `worker_profile="improve"`) を持つ。workdir・staging・scratch home は Mission id 単位
- **重複解決の線形化点は §4.1 Tx-1 の backlog CAS** (先に `selected` を取った方が勝ち、後着は observation)。「先に commit した方」ではなく「先に選択を DB に取った方」
- **shutdown / join** (I4): `ImproveSupervisor.shutdown()` は `stop_event` を立てて新規 submit を拒否し、走行中の N 本の WorkerRunner に stop を伝播 (`WorkerRunner` の既存 `stop_event` を共有 — 各 WorkerRunner が自分の worker を kill し `MissionResult` を返す) → 各 commit 相の `finally` が missions 行を終端 → `ImproveSupervisor.join(shutdown_join_timeout_sec)`。`run_service` の停止シーケンスは取引レーンの `supervisor.join` と**同じ位置で**改善レーンも join する (取引側の順序は変えない)
- **LLM の競合**: backend=local で改善と取引が別 alias を使うと llama-swap がモデルを入れ替える (TTL・swap 遅延)。既定は同一 alias。設定で別 alias にした場合の遅延は受容 (警告を出す)。claude / codex は競合しない
- 週次の tick は取引 Mission と重ならない時間帯 (`schedule.improve_at` = 曜日+時刻、既定 土曜 03:00 表示 TZ) に置く

### 3.2 注入コンテキスト (親が決定論的に集計してプロンプトに焼く)

`loops/improve_context.py` が生成し、`loops/prompts/improve_mission.md` (新規) のテンプレートに差し込む:

| 節 | 内容 | 出所 |
|---|---|---|
| 成績レポート | 直近 30/90 日の勝率・PF・ペア別・時間帯別・却下 intent の内訳 (reject_category 別)・hold 率 | `trade_intents` / `orders` (親の RO 集計) |
| 改善履歴 | 過去の `improvement_runs` (何を試し、approval / report / observation のどれで終わったか)。**各バックログ課題の試行回数と、strategy なら標本 (取引数)** を添える (R8) | `improvement_runs` / `improvement_backlog` / `backtest_runs` |
| 現行構成インベントリ | 組み込み + 承認済み plugin (kind・pairs・timeframe)、ニュースソース一覧、risk gate 現行値 | registry / `approved_plugins` / `news_sources` / settings |
| バックログ | open + observation の一覧 (担当 partition に印。手動 wave は印なし) | `improvement_backlog` |
| ユーザー方針 | `policy/directives.md` 末尾 4000 文字 (全 Mission 共通) | `Policy.tail` |
| 参照 | サンプル plugin の場所 (`docs/examples/plugins/`)・plugin 契約の要約・候補置き場のパス・**plugin 名の正規形** | 定数 |
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
| `read_plugin_source(name)` | worker | 稼働中 plugin の 3 ファイル読取 (改良の起点) | 読取のみ |
| `run_plugin_tests(name)` | worker | `python -m pytest -q -p no:logging <staging>/<name>/test_plugin.py` を subprocess (rlimit 継承・timeout `plugin.pytest_timeout_sec`) | 結果は**参考** — 親が改めて回す (§4) |
| `run_backtest(name, pair)` | **親 RPC** | `holdout.run_in_sample` を親が回し、**集計指標のみ** (取引数・PF・勝率・平均 R・DD。期間端点なし) を返す。**DB には書かない** — 呼出し (params・result・mission_id) は親の **Mission 内 RPC 台帳** (メモリ) に積まれ、commit 相で `backtest_runs` (`scope=in_sample, issued_by=harness`) として永続化される (§4.1 Tx-2、codex 1 周目 I1) | 遮断 1・2 |
| `analyze_corr(request)` | **親 RPC** | 既存 `backtest.analysis.analyze_for_agent` (列挙制パラメータ・固定個数の要約統計)。**`analysis_runs.save` は呼ばない** (`persist=False` 相当に改修) — 同じく台帳経由で commit 相に永続化 | 遮断 7 |
| **無いもの** | — | `get_signals` / 任意 SQL / `ohlcv_*` 直読 / holdout / `bless` / approval 発行 / backlog 書込 / news 提案 | 遮断 3・6・8、R2、R6 |

RPC は既存 `tool_rpc` フレーム (in-flight 1) に `run_backtest` / `analyze_corr` を追加する。親側 dispatcher は `rpc_timeout_sec` を **RPC 種別ごと**に持つ (バックテストは数十秒〜数分。`improve.backtest_rpc_timeout_sec` 既定 600)。**RPC 台帳** (`ImproveRpcLedger`、Mission ごと・メモリ、lock 付き状態機械 `OPEN → FROZEN → PERSISTED | DISCARDED` — codex 2 周目 I4): dispatcher は**期限内に完了した**各呼出しの `{opaque_ref, kind, params, result_summary, trial_count}` を `OPEN` の間だけ追記する。`trial_count` は `analysis_runs.trial_count` と同じ意味 (= 計算した相関値の個数。lead-lag 1 呼出しでも 25 になり得る) で、`analyze_for_agent` が返す値をそのまま持つ。commit 相は先頭で台帳を**原子的に `FROZEN`** にしてから読む。**FROZEN 以後に届く遅延 RPC 完了は missions.status に関係なく拒否** (activity にカウント)。**RPC timeout (`rpc_timeout_sec` 超過) した呼出しは監査上「試行」に数えない** — その遅延結果は捨てる。監査に載るのは期限内完了分のみ (この規則を payload の説明文にも焼く)。台帳は成功 commit 後 `PERSISTED`、Mission 失敗/timeout で `DISCARDED`

### 3.5 出力 schema (`loops/summary.py` に `IMPROVE_OUTPUT_SCHEMA`)

```json
{
  "discoveries":  [{"idea": str, "source": "agent"|"research", "evidence": str}],   // 上限 §4.2-2
  "selected":     {"backlog_id": int|null, "idea": str},                             // 既存 id か新規
  "artifact":     {"type": "plugin", "name": "^[a-z][a-z0-9_]{0,63}$", "kind": "indicator"|"signal"|"strategy",
                   "self_test": "passed"|"failed"|"not_run", "summary": str}
               |  {"type": "report", "title": str, "body_md": str}
               |  {"type": "observation", "reason": str},
  "selection_rationale": str        // 分析 id・探索回数は agent に書かせない — 親が RPC 台帳から作る (codex 1 周目 I7)
}
```

`analysis_run_ids` / `trial_count` は**出力 schema に無い**。approval payload とレポートに載る値は親が台帳から生成する (§4.2-5)。agent が本文中に id を書いても無視される。

### 3.6 失敗の扱い

timeout / 出力不正 / worker 異常死 → missions 行を該当 status で終端、`improvement_runs` は `result=NULL` のまま `finished_at` を書く (新しい終端値は足さない — 既存 CHECK `('approval','report')` を維持し、失敗は missions 側で読む)、staging を削除、**RPC 台帳を `DISCARDED` (`backtest_runs` / `analysis_runs` は書かれない)**。**再試行はしない** (次の wave で自然に再実行。R8 により課題は消えない)。

**変異 (§3)**: 改善を取引レーンに submit する (→ 取引受理テスト) / wave が M=1 しか起こさない (→ `parallel=4` で 4 partition が全て担当されるテスト) / 分担を渡さず全件を全 Mission に見せる (→ 重複解決テストが二重採用を検出) / `IMPROVE_FORBIDDEN` のツールが improve registry に居る (→ 既存 pin 拡張) / `run_backtest` が期間端点を返す (→ 返却 schema pin) / **RPC が `backtest_runs` に直接書く** (→ timeout Mission 後に行が残るテスト) / 台帳が finalize 後の遅延 RPC を受け付ける (→ 遅延注入テスト) / `write_staging_file` が `..` を通す (→ パス正規化テスト) / `run_plugin_tests` の結果でゲートを省く (§4 の変異) / 週次判定を落とす (→ scheduler pin) / shutdown が改善レーンを join しない (→ 停止時に worker 残留テスト)。

---

## 4. 親側 commit 相 — 出力の検証とゲート (`loops/improve_loop.py`)

`ImproveLoop` は `TradeLoop` と同じ prepare / run / commit の三相。prepare (core_lock 内・短時間): missions 行作成・staging mkdir (0700)・注入コンテキスト生成・RPC 台帳の生成。run (lock 外): `WorkerRunner.run`。commit (lock 外、**DB は improve レーン専用接続**、`missions.finish` の CAS で終端)。

### 4.1 transaction 設計 (codex 2 周目 C2) — 長い仕事は transaction の外、DB 書込は短い 2 回

SQLite の writer は 1 本なので、**`BEGIN IMMEDIATE` を pytest / バックテスト越しに保持してはならない** (取引レーンが busy timeout を踏み R7 が破れる)。commit 相の DB アクセスは次の 2 つの短い transaction に閉じる:

- **Tx-1 (早い短い tx、選択の線形化)**: `BEGIN IMMEDIATE; UPDATE improvement_backlog SET status='selected', attempts=attempts+1, updated_at=? WHERE id=? AND status IN ('open','observation'); COMMIT` — `rowcount=1` がこの Mission を唯一の勝者にする (I2)。新規 idea なら同じ tx で INSERT → UPDATE。`rowcount=0` (並行 Mission が先に取った / 人間が閉じた) → **敗者経路**: ゲート作業を一切せず、レポート (「重複のため見送り」) と `improvement_runs.finish` だけを Tx-2 で書く
- **transaction 外**: 出力検査・スナップショット・AST・pytest・in-sample・holdout・レポート本文生成・レポートファイル作成。**親が回す `run_in_sample` / `run_holdout_gate` は non-committing 版**を使う — 既存 `_run_scope → save_harness_run(commit)` を **caller-owned sink (`record_fn`)** に差し替え、行は commit 相が Tx-2 で書く。`analyze_for_agent` も同様 (`persist=False` → 保存パラメータを返す)
- **Tx-2 (最後の短い tx)**: `BEGIN IMMEDIATE` → 台帳の `backtest_runs` / `analysis_runs` 行 → 親ゲートの `backtest_runs` 行 (in-sample / holdout_gate) → `approvals.create` → backlog 遷移 (§4.3) + `last_result` → `improvement_runs.finish` → `COMMIT`。**その後**に `missions.finish` の CAS。途中失敗は全部ロールバック (staging は残り、Mission は failed、backlog は Tx-1 の `selected` のまま → §4.3 の失敗遷移を別の短い tx で `observation` へ)
- **store helper に caller-owned transaction 版を用意する**: `save_harness_run` / `approvals.create` / `improve_runs.finish` / `analysis_runs.save` / `backlog.set_status` の各々に `commit=False` (呼び出し側が transaction を持つ) 変種。現行の内部 `conn.commit()` はそのまま残し (他の呼び出し元は不変)、improve レーンだけが変種を使う

### 4.2 手順

0. **台帳の凍結**: `ImproveRpcLedger` を `FROZEN` にする (これ以後の遅延 RPC 完了は拒否 — §3.4)
1. **出力検査** (tx 外): schema 検証 (runner 側でも済んでいるが親で再検証) / `artifact.name` が正規形 (§2.3) / `selected.backlog_id` が実在 / `artifact.type=plugin` なら `staging_dir/<name>` が dirfd + `lstat` で「通常ディレクトリ」、`plugins/<name>` が **{不存在 / プレーン dir / 正規形 symlink}** のどれか (§2.3、`resolve()` は使わない)。不合格 → Mission `failed`、staging 削除、台帳 `DISCARDED`、終わり
2. **バックログ追記 + 選択 (Tx-1)**: `discoveries` を追加 (`source` = agent|research)。**正規化 (空白・大小文字) した `idea` の完全一致は重複として捨てる**。**上限 `improve.max_new_backlog_per_mission` (既定 20)** — 超過分は捨てて activity に件数を記録。続けて選択の CAS (§4.1)。敗者 → 敗者経路
3. **plugin ゲート (artifact.type=plugin、tx 外) — 候補は不変スナップショットとして扱う (codex 1 周目 C3)**。順に、どれか 1 つでも不合格なら**承認申請は出さず**、結果をレポートに残して backlog を `observation` へ:
   - a. **スナップショット検査**: `staging_dir/<name>/` 直下に**ちょうど 3 本の通常ファイル** (`plugin.py` / `config.yaml` / `test_plugin.py`)、サブディレクトリ・symlink・hardlink (`st_nlink==1`)・その他ファイル無し、各サイズ ≤ `_MAX_FILE_BYTES`。dirfd + `O_NOFOLLOW` で開く。`config.yaml` の `kind` 一致・`plugin/loader._validate_config` 相当
   - b. **2 つのハッシュを先に計算**: `content_hash` (H_before、既存定義 = `plugin.py` + `config.yaml`。**承認の実行時 identity**、定義不変) と **`artifact_hash`** (新設、`sha256(b"plugin.py\0"+p+b"\0config.yaml\0"+c+b"\0test_plugin.py\0"+t)` = 3 本全体。**版ディレクトリのキー**、codex 2 周目 I2)
   - c. `plugin/sandbox.check_source` を **`plugin.py` と `test_plugin.py` の両方**に (現行 `submit_plugin` と同じ)
   - d. **pytest を Landlock で囲った別プロセスで回す — 候補ディレクトリは read-only**: 新ヘルパ `plugin/gate_pytest.py:run_gate_pytest(plugin_dir, *, settings) -> GateResult`。`sys.executable -m agentic_fx.plugin.gate_pytest_worker` を `Popen(cwd=<tmp workdir>, env=最小 + PYTHONPYCACHEPREFIX=<tmp>/pyc, start_new_session=True)` (**`PYTHONPYCACHEPREFIX` は interpreter 起動前に env で渡す** — 起動後に `os.environ` へ入れても効かない、codex 2 周目 M1。worker 冒頭で `sys.pycache_prefix` を assert)、子は起動直後に **improve profile と同じ allowlist ファミリ** (`read_only` = code_root/venv/stdlib/`/usr/lib`/zoneinfo/`/etc` **+ 候補 plugin_dir (ro)**、`execute` = venv/base/`/usr/lib`/`/usr/lib64`、`read_write` = **tmp workdir + `/dev` のみ**) で `landlock.restrict_to` → `pytest.main(["-q", "-p", "no:logging", "-p", "no:cacheprovider", "--rootdir", <tmp>, str(plugin_dir)])`。rlimit は既存 `_pytest_rlimit_preexec` (この子は single-thread で起動されるので preexec 可)、timeout は `plugin.pytest_timeout_sec`。**Landlock 不可の環境では improve と同様 fail closed**。**`submit_plugin` / `bless` の `_default_pytest_runner` もこのヘルパに置き換える** (`plugin/approval.py:149` の無隔離実行を廃止)
   - e. **両ハッシュを再計算。H_after ≠ H_before または artifact_hash が変わっていれば不合格** (テストが自分の候補を書き換えた)。以後の全工程は **この不変スナップショット** だけを入力にする
4. **戦略採用ゲート (kind=strategy のみ、tx 外)**: 親が `run_in_sample(..., record_fn=<sink>)` を各 pair で回す (worker の `run_backtest` 結果は使わない)。**取引数 < `backtest.min_trades` (30) → 「評価不能」: 承認申請を出さず `observation` (悪いとは記録しない — R8)**。≥30 → `run_holdout_gate(..., record_fn=<sink>)` を回し、結果は **approval payload の添付のみ** (改善ループの読取ビューには載せない — 遮断 8)。`indicator` / `signal` はこのゲートを課さない (設計書 §6)。sink に積んだ行は Tx-2 で書く
5. **承認申請 (Tx-2 の一部)**: `approvals.create(kind="plugin", commit=False, payload={name, kind, staging_path, content_hash: H_before, artifact_hash, mission_id, in_sample: {...}, holdout: {...}|null, baseline: {...}|null, analysis_run_ids: <台帳→Tx-2 で解決した実 id>, trial_count: <台帳 entries の trial_count の総和 = 計算した相関値の個数>, analysis_call_count: <台帳の analyze_corr 呼出数>, backtest_call_count: <台帳の run_backtest 呼出数>, selection_rationale: <agent>, summary, audit_note: "RPC timeout した呼出しは数えていない"})`。**id・回数は agent 出力から取らない** (I7)。**`bless` は経由しない**
6. **レポート (親だけが書く、tx 外)**: `artifact.type=report`、およびゲート不合格・評価不能・observation・敗者のとき、`reports/improve-YYYY-MM-DD-<mission_id>.md` を親が整形して書く。**書き方**: `reports/` の dirfd に対し `openat(name, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW)` — 既存パス (通常ファイルであれ symlink であれ) が在れば **fail closed** (activity + 通知、Mission 処理は続行)。`reports/` は起動時に親が mkdir、gitignore 済み、**worker の rw には無い** (§2.1)。**agent の `body_md` は「提案本文」節に引用として入れる — 信用しない**
7. **`improvement_runs.finish` (Tx-2)**: 承認申請を出したら `result='approval'` + `approval_id` (+ レポートも書けていれば `report_path`)。承認申請なしで**レポートファイルの作成に成功したときだけ** `result='report'` + `report_path`。**レポート作成に失敗したら `result=NULL` + `finished_at` + activity に理由** (codex 2 周目 M2 — 存在しない成果物を DB が指さない)
8. **backlog 遷移 (Tx-2、§4.3)** と `last_result`
9. **`COMMIT` → `missions.finish` CAS → 掃除**: 承認申請を出した staging は残す。それ以外は削除。台帳 `PERSISTED`

**失敗の隔離**: commit 相の例外は improve レーンで握り、activity + 通知 (`improve_commit_failed`)。取引レーン・core_lock・資金保護には波及しない。missions 行は必ず終端する (`finally`)。Tx-2 の失敗はロールバックし、別の短い tx で backlog を `observation` (`last_result='commit_failed'`) へ戻す。

### 4.3 バックログの状態機械 (codex 2 周目 I6)

status: `open | observation | selected | done | rejected` (`rejected` は人間が `backlog reject <id>` で閉じる用。`improvement_backlog.status` に CHECK は無い — `db.py:168-174` — ので migration 不要)。`attempts INTEGER NOT NULL DEFAULT 0` と `last_result TEXT` を `ensure_column` で追加 (冪等)。`list_open` = `open|observation`。**全遷移は決定と同じ transaction で `last_result` を更新する**。

| 現在 | 事象 | 次 | `last_result` |
|---|---|---|---|
| open / observation | Mission が選択 (Tx-1 CAS、`attempts+1`) | selected | (変更なし) |
| selected | Tx-1 で敗者 (rowcount=0 — 遷移は起きない。敗者は自分の Mission の成果を観察に落とすだけ) | — | — |
| selected | レポートを書いて完了 (承認申請なし) | done | `report:<path>` |
| selected | ゲート不合格 / 評価不能 (<30) / artifact=observation | observation | `gate_failed:<reason>` / `insufficient_trades:<n>` / `observation:<reason>` |
| selected | 承認申請を発行 (pending) | selected (据え置き) | `approval_pending:<approval_id>` |
| selected | 承認 (approve、§5.1-7 と同じ tx) | done | `approved:<approval_id>` |
| selected | 却下 / 期限切れ / 失効 (approval 決定 tx で) | observation | `rejected:<reason>` / `expired` / `invalidated` |
| selected | Mission failed / timeout / Tx-2 失敗 | observation | `mission_failed:<status>` / `commit_failed` |
| observation / open | 人間が `backlog reject <id>` | rejected | `human_rejected` |
| done / rejected | (終端。人間が `backlog reopen <id>` で open に戻せる) | open | `reopened` |

approval の決定 (`approve` / `reject` / `expire_due` / invalidate) は payload の `backlog_id` を見て上表の遷移を**同じ transaction で**行う (`approvals.decide` の呼び出し元 = シェル / reconcile / 将来 REST が担う。決定論的ヘルパ `backlog.apply_approval_outcome(conn, approval_row, outcome, now, commit=False)` を 1 つ置く)。

**変異 (§4)**: worker の `self_test="passed"` を信じて 3d を省く (→ 「worker が passed と言い、親ゲートで落ちる」テスト) / 3d を Landlock 無しで回す (→ ゲート子プロセスから `data/` を読む fake test が通ってしまう pin。killer = ゲート pytest 内で `data/agentic.db` を open して失敗することを assert) / **候補ディレクトリを rw で pytest に渡す・pytest 後に hash を取り直さない (killer = `test_plugin.py` が `plugin.py` を strategy 版に書き換えるテストで、承認申請が出ないこと)** / `PYTHONPYCACHEPREFIX` を起動後に設定する (→ `sys.pycache_prefix` assert) / スナップショット検査を落とす (→ symlink を置いた候補が通る pin) / `name` の正規形検査を落とす (→ `../../docs/examples/plugins/rsi_indicator` が弾かれる pin) / **切替先を `resolve()` で検査する (→ 承認済み symlink plugin の再改善が failed になる pin)** / 選択 UPDATE を無条件にする (→ 2 接続同時選択で二重承認申請) / **Tx-1 を pytest 越しに保持する (killer = 2 接続同時 writer: ゲート中に取引レーンの `INSERT` が `busy_timeout` 内に通ること)** / `save_harness_run` の内部 commit 版を親ゲートで呼ぶ (→ holdout 失敗後に in-sample 行だけ残る pin) / 30 未満を `rejected` にする (→ observation pin) / holdout 結果を Mission 出力や次回注入に含める (→ 遮断 8 pin) / **agent 申告の analysis id / trial_count を payload に載せる (→ 偽装テスト: 台帳 100 件・申告 1 件で payload が 100)** / **`trial_count` を呼出回数にする (→ lead-lag 1 呼出しで 25 になる pin)** / 台帳を凍結せずに読む (→ 遅延 RPC 注入で保存漏れ・追記競合) / バックログ上限を外す (→ 21 件投入テスト) / **report 失敗でも `result='report'` を書く (→ dangling path pin)** / **approve/reject 後に backlog が `selected` のまま (→ 状態機械 pin)** / `bless` を呼ぶ (→ 承認申請が `pending` であることの pin) / レポートを `open(..., "w")` で書く (→ 事前に symlink を置いた fake で fail closed になる pin) / commit 相の例外で missions 行が終端しない (→ CAS finalize pin) / staging を削除しない (→ 掃除テスト)。

---

## 5. 承認時の履歴記録と昇格 (D6 の再収束) — 記録が先、切替は原子 (1 rename または 1 exchange)

### 5.1 承認手順 (人間が `approve <id>` した瞬間。シェル / 起動時 reconcile / 将来の REST から。**scheduler スレッドでは決して実行しない**)

**順序の原則: 履歴への記録が先、本番への切替は最後に原子操作 1 回。** git がどう失敗しても稼働中の旧版は消えず、「live が無い瞬間」が存在しない。

**レイアウト**:
- 承認済み内容は **版ディレクトリ `plugins/.versions/<name>/<artifact_hash>/`** (3 本全体の hash がキー — 同じ code/config で test だけ違う版も区別する、codex 2 周目 I2)。approval の実行時 identity は従来どおり `content_hash` (2 本) で、payload は両方を持つ
- 稼働中の `plugins/<name>` は**版ディレクトリを指す相対 symlink** (`.versions/<name>/<artifact_hash>`)。ロールバック = symlink を別の版へ向け直す (`plugin rollback <name> <artifact_hash>`。**git checkout は使わない**)
- 履歴は **bare リポジトリ `plugins/.history.git`** (`git init --bare` を lazy に。**ワークツリーを持たない** — codex 2 周目 I9: live が symlink である以上、通常ファイルを記録する git tree とワークツリーは型が合わず、`git status` が常時 dirty になり `git restore` が live symlink を実ディレクトリに置換して切替点を迂回する)。人間の閲覧は `git --git-dir=plugins/.history.git log|show`。`.git/info/exclude` の類は不要 (ワークツリーが無い)
- `plugin/loader.discover` と `content_hash(Path)` は symlink を辿って動く。`discover` は先頭 `.`/`_` を除外 (§2.3) するので `.versions/`・`.locks/`・`.history.git/` は列挙されない。プレーンなディレクトリ (手作り、初回移行前) と symlink 版は共存できる

1. **排他**: **`flock` を `plugins/.locks/<name>.lock` に取得** (プロセス間 — サービスの approve / 起動時 reconcile / 別プロセスの `afx plugin bless` が全て同じ lock を取る) + プロセス内は plugin 名ごとの `threading.Lock` (ⓒ、順序は flock → thread lock)。異なる plugin の並行承認は 5.2 の CAS が守る
2. **後発決定の確認 (ⓓ)**: 同一 plugin 名へのより新しい決定 (reject) があれば、この承認は失効 (`invalidated`) — 再試行で復活させない (D4 の順序規則)。backlog は §4.3 の遷移
3. **ハッシュ再照合 (ⓐ)**: 候補 (staging) を §4.2-3a と同じスナップショット検査に通し `content_hash` と `artifact_hash` を再計算、payload と一致しなければ承認は成立せず **pending のまま** + activity + 通知
4. **版ディレクトリの作成 (冪等)**: `plugins/.versions/<name>/<artifact_hash>.tmp-<approval_id>/` に 3 ファイルをコピー → 各ファイルとディレクトリを `fsync` → `rename` で `plugins/.versions/<name>/<artifact_hash>/` へ (既に同 artifact_hash の版があれば作らない)。失敗 → pending + 通知。**本番 symlink には触れていない**
5. **履歴への記録 (ⓑ) — 版ディレクトリの 3 本から、`<name>/<file>` のパスで**: 下記 5.2 の blob-level plumbing。失敗 (git 不在 / detached / identity 無し / CAS 上限) → **pending のまま + 通知。本番には一切触れていない**
6. **切替 (原子操作 1 回)**: temp symlink `plugins/.<name>.link-<approval_id>` (→ `.versions/<name>/<artifact_hash>`) を作り、`plugins/<name>` の現在の形で分岐:
   - **不存在 / symlink**: `os.rename(temp, plugins/<name>)` (symlink → symlink の rename は原子的)
   - **プレーンなディレクトリ (初回移行)**: `renameat2(AT_FDCWD, temp, AT_FDCWD, plugins/<name>, RENAME_EXCHANGE)` (ctypes syscall — `core/landlock.py` と同じ流儀。Linux 3.15+、同一 FS) で **temp symlink と live ディレクトリを 1 回で交換**する。交換後、temp パスに来た旧ディレクトリを版ディレクトリの内容 (自身の artifact_hash の版) と照合し、一致すれば削除、不一致なら残して activity ERROR。**`RENAME_EXCHANGE` 非対応 (kernel/FS) は初回移行を fail closed** — その plugin の approve/bless は pending (理由 `exchange_unsupported`) に留める。人間は plugin を一度退避して symlink 版から作り直せる (手順は運用ドキュメント)
   - 切替後 `content_hash(plugins/<name>)` (symlink を辿る) を再計算し payload と一致することを確認 (不一致 → 旧版へ向け直し pending)
7. **`decide(status="approved")` + backlog `done` (§4.3、同じ tx)**。ここで初めて `approved_plugins()` に載る (次回の再読込/起動から)
8. staging を削除、lock 解放

**却下・期限切れ・失効**: staging を削除、backlog を `observation` (§4.3)。`plugins/<name>`・版・履歴には触れない。

**bless (人間が本番の場所を直接編集して即時承認)**: 同じ `flock` を取り、`plugins/<name>` の現物 (symlink なら辿った先、プレーンならそのディレクトリ) をスナップショット検査 → 両 hash → **版ディレクトリへコピー** (4、冪等 — 同 artifact_hash の版があれば作らない) → 履歴記録 (5、blob の出所が版ディレクトリになるだけ) → 切替 (6。プレーンなら `RENAME_EXCHANGE`、symlink なら rename — **旧ディレクトリを `.versions` へ「移動」することは無い**ので destination 衝突は起きない、codex 2 周目 I3) → 照合 → decide (7)。ゲート pytest は §4.2-3d の Landlock ヘルパ。**既存のプレーンな `plugins/<name>/` は起動時 reconcile では触らない** — 次の承認/bless まではそのまま動く (discover は両形を読む)。

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
  - **(c) 切替済み・pending** (`decide` の DB 書込だけ失敗 — 窓は極小): live は新版を指すが approved でないため**新版は load されず、旧版も load されない** (旧版は `.versions/<name>/<旧 artifact_hash>/` と履歴に残る)。起動時 reconcile (`approved_plugins()` より前) と次の承認操作が拾って decide を完了させる。人間が急ぐなら `plugin rollback <name> <artifact_hash>` (approved 済み hash の版にしか向けられない)
- **再試行の 3 契機**: ①起動時 reconcile ②次の承認操作 ③シェル `approval retry <id>`。**scheduler tick にぶら下げない**。再試行は手順を頭から流す (lock → ⓓ → ⓐ → 版 (冪等) → git (tree 一致なら no-op) → 切替 (既に新版を指していれば no-op) → decide)
- **起動時 reconcile の位置**: `init_db` と中断 Mission の回収より後、**`approved_plugins()` より前** (後だとその起動では復旧した承認がロードされない — codex 4 周目 I3)。同時に ①§2.3 の孤児 staging 掃除 ②`.versions/<name>/*.tmp-*` の削除 ③「どの approval も参照せず、どの symlink も指していない」版ディレクトリの削除 ④**temp link `.<name>.link-*` の残骸削除** ⑤**exchange 後に temp パスへ来た旧プレーン dir の残骸** (版と一致すれば削除、不一致なら ERROR で残す) ⑥`plugins/<name>` が dangling symlink なら activity ERROR (人間の操作を待つ。自動では向け直さない) を行う。git 不在・reconcile 失敗はサービス起動を止めない (警告 + pending のまま)
- **git 不在**: 起動時検査で警告 (SQLite ≥3.35 assert と同じ扱い)。plugin 承認だけが成立せず、取引は動く

### 5.4 資金保護への非波及

承認スレッド (シェル / 将来 API) と起動シーケンスでのみ実行。**scheduler スレッドから git サブプロセスが呼ばれないこと**を回帰テストで固定 (呼ばれたら fail するシーム — D3' の通知と同じ形。「lock 内か」でなく「どのスレッドか」で検査)。`core_lock` は触らない。

**変異 (§5)**: 決定時ハッシュ再照合を削除 / 照合対象を index の blob から候補置き場のワークツリーへ戻す (TOCTOU 復活) / 専用 index をやめ `git add` + `git commit -- <path>` (**killer = 検証後に staging の plugin.py を書き換えてから commit させ、commit 内容が検証済みの内容であることを assert**) / **git 記録より先に切替する (killer = git 失敗を注入 (identity env 除去 / detached HEAD) して承認させ、`plugins/<name>` が旧版を指したまま `approved_plugins()` に残ることを assert)** / **プレーン初回移行を「旧を退避 → 新を置く」の 2 段 rename にする (killer = 1 段目の直後にプロセスを落とし、起動時に live が欠けている)** / **`RENAME_EXCHANGE` 非対応で 2 段にフォールバックする (→ fail closed pin)** / **版キーを `content_hash` にする (killer = code/config 同一・test 違いの 2 候補を順に承認し、履歴に 2 つの test が別 commit で残り、両版に rollback できること)** / **`flock` を落とす (killer = 別プロセスの bless と同時に走らせ、両者が互いの版を壊さないこと)** / **履歴を非 bare にしてワークツリーを持つ (killer = 承認後に `git status` が clean であること / `git restore .` 相当の操作で live symlink が実 dir に置換されないこと)** / 切替後の hash 再検証を落とす (killer = swap 中に版ディレクトリを差し替え、旧版へ戻され pending のままであること) / 切替先を `resolve()` で検査する (§4 と共有) / `_staging/` `.versions/` が index に入る (→ 記録後の tree に無い pin) / 旧版の消えたファイルが index に残る (→ `git rm --cached` 落とし) / 空 commit 判定を `git diff --cached` に戻す / commit と decide の順序を入れ替える / commit 失敗で承認を成立させる / 後発 reject の確認を削除 / 起動時 reconcile を `approved_plugins()` の後に置く / reconcile が dangling symlink を勝手に向け直す (→ ERROR に留める pin) / git を scheduler スレッドから呼ぶ / CAS 無し update-ref (→ 並行承認で先発 commit が消える) / identity env を落とす (→ 空 git config 環境で永久 pending) / `fsync` を落とす (→ 実装計画の fault-injection、設計では要求のみ) / bless が Landlock ヘルパでなく `_default_pytest_runner` を使う (→ §4.2-3d の pin) / discover が symlink を辿らない (→ 承認直後に plugin が消える pin) / approve 時に backlog を `done` にしない (→ §4.3 pin)。

---

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

1. **遮断 8 項目の統合回帰** (`staging_dir` は handshake 1 値から導かれる — root/DB は渡さない) (改善 worker の**実プロセス**に対して。**improve registry の task 直後に red で書き始める** — 最後の E2E に置かない): ①`data/agentic.db` の絶対パス open / `data/` 列挙が失敗 ②`run_holdout_gate` を呼んでもデータ到達不能で失敗 ③`ohlcv_history` / `ohlcv_cache` を直読するツールが registry に無い + DB パスが handshake に無い ④**書き込み可能パスが staging・workdir・`/dev` に閉じる** (`reports/`・リポジトリ本体・`plugins/<name>`・`plugins/.versions`・`config/`・`policy/` への write が `EACCES`) ⑤plugin サンドボックスの入力 DataFrame はハーネスが与える (既存 pin 継続) ⑥`get_signals` を含む `IMPROVE_FORBIDDEN` + 取引 registry の全ツールが improve registry に**無い** ⑦`analyze_corr` / `run_backtest` の返却 schema に日時・期間端点・順序付き窓列・観測数が無い、**かつ RPC が DB に直接書かない** ⑧approval の結果として holdout の指標・baseline 差分・閾値別合否が Mission 出力・注入コンテキスト・RPC 返却のどこにも現れない、**かつ payload の analysis id / 回数は agent 出力からでなく RPC 台帳から来る**
2. **3 実装 (Local / Claude / Codex) が同一の契約テストスイートに合格** (fake CLI スクリプトで実 LLM を呼ばずに回す。運用で選ばれるのは 1 つ): 4 終端 / reason 安全化 / timeout 優先 (fake が sleep) → CLI セッションが killpg され worker 自身は生きて `timeout` を返す / schema 不適合 → failed / **子 env に鍵の名前がゼロ・scratch home のみ** / claude の allowedTools が profile で固定 / codex は trade で拒否 / `max_turns` の runner 別セマンティクス / **exec closure の 1 要素 drop pin** (shell 系 backend で `/usr/bin` を落とすと fake shell 起動が `EACCES`)
3. **ゲート pytest が柵の中で動き、候補は不変**: ゲート子プロセスから `data/agentic.db` を open する fake test が `EACCES` で失敗する / **`test_plugin.py` が `plugin.py` を書き換える fake で H_before ≠ H_after となり承認申請が出ない** / symlink・余分ファイルを含む候補がスナップショット検査で落ちる / `sys.pycache_prefix` が tmp を指す / `submit_plugin` と `bless` が同じヘルパを通る (無隔離の `_default_pytest_runner` が呼ばれない pin) / **transaction が pytest / バックテストを跨がない** (ゲート中に別接続の writer が `busy_timeout` 内に通る)
4. **D6 変異列** (§5) を全て殺す / **git サブプロセスが scheduler スレッドから呼ばれない**回帰テスト / 承認は git 記録成功後にしか `approved` にならない / **git 失敗を注入しても稼働中の旧版が消えない** (記録が切替に先行) / **切替は原子 (1 rename または 1 exchange)** (2 段化の killer、exchange 非対応は fail closed) / **`flock` により別プロセス bless と競合しない** / **履歴は bare** (承認後に live symlink を壊す git 操作の面が無い) / **版キーは `artifact_hash`** (test 違いの版が区別される) / **承認済み symlink plugin を再改善できる** (`resolve()` 非依存)
5. **改善レーンが取引レーンを塞がない**: 改善 Mission 実行中に `MissionSupervisor.try_submit("trade")` が受理される / 取引レーンの容量 1・直列性の既存 pin が不変 / **wave が `parallel=N` で N partition を全て担当する** / **backlog 選択の CAS** (2 接続同時で勝者 1) / **backlog 状態機械** (approve → done、reject/expire → observation、report → done、失敗 → observation) / **台帳の凍結** (FROZEN 後の遅延 RPC が拒否され、payload `trial_count` が実 trial の総和) / shutdown が改善レーンの worker を全て回収する / **認証コピーは親が spawn 前に行い、worker は原本を読まない**
6. **FakeRunner E2E**: 発見 → バックログ追加 (上限・重複) → 候補 → ゲート不合格でレポート止まり (承認申請なし) / ゲート合格で承認申請 (pending) → `approve` で 版 → git 記録 → symlink 切替 → approved (この順) / 30 未満 strategy が observation / 並行 2 Mission の重複選択が後着 observation / **`artifact.name` に `../` や絶対パスを返す fake が failed** / **レポート先に symlink を事前に置くと fail closed** / **timeout した Mission の `backtest_runs` / `analysis_runs` が残らない** / **レポート作成失敗時に `result=NULL`**
7. `MissionResult.status` 4 値・決定論的コア (`risk_gate` / `paper_broker` / `transitions` / `executor` の判定) は diff ゼロ / 既存 2071 テストが壊れない / 新規 config キーは `settings.yaml.example` と同期 / migration (`improvement_backlog` の列追加) は空 DB・既存 DB で冪等 / **`discover` が `_`/`.` 先頭ディレクトリを列挙せず、正規形外の名前を skip する pin**

### 7.2 有効化後の実測 (8) — blocking ではない。既定見直しの材料

8. **実機 E2E (Task 13)**: 3 backend それぞれで「サンプル indicator plugin 1 本を候補置き場に実装し、親ゲートを通す」を実測。あわせて claude の rlimit 下 1 ターン / node ラッパ経由 codex / **`--disable apps` の egress (`strace -e trace=connect` 相当で記録)** / `--setting-sources ""` 下の init イベント / auth ローテーション有無 / exec closure の実測 (1 要素 drop) を記録する。結果は §0.2 の既定見直しに使う

---

## 8. task 一覧・依存・並列束 (概略 — 詳細は実装計画で)

| 束 | # | task | 由来 | 依存 |
|---|---|---|---|---|
| **A** (runner) | 1 | `CliRunner` 共通基盤 (launcher 経由 exec + PDEATHSIG + 別セッション + timeout/killpg) + factory + config schema (`^(local\|claude\|codex)$`, `runner.claude/codex`, trade=codex 拒否) + 起動時検査 (bin/--version/認証/shebang 解決) + **`WorkerRunner` の親側 workdir 0700・`home/tmp/cfg` 作成・認証コピー (検査付き)** | §1.1/1.4 | — |
| A | 2 | `ClaudeRunner` + fake CLI 契約テスト | §1.2 | 1 |
| A | 3 | `CodexRunner` (provider 2 択) + fake CLI 契約テスト + argv pin | §1.3 | 1 |
| A | 4 | MCP stdio シム + mission_worker 側 dispatcher (Unix socket) | §1.6 | 1 |
| **B** (柵) | 5 | `landlock.execute_paths` + backend 別 exec closure + `_bootstrap_improve_profile` 拡張 (`/dev` rw, `/proc`, resolve, **handshake `staging_dir` の受領・再検証・rw 化**) + assert 拡張 + env 追加 + `/dev/null` O_RDONLY + **`discover` の `_`/`.` 除外と名前正規形 + symlink 追従** | §2 | — |
| B | 6 | Landlock ゲート pytest ヘルパ (候補 ro・pyc prefix を env で・スナップショット検査・content/artifact hash before/after) + `submit_plugin` / `bless` の置換 | §4.2-3 | 5 |
| **C** (registry) | 7 | improve registry (研究ツール + advisory 予算 / **`staging_dir` を根とする** staging ファイル / `run_plugin_tests` / RPC 2 種 **+ RPC 台帳 (状態機械)** + `analyze_for_agent`/`run_in_sample` の non-committing 版) + **遮断 8 項目の統合回帰を red で開始** | §3.4/§6 | 5 |
| C | 8 | バックログ拡張 (`observation` / attempts / last_result / **選択 CAS / 状態機械ヘルパ `apply_approval_outcome`** / `backlog reject|reopen`) + store helper の `commit=False` 変種 + 注入コンテキスト生成 + prompt | §3.2/§4.1/§4.3 | — |
| **D** (loop) | 9 | `ImproveSupervisor` (N スロット・wave・shutdown/join) + scheduler 週次判定 + `improve` / `improve add` / `backlog` / `policy add` コマンド (**`improve` の有効化配線は Task 12**) | §3.1 | — |
| D | 10 | `ImproveLoop` (三相 + Tx-1/Tx-2 + commit 相ゲート + 台帳永続化 + 承認申請 + レポート (O_EXCL\|O_NOFOLLOW、失敗時 result=NULL) + improvement_runs + backlog 遷移) | §4 | 7, 8, 6 |
| **E** (承認) | 11 | 版ディレクトリ (`artifact_hash`) + bare 履歴 (blob-level plumbing) → symlink 切替 (rename / `RENAME_EXCHANGE`) + `flock` + reconcile (孤児 staging・tmp 版・未参照版・temp link・旧 dir 残骸・dangling) + bless 経路 (初回 symlink 化) + **`approval retry` / `plugin rollback` の handler と配線** + approval 決定時の backlog 遷移 | §5 | 6, 8 |
| **F** | 12 | FakeRunner E2E + 遮断 8 項目の完了 + **有効化配線 = `schedule.improve` の消費と `improve` コマンドの有効化のみ** (gate = §7.1 の 1〜7 逐語) | §7.1 | 全部 |
| F | 13 | 実機 E2E (3 backend、§7.2 の実測項目、`--disable apps` egress 記録) + 既定見直し提案 | §7.2 | 12 |

- **A / B / C-8 / D-9 は worktree 並列**可。C-7 は B-5 の後。D-10 は C・B-6 の後。E-11 は B-6・C-8 の後 (A と並列可)。F は最後
- ファイル競合: `mission_worker.py` (A-4 / B-5) は**同一ファイル** — A-4 を B-5 の後に直列 / `worker_runner.py` (A-1 認証コピー / B-5 handshake `staging_dir`) は同一ファイル — マージ順に注意 / `plugin/approval.py` (B-6 / E-11) は順序依存 / `plugin/loader.py` (B-5 discover / E-11) は順序依存 / `store/db.py` は C-8 のみ / `service.py` (A-1 起動時検査 / D-9 / E-11 reconcile) はマージ順に注意

### 8.1 実装計画へ送る項目 (codex 1 周目の「実装計画へ送る項目」を要約)

1. C1/C2/C3: dirfd 基準の `openat` / `O_NOFOLLOW|O_EXCL`、plugin 名 regex、3 本の不変マニフェスト、pytest 用 read-only スナップショットのヘルパと変異テストを具体化
2. C4: exec closure を claude native / codex vendor native / nvm node ラッパの 3 形で採取し、`/bin/bash`・`/usr/bin/env`・node・pytest python・動的ローダの 1 要素 drop テストを作る
3. I1: Mission ローカル RPC 台帳・commit 時の id 解決・failed/timeout 時破棄・RPC timeout 後のスレッド回収を protocol sequence と SQL transaction まで落とす
4. I2: backlog の条件付き UPDATE と、勝者だけが approval/report の副作用を出す transaction 境界を SQL 単位で書く
5. I3: wave id、M/k 予約、scheduler/manual 重複、部分 submit、shutdown 中の受付拒否を state machine と test matrix にする
6. I4: pgid ベースの所有 (別セッション + PDEATHSIG) を、SIGTERM 無視 CLI・CLI→bash 生存中の kill・service SIGTERM・N=4 同時停止の実プロセステストにする (`setsid()` 孫の逃避は起票側の cgroup で扱う — テストは「逃げる」事実の記録まで)
7. I5: advisory に縮めたので、受入条件と変異リストから安全保証の表現を外したことを実装計画でも維持 (proxy は起票)
8. I6: `flock` ファイル名・版ディレクトリ・fsync/rename 順・各 rename/commit/decide 直後の crash に対する起動時 reconcile テスト
9. I7: RPC 台帳から `analysis_run_ids` / trial count を生成する schema と、agent が id/count を偽装しても payload に反映されないテスト
10. 実測 task (claude fsize 8MB / node ラッパ / `--disable apps` egress / claude init tools / auth rotation) + 全新規 `_Strict` config (`improve.parallel`、RPC timeout、research 予算、backlog 上限、`schedule.improve_at`、CLI grace 等) の `Settings`/example 同期 + **改善 N 並行中に取引 DB commit が busy timeout を踏まない負荷テスト**

**codex 2 周目から (要約)**:

11. C1/I7: 親が auth と `staging_dir` capability を準備する protocol sequence (mkdir 0700 → 通常ファイル/mode 検査 → auth copy → handshake → 子の再検証 → Landlock → runner 起動) をフィールド単位で書く
12. C2: 長時間処理を全て transaction 外へ出し、Tx-1 (CAS) と Tx-2 (台帳/ゲート行/approval/improvement_run/backlog 遷移) の SQL sequence を書く。全 store helper の `commit=False` 変種を列挙する
13. I4: `ImproveRpcLedger` の `OPEN/FROZEN/PERSISTED/DISCARDED`、in-flight counter、RPC timeout、Mission timeout、commit 同時発生の race matrix
14. I5: 台帳 entry `{opaque_ref, params, result, trial_count}` → 保存後の id 群と `sum(trial_count)` を payload へ解決。call count は別名
15. I1: plugin 名の字句検証、最終 symlink を辿らない dirfd API、許可する live 3 形 (absent / plain / canonical symlink) の test matrix
16. I2: `content_hash` と 3 本 `artifact_hash` の分離。同 code/config・異 test の 2 版が保存・commit・rollback できる pin
17. I3/I9: `RENAME_EXCHANGE` の ctypes 実装と非対応 FS の検出、crash point ごとの reconcile テスト、bare 履歴で `git status/restore` の面が無いことの pin
18. I6: backlog × approval の状態直積から許可遷移と `last_result/attempts` 更新 transaction の表
19. I8: launcher (`python -c` + PDEATHSIG + execv) の実プロセステスト: SIGTERM 無視 CLI・CLI→bash 生存中の kill・worker 親死・N=4 shutdown。`setsid()` 孫の逃避は記録のみ
20. M1/M2: gate worker の Popen env に pycache prefix を入れる pin、report file 作成成功だけを `report_path` の成立条件にする fault-injection
21. M3: Task 9/11/12 のコマンド所有と依存 (本書 §8 の表に反映済み) を実装計画でも維持
22. 継続実測 (claude fsize、node wrapper、apps egress、claude init tools、auth rotation)、exec closure 1 要素 drop、SQLite trade/improve 同時 writer 負荷、初回移行の各 crash 点を blocking/non-blocking の該当節へ逐語対応

## 9. 変えないもの

- 決定論的コアの判定ロジック / drawdown kill switch / 取引レーンの容量 1・直列性・preemption 契約
- `MissionResult` の 4 値 / `Mission` のフィールド / `LocalRunner` の tool-calling loop とリトライ規律
- trade profile の権限境界 (RO DB + 資格情報 env。Landlock 任意) — ClaudeRunner を trade で選んでも MCP ツールのみ
- `IMPROVE_FORBIDDEN` の pin / 遮断 8 項目の意味論 / holdout の所有 (ハーネス) / `backtest.holdout_months` はコア所有
- plugin 機構の契約 (3 ファイル・純関数・AST allowlist・サンドボックス実行・**`content_hash` の定義 (2 本、承認の実行時 identity)** — `artifact_hash` (3 本) は版ディレクトリのキーとして**新設**するが `content_hash` を置き換えない)
- 改善ループの出力先が gitignore 領域 (`plugins/`・`reports/`) に閉じること (2026-08-08 裁定)。**worker が書くのは `plugins/_staging/` のみ、`reports/` は親が書く**
- `RLIMIT_NPROC` を mission worker に掛けない現状 (変えない)
- `Notifier.send` の握り潰し (起票のまま)

## 10. 起票 (本プランでは直さない)

プラン 9 §6 から引き継ぎ: 外向きリクエスト予算の**全体**設計 (Global Constraints 化・共通出口・datafeed 4 経路・Discord) / `missions.failure_reason` / worker 診断タスク (result フレーム 5 箇所の `error` を親へ) / **improve モデルの `/models` 存在確認** (backend=local の improve が本プランで初めて実ツールを持つ — 実装計画で warn-only の扱いを決めてよい) / plugin `max_bars` × cache 保持期間 / SSE error event / `ohlcv_cache` の物理分離 / キャッシュ→履歴の昇格 / ヘッジ併存。プラン 9 束 D/E から: `Notifier.send` の成功戻り値契約 / `trade_intents` の保持・prune / `reject_category` の StrEnum 化 / db.py の table rebuild 骨格重複 / `_backup_before_migration` のログ文言。

本プランで新規:
- **代替ローカルハーネス候補 (Qwen Code / Aider / OpenCode / Goose)** — 「ローカル LLM でハーネスを回す」目的は codex+llama-swap で満たす方針だが、実機 E2E (§7.2) で codex+llama-swap の plugin 実装力が不足と出たら、Qwen Code (qwen3.x 系の本家ハーネス、モデル相性) / Aider (弱いモデル向けの編集形式で成熟) / OpenCode / Goose を **4 番目の backend 候補として実測**する。§1 の `CliRunner` は「CLI 起動 → JSON 回収」の共通基盤なので追加コストは argv・出力形式・認証の差分に限られる。版・機能・ライセンスは採用検討時に実測で確認 (2026-08-16 ユーザー希望で起票)
- **claude → ローカル LLM 駆動** (R9): 要実測 (llama-swap が Anthropic 形式を受けるか) / 鍵契約の言い換え (「課金エンドポイントへの鍵は渡さない」) / 規約確認。E2E で codex+llama-swap が不足と分かったら再訪
- **codex `apps` egress の停止手段** — 検証・記録は本プラン Task 13 で行う (§1.3/§7.2)。`--disable apps` で止まらなかった場合の停止手段 (別 config key / proxy) はここに残る。argv pin は既知 3 経路の退行防止であり、この egress を「対処済み」にするものではない
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
