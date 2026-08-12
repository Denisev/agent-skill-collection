from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint3_support import add_discovered_skills, valid_collection, write_binding
from skill_collection.cli import main
from skill_collection import CleanupReport, Location, ValidationIssue, prepare_activation


class CliPublicSeamTests(unittest.TestCase):
    def test_activate_is_dry_run_until_apply_and_plan_id_are_explicit(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            dry_stdout = io.StringIO()

            dry_code = main(
                ["activate", "--collection-root", str(collection), "--project-root", str(project)],
                stdout=dry_stdout,
                stderr=io.StringIO(),
            )
            self.assertEqual(dry_code, 0)
            self.assertFalse((project / ".agents").exists())
            plan_id = json.loads(dry_stdout.getvalue())["result"]["plan_id"]
            apply_stdout = io.StringIO()
            apply_code = main(
                [
                    "activate", "--collection-root", str(collection),
                    "--project-root", str(project), "--apply", "--plan-id", plan_id,
                ],
                stdout=apply_stdout,
                stderr=io.StringIO(),
            )

            self.assertEqual(apply_code, 0)
            self.assertEqual(json.loads(apply_stdout.getvalue())["result"]["status"], "applied")
            self.assertTrue((project / ".agents/skills/alpha").is_symlink())

    def test_activate_stale_plan_returns_one_with_rooted_json_and_no_absolute_roots(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main(
                [
                    "activate", "--collection-root", str(collection),
                    "--project-root", str(project), "--apply", "--plan-id", "stale",
                ],
                stdout=stdout,
                stderr=stderr,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["result"]["issues"][0]["code"], "activation.stale_plan")
        self.assertNotIn(str(collection), stdout.getvalue())
        self.assertNotIn(str(project), stdout.getvalue())

    def test_activate_apply_requires_plan_id(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["activate", "--project-root", "/tmp/project", "--apply"],
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("--plan-id", stderr.getvalue())

    def test_activate_rejects_plan_id_without_apply(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["activate", "--project-root", "/tmp/project", "--plan-id", "x"],
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")

    def test_interrupted_cli_reports_only_incomplete_cleanup_details(self) -> None:
        cleanup = CleanupReport(
            True,
            False,
            (),
            (),
            (Location("project", ".agent-skill-collection/temporary"),),
            (
                ValidationIssue(
                    "activation.cleanup_incomplete",
                    "Cleanup incomplete.",
                    Location("project", ".agent-skill-collection/temporary"),
                ),
            ),
        )
        interruption = KeyboardInterrupt()
        interruption.activation_cleanup_report = cleanup
        stderr = io.StringIO()
        with patch("skill_collection.cli.apply_activation", side_effect=interruption):
            code = main(
                ["activate", "--project-root", "/private/project", "--apply", "--plan-id", "x"],
                stdout=io.StringIO(),
                stderr=stderr,
            )
        payload = json.loads(stderr.getvalue())
        self.assertEqual(code, 130)
        self.assertEqual(payload["error"]["code"], "system.interrupted")
        self.assertEqual(payload["cleanup"]["issues"][0]["code"], "activation.cleanup_incomplete")
        self.assertNotIn("/private/project", stderr.getvalue())

    def test_unexpected_cli_failure_reports_incomplete_cleanup_without_exception_details(self) -> None:
        cleanup = CleanupReport(
            True,
            False,
            (),
            (),
            (Location("project", ".agents/skills/alpha"),),
            (
                ValidationIssue(
                    "activation.cleanup_incomplete",
                    "Cleanup incomplete.",
                    Location("project", ".agents/skills/alpha"),
                ),
            ),
        )
        failure = RuntimeError("secret traceback detail")
        failure.activation_cleanup_report = cleanup
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("skill_collection.cli.apply_activation", side_effect=failure):
            code = main(
                ["activate", "--project-root", "/tmp/project", "--apply", "--plan-id", "x"],
                stdout=stdout,
                stderr=stderr,
            )

        payload = json.loads(stderr.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "system.unexpected")
        self.assertEqual(
            payload["cleanup"]["remaining_objects"],
            [{"root": "project", "relative_path": ".agents/skills/alpha"}],
        )
        self.assertNotIn("secret", stderr.getvalue())
        self.assertNotIn("/tmp/project", stderr.getvalue())
    def test_scan_json_is_deterministic_and_omits_absolute_root(self) -> None:
        with valid_collection() as collection:
            add_discovered_skills(collection, "alpha", "beta")
            outputs: list[str] = []
            for _ in range(2):
                stdout = io.StringIO()
                stderr = io.StringIO()
                exit_code = main(
                    ["scan", "--collection-root", str(collection)],
                    stdout=stdout,
                    stderr=stderr,
                )
                self.assertEqual(exit_code, 0)
                self.assertEqual(stderr.getvalue(), "")
                outputs.append(stdout.getvalue())

        self.assertEqual(outputs[0], outputs[1])
        self.assertNotIn(str(collection), outputs[0])
        payload = json.loads(outputs[0])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["command"], "scan")

    def test_plan_requires_explicit_project_root(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(["plan"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("--project-root", stderr.getvalue())

    def test_blocked_plan_returns_one_and_json_on_stdout(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha")
            project = Path(directory)
            write_binding(project)
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                [
                    "plan",
                    "--collection-root",
                    str(collection),
                    "--project-root",
                    str(project),
                ],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["result"]["status"], "blocked")

    def test_validate_defaults_collection_root_to_current_directory(self) -> None:
        with valid_collection() as collection:
            previous = Path.cwd()
            stdout = io.StringIO()
            stderr = io.StringIO()
            try:
                os.chdir(collection)
                exit_code = main(["validate"], stdout=stdout, stderr=stderr)
            finally:
                os.chdir(previous)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["result"], {"issues": []})

    def test_scan_rejects_project_root_flag(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(
            ["scan", "--project-root", "/tmp/project"],
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_validate_issue_returns_one_with_json_only_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = main(
                ["validate", "--collection-root", str(missing)],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue())["result"]["issues"][0]["code"],
            "root.missing",
        )

    def test_ready_plan_json_is_deterministic_and_omits_both_absolute_roots(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            outputs: list[str] = []
            for _ in range(2):
                stdout = io.StringIO()
                stderr = io.StringIO()
                exit_code = main(
                    ["plan", "--collection-root", str(collection), "--project-root", str(project)],
                    stdout=stdout,
                    stderr=stderr,
                )
                self.assertEqual(exit_code, 0)
                self.assertEqual(stderr.getvalue(), "")
                outputs.append(stdout.getvalue())

        self.assertEqual(outputs[0], outputs[1])
        self.assertNotIn(str(collection), outputs[0])
        self.assertNotIn(str(project), outputs[0])

    def test_plan_defaults_collection_root_to_current_directory(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            previous = Path.cwd()
            stdout = io.StringIO()
            stderr = io.StringIO()
            try:
                os.chdir(collection)
                exit_code = main(
                    ["plan", "--project-root", str(project)],
                    stdout=stdout,
                    stderr=stderr,
                )
            finally:
                os.chdir(previous)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_unexpected_output_failure_returns_three_as_json_on_stderr(self) -> None:
        class FailingOutput(io.StringIO):
            def write(self, value: str) -> int:
                raise RuntimeError("output failed")

        with valid_collection() as collection:
            add_discovered_skills(collection, "alpha", "beta")
            stderr = io.StringIO()
            exit_code = main(
                ["scan", "--collection-root", str(collection)],
                stdout=FailingOutput(),
                stderr=stderr,
            )

        self.assertEqual(exit_code, 3)
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "system.unexpected")
        self.assertEqual(error["message"], "An unexpected system failure occurred.")
        self.assertNotIn("output failed", stderr.getvalue())

    def test_keyboard_interrupt_returns_130_without_output(self) -> None:
        class InterruptingOutput(io.StringIO):
            def write(self, value: str) -> int:
                raise KeyboardInterrupt

        with valid_collection() as collection:
            add_discovered_skills(collection, "alpha", "beta")
            stderr = io.StringIO()
            exit_code = main(
                ["scan", "--collection-root", str(collection)],
                stdout=InterruptingOutput(),
                stderr=stderr,
            )

        self.assertEqual(exit_code, 130)
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
