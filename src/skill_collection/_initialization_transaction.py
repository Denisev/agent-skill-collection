from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import tomllib
from typing import Literal, TypeAlias

from ._binding import canonical_digest, semantic_binding_payload
from ._capabilities import containment_capability, directory_fsync_capability
from .initialization import InitializationPlan, plan_project_initialization
from .validation import Location, ValidationIssue


InitializationApplyStatus: TypeAlias = Literal[
    "created", "created_with_incomplete_cleanup", "blocked", "failed"
]
_BINDING = Location("project", "skill-collection.toml")
_ROOT = Location("project", ".")


@dataclass(frozen=True, slots=True)
class InitializationCleanupReport:
    attempted: bool
    removed_binding: bool
    removed_temporary_files: tuple[Location, ...]
    remaining_objects: tuple[Location, ...]
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class InitializationResult:
    status: InitializationApplyStatus
    plan_id: str | None
    binding_location: Location | None
    binding_digest: str | None
    issues: tuple[ValidationIssue, ...]
    cleanup: InitializationCleanupReport | None


@dataclass(slots=True)
class _Ledger:
    temporary: Location | None = None
    temporary_identity: tuple[int, int] | None = None
    binding_identity: tuple[int, int] | None = None
    committed: bool = False
    temporary_removed: bool = False
    temporary_unlink_failed: bool = False
    temporary_cleanup_issue: ValidationIssue | None = None

    @property
    def started(self) -> bool:
        return self.temporary is not None or self.binding_identity is not None


class _InitializationFailure(Exception):
    def __init__(self, code: str, location: Location = _BINDING) -> None:
        super().__init__(code)
        self.code = code
        self.location = location


class _DescriptorCloseFailure(OSError):
    """A descriptor-close failure is not a normal precondition change."""


def apply_project_initialization(
    collection_root: str | Path,
    project_root: str | Path,
    profile: str,
    plan_id: str | None,
) -> InitializationResult:
    collection = Path(collection_root).absolute()
    project = Path(project_root).absolute()
    first = plan_project_initialization(collection, project, profile)
    if first.status == "blocked":
        return _blocked(first.blocking_issues)
    assert first.plan_id is not None
    if plan_id != first.plan_id:
        return _blocked((_issue(
            "initialization.stale_plan",
            "The supplied plan identifier does not match the current Initialization Plan.",
        ),))

    support = _support_issue(project)
    if support is not None:
        return _blocked((support,))
    second = plan_project_initialization(collection, project, profile)
    if second.status != "ready" or second != first:
        return _blocked((_issue(
            "initialization.stale_plan",
            "Filesystem or collection state changed after the reviewed plan was selected.",
        ),))
    assert second.binding_content is not None
    assert second.binding_digest is not None
    assert second.plan_id is not None
    try:
        path_review = _PathReview.capture(project)
    except _DescriptorCloseFailure:
        raise
    except (OSError, _InitializationFailure):
        return _blocked((_issue(
            "initialization.precondition_changed",
            "A reviewed initialization precondition changed before Binding creation completed.",
        ),))
    ledger = _Ledger()
    try:
        path_review.verify()
        _apply_transaction(path_review, second, ledger)
    except _InitializationFailure as error:
        if not ledger.started:
            path_review.close()
            return _blocked((_failure_issue(error),))
        cleanup = _cleanup(path_review, ledger, error)
        if ledger.committed:
            return _created_with_incomplete_cleanup(second, ledger, cleanup)
        return InitializationResult(
            "failed", second.plan_id, _BINDING, second.binding_digest,
            (_failure_issue(error),), cleanup,
        )
    except _DescriptorCloseFailure as error:
        if ledger.started:
            cleanup = _cleanup(path_review, ledger, error)
            _attach_cleanup(error, cleanup)
        else:
            _close_secondary(path_review, error)
        raise
    except OSError as error:
        if not ledger.started:
            path_review.close()
            if error.errno in (errno.EEXIST, errno.EISDIR, errno.ENOTEMPTY):
                return _blocked((_issue(
                    "initialization.binding_exists",
                    "Project Binding destination already exists.",
                ),))
            return _blocked((_issue(
                "initialization.precondition_changed",
                "A reviewed initialization precondition changed before Binding creation completed.",
            ),))
        cleanup = _cleanup(path_review, ledger, error)
        if ledger.committed:
            return _created_with_incomplete_cleanup(second, ledger, cleanup)
        return InitializationResult(
            "failed", second.plan_id, _BINDING, second.binding_digest,
            (_issue("initialization.operation_failed", "Project Binding creation could not be completed."),),
            cleanup,
        )
    except (KeyboardInterrupt, Exception) as error:
        if ledger.started:
            cleanup = _cleanup(path_review, ledger, error)
            _attach_cleanup(error, cleanup)
        else:
            _close_secondary(path_review, error)
        raise
    try:
        path_review.close(retain_project_on_anchor_failure=True)
    except BaseException as error:
        cleanup = _cleanup(path_review, ledger, error)
        _attach_cleanup(error, cleanup)
        raise
    return InitializationResult(
        "created", second.plan_id, _BINDING, second.binding_digest, (), None
    )


def _support_issue(project: Path) -> ValidationIssue | None:
    if containment_capability() != "supported":
        return ValidationIssue(
            "initialization.containment_unsupported",
            "Safe project-contained Binding creation is not supported on this platform.",
            _ROOT,
        )
    if directory_fsync_capability(project.resolve()) != "supported":
        return ValidationIssue(
            "initialization.directory_fsync_unsupported",
            "The project filesystem does not support directory fsync required for durable Binding creation.",
            _ROOT,
        )
    return None


def _apply_transaction(review: _PathReview, plan: InitializationPlan, ledger: _Ledger) -> None:
    assert plan.binding_content is not None and plan.actions
    data = plan.binding_content.encode("utf-8")
    if "sha256:" + hashlib.sha256(data).hexdigest() != plan.actions[0].content_sha256:
        raise _InitializationFailure("initialization.content_mismatch")
    review.verify()
    temporary, fd = _create_temporary(review.project_fd, ledger)
    try:
        _write_all(fd, data)
        try:
            os.fsync(fd)
        except OSError as error:
            raise _InitializationFailure("initialization.file_fsync_failed") from error
    except BaseException as error:
        _close_fd_secondary(fd, error)
        raise
    else:
        _close_fd_or_raise(fd)
    _verify_file(review.project_fd, temporary, ledger.temporary_identity, data, plan.binding_digest)
    review.verify()
    try:
        os.link(
            temporary, "skill-collection.toml",
            src_dir_fd=review.project_fd, dst_dir_fd=review.project_fd,
            follow_symlinks=False,
        )
    except FileExistsError as error:
        raise _InitializationFailure("initialization.precondition_changed") from error
    # Link publication is a creation boundary.  Ledger it before inspecting it.
    ledger.binding_identity = ledger.temporary_identity
    observed_identity = _identity_name(review.project_fd, "skill-collection.toml")
    if observed_identity != ledger.binding_identity:
        raise _InitializationFailure("initialization.binding_verification_failed")
    _fsync_directory(review.project_fd)
    _verify_file(review.project_fd, "skill-collection.toml", ledger.binding_identity, data, plan.binding_digest)
    review.verify()
    _verify_file(review.project_fd, "skill-collection.toml", ledger.binding_identity, data, plan.binding_digest)
    ledger.committed = True
    try:
        _unlink_owned(review.project_fd, temporary, ledger.temporary_identity)
    except OSError:
        ledger.temporary_unlink_failed = True
        ledger.temporary_cleanup_issue = ValidationIssue(
            "initialization.cleanup_remove_failed",
            "Cleanup could not remove an invocation-created object.", ledger.temporary,
        )
        raise
    ledger.temporary_removed = True
    try:
        _fsync_directory(review.project_fd)
    except _InitializationFailure:
        ledger.temporary_cleanup_issue = ValidationIssue(
            "initialization.cleanup_directory_fsync_failed",
            "Cleanup directory synchronization could not be confirmed.", _ROOT,
        )
        raise
    ledger.temporary = None
    ledger.temporary_identity = None


def _create_temporary(project_fd: int, ledger: _Ledger) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(16):
        name = ".skill-collection.toml.tmp-" + secrets.token_hex(16)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=project_fd)
        except FileExistsError:
            continue
        ledger.temporary = Location("project", name)
        try:
            ledger.temporary_identity = _identity_fd(fd)
        except BaseException as error:
            _close_fd_secondary(fd, error)
            raise
        return name, fd
    raise _InitializationFailure("initialization.temporary_unavailable")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise _InitializationFailure("initialization.operation_failed")
        view = view[written:]


def _verify_file(
    project_fd: int,
    name: str,
    expected_identity: tuple[int, int] | None,
    data: bytes,
    binding_digest: str,
) -> None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=project_fd)
    except OSError as error:
        raise _InitializationFailure("initialization.binding_verification_failed") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != expected_identity:
            raise _InitializationFailure("initialization.binding_verification_failed")
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, 65536)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        observed = b"".join(chunks)
        if observed != data:
            raise _InitializationFailure("initialization.binding_verification_failed")
        document = tomllib.loads(observed.decode("utf-8"))
        if canonical_digest(semantic_binding_payload(document)) != binding_digest:
            raise _InitializationFailure("initialization.binding_verification_failed")
    except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise _InitializationFailure("initialization.binding_verification_failed") from error
    except BaseException as error:
        _close_fd_secondary(fd, error)
        raise
    else:
        _close_fd_or_raise(fd)


def _fsync_directory(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as error:
        raise _InitializationFailure("initialization.directory_fsync_failed", _ROOT) from error


@dataclass(slots=True)
class _PathReview:
    anchor_fd: int
    project_fd: int
    anchor_identity: tuple[int, int]
    canonical_components: tuple[tuple[str, tuple[int, int]], ...]
    lexical_components: tuple[tuple[str, tuple[int, int]], ...] | None
    lexical_symlink: tuple[str, tuple[int, int], str] | None

    @classmethod
    def capture(cls, project: Path) -> _PathReview:
        canonical = project.resolve(strict=True)
        anchor = Path(canonical.anchor)
        anchor_fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
        project_fd = -1
        try:
            canonical_components, project_fd = _capture_directory_chain(anchor_fd, canonical)
            lexical_components = None
            lexical_symlink = None
            lexical_metadata = os.lstat(project)
            if stat.S_ISLNK(lexical_metadata.st_mode):
                lexical_components, lexical_parent = _capture_directory_chain(anchor_fd, project.parent)
                try:
                    lexical_symlink = (project.name, _identity(lexical_metadata), os.readlink(project))
                except BaseException as error:
                    _close_fd_secondary(lexical_parent, error)
                    raise
                else:
                    _close_fd_or_raise(lexical_parent)
            elif project.absolute() != canonical:
                raise _InitializationFailure("initialization.precondition_changed")
            return cls(
                anchor_fd, project_fd, _identity_fd(anchor_fd), canonical_components,
                lexical_components, lexical_symlink,
            )
        except BaseException as error:
            if project_fd >= 0:
                _close_fd_secondary(project_fd, error)
            _close_fd_secondary(anchor_fd, error)
            raise

    def verify(self) -> None:
        if _identity_fd(self.anchor_fd) != self.anchor_identity:
            raise _InitializationFailure("initialization.precondition_changed")
        fd = _verify_directory_chain(self.anchor_fd, self.canonical_components)
        try:
            if _identity_fd(fd) != _identity_fd(self.project_fd):
                raise _InitializationFailure("initialization.precondition_changed")
        except BaseException as error:
            _close_fd_secondary(fd, error)
            raise
        else:
            _close_fd_or_raise(fd)
        if self.lexical_components is not None and self.lexical_symlink is not None:
            parent = _verify_directory_chain(self.anchor_fd, self.lexical_components)
            name, expected_identity, expected_text = self.lexical_symlink
            try:
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                text = os.readlink(name, dir_fd=parent)
                if not stat.S_ISLNK(metadata.st_mode) or _identity(metadata) != expected_identity or text != expected_text:
                    raise _InitializationFailure("initialization.precondition_changed")
            except OSError as error:
                _close_fd_secondary(parent, error)
                raise _InitializationFailure("initialization.precondition_changed") from error
            except BaseException as error:
                _close_fd_secondary(parent, error)
                raise
            else:
                _close_fd_or_raise(parent)

    def close(self, *, retain_project_on_anchor_failure: bool = False) -> None:
        pending: BaseException | None = None
        for attribute in ("anchor_fd", "project_fd"):
            fd = getattr(self, attribute)
            if fd < 0:
                continue
            try:
                os.close(fd)
            except BaseException as error:
                _mark_descriptor_close_failure(error)
                setattr(self, attribute, -1)
                if retain_project_on_anchor_failure and attribute == "anchor_fd":
                    # The final success-close handler needs the project descriptor
                    # for identity-checked cleanup of a published Binding.
                    raise
                if pending is None:
                    pending = error
                else:
                    _add_secondary_note(pending, error)
            else:
                setattr(self, attribute, -1)
        if pending is not None:
            raise pending


def _capture_directory_chain(anchor_fd: int, path: Path) -> tuple[tuple[tuple[str, tuple[int, int]], ...], int]:
    components = path.absolute().parts[1:]
    fd = os.dup(anchor_fd)
    records: list[tuple[str, tuple[int, int]]] = []
    try:
        for component in components:
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            try:
                _close_fd_or_raise(fd)
            except BaseException as error:
                _close_fd_secondary(next_fd, error)
                raise
            fd = next_fd
            records.append((component, _identity_fd(fd)))
        return tuple(records), fd
    except BaseException as error:
        _close_fd_secondary(fd, error)
        raise


def _verify_directory_chain(anchor_fd: int, records: tuple[tuple[str, tuple[int, int]], ...]) -> int:
    fd = os.dup(anchor_fd)
    try:
        for component, expected in records:
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            try:
                _close_fd_or_raise(fd)
            except BaseException as error:
                _close_fd_secondary(next_fd, error)
                raise
            fd = next_fd
            if _identity_fd(fd) != expected:
                raise _InitializationFailure("initialization.precondition_changed")
        return fd
    except _DescriptorCloseFailure as error:
        _close_fd_secondary(fd, error)
        raise
    except OSError as error:
        _close_fd_secondary(fd, error)
        raise _InitializationFailure("initialization.precondition_changed") from error
    except BaseException as error:
        _close_fd_secondary(fd, error)
        raise


def _cleanup(
    review: _PathReview, ledger: _Ledger, primary: BaseException | None = None,
) -> InitializationCleanupReport:
    removed_binding = False
    removed_temporary: list[Location] = (
        [ledger.temporary] if ledger.temporary_removed and ledger.temporary is not None else []
    )
    remaining: list[Location] = (
        [ledger.temporary]
        if ledger.committed and ledger.temporary_unlink_failed and ledger.temporary is not None
        else []
    )
    issues: list[ValidationIssue] = (
        [ledger.temporary_cleanup_issue] if ledger.temporary_cleanup_issue is not None else []
    )
    if primary is not None and _has_descriptor_close_failure(primary):
        _append_descriptor_close_issue(issues)
    try:
        review.verify()
    except _DescriptorCloseFailure:
        _append_descriptor_close_issue(issues)
    except BaseException:
        pass
    if not ledger.committed and ledger.binding_identity is not None:
        if _cleanup_name(review.project_fd, "skill-collection.toml", ledger.binding_identity, _BINDING, issues):
            removed_binding = True
        else:
            remaining.append(_BINDING)
    if (
        ledger.temporary is not None
        and not ledger.temporary_removed
        and not ledger.temporary_unlink_failed
    ):
        if ledger.temporary_identity is not None and _cleanup_name(
            review.project_fd, ledger.temporary.relative_path,
            ledger.temporary_identity, ledger.temporary, issues,
        ):
            removed_temporary.append(ledger.temporary)
        else:
            remaining.append(ledger.temporary)
    try:
        review.close()
    except BaseException:
        _append_descriptor_close_issue(issues)
    return InitializationCleanupReport(
        True, removed_binding, tuple(removed_temporary), tuple(remaining), tuple(issues)
    )


def _close_fd_secondary(fd: int, primary: BaseException) -> None:
    try:
        os.close(fd)
    except BaseException as close_error:
        _mark_descriptor_close_failure(primary)
        _add_secondary_note(primary, close_error)


def _mark_descriptor_close_failure(error: BaseException) -> None:
    try:
        setattr(error, "initialization_descriptor_close_failed", True)
    except Exception:
        pass


def _has_descriptor_close_failure(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _DescriptorCloseFailure) or bool(
            getattr(current, "initialization_descriptor_close_failed", False)
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _append_descriptor_close_issue(issues: list[ValidationIssue]) -> None:
    if not any(issue.code == "initialization.cleanup_descriptor_close_failed" for issue in issues):
        issues.append(ValidationIssue(
            "initialization.cleanup_descriptor_close_failed",
            "Cleanup descriptor closure could not be confirmed.", _ROOT,
        ))


def _close_fd_or_raise(fd: int) -> None:
    try:
        os.close(fd)
    except OSError as error:
        raise _DescriptorCloseFailure() from error


def _close_secondary(review: _PathReview, primary: BaseException) -> None:
    try:
        review.close()
    except BaseException as close_error:
        _add_secondary_note(primary, close_error)


def _add_secondary_note(primary: BaseException, secondary: BaseException) -> None:
    try:
        primary.add_note(f"Descriptor close also failed: {type(secondary).__name__}")
    except Exception:
        pass


def _cleanup_name(
    project_fd: int, name: str, expected: tuple[int, int], location: Location,
    issues: list[ValidationIssue],
) -> bool:
    try:
        metadata = os.stat(name, dir_fd=project_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != expected:
            issues.append(ValidationIssue(
                "initialization.cleanup_identity_changed",
                "Cleanup did not remove an object whose identity changed.", location,
            ))
            return False
        os.unlink(name, dir_fd=project_fd)
        try:
            os.fsync(project_fd)
        except OSError:
            issues.append(ValidationIssue(
                "initialization.cleanup_directory_fsync_failed",
                "Cleanup directory synchronization could not be confirmed.", _ROOT,
            ))
        return True
    except FileNotFoundError:
        issues.append(ValidationIssue(
            "initialization.cleanup_identity_changed",
            "Cleanup did not remove an object whose identity changed.", location,
        ))
        return False
    except OSError:
        if not any(
            issue.code == "initialization.cleanup_remove_failed" and issue.location == location
            for issue in issues
        ):
            issues.append(ValidationIssue(
                "initialization.cleanup_remove_failed",
                "Cleanup could not remove an invocation-created object.", location,
            ))
        return False


def _unlink_owned(project_fd: int, name: str, expected: tuple[int, int] | None) -> None:
    if expected is None or _identity_name(project_fd, name) != expected:
        raise _InitializationFailure("initialization.precondition_changed")
    os.unlink(name, dir_fd=project_fd)


def _identity_name(parent_fd: int, name: str) -> tuple[int, int]:
    return _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))


def _identity_fd(fd: int) -> tuple[int, int]:
    return _identity(os.fstat(fd))


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _created_with_incomplete_cleanup(
    plan: InitializationPlan, ledger: _Ledger, cleanup: InitializationCleanupReport,
) -> InitializationResult:
    assert plan.plan_id is not None
    assert plan.binding_digest is not None
    assert ledger.temporary is not None
    return InitializationResult(
        "created_with_incomplete_cleanup", plan.plan_id, _BINDING,
        plan.binding_digest, (ValidationIssue(
            "initialization.temporary_cleanup_incomplete",
            "Binding creation completed, but temporary-file cleanup could not be confirmed.",
            ledger.temporary,
        ),), cleanup,
    )


def _failure_issue(error: _InitializationFailure) -> ValidationIssue:
    messages = {
        "initialization.content_mismatch": "The reviewed Binding content did not match its proposed digest.",
        "initialization.file_fsync_failed": "The Binding file could not be synchronized.",
        "initialization.directory_fsync_failed": "The project directory could not be synchronized after Binding creation.",
        "initialization.binding_verification_failed": "The created Binding could not be verified.",
        "initialization.precondition_changed": "A reviewed initialization precondition changed before Binding creation completed.",
        "initialization.temporary_unavailable": "An exclusive temporary Binding file could not be created.",
        "initialization.operation_failed": "Project Binding creation could not be completed.",
    }
    return ValidationIssue(error.code, messages[error.code], error.location)


def _issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code, message, _BINDING)


def _blocked(issues: tuple[ValidationIssue, ...]) -> InitializationResult:
    return InitializationResult("blocked", None, None, None, issues, None)


def _attach_cleanup(error: BaseException, cleanup: InitializationCleanupReport) -> None:
    try:
        setattr(error, "initialization_cleanup_report", cleanup)
        if cleanup.remaining_objects or cleanup.issues:
            error.add_note("Project initialization cleanup was incomplete.")
    except Exception:
        pass
