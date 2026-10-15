#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' 'usage: first-run-reset.sh [--root DIR] [--dry-run] [--yes] [--include-env] [--include-linger] [--stop-running]'
    printf '%s\n' '稼働中の instance は stop の確認または --stop-running が必要です。'
}
die() { printf '%s\n' "$1" >&2; exit 2; }

root_override=''
root_given=false
dry_run=false
assume_yes=false
include_env=false
include_linger=false
stop_running=false
while (($#)); do
    case "$1" in
        --root) (($# >= 2)) || die '--root needs a directory'; root_override=$2; root_given=true; shift 2 ;;
        --dry-run) dry_run=true; shift ;;
        --yes) assume_yes=true; shift ;;
        --include-env) include_env=true; shift ;;
        --include-linger) include_linger=true; shift ;;
        --stop-running) stop_running=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null 2>&1 || die 'python3 が要ります'
helper=$script_dir/first_run_reset_helper.py
[[ -f $helper ]] || die 'first_run_reset_helper.py が見つかりません'
if $root_given; then
    [[ -n $root_override ]] || die '--root に空の値は指定できません'
    root_input=$root_override
else
    root_input=$script_dir/..
fi
root_details=$(python3 -I "$helper" root "$root_input") || die 'root を実パスに解決できません'
mapfile -t root_values <<<"$root_details"
root=${root_values[0]}
instance_id=${root_values[1]}
[[ -e $root/.git && -f $root/config/settings.yaml.example ]] || die 'root は agentic-fx の git checkout ではありません'

if [[ ${XDG_CONFIG_HOME:-} == /* ]]; then config_home=$XDG_CONFIG_HOME; else config_home=${HOME:?HOME is required}/.config; fi
home_config=${HOME:?HOME is required}/.config
unit_dir=$config_home/systemd/user
api_dir=$home_config/agentic-fx/api
ops_dir=$config_home/agentic-fx/ops
declare -a target_paths=() target_reasons=() target_kinds=() excluded_paths=() excluded_reasons=()
add_target() { target_paths+=("$1"); target_reasons+=("$2"); target_kinds+=("$3"); }
add_excluded() { excluded_paths+=("$1"); excluded_reasons+=("$2"); }

out_of_root() { [[ $(python3 -I "$helper" contained "$1" "$2") != ok ]]; }
plan_repo_path() {
    local relative reason kind path
    relative=$1
    reason=$2
    kind=$3
    path=$root/$relative
    if [[ -L $path ]]; then
        add_excluded "$path" '対象外 (symlink)'
    elif [[ -e $path ]]; then
        if out_of_root "$root" "$path"; then add_excluded "$path" '対象外 (symlink-out-of-root)'; else add_target "$path" "$reason" "$kind"; fi
    fi
}

plan_repo_path 'config/settings.yaml' 'repo の設定' repo_settings
for path in "$root"/config/settings.yaml.bak-*; do
    [[ -e $path || -L $path ]] || continue
    plan_repo_path "${path#"$root/"}" 'repo の設定 backup' repo_settings
done
plan_repo_path 'policy/directives.md' 'repo の方針文' repo_settings
if $include_env; then plan_repo_path '.env' '--include-env で指定された環境ファイル' repo_env; elif [[ -e $root/.env || -L $root/.env ]]; then add_excluded "$root/.env" '--include-env が無いため触らない'; fi
# plugins: every entry except the tracked placeholder is planned on its own,
# so that nothing created after the plan is shown can be removed.
if [[ -L $root/plugins ]]; then
    add_excluded "$root/plugins" '対象外 (symlink)'
elif [[ -d $root/plugins ]]; then
    if out_of_root "$root" "$root/plugins"; then
        add_excluded "$root/plugins" '対象外 (symlink-out-of-root)'
    else
        while IFS= read -r -d '' path; do
            plan_repo_path "${path#"$root/"}" 'repo の plugin 生成物' repo_plugins
        done < <(find -P "$root/plugins" -mindepth 1 -maxdepth 1 ! -name .gitkeep -print0 | sort -z)
    fi
elif [[ -e $root/plugins ]]; then
    plan_repo_path 'plugins' 'repo の plugin 生成物' repo_plugins
fi
plan_repo_path 'logs' 'repo のログ' repo_logs
plan_repo_path 'data' 'repo の状態・取引データ' repo_data

primary_unit_owned=false
owned_unit_found=false
unit_matches_root() { [[ $(python3 -I "$helper" unit-match "$root" "$1") == owned ]]; }
for path in "$unit_dir/afx.service" "$unit_dir"/afx.service.bak-*; do
    [[ -e $path || -L $path ]] || continue
    if [[ -L $path ]]; then
        add_excluded "$path" '対象外 (symlink)'
    elif unit_matches_root "$path"; then
        add_target "$path" 'WorkingDirectory がこの root と一致する unit' unit
        owned_unit_found=true
        [[ $path == "$unit_dir/afx.service" ]] && primary_unit_owned=true
    else
        add_excluded "$path" '別 instance の unit を検出、触らない'
    fi
done
if $primary_unit_owned; then
    for path in "$unit_dir/afx-ops@.service" "$unit_dir/afx.service.d/afx-ops.conf"; do
        [[ -e $path || -L $path ]] || continue
        if [[ -L $path ]]; then
            add_excluded "$path" '対象外 (symlink)'
        elif [[ $path == */afx-ops@.service ]]; then
            add_target "$path" 'この root の afx.service が所有する ops unit' ops_unit
        else
            add_target "$path" 'この root の afx.service が所有する ops unit' ops_dropin
        fi
    done
fi

own_key_dir=$api_dir/$instance_id
if [[ -e $own_key_dir || -L $own_key_dir ]]; then
    if out_of_root "$home_config/agentic-fx" "$own_key_dir"; then add_excluded "$own_key_dir" '対象外 (symlink-out-of-root)'
    elif [[ -L $own_key_dir ]]; then add_excluded "$own_key_dir" '対象外 (symlink)'; else add_target "$own_key_dir" 'この root の instance_id に対応する API 鍵' api_key; fi
fi
for path in "$api_dir"/*; do
    [[ -e $path || -L $path ]] || continue
    [[ $path == "$own_key_dir" ]] && continue
    add_excluded "$path" '別 instance の鍵を検出、触らない'
done

for path in "$ops_dir"/*; do
    [[ -e $path || -L $path ]] || continue
    if out_of_root "$config_home/agentic-fx" "$path"; then add_excluded "$path" '対象外 (symlink-out-of-root)'; continue; fi
    if [[ -L $path ]]; then add_excluded "$path" '対象外 (symlink)'; continue; fi
    config=$path/config/ops.toml
    if [[ -L $config ]]; then
        add_excluded "$path" '対象外 (symlink)'
    elif out_of_root "$config_home/agentic-fx" "$config"; then
        add_excluded "$path" '対象外 (symlink-out-of-root)'
    elif [[ -f $config ]]; then
        verdict=$(python3 -I "$helper" feature-match "$root" "$config")
        case $verdict in
            owned) add_target "$path" 'ops.toml の instance_root がこの root と canonical に一致する feature' ops_feature ;;
            unreadable) add_excluded "$path" 'ops.toml を判定できないため触らない' ;;
            *) add_excluded "$path" '所有不明または別 instance の資材を検出、触らない' ;;
        esac
    else
        add_excluded "$path" '所有不明または別 instance の資材を検出、触らない'
    fi
done

linger_owned=false
if [[ -f $root/data/state/setup.json && $(python3 -I "$helper" linger "$root/data/state/setup.json") == owned ]]; then linger_owned=true; fi
if $linger_owned && $include_linger; then add_target 'linger' 'wizard の owned_by_wizard 記録と --include-linger による候補' linger; elif [[ -f $root/data/state/setup.json ]]; then add_excluded 'linger' 'linger は手動で有効化されたものか、所有を確かめられないため触りません'; fi

printf 'root: %s\ninstance_id: %s\n対象:\n' "$root" "$instance_id"
if ((${#target_paths[@]} == 0)); then printf '%s\n' '  (なし)'; else for index in "${!target_paths[@]}"; do printf '  %s (%s)\n' "${target_paths[index]}" "${target_reasons[index]}"; done; fi
printf '%s\n' '触らない:'
if ((${#excluded_paths[@]} == 0)); then printf '%s\n' '  (なし)'; else for index in "${!excluded_paths[@]}"; do printf '  %s (%s)\n' "${excluded_paths[index]}" "${excluded_reasons[index]}"; done; fi
printf '%s\n' 'paper 取引履歴、注文、approval、改善 report、RAG、plugins を永久削除します (`data/`・`logs/`・`plugins/`)。復元できません'
if $dry_run; then exit 0; fi
if ! $assume_yes; then
    if [[ ! -t 0 ]]; then printf '%s\n' '非端末では --yes が必要です' >&2; exit 2; fi
    read -r -p '永久に削除します。続けるには reset と入力: ' answer || answer=''
    [[ $answer == reset ]] || exit 0
fi
if $linger_owned && $include_linger && [[ -t 0 ]]; then
    read -r -p 'linger を解除します。続けるには linger と入力: ' linger_answer || linger_answer=''
    [[ $linger_answer == linger ]] || exit 2
fi

# Units whose state this run changes: only features planned above.
declare -a ops_units=()
for index in "${!target_paths[@]}"; do
    [[ ${target_kinds[index]} == ops_feature ]] && ops_units+=("afx-ops@${target_paths[index]##*/}.service")
done

# One helper process holds data/ops/setup.lock and data/instance.lock for the
# whole deletion.  It lives exactly as long as the write end of its stdin; the
# exit trap closes that end, and a killed script closes it implicitly.
locker_open=false
release_locks() {
    if $locker_open; then
        locker_open=false
        eval "exec ${LOCKER[1]}>&-" 2>/dev/null || true
        wait "$LOCKER_PID" 2>/dev/null || true
    fi
}
trap release_locks EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
read_verdict() {
    local line=''
    IFS= read -r -t 30 -u "${LOCKER[0]}" line || line=error
    printf '%s' "$line"
}
lock_busy=false
if [[ -e $root/data || -L $root/data ]]; then
    coproc LOCKER { exec python3 -I "$helper" hold-locks "$root"; }
    locker_open=true
    verdict=$(read_verdict)
    case $verdict in
        ok|instance-busy) ;;
        setup-busy) die '`afx init` または `afx ops enable` が実行中のため、停止も削除もしません' ;;
        lock_path_unsafe) die 'data/ の経路に symlink があるため lock を取れません。何も消していません' ;;
        *) die 'setup.lock / instance.lock の保持者を判定できないため停止も削除しません' ;;
    esac
    [[ $verdict == instance-busy ]] && lock_busy=true
fi

unit_is_running() {
    local state
    state=$(systemctl --user is-active "$1" 2>/dev/null || true)
    case $state in active|activating|reloading|deactivating) return 0 ;; *) return 1 ;; esac
}
unit_active=false
if $primary_unit_owned && unit_is_running afx.service; then unit_active=true; fi
for unit in "${ops_units[@]}"; do
    if unit_is_running "$unit"; then unit_active=true; fi
done
# A held lock is a running manual invocation when no matching unit is active.
# Never remove its files: the process may still have them open.
if $lock_busy && ! $unit_active; then
    printf '%s\n' 'instance.lock を保持する手動起動を検出したため、停止も削除もしません' >&2
    exit 2
fi
if $unit_active; then
    if ! $stop_running; then
        if [[ ! -t 0 ]]; then printf '%s\n' '稼働中の instance には非端末で --stop-running が必要です' >&2; exit 2; fi
        read -r -p '稼働中の unit を停止します。続けるには stop と入力: ' stop_answer || stop_answer=''
        [[ $stop_answer == stop ]] || exit 2
    fi
    if $primary_unit_owned; then systemctl --user stop afx.service; fi
    for unit in "${ops_units[@]}"; do systemctl --user stop "$unit"; done
fi
disable_unit() {
    if systemctl --user disable "$1"; then return 0; fi
    # A unit that is not enabled has nothing to disable.
    if systemctl --user is-enabled "$1" >/dev/null 2>&1; then
        printf '%s\n' "$1 を無効化できませんでした" >&2
        exit 2
    fi
}
if $primary_unit_owned; then disable_unit afx.service; fi
for unit in "${ops_units[@]}"; do disable_unit "$unit"; done
if $lock_busy; then
    # The stopped instance releases its lock; keep the setup lock while waiting.
    for _ in $(seq 1 50); do
        if ! kill -0 "$LOCKER_PID" 2>/dev/null; then die 'lock の保持が失われました。削除しません'; fi
        trap '' PIPE
        if ! printf 'retry\n' >&"${LOCKER[1]}" 2>/dev/null; then trap - PIPE; die 'lock の保持が失われました。削除しません'; fi
        trap - PIPE
        verdict=$(read_verdict)
        [[ $verdict == ok ]] && break
        [[ $verdict == instance-busy ]] || break
        sleep 0.2
    done
    [[ $verdict == ok ]] || die 'unit を止めましたが instance.lock の保持者が残っています (手動起動)。削除しません'
fi

# Everything below removes only what the plan listed, in a fixed order of
# kinds.  The unit that proves ownership goes after the files it vouches for.
# A failing kind is recorded and the remaining kinds still run.
remove_kind() {
    local kind=$1 index path status=0
    for index in "${!target_paths[@]}"; do
        [[ ${target_kinds[index]} == "$kind" ]] || continue
        path=${target_paths[index]}
        [[ -L $path ]] && continue
        case $kind in
            repo_settings|repo_plugins|repo_logs|repo_data|repo_env)
                if [[ -d $path ]]; then chmod -R u+rwX -- "$path" || true; fi ;;
        esac
        rm -rf -- "$path" || status=1
    done
    return $status
}
declare -a removed_kinds=() failed_kinds=()
kind_order=(ops_feature api_key ops_unit ops_dropin unit repo_settings repo_plugins repo_logs linger repo_data repo_env)
for kind in "${kind_order[@]}"; do
    status=0
    case $kind in
        linger)
            # Last before data/: the record that proves ownership stays until
            # the linger setting has been handled.
            if $linger_owned && $include_linger; then
                if ((${#failed_kinds[@]})); then
                    printf '%s\n' '他の削除に失敗したため linger は解除しません' >&2
                    status=1
                else
                    loginctl disable-linger || status=1
                fi
            fi ;;
        repo_data)
            if ((${#failed_kinds[@]})); then
                printf '%s\n' '他の削除に失敗したため data/ は残します' >&2
                status=1
            else
                remove_kind "$kind" || status=1
            fi ;;
        unit)
            if [[ " ${failed_kinds[*]:-} " == *" ops_unit "* || " ${failed_kinds[*]:-} " == *" ops_dropin "* ]]; then
                printf '%s\n' 'primary unit は所有の証拠として残しました。再実行で続きを消します' >&2
                status=1
            else
                remove_kind "$kind" || status=1
            fi ;;
        *) remove_kind "$kind" || status=1 ;;
    esac
    case $kind in
        ops_dropin) rmdir --ignore-fail-on-non-empty "$unit_dir/afx.service.d" 2>/dev/null || true ;;
        unit)
            if $owned_unit_found; then
                systemctl --user daemon-reload || printf '%s\n' 'systemctl --user daemon-reload に失敗しました' >&2
            fi
            if $primary_unit_owned; then systemctl --user reset-failed afx.service || true; fi
            for unit in "${ops_units[@]}"; do systemctl --user reset-failed "$unit" || true; done ;;
    esac
    if ((status)); then failed_kinds+=("$kind"); else removed_kinds+=("$kind"); fi
done
printf '消せた kind: %s\n' "${removed_kinds[*]:-(なし)}"
if ((${#failed_kinds[@]})); then printf '失敗した kind: %s\n' "${failed_kinds[*]}" >&2; fi
printf '%s\n' 'git status --porcelain --ignored:'
git -C "$root" status --porcelain --ignored || true
((${#failed_kinds[@]} == 0)) || exit 1
