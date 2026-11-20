#!/bin/sh
# Disposable Ubuntu 24.04 CI runner only. No cluster installation.
set -eu
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
import urllib.request
tools = json.loads(Path("image/inputs.json").read_text())["build_tools"]
with urllib.request.urlopen(tools["apptainer_deb_url"]) as response:
    data = response.read()
if hashlib.sha256(data).hexdigest() != tools["apptainer_deb_sha256"]:
    raise SystemExit("Apptainer package checksum mismatch")
Path("/tmp/codex-hpc-apptainer.deb").write_bytes(data)
PY
sudo apt-get update
sudo apt-get install -y /tmp/codex-hpc-apptainer.deb squashfs-tools uidmap fakeroot
sudo apptainer config fakeroot --add "$(id -un)"
apptainer --version
mksquashfs -version
