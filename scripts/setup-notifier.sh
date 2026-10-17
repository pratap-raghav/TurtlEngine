#!/usr/bin/env bash
#
# setup-notifier.sh -- install AND configure the TurtlEngine Telegram notifier.
#
# Installs notify.py + the systemd unit, then walks you through entering your
# Telegram credentials: you create the bot in the Telegram app (@BotFather) and
# paste the token; the chat id is auto-detected from the bot's recent messages
# (with a manual fallback). A test send is offered. If credentials already exist
# it asks before replacing them. Idempotent and safe to re-run.
#
# For bot creation it opens @BotFather via telegram.me (best effort) -- that
# alias often loads where t.me is blocked, and the Telegram app always works.
# The chat id is fetched from the Bot API directly.
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

open_url() { # best-effort: open in the invoking user's browser; always print it
    local url="$1" uid
    echo "      ${url}"
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
}

fetch_chat_id() { # best-effort: read the chat id from the bot's recent updates
    local tok="$1" json
    # Tests set this to stay offline; also skip if we have no curl.
    [[ -n "${NOTIFIER_SKIP_AUTODETECT:-}" ]] && return 0
    command -v curl >/dev/null 2>&1 || return 0
    # Any TLS/HTTP failure -- including an ISP block page with a mismatched cert
    # -- yields empty output, so we fall back to manual entry cleanly.
    json="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${tok}/getUpdates" 2>/dev/null || true)"
    [[ -z "${json}" ]] && return 0
    python3 -c '
import sys, json
try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
ids = []
for u in data.get("result", []):
    chat = ((u.get("message") or u.get("edited_message") or {}).get("chat") or {})
    if "id" in chat:
        ids.append(chat["id"])
print(ids[-1] if ids else "")
' "${json}" 2>/dev/null || true
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
    2) your CHAT ID  -- detected automatically once you've pressed START
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
echo "  Step 1 - create your bot and copy its token."
echo "  Start a chat with @BotFather and create your bot:"
echo "    - send /newbot and follow the prompts (a name, then a username),"
echo "    - copy the token it replies with (looks like 123456789:AA...),"
echo "    - then open your new bot and press START (so it can message you later)."
echo "  Opening BotFather for you (telegram.me usually loads even where t.me is"
echo "  blocked; you can also just open the Telegram app and search BotFather):"
open_url "https://telegram.me/BotFather"
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
echo "  Step 2 - reading your chat id from Telegram (no need to look it up)."
# The token alone is enough to call getUpdates; do it right away.
chat_id="$(fetch_chat_id "${token}")"
if [[ -n "${chat_id}" ]]; then
    echo "  Found it -- your chat id is ${chat_id}."
    ask_yes "Use this chat id?" "Y" || chat_id=""
fi
# Nothing came back: the bot has no message from you yet (or Telegram is
# unreachable). Guide to START and retry, allow a manual id, or skip. The loop
# ends on EOF, so it never blocks when run non-interactively.
while [[ -z "${chat_id}" ]]; do
    echo ""
    echo "  Nothing yet. In Telegram, open your new bot and press START (or send"
    echo "  /start) -- a bot can only message you after you've messaged it first."
    echo "  Then press Enter to retry. Or type your chat id (e.g. from"
    echo "  @userinfobot), or 's' to skip. Manual lookup, if you prefer:"
    echo "    curl -s \"https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates\" | jq '.result[].message.chat.id'"
    read -r -p "  [Enter = retry / <number> / s = skip] " ans || ans="s"
    case "${ans}" in
        s|S) break ;;
        "")
            chat_id="$(fetch_chat_id "${token}")"
            if [[ -n "${chat_id}" ]]; then
                echo "  Found it -- your chat id is ${chat_id}."
                ask_yes "Use this chat id?" "Y" || chat_id=""
            fi
            ;;
        *) chat_id="${ans}" ;;
    esac
done
if [[ -z "${chat_id}" ]]; then
    echo ""
    echo "  No chat id set -- your token was not saved. Re-run when ready:"
    echo "      sudo ./scripts/setup-notifier.sh"
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
