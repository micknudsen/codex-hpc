"""Public command-line interface."""
import argparse
from importlib.resources import files
import json
import os
import shlex
import subprocess
import sys


def ssh_arguments(host, command):
    return ["ssh", "-T", "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "--", host, command]


def main():
    parser = argparse.ArgumentParser(
        description="Run Codex in one remote Slurm workspace.",
        epilog="Setup installs the bundled release on the login host. "
               "Development build: connect does not launch sessions yet.")
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
    args = parser.parse_args()
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
            operation = "setup" if args.command == "setup" else "preflight"
            command = "python3 - --" + operation + " " + shlex.quote(json.dumps(payload))
            return subprocess.run(
                ssh_arguments(args.host, command),
                input=helper, text=True).returncode
        except (OSError, ValueError) as error:
            print("codex-hpc: " + args.command + " failed: " + str(error), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("codex-hpc: " + args.command + " interrupted; rerun to retry.", file=sys.stderr)
            return 130
    print("codex-hpc: remote release not installed; this development client only discovers storage.",
          file=sys.stderr)
    return 1
