"""Public packaged setup under controlled SSH and release responses.

CODEX_HPC_TEST_CANDIDATE runs these same scenarios against the real signed SIF.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv

REPO = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RemoteCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = tempfile.TemporaryDirectory(prefix="codex-hpc-setup-install-")
        cls.addClassCleanup(cls.install.cleanup)
        root = Path(cls.install.name)
        candidate = os.environ.get("CODEX_HPC_TEST_CANDIDATE")
        cls.real = bool(candidate)
        if candidate:
            candidate = Path(candidate)
            cls.image = candidate / "codex-hpc-linux-amd64.sif"
            wheel = next(candidate.glob("*.whl"))
            cls.apptainer = shutil.which("apptainer")
        else:
            source = root / "source"
            shutil.copytree(REPO / "src", source / "src", ignore=shutil.ignore_patterns(
                "__pycache__", "*.egg-info", "release"))
            for file in ("pyproject.toml", "setup.cfg", "README.md", "LICENSE"):
                shutil.copyfile(REPO / file, source / file)
            bundle = source / "src/codex_hpc/release"
            bundle.mkdir()
            shutil.copyfile(source / "src/codex_hpc/remote_helper.py", bundle / "helper.py")
            (bundle / "trusted.pem").write_text("fixture trusted public key")
            cls.image = root / "fixture.sif"
            cls.image.write_bytes(b"signed fixture image")
            release = {"schema": 1, "version": "0.1.0.dev0", "protocol_version": 1,
                       "image_interface_version": 1, "helper_sha256": sha(bundle / "helper.py"),
                       "trust": {"format": "PEM", "public_key": "trusted.pem",
                                 "sha256": sha(bundle / "trusted.pem")},
                       "image": {"architecture": "linux/amd64", "sha256": sha(cls.image),
                                 "size": cls.image.stat().st_size,
                                 "url": "https://github.com/micknudsen/codex-hpc/releases/download/v0.1.0/codex-hpc-linux-amd64.sif"}}
            (bundle / "release.json").write_text(json.dumps(release))
            subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps",
                            "--no-build-isolation", "--wheel-dir", str(root / "wheels"),
                            str(source)], check=True, stdout=subprocess.DEVNULL)
            wheel = next((root / "wheels").glob("*.whl"))
        venv.create(root / "venv", with_pip=True)
        cls.python = root / "venv/bin/python"
        subprocess.run([str(cls.python), "-m", "pip", "install", "--no-index", "--no-deps",
                        str(wheel)], check=True, stdout=subprocess.DEVNULL)
        cls.cli = root / "venv/bin/codex-hpc"
        cls.bundle = Path(subprocess.check_output([str(cls.python), "-c",
            "from importlib.resources import files; print(files('codex_hpc').joinpath('release'))"],
            text=True).strip())
        cls.original_release = (cls.bundle / "release.json").read_text()
        cls.original_key = (cls.bundle / "trusted.pem").read_text()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-hpc-setup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "remote home æ"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("CODEX_HPC_", "XDG_", "PYTHON", "APPTAINER", "SINGULARITY"))}
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env["PATH"],
                        HOME=str(self.root / "local-home"), REMOTE_HOME=str(self.home),
                        EVENTS=str(self.root / "events"), FIXTURE_IMAGE=str(self.image),
                        REAL_APPTAINER=self.apptainer if self.real else "")
        self.release = json.loads(self.original_release)
        self.save_release()
        (self.bundle / "trusted.pem").write_text(self.original_key)
        self.script("ssh", '''
import os, subprocess, sys
env = os.environ.copy()
env["HOME"] = env["REMOTE_HOME"]
raise SystemExit(subprocess.call(["/bin/sh", "-c", sys.argv[-1]], env=env))
''')
        self.script("curl", '''
import os, shutil, sys
from pathlib import Path
with open(os.environ["EVENTS"], "a") as log: log.write("fetch " + sys.argv[-1] + "\\n")
shutil.copyfile(os.environ["FIXTURE_IMAGE"], sys.argv[sys.argv.index("--output") + 1])
''')
        self.script("apptainer", '''
import os, sys
from pathlib import Path
with open(os.environ["EVENTS"], "a") as log: log.write("verify\\n")
if os.environ.get("REAL_APPTAINER"):
    os.execv(os.environ["REAL_APPTAINER"], [os.environ["REAL_APPTAINER"], *sys.argv[1:]])
key = Path(sys.argv[sys.argv.index("--key") + 1]).read_text()
raise SystemExit(0 if key == "fixture trusted public key" else 1)
''')
        for forbidden in ("sbatch", "srun", "salloc", "codex"):
            self.script(forbidden, 'raise SystemExit("setup invoked a forbidden command")\n')

    def script(self, name, source):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + source)
        path.chmod(0o755)

    def save_release(self):
        (self.bundle / "release.json").write_text(json.dumps(self.release))

    def command(self):
        return [str(self.cli), "setup", "--host", "test-login"]

    def setup(self):
        return subprocess.run(self.command(), env=self.env, cwd=self.root,
                              capture_output=True, text=True)

    def events(self):
        path = self.root / "events"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def data(self):
        return self.home / ".local/share/codex-hpc"

    @property
    def cache(self):
        return self.home / ".cache/codex-hpc"


    def snapshot(self):
        current = self.data / "current"
        return os.readlink(current), {path.name: path.read_bytes() for path in current.iterdir()}


class SetupCLI(RemoteCLI):
    def test_cold_install_and_verified_warm_repeat(self):
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ready", result.stdout)
        current = self.data / "current"
        self.assertTrue(current.is_dir())
        self.assertEqual((current / "release.json").read_text(), json.dumps(self.release, sort_keys=True))
        self.assertEqual(sha(current / "helper.py"), self.release["helper_sha256"])
        images = list(self.cache.rglob("*.sif"))
        self.assertEqual(len(images), 1)
        self.assertEqual(sha(images[0]), self.release["image"]["sha256"])
        first_target = os.readlink(current)
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(current), first_target)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify", "verify"])
        self.assertFalse((self.home / ".apptainer").exists())

    def test_storage_preflight_prevents_download(self):
        # Simulate the filesystem's free-block response in the remote interpreter.
        self.script("python3", '''
import os, sys
os.statvfs = lambda path: os.statvfs_result((4096, 4096, 1, 0, 0, 0, 0, 0, 0, 255))
sys.argv.pop(1)
exec(compile(sys.stdin.read(), "<remote>", "exec"))
''')
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("space", result.stderr)
        self.assertEqual(self.events(), [])
        self.assertFalse((self.data / "current").exists())

    def test_failed_downloads_preserve_active_release_and_unrelated_state(self):
        self.assertEqual(self.setup().returncode, 0)
        before = self.snapshot()
        for name in (".apptainer/global-trust", ".local/state/codex-hpc/credentials", "unrelated"):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep this")
        self.release["version"] = "0.2.0"
        # A different signed digest needs a separate transfer, even for a newer client.
        self.release["image"]["sha256"] = "0" * 64
        self.save_release()
        for failure in ("transfer", "truncated", "corrupt", "quota"):
            with self.subTest(failure=failure):
                self.script("curl", '''
import os, sys
from pathlib import Path
with open(os.environ["EVENTS"], "a") as log: log.write("fetch " + sys.argv[-1] + "\\n")
output = Path(sys.argv[sys.argv.index("--output") + 1])
mode = ''' + repr(failure) + '''
if mode in ("transfer", "quota"):
    print("Disk quota exceeded" if mode == "quota" else "connection reset", file=sys.stderr)
    raise SystemExit(23)
output.write_bytes(b"bad" if mode == "truncated" else b"x" * int(os.environ["IMAGE_SIZE"]))
''')
                self.env["IMAGE_SIZE"] = str(self.release["image"]["size"])
                result = self.setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("download failed" if failure in ("transfer", "quota") else "checksum", result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(list(self.cache.rglob("*.sif"))), 1)
        for name in (".apptainer/global-trust", ".local/state/codex-hpc/credentials", "unrelated"):
            self.assertEqual((self.home / name).read_text(), "keep this")
        self.assertEqual(len([event for event in self.events() if event.startswith("fetch ")]), 5)

    def test_warm_wrong_key_and_corruption_never_activate_new_release(self):
        self.assertEqual(self.setup().returncode, 0)
        before = self.snapshot()
        self.release["version"] = "0.2.0"
        if self.real:
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                            "-out", str(self.root / "wrong.pem")], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", str(self.root / "wrong.pem"), "-pubout",
                            "-out", str(self.bundle / "trusted.pem")], check=True, capture_output=True)
        else:
            (self.bundle / "trusted.pem").write_text("wrong public key")
        self.release["trust"]["sha256"] = sha(self.bundle / "trusted.pem")
        self.save_release()
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signature", result.stderr)
        self.assertEqual(self.snapshot(), before)
        (self.bundle / "trusted.pem").write_text(self.original_key)
        self.release["trust"]["sha256"] = sha(self.bundle / "trusted.pem")
        self.save_release()
        image = next(self.cache.rglob("*.sif"))
        image.chmod(0o644)
        with image.open("r+b") as stream:
            stream.write(b"corrupt")
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len([event for event in self.events() if event.startswith("fetch ")]), 1)

    def test_new_client_reuses_original_asset_and_shared_digest(self):
        self.assertEqual(self.setup().returncode, 0)
        previous = self.snapshot()
        self.release["version"] = "0.2.0"
        original_url = self.release["image"]["url"]
        self.save_release()
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.snapshot()[0], previous[0])
        self.assertEqual(len(list(self.cache.rglob("*.sif"))), 1)
        self.assertEqual(self.events(), ["fetch " + original_url, "verify", "verify"])
        # With a cold cache the newer client still fetches the original asset URL.
        self.env["CODEX_HPC_CACHE_ROOT"] = str(self.home / "another cache")
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-2:], ["fetch " + original_url, "verify"])
        installed = json.loads((self.data / "current/installation.json").read_text())
        self.assertTrue(installed["image"].startswith(str(Path(self.env["CODEX_HPC_CACHE_ROOT"]).resolve()) + "/"))

    def test_concurrent_attempts_download_once_and_publish_complete_release(self):
        import time
        original = (self.bin / "curl").read_text().split("\n", 1)[1]
        self.script("curl", "import time\ntime.sleep(0.4)\n" + original)
        processes = [subprocess.Popen(self.command(), env=self.env, cwd=self.root,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("ready", stdout)
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify", "verify"])
        self.assertEqual(len(list((self.data / "releases").iterdir())), 1)
        self.assertEqual(sha(self.data / "current/helper.py"), self.release["helper_sha256"])

    def test_interrupted_transfer_keeps_previous_activation_and_can_retry(self):
        import signal
        import time
        self.assertEqual(self.setup().returncode, 0)
        before = self.snapshot()
        self.release["version"] = "0.2.0"
        self.save_release()
        self.env["CODEX_HPC_CACHE_ROOT"] = str(self.home / "retry-cache")
        original = (self.bin / "curl").read_text().split("\n", 1)[1]
        marker = self.root / "transfer-started"
        self.env["TRANSFER_MARKER"] = str(marker)
        self.script("curl", '''
import os, sys, time
from pathlib import Path
Path(sys.argv[sys.argv.index("--output") + 1]).write_bytes(b"partial")
Path(os.environ["TRANSFER_MARKER"]).touch()
time.sleep(30)
''')
        process = subprocess.Popen(self.command(), env=self.env, cwd=self.root, start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "transfer never started")
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        self.assertEqual(self.snapshot(), before)
        abandoned_parent = next((self.home / "retry-cache/images").iterdir())
        unrelated = abandoned_parent / ".download-unrelated"
        unrelated.mkdir()
        (unrelated / "notes").write_text("keep this")
        self.script("curl", original)
        # A leftover partial consumes the available filesystem capacity until recovered.
        self.env["ABANDONED_PARENT"] = str(abandoned_parent)
        self.script("python3", '''
import os, sys
from pathlib import Path
original_statvfs = os.statvfs
def storage(path):
    if any(Path(os.environ["ABANDONED_PARENT"]).glob(".download-*/image.sif")):
        return os.statvfs_result((4096, 4096, 1, 0, 0, 0, 0, 0, 0, 255))
    return original_statvfs(path)
os.statvfs = storage
sys.argv.pop(1)
exec(compile(sys.stdin.read(), "<remote>", "exec"))
''')
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.snapshot()[0], before[0])
        self.assertEqual(list(abandoned_parent.glob(".download-*")), [unrelated])
        self.assertEqual((unrelated / "notes").read_text(), "keep this")
        self.assertEqual(list((self.data / "releases").glob(".setup-*")), [])
        self.assertEqual(self.events(), ["fetch " + self.release["image"]["url"], "verify",
                                       "fetch " + self.release["image"]["url"], "verify"])

    def test_missing_tools_and_nonwritable_roots_do_not_fetch(self):
        # Remote tool discovery is a system boundary; no product functions are replaced.
        self.script("python3", '''
import os, shutil, sys
shutil.which = lambda name: None if name == "apptainer" else "/usr/bin/" + name
sys.argv.pop(1)
exec(compile(sys.stdin.read(), "<remote>", "exec"))
''')
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing: apptainer", result.stderr)
        self.assertEqual(self.events(), [])
        (self.bin / "python3").unlink()
        self.env["CODEX_HPC_DATA_ROOT"] = "/dev/null/installation"
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data root", result.stderr)
        self.assertEqual(self.events(), [])

    def test_damaged_installed_helper_cannot_report_ready(self):
        self.assertEqual(self.setup().returncode, 0)
        (self.data / "current/helper.py").write_text("damaged helper")
        before = self.snapshot()
        result = self.setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed release", result.stderr)
        self.assertEqual(self.snapshot(), before)
