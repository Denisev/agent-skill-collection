from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tomllib
from typing import Literal

from ._issues import normalize_issues
from .planning import ProposedAction, _activation_record_issue, _plan_activation
from .validation import Location, ValidationIssue


ActivationMode = Literal["initial", "repeat", "repair"]
ReviewStatus = Literal["ready", "blocked"]
FilesystemKind = Literal[
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


@dataclass(frozen=True, slots=True)
class ManagedLink:
    location: Location
    target: Location


@dataclass(frozen=True, slots=True)
class FilesystemPrecondition:
    location: Location
    kind: FilesystemKind
    link_text: str | None
    resolved_location: Location | None
    content_sha256: str | None
    readable: bool
    writable: bool
    searchable: bool


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    version: Literal[1]
    activation_id: str
    applied_plan_id: str
    binding: Location
    binding_digest: str
    collection_revision: str
    profile: str
    managed_links: tuple[ManagedLink, ...]
    created_directories: tuple[Location, ...]


@dataclass(frozen=True, slots=True)
class ActivationReview:
    status: ReviewStatus
    mode: ActivationMode | None
    activation_id: str | None
    plan_id: str | None
    actions: tuple[ProposedAction, ...]
    unchanged_links: tuple[ManagedLink, ...]
    filesystem_preconditions: tuple[FilesystemPrecondition, ...]
    proposed_activation_record: ActivationRecord | None
    blocking_issues: tuple[ValidationIssue, ...]


def prepare_activation(
    collection_root: str | Path, project_root: str | Path
) -> ActivationReview:
    collection = Path(collection_root)
    project = Path(project_root)
    record_path = project / ".agent-skill-collection" / "activation.toml"
    path_issue = _activation_record_issue(project)
    if path_issue is not None and path_issue.code != "activation.record_exists":
        return _blocked_review((path_issue,))
    record_exists = path_issue is not None
    plan = _plan_activation(
        collection, project, allow_activation_record=record_exists
    )
    if plan.status == "blocked":
        return _blocked_review(plan.blocking_issues)

    if not record_exists and plan.unchanged_links:
        issues = tuple(
            ValidationIssue(
                "activation.unrecorded_object",
                "An existing object has no Activation Record ownership proof.",
                link.location,
            )
            for link in plan.unchanged_links
        )
        return _blocked_review(issues)

    binding = _read_toml(project / "skill-collection.toml")
    binding_digest = _digest(_binding_payload(binding))
    planned_managed_links = tuple(
        sorted(
            (
                ManagedLink(link.location, link.target)
                for link in plan.proposed_activation_record.managed_links
            ),
            key=_managed_link_key,
        )
    )
    planned_created_directories = tuple(
        sorted(
            plan.proposed_activation_record.created_directories,
            key=lambda location: (
                len(Path(location.relative_path).parts),
                location.root,
                location.relative_path,
            ),
        )
    )
    existing_record: ActivationRecord | None = None
    if record_exists:
        existing_record, record_issues = _load_activation_record(record_path)
        if record_issues:
            return _blocked_review(record_issues)
        assert existing_record is not None
        managed_links = existing_record.managed_links
        created_directories = existing_record.created_directories
    else:
        managed_links = planned_managed_links
        created_directories = planned_created_directories

    activation_payload = {
        "activation_identity_version": 1,
        "record_version": 1,
        "collection_revision": plan.proposed_activation_record.collection_revision,
        "binding": {
            "root": "project",
            "path": "skill-collection.toml",
            "digest": binding_digest,
        },
        "profile": plan.proposed_activation_record.profile,
        "managed_links": [
            {
                "type": "symlink",
                "location": _location_payload(link.location),
                "target": _location_payload(link.target),
            }
            for link in managed_links
        ],
        "created_directories": [
            {"type": "directory", "location": _location_payload(location)}
            for location in created_directories
        ],
    }
    activation_id = _digest(activation_payload)
    if existing_record is not None:
        ownership_issues = _record_ownership_issues(
            existing_record,
            activation_id,
            binding_digest,
            plan.proposed_activation_record.collection_revision,
            plan.proposed_activation_record.profile,
            planned_managed_links,
            collection,
            project,
        )
        if ownership_issues:
            return _blocked_review(ownership_issues)
        owned_directories = set(existing_record.created_directories)
        unowned_directory_issues = tuple(
            ValidationIssue(
                "activation.repair_unowned_directory",
                "Repair would require creating a directory not owned by the Activation Record.",
                action.location,
            )
            for action in plan.actions
            if action.kind == "create-directory"
            and action.location not in owned_directories
        )
        if unowned_directory_issues:
            return _blocked_review(unowned_directory_issues)
    preconditions = _initial_preconditions(
        collection, project, plan.actions, managed_links, created_directories
    )
    unchanged = tuple(
        link
        for link in managed_links
        if _managed_link_matches(link, collection, project)
    )
    mode: ActivationMode = (
        "initial" if existing_record is None else ("repair" if plan.actions else "repeat")
    )
    plan_payload = {
        "activation_review_version": 1,
        "activation_id": activation_id,
        "mode": mode,
        "actions": [_action_payload(action) for action in plan.actions],
        "unchanged_links": [
            {
                "location": _location_payload(link.location),
                "target": _location_payload(link.target),
            }
            for link in unchanged
        ],
        "filesystem_preconditions": [
            _precondition_payload(precondition) for precondition in preconditions
        ],
    }
    plan_id = _digest(plan_payload)
    record = existing_record or ActivationRecord(
            version=1,
            activation_id=activation_id,
            applied_plan_id=plan_id,
            binding=Location("project", "skill-collection.toml"),
            binding_digest=binding_digest,
            collection_revision=plan.proposed_activation_record.collection_revision,
            profile=plan.proposed_activation_record.profile,
            managed_links=managed_links,
            created_directories=created_directories,
        )
    return ActivationReview(
        "ready",
        mode,
        activation_id,
        plan_id,
        plan.actions,
        unchanged,
        preconditions,
        record,
        (),
    )


def _blocked_review(issues: tuple[ValidationIssue, ...]) -> ActivationReview:
    return ActivationReview(
        "blocked", None, None, None, (), (), (), None, issues
    )


def serialize_activation_record(record: ActivationRecord) -> bytes:
    _validate_activation_record(record)
    return _serialize_valid_activation_record(record)


def _serialize_valid_activation_record(record: ActivationRecord) -> bytes:
    created_directories = tuple(
        sorted(
            record.created_directories,
            key=lambda location: (
                len(Path(location.relative_path).parts),
                location.root,
                location.relative_path,
            ),
        )
    )
    managed_links = tuple(sorted(record.managed_links, key=_managed_link_key))
    values = [
        record.activation_id,
        record.applied_plan_id,
        record.binding_digest,
        record.collection_revision,
        record.profile,
        record.binding.root,
        record.binding.relative_path,
        *(location.relative_path for location in created_directories),
        *(
            value
            for link in managed_links
            for value in (
                link.location.root,
                link.location.relative_path,
                link.target.root,
                link.target.relative_path,
            )
        ),
    ]
    if any(_contains_surrogate(value) for value in values):
        raise ValueError("Activation Record strings cannot contain Unicode surrogates.")

    lines = [
        f"version = {record.version}",
        f"activation_id = {_toml_string(record.activation_id)}",
        f"applied_plan_id = {_toml_string(record.applied_plan_id)}",
        f"binding_digest = {_toml_string(record.binding_digest)}",
        f"collection_revision = {_toml_string(record.collection_revision)}",
        f"profile = {_toml_string(record.profile)}",
        "created_directories = ["
        + ", ".join(
            _toml_string(location.relative_path)
            for location in created_directories
        )
        + "]",
    ]
    if not managed_links:
        lines.append("managed_links = []")
    lines.extend(
        (
            "[binding]",
            f"root = {_toml_string(record.binding.root)}",
            f"path = {_toml_string(record.binding.relative_path)}",
        )
    )
    for link in managed_links:
        lines.extend(
            (
                "",
                "[[managed_links]]",
                f"location_root = {_toml_string(link.location.root)}",
                f"location_path = {_toml_string(link.location.relative_path)}",
                f"target_root = {_toml_string(link.target.root)}",
                f"target_path = {_toml_string(link.target.relative_path)}",
            )
        )
    rendered = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        tomllib.loads(rendered.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Activation Record strings must round-trip as TOML.") from error
    return rendered


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _validate_activation_record(record: ActivationRecord) -> None:
    if not isinstance(record, ActivationRecord):
        raise ValueError("invalid Activation Record value")
    if type(record.version) is not int or record.version != 1:
        raise ValueError("unsupported Activation Record version")
    if any(
        not isinstance(value, str)
        for value in (
            record.activation_id,
            record.applied_plan_id,
            record.binding_digest,
            record.collection_revision,
            record.profile,
        )
    ):
        raise ValueError("invalid Activation Record string field")
    if not isinstance(record.binding, Location):
        raise ValueError("invalid Binding location")
    if not isinstance(record.managed_links, tuple) or not all(
        isinstance(link, ManagedLink) for link in record.managed_links
    ):
        raise ValueError("invalid managed links")
    if not isinstance(record.created_directories, tuple) or not all(
        isinstance(location, Location) for location in record.created_directories
    ):
        raise ValueError("invalid created directories")
    digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    if any(
        digest_pattern.fullmatch(value) is None
        for value in (
            record.activation_id,
            record.applied_plan_id,
            record.binding_digest,
        )
    ):
        raise ValueError("invalid Activation Record digest")
    if re.fullmatch(r"[0-9a-f]{40}", record.collection_revision) is None:
        raise ValueError("invalid collection revision")
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record.profile) is None:
        raise ValueError("invalid profile")
    if record.binding != Location("project", "skill-collection.toml"):
        raise ValueError("invalid Binding location")

    for link in record.managed_links:
        if link.location.root != "project" or link.target.root != "collection":
            raise ValueError("invalid managed-link roots")
        _safe_relative_string(link.location.relative_path)
        _safe_relative_string(link.target.relative_path)
    for location in record.created_directories:
        if location.root != "project":
            raise ValueError("invalid created-directory root")
        _safe_relative_string(location.relative_path)

    if len(set(record.managed_links)) != len(record.managed_links):
        raise ValueError("duplicate managed link")
    if len({link.location for link in record.managed_links}) != len(record.managed_links):
        raise ValueError("duplicate managed-link destination")
    if len(set(record.created_directories)) != len(record.created_directories):
        raise ValueError("duplicate created directory")
    for location in record.created_directories:
        if not any(
            location.relative_path
            in {
                parent.as_posix()
                for parent in Path(link.location.relative_path).parents
                if parent.as_posix() != "."
            }
            for link in record.managed_links
        ):
            raise ValueError("created directory is not a managed-link parent")


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def _binding_payload(binding: dict[str, object]) -> dict[str, object]:
    collection = binding["collection"]
    assert isinstance(collection, dict)
    return {
        "binding_schema_version": 1,
        "location": {"root": "project", "path": "skill-collection.toml"},
        "document": {
            "version": 1,
            "collection": {
                "url": collection["url"],
                "revision": collection["revision"],
            },
            "profile": binding["profile"],
            "target": binding.get("target", ".agents/skills"),
            "add": sorted(binding.get("add", [])),
            "remove": sorted(binding.get("remove", [])),
        },
    }


def _location_payload(location: Location) -> dict[str, str]:
    return {"root": location.root, "path": location.relative_path}


def _managed_link_key(link: ManagedLink) -> tuple[str, ...]:
    return (
        link.location.root,
        link.location.relative_path,
        link.target.root,
        link.target.relative_path,
    )


def _action_payload(action: ProposedAction) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": action.kind,
        "location": _location_payload(action.location),
        "precondition": action.precondition,
    }
    target = getattr(action, "target", None)
    if target is not None:
        payload["target"] = _location_payload(target)
    return payload


def _initial_preconditions(
    collection: Path,
    project: Path,
    actions: tuple[ProposedAction, ...],
    links: tuple[ManagedLink, ...],
    created_directories: tuple[Location, ...],
) -> tuple[FilesystemPrecondition, ...]:
    locations = {
        Location("project", "skill-collection.toml"),
        Location("project", ".agent-skill-collection"),
        Location("project", ".agent-skill-collection/activation.toml"),
        *(action.location for action in actions),
        *created_directories,
        *(link.location for link in links),
        *(
            Location("project", parent.as_posix())
            for link in links
            for parent in Path(link.location.relative_path).parents
            if parent.as_posix() != "."
        ),
        *(link.target for link in links),
        *(
            Location("collection", (Path(link.target.relative_path) / "SKILL.md").as_posix())
            for link in links
        ),
    }
    return tuple(
        _inspect_location(location, collection, project)
        for location in sorted(
            locations, key=lambda item: (item.root, item.relative_path)
        )
    )


def _inspect_location(
    location: Location, collection: Path, project: Path
) -> FilesystemPrecondition:
    root = collection if location.root == "collection" else project
    path = root / location.relative_path
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return FilesystemPrecondition(
            location, "absent", None, None, None, False, False, False
        )
    except OSError:
        return FilesystemPrecondition(
            location, "unreadable", None, None, None, False, False, False
        )

    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        try:
            link_text = os.readlink(path)
        except OSError:
            return FilesystemPrecondition(
                location, "unreadable", None, None, None, False, False, False
            )
        try:
            resolved = path.resolve(strict=True)
        except RuntimeError:
            return FilesystemPrecondition(
                location, "looping-symlink", link_text, None, None, False, False, False
            )
        except OSError as error:
            kind: FilesystemKind = (
                "looping-symlink" if error.errno == errno.ELOOP else "broken-symlink"
            )
            return FilesystemPrecondition(
                location, kind, link_text, None, None, False, False, False
            )
        resolved_location = _rooted_resolved_location(resolved, collection, project)
        if resolved_location is None:
            return FilesystemPrecondition(
                location, "unreadable", link_text, None, None, False, False, False
            )
        return FilesystemPrecondition(
            location,
            "symlink",
            link_text,
            resolved_location,
            None,
            os.access(resolved, os.R_OK),
            os.access(resolved, os.W_OK),
            resolved.is_dir() and os.access(resolved, os.X_OK),
        )

    readable = os.access(path, os.R_OK)
    writable = os.access(path, os.W_OK)
    if stat.S_ISDIR(mode):
        return FilesystemPrecondition(
            location, "directory", None, None, None, readable, writable, os.access(path, os.X_OK)
        )
    if stat.S_ISREG(mode):
        try:
            content_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return FilesystemPrecondition(
                location, "unreadable", None, None, None, False, False, False
            )
        return FilesystemPrecondition(
            location, "regular-file", None, None, content_digest, readable, writable, False
        )
    if stat.S_ISFIFO(mode):
        kind: FilesystemKind = "fifo"
    elif stat.S_ISSOCK(mode):
        kind = "socket"
    elif stat.S_ISBLK(mode):
        kind = "block-device"
    else:
        kind = "character-device"
    return FilesystemPrecondition(
        location, kind, None, None, None, readable, writable, False
    )


def _rooted_resolved_location(
    path: Path, collection: Path, project: Path
) -> Location | None:
    for root_name, root in (("collection", collection), ("project", project)):
        try:
            relative = path.relative_to(root.resolve())
        except ValueError:
            continue
        return Location(root_name, relative.as_posix())
    return None


def _precondition_payload(value: FilesystemPrecondition) -> dict[str, object]:
    return {
        "location": _location_payload(value.location),
        "kind": value.kind,
        "link_text": value.link_text,
        "resolved_location": (
            None
            if value.resolved_location is None
            else _location_payload(value.resolved_location)
        ),
        "content_sha256": value.content_sha256,
        "readable": value.readable,
        "writable": value.writable,
        "searchable": value.searchable,
    }


def _load_activation_record(
    path: Path,
) -> tuple[ActivationRecord | None, tuple[ValidationIssue, ...]]:
    location = Location("project", ".agent-skill-collection/activation.toml")
    if path.is_symlink() or not path.is_file():
        return None, (
            ValidationIssue(
                "activation.record_invalid_type",
                "The Activation Record must be an ordinary file.",
                location,
            ),
        )
    try:
        raw = path.read_bytes()
        document = tomllib.loads(raw.decode("utf-8"))
        record = _record_from_document(document)
        canonical = _serialize_valid_activation_record(record)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError):
        return None, (
            ValidationIssue(
                "activation.record_invalid",
                "The Activation Record is malformed or unsupported.",
                location,
            ),
        )
    if canonical != raw:
        return None, (
            ValidationIssue(
                "activation.record_noncanonical",
                "The Activation Record is not byte-for-byte canonical.",
                location,
            ),
        )
    return record, ()


def _record_from_document(document: dict[str, object]) -> ActivationRecord:
    expected = {
        "version",
        "activation_id",
        "applied_plan_id",
        "binding_digest",
        "collection_revision",
        "profile",
        "created_directories",
        "binding",
        "managed_links",
    }
    if set(document) != expected:
        raise ValueError("invalid Activation Record shape")
    binding = document["binding"]
    links = document["managed_links"]
    directories = document["created_directories"]
    if not isinstance(binding, dict) or set(binding) != {"root", "path"}:
        raise ValueError("invalid Binding location")
    if not isinstance(links, list) or not isinstance(directories, list):
        raise ValueError("invalid record collections")
    managed: list[ManagedLink] = []
    for item in links:
        if not isinstance(item, dict) or set(item) != {
            "location_root", "location_path", "target_root", "target_path"
        }:
            raise ValueError("invalid managed link")
        managed.append(
            ManagedLink(
                Location(item["location_root"], item["location_path"]),  # type: ignore[arg-type]
                Location(item["target_root"], item["target_path"]),  # type: ignore[arg-type]
            )
        )
    record = ActivationRecord(
        document["version"],  # type: ignore[arg-type]
        document["activation_id"],  # type: ignore[arg-type]
        document["applied_plan_id"],  # type: ignore[arg-type]
        Location(binding["root"], binding["path"]),  # type: ignore[arg-type]
        document["binding_digest"],  # type: ignore[arg-type]
        document["collection_revision"],  # type: ignore[arg-type]
        document["profile"],  # type: ignore[arg-type]
        tuple(managed),
        tuple(
            Location("project", item)  # type: ignore[arg-type]
            for item in directories
        ),
    )
    _validate_activation_record(record)
    return record


def _safe_relative_string(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or _contains_surrogate(value)
    ):
        raise ValueError("invalid relative path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != value:
        raise ValueError("invalid relative path")
    return value


def _record_ownership_issues(
    record: ActivationRecord,
    activation_id: str,
    binding_digest: str,
    collection_revision: str,
    profile: str,
    planned_links: tuple[ManagedLink, ...],
    collection: Path,
    project: Path,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if (
        record.activation_id != activation_id
        or record.binding_digest != binding_digest
        or record.collection_revision != collection_revision
        or record.profile != profile
        or record.managed_links != planned_links
    ):
        issues.append(
            ValidationIssue(
                "activation.record_intent_mismatch",
                "The Activation Record does not match current activation intent.",
                Location("project", ".agent-skill-collection/activation.toml"),
            )
        )
    for location in record.created_directories:
        state = _inspect_location(location, collection, project)
        if state.kind not in ("directory", "absent"):
            issues.append(
                ValidationIssue(
                    "activation.owned_object_mismatch",
                    "A record-owned directory has a semantic mismatch.",
                    location,
                )
            )
    for link in record.managed_links:
        state = _inspect_location(link.location, collection, project)
        if state.kind == "absent" or _managed_link_matches(link, collection, project):
            continue
        issues.append(
            ValidationIssue(
                "activation.owned_object_mismatch",
                "A record-owned link has a semantic mismatch.",
                link.location,
                (link.target,),
            )
        )
    return tuple(normalize_issues(issues))


def _managed_link_matches(link: ManagedLink, collection: Path, project: Path) -> bool:
    destination = project / link.location.relative_path
    expected = collection / link.target.relative_path
    if not destination.is_symlink():
        return False
    try:
        return destination.resolve(strict=True) == expected.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
