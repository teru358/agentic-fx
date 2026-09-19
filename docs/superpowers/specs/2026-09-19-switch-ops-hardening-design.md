# [switch-ops-hardening] 設計書 v1.1

束: plugin 承認・切替回廊の**収束の穴と、人間から見える状態の穴**を塞ぐ。
起票済み 3 件 — `[retry-switched-approves-without-deploy]` / `[cli-bless-unresolved-journal]`
(+ 対話シェルに pending 一覧が無い) を対象とする。新しい配備経路も新しい自動化も作らない。

前提束: プラン 10 (`docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` §5.1 / §5.1-1 / §5.3)
が switch journal の状態機械と収束規則を定義済み。**本束はその規則に実装を合わせる**のが主眼であり、
規則そのものを新しくはしない。調査記録は `tmp/design-switch-ops/survey.md`、設計案 v0.1 は
`tmp/design-switch-ops/design.md` (2026-09-19 ユーザー承認済)。

## 0. 位置づけとユーザー裁定 (2026-09-19、設計 v0.1 承認済)

| # | 論点 | 裁定 |
|---|---|---|
| R1 | `switched` journal の retry (件 1) | **案 C**: `approve_candidate` の 0d は live の実際の指す先を見て分類し、`not_switched` なら**巻き戻して停止行を閉じ、同じ呼び出しで手順を頭から流す**。`foreign` は**触らず** activity ERROR + approval は `pending` のまま |
| R2 | 判定の一本化 | 分類 (live ∈ `switched` / `not_switched` / `foreign`) は**1 つの関数**に寄せ、起動時 reconcile と 0d が共用する。**処置は actor で分かれてよい** (reconcile は無人なので pending 留置、retry は人間の明示操作なので配備まで完了させる) |
| R3 | 対話シェルの一覧 (件 3) | `approval list` を新設。pending approval の一覧に加え、**未終端 journal の節を同じ出力の末尾に出す** (0 件なら節ごと非表示) |
| R4 | `force_revert_op_id` | **人間向けの入口は足さない** (非スコープ)。R1 で人間の収束操作は `approval retry` と `reject` に閉じる |
| R5 | reconcile の lock (Q4) | **巻き戻しの枝だけ**を plugin flock の下に入れる。`live == new_target` の枝は `retry_approval` が内部で `_plugin_locks` を取るので**包まない** (flock は非再入 — 包むと自己デッドロック)。**v1.1 (設計レビュー r1 Critical 1) で強化**: lock を取るだけでは足りず、**lock 取得後に journal 行と live を読み直して分類をやり直す**ことまでを要件とする (§3.3) |
| R6 | 退役 (件 4) | `[retire-symlink-deployed-plugin]` は**別束**。本束の非スコープ |
| R7 | 件 4 の既定 (依存 strategy がある場合) | 別束の設計時に再度伺う (現時点の推奨は「拒否 + `--force`」) |
| R8 | CLI / シェルの例外 (件 2) | `_plugin_bless` / `_plugin_materialize` を `_plugin_retire` の作法 (rc=1 + `エラー: ` + 次の 1 手) に揃える。`UnresolvedJournalError` の文言が `op_id` / `approval_id` を含むことは維持する |

---

## 1. スコープと非スコープ

**スコープ**
- `switch.py`: `switched` 行の live 分類器の新設、`reconcile_switch_journals` の載せ替え (挙動不変)、
  `approve_candidate` 0d の R1 化、reconcile の巻き戻し枝の flock 化 (R5)。
- `backtest/cli.py`: `_plugin_bless` / `_plugin_materialize` の例外処理を揃える。
- `switch.py`: `_finalize_decision` の phase ガード (r1 Important 2 の実測から追加採用、§3.2)。
- `commands.py`: `approval list` 新設、`approval retry` の結果報告を実態から読み直す、`_HELP` 更新。
- 既存 pin テスト 1 本の書き換え、runbook (`docs/operations/indicator-initial-set-deploy-2026-09-19.md`) の改訂。

**非スコープ (明示)**
- **`[retire-symlink-deployed-plugin]`** (件 4)。`retire_plugin` は 1 行も変えない。
  実環境の `rsi_indicator` / `rsi_wilder` は引き続き新 `rsi` と併存させる。
- **`force_revert_op_id` の CLI / シェル入口** (R4)。`reconcile_switch_journals` の引数としては残すが、
  人間が直接叩ける場所は作らない。**理由**: phase を無視して無条件に巻き戻す割込用の道具であり、
  人間に握らせると「終端済みの行を後から revert する」誤用を招く。R1 と R3 が入れば
  「id が見えない」「retry が効かない」という本来の困りごとは消える。
- **journal の phase 語彙・DDL の変更**。本束は既存の 6 phase の中で閉じる。
- **切替の順序 (journal-first) の変更**。`_advance_to_decided` が `phase='switched'` を commit してから
  `switch_live` を呼ぶ順序 (`switch.py:1231-1234`) は設計書 §5.1 の要求どおりで**正しい**。変えない。
- **承認の自動化**。approved にする契機は従来どおり人間の `approve` / `bless` / `approval retry` のみ。

---

## 2. 動作目線の設計 (誰が何をどう扱うか)

### 2.1 配備者 (人間、CLI)
`uv run afx plugin bless <名前> --from _human` を打つ。ゲート通過後の I/O 失敗 (版作成・git 記録・symlink 切替)
では、これまでどおり rc=1 で止まる。**変わるのは、同じ名前をもう一度 bless したとき**:
Python の traceback ではなく
`エラー: plugin '<名前>' に未終端の切替ジャーナルが残っています (op_id=3, approval_id=12)。…`
という 1 行 + rc=1 が返る。`materialize` も同じ作法に揃う。

### 2.2 配備者 (人間、対話シェル)
`afx>` で `approval list` と打つと、**pending の承認申請が id 付きで並ぶ**。
未終端の切替ジャーナルがあれば、同じ出力の末尾に `op_id` / `name` / `phase` / `approval_id` が出る。
これで「retry すべき id」を知るために**わざとエラーを起こす必要がなくなる**。

`approval retry <id>` を打つと、**打った結果が何だったかが返る**:
配備まで完了したのか、巻き戻して再実行したのか、第三者に触られていて人間待ちなのか、
候補が壊れていて pending に留まったのか。今までは常に「再試行しました」だった。

### 2.3 サービスの起動時 reconcile
役割は変わらない — 無人で走るので、**配備を前に進めるのは live が既に新 target を指している行だけ**。
それ以外は安全な既知状態へ戻して `pending` のまま人間に委ねる。
変わるのは (a) live の分類を 2.4 と**同じ関数**で行うこと、(b) 巻き戻しのときに plugin flock を取ること。

### 2.4 人間が `approval retry` を打ったときの切替 (本束の中心)
retry は「人間が今ここにいて、この承認を成立させたい」と言っている操作なので、
**停止した行を安全に畳んでから、同じ呼び出しで手順を頭から流し直す**。
畳む先は reconcile と同じ「切替前の既知状態」であり、**途中の半端な状態から先へ進むことはしない**。
第三者が live を触っていた場合だけは、何が正しいか機械には決められないので**何もせずに人間待ちにする**。

---

## 3. 件ごとの設計

### 3.1 `switched` 行の live 分類器 (共通部品)

**入力**: 非終端 journal 行 (`op_id` / `name` / `old_kind` / `old_target` / `new_target` / `switch_required`) と `plugins_root`。
**出力**: `switched` / `not_switched` / `foreign` の 3 値。**副作用なし** (読み取りのみ — これは不変条件 IV-5)。
**可視性**: `switch.py` のモジュール公開関数とし、`commands.py` から import できることを契約に含める
(§3.5 の `approval retry` の結果報告が `foreign` と再失敗を区別するために呼ぶ)。

**比較方法は既存 reconcile を正とする** (`switch.py:184-186` 逐語):

```python
live = plugins_root / row["name"]
live_target = live.readlink().as_posix() if live.is_symlink() else None
```

- **`readlink` の生文字列**で比較する。`resolve()` は**使わない**。
  理由: journal の `new_target` / `old_target` は `.versions/<name>/<hash>` という
  **`plugins_root` 相対の文字列**として書かれ (`switch.py:1234` / `:1535`)、`switch_live` はそれをそのまま
  symlink の中身にする (`_atomic_symlink_swap`、`:113-125`)。`resolve()` を使うと
  (i) **dangling symlink** (版 dir が消えている) で比較が壊れ、(ii) `plugins_root` 自体が symlink 経由の
  パスだと別名に化ける。**0d 側を既存 reconcile の比較に揃える** (逆をしない)。
- 正規化 (`Path.as_posix()` 以外の normalize) は**しない**。書いた側と読む側が同じ規約なので不要であり、
  「見かけ上一致するが実体が違う」を作らないため。

**判定表** (`old_kind` は DDL 上 `absent` / `symlink` の 2 値のみ — `db.py:169` の CHECK 制約。
`plain` は journal 行を作らない = `legacy_plain_present` で pending 留置される `switch.py:1537-1550` ので
この表に現れない):

| `old_kind` | live の状態 | `live_target` | 分類 | 意味 |
|---|---|---|---|---|
| `symlink` | `new_target` を指す symlink (dangling 可) | `== new_target` | **`switched`** | 切替は完了している |
| `symlink` | `old_target` を指す symlink | `== old_target` | **`not_switched`** | 切替前のまま |
| `symlink` | 別の先を指す symlink | 上記以外 | **`foreign`** | 第三者が触った |
| `symlink` | symlink でない (plain dir / 通常ファイル / 不在) | `None` | **`foreign`** | 第三者が触った (fail closed) |
| `absent` | symlink が無い (不在) | `None` | **`not_switched`** | まだ作られていない |
| `absent` | symlink でないものが在る (plain dir / 通常ファイル) | `None` | **`not_switched`** | 下記の注を参照 |
| `absent` | `new_target` を指す symlink | `== new_target` | **`switched`** | 切替は完了している |
| `absent` | `old_target` (通常 `None`) を指す symlink | `== old_target` | **`not_switched`** | disjunct 1 側 |
| `absent` | 別の先を指す symlink | 上記以外 | **`foreign`** | 第三者が触った |

- `old_kind='absent'` の `not_switched` は **2 つの disjunct**を持つ。実コード (`switch.py:224` 逐語) は
  `live_target == old_norm or (row["old_kind"] == "absent" and live_target is None)` であり、
  **disjunct 2 は `old_target` の値に一切条件を付けない**。通常の呼び出し元は `old_target=None` を渡すので
  disjunct 1 だけで足りるが、DB 層は `old_kind` と `old_target` を独立に受けるため
  **`old_target` が非 `None` の `absent` 行でも live 不在なら `not_switched`** になる。
  `test_switch_journal.py:238 test_switched_recovery_absent_old_kind_with_no_live_reverts` は
  まさにこの形 (`old_target="bogus-stale-old-target"` / live 不在 / symlink 無し) を作って
  **disjunct 2** を pin している。**分類器はこの条件を逐語で保つ** (「`old_target is None` かつ」を足すと
  この既存テストが red になる)。
- **`absent` かつ live が symlink でない実体 (plain dir / 通常ファイル)** は `live_target = None` なので
  disjunct 2 により `not_switched`。このとき `_revert_one` は **FS 上 no-op** —
  `old_kind='absent'` の枝は `if live.is_symlink(): live.unlink()` しか行わない (`switch.py:132-134`) ため、
  **人間が置いた plain ディレクトリを消さない**。0d-2c で新規経路へ落ちた後は
  `old_kind` が `plain` と判定され (`switch.py:1529-1531`)、`legacy_plain_present` で pending 留置になる
  (`:1537-1550`)。つまり **fail closed で、既存の legacy plain 回廊 (`retire` → `approval retry`) に合流する**。
- **`switch_required=0` の行はこの分類器の対象外**。`advance_switch_journal` が
  `switch_required=0` の行の `switched` 化を `ValueError` で禁じている (`switch.py:99-102`) ので、
  そもそも `phase='switched'` に到達しない。分類器は**呼び出し側が `phase == 'switched'` かつ
  `switch_required=1` を確認してから呼ぶ**契約にし、それ以外で呼ばれたら `ValueError` (fail closed)。
- **dangling symlink** (live は `new_target` を指すが版 dir が無い) は `switched` に分類される。
  その後の処置 (`_reverify_switched_journal`) が版の欠損を検出して候補から再作成するか、
  できなければ `reverted` で閉じる — **既存の B3 是正の経路がそのまま効く**
  (`test_switch_paths.py:807 test_switched_journal_reverify_fails_closed_when_version_and_candidate_missing`)。

### 3.2 `approve_candidate` 0d の新しい手順 (R1 = 案 C)

**現行** (`switch.py:1437-1456`): `phase == 'switched'` なら `_reverify_switched_journal` → `_finalize_decision`。
**live を一度も読まない**ため、切替が失敗した行を「approved だが未配備」で終端させる (probe 実測、§6 AC-1)。

**新しい手順** (すべて `_plugin_locks(plugins_root, dep_names)` の内側 = `switch.py:1397`):

| 段 | 分類 | 何をするか | journal | approval | FS | commit |
|---|---|---|---|---|---|---|
| 0d-1 | — | 分類器を呼ぶ (`phase=='switched'` かつ `switch_required=1` のとき) | 不変 | 不変 | 不変 | なし |
| 0d-2a | `switched` | 現行どおり `_reverify_switched_journal` → `_finalize_decision` | → `decided` | → `approved` | 不変 (再検証で版を再作成することはある) | `_finalize_decision` の 1 tx |
| 0d-2b | `foreign` | activity に **`switch_retry_unrecognized_live_target`** (`name=<name> op_id=<M>`) を書いて **return** (触らない) | 非終端のまま | `pending` のまま | **不変** | なし |
| 0d-2c | `not_switched` | `_revert_one` で live を旧状態へ戻し停止行を閉じる → **`op_id = None`** → そのまま新規経路へ落ちる | → `reverted` (+ 後段で新しい行) | `pending` のまま (後段で決まる) | live が旧状態へ | `_revert_one` 直後に commit |

**0d-2c の必須事項 (probe で確認、v1.1 で根拠を訂正)**: 0d は `op_id = existing_journal["op_id"]` を
保持したまま下流へ進み (`switch.py:1440`)、新規経路は **`if op_id is None:` のときだけ
`begin_switch_journal` を呼ぶ** (`switch.py:1554-1559`)。したがって 0d-2c では
**`op_id = None` への再設定が必須**である。

**v1.0 の根拠は誤りだった** (r1 Important 2)。v1.0 は「戻さないと `advance_switch_journal` の
単調性チェックで `ValueError`」と書いたが、**実際には例外にならない**。
`tmp/design-switch-ops/probe/test_probe_r1.py::test_P1_advance_on_reverted_row_does_not_raise` の実測:

```
[after revert] phase= reverted
[P1] outcome = no exception
[P1] journal rows = [(1, 'decided')]      ← 終端の監査記録が decided に書き換わった
[P1] approval status = approved
[P1] live is_symlink = True
```

理由 (実コード): `_advance_to_decided` は先頭で現在 phase を読み (`switch.py:1196`)、
`reverted` は `_PHASE_ORDER` の**末尾** (index 5) なので `versioned` / `recorded` / `switched` への
advance はすべて `if current_idx < ...` の guard でスキップされる (`:1200-1217`)。
`switch_live` は guard の外なので**実行され**、最後の `_finalize_decision` は単調性 API を通さず
`journal_store.set_phase(..., phase="decided")` を直接書く (`:1174`)。

**正しい必須理由**: `op_id = None` は、**巻き戻した停止行を `reverted` のまま監査記録として残し、
新しい操作を新しい journal 行に載せる**ために必要。落とすと (a) 「いつ何が巻き戻されたか」の記録が
`decided` に上書きされて消え、(b) journal 行が 1 本に潰れて §3.2 の「試行履歴が読める」性質と
AC-7 の非終端 1 本の数え方が崩れる。**配備自体は成功してしまうので、観測は「行の数と phase」で取る**
(§6 AC-6 をこの形に書き直した)。

**あわせて採用する防御 (`_finalize_decision` の phase ガード)**: 上の実測は
「`_finalize_decision` が終端行 (`reverted`) を `decided` に上書きできる」という、本束の欠陥とは独立の
穴も示している。`journal_store.set_phase` の呼び出し元は全数で 3 箇所
(`switch.py:100` = `advance_switch_journal` (単調性チェック付き) / `:138` = `_revert_one` (`reverted` 固定) /
`:1174` = `_finalize_decision` (`decided` 固定)) であり、**単調性 API を迂回しているのは `:1174` だけ**。
そこで `_finalize_decision` に「行を読み直し、期待 phase でなければ `ValueError` で fail closed」という
ガードを入れる。**期待 phase は `switch_required` で決まる** — 逐語で:

```
期待 = "switched" if row["switch_required"] else "recorded"
```

**`phase in ("switched", "recorded")` と書いてはいけない** — `switch_required=1` の行が `recorded` のまま
(= 切替をしていないのに) 決定できてしまい、IV-3 を破る fail open になる。
`switch_required=0` の行が `recorded` で終わるのは `_advance_to_decided` の `switched` 昇格が
`if switch_required:` の内側にある (`switch.py:1218-1233`) ためで、これが唯一の正規の例外。

- **なぜ `journal_store.set_phase` 側に置かないか**: `set_phase` は `_revert_one` が
  「まだ非終端の行を `reverted` にする」ためにも使う汎用 setter で、ここに終端書込一律禁止を入れると
  store 層に状態機械の知識が漏れ、`_revert_one` の冪等性 (同じ行に 2 度呼んでも安全) も壊しかねない。
  **迂回している 1 箇所に、必要十分な条件だけを置く**方が変異に強い。
- **既存経路への影響**: `_finalize_decision` に到達する正規の経路は `_advance_to_decided` の末尾
  (switch_required=1 なら `switched`、0 なら `recorded`) と 0d-2a (`switched`) の 2 つだけなので、
  ガードは正規の呼び出しを 1 つも塞がない (§6 AC-16 で観測)。

**`_close_own_unfinished_journal_if_any` との一本化**: 同 helper (`switch.py:1468-1489`) は
「preparing/versioned/recorded の自分の未完ジャーナルを閉じる」もので、コメントに
**「switched はここに来ない — 上の 0d 分岐で先に処理・return 済み」**と書いてある。
0d-2c の導入でこの前提が**古くなる**ので、「停止行を `_revert_one` で閉じて `op_id` を `None` に戻す」処理を
**この helper に寄せて 1 本化**し、コメントを新しい事実 (switched の `not_switched` も通る) に書き換える。
`_revert_one` は非 `switched` 行に対して FS 副作用を持たない (`switch.py:130-137`) ので、
1 本化しても既存経路の意味は変わらない。

**巻き戻し自体が失敗したときの終点**: `_revert_one` の `_atomic_symlink_swap` が `OSError` を投げた場合、
例外は `approve_candidate` の外へ抜ける (握りつぶさない)。
そのとき journal 行は**非終端のまま**、approval は `pending` のまま、live は不定 (ただし `_atomic_symlink_swap` は
temp → rename の 1 手なので「live が消えた瞬間」は作らない)。
→ **次回起動の reconcile が同じ行を同じ分類器で再評価する**。fail closed。

#### クラッシュ点の全数表 (0d-2c を通る 1 回の retry)

`S1` = `_revert_one` の FS 操作、`S2` = 停止行 `reverted` の commit、`S3` = 新 journal 行 (`preparing`) の commit、
`S4` = 版作成 + `versioned`、`S5` = git 記録 + `recorded`、`S6` = `switched` commit → `switch_live` →
切替後再照合、`S7` = `_finalize_decision` (approved + decided を 1 tx)。

| 落ちた位置 | 残る状態 | 次の起動時 reconcile が見るもの | 次の `approval retry` が見るもの |
|---|---|---|---|
| S1 の前 | 停止行 `switched` / live 旧 / pending | 分類 `not_switched` → 巻き戻し → `reverted`、pending 留置 | 0d-2c から同じ処理をやり直す (冪等) |
| S1 と S2 の間 (FS は戻ったが phase 未 commit) | 行は `switched` のまま / live 旧 | 同上 (`not_switched`)。`_revert_one` は既に旧状態の live に対して**同じ target を書き直す no-op** | 同上 |
| S2 の後・S3 の前 | 停止行 `reverted` (終端) / live 旧 / pending / **非終端行なし** | 非終端行が無いので何もしない | `get_open_by_name` が `None` → **素の新規 approve 経路**で完遂 |
| S3 の後 (`preparing`) | 新行 `preparing` / live 旧 | `phase != 'switched'` → **skip** (`switch.py:180-183`) | 0d の自分の行 → 頭から再実行 (`create_version_dir` / `record_version` は冪等) |
| S4 の後 (`versioned`) | 新行 `versioned` / 版あり / live 旧 | skip | 同上 (到達済み phase をスキップして続行 `switch.py:1204-1216`) |
| S5 の後 (`recorded`) | 新行 `recorded` / 版 + git / live 旧 | skip | 同上 |
| S6 の `switch_live` 前後 (切替失敗) | 新行 `switched` / live 旧 | 分類 `not_switched` → 巻き戻し → `reverted`、pending 留置 | **再び 0d-2c** (巻き戻し → 新行 → …)。行は retry 1 回につき 1 本増えるが、**非終端行は常に高々 1 本** (部分 UNIQUE index を破らない) |
| S6 の再照合で不一致 | 新行 `switched` / live **新** / pending | 分類 `switched` → `_unresolved_after_switch` を経て `retry_approval` → 0d-2a。**候補から版を再作成できれば完遂** (`switch.py:1303-1345`、`test_switch_paths.py:770` が pin)、候補も無ければ `reverted` (`:807` が pin) | 0d-2a (現行どおり、同上) |
| S7 の tx の中 | tx は atomic (`BEGIN IMMEDIATE` + rollback、`switch.py:1167-1179`) — approved と decided は**同時に成立するか両方成立しないか** | 分類 `switched` → 完遂 | 0d-2a → `_finalize_decision` |
| S7 の後 | `decided` / `approved` / live 新 | 終端なので対象外 | approval が pending でないので早期 return (no-op) |

**不変条件**: どのクラッシュ点から再開しても、**approved になるのは live が `new_target` を指しているときだけ**
(IV-3)。これが本束が直す欠陥そのものである。

#### 二重実行 (排他) — 実コードで確認した事実

| 組み合わせ | 排他するもの | 根拠 |
|---|---|---|
| 起動時 reconcile ↔ 同一プロセスの対話シェル `approval retry` | **時間的に重ならない** | `reconcile_switch_journals` の呼び出し元は `service.py:802` の 1 箇所のみ (grep 全数)。これは `build_app` の中で、scheduler / watchdog スレッドの起動 (`service.py:1389-1390`) とシェル起動 (`:1407-1408`) より**前**に完了する |
| 対話シェル `approval retry` ↔ scheduler スレッドの期限切れ処理 | **plugin flock** | `process_expired_approvals` は行ごとに `plugins/.locks/<name>.lock` を `flock(LOCK_EX)` してから未終端 journal の有無を確認する (`switch.py:1745-1752`)。0d は `_plugin_locks` (`:1397`) で同じパスを取る |
| 対話シェル ↔ scheduler スレッド (DB) | **別コネクション** | シェルは `conn_shell`、scheduler は `conn_core` (`service.py:705` / `:1049-1055`)。`_finalize_decision` は `BEGIN IMMEDIATE` で書込を直列化する |
| サービス起動時 reconcile ↔ **別プロセスの CLI** (`afx plugin bless` 等) | **現状は何も無い ← 本束で塞ぐ (R5)** | サービスは `acquire_instance_lock(root/"data")` を取る (`service.py:681`) が、**CLI はこの lock を取らない** (`backtest/cli.py` / `entry.py` に `instance_lock` の参照なし)。CLI 側の `bless` / `approve` は `_plugin_locks` を取るのに、**reconcile のループは plugin flock を一切取らない** (`switch.py:167-232`) |

→ 実際に危険なのは最後の 1 行だけ。**サービス再起動中に人間が `afx plugin bless` を叩く**と、
reconcile の `_revert_one` と CLI の切替が同じ live symlink を無ロックで奪い合える。R5 で塞ぐ。

### 3.3 R5: reconcile の巻き戻しの lock と **stale row の再検証** (r1 Critical 1 / Important 1)

#### 3.3.1 なぜ「lock を取る」だけでは足りないか (probe で確認済み)

現行 reconcile は `list_non_terminal` で読んだ行と、その時点の live を lock の**外**で読んで分類する。
ここに lock を足しただけだと、**lock 待ちの間に別プロセスの `approval retry` が同じ行を畳んで
配備まで完了していても、reconcile は手元の古い行 (stale row) で巻き戻してしまう**。

`tmp/design-switch-ops/probe/test_probe_r1.py::test_P2_stale_row_revert_after_competitor_completed`
の実測 (2026-09-19):

```
[P2 classify]        live is_symlink=False → 分類 = not_switched
[P2 competitor done] live is_symlink=True target=.versions/sma/2633ad28...
[P2 competitor done] approval=approved   rows=[(1,'reverted'), (2,'decided')]
[P2 stale revert]    live exists=False is_symlink=False        ← 配備が消える
[P2 stale revert]    approval=approved   rows=[(1,'reverted'), (2,'decided')]
```

**approved のまま live が消える = 不変条件 IV-3 の破れ**。本束が直そうとしている欠陥と同じ形を
自分で作ってしまうので、r1 Critical 1 を採用する。

#### 3.3.2 再検証プロトコル (live を書き換える全経路に適用)

live を書き換える操作は、**すべて次の順で行う**:

1. `with _plugin_lock(plugins_root, row["name"]):` を取る。
2. **lock の内側で `journal_store.get(conn, op_id)` により行を取り直す。**
   行が消えている / `phase` が `decided` / `reverted` (終端) / 読み直した `phase` が
   lock 前と違う → **何もせず次の行へ**
   (比較の基準となる「lock 前の `phase`」は `list_non_terminal` が返した行の値。
   `force_revert_op_id` 分岐もこの手順 2 だけは同じ規律で行う) (activity に `switch_reconcile_skipped_stale_row`
   を `name=<name> op_id=<M> phase_before=<p1> phase_now=<p2>` の形で 1 行。
   **この event 名は新規** — `grep -rn "skipped_stale" src/ tests/` は空で、既存の activity 語彙と衝突しない)。
3. **lock の内側で live を読み直し、分類をやり直す** (§3.1 の分類器)。
   lock 前の分類と違う → 同じく**何もせず次の行へ** (同じ activity 文言、`class_before` / `class_now` を添える)。
4. 2 と 3 が両方とも一致したときだけ `_revert_one` (または該当の FS/DB 更新) を実行する。

**この「読み直して一致を確認する」までが R5 の要件**であり、lock は前提条件にすぎない。

#### 3.3.3 適用箇所 — reconcile 内で live を書き換えるのは **3 箇所** (全数確認)

`grep -n "_revert_one(" src/agentic_fx/plugin/switch.py` の結果 (reconcile 内):

| # | 箇所 | 現行行 | 分類 | 扱い |
|---|---|---|---|---|
| 1 | `force_revert_op_id` 分岐の `_revert_one` | `switch.py:174` | (分類を経ない割込) | **lock + 行の再取得を適用** (r1 Important 1)。**分類の一致確認は求めない** — force revert は「phase に依らず巻き戻す」割込操作という既存の意味論 (`:170-173` のコメント、表 2) を保つため。行が既に終端なら skip |
| 2 | `switched` かつ pin 破れ → `_revert_one` | `switch.py:203-207` | `switched` | lock + 行の再取得 + **分類の再確認** |
| 3 | `not_switched` → `_revert_one` | `switch.py:225-226` | `not_switched` | lock + 行の再取得 + **分類の再確認** |

v1.0 は 1 を数え落としていた (「2 箇所」)。`force_revert_op_id` は R4 により人間向け入口を作らないだけで
**引数としては残り、既存テスト `tests/plugin/test_switch_journal.py:321-350` が switched live の除去・復元を
通している**ので、無 lock のまま残すと IV-6 が偽になる。公開シグネチャと既存テストは変えない。

#### 3.3.4 包まない枝とその理由

- **`switched` かつ pin 健全 → `retry_approval`** (`:216-220`): `retry_approval` → `approve_candidate` は
  **`_plugin_locks` を自分で取る**。外側で同じ lock を持っていると**自己デッドロックする** —
  `flock(2)` のロックは open file description に紐づき、同じパスを 2 回 `open()` すれば別 ofd になるため
  自プロセスのロックで待たされる (`_plugin_locks` docstring の probe 実測 `switch.py:820-846`、
  `test_plugin_lock_is_not_reentrant_within_one_process` が pin)。
  `_unresolved_after_switch` の docstring (`:262-269`) も同じ理由を明記している。**この規律を守る。**
  **stale row 問題は生じない** — `approve_candidate` が lock を取った後に自分で
  `get_open_by_name` と live を読み直すため (§3.3.5)。reconcile は「この行を retry に委譲する」という
  判断だけを lock の外で行い、**FS/DB は 1 バイトも触らない**。
- **`foreign`** (`:227-232`): FS を触らないので lock 不要。activity を書くだけ。
- **`phase != 'switched'`** (`:180-183`): skip。FS 効果が無い。

#### 3.3.5 0d (人間の retry) 側は既に同じ規律か — 実コードで確認した

| 読むもの | 場所 | lock との関係 |
|---|---|---|
| `dep_names` の材料 (候補 `config.yaml`) | `switch.py:1386-1395` | **lock の外** (lock 集合を決めるために先に読む必要がある)。この値は lock 対象の決定にのみ使い、FS 更新の判断には使わない。`bless_candidate` は同じ問題に対し lock 取得後の再読比較 (`candidate_changed`、`:1802-1806`) を持つ |
| approval 行 (`status` / `payload`) | `:1398-1402` | **lock の内側で読み直している** (lock 前にも読むが、内側の読み直しを正として使う) |
| 未完 journal (`get_open_by_name`) | `:1437` | **lock の内側** |
| live の形 (`old_kind` / `old_target`) | `:1527-1533` | **lock の内側** |

→ **0d は既に「lock の内側で読んだ値だけで FS を触る」規律を満たしている。** 本束で追加する分類器の呼び出しも
0d-1 として lock の内側に置く (§3.2 の表)。**0d 側の是正は不要**で、直すのは reconcile 側のみ。

#### 3.3.6 lock の取り方と順序

巻き戻しは **その name 1 本だけ**を取る (`_plugin_lock`、依存 indicator は含めない)。
理由: 巻き戻しが触るのは `plugins/<name>` の 1 エントリだけで、依存名の inventory を読まない。
`_plugin_locks` の `sorted(set(...))` 規約は**複数名を取る場合の順序固定**のためのもので、1 名なら順序問題は起きない。
`_unresolved_after_switch` が内部で依存名込みの `_plugin_locks` を取るが、
それは**この巻き戻しより前に閉じている** (with を抜けてから返る) ので入れ子にならない。

**自己デッドロックしないことの根拠 (全数)**: 新しく lock を取る 3 箇所の内側から呼ばれるのは
`journal_store.get` / `_revert_one` / `journal_store.set_phase` / `activity.write` と §3.1 の分類器だけで、
**いずれも `_plugin_lock` を取らない** (`switch.py:128-142`、分類器は読み取りのみ)。
`retry_approval` / `approve_candidate` / `_unresolved_after_switch` はどれも lock の**外**に置く。

### 3.4 件 2: CLI の例外の扱い

`_plugin_retire` (`cli.py:611-620`) の形を正とする:
`except (plugin_switch.UnresolvedJournalError, ValueError, OSError) as e: print(f"エラー: {e}", file=sys.stderr); return 1`

| 関数 | 現行 | 変更後 |
|---|---|---|
| `_plugin_bless` (`cli.py:589`) | `except (ValueError, SandboxError)` | **`UnresolvedJournalError` を追加** |
| `_plugin_materialize` (`cli.py:600`) | `except (FileExistsError, FileNotFoundError, OSError)` | **`ValueError` を追加** (`materialize_plugin` は live symlink が `plugins_root` の外を指すとき `ValueError` を投げる `switch.py:1591-1597`。現状は外側の包括 catch `cli.py:766-770` に落ち、メッセージ形式だけが不揃い) |
| `_plugin_submit` / `_plugin_lock` / `_plugin_retire` | — | **変更なし** (全数確認済、同型の穴なし。`submit_candidate` は journal を見ないので `UnresolvedJournalError` を投げない) |

**文言** (bless):
```
エラー: plugin 'sma' に未終端の切替ジャーナルが残っています (op_id=3, approval_id=12)。
  先に収束させてください: サービスの対話シェルで `approval list` → `approval retry 12`
```
`UnresolvedJournalError` の元メッセージが `op_id=` と `approval_id=` を含むこと (`switch.py:1812-1816`) は
**維持する** — runbook と既存テストがこの 2 語に依存している。

### 3.5 件 3: 対話シェル

**`approval list`** — **dispatch の条件** (r1 Important 3 で訂正。v1.0 の
`args == ["list"]` では `approval list 5` が**どの分岐にも当たらず** `_HELP` に落ちるので到達不能だった):

```
cmd == "approval" and args and args[0] == "list" and len(args) <= 2
```

既存の `approval <id>` は `len(args) == 1 and args[0].isdigit()` (`commands.py:154`) なので**衝突しない**
(`list` は `isdigit()` が False。`approval 999` の既存テスト `tests/test_commands.py:780` も不変)。
`approval retry <id>` は `len(args) == 2 and args[0] == "retry"` (`:139`) で、こちらとも衝突しない。

**引数 `<n>` の扱い**:

| 入力 | 挙動 | 根拠 |
|---|---|---|
| `approval list` | 既定 **20** 件 | `log` / `activity` の既定 20 に揃える (`commands.py:70-76`) |
| `approval list 5` | 5 件 | 同上の `[n]` 作法 |
| `approval list 999` | **上限 200 で丸める**。丸めたときは末尾に `(上限 200 件で打ち切り)` を出す | 端末に流し込む量の上限。丸めたことを黙らない |
| `approval list 0` / 負数 | `usage: approval list [n]` を返す (一覧を出さない) | 0 件表示は「承認待ちが無い」と区別がつかないので拒否する方が fail closed |
| `approval list abc` | 同上 `usage:` | `int()` の `ValueError` を `dispatch` の包括 catch に落とさず、その場で使い方を返す |
| `approval list 5 6` (引数過多) | `len(args) <= 2` を外れるので `_HELP` | 既存の未知コマンドと同じ扱い |

**表示の契約**:

| 項目 | 決定 | 根拠 |
|---|---|---|
| 並び順 | `id` 昇順 (= 申請順) | 古い順に片付けるのが運用の自然な順序。`approvals.pending` の既定に合わせる |
| 表示項目 | `id` / `kind` / `name` / `created_at` / (plugin なら) `content_hash` 先頭 8 桁 | **1 行 1 件で id を見つけるための道具**。成績や判断材料は `approval <id>` (詳細) の役目で、二重には持たない |
| 0 件 | `承認待ちはありません` | `activity` の `(なし)` と同じ作法 |
| 未終端 journal の節 | 出力の末尾に `-- 未終端の切替ジャーナル --` として `op_id` / `name` / `phase` / `approval_id`。**0 件なら節ごと出さない** | R3。材料は `journal_store.list_non_terminal(conn)` (reconcile が使っているものと同一) |
| `plugins_root` 未配線 | journal の節だけ省略し、approval の一覧は出す | 既存の fail-soft 作法 (`_dependent_strategies` `commands.py:404-418`) |

**遮断 8 との関係**: `approval list` は **holdout / in_sample の数値を一切出さない** (上表)。
そもそも遮断 8 は「改善 agent のプロンプトに holdout 情報を渡さない」規律であり、
人間向け表示は対象外 (`approval <id>` は既に holdout を人間に出している `commands.py:363-364`、
`cli.py:584` の `floor_rule_text(g, audience="human")` のコメント「人間は遮断 8 の対象外」と同じ区分)。
**この文字列が改善 agent に届く経路が無いことの確認 (全数)**: `Commands.dispatch` の戻り値は
`shell.run_shell` が端末へ書くだけで、`approval_requests.reason` のように
`backlog.last_result` → 改善プロンプトへ流れる経路を持たない
(`[[human-reject-reason-leaks-to-improve-prompt]]` の漏洩経路は `reason` 列であり、シェルの出力文字列ではない)。
**新しい文字列を `reason` 列や backlog に書かないこと**を本束の遮断条件とする (§5)。

**`approval retry <id>` の結果報告** (`commands.py:139-153`):
現行は `retry_approval` の後に**無条件で**「approval #N を再試行しました」と返す。
`approve` ハンドラは同じ欠陥を検収 m5 で是正済みで、`status` を読み直してから報告する (`:102-110`)。
**同じ作法に揃える**。文言は **`approval #N を再試行しました: ` を共通の接頭辞**にし、
その後ろに到達状態を続ける (接頭辞を固定する理由は §7.1 — 既存テストの
`assert "再試行" in out` を壊さずに済み、かつ「何を打ったか」が先頭に来るので読みやすい):

| 到達状態 (retry 後に読み直す) | 接頭辞に続く文言 |
|---|---|
| `approved` かつ live が `new_target` | `配備まで完了しました (plugins/<name> → <new_target>)` |
| `approved` だが live が違う | **起きない** (不変条件 IV-3)。起きたら契約違反なので `⚠ approved ですが live が一致しません — activity を確認してください` |
| `pending` かつ journal が非終端 (`foreign`) | `live が第三者に触られているため自動収束しません (op_id=M)。plugins/<name> の状態を確認してください` |
| ↑ の**判定方法** | `retry_approval` の後に `journal_store.get_open_by_name(conn, name)` を引き、`phase == 'switched'` なら §3.1 の分類器をその行に掛ける。`foreign` ならこの文言、それ以外は下の行 (両者とも `pending` + 非終端 journal なので、**status だけでは区別できない**) |
| `pending` (その他: 候補欠損 / hash 不一致 / 再失敗) | `approved になりませんでした (status=pending, reason=<reason or '-'>)` |
| approval 行が読めない | `状態を確認できませんでした (approval 行が見つかりません)` |

---

## 4. 状態遷移と不変条件

phase は従来どおり `preparing → versioned → recorded → switched → decided` と、いつでも `reverted`。
**新しい phase は追加しない。**

| ID | 不変条件 |
|---|---|
| IV-1 | 非終端 (`decided`/`reverted` 以外) の journal 行は name ごとに高々 1 本 (部分 UNIQUE index、`db.py`)。0d-2c が行を増やしても、巻き戻し → 新規の順なので同時に 2 本にならない |
| IV-2 | phase の書込は次の FS 効果より**先** (journal-first)。本束は順序を変えない |
| IV-3 | **approval が `approved` になるのは、その approval に対応する live が `new_target` を指しているときだけ** ← 本束が回復する不変条件 |
| IV-4 | 人間の明示操作 (`approve` / `bless` / `approval retry`) が無ければ、`pending` の approval が `approved` になることはない。reconcile が完遂させるのは **live が既に新 target を指している行**に限る (= 人間の承認操作が切替まで到達済みの行) |
| IV-5 | 分類器は読み取り専用。分類と処置は分離する (分類器が FS を書き換えない) |
| IV-6 | **live symlink を書き換える全経路は、`plugins/.locks/<name>.lock` の `flock` の内側で読み直した journal 行と live に基づいてのみ操作する** (lock を取るだけでは不十分 — r1 Critical 1 の stale row。§3.3.2 の再検証プロトコル)。**唯一の例外は `force_revert_op_id` 分岐で、行の再取得のみを行い live の分類は見ない** — 「phase に依らず巻き戻す割込」という既存の意味論を保つため (§3.3.3)。本束で reconcile の 3 箇所が揃い、下表の全経路で成立する |

#### live を書き換える全経路 (`_revert_one` / `switch_live` / `_atomic_symlink_swap` の呼び出し元を grep で全数)

| 経路 | 場所 | lock | lock 内で読み直すか | 本束で直すか |
|---|---|---|---|---|
| `_advance_to_decided` → `switch_live` (approve / bless の本線) | `switch.py:1219` | `_plugin_locks` (`:1397` / `:1802`) | **はい** (live の形は `:1527-1533` で lock 内、§3.3.5) | 直さない (現状で正しい) |
| `_reverify_switched_journal` → `_revert_one` (再検証失敗) | `switch.py:1352` | 0d の `_plugin_locks` の内側 | はい (0d が lock 内で読んだ行をそのまま使う) | 直さない |
| `approve_candidate` の自分の未完 journal を閉じる `_revert_one` | `switch.py:1491` | `_plugin_locks` の内側 | はい (`journal_store.get` を lock 内で呼ぶ `:1489`) | 直さない (0d-2c の 1 本化先、§3.2) |
| `reject_candidate` → `_revert_one` | `switch.py:1699` | `_plugin_lock` (`:1694`) | はい (`get_open_by_name` を lock 内 `:1697`) | 直さない |
| **reconcile: `force_revert_op_id` 分岐** | `switch.py:174` | **無し** | — | **直す** (lock + 行の再取得。分類の一致は求めない — §3.3.3) |
| **reconcile: pin 破れの `_revert_one`** | `switch.py:203-207` | **無し** | — | **直す** (lock + 行 + 分類) |
| **reconcile: `not_switched` の `_revert_one`** | `switch.py:225-226` | **無し** | — | **直す** (lock + 行 + 分類) |
| `process_expired_approvals` | `switch.py:1745-1752` | 生 `flock` (同じパス) | はい (lock 内で `get_open_by_name` と status を再確認 `:1750-1759`) | 直さない (現状の事実として記録) |
| `retire_plugin` (live を `_retired/` へ rename) | `switch.py:1613-1636` | 生 `flock` (同じパス、`_plugin_lock` の名前検証は通らない) | はい (lock 内で `get_open_by_name` と live の形を確認) | 直さない (件 4 = 別束の範囲) |
| `materialize_plugin` (live を**読む**だけ、書き換えない) | `switch.py:1576-1603` | 無し | — | 直さない (書き換えないので IV-6 の対象外) |

---

## 5. 遮断と安全

- **遮断 8 (改善 agent に holdout を渡さない)**: 本束が触るのは CLI / 対話シェル / 起動時 reconcile の
  **人間向け経路のみ**。改善 agent のプロンプト生成 (`improve_loop`) には 1 行も触れない。
  新しい文字列を `approval_requests.reason` / `improvement_backlog.last_result` に**書かない**
  (書けば `last_result` 経由で改善プロンプトに漏れる — 既知の事故 `[[human-reject-reason-leaks-to-improve-prompt]]`)。
- **承認規律**: IV-4。本束は承認を自動化しない。`approval retry` が配備まで進めるのは
  **人間がその id を指定して打った場合だけ**で、無人の reconcile は従来どおり `pending` 留置に留まる。
- **fail closed**: `foreign` は両経路とも**何も触らない**。巻き戻し失敗は例外で上へ抜け、
  approval は `pending`、journal は非終端で残る (次回 reconcile が再評価)。
- **資金保護との関係**: plugin の配備状態は発注経路に直接は関わらないが、
  「approved なのに未配備」は運用者の認識と現物のずれなので、**表示と現物を一致させる**ことが本束の安全上の主張。
- **新しい外向きリクエスト・新しい sink・新しい入力経路は増えない**。

---

## 6. 受入条件 (ID 付き、観測可能)

`AC-*` は新規テスト。既存テストとの対応は §7。

| ID | 観測 |
|---|---|
| **AC-1** | `switch_live` を 1 回だけ `OSError` にして bless → journal `switched` / live 不在 / approval `pending` を作り、`retry_approval` を 1 回呼ぶと: **`plugins/<name>` が `.versions/<name>/<artifact_hash>` を指す symlink になり**、approval が `approved`、journal は **`reverted` の行 1 本と `decided` の行 1 本の計 2 本** (probe の逆転。現行は approved + live 不在) |
| **AC-2** | AC-1 と同じ状態に `reconcile_switch_journals` を掛けると、journal `reverted` / approval `pending` / live 旧状態 (**現行と同じ = 不変であることの pin**) |
| **AC-3** | live が `new_target` でも `old_target` でもない先を指す (`foreign`) 行に対し、(a) `retry_approval` (b) `reconcile_switch_journals` のどちらを掛けても **live が 1 バイトも変わらず**、approval は `pending`、journal は非終端のまま。activity は (a) が `switch_retry_unrecognized_live_target`、(b) が既存の `switch_reconcile_unrecognized_live_target` (**actor が判別できるよう別イベント名にする** — 同名にすると「再起動で出たのか人間の retry で出たのか」が log から読めない) |
| **AC-4** | 変異: 0d の分類を落として「`switched` なら常に完遂」に戻すと **AC-1 が red** (= 2026-08-16 設計書 §5 の変異「`switched` の復旧を『常に完遂』にする」の killer が 0d 側にも立つ) |
| **AC-5** | 変異: 分類器の比較を `readlink` から `resolve()` に変えると、版 dir を消した dangling 状態のケースが red |
| **AC-6** | AC-1 の状態で、**journal 行が 2 本**になり、巻き戻された行は `reverted` の**まま**、`decided` になるのは**別の新しい `op_id`** の行であること。変異: 0d-2c の `op_id = None` を落とすと (例外にはならず配備も成功するが) **行が 1 本に潰れ、元の行が `reverted` → `decided` に上書きされる**ので red (probe `test_P1_advance_on_reverted_row_does_not_raise` の実測が根拠) |
| **AC-7** | `switch_live` を**恒久的に**失敗させた状態で `approval retry` を 2 回打つと、毎回「`reverted` 1 本 + 新しい `switched` 1 本」になり、**非終端行は常に 1 本** (IV-1)、approval は `pending` のまま |
| **AC-8** | AC-1 の成功後にもう一度 `approval retry` を打つと **no-op** (approval は `approved` のまま、journal 行は増えない、live 不変) |
| **AC-9a** | reconcile の巻き戻し 3 箇所 (`force_revert` / pin 破れ / `not_switched`) が `plugins/.locks/<name>.lock` を保持していることの pin (`_plugin_lock` の spy、`test_reconcile.py:443` の流儀)。かつ `live == new_target` → `retry_approval` の枝では**外側の lock を取っていない**こと (取れば自己デッドロック — 既存 pin `test_plugin_lock_is_not_reentrant_within_one_process` と整合) |
| **AC-9b** | **stale row 競合 (r1 Critical 1 の killer)**: barrier で順序を固定する — (1) reconcile が行 M を `not_switched` と分類する、(2) reconcile が **`_plugin_lock` を呼ぶ直前**で止め、その間に競合者 (別スレッド + 別コネクション) が**同じ lock を取って** M を `reverted` で閉じ、新しい行 N で配備を完遂し approval を `approved` にし、**lock を解放する**、(3) reconcile を再開して lock を取らせる。**seam は `_plugin_lock` の呼び出し** (monkeypatch する) — 取得後に barrier を置くと競合者が同じ lock を取れず、狙った窓を試験できない。**観測**: live は `new_target` を指したまま、approval は `approved`、journal は M=`reverted` / N=`decided`、activity に `switch_reconcile_skipped_stale_row` が 1 行。**変異**: §3.3.2 の再取得 (手順 2) または分類の再確認 (手順 3) を落とすと、live symlink が消えて red (probe `test_P2_stale_row_revert_after_competitor_completed` がこの故障形を実測済み) |
| **AC-9c** | `force_revert_op_id` を指定した reconcile が lock を取り、行を再取得してから巻き戻すこと。既存 `tests/plugin/test_switch_journal.py:321-350` (switched live の除去・復元) が**書き換えなしで緑のまま**であること |
| **AC-10** | 未終端 journal が残った状態で `uv run afx plugin bless <name> --from _human` → **rc=1**、stderr に `op_id=` と `approval_id=` と次の 1 手、**traceback が出ない** |
| **AC-11** | `uv run afx plugin materialize <name>` が containment 違反で失敗するとき rc=1 と `エラー: ` 形式 (`_plugin_materialize` の中で処理される) |
| **AC-12a** | `afx> approval list` が pending を id 昇順で列挙し、0 件なら `承認待ちはありません`。未終端 journal があれば末尾の節に `op_id` / `name` / `phase` / `approval_id`、無ければ節ごと出ない |
| **AC-12b** | `approval list 5` が 5 件に制限される。`approval list` の既定が 20 件。**`approval list 999` は 200 件で打ち切られ、打ち切りの断り書きが出る** (上限が実装されていないと red) |
| **AC-12c** | `approval list 0` / `approval list -1` / `approval list abc` が `usage: approval list [n]` を返し、**一覧を出さず例外も出さない**。`approval 999` (既存の詳細表示) と `approval retry <id>` が**この追加で壊れない** |
| **AC-13** | `approval list` の出力に holdout / in_sample の数値が**含まれない** |
| **AC-14** | `approval retry` の戻り文言 (§3.5 の表の全行) をそれぞれの到達状態で観測する。特に **`foreign` のときに「配備まで完了しました」と言わない**こと、および `foreign` と再失敗が**別の文言**になること (= 分類器で区別できていること) |
| **AC-15** | 既存の `tests/plugin/test_switch_journal.py` / `test_reconcile.py` / `test_switch_paths.py` / `test_materialize_retire.py` / `test_commands.py` / `tests/fixtures/test_wiring_envs.py` / `tests/test_service_app.py` が**1 本も書き換えずに緑**。`tests/plugin/test_indicator_initial_set.py` は §7.1 の **2 本だけ**が書き換え対象で、**同ファイルの他のテストは不変** |
| **AC-16** | `_finalize_decision` の phase ガード: `reverted` / `decided` の行に対して呼ぶと `ValueError` で fail closed になり、journal 行の phase が**変わらない**。正規の 2 経路 (`_advance_to_decided` 末尾 / 0d-2a) は 1 つも塞がれない (既存テストが全部緑であることで観測) |

---

## 7. 変更ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/agentic_fx/plugin/switch.py` | 分類器の新設 (公開関数) / `reconcile_switch_journals` の載せ替え + **巻き戻し 3 箇所の flock 化と stale row 再検証** (§3.3) / `approve_candidate` 0d の R1 化 + `op_id = None` / `_close_own_unfinished_journal_if_any` の 1 本化とコメント更新 / **`_finalize_decision` の phase ガード** (§3.2) / 新 activity イベント `switch_retry_unrecognized_live_target` と `switch_reconcile_skipped_stale_row` |
| `src/agentic_fx/backtest/cli.py` | `_plugin_bless` に `UnresolvedJournalError`、`_plugin_materialize` に `ValueError` |
| `src/agentic_fx/commands.py` | `approval list` / `approval retry` の結果報告 / `_HELP` |
| `tests/plugin/test_switch_ops_hardening.py` (新規) | AC-1〜AC-9c、AC-16。**AC-9b は barrier 付きの競合テスト**なので、既存の multi-process 流儀 (`tests/plugin/_flock_worker.py`) かスレッド + 別コネクションで書く |
| `tests/backtest/test_cli.py` または新規 | AC-10 / AC-11 |
| `tests/test_commands.py` | AC-12〜AC-14 を**追記** (既存は書き換えない) |
| `docs/operations/indicator-initial-set-deploy-2026-09-19.md` | 改訂 (下記) |
| `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` | §5.1-1 に「retry の `not_switched` 処置」を 1 段落追記 (既存規則の具体化。規則自体は変えない) |

### 7.1 既存テストへの影響 (全数、v1.1 で作り直し)

**洗い出しの方法** (推測しない): `grep -rn "再試行\|approval #\|UnresolvedJournalError\|switched" tests/ --include=*.py`
に加え、`dispatch("approval` / `retry_approval` / `reconcile_switch_journals` / `_revert_one` の呼び出しを全数。
ヒットした 1 本ずつを開いて**完全一致 (`==`) か部分一致 (`in`) か**を確認した。

**書き換える (2 本)**

| テスト | 現状が pin しているもの | 扱い |
|---|---|---|
| `tests/plugin/test_indicator_initial_set.py::test_runbook_post_gate_failure_converges_via_approval_retry` (`:854-855`) | `cmds.dispatch(f"approval retry {id}") == f"approval #{id} を再試行しました"` — **完全一致** | **書き換え**。§3.5 の新文言 (接頭辞 + `: 配備まで完了しました (...)`) に更新。**docstring の phase 別表 (`:794-806`) も更新対象** — `switched` 行の「journal 終端・approval `approved` だが**配備されない**」「再 bless **必須**」は本束で偽になる。`[retry-switched-approves-without-deploy]` を「指揮者へ申告済みの観測」と書いた段落も削除する。本体の `versioned` 経路のアサーション (`:857-876`) は**不変** |
| `tests/plugin/test_indicator_initial_set.py::test_runbook_cli_bless_after_post_gate_failure_raises_traceback` (`:996-1033`) | CLI から `UnresolvedJournalError` が素通りして traceback になること (テスト自身の docstring が「直ったら書き換えること」と明記 `:1006-1008`) | **書き換え**。`pytest.raises` → `rc == 1` + stderr の `op_id=` / `approval_id=` |

**不変 (1 本ずつ確認した根拠付き)**

| テスト | 何を見ているか | 不変である根拠 |
|---|---|---|
| `tests/test_commands.py:541` (`approval_retry_dispatches_to_switch_retry_approval`) | `calls == [(42, "shell")]` / **`assert "再試行" in out` (部分一致)** / activity `retry` | 新文言の接頭辞 `approval #N を再試行しました: ` に「再試行」を含む。id 42 は DB に無いので §3.5 の「approval 行が読めない」に落ちるが、それでも部分一致は成立 |
| `tests/test_commands.py:569` (未配線) | `"未配線" in out` | 未配線の早期 return は変えない |
| `tests/test_commands.py:125-155` (`approve_plugin_kind_reports_actual_outcome`) | `out != f"approval #{id} approved"` / `"pending" in out` | **`approve` ハンドラは変更しない** (揃える先であって、揃えられる側ではない) |
| `tests/test_commands.py:599 / :615 / :637 / :653 / :681 / :698 / :714 / :732 / :750 / :772 / :780` (`approval <id>` の詳細表示 11 本) | `approval #<id>` の詳細出力 | `approval list` の dispatch 条件は `args[0] == "list"` で、`isdigit()` 分岐と衝突しない (§3.5)。`approval 999` (`:780`) も不変 |
| `tests/loops/test_floor_leak_guard.py:135` | `approval <id>` の詳細に holdout が出る / 漏れない | 同上。`approval list` は数値を出さない (AC-13) |
| `tests/plugin/test_switch_paths.py:367-397` (`approve_raises_unresolved_journal_error_for_different_approval_id`) | `pytest.raises(switch.UnresolvedJournalError)` | **例外型も送出条件も変えない** (変えるのは CLI の捕捉側だけ) |
| `tests/plugin/test_switch_paths.py:1195` (`bless_raises_unresolved_journal_error_when_open_journal_exists`) | 同上 | 同上 |
| `tests/plugin/test_materialize_retire.py:70` | `pytest.raises(switch.UnresolvedJournalError)` (retire) | `retire_plugin` は本束では 1 行も変えない |
| `tests/plugin/test_materialize_retire.py:236` | legacy plain → retire → `approval retry` の E2E | retire 後は journal 無し = 素の新規経路 |
| `tests/plugin/test_switch_paths.py:770 / :807 / :1023 / :1077` (0d の `switched` 系 4 本) | 再検証・候補からの再作成・tamper 検出 | ヘルパ `_simulate_crash_after_switch_before_decide` (`:735-767`) は **`switch_live` 完了後**に止めるので live は `new_target` = 分類 `switched` = 0d-2a (現行と同じ経路) |
| `tests/plugin/test_switch_paths.py:635` (`switch_live_runs_after_switched_phase_is_committed`) | journal-first の write-ahead 不変条件 | 順序は変えない (§1 非スコープ) |
| `tests/plugin/test_switch_paths.py:677` / `:1119` / `:1160` | `recorded` からの retry / `_close_own_unfinished_journal_if_any` の 2 経路 | `phase != 'switched'`。1 本化しても非 switched 行の扱いは同じ |
| `tests/plugin/test_switch_paths.py:1583 / :1604 / :1635` | `_plugin_locks` の sorted / unique / 依存集合 | 巻き戻しは 1 名のみ `_plugin_lock` を取るので順序規約に触れない |
| `tests/plugin/test_switch_journal.py:238` | reconcile の `absent` disjunct 2 | 分類器がこの条件を逐語で保つ (§3.1) |
| `tests/plugin/test_switch_journal.py:321-350` | `force_revert_op_id` の switched live 除去・復元 | 公開シグネチャと意味論を変えない。lock と行の再取得を足すだけ (AC-9c が明示的に守る) |
| `tests/plugin/test_reconcile.py:382 / :423` | reconcile の `switched` 2 枝 (pin 破れ → revert / 健全 → decided) | 載せ替えは挙動不変。lock と再検証を足しても、競合が無い単一プロセスのテストでは再取得結果が同じ |
| `tests/plugin/test_reconcile.py:443` | `_unresolved_after_switch` が依存 lock を握ること | AC-9a と**衝突しない** (こちらは `switched` 枝の内側、AC-9a は巻き戻し) |
| `tests/fixtures/wiring_envs.py:327` / `tests/fixtures/test_wiring_envs.py:212-236` | `stage_switched_journal` (live symlink を `new_target` へ差し替えてから `switched`) | 作るのは分類 `switched` の行なので 0d-2a / reconcile の既存枝 |
| `tests/test_service_app.py:3368 / :3467 / :3486 / :3513 / :3537` | `build_app` が reconcile → sweep → expire の順に呼ぶこと / 例外分離 | 呼び出し順・引数・例外方針を変えない |
| `tests/store/test_plugin_switch_journal.py` | store 層の `insert` / `set_phase` ほか | **`journal_store.set_phase` に手を入れない** (ガードは `_finalize_decision` 側、§3.2) |

### 7.2 runbook の改訂 (`docs/operations/indicator-initial-set-deploy-2026-09-19.md`)

| 箇所 | 現状 | 改訂 |
|---|---|---|
| L56-57 / L108-127 | 「(B) では Python traceback が出る」 | **rc=1 + `エラー: ` の 1 行**に差し替え (AC-10) |
| L146-153 の phase 別表 | `switched` 行 = 「journal 終端・approval は `approved` だが**配備されない**」「手順 5 の再 bless が**必須**」 | **`switched` 行を「retry で配備完了」に改める**。再 bless は**不要**になる (`foreign` のときだけ人間判断) |
| L155-164 | `[retry-switched-approves-without-deploy]` の既知の観測事項としての説明 | **削除** (本束で解消)。代わりに「`foreign` は自動収束しない」を残す |
| **L167-169** (v1.1 で追加、r1 Important 4) | 「いずれの phase でも、**手順 5 (もう一度 bless する) を必ず実行する**」「`switched` では**必須の配備手順**になる」 | **改訂**。本束の後は `switched` でも `approval retry` が配備まで完了するので、手順 5 は**どの phase でも確認 (no-op)** になる。旧手順のままだと不要な再 bless を要求し、**approval 行を 1 本無駄に増やす** (同ファイル「共通の規則」の表が自認している副作用)。「`foreign` (live が第三者に触られている) のときだけ人間が判断する」に置き換える |
| 「approval id の入手」節 | 「同じ bless をもう一度実行して例外文言から読む」「`sqlite3 -readonly` で見る」 | **`afx> approval list` を第一手**に差し替え (例外文言と直接 SQL は代替手段として残す) |
| L162-165 (`preparing` 等は再起動で終端しない / `switched` は reconcile が扱う) | 現状のまま正しい | **不変** (実コードの skip `switch.py:180-183` は変えない) |

---

## 8. 要裁定事項

なし (§0 の R1〜R8 で確定済み)。

起草中に上がった 1 点 (`tests/test_commands.py:541` の戻り文字列アサーションが §3.5 で壊れないか) は
**現物を読んで解消した** — §3.5 の共通接頭辞により既存アサーションは通る (§7.1)。
したがって**書き換える既存テストは `test_runbook_cli_bless_after_post_gate_failure_raises_traceback` の 1 本のみ**
(それ自身が書き換えを指示している)。

なお **R7 (件 4 = 退役の既定)** は別束の設計時に改めて伺う — 本束では扱わない。

---

### 8.1 設計レビュー r1 の処置 (codex sol、`tmp/design-switch-ops/codex-design-r1.md`、C1 / I4 / M0)

**5 件とも採用** (ユーザー裁定 2026-09-19)。sol は全件「未検証 (静的読解)」と明記していたので、
**5 件とも実コードで事実確認し、2 件は probe で裏を取った**。

| # | 指摘 | 事実確認の結果 | 処置 |
|---|---|---|---|
| C1 | reconcile が lock 外で読んだ行/live で巻き戻すと、別プロセスの retry 完遂後に「approved だが未配備」を再生産する | **再現した** (`probe/test_probe_r1.py::test_P2_...`: competitor 完遂後に stale row で `_revert_one` → live 消失・approval approved のまま) | §3.3 を全面改稿。R5 を「lock + **lock 内での行の再取得と分類の再確認**」へ。IV-6 を書き直し、AC-9b (barrier 競合テスト) を新設 |
| I1 | `force_revert_op_id` 分岐の `_revert_one` が lock 対象から漏れ、書き換え箇所は 3 | **そのとおり** (`switch.py:174`。grep で `_revert_one` の呼び出し元は reconcile 内 3・reconcile 外 3 の計 6) | §3.3.3 を 3 箇所の表に訂正。force revert は「行の再取得」まで適用し、**分類の一致は求めない** (phase 無視の割込という既存の意味論を保つ)。§4 に「live を書き換える全経路」の表を新設 (reconcile 外 3 + expire + retire + materialize も現状の事実として記録)。AC-9c |
| I2 | `op_id = None` を落としても `ValueError` にならない (advance は全スキップ、`_finalize_decision` が `set_phase` で `reverted → decided` を直接書く) | **そのとおり** (`probe/test_probe_r1.py::test_P1_...`: 例外なし / 行 1 本 / `decided` / 配備は成功) | §3.2 の根拠を訂正し、必須理由を「停止行を監査記録として残す」に。AC-6 を「行数 2・旧行 `reverted`・新 `op_id` が `decided`」の観測へ書き直し。**追加で `_finalize_decision` の phase ガードを採用** (`set_phase` の呼び出し元 3 箇所を全数確認し、単調性 API を迂回する 1 箇所にだけ置く。store 層には置かない)。AC-16 |
| I3 | `args == ["list"]` では `approval list <n>` が到達不能、`<n>` の観測も無い | **そのとおり** (`commands.py:62-75` の parse) | §3.5 に dispatch 条件と `<n>` の扱いの表を新設 (既定 20 / 上限 200 / 0・負数・非数値は `usage:`)。AC-12a/b/c |
| I4 | 書き換える既存テストは 1 本でなく 2 本 (`test_runbook_post_gate_failure_converges_via_approval_retry` が旧文言と完全一致)、runbook L167-169 も改訂対象 | **そのとおり** (`tests/plugin/test_indicator_initial_set.py:854-855` が `==`、runbook L167-169 が「再 bless 必須」) | §7.1 を **grep 全数から作り直し** (完全一致か部分一致かを 1 本ずつ確認)。§7.2 に L167-169 を追加。AC-15 に同ファイルを明示 |

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-19 | v1.0 | 初版 (設計案 v0.1 の全裁定 R1〜R8 を反映。分類器の契約表・クラッシュ点の全数表・二重実行の排他調査・既存テスト全数表・runbook 改訂表を追加) | `tmp/design-switch-ops/design.md` v0.1 のユーザー承認を受けた spec 化 | `66b4248` |
| 2026-09-19 | v1.1 | 設計レビュー r1 の 5 件を全採用: §3.3 を stale row 再検証プロトコルへ全面改稿 (C1) / live 書き換え経路を 3 箇所へ訂正し §4 に全経路表 (I1) / §3.2 の `op_id = None` の根拠を probe で訂正し `_finalize_decision` の phase ガードを追加採用 (I2) / §3.5 に dispatch 条件と `<n>` の表 (I3) / §7.1 を grep 全数から作り直し書き換え対象を 2 本に、§7.2 に runbook L167-169 を追加 (I4)。AC を 15 → 19 項目に | `tmp/design-switch-ops/codex-design-r1.md` (C1 / I4 / M0)。5 件とも実コードで事実確認、C1 と I2 は `tmp/design-switch-ops/probe/test_probe_r1.py` で再現を実測 | — |
