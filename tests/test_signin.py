"""Forwarded sign-in through the installed connect command and controlled peers."""
import json
import os
from pathlib import Path
import socket
import time
import subprocess
import signal
import sys

from test_compute import ComputeFixture


class SignInCLI(ComputeFixture):
    def setUp(self):
        super().setUp()
        self.env["SIGNIN_LOG"] = str(self.root / "signin.jsonl")
        self.env["AUTH_CALLBACK"] = str(self.root / "callback")
        self.env["BROWSER"] = str(self.bin / "browser")
        self.auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "disposable-access",
                    "refresh_token": "disposable-refresh", "id_token": "disposable-id"}}
        ssh = (self.bin / "ssh").read_text()
        insertion = '''
from pathlib import Path
import signal, time
def event(name):
    with open(os.environ["SIGNIN_LOG"], "a") as log: log.write(json.dumps({"event": name}) + "\\n")
if "-O" in sys.argv:
    control = Path(sys.argv[sys.argv.index("-S") + 1])
    if control.exists():
        os.kill(int(control.read_text()), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while control.exists() and time.monotonic() < deadline: time.sleep(0.01)
    raise SystemExit(0)
if "-f" in sys.argv:
    if os.environ.get("TUNNEL_FAIL"):
        event("tunnel-failed")
        raise SystemExit(255)
    control = sys.argv[sys.argv.index("-S") + 1]
    port = sys.argv[sys.argv.index("-L") + 1].split(":")[1]
    child = subprocess.Popen([sys.executable, os.environ["TUNNEL_SCRIPT"], control, port],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             start_new_session=True, text=True)
    ready = child.stdout.readline()
    child.stdout.close()
    if ready.strip() != "ready":
        child.wait()
        raise SystemExit(255)
    event("tunnel-ready")
    if os.environ.get("DELAY_FORWARDING_RESPONSE"):
        event("forwarding-response-pending")
        time.sleep(30)
    raise SystemExit(0)
'''
        (self.bin / "ssh").write_text(ssh.replace("env = os.environ.copy()", insertion + "\nenv = os.environ.copy()"))
        self.script("tunnel", '''
import json, os, signal, socket, sys, time
from pathlib import Path
control, port = Path(sys.argv[1]), int(sys.argv[2])
listener = socket.socket()
def close(signum, frame):
    listener.close()
    control.unlink(missing_ok=True)
    with open(os.environ["SIGNIN_LOG"], "a") as log: log.write(json.dumps({"event": "tunnel-closed"}) + "\\n")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, close)
try: listener.bind(("127.0.0.1", port))
except OSError: raise SystemExit(1)
listener.listen()
control.write_text(str(os.getpid()))
print("ready", flush=True)
connection, _ = listener.accept()
connection.recv(100)
connection.close()
Path(os.environ["AUTH_CALLBACK"]).touch()
while True: time.sleep(1)
''')
        self.env["TUNNEL_SCRIPT"] = str(self.bin / "tunnel")
        self.script("browser", '''
import json, os, socket, sys
from urllib.parse import parse_qs, urlparse
with open(os.environ["SIGNIN_LOG"], "a") as log:
    log.write(json.dumps({"event": "browser", "url": sys.argv[-1]}) + "\\n")
if os.environ.get("BROWSER_FAIL"): raise SystemExit(1)
redirect = parse_qs(urlparse(sys.argv[-1]).query)["redirect_uri"][0]
with socket.create_connection(("127.0.0.1", urlparse(redirect).port)) as callback:
    callback.sendall(b"disposable callback")
''')
        apptainer = (self.bin / "apptainer").read_text()
        prefix = apptainer[:apptainer.index('if sys.argv[-2:] == ["login", "status"]:')]
        # Leave verification/capability handling real, control only Codex behavior.
        self.script("apptainer", prefix.split("\n", 1)[1] + '''
import time
from urllib.parse import urlencode
home = Path(sys.argv[sys.argv.index("--home") + 1].split(":")[0])
auth = home / ".codex/auth.json"
def event(name):
    with open(os.environ["SIGNIN_LOG"], "a") as log: log.write(json.dumps({"event": name, "pid": os.getpid()}) + "\\n")
if sys.argv[-2:] == ["login", "status"]:
    event("status")
    if os.environ.get("REAL_LOGIN_STATUS"):
        import subprocess
        status = subprocess.run([os.environ["REAL_APPTAINER"], *sys.argv[1:]], capture_output=True)
        raise SystemExit(status.returncode)
    raise SystemExit(0 if auth.exists() and not os.environ.get("INVALID_SAVED") else 1)
if "login" in sys.argv:
    event("login")
    if "--device-auth" in sys.argv:
        event("device-auth")
        print("Enter DISPOSABLE-CODE at https://auth.openai.com/codex/device", file=sys.stderr, flush=True)
        auth.write_text(''' + repr(json.dumps(self.auth)) + ''')
        raise SystemExit(0)
    port = int(os.environ.get("CALLBACK_PORT", "1455"))
    print("Starting local login server on http://localhost:" + str(port) + ".", file=sys.stderr, flush=True)
    print("https://auth.openai.com/oauth/authorize?" + urlencode({"redirect_uri":
          "http://127.0.0.1:" + str(port) + "/auth/callback"}), file=sys.stderr, flush=True)
    deadline = time.monotonic() + 15
    while not Path(os.environ["AUTH_CALLBACK"]).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not Path(os.environ["AUTH_CALLBACK"]).exists(): raise SystemExit(1)
    if os.environ.get("LOGIN_FAIL"): raise SystemExit(1)
    auth.write_text(''' + repr(json.dumps(self.auth)) + ''')
    event("authenticated")
    raise SystemExit(0)
event("interactive")
assert auth.exists()
if os.environ.get("EXPECTED_AUTH"):
    assert json.loads(auth.read_text()) == json.loads(os.environ["EXPECTED_AUTH"])
print("signed-in interactive session")
''')

    def signin_events(self):
        path = Path(self.env["SIGNIN_LOG"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_known_needed_busy_callback_port_stops_before_submission(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 1455))
            busy.listen()
            result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1455", result.stderr)
        self.assertIn("free", result.stderr)
        self.assertIn("rerun", result.stderr)
        self.assertEqual(self.scheduler_events(), [])

    def test_browser_opens_after_tunnel_and_tunnel_closes_before_session(self):
        unrelated_control = self.root / "unrelated-control"
        unrelated = subprocess.Popen([sys.executable, self.env["TUNNEL_SCRIPT"], str(unrelated_control), "0"],
                                     env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     text=True, start_new_session=True)
        self.assertEqual(unrelated.stdout.readline().strip(), "ready")
        try:
            result = self.connect()
            self.assertIsNone(unrelated.poll())
            self.assertTrue(unrelated_control.exists())
        finally:
            unrelated.terminate()
            unrelated.communicate(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [event["event"] for event in self.signin_events()]
        self.assertEqual(names, ["status", "login", "tunnel-ready", "browser",
                                 "authenticated", "tunnel-closed", "interactive", "tunnel-closed"])
        self.assertIn("signed-in interactive session", result.stdout)
        saved = self.home / ".local/state/codex-hpc/credentials/auth.json"
        self.assertEqual(json.loads(saved.read_text()), self.auth)
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})

    def test_browser_failure_keeps_url_and_tunnel_for_manual_completion(self):
        self.env["BROWSER_FAIL"] = "1"
        process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_event("browser", process)
            time.sleep(0.1)
            self.assertIsNone(process.poll())
            self.assertNotIn("tunnel-closed", [event["event"] for event in self.signin_events()])
            with socket.create_connection(("127.0.0.1", 1455)) as callback:
                callback.sendall(b"manual callback")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("Open this sign-in URL", stdout)
            self.assertIn("https://auth.openai.com/oauth/authorize?", stdout)
            self.assertIn("Keeping the callback tunnel open", stdout)
            self.assertIn("tunnel-closed", [event["event"] for event in self.signin_events()])
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=10)

    def wait_event(self, expected, process):
        deadline = time.monotonic() + 20
        while expected not in [event["event"] for event in self.signin_events()]:
            self.assertIsNone(process.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def save_auth(self):
        directory = self.home / ".local/state/codex-hpc/credentials"
        directory.mkdir(exist_ok=True)
        (directory / "auth.json").write_text(json.dumps(self.auth))

    def test_cached_login_needs_no_callback_port_or_tunnel(self):
        if self.real:
            # Exercise the pinned executable's actual status command without an
            # API request: Codex reports the configured disposable file credential.
            self.auth = {"OPENAI_API_KEY": "disposable-not-a-real-key"}
            self.env["REAL_LOGIN_STATUS"] = "1"
        self.save_auth()
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 1455))
            busy.listen()
            result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([event["event"] for event in self.signin_events()], ["status", "interactive"])

    def test_late_callback_bind_failure_releases_owned_job_without_browser(self):
        # Codex can select its documented fallback when the node's 1455 is busy.
        self.env["CALLBACK_PORT"] = "1457"
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 1457))
            busy.listen()
            result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1457", result.stderr)
        self.assertNotIn("browser", [event["event"] for event in self.signin_events()])
        self.assertEqual([event["tool"] for event in self.scheduler_events()], ["sbatch", "squeue", "scancel"])
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])

    def test_device_code_is_explicit_and_ignores_busy_browser_port(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 1455))
            busy.listen()
            result = self.connect("--device-auth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DISPOSABLE-CODE", result.stdout)
        self.assertEqual([event["event"] for event in self.signin_events()],
                         ["status", "login", "device-auth", "interactive"])

    def test_failed_forwarding_never_opens_browser_or_falls_back_to_device_code(self):
        self.env["TUNNEL_FAIL"] = "1"
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot establish callback tunnel", result.stderr)
        self.assertEqual([event["event"] for event in self.signin_events()],
                         ["status", "login", "tunnel-failed"])
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])

    def test_cancel_during_signin_closes_tunnel_processes_scratch_and_job(self):
        self.env["BROWSER_FAIL"] = "1"
        process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_event("browser", process)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertNotIn("interactive", [event["event"] for event in self.signin_events()])
            self.assertIn("tunnel-closed", [event["event"] for event in self.signin_events()])
            self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
            self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
            login = next(event for event in self.signin_events() if event["event"] == "login")
            with self.assertRaises(ProcessLookupError): os.kill(login["pid"], 0)
            with socket.socket() as free:
                free.bind(("127.0.0.1", 1455))
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=10)

    def test_failed_login_closes_tunnel_without_starting_session(self):
        self.env["LOGIN_FAIL"] = "1"
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sign-in failed", result.stderr)
        names = [event["event"] for event in self.signin_events()]
        self.assertIn("tunnel-closed", names)
        self.assertNotIn("interactive", names)
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])

    def test_newer_saved_login_during_authentication_is_used_by_interactive_step(self):
        self.save_auth()
        newer = dict(self.auth, last_refresh="newer-disposable-login")
        self.env.update(INVALID_SAVED="1", BROWSER_FAIL="1", EXPECTED_AUTH=json.dumps(newer))
        process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_event("browser", process)
            stored = self.home / ".local/state/codex-hpc/credentials/auth.json"
            stored.write_text(json.dumps(newer))
            with socket.create_connection(("127.0.0.1", 1455)) as callback:
                callback.sendall(b"manual callback")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stored.read_text()), newer)
            self.assertIn("signed-in interactive session", stdout)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=10)

    def test_cancel_during_forwarding_startup_closes_owned_connection(self):
        self.env["DELAY_FORWARDING_RESPONSE"] = "1"
        process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_event("forwarding-response-pending", process)
            process.send_signal(signal.SIGINT)
            try:
                stdout, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                self.fail("forwarding startup process outlived canceled connect")
            self.assertEqual(process.returncode, 130, stderr)
            names = [event["event"] for event in self.signin_events()]
            self.assertIn("tunnel-closed", names)
            self.assertNotIn("browser", names)
            self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
            self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            process.communicate(timeout=35)
