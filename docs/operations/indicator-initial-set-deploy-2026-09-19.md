# 標準指標の初期セット 9 本 — 配備手順 (2026-09-19)

**暫定手順**。[first-run-setup] (初回起動の対話ウィザード) が一括配備の手順に
置き換える予定。**恒久なのは手順の細部ではなく「採用には人間の明示的確認が要る、
LLM の自動配備経路は作らない」という規律のほう** — この規律は [first-run-setup]
のあとも変わらない。

対象は `docs/examples/plugins/{sma,ema,rsi,macd,bollinger,atr,adx,stochastic,ichimoku}`
の 9 本 (`docs/examples/plugins/{rsi_indicator,rsi_pullback,sma_cross}` は既存で対象外)。
配備先の新コマンド・新機構・LLM の自動配備経路は無い — 既存の `afx plugin bless`
(人間 CLI) をそのまま使う。

## 手順 (0)〜(6)

### (0) 既存の配備名との衝突確認

配備前に、以下のいずれの名前も `plugins/` に無いことを確認する:

```
ls -l plugins/
```

`sma` / `ema` / `rsi` / `macd` / `bollinger` / `atr` / `adx` / `stochastic` /
`ichimoku` のいずれかが既に存在する場合、この bless は**既存名の新版への切り替え**
になる。その名前を pin している strategy がある場合、pin 破れで inventory から
外れる可能性がある (設計書 §8 D3)。事前に依存 strategy の有無を確認すること。

### (1) コピー

9 本のうち配備する 1 本を選び、`docs/examples/plugins/<名前>` を
`plugins/_human/<名前>` へコピーする:

```
cp -r docs/examples/plugins/<名前> plugins/_human/<名前>
```

`__pycache__` や `.pytest_cache` が巻き込まれても問題ない —
`check_candidate_snapshot` (`gate_pytest.py`) がこれらを無視する。

### (2) bless

リポジトリの root で実行する。`afx` はプロジェクトの仮想環境 (`.venv/bin/afx`) に
入っているコマンドで、シェルの PATH には無い — **`uv run` を付ける**:

```
cd ~/project/agentic-fx
uv run afx plugin bless <名前> --from _human
```

以下、本書の `afx plugin ...` はすべて `uv run afx plugin ...` の意味である
(`afx> ...` と書いた箇所は、起動中のサービスの対話シェルに打つコマンド)。

### (3) 結果の確認

- **rc=0** の場合: stdout に `approval id=<N>` が出る (成功)。
- **rc=1 / エラーメッセージ**、または**Python traceback が出て停止する**場合:
  そこで停止し、失敗が (A) ゲート前・ゲート中か、(B) ゲート後かを判定して
  下の「失敗の 2 系統」の該当手順で収束させる。**既に配備済みの分は巻き戻さない**
  (設計書 §6.3) — 9 本は束ではなく、1 本ずつ独立に配備される。

### (4) 後片付け

```
rm -rf plugins/_human/<名前>
```

**bless された結果 (`plugins/<名前>` の symlink や `.versions/` 配下) は消さない。**
`_human/<名前>` を残したままにすると、後日の `afx plugin materialize <名前>` が
`FileExistsError` になる。

### (5) 9 本ぶん繰り返す

(1)〜(4) を `sma` / `ema` / `rsi` / `macd` / `bollinger` / `atr` / `adx` /
`stochastic` / `ichimoku` の 9 本ぶん繰り返す。順序に依存関係は無い。

### (6) 最終確認

service を再起動し、起動ログに 9 本が indicator として載ることを確認する。

> **(6) は人間の手動確認であり、自動テストの範囲外** (2026-09-19、1 周目レビュー)。
> 受入テスト `tests/plugin/test_indicator_initial_set.py` は (1)〜(5) を
> tmp 環境で逐語実行するが、**service の再起動と起動ログの目視は観測しない**。
> (1)〜(5) が通っても (6) を省略してよいという意味ではない。

## 失敗の 2 系統 (設計書 §6.3)

配備は 1 本ずつ独立している。**9 本は束ではない** — 途中で失敗しても、それまでに
配備済みの分は巻き戻らない。失敗の性質は「ゲート前・ゲート中」か「ゲート後」かで
まったく異なる。

### (A) ゲート前・ゲート中の失敗 (`check_source` / pytest / `max_bars` / `outputs_required`)

**何も残らない。** 候補を直して同じ名前でもう一度 bless するだけでよい。

指揮者の実測 (5 本目 `bollinger` の `test_plugin.py` をわざと壊した場合):

```
ValueError: plugin 'bollinger': test_plugin.py failed pytest gate (returncode=1)
```

- 先行 4 本 (`sma` / `ema` / `rsi` / `macd`) は配備済みのまま
- `.versions/bollinger` は作られない
- 未終端の journal は残らない
- pending の approval は 0 件

候補を直し、`plugins/_human/bollinger` を作り直して bless を再実行すればよい。

### (B) ゲート後の失敗 (version 作成・history 記録・symlink 切替)

**journal / pending approval / `.versions/` が残る。** 次に同じ名前で bless すると
`UnresolvedJournalError` になる。

**この例外は `ValueError` ではない** (`Exception` を直接継承 — `switch.py:1572`)。
そのため CLI の `except (ValueError, SandboxError)` (`backtest/cli.py:590`) を
すり抜け、**Python の traceback がそのまま表示される**。意味は「未終端の
switch journal を検出した」ということ。

収束手順 (設計書 §6.3 (B) から逐語で転記):

1. traceback または `エラー:` を見て、**ゲート後の失敗かを判定**する
   (`.versions/<名前>/` が増えている / 同じ名前で bless し直すと
   `UnresolvedJournalError` になる → (B))。
2. **同じ bless をもう一度実行**し、`UnresolvedJournalError` のメッセージから
   `op_id` と `approval_id` を読み取る。この例外のメッセージ自身が
   `op_id=...` と `approval_id=...` を含む (`switch.py:1813-1816` 逐語:
   `plugin '<名前>': an unresolved switch journal (op_id=..., approval_id=...)
   blocks bless — resolve it first (reconcile or approval retry)`)。
   **これが approval id を知る正規の手段** — 対話シェルに pending approval を
   列挙するコマンドは無い (`status` は orders/mission だけ)。
3. サービスの対話シェルで `approval <approval_id>` を見て status を確認し、
   次を実行する:
   ```
   afx> approval retry <approval_id>
   ```
   `preparing` / `versioned` / `recorded` を終端させる唯一の手段はこれ。
   **サービスの再起動だけではこの 3 phase の journal は終端しない**
   (`switch.py:180-183` が再起動時の reconcile でこれらを skip する)。
4. それでも解けない場合 (phase が `switched` で live が第三者に触られている等)
   は**サービスを再起動**し、`logs/activity.log` の `switch_reverted` /
   `switch_reconcile_unrecognized_live_target` / `plugin_reconcile_failed`
   を確認する。`unrecognized` が出ていたら**自動収束しない** — 人間が
   `plugins/<名前>` の symlink の状態を確認して判断する。
5. **同じ bless をもう一度実行して `UnresolvedJournalError` が出ないこと**
   (= 未終端 journal が無いこと) **を確認する。** そのまま成功すれば配備完了。

**`approval retry` の効き方は失敗した phase によって変わる** (指揮者の実測、
2026-09-19):

| 失敗した phase | 症状 | `approval retry` 後 | 手順 5 の再 bless |
|---|---|---|---|
| `preparing` (版作成で失敗) | `.versions/` は増えていない | journal 終端 + **配備完了** | 確認になる (同内容なので切替は no-op) |
| `versioned` / `recorded` (版はできた / history・切替直前) | **`.versions/<名前>/<hash>` が増えている** | journal 終端 + **配備完了** | 同上 |
| `switched` (切替で失敗し live が新 target を指していない) | live が無い / 旧 target のまま | journal 終端・approval は `approved` だが **配備されない** | **必須** (手順 5 を省略しないこと) |

上の表の `switched` 行は `src/` 側の既知の観測事項:
`approve_candidate` の 0d は `phase == "switched"` を `_reverify_switched_journal`
→ `_finalize_decision` で閉じるだけで `switch_live` を呼ばない
(`switch.py:1440-1454`)。そのため「approval は `approved` なのに何も配備されて
いない」状態になり得る。**`[retry-switched-approves-without-deploy]` として
起票済み** (本ドキュメントの対象範囲では `src/` を直さない)。

**`preparing` / `versioned` / `recorded` は再起動では終端しない**
(`switch.py:180-183` が skip する) — これが「`approval retry` が唯一の手段」の
意味。**`switched` は再起動時の reconcile が扱う**ので、この 2 つの記述を
混同しないこと。

いずれの phase でも、**手順 5 (もう一度 bless する) を必ず実行する。**
`preparing` / `versioned` / `recorded` では確認 (no-op) になり、`switched` では
必須の配備手順になる — どちらの場合でも正しく働く。

## strategy 作者向けの注意

これらの indicator に依存する strategy は、自分の `max_bars` を
**400 以上**に宣言すること (設計書 §3.2)。9 本はいずれも `max_bars: 400` で
先頭依存の誤差が公差内であることが保証されているのはこの範囲のみで、
`max_bars` をこれより小さくすると値は計算されるが保証の範囲外になる
(例: `rsi` は `max_bars: 400` で最終値 `65.797529`、`max_bars: 200` では
`65.797530` — 食い違う。エラーにはならないが一致は要求できない)。

## 任意の後片付け

新 `rsi` を配備したあと、**依存する strategy が無いことを確認したうえで**、
既に配備済みの `rsi_indicator` / `rsi_wilder` を退役させてよい。これは
**人間の判断であり、本手順 (=配備) の完了条件ではない**。

**ただし現時点では、symlink 配備された plugin を退役させる CLI 経路が存在しない**
(設計書 v1.3b §1 非スコープ、`[retire-symlink-deployed-plugin]` として起票済み)。

`afx plugin retire <名前>` は名前 1 個だけを引数に取るが、**「legacy plain live」
— つまり `plugins/<名前>` が (symlink ではなく) 普通のディレクトリである場合に
しか使えない**。bless で配備したものは `.versions/` への symlink になっているため、
`retire` は次で拒否する:

```
ValueError: plugins/<名前> is not a plain directory (retire only applies to
legacy plain live)
```

(`switch.py:1627-1629`)。未終端の journal がある場合も `UnresolvedJournalError`
で拒否する (`switch.py:1621-1625`)。

**したがって `rsi_indicator` / `rsi_wilder` が bless / approve で配備されている
場合、退役はできない — 新 `rsi` と併存させること。**

**`retire` は依存 strategy を一切検査しない** (`switch.py:1605-1638` の
`retire_plugin` 全体にその検査コードが無い)。退役させた indicator を pin して
いる strategy は、次の `approved_plugins()` の第 2 相で `not_found` になり
**黙って inventory から外れる** (pin 破れ)。**人間が退役前に必ず確認すること。**
確認手段は対話シェルの `afx> approval <id>` の詳細表示に出る
`dependent_pinned_here` / `dependent_pinned_elsewhere` の 2 欄
(`commands.py:394-400`)。

**example 側の `docs/examples/plugins/rsi_indicator` は残す** — example 戦略
`rsi_pullback` が依存しており、改善ループの `_examples` スナップショットと
`tests/plugin/test_loader.py` / `tests/loops/test_improve_e2e.py` が参照している
(設計書 R7)。
