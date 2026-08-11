from __future__ import annotations

from collections.abc import Callable


def strings(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def resolve_group(
    name: str,
    groups: dict[str, dict[str, object]],
    resolving: frozenset[str] = frozenset(),
) -> set[str]:
    if name not in groups or name in resolving:
        return set()
    group = groups[name]
    next_resolving = resolving | {name}
    result = strings(group.get("skills"))
    for nested in strings(group.get("groups")):
        result.update(resolve_group(nested, groups, next_resolving))
    return result


def resolve_profile(
    name: str,
    profiles: dict[str, dict[str, object]],
    groups: dict[str, dict[str, object]],
    *,
    skill_ids: set[str] | None = None,
    invalid_profiles: set[str] | None = None,
    resolving: frozenset[str] = frozenset(),
    on_missing_removal: Callable[[str, str], None] | None = None,
) -> set[str]:
    invalid = set() if invalid_profiles is None else invalid_profiles
    if name in invalid or name not in profiles or name in resolving:
        return set()
    profile = profiles[name]
    next_resolving = resolving | {name}
    result: set[str] = set()
    for parent in strings(profile.get("inherits")):
        result.update(
            resolve_profile(
                parent,
                profiles,
                groups,
                skill_ids=skill_ids,
                invalid_profiles=invalid,
                resolving=next_resolving,
            )
        )
    for group in strings(profile.get("groups")):
        result.update(resolve_group(group, groups))
    result.update(strings(profile.get("skills")))
    result.update(strings(profile.get("add")))
    for skill in strings(profile.get("remove")):
        if skill not in result and on_missing_removal is not None:
            on_missing_removal(name, skill)
        result.discard(skill)
    return result if skill_ids is None else result & skill_ids
