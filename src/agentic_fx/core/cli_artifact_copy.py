"""改善 mission の CLI 実行 artifact を封印した memfd に写す (launcher が mission cgroup の中で使う)。

launcher は mission cgroup に入った後で、worker から受け取った artifact の fd を先頭から読んで
memfd に全量を写し、封印 (SHRINK / GROW / WRITE / SEAL) してから SHA-256 を取り、afx が
admission で見た digest と照合する。exec するのはこの memfd なので、照合の後に元の file が
書き換えられても exec される bytes は変わらない。memfd の page (shmem) は作成・書込みをした
process の cgroup に課金されるので、mission cgroup の `memory.max` の内側に入る。

launcher の source hash (fingerprint の材料) に入るので、本 module は標準ライブラリだけを
import する。
"""
import ctypes
import errno
import fcntl
import hashlib
import os
import stat
import struct

# memfd と封印の定数 (`os.memfd_create` と `fcntl.F_ADD_SEALS` を持たない Python の build でも
# 使えるよう、kernel の値を直接持つ)
MFD_CLOEXEC = 0x0001
MFD_ALLOW_SEALING = 0x0002
#: memfd を実行可能で作る flag (`vm.memfd_noexec` が 1 の host でも exec できるように)。
#: 古い kernel は EINVAL を返すので、そのときは付けずに作り直す
MFD_EXEC = 0x0010
F_ADD_SEALS = 1033
F_GET_SEALS = 1034
F_SEAL_SEAL = 0x0001
F_SEAL_SHRINK = 0x0002
F_SEAL_GROW = 0x0004
F_SEAL_WRITE = 0x0008
#: exec 用の複製に掛ける封印 (内容と大きさを固定し、封印も外せなくする)
EXEC_COPY_SEALS = F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL

#: `os.memfd_create` が無いときに直接呼ぶ syscall 番号 (arch ごとに固定。他の arch は呼ばない)
SYS_MEMFD_CREATE = {"x86_64": 319, "aarch64": 279}

MEMFD_NAME = b"afx-cli"

_CHUNK = 1 << 20


class ArtifactCopyChanged(OSError):
    """写している途中で元の file の大きさか mtime が変わった。"""


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _machine() -> str:
    return os.uname().machine


def memfd_create(name: bytes, flags: int) -> int:
    """`os.memfd_create` があればそれを、無ければ `syscall(SYS_memfd_create)` を直接呼ぶ。

    失敗は全部 `OSError` (未対応の arch・libc に `syscall` が無いときは `ENOSYS`)。"""
    native = getattr(os, "memfd_create", None)
    if native is not None:
        return native(name.decode("ascii"), flags)
    nr = SYS_MEMFD_CREATE.get(_machine())
    if nr is None:
        raise OSError(errno.ENOSYS, f"memfd_create: no syscall number for {_machine()}")
    try:
        syscall = _libc().syscall
        syscall.restype = ctypes.c_long
    except (AttributeError, OSError) as e:
        raise OSError(errno.ENOSYS, f"memfd_create: libc syscall unavailable ({e})") from None
    ctypes.set_errno(0)
    fd = syscall(ctypes.c_long(nr), ctypes.c_char_p(name), ctypes.c_uint(flags))
    if fd < 0:
        err = ctypes.get_errno() or errno.EIO
        raise OSError(err, os.strerror(err))
    return int(fd)


def exec_memfd() -> int:
    flags = MFD_CLOEXEC | MFD_ALLOW_SEALING
    try:
        return memfd_create(MEMFD_NAME, flags | MFD_EXEC)
    except OSError as e:
        if e.errno != errno.EINVAL:
            raise
    return memfd_create(MEMFD_NAME, flags)


def sha256_fd(fd: int) -> str:
    """fd を先頭から pread で読んだ SHA-256 (file offset を動かさない)。"""
    h = hashlib.sha256()
    off = 0
    while True:
        chunk = os.pread(fd, _CHUNK, off)
        if not chunk:
            break
        h.update(chunk)
        off += len(chunk)
    return h.hexdigest()


def sealed_copy(fd: int) -> int:
    """開いた artifact の bytes を封印した memfd (CLOEXEC) に写して返す。

    写す前後で元の file の大きさか mtime が変われば `ArtifactCopyChanged`。途中で作った memfd は
    失敗の経路で閉じる。"""
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactCopyChanged(0, "artifact is not a regular file")
    memfd = exec_memfd()
    try:
        off = 0
        while True:
            chunk = os.pread(fd, _CHUNK, off)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                n = os.pwrite(memfd, view, off)
                view = view[n:]
                off += n
        after = os.fstat(fd)
        if (off != before.st_size or (after.st_size, after.st_mtime_ns)
                != (before.st_size, before.st_mtime_ns)):
            raise ArtifactCopyChanged(0, "artifact changed while copying")
        fcntl.fcntl(memfd, F_ADD_SEALS, EXEC_COPY_SEALS)
        if fcntl.fcntl(memfd, F_GET_SEALS) & EXEC_COPY_SEALS != EXEC_COPY_SEALS:
            raise OSError(errno.EPERM, "memfd seals were not applied")
        if os.fstat(memfd).st_size != before.st_size:
            raise ArtifactCopyChanged(0, "artifact copy has a different size")
    except BaseException:
        os.close(memfd)
        raise
    return memfd


# ELF header の受け入れ値: 64 bit、little endian、x86_64、EXEC / DYN
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_EM_X86_64 = 62
_ET_EXEC_DYN = (2, 3)
_ELF_HEADER_PREFIX = 20


def inspect_cli_artifact(fd: int) -> tuple[tuple[int, int] | None, str]:
    """開いた fd そのものを `fstat` と先頭 bytes で検査し、x86_64 の 64 bit ELF
    (EXEC / DYN) なら `((st_dev, st_ino), "")`、そうでなければ `(None, 理由)` を返す。

    `#!` script は exec 時に fd が CLOEXEC で閉じて interpreter が開けないので落とす
    (codex は npm の wrapper でなく native ELF を渡す)。path は開き直さない。

    multicall binary (uutils coreutils 等) は ELF として通るが、fd 経由の exec では
    呼ばれた名前を判定できず目的の動作にならない。この検査では見分けられない。"""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        return None, "not a regular file"
    head = os.pread(fd, _ELF_HEADER_PREFIX, 0)
    if len(head) < _ELF_HEADER_PREFIX or head[:4] != b"\x7fELF":
        return None, f"not ELF: {head[:4]!r}"
    if head[4] != _ELFCLASS64 or head[5] != _ELFDATA2LSB:
        return None, f"ELF class={head[4]} data={head[5]}"
    e_type, e_machine = struct.unpack_from("<HH", head, 16)
    if e_machine != _EM_X86_64 or e_type not in _ET_EXEC_DYN:
        return None, f"ELF type={e_type} machine={e_machine}"
    return (st.st_dev, st.st_ino), ""
