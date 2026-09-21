# [backtest-failure-readable] 設計書 v1.2

対象 commit: `fc318ab`。束 A は、現行の CPU 上限を変えずに、backtest と live plugin 評価の失敗を人間には診断可能に、改善 agent には安全な固定分類として届ける。

## 要点

- 変わる: worker の終了理由・CPU を親が一度だけ観測し、人間向け技術ログと activity に記録する。
- 変わる: agent には数値や stderr でなく、固定 `error` と固定 hint だけを返す。
- 変わる: 同じ content hash・pair の CPU 上限失敗は 2 回観測後、3 回目から実行せずに拒否する。
- 変わらない: `plugin.sandbox_session_cpu_sec` は worker 1 プロセス寿命の累積 CPU 上限、既定 60 秒、soft==hard のままである。
- 変わらない: 15m・依存 3 本の戦略は束 A 後も 60 秒付近で失敗する。完走ではなく、`worker_cpu_limit` として読める失敗になる。
- 束 B へ送る: per-call/平均 CPU、planned call 数、CPU 予算式・設定移行、timeout の cancellation、live hook の非同期化と wall 遅延対策。

下書きと設計レビュー記録（リポジトリ外）は `tmp/design-b1/design-A.md` および `tmp/design-b1/r4/verdicts.md` に保管する。本書はそれらを参照せず単体で読める完成版である。

---

## 1. 目的、範囲、前提

### 1.1 目的

1. worker が死んだとき、人間は親観測の終了 code/signal、累積 CPU、bounded・escaped stderr tail を技術ログで読める。
2. 改善 agent は固定分類と固定 hint だけを受け、CPU 数値、設定値、終了 code/signal、stderr は受け取らない。
3. started 後の improve backtest は成功・失敗を問わず `backtest_cpu` activity をちょうど 1 行残し、live の失敗も activity で見える。
4. 同じ `(content_hash, pair)` の実測済み CPU 上限失敗だけを、3 回目から実行前に抑制する。

### 1.2 範囲

- worker lifecycle の単一 `wait4`、親 kill の区別、終了 code/rusage 保存、stdout 待ちと worker 死亡監視の競合解消。
- `RLIMIT_CPU` の現行値を維持した typed `SandboxError`、plugin import 前の `RLIMIT_CORE=(0,0)`。
- 匿名 stderr file の bounded tail・escape と、人間向け技術ログだけへの出力。
- improve の固定公開分類、counter/ledger の整合、全 outcome の activity。
- live の failure activity、固定分類、間引き通知、サービス composition root の配線。
- unreaped worker の強参照 orphan 管理と、終端状態の冪等 close。
- 運用 runbook と、各条件を殺す逆変異テスト。

### 1.3 範囲外

- `sandbox_session_cpu_sec` の名称・意味・既定値・設定 schema/example の変更。
- per-call CPU、平均 CPU、startup 枠、planned call 数、`getrusage` 差分、旧キー移行。
- replay 後に得る raw grid 数を session 開始前の planned call 数や CPU 予算式へ使うこと。raw grid metadata は replay 後にしか得られず、これらは束 B で扱う。
- RPC timeout の cancellation/process-group 中止、live maintenance の非同期化、scheduler busy watchdog、資金保護 tick の遅延上限。
- worker rotation、backtest 高速化、holdout 期間・gate・承認基準、実設定/DB/log/plugin の変更、サービス再起動。
- process group を抜けた孫の完全回収・CPU 完全計上、悪意ある plugin の完全隔離。

### 1.4 既知の事実

- `sandbox_session_cpu_sec` は worker ごとの累積 CPU 上限であり、strategy replay は初回評価から replay 全体で同じ worker を使う。
- 調査対象の 15m・依存 3 本は完走に約 64 CPU 秒を要するため、既定 60 秒では失敗する。束 A は上限を上げない。
- 成功時の worker 自己申告 CPU は異常終了時に得られない。以後の telemetry は成功・失敗とも親の `wait4` 値を唯一の出所にする。close 応答の `cpu_sec` field は protocol 互換のため残すが、比較・fallback に使わない。
- live worker の寿命は 1 maintenance tick の session 辞書内に限られる。束 A は例外で scheduler を終了させないことを守るが、重い評価の wall 遅延を解消しない。

## 2. 動作目線の設計

### 2.1 人間: worker の死因を親で固定して読む

`PluginSession` は worker を reap する唯一の helper を持つ。helper だけが `os.wait4(pid, WNOHANG|0)` を呼び、同じ critical section で `Popen.returncode`、`worker_signal`、`worker_cpu_sec` を一度だけ保存する。`Popen.poll()`、`wait()`、`communicate()` は session 内で使わず、reaped PID を再 wait しない。stdout reader thread も reap しない。

親専用の状態は次である。

| field | 意味 |
|---|---|
| `worker_returncode` / `worker_signal` | 親が cleanup kill より前に観測した終了状態。未観測は `None` |
| `worker_cpu_sec` | 親 `wait4` の `ru_utime + ru_stime`。未 reap は `None` |
| `parent_kill_sent` | timeout/cleanup の SIGKILL を親が送ったか。signal より先に固定する |
| `worker_unreaped` | kill 後 5 秒でも reap できない PID |
| `error_code` | 内部固定 enum |
| `stderr_tail` | 技術ログ出力直前だけに保持する最大 8 KiB の escaped text |

`_read_response()` は reader queue だけを待たず、短い間隔で queue と `wait4(WNOHANG)` を競合させる。worker が死に、孫だけが stdout を保持しても deadline より先に死因を確定する。deadline 時は最後にもう一度 reap を試し、生存しているときだけ `parent_kill_sent=True` を保存して killpg する。kill 後は最大 5 秒、non-blocking `wait4` だけを再試行する。正常 close を含め、`PluginStrategyIntentSource.close()` は session 参照を消す前に親観測の `cpu_sec`、`worker_returncode`、`worker_signal` を adapter へ取り込む。handler の outer `finally` はこの診断を成功・失敗を問わず activity に渡し、close 応答の worker 自己申告 `cpu_sec` は protocol 互換のため残しても比較・fallback に使わない。

5 秒以内に reap できなければ session は `UNREAPED_CLOSED` となる。CPU は `null`、分類は `crashed`、親側 fd は閉じる。この状態で 2 回目以降の `close()` と `__exit__()` は kill、reap、fd close をせず即 return する。

unreaped `Popen` は session が参照を外さず、モジュール内の上限 64 の強参照 orphan リストへ移す。これにより destructor の cleanup 経路に渡さない。新 session 作成直前とサービス終了時に helper が各 orphan を一度だけ `WNOHANG` で試し、回収できればリストから外して技術ログに 1 行記録する。64 を超えても最古を捨てず、GC cleanup を避けるため保持したまま WARNING を 1 回だけ出す。64 は設定ノブにしない内部定数である。

分類は親の行為と観測値だけで決める。

| 観測 | `SandboxError.code` | 扱い |
|---|---|---|
| kill 後 5 秒の reap 期限超過 | `crashed` | unreaped、`cpu_sec=null` |
| `parent_kill_sent` かつ wall deadline | `timeout` | 親が生存確認後に timeout kill |
| 親 kill なし、`SIGKILL`、CPU が `limit − 0.05 秒` 以上 | `cpu_limit` | worker 寿命の累積 CPU 上限 |
| 親 kill なしの `SIGKILL` で CPU 欠測/閾値未満 | `crashed` | OOM/external kill 等を CPU と断定しない |
| `SIGXFSZ`、その他 signal/exit/EOF 未確定 | `crashed` | 技術ログには事実を残し原因は断定しない |
| 生存 worker の `ok:false` | `plugin_error` | plugin/依存/validation/serialization failure |
| invalid JSON、oversize、read failure | `protocol_error` | IPC protocol failure |

CPU 判定の許容幅は 0.05 秒の内部定数である (設定ノブにしない)。カーネルが `RLIMIT_CPU` を判定する時刻と `wait4` が返す精密な累積 CPU は一致せず、実測 (2026-09-21、設定値 1/2/5/60、計 56 回) では親観測値が上限を最大 26 ms 下回った。許容幅 0 では CPU 上限死のほぼ全部が `crashed` になるため、実測最大の約 2 倍を取る。実装前 harness は実 plugin worker でも同じ測定を行い、不足が 0.05 秒を超えたら分類を実装せず裁定へ戻す。OOM、外部 kill、自発 `SIGKILL` が閾値近くで重なる誤分類、kernel tick/float の丸め、worker が wait していない孫（特に group 外へ逃げた孫）の未計上・未回収は残余リスクとして隠さない。activity/技術ログに親観測 CPU と signal を併記し、人間が再判定できるようにする。

`SandboxError` は互換 constructor `SandboxError(message, *, code="backtest_failed")` を持ち、既存の人間向け message は維持する。runtime lifecycle の分岐は具体 code を必ず指定し、stderr を `__str__` や diagnostic snapshot に含めない。

### 2.2 人間: stderr と core を技術ログだけで扱う

親は `Popen` 前に `TemporaryFile(mode="w+b")` を作り `stderr=` に渡す。所有者は `PluginSession` であり、startup failure、正常 close、timeout、EOF、oversize、明示 close を含む全 path で閉じる。`PIPE` と drain thread は使わない。TemporaryFile が作れないときは `DEVNULL` に限定フォールバックし、評価は続け、技術ログには固定値 `stderr_unavailable=true` を残す。

回収時は末尾最大 8 KiB の bytes を読み、backslash、不正 UTF-8、Unicode `Cc`/`Cf`/`Zl`/`Zp` を可視 escape して 1 log record にする。先頭を落としたときは `truncated=true` を加える。tail が空なら `stderr_tail` 本文を出さない。raw bytes や複数行は logger に渡さない。worker は handshake 後、plugin/依存 import 前に `RLIMIT_CORE=(0,0)` を fail closed で設定する。pipe 型 `core_pattern` 等の host 側 collector が limit 0 をどう扱うかは host policy に依存するため、束 A だけでの完全封鎖は主張しない。

技術ログには固定 prefix、plugin 名、内部 code、親 CPU、returncode/signal、`stderr_unavailable`、escaped `stderr_tail` だけを許す。stderr と、その有無を示す bool は、tool response、transcript、ledger、`last_result`、改善 prompt、report、activity、`SandboxError.__str__` へ出さない。改善 agent が `agentic.log` と `activity.log` を read/listdir できないことを実 worker の Landlock negative test と registry/context inventory の両方で固定する。

### 2.3 改善 agent: 固定公開分類と固定 hint を受ける

`run_backtest_handler` は generic catch より前に `except SandboxError` を置き、`exc.code` だけを写像する。例外文字列、stderr、returncode の substring 判定は禁止する。

| 内部 code | 公開 `error` | hint |
|---|---|---|
| `cpu_limit` | `worker_cpu_limit` | CPU 上限に達した。より粗い timeframe、依存・計算削減、または人間への報告 |
| `timeout` | `worker_timeout` | 評価が時間内に完了しない。計算削減、または人間への報告 |
| `crashed` | `worker_crashed` | worker が異常終了。原因を推測せず人間へ報告 |
| `plugin_error` / `protocol_error` / `backtest_failed` | `backtest_failed` | 候補を確認・修正し、繰り返すなら人間へ報告 |

response に許すのは `error`、固定 `hint`、`started`、既存 wrapper の `remaining_budget` だけである。`no_history_for_symbol` と `pair_not_declared_by_plugin` の既存固定分岐は維持する。tool response、transcript、ledger の `result_summary`、counter は同一の公開 `error` を扱い、成功のみが同 tool の error streak を reset する。

### 2.4 改善 agent: 同じ CPU 失敗は 3 回目から実行しない

`MissionToolCounters` に mission-local の `cpu_limit_observations[(content_hash, pair)]` を追加する。candidate を discover して content hash を得た後、backtest 枠を reserve する前に照会する。

1. 観測数 0 または 1 は handler を実行する。
2. handler が `started:true, error:"worker_cpu_limit"` を返したときだけ、lock 内で +1 する。
3. 観測数 2 は handler を呼ばず、候補枠も消費せず、`started:false, error:"repeated_worker_cpu_limit"` と固定 hint を返す。

この 3 回目拒否だけは専用 finalization で ledger に残す。tool call として `total_calls`、`errors`、refusal streak は増えるが、候補別 `backtest_calls` と CPU 観測数は増えない。既存 3 種の preflight は変えない。`worker_crashed`、`worker_timeout`、`backtest_failed` は対象外であり、content hash または pair が変われば再試行できる。これは mission 終了保証ではない。agent が hash/pair を変えて続行すれば、既存の候補別 6 回枠、budget refusal、refusal/tool-call 上限、runner の待ち loop が最終停止を担い、3 回目拒否だけで `abort_pending` を立てない。

### 2.5 人間: activity で backtest と live を読む

improve backtest は started 後、resource close と分類確定を行う outer `finally` から `backtest_cpu` をちょうど 1 行書く。既存の mission/plugin/scope/pair/deps に `result` と `cpu_source=parent_wait4` を加える。`result` は `ok|plugin_error|cpu_limit|timeout|crashed|backtest_failed` の固定値だけである。取得不能の CPU、returncode、signal は推測せず `null` とする。旧 activity は `cpu_source` がなく worker 自己申告なので、新行の `cpu_sec` と同列比較しない。activity writer failure は本来の結果を変えない。

live producer は `ActivityLog` を composition root から受け、bucket 評価の catch 箇所で `plugin_eval_failed` を記録する。公開 `SandboxError.code` と live `result` の写像は次で固定する。

| 例外 | live `result` |
|---|---|
| `SandboxError(timeout)` | `timeout` |
| `SandboxError(cpu_limit)` | `cpu_limit` |
| `SandboxError(crashed)` | `crashed` |
| `SandboxError(plugin_error)` / `SandboxError(protocol_error)` | `plugin_error` |
| `SandboxError` 以外 | `internal_error` |

抑制 key は `(plugin_name, content_hash, pair, bucket, result)` とする。key ごとに初回、以後 60 回抑制した次の試行（61、121、…回目）だけ activity と warning を出し、再通知に `suppressed_count=60` を載せる。成功、content hash 変更、bucket 放棄で key を解除する。抑制された結果も同じ写像を使う。cursor 不変、同 tick の break、次 tick の新 worker、scheduler 非終了は現状どおりである。live maintenance は同期 hook のままなので、束 A が保証するのは例外で scheduler を終了させないことまでであり、完了する重い plugin の wall 遅延や hook の非同期化は束 B の範囲である。

### 2.6 15m 戦略で実際に見えること

対象戦略を再実行すると、worker は現行どおり累積 60 CPU 秒の hard 到達で終了する。親が kill を送らず、`wait4` CPU が `limit − 0.05 秒` 以上なら `cpu_limit` となる。人間には技術ログの `code=cpu_limit`、signal、実測 CPU、escaped stderr tail と、`backtest_cpu result=cpu_limit cpu_source=parent_wait4` が残る。agent には `worker_cpu_limit` と固定 hint だけが届く。2 回目までは実測し、同じ content hash/pair の 3 回目は `repeated_worker_cpu_limit` で事前拒否する。

## 3. 不変条件

| ID | 不変条件 |
|---|---|
| IV-1 | `sandbox_session_cpu_sec` は worker 寿命の累積 CPU 上限、既定 60、soft==hard を維持する |
| IV-2 | CPU 判定は親 kill なし、`SIGKILL`、親観測 CPU `>= limit − 0.05 秒` の積だけで行う |
| IV-3 | stderr は匿名 file から技術ログへの一方向で、agent 面へ出ない |
| IV-4 | tool/transcript/ledger/counter の公開 error は一致する |
| IV-5 | 欠測値は推測せず `null` にする |
| IV-6 | live failure と activity writer failure は scheduler/本来の結果を変えない |
| IV-7 | 3 回目拒否は実測 CPU limit だけを対象にし、crash/timeout を巻き込まない |
| IV-8 | wall timeout、AS/NOFILE/FSIZE/output/hash/AST、process-group cleanup を弱めない |
| IV-9 | activity/technical log は改善 agent が読めない |
| IV-10 | 既存 `SandboxError` の人間向け message 互換を維持する |
| IV-11 | live の同一失敗は key ごとの初回+間引き再通知にとどめる |
| IV-12 | plugin import 前に `RLIMIT_CORE=(0,0)` を fail closed で設定する |
| IV-13 | kill 後の backtest/live tick/close/`__exit__` は最長 5 秒で戻る |
| IV-14 | session の reaping は helper だけが行い、reaped PID を再 wait しない |
| IV-15 | `UNREAPED_CLOSED` の close/exit は冪等で追加の kill/reap/fd close をしない |
| IV-16 | orphan `Popen` は強参照で保持し、helper 以外の cleanup/GC 経路へ渡さない |
| IV-17 | 新 `backtest_cpu` は `parent_wait4` を明記し、旧 self-report 行と CPU を比較しない |
| IV-18 | orphan リストは内部上限 64 を超えても最古を捨てず、WARNING は 1 回だけ出す |
| IV-19 | raw grid metadata は replay 後にだけ得られ、束 A の planned call 数や CPU 予算式に使わない |

## 4. 受入条件

| ID | 観測可能な受入条件 |
|---|---|
| AC-1 | 正常 close と異常終了の `cpu_sec` はともに親 `wait4` rusage であり、close 応答の偽 CPU は採用しない |
| AC-1a | `PluginStrategyIntentSource.close()` は session 参照を消す前に親観測の CPU/returncode/signal を adapter へ取り込み、outer `finally` の activity が成功・失敗ともその診断を使う。逆変異: session 参照を先に消す。殺すテスト: close 応答の偽 CPU と親 `wait4` 診断を異なる値にした成功/失敗 fixture で、activity が親診断のみを記録することを assert する |
| AC-2 | 全 reap は単一 helper を通り、1 PID を一度だけ wait する。fake process の `wait/poll` と、2 回目の `wait4` を fail にして各 path を検証する |
| AC-2a | reap 不能後の `UNREAPED_CLOSED` は backtest/live/close/`__exit__` から復帰し、二重 close と close→`__exit__` は kill/reap/fd close を追加しない |
| AC-2b | helper reap 後は `Popen` wait 系 API を混在させず、実 Popen の `ECHILD` harness でも returncode を固定する |
| AC-2c | unreaped `Popen` は GC/`_cleanup` に触れず強参照 orphan に残り、後続 session 作成時の helper 試行で回収・技術ログ記録される |
| AC-2d | orphan リストは 64 件を超えても最古を捨てず、WARNING は 1 回だけ出す。逆変異: 65 件目で最古を除去する、又は WARNING を都度出す。殺すテスト: unreaped fake を 65 件登録し、先頭を含む全参照が残ることと WARNING が 1 回だけであることを assert する |
| AC-3 | 孫が stdout を保持しても worker 死亡を timeout より先に観測し、親 kill 未送信・非 timeout に分類する |
| AC-4 | deadline 直前の死亡は元死因、生存時だけ kill-before-reap の timeout となる |
| AC-5 | 設定値 1/5/60 の各 `RLIMIT_CPU` は厳密に `(n, n)` である |
| AC-6 | signal、親 kill、CPU を table 化し、親 kill なし・`SIGKILL`・`limit` 以上だけが `cpu_limit` / `worker_cpu_limit` になる |
| AC-7 | plugin import 前に `RLIMIT_CORE=(0,0)` を設定し、失敗時は import せず fail closed となる |
| AC-8 | `SIGXFSZ` は `worker_crashed` で、CPU と断定しない |
| AC-9 | 大量 stderr でも停止せず、匿名 file は全 lifecycle path で close される |
| AC-10 | 多数 session、TemporaryFile 失敗、孫 fd 継承を bounded に扱い、stderr 取得不能でも評価は継続する |
| AC-11 | stderr tail は 8 KiB 以下の 1 行 escaped text で技術ログだけにあり、agent sink と例外文字列にはない |
| AC-11a | 空の stderr tail は技術ログ本文から省略し、tail の有無 bool も agent 面に出ない。逆変異: 空 `stderr_tail` 又は `has_stderr_tail` を公開 sink に追加する。殺すテスト: 0 B stderr の fixture で技術ログに tail 本文がないこと、tool response/transcript/ledger/activity/例外文字列に両 field がないことを assert する |
| AC-12 | CPU death の agent response は固定 `worker_cpu_limit`、hint、`started:true` だけで、内部診断を含まない |
| AC-13 | timeout/crashed は別 streak、plugin/protocol/general failure と既存 no-history/pair 分岐は既存分類を保つ |
| AC-14 | tool response、transcript、ledger `result_summary`、counter の error 値は同じ公開分類である |
| AC-15 | CPU limit は 2 回 handler 実行後、3 回目を非実行・候補枠非消費で拒否する |
| AC-16 | 3 回目拒否は ledger に記録し、既存 3 preflight の ledger/counter 挙動は変えない |
| AC-17 | content hash または pair が変われば同名でも handler を再実行する |
| AC-18 | `worker_crashed` を 3 回返しても CPU 観測数は 0、handler は 3 回実行される |
| AC-19 | started 後の `ok/plugin_error/cpu_limit/timeout/crashed/backtest_failed` は各 1 行の `backtest_cpu` と `cpu_source=parent_wait4` を残す |
| AC-20 | started 前失敗は `backtest_cpu` を書かない |
| AC-21 | activity writer 例外は 6 outcome の公開結果を変えない |
| AC-22 | live の全分類を parameterize し、cursor 不変・同 tick break・次 tick 新 PID と、1/61/121 回目だけの通知を確認する |
| AC-23 | live の全分類で、成功/hash 変更/bucket 放棄による key 解除後の次失敗は初回通知となる |
| AC-24 | 実 improve worker は `logs/agentic.log` と `logs/activity.log` の open/listdir を `EACCES` で拒否する |
| AC-25 | registry/context に両 log を読む tool/path/content がない |
| AC-26a | config 省略時も明示 60 時も handshake は 60 |
| AC-26b | 親診断から公開分類までの短縮 15m 相当 E2E は `worker_cpu_limit` となり、内部診断を漏らさない |
| AC-27 | runbook は `backtest_cpu`、`plugin_eval_failed`、技術ログの死因行、`cpu_source` の非比較性、CPU 上限時の人間の選択肢と運用注意を明記する |

## 5. 実装 task

```text
T1 worker 観測境界 ─┬→ T2 improve 公開分類・backtest activity ─┬→ T4 CPU 再試行拒否 ─┐
                    └→ T3 live failure activity                   ├→ T6 統合検収
T5 運用 runbook ────────────────────────────────────────────────────┘
```

| Task | 内容 | 主な AC |
|---|---|---|
| T1 | `sandbox.py`/`worker.py` の単一 wait4、typed error、core、stderr、unreaped/orphan lifecycle。開始前に monkeypatch/fake/constructor 呼出しを全数 inventory する | AC-1〜11、26a |
| T2 | improve の公開分類、全 outcome の `backtest_cpu`、sink 漏洩防止。開始前に intent source/activity の fake 契約を inventory する | AC-12〜14、19〜21 |
| T3 | live failure activity、完全な result 写像、抑制・解除、service 配線。開始前に producer/activity callback の呼出しを inventory する | AC-22、23 |
| T4 | mission-local CPU 観測と専用 finalization。開始前に counter/reserve/ledger の早期 return を inventory する | AC-15〜18 |
| T5 | `docs/operations/backtest-failure-readable.md` を新設し、人間が見る activity/技術ログ、`cpu_source` の新旧非比較、`worker_cpu_limit` 時の選択肢を記載する。上限を上げる場合は再起動が必要で live にも効くと明記する | AC-27 |
| T6 | response/transcript/ledger/last_result/prompt/report/activity の漏洩否定、Landlock/inventory、短縮 E2E、全逆変異の統合検収 | AC-11〜14、24〜26b |

### 5.1 実装前に測る項目

T1 の最初の成果物として測り、結果を実装報告に残す。項目 1 で `cpu_sec < limit` の実測が出た場合は実装を止め、`cpu_limit` 判定の裁定へ戻す。

pytest や実サービスではなく、実装 task の最初に小さい process/fake-process harness で境界値を確認する。

1. 対象 Linux/Python の `wait4` が正常終了・`RLIMIT_CPU`・親killで返す status/rusage と、設定値
   1/5/60 で CPU kill の `limit − cpu_sec` が 0.05 秒以内に収まること (純 Python の busy loop では最大 26 ms を実測済)。実 plugin worker で超える実測が出たら分類裁定へ戻す。
2. `Popen` を親が `wait4` で reap した後に `returncode` を設定したとき、destructor/closeで二重waitや
   ResourceWarningがないこと。実Popenで `ECHILD`、returncode固定、reaped後のPID再waitなしも確認する。EOFより先に死亡、孫がstdout保持、deadline直前死亡もprobeする。
3. 匿名 stderr file に 0 B、8 KiB 境界、8 MiB 超を書いたときの回収時間、`SIGXFSZ`、fd close。
4. backslash、Unicode `Cc/Cf/Zl/Zp`、不正 UTF-8、長い prompt 命令を含む tail の escape 結果が
   1 log record か。
5. timeout/EOF/oversize/startup failure/normal close の各 path で temporary fd と worker/process group が残らないか。
6. tool response→registry counter→ledger→transcript→`last_result`/prompt/report/activity を fixture 上で検索し、
   stderr marker と内部 diagnostic が 0 件か。
7. live の失敗 activity 追加自体の時間、間引き/解除/抑制件数と、activity write failure が
   producer/scheduler へ伝播しないか。
8. 多数session時の親fd/inode、TemporaryFile失敗、group内外の孫によるstderr fd継承。group外の孫は
   回収不能という残余リスクを監視値と既存NOFILE/FSIZE上限込みで記録する。
9. kill後 `wait4(WNOHANG)` が常に0となるD-state模擬で、5秒期限後に backtest / live tick / close / `__exit__` が
   復帰し、unreaped PIDへの後続waitがないこと。

## 6. 影響ファイル

| ファイル | 変更 |
|---|---|
| `src/agentic_fx/plugin/sandbox.py` | wait4/reap、parent kill、typed error、匿名 stderr、technical log、orphan 管理 |
| `src/agentic_fx/plugin/worker.py` | soft==hard 維持、plugin import 前の CORE 無効化 |
| `src/agentic_fx/loops/improve_loop.py` | 固定公開分類、全 outcome activity、`cpu_source` |
| `src/agentic_fx/tools/mission_counters.py` | CPU observation map |
| `src/agentic_fx/tools/improve_rpc_tools.py` | reserve 前の 3 回目拒否、ledger 整合 |
| `src/agentic_fx/plugin/signal_producer.py` | live activity、result 写像、抑制/解除 |
| `src/agentic_fx/service.py` | `SignalProducer` への `ActivityLog` 注入、サービス終了時 orphan 回収 |
| `docs/operations/backtest-failure-readable.md` | 人間向け failure runbook（AC-27） |
| `tests/plugin/test_sandbox.py` | AC-1〜11、26a |
| `tests/loops/test_improve_loop_rpc_handlers.py` | AC-12、13、19〜21 |
| `tests/tools/test_mission_counters.py` / `tests/tools/test_improve_rpc_tools.py` | AC-13〜18 |
| `tests/plugin/test_signal_producer.py` / `tests/test_service_app.py` | AC-22、23 と production 配線 |
| `tests/integration/test_improve_worker_permission_boundary.py` | AC-24 |
| `tests/loops/test_improve_e2e.py` / `tests/loops/test_improve_context.py` | AC-11〜14、25、26b |

## 7. 退けた案

1. **束 A で CPU 上限を上げる**: 15m を完走させても、安全天井と worker 寿命累積という意味を変えるため採用しない。
2. **soft<hard と `SIGXCPU`**: plugin が捕捉/無視でき、強制停止時刻を変えるため採用しない。
3. **すべての `SIGKILL` を CPU 扱い**: OOM/external kill を巻き込むため採用しない。
4. **stderr を PIPE、例外、tool、activity に載せる**: backpressure と prompt injection 面を作るため採用しない。
5. **`worker_crashed` も 3 回目拒否する / 即 mission abort する**: 一過性失敗を恒久扱いにし、修正余地を失わせるため採用しない。

## 8. 人間の裁定が要る点

### 承認済み

- 束は、可読化・分類・安全な再試行抑制を先行する束 A と、CPU 予算式・設定移行等を扱う束 B に分割する。依存は A から B への一方向である。
- 束 B は CPU 予算を二層にする方向で設計する: 「1 回の評価の瞬間上限」と「1 回あたりの平均予算 × 親が計算した予定評価回数 + 起動枠 = worker 寿命の予算」。瞬間上限 × 回数だけの単層は、細かい足で上限が数時間になり停止保証にならないので退けた。

### この設計書の提示で確認する点

- `cpu_limit` は、親が観測した worker 寿命累積 CPU が `limit − 0.05 秒` 以上、親 kill なし、`SIGKILL` の三条件でだけ判定する。許容幅は 0.05 秒 (内部定数、実測最大 26 ms の約 2 倍) である。当初案の許容幅 0 は実測で不成立と分かったため改めた。
- 三条件の確証がないものは `crashed` とする。CPU とは断定しない。
- 同一失敗の事前打ち切りは `cpu_limit` だけに適用し、同一 content hash/pair の 3 回目からに限る。crash、timeout、一般 failure は対象外である。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-21 | v0.1 | 可読化と予算意味変更を一文書で提案 | 初期検討 | — |
| 2026-09-21 | v0.2 | 可読化を先行する束へ分割し、typed 分類・stderr 隔離・activity・再試行抑制を定義 | 安全上限と予算設計を分離 | — |
| 2026-09-21 | v0.3 | 親 wait4、soft==hard、core 無効化、死亡監視、sink 隔離、live 抑制を追加 | 観測の信頼境界と運用安全性を明確化 | — |
| 2026-09-21 | v0.4 | reap 期限、許容幅 0、CPU source、preflight 規律、live 通知、実 Popen harness を明確化 | 境界条件と検証可能性を固定 | — |
| 2026-09-21 | v1.0 | orphan 強参照・冪等終端状態・live 完全写像・runbook を反映して公開向けに清書 | 最終裁定を全設計要素へ反映 | `630edc8` |
| 2026-09-21 | v1.1 | 清書時に圧縮で落ちた8契約と対応ACを復元 | 清書時の圧縮で落ちた契約の復元 | `d95990d` |
| 2026-09-21 | v1.2 | `cpu_limit` 判定の許容幅を 0 → 0.05 秒 (内部定数) | 実測: RLIMIT_CPU の kill 時、親が観測する累積 CPU は上限を最大 26 ms 下回る | (本 commit) |
