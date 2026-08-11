from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .output import error_document, json_document
from .planning import plan_activation
from .scanning import scan
from .validation import validate


class _UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(self, message)


def main(
    argv: Sequence[str], *, stdout: TextIO, stderr: TextIO
) -> int:
    parser = _build_parser()
    try:
        arguments = parser.parse_args(list(argv))
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
        else:
            project = _absolute(arguments.project_root)
            result = plan_activation(collection, project)
            exit_code = 1 if result.status == "blocked" else 0
        stdout.write(json_document(arguments.command, result))
        return exit_code
    except _UsageError as error:
        error.parser.print_usage(file=stderr)
        stderr.write(f"{error.parser.prog}: error: {error.message}\n")
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        stderr.write(
            error_document(
                "system.unexpected", "An unexpected system failure occurred."
            )
        )
        return 3


def _absolute(value: str) -> Path:
    return Path(value).absolute()


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
    return parser


def _collection_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--collection-root", default=str(Path.cwd()))
