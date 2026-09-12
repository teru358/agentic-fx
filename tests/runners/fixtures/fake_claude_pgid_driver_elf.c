/* `test_worker_runner_reaps_real_cli_pgid_under_improve_profile` 専用の
 * fake claude CLI — improve profile (Landlock, core/landlock.py の
 * `elf_interpreter()` が `claude.bin` に ELF 実体であることを要求する)
 * 向けに、shebang script ではなく実際の ELF バイナリとしてビルドする。
 *
 * /code-review 2 周目 CR2 是正 (2026-09-12): 設計書 §D (T-D) 是正後、
 * `ClaudeRunner._build_argv` は `mission.prompt` を argv に積まない
 * (workdir/prompt.txt 経由の stdin に回る) ため、この fake CLI は argv の
 * 中身を一切読まない — どんな argv 列 (`-p --output-format stream-json
 * --verbose --json-schema <json> ...`) を渡されても無視して同じ動作を
 * する。これにより「実際の ClaudeRunner が組み立てる argv をそのまま
 * 渡せる」実プロセス pin が、shebang script (Landlock の ELF-only gate
 * に弾かれる) や `/bin/sh` (argv を `-p <script>` として解釈する古い
 * 挙動に依存していた) を使わずに成立する。
 *
 * 自身の pid と、PDEATHSIG を継がない孫プロセスの pid を cwd 直下の
 * 固定ファイル名へ書く。孫の生存が `WorkerRunner._terminate_cli_pgid` の
 * pgid 単位回収 (SIGTERM→grace→SIGKILL のエスカレーション) の観測対象
 * になる。SIGTERM は自分・孫の両方が無視する (エスカレーションが実際に
 * 発火することを確認するため)。
 */
#include <signal.h>
#include <stdio.h>
#include <unistd.h>

static void ignore_signal(int sig) { (void)sig; }

int main(void) {
    signal(SIGTERM, ignore_signal);

    FILE *self_marker = fopen("fake_claude_pid", "w");
    if (self_marker != NULL) {
        fprintf(self_marker, "%d", (int)getpid());
        fclose(self_marker);
    }

    pid_t grandchild = fork();
    if (grandchild == 0) {
        /* 孫: setsid しない (親と同じ pgid のまま) — 自分で prctl を
         * 呼ばないため PDEATHSIG も継がない。SIGTERM を無視して、
         * SIGKILL への昇格が実際に発火することを観測できるようにする。 */
        signal(SIGTERM, ignore_signal);
        FILE *gc_marker = fopen("fake_claude_grandchild_pid", "w");
        if (gc_marker != NULL) {
            fprintf(gc_marker, "%d", (int)getpid());
            fclose(gc_marker);
        }
        for (;;) {
            pause();
        }
    }

    for (;;) {
        pause();
    }
    return 0;
}
