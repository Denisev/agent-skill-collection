from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePath
import tomllib
from typing import Literal

from ._issues import normalize_issues
from ._resolution import resolve_profile, strings
from .scanning import scan
from .validation import Location, ValidationIssue, validate


@dataclass(frozen=True, slots=True)
class CreateDirectoryAction:
    action_id: str
    kind: Literal["create-directory"]
    location: Location
    precondition: Literal["absent"]


@dataclass(frozen=True, slots=True)
class CreateSymlinkAction:
    action_id: str
    kind: Literal["create-symlink"]
    location: Location
    precondition: Literal["absent"]
    target: Location


ProposedAction = CreateDirectoryAction | CreateSymlinkAction


@dataclass(frozen=True, slots=True)
class ProposedManagedLink:
    location: Location
    target: Location


@dataclass(frozen=True, slots=True)
class ProposedActivationRecord:
    version: Literal[1]
    collection_revision: str
    binding: Location
    profile: str
    managed_links: tuple[ProposedManagedLink, ...]
    created_directories: tuple[Location, ...]


@dataclass(frozen=True, slots=True)
class ActivationPlan:
    status: Literal["ready", "blocked"]
    blocking_issues: tuple[ValidationIssue, ...]
    actions: tuple[ProposedAction, ...]
    unchanged_links: tuple[ProposedManagedLink, ...]
    proposed_activation_record: ProposedActivationRecord | None


def plan_activation(
    collection_root: str | Path, project_root: str | Path
) -> ActivationPlan:
    collection = Path(collection_root)
    project = Path(project_root)
    scan_result = scan(collection)
    issues = list(validate(collection, project)) + list(scan_result.issues)
    record_issue = _activation_record_issue(project)
    if record_issue is not None:
        issues.append(record_issue)
    issues = normalize_issues(issues)
    if issues:
        return ActivationPlan("blocked", tuple(issues), (), (), None)

    catalog_document = _read_toml(collection / "catalog.toml")
    groups_document = _read_toml(collection / "groups.toml")
    profiles_document = _read_toml(collection / "profiles.toml")
    binding = _read_toml(project / "skill-collection.toml")
    binding_collection = binding["collection"]
    assert isinstance(binding_collection, dict)
    if binding_collection["revision"] != catalog_document["collection_revision"]:
        issue = ValidationIssue(
            "binding.collection_revision_mismatch",
            "The Binding collection revision does not match the Catalog revision.",
            Location(
                "project", "skill-collection.toml#binding.collection.revision"
            ),
            (Location("collection", "catalog.toml#collection_revision"),),
        )
        return ActivationPlan("blocked", (issue,), (), (), None)
    catalog = _tables(catalog_document, "skills")
    groups = _by_name(_tables(groups_document, "groups"))
    profiles = _by_name(_tables(profiles_document, "profiles"))
    profile_name = str(binding["profile"])
    by_id = {item["id"]: item for item in catalog if isinstance(item.get("id"), str)}
    skill_ids = {str(identity) for identity in by_id}
    selected = resolve_profile(
        profile_name, profiles, groups, skill_ids=skill_ids
    )
    selected.update(strings(binding.get("add")))
    selected.difference_update(strings(binding.get("remove")))
    target_relative = str(binding.get("target", ".agents/skills"))
    target_root = project / PurePath(target_relative)

    directory_locations: list[Location] = []
    current = project
    for component in PurePath(target_relative).parts:
        current = current / component
        if not current.exists() and not current.is_symlink():
            directory_locations.append(
                Location("project", current.relative_to(project).as_posix())
            )

    managed_links: list[ProposedManagedLink] = []
    unchanged: list[ProposedManagedLink] = []
    pending_links: list[ProposedManagedLink] = []
    for identity in sorted(selected, key=lambda item: str(by_id[item]["name"])):
        item = by_id[identity]
        name = str(item["name"])
        source_path = str(item["path"])
        link = ProposedManagedLink(
            Location("project", (PurePath(target_relative) / name).as_posix()),
            Location("collection", source_path),
        )
        managed_links.append(link)
        destination = target_root / name
        expected = collection / source_path
        if destination.is_symlink() and destination.exists() and destination.resolve() == expected.resolve():
            unchanged.append(link)
        else:
            pending_links.append(link)

    directory_actions = tuple(
        CreateDirectoryAction(
            action_id=_action_id("create-directory", location),
            kind="create-directory",
            location=location,
            precondition="absent",
        )
        for location in directory_locations
    )
    link_actions = tuple(
        CreateSymlinkAction(
            action_id=_action_id("create-symlink", link.location),
            kind="create-symlink",
            location=link.location,
            precondition="absent",
            target=link.target,
        )
        for link in pending_links
    )
    created_directories = tuple(action.location for action in directory_actions)
    record = ProposedActivationRecord(
        version=1,
        collection_revision=str(catalog_document["collection_revision"]),
        binding=Location("project", "skill-collection.toml"),
        profile=profile_name,
        managed_links=tuple(managed_links),
        created_directories=created_directories,
    )
    return ActivationPlan(
        "ready",
        (),
        directory_actions + link_actions,
        tuple(unchanged),
        record,
    )


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _tables(document: dict[str, object], key: str) -> list[dict[str, object]]:
    value = document[key]
    assert isinstance(value, list)
    return [item for item in value if isinstance(item, dict)]


def _by_name(items: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["name"]): item for item in items}


def _action_id(kind: str, location: Location) -> str:
    material = f"cli-schema-v1\0{kind}\0{location.root}\0{location.relative_path}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _activation_record_issue(project: Path) -> ValidationIssue | None:
    project_resolved = project.resolve()
    relative_parent = PurePath(".agent-skill-collection")
    current = project
    for component in relative_parent.parts:
        current = current / component
        if current.is_symlink():
            try:
                resolved = current.resolve(strict=True)
            except (OSError, RuntimeError):
                return ValidationIssue(
                    "activation.broken_symlink",
                    "Activation Record path contains a broken or looping symlink.",
                    Location("project", current.relative_to(project).as_posix()),
                )
            if not resolved.is_relative_to(project_resolved):
                return ValidationIssue(
                    "activation.record_outside_project",
                    "Activation Record path resolves outside the project root.",
                    Location("project", current.relative_to(project).as_posix()),
                )
            if not resolved.is_dir():
                return ValidationIssue(
                    "activation.record_path_owned",
                    "Activation Record path contains a non-directory component.",
                    Location("project", current.relative_to(project).as_posix()),
                )
        elif current.exists():
            if not current.is_dir():
                return ValidationIssue(
                    "activation.record_path_owned",
                    "Activation Record path contains a non-directory component.",
                    Location("project", current.relative_to(project).as_posix()),
                )
        else:
            return None

    record_path = current / "activation.toml"
    if record_path.is_symlink() or record_path.exists():
        return ValidationIssue(
            "activation.record_exists",
            "An Activation Record already exists and cannot be interpreted yet.",
            Location("project", ".agent-skill-collection/activation.toml"),
        )
    return None
