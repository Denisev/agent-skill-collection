from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path


@contextmanager
def valid_collection() -> Iterator[Path]:
    source = Path(__file__).parent / "fixtures" / "valid"
    with tempfile.TemporaryDirectory() as directory:
        collection = Path(directory)
        shutil.copytree(source, collection, dirs_exist_ok=True)
        yield collection


def add_discovered_skills(collection: Path, *names: str) -> None:
    for name in names:
        directory = collection / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


def write_binding(
    project: Path,
    *,
    profile: str = "base",
    target: str = ".agents/skills",
) -> None:
    (project / "skill-collection.toml").write_text(
        f'version = 1\nprofile = "{profile}"\ntarget = "{target}"\n\n'
        '[collection]\nurl = "file:///collection"\n'
        'revision = "0000000000000000000000000000000000000000"\n',
        encoding="utf-8",
    )


def tree_contents(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    if not root.exists() and not root.is_symlink():
        return ((".", "missing", b""),)
    entries: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path).encode()))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        elif path.is_dir():
            entries.append((relative, "directory", b""))
    return tuple(entries)
