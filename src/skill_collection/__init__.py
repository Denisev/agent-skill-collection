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
    ActivationResult,
    ActivationMode,
    ActivationRecord,
    ActivationReview,
    CleanupReport,
    CreateActivationStateDirectoryAction,
    FilesystemKind,
    FilesystemPrecondition,
    ManagedLink,
    ReviewStatus,
    WriteActivationRecordAction,
    prepare_activation,
    serialize_activation_record,
)
from ._activation_transaction import apply_activation

__all__ = [
    "ActivationPlan",
    "ActivationMode",
    "ActivationRecord",
    "ActivationReview",
    "ActivationResult",
    "CleanupReport",
    "CreateActivationStateDirectoryAction",
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
    "WriteActivationRecordAction",
    "ValidationIssue",
    "plan_activation",
    "apply_activation",
    "prepare_activation",
    "scan",
    "serialize_activation_record",
    "validate",
]
