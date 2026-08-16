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
| Landlock 拡張 | 「EXECUTE 権の与え方は Task 18 で裁定」(未裁定) | **`execute_paths` を新設** (§2.2)。対象は python・ローダ・CLI ディレクトリ |

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

1. **子プロセス起動**: `subprocess.Popen(argv, cwd=workdir, env=<完全指定>, stdin=<open("/dev/null", O_RDONLY)>, stdout=PIPE, stderr=PIPE, start_new_session=False)`。**`subprocess.DEVNULL` は使わない** — Landlock 下で `/dev` が ro だと Python は `/dev/null` を `O_RDWR` で開けず失敗する (probe §5-③)。`/dev` は §2 で rw にするが、`CliRunner` 単体テストは ro 環境でも動く形にしておく
2. **scratch home**: Mission ごとに `workdir/home/` (`$HOME`) と `workdir/cfg/` (`$CODEX_HOME` または `$CLAUDE_CONFIG_DIR`) を作り、**認証ファイルだけ**をコピーする — codex: `~/.codex/auth.json` / claude: `~/.claude/.credentials.json`。ソースの場所は `runner.codex.auth_file` / `runner.claude.credentials_file` (既定は上記。`~` 展開はサービス側)。**コピー先が更新されても元へ書き戻さない** (トークンリフレッシュが起きた実行では実ファイルが古くなる方向のドリフトが予想される — 起票 §10)。実ファイルは Landlock allowlist に**入れない** (probe P4: strace で実 `~/.codex` への openat 0 件)
3. **env の完全指定** (継承しない。`_mission_worker_env` の allowlist を拡張する形): `PATH=/usr/bin:/bin` (codex が shell snapshot で `/bin/bash` を起動する — probe §3) / `HOME` / `TMPDIR` / `CODEX_HOME` or `CLAUDE_CONFIG_DIR` / `PYTHONPATH` `PYTHONSAFEPATH` (既存)。**`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `AFX_*` / データ資格情報は構造的に存在しない**。pin テスト: 起動 argv と env をキャプチャする fake Popen で「鍵の名前を含む変数がゼロ」を assert
4. **timeout**: `mission.timeout_sec` を壁時計で監視。超過で SIGTERM → `runner.cli_terminate_grace_sec` (既定 10) → `os.killpg(os.getpgid(proc.pid), SIGKILL)`。CLI は親 (mission_worker) と同一 pgid に留まる (probe P9) ので `WorkerRunner._escalate_kill` の外側防衛線も効く。`status="timeout"`
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
  --disable apps                                   # ← probe 未検証 (§10 起票 / 実装計画の実測項目)
  -c mcp_servers.afx.command=<python> -c mcp_servers.afx.args=[…]   # §1.6
  [-c model_providers.llamaswap.base_url=<llama_swap.base_url> -c model_providers.llamaswap.wire_api=responses
   -c model_provider=llamaswap]                    # provider=llama_swap のとき
  -m <runner.improve.model>
```

- **`--disable plugins --disable remote_plugin --disable recommended_plugins` は必須** — 起動時に 11.8MB のプラグインカタログを外部取得し、本番 `RLIMIT_FSIZE=8MB` で `SIGXFSZ` 即死する (probe §5-②)。argv pin テストで固定
- `apps` feature は provider が llama-swap でも `https://chatgpt.com/backend-api/ps/mcp` を叩く (probe §5-②)。`--disable apps` で止まるかは**未検証** — 実装計画で実測し、止まらなければ代替 (config key) を探す。**LLM エンドポイント以外の egress が既定で存在する**事実は §6 の egress pin に含める
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

**変異 (§1)**: env に `ANTHROPIC_API_KEY` を通す (→ pin が落ちる) / scratch home でなく実 `$HOME` を渡す / `--setting-sources ""` を落とす / codex の `--disable plugins…` を落とす (→ argv pin) / `--dangerously-bypass…` を落とす (→ shell 全滅、E2E で検出) / trade+claude の allowedTools に `Bash` を足す (→ pin) / `runner.trade.backend=codex` を通す (→ validator pin) / timeout 後に killpg しない (→ 孫残留テスト) / `parse_json_output` を通さず生 JSON を `json.loads` (→ フェンス付き fake 出力で failed になる契約テスト) / reason に stderr 全文を入れる (→ 安全化契約テスト) / `max_turns` 超過で `completed` を返す (claude fake の `num_turns` 上限テスト)。

---

## 2. improve worker profile の拡張 — 権限境界と不変条件 (裁定①)

### 2.1 不変条件 (受入条件の核。§7-1 で実プロセスに対して測る)

improve worker プロセスとその**全子孫** (claude / codex CLI・MCP シム・pytest・shell) について:

1. `data/` 配下 (DB・履歴・RAG) に読み書きとも到達できない (`open` / `listdir` / `truncate` / `exec` すべて `EACCES`)
2. 書き込み可能パスが **①候補置き場 `<root>/plugins/_staging/<mission_id>/` ②`<root>/reports/` ③workdir (scratch home を含む) ④`/dev`** に閉じる。**`plugins/` 全体・リポジトリ本体・`config/`・`policy/` には書けない**
3. 従量課金経路が無い (env に鍵が無い — §1.1)
4. 個人設定を継承しない (scratch home / `--ignore-user-config` / `--setting-sources ""`)

**「シェルが無いこと」は条件にしない** (プラン 9 §5 の D3 放棄裁定)。

### 2.2 Landlock 配線の変更 (`core/landlock.py` + `mission_worker._bootstrap_improve_profile`)

- `restrict_to(*, read_only_paths, read_write_paths, execute_paths: list[Path] = ())` に **`execute_paths` を追加**。マスクは `_EXECUTE_ACCESS = _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR` (**自己充足** — 同一 inode に対する ro ルールとの併合に依存させない。probe の `landlock_probe.py` と同一)。`_HANDLED_ACCESS_FS` は不変 (EXECUTE は元から handled)
- **execute_paths** = `sys.prefix` (venv), `sys.base_prefix` (uv の cpython), `/usr/lib`, `/usr/lib64` (動的リンクのローダ — claude・python 双方に必須。`/usr/lib64` 単独では不十分、probe §2.2), **選択された backend の CLI 実体ディレクトリ** (`shutil.which(bin)` → realpath の親。node ラッパなら node の親も)。backend=local のときは CLI ディレクトリを入れない
- **read_only 追加**: `/run/systemd/resolve` (外部 DNS。`/etc/resolv.conf` が symlink で Landlock は解決先で判定 — probe §2.1。存在するときのみ) / `/proc` (claude のみ。bun が panic して SIGABRT — probe §2.2)
- **`/dev` を read_only → read_write へ**。理由: `subprocess.DEVNULL`・bash のリダイレクト・pytest logging が `/dev/null` を書込オープンする (probe §5-③、P7)。**脅威分析**: rw マスクに `MAKE_CHAR` は無いのでデバイスノード作成は不可。既存デバイスへの write は Unix パーミッション次第で `/dev/shm` (tmpfs) には書ける — `data/` 到達には寄与しない。`_ACCESS_FS_IOCTL_DEV` は従来どおり handled にしない (プラン 8 の判断を維持)
- **read_write 追加**: `<root>/plugins/_staging/<mission_id>/` (親が spawn 前に mkdir) と `<root>/reports/` (親が起動時に mkdir)。**`<root>/plugins/` 自体は入れない**
- **`_assert_allowlist_excludes_data_dir` を拡張**: 入力を `read_only + read_write + execute` の全部にする。加えて**静的 pin**: `read_write_paths` の集合が `{staging, reports, workdir, /dev}` と一致し、いずれも `<root>` 直下の `plugins/_staging/<id>` と `reports/` 以外に `<root>` 配下を含まないこと (テストは `_bootstrap_improve_profile` が組む allowlist を dry-run で取り出して assert)
- **rlimit**: `child_fsize_mb=8` は維持 (codex は `--disable plugins…` で 664KB が最大 — probe §5-⑧)。**claude を rlimit 下で実 1 ターン回すのは未測** → 実装計画の実測項目 (超えるなら improve のみ `child_fsize_mb` を上げる)
- **env 追加** (`_mission_worker_env` の improve 分岐): `HOME=<workdir>/home`, `TMPDIR=<workdir>/tmp`, `CODEX_HOME=<workdir>/cfg` または `CLAUDE_CONFIG_DIR=<workdir>/cfg`。値は WorkerRunner が workdir を作った後に決まるので、handshake で子に渡すのではなく **Popen の env に直接入れる** (子は Landlock 適用前に mkdir する)

### 2.3 候補置き場 (staging) の意味論

- worker が plugin を書ける唯一の場所は `plugins/_staging/<mission_id>/<name>/`。**稼働中の `plugins/<name>/` は読めるが書けない**
- 根拠: 承認済み plugin をその場で書き換えると content_hash が承認済みハッシュと不一致になり、**承認が下りるまでその plugin は `approved_plugins()` から消える** — 週次の改善が稼働中の指標を止める。staging なら承認までは旧版が生き続ける
- `plugin/loader.discover` は先頭が `_` のディレクトリを無視する (`_staging` を予約。`_reject_unexpected_py_files` の走査も同様)
- ライフサイクル: 親が Mission 起動前に空 dir を作る → worker が書く → commit 相 (§4) でゲート → 承認申請を出した候補は**決定まで残す** → 承認時に git 記録 → 昇格 (§5、この順) → 決定 (approved / rejected / expired / invalidated) 時に削除。承認申請に至らなかった候補は commit 相の末尾で削除
- **孤児の掃除**: 起動時 reconcile (§5.4 の位置) で、`_staging/` 配下のうち「対応する pending の approval_request が無い」ものを削除する
- `.gitignore` の `/plugins/` はそのまま staging も覆う

### 2.4 残余リスク (明記)

improve worker は任意コード実行 + ネットワーク (LLM・研究ツール) を持つ。plugin ソースの信頼モデルは従来どおり「人間承認まで信用しない」。egress は §6 の予算で律する。`/dev/shm` への書込は可能 (上記)。

**変異 (§2)**: `execute_paths` を `_assert_allowlist_excludes_data_dir` に渡さない (→ `data/` を execute に入れても通る、pin が落ちる) / `plugins/` 全体を rw に入れる (→ 静的 pin) / `/dev` を ro に戻す (→ subprocess.DEVNULL 経路の実測テスト) / staging を Mission id で分けず共有にする (→ 並行 Mission の衝突テスト) / `discover` が `_staging` を拾う (→ 承認前 plugin がロードされる pin) / 孤児 reconcile を落とす (→ 起動時テスト) / `HOME` に実ホームを渡す (→ env pin)。

---

## 3. 改善 Mission — 起動・レーン・注入・道具・出力

### 3.1 起動とレーン (R7)

- **起動契機**: `schedule.improve` (`weekly` / `daily`、既存 config で未消費だったキーをここで消費) — scheduler tick が `improve_due(now)` を判定し `improve_supervisor.try_submit(...)`。**手動**: 対話シェル `improve` (即時 1 件)。どちらも missions 行 `loop='improve'`, `trigger` は NULL (設計書 §12 — trigger は trade 専用)
- **別レーン**: 新設 `ImproveSupervisor` (`core/improve_supervisor.py`)。容量 `improve.parallel` (既定 1、上限 4)。**取引レーン (`MissionSupervisor`、容量 1) とは独立** — 改善実行中も取引 Mission は受理される。**preemption はしない** (改善は取引のために中断されない)。実装は `MissionSupervisor` の一般化 (N スロット + `kind="improve"`) でも別クラスでもよいが、**取引レーンの直列性契約 (容量 1・原子的 try_submit) を変えない**ことを受入条件に置く
- **各 Mission は独立した improve worker** (WorkerRunner `worker_profile="improve"`) を持つ。workdir・staging・scratch home は Mission id 単位
- **並行 Mission の課題分担**: 親が起動時に open バックログを `id % N == k` で分割し、Mission k には「あなたの担当分」として渡す (他の分は一覧に載せるが「選ばないこと」と指示)。発見・リサーチで新規に見つけた課題は自由に追加してよい。commit 相で同じ課題を選んだ結果が重なった場合、**先に commit した方を採り、後着は「観察」に落とす** (捨てない)
- **LLM の競合**: backend=local で改善と取引が別 alias を使うと llama-swap がモデルを入れ替える (TTL・swap 遅延)。既定は同一 alias。設定で別 alias にした場合の遅延は受容 (警告を出す)。claude / codex は競合しない
- 週次の tick は取引 Mission と重ならない時間帯 (`schedule.improve_at` = 曜日+時刻、既定 土曜 03:00 表示 TZ) に置く

### 3.2 注入コンテキスト (親が決定論的に集計してプロンプトに焼く)

`loops/improve_context.py` が生成し、`loops/prompts/improve_mission.md` (新規) のテンプレートに差し込む:

| 節 | 内容 | 出所 |
|---|---|---|
| 成績レポート | 直近 30/90 日の勝率・PF・ペア別・時間帯別・却下 intent の内訳 (reject_category 別)・hold 率 | `trade_intents` / `orders` (親の RO 集計) |
| 改善履歴 | 過去の `improvement_runs` (何を試し、approval / report / observation のどれで終わったか)。**各バックログ課題の試行回数と、strategy なら標本 (取引数)** を添える (R8) | `improvement_runs` / `improvement_backlog` / `backtest_runs` |
| 現行構成インベントリ | 組み込み + 承認済み plugin (kind・pairs・timeframe)、ニュースソース一覧、risk gate 現行値 | registry / `approved_plugins` / `news_sources` / settings |
| バックログ | open + observation の一覧 (担当分に印) | `improvement_backlog` |
| ユーザー方針 | `policy/directives.md` 末尾 4000 文字 (全 Mission 共通) | `Policy.tail` |
| 参照 | サンプル plugin の場所 (`docs/examples/plugins/`)・plugin 契約の要約・候補置き場のパス | 定数 |

### 3.3 3 ステップ 1 Mission (設計書 §6 のまま)

1. **発見**: 注入内容から課題を特定
2. **リサーチ**: 研究ツールで外部知識を取り込む
3. **実施**: 1 件を選び、候補置き場に実装・自分でテストを回し、成果物を出力に載せる

### 3.4 道具 (improve registry — 取引 registry と分離)

`build_mission_registry("improve", …)` が返す集合。**取引 registry のツールは 1 つも含まない** (`get_ohlcv` / `get_signals` 等)。

| ツール | 実行場所 | 内容 | 制約 |
|---|---|---|---|
| `web_search(query, max_results)` | worker | ddgs (DuckDuckGo) 検索 | §6 の Mission 予算 |
| `fetch_article(url)` | worker | 記事本文抽出 (`trafilatura`、既存依存。前身 article_fetcher 相当) | §6 の予算 / サイズ上限 / `data/` 不可視のまま |
| `list_staging()` / `read_staging_file(name, rel)` / `write_staging_file(name, rel, content)` | worker | 候補置き場のファイル操作。`name` は plugin 名、`rel ∈ {plugin.py, config.yaml, test_plugin.py}` のみ。パス正規化して staging 外は拒否 | LocalRunner 用。claude/codex はネイティブでも同じ場所しか書けない (Landlock) |
| `read_plugin_source(name)` | worker | 稼働中 plugin の 3 ファイル読取 (改良の起点) | 読取のみ |
| `run_plugin_tests(name)` | worker | `python -m pytest -q -p no:logging <staging>/<name>/test_plugin.py` を subprocess (rlimit 継承・timeout `plugin.pytest_timeout_sec`) | 結果は**参考** — 親が改めて回す (§4) |
| `run_backtest(name, pair)` | **親 RPC** | `holdout.run_in_sample` を親が回し、**集計指標のみ** (取引数・PF・勝率・平均 R・DD。期間端点なし) を返す。`backtest_runs` に `scope=in_sample, issued_by=harness` で保存 | 遮断 1・2 |
| `analyze_corr(request)` | **親 RPC** | 既存 `backtest.analysis.analyze_for_agent` (列挙制パラメータ・固定個数の要約統計) | 遮断 7 |
| **無いもの** | — | `get_signals` / 任意 SQL / `ohlcv_*` 直読 / holdout / `bless` / approval 発行 / backlog 書込 / news 提案 | 遮断 3・6・8、R2、R6 |

RPC は既存 `tool_rpc` フレーム (in-flight 1) に `run_backtest` / `analyze_corr` を追加する。親側 dispatcher は `rpc_timeout_sec` を **RPC 種別ごと**に持つ (バックテストは数十秒〜数分。`improve.backtest_rpc_timeout_sec` 既定 600)。

### 3.5 出力 schema (`loops/summary.py` に `IMPROVE_OUTPUT_SCHEMA`)

```json
{
  "discoveries":  [{"idea": str, "source": "agent"|"research", "evidence": str}],   // 上限 §4.2
  "selected":     {"backlog_id": int|null, "idea": str},                             // 既存 id か新規
  "artifact":     {"type": "plugin", "name": str, "kind": "indicator"|"signal"|"strategy",
                   "self_test": "passed"|"failed"|"not_run", "summary": str}
               |  {"type": "report", "title": str, "body_md": str}
               |  {"type": "observation", "reason": str},
  "analysis_refs": {"analysis_run_ids": [int], "trial_count": int, "selection_rationale": str}
}
```

### 3.6 失敗の扱い

timeout / 出力不正 / worker 異常死 → missions 行を該当 status で終端、`improvement_runs` は `result=NULL` のまま `finished_at` を書く (新しい終端値は足さない — 既存 CHECK `('approval','report')` を維持し、失敗は missions 側で読む)、staging を削除。**再試行はしない** (次の週次で自然に再実行。R8 により課題は消えない)。

**変異 (§3)**: 改善を取引レーンに submit する (→ 取引受理テスト) / 分担を渡さず全件を全 Mission に見せる (→ 重複解決テストが二重採用を検出) / `IMPROVE_FORBIDDEN` のツールが improve registry に居る (→ 既存 pin 拡張) / `run_backtest` が期間端点を返す (→ 返却 schema pin) / `write_staging_file` が `..` を通す (→ パス正規化テスト) / `run_plugin_tests` の結果でゲートを省く (§4 の変異) / 週次判定を落とす (→ scheduler pin)。

---

## 4. 親側 commit 相 — 出力の検証とゲート (`loops/improve_loop.py`)

`ImproveLoop` は `TradeLoop` と同じ prepare / run / commit の三相。prepare (core_lock 内・短時間): missions 行作成・staging mkdir・注入コンテキスト生成。run (lock 外): `WorkerRunner.run`。commit (lock 外、**DB は improve レーン専用接続**、`missions.finish` の CAS で終端):

1. **出力検査**: schema 検証 (runner 側でも済んでいるが親で再検証) / `selected.backlog_id` が実在し open|observation / `artifact.type=plugin` なら `staging/<name>/` が存在し 3 ファイルが揃う。不合格 → Mission `failed`、staging 削除、終わり
2. **バックログ反映**: `discoveries` を追加 (`source` = agent|research)。**正規化 (空白・大小文字) した `idea` の完全一致は重複として捨てる**。**上限 `improve.max_new_backlog_per_mission` (既定 20)** — 超過分は捨てて activity に件数を記録。`selected` を `selected` 状態へ (新規なら追加してから)。**並行 Mission の後着が同じ id を選んでいたら `observation` に落とす** (R7)
   - **新 status `observation`**: 既存の `open|selected|done|rejected` に加える。`improvement_backlog.status` に CHECK は無い (`db.py:168-174`) ので migration 不要。`store/backlog.py` の `list_open` は `open` と `observation` を返す (observation は「再挑戦可」)。`attempts INTEGER NOT NULL DEFAULT 0` と `last_result TEXT` を `ensure_column` で追加し (冪等)、commit 相で `attempts += 1`、`last_result` に終わり方 (approval / report / observation:<reason>) を書く (R8 の履歴材料)
3. **plugin ゲート (artifact.type=plugin)** — 順に、どれか 1 つでも不合格なら**承認申請は出さず**、結果をレポートに残して `selected` を `observation` へ:
   - a. 3 ファイル存在・サイズ上限・`config.yaml` の `kind` 一致・`plugin/loader._validate_config` 相当
   - b. `plugin/sandbox.check_source` (AST allowlist)
   - c. **pytest を Landlock で囲った別プロセスで回す**: 新ヘルパ `plugin/gate_pytest.py:run_gate_pytest(plugin_dir, *, settings) -> GateResult`。実装は `sys.executable -m agentic_fx.plugin.gate_pytest_worker` を `Popen(cwd=<tmp workdir>, env=最小, start_new_session=True)`、子は起動直後に **improve profile と同じ allowlist ファミリ** (`read_only` = code_root/venv/stdlib/`/usr/lib`/zoneinfo/`/etc`, `execute` = venv/base/`/usr/lib`/`/usr/lib64`, `read_write` = tmp workdir + **対象 plugin_dir (`__pycache__` のため)** + `/dev`) で `landlock.restrict_to` → `pytest.main(["-q", "-p", "no:logging", "-p", "no:cacheprovider", str(plugin_dir)])`。rlimit は既存 `_pytest_rlimit_preexec`、timeout は `plugin.pytest_timeout_sec`。**Landlock 不可の環境では improve と同様 fail closed** (ゲート pytest を回せない = 承認申請を出せない)。**`submit_plugin` / `bless` の `_default_pytest_runner` もこのヘルパに置き換える** (人間の plugin にも害はない。`plugin/approval.py:149` の無隔離実行を廃止)
   - d. `content_hash(staging/<name>)` を計算
4. **戦略採用ゲート (kind=strategy のみ)**: 親が `holdout.run_in_sample` を各 pair で回す (worker の `run_backtest` 結果は使わない)。**取引数 < `backtest.min_trades` (30) → 「評価不能」: 承認申請を出さず `observation` (悪いとは記録しない — R8)**。≥30 → `run_holdout_gate` を回し、結果は **approval payload の添付のみ** (改善ループの読取ビューには載せない — 遮断 8)。`indicator` / `signal` はこのゲートを課さない (設計書 §6)
5. **承認申請**: `approvals.create(kind="plugin", payload={name, kind, staging_path, content_hash, mission_id, in_sample: {...}, holdout: {...}|null, baseline: {...}|null, analysis_run_ids, trial_count, selection_rationale, summary})`。**`bless` は経由しない** (改善ループに自己承認経路は無い)
6. **レポート**: `artifact.type=report`、およびゲート不合格・評価不能・observation のとき、`reports/improve-YYYY-MM-DD-<mission_id>.md` を親が整形して書く (見出し・Mission 要約・ゲート結果・添付。**agent の `body_md` は「提案本文」節に引用として入れる — 信用しない**)。`reports/` は起動時に mkdir、gitignore 済み
7. **`improvement_runs.finish`**: `result='approval'` + `approval_id`、または `result='report'` + `report_path`。両方出た場合 (承認申請 + レポート) は `approval` を優先し `report_path` も埋める (列は両方ある)
8. **掃除**: 承認申請を出した staging は残す。それ以外は削除

**失敗の隔離**: commit 相の例外は improve レーンで握り、activity + 通知 (`improve_commit_failed`)。取引レーン・core_lock・資金保護には波及しない。missions 行は必ず終端する (`finally`)。

**変異 (§4)**: worker の `self_test="passed"` を信じて 3c を省く (→ 「worker が passed と言い、親ゲートで落ちる」テスト) / 3c を Landlock 無しで回す (→ ゲート子プロセスから `data/` を読む fake test が通ってしまう pin。killer = ゲート pytest 内で `data/agentic.db` を open して失敗することを assert) / 30 未満を `rejected` にする (→ observation pin) / holdout 結果を Mission 出力や次回注入に含める (→ 遮断 8 pin) / バックログ上限を外す (→ 21 件投入テスト) / 重複解決を落とす (→ 二重承認申請テスト) / `bless` を呼ぶ (→ 承認申請が `pending` であることの pin) / commit 相の例外で missions 行が終端しない (→ CAS finalize pin) / staging を削除しない (→ 掃除テスト)。

---

## 5. 承認時の入れ子 git 記録と昇格 (D6 の再収束) — 記録が先、昇格が後

### 5.1 承認手順 (人間が `approve <id>` した瞬間。シェル / 起動時 reconcile / 将来の REST から。**scheduler スレッドでは決して実行しない**)

**順序の原則: 履歴への記録が先、本番の場所への昇格は後。** git がどう失敗しても稼働中の旧版は消えない (§2.3 の「承認までは旧版が生き続ける」を承認処理の途中まで延長する)。

1. **後発決定の確認 (ⓓ)**: 同一 plugin 名へのより新しい決定 (reject) があれば、この承認は失効 (`invalidated`) — 再試行で復活させない (D4 の順序規則)
2. **ハッシュ再照合 (ⓐ)**: `payload.staging_path` の `content_hash` を再計算し `payload.content_hash` と一致しなければ承認は成立せず **pending のまま** + activity + 通知 (申請〜承認の間に書き換わった)
3. **入れ子 git への記録 (ⓑ) — 候補置き場の内容から、`plugins/<name>/` のパスで**: 下記 5.2 の blob-level plumbing。ここで失敗 (git 不在 / detached / identity 無し / CAS 上限) → **pending のまま + 通知。本番の場所には一切触れていない**
4. **昇格 (atomic swap)**: `plugins/<name>.promote-<approval_id>/` に staging をコピー → 既存 `plugins/<name>/` があれば `plugins/<name>.prev-<approval_id>/` に rename → 新を `plugins/<name>/` に rename → **`content_hash(plugins/<name>/)` を再計算し payload と一致することを確認** → `.prev` を削除。途中失敗・不一致は逆順に戻す (`.prev` があれば戻す) → pending のまま + 通知
5. **`decide(status="approved")`**。ここで初めて `approved_plugins()` に載る (次回の再読込/起動から)
6. staging を削除

**同一 plugin 名への承認は直列化 (ⓒ)** — plugin 名ごとの lock (`threading.Lock` の dict、承認スレッド内)。異なる plugin の並行承認は 5.2 の CAS が守る。

**却下・期限切れ・失効**: staging を削除するだけ。`plugins/<name>/` と履歴には触れない。

**bless (人間が本番の場所を直接編集して即時承認)**: staging を経由せず `plugins/<name>/` の現物に対して 2 (in-place のハッシュ) → 3 (blob の出所が `plugins/<name>/` になるだけで同じ plumbing) → 5。昇格 (4) は無い。ゲート pytest は §4-3c の Landlock ヘルパ。

### 5.2 記録手順 — 専用 index + blob-level plumbing (プラン 9 D6 の確定形を、出所が候補置き場である点に合わせて組み替え)

- **初期化**: `plugins/.git` が無ければ親が `git init` (lazy)。親リポジトリとは独立、gitlink は発生しない (`.gitignore` の `/plugins/` で親から見えない)。**同時に `plugins/.git/info/exclude` に `_staging/` と `*.promote-*/` `*.prev-*/` を書く** — 候補置き場は入れ子リポジトリのワークツリー内にあるが**決して commit しない**。人間が `plugins/` で `git status` を打っても汚れて見えないようにする
- **ポーセリン (`git add` / `git commit`) は使わない** — `git commit -- <path>` はワークツリー内容を取り直すので検証後の書き換えを拾い、pathspec 無しの commit は他 plugin の staged 差分を巻き込む (codex 4 周目 C1 / 3 周目 M1)。加えて本書では**記録時点で `plugins/<name>/` にはまだ新内容が無い** (昇格前) ので、ワークツリーは出所にならない。**blob を候補置き場のファイルから直接作り、専用 index に `plugins/<name>/` のパス名で置く**

```
src  = <staging>/<name>/            # bless では plugins/<name>/ (現物)
ref  = git symbolic-ref HEAD        # unborn でも HEAD が指す ref 名は返る。init.defaultBranch に依存しない
old  = git rev-parse --verify <ref> # 失敗 = unborn (初回)
b_py = git hash-object -w <src>/plugin.py ; b_cfg = … config.yaml ; b_test = … test_plugin.py   # 3 blob を object DB へ
GIT_INDEX_FILE=<tmp> git read-tree <old>                # unborn なら read-tree --empty
GIT_INDEX_FILE=<tmp> git rm --cached -r -q --ignore-unmatch -- <name>/     # 旧版の同 prefix エントリを専用 index から外す (消えたファイルが残らない)
GIT_INDEX_FILE=<tmp> git update-index --add --cacheinfo 100644,<b_py>,<name>/plugin.py   (config.yaml / test_plugin.py も同様)
GIT_INDEX_FILE=<tmp> git cat-file blob :<name>/plugin.py / :<name>/config.yaml → bytes → content_hash_bytes()   # 検証は index の blob に対して。payload.content_hash と一致しなければ中止 (pending)
GIT_INDEX_FILE=<tmp> git write-tree     # → tree。unborn でなく tree == <old>^{tree} なら「変化なし」= commit を作らず成功
git commit-tree <tree> [-p <old>] -m "approve <name> <hash> (approval #id)"   # unborn なら -p なし
git update-ref <ref> <new> <old>        # CAS。unborn は <old> = 空文字。失敗したら頭からやり直し (有限回、超過は pending のまま)
```

- **`_staging/` を stage する経路が構造的に無い** (`git add` を使わない。index に入るのは `update-index --cacheinfo` で明示した `<name>/<file>` の 3 本だけ)
- **`symbolic-ref` の失敗は終了コードで区別**: 1 = detached HEAD (人間が履歴操作中 → 待てば直る) / 128 = リポジトリ障害 (運用者の対処が要る)。どちらも pending だが activity と通知の理由を分ける
- **CAS 無しの `update-ref` は不可**。代替としてリポジトリ単位 lock で全 git 操作を直列化してもよい (実装はどちらでも)
- **content_hash の bytes 版**: `plugin/loader.content_hash(Path)` を `content_hash_bytes(plugin_py: bytes, config_yaml: bytes)` の薄いラッパにする (定義 `sha256(b"plugin.py\0"+p+b"\0config.yaml\0"+c)` は不変)
- **commit identity はサービスが供給**: `GIT_AUTHOR_NAME/EMAIL` / `GIT_COMMITTER_NAME/EMAIL` = `agentic-fx <noreply@localhost>` を env で渡す (利用者の git 設定に依存させない。無いと `commit-tree` が失敗し永久 pending — codex 6 周目 I2)
- git サブプロセスの env は最小 (`PATH`, `HOME` は実ホームでよい — 親プロセス、Landlock 外)。`GIT_DIR` / `GIT_WORK_TREE` を明示し、親リポジトリの `.git` を誤って触らない

### 5.3 git と SQLite は原子化できない — 収束性で担保 (codex I5)

- 「approved になっている plugin は必ず commit 済み」の**一方向**だけを不変条件にする。逆は保証しない
- **git の失敗は稼働中の版を決して壊さない** (記録が昇格に先行するため — 5.1 の順序の原則)。起こりうる中間状態は 2 つだけで、どちらも pending に統一される:
  - **(a) commit 済み・未昇格・pending** (git は成功、昇格 4 で失敗または hash 不一致でロールバック): 旧版は無傷で `approved_plugins()` に載り続ける。再試行は 5.1 を頭から流す — git は tree 一致で no-op、昇格をやり直す
  - **(b) 昇格済み・pending** (昇格まで成功、`decide` の DB 書込だけ失敗 — 窓は極小): 旧版は `plugins/<name>/` から消えており、再試行までは**新旧どちらも load されない**。旧版は入れ子 git の履歴から復元できる。再試行は hash 一致で昇格 no-op → decide のみ。起動時 reconcile (`approved_plugins()` より前) と次の承認操作が拾う
- **再試行の 3 契機**: ①起動時 reconcile ②次の承認操作 ③シェル `approval retry <id>`。**scheduler tick にぶら下げない**。再試行は手順を頭から流す (ⓓ → ⓐ → git は tree 一致なら no-op → 昇格は hash 一致なら no-op → decide)
- **起動時 reconcile の位置**: `init_db` と中断 Mission の回収より後、**`approved_plugins()` より前** (後だとその起動では復旧した承認がロードされない — codex 4 周目 I3)。同時に §2.3 の孤児 staging 掃除を行う。git 不在・reconcile 失敗はサービス起動を止めない (警告 + pending のまま)
- **git 不在**: 起動時検査で警告 (SQLite ≥3.35 assert と同じ扱い)。plugin 承認だけが成立せず、取引は動く

### 5.4 資金保護への非波及

承認スレッド (シェル / 将来 API) と起動シーケンスでのみ実行。**scheduler スレッドから git サブプロセスが呼ばれないこと**を回帰テストで固定 (呼ばれたら fail するシーム — D3' の通知と同じ形。「lock 内か」でなく「どのスレッドか」で検査)。`core_lock` は触らない。

**変異 (§5)**: 決定時ハッシュ再照合を削除 / 照合対象を index の blob から候補置き場のワークツリーへ戻す (TOCTOU 復活) / 専用 index をやめ共有 index + `git add` + `git commit -- <path>` (**killer = 検証後に staging の plugin.py を書き換えてから commit させ、commit 内容が検証済みの内容であることを assert**) / **git 記録より先に昇格する (killer = git 失敗を注入 (identity env 除去 / detached HEAD) して承認させ、`plugins/<name>/` の旧版が無傷で `approved_plugins()` に残ることを assert)** / **昇格後の hash 再検証を落とす (killer = swap 中に `plugins/<name>/plugin.py` を差し替え、ロールバックされ pending のままであること)** / `_staging/` が index に入る (→ 記録後の tree に `_staging` が無い pin) / 旧版の消えたファイルが index に残る (→ `git rm --cached` 落とし: 4 ファイル目を消した再承認で tree から消える pin) / 空 commit 判定を `git diff --cached` に戻す / commit と decide の順序を入れ替える / commit 失敗で承認を成立させる / 後発 reject の確認を削除 / 起動時 reconcile を `approved_plugins()` の後に置く / git を scheduler スレッドから呼ぶ / CAS 無し update-ref (→ 並行承認で先発 commit が消える) / 昇格失敗時に `.prev` を戻さない (→ 旧版消失テスト) / identity env を落とす (→ 空 git config 環境で永久 pending) / bless が Landlock ヘルパでなく `_default_pytest_runner` を使う (→ §4-3c の pin)。

---

## 6. 外向きリクエストの予算 (研究ツールに閉じる) と egress の pin

**全体設計 (Global Constraints 化・共通出口ラッパ・datafeed 4 経路の集約) は本プランではやらない — 起票のまま (§10)。** ここでは**本プランが新たに増やす外向き経路 (研究ツール・CLI 自身の通信) だけ**を律する。「今それが無事なのは設計のおかげか偶然か」の区別を、少なくとも新経路については設計で答える。

- **Mission ごとの予算** (`improve.research` config、`_Strict`):
  - `max_searches` (既定 20) / `max_fetches` (既定 30) / `min_interval_sec` (既定 2.0) / `max_per_host` (既定 5) / `fetch_max_bytes` (既定 2 MiB) / `user_agent` (既定 `agentic-fx/<version> (+https://github.com/<repo>)` — 素性を名乗る)
  - 予算は worker 内のツール実装が数える (Mission = プロセスなので状態はプロセス内で足りる)。使い切ったらツールは `{"error": "budget exhausted"}` を返し、Mission は続く
  - **429 / 503 を受けたホストは同一 Mission 内で以後打ち切り** (再試行しない — 既に絞られた相手に重ねない)。他ホストは続行
  - 既定は未設定でも安全側 (上記) で動く。設定し忘れが連射にならない
- **CLI 自身の egress**: codex は `--disable plugins --disable remote_plugin --disable recommended_plugins --disable apps` を argv pin。claude は `--setting-sources ""` (plugin sync 等)。CLI が LLM エンドポイント以外へ出る通信をゼロにできる保証は無い (probe §5-②: `--disable apps` 未検証) — 実装計画で `strace -e trace=connect` 相当で観測し、残るものは文書化する
- **ネットワーク遮断はしない** (プラン 8 §4.5 の裁定どおり。改善 worker は LLM と研究に外へ出る必要がある)

**変異 (§6)**: 予算カウンタを落とす (→ 21 回目の検索が通る pin) / 429 で再試行する (→ fake サーバで 2 回目のリクエストが飛ぶ pin) / UA を空にする / `--disable plugins` を落とす (§1 の pin と共有)。

---

## 7. 受入条件 (blocking)

**全部が緑になるまで改善ループは有効化しない** (`schedule.improve` の消費と `improve` コマンドは最後の task で配線する)。

1. **遮断 8 項目の統合回帰** (改善 worker の**実プロセス**に対して。**improve registry の task 直後に red で書き始める** — 最後の E2E に置かない): ①`data/agentic.db` の絶対パス open / `data/` 列挙が失敗 ②`run_holdout_gate` を呼んでもデータ到達不能で失敗 ③`ohlcv_history` / `ohlcv_cache` を直読するツールが registry に無い + DB パスが handshake に無い ④**書き込み可能パスが staging・reports・workdir・`/dev` に閉じる** (リポジトリ本体・`plugins/<name>/`・`config/`・`policy/` への write が `EACCES`) ⑤plugin サンドボックスの入力 DataFrame はハーネスが与える (既存 pin 継続) ⑥`get_signals` を含む `IMPROVE_FORBIDDEN` + 取引 registry の全ツールが improve registry に**無い** ⑦`analyze_corr` / `run_backtest` の返却 schema に日時・期間端点・順序付き窓列・観測数が無い ⑧approval の結果として holdout の指標・baseline 差分・閾値別合否が Mission 出力・注入コンテキスト・RPC 返却のどこにも現れない
2. **3 実装 (Local / Claude / Codex) が同一の契約テストスイートに合格** (fake CLI スクリプトで実 LLM を呼ばずに回す。運用で選ばれるのは 1 つ): 4 終端 / reason 安全化 / timeout 優先 (fake が sleep) / schema 不適合 → failed / **子 env に鍵の名前がゼロ・scratch home のみ** / claude の allowedTools が profile で固定 / codex は trade で拒否 / `max_turns` の runner 別セマンティクス
3. **ゲート pytest が柵の中で動く**: ゲート子プロセスから `data/agentic.db` を open する fake test が `EACCES` で失敗する (変異 killer) / `submit_plugin` と `bless` が同じヘルパを通る (無隔離の `_default_pytest_runner` が呼ばれない pin)
4. **D6 変異列** (§5) を全て殺す / **git サブプロセスが scheduler スレッドから呼ばれない**回帰テスト / 承認は git 記録成功後にしか `approved` にならない / **git 失敗を注入しても稼働中の旧版が消えない** (記録が昇格に先行)
5. **改善レーンが取引レーンを塞がない**: 改善 Mission 実行中に `MissionSupervisor.try_submit("trade")` が受理される / 取引レーンの容量 1・直列性の既存 pin が不変
6. **FakeRunner E2E**: 発見 → バックログ追加 (上限・重複) → 候補 → ゲート不合格でレポート止まり (承認申請なし) / ゲート合格で承認申請 (pending) → `approve` で git 記録 → 昇格 → approved (この順) / 30 未満 strategy が observation / 並行 2 Mission の重複選択が後着 observation
7. `MissionResult.status` 4 値・決定論的コア (`risk_gate` / `paper_broker` / `transitions` / `executor` の判定) は diff ゼロ / 既存 2071 テストが壊れない / 新規 config キーは `settings.yaml.example` と同期 / migration (`improvement_backlog` の列追加) は空 DB・既存 DB で冪等
8. **実機 E2E (実装計画の実測項目、blocking ではないが既定見直しの材料)**: 3 backend それぞれで「サンプル indicator plugin 1 本を候補置き場に実装し、親ゲートを通す」を実測。あわせて claude の rlimit 下 1 ターン / node ラッパ経由 codex / `--disable apps` の egress / `--setting-sources ""` 下の init イベント / auth ローテーション有無 を記録

---

## 8. task 一覧・依存・並列束 (概略 — 詳細は実装計画で)

| 束 | # | task | 由来 | 依存 |
|---|---|---|---|---|
| **A** (runner) | 1 | `CliRunner` 共通基盤 + factory + config schema (`^(local\|claude\|codex)$`, `runner.claude/codex`, trade=codex 拒否) + 起動時検査 | §1.1/1.4 | — |
| A | 2 | `ClaudeRunner` + fake CLI 契約テスト | §1.2 | 1 |
| A | 3 | `CodexRunner` (provider 2 択) + fake CLI 契約テスト + argv pin | §1.3 | 1 |
| A | 4 | MCP stdio シム + mission_worker 側 dispatcher (Unix socket) | §1.6 | 1 |
| **B** (柵) | 5 | `landlock.execute_paths` + `_bootstrap_improve_profile` 拡張 (`/dev` rw, `/proc`, resolve, staging, reports) + assert 拡張 + env 追加 + `/dev/null` O_RDONLY | §2 | — |
| B | 6 | Landlock ゲート pytest ヘルパ + `submit_plugin` / `bless` の置換 | §4-3c | 5 |
| **C** (registry) | 7 | improve registry (研究ツール + 予算 / staging ファイル / `run_plugin_tests` / RPC 2 種) + **遮断 8 項目の統合回帰を red で開始** | §3.4/§6 | 5 |
| C | 8 | バックログ拡張 (`observation` / attempts / last_result) + 注入コンテキスト生成 + prompt | §3.2/§4-2 | — |
| **D** (loop) | 9 | `ImproveSupervisor` (N スロット) + scheduler 週次判定 + `improve` / `improve add` / `backlog` / `policy add` コマンド (配線は最後) | §3.1 | — |
| D | 10 | `ImproveLoop` (三相 + commit 相ゲート + 承認申請 + レポート + improvement_runs) | §4 | 7, 8, 6 |
| **E** (承認) | 11 | 入れ子 git 記録 (blob-level plumbing、staging 出所) → staging 昇格 (atomic swap + hash 再検証) + reconcile + `approval retry` + bless 経路 | §5 | 6 |
| **F** | 12 | FakeRunner E2E + 遮断 8 項目の完了 + `schedule.improve` 配線 (有効化) | §7 | 全部 |
| F | 13 | 実機 E2E (3 backend、実測項目) + 既定見直し提案 | §7-8 | 12 |

- **A / B / C-8 / D-9 は worktree 並列**可。C-7 は B-5 の後。D-10 は C・B-6 の後。E-11 は B-6 の後 (A と並列可)。F は最後
- ファイル競合: `mission_worker.py` (A-4 / B-5) は**同一ファイル** — A-4 を B-5 の後に直列 / `plugin/approval.py` (B-6 / E-11) は順序依存 / `store/db.py` は C-8 のみ / `service.py` (A-1 起動時検査 / D-9 / E-11 reconcile) はマージ順に注意

---

## 9. 変えないもの

- 決定論的コアの判定ロジック / drawdown kill switch / 取引レーンの容量 1・直列性・preemption 契約
- `MissionResult` の 4 値 / `Mission` のフィールド / `LocalRunner` の tool-calling loop とリトライ規律
- trade profile の権限境界 (RO DB + 資格情報 env。Landlock 任意) — ClaudeRunner を trade で選んでも MCP ツールのみ
- `IMPROVE_FORBIDDEN` の pin / 遮断 8 項目の意味論 / holdout の所有 (ハーネス) / `backtest.holdout_months` はコア所有
- plugin 機構の契約 (3 ファイル・純関数・AST allowlist・サンドボックス実行・content_hash の定義)
- 改善ループの出力先が gitignore 領域 (`plugins/`・`reports/`) に閉じること (2026-08-08 裁定)
- `Notifier.send` の握り潰し (起票のまま)

## 10. 起票 (本プランでは直さない)

プラン 9 §6 から引き継ぎ: 外向きリクエスト予算の**全体**設計 (Global Constraints 化・共通出口・datafeed 4 経路・Discord) / `missions.failure_reason` / worker 診断タスク (result フレーム 5 箇所の `error` を親へ) / **improve モデルの `/models` 存在確認** (backend=local の improve が本プランで初めて実ツールを持つ — 実装計画で warn-only の扱いを決めてよい) / plugin `max_bars` × cache 保持期間 / SSE error event / `ohlcv_cache` の物理分離 / キャッシュ→履歴の昇格 / ヘッジ併存。プラン 9 束 D/E から: `Notifier.send` の成功戻り値契約 / `trade_intents` の保持・prune / `reject_category` の StrEnum 化 / db.py の table rebuild 骨格重複 / `_backup_before_migration` のログ文言。

本プランで新規:
- **代替ローカルハーネス候補 (Qwen Code / Aider / OpenCode / Goose)** — 「ローカル LLM でハーネスを回す」目的は codex+llama-swap で満たす方針だが、実機 E2E (§7-8) で codex+llama-swap の plugin 実装力が不足と出たら、Qwen Code (qwen3.x 系の本家ハーネス、モデル相性) / Aider (弱いモデル向けの編集形式で成熟) / OpenCode / Goose を **4 番目の backend 候補として実測**する。§1 の `CliRunner` は「CLI 起動 → JSON 回収」の共通基盤なので追加コストは argv・出力形式・認証の差分に限られる。版・機能・ライセンスは採用検討時に実測で確認 (2026-08-16 ユーザー希望で起票)
- **claude → ローカル LLM 駆動** (R9): 要実測 (llama-swap が Anthropic 形式を受けるか) / 鍵契約の言い換え (「課金エンドポイントへの鍵は渡さない」) / 規約確認。E2E で codex+llama-swap が不足と分かったら再訪
- `--disable apps` が codex の chatgpt.com egress を止めるかの検証 (止まらなければ config key を探す)
- claude を本番 rlimit (`fsize 8MB`) 下で実ターン (probe 未測)
- codex+llama-swap の schema 安定性 (フェンス付き応答。E2E で計測、`response_parser` で足りなければ再出力プロンプトを検討)
- 認証ファイルのコピー・ドリフト (トークンリフレッシュが scratch 側だけに落ちる)。長期運用で「コピーが古くなる」方向。対処候補: 実ファイルの mtime 監視 / 期限切れ時の明示エラー
- MCP シムのプロトコル版追随 (claude / codex が要求する MCP バージョン)
- `ImproveSupervisor` の heartbeat / watchdog 統合 (取引レーンの `busy_since` 相当を改善レーンにも持つか)
- 改善 Mission の transcript 保存量 (CLI のストリームは LocalRunner より多弁。`transcript_max_bytes` の improve 別設定)

## 11. レビュー方針

**設計レビューは codex 単独の反復**。新規設計を含む文書は 7 周を見込む (プラン 9 実績)。毎周「文書全体の自己矛盾の総ざらい」を依頼に含め、3 周目以降は指摘のあった節を通しで書き直す (パッチしない)。指摘がツールの実装詳細 (CLI の引数名・MCP のフィールド) に降りてきたら実装計画へ送る判断をレビュアーに併せて求める。実装計画のレビューは別途 (設計収束を計画の品質の根拠にしない)。
