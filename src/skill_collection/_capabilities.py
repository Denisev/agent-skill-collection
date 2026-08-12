from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from typing import Literal


CapabilityProbeResult = Literal["supported", "unsupported", "target-unavailable"]

_TARGET_ERRNOS = frozenset(
    value
    for name in ("ENOENT", "ENOTDIR", "EACCES", "EPERM", "ELOOP", "ESTALE")
    if (value := getattr(errno, name, None)) is not None
)
_UNSUPPORTED_FSYNC_ERRNOS = frozenset(
    value
    for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP")
    if (value := getattr(errno, name, None)) is not None
)


def containment_capability() -> CapabilityProbeResult:
    required_dir_fd = (os.open, os.stat, os.readlink, os.mkdir, os.symlink, os.unlink, os.rmdir, os.link)
    if (
        getattr(os, "O_NOFOLLOW", None) is None
        or getattr(os, "O_DIRECTORY", None) is None
        or any(function not in os.supports_dir_fd for function in required_dir_fd)
        or os.link not in os.supports_follow_symlinks
        or os.stat not in os.supports_follow_symlinks
    ):
        return "unsupported"
    return "supported"


def directory_fsync_capability(project: Path) -> CapabilityProbeResult:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return "target-unavailable"
    return _fsync_capability(project, expected="directory", flags=os.O_RDONLY | directory_flag)


def regular_file_fsync_capability(binding: Path) -> CapabilityProbeResult:
    nofollow_flag = getattr(os, "O_NOFOLLOW", None)
    if nofollow_flag is None:
        return "target-unavailable"
    return _fsync_capability(binding, expected="regular-file", flags=os.O_RDONLY | nofollow_flag)


def _fsync_capability(path: Path, *, expected: Literal["directory", "regular-file"], flags: int) -> CapabilityProbeResult:
    try:
        before = path.stat(follow_symlinks=False)
        if not _right_type(before.st_mode, expected) or path.is_symlink():
            return "target-unavailable"
        fd = os.open(path, flags)
    except InterruptedError:
        raise
    except OSError as error:
        if error.errno in _TARGET_ERRNOS:
            return "target-unavailable"
        raise

    pending: BaseException | None = None
    traceback = None
    result: CapabilityProbeResult | None = None
    try:
        opened = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        if (
            not _right_type(opened.st_mode, expected)
            or not _right_type(current.st_mode, expected)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            result = "target-unavailable"
        else:
            try:
                os.fsync(fd)
            except InterruptedError:
                raise
            except OSError as error:
                if error.errno in _UNSUPPORTED_FSYNC_ERRNOS:
                    result = "unsupported"
                else:
                    raise
            if result == "supported" or result is None:
                after = path.stat(follow_symlinks=False)
                result = (
                    "supported"
                    if _right_type(after.st_mode, expected)
                    and (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
                    else "target-unavailable"
                )
    except InterruptedError as error:
        pending, traceback = error, error.__traceback__
    except OSError as error:
        if error.errno in _TARGET_ERRNOS:
            result = "target-unavailable"
        else:
            pending, traceback = error, error.__traceback__
    except BaseException as error:
        pending, traceback = error, error.__traceback__

    try:
        os.close(fd)
    except BaseException as close_error:
        if pending is None:
            raise
        try:
            pending.add_note(f"Descriptor close also failed: {type(close_error).__name__}")
        except Exception:
            pass
    if pending is not None:
        raise pending.with_traceback(traceback)
    assert result is not None
    return result


def _right_type(mode: int, expected: str) -> bool:
    return stat.S_ISDIR(mode) if expected == "directory" else stat.S_ISREG(mode)
