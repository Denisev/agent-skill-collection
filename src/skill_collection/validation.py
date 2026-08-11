from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from pathlib import PurePath
import re
import subprocess
import tomllib
from collections.abc import Callable
from typing import Literal
from urllib.parse import urlparse

RootName = Literal["collection", "project"]


@dataclass(frozen=True, order=True, slots=True)
class Location:
    root: RootName
    relative_path: str


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    location: Location
    related_locations: tuple[Location, ...] = ()


_COLLECTION_DOCUMENTS = (
    "catalog.toml",
    "groups.toml",
    "profiles.toml",
    "sources.toml",
)


def validate(
    collection_root: str | Path, project_root: str | Path | None = None
) -> list[ValidationIssue]:
    """Return deterministic validation issues without changing either root."""
    collection = Path(collection_root)
    if not collection.exists() or not collection.is_dir():
        return [
            ValidationIssue(
                "root.missing",
                "Collection root does not exist or is not a directory.",
                Location("collection", "."),
            )
        ]

    issues: list[ValidationIssue] = [
        ValidationIssue(
            "document.missing",
            f"Required collection document {name} is missing.",
            Location("collection", name),
        )
        for name in _COLLECTION_DOCUMENTS
        if not (collection / name).is_file()
    ]

    documents: dict[str, dict[str, object]] = {}
    for name in _COLLECTION_DOCUMENTS:
        path = collection / name
        if not path.is_file():
            continue
        parsed = _read_toml(path, Location("collection", name), issues)
        if parsed is not None:
            documents[name] = parsed

    sources = _document_entries(documents.get("sources.toml"), "sources.toml", "sources", issues)
    catalog = _document_entries(documents.get("catalog.toml"), "catalog.toml", "skills", issues)
    groups = _document_entries(documents.get("groups.toml"), "groups.toml", "groups", issues)
    profiles = _document_entries(documents.get("profiles.toml"), "profiles.toml", "profiles", issues)

    _validate_sources(collection, sources, issues)
    skill_ids = _validate_catalog(collection, catalog, sources, issues)
    group_map = _validate_groups(groups, skill_ids, issues)
    profile_map, invalid_profiles = _validate_profiles(profiles, skill_ids, group_map, issues)

    if project_root is not None:
        project = Path(project_root)
        if not project.exists() or not project.is_dir():
            issues.append(
                ValidationIssue(
                    "root.missing",
                    "Project root does not exist or is not a directory.",
                    Location("project", "."),
                )
            )
        else:
            _validate_project(
                collection,
                project,
                catalog,
                profile_map,
                invalid_profiles,
                group_map,
                issues,
            )

    return _sorted(issues)


def _sorted(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return sorted(
        issues,
        key=lambda issue: (
            issue.code,
            issue.location.root,
            issue.location.relative_path,
            tuple((item.root, item.relative_path) for item in issue.related_locations),
            issue.message,
        ),
    )


def _read_toml(
    path: Path, location: Location, issues: list[ValidationIssue]
) -> dict[str, object] | None:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (tomllib.TOMLDecodeError, OSError) as error:
        issues.append(ValidationIssue("toml.malformed", str(error), location))
        return None
    return value


def _document_entries(
    document: dict[str, object] | None,
    filename: str,
    key: str,
    issues: list[ValidationIssue],
) -> list[dict[str, object]]:
    if document is None:
        return []
    allowed_root = {"version", key}
    if filename == "catalog.toml":
        allowed_root.add("collection_revision")
    for property_name in document.keys() - allowed_root:
        _unexpected(issues, "collection", f"{filename}#{property_name}")
    if "version" not in document:
        _required(issues, "collection", f"{filename}#version")
    elif type(document["version"]) is not int or document["version"] != 1:
        _invalid(issues, "collection", f"{filename}#version")
    if filename == "catalog.toml":
        revision = document.get("collection_revision")
        if "collection_revision" not in document:
            _required(issues, "collection", "catalog.toml#collection_revision")
        elif not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
            _invalid(issues, "collection", "catalog.toml#collection_revision")
    if key not in document:
        _required(issues, "collection", f"{filename}#{key}")
        return []
    value = document[key]
    if not isinstance(value, list):
        _invalid(issues, "collection", f"{filename}#{key}")
        return []
    result: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            result.append(item)
        else:
            _invalid(issues, "collection", f"{filename}#{key}[{index}]")
    return result


def _required(
    issues: list[ValidationIssue], root: RootName, relative_path: str
) -> None:
    issues.append(
        ValidationIssue(
            "field.required", "Required field is missing.", Location(root, relative_path)
        )
    )


def _invalid(
    issues: list[ValidationIssue], root: RootName, relative_path: str
) -> None:
    issues.append(
        ValidationIssue(
            "field.invalid", "Field has an invalid type or value.",
            Location(root, relative_path),
        )
    )


def _unexpected(
    issues: list[ValidationIssue], root: RootName, relative_path: str
) -> None:
    issues.append(
        ValidationIssue(
            "field.unexpected", "Field is not allowed by the schema.",
            Location(root, relative_path),
        )
    )


def _duplicate(
    issues: list[ValidationIssue], root: RootName, relative_path: str
) -> None:
    issues.append(
        ValidationIssue(
            "field.duplicate", "Array value must be unique.",
            Location(root, relative_path),
        )
    )


_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _validate_identifier(
    value: object, root: RootName, relative_path: str, issues: list[ValidationIssue]
) -> bool:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _invalid(issues, root, relative_path)
        return False
    return True


def _validate_allowed_fields(
    item: dict[str, object],
    allowed: set[str],
    root: RootName,
    prefix: str,
    issues: list[ValidationIssue],
) -> None:
    for field in item.keys() - allowed:
        _unexpected(issues, root, f"{prefix}.{field}")


def _require_string(
    item: dict[str, object],
    field: str,
    root: RootName,
    prefix: str,
    issues: list[ValidationIssue],
) -> bool:
    if field not in item:
        _required(issues, root, f"{prefix}.{field}")
        return False
    if not isinstance(item[field], str) or not item[field]:
        _invalid(issues, root, f"{prefix}.{field}")
        return False
    return True


def _validate_name_array(
    item: dict[str, object],
    field: str,
    root: RootName,
    prefix: str,
    issues: list[ValidationIssue],
) -> bool:
    if field not in item:
        return True
    value = item[field]
    if not isinstance(value, list):
        _invalid(issues, root, f"{prefix}.{field}")
        return False
    valid = True
    seen: list[object] = []
    for index, entry in enumerate(value):
        if entry in seen:
            _duplicate(issues, root, f"{prefix}.{field}[{index}]")
            valid = False
        else:
            seen.append(entry)
        if not _validate_identifier(entry, root, f"{prefix}.{field}[{index}]", issues):
            valid = False
    return valid


def _report_identity_duplicates(
    items: list[dict[str, object]],
    field: str,
    code: str,
    filename: str,
    array_name: str,
    issues: list[ValidationIssue],
) -> None:
    locations: dict[str, list[Location]] = {}
    for index, item in enumerate(items):
        value = item.get(field)
        if isinstance(value, str):
            locations.setdefault(value, []).append(
                Location("collection", f"{filename}#{array_name}[{index}].{field}")
            )
    for value, entries in locations.items():
        if len(entries) > 1:
            issues.append(
                ValidationIssue(
                    code,
                    f"Identity {value!r} is declared more than once.",
                    entries[0],
                    tuple(entries[1:]),
                )
            )


def _names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _validate_sources(
    collection: Path,
    sources: list[dict[str, object]],
    issues: list[ValidationIssue],
) -> None:
    _report_identity_duplicates(
        sources, "id", "source.duplicate_id", "sources.toml", "sources", issues
    )
    for index, source in enumerate(sources):
        prefix = f"sources.toml#sources[{index}]"
        location = Location("collection", prefix)
        _validate_allowed_fields(
            source,
            {"id", "kind", "path", "url", "skills_root"},
            "collection",
            prefix,
            issues,
        )
        kind = source.get("kind")
        path = source.get("path")
        valid = _require_string(source, "id", "collection", prefix, issues)
        if valid:
            valid = _validate_identifier(source["id"], "collection", f"{prefix}.id", issues)
        valid = _require_string(source, "kind", "collection", prefix, issues) and valid
        if isinstance(kind, str) and kind not in ("collection", "git-submodule"):
            _invalid(issues, "collection", f"{prefix}.kind")
            valid = False
        valid = _require_string(source, "path", "collection", prefix, issues) and valid
        if "skills_root" in source:
            valid = _require_string(source, "skills_root", "collection", prefix, issues) and valid
        if kind == "git-submodule":
            valid = _require_string(source, "url", "collection", prefix, issues) and valid
            if isinstance(source.get("url"), str) and not urlparse(source["url"]).scheme:
                _invalid(issues, "collection", f"{prefix}.url")
                valid = False
        elif "url" in source:
            _invalid(issues, "collection", f"{prefix}.url")
            valid = False
        if not valid:
            issues.append(
                ValidationIssue(
                    "source.invalid", "Source is missing required or valid fields.",
                    location,
                )
            )
            continue
        assert isinstance(path, str)
        source_path = _resolve_confined(collection, path)
        if source_path is None:
            issues.append(
                ValidationIssue(
                    "source.path_outside_collection",
                    "Source path must remain inside the collection root.",
                    Location("collection", f"{prefix}.path"),
                )
            )
            continue
        skills_root = source.get("skills_root", ".")
        assert isinstance(skills_root, str)
        if _resolve_confined(source_path, skills_root) is None:
            issues.append(
                ValidationIssue(
                    "source.path_outside_collection",
                    "Source skills root must remain inside the Source path.",
                    Location("collection", f"{prefix}.skills_root"),
                )
            )
            continue
        if kind == "git-submodule":
            if not source_path.is_dir():
                issues.append(
                    ValidationIssue("source.submodule_missing", "Submodule is not initialized.", location)
                )
                continue
            submodule = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-C",
                    str(collection),
                    "submodule",
                    "status",
                    "--",
                    path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
            if submodule.returncode != 0 or not submodule.stdout.strip():
                issues.append(
                    ValidationIssue(
                        "source.submodule_invalid",
                        "Source path is not a registered Git submodule.",
                        location,
                    )
                )
                continue
            if submodule.stdout.startswith("-"):
                issues.append(
                    ValidationIssue(
                        "source.submodule_missing", "Submodule is not initialized.", location
                    )
                )
                continue
            if submodule.stdout.startswith(("+", "U")):
                issues.append(
                    ValidationIssue(
                        "source.submodule_unpinned",
                        "Submodule worktree is not at the parent repository pin.",
                        location,
                    )
                )
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(source_path), "status", "--porcelain"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
            if result.returncode != 0:
                issues.append(
                    ValidationIssue("source.submodule_invalid", "Source path is not a Git worktree.", location)
                )
            elif result.stdout:
                issues.append(
                    ValidationIssue(
                        "source.submodule_dirty", "Submodule contains uncommitted changes.",
                        Location("collection", path)
                    )
                )


def _resolve_confined(root: Path, relative_path: str) -> Path | None:
    candidate_path = PurePath(relative_path)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        return None
    resolved_root = root.resolve()
    resolved_candidate = (root / candidate_path).resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_root):
        return None
    return resolved_candidate


def _validate_catalog(
    collection: Path,
    skills: list[dict[str, object]],
    sources: list[dict[str, object]],
    issues: list[ValidationIssue],
) -> set[str]:
    _report_identity_duplicates(
        skills, "id", "skill.duplicate_id", "catalog.toml", "skills", issues
    )
    source_map = {
        source["id"]: source
        for source in sources
        if isinstance(source.get("id"), str)
        and isinstance(source.get("path"), str)
    }
    for index, skill in enumerate(skills):
        prefix = f"catalog.toml#skills[{index}]"
        _validate_allowed_fields(
            skill,
            {"id", "name", "source", "path", "description", "content_hash"},
            "collection",
            prefix,
            issues,
        )
        for field in ("id", "name", "source", "path", "content_hash"):
            _require_string(skill, field, "collection", prefix, issues)
        for field in ("id", "name", "source"):
            if isinstance(skill.get(field), str):
                _validate_identifier(
                    skill[field], "collection", f"{prefix}.{field}", issues
                )
        if "description" in skill and not isinstance(skill["description"], str):
            _invalid(issues, "collection", f"{prefix}.description")
        content_hash = skill.get("content_hash")
        if isinstance(content_hash, str) and _CONTENT_HASH.fullmatch(content_hash) is None:
            _invalid(issues, "collection", f"{prefix}.content_hash")
        source_id = skill.get("source")
        if isinstance(source_id, str) and source_id not in source_map:
            issues.append(
                ValidationIssue(
                    "skill.source_missing",
                    f"Skill references missing Source {source_id!r}.",
                    Location("collection", f"{prefix}.source"),
                )
            )
        path_value = skill.get("path")
        source = source_map.get(source_id) if isinstance(source_id, str) else None
        if isinstance(path_value, str) and source is not None:
            source_path = PurePath(str(source["path"])) / str(source.get("skills_root", "."))
            skill_path = PurePath(path_value)
            lexical_outside = (
                skill_path.is_absolute()
                or ".." in skill_path.parts
                or not skill_path.is_relative_to(source_path)
            )
            resolved_source = _resolve_confined(collection, source_path.as_posix())
            resolved_skill = _resolve_confined(collection, skill_path.as_posix())
            resolved_outside = (
                resolved_source is None
                or resolved_skill is None
                or not resolved_skill.is_relative_to(resolved_source)
            )
            outside = lexical_outside or resolved_outside
            if outside:
                issues.append(
                    ValidationIssue(
                        "skill.path_outside_source",
                        "Skill path is outside its declared Source root.",
                        Location("collection", f"{prefix}.path"),
                        (
                            Location(
                                "collection",
                                f"sources.toml#sources[{sources.index(source)}].path",
                            ),
                        ),
                    )
                )
    identities = {
        item["id"] for item in skills if isinstance(item.get("id"), str)
    }
    by_name: dict[str, list[int]] = {}
    for index, skill in enumerate(skills):
        name = skill.get("name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(index)
    for name, indices in by_name.items():
        if len(indices) > 1:
            primary, *related = indices
            issues.append(
                ValidationIssue(
                    "skill.name_collision",
                    f"Multiple Skills use the Codex-facing name {name!r}.",
                    Location("collection", f"catalog.toml#skills[{primary}]"),
                    tuple(Location("collection", f"catalog.toml#skills[{item}]") for item in related),
                )
            )
    return identities


def _validate_groups(
    groups: list[dict[str, object]],
    skill_ids: set[str],
    issues: list[ValidationIssue],
) -> dict[str, dict[str, object]]:
    _report_identity_duplicates(
        groups, "name", "group.duplicate_name", "groups.toml", "groups", issues
    )
    group_map = {
        item["name"]: item for item in groups if isinstance(item.get("name"), str)
    }
    indices = {
        item["name"]: index for index, item in enumerate(groups)
        if isinstance(item.get("name"), str)
    }
    for index, group in enumerate(groups):
        prefix = f"groups.toml#groups[{index}]"
        _validate_allowed_fields(
            group,
            {"name", "description", "skills", "groups"},
            "collection",
            prefix,
            issues,
        )
        _require_string(group, "name", "collection", prefix, issues)
        if isinstance(group.get("name"), str):
            _validate_identifier(group["name"], "collection", f"{prefix}.name", issues)
        if "description" in group:
            _require_string(group, "description", "collection", prefix, issues)
        skills_valid = _validate_name_array(group, "skills", "collection", prefix, issues)
        groups_valid = _validate_name_array(group, "groups", "collection", prefix, issues)
        if "skills" not in group and "groups" not in group:
            _required(issues, "collection", f"{prefix}.skills-or-groups")
        for skill_index, skill in enumerate(_names(group.get("skills")) if skills_valid else []):
            if skill not in skill_ids:
                issues.append(
                    ValidationIssue(
                        "skill.missing", f"Group references missing Skill {skill!r}.",
                        Location("collection", f"{prefix}.skills[{skill_index}]")
                    )
                )
        for group_index, nested in enumerate(_names(group.get("groups")) if groups_valid else []):
            if nested not in group_map:
                issues.append(
                    ValidationIssue(
                        "group.missing", f"Group references missing Group {nested!r}.",
                        Location("collection", f"{prefix}.groups[{group_index}]")
                    )
                )
    _report_cycles(
        group_map,
        lambda item: _names(item.get("groups")),
        indices,
        "group.cycle",
        "groups.toml#groups",
        issues,
    )
    return group_map


def _validate_profiles(
    profiles: list[dict[str, object]],
    skill_ids: set[str],
    groups: dict[str, dict[str, object]],
    issues: list[ValidationIssue],
) -> tuple[dict[str, dict[str, object]], set[str]]:
    _report_identity_duplicates(
        profiles,
        "name",
        "profile.duplicate_name",
        "profiles.toml",
        "profiles",
        issues,
    )
    profile_map = {
        item["name"]: item for item in profiles if isinstance(item.get("name"), str)
    }
    indices = {
        item["name"]: index for index, item in enumerate(profiles)
        if isinstance(item.get("name"), str)
    }
    for index, profile in enumerate(profiles):
        prefix = f"profiles.toml#profiles[{index}]"
        location = Location("collection", prefix)
        _validate_allowed_fields(
            profile,
            {"name", "description", "inherits", "groups", "skills", "add", "remove"},
            "collection",
            prefix,
            issues,
        )
        _require_string(profile, "name", "collection", prefix, issues)
        if isinstance(profile.get("name"), str):
            _validate_identifier(profile["name"], "collection", f"{prefix}.name", issues)
        if "description" in profile:
            _require_string(profile, "description", "collection", prefix, issues)
        valid_arrays = {
            key: _validate_name_array(profile, key, "collection", prefix, issues)
            for key in ("inherits", "groups", "skills", "add", "remove")
        }
        if not any(field in profile for field in ("inherits", "groups", "skills", "add")):
            _required(issues, "collection", f"{prefix}.selection")
        for key in ("skills", "add"):
            for skill_index, skill in enumerate(
                _names(profile.get(key)) if valid_arrays[key] else []
            ):
                if skill not in skill_ids:
                    issues.append(ValidationIssue("skill.missing", f"Profile references missing Skill {skill!r}.", Location("collection", f"{prefix}.{key}[{skill_index}]")))
        for group_index, group in enumerate(
            _names(profile.get("groups")) if valid_arrays["groups"] else []
        ):
            if group not in groups:
                issues.append(ValidationIssue("group.missing", f"Profile references missing Group {group!r}.", Location("collection", f"{prefix}.groups[{group_index}]")))
        for parent_index, parent in enumerate(
            _names(profile.get("inherits")) if valid_arrays["inherits"] else []
        ):
            if parent not in profile_map:
                issues.append(ValidationIssue("profile.missing", f"Profile inherits missing Profile {parent!r}.", Location("collection", f"{prefix}.inherits[{parent_index}]")))
    cyclic_profiles = _report_cycles(
        profile_map,
        lambda item: _names(item.get("inherits")),
        indices,
        "profile.inheritance_cycle",
        "profiles.toml#profiles",
        issues,
    )
    invalid_profiles = set(cyclic_profiles)
    changed = True
    while changed:
        changed = False
        for name, profile in profile_map.items():
            if name not in invalid_profiles and any(
                parent in invalid_profiles for parent in _names(profile.get("inherits"))
            ):
                invalid_profiles.add(name)
                changed = True
    for name in profile_map:
        if name not in invalid_profiles:
            _resolve_profile(
                name, profile_map, groups, skill_ids, issues, indices, invalid_profiles
            )
    return profile_map, invalid_profiles


def _report_cycles(
    nodes: dict[str, dict[str, object]],
    edges: Callable[[dict[str, object]], list[str]],
    indices: dict[str, int],
    code: str,
    location_prefix: str,
    issues: list[ValidationIssue],
) -> set[str]:
    visiting: list[str] = []
    visited: set[str] = set()
    cycles: set[tuple[str, ...]] = set()

    def visit(name: str) -> None:
        if name in visiting:
            members = visiting[visiting.index(name):]
            cycles.add(tuple(sorted(members, key=lambda item: indices[item])))
            return
        if name in visited or name not in nodes:
            return
        visiting.append(name)
        for target in edges(nodes[name]):  # type: ignore[operator]
            visit(target)
        visiting.pop()
        visited.add(name)

    for name in nodes:
        visit(name)
    for cycle in sorted(cycles):
        locations = tuple(
            Location("collection", f"{location_prefix}[{indices[name]}]") for name in cycle
        )
        issues.append(ValidationIssue(code, "Reference cycle detected.", locations[0], locations[1:]))
    return {name for cycle in cycles for name in cycle}


def _resolve_group(
    name: str, groups: dict[str, dict[str, object]], seen: set[str] | None = None
) -> set[str]:
    if name not in groups:
        return set()
    seen = set() if seen is None else seen
    if name in seen:
        return set()
    seen.add(name)
    result = set(_names(groups[name].get("skills")))
    for nested in _names(groups[name].get("groups")):
        result.update(_resolve_group(nested, groups, seen))
    return result


def _resolve_profile(
    name: str,
    profiles: dict[str, dict[str, object]],
    groups: dict[str, dict[str, object]],
    skill_ids: set[str],
    issues: list[ValidationIssue] | None = None,
    indices: dict[str, int] | None = None,
    invalid_profiles: set[str] | None = None,
    resolving: set[str] | None = None,
) -> set[str]:
    invalid_profiles = set() if invalid_profiles is None else invalid_profiles
    if name in invalid_profiles:
        return set()
    profile = profiles.get(name)
    if profile is None:
        return set()
    resolving = set() if resolving is None else resolving
    if name in resolving:
        return set()
    resolving = {*resolving, name}
    result: set[str] = set()
    for parent in _names(profile.get("inherits")):
        result.update(
            _resolve_profile(
                parent,
                profiles,
                groups,
                skill_ids,
                invalid_profiles=invalid_profiles,
                resolving=resolving,
            )
        )
    for group in _names(profile.get("groups")):
        result.update(_resolve_group(group, groups))
    result.update(_names(profile.get("skills")))
    result.update(_names(profile.get("add")))
    for skill in _names(profile.get("remove")):
        if skill not in result and issues is not None and indices is not None:
            issues.append(
                ValidationIssue(
                    "skill.remove_missing", f"Profile removes absent Skill {skill!r}.",
                    Location("collection", f"profiles.toml#profiles[{indices[name]}]")
                )
            )
        result.discard(skill)
    return result & skill_ids


def _validate_project(
    collection: Path,
    project: Path,
    catalog: list[dict[str, object]],
    profiles: dict[str, dict[str, object]],
    invalid_profiles: set[str],
    groups: dict[str, dict[str, object]],
    issues: list[ValidationIssue],
) -> None:
    binding_path = project / "skill-collection.toml"
    if not binding_path.is_file():
        issues.append(ValidationIssue("document.missing", "Project Binding is missing.", Location("project", "skill-collection.toml")))
        return
    binding = _read_toml(binding_path, Location("project", "skill-collection.toml"), issues)
    if binding is None:
        return
    binding_prefix = "skill-collection.toml#binding"
    _validate_allowed_fields(
        binding,
        {"version", "collection", "profile", "add", "remove", "target"},
        "project",
        binding_prefix,
        issues,
    )
    if "version" not in binding:
        _required(issues, "project", f"{binding_prefix}.version")
    elif type(binding["version"]) is not int or binding["version"] != 1:
        _invalid(issues, "project", f"{binding_prefix}.version")
    profile_is_valid = _require_string(
        binding, "profile", "project", binding_prefix, issues
    )
    if profile_is_valid:
        profile_is_valid = _validate_identifier(
            binding["profile"], "project", f"{binding_prefix}.profile", issues
        )
    _validate_name_array(binding, "add", "project", binding_prefix, issues)
    _validate_name_array(binding, "remove", "project", binding_prefix, issues)
    if "target" in binding and (
        not isinstance(binding["target"], str) or not binding["target"]
    ):
        _invalid(issues, "project", f"{binding_prefix}.target")
    collection_value = binding.get("collection")
    if not isinstance(collection_value, dict):
        if "collection" not in binding:
            _required(issues, "project", f"{binding_prefix}.collection")
        else:
            _invalid(issues, "project", f"{binding_prefix}.collection")
    else:
        _validate_allowed_fields(
            collection_value,
            {"url", "revision"},
            "project",
            f"{binding_prefix}.collection",
            issues,
        )
        _require_string(
            collection_value,
            "url",
            "project",
            f"{binding_prefix}.collection",
            issues,
        )
        revision_valid = _require_string(
            collection_value,
            "revision",
            "project",
            f"{binding_prefix}.collection",
            issues,
        )
        if revision_valid and _REVISION.fullmatch(str(collection_value["revision"])) is None:
            _invalid(issues, "project", f"{binding_prefix}.collection.revision")
        url_value = collection_value.get("url")
        if isinstance(url_value, str) and not urlparse(url_value).scheme:
            _invalid(issues, "project", f"{binding_prefix}.collection.url")
    target_value = binding.get("target", ".agents/skills")
    target_relative = target_value if isinstance(target_value, str) else ".agents/skills"
    target_path = PurePath(target_relative)
    if target_path.is_absolute() or ".." in target_path.parts:
        issues.append(
            ValidationIssue(
                "binding.target_outside_project",
                "Binding target must remain inside the project root.",
                Location("project", f"{binding_prefix}.target"),
            )
        )
        return
    broken_component = _broken_target_component(project, target_path)
    if broken_component is not None:
        issues.append(
            ValidationIssue(
                "activation.broken_symlink",
                "Binding target contains a broken or looping symlink.",
                Location("project", broken_component.relative_to(project).as_posix()),
            )
        )
        return
    target = project / target_path
    project_resolved = project.resolve()
    if not target.resolve(strict=False).is_relative_to(project_resolved):
        issues.append(
            ValidationIssue(
                "binding.target_outside_project",
                "Binding target resolves outside the project root.",
                Location("project", f"{binding_prefix}.target"),
            )
        )
        return
    if target.is_symlink() and not target.exists():
        issues.append(
            ValidationIssue(
                "activation.broken_symlink",
                "Binding target root is a broken symlink.",
                Location("project", target.relative_to(project).as_posix()),
            )
        )
        return
    if target.exists() and not target.is_dir():
        issues.append(
            ValidationIssue(
                "activation.target_owned",
                "Binding target root is a project-owned non-directory.",
                Location("project", target.relative_to(project).as_posix()),
            )
        )
        return
    if target.is_dir():
        for entry in target.iterdir():
            if entry.is_symlink() and not entry.exists():
                issues.append(ValidationIssue("activation.broken_symlink", "Activation symlink target is missing.", Location("project", entry.relative_to(project).as_posix())))
    profile_name = binding.get("profile")
    if not profile_is_valid:
        return
    assert isinstance(profile_name, str)
    if profile_name not in profiles:
        issues.append(ValidationIssue("profile.missing", "Binding references a missing Profile.", Location("project", f"{binding_prefix}.profile")))
        return
    if profile_name in invalid_profiles:
        issues.append(
            ValidationIssue(
                "profile.invalid_selection",
                "Binding selects a Profile with invalid inheritance.",
                Location("project", f"{binding_prefix}.profile"),
            )
        )
        return
    skill_ids = {item["id"] for item in catalog if isinstance(item.get("id"), str)}
    selected = _resolve_profile(
        profile_name,
        profiles,
        groups,
        skill_ids,
        invalid_profiles=invalid_profiles,
    )
    for index, identity in enumerate(_names(binding.get("add"))):
        if identity not in skill_ids:
            issues.append(
                ValidationIssue(
                    "skill.missing",
                    f"Binding references missing Skill {identity!r}.",
                    Location("project", f"{binding_prefix}.add[{index}]"),
                )
            )
        else:
            selected.add(identity)
    for index, identity in enumerate(_names(binding.get("remove"))):
        if identity not in selected:
            issues.append(
                ValidationIssue(
                    "skill.remove_missing",
                    f"Binding removes absent Skill {identity!r}.",
                    Location("project", f"{binding_prefix}.remove[{index}]"),
                )
            )
        selected.discard(identity)
    by_id = {item.get("id"): item for item in catalog}
    for identity in selected:
        item = by_id.get(identity, {})
        name = item.get("name")
        if not isinstance(name, str):
            continue
        destination = target / name
        expected_path = item.get("path")
        expected = collection / expected_path if isinstance(expected_path, str) else None
        if destination.is_symlink():
            if destination.exists() and expected is not None and destination.resolve() == expected.resolve():
                continue
            if not destination.exists():
                continue
        if destination.exists() or destination.is_symlink():
            issues.append(ValidationIssue("activation.target_owned", "Activation would overwrite a project-owned target.", Location("project", destination.relative_to(project).as_posix())))


def _broken_target_component(project: Path, target_path: PurePath) -> Path | None:
    current = project
    for component in target_path.parts:
        current = current / component
        if current.is_symlink():
            try:
                current.resolve(strict=True)
            except (OSError, RuntimeError):
                return current
        if not current.exists():
            break
    return None
