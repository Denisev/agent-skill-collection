from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
import socket
import ssl
import signal
import tempfile
import threading
import sys
import time
import os

from skill_collection.source_update import (
    _CleanupEvidence,
    _RemoteCleanupIncomplete,
    _anonymous_https_url,
    _git_environment,
    _run_bounded_remote,
    _run_remote_git,
    _terminate_process_group,
    _terminate_process_group_impl,
)


class _MatrixStream:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.closed = False

    def fileno(self) -> int:
        return self.descriptor

    def close(self) -> None:
        self.closed = True


class _MatrixKey:
    def __init__(self, stream: _MatrixStream, label: str) -> None:
        self.fileobj = stream
        self.data = label


class _MatrixSelector:
    def __init__(self, process: "_MatrixProcess", *, reads: bool) -> None:
        self.closed = False
        self._keys = (
            [_MatrixKey(process.stdout, "stdout"), _MatrixKey(process.stderr, "stderr")]
            if reads else []
        )

    def register(self, *args: object) -> None:
        return None

    def get_map(self) -> dict[int, _MatrixKey]:
        return {key.fileobj.fileno(): key for key in self._keys}

    def select(self, timeout: float) -> list[tuple[_MatrixKey, int]]:
        return [(key, 1) for key in tuple(self._keys)]

    def unregister(self, stream: _MatrixStream) -> None:
        self._keys = [key for key in self._keys if key.fileobj is not stream]

    def close(self) -> None:
        self.closed = True


class _MatrixProcess:
    pid = 24680

    def __init__(self) -> None:
        self.stdout = _MatrixStream(11)
        self.stderr = _MatrixStream(12)
        self.poll_count = 0

    def wait(self, **kwargs: object) -> int:
        return 0

    def poll(self) -> int | None:
        self.poll_count += 1
        return None if self.poll_count == 1 else 0

    def kill(self) -> None:
        return None


class RemoteSafetyTests(unittest.TestCase):
    def test_success_requires_confirmed_empty_process_group(self) -> None:
        completed = subprocess.CompletedProcess((sys.executable, "-c", "pass"), 0, "", "")
        with patch("skill_collection.source_update._terminate_process_group", return_value=_CleanupEvidence("group-not-empty")):
            with self.assertRaises(_RemoteCleanupIncomplete):
                _run_bounded_remote(completed.args)

    def test_success_cleanup_failure_retains_the_first_bounded_evidence(self) -> None:
        first = _CleanupEvidence("group-not-empty")
        with patch("skill_collection.source_update._terminate_process_group", side_effect=(first, None)):
            with self.assertRaises(_RemoteCleanupIncomplete) as raised:
                _run_bounded_remote((sys.executable, "-c", "pass"))
        self.assertIs(raised.exception.remote_cleanup_evidence, first)  # type: ignore[attr-defined]

    def test_descriptor_close_failure_is_cleanup_incomplete(self) -> None:
        class Stream:
            def fileno(self): return 9
            def close(self): raise OSError("private close detail")
        class Process:
            pid = 9090
            stdout, stderr = Stream(), Stream()
            def wait(self, **kwargs): return 0
        class Selector:
            def register(self, *args): return None
            def get_map(self): return {}
            def close(self): return None
        with patch("skill_collection.source_update.subprocess.Popen", return_value=Process()), patch(
            "skill_collection.source_update.selectors.DefaultSelector", return_value=Selector()
        ), patch("skill_collection.source_update._terminate_process_group", return_value=None):
            with self.assertRaises(_RemoteCleanupIncomplete):
                _run_bounded_remote(("git", "ls-remote"))

    def test_spawn_capture_read_wait_and_close_failure_matrix(self) -> None:
        stages = (
            "spawn", "capture", "stdout_read", "stderr_read", "wait",
            "selector_close", "stdout_close", "stderr_close",
        )
        failures = (KeyboardInterrupt(), PermissionError("private"), RuntimeError("private"))
        for stage in stages:
            for failure in failures:
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    process = _MatrixProcess()
                    selector = _MatrixSelector(process, reads=stage in ("stdout_read", "stderr_read"))
                    visited: list[str] = []

                    def lifecycle(active: str, operation: object, *args: object, **kwargs: object) -> object:
                        visited.append(active)
                        if active == "spawn":
                            if stage == active:
                                raise failure
                            return process
                        if active == "capture":
                            if stage == active:
                                raise failure
                            return selector
                        if active == stage:
                            raise failure
                        if active in ("stdout_read", "stderr_read"):
                            return b""
                        return operation(*args, **kwargs)  # type: ignore[operator]

                    expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else (
                        _RemoteCleanupIncomplete if stage.endswith("close") else type(failure)
                    )
                    with patch("skill_collection.source_update._lifecycle", side_effect=lifecycle), patch(
                        "skill_collection.source_update._terminate_process_group", return_value=None
                    ):
                        with self.assertRaises(expected) as raised:
                            _run_bounded_remote(("git", "ls-remote"))
                    self.assertIs(raised.exception, failure) if expected is type(failure) else None
                    if stage != "spawn":
                        self.assertIn("stdout_close", visited)
                        self.assertIn("stderr_close", visited)
                    if stage not in ("spawn", "capture"):
                        self.assertIn("selector_close", visited)

    def test_real_sigint_after_spawn_is_deferred_until_the_group_handle_is_retained(self) -> None:
        spawned: list[subprocess.Popen[bytes]] = []

        def lifecycle(stage: str, operation: object, *args: object, **kwargs: object) -> object:
            if stage == "spawn":
                process = operation(*args, **kwargs)  # type: ignore[operator]
                spawned.append(process)
                os.kill(os.getpid(), signal.SIGINT)
                return process
            return operation(*args, **kwargs)  # type: ignore[operator]

        with patch("skill_collection.source_update._lifecycle", side_effect=lifecycle):
            with self.assertRaises(KeyboardInterrupt):
                _run_bounded_remote((sys.executable, "-c", "import time; time.sleep(30)"))
        self.assertEqual(len(spawned), 1)
        try:
            os.killpg(spawned[0].pid, 0)
        except ProcessLookupError:
            survived = False
        else:
            survived = True
            os.killpg(spawned[0].pid, signal.SIGKILL)
            spawned[0].wait(timeout=2)
        self.assertFalse(survived)
        self.assertTrue(spawned[0].stdout is None or spawned[0].stdout.closed)
        self.assertTrue(spawned[0].stderr is None or spawned[0].stderr.closed)

    def test_cleanup_failure_matrix_preserves_pending_primary(self) -> None:
        stages = ("term", "term_wait", "kill", "kill_wait", "group_verification")
        for stage in stages:
            for cleanup_failure in (KeyboardInterrupt(), PermissionError("private"), RuntimeError("private")):
                with self.subTest(stage=stage, failure=type(cleanup_failure).__name__):
                    process = _MatrixProcess()
                    selector = _MatrixSelector(process, reads=False)
                    primary = subprocess.TimeoutExpired(("git",), 1)

                    def lifecycle(active: str, operation: object, *args: object, **kwargs: object) -> object:
                        if active == "spawn": return process
                        if active == "capture": return selector
                        if active == "wait": raise primary
                        if active == stage: raise cleanup_failure
                        return operation(*args, **kwargs)  # type: ignore[operator]

                    def killpg(group: int, sig: int) -> None:
                        if sig == 0 and getattr(killpg, "killed", False):
                            raise ProcessLookupError()
                        if sig == signal.SIGKILL:
                            killpg.killed = True  # type: ignore[attr-defined]

                    with patch("skill_collection.source_update._lifecycle", side_effect=lifecycle), patch(
                        "skill_collection.source_update.os.killpg", side_effect=killpg
                    ), patch(
                        "skill_collection.source_update._PROCESS_GROUP_WAIT_SECONDS",
                        0.001 if stage == "term_wait" else 0,
                    ):
                        with self.assertRaises(subprocess.TimeoutExpired) as raised:
                            _run_bounded_remote(("git", "ls-remote"))
                    self.assertIs(raised.exception, primary)
                    self.assertIsInstance(primary.remote_cleanup_evidence, _CleanupEvidence)  # type: ignore[attr-defined]
                    self.assertTrue(selector.closed)
                    self.assertTrue(process.stdout.closed)
                    self.assertTrue(process.stderr.closed)

    def test_only_anonymous_https_urls_are_eligible(self) -> None:
        self.assertTrue(_anonymous_https_url("https://example.invalid/repo.git"))
        for value in (
            "http://example.invalid/repo.git",
            "ssh://example.invalid/repo.git",
            "https://user:secret@example.invalid/repo.git",
            "https://example.invalid/repo.git?token=secret",
            "https://example.invalid/repo git",
            "https://example.invalid/repo.git\nheader:value",
            "https://-bad.example/repo.git",
            "https://bad-.example/repo.git",
            "https://bad_name.example/repo.git",
            "https://example..invalid/repo.git",
            "https://999.999.999.999/repo.git",
            "https://[not-ipv6]/repo.git",
            "https://éxample.invalid/repo.git",
        ):
            with self.subTest(value=value):
                self.assertFalse(_anonymous_https_url(value))

    def test_remote_command_is_exact_and_bounded(self) -> None:
        completed = subprocess.CompletedProcess((), 0, "a" * 40 + "\trefs/heads/main\n", "")
        with patch("skill_collection.source_update._run_bounded_remote", return_value=completed) as runner:
            result = _run_remote_git("https://example.invalid/repo.git", "refs/heads/main")
        self.assertEqual(result.stdout, completed.stdout)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[0][-3:], ("--refs", "https://example.invalid/repo.git", "refs/heads/main"))

    def test_environment_removes_proxy_credentials_and_alternate_tls_inputs(self) -> None:
        environment = _git_environment(remote=True)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_ASKPASS"], "/dev/null")
        self.assertEqual(environment["HOME"], os.devnull)
        self.assertEqual(environment["NETRC"], os.devnull)
        self.assertEqual(environment["CURL_HOME"], os.devnull)
        self.assertEqual(environment["XDG_CONFIG_HOME"], os.devnull)
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["GIT_DIR"], os.devnull)
        self.assertEqual(environment["GIT_WORK_TREE"], os.devnull)

    def test_remote_spawn_is_isolated_from_the_callers_repository(self) -> None:
        process = _MatrixProcess()
        selector = _MatrixSelector(process, reads=False)
        with patch.dict(os.environ, {
            "PATH": "/untrusted/user-controlled/bin",
            "HOME": "/private/account/home",
            "NETRC": "/private/account/home/.netrc",
            "CURL_HOME": "/private/account/curl",
            "XDG_CONFIG_HOME": "/private/account/config",
        }), patch(
            "skill_collection.source_update.subprocess.Popen", return_value=process
        ) as popen, patch(
            "skill_collection.source_update.selectors.DefaultSelector", return_value=selector
        ), patch("skill_collection.source_update._terminate_process_group", return_value=None):
            _run_bounded_remote(("/trusted/git", "ls-remote"))
        self.assertEqual(popen.call_args.kwargs["cwd"], "/")
        self.assertEqual(popen.call_args.kwargs["env"]["GIT_DIR"], os.devnull)
        self.assertEqual(popen.call_args.kwargs["env"]["PATH"], os.defpath)
        for name in ("HOME", "NETRC", "CURL_HOME", "XDG_CONFIG_HOME"):
            self.assertEqual(popen.call_args.kwargs["env"][name], os.devnull)

    def test_remote_transport_helper_and_credential_isolation_matrix(self) -> None:
        completed = subprocess.CompletedProcess((), 0, "", "")
        with patch("skill_collection.source_update._run_bounded_remote", return_value=completed) as runner:
            _run_remote_git("https://example.invalid/repo.git", "refs/heads/main")
        command = runner.call_args.args[0]
        self.assertIn("credential.helper=", command)
        self.assertIn("core.askPass=", command)
        self.assertIn("http.proxy=", command)
        self.assertIn("http.followRedirects=false", command)
        self.assertIn("http.emptyAuth=false", command)
        self.assertEqual(command[-3:], ("--refs", "https://example.invalid/repo.git", "refs/heads/main"))
        environment = _git_environment(remote=True)
        forbidden = ("SSH_AUTH_SOCK", "GIT_ASKPASS", "GIT_SSL_NO_VERIFY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_ASKPASS"], os.devnull)
        self.assertFalse(any(name in environment for name in forbidden if name != "GIT_ASKPASS"))

    def test_cleanup_escalates_term_kill_and_requires_empty_group(self) -> None:
        class Process:
            pid = 31415
            returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, **kwargs):
                return 0

        signals = []
        with patch("skill_collection.source_update._PROCESS_GROUP_WAIT_SECONDS", 0), patch("skill_collection.source_update.os.killpg", side_effect=lambda group, signal: signals.append((group, signal)) if signal else (_ for _ in ()).throw(ProcessLookupError()),):
            _terminate_process_group(Process())  # type: ignore[arg-type]
        self.assertEqual(signals[:2], [(31415, 15), (31415, 9)])

    def test_term_and_kill_waits_receive_independent_fixed_deadlines(self) -> None:
        class Process:
            pid = 31416

            def poll(self):
                return 0

            def wait(self, **kwargs):
                return 0

        waits: list[float] = []

        def lifecycle(stage, operation, *args, **kwargs):
            if stage == "kill_wait":
                waits.append(kwargs["timeout"])
                return 0
            return operation(*args, **kwargs)

        def killpg(group, sig):
            if sig == 0:
                raise ProcessLookupError()

        with patch("skill_collection.source_update.time.monotonic", side_effect=(10.0, 12.0, 20.0, 20.25)), patch(
            "skill_collection.source_update._lifecycle", side_effect=lifecycle
        ), patch("skill_collection.source_update.os.killpg", side_effect=killpg):
            self.assertIsNone(_terminate_process_group(Process()))  # type: ignore[arg-type]
        self.assertEqual(waits, [1.75])

    def test_term_grace_tracks_the_process_group_after_the_leader_exits(self) -> None:
        class Process:
            pid = 31418

            def poll(self):
                return 0

            def wait(self, **kwargs):
                return 0

        signals: list[int] = []
        term_waits = 0
        verification_count = 0

        def killpg(group, sig):
            nonlocal verification_count
            if sig == 0:
                verification_count += 1
                if verification_count == 2:
                    raise ProcessLookupError()
            else:
                signals.append(sig)

        def lifecycle(stage, operation, *args, **kwargs):
            nonlocal term_waits
            if stage == "term_wait":
                term_waits += 1
            return operation(*args, **kwargs)

        with patch("skill_collection.source_update.time.monotonic", side_effect=(10.0, 10.5, 12.0, 20.0, 20.25, 20.5)), patch(
            "skill_collection.source_update._lifecycle", side_effect=lifecycle
        ), patch("skill_collection.source_update.os.killpg", side_effect=killpg):
            self.assertIsNone(_terminate_process_group(Process()))  # type: ignore[arg-type]
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(term_waits, 1)

    def test_post_kill_wait_drains_the_group_before_confirming_esrch(self) -> None:
        class Process:
            pid = 31417

            def poll(self):
                return 0

            def wait(self, **kwargs):
                return 0

        verification_count = 0
        kill_wait_sleeps = 0

        def killpg(group, sig):
            nonlocal verification_count
            if sig == 0:
                verification_count += 1
                if verification_count == 2:
                    raise ProcessLookupError()

        def lifecycle(stage, operation, *args, **kwargs):
            nonlocal kill_wait_sleeps
            if stage == "kill_wait" and operation is time.sleep:
                kill_wait_sleeps += 1
            return operation(*args, **kwargs)

        with patch("skill_collection.source_update.time.monotonic", side_effect=(10.0, 12.0, 20.0, 20.25, 20.5)), patch(
            "skill_collection.source_update._lifecycle", side_effect=lifecycle
        ), patch("skill_collection.source_update.os.killpg", side_effect=killpg):
            self.assertIsNone(_terminate_process_group(Process()))  # type: ignore[arg-type]
        self.assertEqual(verification_count, 2)
        self.assertEqual(kill_wait_sleeps, 1)

    def test_cleanup_returns_bounded_evidence_for_term_kill_wait_and_verify_failures(self) -> None:
        class Process:
            pid = 27182

            def poll(self):
                return 0

            def wait(self, **kwargs):
                return 0

            def kill(self):
                return None

        for failure, expected in (("term", "term"), ("kill", "kill"), ("verify", "verify")):
            with self.subTest(failure=failure):
                def killpg(group, sig):
                    if sig == 0:
                        if failure == "verify":
                            raise PermissionError()
                        raise ProcessLookupError()
                    if failure == "term" and sig == 15:
                        raise PermissionError()
                    if failure == "kill" and sig == 9:
                        raise PermissionError()

                with patch("skill_collection.source_update._PROCESS_GROUP_WAIT_SECONDS", 0), patch("skill_collection.source_update.os.killpg", side_effect=killpg):
                    evidence = _terminate_process_group(Process())  # type: ignore[arg-type]
                self.assertEqual(evidence.phase, expected)  # type: ignore[union-attr]

    def test_cleanup_never_leaks_wait_or_descriptor_failures(self) -> None:
        class BrokenProcess:
            pid = 16180

            def poll(self):
                raise RuntimeError("private wait failure")

            def wait(self, **kwargs):
                raise RuntimeError("private wait failure")

            def kill(self):
                raise RuntimeError("private kill failure")

        with patch("skill_collection.source_update.os.killpg", side_effect=ProcessLookupError()):
            evidence = _terminate_process_group(BrokenProcess())  # type: ignore[arg-type]
        self.assertIsNone(evidence)
        with patch("skill_collection.source_update._PROCESS_GROUP_WAIT_SECONDS", 0), patch("skill_collection.source_update.os.killpg", return_value=None):
            evidence = _terminate_process_group(BrokenProcess())  # type: ignore[arg-type]
        self.assertEqual(evidence.phase, "internal")  # type: ignore[union-attr]

    def test_lifecycle_cleanup_matrix_preserves_primary_and_closes_resources(self) -> None:
        """Every cleanup seam is evidence-only when a remote failure is pending."""
        class Stream:
            def __init__(self) -> None:
                self.closed = False

            def fileno(self) -> int:
                return 9

            def close(self) -> None:
                self.closed = True

        class Process:
            pid = 14444

            def __init__(self) -> None:
                self.stdout, self.stderr = Stream(), Stream()

            def wait(self, **kwargs):
                raise subprocess.TimeoutExpired(("git",), 1)

            def poll(self):
                return None

            def kill(self):
                return None

        # The stages not reached before the primary wait failure are covered by
        # the direct cleanup matrix above; this matrix proves containment for
        # each outer-finally descriptor edge.
        for failing_stage in (None, "selector_close", "stdout_close", "stderr_close"):
            with self.subTest(failing_stage=failing_stage):
                process = Process()

                class Selector:
                    def register(self, *args):
                        return None

                    def get_map(self):
                        return {}

                    def close(self):
                        return None

                stages = []
                def lifecycle(stage, operation, *args, **kwargs):
                    stages.append(stage)
                    if stage == "spawn":
                        return process
                    if stage == failing_stage:
                        raise RuntimeError("private cleanup detail")
                    return operation(*args, **kwargs)

                with patch("skill_collection.source_update.selectors.DefaultSelector", return_value=Selector()), patch(
                    "skill_collection.source_update._lifecycle", side_effect=lifecycle
                ), patch("skill_collection.source_update._terminate_process_group", return_value=_CleanupEvidence("verify")):
                    with self.assertRaises(subprocess.TimeoutExpired) as raised:
                        _run_bounded_remote(("git", "ls-remote"))
                self.assertEqual(raised.exception.remote_cleanup_evidence.phase, "verify")
                self.assertIn("stdout_close", stages)
                self.assertIn("stderr_close", stages)
                self.assertEqual(process.stdout.closed, failing_stage != "stdout_close")
                self.assertEqual(process.stderr.closed, failing_stage != "stderr_close")

    def test_cleanup_stage_failure_matrix_is_sanitized_and_bounded(self) -> None:
        class Process:
            pid = 8118

            def poll(self):
                return None

            def wait(self, **kwargs):
                return 0

            def kill(self):
                return None

        for stage in ("term", "term_wait", "kill", "kill_wait", "group_verification"):
            for failure in (KeyboardInterrupt(), PermissionError("private"), RuntimeError("private")):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    def lifecycle(active, operation, *args, **kwargs):
                        if active == stage:
                            raise failure
                        return operation(*args, **kwargs)

                    def killpg(group, signal):
                        if signal == 0 and getattr(killpg, "killed", False):
                            raise ProcessLookupError()
                        if signal == 9:
                            killpg.killed = True  # type: ignore[attr-defined]

                    with patch("skill_collection.source_update._lifecycle", side_effect=lifecycle), patch(
                        "skill_collection.source_update.os.killpg", side_effect=killpg
                    ), patch(
                        "skill_collection.source_update._PROCESS_GROUP_WAIT_SECONDS",
                        0.001 if stage == "term_wait" else 0,
                    ):
                        evidence = _terminate_process_group(Process())  # type: ignore[arg-type]
                    expected = {
                        "term": "term", "kill": "kill", "group_verification": "verify",
                    }.get(stage, "internal") if isinstance(failure, OSError) else "internal"
                    self.assertEqual(evidence.phase if evidence else None, expected)

    def test_untrusted_local_tls_endpoint_is_rejected_without_trust_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            certificate = root / "certificate.pem"
            key = root / "key.pem"
            generated = subprocess.run(
                ("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(certificate), "-days", "1",
                 "-subj", "/CN=127.0.0.1"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            self.assertEqual(generated.returncode, 0)
            listener = socket.socket()
            try:
                listener.bind(("127.0.0.1", 0))
            except PermissionError:
                listener.close()
                self.skipTest("the filesystem sandbox does not permit local listeners")
            listener.listen(1)
            listener.settimeout(2)
            port = listener.getsockname()[1]
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            handshake_succeeded = threading.Event()
            handshake_rejected = threading.Event()

            def serve_once() -> None:
                try:
                    connection, _ = listener.accept()
                    try:
                        with context.wrap_socket(connection, server_side=True):
                            handshake_succeeded.set()
                            pass
                    except ssl.SSLError:
                        handshake_rejected.set()
                    except OSError:
                        pass
                except (OSError, TimeoutError):
                    pass
                finally:
                    listener.close()

            thread = threading.Thread(target=serve_once)
            thread.start()
            try:
                result = _run_remote_git(f"https://127.0.0.1:{port}/repo.git", "refs/heads/main")
            finally:
                listener.close()
                thread.join(timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(handshake_rejected.is_set())
        self.assertFalse(handshake_succeeded.is_set())
        self.assertFalse(thread.is_alive())
        self.assertEqual(listener.fileno(), -1)
        self.assertNotIn("GIT_SSL_NO_VERIFY", _git_environment(remote=True))

    def test_real_timeout_kills_term_ignoring_descendant_and_leaves_no_group(self) -> None:
        """Production runner evidence for timeout, escalation, closed pipes, and survival."""
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "descendant.pid"
            program = (
                "import os,signal,time,sys; "
                "pid=os.fork(); "
                "(open(sys.argv[1],'w').write(str(os.getpid())) if pid == 0 else None); "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            )
            # Both parent and child ignore TERM; KILL must clear the planner group.
            with patch("skill_collection.source_update.REMOTE_TIMEOUT_SECONDS", 0.05):
                with self.assertRaises(subprocess.TimeoutExpired):
                    _run_bounded_remote((sys.executable, "-c", program, str(pid_file)))
            deadline = time.monotonic() + 1
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            descendant = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(descendant, 0)

    def test_real_process_group_survival_outcome_matrix(self) -> None:
        original_popen = subprocess.Popen

        class EmptySelector:
            def register(self, *args: object) -> None: return None
            def get_map(self) -> dict[object, object]: return {}
            def close(self) -> None: return None

        cases = (
            ("success", None, 0),
            ("ordinary-block", None, 7),
            ("interruption", KeyboardInterrupt(), None),
            ("unexpected", RuntimeError("private"), None),
            ("cleanup-failure", subprocess.TimeoutExpired(("helper",), 1), None),
        )
        for name, failure, returncode in cases:
            with self.subTest(outcome=name):
                spawned: list[subprocess.Popen[bytes]] = []
                sleeping = failure is not None
                command = (
                    sys.executable, "-c",
                    "import time; time.sleep(30)" if sleeping else f"import sys; sys.exit({returncode})",
                )

                def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                    process = original_popen(*args, **kwargs)  # type: ignore[call-overload]
                    spawned.append(process)
                    return process

                def lifecycle(stage: str, operation: object, *args: object, **kwargs: object) -> object:
                    if sleeping and stage == "capture": return EmptySelector()
                    if sleeping and stage == "wait": raise failure  # type: ignore[misc]
                    return operation(*args, **kwargs)  # type: ignore[operator]

                def cleanup_then_report(process: subprocess.Popen[bytes], **kwargs: object) -> _CleanupEvidence:
                    self.assertIsNone(_terminate_process_group_impl(process))
                    return _CleanupEvidence("verify")

                cleanup_patch = (
                    patch("skill_collection.source_update._terminate_process_group", side_effect=cleanup_then_report)
                    if name == "cleanup-failure"
                    else patch("skill_collection.source_update._terminate_process_group", wraps=_terminate_process_group)
                )
                with patch("skill_collection.source_update.subprocess.Popen", side_effect=spawn), patch(
                    "skill_collection.source_update._lifecycle", side_effect=lifecycle
                ), cleanup_patch:
                    if failure is None:
                        result = _run_bounded_remote(command)
                        self.assertEqual(result.returncode, returncode)
                    else:
                        with self.assertRaises(type(failure)):
                            _run_bounded_remote(command)
                self.assertEqual(len(spawned), 1)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(spawned[0].pid, 0)
