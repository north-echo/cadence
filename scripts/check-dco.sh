#!/usr/bin/env bash
# DCO sign-off check.
#
# Pre-commit invokes this with the commit-message file as $1. Every commit must
# carry a Signed-off-by trailer. Use `git commit -s` to add it automatically.
#
# Per CADENCE-SPEC.md §11 the canonical trailer is:
#   Signed-off-by: Christopher Lusk <clusk@northecho.dev>

set -euo pipefail

msg_file="${1:?commit message file required}"

# Strip comments before checking.
if grep -E '^Signed-off-by: .+ <.+@.+>' "$msg_file" | grep -qv '^#'; then
    exit 0
fi

cat >&2 <<'EOF'
ERROR: missing DCO sign-off.

Every commit must carry a "Signed-off-by: Name <email>" trailer.

Per CADENCE-SPEC.md §11, use:

    git commit -s

with git configured as:

    git config user.name  "Christopher Lusk"
    git config user.email "clusk@northecho.dev"

EOF
exit 1
