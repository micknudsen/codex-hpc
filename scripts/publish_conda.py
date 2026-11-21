"""Publish the exact qualified Conda package and stage matching GitHub draft assets."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from build_candidate import digest, write_json
from conda_release import assets, installed_identity, verify
from release_identity import CHANNEL, REPOSITORY, api, select_artifact, select_run
from release_image import download


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def verify_origin(source, tag, run_id, artifact_id):
    record = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    select_run(record, source, tag, publication=True)
    if record["id"] != run_id:
        raise ValueError("Source qualification run ID differs")
    artifact = select_artifact(api(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"])
    if artifact["id"] != artifact_id:
        raise ValueError("Selected immutable artifact ID differs")
    comparison = api(f"repos/{REPOSITORY}/compare/{source}...master")
    if (comparison["status"] not in {"ahead", "identical"}
            or comparison["base_commit"]["sha"] != source
            or comparison["merge_base_commit"]["sha"] != source):
        raise ValueError("Qualified source is not on master")
    reference = api(f"repos/{REPOSITORY}/git/ref/tags/{tag}")["object"]
    for _ in range(4):
        if reference["type"] == "commit":
            break
        if reference["type"] != "tag":
            raise ValueError("Unexpected release tag object")
        reference = api(f"repos/{REPOSITORY}/git/tags/{reference['sha']}")["object"]
    if reference.get("sha") != source or reference.get("type") != "commit":
        raise ValueError("Tag does not identify the qualified commit")


def channel_package(package):
    identity = CHANNEL + "/codex-hpc/" + quote(package["version"], safe="") + "/noarch/" + quote(package["fn"], safe="")
    try:
        with urlopen("https://api.anaconda.org/dist/" + identity, timeout=60) as response:
            remote = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return False
        raise ValueError("Cannot inspect channel distribution: " + str(error)) from error
    if (remote["basename"] != "noarch/" + package["fn"] or remote["version"] != package["version"]
            or remote["attrs"]["subdir"] != "noarch" or "main" not in remote["labels"]
            or remote["sha256"] != package["sha256"]):
        raise ValueError("Existing micknudsen distribution differs; refusing replacement")
    with tempfile.TemporaryDirectory(prefix="codex-hpc-channel-check-") as temporary:
        download("https://api.anaconda.org/download/" + identity, Path(temporary) / package["fn"], package["sha256"])
    return True


def ensure_channel(bundle, package, conda):
    if channel_package(package):
        return
    if not os.environ.get("ANACONDA_API_TOKEN"):
        raise ValueError("ANACONDA_API_TOKEN is required to upload to micknudsen")
    result = subprocess.run([conda, "run", "-n", "base", "anaconda", "upload", "--user", CHANNEL,
                             "--register", "--label", "main", "--no-progress", "--fail",
                             str(bundle / package["fn"])])
    # An upload error may be a lost response after success. Recheck exact remote bytes.
    if not channel_package(package):
        raise ValueError("Package absent after upload (exit " + str(result.returncode) + ")")


def draft(tag):
    result = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"],
                            capture_output=True, text=True)
    if result.returncode:
        if "HTTP 404" not in result.stderr:
            raise ValueError("Cannot inspect GitHub release: " + result.stderr)
        # GitHub's tag endpoint omits unpublished drafts. The authenticated list
        # includes them; inspect every page before treating a tag as absent.
        listing = subprocess.run(["gh", "api", "--paginate", "--slurp",
                                  f"repos/{REPOSITORY}/releases?per_page=100"],
                                 capture_output=True, text=True)
        if listing.returncode:
            raise ValueError("Cannot inspect GitHub release drafts: " + listing.stderr)
        matches = [item for page in json.loads(listing.stdout) for item in page
                   if item.get("tag_name") == tag]
        if len(matches) > 1:
            raise ValueError("Multiple GitHub releases match the requested tag")
        if not matches:
            return None
        release = matches[0]
    else:
        release = json.loads(result.stdout)
    if not release["draft"] or release["tag_name"] != tag:
        raise ValueError("Only the matching unpublished draft may receive assets")
    if release["prerelease"] != (re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) is None):
        raise ValueError("Existing draft prerelease status differs")
    return release


def attach(tag, directory):
    release = draft(tag)
    if release is None:
        notes = directory.parent / "draft-notes.md"
        notes.write_text("Qualified Conda package published to micknudsen. Apply approved release notes and verify assets before GitHub publication.\n")
        command = ["release", "create", tag, "--repo", REPOSITORY, "--verify-tag", "--draft",
                   "--title", tag, "--notes-file", str(notes), "--latest=false"]
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
            command.append("--prerelease")
        gh(*command)
        release = draft(tag)
        if release is None:
            raise ValueError("Created draft is not visible yet; retry using the same retained artifact")
    expected = {path.name for path in directory.iterdir()}
    existing = {item["name"] for item in release["assets"]}
    if len(existing) != len(release["assets"]) or not existing.issubset(expected):
        raise ValueError("Draft contains unexpected assets; preserve and investigate them")
    with tempfile.TemporaryDirectory(prefix="codex-hpc-draft-check-") as temporary:
        for name in sorted(expected, key=lambda name: name == "SHA256SUMS"):
            if name not in existing:
                gh("release", "upload", tag, str(directory / name), "--repo", REPOSITORY)
            gh("release", "download", tag, "--repo", REPOSITORY, "--pattern", name, "--dir", temporary)
            if digest(Path(temporary) / name) != digest(directory / name):
                raise ValueError("Draft asset bytes differ; refusing replacement: " + name)


def publish(args):
    if args.output.exists():
        raise ValueError("Select a fresh publication evidence directory")
    record = verify(args.bundle, args.tag, args.source)
    verify_origin(args.source, args.tag, args.run_id, args.artifact_id)
    # Check conflicts before publishing a new channel distribution.
    draft(args.tag)
    candidate = args.bundle / "candidate"
    metadata = json.loads((candidate / "release/release.json").read_text())
    with tempfile.TemporaryDirectory(prefix="codex-hpc-publication-") as temporary:
        work = Path(temporary)
        image = candidate / "codex-hpc-linux-amd64.sif"
        if not image.exists():
            image = work / "selected.sif"
            download(metadata["image"]["url"], image, metadata["image"]["sha256"])
        subprocess.run(["apptainer", "verify", "--key", str(candidate / "release/trusted.pem"), str(image)], check=True)
        ensure_channel(args.bundle, record["package"], args.conda)
        prefix = work / "environment"
        package = record["package"]
        for attempt in range(10):
            result = subprocess.run([args.conda, "create", "-y", "-p", str(prefix), "--override-channels",
                                     "-c", CHANNEL, "-c", "conda-forge", "python=3.12",
                                     "codex-hpc=" + package["version"] + "=" + package["build"]])
            if result.returncode == 0:
                break
            if attempt == 9:
                raise ValueError("Published package not installable after ten attempts")
            shutil.rmtree(prefix, ignore_errors=True)
            time.sleep(args.retry_delay)
        installed_identity(prefix, candidate, args.bundle / package["fn"])
        env = os.environ.copy()
        env.update(CODEX_HPC_TEST_PREFIX=str(prefix), CODEX_HPC_TEST_CONDA=args.conda)
        subprocess.run([str(prefix / "bin/python"), "tests/check_compatibility.py", str(candidate), str(image)],
                       check=True, env=env)
        asset_dir = work / "assets"
        assets(args.bundle, asset_dir, args.tag, args.source, args.run_id)
        attach(args.tag, asset_dir)
        evidence = {"repository": REPOSITORY, "tag": args.tag, "source": args.source,
                    "source_ci_run": args.run_id, "artifact_id": args.artifact_id,
                    "publication_run": os.environ.get("GITHUB_RUN_ID"), "channel": CHANNEL,
                    "package": package, "fresh_channel_install": "passed",
                    "installed_setup_connect": "passed", "github_status": "draft"}
        args.output.mkdir(parents=True)
        shutil.copytree(asset_dir, args.output / "assets")
        write_json(args.output / "publication-attempt.json", evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("bundle", "output"):
        parser.add_argument("--" + option, type=Path, required=True)
    for option in ("tag", "source"):
        parser.add_argument("--" + option, required=True)
    for option in ("run-id", "artifact-id"):
        parser.add_argument("--" + option, type=int, required=True)
    parser.add_argument("--conda", default="conda")
    parser.add_argument("--retry-delay", type=float, default=30)
    args = parser.parse_args()
    try:
        publish(args)
    except (ValueError, KeyError, TypeError, OSError, URLError, subprocess.CalledProcessError) as error:
        parser.exit(1, "Conda publication failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
