"""Public command-line interface."""
import argparse
from importlib.resources import files
import json
import os
import shlex
import signal
import subprocess
import sys

from . import allocation, signin
from .transport import ssh_arguments


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    parser = argparse.ArgumentParser(
        description="Run Codex in one remote Slurm workspace.",
        epilog="Setup installs the bundled release on the login host. "
               "Connect launches Codex in one isolated Slurm workspace.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (("setup", "Prepare the matching remote release."),
                               ("connect", "Run Codex in an existing remote workspace.")):
        subparser = commands.add_parser(
            command, help=help_text, description=help_text,
            epilog="CLI options override CODEX_HPC_* values. Storage defaults use the remote "
                   "XDG homes, falling back to ~/.local/share, ~/.cache and ~/.local/state "
                   "with /codex-hpc appended. Quote ~/ paths to avoid local shell expansion.")
        subparser.add_argument("--host", default=os.environ.get("CODEX_HPC_HOST"),
                               help="SSH hostname, user@host or alias (CODEX_HPC_HOST).")
        for name in ("data", "cache", "state"):
            variable = "CODEX_HPC_" + name.upper() + "_ROOT"
            subparser.add_argument("--" + name + "-root", default=os.environ.get(variable),
                                   help="Remote absolute path or '~/path' (" + variable + ").")
        if command == "connect":
            subparser.add_argument("workspace", help="Existing absolute remote path or quoted '~/project'.")
            subparser.add_argument("--device-auth", action="store_true",
                                   help="Explicitly use device-code sign-in instead of browser forwarding.")
            for name, default in (("cpus", "2"), ("mem", "4G"), ("time", "02:00:00"),
                                  ("account", None), ("partition", None)):
                variable = "CODEX_HPC_" + name.upper()
                subparser.add_argument("--" + name, default=os.environ.get(variable, default),
                                      help="Slurm " + name + " (" + variable + "; default: " +
                                      (default or "Slurm choice") + ").")
    args = parser.parse_args()
    if args.command == "connect":
        resources = {name: getattr(args, name) for name in ("cpus", "mem", "time", "account", "partition")}
        for name, value in resources.items():
            if value is not None and (not value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                parser.error("--" + name + " must be a nonempty Slurm value without control characters")
        if not args.cpus.isdecimal() or int(args.cpus) < 1:
            parser.error("--cpus must be a positive integer")
    if not args.host:
        parser.error("set --host or CODEX_HPC_HOST to an SSH destination")
    if args.host.startswith("-") or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                                        for char in args.host):
        parser.error("SSH destination must be a hostname, user@host or SSH alias without whitespace")
    source = files("codex_hpc").joinpath("discovery.py").read_text(encoding="utf-8")
    overrides = {name: getattr(args, name + "_root") for name in ("data", "cache", "state")}
    remote_command = "python3 - " + shlex.quote(json.dumps(overrides))
    try:
        result = subprocess.run(
            ssh_arguments(args.host, remote_command),
            input=source, text=True, stdout=subprocess.PIPE)
    except OSError as error:
        print("codex-hpc: cannot start SSH discovery: " + str(error) +
              "; install OpenSSH and check PATH.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("codex-hpc: discovery interrupted.", file=sys.stderr)
        return 130
    if result.returncode:
        print("codex-hpc: remote discovery failed; check the SSH destination, remote Python 3 "
              "and storage settings (exit " + str(result.returncode) + ").", file=sys.stderr)
        return 1
    try:
        roots = json.loads(result.stdout)
        if (not isinstance(roots, dict) or set(roots) != {"data", "cache", "state"}
                or any(not isinstance(path, str) or not path.startswith("/")
                       for path in roots.values())):
            raise ValueError("invalid storage roots")
    except ValueError:
        print("codex-hpc: invalid remote discovery response; ensure SSH startup scripts "
              "do not print to stdout and Python 3 is available.", file=sys.stderr)
        return 1
    print("SSH destination: " + args.host)
    for name, path in roots.items():
        print(name + " root: " + path)
    bundle = files("codex_hpc").joinpath("release")
    if bundle.joinpath("release.json").is_file():
        try:
            helper = bundle.joinpath("helper.py").read_text(encoding="utf-8")
            payload = {"roots": roots, "release": json.loads(bundle.joinpath("release.json").read_text()),
                       "key": bundle.joinpath("trusted.pem").read_text(encoding="utf-8"),
                       "helper": helper}
            if args.command == "connect":
                payload["workspace"] = args.workspace
                recovery = ["codex-hpc", "setup", "--host", args.host]
                for name, path in roots.items():
                    recovery.extend(["--" + name + "-root", path])
                payload["setup_command"] = shlex.join(recovery)
            def remote(operation, request):
                command = "python3 - --" + operation + " " + shlex.quote(json.dumps(request))
                return subprocess.run(ssh_arguments(args.host, command), input=helper, text=True,
                                      stdout=subprocess.PIPE)

            result = remote("setup" if args.command == "setup" else "preflight", payload)
            if args.command == "setup" or result.returncode:
                print(result.stdout, end="", flush=True)
                return result.returncode
            ready = json.loads(result.stdout)
            print("Workspace: " + ready["workspace"], flush=True)
            print("Preflight passed.", flush=True)
            if ready["needs_login"] and not args.device_auth:
                signin.check_callback_port()
            def launch(job_id, node, cluster):
                request = dict(ready, job_id=job_id, node=node, cluster=cluster,
                               cpus=resources["cpus"], release=payload["release"],
                               device_auth=args.device_auth)
                host = ready["user"] + "@" + node
                authentication = shlex.join(["python3", ready["helper"], "--authenticate", json.dumps(request)])
                signin.authenticate(ssh_arguments(host, authentication, jump_host=args.host, terminal=False),
                                    host, args.host)
                command = shlex.join(["python3", ready["helper"], "--compute", json.dumps(request)])
                return subprocess.run(ssh_arguments(
                    host, command, jump_host=args.host)).returncode

            return allocation.connect(args.host, remote, resources, launch)
        except (OSError, ValueError) as error:
            print("codex-hpc: " + args.command + " failed: " + str(error), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("codex-hpc: " + args.command + " interrupted; rerun to retry.", file=sys.stderr)
            return 130
    print("codex-hpc: remote release not installed; this development client only discovers storage.",
          file=sys.stderr)
    return 1
