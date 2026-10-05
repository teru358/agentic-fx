# [plugin-worker-landlock] plugin worker 隔離設計 v1.3

版: v1.3

日付: 2026-10-04

対象チケット: `candidate-strategy-backtest-runs-without-landlock` (高)

対象: `PluginSession` が起動する `python -B -m agentic_fx.plugin.worker <plugin_dir>`

## 要点

- 親は全 `PluginSession` に共通する admission gate で runtime fingerprint を判定し、`Popen` 直前に現在 thread の継承 seccomp filter を検査する。安全に起動できない場合は plugin を一切読まず `sandbox_unavailable` にする。
- worker は rlimit、継承 filter の再検査、匿名 keyring、Landlock、seccomp、runtime import 自己試験を一続きで完了してから `sandbox_ready` を返す。親が attestation と `/proc` を検証して `load` を送るまで plugin source を読まない。
- Landlock は読み取り専用の最小 FS 規則、seccomp は `allow/6` の allowlist とする。未知 syscall と allowlist 外 syscall は `KILL_PROCESS`、glibc fallback として立証済みの `clone3` だけ ENOSYS とする。
- plugin は検査済み dir fd から `plugin.py` と `config.yaml` だけを読み、hash を照合した同じ bytes を compile/exec する。pyc と追加 payload は読まない。
- 対応済み fingerprint は 2026-10-03 に正常 workload を完走した Landlock ABI 8 の 1 組だけである。ABI 3〜7 を含む集合外 fingerprint は代表自己試験に成功した場合だけ WARNING 付きで続行する。
- `sandbox_unavailable` は候補の責任にしない。`load` 後の SIGSYS は禁止 syscall と同 uid 外部 signal を区別できないため、原因未確定の `crashed / sigsys_unattributed` とする。

## 1. 目的、信頼境界、範囲

### 1.1 目的

plugin worker 内のコードに対して、OS が次を強制する。

1. 履歴 DB、台帳、設定、鍵、ログ、他 plugin、他 mission の一時領域を読ませず、どこにも書かせない。
2. process を作らせず、pathname、fd、memfd のいずれからも別の実行イメージへ移らせない。
3. MT5 bridge、llama-swap、D-Bus、将来の操作 API を含む socket 到達を許さない。
4. 親の session/user keyring の鍵を読ませず、keyring の変更や `request_key` helper の起動を許さない。
5. 隔離を準備・検証できない場合は plugin を 1 行も動かさず、全呼び出し元で同じ固定分類にする。

AST の `check_source` は善意の事故防止として残すが、隔離の主防御にはしない。plugin は Python/ctypes を使って AST 検査を迂回できるものとして扱う。

### 1.2 信頼境界

信頼済み runtime は uv 管理の CPython、repo の `.venv`、`src/agentic_fx`、`sysconfig` が返す runtime path である。これらは interpreter と隔離コードを動かすため sandbox 適用前にも読まれ、改竄されていれば本設計は保証しない。

**親が `load` を送った後に worker から届く行は、すべて候補が制御し得る入力として扱う。** worker は protocol 用の fd を持ったまま同じ process で plugin を実行するので、plugin はその fd へ直接書ける (要求行を先に読むこともできる)。`sandbox_ready` は plugin が 1 行も動く前に検証を終えるので偽造できないが、それより後の `plugin_ready`、call の応答、error 文字列、申告 pid は、worker の信頼コードを経由した保証を持たない。安全は親の検証だけで成立させ、worker 側の検証に依存しない (§2.5、LL-29)。supervisor process と plugin 実行 process の分離は採らない (§12)。

同 uid の外部 process は worker へ signal を送れるため信頼境界外である。外部 SIGSYS は防御対象外だが、候補の syscall と誤帰責しない。

### 1.3 対象経路

|経路|実行するコード|扱い|
|---|---|---|
|改善 mission の run_backtest、holdout、承認回廊|承認前 strategy と承認済み indicator|共通 `PluginSession` admission、二段 protocol、束 A v1.5 の公開分類|
|live signal / strategy 評価|配備済み plugin|隔離不能ならその tick の評価を行わず、scheduler は継続|
|trade loop の indicator tool|配備済み indicator|固定 tool error|
|人間 CLI `backtest run --plugin`、`signal_eval`|discover 済み plugin|共通 admission を lazy 実行し、失敗時 rc=1|
|service|上記経路の composition root|起動時に admission を eager 実行し、結果を fingerprint 単位で cache|

### 1.4 対応外

- gate pytest worker、improve の CLI backend、trade profile worker、操作 API 本体。gate pytest は `PluginSession` を通らない別 worker (`gate_pytest_worker.py`) で、その隔離 profile は T5 とチケット `gate-pytest-dev-writable-and-ldso-exec` で扱い、本件の admission gate の対象に含めない。
- 別 uid、namespace、AppArmor による追加隔離。
- `RLIMIT_NPROC` の意味の見直し。process 生成は seccomp で止め、NPROC は別チケットでスレッド上限として扱う。
- system Python、pyenv、conda、musl の正式対応。集合外 fingerprint として best-effort 自己試験へ送る。

## 2. 起動とデータフロー

### 2.1 全 `PluginSession` 共通 admission gate

`PluginSession.__enter__` は呼び出し元 (service の live、improve の backtest、CLI) の別なく、次を共通 gate として通す。gate pytest worker は `PluginSession` を通らないため対象外とする (§1.4)。

1. 正規化した runtime fingerprint を作る (§6)。
2. 対応集合内なら admission 成功とする。集合外なら専用 harness で代表自己試験を 1 回行い、成功時だけ WARNING 付きで許可する。失敗は `sandbox_unavailable / runtime_fingerprint_selftest_failed` とする。
3. 結果を fingerprint 単位で process 内 cache する。service は eager、その他の入口は最初の session で lazy に実行する。自己試験 harness は `PluginSession` を再帰的に呼ばず、同じ Landlock/seccomp profile を直接適用する。
4. x86_64、Landlock ABI 3 以上、seccomp TSYNC/LOG の利用可否を測り、固定 reason と期待 attestation を作る。
5. **`Popen` の直前**に現在 thread の `/proc/thread-self/status` を読む。`Seccomp_filters` field がない、読めない、または 0 でない場合は Popen を呼ばず `sandbox_unavailable / inherited_seccomp_filter` とする。なお `Seccomp_filters` 欄の無い kernel は host preflight が先に `seccomp_status_field_unavailable` で検知するので、通常この field 不在には至らない (多層防御として残す)。

親 preflight と worker 再検査の両方を必須にする。親 preflight は CPython 起動前に固定 reason を保証し、worker 再検査は起動経路の取り違えに対する多層防御になる。

### 2.2 worker 内の順序

worker は次の順序を変えない。

1. `sys.dont_write_bytecode = True` を設定する (親も `-B` で起動する。sandbox 下の遅延 import が `__pycache__` を作ろうとすると `mkdir` が allowlist 外で worker が SIGSYS 死するため、bytecode の書き出しそのものを止める)。stdout を protocol 用に退避し、fd 1 を stderr へ向ける。rlimit 前に全固定失敗行を bytes 化し、隔離段が使う module、traceback、linecache、tokenize を import する。
2. handshake を読み、main/indicator の `content_hash` と 128 bit の `attest_nonce` を得る。
3. CPU、CORE、AS、NOFILE、FSIZE、NPROC の rlimit を設定する。失敗は `rlimit_failed`。
4. `/proc/self/status` の `Seccomp_filters` を読み、field 不在・読取不能・非 0 を `inherited_seccomp_filter` で拒否する。
5. Landlock ABI を測り、匿名 session keyring へ切り替える。ABI 3〜7 では Landlock 適用前の task 数が 1 であることを検査する。
6. runtime、system、plugin file の allowlist を検査済み fd から組み、guarded root 自己検査を行う。
7. Landlock を適用する。ABI 8 以上では `LANDLOCK_RESTRICT_SELF_TSYNC`、ABI 3〜7 では flags 0 と事前の single-task 検査を使う。TSYNC 失敗時に flags 0 へ落とさない。
8. seccomp `allow/6` を `SECCOMP_FILTER_FLAG_TSYNC | SECCOMP_FILTER_FLAG_LOG` で適用する。
9. 規則用 fd を閉じ、plugin dir fd だけを loader 用に保持する。
10. sandbox 下で `numpy`、`pandas`、`agentic_fx.core.plugin_contract` を import する。失敗は `runtime_import_failed`。
11. `sandbox_ready` を書き、親の `{"op":"load"}` を待つ。EOF や別 op なら plugin を読まず終了する。
12. socket module の既存 poison を多層防御として適用し、source-only loader で main と indicator を読み、`plugin_ready` を返す。

隔離段の既知・未知の失敗は保持 fd を閉じ、traceback と allowlist 要約を stderr に書き、事前生成した `sandbox_ready ok:false` を `os.write` で出して `os._exit(0)` する。AS が極小でも JSON 整形や遅延 import に依存しない。

### 2.3 二段 protocol と attestation

|順|向き|message|
|---|---|---|
|1|親 → worker|handshake (`attest_nonce`、main/indicator `content_hash` を含む)|
|2|worker → 親|`sandbox_ready`。成功は attestation、失敗は `ok:false, stage:"sandbox", reason`|
|3|親 → worker|検証成功時だけ `{"op":"load"}`|
|4|worker → 親|`plugin_ready`。import/validation 失敗は `ok:false`|

成功 `sandbox_ready` の envelope は `phase="sandbox_ready"`、`ok=true`、`pid=Popen.pid` の 3 field、attested field は次の 8 fieldとする。余分、欠落、順序違い、不一致を許さない。

|field|期待値|
|---|---|
|`sandbox_profile_version`|`plugin-worker/6`|
|`landlock_fs_abi`|親が測った ABI|
|`landlock_tsync`|ABI 8 以上は `applied`、ABI 3〜7 は `single_task`|
|`seccomp`|`allow/6`|
|`keyring`|`anonymous`|
|`network`|ABI 4 以上は `applied`、それ未満は `unsupported`|
|`scope`|ABI 6 以上は `applied`、それ未満は `unsupported`|
|`nonce`|handshake の値|

親は envelope/attested field の完全一致と、`Popen.pid` 配下の全 task の `/proc/<pid>/task/*/status` が `NoNewPrivs: 1`、`Seccomp: 2`、`Seccomp_filters: 1` であることを検査する。継承 filter を事前拒否するため `N+1` は使わず 1 固定とする。`/proc` 検査の対象は常に `Popen.pid` で、worker の `pid` 申告は使わない。

検査は次の順で行い、どれかで止まれば `load` を送らず kill して `sandbox_unavailable` とする。ただし順 3 の `ok:false` のうち候補由来の reason (`plugin_file_invalid`・`allowlist_not_leaf`・`allowlist_guarded`) は `plugin_error` とする (§5.1)。複数の違反が同時にあっても、先に検査される項目の reason に固定される。

|順|検査|reason|
|---|---|---|
|1|1 行目が `phase` を持たない ready (旧 worker)|`attestation_missing`|
|2|1 行目の `phase` が `plugin_ready`、または `sandbox_ready` 以外|`attestation_order`、`attestation_unexpected_message`|
|3|`ok:false`|worker の reason が `inherited_seccomp_filter` なら同名、それ以外は `sandbox_setup_failed:<reason>` (enum 外は `unknown`)|
|4|envelope と attested のどちらにも無い field、次に `pid` (無い、`Popen.pid` と違う)、次に attested field (表の順、`nonce` が最後)|`attestation_unexpected_field`、`attestation_missing:pid`、`attestation_mismatch:pid`、`attestation_missing:<field>`、`attestation_mismatch:<field>`|
|5|`sandbox_ready` の後に既に次の行が読み取りバッファにある|`attestation_unexpected_message`|
|6|全 task の `/proc/<pid>/task/*/status` (読めない場合を含む)|`proc_status_mismatch:<NoNewPrivs\|Seccomp\|Seccomp_filters>`、`proc_status_unreadable`|
|7|4 行目の `phase` が `plugin_ready` でなく `ok:true`|`attestation_order`。`ok:false` は `plugin_error` (候補の import 失敗)|

旧 worker の一段 ready は受けず、protocol の後方互換を持たない。Landlock domain は `/proc` に出ないので、適用の独立確認は結合テストが担う。

起動 deadline は handshake 送信直前に 1 回だけ作り、`sandbox_ready` 読取、検証、`load` 送信、`plugin_ready` 読取で共有する。検証完了時に期限切れなら `load` を送らず kill する。

### 2.4 source-only loader

main と各 indicator は次だけで読む。

1. 検査に使った plugin dir fd から `plugin.py` と `config.yaml` を `openat(O_RDONLY|O_NOFOLLOW|O_CLOEXEC)` で開き、通常ファイルであることを確認する。
2. 共通 `_MAX_FILE_BYTES` (= 1 MiB) に対し最大 `_MAX_FILE_BYTES + 1` bytes だけ読む。超過は `plugin_error / file_too_large`。親の `content_hash()` も同じ有界 reader を使う。
3. `content_hash_bytes(plugin_bytes, config_bytes)` が handshake と一致することを確認する。
4. 同じ bytes を UTF-8 decode し (失敗は `plugin_error`)、`compile(plugin_bytes, "<dir の実体パス>/plugin.py", "exec", dont_inherit=True)` する。`dont_inherit` で worker 自身の `from __future__` を持ち込まない。
5. exec の前に、decode した行を `linecache.cache[<実体パス>] = (len(text), None, text.splitlines(True), <実体パス>)` に登録する。mtime を `None` にして `linecache.checkcache` が stat して捨てないようにし、traceback の表示をディスクの再読みに頼らせない。
6. `types.ModuleType(<一意な名前>)` を作り、`__file__` を上の実体パス、`__spec__` を `ModuleSpec(name, loader=None, origin=__file__)` (`has_location=True`)、`__loader__` を `None`、`__package__` を `""`、`__cached__` を `None` にして exec する。名前は main が `plugin`、indicator が `indicator_<alias>` とし、`sys.modules` には登録しない。相対 import は AST 検査で禁止のままである。

`importlib` の file loader は使わない。読取上限超過、hash 不一致、decode 失敗、規則追加後の rename による `openat` の EACCES、exec 失敗はすべて起動失敗の `plugin_error` で、候補側の事象とする。exec 失敗時は module を捨てる (どこにも登録しないので掃除は不要)。

plugin dir 自体に read 規則を張らないため、pyc、`test_plugin.py`、追加 payload、dir listing は使えない。規則追加後の rename で別 inode に差し替えられた名前も EACCES になる。

### 2.5 `load` 後の応答の検証 (親)

親は `load` 送信後の全ての行を次の規則で扱う。違反は protocol 違反として session を kill + reap し、候補側の失敗に分類する。

1. 行長の上限を超える行、JSON として読めない行、読み取りが例外になる行 (桁数の多すぎる整数、深すぎる入れ子を含む)、`NaN`・`Infinity`、重複キー、object でない値は拒否する。どの例外も session の後始末を飛ばさない。
2. 要求ごとに親が乱数の request id を入れ、応答に同じ id が無ければ拒否する。要求を送る前に受信済みの bytes が残っていれば拒否する。1 要求に 2 行以上届けば拒否する。id は plugin からも観測できるので出所の証明ではなく、ずれと先回りを排除するためのものである。
3. worker の終了を観測した後に残っていた行は採用しない。ただし `load` への正規の 1 応答である `plugin_ready` だけは例外で、worker が ok:false を書いて即座に死に、親が遅れて死を先に観測したときも、その 1 行は読んで分類する (候補の import 失敗 `plugin_error` を `crashed` に化けさせない)。drain で読んだ `plugin_ready` が ok:true なら `load` 後の死として `crashed` にする。call 応答 (`plugin_ready` より後の行) はこの例外に含めない。
4. 応答の中身は、要求の種類ごとに親が意味まで検証する (indicator は系列長と index を親が渡した frame と照合、数値は有限で float に表せること、signal・strategy は既存の contract)。worker 側の検証と同じかそれ以上を親が行う。
5. 申告 pid は `Popen.pid` と照合するだけで保持しない。error 文字列と未知 field は、ログ・activity・CLI に出す前に長さ上限と制御文字の除去を通す。改善 agent への tool 応答と承認 payload には worker 由来の文字列を入れない。
6. `close()` は例外を出さず、必ず kill・reap・fd の close まで行う。

親の検証を通る値を正しい request id つきで plugin が直接書いた場合は、plugin の通常の戻り値と区別しない (plugin は同じ値を戻り値として返せる)。CPU 超過・crash・SIGSYS・timeout の分類と CPU の値は親の wait4 と時計で決まり、行の内容では変わらない。

## 3. Landlock / FS allowlist

### 3.1 最終規則

|種別|対象|許可・条件|
|---|---|---|
|runtime dir|`src/agentic_fx`|worker の `__file__` から導出。repo の `src/` 全体は許さない|
|runtime dir|`sysconfig.get_paths()` の `stdlib`、`platstdlib`、`purelib`、`platlib` と実体の `lib-dynload`|子孫を親へ畳む。`/`、`/usr`、`/usr/local`、`/opt`、`/var`、`/home`、`$HOME`、repo root の一致・祖先は `runtime_root_too_wide`|
|plugin file|main と依存 indicator の各 `plugin.py`、`config.yaml`|検査済み dir fd から `O_PATH\|O_NOFOLLOW` で開いた通常ファイル。`READ_FILE` だけ|
|system dir|`/usr/lib`、実体が別なら `/usr/lib64`・`/lib64`|読み取りだけ。distro 配布物以外の秘密を置かないことが運用前提|
|system dir|`/usr/share/zoneinfo`|読み取りだけ|
|device file|`/dev/urandom`|読み取りだけ。`/dev` dir は許さない|
|write|なし|作成、truncate、rename、metadata 変更を許さない|
|execute|なし|EXECUTE は handled、規則は 0。seccomp も exec/memfd を拒否|
|network (ABI 4+)|`BIND_TCP`、`CONNECT_TCP` を handled|規則 0。TCP を全拒否|
|scope (ABI 6+)|`ABSTRACT_UNIX_SOCKET`、`SIGNAL`|domain 外への abstract UDS と signal を拒否|

除外対象は `data/`、`config/`、`logs/`、`~/.config`、repo root、`plugins/` root、`plugins/_staging` root、他 plugin、`$HOME`、`/tmp`、`/etc`、`/proc`、`/sys`、`/dev` dir、`/run` である。plugin leaf だけは `/tmp` 配下を許せるが、その leaf の 2 ファイル以外は読めない。

`/usr/lib` を dir で許すのは確定した運用判断である。numpy/pandas/lib-dynload の `.so` 69 本から DT_NEEDED 閉包を求めると subtree 外は 7 本、filter 後に実際に開いたのは `libgcc_s`、`libstdc++`、`libz` の 3 本だったが、起動ごとの閉包計算は 30〜66 ms で +20 ms の性能目標を超えた。実行権は与えず、正式対応環境を広げるときだけ親が一度計算する file 規則を再検討する。

### 3.2 allowlist 自己検査

|種別|検査|
|---|---|
|全拒否|`data/`、`config/`、`logs/`、`~/.config` の祖先・一致・子孫を拒否|
|覆い拒否|repo root、`plugins/` root、staging root、`$HOME`、`/tmp`、`/` の祖先・一致を拒否|
|runtime/system|親自身が導出した固定値だけを受け、`/tmp` 子孫と広すぎる root を拒否|
|plugin|親が選んだ leaf だけを受け、直下の `plugin.py` を要求。dir には規則を張らない|
|plugin file|`openat(O_PATH\|O_NOFOLLOW\|O_CLOEXEC)` + `fstat` で通常ファイルを確認|
|device|`/dev/urandom` 1 本が文字デバイスであることを確認|

検査、`landlock_add_rule`、source 読取は同じ fd 系統を使い、path の再 open によるすり替え窓を作らない。

## 4. seccomp `allow/6`

### 4.1 評価順

|段|条件|結果|
|---|---|---|
|1|arch が x86_64 でない、または x32 (`nr >= 0x40000000`)|`KILL_PROCESS`|
|2|`nr >= 471` (番号表外)|`KILL_PROCESS`。default と別の return 命令へ飛ばす|
|3|無条件 allow 表|`ALLOW`|
|4|`socket`(41)、`io_uring_setup`(425)|`ERRNO(EACCES)`|
|5|`fork`(57)、`vfork`(58)、`execve`(59)、`memfd_create`(319)、`execveat`(322)|`ERRNO(EPERM)`|
|5b|`clone3`(435)|`ERRNO(ENOSYS)`。唯一の立証済み fallback|
|6|引数条件つき表|条件内は `ALLOW`、外は表の errno|
|7|その他すべて|`KILL_PROCESS`|

### 4.2 無条件 allow

|syscall (x86_64 番号)|出所|許可理由|
|---|---|---|
|`read`(0)、`write`(1)、`close`(3)、`lseek`(8)、`pread64`(17)|実測|継承 pipe/匿名 file と許可済み read-only fd|
|`openat`(257)、`getdents64`(217)|実測|到達先は Landlock が判定|
|`stat`(4)、`fstat`(5)、`lstat`(6)、`newfstatat`(262)|実測|import 探索。allowlist 外 path の metadata 可視性は残余|
|`mmap`(9)、`mprotect`(10)、`munmap`(11)、`brk`(12)|実測|メモリ確保と共有ライブラリ読込|
|`mbind`(237)|実測|numpy allocator の自 process NUMA 方針|
|`futex`(202)|実測|自メモリの lock|
|`rt_sigaction`(13)|実測|Python signal handler|
|`sched_getaffinity`(204)|実測|CPU 数判定|
|`getrusage`(98)|実測|worker の自己 CPU 観測|
|`uname`(63)|実測|`platform`|
|`epoll_create1`(291)|実測|runtime import|
|`exit_group`(231)|実測|process 終了|
|`exit`(60)、`gettid`(186)、`rseq`(334)、`set_robust_list`(273)、`rt_sigprocmask`(14)、`madvise`(28)|threading 実測|thread の開始・終了|
|`rt_sigreturn`(15)、`restart_syscall`(219)|明示|signal 復帰と syscall 再開|
|`getpid`(39)|明示|自 pid|
|`clock_gettime`(228)、`clock_getres`(229)、`gettimeofday`(96)、`time`(201)|明示|vDSO fallback|
|`clock_nanosleep`(230)、`nanosleep`(35)|明示|wall timeout 内の sleep|
|`sched_yield`(24)|明示|BLAS の譲り|
|`getrandom`(318)|明示|乱数 seed|
|`mremap`(25)|明示|自メモリ realloc|

`ioctl` と `clone` は実測に現れるが、無条件には許さず次表で絞る。`getppid`(110)、`getuid`(102)、`geteuid`(107)、`getgid`(104)、`getegid`(108)、`getpgrp`(111)、`times`(100)、`alarm`(37)、`sysinfo`(99) は許可しない。これらを呼んだ worker は誤値で続行せず SIGSYS で終了する。

### 4.3 引数条件つき allow

|syscall|許す条件|表外|理由|
|---|---|---|---|
|`clone`(56)|下位 32 bit に `CLONE_THREAD\|CLONE_VM\|CLONE_SIGHAND` がすべてあり、`CLONE_NEW*` がない|EPERM|thread だけを許可|
|`kill`(62)|pid が filter 生成時の自 pid、signal が SIGSYS(31) でない|EPERM|自 process の通常 signal だけ|
|`tgkill`(234)|tgid が自 pid、signal が SIGSYS(31) でない|EPERM|自 thread group の通常 signal だけ|
|`prlimit64`(302)|pid が 0 または自 pid|EPERM|自 process の rlimit 参照・引下げだけ|
|`fcntl`(72)|`F_GETFD`、`F_SETFD`、`F_GETFL`、`F_SETFL`、`F_DUPFD_CLOEXEC`|EPERM|`F_SETOWN*`、`F_SETSIG`、`F_SETLEASE`、`F_NOTIFY`、lock 系を拒否|
|`ioctl`(16)|`TCGETS`、`TCGETS2`、`TIOCGWINSZ`|ENOTTY|端末属性の読取だけ。`FIOSETOWN`、`SIOCSPGRP`、`FS_IOC_SETFLAGS`、`FS_IOC_FSSETXATTR` を拒否|

filter は 100 命令で、kernel 上限 4096 命令以内とする。命令列は syscall/条件のデータ表から生成し、手書き分岐にしない。

### 4.4 fallback と許可 syscall の残余

|syscall|KILL に戻す差分|決定|
|---|---|---|
|`clone3`(435)|OpenBLAS 4 thread と `threading` が SIGSYS で失敗し、ENOSYS では `clone` へ切り替わって完走|ENOSYS fallback|
|`statx`、`faccessat2`、`fchmodat2`、その他の未観測 fallback 候補|正常 workload では filter 後に未観測|先回りで入れない|

|許可 syscall|残る能力|許可理由|
|---|---|---|
|`openat`、`getdents64`|file 内容、dir 列挙|Landlock が到達先を判定|
|`stat`、`lstat`、`newfstatat`、`fstat`|外部 path の存在、size、mtime|import 探索に必須。内容は読めない|
|`mmap`、`mprotect(PROT_EXEC)`|write+execute memory|plugin は ctypes を含む任意 Python を実行可能という前提で能力を増やさず、共有 library 読込に必須|
|`write`|保持 fd への書込み、protocol 応答の偽装|自 session の結果を偽るだけで新しい境界を越えない|
|`sched_getaffinity`|他 process の affinity 読取|読取だけ。BLAS build 互換性に必要|
|`uname`|kernel version|秘密ではなく runtime が利用|
|`futex`|shared futex|共有 object を作れず、自 memory に閉じる|
|thread `clone`|thread 増加|CPU は process RLIMIT と親 wait4 に計上|
|self `kill` / `tgkill`|自己停止|SIGSYS は除外し、終了は自 session に閉じる|
|self `prlimit64`|自上限の引下げ|hard 上限は上げられない|
|`fcntl(F_SETFL)`|`O_ASYNC`|owner 設定を拒否するため SIGIO の送信先を作れない|
|`getrandom`、`clock_nanosleep`|entropy、sleep|副作用なし、または wall deadline 内|

### 4.5 明示的に許可しない類

|類|代表 syscall|結果|
|---|---|---|
|System V IPC|`shmget`、`shmat`、`shmctl`、`shmdt`、`semget`、`semop`、`semtimedop`、`semctl`、`msgget`、`msgsnd`、`msgrcv`、`msgctl`|default KILL|
|POSIX mq|`mq_open`、`mq_unlink`、`mq_timedsend`、`mq_timedreceive`、`mq_notify`、`mq_getsetattr`|default KILL|
|keyring|`add_key`、`request_key`、`keyctl`|default KILL|
|他 process/signal|`tkill`、`rt_sigqueueinfo`、`rt_tgsigqueueinfo`、`pidfd_open`、`pidfd_getfd`、`pidfd_send_signal`、`setpriority`、`sched_setaffinity`、`sched_setscheduler`、`ioprio_set`、`migrate_pages`、`move_pages`、`kcmp`|default KILL|
|metadata|`chmod`/`chown` 系、`utime*`、`*xattr`、`fchmodat2`、`file_setattr`|default KILL|
|ptrace/namespace/mount|`ptrace`、`process_vm_readv`、`process_vm_writev`、`prctl`、`unshare`、`setns`、`personality`、`fsopen` と新 mount API|default KILL|

### 4.6 診断と変更規律

allowlist 外 syscall は process 全体を SIGSYS で終わらせ、handler で捕捉・無視できない。親は常に固定 1 行で pid と `journalctl -k -g 'type=1326.*pid=<pid> '` の読み方を示す。kernel log は番号を調べる運用者向け情報だけに使い、分類には使わない。読めない host では人間 CLI を `strace -f` で再現する。

KILL は `actions_logged` の `kill_process`、errno 判定は `SECCOMP_FILTER_FLAG_LOG` により記録される。`clone3` の ENOSYS は glibc が `clone` へ fallback するためだけに残す。fallback 集合へ追加するには、その syscall を KILL に戻すと正常 workload が失敗し、ENOSYS なら代替へ切り替わって完走する差分試験を必須とする。

表、引数条件、名前つき拒否、fallback 集合を変えたら `allow/6` と `plugin-worker/6` の両版を上げる。

## 5. 失敗契約

### 5.1 固定 reason と公開分類

|発生点|固定 reason|内部 code|公開分類 / 動作|
|---|---|---|---|
|親 host preflight|`arch_unsupported`、`landlock_unavailable`、`landlock_abi_too_old`、`seccomp_unavailable`、`missing_system_dir:<name>` (zoneinfo 等が無い)、`seccomp_status_field_unavailable` (`Seccomp_filters` 欄の無い kernel)|`sandbox_unavailable`|`worker_sandbox_unavailable`、`started:false`|
|共通 fingerprint gate|`runtime_fingerprint_selftest_failed`|`sandbox_unavailable`|同上。Popen/load 前|
|親 Popen 直前または worker 再検査|`inherited_seccomp_filter`|`sandbox_unavailable`|同上。親で検出した場合は Popen 未実行|
|worker 隔離段 (環境側)|`rlimit_failed`、`landlock_abi_too_old`、`keyring_join_failed`、`landlock_task_inspection_failed`、`landlock_multithreaded`、`runtime_root_too_wide`、`fd_open_failed`、`landlock_create_failed`、`landlock_add_rule_failed`、`no_new_privs_failed`、`landlock_restrict_failed`、`seccomp_failed`、`isolation_unexpected_error`、`runtime_import_failed`|`sandbox_unavailable`|親 reason は `sandbox_setup_failed:<reason>`、未知値は `sandbox_setup_failed:unknown`|
|worker 隔離段 (候補側)|`plugin_file_invalid`、`allowlist_not_leaf`、`allowlist_guarded`|`plugin_error`|候補の `plugin.py`/`config.yaml` が原因 (symlink・非通常ファイル・guarded dir)。`started:true`、候補枠を消費、agent に修正を促す|
|attestation|`attestation_missing`、`attestation_missing:<field>`、`attestation_order`、`attestation_unexpected_message`、`attestation_unexpected_field`、`attestation_mismatch:<field>`|`sandbox_unavailable`|`load` を送らず kill|
|子 `/proc` 検査|`proc_status_mismatch:<NoNewPrivs\|Seccomp\|Seccomp_filters>`、`proc_status_unreadable`|`sandbox_unavailable`|同上|
|Popen/worker bootstrap|`worker_bootstrap_failed`|`sandbox_unavailable`|Popen OSError、`load` 前の非 SIGSYS 自己終了|
|`load` 前の deadline|`sandbox_startup_timeout`|`sandbox_unavailable`|親 kill、`started:false`|
|`load` 前の SIGSYS|`startup_sigsys_unattributed`|`sandbox_unavailable`|原因未確定、`started:false`|
|`load` 後の SIGSYS|`sigsys_unattributed`|`crashed`|公開 `worker_crashed`、`started:true`、候補修正要求なし|
|有界 reader|`file_too_large`|`plugin_error`|候補側の失敗|

`<field>` は envelope の `pid` と attested field に限る。reason は技術ログと activity の `sandbox_reason` にだけ出し、agent response、transcript、ledger、counter、例外文字列へ出さない。

### 5.2 呼び出し元の動作

|呼び出し元|`sandbox_unavailable`|SIGSYS after `load`|
|---|---|---|
|改善 run_backtest|`worker_sandbox_unavailable`、固定 hint、`started:false`。候補枠・CPU 観測は不変、tool error streak は増加。activity は `result=sandbox_unavailable sandbox_reason=...`|`worker_crashed`、`started:true`、通常 crash と同じ候補枠/streak。activity は `result=crashed sandbox_reason=sigsys_unattributed`|
|holdout / commit gate|候補の不合格にしない。staging を残し backlog を observation にせず、mission を固定 reason で `failed` 終端 (backlog は再選択可能な `open` へ戻す)。候補側の失敗 (`plugin_error` 等) は従来どおり commit の外へ伝播し、補償 (`commit_failed`) で終端|原因未確定 crash として人間へ報告|
|live|signal を出さず `result=sandbox_unavailable` を初回 + 60 回ごとに通知。scheduler は継続|`result=crashed`、候補修正要求なし|
|indicator tool|固定 tool error|固定 crash error|
|CLI|rc=1 と人間向け固定理由|rc=1 と SIGSYS 診断|
|service|起動時診断を eager 実行。失敗しても service は起動し、評価ごとに上記 fail-closed|WARNING/ERROR は運用者向け、分類は変えない|

## 6. runtime fingerprint と admission

### 6.1 fingerprint の構成

正規化 fingerprint は次をすべて含む。

- kernel release、machine arch、Landlock ABI。
- glibc version と package/build identity。
- CPython major/minor/micro、build string、executable identity。
- numpy/pandas の version、wheel Generator、compatibility Tag。
- installed numpy/pandas の各 `.dist-info/RECORD` の正規化 SHA-256。正規化は、path が `../` で始まる行 (site-packages の外を指す console script 等。shebang に venv の絶対 path が入り、同じ wheel でも venv の場所ごとに hash が変わる) を除き、残りの行を並び順・行末のまま連結した bytes の SHA-256 とする。
- sandbox profile と seccomp filter の版。

### 6.2 対応済み集合

2026-10-03 に測定した次の 1 組だけを対応済みとする。

|要素|値|
|---|---|
|kernel / arch / Landlock|`7.0.0-38-generic` / `x86_64` / ABI `8`|
|glibc|`2.43` (`Ubuntu GLIBC 2.43-2ubuntu2.4`)|
|CPython|`3.13.14` (`main`, 2026-06-11 04:03:13)|
|numpy|`2.5.1`、Generator `meson`、Tag `cp313-cp313-manylinux_2_27_x86_64` / `manylinux_2_28_x86_64`、RECORD 正規化 SHA-256 `1302287028025a50047b0a8ca45e78195209fdc9a53efb0d25c3a7a86a2e792b`|
|pandas|`3.0.5`、Generator `meson`、Tag `cp313-cp313-manylinux_2_24_x86_64` / `manylinux_2_28_x86_64`、RECORD 正規化 SHA-256 `e9ba5610142346491f808af3ed84b371105e227ddb43d5b761bff583c2c990f3` (`../` 行なし)|
|profile|`plugin-worker/6` / seccomp `allow/6`|

ABI 3〜7 は構造上サポートする分岐を持つが、対応済み集合には入れない。kernel/ABI、userspace、wheel artifact のどれかが違えば集合外である。

### 6.3 集合外の代表自己試験

専用 harness は実 sandbox 下で、runtime import、rolling、timezone 変換、DataFrame 構築、BLAS (`OPENBLAS_NUM_THREADS=4`)、`threading`、既存 indicator の代表計算を実行する。正常終了、代表 oracle 一致、想定外 SIGSYS なしを成功条件とする。

成功時だけ「安全性は維持するが可用性・計算互換性は未保証」と WARNING して session を許可する。失敗時は `runtime_fingerprint_selftest_failed`。この試験は代表例に限られ、集合外環境の全 API と計算結果を保証しない。

## 7. 実測された事実

環境は kernel 7.0.0-38 / x86_64 / Landlock ABI 8 / glibc 2.43 / CPython 3.13.14 / numpy 2.5.1 / pandas 3.0.5、測定日は 2026-10-03。生ログは未コミットの作業領域にある。

|観測|結果|
|---|---|
|既存実 worker tests|14 ファイル 518 件が完走。worker 92 本すべて二段検証成功、正常系 SIGSYS 0、所要 408 秒|
|examples|rsi/sma/rsi_indicator と rsi_pullback の in_sample 1508 評価が独立 oracle と全件一致、正常系 SIGSYS 0|
|threading/BLAS|OpenBLAS 4 thread + Python `threading` が完走。ENOSYS は `clone3` 1 件だけ|
|起動性能|隔離なし中央値 123.3〜123.9 ms、allowlist あり 128.4〜128.5 ms、差 +5 ms。目標 +20 ms 以下|
|live tick 相当|1 本 158.2 → 162.1 ms、5 本 788.8 → 812.5 ms、差 +4〜5 ms/本|
|FS|`data/`、`config/`、`logs/`、`~/.config`、repo root、他 plugin、非選択 `/tmp` leaf、plugin dir の追加 payload は EACCES。選択した `plugin.py` と `config.yaml` だけ読める|
|socket|TCP、abstract UDS、pathname UDS はすべて到達不能。FS+network+scope だけでは pathname UDS が届くが、seccomp の `socket` 拒否で閉じる|
|process/exec|fork/vfork/非 thread clone、execve/execveat/memfd は拒否。`clone3` は ENOSYS fallback。thread は動作|
|source-only|偽 pyc を無視し `__pycache__` を作らない。rename 差替えは EACCES。traceback 行は実行 bytes と一致|
|AS 極小|1 MiB から起動可能域まで、Python に制御が戻る失敗は固定 sandbox reason、native 自己終了帯だけ `worker_bootstrap_failed`。`plugin_error` へ誤分類しない|
|SIGSYS/coredump|`RLIMIT_CORE=0` で core 保存なし。systemd-coredump の起動から回収まで約 90 ms、journal は 1 件につき 2 行|

## 8. 不変条件

|ID|不変条件|
|---|---|
|LL-1|main/indicator と numpy/pandas を読む前に、匿名 keyring、FS 規則、seccomp を 1 点で完了し、worker の全 task に及ぼす。seccomp は TSYNC、Landlock は ABI 8+ で TSYNC、ABI 3〜7 で適用時 task 数 1 を必須とし、失敗時は plugin を読まない|
|LL-2|FS allowlist は読み取りだけで write/execute 規則を持たない。pathname/fd/memfd の exec は seccomp、pathname exec は Landlock でも拒否する。mode、owner、xattr、timestamp、inode flag を変える syscall は許可しない|
|LL-3|allowlist は guarded root を覆わない。runtime/system は `/tmp` 子孫でなく、plugin は親が選んだ leaf の 2 ファイルだけ、runtime は `sysconfig` subtree だけとする|
|LL-4|規則追加、自己検査、source 読取に同じ fd 系統を使う。plugin dir に規則を張らず、`plugin.py` と `config.yaml` の inode に `READ_FILE` だけを張る|
|LL-5|`socket` と `io_uring_setup` は常に EACCES。利用できる kernel では Landlock network/scope も必ず適用する|
|LL-6|隔離不能 host、admission 失敗、環境側の worker 隔離失敗は `sandbox_unavailable` とし、`backtest_failed` にしない。候補の `plugin.py`/`config.yaml` が原因の隔離失敗 (`plugin_file_invalid`・`allowlist_not_leaf`・`allowlist_guarded`) だけは候補の責任として `plugin_error` にする。agent には固定文言だけを出す|
|LL-7|隔離を無効化する設定キーを作らない|
|LL-8|pyc と追加 payload を読まず `__pycache__` を書かない。hash 照合した同じ source bytes だけを実行し、traceback も同じ bytes を表示する|
|LL-9|束 A v1.5 の IV-1〜IV-21 を弱めない|
|LL-10|worker は thread 以外の process/task を作れない。fork/vfork/非 thread clone は EPERM、`clone3` は ENOSYS|
|LL-11|worker は継承 filter 0 を再確認し、`sandbox_ready` 後に親の `load` を待つ。親は attestation、nonce、全 task の NoNewPrivs 1 / Seccomp 2 / Seccomp_filters 1 を検証してからだけ `load` を送る。全工程は単一 deadline 内とする|
|LL-12|worker が例外として処理できる起動/call 失敗は、固定 protocol error より前に traceback を stderr へ書く。起動失敗は保持 fd を先に閉じる|
|LL-13|worker は seccomp 前に匿名 session keyring へ切り替え、以後 `add_key`、`request_key`、`keyctl` を allowlist 外とする|
|LL-14|`execve`、`execveat`、`memfd_create` は常に EPERM|
|LL-15|seccomp は §4 の表だけを許す `allow/6` とする。名前つき拒否、引数条件、`clone3` fallback、番号表外の独立 KILL 行を保持し、変更時は filter/profile の版を上げる|
|LL-16|System V IPC と POSIX mq は allowlist 外とし、object の作成・参照・変更を許さない|
|LL-17|他 process への signal と資源変更を許さない。pid 条件、fcntl cmd、ioctl request による fd 経由の signal も閉じる。自分宛て SIGSYS も EPERM とする|
|LL-18|隔離段の既知・未知の失敗は固定 reason の `sandbox_ready ok:false stage:sandbox` とする。失敗行は rlimit 前に bytes 化し、`os.write` だけで返す|
|LL-19|worker loader と親 `content_hash()` は最大 `_MAX_FILE_BYTES + 1` bytes だけ読み、超過を `plugin_error / file_too_large` とする|
|LL-20|Popen 失敗、`load` 前の自己終了、起動 deadline を `sandbox_unavailable` とする。`load` 後の deadline は `timeout`、回収不能は `crashed` とする|
|LL-21|allowlist 外 syscall は無音で続行しない。名前つき errno と `clone3` fallback を除き、呼んだ時点で process 全体を SIGSYS で終了する|
|LL-22|kernel log の可読性を隔離要件や分類条件にしない。wait4 status、親 kill、`load` の印だけで分類する|
|LL-23|親 kill なしの SIGSYS は `load` 後なら `crashed / sigsys_unattributed`、前なら `sandbox_unavailable / startup_sigsys_unattributed` とし、候補へ誤帰責しない|
|LL-24|継承 seccomp filter を worker が独自 filter/Landlock 前に再検査し、非 0・field 不在・読取不能を `inherited_seccomp_filter` で拒否する。filter 合成と `N+1` 検査はしない|
|LL-25|親は `Popen` 直前に現在 thread の `/proc/thread-self/status` を検査し、`Seccomp_filters` の非 0・field 不在・読取不能を `sandbox_unavailable / inherited_seccomp_filter` として Popen 未実行で拒否する。欄の無い kernel は host preflight が先に `seccomp_status_field_unavailable` で拒否するので、field 不在の拒否は多層防御として残す|
|LL-26|fingerprint 判定と集合外の代表自己試験は全 `PluginSession` の共通 admission gate であり、`PluginSession` の全呼び出し元 (service の live、improve の backtest、CLI) のどの入口からも迂回できない。成功と恒久失敗は fingerprint 単位で process 内 cache する (選択肢が変わらないので再試行しない)。一時要因 (`timeout`・`spawn_failed` = 負荷・fd/メモリ逼迫) の失敗は恒久 cache せず、最小間隔 (`SELFTEST_RETRY_INTERVAL_SEC`) を空けて再試行する (環境が直れば service 再起動なしで許可へ戻れる)。失敗 reason はいずれも `runtime_fingerprint_selftest_failed` とする|
|LL-27|fingerprint は numpy/pandas の version/tag だけでなく、各 dist-info `RECORD` の正規化 SHA-256 (§6.1) と Landlock ABI を含む|
|LL-28|対応済み集合は §6.2 の ABI 8 の 1 組だけとする。ABI 3〜7 は集合外として代表自己試験へ送り、実 kernel 測定なしに対応集合へ追加しない|
|LL-29|`load` 送信後に worker から届く行は候補が制御し得る入力であり、親は §2.5 の検証だけで安全を成立させる。worker 側の検証・正規化に親が依存する箇所を持たない|

## 9. 受入条件

1. 実 worker の probe が allowlist 外 FS、兄弟 plugin、作成、pathname/fd exec、TCP、abstract/pathname UDS への到達に失敗し、server hit は 0。seccomp の exec 行だけを外した対照でも pathname exec は Landlock の EACCES になる。
2. guarded root 判定の純関数は、全拒否の 3 方向、覆い拒否の 2 方向、runtime/system の `/tmp` 子孫、plugin leaf/symlink、広すぎる runtime root を全て拒否する。
3. examples の rsi_pullback と symlink 配備 rsi_indicator の in_sample が完走し、独立 oracle と全件一致する。
4. 既存の実 worker tests 14 ファイル 518 件を通し (bytecode cache の生成を前提にしていた 1 件は #40 により期待を反転)、92 worker 全てで二段検証が成功し、正常系 SIGSYS が 0 である。examples 1508 評価と thread/BLAS workload も完走し、ENOSYS は `clone3` だけである。
5. 実子 process で rlimit、ABI 3〜7 task 検査経路、未知例外、guarded path、EMFILE を実際に失敗させ、固定 reason、traceback、`sandbox_ready ok:false` を確認する。親の写像は制御 worker で `sandbox_unavailable` と stderr 技術ログを確認する。
6. 偽 pyc を無視し `__pycache__` を作らない。rename 差替えと hash 不一致を実行せず、top-level/call traceback の source 行は書換え前の実行 bytes と一致する。main の module 名は `plugin`、複数 indicator は各 `indicator_<alias>` で互いに衝突せず、`__spec__` の name・origin (= `__file__`)・`has_location`、`__loader__ is None` を確認する。decode 失敗、hash 不一致、rename 後の open 失敗、exec 失敗はいずれも `plugin_error` となる。
7. 同一 host の隔離あり/なし 7 回の `__enter__` 中央値差が 20 ms 以下。時間 assert は CI 単体テストにしない。
8. 実 worker から fork/vfork/非 thread clone/posix_spawn は EPERM、`clone3` は ENOSYS、thread は成功する。拒否されなかった対照子は即 `_exit` して残さない。
9. 実 Popen の二段 protocol で、旧一段、順序違い、余分な行、nonce/pid/field 不一致、隔離なし偽 attestation、plugin による `sandbox_ready` (attestation) の偽造を拒否し、親が `load` を送らなかったとき plugin top-level が動いていないことを確認する。複数の違反が同時にある応答 (例: `ok:false` と余分 field、余分 field と pid 不一致、pid 不一致と nonce 不一致、attestation 不一致と先読み余分行) は、§2.3 の表で先に検査される項目の reason に固定される。
10. ABI 8 の実 host で raw syscall の名前つき errno、ruleset、network/scope、attestation、全正常 workload を確認する。ABI 3、4〜5、6〜7 の ruleset 構造体サイズ、optional field、TSYNC/single-task、attestation 分岐は構造検査で確認する。旧 ABI の実 kernel 完走は対応集合を広げる条件であり、本受入の完了条件にしない。
11. live tick 相当の 1 本/5 本直列測定で増分を記録し、複数 plugin 配備前に `live-signal-eval-blocks-protection-tick` を解決する。
12. 実 worker で `keyctl`、`add_key`、`request_key` が SIGSYS となり、匿名 session keyring が親と別である。helper がある host で対照だけ helper が起動することは対応拡張時に測る。
13. 隔離前 thread は Landlock TSYNC なしの対照で外部 file を読め、TSYNC ありでは EACCES。ABI 3〜7 分岐を強制した対照は複数 task を `landlock_multithreaded` で拒否する。
14. seccomp の exec 行を外した対照では memfd + `execveat(AT_EMPTY_PATH)` が成立し、最終 profile では `memfd_create`、`execveat`、`execve` が EPERM となる。
15. runtime entry が `sysconfig` subtree だけであり、`/usr`、`/usr/local`、`$HOME` 等の広すぎる値を `runtime_root_too_wide` で拒否する。
16. main、依存 indicator、indicator 単独の各位置で、追加 source/pyc/test file の open と dir listing が EACCES、選択済み `plugin.py`/`config.yaml` だけ読める。plugin dir に READ_DIR を戻す対照は失敗する。
17. 最終 `allow/6` では `shmget`、`msgget`、`semget`、`mq_open`、`setpriority`、`sched_setaffinity`、`pidfd_open`、`kcmp`、`prctl(PR_SET_PTRACER)`、`ptrace`、`unshare`、`personality`、`fsopen` を 1 session 1 syscall で呼ぶと worker が SIGSYS で終了し、`crashed / sigsys_unattributed` となる。IPC object 数と測定対象 process の rlimit は前後不変。他 pid への `kill` と `prlimit64` は EPERM、自分宛て通常 signal と thread は成功する。
18. `sandbox_ready` 前と `plugin_ready` 前に眠る実 Popen 制御 workerで、合計 deadline を超えたとき上限 + 2 秒以内に終了する。分類は #30 に従う。
19. 最終 `allow/6` では `chmod`、`fchmodat2`、`chown`、`utimensat`、`setxattr`、`file_setattr`、keyring、IPC、mq、他 process/namespace/mount 系を 1 session 1 syscall で呼ぶと worker が SIGSYS で終了し、対象 file の mode/mtime/xattr/inode flag は不変。`F_SETOWN*`、`F_SETSIG`、`F_SETLEASE` は EPERM、危険な ioctl は ENOTTY、process/exec/memfd は EPERM、socket/io_uring は EACCES、`clone3` だけ ENOSYS とする。
20. syscall 468〜470、471、600、1000、`0x3fffffff` は KILL。default を ALLOW にする変異でも `nr >= 471` は独立の KILL 行により KILL のままとする。
21. §6.2 の対応済み fingerprint で examples、既存 worker tests、OpenBLAS 4 thread + `threading`、代表計算を完走し、KILL 0、ENOSYS `{clone3}` だけを確認する。集合外は共通 admission の代表自己試験に成功したときだけ WARNING 続行する。
22. `sandbox_ready` 後・`load` 前に同じ inode の `plugin.py` を上限超過へ追記しても 1 秒以内に `plugin_error / file_too_large` となり、親 hash reader の RSS はファイル量に比例して増えない。
23. module 欠落、syntax error、最初の行前の signal/exit、interpreter 不在を実 Popen で `sandbox_unavailable / worker_bootstrap_failed` とし、眠る worker は `sandbox_startup_timeout`、`load` 未送信とする。
24. sandbox 後の runtime import failure と自己試験中の `MemoryError` は `runtime_import_failed` とし、親で `sandbox_unavailable`、stderr 技術ログに traceback を残す。
25. `os.sync()`、許可しない getter 9 本、`os.getcwd()` を 1 session 1 API で呼ぶと worker は SIGSYS で終了する。`load` 後は `crashed / sigsys_unattributed`、前は `sandbox_unavailable / startup_sigsys_unattributed`。kernel log の可否で分類を変えない。
26. kernel log 診断関数は readable、permission、no-journalctl、timeout、`actions_logged` の `errno`/`kill_process` 欠落を固定 code にする。readable 以外は WARNING で評価を止めない。
27. 実 RLIMIT_AS を 1 MiB から起動可能域まで振り、Python に戻る隔離/runtime 失敗を固定 `sandbox_unavailable` reason、native 自己終了帯だけ `worker_bootstrap_failed` とし、同じ AS の繰返しで揺れない。
28. main strategy、依存 indicator、indicator 単独の実 worker から実 guarded path を O_RDONLY open/close と listdir し、全て EACCES、選択済み 2 ファイルだけ readable とする。実データを読み出し・変更しない。
29. `raise_signal`、`os.kill`、raw `kill`/`tgkill` で自分へ SIGSYS を送ると EPERM で worker は生存する。表外 signal syscall による SIGSYS も provenance を断定しない。
30. `load` 前 deadline は `sandbox_unavailable / sandbox_startup_timeout`、`load` 後 deadline は `timeout`。`load` 前 SIGSYS は `startup_sigsys_unattributed`、後は `crashed / sigsys_unattributed` とする。
31. `clone3` を KILL に戻すと OpenBLAS 4 thread と `threading` が SIGSYS で失敗し、ENOSYS なら完走する。将来の fallback 追加も同じ差分 oracle を持つ。
32. 継承 filter を持つ環境では worker 再検査が独自 filter/Landlock 前に `inherited_seccomp_filter` を返し、親は `load` を送らない。独自 filter 後の全 task は `Seccomp_filters=1` 固定とする。
33. `load` 後に同 uid helper が外部から SIGSYS を送ると `crashed / sigsys_unattributed`、公開 `worker_crashed`、`started:true`、通常 crash と同じ候補枠/streak、候補修正要求なしとなる。
34. 同じ SIGSYS wait status について kernel log が読める、該当行なし、権限で読めない、の全てで公開分類、reason、候補枠、hint が同一になる。
35. 実 filter を持つ別 process 内で `PluginSession` を呼ぶと、親の `/proc/thread-self/status` preflight が field 非 0 を検出し、Popen を 1 回も呼ばず `sandbox_unavailable / inherited_seccomp_filter` とする。読取不能も同じとする。読めるが `Seccomp_filters` 欄の無い kernel は host preflight が先に `sandbox_unavailable / seccomp_status_field_unavailable` とする (Popen 未実行)。
36. `PluginSession` の全呼び出し元である service eager、CLI、`signal_eval`、live、improve の backtest の各入口で同じ fingerprint gate が必ず動く。集合外 fingerprint の自己試験は、成功・恒久失敗なら process 内で 1 回だけ (cache hit では再実行しない)。一時失敗 (`timeout`・`spawn_failed`) は最小間隔内では再実行せず、間隔を超えたら再試行する。失敗は全入口で `runtime_fingerprint_selftest_failed` とする。専用 harness は再帰しない。
37. fingerprint の単体テストは numpy/pandas の version/tag が同じでも `RECORD` の `../` 以外の行のどちらか 1 bit が変われば別 fingerprint とし、`../` で始まる行の変更では変わらず、§6.2 の 2 SHA-256 を正規化後も保持する。
38. Landlock ABI が 8 から 3〜7 のいずれかへ変われば集合外になり、代表自己試験へ送られる。T1 は ABI 分岐の構造検査と ABI 8 host の正常 workload 完走で完了し、ABI 3〜7 の実 kernel 測定なしに対応集合を広げない。
39. config schema、`config/settings.yaml.example`、環境変数、CLI flag のいずれにも隔離 (admission、Landlock、seccomp、二段 protocol) を無効化・緩和する設定口がないことを検査する。schema の全 key、example、`os.environ` の参照、`backtest run` と `afx` の全 flag を走査し、隔離に関わる名前が存在せず、未知 key を与えても隔離が有効のままである。
40. bytecode cache (`__pycache__`) が 1 つも無い venv (`uv sync` 直後に相当) で実 worker を起動し、runtime 自己試験と examples の in_sample が SIGSYS 0 で完走する。worker の `sys.dont_write_bytecode` を外す変異、および親の `-B` と worker の設定の両方を外す変異では、遅延 import の `mkdir` で worker が SIGSYS 死する。完走後も venv と plugin dir に `__pycache__` が増えていない。
41. 実 worker の中で plugin が protocol fd へ直接、(a) 壊れた行・桁数の多すぎる整数・深すぎる入れ子・`NaN`・上限超の行、(b) 余分な行と先回りの応答、(c) 偽の `plugin_ready`・`sandbox_ready`、(d) 応答の直後の SIGSYS 死・CPU の空転、を起こしても、親は protocol 違反または通常の失敗分類にし、worker を残さず、候補枠・CPU 観測・cursor・signal の commit を誤って進めない。親の検証を通る値を正しい request id つきで直接書いた場合は通常の戻り値と同じ扱いになり、その値が親の validator を通ったものだけであることを確認する。indicator の系列長の不一致、float に表せない数値は、worker の検証を迂回して届いても親が拒否する。

## 10. テスト方針

### 10.1 モック禁止の次元

次は実 kernel、実 Popen、実 worker/子 process で確認し、mock に置き換えない。

- FS の read/write/create EACCES、exec、socket/io_uring、fork/vfork/clone/clone3、pathname/abstract UDS、scope signal。
- seccomp/Landlock TSYNC、適用前 thread、keyring 切替、memfd exec、IPC object 数、他 process 操作、metadata 不変。
- source-only loader、偽 pyc、rename/同 inode 書換え、linecache、bounded reader、AS 極小時の固定応答。
- 二段 protocol の拒否、拒否時 plugin 未実行、子 `/proc` status、単一 deadline、SIGSYS death、外部 SIGSYS、fallback 差分。
- 親 Popen 直前の実 `Seccomp_filters`、共通 fingerprint gate の全入口、実 dist-info `RECORD` hash。

純関数としてモック可能なのは、親の分類表/reason 写像、通知の間引き、Popen 未呼出し assertion、attestation field 検証、ABI ごとの構造体生成、BPF 命令列生成、fingerprint 正規化、kernel log 診断分類である。実効性は必ず上の結合テストでも取る。

実 `data/` と実 `plugins/` は変更しない。guarded path probe は O_RDONLY open→close と listdir だけ、fork 対照の子は即 `_exit` する。

### 10.2 変異候補

- 適用境界: plugin/runtime import の後へ移す、rlimit を隔離段外へ出す、隔離 module を遅延 import、未知例外捕捉を外す、失敗応答を実行時 JSON 生成へ戻す。
- Landlock: TRUNCATE/EXECUTE handled を外す、fd でなく path を再 open、plugin dir に READ_DIR を戻す、`O_NOFOLLOW` を外す、runtime root を prefix 全体へ広げる、guarded direction/type を 1 つ外す、TSYNC を外す/失敗時 flags 0 fallbackを足す、ABI 3〜7 task 検査を外す。
- seccomp: arch 検査、番号表外の独立行、default KILL、TSYNC、LOG を外す。fork/vfork/exec/memfd/socket/io_uring の各行を外す。`clone3` を KILL/EPERM にする。clone namespace/thread flags、kill/tgkill/prlimit pid、SIGSYS signal、fcntl cmd、ioctl request の各条件を外す。
- protocol: `load` 待ち、順序/余分行/nonce/pid/field/全 task `/proc` 検査、deadline 再確認を 1 つずつ外す。期待値を worker 申告から作る。`Seccomp_filters` を 1 以外にする。
- loader/診断: pyc を読む、hash/上限を外す、親 hash を全読みに戻す、linecache を exec 後へ移す、fd close 前に traceback、call traceback、親の SIGSYS 固定 1 行を外す。kernel log で分類を変える。
- 分類: `stage` を無視して plugin error、bootstrap EOF を crash、親 kill 条件を外す、`load` 印を見ず timeout/CPU/SIGSYS を分類、post-load SIGSYS を候補責任へ戻す。
- admission: 親 Popen 直前検査を外す/worker 側だけにする、field 不在/読取不能を許す、fingerprint gate を service だけにする、cache key から ABI/RECORD hash を外す、集合外自己試験を import だけにする/失敗しても続行、自己試験を `PluginSession` 再帰にする。
- 束 A: `worker_sandbox_unavailable` を `started:true`、streak 対象外、live/commit gate の写像なし、SIGSYS activity の `sandbox_reason` なし、候補失敗 resultへ戻す。

## 11. 実装 task

依存順は **`T1 ∥ T2 → T3 → T4 → T5`** とする。

T1・T2 の完了判定は、二段 protocol を通さない直接子 process の harness (テストが起こす `python -c` の子が隔離関数を直接呼ぶ。worker の protocol は通さない) で行う。T1・T2 の正常 workload、examples、BLAS/threading は、この harness が適用した profile の下で測る。実 worker を起動する E2E は T3 (関数を直接呼ぶ子) と T4 (実 Popen の二段 protocol) が担い、T1・T2 の完了判定には使わない。

|task|範囲|依存|所有ファイル|完了条件|並列|
|---|---|---|---|---|---|
|T1 Landlock|ABI 別 ruleset、fd/file 規則、TSYNC/single-task、ABI API。既存 `restrict_to` の呼び出し元 (gate、improve) の挙動は変えない|なし|`core/landlock.py`、tests|#2、#10、#13、#15、#16、#28、#38。この host で直接子 harness による正常 workload 完走 + ABI 3〜7 分岐の構造検査|T2 と可|
|T2 seccomp + fingerprint|データ表から `allow/6` 生成、引数条件、fallback、fingerprint/RECORD hash、代表自己試験 harness/cache 契約|なし|`core/seccomp.py` (新設)、fingerprint と自己試験 harness の新設 module、開発用スクリプト、tests|#8、#10、#14、#17、#19〜21、#25〜26、#29、#31、#37。直接子 harness による単独 process 受入|T1 と可|
|T3 worker isolation + loader|protocol を通さない実子で隔離段、固定失敗応答、source-only loader|T1、T2|`plugin/worker_isolation.py` (新設)、tests。`plugin/worker.py`・`plugin/sandbox.py` は触らない|#5〜6、#12〜17、#19、#22、#24〜25、#27〜29、#31、#40|不可|
|T4 親 + 共通 admission + 束 A + live/CLI|Popen 直前 preflight、二段 protocol、`PluginSession` の全呼び出し元、束 A v1.5、service eager/CLI lazy gate|T3|`plugin/worker.py`、`plugin/sandbox.py`、`loops/improve_loop.py`、`plugin/signal_producer.py`、`backtest/cli.py`、`service.py`、tests|#1、#3〜5、#9、#18、#21〜23、#25〜26、#30、#32〜36、#41、#39、束 A AC-28〜35|不可|
|T5 運用 + perf + gate profile|service uid の kernel log、起動/tick性能、SIGSYS/coredump負荷、runbook、別 gate pytest profile/ticket (`gate-pytest-dev-writable-and-ldso-exec`)|T4|`docs/tickets/`、runbook、実測表|#7、#11、#26、実測表と運用手順の更新|不可|

T1 は `core/landlock.py` だけ、T2 は新設の `core/seccomp.py` と fingerprint/自己試験 module だけを所有し、同じファイルを触らない。`plugin/sandbox.py` と `plugin/worker.py` を触るのは T4 だけである。

T4 の最初に偽 worker fixture、Popen seam、旧一段 ready、既存 6 outcome 固定テストを全数 inventory する。`plugin/sandbox.py` と `plugin/worker.py` を同じ task で更新し、中間状態で protocol を壊さない。

## 12. 残余リスクと対応外

|点|扱い|
|---|---|
|継承 seccomp filter の環境|systemd `SystemCallFilter=` と container filter は `inherited_seccomp_filter` で拒否。合成しない|
|真の禁止 syscall|post-load SIGSYS は原因未確定 crash のため agent は自動修正せず、人間が kernel log/strace で診断する|
|集合外 fingerprint|代表自己試験は best-effort。成功しても安全性は維持するが、可用性・計算互換性の全ては保証しない|
|同 uid 外部 signal|脅威モデル外。SIGSYS を候補へ誤帰責しないことで吸収する|
|SIGSYS coredump cost|`RLIMIT_CORE=0` でも systemd-coredump 起動と journal 記録に約 90 ms。core 本体は保存しない|
|metadata side channel|`stat` 系により allowlist 外 path の存在・size・mtime が見える場合がある。内容と書込みは拒否|
|`/usr/lib`|distro 配布物だけを置く運用前提。秘密を置く運用は対応外|
|RLIMIT_NPROC|per-uid 累積で thread 可用性が host 依存。別チケットで扱う|

- **`load` 後の応答は plugin が直接書ける。** 同じ process に protocol fd があるため、plugin は worker の信頼コードを通さずに応答行を書ける。親の検証 (§2.5) を通る値については通常の戻り値と区別できない。supervisor process と plugin 実行 process の分離は、process 生成を閉じる構造と起動時間 (1 worker 約 150 ms) を大きく変えるため採らず、親側の検証で閉じる。
- **cwd に依存する API は worker を殺す。** `getcwd` は allowlist 外なので、sandbox 下で `os.getcwd()`、相対 path の `os.path.abspath()`、`Path.resolve()`、`Path.cwd()` を呼ぶと SIGSYS 死する。worker は `-m` 起動で `sys.path` を起動時に絶対化しているので import は影響を受けない。候補 plugin がこれらを呼んだ場合は `crashed / sigsys_unattributed` になり、人間の診断を要する。

## 13. 未決事項

- 対応集合を ABI 3〜7 へ広げる際は、各実 kernel で ruleset/attestation に加え、examples oracle、既存 worker E2E、BLAS/threading、正常系 SIGSYS 0 を測る。現時点では広げない。
- system Python、pyenv、conda、別 glibc/distro、musl、`/etc/ld.so.cache` を要する配置は正式対応時に再測定する。
- `request_key` helper が実在する host、service uid が kernel log を読めない host、ABI 3〜5 の SIGIO end-to-end は対応拡張または運用確認時に測る。

## 14. 設計レビューの経過

|周|指摘と決定|
|---|---|
|r1|fork による CPU/orphan 迂回、適用位置、allowlist 自己検査、偽 pyc、traceback、ABI/性能を指摘。process 生成拒否、1 点適用、attestation、source-only loader を採用|
|r2|継承 keyring、Landlock TSYNC、memfd exec、attestation 偽造を指摘。匿名 keyring、二段 protocol、全 task `/proc` 検査を採用|
|r3|runtime root、plugin dir の隠し payload、IPC/他 process、二重 deadline、reason 網羅、task 境界を指摘。file 規則、subtree、単一 deadlineへ修正|
|r4|metadata/fd signal と deny-list の列挙漏れ、runtime 配置、loader 上限を指摘。seccomp を allowlist へ反転し、対応 runtime と有界 reader を固定|
|r5|ENOSYS の誤値、束 A、AS 極小、linecache、実効 EACCES、pid field を指摘。診断・固定失敗応答・Bundle A 改訂を追加|
|r6|void wrapper で ENOSYS 拒否が無音、`load` 前 timeout の started 矛盾を指摘。default KILL と `load` 境界分類へ変更|
|r7|SIGSYS の帰責不能、version matrix の非有限性、旧受入文を指摘。SIGSYS を原因未確定、継承 filter fail-closed、有限 fingerprint と集合外自己試験へ変更|
|r8|親 Popen 前検査、全入口共通 admission、RECORD hash、ABI fingerprint、#17/#19 の旧 oracle を指摘。5 件すべて採用し、Critical/High 0 で収束|
|r9 (受入)|gate pytest の対象範囲矛盾 (High)、attestation 検査順・source-only loader 契約の脱落、T1/T2 の完了 harness と所有境界、LL-7 の受入欠落を指摘。全件採用し、設計判断は変えず清書の欠落を復元|

## 15. 変更履歴

|日付|版|変更|理由|commit|
|---|---|---|---|---|
|2026-10-03|C0 v0.8|8 周目直前までの隔離、allowlist、二段 protocol、loader、分類、実測を統合|設計レビュー 7 周と裁定の統合|未コミット|
|2026-10-04|v1.0|r8 5 件を反映し、最終動作、表、LL-1〜28、受入 #1〜38、task、残余を公開 spec として清書|設計レビュー収束と実装着手条件の固定|—|
|2026-10-04|v1.0|受入レビューの反映: admission gate の対象を `PluginSession` の全呼び出し元に限定 (gate pytest worker を除外)、attestation 検査順と source-only loader 契約を C0 から復元、T1・T2 の直接子 harness と所有ファイル境界を task 表へ、LL-7 の受入 #39 を追加|受入レビュー r9 の High 1 / Medium 3 / Low 1|—|
|2026-10-04|v1.1|実装 (seccomp・fingerprint) で見つかった 2 点を反映: RECORD hash を `../` 行を除く正規化に変更し numpy の値を差し替え (LL-27、#37)。worker は隔離前に `sys.dont_write_bytecode = True`、親は `-B` で起動、受入 #40 を追加|numpy の RECORD は console script 行が venv の絶対 path に依存し別の場所の同じ wheel が集合外になる / pycache の無い venv では遅延 import の `mkdir` で worker が SIGSYS 死する (実測)|—|
|2026-10-04|v1.2|`load` 後の応答を候補が制御し得る入力と定め、親の検証 (§2.5、LL-29、受入 #41) を追加。#9 の「偽造」が `sandbox_ready` を指すことを明記。残余に追記|実装レビューの指摘 (plugin が protocol fd へ直接書ける)。現物の調査で、worker だけが担保し親が再検証していない性質 (indicator の系列長、数値の表現可能性、パース例外、要求と応答の対応、死後の最終行) を特定し、親側で閉じる裁定|—|
|2026-10-04|v1.2|受入 #4 の文言を訂正: `tests/plugin/test_gate_pytest.py` の 1 件 (PluginSession 実行後に `__pycache__` が生成される前提) は #40 により「生成されない」へ反転したため、「変更なしで通す」を「通す (反転 1 件を除く)」に直した|実装で #40 と #4 の文が食い違った|—|
|2026-10-04|v1.3|実装レビューの指摘 14 件のうち spec に影響する 5 件を反映: (1) commit gate の環境側失敗は候補の不合格にせず staging を残し backlog を `open` へ戻して mission を `failed` 終端 (§5.2)。(2) admission の一時失敗 (`timeout`・`spawn_failed`) は恒久 cache せず間隔を空けて再試行、成功・恒久失敗だけ cache (LL-26、#36)。(4) 候補由来の隔離失敗 (`plugin_file_invalid`・`allowlist_not_leaf`・`allowlist_guarded`) は `plugin_error` に分類 (§5.1、LL-6)。(5) host preflight が zoneinfo 欠落と `Seccomp_filters` 欄なし kernel を先に検知 (§2.1、§5.1、LL-25)。(6) `load` 後に死んだ worker の `plugin_ready` を読んでから分類 (§2.5)|実装レビュー (/code-review) の指摘。環境側失敗で候補が失われる・一時失敗が恒久化する・候補責任を環境障害に写す・事前診断できない水準・死後の `plugin_ready` を取りこぼす、の是正|—|
