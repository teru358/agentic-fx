#!/usr/bin/env bash
# 実機受入の観測コマンド (docs/operations/first-run-acceptance.md の観測表) を順に実行し、
# 出力を 1 つの tar.gz にまとめる。読み取りだけで、unit・設定・データは変えない。
# 依存: bash、coreutils、tar、sed、python3 (標準ライブラリのみ)、git、systemctl / journalctl / loginctl (無くても続行)。
# agentic-fx のコードは import しない。診断は fail closed にしない: 取れないものは「取得できず」と記録して続け、終了コードは 0。
set -u
umask 077

usage() {
    printf '%s\n' 'usage: collect-first-run-diagnostics.sh [--root DIR] [--out FILE]'
    printf '%s\n' '  --root DIR   repo の root (既定: このスクリプトの位置から決める)'
    printf '%s\n' '  --out FILE   出力する tar.gz (既定: <root>/tmp/acceptance/diag-<UTC 時刻>.tar.gz)'
}
die() { printf '%s\n' "$1" >&2; exit 2; }

root_override=''
out_override=''
while (($#)); do
    case "$1" in
        --root) (($# >= 2)) || die '--root needs a directory'; root_override=$2; shift 2 ;;
        --out) (($# >= 2)) || die '--out needs a file name'; out_override=$2; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_input=${root_override:-$script_dir/..}
root=$(cd -P -- "$root_input" 2>/dev/null && pwd) || die "root を開けません: $root_input"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -n $out_override ]]; then
    out_file=$out_override
else
    out_file=$root/tmp/acceptance/diag-$stamp.tar.gz
fi

cat <<'EOF'
診断の収集 (読み取りだけ。unit・設定・データは変えません)

収集するもの (手順書の観測表の行ごとに 1 ファイル):
  01 実効の unit        systemctl --user cat / show afx.service
  02 cgroup の形        MainPID の /proc/<pid>/cgroup と ControlGroup 配下の dir 名
  03 linger             loginctl show-user の Linger、afx の active / enabled
  04 journal の観測契約  InvocationID、cursor、_SYSTEMD_INVOCATION_ID / USER_INVOCATION_ID の実在、improve isolation 行
  05 終了コード         ExecMainStatus / NRestarts / Result、setup_in_progress の有無
  06 依存の実効値        afx.service と afx-ops@<feature> の Wants / Requisite / PartOf / After
  07 ops の ready       afx-ops@* の状態、afx ctl status、afx ops status <feature>
  08 ops の restart 連動  afx と ops の NRestarts / ActiveEnterTimestamp (restart / stop 自体は手で行う)
  09 リセット後の状態     git status --porcelain --ignored
  10 環境差             OS、systemd、cgroup の種類、WSL かどうか
  11 journal の末尾      journalctl --user -u afx -n 100
  12 確認記録            data/state/setup.json の result / reason_code / state の固定語彙だけ
  13 道具               uv / node / claude / codex の場所 (実行はしません)

収集しないもの (読みも、一覧にも載せません):
  .env
  config/settings.yaml (URL を含みうる)
  ops の secret ディレクトリと feature の設定 (~/.config/agentic-fx/ops/ 配下の中身)
  操作 API の鍵ディレクトリ (~/.config/agentic-fx/api/ 配下)
  取得した出力の URL、Authorization 行、Bearer の値、AUTH / KEY / TOKEN / SECRET / PASSWORD を名前に含む設定の値は <url> / *** に置き換えます。
EOF

work_parent=$(mktemp -d "${TMPDIR:-/tmp}/afx-diag.XXXXXX") || { printf '%s\n' '作業ディレクトリを作れません' >&2; exit 0; }
cleanup() { rm -rf -- "$work_parent"; }
trap cleanup EXIT
work=$work_parent/diag-$stamp
mkdir -p -- "$work" || exit 0

redact() {
    sed -E \
        -e 's#[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]"'"'"'<>]*#<url>#g' \
        -e 's/(authorization[[:space:]]*:[[:space:]]*).*/\1***/I' \
        -e 's/(bearer[[:space:]]+)[^[:space:]"'"'"']+/\1***/Ig' \
        -e 's/([A-Za-z0-9_]*(auth|key|token|secret|password|passwd|pass)[A-Za-z0-9_]*"?[[:space:]]*[=:][[:space:]]*"?'"'"'?)[^[:space:],"'"'"'}]+/\1***/Ig'
}

# capture <ファイル名> <コマンド...>: 出力 (伏せ字済み) を <ファイル名> に追記する。取れなければ「取得できず」。
# 外部コマンドは 60 秒で打ち切る。関数はそのまま呼ぶ。
capture() {
    local name=$1; shift
    local file=$work/$name.txt
    local scratch rc
    printf '$ %s\n' "$*" >> "$file"
    if ! command -v -- "$1" >/dev/null 2>&1; then
        printf '取得できず (コマンドが見つかりません: %s)\n\n' "$1" >> "$file"
        return 0
    fi
    scratch=$(mktemp "$work_parent/capture.XXXXXX") || { printf '取得できず (一時ファイルを作れません)\n\n' >> "$file"; return 0; }
    if [[ $(type -t -- "$1") == function ]]; then
        "$@" >"$scratch" 2>&1 </dev/null
        rc=$?
    else
        timeout 60 "$@" >"$scratch" 2>&1 </dev/null
        rc=$?
    fi
    redact < "$scratch" >> "$file"
    if ((rc != 0)); then
        printf '取得できず (終了コード %d)\n' "$rc" >> "$file"
    fi
    printf '\n' >> "$file"
    rm -f -- "$scratch"
    return 0
}

# 終了コードが情報そのもののコマンド (is-active が inactive で 3 を返すなど) 用。
capture_state() {
    local name=$1; shift
    local file=$work/$name.txt
    local scratch rc
    printf '$ %s\n' "$*" >> "$file"
    if ! command -v -- "$1" >/dev/null 2>&1; then
        printf '取得できず (コマンドが見つかりません: %s)\n\n' "$1" >> "$file"
        return 0
    fi
    scratch=$(mktemp "$work_parent/capture.XXXXXX") || return 0
    timeout 60 "$@" >"$scratch" 2>&1 </dev/null
    rc=$?
    redact < "$scratch" >> "$file"
    printf '(終了コード %d)\n\n' "$rc" >> "$file"
    rm -f -- "$scratch"
    return 0
}

note() { printf '%s\n\n' "$2" >> "$work/$1.txt"; }

unit_props='-p Type -p NotifyAccess -p KillMode -p DelegateControllers -p MainPID -p TimeoutStopUSec -p SendSIGKILL -p FinalKillSignal -p TimeoutStopFailureMode -p RestartPreventExitStatus -p Restart -p RestartUSec -p ControlGroup'

# ---- 01 実効の unit
capture 01-effective-unit systemctl --user cat afx.service
# shellcheck disable=SC2086
capture 01-effective-unit systemctl --user show afx.service $unit_props

# ---- 02 cgroup の形
cgroup_shape() {
    command -v systemctl >/dev/null 2>&1 || { printf '取得できず (systemctl が見つかりません)\n'; return 0; }
    local main_pid group dir
    main_pid=$(systemctl --user show afx.service -p MainPID 2>/dev/null | sed -n 's/^MainPID=//p')
    group=$(systemctl --user show afx.service -p ControlGroup 2>/dev/null | sed -n 's/^ControlGroup=//p')
    printf 'MainPID=%s\nControlGroup=%s\n' "${main_pid:-}" "${group:-}"
    if [[ $main_pid =~ ^[1-9][0-9]*$ ]]; then
        printf -- '--- /proc/%s/cgroup\n' "$main_pid"
        cat -- "/proc/$main_pid/cgroup" 2>&1 || printf '取得できず (読めません)\n'
    else
        printf '取得できず (MainPID がありません: afx は動いていません)\n'
    fi
    if [[ -n $group && $group == /* && $group != *..* ]]; then
        dir=/sys/fs/cgroup$group
        printf -- '--- %s の配下 (dir 名だけ、深さ 2 まで)\n' "$dir"
        if [[ -d $dir ]]; then
            find "$dir" -maxdepth 2 -type d 2>&1 | sort
        else
            printf '取得できず (cgroup の dir がありません)\n'
        fi
    fi
}
capture 02-cgroup cgroup_shape

# ---- 03 linger
capture_state 03-linger loginctl show-user "${USER:-$(id -un)}" -p Linger
capture_state 03-linger systemctl --user is-active afx
capture_state 03-linger systemctl --user is-enabled afx

# ---- 04 journal の観測契約 (InvocationID、cursor、field の実在)
journal_fields() {
    command -v journalctl >/dev/null 2>&1 || { printf '取得できず (journalctl が見つかりません)\n'; return 0; }
    journalctl --user -u afx.service -o json --no-pager -n 2000 | python3 -I -c '
import json, sys
fields = {"_SYSTEMD_INVOCATION_ID": 0, "USER_INVOCATION_ID": 0}
seen = {}
total = 0
for line in sys.stdin:
    try:
        entry = json.loads(line)
    except ValueError:
        continue
    if not isinstance(entry, dict):
        continue
    total += 1
    for name in fields:
        value = entry.get(name)
        if isinstance(value, str) and value:
            fields[name] += 1
            seen[value] = seen.get(value, 0) + 1
print("journal の行数 (afx.service):", total)
for name, count in fields.items():
    print("%s を持つ行: %d" % (name, count))
print("異なる InvocationID の数:", len(seen))
for value, count in list(seen.items())[-3:]:
    print("  %s ... %d 行" % (value[:32], count))
if total == 0:
    print("判定: 行が読めません (journal 不可か afx が未起動)")
elif not any(fields.values()):
    print("判定: どちらの field も無い -> cursor の fallback になる (契約の手順 4)")
elif fields["_SYSTEMD_INVOCATION_ID"]:
    print("判定: _SYSTEMD_INVOCATION_ID が使える (契約の手順 3 の第一)")
else:
    print("判定: USER_INVOCATION_ID だけが使える (契約の手順 3 の第二)")
'
}
journal_isolation_lines() {
    command -v journalctl >/dev/null 2>&1 || { printf '取得できず (journalctl が見つかりません)\n'; return 0; }
    journalctl --user -u afx.service -o cat --no-pager -n 2000 | grep 'improve isolation' \
        || printf '(improve isolation の行はありません)\n'
    return 0
}
capture 04-journal-contract systemctl --user show afx.service -p InvocationID --value
capture 04-journal-contract journalctl --user -u afx.service -n 0 --show-cursor
capture 04-journal-contract journal_fields
capture 04-journal-contract journal_isolation_lines

# ---- 05 終了コード 75 / 76 の非再起動
capture 05-exit-status systemctl --user show afx.service -p ExecMainStatus -p NRestarts -p Result
marker_state() {
    if [[ -e $root/data/state/setup_in_progress ]]; then
        printf 'setup_in_progress: あり\n'
    else
        printf 'setup_in_progress: なし\n'
    fi
}
capture 05-exit-status marker_state
note 05-exit-status '(marker を置いて start する観測は手で行う。手順書の観測表 5 行目)'

# ---- 06 依存の実効値 / 07 ops の ready / 08 restart 連動
capture 06-dependencies systemctl --user show afx.service -p Wants -p After
ops_features() {
    command -v systemctl >/dev/null 2>&1 || return 0
    systemctl --user list-units 'afx-ops@*' --all --no-legend --plain --no-pager 2>/dev/null \
        | awk '{ unit = $1; if (unit !~ /^afx-ops@/) unit = $2; print unit }' | sed -n 's/^afx-ops@\([a-z0-9_-]\{1,32\}\)\.service$/\1/p'
}
features=()
while IFS= read -r feature; do
    [[ -n $feature ]] && features+=("$feature")
done < <(ops_features)
capture 07-ops-ready systemctl --user list-units 'afx-ops@*' --all --no-legend --plain --no-pager
for feature in "${features[@]+"${features[@]}"}"; do
    capture 06-dependencies systemctl --user show "afx-ops@$feature.service" -p Wants -p Requisite -p PartOf -p After
    capture_state 07-ops-ready systemctl --user is-active "afx-ops@$feature.service"
    capture 08-ops-restart-link systemctl --user show "afx-ops@$feature.service" -p NRestarts -p ActiveEnterTimestamp -p Result
done
if [[ -x $root/.venv/bin/afx ]]; then
    capture 07-ops-ready "$root/.venv/bin/afx" ctl status
    for feature in "${features[@]+"${features[@]}"}"; do
        capture 07-ops-ready "$root/.venv/bin/afx" ops status "$feature"
    done
else
    note 07-ops-ready '取得できず (.venv/bin/afx がありません: uv sync の前か、別の場所の clone)'
fi
capture 08-ops-restart-link systemctl --user show afx.service -p NRestarts -p ActiveEnterTimestamp -p Result
note 08-ops-restart-link '(restart / stop の連動を見る操作自体は手で行う。手順書の観測表 8 行目)'

# ---- 09 リセットからのやり直し
capture 09-reset-state git -C "$root" status --porcelain --ignored
capture 09-reset-state git -C "$root" rev-parse HEAD

# ---- 10 環境差
os_release() { grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release 2>&1; }
wsl_state() {
    if grep -qi microsoft /proc/version 2>/dev/null; then printf 'WSL: はい\n'; else printf 'WSL: いいえ\n'; fi
}
capture 10-environment os_release
capture 10-environment uname -r
capture 10-environment systemctl --version
capture 10-environment stat -fc %T /sys/fs/cgroup
capture_state 10-environment systemctl --user is-system-running
capture 10-environment wsl_state

# ---- 11 journal の末尾
capture 11-journal-tail journalctl --user -u afx -n 100 --no-pager

# ---- 12 確認記録の固定語彙だけ
setup_record() {
    local record=$root/data/state/setup.json
    if [[ ! -f $record ]]; then
        printf '取得できず (data/state/setup.json がありません)\n'
        return 0
    fi
    python3 -I -c '
import json, re, sys
KEYS = ("result", "reason_code", "state")
SAFE = re.compile(r"[A-Za-z0-9_:.-]{1,48}")
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception as error:
    print("取得できず (setup.json を読めません: %s)" % type(error).__name__)
    sys.exit(0)

def name(part):
    text = str(part)
    return text if SAFE.fullmatch(text) else "?"

def walk(node, prefix):
    if isinstance(node, dict):
        for key, value in node.items():
            path = prefix + "." + name(key) if prefix else name(key)
            if key in KEYS and isinstance(value, str):
                print("%s=%s" % (path, value if SAFE.fullmatch(value) else "<omitted>"))
            else:
                walk(value, path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk(value, "%s[%d]" % (prefix, index))

walk(data, "")
' "$record"
}
capture 12-setup-record setup_record

# ---- 13 道具 (場所だけ。実行はしない)
tool_paths() {
    local tool found
    for tool in uv node claude codex python3 git; do
        if found=$(command -v -- "$tool" 2>/dev/null); then
            printf '%s: %s -> %s\n' "$tool" "$found" "$(readlink -f -- "$found" 2>/dev/null || printf '?')"
        else
            printf '%s: なし\n' "$tool"
        fi
    done
}
capture 13-tools tool_paths

mkdir -p -- "$(dirname -- "$out_file")" 2>/dev/null
if tar -C "$work_parent" -czf "$out_file" "diag-$stamp" 2>/dev/null; then
    printf '\n収集しました: %s\n' "$out_file"
    printf '%s\n' '貼る前に中身を見て、秘密と URL が無いことを確かめてください。'
else
    printf '\ntar.gz を作れませんでした: %s\n' "$out_file" >&2
fi
exit 0
