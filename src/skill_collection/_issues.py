from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

T = TypeVar("T")


def normalize_issues(issues: Iterable[T]) -> list[T]:
    unique: list[T] = []
    for issue in issues:
        if issue not in unique:
            unique.append(issue)
    return sorted(unique, key=_issue_key)


def _issue_key(issue: Any) -> tuple[object, ...]:
    return (
        issue.code,
        issue.location.root,
        issue.location.relative_path,
        tuple(
            (location.root, location.relative_path)
            for location in issue.related_locations
        ),
        issue.message,
    )
