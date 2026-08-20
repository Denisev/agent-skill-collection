from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .output import error_document, inspection_text, json_document
from .planning import plan_activation
from .activation import prepare_activation
from ._activation_transaction import apply_activation
from .scanning import scan
from .validation import validate
from .inspection import doctor, status
from .initialization import plan_project_initialization
from ._initialization_transaction import apply_project_initialization
from .source_update import (
    NetworkAuthorization,
    RemoteCandidateRequest,
    _RemoteCleanupIncomplete,
    inspect_remote_candidates,
)


class _UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise _UsageError(self, message)


def main(
    argv: Sequence[str], *, stdout: TextIO, stderr: TextIO
) -> int:
    parser = _build_parser()
    try:
        values = list(argv)
        _reject_duplicate_source_inspection_options(values, parser)
        arguments = parser.parse_args(values)
        collection = _absolute(arguments.collection_root)
        if arguments.command == "scan":
            result = scan(collection)
            exit_code = 1 if result.issues else 0
        elif arguments.command == "validate":
            project = (
                _absolute(arguments.project_root)
                if arguments.project_root is not None
                else None
            )
            issues = tuple(validate(collection, project))
            result = {"issues": issues}
            exit_code = 1 if issues else 0
        elif arguments.command == "plan":
            project = _absolute(arguments.project_root)
            result = plan_activation(collection, project)
            exit_code = 1 if result.status == "blocked" else 0
        elif arguments.command == "status":
            project = _absolute(arguments.project_root)
            result = status(collection, project)
            exit_code = 0 if result.category == "active" else 1
        elif arguments.command == "doctor":
            project = _absolute(arguments.project_root)
            result = doctor(collection, project)
            exit_code = 0 if result.category == "ok" else 1
        elif arguments.command == "init-project":
            project = _absolute(arguments.project_root)
            if arguments.apply and arguments.plan_id is None:
                raise _UsageError(parser, "--plan-id is required with --apply")
            if not arguments.apply and arguments.plan_id is not None:
                raise _UsageError(parser, "--plan-id requires --apply")
            result = (
                apply_project_initialization(
                    collection, project, arguments.profile, arguments.plan_id
                )
                if arguments.apply
                else plan_project_initialization(collection, project, arguments.profile)
            )
            exit_code = 1 if result.status in (
                "blocked", "failed", "created_with_incomplete_cleanup",
            ) else 0
        elif arguments.command == "inspect-source-candidates":
            if not arguments.allow_network:
                raise _UsageError(parser, "--allow-network is required")
            requests: list[RemoteCandidateRequest] = []
            for value in arguments.source:
                source_id, separator, remote_ref = value.partition("=")
                if not separator or not source_id or not remote_ref:
                    raise _UsageError(
                        parser, "--source must use SOURCE=refs/heads/BRANCH"
                    )
                try:
                    requests.append(RemoteCandidateRequest(source_id, remote_ref))
                except ValueError as error:
                    raise _UsageError(
                        parser, "--source must use SOURCE=refs/heads/BRANCH"
                    ) from error
            result = inspect_remote_candidates(
                collection,
                tuple(requests),
                NetworkAuthorization("anonymous-https-remote-inspection"),
            )
            exit_code = 0 if result.status == "ready" else 1
        else:
            project = _absolute(arguments.project_root)
            if arguments.apply and arguments.plan_id is None:
                raise _UsageError(parser, "--plan-id is required with --apply")
            if not arguments.apply and arguments.plan_id is not None:
                raise _UsageError(parser, "--plan-id requires --apply")
            result = (
                apply_activation(collection, project, arguments.plan_id)
                if arguments.apply
                else prepare_activation(collection, project)
            )
            exit_code = 1 if result.status in ("blocked", "failed") else 0
        output_format = getattr(arguments, "format", "json")
        stdout.write(json_document(arguments.command, result) if output_format == "json" else inspection_text(result))
        return exit_code
    except _UsageError as error:
        error.parser.print_usage(file=stderr)
        stderr.write(f"{error.parser.prog}: error: {error.message}\n")
        return 2
    except KeyboardInterrupt as error:
        initialization_cleanup = getattr(error, "initialization_cleanup_report", None)
        cleanup = initialization_cleanup or getattr(error, "activation_cleanup_report", None)
        if cleanup is not None and (cleanup.remaining_objects or cleanup.issues):
            stderr.write(
                error_document(
                    "system.interrupted",
                    (
                        "Project initialization was interrupted."
                        if initialization_cleanup is not None
                        else "Activation was interrupted."
                    ),
                    cleanup=cleanup,
                )
            )
        return 130
    except Exception as error:
        remote_cleanup_incomplete = isinstance(error, _RemoteCleanupIncomplete)
        cleanup = getattr(error, "initialization_cleanup_report", None) or getattr(error, "activation_cleanup_report", None)
        stderr.write(
            error_document(
                "system.cleanup_failed" if remote_cleanup_incomplete else "system.unexpected",
                (
                    "Remote inspection cleanup could not be confirmed."
                    if remote_cleanup_incomplete
                    else "An unexpected system failure occurred."
                ),
                cleanup=(
                    cleanup
                    if cleanup is not None
                    and (cleanup.remaining_objects or cleanup.issues)
                    else None
                ),
            )
        )
        return 3


def _absolute(value: str) -> Path:
    return Path(value).absolute()


def _reject_duplicate_source_inspection_options(
    argv: list[str], parser: argparse.ArgumentParser
) -> None:
    if not argv or argv[0] != "inspect-source-candidates":
        return
    for option in ("--collection-root", "--format", "--allow-network"):
        count = sum(
            value == option or value.startswith(f"{option}=")
            for value in argv[1:]
        )
        if count > 1:
            raise _UsageError(parser, f"{option} may be specified only once")


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="skill-collection")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    scan_parser = subparsers.add_parser("scan")
    _collection_flag(scan_parser)

    validate_parser = subparsers.add_parser("validate")
    _collection_flag(validate_parser)
    validate_parser.add_argument("--project-root")

    plan_parser = subparsers.add_parser("plan")
    _collection_flag(plan_parser)
    plan_parser.add_argument("--project-root", required=True)
    source_update_parser = subparsers.add_parser("inspect-source-candidates")
    _collection_flag(source_update_parser)
    source_update_parser.add_argument("--source", action="append", required=True)
    source_update_parser.add_argument("--allow-network", action="store_true")
    source_update_parser.add_argument(
        "--format", choices=("json", "text"), default="json"
    )
    initialization_parser = subparsers.add_parser("init-project")
    _collection_flag(initialization_parser)
    initialization_parser.add_argument("--project-root", required=True)
    initialization_parser.add_argument("--profile", required=True)
    initialization_parser.add_argument("--format", choices=("json", "text"), default="json")
    initialization_parser.add_argument("--apply", action="store_true")
    initialization_parser.add_argument("--plan-id")
    for command in ("status", "doctor"):
        inspection_parser = subparsers.add_parser(command)
        _collection_flag(inspection_parser)
        inspection_parser.add_argument("--project-root", required=True)
        inspection_parser.add_argument("--format", choices=("json", "text"), default="json")
    activate_parser = subparsers.add_parser("activate")
    _collection_flag(activate_parser)
    activate_parser.add_argument("--project-root", required=True)
    activate_parser.add_argument("--apply", action="store_true")
    activate_parser.add_argument("--plan-id")
    return parser


def _collection_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--collection-root", default=str(Path.cwd()))
