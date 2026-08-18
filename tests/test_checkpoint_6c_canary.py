from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class DisposableCanaryTests(unittest.TestCase):
    def test_public_cli_lifecycle_uses_only_disposable_roots(self) -> None:
        checkout = Path(__file__).parents[1].resolve()
        parent = Path(tempfile.mkdtemp(prefix="skill-collection-6c-")).resolve()
        parent_identity = _identity(parent)
        collection = parent / "collection"
        project = parent / "project"
        commands: list[list[str]] = []
        try:
            shutil.copytree(checkout / "tests" / "fixtures" / "valid", collection)
            _add_collection_url(collection)
            _add_discovered_skills(collection, "alpha", "beta")
            project.mkdir()
            self.assertEqual(list(project.iterdir()), [])
            self.assertFalse(os.path.lexists(project / "skill-collection.toml"))

            initialization = _run_cli(
                checkout, parent, collection, project, commands,
                "init-project", "--profile", "base",
            )
            self.assertEqual(initialization.returncode, 0)
            initialization_result = _result(initialization, "init-project")
            self.assertEqual(initialization_result["status"], "ready")
            initialization_id = initialization_result["plan_id"]
            self.assertIsInstance(initialization_id, str)
            expected_binding = initialization_result["binding_content"].encode()

            initialized = _run_cli(
                checkout, parent, collection, project, commands,
                "init-project", "--profile", "base", "--apply", "--plan-id", initialization_id,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr + initialized.stdout)
            self.assertEqual(_result(initialized, "init-project")["status"], "created")
            self.assertTrue((project / "skill-collection.toml").is_file())
            binding = project / "skill-collection.toml"
            self.assertEqual(binding.read_bytes(), expected_binding)

            activation = _run_cli(checkout, parent, collection, project, commands, "activate")
            self.assertEqual(activation.returncode, 0)
            activation_result = _result(activation, "activate")
            self.assertEqual(activation_result["status"], "ready")
            activation_id = activation_result["plan_id"]
            self.assertIsInstance(activation_id, str)

            activated = _run_cli(
                checkout, parent, collection, project, commands,
                "activate", "--apply", "--plan-id", activation_id,
            )
            self.assertEqual(activated.returncode, 0, activated.stderr + activated.stdout)
            self.assertEqual(_result(activated, "activate")["status"], "applied")

            status = _run_cli(checkout, parent, collection, project, commands, "status")
            self.assertEqual(status.returncode, 0)
            self.assertEqual(_result(status, "status")["category"], "active")

            doctor = _run_cli(checkout, parent, collection, project, commands, "doctor")
            self.assertEqual(doctor.returncode, 0)
            self.assertEqual(_result(doctor, "doctor")["category"], "ok")
            self.assertEqual(
                sorted(path.relative_to(project).as_posix() for path in project.rglob("*")),
                [
                    ".agent-skill-collection",
                    ".agent-skill-collection/activation.toml",
                    ".agents",
                    ".agents/skills",
                    ".agents/skills/alpha",
                    "skill-collection.toml",
                ],
            )
            self.assertTrue((project / ".agent-skill-collection").is_dir())
            self.assertTrue((project / ".agent-skill-collection/activation.toml").is_file())
            for name in ("alpha",):
                link = project / ".agents/skills" / name
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), (collection / "skills" / name).resolve())

            self.assertEqual(
                [command[3] for command in commands],
                ["init-project", "init-project", "activate", "activate", "status", "doctor"],
            )
            for command in commands:
                self.assertEqual(command[:3], [sys.executable, "-m", "skill_collection"])
                for option in ("--collection-root", "--project-root"):
                    root = Path(command[command.index(option) + 1]).resolve()
                    self.assertTrue(root.is_relative_to(parent))
            self.assertNotIn("bystro", {part.lower() for part in parent.parts})
        finally:
            self.assertTrue(
                _teardown_parent(parent, parent_identity),
                _teardown_failure(parent),
            )
        self.assertFalse(_lexists(parent))

    def test_attention_required_initialization_stops_before_activation(self) -> None:
        checkout = Path(__file__).parents[1].resolve()
        parent = Path(tempfile.mkdtemp(prefix="skill-collection-6c-attention-")).resolve()
        parent_identity = _identity(parent)
        collection = parent / "collection"
        project = parent / "project"
        commands: list[list[str]] = []
        try:
            shutil.copytree(checkout / "tests" / "fixtures" / "valid", collection)
            _add_collection_url(collection)
            _add_discovered_skills(collection, "alpha", "beta")
            project.mkdir()
            planned = _run_cli(checkout, parent, collection, project, commands, "init-project", "--profile", "base")
            self.assertEqual(planned.returncode, 0, planned.stderr + planned.stdout)
            plan = _result(planned, "init-project")
            applied = _run_cli(
                checkout, parent, collection, project, commands, "init-project", "--profile", "base",
                "--apply", "--plan-id", plan["plan_id"], force_cleanup_failure=True,
            )
            self.assertEqual(applied.returncode, 1, applied.stderr + applied.stdout)
            result = _result(applied, "init-project")
            self.assertEqual(result["status"], "created_with_incomplete_cleanup", applied.stderr + applied.stdout)
            self.assertEqual((project / "skill-collection.toml").read_bytes(), plan["binding_content"].encode())
            cleanup = result["cleanup"]
            self.assertEqual([item["code"] for item in cleanup["issues"]], ["initialization.cleanup_remove_failed"])
            temporary = cleanup["remaining_objects"]
            self.assertEqual(len(temporary), 1)
            self.assertEqual(temporary[0]["root"], "project")
            relative_temporary = Path(temporary[0]["relative_path"])
            self.assertFalse(relative_temporary.is_absolute())
            self.assertNotIn("..", relative_temporary.parts)
            self.assertTrue(relative_temporary.name.startswith(".skill-collection.toml.tmp-"))
            temporary_path = project / relative_temporary
            self.assertTrue(temporary_path.resolve().is_relative_to(project.resolve()))
            self.assertTrue(temporary_path.is_file())
            self.assertEqual([command[3] for command in commands], ["init-project", "init-project"])
        finally:
            self.assertTrue(
                _teardown_parent(parent, parent_identity),
                _teardown_failure(parent),
            )

    def test_teardown_refuses_missing_parent(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="skill-collection-6c-missing-"))
        identity = _identity(parent)
        shutil.rmtree(parent)
        self.assertFalse(_teardown_parent(parent, identity))
        self.assertIn("parent is missing", _teardown_failure(parent))

    def test_teardown_refuses_replaced_parent(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="skill-collection-6c-replaced-"))
        identity = _identity(parent)
        replacement = parent.with_name(parent.name + "-replacement")
        parent.rename(replacement)
        parent.mkdir()
        try:
            self.assertFalse(_teardown_parent(parent, identity))
            self.assertIn("replaced or changed identity", _teardown_failure(parent))
            self.assertTrue(parent.is_dir())
            self.assertTrue(replacement.is_dir())
        finally:
            shutil.rmtree(parent)
            shutil.rmtree(replacement)

    def test_child_guard_rejects_each_forbidden_operation_class(self) -> None:
        checkout = Path(__file__).parents[1].resolve()
        parent = Path(tempfile.mkdtemp(prefix="skill-collection-6c-guard-"))
        source_probe = checkout / "src" / ".checkpoint-6c-write-probe"
        escape = parent / "escape"
        escape.symlink_to(checkout / "src" / "skill_collection", target_is_directory=True)
        probes = {
            "checkout read": "open(argv[1], 'rb')",
            "checkout write": "open(argv[1], 'wb')",
            "enumeration": "os.listdir(argv[1])",
            "metadata": "os.stat(argv[1])",
            "readlink": "os.readlink(argv[1])",
            "access": "os.access(argv[1], os.R_OK)",
            "descriptor read": (
                "fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY); "
                "os.open(argv[1], os.O_RDONLY, dir_fd=fd)"
            ),
            "descriptor write": (
                "fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY); "
                "os.open(argv[1], os.O_WRONLY | os.O_CREAT, dir_fd=fd)"
            ),
            "symlink traversal read": "open(argv[1], 'rb')",
            "symlink traversal write": "open(argv[1], 'wb')",
            "subprocess": "subprocess.run(['/usr/bin/true'])",
            "network": "socket.socket()",
            "environment": "os.environ['CHECKPOINT_6C_PROBE'] = '1'",
        }
        targets = {
            "checkout read": checkout / "README.md",
            "checkout write": source_probe,
            "enumeration": Path.home(),
            "metadata": Path.home() / ".ssh",
            "readlink": Path.home() / ".ssh",
            "access": Path.home() / ".ssh",
            "descriptor read": (checkout / "README.md").relative_to(Path("/")),
            "descriptor write": source_probe.relative_to(Path("/")),
            "symlink traversal read": escape / ".." / ".." / "README.md",
            "symlink traversal write": escape / ".." / ".checkpoint-6c-write-probe",
        }
        if hasattr(os, "setxattr"):
            probes["extended attribute"] = "os.setxattr(argv[1], b'checkpoint-6c', b'1')"
            targets["extended attribute"] = source_probe
        try:
            for label, expression in probes.items():
                with self.subTest(label=label):
                    completed = _run_guard_probe(
                        checkout, parent, expression, targets.get(label),
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("forbidden child", completed.stderr)
            self.assertFalse(source_probe.exists())
        finally:
            shutil.rmtree(parent)

    def test_teardown_refuses_dangling_symlink_parent(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="skill-collection-6c-dangling-"))
        identity = _identity(parent)
        shutil.rmtree(parent)
        parent.symlink_to(parent.with_name(parent.name + "-gone"), target_is_directory=True)
        self.assertFalse(_teardown_parent(parent, identity))
        self.assertTrue(parent.is_symlink())
        parent.unlink()

    def test_teardown_refuses_uninspectable_parent(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="skill-collection-6c-uninspectable-"))
        identity = _identity(parent)
        try:
            with patch("os.lstat", side_effect=OSError("inspection failed")):
                self.assertFalse(_teardown_parent(parent, identity))
                self.assertIn("could not be inspected", _teardown_failure(parent))
            self.assertTrue(parent.is_dir())
        finally:
            shutil.rmtree(parent)


def _run_cli(
    checkout: Path,
    parent: Path,
    collection: Path,
    project: Path,
    commands: list[list[str]],
    command: str,
    *arguments: str,
    force_cleanup_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    invocation = [
        sys.executable, "-m", "skill_collection", command,
        "--collection-root", str(collection), "--project-root", str(project), *arguments,
    ]
    if command != "activate":
        invocation.extend(("--format", "json"))
    commands.append(invocation)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(checkout / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SIX_C_DISPOSABLE_PARENT": str(parent),
        "SIX_C_SOURCE_ROOT": str(checkout / "src"),
    }
    guard = checkout / "tests" / "child_guard"
    environment["PYTHONPATH"] = os.pathsep.join((str(guard), str(checkout / "src")))
    if force_cleanup_failure:
        environment["SIX_C_FORCE_CLEANUP_FAILURE"] = "1"
    try:
        return subprocess.run(
            invocation, cwd=parent, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        stdout = (error.stdout or "")[-2048:]
        stderr = (error.stderr or "")[-2048:]
        raise AssertionError(
            f"CLI command timed out after 15s: {command}; stdout={stdout!r}; stderr={stderr!r}"
        ) from error


def _result(completed: subprocess.CompletedProcess[str], command: str) -> dict[str, object]:
    payload = json.loads(completed.stdout)
    if payload["schema_version"] != 1:
        raise AssertionError("Unexpected CLI schema version")
    if payload["command"] != command:
        raise AssertionError("Unexpected CLI command")
    return payload["result"]


def _run_guard_probe(
    checkout: Path, parent: Path, expression: str, target: Path | None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(checkout / "tests" / "child_guard"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SIX_C_DISPOSABLE_PARENT": str(parent),
        "SIX_C_SOURCE_ROOT": str(checkout / "src"),
    }
    script = "import os, socket, subprocess, sys; argv = sys.argv; " + expression
    invocation = [sys.executable, "-c", script]
    if target is not None:
        invocation.append(str(target))
    return subprocess.run(
        invocation, cwd=parent, env=environment, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=15,
    )


def _add_collection_url(collection: Path) -> None:
    catalog = collection / "catalog.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            "version = 1\n", 'version = 1\ncollection_url = "https://example.invalid/collection.git"\n', 1,
        ),
        encoding="utf-8",
    )


def _add_discovered_skills(collection: Path, *names: str) -> None:
    for name in names:
        skill = collection / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


def _identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _teardown_parent(parent: Path, expected_identity: tuple[int, int]) -> bool:
    """Remove only a parent whose no-follow identity still matches creation."""
    try:
        metadata = os.lstat(parent)
    except OSError:
        return False
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    shutil.rmtree(parent)
    return True


def _teardown_failure(parent: Path) -> str:
    try:
        os.lstat(parent)
    except FileNotFoundError:
        reason = "the parent is missing"
    except OSError:
        reason = "the parent could not be inspected"
    else:
        reason = "the parent was replaced or changed identity"
    return f"Disposable canary teardown refused because {reason}: {parent}"
