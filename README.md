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
| `scripts/install.sh` | Main provisioner: SSH, tmux, core tools, no-sleep, 16 GB swap, Tailscale, MOTD, optional notifier |
| `scripts/setup-notifier.sh` | Installs the Telegram boot-notifier (systemd service) |
| `scripts/mount-drive.sh` | Formats + auto-mounts a data drive by UUID (`nofail`) |
| `scripts/setup-samba.sh` | SMB file share for Windows / Android / iOS |
| `notifier/notify.py` | The boot-notifier itself (Python stdlib only) |
| `systemd/nas-notifier.service` | Runs the notifier once on every boot |
| `motd/turtlengine.txt` | ASCII login banner |

## Quick start

```bash
git clone <this-repo> turtlengine
cd turtlengine
sudo ./scripts/install.sh      # idempotent; safe to re-run
sudo tailscale up              # interactive browser login (manual)
```

Then, from your laptop or phone, copy your SSH key to the box and start a
session:

```bash
ssh-copy-id <user>@<nas-ip>       # run on the client, not the NAS
tmux new-session -A -s main       # on the NAS
```

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

## Other scripts

### Mount a data drive

```bash
# Format (DESTRUCTIVE) + mount. You must re-type the device name to confirm.
sudo ./scripts/mount-drive.sh /dev/sdb /mnt/data --format

# Mount an already-formatted drive (adds a UUID entry to /etc/fstab, nofail).
sudo ./scripts/mount-drive.sh /dev/sdb /mnt/data
```

### SMB file share

```bash
sudo ./scripts/setup-samba.sh <user> /mnt/data            # share name: turtlengine
sudo ./scripts/setup-samba.sh <user> /mnt/data mystuff    # custom share name
```

Prints Windows / iOS / Android connect strings. The SMB password is separate
from the Linux password.

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
