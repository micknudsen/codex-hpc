"""Packaged connect preflight against a prepared remote release."""
import subprocess
import tempfile
import shlex
import shutil

from test_setup import RemoteCLI


class ConnectCLI(RemoteCLI):
    def setUp(self):
        super().setUp()
        for tool in ("sbatch", "salloc", "srun"):
            self.script(tool, '''
import os
with open(os.environ["EVENTS"], "a") as log: log.write("SLURM SUBMISSION\\n")
raise SystemExit(99)
''')

    def connect(self, workspace, *arguments):
        return subprocess.run([str(self.cli), "connect", str(workspace), "--host", "test-login",
                               *arguments], env=self.env, cwd=self.root,
                              capture_output=True, text=True)

    def test_project_symlink_and_remote_home_resolve_before_verification(self):
        workspace = self.home / "projects/my project æ"
        workspace.mkdir(parents=True)
        (self.home / "shortcut").symlink_to(workspace)
        self.assertEqual(self.setup().returncode, 0)
        for selected in ("~/shortcut", workspace):
            result = self.connect(selected)
            self.assertIn("Workspace: " + str(workspace.resolve()), result.stdout, result.stderr)
            self.assertIn("Preflight passed", result.stdout, result.stderr)
            self.assertNotEqual(result.returncode, 0)  # Session launch is a later issue.
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"],
                                        "verify", "verify", "verify"])

    def test_invalid_workspaces_stop_before_verification_or_submission(self):
        self.assertEqual(self.setup().returncode, 0)
        regular = self.home / "file"
        regular.write_text("preserve")
        broken = self.home / "broken"
        broken.symlink_to(self.home / "missing")
        for path in ("project", "", "~other/project", regular, broken, self.home / "missing",
                     str(self.home) + "/newline\npath"):
            with self.subTest(path=path):
                result = self.connect(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("workspace", result.stderr.lower())
                self.assertNotIn("Preflight passed", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
        self.assertIn("'~/project'", self.connect("project").stderr)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify"])

    def test_cached_image_tampering_and_signature_failure_stop_without_submission(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        self.assertEqual(self.setup().returncode, 0)
        image = next(self.cache.rglob("*.sif"))
        image.chmod(0o644)
        with image.open("r+b") as stream:
            stream.write(b"corrupt")
        result = self.connect(workspace)
        self.assertIn("checksum", result.stderr)
        self.assertNotIn("Preflight passed", result.stdout)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify"])
        shutil.copyfile(self.image, image)
        # Force a real signature rejection in image CI, and a controlled rejection locally.
        if self.real:
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                            "-out", str(self.root / "wrong.pem")], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", str(self.root / "wrong.pem"), "-pubout",
                            "-out", str(self.root / "wrong-public.pem")], check=True, capture_output=True)
        self.env["WRONG_KEY"] = str(self.root / "wrong-public.pem")
        self.script("apptainer", '''
import os, sys
with open(os.environ["EVENTS"], "a") as log: log.write("verify rejected\\n")
if os.environ.get("REAL_APPTAINER"):
    sys.argv[sys.argv.index("--key") + 1] = os.environ["WRONG_KEY"]
    os.execv(os.environ["REAL_APPTAINER"], [os.environ["REAL_APPTAINER"], *sys.argv[1:]])
raise SystemExit(1)
''')
        result = self.connect(workspace)
        self.assertIn("signature verification failed", result.stderr)
        self.assertNotIn("Preflight passed", result.stdout)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify", "verify rejected"])

    def test_managed_roots_and_symlink_exposure_are_rejected(self):
        self.assertEqual(self.setup().returncode, 0)
        workspace = self.home / "workspace"
        workspace.mkdir()
        state = self.home / ".local/state/codex-hpc"
        for selected in (self.home, self.data, self.cache, state, self.data / "releases",
                         tempfile.gettempdir()):
            with self.subTest(selected=selected):
                result = self.connect(selected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe workspace", result.stderr)
                self.assertNotIn("Preflight passed", result.stdout)
        for target in (self.data, self.cache, state, self.home):
            with self.subTest(link=target):
                link = workspace / "exposed"
                link.symlink_to(target)
                result = self.connect(workspace)
                self.assertIn("unsafe workspace", result.stderr)
                self.assertIn("exposed", result.stderr)
                link.unlink()
        # A benign cycle must not hang traversal or reject a safe workspace.
        (workspace / "cycle").symlink_to(workspace)
        result = self.connect(workspace)
        self.assertIn("Preflight passed", result.stdout, result.stderr)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify", "verify"])

    def test_missing_matching_release_prints_literal_recovery_command(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        overrides = {name: str(self.home / (name + " ' ; $(touch INJECTED) æ"))
                     for name in ("data", "cache", "state")}
        arguments = [item for name, path in overrides.items() for item in ("--" + name + "-root", path)]
        result = self.connect(workspace, *arguments)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matching release is not ready", result.stderr)
        recovery = next(line.removeprefix("Run: ") for line in result.stderr.splitlines()
                        if line.startswith("Run: "))
        expected = ["codex-hpc", "setup", "--host", "test-login", *arguments]
        self.assertEqual(shlex.split(recovery), expected)
        self.assertFalse((self.root / "INJECTED").exists())
        self.assertEqual(self.events(), [])
        # The displayed command must actually prepare the same selected roots.
        command = shlex.split(recovery)
        command[0] = str(self.cli)
        installed = subprocess.run(command, env=self.env, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("Preflight passed", self.connect(workspace, *arguments).stdout)

    def test_resolved_managed_paths_and_temporary_root_cannot_enter_workspace(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        self.assertEqual(self.setup().returncode, 0)
        credentials = workspace / "credentials"
        credentials.mkdir()
        alias = self.home / "state alias"
        alias.symlink_to(credentials)
        result = self.connect(workspace, "--state-root", str(alias))
        self.assertIn("unsafe workspace", result.stderr)
        self.env["TMPDIR"] = str(workspace)
        result = self.connect(workspace)
        self.assertIn("temporary root", result.stderr)
        del self.env["TMPDIR"]
        image = next(self.cache.rglob("*.sif"))
        relocated = workspace / "image.sif"
        image.rename(relocated)
        image.symlink_to(relocated)
        result = self.connect(workspace)
        self.assertIn("unsafe workspace", result.stderr)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify"])

    def test_incomplete_or_modified_installation_stops_with_setup_recovery(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        self.assertEqual(self.setup().returncode, 0)
        current = self.data / "current"
        for name in ("helper.py", "trusted.pem", "release.json", "installation.json"):
            path = current / name
            original = path.read_bytes()
            for content in (None, b"modified installed file"):
                with self.subTest(file=name, content=content):
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    try:
                        result = self.connect(workspace)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("matching release is not ready", result.stderr)
                        self.assertIn("Run: codex-hpc setup", result.stderr)
                        self.assertNotIn("Preflight passed", result.stdout)
                    finally:
                        path.write_bytes(original)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify"])

    def test_cyclic_managed_image_is_an_actionable_installation_error(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        self.assertEqual(self.setup().returncode, 0)
        image = next(self.cache.rglob("*.sif"))
        image.unlink()
        image.symlink_to(image.name)
        result = self.connect(workspace)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matching release is not ready", result.stderr)
        self.assertIn(image.name, result.stderr)
        self.assertIn("Run: codex-hpc setup", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify"])
