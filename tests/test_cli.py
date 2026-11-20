"""Exercise only the clean-installed public command and its SSH boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv

REPO = Path(__file__).resolve().parents[1]


class PackagedCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = tempfile.TemporaryDirectory(prefix="codex-hpc-install-")
        cls.addClassCleanup(cls.install.cleanup)
        root = Path(cls.install.name)
        wheels = root / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps",
                        "--no-build-isolation", "--wheel-dir", str(wheels), str(REPO)], check=True)
        venv.create(root / "venv", with_pip=True)
        subprocess.run([str(root / "venv/bin/python"), "-m", "pip", "install",
                        "--no-index", "--no-deps", str(next(wheels.glob("*.whl")))], check=True)
        cls.cli = root / "venv/bin/codex-hpc"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-hpc-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("CODEX_HPC_", "XDG_", "PYTHON"))}
        self.env["HOME"] = str(self.root / "local-home")

    def run_cli(self, *args):
        return subprocess.run([str(self.cli), *args], env=self.env, cwd=self.root,
                              text=True, capture_output=True)

    def test_help_and_required_destination(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("setup", result.stdout)
        self.assertIn("connect", result.stdout)
        result = self.run_cli("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CODEX_HPC_HOST", result.stderr)
        result = self.run_cli("connect", "--host", "cluster")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace", result.stderr.lower())

    def controlled_ssh(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text("#!" + sys.executable + "\n" + '''
import json, os, subprocess, sys
from pathlib import Path
Path(os.environ["SSH_RECORD"]).write_text(json.dumps(sys.argv[1:]))
env = {key: value for key, value in os.environ.items()
       if not key.startswith(("XDG_", "CODEX_HPC_"))}
env.update(json.loads(os.environ["REMOTE_ENV"]))
raise SystemExit(subprocess.call(["/bin/sh", "-c", sys.argv[-1]], env=env))
''')
        ssh.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        self.env["SSH_RECORD"] = str(self.root / "ssh.json")
        self.remote_home = self.root / "remote home æ"
        self.remote_home.mkdir()
        self.remote_env = {"HOME": str(self.remote_home)}
        self.set_remote_env()

    def set_remote_env(self):
        self.env["REMOTE_ENV"] = json.dumps(self.remote_env)

    def test_setup_discovers_remote_defaults_without_claiming_ready(self):
        self.controlled_ssh()
        self.env["XDG_DATA_HOME"] = "/laptop/data"
        result = self.run_cli("setup", "--host", "user@cluster")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data root: " + str(self.remote_home / ".local/share/codex-hpc"), result.stdout)
        self.assertIn("cache root: " + str(self.remote_home / ".cache/codex-hpc"), result.stdout)
        self.assertIn("state root: " + str(self.remote_home / ".local/state/codex-hpc"), result.stdout)
        self.assertIn("not installed", result.stderr)
        argv = json.loads((self.root / "ssh.json").read_text())
        self.assertIn("user@cluster", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertIn("ForwardX11=no", argv)
        self.assertFalse(any("StrictHostKeyChecking" in arg or "UserKnownHostsFile" in arg
                             or "BatchMode" in arg for arg in argv))
        self.assertFalse((self.remote_home / ".local/share/codex-hpc").exists())
        self.assertFalse((self.remote_home / ".cache/codex-hpc").exists())
        self.assertFalse((self.remote_home / ".local/state/codex-hpc").exists())

    def test_cli_environment_and_remote_xdg_precedence_for_both_commands(self):
        self.controlled_ssh()
        self.remote_env.update(XDG_DATA_HOME="/remote/data", XDG_CACHE_HOME="relative/cache",
                               XDG_STATE_HOME="/remote/state")
        self.set_remote_env()
        self.env.update(CODEX_HPC_HOST="env-alias", CODEX_HPC_DATA_ROOT="/environment/data",
                        CODEX_HPC_CACHE_ROOT="~/environment cache")
        for command in (("setup",), ("connect", "~/workspace")):
            with self.subTest(command=command):
                result = self.run_cli(*command, "--host", "cli-alias", "--data-root", "/CLI æ/data")
                self.assertIn("SSH destination: cli-alias", result.stdout)
                self.assertIn("data root: /CLI æ/data", result.stdout)
                self.assertIn("cache root: " + str(self.remote_home / "environment cache"), result.stdout)
                self.assertIn("state root: /remote/state/codex-hpc", result.stdout)
                self.assertNotEqual(result.returncode, 0)
        del self.env["CODEX_HPC_DATA_ROOT"]
        del self.env["CODEX_HPC_CACHE_ROOT"]
        result = self.run_cli("setup")
        self.assertIn("SSH destination: env-alias", result.stdout)
        self.assertIn("data root: /remote/data/codex-hpc", result.stdout)
        self.assertIn("cache root: " + str(self.remote_home / ".cache/codex-hpc"), result.stdout)

    def test_rejects_unsupported_storage_paths_and_file_roots(self):
        self.controlled_ssh()
        file_root = self.root / "regular file"
        file_root.write_text("keep me")
        for path in ("relative/path", "", "~someone/data", str(file_root), str(file_root / "child")):
            with self.subTest(path=path):
                result = self.run_cli("setup", "--host", "cluster", "--data-root", path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("data root", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("data root:", result.stdout)
        self.assertEqual(file_root.read_text(), "keep me")

    def test_discovery_failures_are_actionable(self):
        self.controlled_ssh()
        ssh = self.root / "bin/ssh"
        for behavior in ('echo "permission denied" >&2; exit 255',
                         'printf "login banner\\n{}\\n"', 'echo "[]"'):
            with self.subTest(behavior=behavior):
                ssh.write_text("#!/bin/sh\n" + behavior + "\n")
                result = self.run_cli("setup", "--host", "cluster")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("discovery", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("root:", result.stdout)

    def test_paths_are_literal_and_home_expansion_stays_remote(self):
        self.controlled_ssh()
        for path in ("~/quotes ' \" ; $(touch INJECTED) `touch INJECTED` $HOME æ",
                     "~//double slash"):
            with self.subTest(path=path):
                result = self.run_cli("setup", "--host", "cluster", "--data-root", path)
                expected = str(self.remote_home) + "/" + path[2:]
                self.assertIn("data root: " + expected, result.stdout, result.stderr)
                self.assertFalse((self.root / "INJECTED").exists())

    def test_rejects_invalid_destination_before_ssh(self):
        self.controlled_ssh()
        for host in ("", "-oProxyCommand=bad", "host\nname", "host name"):
            with self.subTest(host=host):
                result = self.run_cli("setup", "--host=" + host)
                self.assertEqual(result.returncode, 2)
                self.assertIn("destination", result.stderr)
                self.assertFalse((self.root / "ssh.json").exists())

    def test_each_storage_root_has_independent_precedence(self):
        self.controlled_ssh()
        self.env["CODEX_HPC_HOST"] = "alias"
        for name in ("data", "cache", "state"):
            with self.subTest(root=name):
                variable = "CODEX_HPC_" + name.upper() + "_ROOT"
                self.env[variable] = "/env/" + name
                result = self.run_cli("setup")
                self.assertIn(name + " root: /env/" + name, result.stdout)
                result = self.run_cli("setup", "--" + name + "-root", "~/cli " + name)
                self.assertIn(name + " root: " + str(self.remote_home / ("cli " + name)), result.stdout)
                del self.env[variable]

    def test_remote_home_and_xdg_errors(self):
        self.controlled_ssh()
        self.remote_env.update(XDG_DATA_HOME="relative", XDG_CACHE_HOME="", XDG_STATE_HOME="~/state")
        self.set_remote_env()
        result = self.run_cli("setup", "--host", "cluster")
        for path in (".local/share/codex-hpc", ".cache/codex-hpc", ".local/state/codex-hpc"):
            self.assertIn(str(self.remote_home / path), result.stdout)
        self.remote_env["HOME"] = "relative"
        self.set_remote_env()
        result = self.run_cli("setup", "--host", "cluster")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote HOME", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_ssh_is_actionable(self):
        self.env["PATH"] = str(self.root / "missing-bin")
        result = self.run_cli("setup", "--host", "cluster")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install OpenSSH", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
