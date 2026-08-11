from __future__ import annotations

import tempfile
import unittest
import shutil
import subprocess
import os
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from skill_collection import Location, ValidationIssue, validate


class ValidationContractTests(unittest.TestCase):
    def test_missing_collection_documents_return_stable_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            issues = self.validate_unchanged(Path(directory))

        self.assertTrue(all(isinstance(issue, ValidationIssue) for issue in issues))
        self.assertEqual(
            [(issue.code, issue.location) for issue in issues],
            [
                ("document.missing", Location("collection", "catalog.toml")),
                ("document.missing", Location("collection", "groups.toml")),
                ("document.missing", Location("collection", "profiles.toml")),
                ("document.missing", Location("collection", "sources.toml")),
            ],
        )

    def test_malformed_toml_returns_issue(self) -> None:
        with self.fixture("valid") as collection:
            (collection / "sources.toml").write_text("version = [", encoding="utf-8")
            issues = self.validate_unchanged(collection)

        self.assert_issue(issues, "toml.malformed", "collection", "sources.toml")

    def test_valid_nested_groups_and_additions_and_removals(self) -> None:
        with self.fixture("valid") as collection:
            issues = self.validate_unchanged(collection)

        self.assertEqual(issues, [])

    def test_invalid_source_is_reported(self) -> None:
        with self.fixture("invalid-source") as collection:
            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues, "source.invalid", "collection", "sources.toml#sources[0]"
        )

    def test_missing_skill_reference_is_reported(self) -> None:
        with self.fixture("missing-skill") as collection:
            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues, "skill.missing", "collection", "groups.toml#groups[0].skills[0]"
        )

    def test_duplicate_codex_facing_name_reports_both_catalog_locations(self) -> None:
        with self.fixture("duplicate-name") as collection:
            issues = self.validate_unchanged(collection)

        issue = self.issue(issues, "skill.name_collision")
        self.assertEqual(issue.location, Location("collection", "catalog.toml#skills[0]"))
        self.assertEqual(
            issue.related_locations,
            (Location("collection", "catalog.toml#skills[1]"),),
        )

    def test_group_cycle_is_reported(self) -> None:
        with self.fixture("group-cycle") as collection:
            issues = self.validate_unchanged(collection)

        issue = self.issue(issues, "group.cycle")
        self.assertEqual(issue.location.root, "collection")
        self.assertEqual(issue.location.relative_path, "groups.toml#groups[0]")
        self.assertTrue(issue.related_locations)

    def test_profile_inheritance_cycle_is_reported(self) -> None:
        with self.fixture("profile-cycle") as collection:
            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues, "profile.inheritance_cycle", "collection", "profiles.toml#profiles[0]"
        )

    def test_removing_absent_skill_is_reported(self) -> None:
        with self.fixture("invalid-removal") as collection:
            issues = self.validate_unchanged(collection)

        self.assert_issue(issues, "skill.remove_missing", "collection", "profiles.toml#profiles[0]")

    def test_broken_activation_symlink_is_reported(self) -> None:
        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.write_binding(project)
            target = project / ".agents" / "skills"
            target.mkdir(parents=True)
            (target / "broken").symlink_to(collection / "does-not-exist")

            issues = self.validate_unchanged(collection, project)

        self.assert_issue(
            issues, "activation.broken_symlink", "project", ".agents/skills/broken"
        )

    def test_project_owned_skill_is_not_overwritable(self) -> None:
        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.write_binding(project)
            owned = project / ".agents" / "skills" / "alpha"
            owned.mkdir(parents=True)

            issues = self.validate_unchanged(collection, project)

        self.assert_issue(
            issues, "activation.target_owned", "project", ".agents/skills/alpha"
        )

    def test_dirty_submodule_is_reported_from_real_git_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()
            self.git(upstream, "init")
            self.git(upstream, "config", "user.email", "tests@example.invalid")
            self.git(upstream, "config", "user.name", "Validation Tests")
            (upstream / "alpha").mkdir()
            (upstream / "alpha" / "SKILL.md").write_text("# Alpha\n", encoding="utf-8")
            self.git(upstream, "add", ".")
            self.git(upstream, "commit", "-m", "fixture")

            with self.fixture("valid") as collection:
                self.git(collection, "init")
                self.git(collection, "config", "user.email", "tests@example.invalid")
                self.git(collection, "config", "user.name", "Validation Tests")
                self.git(
                    collection,
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(upstream),
                    "vendor/upstream",
                )
                (collection / "sources.toml").write_text(
                    'version = 1\n\n[[sources]]\nid = "upstream"\n'
                    'kind = "git-submodule"\npath = "vendor/upstream"\n'
                    f'url = "{upstream.as_uri()}"\n',
                    encoding="utf-8",
                )
                (collection / "vendor" / "upstream" / "dirty.txt").write_text(
                    "dirty\n", encoding="utf-8"
                )

                issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues, "source.submodule_dirty", "collection", "vendor/upstream"
        )

    def test_project_binding_selecting_cyclic_profile_returns_issues(self) -> None:
        with self.fixture("profile-cycle") as collection, tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.write_binding(project, profile="one")

            issues = self.validate_unchanged(collection, project)

        self.assert_issue(
            issues,
            "profile.inheritance_cycle",
            "collection",
            "profiles.toml#profiles[0]",
        )
        self.assert_issue(
            issues,
            "profile.invalid_selection",
            "project",
            "skill-collection.toml#binding.profile",
        )

    def test_nonexistent_collection_root_returns_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            issues = self.validate_unchanged(missing)

        self.assert_issue(issues, "root.missing", "collection", ".")

    def test_nonexistent_project_root_returns_issue(self) -> None:
        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            issues = self.validate_unchanged(collection, missing)

        self.assert_issue(issues, "root.missing", "project", ".")

    def test_structurally_invalid_toml_values_return_issues(self) -> None:
        with self.fixture("valid") as collection:
            (collection / "groups.toml").write_text(
                'version = 1\n\n[[groups]]\nname = "bad"\nskills = "alpha"\n',
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues, "field.invalid", "collection", "groups.toml#groups[0].skills"
        )

    def test_missing_required_fields_return_issues(self) -> None:
        with self.fixture("invalid-source") as collection, tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "skill-collection.toml").write_text(
                'version = 1\nprofile = "missing"\n\n[collection]\nurl = "file:///collection"\n',
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection, project)

        self.assert_issue(
            issues, "field.required", "collection", "sources.toml#sources[0].path"
        )
        binding_issue = next(
            issue
            for issue in issues
            if issue.code == "field.required" and issue.location.root == "project"
        )
        self.assertEqual(
            binding_issue.location,
            Location("project", "skill-collection.toml#binding.collection.revision"),
        )

    def test_multiple_invalid_entries_have_distinct_locations(self) -> None:
        with self.fixture("valid") as collection:
            (collection / "sources.toml").write_text(
                'version = 1\n\n[[sources]]\nid = "one"\nkind = "collection"\n\n'
                '[[sources]]\nid = "two"\nkind = "collection"\n',
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection)

        locations = [
            issue.location
            for issue in issues
            if issue.code == "field.required"
        ]
        self.assertEqual(
            locations,
            [
                Location("collection", "sources.toml#sources[0].path"),
                Location("collection", "sources.toml#sources[1].path"),
            ],
        )

    def test_schema_required_format_unique_items_and_unexpected_fields(self) -> None:
        with self.fixture("valid") as collection:
            (collection / "sources.toml").write_text(
                'version = 1\n\n[[sources]]\nid = "Bad_ID"\nkind = "collection"\n'
                'path = "skills"\nunexpected = true\n',
                encoding="utf-8",
            )
            (collection / "catalog.toml").write_text(
                'version = 1\nskills = []\n', encoding="utf-8"
            )
            (collection / "profiles.toml").write_text(
                'version = 1\n\n[[profiles]]\nname = "default"\n'
                'add = ["alpha", "alpha"]\n',
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues, "field.required", "collection", "catalog.toml#collection_revision"
        )
        self.assert_issue(
            issues, "field.invalid", "collection", "sources.toml#sources[0].id"
        )
        self.assert_issue(
            issues, "field.unexpected", "collection", "sources.toml#sources[0].unexpected"
        )
        self.assert_issue(
            issues, "field.duplicate", "collection", "profiles.toml#profiles[0].add[1]"
        )

    def test_all_document_types_reject_unexpected_properties(self) -> None:
        cases = {
            "sources.toml": ("\nextra = true\n", "sources.toml#sources[0].extra"),
            "groups.toml": ("\nextra = true\n", "groups.toml#groups[1].extra"),
            "profiles.toml": ("\nextra = true\n", "profiles.toml#profiles[1].extra"),
            "catalog.toml": ("\nextra = true\n", "catalog.toml#skills[1].extra"),
        }
        for filename, (addition, location) in cases.items():
            with self.subTest(filename=filename), self.fixture("valid") as collection:
                path = collection / filename
                path.write_text(path.read_text(encoding="utf-8") + addition, encoding="utf-8")
                issues = self.validate_unchanged(collection)
                self.assert_issue(issues, "field.unexpected", "collection", location)

        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.write_binding(project, extra='unexpected = true\n')
            issues = self.validate_unchanged(collection, project)
        self.assert_issue(
            issues,
            "field.unexpected",
            "project",
            "skill-collection.toml#binding.unexpected",
        )

    def test_domain_identity_uniqueness_is_enforced(self) -> None:
        with self.fixture("valid") as collection:
            (collection / "sources.toml").write_text(
                'version = 1\n\n[[sources]]\nid = "same"\nkind = "collection"\npath = "one"\n\n'
                '[[sources]]\nid = "same"\nkind = "collection"\npath = "two"\n',
                encoding="utf-8",
            )
            (collection / "groups.toml").write_text(
                'version = 1\n\n[[groups]]\nname = "same"\nskills = ["alpha"]\n\n'
                '[[groups]]\nname = "same"\nskills = ["beta"]\n',
                encoding="utf-8",
            )
            (collection / "profiles.toml").write_text(
                'version = 1\n\n[[profiles]]\nname = "same"\nskills = ["alpha"]\n\n'
                '[[profiles]]\nname = "same"\nskills = ["beta"]\n',
                encoding="utf-8",
            )
            catalog = collection / "catalog.toml"
            catalog.write_text(
                catalog.read_text(encoding="utf-8").replace('id = "beta"', 'id = "alpha"'),
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection)

        for code in (
            "source.duplicate_id",
            "group.duplicate_name",
            "profile.duplicate_name",
            "skill.duplicate_id",
        ):
            issue = self.issue(issues, code)
            self.assertTrue(issue.related_locations)

    def test_catalog_provenance_and_source_root_are_enforced(self) -> None:
        with self.fixture("valid") as collection:
            catalog = collection / "catalog.toml"
            catalog.write_text(
                catalog.read_text(encoding="utf-8")
                .replace('source = "collection"', 'source = "missing"', 1)
                .replace('path = "skills/beta"', 'path = "../outside/beta"'),
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues, "skill.source_missing", "collection", "catalog.toml#skills[0].source"
        )
        self.assert_issue(
            issues,
            "skill.path_outside_source",
            "collection",
            "catalog.toml#skills[1].path",
        )

    def test_binding_target_must_remain_inside_project(self) -> None:
        for target_value in ("../outside", None):
            with self.subTest(target=target_value), self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = root / "project"
                outside = root / "outside"
                project.mkdir()
                outside.mkdir()
                (outside / "broken").symlink_to(root / "missing")
                target = str(outside) if target_value is None else target_value
                self.write_binding(project, target=target)

                issues = self.validate_unchanged(collection, project)

            self.assert_issue(
                issues,
                "binding.target_outside_project",
                "project",
                "skill-collection.toml#binding.target",
            )
            self.assertFalse(
                any(issue.code.startswith("activation.") for issue in issues), issues
            )

    def test_binding_skill_references_and_removals_are_validated(self) -> None:
        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "skill-collection.toml").write_text(
                'version = 1\nprofile = "base"\nadd = ["absent"]\n'
                'remove = ["beta"]\ntarget = ".agents/skills"\n\n'
                '[collection]\nurl = "file:///collection"\n'
                'revision = "0000000000000000000000000000000000000000"\n',
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection, project)

        self.assert_issue(
            issues, "skill.missing", "project", "skill-collection.toml#binding.add[0]"
        )
        self.assert_issue(
            issues,
            "skill.remove_missing",
            "project",
            "skill-collection.toml#binding.remove[0]",
        )

    def test_source_path_escape_is_rejected_before_git_inspection(self) -> None:
        for path_kind in ("absolute", "traversal", "symlink"):
            with self.subTest(path_kind=path_kind), self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
                outside = Path(directory) / "outside"
                outside.mkdir()
                if path_kind == "absolute":
                    source_path = str(outside)
                elif path_kind == "traversal":
                    source_path = "../outside"
                else:
                    (collection / "escaped").symlink_to(outside, target_is_directory=True)
                    source_path = "escaped"
                (collection / "sources.toml").write_text(
                    'version = 1\n\n[[sources]]\nid = "upstream"\n'
                    f'kind = "git-submodule"\npath = "{source_path}"\n'
                    f'url = "{outside.as_uri()}"\n',
                    encoding="utf-8",
                )

                issues = self.validate_unchanged(collection)

            self.assert_issue(
                issues,
                "source.path_outside_collection",
                "collection",
                "sources.toml#sources[0].path",
            )
            self.assertFalse(
                any(issue.code.startswith("source.submodule_") for issue in issues), issues
            )

    def test_catalog_path_cannot_escape_source_through_symlink(self) -> None:
        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside"
            outside.mkdir()
            (collection / "skills").mkdir()
            (collection / "skills" / "link").symlink_to(outside, target_is_directory=True)
            catalog = collection / "catalog.toml"
            catalog.write_text(
                catalog.read_text(encoding="utf-8").replace(
                    'path = "skills/beta"', 'path = "skills/link/beta"'
                ),
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues,
            "skill.path_outside_source",
            "collection",
            "catalog.toml#skills[1].path",
        )

    def test_existing_file_at_binding_target_root_is_reported(self) -> None:
        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.write_binding(project)
            target = project / ".agents" / "skills"
            target.parent.mkdir()
            target.write_text("project-owned\n", encoding="utf-8")

            issues = self.validate_unchanged(collection, project)

        self.assert_issue(
            issues, "activation.target_owned", "project", ".agents/skills"
        )

    def test_unregistered_git_repository_is_not_a_submodule(self) -> None:
        with self.fixture("valid") as collection:
            source = collection / "vendor" / "upstream"
            source.mkdir(parents=True)
            self.git(source, "init")
            (collection / "sources.toml").write_text(
                'version = 1\n\n[[sources]]\nid = "upstream"\n'
                'kind = "git-submodule"\npath = "vendor/upstream"\n'
                f'url = "{source.as_uri()}"\n',
                encoding="utf-8",
            )

            issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues,
            "source.submodule_invalid",
            "collection",
            "sources.toml#sources[0]",
        )

    def test_submodule_checked_out_past_parent_pin_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()
            self.git(upstream, "init")
            self.git(upstream, "config", "user.email", "tests@example.invalid")
            self.git(upstream, "config", "user.name", "Validation Tests")
            (upstream / "SKILL.md").write_text("# One\n", encoding="utf-8")
            self.git(upstream, "add", ".")
            self.git(upstream, "commit", "-m", "one")

            with self.fixture("valid") as collection:
                self.git(collection, "init")
                self.git(collection, "-c", "protocol.file.allow=always", "submodule", "add", str(upstream), "vendor/upstream")
                submodule = collection / "vendor" / "upstream"
                self.git(submodule, "config", "user.email", "tests@example.invalid")
                self.git(submodule, "config", "user.name", "Validation Tests")
                (submodule / "SKILL.md").write_text("# Two\n", encoding="utf-8")
                self.git(submodule, "add", ".")
                self.git(submodule, "commit", "-m", "two")
                (collection / "sources.toml").write_text(
                    'version = 1\n\n[[sources]]\nid = "upstream"\n'
                    'kind = "git-submodule"\npath = "vendor/upstream"\n'
                    f'url = "{upstream.as_uri()}"\n',
                    encoding="utf-8",
                )

                issues = self.validate_unchanged(collection)

        self.assert_issue(
            issues,
            "source.submodule_unpinned",
            "collection",
            "sources.toml#sources[0]",
        )

    def test_binding_target_cannot_escape_through_project_symlink(self) -> None:
        with self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / "linked").symlink_to(outside, target_is_directory=True)
            self.write_binding(project, target="linked/skills")

            issues = self.validate_unchanged(collection, project)

        self.assert_issue(
            issues,
            "binding.target_outside_project",
            "project",
            "skill-collection.toml#binding.target",
        )

    def test_binding_target_reports_broken_intermediate_symlink(self) -> None:
        for link_kind in ("broken", "loop"):
            with self.subTest(link_kind=link_kind), self.fixture("valid") as collection, tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                agents = project / ".agents"
                agents.mkdir()
                link = agents / link_kind
                link.symlink_to("missing" if link_kind == "broken" else link_kind)
                self.write_binding(project, target=f".agents/{link_kind}/skills")

                issues = self.validate_unchanged(collection, project)

            self.assert_issue(
                issues,
                "activation.broken_symlink",
                "project",
                f".agents/{link_kind}",
            )

    @staticmethod
    def git(directory: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(directory), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def write_binding(
        project: Path,
        profile: str = "base",
        target: str = ".agents/skills",
        extra: str = "",
    ) -> None:
        (project / "skill-collection.toml").write_text(
            f'version = 1\nprofile = "{profile}"\ntarget = "{target}"\n{extra}\n'
            '[collection]\nurl = "file:///collection"\n'
            'revision = "0000000000000000000000000000000000000000"\n',
            encoding="utf-8",
        )

    @staticmethod
    def issue(issues: list[ValidationIssue], code: str) -> ValidationIssue:
        matches = [issue for issue in issues if issue.code == code]
        if len(matches) != 1:
            raise AssertionError(f"expected one {code!r}, got {matches!r} from {issues!r}")
        return matches[0]

    def assert_issue(
        self, issues: list[ValidationIssue], code: str, root: str, relative_path: str
    ) -> None:
        expected = Location(root, relative_path)
        matches = [
            issue
            for issue in issues
            if issue.code == code and issue.location == expected
        ]
        self.assertEqual(len(matches), 1, f"expected {code!r} at {expected!r}: {issues!r}")

    def validate_unchanged(
        self, collection: Path, project: Path | None = None
    ) -> list[ValidationIssue]:
        before_collection = self.tree_contents(collection)
        before_project = self.tree_contents(project) if project is not None else None
        issues = validate(collection, project)
        self.assertEqual(self.tree_contents(collection), before_collection)
        if project is not None:
            self.assertEqual(self.tree_contents(project), before_project)
        return issues

    @staticmethod
    def tree_contents(root: Path) -> tuple[tuple[str, str, bytes], ...]:
        if not root.exists() and not root.is_symlink():
            return ((".", "missing", b""),)
        entries: list[tuple[str, str, bytes]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append((relative, "symlink", os.readlink(path).encode()))
            elif path.is_file():
                entries.append((relative, "file", path.read_bytes()))
            elif path.is_dir():
                entries.append((relative, "directory", b""))
        return tuple(entries)

    @staticmethod
    @contextmanager
    def fixture(name: str) -> Iterator[Path]:
        source = Path(__file__).parent / "fixtures" / name
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            shutil.copytree(source, destination, dirs_exist_ok=True)
            yield destination


if __name__ == "__main__":
    unittest.main()
