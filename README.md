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

[![CI](https://github.com/pratap-raghav/TurtlEngine/actions/workflows/ci.yml/badge.svg)](https://github.com/pratap-raghav/TurtlEngine/actions/workflows/ci.yml)

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
| `scripts/setup-bot.sh` | Installs the optional read-only Telegram command bot |
| `scripts/mount-drive.sh` | Mounts a data drive by UUID (`nofail`); optional `--format` |
| `scripts/setup-samba.sh` | Configures one SMB share for Windows / Android / iOS |
| `notifier/notify.py` | The boot-notifier itself (Python stdlib only) |
| `notifier/bot.py` | The read-only Telegram command bot (Python stdlib only) |
| `systemd/nas-notifier.service` | Runs the notifier once on every boot |
| `systemd/turtlebot.service` | Runs the command bot (long-polling, non-root) |
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

> **The Tailscale IP only works from a device that is itself on the tailnet.**
> Install Tailscale on each client (laptop/phone) and sign in to the **same
> account** as the NAS — otherwise there is simply no route to the `100.x.y.z`
> address and the connection will hang or fail. At home the LAN IP
> (`192.168.x.x`) needs nothing extra. Check both ends are connected with
> `tailscale status` — the other device should appear as a peer.

## Telegram boot-notifier

The notifier messages you a status card on every boot — host, uptime, CPU / GPU
/ RAM / disk / load / kernel, and a **tap-to-copy** reconnect command for each
address (LAN and Tailscale). Handy from the phone, where it's one tap. The GPU
line uses `nvidia-smi` when present, and falls back to the model name from
`lspci` for other cards.

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

## Telegram command bot (optional, read-only)

Beyond boot messages, you can turn the bot into a **read-only remote control** —
message it to check status, grab a fresh SSH command, or inspect storage from
your phone. It's a **separate**, hardened service from the boot notifier.

Install it after the notifier is configured:

```bash
sudo ./scripts/setup-bot.sh        # or: sudo turtlengine bot setup
```

It reuses your bot token, auto-detects your Telegram **user id**, and asks you to
confirm it before enabling — the bot then answers **only that account**.

| Command | What it returns |
| --- | --- |
| `/help` | the command list |
| `/status` | full status card (specs + reconnect commands) |
| `/ip` | current LAN and Tailscale addresses |
| `/ssh_local` · `/ssh_remote` | a fresh SSH command for home / for anywhere |
| `/drives` | just the shared-drive names (quick glance) |
| `/shares` | shared drives + tap-to-copy connect commands for every OS |
| `/add_share` | how to add a new shared drive (steps + command) |
| `/storage` | mounted disks and free space |
| `/gpu` | GPU load, VRAM and temperature (`nvidia-smi`) |
| `/top` | processes using the most CPU right now |
| `/temp` | CPU and GPU temperatures |
| `/services` | listening services and tap-to-open URLs |
| `/net` | Tailscale devices online |
| `/sessions` | tmux sessions (and whether `main` exists) |
| `/new_terminal [name]` | a copy-paste command to open another terminal |
| `/logs` | recent notifier logs (sanitized) |
| `/ping` | confirm the bot is alive |
| `/notify` | resend the status card |

`/shares` reads your configured Samba shares and prints a ready-to-paste connect
string for each one on **Windows** (`\\ip\Share`), **macOS**, **Linux** (Files
and a `mount` command), **Android** and **iOS** — tap any line to copy it. No
more hunting for the address after you add a drive. `/drives` is the quick
version — just the names — and `/add_share` walks you through adding one.

When you type `/` in the chat, your Telegram client shows the full command list
with descriptions (BotFather-style). The bot registers this menu on startup via
`setMyCommands`, so it stays in sync with what the bot actually supports.

`/new_terminal` helps when you're juggling projects and want a second (third, …)
terminal: it **hands you a command to copy and run** — it never starts anything
itself. Send it bare for an auto-named `work`/`work2`/… terminal, or
`/new_terminal api` to name it. `tmux new-session -A` opens that terminal, or
re-opens it if it already exists.

Manage it with `turtlengine bot {status|logs|restart}`.

**Security.** It runs as your user (**non-root**, never `sudo`), accepts commands
only from your allowlisted user id **and** chat id, and never builds a shell from
message text. It is **read-only** — every command either reports state or hands
you a command to run yourself; the bot never changes anything on the box. If it
can't read something (a tmux socket, the journal) it says so with a distinct
state rather than failing or guessing. The token lives in `/etc/turtlebot.env`
(chmod 600).

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

- **Bot token is a secret.** It lives in `/etc/nas-notifier.env` (and, if you
  enable the command bot, a matching copy in `/etc/turtlebot.env`) — both chmod
  600 and gitignored. Never commit it. If it leaks, revoke via @BotFather.
- **Key-based SSH only.** No script handles a password; the notifier never does.
  Once your key login works, harden SSH by setting `PasswordAuthentication no`
  in `/etc/ssh/sshd_config` and `sudo systemctl restart ssh`. Do this only after
  confirming key login works, or you risk locking yourself out — which is why no
  script flips it for you.
- **Tailscale exposes nothing to the internet.** Access rides the tailnet; no
  ports are forwarded on your router.

## Troubleshooting

**The command bot doesn't answer anything.** Check `turtlengine bot logs`.
Repeated `getUpdates error: HTTP Error 401: Unauthorized` means the token in
`/etc/turtlebot.env` is wrong or revoked — it must match the one in
`/etc/nas-notifier.env`. Re-run `sudo ./scripts/setup-bot.sh` (it copies the
notifier's token) and then `turtlengine bot restart`. Confirm the token itself
is live with `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"` — expect
`"ok":true`. Note the boot notifier and the command bot are separate services:
getting the boot card only proves the *notifier's* token works, not the bot's.

**Can't reach the NAS over Tailscale.** Run `tailscale status` on the NAS. If it
lists **no peers**, the device you're connecting from isn't on the tailnet —
install Tailscale on that laptop/phone and sign in to the same account, then
retry. On the LAN you don't need Tailscale at all; use the `192.168.x.x` address.

**SSH says `Permission denied (publickey,password)` or keeps asking for a
password.** That's expected until you install a key — run `ssh-copy-id
<user>@<nas-ip>` from the client. Logging in with your normal account password
is also fine; only set `PasswordAuthentication no` *after* key login works (see
Security notes), or you can lock yourself out.

## Manual steps (not automated on purpose)

- `sudo tailscale up` — interactive browser login. The installer offers to run
  it for you; you can also run it anytime to (re)connect.
- SSH key generation + `ssh-copy-id` — done on the client machines, not the NAS.
- DHCP reservation / static IP — set on your router.
- Telegram token + chat id — the installer (and `setup-notifier.sh`) prompt for
  these interactively; re-run `setup-notifier.sh` to change them later.
- Disabling SSH password auth — see Security notes above.
