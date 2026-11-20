"""Qualify a candidate wheel against exact image bytes through installed commands.

SSH, release transfer and Slurm are controlled peers; Apptainer and image execution
are real. This is release compatibility, not real-cluster or OAuth qualification.
"""
import json
import os
from pathlib import Path
import sys
import unittest

from test_compute import ComputeFixture


class CandidateCompatibility(ComputeFixture):
    def test_installed_setup_and_connect_use_exact_image(self):
        expected = self.release["image"]
        # Fixture setup already exercised a cold transfer from the original URL.
        self.assertEqual(self.events().count("fetch " + expected["url"]), 1)
        self.assertEqual(self.setup().returncode, 0)
        self.assertEqual(self.events().count("fetch " + expected["url"]), 1)
        credentials = self.home / ".local/state/codex-hpc/credentials"
        credentials.mkdir(mode=0o700, parents=True, exist_ok=True)
        (credentials / "auth.json").write_text(json.dumps({"auth_mode": "apikey",
                                                          "OPENAI_API_KEY": "disposable-compatibility-key"}))
        (credentials / "auth.json").chmod(0o600)
        self.env["PROBE_MOUNTS"] = (
            "from pathlib import Path; import subprocess; "
            "print(subprocess.check_output(['/usr/local/bin/codex', '--version'], text=True).strip()); "
            "Path(" + repr(str(self.workspace / "compatibility-write")) + ").write_text('compatible')")
        result = self.connect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("codex-cli " + expected["codex_version"], result.stdout)
        self.assertEqual((self.workspace / "compatibility-write").read_text(), "compatible")
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        self.assertEqual(list(Path(self.env["NODE_TMP"]).iterdir()), [])
        self.assertEqual(self.events().count("fetch " + expected["url"]), 1)
        metadata = json.loads((self.data / "current/release.json").read_text())
        self.assertEqual(metadata["image"], expected)


if __name__ == "__main__":
    candidate, image = map(lambda value: str(Path(value).resolve()), sys.argv[1:])
    os.environ["CODEX_HPC_TEST_CANDIDATE"] = candidate
    os.environ["CODEX_HPC_TEST_IMAGE"] = image
    unittest.main(argv=[sys.argv[0]], verbosity=2)
