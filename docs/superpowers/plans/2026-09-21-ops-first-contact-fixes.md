# [ops-first-contact-fixes] 実装プラン v1.1

設計書: `docs/superpowers/specs/2026-09-21-ops-first-contact-fixes-design.md` v1.1。
v1.0 からの変更点は末尾「変更履歴」、および裁定の詳細は `tmp/design-ops-first-contact/codex-r1/verdicts.md`。
対象コードは main `ceedd1d` の現物。**実装は codex (terra/medium) に渡す前提** — 各 task は
「failing test を先に固定 (テスト名・観測点・逐語の期待値) → 本体の最小変更」までを codex に渡し、
**red/green の実測とフルスイート・変異確認は指揮者側の subagent が受ける** (codex は read-only sandbox で
pytest を回せないことがあるため)。

## Global Constraints

- **設計を変えない。** 設計書 v1.1 が正。設計書に無い判断が要るときは実装を止めて申告
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
- **既存テストの assert・parametrize は 1 つも変えない (v1.1、Global Constraints 訂正)。**
  v1.0 は「既存テストの書き換えは 0 本」としていたが、これは誤り — 検査⑤の本体が
  `settings.service.secret_env_allowlist` を読むようになると、第一引数に `object()` を渡す既存呼び出し
  **4 箇所 (実行 8 本)** (`tests/test_service_app.py:3045,3053,3097[parametrize 5 件],3107`) は
  `AttributeError: 'object' object has no attribute 'service'` になる (codex r1 V1、指揮者確認済)。
  **本番コードに互換層 (`getattr(settings, "service", None)` の類) は入れない** — 「service 節の無い
  settings を黙って空 allowlist 扱いにする」のは fail-closed の逆であり、失敗の理由も読めなくなる。
  代わりに **T2 でこの 4 箇所の第一引数 stub を最小構成 (`_object_with_empty_allowlist()`、
  `service=SimpleNamespace(secret_env_allowlist=[])` を持つだけ) に差し替える。assert 本体・
  parametrize の値は 1 つも変えない**。加えて、`_check_cli_backend` (`service.py:373`) の呼び出しが
  `which=which, backend=backend` をキーワードで渡すよう変わるため、既存の monkeypatch 型配線テストが
  持つ差し替え関数 (`tests/test_service_app.py:2989` のラムダ、`:3010`/`:3348` の `_raise`) のうち
  **実際にこの呼び出しへ到達する 3 箇所** (backend=local で早期 return する
  `test_build_app_does_not_check_secret_env_when_improve_backend_is_local` の `_raise` は到達しないため
  対象外) は `which=`/`backend=` を受け取れる形にシグネチャを直す。**「既存テストの assert 本体・
  parametrize は無改変」という不変で挙動が変わっていないことを示す** — 引数 stub・関数シグネチャの
  差し替え (計 7 箇所、実行本数 11 本) は上記不変の対象外
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
| `tests/test_service_app.py` | **追記**: AC-1a〜AC-1c (T1) / AC-2a〜AC-2f (T2)。**加えて (v1.1)** 既存 4 箇所の `object()` 引数 stub 差し替え + 既存 3 箇所の monkeypatch スタブ関数への `which=`/`backend=` kwargs 追加 (assert 本体は無改変、T2) | T1・T2 |
| `tests/commands/test_improve_commands.py` | **追記**: AC-3a〜AC-3h (T3、v1.1 で AC-3f〜AC-3h 追加) | T3 |

## 受入条件 (設計書 §5) と task の対応

| AC | task | テスト名 (予定) |
|---|---|---|
| AC-1a / AC-1b | T1 | `test_build_app_wires_policy_path` / `test_build_app_wired_policy_path_can_append_via_dispatch` |
| AC-1c (v1.1: spy 方式) | T1 | `test_commands_optional_params_all_wired_by_build_app` |
| AC-2a / AC-2b / AC-2e | T2 | `test_check_service_initial_env_has_no_secrets_allowlist_excludes_exact_name` / `test_check_service_initial_env_has_no_secrets_rejects_each_pattern` (assert・parametrize 無改変、回帰) / `test_check_service_initial_env_has_no_secrets_allowlist_is_exact_match_only` |
| AC-2c / AC-2d | T2 | `test_check_service_initial_env_has_no_secrets_message_includes_which_backend_and_pattern` |
| AC-2f (v1.1、新規) | T2 | `test_check_service_initial_env_has_no_secrets_allowlist_hit_warns_names_only` / `test_check_service_initial_env_has_no_secrets_allowlist_hit_with_no_exclusion_does_not_warn` |
| AC-3a / AC-3c | T3 | `test_improve_add_echoes_the_registered_idea_text` |
| AC-3b | T3 | `test_improve_add_warns_on_short_or_placeholder_idea` |
| AC-3d | T3 | `test_improve_add_does_not_block_registration_when_warned` |
| AC-3e | T3 | `test_improve_add_warning_text_does_not_leak_to_backlog_last_result` |
| AC-3f (v1.1、新規) | T3 | `test_improve_add_warns_and_notes_removed_chars_for_zero_width_input` |
| AC-3g (v1.1、新規) | T3 | `test_improve_add_strips_terminal_control_sequences_from_reply` |
| AC-3h (v1.1、新規) | T3 | `test_improve_add_normalizes_multiline_input_newline_to_space` |

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


def test_commands_optional_params_all_wired_by_build_app(tmp_path, monkeypatch):
    """[ops-first-contact-fixes] T1 (AC-1c、v1.1: spy 方式):
    Commands.__init__ のオプション引数が増えたのに build_app 側が追随し
    忘れる再発 ([[verify-integration-not-just-units]]) を機械的に検出する。
    v1.0 の「構築後インスタンス属性が truthy」判定は、
    `commands.py:54` の `self.health_latch = health_latch or HealthLatch()`
    のように truthy な default 値を持つ引数の未配線を見逃す (codex r1 V3、
    現状の 5 引数中 health_latch がまさにこの穴)。`agentic_fx.service.Commands`
    (`service.py:25` で module 属性として import されている) を、渡された
    kwargs を記録してから本物の Commands へ委譲する spy に monkeypatch で
    差し替え、シグネチャ上の全オプション引数名が実際に渡された kwargs の
    キー集合に含まれることを見る。新しいオプション引数を足したら
    EXPECTED_OPTIONAL_PARAMS にも追記しないとこのテスト自体が red になる —
    対応表の更新を強制する。"""
    import inspect

    import agentic_fx.service as service_mod
    from agentic_fx.commands import Commands as RealCommands

    EXPECTED_OPTIONAL_PARAMS = {
        "health_latch", "improve_supervisor", "policy_path",
        "plugins_root", "settings",
    }
    sig = inspect.signature(RealCommands.__init__)
    optional_params = {
        name for name, p in sig.parameters.items()
        if name != "self" and p.default is not inspect.Parameter.empty
    }
    assert optional_params == EXPECTED_OPTIONAL_PARAMS, (
        "Commands.__init__ のオプション引数と EXPECTED_OPTIONAL_PARAMS が "
        "食い違っている — 新規引数を追記するか、削除された引数を消すこと")

    captured_kwargs: dict = {}

    def _spy(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return RealCommands(*args, **kwargs)

    monkeypatch.setattr(service_mod, "Commands", _spy)

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        missing = EXPECTED_OPTIONAL_PARAMS - captured_kwargs.keys()
        assert not missing, (
            f"build_app が Commands(...) に渡していないオプション引数: {missing}")
    finally:
        app.close()
```

- [ ] red を確認する (**着手前検証で実走、逐語を貼る**):
      `test_build_app_wires_policy_path` / `test_build_app_wired_policy_path_can_append_via_dispatch` は
      `AssertionError: assert None == PosixPath('.../policy/directives.md')` 系、
      `test_commands_optional_params_all_wired_by_build_app` は (v1.1、spy 方式)
      `AssertionError: build_app が Commands(...) に渡していないオプション引数: {'policy_path'}`
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
| T1-M1 | `policy_path=root / "policy" / "directives.md",` | (この行ごと削除) | `test_build_app_wires_policy_path` / `test_build_app_wired_policy_path_can_append_via_dispatch` / `test_commands_optional_params_all_wired_by_build_app` (spy の captured_kwargs に `policy_path` が無くなる) |
| T1-M2 | `EXPECTED_OPTIONAL_PARAMS = {...}` (5 エントリ) | `policy_path` のエントリだけ削除した 4 エントリ版 | `test_commands_optional_params_all_wired_by_build_app` (集合比較 `optional_params == EXPECTED_OPTIONAL_PARAMS` が false) |
| T1-M3 (この束で一番大事な変異、「次の再発」の模擬) | `Commands.__init__` にダミーのオプション引数 (例 `dummy_flag: bool = False`) を 1 個追加し、`build_app` の `Commands(...)` 呼び出しに**対応する行を足さない** | (上記の追加のみ) | `test_commands_optional_params_all_wired_by_build_app` (対応表とシグネチャの集合が食い違う) |
| T1-M4 (v1.1 追加、codex r1 V3 の killer) | `build_app` の `Commands(...)` 呼び出しから `health_latch=health_latch,` を削除 | (この行ごと削除) | `test_commands_optional_params_all_wired_by_build_app` (spy の captured_kwargs に `health_latch` が無くなる — v1.0 の truthy 検査ではこの変異を検出できなかった。`self.health_latch = health_latch or HealthLatch()` により属性は削除後も truthy なままになるため) |

- [ ] commit: `fix(ops-first-contact): build_app が Commands に policy_path を渡すよう配線 + 構造的配線テスト (T1)`

---

## T2: 起動時検査⑤の allowlist + `which`/`backend`/パターンを含むメッセージ

**担当**: 設計書 §5「件 2」。`commands.py` を触らないので T1・T3 と並列可。`service.py` は触るが
T1 とは別関数 (`_check_service_initial_env_has_no_secrets`/`_check_cli_backend`)。

### Step 2-a: テストを置いて red を確認する

- [ ] `src/agentic_fx/config.py` に `ServiceSettings` を追加する前提で、`tests/test_service_app.py` に
  以下のヘルパーとテストを追記する。**v1.1 (codex r1 V1): ヘルパーは `load_settings` 経由のフル
  `Settings` ではなく軽量な `SimpleNamespace` にする** — `_check_service_initial_env_has_no_secrets` は
  `settings.service.secret_env_allowlist` しか読まないので、実 `Settings` を組む必要が無い。加えて
  `load_settings` → `_root_with_settings` → `_init` → `run_init` の経路は `setup_technical_logging()`
  を呼び `agentic_fx` logger の `propagate` を `False` に固定する ([[design-review-style]] と同じ罠、
  `tests/test_service_app.py:3417-3423` の既存コメント参照) ため、この経路を通すと後続の WARNING テストで
  `caplog` が記録を拾えなくなる。このヘルパーは既存の `object()` 引数 stub の置き換えにも使う
  (下記「既存呼び出しの stub 差し替え」参照):

```python
def _settings_stub(allowlist=()):
    """[ops-first-contact-fixes] T2 (v1.1、codex r1 V1):
    `_check_service_initial_env_has_no_secrets` の本体は
    `settings.service.secret_env_allowlist` しか読まない。フル `Settings` を
    `load_settings` 経由で組むと `_init`/`run_init` が
    `setup_technical_logging()` を呼んでしまい、`agentic_fx` logger の
    propagate が False に固定されて `caplog` が拾えなくなる
    ([[design-review-style]] の先例と同じ罠)。単体テストには不要な依存
    (tmp_path・yaml ファイル) でもあるため、必要最小限の SimpleNamespace を
    使う (`object()` の代替でもある)。"""
    from types import SimpleNamespace
    return SimpleNamespace(service=SimpleNamespace(secret_env_allowlist=list(allowlist)))


def test_check_service_initial_env_has_no_secrets_allowlist_excludes_exact_name():
    """[ops-first-contact-fixes] T2 (AC-2a): settings.service.secret_env_allowlist
    に完全一致で載っている名前は誤検知から除外される。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    settings = _settings_stub(["CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"])
    _check_service_initial_env_has_no_secrets(
        settings, which="trade", backend="codex",
        read_initial_env_names=lambda: {
            "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS", "HOME"})
    # 例外が出なければ pass


def test_check_service_initial_env_has_no_secrets_allowlist_is_exact_match_only():
    """[ops-first-contact-fixes] T2 (AC-2e): allowlist は完全一致のみ。
    近い名前 (末尾に _V2 等) までは除外しない — パターン化への後退を防ぐ。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    settings = _settings_stub(["CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"])
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

    settings = _settings_stub([])
    with pytest.raises(RuntimeError) as exc_info:
        _check_service_initial_env_has_no_secrets(
            settings, which="trade", backend="codex",
            read_initial_env_names=lambda: {
                "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS", "HOME"})
    msg = str(exc_info.value)
    assert "trade+codex" in msg
    assert "OPENAI_" in msg
    assert "secret_env_allowlist" in msg


def test_check_service_initial_env_has_no_secrets_allowlist_hit_warns_names_only(caplog):
    """[ops-first-contact-fixes] T2 (AC-2f、v1.1、codex r1 V2): allowlist が
    実際に除外した名前 (allowlist に載っていて、かつ env に実在し、かつ
    秘密名パターンに当たった名前) があれば起動時 WARNING が 1 回出る。値は
    出さず変数名のみ含む — seam (`read_initial_env_names`) は名前しか
    返さない契約なので「caplog に記録された文字列がこの名前集合の範囲に
    収まる」ことで値が混ざらないことを確認できる。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    settings = _settings_stub(["CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"])
    with caplog.at_level("WARNING", logger="agentic_fx.service"):
        _check_service_initial_env_has_no_secrets(
            settings, which="trade", backend="codex",
            read_initial_env_names=lambda: {
                "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS", "HOME"})
    assert "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS" in caplog.text
    assert "secret_env_allowlist" in caplog.text


def test_check_service_initial_env_has_no_secrets_no_warning_when_nothing_excluded(caplog):
    """[ops-first-contact-fixes] T2 (AC-2f 否定側、v1.1): allowlist が空、
    または allowlist の名前が env に無いときは WARNING を出さない
    (雑音にしない)。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    with caplog.at_level("WARNING", logger="agentic_fx.service"):
        _check_service_initial_env_has_no_secrets(
            _settings_stub([]), which="trade", backend="codex",
            read_initial_env_names=lambda: {"HOME", "PATH"})
    assert caplog.text == ""

    with caplog.at_level("WARNING", logger="agentic_fx.service"):
        _check_service_initial_env_has_no_secrets(
            _settings_stub(["CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"]),
            which="trade", backend="codex",
            read_initial_env_names=lambda: {"HOME", "PATH"})
    assert caplog.text == ""
```

- [ ] **既存呼び出しの stub 差し替え (v1.1、Global Constraints 参照)。assert 本体・parametrize の値は
      1 つも変えない**:
  - `tests/test_service_app.py` の `_check_service_initial_env_has_no_secrets(object(), ...)` の形の
    直接呼び出し **4 箇所 (実行 8 本)**: `:3045-3046`
    (`test_check_service_initial_env_has_no_secrets_rejects_leaked_key_via_seam`)、`:3053-3054`
    (`_passes_when_seam_clean`)、`:3097-3098` (`_rejects_each_pattern`、`@pytest.mark.parametrize` 5 件)、
    `:3107-3108` (`_rejects_lowercase_name`)。第一引数を `object()` → `_settings_stub()` に置き換える
    (`.service.secret_env_allowlist` を持つだけの stub。第一引数は元々未使用のダミーだったので置き換えても
    意味は変わらない)。
  - 検査⑤の呼び出しが `_check_cli_backend` から `which=which, backend=backend` をキーワードで渡す形に
    変わる (Step 2-b)。既存の monkeypatch 型配線テストのうち、`_check_service_initial_env_has_no_secrets`
    を差し替え関数に monkeypatch していて **かつ実際にこの呼び出しへ到達する** 3 箇所は、差し替え関数の
    シグネチャに `which=`/`backend=` を追加しないと `TypeError: got an unexpected keyword argument
    'which'` になる (assert 本体は変えない、シグネチャのみ):
    - `:2987-2989` (`test_build_app_opencode_does_not_require_credentials`、improve.backend=opencode →
      到達する): `lambda settings, *, read_initial_env_names=None: None` →
      `lambda settings, *, which=None, backend=None, read_initial_env_names=None: None`
    - `:3010-3013` (`test_build_app_rejects_when_service_initial_env_has_secret_pattern`、
      improve.backend=claude、trade は既定 local で早期 return するので実際に到達するのは
      `which="improve", backend="claude"`) — `_raise` のシグネチャに `which=None, backend=None` を足し、
      到達した `which`/`backend` を捕捉して `_check_cli_backend` が正しく転送していることも pin する
      (下記コード例)
    - `:3348-3351` (`test_check_service_initial_env_has_no_secrets_is_called_for_opencode_backend`、
      improve.backend=opencode、実際に到達するのは `which="improve", backend="opencode"`) — 同様に
      `which=None, backend=None` を足し、捕捉値を pin する
  - **対象外 (到達しないので変更不要)**: `:3030-3033`
    (`test_build_app_does_not_check_secret_env_when_improve_backend_is_local`) の `_raise` —
    `_check_cli_backend` は `backend == "local"` で検査⑤に到達する前に early return するため、この
    `_raise` は呼ばれない (この試験がそもそも「呼ばれないこと」を見ている)。

  `:3010-3013` と `:3348-3351` の pin 追加コード例 (該当テストの `_raise` 定義と `pytest.raises` ブロックを
  以下に差し替える。**assert 本体である `pytest.raises(RuntimeError, match=...)` は変えない** — `which`/
  `backend` の捕捉 assert を `with` ブロックの後に追加するだけ):

```python
def test_build_app_rejects_when_service_initial_env_has_secret_pattern(tmp_path, monkeypatch):
    """⑤ の配線: improve+claude のとき `_check_service_initial_env_has_no_secrets`
    が呼ばれ、例外がそのまま `build_app` から伝播する。検査本体 (`/proc/self/environ`
    の読み取り・パターン照合の正しさ) は `test_check_service_initial_env_has_no_secrets_*`
    (下記) が別途 pin する — ここでは配線のみを見る (R3: `monkeypatch.setenv` は
    `/proc/self/environ` を書き換えないため、実環境の秘密漏れに依存したテストは
    書けない、B1 の再発防止)。**v1.1 追加 (codex r1 V1)**: `_check_cli_backend` が
    `which`/`backend` を実際にこの検査へ転送していることも `captured` で pin する。"""
    import sys
    import agentic_fx.service as service_mod

    captured = {}

    def _raise(settings, *, which=None, backend=None, read_initial_env_names=None):
        captured["which"], captured["backend"] = which, backend
        raise RuntimeError("SOME_SERVICE_API_KEY leaked")

    monkeypatch.setattr(service_mod, "_check_service_initial_env_has_no_secrets", _raise)
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text('{"token":"x"}')
    creds_file.chmod(0o600)
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "claude", "model": "m"},
        "claude": {"bin": sys.executable, "credentials_file": str(creds_file)}})
    with pytest.raises(RuntimeError, match="API_KEY"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())
    assert captured == {"which": "improve", "backend": "claude"}
```

  (`test_check_service_initial_env_has_no_secrets_is_called_for_opencode_backend` も同型の差し替えで
  (既存の docstring はそのまま残し) `assert captured == {"which": "improve", "backend": "opencode"}`
  を足す。この 2 本が `_check_cli_backend` から実際に `which`/`backend` が転送されることの pin になる —
  codex r1 V1 の「配線テストで pin する」対応。**着手時の確認 (実測済み)**: `settings.runner.trade.backend`
  は両テストとも `config/settings.yaml.example:26` の既定 `local` のままのため、`build_app` の
  `_check_cli_backend(settings, which="trade")` (`service.py:851`) は検査⑤に到達する前に early
  return し、`which="improve")` (`:852`) の呼び出しだけが `_raise` に届く — `captured` が
  `which="improve"` になるのはこの経路による)。

- [ ] red を確認する (**実測して埋める**): 新規 6 本 (allowlist 2 本・メッセージ 1 本・WARNING 2 本・
      旧 settings.yaml 無改変ロード 1 本 [下記「T2 追加 Step」]) は
      `TypeError: _check_service_initial_env_has_no_secrets() got an unexpected keyword argument 'which'`
      (シグネチャ変更前、旧 settings.yaml テストのみ `AttributeError: 'Settings' object has no
      attribute 'service'`)。stub 差し替え後の既存 4 箇所は変更前は素通り (green のまま、`object()`
      でも `settings` 未使用だったため) だが、**Step 2-b の本体変更を先に当ててから stub 差し替え前の
      状態で走らせると** `AttributeError: 'object' object has no attribute 'service'` になることを
      確認する (stub 差し替えの必要性そのものの red)。monkeypatch スタブ 3 箇所も同様に、本体変更後・
      シグネチャ未修正の状態で `TypeError: ... unexpected keyword argument 'which'` になることを
      確認する。

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
+    excluded_by_allowlist: list[str] = []
+    for k in names:
+        # allowlist は完全一致のみ (パターン化しない — 守りを弱めない)
+        if k in allowlist:
+            # [ops-first-contact-fixes] T2 v1.1 (AC-2f、codex r1 V2):
+            # allowlist が実際に (= 秘密名パターンに当たったからこそ)
+            # 除外した名前だけを対象にする — allowlist に載っているが
+            # パターンに当たらない名前は対象外 (雑音にしない)。
+            if _matched_pattern(k) is not None:
+                excluded_by_allowlist.append(k)
+            continue
+        pat = _matched_pattern(k)
+        if pat is not None:
+            leaked.append(k)
+            matches[k] = pat
+    if excluded_by_allowlist:
+        _log.warning(
+            "secret_env_allowlist により次の名前を検査⑤から除外した: %s "
+            "— 同 UID の CLI worker は /proc/<pid>/environ からこの値を"
+            "読める", sorted(excluded_by_allowlist))
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
`_log.warning(...)` (AC-2f、v1.1) は `service.py:62` の既存モジュールロガー
`_log = logging.getLogger("agentic_fx.service")` をそのまま使う (新規 import 不要)。

### Step 2-c: green + 既存 pin の無改変回帰確認

- [ ] 新規 6 本 (allowlist 2 本・メッセージ 1 本・WARNING 2 本・旧 settings.yaml 無改変ロード 1 本
      [下記「T2 追加 Step」、AC には対応せず問い5副産物の回帰点として追加]) が green、かつ
      **`tests/test_service_app.py` の検査⑤関連の既存テスト全部**
      (`test_check_service_initial_env_has_no_secrets_rejects_leaked_key_via_seam` /
      `_passes_when_seam_clean` / `_default_seam_is_proc_self_environ` /
      `_rejects_each_pattern` (parametrize 5 件) / `_rejects_lowercase_name` /
      `test_build_app_rejects_when_service_initial_env_has_secret_pattern` /
      `test_build_app_does_not_check_secret_env_when_improve_backend_is_local` /
      `test_check_service_initial_env_has_no_secrets_is_called_for_opencode_backend`) が
      **assert 本体・parametrize は無改変のまま green** であることを確認する (AC-2b の回帰確認、
      実測して埋める)。この 8 本のうち 7 本は第一引数 stub (`object()` → `_settings_stub()`) または
      monkeypatch スタブのシグネチャ (`which=None, backend=None` 追加、うち 2 本は捕捉 assert 追加)
      が v1.1 で変わっているが、**assert 本体・parametrize の値自体は無改変** (Global Constraints 参照)

### Step 2-d: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T2-M1 | `if k in allowlist: continue` | (この分岐ごと削除) | `test_check_service_initial_env_has_no_secrets_allowlist_excludes_exact_name` |
| T2-M2 | `if k in allowlist:` | `if any(k.startswith(a) for a in allowlist):` (前方一致に緩める) | `test_check_service_initial_env_has_no_secrets_allowlist_is_exact_match_only` |
| T2-M3 | `f"{which}+{backend} backend refuses..."` | `"improve+claude backend refuses..."` (固定文言に戻す) | `test_check_service_initial_env_has_no_secrets_message_includes_which_backend_and_pattern` |
| T2-M4 | `f"(matched pattern(s) {patterns_hit!r})."` | (この句を削除) | `test_check_service_initial_env_has_no_secrets_message_includes_which_backend_and_pattern` (`"OPENAI_" in msg` が false) |
| T2-M5 | `_check_service_initial_env_has_no_secrets(settings, which=which, backend=backend)` | `_check_service_initial_env_has_no_secrets(settings)` (呼び出し元で `which`/`backend` を渡さない) | `test_build_app_rejects_when_service_initial_env_has_secret_pattern` の `assert captured == {"which": "improve", "backend": "claude"}` (v1.1 追加の pin、既定値 `"improve"`/`"claude"` が渡らなくなるので即 red) / `test_check_service_initial_env_has_no_secrets_is_called_for_opencode_backend` の同型 assert |
| T2-M6 (v1.1 追加) | `if excluded_by_allowlist: _log.warning(...)` | (この分岐ごと削除) | `test_check_service_initial_env_has_no_secrets_allowlist_hit_warns_names_only` |
| T2-M7 (v1.1 追加) | `if excluded_by_allowlist:` | `if allowlist:` (allowlist が非空なら env との交差を見ずに常に警告する方向へ緩める) | `test_check_service_initial_env_has_no_secrets_no_warning_when_nothing_excluded` (allowlist に名前はあるが env に無いケースで警告が出てしまい red) |

- [ ] commit: `fix(ops-first-contact): 起動時検査⑤に secret_env_allowlist + which/backend/パターン/allowlist除外WARNINGを含むメッセージ (T2)`

---

## T3: `improve add` の echo-back + 非ブロッキング警告

**担当**: 設計書 §5「件 3」。`service.py`/`config.py` を触らないので T1・T2 と並列可。

### Step 3-a: テストを置いて red を確認する

- [ ] `tests/commands/test_improve_commands.py` に以下を追記する (既存 `test_policy_add_appends_to_directives_file` 付近)。
  **v1.1 で修正**: `commands` フィクスチャ (`tests/commands/test_improve_commands.py:36-55`) は
  `Commands` インスタンス単体ではなく **`(cmds, conn, tmp_path)` のタプルを返す** (同ファイル内の
  他の全テストが `cmds, conn, _ = commands` の形で受けている、例 `:181` の
  `test_policy_add_appends_to_directives_file`)。v1.0 の下書きコードは `cmds = commands` と誤って
  タプルをそのまま束縛していたため (`cmds.dispatch(...)` が `AttributeError: 'tuple' object has no
  attribute 'dispatch'` になる、着手前検証で発見)、**同じ unpacking 形式に訂正**した:

```python
def test_improve_add_echoes_the_registered_idea_text(commands):
    """[ops-first-contact-fixes] T3 (AC-3a/AC-3c): 登録直後に課題文の
    正規化後の表示文字列が戻り値へそのまま出る (制御文字・ゼロ幅文字を
    含まない通常入力では原文と一致する)。通常の長さ・非プレースホルダでは
    警告が付かない。"""
    cmds, conn, _ = commands
    result = cmds.dispatch("improve add USDJPY のスプレッドが広い時間帯の指値精度を上げたい")
    assert "backlog #" in result
    assert "「USDJPY のスプレッドが広い時間帯の指値精度を上げたい」" in result
    assert "⚠" not in result


def test_improve_add_warns_on_short_or_placeholder_idea(commands):
    """[ops-first-contact-fixes] T3 (AC-3b): 可視 4 文字未満、または
    <…>/[…] で完全に囲まれた idea には非ブロッキング警告が付く
    (2026-09-20 実機: `improve add <案1>` → backlog #77 →
    改善 mission #86 が誤解釈して戦略を作った、の再発防止)。"""
    cmds, conn, _ = commands
    result_placeholder = cmds.dispatch("improve add <案1>")
    assert "「<案1>」" in result_placeholder
    assert "⚠" in result_placeholder

    result_short = cmds.dispatch("improve add ab")
    assert "⚠" in result_short

    result_bracket = cmds.dispatch("improve add [TODO]")
    assert "⚠" in result_bracket


def test_improve_add_does_not_block_registration_when_warned(commands):
    """[ops-first-contact-fixes] T3 (AC-3d): 警告が出ても登録は成立する
    (status='open' のまま、拒否しない — R3 の裁定)。`backlog.idea` 列には
    正規化前の原文がそのまま入る (保存側は無加工)。"""
    cmds, conn, _ = commands
    result = cmds.dispatch("improve add <案1>")
    bid = int(result.split("#")[1].split(" ")[0])
    row = conn.execute(
        "SELECT status, idea FROM improvement_backlog WHERE id=?",
        (bid,)).fetchone()
    assert row["status"] == "open"
    assert row["idea"] == "<案1>"


def test_improve_add_warning_text_does_not_leak_to_backlog_last_result(commands):
    """[ops-first-contact-fixes] T3 (AC-3e): echo-back/警告文言は
    dispatch の戻り値 (端末表示) にのみ出る。DB 列 (last_result 等) には
    新しい文字列を書かない ([[human-reject-reason-leaks-to-improve-prompt]]
    と同じ遮断 8 のクラス)。"""
    cmds, conn, _ = commands
    cmds.dispatch("improve add <案1>")
    row = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["last_result"] is None


def test_improve_add_warns_and_notes_removed_chars_for_zero_width_input(commands):
    """[ops-first-contact-fixes] T3 (AC-3b/AC-3f、v1.1、codex r1 V4):
    ゼロ幅スペース (U+200B、Unicode カテゴリ Cf) 4 個は `len()` では
    4 文字だが正規化後の可視文字数は 0 — 「4 文字未満」警告が出る。
    正規化で除去が起きたことも追加の 1 行で知らせる。"""
    cmds, conn, _ = commands
    idea = "​" * 4
    result = cmds.dispatch(f"improve add {idea}")
    assert "⚠" in result
    assert "表示できない文字を 4 個含みます" in result


def test_improve_add_strips_terminal_control_sequences_from_reply(commands):
    """[ops-first-contact-fixes] T3 (AC-3g、v1.1、codex r1 V4): 端末制御列
    (`\\x1b[2J` 等、ESC は Unicode カテゴリ Cc) を含む idea を echo-back
    しても、戻り値に ESC 文字そのものは含まれない (端末制御列をそのまま
    流さない — ESC 以降の可視文字 `[2J` はテキストとして残る、フル ANSI
    シーケンス解釈はしない)。"""
    cmds, conn, _ = commands
    idea = "\x1b[2J値は秘密ではない長めの説明文です"
    result = cmds.dispatch(f"improve add {idea}")
    assert "\x1b" not in result


def test_normalize_idea_display_converts_newline_to_space_before_removing_control_chars():
    """[ops-first-contact-fixes] T3 (AC-3h、v1.1、codex r1 V4):
    `_normalize_idea_display` を直接単体テストする — `Commands.dispatch(line:
    str)` は `line.strip().split()` で args を切るため、シェル経由では
    `text` に生の `\\n` が渡ることは無い (改行はこの時点で既に単一空白へ
    畳まれる)。正規化関数自体は `commands.py` の docstring が言う
    「main.py シェルと (Phase 2) client.py で共有」の将来の呼び出し元
    (行区切りでない入力経路) のために改行→空白の変換を独立して持つ。"""
    from agentic_fx.commands import _normalize_idea_display

    display, removed = _normalize_idea_display("1行目\n2行目")
    assert display == "1行目 2行目"
    assert removed == 0  # 改行は「除去」ではなく置換 — 表示できない文字数には数えない
```

- [ ] red を確認する (**実測して埋める**): 1〜2 本目は `AssertionError: assert '「...」' in
      'backlog #78 を追加しました'` (echo-back が無い)、2 本目後半は `assert '⚠' in
      'backlog #79 を追加しました: 「<案1>」'` (警告が無い)、v1.1 追加 3 本は
      `AttributeError: module 'agentic_fx.commands' has no attribute '_normalize_idea_display'`
      (未実装) — **(逐語は実測後に埋める)**

### Step 3-b: 実装を転写する

```diff path=src/agentic_fx/commands.py
--- a/src/agentic_fx/commands.py
+++ b/src/agentic_fx/commands.py
@@ -1,8 +1,9 @@
 """操作コマンド定義 — main.py シェルと (Phase 2) client.py で共有 (設計書 §8)。"""
 from __future__ import annotations
 
 import json
 import sqlite3
+import unicodedata
 from pathlib import Path
 
 from agentic_fx._safe_error import safe_error_text
```

```diff path=src/agentic_fx/commands.py
--- a/src/agentic_fx/commands.py
+++ b/src/agentic_fx/commands.py
@@ -33,6 +33,25 @@
   stop                       graceful shutdown (シェルのみ)
 (Phase 2 で追加: news / model / mode / autopilot)"""
 
 
+def _normalize_idea_display(text: str) -> tuple[str, int]:
+    """[ops-first-contact-fixes] T3 (v1.1、codex r1 V4): `improve add` の
+    echo-back 表示と短文/プレースホルダ警告判定の両方に使う正規化。
+    改行は空白 1 個に置換してから (複数行の貼り付けが単語ごと連結しない
+    ように)、Unicode カテゴリが `C` で始まる文字 (Cc 制御 / Cf 書式 =
+    ゼロ幅・bidi 制御など) を除去し、最後に strip する。戻り値は
+    (正規化後の表示用文字列, 除去した C* 文字数) — 除去件数は
+    「表示できない文字を N 個含みます」の非ブロッキング注記に使う
+    (改行→空白の置換はここでいう「除去」には数えない)。backlog に保存する
+    原文 (`backlog.idea`) はこの関数を通さない — 保存側は無加工のまま
+    (本束の範囲外)。"""
+    step1 = text.replace("\n", " ")
+    kept = [ch for ch in step1 if not unicodedata.category(ch).startswith("C")]
+    removed = len(step1) - len(kept)
+    return "".join(kept).strip(), removed
+
+
 class Commands:
     def __init__(self, *, conn: sqlite3.Connection, state_store: StateStore,
```

```diff path=src/agentic_fx/commands.py
--- a/src/agentic_fx/commands.py
+++ b/src/agentic_fx/commands.py
@@ -215,10 +215,29 @@
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
+                # が「自分なりに解釈」して戦略を作った): 登録直後に正規化後の
+                # 表示文字列を見せる (echo-back)。DB へは何も追加で書かない —
+                # last_result 等の改善プロンプト注入経路に触れないこと
+                # ([[human-reject-reason-leaks-to-improve-prompt]] と同型)。
+                display, removed = _normalize_idea_display(text)
+                reply = f"backlog #{bid} を追加しました: 「{display}」"
+                is_placeholder = ((display.startswith("<") and display.endswith(">"))
+                                  or (display.startswith("[") and display.endswith("]")))
+                is_too_short = len(display) < 4
+                if is_placeholder or is_too_short:
+                    reply += ("\n⚠ 短い/プレースホルダのように見えます。意図した内容で"
+                             "あることを確認してください (削除・訂正は "
+                             f"`backlog reject {bid}` の上で `improve add` を"
+                             "やり直す)")
+                if removed:
+                    reply += f"\n表示できない文字を {removed} 個含みます"
+                return reply
```

(実際の `git diff` 適用時は 3 つの hunk とも同一ファイル `src/agentic_fx/commands.py` の別々の箇所 —
codex には 1 ファイル 3 箇所の変更としてまとめて渡してよい)

### Step 3-c: green

- [ ] 8 本とも pass、かつ**既存の `test_policy_add_appends_to_directives_file` を含む
      `tests/commands/test_improve_commands.py` 全体**が無改変のまま green であることを確認
      (**実測して埋める**)

### Step 3-d: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T3-M1 | `reply = f"backlog #{bid} を追加しました: 「{display}」"` | `reply = f"backlog #{bid} を追加しました"` (echo-back を消す) | `test_improve_add_echoes_the_registered_idea_text` / `test_improve_add_warns_on_short_or_placeholder_idea` |
| T3-M2 | `is_too_short = len(display) < 4` | `is_too_short = len(display) < 1` (閾値を実質無効化) | `test_improve_add_warns_on_short_or_placeholder_idea` (`"ab"` で警告が付かなくなる) |
| T3-M3 | `(display.startswith("<") and display.endswith(">"))` | `False` (プレースホルダ判定を無効化) | `test_improve_add_warns_on_short_or_placeholder_idea` (`<案1>` は 3 文字扱いになり短さ判定で拾われるので、**`[TODO]` (6 文字、短さでは拾えない) を使う枝**が red — この変異の真の killer は `[TODO]` 側の assert であることに注意。着手時に `python3 -c 'print(len("<案1>"))'` (3) と `python3 -c 'print(len("[TODO]"))'` (6) で実測し直すこと) |
| T3-M4 | `if is_placeholder or is_too_short:` の下の `reply +=` ブロックを `backlog.add` 呼び出しの**前**に繰り上げ、失敗時に return する (誤ってブロッキング化する退行) | (上記の入れ替え) | `test_improve_add_does_not_block_registration_when_warned` |
| T3-M5 | `self.activity.write(Category.IMPROVE, "backlog_added", f"#{bid} via shell", ...)` | `self.activity.write(Category.IMPROVE, "backlog_added", f"#{bid} via shell: {text}", ...)` (警告文言や原文を activity 経由で別経路に漏らす方向の変異) | 直接の red は無いが (activity は遮断 8 の対象外)、**AC-3e は `last_result` 列のみを見る**ため、この変異では red にならないことを実装時に確認し、`last_result` に触れる変異 (例: `backlog.add` の直後に `last_result` を更新するコードを誤って追加する) を追加で 1 本足す (T3-M5': `backlog.add` 呼び出し後に `self.conn.execute("UPDATE improvement_backlog SET last_result=? WHERE id=?", (text, bid))` を挿入 → `test_improve_add_warning_text_does_not_leak_to_backlog_last_result` が red) |
| T3-M6 (v1.1 追加) | `kept = [ch for ch in step1 if not unicodedata.category(ch).startswith("C")]` | `kept = list(step1)` (C* 除去を丸ごと外す) | `test_improve_add_warns_and_notes_removed_chars_for_zero_width_input` (ゼロ幅 4 個が `len()==4` のまま警告が出なくなる) / `test_improve_add_strips_terminal_control_sequences_from_reply` (`\x1b` がそのまま残る) |
| T3-M7 (v1.1 追加) | `step1 = text.replace("\n", " ")` | `step1 = text` (改行→空白の置換を外す) | `test_normalize_idea_display_converts_newline_to_space_before_removing_control_chars` (`\n` が Cc として除去されるだけになり `"1行目 2行目"` ではなく `"1行目2行目"` になる) |
| T3-M8 (v1.1 追加、判定だけ raw に戻す) | `is_placeholder`/`is_too_short` の判定対象を `display` から `text.strip()` に戻す (echo-back は正規化後のまま) | (上記の差し替え) | `test_improve_add_warns_and_notes_removed_chars_for_zero_width_input` (`len(text.strip())==4` になり短さ警告が出なくなる) |
| T3-M9 (v1.1 追加、echo だけ raw に戻す) | `reply = f"backlog #{bid} を追加しました: 「{display}」"` の `display` を `text` に戻す (判定は正規化後のまま) | (上記の差し替え) | `test_improve_add_strips_terminal_control_sequences_from_reply` (`\x1b` が戻り値に残る) |

- [ ] commit: `fix(ops-first-contact): improve add の登録文を正規化して echo-back + 短文/プレースホルダ/不可視文字警告 (T3、拒否しない)`

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

- **v1.1 で解消 (旧 T2-M3)**: `_check_service_initial_env_has_no_secrets` のシグネチャに `which`/
  `backend` を追加すると既存呼び出しが通らなくなる懸念は、codex r1 レビューで実測・確定した
  (`object()` を渡す直接呼び出し 4 箇所・実行 8 本が `AttributeError` になる、`which`/`backend` 未対応の
  monkeypatch スタブ 3 箇所が `TypeError` になる)。Step 2-a に対応方法 (stub 差し替え・シグネチャ追加) を
  明記済み — 指揮者裁定済みのため、実装時の追加申告は不要。
- **`ServiceSettings` の pydantic `_Strict` 挙動 (v1.0 の未実測、codex r1 の問い 5 の副産物で解消)**:
  `_Strict` (`config.py:28-29`) は `model_config = ConfigDict(extra="forbid")`。`ServiceSettings` を
  `default_factory=ServiceSettings` で `Settings.service` に足せば、既存 `settings.yaml` (旧 `service:`
  セクション無し) は無改変でロードできる (`extra="forbid"` は未知キーを拒否するだけで、省略された
  フィールドは default で埋まる — pydantic の通常挙動)。**残す確認**: T2 の Step に「`service:` 節の無い
  yaml をロードするテスト 1 本」を追加すること (下記追記)。
- **T3-M3 の killer**: 逆変異表 (Step 3-d) に実測値 (`len("<案1>")` = 3、`len("[TODO]")` = 6) を反映済み。
  着手時に念のため再確認すること。

### T2 追加 Step: 旧 settings.yaml の無改変ロード確認

- [ ] `tests/test_service_app.py` に以下を追記する (`service:` 節を含まない旧形式 yaml でも
      `Settings.service` が default で埋まることの pin。実装が `ServiceSettings` を正しく
      `default_factory` 付きで追加していることの回帰点):

```python
def test_settings_without_service_section_loads_with_default_empty_allowlist(tmp_path):
    """[ops-first-contact-fixes] T2 (v1.1、codex r1 問い5 副産物): 旧
    `settings.yaml` (`service:` 節が無い) でも `Settings.service` が
    `default_factory=ServiceSettings` で空 allowlist として埋まり、
    ロードが失敗しない。"""
    from agentic_fx.config import load_settings

    _init(tmp_path)
    path = tmp_path / "config" / "settings.yaml"
    import yaml as _yaml
    raw = _yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.pop("service", None)  # 旧形式を模す (そもそも example にまだ無い場合はこの pop は no-op)
    path.write_text(_yaml.safe_dump(raw), encoding="utf-8")

    settings = load_settings(path)
    assert settings.service.secret_env_allowlist == []
```

- [ ] red を確認する (**実測して埋める**): `config.py` 未変更の間は
      `AttributeError: 'Settings' object has no attribute 'service'` — **(実測後に埋める)**
- [ ] commit はしない (Step 2 の一連のコミットに含める — 上記 Step 2-c の green 確認対象に含める)

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-21 | v1.0 | 初版。設計書 v1.0 の AC-1a〜AC-3e に対応する T1/T2/T3 を起こす。3 task は互いに独立ファイル域で完全並列、codex にはテスト+本体コードのみ渡し red/green・フルスイート・変異確認は指揮者側 subagent が担当する体制を明記 | 設計書 v1.0 承認 (2026-09-21) を受けたプラン起草 | (本 commit) |
| 2026-09-21 | v1.1 | codex 設計レビュー r1 (Important 4) の反映。Global Constraints の「既存テストの書き換え 0 本」を訂正 (引数 stub 7 箇所・実行 11 本の差し替えを明記、assert 本体は無改変)。T1: 構造的テストを truthy 検査から spy 方式に変更 (AC-1c、`health_latch` の見逃しを解消)。T2: allowlist 除外時の起動 WARNING (AC-2f) を追加、テストヘルパーを `load_settings` 経由から軽量 `SimpleNamespace` (`_settings_stub`) に変更 (caplog が `agentic_fx` logger の `propagate=False` に阻まれる罠を回避)、旧 settings.yaml 無改変ロードの pin テストを追加。T3: 表示・警告判定共通の正規化関数 `_normalize_idea_display` を新設 (Unicode カテゴリ C* 除去 + 改行→空白、AC-3a/AC-3b 書き換え + AC-3f〜AC-3h 新設)、`commands` フィクスチャが `(cmds, conn, tmp_path)` のタプルを返す実仕様に合わせてテストコードの unpacking を訂正 (v1.0 の下書きコードのバグ) | codex 設計レビュー r1 (Important 4 件、Critical 0) + 実コード照合時に発見したプラン記述の欠陥 | (未コミット) |
