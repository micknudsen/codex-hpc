"""Maintainer commands are the release-preparation test boundary."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class CandidateInputs(unittest.TestCase):
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
                             "scripts/build_candidate.py"):
                path = source / relative
                before = path.read_bytes()
                if relative.endswith(".json"):
                    inputs = json.loads(before)
                    for field in ("base", "codex"):
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
