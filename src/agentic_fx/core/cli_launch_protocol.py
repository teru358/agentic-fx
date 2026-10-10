"""改善 mission の launcher と worker の間の status protocol。

worker が `SOCK_SEQPACKET` の socketpair を作り、子端を launcher に渡す。setup の
成否はこの protocol の frame だけで決め、launcher の stderr や exit code は根拠に
しない。

frame = header (magic `AFXL`、u16 version、u16 type、128 bit nonce の 24 byte) +
type ごとの固定 field。長さは type ごとに 1 通りで、余りも不足も malformed。
ancillary は PREPARED に付く SCM_RIGHTS の fd ちょうど 1 個だけを許す。

launcher の source hash (fingerprint の材料) に入るので、本 module は標準
ライブラリだけを import する。
"""
import os
import secrets
import socket
import struct
from dataclasses import dataclass

MAGIC = b"AFXL"
VERSION = 2
NONCE_LEN = 16

_HEADER = struct.Struct("<4sHH16s")
HEADER_LEN = _HEADER.size  # 24

# type 番号。向きは launcher → worker が HELLO / PREPARED / EXEC_FAILED /
# SETUP_FAILED、worker → launcher が JOINED / GO。
HELLO = 1
JOINED = 2
PREPARED = 3
GO = 4
EXEC_FAILED = 5
SETUP_FAILED = 6

TYPE_NAMES = {HELLO: "HELLO", JOINED: "JOINED", PREPARED: "PREPARED", GO: "GO",
              EXEC_FAILED: "EXEC_FAILED", SETUP_FAILED: "SETUP_FAILED"}

PROFILE_ID_LEN = 32
_BODY = {
    HELLO: struct.Struct("<i"),                 # pid
    JOINED: struct.Struct("<"),
    # profile id (ASCII、NUL 詰め)、policy hash、BPF hash (SHA-256 の生 32 byte)、
    # filter flags、実 Landlock ABI、subreaper の読み値 (1 = 立っている)
    PREPARED: struct.Struct("<32s32s32sIII"),
    GO: struct.Struct("<"),
    EXEC_FAILED: struct.Struct("<i"),           # errno
    SETUP_FAILED: struct.Struct("<HHi"),        # reason code、予約 (0)、errno / 補足
}
FRAME_LEN = {t: HEADER_LEN + s.size for t, s in _BODY.items()}
MAX_FRAME_LEN = max(FRAME_LEN.values())

# 受信 buffer。最長 frame より大きく取り、超えた分は MSG_TRUNC で検出する。
RECV_DATA_LEN = MAX_FRAME_LEN + 64
# fd を何個まで受け取って閉じられるか。超えた分は kernel が MSG_CTRUNC で捨てる。
RECV_ANC_LEN = socket.CMSG_SPACE(16 * 4)

# --- 固定 reason -----------------------------------------------------------------

# launcher が SETUP_FAILED で申告する reason。番号は wire 上の値で、変えない。
SETUP_REASONS = {
    1: "inherited_seccomp_filter",
    2: "credentials_unexpected",
    3: "rlimit_failed",
    4: "subreaper_failed",
    5: "landlock_abi_too_old",
    6: "landlock_create_failed",
    7: "no_new_privs_failed",
    8: "landlock_restrict_failed",
    9: "seccomp_install_failed",
    10: "seccomp_self_check_failed",
    11: "policy_compile_failed",
}
SETUP_REASON_CODES = {v: k for k, v in SETUP_REASONS.items()}

# credentials_unexpected の補足 (どの検査で外れたか)
CRED_DETAIL_CAPS = 1
CRED_DETAIL_UID = 2
CRED_DETAIL_GID = 3
CRED_DETAIL_GROUPS = 4

# worker 側で決まる reason
ATTESTATION_MALFORMED = "attestation_malformed"    # 長さ・magic・version・nonce・type・ancillary
ATTESTATION_ORDER = "attestation_order"            # state に合わない type
ATTESTATION_DEADLINE = "attestation_deadline"      # setup の期限切れ
ATTESTATION_MISMATCH = "attestation_mismatch"      # HELLO の pid、PREPARED の値が期待と違う
ATTESTATION_GO_SEND_FAILED = "attestation_go_send_failed"
ATTESTATION_EXEC_FAILED = "attestation_exec_failed"  # EXEC_FAILED を受けた
LAUNCHER_DIED = "launcher_died"
LAUNCHER_SPAWN_FAILED = "launcher_spawn_failed"
CGROUP_JOIN_FAILED = "cgroup_join_failed"
RECEIVER_HANDOFF_FAILED = "receiver_handoff_failed"
CLI_EXEC_UNPROVEN = "cli_exec_unproven"
# 実行 artifact が x86_64 の 64 bit ELF (EXEC / DYN) でない。launcher を起こす前の
# admission で決まる
CLI_ARTIFACT_UNSUPPORTED = "cli_artifact_unsupported"
# worker の main thread 以外から呼ばれた。launcher の PDEATHSIG は親の thread に
# 結びつくので、launcher を起こす前に断る
LAUNCH_THREAD_UNSUPPORTED = "launch_thread_unsupported"

ATTESTATION_REASONS = (ATTESTATION_MALFORMED, ATTESTATION_ORDER, ATTESTATION_DEADLINE,
                       ATTESTATION_MISMATCH, ATTESTATION_GO_SEND_FAILED,
                       ATTESTATION_EXEC_FAILED)

# probe (install せずに判定する kernel 能力) の reason
SECCOMP_USER_NOTIF_UNAVAILABLE = "seccomp_user_notif_unavailable"
SECCOMP_WAIT_KILLABLE_UNAVAILABLE = "seccomp_wait_killable_unavailable"
SECCOMP_TSYNC_ESRCH_UNAVAILABLE = "seccomp_tsync_esrch_unavailable"

# --- rlimit の profile 定数 ------------------------------------------------------
# settings から変えられない (隔離を緩める設定 key を作らない)。値は現行の mission
# worker の既定 (NOFILE 128、FSIZE 8 MiB、CORE 0、AS 4096 MiB) を写し、codex と
# opencode の AS だけは V8 / bun の巨大な仮想 address 予約のため現行と同じく
# 262144 MiB にする。memory の実質的な上限は cgroup の memory.max が持つ。
_MIB = 1024 * 1024
RLIMIT_AS_MB = {"claude": 4096, "codex": 262144, "opencode": 262144}
RLIMIT_NOFILE = 128
RLIMIT_FSIZE_MB = 8
# hard を (0, 0) に固定する rlimit。SIGPENDING は uid 全体に効くので触らない。
ZERO_RLIMITS = ("RLIMIT_CORE", "RLIMIT_NICE", "RLIMIT_RTPRIO", "RLIMIT_RTTIME",
                "RLIMIT_MEMLOCK", "RLIMIT_MSGQUEUE")


def rlimit_profile(backend: str) -> dict[str, tuple[int, int]]:
    """backend の rlimit 一式 (soft = hard)。未知の backend は KeyError。"""
    as_bytes = RLIMIT_AS_MB[backend] * _MIB
    out = {
        "RLIMIT_AS": (as_bytes, as_bytes),
        "RLIMIT_NOFILE": (RLIMIT_NOFILE, RLIMIT_NOFILE),
        "RLIMIT_FSIZE": (RLIMIT_FSIZE_MB * _MIB, RLIMIT_FSIZE_MB * _MIB),
    }
    for name in ZERO_RLIMITS:
        out[name] = (0, 0)
    return out


def new_launch_nonce() -> bytes:
    """launch ごとの 128 bit nonce。mission nonce に差し替えるときはここだけ替える。"""
    return secrets.token_bytes(NONCE_LEN)


# --- encode / decode --------------------------------------------------------------

class MalformedFrame(Exception):
    """frame が protocol に合わない。`detail` は技術ログ用。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Prepared:
    profile_id: str
    policy_hash: str   # hex
    bpf_hash: str      # hex
    filter_flags: int
    landlock_abi: int
    subreaper: int = 1   # launcher が `PR_GET_CHILD_SUBREAPER` で読んだ値


@dataclass(frozen=True)
class Frame:
    type: int
    nonce: bytes
    pid: int | None = None
    prepared: Prepared | None = None
    errno: int | None = None
    reason: str | None = None


def _header(ftype: int, nonce: bytes) -> bytes:
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 16 bytes")
    return _HEADER.pack(MAGIC, VERSION, ftype, nonce)


def encode_hello(nonce: bytes, pid: int) -> bytes:
    return _header(HELLO, nonce) + _BODY[HELLO].pack(pid)


def encode_joined(nonce: bytes) -> bytes:
    return _header(JOINED, nonce)


def encode_go(nonce: bytes) -> bytes:
    return _header(GO, nonce)


def encode_exec_failed(nonce: bytes, err: int) -> bytes:
    return _header(EXEC_FAILED, nonce) + _BODY[EXEC_FAILED].pack(err)


def encode_setup_failed(nonce: bytes, reason: str, detail: int = 0) -> bytes:
    return _header(SETUP_FAILED, nonce) + _BODY[SETUP_FAILED].pack(
        SETUP_REASON_CODES[reason], 0, detail)


def encode_prepared(nonce: bytes, p: Prepared) -> bytes:
    pid = p.profile_id.encode("ascii")
    if len(pid) > PROFILE_ID_LEN:
        raise ValueError("profile id too long")
    return _header(PREPARED, nonce) + _BODY[PREPARED].pack(
        pid, bytes.fromhex(p.policy_hash), bytes.fromhex(p.bpf_hash),
        p.filter_flags, p.landlock_abi, p.subreaper)


def decode(data: bytes, nonce: bytes) -> Frame:
    """frame 1 個を検査して読む。合わなければ `MalformedFrame`。"""
    if len(data) < HEADER_LEN:
        raise MalformedFrame(f"short frame: {len(data)}")
    magic, version, ftype, got_nonce = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise MalformedFrame("magic")
    if version != VERSION:
        raise MalformedFrame(f"version {version}")
    if ftype not in _BODY:
        raise MalformedFrame(f"unknown type {ftype}")
    if len(data) != FRAME_LEN[ftype]:
        raise MalformedFrame(f"length {len(data)} for {TYPE_NAMES[ftype]}")
    # nonce の比較は定数時間で行う
    if not secrets.compare_digest(got_nonce, nonce):
        raise MalformedFrame("nonce")
    body = _BODY[ftype].unpack_from(data, HEADER_LEN)
    if ftype in (HELLO,):
        return Frame(ftype, got_nonce, pid=body[0])
    if ftype == EXEC_FAILED:
        return Frame(ftype, got_nonce, errno=body[0])
    if ftype == SETUP_FAILED:
        code, reserved, detail = body
        if reserved != 0 or code not in SETUP_REASONS:
            raise MalformedFrame(f"setup reason {code}/{reserved}")
        return Frame(ftype, got_nonce, reason=SETUP_REASONS[code], errno=detail)
    if ftype == PREPARED:
        raw_id, ph, bh, flags, abi, subreaper = body
        stripped = raw_id.rstrip(b"\0")
        if b"\0" in stripped:
            raise MalformedFrame("profile id")
        try:
            profile_id = stripped.decode("ascii")
        except UnicodeDecodeError:
            raise MalformedFrame("profile id") from None
        return Frame(ftype, got_nonce, prepared=Prepared(
            profile_id, ph.hex(), bh.hex(), flags, abi, subreaper))
    return Frame(ftype, got_nonce)


def take_fds(ancdata: list) -> tuple[list[int], list[tuple[int, int]]]:
    """ancillary から SCM_RIGHTS の fd を全部取り出す。

    返り値は (受け取った fd、SCM_RIGHTS 以外の (level, type))。受け取った fd は
    呼び出し側が必ず閉じるか所有する。"""
    fds: list[int] = []
    other: list[tuple[int, int]] = []
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            usable = len(cdata) - len(cdata) % 4
            fds.extend(struct.unpack(f"<{usable // 4}i", cdata[:usable]))
        else:
            other.append((level, ctype))
    return fds, other


def close_fds(fds: list[int]) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass
