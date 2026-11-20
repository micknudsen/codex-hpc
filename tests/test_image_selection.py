"""Candidate preparation is tested as a maintainer command, not private functions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ImageSelection(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-hpc-selection-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        shutil.copytree(REPO, self.source, ignore=shutil.ignore_patterns(
            ".git", ".local", "build", "*.egg-info", "__pycache__"))
        self.previous = self.root / "previous"
        self.previous.mkdir()
        self.image = self.previous / "codex-hpc-linux-amd64.sif"
        self.image.write_bytes(b"signed by trusted fixture publisher")
        self.key = self.root / "trusted.pem"
        self.key.write_text("trusted fixture publisher")
        fingerprint = subprocess.check_output([sys.executable,
            str(self.source / "scripts/build_candidate.py"), "fingerprint"], text=True)
        self.provenance = json.loads(fingerprint)
        self.provenance.update(signed_image_sha256=sha(self.image), build_tools_observed={
            name: self.provenance["inputs"]["build_tools"][name]
            for name in ("apptainer", "python", "squashfs_tools")},
            build_packages_observed=self.provenance["inputs"]["build_packages"], base_package_versions=["fixture=1"],
            qualification_output=["fixture qualified"])
        self.write_provenance()
        lock = self.provenance["inputs"]
        self.release = {"schema": 1, "candidate": False, "version": "0.1.0",
            "protocol_version": 1, "image_interface_version": 1,
            "image": {"url": "https://github.com/micknudsen/codex-hpc/releases/download/v0.1.0/codex-hpc-linux-amd64.sif",
                "architecture": "linux/amd64", "sha256": sha(self.image),
                "size": self.image.stat().st_size, "input_fingerprint": self.provenance["fingerprint"],
                "codex_version": lock["codex"]["version"], "base_digest": "sha256:" + lock["base"]["sha256"]},
            "trust": {"format": "PEM", "public_key": "trusted.pem", "sha256": sha(self.key)},
            "executables": {"codex": "/usr/local/bin/codex"}}
        self.trusted = self.root / "trusted-release.json"
        self.write_release()
        self.output = self.root / "candidate"
        binary = self.root / "bin"
        binary.mkdir()
        self.events = self.root / "actions"
        apptainer = binary / "apptainer"
        apptainer.write_text("#!" + sys.executable + '\n' + '''
import json, os, subprocess, sys
from pathlib import Path
with open(os.environ["PREPARE_ACTIONS"], "a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] in ("build", "sign"): raise SystemExit("reuse attempted image mutation")
if sys.argv[1] == "verify":
    key = Path(sys.argv[sys.argv.index("--key") + 1]).read_text()
    raise SystemExit(0 if key == "trusted fixture publisher" and Path(sys.argv[-1]).read_bytes().startswith(b"signed by trusted") else 1)
if sys.argv[-1] == "/usr/local/bin/codex-hpc-probe":
    print("codex-cli 0.170.0\\nripgrep 15.1.0")
elif "dpkg-query" in sys.argv: print("fixture=1")
elif "-c" in sys.argv:
    probe = sys.argv[-1].replace("/usr/local/bin/codex", str(Path(__file__).with_name("codex")))
    raise SystemExit(subprocess.call([sys.executable, "-c", probe]))
elif sys.argv[-1] == "--version": print("codex-cli 0.170.0")
else: raise SystemExit("unexpected fixture runtime operation")
''')
        apptainer.chmod(0o755)
        codex = binary / "codex"
        codex.write_text("#!" + sys.executable + '\nprint("codex-cli 0.170.0")\n')
        codex.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(PATH=str(binary) + os.pathsep + self.env["PATH"],
                        PREPARE_ACTIONS=str(self.events))

    def write_provenance(self):
        (self.previous / "provenance.json").write_text(json.dumps(self.provenance))

    def write_release(self):
        self.trusted.write_text(json.dumps(self.release))

    def prepare(self, *arguments):
        return subprocess.run([sys.executable, str(self.source / "scripts/build_candidate.py"),
            "prepare", "--version", "0.2.0", "--output", str(self.output),
            "--previous", str(self.previous), "--trusted-release", str(self.trusted),
            "--public-key", str(self.key), *arguments], env=self.env, text=True,
            capture_output=True, timeout=60)

    def test_equal_inputs_reuse_original_identity_without_build_sign_or_image_upload(self):
        with (self.source / "src/codex_hpc/cli.py").open("a") as stream:
            stream.write("\n# Client-only update\n")
        before = self.image.read_bytes()
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        release = json.loads((self.output / "release/release.json").read_text())
        self.assertEqual(release["version"], "0.2.0")
        self.assertEqual(release["image"], self.release["image"])
        self.assertEqual(release["image_source_release"], "v0.1.0")
        self.assertFalse((self.output / self.image.name).exists())
        selection = json.loads((self.output / "selection.json").read_text())
        self.assertEqual(selection["decision"], "reuse")
        self.assertEqual(selection["actions"], {"build": False, "sign": False, "upload_image": False})
        self.assertEqual(self.image.read_bytes(), before)
        self.assertTrue(next(self.output.glob("*.whl")))
        actions = [json.loads(line)[0] for line in self.events.read_text().splitlines()]
        self.assertNotIn("build", actions)
        self.assertNotIn("sign", actions)

    def test_incompatible_candidate_is_not_published(self):
        (self.source / "src/codex_hpc/cli.py").write_text(
            'def main():\n    raise SystemExit("candidate client incompatible")\n')
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("compatibility", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.image.read_bytes(), b"signed by trusted fixture publisher")

    def test_changed_or_incomplete_inputs_take_new_build_path(self):
        definition = self.source / "image/codex.def"
        original = definition.read_text()
        definition.write_text(original + "\n# new included input\n")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new image: changed image inputs", result.stdout)
        self.assertIn("private key", result.stderr)
        definition.write_text(original)
        self.provenance["build_tools_observed"] = {}
        self.write_provenance()
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new image: incomplete provenance", result.stdout)
        self.assertIn("private key", result.stderr)
        self.assertFalse(self.output.exists())

    def test_integrity_or_trust_failure_never_falls_back_to_build(self):
        original = self.image.read_bytes()
        for failure in ("altered bytes", "wrong signer", "untrusted key", "untrusted location"):
            with self.subTest(failure=failure):
                self.image.write_bytes(original)
                self.key.write_text("trusted fixture publisher")
                self.release["image"]["url"] = "https://github.com/micknudsen/codex-hpc/releases/download/v0.1.0/codex-hpc-linux-amd64.sif"
                if failure == "altered bytes": self.image.write_bytes(b"altered image")
                if failure == "wrong signer": self.image.write_bytes(b"signed by wrong publisher")
                if failure == "untrusted key": self.key.write_text("wrong public key")
                if failure == "untrusted location": self.release["image"]["url"] = "https://example.invalid/image.sif"
                if failure != "altered bytes":
                    self.release["image"].update(sha256=sha(self.image), size=self.image.stat().st_size)
                    self.provenance["signed_image_sha256"] = sha(self.image)
                self.write_release()
                self.write_provenance()
                result = self.prepare("--private-key", str(self.root / "must-not-read.pem"))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Preparing reused image", result.stdout)
                self.assertFalse(self.output.exists())
                self.assertNotIn("new image", result.stdout)

    def test_incompatible_helper_or_image_runtime_blocks_preparation(self):
        helper = self.source / "src/codex_hpc/remote_helper.py"
        before = helper.read_text()
        helper.write_text('raise SystemExit("helper incompatible")\n')
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compatibility", result.stderr)
        self.assertFalse(self.output.exists())
        helper.write_text(before)
        codex = self.root / "bin/codex"
        codex.write_text("#!" + sys.executable + '\nprint("codex-cli incompatible")\n')
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compatibility", result.stderr)
        self.assertFalse(self.output.exists())

