#!/usr/bin/env bash
#
# install.sh -- TurtlEngine main provisioning.
#
# Turns a fresh Ubuntu 26.04 Desktop install into an always-on headless dev box.
# Idempotent: every step checks state before acting, so it is safe to re-run
# (the same repo will later provision a second machine too).
#
# Usage: sudo ./scripts/install.sh

set -euo pipefail

MOTD_SRC_NAME="turtlengine.txt"
OPT_DIR="/opt/turtlengine"
CLI_LINK="/usr/local/bin/turtlengine"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Banner -- printed first, before any provisioning work, as TurtlEngine
# branding. Kept byte-identical to motd/turtlengine.txt and the README header.
# ---------------------------------------------------------------------------
print_banner() {
    cat <<'BANNER'

                        __
             .,-;-;-,. /'_\
           _/_/_/_|_\_\) /
         '-<_><_><_><_>=/\
           `/_/====/_/-'\_\
            ""     ""    ""

   ╔════════════════════════════╗
   ║   T u r t l E n g i n e    ║
   ╚════════════════════════════╝
   slow is smooth, smooth is fast

BANNER
}

print_banner

# ---------------------------------------------------------------------------
# Guard: require root, detect the invoking user.
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
# 1. System update.
# ---------------------------------------------------------------------------
echo "==> Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y

# ---------------------------------------------------------------------------
# 2. Core packages + enable SSH.
# ---------------------------------------------------------------------------
echo "==> Installing core packages..."
apt-get install -y openssh-server tmux curl ca-certificates python3 jq
systemctl enable --now ssh

# ---------------------------------------------------------------------------
# 3. Disable sleep/suspend (headless always-on box).
# ---------------------------------------------------------------------------
echo "==> Masking sleep targets..."
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target \
    >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 4. Tailscale (install only; `tailscale up` is interactive -- user runs it).
# ---------------------------------------------------------------------------
echo "==> Installing Tailscale..."
if command -v tailscale >/dev/null 2>&1; then
    echo "    tailscale already installed; skipping."
else
    curl -fsSL https://tailscale.com/install.sh | sh
fi

# ---------------------------------------------------------------------------
# 5. MOTD login banner (idempotent; overwrite is fine).
# ---------------------------------------------------------------------------
echo "==> Installing MOTD banner..."
if [[ -f "${REPO_ROOT}/motd/${MOTD_SRC_NAME}" ]]; then
    cp "${REPO_ROOT}/motd/${MOTD_SRC_NAME}" /etc/motd
    echo "    banner -> /etc/motd"
fi

# ---------------------------------------------------------------------------
# 6. Install the turtlengine CLI + helper scripts to a stable system path.
#    No disks are formatted/mounted and no shares are created here -- the CLI
#    is how the user configures storage and shares later, on their terms.
# ---------------------------------------------------------------------------
echo "==> Installing the turtlengine CLI..."
install -d -m 0755 "${OPT_DIR}/scripts"
for s in turtlengine mount-drive.sh setup-samba.sh setup-notifier.sh; do
    if [[ -f "${REPO_ROOT}/scripts/${s}" ]]; then
        install -m 0755 "${REPO_ROOT}/scripts/${s}" "${OPT_DIR}/scripts/${s}"
    fi
done
ln -sf "${OPT_DIR}/scripts/turtlengine" "${CLI_LINK}"
echo "    turtlengine -> ${CLI_LINK}"

# ---------------------------------------------------------------------------
# 7. Notifier (optional).
# ---------------------------------------------------------------------------
echo ""
# `|| answer=""` keeps `set -e` from aborting the script on EOF (e.g. when
# install.sh is run non-interactively); an empty answer falls through to skip.
read -r -p "Install the Telegram boot-notifier now? [y/N] " answer || answer=""
NOTIFIER_INSTALLED="no"
case "${answer}" in
    [yY]|[yY][eE][sS])
        "${SCRIPT_DIR}/setup-notifier.sh" "${TARGET_USER}"
        NOTIFIER_INSTALLED="yes"
        ;;
    *)
        echo "    Skipped. Run scripts/setup-notifier.sh later to add it."
        ;;
esac

# ---------------------------------------------------------------------------
# 8. Final summary -- what was done automatically vs. what you still must do.
# ---------------------------------------------------------------------------
if [[ "${NOTIFIER_INSTALLED}" == "yes" ]]; then
    notifier_done="the boot-notifier service is installed and enabled (not yet configured)"
else
    notifier_done="the boot-notifier was NOT installed"
fi

cat <<EOF

=== TurtlEngine install complete ===

Done automatically on this box:
  - SSH server installed and enabled (reachable now)
  - tmux + core tools installed
  - sleep/suspend/hibernate masked (stays awake)
  - Tailscale installed (NOT yet logged in)
  - login banner set
  - the 'turtlengine' CLI installed (${CLI_LINK})
  - ${notifier_done}

No disks were formatted or mounted, and no network shares were created.
Use 'sudo turtlengine' to configure storage and shares when you're ready.

Remaining manual steps (nothing below has been done for you):

  1. Tailscale -- enables private remote access from anywhere:
       sudo tailscale up
     Complete the interactive login it prints. Nothing is exposed to the
     public internet; access rides your tailnet.

  2. SSH key access -- from your laptop/phone (the CLIENT, not this box):
       ssh-copy-id ${TARGET_USER}@<nas-ip>
     Verify you can log in with your key BEFORE hardening SSH. Only once key
     login works, disable password auth: set 'PasswordAuthentication no' in
     /etc/ssh/sshd_config, then 'sudo systemctl restart ssh'. (Not scripted --
     doing it too early can lock you out.)

  3. Telegram notifier -- a paste-ready reconnect command on every boot:
       - In Telegram, message @BotFather, send /newbot, copy the TOKEN.
       - Message your new bot once, then open
           https://api.telegram.org/bot<TOKEN>/getUpdates
         and read "chat":{"id":<number>} -- that's your CHAT ID.
       - If you didn't install it above: sudo ./scripts/setup-notifier.sh
       - Put the values in /etc/nas-notifier.env (root-only, chmod 600):
           sudo nano /etc/nas-notifier.env

  4. Test the notifier (no reboot needed):
       sudo systemctl start nas-notifier.service
       journalctl -u nas-notifier.service -b
     Confirm the Telegram message arrived and its ssh commands are usable.

  5. Start your persistent work session (attaches, or creates if absent):
       tmux new-session -A -s main
     Reconnect from anywhere later with:
       ssh ${TARGET_USER}@<ip> -t tmux new-session -A -s main

See README.md for the full walkthrough and security notes.
EOF
