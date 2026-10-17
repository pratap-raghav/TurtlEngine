#!/usr/bin/env bash
#
# install.sh -- TurtlEngine guided setup.
#
# Turns a fresh Ubuntu 26.04 Desktop install into an always-on headless server
# you can reach from anywhere. It reads like a friendly setup assistant: each
# phase reports plainly what it is doing, noisy command output goes to a log
# (use --verbose to watch it live), and the box is never changed destructively
# without asking.
#
# Idempotent: every step checks state first, so it is safe to re-run.
#
# Usage: sudo ./scripts/install.sh [--verbose]

set -euo pipefail

MOTD_SRC_NAME="turtlengine.txt"
OPT_DIR="/opt/turtlengine"
CLI_LINK="/usr/local/bin/turtlengine"
LOG_FILE="/var/log/turtlengine-install.log"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERBOSE=0
for arg in "$@"; do
    case "${arg}" in
        -v|--verbose) VERBOSE=1 ;;
        -h|--help)
            echo "Usage: sudo $0 [--verbose]"
            echo "  --verbose   show full command output instead of a tidy summary"
            exit 0
            ;;
        *) echo "Unknown option: ${arg} (try --help)" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Banner -- printed first, before any work, as TurtlEngine branding.
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
# Presentation helpers (no system changes happen in here).
# ---------------------------------------------------------------------------
phase() { # phase <n> <total> <title> [subtitle]
    printf '\n[%s/%s]  %s\n' "$1" "$2" "$3"
    [[ -n "${4:-}" ]] && printf '        %s\n' "$4"
    return 0
}

fail_step() { # fail_step <human description>
    echo ""
    echo "  ✗  $1 did not succeed."
    echo ""
    echo "     The full output of this step was saved to:"
    echo "         ${LOG_FILE}"
    echo ""
    echo "     This is usually a dropped network connection or another package"
    echo "     manager still holding a lock. Sort that out, then run this"
    echo "     installer again -- it is safe to re-run and skips finished work."
    echo ""
    exit 1
}

run_step() { # run_step "done message" command [args...]
    local msg="$1"; shift
    if [[ "${VERBOSE}" -eq 1 ]]; then
        echo "        -> ${msg}"
        "$@" 2>&1 | tee -a "${LOG_FILE}"
        local vrc=${PIPESTATUS[0]}
        [[ ${vrc} -eq 0 ]] && echo "        done" || echo "        FAILED"
        return "${vrc}"
    fi

    "$@" >>"${LOG_FILE}" 2>&1 &
    local pid=$! rc=0
    if [[ -t 1 ]]; then
        local chars='|/-\' i=0
        while kill -0 "${pid}" 2>/dev/null; do
            i=$(((i + 1) % 4))
            printf '\r        %s  %s' "${chars:${i}:1}" "${msg}"
            sleep 0.2
        done
        printf '\r\033[K'
    fi
    if wait "${pid}"; then rc=0; else rc=$?; fi
    [[ ${rc} -eq 0 ]] && echo "        ✓  ${msg}" || echo "        ✗  ${msg}"
    return "${rc}"
}

ask_yes() { # ask_yes "question" <Y|N default> -> 0 on yes
    local prompt="$1" def="${2:-N}" ans hint
    [[ "${def}" =~ ^[Yy]$ ]] && hint="[Y/n]" || hint="[y/N]"
    read -r -p "  ${prompt} ${hint} " ans || ans=""
    ans="${ans:-${def}}"
    [[ "${ans}" =~ ^[Yy]([eE][sS])?$ ]]
}

# ---------------------------------------------------------------------------
# Guard: require root, detect the invoking user.
# ---------------------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
    echo "This installer needs root. Please run it with sudo:"
    echo "    sudo $0"
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "${TARGET_USER}" ]]; then
    echo "Note: could not detect the user behind sudo; continuing as root." >&2
    TARGET_USER="root"
fi

: > "${LOG_FILE}" 2>/dev/null || LOG_FILE="/tmp/turtlengine-install.log"

cat <<EOF
Welcome to TurtlEngine.

This prepares ${TARGET_USER}'s machine as an always-on server you can reach from
anywhere -- SSH access, a persistent tmux session, Tailscale for private remote
access, and (optionally) a Telegram message with a reconnect link on every boot.

Nothing destructive happens here: no disk is formatted, mounted, or shared
during setup. Your storage stays exactly as it is.

Detailed output is saved to ${LOG_FILE} (re-run with --verbose to watch it live).
EOF

# ---------------------------------------------------------------------------
# Work steps. Defined here (after the banner call) so the installer reads top
# to bottom; each is run through run_step so output stays tidy.
# ---------------------------------------------------------------------------
step_update() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get upgrade -y
}

step_core() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y openssh-server tmux curl ca-certificates python3 jq
    systemctl enable --now ssh
}

step_power() {
    systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
}

step_tailscale() {
    if command -v tailscale >/dev/null 2>&1; then
        echo "tailscale already installed; skipping."
        return 0
    fi
    curl -fsSL https://tailscale.com/install.sh | sh
}

step_base() {
    # Login banner.
    if [[ -f "${REPO_ROOT}/motd/${MOTD_SRC_NAME}" ]]; then
        cp "${REPO_ROOT}/motd/${MOTD_SRC_NAME}" /etc/motd
    fi
    # The turtlengine CLI + its helper scripts, to a stable system path. No disk
    # is touched and no share is created -- the CLI is how you do that later, on
    # your terms.
    install -d -m 0755 "${OPT_DIR}/scripts"
    local s
    for s in turtlengine mount-drive.sh setup-samba.sh setup-notifier.sh; do
        if [[ -f "${REPO_ROOT}/scripts/${s}" ]]; then
            install -m 0755 "${REPO_ROOT}/scripts/${s}" "${OPT_DIR}/scripts/${s}"
        fi
    done
    ln -sf "${OPT_DIR}/scripts/turtlengine" "${CLI_LINK}"
}

# ---------------------------------------------------------------------------
# Run the install phases.
# ---------------------------------------------------------------------------
phase 1 6 "Updating the system" "Fetching the latest security and software updates."
run_step "System is up to date" step_update || fail_step "The system update"

phase 2 6 "Installing essential tools" "SSH, tmux, and the utilities the server needs."
run_step "Tools installed, SSH enabled" step_core || fail_step "Installing the core tools"

phase 3 6 "Configuring power settings" "Keeping the machine awake (no sleep or hibernate)."
run_step "Sleep and hibernate disabled" step_power || fail_step "Adjusting power settings"

phase 4 6 "Installing Tailscale" "Software for private remote access (sign-in comes later)."
run_step "Tailscale installed" step_tailscale || fail_step "Installing Tailscale"

phase 5 6 "Finishing the base system" "Login banner and the 'turtlengine' management CLI."
run_step "Banner and CLI installed" step_base || fail_step "Installing the CLI and banner"

# ---------------------------------------------------------------------------
# Phase 6: boot notifications. setup-notifier.sh installs the service AND walks
# you through entering your Telegram credentials (opening the right web pages).
# ---------------------------------------------------------------------------
phase 6 6 "Setting up boot notifications" "Optional: a Telegram reconnect link on every boot."
echo ""
NOTIFIER_INSTALLED="no"
if ask_yes "Set up Telegram boot notifications now?" "Y"; then
    echo ""
    if "${SCRIPT_DIR}/setup-notifier.sh" "${TARGET_USER}"; then
        NOTIFIER_INSTALLED="yes"
    else
        echo ""
        echo "  Notifier setup didn't finish cleanly. You can run it again later:"
        echo "      sudo ./scripts/setup-notifier.sh"
    fi
else
    echo "  Skipped. You can enable Telegram notifications anytime with:"
    echo "      sudo ./scripts/setup-notifier.sh"
fi

# ---------------------------------------------------------------------------
# Guided Tailscale sign-in -- the next thing after Telegram, as requested.
# ---------------------------------------------------------------------------
echo ""
echo "────────────────────────────────────────────────────────────"
echo "  Connect this machine to Tailscale (private remote access)"
echo "────────────────────────────────────────────────────────────"
echo ""
echo "  Tailscale lets you reach this box from anywhere without opening any"
echo "  ports to the internet. Signing in is interactive -- it shows a link you"
echo "  approve in a browser (it may also open one for you)."
echo ""
TAILSCALE_CONNECTED="no"
if ask_yes "Sign in to Tailscale now?" "Y"; then
    echo ""
    echo "  Follow the link below to authorize this machine, then come back here."
    echo ""
    if tailscale up; then
        TAILSCALE_CONNECTED="yes"
        echo ""
        echo "  Connected to your tailnet."
    else
        echo ""
        echo "  Sign-in didn't complete -- no problem. Do it anytime with:"
        echo "      sudo tailscale up"
    fi
else
    echo "  Skipped. Connect later with:  sudo tailscale up"
fi

# ---------------------------------------------------------------------------
# Completion summary -- the state of the machine at a glance.
# ---------------------------------------------------------------------------
if [[ "${TAILSCALE_CONNECTED}" == "yes" ]]; then
    ts_state="installed and connected"
else
    ts_state="installed -- sign in with: sudo tailscale up"
fi
if [[ "${NOTIFIER_INSTALLED}" == "yes" ]]; then
    notifier_mark="✓"
    notifier_state="installed and enabled"
else
    notifier_mark="·"
    notifier_state="not installed (run: sudo ./scripts/setup-notifier.sh)"
fi

cat <<EOF

════════════════════════════════════════════════════════════
  TurtlEngine is ready
════════════════════════════════════════════════════════════

What's set up:
  ✓  SSH remote access     installed and enabled (reachable now)
  ✓  tmux + core tools     installed and ready
  ✓  Power management      sleep / suspend / hibernate disabled
  ✓  Tailscale             ${ts_state}
  ✓  TurtlEngine CLI       installed (run: sudo turtlengine)
  ${notifier_mark}  Telegram notifier     ${notifier_state}

Your storage is untouched:
  Nothing was erased, mounted, or shared. Set up disks and network
  shares whenever you're ready, on your terms:
      sudo turtlengine

Before you call it done:

  1. Set up SSH key login (run on your laptop/phone, NOT on this box):
         ssh-copy-id ${TARGET_USER}@<nas-ip>
     Confirm key login works, THEN harden SSH (see README security notes).
     Disabling password auth too early can lock you out, so it isn't scripted.

  2. Start your persistent work session (survives disconnects):
         tmux new-session -A -s main
     Reconnect from anywhere later with:
         ssh ${TARGET_USER}@<ip> -t tmux new-session -A -s main

  3. Configure disks and network shares when ready:
         sudo turtlengine
EOF

if [[ "${TAILSCALE_CONNECTED}" != "yes" ]]; then
    cat <<EOF

  •  Still to do: connect to Tailscale for remote access:
         sudo tailscale up
EOF
fi

cat <<EOF

See README.md for the full walkthrough and security notes.
Take your time -- your data is untouched.
════════════════════════════════════════════════════════════
EOF
