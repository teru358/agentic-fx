# [ops-first-contact-fixes] 実装プラン v1.0

設計書: `docs/superpowers/specs/2026-09-21-ops-first-contact-fixes-design.md` v1.0。
対象コードは main `ceedd1d` の現物。**実装は codex (terra/medium) に渡す前提** — 各 task は
「failing test を先に固定 (テスト名・観測点・逐語の期待値) → 本体の最小変更」までを codex に渡し、
**red/green の実測とフルスイート・変異確認は指揮者側の subagent が受ける** (codex は read-only sandbox で
pytest を回せないことがあるため)。

## Global Constraints

- **設計を変えない。** 設計書 v1.0 が正。設計書に無い判断が要るときは実装を止めて申告
  ([[plan-code-defects-not-implementer-defects]])
- **秘密 env の守りを弱めない。** `_SECRET_ENV_PATTERNS` (`service.py:200-201`) は 1 文字も変えない。
  T2 が足す allowlist は**名前の完全一致のみ**で、パターン・正規表現・前方一致を許す実装にしない
  (`k in allowlist` の単純な集合所属判定)。allowlist が空 (既定) のときの挙動が現状と完全に同じである
  ことを既存 pin 全数の回帰確認で担保する (AC-2b)
- **遮断 8 を破らない。** T3 が足す echo-back・警告文言は `Commands.dispatch` の戻り値 (端末表示) にのみ
  書く。`improvement_backlog.last_result` / `approval_requests.reason` など改善プロンプト注入経路の
  DB 列に新しい文字列を書き込まない ([[human-reject-reason-leaks-to-improve-prompt]])
- **`.env` を読まない、env の値を読まない。** T2 の allowlist は変数**名**の集合。テストも
  `read_initial_env_names` の seam (既存、`service.py:271-273`) を fake で差し替えて確認し、実 env は読まない
- **実 DB (`data/agentic.db`)・実 `plugins/`・実 `policy/`・実 `config/settings.yaml` を読み書きしない。**
  全テストは `tmp_path` 上の sqlite・ダミーディレクトリを使う ([[tests-touching-real-repo-resources]])。
  T2 で `config/settings.yaml.example` に足す新規キーは、**ユーザーの個人 `config/settings.yaml`
  には実装 task が書き込まない** (設計書 §6「settings.yaml.example の同期」)
- **既存テストの書き換えは 0 本。** 3 task とも新規テストの追記のみ (置換・削除はしない)。
  T2 は既存 pin (`test_check_service_initial_env_has_no_secrets_rejects_each_pattern` 等) の
  **assert・parametrize を 1 つも変えない** — 挙動が変わっていないことをこの不変で示す
- **未コミットの差分の上で `git checkout` / `git restore` を使わない。** 逆変異の復元は `cp` 退避で行う
  ([[no-git-checkout-over-uncommitted-subagent-work]])
- **出力を `| grep` / `| head` に通して途中終了させない。** pytest の結果は最後まで読む
- **一時ファイルは `tmp/` か scratchpad のみ。** `rm` は自分が同セッションで作った一時領域のみ
  ([[rm-allowed-directories]])
- **逐語転写は機械 diff する。** 本文のコードブロックをコピーしたら `diff` で 0 を確認してから次へ進む
  ([[transcription-must-be-machine-diffed]])

## プラン規約

- 各 task は「テストを置く → **red の逐語確認** → 実装 → **green** → **逆変異** → commit」の順
- 逆変異は適用可能な形で書く (置換前の行 / 置換後の行 / 対象ファイル / red になるべきテスト名)。
  **リストは下限であって上限ではない** ([[mutation-testing]])
- codex への切り出しは「テストと本体のコードを書くところまで」。red/green の実行結果・フルスイート・
  逆変異の実走は指揮者側の subagent が引き取る (コード自体は codex が機械的に当てられる粒度の diff で渡す)
- 起草者 (このプラン) は未実測の箇所を明示申告している — 末尾の「未実測の申告」

## File Structure

| ファイル | 変更 | task |
|---|---|---|
| `src/agentic_fx/service.py` | `Commands(...)` に `policy_path=` 追加 (T1) / `_check_service_initial_env_has_no_secrets` の allowlist・`which`・`backend`・パターン引き渡し + 呼び出し元変更 (T2) | T1・T2 |
| `src/agentic_fx/config.py` | `ServiceSettings` 新設 + `Settings.service` フィールド (T2) | T2 |
| `src/agentic_fx/commands.py` | `improve add` の echo-back + 警告 (T3) | T3 |
| `config/settings.yaml.example` | `service.secret_env_allowlist` 追記 (T2) | T2 |
| `tests/test_service_app.py` | **追記**: AC-1a〜AC-1c (T1) / AC-2a〜AC-2e (T2) | T1・T2 |
| `tests/commands/test_improve_commands.py` | **追記**: AC-3a〜AC-3e (T3) | T3 |

## 受入条件 (設計書 §5) と task の対応

| AC | task | テスト名 (予定) |
|---|---|---|
| AC-1a / AC-1b | T1 | `test_build_app_wires_policy_path` / `test_build_app_wired_policy_path_can_append_via_dispatch` |
| AC-1c | T1 | `test_commands_optional_params_all_wired_by_build_app` |
| AC-2a / AC-2b / AC-2e | T2 | `test_check_service_initial_env_has_no_secrets_allowlist_excludes_exact_name` / `test_check_service_initial_env_has_no_secrets_rejects_each_pattern` (無改変、回帰) / `test_check_service_initial_env_has_no_secrets_allowlist_is_exact_match_only` |
| AC-2c / AC-2d | T2 | `test_check_service_initial_env_has_no_secrets_message_includes_which_backend_and_pattern` |
| AC-3a / AC-3c | T3 | `test_improve_add_echoes_the_registered_idea_text` |
| AC-3b | T3 | `test_improve_add_warns_on_short_or_placeholder_idea` |
| AC-3d | T3 | `test_improve_add_does_not_block_registration_when_warned` |
| AC-3e | T3 | `test_improve_add_warning_text_does_not_leak_to_backlog_last_result` |

## task 依存図

T1・T2・T3 は互いにファイルが重ならない、**完全並列** (worktree 分離可):
- T1: `service.py` の `Commands(...)` 呼び出し行 (`:1054-1060`) 周辺のみ
- T2: `service.py` の `_check_service_initial_env_has_no_secrets`/`_check_cli_backend` (`:200-374`) + `config.py` + `config/settings.yaml.example`
- T3: `commands.py` の `improve add` 分岐 (`:215-223`) のみ

**T1 と T2 は同じ `service.py` を触るが別関数・別行域**。統合時にマージ衝突しないよう、
先に統合した側を正として後発側がリベースする (節目確認 1 回)。

---

## T1: `policy_path` 配線 + 構造的シグネチャ突合テスト

**担当**: 設計書 §5「件 1」。`commands.py`/`config.py` を触らないので T2・T3 と並列可。

### Step 1-a: テストを置いて red を確認する

- [ ] `tests/test_service_app.py` に以下 2 本を追記する (`test_build_app_wires_everything` の直後が適切):

```python
def test_build_app_wires_policy_path(tmp_path):
    """[ops-first-contact-fixes] T1 (AC-1a): build_app が Commands に
    policy_path を渡していないと、本番の `policy add` が常に未配線エラーを
    返す (2026-09-20 実機で発覚)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert app.commands._policy_path == tmp_path / "policy" / "directives.md"
    finally:
        app.close()


def test_build_app_wired_policy_path_can_append_via_dispatch(tmp_path):
    """[ops-first-contact-fixes] T1 (AC-1b): build_app 経由の Commands で
    `policy add` を叩くと実際に directives.md へ追記される (配線の到達性)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        result = app.commands.dispatch("policy add こんにちは")
        assert result == "policy に追記しました"
        text = (tmp_path / "policy" / "directives.md").read_text(encoding="utf-8")
        assert "こんにちは" in text
    finally:
        app.close()


def test_commands_optional_params_all_wired_by_build_app(tmp_path):
    """[ops-first-contact-fixes] T1 (AC-1c、構造的配線テスト):
    Commands.__init__ のオプション引数が増えたのに build_app 側が追随し
    忘れる再発 ([[verify-integration-not-just-units]]) を機械的に検出する。
    新しいオプション引数を足したら OPTIONAL_PARAM_TO_ATTR にも追記しないと
    このテスト自体が red になる — 対応表の更新を強制する。"""
    import inspect

    from agentic_fx.commands import Commands

    OPTIONAL_PARAM_TO_ATTR = {
        "health_latch": "health_latch",
        "improve_supervisor": "improve_supervisor",
        "policy_path": "_policy_path",
        "plugins_root": "plugins_root",
        "settings": "settings",
    }
    sig = inspect.signature(Commands.__init__)
    optional_params = {
        name for name, p in sig.parameters.items()
        if name != "self" and p.default is not inspect.Parameter.empty
    }
    assert optional_params == set(OPTIONAL_PARAM_TO_ATTR), (
        "Commands.__init__ のオプション引数と OPTIONAL_PARAM_TO_ATTR が "
        "食い違っている — 新規引数を追記するか、削除された引数を消すこと")

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        for param, attr in OPTIONAL_PARAM_TO_ATTR.items():
            value = getattr(app.commands, attr)
            assert value, (
                f"Commands.{attr} (build_app 経由、引数名 {param}) が "
                f"falsy = 未配線の疑い: {value!r}")
    finally:
        app.close()
```

- [ ] red を確認する (**着手前検証で実走、逐語を貼る**):
      `test_build_app_wires_policy_path` / `test_build_app_wired_policy_path_can_append_via_dispatch` は
      `AssertionError: assert None == PosixPath('.../policy/directives.md')` 系、
      `test_commands_optional_params_all_wired_by_build_app` は
      `AssertionError: Commands._policy_path (build_app 経由、引数名 policy_path) が falsy = 未配線の疑い: None`
      → **(実測して埋める。指揮者側 subagent の担当)**

### Step 1-b: 実装を転写する

```diff path=src/agentic_fx/service.py
--- a/src/agentic_fx/service.py
+++ b/src/agentic_fx/service.py
@@ -1054,7 +1054,9 @@
         commands = Commands(conn=conn_shell, state_store=state,
                             broker=shell_broker,
                             trade_loop=_SupervisorAsk(supervisor, ask_wait_timeout_sec),
                             activity=activity, log_dir=root / "logs", clock=clock,
                             health_latch=health_latch,
                             improve_supervisor=improve_supervisor,
+                            # [ops-first-contact-fixes] T1: policy_path が
+                            # 未配線だと本番の `policy add` が常に「path が
+                            # 未配線です」を返す (2026-09-20 実機発覚)。
+                            # 取引側の Policy (上の policy 変数、:866 付近) と
+                            # 同じパスを渡す。
+                            policy_path=root / "policy" / "directives.md",
                             plugins_root=plugins_dir, settings=settings)
```

(実際の `git diff` 適用時は既存行 `plugins_root=plugins_dir, settings=settings)` の直前に
`policy_path=root / "policy" / "directives.md",` を 1 行差し込む形になる — 上記 diff の hunk 番号は
参考、codex は現物の行番号で当てること)

### Step 1-c: green

- [ ] 3 本とも pass することを確認 (**実測して埋める**)

### Step 1-d: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T1-M1 | `policy_path=root / "policy" / "directives.md",` | (この行ごと削除) | `test_build_app_wires_policy_path` / `test_build_app_wired_policy_path_can_append_via_dispatch` / `test_commands_optional_params_all_wired_by_build_app` |
| T1-M2 | `OPTIONAL_PARAM_TO_ATTR = {...}` (5 エントリ) | `policy_path` のエントリだけ削除した 4 エントリ版 | `test_commands_optional_params_all_wired_by_build_app` (集合比較 `optional_params == set(OPTIONAL_PARAM_TO_ATTR)` が false) |
| T1-M3 (この束で一番大事な変異、「次の再発」の模擬) | `Commands.__init__` にダミーのオプション引数 (例 `dummy_flag: bool = False`) を 1 個追加し、`build_app` の `Commands(...)` 呼び出しに**対応する行を足さない** | (上記の追加のみ) | `test_commands_optional_params_all_wired_by_build_app` (対応表とシグネチャの集合が食い違う) |

- [ ] commit: `fix(ops-first-contact): build_app が Commands に policy_path を渡すよう配線 + 構造的配線テスト (T1)`

---

## T2: 起動時検査⑤の allowlist + `which`/`backend`/パターンを含むメッセージ

**担当**: 設計書 §5「件 2」。`commands.py` を触らないので T1・T3 と並列可。`service.py` は触るが
T1 とは別関数 (`_check_service_initial_env_has_no_secrets`/`_check_cli_backend`)。

### Step 2-a: テストを置いて red を確認する

- [ ] `src/agentic_fx/config.py` に `ServiceSettings` を追加する前提で、`tests/test_service_app.py` に以下を追記する:

```python
def test_check_service_initial_env_has_no_secrets_allowlist_excludes_exact_name():
    """[ops-first-contact-fixes] T2 (AC-2a): settings.service.secret_env_allowlist
    に完全一致で載っている名前は誤検知から除外される。"""
    from agentic_fx.config import Settings
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    settings = _settings_with_allowlist(["CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"])
    _check_service_initial_env_has_no_secrets(
        settings, which="trade", backend="codex",
        read_initial_env_names=lambda: {
            "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS", "HOME"})
    # 例外が出なければ pass


def test_check_service_initial_env_has_no_secrets_allowlist_is_exact_match_only():
    """[ops-first-contact-fixes] T2 (AC-2e): allowlist は完全一致のみ。
    近い名前 (末尾に _V2 等) までは除外しない — パターン化への後退を防ぐ。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    settings = _settings_with_allowlist(["CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"])
    with pytest.raises(RuntimeError, match="secret"):
        _check_service_initial_env_has_no_secrets(
            settings, which="trade", backend="codex",
            read_initial_env_names=lambda: {
                "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS_V2", "HOME"})


def test_check_service_initial_env_has_no_secrets_message_includes_which_backend_and_pattern():
    """[ops-first-contact-fixes] T2 (AC-2c/AC-2d): エラー文言が実際の
    which/backend/当たったパターン名を含む (旧: 'improve+claude' 固定で
    trade+codex でも同じ文言が出ていた、2026-09-20 実機発覚)。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    settings = _settings_with_allowlist([])
    with pytest.raises(RuntimeError) as exc_info:
        _check_service_initial_env_has_no_secrets(
            settings, which="trade", backend="codex",
            read_initial_env_names=lambda: {
                "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS", "HOME"})
    msg = str(exc_info.value)
    assert "trade+codex" in msg
    assert "OPENAI_" in msg
    assert "secret_env_allowlist" in msg
```

  併せて `tests/test_service_app.py` の既存呼び出し 6 本 (`_check_service_initial_env_has_no_secrets(
  object(), read_initial_env_names=...)` の形) を、**新しいシグネチャに合わせて呼べるよう** `which`/
  `backend` にデフォルト値を持たせる (下記 Step 2-b で `which: str = "improve"`, `backend: str = "claude"`
  をキーワード引数の既定にすることで、既存呼び出しは無改変のまま通る設計にする — 「既存テストの書き換えは
  0 本」の Global Constraints を守るため)。ヘルパー `_settings_with_allowlist(names: list[str])` は
  同ファイル内に小さく追加する (`Settings` の最小構成に `service=ServiceSettings(secret_env_allowlist=names)`
  を足すだけ、他フィールドは既存の `_init`/fixture の設定値を流用)。

- [ ] red を確認する (**実測して埋める**): allowlist テスト 2 本は `AttributeError:
      'Settings' object has no attribute 'service'` (config.py 未変更の間) または
      `TypeError: _check_service_initial_env_has_no_secrets() got an unexpected keyword argument 'which'`
      (シグネチャ変更前)、メッセージテストは `AssertionError: assert 'trade+codex' in "improve+claude backend refuses..."`

### Step 2-b: 実装を転写する

```diff path=src/agentic_fx/config.py
--- a/src/agentic_fx/config.py
+++ b/src/agentic_fx/config.py
@@ -318,6 +318,14 @@
 class LoggingSettings(_Strict):
     level: str = "INFO"
 
 
+class ServiceSettings(_Strict):
+    # [ops-first-contact-fixes] T2: 起動時検査⑤
+    # (_check_service_initial_env_has_no_secrets) が誤検知した env 変数名を
+    # 個別に除外するための明示リスト。名前の完全一致のみ (パターン不可) —
+    # 既定は空で、検査の守りは何も変わらない (ユーザーが 1 個ずつ宣言する)。
+    secret_env_allowlist: list[str] = Field(default_factory=list)
+
+
 class ApiSettings(_Strict):
     enabled: bool = False
     host: str = "127.0.0.1"
     port: int = 8420
```

```diff path=src/agentic_fx/config.py
--- a/src/agentic_fx/config.py
+++ b/src/agentic_fx/config.py
@@ -459,6 +459,7 @@
     improve: ImproveSettings = Field(default_factory=ImproveSettings)
     logging: LoggingSettings
     api: ApiSettings
+    service: ServiceSettings = Field(default_factory=ServiceSettings)
     discord: DiscordSettings
     analysis: AnalysisSettings
```

```diff path=src/agentic_fx/service.py
--- a/src/agentic_fx/service.py
+++ b/src/agentic_fx/service.py
@@ -270,25 +270,40 @@
 def _check_service_initial_env_has_no_secrets(
-        settings, *,
+        settings, *, which: str = "improve", backend: str = "claude",
         read_initial_env_names=_read_proc_self_environ_names) -> None:
     """検査⑤ (設計書 §1.4、裁定 R3): サービス自身の**初期 env**
     (`/proc/self/environ` 相当。既定 seam = `_read_proc_self_environ_names`) に
     秘密名パターンがあれば起動拒否する。`.env`→`load_dotenv()` で
     `os.environ` にのみ現れるキーは対象外 (設計が明示的に許容している —
     exported shell env にだけ秘密を置くな、という検査)。`settings` は
-    呼び出し規約を他の `_check_*` 検査と揃えるために受け取るのみで、
-    現状は未使用。"""
+    `settings.service.secret_env_allowlist` (名前の完全一致のみ) を読む。
+    `which`/`backend` はエラーメッセージに実際の呼び出しコンテキストを
+    出すためだけに使う ([ops-first-contact-fixes] T2、2026-09-20 実機発覚:
+    旧実装は文言が常に 'improve+claude' 固定で trade+codex 拒否時も同じ
+    文言が出ていた)。"""
     names = read_initial_env_names()
+    allowlist = set(settings.service.secret_env_allowlist)
     # #94 (verified-round1.md 1-B): `_SECRET_ENV_PATTERNS` は大文字のみ
     # なので、照合前に `k.upper()` を掛けて小文字/混在の env 名 (`my_api_key`
     # 等) も検出する。
-    leaked = [k for k in names
-              if any(pat in k.upper() for pat in _SECRET_ENV_PATTERNS)]
+    def _matched_pattern(k: str) -> str | None:
+        upper = k.upper()
+        for pat in _SECRET_ENV_PATTERNS:
+            if pat in upper:
+                return pat
+        return None
+
+    leaked = []
+    matches: dict[str, str] = {}
+    for k in names:
+        # allowlist は完全一致のみ (パターン化しない — 守りを弱めない)
+        if k in allowlist:
+            continue
+        pat = _matched_pattern(k)
+        if pat is not None:
+            leaked.append(k)
+            matches[k] = pat
     if leaked:
+        patterns_hit = sorted(set(matches.values()))
         raise RuntimeError(
-            "improve+claude backend refuses to start: service initial env "
+            f"{which}+{backend} backend refuses to start: service initial env "
             f"contains secret-like variable name(s) {leaked!r} — improve "
+            f"worker can read /proc/self/environ of same-UID processes "
+            f"(R10) (matched pattern(s) {patterns_hit!r}). Put secrets in "
+            f".env, not exported shell env. If a name is NOT a secret, "
+            f"either unset it before starting the service, or add its "
+            f"exact name to service.secret_env_allowlist in settings.yaml.")
-            "worker can read /proc/self/environ of same-UID processes "
-            "(R10). Put secrets in .env, not exported shell env.")
```

```diff path=src/agentic_fx/service.py
--- a/src/agentic_fx/service.py
+++ b/src/agentic_fx/service.py
@@ -370,7 +370,7 @@
         settings = settings.model_copy(update={"runner": settings.runner.model_copy(
             update={"opencode": settings.runner.opencode.model_copy(
                 update={"bin": str(bin_path)})})})
-    _check_service_initial_env_has_no_secrets(settings)
+    _check_service_initial_env_has_no_secrets(settings, which=which, backend=backend)
     return settings
```

**注**: 上の 3 つの diff hunk はいずれも `src/agentic_fx/service.py` の同一ファイル内なので、
codex には 1 ファイル 2 箇所の変更としてまとめて渡してよい。`_matched_pattern`/`matches` の導入は
「当たったパターン名をメッセージに出す」(AC-2d) のための最小限の追加ロジックで、**`_SECRET_ENV_PATTERNS`
自体や既存の `any(pat in k.upper() ...)` の判定条件は変えていない** (Global Constraints 遵守の確認点)。

### Step 2-c: green + 既存 pin の無改変回帰確認

- [ ] 新規 3 本が green、かつ **`tests/test_service_app.py` の検査⑤関連の既存テスト全部
      (`test_check_service_initial_env_has_no_secrets_rejects_leaked_key_via_seam` /
      `_passes_when_seam_clean` / `_default_seam_is_proc_self_environ` /
      `_rejects_each_pattern` (parametrize 5 件) / `_rejects_lowercase_name` /
      `test_build_app_rejects_when_service_initial_env_has_secret_pattern` /
      `test_build_app_does_not_check_secret_env_when_improve_backend_is_local` /
      `test_check_service_initial_env_has_no_secrets_is_called_for_opencode_backend`) が
      **無改変のまま green** であることを確認する (AC-2b の回帰確認、実測して埋める)

### Step 2-d: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T2-M1 | `if k in allowlist: continue` | (この分岐ごと削除) | `test_check_service_initial_env_has_no_secrets_allowlist_excludes_exact_name` |
| T2-M2 | `if k in allowlist:` | `if any(k.startswith(a) for a in allowlist):` (前方一致に緩める) | `test_check_service_initial_env_has_no_secrets_allowlist_is_exact_match_only` |
| T2-M3 | `f"{which}+{backend} backend refuses..."` | `"improve+claude backend refuses..."` (固定文言に戻す) | `test_check_service_initial_env_has_no_secrets_message_includes_which_backend_and_pattern` |
| T2-M4 | `f"(matched pattern(s) {patterns_hit!r})."` | (この句を削除) | `test_check_service_initial_env_has_no_secrets_message_includes_which_backend_and_pattern` (`"OPENAI_" in msg` が false) |
| T2-M5 | `_check_service_initial_env_has_no_secrets(settings, which=which, backend=backend)` | `_check_service_initial_env_has_no_secrets(settings)` (呼び出し元で `which`/`backend` を渡さない) | `test_build_app_rejects_when_service_initial_env_has_secret_pattern` 系が既定値 `"improve"`/`"claude"` を使ってしまい、trade+codex 経路のメッセージ確認テストが red (T2-b の呼び出し元変更が効いているかの killer) |

- [ ] commit: `fix(ops-first-contact): 起動時検査⑤に secret_env_allowlist + which/backend/パターンを含むメッセージ (T2)`

---

## T3: `improve add` の echo-back + 非ブロッキング警告

**担当**: 設計書 §5「件 3」。`service.py`/`config.py` を触らないので T1・T2 と並列可。

### Step 3-a: テストを置いて red を確認する

- [ ] `tests/commands/test_improve_commands.py` に以下を追記する (既存 `test_policy_add_appends_to_directives_file` 付近):

```python
def test_improve_add_echoes_the_registered_idea_text(commands):
    """[ops-first-contact-fixes] T3 (AC-3a/AC-3c): 登録直後に課題文の原文が
    戻り値へそのまま出る。通常の長さ・非プレースホルダでは警告が付かない。"""
    cmds = commands
    result = cmds.dispatch("improve add USDJPY のスプレッドが広い時間帯の指値精度を上げたい")
    assert "backlog #" in result
    assert "「USDJPY のスプレッドが広い時間帯の指値精度を上げたい」" in result
    assert "⚠" not in result


def test_improve_add_warns_on_short_or_placeholder_idea(commands):
    """[ops-first-contact-fixes] T3 (AC-3b): 可視 4 文字未満、または
    <…>/[…] で完全に囲まれた idea には非ブロッキング警告が付く
    (2026-09-20 実機: `improve add <案1>` → backlog #77 →
    改善 mission #86 が誤解釈して戦略を作った、の再発防止)。"""
    cmds = commands
    result_placeholder = cmds.dispatch("improve add <案1>")
    assert "「<案1>」" in result_placeholder
    assert "⚠" in result_placeholder

    result_short = cmds.dispatch("improve add ab")
    assert "⚠" in result_short

    result_bracket = cmds.dispatch("improve add [TODO]")
    assert "⚠" in result_bracket


def test_improve_add_does_not_block_registration_when_warned(commands):
    """[ops-first-contact-fixes] T3 (AC-3d): 警告が出ても登録は成立する
    (status='open' のまま、拒否しない — R3 の裁定)。"""
    cmds = commands
    result = cmds.dispatch("improve add <案1>")
    bid = int(result.split("#")[1].split(" ")[0])
    row = cmds.conn.execute(
        "SELECT status, idea FROM improvement_backlog WHERE id=?",
        (bid,)).fetchone()
    assert row["status"] == "open"
    assert row["idea"] == "<案1>"


def test_improve_add_warning_text_does_not_leak_to_backlog_last_result(commands):
    """[ops-first-contact-fixes] T3 (AC-3e): echo-back/警告文言は
    dispatch の戻り値 (端末表示) にのみ出る。DB 列 (last_result 等) には
    新しい文字列を書かない ([[human-reject-reason-leaks-to-improve-prompt]]
    と同じ遮断 8 のクラス)。"""
    cmds = commands
    cmds.dispatch("improve add <案1>")
    row = cmds.conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["last_result"] is None
```

- [ ] red を確認する (**実測して埋める**): 1〜2 本目は `AssertionError: assert '「...」' in
      'backlog #78 を追加しました'` (echo-back が無い)、2 本目後半は `assert '⚠' in
      'backlog #79 を追加しました: 「<案1>」'` (警告が無い) — **(逐語は実測後に埋める)**

### Step 3-b: 実装を転写する

```diff path=src/agentic_fx/commands.py
--- a/src/agentic_fx/commands.py
+++ b/src/agentic_fx/commands.py
@@ -215,10 +215,26 @@
             if cmd == "improve" and args and args[0] == "add":
                 text = " ".join(args[1:])
                 if not text:
                     return "usage: improve add <idea text>"
                 bid = backlog.add(self.conn, idea=text, source="user",
                                   now=self.clock.now())
                 self.activity.write(Category.IMPROVE, "backlog_added",
                                     f"#{bid} via shell", ref_id=str(bid))
-                return f"backlog #{bid} を追加しました"
+                # [ops-first-contact-fixes] T3 (2026-09-20 実機: `improve add
+                # <案1>` の 4 文字が backlog #77 に登録され、改善 mission #86
+                # が「自分なりに解釈」して戦略を作った): 登録直後に原文を
+                # そのまま見せる (echo-back)。DB へは何も追加で書かない —
+                # last_result 等の改善プロンプト注入経路に触れないこと
+                # ([[human-reject-reason-leaks-to-improve-prompt]] と同型)。
+                reply = f"backlog #{bid} を追加しました: 「{text}」"
+                stripped = text.strip()
+                is_placeholder = ((stripped.startswith("<") and stripped.endswith(">"))
+                                  or (stripped.startswith("[") and stripped.endswith("]")))
+                is_too_short = len(stripped) < 4
+                if is_placeholder or is_too_short:
+                    reply += ("\n⚠ 短い/プレースホルダのように見えます。意図した内容で"
+                             "あることを確認してください (削除・訂正は "
+                             f"`backlog reject {bid}` の上で `improve add` を"
+                             "やり直す)")
+                return reply
```

### Step 3-c: green

- [ ] 4 本とも pass、かつ**既存の `test_policy_add_appends_to_directives_file` を含む
      `tests/commands/test_improve_commands.py` 全体**が無改変のまま green であることを確認
      (**実測して埋める**)

### Step 3-d: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T3-M1 | `reply = f"backlog #{bid} を追加しました: 「{text}」"` | `reply = f"backlog #{bid} を追加しました"` (echo-back を消す) | `test_improve_add_echoes_the_registered_idea_text` / `test_improve_add_warns_on_short_or_placeholder_idea` |
| T3-M2 | `is_too_short = len(stripped) < 4` | `is_too_short = len(stripped) < 1` (閾値を実質無効化) | `test_improve_add_warns_on_short_or_placeholder_idea` (`"ab"` で警告が付かなくなる) |
| T3-M3 | `(stripped.startswith("<") and stripped.endswith(">"))` | `False` (プレースホルダ判定を無効化) | `test_improve_add_warns_on_short_or_placeholder_idea` (`<案1>` は 3 文字扱いになり短さ判定で拾われるので、**`[TODO]` (5 文字、短さでは拾えない) を使う枝**が red — この変異の真の killer は `[TODO]` 側の assert であることに注意。実測時に確認) |
| T3-M4 | `if is_placeholder or is_too_short:` の下の `reply +=` ブロックを `backlog.add` 呼び出しの**前**に繰り上げ、失敗時に return する (誤ってブロッキング化する退行) | (上記の入れ替え) | `test_improve_add_does_not_block_registration_when_warned` |
| T3-M5 | `self.activity.write(Category.IMPROVE, "backlog_added", f"#{bid} via shell", ...)` | `self.activity.write(Category.IMPROVE, "backlog_added", f"#{bid} via shell: {text}", ...)` (警告文言や原文を activity 経由で別経路に漏らす方向の変異) | 直接の red は無いが (activity は遮断 8 の対象外)、**AC-3e は `last_result` 列のみを見る**ため、この変異では red にならないことを実装時に確認し、`last_result` に触れる変異 (例: `backlog.add` の直後に `last_result` を更新するコードを誤って追加する) を追加で 1 本足す (T3-M5': `backlog.add` 呼び出し後に `self.conn.execute("UPDATE improvement_backlog SET last_result=? WHERE id=?", (text, bid))` を挿入 → `test_improve_add_warning_text_does_not_leak_to_backlog_last_result` が red) |

- [ ] commit: `fix(ops-first-contact): improve add の登録文を echo-back + 短文/プレースホルダ警告 (T3、拒否しない)`

---

## 全体の green 確認 (T1〜T3 統合後)

- [ ] フルスイート: `uv run pytest -q -p no:cacheprovider --deselect tests/test_shell_interrupt.py --deselect "tests/test_service_app.py::test_interactive_mode_actually_stops_via_stop_event_end_to_end"`
      (`--basetemp` は付けない。約 11 分、background 起動 + 結果行を grep で待つ)
      **フルスイート基準 (着手前に指揮者が実測して埋める)**:
      - 実行前の baseline (この束の変更前、main `ceedd1d`): `____ passed, ____ deselected in ____s` (未記入)
      - T1〜T3 統合後: `____ passed, ____ deselected in ____s` (未記入、baseline + 新規テスト本数と一致すること)
- [ ] `git status` で意図しない差分 (実 DB・plugins/・policy/・config/settings.yaml) が無いことを確認

---

## 未実測の申告

- **T2-M3 の existing-call 互換性**: `_check_service_initial_env_has_no_secrets` のシグネチャに
  `which`/`backend` をキーワード既定値付きで追加する設計 (Step 2-b) は、**既存呼び出し (6 箇所、`object()`
  や fake settings を渡す形) が無改変のまま通ることを前提にしている**が、着手前検証で実際に
  `pytest tests/test_service_app.py -k secrets` を走らせて確認していない。もし `settings.service`
  アクセスで `object()` 渡しの既存テストが `AttributeError` になる場合、Step 2-a のヘルパー
  `_settings_with_allowlist` と同様の最小 stub 化が既存テスト側にも要るかもしれない
  (その場合は「既存テストの書き換えは 0 本」の制約に抵触するため、**指揮者へ申告してから対応すること**)。
- **T3-M3 の killer 対応**: 逆変異表の T3-M3 (プレースホルダ判定の無効化) が実際にどちらの assert
  (`<案1>` か `[TODO]`) で red になるかは、`len("<案1>")` (3) と `len("[TODO]")` (6) の実測次第。
  着手時に `python3 -c 'print(len("<案1>".strip()))'` 等で確認し、逆変異表を実測値に合わせて訂正すること。
- **`ServiceSettings` の pydantic `_Strict` 挙動**: `config.py` の `_Strict` (`class _Strict(BaseModel)`)
  が `extra="forbid"` 相当かどうかを本プランは確認していない (他クラスの命名から類推)。`ServiceSettings`
  を追加する際、モデル定義の作法 (`model_config`/`class Config` の要否) を `config.py:28-31` の
  `_Strict` 定義を見て合わせること。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-21 | v1.0 | 初版。設計書 v1.0 の AC-1a〜AC-3e に対応する T1/T2/T3 を起こす。3 task は互いに独立ファイル域で完全並列、codex にはテスト+本体コードのみ渡し red/green・フルスイート・変異確認は指揮者側 subagent が担当する体制を明記 | 設計書 v1.0 承認 (2026-09-21) を受けたプラン起草 | (本 commit) |
