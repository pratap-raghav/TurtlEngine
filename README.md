```
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
```

# TurtlEngine

Provisioning scripts that turn a repurposed NAS running **Ubuntu 26.04.1 LTS
Desktop** (BIOS/Legacy boot) into an always-on, headless development server.
Clone the repo onto a fresh install, run one script, and the box is ready for
remote development.

The idea: code, git, Claude Code, and long-running pipelines all run **on the
NAS**. Your laptop and phone are thin clients that attach over SSH. A persistent
**tmux** session means work survives disconnects — detach on the phone, reattach
on the laptop, nothing stops. **Tailscale** gives secure access from anywhere
without exposing ports, and a **Telegram boot-notifier** messages a ready-to-paste
SSH command on every boot.

## What it sets up

| Component | What it does |
| --- | --- |
| `scripts/install.sh` | Main provisioner: SSH, tmux, core tools, no-sleep, Tailscale (install only), MOTD, the `turtlengine` CLI, optional notifier |
| `scripts/turtlengine` | Local CLI to manage status, storage, and shares after install |
| `scripts/setup-notifier.sh` | Installs the Telegram boot-notifier (systemd service) |
| `scripts/mount-drive.sh` | Mounts a data drive by UUID (`nofail`); optional `--format` |
| `scripts/setup-samba.sh` | Configures one SMB share for Windows / Android / iOS |
| `notifier/notify.py` | The boot-notifier itself (Python stdlib only) |
| `systemd/nas-notifier.service` | Runs the notifier once on every boot |
| `motd/turtlengine.txt` | ASCII login banner |

`install.sh` prints the TurtlEngine banner the moment it starts, provisions the
base system, and installs the `turtlengine` CLI — but it does **not** format
disks, mount data drives, or create any network share. You choose storage and
shares yourself afterwards with the CLI.

## Quick start

```bash
git clone <this-repo> turtlengine
cd turtlengine
sudo ./scripts/install.sh      # idempotent; safe to re-run
```

`install.sh` finishes by printing exactly what it did automatically and what you
still need to do by hand. The remaining manual steps are:

```bash
sudo tailscale up                 # 1. interactive login; enables remote access
ssh-copy-id <user>@<nas-ip>       # 2. from the CLIENT (laptop/phone), not the NAS
#    then verify key login works BEFORE disabling password auth (see Security notes)
#    3-4. configure + test the Telegram notifier (see below)
tmux new-session -A -s main       # 5. start your persistent session, on the NAS
```

Nothing above is done for you — Tailscale is installed but not logged in, and the
notifier is installed (if you opted in) but not configured.

### Reconnect from anywhere

```bash
ssh <user>@<ip> -t tmux new-session -A -s main
```

Use the LAN IP at home, or the Tailscale IP from anywhere. `new-session -A`
attaches to the `main` session if it exists and creates it otherwise, so the
very first connect after a boot works too (a plain `tmux attach` would fail when
no session exists yet). The boot-notifier sends you exactly this command for
each reachable address on every boot.

## Telegram boot-notifier

The notifier messages you a paste-ready reconnect command on every boot — handy
from the phone, where it's one tap.

1. **Create a bot.** In Telegram, message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts, and copy the **token**.
2. **Get your chat id.** Send any message to your new bot once (so it may reply
   to you), then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a
   browser and read `"chat":{"id":<number>}`.
3. **Install it.** Either answer `y` to the prompt during `install.sh`, or run:
   ```bash
   sudo ./scripts/setup-notifier.sh
   ```
4. **Fill in the config:**
   ```bash
   sudo nano /etc/nas-notifier.env
   ```
   Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. `SSH_USER` and `TMUX_SESSION`
   are optional (they default to the invoking user and `main`).
5. **Test without rebooting:**
   ```bash
   sudo systemctl start nas-notifier.service
   journalctl -u nas-notifier.service -b
   ```

The real `/etc/nas-notifier.env` is chmod 600 and gitignored — only
`notifier/nas-notifier.env.example` is committed.

On boot the notifier waits for **real** connectivity (wired or Wi-Fi) before
sending — it polls `api.telegram.org` rather than trusting that a link is up,
retries at a fixed interval up to a timeout, then gives Tailscale a moment to
come up. LAN and Tailscale addresses are gathered independently, so if Tailscale
isn't connected you still get the LAN command. If connectivity never arrives it
exits non-zero (visible in `journalctl`, no restart loop) and boot proceeds
normally. systemd's `network-online.target` is only a hint; the script's own
check is authoritative.

## Managing the NAS: the `turtlengine` CLI

After install, `turtlengine` is on your `PATH`. Discovery is read-only; anything
that changes the system needs root and asks for confirmation. Nothing is
formatted, mounted, or shared unless you say so.

```bash
sudo turtlengine                 # interactive menu
sudo turtlengine status          # services, network, storage, notifier
     turtlengine storage list    # disks/partitions/filesystems/space (read-only)
sudo turtlengine storage mount   # guided mount of a chosen filesystem
sudo turtlengine share add       # configure one Samba share (guided)
     turtlengine share list      # list configured shares and their paths
sudo turtlengine share remove X  # remove share X's config (keeps the files)
```

`status` never prints secrets — it reports that `/etc/nas-notifier.env` exists
and its permissions, not its contents.

## Storage and sharing — you choose the paths

TurtlEngine does not assume a single data disk. Each share is independent: you
pick the directory, the Linux user, and the share name. For example:

| Directory | Share name |
| --- | --- |
| `/mnt/nas-1tb/media` | `Storage1TB` |
| `/mnt/nas-4tb/backups` | `Storage4TB` |
| `/srv/shared` | `SharedFiles` |

These are **examples, not defaults** — none of them are created automatically.

### Mount a data drive

```bash
# Mount an already-formatted drive (adds a UUID entry to /etc/fstab, nofail).
sudo ./scripts/mount-drive.sh /dev/sdb1 /mnt/nas-1tb

# Format (DESTRUCTIVE) then mount. You must re-type the device name to confirm.
sudo ./scripts/mount-drive.sh /dev/sdb /mnt/nas-1tb --format
```

### Add an SMB share

```bash
sudo ./scripts/setup-samba.sh <user> /mnt/nas-1tb/media Storage1TB
sudo ./scripts/setup-samba.sh <user> /mnt/nas-4tb/backups Storage4TB   # another, independent
```

Or use the guided flow: `sudo turtlengine share add`. Before creating a share
it validates the directory and user, reports which filesystem the path is on,
**warns and re-confirms** if the path is on the root filesystem or a
system-critical location, and never changes ownership recursively (it offers to
set the top-level owner only, and only if you say yes). Re-running never
duplicates an existing `[share]` block. Share name defaults to the directory's
basename. Prints Windows / iOS / Android connect strings; the SMB password is
separate from the Linux password.

To remove a share later without deleting its files:

```bash
sudo turtlengine share remove Storage4TB    # backs up smb.conf, reloads smbd
```

## Security notes

- **Bot token is a secret.** It lives only in `/etc/nas-notifier.env` (chmod
  600, gitignored). Never commit it. If it leaks, revoke via @BotFather.
- **Key-based SSH only.** No script handles a password; the notifier never does.
  Once your key login works, harden SSH by setting `PasswordAuthentication no`
  in `/etc/ssh/sshd_config` and `sudo systemctl restart ssh`. Do this only after
  confirming key login works, or you risk locking yourself out — which is why no
  script flips it for you.
- **Tailscale exposes nothing to the internet.** Access rides the tailnet; no
  ports are forwarded on your router.

## Manual steps (not automated on purpose)

- `sudo tailscale up` — interactive browser login.
- SSH key generation + `ssh-copy-id` — done on the client machines, not the NAS.
- DHCP reservation / static IP — set on your router.
- Filling `/etc/nas-notifier.env` with the real token and chat id.
- Disabling SSH password auth — see Security notes above.
