from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import shutil
import subprocess
import time
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from .validation import is_identifier, is_portable_collection_url

InspectionRoot = Literal["collection", "source"]
InspectionStatus = Literal["ready", "blocked"]
InspectionRelationship = Literal["unchanged", "unverified"]
_REF = re.compile(r"^refs/heads/(?!.*(?:\.\.|//|@\{|\\))[A-Za-z0-9][A-Za-z0-9._/-]*$")
_OBJECT = re.compile(r"^[0-9a-f]{40}$")
_INSPECTION_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
REMOTE_TIMEOUT_SECONDS = 15
REMOTE_OUTPUT_LIMIT_BYTES = 65_536
def _resolve_trusted_git_executable() -> str | None:
    """Resolve Git only through the platform's fixed default executable path."""
    return shutil.which("git", path=os.defpath)


_PROCESS_GROUP_WAIT_SECONDS = 2.0

_ISSUE_MESSAGES = {
    "source-update.network_not_authorized": "Explicit anonymous HTTPS remote inspection authorization is required.",
    "source-update.request_invalid": "A Source request or full branch ref is malformed.",
    "source-update.request_duplicate": "A Source identity was requested more than once.",
    "source-update.source_missing": "The requested Source does not exist.",
    "source-update.source_not_external": "The requested Source is not a native Git submodule Source.",
    "source-update.remote_transport_unsupported": "The Source URL is outside the anonymous HTTPS transport policy.",
    "source-update.current_pin_unavailable": "The committed Source gitlink pin cannot be established.",
    "source-update.object_format_unsupported": "Checkpoint 7A supports only the SHA-1 Git object format.",
    "source-update.remote_unavailable": "The exact remote could not be inspected within the required bounds.",
    "source-update.remote_response_invalid": "The remote advertisement was malformed or ambiguous.",
    "source-update.remote_ref_missing": "The exact requested ref was not advertised.",
    "source-update.credentials_required": "The remote rejected anonymous inspection or required credentials.",
}
_SOURCE_ENTRY = re.compile(r"^sources\.toml#sources\[[0-9]+\]$")
_SOURCE_URL = re.compile(r"^sources\.toml#sources\[[0-9]+\]\.url$")
_SOURCE_PATH = re.compile(r"^sources\.toml#sources\[[0-9]+\]\.path$")


@dataclass(frozen=True, slots=True)
class _CleanupEvidence:
    phase: str


@dataclass(frozen=True, slots=True)
class _SourceInspectionState:
    sources: list[dict[str, object]]
    issues: tuple[InspectionIssue, ...]
    document_text: str | None = None


class _RemoteLifecycleFailure(RuntimeError):
    """A deliberately detail-free internal lifecycle failure."""


class _RemoteCleanupIncomplete(RuntimeError):
    """Crosses only the CLI's sanitized system-error boundary."""


def _lifecycle(stage: str, operation: object, /, *args: object, **kwargs: object) -> object:
    """A small, named seam for lifecycle fault injection and auditing."""
    return operation(*args, **kwargs)  # type: ignore[operator]


def _record_cleanup_evidence(primary: BaseException | None, evidence: _CleanupEvidence | None) -> _CleanupEvidence | None:
    if evidence is not None and primary is not None and not hasattr(primary, "remote_cleanup_evidence"):
        setattr(primary, "remote_cleanup_evidence", evidence)
    return evidence


@dataclass(frozen=True, slots=True, order=True)
class InspectionLocation:
    root: InspectionRoot
    label: str | None
    relative_path: str

    def __post_init__(self) -> None:
        if type(self.root) is not str or type(self.relative_path) is not str:
            raise ValueError("invalid inspection location")
        if self.root not in ("collection", "source"):
            raise ValueError("invalid inspection root")
        if self.root == "collection" and self.label is not None:
            raise ValueError("collection locations cannot have labels")
        if self.root == "source" and (
            type(self.label) is not str
            or not is_identifier(self.label)
        ):
            raise ValueError("invalid source label")
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or "\\" in self.relative_path
            or any(ord(character) < 32 or ord(character) == 127 for character in self.relative_path)
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != self.relative_path
        ):
            raise ValueError("invalid rooted path")


@dataclass(frozen=True, slots=True)
class RemoteCandidateRequest:
    source_id: str
    remote_ref: str

    def __post_init__(self) -> None:
        if type(self.source_id) is not str or type(self.remote_ref) is not str:
            raise ValueError("invalid remote candidate request")
        if not is_identifier(self.source_id) or not _valid_remote_ref(self.remote_ref):
            raise ValueError("invalid remote candidate request")


@dataclass(frozen=True, slots=True)
class NetworkAuthorization:
    kind: Literal["anonymous-https-remote-inspection"]

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind != "anonymous-https-remote-inspection":
            raise ValueError("invalid network authorization")


@dataclass(frozen=True, slots=True)
class RemoteCandidateComparison:
    source_id: str
    source_location: InspectionLocation
    remote_ref: str
    current_revision: str
    candidate_revision: str
    relationship: InspectionRelationship

    def __post_init__(self) -> None:
        if (
            type(self.source_id) is not str
            or type(self.remote_ref) is not str
            or type(self.current_revision) is not str
            or type(self.candidate_revision) is not str
            or not is_identifier(self.source_id)
            or not _valid_remote_ref(self.remote_ref)
        ):
            raise ValueError("invalid comparison identity")
        if type(self.source_location) is not InspectionLocation or self.source_location != InspectionLocation("source", self.source_id, "."):
            raise ValueError("invalid comparison location")
        if type(self.relationship) is not str or self.relationship not in ("unchanged", "unverified"):
            raise ValueError("invalid inspection relationship")
        if not _OBJECT.fullmatch(self.current_revision) or not _OBJECT.fullmatch(self.candidate_revision):
            raise ValueError("invalid object ID")
        if self.relationship == "unchanged" and self.current_revision != self.candidate_revision:
            raise ValueError("invalid unchanged relationship")
        if self.relationship == "unverified" and self.current_revision == self.candidate_revision:
            raise ValueError("invalid unverified relationship")


@dataclass(frozen=True, slots=True)
class InspectionIssue:
    code: str
    message: str
    location: InspectionLocation
    related_locations: tuple[InspectionLocation, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or not self.code
            or type(self.message) is not str
            or not self.message
            or type(self.location) is not InspectionLocation
            or type(self.related_locations) is not tuple
        ):
            raise ValueError("invalid inspection issue")
        if not all(type(item) is InspectionLocation for item in self.related_locations):
            raise ValueError("invalid related inspection locations")
        if self.code not in _ISSUE_MESSAGES or self.message != _ISSUE_MESSAGES[self.code]:
            raise ValueError("invalid inspection issue taxonomy")
        if self.related_locations or not _valid_issue_location(self.code, self.location):
            raise ValueError("invalid inspection issue location")


@dataclass(frozen=True, slots=True)
class RemoteCandidateInspection:
    status: InspectionStatus
    inspection_id: str | None
    comparisons: tuple[RemoteCandidateComparison, ...]
    issues: tuple[InspectionIssue, ...]

    def __post_init__(self) -> None:
        if type(self.status) is not str:
            raise ValueError("invalid inspection status")
        if self.status == "ready":
            if (not self.comparisons or self.issues or type(self.inspection_id) is not str
                    or _INSPECTION_ID.fullmatch(self.inspection_id) is None):
                raise ValueError("invalid ready inspection")
        elif self.status == "blocked":
            if self.inspection_id is not None or self.comparisons or not self.issues:
                raise ValueError("invalid blocked inspection")
        else:
            raise ValueError("invalid inspection status")
        if type(self.comparisons) is not tuple or type(self.issues) is not tuple:
            raise ValueError("inspection collections must be tuples")
        if not all(type(item) is RemoteCandidateComparison for item in self.comparisons):
            raise ValueError("invalid comparisons")
        if not all(type(item) is InspectionIssue for item in self.issues):
            raise ValueError("invalid issues")
        if tuple(item.source_id for item in self.comparisons) != tuple(sorted(item.source_id for item in self.comparisons)):
            raise ValueError("comparisons must be sorted")
        if len({item.source_id for item in self.comparisons}) != len(self.comparisons):
            raise ValueError("comparisons must be unique")
        if self.issues != _normalize_issues(self.issues):
            raise ValueError("issues must be normalized")


def inspect_remote_candidates(
    collection_root: str | Path,
    requests: tuple[RemoteCandidateRequest, ...],
    network_authorization: NetworkAuthorization | None,
) -> RemoteCandidateInspection:
    if type(network_authorization) is not NetworkAuthorization or network_authorization.kind != "anonymous-https-remote-inspection":
        return _blocked(_issue("source-update.network_not_authorized", "collection", None, "."))
    if type(requests) is not tuple or not requests or any(
        type(request) is not RemoteCandidateRequest for request in requests
    ):
        return _blocked(_issue("source-update.request_invalid", "collection", None, "sources.toml"))
    if len({request.source_id for request in requests}) != len(requests):
        return _blocked(_issue("source-update.request_duplicate", "collection", None, "sources.toml"))
    collection = Path(collection_root)
    try:
        state = _validate_source_collection(
            collection, tuple(sorted(request.source_id for request in requests))
        )
    except (OSError, subprocess.TimeoutExpired):
        return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, "."))
    if state.issues:
        return _blocked(*state.issues)
    try:
        if type(state) is _SourceInspectionState:
            collection_output = _local_git(
                collection, "rev-parse", "--show-prefix", "--show-object-format"
            ).stdout
            if not collection_output.startswith("\n"):
                return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, "."))
            object_format = collection_output[1:-1] if collection_output.endswith("\n") else ""
        else:
            object_format = _local_git(collection, "rev-parse", "--show-object-format").stdout.strip()
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, "."))
    if object_format != "sha1":
        return _blocked(_issue("source-update.object_format_unsupported", "collection", None, "."))
    source_map = {source.get("id"): source for source in state.sources}
    selected = []
    for request in sorted(requests, key=lambda item: item.source_id):
        source = source_map.get(request.source_id)
        if source is None:
            return _blocked(_issue("source-update.source_missing", "collection", None, "sources.toml"))
        entry_path = _source_path(state.sources, source)
        url_path = _source_path(state.sources, source, "url")
        path_path = _source_path(state.sources, source, "path")
        if source.get("kind") != "git-submodule":
            return _blocked(_issue("source-update.source_not_external", "collection", None, entry_path))
        if not _anonymous_https_url(source.get("url")):
            return _blocked(_issue("source-update.remote_transport_unsupported", "collection", None, url_path))
        source_repository = source.get("_inspection_repository")
        if not isinstance(source_repository, Path):
            source_repository = collection / str(source.get("path"))
        selected.append((request, source, entry_path, url_path, path_path, source_repository))
    for _, source, _, _, path_path, source_repository in selected:
        try:
            submodule_output = _local_git(
                source_repository,
                "rev-parse", "--show-prefix", "--show-object-format",
            ).stdout
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, path_path))
        if not submodule_output.startswith("\n"):
            return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, path_path))
        if submodule_output != "\nsha1\n":
            return _blocked(_issue("source-update.object_format_unsupported", "collection", None, path_path))
    try:
        head_result = _local_git(collection, "rev-parse", "--verify", "HEAD")
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, "."))
    head = _parse_object_id(head_result)
    if head is None:
        return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, "."))
    if type(state) is _SourceInspectionState and state.document_text is not None:
        try:
            committed_sources = _local_git(
                collection, "show", f"{head}:sources.toml", check=False
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, "."))
        if committed_sources.returncode != 0 or committed_sources.stdout != state.document_text:
            return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, "."))
    comparisons = []
    evidence = []
    for request, source, entry_path, url_path, path_path, _ in selected:
        path = str(source.get("path"))
        try:
            gitlink_result = _local_git(
                collection, "ls-tree", "-z", head, "--", f":(literal){path}", check=False
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, path_path))
        current = _parse_gitlink(gitlink_result.stdout, path) if gitlink_result.returncode == 0 else None
        if current is None:
            return _blocked(_issue("source-update.current_pin_unavailable", "collection", None, path_path))
        try:
            remote = _run_remote_git(str(source.get("url")), request.remote_ref)
        except KeyboardInterrupt:
            raise
        except UnicodeDecodeError:
            return _blocked(_issue("source-update.remote_response_invalid", "collection", None, entry_path))
        except _RemoteLifecycleFailure as error:
            if getattr(error, "remote_cleanup_evidence", None) is not None:
                raise _RemoteCleanupIncomplete() from error
            return _blocked(_issue("source-update.remote_unavailable", "collection", None, url_path))
        except (OSError, subprocess.TimeoutExpired) as error:
            if getattr(error, "remote_cleanup_evidence", None) is not None:
                raise _RemoteCleanupIncomplete() from error
            return _blocked(_issue("source-update.remote_unavailable", "collection", None, url_path))
        candidate, issue = _parse_advertisement(remote, request.remote_ref, entry_path, url_path)
        if issue:
            return _blocked(issue)
        assert candidate is not None
        relationship: InspectionRelationship = "unchanged" if candidate == current else "unverified"
        comparisons.append(RemoteCandidateComparison(request.source_id, InspectionLocation("source", request.source_id, "."), request.remote_ref, current, candidate, relationship))
        evidence.append({"source_id": request.source_id, "url": str(source.get("url")), "ref": request.remote_ref, "current": current, "candidate": candidate})
    inspection_id = _inspection_id(head, evidence)
    return RemoteCandidateInspection("ready", inspection_id, tuple(comparisons), ())


def _blocked(*issues: InspectionIssue) -> RemoteCandidateInspection:
    return RemoteCandidateInspection("blocked", None, (), tuple(_normalize_issues(issues)))


def _issue(code: str, root: InspectionRoot, label: str | None, path: str) -> InspectionIssue:
    return InspectionIssue(code, _ISSUE_MESSAGES[code], InspectionLocation(root, label, path))


def _validate_source_collection(
    collection: Path, selected_source_ids: tuple[str, ...]
) -> _SourceInspectionState:
    if not collection.is_dir():
        return _SourceInspectionState([], (_issue(
            "source-update.current_pin_unavailable",
            "collection", None, ".",
        ),))
    source_document = collection / "sources.toml"
    try:
        document_text = source_document.read_bytes().decode("utf-8")
        document = tomllib.loads(document_text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _SourceInspectionState([], (_issue(
            "source-update.source_missing",
            "collection", None, "sources.toml",
        ),))
    raw_sources = document.get("sources")
    version = document.get("version")
    if type(version) is not int or version != 1 or not isinstance(raw_sources, list):
        return _SourceInspectionState([], (_issue(
            "source-update.source_missing",
            "collection", None, "sources.toml",
        ),))
    selected: list[dict[str, object]] = []
    for source_id in selected_source_ids:
        matches = [
            (index, item) for index, item in enumerate(raw_sources)
            if isinstance(item, dict) and item.get("id") == source_id
        ]
        if len(matches) != 1:
            return _SourceInspectionState([], (_issue(
                "source-update.source_missing",
                "collection", None, "sources.toml",
            ),))
        index, source = matches[0]
        entry_path = f"sources.toml#sources[{index}]"
        if source.get("kind") != "git-submodule":
            return _SourceInspectionState([], (_issue(
                "source-update.source_not_external",
                "collection", None, entry_path,
            ),))
        path_value = source.get("path")
        if not _valid_source_path(path_value):
            return _SourceInspectionState([], (_issue(
                "source-update.current_pin_unavailable",
                "collection", None, f"{entry_path}.path",
            ),))
        source_repository = _confined_source_repository(collection, path_value)
        if source_repository is None:
            return _SourceInspectionState([], (_issue(
                "source-update.current_pin_unavailable",
                "collection", None, f"{entry_path}.path",
            ),))
        selected.append({
            **source,
            "_inspection_index": index,
            "_inspection_repository": source_repository,
        })
    return _SourceInspectionState(selected, (), document_text)


def _valid_source_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return value != "." and not path.is_absolute() and ".." not in path.parts


def _confined_source_repository(collection: Path, value: object) -> Path | None:
    if not _valid_source_path(value):
        return None
    assert isinstance(value, str)
    try:
        root = collection.resolve(strict=True)
        candidate = collection
        for component in PurePosixPath(value).parts:
            candidate /= component
            if candidate.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    expected = root.joinpath(*PurePosixPath(value).parts)
    return resolved if resolved == expected and resolved.is_dir() else None


def _valid_issue_location(code: str, location: InspectionLocation) -> bool:
    if location.root != "collection" or location.label is not None:
        return False
    path = location.relative_path
    if code == "source-update.network_not_authorized":
        return path == "."
    if code in ("source-update.request_invalid", "source-update.request_duplicate", "source-update.source_missing"):
        return path == "sources.toml"
    if code in ("source-update.source_not_external", "source-update.remote_response_invalid", "source-update.remote_ref_missing"):
        return _SOURCE_ENTRY.fullmatch(path) is not None
    if code in ("source-update.remote_transport_unsupported", "source-update.remote_unavailable", "source-update.credentials_required"):
        return _SOURCE_URL.fullmatch(path) is not None
    if code in ("source-update.current_pin_unavailable", "source-update.object_format_unsupported"):
        return path == "." or _SOURCE_PATH.fullmatch(path) is not None
    return False


def _normalize_issues(issues: tuple[InspectionIssue, ...]) -> tuple[InspectionIssue, ...]:
    return tuple(sorted(set(issues), key=lambda issue: (
        issue.code,
        issue.location.root,
        issue.location.label or "",
        issue.location.relative_path,
        tuple((item.root, item.label or "", item.relative_path) for item in issue.related_locations),
        issue.message,
    )))


def _valid_remote_ref(value: str) -> bool:
    if _REF.fullmatch(value) is None:
        return False
    if any(ord(character) < 32 or ord(character) == 127 or character in " ~^:?*[" for character in value):
        return False
    branch = value.removeprefix("refs/heads/")
    components = branch.split("/")
    return bool(branch) and all(
        component
        and not component.startswith(".")
        and not component.endswith((".", ".lock"))
        for component in components
    )


def _source_path(sources: list[dict[str, object]], source: dict[str, object], field: str | None = None) -> str:
    index = source["_inspection_index"] if "_inspection_index" in source else sources.index(source)
    entry = f"sources.toml#sources[{index}]"
    return entry if field is None else f"{entry}.{field}"


def _anonymous_https_url(value: object) -> bool:
    if not is_portable_collection_url(value) or not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.path not in ("", "/")


def _parse_advertisement(
    result: subprocess.CompletedProcess[str], remote_ref: str,
    entry_path: str = "sources.toml#sources[0]",
    url_path: str = "sources.toml#sources[0].url",
) -> tuple[str | None, InspectionIssue | None]:
    if result.returncode != 0:
        if any(marker in result.stderr.casefold() for marker in (
            "authentication failed", "could not read username", "terminal prompts disabled",
            "http 401", "http 403", "error: 401", "error: 403", "authorization failed",
        )):
            return None, _issue("source-update.credentials_required", "collection", None, url_path)
        return None, _issue("source-update.remote_unavailable", "collection", None, url_path)
    if result.stdout == "":
        return None, _issue("source-update.remote_ref_missing", "collection", None, entry_path)
    if result.stdout.count("\n") != 1 or not result.stdout.endswith("\n"):
        return None, _issue("source-update.remote_response_invalid", "collection", None, entry_path)
    fields = result.stdout[:-1].split("\t")
    object_id, ref = fields if len(fields) == 2 else ("", "")
    if ref != remote_ref or _OBJECT.fullmatch(object_id) is None:
        return None, _issue("source-update.remote_response_invalid", "collection", None, entry_path)
    return object_id, None


def _parse_gitlink(output: str, source_path: str) -> str | None:
    match = re.fullmatch(
        rf"160000 commit ([0-9a-f]{{40}})\t{re.escape(source_path)}\x00",
        output,
    )
    return match.group(1) if match is not None else None


def _parse_object_id(result: subprocess.CompletedProcess[str]) -> str | None:
    if result.returncode != 0 or result.stdout.count("\n") != 1 or not result.stdout.endswith("\n"):
        return None
    value = result.stdout[:-1]
    return value if _OBJECT.fullmatch(value) is not None else None


def _identity(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _inspection_id(collection_head: str, sources: list[dict[str, str]]) -> str:
    return _identity({
        "contract": "checkpoint-7a-remote-inspection-v1",
        "collection_head": collection_head,
        "object_format": "sha1",
        "sources": sorted(
            (
                {
                    "source_id": item["source_id"],
                    "url": item["url"],
                    "ref": item["ref"],
                    "current": item["current"],
                    "candidate": item["candidate"],
                }
                for item in sources
            ),
            key=lambda item: item["source_id"],
        ),
    })


def _local_git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    executable = _resolve_trusted_git_executable()
    if executable is None:
        raise OSError("trusted Git executable is unavailable")
    return _run_git(executable, "--no-optional-locks", "-C", str(repository), *args, check=check)


def _run_remote_git(url: str, remote_ref: str) -> subprocess.CompletedProcess[str]:
    executable = _resolve_trusted_git_executable()
    if executable is None:
        raise OSError("trusted Git executable is unavailable")
    return _run_bounded_remote((executable, "--no-optional-locks", "-c", "credential.helper=", "-c", "core.askPass=", "-c", "http.proxy=", "-c", "http.followRedirects=false", "-c", "http.emptyAuth=false", "-c", "protocol.version=0", "ls-remote", "--refs", url, remote_ref))


def _run_bounded_remote(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    primary: BaseException | None = None
    close_interruption: KeyboardInterrupt | None = None
    close_evidence: _CleanupEvidence | None = None
    try:
        previous_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            process = _lifecycle("spawn", subprocess.Popen, command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_environment(remote=True), cwd="/", start_new_session=True, text=False)  # type: ignore[assignment]
        finally:
            # A pending SIGINT is delivered only after STORE_FAST retained the
            # child handle, so the outer exception path can clean its group.
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
        assert process.stdout is not None and process.stderr is not None
        selector = _lifecycle("capture", selectors.DefaultSelector)  # type: ignore[assignment]
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + REMOTE_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, REMOTE_TIMEOUT_SECONDS)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, REMOTE_TIMEOUT_SECONDS)
            for key, _ in events:
                stage = f"{key.data}_read"
                chunk = _lifecycle(stage, os.read, key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    buffers[key.data].extend(chunk)  # type: ignore[arg-type]
                    if len(buffers[key.data]) > REMOTE_OUTPUT_LIMIT_BYTES:
                        raise _RemoteLifecycleFailure()
        returncode = _lifecycle("wait", process.wait, timeout=max(0.01, deadline - time.monotonic()))
        cleanup_evidence = _terminate_process_group(process, propagate_interruption=True)
        if cleanup_evidence is not None:
            cleanup_failure = _RemoteCleanupIncomplete()
            _record_cleanup_evidence(cleanup_failure, cleanup_evidence)
            raise cleanup_failure
    except BaseException as error:
        # Keep the original exception authoritative; cleanup is evidence only.
        primary = error
        if process is not None:
            _record_cleanup_evidence(primary, _terminate_process_group(process))
        raise
    finally:
        if selector is not None:
            try:
                _lifecycle("selector_close", selector.close)
            except BaseException as error:
                close_evidence = _CleanupEvidence("selector-close")
                if primary is None and isinstance(error, KeyboardInterrupt):
                    close_interruption = error
        if process is not None:
            if process.stdout is not None:
                try:
                    _lifecycle("stdout_close", process.stdout.close)
                except BaseException as error:
                    close_evidence = close_evidence or _CleanupEvidence("stdout-close")
                    if primary is None and isinstance(error, KeyboardInterrupt):
                        close_interruption = close_interruption or error
            if process.stderr is not None:
                try:
                    _lifecycle("stderr_close", process.stderr.close)
                except BaseException as error:
                    close_evidence = close_evidence or _CleanupEvidence("stderr-close")
                    if primary is None and isinstance(error, KeyboardInterrupt):
                        close_interruption = close_interruption or error
        _record_cleanup_evidence(primary, close_evidence)
    if close_interruption is not None:
        _record_cleanup_evidence(close_interruption, close_evidence)
        raise close_interruption
    if close_evidence is not None:
        raise _RemoteCleanupIncomplete()
    return subprocess.CompletedProcess(command, returncode, bytes(buffers["stdout"]).decode("utf-8"), bytes(buffers["stderr"]).decode("utf-8"))


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, propagate_interruption: bool = False
) -> _CleanupEvidence | None:
    try:
        return _terminate_process_group_impl(process)
    except KeyboardInterrupt:
        if propagate_interruption:
            raise
        return _CleanupEvidence("internal")
    except BaseException:
        return _CleanupEvidence("internal")


def _terminate_process_group_impl(process: subprocess.Popen[bytes]) -> _CleanupEvidence | None:
    group = process.pid
    term_deadline = time.monotonic() + _PROCESS_GROUP_WAIT_SECONDS
    try:
        _lifecycle("term", os.killpg, group, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except OSError:
        return _CleanupEvidence("term")
    while time.monotonic() < term_deadline:
        # Reap an exited leader, but use group existence—not leader state—as
        # the grace-period completion condition because descendants may remain.
        process.poll()
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return None
        except OSError:
            # Continue to the forced-termination boundary; final verification
            # remains authoritative.
            pass
        _lifecycle("term_wait", time.sleep, 0.01)
    try:
        _lifecycle("kill", os.killpg, group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return _CleanupEvidence("kill")
    kill_deadline = time.monotonic() + _PROCESS_GROUP_WAIT_SECONDS
    try:
        _lifecycle("kill_wait", process.wait, timeout=max(0.01, kill_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            _lifecycle("kill", process.kill)
            _lifecycle("kill_wait", process.wait, timeout=max(0.01, kill_deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            return _CleanupEvidence("leader-wait")
    while True:
        try:
            _lifecycle("group_verification", os.killpg, group, 0)
        except ProcessLookupError:
            return None
        except OSError:
            return _CleanupEvidence("verify")
        if time.monotonic() >= kill_deadline:
            return _CleanupEvidence("group-not-empty")
        _lifecycle("kill_wait", time.sleep, 0.01)


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="strict",
            timeout=REMOTE_TIMEOUT_SECONDS,
            env=_git_environment(),
        )
    except UnicodeDecodeError as error:
        raise OSError("local Git output was not valid UTF-8") from error


def _git_environment(*, remote: bool = False) -> dict[str, str]:
    env = {"PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0"}
    if remote:
        env.update({
            "HOME": os.devnull,
            "NETRC": os.devnull,
            "CURL_HOME": os.devnull,
            "XDG_CONFIG_HOME": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
            "GIT_DIR": os.devnull,
            "GIT_WORK_TREE": os.devnull,
        })
    return env
