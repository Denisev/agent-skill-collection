from __future__ import annotations

import io
import json
import os
import secrets
import stat
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from checkpoint3_support import add_discovered_skills, valid_collection
from skill_collection import (
    InitializationCleanupReport,
    InitializationResult,
    Location,
    ValidationIssue,
    apply_project_initialization,
    plan_project_initialization,
)
from skill_collection.cli import main
from skill_collection._initialization_transaction import (
    _DescriptorCloseFailure,
    _InitializationFailure,
    _PathReview,
    _close_fd_or_raise,
)
from test_initialization import add_collection_url


class InitializationApplyPublicSeamTests(unittest.TestCase):
    @contextmanager
    def initialized_roots(self):
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory).resolve()
            plan = plan_project_initialization(collection, project, "base")
            yield collection, project, plan

    def test_exact_reviewed_binding_is_created_exclusively(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory).resolve()
            plan = plan_project_initialization(collection, project, "base")

            result = apply_project_initialization(collection, project, "base", plan.plan_id)
            binding = project / "skill-collection.toml"

            self.assertEqual(binding.read_text(encoding="utf-8"), plan.binding_content)
            self.assertEqual(stat.S_IMODE(binding.stat().st_mode) & 0o077, 0)
            self.assertEqual(tuple(project.iterdir()), (binding,))

        self.assertEqual(
            result,
            InitializationResult(
                "created", plan.plan_id, Location("project", "skill-collection.toml"),
                plan.binding_digest, (), None,
            ),
        )

    def test_stale_plan_and_existing_destination_are_blocked_without_mutation(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory).resolve()
            stale = apply_project_initialization(collection, project, "base", "stale")
            self.assertFalse((project / "skill-collection.toml").exists())
            plan = plan_project_initialization(collection, project, "base")
            (project / "skill-collection.toml").write_bytes(b"owned\n")
            existing = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual((project / "skill-collection.toml").read_bytes(), b"owned\n")

        self.assertEqual(stale.status, "blocked")
        self.assertEqual(stale.issues[0].code, "initialization.stale_plan")
        self.assertEqual(existing.status, "blocked")
        self.assertEqual(existing.issues[0].code, "initialization.binding_exists")

    def test_result_types_are_frozen(self) -> None:
        cleanup = InitializationCleanupReport(True, False, (), (), ())
        result = InitializationResult("failed", None, None, None, (), cleanup)
        with self.assertRaises(FrozenInstanceError):
            result.status = "created"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            cleanup.attempted = False  # type: ignore[misc]

    def test_cli_requires_exact_apply_handshake_and_defaults_to_read_only(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory).resolve()
            base = ["init-project", "--collection-root", str(collection), "--project-root", str(project), "--profile", "base"]
            dry = io.StringIO()
            self.assertEqual(main(base, stdout=dry, stderr=io.StringIO()), 0)
            self.assertFalse((project / "skill-collection.toml").exists())
            plan_id = json.loads(dry.getvalue())["result"]["plan_id"]
            applied = io.StringIO()
            self.assertEqual(main([*base, "--apply", "--plan-id", plan_id], stdout=applied, stderr=io.StringIO()), 0)

        self.assertEqual(json.loads(applied.getvalue())["result"]["status"], "created")
        for arguments in ([*base, "--apply"], [*base, "--plan-id", "x"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(main(arguments, stdout=io.StringIO(), stderr=io.StringIO()), 2)

    def test_apply_uses_two_fresh_public_plans_and_rejects_a_changed_second_review(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            calls = 0

            def changing_plan(*arguments):
                nonlocal calls
                calls += 1
                current = plan_project_initialization(*arguments)
                if calls == 1:
                    return current
                catalog = collection / "catalog.toml"
                catalog.write_text(
                    catalog.read_text(encoding="utf-8").replace("revision = \"" + "0" * 40, "revision = \"" + "1" * 40),
                    encoding="utf-8",
                )
                return plan_project_initialization(*arguments)

            with patch("skill_collection._initialization_transaction.plan_project_initialization", side_effect=changing_plan):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(calls, 2)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.issues[0].code, "initialization.stale_plan")
            self.assertEqual(list(project.iterdir()), [])

    def test_unsupported_capability_blocks_before_any_creation(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction.containment_capability",
            return_value="unsupported",
        ):
            result = apply_project_initialization(collection, project, "base", plan.plan_id)
            remaining = list(project.iterdir())

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.issues[0].code, "initialization.containment_unsupported")
        self.assertEqual(remaining, [])

    def test_final_project_symlink_is_bound_to_its_identity_text_and_target(self) -> None:
        with self.initialized_roots() as (collection, canonical, _), tempfile.TemporaryDirectory() as parent:
            lexical = Path(parent).resolve() / "project-link"
            lexical.symlink_to(canonical)
            plan = plan_project_initialization(collection, lexical, "base")
            result = apply_project_initialization(collection, lexical, "base", plan.plan_id)
            created = (canonical / "skill-collection.toml").is_file()

        self.assertEqual(result.status, "created")
        self.assertTrue(created)

    def test_root_rename_after_temporary_creation_fails_and_cleans_only_original(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            renamed = project.with_name(project.name + "-renamed")
            real_token = secrets.token_hex
            invoked = False

            def rename_during_creation(size: int) -> str:
                nonlocal invoked
                if not invoked:
                    invoked = True
                    project.rename(renamed)
                    project.mkdir()
                return real_token(size)

            with patch("skill_collection._initialization_transaction.secrets.token_hex", side_effect=rename_during_creation):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.issues[0].code, "initialization.precondition_changed")
            self.assertEqual(list(project.iterdir()), [])
            self.assertEqual(list(renamed.iterdir()), [])
            renamed.rmdir()

    def test_parent_rename_after_temporary_creation_never_mutates_replacement_tree(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as outer:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            outer_path = Path(outer).resolve()
            parent = outer_path / "projects"
            parent.mkdir()
            project = parent / "project"
            project.mkdir()
            plan = plan_project_initialization(collection, project, "base")
            old_parent = outer_path / "old-projects"
            real_token = secrets.token_hex
            invoked = False

            def rename_parent(size: int) -> str:
                nonlocal invoked
                if not invoked:
                    invoked = True
                    parent.rename(old_parent)
                    parent.mkdir()
                    (parent / "project").mkdir()
                return real_token(size)

            with patch("skill_collection._initialization_transaction.secrets.token_hex", side_effect=rename_parent):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(list((parent / "project").iterdir()), [])
            self.assertEqual(list((old_parent / "project").iterdir()), [])

    def test_higher_ancestor_rename_after_creation_is_detected_and_cleaned(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as outer:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            outer_path = Path(outer).resolve()
            ancestor = outer_path / "work"
            project = ancestor / "projects" / "project"
            project.mkdir(parents=True)
            plan = plan_project_initialization(collection, project, "base")
            renamed = outer_path / "old-work"
            real_token = secrets.token_hex
            invoked = False

            def rename_ancestor(size: int) -> str:
                nonlocal invoked
                if not invoked:
                    invoked = True
                    ancestor.rename(renamed)
                    project.mkdir(parents=True)
                return real_token(size)

            with patch("skill_collection._initialization_transaction.secrets.token_hex", side_effect=rename_ancestor):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.issues[0].code, "initialization.precondition_changed")
            self.assertEqual(list(project.iterdir()), [])
            self.assertEqual(list((renamed / "projects" / "project").iterdir()), [])

    def test_lexical_project_symlink_retarget_after_creation_cannot_publish(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as outer:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            root = Path(outer).resolve()
            original = root / "original"
            replacement = root / "replacement"
            original.mkdir()
            replacement.mkdir()
            lexical = root / "project"
            lexical.symlink_to(original.name)
            plan = plan_project_initialization(collection, lexical, "base")
            real_token = secrets.token_hex

            def retarget(size: int) -> str:
                lexical.unlink()
                lexical.symlink_to(replacement.name)
                return real_token(size)

            with patch("skill_collection._initialization_transaction.secrets.token_hex", side_effect=retarget):
                result = apply_project_initialization(collection, lexical, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(list(original.iterdir()), [])
            self.assertEqual(list(replacement.iterdir()), [])

    def test_competing_binding_at_publication_is_preserved(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_link = os.link

            def competitor(*args, **kwargs):
                (project / "skill-collection.toml").write_bytes(b"competitor\n")
                return real_link(*args, **kwargs)

            with patch("skill_collection._initialization_transaction.containment_capability", return_value="supported"), patch("skill_collection._initialization_transaction.os.link", side_effect=competitor):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.issues[0].code, "initialization.precondition_changed")
            self.assertEqual((project / "skill-collection.toml").read_bytes(), b"competitor\n")
            self.assertEqual([item.name for item in project.iterdir()], ["skill-collection.toml"])

    def test_file_sync_failure_returns_failed_and_removes_the_temporary_file(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction.directory_fsync_capability",
            return_value="supported",
        ), patch(
            "skill_collection._initialization_transaction.os.fsync",
            side_effect=OSError("sync failed"),
        ):
            result = apply_project_initialization(collection, project, "base", plan.plan_id)
            remaining = list(project.iterdir())

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.issues[0].code, "initialization.file_fsync_failed")
        self.assertTrue(result.cleanup.attempted)
        self.assertEqual(remaining, [])

    def test_directory_sync_failure_after_publication_cleans_both_links(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_fsync = os.fsync
            calls = 0

            def fail_first_directory_sync(fd: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory sync failed")
                real_fsync(fd)

            with patch("skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.os.fsync",
                side_effect=fail_first_directory_sync,
            ):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.issues[0].code, "initialization.directory_fsync_failed")
            self.assertTrue(result.cleanup.removed_binding)
            self.assertEqual(result.cleanup.remaining_objects, ())
            self.assertEqual(list(project.iterdir()), [])

    def test_concurrently_disappeared_owned_binding_remains_reported(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_fsync = os.fsync
            calls = 0

            def fail_after_removing_published_binding(fd: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    (project / "skill-collection.toml").unlink()
                    raise OSError("directory sync failed")
                real_fsync(fd)

            with patch("skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.os.fsync",
                side_effect=fail_after_removing_published_binding,
            ):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertFalse(result.cleanup.removed_binding)
            self.assertEqual(result.cleanup.remaining_objects, (Location("project", "skill-collection.toml"),))
            self.assertEqual(result.cleanup.issues[0].code, "initialization.cleanup_identity_changed")

    def test_cleanup_never_claims_removal_when_unlink_fails(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction.containment_capability",
            return_value="supported",
        ), patch(
            "skill_collection._initialization_transaction.directory_fsync_capability",
            return_value="supported",
        ), patch(
            "skill_collection._initialization_transaction.os.fsync",
            side_effect=OSError("file sync failed"),
        ), patch(
            "skill_collection._initialization_transaction.os.unlink",
            side_effect=OSError("remove failed"),
        ):
            result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.cleanup.removed_temporary_files, ())
            self.assertEqual(len(result.cleanup.remaining_objects), 1)
            self.assertEqual(result.cleanup.issues[0].code, "initialization.cleanup_remove_failed")
            self.assertEqual(len(list(project.iterdir())), 1)

    def test_interruption_after_creation_preserves_interruption_and_attaches_cleanup(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction.os.write",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                apply_project_initialization(collection, project, "base", plan.plan_id)

            cleanup = raised.exception.initialization_cleanup_report
            self.assertTrue(cleanup.attempted)
            self.assertEqual(cleanup.remaining_objects, ())
            self.assertEqual(list(project.iterdir()), [])

    def test_preexisting_temporary_names_are_not_adopted_and_retry_is_bounded(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            orphan = project / (".skill-collection.toml.tmp-" + "a" * 32)
            orphan.write_bytes(b"owned\n")
            with patch("skill_collection._initialization_transaction.secrets.token_hex", return_value="a" * 32):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.issues[0].code, "initialization.temporary_unavailable")
            self.assertEqual(orphan.read_bytes(), b"owned\n")

    def test_restrictive_umask_may_reduce_but_never_broaden_permissions(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            previous = os.umask(0o377)
            try:
                result = apply_project_initialization(collection, project, "base", plan.plan_id)
            finally:
                os.umask(previous)
            mode = stat.S_IMODE((project / "skill-collection.toml").stat().st_mode)

        self.assertEqual(result.status, "created")
        self.assertEqual(mode & 0o077, 0)
        self.assertEqual(mode, 0o400)

    def test_success_uses_no_replace_rename_chmod_directory_link_or_network_seam(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction.containment_capability",
            return_value="supported",
        ), patch(
            "skill_collection._initialization_transaction.os.rename",
            side_effect=AssertionError("rename forbidden"),
        ), patch(
            "skill_collection._initialization_transaction.os.replace",
            side_effect=AssertionError("replace forbidden"),
        ), patch(
            "skill_collection._initialization_transaction.os.chmod",
            side_effect=AssertionError("chmod forbidden"),
        ), patch(
            "skill_collection._initialization_transaction.os.mkdir",
            side_effect=AssertionError("directory creation forbidden"),
        ), patch(
            "skill_collection._initialization_transaction.os.symlink",
            side_effect=AssertionError("symlink creation forbidden"),
        ):
            result = apply_project_initialization(collection, project, "base", plan.plan_id)

        self.assertEqual(result.status, "created")

    def test_cli_apply_text_is_deterministic_and_contains_no_absolute_roots(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main([
                "init-project", "--collection-root", str(collection),
                "--project-root", str(project), "--profile", "base",
                "--apply", "--plan-id", plan.plan_id, "--format", "text",
            ], stdout=stdout, stderr=stderr)
            rendered = stdout.getvalue()

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(rendered.startswith("Project initialization apply: created\n"))
        self.assertTrue(rendered.endswith("Cleanup:\nNone.\n"))
        self.assertNotIn(str(collection), rendered)
        self.assertNotIn(str(project), rendered)

    def test_all_opened_descriptors_close_and_only_temporary_creation_is_write_capable(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_open = os.open
            real_close = os.close
            real_link = os.link
            outstanding: set[int] = set()
            write_flags: list[int] = []

            def tracked_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                outstanding.add(fd)
                flags = args[1]
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    write_flags.append(flags)
                return fd

            def tracked_close(fd: int):
                result = real_close(fd)
                outstanding.discard(fd)
                return result

            with patch("skill_collection._initialization_transaction.containment_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.os.open", side_effect=tracked_open
            ), patch("skill_collection._initialization_transaction.os.close", side_effect=tracked_close):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

        self.assertEqual(result.status, "created")
        self.assertEqual(outstanding, set())
        self.assertEqual(len(write_flags), 1)
        self.assertTrue(write_flags[0] & os.O_CREAT)
        self.assertTrue(write_flags[0] & os.O_EXCL)
        self.assertTrue(write_flags[0] & os.O_NOFOLLOW)

    def test_path_capture_follows_the_two_public_plan_reviews(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            events: list[str] = []
            real_plan = plan_project_initialization
            real_capture = _PathReview.capture

            def observed_plan(*arguments):
                events.append("plan")
                return real_plan(*arguments)

            def observed_capture(path):
                events.append("capture")
                return real_capture(path)

            with patch("skill_collection._initialization_transaction.plan_project_initialization", side_effect=observed_plan), patch(
                "skill_collection._initialization_transaction._PathReview.capture", side_effect=observed_capture
            ):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

        self.assertEqual(result.status, "created")
        self.assertEqual(events[:3], ["plan", "plan", "capture"])

    def test_failure_immediately_after_publication_cleans_the_final_binding(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction._identity_name",
            side_effect=OSError("identity inspection failed"),
        ):
            result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertTrue(result.cleanup.removed_binding)
            self.assertEqual(result.cleanup.remaining_objects, ())
            self.assertEqual(list(project.iterdir()), [])

    def test_first_plan_exception_and_interruption_do_not_retain_path_descriptors(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            for failure in (RuntimeError("first plan"), KeyboardInterrupt()):
                with self.subTest(failure=type(failure).__name__), patch(
                    "skill_collection._initialization_transaction.plan_project_initialization", side_effect=failure
                ), patch(
                    "skill_collection._initialization_transaction._PathReview.capture",
                    side_effect=AssertionError("capture must follow planning"),
                ):
                    with self.assertRaises(type(failure)):
                        apply_project_initialization(collection, project, "base", plan.plan_id)

    def test_capability_and_second_plan_exceptions_precede_path_capture(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            for phase, failure in (
                ("capability", RuntimeError("capability")),
                ("capability", KeyboardInterrupt()),
                ("second-plan", RuntimeError("second plan")),
                ("second-plan", KeyboardInterrupt()),
            ):
                with self.subTest(phase=phase, failure=type(failure).__name__), patch(
                    "skill_collection._initialization_transaction._PathReview.capture",
                    side_effect=AssertionError("capture must follow final planning"),
                ):
                    if phase == "capability":
                        capability = patch(
                            "skill_collection._initialization_transaction.containment_capability",
                            side_effect=failure,
                        )
                        plans = None
                    else:
                        capability = patch(
                            "skill_collection._initialization_transaction.containment_capability",
                            return_value="supported",
                        )
                        plans = patch(
                            "skill_collection._initialization_transaction.plan_project_initialization",
                            side_effect=(plan, failure),
                        )
                    with capability:
                        if plans is None:
                            with self.assertRaises(type(failure)):
                                apply_project_initialization(collection, project, "base", plan.plan_id)
                        else:
                            with plans, self.assertRaises(type(failure)):
                                apply_project_initialization(collection, project, "base", plan.plan_id)

    def test_close_failure_does_not_mask_primary_interruption_after_creation(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_close = os.close
            writing = False

            def close_with_secondary_failure(fd: int):
                nonlocal writing
                if writing:
                    writing = False
                    real_close(fd)
                    raise OSError("close failed")
                return real_close(fd)

            def interrupted_write(fd: int, data: bytes) -> int:
                nonlocal writing
                writing = True
                raise KeyboardInterrupt()

            with patch("skill_collection._initialization_transaction.containment_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"
            ), patch("skill_collection._initialization_transaction.os.write", side_effect=interrupted_write), patch(
                "skill_collection._initialization_transaction.os.close", side_effect=close_with_secondary_failure
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(list(project.iterdir()), [])
            self.assertIn(
                "initialization.cleanup_descriptor_close_failed",
                [issue.code for issue in raised.exception.initialization_cleanup_report.issues],
            )

    def test_final_anchor_close_failure_retains_project_descriptor_for_cleanup(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_open = os.open
            real_close = os.close
            real_link = os.link
            anchor_fd: int | None = None
            binding_published = False

            def capture_open(*arguments, **kwargs):
                nonlocal anchor_fd
                fd = real_open(*arguments, **kwargs)
                if anchor_fd is None:
                    anchor_fd = fd
                return fd

            def observe_link(*arguments, **kwargs):
                nonlocal binding_published
                result = real_link(*arguments, **kwargs)
                binding_published = True
                return result

            def fail_final_anchor_close(fd: int):
                result = real_close(fd)
                if binding_published and fd == anchor_fd:
                    raise OSError("anchor close failed")
                return result

            with patch("skill_collection._initialization_transaction.containment_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"
            ), patch("skill_collection._initialization_transaction.os.open", side_effect=capture_open), patch(
                "skill_collection._initialization_transaction.os.link", side_effect=observe_link
            ), patch("skill_collection._initialization_transaction.os.close", side_effect=fail_final_anchor_close):
                with self.assertRaises(OSError) as raised:
                    apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(list(project.iterdir()), [])
            self.assertIn(
                "initialization.cleanup_descriptor_close_failed",
                [issue.code for issue in raised.exception.initialization_cleanup_report.issues],
            )

    def test_precreation_anchor_close_failure_still_closes_project_descriptor(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_open = os.open
            real_close = os.close
            capture_open_count = len(project.parts)
            open_count = 0
            anchor_fd: int | None = None
            project_fd: int | None = None
            project_identity = (project.stat().st_dev, project.stat().st_ino)
            closed: list[int] = []

            def fail_first_review_open(*arguments, **kwargs):
                nonlocal anchor_fd, open_count, project_fd
                open_count += 1
                if open_count > capture_open_count:
                    raise OSError("review open failed")
                fd = real_open(*arguments, **kwargs)
                if anchor_fd is None:
                    anchor_fd = fd
                metadata = os.fstat(fd)
                if (metadata.st_dev, metadata.st_ino) == project_identity:
                    project_fd = fd
                return fd

            def fail_anchor_close(fd: int):
                closed.append(fd)
                result = real_close(fd)
                if fd == anchor_fd:
                    raise OSError("anchor close failed")
                return result

            with patch("skill_collection._initialization_transaction.containment_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"
            ), patch("skill_collection._initialization_transaction.os.open", side_effect=fail_first_review_open), patch(
                "skill_collection._initialization_transaction.os.close", side_effect=fail_anchor_close
            ):
                with self.assertRaises(OSError):
                    apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertIn(project_fd, closed)
            self.assertEqual(list(project.iterdir()), [])

    def test_precreation_path_review_close_failure_is_unexpected(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction._PathReview.verify",
            side_effect=_InitializationFailure("initialization.precondition_changed"),
        ), patch(
            "skill_collection._initialization_transaction._PathReview.close",
            side_effect=OSError("close failed"),
        ):
            with self.assertRaises(OSError):
                apply_project_initialization(collection, project, "base", plan.plan_id)

    def test_precreation_reopened_chain_close_failure_is_unexpected(self) -> None:
        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction._close_fd_or_raise",
            side_effect=_DescriptorCloseFailure("close failed"),
        ):
            with self.assertRaises(_DescriptorCloseFailure):
                apply_project_initialization(collection, project, "base", plan.plan_id)
            self.assertEqual(list(project.iterdir()), [])

    def test_precreation_capture_chain_close_failure_is_unexpected(self) -> None:
        real_close = os.close
        calls = 0

        def close_capture_descriptor(fd: int) -> None:
            nonlocal calls
            calls += 1
            real_close(fd)
            if calls == 1:
                raise OSError("capture close failed")

        with self.initialized_roots() as (collection, project, plan), patch(
            "skill_collection._initialization_transaction.containment_capability", return_value="supported"
        ), patch(
            "skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"
        ), patch(
            "skill_collection._initialization_transaction.os.close", side_effect=close_capture_descriptor,
        ):
            with self.assertRaises(OSError):
                apply_project_initialization(collection, project, "base", plan.plan_id)
            self.assertEqual(list(project.iterdir()), [])

    def test_lexical_capture_final_descriptor_close_failure_is_unexpected(self) -> None:
        with self.initialized_roots() as (collection, canonical, _), tempfile.TemporaryDirectory() as parent:
            lexical = Path(parent).resolve() / "project-link"
            lexical.symlink_to(canonical)
            plan = plan_project_initialization(collection, lexical, "base")
            real_readlink = os.readlink
            after_lexical_read = False

            def observe_lexical_read(path, *arguments, **kwargs):
                nonlocal after_lexical_read
                result = real_readlink(path, *arguments, **kwargs)
                if Path(path) == lexical:
                    after_lexical_read = True
                return result

            def fail_final_lexical_close(fd: int) -> None:
                if after_lexical_read:
                    raise _DescriptorCloseFailure("lexical capture close failed")
                _close_fd_or_raise(fd)

            with patch("skill_collection._initialization_transaction.containment_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"
            ), patch("skill_collection._initialization_transaction.os.readlink", side_effect=observe_lexical_read), patch(
                "skill_collection._initialization_transaction._close_fd_or_raise",
                side_effect=fail_final_lexical_close,
            ):
                with self.assertRaises(_DescriptorCloseFailure):
                    apply_project_initialization(collection, lexical, "base", plan.plan_id)
            self.assertEqual(list(canonical.iterdir()), [])

    def test_cleanup_reports_reachability_descriptor_close_failure(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_fsync = os.fsync
            cleanup_review = False
            fsync_calls = 0

            def fail_directory_sync(fd: int) -> None:
                nonlocal cleanup_review, fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    cleanup_review = True
                    raise OSError("directory sync failed")
                real_fsync(fd)

            def fail_cleanup_review_close(fd: int) -> None:
                if cleanup_review:
                    raise _DescriptorCloseFailure("cleanup review close failed")
                _close_fd_or_raise(fd)

            with patch("skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.os.fsync", side_effect=fail_directory_sync
            ), patch(
                "skill_collection._initialization_transaction._close_fd_or_raise",
                side_effect=fail_cleanup_review_close,
            ):
                result = apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertIn(
                "initialization.cleanup_descriptor_close_failed",
                [issue.code for issue in result.cleanup.issues],
            )

    def test_post_publication_final_verification_close_failure_cleans_and_reraises(self) -> None:
        with self.initialized_roots() as (collection, project, plan):
            real_link = os.link
            published = False

            def publish(*arguments, **kwargs):
                nonlocal published
                result = real_link(*arguments, **kwargs)
                published = True
                return result

            def fail_final_verification_close(fd: int) -> None:
                if published:
                    raise _DescriptorCloseFailure("final verification close failed")
                _close_fd_or_raise(fd)

            with patch("skill_collection._initialization_transaction.containment_capability", return_value="supported"), patch(
                "skill_collection._initialization_transaction.directory_fsync_capability", return_value="supported"
            ), patch("skill_collection._initialization_transaction.os.link", side_effect=publish), patch(
                "skill_collection._initialization_transaction._close_fd_or_raise",
                side_effect=fail_final_verification_close,
            ):
                with self.assertRaises(_DescriptorCloseFailure) as raised:
                    apply_project_initialization(collection, project, "base", plan.plan_id)

            self.assertTrue(raised.exception.initialization_cleanup_report.attempted)
            self.assertEqual(list(project.iterdir()), [])

    def test_cli_initialization_interruption_and_unexpected_failure_are_sanitized(self) -> None:
        cleanup = InitializationCleanupReport(
            True, False, (), (Location("project", ".skill-collection.toml.tmp-opaque"),),
            (ValidationIssue(
                "initialization.cleanup_remove_failed", "Cleanup could not remove an invocation-created object.",
                Location("project", ".skill-collection.toml.tmp-opaque"),
            ),),
        )
        base = ["init-project", "--project-root", "/private/project", "--profile", "base", "--apply", "--plan-id", "x"]
        for failure, expected_code, expected_exit, expected_message in (
            (KeyboardInterrupt(), "system.interrupted", 130, "Project initialization was interrupted."),
            (RuntimeError("secret"), "system.unexpected", 3, "An unexpected system failure occurred."),
        ):
            with self.subTest(expected_code=expected_code):
                failure.initialization_cleanup_report = cleanup
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch("skill_collection.cli.apply_project_initialization", side_effect=failure):
                    code = main(base, stdout=stdout, stderr=stderr)
                payload = json.loads(stderr.getvalue())
                self.assertEqual(code, expected_exit)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(payload["error"], {"code": expected_code, "message": expected_message})
                self.assertNotIn("secret", stderr.getvalue())
                self.assertNotIn("/private/project", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
