# [ops-first-contact-fixes] 設計書 v1.2

束: 2026-09-20 の実機運用でユーザー本人が直接踏んだ小さな不具合 3 件の是正。
新しい機能・新しい配備経路・新しい自動化は作らない。3 件とも入口層 (`service.py` の起動時検査/配線、
`commands.py` の CLI 分岐) の小改修に閉じる。

対象:
1. `[policy-add-unwired-in-service]` (台帳起票済、`.superpowers/sdd/plan10-plan/tickets.md:109`)
2. `[secret-env-guard-false-positive]` (台帳起票済、`.superpowers/sdd/plan10-plan/tickets.md:110`)
3. `improve add` が中身の無い課題文を受け付ける (未起票、本書で起票を兼ねる)

下書き (調査記録 + 選択肢比較) は `tmp/design-ops-first-contact/design.md` (2026-09-21、ユーザー承認済)。
本書はその承認内容を spec 化したもの。**設計判断は下書きから変えていない** — 本書は §0 にユーザー/指揮者の
最終裁定 4 点を追記し、§2〜§6 を実装プランが直接参照できる形 (動作目線 + AC + file:line) に整理した。

対象コードは main `ceedd1d` 時点の現物。

---

## 0. 位置づけとユーザー裁定 (2026-09-21)

下書きの推奨 (各件 A 案) は **3 件ともユーザー承認 (2026-09-21)**。加えて指揮者が以下 4 点を決めた:

| # | 論点 | 裁定 |
|---|---|---|
| R1 | 件 2: 許可リストの置き場所 | **`service.secret_env_allowlist` を新設**。既存の `runner.*` 配下には合流させない (backend 選択の意味論と別物のため) |
| R2 | 件 2: エラー文に当たったパターン名を含めるか | **含める** (デバッグ性優先。標準エラーは外部公開されない) |
| R3 | 件 3: 警告条件 | **「strip 後の可視文字数が 4 文字未満」または「全体が `<…>` か `[…]` の形」のどちらか**。**登録は止めない** (非ブロッキング、拒否しない) |
| R4 | 件 3: 対話確認 (2 段階) | **見送り、`[ops-ui]` 方針の設計時に改めて検討**。本束は echo-back + 非ブロッキング警告のみ |

下書きで退けた案 (件 2 のパターン語境界化・値検査、件 3 の入力ブロック・対話確認) は本書でも退けたまま —
理由は各件の §3 に転記する。

### 0.1 codex 設計レビュー r1 の反映 (2026-09-21、v1.1)

v1.0 を対象に codex (terra/medium) 設計レビュー 1 周を実施 (Critical 0 / Important 4)。
裁定 (`tmp/design-ops-first-contact/codex-r1/verdicts.md`、指揮者確定) を反映して v1.1 とした。
4 件とも採用 (V4 は範囲を絞って採用):

| # | 指摘 | 裁定 |
|---|---|---|
| V1 | T2 で `settings.service.secret_env_allowlist` を読むようになると、`object()` を渡す既存呼び出し 4 箇所 (実行 8 本) が `AttributeError` になる。「既存テストの書き換えは 0 本」という Global Constraints の記述自体が誤り | 本番コードに互換層は入れない。テスト側の第一引数 stub を最小構成に差し替える (assert 本体は無改変)。Global Constraints の記述を訂正する |
| V2 | allowlist が本物の秘密名を無警告で通す (人間の誤設定への safety net が無い) | allowlist が実際に除外した名前があれば起動時に WARNING を 1 回出す (値は出さない、名前のみ)。攻撃面拡大ではなく誤用時の可視性の話 |
| V3 | T1 の構造的テストが「値が truthy」を見ているため、正当な falsy 値・truthy な default 値の未配線を見逃す (`commands.py:54` の `health_latch or HealthLatch()` は現に未配線でも truthy) | truthy 検査を廃止し、`Commands` を kwargs 記録 wrapper に差し替える spy 方式にする |
| V4 | 「strip 後の可視文字数」の実装 (`len(stripped)`) は Unicode コードポイント数であり、ゼロ幅文字・端末制御列を可視文字として数えてしまう | 表示・警告判定共通の正規化 (Unicode カテゴリ C* 除去 + 改行→空白) を新設し、AC-3b をこの定義で書き換える。全角括弧の検出・長さ上限・対話確認は本束の範囲外のまま (不採用) |

### 0.2 codex 設計レビュー r2 の反映 (2026-09-21、v1.2)

v1.1 (main `34d3dc9` コミット済) を対象に codex (terra/medium) 設計レビュー 2 周目を実施 (Critical 0 /
Important 4 / Minor 2)。裁定 (指揮者確定、全件採用) を反映して v1.2 とした:

| # | 指摘 | 裁定 |
|---|---|---|
| W1 | T2 の転送 pin 2 本 (`captured == {"which": "improve", ...}`) はどちらも trade=local の構成なので、`_check_cli_backend` の `which` を `"improve"` に固定する変異でも green のまま — improve 経路しか実際に踏んでいない | trade 側を非 local (claude) にした構成で `build_app` を実行し、`captured["which"] == "trade"` を pin するテストを追加する (trade→improve の順で呼ばれるため trade 側の検査⑤で例外を投げれば improve 側は呼ばれない) |
| W2 | WARNING テスト (AC-2f) が `caplog` に届かない — `agentic_fx` logger の `propagate=False` はプロセスに一度固定されると戻らず、`caplog.at_level(..., logger=...)` は handler を子 logger に付与しない (レベルを変えるだけ)。T2-M6/M7 の killer になっていない | `logging.getLogger("agentic_fx.service")` に直接 handler を付けて記録し、`finally` で外す contextmanager を使う。caplog の伝播・テスト実行順序に依存しない形にする |
| W3 | AC-2f の「秘密パターンにも当たる」条件とソート済みという仕様がテストで pin されていない — allowlist に載っているが非秘密パターンの名前で warn してしまう変異、集合順のまま出す変異のどちらも green になりうる | 否定側テスト (allowlist に載っていて env に実在するが秘密パターンに当たらない → WARNING なし) とソート順テスト (`{"Z_TOKEN","A_TOKEN"}` → 文言中で `A_TOKEN` が先) を追加する |
| W4 | `commands.py:63-67` の tokenizer (`line.strip().split()` → `" ".join`) が全角空白 (U+3000) を ASCII 空白へ畳んでから `backlog.add`/echo-back に渡す。「逐語表示」という表現が実態 (tokenizer 通過後の文字列) とずれている。加えて「保存は無加工」が C* 文字を含むケースで pin されていない | tokenizer は変更しない (本束の範囲外、既存挙動)。spec/plan の「逐語」の定義を「登録された課題文 (tokenizer 通過後 = 連続空白・全角空白は ASCII 空白 1 個に畳まれた状態) を表示用正規化したもの」に書き直す。目的は「何が登録されたかを目で確かめられること」であって入力行の再現ではない、と明記する。C* 文字を含む idea が `backlog.idea` に無加工 (tokenizer 通過後の文字列のまま、表示正規化はかからない) で残ることを pin するテストを追加する |
| M1 | T3 の新規テスト本数の記載が実数 (7 本) と食い違っていた | 本数の記載を実装対象に合わせて数え直す (今回の追加分も含む) |
| M2 | spec §6 の変更ファイル表が AC-2f・AC-3f〜h を含む v1.1 の AC 表と食い違っていた | §6 の表を v1.1/v1.2 の AC 一覧に合わせて更新する |

---

## 1. スコープと非スコープ

**スコープ**
- `src/agentic_fx/service.py`: `build_app` の `Commands(...)` 呼び出しに `policy_path` を追加 (件 1)。
  `_check_service_initial_env_has_no_secrets` に allowlist 適用 + `which`/`backend`/当たったパターンを
  メッセージに埋め込む + allowlist が実際に除外した名前があれば起動時 WARNING を 1 回出す (件 2、v1.1)。
- `src/agentic_fx/config.py`: `ServiceSettings.secret_env_allowlist: list[str] = []` を新設し
  `Settings.service` として追加 (件 2)。
- `src/agentic_fx/commands.py`: `improve add` の応答に、表示・警告判定共通の正規化 (v1.1、Unicode
  カテゴリ C* 除去 + 改行→空白) を通した登録文を echo-back + 条件付き警告 1 行 (件 3)。
- `config/settings.yaml.example`: 新規キー `service.secret_env_allowlist` を追記・コメント付け (件 2)。
- `tests/test_service_app.py`: 件 1 の配線テスト (spy 方式の構造的テストに変更、v1.1)、件 2 の
  allowlist / メッセージ / WARNING テスト (v1.1)。既存の `object()` 引数 stub 4 箇所・
  `which`/`backend` 未対応の monkeypatch スタブ 3 箇所の差し替え (v1.1、詳細はプラン Global Constraints)。
- `tests/commands/test_improve_commands.py`: 件 3 の echo-back / 警告条件 / 制御文字正規化テスト (v1.1)。

**非スコープ (明示)**
- **件 2 のパターン変更** (`_SECRET_ENV_PATTERNS` の中身・照合ロジック自体)。既存の回帰 pin
  (`tests/test_service_app.py::test_check_service_initial_env_has_no_secrets_rejects_each_pattern`
  の `"SECRETSTUFF"` = 境界なし部分一致の pin) を維持する。本束はパターンの**外側**に allowlist を足すだけ。
- **件 3 の入力ブロック・対話確認**。警告は表示のみで登録を止めない (R3)。2 段階確認は `[ops-ui]` へ (R4)。
- **改善 mission 側の「課題文が意味を成さない」判断ロジック**。`improve_loop.py` の
  `_upsert_backlog_idea` やプロンプト設計には触れない — mission が選んだ backlog 項目をどう解釈するかは
  改善ループの設計 (spec) 側の話で、この束の規模を超える。**別途 ticket として申告**
  (`[improve-mission-no-bearing-idea-handling]`、本書末尾「残余」参照)。
- **`Commands.__init__` の必須引数化**。下書きで検討した C 案 (デフォルト `None` 全廃) は
  既存の大量のテスト呼び出し元を洗い出す規模になるため見送り。件 1 の構造的テスト (AC-1c) で代替する。
- **`policy_path` 以外の未配線調査の再実施**。下書きで全数確認済み (5 個のオプション引数のうち未配線は
  `policy_path` のみ) — 本束はその結果を前提にする。実装プラン起草時 (2026-09-21) に `service.py:1054-1060`
  の `Commands(...)` 呼び出しで再確認済み: `health_latch=health_latch` / `improve_supervisor=improve_supervisor` /
  `plugins_root=plugins_dir` / `settings=settings` は渡されており、未配線は引き続き `policy_path` のみ。
- **件 3 の全角括弧 (`＜＞`・`【】`・`［］`) や `{…}` の検出 (v1.1、範囲外)**。R3 裁定の範囲は ASCII の
  `<…>`/`[…]` のみ。「プレースホルダのように見える」を広く意図する拡張は別途検討事項として別 ticket に回す
  (本束では追加しない)。
- **`Commands.dispatch(line: str)` の tokenizer (`commands.py:63-67`、`line.strip().split()` →
  `" ".join(args[1:])`) の変更 (v1.2、範囲外、codex r2 W4)**。この tokenizer は連続する空白・全角空白
  (U+3000) を ASCII 空白 1 個に畳んでから `text` を組み立てる既存挙動であり、件 3 の変更対象ではない。
  したがって件 3 が「表示する」のは入力行そのものの再現ではなく、**tokenizer を通過した後に実際に
  `backlog.add`/echo-back へ渡る文字列**である (§2.3・§3.3 の「逐語」の定義はこれに揃えた)。

---

## 2. 動作目線の設計 (誰が何をどう扱うか)

### 2.1 件 1: `policy add` を打つ人 (人間、`afx>` シェル)

**現状**: `afx> policy add 週末はドル円のみ` と打つと、常に
`policy directives の path が未配線です` が返り、`policy/directives.md` に何も書かれない
(本番の `Commands` に `policy_path` が渡っていないため)。取引・改善 mission への反映経路
(`policy/directives.md` を都度読み直す) 自体は生きているので、手でファイルを編集すれば効くが、
CLI からは一切書けない。

**直した後**: 同じコマンドを打つと `policy に追記しました` が返り、`policy/directives.md` に
`\n- 週末はドル円のみ\n` が追記される。次回以降の mission (取引・改善) の prompt にこの行が乗る
(`Policy.tail` が毎回読み直すため再起動不要 — 既存動作のまま、変更なし)。

### 2.2 件 2: CLI backend (claude/codex/opencode) でサービスを起動する人 (人間、シェル起動)

**現状**: `runner.trade.backend: codex` (または improve 側) でサービスを起動すると、
起動時検査⑤が自身の初期 env (`/proc/self/environ`) を走査し、`~/.bashrc` の
`CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` (llama-swap のコンテキスト長の表、秘密ではない) を
`OPENAI_` パターンで誤検知し、
`improve+claude backend refuses to start: ... contains secret-like variable name(s) ['CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS'] ...`
と表示して**起動を拒否する**。`which`/`backend` は実際が `trade`/`codex` でも文言は常に
`improve+claude` 固定で嘘をつく。暫定運用は `env -u CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS uv run python main.py`。

**直した後**:
- `settings.yaml` に何も足さずに起動した場合、拒否メッセージは実態を正確に言う:
  `trade+codex backend refuses to start: service initial env contains secret-like variable name(s) ['CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS'] (matched pattern 'OPENAI_') — improve/trade worker can read /proc/self/environ of same-UID processes (R10). If this name is NOT a secret, either (a) unset it before starting the service, or (b) add its exact name to service.secret_env_allowlist in settings.yaml. Otherwise move the value into .env.`
  (`which`/`backend` が実値、当たったパターン名を含む、次の一手 2 つを案内)
- `settings.yaml` に
  ```yaml
  service:
    secret_env_allowlist: [CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS]
  ```
  を足してから起動すると、この名前だけが除外され、他に秘密っぽい名前が無ければ起動できる。
  allowlist に載っていない秘密っぽい名前が env にあれば従来通り拒否される (守りは弱めない)。

### 2.3 件 3: `improve add` を打つ人 (人間、`afx>` シェル)

**現状**: `afx> improve add <案 1>` と打つと `backlog #77 を追加しました` とだけ返り、
登録された課題文そのものは画面に出ない。ユーザーは自分が打った内容の記憶に頼るしかなく、
手順書の置換記号を打ち間違えたことに気づく手掛かりが無い。後日、改善 mission #86 がこの
4 文字を「自分なりに解釈」して戦略を 1 本作り承認申請まで出した。

**直した後**:
```
afx> improve add <案1>
backlog #78 を追加しました: 「<案1>」
⚠ 短い/プレースホルダのように見えます。意図した内容であることを確認してください (削除・訂正は `backlog reject 78` の上で `improve add` をやり直す)
```
```
afx> improve add USDJPY のスプレッドが広い時間帯の指値精度を上げたい
backlog #79 を追加しました: 「USDJPY のスプレッドが広い時間帯の指値精度を上げたい」
```
(警告は R3 の条件に合致したときだけ 2 行目に出る。合致しなければ 1 行のみ。**登録はどちらも成立する** — 拒否しない。
警告判定と echo-back の表示はどちらも同じ正規化 (v1.1、§3.3 参照) を通した文字列を使う。ゼロ幅文字や
端末制御列 (`\x1b[...` 等) を含む入力ではこの正規化で除去され、除去が起きた場合は 3 行目に
`表示できない文字を N 個含みます` が付く。**backlog に保存する課題文自体は無加工のまま** — 正規化は
表示・警告判定にのみ使う。**(v1.2、codex r2 W4)** ここでいう「表示する」「保存する」課題文は、いずれも
`Commands.dispatch(line: str)` の tokenizer (`commands.py:63-67`) を通過した後の文字列 — 連続する空白・
全角空白 (U+3000) は tokenizer によって既に ASCII 空白 1 個に畳まれている (この tokenizer 自体は件 3 の
変更対象外、既存挙動)。件 3 が保証するのは「入力行そのものの再現」ではなく「実際に登録された文字列を
利用者が目で確かめられること」)

---

## 3. 件ごとの設計 (前提を疑う節・選択肢・退けた案)

### 3.1 件 1

**現物確認 (全数)**: `Commands.__init__` (`commands.py:38-61`) のオプション引数 5 個
(`health_latch` / `improve_supervisor` / `policy_path` / `plugins_root` / `settings`) のうち、
`build_app` の `Commands(...)` 呼び出し (`service.py:1054-1060`) が渡していないのは **`policy_path` のみ**。
他 4 個は正しく配線されている。`policy_path` が `None` のときの唯一の影響範囲は `dispatch()` の
`policy add` 分岐 (`commands.py:284-294`) だけで、他コマンドへの副作用はない。

既存の配線テスト (`tests/test_service_app.py:120` `test_build_app_wires_everything` とその周辺)
は `conn`/`broker`/`health_latch` は確認しているが、`policy_path`/`plugins_root`/`settings` は
1 本も検証していない。ユニット側 (`tests/commands/test_improve_commands.py:181-188`) は
`cmds._policy_path = ...` の直接代入で常に緑になっており、これが本番の未配線を隠していた
([[verify-integration-not-just-units]] の実例)。

**前提を疑う**: 「`policy_path` を足すだけ」で直るが、`Commands.__init__` は今後も引数が増える設計
(実際プラン 7〜11 で段階的に増えた)。個別テストを 1 本足すだけでは「次に増える引数」の配線漏れを
検出できない。→ **構造的な配線検査**を対案として採用する: `Commands.__init__` のオプション引数名の集合を
`inspect.signature` で取る。

**前提を疑う (v1.1、codex r1 V3)**: 「構築後のインスタンス属性が truthy であること」を配線の証拠にする
という前提も疑う。`commands.py:54` の `self.health_latch = health_latch or HealthLatch()` により、
`health_latch` は `build_app` が渡し忘れても `or HealthLatch()` で truthy な新規インスタンスが入り、
truthy 検査は通ってしまう (現在のプラン記述のままでも 5 引数中 1 本を見逃す、将来 `dry_run: bool = True`
のような truthy default を持つ引数が増えれば同様の穴が増える)。→ **spy 方式**に変更する:
`agentic_fx.service.Commands` (`service.py:25` で module 属性として import、`:1054` で呼ばれる ため
monkeypatch 可能) を、渡された kwargs を記録してから本物へ委譲する wrapper に差し替えて `build_app` を
1 回実行し、`Commands.__init__` の「既定値を持つ keyword 引数」全部が実際に渡された kwargs のキー集合に
含まれることを assert する。属性の truthy 検査は廃止する。

**選択肢**:
- **A (採用)**: `policy_path` 配線 + 個別配線テスト + 構造的シグネチャ突合テスト。
  採用根拠: このバグ自体が「引数を足したのに呼び出し側を直し忘れた」形なので、個別 pin だけでは
  同型の再発を防げない。構造的テストの実装コストは低い (introspection のみ、実行時オーバーヘッドなし)。
- B: `policy_path` の配線だけ直し、個別テストのみ足す。次に同型の引数が増えたとき再発しうる。**退ける**。
- C: `Commands` のオプション引数を必須化。全呼び出し元 (`tests/commands/*` 含む) の書き換えが要り、
  本束の規模を超える。**退ける** (将来検討事項として非スコープに記録)。

### 3.2 件 2

**現物確認**: `_SECRET_ENV_PATTERNS = ("_API_KEY", "TOKEN", "SECRET", "WEBHOOK", "ANTHROPIC_", "OPENAI_")`
(`service.py:200-201`)、照合は `any(pat in k.upper() for pat in _SECRET_ENV_PATTERNS)`
(`service.py:285-286`) — 完全な部分一致、アンカーも語境界もない。`CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS`
が当たったのは `"OPENAI_"` (文字列中央に語境界付きで出現)。検査は `_check_cli_backend` の末尾
(`service.py:373`) から呼ばれ、`which="trade"` と `which="improve"` の両方の経路で backend が
`local` 以外なら実行される (`build_app` は両方を呼ぶ) — **trade 側でも走る**。エラーメッセージは
`which`/`backend` を無視して常に `"improve+claude"` 固定 (`service.py:288-289`)。

設計書該当箇所 (`docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` R10、§1.4-⑤、
§2.2、§2.4) の脅威モデル: claude/codex/opencode worker は Landlock 下で `/proc` read-only を許可され、
同一 UID の他プロセスの `/proc/<pid>/environ` (exec 時点の初期 env のみ、`load_dotenv()` 経由の
`os.environ` セットは対象外) を読める。検査はこのプロセス自身の初期 env に秘密**名**が無いことだけを
名前ベースで確認する — 値は一切読まない設計。

**既存の回帰 pin**: `tests/test_service_app.py:3086-3097`
(`test_check_service_initial_env_has_no_secrets_rejects_each_pattern`) は `leaked_name` に
`"SECRETSTUFF"` (前後どちらの境界も無い純粋な部分一致) を明示的に pin している。コメントに
「`endswith` へ緩める変異が生存するのを防ぐため」とある — **部分一致であること自体が意図的な設計判断**。

**前提を疑う**: 「パターンを絞れば直る」という前提を疑う。`OPENAI_` を前方一致限定にしても、
`CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` は `OPENAI_` が `_` で挟まれた独立セグメントとして
文字列の中央に出現するため、語境界化 (`\bOPENAI_\b` 相当) でもこの誤検知は解消しない。
前方一致限定 (`k.startswith("OPENAI_")`) なら回避できるが、同時に「変数名の途中に埋め込まれた
本物の秘密名」の検出力を落とし、かつ既存 pin (`SECRETSTUFF`) と正面衝突する。**パターン側の調整では
守りを弱めずにこの誤検知だけを消すことはできない**。

**選択肢**:
- **A (採用)**: パターンは無変更 + 明示的ユーザー宣言のホワイトリスト (`service.secret_env_allowlist`、
  既定空) + エラーメッセージ修正 (`which`/`backend`/当たったパターン名を実値にする)。
  採用根拠: ①脅威モデル (R10) を一切弱めない — allowlist 未記載の変数は従来通り fail closed
  ②ユーザーの意思決定を明示的な設定行として残す (黙って通すのではなく「これは秘密でないと自分で
  確認した」という記録になる、[[product-vision-grown-by-its-user]] の「育てる」哲学と合致)
  ③実装コストが小さい (`leaked` の内包表記に 1 条件足すだけ) ④メッセージが正確になり、初めて
  遭遇したユーザーも正確に読める。
- B: パターンを前方一致/語境界化に変更。当該誤検知を解消できず、既存 pin と衝突する。**退ける**。
- C: 変数名でなく値の形状で判定。「値を見ずに名前だけで守る」という検査の設計原則 (`.env` 経由の値は
  最初から対象外) と矛盾し、値をエラーメッセージ/ログに載せるリスクを新たに生む。**退ける**。

**前提を疑う (v1.1、codex r1 V2)**: 「allowlist に載っていれば起動を通すだけでよい」という前提を疑う。
allowlist は利用者が自分の意思で正確な変数名を打鍵する必要がある誤用経路であり、改善/取引 worker が
`config/settings.yaml` を書き換えて allowlist に足す自動昇格経路は無い (`mission_worker.py:163-203` の
handshake に `db_path`/`plugins_dir`/config path が含まれず、Landlock で `code_root` は read-only —
確認済み)。したがって攻撃面の拡大ではないが、利用者が誤って本物の秘密名 (例: `OPENAI_API_KEY`) を
allowlist に書いてしまっても、現状は無警告で起動が通ってしまう — safety net が無い。
**直す (追加)**: allowlist に載っていて、かつ初期 env に実在し、かつ秘密名パターンに当たった名前
(= allowlist が実際に除外した名前) があれば、起動時に **WARNING を 1 回**出す。内容は変数名のみ
(ソート済み)、**値は出さない**。allowlist に載っているが env に無い名前・パターンに当たらない名前は
何も出さない (雑音にしない)。パターンに当たる名前を allowlist に書くこと自体は拒否しない (それが
allowlist の用途)。

### 3.3 件 3

**現物確認 (投入経路の全数)**: `backlog.add()` (`store/backlog.py:42-52`) の呼び出し元は
**`commands.py` の `improve add` (`commands.py:215-223`) 1 箇所のみ** — 人間の手入力経路はここだけ。
検証は「空文字でないこと」のみ、`idea.strip().lower()` の正規化以外に長さ・内容の検査は無い。
改善 mission 自身の discovery/研究由来の投入は別関数 `improve_loop._upsert_backlog_idea`
(`improve_loop.py:1391-1421`) で、こちらも同様に長さ・内容の妥当性検査は無い (対象外、後述)。
`upsert_system_note` (`backlog.py:19-38`) は system note 専用で人間入力とは無関係。

改善 mission 側に「選ばれた backlog 項目の課題文が意味を成さない」と判断して observation に
落とす専用経路は無い — 既存の `observation` 遷移は全て決定論的なライフサイクルイベント
(`interrupted`/`report_failed`/gate 不合格、`improve_loop.py:673`,`1815` 他) の発火であり、
LLM が内容を読んで判断する形の `observation` 落としは存在しない。

**前提を疑う**: 「入力検証で防ぐ」を最初の対策にする前提を疑う。プレースホルダ記号や極端な短さは
機械的に定義しづらく (正当な短い課題文もありうる)、ヒューリスティックな入力検証は
「今回のパターンだけ狭く塞ぐ」か「広く塞いで正当な短文まで弾く」かのトレードオフを常に抱える。
一方、今回の不具合の本質は「ユーザーが自分の入力ミスに気づける手掛かりが画面に無かった」ことであり、
入力を機械的に弾くことではない。**登録直後に登録された課題文をそのまま見せて確認させる方が
[[product-vision-grown-by-its-user]] の製品像に合う** — あらゆる種類の入力ミス (プレースホルダに
限らずタイプミス・貼り付け位置ずれ) に一般化して効き、実装コストも最小。

改善 mission 側の判断ロジック追加 (「意味を成さない」の検出、プロンプト変更) は、この束の対象外
(§1 非スコープ参照、別途 ticket 化)。

**選択肢**:
- **A (採用)**: echo-back (登録直後に、tokenizer 通過後の課題文をそのまま表示 — v1.2、codex r2 W4:
  「逐語」は入力行そのものの再現ではなく「実際に登録された文字列 (tokenizer が空白を畳んだ後) を見せる」
  という意味) + 非ブロッキング警告 (R3 の条件: 可視 4 文字未満、または全体が `<…>`/`[…]` の形)。
  登録は止めない。
  採用根拠: ①一般化する (echo-back は入力ミスの種類を問わず効く) ②資金・承認に関わらない低リスク
  操作なので「拒否」でなく「見せる + 軽く警告」で十分 ③実装が CLI 表示層のみに閉じ、遮断規律・承認の
  重みに触れない ④[[product-vision-grown-by-its-user]] の「失敗や空振りが見える・理由が分かる」に直接合致。
- B: echo-back のみ (警告なし)。実装最小だが、破綻パターンでも特筆しないので見落とし率が上がる。
  今回は R3 で A (警告あり) を裁定したため **退ける**。
- C: 最小文字数/プレースホルダパターンで拒否 (ブロッキング)。正当な短文を弾く恐れがあり、
  資金操作でも承認操作でもない操作を拒否までする理由が乏しい。**退ける**。
- D: 対話 2 段階確認。`Commands.dispatch(line: str) -> str` が 1 行 1 応答の同期モデルであり、
  複数行にまたがる確認フローを持たない。R4 により **`[ops-ui]` へ先送り**。

**前提を疑う (v1.1、codex r1 V4)**: 「strip 後の `len()`」が「可視文字数」だという前提を疑う。
`len()` は Unicode コードポイント数であり、ゼロ幅スペース (U+200B) や bidi 制御文字のような
「画面に何も描画しない」文字も 1 文字として数える。ゼロ幅文字 4 個を貼り付けると `len(stripped) == 4`
で「4 文字未満」警告が出ない (見た目は空なのに警告なしで登録される) し、`\x1b[2J` のような端末制御列を
含む原文をそのまま echo-back すると端末側で解釈され表示が壊れる恐れがある。
**直す (採用、範囲を絞る)**: 表示と警告判定の両方に使う正規化を 1 個定義する。改行 (`\n`) は空白 1 個に
置換してから (複数行の貼り付けが単語ごと連結しないように)、`unicodedata.category(ch)` が `C` で始まる
文字 (Cc 制御 / Cf 書式 = ゼロ幅・bidi 制御など) を除去し、`strip()` する。警告判定 (AC-3b) と
echo-back の表示 (AC-3a) はどちらもこの正規化後の文字列を使う。正規化で 1 文字でも除去された場合は
非ブロッキング警告と同じ枠で「表示できない文字を N 個含みます」を追加の 1 行で出す。**backlog に保存する
課題文自体 (`backlog.idea`) は従来どおり無加工** — 保存側の変更は本束の範囲外。
**不採用 (報告のみ、範囲外)**: 表示の長さ上限 (端末が折り返すだけで欠陥ではない — 実際に登録された
文字列を見せることが件 3 の目的)。全角 `＜＞`・`【】`・`［］`・`{…}` のプレースホルダ検出 (R3 裁定の範囲は
ASCII の `<…>`/`[…]` のみ — これを超える仕様拡張になるため本束では入れない)。対話確認は引き続き R4 により
`[ops-ui]` へ先送り。**(v1.2、codex r2 W4)** `Commands.dispatch` の tokenizer (`commands.py:63-67`、
連続空白・全角空白を ASCII 空白 1 個に畳む) の変更も不採用 — 件 3 は tokenizer より後段 (echo-back・
警告判定・保存) にのみ触れる。

---

## 4. 不変条件・遮断規律との関係

| ID | 不変条件 | この束での扱い |
|---|---|---|
| IV-1 | 秘密 env の守りを弱めない | 件 2 は `_SECRET_ENV_PATTERNS` を 1 文字も変えない。allowlist は**名前の完全一致のみ**で、パターン/正規表現/前方一致は不可 (実装で `k in allowlist` の単純な集合所属判定にする — ワイルドカードを許すと守りが弱まる)。allowlist が空 (既定) のときの挙動は現状と完全に同じ (既存 pin 全数が退行しないことを AC で確認する)。**(v1.1 追加)** allowlist が実際に名前を除外したときは起動時 WARNING を出す (拒否はしない、可視性のみ追加 — 守りの強さ自体は変えない) |
| IV-2 | 遮断 8 (改善プロンプトへの人間判断の漏洩) | 件 3 の echo-back・警告文言は `Commands.dispatch` の**戻り値 (端末表示) にのみ**書く。`backlog.idea` (ユーザーの原文そのもの、これは元々改善プロンプトに乗る設計) 以外の新しい文字列 (警告文・確認文言) を `improvement_backlog.last_result` / `approval_requests.reason` などの改善プロンプト注入経路の DB 列に書き込まない。件 1・件 2 は DB 書き込みを増やさない |
| IV-3 | 承認の重みを変えない | 3 件とも承認 (`approve`/`reject`/plugin 切替) のフローに触れない。件 3 の登録は従来通り `status='open'` で、改善ループの選択・承認プロセスは無変更 |
| IV-4 | `.env` を読まない / 値を読まない (件 2) | 件 2 の allowlist は変数**名**の集合であり、実装・テストとも env の**値**を読む経路を増やさない (既存の `_read_proc_self_environ_names` は名前だけを返す契約のまま) |

---

## 5. 受入条件 (ID 付き、観測可能)

### 件 1

| ID | 観測 |
|---|---|
| **AC-1a** | `build_app` で構築した `App.commands._policy_path == root / "policy" / "directives.md"` |
| **AC-1b** | `build_app` 経由で構築した `Commands` に対し `dispatch("policy add こんにちは")` を実行すると `policy/directives.md` に追記され、戻り値が `"policy に追記しました"` |
| **AC-1c** | **構造的テスト (v1.1、spy 方式に変更)**: `Commands.__init__` のキーワード専用オプション引数名の集合 (`inspect.signature` で取得) と、テスト側が保持する対応表のキー集合が一致する (新しいオプション引数が対応表に無ければこの比較で red — 対応表の更新を強制する)。かつ `agentic_fx.service.Commands` を、渡された kwargs を記録してから本物の `Commands` へ委譲する wrapper に monkeypatch で差し替えて `build_app` を実行し、シグネチャ上の全オプション引数名が実際に渡された kwargs のキー集合に**含まれる**ことを確認する (**属性の truthy 検査はしない** — `commands.py:54` の `health_latch or HealthLatch()` のように truthy な default 値を持つ引数は、truthy 検査では未配線を見逃すため) |

**変異案**: `policy_path=` の行を削除 → AC-1a/AC-1b/AC-1c が red (v1.1: spy が `policy_path` を kwargs に
見ないため AC-1c も red になる)。`Commands.__init__` にダミーのオプション引数を追加し
対応表を更新しない → AC-1c が red (この束で一番大事な変異 — 「次の再発」を模擬する)。
`build_app` から `health_latch=` を渡す行を外す → AC-1c が red (v1.1 追加、truthy 検査では拾えなかった変異)。

**AC-1c の pin における注意 (v1.2、codex r2 問い3)**: `agentic_fx.service.Commands` は `service.py:25` の
import と `:1054` の呼び出しのみで参照され、`isinstance(x, Commands)` の類の他参照は無い (確認済み) ため
spy wrapper への monkeypatch が `build_app` の他の挙動を壊さない。

### 件 2

| ID | 観測 |
|---|---|
| **AC-2a** | `settings.service.secret_env_allowlist` に `CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` を含めた settings で `_check_service_initial_env_has_no_secrets` を呼ぶと、他に秘密っぽい名前が無い限り例外を投げない |
| **AC-2b** | allowlist が空 (既定) のとき、既存の全 pin (6 パターン + 小文字混在 + `SECRETSTUFF`) が**そのまま red のまま**維持される (退行防止の回帰確認、既存テストの書き換えなしで緑のまま) |
| **AC-2c** | エラーメッセージに実際の `which` (`trade`/`improve`) と実際の `backend` (`claude`/`codex`/`opencode`) が入る (trade+codex で拒否したとき `improve+claude` と出ない) |
| **AC-2d** | エラーメッセージに「当たったパターン名」と「allowlist へ追加する」案内文言が入る |
| **AC-2e** | allowlist の一致は**完全一致のみ** — `CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` を allowlist に入れても `CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS_V2` のような類似名は除外されない (部分一致・前方一致に広げていないことの pin) |
| **AC-2f** (v1.1、新規) | allowlist に載っていて、かつ初期 env に実在し、かつ秘密名パターンに当たった名前 (= 実際に除外された名前) があれば、起動時に WARNING が 1 回出る。ログ本文に変数**名**のみ (ソート済み) を含み、値に相当する文字列は含まない。allowlist に載っているが env に無い名前・パターンに当たらない名前がある場合は WARNING を出さない (雑音にしない)。除外が 0 件のとき (allowlist が空、または allowlist と env の交差が無いとき) は WARNING を出さない。**(v1.2、codex r2 W3)** 「パターンに当たらない名前では出さない」条件と「複数名はソート順」の 2 点をそれぞれ個別にテストで pin する |
| **AC-2g** (v1.2、新規、codex r2 W1) | `build_app` は `_check_cli_backend` を `which="trade"` → `which="improve"` の順に呼ぶ (`service.py:851-852`)。trade 側が非 local backend のとき、検査⑤に実際に渡る `which` は `"trade"` である (改行を跨いだ「`which` が `"improve"` に固定されていても見た目上は動く」という変異を、trade 経路でも検出できることの pin) |

**変異案**: `k not in allowlist` の条件を落とす → AC-2a が red。allowlist 判定を `in` から
先頭一致 (`any(k.startswith(a) for a in allowlist)`) に緩める → AC-2e が red。
メッセージ文字列から `which`/`backend`/パターン名の埋め込みを外す → AC-2c/AC-2d が red。
WARNING の呼び出しを削除する、または条件を「allowlist 非空なら常に出す」に変える (実際に除外した名前の
有無を見ない) → AC-2f が red (v1.1 追加)。**(v1.2 追加)** allowlist 一致だけで秘密パターン判定を
省略する (`if k in allowlist:` の分岐内で `_matched_pattern` を確認せず常に WARNING 対象にする) →
AC-2f の否定側 (パターン不一致) が red。`sorted(excluded_by_allowlist)` の `sorted` を外す →
AC-2f のソート順 pin が red。`_check_cli_backend` 内で `which=which` を `which="improve"` に固定する →
AC-2g が red (v1.2、`which="improve"` 固定変異が v1.1 の 2 本の pin では検出できなかったことに対する追加)。

### 件 3

| ID | 観測 |
|---|---|
| **AC-3a** | `dispatch("improve add 何かの課題")` の戻り値に、登録した idea (v1.2、codex r2 W4: `Commands.dispatch` の tokenizer [`commands.py:63-67`] を通過した後の文字列 — 連続空白・全角空白は既にASCII空白1個に畳まれている、この tokenizer は件3の変更対象外) の**正規化後の表示文字列** (v1.1: 改行→空白 + Unicode カテゴリ C* 除去 + strip。制御文字・ゼロ幅文字を含まない通常の入力では tokenizer 通過後の文字列と一致する) が `「…」` の形で含まれる |
| **AC-3b** (v1.1 書き換え) | 正規化後の表示文字列が `<…>` または `[…]` で完全に囲まれている、または**正規化後の文字列の `len()`** (= Unicode カテゴリ C* を除去済みなので「可視文字数」に一致する) が 4 未満のとき、戻り値に警告行 (`⚠` で始まる) が追加される |
| **AC-3c** | AC-3b の条件に合致しない (通常の長さ・非プレースホルダの) idea では警告行が付かない (否定側 pin) |
| **AC-3d** | 警告が出ても `backlog.add` は実行され、登録された行の `status` は従来通り `'open'` (ブロックしない)。`backlog.idea` 列には **tokenizer 通過後・表示正規化前の文字列**がそのまま入る (表示用正規化は保存側にかからない、本束の範囲外) |
| **AC-3e** | echo-back・警告文言のいずれも `improvement_backlog.last_result` / `approval_requests.reason` 等 DB 列に新規の文字列を書き込まない (`dispatch` の戻り値以外に副作用が増えないことを確認する。activity ログの記録内容も従来 (`"#{bid} via shell"`) から変えない) |
| **AC-3f** (v1.1、新規) | idea にゼロ幅スペース (U+200B) を 4 個含む入力は、正規化後の可視文字数が 0 になり AC-3b の短さ条件に合致して警告が出る。かつ戻り値に「表示できない文字を N 個含みます」の行が追加される |
| **AC-3g** (v1.1、新規) | idea に端末制御列 (`\x1b[2J` 等) を含む入力では、戻り値 (echo-back・警告行とも) に `\x1b` に相当する文字が含まれない |
| **AC-3h** (v1.1、新規) | idea が複数行にまたがる入力 (`\n` を含む) では、正規化後の表示文字列は改行が空白 1 個に置換され、単語同士が連結しない |
| **AC-3i** (v1.2、新規、codex r2 W4) | ゼロ幅スペース (U+200B) を含む idea を登録すると、`backlog.idea` 列にはこの文字が無加工のまま残る (AC-3d の「保存側は表示正規化を通さない」を C* 文字を含むケースで個別に pin する — AC-3f の表示側とは別に、保存側が正規化されていないことを確認する) |

**変異案**: echo-back の文字列連結を消す → AC-3a が red。警告条件の `<`/`[` 判定を落とす、
短さ閾値 (`< 4`) を変える → AC-3b・AC-3c の両方 (正例・否定側) で検出。`backlog.add` 呼び出しを
警告分岐の中に誤って移動する (警告時にブロックしてしまう退行) → AC-3d が red。
正規化関数 (Unicode カテゴリ C* 除去) を丸ごと外す、または改行→空白の置換だけを外す → AC-3f/AC-3g/AC-3h が
red (v1.1 追加)。警告判定だけを正規化前の生文字列に戻す、または echo-back だけを正規化前の生文字列に戻す →
それぞれ AC-3b/AC-3f 側、AC-3a/AC-3g 側が red (v1.1 追加、正規化の適用箇所を個別に殺す変異)。
正規化後の表示文字列を `backlog.add` の `idea=` に渡す (誤って保存側にも正規化をかける退行) →
AC-3i が red (v1.2 追加)。

---

## 6. 変更ファイル一覧

| ファイル | 変更 | 件 |
|---|---|---|
| `src/agentic_fx/service.py` | `Commands(...)` に `policy_path=` 追加 / `_check_service_initial_env_has_no_secrets` に allowlist・`which`・`backend`・当たったパターンの引き渡し / `_check_cli_backend` の呼び出し変更 | 1・2 |
| `src/agentic_fx/config.py` | `ServiceSettings` 新設、`Settings.service` フィールド追加 | 2 |
| `src/agentic_fx/commands.py` | `improve add` 分岐: echo-back + 警告判定 | 3 |
| `config/settings.yaml.example` | 新規キー `service.secret_env_allowlist` を追記 (下記「settings.yaml.example の同期」参照) | 2 |
| `tests/test_service_app.py` | AC-1a〜AC-1c、AC-2a〜AC-2g (v1.2、M2 訂正: AC-2f・AC-2g を含める) | 1・2 |
| `tests/commands/test_improve_commands.py` | AC-3a〜AC-3i (v1.2、M2 訂正: AC-3f〜AC-3i を含める) | 3 |

### `settings.yaml.example` の同期 (CLAUDE.md 規約)

新規キー `service.secret_env_allowlist` を `config/settings.yaml.example` に追加する
(実装 task の対象、T2 が行う)。コメント案:
```yaml
service:
  secret_env_allowlist: []   # ⑤検査 (起動時、CLI backend のみ) が誤検知した exported env 変数の名前をここに書くと除外される。
                              # 名前の完全一致のみ (パターン不可)。値は見ない — 本当に秘密でないと自分で確認してから追加すること
```

**`config/settings.yaml` (ユーザーの個人設定、gitignore) は実装 task では触らない** — pydantic の
`Settings.service` はデフォルト値 (`Field(default_factory=ServiceSettings)`) を持つため、個人設定に
このキーが無くても起動できる。`CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` を実際に allowlist へ入れたい場合は
**ユーザー自身が** 個人 `settings.yaml` に以下の 1 行を足す (runbook 的な案内、実装ではない):
```yaml
service:
  secret_env_allowlist: [CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS]
```

---

## 7. 残余 (この束で閉じない項目)

- **`[improve-mission-no-bearing-idea-handling]` (新規申告)**: 改善 mission が選択した backlog 項目の
  課題文が意味を成さない場合に、mission 側で「解釈を試みず observation へ落とす」判断ロジックを持たせるか。
  §3.3 で検討したが、改善ループのプロンプト/判断設計 (spec) 側の変更でありこの束の規模を超えるため
  ticket 化のみ行い、設計は別途起こす。
- **未確認事項**: ユーザーの実 `~/.bashrc` の中身 (値・変数名とも) — 下書き作成時点ではこのセッション自身の
  env 変数名一覧 (`CLAUDE_CODE_MESSAGING_TOKEN` が `TOKEN` パターンに当たる例) で代替観測した。
  実機の `env | cut -d= -f1` (値は読まない) を別途確認すれば、allowlist に載せるべき他の名前が
  見つかる可能性がある。件 3 の echo-back 導入で実際に #77→#86 のケースでユーザーが気づけたかは
  実装後の実運用でしか確認できない。

---

## 8. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-21 | v1.0 | 初版。`tmp/design-ops-first-contact/design.md` (下書き、3 件とも A 案) のユーザー承認 (2026-09-21) を受けて spec 化。§0 に指揮者裁定 4 点 (R1〜R4: allowlist 置き場所 `service.*` 新設 / エラー文にパターン名を含める / 件 3 警告条件 = 4 文字未満 or `<…>`/`[…]` 完全一致・非ブロッキング / 対話確認は `[ops-ui]` へ先送り) を追加し、AC・変更ファイル表・不変条件表・`settings.yaml.example` 同期方針 (個人 `settings.yaml` はユーザー自身が追記するランブック扱い) を新設 | 下書き承認 + 指揮者裁定 | (本 commit) |
| 2026-09-21 | v1.1 | §0.1 に codex r1 の反映を追記。V1: 既存テストの `object()` 引数 stub が `AttributeError` になることを認め、Global Constraints・スコープ側の記述を訂正 (本番コードへの互換層は入れない)。V2: AC-2f (allowlist 除外時の起動 WARNING、値は出さず名前のみ) を新設、IV-1 に追記。V3: AC-1c を truthy 検査から spy 方式 (kwargs 記録 wrapper) に書き換え、`health_latch` の truthy-default 見逃しを解消。V4: AC-3a/AC-3b を Unicode 正規化 (改行→空白 + カテゴリ C* 除去) 基準に書き換え、AC-3f〜AC-3h (ゼロ幅文字・端末制御列・複数行) を新設。全角括弧検出・長さ上限・対話確認は不採用のまま (非スコープに明記) | codex 設計レビュー r1 (Important 4 件、Critical 0) | `34d3dc9` |
| 2026-09-21 | v1.2 | §0.2 に codex r2 の反映を追記。W1: AC-2g (trade 非 local 経路での `which` 転送 pin) を新設 — v1.1 の pin 2 本は trade=local のため `which="improve"` 固定変異を検出できていなかった。W2: WARNING テストを `caplog` から `logging.getLogger("agentic_fx.service")` への直接 handler 付与に変更 (`agentic_fx` logger の `propagate=False` 固定に依存しない)。W3: AC-2f に否定側 (非秘密パターン名) とソート順の pin を追加。W4: AC-3a/AC-3d の「逐語」を「tokenizer (`commands.py:63-67`) 通過後の文字列」と定義し直し、tokenizer の空白畳み込み変更は非スコープに明記、AC-3i (保存側は C* 文字を含め無加工) を新設。M1/M2: T3 の新規テスト本数記載・§6 変更ファイル表を実数/実 AC に合わせて訂正 | codex 設計レビュー r2 (Important 4 件・Minor 2 件、Critical 0) | (未コミット) |
