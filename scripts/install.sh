#!/usr/bin/env bash
#
# install.sh -- TurtlEngine main provisioning.
#
# Turns a fresh Ubuntu 26.04 Desktop install into an always-on headless dev box.
# Idempotent: every step checks state before acting, so it is safe to re-run
# (the same repo will later provision a second machine too).
#
# Usage: sudo ./scripts/install.sh
#
# Env overrides:
#   SWAP_SIZE_GB   swapfile size in GB (default 16)

set -euo pipefail

SWAP_SIZE_GB="${SWAP_SIZE_GB:-16}"
SWAPFILE="/swapfile"
MOTD_SRC_NAME="turtlengine.txt"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# 1. Guard: require root, detect the invoking user.
# ---------------------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "${TARGET_USER}" ]]; then
    echo "Warning: \$SUDO_USER is empty; run via sudo for correct ownership." >&2
    TARGET_USER="root"
fi

echo "=== TurtlEngine install (user: ${TARGET_USER}) ==="

# ---------------------------------------------------------------------------
# 2. System update.
# ---------------------------------------------------------------------------
echo "==> Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y

# ---------------------------------------------------------------------------
# 3. Core packages + enable SSH.
# ---------------------------------------------------------------------------
echo "==> Installing core packages..."
apt-get install -y openssh-server tmux curl ca-certificates python3 jq
systemctl enable --now ssh

# ---------------------------------------------------------------------------
# 4. Disable sleep/suspend (headless always-on box).
# ---------------------------------------------------------------------------
echo "==> Masking sleep targets..."
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target \
    >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 5. Swapfile (RAM is tight on this box).
# ---------------------------------------------------------------------------
echo "==> Configuring ${SWAP_SIZE_GB}G swapfile..."
if swapon --show | grep -q "${SWAPFILE}"; then
    echo "    swap already active; skipping creation."
elif [[ -f "${SWAPFILE}" ]]; then
    echo "    ${SWAPFILE} exists but is not active; enabling it."
    swapon "${SWAPFILE}" || true
else
    # ext4 OS drive, so fallocate for swap is fine (no btrfs special-casing).
    if ! fallocate -l "${SWAP_SIZE_GB}G" "${SWAPFILE}"; then
        echo "    fallocate failed; falling back to dd (slower)..."
        dd if=/dev/zero of="${SWAPFILE}" bs=1M count=$((SWAP_SIZE_GB * 1024)) status=progress
    fi
    chmod 600 "${SWAPFILE}"
    mkswap "${SWAPFILE}"
    swapon "${SWAPFILE}"
fi

# Persist in fstab if not already present.
if ! grep -qE "^\s*${SWAPFILE}\s" /etc/fstab; then
    echo "${SWAPFILE} none swap sw 0 0" >> /etc/fstab
    echo "    added ${SWAPFILE} to /etc/fstab."
fi

# Prefer RAM; only swap when needed.
SWAPPINESS_CONF="/etc/sysctl.d/99-nas-swappiness.conf"
if [[ ! -f "${SWAPPINESS_CONF}" ]]; then
    echo "vm.swappiness=10" > "${SWAPPINESS_CONF}"
    sysctl -p "${SWAPPINESS_CONF}" >/dev/null || true
    echo "    set vm.swappiness=10."
fi

# ---------------------------------------------------------------------------
# 6. Tailscale (install only; `tailscale up` is interactive -- user runs it).
# ---------------------------------------------------------------------------
echo "==> Installing Tailscale..."
if command -v tailscale >/dev/null 2>&1; then
    echo "    tailscale already installed; skipping."
else
    curl -fsSL https://tailscale.com/install.sh | sh
fi

# ---------------------------------------------------------------------------
# 7. MOTD login banner (idempotent; overwrite is fine).
# ---------------------------------------------------------------------------
echo "==> Installing MOTD banner..."
if [[ -f "${REPO_ROOT}/motd/${MOTD_SRC_NAME}" ]]; then
    cp "${REPO_ROOT}/motd/${MOTD_SRC_NAME}" /etc/motd
    echo "    banner -> /etc/motd"
fi

# ---------------------------------------------------------------------------
# 8. Notifier (optional).
# ---------------------------------------------------------------------------
echo ""
read -r -p "Install the Telegram boot-notifier now? [y/N] " answer
case "${answer}" in
    [yY]|[yY][eE][sS])
        "${SCRIPT_DIR}/setup-notifier.sh" "${TARGET_USER}"
        ;;
    *)
        echo "    Skipped. Run scripts/setup-notifier.sh later to add it."
        ;;
esac

# ---------------------------------------------------------------------------
# 9. Final summary.
# ---------------------------------------------------------------------------
cat <<EOF

=== TurtlEngine install complete ===

Next steps (these stay manual on purpose):

  1. Bring up Tailscale (interactive browser login):
       sudo tailscale up

  2. From your laptop/phone, copy your SSH key to this box:
       ssh-copy-id ${TARGET_USER}@<nas-ip>

  3. Start your persistent work session:
       tmux new -s main

  Reconnect from anywhere later with:
       ssh ${TARGET_USER}@<ip> -t tmux attach -t main

See README.md for the full walkthrough and security notes.
EOF
