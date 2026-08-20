from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
import json
import io
import tempfile
import subprocess
from unittest.mock import patch
from types import SimpleNamespace

from skill_collection import (
    InspectionIssue, InspectionLocation, NetworkAuthorization,
    RemoteCandidateComparison, RemoteCandidateInspection, RemoteCandidateRequest,
    inspect_remote_candidates,
)
from skill_collection.cli import main
from skill_collection.source_update import _CleanupEvidence, _RemoteCleanupIncomplete, _identity, _inspection_id
from skill_collection.output import inspection_text, json_document

_SUBMODULE_SHA1 = subprocess.CompletedProcess((), 0, "\nsha1\n", "")


class RemoteInspectionContractTests(unittest.TestCase):
    def test_public_python_identity_inclusion_and_exclusion_matrix(self) -> None:
        authorization = NetworkAuthorization("anonymous-https-remote-inspection")

        def inspect(*, source_id: str = "upstream", url: str = "https://example.invalid/upstream.git", remote_ref: str = "refs/heads/main", current: str = "1" * 40, candidate: str = "2" * 40, head: str = "a" * 40, path: str = "vendor/upstream", raw_stderr: str = "") -> RemoteCandidateInspection:
            source = {"id": source_id, "kind": "git-submodule", "path": path, "url": url}
            state = SimpleNamespace(issues=(), sources=[source])

            def local_git(repository: object, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args == ("rev-parse", "--show-object-format"):
                    return subprocess.CompletedProcess((), 0, "sha1\n", "")
                if args == ("rev-parse", "--show-prefix", "--show-object-format"):
                    return _SUBMODULE_SHA1
                if args[:2] == ("rev-parse", "--verify"):
                    return subprocess.CompletedProcess((), 0, head + "\n", "")
                return subprocess.CompletedProcess((), 0, f"160000 commit {current}\t{path}\x00", "")

            remote = subprocess.CompletedProcess((), 0, f"{candidate}\t{remote_ref}\n", raw_stderr)
            with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
                "skill_collection.source_update._local_git", side_effect=local_git
            ), patch("skill_collection.source_update._run_remote_git", return_value=remote):
                return inspect_remote_candidates(".", (RemoteCandidateRequest(source_id, remote_ref),), authorization)

        baseline = inspect()
        self.assertEqual(baseline.status, "ready")
        included = (
            {"source_id": "alternate"}, {"url": "https://example.invalid/alternate.git"},
            {"remote_ref": "refs/heads/alternate"}, {"current": "3" * 40},
            {"candidate": "4" * 40}, {"head": "b" * 40},
        )
        for change in included:
            with self.subTest(included=change):
                self.assertNotEqual(inspect(**change).inspection_id, baseline.inspection_id)
        for change in ({"path": "vendor/relocated"}, {"raw_stderr": "ignored diagnostic timing pid=99"}):
            with self.subTest(excluded=change):
                self.assertEqual(inspect(**change).inspection_id, baseline.inspection_id)

    def test_public_python_and_cli_request_response_traversal_shuffle_determinism(self) -> None:
        sources = (
            {"id": "alpha", "kind": "git-submodule", "path": "vendor/alpha", "url": "https://example.invalid/alpha.git"},
            {"id": "beta", "kind": "git-submodule", "path": "vendor/beta", "url": "https://example.invalid/beta.git"},
        )
        requests = (
            RemoteCandidateRequest("alpha", "refs/heads/main"),
            RemoteCandidateRequest("beta", "refs/heads/stable"),
        )
        currents = {"vendor/alpha": "1" * 40, "vendor/beta": "2" * 40}
        candidates = {"alpha": "3" * 40, "beta": "4" * 40}

        def local_git(repository: object, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args == ("rev-parse", "--show-object-format"):
                return subprocess.CompletedProcess((), 0, "sha1\n", "")
            if args == ("rev-parse", "--show-prefix", "--show-object-format"):
                return _SUBMODULE_SHA1
            if args[:2] == ("rev-parse", "--verify"):
                return subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
            path = args[-1].removeprefix(":(literal)")
            return subprocess.CompletedProcess((), 0, f"160000 commit {currents[path]}\t{path}\x00", "")

        def remote(url: str, ref: str) -> subprocess.CompletedProcess[str]:
            source_id = "alpha" if "alpha" in url else "beta"
            return subprocess.CompletedProcess((), 0, f"{candidates[source_id]}\t{ref}\n", "ignored timing")

        python_results = []
        cli_documents: dict[str, list[str]] = {"json": [], "text": []}
        for source_order, request_order in ((sources, requests), (tuple(reversed(sources)), tuple(reversed(requests)))):
            state = SimpleNamespace(issues=(), sources=list(source_order))
            with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
                "skill_collection.source_update._local_git", side_effect=local_git
            ), patch("skill_collection.source_update._run_remote_git", side_effect=remote):
                python_results.append(inspect_remote_candidates(".", request_order, NetworkAuthorization("anonymous-https-remote-inspection")))
            for output_format in ("json", "text"):
                stdout, stderr = io.StringIO(), io.StringIO()
                argv = ["inspect-source-candidates", "--allow-network", "--format", output_format]
                for request in request_order:
                    argv.extend(("--source", f"{request.source_id}={request.remote_ref}"))
                with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
                    "skill_collection.source_update._local_git", side_effect=local_git
                ), patch("skill_collection.source_update._run_remote_git", side_effect=remote):
                    self.assertEqual(main(argv, stdout=stdout, stderr=stderr), 0)
                self.assertEqual(stderr.getvalue(), "")
                cli_documents[output_format].append(stdout.getvalue())
        self.assertEqual(python_results[0], python_results[1])
        self.assertEqual(python_results[0].inspection_id, python_results[1].inspection_id)
        self.assertEqual(cli_documents["json"][0], cli_documents["json"][1])
        self.assertEqual(cli_documents["text"][0], cli_documents["text"][1])

    def test_locations_and_issues_are_immutable_and_rooted(self) -> None:
        location = InspectionLocation("collection", None, ".")
        issue = InspectionIssue(
            "source-update.network_not_authorized",
            "Explicit anonymous HTTPS remote inspection authorization is required.",
            location,
        )
        with self.assertRaises(FrozenInstanceError):
            issue.code = "changed"  # type: ignore[misc]
        self.assertEqual(issue.related_locations, ())
        with self.assertRaises(ValueError):
            InspectionLocation("collection", "unexpected", ".")  # type: ignore[arg-type]

    def test_authorization_and_status_require_exact_builtin_string_literals(self) -> None:
        class Spoof:
            def __eq__(self, other: object) -> bool:
                return True

        class StringSubclass(str):
            pass

        issue = InspectionIssue(
            "source-update.network_not_authorized",
            "Explicit anonymous HTTPS remote inspection authorization is required.",
            InspectionLocation("collection", None, "."),
        )
        for value in (Spoof(), StringSubclass("anonymous-https-remote-inspection")):
            with self.subTest(authorization=type(value).__name__), self.assertRaises(ValueError):
                NetworkAuthorization(value)  # type: ignore[arg-type]
        for value in (Spoof(), StringSubclass("blocked")):
            with self.subTest(status=type(value).__name__), self.assertRaises(ValueError):
                RemoteCandidateInspection(value, None, (), (issue,))  # type: ignore[arg-type]

        class RelationshipSpoof:
            def __eq__(self, other: object) -> bool:
                return other == "unchanged"

        for value in (RelationshipSpoof(), StringSubclass("unchanged")):
            with self.subTest(relationship=type(value).__name__), self.assertRaises(ValueError):
                RemoteCandidateComparison(
                    "upstream", InspectionLocation("source", "upstream", "."),
                    "refs/heads/main", "1" * 40, "1" * 40, value,  # type: ignore[arg-type]
                )

        string_subclass_cases = (
            lambda: InspectionLocation(StringSubclass("collection"), None, "."),
            lambda: InspectionLocation("collection", None, StringSubclass(".")),
            lambda: InspectionLocation("source", StringSubclass("upstream"), "."),
            lambda: RemoteCandidateRequest(StringSubclass("upstream"), "refs/heads/main"),
            lambda: RemoteCandidateRequest("upstream", StringSubclass("refs/heads/main")),
            lambda: RemoteCandidateComparison(
                StringSubclass("upstream"), InspectionLocation("source", "upstream", "."),
                "refs/heads/main", "1" * 40, "2" * 40, "unverified",
            ),
            lambda: RemoteCandidateComparison(
                "upstream", InspectionLocation("source", "upstream", "."),
                StringSubclass("refs/heads/main"), "1" * 40, "2" * 40, "unverified",
            ),
            lambda: RemoteCandidateComparison(
                "upstream", InspectionLocation("source", "upstream", "."),
                "refs/heads/main", StringSubclass("1" * 40), "2" * 40, "unverified",
            ),
            lambda: InspectionIssue(
                StringSubclass("source-update.network_not_authorized"),
                "Explicit anonymous HTTPS remote inspection authorization is required.",
                InspectionLocation("collection", None, "."),
            ),
            lambda: RemoteCandidateInspection(
                "ready", StringSubclass("sha256:" + "a" * 64),
                (RemoteCandidateComparison(
                    "upstream", InspectionLocation("source", "upstream", "."),
                    "refs/heads/main", "1" * 40, "2" * 40, "unverified",
                ),), (),
            ),
        )
        for construct in string_subclass_cases:
            with self.subTest(construct=construct), self.assertRaises(ValueError):
                construct()

    def test_public_model_rejects_mutable_nested_values_and_invalid_related_locations(self) -> None:
        location = InspectionLocation("collection", None, ".")
        valid_code = "source-update.network_not_authorized"
        valid_message = "Explicit anonymous HTTPS remote inspection authorization is required."
        for related in ([], ("not-a-location",), (InspectionLocation("source", "upstream", "."),)):  # type: ignore[arg-type]
            with self.subTest(related=related):
                with self.assertRaises(ValueError):
                    InspectionIssue(valid_code, valid_message, location, related)  # type: ignore[arg-type]
        for code, message, primary in (
            (1, valid_message, location),
            (valid_code, 2, location),
            (valid_code, valid_message, "not-a-location"),
            ("source-update.unknown", valid_message, location),
            (valid_code, "wrong message", location),
            (valid_code, valid_message, InspectionLocation("collection", None, "sources.toml")),
        ):
            with self.subTest(code=code, message=message, primary=primary), self.assertRaises(ValueError):
                InspectionIssue(code, message, primary)  # type: ignore[arg-type]
        for root, label, path in (("collection", None, 1), ("source", 1, ".")):
            with self.subTest(root=root, label=label, path=path), self.assertRaises(ValueError):
                InspectionLocation(root, label, path)  # type: ignore[arg-type]
        for path in ("foo//bar", "foo/", "foo/./bar", "foo\\bar", "foo\x00bar", "foo\x7fbar"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                InspectionLocation("collection", None, path)

    def test_public_issue_taxonomy_accepts_only_the_twelve_exact_contract_forms(self) -> None:
        cases = (
            ("source-update.network_not_authorized", "Explicit anonymous HTTPS remote inspection authorization is required.", "."),
            ("source-update.request_invalid", "A Source request or full branch ref is malformed.", "sources.toml"),
            ("source-update.request_duplicate", "A Source identity was requested more than once.", "sources.toml"),
            ("source-update.source_missing", "The requested Source does not exist.", "sources.toml"),
            ("source-update.source_not_external", "The requested Source is not a native Git submodule Source.", "sources.toml#sources[0]"),
            ("source-update.remote_transport_unsupported", "The Source URL is outside the anonymous HTTPS transport policy.", "sources.toml#sources[0].url"),
            ("source-update.current_pin_unavailable", "The committed Source gitlink pin cannot be established.", "sources.toml#sources[0].path"),
            ("source-update.object_format_unsupported", "Checkpoint 7A supports only the SHA-1 Git object format.", "."),
            ("source-update.remote_unavailable", "The exact remote could not be inspected within the required bounds.", "sources.toml#sources[0].url"),
            ("source-update.remote_response_invalid", "The remote advertisement was malformed or ambiguous.", "sources.toml#sources[0]"),
            ("source-update.remote_ref_missing", "The exact requested ref was not advertised.", "sources.toml#sources[0]"),
            ("source-update.credentials_required", "The remote rejected anonymous inspection or required credentials.", "sources.toml#sources[0].url"),
        )
        for code, message, path in cases:
            with self.subTest(code=code):
                issue = InspectionIssue(code, message, InspectionLocation("collection", None, path))
                self.assertEqual((issue.code, issue.message, issue.location.relative_path), (code, message, path))
                with self.assertRaises(ValueError):
                    InspectionIssue(code, message + " raw", issue.location)

    def test_comparison_constructor_rejects_invalid_source_ref_and_location_invariants(self) -> None:
        valid = {
            "source_id": "upstream",
            "source_location": InspectionLocation("source", "upstream", "."),
            "remote_ref": "refs/heads/main",
            "current_revision": "1" * 40,
            "candidate_revision": "2" * 40,
            "relationship": "unverified",
        }
        invalid = (
            {"source_id": "Bad_ID"},
            {"remote_ref": "refs/heads/main.lock"},
            {"remote_ref": "refs/heads/.hidden"},
            {"remote_ref": "refs/heads/has space"},
            {"source_location": InspectionLocation("source", "other", ".")},
            {"source_location": InspectionLocation("source", "upstream", "nested")},
            {"source_location": InspectionLocation("collection", None, ".")},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                RemoteCandidateComparison(**{**valid, **change})  # type: ignore[arg-type]

    def test_issue_normalization_is_complete_deterministic_and_deduplicated(self) -> None:
        network = InspectionIssue(
            "source-update.network_not_authorized",
            "Explicit anonymous HTTPS remote inspection authorization is required.",
            InspectionLocation("collection", None, "."),
        )
        request = InspectionIssue(
            "source-update.request_invalid",
            "A Source request or full branch ref is malformed.",
            InspectionLocation("collection", None, "sources.toml"),
        )
        normalized = (network, request)
        result = RemoteCandidateInspection("blocked", None, (), normalized)
        self.assertEqual(result.issues, normalized)
        with self.assertRaises(ValueError):
            RemoteCandidateInspection("blocked", None, (), tuple(reversed(normalized)))
        with self.assertRaises(ValueError):
            RemoteCandidateInspection("blocked", None, (), (network, network))

    def test_identity_is_canonical_and_sensitive_only_to_material_inputs(self) -> None:
        base = {"ref": "refs/heads/main", "candidate": "a" * 40}
        self.assertEqual(_identity(base), _identity({"candidate": "a" * 40, "ref": "refs/heads/main"}))
        self.assertNotEqual(_identity(base), _identity({**base, "candidate": "b" * 40}))

    def test_inspection_identity_input_and_order_matrix(self) -> None:
        source = {
            "source_id": "upstream", "url": "https://example.invalid/upstream.git",
            "ref": "refs/heads/main", "current": "1" * 40, "candidate": "2" * 40,
        }
        baseline = _inspection_id("a" * 40, [source])
        for field, value in (
            ("source_id", "other"), ("url", "https://example.invalid/other.git"),
            ("ref", "refs/heads/other"), ("current", "3" * 40),
            ("candidate", "4" * 40),
        ):
            with self.subTest(field=field):
                changed = dict(source)
                changed[field] = value
                self.assertNotEqual(baseline, _inspection_id("a" * 40, [changed]))
        self.assertNotEqual(baseline, _inspection_id("b" * 40, [source]))
        second = {**source, "source_id": "zzz"}
        self.assertEqual(_inspection_id("a" * 40, [source, second]), _inspection_id("a" * 40, [second, source]))
        self.assertEqual(baseline, _inspection_id("a" * 40, [dict(source)]))

    def test_identity_exclusion_and_byte_determinism_matrix(self) -> None:
        source = {
            "source_id": "upstream", "url": "https://example.invalid/upstream.git",
            "ref": "refs/heads/main", "current": "1" * 40, "candidate": "2" * 40,
        }
        baseline = _inspection_id("a" * 40, [source])
        # These values are deliberately outside the canonical identity model.
        for excluded in (
            {**source, "path": "vendor/one"},
            {**source, "raw_output": "private header"},
            {**source, "timing": "999"},
        ):
            with self.subTest(excluded=excluded):
                self.assertEqual(baseline, _inspection_id("a" * 40, [excluded]))
        comparison = RemoteCandidateComparison("upstream", InspectionLocation("source", "upstream", "."), "refs/heads/main", "1" * 40, "2" * 40, "unverified")
        ready = RemoteCandidateInspection("ready", baseline, (comparison,), ())
        self.assertEqual(json_document("inspect-source-candidates", ready), json_document("inspect-source-candidates", ready))
        self.assertEqual(inspection_text(ready), inspection_text(ready))

    def test_blocked_json_is_structured_and_keeps_stdout_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["inspect-source-candidates", "--collection-root", directory,
                 "--source", "upstream=refs/heads/main", "--allow-network"],
                stdout=stdout, stderr=stderr,
            )
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "")
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["command"], "inspect-source-candidates")
        self.assertEqual(document["result"]["status"], "blocked")
        self.assertTrue(document["result"]["issues"])

    def test_cli_error_boundary_goldens_and_redaction_matrix(self) -> None:
        cases = (
            (
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main"],
                None, 2, "", "usage: skill-collection [-h]\n                        {scan,validate,plan,inspect-source-candidates,init-project,status,doctor,activate} ...\nskill-collection: error: --allow-network is required\n",
            ),
            (
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"],
                KeyboardInterrupt(), 130, "", "",
            ),
            (
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"],
                RuntimeError("https://user:secret@example.invalid private Git stderr"), 3, "",
                '{\n  "error": {\n    "code": "system.unexpected",\n    "message": "An unexpected system failure occurred."\n  },\n  "schema_version": 1\n}\n',
            ),
        )
        for argv, failure, expected_code, expected_stdout, expected_stderr in cases:
            with self.subTest(expected_code=expected_code):
                stdout, stderr = io.StringIO(), io.StringIO()
                if failure is None:
                    code = main(argv, stdout=stdout, stderr=stderr)
                    self.assertEqual(code, expected_code)
                    self.assertEqual(stdout.getvalue(), expected_stdout)
                    self.assertEqual(stderr.getvalue(), expected_stderr)
                else:
                    with patch("skill_collection.cli.inspect_remote_candidates", side_effect=failure):
                        code = main(argv, stdout=stdout, stderr=stderr)
                    self.assertEqual(code, expected_code)
                    self.assertEqual(stdout.getvalue(), expected_stdout)
                    self.assertEqual(stderr.getvalue(), expected_stderr)
                    self.assertNotIn("secret", stderr.getvalue())

    def test_cli_malformed_and_duplicate_request_documents_are_exact(self) -> None:
        usage = "usage: skill-collection [-h]\n                        {scan,validate,plan,inspect-source-candidates,init-project,status,doctor,activate} ...\n"
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["inspect-source-candidates", "--source", "upstream=main", "--allow-network"],
            stdout=stdout, stderr=stderr,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), usage + "skill-collection: error: --source must use SOURCE=refs/heads/BRANCH\n")

        expected_json = (
            '{\n  "command": "inspect-source-candidates",\n  "result": {\n    "comparisons": [],\n'
            '    "inspection_id": null,\n    "issues": [\n      {\n        "code": "source-update.request_duplicate",\n'
            '        "location": {\n          "label": null,\n          "relative_path": "sources.toml",\n'
            '          "root": "collection"\n        },\n        "message": "A Source identity was requested more than once.",\n'
            '        "related_locations": []\n      }\n    ],\n    "status": "blocked"\n  },\n  "schema_version": 1\n}\n'
        )
        expected_text = (
            "Remote candidate inspection: blocked\nInspection ID: -\n\nSources (0):\nNone.\n\nIssues (1):\n"
            "1. [source-update.request_duplicate] A Source identity was requested more than once.\n"
            "   Location: collection:sources.toml\n"
        )
        duplicate = [
            "inspect-source-candidates", "--allow-network",
            "--source", "upstream=refs/heads/main",
            "--source", "upstream=refs/heads/stable",
        ]
        for output_format, expected in (("json", expected_json), ("text", expected_text)):
            with self.subTest(output_format=output_format):
                stdout, stderr = io.StringIO(), io.StringIO()
                code = main([*duplicate, "--format", output_format], stdout=stdout, stderr=stderr)
                self.assertEqual(code, 1)
                self.assertEqual(stdout.getvalue(), expected)
                self.assertEqual(stderr.getvalue(), "")

    def test_cli_duplicate_singleton_options_are_exact_usage_errors(self) -> None:
        usage = "usage: skill-collection [-h]\n                        {scan,validate,plan,inspect-source-candidates,init-project,status,doctor,activate} ...\n"
        cases = (
            (["--allow-network", "--allow-network"], "--allow-network"),
            (["--format", "json", "--format", "text", "--allow-network"], "--format"),
            (["--collection-root", ".", "--collection-root", "/tmp", "--allow-network"], "--collection-root"),
        )
        for options, duplicate in cases:
            with self.subTest(duplicate=duplicate):
                stdout, stderr = io.StringIO(), io.StringIO()
                code = main(
                    ["inspect-source-candidates", "--source", "upstream=refs/heads/main", *options],
                    stdout=stdout, stderr=stderr,
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(),
                    usage + f"skill-collection: error: {duplicate} may be specified only once\n",
                )

    def test_cli_rejects_every_singleton_abbreviation_and_exact_plus_abbreviation(self) -> None:
        usage = "usage: skill-collection [-h]\n                        {scan,validate,plan,inspect-source-candidates,init-project,status,doctor,activate} ...\n"
        cases = (
            (["--allow-net"], "--allow-net"),
            (["--allow-network", "--allow-net"], "--allow-net"),
            (["--form", "json", "--allow-network"], "--form json"),
            (["--format", "json", "--form", "text", "--allow-network"], "--form text"),
            (["--collection", ".", "--allow-network"], "--collection ."),
            (["--collection-root", ".", "--collection", "/tmp", "--allow-network"], "--collection /tmp"),
        )
        for options, rejected in cases:
            with self.subTest(rejected=rejected):
                stdout, stderr = io.StringIO(), io.StringIO()
                code = main(
                    ["inspect-source-candidates", "--source", "upstream=refs/heads/main", *options],
                    stdout=stdout, stderr=stderr,
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(),
                    usage + f"skill-collection: error: unrecognized arguments: {rejected}\n",
                )

    def test_cleanup_failure_is_a_sanitized_system_boundary(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("skill_collection.cli.inspect_remote_candidates", side_effect=_RemoteCleanupIncomplete("pid=17 /private/secret")):
            code = main(
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"],
                stdout=stdout, stderr=stderr,
            )
        self.assertEqual(code, 3)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            '{\n  "error": {\n    "code": "system.cleanup_failed",\n    "message": "Remote inspection cleanup could not be confirmed."\n  },\n  "schema_version": 1\n}\n',
        )
        self.assertNotIn("secret", stderr.getvalue())

    def test_interruption_remains_exit_130_with_internal_cleanup_evidence(self) -> None:
        interruption = KeyboardInterrupt()
        interruption.remote_cleanup_evidence = object()  # type: ignore[attr-defined]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("skill_collection.cli.inspect_remote_candidates", side_effect=interruption):
            code = main(
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"],
                stdout=stdout, stderr=stderr,
            )
        self.assertEqual(code, 130)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(hasattr(interruption, "remote_cleanup_evidence"))

    def test_runner_to_cli_interruption_with_incomplete_cleanup_is_exit_130(self) -> None:
        class Stream:
            def __init__(self) -> None:
                self.closed = False

            def fileno(self) -> int:
                return 9

            def close(self) -> None:
                self.closed = True

        class Process:
            pid = 31337

            def __init__(self) -> None:
                self.stdout, self.stderr = Stream(), Stream()

            def wait(self, **kwargs: object) -> int:
                return 0

        class Selector:
            def register(self, *args: object) -> None: return None
            def get_map(self) -> dict[object, object]: return {}
            def close(self) -> None: return None

        process, selector, interruption = Process(), Selector(), KeyboardInterrupt()
        state = SimpleNamespace(issues=(), sources=[{
            "id": "upstream", "kind": "git-submodule", "path": "vendor/upstream",
            "url": "https://example.invalid/upstream.git",
        }])
        sha1 = subprocess.CompletedProcess((), 0, "sha1\n", "")
        head = subprocess.CompletedProcess((), 0, "a" * 40 + "\n", "")
        gitlink = subprocess.CompletedProcess((), 0, "160000 commit " + "a" * 40 + "\tvendor/upstream\x00", "")

        def lifecycle(stage: str, operation: object, *args: object, **kwargs: object) -> object:
            if stage == "spawn": return process
            if stage == "capture": return selector
            if stage == "wait": raise interruption
            return operation(*args, **kwargs)  # type: ignore[operator]

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("skill_collection.source_update._validate_source_collection", return_value=state), patch(
            "skill_collection.source_update._local_git", side_effect=(sha1, _SUBMODULE_SHA1, head, gitlink)
        ), patch("skill_collection.source_update._lifecycle", side_effect=lifecycle), patch(
            "skill_collection.source_update._terminate_process_group", return_value=_CleanupEvidence("verify")
        ):
            code = main(
                ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"],
                stdout=stdout, stderr=stderr,
            )
        self.assertEqual(code, 130)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(interruption.remote_cleanup_evidence.phase, "verify")  # type: ignore[attr-defined]
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_every_blocked_issue_family_has_public_json_text_and_redaction(self) -> None:
        families = (
            ("source-update.network_not_authorized", "Explicit anonymous HTTPS remote inspection authorization is required.", "."),
            ("source-update.request_invalid", "A Source request or full branch ref is malformed.", "sources.toml"),
            ("source-update.request_duplicate", "A Source identity was requested more than once.", "sources.toml"),
            ("source-update.source_missing", "The requested Source does not exist.", "sources.toml"),
            ("source-update.source_not_external", "The requested Source is not a native Git submodule Source.", "sources.toml#sources[0]"),
            ("source-update.remote_transport_unsupported", "The Source URL is outside the anonymous HTTPS transport policy.", "sources.toml#sources[0].url"),
            ("source-update.current_pin_unavailable", "The committed Source gitlink pin cannot be established.", "."),
            ("source-update.object_format_unsupported", "Checkpoint 7A supports only the SHA-1 Git object format.", "."),
            ("source-update.remote_unavailable", "The exact remote could not be inspected within the required bounds.", "sources.toml#sources[0].url"),
            ("source-update.remote_response_invalid", "The remote advertisement was malformed or ambiguous.", "sources.toml#sources[0]"),
            ("source-update.remote_ref_missing", "The exact requested ref was not advertised.", "sources.toml#sources[0]"),
            ("source-update.credentials_required", "The remote rejected anonymous inspection or required credentials.", "sources.toml#sources[0].url"),
        )
        argv = ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"]
        for code_name, message, path in families:
            with self.subTest(code=code_name):
                issue = InspectionIssue(code_name, message, InspectionLocation("collection", None, path))
                result = RemoteCandidateInspection("blocked", None, (), (issue,))
                for output_format in ("json", "text"):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch("skill_collection.cli.inspect_remote_candidates", return_value=result):
                        code = main([*argv, "--format", output_format], stdout=stdout, stderr=stderr)
                    self.assertEqual(code, 1)
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertTrue(stdout.getvalue().endswith("\n"))
                    self.assertIn(code_name, stdout.getvalue())
                    self.assertIn(message, stdout.getvalue())
                    self.assertIn(path, stdout.getvalue())
                    for forbidden in ("https://", "/private/", "Authorization:", "password", "Traceback"):
                        self.assertNotIn(forbidden, stdout.getvalue())

    def test_every_blocked_issue_family_has_exact_document_goldens(self) -> None:
        families = (
            ("source-update.network_not_authorized", "Explicit anonymous HTTPS remote inspection authorization is required.", "."),
            ("source-update.request_invalid", "A Source request or full branch ref is malformed.", "sources.toml"),
            ("source-update.request_duplicate", "A Source identity was requested more than once.", "sources.toml"),
            ("source-update.source_missing", "The requested Source does not exist.", "sources.toml"),
            ("source-update.source_not_external", "The requested Source is not a native Git submodule Source.", "sources.toml#sources[0]"),
            ("source-update.remote_transport_unsupported", "The Source URL is outside the anonymous HTTPS transport policy.", "sources.toml#sources[0].url"),
            ("source-update.current_pin_unavailable", "The committed Source gitlink pin cannot be established.", "."),
            ("source-update.object_format_unsupported", "Checkpoint 7A supports only the SHA-1 Git object format.", "."),
            ("source-update.remote_unavailable", "The exact remote could not be inspected within the required bounds.", "sources.toml#sources[0].url"),
            ("source-update.remote_response_invalid", "The remote advertisement was malformed or ambiguous.", "sources.toml#sources[0]"),
            ("source-update.remote_ref_missing", "The exact requested ref was not advertised.", "sources.toml#sources[0]"),
            ("source-update.credentials_required", "The remote rejected anonymous inspection or required credentials.", "sources.toml#sources[0].url"),
        )
        argv = ["inspect-source-candidates", "--source", "upstream=refs/heads/main", "--allow-network"]
        for name, message, path in families:
            with self.subTest(issue=name):
                result = RemoteCandidateInspection("blocked", None, (), (InspectionIssue(name, message, InspectionLocation("collection", None, path)),))
                json_golden = (
                    '{\n  "command": "inspect-source-candidates",\n  "result": {\n'
                    '    "comparisons": [],\n    "inspection_id": null,\n    "issues": [\n      {\n'
                    f'        "code": "{name}",\n        "location": {{\n          "label": null,\n          "relative_path": "{path}",\n          "root": "collection"\n        }},\n'
                    f'        "message": "{message}",\n        "related_locations": []\n      }}\n    ],\n    "status": "blocked"\n  }},\n  "schema_version": 1\n}}\n'
                )
                text_golden = (
                    "Remote candidate inspection: blocked\nInspection ID: -\n\nSources (0):\nNone.\n\nIssues (1):\n"
                    f"1. [{name}] {message}\n   Location: collection:{path}\n"
                )
                for output_format, golden in (("json", json_golden), ("text", text_golden)):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch("skill_collection.cli.inspect_remote_candidates", return_value=result):
                        exit_code = main([*argv, "--format", output_format], stdout=stdout, stderr=stderr)
                    self.assertEqual((exit_code, stdout.getvalue(), stderr.getvalue()), (1, golden, ""))
