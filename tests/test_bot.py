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
