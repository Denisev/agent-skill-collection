"""Read-only validation seam for an agent skill collection."""

from .validation import Location, ValidationIssue, validate
from .scanning import DiscoveredSkill, ScanResult, scan
from .planning import (
    ActivationPlan,
    CreateDirectoryAction,
    CreateSymlinkAction,
    ProposedActivationRecord,
    ProposedManagedLink,
    plan_activation,
)
from .activation import (
    ActivationMode,
    ActivationRecord,
    ActivationReview,
    FilesystemKind,
    FilesystemPrecondition,
    ManagedLink,
    ReviewStatus,
    prepare_activation,
    serialize_activation_record,
)

__all__ = [
    "ActivationPlan",
    "ActivationMode",
    "ActivationRecord",
    "ActivationReview",
    "CreateDirectoryAction",
    "CreateSymlinkAction",
    "DiscoveredSkill",
    "Location",
    "FilesystemPrecondition",
    "FilesystemKind",
    "ManagedLink",
    "ProposedActivationRecord",
    "ProposedManagedLink",
    "ScanResult",
    "ReviewStatus",
    "ValidationIssue",
    "plan_activation",
    "prepare_activation",
    "scan",
    "serialize_activation_record",
    "validate",
]
