# テスト衛生 + 小物 設計書 v1 (2026-09-12)

- 日付: 2026-09-12 (初版)
- ステータス: 起案 (レビュー前)
- 対象: main `3e26503`。根拠 = `.superpowers/sdd/plan10-plan/tickets.md` の
  [bootstrap-probe-tests-mkdir-real-logs-dir] (67 行目) / [plugin-locks-accumulate]
  (49 行目) / [missions-finished-at-is-logical] (77 行目) /
  [codex-subscription-expiry-check-never-fires] (73 行目) /
  [archive-index-naming] の残り (78 行目末尾「軽微、据え置き」の再検討)、
  および実コード読解 (grep・`Read`、推測なし)
- 本書の位置づけ: approval-quality 束 (v1.7 でレビューサイクル終了) の次に
  出た小粒欠陥 5 件を 1 束にまとめる。T1 (テストが実資源を触る事故の再演)
  が本体、T2〜T5 は独立した小物

## 0. 前提を疑う

| 通説 | 実測・再読 | 帰結 |
|---|---|---|
| 「session fixture が `cli_runner._TRANSCRIPT_DIR_DEFAULT` を差し替えれば、mission-transcripts の既定保存先は常に隔離される」 | `mission_worker.py:76` は `from ...cli_runner import _TRANSCRIPT_DIR_DEFAULT as _MISSION_TRANSCRIPT_DIR` という **値コピーの import** をしている。`tests/conftest.py:72-88` の session fixture が書き換えるのは `cli_runner` モジュール属性であって、`mission_worker._MISSION_TRANSCRIPT_DIR` という別名の束縛ではない。**同一プロセス内でも** この monkeypatch は `mission_worker.py:253` の `mkdir` を隔離できない。加えて `_run_bootstrap_probe` 系 (`tests/test_mission_worker.py`) は `subprocess.run([sys.executable, "-c", ...])` で **別プロセス**を起こすため、そもそも親プロセスの monkeypatch が届く余地がない (`tests/conftest.py:91-106` の `_guard_real_mission_transcripts_dir_is_never_touched` docstring が後者だけを名指ししているが、前者 (import 時束縛) の方がより基礎的な欠陥) | 「session fixture で差し替えたから安全」という前提そのものが崩れている。是正は隔離のやり方を変える (§T1-a) |
| 「`_plugin_lock` は with 文で獲得・解放される mutex だから、対応する `unlock` で完結する」 | `switch.py:614-628` の `_plugin_lock` は `flock` を取って `yield` し、`finally` で `LOCK_UN` してから `fh.close()` するだけで、**ロックファイル自体 (`plugins/.locks/<name>.lock`) を消す経路がどこにも無い**。`sweep_orphans` (`switch.py:234` 以降) は staging (①) と archive (⑥⑦) を掃除するが `.locks/` には触れない | ロックファイルは意図的な「常駐 mutex」なのか単なる消し忘れなのか、設計として言語化されていなかった。今回明記する (§T2) |
| 「`missions.finished_at` は mission が実際に終わった時刻」 | `core/improve_supervisor.py:134` で `self._improve_loop.commit(mission=mission, ctx=ctx, result=result, now=self._clock.now())` と、worker の結果が返った直後に `now` を 1 回だけ束縛する。この `now` がそのまま `loops/improve_loop.py:2011` `commit()` 内を素通しされ、`_run_strategy_gate` (親ゲート、in-sample + holdout の複数 backtest、実測 ≒70 秒) を経由したあとの `_finalize_success` (`improve_loop.py:1872`) → `finish_improve_mission(..., now=now)` (`improve_loop.py:1945-1948`) にまで渡る。つまり `finished_at` は「worker が結果を返した時刻」であって「決定論コアが承認/観察を確定した時刻」ではない | 名前と実態がずれている。「論理終端時刻」であることを明記するか、直前に取り直すかの二択 (§T3) |
| 「`_check_codex_subscription_expiry` の WARNING は auth.json が壊れている証拠」 | 実 `~/.codex/auth.json` (codex CLI 0.150.1、キー名のみ確認・値は未読) のトップレベルキーは `auth_mode` / `OPENAI_API_KEY` / `tokens` / `last_refresh` で、`tokens` の中身は `id_token` / `access_token` / `refresh_token` / `account_id`。`chatgpt_subscription_active_until` というキーはトップレベルにもネストにも **存在しない** (`service.py:295-326` が読もうとしているキー名がそもそも現行 auth.json のスキーマに無い)。auth.json が壊れているのではなく、検査対象のキー名が実態と合っていない | 「直せば直る」ではなく「今のスキーマにその情報が無い」可能性がある。実装可否を裁定候補として両論併記する (§T4) |
| 「`plugins/_archive/INDEX.md` のヘッダ注記は approval-quality v1.1 (`7837fe9`) で足したので、既存ファイルにも付いている」 | `_append_archive_index` (`improve_loop.py:1615-1659`) はヘッダ (1641-1656 行) を `is_new` (= `not index_path.exists()`) の分岐でしか書かない。approval-quality 実装より前に作られた `INDEX.md` は既に存在するファイルなので、実装後もヘッダが付かないまま追記され続ける (v1.4 の変更履歴で「軽微、据え置き」と記録済みの事実そのもの) | 「実装したから直った」という前提が崩れている。新規作成時だけでなく既存ファイルへの後付けをどうするか裁定が要る (§T5) |

## T1: bootstrap probe テストが実 `logs/mission-transcripts` を作り、runner テストが cwd に残骸を書く [bootstrap-probe-tests-mkdir-real-logs-dir]

### 誰が何をどう扱うか

**(a) `_bootstrap_improve_profile` の mkdir が session fixture の monkeypatch を素通りする**

- `mission_worker.py:76` の `from agentic_fx.runners.cli_runner import _TRANSCRIPT_DIR_DEFAULT as _MISSION_TRANSCRIPT_DIR` は **import 実行時点の値**を `mission_worker` モジュールの別名にコピーする。以後 `cli_runner._TRANSCRIPT_DIR_DEFAULT` を誰かが再代入しても (`tests/conftest.py:86` の session fixture がまさにこれをやる)、`mission_worker._MISSION_TRANSCRIPT_DIR` という名前は最初にバインドされた実体 (`Path(__file__).resolve().parents[3] / "logs" / "mission-transcripts"`、つまり本物のリポジトリ配下) を指したままになる。
- `mission_worker.py:253` の `_MISSION_TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)` は `_bootstrap_improve_profile` (Landlock 適用直前) から呼ばれるため、**この関数を経由する実行はすべて** (同一プロセス内の直接呼び出しであっても、`tests/test_mission_worker.py::_run_bootstrap_probe` の `subprocess.run([sys.executable, "-c", ...], cwd=str(workdir), ...)` (169-178 行目) による別プロセス起動であっても) 本物の `<repo>/logs/mission-transcripts/` を mkdir する。既存リポジトリでは既にディレクトリがある (`ls -la logs/mission-transcripts` で `Access: 2026-09-12 16:18` が確認済み — 本日のテスト実行で触られた形跡) ため気づきにくいが、fresh worktree ではディレクトリが存在せず、`tests/conftest.py:91-114` の `_guard_real_mission_transcripts_dir_is_never_touched` (session 前後のディレクトリ内容スナップショット比較) が ERROR になる。
- 是正: `mission_worker.py` は `cli_runner` モジュールを import した上で `cli_runner._TRANSCRIPT_DIR_DEFAULT` を **都度参照**する形に変える (`from ... import X as Y` の値コピーをやめ、`cli_runner.py` 側のモジュール属性を都度読む)。ただし **これだけでは (a) の後半 (subprocess 起動テスト) は直らない** — 別プロセスは `cli_runner` を再 import して工場出荷時の値に戻る。subprocess 経由のテストに対しては、環境変数 (例: `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR`) 経由で override できる口を `cli_runner._TRANSCRIPT_DIR_DEFAULT` の初期化に追加し、`_run_bootstrap_probe` がその環境変数を tmp_path 配下に向けて子プロセスへ渡す形にする。

**(b) runner テストが cwd に `mcp.json` / `schema.json` / `prompt.txt` を残す (今日 5 回、repo root と worktree `tmp/wt/cx` で観測)**

- `tests/runners/test_claude_runner.py` / `test_codex_runner.py` / `test_cli_runner.py` を全数確認した限り、`workdir=` に渡している値はすべて `tmp_path` 由来 (`workdir = tmp_path / "wd"` 等) で、pytest のユニットテスト自体に workdir を repo root へ向けている箇所は見つからなかった (`grep -n "workdir=" tests/runners | grep -v tmp_path` の結果は全件 `workdir=workdir` という変数参照で、その変数はいずれも同ファイル内で `tmp_path` から作られている)。
- 一方 `mission_worker.py:161,764,898,900` は **設計どおり** `workdir = Path.cwd()` としており (WorkerRunner が `Popen(..., cwd=<mission 専用 dir>)` で子の cwd を固定する契約 — `tests/runners/test_worker_runner.py::test_child_cwd_is_a_dedicated_dir_outside_the_repository` がこの配線を pin 済み)、`ClaudeRunner._build_argv` (`claude_runner.py:64` `self._workdir / "mcp.json"`) / `CodexRunner._build_argv` (`codex_runner.py:47` `self._workdir / "schema.json"`) / `_write_prompt_file` (`cli_runner.py:191,321` `self._workdir / "prompt.txt"`) はいずれも `self._workdir` (= 呼び出し元が渡した workdir) を書き先にする。したがって **pytest 経由での残骸はこのセッションでは再現できなかった** — 観測されている repo root / `tmp/wt/cx` の残骸は、pytest 以外の経路 (worktree で `python -m agentic_fx.mission_worker` を手動起動する A4/codex-host-probe 系の実機確認、または cwd を明示せずに `ClaudeRunner`/`CodexRunner` を対話的に生成した調査スクリプト) から来た可能性が高い。**推測で断定しない** — T1 着手時にまず `find . -maxdepth 2 -newer <直近コミット> \( -name mcp.json -o -name schema.json -o -name prompt.txt \)` 相当で再現条件を1本特定してから直す。
- 対処の向き自体は決まっている: どの経路であれ「cwd に production ファイルを残さない」という不変条件を **session guard** で機械的に検出できるようにする (下記)。

**両方に共通する対処: 「repo root に新規 untracked ファイルが無い」session guard**

- `tests/conftest.py` に、`_guard_real_mission_transcripts_dir_is_never_touched` と同じ「session 前後のスナップショット比較」形の autouse session fixture を追加する。対象は repo root 直下 (再帰しない — サブディレクトリの意図した一時ファイルまで拾うと fail closed が過検出になる) の `git status --porcelain --ignored=no` 差分、または `os.listdir(repo_root)` の差分。fail closed (新規 untracked ファイルが 1 つでも増えたら ERROR)。

### 変更点表

| file | 変更 |
|---|---|
| `src/agentic_fx/mission_worker.py` | `_TRANSCRIPT_DIR_DEFAULT as _MISSION_TRANSCRIPT_DIR` の値コピー import をやめ、`cli_runner` モジュールを import して `cli_runner._TRANSCRIPT_DIR_DEFAULT` を `_bootstrap_improve_profile` 呼び出し時に都度参照する形へ変更 |
| `src/agentic_fx/runners/cli_runner.py` | `_TRANSCRIPT_DIR_DEFAULT` の初期化に環境変数 override (例: `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR`) を追加し、subprocess 経由でも隔離できるようにする |
| `tests/test_mission_worker.py::_run_bootstrap_probe` | 子プロセスの env に隔離用環境変数を tmp_path 配下で設定して渡す |
| `tests/conftest.py` | repo root 直下の untracked ファイル差分を session 前後で比較する新規 autouse fixture (fail closed) を追加。既存 `_guard_real_mission_transcripts_dir_is_never_touched` はそのまま残す (mission-transcripts 専用の観測は引き続き有用) |

### 検証

- pin: `_bootstrap_improve_profile` を (i) 同一プロセス内で直接呼ぶケース、(ii) `_run_bootstrap_probe` で別プロセス起動するケースの両方で、隔離用の tmp_path 配下だけが mkdir され、`cli_runner._TRANSCRIPT_DIR_DEFAULT`/環境変数どちらの隔離が効いても実 `logs/mission-transcripts/` に新規エントリが増えないこと
- 段0 変異: `mission_worker.py` の値コピー import を都度参照に変えた後、わざと元の値コピー方式へ戻す変異が red になること (session fixture 経由の隔離が効かなくなる) / 環境変数 override を読まない変異 (subprocess 経路の隔離が効かなくなる)
- 実機 CP: **fresh worktree でフルスイートを回し**、実行前後で repo root 直下の untracked ファイルがゼロであること、かつ `_guard_real_mission_transcripts_dir_is_never_touched` が ERROR にならないこと

## T2: `plugins/.locks/<name>.lock` の蓄積 [plugin-locks-accumulate]

### 誰が何をどう扱うか

- `_plugin_lock` (`switch.py:614-628`) は `flock` 契約 (blocking, `LOCK_EX`) を守るためだけの実装で、ロックファイルの unlink はどこにもない。approve/reject/bless の 4 箇所 (`switch.py:710,980,1257,1350`) がこの context manager を通るたびに `plugins/.locks/<name>.lock` が (無ければ) 新規作成されるが、**削除されることは一度もない** — 名前の異なる候補が approve/reject されるたびに 1 ファイルずつ純増する (再発観測: reject 4 件で 3 → 7 件)。
- `sweep_orphans` (`switch.py:234` 以降) は① 孤児 staging、⑥⑦ archive GC を扱うが `.locks/` には触れない — 掃除の受け皿がそもそも設計されていない。

**裁定候補 1: decide (approve/reject/bless) 完了直後に unlink する**

- 実装: `_plugin_lock` を出た直後 (`with` を抜けた後) に `lock_path.unlink(missing_ok=True)` を呼ぶ。
- 長所: ロックファイルが「今まさに進行中の候補」の生存期間とだいたい一致し、`.locks/` の恒常肥大が起きない。
- 短所: `flock` + unlink の組み合わせは古典的な TOCTOU の穴を持つ — プロセス A が `LOCK_UN` して unlink する直前に、プロセス B が (unlink 前の) 同じ inode を `open()` 済みで `flock` 待ちしていた場合、B は「削除済みだが open fd は生きている」ファイルをロックし、その後 A (または別プロセス C) が同名で新規作成した**別 inode** のファイルとは排他されない。ただし本設計での呼び出しパターン (`with _plugin_lock(...): ...` を抜けたら即 unlink、次の呼び出しは `lock_dir.mkdir` 済みの状態で `open(..., "a+")` から入る) では、同一 `name` に対する approve/reject/bless の呼び出し頻度は人間の承認操作律速 (秒〜分単位) であり、mission 実行中の自動生成候補 (`switch.py:710` の `switch_candidate` 相当) との衝突ウィンドウは実務上ミリ秒オーダーに閉じる。**理論的な穴が残ることを明記した上で採用する**なら、この裁定でよい。

**裁定候補 2: startup sweep (`sweep_orphans`) で回収する**

- 実装: `sweep_orphans` に ⑧ として `.locks/` 掃除を追加。DB 側に対応する `pending` な approval/candidate が存在しない名前のロックファイルだけを消す (staging の① と同じ「参照されているか」判定パターンを流用できる — ロック名 = plugin/candidate 名なので `approval_requests`/`candidate` テーブルとの突合で決まる)。
- 長所: `sweep_orphans` は「runner 起動前の単一プロセス、他プロセスからの同時アクセスが無い」前提 (docstring 244-250 行目) で動くため、TOCTOU の懸念が構造的に無い。既存の掃除の受け皿にただ 1 経路足すだけで済む。
- 短所: 蓄積してから次回起動まで消えない (即時性が無い) — ただし ticket の観測 (「reject 4 件で 3 → 7 件」) はディスク容量問題ではなく「掃除経路が無い」ことそのものが問題であり、即時性は要件に無い。

**推奨**: 裁定候補 2 (startup sweep) を既定とし、裁定候補 1 は「即時性が必要になった場合の追加策」として設計に記録だけしておく (両方入れても矛盾しない — 二重の安全網になる)。**最終判断はユーザー裁定**。

### 変更点表

| file | 変更 (裁定 2 採用時) |
|---|---|
| `src/agentic_fx/plugin/switch.py::sweep_orphans` | `.locks/*.lock` のうち、対応する pending approval_request/候補が存在しない名前のファイルを削除する分岐 (⑧) を追加 |

### 検証

- pin: pending な approval が無い名前のロックファイルは sweep で消える / pending がある名前のロックファイルは残る (誤って進行中の排他を壊さないこと) / `.locks/` ディレクトリ自体が無い場合は何もしない
- 段0 変異: 参照判定 (pending 突合) を外して無条件削除にする変異が red になること (進行中候補のロックを消してしまう事故が検出できるように)

## T3: `missions.finished_at` は論理終端時刻であり壁時計ではない [missions-finished-at-is-logical]

### 誰が何をどう扱うか

- `core/improve_supervisor.py:134` で `now=self._clock.now()` が worker 結果到着直後に 1 回束縛され、`loops/improve_loop.py:2011` の `commit(..., now, ...)` を通じて `_run_strategy_gate` (in-sample + holdout の複数 backtest、実測 ≒70 秒) の**後**に呼ばれる `_finalize_success`/`finish_improve_mission` (`improve_loop.py:1945-1948`) の `finished_at` に使われる。つまり `finished_at` は「決定論コアが親ゲートまで含めて確定した時刻」ではなく「worker プロセスが completed で戻ってきた時刻」であり、実際の所要時間 (壁時計) より約 22%(≒70 秒) 過小に出る。

**裁定候補 1: 「論理終端時刻」と設計書に明記し、実測が要る場面は activity の壁時計を併記する運用にする**

- 実装コストゼロ (ドキュメントのみ)。`mission_worker` 設計書・ledger 設計書に「`finished_at` = worker 結果到着時刻 (論理終端)。実際の壁時計終端が要る分析は `activity` テーブルの `approval_requested`/`gate_failed` 等のタイムスタンプ (commit 成功後に書かれる — `_finalize_success` の 1963 行目以降参照) を使うこと」と明記する。
- 長所: `now` を単一の値として `commit()` 全体を貫通させる既存の Clock 注入の流儀 (テストでの `FixedClock` 差し替えやすさ) を一切壊さない。
- 短所: 「終端時刻」という列名からは論理終端であることが読み取れず、次に触る人がまた同じ誤解をする可能性がある (今回がまさにその再演)。

**裁定候補 2: `finished_at` を commit 直前 (`_finalize_success`/`finish_improve_mission` 呼び出し直前) に取り直す**

- 実装: `_finalize_success` 等の各終端メソッド内で `finished_at` 用にだけ新しい `datetime` (`self._clock.now()` 相当) を取り直し、他の用途 (ledger の `now` 列、gate row の timestamp 等、既存のまま `now` 引数を使う) とは別の値にする。
- 長所: 列名と実態が一致する。
- 短所: `commit()` は `self._improve_loop` (状態を持たないメソッド群) からしか呼ばれず、`Clock` は `ImproveSupervisor` 側にある (`core/improve_supervisor.py`) ため、`improve_loop.py` 側の各終端メソッドに `Clock` を追加で注入するか、`now` 引数とは別に「finished_at 用の clock callable」を渡す配線変更が要る。テストの `FixedClock` 前提 (1 回の `now()` 呼び出しで全体が確定する pin が多数ある) を壊さない範囲での変更が必要 — 影響範囲の洗い出しに手間がかかる。

**推奨**: 裁定候補 1 (ドキュメント明記) を既定とする。裁定候補 2 は Clock 配線の変更コストに対して得られる精度の改善が小さい (22% のずれは診断用途では致命的でない) ため、ユーザーが「厳密な壁時計が要る」と判断した場合のみ着手する。**最終判断はユーザー裁定**。

### 変更点表

| file | 変更 (裁定 1 採用時) |
|---|---|
| `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` または `2026-09-10-ledger-preserve-design.md` の該当節 (`missions.finished_at` の説明箇所) | 「`finished_at` は論理終端時刻 (worker 結果到着時点の `now`) であり、親ゲート (in-sample/holdout backtest) 実行分は含まない。壁時計の終端が要る場合は `activity` の該当行のタイムスタンプを使う」旨を追記 |

### 検証

- (ドキュメントのみのため pin/逆変異は無し。裁定候補 2 を選んだ場合は別途 pin: `finished_at` と `_run_strategy_gate` 呼び出し前後の壁時計を突合し、`finished_at` が gate 後の時刻に一致することを確認)

## T4: `_check_codex_subscription_expiry` が実 auth.json のスキーマと合っていない [codex-subscription-expiry-check-never-fires]

### 誰が何をどう扱うか

- `service.py:295-326` の `_check_codex_subscription_expiry` は `auth.json` を読み `raw.get("chatgpt_subscription_active_until")` を期待するが、実 `~/.codex/auth.json` (codex CLI 0.150.1、キー名のみ確認: `python -c "import json;print(list(json.load(open('~/.codex/auth.json')).keys()))"`) のトップレベルキーは
  ```
  ['auth_mode', 'OPENAI_API_KEY', 'tokens', 'last_refresh']
  ```
  であり、`tokens` のネストキーは `id_token` / `access_token` / `refresh_token` / `account_id`。`chatgpt_subscription_active_until` はどこにも無い。よって `_check_codex_subscription_expiry` は `service.py:317` の「キーが無い」分岐 (WARNING) を毎回踏む — 4 run 連続で観測されている空振りは auth.json の破損ではなく、**検査対象のキー名が現行スキーマに存在しない**ことが原因。

**裁定候補 1: 実キーから期限相当の情報を導出できるなら是正する**

- `tokens.id_token` は JWT (OpenID Connect の id_token) である可能性が高く、JWT の payload (base64url デコードのみ、署名検証は不要 — 本検査は「期限切れの目安表示」であって認可判定ではない) に `exp` (Unix epoch 秒) クレームが含まれていれば、そこから有効期限相当の情報が取れる可能性がある。ただし **本タスクの制約 (値を読まない) の範囲では JWT payload の中身までは確認していない** — 実装着手時に `id_token` の payload 部分 (署名部分は読まない) をデコードして `exp` の有無を確認する 1 手順が必要。`exp` があれば `_check_codex_subscription_expiry` を「`tokens.id_token` の `exp` クレームを見る」形に書き換える。
- 長所: 検査の意図 (サブスク切れの早期警告) を活かせる。
- 短所: `exp` は ChatGPT サブスクリプションの契約期限ではなく OAuth トークンの有効期限 (数時間〜数日オーダーで自動更新される) である可能性が高く、その場合は「サブスク切れ警告」としての意味が無い (別物を測ることになる) — 実装前に `exp` が指す対象を codex CLI のドキュメント/挙動から確認する必要がある。

**裁定候補 2: 検査を撤去する**

- 実装: `_check_codex_subscription_expiry` の呼び出し (`service.py:386`) と定義を削除する。
- 長所: 存在しないキーへの空振り WARNING が消え、「常に鳴る警告」という運用上のノイズが無くなる (fail closed にしていない = 実害は無いが、無意味な警告は監視の signal-to-noise を下げる — [[outbound-request-budget-is-a-design-constraint]] 等と同様、監視の判定条件自体を疑う姿勢に沿う)。
- 短所: サブスク切れの早期検知という当初の意図そのものを失う。

**推奨**: まず裁定候補 1 の前提確認 (`id_token` の payload に `exp` があるか、それが指す対象は何か) を実装着手時の 1 手順として行い、有用な情報が取れなければ裁定候補 2 (撤去) に倒す。**最終判断はユーザー裁定** (裁定候補を両論併記のまま提示)。

### 変更点表

| file | 変更 |
|---|---|
| `src/agentic_fx/service.py::_check_codex_subscription_expiry` | 裁定 1: `chatgpt_subscription_active_until` の代わりに `tokens.id_token` の JWT `exp` クレームを見る形に書き換え (`exp` の意味が「サブスク期限」でないと判明した場合は裁定 2 へ) / 裁定 2: 関数定義と `service.py:386` の呼び出しを削除 |

### 検証

- 裁定 1 の場合 pin: `exp` がある/ない両方の auth.json フィクスチャで正しく警告/非警告になる / `exp` が過去日時のフィクスチャで警告が出る
- 裁定 2 の場合: 呼び出し削除により該当テストが不要になることを確認し、テストごと削除 (欠陥を隠すのではなく検査自体を消すという裁定を明記)

## T5: `plugins/_archive/INDEX.md` の既存ファイルにヘッダ注記が付かない [archive-index-naming] の残り

### 誰が何をどう扱うか

- approval-quality 設計 v1.1 (`7837fe9`) で `_append_archive_index` (`improve_loop.py:1615-1659`) にヘッダ (「終端ログ (GC 非対象)。承認可否の目録ではない。…」1641-1656 行目) を追加したが、これは `is_new` (`index_path.exists()` が False) の分岐でしか書かれない。したがって **実装より前に作られた既存 `INDEX.md`** (承認品質 v1.4 の変更履歴に「既存 INDEX.md にヘッダ注記は付かない (軽微、据え置き)」と明記済みの事実そのもの) は、実装後もヘッダ無しのまま行が追記され続ける。

**裁定候補 1: startup sweep で 1 回だけヘッダを補う**

- 実装: `sweep_orphans` (または専用の起動時チェック) に、`INDEX.md` が存在してかつファイル先頭がヘッダ文言と一致しない場合、既存内容の直前にヘッダ 3 行 (説明文 + テーブルヘッダ 2 行) を挿入する処理を追加する (ファイル全体を読み直して先頭に挿入し直す — 追記専用の現行方式とは別処理になる)。
- 長所: 既存ファイルも新規ファイルも同じ見た目になり、「このファイルは目録ではない」という注記がどのファイルにも及ぶ。
- 短所: 1 回限りの移行処理をどこに置くか (`sweep_orphans` は起動のたびに呼ばれるため、「先頭が既にヘッダと一致するか」を毎回文字列比較する軽いコストは常時発生する — 実害は小さいが「1 回だけ」ではなく「毎起動チェックして未挿入なら挿入」という実装になる)。ファイル全体の書き換えは `fsync` 済みの追記専用ファイルに対する非可逆な形式変更であり、慎重な実装 (一時ファイル→rename) が要る。

**裁定候補 2: 据え置き (現状のまま)**

- 実装コストゼロ。「終端ログであって目録ではない」という位置づけは設計書 (approval-quality §B) に明記済みであり、既存ファイルへの後付けは運用上の見た目の問題に留まる (実害: 人間が既存の古い `INDEX.md` を読んだときにヘッダが無く、目録だと誤解する可能性が残るのみ)。
- 長所: シンプル。実装 v1.4 で既に「軽微、据え置き」として一度裁定された経緯がある。
- 短所: 「軽微」の判断が変わらない限り、この観測は何度も再浮上する (実際に今回のチケット化で再浮上した)。

**推奨**: 据え置き (裁定候補 2) を既定とし、T2 の startup sweep 拡張 (§T2 裁定候補 2 採用時) のついでに実装するなら裁定候補 1 を安価に相乗りできる、という位置づけで両論併記する。**最終判断はユーザー裁定**。

### 変更点表

| file | 変更 (裁定 1 採用時) |
|---|---|
| `src/agentic_fx/plugin/switch.py::sweep_orphans` | `plugins/_archive/INDEX.md` の先頭がヘッダ文言と一致しなければ、一時ファイル書き込み→rename でヘッダを先頭に挿入する処理を追加 (T2 の `.locks` 掃除と同じ起動時 sweep に相乗り可) |

### 検証

- 裁定 1 の場合 pin: ヘッダ無し既存ファイル → sweep 後にヘッダ付き / 既にヘッダ付きファイル → 二重挿入されない / ファイルが無い場合は何もしない (新規作成は既存の `_append_archive_index` に任せる)
- 段0 変異: ヘッダ一致判定を外す (毎起動で二重挿入される事故が red になること)

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案。T1 (bootstrap probe の import 時束縛欠陥 + runner cwd 残骸の session guard) を本体、T2 (`.locks` 蓄積) / T3 (`finished_at` 論理時刻) / T4 (codex auth.json スキーマ不一致) / T5 (INDEX.md 既存ファイルへのヘッダ後付け) を小物として起票。根拠 = `tickets.md` 該当行 + 実コード読解 (`mission_worker.py:76,161,253,764,898,900`、`cli_runner.py:43,191,321`、`claude_runner.py:64`、`codex_runner.py:47`、`switch.py:234,614-628,710,980,1257,1350`、`improve_loop.py:1615-1659,1872-1953,2011,2163,2239-2245`、`core/improve_supervisor.py:134`、`service.py:295-326,386`、`tests/conftest.py:72-114`)、`~/.codex/auth.json` のキー名実測 (値は未読) | 新束の起案 | - |
