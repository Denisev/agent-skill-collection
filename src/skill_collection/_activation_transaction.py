from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import stat

from ._issues import normalize_issues
from .activation import (
    ActivationAction,
    ActivationResult,
    CleanupReport,
    CreateActivationStateDirectoryAction,
    ManagedLink,
    WriteActivationRecordAction,
    prepare_activation,
    serialize_activation_record,
)
from .validation import Location, ValidationIssue


# Transaction implementation follows.
@dataclass(frozen=True, slots=True)
class _ReviewedSource:
    target: Location
    directory_identity: tuple[int, int]
    skill_file_identity: tuple[int, int]
    skill_file_sha256: str


def apply_activation(
    collection_root: str | Path,
    project_root: str | Path,
    plan_id: str | None,
) -> ActivationResult:
    """Apply exactly one freshly revalidated Activation Review."""
    collection = Path(collection_root).absolute()
    project = Path(project_root).absolute()
    review = prepare_activation(collection, project)
    if review.status == "blocked":
        return ActivationResult("blocked", None, None, None, (), (), None, review.blocking_issues, None)
    assert review.mode is not None and review.activation_id is not None and review.plan_id is not None
    if plan_id != review.plan_id:
        issue = ValidationIssue(
            "activation.stale_plan",
            "The supplied plan identifier does not match the current Activation Review.",
            Location("project", "skill-collection.toml"),
        )
        return ActivationResult("blocked", None, None, None, (), (), None, (issue,), None)
    if review.mode == "repeat":
        return ActivationResult(
            "unchanged", "repeat", review.activation_id, review.plan_id, (), (), None, (), None
        )

    support_issue = _mutation_support_issue(project)
    if support_issue is not None:
        return ActivationResult("blocked", None, None, None, (), (), None, (support_issue,), None)

    current_review = prepare_activation(collection, project)
    if current_review.status != "ready" or current_review.plan_id != review.plan_id:
        issue = ValidationIssue(
            "activation.stale_plan",
            "Filesystem state changed after the reviewed plan was selected.",
            Location("project", "skill-collection.toml"),
        )
        return ActivationResult("blocked", None, None, None, (), (), None, (issue,), None)
    review = current_review
    reviewed_root_identity = _path_identity(project)
    mutation_path_issue = _mutation_path_issue(project, review.actions)
    if mutation_path_issue is not None:
        return ActivationResult(
            "blocked", None, None, None, (), (), None, (mutation_path_issue,), None
        )
    reviewed_parent_identities = _reviewed_parent_identities(project, review.actions)
    assert review.proposed_activation_record is not None
    try:
        reviewed_sources = _capture_reviewed_sources(
            collection, review.proposed_activation_record.managed_links
        )
    except _ActivationFailure as error:
        issue = ValidationIssue(
            error.code, "A reviewed Skill source changed before activation.", error.location
        )
        return ActivationResult("blocked", None, None, None, (), (), None, (issue,), None)

    ledger = _InvocationLedger()
    transaction_started = False
    try:
        with _ProjectDirectory(
            project, reviewed_root_identity, reviewed_parent_identities
        ) as project_directory:
            project_directory.verify_root()
            for action in review.actions:
                project_directory.verify_root()
                transaction_started = True
                if action.kind in ("create-directory", "create-activation-state-directory"):
                    project_directory.mkdir(action.location.relative_path)
                    if action.kind == "create-activation-state-directory":
                        ledger.state_directory = action.location
                    else:
                        ledger.created_directories.append(action.location)
                    ledger.identities[action.location] = project_directory.identity_of(
                        action.location.relative_path
                    )
                    project_directory.remember_directory(action.location)
                    project_directory.fsync_parent(action.location.relative_path)
                elif action.kind == "create-symlink":
                    assert hasattr(action, "target")
                    source = reviewed_sources[action.target]
                    _revalidate_source(collection, source)
                    target = _source_path(collection, action.target)
                    link = ManagedLink(action.location, action.target)
                    project_directory.symlink(
                        str(target),
                        action.location.relative_path,
                        ledger,
                        link,
                    )
                    _revalidate_source(collection, source)
                    ledger.verify_created_link(link, str(target))
                    project_directory.fsync_parent(action.location.relative_path)
                else:
                    assert isinstance(action, WriteActivationRecordAction)
                    assert review.proposed_activation_record is not None
                    _revalidate_all_managed_links(
                        collection,
                        project,
                        review.proposed_activation_record.managed_links,
                        reviewed_sources,
                        ledger,
                    )
                    data = serialize_activation_record(review.proposed_activation_record)
                    if hashlib.sha256(data).hexdigest() != action.content_sha256:
                        raise _ActivationFailure("activation.record_content_mismatch", action.location)
                    temporary = (
                        ".agent-skill-collection/.activation.toml.tmp-"
                        + secrets.token_hex(8)
                    )
                    fd = project_directory.create_file(temporary, 0o600)
                    ledger.temporary_location = Location("project", temporary)
                    ledger.identities[ledger.temporary_location] = _file_identity(fd)
                    try:
                        _write_all(fd, data)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    if project_directory.read_file(temporary) != data:
                        raise _ActivationFailure("activation.record_verification_failed", action.location)
                    _revalidate_all_managed_links(
                        collection,
                        project,
                        review.proposed_activation_record.managed_links,
                        reviewed_sources,
                        ledger,
                    )
                    project_directory.link(temporary, action.location.relative_path)
                    ledger.record_location = action.location
                    ledger.identities[action.location] = project_directory.identity_of(
                        action.location.relative_path
                    )
                    project_directory.fsync_parent(action.location.relative_path)
                    if project_directory.read_file(action.location.relative_path) != data:
                        raise _ActivationFailure("activation.record_verification_failed", action.location)
                    project_directory.unlink(temporary)
                    project_directory.fsync_parent(temporary)
                    ledger.temporary_location = None
            _revalidate_all_managed_links(
                collection,
                project,
                review.proposed_activation_record.managed_links,
                reviewed_sources,
                ledger,
            )
            ledger.close_handles()
        return ActivationResult(
            "applied",
            review.mode,
            review.activation_id,
            review.plan_id,
            ledger.result_directories(),
            tuple(link.location for link in ledger.created_links),
            ledger.record_location,
            (),
            None,
        )
    except _ActivationFailure as error:
        if not transaction_started:
            issue = ValidationIssue(error.code, "Activation was blocked before creation.", error.location)
            return ActivationResult("blocked", None, None, None, (), (), None, (issue,), None)
        cleanup = _safe_cleanup(collection, project, ledger) if transaction_started else None
        return ActivationResult(
            "failed",
            review.mode,
            review.activation_id,
            review.plan_id,
            ledger.result_directories(),
            tuple(link.location for link in ledger.created_links),
            ledger.record_location,
            (ValidationIssue(error.code, "Activation could not be completed.", error.location),),
            cleanup,
        )
    except OSError:
        if not transaction_started:
            issue = ValidationIssue(
                "activation.precondition_changed",
                "A reviewed filesystem precondition changed before activation began.",
                Location("project", "."),
            )
            return ActivationResult("blocked", None, None, None, (), (), None, (issue,), None)
        cleanup = _safe_cleanup(collection, project, ledger)
        return ActivationResult(
            "failed", review.mode, review.activation_id, review.plan_id,
            ledger.result_directories(), tuple(link.location for link in ledger.created_links),
            ledger.record_location,
            (ValidationIssue("activation.operation_failed", "Activation could not be completed.", Location("project", ".")),),
            cleanup,
        )
    except (KeyboardInterrupt, Exception) as error:
        if transaction_started:
            report = _safe_cleanup(collection, project, ledger)
            try:
                setattr(error, "activation_cleanup_report", report)
                if report.remaining_objects or report.issues:
                    error.add_note("Activation cleanup was incomplete.")
            except Exception:
                pass
        raise


class _ActivationFailure(Exception):
    def __init__(self, code: str, location: Location) -> None:
        super().__init__(code)
        self.code = code
        self.location = location


@dataclass(slots=True)
class _InvocationLedger:
    created_directories: list[Location]
    created_links: list[ManagedLink]
    temporary_location: Location | None
    record_location: Location | None
    identities: dict[Location, tuple[int, int]]
    state_directory: Location | None
    link_parent_handles: dict[Location, tuple[int, str]]

    def __init__(self) -> None:
        self.created_directories = []
        self.created_links = []
        self.temporary_location = None
        self.record_location = None
        self.identities = {}
        self.state_directory = None
        self.link_parent_handles = {}

    def result_directories(self) -> tuple[Location, ...]:
        return (
            ((self.state_directory,) if self.state_directory is not None else ())
            + tuple(self.created_directories)
        )

    def register_symlink(
        self,
        link: ManagedLink,
        retained_parent_fd: int,
        name: str,
    ) -> None:
        self.created_links.append(link)
        self.link_parent_handles[link.location] = (retained_parent_fd, name)
        metadata = os.stat(name, dir_fd=retained_parent_fd, follow_symlinks=False)
        self.identities[link.location] = (metadata.st_dev, metadata.st_ino)

    def close_handles(self) -> None:
        for parent_fd, _ in self.link_parent_handles.values():
            try:
                os.close(parent_fd)
            except OSError:
                pass
        self.link_parent_handles.clear()

    def verify_created_link(self, link: ManagedLink, expected_text: str) -> None:
        retained = self.link_parent_handles.get(link.location)
        if retained is None:
            raise _ActivationFailure("activation.source_changed", link.location)
        parent_fd, name = retained
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or os.readlink(name, dir_fd=parent_fd) != expected_text
            ):
                raise _ActivationFailure("activation.source_changed", link.location)
        except OSError as error:
            raise _ActivationFailure(
                "activation.source_changed", link.location
            ) from error


class _ProjectDirectory:
    def __init__(
        self,
        project: Path,
        expected_root: tuple[int, int],
        expected_parents: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.project = project.absolute()
        self.parent_fd = -1
        self.root_fd = -1
        self.root_name = self.project.name
        self.identity = expected_root
        self.expected_parents = dict(expected_parents or {})

    def __enter__(self) -> _ProjectDirectory:
        flags = os.O_RDONLY | os.O_DIRECTORY
        self.parent_fd = os.open(self.project.parent, flags)
        self.root_fd = os.open(
            self.root_name, flags | os.O_NOFOLLOW, dir_fd=self.parent_fd
        )
        metadata = os.fstat(self.root_fd)
        if (metadata.st_dev, metadata.st_ino) != self.identity:
            os.close(self.root_fd)
            self.root_fd = -1
            os.close(self.parent_fd)
            self.parent_fd = -1
            raise _ActivationFailure(
                "activation.project_replaced", Location("project", ".")
            )
        return self

    def __exit__(self, *_: object) -> None:
        if self.root_fd >= 0:
            os.close(self.root_fd)
        if self.parent_fd >= 0:
            os.close(self.parent_fd)

    def verify_root(self) -> None:
        try:
            fd = os.open(
                self.root_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.parent_fd,
            )
        except OSError as error:
            raise _ActivationFailure(
                "activation.project_replaced", Location("project", ".")
            ) from error
        try:
            metadata = os.fstat(fd)
            if (metadata.st_dev, metadata.st_ino) != self.identity:
                raise _ActivationFailure(
                    "activation.project_replaced", Location("project", ".")
                )
        finally:
            os.close(fd)

    def _parent(self, relative: str) -> tuple[int, str]:
        parts = Path(relative).parts
        fd = os.dup(self.root_fd)
        try:
            for index, component in enumerate(parts[:-1]):
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                parent_relative = Path(*parts[: index + 1]).as_posix()
                expected = self.expected_parents.get(parent_relative)
                metadata = os.fstat(next_fd)
                if expected is not None and (metadata.st_dev, metadata.st_ino) != expected:
                    os.close(next_fd)
                    raise _ActivationFailure(
                        "activation.parent_changed",
                        Location("project", parent_relative),
                    )
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except Exception:
            os.close(fd)
            raise

    def mkdir(self, relative: str) -> None:
        parent, name = self._parent(relative)
        try:
            os.mkdir(name, 0o755, dir_fd=parent)
        except OSError as error:
            raise _ActivationFailure(
                "activation.precondition_changed", Location("project", relative)
            ) from error
        finally:
            os.close(parent)

    def symlink(
        self,
        target: str,
        relative: str,
        ledger: _InvocationLedger,
        link: ManagedLink,
    ) -> None:
        parent, name = self._parent(relative)
        retained_parent = os.dup(parent)
        try:
            os.symlink(target, name, dir_fd=parent)
            ledger.register_symlink(link, retained_parent, name)
            retained_parent = -1
            self._verify_parent_path(relative)
        except OSError as error:
            raise _ActivationFailure(
                "activation.precondition_changed", Location("project", relative)
            ) from error
        finally:
            if retained_parent >= 0:
                os.close(retained_parent)
            os.close(parent)

    def create_file(self, relative: str, mode: int) -> int:
        parent, name = self._parent(relative)
        try:
            return os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode,
                dir_fd=parent,
            )
        finally:
            os.close(parent)

    def link(self, source: str, destination: str) -> None:
        source_parent, source_name = self._parent(source)
        destination_parent, destination_name = self._parent(destination)
        try:
            os.link(
                source_name,
                destination_name,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
                follow_symlinks=False,
            )
        finally:
            os.close(source_parent)
            os.close(destination_parent)

    def unlink(self, relative: str) -> None:
        parent, name = self._parent(relative)
        try:
            os.unlink(name, dir_fd=parent)
        finally:
            os.close(parent)

    def rmdir(self, relative: str) -> None:
        parent, name = self._parent(relative)
        try:
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(parent)

    def read_file(self, relative: str) -> bytes:
        parent, name = self._parent(relative)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            chunks: list[bytes] = []
            while chunk := os.read(fd, 65536):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def matches_created_object(
        self,
        kind: str,
        relative: str,
        expected_link_text: str | None,
        expected_identity: tuple[int, int] | None,
    ) -> bool:
        parent, name = self._parent(relative)
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if expected_identity is None or (
                metadata.st_dev,
                metadata.st_ino,
            ) != expected_identity:
                return False
            if kind == "directory":
                return stat.S_ISDIR(metadata.st_mode)
            if kind == "link":
                return stat.S_ISLNK(metadata.st_mode) and os.readlink(
                    name, dir_fd=parent
                ) == expected_link_text
            return stat.S_ISREG(metadata.st_mode)
        except OSError:
            return False
        finally:
            os.close(parent)

    def identity_of(self, relative: str) -> tuple[int, int]:
        parent, name = self._parent(relative)
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            return metadata.st_dev, metadata.st_ino
        finally:
            os.close(parent)

    def remember_directory(self, location: Location) -> None:
        self.expected_parents[location.relative_path] = self.identity_of(
            location.relative_path
        )

    def _verify_parent_path(self, relative: str) -> None:
        parent, _ = self._parent(relative)
        os.close(parent)

    def fsync_parent(self, relative: str) -> None:
        parent, _ = self._parent(relative)
        try:
            os.fsync(parent)
        except OSError as error:
            raise _ActivationFailure(
                "activation.directory_fsync_failed", Location("project", str(Path(relative).parent.as_posix()))
            ) from error
        finally:
            os.close(parent)


def _mutation_support_issue(project: Path) -> ValidationIssue | None:
    required_dir_fd = (
        os.open,
        os.stat,
        os.readlink,
        os.mkdir,
        os.symlink,
        os.unlink,
        os.rmdir,
        os.link,
    )
    if (
        not hasattr(os, "O_NOFOLLOW")
        or any(function not in os.supports_dir_fd for function in required_dir_fd)
        or os.link not in os.supports_follow_symlinks
        or os.stat not in os.supports_follow_symlinks
    ):
        return ValidationIssue(
            "activation.containment_unsupported",
            "This platform cannot safely confine project activation.",
            Location("project", "."),
        )
    try:
        fd = os.open(project.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return ValidationIssue(
            "activation.directory_fsync_unsupported",
            "This platform cannot durably synchronize project directories.",
            Location("project", "."),
        )
    try:
        fd = os.open(
            project / "skill-collection.toml", os.O_RDONLY | os.O_NOFOLLOW
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return ValidationIssue(
            "activation.file_fsync_unsupported",
            "This platform cannot durably synchronize regular files.",
            Location("project", "skill-collection.toml"),
        )
    return None


def _mutation_path_issue(
    project: Path, actions: tuple[ActivationAction, ...]
) -> ValidationIssue | None:
    for action in actions:
        current = project
        parts = Path(action.location.relative_path).parts
        for component in parts[:-1]:
            current = current / component
            if current.is_symlink():
                return ValidationIssue(
                    "activation.mutation_parent_symlink",
                    "A mutation parent is a symlink and cannot be opened without following it.",
                    Location("project", current.relative_to(project).as_posix()),
                )
            if not current.exists():
                break
    return None


def _reviewed_parent_identities(
    project: Path, actions: tuple[ActivationAction, ...]
) -> dict[str, tuple[int, int]]:
    identities: dict[str, tuple[int, int]] = {}
    for action in actions:
        parts = Path(action.location.relative_path).parts
        for index in range(1, len(parts)):
            relative = Path(*parts[:index]).as_posix()
            path = project / relative
            try:
                metadata = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                break
            if stat.S_ISDIR(metadata.st_mode):
                identities[relative] = (metadata.st_dev, metadata.st_ino)
    return identities


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _capture_reviewed_sources(
    collection: Path, links: tuple[ManagedLink, ...]
) -> dict[Location, _ReviewedSource]:
    return {
        link.target: _source_identity(collection, link.target)
        for link in links
    }


def _source_identity(collection: Path, target: Location) -> _ReviewedSource:
    source = collection / target.relative_path
    skill_file = source / "SKILL.md"
    try:
        collection_resolved = collection.resolve(strict=True)
        resolved = source.resolve(strict=True)
        expected = collection_resolved / target.relative_path
        if not resolved.is_relative_to(collection_resolved) or resolved != expected:
            raise OSError("source escaped or traversed a symlink")
        source_metadata = source.lstat()
        skill_metadata = skill_file.lstat()
        if not stat.S_ISDIR(source_metadata.st_mode) or not stat.S_ISREG(
            skill_metadata.st_mode
        ):
            raise OSError("source type changed")
        skill_digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
        confirmed_skill_metadata = skill_file.lstat()
        if (
            confirmed_skill_metadata.st_dev,
            confirmed_skill_metadata.st_ino,
        ) != (skill_metadata.st_dev, skill_metadata.st_ino):
            raise OSError("skill file changed while inspected")
    except (OSError, RuntimeError) as error:
        raise _ActivationFailure("activation.source_changed", target) from error
    return _ReviewedSource(
        target,
        (source_metadata.st_dev, source_metadata.st_ino),
        (skill_metadata.st_dev, skill_metadata.st_ino),
        skill_digest,
    )


def _revalidate_source(collection: Path, reviewed: _ReviewedSource) -> None:
    current = _source_identity(collection, reviewed.target)
    if current != reviewed:
        raise _ActivationFailure("activation.source_changed", reviewed.target)


def _source_path(collection: Path, target: Location) -> Path:
    return collection.resolve(strict=True) / target.relative_path


def _revalidate_all_managed_links(
    collection: Path,
    project: Path,
    links: tuple[ManagedLink, ...],
    reviewed_sources: dict[Location, _ReviewedSource],
    ledger: _InvocationLedger,
) -> None:
    for link in links:
        reviewed = reviewed_sources[link.target]
        _revalidate_source(collection, reviewed)
        expected_text = str(_source_path(collection, link.target))
        if link.location in ledger.link_parent_handles:
            ledger.verify_created_link(link, expected_text)
            continue
        destination = project / link.location.relative_path
        try:
            metadata = destination.lstat()
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or os.readlink(destination) != expected_text
            ):
                raise OSError("managed link changed")
        except OSError as error:
            raise _ActivationFailure(
                "activation.source_changed", link.location
            ) from error


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def _file_identity(fd: int) -> tuple[int, int]:
    metadata = os.fstat(fd)
    return metadata.st_dev, metadata.st_ino


def _cleanup_invocation(
    collection: Path, project: Path, ledger: _InvocationLedger
) -> CleanupReport:
    try:
        return _cleanup_invocation_with_project(collection, project, ledger)
    finally:
        ledger.close_handles()


def _cleanup_invocation_with_project(
    collection: Path, project: Path, ledger: _InvocationLedger
) -> CleanupReport:
    removed_record = False
    removed_links: list[Location] = []
    removed_directories: list[Location] = []
    remaining: list[Location] = []
    issues: list[ValidationIssue] = []
    all_locations = (
        *((ledger.record_location,) if ledger.record_location is not None else ()),
        *((ledger.temporary_location,) if ledger.temporary_location is not None else ()),
        *(link.location for link in ledger.created_links),
        *ledger.created_directories,
        *((ledger.state_directory,) if ledger.state_directory else ()),
    )
    try:
        project_context = _ProjectDirectory(project, _path_identity(project))
        project_directory = project_context.__enter__()
    except BaseException:
        return CleanupReport(
            True,
            False,
            (),
            (),
            tuple(sorted(set(all_locations), key=lambda item: item.relative_path)),
            tuple(
                ValidationIssue(
                    "activation.cleanup_incomplete",
                    "Same-invocation cleanup could not reopen the project safely.",
                    location,
                )
                for location in sorted(set(all_locations), key=lambda item: item.relative_path)
            ),
        )
    try:
        objects: list[tuple[str, Location]] = []
        if ledger.record_location is not None:
            objects.append(("record", ledger.record_location))
        if ledger.temporary_location is not None:
            objects.append(("temporary", ledger.temporary_location))
        objects.extend(("link", link.location) for link in reversed(ledger.created_links))
        objects.extend(("directory", item) for item in reversed(ledger.created_directories))
        if ledger.state_directory is not None:
            objects.append(("directory", ledger.state_directory))
        for kind, location in objects:
            try:
                retained = ledger.link_parent_handles.get(location)
                if kind == "link" and retained is not None:
                    parent_fd, name = retained
                    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    expected_identity = ledger.identities.get(location)
                    expected_target = next(
                        str((collection / link.target.relative_path).resolve(strict=True))
                        for link in ledger.created_links
                        if link.location == location
                    )
                    if (
                        expected_identity is None
                        or (metadata.st_dev, metadata.st_ino) != expected_identity
                        or not stat.S_ISLNK(metadata.st_mode)
                        or os.readlink(name, dir_fd=parent_fd) != expected_target
                    ):
                        raise OSError("created link no longer matches")
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    removed_links.append(location)
                    continue
                project_directory.verify_root()
                if not project_directory.matches_created_object(
                    kind,
                    location.relative_path,
                    next(
                        (
                            str((collection / link.target.relative_path).resolve(strict=True))
                            for link in ledger.created_links
                            if link.location == location
                        ),
                        None,
                    ),
                    ledger.identities.get(location),
                ):
                    raise OSError("created object no longer matches")
                if kind == "directory":
                    project_directory.rmdir(location.relative_path)
                else:
                    project_directory.unlink(location.relative_path)
                project_directory.fsync_parent(location.relative_path)
                if kind == "record":
                    removed_record = True
                elif kind == "link":
                    removed_links.append(location)
                elif kind == "directory":
                    removed_directories.append(location)
            except BaseException:
                remaining.append(location)
                issues.append(
                    ValidationIssue(
                        "activation.cleanup_incomplete",
                        "Same-invocation cleanup could not confirm removal.",
                        location,
                    )
                )
    finally:
        ledger.close_handles()
        try:
            project_context.__exit__(None, None, None)
        except BaseException:
            for location in all_locations:
                if location not in remaining:
                    remaining.append(location)
                    issues.append(
                        ValidationIssue(
                            "activation.cleanup_incomplete",
                            "Cleanup descriptor closure could not be confirmed.",
                            location,
                        )
                    )
    return CleanupReport(
        True,
        removed_record,
        tuple(removed_links),
        tuple(removed_directories),
        tuple(remaining),
        tuple(normalize_issues(issues)),
    )


def _safe_cleanup(
    collection: Path, project: Path, ledger: _InvocationLedger
) -> CleanupReport:
    try:
        return _cleanup_invocation(collection, project, ledger)
    except BaseException:
        locations = tuple(
            sorted(
                {
                    *((ledger.record_location,) if ledger.record_location else ()),
                    *((ledger.temporary_location,) if ledger.temporary_location else ()),
                    *(link.location for link in ledger.created_links),
                    *ledger.created_directories,
                    *((ledger.state_directory,) if ledger.state_directory else ()),
                },
                key=lambda item: item.relative_path,
            )
        )
        return CleanupReport(
            True,
            False,
            (),
            (),
            locations,
            tuple(
                ValidationIssue(
                    "activation.cleanup_incomplete",
                    "Same-invocation cleanup failed unexpectedly.",
                    location,
                )
                for location in locations
            ),
        )
