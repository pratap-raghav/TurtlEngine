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
    table; there is no /exec and no dynamic service/command name. Every command
    only reads state or hands the user a command to run themselves -- the bot
    never changes anything on the box.
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
# Overridable so tests can point at a throwaway smb.conf.
SMB_CONF = os.environ.get("SMB_CONF", "/etc/samba/smb.conf")
POLL_TIMEOUT_S = 50          # Telegram long-poll hold time
ERROR_BACKOFF_S = 5
MAX_LOG_CHARS = 3500         # keep /logs within Telegram's message limit
# tmux session-name characters we accept from /new_terminal. tmux forbids '.'
# and ':'; we keep an even tighter token so the name is also shell-copy-safe.
SAFE_SESSION_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
MAX_SESSION_NAME = 32

# Single source of truth for the command list. /help renders it, and it's
# registered with Telegram (setMyCommands) so typing "/" pops up the menu with
# descriptions -- just like BotFather. Names must be [a-z0-9_], 1-32 chars;
# descriptions 1-256. Aliases (start, addshare, ...) are intentionally omitted
# here so they don't clutter the menu.
COMMAND_HELP = [
    ("status", "full status card"),
    ("ip", "current LAN and Tailscale addresses"),
    ("ssh_local", "SSH command for home / LAN"),
    ("ssh_remote", "SSH command via Tailscale"),
    ("drives", "just the shared-drive names"),
    ("shares", "shared drives + connect commands (all OSes)"),
    ("add_share", "how to add a new shared drive"),
    ("storage", "disks and free space"),
    ("gpu", "GPU load, VRAM and temperature"),
    ("top", "processes using the most CPU"),
    ("temp", "CPU and GPU temperatures"),
    ("services", "listening services and their URLs"),
    ("net", "Tailscale devices online"),
    ("sessions", "tmux sessions"),
    ("new_terminal", "command to open another terminal (add a name)"),
    ("logs", "recent notifier logs"),
    ("ping", "check the bot is alive"),
    ("notify", "resend the status card"),
    ("help", "this list"),
]

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


def set_my_commands(token, commands=COMMAND_HELP):
    """Register the command list so Telegram shows it when you type "/".

    This is what gives the BotFather-style autocomplete popup. It's a one-off
    call at startup and purely cosmetic (it configures the bot's menu, not the
    box), so a failure here must never stop the bot from serving commands.
    """
    payload = json.dumps({
        "commands": [{"command": c, "description": d} for c, d in commands],
    }).encode("utf-8")
    url = "%s/bot%s/setMyCommands" % (notify.TELEGRAM_API, token)
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError) as exc:
        print("setMyCommands failed (the '/' menu won't update): %s" % exc,
              file=sys.stderr)
        return False


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


def get_shares(path=SMB_CONF):
    """Parse smb.conf into a list of {name, path, users} for real shares.

    Section headers are '[name]'; we skip the special [global], [printers] and
    [print$] blocks and any section with no 'path =' (not an actual share).
    Keys are case-insensitive. 'valid users' may list several accounts/@groups;
    we keep the raw value and let the formatter show the first token as a hint.
    Returns None only if the config can't be read at all (vs. [] for none).
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    skip = {"global", "printers", "print$"}
    shares = []
    cur = None
    for raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            cur = None if name.lower() in skip else {
                "name": name, "path": "", "users": ""}
            if cur is not None:
                shares.append(cur)
            continue
        if cur is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "path":
            cur["path"] = value
        elif key in ("valid users", "username", "users") and not cur["users"]:
            cur["users"] = value
    return [s for s in shares if s["path"]]


def get_top(n=6):
    """Top `n` processes by CPU as (command, cpu%, mem%). None if unreadable."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pcpu,pmem,comm", "--sort=-pcpu", "--no-headers"],
            capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows.append((parts[2], parts[0], parts[1]))   # comm, cpu, mem
        if len(rows) >= n:
            break
    return rows


def get_temps():
    """Return [(label, celsius)] from thermal zones and any NVIDIA GPU."""
    temps = []
    base = "/sys/class/thermal"
    try:
        zones = sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
    except OSError:
        zones = []
    for zone in zones:
        try:
            with open(os.path.join(base, zone, "type"), encoding="utf-8") as handle:
                label = handle.read().strip()
            with open(os.path.join(base, zone, "temp"), encoding="utf-8") as handle:
                milli = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        temps.append((label, milli / 1000.0))
    for gpu in notify.get_gpus():
        if gpu.get("temp_c") is not None:
            temps.append((gpu["name"], float(gpu["temp_c"])))
    return temps


def get_tailscale_peers():
    """Return (state, [(name, ip, online)]). state: ok | not_installed | down."""
    try:
        result = subprocess.run(["tailscale", "status", "--json"],
                                 capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return "not_installed", []
    except subprocess.SubprocessError:
        return "down", []
    if result.returncode != 0:
        return "down", []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return "down", []
    peers = []
    for peer in (data.get("Peer") or {}).values():
        name = peer.get("HostName") or (peer.get("DNSName") or "").split(".")[0]
        ips = peer.get("TailscaleIPs") or []
        peers.append((name or "?", ips[0] if ips else "", bool(peer.get("Online"))))
    return "ok", peers


def get_listening_ports():
    """Return sorted [(port, reachable)] for TCP listeners. None if unreadable.

    `reachable` is True when the port is bound to something other than
    loopback, i.e. other devices on the network/tailnet can reach it. We query
    with `ss -tln` (no -p), so no root and no process names are needed.
    """
    try:
        result = subprocess.run(["ss", "-H", "-tln"],
                                 capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    loopback = {"127.0.0.1", "[::1]", "::1"}
    reachable = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        is_local = addr in loopback
        port = int(port)
        reachable[port] = reachable.get(port, False) or (not is_local)
    return sorted(reachable.items())


def sanitize_session_name(raw):
    """Keep only safe characters from a requested session name (may be empty)."""
    return "".join(c for c in (raw or "").strip()
                   if c in SAFE_SESSION_CHARS)[:MAX_SESSION_NAME]


def next_session_name(existing, base="work"):
    """First free 'work', 'work2', 'work3', ... not already in `existing`."""
    if base not in existing:
        return base
    n = 2
    while "%s%d" % (base, n) in existing:
        n += 1
    return "%s%d" % (base, n)


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


def cmd_help(config, args=""):
    lines = ["🐢 *TurtlEngine bot* " + notify.md2_escape("(read-only)"), ""]
    for name, desc in COMMAND_HELP:
        lines.append("`/%s` — %s" % (name, notify.md2_escape(desc)))
    lines.append("")
    lines.append(notify.md2_escape("Tip: type \"/\" to pick a command from the menu."))
    return "\n".join(lines)


def cmd_ping(config, args=""):
    host = socket.gethostname()
    up = notify.get_uptime() or "?"
    return "🐢 " + notify.md2_escape("pong — %s is up (uptime %s)" % (host, up))


def cmd_status(config, args=""):
    return notify.build_message(notify.gather_info(config))


def cmd_ip(config, args=""):
    lan = notify.get_lan_ip()
    ts = notify.get_tailscale_ip()
    lines = ["📡 *Addresses*", ""]
    lines.append("🏠 LAN: " + ("`%s`" % notify.md2_code_escape(lan)
                               if lan else notify.md2_escape("unavailable")))
    lines.append("🌍 Tailscale: " + ("`%s`" % notify.md2_code_escape(ts)
                                     if ts else notify.md2_escape("not connected")))
    return "\n".join(lines)


def cmd_ssh_local(config, args=""):
    user = config.get("SSH_USER") or "user"
    session = config.get("TMUX_SESSION") or "main"
    lan = notify.get_lan_ip()
    if not lan:
        return notify.md2_escape(
            "🏠 LAN address couldn't be determined — is the NAS on the network?")
    return "🏠 *Home / LAN*\n" + _copy(_ssh_command(user, lan, session))


def cmd_ssh_remote(config, args=""):
    user = config.get("SSH_USER") or "user"
    session = config.get("TMUX_SESSION") or "main"
    ts = notify.get_tailscale_ip()
    if not ts:
        return (notify.md2_escape(
            "🌍 Tailscale isn't connected. Run this on the NAS, then try again:")
            + "\n" + _copy("sudo tailscale up"))
    return "🌍 *Anywhere / Tailscale*\n" + _copy(_ssh_command(user, ts, session))


def _share_block(share, ip, default_user):
    """MarkdownV2 lines for one share: a header plus one copyable line per OS.

    Each command is an inline code span so a tap copies it on mobile. The
    `smb://` URL is identical on macOS, Linux (Files) and iOS, but we label each
    OS on its own line so there is nothing to figure out -- copy the one for the
    device in your hand.
    """
    name = share["name"]
    user = (share.get("users") or "").split()[0] if share.get("users") else ""
    user = user.lstrip("@") or default_user
    smb_url = "smb://%s/%s" % (ip, name)
    mount = ("sudo mount -t cifs //%s/%s /mnt/%s "
             "-o username=%s,uid=$(id -u),gid=$(id -g)" % (ip, name, name, user))
    header = "📁 *%s* %s" % (
        notify.md2_escape(name),
        "\\(%s\\)" % notify.md2_escape(share["path"]) if share.get("path") else "")
    lines = [header.rstrip()]
    lines.append("🪟 Windows: " + _copy("\\\\%s\\%s" % (ip, name)))
    lines.append("🍎 macOS: " + _copy(smb_url))
    lines.append("🐧 Linux \\(Files\\): " + _copy(smb_url))
    lines.append("🐧 Linux \\(mount\\): " + _copy(mount))
    lines.append("🤖 Android \\(CX/Solid\\): host "
                 + _copy(ip) + notify.md2_escape(" · share ") + _copy(name)
                 + notify.md2_escape(" · user ") + _copy(user))
    lines.append("📱 iOS: " + _copy(smb_url))
    return "\n".join(lines)


def cmd_shares(config, args=""):
    shares = get_shares()
    if shares is None:
        return notify.md2_escape(
            "🗂 Can't read the Samba config (%s)." % SMB_CONF)
    if not shares:
        return (notify.md2_escape("🗂 No shared drives yet. Add one on the NAS with:")
                + "\n" + _copy("sudo turtlengine share add"))
    lan = notify.get_lan_ip()
    ts = notify.get_tailscale_ip()
    ip = lan or ts
    if ip is None:
        return notify.md2_escape(
            "🗂 Found shares, but couldn't determine this NAS's address.")
    default_user = config.get("SSH_USER") or "user"
    blocks = [_share_block(s, ip, default_user) for s in shares]
    out = ["🗂 *Shared drives* " + notify.md2_escape("(%d)" % len(shares)), ""]
    out.append("\n\n".join(blocks))
    out.append("")
    out.append(notify.md2_escape(
        "Sign in with your SMB username + the SMB password you set "
        "(not the Linux password)."))
    if ts and lan:
        out.append(notify.md2_escape(
            "🌍 Away from home? Swap the IP for your Tailscale IP: ") + _copy(ts))
    return "\n".join(out)


def cmd_drives(config, args=""):
    """Just the share names -- a quick glance, without the per-OS commands."""
    shares = get_shares()
    if shares is None:
        return notify.md2_escape(
            "🗂 Can't read the Samba config (%s)." % SMB_CONF)
    if not shares:
        return (notify.md2_escape("🗂 No shared drives yet. Send /add_share to set one up."))
    lines = ["🗂 *Shared drives* " + notify.md2_escape("(%d)" % len(shares)), ""]
    for share in shares:
        lines.append("• `%s`" % notify.md2_code_escape(share["name"]))
    lines.append("")
    lines.append(notify.md2_escape(
        "Send /shares for the connect commands, or /add_share to add one."))
    return "\n".join(lines)


def cmd_add_share(config, args=""):
    """How to add a new shared drive. Read-only: it explains, it doesn't act."""
    return "\n".join([
        "➕ *Add a shared drive*",
        "",
        notify.md2_escape(
            "Adding a drive runs on the NAS itself and needs admin rights, so "
            "this read-only bot can't do it for you — but here's exactly how:"),
        "",
        notify.md2_escape("1. Open a terminal on the NAS (see /ssh_local), then run:"),
        _copy("sudo turtlengine share add"),
        "",
        notify.md2_escape("2. It walks you through three questions:"),
        notify.md2_escape("   • which folder to share (e.g. /mnt/nas-1tb/media)"),
        notify.md2_escape("   • which user may open it (your login name)"),
        notify.md2_escape("   • a name for the share"),
        "",
        notify.md2_escape(
            "3. When it's done, send /shares here for the connect commands."),
    ])


def cmd_storage(config, args=""):
    rows = get_storage()
    if rows is None:
        return notify.md2_escape("💾 Storage info unavailable.")
    if not rows:
        return notify.md2_escape("💾 No mounted data filesystems found.")
    table = "\n".join(
        "%-16s %5.0f/%-5.0f GiB %3d%% %s" % (m[:16], used, total, pct, fs)
        for (m, used, total, pct, fs) in rows)
    return "💾 *Storage*\n" + _fenced(table)


def cmd_gpu(config, args=""):
    gpus = notify.get_gpus()
    if not gpus:
        return notify.md2_escape(
            "🎮 No GPU detected (nvidia-smi isn't available). An NVIDIA GPU shows "
            "live load/VRAM/temp here once its driver is installed.")
    lines = ["🎮 *GPU*"]
    for gpu in gpus:
        lines.append("")
        lines.append("*%s*" % notify.md2_escape(gpu["name"]))
        detail = []
        if gpu.get("util_pct") is not None:
            detail.append("load %d%%" % gpu["util_pct"])
        if gpu.get("mem_total_mib"):
            detail.append("VRAM %.1f/%.1f GiB" % (
                (gpu.get("mem_used_mib") or 0) / 1024.0,
                gpu["mem_total_mib"] / 1024.0))
        if gpu.get("temp_c") is not None:
            detail.append("%d°C" % gpu["temp_c"])
        if detail:
            lines.append(notify.md2_escape(" · ".join(detail)))
    return "\n".join(lines)


def cmd_top(config, args=""):
    rows = get_top()
    if rows is None:
        return notify.md2_escape("📈 Couldn't read the process list.")
    if not rows:
        return notify.md2_escape("📈 No processes to show.")
    header = "%5s %5s  %s" % ("CPU", "MEM", "PROCESS")
    body = "\n".join("%4s%% %4s%%  %s" % (cpu, mem, comm[:18])
                     for comm, cpu, mem in rows)
    return ("📈 *Top processes* " + notify.md2_escape("(by CPU)")
            + "\n" + _fenced(header + "\n" + body))


def cmd_temp(config, args=""):
    temps = get_temps()
    if not temps:
        return notify.md2_escape(
            "🌡 No temperature sensors found on this box.")
    table = "\n".join("%-24s %4.0f°C" % (label[:24], celsius)
                      for label, celsius in temps)
    return "🌡 *Temperatures*\n" + _fenced(table)


def cmd_net(config, args=""):
    state, peers = get_tailscale_peers()
    if state == "not_installed":
        return notify.md2_escape("🌐 Tailscale isn't installed on this box.")
    if state == "down":
        return (notify.md2_escape("🌐 Tailscale isn't connected. On the NAS, run:")
                + "\n" + _copy("sudo tailscale up"))
    if not peers:
        return notify.md2_escape("🌐 No other devices in your tailnet yet.")
    # Online first, then alphabetical.
    peers = sorted(peers, key=lambda p: (not p[2], p[0].lower()))
    lines = ["🌐 *Tailscale devices*", ""]
    for name, ip, online in peers:
        dot = "🟢" if online else "⚪️"
        addr = ("`%s`" % notify.md2_code_escape(ip)) if ip else notify.md2_escape("—")
        lines.append("%s %s — %s" % (dot, notify.md2_escape(name), addr))
    return "\n".join(lines)


def cmd_services(config, args=""):
    ports = get_listening_ports()
    if ports is None:
        return notify.md2_escape(
            "🔌 Couldn't read listening ports (ss isn't available).")
    if not ports:
        return notify.md2_escape("🔌 Nothing is listening for connections.")
    ip = notify.get_lan_ip() or notify.get_tailscale_ip()
    lines = ["🔌 *Listening services*", ""]
    for port, reachable in ports:
        if not reachable:
            lines.append(notify.md2_escape("• %d — local only" % port))
        elif port == 22:
            lines.append(notify.md2_escape("• 22 — SSH"))
        elif ip:
            scheme = "https" if port in (443, 8443) else "http"
            lines.append("• " + _copy("%s://%s:%d" % (scheme, ip, port)))
        else:
            lines.append(notify.md2_escape("• %d" % port))
    lines.append("")
    lines.append(notify.md2_escape(
        "Reachable from devices on your network/tailnet. Only you see this list."))
    return "\n".join(lines)


def cmd_sessions(config, args=""):
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


def cmd_new_terminal(config, args=""):
    """Hand back a copy-paste command that opens a new terminal on the NAS.

    The bot doesn't start anything itself -- you stay in control: copy the line
    and run it. `args` is the optional name; blank picks the next free 'work[N]'
    by reading (read-only) the sessions already running. `tmux new-session -A`
    opens the named terminal, or re-opens it if it's already there.
    """
    user = config.get("SSH_USER") or "user"
    requested = sanitize_session_name(args)
    if args.strip() and not requested:
        return notify.md2_escape(
            "That name won't work. Use only letters, numbers, - or _ — "
            "for example:  /new_terminal project2")

    # Read-only peek so a blank request gets a name that isn't already taken.
    state, names = get_sessions(user)
    name = requested or next_session_name(set(names) if state == "ok" else set())

    lan = notify.get_lan_ip()
    ts = notify.get_tailscale_ip()
    targets = []
    if lan:
        targets.append(("🏠 At home / same Wi‑Fi", lan))
    if ts:
        targets.append(("🌍 Away from home (Tailscale)", ts))

    lines = ["🧩 *New terminal:* `%s`" % notify.md2_code_escape(name), ""]
    lines.append(notify.md2_escape(
        "Copy one line below and run it on your laptop or phone. It opens a new "
        "terminal called \"%s\" on the NAS (or re‑opens it if it already "
        "exists):" % name))
    if targets:
        for label, ip in targets:
            lines.append("")
            lines.append("*%s*" % notify.md2_escape(label))
            lines.append(_copy(_ssh_command(user, ip, name)))
    else:
        lines.append("")
        lines.append(notify.md2_escape(
            "No network address is available right now. Once you're on the NAS "
            "itself, this opens it:"))
        lines.append(_copy("tmux new-session -A -s %s" % name))
    return "\n".join(lines)


def cmd_logs(config, args=""):
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
    "shares": cmd_shares,
    "drives": cmd_drives,
    "add_share": cmd_add_share, "addshare": cmd_add_share,
    "share_add": cmd_add_share,
    "storage": cmd_storage,
    "gpu": cmd_gpu,
    "top": cmd_top,
    "temp": cmd_temp, "temps": cmd_temp,
    "net": cmd_net,
    "services": cmd_services, "ports": cmd_services,
    "sessions": cmd_sessions,
    "new_terminal": cmd_new_terminal, "terminal": cmd_new_terminal,
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
    text = message.get("text", "")
    cmd = parse_command(text)
    if not cmd:
        return
    # Everything after the command word is the argument (e.g. a session name).
    parts = (text or "").strip().split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    if handler is None:
        reply = notify.md2_escape("Unknown command /%s — try /help." % cmd)
    else:
        try:
            reply = handler(config, args)
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

    # Register the "/" autocomplete menu (best-effort; never blocks serving).
    set_my_commands(token)

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
