#!/usr/bin/env python3
"""TurtlEngine Telegram command bot -- read-only remote control (v1).

A small long-polling bot that answers a fixed set of READ-ONLY commands from a
single authorised Telegram account. It shares helpers with notify.py (status,
IPs, system info, message escaping) and sends replies through the same Bot API.

Security model (deliberately small and auditable):
  - Separate from the boot notifier; runs as a NON-root user (the box owner),
    via its own hardened systemd unit.
  - An update is processed only if BOTH the sender's numeric user id AND the
    chat id match the configured allowlist. Everything else is ignored silently.
  - No shell is ever built from message text. Commands are a fixed dispatch
    table; there is no /exec and no dynamic service/command name.
  - Where the bot cannot read something (a tmux socket, the journal), it says so
    with a distinct state instead of failing the whole reply or pretending.
  - It never prints the token/env/secrets; /logs is scoped to the notifier unit
    and the token is redacted defensively.

Config: /etc/turtlebot.env (chmod 600), KEY=value lines:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, AUTHORIZED_USER_ID, SSH_USER, TMUX_SESSION
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Reuse the notifier's helpers, installed alongside this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify  # noqa: E402

BOT_ENV_PATH = "/etc/turtlebot.env"
POLL_TIMEOUT_S = 50          # Telegram long-poll hold time
ERROR_BACKOFF_S = 5
MAX_LOG_CHARS = 3500         # keep /logs within Telegram's message limit

REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs",
    "vfat", "exfat", "ntfs", "ntfs3", "reiserfs", "jfs",
}


# --- Telegram I/O -----------------------------------------------------------
def get_updates(token, offset=None, timeout=POLL_TIMEOUT_S):
    """Long-poll getUpdates; returns the list of updates (possibly empty)."""
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = "%s/bot%s/getUpdates?%s" % (
        notify.TELEGRAM_API, token, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=timeout + 10) as response:
        data = json.load(response)
    return data.get("result", []) if data.get("ok") else []


# --- authorisation (the security gate) --------------------------------------
def is_authorized(message, user_id, chat_id):
    """True only if BOTH the sender id and the chat id match the allowlist."""
    frm = message.get("from") or {}
    chat = message.get("chat") or {}
    return bool(
        user_id and chat_id
        and str(frm.get("id")) == str(user_id)
        and str(chat.get("id")) == str(chat_id)
    )


def parse_command(text):
    """'/ssh_local@TurtlEngineBot x' -> 'ssh_local' (lowercase); else ''."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split()[0][1:].split("@", 1)[0].lower()


# --- read-only data gatherers (permission-aware) ----------------------------
def get_storage():
    """List real mounted filesystems as (mount, used_gib, total_gib, pct, fs).

    One row per distinct filesystem: mounts that share an underlying device are
    collapsed to a single row at the shortest mount path. Without this, the bind
    mounts systemd sandboxing adds for us (ProtectSystem/ProtectHome bind /etc,
    /home, /usr, ... read-only) each show up in our mount namespace and the one
    root disk would otherwise render as half a dozen identical rows. Returns None
    only if /proc/mounts can't be read at all.
    """
    best = {}   # st_dev -> (mount, used_gib, total_gib, pct, fs)
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3 or parts[2] not in REAL_FS:
                    continue
                mount = parts[1].replace("\\040", " ")
                try:
                    key = os.stat(mount).st_dev
                    stat = os.statvfs(mount)
                except OSError:
                    continue
                total = stat.f_blocks * stat.f_frsize
                if total <= 0:
                    continue
                used = total - stat.f_bavail * stat.f_frsize
                gib = 1024 ** 3
                row = (mount, used / gib, total / gib,
                       int(round(used * 100 / total)), parts[2])
                prev = best.get(key)
                if prev is None or len(mount) < len(prev[0]):
                    best[key] = row
    except OSError:
        return None
    return list(best.values())


def get_sessions(user):
    """Return (state, names). state in: ok | none | denied | no_tmux | error."""
    cmd = ["tmux"]
    socket_path = notify.tmux_socket_for_user(user)
    if socket_path:
        cmd += ["-S", socket_path]
    cmd += ["list-sessions", "-F", "#{session_name}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return "no_tmux", []
    except subprocess.SubprocessError:
        return "error", []
    if result.returncode == 0:
        return "ok", [s for s in result.stdout.splitlines() if s.strip()]
    if "permission denied" in (result.stderr or "").lower():
        return "denied", []
    return "none", []   # no server running is the common non-zero case


def get_logs(token=None, lines=15):
    """Recent notifier logs, or 'denied', or None. Redacts the token if given."""
    cmd = ["journalctl", "-u", "nas-notifier.service",
           "-n", str(lines), "--no-pager", "-o", "short"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return "denied"
    out = result.stdout
    if token:
        out = out.replace(token, "***")
    return out


# --- command handlers (return MarkdownV2 text; Telegram-agnostic) -----------
def _ssh_command(user, ip, session):
    return "ssh %s@%s -t tmux new-session -A -s %s" % (user, ip, session)


def _copy(text):
    """Inline code span -- tap-to-copy on Telegram mobile, like BotFather's token.

    Fenced code blocks don't reliably copy on tap on mobile; a single-backtick
    inline span does. Use this for single-line commands the user will copy.
    """
    return "`%s`" % notify.md2_code_escape(text)


def _fenced(text):
    """MarkdownV2 fenced code block -- for multi-line display (tables, logs)."""
    return "```\n%s\n```" % notify.md2_code_escape(text)


def cmd_help(config):
    rows = [
        ("/status", "full status card"),
        ("/ip", "current LAN and Tailscale addresses"),
        ("/ssh_local", "SSH command for home / LAN"),
        ("/ssh_remote", "SSH command via Tailscale"),
        ("/storage", "disks and free space"),
        ("/sessions", "tmux sessions"),
        ("/logs", "recent notifier logs"),
        ("/ping", "check the bot is alive"),
        ("/notify", "resend the status card"),
        ("/help", "this list"),
    ]
    lines = ["🐢 *TurtlEngine bot* " + notify.md2_escape("(read-only)"), ""]
    for name, desc in rows:
        lines.append("`%s` — %s" % (name, notify.md2_escape(desc)))
    return "\n".join(lines)


def cmd_ping(config):
    host = socket.gethostname()
    up = notify.get_uptime() or "?"
    return "🐢 " + notify.md2_escape("pong — %s is up (uptime %s)" % (host, up))


def cmd_status(config):
    return notify.build_message(notify.gather_info(config))


def cmd_ip(config):
    lan = notify.get_lan_ip()
    ts = notify.get_tailscale_ip()
    lines = ["📡 *Addresses*", ""]
    lines.append("🏠 LAN: " + ("`%s`" % notify.md2_code_escape(lan)
                               if lan else notify.md2_escape("unavailable")))
    lines.append("🌍 Tailscale: " + ("`%s`" % notify.md2_code_escape(ts)
                                     if ts else notify.md2_escape("not connected")))
    return "\n".join(lines)


def cmd_ssh_local(config):
    user = config.get("SSH_USER") or "user"
    session = config.get("TMUX_SESSION") or "main"
    lan = notify.get_lan_ip()
    if not lan:
        return notify.md2_escape(
            "🏠 LAN address couldn't be determined — is the NAS on the network?")
    return "🏠 *Home / LAN*\n" + _copy(_ssh_command(user, lan, session))


def cmd_ssh_remote(config):
    user = config.get("SSH_USER") or "user"
    session = config.get("TMUX_SESSION") or "main"
    ts = notify.get_tailscale_ip()
    if not ts:
        return (notify.md2_escape(
            "🌍 Tailscale isn't connected. Run this on the NAS, then try again:")
            + "\n" + _copy("sudo tailscale up"))
    return "🌍 *Anywhere / Tailscale*\n" + _copy(_ssh_command(user, ts, session))


def cmd_storage(config):
    rows = get_storage()
    if rows is None:
        return notify.md2_escape("💾 Storage info unavailable.")
    if not rows:
        return notify.md2_escape("💾 No mounted data filesystems found.")
    table = "\n".join(
        "%-16s %5.0f/%-5.0f GiB %3d%% %s" % (m[:16], used, total, pct, fs)
        for (m, used, total, pct, fs) in rows)
    return "💾 *Storage*\n" + _fenced(table)


def cmd_sessions(config):
    user = config.get("SSH_USER") or "user"
    session = config.get("TMUX_SESSION") or "main"
    state, names = get_sessions(user)
    if state == "no_tmux":
        return notify.md2_escape("🧩 tmux isn't installed.")
    if state == "denied":
        return "🔒 " + notify.md2_escape(
            "Session details unavailable — permission denied.")
    if state == "error":
        return notify.md2_escape("🧩 Couldn't query tmux sessions.")
    if state == "none" or not names:
        return (notify.md2_escape(
            "🧩 No tmux sessions running. Start one on the NAS with:")
            + "\n" + _copy("tmux new-session -A -s %s" % session))
    note = ("'%s' is running ✅" % session if session in names
            else "'%s' is not running yet" % session)
    return "🧩 *tmux sessions*\n" + _fenced("\n".join(names)) + "\n" + notify.md2_escape(note)


def cmd_logs(config):
    out = get_logs(token=config.get("TELEGRAM_BOT_TOKEN"))
    if out is None:
        return notify.md2_escape("📜 journalctl isn't available on this system.")
    if out == "denied":
        return "🔒 " + notify.md2_escape(
            "Logs unavailable — insufficient permissions. Add your user to the "
            "'systemd-journal' group, or run: sudo turtlengine logs")
    out = out.strip() or "(no recent entries)"
    if len(out) > MAX_LOG_CHARS:
        out = out[-MAX_LOG_CHARS:]
    return "📜 *Notifier logs* " + notify.md2_escape("(recent)") + "\n" + _fenced(out)


COMMANDS = {
    "help": cmd_help, "start": cmd_help,
    "status": cmd_status, "notify": cmd_status,
    "ip": cmd_ip,
    "ssh_local": cmd_ssh_local,
    "ssh_remote": cmd_ssh_remote,
    "storage": cmd_storage,
    "sessions": cmd_sessions,
    "logs": cmd_logs,
    "ping": cmd_ping,
}


def handle_update(update, config, token, chat_id, user_id):
    """Authorise, then dispatch one update. Unauthorized updates are ignored."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    if not is_authorized(message, user_id, chat_id):
        return
    cmd = parse_command(message.get("text", ""))
    if not cmd:
        return
    handler = COMMANDS.get(cmd)
    if handler is None:
        reply = notify.md2_escape("Unknown command /%s — try /help." % cmd)
    else:
        try:
            reply = handler(config)
        except Exception as exc:          # one bad command must not crash the bot
            reply = notify.md2_escape("Error handling /%s: %s" % (cmd, exc))
    notify.send_message(token, chat_id, reply)


def main():
    notify.print_banner()
    config = notify.load_env(BOT_ENV_PATH)
    token = config.get("TELEGRAM_BOT_TOKEN")
    chat_id = config.get("TELEGRAM_CHAT_ID")
    user_id = config.get("AUTHORIZED_USER_ID")
    if not token or not chat_id or not user_id:
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / AUTHORIZED_USER_ID "
              "in %s" % BOT_ENV_PATH, file=sys.stderr)
        return 1

    print("TurtlEngine bot online; authorised user %s in chat %s."
          % (user_id, chat_id), file=sys.stderr)

    # Drain any backlog so old messages (e.g. the /start used during setup) are
    # not replayed as commands when the bot starts.
    offset = None
    try:
        backlog = get_updates(token, offset=None, timeout=0)
        if backlog:
            offset = backlog[-1]["update_id"] + 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("Startup getUpdates failed (will retry): %s" % exc, file=sys.stderr)

    while True:
        try:
            updates = get_updates(token, offset=offset, timeout=POLL_TIMEOUT_S)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("getUpdates error: %s" % exc, file=sys.stderr)
            time.sleep(ERROR_BACKOFF_S)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update, config, token, chat_id, user_id)
            except Exception as exc:
                print("handler error: %s" % exc, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
