"""Installed connect through controlled compute SSH and Slurm step boundaries."""
import json
import os
from pathlib import Path
import subprocess
import csv
import signal
import time
import pty
import select

import test_allocation
from test_setup import RemoteCLI


class ComputeCLI(test_allocation.AllocationFixture):
    def setUp(self):
        super().setUp()
        self.env["COMPUTE_LOG"] = str(self.root / "compute.jsonl")
        self.env["NODE_TMP"] = str(self.root / "node-local")
        Path(self.env["NODE_TMP"]).mkdir()
        self.script("ssh", '''
import json, os, subprocess, sys
env = os.environ.copy()
env["HOME"] = env["REMOTE_HOME"]
if "-J" in sys.argv:
    with open(env["COMPUTE_LOG"], "a") as log:
        log.write(json.dumps({"ssh": sys.argv[1:], "pid": os.getpid()}) + "\\n")
    env["NODE_STAGE"] = "1"
    if env.get("NODE_SSH_FAIL"): raise SystemExit(255)
    if env.get("NODE_CHANGE_IMAGE"):
        image = next(__import__("pathlib").Path(env["REMOTE_HOME"]).glob(".cache/codex-hpc/images/*/*.sif"))
        image.chmod(0o644)
        with image.open("r+b") as stream: stream.write(b"different image on node")
raise SystemExit(subprocess.call(["/bin/sh", "-c", sys.argv[-1]], env=env))
''')
        self.script("python3", '''
import os, platform, runpy, sys, tempfile
if os.environ.get("NODE_STAGE"):
    platform.system = lambda: os.environ.get("NODE_SYSTEM", "Linux")
    platform.machine = lambda: os.environ.get("NODE_ARCH", "x86_64")
    original = tempfile.TemporaryDirectory
    def temporary(*args, **kwargs):
        if kwargs.get("dir") == "/tmp": kwargs["dir"] = os.environ["NODE_TMP"]
        return original(*args, **kwargs)
    tempfile.TemporaryDirectory = temporary
    original_access = os.access
    os.access = lambda path, mode: False if str(path) == os.environ.get("NODE_INACCESSIBLE") else original_access(path, mode)
sys.argv.pop(0)
if sys.argv[0] == "-":
    exec(compile(sys.stdin.read(), "<remote>", "exec"))
else:
    runpy.run_path(sys.argv[0], run_name="__main__")
''')
        self.script("srun", '''
import json, os, sys
with open(os.environ["COMPUTE_LOG"], "a") as log:
    log.write(json.dumps({"srun": sys.argv[1:], "pid": os.getpid()}) + "\\n")
if os.environ.get("STEP_FAIL"): raise SystemExit(6)
position = sys.argv.index("python3")
os.environ["SLURM_JOB_ID"] = "321"
os.environ["SLURM_STEP_ID"] = "0"
os.execvp("python3", sys.argv[position:])
''')
        self.script("apptainer", '''
import json, os, sys
from pathlib import Path
if sys.argv[1] == "verify":
    with open(os.environ["EVENTS"], "a") as log: log.write("verify\\n")
    if os.environ.get("REAL_APPTAINER"):
        os.execv(os.environ["REAL_APPTAINER"], [os.environ["REAL_APPTAINER"], *sys.argv[1:]])
    raise SystemExit(0)
if "--help" in sys.argv:
    print("" if os.environ.get("MISSING_CAPABILITY") else "--cleanenv --containall --no-mount --home --mount --pwd --env --workdir")
    raise SystemExit(0)
with open(os.environ["COMPUTE_LOG"], "a") as log:
    log.write(json.dumps({"apptainer": sys.argv[1:], "job": os.environ.get("SLURM_JOB_ID"),
                         "step": os.environ.get("SLURM_STEP_ID"), "pid": os.getpid(),
                         "tty": os.isatty(0),
                         "ambient": [key for key in os.environ if key.startswith(("APPTAINER_BIND", "APPTAINERENV_", "SINGULARITY"))]}) + "\\n")
if os.environ.get("PROBE_MOUNTS"):
    probe = os.environ["PROBE_MOUNTS"]
    os.execv(os.environ["REAL_APPTAINER"], [os.environ["REAL_APPTAINER"], *sys.argv[1:-1],
                                          "/usr/bin/python3", "-c", probe])
if os.environ.get("INTERACTIVE_INPUT"):
    print("ready for terminal input", flush=True)
    print("received: " + input(), flush=True)
if os.environ.get("CODEX_WAIT"):
    import time
    print("waiting session", flush=True)
    time.sleep(60)
print("controlled Codex session")
raise SystemExit(int(os.environ.get("CODEX_EXIT", "0")))
''')

    def compute_events(self):
        path = Path(self.env["COMPUTE_LOG"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_launches_on_assigned_node_inside_owned_step_and_preserves_exit(self):
        self.env["CODEX_EXIT"] = "7"
        result = self.connect()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("controlled Codex session", result.stdout)
        events = self.compute_events()
        ssh, step, container = events
        self.assertIn("-tt", ssh["ssh"])
        self.assertEqual(ssh["ssh"][ssh["ssh"].index("-J") + 1], "test-login")
        self.assertTrue(ssh["ssh"][-2].endswith("@node01"))
        for flag in ("ForwardAgent=no", "ForwardX11=no", "ControlPath=none"):
            self.assertIn(flag, ssh["ssh"])
        for flag in ("--jobid=321", "--nodes=1", "--ntasks=1", "--cpus-per-task=2", "--pty"):
            self.assertIn(flag, step["srun"])
        self.assertEqual(container["job"], "321")
        self.assertEqual(container["step"], "0")
        self.assertEqual(container["apptainer"][-1], "/usr/local/bin/codex")
        self.assertEqual(container["apptainer"][container["apptainer"].index("--pwd") + 1], str(self.workspace.resolve()))
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])

    def test_node_platform_paths_runtime_and_ssh_failures_release_owned_job(self):
        for settings, expected in (({"NODE_ARCH": "aarch64"}, "Linux aarch64"),
                                   ({"NODE_SYSTEM": "Darwin"}, "Darwin x86_64"),
                                   ({"MISSING_CAPABILITY": "1"}, "capability"),
                                   ({"NODE_INACCESSIBLE": str(self.workspace.resolve())}, str(self.workspace.resolve())),
                                   ({"NODE_INACCESSIBLE": str((self.home / ".local/state/codex-hpc").resolve())}, "state path"),
                                   ({"NODE_SSH_FAIL": "1"}, None),
                                   ({"STEP_FAIL": "1"}, None)):
            with self.subTest(settings=settings):
                self.env.update(settings)
                result = self.connect()
                self.assertNotEqual(result.returncode, 0)
                if expected: self.assertIn(expected, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
                self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
                for key in settings: del self.env[key]

    def test_mounts_are_literal_and_ambient_mount_settings_cannot_escape_workspace(self):
        unusual = self.home / "workspace,colon:quote' æ"
        self.workspace.rename(unusual)
        self.workspace = unusual
        self.env.update(APPTAINER_BIND=str(self.home), APPTAINER_BINDPATH="/",
                        APPTAINER_MOUNT="type=bind,src=/,dst=/host",
                        APPTAINERENV_HOME=str(self.home), SINGULARITY_BIND="/",
                        SRUN_JOB_ID="777", SLURM_JOB_ID="777")
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        container = self.compute_events()[-1]
        self.assertEqual(container["ambient"], [])
        args = container["apptainer"]
        self.assertEqual(args[args.index("--no-mount") + 1], "hostfs,bind-paths,cwd")
        fields = next(csv.reader([args[args.index("--mount") + 1]]))
        self.assertEqual(fields, ["type=bind", "src=" + str(self.workspace.resolve()),
                                  "dst=" + str(self.workspace.resolve())])
        self.assertEqual(args.count("--mount"), 1)
        private_home = Path(args[args.index("--home") + 1].split(":")[0])
        self.assertFalse(private_home.exists())

    def test_terminal_input_reaches_the_interactive_step(self):
        self.env["INTERACTIVE_INPUT"] = "1"
        master, slave = pty.openpty()
        process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        output = b""
        try:
            deadline = time.monotonic() + 20
            while b"ready for terminal input" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]: output += os.read(master, 65536)
            self.assertIn(b"ready for terminal input", output)
            os.write(master, b"hello terminal\n")
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try: output += os.read(master, 65536)
                    except OSError: break
            self.assertEqual(process.wait(timeout=5), 0, output.decode())
            self.assertIn(b"received: hello terminal", output)
            self.assertTrue(self.compute_events()[-1]["tty"])
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            os.close(master)

    def interrupt_session(self, interrupt):
        self.env["CODEX_WAIT"] = "1"
        process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while not any("apptainer" in event for event in self.compute_events()):
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            container = self.compute_events()[-1]
            os.killpg(process.pid, interrupt)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
            for event in self.compute_events():
                with self.assertRaises(ProcessLookupError): os.kill(event["pid"], 0)
            self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

    def test_interrupt_removes_step_process_and_scratch_and_cancels_only_owned_job(self):
        self.interrupt_session(signal.SIGINT)

    def test_termination_unwinds_scratch_and_owned_job(self):
        self.interrupt_session(signal.SIGTERM)

    def test_real_container_has_only_workspace_and_private_ephemeral_home(self):
        if not self.real:
            self.skipTest("actual mount boundary requires Linux signed candidate CI")
        state = self.home / ".local/state/codex-hpc/secret"
        sibling = self.home / "sibling/secret"
        personal = self.home / "personal-secret"
        for path in (state, sibling, personal):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must remain inaccessible")
        forbidden = [str(path) for path in (state, sibling, personal)]
        self.env["PROBE_MOUNTS"] = '''
import json, os
from pathlib import Path
assert os.getcwd() == ''' + repr(str(self.workspace.resolve())) + '''
for name in ''' + repr(forbidden) + ''':
    assert not Path(name).exists(), "host path exposed: " + name
    assert not Path("/proc/1/root" + name).exists(), "host proc root exposed: " + name
home = Path.home()
assert str(home) == "/codex-hpc-home", home
(home / "settings").write_text("ephemeral")
Path("/tmp/ephemeral").write_text("scratch")
Path("persisted.txt").write_text("workspace write survives")
print("actual mount boundary passed")
'''
        self.env.update(APPTAINER_BIND=str(self.home), APPTAINERENV_HOME=str(self.home),
                        SINGULARITY_BIND="/")
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("actual mount boundary passed", result.stdout)
        self.assertEqual((self.workspace / "persisted.txt").read_text(), "workspace write survives")
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
        self.assertFalse((self.home / "settings").exists())

    def test_node_image_must_match_verified_release_before_step_launch(self):
        self.env["NODE_CHANGE_IMAGE"] = "1"
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compute image differs", result.stderr)
        self.assertEqual(len(self.compute_events()), 1)
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
