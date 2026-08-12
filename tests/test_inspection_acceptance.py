from __future__ import annotations

from contextlib import ExitStack
from dataclasses import FrozenInstanceError
import errno
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from checkpoint3_support import add_discovered_skills, tree_contents, valid_collection, write_binding
from skill_collection import (
    ActivationReview,
    CapabilityCheck,
    DoctorReport,
    Guidance,
    GuidedIssue,
    Location,
    ProjectStatus,
    RecommendedCommand,
    ValidationIssue,
    apply_activation,
    doctor,
    prepare_activation,
    status,
)
from skill_collection.cli import main


def ready_project():
    stack = ExitStack()
    collection = stack.enter_context(valid_collection())
    directory = stack.enter_context(tempfile.TemporaryDirectory())
    add_discovered_skills(collection, "alpha", "beta")
    project = Path(directory)
    write_binding(project)
    return stack, collection, project


class StatusAcceptanceMatrixTests(unittest.TestCase):
    def test_every_public_result_is_frozen_and_uses_tuples(self) -> None:
        guidance = Guidance("g", "text")
        guided = GuidedIssue(ValidationIssue("code", "message", Location("project", ".")), guidance)
        command = RecommendedCommand("c", "command", "description")
        project_status = ProjectStatus("blocked", None, None, None, 0, 0, (guided,), (command,))
        capability = CapabilityCheck("safe-project-containment", "supported", "summary", None)
        report = DoctorReport("blocked", project_status, (capability,), (command,))
        for value, field in ((guidance, "id"), (guided, "issue"), (command, "id"), (project_status, "category"), (capability, "outcome"), (report, "category")):
            with self.subTest(type=type(value).__name__), self.assertRaises(FrozenInstanceError):
                setattr(value, field, getattr(value, field))
        self.assertIsInstance(project_status.issues, tuple)
        self.assertIsInstance(project_status.recommended_commands, tuple)
        self.assertIsInstance(report.capabilities, tuple)
        self.assertIsInstance(report.recommended_commands, tuple)

    def test_status_and_doctor_leave_both_roots_byte_identical(self) -> None:
        stack, collection, project = ready_project()
        with stack:
            before_collection = tree_contents(collection)
            before_project = tree_contents(project)
            status(collection, project)
            doctor(collection, project)
            self.assertEqual(tree_contents(collection), before_collection)
            self.assertEqual(tree_contents(project), before_project)

    def test_active_and_drifted_map_exact_review_counts(self) -> None:
        stack, collection, project = ready_project()
        with stack:
            initial = prepare_activation(collection, project)
            applied = apply_activation(collection, project, initial.plan_id)
            self.assertEqual(applied.status, "applied")
            active_review = prepare_activation(collection, project)
            active = status(collection, project)
            (project / ".agents/skills/alpha").unlink()
            drifted_review = prepare_activation(collection, project)
            drifted = status(collection, project)

        self.assertEqual(active.category, "active")
        self.assertEqual(active.pending_action_count, len(active_review.actions))
        self.assertEqual(active.unchanged_link_count, len(active_review.unchanged_links))
        self.assertEqual(drifted.category, "drifted")
        self.assertEqual(drifted.pending_action_count, len(drifted_review.actions))
        self.assertEqual(drifted.unchanged_link_count, len(drifted_review.unchanged_links))

    def test_guidance_registry_is_exhaustive_and_unknown_is_stable(self) -> None:
        texts = {
            "inspect.root": "Provide existing collection and project directories, then inspect again.",
            "inspect.document": "Correct the reported TOML document or field, then validate again.",
            "inspect.source": "Correct the reported Source state, then validate and scan again.",
            "inspect.catalog": "Correct Catalog or Skill discovery state, then validate and scan again.",
            "inspect.composition": "Correct the reported Group or Profile composition, then validate again.",
            "inspect.binding": "Correct the project Binding so it selects the intended pinned collection state, then validate again.",
            "inspect.activation-ownership": "Review the reported project-owned or Activation-owned object; inspection will not change it.",
            "inspect.platform-containment": "Use a platform that provides the no-follow and directory-relative operations required for safe Activation.",
            "inspect.platform-directory-fsync": "Use a project filesystem that supports directory fsync before applying Activation.",
            "inspect.platform-file-fsync": "Use a project filesystem that supports regular-file fsync before applying Activation.",
            "inspect.unclassified": "Review the reported issue, then run status again.",
        }
        groups = {
            "inspect.root": {"root.missing"},
            "inspect.document": {"document.missing", "toml.malformed", "field.required", "field.invalid", "field.unexpected", "field.duplicate"},
            "inspect.source": {"source.duplicate_id", "source.invalid", "source.path_outside_collection", "source.path_symlink", "source.path_unavailable", "source.submodule_dirty", "source.submodule_invalid", "source.submodule_missing", "source.submodule_unpinned"},
            "inspect.catalog": {"catalog.skill_not_discovered", "discovery.ambiguous_catalog", "discovery.uncataloged", "discovery.unreadable", "skill.duplicate_id", "skill.missing", "skill.name_collision", "skill.path_outside_source", "skill.remove_missing", "skill.source_missing"},
            "inspect.composition": {"group.cycle", "group.duplicate_name", "group.missing", "profile.duplicate_name", "profile.inheritance_cycle", "profile.invalid_selection", "profile.missing"},
            "inspect.binding": {"binding.collection_revision_mismatch", "binding.target_outside_project"},
            "inspect.activation-ownership": {"activation.broken_symlink", "activation.owned_object_mismatch", "activation.record_exists", "activation.record_intent_mismatch", "activation.record_invalid", "activation.record_invalid_type", "activation.record_noncanonical", "activation.record_outside_project", "activation.record_path_owned", "activation.repair_unowned_directory", "activation.target_owned", "activation.unrecorded_object"},
            "inspect.platform-containment": {"activation.containment_unsupported"},
            "inspect.platform-directory-fsync": {"activation.directory_fsync_unsupported"},
            "inspect.platform-file-fsync": {"activation.file_fsync_unsupported"},
            "inspect.unclassified": {"future.issue"},
        }
        for guidance_id, codes in groups.items():
            for code in codes:
                with self.subTest(code=code):
                    issue = ValidationIssue(code, "message", Location("project", "."))
                    review = ActivationReview("blocked", None, None, None, (), (), (), None, (issue,))
                    with patch("skill_collection.inspection.prepare_activation", return_value=review):
                        result = status("collection", "project")
                    self.assertEqual(result.issues[0].guidance.id, guidance_id)
                    self.assertEqual(result.issues[0].guidance.text, texts[guidance_id])
                    self.assertEqual(result.issues[0].issue, issue)

    def test_recommended_commands_are_ordered_and_deduplicated(self) -> None:
        issues = tuple(
            ValidationIssue(code, "message", Location("project", "."))
            for code in ("source.invalid", "catalog.skill_not_discovered", "activation.containment_unsupported", "source.invalid")
        )
        review = ActivationReview("blocked", None, None, None, (), (), (), None, issues)
        with patch("skill_collection.inspection.prepare_activation", return_value=review):
            result = status("collection", "project")
        self.assertEqual([item.id for item in result.recommended_commands], ["validate", "scan", "inspect-doctor"])
        with patch("skill_collection.inspection.status", return_value=result), patch("skill_collection.inspection.containment_capability", return_value="supported"), patch("skill_collection.inspection.directory_fsync_capability", return_value="supported"), patch("skill_collection.inspection.regular_file_fsync_capability", return_value="supported"):
            report = doctor("collection", "project")
        self.assertEqual([item.id for item in report.recommended_commands], ["validate", "scan"])


class DoctorCapabilityAcceptanceMatrixTests(unittest.TestCase):
    def test_all_aggregate_categories_follow_the_status_and_capability_matrix(self) -> None:
        base = ProjectStatus("active", "base", "a", "p", 0, 1, (), ())
        supported = (CapabilityCheck("safe-project-containment", "supported", "ok", None),) * 3
        unavailable = supported[:1] + (CapabilityCheck("project-directory-fsync", "not-inspected", "n/a", None),) + supported[2:]
        issue = GuidedIssue(ValidationIssue("activation.file_fsync_unsupported", "no", Location("project", ".")), Guidance("inspect.platform-file-fsync", "g"))
        unsupported = supported[:2] + (CapabilityCheck("binding-file-fsync", "unsupported", "no", issue),)
        cases = (
            (base, supported, "ok"),
            (ProjectStatus("inactive", "base", "a", "p", 1, 0, (), ()), supported, "ok"),
            (ProjectStatus("drifted", "base", "a", "p", 1, 0, (), ()), supported, "ok"),
            (base, unavailable, "attention"),
            (base, unsupported, "blocked"),
            (ProjectStatus("blocked", None, None, None, 0, 0, (), ()), supported, "blocked"),
        )
        for project_status, capabilities, expected in cases:
            with self.subTest(expected=expected, status=project_status.category), patch("skill_collection.inspection.status", return_value=project_status), patch("skill_collection.inspection.containment_capability", return_value="supported"), patch("skill_collection.inspection.directory_fsync_capability", return_value="supported"), patch("skill_collection.inspection.regular_file_fsync_capability", return_value="supported"):
                actual = doctor("collection", "project")
                # Replace probe outputs through their public representation for aggregate-only cases.
                if capabilities is unavailable:
                    with patch("skill_collection.inspection.directory_fsync_capability", return_value="target-unavailable"):
                        actual = doctor("collection", "project")
                elif capabilities is unsupported:
                    with patch("skill_collection.inspection.regular_file_fsync_capability", return_value="unsupported"):
                        actual = doctor("collection", "project")
                self.assertEqual(actual.category, expected)

    def test_missing_wrong_type_and_unsafe_link_targets_are_not_inspected(self) -> None:
        base = ProjectStatus("active", "base", "a", "p", 0, 1, (), ())
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            cases = ("missing", "wrong-type", "unsafe-link")
            for case in cases:
                binding = project / "skill-collection.toml"
                if binding.exists() or binding.is_symlink():
                    binding.unlink() if not binding.is_dir() else binding.rmdir()
                if case == "wrong-type":
                    binding.mkdir()
                elif case == "unsafe-link":
                    binding.symlink_to(project / "absent")
                with self.subTest(case=case), patch("skill_collection.inspection.status", return_value=base):
                    result = doctor("collection", project)
                self.assertEqual(result.capabilities[2].outcome, "not-inspected")
                self.assertIsNone(result.capabilities[2].issue)

    def test_replaced_and_inaccessible_targets_are_not_inspected(self) -> None:
        base = ProjectStatus("active", "base", "a", "p", 0, 1, (), ())
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            binding = project / "skill-collection.toml"
            binding.write_text("original")
            real_open = os.open
            replaced = False

            def replace_then_open(path, flags, *args, **kwargs):
                nonlocal replaced
                if Path(path) == binding and not replaced:
                    replaced = True
                    binding.unlink()
                    binding.write_text("replacement")
                return real_open(path, flags, *args, **kwargs)

            with patch("skill_collection.inspection.status", return_value=base), patch("skill_collection._capabilities.os.open", side_effect=replace_then_open) as mocked_open:
                os.supports_dir_fd.add(mocked_open)
                try:
                    replaced_result = doctor("collection", project)
                finally:
                    os.supports_dir_fd.remove(mocked_open)
            self.assertEqual(replaced_result.capabilities[2].outcome, "not-inspected")

            with patch("skill_collection.inspection.status", return_value=base), patch("skill_collection._capabilities.os.open", side_effect=PermissionError(errno.EACCES, "denied")) as mocked_open:
                os.supports_dir_fd.add(mocked_open)
                try:
                    inaccessible = doctor("collection", project)
                finally:
                    os.supports_dir_fd.remove(mocked_open)
            self.assertEqual([item.outcome for item in inaccessible.capabilities[1:]], ["not-inspected", "not-inspected"])

    def test_each_acquisition_errno_is_target_unavailable(self) -> None:
        names = ("ENOENT", "ENOTDIR", "EACCES", "EPERM", "ELOOP", "ESTALE")
        base = ProjectStatus("active", "base", "a", "p", 0, 1, (), ())
        for name in names:
            if not hasattr(errno, name):
                continue
            with self.subTest(errno=name), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                (project / "skill-collection.toml").write_text("binding")
                error = OSError(getattr(errno, name), name)
                with patch("skill_collection.inspection.status", return_value=base), patch("skill_collection._capabilities.os.open", side_effect=error) as mocked_open:
                    os.supports_dir_fd.add(mocked_open)
                    try:
                        result = doctor("collection", project)
                    finally:
                        os.supports_dir_fd.remove(mocked_open)
                self.assertEqual(result.category, "attention")
                self.assertEqual([item.outcome for item in result.capabilities[1:]], ["not-inspected", "not-inspected"])
                self.assertTrue(all(item.issue is None for item in result.capabilities[1:]))

    def test_each_documented_fsync_errno_is_unsupported_for_each_file_type(self) -> None:
        values = {name: getattr(errno, name) for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP") if hasattr(errno, name)}
        for name, value in values.items():
            for target_kind in ("directory", "regular"):
                stack, collection, project = ready_project()
                with stack, self.subTest(errno=name, target=target_kind):
                    real_fsync = os.fsync

                    def fail_selected(fd):
                        mode = os.fstat(fd).st_mode
                        if (target_kind == "directory" and stat.S_ISDIR(mode)) or (target_kind == "regular" and stat.S_ISREG(mode)):
                            raise OSError(value, name)
                        real_fsync(fd)

                    with patch("skill_collection._capabilities.os.fsync", side_effect=fail_selected):
                        result = doctor(collection, project)
                    expected_index = 1 if target_kind == "directory" else 2
                    self.assertEqual(result.capabilities[expected_index].outcome, "unsupported")

    def test_interruptions_and_unexpected_errors_propagate_from_each_stage(self) -> None:
        stages = ("acquisition", "verification", "fsync", "close")
        for exception in (InterruptedError("stop"), OSError(errno.EIO, "io")):
            for stage in stages:
                stack, collection, project = ready_project()
                with stack, self.subTest(stage=stage, exception=type(exception).__name__), ExitStack() as mocks:
                    if stage == "acquisition":
                        mocked = mocks.enter_context(patch("skill_collection._capabilities.os.open", side_effect=exception))
                        os.supports_dir_fd.add(mocked)
                        mocks.callback(os.supports_dir_fd.remove, mocked)
                    elif stage == "verification":
                        mocks.enter_context(patch("skill_collection._capabilities.os.fstat", side_effect=exception))
                    elif stage == "fsync":
                        mocks.enter_context(patch("skill_collection._capabilities.os.fsync", side_effect=exception))
                    else:
                        mocks.enter_context(patch("skill_collection._capabilities.os.close", side_effect=exception))
                    with self.assertRaises(type(exception)):
                        doctor(collection, project)

    def test_descriptors_close_exactly_once_for_every_post_open_outcome(self) -> None:
        for outcome in ("supported", "unsupported", "target-unavailable", "interrupted", "unexpected"):
            stack, collection, project = ready_project()
            with stack, self.subTest(outcome=outcome):
                real_close = os.close
                real_open = os.open
                opened: list[int] = []
                closed: list[int] = []

                def record_open(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    opened.append(fd)
                    return fd

                def record_close(fd):
                    closed.append(fd)
                    real_close(fd)

                with ExitStack() as mocks:
                    mocked_open = mocks.enter_context(patch("skill_collection._capabilities.os.open", side_effect=record_open))
                    os.supports_dir_fd.add(mocked_open)
                    mocks.callback(os.supports_dir_fd.remove, mocked_open)
                    mocks.enter_context(patch("skill_collection._capabilities.os.close", side_effect=record_close))
                    if outcome == "unsupported":
                        mocks.enter_context(patch("skill_collection._capabilities.os.fsync", side_effect=OSError(errno.EINVAL, "no")))
                    elif outcome == "target-unavailable":
                        original_fstat = os.fstat

                        def wrong_type(fd):
                            value = original_fstat(fd)
                            return os.stat_result((stat.S_IFIFO,) + tuple(value)[1:])

                        mocks.enter_context(patch("skill_collection._capabilities.os.fstat", side_effect=wrong_type))
                    elif outcome == "interrupted":
                        mocks.enter_context(patch("skill_collection._capabilities.os.fsync", side_effect=InterruptedError("stop")))
                    elif outcome == "unexpected":
                        mocks.enter_context(patch("skill_collection._capabilities.os.fsync", side_effect=OSError(errno.EIO, "io")))
                    if outcome in ("interrupted", "unexpected"):
                        with self.assertRaises(InterruptedError if outcome == "interrupted" else OSError):
                            doctor(collection, project)
                    else:
                        doctor(collection, project)
                self.assertEqual(closed, opened)
                self.assertEqual(len(closed), 1 if outcome in ("interrupted", "unexpected") else 2)

    def test_close_failure_preserves_each_pending_failure(self) -> None:
        for original in (InterruptedError("stop"), KeyboardInterrupt(), OSError(errno.EIO, "original")):
            stack, collection, project = ready_project()
            with stack, self.subTest(original=type(original).__name__), patch("skill_collection._capabilities.os.fsync", side_effect=original), patch("skill_collection._capabilities.os.close", side_effect=OSError(errno.EBADF, "close")):
                with self.assertRaises(type(original)) as raised:
                    doctor(collection, project)
            self.assertIs(raised.exception, original)
            self.assertTrue(any("close" in note.lower() for note in getattr(original, "__notes__", ())))

    def test_close_failure_without_pending_failure_is_primary(self) -> None:
        stack, collection, project = ready_project()
        close_error = OSError(errno.EBADF, "close")
        with stack, patch("skill_collection._capabilities.os.close", side_effect=close_error):
            with self.assertRaises(OSError) as raised:
                doctor(collection, project)
        self.assertIs(raised.exception, close_error)

    def test_doctor_never_calls_write_capable_operations(self) -> None:
        stack, collection, project = ready_project()
        with stack, ExitStack() as mocks:
            forbidden = []
            for target in ("os.write", "os.replace", "os.rename", "os.chmod", "os.remove", "pathlib.Path.write_bytes", "pathlib.Path.write_text", "pathlib.Path.mkdir", "pathlib.Path.symlink_to", "pathlib.Path.unlink"):
                forbidden.append(mocks.enter_context(patch(target, side_effect=AssertionError(target))))
            real_open = os.open
            open_flags = []

            def inspect_open(path, flags, *args, **kwargs):
                open_flags.append(flags)
                return real_open(path, flags, *args, **kwargs)

            mocked_open = mocks.enter_context(patch("skill_collection._capabilities.os.open", side_effect=inspect_open))
            os.supports_dir_fd.add(mocked_open)
            mocks.callback(os.supports_dir_fd.remove, mocked_open)
            for name in ("mkdir", "symlink", "unlink", "rmdir", "link"):
                original = getattr(os, name)
                mocked = mocks.enter_context(patch(f"skill_collection._capabilities.os.{name}", wraps=original))
                os.supports_dir_fd.add(mocked)
                mocks.callback(os.supports_dir_fd.remove, mocked)
                if name == "link":
                    os.supports_follow_symlinks.add(mocked)
                    mocks.callback(os.supports_follow_symlinks.remove, mocked)
                forbidden.append(mocked)
            doctor(collection, project)
        self.assertTrue(all(item.call_count == 0 for item in forbidden))
        write_mask = sum(getattr(os, name, 0) for name in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_EXCL", "O_TRUNC", "O_APPEND"))
        self.assertTrue(open_flags)
        self.assertTrue(all(flags & write_mask == 0 for flags in open_flags))

    def test_checkpoint_five_layer_adds_no_external_or_global_behavior(self) -> None:
        project_status = ProjectStatus("active", "base", "a", "p", 0, 1, (), ())
        forbidden_targets = (
            "subprocess.run", "subprocess.Popen", "os.system", "socket.socket",
            "urllib.request.urlopen", "http.client.HTTPConnection",
            "os.putenv", "os.unsetenv", "os.chdir",
        )
        with ExitStack() as mocks:
            mocks.enter_context(patch("skill_collection.inspection.status", return_value=project_status))
            mocks.enter_context(patch("skill_collection.inspection.containment_capability", return_value="supported"))
            mocks.enter_context(patch("skill_collection.inspection.directory_fsync_capability", return_value="supported"))
            mocks.enter_context(patch("skill_collection.inspection.regular_file_fsync_capability", return_value="supported"))
            forbidden = [mocks.enter_context(patch(target, side_effect=AssertionError(target))) for target in forbidden_targets]
            result = doctor("collection", "project")
        self.assertEqual(result.category, "ok")
        self.assertTrue(all(mock.call_count == 0 for mock in forbidden))

    def test_real_collection_owned_inspection_adds_no_external_or_global_behavior(self) -> None:
        stack, collection, project = ready_project()
        forbidden_targets = (
            "subprocess.run", "subprocess.Popen", "os.system", "socket.socket",
            "urllib.request.urlopen", "http.client.HTTPConnection",
            "os.putenv", "os.unsetenv", "os.chdir",
        )
        with stack, ExitStack() as mocks:
            forbidden = [mocks.enter_context(patch(target, side_effect=AssertionError(target))) for target in forbidden_targets]
            result = doctor(collection, project)
        self.assertEqual(result.status.category, "inactive")
        self.assertEqual(result.category, "ok")
        self.assertTrue(all(mock.call_count == 0 for mock in forbidden))


class ActivationPreflightAcceptanceMatrixTests(unittest.TestCase):
    def test_every_probe_result_blocks_or_continues_before_writing(self) -> None:
        cases = (
            ("directory", "unsupported", "activation.directory_fsync_unsupported"),
            ("directory", "target-unavailable", "activation.precondition_changed"),
            ("file", "unsupported", "activation.file_fsync_unsupported"),
            ("file", "target-unavailable", "activation.precondition_changed"),
        )
        for probe, value, code in cases:
            stack, collection, project = ready_project()
            with stack, self.subTest(probe=probe, value=value):
                review = prepare_activation(collection, project)
                target = "skill_collection._activation_transaction.directory_fsync_capability" if probe == "directory" else "skill_collection._activation_transaction.regular_file_fsync_capability"
                with patch(target, return_value=value):
                    result = apply_activation(collection, project, review.plan_id)
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.issues[0].code, code)
                self.assertFalse((project / ".agent-skill-collection").exists())

        stack, collection, project = ready_project()
        with stack:
            review = prepare_activation(collection, project)
            with patch("skill_collection._activation_transaction.containment_capability", return_value="unsupported"):
                result = apply_activation(collection, project, review.plan_id)
            self.assertEqual(result.issues[0].code, "activation.containment_unsupported")
            self.assertFalse((project / ".agent-skill-collection").exists())

        stack, collection, project = ready_project()
        with stack:
            review = prepare_activation(collection, project)
            result = apply_activation(collection, project, review.plan_id)
            self.assertEqual(result.status, "applied")

    def test_unexpected_probe_failure_propagates_before_writing(self) -> None:
        stack, collection, project = ready_project()
        with stack:
            review = prepare_activation(collection, project)
            with patch("skill_collection._activation_transaction.directory_fsync_capability", side_effect=OSError(errno.EIO, "io")):
                with self.assertRaises(OSError):
                    apply_activation(collection, project, review.plan_id)
            self.assertFalse((project / ".agent-skill-collection").exists())


class InspectionCliAcceptanceMatrixTests(unittest.TestCase):
    def test_active_drifted_and_blocked_status_text_are_complete_golden_documents(self) -> None:
        review_command = RecommendedCommand(
            "review-activation",
            "skill-collection activate --collection-root <collection-root> --project-root <project-root>",
            "Review the current Activation without applying it.",
        )
        doctor_command = RecommendedCommand(
            "inspect-doctor",
            "skill-collection doctor --collection-root <collection-root> --project-root <project-root>",
            "Inspect project state and platform capabilities.",
        )
        validate_command = RecommendedCommand(
            "validate",
            "skill-collection validate --collection-root <collection-root> --project-root <project-root>",
            "Validate collection and project documents.",
        )
        scan_command = RecommendedCommand(
            "scan",
            "skill-collection scan --collection-root <collection-root>",
            "Inspect Skill discovery and Catalog correlation.",
        )
        blocked_issue = GuidedIssue(
            ValidationIssue(
                "catalog.skill_not_discovered",
                "Catalog Skill has no matching discovery.",
                Location("collection", "catalog.toml#skills[0].path"),
                (Location("collection", "skills/alpha"),),
            ),
            Guidance(
                "inspect.catalog",
                "Correct Catalog or Skill discovery state, then validate and scan again.",
            ),
        )
        cases = (
            (
                ProjectStatus("active", "base", "activation", "plan", 0, 2, (), (doctor_command,)),
                """Project status: active
Profile: base
Activation ID: activation
Plan ID: plan
Pending actions: 0
Unchanged links: 2

Issues (0):
None.

Recommended next commands (1):
1. skill-collection doctor --collection-root <collection-root> --project-root <project-root>
   Inspect project state and platform capabilities.
""",
            ),
            (
                ProjectStatus("drifted", "base", "activation", "plan", 1, 1, (), (review_command,)),
                """Project status: drifted
Profile: base
Activation ID: activation
Plan ID: plan
Pending actions: 1
Unchanged links: 1

Issues (0):
None.

Recommended next commands (1):
1. skill-collection activate --collection-root <collection-root> --project-root <project-root>
   Review the current Activation without applying it.
""",
            ),
            (
                ProjectStatus("blocked", None, None, None, 0, 0, (blocked_issue,), (validate_command, scan_command)),
                """Project status: blocked
Profile: -
Activation ID: -
Plan ID: -
Pending actions: 0
Unchanged links: 0

Issues (1):
1. [catalog.skill_not_discovered] Catalog Skill has no matching discovery.
   Location: collection:catalog.toml#skills[0].path
   Related: collection:skills/alpha
   Guidance: Correct Catalog or Skill discovery state, then validate and scan again.

Recommended next commands (2):
1. skill-collection validate --collection-root <collection-root> --project-root <project-root>
   Validate collection and project documents.
2. skill-collection scan --collection-root <collection-root>
   Inspect Skill discovery and Catalog correlation.
""",
            ),
        )
        for result, expected in cases:
            stdout = io.StringIO()
            with self.subTest(category=result.category), patch("skill_collection.cli.status", return_value=result):
                code = main(["status", "--project-root", "/project", "--format", "text"], stdout=stdout, stderr=io.StringIO())
            self.assertEqual(code, 0 if result.category == "active" else 1)
            self.assertEqual(stdout.getvalue(), expected)

    def test_doctor_text_is_a_complete_golden_document(self) -> None:
        command = RecommendedCommand("review-activation", "skill-collection activate --collection-root <collection-root> --project-root <project-root>", "Review the current Activation without applying it.")
        project_status = ProjectStatus("inactive", "base", "activation", "plan", 2, 1, (), (command,))
        report = DoctorReport("ok", project_status, (
            CapabilityCheck("safe-project-containment", "supported", "Required no-follow and directory-relative operations are available.", None),
            CapabilityCheck("project-directory-fsync", "supported", "The project filesystem supports directory fsync.", None),
            CapabilityCheck("binding-file-fsync", "supported", "The project filesystem supports regular-file fsync.", None),
        ), (command,))
        stdout = io.StringIO()
        with patch("skill_collection.cli.doctor", return_value=report):
            code = main(["doctor", "--project-root", "/project", "--format", "text"], stdout=stdout, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), """Doctor: ok

Capabilities (3):
- safe-project-containment: supported — Required no-follow and directory-relative operations are available.
- project-directory-fsync: supported — The project filesystem supports directory fsync.
- binding-file-fsync: supported — The project filesystem supports regular-file fsync.

Project:
  Status: inactive
  Profile: base
  Activation ID: activation
  Plan ID: plan
  Pending actions: 2
  Unchanged links: 1

  Issues (0):
  None.

Recommended next commands (1):
1. skill-collection activate --collection-root <collection-root> --project-root <project-root>
   Review the current Activation without applying it.
""")

    def test_doctor_attention_and_blocked_capability_text_are_golden_documents(self) -> None:
        project_status = ProjectStatus("drifted", "base", "activation", "plan", 1, 1, (), ())
        attention = DoctorReport("attention", project_status, (
            CapabilityCheck("safe-project-containment", "supported", "Required no-follow and directory-relative operations are available.", None),
            CapabilityCheck("project-directory-fsync", "not-inspected", "Directory fsync was not inspected because the project root could not be safely inspected.", None),
            CapabilityCheck("binding-file-fsync", "supported", "The project filesystem supports regular-file fsync.", None),
        ), ())
        platform_issue = GuidedIssue(
            ValidationIssue("activation.file_fsync_unsupported", "The project filesystem does not support regular-file fsync.", Location("project", "skill-collection.toml")),
            Guidance("inspect.platform-file-fsync", "Use a project filesystem that supports regular-file fsync before applying Activation."),
        )
        blocked = DoctorReport("blocked", project_status, (
            CapabilityCheck("safe-project-containment", "supported", "Required no-follow and directory-relative operations are available.", None),
            CapabilityCheck("project-directory-fsync", "supported", "The project filesystem supports directory fsync.", None),
            CapabilityCheck("binding-file-fsync", "unsupported", "The project filesystem does not support regular-file fsync.", platform_issue),
        ), ())
        expected = {
            "attention": """Doctor: attention

Capabilities (3):
- safe-project-containment: supported — Required no-follow and directory-relative operations are available.
- project-directory-fsync: not-inspected — Directory fsync was not inspected because the project root could not be safely inspected.
- binding-file-fsync: supported — The project filesystem supports regular-file fsync.

Project:
  Status: drifted
  Profile: base
  Activation ID: activation
  Plan ID: plan
  Pending actions: 1
  Unchanged links: 1

  Issues (0):
  None.

Recommended next commands (0):
None.
""",
            "blocked": """Doctor: blocked

Capabilities (3):
- safe-project-containment: supported — Required no-follow and directory-relative operations are available.
- project-directory-fsync: supported — The project filesystem supports directory fsync.
- binding-file-fsync: unsupported — The project filesystem does not support regular-file fsync.
  1. [activation.file_fsync_unsupported] The project filesystem does not support regular-file fsync.
     Location: project:skill-collection.toml
     Guidance: Use a project filesystem that supports regular-file fsync before applying Activation.

Project:
  Status: drifted
  Profile: base
  Activation ID: activation
  Plan ID: plan
  Pending actions: 1
  Unchanged links: 1

  Issues (0):
  None.

Recommended next commands (0):
None.
""",
        }
        for report in (attention, blocked):
            stdout = io.StringIO()
            with self.subTest(category=report.category), patch("skill_collection.cli.doctor", return_value=report):
                main(["doctor", "--project-root", "/project", "--format", "text"], stdout=stdout, stderr=io.StringIO())
            self.assertEqual(stdout.getvalue(), expected[report.category])

    def test_text_escapes_control_characters(self) -> None:
        issue = GuidedIssue(ValidationIssue("bad\ncode", "line\r\ntext\t", Location("project", "a\nb")), Guidance("g", "guide\ttext"))
        result = ProjectStatus("blocked", None, None, None, 0, 0, (issue,), ())
        stdout = io.StringIO()
        with patch("skill_collection.cli.status", return_value=result):
            main(["status", "--project-root", "/project", "--format", "text"], stdout=stdout, stderr=io.StringIO())
        self.assertIn("[bad\\ncode] line\\r\\ntext\\t", stdout.getvalue())
        self.assertIn("project:a\\nb", stdout.getvalue())
        self.assertIn("guide\\ttext", stdout.getvalue())

    def test_json_and_text_have_semantic_parity_for_status(self) -> None:
        command = RecommendedCommand("validate", "validate command", "Validate.")
        result = ProjectStatus("blocked", None, None, None, 0, 0, (), (command,))
        outputs = {}
        with patch("skill_collection.cli.status", return_value=result):
            for fmt in ("json", "text"):
                stdout = io.StringIO()
                main(["status", "--project-root", "/project", "--format", fmt], stdout=stdout, stderr=io.StringIO())
                outputs[fmt] = stdout.getvalue()
        payload = json.loads(outputs["json"])["result"]
        self.assertIn(f"Project status: {payload['category']}", outputs["text"])
        self.assertIn(f"Pending actions: {payload['pending_action_count']}", outputs["text"])
        self.assertIn(payload["recommended_commands"][0]["command"], outputs["text"])

    def test_doctor_json_and_text_are_deterministic_and_semantically_aligned(self) -> None:
        stack, collection, project = ready_project()
        with stack:
            values = {}
            for fmt in ("json", "text"):
                outputs = []
                for _ in range(2):
                    stdout = io.StringIO()
                    main(["doctor", "--collection-root", str(collection), "--project-root", str(project), "--format", fmt], stdout=stdout, stderr=io.StringIO())
                    outputs.append(stdout.getvalue())
                self.assertEqual(outputs[0], outputs[1])
                values[fmt] = outputs[0]
        payload = json.loads(values["json"])["result"]
        self.assertIn(f"Doctor: {payload['category']}", values["text"])
        self.assertIn(f"Status: {payload['status']['category']}", values["text"])
        for capability in payload["capabilities"]:
            self.assertIn(f"{capability['id']}: {capability['outcome']}", values["text"])

    def test_doctor_json_and_text_preserve_every_semantic_field(self) -> None:
        project_issue = GuidedIssue(
            ValidationIssue("document.missing", "Missing document.", Location("project", "binding"), (Location("collection", "catalog"),)),
            Guidance("inspect.document", "Correct the reported TOML document or field, then validate again."),
        )
        capability_issue = GuidedIssue(
            ValidationIssue("activation.file_fsync_unsupported", "No file fsync.", Location("project", "binding")),
            Guidance("inspect.platform-file-fsync", "Use a project filesystem that supports regular-file fsync before applying Activation."),
        )
        command = RecommendedCommand("validate", "validate command", "Validate collection and project documents.")
        project_status = ProjectStatus("blocked", None, None, None, 0, 0, (project_issue,), (command,))
        report = DoctorReport("blocked", project_status, (
            CapabilityCheck("safe-project-containment", "supported", "Containment supported.", None),
            CapabilityCheck("project-directory-fsync", "not-inspected", "Directory not inspected.", None),
            CapabilityCheck("binding-file-fsync", "unsupported", "File fsync unsupported.", capability_issue),
        ), (command,))
        outputs = {}
        with patch("skill_collection.cli.doctor", return_value=report):
            for fmt in ("json", "text"):
                stdout = io.StringIO()
                main(["doctor", "--project-root", "/project", "--format", fmt], stdout=stdout, stderr=io.StringIO())
                outputs[fmt] = stdout.getvalue()
        payload = json.loads(outputs["json"])["result"]
        text = outputs["text"]
        self.assertEqual(payload["category"], "blocked")
        self.assertIn("Doctor: blocked", text)
        for key, label in (("category", "Status"), ("profile", "Profile"), ("activation_id", "Activation ID"), ("plan_id", "Plan ID"), ("pending_action_count", "Pending actions"), ("unchanged_link_count", "Unchanged links")):
            value = payload["status"][key]
            self.assertIn(f"{label}: {value if value is not None else '-'}", text)
        for issue_payload in (payload["status"]["issues"][0], payload["capabilities"][2]["issue"]):
            issue = issue_payload["issue"]
            self.assertIn(issue["code"], text)
            self.assertIn(issue["message"], text)
            self.assertIn(f"{issue['location']['root']}:{issue['location']['relative_path']}", text)
            for related in issue["related_locations"]:
                self.assertIn(f"{related['root']}:{related['relative_path']}", text)
            self.assertIn(issue_payload["guidance"]["text"], text)
        for capability in payload["capabilities"]:
            self.assertIn(capability["id"], text)
            self.assertIn(capability["outcome"], text)
            self.assertIn(capability["summary"], text)
        for recommended in payload["recommended_commands"]:
            self.assertIn(recommended["command"], text)
            self.assertIn(recommended["description"], text)

    def test_exit_codes_and_streams_for_all_expected_categories_and_formats(self) -> None:
        for command, categories in (("status", {"active": 0, "inactive": 1, "drifted": 1, "blocked": 1}), ("doctor", {"ok": 0, "attention": 1, "blocked": 1})):
            for category, expected in categories.items():
                for fmt in ("json", "text"):
                    if command == "status":
                        result = ProjectStatus(category, None, None, None, 0, 0, (), ())
                    else:
                        result = DoctorReport(category, ProjectStatus("active", None, None, None, 0, 0, (), ()), (), ())
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with self.subTest(command=command, category=category, format=fmt), patch(f"skill_collection.cli.{command}", return_value=result):
                        code = main([command, "--project-root", "/project", "--format", fmt], stdout=stdout, stderr=stderr)
                    self.assertEqual(code, expected)
                    self.assertNotEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), "")

    def test_usage_unexpected_and_interruption_stream_boundaries(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        self.assertEqual(main(["status", "--format", "text"], stdout=stdout, stderr=stderr), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotEqual(stderr.getvalue(), "")
        stdout, stderr = io.StringIO(), io.StringIO()
        self.assertEqual(main(["doctor", "--project-root", "/project", "--format", "yaml"], stdout=stdout, stderr=stderr), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotEqual(stderr.getvalue(), "")
        for failure, expected in ((OSError("failure"), 3), (KeyboardInterrupt(), 130)):
            for fmt in ("json", "text"):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("skill_collection.cli.doctor", side_effect=failure):
                    code = main(["doctor", "--project-root", "/project", "--format", fmt], stdout=stdout, stderr=stderr)
                self.assertEqual(code, expected)
                self.assertEqual(stdout.getvalue(), "")
                if expected == 3:
                    self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "system.unexpected")
                else:
                    self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
