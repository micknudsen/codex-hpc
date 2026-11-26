"""Maintainer commands are the release-preparation test boundary."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import build_candidate


class CandidateInputs(unittest.TestCase):
    def test_build_stages_pinned_runtime_dependencies(self):
        lock = build_candidate.inputs()["inputs"]

        class ContextChecked(Exception):
            pass

        def fetch(spec, target):
            target.write_text(spec["url"])
            target.chmod(0o755)

        def run(*args, cwd=None, **kwargs):
            if args[:2] == ("apptainer", "--version"):
                return "apptainer version " + lock["build_tools"]["apptainer"]
            if args[0] == "mksquashfs":
                return "mksquashfs version " + lock["build_tools"]["squashfs_tools"]
            if args[0] == "dpkg-query":
                return lock["build_packages"][args[-1]]
            self.assertEqual(args[:2], ("apptainer", "build"))
            context = Path(cwd)
            section = (context / "codex.def").read_text().split("%files\n", 1)[1].split("\n%", 1)[0]
            files = {line.split()[1]: context / line.split()[0]
                     for line in section.splitlines() if line.strip()}
            host = files.get("/usr/local/bin/codex-code-mode-host")
            self.assertIsNotNone(host, "image omits /usr/local/bin/codex-code-mode-host")
            self.assertTrue(host.is_file(), "companion was not downloaded into build context")
            self.assertTrue(host.stat().st_mode & 0o111)
            self.assertEqual(host.read_text(), lock["code_mode_host"]["url"])
            self.assertEqual(lock["code_mode_host"]["version"], lock["codex"]["version"])
            self.assertIn("rust-v" + lock["codex"]["version"] + "/",
                          lock["code_mode_host"]["url"])
            self.assertEqual(files["/bubblewrap.deb"].read_text(), lock["bubblewrap"]["url"])
            self.assertEqual(files["/node.tar.xz"].read_text(), lock["node"]["url"])
            self.assertEqual(files["/coding-tools.sources"].read_bytes(),
                             (REPO / "image/coding-tools.sources").read_bytes())
            raise ContextChecked

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(build_candidate.platform, "system", return_value="Linux"), \
                mock.patch.object(build_candidate.platform, "machine", return_value="x86_64"), \
                mock.patch.object(build_candidate.platform, "python_version", return_value=lock["build_tools"]["python"]), \
                mock.patch.object(build_candidate, "fetch_binary", side_effect=fetch), \
                mock.patch.object(build_candidate, "fetch_verified", side_effect=fetch), \
                mock.patch.object(build_candidate, "run", side_effect=run):
            args = SimpleNamespace(version="0.2.0", output=str(Path(temporary) / "candidate"),
                                   private_key="unused-private.pem", public_key="unused-public.pem")
            with self.assertRaises(ContextChecked):
                build_candidate.build(args)

    def test_first_prepare_selects_build_before_requesting_build_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run([sys.executable, str(REPO / "scripts/build_candidate.py"),
                "prepare", "--version", "0.2.0", "--output", str(root / "candidate"),
                "--public-key", str(root / "public.pem")], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("new image: first image", result.stdout)
            self.assertIn("private key", result.stderr)
            self.assertFalse((root / "candidate").exists())

    def test_fingerprint_tracks_image_inputs_but_not_client_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            shutil.copytree(REPO, source, ignore=shutil.ignore_patterns(
                ".git", ".local", "build", "*.egg-info", "__pycache__"))

            def fingerprint():
                result = subprocess.run([sys.executable, str(source / "scripts/build_candidate.py"),
                                         "fingerprint"], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)["fingerprint"]

            original = fingerprint()
            self.assertEqual(original, fingerprint())
            with (source / "src/codex_hpc/cli.py").open("a") as stream:
                stream.write("\n# Client-only change\n")
            self.assertEqual(original, fingerprint())
            for relative in ("image/probe.sh", "image/inputs.json", "image/codex.def",
                             "image/coding-tools.sources",
                             "scripts/build_candidate.py"):
                path = source / relative
                before = path.read_bytes()
                if relative.endswith(".json"):
                    inputs = json.loads(before)
                    for field in ("base", "codex", "code_mode_host", "bubblewrap", "node"):
                        altered = json.loads(before)
                        altered[field]["sha256"] = "a" * 64
                        path.write_text(json.dumps(altered))
                        self.assertNotEqual(original, fingerprint(), field)
                    path.write_bytes(before)
                else:
                    with path.open("a") as stream:
                        stream.write("\n# Included build input changed\n")
                    self.assertNotEqual(original, fingerprint(), relative)
                    path.write_bytes(before)
