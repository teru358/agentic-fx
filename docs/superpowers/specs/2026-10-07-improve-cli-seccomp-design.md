# [improve-cli-seccomp] 改善 mission の CLI 部分木の隔離 v1.3

版: v1.3

日付: 2026-10-09

対象チケット: `sandbox-escape-via-user-daemons-unmeasured`

対象: 改善 mission が起動する CLI backend (`claude` / `codex` / `opencode`) とその全子孫

## 要点

- 非信頼の CLI 部分木を、outer / nested Landlock、全 syscall 番号を分類する seccomp、mission cgroup、connect supervisor、CONNECT proxy、spool により隔離する。
- `worker_proc` は WorkerRunner の direct child、`launcher_proc` は mission worker の direct child である。各 `Popen` を wait / reap するのは direct parent だけとする。
- connect は seccomp user-notification で止め、worker の supervisor が検査済み socket の open file description を複製して代行する。外向き TCP は proxy と opencode 専用の `127.0.0.1:8080` に限る。
- seccomp listener の handoff は、receiver へ所有権を移し、worker が参照を破棄した時点を handoff 完了とする。handoff 後に listener を close できるのは receiver thread だけである。
- cleanup の全系列の正本は §4.2.3 とする。afx 本体が単一の absolute cleanup deadline を先に発行し、WorkerRunner は ACK に依存せず `worker_proc.wait()` で worker を reap する。
- unit の admission は `Delegate=pids memory`、direct `ExecStart`、固定 `systemctl --user show` argv、停止の層の property を要求する。起動後の property 変更・reload は検出しないため、運用前提と残余を §6.1、§12 に固定する。
- 対応済み fingerprint の集合は、最終 profile による自己試験を完了するまで空とする。opencode の正常完走は enable の blocker である。

## 1. 目的、信頼境界、範囲

### 1.1 目的

改善 mission の CLI backend とその全子孫は、次をできない。

| 類 | 部分木ができないこと |
|---|---|
| process | 部分木外 process への signal、rlimit・scheduler・優先度の変更、ptrace、memory 参照、pidfd による fd 取得 |
| IPC | pathname / abstract UDS、`/dev/shm`、PTY / tty、SysV IPC、POSIX mq、keyring、netlink |
| network | 許可宛先以外への TCP、UDP・raw・DNS、明示 bind と bind 無し `listen`。許可は proxy と opencode 専用 `127.0.0.1:8080` だけ |
| file metadata | allowlist 外 file および部分木外と共有する ro file の mode、owner、時刻、xattr、attr の変更。path 形と fd 形の双方を含む |
| kernel | namespace、mount、bpf、perf、io_uring、AIO、時刻・hostname、kernel module、credential の変更 |
| 資源 | mission cgroup の `pids.max`、`memory.max` を超える process 数と memory |
| cgroup | mission cgroup からの脱出、上位 cgroup の操作 |

network の保証は application host 名ではなく、proxy が定数 host 名を解決して mission 内で固定した address 集合である。同じ address の別 site / tenant は §12 の残余である。

### 1.2 信頼境界

| 主体 | 信頼 | 役割と隔離 |
|---|---|---|
| afx 本体 / WorkerRunner | 自前 code | unit topology、admission、`worker_proc` の唯一の wait / reap、cgroup の最終回収。Landlock なし |
| mission worker | 自前 code | dispatcher、connect supervisor、proxy。outer Landlock 下、seccomp なし、subreaper ではない |
| launcher | exec 前は自前 code | `PR_SET_CHILD_SUBREAPER`、nested Landlock、seccomp を設定し CLI を exec。`launcher_proc` の status は worker が回収 |
| CLI と子孫 | 非信頼の一つの principal | outer / nested Landlock、seccomp、mission cgroup を継承 |

部分木内の socket fd 差替え、spool 応答の先置き、相互 signal、zombie による `pids.max` の消費は同一 principal の自己妨害とする。ただし部分木外と共有する ro file の lock と metadata 変更は境界の対象である。

### 1.3 対象外

- `sched_get*`、`getpriority`、`ioprio_get`、`pidfd_open`、`/proc/<pid>/stat` など、他 process の公開 metadata の読取り。
- file metadata の読取り (`stat`、`statx`、`access`、`getxattr`、`listxattr`、`O_PATH + fstat`)。
- `/proc/<pid>/cmdline` の露出、trade profile、local backend。ただし MCP transport の置換 (§4.1 の spool dispatcher と `afx.sock` 廃止) は trade profile にも適用する。trade には seccomp・nested Landlock・cgroup の隔離を掛けないが、transport は一本化する。

## 2. 起動、所有権、データフロー

```text
afx 本体 (unit の main PID、<unit>/supervisor/)
  └─ WorkerRunner -- worker_proc --> mission worker
       ├─ mission cgroup <unit>/m-<nonce>/ を作成、interface fd を保持
       ├─ spool、proxy、NETLINK_SOCK_DIAG を Landlock 前に作成
       └─ launcher_proc --> launcher --> CLI と全子孫
```

1. afx 本体は unit delegation、topology、kernel 能力を admission で検査し、WorkerRunner が 128 bit 以上の mission nonce を worker に渡す。
2. worker は cgroup を新設し、`pids.max`、`memory.max`、`memory.swap.max=0` を設定する。`cgroup.procs`、`cgroup.kill`、`cgroup.events` を CLOEXEC で保持する。
3. worker は spool root、proxy listener、NETLINK_SOCK_DIAG socket を作り、`launcher_proc = Popen(...)` で launcher を起動する。worker は直後に自分の status 子端と backend execution artifact fd copy を閉じる。
4. launcher は HELLO、JOINED、PREPARED、GO の protocol を完了して backend execution artifact を `execveat` する。codex の artifact は native ELF (`codex-rs`、static PIE musl) の fd とし、npm wrapper は実行 TCB に含めない。`CODEX_MANAGED_PACKAGE_ROOT` / `CODEX_MANAGED_BY_NPM` などの managed 環境変数は意図的に省略する契約とし、現行の正常系は未設定で完走する。PREPARED の listener fd を receiver に渡し、worker が参照を破棄した時点で handoff 完了となる。
5. CLI の exec 後、supervisor は connect を代行し、dispatcher は spool を処理する。CLI env の proxy は `http://127.0.0.1:<port>`、opencode の llama-swap URL は `http://127.0.0.1:8080/v1` の literal とする。
6. 終了時は WorkerRunner が cleanup より前に monotonic absolute deadline を発行する。正確な順序は §4.2.3 のみを正本とする。

worker から CLI への `killpg` は、親 Landlock domain から子 domain への操作として許可する。

## 3. seccomp `improve-cli/1`

### 3.1 評価順、flag、表の規律

| 段 | 条件 | 結果 |
|---|---|---|
| 1 | arch が x86_64 でない、または x32 (`nr >= 0x40000000`) | KILL_PROCESS |
| 2 | `nr >= 472` | ERRNO(ENOSYS) |
| 3 | 0〜471 の canonical table | ALLOW / COND / NOTIF / EPERM / EACCES / ENOSYS |

- filter flag は `TSYNC | TSYNC_ESRCH | NEW_LISTENER | WAIT_KILLABLE_RECV` とし、`LOG` は含めない。
- `TSYNC_ESRCH` は `TSYNC | NEW_LISTENER` の install に必須である。`WAIT_KILLABLE_RECV` は signal 下の notification livelock を防ぐ。
- 全 syscall 番号 0〜471 を明示分類し、ceiling 472 を data として固定する。割当済み name の未分類または重複は生成失敗とする。
- canonical table、生成 BPF、profile 版、policy hash は 1 対 1 とする。v1 の件数は ALLOW 202、COND 12、NOTIF 1、EPERM 150、EACCES 1、ENOSYS 106、policy hash は `411c5239c362429890312987d9cb0f534f2a5c826123a6125931a6d788466108` である (canonical text = `nr\tname\taction\tcondition_id` の 472 行に、`cond\t<syscall>\t<condition_id>\t<条件定義 JSON>` 12 行を続けた ASCII の SHA-256。条件定義を含むので条件の 1 bit の変更で hash が変わる)。生成 BPF は 404 命令、filter flags は `TSYNC|NEW_LISTENER|TSYNC_ESRCH|WAIT_KILLABLE_RECV` (LOG なし)。
- errno の既定は EPERM とする。ENOSYS は `clone3` fallback、廃止・未実装、keyring に限定し、各 fallback は受入 12b で差分試験する。

### 3.2 ALLOW

ALLOW は自 process に閉じる memory、thread、signal mask、時刻と id の取得、fd I/O、poll、eventfd、timerfd、inotify、自分の socket の recv、Landlock が判定する path 操作、path metadata の読取り、fork / vfork / 条件内 clone / exec、`setsid` / `setpgid`、nested Landlock が scope を制限する signal 系、`seccomp`、`landlock_*` に限る。

`accept` は許可するが `listen` は拒否する。`memfd_create` と `execveat` は部分木内の任意 code 実行を前提として許可する。POSIX timer は許可しない。

### 3.3 条件つき syscall

| syscall | 許す条件 | 条件外 |
|---|---|---|
| `socket` | AF_INET/6、基本 type が SOCK_STREAM、protocol が 0 または 6 | AF_UNIX と INET の他 type は EACCES、他 family は EAFNOSUPPORT |
| `socketpair` | AF_UNIX | EAFNOSUPPORT |
| `clone` | `CLONE_NEW*` と `CLONE_PARENT` が無い | EPERM。`CLONE_PARENT_SETTID` は許可 |
| `prlimit64` | pid = 0 | EPERM |
| `membarrier` | QUERY、PRIVATE_EXPEDITED、REGISTER_PRIVATE_EXPEDITED、`*_SYNC_CORE`、`*_RSEQ` | EPERM |
| `personality` | `0xffffffff` の照会だけ | EPERM |
| `sendto` | `MSG_FASTOPEN` 無し、dest_addr = NULL | EPERM |
| `sendmsg`、`sendmmsg` | `MSG_FASTOPEN` 無し | EPERM |
| `prctl` | §3.3.1 の option | EINVAL |
| `fcntl` | §3.3.2 の cmd | EINVAL |
| `ioctl` | §3.3.3 の request | ENOTTY |

#### 3.3.1 `prctl`

許可する option は `PR_SET_NAME`、`PR_GET_NAME`、`PR_SET_VMA(ANON_NAME)`、`PR_GET/SET_THP_DISABLE`、`PR_SET/GET_PDEATHSIG`、`PR_SET/GET_NO_NEW_PRIVS`、`PR_GET_SECCOMP`、`PR_CAPBSET_READ`、`PR_GET_DUMPABLE`、`PR_GET_CHILD_SUBREAPER` だけである。

`PR_SCHED_CORE`、`PR_SET_PTRACER`、`PR_SET_MM`、`PR_SET_DUMPABLE`、`PR_SET_CHILD_SUBREAPER`、`PR_SET_SECUREBITS`、`PR_CAP_AMBIENT`、`PR_SET_SPECULATION_CTRL`、その他は EINVAL とする。

#### 3.3.2 `fcntl` と lock

許可 cmd は `F_DUPFD`、`F_DUPFD_CLOEXEC`、`F_GETFD`、`F_SETFD`、`F_GETFL`、`F_SETFL`、`F_GETLK`、`F_SETLK`、`F_GET_SEALS`、`F_ADD_SEALS`、`F_GETPIPE_SZ` とする。`flock` は許可する。

`F_SETLKW`、`F_OFD_GETLK`、`F_OFD_SETLK`、`F_OFD_SETLKW`、`F_SETOWN*`、`F_SETSIG`、`F_SETLEASE`、`F_NOTIFY`、`F_SETPIPE_SZ`、その他は EINVAL とする。

共有 ro file の `flock` と `F_SETLK(F_RDLCK)` は外部 process の lock を mission 終了まで待たせ得るため §12 の残余とする。workdir の DB に必要な lock を worker が代行する方式、または共有 tree の private copy は採らない。

#### 3.3.3 `ioctl`

許可 request は `TCGETS`、`TCGETS2`、`TIOCGWINSZ`、`TIOCGPGRP`、`TIOCGPTN`、`FIONBIO`、`FIONREAD`、`FIOCLEX`、`FIONCLEX`、`FICLONE`、`FICLONERANGE` とする。`TIOCSTI`、`TIOCSCTTY`、`FIOSETOWN`、`FS_IOC_SETFLAGS`、line discipline、VT、DRM、input、block、KVM を含むその他は ENOTTY とする。

### 3.4 名前つき拒否

| 類 | syscall | errno |
|---|---|---|
| ptrace | `ptrace`、`process_vm_*`、`pidfd_getfd`、`kcmp`、`process_madvise`、`process_mrelease`、`get_robust_list`、`migrate_pages`、`move_pages` | EPERM |
| credential | `capset`、`setuid`、`setgid`、`setreuid`、`setregid`、`setresuid`、`setresgid`、`setfsuid`、`setfsgid`、`setgroups` | EPERM |
| scheduler | `sched_setscheduler`、`sched_setparam`、`sched_setaffinity`、`sched_setattr`、`setpriority`、`ioprio_set` | EPERM |
| resource | `io_setup`、`io_destroy`、`io_submit`、`io_cancel`、`io_getevents`、`io_pgetevents`、`mlock`、`mlock2`、`mlockall` | EPERM |
| namespace / mount | `unshare`、`setns`、mount 系、新 mount API、`statmount`、`listmount`、`listns`、`chroot`、`pivot_root` | EPERM |
| listener | `listen` (TCP と AF_UNIX の双方) | EPERM |
| IPC | SysV IPC 12 個、POSIX mq 6 個 | EPERM |
| keyring | `add_key`、`request_key`、`keyctl` | ENOSYS |
| io_uring | `io_uring_setup`、`io_uring_enter`、`io_uring_register` | EPERM |
| kernel | `bpf`、`userfaultfd`、`fanotify_init`、`fanotify_mark`、`syslog`、`_sysctl`、`adjtimex`、`settimeofday`、`clock_settime`、`clock_adjtime`、`sethostname`、`setdomainname`、`sync`、`syncfs`、`kexec_*`、`*_module`、`reboot`、`swapon`、`swapoff`、`acct`、`quotactl*`、`iopl`、`ioperm`、`open_by_handle_at`、`name_to_handle_at`、`lookup_dcookie`、`vhangup`、`nfsservctl`、`lsm_*`、`pkey_*`、`modify_ldt`、`mknod*`、`remap_file_pages`、`memfd_secret`、`map_shadow_stack`、`mseal` | EPERM |
| obsolete | `create_module`、`get_kernel_syms`、`query_module`、`afs_syscall`、`tuxcall`、`security`、`getpmsg`、`putpmsg`、`vserver`、`*_old`、`uprobe`、`uretprobe`、`set/get_thread_area`、`clone3` | ENOSYS |
| path metadata | `chmod`、`fchmodat*`、`chown`、`lchown`、`fchownat`、`utime*`、`setxattr*`、`removexattr*`、`file_setattr` | EPERM |
| fd metadata | `fchmod`、`fchown`、`fsetxattr`、`fremovexattr`、`utimensat` の全形 | EPERM |
| POSIX timer | `timer_create`、`timer_settime`、`timer_gettime`、`timer_getoverrun`、`timer_delete` | EPERM |

### 3.5 metadata、credential、rlimit

Landlock は O_PATH と metadata 読取りを拒否しないため、metadata の変更は path 形と fd 形の双方を seccomp で拒否する。共有 ro file を O_RDONLY で開いた fd に対する `fchmod`、`fchown`、`fsetxattr`、`fremovexattr`、`utimensat` も EPERM とする。workdir file も例外にしない。

launcher は HELLO 前に effective / permitted / inheritable capability が全て 0、uid / gid / supplementary group が worker の期待値と一致することを検査する。AS、NOFILE、FSIZE、CORE、`RLIMIT_NICE`、`RLIMIT_RTPRIO`、`RLIMIT_RTTIME`、`RLIMIT_MEMLOCK`、`RLIMIT_MSGQUEUE` の hard limit を固定し、後者 5 個は `(0,0)` とする。`RLIMIT_SIGPENDING` は uid 全体に影響するため 0 にしない。process と memory の上限は cgroup に置く。

## 4. MCP と外向き通信

### 4.1 spool

spool root は worker が Landlock 前に `workdir/afx-spool/<mission nonce>/` として 0700 で新設し、dirfd を保持する。cross-directory rename は使わず、同一 dir で `RENAME_NOREPLACE` を使う。

| 項目 | 契約 |
|---|---|
| name | `<id>` は 128 bit random hex。`tmp-` → `req-` → `claim-`、応答は `rtmp-` → `resp-` |
| publish | shim は `O_CREAT|O_EXCL|O_NOFOLLOW`、全量 write、fsync、`renameat2(..., RENAME_NOREPLACE)` |
| claim | dispatcher は `req-` を `claim-` に NOREPLACE rename。成功した 1 回だけを実行 |
| validate | dirfd 相対 `openat2`、`RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_XDEV`、regular file、uid、一つの link、private mode、bounded read、JSON 深さ・key 数・重複 key を検査 |
| replay | 処理済み id を mission 中保持し、重複を実行しない |
| response | 実行前に `rtmp-` を O_EXCL で確保。先置きなら実行しない。実行後 fsync と NOREPLACE publish。`resp-` の先置きは ledger に `response_preempted` |
| authority | tool 実行の正本は worker ledger。shim の応答は同一 principal により偽造可能であり実行証明に使わない |
| scan | inotify は wake 用。起動時、各 wake、`IN_Q_OVERFLOW`、`IN_IGNORED`、1 秒周期の rescan を正本とし、entry 上限超過は `spool_flood` |

dispatcher は allowed、budget、実行、counter を一つの lock 区間で直列化する。終了時は §4.2.3 の cgroup kill と `populated 0` の後に dispatcher を止め、以後 rescan しない。AF_UNIX の `afx.sock` は廃止する。

### 4.2 connect の代行

seccomp は `connect` を USER_NOTIF にする。supervisor は `CONTINUE` を使わず、各通知を一度だけ処理する。

#### 4.2.1 通知 1 件の状態機械

| 段 | 動作 | 失敗時 |
|---|---|---|
| 0 | `SECCOMP_GET_NOTIF_SIZES` から buffer size を取得 | `seccomp_user_notif_unavailable` |
| 1 | zeroed buffer で `NOTIF_RECV` | EINTR retry、ENOENT は終了、その他は supervisor fault |
| 2 | x86_64、`connect`、addrlen 16〜128 を検査 | 不整合は EPERM と fault、範囲外は EINVAL |
| 3 | ID_VALID (1 回目) | ENOENT は無応答終了 |
| 4 | `pidfd_open(tid, PIDFD_THREAD)` | 有効なら EPERM、無効なら無応答終了 |
| 5 | ID_VALID (2 回目) | ENOENT なら pidfd を閉じ無応答終了 |
| 6 | `process_vm_readv` で sockaddr を完全長 1 回 copy | EFAULT |
| 7 | ID_VALID (3 回目) | ENOENT なら copy と pidfd を破棄 |
| 8 | AF_INET、`127.0.0.1:proxy` または opencode の `127.0.0.1:8080` を完全一致で検査 | EAFNOSUPPORT / EINVAL / ECONNREFUSED |
| 9 | `pidfd_getfd`、SO_DOMAIN、SOCK_STREAM、TCP を検査 | ENOTSOCK / EPERM |
| 10 | ID_VALID (4 回目) | ENOENT なら複製を閉じ接続しない |
| 10b | lock 下で RUNNING の検査と committed 記録を原子的に更新。proxy 宛は pending entry を追加 | STOPPING なら EPERM |
| 11 | copy 済み sockaddr で複製 fd に raw `connect` | tracee の nonblocking 設定を維持 |
| 11b | proxy 宛の成功 / EINPROGRESS は client tuple を記録し、1 秒の ack を待つ | 不一致・timeout は shutdown して ECONNREFUSED |
| 12 | `NOTIF_SEND`、entry 削除、複製と pidfd close | ENOENT は正常 race として記録 |

IPv6、IPv4-mapped、`AF_UNSPEC`、scope id 付き IPv6 は拒否する。fd 番号の差替えは同一 principal の race だが、複製 fd は TCP と許可宛先を再検査するため境界を越えない。

#### 4.2.2 supervisor、receiver、deadline

| 部品 | 契約 |
|---|---|
| receiver | handoff 完了後の listener の唯一の所有者。`poll([listener, stop_efd])` の POLLIN 時だけ RECV。最外周 `finally` で receiver 異常を確定する |
| state | `RUNNING`、`STOPPING`、`CLOSED`、`STUCK`、`in_flight`、committed record は一つの lock で守る |
| slot | `CONNECT_SLOTS = 16`。handler 起動前に nonblocking 取得し、飽和は EAGAIN |
| watchdog | handler deadline は monotonic 2 秒、監視頻度 100 ms。例外、timeout、想定外 RECV / SEND errno は supervisor fault |
| stop | lock 下の RUNNING → STOPPING が線形化点。stop_efd に書いた後、新しい RECV・commit・handler 起動を禁止 |
| close | receiver は `in_flight` が 0 になるまで close しない。CLOSED は不可逆で ack を立てる |
| STUCK | handler が drain deadline 内に終わらなければ、listener を開いたまま RECV / commit を禁止し ack を立てる。最終 close は worker process の終了 |

receiver が RECV した通知は、同じ lock で receiver-owned 集合に登録し `in_flight` を増やしてから handler に渡す。handler 完了または receiver 自身による EPERM の SEND 完了で減らす。登録前例外は receiver 異常であり、後続 close ができれば ENOSYS、STUCK なら通知を block のまま `cgroup.kill` で終端する。

| stop の観測点 | STOPPING 時の結果 |
|---|---|
| poll 復帰後、RECV 前 | RECV せず ENOSYS |
| RECV 後、登録・handler 起動前 | 登録済みなら receiver が EPERM、登録前例外は ENOSYS または STUCK block |
| commit 前 | handler は connect せず EPERM |
| commit 後、raw connect 前 | pre-stop の committed 操作として connect の実値 |
| SEND 前 | committed なら connect の実値 |

`CLEANUP_BUDGET_SEC = 10` とする。WorkerRunner だけが `cleanup_deadline = monotonic_now + 10s` を、cleanup の最初の操作より前に発行する。契機は worker の cleanup 要求、worker exit / result EOF / 無応答、afx 本体の mission deadline、afx 本体の停止開始の早いものとする。

worker は deadline を受けて ACK を返すまで stop と cgroup 操作を開始しない。ACK 後に、deadline の 3 秒前に `os._exit` する独立 cleanup watchdog を始動する。WorkerRunner は ACK や cleanup 完了通知を待たず、`worker_proc.wait(timeout=cleanup_deadline - 2s - now)` を行う。timeout 時は worker を SIGKILL し、必ず `worker_proc.wait(timeout=cleanup_deadline - now)` を再実行する。reap 不可なら `worker_not_reaped` とし rmdir と次 mission を禁止する。

#### 4.2.3 cleanup 順序の正本

すべての段に発行済み absolute cleanup deadline を適用する。`launcher_proc` を wait / reap するのは mission worker だけ、`worker_proc` を wait / reap するのは WorkerRunner だけである。

| 経路 | 正本の順序 |
|---|---|
| handoff 後の正常時 | receiver-owned STOPPING → drain → receiver の CLOSED / listener close → `cgroup.kill` → `populated 0` → worker の `launcher_proc.wait()` / reap → WorkerRunner の `worker_proc.wait()` / reap → afx の保険 kill / 空確認 → rmdir |
| handoff 後の receiver 異常 | receiver 自身が最外周で異常確定 → 新規 RECV / commit 禁止 → drain。空なら receiver の CLOSED / close、空でなければ STUCK。後続は該当行の順序 |
| handoff 後の STUCK | STUCK 確定 → listener を開いたまま RECV / commit 禁止 → `cgroup.kill` → `populated 0` → worker の `launcher_proc.wait()` / reap → WorkerRunner の `worker_proc.wait()` / reap（worker exit で listener と複製が消滅）→ afx の保険 kill / 空確認 → rmdir |
| handoff 前 setup 失敗 | worker が所有する未移譲 listener fd（存在する場合）と status 親端を close → `cgroup.kill` → `populated 0` → worker の `launcher_proc.wait()` / reap → WorkerRunner の `worker_proc.wait()` / reap → afx の保険 kill / 空確認 → rmdir |
| handoff 後 setup / runtime 失敗 | `GO` 送信失敗、`wait_exec` の `EXEC_FAILED` / order / malformed / timeout、`cli_exec_unproven`、`spool_flood` は receiver-owned STOPPING → drain → CLOSED、または STUCK の系列に入る。その後は該当する handoff 後の行に従う |

EOF、cleanup ACK、`populated 0` は worker exit の証拠ではない。最初の worker wait の timeout は SIGKILL 後の再 wait を必要とし、deadline までに reap できなければ `worker_not_reaped` とする。CLOSED、STUCK ack、`populated 0`、direct-parent reap、保険 kill / 確認、rmdir は全て同じ deadline 内に終える。

### 4.3 CONNECT proxy

proxy は worker thread 群に置き、outer Landlock 前に `127.0.0.1:ephemeral`、backlog 128 の listener を作る。CONNECT authority は backend 定数だけとし、claude は `api.anthropic.com`、codex は `chatgpt.com` と `ab.chatgpt.com`、opencode は空集合とする。telemetry と opencode の外向き host は許可しない。

host ごとに最初の `getaddrinfo(host,443,AF_UNSPEC,SOCK_STREAM)` を一度だけ行い、全 address が global であることを要求する。解決集合は mission 中固定する。loopback、RFC1918、CGNAT、link-local、ULA、multicast、unspecified、documentation、reserved、非 global を埋め込む mapped / compatible / 6to4 / NAT64 は拒否する。resolver は上限 4 thread、5 秒で 504、NXDOMAIN と不適格集合は 502 とする。各 address を最大 5 秒、全体 15 秒で順に試す。

| parser state | 契約 |
|---|---|
| REQ_LINE | `CONNECT SP authority SP HTTP/1.1 CRLF` の 3 要素だけ。行は 1,024 byte 以下 |
| authority | ASCII 小文字 hostname、user info / IP literal / terminal dot / uppercase / IDN を拒否。port は `443` の完全一致 |
| HEADERS | 32 行、各 1,024 byte、全体 8 KiB。裸 LF、obs-fold、NUL、control、重複 Host / Proxy-Authorization を拒否。Host は authority と完全一致 |
| DONE | 同一 recv の ClientHello 先読みを保存し、upstream 接続後に送る |
| timeout / EOF | accept から 5 秒、EOF は応答せず close |

relay は CONNECTING、OPEN、HALF_CLOSED、CLOSED の状態を持つ。tunnel 上限は 32、上限時は accept 後に 503、idle は 300 秒とする。

#### 4.3.1 同期 handoff

pending entry は複製 fd、`fstat().st_ino`、connect 後の client tuple、ack Event、ack tuple を持つ。`SO_COOKIE` は `dict[cookie, list[entry]]` の索引だけであり、同一性の根拠にはしない。

1. supervisor は commit lock 内で pending entry を bucket に追加し、connect 成功 / EINPROGRESS 後に client tuple を記録する。
2. proxy accept thread は待たずに `getpeername`、client tuple と server tuple の AF_INET exact INET_DIAG lookup を行う。
3. 両側が ESTABLISHED であり、bucket 内で「複製の現在の `getsockname` が peer、client `idiag_inode` が entry inode、server `idiag_inode` が accept fd inode」を満たす entry がちょうど 1 件なら受理する。
4. client lookup をもう一度行い inode と cookie が不変であることを確認し、peer tuple を添えて ack を立てる。
5. supervisor は 1 秒まで ack を待ち、tuple が一致すれば tracee に実値を返す。不一致・timeout は複製を `shutdown(SHUT_RDWR)` して ECONNREFUSED とする。

0 件、2 件以上、lookup 消失、state 不一致、再 lookup 不一致、IPv4-mapped、supervisor を経ない接続は要求行を読まずに close する。NETLINK_SOCK_DIAG を作れなければ `proxy_start_failed` とする。

### 4.4 nested Landlock

nested Landlock は `CONNECT_TCP` と `BIND_TCP` を規則なしで handled にする。明示 bind は Landlock が EACCES、bind 無し listener は seccomp の `listen` EPERM が閉じる。`sendto` の destination と MSG_FASTOPEN を拒否し、TFO の直接経路を作らない。ABI は 8 以上、TSYNC 必須、fallback はなしとする。

## 5. outer Landlock

`/dev` は directory 規則で許可しない。

| file | 権限 |
|---|---|
| `/dev/null` | READ_FILE、WRITE_FILE、TRUNCATE |
| `/dev/urandom`、`/dev/random`、`/dev/zero` | READ_FILE |
| `/dev/tty`、`/dev/shm`、`/dev/pts/*`、`/dev/mqueue`、その他 | 与えない |

`LANDLOCK_ACCESS_FS_IOCTL_DEV` を handled に加え、どの device file にも与えない。file rule は character device を検査し、dir 専用 bit を混在させない。`/dev/fd/N` は pipefs のため rule を要しない。

## 6. mission cgroup と systemd admission

### 6.1 topology、unit、運用前提

```text
<afx unit>/  Delegate=pids memory、root に process を置かない
  ├─ supervisor/     afx 本体 (unit main PID) と mission worker
  └─ m-<nonce>/      CLI 部分木だけ。pids.max、memory.max、memory.swap.max=0
```

unit は `Delegate=pids memory`、`Type=exec`、`NotifyAccess=none`、`KillMode=control-group`、`0 < TimeoutStopUSec < infinity`、`SendSIGKILL=yes`、`FinalKillSignal=9`、`TimeoutStopFailureMode=terminate` または `kill` を満たす必要がある。`abort`、他の Type、他の NotifyAccess、`TimeoutStopUSec=0`、`infinity` は `unit_stop_policy_unsupported` とする。

afx は unit の main PID であり、unit root の `cgroup.procs` に自分だけが居ることを確認してから `supervisor/` に自己移動し、`+pids +memory` を有効化する。screen または shell wrapper を挟む形は supported ではない。

運用前提として、afx process の存続中は実効 unit property を変更・reload しない。変更時は afx を再起動して admission を再実行する。起動後の drop-in 変更と `daemon-reload` は検出しない。非信頼 CLI は unit file と user bus に到達できない前提である。

### 6.2 cgroup の操作と回収

| 段 | 主体 | 契約 |
|---|---|---|
| 起動時 | afx 本体 | delegation、topology、probe、前回 `m-*` の回収を確認。失敗は backend を fail closed |
| mission 開始 | worker | cgroup を作成し interface fd を保持 |
| launcher join | worker | HELLO 後に launcher pid を `cgroup.procs` へ書き、読み戻して JOINED |
| 隔離後 | worker | path / openat による cgroup 操作は EACCES。保持 fd だけを使う |
| cleanup | worker | §4.2.3 に従い `cgroup.kill`、`populated 0` を deadline まで待つ |
| rmdir | afx 本体 | reap 後に保険 kill、`populated 0` と empty `cgroup.procs` を確認して rmdir |

`cgroup_cleanup_timeout`、`cgroup_not_empty`、`cgroup_rmdir_failed`、`worker_not_reaped` のいずれかは次 mission を禁止する。部分木は cgroup interface fd を exec 後に持たない。

### 6.3 unit の同定と固定問い合わせ

afx は `sd_pid_get_user_unit(0,&unit)` を libsystemd から一度だけ呼ぶ。dlopen、symbol、負の return、null pointer、UTF-8 decode、`.service` grammar のいずれかの失敗は `cgroup_delegation_missing` とし fallback を持たない。ctypes は `argtypes=[c_int, POINTER(c_void_p)]`、`restype=c_int` とし、copy 後に `libc.free(c_void_p)` を必ず一度呼ぶ。

実行 file は root 所有かつ group / other 非書込みの `/usr/bin/systemctl`、なければ `/bin/systemctl` とする。固定 argv は次である。

```text
[path, "--user", "--no-pager", "show",
 "-p", "Delegate", "-p", "DelegateControllers", "-p", "MainPID",
 "-p", "Type", "-p", "NotifyAccess", "-p", "KillMode",
 "-p", "TimeoutStopUSec", "-p", "SendSIGKILL", "-p", "FinalKillSignal",
 "-p", "TimeoutStopFailureMode", "-p", "TimeoutAbortUSec", "--", unit]
```

sanitized env は `PATH=/usr/bin:/bin`、`LANG=C`、`LC_ALL=C`、pager 無効、`SYSTEMD_LOG_LEVEL=err`、uid から作る `XDG_RUNTIME_DIR` だけとする。stdin は `/dev/null`、stdout は 4 KiB、timeout は 5 秒、`close_fds=True` とする。

11 key はちょうど各 1 回でなければならない。重複、未知 key、`=` 無し、空出力、欠落は fail である。

| key | 要求値 |
|---|---|
| `Delegate` | `yes` |
| `DelegateControllers` | `pids` と `memory` を含む集合 |
| `MainPID` | afx 自身の decimal pid |
| `Type` | `exec` |
| `NotifyAccess` | `none` |
| `KillMode` | `control-group` |
| `TimeoutStopUSec` | 正の有限値 |
| `SendSIGKILL` | `yes` |
| `FinalKillSignal` | `9` |
| `TimeoutStopFailureMode` | `terminate` または `kill` |
| `TimeoutAbortUSec` | duration として形式妥当 |

delegation の不足は `cgroup_delegation_missing`、MainPID / root topology は `cgroup_topology_unsupported`、停止 property は `unit_stop_policy_unsupported` とする。systemd user manager がない環境は fail closed である。

### 6.4 subreaper

launcher は seccomp 前、exec 前に `PR_SET_CHILD_SUBREAPER` を設定する。CLI process だけが subreaper であり、worker と afx 本体はならない。`clone(CLONE_PARENT)` は EPERM として worker を CLI 部分木の parent にする経路を閉じる。

CLI が回収しない zombie は mission の `pids.max` を消費する自己妨害である。`cgroup.kill` は生存 task を kill し、zombie は CLI 死亡後に systemd user manager など次の subreaper が reap する。zombie は `populated` と rmdir を妨げない。

### 6.5 unit 移行

人が unit の `ExecStart` を afx の直接 exec に変更し、screen / shell wrapper を除く。`Type=exec`、`NotifyAccess=none`、`Delegate=pids memory`、`KillMode=control-group`、有限の `TimeoutStopSec`、`SendSIGKILL=yes`、`FinalKillSignal=SIGKILL`、`TimeoutStopFailureMode=terminate` を明示する。reload と restart 後に §6.3 の全 property、MainPID の command、root と `supervisor/` の process を確認する。外れた場合は人が保存済み unit へ rollback する。

## 7. setup の証明と固定 reason

### 7.1 status protocol

worker は `launcher_proc` の前に `SOCK_SEQPACKET` socketpair を作り、child end だけを `pass_fds` する。frame は magic `AFXL`、u16 version、u16 type、128 bit nonce、type 固定 fields とする。`recvmsg(MSG_CMSG_CLOEXEC)` の truncation、長さ、magic、version、nonce 不一致は malformed とする。ancillary は PREPARED の SCM_RIGHTS fd 一つだけとする。

| launcher 段 | 動作 |
|---|---|
| L1–L3 | status CLOEXEC、PDEATHSIG / ppid、rlimit、seccomp 継承なし、credential を検査 |
| L4 | HELLO{pid} を送る。JOINED を厳密に検査 |
| L5 | subreaper、no_new_privs、nested Landlock TSYNC |
| L6–L7 | seccomp install、listener 取得、seccomp=2 と no_new_privs=1 を自己検査 |
| L8 | PREPARED と listener fd 一つを送る。送信成否によらず launcher copy を close |
| L8w | GO を厳密に検査 |
| L9 | `close_range(...,CLOEXEC)` 後、hash 済み backend execution artifact fd を `execveat`。codex は native ELF (`codex-rs`、static PIE musl) の fd とし、npm wrapper は実行 TCB に含めない。失敗は EXEC_FAILED |

| worker state | 判定 |
|---|---|
| wait_hello | nonce と `launcher_proc.pid` が一致する HELLO を cgroup join して JOINED |
| wait_prepared | profile / hash / ABI / flags と listener fd 一つを検査し、supervisor 起動後に GO |
| wait_exec | EOF 後の `/proc/<pid>/exe` の `(st_dev, st_ino)` が、open 時に同じ backend execution artifact fd から保存した `(st_dev, st_ino)` と一致すれば `cli_started`、確認不能なら `cli_exec_unproven` |
| 任意 state | order、malformed、deadline、EOF は固定 reason |

handoff 前の `sandbox_setup_failed`、`attestation_*`、`launcher_died` は §4.2.3 の handoff 前系列に入る。GO 送信失敗、`wait_exec` の EXEC_FAILED / order / malformed / timeout、`cli_exec_unproven` は handoff 後系列に入る。

| fd | close 所有権 |
|---|---|
| status child end | launcher が CLOEXEC / `_exit`、worker は Popen 直後の copy を close |
| status parent end | worker が started / unproven / failure の確定で close |
| seccomp listener、handoff 前 | launcher が L8 で close。worker は未移譲 listener fd が存在すれば close |
| seccomp listener、handoff 後 | receiver だけが close。PREPARED 不正など handoff 前は worker が受信 fd を close |
| backend execution artifact fd | worker は Popen 直後、launcher は exec 時に close。codex は native ELF (`codex-rs`、static PIE musl) の fd であり、npm wrapper は実行 TCB に含めない |
| cgroup / spool / proxy / diag fd | launcher は持たず、worker が mission 終了まで保持 |

### 7.2 固定 reason

worker は launcher 起動前、backend execution artifact を open した同じ fd で `fstat` と先頭 bytes の検査を行い、ELF magic、class、machine を検証する。non-ELF、shebang、別 arch の artifact は admission の固定 reason `cli_artifact_unsupported` とし、`attestation_exec_failed` まで進めない。この判定は L9 の前段、すなわち worker が fd を open した時点で行う。

| 発生点 | reason / 分類 |
|---|---|
| admission | `arch_unsupported`、`landlock_abi_too_old`、`seccomp_unavailable`、`seccomp_user_notif_unavailable`、`seccomp_wait_killable_unavailable`、`pidfd_thread_unavailable`、`ptrace_scope_unsupported`、`cli_artifact_unsupported`、`cgroup_*`、`unit_stop_policy_unsupported`、`spool_create_failed`、`proxy_start_failed`、`runtime_fingerprint_selftest_failed`、`safety_canary_failed` は `sandbox_unavailable` |
| setup | `inherited_seccomp_filter`、`credentials_unexpected`、`rlimit_failed`、`subreaper_failed`、Landlock / seccomp / self-check failure、`attestation_*`、`launcher_died`、`cgroup_join_failed` は `sandbox_unavailable` |
| runtime | `supervisor_died`、`supervisor_stalled`、`proxy_died` は `sandbox_unavailable`。`spool_flood` と `cli_exec_unproven` は `failed` |
| cleanup | `cleanup_deadline_exceeded` は `sandbox_unavailable`。`worker_not_reaped`、`cgroup_cleanup_timeout`、`cgroup_not_empty`、`cgroup_rmdir_failed` は次 mission gate を閉じる |

## 8. fingerprint と admission

fingerprint は profile、canonical seccomp hash、BPF byte hash と flags、nested / outer Landlock hash、egress constants、launcher source hash、supervisor / proxy / spool / cgroup / protocol / canary source hash、kernel capability、userspace、backend execution artifact fd の SHA-256 と `(dev,ino,size,mtime)` の正規化組、`launch_kind`、argv / env-policy の hash と aggregate SHA-256 とする。codex は native ELF fd の SHA-256 と metadata を含め、npm wrapper / script の hash は含めない。codex の `launch_kind=native_elf` とし、managed 環境変数を加えない env-policy を hash 対象にする。

worker は backend execution artifact を `O_RDONLY|O_CLOEXEC` で開き、同じ fd を hash して launcher に渡し `execveat(AT_EMPTY_PATH)` する。path を hash 後に再 open しない。対応済み集合は最終 profile の全自己試験前は空とする。

集合外 fingerprint は次を順に通す。

1. 安全性 canary: AF_UNIX 拒否、不許可 port、IPv4 / IPv6 bind 無し `listen` の EPERM、`CLONE_PARENT` の EPERM、外部 signal、`/dev/tty`、cgroup file、共有 ro fd metadata、subreaper、proxy 外部接続、順逆 port reuse、bucket collision、receiver close race を、実 launcher、実 user service、実 cgroup、実 supervisor、実 proxy で検査する。
2. 互換性自己試験: 実 CLI、spool tool、schema、SIGSYS 0、終了後 cgroup empty を検査する。

成功と恒久失敗は process 内 cache する。一時的 timeout、spawn、provider failure は cache せず `SELFTEST_RETRY_INTERVAL_SEC` 後に再試行する。CLI 自動更新の source host は許可しない。hash が変われば gate を再評価する。

## 9. 不変条件

| ID | 不変条件 |
|---|---|
| IC-NR | 全番号 table、ceiling、policy hash、BPF、版は 1 対 1。未分類と重複は生成失敗 |
| IC-NET | §4.2 の段を満たす supervisor 代行、proxy の inode + tuple handoff、固定 global address 集合だけ。UDP、raw、netlink、直接 TFO、listener は不可 |
| IC-SIG | nested Landlock scope が部分木外 signal と abstract UDS を拒否 |
| IC-DEV | `/dev/shm`、PTY、tty、非許可 device と device ioctl に到達不能 |
| IC-FD | backend execution artifact exec 時の fd は 0/1/2 だけ。status、backend execution artifact、cgroup fd は CLOEXEC。全 backend で実際に exec する artifact と同じ fd を hash・exec する |
| IC-SPOOL | 同一 dir NOREPLACE、single claim、bounded validation、rescan 正本、worker ledger 正本 |
| IC-LIFECYCLE | direct-parent wait / reap、receiver-only listener close、handoff 前後の §4.2.3 系列、STUCK、`worker_not_reaped` gate を守る |
| IC-FINITE | forward progress する制御経路の下で、単一 absolute deadline、ACK 非依存 wait、SIGKILL 後再 wait、固定 reason を守る |
| IC-CRED | launcher は zero capability と期待 credential を検査し、setter は拒否 |
| IC-ATTEST | setup 判定は status protocol だけ。ancillary は PREPARED の fd 一つだけ |
| IC-FP | 全 backend で実際に exec する artifact と同じ hash 済み fd を exec。集合外 fingerprint は canary と自己試験前に exec しない |
| IC-NOKEY | 隔離を緩める設定 key を作らず、必要 capability / topology / unit policy 不足は fail closed |
| IC-META | path / fd 形の全 metadata 変更を EPERM |
| IC-WORKER | worker は seccomp / subreaper なし。worker から子への kill と cgroup kill は通り、supervisor / proxy fault は mission failure |

## 10. 受入条件

実子 process、実 kernel、Landlock domain、seccomp user-notif、cgroup、実 CLI fallback を使い、境界の実体をモックしない。

1. 三 backend が最終 BPF、subreaper、spool、timeout / abort、opencode resume、SIGSYS 0、empty cgroup を通す。codex は fd `utimensat` EPERM 下で完走する。
2. 初回 login、token refresh、自動更新 gate、claude telemetry 拒否を検証する。
3. proxy / 8080 以外、別 uid localhost、`::1`、mapped IPv6、任意 host を拒否し、bind 無し listener は IPv4 / IPv6 とも EPERM、bind は EACCES とする。
4. blocking / nonblocking connect、再 connect、thread、孫、subreaper 孤児、addrlen 境界、fd 差替え、tracee death、signal、飽和 EAGAIN を検証する。
5. TFO、destination 付き `sendto`、FASTOPEN `sendmsg` / `sendmmsg` が迂回不能である。
6. UDP、raw、ICMP、SCTP、AF_PACKET、AF_VSOCK、AF_UNIX、AF_NETLINK を拒否し、AF_UNIX socketpair は許可、全 `listen` は EPERM とする。
7. CONNECT parser の空白、header、authority、resolver special range、SNI / Host 不一致の残余を検証する。
8. 部分木外 signal、pgid、signal 0 を拒否し、部分木内 signal と worker からの killpg を許可する。
9. priority / scheduler / other-pid `prlimit64` を拒否し、pid 0 の hard limit 制約を検証する。
10. capability / credential mismatch の launcher fail と setter EPERM を検証する。
11. fork bomb と memory 上限、AIO、mlock、uid-wide inotify 残余を検証する。
12. membarrier GLOBAL、危険 prctl / fcntl / ioctl、fanotify、mount list、時計・hostname・sync の errno を検証する。
12b. `clone3` の ENOSYS fallback と EPERM 差分、POSIX timer EPERM、filter flag と LOG 不在を検証する。
13. `/dev/shm`、PTY を拒否し、許可 device と ioctl EACCES を検証する。
14. path / fd metadata、O_PATH、共有 ro fd、workdir fd の不変性と read-only 残余を検証する。
15. lock の到達範囲、共有 ro lock の外部待機、kill による解放、write lock EBADF を検証する。
16. spool の symlink、hardlink、伸長、replay、先置き、競合、overflow、flood、JSON 制限、EXDEV を検証する。
17. budget 境界と allowed 外 request を dispatcher が拒否する。
18. `setsid` / double-fork 後も cgroup を空にし、`CLONE_PARENT` 拒否と `CLONE_PARENT_SETTID` 許可を検証する。
19. cgroup 脱出、親 interface 操作、exec 後 fd 消滅を検証する。
20. delegation、stale cgroup、mkdir / join / populated / rmdir failure が固定 reason と次 mission 禁止になる。
21. stderr と exit code を setup failure の根拠にしない。
22. protocol frame 異常、early / late setup failure fault injection、handoff 前 worker close、handoff 後 receiver-only close、direct-parent wait / reap を検証する。
23. inherited filter、TSYNC、listener send、WAIT_KILLABLE、PIDFD_THREAD、Yama の fail closed を検証する。
24. `pidfd_getfd`、memory copy、receiver stop の supervisor failure を検証する。
25. 全 backend で実際に exec する artifact と同じ fd を hash・exec すること、artifact mutation、inode-fixed exec、canary、temporary retry、空の supported set を検証する。
26. data evaluator と独立 BPF evaluator で全 nr、x32、ceiling、12 COND partition を一致させる。
27. unclassified、duplicate、unknown header name が generator failure になる。
28. seccomp、nested Landlock、supervisor allowlist、outer `/sys` rule の外した対照を置く。
29. ID_VALID の四 seam、pidfd failure、short read、SEND ENOENT で resource が破棄される。
30. slot saturation、receiver / handler exception、blocking connect、RECV / SEND errno、登録前例外、drain、STUCK、cleanup deadline を fault injection する。
31. 実 CLI 四種で subreaper、orphan、zombie、worker child 集合、孤児の Yama failure を検証する。
32. proxy incremental parser、slowloris、half-close、resolver、relay fault、pending cleanup、supervisor 外接続を検証する。
33. `systemctl` / libsystemd / hostile output / 11 property / topology / EXTEND_TIMEOUT 拒否を検証する。
34. 実 BPF の拒否連打が audit log を増やさない。
35. BPF jump、width、errno、arch、mask、dispatch、action、default、ceiling、fallthrough、endianness、flag mutation を検出する。
36. listener close race、poll、ENOENT、receiver 以外の close 経路無し、四 stop seam を検証する。
37. canary と adversarial suite の listener、handoff、clone、systemctl 項目を一致させる。
38. 四 stop seam を 1,000 回ずらし、ENOSYS / EPERM / actual result / accept count / CLOSED を検証する。
39. 順逆 port reuse、bucket collision、duplicate entry、inode 比較、disconnect、mapped IPv6、CapEff=0 を検証する。
40. fork、vfork、posix_spawn、thread、`clone3` fallback、subreaper child を実 syscall trace で検証する。
41. EAGAIN と 503 注入下の三 CLI を検証し、失敗 fingerprint を登録しない。
42. disposable user service で unit name、11 property、self move、wrapper / Delegate 対照、unit removal を検証する。
43. STUCK blocking connect を実 cgroup で fault inject し、listener open、new tracee block、direct-parent reap、SIGKILL 後再 wait、rmdir gate、commit 後 stop を検証する。
44. cleanup request 前、deadline 受領前、ACK 後の worker 停止を fault inject し、deadline 一回発行、ACK 非依存 wait、SIGKILL 後再 wait、`worker_not_reaped` gate を検証する。
45. `pids.max` 到達時の zombie、`populated 0`、rmdir、次 subreaper、外部 process 非影響を検証する。
46. disposable `Type=exec` / `NotifyAccess=none` service の stop が有限 timeout 後 cgroup 全体を SIGKILL し、unit fixture と admission 負例を検証する。
47. codex の npm wrapper（shebang）を artifact に設定すると `cli_artifact_unsupported` となり、CLI が exec されないことを検証する。

## 11. task 表と依存

| # | 内容 | 依存 |
|---|---|---|
| A1 | `/dev` file rule と互換回帰 | なし |
| A2 | policy artifact、generator、独立 evaluator、mutation | なし |
| T2 | nested Landlock | D-2 |
| T3a | unit admission、cgroup topology、cleanup gate | D-12、D-19、D-23、D-24 |
| T3b | launcher、status protocol、close table | A2、T2 |
| T3c | CLI subreaper | T3b |
| T3d1 | notification、receiver、handoff、watchdog | A2、T3b |
| T3d2 | cgroup lifecycle、deadline、direct-parent reap | T3a、T3d1 |
| T4a | CONNECT parser | なし |
| T4b | resolver、range、fixed address set | なし |
| T4c | relay、tunnel lifecycle | T4a、T4b |
| T4d | proxy / supervisor synchronous handoff | T3d1、T4c |
| T5 | spool dispatcher / shim、UDS removal | なし |
| T6 | fingerprint、canary、selftest | T3c、T3d2、T4d、T5 |
| T7a | product BPF recheck | A2、T3b、T3d1 |
| T7b1 | seccomp kernel test | T3d1 |
| T7b2 | connect、receiver race、handoff、STUCK test | T3c、T3d1、T4d |
| T7b3 | process、deadline、zombie test | T3c、T3d2 |
| T7b4 | FS、Landlock、contrast test | T3d2 |
| T7c | cgroup / systemd test | T3a、T3d2 |
| T7d | spool / proxy adversarial test | T4d、T5 |
| T7e | final E2E、fingerprint registration | 全部 |

有効化は T7e と §12.1 の全項目が済の後とする。A1 の main 投入は opencode 正常回帰を前提とする。順序は A2 / T2 / T3a / T4a / T4b / T5、次に T3b / T3c / T3d1 / T3d2 / T4c、次に T4d / T6 / T7a、最後に fault injection 群と T7e とする。

## 12. 残余リスク

- allowlist 外 path の metadata 読取りと O_PATH は残る。
- High: proxy は address 集合のみを制限し、TLS SNI と HTTP Host を検査しない。同一 CDN / anycast address の他 site / tenant に届き得る。
- 最初の DNS 解決が外部 resolver / hosts により攻撃者 global address を返す問題は閉じない。
- allowlist host は mission 外用途にも使用され得る。opencode は llama-swap 全 endpoint に届く。
- 共有 ro file の `flock` と read lock は外部 process を mission 終了まで待たせ得る。
- uid 単位の inotify、watch、pipe、epoll、pending signal と disk capacity は共有資源である。
- supervisor / proxy は worker 内で動くため部分木が可用性を圧迫し得る。slot、tunnel、resolver 上限は互換性 failure を起こし得る。
- INET_DIAG の挙動変更は fail closed の可用性低下になる。proxy listener への外部接続は backlog を一時圧迫し得る。
- CLI zombie は `pids.max` を自己消費する。Yama >= 2、ABI < 8、未計測 AppArmor は admission で fail closed とする。
- cleanup watchdog は GIL 停止時 best-effort である。IC-FINITE は deadline 発行・監視・SIGKILL 配送を担う制御経路の forward progress を前提とする。
- afx が停止したまま誰も unit を stop / restart しない間には終了上限がない。systemd の停止層は afx の hang を自動検出しない。`NotifyAccess=none` のため sd_notify と WatchdogSec は範囲外である。
- 起動後の unit property 変更・reload は検出しない。§6.1 の運用前提に従う。
- inode 固定は rename による差替えを防ぐが、同一 inode への in-place 書込み（npm 更新との同時実行を含む）は防がない。運用前提として稼働中に backend を更新せず、更新後は fingerprint を再評価する。
- `/proc/<pid>/exe` は観測時点の証明であり、CLI 自身が別 executable へ exec することは禁じない。その挙動は既存の seccomp / Landlock の範囲で許す。

### 12.1 readiness

| # | 条件 |
|---|---|
| R1 | libsystemd ctypes 契約と failure test |
| R2 | disposable service、11 property、停止層の正負例 |
| R3 | `pids.max`、`memory.max`、worker NOFILE の実測固定 |
| R4 | ABI 8、user notification、WAIT_KILLABLE、PIDFD_THREAD、INET_DIAG、Yama の admission |
| R5 | inode handoff、collision、disconnect、mapped IPv6 |
| R6 | STUCK、blocking connect、commit 後 stop、実 cgroup kill |
| R7 | deadline emission fault injection、wait / reap / re-wait |
| R8 | product BPF、canary、mutation、handoff 前後 cleanup、direct-parent wait |
| R9 | supported fingerprint 集合が空 |
| R10 | backend ごとの cold、reject、saturation test |
| R11 | opencode が拒否下の最終 profile で正常完走するまで enable / registration しない |
| R12 | cleanup、restart、external systemd supervision |
| R13 | A1 / A2 merge checklist |
| R14 | human による仮運用 unit の移行 |

## 13. 設計裁定

| ID | 裁定 |
|---|---|
| D-1 | MCP は同一 dir rename の spool |
| D-2 | minimum Landlock ABI は全 backend で 8 |
| D-3 | opencode は `127.0.0.1:8080` だけ。外向き host は拒否し、正常回帰まで blocker |
| D-4 | `/dev` は individual file rule と IOCTL_DEV |
| D-5 | metadata 変更を path / fd 形とも拒否、読取りは残余 |
| D-6 | 全 backend で実際に exec する artifact と同じ fd による fingerprint admission、inode-fixed exec、artifact hash、canary と selftest |
| D-7 | 全番号 explicit classification、未分類は generator failure |
| D-8 | `pidfd_getfd` connect delegation と CONNECT proxy |
| D-9 | delegated cgroup、`cgroup.kill`、pids / memory、absolute cleanup deadline |
| D-10 | ABI 6 / 7 の codex は fail closed。D-2 で通常は不要 |
| D-11 | claude telemetry は allowlist に入れない |
| D-12 | libsystemd unit name と固定 `systemctl show` による delegation admission |
| D-13 | SNI 不照合を address-set guarantee の High residual として受容 |
| D-14 | venv rule 縮小は lock 問題の解決策ではない |
| D-15 | proxy 利用者は inode + tuple。cookie は bucket index のみ |
| D-16 | rlimit hard 0 と POSIX timer EPERM、SIGPENDING は 0 にしない |
| D-17 | pids / memory は profile constants とし実測で決定 |
| D-18 | launcher が CLI subreaper、`CLONE_PARENT` EPERM |
| D-19 | afx は service main PID。screen / wrapper は unsupported |
| D-20 | seccomp LOG は外す |
| D-21 | slots 16、handler 2s、tunnel 32、cleanup 10s、ack 1s |
| D-22 | `listen` は無条件 EPERM |
| D-23 | libsystemd failure は fallback なしで fail closed |
| D-24 | unit 停止層の全 property を起動時 admission で検査 |

## 14. 確定計測値

根拠データは `tmp/design-improve-seccomp/measure/proto-results/*.json`、`measure/p4.md`、`measure/syscall-map.md`、`measure/raw-traces*.tar.gz` に置く。ここには設計値を確定した結果だけを残す。

未完了の互換性検証は §12.1 の readiness と受入条件で扱い、計測逐次ログには含めない。

| 項目 | 確定値 |
|---|---|
| 環境 | kernel 7.0.0-38、Landlock ABI 8、Ubuntu 26.04.1、systemd 259、Yama 1、inotify instance 128 |
| seccomp | ALLOW 202、COND 12、NOTIF 1、EPERM 150、EACCES 1、ENOSYS 106、ceiling 472 |
| connect burst | 10 ms 最大 1、100 ms 最大 5、1 秒最大 8 |
| handler | peak 2、p50 0.071 ms、p99 0.18 ms、最大 0.33 ms |
| tracee connect | loopback 最大 4.9 ms |
| proxy tunnel | claude 最大 8、codex 4、opencode 2 |
| handoff | p50 0.056 ms、p99 0.21 ms、最大 17 ms |
| cgroup cleanup | `cgroup.kill` から `populated 0` は 23〜33 ms、pids 到達時 5〜26 ms |
| listener close | stop から CLOSED は 0.13 ms |
| systemd stop | finite 2 秒 service が stop 開始から 2.18 秒で main・子・孫を SIGKILL |
| CLI paths | claude / codex は `listen` と `CLONE_PARENT` EPERM、fd metadata deny 下で完走。opencode の最終 profile 正常完走は未達 |
| codex execution artifact | native musl ELF。path は `runner.codex.bin`（`config/settings.yaml.example` の例） |

## 15. 変更履歴

|日付|版|変更|理由|commit|
|---|---|---|---|---|
|2026-10-09|v1.0|C0 revision 0.12 (設計レビュー 12 周、r12 で収束) を公開 spec として清書|実装着手条件の固定|—|
|2026-10-09|v1.1|§1.3: MCP transport の置換 (spool、`afx.sock` 廃止) は trade にも適用と明記。§3.4: `perf_event_open` を EPERM 列から外す (分類表の EACCES 1 件が正)。§3.1 の policy hash は canonical 形を `nr\tname\taction\tcondition_id` (条件定義を含む) に改めた後、実装時に更新する|第 1 波ブリーフの要確認 (sol 助言 2026-10-09)|—|
|2026-10-09|v1.2|§3.1 policy hash を canonical 形改定後の値に更新、BPF 命令数と filter flags を記載|A2 実装 (sc-a2) の受領|—|
|2026-10-09|v1.3|codex の実行 artifact を native ELF に固定 (shebang wrapper は cli_artifact_unsupported)、wait_exec は保存した (st_dev, st_ino) と比較、fingerprint から wrapper を除外|T3b 実装で nvm の codex (Node の #! script) が execveat(AT_EMPTY_PATH) で ENOENT と実測|—|
