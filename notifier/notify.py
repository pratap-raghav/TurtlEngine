#!/usr/bin/env python3
"""TurtlEngine Telegram boot-notifier.

On every boot this sends a Telegram message with a ready-to-paste command that
SSHes into the NAS and attaches the persistent tmux session. Python standard
library only -- nothing to pip-install on a fresh box.

Config lives in /etc/nas-notifier.env (chmod 600). No password is ever handled
or sent; SSH login is key-based, and the message only carries host, IPs, and the
paste-ready command.
"""

import getpass
import os
import pwd
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The system path the systemd unit's EnvironmentFile points at.
ENV_PATH = "/etc/nas-notifier.env"
TELEGRAM_API = "https://api.telegram.org"

# Connectivity poll tuning (see wait_for_connectivity).
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 300  # ~5 minutes
# Give Tailscale a moment to establish once the net is actually up.
TAILSCALE_SETTLE_S = 5

# MarkdownV2 special characters that must be backslash-escaped in plain text.
# Backslash itself is escaped first so it does not double-escape the others.
MD2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def print_banner():
    """Print the TurtlEngine banner so it heads every run / journalctl entry."""
    print(r"""
                        __
             .,-;-;-,. /'_\
           _/_/_/_|_\_\) /
         '-<_><_><_><_>=/\
           `/_/====/_/-'\_\
            ""     ""    ""

   ╔════════════════════════════╗
   ║   T u r t l E n g i n e    ║
   ╚════════════════════════════╝
   slow is smooth, smooth is fast""")


def load_env(path=ENV_PATH):
    """Parse a simple KEY=value file. No dotenv dependency.

    Blank lines and # comments are ignored. A surrounding pair of single or
    double quotes on a value is stripped. Returns a dict (possibly empty).
    """
    config = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                # Match systemd EnvironmentFile parsing: ignore blank lines and
                # whole-line comments beginning with # or ; (no inline comments).
                if not line or line[0] in "#;" or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key:
                    config[key] = value
    except FileNotFoundError:
        pass
    return config


def wait_for_connectivity():
    """Poll the Telegram API until DNS + TCP + TLS actually work.

    A plain "link up" check is not enough on a box that boots faster than the
    network settles. We issue a HEAD request; any HTTP response -- including an
    HTTPError -- proves the full path resolved and connected, so that counts as
    online. Returns True once reachable, False after the timeout.
    """
    print(
        "Waiting for network connectivity (up to %ds, polling every %ds)..."
        % (POLL_TIMEOUT_S, POLL_INTERVAL_S),
        file=sys.stderr,
    )
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        request = urllib.request.Request(TELEGRAM_API, method="HEAD")
        try:
            urllib.request.urlopen(request, timeout=POLL_INTERVAL_S)
            return True
        except urllib.error.HTTPError:
            # Server answered (e.g. 404/405) -- the network path works.
            return True
        except (urllib.error.URLError, socket.timeout, OSError):
            # Interface up or Wi-Fi associated is NOT proof of connectivity;
            # keep polling until a real request to Telegram succeeds.
            time.sleep(POLL_INTERVAL_S)
    return False


def get_lan_ip():
    """Best-effort primary LAN IP via the UDP-connect trick (no packets sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def get_tailscale_ip():
    """Return the Tailscale IPv4 address, or None if Tailscale isn't up/present."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    ip = result.stdout.strip().splitlines()
    return ip[0].strip() if ip and ip[0].strip() else None


def get_uptime():
    """Human-ish uptime string from /proc/uptime, or None."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def get_cpu():
    """Return (model_name, core_count); either may be None/0 on failure."""
    model = None
    count = 0
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("processor"):
                    count += 1
                elif model is None and line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
    except OSError:
        pass
    if not count:
        count = os.cpu_count() or 0
    return model, count


def get_mem():
    """Return (used_gib, total_gib) from /proc/meminfo, or (None, None)."""
    try:
        info = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
        total_kb = int(info["MemTotal"].split()[0])
        avail_kb = int(info.get("MemAvailable", "0").split()[0])
    except (OSError, KeyError, ValueError, IndexError):
        return None, None
    gib = 1024 * 1024  # KiB -> GiB
    return (total_kb - avail_kb) / gib, total_kb / gib


def get_disk(path="/"):
    """Return (used_gib, total_gib, percent_used) for the fs at path."""
    try:
        stat = os.statvfs(path)
    except OSError:
        return None, None, None
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    if total <= 0:
        return None, None, None
    used = total - free
    gib = 1024 ** 3
    return used / gib, total / gib, int(round(used * 100 / total))


def get_loadavg():
    """Return the 1/5/15-minute load-average tuple, or None."""
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None


def get_kernel():
    """Return the kernel release string, or None."""
    try:
        return os.uname().release
    except AttributeError:
        return None


def tmux_socket_for_user(user):
    """Default tmux socket path for a user, or None if the user is unknown.

    tmux uses /tmp/tmux-<uid>/default unless TMUX_TMPDIR is set. This lets the
    notifier (running as root) check the target user's own tmux server rather
    than root's, which is a different server entirely.
    """
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return None
    return "/tmp/tmux-%d/default" % uid


def tmux_check_cmd(session, user=None, current_user=None):
    """Build the argv for checking a user's tmux session.

    If a distinct target user is given, point tmux at that user's default
    socket; otherwise check the current user's own server. Factored out (pure,
    no I/O) so it can be unit-tested.
    """
    if current_user is None:
        current_user = getpass.getuser()
    if user and user != current_user:
        socket_path = tmux_socket_for_user(user)
        if socket_path:
            return ["tmux", "-S", socket_path, "has-session", "-t", session]
    return ["tmux", "has-session", "-t", session]


def tmux_session_alive(session, user=None):
    """True if the target user's tmux session by that name exists."""
    try:
        result = subprocess.run(
            tmux_check_cmd(session, user),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def md2_escape(text):
    """Escape a plain-text string for Telegram MarkdownV2."""
    text = text.replace("\\", "\\\\")
    for char in MD2_SPECIAL:
        text = text.replace(char, "\\" + char)
    return text


def md2_code_escape(text):
    """Escape a string for inside a MarkdownV2 code span.

    Only backtick and backslash need escaping inside code/pre entities; the
    other special chars are literal there, so the full md2_escape would leave
    visible stray backslashes.
    """
    return text.replace("\\", "\\\\").replace("`", "\\`")


def gather_info(config):
    """Collect everything the message needs into one dict (easy to fake/test)."""
    user = config.get("SSH_USER") or os.environ.get("SUDO_USER") or os.environ.get("USER") or "user"
    session = config.get("TMUX_SESSION") or "main"
    cpu_model, cpu_count = get_cpu()
    mem_used, mem_total = get_mem()
    disk_used, disk_total, disk_pct = get_disk("/")
    return {
        "hostname": socket.gethostname(),
        "lan_ip": get_lan_ip(),
        "tailscale_ip": get_tailscale_ip(),
        "uptime": get_uptime(),
        "tmux_alive": tmux_session_alive(session, user),
        "ssh_user": user,
        "tmux_session": session,
        "cpu_model": cpu_model,
        "cpu_count": cpu_count,
        "mem_used_gib": mem_used,
        "mem_total_gib": mem_total,
        "disk_used_gib": disk_used,
        "disk_total_gib": disk_total,
        "disk_pct": disk_pct,
        "loadavg": get_loadavg(),
        "kernel": get_kernel(),
    }


def build_message(info):
    """Build the MarkdownV2 message body from a gathered-info dict.

    Factored out with no I/O so it can be smoke-tested with a fake config.
    - A reachable address contributes one "label + command" pair.
    - A missing IP omits its line; with neither IP we say so plainly.
    """
    lines = ["🐢 *TurtlEngine* is up", ""]
    lines.append("🖥 Host: `%s`" % md2_code_escape(info["hostname"]))
    if info.get("uptime"):
        lines.append("⏱ Uptime: %s" % md2_escape(info["uptime"]))
    if info.get("cpu_model"):
        cpu = info["cpu_model"]
        if info.get("cpu_count"):
            cpu = "%s (%d cores)" % (cpu, info["cpu_count"])
        lines.append("🧮 CPU: %s" % md2_escape(cpu))
    if info.get("mem_total_gib"):
        lines.append(
            "🧠 RAM: %s"
            % md2_escape("%.1f / %.1f GiB used"
                         % (info.get("mem_used_gib") or 0.0, info["mem_total_gib"]))
        )
    if info.get("disk_total_gib"):
        lines.append(
            "💾 Disk /: %s"
            % md2_escape("%.0f / %.0f GiB (%d%% used)"
                         % (info.get("disk_used_gib") or 0.0,
                            info["disk_total_gib"], info.get("disk_pct") or 0))
        )
    if info.get("loadavg"):
        lines.append("📊 Load: %s" % md2_escape("%.2f %.2f %.2f" % tuple(info["loadavg"])))
    if info.get("kernel"):
        lines.append("🐧 Kernel: `%s`" % md2_code_escape(info["kernel"]))

    session = info["tmux_session"]
    state = "alive ✅" if info.get("tmux_alive") else "not started ⚪"
    lines.append("🧩 tmux `%s`: %s" % (md2_code_escape(session), state))

    user = info["ssh_user"]
    reachable = []
    if info.get("lan_ip"):
        reachable.append(("🏠 Home / LAN", info["lan_ip"]))
    if info.get("tailscale_ip"):
        reachable.append(("🌍 Anywhere / Tailscale", info["tailscale_ip"]))

    if reachable:
        lines.append("")
        lines.append(md2_escape("Tap a command to copy it, then paste into your terminal:"))
        for label, ip in reachable:
            # new-session -A attaches to the session if it exists, or creates
            # it if not -- so the very first connect after a boot works too.
            command = "ssh %s@%s -t tmux new-session -A -s %s" % (user, ip, session)
            lines.append("")
            lines.append("*%s* \\(%s\\)" % (md2_escape(label), md2_escape(ip)))
            # A fenced code block renders with a one-tap "copy" button in the
            # Telegram apps. Inside it only ` and \ need escaping.
            lines.append("```")
            lines.append(md2_code_escape(command))
            lines.append("```")
    else:
        lines.append("")
        lines.append(md2_escape("No LAN or Tailscale address available yet."))

    return "\n".join(lines)


def build_welcome_message(info):
    """Celebratory 'setup complete' message (MarkdownV2, pure).

    Sent once, right after setup, as the first of two messages -- the status
    card built by build_message() follows it.
    """
    lines = [
        "🎉 *TurtlEngine is live" + md2_escape("!") + "*",
        "",
        "`%s` " % md2_code_escape(info["hostname"])
        + md2_escape("is now an always-on dev server you can reach from anywhere."),
        md2_escape("Code, git and long jobs run here — your laptop and phone just attach."),
        "",
        md2_escape("Everything you need to jump in is in the next message") + " 👇",
    ]
    return "\n".join(lines)


def send_message(token, chat_id, text):
    """POST the message to Telegram. Returns True on HTTP 200."""
    url = "%s/bot%s/sendMessage" % (TELEGRAM_API, token)
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        print("Telegram API error %s: %s" % (error.code, body), file=sys.stderr)
        return False
    except (urllib.error.URLError, OSError) as error:
        print("Failed to reach Telegram: %s" % error, file=sys.stderr)
        return False


def main():
    print_banner()

    # --welcome: first-setup run -> send a celebratory message, then the card.
    welcome = "--welcome" in sys.argv[1:]

    config = load_env()
    token = config.get("TELEGRAM_BOT_TOKEN")
    chat_id = config.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in %s" % ENV_PATH,
            file=sys.stderr,
        )
        return 1

    if not wait_for_connectivity():
        print("No connectivity after %ss; giving up." % POLL_TIMEOUT_S, file=sys.stderr)
        return 1

    # Let Tailscale establish before we read its IP.
    time.sleep(TAILSCALE_SETTLE_S)

    info = gather_info(config)

    if welcome:
        if not send_message(token, chat_id, build_welcome_message(info)):
            return 1

    if not send_message(token, chat_id, build_message(info)):
        return 1

    print("Notification sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
