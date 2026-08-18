from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Literal, TypeAlias

from ._issues import normalize_issues
from ._binding import canonical_digest, semantic_binding_payload, serialize_binding
from ._resolution import resolve_profile
from ._filesystem import FilesystemKind, classify_path
from .scanning import DiscoveredSkill, _scan_validated
from .validation import (
    Location,
    ValidationIssue,
    _CollectionValidationState,
    _validate_collection,
)


InitializationStatus: TypeAlias = Literal["ready", "blocked"]


@dataclass(frozen=True, slots=True)
class BindingDestinationObservation:
    location: Location
    kind: FilesystemKind


@dataclass(frozen=True, slots=True)
class CreateBindingAction:
    action_id: str
    kind: Literal["create-binding"]
    location: Location
    precondition: Literal["absent"]
    content_sha256: str


ProposedInitializationAction: TypeAlias = CreateBindingAction
FrozenValue: TypeAlias = str | int | bool | None | tuple["FrozenValue", ...]


@dataclass(frozen=True, slots=True)
class ValidatedCollectionDocuments:
    sources: tuple[FrozenValue, ...]
    skills: tuple[FrozenValue, ...]
    groups: tuple[FrozenValue, ...]
    profiles: tuple[FrozenValue, ...]
    collection_revision: str
    collection_url: str


@dataclass(frozen=True, slots=True)
class CollectionSelectionReview:
    status: Literal["ready", "blocked"]
    documents: ValidatedCollectionDocuments | None
    discoveries: tuple[DiscoveredSkill, ...]
    profile: str | None
    selected_skill_ids: tuple[str, ...]
    collection_revision: str | None
    collection_url: str | None
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class InitializationPlan:
    status: InitializationStatus
    plan_id: str | None
    profile: str | None
    collection_revision: str | None
    collection_url: str | None
    binding_location: Location
    binding_observation: BindingDestinationObservation
    binding_content: str | None
    binding_digest: str | None
    actions: tuple[ProposedInitializationAction, ...]
    blocking_issues: tuple[ValidationIssue, ...]


_PROFILE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BINDING_LOCATION = Location("project", "skill-collection.toml")


def plan_project_initialization(
    collection_root: str | Path,
    project_root: str | Path,
    profile: str,
) -> InitializationPlan:
    collection = Path(collection_root)
    project = Path(project_root)
    selection = _prepare_collection_selection(collection, profile)
    observation = BindingDestinationObservation(
        _BINDING_LOCATION, _classify_binding_destination(project)
    )
    issues = list(selection.issues)
    if not project.is_dir():
        issues.append(
            ValidationIssue(
                "root.missing",
                "Project root does not exist or is not a directory.",
                Location("project", "."),
            )
        )
    if observation.kind == "unreadable" and project.is_dir():
        issues.append(
            ValidationIssue(
                "initialization.binding_uninspectable",
                "Project Binding destination could not be safely inspected.",
                _BINDING_LOCATION,
            )
        )
    elif observation.kind != "absent" and project.is_dir():
        issues.append(
            ValidationIssue(
                "initialization.binding_exists",
                "Project Binding destination already exists.",
                _BINDING_LOCATION,
            )
        )
    elif project.is_dir() and not _binding_destination_is_contained(project):
        issues.append(
            ValidationIssue(
                "initialization.binding_outside_project",
                "Project Binding destination must remain inside the project root.",
                _BINDING_LOCATION,
            )
        )
    normalized = tuple(normalize_issues(issues))
    if normalized:
        return InitializationPlan(
            "blocked", None, None, None, None, _BINDING_LOCATION,
            observation, None, None, (), normalized,
        )
    assert selection.collection_revision is not None
    assert selection.collection_url is not None
    assert selection.profile is not None
    revision = selection.collection_revision
    collection_url = selection.collection_url
    binding = {
        "version": 1,
        "profile": profile,
        "target": ".agents/skills",
        "collection": {"url": collection_url, "revision": revision},
    }
    content = serialize_binding(binding)
    content_sha256 = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    binding_digest = canonical_digest(semantic_binding_payload(binding))
    action = CreateBindingAction(
        _action_id("create-binding", _BINDING_LOCATION),
        "create-binding",
        _BINDING_LOCATION,
        "absent",
        content_sha256,
    )
    plan_payload = {
        "initialization_plan_version": 1,
        "binding_digest": binding_digest,
        "binding_observation": {
            "kind": "absent",
            "location": _location_payload(_BINDING_LOCATION),
        },
        "collection_revision": revision,
        "profile": profile,
        "actions": [{
            "content_sha256": content_sha256,
            "kind": action.kind,
            "location": _location_payload(action.location),
            "precondition": action.precondition,
        }],
    }
    return InitializationPlan(
        "ready", canonical_digest(plan_payload), profile, revision, collection_url,
        _BINDING_LOCATION, observation, content, binding_digest, (action,), (),
    )


def _prepare_collection_selection(
    collection_root: str | Path,
    profile: str,
) -> CollectionSelectionReview:
    collection = Path(collection_root)
    state = _validate_collection(collection)
    scan_result = _scan_validated(collection, state)
    issues = list(scan_result.issues)
    profile_valid = isinstance(profile, str) and _PROFILE_NAME.fullmatch(profile) is not None
    if not profile_valid:
        issues.append(
            ValidationIssue(
                "field.invalid",
                "Field has an invalid type or value.",
                Location("collection", "profiles.toml#selection"),
            )
        )
    elif "profiles.toml" in state.documents and profile not in state.profile_map:
        issues.append(
            ValidationIssue(
                "profile.missing",
                "Selected Profile is missing.",
                Location("collection", "profiles.toml#selection"),
            )
        )
    catalog_document = state.documents.get("catalog.toml", {})
    collection_url = catalog_document.get("collection_url")
    if "catalog.toml" in state.documents and "collection_url" not in catalog_document:
        issues.append(
            ValidationIssue(
                "field.required",
                "Required field is missing.",
                Location("collection", "catalog.toml#collection_url"),
            )
        )
    normalized = tuple(normalize_issues(issues))
    if normalized:
        return CollectionSelectionReview(
            "blocked", None, scan_result.discovered, None, (), None, None, normalized
        )
    revision = catalog_document.get("collection_revision")
    assert isinstance(revision, str)
    assert isinstance(collection_url, str)
    assert profile_valid
    selected = resolve_profile(
        profile,
        state.profile_map,
        state.group_map,
        skill_ids=state.skill_ids,
        invalid_profiles=state.invalid_profiles,
    )
    documents = _frozen_documents(state, revision, collection_url)
    return CollectionSelectionReview(
        "ready",
        documents,
        scan_result.discovered,
        profile,
        tuple(sorted(selected)),
        revision,
        collection_url,
        (),
    )


def _frozen_documents(
    state: _CollectionValidationState,
    revision: str,
    collection_url: str,
) -> ValidatedCollectionDocuments:
    return ValidatedCollectionDocuments(
        tuple(_freeze(item) for item in state.sources),
        tuple(_freeze(item) for item in state.catalog),
        tuple(_freeze(item) for item in state.groups),
        tuple(_freeze(item) for item in state.profiles),
        revision,
        collection_url,
    )


def _freeze(value: object) -> FrozenValue:
    if isinstance(value, dict):
        return tuple(
            (str(key), _freeze(item)) for key, item in sorted(value.items())
        )  # type: ignore[return-value]
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _location_payload(location: Location) -> dict[str, str]:
    return {"root": location.root, "path": location.relative_path}


def _action_id(kind: str, location: Location) -> str:
    material = f"cli-schema-v1\0{kind}\0{location.root}\0{location.relative_path}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _classify_binding_destination(project: Path) -> FilesystemKind:
    if not project.is_dir():
        return "unreadable"
    return classify_path(project / "skill-collection.toml")


def _binding_destination_is_contained(project: Path) -> bool:
    try:
        resolved_project = project.resolve(strict=True)
        resolved_destination = (project / "skill-collection.toml").resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved_destination.is_relative_to(resolved_project)
