"""Bootstrap sent to the login host before a helper has been installed.

Keep this script self-contained: the remote host only needs Python's standard library.
"""
import json
import os
import sys


def validate_path(path, label):
    if not path.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError(label + " must be an absolute POSIX path or a quoted '~/path'; "
                         "control characters are unsupported")
    ancestor = path
    while not os.path.lexists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    if not os.path.isdir(ancestor):
        raise ValueError(label + " is not a directory or has a non-directory ancestor: " + ancestor)
    return path


def main():
    home = os.environ.get("HOME", "")
    if not home.startswith("/"):
        raise ValueError("remote HOME must be an absolute POSIX path")
    overrides = json.loads(sys.argv[1])
    roots = {}
    for name, fallback in (("data", ".local/share"), ("cache", ".cache"),
                           ("state", ".local/state")):
        base = os.environ.get("XDG_" + name.upper() + "_HOME", "")
        if not base.startswith("/"):
            base = os.path.join(home, fallback)
        path = overrides[name]
        if path is None:
            path = os.path.join(base, "codex-hpc")
        elif path.startswith("~/"):
            path = home.rstrip("/") + "/" + path[2:]
        roots[name] = validate_path(path, name + " root")
    print(json.dumps(roots))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print("codex-hpc: " + str(error), file=sys.stderr)
        sys.exit(1)
