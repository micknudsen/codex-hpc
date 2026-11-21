"""Prepare the release image using a source-controlled pin to an earlier publication."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen

from build_candidate import digest, inputs, prior_inputs
from release_identity import REPOSITORY, VERSION, version_at


def download(url, target, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Expected a pinned SHA-256")
    with urlopen(url, timeout=60) as response, target.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    if digest(target) != expected:
        raise ValueError("Published asset differs from its trusted pin: " + target.name)


def prepare(args):
    if args.candidate.exists() or args.image.exists():
        raise ValueError("Candidate and selected-image destinations must be fresh")
    if args.candidate.resolve() in args.image.resolve().parents:
        raise ValueError("Selected-image working copy must be outside candidate output")
    with tempfile.TemporaryDirectory(prefix="codex-hpc-prior-image-") as temporary:
        previous = Path(temporary)
        command = [sys.executable, "scripts/build_candidate.py", "prepare", "--version", version_at(),
                   "--output", str(args.candidate)]
        reuse = False
        if args.pin.exists():
            pin = json.loads(args.pin.read_text())
            if not re.fullmatch("v" + VERSION, pin["tag"]):
                raise ValueError("Previous release pin requires a version tag")
            names = {"image-release.json": "release.json", "trusted.pem": "trusted.pem",
                     "image-provenance.json": "provenance.json"}
            if set(pin["sha256"]) != set(names):
                raise ValueError("Pin must identify metadata, public key and provenance")
            for name, local in names.items():
                download(f"https://github.com/{REPOSITORY}/releases/download/{pin['tag']}/{name}",
                         previous / local, pin["sha256"][name])
            prior, _ = prior_inputs(previous, inputs())
            reuse = prior is not None
            command += ["--previous", str(previous), "--trusted-release", str(previous / "release.json")]
            if reuse:
                metadata = json.loads((previous / "release.json").read_text())
                image = metadata["image"]
                if not re.fullmatch(r"https://github\.com/micknudsen/codex-hpc/releases/download/v[0-9A-Za-z.\-]+/codex-hpc-linux-amd64\.sif", image["url"]):
                    raise ValueError("Pinned image must retain its original official Release URL")
                download(image["url"], previous / "codex-hpc-linux-amd64.sif", image["sha256"])
                command += ["--public-key", str(previous / "trusted.pem")]
        if not reuse:
            if not args.private_key.is_file() or not args.public_key.is_file():
                raise ValueError("A new release image requires SIF_SIGNING_PRIVATE_KEY; PR CI uses disposable keys")
            command += ["--private-key", str(args.private_key), "--public-key", str(args.public_key)]
        subprocess.run(command, check=True)
        selected = (previous if reuse else args.candidate) / "codex-hpc-linux-amd64.sif"
        shutil.copyfile(selected, args.image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=Path("release/previous-image.json"))
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "Release image preparation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
