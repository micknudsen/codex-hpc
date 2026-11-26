#!/bin/sh
set -eu
test "$(uname -m)" = x86_64
test -x /usr/local/bin/codex
test -x /usr/local/bin/codex-code-mode-host
test -x /usr/local/bin/rg
test "$(command -v bwrap)" = /usr/bin/bwrap
test -x /usr/bin/git
test -x /bin/bash
test -x /usr/bin/python3
test ! -e /root/.codex/auth.json
/usr/local/bin/codex --version
/usr/local/bin/codex-code-mode-host --help > /dev/null
/usr/local/bin/rg --version | head -n 1
bwrap --version
# Codex checks these options before it selects a system Bubblewrap binary.
bwrap_help=$(bwrap --help 2>&1)
for option in --as-pid-1 --perms; do
    printf '%s\n' "$bwrap_help" | grep -Fq -- "$option"
done
/usr/bin/git --version
/usr/bin/python3 --version
node --version
npm --version
npx --version
jq --version

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
cd "$workdir"
python3 -m venv venv
venv/bin/python -m pip --version

cat > check_c.c <<'EOF'
#include <stdlib.h>
int main(void) { return EXIT_SUCCESS; }
EOF
cat > check_cxx.cc <<'EOF'
#include <vector>
int main() { return std::vector<int>{0}.front(); }
EOF
make -s check_c check_cxx
./check_c
./check_cxx

printf '%s\n' '{"scripts":{"test":"node --check check.js"}}' > package.json
printf '%s\n' 'const ready = true' > check.js
jq -e '.scripts.test == "node --check check.js"' package.json > /dev/null
npm --offline --no-update-notifier --cache "$workdir/npm-cache" run --silent test

