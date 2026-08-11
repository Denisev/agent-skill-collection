from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from checkpoint3_support import (
    add_discovered_skills,
    tree_contents,
    valid_collection,
    write_binding,
)
from skill_collection import (
    ActivationPlan,
    CreateDirectoryAction,
    CreateSymlinkAction,
    Location,
    plan_activation,
)


class ActivationPlanPublicSeamTests(unittest.TestCase):
    def test_collection_revision_mismatch_is_a_rooted_blocker(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            binding = project / "skill-collection.toml"
            binding.write_text(
                binding.read_text(encoding="utf-8").replace("0" * 40, "1" * 40),
                encoding="utf-8",
            )

            result = plan_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.actions, ())
        self.assertIsNone(result.proposed_activation_record)
        issue = next(
            issue
            for issue in result.blocking_issues
            if issue.code == "binding.collection_revision_mismatch"
        )
        self.assertEqual(
            issue.location,
            Location("project", "skill-collection.toml#binding.collection.revision"),
        )
        self.assertEqual(
            issue.related_locations,
            (Location("collection", "catalog.toml#collection_revision"),),
        )

    def test_ready_plan_allows_containers_and_previews_exact_created_directories(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            (project / ".agents").mkdir()
            before_collection = tree_contents(collection)
            before_project = tree_contents(project)

            first = plan_activation(collection, project)
            second = plan_activation(collection, project)

            self.assertEqual(tree_contents(collection), before_collection)
            self.assertEqual(tree_contents(project), before_project)

        self.assertIsInstance(first, ActivationPlan)
        self.assertEqual(first, second)
        self.assertEqual(first.status, "ready")
        self.assertEqual(first.blocking_issues, ())
        directory_actions = tuple(
            action for action in first.actions if isinstance(action, CreateDirectoryAction)
        )
        link_actions = tuple(
            action for action in first.actions if isinstance(action, CreateSymlinkAction)
        )
        self.assertEqual(
            [action.location.relative_path for action in directory_actions],
            [".agents/skills"],
        )
        self.assertEqual(
            [action.location.relative_path for action in link_actions],
            [".agents/skills/alpha"],
        )
        self.assertTrue(all(action.action_id for action in first.actions))
        self.assertEqual(
            first.proposed_activation_record.created_directories,
            tuple(action.location for action in directory_actions),
        )

    def test_discovery_issue_blocks_plan_and_removes_partial_preview(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha")
            project = Path(directory)
            write_binding(project)

            result = plan_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn("catalog.skill_not_discovered", [issue.code for issue in result.blocking_issues])
        self.assertEqual(result.actions, ())
        self.assertEqual(result.unchanged_links, ())
        self.assertIsNone(result.proposed_activation_record)

    def test_existing_activation_record_blocks_but_container_directory_does_not(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            state = project / ".agent-skill-collection"
            state.mkdir()
            (state / "activation.toml").write_text("version = 1\n", encoding="utf-8")

            result = plan_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn("activation.record_exists", [issue.code for issue in result.blocking_issues])

    def test_existing_exact_skill_symlink_is_unchanged(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            target = project / ".agents" / "skills"
            target.mkdir(parents=True)
            (target / "alpha").symlink_to(collection / "skills" / "alpha")

            result = plan_activation(collection, project)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.actions, ())
        self.assertEqual(
            [link.location.relative_path for link in result.unchanged_links],
            [".agents/skills/alpha"],
        )
        self.assertEqual(
            result.proposed_activation_record.managed_links,
            result.unchanged_links,
        )

    def test_non_directory_container_component_blocks_plan(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project)
            (project / ".agents").write_text("owned\n", encoding="utf-8")

            result = plan_activation(collection, project)

        self.assertEqual(result.status, "blocked")
        self.assertIn("activation.target_owned", [issue.code for issue in result.blocking_issues])
        self.assertEqual(result.actions, ())

    def test_action_id_is_stable_when_unrelated_preceding_actions_differ(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            first_project = Path(directory) / "first"
            second_project = Path(directory) / "second"
            first_project.mkdir()
            second_project.mkdir()
            write_binding(first_project)
            write_binding(second_project)
            (second_project / ".agents").mkdir()

            first = plan_activation(collection, first_project)
            second = plan_activation(collection, second_project)

        first_link = next(action for action in first.actions if isinstance(action, CreateSymlinkAction))
        second_link = next(action for action in second.actions if isinstance(action, CreateSymlinkAction))
        self.assertEqual(first_link.location, second_link.location)
        self.assertEqual(first_link.action_id, second_link.action_id)

    def test_activation_record_path_components_are_checked_before_record_lookup(self) -> None:
        for link_kind in ("broken", "loop", "escape", "file"):
            with self.subTest(link_kind=link_kind), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_discovered_skills(collection, "alpha", "beta")
                root = Path(directory)
                project = root / "project"
                outside = root / "outside"
                project.mkdir()
                outside.mkdir()
                write_binding(project)
                component = project / ".agent-skill-collection"
                if link_kind == "escape":
                    (outside / "activation.toml").write_text("outside\n", encoding="utf-8")
                    component.symlink_to(outside, target_is_directory=True)
                elif link_kind == "file":
                    component.write_text("owned\n", encoding="utf-8")
                else:
                    component.symlink_to("missing" if link_kind == "broken" else component.name)

                result = plan_activation(collection, project)

            self.assertEqual(result.status, "blocked")
            expected = (
                "activation.record_outside_project"
                if link_kind == "escape"
                else (
                    "activation.record_path_owned"
                    if link_kind == "file"
                    else "activation.broken_symlink"
                )
            )
            self.assertIn(expected, [issue.code for issue in result.blocking_issues])
            self.assertNotIn("activation.record_exists", [issue.code for issue in result.blocking_issues])

    def test_selected_skill_destination_conflict_matrix_blocks_without_preview(self) -> None:
        for object_kind in ("file", "directory", "foreign", "broken", "loop"):
            with self.subTest(object_kind=object_kind), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                write_binding(project)
                target = project / ".agents" / "skills"
                target.mkdir(parents=True)
                destination = target / "alpha"
                if object_kind == "file":
                    destination.write_text("owned\n", encoding="utf-8")
                elif object_kind == "directory":
                    destination.mkdir()
                elif object_kind == "foreign":
                    destination.symlink_to(collection / "skills" / "beta")
                elif object_kind == "broken":
                    destination.symlink_to(collection / "missing")
                else:
                    destination.symlink_to(destination.name)

                result = plan_activation(collection, project)

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.actions, ())
            self.assertIsNone(result.proposed_activation_record)

    def test_every_existing_activation_record_object_is_a_blocker(self) -> None:
        for object_kind in ("file", "directory", "symlink", "broken", "loop"):
            with self.subTest(object_kind=object_kind), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                write_binding(project)
                state = project / ".agent-skill-collection"
                state.mkdir()
                record = state / "activation.toml"
                if object_kind == "file":
                    record.write_text("record\n", encoding="utf-8")
                elif object_kind == "directory":
                    record.mkdir()
                elif object_kind == "symlink":
                    target = state / "other.toml"
                    target.write_text("record\n", encoding="utf-8")
                    record.symlink_to(target)
                elif object_kind == "broken":
                    record.symlink_to(state / "missing.toml")
                else:
                    record.symlink_to(record.name)

                result = plan_activation(collection, project)

            self.assertEqual(result.status, "blocked")
            self.assertIn("activation.record_exists", [issue.code for issue in result.blocking_issues])

    def test_inherited_profile_resolution_uses_final_additions_and_removals(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            write_binding(project, profile="default")

            result = plan_activation(collection, project)

        self.assertEqual(result.status, "ready")
        links = [
            action.location.relative_path
            for action in result.actions
            if isinstance(action, CreateSymlinkAction)
        ]
        self.assertEqual(links, [".agents/skills/beta"])


if __name__ == "__main__":
    unittest.main()
