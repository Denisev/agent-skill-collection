from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from skill_collection import (
    InspectionIssue,
    InspectionLocation,
    NetworkAuthorization,
    RemoteCandidateComparison,
    RemoteCandidateInspection,
    RemoteCandidateRequest,
    inspect_remote_candidates,
)
from skill_collection.cli import main
from skill_collection.output import inspection_text, json_document
from skill_collection.source_update import (
    _RemoteCleanupIncomplete,
    _RemoteLifecycleFailure,
    _parse_advertisement,
    _resolve_trusted_git_executable,
)

_SUBMODULE_SHA1 = subprocess.CompletedProcess((), 0, "\nsha1\n", "")


def _test_git_executable() -> str:
    executable = _resolve_trusted_git_executable()
    if executable is None:
        raise AssertionError("tests require Git on the trusted platform path")
    return executable


class RemoteInspectionModelTests(unittest.TestCase):
    def test_git_executable_resolution_ignores_the_callers_path(self) -> None:
        executable = _test_git_executable()
        self.assertTrue(Path(executable).is_absolute())
        with patch.dict("os.environ", {"PATH": "/untrusted/user-controlled/bin"}):
            self.assertEqual(_resolve_trusted_git_executable(), executable)

    def test_ready_and_blocked_invariants_are_exact(self) -> None:
        comparison = RemoteCandidateComparison(
            "upstream", InspectionLocation("source", "upstream", "."),
            "refs/heads/main", "1" * 40, "2" * 40, "unverified",
        )
        ready = RemoteCandidateInspection("ready", "sha256:" + "a" * 64, (comparison,), ())
        self.assertEqual(ready.status, "ready")
        issue = InspectionIssue(
            "source-update.network_not_authorized",
            "Explicit anonymous HTTPS remote inspection authorization is required.",
            InspectionLocation("collection", None, "."),
        )
        blocked = RemoteCandidateInspection("blocked", None, (), (issue,))
        self.assertEqual(blocked.comparisons, ())

    def test_advertisement_parser_accepts_only_one_exact_record(self) -> None:
        valid = subprocess.CompletedProcess((), 0, "a" * 40 + "\trefs/heads/main\n", "")
        candidate, issue = _parse_advertisement(valid, "refs/heads/main")
        self.assertEqual(candidate, "a" * 40)
        self.assertIsNone(issue)
        malformed = subprocess.CompletedProcess((), 0, "A" * 40 + "\trefs/heads/main\n", "")
        _, issue = _parse_advertisement(malformed, "refs/heads/main")
        self.assertEqual(issue.code, "source-update.remote_response_invalid")  # type: ignore[union-attr]

    def test_advertisement_parser_rejects_every_noncanonical_record_shape(self) -> None:
        object_id = "a" * 40
        malformed_outputs = (
            f"{object_id}\trefs/heads/main\n{object_id}\trefs/heads/main\n",
            f"{object_id}\trefs/heads/other\n",
            f"{object_id}\trefs/heads/main\r\n",
            f"{object_id}\trefs/heads/main",
            f"{object_id}\trefs/heads/main\textra\n",
            f"ref: refs/heads/other\trefs/heads/main\n",
        )
        for stdout in malformed_outputs:
            with self.subTest(stdout=stdout):
                candidate, issue = _parse_advertisement(
                    subprocess.CompletedProcess((), 0, stdout, ""), "refs/heads/main"
                )
                self.assertIsNone(candidate)
                self.assertEqual(issue.code, "source-update.remote_response_invalid")  # type: ignore[union-attr]

    def test_committed_gitlink_parser_rejects_noncanonical_ls_tree_records(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{"id": "upstream", "kind": "git-submodule", "path": "vendor/upstream", "url": "https://example.invalid/upstream.git"}])
        object_id = "1" * 40
        malformed = (
            f"100644 commit {object_id}\tvendor/upstream\x00",
            f"160000 blob {object_id}\tvendor/upstream\x00",
            f"160000 commit {object_id}\tvendor/other\x00",
            f"160000 commit {object_id}\tvendor/upstream\x00"
            f"160000 commit {object_id}\tvendor/upstream\x00",
            f"160000 commit {object_id}\tvendor/upstream",
            f"160000 commit {object_id}\tvendor/upstream\n",
            f"160000  commit {object_id}\tvendor/upstream\x00",
        )
        sha1 = subprocess.CompletedProcess((), 0, "sha1\n", "")
        head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
        request = (RemoteCandidateRequest("upstream", "refs/heads/main"),)
        for output in malformed:
            with self.subTest(output=output), patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
                "skill_collection.source_update._local_git",
                side_effect=(sha1, _SUBMODULE_SHA1, head, subprocess.CompletedProcess((), 0, output, "")),
            ), patch("skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")):
                result = inspect_remote_candidates(".", request, NetworkAuthorization("anonymous-https-remote-inspection"))
            self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
            self.assertEqual(result.issues[0].location.relative_path, "sources.toml#sources[0].path")

        valid_but_failed = subprocess.CompletedProcess(
            (), 128, f"160000 commit {object_id}\tvendor/upstream\x00", "private Git error"
        )
        with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
            "skill_collection.source_update._local_git", side_effect=(sha1, _SUBMODULE_SHA1, head, valid_but_failed)
        ), patch("skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")):
            result = inspect_remote_candidates(".", request, NetworkAuthorization("anonymous-https-remote-inspection"))
        self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")

    def test_collection_head_requires_one_exact_lowercase_object_id_record(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{
            "id": "upstream", "kind": "git-submodule", "path": "vendor/upstream",
            "url": "https://example.invalid/upstream.git",
        }])
        sha1 = subprocess.CompletedProcess((), 0, "sha1\n", "")
        malformed = (
            subprocess.CompletedProcess((), 0, "A" * 40 + "\n", ""),
            subprocess.CompletedProcess((), 0, "a" * 40, ""),
            subprocess.CompletedProcess((), 0, "a" * 40 + "\r\n", ""),
            subprocess.CompletedProcess((), 0, "a" * 40 + "\n" + "b" * 40 + "\n", ""),
            subprocess.CompletedProcess((), 128, "a" * 40 + "\n", "private Git error"),
        )
        request = (RemoteCandidateRequest("upstream", "refs/heads/main"),)
        for head in malformed:
            with self.subTest(head=head), patch(
                "skill_collection.source_update._validate_source_collection", return_value=state
            ), patch("skill_collection.source_update._local_git", side_effect=(sha1, _SUBMODULE_SHA1, head)), patch(
                "skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")
            ):
                result = inspect_remote_candidates(
                    ".", request, NetworkAuthorization("anonymous-https-remote-inspection")
                )
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
            self.assertEqual(result.issues[0].location, InspectionLocation("collection", None, "."))

    def test_gitlink_lookup_is_bound_to_the_captured_collection_head(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{
            "id": "upstream", "kind": "git-submodule", "path": "vendor/upstream",
            "url": "https://example.invalid/upstream.git",
        }])
        captured_head = "a" * 40
        observed_treeish: list[str] = []

        def local_git(repository: object, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args == ("rev-parse", "--show-object-format"):
                return subprocess.CompletedProcess((), 0, "sha1\n", "")
            if args == ("rev-parse", "--show-prefix", "--show-object-format"):
                return _SUBMODULE_SHA1
            if args[:2] == ("rev-parse", "--verify"):
                return subprocess.CompletedProcess((), 0, captured_head + "\n", "")
            observed_treeish.append(args[2])
            revision = "1" * 40 if args[2] == captured_head else "9" * 40
            return subprocess.CompletedProcess((), 0, f"160000 commit {revision}\tvendor/upstream\x00", "")

        remote = subprocess.CompletedProcess((), 0, "2" * 40 + "\trefs/heads/main\n", "")
        with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
            "skill_collection.source_update._local_git", side_effect=local_git
        ), patch("skill_collection.source_update._run_remote_git", return_value=remote):
            result = inspect_remote_candidates(
                ".", (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                NetworkAuthorization("anonymous-https-remote-inspection"),
            )
        self.assertEqual(result.status, "ready")
        self.assertEqual(observed_treeish, [captured_head])
        self.assertEqual(result.comparisons[0].current_revision, "1" * 40)

    def test_authorization_blocks_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "skill_collection.source_update._run_git",
            side_effect=AssertionError("Git must not run"),
        ):
            result = inspect_remote_candidates(Path(directory), (), None)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.network_not_authorized")

    def test_public_seam_rejects_non_tuple_and_non_request_values_before_filesystem_work(self) -> None:
        authorization = NetworkAuthorization("anonymous-https-remote-inspection")

        class RequestTuple(tuple):
            pass

        invalid = (
            [RemoteCandidateRequest("upstream", "refs/heads/main")],
            (object(),),
            RequestTuple((RemoteCandidateRequest("upstream", "refs/heads/main"),)),
        )
        for requests in invalid:
            with self.subTest(requests=type(requests).__name__), patch(
                "skill_collection.source_update.Path.is_dir",
                side_effect=AssertionError("filesystem must not run"),
            ):
                result = inspect_remote_candidates(".", requests, authorization)  # type: ignore[arg-type]
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.issues[0].code, "source-update.request_invalid")

    def test_source_only_validation_does_not_leak_catalog_group_or_profile_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vendor/upstream").mkdir(parents=True)
            source_text = (
                'version = 1\n\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/upstream"\nurl = "https://example.invalid/upstream.git"\n'
            )
            (root / "sources.toml").write_text(source_text, encoding="utf-8")
            head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
            committed_sources = subprocess.CompletedProcess((), 0, source_text, "")
            gitlink = subprocess.CompletedProcess((), 0, "160000 commit " + "1" * 40 + "\tvendor/upstream\x00", "")
            remote = subprocess.CompletedProcess((), 0, "2" * 40 + "\trefs/heads/main\n", "")
            with patch("skill_collection.validation.subprocess.run", side_effect=AssertionError("generic validation must not run")), patch(
                "skill_collection.source_update._local_git", side_effect=(_SUBMODULE_SHA1, _SUBMODULE_SHA1, head, committed_sources, gitlink)
            ), patch("skill_collection.source_update._run_remote_git", return_value=remote):
                result = inspect_remote_candidates(
                    root,
                    (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.issues, ())

    def test_source_validation_failures_map_only_to_approved_issue_taxonomy(self) -> None:
        request = (RemoteCandidateRequest("upstream", "refs/heads/main"),)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = inspect_remote_candidates(root, request, NetworkAuthorization("anonymous-https-remote-inspection"))
            self.assertEqual((result.issues[0].code, result.issues[0].location.relative_path), ("source-update.source_missing", "sources.toml"))
            (root / "sources.toml").write_text(
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\npath = "../outside"\nurl = "https://example.invalid/upstream.git"\n',
                encoding="utf-8",
            )
            result = inspect_remote_candidates(root, request, NetworkAuthorization("anonymous-https-remote-inspection"))
            self.assertEqual((result.issues[0].code, result.issues[0].location.relative_path), ("source-update.current_pin_unavailable", "sources.toml#sources[0].path"))

    def test_invalid_utf8_source_document_is_a_normalized_source_missing_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sources.toml").write_bytes(b"version = 1\n\xff")
            result = inspect_remote_candidates(
                root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                NetworkAuthorization("anonymous-https-remote-inspection"),
            )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.source_missing")
        self.assertEqual(result.issues[0].location.relative_path, "sources.toml")

    def test_boolean_source_document_version_is_rejected_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vendor/upstream").mkdir(parents=True)
            (root / "sources.toml").write_text(
                'version = true\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/upstream"\nurl = "https://example.invalid/upstream.git"\n',
                encoding="utf-8",
            )
            with patch("skill_collection.source_update._local_git", side_effect=AssertionError("Git must not run")):
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.source_missing")

    def test_local_git_output_uses_strict_utf8_and_decode_failure_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vendor/upstream").mkdir(parents=True)
            (root / "sources.toml").write_text(
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/upstream"\nurl = "https://example.invalid/upstream.git"\n',
                encoding="utf-8",
            )
            invalid_utf8 = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            with patch("skill_collection.source_update.subprocess.run", side_effect=invalid_utf8) as runner:
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
        self.assertEqual(result.issues[0].location, InspectionLocation("collection", None, "."))
        self.assertEqual(runner.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(runner.call_args.kwargs["errors"], "strict")

    def test_plain_selected_directory_cannot_inherit_the_parent_repository_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run((_test_git_executable(), "init", "-q", str(root)), check=True)
            (root / "vendor/upstream").mkdir(parents=True)
            subprocess.run((_test_git_executable(), "init", "-q", str(root / "vendor/upstream")), check=True)
            (root / "tracked").write_text("collection\n", encoding="utf-8")
            subprocess.run((_test_git_executable(), "-C", str(root), "add", "tracked"), check=True)
            subprocess.run((
                _test_git_executable(), "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "fixture",
            ), check=True)
            (root / "sources.toml").write_text(
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/upstream"\nurl = "https://example.invalid/upstream.git"\n',
                encoding="utf-8",
            )
            subprocess.run((_test_git_executable(), "-C", str(root), "add", "sources.toml"), check=True)
            subprocess.run((
                _test_git_executable(), "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "sources",
            ), check=True)
            with patch("skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")):
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
        self.assertEqual(result.issues[0].location.relative_path, "sources.toml#sources[0].path")

    def test_nested_collection_directory_cannot_inherit_a_parent_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent, nested = Path(directory), Path(directory) / "nested"
            subprocess.run((_test_git_executable(), "init", "-q", str(parent)), check=True)
            nested.mkdir()
            (nested / "vendor/upstream").mkdir(parents=True)
            (nested / "sources.toml").write_text(
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/upstream"\nurl = "https://example.invalid/upstream.git"\n',
                encoding="utf-8",
            )
            with patch("skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")):
                result = inspect_remote_candidates(
                    nested, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
        self.assertEqual(result.issues[0].location.relative_path, ".")

    def test_dirty_sources_document_cannot_mix_worktree_url_with_committed_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run((_test_git_executable(), "init", "-q", str(root)), check=True)
            subprocess.run((_test_git_executable(), "init", "-q", str(root / "vendor/upstream")), check=True)
            sources = (
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/upstream"\nurl = "https://example.invalid/original.git"\n'
            )
            (root / "sources.toml").write_text(sources, encoding="utf-8")
            subprocess.run((_test_git_executable(), "-C", str(root), "add", "sources.toml"), check=True)
            subprocess.run((
                _test_git_executable(), "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "fixture",
            ), check=True)
            (root / "sources.toml").write_text(
                sources.replace("original.git", "dirty-secret.git"), encoding="utf-8"
            )
            with patch("skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")):
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
        self.assertEqual(result.issues[0].location.relative_path, ".")

    def test_real_gitlink_lookup_is_literal_and_nul_delimited_for_utf8_special_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = "vendor/café[one]"
            source_repository = root / source_path
            source_repository.mkdir(parents=True)
            git = _test_git_executable()
            subprocess.run((git, "init", "-q", str(root)), check=True)
            subprocess.run((git, "init", "-q", str(source_repository)), check=True)
            source_text = (
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                f'path = "{source_path}"\nurl = "https://example.invalid/upstream.git"\n'
            )
            (root / "sources.toml").write_text(source_text, encoding="utf-8")
            current = "1" * 40
            subprocess.run((git, "-C", str(root), "add", "sources.toml"), check=True)
            subprocess.run((
                git, "-C", str(root), "update-index", "--add", "--info-only",
                "--cacheinfo", f"160000,{current},{source_path}",
            ), check=True)
            subprocess.run((
                git, "-C", str(root), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "fixture",
            ), check=True)
            remote = subprocess.CompletedProcess(
                (), 0, "2" * 40 + "\trefs/heads/main\n", "",
            )
            with patch("skill_collection.source_update._run_remote_git", return_value=remote):
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.comparisons[0].current_revision, current)

    def test_selected_source_symlink_escape_blocks_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root, outside = parent / "collection", parent / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "vendor").mkdir()
            (root / "vendor/upstream").symlink_to(outside, target_is_directory=True)
            (root / "sources.toml").write_text(
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/upstream"\nurl = "https://example.invalid/upstream.git"\n',
                encoding="utf-8",
            )
            with patch("skill_collection.source_update._local_git", side_effect=AssertionError("Git must not run")):
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
        self.assertEqual(result.issues[0].location.relative_path, "sources.toml#sources[0].path")

    def test_selected_source_path_control_characters_block_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sources.toml").write_text(
                'version = 1\n[[sources]]\nid = "upstream"\nkind = "git-submodule"\n'
                'path = "vendor/\\u0000upstream"\nurl = "https://example.invalid/upstream.git"\n',
                encoding="utf-8",
            )
            with patch(
                "skill_collection.source_update._local_git",
                side_effect=AssertionError("Git must not run"),
            ):
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
        self.assertEqual(result.issues[0].location.relative_path, "sources.toml#sources[0].path")

    def test_unselected_invalid_sources_are_outside_the_selected_source_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vendor/upstream").mkdir(parents=True)
            source_text = (
                'version = 1\n'
                '[[sources]]\nid = "ignored"\nkind = "unsupported"\npath = "../outside"\nurl = "file:///private"\n'
                '[[sources]]\nid = "upstream"\nkind = "git-submodule"\npath = "vendor/upstream"\nurl = "https://example.invalid/upstream.git"\n'
            )
            (root / "sources.toml").write_text(source_text, encoding="utf-8")
            head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
            committed_sources = subprocess.CompletedProcess((), 0, source_text, "")
            gitlink = subprocess.CompletedProcess((), 0, "160000 commit " + "1" * 40 + "\tvendor/upstream\x00", "")
            remote = subprocess.CompletedProcess((), 0, "2" * 40 + "\trefs/heads/main\n", "")
            with patch("skill_collection.source_update._local_git", side_effect=(_SUBMODULE_SHA1, _SUBMODULE_SHA1, head, committed_sources, gitlink)), patch(
                "skill_collection.source_update._run_remote_git", return_value=remote
            ):
                result = inspect_remote_candidates(
                    root, (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
        self.assertEqual(result.status, "ready")

    def test_invalid_selected_sources_are_deterministic_under_request_permutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sources.toml").write_text(
                'version = 1\n'
                '[[sources]]\nid = "beta"\nkind = "git-submodule"\npath = "../outside"\nurl = "https://example.invalid/beta.git"\n'
                '[[sources]]\nid = "alpha"\nkind = "embedded"\npath = "vendor/alpha"\nurl = "https://example.invalid/alpha.git"\n',
                encoding="utf-8",
            )
            requests = (
                RemoteCandidateRequest("beta", "refs/heads/main"),
                RemoteCandidateRequest("alpha", "refs/heads/main"),
            )
            results = tuple(
                inspect_remote_candidates(
                    root, order, NetworkAuthorization("anonymous-https-remote-inspection")
                )
                for order in (requests, tuple(reversed(requests)))
            )
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].issues[0].code, "source-update.source_not_external")
        self.assertEqual(results[0].issues[0].location.relative_path, "sources.toml#sources[1]")

    def test_public_constructors_enforce_authorization_ids_order_and_normalization(self) -> None:
        with self.assertRaises(ValueError):
            NetworkAuthorization("broader")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            RemoteCandidateRequest("UPSTREAM", "refs/heads/main")
        issue = InspectionIssue(
            "source-update.network_not_authorized",
            "Explicit anonymous HTTPS remote inspection authorization is required.",
            InspectionLocation("collection", None, "."),
        )
        comparison = RemoteCandidateComparison(
            "z", InspectionLocation("source", "z", "."), "refs/heads/main",
            "1" * 40, "2" * 40, "unverified",
        )
        with self.assertRaises(ValueError):
            RemoteCandidateInspection("ready", "sha256:" + "a" * 64, (comparison,), (issue,))
        with self.assertRaises(ValueError):
            RemoteCandidateInspection("blocked", "sha256:" + "a" * 64, (), (issue,))

    def test_remote_failures_use_selected_source_locations(self) -> None:
        state = SimpleNamespace(
            issues=(),
            sources=[{"id": "upstream", "kind": "git-submodule", "path": "vendor/upstream", "url": "https://example.invalid/upstream.git"}],
        )
        object_format = subprocess.CompletedProcess((), 0, "sha1\n", "")
        head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
        gitlink = subprocess.CompletedProcess((), 0, "160000 commit " + "a" * 40 + "\tvendor/upstream\x00", "")
        for diagnostic in ("Authentication failed", "The requested URL returned error: 403"):
            with self.subTest(diagnostic=diagnostic), patch(
                "skill_collection.source_update._validate_source_collection", return_value=state
            ), patch(
                "skill_collection.source_update._local_git", side_effect=(object_format, _SUBMODULE_SHA1, head, gitlink)
            ), patch(
                "skill_collection.source_update._run_remote_git",
                return_value=subprocess.CompletedProcess((), 128, "", diagnostic),
            ):
                result = inspect_remote_candidates(
                    ".", (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
            self.assertEqual(result.issues[0].code, "source-update.credentials_required")
            self.assertEqual(result.issues[0].location.relative_path, "sources.toml#sources[0].url")

    def test_ls_tree_execution_failures_are_rooted_current_pin_blocks(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{
            "id": "upstream", "kind": "git-submodule", "path": "vendor/upstream",
            "url": "https://example.invalid/upstream.git",
        }])
        sha1 = subprocess.CompletedProcess((), 0, "sha1\n", "")
        head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
        for failure in (OSError("private path"), subprocess.TimeoutExpired(("git",), 1)):
            with self.subTest(failure=type(failure).__name__), patch(
                "skill_collection.source_update._validate_source_collection", return_value=state
            ), patch("skill_collection.source_update._local_git", side_effect=(sha1, _SUBMODULE_SHA1, head, failure)), patch(
                "skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")
            ):
                result = inspect_remote_candidates(
                    ".", (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
            self.assertEqual(result.issues[0].code, "source-update.current_pin_unavailable")
            self.assertEqual(result.issues[0].location.relative_path, "sources.toml#sources[0].path")

    def test_mutation_isolation_and_request_order_matrix(self) -> None:
        state = SimpleNamespace(
            issues=(),
            sources=[
                {"id": "zzz", "kind": "git-submodule", "path": "vendor/zzz", "url": "https://example.invalid/zzz.git"},
                {"id": "aaa", "kind": "git-submodule", "path": "vendor/aaa", "url": "https://example.invalid/aaa.git"},
            ],
        )
        head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
        gitlinks = {
            "vendor/aaa": subprocess.CompletedProcess((), 0, "160000 commit " + "1" * 40 + "\tvendor/aaa\x00", ""),
            "vendor/zzz": subprocess.CompletedProcess((), 0, "160000 commit " + "2" * 40 + "\tvendor/zzz\x00", ""),
        }
        local_commands = []

        def local_git(repository, *args, **kwargs):
            local_commands.append(args)
            if args == ("rev-parse", "--show-object-format"):
                return subprocess.CompletedProcess((), 0, "sha1\n", "")
            if args == ("rev-parse", "--show-prefix", "--show-object-format"):
                return _SUBMODULE_SHA1
            return head if args[:2] == ("rev-parse", "--verify") else gitlinks[args[-1].removeprefix(":(literal)")]

        def remote(url, ref):
            value = "3" if "aaa" in url else "4"
            return subprocess.CompletedProcess((), 0, value * 40 + "\t" + ref + "\n", "")

        requests = (RemoteCandidateRequest("zzz", "refs/heads/main"), RemoteCandidateRequest("aaa", "refs/heads/main"))
        with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
            "skill_collection.source_update._local_git", side_effect=local_git
        ), patch("skill_collection.source_update._run_remote_git", side_effect=remote):
            result = inspect_remote_candidates(".", requests, NetworkAuthorization("anonymous-https-remote-inspection"))
        self.assertEqual([item.source_id for item in result.comparisons], ["aaa", "zzz"])
        self.assertTrue(all(command[0] in {"rev-parse", "ls-tree"} for command in local_commands))

    def test_read_only_snapshot_matrix_preserves_git_worktree_config_and_environment(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{"id": "upstream", "kind": "git-submodule", "path": "vendor/upstream", "url": "https://example.invalid/upstream.git"}])
        def snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
            return tuple(sorted((str(path.relative_to(root)), path.read_bytes()) for path in root.rglob("*") if path.is_file()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".git/refs/heads/main", ".git/objects/a", ".git/index", ".git/config", "vendor/upstream/file", "catalog.toml", "cache/x", "lock"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            before = snapshot(root)
            saved_environment = dict(__import__("os").environ)
            sha1 = subprocess.CompletedProcess((), 0, "sha1\n", "")
            head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
            gitlink = subprocess.CompletedProcess((), 0, "160000 commit " + "a" * 40 + "\tvendor/upstream\x00", "")
            remote = subprocess.CompletedProcess((), 0, "b" * 40 + "\trefs/heads/main\n", "")
            with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
                "skill_collection.source_update._local_git", side_effect=(sha1, _SUBMODULE_SHA1, head, gitlink)
            ), patch("skill_collection.source_update._run_remote_git", return_value=remote):
                result = inspect_remote_candidates(root, (RemoteCandidateRequest("upstream", "refs/heads/main"),), NetworkAuthorization("anonymous-https-remote-inspection"))
            self.assertEqual(result.status, "ready")
            self.assertEqual(snapshot(root), before)
            self.assertEqual(dict(__import__("os").environ), saved_environment)

    def test_object_format_and_pin_location_matrix(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{"id": "upstream", "kind": "git-submodule", "path": "vendor/upstream", "url": "https://example.invalid/upstream.git"}])
        request = (RemoteCandidateRequest("upstream", "refs/heads/main"),)
        authorization = NetworkAuthorization("anonymous-https-remote-inspection")
        cases = (
            ((OSError("private"),), "source-update.current_pin_unavailable", "."),
            ((subprocess.CompletedProcess((), 0, "sha256\n", ""),), "source-update.object_format_unsupported", "."),
            ((subprocess.CompletedProcess((), 0, "sha1\n", ""), OSError("private")), "source-update.current_pin_unavailable", "sources.toml#sources[0].path"),
            ((subprocess.CompletedProcess((), 0, "sha1\n", ""), subprocess.CompletedProcess((), 0, "\nsha256\n", "")), "source-update.object_format_unsupported", "sources.toml#sources[0].path"),
            ((subprocess.CompletedProcess((), 0, "sha1\n", ""), _SUBMODULE_SHA1, OSError("private")), "source-update.current_pin_unavailable", "."),
            ((subprocess.CompletedProcess((), 0, "sha1\n", ""), _SUBMODULE_SHA1, subprocess.CompletedProcess((), 0, "a" * 40 + "\n", ""), subprocess.CompletedProcess((), 0, "", "")), "source-update.current_pin_unavailable", "sources.toml#sources[0].path"),
        )
        for side_effect, code, path in cases:
            with self.subTest(code=code, path=path), patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
                "skill_collection.source_update._local_git", side_effect=side_effect
            ):
                result = inspect_remote_candidates(".", request, authorization)
            self.assertEqual((result.issues[0].code, result.issues[0].location.relative_path), (code, path))

    def test_unknown_malformed_and_mixed_source_object_formats_block_before_remote(self) -> None:
        sources = [
            {"id": "alpha", "kind": "git-submodule", "path": "vendor/alpha", "url": "https://example.invalid/alpha.git"},
            {"id": "beta", "kind": "git-submodule", "path": "vendor/beta", "url": "https://example.invalid/beta.git"},
        ]
        requests = (RemoteCandidateRequest("beta", "refs/heads/main"), RemoteCandidateRequest("alpha", "refs/heads/main"))
        authorization = NetworkAuthorization("anonymous-https-remote-inspection")
        for formats, path in (("sha512\n", "."), ("sha1\nextra\n", "."), (("sha1\n", "\nsha1\n", "\nsha256\n"), "sources.toml#sources[1].path")):
            with self.subTest(formats=formats), patch("skill_collection.source_update._validate_source_collection", return_value=SimpleNamespace(issues=(), sources=sources)), patch(
                "skill_collection.source_update._local_git", side_effect=(
                    (subprocess.CompletedProcess((), 0, formats, ""),)
                    if isinstance(formats, str) else tuple(subprocess.CompletedProcess((), 0, item, "") for item in formats)
                )
            ), patch("skill_collection.source_update._run_remote_git", side_effect=AssertionError("network must not run")):
                result = inspect_remote_candidates(".", requests, authorization)
            self.assertEqual(result.issues[0].code, "source-update.object_format_unsupported")
            self.assertEqual(result.issues[0].location.relative_path, path)

    def test_incomplete_remote_cleanup_cannot_be_classified_as_remote_unavailable(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{"id": "upstream", "kind": "git-submodule", "path": "vendor/upstream", "url": "https://example.invalid/upstream.git"}])
        completed = subprocess.CompletedProcess((), 0, "sha1\n", "")
        head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
        gitlink = subprocess.CompletedProcess((), 0, "160000 commit " + "a" * 40 + "\tvendor/upstream\x00", "")
        timeout = subprocess.TimeoutExpired(("git",), 1)
        timeout.remote_cleanup_evidence = object()  # type: ignore[attr-defined]
        with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
            "skill_collection.source_update._local_git", side_effect=(completed, _SUBMODULE_SHA1, head, gitlink)
        ), patch("skill_collection.source_update._run_remote_git", side_effect=timeout):
            with self.assertRaises(_RemoteCleanupIncomplete):
                inspect_remote_candidates(".", (RemoteCandidateRequest("upstream", "refs/heads/main"),), NetworkAuthorization("anonymous-https-remote-inspection"))

    def test_remote_output_bounds_and_encoding_failures_use_sanitized_blocked_families(self) -> None:
        state = SimpleNamespace(issues=(), sources=[{
            "id": "upstream", "kind": "git-submodule", "path": "vendor/upstream",
            "url": "https://example.invalid/upstream.git",
        }])
        sha1 = subprocess.CompletedProcess((), 0, "sha1\n", "")
        head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
        gitlink = subprocess.CompletedProcess((), 0, "160000 commit " + "a" * 40 + "\tvendor/upstream\x00", "")
        failures = (
            (_RemoteLifecycleFailure("private oversized output"), "source-update.remote_unavailable", "sources.toml#sources[0].url"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private bytes"), "source-update.remote_response_invalid", "sources.toml#sources[0]"),
        )
        for failure, code, path in failures:
            with self.subTest(code=code), patch(
                "skill_collection.source_update._validate_source_collection", return_value=state
            ), patch("skill_collection.source_update._local_git", side_effect=(sha1, _SUBMODULE_SHA1, head, gitlink)), patch(
                "skill_collection.source_update._run_remote_git", side_effect=failure
            ):
                result = inspect_remote_candidates(
                    ".", (RemoteCandidateRequest("upstream", "refs/heads/main"),),
                    NetworkAuthorization("anonymous-https-remote-inspection"),
                )
            self.assertEqual(result.status, "blocked")
            self.assertEqual((result.issues[0].code, result.issues[0].location.relative_path), (code, path))
            self.assertNotIn("private", result.issues[0].message)


class RemoteInspectionCliTests(unittest.TestCase):
    def test_usage_requires_allow_network_and_uses_new_command(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["inspect-source-candidates", "--source", "upstream=refs/heads/main"],
            stdout=stdout, stderr=stderr,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("--allow-network", stderr.getvalue())

    def test_json_output_is_the_remote_inspection_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["inspect-source-candidates", "--collection-root", directory,
                 "--source", "upstream=refs/heads/main", "--allow-network"],
                stdout=stdout, stderr=stderr,
            )
        self.assertIn(code, (1, 3))
        if stdout.getvalue():
            document = json.loads(stdout.getvalue())
            self.assertEqual(document["command"], "inspect-source-candidates")

    def test_exact_ready_and_blocked_documents(self) -> None:
        comparison = RemoteCandidateComparison(
            "upstream", InspectionLocation("source", "upstream", "."),
            "refs/heads/main", "1" * 40, "2" * 40, "unverified",
        )
        ready = RemoteCandidateInspection("ready", "sha256:" + "a" * 64, (comparison,), ())
        self.assertEqual(
            json_document("inspect-source-candidates", ready),
            "{\n"
            "  \"command\": \"inspect-source-candidates\",\n"
            "  \"result\": {\n"
            "    \"comparisons\": [\n"
            "      {\n"
            "        \"candidate_revision\": \"2222222222222222222222222222222222222222\",\n"
            "        \"current_revision\": \"1111111111111111111111111111111111111111\",\n"
            "        \"relationship\": \"unverified\",\n"
            "        \"remote_ref\": \"refs/heads/main\",\n"
            "        \"source_id\": \"upstream\",\n"
            "        \"source_location\": {\n"
            "          \"label\": \"upstream\",\n"
            "          \"relative_path\": \".\",\n"
            "          \"root\": \"source\"\n"
            "        }\n"
            "      }\n"
            "    ],\n"
            "    \"inspection_id\": \"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\n"
            "    \"issues\": [],\n"
            "    \"status\": \"ready\"\n"
            "  },\n"
            "  \"schema_version\": 1\n"
            "}\n",
        )
        self.assertEqual(
            inspection_text(ready),
            "Remote candidate inspection: ready\n"
            "Inspection ID: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n\n"
            "Sources (1):\n"
            "- upstream: 1111111111111111111111111111111111111111 -> 2222222222222222222222222222222222222222\n"
            "  Ref: refs/heads/main\n"
            "  Relationship: unverified\n\n"
            "Issues (0):\nNone.\n",
        )
        issue = InspectionIssue("source-update.network_not_authorized", "Explicit anonymous HTTPS remote inspection authorization is required.", InspectionLocation("collection", None, "."))
        blocked = RemoteCandidateInspection("blocked", None, (), (issue,))
        self.assertEqual(inspection_text(blocked), "Remote candidate inspection: blocked\nInspection ID: -\n\nSources (0):\nNone.\n\nIssues (1):\n1. [source-update.network_not_authorized] Explicit anonymous HTTPS remote inspection authorization is required.\n   Location: collection:.\n")

    def test_cli_unexpected_and_interruption_are_sanitized(self) -> None:
        with patch("skill_collection.cli.inspect_remote_candidates", side_effect=RuntimeError("secret platform detail")):
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"],
                stdout=stdout, stderr=stderr,
            )
        self.assertEqual(code, 3)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("secret", stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "system.unexpected")
        with patch("skill_collection.cli.inspect_remote_candidates", side_effect=KeyboardInterrupt()):
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"],
                stdout=stdout, stderr=stderr,
            )
        self.assertEqual(code, 130)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
