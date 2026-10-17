#!/usr/bin/env bash
#
# setup-samba.sh -- share ONE directory over SMB (Windows / Android / iOS).
#
# Usage: sudo ./scripts/setup-samba.sh <username> <directory> [sharename]
#   <username>     existing Linux user allowed to access the share.
#   <directory>    directory to share, e.g. /mnt/nas-1tb/media
#   [sharename]    SMB share name (default: basename of the directory)
#
# This configures exactly the one share you name. It does NOT decide which
# disks or folders to share -- you choose each path explicitly, and you can run
# it again (or `turtlengine share add`) to add another independent share.
#
# Idempotent: re-running won't duplicate the share block. The SMB password is
# separate from the Linux password; you'll be prompted to set it.

set -euo pipefail

SMB_CONF="${SMB_CONF:-/etc/samba/smb.conf}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

if [[ $# -lt 2 ]]; then
    echo "Usage: sudo $0 <username> <directory> [sharename]" >&2
    exit 1
fi

SHARE_USER="$1"
SHARE_DIR="$2"
# Default share name = basename of the directory, sanitised to a safe token.
DEFAULT_NAME="$(basename "${SHARE_DIR}" | tr -c 'A-Za-z0-9_-' '_')"
SHARENAME="${3:-${DEFAULT_NAME}}"

# --- Validation -------------------------------------------------------------
if [[ ! -d "${SHARE_DIR}" ]]; then
    echo "Error: directory ${SHARE_DIR} does not exist." >&2
    echo "       Create it first (and, for a data drive, mount it) then re-run." >&2
    exit 1
fi

if ! id "${SHARE_USER}" >/dev/null 2>&1; then
    echo "Error: user ${SHARE_USER} does not exist." >&2
    exit 1
fi

# Resolve to an absolute path for the config and the checks below.
SHARE_DIR="$(cd "${SHARE_DIR}" && pwd)"

# --- Where does this path actually live? ------------------------------------
MOUNT_SRC=""
MOUNT_TGT=""
if command -v findmnt >/dev/null 2>&1; then
    # Trailing `|| true`: `read` returns non-zero at EOF (findmnt printed
    # nothing), which under `set -e` would otherwise abort the script.
    read -r MOUNT_SRC MOUNT_TGT < <(findmnt -n -o SOURCE,TARGET --target "${SHARE_DIR}" 2>/dev/null || true) || true
fi
echo "Directory:  ${SHARE_DIR}"
echo "Share name: ${SHARENAME}"
echo "User:       ${SHARE_USER}"
[[ -n "${MOUNT_TGT}" ]] && echo "Filesystem: ${MOUNT_SRC} mounted at ${MOUNT_TGT}"
echo ""

# Warn when the path is on the root filesystem or a system-critical location.
critical=""
case "${SHARE_DIR}/" in
    /|/bin/*|/boot/*|/etc/*|/lib/*|/lib64/*|/proc/*|/run/*|/sbin/*|/sys/*|/usr/*|/var/*|/root/*)
        critical="system-critical path"
        ;;
esac
if [[ "${MOUNT_TGT}" == "/" ]]; then
    critical="${critical:+${critical}; }on the root filesystem (/)"
fi
if [[ -n "${critical}" ]]; then
    echo "WARNING: ${SHARE_DIR} is ${critical}." >&2
    echo "         Sharing it over SMB may expose sensitive files. Prefer a" >&2
    echo "         directory on a dedicated data mount (e.g. /mnt/nas-1tb/...)." >&2
    read -r -p "Type the share directory again to confirm you really mean it: " c || c=""
    if [[ "${c}" != "${SHARE_DIR}" ]]; then
        echo "Confirmation did not match; aborting. Nothing was changed." >&2
        exit 1
    fi
fi

echo "==> Installing samba..."
export DEBIAN_FRONTEND=noninteractive
apt-get install -y samba

# --- Append the share block if it isn't already there -----------------------
if grep -q "^\[${SHARENAME}\]" "${SMB_CONF}"; then
    echo "    share [${SHARENAME}] already in ${SMB_CONF}; leaving it untouched."
else
    echo "==> Adding [${SHARENAME}] to ${SMB_CONF}..."
    cat >> "${SMB_CONF}" <<EOF

[${SHARENAME}]
   path = ${SHARE_DIR}
   browseable = yes
   read only = no
   valid users = ${SHARE_USER}
   create mask = 0644
   directory mask = 0755
EOF
fi

# --- SMB password (separate from the Linux password) ------------------------
echo "==> Set the SMB password for ${SHARE_USER} (separate from the Linux password):"
smbpasswd -a "${SHARE_USER}"
smbpasswd -e "${SHARE_USER}"

# --- Ownership: opt-in, never recursive, never automatic --------------------
# We do NOT recursively chown/chmod. Offer to set ownership of just the top
# directory so the user can write to the share; skip entirely on anything but
# an explicit yes.
owner="$(stat -c '%U' "${SHARE_DIR}")"
if [[ "${owner}" != "${SHARE_USER}" ]]; then
    read -r -p "Set owner of ${SHARE_DIR} (top level only) to ${SHARE_USER}? [y/N] " a || a=""
    case "${a}" in
        [yY]|[yY][eE][sS]) chown "${SHARE_USER}" "${SHARE_DIR}"; echo "    owner set (non-recursive)." ;;
        *) echo "    left ownership as-is (${owner}). Adjust manually if writes fail." ;;
    esac
fi

echo "==> Restarting and enabling smbd..."
systemctl restart smbd
systemctl enable smbd >/dev/null

# --- Connect strings --------------------------------------------------------
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
LAN_IP="${LAN_IP:-<nas-ip>}"

cat <<EOF

SMB share [${SHARENAME}] is live. Connect with:

  Windows (File Explorer address bar):
      \\\\${LAN_IP}\\${SHARENAME}

  iOS (Files app -> Connect to Server):
      smb://${LAN_IP}

  Android (e.g. CX File Explorer / Solid Explorer -> add SMB):
      Host: ${LAN_IP}
      Share: ${SHARENAME}
      User: ${SHARE_USER}

Use the SMB password you just set (not the Linux one).

To add ANOTHER independent share later, run this again with a different
directory and name, e.g.:
      sudo ./scripts/setup-samba.sh ${SHARE_USER} /mnt/nas-4tb/backups Storage4TB
or use the guided flow:
      sudo turtlengine share add
EOF
