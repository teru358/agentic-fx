# [switch-ops-hardening] 設計書 v1.0

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
| R5 | reconcile の lock (Q4) | **巻き戻しの枝だけ**を plugin flock の下に入れる。`live == new_target` の枝は `retry_approval` が内部で `_plugin_locks` を取るので**包まない** (flock は非再入 — 包むと自己デッドロック) |
| R6 | 退役 (件 4) | `[retire-symlink-deployed-plugin]` は**別束**。本束の非スコープ |
| R7 | 件 4 の既定 (依存 strategy がある場合) | 別束の設計時に再度伺う (現時点の推奨は「拒否 + `--force`」) |
| R8 | CLI / シェルの例外 (件 2) | `_plugin_bless` / `_plugin_materialize` を `_plugin_retire` の作法 (rc=1 + `エラー: ` + 次の 1 手) に揃える。`UnresolvedJournalError` の文言が `op_id` / `approval_id` を含むことは維持する |

---

## 1. スコープと非スコープ

**スコープ**
- `switch.py`: `switched` 行の live 分類器の新設、`reconcile_switch_journals` の載せ替え (挙動不変)、
  `approve_candidate` 0d の R1 化、reconcile の巻き戻し枝の flock 化 (R5)。
- `backtest/cli.py`: `_plugin_bless` / `_plugin_materialize` の例外処理を揃える。
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

**0d-2c の必須事項 (実コードで確認)**: 0d は `op_id = existing_journal["op_id"]` を保持したまま下流へ進み
(`switch.py:1440`)、新規経路は **`if op_id is None:` のときだけ `begin_switch_journal` を呼ぶ**
(`switch.py:1554-1559`)。巻き戻しで停止行は `reverted` (終端) になるので、**`op_id` を `None` に戻さないと**
`_advance_to_decided` → `advance_switch_journal` の単調性チェック (`switch.py:90-98`: `reverted` → `versioned` は後退)
で `ValueError` になる。**`op_id = None` への再設定は実装の必須事項**として本設計書に固定する
(§6 AC-6 がこれを変異で守る)。

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

### 3.3 R5: reconcile の巻き戻し枝の flock 化

**包む枝**: `reconcile_switch_journals` の中で **live symlink を書き換える 2 箇所**。
1. 分類 `not_switched` → `_revert_one` (現行 `switch.py:225-226`)
2. 分類 `switched` かつ `_unresolved_after_switch` が pin 破れを返したときの `_revert_one` (現行 `:205-207`)

どちらも `with _plugin_lock(plugins_root, row["name"]):` の内側に入れる。

**包まない枝とその理由**
- **`switched` かつ pin 健全 → `retry_approval`** (`:216-220`): `retry_approval` → `approve_candidate` →
  **`_plugin_locks` を自分で取る**。外側で同じ lock を持っていると**自己デッドロックする** —
  `flock(2)` のロックは open file description に紐づき、同じパスを 2 回 `open()` すれば別 ofd になるため
  自プロセスのロックで待たされる (`_plugin_locks` docstring の probe 実測 `switch.py:820-846`、
  `test_plugin_lock_is_not_reentrant_within_one_process` が pin)。
  `_unresolved_after_switch` の docstring (`:262-269`) も「本 helper と `retry_approval` を同一 lock 内へ
  まとめてはならない」と同じ理由で明記している。**この規律を本束でも守る。**
- **`foreign`** (`:227-232`): FS を触らないので lock 不要。activity を書くだけ。
- **`phase != 'switched'`** (`:180-183`): skip。FS 効果が無い。

**lock の取り方と順序**: 巻き戻しは **その name 1 本だけ**を取る (`_plugin_lock`、依存 indicator は含めない)。
理由: 巻き戻しが触るのは `plugins/<name>` の 1 エントリだけで、依存名の inventory を読まない。
`_plugin_locks` の `sorted(set(...))` 規約は**複数名を取る場合の順序固定**のためのもので、
1 名なら順序問題は起きない。`_unresolved_after_switch` が内部で依存名込みの `_plugin_locks` を取るが、
それは**この巻き戻しより前に閉じている** (with を抜けてから返る) ので入れ子にならない。

**自己デッドロックしないことの根拠 (全数)**: 新しく lock を取る 2 箇所の内側から呼ばれるのは
`_revert_one` と `journal_store.set_phase` と `activity.write` だけで、**いずれも `_plugin_lock` を取らない**
(`switch.py:128-142`)。`retry_approval` / `approve_candidate` / `_unresolved_after_switch` は
どれも lock の**外**に置く。

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

**`approval list`** (`commands.py` の `dispatch` に `cmd == "approval" and args == ["list"]` を追加):

| 項目 | 決定 | 根拠 |
|---|---|---|
| 並び順 | `id` 昇順 (= 申請順) | 古い順に片付けるのが運用の自然な順序。`approvals.pending` の既定に合わせる |
| 件数上限 | 既定 50、`approval list <n>` で変更可 | `log` / `activity` の `[n]` 引数と同じ作法 (`commands.py:73-76`) |
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
| IV-6 | live symlink を**書き換える**操作はすべて `plugins/.locks/<name>.lock` の `flock` 下で行う ← R5 で reconcile も揃い、全経路で成立する |

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
| **AC-6** | 変異: 0d-2c の `op_id = None` を落とすと **`ValueError` (phase order violation) で red** |
| **AC-7** | `switch_live` を**恒久的に**失敗させた状態で `approval retry` を 2 回打つと、毎回「`reverted` 1 本 + 新しい `switched` 1 本」になり、**非終端行は常に 1 本** (IV-1)、approval は `pending` のまま |
| **AC-8** | AC-1 の成功後にもう一度 `approval retry` を打つと **no-op** (approval は `approved` のまま、journal 行は増えない、live 不変) |
| **AC-9** | reconcile の巻き戻し枝が `plugins/.locks/<name>.lock` を保持していることの pin (`_plugin_lock` の spy、`test_reconcile.py:443 test_reconcile_resolution_holds_the_dependency_locks` の流儀)。かつ `live == new_target` の枝では**外側の lock を取っていない**こと (取れば自己デッドロック — 既存 pin `test_plugin_lock_is_not_reentrant_within_one_process` と整合) |
| **AC-10** | 未終端 journal が残った状態で `uv run afx plugin bless <name> --from _human` → **rc=1**、stderr に `op_id=` と `approval_id=` と次の 1 手、**traceback が出ない** |
| **AC-11** | `uv run afx plugin materialize <name>` が containment 違反で失敗するとき rc=1 と `エラー: ` 形式 (`_plugin_materialize` の中で処理される) |
| **AC-12** | `afx> approval list` が pending を id 昇順で列挙し、0 件なら `承認待ちはありません`。未終端 journal があれば末尾の節に `op_id` / `name` / `phase` / `approval_id`、無ければ節ごと出ない |
| **AC-13** | `approval list` の出力に holdout / in_sample の数値が**含まれない** |
| **AC-14** | `approval retry` の戻り文言 (§3.5 の表の全行) をそれぞれの到達状態で観測する。特に **`foreign` のときに「配備まで完了しました」と言わない**こと、および `foreign` と再失敗が**別の文言**になること (= 分類器で区別できていること) |
| **AC-15** | 既存の `tests/plugin/test_switch_journal.py` / `test_reconcile.py` / `test_switch_paths.py` / `test_materialize_retire.py` / `test_commands.py` が**1 本も書き換えずに緑** |

---

## 7. 変更ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/agentic_fx/plugin/switch.py` | 分類器の新設 / `reconcile_switch_journals` の載せ替え (挙動不変) + 巻き戻し枝の flock 化 / `approve_candidate` 0d の R1 化 + `op_id = None` / `_close_own_unfinished_journal_if_any` の 1 本化とコメント更新 |
| `src/agentic_fx/backtest/cli.py` | `_plugin_bless` に `UnresolvedJournalError`、`_plugin_materialize` に `ValueError` |
| `src/agentic_fx/commands.py` | `approval list` / `approval retry` の結果報告 / `_HELP` |
| `tests/plugin/test_switch_ops_hardening.py` (新規) | AC-1〜AC-9 |
| `tests/backtest/test_cli.py` または新規 | AC-10 / AC-11 |
| `tests/test_commands.py` | AC-12〜AC-14 を**追記** (既存は書き換えない) |
| `docs/operations/indicator-initial-set-deploy-2026-09-19.md` | 改訂 (下記) |
| `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` | §5.1-1 に「retry の `not_switched` 処置」を 1 段落追記 (既存規則の具体化。規則自体は変えない) |

### 7.1 既存テストへの影響 (全数)

| テスト | 現状が pin しているもの | 本束での扱い |
|---|---|---|
| `tests/plugin/test_indicator_initial_set.py::test_runbook_cli_bless_after_post_gate_failure_raises_traceback` (`:996`) | **`UnresolvedJournalError` が CLI を素通りして traceback になること** | **書き換える**。テスト自身の docstring が「ticket が直ったら新しい振る舞いへ書き換えること」と明記している (`:1006-1008`)。`pytest.raises` → `rc == 1` + stderr の `op_id=` / `approval_id=` へ。前半 (1 回目の失敗が rc=1) は不変。**逸脱として最終報告に明記する** |
| `tests/plugin/test_switch_paths.py:770 / :807 / :1023 / :1077` (`_simulate_crash_after_switch_before_decide` を使う 4 本) | 0d の `switched` 経路 (再検証・再作成・tamper 検出) | **不変**。この helper は **`switch_live` 完了後**に止めるので (`:751-757`)、live は `new_target` を指す = 分類 `switched` = 0d-2a (現行と同じ経路) |
| `tests/plugin/test_switch_paths.py:677` (`retry_from_recorded_phase`) | `recorded` からの retry | **不変** (`phase != 'switched'`) |
| `tests/plugin/test_switch_paths.py:1119 / :1160` (`stuck_preparing_journal`) | `_close_own_unfinished_journal_if_any` の 2 経路 | **不変** (1 本化しても非 switched 行の扱いは同じ) |
| `tests/plugin/test_switch_journal.py:238` (`switched_recovery_absent_old_kind_with_no_live_reverts`) | reconcile の `absent` disjunct 2 | **不変** (分類器がこの disjunct を保持する — §3.1) |
| `tests/plugin/test_reconcile.py:382 / :423` (`broken_pin_is_reverted` / `intact_pin_proceeds_to_decided`) | reconcile の `switched` 2 枝 | **不変** (載せ替えは挙動不変。R5 の lock 追加後も結果は同じ) |
| `tests/plugin/test_reconcile.py:443` (`reconcile_resolution_holds_the_dependency_locks`) | `_unresolved_after_switch` が依存 lock を握ること | **不変**。AC-9 の新規 pin と**衝突しない** (こちらは `switched` 枝の内側、AC-9 は巻き戻し枝) |
| `tests/plugin/test_materialize_retire.py:236` | legacy plain → retire → `approval retry` | **不変** (retire 後は journal 無し = 素の新規経路) |
| `tests/test_commands.py:541 / :569` (`approval_retry_dispatches` / 未配線) | `:541` は `retry_approval` を spy に差し替えて `calls == [(42, "shell")]` / `assert "再試行" in out` / activity `retry` を見る (実物で確認)。`:569` は未配線文言 | **両方とも不変**。`:541` の id 42 は DB に無いので §3.5 の「approval 行が読めない」に落ちるが、**接頭辞 `approval #N を再試行しました: ` に「再試行」が含まれる**ので既存アサーションは通る。activity `retry` の記録位置も変えない |
| `tests/test_service_app.py:3368 ほか` | `build_app` が reconcile → sweep → expire の順に呼ぶこと / 例外分離 | **不変** (呼び出し順・引数は変えない) |
| `tests/plugin/test_switch_paths.py:1583 / :1604 / :1635` (lock 順序) | `_plugin_locks` の sorted/unique | **不変** (巻き戻しは 1 名のみ `_plugin_lock`) |

### 7.2 runbook の改訂 (`docs/operations/indicator-initial-set-deploy-2026-09-19.md`)

| 箇所 | 現状 | 改訂 |
|---|---|---|
| L56-57 / L108-127 | 「(B) では Python traceback が出る」 | **rc=1 + `エラー: ` の 1 行**に差し替え (AC-10) |
| L146-153 の phase 別表 | `switched` 行 = 「journal 終端・approval は `approved` だが**配備されない**」「手順 5 の再 bless が**必須**」 | **`switched` 行を「retry で配備完了」に改める**。再 bless は**不要**になる (`foreign` のときだけ人間判断) |
| L155-164 | `[retry-switched-approves-without-deploy]` の既知の観測事項としての説明 | **削除** (本束で解消)。代わりに「`foreign` は自動収束しない」を残す |
| 「approval id の入手」節 | 「同じ bless をもう一度実行して例外文言から読む」「`sqlite3 -readonly` で見る」 | **`afx> approval list` を第一手**に差し替え (例外文言と直接 SQL は代替手段として残す) |

---

## 8. 要裁定事項

なし (§0 の R1〜R8 で確定済み)。

起草中に上がった 1 点 (`tests/test_commands.py:541` の戻り文字列アサーションが §3.5 で壊れないか) は
**現物を読んで解消した** — §3.5 の共通接頭辞により既存アサーションは通る (§7.1)。
したがって**書き換える既存テストは `test_runbook_cli_bless_after_post_gate_failure_raises_traceback` の 1 本のみ**
(それ自身が書き換えを指示している)。

なお **R7 (件 4 = 退役の既定)** は別束の設計時に改めて伺う — 本束では扱わない。

---

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-19 | v1.0 | 初版 (設計案 v0.1 の全裁定 R1〜R8 を反映。分類器の契約表・クラッシュ点の全数表・二重実行の排他調査・既存テスト全数表・runbook 改訂表を追加) | `tmp/design-switch-ops/design.md` v0.1 のユーザー承認を受けた spec 化 | — |
