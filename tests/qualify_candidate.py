"""Exercise a real signed candidate and its clean-installed wheel in Linux CI."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


candidate = Path(sys.argv[1]).resolve()
provenance = json.loads((candidate / "provenance.json").read_text())
exported = json.loads(run(sys.executable, str(candidate / "source/scripts/build_candidate.py"),
                          "fingerprint"))
assert exported["fingerprint"] == provenance["fingerprint"]
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    venv.create(root / "venv", with_pip=True)
    python = str(root / "venv/bin/python")
    run(python, "-m", "pip", "install", "--no-index", "--no-deps",
        str(next(candidate.glob("*.whl"))))
    bundle = Path(run(python, "-c", "from importlib.resources import files; "
                      "print(files('codex_hpc').joinpath('release'))", cwd=root))
    metadata = json.loads((bundle / "release.json").read_text())
    observed = json.loads(run(python, str(bundle / "helper.py"), "--describe", cwd=root))
    assert observed["version"] == metadata["version"]
    assert observed["protocol_version"] == 1
    assert observed["image_interface_version"] == 1
    assert metadata["image"]["url"] == (
        "https://github.com/micknudsen/codex-hpc/releases/download/v" + metadata["version"] +
        "/codex-hpc-linux-amd64.sif")
    image = candidate / "codex-hpc-linux-amd64.sif"
    assert hashlib.sha256(image.read_bytes()).hexdigest() == metadata["image"]["sha256"]
    assert image.stat().st_size == metadata["image"]["size"]
    assert hashlib.sha256((bundle / "helper.py").read_bytes()).hexdigest() == metadata["helper_sha256"]
    assert hashlib.sha256((bundle / "trusted.pem").read_bytes()).hexdigest() == metadata["trust"]["sha256"]
    run("apptainer", "verify", "--key", str(bundle / "trusted.pem"), str(image))
    run("openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
        "-out", str(root / "wrong.pem"))
    run("openssl", "pkey", "-in", str(root / "wrong.pem"), "-pubout",
        "-out", str(root / "wrong-public.pem"))
    wrong = subprocess.run(["apptainer", "verify", "--key", str(root / "wrong-public.pem"),
                            str(image)], capture_output=True)
    assert wrong.returncode != 0, "wrong publisher was accepted"
    damaged = root / "damaged.sif"
    damaged.write_bytes(image.read_bytes())
    with damaged.open("r+b") as stream:
        stream.seek(-1, 2)
        original = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([original[0] ^ 1]))
    altered = subprocess.run(["apptainer", "verify", "--key", str(bundle / "trusted.pem"),
                              str(damaged)], capture_output=True)
    assert altered.returncode != 0, "altered image was accepted"
    home = root / "home"
    home.mkdir()
    version = run("apptainer", "exec", "--cleanenv", "--containall", "--no-mount",
                  "hostfs,bind-paths", "--home", str(home) + ":/root", str(image),
                  metadata["executables"]["codex"], "--version")
    assert version == "codex-cli " + metadata["image"]["codex_version"]
    assert metadata["candidate"] is True
    print("Qualified signed SIF, publisher rejection, tamper rejection and installed candidate/helper.")
