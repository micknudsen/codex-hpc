#!/bin/sh
# Disposable Ubuntu 24.04 CI runner only. No cluster installation.
set -eu
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request
inputs = json.loads(Path("image/inputs.json").read_text())
tools = inputs["build_tools"]
with urllib.request.urlopen(tools["apptainer_deb_url"]) as response:
    data = response.read()
if hashlib.sha256(data).hexdigest() != tools["apptainer_deb_sha256"]:
    raise SystemExit("Apptainer package checksum mismatch")
Path("/tmp/codex-hpc-apptainer.deb").write_bytes(data)
subprocess.run(["sudo", "apt-get", "update"], check=True)
subprocess.run(["sudo", "apt-get", "install", "-y", "/tmp/codex-hpc-apptainer.deb",
                "squashfs-tools"] + [name + "=" + version
                for name, version in inputs["build_packages"].items()], check=True)
PY
sudo apptainer config fakeroot --add "$(id -un)"
apptainer --version
mksquashfs -version
