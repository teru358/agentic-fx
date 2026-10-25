"""`afx completion bash`: bash の補完スクリプトを分類メタデータから生成する。"""
from __future__ import annotations

import sys

from agentic_fx.cli_surface import COMMANDS, CommandRecord, Lane, expand_command

TOP_OPTIONS = ("--root", "--daemon")
CTL_OPTIONS = ("--as",)
PRINCIPALS = ("operator", "approver")


def _add(table: dict[tuple[str, ...], list[str]], key: tuple[str, ...], word: str) -> None:
    words = table.setdefault(key, [])
    if word not in words:
        words.append(word)


def candidate_table(records: tuple[CommandRecord, ...] = COMMANDS) -> dict[tuple[str, ...], list[str]]:
    """語の並び (位置) ごとの候補。公開された実装済みの record だけを数える。"""
    table: dict[tuple[str, ...], list[str]] = {}
    for record in records:
        if not (record.exposed and record.implemented and not record.planned):
            continue
        if record.lane is Lane.CTL:
            words = record.command.split(" ")
            if words[0] != "ctl" or len(words) < 2:
                continue
            _add(table, (), "ctl")
            _add(table, ("ctl",), words[1])
            if len(words) > 2:
                _add(table, ("ctl", words[1]), words[2])
            continue
        for path in expand_command(record):
            words = path.split(" ")
            for size, word in enumerate(words):
                _add(table, tuple(words[:size]), word)
    return table


def _quote(words: list[str]) -> str:
    return '"' + " ".join(words) + '"'


def render_bash(records: tuple[CommandRecord, ...] = COMMANDS) -> str:
    """`eval "$(afx completion bash)"` で読み込める補完関数を返す。"""
    table = candidate_table(records)
    arms: list[str] = []
    for key, words in table.items():
        label = '"' + " ".join(key) + '"'
        options = list(TOP_OPTIONS) if not key else (list(CTL_OPTIONS) if key == ("ctl",) else [])
        arms.append(f"        {label})\n"
                    f"            candidates={_quote(words)}\n"
                    f"            options={_quote(options)} ;;")
    arm_text = "\n".join(arms)
    return f"""# bash completion for afx
_afx() {{
    local IFS=' '
    local cur prev key candidates options i word line
    local -a words
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev=""
    if (( COMP_CWORD > 0 )); then
        prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    fi
    case "$prev" in
        --root)
            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -d -- "$cur")
            return 0 ;;
        --as)
            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W {_quote(list(PRINCIPALS))} -- "$cur")
            return 0 ;;
    esac
    words=()
    i=1
    while (( i < COMP_CWORD )); do
        word="${{COMP_WORDS[i]}}"
        case "$word" in
            --root|--as) (( i += 2 )); continue ;;
            -*) ;;
            *) words+=("$word") ;;
        esac
        (( i++ ))
    done
    key="${{words[*]}}"
    candidates=""
    options=""
    case "$key" in
{arm_text}
    esac
    if [[ "$cur" == -* ]]; then
        candidates="$options"
    fi
    while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "$candidates" -- "$cur")
    return 0
}}
complete -F _afx afx
"""


def main(shell: str) -> int:
    if shell != "bash":
        print("afx: 補完は bash だけです", file=sys.stderr)
        return 2
    sys.stdout.write(render_bash())
    return 0
