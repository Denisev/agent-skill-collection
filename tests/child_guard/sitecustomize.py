"""Test-only isolation and fault-injection guard for Checkpoint 6C children."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
import sys


_PARENT = Path(os.environ["SIX_C_DISPOSABLE_PARENT"]).resolve()
_SOURCE = Path(os.environ["SIX_C_SOURCE_ROOT"]).resolve()
_GUARD = Path(__file__).parent.resolve()
_RUNTIME_ROOTS = tuple(
    Path(value).resolve()
    for value in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
)
_READ_ROOTS = (_PARENT, _SOURCE, _GUARD, *_RUNTIME_ROOTS)
_FORCE_CLEANUP_FAILURE = os.environ.get("SIX_C_FORCE_CLEANUP_FAILURE") == "1"

_real_stat = os.stat
_real_lstat = os.lstat
_real_readlink = os.readlink
_real_open = os.open
_real_access = os.access


def _absolute(path: str | bytes, *, dir_fd: int | None = None) -> Path:
    candidate = Path(os.fsdecode(path))
    if candidate.is_absolute():
        return candidate
    if dir_fd is None:
        return Path.cwd() / candidate
    try:
        anchor = Path(_real_readlink(f"/proc/self/fd/{dir_fd}"))
    except OSError:
        raw = fcntl.fcntl(dir_fd, 50, b"\0" * 1024)
        anchor = Path(raw.split(b"\0", 1)[0].decode())
    return anchor / candidate


def _beneath(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _physical(path: Path) -> Path:
    """Resolve existing symlink components using the unguarded metadata calls."""
    pending = list(path.parts[1:])
    current = Path(path.anchor)
    followed = 0
    while pending:
        component = pending.pop(0)
        if component in ("", "."):
            continue
        if component == "..":
            current = current.parent
            continue
        candidate = current / component
        try:
            metadata = _real_lstat(candidate)
        except OSError:
            current = candidate
            while pending:
                component = pending.pop(0)
                if component in ("", "."):
                    continue
                current = current.parent if component == ".." else current / component
            return current
        if not stat.S_ISLNK(metadata.st_mode):
            current = candidate
            continue
        followed += 1
        if followed > 40:
            raise AssertionError(f"forbidden child symlink loop: {path}")
        target = Path(_real_readlink(candidate))
        if target.is_absolute():
            pending = list(target.parts[1:]) + pending
            current = Path(target.anchor)
        else:
            pending = list(target.parts) + pending
    return current


def _require_read(path: str | bytes, *, dir_fd: int | None = None) -> None:
    lexical = _absolute(path, dir_fd=dir_fd)
    normalized = Path(os.path.normpath(lexical))
    if normalized != Path("/dev/null") and not _beneath(normalized, _READ_ROOTS):
        os.write(2, f"6C guard denied read: {normalized}\n".encode())
        raise AssertionError(f"forbidden child read: {normalized}")
    candidate = _physical(lexical)
    if candidate != Path("/dev/null") and not _beneath(candidate, _READ_ROOTS):
        os.write(2, f"6C guard denied read: {candidate}\n".encode())
        raise AssertionError(f"forbidden child read: {candidate}")


def _require_mutation(path: str | bytes, *, dir_fd: int | None = None) -> None:
    lexical = _absolute(path, dir_fd=dir_fd)
    normalized = Path(os.path.normpath(lexical))
    if not _beneath(normalized, (_PARENT,)):
        os.write(2, f"6C guard denied mutation: {normalized}\n".encode())
        raise AssertionError(f"forbidden child mutation: {normalized}")
    candidate = _physical(lexical)
    if not _beneath(candidate, (_PARENT,)):
        os.write(2, f"6C guard denied mutation: {candidate}\n".encode())
        raise AssertionError(f"forbidden child mutation: {candidate}")


def _require_metadata(path: str | bytes, *, dir_fd: int | None = None) -> None:
    lexical = _absolute(path, dir_fd=dir_fd)
    normalized = Path(os.path.normpath(lexical))
    allowed_ancestor = normalized in _PARENT.parents
    if not _beneath(normalized, _READ_ROOTS) and not allowed_ancestor:
        os.write(2, f"6C guard denied metadata read: {normalized}\n".encode())
        raise AssertionError(f"forbidden child metadata read: {normalized}")
    candidate = _physical(lexical)
    if not _beneath(candidate, _READ_ROOTS) and candidate not in _PARENT.parents:
        os.write(2, f"6C guard denied metadata read: {candidate}\n".encode())
        raise AssertionError(f"forbidden child metadata read: {candidate}")


def _require_fd_read(fd: int) -> None:
    _require_read(".", dir_fd=fd)


def _write_open(flags: int) -> bool:
    return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))


def _temporary_cleanup_event(event: str, args: tuple[object, ...]) -> bool:
    if not _FORCE_CLEANUP_FAILURE or event not in {"os.remove", "os.unlink"} or not args:
        return False
    path = args[0]
    if (
        not isinstance(path, (str, bytes))
        or not Path(os.fsdecode(path)).name.startswith(".skill-collection.toml.tmp-")
    ):
        return False
    dir_fd = args[1] if len(args) > 1 and isinstance(args[1], int) and args[1] >= 0 else None
    try:
        metadata = _real_stat("skill-collection.toml", dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode)


def _audit(event: str, args: tuple[object, ...]) -> None:
    if _temporary_cleanup_event(event, args):
        os.write(2, f"6C guard injected cleanup failure: {args!r}\n".encode())
        raise OSError("test-forced temporary cleanup failure")
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp"}:
        raise AssertionError(f"forbidden child process: {event}")
    if event.startswith("socket.") or event.startswith("urllib.") or event.startswith("http.client."):
        raise AssertionError(f"forbidden child network access: {event}")
    if event in {"os.putenv", "os.unsetenv", "os.chdir"}:
        raise AssertionError(f"forbidden child global mutation: {event}")
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if _write_open(flags):
            _require_mutation(args[0])
        elif flags & getattr(os, "O_DIRECTORY", 0):
            _require_metadata(args[0])
        else:
            _require_read(args[0])
    elif event in {"os.listdir", "os.scandir"} and args:
        if isinstance(args[0], (str, bytes)):
            _require_read(args[0])
        elif isinstance(args[0], int):
            _require_fd_read(args[0])
    elif (
        event in {
            "os.remove", "os.unlink", "os.rmdir", "os.mkdir", "os.chmod",
            "os.chown", "os.truncate", "os.utime", "os.setxattr",
            "os.removexattr", "os.mkfifo", "os.mknod", "os.chflags",
            "os.lchflags",
        }
        and args
        and isinstance(args[0], (str, bytes))
    ):
        dir_fd_index = {
            "os.remove": 1, "os.unlink": 1, "os.rmdir": 1,
            "os.mkdir": 2, "os.chmod": 2, "os.chown": 3, "os.utime": 3,
            "os.mkfifo": 2, "os.mknod": 3,
        }.get(event)
        dir_fd = (
            args[dir_fd_index]
            if dir_fd_index is not None and len(args) > dir_fd_index
            and isinstance(args[dir_fd_index], int) and args[dir_fd_index] >= 0
            else None
        )
        _require_mutation(args[0], dir_fd=dir_fd)
    elif event in {"os.rename", "os.replace", "os.link"} and len(args) >= 2:
        if isinstance(args[0], (str, bytes)):
            src_fd = args[2] if len(args) > 2 and isinstance(args[2], int) and args[2] >= 0 else None
            _require_mutation(args[0], dir_fd=src_fd)
        if isinstance(args[1], (str, bytes)):
            dst_fd = args[3] if len(args) > 3 and isinstance(args[3], int) and args[3] >= 0 else None
            _require_mutation(args[1], dir_fd=dst_fd)
    elif event == "os.symlink" and len(args) >= 2 and isinstance(args[1], (str, bytes)):
        dir_fd = args[2] if len(args) > 2 and isinstance(args[2], int) and args[2] >= 0 else None
        _require_mutation(args[1], dir_fd=dir_fd)


def _guarded_stat(path: str | bytes | int, *args: object, **kwargs: object) -> os.stat_result:
    if isinstance(path, (str, bytes)):
        dir_fd = kwargs.get("dir_fd")
        _require_metadata(path, dir_fd=dir_fd if isinstance(dir_fd, int) else None)
    return _real_stat(path, *args, **kwargs)


def _guarded_lstat(path: str | bytes, *args: object, **kwargs: object) -> os.stat_result:
    dir_fd = kwargs.get("dir_fd")
    _require_metadata(path, dir_fd=dir_fd if isinstance(dir_fd, int) else None)
    return _real_lstat(path, *args, **kwargs)


def _guarded_readlink(path: str | bytes, *args: object, **kwargs: object) -> str | bytes:
    dir_fd = kwargs.get("dir_fd")
    _require_metadata(path, dir_fd=dir_fd if isinstance(dir_fd, int) else None)
    return _real_readlink(path, *args, **kwargs)


def _guarded_open(
    path: str | bytes, flags: int, mode: int = 0o777, *, dir_fd: int | None = None,
) -> int:
    if _write_open(flags):
        _require_mutation(path, dir_fd=dir_fd)
    elif flags & getattr(os, "O_DIRECTORY", 0):
        _require_metadata(path, dir_fd=dir_fd)
    else:
        _require_read(path, dir_fd=dir_fd)
    return _real_open(path, flags, mode, dir_fd=dir_fd)


def _guarded_access(
    path: str | bytes, mode: int, *, dir_fd: int | None = None,
    effective_ids: bool = False, follow_symlinks: bool = True,
) -> bool:
    _require_metadata(path, dir_fd=dir_fd)
    return _real_access(
        path, mode, dir_fd=dir_fd, effective_ids=effective_ids,
        follow_symlinks=follow_symlinks,
    )


sys.addaudithook(_audit)
os.stat = _guarded_stat  # type: ignore[assignment]
os.lstat = _guarded_lstat  # type: ignore[assignment]
os.readlink = _guarded_readlink  # type: ignore[assignment]
os.open = _guarded_open  # type: ignore[assignment]
os.access = _guarded_access  # type: ignore[assignment]
os.supports_dir_fd.add(_guarded_stat)
os.supports_dir_fd.add(_guarded_readlink)
os.supports_dir_fd.add(_guarded_open)
os.supports_dir_fd.add(_guarded_access)
os.supports_follow_symlinks.add(_guarded_stat)
os.supports_follow_symlinks.add(_guarded_access)
os.supports_effective_ids.add(_guarded_access)
