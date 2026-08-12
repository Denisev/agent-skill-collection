from __future__ import annotations

import tempfile
import unittest
import errno
from pathlib import Path
from unittest.mock import patch
import os

from checkpoint3_support import add_discovered_skills, valid_collection, write_binding
from skill_collection import apply_activation, prepare_activation


class ActivationApplyPublicSeamTests(unittest.TestCase):
    def test_source_change_while_record_is_written_never_publishes_record(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            skill_file = collection / "skills/alpha/SKILL.md"
            real_write = os.write
            changed = False

            def change_source_then_write(fd, data):
                nonlocal changed
                if not changed:
                    skill_file.write_text("# changed during publication\n", encoding="utf-8")
                    changed = True
                return real_write(fd, data)

            with patch("os.write", side_effect=change_source_then_write):
                result = apply_activation(collection, project, review.plan_id)

        self.assertEqual(result.status, "failed")
        self.assertEqual([issue.code for issue in result.issues], ["activation.source_changed"])
        self.assertFalse((project / ".agent-skill-collection/activation.toml").exists())

    def test_cleanup_reopen_failure_closes_retained_symlink_parent_descriptor(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            parent = Path(directory)
            project = parent / "project"
            project.mkdir()
            write_binding(project)
            (project / ".agents/skills").mkdir(parents=True)
            review = prepare_activation(collection, project)
            renamed = parent / "renamed-project"
            real_symlink = os.symlink
            descriptor_count_before = len(tuple(Path("/dev/fd").iterdir()))

            def link_then_remove_project_name(target, path, *args, **kwargs):
                created = real_symlink(target, path, *args, **kwargs)
                project.rename(renamed)
                return created

            with patch("os.symlink", side_effect=link_then_remove_project_name) as symlink_mock:
                os.supports_dir_fd.add(symlink_mock)
                try:
                    result = apply_activation(collection, project, review.plan_id)
                finally:
                    os.supports_dir_fd.discard(symlink_mock)

            descriptor_count_after = len(tuple(Path("/dev/fd").iterdir()))

        self.assertEqual(result.status, "failed")
        self.assertEqual(descriptor_count_after, descriptor_count_before)
        self.assertTrue(result.cleanup.attempted)
        self.assertIn(
            ".agents/skills/alpha",
            [item.relative_path for item in result.cleanup.remaining_objects],
        )

    def test_replacing_skill_directory_during_link_creation_blocks_record_publication(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            source = collection / "skills/alpha"
            replaced = collection / "skills/reviewed-alpha"
            real_symlink = os.symlink

            def replace_source_then_link(target, path, *args, **kwargs):
                source.rename(replaced)
                source.mkdir()
                (source / "SKILL.md").write_text("# replacement\n", encoding="utf-8")
                return real_symlink(target, path, *args, **kwargs)

            with patch("os.symlink", side_effect=replace_source_then_link) as symlink_mock:
                os.supports_dir_fd.add(symlink_mock)
                try:
                    result = apply_activation(collection, project, review.plan_id)
                finally:
                    os.supports_dir_fd.discard(symlink_mock)

        self.assertEqual(result.status, "failed")
        self.assertEqual([issue.code for issue in result.issues], ["activation.source_changed"])
        self.assertFalse((project / ".agents/skills/alpha").exists())
        self.assertFalse((project / ".agent-skill-collection/activation.toml").exists())

    def test_replacing_skill_file_with_symlink_during_link_creation_blocks_record(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            skill_file = collection / "skills/alpha/SKILL.md"
            replacement = collection / "replacement-skill.md"
            replacement.write_text("# replacement\n", encoding="utf-8")
            real_symlink = os.symlink

            def replace_skill_file_then_link(target, path, *args, **kwargs):
                skill_file.unlink()
                real_symlink(str(replacement), str(skill_file))
                return real_symlink(target, path, *args, **kwargs)

            with patch("os.symlink", side_effect=replace_skill_file_then_link) as symlink_mock:
                os.supports_dir_fd.add(symlink_mock)
                try:
                    result = apply_activation(collection, project, review.plan_id)
                finally:
                    os.supports_dir_fd.discard(symlink_mock)

        self.assertEqual(result.status, "failed")
        self.assertEqual([issue.code for issue in result.issues], ["activation.source_changed"])
        self.assertFalse((project / ".agent-skill-collection/activation.toml").exists())

    def test_replacing_entire_project_during_handle_acquisition_mutates_neither_replacement(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            parent = Path(directory)
            project = parent / "project"
            project.mkdir()
            write_binding(project)
            review = prepare_activation(collection, project)
            original = parent / "reviewed-project"
            real_open = os.open
            replaced = False

            def replace_before_root_open(path, flags, *args, **kwargs):
                nonlocal replaced
                if not replaced and path == project.name and kwargs.get("dir_fd") is not None:
                    project.rename(original)
                    project.mkdir()
                    write_binding(project)
                    replaced = True
                return real_open(path, flags, *args, **kwargs)

            with patch("os.open", side_effect=replace_before_root_open) as open_mock:
                os.supports_dir_fd.add(open_mock)
                try:
                    result = apply_activation(collection, project, review.plan_id)
                finally:
                    os.supports_dir_fd.discard(open_mock)

            self.assertEqual(result.status, "blocked")
            self.assertEqual(
                [issue.code for issue in result.issues],
                ["activation.project_replaced"],
            )
            self.assertFalse((project / ".agents").exists())
            self.assertFalse((project / ".agent-skill-collection").exists())
            self.assertFalse((original / ".agents").exists())
            self.assertFalse((original / ".agent-skill-collection").exists())

    def test_result_created_links_are_rooted_locations(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            result = apply_activation(collection, project, review.plan_id)

        self.assertEqual(
            [(item.root, item.relative_path) for item in result.created_links],
            [("project", ".agents/skills/alpha")],
        )

    def test_state_directory_is_created_first_and_cleanup_removes_it_last(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            events: list[tuple[str, str]] = []
            real_mkdir = os.mkdir
            real_rmdir = os.rmdir

            def observe_mkdir(path, *args, **kwargs):
                events.append(("create", str(path)))
                return real_mkdir(path, *args, **kwargs)

            def observe_rmdir(path, *args, **kwargs):
                events.append(("remove", str(path)))
                return real_rmdir(path, *args, **kwargs)

            with patch("os.mkdir", side_effect=observe_mkdir) as mkdir_mock, patch(
                "os.rmdir", side_effect=observe_rmdir
            ) as rmdir_mock, patch("os.symlink", side_effect=OSError("stop")) as symlink_mock:
                os.supports_dir_fd.update((mkdir_mock, rmdir_mock, symlink_mock))
                try:
                    result = apply_activation(collection, project, review.plan_id)
                finally:
                    os.supports_dir_fd.difference_update((mkdir_mock, rmdir_mock, symlink_mock))

        self.assertEqual(result.status, "failed")
        creates = [path for operation, path in events if operation == "create"]
        removes = [path for operation, path in events if operation == "remove"]
        self.assertEqual(creates[:3], [".agent-skill-collection", ".agents", "skills"])
        self.assertEqual(removes[-1], ".agent-skill-collection")

    def test_regular_file_fsync_failure_blocks_before_project_creation(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            real_fsync = os.fsync

            def reject_regular_file(fd: int) -> None:
                if __import__("stat").S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "regular fsync unsupported")
                real_fsync(fd)

            with patch("os.fsync", side_effect=reject_regular_file):
                result = apply_activation(collection, project, review.plan_id)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["activation.file_fsync_unsupported"],
        )
        self.assertFalse((project / ".agent-skill-collection").exists())

    def test_replacing_reviewed_real_parent_blocks_before_redirected_write(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            (project / ".agents").mkdir()
            reviewed_parent = project / ".agents/skills"
            reviewed_parent.mkdir()
            replacement = project / "replacement"
            replacement.mkdir()
            review = prepare_activation(collection, project)
            real_symlink = os.symlink

            def replace_then_create(target, path, *args, **kwargs):
                reviewed_parent.rename(project / "reviewed-skills")
                replacement.rename(reviewed_parent)
                return real_symlink(target, path, *args, **kwargs)

            with patch("os.symlink", side_effect=replace_then_create) as symlink_mock:
                os.supports_dir_fd.add(symlink_mock)
                try:
                    result = apply_activation(collection, project, review.plan_id)
                finally:
                    os.supports_dir_fd.discard(symlink_mock)

        self.assertEqual(result.status, "failed")
        self.assertEqual([issue.code for issue in result.issues], ["activation.parent_changed"])
        self.assertFalse((reviewed_parent / "alpha").exists())
        leaked = project / "reviewed-skills/alpha"
        self.assertEqual(
            [item.relative_path for item in result.created_links],
            [".agents/skills/alpha"],
        )
        self.assertTrue(
            not leaked.exists()
            or any(
                item.relative_path == ".agents/skills/alpha"
                for item in result.cleanup.remaining_objects
            )
        )
    def test_state_and_record_action_ids_are_opaque_and_stable_as_whole_values(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            add_discovered_skills(collection, "alpha", "beta")
            first = Path(first_directory)
            second = Path(second_directory)
            write_binding(first)
            write_binding(second)
            (second / ".agents/skills").mkdir(parents=True)
            first_review = prepare_activation(collection, first)
            second_review = prepare_activation(collection, second)

        first_ids = {
            action.kind: action.action_id
            for action in first_review.actions
            if action.kind in ("create-activation-state-directory", "write-activation-record")
        }
        second_ids = {
            action.kind: action.action_id
            for action in second_review.actions
            if action.kind in ("create-activation-state-directory", "write-activation-record")
        }
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(set(first_ids), {"create-activation-state-directory", "write-activation-record"})

    def test_missing_containment_primitive_blocks_before_creation(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            os.supports_dir_fd.remove(os.mkdir)
            try:
                result = apply_activation(collection, project, review.plan_id)
            finally:
                os.supports_dir_fd.add(os.mkdir)
            self.assertEqual(result.status, "blocked")
            self.assertEqual([issue.code for issue in result.issues], ["activation.containment_unsupported"])
            self.assertFalse((project / ".agents").exists())

    def test_unsupported_directory_fsync_blocks_before_creation(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            with patch("skill_collection.activation.os.fsync", side_effect=OSError(errno.EINVAL, "unsupported")):
                result = apply_activation(collection, project, review.plan_id)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(
                [issue.code for issue in result.issues],
                ["activation.directory_fsync_unsupported"],
            )
            self.assertFalse((project / ".agents").exists())
    def test_initial_activation_applies_the_exact_review_and_writes_record(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)

            result = apply_activation(collection, project, review.plan_id)

            self.assertEqual(result.status, "applied")
            self.assertEqual(result.mode, "initial")
            self.assertEqual(result.activation_id, review.activation_id)
            self.assertEqual(result.plan_id, review.plan_id)
            self.assertEqual(
                [item.relative_path for item in result.created_directories],
                [".agent-skill-collection", ".agents", ".agents/skills"],
            )
            self.assertEqual(
                [item.relative_path for item in result.created_links],
                [".agents/skills/alpha"],
            )
            self.assertEqual(
                result.record_location.relative_path,
                ".agent-skill-collection/activation.toml",
            )
            self.assertEqual(result.issues, ())
            self.assertIsNone(result.cleanup)
            self.assertTrue((project / ".agents/skills/alpha").is_symlink())
            self.assertTrue((project / ".agent-skill-collection/activation.toml").is_file())

            repeated = prepare_activation(collection, project)
            self.assertEqual(repeated.status, "ready")
            self.assertEqual(repeated.mode, "repeat")

    def test_stale_plan_is_blocked_without_writes(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)

            result = apply_activation(collection, project, "sha256:" + "f" * 64)

            self.assertEqual(result.status, "blocked")
            self.assertIsNone(result.mode)
            self.assertIsNone(result.activation_id)
            self.assertIsNone(result.plan_id)
            self.assertEqual(result.created_directories, ())
            self.assertEqual(result.created_links, ())
            self.assertIsNone(result.record_location)
            self.assertEqual([issue.code for issue in result.issues], ["activation.stale_plan"])
            self.assertIsNone(result.cleanup)
            self.assertFalse((project / ".agents").exists())

    def test_existing_state_container_is_allowed_but_not_claimed_as_created(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            (project / ".agent-skill-collection").mkdir()
            review = prepare_activation(collection, project)

            result = apply_activation(collection, project, review.plan_id)

            self.assertEqual(result.status, "applied")
            self.assertNotIn(
                ".agent-skill-collection",
                [item.relative_path for item in result.created_directories],
            )
            self.assertTrue((project / ".agent-skill-collection/activation.toml").is_file())

    def test_symlinked_mutation_parent_blocks_before_any_creation(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            real = project / "real-agents"
            (real / "skills").mkdir(parents=True)
            (project / ".agents").symlink_to(real, target_is_directory=True)
            review = prepare_activation(collection, project)

            result = apply_activation(collection, project, review.plan_id)

            self.assertEqual(result.status, "blocked")
            self.assertEqual([issue.code for issue in result.issues], ["activation.mutation_parent_symlink"])
            self.assertFalse((project / ".agent-skill-collection").exists())

    def test_repeat_is_unchanged_and_missing_managed_link_is_repaired(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            initial_review = prepare_activation(collection, project)
            applied = apply_activation(collection, project, initial_review.plan_id)
            repeat_review = prepare_activation(collection, project)

            repeated = apply_activation(collection, project, repeat_review.plan_id)
            (project / ".agents/skills/alpha").unlink()
            repair_review = prepare_activation(collection, project)
            repaired = apply_activation(collection, project, repair_review.plan_id)

            self.assertEqual(applied.status, "applied")
            self.assertEqual(repeated.status, "unchanged")
            self.assertEqual(repeated.mode, "repeat")
            self.assertEqual(repeated.created_directories, ())
            self.assertEqual(repeated.created_links, ())
            self.assertIsNone(repeated.record_location)
            self.assertEqual(repaired.status, "applied")
            self.assertEqual(repaired.mode, "repair")
            self.assertEqual(repaired.created_directories, ())
            self.assertEqual(
                [link.relative_path for link in repaired.created_links],
                [".agents/skills/alpha"],
            )
            self.assertIsNone(repaired.record_location)

    def test_expected_failure_reports_every_creation_and_cleans_it_up(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)

            calls = 0
            real_fsync = os.fsync

            def fail_first_forward_directory_sync(fd: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("boom")
                real_fsync(fd)

            with patch("skill_collection.activation.os.fsync", side_effect=fail_first_forward_directory_sync):
                result = apply_activation(collection, project, review.plan_id)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.mode, "initial")
            self.assertEqual(
                [item.relative_path for item in result.created_directories],
                [".agent-skill-collection"],
            )
            self.assertEqual(result.created_links, ())
            self.assertEqual([issue.code for issue in result.issues], ["activation.directory_fsync_failed"])
            self.assertTrue(result.cleanup.attempted)
            self.assertEqual(result.cleanup.remaining_objects, ())
            self.assertEqual(result.cleanup.issues, ())
            self.assertFalse((project / ".agents").exists())

    def test_apply_to_blocked_review_has_the_blocked_result_invariants(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            destination = project / ".agents/skills/alpha"
            destination.parent.mkdir(parents=True)
            destination.write_text("owned", encoding="utf-8")

            result = apply_activation(collection, project, "sha256:" + "0" * 64)

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.mode)
        self.assertIsNone(result.activation_id)
        self.assertIsNone(result.plan_id)
        self.assertEqual(result.created_directories, ())
        self.assertEqual(result.created_links, ())
        self.assertIsNone(result.record_location)
        self.assertTrue(result.issues)
        self.assertIsNone(result.cleanup)

    def test_unexpected_exception_after_creation_preserves_exception_and_attaches_cleanup(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            failure = RuntimeError("original")

            with patch("skill_collection.activation.os.write", side_effect=failure):
                with self.assertRaises(RuntimeError) as raised:
                    apply_activation(collection, project, review.plan_id)

            self.assertIs(raised.exception, failure)
            cleanup = raised.exception.activation_cleanup_report
            self.assertTrue(cleanup.attempted)
            self.assertEqual(cleanup.remaining_objects, ())
            self.assertEqual(cleanup.issues, ())
            self.assertFalse((project / ".agents").exists())
            self.assertFalse((project / ".agent-skill-collection").exists())

    def test_keyboard_interrupt_after_creation_also_attempts_cleanup(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            interruption = KeyboardInterrupt()

            with patch("skill_collection.activation.os.write", side_effect=interruption):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    apply_activation(collection, project, review.plan_id)

            self.assertIs(raised.exception, interruption)
            self.assertTrue(raised.exception.activation_cleanup_report.attempted)

    def test_unremovable_temporary_file_is_the_only_temporary_name_reported(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            review = prepare_activation(collection, project)
            real_unlink = __import__("os").unlink

            def refuse_temporary(path, *args, **kwargs):
                if str(path).startswith(".activation.toml.tmp-"):
                    raise OSError("cannot remove")
                return real_unlink(path, *args, **kwargs)

            with patch("skill_collection.activation.os.write", side_effect=RuntimeError("original")):
                with patch("skill_collection.activation.os.unlink", side_effect=refuse_temporary) as unlink:
                    __import__("os").supports_dir_fd.add(unlink)
                    try:
                        with self.assertRaises(RuntimeError) as raised:
                            apply_activation(collection, project, review.plan_id)
                    finally:
                        __import__("os").supports_dir_fd.discard(unlink)

            cleanup = raised.exception.activation_cleanup_report
            temporary = [
                item.relative_path
                for item in cleanup.remaining_objects
                if ".activation.toml.tmp-" in item.relative_path
            ]
            self.assertEqual(len(temporary), 1)
            self.assertTrue(temporary[0].startswith(".agent-skill-collection/.activation.toml.tmp-"))


if __name__ == "__main__":
    unittest.main()
