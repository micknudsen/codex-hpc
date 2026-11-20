#!/bin/sh
set -eu
test "$(uname -m)" = x86_64
test -x /usr/local/bin/codex
test -x /usr/local/bin/rg
test -x /usr/bin/git
test -x /bin/bash
test -x /usr/bin/python3
test ! -e /root/.codex/auth.json
/usr/local/bin/codex --version
/usr/local/bin/rg --version | head -n 1
/usr/bin/git --version
/usr/bin/python3 --version

