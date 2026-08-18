from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from typing import Literal, TypeAlias


FilesystemKind: TypeAlias = Literal[
    "absent",
    "directory",
    "regular-file",
    "symlink",
    "broken-symlink",
    "looping-symlink",
    "fifo",
    "socket",
    "block-device",
    "character-device",
    "unreadable",
]


def classify_path(path: Path) -> FilesystemKind:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    kind = kind_from_mode(metadata.st_mode)
    if kind != "symlink":
        return kind
    symlink_kind, _, _ = inspect_symlink(path)
    return symlink_kind


def kind_from_mode(mode: int) -> FilesystemKind:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular-file"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block-device"
    if stat.S_ISCHR(mode):
        return "character-device"
    return "unreadable"


def inspect_symlink(
    path: Path,
) -> tuple[FilesystemKind, str | None, Path | None]:
    try:
        link_text = os.readlink(path)
    except OSError:
        return "unreadable", None, None
    try:
        resolved = path.resolve(strict=True)
    except RuntimeError:
        return "looping-symlink", link_text, None
    except OSError as error:
        kind: FilesystemKind = (
            "looping-symlink" if error.errno == errno.ELOOP else "broken-symlink"
        )
        return kind, link_text, None
    return "symlink", link_text, resolved
