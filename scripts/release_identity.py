"""Trusted release events and immutable qualification artifact selection."""
import argparse
import configparser
import json
from pathlib import Path
import re
import subprocess

REPOSITORY = "micknudsen/codex-hpc"
CHANNEL = "micknudsen"
ARTIFACT = "codex-hpc-conda-release"
WORKFLOW = ".github/workflows/release-qualification.yml"
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:(?:a|b|rc)[0-9]+|\.dev[0-9]+)?"


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def api(endpoint):
    return json.loads(run("gh", "api", endpoint))


def version_at(source=None):
    def read(path):
        return run("git", "show", source + ":" + path) if source else Path(path).read_text()
    config = configparser.ConfigParser()
    config.read_string(read("setup.cfg"))
    version = config["metadata"]["version"]
    if not re.fullmatch(VERSION, version):
        raise ValueError("Unsupported package version")
    if '{% set version = "' + version + '" %}' not in read("conda-recipe/meta.yaml").splitlines():
        raise ValueError("setup.cfg and conda-recipe/meta.yaml versions differ")
    return version


def check_source(source, tag):
    if not re.fullmatch(r"[0-9a-f]{40}", source or "") or not re.fullmatch("v" + VERSION, tag or ""):
        raise ValueError("Expected full source SHA and matching version tag")
    subprocess.run(["git", "fetch", "origin", "master"], check=True)
    subprocess.run(["git", "merge-base", "--is-ancestor", source, "origin/master"], check=True)
    if tag != "v" + version_at(source):
        raise ValueError("Tag differs from source package version")


def select_run(record, source, tag, publication=False):
    allowed_event = record.get("event") == "push" or (
        not publication and record.get("event") == "workflow_dispatch")
    allowed_branch = record.get("head_branch") == tag or (
        not publication and record.get("event") == "workflow_dispatch" and record.get("head_branch") == "master")
    if not (record.get("status") == "completed" and record.get("conclusion") == "success"
            and allowed_event and allowed_branch and record.get("head_sha") == source
            and record.get("repository", {}).get("full_name") == REPOSITORY
            and record.get("head_repository", {}).get("full_name") == REPOSITORY
            and record.get("path") == WORKFLOW and type(record.get("id")) is int and record["id"] > 0):
        raise ValueError("Not successful trusted qualification for this exact tag/source")
    return record


def select_artifact(records):
    matches = [item for item in records if item.get("name") == ARTIFACT]
    if (len(matches) != 1 or matches[0].get("expired") is not False
            or matches[0].get("size_in_bytes", 0) <= 0 or type(matches[0].get("id")) is not int):
        raise ValueError("Qualified Conda artifact is missing, ambiguous, or expired")
    return matches[0]


def qualify_event(event, source, requested_source, tag):
    if event.get("repository", {}).get("full_name") != REPOSITORY or source != requested_source:
        raise ValueError("Repository or selected source differs from request")
    if "inputs" in event:
        if event.get("ref") not in {"refs/heads/master", "refs/tags/" + tag}:
            raise ValueError("Manual qualification requires master or matching release tag")
    elif event.get("ref") != "refs/tags/" + tag or event.get("deleted", False):
        raise ValueError("Qualification requires a tag push or manual dispatch")


def publication_event(event):
    if event.get("repository", {}).get("full_name") != REPOSITORY:
        raise ValueError("Unexpected event repository")
    record = event["workflow_run"]
    source, tag = record["head_sha"], record["head_branch"]
    select_run(record, source, tag, publication=True)
    if run("git", "rev-parse", "refs/tags/" + tag + "^{commit}") != source:
        raise ValueError("Tag no longer identifies the qualified source")
    return source, tag, record["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("version", "qualify", "publish"))
    parser.add_argument("--event", type=Path)
    parser.add_argument("--source")
    parser.add_argument("--requested-source")
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "version":
            print(version_at())
            return
        event = json.loads(args.event.read_text())
        if args.mode == "qualify":
            qualify_event(event, args.source, args.requested_source, args.tag)
            check_source(args.source, args.tag)
        else:
            source, tag, run_id = publication_event(event)
            check_source(source, tag)
            # Read the actual run rather than trusting only the event payload.
            actual = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
            select_run(actual, source, tag, publication=True)
            if actual["id"] != run_id:
                raise ValueError("Qualification run ID changed")
            artifact = select_artifact(api(
                f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"])
            with args.output.open("a") as stream:
                stream.write(f"source={source}\ntag={tag}\nrun_id={run_id}\nartifact_id={artifact['id']}\n")
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "Release identity rejected: " + str(error) + "\n")


if __name__ == "__main__":
    main()
