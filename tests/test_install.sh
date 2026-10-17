#!/usr/bin/env bash
#
# Static checks on install.sh that need no root and no real provisioning:
#   - the TurtlEngine banner is printed BEFORE any package work (item 2)
#   - the removed swapfile step is really gone (item 1)
#   - installation shares/formats nothing automatically (item 4)

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
INSTALL="${REPO}/scripts/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

# Banner emitted before the first apt-get.
banner_line="$(grep -n 'print_banner$' "${INSTALL}" | tail -n1 | cut -d: -f1)"
apt_line="$(grep -n 'apt-get' "${INSTALL}" | head -n1 | cut -d: -f1)"
[[ -n "${banner_line}" ]] || fail "install.sh never calls print_banner"
[[ -n "${apt_line}" ]] || fail "install.sh has no apt-get step"
[[ "${banner_line}" -lt "${apt_line}" ]] \
    || fail "banner call (line ${banner_line}) is not before first apt-get (line ${apt_line})"

# The banner artwork/tagline is actually present.
grep -q 'T u r t l E n g i n e' "${INSTALL}" || fail "banner artwork missing from install.sh"
grep -q 'slow is smooth, smooth is fast' "${INSTALL}" || fail "banner tagline missing"

# Swapfile step removed.
grep -qiE 'swapfile|swappiness|SWAP_SIZE_GB' "${INSTALL}" \
    && fail "swapfile step still present in install.sh"

# No automatic sharing/formatting during install. The CLI-install step legitimately
# COPIES setup-samba.sh/mount-drive.sh into place, so we check for the dangerous
# OPERATIONS themselves rather than the filenames.
grep -qE 'mkfs|mkswap|smbpasswd|smb\.conf|--format|parted' "${INSTALL}" \
    && fail "install.sh must not format disks or create shares automatically"
# A real invocation would run the helper as a command (e.g. ".../setup-samba.sh" <args>);
# copying it via `install -m` is fine. Flag an exec-style call only.
grep -qE '(\./|/)(setup-samba|mount-drive)\.sh"?[[:space:]]+[^;]' "${INSTALL}" \
    && fail "install.sh must not invoke the storage/share helpers automatically"

echo "PASS: banner-first, swap removed, no auto share/format in install.sh."
