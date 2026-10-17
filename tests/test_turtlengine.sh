#!/usr/bin/env bash
#
# turtlengine CLI tests -- the unprivileged, read-only / config-editing paths.
# Uses a throwaway smb.conf via SMB_CONF, so no root and no real samba needed.
# Covers: invalid args, share list, multiple independent shares, targeted
# removal that preserves the rest, non-existent removal, and cancellation.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
CLI="${REPO}/scripts/turtlengine"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
CONF="${TMP}/smb.conf"

fail() { echo "FAIL: $*" >&2; exit 1; }

cat > "${CONF}" <<'EOF'
[global]
   workgroup = WORKGROUP

[Storage1TB]
   path = /mnt/nas-1tb/media
   valid users = ghost

[Storage4TB]
   path = /mnt/nas-4tb/backups
   valid users = ghost

[SharedFiles]
   path = /srv/shared
   valid users = ghost
EOF

# --- invalid command -> exit 2 ----------------------------------------------
"${CLI}" bogus >/dev/null 2>&1 && fail "invalid command should exit non-zero"
rc=$?; [[ "${rc}" -eq 2 ]] || fail "invalid command exit was ${rc}, expected 2"

"${CLI}" storage frobnicate >/dev/null 2>&1 && fail "invalid subcommand should fail"

# --- share list shows all three --------------------------------------------
out="$(SMB_CONF="${CONF}" "${CLI}" share list)"
for s in Storage1TB Storage4TB SharedFiles; do
    grep -q "${s}" <<<"${out}" || fail "share list missing ${s}"
done
grep -q "global" <<<"${out}" && fail "share list should not show [global]"

# --- remove the middle share; others + global survive -----------------------
SMB_CONF="${CONF}" "${CLI}" share remove Storage4TB <<<"y" >/dev/null
grep -q '^\[global\]'      "${CONF}" || fail "[global] was lost"
grep -q '^\[Storage1TB\]'  "${CONF}" || fail "[Storage1TB] was lost"
grep -q '^\[SharedFiles\]' "${CONF}" || fail "[SharedFiles] was lost"
grep -q '^\[Storage4TB\]'  "${CONF}" && fail "[Storage4TB] was not removed"
[[ -f "${CONF}.turtlengine.bak" ]] || fail "no backup was written"

# Its path line must be gone too (not just the header).
grep -q '/mnt/nas-4tb/backups' "${CONF}" && fail "removed share's path lingered"

# --- removing a non-existent share errors -----------------------------------
SMB_CONF="${CONF}" "${CLI}" share remove NoSuchShare <<<"y" >/dev/null 2>&1 \
    && fail "removing a missing share should fail"

# --- cancellation leaves config untouched -----------------------------------
before="$(sha256sum "${CONF}" | awk '{print $1}')"
SMB_CONF="${CONF}" "${CLI}" share remove Storage1TB <<<"n" >/dev/null
after="$(sha256sum "${CONF}" | awk '{print $1}')"
[[ "${before}" == "${after}" ]] || fail "cancelled removal still modified the config"

echo "PASS: turtlengine CLI args, share list/remove, multi-share and cancel."
