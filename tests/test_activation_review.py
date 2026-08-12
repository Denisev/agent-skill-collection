from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from checkpoint3_support import add_discovered_skills, tree_contents, valid_collection, write_binding

from skill_collection import (
    ActivationRecord,
    Location,
    ManagedLink,
    prepare_activation,
    serialize_activation_record,
)


class ActivationRecordPublicSeamTests(unittest.TestCase):
    def test_canonical_record_uses_applied_plan_id_and_round_trips_with_tomllib(self) -> None:
        record = ActivationRecord(
            version=1,
            activation_id="sha256:" + "a" * 64,
            applied_plan_id="sha256:" + "b" * 64,
            binding=Location("project", "skill-collection.toml"),
            binding_digest="sha256:" + "c" * 64,
            collection_revision="0" * 40,
            profile="base",
            managed_links=(
                ManagedLink(
                    Location("project", ".agents/skills/alpha"),
                    Location("collection", 'skills/alpha "quoted"'),
                ),
            ),
            created_directories=(
                Location("project", ".agents"),
                Location("project", ".agents/skills"),
            ),
        )

        rendered = serialize_activation_record(record)

        expected = (
            'version = 1\n'
            f'activation_id = "sha256:{"a" * 64}"\n'
            f'applied_plan_id = "sha256:{"b" * 64}"\n'
            f'binding_digest = "sha256:{"c" * 64}"\n'
            f'collection_revision = "{"0" * 40}"\n'
            'profile = "base"\n'
            'created_directories = [".agents", ".agents/skills"]\n'
            '[binding]\n'
            'root = "project"\n'
            'path = "skill-collection.toml"\n'
            '\n[[managed_links]]\n'
            'location_root = "project"\n'
            'location_path = ".agents/skills/alpha"\n'
            'target_root = "collection"\n'
            'target_path = "skills/alpha \\"quoted\\""\n'
        ).encode("utf-8")
        self.assertEqual(rendered, expected)
        self.assertEqual(
            tomllib.loads(rendered.decode("utf-8"))["managed_links"][0]["target_path"],
            'skills/alpha "quoted"',
        )
        with self.assertRaises(FrozenInstanceError):
            record.profile = "changed"  # type: ignore[misc]

    def test_canonical_record_rejects_unicode_surrogates(self) -> None:
        record = ActivationRecord(
            version=1,
            activation_id="sha256:" + "a" * 64,
            applied_plan_id="sha256:" + "b" * 64,
            binding=Location("project", "skill-collection.toml"),
            binding_digest="sha256:" + "c" * 64,
            collection_revision="0" * 40,
            profile="bad\ud800value",
            managed_links=(),
            created_directories=(),
        )

        with self.assertRaises(ValueError):
            serialize_activation_record(record)

    def test_canonical_record_rejects_strings_that_cannot_round_trip_as_toml(self) -> None:
        record = ActivationRecord(
            version=1,
            activation_id="sha256:" + "a" * 64,
            applied_plan_id="sha256:" + "b" * 64,
            binding=Location("project", "skill-collection.toml"),
            binding_digest="sha256:" + "c" * 64,
            collection_revision="0" * 40,
            profile="bad\x7fvalue",
            managed_links=(),
            created_directories=(),
        )

        with self.assertRaises(ValueError):
            serialize_activation_record(record)

    def test_canonical_record_orders_directories_and_links(self) -> None:
        record = ActivationRecord(
            version=1,
            activation_id="sha256:" + "a" * 64,
            applied_plan_id="sha256:" + "b" * 64,
            binding=Location("project", "skill-collection.toml"),
            binding_digest="sha256:" + "c" * 64,
            collection_revision="0" * 40,
            profile="base",
            managed_links=(
                ManagedLink(Location("project", "z/child/link"), Location("collection", "z")),
                ManagedLink(Location("project", "a/link"), Location("collection", "a")),
            ),
            created_directories=(
                Location("project", "z/child"),
                Location("project", "z"),
                Location("project", "a"),
            ),
        )

        rendered = serialize_activation_record(record).decode("utf-8")

        self.assertLess(rendered.index('"z"'), rendered.index('"z/child"'))
        self.assertLess(rendered.index('location_path = "a/link"'), rendered.index('location_path = "z/child/link"'))

    def test_serializer_rejects_structurally_invalid_records(self) -> None:
        valid = ActivationRecord(
            version=1,
            activation_id="sha256:" + "a" * 64,
            applied_plan_id="sha256:" + "b" * 64,
            binding=Location("project", "skill-collection.toml"),
            binding_digest="sha256:" + "c" * 64,
            collection_revision="0" * 40,
            profile="base",
            managed_links=(
                ManagedLink(
                    Location("project", ".agents/skills/alpha"),
                    Location("collection", "skills/alpha"),
                ),
            ),
            created_directories=(Location("project", ".agents/skills"),),
        )
        invalid_records = (
            ActivationRecord(**{**record_values(valid), "version": 2}),
            ActivationRecord(**{**record_values(valid), "activation_id": "bad"}),
            ActivationRecord(**{**record_values(valid), "binding": Location("collection", "skill-collection.toml")}),
            ActivationRecord(
                **{
                    **record_values(valid),
                    "managed_links": (
                        ManagedLink(Location("project", ".agents\\skills\\alpha"), Location("collection", "skills/alpha")),
                    ),
                }
            ),
            ActivationRecord(**{**record_values(valid), "managed_links": valid.managed_links * 2}),
            ActivationRecord(**{**record_values(valid), "created_directories": (Location("project", "unrelated"),)}),
        )

        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                serialize_activation_record(record)


class ActivationReviewPublicSeamTests(unittest.TestCase):
    def test_initial_review_is_read_only_and_has_two_canonical_identities(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            before_collection = tree_contents(collection)
            before_project = tree_contents(project)

            first = prepare_activation(collection, project)
            second = prepare_activation(collection, project)

            self.assertEqual(tree_contents(collection), before_collection)
            self.assertEqual(tree_contents(project), before_project)

        self.assertEqual(first, second)
        self.assertEqual(first.status, "ready")
        self.assertEqual(first.mode, "initial")
        self.assertRegex(first.activation_id or "", r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(first.plan_id or "", r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(first.activation_id, first.plan_id)
        self.assertEqual(first.proposed_activation_record.activation_id, first.activation_id)
        self.assertEqual(first.proposed_activation_record.applied_plan_id, first.plan_id)
        self.assertEqual(
            first.proposed_activation_record.binding_digest,
            "sha256:0e6f6d73d2e3c82e5a3182cff3a1b741d123c095ff8e367c29a3736e71536961",
        )
        self.assertEqual(
            first.activation_id,
            "sha256:266b1b2776982d90c66d3a6d0e7e6daeab998132a070fb281ee4932e4be9a9f6",
        )
        self.assertEqual(
            [action.location.relative_path for action in first.actions],
            [
                ".agent-skill-collection",
                ".agents",
                ".agents/skills",
                ".agents/skills/alpha",
                ".agent-skill-collection/activation.toml",
            ],
        )
        binding_state = next(
            item
            for item in first.filesystem_preconditions
            if item.location == Location("project", "skill-collection.toml")
        )
        self.assertEqual(binding_state.kind, "regular-file")
        self.assertTrue(binding_state.readable)
        self.assertFalse(binding_state.searchable)
        absent_state = next(
            item
            for item in first.filesystem_preconditions
            if item.location == Location("project", ".agents/skills/alpha")
        )
        self.assertEqual(absent_state.kind, "absent")
        self.assertEqual(
            (absent_state.readable, absent_state.writable, absent_state.searchable),
            (False, False, False),
        )
        with self.assertRaises(FrozenInstanceError):
            first.status = "blocked"  # type: ignore[misc]

    def test_canonical_record_supports_repeat_and_missing_link_repair(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            initial = prepare_activation(collection, project)
            (project / ".agents" / "skills").mkdir(parents=True)
            link = project / ".agents" / "skills" / "alpha"
            link.symlink_to(collection / "skills" / "alpha")
            state = project / ".agent-skill-collection"
            state.mkdir()
            record_path = state / "activation.toml"
            record_path.write_bytes(serialize_activation_record(initial.proposed_activation_record))

            before_repeat_collection = tree_contents(collection)
            before_repeat_project = tree_contents(project)
            repeated = prepare_activation(collection, project)
            self.assertEqual(tree_contents(collection), before_repeat_collection)
            self.assertEqual(tree_contents(project), before_repeat_project)
            link.unlink()
            before_repair_collection = tree_contents(collection)
            before_repair_project = tree_contents(project)
            repaired = prepare_activation(collection, project)
            self.assertEqual(tree_contents(collection), before_repair_collection)
            self.assertEqual(tree_contents(project), before_repair_project)

        self.assertEqual(repeated.status, "ready")
        self.assertEqual(repeated.mode, "repeat")
        self.assertEqual(repeated.activation_id, initial.activation_id)
        self.assertNotEqual(repeated.plan_id, initial.plan_id)
        self.assertEqual(repeated.actions, ())
        self.assertEqual(
            [item.location.relative_path for item in repeated.unchanged_links],
            [".agents/skills/alpha"],
        )
        self.assertEqual(repaired.status, "ready")
        self.assertEqual(repaired.mode, "repair")
        self.assertEqual(repaired.activation_id, initial.activation_id)
        self.assertNotEqual(repaired.plan_id, repeated.plan_id)
        self.assertEqual(
            [action.location.relative_path for action in repaired.actions],
            [".agents/skills/alpha"],
        )
        self.assertEqual(
            repaired.proposed_activation_record.applied_plan_id,
            initial.proposed_activation_record.applied_plan_id,
        )

    def test_unrecorded_matching_symlink_is_project_owned(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            destination = project / ".agents" / "skills" / "alpha"
            destination.parent.mkdir(parents=True)
            destination.symlink_to(collection / "skills" / "alpha")

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "activation.unrecorded_object",
            [issue.code for issue in result.blocking_issues],
        )

    def test_binding_digest_is_semantic_but_plan_tracks_binding_bytes(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            first = prepare_activation(collection, project)
            (project / "skill-collection.toml").write_text(
                '# formatting only\nprofile="base"\nversion=1\ntarget=".agents/skills"\n'
                '[collection]\nrevision="' + "0" * 40 + '"\nurl="file:///collection"\n',
                encoding="utf-8",
            )

            reformatted = prepare_activation(collection, project)
            (project / "skill-collection.toml").write_text(
                'version = 1\nprofile = "base"\ntarget = ".agents/skills"\nadd = ["beta"]\n\n'
                '[collection]\nurl = "file:///collection"\nrevision = "' + "0" * 40 + '"\n',
                encoding="utf-8",
            )
            changed = prepare_activation(collection, project)

        self.assertEqual(
            first.proposed_activation_record.binding_digest,
            reformatted.proposed_activation_record.binding_digest,
        )
        self.assertEqual(first.activation_id, reformatted.activation_id)
        self.assertNotEqual(first.plan_id, reformatted.plan_id)
        self.assertNotEqual(first.activation_id, changed.activation_id)
        self.assertNotEqual(first.plan_id, changed.plan_id)

    def test_noncanonical_record_blocks_and_is_not_rewritten(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            initial = prepare_activation(collection, project)
            state = project / ".agent-skill-collection"
            state.mkdir()
            record_path = state / "activation.toml"
            canonical = serialize_activation_record(initial.proposed_activation_record)
            noncanonical = b"# comment\n" + canonical
            record_path.write_bytes(noncanonical)

            result = prepare_activation(collection, project)

            self.assertEqual(record_path.read_bytes(), noncanonical)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.filesystem_preconditions, ())
        self.assertIn(
            "activation.record_noncanonical",
            [issue.code for issue in result.blocking_issues],
        )

    def test_safe_symlink_access_flags_describe_its_resolved_directory(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            initial = prepare_activation(collection, project)
            (project / ".agents" / "skills").mkdir(parents=True)
            destination = project / ".agents" / "skills" / "alpha"
            target = collection / "skills" / "alpha"
            destination.symlink_to(target)
            state = project / ".agent-skill-collection"
            state.mkdir()
            (state / "activation.toml").write_bytes(
                serialize_activation_record(initial.proposed_activation_record)
            )

            result = prepare_activation(collection, project)
            expected_access = (
                os.access(target, os.R_OK),
                os.access(target, os.W_OK),
                os.access(target, os.X_OK),
            )

        symlink_state = next(
            item
            for item in result.filesystem_preconditions
            if item.location == Location("project", ".agents/skills/alpha")
        )
        self.assertEqual(symlink_state.kind, "symlink")
        self.assertEqual(
            (symlink_state.readable, symlink_state.writable, symlink_state.searchable),
            expected_access,
        )
        self.assertEqual(
            symlink_state.resolved_location,
            Location("collection", "skills/alpha"),
        )

    def test_semantically_identical_object_replacement_is_indistinguishable(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            initial = prepare_activation(collection, project)
            (project / ".agents" / "skills").mkdir(parents=True)
            destination = project / ".agents" / "skills" / "alpha"
            destination.symlink_to(collection / "skills" / "alpha")
            state = project / ".agent-skill-collection"
            state.mkdir()
            (state / "activation.toml").write_bytes(
                serialize_activation_record(initial.proposed_activation_record)
            )
            before = prepare_activation(collection, project)
            destination.unlink()
            destination.parent.rmdir()
            destination.parent.mkdir()
            destination.symlink_to(collection / "skills" / "alpha")

            after = prepare_activation(collection, project)

        self.assertEqual(after.status, "ready")
        self.assertEqual(after.mode, "repeat")
        self.assertEqual(after.activation_id, before.activation_id)
        self.assertEqual(after.plan_id, before.plan_id)

    def test_valid_canonical_record_is_trusted_ownership_proof(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            root = Path(directory)
            source_project = root / "source-project"
            receiving_project = root / "receiving-project"
            source_project.mkdir()
            receiving_project.mkdir()
            write_binding(source_project)
            write_binding(receiving_project)
            source_review = prepare_activation(collection, source_project)

            destination = receiving_project / ".agents" / "skills" / "alpha"
            destination.parent.mkdir(parents=True)
            destination.symlink_to(collection / "skills" / "alpha")
            state = receiving_project / ".agent-skill-collection"
            state.mkdir()
            record_bytes = serialize_activation_record(
                source_review.proposed_activation_record
            )
            (state / "activation.toml").write_bytes(record_bytes)

            result = prepare_activation(collection, receiving_project)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.mode, "repeat")
        self.assertEqual(result.activation_id, source_review.activation_id)
        self.assertEqual(result.proposed_activation_record.created_directories, (
            Location("project", ".agents"),
            Location("project", ".agents/skills"),
        ))

    def test_invalid_record_identifier_format_blocks(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            initial = prepare_activation(collection, project)
            state = project / ".agent-skill-collection"
            state.mkdir()
            canonical = serialize_activation_record(initial.proposed_activation_record)
            invalid = canonical.replace(
                initial.proposed_activation_record.applied_plan_id.encode("utf-8"),
                b"not-a-plan-id",
                1,
            )
            (state / "activation.toml").write_bytes(invalid)

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "activation.record_invalid",
            [issue.code for issue in result.blocking_issues],
        )

    def test_structurally_invalid_record_returns_issue_without_raising(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            state = project / ".agent-skill-collection"
            state.mkdir()
            (state / "activation.toml").write_text(
                'version = true\nactivation_id = "sha256:' + "a" * 64 + '"\n'
                'applied_plan_id = "sha256:' + "b" * 64 + '"\n'
                'binding_digest = "sha256:' + "c" * 64 + '"\n'
                'collection_revision = "' + "0" * 40 + '"\nprofile = "base"\n'
                'created_directories = []\nmanaged_links = []\n'
                '[binding]\nroot = "project"\npath = "skill-collection.toml"\n',
                encoding="utf-8",
            )

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "activation.record_invalid",
            [issue.code for issue in result.blocking_issues],
        )

    def test_record_with_non_round_trippable_path_returns_issue_without_raising(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            state = project / ".agent-skill-collection"
            state.mkdir()
            (state / "activation.toml").write_text(
                'version = 1\nactivation_id = "sha256:' + "a" * 64 + '"\n'
                'applied_plan_id = "sha256:' + "b" * 64 + '"\n'
                'binding_digest = "sha256:' + "c" * 64 + '"\n'
                'collection_revision = "' + "0" * 40 + '"\nprofile = "base"\n'
                'created_directories = []\n[binding]\nroot = "project"\n'
                'path = "skill-collection.toml"\n\n[[managed_links]]\n'
                'location_root = "project"\nlocation_path = ".agents/skills/\\u007f"\n'
                'target_root = "collection"\ntarget_path = "skills/alpha"\n',
                encoding="utf-8",
            )

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "activation.record_invalid",
            [issue.code for issue in result.blocking_issues],
        )

    def test_unavailable_activation_record_parent_blocks_without_preconditions(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            (project / ".agent-skill-collection").symlink_to("missing")

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.filesystem_preconditions, ())
        self.assertIn(
            "activation.broken_symlink",
            [issue.code for issue in result.blocking_issues],
        )

    def test_existing_destination_containers_are_current_review_preconditions(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            (project / ".agents" / "skills").mkdir(parents=True)

            result = prepare_activation(collection, project)

        locations = {item.location for item in result.filesystem_preconditions}
        self.assertIn(Location("project", ".agents"), locations)
        self.assertIn(Location("project", ".agents/skills"), locations)

    def test_unrelated_project_file_does_not_change_review_identities(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            first = prepare_activation(collection, project)
            (project / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")

            second = prepare_activation(collection, project)

        self.assertEqual(second.activation_id, first.activation_id)
        self.assertEqual(second.plan_id, first.plan_id)

    def test_repair_does_not_claim_a_missing_unrecorded_container(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            container = project / ".agents" / "skills"
            container.mkdir(parents=True)
            initial = prepare_activation(collection, project)
            self.assertEqual(initial.proposed_activation_record.created_directories, ())
            destination = container / "alpha"
            destination.symlink_to(collection / "skills" / "alpha")
            state = project / ".agent-skill-collection"
            state.mkdir()
            (state / "activation.toml").write_bytes(
                serialize_activation_record(initial.proposed_activation_record)
            )
            destination.unlink()
            container.rmdir()
            container.parent.rmdir()

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "activation.repair_unowned_directory",
            [issue.code for issue in result.blocking_issues],
        )

    def test_activation_record_parent_cannot_escape_project(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            root = Path(directory)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            write_binding(project)
            initial = prepare_activation(collection, project)
            (outside / "activation.toml").write_bytes(
                serialize_activation_record(initial.proposed_activation_record)
            )
            (project / ".agent-skill-collection").symlink_to(
                outside, target_is_directory=True
            )

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "activation.record_outside_project",
            [issue.code for issue in result.blocking_issues],
        )

    def test_broken_and_looping_record_parents_block_without_raising(self) -> None:
        for link_kind in ("broken", "loop"):
            with self.subTest(link_kind=link_kind), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                write_binding(project)
                parent = project / ".agent-skill-collection"
                parent.symlink_to("missing" if link_kind == "broken" else parent.name)

                result = prepare_activation(collection, project)

            self.assertEqual(result.status, "blocked")
            self.assertIn(
                "activation.broken_symlink",
                [issue.code for issue in result.blocking_issues],
            )
            self.assertEqual(result.filesystem_preconditions, ())

    def test_owned_links_with_wrong_type_or_target_block(self) -> None:
        for replacement in ("file", "directory", "foreign-link", "broken-link", "loop"):
            with self.subTest(replacement=replacement), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                write_binding(project)
                initial = prepare_activation(collection, project)
                destination = project / ".agents" / "skills" / "alpha"
                destination.parent.mkdir(parents=True)
                state = project / ".agent-skill-collection"
                state.mkdir()
                (state / "activation.toml").write_bytes(
                    serialize_activation_record(initial.proposed_activation_record)
                )
                if replacement == "file":
                    destination.write_text("owned\n", encoding="utf-8")
                elif replacement == "directory":
                    destination.mkdir()
                elif replacement == "foreign-link":
                    destination.symlink_to(collection / "skills" / "beta")
                elif replacement == "broken-link":
                    destination.symlink_to(collection / "missing")
                else:
                    destination.symlink_to(destination.name)

                result = prepare_activation(collection, project)

            self.assertEqual(result.status, "blocked")

    def test_special_managed_destinations_block_with_no_ready_preconditions(self) -> None:
        for object_kind in ("fifo",):
            with self.subTest(object_kind=object_kind), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                write_binding(project)
                destination = project / ".agents" / "skills" / "alpha"
                destination.parent.mkdir(parents=True)
                os.mkfifo(destination)
                result = prepare_activation(collection, project)

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.filesystem_preconditions, ())

    def test_unreadable_paths_have_all_access_flags_false(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project, target=".locked/skills")
            locked = project / ".locked"
            locked.mkdir()
            locked.chmod(0)
            if os.access(locked, os.R_OK):
                locked.chmod(0o700)
                self.skipTest("test process can read mode-000 directories")
            try:
                result = prepare_activation(collection, project)
            finally:
                locked.chmod(0o700)

        unreadable = next(
            item
            for item in result.filesystem_preconditions
            if item.location == Location("project", ".locked/skills")
        )
        self.assertEqual(unreadable.kind, "unreadable")
        self.assertEqual(
            (unreadable.readable, unreadable.writable, unreadable.searchable),
            (False, False, False),
        )

    def test_recorded_directories_replaced_by_files_or_symlinks_block(self) -> None:
        for replacement in ("file", "symlink"):
            with self.subTest(replacement=replacement), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                write_binding(project)
                initial = prepare_activation(collection, project)
                state = project / ".agent-skill-collection"
                state.mkdir()
                (state / "activation.toml").write_bytes(
                    serialize_activation_record(initial.proposed_activation_record)
                )
                owned = project / ".agents"
                if replacement == "file":
                    owned.write_text("replacement\n", encoding="utf-8")
                else:
                    target = project / "other"
                    target.mkdir()
                    owned.symlink_to(target, target_is_directory=True)

                result = prepare_activation(collection, project)

            self.assertEqual(result.status, "blocked")

    def test_unsupported_record_version_blocks(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            initial = prepare_activation(collection, project)
            raw = serialize_activation_record(initial.proposed_activation_record).replace(
                b"version = 1\n", b"version = 2\n", 1
            )
            state = project / ".agent-skill-collection"
            state.mkdir()
            (state / "activation.toml").write_bytes(raw)

            result = prepare_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn("activation.record_invalid", [issue.code for issue in result.blocking_issues])


def record_values(record: ActivationRecord) -> dict[str, object]:
    return {
        "version": record.version,
        "activation_id": record.activation_id,
        "applied_plan_id": record.applied_plan_id,
        "binding": record.binding,
        "binding_digest": record.binding_digest,
        "collection_revision": record.collection_revision,
        "profile": record.profile,
        "managed_links": record.managed_links,
        "created_directories": record.created_directories,
    }


if __name__ == "__main__":
    unittest.main()
