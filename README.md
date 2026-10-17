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
sudo ./scripts/install.sh      # guided; idempotent and safe to re-run
```

The installer runs like a guided setup assistant. It shows each phase as a tidy
`[n/6]` step (add `--verbose` to watch full command output; otherwise it's saved
to `/var/log/turtlengine-install.log`), then walks you through the two
interactive steps itself:

- **Telegram notifier** — you create the bot in the Telegram app (@BotFather)
  and paste its token; your chat id is auto-detected from the bot's recent
  messages (or entered by hand). Saves them (asking first if credentials already
  exist) and offers a test send.
- **Tailscale** — offers to run `tailscale up` so you can sign in right then.

When it finishes it prints what's configured and a short checklist of anything
left. The steps it deliberately does **not** automate:

```bash
ssh-copy-id <user>@<nas-ip>       # from the CLIENT (laptop/phone), not the NAS
#    verify key login works BEFORE disabling password auth (see Security notes)
tmux new-session -A -s main       # start your persistent session, on the NAS
sudo turtlengine                  # configure disks and shares, on your terms
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

The notifier messages you a status card on every boot — host, uptime, CPU / RAM
/ disk / load / kernel, and a **tap-to-copy** reconnect command for each address
(LAN and Tailscale). Handy from the phone, where it's one tap.

Setup is guided — during `install.sh`, or by running
`sudo ./scripts/setup-notifier.sh` any time:

1. **Create a bot.** Message **@BotFather** (the setup opens it via
   `telegram.me`, which often loads even where `t.me` is blocked — or just use
   the Telegram app), send `/newbot`, follow the prompts, and paste the **token**
   into the terminal.
2. **Get your chat id** — a number like `123456789` identifying *your* chat, not
   the bot's `@name`. Right after you paste the token the setup **auto-detects**
   it from the Bot API; just **press START in your new bot** first so there's a
   message to read. If nothing comes back it guides you to press START and
   retries; you can also type it manually — from **@userinfobot**, or via
   `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[].message.chat.id'`.
3. **It saves and sends a welcome.** Credentials are written to
   `/etc/nas-notifier.env` (chmod 600); if real credentials already exist it
   asks first before replacing them. On success it immediately sends a
   setup-complete message, then your status card — so you know it works right
   away. (Re-running also reinstalls `notify.py`, so it's how you pick up
   notifier updates.)

> **If your network blocks Telegram** (some ISPs intercept `t.me` and sometimes
> `api.telegram.org`): the token step still works from the app, but auto-detect
> and the notifier itself need `api.telegram.org`. Check it with
> `curl -sS -o /dev/null -w '%{http_code}\n' https://api.telegram.org/` — if that
> fails, route the NAS through Tailscale or a VPN (a public DNS resolver may help
> when the block is DNS-only).

`SSH_USER` and `TMUX_SESSION` in that file are optional (they default to the
invoking user and `main`). To reconfigure or test later without rebooting:

```bash
sudo ./scripts/setup-notifier.sh    # re-run the guided setup (reinstalls notify.py)
sudo turtlengine notify             # send a status card now
turtlengine logs                    # read the notifier logs
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
     turtlengine logs            # view the boot-notifier's logs (-f to follow)
sudo turtlengine notify          # send a status card to Telegram now
```

The `logs` and `notify` commands wrap the underlying `journalctl` / `systemctl`
calls, so you never have to remember them.

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

`--format` refuses to touch a device that has mounted or active-swap partitions
— so it will not erase the OS/root disk — prints the drive's current layout, and
then makes you re-type the device name before anything is erased. `/etc/fstab` is
backed up before the `nofail` entry is added.

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
duplicates an existing `[share]` block. It backs up `smb.conf` before editing,
validates the result with `testparm` (restoring the backup if validation fails),
and **reloads** `smbd` rather than restarting it, so existing connections aren't
dropped. Share name defaults to the directory's basename. Prints Windows / iOS /
Android connect strings; the SMB password is separate from the Linux password.

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

- `sudo tailscale up` — interactive browser login. The installer offers to run
  it for you; you can also run it anytime to (re)connect.
- SSH key generation + `ssh-copy-id` — done on the client machines, not the NAS.
- DHCP reservation / static IP — set on your router.
- Telegram token + chat id — the installer (and `setup-notifier.sh`) prompt for
  these interactively; re-run `setup-notifier.sh` to change them later.
- Disabling SSH password auth — see Security notes above.
