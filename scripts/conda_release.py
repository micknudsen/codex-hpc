"""Build and qualify one Conda artifact, then verify or export its exact bytes."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from build_candidate import digest, write_json
from release_identity import CHANNEL, REPOSITORY, version_at

REPO = Path(__file__).resolve().parents[1]
SUITES = ("setup", "connect", "allocation", "compute", "credentials", "signin", "lifetime")


def command(*args, **kwargs):
    return subprocess.run([str(value) for value in args], check=True, text=True, **kwargs)


def candidate_identity(candidate):
    release = json.loads((candidate / "release/release.json").read_text())
    selection = json.loads((candidate / "selection.json").read_text())
    provenance = json.loads((candidate / "provenance.json").read_text())
    image = release["image"]
    if (release["schema"] != 1 or image["architecture"] != "linux/amd64"
            or not image["url"].startswith(f"https://github.com/{REPOSITORY}/releases/download/v")
            or not image["url"].endswith("/codex-hpc-linux-amd64.sif")
            or selection["image"] != image or selection["published"] is not False
            or selection["decision"] not in {"new", "reuse"}
            or selection["actions"] != dict.fromkeys(("build", "sign", "upload_image"), selection["decision"] == "new")
            or provenance["signed_image_sha256"] != image["sha256"]
            or provenance["fingerprint"] != image["input_fingerprint"]):
        raise ValueError("Candidate image, selection and provenance identities differ")
    if digest(candidate / "release/helper.py") != release["helper_sha256"]:
        raise ValueError("Candidate helper differs from release metadata")
    if digest(candidate / "release/trusted.pem") != release["trust"]["sha256"]:
        raise ValueError("Candidate public key differs from release metadata")
    wheels = list(candidate.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != "codex_hpc-" + release["version"] + "-py3-none-any.whl":
        raise ValueError("Expected exactly the versioned candidate wheel")
    with zipfile.ZipFile(wheels[0]) as wheel:
        for name in ("release.json", "helper.py", "trusted.pem"):
            if wheel.read("codex_hpc/release/" + name) != (candidate / "release" / name).read_bytes():
                raise ValueError("Wheel release bundle differs from candidate: " + name)
    sif = candidate / "codex-hpc-linux-amd64.sif"
    if selection["decision"] == "new":
        if sif.stat().st_size != image["size"] or digest(sif) != image["sha256"]:
            raise ValueError("New image bytes differ from candidate metadata")
    elif sif.exists():
        raise ValueError("Reused images must not be staged for duplicate publication")
    return release, selection


def files(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Release bundles cannot contain symlinks")
        if path.is_file() and path.relative_to(root).as_posix() not in {"release.json", "SHA256SUMS"}:
            result[path.relative_to(root).as_posix()] = {"size": path.stat().st_size, "sha256": digest(path)}
    return result


def checksums(root):
    return "".join(f"{digest(path)}  {path.relative_to(root).as_posix()}\n"
                   for path in sorted(root.rglob("*"))
                   if path.is_file() and path != root / "SHA256SUMS")


def installed_identity(prefix, candidate, package):
    expected, _ = candidate_identity(candidate)
    records = list((prefix / "conda-meta").glob("codex-hpc-*.json"))
    if len(records) != 1:
        raise ValueError("Expected exactly one installed Conda package record")
    record = json.loads(records[0].read_text())
    if (record["name"] != "codex-hpc" or record["version"] != expected["version"]
            or record["subdir"] != "noarch" or record["sha256"] != digest(package)
            or record["fn"] != package.name):
        raise ValueError("Installed Conda artifact differs from selected candidate")
    with tempfile.TemporaryDirectory(prefix="codex-hpc-installed-check-") as temporary:
        with zipfile.ZipFile(next(candidate.glob("*.whl"))) as wheel:
            payload = {name: hashlib.sha256(wheel.read(name)).hexdigest()
                       for name in wheel.namelist() if name.startswith("codex_hpc/") and not name.endswith("/")}
        script = ("from importlib.resources import files; from importlib.metadata import version; "
                  "import hashlib,json; b=files('codex_hpc').joinpath('release'); "
                  "print(json.dumps({'version':version('codex-hpc'), 'release':json.loads(b.joinpath('release.json').read_text()), "
                  "'helper':hashlib.sha256(b.joinpath('helper.py').read_bytes()).hexdigest(), "
                  "'key':hashlib.sha256(b.joinpath('trusted.pem').read_bytes()).hexdigest(), "
                  "'payload':{name:hashlib.sha256(files('codex_hpc').joinpath(name.removeprefix('codex_hpc/')).read_bytes()).hexdigest() "
                  "for name in " + repr(list(payload)) + "}}))")
        output = command(prefix / "bin/python", "-c", script, cwd=temporary,
                         capture_output=True).stdout
        observed = json.loads(output)
        if observed != {"version": expected["version"], "release": expected,
                        "helper": expected["helper_sha256"], "key": expected["trust"]["sha256"], "payload": payload}:
            raise ValueError("Installed client/helper/trust metadata differs from candidate")
        command(prefix / "bin/codex-hpc", "--help", cwd=temporary, stdout=subprocess.DEVNULL)
    return {key: record[key] for key in ("name", "version", "build", "subdir", "fn", "sha256")}


def build(args):
    candidate = args.candidate.resolve()
    release, _ = candidate_identity(candidate)
    if release["version"] != version_at():
        raise ValueError("Candidate and checkout package versions differ")
    if args.output.exists() or args.prefix.exists():
        raise ValueError("Output and installation prefix must be fresh")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="conda-build-", dir=args.output.parent) as temporary:
        stage = Path(temporary)
        recipe = stage / "recipe"
        shutil.copytree(REPO / "conda-recipe", recipe)
        shutil.copyfile(REPO / "LICENSE", recipe / "LICENSE")
        wheel = next(candidate.glob("*.whl"))
        shutil.copyfile(wheel, recipe / wheel.name)
        channel = stage / "channel"
        command(args.conda, "build", "--override-channels", "-c", "conda-forge",
                "--package-format", "2", "--output-folder", channel, recipe)
        packages = list((channel / "noarch").glob("codex-hpc-*.conda"))
        if len(packages) != 1:
            raise ValueError("Build must produce exactly one noarch Conda package")
        command(args.conda, "index", channel)
        command(args.conda, "create", "-y", "-p", args.prefix, "--override-channels",
                "-c", channel.as_uri(), "-c", "conda-forge", "python=3.12",
                "codex-hpc=" + release["version"])
        identity = installed_identity(args.prefix, candidate, packages[0])
        result = stage / "bundle"
        result.mkdir()
        shutil.copytree(candidate, result / "candidate")
        shutil.copyfile(packages[0], result / packages[0].name)
        write_json(result / "build.json", {"source": args.source, "tag": "v" + release["version"],
                                           "package": identity, "channel": CHANNEL})
        result.rename(args.output)


def qualify(args):
    bundle = args.bundle.resolve()
    record = json.loads((bundle / "build.json").read_text())
    candidate = bundle / "candidate"
    package = bundle / record["package"]["fn"]
    identity = installed_identity(args.prefix, candidate, package)
    if record["source"] != args.source or identity != record["package"]:
        raise ValueError("Qualification source or installed package changed")
    env = os.environ.copy()
    env.update(CODEX_HPC_TEST_CANDIDATE=str(candidate),
               CODEX_HPC_TEST_PREFIX=str(args.prefix.resolve()),
               CODEX_HPC_TEST_IMAGE=str(args.image.resolve()),
               CODEX_HPC_TEST_CONDA=args.conda)
    records = bundle / "conda-tests"
    records.mkdir(exist_ok=True)
    for suite in SUITES:
        command(args.prefix / "bin/python", REPO / "scripts/run_conda_suite.py", suite,
                records / (suite + ".json"), cwd=REPO, env=env)
    command(args.prefix / "bin/python", REPO / "tests/check_compatibility.py",
            candidate, args.image.resolve(), cwd=REPO, env=env)
    # Test fixtures mutate only clones; recheck the qualified installation and files.
    installed_identity(args.prefix, candidate, package)
    runtime = json.loads(command(args.conda, "list", "-p", args.prefix, "--json", capture_output=True).stdout)
    write_json(bundle / "qualification.json", {
        "source": args.source, "package": identity, "status": "passed", "suites": list(SUITES),
        "boundary": "Conda-installed CLI, controlled SSH/Slurm/login, actual signed SIF",
        "cluster_qualified": False,
        "runtime": [{key: item[key] for key in ("name", "version", "build_string")} for item in runtime]})
    write_json(bundle / "release.json", {"schema": 1, "repository": REPOSITORY, "channel": CHANNEL,
                                         "tag": record["tag"], "source": args.source,
                                         "package": identity, "files": files(bundle)})
    (bundle / "SHA256SUMS").write_text(checksums(bundle))
    verify(bundle, record["tag"], args.source)


def verify(bundle, tag, source):
    record = json.loads((bundle / "release.json").read_text())
    if (record["schema"] != 1 or record["repository"] != REPOSITORY or record["channel"] != CHANNEL
            or record["tag"] != tag or record["source"] != source or record["files"] != files(bundle)
            or (bundle / "SHA256SUMS").read_text() != checksums(bundle)):
        raise ValueError("Release bundle source, channel or bytes differ")
    release, _ = candidate_identity(bundle / "candidate")
    package = record["package"]
    filename = "codex-hpc-" + release["version"] + "-" + package["build"] + ".conda"
    if (package["name"] != "codex-hpc" or package["subdir"] != "noarch"
            or package["version"] != release["version"] or tag != "v" + release["version"]
            or package["fn"] != filename or digest(bundle / filename) != package["sha256"]):
        raise ValueError("Conda package identity differs from candidate")
    qualification = json.loads((bundle / "qualification.json").read_text())
    if (qualification["source"] != source or qualification["package"] != package
            or qualification["status"] != "passed" or qualification["suites"] != list(SUITES)):
        raise ValueError("Complete Conda qualification is missing or mismatched")
    for suite in SUITES:
        result = json.loads((bundle / "conda-tests" / (suite + ".json")).read_text())
        if (result["suite"] != suite or result["tests"] <= 0
                or any(result[key] != 0 for key in ("failures", "errors", "skips"))):
            raise ValueError("Incomplete installed-package test suite: " + suite)
    return record


def assets(bundle, output, tag, source, run_id):
    record = verify(bundle, tag, source)
    if output.exists():
        raise ValueError("Assets output must be fresh")
    output.mkdir()
    shutil.copyfile(bundle / record["package"]["fn"], output / record["package"]["fn"])
    candidate = bundle / "candidate"
    for source_name, target_name in (("release/release.json", "image-release.json"),
                                     ("release/trusted.pem", "trusted.pem"),
                                     ("provenance.json", "image-provenance.json")):
        shutil.copyfile(candidate / source_name, output / target_name)
    sif = candidate / "codex-hpc-linux-amd64.sif"
    if sif.exists():
        shutil.copyfile(sif, output / sif.name)
    # Deterministic validation assets make retries compare bytes instead of replacing them.
    publication = json.dumps({"repository": REPOSITORY, "channel": CHANNEL, "tag": tag,
                              "source": source, "source_ci_run": run_id,
                              "package": record["package"]}, sort_keys=True, indent=2).encode() + b"\n"
    archive = output / ("codex-hpc-" + tag[1:] + "-validation.tar.gz")
    with archive.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for path in sorted(bundle.rglob("*")):
                if not path.is_file() or path.name in {record["package"]["fn"], "codex-hpc-linux-amd64.sif"}:
                    continue
                data = path.read_bytes()
                info = tarfile.TarInfo(path.relative_to(bundle).as_posix())
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo("publication.json")
            info.size, info.mode = len(publication), 0o644
            tar.addfile(info, io.BytesIO(publication))
    (output / "SHA256SUMS").write_text(checksums(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("build", "qualify", "verify", "assets"):
        command_parser = sub.add_parser(mode)
        command_parser.add_argument("--source", required=True)
        if mode == "build":
            command_parser.add_argument("--candidate", type=Path, required=True)
        else:
            command_parser.add_argument("--bundle", type=Path, required=True)
        if mode in {"build", "assets"}:
            command_parser.add_argument("--output", type=Path, required=True)
        if mode in {"build", "qualify"}:
            command_parser.add_argument("--prefix", type=Path, required=True)
            command_parser.add_argument("--conda", default="conda")
        if mode == "qualify":
            command_parser.add_argument("--image", type=Path, required=True)
        if mode in {"verify", "assets"}:
            command_parser.add_argument("--tag", required=True)
        if mode == "assets":
            command_parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "build":
            build(args)
        elif args.mode == "qualify":
            qualify(args)
        elif args.mode == "assets":
            assets(args.bundle, args.output, args.tag, args.source, args.run_id)
        else:
            verify(args.bundle, args.tag, args.source)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "Conda release rejected: " + str(error) + "\n")


if __name__ == "__main__":
    main()
