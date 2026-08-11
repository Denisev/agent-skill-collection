from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from checkpoint3_support import add_discovered_skills, tree_contents, valid_collection
from skill_collection import DiscoveredSkill, ScanResult, scan


class ScanPublicSeamTests(unittest.TestCase):
    def test_scan_is_recursive_exact_and_does_not_follow_directory_symlinks(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            nested = collection / "skills" / "nested" / "gamma"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("# gamma\n", encoding="utf-8")
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "SKILL.md").write_text("# outside\n", encoding="utf-8")
            (collection / "skills" / "linked").symlink_to(outside, target_is_directory=True)
            before = tree_contents(collection)

            result = scan(collection)

            self.assertEqual(tree_contents(collection), before)

        self.assertIsInstance(result, ScanResult)
        self.assertTrue(all(isinstance(item, DiscoveredSkill) for item in result.discovered))
        self.assertEqual(
            [item.directory.relative_path for item in result.discovered],
            ["skills/alpha", "skills/beta", "skills/nested/gamma"],
        )
        self.assertEqual(
            [(item.catalog_skill_id, item.catalog_name) for item in result.discovered],
            [("alpha", "alpha"), ("beta", "beta"), (None, None)],
        )
        self.assertIn("discovery.uncataloged", [issue.code for issue in result.issues])
        self.assertNotIn("skills/linked", [item.directory.relative_path for item in result.discovered])

    def test_catalog_entry_without_discovery_is_an_issue(self) -> None:
        with valid_collection() as collection:
            add_discovered_skills(collection, "alpha")
            result = scan(collection)

        missing = [issue for issue in result.issues if issue.code == "catalog.skill_not_discovered"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].location.relative_path, "catalog.toml#skills[1].path")

    def test_broken_or_looping_source_root_returns_issues_without_raising(self) -> None:
        for link_kind in ("broken", "loop"):
            with self.subTest(link_kind=link_kind), valid_collection() as collection:
                link = collection / link_kind
                link.symlink_to("missing" if link_kind == "broken" else link_kind)
                (collection / "sources.toml").write_text(
                    'version = 1\n\n[[sources]]\nid = "collection"\nkind = "collection"\n'
                    f'path = "{link_kind}"\n',
                    encoding="utf-8",
                )

                result = scan(collection)

            self.assertTrue(result.issues)
            self.assertIn(
                "source.path_unavailable", [issue.code for issue in result.issues]
            )

    def test_source_root_directory_symlink_is_not_followed(self) -> None:
        with valid_collection() as collection:
            actual = collection / "actual-skills"
            actual.mkdir()
            (actual / "SKILL.md").write_text("# hidden\n", encoding="utf-8")
            (collection / "linked-skills").symlink_to(actual, target_is_directory=True)
            (collection / "sources.toml").write_text(
                'version = 1\n\n[[sources]]\nid = "collection"\nkind = "collection"\n'
                'path = "linked-skills"\n',
                encoding="utf-8",
            )

            result = scan(collection)

        self.assertEqual(result.discovered, ())
        self.assertIn("source.path_symlink", [issue.code for issue in result.issues])

    def test_symlinked_skill_file_is_not_discovered(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            candidate = collection / "skills" / "linked-file"
            candidate.mkdir()
            outside = Path(directory) / "SKILL.md"
            outside.write_text("# outside\n", encoding="utf-8")
            (candidate / "SKILL.md").symlink_to(outside)

            result = scan(collection)

        self.assertNotIn(
            "skills/linked-file",
            [item.directory.relative_path for item in result.discovered],
        )

    def test_multiple_catalog_entries_for_one_discovery_are_ambiguous(self) -> None:
        with valid_collection() as collection:
            add_discovered_skills(collection, "alpha", "beta")
            catalog = collection / "catalog.toml"
            catalog.write_text(
                catalog.read_text(encoding="utf-8")
                + '\n[[skills]]\nid = "other-alpha"\nname = "other-alpha"\n'
                + 'source = "collection"\npath = "skills/alpha"\n'
                + 'content_hash = "sha256:2222222222222222222222222222222222222222222222222222222222222222"\n',
                encoding="utf-8",
            )

            result = scan(collection)

        alpha = next(item for item in result.discovered if item.directory.relative_path == "skills/alpha")
        self.assertIsNone(alpha.catalog_skill_id)
        self.assertIn("discovery.ambiguous_catalog", [issue.code for issue in result.issues])

    def test_skills_root_failure_points_to_skills_root_field(self) -> None:
        for link_kind in ("broken", "loop", "symlink"):
            with self.subTest(link_kind=link_kind), valid_collection() as collection:
                skills = collection / "skills"
                skills.mkdir()
                actual = collection / "actual"
                actual.mkdir()
                link = skills / link_kind
                target = actual if link_kind == "symlink" else ("missing" if link_kind == "broken" else link_kind)
                link.symlink_to(target, target_is_directory=True)
                (collection / "sources.toml").write_text(
                    'version = 1\n\n[[sources]]\nid = "collection"\nkind = "collection"\n'
                    f'path = "skills"\nskills_root = "{link_kind}"\n',
                    encoding="utf-8",
                )

                result = scan(collection)

            source_issue = next(
                issue
                for issue in result.issues
                if issue.code in ("source.path_symlink", "source.path_unavailable")
            )
            self.assertEqual(
                source_issue.location.relative_path,
                "sources.toml#sources[0].skills_root",
            )

    def test_absolute_source_root_is_rejected_without_discovery(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "SKILL.md").write_text("# outside\n", encoding="utf-8")
            (collection / "sources.toml").write_text(
                'version = 1\n\n[[sources]]\nid = "collection"\nkind = "collection"\n'
                f'path = "{outside}"\n',
                encoding="utf-8",
            )

            result = scan(collection)

        self.assertEqual(result.discovered, ())
        self.assertIn("source.path_outside_collection", [issue.code for issue in result.issues])

    def test_traversal_source_root_is_rejected_without_discovery(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory(
            dir=collection.parent
        ) as outside_directory:
            outside = Path(outside_directory)
            (outside / "SKILL.md").write_text("# outside\n", encoding="utf-8")
            (collection / "sources.toml").write_text(
                'version = 1\n\n[[sources]]\nid = "collection"\nkind = "collection"\n'
                f'path = "../{outside.name}"\n',
                encoding="utf-8",
            )

            result = scan(collection)

        self.assertEqual(result.discovered, ())
        self.assertIn("source.path_outside_collection", [issue.code for issue in result.issues])

    def test_unreadable_directory_during_recursive_discovery_is_an_issue(self) -> None:
        with valid_collection() as collection:
            add_discovered_skills(collection, "alpha", "beta")
            locked = collection / "skills" / "locked"
            locked.mkdir()
            locked.chmod(0)
            if os.access(locked, os.R_OK):
                locked.chmod(0o700)
                self.skipTest("test process can read mode-000 directories")
            try:
                result = scan(collection)
            finally:
                locked.chmod(0o700)

        issue = next(issue for issue in result.issues if issue.code == "discovery.unreadable")
        self.assertEqual(issue.location.relative_path, "skills/locked")

    def test_non_regular_skill_file_is_not_discovered(self) -> None:
        with valid_collection() as collection:
            add_discovered_skills(collection, "alpha", "beta")
            candidate = collection / "skills" / "non-regular"
            candidate.mkdir()
            (candidate / "SKILL.md").mkdir()

            result = scan(collection)

        self.assertNotIn(
            "skills/non-regular",
            [item.directory.relative_path for item in result.discovered],
        )


if __name__ == "__main__":
    unittest.main()
