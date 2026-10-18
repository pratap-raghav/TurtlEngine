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
echo "About to share a folder on your network so other devices can open it."
echo ""
echo "  Folder being shared : ${SHARE_DIR}"
echo "  Name on the network : ${SHARENAME}   (what it shows up as on your phone/PC)"
echo "  Who may open it      : ${SHARE_USER}   (your Linux user; needs a password, set below)"
[[ -n "${MOUNT_TGT}" ]] && echo "  Lives on disk        : ${MOUNT_SRC}, mounted at ${MOUNT_TGT}"
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
    echo "CAUTION: ${SHARE_DIR} is a ${critical}." >&2
    echo "         Sharing it could expose private system files to anyone on" >&2
    echo "         the network. It's safer to share a folder on a data drive," >&2
    echo "         e.g. /mnt/nas-1tb/media." >&2
    read -r -p "To go ahead anyway, type the folder path again: " c || c=""
    if [[ "${c}" != "${SHARE_DIR}" ]]; then
        echo "That didn't match, so I stopped. Nothing was changed." >&2
        exit 1
    fi
fi

echo "Step 1 of 4 — Installing the file-sharing service (Samba)."
echo "  This is what lets Windows, Mac, phones and other Linux machines"
echo "  see the folder over the network."
export DEBIAN_FRONTEND=noninteractive
apt-get install -y samba

# --- Append the share block if it isn't already there -----------------------
echo ""
echo "Step 2 of 4 — Registering the share with Samba."
if grep -q "^\[${SHARENAME}\]" "${SMB_CONF}"; then
    echo "  A share named \"${SHARENAME}\" already exists, so I left it untouched."
else
    echo "  Adding \"${SHARENAME}\" to Samba's config. (Your old config is backed"
    echo "  up first, so this can always be undone.)"
    # Back up first so a rejected config can be restored exactly.
    BACKUP="${SMB_CONF}.turtlengine.bak"
    cp "${SMB_CONF}" "${BACKUP}"
    cat >> "${SMB_CONF}" <<EOF

[${SHARENAME}]
   path = ${SHARE_DIR}
   browseable = yes
   read only = no
   valid users = ${SHARE_USER}
   create mask = 0644
   directory mask = 0755
EOF
    # Validate the edited file; if testparm rejects it, restore the backup so
    # smbd is never reloaded onto a broken smb.conf.
    if command -v testparm >/dev/null 2>&1; then
        if testparm -s "${SMB_CONF}" >/dev/null 2>&1; then
            echo "  Config checked — all good."
        else
            echo "  The new config didn't check out, so I put your old one back." >&2
            echo "  Nothing was changed. Please re-run and try again." >&2
            cp "${BACKUP}" "${SMB_CONF}"
            exit 1
        fi
    fi
fi

# --- SMB password (separate from the Linux password) ------------------------
echo ""
echo "Step 3 of 4 — Set the password for opening the share."
echo "  This password is used ONLY to open the share from other devices."
echo "  It is separate from your normal Linux login — it can be the same or"
echo "  different, your choice. You'll enter it on your phone/PC the first time"
echo "  you connect as \"${SHARE_USER}\". (Typing it now shows nothing on screen.)"
echo ""
smbpasswd -a "${SHARE_USER}"
smbpasswd -e "${SHARE_USER}"

# --- Ownership: opt-in, never recursive, never automatic --------------------
# We do NOT recursively chown/chmod. Offer to set ownership of just the top
# directory so the user can write to the share; skip entirely on anything but
# an explicit yes.
owner="$(stat -c '%U' "${SHARE_DIR}")"
if [[ "${owner}" != "${SHARE_USER}" ]]; then
    echo ""
    echo "Right now the folder is owned by \"${owner}\", not \"${SHARE_USER}\"."
    echo "If \"${SHARE_USER}\" later can't SAVE files into the share, making them the"
    echo "owner usually fixes it. (Only the top folder changes — files inside keep"
    echo "their current owners.)"
    read -r -p "Make \"${SHARE_USER}\" the owner of this folder now? [y/N] " a || a=""
    case "${a}" in
        [yY]|[yY][eE][sS]) chown "${SHARE_USER}" "${SHARE_DIR}"; echo "  Done — \"${SHARE_USER}\" now owns the folder." ;;
        *) echo "  Left as-is. If saving files fails later, re-run this and answer yes." ;;
    esac
fi

echo ""
echo "Step 4 of 4 — Turning the share on."
# Reload applies the new share without dropping existing SMB connections. If
# smbd isn't running yet (first share, right after installing samba), fall back
# to a restart to start it.
systemctl reload smbd 2>/dev/null || systemctl restart smbd
systemctl enable smbd >/dev/null

# --- Connect strings --------------------------------------------------------
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
LAN_IP="${LAN_IP:-<nas-ip>}"

cat <<EOF

✅ Done! The folder is now shared as "${SHARENAME}".

Open it from any device using the matching line below — copy it exactly.
When asked, sign in as "${SHARE_USER}" with the share password you just set.

  Windows (paste into the File Explorer address bar):
      \\\\${LAN_IP}\\${SHARENAME}

  macOS (Finder -> Go -> Connect to Server, Cmd-K):
      smb://${LAN_IP}/${SHARENAME}

  Linux (Files location bar, Ctrl-L):
      smb://${LAN_IP}/${SHARENAME}
  Linux (mount from a terminal):
      sudo mount -t cifs //${LAN_IP}/${SHARENAME} /mnt/${SHARENAME} -o username=${SHARE_USER},uid=\$(id -u),gid=\$(id -g)

  iOS (Files app -> Connect to Server):
      smb://${LAN_IP}/${SHARENAME}

  Android (e.g. CX File Explorer / Solid Explorer -> add SMB):
      Host: ${LAN_IP}
      Share: ${SHARENAME}
      User: ${SHARE_USER}

Use the SMB password you just set (not the Linux one).

Tip: from Telegram, send your bot /shares anytime to get these connect
commands (every OS, tap-to-copy) without looking them up again.

To add ANOTHER independent share later, run this again with a different
directory and name, e.g.:
      sudo ./scripts/setup-samba.sh ${SHARE_USER} /mnt/nas-4tb/backups Storage4TB
or use the guided flow:
      sudo turtlengine share add
EOF
