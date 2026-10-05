"""代表自己試験の子 process が呼ぶ隔離 hook (テスト用)。

plugin を持たない runtime / system 規則だけの Landlock の後に seccomp を掛け、隔離の証跡を返す。
"""
from __future__ import annotations

from agentic_fx.core import landlock, runtime_fingerprint, seccomp


def isolate() -> dict:
    applied = landlock.apply_plugin_ruleset([])
    seccomp.apply_allow6()
    return {**applied.attestation_fields(),
            "seccomp_profile": seccomp.SECCOMP_PROFILE,
            "sandbox_profile": runtime_fingerprint.SANDBOX_PROFILE_VERSION}


def seccomp_only() -> None:
    seccomp.apply_allow6()


def forged() -> dict:
    """Landlock を掛けずに、掛けたと称する証跡だけを返す。"""
    seccomp.apply_allow6()
    return {"landlock_fs_abi": 8, "landlock_tsync": "applied", "network": "applied",
            "scope": "applied", "seccomp_profile": seccomp.SECCOMP_PROFILE,
            "sandbox_profile": runtime_fingerprint.SANDBOX_PROFILE_VERSION}


def stale_profile() -> dict:
    out = isolate()
    out["seccomp_profile"] = "allow/0"
    return out


def low_abi() -> dict:
    out = isolate()
    out["landlock_fs_abi"] = 2
    return out
