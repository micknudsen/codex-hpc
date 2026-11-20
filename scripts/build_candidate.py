#!/usr/bin/env python3
"""Build, sign and qualify a candidate; never publish it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def inputs():
    lock = json.loads((REPO / "image/inputs.json").read_text())
    paths = sorted((REPO / "image").rglob("*")) + [Path(__file__).resolve(),
                                                     REPO / "scripts/install_apptainer_ci.sh"]
    files = {str(path.relative_to(REPO)): digest(path) for path in paths if path.is_file()}
    resolved = {"schema": 1, "inputs": lock, "files": files}
    fingerprint = hashlib.sha256(json.dumps(resolved, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
    return dict(resolved, fingerprint=fingerprint)


def run(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def fetch_binary(spec, target):
    archive = target.with_suffix(".tar.gz")
    with urllib.request.urlopen(spec["url"]) as response, archive.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    if digest(archive) != spec["sha256"]:
        raise ValueError("download checksum mismatch: " + spec["url"])
    with tarfile.open(archive) as package:
        member = package.getmember(spec["member"])
        if not member.isfile():
            raise ValueError("binary archive member is not a regular file")
        with package.extractfile(member) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
    target.chmod(0o755)
    archive.unlink()


def package_client(stage, result, release, public_key):
    package = stage / "package"
    package.mkdir()
    for name in ("pyproject.toml", "setup.cfg", "README.md", "LICENSE"):
        shutil.copyfile(REPO / name, package / name)
    shutil.copytree(REPO / "src", package / "src", ignore=shutil.ignore_patterns(
        "*.egg-info", "__pycache__", "release"))
    config = package / "setup.cfg"
    config.write_text(re.sub(r"(?m)^version = .*", "version = " + release["version"],
                             config.read_text()))
    bundled = package / "src/codex_hpc/release"
    bundled.mkdir()
    shutil.copyfile(REPO / "src/codex_hpc/remote_helper.py", bundled / "helper.py")
    release["helper_sha256"] = digest(bundled / "helper.py")
    write_json(bundled / "release.json", release)
    shutil.copyfile(public_key, bundled / "trusted.pem")
    shutil.copytree(bundled, result / "release")
    run(sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
        "--wheel-dir", str(result), str(package))


def export_source(result):
    source = result / "source"
    source.mkdir()
    for directory in ("image", "src", "tests"):
        shutil.copytree(REPO / directory, source / directory, ignore=shutil.ignore_patterns(
            "*.egg-info", "__pycache__", "release"))
    for relative in ("scripts/build_candidate.py", "scripts/install_apptainer_ci.sh",
                     "docs/build-image.md", "README.md", "pyproject.toml", "setup.cfg", "LICENSE"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)


def build(args):
    provenance = inputs()
    lock = provenance["inputs"]
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("candidate builds require Linux x86-64")
    expected = lock["build_tools"]
    actual = {"apptainer": run("apptainer", "--version").split()[-1],
              "python": platform.python_version(),
              "squashfs_tools": run("mksquashfs", "-version").splitlines()[0].split()[2]}
    for tool, version in actual.items():
        if version != expected[tool]:
            raise ValueError(tool + " version " + version + "; required " + expected[tool])
    actual_packages = {name: run("dpkg-query", "-W", "-f=${Version}", name)
                       for name in lock["build_packages"]}
    if actual_packages != lock["build_packages"]:
        raise ValueError("build package versions differ from image/inputs.json")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[ab]|rc|\.dev)?[0-9]*", args.version):
        raise ValueError("version must be a numeric PEP 440 release or prerelease")
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("output already exists; choose a new candidate directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    private_key = Path(args.private_key).resolve()
    public_key = Path(args.public_key).resolve()
    with tempfile.TemporaryDirectory(prefix="candidate-", dir=output.parent) as temporary:
        stage = Path(temporary)
        context = stage / "context"
        context.mkdir()
        fetch_binary(lock["codex"], context / "codex")
        fetch_binary(lock["ripgrep"], context / "rg")
        shutil.copyfile(REPO / "image/probe.sh", context / "probe.sh")
        definition = (REPO / "image/codex.def").read_text().replace(
            "{{BASE}}", lock["base"]["name"] + "@sha256:" + lock["base"]["sha256"])
        (context / "codex.def").write_text(definition)
        result = stage / "result"
        result.mkdir()
        image_name = "codex-hpc-linux-amd64.sif"
        image = result / image_name
        build_env = os.environ.copy()
        # Only the explicit definition and files enter the image. No host environment
        # or credential directory is copied into the build context.
        run("apptainer", *lock["build_arguments"], str(image), "codex.def",
            cwd=context, env=build_env)
        run("apptainer", "sign", "--key", str(private_key), str(image))
        run("apptainer", "verify", "--key", str(public_key), str(image))
        # Hash only after the signature has been appended to the SIF.
        image_digest = digest(image)
        with tempfile.TemporaryDirectory(prefix="qualification-") as home:
            command = ["apptainer", "exec", "--cleanenv", "--containall", "--no-mount",
                       "hostfs,bind-paths", "--home", home + ":/root", str(image)]
            probe = run(*command, "/usr/local/bin/codex-hpc-probe")
            if "codex-cli " + lock["codex"]["version"] not in probe.splitlines():
                raise ValueError("image Codex version differs from pinned input")
            rg_version = "ripgrep " + lock["ripgrep"]["version"]
            if not any(line == rg_version or line.startswith(rg_version + " ")
                       for line in probe.splitlines()):
                raise ValueError("image ripgrep version differs from pinned input")
            packages = run(*command, "dpkg-query", "-W", "-f=${Package}=${Version}\n")
        release = {
            "schema": 1, "candidate": True, "version": args.version,
            "protocol_version": 1, "image_interface_version": 1,
            "image": {"url": "https://github.com/micknudsen/codex-hpc/releases/download/v" +
                      args.version + "/" + image_name, "sha256": image_digest,
                      "size": image.stat().st_size, "architecture": lock["architecture"],
                      "codex_version": lock["codex"]["version"],
                      "base_digest": "sha256:" + lock["base"]["sha256"],
                      "input_fingerprint": provenance["fingerprint"]},
            "trust": {"format": "PEM", "public_key": "trusted.pem",
                      "sha256": digest(public_key)},
            "executables": {"codex": "/usr/local/bin/codex", "rg": "/usr/local/bin/rg",
                            "git": "/usr/bin/git", "shell": "/bin/bash",
                            "python": "/usr/bin/python3"},
            "tested_capabilities": ["offline-pem-verification", "cleanenv", "containall",
                                    "no-mount-hostfs-bind-paths", "private-home",
                                    "codex-version", "runtime-executables"],
            "tested_apptainer": actual["apptainer"],
        }
        package_client(stage, result, release, public_key)
        provenance["signed_image_sha256"] = image_digest
        provenance["build_tools_observed"] = actual
        provenance["build_packages_observed"] = actual_packages
        provenance["base_package_versions"] = packages.splitlines()
        provenance["qualification_output"] = probe.splitlines()
        write_json(result / "provenance.json", provenance)
        (result / "codex.def").write_text(definition)
        export_source(result)
        shutil.copyfile(REPO / "docs/build-image.md", result / "BUILD.md")
        result.rename(output)
    print(str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fingerprint", help="Describe every resolved image build input.")
    candidate = commands.add_parser("build", help="Build/sign/verify/qualify and package a candidate.")
    for option in ("version", "output", "private-key", "public-key"):
        candidate.add_argument("--" + option, required=True)
    args = parser.parse_args()
    try:
        if args.command == "fingerprint":
            print(json.dumps(inputs(), sort_keys=True))
        else:
            build(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "candidate: " + str(error) + "\n")


if __name__ == "__main__":
    main()
