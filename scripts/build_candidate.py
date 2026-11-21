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


def fingerprint(resolved):
    return hashlib.sha256(json.dumps(resolved, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def inputs():
    lock = json.loads((REPO / "image/inputs.json").read_text())
    paths = sorted((REPO / "image").rglob("*")) + [Path(__file__).resolve(),
                                                     REPO / "scripts/install_apptainer_ci.sh"]
    files = {str(path.relative_to(REPO)): digest(path) for path in paths if path.is_file()}
    resolved = {"schema": 1, "inputs": lock, "files": files}
    return dict(resolved, fingerprint=fingerprint(resolved))


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
    for directory in ("image", "src", "tests", "scripts", "conda-recipe"):
        shutil.copytree(REPO / directory, source / directory, ignore=shutil.ignore_patterns(
            "*.egg-info", "__pycache__", "release"))
    for relative in ("docs/build-image.md", "README.md", "pyproject.toml", "setup.cfg", "LICENSE"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)


def candidate_output(args):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[ab]|rc|\.dev)?[0-9]*", args.version):
        raise ValueError("version must be a numeric PEP 440 release or prerelease")
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("output already exists; choose a new candidate directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def selection(result, decision, reason, release):
    write_json(result / "selection.json", {
        "schema": 1, "decision": decision, "reason": reason,
        "actions": {"build": decision == "new", "sign": decision == "new",
                    "upload_image": decision == "new"},
        "published": False,
        "image": release["image"], "source_release": release["image_source_release"],
    })


def qualify_client(candidate, image):
    result = subprocess.run([sys.executable, str(REPO / "tests/check_compatibility.py"),
                             str(candidate), str(image)], text=True, capture_output=True)
    if result.returncode:
        raise ValueError("candidate client/helper compatibility failed:\n" + result.stderr)
    expected = json.loads((candidate / "release/release.json").read_text())["image"]
    actual_digest = digest(image)
    if actual_digest != expected["sha256"] or image.stat().st_size != expected["size"]:
        raise ValueError("image changed during candidate compatibility qualification")
    write_json(candidate / "compatibility.json", {
        "schema": 1, "signed_image_sha256": actual_digest,
        "boundary": "installed setup and connect with controlled SSH/Slurm and actual Apptainer",
        "checks": ["cold-original-url", "warm-no-download", "signature-verification",
                   "helper-launch", "generated-codex-command", "codex-version",
                   "workspace-write", "owned-job-cleanup"],
    })


def prior_inputs(previous, current):
    try:
        prior = json.loads((previous / "provenance.json").read_text())
        required = {"schema", "inputs", "files", "fingerprint", "signed_image_sha256",
                    "build_tools_observed", "build_packages_observed", "base_package_versions",
                    "qualification_output"}
        resolved = {key: prior[key] for key in ("schema", "inputs", "files")}
        if (not required.issubset(prior) or prior["schema"] != 1 or not prior["files"]
                or fingerprint(resolved) != prior["fingerprint"]):
            return None, "incomplete provenance"
        expected_tools = {name: prior["inputs"]["build_tools"][name]
                          for name in ("apptainer", "python", "squashfs_tools")}
        if (prior["build_tools_observed"] != expected_tools
                or prior["build_packages_observed"] != prior["inputs"]["build_packages"]
                or not prior["base_package_versions"] or not prior["qualification_output"]):
            return None, "incomplete provenance"
    except (OSError, ValueError, KeyError, TypeError):
        return None, "incomplete provenance"
    if any(prior[key] != current[key] for key in ("schema", "inputs", "files", "fingerprint")):
        return None, "changed image inputs"
    return prior, "complete matching inputs"


def reuse(args, prior, reason):
    if not args.trusted_release:
        raise ValueError("reuse requires independently trusted release metadata (--trusted-release)")
    release = json.loads(Path(args.trusted_release).read_text())
    key = Path(args.public_key).resolve()
    identity = release["image"]
    source = re.fullmatch(r"https://github\.com/micknudsen/codex-hpc/releases/download/"
                         r"(v[0-9][A-Za-z0-9.\-]*)/codex-hpc-linux-amd64\.sif", identity["url"])
    if (not source or release["schema"] != 1 or identity["architecture"] != "linux/amd64"
            or identity["input_fingerprint"] != prior["fingerprint"]
            or identity["sha256"] != prior["signed_image_sha256"]
            or release["trust"] != {"format": "PEM", "public_key": "trusted.pem", "sha256": digest(key)}):
        raise ValueError("prior image identity or trust does not match trusted release metadata")
    image = Path(args.previous).resolve() / "codex-hpc-linux-amd64.sif"
    if image.stat().st_size != identity["size"] or digest(image) != identity["sha256"]:
        raise ValueError("prior image checksum or size differs from trusted release metadata")
    run("apptainer", "verify", "--key", str(key), str(image))
    output = candidate_output(args)
    with tempfile.TemporaryDirectory(prefix="candidate-", dir=output.parent) as temporary:
        stage = Path(temporary)
        result = stage / "result"
        result.mkdir()
        release.update(candidate=True, version=args.version, image_source_release=source[1])
        package_client(stage, result, release, key)
        write_json(result / "provenance.json", prior)
        export_source(result)
        shutil.copyfile(REPO / "docs/build-image.md", result / "BUILD.md")
        qualify_client(result, image)
        selection(result, "reuse", reason, release)
        result.rename(output)
    print(str(output))


def prepare(args):
    prior, reason = (prior_inputs(Path(args.previous), inputs()) if args.previous
                     else (None, "first image"))
    if prior is not None:
        print("Preparing reused image: " + reason, flush=True)
        reuse(args, prior, reason)
    else:
        print("Preparing new image: " + reason, flush=True)
        if not args.private_key:
            raise ValueError("a private key is required to build a new image")
        args.selection_reason = reason
        args.qualify_client = True
        build(args)


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
    output = candidate_output(args)
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
            "image_source_release": "v" + args.version,
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
        if getattr(args, "qualify_client", False):
            qualify_client(result, image)
        selection(result, "new", getattr(args, "selection_reason", "explicit build"), release)
        result.rename(output)
    print(str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fingerprint", help="Describe every resolved image build input.")
    candidate = commands.add_parser("build", help="Build/sign/verify/qualify and package a candidate.")
    for option in ("version", "output", "private-key", "public-key"):
        candidate.add_argument("--" + option, required=True)
    preparation = commands.add_parser("prepare", help="Select and qualify a new or reused image.")
    for option in ("version", "output", "public-key"):
        preparation.add_argument("--" + option, required=True)
    preparation.add_argument("--private-key", help="Required only when a new image is selected.")
    preparation.add_argument("--previous", help="Earlier image/provenance directory; never modified.")
    preparation.add_argument("--trusted-release", help="Independently trusted earlier release.json.")
    args = parser.parse_args()
    try:
        if args.command == "fingerprint":
            print(json.dumps(inputs(), sort_keys=True))
        elif args.command == "prepare":
            prepare(args)
        else:
            build(args)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, "candidate: " + str(error) + "\n")


if __name__ == "__main__":
    main()
