"""Credential lifetime through installed connect and a controlled Codex process."""
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import time

from test_compute import ComputeFixture


class CredentialCLI(ComputeFixture):
    def setUp(self):
        super().setUp()
        self.state = self.home / ".local/state/codex-hpc"
        self.apptainer_source = (self.bin / "apptainer").read_text()
        self.auth = {"auth_mode": "chatgpt", "tokens": {
            "id_token": "disposable-id", "access_token": "disposable-access",
            "refresh_token": "disposable-refresh", "account_id": "test-account"}}

    def codex(self, source):
        wrapper = self.apptainer_source
        prefix = wrapper[:wrapper.index('if os.environ.get("PROBE_MOUNTS"):')]
        prefix += '''
home = Path(sys.argv[sys.argv.index("--home") + 1].split(":")[0])
workspace = Path(sys.argv[sys.argv.index("--pwd") + 1])
auth = home / ".codex/auth.json"
'''
        (self.bin / "apptainer").write_text(prefix + source)

    def test_first_login_is_reused_without_retaining_home_settings(self):
        self.codex('''
assert not auth.exists()
auth.parent.mkdir(parents=True, exist_ok=True)
auth.write_text(''' + repr(json.dumps(self.auth)) + ''')
(home / "history").write_text("temporary conversation")
(auth.parent / "personal-settings").write_text("temporary settings")
(workspace / "persistent-edit").write_text("project edit")
''')
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.codex('''
assert json.loads(auth.read_text()) == ''' + repr(self.auth) + '''
assert not (home / "history").exists()
assert not (auth.parent / "personal-settings").exists()
assert (workspace / "persistent-edit").read_text() == "project edit"
print("cached login reused")
''')
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cached login reused", result.stdout)
        self.assertEqual(list(self.workspace.iterdir()), [self.workspace / "persistent-edit"])
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])

    def test_shared_aliases_reuse_login_while_separate_roots_are_empty(self):
        self.codex('auth.write_text(' + repr(json.dumps(self.auth)) + ')\n')
        self.assertEqual(self.connect().returncode, 0)
        self.codex('assert json.loads(auth.read_text()) == ' + repr(self.auth) + '\n')
        # A cached login cannot depend on owning the browser callback port.
        with socket.socket() as busy:
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            busy.bind(("127.0.0.1", 1455))
            busy.listen()
            result = self.connect("--host", "another-alias")
            self.assertEqual(result.returncode, 0, result.stderr)
        other = self.home / "different-state"
        other.mkdir()
        self.codex('assert not auth.exists()\n')
        result = self.connect("--state-root", str(other))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlapping_sessions_preserve_newer_login_when_stale_session_closes_last(self):
        self.codex('auth.write_text(' + repr(json.dumps(self.auth)) + ')\n')
        self.assertEqual(self.connect().returncode, 0)
        refreshed = dict(self.auth, last_refresh="2026-11-20T12:00:00Z")
        ready, close = self.root / "session-ready", self.root / "close-session"
        self.codex('''
assert json.loads(auth.read_text()) == ''' + repr(self.auth) + '''
if os.environ.get("OLDER_SESSION"):
    import time
    Path(''' + repr(str(ready)) + ''').touch()
    while not Path(''' + repr(str(close)) + ''').exists(): time.sleep(0.02)
    # Even a changed stale document must not replace the newer saved login.
    auth.write_text(''' + repr(json.dumps(dict(self.auth, last_refresh="2026-11-19T12:00:00Z"))) + ''')
else:
    auth.write_text(''' + repr(json.dumps(refreshed)) + ''')
''')
        process = subprocess.Popen(self.connect_command(), env=dict(self.env, OLDER_SESSION="1"),
                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            result = self.connect()
            self.assertEqual(result.returncode, 0, result.stderr)
            close.touch()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            close.touch()
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.codex('assert json.loads(auth.read_text()) == ' + repr(refreshed) + '\n')
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_permissions_and_no_credentials_in_workspace_or_logs(self):
        self.codex('''
assert home.stat().st_mode & 0o777 == 0o700
assert auth.parent.stat().st_mode & 0o777 == 0o700
auth.write_text(''' + repr(json.dumps(self.auth)) + ''')
''')
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.state / "credentials"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for name in ("auth.json", "lock"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(path.name for path in directory.iterdir()), {"auth.json", "lock"})
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
        logs = result.stdout + result.stderr + Path(self.env["COMPUTE_LOG"]).read_text()
        for token in self.auth["tokens"].values():
            self.assertNotIn(token, logs)

    def test_interrupted_atomic_persistence_preserves_complete_saved_login(self):
        self.codex('auth.write_text(' + repr(json.dumps(self.auth)) + ')\n')
        self.assertEqual(self.connect().returncode, 0)
        refreshed = dict(self.auth, last_refresh="2026-11-20T12:00:00Z")
        self.codex('auth.write_text(' + repr(json.dumps(refreshed)) + ')\n')
        ready = self.root / "persistence-ready"
        interpreter = (self.bin / "python3").read_text()
        intercept = '''
if os.environ.get("PAUSE_PERSIST"):
    import time
    original_replace = os.replace
    def replace(source, target):
        if str(target).endswith("/credentials/auth.json"):
            open(os.environ["PAUSE_PERSIST"], "w").close()
            time.sleep(60)
        return original_replace(source, target)
    os.replace = replace
'''
        (self.bin / "python3").write_text(interpreter.replace("sys.argv.pop(0)", intercept + "\nsys.argv.pop(0)"))
        process = subprocess.Popen(self.connect_command(), env=dict(self.env, PAUSE_PERSIST=str(ready)),
                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        self.assertEqual(list((self.state / "credentials").glob(".auth-*")), [])
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
        self.codex('assert json.loads(auth.read_text()) == ' + repr(self.auth) + '\n')
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_or_symlinked_auth_never_replaces_saved_login_or_leaks_content(self):
        self.codex('auth.write_text(' + repr(json.dumps(self.auth)) + ')\n')
        self.assertEqual(self.connect().returncode, 0)
        for body in ('auth.write_text("not-json-disposable-secret")',
                     'auth.unlink(); auth.symlink_to(workspace / "unrelated")'):
            with self.subTest(body=body):
                (self.workspace / "unrelated").write_text('{"unrelated":"private-project-data"}')
                self.codex(body + "\n")
                result = self.connect()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("not-json-disposable-secret", result.stderr)
                self.assertNotIn("private-project-data", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.codex('assert json.loads(auth.read_text()) == ' + repr(self.auth) + '\n')
                result = self.connect()
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_managed_credential_directory_cannot_be_redirected_into_workspace(self):
        directory = self.state / "credentials"
        directory.symlink_to(self.workspace)
        self.codex('raise AssertionError("redirected credential directory launched Codex")\n')
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credential directory", result.stderr)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_nonregular_auth_cannot_block_session_cleanup(self):
        self.codex('os.mkfifo(auth)\n')
        process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.fail("non-regular auth document blocked session cleanup")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("regular file", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
            self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=10)

    def test_node_state_alias_and_workspace_settings_cannot_redirect_credentials(self):
        self.codex('''assert '--config=cli_auth_credentials_store="file"' in sys.argv
auth.write_text(''' + repr(json.dumps(self.auth)) + ''')
''')
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        wrapper = (self.bin / "ssh").read_text()
        redirect = '''
if "-J" in sys.argv:
    from pathlib import Path
    state = Path(''' + repr(str(self.state)) + ''')
    state.rename(state.with_name("saved-state"))
    state.symlink_to(''' + repr(str(self.workspace)) + ''')
'''
        (self.bin / "ssh").write_text(wrapper.replace("raise SystemExit(subprocess.call(",
            redirect + "\nraise SystemExit(subprocess.call("))
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("state resolves differently", result.stderr)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_real_container_uses_private_auth_copy_without_exposing_store(self):
        if not self.real:
            self.skipTest("actual auth mount boundary requires Linux signed candidate CI")
        self.codex('auth.write_text(' + repr(json.dumps(self.auth)) + ')\n')
        self.assertEqual(self.connect().returncode, 0)
        (self.bin / "apptainer").write_text(self.apptainer_source)
        stored = self.state / "credentials/auth.json"
        refreshed = dict(self.auth, last_refresh="2026-11-20T12:00:00Z")
        self.env["PROBE_MOUNTS"] = '''
import json, os
from pathlib import Path
home = Path.home()
assert os.environ["CODEX_HOME"] == str(home / ".codex")
auth = home / ".codex/auth.json"
assert json.loads(auth.read_text()) == ''' + repr(self.auth) + '''
assert auth.stat().st_mode & 0o777 == 0o600
assert not Path(''' + repr(str(stored)) + ''').exists()
assert not Path("/proc/1/root" + ''' + repr(str(stored)) + ''').exists()
auth.write_text(''' + repr(json.dumps(refreshed)) + ''')
(home / "history").write_text("temporary conversation")
Path("project-edit").write_text("survives")
print("private auth copy refreshed")
'''
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("private auth copy refreshed", result.stdout)
        del self.env["PROBE_MOUNTS"]
        self.codex('assert json.loads(auth.read_text()) == ' + repr(refreshed) + '\n')
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.workspace.iterdir()), [self.workspace / "project-edit"])
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
