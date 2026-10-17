# TurtlEngine — NAS Dev-Box Setup (Build Plan)

A build spec for Claude Code to implement. Target: a repurposed NAS running
**Ubuntu 26.04.1 LTS Desktop** on a **BIOS/Legacy** board, turned into an
always-on headless development server. The project is named **TurtlEngine**;
use `turtlengine` as the repo name and in system paths (`/opt/turtlengine/...`).

The deliverable is a **git repo** of provisioning scripts: clone onto the fresh
Ubuntu install, run one script, and the box is ready for remote development.

> **Tone / scope note for the implementer:** build exactly what's specified
> here — plain Bash + a stdlib Python script. No frameworks, no extra
> dependencies, no cleverness beyond what's written. If something is genuinely
> ambiguous, pick the simplest option that satisfies the spec and note it in a
> comment; don't expand scope.

---

## Hardware / environment context

- **CPU/RAM:** Intel i3 (4th gen), 6 GB RAM — RAM is the tight resource.
- **GPU:** GT 1030 (not used for compute; display only).
- **Boot mode:** BIOS/Legacy (no UEFI). GRUB installed to the disk MBR.
- **Drives:**
  - `sda` — 480 GB SSD → OS (already installed: single ext4 `/` + 1 MB bios_grub).
  - `sdb` — 1 TB HDD (Seagate) → data drive, **not yet formatted/mounted**.
- **OS:** Ubuntu 26.04.1 LTS Desktop, no full-disk encryption (so SSH is reachable
  immediately at boot with no console unlock).
- **Network:** 1 GbE LAN. Will get a DHCP reservation / static IP on the router.
- **Clients:** a powerful laptop (Core Ultra 9 / RTX 5070 / 64 GB), and a phone
  running Termux / NetHunter.

### Intended workflow (the "why")
- Code, git, Claude Code, and long-running pipelines all run **on the NAS**.
- Laptop and phone are **thin clients** — they attach over SSH, they don't run
  the workload. Laptop stays off/cool most of the time.
- A **persistent tmux session** on the NAS means work survives disconnects:
  detach on the phone, reattach on the laptop, nothing stops.
- **Tailscale** gives secure access from anywhere without exposing ports.
- A **Telegram boot-notifier** messages a ready-to-paste SSH command on every
  boot, so reconnecting (especially from the phone) is one tap.

---

## Repo structure to create

```
turtlengine/
├── scripts/
│   ├── install.sh          # main entry point (idempotent)
│   ├── setup-notifier.sh   # installs the Telegram boot-notifier
│   ├── setup-samba.sh      # SMB file sharing (Windows / Android / iOS)
│   └── mount-drive.sh      # format + auto-mount a data drive by UUID
├── notifier/
│   ├── notify.py           # the boot notifier (Python stdlib only)
│   └── nas-notifier.env.example
├── systemd/
│   └── nas-notifier.service
├── motd/
│   └── turtlengine.txt     # ASCII login banner
├── .gitignore
└── README.md
```

---

## Component specs

### 1. `scripts/install.sh` — main provisioning (idempotent)

Runs with `sudo`. Must be safe to re-run (check state before each action).

Steps, in order:
1. **Guard:** require root; detect the invoking user via `$SUDO_USER` (operate
   for that user, not root).
2. **System update:** `apt-get update && apt-get upgrade -y`.
3. **Core packages:** `openssh-server tmux curl ca-certificates python3 jq`.
   Then `systemctl enable --now ssh`.
4. **Disable sleep** (critical for a headless always-on box):
   `systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
5. **Swapfile** (16 GB, resizable, lives on `/`):
   - Skip if already active (`swapon --show`).
   - `fallocate -l 16G /swapfile` (fall back to `dd` if fallocate fails). The OS
     drive is **ext4**, where `fallocate` for swap is fine — no btrfs special-
     casing needed.
   - `chmod 600`, `mkswap`, `swapon`.
   - Add to `/etc/fstab` if not present: `/swapfile none swap sw 0 0`.
   - Set `vm.swappiness=10` via `/etc/sysctl.d/99-nas-swappiness.conf`.
   - Make the swap size overridable via a `SWAP_SIZE_GB` env var (default 16).
6. **Tailscale:** install via `curl -fsSL https://tailscale.com/install.sh | sh`
   (skip if already installed). **Do not** run `tailscale up` automatically —
   it needs an interactive browser login; just print the instruction.
7. **Notifier (optional):** prompt y/N; if yes, call `setup-notifier.sh`.
8. **Final summary:** print next steps (tailscale up, ssh-copy-id, tmux new).

### 2. `notifier/notify.py` — Telegram boot-notifier

**Python standard library only** (no pip installs — nothing to break on a fresh
box). Behavior:

- **Print the TurtlEngine banner first** (the `print(r"""...""")` snippet in §11,
  verbatim) so it shows at the top of every run / `journalctl` entry.
- Read config from `/etc/nas-notifier.env` (simple `KEY=value` parser, no
  dotenv dependency):
  - `TELEGRAM_BOT_TOKEN` (required)
  - `TELEGRAM_CHAT_ID` (required)
  - `SSH_USER` (optional; default to invoking user)
  - `TMUX_SESSION` (optional; default `main`)
- **Wait for real connectivity**, not just "link up": poll
  `https://api.telegram.org` with a HEAD request until it responds (an HTTPError
  counts as online — DNS+TCP+TLS worked). Give up after ~5 min. Poll every 5 s.
- After connectivity, sleep ~5 s to let Tailscale establish.
- **Gather:** hostname; LAN IP (via a UDP socket `connect` to 8.8.8.8 trick);
  Tailscale IP (`tailscale ip -4`, tolerate absence); uptime; whether the tmux
  session is alive (`tmux has-session`).
- **Build a MarkdownV2 message** with a ready-to-paste command for each reachable
  address:
  - `ssh <user>@<lan-ip> -t tmux attach -t <session>`  (Home / LAN)
  - `ssh <user>@<tailscale-ip> -t tmux attach -t <session>`  (Anywhere / Tailscale)
  - Omit a line if that IP isn't available. Handle neither-available gracefully.
  - **Escape all MarkdownV2 special chars** (`_*[]()~\`>#+-=|{}.!`).
- **Send** via `POST https://api.telegram.org/bot<token>/sendMessage` with
  `chat_id`, `text`, `parse_mode=MarkdownV2`, `disable_web_page_preview=true`.
- **No password anywhere** — login is key-based; the message carries only host,
  IPs, and the paste-ready command.
- Exit non-zero on failure (missing config, no connectivity, non-200 response)
  so `journalctl` shows it clearly.

### 3. `notifier/nas-notifier.env.example`

Template with placeholder values and inline comments explaining how to get each:
- `@BotFather` → `/newbot` → copy token.
- Message the bot once so it can reply.
- Visit `https://api.telegram.org/bot<token>/getUpdates` → read `"chat":{"id":...}`.

### 4. `systemd/nas-notifier.service`

- `Type=oneshot`.
- `Wants=network-online.target`, `After=network-online.target tailscaled.service`.
- `EnvironmentFile=/etc/nas-notifier.env`.
- `ExecStart=/usr/bin/python3 /opt/turtlengine/notifier/notify.py`.
- `TimeoutStartSec=360` (the script waits internally for connectivity).
- `User=root` (so it can read the chmod-600 env file and call tailscale).
- `WantedBy=multi-user.target`.

> **Note:** Ubuntu **Desktop** uses NetworkManager, so `network-online.target`
> is satisfied by `NetworkManager-wait-online.service` (already enabled by
> default). Don't add a hard `Requires=` on it — the script's own connectivity
> poll is the real guard, so a soft `Wants/After` is enough and won't wedge boot
> if the net is briefly down.

### 5. `scripts/setup-notifier.sh`

- Guard root; take target username as `$1` (default `$SUDO_USER`).
- Copy `notify.py` to `/opt/turtlengine/notifier/` (stable system path, matches
  the service's ExecStart).
- Create `/etc/nas-notifier.env` from the example if absent; pre-fill `SSH_USER`;
  `chmod 600`. Leave it alone if it already exists.
- Install the service to `/etc/systemd/system/`, `daemon-reload`, `enable`.
- Print how to fill the env file, test (`systemctl start`), and read logs.

### 6. `scripts/mount-drive.sh` — format + auto-mount a data drive

- `sudo ./scripts/mount-drive.sh <device> <mountpoint> [--format]`.
- Guard root; validate the device is a block device.
- **Safety:** print device/mountpoint/mode, then require the user to **re-type the
  device name** to confirm before any destructive action.
- With `--format`: `parted` GPT label + one ext4 partition spanning the disk,
  then `mkfs.ext4`. Detect the resulting partition (`sdX1` or `nvme…p1`).
- `mkdir -p` the mountpoint.
- Add to `/etc/fstab` **by UUID** (`blkid -s UUID`), with `defaults,nofail 0 2`
  (nofail so a missing data drive never blocks boot). Skip if UUID already there.
- `mount` it and show `df -h`.

### 7. `scripts/setup-samba.sh` — SMB file sharing

- `sudo ./scripts/setup-samba.sh <username> <mountpoint> [sharename]`.
- Guard root; validate the mountpoint exists.
- `apt-get install -y samba`.
- Append a share block to `/etc/samba/smb.conf` (skip if `[sharename]` exists):
  path, `browseable = yes`, `read only = no`, `valid users = <user>`, sane masks.
- `smbpasswd -a` + `-e` for the user (note: separate from Linux password).
- `chown` the mountpoint to the user.
- `systemctl restart smbd && systemctl enable smbd`.
- Print connect strings for Windows (`\\ip\share`), iOS (`smb://ip`), Android.

### 8. `.gitignore`

Must ignore the **real secrets file** (`nas-notifier.env`, `/etc/nas-notifier.env`),
`__pycache__/`, `*.pyc`, and editor/OS cruft. The `.example` file **is** committed;
the real one never is.

### 9. `README.md`

- Open with the TurtlEngine ASCII banner (see §11) in a fenced code block.
- What it sets up (table).
- Quick start: clone → `sudo ./scripts/install.sh` → `tailscale up` →
  `ssh-copy-id` from laptop → `tmux new -s main`.
- The reconnect-from-anywhere command:
  `ssh user@<ip> -t tmux attach -t main`.
- Telegram notifier setup walkthrough (BotFather → token → chat id → env file →
  test).
- The other scripts' usage.
- Security notes (token secrecy, disable SSH password auth once keys work,
  Tailscale = nothing exposed to the internet).

### 10. `motd/turtlengine.txt` + install step (login banner)

Optional but on-brand: a login banner so every SSH session greets you with
TurtlEngine. `install.sh` should copy it to `/etc/motd` (or drop a script in
`/etc/update-motd.d/` that `cat`s it). Keep it idempotent (overwrite is fine).
Use the ASCII art in §11.

### 11. TurtlEngine ASCII banner (use verbatim in README + MOTD)

Use this exact art for the login banner (`motd/turtlengine.txt`) and the README
header. `notify.py` should also print it at the start of each run — use the
Python snippet below verbatim (raw string, so the backslashes are literal):

```python
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
```

For the README/MOTD text file, use the same art without the Python wrapper:

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

---

## Cross-cutting requirements

- **Idempotency:** every script checks state before acting; re-running is safe.
  This matters because the same repo will later provision a second machine (a
  dedicated GPU server).
- **No secrets in git:** the bot token lives only in `/etc/nas-notifier.env`
  (chmod 600), gitignored.
- **No password in any SSH flow:** key-based auth only; the notifier never
  handles a password.
- **Validate before shipping:** `python3 -m py_compile notifier/notify.py` and
  `bash -n` on every script. Smoke-test `notify.py`'s message builder with a fake
  config (no real token) to confirm output and graceful fallbacks (no Tailscale →
  line omitted; no tmux → "not started").
- **Set executable bits** on all `.sh` files and `notify.py`.
- **BIOS/Legacy awareness:** nothing here touches the bootloader (that's done at
  install time), but don't assume UEFI paths anywhere.

---

## Manual steps that stay OUT of the scripts (document in README, don't automate)

- **`sudo tailscale up`** — interactive browser login.
- **SSH key generation + `ssh-copy-id`** — these belong on the *client* machines
  (laptop, phone), not the server.
- **DHCP reservation / static IP** — done on the router.
- **Filling `/etc/nas-notifier.env`** with the real token and chat id.
- **Disabling SSH password auth** (`PasswordAuthentication no`) — suggest it in
  the README once key login is confirmed working, but don't flip it in a script
  (risks locking the user out if keys aren't set up yet).

---

## Suggested build order for Claude Code

1. Scaffold the directory tree + `.gitignore`.
2. `notifier/notify.py` + `nas-notifier.env.example` → smoke-test the builder.
3. `systemd/nas-notifier.service` + `scripts/setup-notifier.sh`.
4. `scripts/install.sh` (wire in the notifier prompt + MOTD copy).
5. `scripts/mount-drive.sh`, `scripts/setup-samba.sh`.
6. `motd/turtlengine.txt` + `README.md` (both using the §11 banner).
7. Validate everything (`py_compile`, `bash -n`, executable bits), then
   `git init` and first commit.

---

## Current state (as built) — reconciliation with this plan

> Everything above is the **original brainstorming spec**, kept verbatim as a
> historical record of the idea. The implementation has since evolved; where the
> code differs from the plan above, the **code is authoritative**. This section
> records those deviations so the document reflects the current state without
> rewriting the original intent. (Updated 2026-10-17.)

**New components not in the original tree**
- `scripts/turtlengine` — a local Bash CLI to manage the box after install:
  `status`, `storage list|mount`, `share add|list|remove`, and an interactive
  `menu`. Discovery is read-only; mutations require root + confirmation. Installed
  to `/opt/turtlengine/scripts/` and symlinked to `/usr/local/bin/turtlengine`.
- `tests/` — a reliability test suite (stdlib `unittest` for `notify.py`; Bash
  tests with path overrides and a stubbed `systemctl`). Root-gated tests self-skip
  (exit 77) when they cannot run safely.
- `docs/requirement/CONTEXT.md` — this document.

**install.sh**
- Runs as a **guided, phased installer**: tidy `[n/6]` progress with full command
  output sent to `/var/log/turtlengine-install.log` (a `--verbose` flag streams it
  live instead). After the base phases it **interactively configures Telegram**
  (token from @BotFather -- opened via telegram.me -- chat id auto-detected from
  the Bot API with a manual fallback, asking before overwriting existing
  credentials, optional test send) and then **offers to run `tailscale up`** for
  sign-in.
- The **swapfile step (§1.5) was removed entirely** and is not recreated — the
  box runs with no configured swap (`SWAP_SIZE_GB`, `vm.swappiness`, and the fstab
  swap line are all gone).
- The **TurtlEngine banner prints first**, before any provisioning work.
- Installs the `turtlengine` CLI + helper scripts to `/opt/turtlengine/scripts`.
- Ends with a **two-part summary**: what was done automatically vs. the remaining
  manual steps. It does **not** claim Tailscale or Telegram are configured.
- Does **not** format disks, mount drives, or create shares — storage and shares
  are entirely user-driven afterwards via the CLI/helpers.

**notify.py**
- The boot message is a **status card**: host, uptime, CPU / RAM / disk / load /
  kernel (from `/proc` + `os.statvfs`), plus a **tap-to-copy** fenced code block
  with the reconnect command for each reachable address.
- The reconnect command is **`tmux new-session -A -s <session>`** (attach-or-
  create), not `tmux attach -t`, so the first connect after a cold boot works.
- MarkdownV2 escaping is **split**: full escaping for prose, and a narrower
  code-span escape (only `` ` `` and `\`) inside code spans, so code spans don't
  render stray backslashes.
- The tmux liveness check targets the **invoking user's** tmux socket
  (`/tmp/tmux-<uid>/default`), not root's.

**systemd/nas-notifier.service**
- Hardened: `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, plus kernel/
  cgroup protections. `PrivateTmp` is deliberately **not** set (the notifier needs
  the user's `/tmp` tmux socket); `ProtectSystem=full` (not `strict`) keeps `/run`
  writable for the tailscaled socket.

**mount-drive.sh**
- `--format` **refuses a device with mounted or active-swap partitions** (which
  also protects the OS/root disk), prints the current layout, then requires the
  device name to be re-typed before erasing. `/etc/fstab` is backed up before the
  `nofail` entry is appended.

**setup-samba.sh**
- Shares are **independent and user-chosen** — any directory/user/name, not tied
  to a single data disk, and never auto-created at install.
- **Warns and re-confirms** if the path is on the root filesystem or a
  system-critical location.
- Ownership changes are **opt-in and non-recursive** (top-level only, on an
  explicit yes) — not an automatic `chown`.
- Backs up `smb.conf`, validates with `testparm` (restoring on failure), and
  **reloads** smbd rather than restarting it.

**Unchanged from the plan:** idempotency; no secrets in git
(`/etc/nas-notifier.env` is chmod 600 and gitignored); key-based SSH only;
BIOS/Legacy awareness; the ASCII banner (§11); and the "manual steps stay out of
the scripts" list.
