#!/usr/bin/env bash
#
# setup-bot.sh -- install & enable the TurtlEngine Telegram command bot (v1).
#
# The bot answers a fixed set of READ-ONLY commands from one authorised Telegram
# account. It reuses the notifier's bot token, but runs as its OWN hardened
# systemd service as the box owner (NON-root) -- the boot notifier is untouched.
#
# Requires setup-notifier.sh to have been run first (for the token + chat id).
#
# Usage: sudo ./scripts/setup-bot.sh [owner-username]
#   owner-username  the account the bot runs as (default: $SUDO_USER). The bot
#                   runs as this user so it can read that user's tmux socket.

set -euo pipefail

# Overridable so tests can redirect to throwaway paths.
INSTALL_DIR="${INSTALL_DIR:-/opt/turtlengine/notifier}"   # alongside notify.py
BOT_ENV_PATH="${BOT_ENV_PATH:-/etc/turtlebot.env}"
NOTIFIER_ENV_PATH="${NOTIFIER_ENV_PATH:-/etc/nas-notifier.env}"
UNIT_PATH="${UNIT_PATH:-/etc/systemd/system/turtlebot.service}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This needs root. Please run it with sudo:  sudo $0" >&2
    exit 1
fi

OWNER="${1:-${SUDO_USER:-}}"
if [[ -z "${OWNER}" ]]; then
    echo "Could not determine the owner user; pass one as the first argument." >&2
    exit 1
fi
if ! id "${OWNER}" >/dev/null 2>&1; then
    echo "User ${OWNER} does not exist." >&2
    exit 1
fi
if [[ "${OWNER}" == "root" ]]; then
    echo "WARNING: the bot would run as root. It is designed to run as a" >&2
    echo "         non-root owner. Re-run as 'sudo ./scripts/setup-bot.sh <user>'" >&2
    echo "         (or 'sudo turtlengine bot setup') to run it as that user." >&2
fi

# --- helpers ----------------------------------------------------------------
ask_yes() {
    local prompt="$1" def="${2:-N}" ans hint
    [[ "${def}" =~ ^[Yy]$ ]] && hint="[Y/n]" || hint="[y/N]"
    read -r -p "  ${prompt} ${hint} " ans || ans=""
    ans="${ans:-${def}}"
    [[ "${ans}" =~ ^[Yy]([eE][sS])?$ ]]
}

env_from() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n1; }

# Pull a sender's numeric user id from the bot's recent updates.
fetch_user_id() {
    local tok="$1" json
    [[ -n "${BOT_SKIP_AUTODETECT:-}" ]] && return 0
    command -v curl >/dev/null 2>&1 || return 0
    json="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${tok}/getUpdates" 2>/dev/null || true)"
    [[ -z "${json}" ]] && return 0
    python3 -c '
import sys, json
try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
uid = ""
for u in data.get("result", []):
    frm = ((u.get("message") or u.get("edited_message") or {}).get("from") or {})
    if "id" in frm:
        uid = frm["id"]
print(uid)
' "${json}" 2>/dev/null || true
}

# --- 1. Require the notifier to be configured (reuse its token + chat id) ----
if [[ ! -f "${NOTIFIER_ENV_PATH}" ]]; then
    echo "The notifier isn't set up yet. Run this first, then re-run me:" >&2
    echo "    sudo ./scripts/setup-notifier.sh" >&2
    exit 1
fi
TOKEN="$(env_from TELEGRAM_BOT_TOKEN "${NOTIFIER_ENV_PATH}")"
CHAT_ID="$(env_from TELEGRAM_CHAT_ID "${NOTIFIER_ENV_PATH}")"
SESSION="$(env_from TMUX_SESSION "${NOTIFIER_ENV_PATH}")"; SESSION="${SESSION:-main}"
if [[ -z "${TOKEN}" || "${TOKEN}" == *ExampleToken* || -z "${CHAT_ID}" ]]; then
    echo "The notifier token/chat id aren't configured. Run setup-notifier.sh first." >&2
    exit 1
fi

echo "Setting up the TurtlEngine command bot (runs as: ${OWNER}, non-root)."

# --- 2. Install bot.py alongside notify.py ----------------------------------
# Install both as a matched pair (bot.py imports notify.py at runtime). Skip any
# file whose source already IS the destination -- this happens when the script
# runs from the staged /opt copy (e.g. `turtlengine bot setup`), where `install`
# would otherwise error on same-file.
install -d -m 0755 "${INSTALL_DIR}"
for p in notify.py bot.py; do
    if [[ "${REPO_ROOT}/notifier/${p}" -ef "${INSTALL_DIR}/${p}" ]]; then
        echo "  ${p} already in place (${INSTALL_DIR})."
    elif [[ -f "${REPO_ROOT}/notifier/${p}" ]]; then
        install -m 0755 "${REPO_ROOT}/notifier/${p}" "${INSTALL_DIR}/${p}"
        echo "  Installed ${p} -> ${INSTALL_DIR}/${p}"
    else
        echo "ERROR: ${REPO_ROOT}/notifier/${p} not found." >&2
        exit 1
    fi
done

# --- 3. Determine and CONFIRM the authorised Telegram user id ---------------
# Only this numeric id (in the configured chat) may command the bot. The owner
# must confirm it before the bot is enabled.
echo ""
echo "  The bot will ONLY respond to one Telegram account, in chat ${CHAT_ID}."
echo "  Make sure you've sent your bot a message (e.g. /start) so it can see you."
DETECTED="$(fetch_user_id "${TOKEN}")"
DEFAULT_ID="${DETECTED:-${CHAT_ID}}"
echo ""
echo "  Your Telegram user id appears to be: ${DEFAULT_ID}"
read -r -p "  Authorise this id? Press Enter to accept, type a different id, or 'n' to cancel: " reply || reply=""
case "${reply}" in
    n|N)      echo "Cancelled; nothing was enabled." >&2; exit 1 ;;
    "")       AUTH_ID="${DEFAULT_ID}" ;;
    *[!0-9]*) echo "That isn't a numeric id; cancelled." >&2; exit 1 ;;
    *)        AUTH_ID="${reply}" ;;
esac
if [[ -z "${AUTH_ID}" ]]; then
    echo "No id to authorise; cancelled." >&2
    exit 1
fi

# --- 4. Write /etc/turtlebot.env, owned by the owner, chmod 600 -------------
umask 077
cat > "${BOT_ENV_PATH}" <<EOF
# TurtlEngine command bot config. Owned by ${OWNER}, chmod 600. Never commit.
TELEGRAM_BOT_TOKEN=${TOKEN}
TELEGRAM_CHAT_ID=${CHAT_ID}
AUTHORIZED_USER_ID=${AUTH_ID}
SSH_USER=${OWNER}
TMUX_SESSION=${SESSION}
EOF
chown "${OWNER}:${OWNER}" "${BOT_ENV_PATH}" 2>/dev/null || chown "${OWNER}" "${BOT_ENV_PATH}"
chmod 600 "${BOT_ENV_PATH}"
echo "  Wrote ${BOT_ENV_PATH} (owner ${OWNER}, chmod 600)."

# --- 5. Install the unit (filling in the run-as user) and enable it ---------
tmp_unit="$(mktemp)"
sed "s/__TURTLE_USER__/${OWNER}/" "${REPO_ROOT}/systemd/turtlebot.service" > "${tmp_unit}"
install -D -m 0644 "${tmp_unit}" "${UNIT_PATH}"
rm -f "${tmp_unit}"
systemctl daemon-reload
if [[ -n "${BOT_SKIP_ENABLE:-}" ]]; then
    echo "  Unit installed -> ${UNIT_PATH} (not started: BOT_SKIP_ENABLE set)."
else
    systemctl enable turtlebot.service >/dev/null 2>&1 || true
    # restart (not just enable --now): on a re-run the service is already
    # active, and --now would leave the old process running the previous
    # bot.py. restart makes the reinstalled code take effect; fall back to
    # start if it wasn't running yet.
    systemctl restart turtlebot.service >/dev/null 2>&1 \
        || systemctl start turtlebot.service >/dev/null 2>&1 || true
    echo "  Unit installed and (re)started -> ${UNIT_PATH}"
fi

cat <<EOF

The command bot is live. From Telegram, send your bot:
  /help          list commands
  /status        full status card
  /ip            current LAN + Tailscale addresses
  /drives        just the shared-drive names
  /shares        shared drives + connect commands for every OS
  /add_share     how to add a new shared drive
  /gpu /top /temp  GPU, busiest processes, temperatures
  /services /net   listening services + their URLs, Tailscale devices online
  /new_terminal  a copy-paste command to open another terminal (or add a name)
  /ssh_local /ssh_remote /storage /sessions /logs /ping

Type "/" in the chat to pick any command from the menu (it's registered
automatically when the bot starts).

It answers only your account (id ${AUTH_ID}) in chat ${CHAT_ID}. It is read-only:
it only reports status or hands you a command to run yourself, and never changes
anything on the box. Manage it with:
  turtlengine bot status | logs | restart
EOF
