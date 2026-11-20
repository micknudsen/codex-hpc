"""Public command-line interface."""
import argparse
from importlib.resources import files
import json
import os
import shlex
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Run Codex in one remote Slurm workspace.",
        epilog="Development build: setup and connect currently discover storage and exit nonzero; "
               "remote release installation and session launch are not implemented yet.")
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
            ["ssh", "-T", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
             "--", args.host, remote_command],
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
    print("codex-hpc: remote release not installed; this development client only discovers storage.",
          file=sys.stderr)
    return 1
