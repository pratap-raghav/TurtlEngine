# TurtlEngine tests

Run everything:

```bash
bash tests/run.sh
```

- **Python notifier tests** (`test_notify.py`) run everywhere — stdlib
  `unittest` only, with `urllib` and `subprocess` mocked. No real Telegram bot,
  no network.
- **Bash tests** redirect every real path to a throwaway directory and stub
  `systemctl`. They need elevated privileges; when they can't run safely they
  exit `77` (SKIP) rather than fail.

## Service-reliability checklist coverage

| # | Scenario | Test | How it runs |
| --- | --- | --- | --- |
| 0 | Fresh boot — no tmux session, still a usable SSH command | `test_notify.py::test_item0_fresh_boot...` | automated, everywhere |
| 1 | Existing session — check targets the correct user's tmux server | `test_notify.py::test_item1_targets_other_users_socket` | automated, everywhere |
| 2 | LAN only — message works without a Tailscale IP | `test_notify.py::test_item2_lan_only...` | automated, everywhere |
| 3 | Network unavailable — waits for the timeout then exits visibly | `test_notify.py::test_item3_network_unavailable_times_out` | automated (clock mocked) |
| 4 | Invalid credentials — API error reported, token never exposed | `test_notify.py::test_item4_api_error...` | automated, everywhere |
| 5 | Repeated setup — secret preserved, config not duplicated | `test_setup_notifier.sh` | root or `fakeroot`, else SKIP |
| 6 | Storage failure — missing UUID writes no fstab entry | `test_mount_drive.sh` | real root + `losetup`, else SKIP |
| 7 | Reboot recovery — SSH up, data mounts, notifier runs | manual (see below) | integration only |

Additional coverage for the later change waves:

| Area | Test | How it runs |
| --- | --- | --- |
| Banner printed at install start, swap step removed, no auto share/format | `test_install.sh` | static, everywhere |
| Banner printed by the notifier | `test_notify.py::TestBanner` | automated, everywhere |
| CLI: invalid args, share list/remove, multiple independent shares, cancel, logs/notify, bot subcommand | `test_turtlengine.sh` | automated (fixture smb.conf), everywhere |
| Command bot: auth gate (sender id + chat id), command parsing, unauthorized ignored, permission-aware handlers, /logs token redaction | `test_bot.py` | automated, everywhere |

### Item 7 — reboot recovery (manual integration check)

Not automated: it requires a real boot cycle on the target NAS. After an
install, reboot and confirm:

1. `systemctl is-enabled ssh` → `enabled`, and you can SSH in immediately.
2. `findmnt <data-mountpoint>` shows any data drive you mounted is back after
   the reboot — and, because its fstab entry uses `nofail`, boot still succeeds
   even if that drive is pulled. (TurtlEngine configures no swap, so there is
   nothing swap-related to verify here.)
3. `journalctl -u nas-notifier.service -b` shows one clean run (and a Telegram
   message arrived). On failure the unit is `failed`, not looping.

## Notes

- The notifier's env format is parsed identically by systemd's `EnvironmentFile`
  and `notify.py`; `test_notify.py::TestEnvParser` locks in that one format.
- `test_mount_drive.sh` only ever touches a sparse loopback image and a
  throwaway fstab file — never a real disk or the host's `/etc/fstab`.
