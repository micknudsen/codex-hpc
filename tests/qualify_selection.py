"""Exercise real SIF preparation decisions with disposable CI trust material."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read(path):
    return json.loads(path.read_text())


candidate, private_key, public_key = [Path(value).resolve() for value in sys.argv[1:]]
repository = Path(__file__).resolve().parents[1]
image = candidate / "codex-hpc-linux-amd64.sif"
original_digest = sha(image)
trusted = candidate / "release/release.json"
identity = read(trusted)["image"]
assert read(candidate / "selection.json")["decision"] == "new"
assert read(candidate / "selection.json")["reason"] == "first image"
assert read(candidate / "compatibility.json")["signed_image_sha256"] == original_digest

with tempfile.TemporaryDirectory(prefix="image-selection-") as temporary:
    root = Path(temporary)
    source = root / "source"
    shutil.copytree(repository, source, ignore=shutil.ignore_patterns(
        ".git", ".local", "build", "*.egg-info", "__pycache__"))
    binary = root / "bin"
    binary.mkdir()
    actual_apptainer = shutil.which("apptainer")
    events = root / "actions.jsonl"
    wrapper = binary / "apptainer"
    wrapper.write_text("#!" + sys.executable + "\n" +
        "import json, os, sys\n" +
        "with open(" + repr(str(events)) + ", 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n" +
        "os.execv(" + repr(actual_apptainer) + ", [" + repr(actual_apptainer) + ", *sys.argv[1:]])\n")
    wrapper.chmod(0o755)
    env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"])

    def prepare(name, previous=candidate, metadata=trusted, key=public_key, new=False, succeeds=True):
        output = root / name
        command = [sys.executable, str(source / "scripts/build_candidate.py"), "prepare",
                   "--version", "0.2.0", "--output", str(output), "--previous", str(previous),
                   "--trusted-release", str(metadata), "--public-key", str(key)]
        if new: command += ["--private-key", str(private_key)]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        if succeeds:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0, "invalid candidate was prepared: " + name
            assert not output.exists(), "failed candidate was published: " + name
        assert sha(image) == original_digest, "original asset changed"
        return output

    def actions():
        return [json.loads(line)[0] for line in events.read_text().splitlines()]

    client = source / "src/codex_hpc/cli.py"
    original_client = client.read_text()
    client.write_text(original_client + "\n# Client-only candidate update\n")
    reused = prepare("reused")
    assert read(reused / "release/release.json")["image"] == identity
    assert read(reused / "release/release.json")["image_source_release"] == read(trusted)["image_source_release"]
    assert not (reused / image.name).exists()
    assert read(reused / "selection.json")["actions"] == {
        "build": False, "sign": False, "upload_image": False}
    assert "build" not in actions() and "sign" not in actions()
    assert read(reused / "compatibility.json")["signed_image_sha256"] == original_digest
    print("PASS equal inputs: original URL/digest, no build/sign/image upload, cold/warm setup and connect", flush=True)

    client.write_text('def main():\n    raise SystemExit("incompatible candidate client")\n')
    prepare("incompatible-client", succeeds=False)
    client.write_text(original_client)
    helper = source / "src/codex_hpc/remote_helper.py"
    original_helper = helper.read_text()
    helper.write_text('raise SystemExit("incompatible candidate helper")\n')
    prepare("incompatible-helper", succeeds=False)
    helper.write_text(original_helper.replace("--config=cli_auth_credentials_store",
                                               "--invalid-candidate-option"))
    prepare("incompatible-codex-arguments", succeeds=False)
    helper.write_text(original_helper)
    print("PASS incompatible client/helper: no prepared output", flush=True)

    wrong_key = root / "wrong.pem"
    wrong_public = root / "wrong-public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(wrong_key)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(wrong_key), "-pubout", "-out", str(wrong_public)],
                   check=True, capture_output=True)
    wrong_metadata = read(trusted)
    wrong_metadata["trust"]["sha256"] = sha(wrong_public)
    wrong_trusted = root / "wrong-trusted.json"
    wrong_trusted.write_text(json.dumps(wrong_metadata))
    prepare("wrong-signer", metadata=wrong_trusted, key=wrong_public, succeeds=False)
    altered = root / "altered"
    altered.mkdir()
    shutil.copyfile(candidate / "provenance.json", altered / "provenance.json")
    shutil.copyfile(image, altered / image.name)
    with (altered / image.name).open("r+b") as stream:
        stream.seek(-1, 2)
        byte = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([byte[0] ^ 1]))
    prepare("altered-bytes", previous=altered, succeeds=False)
    assert "build" not in actions() and "sign" not in actions()
    print("PASS wrong signer/altered bytes: fail closed without rebuilding", flush=True)

    probe = source / "image/probe.sh"
    original_probe = probe.read_text()
    probe.write_text(original_probe + "\n# Changed included image input\n")
    changed = prepare("changed", new=True)
    assert read(changed / "selection.json")["reason"] == "changed image inputs"
    assert read(changed / "selection.json")["actions"] == {"build": True, "sign": True, "upload_image": True}
    assert (changed / image.name).is_file()
    assert read(changed / "release/release.json")["image"]["url"].endswith("/v0.2.0/" + image.name)
    assert actions().count("build") == 1 and actions().count("sign") == 1
    probe.write_text(original_probe)
    incomplete = root / "incomplete"
    incomplete.mkdir()
    (incomplete / "provenance.json").write_text("{}")
    rebuilt = prepare("rebuilt", previous=incomplete, new=True)
    assert read(rebuilt / "selection.json")["reason"] == "incomplete provenance"
    assert (rebuilt / image.name).is_file()
    assert actions().count("build") == 2 and actions().count("sign") == 2
    print("PASS changed/incomplete provenance: actual new build/sign and installed compatibility", flush=True)

print("Qualified first build, equal-input reuse, rebuild decisions, trust failures and candidate compatibility.")
