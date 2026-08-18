from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePath

from ._issues import normalize_issues
from .validation import (
    Location,
    ValidationIssue,
    _CollectionValidationState,
    _validate_collection,
)


@dataclass(frozen=True, slots=True)
class DiscoveredSkill:
    source_id: str
    directory: Location
    skill_file: Location
    directory_name: str
    catalog_skill_id: str | None
    catalog_name: str | None


@dataclass(frozen=True, slots=True)
class ScanResult:
    discovered: tuple[DiscoveredSkill, ...]
    issues: tuple[ValidationIssue, ...]


def scan(collection_root: str | Path) -> ScanResult:
    collection = Path(collection_root)
    state = _validate_collection(collection)
    return _scan_validated(collection, state)


def _scan_validated(
    collection: Path, state: _CollectionValidationState
) -> ScanResult:
    issues = list(state.issues)
    if not collection.is_dir():
        return ScanResult((), tuple(normalize_issues(issues)))

    sources = state.sources
    catalog = state.catalog
    catalog_by_path: dict[tuple[str, str], list[tuple[int, dict[str, object]]]] = {}
    for index, item in enumerate(catalog):
        source = item.get("source")
        path = item.get("path")
        if isinstance(source, str) and isinstance(path, str):
            key = (source, PurePath(path).as_posix())
            catalog_by_path.setdefault(key, []).append((index, item))

    raw_discoveries: list[tuple[str, Path, str]] = []
    collection_resolved = collection.resolve()
    for source_index, source in enumerate(sources):
        source_id = source.get("id")
        source_path = source.get("path")
        skills_root = source.get("skills_root", ".")
        if not all(isinstance(value, str) and value for value in (source_id, source_path, skills_root)):
            continue
        relative_root = PurePath(source_path) / PurePath(skills_root)
        if relative_root.is_absolute() or ".." in relative_root.parts:
            continue
        current_component = collection
        symlink_component = False
        source_component_count = len(PurePath(source_path).parts)
        for component_index, component in enumerate(relative_root.parts):
            current_component = current_component / component
            if current_component.is_symlink():
                try:
                    current_component.resolve(strict=True)
                    code = "source.path_symlink"
                    message = "Discovery does not follow directory symlinks in a Source root."
                except (OSError, RuntimeError):
                    code = "source.path_unavailable"
                    message = "Source root is broken, looping, or unavailable."
                issues.append(
                    ValidationIssue(
                        code,
                        message,
                        Location(
                            "collection",
                            f"sources.toml#sources[{source_index}]."
                            + (
                                "path"
                                if component_index < source_component_count
                                else "skills_root"
                            ),
                        ),
                    )
                )
                symlink_component = True
                break
            if not current_component.exists():
                break
        if symlink_component:
            continue
        failure_field = "skills_root" if PurePath(skills_root).parts else "path"
        try:
            root = (collection / relative_root).resolve(strict=False)
        except (OSError, RuntimeError):
            issues.append(
                ValidationIssue(
                    "source.path_unavailable",
                    "Source root is broken, looping, or unavailable.",
                    Location(
                        "collection",
                        f"sources.toml#sources[{source_index}].{failure_field}",
                    ),
                )
            )
            continue
        if not root.is_relative_to(collection_resolved):
            continue
        if not root.is_dir():
            issues.append(
                ValidationIssue(
                    "source.path_unavailable",
                    "Source root does not exist or is not a directory.",
                    Location(
                        "collection",
                        f"sources.toml#sources[{source_index}].{failure_field}",
                    ),
                )
            )
            continue

        def on_error(error: OSError) -> None:
            filename = Path(error.filename) if error.filename else root
            try:
                relative = filename.resolve(strict=False).relative_to(collection_resolved).as_posix()
            except ValueError:
                relative = relative_root.as_posix()
            issues.append(
                ValidationIssue(
                    "discovery.unreadable",
                    "A Source directory could not be read.",
                    Location("collection", relative),
                )
            )

        for directory, directory_names, filenames in os.walk(
            root, topdown=True, onerror=on_error, followlinks=False
        ):
            current = Path(directory)
            directory_names[:] = sorted(
                name for name in directory_names if not (current / name).is_symlink()
            )
            if "SKILL.md" not in filenames:
                continue
            skill_file = current / "SKILL.md"
            if skill_file.is_symlink() or not skill_file.is_file():
                continue
            relative = current.relative_to(collection_resolved).as_posix()
            raw_discoveries.append((source_id, current, relative))

    raw_discoveries.sort(key=lambda item: (item[0], item[2]))
    discovered: list[DiscoveredSkill] = []
    matched_catalog_indices: set[int] = set()
    for source_id, directory, relative in raw_discoveries:
        matches = catalog_by_path.get((source_id, relative), [])
        catalog_id: str | None = None
        catalog_name: str | None = None
        if len(matches) == 1:
            index, item = matches[0]
            matched_catalog_indices.add(index)
            if isinstance(item.get("id"), str) and isinstance(item.get("name"), str):
                catalog_id = item["id"]
                catalog_name = item["name"]
        elif not matches:
            issues.append(
                ValidationIssue(
                    "discovery.uncataloged",
                    "Discovered directory has no matching Catalog entry.",
                    Location("collection", relative),
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    "discovery.ambiguous_catalog",
                    "Discovered directory matches multiple Catalog entries.",
                    Location("collection", relative),
                    tuple(
                        Location("collection", f"catalog.toml#skills[{index}].path")
                        for index, _ in matches
                    ),
                )
            )
        discovered.append(
            DiscoveredSkill(
                source_id=source_id,
                directory=Location("collection", relative),
                skill_file=Location("collection", f"{relative}/SKILL.md"),
                directory_name=directory.name,
                catalog_skill_id=catalog_id,
                catalog_name=catalog_name,
            )
        )

    for index, item in enumerate(catalog):
        if index in matched_catalog_indices:
            continue
        path = item.get("path")
        if isinstance(path, str):
            issues.append(
                ValidationIssue(
                    "catalog.skill_not_discovered",
                    "Catalog Skill has no matching discovery.",
                    Location("collection", f"catalog.toml#skills[{index}].path"),
                )
            )

    return ScanResult(tuple(discovered), tuple(normalize_issues(issues)))
