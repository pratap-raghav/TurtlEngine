#!/usr/bin/env bash
#
# Run the TurtlEngine test suite.
#   - Python notifier tests: always run (no root, no network, no bot).
#   - Bash tests: run when the environment allows, else report SKIP.
#
# Exit non-zero if any test actually FAILS. Skips do not fail the run.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pass=0
fail=0
skip=0

echo "=== Python notifier tests ==="
if python3 -m unittest discover -s "${HERE}" -p 'test_*.py' -v; then
    pass=$((pass + 1))
else
    fail=$((fail + 1))
fi

for t in test_setup_notifier.sh test_mount_drive.sh; do
    echo ""
    echo "=== ${t} ==="
    bash "${HERE}/${t}"
    rc=$?
    if [[ "${rc}" -eq 0 ]]; then
        pass=$((pass + 1))
    elif [[ "${rc}" -eq 77 ]]; then
        skip=$((skip + 1))
    else
        fail=$((fail + 1))
    fi
done

echo ""
echo "=== Summary: ${pass} passed, ${fail} failed, ${skip} skipped ==="
[[ "${fail}" -eq 0 ]]
