"""A distributed client must carry the matching helper and image trust bundle."""
import hashlib
from importlib.metadata import version
from importlib.resources import files
import json

bundle = files("codex_hpc").joinpath("release")
release = json.loads(bundle.joinpath("release.json").read_text())
assert release["version"] == version("codex-hpc")
assert hashlib.sha256(bundle.joinpath("helper.py").read_bytes()).hexdigest() == release["helper_sha256"]
assert hashlib.sha256(bundle.joinpath("trusted.pem").read_bytes()).hexdigest() == release["trust"]["sha256"]
assert release["image"]["architecture"] == "linux/amd64"
assert release["image"]["url"].startswith("https://github.com/micknudsen/codex-hpc/releases/download/")
