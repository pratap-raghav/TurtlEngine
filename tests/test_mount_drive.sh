#!/usr/bin/env bash
#
# Item 6 (Storage failure): an unformatted device (no filesystem UUID) must
# cause mount-drive.sh to exit cleanly and write NOTHING to fstab.
#
# Needs real root + loop devices (fakeroot cannot create them), so it SKIPS
# (exit 77) when not root or when losetup is unavailable. It operates only on a
# throwaway sparse file and a throwaway fstab -- never the host's /etc/fstab or
# any real disk.

set -euo pipefail

SKIP=77
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "SKIP: needs real root to attach a loop device." >&2
    exit "${SKIP}"
fi
if ! command -v losetup >/dev/null 2>&1; then
    echo "SKIP: losetup not available." >&2
    exit "${SKIP}"
fi

TMP="$(mktemp -d)"
LOOP=""
cleanup() {
    [[ -n "${LOOP}" ]] && losetup -d "${LOOP}" 2>/dev/null || true
    rm -rf "${TMP}"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# A 16 MiB sparse file, attached as a loop device, left UNFORMATTED (no UUID).
dd if=/dev/zero of="${TMP}/disk.img" bs=1M count=16 status=none
LOOP="$(losetup --find --show "${TMP}/disk.img")"
[[ -b "${LOOP}" ]] || fail "could not create a loop block device"

FSTAB_FILE="${TMP}/fstab"
: > "${FSTAB_FILE}"   # start empty

# Mount-only mode on an unformatted device -> no UUID -> must fail cleanly.
set +e
FSTAB="${FSTAB_FILE}" "${REPO}/scripts/mount-drive.sh" "${LOOP}" "${TMP}/mnt" >/dev/null 2>&1
rc=$?
set -e

[[ "${rc}" -ne 0 ]] || fail "expected non-zero exit for an unformatted device, got 0"
[[ ! -s "${FSTAB_FILE}" ]] || fail "fstab was modified: $(cat "${FSTAB_FILE}")"

echo "PASS: missing UUID exits cleanly with no fstab entry written."
