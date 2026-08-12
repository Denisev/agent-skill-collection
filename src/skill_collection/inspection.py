from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .activation import prepare_activation
from ._capabilities import containment_capability, directory_fsync_capability, regular_file_fsync_capability
from .validation import Location, ValidationIssue


StatusCategory = Literal["blocked", "inactive", "active", "drifted"]
DoctorCategory = Literal["ok", "attention", "blocked"]
CapabilityOutcome = Literal["supported", "unsupported", "not-inspected"]


@dataclass(frozen=True, slots=True)
class Guidance:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class GuidedIssue:
    issue: ValidationIssue
    guidance: Guidance


@dataclass(frozen=True, slots=True)
class RecommendedCommand:
    id: str
    command: str
    description: str


@dataclass(frozen=True, slots=True)
class ProjectStatus:
    category: StatusCategory
    profile: str | None
    activation_id: str | None
    plan_id: str | None
    pending_action_count: int
    unchanged_link_count: int
    issues: tuple[GuidedIssue, ...]
    recommended_commands: tuple[RecommendedCommand, ...]


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    id: Literal["safe-project-containment", "project-directory-fsync", "binding-file-fsync"]
    outcome: CapabilityOutcome
    summary: str
    issue: GuidedIssue | None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    category: DoctorCategory
    status: ProjectStatus
    capabilities: tuple[CapabilityCheck, ...]
    recommended_commands: tuple[RecommendedCommand, ...]


_GUIDANCE: tuple[tuple[frozenset[str], Guidance], ...] = (
    (frozenset({"root.missing"}), Guidance("inspect.root", "Provide existing collection and project directories, then inspect again.")),
    (frozenset({"document.missing", "toml.malformed", "field.required", "field.invalid", "field.unexpected", "field.duplicate"}), Guidance("inspect.document", "Correct the reported TOML document or field, then validate again.")),
    (frozenset({"source.duplicate_id", "source.invalid", "source.path_outside_collection", "source.path_symlink", "source.path_unavailable", "source.submodule_dirty", "source.submodule_invalid", "source.submodule_missing", "source.submodule_unpinned"}), Guidance("inspect.source", "Correct the reported Source state, then validate and scan again.")),
    (frozenset({"catalog.skill_not_discovered", "discovery.ambiguous_catalog", "discovery.uncataloged", "discovery.unreadable", "skill.duplicate_id", "skill.missing", "skill.name_collision", "skill.path_outside_source", "skill.remove_missing", "skill.source_missing"}), Guidance("inspect.catalog", "Correct Catalog or Skill discovery state, then validate and scan again.")),
    (frozenset({"group.cycle", "group.duplicate_name", "group.missing", "profile.duplicate_name", "profile.inheritance_cycle", "profile.invalid_selection", "profile.missing"}), Guidance("inspect.composition", "Correct the reported Group or Profile composition, then validate again.")),
    (frozenset({"binding.collection_revision_mismatch", "binding.target_outside_project"}), Guidance("inspect.binding", "Correct the project Binding so it selects the intended pinned collection state, then validate again.")),
    (frozenset({"activation.broken_symlink", "activation.owned_object_mismatch", "activation.record_exists", "activation.record_intent_mismatch", "activation.record_invalid", "activation.record_invalid_type", "activation.record_noncanonical", "activation.record_outside_project", "activation.record_path_owned", "activation.repair_unowned_directory", "activation.target_owned", "activation.unrecorded_object"}), Guidance("inspect.activation-ownership", "Review the reported project-owned or Activation-owned object; inspection will not change it.")),
    (frozenset({"activation.containment_unsupported"}), Guidance("inspect.platform-containment", "Use a platform that provides the no-follow and directory-relative operations required for safe Activation.")),
    (frozenset({"activation.directory_fsync_unsupported"}), Guidance("inspect.platform-directory-fsync", "Use a project filesystem that supports directory fsync before applying Activation.")),
    (frozenset({"activation.file_fsync_unsupported"}), Guidance("inspect.platform-file-fsync", "Use a project filesystem that supports regular-file fsync before applying Activation.")),
)
_UNKNOWN_GUIDANCE = Guidance("inspect.unclassified", "Review the reported issue, then run status again.")

_COMMANDS = {
    "validate": RecommendedCommand("validate", "skill-collection validate --collection-root <collection-root> --project-root <project-root>", "Validate collection and project documents."),
    "scan": RecommendedCommand("scan", "skill-collection scan --collection-root <collection-root>", "Inspect Skill discovery and Catalog correlation."),
    "review-activation": RecommendedCommand("review-activation", "skill-collection activate --collection-root <collection-root> --project-root <project-root>", "Review the current Activation without applying it."),
    "inspect-doctor": RecommendedCommand("inspect-doctor", "skill-collection doctor --collection-root <collection-root> --project-root <project-root>", "Inspect project state and platform capabilities."),
}


def _guide(issue: ValidationIssue) -> GuidedIssue:
    guidance = next((value for codes, value in _GUIDANCE if issue.code in codes), _UNKNOWN_GUIDANCE)
    return GuidedIssue(issue, guidance)


def status(collection_root: str | Path, project_root: str | Path) -> ProjectStatus:
    review = prepare_activation(collection_root, project_root)
    guided = tuple(_guide(issue) for issue in review.blocking_issues)
    if review.status == "blocked":
        ids = ["validate"]
        guidance_ids = {item.guidance.id for item in guided}
        if guidance_ids & {"inspect.source", "inspect.catalog"}:
            ids.append("scan")
        if any(value.startswith("inspect.platform-") for value in guidance_ids):
            ids.append("inspect-doctor")
        return ProjectStatus("blocked", None, None, None, 0, 0, guided, tuple(_COMMANDS[item] for item in ids))

    assert review.mode is not None and review.proposed_activation_record is not None
    category: StatusCategory = {"initial": "inactive", "repeat": "active", "repair": "drifted"}[review.mode]
    command = "inspect-doctor" if category == "active" else "review-activation"
    return ProjectStatus(category, review.proposed_activation_record.profile, review.activation_id, review.plan_id, len(review.actions), len(review.unchanged_links), (), (_COMMANDS[command],))


def doctor(collection_root: str | Path, project_root: str | Path) -> DoctorReport:
    project_status = status(collection_root, project_root)
    project = Path(project_root)
    definitions = (
        ("safe-project-containment", containment_capability(), "Required no-follow and directory-relative operations are available.", "Required no-follow or directory-relative operations are unavailable.", "activation.containment_unsupported", Location("project", ".")),
        ("project-directory-fsync", directory_fsync_capability(project), "The project filesystem supports directory fsync.", "The project filesystem does not support directory fsync.", "activation.directory_fsync_unsupported", Location("project", ".")),
        ("binding-file-fsync", regular_file_fsync_capability(project / "skill-collection.toml"), "The project filesystem supports regular-file fsync.", "The project filesystem does not support regular-file fsync.", "activation.file_fsync_unsupported", Location("project", "skill-collection.toml")),
    )
    checks: list[CapabilityCheck] = []
    for identity, probe, supported_text, unsupported_text, issue_code, location in definitions:
        if probe == "supported":
            checks.append(CapabilityCheck(identity, "supported", supported_text, None))  # type: ignore[arg-type]
        elif probe == "unsupported":
            issue = ValidationIssue(issue_code, unsupported_text, location)
            checks.append(CapabilityCheck(identity, "unsupported", unsupported_text, _guide(issue)))  # type: ignore[arg-type]
        else:
            summary = ("Directory fsync was not inspected because the project root could not be safely inspected." if identity == "project-directory-fsync" else "Regular-file fsync was not inspected because the Binding could not be safely inspected.")
            checks.append(CapabilityCheck(identity, "not-inspected", summary, None))  # type: ignore[arg-type]
    capabilities = tuple(checks)
    category: DoctorCategory = (
        "blocked" if project_status.category == "blocked" or any(item.outcome == "unsupported" for item in capabilities)
        else "attention" if any(item.outcome == "not-inspected" for item in capabilities)
        else "ok"
    )
    commands = tuple(item for item in project_status.recommended_commands if item.id != "inspect-doctor")
    return DoctorReport(category, project_status, capabilities, commands)
