from __future__ import annotations

import io
import json
import os
import socket
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from checkpoint3_support import add_discovered_skills, tree_contents, valid_collection
from skill_collection.cli import main
from skill_collection import (
    CreateBindingAction,
    InitializationPlan,
    Location,
    prepare_activation,
    plan_project_initialization,
    scan,
    validate,
)


COLLECTION_URL = "ssh://git@github.com/example/agent-skill-collection.git"


def add_collection_url(collection: Path, url: str = COLLECTION_URL) -> None:
    catalog = collection / "catalog.toml"
    content = catalog.read_text(encoding="utf-8")
    catalog.write_text(
        content.replace(
            "version = 1\n",
            f"version = 1\ncollection_url = {json.dumps(url, ensure_ascii=False)}\n",
            1,
        ),
        encoding="utf-8",
    )


def initialize_git_metadata(
    collection: Path,
    *,
    remote: str = "ssh://local@invalid.example/ignored.git",
    credential_helper: str = "!false",
) -> None:
    subprocess.run(
        ["git", "init", "-q", str(collection)], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(collection), "remote", "add", "origin", remote],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(collection), "config", "credential.helper", credential_helper],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(collection), "add", "."], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class InitializationPlanPublicSeamTests(unittest.TestCase):
    def test_ready_plan_previews_one_exact_canonical_binding_without_writing_it(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)

            result = plan_project_initialization(collection, project, "base")

            self.assertFalse((project / "skill-collection.toml").exists())

        self.assertIsInstance(result, InitializationPlan)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.profile, "base")
        self.assertEqual(result.collection_revision, "0" * 40)
        self.assertEqual(result.collection_url, COLLECTION_URL)
        self.assertEqual(result.binding_location, Location("project", "skill-collection.toml"))
        self.assertEqual(result.binding_observation.kind, "absent")
        self.assertEqual(
            result.binding_content,
            'version = 1\nprofile = "base"\ntarget = ".agents/skills"\n\n'
            '[collection]\nurl = "ssh://git@github.com/example/agent-skill-collection.git"\n'
            f'revision = "{"0" * 40}"\n',
        )
        self.assertEqual(len(result.actions), 1)
        self.assertIsInstance(result.actions[0], CreateBindingAction)
        self.assertEqual(result.actions[0].location, result.binding_location)
        self.assertEqual(result.actions[0].precondition, "absent")
        self.assertEqual(
            result.binding_digest,
            "sha256:6edb7a886603a2727b258b72b09e9bde25c96b1f913a1e3686ac2c5fa05689d8",
        )
        self.assertEqual(
            result.actions[0].content_sha256,
            "sha256:d5a8c660f6bc807e18e4fd72027a378d332e15a567eb760f41dfe317dccd0661",
        )
        self.assertEqual(
            result.actions[0].action_id,
            "2faa2bf3fca4fa6f2322aff3ec2a767330f9d3cb4e67bbc5682f0090c2648101",
        )
        self.assertEqual(
            result.plan_id,
            "sha256:c78cd6026c05ab9d02e5ab2df430855078ac37670cac3ff88e673c2f4cd503b0",
        )
        self.assertEqual(result.blocking_issues, ())

    def test_every_existing_binding_object_blocks_without_a_partial_preview(self) -> None:
        for object_kind in ("regular-file", "directory", "symlink", "broken-symlink", "looping-symlink"):
            with self.subTest(object_kind=object_kind), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                binding = project / "skill-collection.toml"
                protected_paths = {binding}
                if object_kind == "regular-file":
                    binding.write_text("version = 1\n", encoding="utf-8")
                elif object_kind == "directory":
                    binding.mkdir()
                elif object_kind == "symlink":
                    target = project / "elsewhere"
                    target.write_text("owned\n", encoding="utf-8")
                    binding.symlink_to(target)
                    protected_paths.add(target)
                elif object_kind == "broken-symlink":
                    binding.symlink_to(project / "missing")
                else:
                    binding.symlink_to(binding.name)
                original_open = Path.open

                def guarded_open(path: Path, *args: object, **kwargs: object):
                    if path in protected_paths:
                        raise AssertionError("existing Binding object or target was opened")
                    return original_open(path, *args, **kwargs)

                with patch("pathlib.Path.open", new=guarded_open):
                    result = plan_project_initialization(collection, project, "base")

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.binding_observation.kind, object_kind)
            self.assertIn(
                "initialization.binding_exists",
                [issue.code for issue in result.blocking_issues],
            )
            self.assertIsNone(result.plan_id)
            self.assertIsNone(result.profile)
            self.assertIsNone(result.collection_revision)
            self.assertIsNone(result.collection_url)
            self.assertIsNone(result.binding_content)
            self.assertIsNone(result.binding_digest)
            self.assertEqual(result.actions, ())

    def test_collection_url_rules_are_scheme_specific_and_rooted(self) -> None:
        allowed = (
            "https://github.com/example/repository.git",
            "git://github.com/example/repository.git",
            "ssh://github.com/example/repository.git",
            "ssh://git@github.com/example/repository.git",
            "ssh://release_bot-1~x@github.com:22/example/repository.git",
            "https://127.0.0.1:1/example/repository.git",
            "git://192.168.1.10:65535/example/repository.git",
            "ssh://git@[2001:db8::1]/example/repository.git",
            "https://a.example-1.com/example/repository.git",
            "https://github.com/example/repository%20name.git",
            "https://github.com/example/repository.git/",
        )
        rejected = (
            "https://user@github.com/example/repository.git",
            "git://user@github.com/example/repository.git",
            "ssh://git:secret@github.com/example/repository.git",
            "ssh://bad%20name@github.com/example/repository.git",
            "ssh://@github.com/example/repository.git",
            "ssh://bad!name@github.com/example/repository.git",
            "git@github.com:example/repository.git",
            "file:///tmp/repository",
            "../repository",
            "https://github.com/example/repository.git?token=x",
            "https://github.com/example/repository.git#fragment",
            "https://127.0.0.999/example/repository.git",
            "https://[2001:db8::zz]/example/repository.git",
            "https://-bad.example/example/repository.git",
            "https://bad-.example/example/repository.git",
            f"https://{'a' * 64}.example/example/repository.git",
            "https://github.com:0/example/repository.git",
            "https://github.com:65536/example/repository.git",
            "https://github.com:not-a-port/example/repository.git",
            "https://github.com/example/repository%2.git",
            "https://github.com/example/repository%GG.git",
            "HTTPS://github.com/example/repository.git",
            "http://github.com/example/repository.git",
            "github.com/example/repository.git",
            "https://github.com",
            "https:///example/repository.git",
            "https://github.com:/example/repository.git",
            "https://github.com/example/répository.git",
            "https://github.com/example/repository git",
            "https://github.com/example/repository\tgit",
            "https://github.com/example/repository\ngit",
            "https://github.com/example\\repository.git",
        )
        for url in allowed:
            with self.subTest(url=url), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection, url)
                add_discovered_skills(collection, "alpha", "beta")

                result = plan_project_initialization(collection, Path(directory), "base")

            self.assertEqual(result.status, "ready")
            self.assertEqual(result.collection_url, url)
            self.assertIn(f'url = "{url}"', result.binding_content)
        for url in rejected:
            with self.subTest(url=url), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection, url)
                add_discovered_skills(collection, "alpha", "beta")

                result = plan_project_initialization(collection, Path(directory), "base")

            self.assertEqual(result.status, "blocked")
            issue = next(
                issue for issue in result.blocking_issues
                if issue.location == Location("collection", "catalog.toml#collection_url")
            )
            self.assertEqual(issue.code, "field.invalid")

    def test_fifo_and_socket_binding_destinations_block_without_being_opened(self) -> None:
        for object_kind in ("fifo", "socket"):
            with self.subTest(object_kind=object_kind), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                binding = project / "skill-collection.toml"
                listener = None
                if object_kind == "fifo":
                    os.mkfifo(binding)
                else:
                    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        listener.bind(str(binding))
                    except PermissionError:
                        listener.close()
                        continue
                try:
                    result = plan_project_initialization(collection, project, "base")
                finally:
                    if listener is not None:
                        listener.close()

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.binding_observation.kind, object_kind)
            self.assertIn(
                "initialization.binding_exists",
                [issue.code for issue in result.blocking_issues],
            )

    def test_binding_digest_is_compatible_with_activation_review(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)

            initialization = plan_project_initialization(collection, project, "base")
            (project / "skill-collection.toml").write_text(
                initialization.binding_content, encoding="utf-8"
            )
            activation = prepare_activation(collection, project)

        self.assertEqual(activation.status, "ready")
        self.assertEqual(
            activation.proposed_activation_record.binding_digest,
            initialization.binding_digest,
        )

    def test_collection_url_is_optional_for_baseline_seams_but_required_for_initialization(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)

            self.assertEqual(validate(collection), [])
            self.assertEqual(scan(collection).issues, ())
            result = plan_project_initialization(collection, project, "base")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            [
                (issue.code, issue.location)
                for issue in result.blocking_issues
                if issue.location.relative_path == "catalog.toml#collection_url"
            ],
            [("field.required", Location("collection", "catalog.toml#collection_url"))],
        )
        self.assertIsNone(result.collection_revision)
        self.assertIsNone(result.collection_url)

    def test_invalid_collection_url_type_is_a_rooted_validation_issue(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            catalog = collection / "catalog.toml"
            catalog.write_text(
                catalog.read_text(encoding="utf-8").replace(
                    "version = 1\n", "version = 1\ncollection_url = 42\n", 1
                ),
                encoding="utf-8",
            )
            add_discovered_skills(collection, "alpha", "beta")

            validation_issues = validate(collection)
            result = plan_project_initialization(collection, Path(directory), "base")

        expected = ("field.invalid", Location("collection", "catalog.toml#collection_url"))
        self.assertIn(expected, [(issue.code, issue.location) for issue in validation_issues])
        self.assertIn(expected, [(issue.code, issue.location) for issue in result.blocking_issues])

    def test_invalid_and_missing_profile_arguments_are_rooted_and_do_not_leak_selection(self) -> None:
        for profile, expected_code in (("Base", "field.invalid"), ("missing", "profile.missing")):
            with self.subTest(profile=profile), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")

                result = plan_project_initialization(collection, Path(directory), profile)

            self.assertEqual(result.status, "blocked")
            self.assertIn(
                (expected_code, Location("collection", "profiles.toml#selection")),
                [(issue.code, issue.location) for issue in result.blocking_issues],
            )
            self.assertIsNone(result.profile)
            self.assertIsNone(result.binding_content)

    def test_plan_identity_is_root_independent_and_changes_with_committed_url(self) -> None:
        with valid_collection() as first_collection, valid_collection() as second_collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(first_collection)
            add_collection_url(second_collection)
            add_discovered_skills(first_collection, "alpha", "beta")
            add_discovered_skills(second_collection, "alpha", "beta")
            first_project = Path(directory) / "first"
            second_project = Path(directory) / "second"
            first_project.mkdir()
            second_project.mkdir()

            first = plan_project_initialization(first_collection, first_project, "base")
            second = plan_project_initialization(second_collection, second_project, "base")
            catalog = second_collection / "catalog.toml"
            catalog.write_text(
                catalog.read_text(encoding="utf-8").replace(
                    COLLECTION_URL,
                    "ssh://git@code.example.com/example/agent-skill-collection.git",
                ),
                encoding="utf-8",
            )
            relocated = plan_project_initialization(second_collection, second_project, "base")

        self.assertEqual(first, second)
        self.assertEqual(first.collection_revision, relocated.collection_revision)
        self.assertNotEqual(first.binding_content, relocated.binding_content)
        self.assertNotEqual(first.binding_digest, relocated.binding_digest)
        self.assertNotEqual(first.plan_id, relocated.plan_id)

    def test_result_graph_is_frozen_and_planning_preserves_both_roots(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            before_collection = tree_contents(collection)
            before_project = tree_contents(project)

            first = plan_project_initialization(collection, project, "base")
            second = plan_project_initialization(collection, project, "base")

            self.assertEqual(tree_contents(collection), before_collection)
            self.assertEqual(tree_contents(project), before_project)

        self.assertEqual(first, second)
        self.assertIsInstance(first.actions, tuple)
        self.assertIsInstance(first.blocking_issues, tuple)
        with self.assertRaises(FrozenInstanceError):
            first.status = "blocked"
        with self.assertRaises(FrozenInstanceError):
            first.binding_observation.kind = "unreadable"
        with self.assertRaises(FrozenInstanceError):
            first.actions[0].precondition = "absent"

    def test_planning_performs_no_mutating_git_network_environment_or_filesystem_call(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            forbidden = AssertionError("read-only initialization attempted mutation")
            with ExitStack() as stack:
                for name in (
                    "mkdir", "makedirs", "write", "pwrite", "truncate", "symlink",
                    "link", "unlink", "remove", "rmdir", "rename", "replace", "chmod",
                    "chown", "utime", "putenv", "unsetenv",
                ):
                    if hasattr(os, name):
                        stack.enter_context(patch(f"os.{name}", side_effect=forbidden))
                for name in ("write_text", "write_bytes", "mkdir", "symlink_to", "unlink", "rename", "replace", "chmod", "touch"):
                    stack.enter_context(patch(f"pathlib.Path.{name}", side_effect=forbidden))
                stack.enter_context(patch("subprocess.run", side_effect=forbidden))
                stack.enter_context(patch("subprocess.Popen", side_effect=forbidden))
                stack.enter_context(patch("socket.socket", side_effect=forbidden))
                stack.enter_context(patch("tempfile.NamedTemporaryFile", side_effect=forbidden))
                stack.enter_context(patch("tempfile.TemporaryFile", side_effect=forbidden))

                result = plan_project_initialization(collection, project, "base")

        self.assertEqual(result.status, "ready")

    def test_missing_project_root_is_unreadable_and_never_claimed_absent(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            missing = Path(directory) / "missing-project"

            result = plan_project_initialization(collection, missing, "base")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.binding_observation.kind, "unreadable")
        self.assertIn(
            ("root.missing", Location("project", ".")),
            [(issue.code, issue.location) for issue in result.blocking_issues],
        )
        self.assertNotIn(
            "initialization.binding_exists",
            [issue.code for issue in result.blocking_issues],
        )

    def test_uninspectable_binding_destination_has_its_own_stable_issue(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            with patch("skill_collection._filesystem.Path.lstat", side_effect=PermissionError()):
                result = plan_project_initialization(collection, project, "base")

        self.assertEqual(result.binding_observation.kind, "unreadable")
        self.assertIn(
            (
                "initialization.binding_uninspectable",
                Location("project", "skill-collection.toml"),
            ),
            [(issue.code, issue.location) for issue in result.blocking_issues],
        )
        self.assertNotIn(
            "initialization.binding_exists",
            [issue.code for issue in result.blocking_issues],
        )

    def test_discovery_issue_blocks_once_without_partial_collection_state(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha")

            result = plan_project_initialization(collection, Path(directory), "base")

        matching = [
            issue for issue in result.blocking_issues
            if issue.code == "catalog.skill_not_discovered"
        ]
        self.assertEqual(len(matching), 1)
        self.assertIsNone(result.profile)
        self.assertIsNone(result.collection_revision)
        self.assertIsNone(result.collection_url)
        self.assertEqual(result.actions, ())

    def test_file_project_root_blocks_and_symlinked_directory_root_is_supported(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            root = Path(directory)
            file_root = root / "project-file"
            file_root.write_text("owned\n", encoding="utf-8")
            real_root = root / "real-project"
            real_root.mkdir()
            linked_root = root / "linked-project"
            linked_root.symlink_to(real_root, target_is_directory=True)

            file_result = plan_project_initialization(collection, file_root, "base")
            linked_result = plan_project_initialization(collection, linked_root, "base")

        self.assertEqual(file_result.status, "blocked")
        self.assertEqual(file_result.binding_observation.kind, "unreadable")
        self.assertIn(
            ("root.missing", Location("project", ".")),
            [(issue.code, issue.location) for issue in file_result.blocking_issues],
        )
        self.assertEqual(linked_result.status, "ready")
        self.assertEqual(linked_result.binding_observation.kind, "absent")

    def test_empty_and_invalid_toml_binding_files_block_without_being_parsed(self) -> None:
        for content in ("", "this is not = valid TOML\n"):
            with self.subTest(content=content), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                binding = project / "skill-collection.toml"
                binding.write_text(content, encoding="utf-8")
                original_open = Path.open

                def guarded_open(path: Path, *args: object, **kwargs: object):
                    if path == binding:
                        raise AssertionError("existing Binding was opened")
                    return original_open(path, *args, **kwargs)

                with patch("pathlib.Path.open", new=guarded_open):
                    result = plan_project_initialization(collection, project, "base")

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.binding_observation.kind, "regular-file")
            self.assertEqual(
                [issue.code for issue in result.blocking_issues],
                ["initialization.binding_exists"],
            )

    def test_available_device_kinds_block_without_opening_the_destination(self) -> None:
        candidates: list[tuple[Path, str]] = []
        for device in (Path("/dev/null"), Path("/dev/disk0"), Path("/dev/rdisk0")):
            if not device.exists():
                continue
            mode = device.stat().st_mode
            if stat.S_ISCHR(mode):
                candidates.append((device, "character-device"))
            elif stat.S_ISBLK(mode):
                candidates.append((device, "block-device"))
        self.assertTrue(candidates)
        for device, expected_kind in candidates:
            with self.subTest(device=device), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                binding = project / "skill-collection.toml"
                original_lstat = Path.lstat
                original_open = Path.open

                def device_lstat(path: Path):
                    return device.stat() if path == binding else original_lstat(path)

                def guarded_open(path: Path, *args: object, **kwargs: object):
                    if path == binding:
                        raise AssertionError("device destination was opened")
                    return original_open(path, *args, **kwargs)

                with patch("pathlib.Path.lstat", new=device_lstat), patch("pathlib.Path.open", new=guarded_open):
                    result = plan_project_initialization(collection, project, "base")

            self.assertEqual(result.binding_observation.kind, expected_kind)
            self.assertIn("initialization.binding_exists", [issue.code for issue in result.blocking_issues])

    def test_final_observation_does_not_claim_to_detect_transient_create_remove(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            binding = project / "skill-collection.toml"
            original_lstat = Path.lstat
            observed = False

            def transient_lstat(path: Path):
                nonlocal observed
                if path == binding and not observed:
                    observed = True
                    binding.write_text("transient\n", encoding="utf-8")
                    binding.unlink()
                return original_lstat(path)

            with patch("pathlib.Path.lstat", new=transient_lstat):
                result = plan_project_initialization(collection, project, "base")

        self.assertTrue(observed)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.binding_observation.kind, "absent")

    def test_every_read_descriptor_is_closed_and_os_open_is_never_write_capable(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            opened: list[object] = []
            original_open = Path.open
            original_os_open = os.open

            def tracked_open(path: Path, *args: object, **kwargs: object):
                stream = original_open(path, *args, **kwargs)
                opened.append(stream)
                return stream

            def guarded_os_open(path: object, flags: int, *args: object, **kwargs: object):
                forbidden = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_TRUNC | os.O_APPEND
                self.assertEqual(flags & forbidden, 0)
                return original_os_open(path, flags, *args, **kwargs)

            with patch("pathlib.Path.open", new=tracked_open), patch("os.open", new=guarded_os_open):
                result = plan_project_initialization(collection, project, "base")

        self.assertEqual(result.status, "ready")
        self.assertTrue(opened)
        self.assertTrue(all(stream.closed for stream in opened))

    def test_read_descriptors_close_on_document_failure_and_interruption(self) -> None:
        for failure in (OSError("unreadable"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                catalog = collection / "catalog.toml"
                original_open = Path.open
                wrapped_streams: list[object] = []

                class FailingStream:
                    def __init__(self, stream: object) -> None:
                        self.stream = stream

                    def __enter__(self):
                        return self

                    def __exit__(self, *args: object) -> None:
                        self.stream.close()

                    def read(self, *args: object, **kwargs: object):
                        raise failure

                def failing_open(path: Path, *args: object, **kwargs: object):
                    stream = original_open(path, *args, **kwargs)
                    if path != catalog:
                        return stream
                    wrapped = FailingStream(stream)
                    wrapped_streams.append(wrapped)
                    return wrapped

                with patch("pathlib.Path.open", new=failing_open):
                    if isinstance(failure, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            plan_project_initialization(collection, project, "base")
                    else:
                        result = plan_project_initialization(collection, project, "base")
                        self.assertEqual(result.status, "blocked")

            self.assertTrue(wrapped_streams)
            self.assertTrue(all(wrapped.stream.closed for wrapped in wrapped_streams))

    def test_git_remote_credentials_environment_and_network_cannot_influence_identity(self) -> None:
        with valid_collection() as first_collection, valid_collection() as second_collection, tempfile.TemporaryDirectory() as directory:
            for collection in (first_collection, second_collection):
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
            initialize_git_metadata(
                first_collection,
                remote="https://machine-user:secret@example.invalid/first.git",
                credential_helper="!echo first-secret",
            )
            initialize_git_metadata(
                second_collection,
                remote="ssh://different@elsewhere.invalid/second.git",
                credential_helper="!echo second-secret",
            )
            first_project = Path(directory) / "first"
            second_project = Path(directory) / "second"
            first_project.mkdir()
            second_project.mkdir()
            forbidden = AssertionError("planning consulted Git or network state")

            with patch.dict(
                os.environ,
                {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url.ssh://rewrite.invalid/.insteadOf", "GIT_CONFIG_VALUE_0": "ssh://git@github.com/"},
            ), patch("subprocess.run", side_effect=forbidden), patch("subprocess.Popen", side_effect=forbidden), patch("socket.socket", side_effect=forbidden):
                first = plan_project_initialization(first_collection, first_project, "base")
                second = plan_project_initialization(second_collection, second_project, "base")

        self.assertEqual(first, second)
        self.assertEqual(first.collection_url, COLLECTION_URL)
        self.assertNotIn("secret", first.binding_content)


class InitializationCliPublicSeamTests(unittest.TestCase):
    def test_init_project_defaults_to_deterministic_json_and_is_read_only(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                [
                    "init-project",
                    "--collection-root", str(collection),
                    "--project-root", str(project),
                    "--profile", "base",
                ],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertFalse((project / "skill-collection.toml").exists())

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["command"], "init-project")
        self.assertEqual(payload["result"]["status"], "ready")
        self.assertTrue(stdout.getvalue().endswith("\n"))
        self.assertNotIn(str(collection), stdout.getvalue())
        self.assertNotIn(str(project), stdout.getvalue())

    def test_init_project_text_renders_the_same_plan_without_applying_it(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            expected = plan_project_initialization(collection, project, "base")
            stdout = io.StringIO()

            exit_code = main(
                [
                    "init-project",
                    "--collection-root", str(collection),
                    "--project-root", str(project),
                    "--profile", "base",
                    "--format", "text",
                ],
                stdout=stdout,
                stderr=io.StringIO(),
            )

            self.assertFalse((project / "skill-collection.toml").exists())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stdout.getvalue(),
            "Project initialization: ready\n"
            "Profile: base\n"
            f"Collection revision: {'0' * 40}\n"
            f"Collection URL: {COLLECTION_URL}\n"
            "Binding: project:skill-collection.toml\n"
            "Binding state: absent\n"
            f"Binding digest: {expected.binding_digest}\n"
            f"Plan ID: {expected.plan_id}\n\n"
            "Proposed actions (1):\n"
            "1. [create-binding] project:skill-collection.toml\n"
            "   Precondition: absent\n"
            f"   Content SHA-256: {expected.actions[0].content_sha256}\n"
            f"   Action ID: {expected.actions[0].action_id}\n\n"
            "Binding content:\n"
            "  version = 1\n"
            '  profile = "base"\n'
            '  target = ".agents/skills"\n'
            "  \n"
            "  [collection]\n"
            f'  url = "{COLLECTION_URL}"\n'
            f'  revision = "{"0" * 40}"\n\n'
            "Issues (0):\n"
            "None.\n",
        )

    def test_init_project_has_no_apply_mode_and_requires_profile(self) -> None:
        for arguments in (
            ["init-project", "--project-root", "/tmp/project"],
            [
                "init-project", "--project-root", "/tmp/project",
                "--profile", "base", "--apply",
            ],
        ):
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()

                exit_code = main(arguments, stdout=stdout, stderr=stderr)

                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("error:", stderr.getvalue())

    def test_blocked_init_project_uses_stdout_exit_one_and_omits_partial_binding(self) -> None:
        with valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
            add_collection_url(collection)
            add_discovered_skills(collection, "alpha", "beta")
            project = Path(directory)
            (project / "skill-collection.toml").write_text("private owned bytes\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                [
                    "init-project", "--collection-root", str(collection),
                    "--project-root", str(project), "--profile", "base",
                ],
                stdout=stdout,
                stderr=stderr,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["result"]["status"], "blocked")
        self.assertEqual(payload["result"]["binding_observation"]["kind"], "regular-file")
        self.assertIsNone(payload["result"]["binding_content"])
        self.assertIsNone(payload["result"]["binding_digest"])
        self.assertNotIn("private owned bytes", stdout.getvalue())

    def test_every_initialization_blocker_has_byte_exact_json(self) -> None:
        expected_documents = {
            "exists": """{
  "command": "init-project",
  "result": {
    "actions": [],
    "binding_content": null,
    "binding_digest": null,
    "binding_location": {
      "relative_path": "skill-collection.toml",
      "root": "project"
    },
    "binding_observation": {
      "kind": "regular-file",
      "location": {
        "relative_path": "skill-collection.toml",
        "root": "project"
      }
    },
    "blocking_issues": [
      {
        "code": "initialization.binding_exists",
        "location": {
          "relative_path": "skill-collection.toml",
          "root": "project"
        },
        "message": "Project Binding destination already exists.",
        "related_locations": []
      }
    ],
    "collection_revision": null,
    "collection_url": null,
    "plan_id": null,
    "profile": null,
    "status": "blocked"
  },
  "schema_version": 1
}
""",
            "uninspectable": """{
  "command": "init-project",
  "result": {
    "actions": [],
    "binding_content": null,
    "binding_digest": null,
    "binding_location": {
      "relative_path": "skill-collection.toml",
      "root": "project"
    },
    "binding_observation": {
      "kind": "unreadable",
      "location": {
        "relative_path": "skill-collection.toml",
        "root": "project"
      }
    },
    "blocking_issues": [
      {
        "code": "initialization.binding_uninspectable",
        "location": {
          "relative_path": "skill-collection.toml",
          "root": "project"
        },
        "message": "Project Binding destination could not be safely inspected.",
        "related_locations": []
      }
    ],
    "collection_revision": null,
    "collection_url": null,
    "plan_id": null,
    "profile": null,
    "status": "blocked"
  },
  "schema_version": 1
}
""",
            "outside": """{
  "command": "init-project",
  "result": {
    "actions": [],
    "binding_content": null,
    "binding_digest": null,
    "binding_location": {
      "relative_path": "skill-collection.toml",
      "root": "project"
    },
    "binding_observation": {
      "kind": "absent",
      "location": {
        "relative_path": "skill-collection.toml",
        "root": "project"
      }
    },
    "blocking_issues": [
      {
        "code": "initialization.binding_outside_project",
        "location": {
          "relative_path": "skill-collection.toml",
          "root": "project"
        },
        "message": "Project Binding destination must remain inside the project root.",
        "related_locations": []
      }
    ],
    "collection_revision": null,
    "collection_url": null,
    "plan_id": null,
    "profile": null,
    "status": "blocked"
  },
  "schema_version": 1
}
""",
        }
        for classification, expected in expected_documents.items():
            with self.subTest(classification=classification), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
                project = Path(directory)
                binding = project / "skill-collection.toml"
                stack = ExitStack()
                if classification == "exists":
                    binding.write_text("owned\n", encoding="utf-8")
                elif classification == "uninspectable":
                    original_lstat = Path.lstat

                    def blocked_lstat(path: Path):
                        if path == binding:
                            raise PermissionError()
                        return original_lstat(path)

                    stack.enter_context(patch("pathlib.Path.lstat", new=blocked_lstat))
                else:
                    original_resolve = Path.resolve
                    outside = project.parent / "outside" / "skill-collection.toml"

                    def escaped_resolve(path: Path, *args: object, **kwargs: object):
                        if path == binding:
                            return outside
                        return original_resolve(path, *args, **kwargs)

                    stack.enter_context(patch("pathlib.Path.resolve", new=escaped_resolve))
                stdout = io.StringIO()
                with stack:
                    exit_code = main(
                        [
                            "init-project", "--collection-root", str(collection),
                            "--project-root", str(project), "--profile", "base",
                        ],
                        stdout=stdout,
                        stderr=io.StringIO(),
                    )

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), expected)

    def test_unexpected_failure_and_interruption_preserve_roots_and_git_metadata(self) -> None:
        for failure, expected_code in ((RuntimeError("private detail"), 3), (KeyboardInterrupt(), 130)):
            with self.subTest(expected_code=expected_code), valid_collection() as collection, tempfile.TemporaryDirectory() as directory:
                add_collection_url(collection)
                add_discovered_skills(collection, "alpha", "beta")
                initialize_git_metadata(collection)
                project = Path(directory)
                before_collection = tree_contents(collection)
                before_project = tree_contents(project)
                stdout = io.StringIO()
                stderr = io.StringIO()
                binding = project / "skill-collection.toml"
                original_lstat = Path.lstat

                def failing_lstat(path: Path):
                    if path == binding:
                        raise failure
                    return original_lstat(path)

                with patch("pathlib.Path.lstat", new=failing_lstat):
                    exit_code = main(
                        [
                            "init-project", "--collection-root", str(collection),
                            "--project-root", str(project), "--profile", "base",
                        ],
                        stdout=stdout,
                        stderr=stderr,
                    )

                self.assertEqual(tree_contents(collection), before_collection)
                self.assertEqual(tree_contents(project), before_project)
                self.assertFalse(any(path.name.endswith((".lock", ".tmp")) for path in collection.rglob("*")))
                self.assertFalse(any(path.name.endswith((".lock", ".tmp")) for path in project.rglob("*")))

            self.assertEqual(exit_code, expected_code)
            self.assertEqual(stdout.getvalue(), "")
            if expected_code == 3:
                self.assertEqual(
                    json.loads(stderr.getvalue())["error"]["code"],
                    "system.unexpected",
                )
                self.assertNotIn("private detail", stderr.getvalue())
            else:
                self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
