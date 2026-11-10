"""web の `write` (ops.toml) と書込みの鍵 (`ops-web-write`) が整合しているかの判定。

整合は 2 通り。`write = false` (key が無いのも false) で鍵が未登録・revoked、又は `write = true` で
鍵が active かつ鍵 file が manifest と一致する。web の stage・設定・secret を書き換える操作と、
web の操作を「成功」で終える早期 return は、この判定を通してから行う。判定は読むだけで何も書かない。

`mode` は呼ぶ時点の web の stage で決まり、不整合のときの回復の案内だけを変える。
- `idle`: 有効でない (一度も enable していない、又は disabled)。設定の file や鍵 dir がまだ無いことは false として扱う。
- `enabled`: 有効 (terminal の enabled)。
- `unfinished`: enable・disable が途中。
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import KeySetError, enrolled_key_matches, key_dir, principal_entry
from agentic_fx.ops_setup.feature_config import web_retained_write

WRITE_PRINCIPAL = Principal.OPS_WEB_WRITE
# manifest は active なのに鍵 file が使えない・一致しない。
KEY_MISMATCH_CODES = frozenset({"key_missing", "key_file_invalid", "digest_mismatch"})
MODES = ("idle", "enabled", "unfinished")
CONFIG_PATH = ("$XDG_CONFIG_HOME/agentic-fx/ops/web/config/ops.toml "
               "(XDG_CONFIG_HOME が無ければ ~/.config/agentic-fx/ops/web/config/ops.toml)")
LIVE_GUIDANCE = "live の ops.toml を読める状態に直してから再実行する (afx ops web status で reason を見る)"
RECOVERY = ("手で直す: 書込みの有無を決める key は `write = true` か `write = false` (真偽値)。"
             "TOML の構文・未知の key・型を直して設定を構文と schema の上で有効に戻し、"
             "afx ops web enable --write で整合を回復する。読取専用にするなら、その後に afx ops web enable --no-write を実行する")
UNDECIDED_GUIDANCE = f"書込みの鍵があるのに ops.toml の write が決められない。対象は {CONFIG_PATH}。{RECOVERY}"
_UNFINISHED_UNDECIDED = (f"書込みの鍵があるのに ops.toml の write が true と読めない。対象は {CONFIG_PATH}。"
                         "`write = true` (真偽値) にするか、afx を止めて afx keys revoke ops-web-write を実行して"
                         "write の行を消し、afx ops web enable (又は disable) を再実行する")
_PREPARING_SWITCH = ("書込みの鍵の操作が途中で止まっている。afx ops web enable --write (又は --no-write) を再実行して"
                     "記録された鍵の操作を完了させる (ops.toml が構文・schema の検査を通らないときは先に直す)")


@dataclass(frozen=True)
class Consistent:
    """整合している。`write` は保持すべき write。"""
    write: bool


@dataclass(frozen=True)
class Inconsistent:
    reason: str
    guidance: str = ""


def _mismatch_guidance(mode: str) -> str:
    rerun = "afx ops web enable (又は disable)" if mode == "unfinished" else "afx ops web enable --write"
    return f"afx を止めて afx keys rotate {WRITE_PRINCIPAL.value} を実行し、{rerun} を再実行する"


def _absent_key_guidance(mode: str, state: str | None) -> str:
    if mode == "unfinished":
        keys = "rotate" if state == "revoked" else "enroll"
        return (f"write = true なのに {WRITE_PRINCIPAL.value} の鍵が active でない。afx を止めて afx keys {keys} "
                f"{WRITE_PRINCIPAL.value} を実行するか、ops.toml の write を消して、afx ops web enable (又は disable) を再実行する。"
                f"対象は {CONFIG_PATH}")
    return (f"write = true なのに {WRITE_PRINCIPAL.value} の鍵が active でない。afx ops web enable --write で鍵を作り直すか、"
            "afx ops web enable --no-write で読取専用に戻す")


def _preparing_guidance(mode: str, op: str | None) -> str:
    if mode == "unfinished":
        return (f"書込みの鍵の操作が途中で止まっている。afx を止めて afx keys {op or 'rotate'} {WRITE_PRINCIPAL.value} を実行して"
                "完了させ、afx ops web enable (又は disable) を再実行する")
    return _PREPARING_SWITCH


def check_write_key_consistency(root, *, mode: str = "enabled",
                                strict_config: bool = False) -> Consistent | Inconsistent:
    """保持すべき write と書込みの鍵の整合を判定する (読むだけ)。

    設定 dir・file の安全性の違反や不在 (読めない write) は、`strict_config` か書込みの鍵が active のときだけ
    その reason で不整合にする。鍵が未登録・revoked なら false として扱う。
    """
    if mode not in MODES:
        raise ValueError(mode)
    idle = mode == "idle"
    directory = key_dir(root)
    retained = web_retained_write(root)
    unreadable = retained if isinstance(retained, str) else None
    if unreadable is not None:
        if strict_config and not idle:
            return Inconsistent(unreadable, LIVE_GUIDANCE)
        retained = None
    try:
        entry = principal_entry(directory, WRITE_PRINCIPAL)
    except KeySetError as error:
        if not idle or error.code != "not_initialized":
            return Inconsistent("invalid_state")
        entry = None
    except OSError:
        return Inconsistent("invalid_state")
    state, op = entry if entry is not None else (None, None)
    if state == "preparing":
        return Inconsistent("write_key_preparing", _preparing_guidance(mode, op))
    if state == "active":
        if unreadable is not None:
            return Inconsistent(unreadable, LIVE_GUIDANCE)
        if retained is not True:
            return Inconsistent("write_state_undecided",
                                _UNFINISHED_UNDECIDED if mode == "unfinished" else UNDECIDED_GUIDANCE)
        try:
            matches = enrolled_key_matches(directory, WRITE_PRINCIPAL)
        except KeySetError as error:
            if error.code in KEY_MISMATCH_CODES:
                return Inconsistent("principal_key_mismatch", _mismatch_guidance(mode))
            return Inconsistent("invalid_state")
        except OSError:
            return Inconsistent("invalid_state")
        return Consistent(True) if matches else Inconsistent("invalid_state")
    if retained is True:
        return Inconsistent("invalid_state", _absent_key_guidance(mode, state))
    return Consistent(False)


def stage_mode(saved: dict | None) -> str:
    """web の stage file (読んだ dict) から mode を決める。"""
    if saved is None or (saved["operation"] == "disable" and saved["stage"] == "disabled"):
        return "idle"
    if saved["operation"] == "enable" and saved["stage"] == "enabled":
        return "enabled"
    return "unfinished"
