"""Self-contained helper bundled beside its matching release metadata."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify(image, release, key):
    expected = release["image"]
    if image.stat().st_size != expected["size"] or digest(image) != expected["sha256"]:
        raise ValueError("image checksum or size mismatch; remove the damaged managed image and rerun setup")
    environment = os.environ.copy()
    environment.update(APPTAINER_CONFIGDIR=str(key.parent / "apptainer"),
                       APPTAINER_KEYSDIR=str(key.parent / "keys"))
    result = subprocess.run(["apptainer", "verify", "--key", str(key), str(image)],
                            capture_output=True, text=True, env=environment)
    if result.returncode:
        raise ValueError("image signature verification failed with this client's trusted public key: " +
                         result.stderr.strip())


def prepare_image(cache, release, key):
    images = cache / "images" / release["image"]["architecture"]
    images.mkdir(parents=True, exist_ok=True)
    image = images / (release["image"]["sha256"] + ".sif")
    if image.exists():
        verify(image, release, key)
        return image
    if shutil.disk_usage(images).free < release["image"]["size"] + 1024 * 1024:
        raise ValueError("insufficient free space for image download at " + str(images))
    with tempfile.TemporaryDirectory(prefix=".download-", dir=images) as temporary:
        partial = Path(temporary) / "image.sif"
        result = subprocess.run(["curl", "--fail", "--location", "--proto", "=https",
                                 "--proto-redir", "=https", "--silent", "--show-error",
                                 "--output", str(partial), release["image"]["url"]],
                                capture_output=True, text=True)
        if result.returncode:
            raise ValueError("image download failed (transfer or storage/quota error): " +
                             result.stderr.strip())
        verify(partial, release, key)
        partial.chmod(0o444)
        os.replace(partial, image)
    return image


def setup(payload):
    os.umask(0o077)
    roots = {name: Path(path).resolve() for name, path in payload["roots"].items()}
    release = payload["release"]
    image_metadata = release["image"]
    if (release["schema"] != 1 or release["protocol_version"] != 1
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", image_metadata["architecture"])
            or not re.fullmatch(r"[0-9a-f]{64}", image_metadata["sha256"])
            or not isinstance(image_metadata["size"], int) or image_metadata["size"] <= 0
            or not image_metadata["url"].startswith(
                "https://github.com/micknudsen/codex-hpc/releases/download/")):
        raise ValueError("invalid or unsupported bundled release metadata")
    for name, root in roots.items():
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with tempfile.TemporaryFile(dir=root):
                pass
        except OSError as error:
            raise ValueError(name + " root is not writable: " + str(root) + ": " + str(error))
    for tool in ("curl", "apptainer"):
        if shutil.which(tool) is None:
            raise ValueError("required login-host tool is missing: " + tool)
    with ExitStack() as locks:
        for root in sorted({roots["data"], roots["cache"]}):
            lock = locks.enter_context((root / ".setup.lock").open("a"))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        releases = roots["data"] / "releases"
        releases.mkdir(exist_ok=True)
        if shutil.disk_usage(releases).free < 1024 * 1024:
            raise ValueError("insufficient free space for release staging at " + str(releases))
        encoded = json.dumps(release, sort_keys=True)
        with tempfile.TemporaryDirectory(prefix=".setup-", dir=releases) as temporary:
            stage = Path(temporary)
            key = stage / "trusted.pem"
            key.write_text(payload["key"])
            helper = stage / "helper.py"
            helper.write_text(payload["helper"])
            if digest(key) != release["trust"]["sha256"] or digest(helper) != release["helper_sha256"]:
                raise ValueError("bundled helper or public key checksum mismatch")
            image = prepare_image(roots["cache"], release, key)
            (stage / "release.json").write_text(encoded)
            installation = json.dumps({"image": str(image)})
            (stage / "installation.json").write_text(installation)
            identity = hashlib.sha256((encoded + installation).encode()).hexdigest()
            target = releases / identity
            if target.exists():
                expected_files = ("helper.py", "trusted.pem", "release.json", "installation.json")
                if target.is_symlink() or any(
                        not (target / name).is_file() or (target / name).is_symlink()
                        or (target / name).read_bytes() != (stage / name).read_bytes()
                        for name in expected_files):
                    raise ValueError("installed release is damaged at " + str(target) +
                                     "; preserve any running sessions before removing it and retrying setup")
            else:
                # Keep TemporaryDirectory's own directory alive until its exit.
                ready = stage / "ready"
                ready.mkdir()
                for child in (key, helper, stage / "release.json", stage / "installation.json"):
                    child.rename(ready / child.name)
                ready.rename(target)
            current = roots["data"] / "current"
            if current.exists() and not current.is_symlink():
                raise ValueError("refusing to replace non-managed current path: " + str(current))
            pointer = stage / "current"
            pointer.symlink_to(target.relative_to(roots["data"]))
            os.replace(pointer, current)
    print("Release " + release["version"] + " ready at " + str(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--describe", action="store_true",
                          help="Report this helper's release and protocol identity.")
    commands.add_argument("--setup", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.describe:
        release = json.loads(Path(__file__).with_name("release.json").read_text())
        print(json.dumps({key: release[key] for key in (
            "version", "protocol_version", "image_interface_version")}))
    else:
        setup(json.loads(args.setup))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("codex-hpc: setup failed: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("codex-hpc: setup interrupted; previous activation preserved; rerun setup", file=sys.stderr)
        sys.exit(130)
