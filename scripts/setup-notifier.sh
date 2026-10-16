#!/usr/bin/env bash
#
# setup-notifier.sh -- install the TurtlEngine Telegram boot-notifier.
#
# Copies notify.py to a stable system path, seeds /etc/nas-notifier.env from the
# example (chmod 600), and installs + enables the systemd unit. Idempotent:
# safe to re-run; an existing env file is left untouched.
#
# Usage: sudo ./scripts/setup-notifier.sh [username]
#   username  account shown as SSH_USER in the notifier message.
#             Defaults to $SUDO_USER (the user behind sudo).

set -euo pipefail

INSTALL_DIR="/opt/turtlengine/notifier"
ENV_PATH="/etc/nas-notifier.env"
UNIT_PATH="/etc/systemd/system/nas-notifier.service"

# Resolve the repo root from this script's own location, so it works from
# anywhere (install.sh calls it too).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "This script must run as root (use sudo)." >&2
        exit 1
    fi
}

require_root

# Operate for the invoking user, not root.
TARGET_USER="${1:-${SUDO_USER:-}}"
if [[ -z "${TARGET_USER}" ]]; then
    echo "Could not determine target user; pass one as the first argument." >&2
    exit 1
fi

echo "==> Installing notifier for user: ${TARGET_USER}"

# 1. Copy notify.py to the stable system path the unit's ExecStart references.
install -d -m 0755 "${INSTALL_DIR}"
install -m 0755 "${REPO_ROOT}/notifier/notify.py" "${INSTALL_DIR}/notify.py"
echo "    notify.py -> ${INSTALL_DIR}/notify.py"

# 2. Seed the env file from the example if it does not already exist.
if [[ -f "${ENV_PATH}" ]]; then
    echo "    ${ENV_PATH} already exists; leaving it untouched."
else
    cp "${REPO_ROOT}/notifier/nas-notifier.env.example" "${ENV_PATH}"
    # Pre-fill SSH_USER with the target user.
    if grep -q '^SSH_USER=' "${ENV_PATH}"; then
        sed -i "s/^SSH_USER=.*/SSH_USER=${TARGET_USER}/" "${ENV_PATH}"
    else
        echo "SSH_USER=${TARGET_USER}" >> "${ENV_PATH}"
    fi
    chmod 600 "${ENV_PATH}"
    echo "    created ${ENV_PATH} (chmod 600, SSH_USER=${TARGET_USER})"
fi

# 3. Install and enable the systemd unit.
install -m 0644 "${REPO_ROOT}/systemd/nas-notifier.service" "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable nas-notifier.service >/dev/null
echo "    unit installed and enabled -> ${UNIT_PATH}"

cat <<EOF

Notifier installed. Next steps:

  1. Fill in your bot token and chat id:
       sudo nano ${ENV_PATH}
     (See the comments in that file, or notifier/nas-notifier.env.example,
      for how to get each value from @BotFather and getUpdates.)

  2. Test it without rebooting:
       sudo systemctl start nas-notifier.service

  3. Read the logs:
       journalctl -u nas-notifier.service -b

It will now also run automatically on every boot.
EOF
