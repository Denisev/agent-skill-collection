from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from typing import Any

from .inspection import DoctorReport, GuidedIssue, ProjectStatus, RecommendedCommand
from .initialization import InitializationPlan
from ._initialization_transaction import InitializationResult


def json_document(command: str, result: object) -> str:
    payload = {
        "command": command,
        "result": _value(result),
        "schema_version": 1,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def error_document(code: str, message: str, *, cleanup: object | None = None) -> str:
    payload = {"error": {"code": code, "message": message}, "schema_version": 1}
    if cleanup is not None:
        payload["cleanup"] = _value(cleanup)
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _value(value: object) -> Any:
    if is_dataclass(value):
        return _value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value


def inspection_text(result: ProjectStatus | DoctorReport | InitializationPlan | InitializationResult) -> str:
    if isinstance(result, InitializationResult):
        lines = _initialization_result_lines(result)
    elif isinstance(result, InitializationPlan):
        lines = _initialization_lines(result)
    else:
        lines = _doctor_lines(result) if isinstance(result, DoctorReport) else _status_lines(result)
    return "\n".join(lines) + "\n"


def _initialization_result_lines(result: InitializationResult) -> list[str]:
    location = (
        f"{result.binding_location.root}:{_escape(result.binding_location.relative_path)}"
        if result.binding_location is not None else "-"
    )
    lines = [
        f"Project initialization apply: {result.status}",
        f"Binding: {location}",
        f"Binding digest: {result.binding_digest or '-'}",
        f"Plan ID: {result.plan_id or '-'}",
        "",
        f"Issues ({len(result.issues)}):",
    ]
    if result.issues:
        for index, issue in enumerate(result.issues, 1):
            lines.extend([
                f"{index}. [{_escape(issue.code)}] {_escape(issue.message)}",
                f"   Location: {issue.location.root}:{_escape(issue.location.relative_path)}",
            ])
    else:
        lines.append("None.")
    lines.extend(["", "Cleanup:"])
    if result.cleanup is None:
        lines.append("None.")
    else:
        cleanup = result.cleanup
        lines.extend([
            f"Attempted: {'yes' if cleanup.attempted else 'no'}",
            f"Removed Binding: {'yes' if cleanup.removed_binding else 'no'}",
            f"Removed temporary files ({len(cleanup.removed_temporary_files)}):",
            *(f"- {item.root}:{_escape(item.relative_path)}" for item in cleanup.removed_temporary_files),
            f"Remaining objects ({len(cleanup.remaining_objects)}):",
            *(f"- {item.root}:{_escape(item.relative_path)}" for item in cleanup.remaining_objects),
            f"Cleanup issues ({len(cleanup.issues)}):",
            *(
                f"- [{_escape(issue.code)}] {_escape(issue.message)}"
                for issue in cleanup.issues
            ),
        ])
    return lines


def _initialization_lines(result: InitializationPlan) -> list[str]:
    lines = [
        f"Project initialization: {result.status}",
        f"Profile: {result.profile or '-'}",
        f"Collection revision: {result.collection_revision or '-'}",
        f"Collection URL: {result.collection_url or '-'}",
        "Binding: project:skill-collection.toml",
        f"Binding state: {result.binding_observation.kind}",
        f"Binding digest: {result.binding_digest or '-'}",
        f"Plan ID: {result.plan_id or '-'}",
        "",
        f"Proposed actions ({len(result.actions)}):",
    ]
    if result.actions:
        for index, action in enumerate(result.actions, 1):
            lines.extend([
                f"{index}. [{action.kind}] {action.location.root}:{_escape(action.location.relative_path)}",
                f"   Precondition: {action.precondition}",
                f"   Content SHA-256: {action.content_sha256}",
                f"   Action ID: {action.action_id}",
            ])
    else:
        lines.append("None.")
    lines.extend(["", "Binding content:"])
    if result.binding_content is None:
        lines.append("None.")
    else:
        lines.extend(f"  {line}" for line in result.binding_content.removesuffix("\n").split("\n"))
    lines.extend(["", f"Issues ({len(result.blocking_issues)}):"])
    if not result.blocking_issues:
        lines.append("None.")
    else:
        for index, issue in enumerate(result.blocking_issues, 1):
            lines.extend([
                f"{index}. [{_escape(issue.code)}] {_escape(issue.message)}",
                f"   Location: {issue.location.root}:{_escape(issue.location.relative_path)}",
            ])
            if issue.related_locations:
                related = ", ".join(
                    f"{item.root}:{_escape(item.relative_path)}"
                    for item in issue.related_locations
                )
                lines.append(f"   Related: {related}")
    return lines


def _status_lines(result: ProjectStatus, *, nested: bool = False) -> list[str]:
    prefix = "  " if nested else ""
    heading = "Status" if nested else "Project status"
    lines = [
        f"{prefix}{heading}: {result.category}",
        f"{prefix}Profile: {result.profile or '-'}",
        f"{prefix}Activation ID: {result.activation_id or '-'}",
        f"{prefix}Plan ID: {result.plan_id or '-'}",
        f"{prefix}Pending actions: {result.pending_action_count}",
        f"{prefix}Unchanged links: {result.unchanged_link_count}",
        "",
        f"{prefix}Issues ({len(result.issues)}):",
    ]
    lines.extend(_issue_lines(result.issues, prefix) if result.issues else [f"{prefix}None."])
    if not nested:
        lines.extend(["", f"Recommended next commands ({len(result.recommended_commands)}):"])
        lines.extend(_command_lines(result.recommended_commands) if result.recommended_commands else ["None."])
    return lines


def _doctor_lines(result: DoctorReport) -> list[str]:
    lines = [f"Doctor: {result.category}", "", f"Capabilities ({len(result.capabilities)}):"]
    for check in result.capabilities:
        lines.append(f"- {check.id}: {check.outcome} — {_escape(check.summary)}")
        if check.issue is not None:
            lines.extend(_issue_lines((check.issue,), "  "))
    lines.extend(["", "Project:", *_status_lines(result.status, nested=True), "", f"Recommended next commands ({len(result.recommended_commands)}):"])
    lines.extend(_command_lines(result.recommended_commands) if result.recommended_commands else ["None."])
    return lines


def _issue_lines(issues: tuple[GuidedIssue, ...], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for index, guided in enumerate(issues, 1):
        issue = guided.issue
        lines.extend([
            f"{prefix}{index}. [{_escape(issue.code)}] {_escape(issue.message)}",
            f"{prefix}   Location: {issue.location.root}:{_escape(issue.location.relative_path)}",
        ])
        if issue.related_locations:
            related = ", ".join(f"{item.root}:{_escape(item.relative_path)}" for item in issue.related_locations)
            lines.append(f"{prefix}   Related: {related}")
        lines.append(f"{prefix}   Guidance: {_escape(guided.guidance.text)}")
    return lines


def _command_lines(commands: tuple[RecommendedCommand, ...]) -> list[str]:
    lines: list[str] = []
    for index, command in enumerate(commands, 1):
        lines.extend([f"{index}. {command.command}", f"   {command.description}"])
    return lines


def _escape(value: str) -> str:
    return value.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
