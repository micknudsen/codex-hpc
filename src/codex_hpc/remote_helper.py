"""Self-contained helper bundled beside its matching release metadata."""
import argparse
import csv
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import io
import os
import re
import platform
import pwd
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify(image, release, key):
    expected = release["image"]
    if image.stat().st_size != expected["size"] or digest(image) != expected["sha256"]:
        raise ValueError("image checksum or size mismatch; remove the damaged managed image and rerun setup")
    environment = os.environ.copy()
    environment.update(APPTAINER_CONFIGDIR=str(key.parent / "apptainer"),
                       APPTAINER_KEYSDIR=str(key.parent / "keys"))
    result = subprocess.run(["apptainer", "verify", "--key", str(key), str(image)],
                            capture_output=True, text=True, env=environment)
    if result.returncode:
        raise ValueError("image signature verification failed with this client's trusted public key: " +
                         result.stderr.strip())


def recover_staging(parent, prefix):
    # Caller holds the canonical root lock: marked transactions cannot still be active.
    for directory in parent.glob(prefix + "*"):
        marker = directory / ".codex-hpc-transaction"
        if (directory.is_dir() and not directory.is_symlink() and marker.is_file()
                and not marker.is_symlink() and marker.read_text() == "setup-v1"):
            shutil.rmtree(directory)


@contextmanager
def staging(parent, prefix):
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as temporary:
        directory = Path(temporary)
        (directory / ".codex-hpc-transaction").write_text("setup-v1")
        yield directory


def image_location(cache, release):
    metadata = release["image"]
    return cache / "images" / metadata["architecture"].replace("/", "-") / (metadata["sha256"] + ".sif")


def installation_identity(release, image):
    encoded = json.dumps(release, sort_keys=True)
    installation = json.dumps({"image": str(image)})
    identity = hashlib.sha256((encoded + installation).encode()).hexdigest()
    return encoded, installation, identity


def prepare_image(image, release, key):
    images = image.parent
    if image.exists():
        verify(image, release, key)
        return image
    if shutil.disk_usage(images).free < release["image"]["size"] + 1024 * 1024:
        raise ValueError("insufficient free space for image download at " + str(images))
    with staging(images, ".download-") as temporary:
        partial = temporary / "image.sif"
        result = subprocess.run(["curl", "--disable", "--fail", "--location", "--proto", "=https",
                                 "--proto-redir", "=https", "--silent", "--show-error",
                                 "--output", str(partial), release["image"]["url"]],
                                capture_output=True, text=True)
        if result.returncode:
            raise ValueError("image download failed (transfer or storage/quota error): " +
                             result.stderr.strip())
        verify(partial, release, key)
        partial.chmod(0o444)
        os.replace(partial, image)
    return image


def setup(payload):
    os.umask(0o077)
    roots = {name: Path(path).resolve() for name, path in payload["roots"].items()}
    release = payload["release"]
    image_metadata = release["image"]
    if (release["schema"] != 1 or release["protocol_version"] != 1
            or image_metadata["architecture"] != "linux/amd64"
            or not re.fullmatch(r"[0-9a-f]{64}", image_metadata["sha256"])
            or not isinstance(image_metadata["size"], int) or image_metadata["size"] <= 0
            or not image_metadata["url"].startswith(
                "https://github.com/micknudsen/codex-hpc/releases/download/")):
        raise ValueError("invalid or unsupported bundled release metadata")
    for name, root in roots.items():
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for tool in ("curl", "apptainer"):
        if shutil.which(tool) is None:
            raise ValueError("required login-host tool is missing: " + tool)
    with ExitStack() as locks:
        for root in sorted({roots["data"], roots["cache"]}):
            lock = locks.enter_context((root / ".setup.lock").open("a"))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        releases = roots["data"] / "releases"
        releases.mkdir(exist_ok=True)
        recover_staging(releases, ".setup-")
        image = image_location(roots["cache"], release)
        images = image.parent
        images.mkdir(parents=True, exist_ok=True)
        recover_staging(images, ".download-")
        for name, root in roots.items():
            try:
                with tempfile.TemporaryFile(dir=root):
                    pass
            except OSError as error:
                raise ValueError(name + " root is not writable: " + str(root) + ": " + str(error))
        if shutil.disk_usage(releases).free < 1024 * 1024:
            raise ValueError("insufficient free space for release staging at " + str(releases))
        encoded, installation, identity = installation_identity(release, image)
        with staging(releases, ".setup-") as stage:
            key = stage / "trusted.pem"
            key.write_text(payload["key"])
            helper = stage / "helper.py"
            helper.write_text(payload["helper"])
            if digest(key) != release["trust"]["sha256"] or digest(helper) != release["helper_sha256"]:
                raise ValueError("bundled helper or public key checksum mismatch")
            prepare_image(image, release, key)
            (stage / "release.json").write_text(encoded)
            (stage / "installation.json").write_text(installation)
            target = releases / identity
            if target.exists():
                expected_files = ("helper.py", "trusted.pem", "release.json", "installation.json")
                if target.is_symlink() or any(
                        not (target / name).is_file() or (target / name).is_symlink()
                        or (target / name).read_bytes() != (stage / name).read_bytes()
                        for name in expected_files):
                    raise ValueError("installed release is damaged at " + str(target) +
                                     "; preserve any running sessions before removing it and retrying setup")
            else:
                # Keep TemporaryDirectory's own directory alive until its exit.
                ready = stage / "ready"
                ready.mkdir()
                for child in (key, helper, stage / "release.json", stage / "installation.json"):
                    child.rename(ready / child.name)
                ready.rename(target)
            current = roots["data"] / "current"
            if current.exists() and not current.is_symlink():
                raise ValueError("refusing to replace non-managed current path: " + str(current))
            pointer = stage / "current"
            pointer.symlink_to(target.relative_to(roots["data"]))
            os.replace(pointer, current)
    print("Release " + release["version"] + " ready at " + str(target))


def contains(directory, path):
    return directory == path or directory in path.parents


def check_workspace_isolation(workspace, roots):
    temporary_root = Path(tempfile.gettempdir()).resolve()
    pending = [workspace]
    visited = set()
    while pending:
        path = pending.pop()
        try:
            resolved = path.resolve()
            for name, root in roots.items():
                if contains(resolved, root) or contains(root, resolved):
                    raise ValueError("unsafe workspace: " + str(path) + " exposes " +
                                     name + " root " + str(root))
            if contains(resolved, temporary_root):
                raise ValueError("unsafe workspace: " + str(path) + " contains temporary root " +
                                 str(temporary_root))
            if resolved in visited or not resolved.is_dir():
                continue
            visited.add(resolved)
            pending.extend(resolved.iterdir())
        except (OSError, RuntimeError) as error:
            raise ValueError("cannot check workspace isolation at " + str(path) + ": " + str(error))


def preflight(payload):
    os.umask(0o077)
    selected = payload["workspace"]
    if (not selected.startswith(("/", "~/")) or
            any(ord(char) < 32 or ord(char) == 127 for char in selected)):
        raise ValueError("workspace must be an absolute POSIX path or a quoted '~/project'; "
                         "control characters are unsupported")
    if selected.startswith("~/"):
        selected = os.environ["HOME"].rstrip("/") + "/" + selected[2:]
    try:
        workspace = Path(selected).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace is not a directory: " + str(workspace))
    except (OSError, RuntimeError) as error:
        raise ValueError("workspace must be an existing directory: " + selected + ": " + str(error))
    roots = {name: Path(path).resolve() for name, path in payload["roots"].items()}
    release = payload["release"]
    image = image_location(roots["cache"], release)
    encoded, installation, identity = installation_identity(release, image)
    target = roots["data"] / "releases" / identity
    protected = dict(roots)
    for name, path in (("helper", target), ("image", image)):
        try:
            protected[name] = path.resolve()
        except (OSError, RuntimeError) as error:
            raise ValueError("matching release is not ready; cannot resolve " + name + " " +
                             str(path) + ": " + str(error) + "\nRun: " + payload["setup_command"])
    check_workspace_isolation(workspace, protected)
    if target.is_dir() and not image.is_file():
        raise ValueError("matching release is not ready; image missing or not a regular file at " +
                         str(image) + "\nRun: " + payload["setup_command"])
    expected = {"helper.py": payload["helper"], "trusted.pem": payload["key"],
                "release.json": encoded, "installation.json": installation}
    if (not target.is_dir() or target.is_symlink() or not image.is_file() or any(
            not (target / name).is_file() or (target / name).is_symlink()
            or (target / name).read_bytes() != content.encode("utf-8")
            for name, content in expected.items())):
        raise ValueError("matching release is not ready at " + str(target) +
                         "\nRun: " + payload["setup_command"])
    verify(image, release, target / "trusted.pem")
    credentials = CredentialStore(roots["state"])
    with credentials.locked():
        needs_login = credentials.read(credentials.path) is None
    return {"workspace": str(workspace), "helper": str(target / "helper.py"), "image": str(image),
            "needs_login": needs_login,
            "user": pwd.getpwuid(os.getuid()).pw_name,
            "roots": {name: str(path) for name, path in roots.items()}}


def compute_environment(inside_step=False):
    # Prevent host options from adding mounts, changing the requested step, or
    # injecting container variables despite --cleanenv.
    prefixes = ("APPTAINER", "SINGULARITY", "SRUN_") + (() if inside_step else ("SLURM_",))
    return {key: value for key, value in os.environ.items() if not key.startswith(prefixes)}


def compute(payload):
    detected = platform.system() + " " + platform.machine()
    if detected != "Linux x86_64":
        raise ValueError("unsupported compute platform " + detected + "; required Linux x86_64")
    workspace = Path(payload["workspace"])
    if workspace.resolve() != workspace:
        raise ValueError("workspace resolves differently on compute node: " + str(workspace))
    state = Path(payload["roots"]["state"])
    if state.resolve() != state:
        raise ValueError("state resolves differently on compute node: " + str(state))
    for name, path, directory in (("workspace", payload["workspace"], True),
                                  ("helper", payload["helper"], False),
                                  ("image", payload["image"], False),
                                  ("state", payload["roots"]["state"], True)):
        location = Path(path)
        if not (location.is_dir() if directory else location.is_file()) or not os.access(path, os.R_OK):
            raise ValueError("compute node cannot access " + name + " path: " + path)
    for tool in ("srun", "apptainer"):
        if shutil.which(tool) is None:
            raise ValueError("required compute tool is missing: " + tool)
    image = Path(payload["image"])
    expected = payload["release"]["image"]
    if image.stat().st_size != expected["size"] or digest(image) != expected["sha256"]:
        raise ValueError("compute image differs from the verified release: " + str(image))
    if digest(Path(payload["helper"])) != payload["release"]["helper_sha256"]:
        raise ValueError("compute helper differs from the verified release: " + payload["helper"])
    environment = compute_environment()
    capabilities = subprocess.run(["apptainer", "exec", "--help"], env=environment,
                                  capture_output=True, text=True)
    for option in ("--cleanenv", "--containall", "--no-mount", "--home", "--mount", "--pwd", "--env", "--workdir"):
        if capabilities.returncode or option not in capabilities.stdout:
            raise ValueError("compute Apptainer lacks required capability " + option)
    command = ["srun", "--jobid=" + payload["job_id"], *payload["cluster"],
               "--nodes=1", "--ntasks=1", "--nodelist=" + payload["node"],
               "--cpus-per-task=" + payload["cpus"], "--exact",
               *([] if payload.get("authenticate") else ["--pty"]), "--export=ALL",
               "python3", payload["helper"], "--execute", json.dumps(payload)]
    return subprocess.run(command, env=environment).returncode


class CredentialStore:
    """Short locked snapshots and conditional commits; never lock a session."""

    def __init__(self, root):
        self.directory = root / "credentials"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("credential directory must be an owned directory, not a symlink")
        self.directory.chmod(0o700)
        self.path = self.directory / "auth.json"

    @contextmanager
    def locked(self):
        descriptor = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+b") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("credential lock must be an owned regular file")
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield

    @staticmethod
    def read(path):
        try:
            # Validate the descriptor before reading; a FIFO must not block exit.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("credential document must be an owned regular file")
            os.fchmod(stream.fileno(), 0o600)
            value = stream.read(1024 * 1024 + 1)
        try:
            parsed = json.loads(value)
            if len(value) > 1024 * 1024 or not isinstance(parsed, dict) or not parsed:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ValueError("credential document is invalid; existing saved login was preserved") from None
        return value

    def stage(self, path):
        with self.locked():
            initial = self.read(self.path)
        if initial is not None:
            path.write_bytes(initial)
            path.chmod(0o600)
        return initial

    def persist(self, path, initial):
        replacement = self.read(path)
        if replacement is None or replacement == initial:
            return
        with self.locked():
            if self.read(self.path) != initial:
                # Another session has already saved a newer login.
                return
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".auth-", suffix=".tmp",
                                                 dir=self.directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(replacement)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def authenticate(command, environment, scratch, device_auth=False):
    def emit(event, **values):
        print(json.dumps(dict(event=event, **values)), flush=True)

    status = subprocess.run([*command, "login", "status"], env=environment,
                            cwd=scratch, capture_output=True)
    if status.returncode == 0:
        emit("complete")
        return 0
    process = subprocess.Popen([*command, "login", *(["--device-auth"] if device_auth else [])],
                               env=environment, cwd=scratch,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    try:
        for line in process.stdout:
            url = line.strip()
            if device_auth:
                emit("message", text=line.rstrip())
            elif url.startswith("https://auth.openai.com/"):
                emit("browser", url=url)
        if process.wait():
            raise ValueError("Codex sign-in failed")
        emit("complete")
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def execute(payload):
    if os.environ.get("SLURM_JOB_ID") != payload["job_id"] or not os.environ.get("SLURM_STEP_ID"):
        raise ValueError("compute launch is not inside its owned Slurm job step")
    os.umask(0o077)
    workspace = Path(payload["workspace"])
    internal_home = "/codex-hpc-home"
    if contains(workspace, Path(internal_home)) or contains(Path(internal_home), workspace):
        raise ValueError("workspace overlaps the private container home: " + str(workspace))
    # /tmp is the node-local scratch boundary, independent of a shared TMPDIR.
    with tempfile.TemporaryDirectory(prefix="codex-hpc-", dir="/tmp") as temporary:
        scratch = Path(temporary).resolve()
        if contains(workspace, scratch):
            raise ValueError("node-local scratch lies within the workspace: " + str(scratch))
        home = scratch / "home"
        home.mkdir(mode=0o700)
        codex_home = home / ".codex"
        codex_home.mkdir(mode=0o700)
        credentials = CredentialStore(Path(payload["roots"]["state"]))
        auth = codex_home / "auth.json"
        initial = credentials.stage(auth)
        work = scratch / "work"
        work.mkdir(mode=0o700)
        mount = io.StringIO()
        csv.writer(mount, lineterminator="").writerow([
            "type=bind", "src=" + str(workspace), "dst=" + str(workspace)])
        environment = compute_environment(inside_step=True)
        environment.update(APPTAINER_CONFIGDIR=str(scratch / "config"),
                           APPTAINER_CACHEDIR=str(scratch / "cache"),
                           APPTAINER_TMPDIR=str(scratch), TMPDIR=str(scratch))
        command = ["apptainer", "exec", "--cleanenv", "--containall", "--no-mount",
                   "hostfs,bind-paths,cwd", "--home", str(home) + ":" + internal_home,
                   "--workdir", str(work), "--mount", mount.getvalue(), "--pwd", str(workspace),
                   "--env", "HOME=" + internal_home,
                   "--env", "CODEX_HOME=" + internal_home + "/.codex",
                   *(["--env", "BROWSER=/bin/true"] if payload.get("authenticate") else []),
                   payload["image"],
                   payload["release"].get("executables", {}).get("codex", "/usr/local/bin/codex"),
                   '--config=cli_auth_credentials_store="file"']
        try:
            if payload.get("authenticate"):
                # Login logs stay private and expire even when workspace config
                # sets another log location. Browser opening belongs to the client.
                command.append('--config=log_dir="' + internal_home + '/.codex/log"')
                return authenticate(command, environment, scratch, payload.get("device_auth", False))
            return subprocess.run(command, env=environment, cwd=scratch).returncode
        finally:
            credentials.persist(auth, initial)


def slurm(arguments):
    if arguments == ["check"]:
        for tool in ("sbatch", "squeue", "scancel"):
            if shutil.which(tool) is None:
                raise ValueError("required login-host tool is missing: " + tool)
        return
    if arguments[0] not in ("sbatch", "squeue", "scancel"):
        raise ValueError("unsupported scheduler operation")
    # Ambient Slurm command options must not change the displayed request, add an
    # array, redirect submission, or suppress/constrain cleanup unexpectedly.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("SBATCH_", "SQUEUE_", "SCANCEL_"))
                   and key != "SLURM_CLUSTERS"}
    os.execvpe(arguments[0], arguments, environment)


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--describe", action="store_true",
                          help="Report this helper's release and protocol identity.")
    commands.add_argument("--setup", help=argparse.SUPPRESS)
    commands.add_argument("--preflight", help=argparse.SUPPRESS)
    commands.add_argument("--slurm", help=argparse.SUPPRESS)
    commands.add_argument("--compute", help=argparse.SUPPRESS)
    commands.add_argument("--execute", help=argparse.SUPPRESS)
    commands.add_argument("--authenticate", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.describe:
        release = json.loads(Path(__file__).with_name("release.json").read_text())
        print(json.dumps({key: release[key] for key in (
            "version", "protocol_version", "image_interface_version")}))
    elif args.setup:
        setup(json.loads(args.setup))
    elif args.slurm:
        slurm(json.loads(args.slurm))
    elif args.compute:
        return compute(json.loads(args.compute))
    elif args.authenticate:
        return compute(dict(json.loads(args.authenticate), authenticate=True))
    elif args.execute:
        return execute(json.loads(args.execute))
    else:
        print(json.dumps(preflight(json.loads(args.preflight))))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("codex-hpc: remote operation failed: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("codex-hpc: remote operation interrupted.", file=sys.stderr)
        sys.exit(130)
