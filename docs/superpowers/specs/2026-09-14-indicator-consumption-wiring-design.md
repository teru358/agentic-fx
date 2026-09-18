# [indicator-consumption-wiring] 設計書 v1.6

束: 指標層 → 戦略層の配線。strategy plugin が `config.yaml` で宣言した配備済 indicator plugin の出力を、strategy worker 内で計算して `evaluate(df, indicators, ...)` に渡す。依存の版は `config.yaml` の `pin` (= `content_hash` の署名対象) に書き込む「ロック方式」で固定する。ユーザー裁定 U1〜U3 (2026-09-13)、下書き `tmp/design-indicator-wiring/design.md` v0.1〜v0.9 を codex 設計レビュー 9 周 (r1〜r3 sol、r4〜r6 sol、r7〜r9 terra) + opus 独立レビュー 1 周で収束 (r9: Critical 0 / Important 0 / Minor 0)。**v1.6 = /code-review 2 周目 CR2 (2026-09-18 ユーザー承認) による §2.7(b) の締め付け (再ロック時の質検査 skip → 自己一致除外)、および §2.7 の承認詳細 (i) 欄の母集団訂正**。v1.5 = §5 の遮断 8 に sink 一覧。v1.4 = 実装プランの codex レビュー r1 (2026-09-15) で判明した §6 S1 の U4 との自己矛盾を訂正**。v1.3 = 実装プランの指揮者裁定 (2026-09-14) で判明した §6 の 2 行 (F2 / P2') を実装プランの申し送りに合わせて改訂。v1.2 = 実装プランの着手前検証 (opus r1) で判明した §6 の 2 行 (C1 / R1) を現物に合わせて改訂。v1.1 = §7 の裁定を反映、実装着手可 (writing-plans へ)。

## 0. ユーザー裁定 (2026-09-13)

| # | 論点 | 裁定 |
|---|---|---|
| U1 | 渡す indicator の範囲 | **strategy が config で宣言した依存のみ** |
| U2 | indicator の出力契約 | **系列 (Series) も返せるように拡張** (スカラー返却は従来どおり有効) |
| U3 | 依存宣言の形 | **別名付き・params 上書き可** |
| U4 (2026-09-14) | indicator の `outputs` 宣言 | **新規承認 (submit / bless、kind=indicator) では必須**。宣言なしの既存配備 (`rsi_indicator` / `rsi_wilder`) は inventory に `outputs: null` で残り standalone (`get_indicators`) では従来どおり使えるが、**strategy の依存先にはできない** (resolver `outputs_undeclared` で fail closed)。依存させたければ `outputs` を足して再承認 |
| U5 (2026-09-14) | ロック時の `config.yaml` 書き換え | **全体を再シリアライズ** (`yaml.safe_load` → pin 追加 → `yaml.safe_dump`)。コメント・キー順は保持しない (lock 応答に差分を表示) |
| U6 (2026-09-14) | 実機 prerequisite の indicator 3 本 (sma / rsi / adx、系列版、`outputs` 宣言) | **fable がひな形を `plugins/_human/` に作り、ユーザーが submit → approve** (実装完了後、validator が系列を通してから) |

## 1. 前提を疑う (何が本当の問題か)

- 現状: `evaluate(df, indicators, signals, params)` の `indicators` は全経路で `None` (survey §2)。archive の strategy 5 系統は全部 RSI/EMA/ADX を自前計算 (§6)。配備済 indicator (`rsi_indicator` / `rsi_wilder`、いずれもスカラー返却) を実行するのは trade LLM の `get_indicators` だけ (§3)。
- 「渡すだけ」では解決しない事実 2 つ: (1) indicator は最終バー 1 点のスカラーしか返せないが strategy は前バー比較を使う → U2。(2) 改善 worker は配備済 indicator の存在も出力キー名も知らない → 露出面が要る。
- 依存を持つ strategy の実行物 = strategy コード + 依存 indicator コード + effective params。**identity を 1 つに保つ唯一の方法は、依存の版を strategy のファイルに書き込むこと**。`content_hash` は `plugin.py + config.yaml` の hash (`loader.py:116-127`) なので、`config.yaml` に pin を書けば「同じコード・別 indicator 版」は別ファイル = 別 hash になり、signals UNIQUE / 承認の最新決定・superseded / `.versions` / archive / 成績一致検査のすべてが**既存キー `(name, content_hash)` のまま**矛盾しない。
- 本束でやらないこと (YAGNI、明示): `signals` 引数の配線 (依然 `None`、pin で退行検出)、多 timeframe 依存、indicator 出力の DB 保存、live tick での indicator 単独実行、`execution_hash` 等の第 2 identity (撤回)、[indicator-first-seeding] の指標セット作成そのもの (実機観測の prerequisite としてのみ)、[improve-targeted-run]、[pine-script-to-plugin]。

## 2. 動作目線の全体像

### 2.1 strategy 作者 (人間 / 改善 worker)
```yaml
kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: levels
max_bars: 250
indicators:
  rsi: {plugin: rsi_wilder, params: {period: 21}}      # 探索中 (unpinned)
  adx: {plugin: adx, pin: "3f9a…64hex"}                 # ロック済 (pinned)
params: {...}
```
`evaluate` の中では `indicators["rsi"]["rsi"]` を読む (系列なら `pd.Series`、df と同じ index、warmup 行は NaN。スカラーなら `float`、NaN = 未確定)。作者は `max_bars` が「依存の warmup + 自分の lookback」を覆うよう宣言する (example docstring に明記、ハーネスは強制しない)。

**ライフサイクル (ラチェット)**: 探索中は `pin` なしでよい (staging の `run_backtest`、人間の `afx backtest run` は現在の inventory で解決)。**提出 (`submit_candidate` / `submit` / `bless`) は unpinned を固定文言で拒否**する (gate も行も作らない)。pin は明示的なロック操作で**ハーネスが書く** (agent が 64 hex を写さない): 改善 tool `lock_staging_deps(name)` / 人間 CLI `afx plugin lock --from _human <name>` (`submit`/`bless` と同じ `--from` 規約、`--from` なしは同じ固定文言で拒否)。ロック = 現在の inventory で解決した各 alias の `content_hash` を `config.yaml` の `pin` に書き込む (U5: `yaml.safe_load` → 各 alias に `pin` を足す → `yaml.safe_dump(sort_keys=False)`。コメントは失われるので lock 応答に書き換え前後の差分を出す。書き換え後の `config.yaml` が discover を通ることを同 tool 内で確認)。**テストした artifact == 提出する artifact** (改善 worker はロック後に `run_plugin_tests` / `run_backtest` を再実行する規律をプロンプトに書く。ロックは pin の追加だけで挙動は変わらない)。

**人間経路の正式手順**: 現行 `afx backtest run --plugin` は `plugins/` 直下を `discover` するだけで `_human` 候補を読めない (`cli.py:374-376`、`loader.py:314` の `_` 除外)。本束では人間の探索 backtest を足さず、**materialize → lock → submit (gate が in_sample/holdout を回す)** を正式手順とする: `afx plugin materialize <name>` (配備済を `_human` へ複製、既存) → 編集 → `afx plugin lock --from _human <name>` → `afx plugin submit --from _human <name>`。`afx backtest run --plugin <deployed>` は配備済 (必ず pinned) に対する `check` で従来どおり使える。

**再ロックの正式手順** (indicator 更新で依存 strategy が配備から外れたとき): 人間 = 上の手順 (materialize は `.versions` 実体からの複製なので pin 破れ後も可能)。改善 worker = `read_plugin_source(name)` → staging へ複製 → `lock_staging_deps` → 提出。そのため **phase 2 で落ちた strategy も `_snapshot_src` には残す** (§2.3: snapshot の材料は phase 1 の metas + phase 2 で落ちた strategy、inventory としては admit しない)。prompt の inventory に「pin 破れで配備から外れている strategy: N 本 (名前)」を出す。

### 2.2 loader (`discover`) — 形の検証のみ、fail closed
- `indicators` は strategy 専用、`outputs` は indicator 専用キー。他 kind に現れたら reject。
- **全 kind の `params`** を再帰的に JSON-safe 検証 (キー str、値 str / int / bool / 有限 float / None / list / dict、YAML date・set・bytes・±Inf・NaN は reject)。`PluginMeta.params` は**従来どおり `dict`** (`MappingProxyType` 化は撤回 — 現行 wire の `json.dumps` を壊す、r3 C3)。不変性は resolver が canonical な frozen 表現を別途持つ (§2.3)。
- reference object の許可キーは `{plugin, params, pin}`。`plugin` 必須 (`_PLUGIN_NAME_RE`、≤64)。`params` 任意 (JSON-safe)。`pin` 任意、`^[0-9a-f]{64}$`。別名 `^[a-z][a-z0-9_]{0,31}$`、重複不可。
- `outputs`: 非空 str list、各 `^[a-z][a-z0-9_]{0,31}$`、重複不可。loader では任意 (既存配備との discover 互換)。**承認時 (submit / bless、kind=indicator) は必須** — 無ければ固定 `ValueError("outputs_required")` で拒否、approval 行なし (U4)。
- **上限** (codex r5 C1、handshake 展開の無制限膨張を spawn 前に fail closed): `indicators` の要素数 ≤ 8 (`MAX_INDICATOR_DEPS`)、`outputs` ≤ 32、各 `params` の canonical JSON ≤ 8 KiB。resolver は展開後の canonical handshake 総 byte 数 ≤ 256 KiB (`MAX_HANDSHAKE_BYTES`、既存 `_STARTUP_MAX_BYTES` 65536 の応答側とは別の送信側上限) を検査し、超過は `IndicatorResolutionError(alias=None, reason="handshake_too_large")`。同一 indicator を複数 alias から参照するのは可 (U3) だが、この総量上限に含まれる。
- `PluginMeta.indicators: tuple[IndicatorRef, ...]` (宣言順、`IndicatorRef(alias, plugin, params: dict, pin: str | None)` frozen)、`PluginMeta.outputs: tuple[str, ...] | None`。
- **loader の reject reason 語彙** (codex r8 M2、`_reject(name, reason)` の `reason` 文字列。既存の `unknown config keys` 等と同じ形で固定): `indicators_not_allowed_for_kind` / `outputs_not_allowed_for_kind` / `indicator_ref_unknown_key:<key>` / `indicator_ref_missing_plugin` / `indicator_ref_bad_plugin_name` / `indicator_ref_bad_alias` / `indicator_ref_duplicate_alias` / `indicator_ref_bad_pin` / `params_not_json_safe:<path>` / `outputs_bad_entry` / `outputs_duplicate` / `too_many_indicators` / `too_many_outputs` / `params_too_large:<path>`。

### 2.3 依存の解決 — composition root ごとに 1 回、`ResolvedIndicatorSet` を配る
- 型 (`plugin/resolve.py`、immutable):
  - `ApprovedInventory(root: Path (実体パス、正規化済み), metas: tuple[PluginMeta, ...])`。`approved_plugins()` の戻りを包む型。
  - `ResolvedIndicator(alias, plugin_name, plugin_py: Path, content_hash, params: FrozenParams (再帰的に frozen: tuple / `frozenset` of items), max_bars, outputs, pinned: bool)`。
  - `ResolvedIndicatorSet(inventory_root: Path, items: tuple[ResolvedIndicator, ...] (alias 順), all_pinned: bool)`。`pin_object()` → `{alias: {plugin, content_hash, params}}` (plain JSON object、alias 順)。
  - `thaw(params) -> dict`: call ごとに完全に独立した plain JSON object を作る (deep)。
- `resolve_indicator_deps(meta, inventory, *, settings, pin_mode: Literal["require", "check", "ignore"]) -> ResolvedIndicatorSet`: 1 箇所。失敗 = `IndicatorResolutionError(alias, reason)`、reason 固定語彙: `not_found` / `not_indicator` / `over_max_bars_limit` / `params_not_json_safe` / `unpinned` (`require` で pin なし) / `pin_mismatch` (`require`/`check` で pin が inventory の hash と不一致) / `handshake_too_large` (§2.2 の総量上限超過、alias=None) / `outputs_undeclared` (依存先 indicator が `outputs` を宣言していない、U4)。(`max_bars` は「渡す履歴の最大本数」であって必要 warmup 本数ではないので、大小比較による拒否は入れない — codex r4 I3。warmup 不足は全 NaN として validator を通る仕様で、規律文と example docstring で作者に伝える。)`pin_mode` の使い分け: **`require`** = submit / bless / commit gate / `approved_plugins` 第 2 相 (pin 必須かつ一致)、**`check`** = 探索中の `run_backtest` / 人間 CLI (pin があれば一致を要求、無ければ通す)、**`ignore`** = ロック操作 (既存 pin が古くても名前で解決し直して上書きする — codex r4 部分指摘: `require`/`check` のままでは古い pin の再ロックに到達できない)。
- params の merge = indicator の params の deep copy に strategy 側を 1 段上書き → 再帰 JSON-safe 検証 → freeze。
- **`approved_plugins()` の二相**: 第 1 相 = 既存規律 (discover + 最新決定 approved + hash 一致) で indicator / signal / strategy を admit。第 2 相 = strategy を、第 1 相の indicator 集合に対して同じ `resolve_indicator_deps(pin_mode="require")` を通し、成功したものだけ最終 admit (失敗は warning + reason を log)。戻り型は **`InventoryBuildResult(inventory: ApprovedInventory, phase1_metas: tuple[PluginMeta, ...], resolved: Mapping[(name, content_hash), ResolvedIndicatorSet], rejected_strategies: tuple[RejectedStrategy(name, content_hash, alias, reason), ...])`** (codex r4 I2 + r5 I1: 第 2 相で成功した strategy の `ResolvedIndicatorSet` を保持し、service はそれを**再解決せず**そのまま producer / session に渡す。resolver 呼び出しは strategy ごとに 1 回、object identity を受入で観測)。`inventory.metas` は最終 admit のみ。snapshot 材料 (`copy_source_snapshot`) は `phase1_metas` (pin 破れ strategy を含む)、prompt の「pin 破れ N 本」は `rejected_strategies` から生成する。`approved_plugins(conn, plugins_dir, *, settings)` に settings を渡す (`max_bars_limit`)。既存の list 利用者 (`len` / index / equality を含む) は本束で `result.inventory.metas` へ**全数移行**する (`__iter__` だけの互換は不正確、codex r5 M1)。移行対象 = プランで `rg 'approved_plugins\(' src tests` の全結果 (少なくとも `service.py` / `mission_worker.py` / `improve_loop.py` / `improve_context.py` / `strategy_gate.py` / `tests/tools/test_plugin_loader.py` / `tests/test_service_app.py:305-334` (mock 戻り値を list 固定) / `tests/test_e2e_plugin_signal.py:202-206` (戻り値を直接 iteration)、codex r6 M1)。mock は `InventoryBuildResult` を返し、利用側は `.inventory.metas` と `settings=` の新契約に更新する。merge/lookup の規則は resolver 1 箇所。
- **composition root** (1 回構築し同じオブジェクトを配る):

| root | inventory | 配り先 / 備考 |
|---|---|---|
| live (`service.py` 起動) | 起動時 `approved_plugins` (hot reload なし) | 第 2 相を通った strategy だけが producer に渡る (producer は解決済みのみ受領)。session cache キーは `content_hash` (pin 込みなので実行物と 1:1) |
| trade worker の `get_indicators` (`mission_worker.py:925`) | **別 root** (子が handshake の `plugins_dir` から `approved_plugins` を再実行、indicator kind のみ) | trade LLM 向け参考情報であり、producer との版ずれは許容 (明記)。strategy の解決は行わない |
| 改善 mission | mission 開始時に materialize した `_snapshot_src` から 1 回 `ApprovedInventory` を作り `ImproveRunContext.inventory` に保持 (snapshot の材料は `phase1_metas`、prompt の pin 破れ表示は `rejected_strategies`、§2.1 再ロック) | prompt 表示 / `list_deployed_plugins` / `run_backtest` (`check`) / `lock_staging_deps` (`ignore`) / commit gate (`require`) の全部がこれ。staging を含まない = staging 内 indicator に依存不可 |
| 人間 CLI `afx backtest run --plugin` | strategy meta = `discover(root/plugins)` (現行不変、承認不要の手元評価) / 依存解決用 inventory = `approved_plugins(conn, root/plugins)` | `check`。解決失敗は **rc=1、固定 stderr `indicator_unresolved:<alias>:<reason>`、`backtest_runs` 行なし** (resolution は保存前・try の中) |
| 人間 CLI `afx plugin lock --from _human` | 同上の inventory | `ignore` |
| 人間 `approve_candidate` (P2 決定時) / `bless_candidate` | 同上の inventory | `require` で**解決のみ** (gate 再実行はしない)。submit → approve の間に indicator が更新されていれば `ValueError("indicator_unresolved:<alias>:pin_mismatch")` で pending のまま拒否 (版作成・symlink 切替に進まない)。superseded 判定 (0c) と同じ位置。**ロック**: strategy の承認・bless は `_plugin_lock` を **`sorted(set({strategy} ∪ 依存 indicator 名))`** (重複排除 + 名前昇順、同一 indicator の複数 alias で二重取得しない) で取得し、その内側で inventory 構築 → `require` 解決 → 切替を行う。bless は候補 (mutable な `_human`/staging) から依存名を lock **前**に読むので、lock 取得後に候補の `content_hash` と依存名集合を再読し、事前読取と完全一致しなければ全解放して固定文言 `candidate_changed` で失敗 (retry しない、codex r5 I2 の TOCTOU) (codex r4 I1: 名前単位ロックのままだと解決と切替の間に indicator の承認が別ロックで完了し、pin_mismatch をすり抜ける)。indicator の承認・切替は従来どおり自名ロック (同じロックを共有するので直列化される) |
| 人間回廊 submit / bless / gate | 同上 | `require`。1 回構築し in_sample と holdout の両 session と gate rows に同じオブジェクト |

- `StrategyAdapter(meta, resolved: ResolvedIndicatorSet, ...)` は必須引数 (依存なしでも空 set)。内部で再解決しない。`PluginSession` には `resolved` を渡し、containment は `resolved.inventory_root` で検査する (r3 C2)。

### 2.4 実行の場所 = strategy worker 内に同居 (§3 で根拠)
- **switch 後 crash の reconcile** (codex r5 I3): journal が `switched` (symlink 切替済・DB decided 前) のまま停止し、再起動までに indicator が更新されて pin が破れた場合、`reconcile_switch_journals` は当該 strategy + 依存名の lock 下で `require` 解決を再試行し、失敗なら**旧 target へ原子的に戻して journal を `reverted`**、approval は `pending` のまま残す (人間が再ロック → 再承認)。activity `switch_reverted reason=indicator_unresolved alias=… cause=pin_mismatch`。成功なら従来どおり `decided` まで進める。
- `PluginSession.__enter__`: main plugin の既存検査 + resolved の各 indicator について **`plugin_py` の実体パスが `inventory_root` 配下 (symlink 解決後)、`content_hash` 再計算一致、`check_source`** を起動直前に行い、検査済みの値だけを handshake に載せる。差し替えは `SandboxError`。
- handshake: 既存 + (kind == indicator のとき) `outputs: [..] | null` (main plugin 自身の `PluginMeta.outputs`、worker validator が standalone 応答の検証に使う。strategy / signal では送らない) + `indicators: [{alias, plugin_py, params: <thaw 済み plain JSON>, max_bars, outputs}]` (alias 順)。
- worker: 各 indicator を `spec_from_file_location(f"indicator_{alias}", plugin_py)` でハーネス側 import。`call()` ごとに `df.tail(max_bars_i)` の独立コピーと **params の deep copy** を渡して `compute` → 検証 (§2.5) → 系列を strategy の `df.index` に左 NaN 埋めで整列 (index 不一致は `SandboxError`) → `evaluate(df, indicators, None, thaw(params))`。main の params も call ごとに deep copy (nested mutation が次 call に残らない、r3 C3)。
- **共有ライブラリ状態** (r3 I7): `check_source` の `_DENY_NAMES` に `{set_option, reset_option, set_eng_float_format, seterr, seterrcall, setbufsize, set_printoptions}` を追加 (`_is_denied_bare_name` は `ast.Attribute.attr` にも効くので `pd.set_option(...)` を拒否できる、`sandbox.py:175-177`。`np.errstate` / `pd.option_context` は with 脱出で復元するので足さない)。属性 Store (`pd.options.mode.chained_assignment = None` 等) は deny 名にかからないので、**`check_source` に「AST 全体で `ast.Attribute` の `ctx` が `Store` または `Del` なら構文種別 (`Assign` / `AugAssign` / `AnnAssign` / タプル target / `for` target / `del` / `with ... as`) によらず reject」を追加** (codex r4 I7、現行 example に外部属性代入の正当用途なし)。deny 名にも `reset_option` を加える。worker は `pd.get_option("mode.chained_assignment")` (= `'warn'`) と `np.geterr()` が call 前後で不変であることを assert する。
- **graceful close**: `close()` は従来どおり戻り値なし。親が `{"op": "close"}` を送り、worker は `{"ok": true, "cpu_sec": ru_utime+ru_stime}` を返して exit。`sandbox_timeout_sec` 内に来なければ SIGKILL。**`PluginSession.cpu_sec: float | None`** は close 完了後に確定する property (正常終了で float、SIGKILL fallback / plugin error 後 / `__enter__` 失敗で worker 未起動なら `None`)。**caller (`run_in_sample` の呼び出し元の `finally` 後)** が読み、in_sample / holdout × pair ごとに合算して activity へ (§2.9 (e))。

### 2.5 indicator の戻り値検証 — 内部表現と wire 表現
- 共通 validator `validate_indicator_result(result, *, df_index, outputs)` は **worker 内**で呼ぶ。dict[str, value]、value は (a) `float`/`int` (bool・±Inf 拒否、NaN 許可) または (b) 系列 (`pd.Series` は `index.equals(df_i.index)` 必須、list/ndarray は `len == len(df_i)`、要素は数値、bool・±Inf 拒否、NaN 許可)。`outputs` 宣言済みは毎回宣言キー集合と完全一致 (warmup は NaN)。宣言なしは standalone (`get_indicators`) でのみ到達し `{}` 許可 (互換。依存経路には U4 により来ない)。
- 同居実行: 検証後 worker 内で Series 化 → strategy へ。
- standalone (`get_indicators`): wire `{key: float | {"series": [float|null,...]}}`、NaN は `null` にしてから `allow_nan=False` で送る。親が境界再検証。`market_tools.get_indicators` は系列を末尾値に射影し、**スカラー NaN と系列末尾 NaN はキー単位で落とす** (fail-open 維持)。

### 2.6 失敗時 = fail closed
- 同居実行の indicator 例外・検証不合格 → `SandboxError` → `backtest_failed`。live は既存どおり warning + tick skip (cursor 不変)。
- 未解決は root で捕捉 (§2.3 表、§2.8、§2.9)。

### 2.7 identity と pin — ロック方式 (作り直し)
- **pin = `config.yaml` の `indicators.<alias>.pin` (indicator の `content_hash`)**。strategy の `content_hash` が pin を含むので、実行物 identity は既存の `(name, content_hash)` のまま。**第 2 identity は作らない**。
- 配備済 strategy は必ず pinned (submit が unpinned を拒否するため)。`approved_plugins` 第 2 相は `pin == inventory の同名 indicator の hash` を要求し、破れた strategy は配備から外れる (warning、既存の hash 不一致と同じ扱い) = indicator 更新後は再ロック + 再承認。
- **r3 C1 のシナリオでの検算**: strategy S (pin I1) が approved。indicator を I2 に更新して承認 → S は第 2 相で外れる (live 停止、fail closed)。作者が S を再ロック (pin I2) → `config.yaml` が変わる → `content_hash` が S' に変わる → S' の approval は `(S, hash_S')` の新規行。旧 `(S, hash_S)` の pending/approve/reject は別キーで交錯しない。superseded 判定は既存の同名・別 hash 規律のとおり。signals は `(S, hash_S', pair, tf, bar_ts)` で旧行と衝突しない。`find_matching_approved_metrics` は `backtest_runs.content_hash = payload.content_hash` の既存 join のまま正しい。
- 承認 payload: `content_hash` (ファイル hash、従来どおり) に加え **`indicator_deps` = `pin_object()` (plain object)** を三経路 (submit / bless / `_build_approval_payload`) で同形に載せる (表示・監査用。identity には使わない)。
- indicator の承認詳細表示 (`commands.py::_approval_detail`) に依存 strategy を **2 欄**で列挙する (payload には入れない、表示時に `InventoryBuildResult` を逆引き): (i) 「この候補の hash に pin 済み」= `phase1_metas` のうち当該 alias の pin が候補 `content_hash` と一致する strategy (**v1.6 訂正**: 旧稿は `inventory.metas` = 第 2 相 admit 済のみだったが、まさにこの候補の hash に pin 済で「現承認 hash と不一致だから live でない」strategy = 承認すれば復帰する strategy が (i) にも (ii) にも現れず、人間の承認判断から完全に隠れていた) (ii) 「同名 indicator の別 hash に pin (承認すると外れる)」= `phase1_metas` のうち同名 indicator への pin が候補 hash と不一致の strategy。決定順 (id) で表示。
- **baseline 判定** (`strategy_gate.py` の同名 approved 探索) は `ApprovedInventory` の strategy 集合を正本にする (pin 破れで外れた strategy は `no_strategy`)。
- **pin は作者の設計判断ではなくハーネスの派生値**なので、「実質的に同じ候補か」の比較器 `same_modulo_pins(candidate, other)` (`plugin/resolve.py`、正規化 AST + `strip_pins(config)` の一致) を 1 つ置き、noop gate と質検査の両方が使う。ただし**同名の配備済 strategy との比較には「再ロック遷移」の例外**がある (codex r4 C2: 例外が無いと正式な再ロック経路 = 複製 → pin だけ I1→I2 → 提出 が必ず `noop_copy_of:S` で拒否される):
  - `is_relock_transition(candidate, deployed, inventory)` = `same_modulo_pins` **かつ** deployed の pin の少なくとも 1 つが現在 inventory と不一致 **かつ** candidate の pin が全て現在 inventory と一致。
  - (a) noop gate `find_noop_copy(candidate_dir, *, source_snapshot_dir, examples_dir, inventory: InventoryBuildResult)` (codex r8 I1: 現行は snapshot dir だけを読む API なので入力を明示注入する): `_examples/*` および**別名**の snapshot plugin との比較は `same_modulo_pins` (lock しただけのコピーは noop)。**同名**の snapshot plugin (= `inventory.phase1_metas` のうち同名のもの。pin 破れで最終 inventory から外れた S も snapshot には残る) との比較だけ `same_modulo_pins and not is_relock_transition(candidate, snapshot_S, inventory.inventory)` を noop とする。`is_relock_transition` の「現在 inventory の hash」は `inventory.inventory` (最終 admit 済 indicator) から引く。
  - (b) 成績質検査 `_check_duplicate_metrics_for_approval` の前段: 既承認版に対して `is_relock_transition` なら**再ロックのみの再提出**として、`find_matching_approved_metrics` の母集団から**候補と同名 plugin の承認行だけ**を除外したうえで検査する (**自己一致除外**、v1.6)。質検査そのものは skip しない — この一致判定は名前非依存 (pair/variant/source/base_interval + (trades, pf, avg_r)) なので、skip にすると**無関係な別 strategy との成績一致**まで見逃し、検査の目的が再ロック経路経由で回避できてしまう。除外は `approval_requests` の payload `$.name` で行う (`backtest_runs.plugin_ref` は `plugins/<name>` と `plugins/_staging/<mid>/<name>` の 2 形式があり契約ではない)。非再ロック時の母集団・SQL は不変。`is_relock_transition` の呼び出しは `BEGIN IMMEDIATE` の内側なので `noop_gate` と同じ `except (OSError, UnicodeError, SyntaxError, yaml.YAMLError)` で「再ロックではない」に倒す。
- **`backtest_runs.indicator_deps` 列は作らない** (r3 M1 の帰結): 依存は当該 `content_hash` の archived `config.yaml` から導出できる。監査列の読者がいないので削除 (r1 I6 の永続化面もこれで消える)。

### 2.8 gate の非送出規律 (境界別)
- `run_kind_gate` は解決エラーを捕捉して `GateOutcome(verdict_kind="indicator_unresolved", indicator_alias, reason)` を**返す** (raise しない)。成功時は **`GateOutcome.resolved: ResolvedIndicatorSet`** に解決結果を載せる (codex r6 I1)。`_run_full_gate` の 4 要素 tuple は維持し、submit / bless / `_build_approval_payload` は `outcome.resolved.pin_object()` から `indicator_deps` を作る (外で再解決しない)。in_sample session・holdout session・gate rows・payload が**同一 object** を参照し、resolver 呼び出しは候補ごとに 1 回。
- `_run_full_gate` / submit / bless は判別子から固定 `ValueError("indicator_unresolved:<alias>:<reason>")`。approval 行なし。unpinned は reason=`unpinned`。
- 改善 commit: `_finalize_gate_failed(reason="indicator_unresolved", mission_outcome="gate_failed")`、`last_result` = `indicator_unresolved` (固定文言のみ)、activity `gate_failed mission=… reason=indicator_unresolved alias=<alias> cause=<reason>`。gate 行なし。
- CLI: §2.3 表 (rc=1、固定 stderr、行なし)。

### 2.9 改善 worker への露出と RPC
- (a) prompt の `current_inventory.approved_plugins` は `ImproveRunContext.inventory` から生成し、indicator の `params` / `outputs` / `content_hash` を含める。
- (b) tool `list_deployed_plugins()` / `lock_staging_deps(name)` は**子 worker プロセス内**の staging tooldefs で実行される (親の `ImproveRunContext.inventory` を直接は参照できない、codex r6 I2)。親は mission handshake に **`inventory_view`** (JSON-safe: 最終 admit 済 plugin の `{name, kind, pairs, params, outputs (宣言なしは null = 依存不可), content_hash}` の list + `rejected_strategies` の `{name, alias, reason}` list) を載せ、子の両 tool はこの view だけを読む (snapshot ディレクトリを列挙しない = phase 2 で落ちた strategy が inventory に混ざらない)。`lock_staging_deps` は view の `content_hash` を pin に書く (staging・examples は参照しない)。view は `ImproveRunContext.inventory` と同じ `InventoryBuildResult` から 1 回だけ生成する。登録先 = staging tooldefs / `mission_registry` / `improve_mission.md` の使用可能 tool 列挙。`IMPROVE_FORBIDDEN` には入れない。
- (c) `run_backtest` の予算と解決は**プロセス境界を跨ぐ** (codex r4 C1 で確定: `reserve_backtest` は子 worker の tooldef (`improve_rpc_tools.py:135`)、`run_backtest_handler` は RPC の先の親 (`improve_loop.py:864`)。`ImproveRunContext.inventory` は親にしか無い。v0.4b の「同じクロージャ」は誤りで撤回)。したがって解決は親でしか行えず、予約は子で先に起きる。契約 = **予約 → 親 RPC → 未開始なら解放**: 子 tooldef は既存どおり検証後に `reserve_backtest` → RPC。親は backtest を始める前に `ImproveRunContext.inventory` で `check` 解決し、未解決なら backtest を走らせず `{"started": false, "error": "indicator_unresolved", "alias", "reason", "available": [indicator 名...]}` を返す。子は `started is False` のときだけ `counters.release_backtest(name)` (lock 内で `backtest_calls[name] -= 1`、0 未満にしない、`successful_backtests` は触らない) で予約を戻す。`error` キーがあるので `registry.py:101-102` により `errors` と recoverable refusal streak に自動計上 (同じ未解決を繰り返す agent は既存規律で abort)。`max_tool_calls` は常に +1。`started` キーが無い応答 (旧形式・RPC 失敗) では解放しない (fail closed = 予算は消費されたまま)。
- (d) `improve_mission.md` の規律文: 「strategy は配備済 indicator を `indicators:` で宣言して使う (`list_deployed_plugins`)。提出前に `lock_staging_deps` でロックし、ロック後に self-test / backtest を再実行する。無い指標は自前計算せず `不足指標: <名前と定義>` として backlog に起票する」。
- (e) activity `IMPROVE backtest_cpu mission=… plugin=… scope=in_sample|holdout pair=… deps=N cpu_sec=<float|null>`: adapter の直接 caller が `finally` 後に `cpu_sec` を読み、**`StrategyGateVerdict.cpu_samples: tuple[(scope, pair, cpu_sec|None), ...]`** に載せる (commit gate では `strategy_gate.py` が adapter を内部生成・close するため verdict 経由でしか ImproveLoop に届かない、codex r5 I4)。ImproveLoop は verdict から scope × pair ごとに 1 行ずつ activity を書く。`run_backtest_handler` 経路は handler が直接読む。例外終了時も `finally` で sample を積み `cpu_sec=null`。

### 2.10 example と prerequisite
- `docs/examples/plugins/rsi_indicator`: 系列返却 + `outputs: [rsi]`。warmup 全 NaN。
- `docs/examples/plugins/sma_cross`: 変更なし。
- 新規 `docs/examples/plugins/rsi_pullback` (依存ありの strategy 例、**判定式を固定**、codex r6 I5): `indicators: {rsi: {plugin: rsi_indicator, params: {period: 14}}}`、params `oversold: 30 / overbought: 70 / stop_loss_pips: 30 / take_profit_pips: 60 / pip_size: 0.01`。`r = indicators["rsi"]["rsi"]`、`prev, curr = r.iloc[-2], r.iloc[-1]`。いずれか NaN → hold。`prev <= oversold and curr > oversold` → open long (market、SL = close − 30 pips、TP = close + 60 pips)。`prev >= overbought and curr < overbought` → open short (対称)。それ以外 hold。`rsi_indicator` の RSI は Wilder 平滑 (`ewm(alpha=1/period, adjust=False, min_periods=period)`、avg_loss 0 → 100、両 0 → 50)。example は unpinned のまま (docstring で「提出前に lock」を説明)。
- 配備済 `plugins/rsi_indicator` は現物がスカラー `rsi_14` のまま。**実機観測の prerequisite = 人間が系列版 indicator 3 本 (sma / rsi / adx) を submit → 承認して配備** ([indicator-first-seeding] 第 1 片、本束の完了条件ではない)。
- 設計書 `2026-07-25` の契約文、`phase2-7-plugins.md:246`、sandbox 脅威モデル注記を更新。

## 3. 実行の場所: 同居 vs 別 worker

| | A. 同居 (採用) | B. 別 worker |
|---|---|---|
| IPC / evaluate | 従来と同じ 1 往復 | 1 + deps 往復 (in_sample deps=2 で float ≈ 23M 個の JSON 化) |
| CPU | `RLIMIT_CPU=60s` 共有 | 独立 |
| 隔離 | 同一 worker、indicator 例外 = session 終了 | 高い |

採用根拠: B は IPC が deps 倍で、非ベクトル化 1 本で 60 秒予算が破れた実績 (survey §4.3)。隔離の残余リスク (承認前 strategy と承認済 indicator の同居): (i) module 到達 (`import` 遮断 + `sys` allowlist 外) (ii) df / params の mutation (deep copy) (iii) pandas/numpy のグローバル状態 (deny 追加 + 回帰)。脅威モデルに追記。

## 4. 変更面

| 面 | 変更 |
|---|---|
| `plugin/loader.py` | 新キー (kind 限定)、reference object `{plugin, params, pin}` schema、全 kind params の JSON-safe 検証、`PluginMeta.indicators` / `.outputs` |
| `plugin/resolve.py` (新) | `ApprovedInventory` / `ResolvedIndicator` / `ResolvedIndicatorSet` (`inventory_root`, `pin_object`, `thaw`) / `resolve_indicator_deps(pin_mode)` / `IndicatorResolutionError` / `lock_config(candidate_dir, resolved)` (pin 書き込み) / `strip_pins` / `same_modulo_pins` / `is_relock_transition` / `InventoryBuildResult` |
| `tools/plugin_loader.py::approved_plugins` | 二相 (indicator → strategy の pin 検査)、`settings` 引数、`ApprovedInventory` で包む |
| `plugin/sandbox.py` | handshake 厳密定義、`__enter__` の indicator 検査 (root containment / hash / `check_source`)、`cpu_sec` property、standalone wire 再検証、`_DENY_NAMES` 追加 + 属性 Store reject、`_KIND_PAYLOAD_KEYS["strategy"] = ("df", "params")` |
| `plugin/worker.py` | 一意名 import、tail + deep copy → compute → validator → NaN 整列 → `evaluate(df, indicators, None, params)`、standalone wire、close 応答、グローバル状態 assert |
| `plugin/strategy_adapter.py` | `resolved` 必須、session へ、`cpu_sec` property、payload `{df, params}` |
| `service.py` / `plugin/signal_producer.py` | 起動時第 2 相の結果だけ producer へ |
| `mission_worker.py` / `tools/market_tools.py` | trade worker も `approved_plugins(conn, plugins_dir, settings=...)` を呼び `result.inventory.metas` (indicator kind のみ使用、phase 2 は strategy にしか効かない) を `market_tools` に渡す (codex r4 I4)。別 root であることを docstring に明記 |
| `backtest/cli.py` | resolution を保存前 try 内へ (meta は `discover` のまま、inventory は `approved_plugins`)、rc=1 固定 stderr、`afx plugin lock --from _human` サブコマンド |
| `plugin/approval.py` / `strategy_gate.py` / `switch.py` | root で resolved 1 回、`GateOutcome.verdict_kind="indicator_unresolved"` + フィールド、`_run_full_gate` 固定 ValueError、unpinned 拒否、`approve_candidate` の決定時 `require` 解決、baseline を inventory 正本に、payload に `indicator_deps` |
| `plugin/noop_gate.py` | `same_modulo_pins` + 同名は `is_relock_transition` 例外 |
| `loops/improve_loop.py` / `loops/improve_run_context.py` | `ImproveRunContext.inventory`、snapshot 材料に pin 破れ strategy を含める、`run_backtest_handler` の解決 + `started`、commit gate へ resolved、`_build_approval_payload`、質検査前段の再ロック判定、`gate_failed reason=indicator_unresolved`、`backtest_cpu` activity、docstring の「配備済 3 本」訂正 (`:222`) |
| `tools/improve_rpc_tools.py` / `mission_counters.py` | 子: `started is False` で `release_backtest(name)`。counters に `release_backtest` (lock 内、0 未満禁止) を追加 |
| `tools/improve_staging_tools.py` / `mission_registry.py` / `improve_mission.md` | `list_deployed_plugins` / `lock_staging_deps` + 許可列挙 + 規律文 (全 NaN 系列は warmup 不足の兆候、`max_bars` の決め方) |
| `loops/improve_context.py` | inventory 表示を snapshot 由来に、`params` / `outputs` / `content_hash` |
| `commands.py::_approval_detail` | indicator 承認詳細に依存 strategy 列挙 |
| `tools/market_tools.py::get_indicators` | wire 系列の末尾射影、NaN キー除去 |
| `docs/examples/plugins/` | `rsi_indicator` 系列化 + outputs、新規 `rsi_pullback` |
| 設計書 | `2026-07-25` 契約文、`phase2-7-plugins.md:246`、脅威モデル |
| tests | §6 + `test_strategy_adapter.py:167-170` 反転、`test_sandbox.py` payload 縮小、approval payload 三経路契約 |

**DB スキーマ変更なし** (`indicator_deps` 列は撤回)。

## 5. 遮断・規律との照合

- 遮断 8: 新 tool / inventory 追加分は params/outputs/content_hash のみ。`last_result` 新語彙 = `indicator_unresolved` (alias なし)。
- **遮断 8 の sink 一覧** (改善 worker に文字列が届く経路。pin は**この 6 つすべて**に対して置くこと — 1 sink だけ見る pin が他の sink の漏れを見逃した事故が段 0 で 2 件 (M4 / M14)):
  1. `improvement_backlog.last_result` — 固定文言のみ (`indicator_unresolved` / `outputs_required`)。alias も cause も付けない
  2. `gate_failed` activity の `reason` — 同上。alias/cause は**activity 行の追加フィールド**にだけ書く (activity は agent に渡らない)
  3. prompt (`improve_mission.md` + `current_inventory`) — §2.9 (a) の`params` / `outputs` / `content_hash` のみ
  4. **改善 RPC 応答** — §2.9 (c) の `{"started": false, "error": "indicator_unresolved", "alias", "reason", "available": [...]}`。**alias / reason はここでは載せる** (解決失敗の種別 = `pin_mismatch` 等であり holdout 由来の情報ではない。agent が自分の `config.yaml` を是正するのに必要 — codex r4 C1 で確定)
  5. 提案レポート本文 (`reason` / `report_detail`) — 固定文言のみ
  6. `inventory_view` (子 tool `list_deployed_plugins` / `lock_staging_deps` が読む) — §2.9 (b) の 6 キーのみ
  いずれの sink にも **holdout の数値・段名 (`in_sample`/`holdout`)・pair・baseline 差分**を載せない。
- tool 予算: `list_deployed_plugins` / `lock_staging_deps` は `max_tool_calls` のみ。未解決 `run_backtest` は予約を解放するので backtest 枠を消費しないが `errors` / refusal streak に計上。
- `IMPROVE_FORBIDDEN` 非交差 (pin)。`_KNOWN_CONFIG_KEYS` fail closed (kind 限定 + allowlist)。`check_source` allowlist + deny 追加、`__enter__` で再適用。`approved_plugins` の hash 一致規律 = 第 1 相そのまま、第 2 相はその延長。`run_kind_gate` 非送出 / `_run_full_gate` 送出。noop gate は pin 除去後の config で比較 (§2.7、ロックだけで noop を回避できない)。外向きリクエストなし。Landlock 変更なし。

## 6. 受入基準 (pin 可能な形、fixture を固定)

**fixture**: テスト用 plugins root に承認済 (approval_requests に approved 行 + `.versions` symlink) として `sma` / `rsi` (系列、`outputs: [rsi]`、`period: 14`) / `adx` (系列、`outputs: [adx]`) の 3 indicator と、`rsi_pullback` (pinned、`max_bars: 200`) を置く。ohlcv fixture = test DB へ `import_history_bars(source="dukascopy")` で投入 (論理 source は既存 `IMPORT_SOURCES` の `dukascopy`、ネットワークは触らない)。`now = 2026-03-01T00:00:00Z`、`holdout_months = 1`。**bar 生成式 (決定論)**: `ts_k = 2025-11-03T00:00:00Z (月曜) + k×5min` (k = 0 … 33,983、末尾 `2026-02-28T23:55Z`、欠損なし、週末も生成 — runner が `is_market_open` で評価を止めるだけ、codex r7 I1)。`close_k = 150.0 + 2.0×sin(2πk/288) + 0.5×sin(2πk/2016)`、`open_k = close_{k-1}` (`open_0 = 150.0`)、`high_k = max(open_k, close_k) + 0.05`、`low_k = min(open_k, close_k) − 0.05`、`volume_k = 100`。区間 = in_sample `[2025-11-03, 2026-02-01)`、holdout `[2026-02-01, 2026-03-01)`。**評価回数は本文で固定しない** (週末・12/25・1/1 の休場で暦格子より減る): 受入テストは同じ `market_hours.is_market_open` を 1h 格子に適用して期待評価 timestamp 列を導出し、**decision sink** (下記) が記録した timestamp 列と完全一致することを検査する。warmup 観測: 最初の評価 (月曜 01:00Z、bar 1 本) から 14 本目までは `rsi` が NaN で hold、15 本目以降で値が入る (開始を月曜 00:00Z にしたのはこのため)。**decision sink** (codex r7 I2): `StrategyAdapter(..., decision_sink: Callable[[datetime, dict], None] | None = None)` を追加し、evaluate ごとに `(bucket_end, strategy の生 decision dict)` を渡す。`backtest_runs` は metrics しか持たず `orders` は open しか表せないので、hold を含む全時点の照合はこの sink でしか取れない。本番経路は `None` (不変)。テスト側の独立参照実装 (Wilder RSI + 判定式を pandas で転写、plugin コードは import しない) が同じ timestamp 列に対して期待 decision を出し、sink の記録と逐次比較する。**比較対象** (codex r8 I3、strategy 契約の語彙に合わせる): `(action, direction)` の組 = `("hold", None)` / `("open", "long")` / `("open", "short")`、および open のときは `entry_type == "market"`、`stop_loss` / `take_profit` を `close ∓ 30 pips` / `close ± 60 pips` (pip 0.01) と ±1e-9 で一致。`rationale` は比較しない。

| # | 基準 | 観測点 |
|---|---|---|
| U4a | submit / bless (kind=indicator) で `outputs` 無し → `ValueError("outputs_required")`、approval 行 0。改善 commit gate でも同じ固定文言で gate 不合格 | |
| U4b | `outputs` 宣言なしの配備済 indicator を依存に書いた strategy → resolver `outputs_undeclared` (require / check とも)。standalone `get_indicators` では従来どおり動く | |
| L1 | loader 表駆動: 各 kind × `indicators`/`outputs`/reference object の unknown key / 型違反 / 別名規則 / pin 形式 / params 非 JSON-safe (date, set, Inf, NaN, bytes) / 上限 → §2.2 の固定 reason 文字列 (完全一致) と reject (戻り集合に含まれない) | `discover` の warning 文字列と戻り集合 |
| R1 | resolver 表駆動: `not_found` / `not_indicator` / `over_max_bars_limit` / `params_not_json_safe` / `unpinned` (`require`) / `pin_mismatch` (`require`/`check`) / `handshake_too_large` の各 reason、`ignore` が古い pin を無視して解決すること、成功時の `pin_object()` の alias 順と値の型。**上限境界** (codex r6 I3): deps 8 通過 / 9 拒否、outputs 32 / 33、params 8 KiB ちょうど / +1 byte、**handshake 256 KiB / +1 byte は `MAX_HANDSHAKE_BYTES` 定数を monkeypatch して `>` 判定そのものを pin する** — 正常入力からは到達不能 (loader が通す最大 = deps 8 × params 8 KiB (+ strategy 側の上書き 8 KiB) で handshake ≒ 136 KB < 256 KiB) なので、実データで到達させる試験は成立しない。上限は「将来 params 上限を緩めたときの最後の壁」として残す。いずれも拒否時に worker spawn 0 回 | 返り値 / 例外フィールド / spawn spy |
| A1 | `rsi_pullback` (依存 1) が `evaluate_strategy_adoption_gate` (strategy gate 経路) を完走 | `backtest_runs` に candidate の `scope='in_sample'` 行 1 (`content_hash` = pinned config の hash) と、baseline 不在時に gate が合成する `no_strategy` 行 1 |
| A1-b | 同 example を `holdout.run_in_sample` 直接 | candidate 行 1 のみ (`no_strategy` 行は作られない) |
| A1' | CLI: 配備済 (pinned) `rsi_pullback` に対し `afx backtest run --plugin rsi_pullback --symbol USDJPY --source dukascopy --from 2025-12-01 --to 2026-01-01` | rc=0、`scope='human_custom'` 行 1、meta は `discover` 由来 (承認行を消しても rc=0 で走る = 手元評価契約の維持) |
| A1'' | CLI 未解決 (not_found / not_indicator / over_max_bars_limit): rc=1、stderr = `indicator_unresolved:<alias>:<reason>` 逐語、`backtest_runs` 行数不変、traceback なし | |
| A2 | `approve_candidate`: submit 後に indicator を更新 → approve → `ValueError("indicator_unresolved:<alias>:pin_mismatch")`、approval は pending のまま、`.versions` に新版なし、symlink 不変 | |
| F1 | adapter: `resolved` 無し → `TypeError`、worker 0 | |
| F2 | service 起動: pin 破れ strategy → warning 1 行 (reason 込み)、producer の plugin 一覧に含まれない (session cache 未登録は producer 一覧に無いことの帰結なので観測しない) | |
| F3 | `run_kind_gate`: 未解決 → `verdict_kind == "indicator_unresolved"` + alias + reason、例外なし、行数不変 | |
| F3' | `_run_full_gate` / submit / bless: unpinned → `ValueError("indicator_unresolved:<alias>:unpinned")`、approval 行 0、gate 行 0 | |
| F4 | 改善 RPC 未解決: `{"started": false, ...}`、`backtest_calls[name]` が呼び出し前後で不変 (予約 → 解放)、`successful_backtests` 不変、`errors` +1、recoverable streak +1、`max_tool_calls` +1、`last_result` 不変。staging 内 indicator 指定は `available` に staging 名なし。`started` キー無し応答では解放されない | counters の全フィールド |
| F5 | 改善 commit gate 未解決 (unpinned 含む): `gate_failed reason=indicator_unresolved`、`last_result == "indicator_unresolved"`、gate 行 0、approval 行 0 | |
| V1 | validator: index 不一致 / 長さ不一致 / bool / ±Inf / outputs 集合不一致 → `SandboxError` (親に届く)。NaN 通過。全 kind の main plugin params が wire を通る (dict) | |
| V2 | mutation 回帰: indicator が df / params (nested list/dict) を書き換えても strategy と後続 indicator と**次回 call** は元の値。`pd.set_option(...)` / `pd.reset_option(...)` / `pd.options.mode.chained_assignment = None` / `pd.options.display.max_rows: int = 5` (AnnAssign) / `(a, np.x.y) = ...` / `for pd.options.x.y in ...` / `del pd.options.x.y` を含む plugin がそれぞれ `check_source` で reject。worker の不変 assert = `pd.get_option("mode.chained_assignment") == 'warn'` と `np.geterr()` | |
| V3 | 差し替え拒否: 解決後に indicator ファイルを書き換えると `__enter__` が `SandboxError`。`inventory_root` 外の実体パスも `SandboxError` (live / snapshot / human root の 3 root) | |
| S1 | standalone: 配備済 `rsi_wilder` 相当 (スカラー、outputs なし) は無変更で **standalone `get_indicators` でのみ**従来どおり使える (**strategy の依存先にはできない** — resolver が `outputs_undeclared` で fail closed、それは U4b の観測点)。系列は末尾値へ射影し、スカラー NaN / 系列末尾 NaN のキーは落ちる | |
| P1 | ロック: `lock_staging_deps` / `afx plugin lock --from _human` が `config.yaml` の各 alias に `pin` を書き、`content_hash` が変わり、書き換え後も `discover` を通る。既に同じ pin なら no-op、**古い pin は上書き** (`ignore`)。`--from` なしは固定文言で拒否 |
| P1' | snapshot: phase 2 で落ちた strategy が `_snapshot_src` に残り `read_plugin_source` で読める、inventory (`list_deployed_plugins`) には出ない、prompt に「pin 破れ N 本」が出る | |
| P4 | noop: `_examples/rsi_pullback` を逐語コピーして lock した候補は `noop_copy_of:_examples/rsi_pullback` で reject。配備済 S (pin I1、I2 承認済で pin 破れ) を複製して pin I2 に再ロックした候補は noop にならず gate に進む。同じ複製を pin I1 のまま (再ロックなし) 提出すると `noop_copy_of:S` | |
| P5 | 再ロック再提出: indicator を出力不変の変更で更新 → 依存 strategy を再ロック → 再提出の成績が既承認版と一致しても `duplicate_metrics_of` で降格されない (pin 除去 config + AST 一致で母集団除外) | | |
| P2 | pin 検算 (r3 C1 シナリオ): S(pin I1) approved → I2 承認 → S 除外 (warning) → 再ロック → hash 変化 → approval / signals / backtest_runs の各行が新 hash で旧行と分離される (approval 新規行、`signals` 2 行挿入成功、`latest_in_sample_metrics(new_hash)` が旧行を返さない)。`find_matching_approved_metrics` は仕様どおり**旧 hash を返す** (重複検出 API) — その結果を再ロック時に無視する caller 側は P5 で検証 | |
| P2' | 並行: S の approve 中 (解決後・切替前) に I2 の承認が待たされる (名前昇順ロック)。`_plugin_lock` の取得順序が常に名前昇順・重複なしであることを spy で pin する (順序が崩れる変異を検出) | |
| P2'' | lock 集合: 同一 indicator を 2 alias で参照する strategy の approve が deadlock せず、当該 indicator の lock 取得は 1 回 (`_plugin_lock` spy)。bless: 事前読取 → lock 取得の間に候補 `config.yaml` を差し替えると固定文言 `candidate_changed`、approval 行 0、`.versions`・symlink 不変、全 lock 解放 | spy / DB / FS |
| P3 | approval payload 三経路に `indicator_deps` (plain object) が同形。`list_deployed_plugins` / prompt inventory が同じ snapshot 由来 (prepare 後に live `plugins/` を差し替えても不変)、staging・examples を含まず、`IMPROVE_FORBIDDEN` と非交差 | |
| P3' | resolver 呼び出し 1 回 + 同一 object (r5 I1 / r6 I1): gate を通す test double で `resolve_indicator_deps` の呼び出し回数が候補ごとに 1、in_sample session と holdout session に渡った `resolved`、`GateOutcome.resolved`、payload の `indicator_deps` を作った元が同一 object (`is`)。service 起動でも strategy ごとに 1 回で producer の session が `InventoryBuildResult.resolved[(name, hash)]` と `is` 一致 | resolver spy / `is` |
| D1 | indicator 承認詳細: I2 承認前 = (ii) 欄に S、承認後 = S は inventory から外れ (ii) 欄に残る、S 再ロック後 = (i) 欄に S' | `_approval_detail` の出力 |
| R2 | reconcile: switch 直後 (journal `switched`、新 version directory は作成済) で停止 → 依存 indicator を別 hash に更新・承認 → 再起動 → `reconcile_switch_journals` が strategy + 依存名の lock 下で `require` 解決に失敗 → live symlink は旧 target、journal `reverted`、approval は `pending`、**新 version directory は `.versions` に残る** (現行 reconcile も version store を回収しない — 回収は本束の範囲外、GC は既存の archive/versions 規律に任せる、codex r8 I2)、activity `switch_reverted reason=indicator_unresolved alias=<alias> cause=pin_mismatch` 逐語。pin が破れていなければ従来どおり `decided` | journal / symlink / approval / `.versions` / activity |
| C1 | graceful close: **正常終了・plugin error 後ともに `cpu_sec` は float** (plugin コード自身の例外はセッションを `_dead` にしない既存契約のため graceful close が成立する)、**`None` は SIGKILL fallback (timeout 後の強制終了) と worker 未起動 (`__enter__` 失敗) の 2 経路のみ**。activity `backtest_cpu` 行の逐語 (scope / pair / deps / cpu_sec) | |
| N1 | `signals` 引数は依然 `None` (worker spy) | |

**運用観測 (受入ではない、A4 で)**: prerequisite 3 indicator 配備後、deps=0 と deps=3 の同一 strategy で in_sample 完走し `backtest_cpu` の差を記録。目安 = deps=3 が 60 秒予算の 50% 未満。

## 7. 未決

なし (v1.1 で U4〜U6 として裁定済)。

## 8. codex r3 対応表

| r3 | 対応 |
|---|---|
| C1 identity | §2.7 作り直し (ロック方式、`execution_hash` 撤回)。P2 で検算 |
| C2 inventory root | `ResolvedIndicatorSet.inventory_root`、V3 |
| C3 params 不変化 | `MappingProxyType` 撤回、resolver 側 freeze + call ごと deep thaw、V1/V2 |
| I1 bootstrap | `approved_plugins` 二相 + settings、resolver 1 箇所 |
| I2 成績一致 join | `execution_hash` 撤回で既存 join のまま (P2) |
| I3 二相予約 | 最終契約 = 予約 (子) → 親 RPC で解決 → `started:false` のときのみ `release_backtest` (v0.4b で一度「予約前に解決」としたが子から親 inventory を読めないため v0.5 で撤回、r4 C1)。予算契約の正は F4 |
| I4 cpu 伝播 | `PluginSession.cpu_sec` property (close 後に確定、`close()` は戻り値なし)、caller が `finally` 後に読む、C1。commit gate は `StrategyGateVerdict.cpu_samples` 経由 (r5 I4) |
| I5 CLI 境界 | §2.3 表、A1'' |
| I6 trade worker root | 別 root と明記 |
| I7 共有 module 状態 | deny 追加 + 回帰、V2 |
| I8 §6 実行可能性 | fixture 固定、表駆動 L1/R1、性能を運用観測へ分離 |
| M1 deps の型 | payload は `pin_object` (plain object)、`backtest_runs.indicator_deps` 列は撤回 (v0.4b で `pin_json` も削除) |
| M2 signal_tools | `execution_hash` 撤回で意味変更なし |

## 9. opus r4a 対応表

| opus | 対応 |
|---|---|
| C1 fixture 期間 | §6 を (最古バー, now, holdout_months=1) で書き直し |
| I1 人間 CLI 到達性 | §2.1 人間経路 = materialize → lock → submit、§2.3 表を meta/inventory 2 列に、A1' は配備済に対する check |
| I2 再ロック手順 | §2.1 に人間/worker の手順、phase 2 落ち strategy を snapshot に残す、P1' |
| I3 approve 時再検査 | §2.3 表に `approve_candidate` 行 (`require` 解決のみ、pin_mismatch は pending 拒否)、A2 |
| I4 noop gate | §2.7 pin 除去比較、`noop_gate.py` を変更面に、P4 |
| I5 質検査 | §2.7 再ロックのみの再提出を母集団除外、P5 |
| I6 二相予約 | opus の「同じクロージャ」前提は誤りだった (r4 C1)。最終契約は §8 r3-I3 と同じ (予約 → 親で解決 → 未開始時のみ release)、F4 |
| I7 deny 列挙 | `set_eval` 削除、`set_eng_float_format`/`seterrcall` 追加、属性 Store reject、V2 |
| M1 `pin_json` | 削除 (R1 は `pin_object` の順序・型) |
| M2 版表記 | 見出し v0.4b |
| M3 close 二重契約 | property 1 本 (`__enter__` 失敗も None) |
| M4 lock の候補領域 | `--from _human` |
| M5 max_bars | v0.4b で resolver reason `max_bars_too_small` を足したが v0.5 で撤回 (codex r4 I3)、規律文のみ |

## 10. codex r4 対応表

| r4 | 対応 |
|---|---|
| C1 RPC 境界 | §2.9c: 予約 → 親 RPC → `started:false` で `release_backtest` (v0.4 の解放案に戻す、v0.4b の「同じクロージャ」前提を撤回)、F4 |
| C2 noop が再ロックを拒否 | §2.7: `same_modulo_pins` + 同名は `is_relock_transition` 例外、質検査も共有、P4 |
| I1 並行承認 | §2.3 表: strategy + 依存 indicator 名の昇順ロック内で解決 → 切替、P2' |
| I2 phase 2 除外のデータモデル | `InventoryBuildResult(inventory, phase1_metas, rejected_strategies)` |
| I3 max_bars は warmup 下限でない | `max_bars_too_small` 撤回、fixture の期待値は period から算出 |
| I4 trade worker の呼び出し | 変更面に `mission_worker.py` / `market_tools.py` (settings + `inventory.metas`) |
| I5 A1 の観測点 | A1 (gate 経由、candidate + no_strategy) / A1-b (run_in_sample 直接、candidate のみ) に分割 |
| I6 P2 の assert | 新 hash による行分離を検証、`find_matching_approved_metrics` は旧 hash を返すのが仕様、caller 側は P5 |
| I7 属性 Store の網羅 | `ctx` が Store/Del の `ast.Attribute` を構文種別によらず reject、`reset_option` deny、V2 に 7 ケース |
| M1 承認詳細の意味論 | 2 欄 (候補 hash に pin 済 / 同名別 hash に pin)、D1 |

## 11. codex r5 対応表

| r5 | 対応 |
|---|---|
| C1 handshake 膨張 | §2.2 上限 (deps ≤ 8 / outputs ≤ 32 / params ≤ 8 KiB / handshake ≤ 256 KiB、`handshake_too_large`) |
| I1 解決結果が live に渡らない | `InventoryBuildResult.resolved` mapping、service は再解決しない (identity 観測) |
| I2 lock 集合と bless TOCTOU | `sorted(set(...))`、lock 後に候補 hash + 依存集合を再読して不一致は `candidate_changed` |
| I3 reconcile | `switched` + pin 破れ → 旧 target へ戻し `reverted`、approval は pending |
| I4 commit gate の CPU | `StrategyGateVerdict.cpu_samples` 経由 |
| I5 fixture | bar 生成式・端点・本数を具体値で。signal 数は v0.7 (r6 I5) で受入から外し、v0.8 で decision sink + 独立 oracle の全時点 action 検査に置換 |
| M1 `__iter__` 互換 | 撤回、全 caller を移行 |
| M2 reason token | `over_max_bars_limit` に統一 |

## 12. codex r6 対応表

| r6 | 対応 |
|---|---|
| I1 解決結果が payload に届かない | `GateOutcome.resolved`、submit/bless/`_build_approval_payload` はそこから `pin_object()`、resolver 1 回 + 同一 object を受入に |
| I2 子 worker の tool が親 inventory を見られない | mission handshake に `inventory_view` (JSON-safe)、両 tool と lock はそれだけを読む |
| I3 上限の語彙と受入 | `handshake_too_large` を固定語彙に、R1 に 4 上限の境界値 + spawn 0 回 |
| I4 `source="fixture"` 不受理 | test DB へ `dukascopy` を論理 source として投入、A1' の argv を更新 |
| I5 signal 数の自己実測 | example の判定式・閾値を固定、テスト側の独立参照実装で全時点の action を検査 (signal 数は受入から除外) |
| I6 reconcile の受入 | R2 |
| I7 lock 重複 / bless TOCTOU の受入 | P2'' |
| M1 caller 一覧 | `rg` 全結果 + `test_service_app` / `test_e2e_plugin_signal` を明記 |

## 13. codex r7 (terra) 対応表

| r7 | 対応 |
|---|---|
| I1 fixture と market hours | 開始を月曜 00:00Z に、評価回数は本文で固定せず `is_market_open` から導出、warmup 観測を明記 |
| I2 全時点 action の観測面 | `StrategyAdapter(decision_sink=)` (テスト専用注入、本番は None) |
| I3 対応表の矛盾 | §8 r3-I3 / §9 opus-I6 を最終契約に訂正、F4 を唯一の予算契約に |
| M1 `main_outputs` | 削除、kind == indicator のとき `outputs` を handshake に |
| M2 §11 の残骸 | r5-I5 行を最終方式に |

## 14. codex r8 (terra) 対応表

| r8 | 対応 |
|---|---|
| I1 noop の比較対象 | `find_noop_copy` に snapshot / examples / `InventoryBuildResult` を明示注入、同名は `phase1_metas` の S と比較 |
| I2 R2 の `.versions` | 新 version directory は残る (回収は範囲外) に修正 |
| I3 oracle 語彙 | `(action, direction)` + entry_type + SL/TP の数値一致に固定 |
| I4 identity の受入 | P3' (resolver spy 1 回、`is` 一致、service 側も) |
| M1 §8 r3-I4 | property 契約に更新 |
| M2 loader reason | §2.2 に固定語彙を列挙、L1 は完全一致 |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-18 | v1.6 | §2.7(b) の再ロック免除を「質検査を skip」から「**候補と同名 plugin の承認行だけを母集団から除外したうえで検査する**」へ締め付け (自己一致除外)。あわせて §2.7 の承認詳細 (i) 欄の母集団を `inventory.metas` → `phase1_metas` へ訂正 | /code-review high 2 周目 CR2、2026-09-18 ユーザー承認 (締め付け方向の設計変更)。(i) 欄は同 2 周目 CR4 — 候補 hash に pin 済だが現承認 hash と不一致で live でない strategy が (i)(ii) どちらにも出ず、人間の承認判断から隠れていた | - |
| 2026-09-17 | v1.5 | §5 の遮断 8 の行に **sink 一覧 (6 経路)** を追記。各 sink に何を載せてよいかを逐語で列挙し、**改善 RPC 応答だけは alias/reason/available を載せる** (§2.9c、codex r4 C1 で確定済) ことを明示 | 段 0 束 3 の改善提案 1 — 遮断 8 の pin が 1 sink しか見ておらず他 sink の漏れを見逃した事故が 2 件 (M4 / M14)。1 周目 codex 束 2 も「RPC 応答の alias/reason は規律違反」と Critical を上げており (= 却下、§2.9c が正)、**どの sink に何を載せるかが設計書 §5 から読み取れない**ことが両者の共通原因 | - |
| 2026-09-15 | v1.4 | §6 S1 の「配備済 `rsi_wilder` 相当 (スカラー、outputs なし) は無変更で `get_indicators` **と依存の両方**で使える」を「**standalone `get_indicators` でのみ**使える (strategy の依存先にはできない — resolver `outputs_undeclared`、それは U4b の観測点)」へ訂正。あわせて系列末尾射影・NaN キー除去の記述を同じ行に明示 | **§0 U4 (2026-09-14 ユーザー裁定) と §6 U4b が「outputs 宣言なしは依存先にできない」と定めているのに、S1 だけが v1.1 以前の文言 (依存でも使える) のまま残っていた自己矛盾**。全受入 ID を逐語 pin する実装プランは S1 と U4b を同時に満たせない (codex プランレビュー r1 の Important、`tmp/plan-indicator-wiring/codex-plan-r1.md`) | - |
| 2026-09-14 | v1.3 | §6 の F2 (pin 破れ strategy の観測を「producer の plugin 一覧に含まれない」に絞り、session cache 未登録はその帰結として別途観測しない) と P2' (「逆順で取っても deadlock しない」という実測不能な主張を、`_plugin_lock` の取得順序が常に名前昇順・重複なしであることを spy で pin する形に置換) を改訂。指揮者の実装プラン既定選択 8 件のうち①(validator の置き場 = `core/plugin_contract.py`)と⑥(lock 後は snapshot を取り直す)をユーザー裁定で変更、②③④⑤⑦⑧は変更なしで採用 | ユーザー裁定 (2026-09-14、実装プラン `docs/superpowers/plans/2026-09-14-indicator-consumption-wiring.md` 側の指揮者の既定選択 8 件 + 申し送り 2 件の確定) | - |
| 2026-09-13 | v0.1〜v0.4a | 初稿 → codex r1 (C5/I10/M2) / r2 (C3/I12/M1) / r3 (C3/I8/M2)。identity 論点が 3 周連続 Critical → v0.4 で §2.7 を作り直し (pin を config.yaml に書くロック方式、`execution_hash` 撤回)。r4 部分 (`pin_mode` 3 値) | 設計レビュー | - |
| 2026-09-13 | v0.4b〜v0.7 | opus 独立レビュー (C1/I7/M5) → codex r4 (C2/I7/M1) → r5 (C1/I5/M2) → r6 (C0/I7/M1)。RPC 境界、noop の再ロック例外、lock 集合、`InventoryBuildResult`、handshake 上限、reconcile、`GateOutcome.resolved`、`inventory_view` | 設計レビュー | - |
| 2026-09-14 | v0.8〜v0.9 | codex r7 terra (C0/I3/M2: fixture の market hours、decision sink、対応表の矛盾) → r8 terra (C0/I4/M2: noop 入力契約、R2 の `.versions`、oracle 語彙、identity 受入、loader reason 語彙) → **r9 terra: 指摘 0** | 設計レビュー | - |
| 2026-09-14 | v1.0 | spec 化 (`tmp/design-indicator-wiring/design.md` v0.9 を移植、対応表 §8〜§14 は経緯として保持)。ユーザーレビュー待ち | 収束 | - |
| 2026-09-14 | v1.2 | §6 の C1 (plugin error 後の `cpu_sec` は float、`None` は SIGKILL fallback / worker 未起動の 2 経路) と R1 (`handshake_too_large` は定数 monkeypatch で境界試験、正常入力では到達不能である算術を注記) を改訂 | **実装プランの着手前検証 (opus r1 I10) で判明** — v1.1 の文言のままでは実装・テストと恒久的に食い違う | - |
| 2026-09-14 | v1.1 | ユーザー裁定 U4 (`outputs` は新規承認で必須、宣言なし既存は null = 依存不可) / U5 (ロックは全体再シリアライズ) / U6 (prerequisite 3 本は fable ひな形 + ユーザー承認) を §0 / §2.2 / §2.3 / §2.5 / §2.9 / §2.10 / §6 (U4a/U4b) に反映、§7 を解消。実装着手可 | 裁定 | - |
