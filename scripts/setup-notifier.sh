#!/usr/bin/env bash
#
# setup-notifier.sh -- install AND configure the TurtlEngine Telegram notifier.
#
# Installs notify.py + the systemd unit, then walks you through entering your
# Telegram credentials: it opens @BotFather and the getUpdates page in your
# browser (falling back to printing the links), takes your token and chat id,
# and offers a test send. If credentials already exist it asks before replacing
# them. Idempotent and safe to re-run.
#
# Usage: sudo ./scripts/setup-notifier.sh [username]
#   username  account shown as SSH_USER in the message (default: $SUDO_USER).

set -euo pipefail

# Overridable so tests can redirect them to a throwaway directory.
INSTALL_DIR="${INSTALL_DIR:-/opt/turtlengine/notifier}"
ENV_PATH="${ENV_PATH:-/etc/nas-notifier.env}"
UNIT_PATH="${UNIT_PATH:-/etc/systemd/system/nas-notifier.service}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This needs root. Please run it with sudo:  sudo $0" >&2
    exit 1
fi

TARGET_USER="${1:-${SUDO_USER:-}}"
if [[ -z "${TARGET_USER}" ]]; then
    echo "Could not determine the target user; pass one as the first argument." >&2
    exit 1
fi

# --- helpers ----------------------------------------------------------------
ask_yes() { # ask_yes "question" <Y|N default> -> 0 on yes
    local prompt="$1" def="${2:-N}" ans hint
    [[ "${def}" =~ ^[Yy]$ ]] && hint="[Y/n]" || hint="[y/N]"
    read -r -p "  ${prompt} ${hint} " ans || ans=""
    ans="${ans:-${def}}"
    [[ "${ans}" =~ ^[Yy]([eE][sS])?$ ]]
}

open_url() { # best-effort: open in the user's browser, and always print it
    local url="$1" uid
    if [[ "${TARGET_USER}" != "root" ]] && command -v xdg-open >/dev/null 2>&1; then
        uid="$(id -u "${TARGET_USER}" 2>/dev/null || true)"
        if [[ -n "${uid}" ]]; then
            sudo -u "${TARGET_USER}" \
                DISPLAY="${DISPLAY:-:0}" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
                xdg-open "${url}" >/dev/null 2>&1 &
            disown 2>/dev/null || true
        fi
    fi
    echo "      ${url}"
}

env_value() { # env_value KEY -> current value in ENV_PATH (or empty)
    [[ -f "${ENV_PATH}" ]] || return 0
    sed -n "s/^$1=//p" "${ENV_PATH}" | head -n1
}

set_env_value() { # set_env_value KEY VALUE  (in place, preserving the rest)
    local key="$1" value="$2" esc
    esc="$(printf '%s' "${value}" | sed -e 's/[\\/&]/\\&/g')"
    if grep -q "^${key}=" "${ENV_PATH}"; then
        sed -i "s/^${key}=.*/${key}=${esc}/" "${ENV_PATH}"
    else
        printf '%s=%s\n' "${key}" "${value}" >> "${ENV_PATH}"
    fi
}

creds_configured() { # 0 if real (non-placeholder) credentials are present
    local tok cid
    tok="$(env_value TELEGRAM_BOT_TOKEN)"
    cid="$(env_value TELEGRAM_CHAT_ID)"
    [[ -n "${tok}" && -n "${cid}" && "${tok}" != *ExampleToken* ]]
}

# --- 1. Install the notifier program + systemd unit -------------------------
echo "Installing the boot notifier for user: ${TARGET_USER}"

install -d -m 0755 "${INSTALL_DIR}"
install -m 0755 "${REPO_ROOT}/notifier/notify.py" "${INSTALL_DIR}/notify.py"

# Create the config from the example on first run; pre-fill SSH_USER. An
# existing config is left in place (credentials are handled below).
if [[ ! -f "${ENV_PATH}" ]]; then
    cp "${REPO_ROOT}/notifier/nas-notifier.env.example" "${ENV_PATH}"
    if grep -q '^SSH_USER=' "${ENV_PATH}"; then
        sed -i "s/^SSH_USER=.*/SSH_USER=${TARGET_USER}/" "${ENV_PATH}"
    else
        echo "SSH_USER=${TARGET_USER}" >> "${ENV_PATH}"
    fi
    chmod 600 "${ENV_PATH}"
fi

install -D -m 0644 "${REPO_ROOT}/systemd/nas-notifier.service" "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable nas-notifier.service >/dev/null 2>&1 || true
echo "  Notifier installed and enabled to run on every boot."

# --- 2. Explain, then configure credentials ---------------------------------
cat <<'EOF'

  The notifier sends you a Telegram message after each boot with a ready-to-paste
  command to reconnect. To finish, it needs two things from Telegram:
    1) a BOT TOKEN   -- from @BotFather
    2) your CHAT ID  -- a number the bot can read from its own updates page
EOF

# If real credentials already exist, ask before replacing them.
if creds_configured; then
    echo ""
    echo "  Telegram credentials are already saved in ${ENV_PATH}."
    if ! ask_yes "Replace them with new credentials?" "N"; then
        echo "  Keeping the existing credentials."
        echo ""
        echo "  Done. Test anytime with:  sudo systemctl start nas-notifier.service"
        exit 0
    fi
fi

echo ""
echo "  Step 1 - get your bot token."
echo "  Opening @BotFather. In Telegram: send /newbot, choose a name and"
echo "  username, then copy the token it replies with."
open_url "https://t.me/BotFather"
echo ""
read -r -p "  Paste your bot token here (or press Enter to skip for now): " token || token=""
if [[ -z "${token}" ]]; then
    echo ""
    echo "  No token entered -- skipping for now. Add it later with:"
    echo "      sudo nano ${ENV_PATH}"
    echo "  then test:  sudo systemctl start nas-notifier.service"
    exit 0
fi

echo ""
echo "  Step 2 - get your chat id."
echo "  First send any message to your new bot in Telegram (so it may reply)."
echo "  Then read the number from the page opening now -- look for"
echo '  "chat":{"id":<number>} and copy just the <number>.'
open_url "https://api.telegram.org/bot${token}/getUpdates"
echo ""
read -r -p "  Paste your chat id here (or press Enter to skip for now): " chat_id || chat_id=""
if [[ -z "${chat_id}" ]]; then
    echo ""
    echo "  No chat id entered -- your token was not saved. Re-run this when you"
    echo "  have both values:  sudo ./scripts/setup-notifier.sh"
    exit 0
fi

# --- 3. Save and optionally test --------------------------------------------
set_env_value TELEGRAM_BOT_TOKEN "${token}"
set_env_value TELEGRAM_CHAT_ID "${chat_id}"
chmod 600 "${ENV_PATH}"
echo ""
echo "  Saved to ${ENV_PATH} (readable only by root)."

echo ""
if ask_yes "Send a test notification now?" "Y"; then
    echo "  Sending (this waits for connectivity, usually a few seconds)..."
    if systemctl start nas-notifier.service; then
        echo "  Done -- check your Telegram for the message."
    else
        echo "  The notifier ran but reported a problem."
    fi
    echo "  To see details:  journalctl -u nas-notifier.service -b"
else
    echo "  You can test anytime with:  sudo systemctl start nas-notifier.service"
fi
