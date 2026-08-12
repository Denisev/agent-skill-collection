from __future__ import annotations

from dataclasses import FrozenInstanceError
import errno
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint3_support import add_discovered_skills, valid_collection, write_binding
from skill_collection import Location, ValidationIssue
from skill_collection.cli import main


class ProjectStatusPublicSeamTests(unittest.TestCase):
    def test_initial_project_maps_review_exactly_and_is_immutable(self) -> None:
        from skill_collection import status

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)

            result = status(collection, project)

        self.assertEqual(result.category, "inactive")
        self.assertEqual(result.profile, "base")
        self.assertIsNotNone(result.activation_id)
        self.assertIsNotNone(result.plan_id)
        self.assertEqual(result.pending_action_count, 5)
        self.assertEqual(result.unchanged_link_count, 0)
        self.assertEqual(result.issues, ())
        self.assertEqual([item.id for item in result.recommended_commands], ["review-activation"])
        with self.assertRaises(FrozenInstanceError):
            result.category = "active"  # type: ignore[misc]

    def test_blocked_project_does_not_enrich_missing_review_fields(self) -> None:
        from skill_collection import ActivationReview, status

        issue = ValidationIssue("document.missing", "Missing.", Location("project", "skill-collection.toml"))
        review = ActivationReview("blocked", None, None, None, (), (), (), None, (issue,))
        with patch("skill_collection.inspection.prepare_activation", return_value=review) as prepare:
            result = status("/collection", "/project")

        prepare.assert_called_once()
        self.assertEqual(result.category, "blocked")
        self.assertEqual((result.profile, result.activation_id, result.plan_id), (None, None, None))
        self.assertEqual((result.pending_action_count, result.unchanged_link_count), (0, 0))
        self.assertEqual(result.issues[0].issue, issue)
        self.assertEqual(result.issues[0].guidance.id, "inspect.document")


class DoctorPublicSeamTests(unittest.TestCase):
    def test_inactive_project_with_supported_capabilities_is_ok(self) -> None:
        from skill_collection import doctor

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            result = doctor(collection, project)

        self.assertEqual(result.category, "ok")
        self.assertEqual(result.status.category, "inactive")
        self.assertEqual([item.outcome for item in result.capabilities], ["supported"] * 3)

    def test_missing_directory_open_constant_is_containment_unsupported(self) -> None:
        from skill_collection import doctor

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            with patch.object(os, "O_DIRECTORY", None):
                result = doctor(collection, project)

        self.assertEqual(result.category, "blocked")
        check = result.capabilities[0]
        self.assertEqual(check.outcome, "unsupported")
        self.assertEqual(check.issue.issue.code, "activation.containment_unsupported")

    def test_missing_nofollow_constant_is_containment_unsupported(self) -> None:
        from skill_collection import doctor

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            with patch.object(os, "O_NOFOLLOW", None):
                result = doctor(collection, project)
        self.assertEqual(result.capabilities[0].outcome, "unsupported")
        self.assertEqual(result.capabilities[0].issue.issue.code, "activation.containment_unsupported")

    def test_unavailable_targets_are_not_inspected_without_issue(self) -> None:
        from skill_collection import doctor

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            with patch("skill_collection._capabilities.os.open", side_effect=FileNotFoundError(errno.ENOENT, "gone")) as mocked_open:
                os.supports_dir_fd.add(mocked_open)
                try:
                    result = doctor(collection, project)
                finally:
                    os.supports_dir_fd.remove(mocked_open)
        self.assertEqual(result.category, "attention")
        self.assertEqual([item.outcome for item in result.capabilities[1:]], ["not-inspected", "not-inspected"])
        self.assertEqual([item.issue for item in result.capabilities[1:]], [None, None])

    def test_only_documented_fsync_errno_is_unsupported(self) -> None:
        from skill_collection import doctor

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            with patch("skill_collection._capabilities.os.fsync", side_effect=OSError(errno.EINVAL, "unsupported")):
                result = doctor(collection, project)

        self.assertEqual(result.category, "blocked")
        self.assertEqual(result.capabilities[1].outcome, "unsupported")
        self.assertEqual(result.capabilities[2].outcome, "unsupported")

    def test_unexpected_fsync_error_propagates(self) -> None:
        from skill_collection import doctor

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            with patch("skill_collection._capabilities.os.fsync", side_effect=OSError(errno.EIO, "failure")):
                with self.assertRaises(OSError) as raised:
                    doctor(collection, project)
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_close_failure_does_not_hide_interruption(self) -> None:
        from skill_collection import doctor

        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            interruption = InterruptedError("stop")
            with patch("skill_collection._capabilities.os.fsync", side_effect=interruption), patch("skill_collection._capabilities.os.close", side_effect=OSError(errno.EIO, "close")):
                with self.assertRaises(InterruptedError) as raised:
                    doctor(collection, project)
        self.assertIs(raised.exception, interruption)
        self.assertTrue(any("close" in note.lower() for note in getattr(raised.exception, "__notes__", ())))


class InspectionCliPublicSeamTests(unittest.TestCase):
    def test_status_defaults_to_deterministic_json(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            outputs = []
            for arguments in ([], ["--format", "json"]):
                stdout = io.StringIO()
                code = main(["status", "--collection-root", str(collection), "--project-root", str(project), *arguments], stdout=stdout, stderr=io.StringIO())
                self.assertEqual(code, 1)
                outputs.append(stdout.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        payload = json.loads(outputs[0])
        self.assertEqual(payload["command"], "status")
        self.assertEqual(payload["result"]["category"], "inactive")

    def test_status_text_has_exact_stable_layout(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            stdout = io.StringIO()
            code = main(["status", "--collection-root", str(collection), "--project-root", str(project), "--format", "text"], stdout=stdout, stderr=io.StringIO())
        self.assertEqual(code, 1)
        self.assertTrue(stdout.getvalue().startswith("Project status: inactive\nProfile: base\nActivation ID: sha256:"))
        self.assertIn("\nIssues (0):\nNone.\n\nRecommended next commands (1):\n", stdout.getvalue())
        self.assertTrue(stdout.getvalue().endswith("   Review the current Activation without applying it.\n"))

    def test_doctor_json_ok_exits_zero(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            stdout = io.StringIO()
            code = main(["doctor", "--collection-root", str(collection), "--project-root", str(project)], stdout=stdout, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["result"]["category"], "ok")


if __name__ == "__main__":
    unittest.main()
