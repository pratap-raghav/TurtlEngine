#!/usr/bin/env python3
"""Unit tests for notifier/bot.py -- stdlib unittest only.

No real Telegram, no network, no root. Focuses on the security-critical bits:
  - the authorisation gate (both sender id AND chat id must match),
  - command parsing,
  - handle_update() ignoring unauthorized updates,
  - permission-aware command handlers with distinct states,
  - /logs redacting the token.
"""

import importlib.util
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_BOT = os.path.join(_HERE, os.pardir, "notifier", "bot.py")
_spec = importlib.util.spec_from_file_location("bot", _BOT)
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)   # bot.py adds its dir to sys.path and imports notify

CONFIG = {
    "SSH_USER": "ghost",
    "TMUX_SESSION": "main",
    "TELEGRAM_BOT_TOKEN": "123:SECRETtoken",
    "TELEGRAM_CHAT_ID": "42",
    "AUTHORIZED_USER_ID": "7",
}


def msg(user_id=7, chat_id=42, text="/ping"):
    return {"from": {"id": user_id}, "chat": {"id": chat_id}, "text": text}


class TestAuthorization(unittest.TestCase):
    def test_correct_user_and_chat_pass(self):
        self.assertTrue(bot.is_authorized(msg(7, 42), "7", "42"))

    def test_wrong_user_rejected(self):
        self.assertFalse(bot.is_authorized(msg(999, 42), "7", "42"))

    def test_wrong_chat_rejected(self):
        self.assertFalse(bot.is_authorized(msg(7, 999), "7", "42"))

    def test_missing_from_rejected(self):
        self.assertFalse(bot.is_authorized({"chat": {"id": 42}}, "7", "42"))

    def test_empty_allowlist_rejected(self):
        self.assertFalse(bot.is_authorized(msg(7, 42), "", ""))


class TestParseCommand(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(bot.parse_command("/status"), "status")

    def test_strips_bot_suffix_and_args(self):
        self.assertEqual(bot.parse_command("/ssh_local@TurtlEngineBot x"), "ssh_local")

    def test_case_insensitive(self):
        self.assertEqual(bot.parse_command("/HELP"), "help")

    def test_non_command(self):
        self.assertEqual(bot.parse_command("hello"), "")
        self.assertEqual(bot.parse_command(""), "")


class TestHandleUpdate(unittest.TestCase):
    def _run(self, update):
        with mock.patch.object(bot.notify, "send_message") as send:
            bot.handle_update(update, CONFIG, "123:tok", "42", "7")
        return send

    def test_authorized_ping_replies(self):
        send = self._run({"update_id": 1, "message": msg(7, 42, "/ping")})
        send.assert_called_once()
        self.assertIn("pong", send.call_args[0][2])

    def test_unauthorized_user_ignored(self):
        send = self._run({"update_id": 1, "message": msg(999, 42, "/ping")})
        send.assert_not_called()

    def test_unauthorized_chat_ignored(self):
        send = self._run({"update_id": 1, "message": msg(7, 999, "/ping")})
        send.assert_not_called()

    def test_non_message_ignored(self):
        send = self._run({"update_id": 1, "edited_channel_post": {}})
        send.assert_not_called()

    def test_unknown_command_replies_help_hint(self):
        send = self._run({"update_id": 1, "message": msg(7, 42, "/frobnicate")})
        send.assert_called_once()
        self.assertIn("Unknown command", send.call_args[0][2])


class TestHandlers(unittest.TestCase):
    def test_help_lists_commands(self):
        out = bot.cmd_help(CONFIG)
        for c in ("/status", "/ip", "/ssh_local", "/logs", "/help"):
            self.assertIn(c, out)

    def test_ip_both_present(self):
        with mock.patch.object(bot.notify, "get_lan_ip", return_value="192.168.0.5"), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value="100.64.0.9"):
            out = bot.cmd_ip(CONFIG)
        self.assertIn("192.168.0.5", out)
        self.assertIn("100.64.0.9", out)

    def test_ip_missing_states_are_distinct(self):
        with mock.patch.object(bot.notify, "get_lan_ip", return_value=None), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value=None):
            out = bot.cmd_ip(CONFIG)
        self.assertIn("unavailable", out)       # LAN
        self.assertIn("not connected", out)     # Tailscale

    def test_ssh_local_without_lan_explains(self):
        with mock.patch.object(bot.notify, "get_lan_ip", return_value=None):
            out = bot.cmd_ssh_local(CONFIG)
        self.assertNotIn("ssh ghost@", out)
        self.assertIn("couldn't be determined", out)

    def test_ssh_local_with_lan_gives_command(self):
        with mock.patch.object(bot.notify, "get_lan_ip", return_value="192.168.0.5"):
            out = bot.cmd_ssh_local(CONFIG)
        # Inline code span (single backticks) so it's tap-to-copy on mobile.
        self.assertIn("`ssh ghost@192.168.0.5 -t tmux new-session -A -s main`", out)
        self.assertNotIn("```", out)

    def test_ssh_remote_without_tailscale_explains(self):
        with mock.patch.object(bot.notify, "get_tailscale_ip", return_value=None):
            out = bot.cmd_ssh_remote(CONFIG)
        self.assertIn("Tailscale isn't connected", out)
        self.assertIn("tailscale up", out)
        self.assertNotIn("ssh ghost@", out)

    def test_sessions_permission_denied_distinct(self):
        with mock.patch.object(bot, "get_sessions", return_value=("denied", [])):
            out = bot.cmd_sessions(CONFIG)
        self.assertIn("🔒", out)
        self.assertIn("permission denied", out)

    def test_sessions_none_running(self):
        with mock.patch.object(bot, "get_sessions", return_value=("none", [])):
            out = bot.cmd_sessions(CONFIG)
        self.assertIn("No tmux sessions", out)

    def test_sessions_lists_and_flags_main(self):
        with mock.patch.object(bot, "get_sessions", return_value=("ok", ["main", "work"])):
            out = bot.cmd_sessions(CONFIG)
        self.assertIn("work", out)
        self.assertIn("running", out)

    def test_storage_formats_rows(self):
        rows = [("/mnt/nas", 45.0, 440.0, 10, "ext4")]
        with mock.patch.object(bot, "get_storage", return_value=rows):
            out = bot.cmd_storage(CONFIG)
        self.assertIn("/mnt/nas", out)
        self.assertIn("ext4", out)
        self.assertIn("```", out)

    def test_storage_dedupes_bind_mounts_by_device(self):
        # Our sandbox bind-mounts /etc, /home, ... off the root device; a second
        # real disk is a distinct device. The root rows must collapse to one ("/")
        # while the extra disk survives.
        mounts = (
            "/dev/sda1 / ext4 rw 0 0\n"
            "/dev/sda1 /etc ext4 ro 0 0\n"
            "/dev/sda1 /home ext4 ro 0 0\n"
            "/dev/sda1 /usr ext4 ro 0 0\n"
            "/dev/sdb1 /mnt/data ext4 rw 0 0\n"
        )
        svfs = mock.Mock(f_blocks=100, f_frsize=1024 ** 3, f_bavail=90)

        def fake_stat(path):
            return mock.Mock(st_dev=2050 if path == "/mnt/data" else 2049)

        with mock.patch("builtins.open", mock.mock_open(read_data=mounts)), \
                mock.patch.object(bot.os, "stat", side_effect=fake_stat), \
                mock.patch.object(bot.os, "statvfs", return_value=svfs):
            rows = bot.get_storage()
        self.assertEqual(sorted(r[0] for r in rows), ["/", "/mnt/data"])

    def test_logs_denied_distinct(self):
        with mock.patch.object(bot, "get_logs", return_value="denied"):
            out = bot.cmd_logs(CONFIG)
        self.assertIn("🔒", out)
        self.assertIn("permissions", out)

    def test_logs_unavailable_distinct(self):
        with mock.patch.object(bot, "get_logs", return_value=None):
            out = bot.cmd_logs(CONFIG)
        self.assertIn("journalctl isn't available", out)


SMB_SAMPLE = """
[global]
   workgroup = WORKGROUP
[Project]
   path = /mnt/nas-1tb/project
   valid users = raghav
   read only = no
[Storage4TB]
   path = /mnt/nas-4tb/backups
   valid users = @devs raghav
[printers]
   path = /var/spool/samba
"""


class TestGetShares(unittest.TestCase):
    def _parse(self, text):
        with mock.patch("builtins.open", mock.mock_open(read_data=text)):
            return bot.get_shares("ignored")

    def test_parses_real_shares_only(self):
        shares = self._parse(SMB_SAMPLE)
        names = [s["name"] for s in shares]
        self.assertEqual(names, ["Project", "Storage4TB"])  # global/printers dropped
        self.assertEqual(shares[0]["path"], "/mnt/nas-1tb/project")
        self.assertEqual(shares[0]["users"], "raghav")

    def test_section_without_path_is_skipped(self):
        self.assertEqual(self._parse("[Empty]\n   valid users = x\n"), [])

    def test_unreadable_returns_none(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertIsNone(bot.get_shares("nope"))


class TestCmdShares(unittest.TestCase):
    def _run(self, shares, lan="192.168.0.116", ts=None):
        with mock.patch.object(bot, "get_shares", return_value=shares), \
             mock.patch.object(bot.notify, "get_lan_ip", return_value=lan), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value=ts):
            return bot.cmd_shares(CONFIG)

    def test_lists_all_os_connect_strings(self):
        out = self._run([{"name": "Project", "path": "/mnt/p", "users": "raghav"}])
        # Windows UNC: backslashes doubled for the MarkdownV2 code span.
        self.assertIn("`\\\\\\\\192.168.0.116\\\\Project`", out)
        self.assertIn("smb://192.168.0.116/Project", out)
        self.assertIn("mount -t cifs //192.168.0.116/Project", out)
        for label in ("Windows", "macOS", "Linux", "Android", "iOS"):
            self.assertIn(label, out)

    def test_none_when_unreadable(self):
        out = self._run(None)
        self.assertIn("Can't read the Samba config", out)

    def test_empty_points_to_share_add(self):
        out = self._run([])
        self.assertIn("No shared drives", out)
        self.assertIn("turtlengine share add", out)

    def test_tailscale_alternative_shown_when_both(self):
        out = self._run([{"name": "P", "path": "/mnt/p", "users": ""}],
                        lan="192.168.0.116", ts="100.64.0.9")
        self.assertIn("100.64.0.9", out)

    def test_group_user_is_stripped_to_hint(self):
        out = self._run([{"name": "S", "path": "/mnt/s", "users": "@devs raghav"}])
        self.assertIn("user `devs`", out)


class TestCmdDrives(unittest.TestCase):
    def test_lists_names_only_no_connect_strings(self):
        shares = [{"name": "Project", "path": "/mnt/p", "users": "raghav"},
                  {"name": "Backups", "path": "/mnt/b", "users": ""}]
        with mock.patch.object(bot, "get_shares", return_value=shares):
            out = bot.cmd_drives(CONFIG)
        self.assertIn("Project", out)
        self.assertIn("Backups", out)
        self.assertNotIn("smb://", out)        # names only, not the full /shares
        self.assertNotIn("Windows", out)

    def test_empty_points_to_add_share(self):
        with mock.patch.object(bot, "get_shares", return_value=[]):
            out = bot.cmd_drives(CONFIG)
        self.assertIn("No shared drives", out)
        self.assertIn("set one up", out)        # points at /add_share

    def test_unreadable_distinct(self):
        with mock.patch.object(bot, "get_shares", return_value=None):
            out = bot.cmd_drives(CONFIG)
        self.assertIn("Can't read the Samba config", out)


class TestCmdAddShare(unittest.TestCase):
    def test_explains_and_gives_command_without_acting(self):
        out = bot.cmd_add_share(CONFIG)
        self.assertIn("`sudo turtlengine share add`", out)   # tap-to-copy
        self.assertIn("admin rights", out)                   # explains the why
        self.assertIn("/shares", out)                        # no special chars


class TestSetMyCommands(unittest.TestCase):
    def test_posts_commands_as_json(self):
        import json as _json
        captured = {}

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["ctype"] = request.get_header("Content-type")
            return FakeResp()

        with mock.patch.object(bot.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok = bot.set_my_commands("123:tok")
        self.assertTrue(ok)
        self.assertIn("/setMyCommands", captured["url"])
        self.assertEqual(captured["ctype"], "application/json")
        body = _json.loads(captured["body"])
        names = [c["command"] for c in body["commands"]]
        self.assertIn("shares", names)
        self.assertIn("add_share", names)

    def test_failure_is_swallowed(self):
        with mock.patch.object(bot.urllib.request, "urlopen",
                               side_effect=OSError("boom")):
            self.assertFalse(bot.set_my_commands("123:tok"))


class TestCommandHelpConsistency(unittest.TestCase):
    def test_every_menu_command_has_a_handler(self):
        for name, desc in bot.COMMAND_HELP:
            self.assertIn(name, bot.COMMANDS, "%s has no handler" % name)
            # Telegram's setMyCommands constraints.
            self.assertRegex(name, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(1 <= len(desc) <= 256)


class TestSessionNames(unittest.TestCase):
    def test_sanitize_keeps_safe_chars_only(self):
        self.assertEqual(bot.sanitize_session_name("my proj!!/x.."), "myprojx")

    def test_sanitize_truncates(self):
        self.assertEqual(len(bot.sanitize_session_name("a" * 99)), bot.MAX_SESSION_NAME)

    def test_next_name_picks_first_free(self):
        self.assertEqual(bot.next_session_name(set()), "work")
        self.assertEqual(bot.next_session_name({"work"}), "work2")
        self.assertEqual(bot.next_session_name({"work", "work2"}), "work3")


class TestCmdNewTerminal(unittest.TestCase):
    # The bot is read-only: /new_terminal must only HAND BACK a command, never
    # start a session itself. These tests pin that contract.
    def test_named_gives_ssh_command_only(self):
        with mock.patch.object(bot, "get_sessions", return_value=("ok", ["main"])), \
             mock.patch.object(bot.notify, "get_lan_ip", return_value="192.168.0.5"), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value=None):
            out = bot.cmd_new_terminal(CONFIG, "proj2")
        self.assertIn("`ssh ghost@192.168.0.5 -t tmux new-session -A -s proj2`", out)

    def test_does_not_run_any_subprocess_to_create(self):
        # No tmux/ssh is spawned to create anything -- only get_sessions reads.
        with mock.patch.object(bot, "get_sessions", return_value=("ok", [])) as gs, \
             mock.patch.object(bot.subprocess, "run") as run, \
             mock.patch.object(bot.notify, "get_lan_ip", return_value="192.168.0.5"), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value=None):
            bot.cmd_new_terminal(CONFIG, "x")
        gs.assert_called_once()
        run.assert_not_called()

    def test_auto_names_when_no_arg(self):
        with mock.patch.object(bot, "get_sessions", return_value=("ok", ["work"])), \
             mock.patch.object(bot.notify, "get_lan_ip", return_value="192.168.0.5"), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value=None):
            out = bot.cmd_new_terminal(CONFIG, "")
        self.assertIn("-s work2", out)     # 'work' taken -> work2

    def test_unusable_name_rejected(self):
        with mock.patch.object(bot, "get_sessions") as gs:
            out = bot.cmd_new_terminal(CONFIG, "!!!")
        gs.assert_not_called()
        self.assertIn("won't work", out)

    def test_no_address_falls_back_to_local_command(self):
        with mock.patch.object(bot, "get_sessions", return_value=("ok", [])), \
             mock.patch.object(bot.notify, "get_lan_ip", return_value=None), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value=None):
            out = bot.cmd_new_terminal(CONFIG, "api")
        self.assertIn("`tmux new-session -A -s api`", out)

    def test_arg_reaches_handler_through_dispatch(self):
        h = mock.Mock(return_value="ok")
        with mock.patch.dict(bot.COMMANDS, {"new_terminal": h}), \
             mock.patch.object(bot.notify, "send_message"):
            bot.handle_update({"update_id": 1, "message": msg(7, 42, "/new_terminal foo")},
                              CONFIG, "123:tok", "42", "7")
        h.assert_called_once_with(CONFIG, "foo")


class TestWorkstationCommands(unittest.TestCase):
    def test_gpu_shows_live_stats(self):
        gpus = [{"name": "RTX 3090", "mem_total_mib": 24576, "mem_used_mib": 1536,
                 "util_pct": 12, "temp_c": 45}]
        with mock.patch.object(bot.notify, "get_gpus", return_value=gpus):
            out = bot.cmd_gpu(CONFIG)
        self.assertIn("RTX 3090", out)
        self.assertIn("load 12%", out)
        self.assertIn("45", out)        # temperature

    def test_gpu_absent_is_explained(self):
        with mock.patch.object(bot.notify, "get_gpus", return_value=[]):
            out = bot.cmd_gpu(CONFIG)
        self.assertIn("No GPU detected", out)

    def test_top_formats_table(self):
        rows = [("python", "95.0", "12.3"), ("chrome", "4.0", "8.1")]
        with mock.patch.object(bot, "get_top", return_value=rows):
            out = bot.cmd_top(CONFIG)
        self.assertIn("python", out)
        self.assertIn("95.0", out)
        self.assertIn("```", out)       # fenced table

    def test_temp_lists_sensors(self):
        with mock.patch.object(bot, "get_temps",
                               return_value=[("x86_pkg_temp", 52.0), ("RTX 3090", 45.0)]):
            out = bot.cmd_temp(CONFIG)
        self.assertIn("x86_pkg_temp", out)
        self.assertIn("52", out)

    def test_net_lists_peers_online_first(self):
        peers = [("phone", "100.0.0.2", False), ("laptop", "100.0.0.3", True)]
        with mock.patch.object(bot, "get_tailscale_peers", return_value=("ok", peers)):
            out = bot.cmd_net(CONFIG)
        self.assertIn("laptop", out)
        self.assertIn("phone", out)
        self.assertLess(out.index("laptop"), out.index("phone"))   # online first

    def test_net_not_installed_distinct(self):
        with mock.patch.object(bot, "get_tailscale_peers",
                               return_value=("not_installed", [])):
            out = bot.cmd_net(CONFIG)
        self.assertIn("isn't installed", out)

    def test_services_builds_urls_and_flags_local(self):
        with mock.patch.object(bot, "get_listening_ports",
                               return_value=[(22, True), (631, False), (8080, True)]), \
             mock.patch.object(bot.notify, "get_lan_ip", return_value="192.168.0.5"), \
             mock.patch.object(bot.notify, "get_tailscale_ip", return_value=None):
            out = bot.cmd_services(CONFIG)
        self.assertIn("SSH", out)                       # port 22 labelled
        self.assertIn("`http://192.168.0.5:8080`", out)  # tap-to-copy URL
        self.assertIn("local only", out)                 # 631 loopback-only


class TestPortParsing(unittest.TestCase):
    def test_marks_reachable_vs_loopback(self):
        ss = ("LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n"
              "LISTEN 0 128 127.0.0.1:631 0.0.0.0:*\n"
              "LISTEN 0 128 [::]:22 [::]:*\n")
        fake = mock.Mock(returncode=0, stdout=ss)
        with mock.patch.object(bot.subprocess, "run", return_value=fake):
            ports = bot.get_listening_ports()
        self.assertEqual(dict(ports), {22: True, 631: False, 8080: True})

    def test_port_bound_both_ways_is_reachable(self):
        ss = ("LISTEN 0 128 127.0.0.1:5432 0.0.0.0:*\n"
              "LISTEN 0 128 0.0.0.0:5432 0.0.0.0:*\n")
        fake = mock.Mock(returncode=0, stdout=ss)
        with mock.patch.object(bot.subprocess, "run", return_value=fake):
            ports = bot.get_listening_ports()
        self.assertEqual(dict(ports), {5432: True})


class TestLogRedaction(unittest.TestCase):
    def test_token_is_redacted_from_logs(self):
        token = "123:SECRETtoken"
        fake = mock.Mock(returncode=0, stdout="line with %s inside\n" % token)
        with mock.patch.object(bot.subprocess, "run", return_value=fake):
            out = bot.get_logs(token=token)
        self.assertNotIn(token, out)
        self.assertIn("***", out)

    def test_nonzero_returncode_is_denied(self):
        fake = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(bot.subprocess, "run", return_value=fake):
            self.assertEqual(bot.get_logs(token="x"), "denied")


if __name__ == "__main__":
    unittest.main(verbosity=2)
