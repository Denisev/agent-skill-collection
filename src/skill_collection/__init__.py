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

__all__ = [
    "ActivationPlan",
    "CreateDirectoryAction",
    "CreateSymlinkAction",
    "DiscoveredSkill",
    "Location",
    "ProposedActivationRecord",
    "ProposedManagedLink",
    "ScanResult",
    "ValidationIssue",
    "plan_activation",
    "scan",
    "validate",
]
