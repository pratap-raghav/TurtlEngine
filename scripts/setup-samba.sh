#!/usr/bin/env bash
#
# setup-samba.sh -- share a directory over SMB (Windows / Android / iOS).
#
# Usage: sudo ./scripts/setup-samba.sh <username> <mountpoint> [sharename]
#   <username>    existing Linux user allowed to access the share.
#   <mountpoint>  directory to share, e.g. /mnt/data
#   [sharename]   SMB share name (default: turtlengine)
#
# Idempotent: re-running won't duplicate the share block. The SMB password is
# separate from the Linux password; you'll be prompted to set it.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

if [[ $# -lt 2 ]]; then
    echo "Usage: sudo $0 <username> <mountpoint> [sharename]" >&2
    exit 1
fi

SHARE_USER="$1"
MOUNTPOINT="$2"
SHARENAME="${3:-turtlengine}"
SMB_CONF="/etc/samba/smb.conf"

if [[ ! -d "${MOUNTPOINT}" ]]; then
    echo "Error: mountpoint ${MOUNTPOINT} does not exist." >&2
    exit 1
fi

if ! id "${SHARE_USER}" >/dev/null 2>&1; then
    echo "Error: user ${SHARE_USER} does not exist." >&2
    exit 1
fi

echo "==> Installing samba..."
export DEBIAN_FRONTEND=noninteractive
apt-get install -y samba

# ---------------------------------------------------------------------------
# Append the share block if it isn't already there.
# ---------------------------------------------------------------------------
if grep -q "^\[${SHARENAME}\]" "${SMB_CONF}"; then
    echo "    share [${SHARENAME}] already in ${SMB_CONF}; leaving it."
else
    echo "==> Adding [${SHARENAME}] to ${SMB_CONF}..."
    cat >> "${SMB_CONF}" <<EOF

[${SHARENAME}]
   path = ${MOUNTPOINT}
   browseable = yes
   read only = no
   valid users = ${SHARE_USER}
   create mask = 0644
   directory mask = 0755
EOF
fi

# ---------------------------------------------------------------------------
# SMB password (separate from the Linux password) + ownership.
# ---------------------------------------------------------------------------
echo "==> Set the SMB password for ${SHARE_USER} (separate from the Linux password):"
smbpasswd -a "${SHARE_USER}"
smbpasswd -e "${SHARE_USER}"

chown "${SHARE_USER}:${SHARE_USER}" "${MOUNTPOINT}"

echo "==> Restarting and enabling smbd..."
systemctl restart smbd
systemctl enable smbd >/dev/null

# ---------------------------------------------------------------------------
# Connect strings.
# ---------------------------------------------------------------------------
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
EOF
