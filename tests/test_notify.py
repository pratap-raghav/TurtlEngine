#!/usr/bin/env python3
"""Unit tests for notifier/notify.py -- stdlib unittest only.

No real Telegram bot, no real network: urllib and subprocess are mocked. Run
with:  python3 -m unittest discover -s tests  (or tests/run.sh)

Covers service-reliability checklist items that need no root or hardware:
  0 Fresh boot      -> usable SSH command with no tmux session
  1 Existing session-> tmux check targets the correct user's server
  2 LAN only        -> message works without a Tailscale IP
  3 Network down    -> wait_for_connectivity exits visibly after the timeout
  4 Bad credentials -> API error reported, bot token never exposed
"""

import getpass
import importlib.util
import io
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest import mock

# Load notify.py by path (it lives in ../notifier, not on sys.path).
_HERE = os.path.dirname(os.path.abspath(__file__))
_NOTIFY = os.path.join(_HERE, os.pardir, "notifier", "notify.py")
_spec = importlib.util.spec_from_file_location("notify", _NOTIFY)
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)


def base_info(**overrides):
    info = {
        "hostname": "turtle-nas",
        "lan_ip": "192.168.1.42",
        "tailscale_ip": "100.101.102.103",
        "uptime": "1h 3m",
        "tmux_alive": False,
        "ssh_user": "ghost",
        "tmux_session": "main",
    }
    info.update(overrides)
    return info


class TestMessageBuilder(unittest.TestCase):
    def test_item0_fresh_boot_no_session_has_usable_command(self):
        """Item 0: no tmux session, but a complete, usable SSH command."""
        msg = notify.build_message(base_info(tmux_alive=False))
        self.assertIn("not started", msg)
        # new-session -A so the first connect after boot actually works.
        self.assertIn(
            "ssh ghost@192.168.1.42 -t tmux new-session -A -s main", msg
        )
        self.assertNotIn("tmux attach -t", msg)

    def test_item2_lan_only_without_tailscale(self):
        """Item 2: no Tailscale IP -> LAN line present, Tailscale line absent."""
        msg = notify.build_message(base_info(tailscale_ip=None))
        self.assertIn("Home / LAN", msg)
        self.assertIn("ssh ghost@192.168.1.42", msg)
        self.assertNotIn("Anywhere / Tailscale", msg)
        self.assertNotIn("100.101.102.103", msg)

    def test_tailscale_only_without_lan(self):
        msg = notify.build_message(base_info(lan_ip=None))
        self.assertIn("Anywhere / Tailscale", msg)
        self.assertNotIn("Home / LAN", msg)

    def test_neither_ip_available(self):
        msg = notify.build_message(base_info(lan_ip=None, tailscale_ip=None))
        self.assertIn("No LAN or Tailscale address available", msg)
        self.assertNotIn("ssh ", msg)

    def test_session_alive_label(self):
        msg = notify.build_message(base_info(tmux_alive=True))
        self.assertIn("alive", msg)
        self.assertNotIn("not started", msg)

    def test_markdownv2_escaping_in_prose_but_not_code(self):
        """A dotted/dashed host is escaped in prose; clean inside a code span."""
        msg = notify.build_message(base_info(hostname="turtle-nas.local"))
        # Inside the `code` span the host must appear literally (no backslash).
        self.assertIn("`turtle-nas.local`", msg)
        # The LAN command code span keeps the IP literal too.
        self.assertIn("`ssh ghost@192.168.1.42 -t tmux new-session -A -s main`", msg)

    def test_custom_session_name_flows_through(self):
        msg = notify.build_message(base_info(tmux_session="work"))
        self.assertIn("-s work`", msg)


class TestTmuxCheck(unittest.TestCase):
    def test_item1_targets_other_users_socket(self):
        """Item 1: a distinct user -> tmux points at /tmp/tmux-<uid>/default."""
        me = getpass.getuser()
        uid = os.getuid()
        cmd = notify.tmux_check_cmd("main", user=me, current_user="someone-else")
        self.assertEqual(
            cmd,
            ["tmux", "-S", "/tmp/tmux-%d/default" % uid, "has-session", "-t", "main"],
        )

    def test_same_user_uses_plain_tmux(self):
        me = getpass.getuser()
        cmd = notify.tmux_check_cmd("main", user=me, current_user=me)
        self.assertEqual(cmd, ["tmux", "has-session", "-t", "main"])

    def test_unknown_user_falls_back_to_plain_tmux(self):
        cmd = notify.tmux_check_cmd(
            "main", user="definitely-no-such-user-xyz", current_user="root"
        )
        self.assertEqual(cmd, ["tmux", "has-session", "-t", "main"])

    def test_session_alive_reads_returncode(self):
        with mock.patch.object(notify.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(notify.tmux_session_alive("main", "ghost"))
            run.return_value = mock.Mock(returncode=1)
            self.assertFalse(notify.tmux_session_alive("main", "ghost"))

    def test_session_alive_handles_missing_tmux(self):
        with mock.patch.object(notify.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(notify.tmux_session_alive("main", "ghost"))


class TestConnectivity(unittest.TestCase):
    def test_item3_network_unavailable_times_out(self):
        """Item 3: never reachable -> returns False, no real sleeping/waiting."""
        clock = {"t": 0.0}

        def fake_monotonic():
            clock["t"] += notify.POLL_INTERVAL_S
            return clock["t"]

        with mock.patch.object(notify.time, "monotonic", side_effect=fake_monotonic), \
             mock.patch.object(notify.time, "sleep") as sleep, \
             mock.patch.object(
                 notify.urllib.request, "urlopen",
                 side_effect=urllib.error.URLError("down")):
            self.assertFalse(notify.wait_for_connectivity())
        self.assertTrue(sleep.called)  # it did back off between polls

    def test_connectivity_ok_on_response(self):
        with mock.patch.object(notify.time, "monotonic", return_value=0.0), \
             mock.patch.object(notify.urllib.request, "urlopen",
                               return_value=mock.Mock()):
            self.assertTrue(notify.wait_for_connectivity())

    def test_http_error_counts_as_online(self):
        err = urllib.error.HTTPError(
            notify.TELEGRAM_API, 404, "Not Found", {}, io.BytesIO(b"")
        )
        with mock.patch.object(notify.time, "monotonic", return_value=0.0), \
             mock.patch.object(notify.urllib.request, "urlopen", side_effect=err):
            self.assertTrue(notify.wait_for_connectivity())


class TestSendMessage(unittest.TestCase):
    SECRET = "123456789:AAExampleSecretBotTokenShouldNeverLeak"

    def test_success_returns_true(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        with mock.patch.object(notify.urllib.request, "urlopen", return_value=resp):
            self.assertTrue(notify.send_message(self.SECRET, "42", "hi"))

    def test_item4_api_error_reported_without_leaking_token(self):
        """Item 4: HTTP error is surfaced, but the bot token never appears."""
        err = urllib.error.HTTPError(
            "https://api.telegram.org/bot%s/sendMessage" % self.SECRET,
            400, "Bad Request", {},
            io.BytesIO(b'{"ok":false,"description":"chat not found"}'),
        )
        buf = io.StringIO()
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=err):
            with redirect_stderr(buf):
                ok = notify.send_message(self.SECRET, "42", "hi")
        self.assertFalse(ok)
        stderr = buf.getvalue()
        self.assertIn("400", stderr)              # the error is visible
        self.assertNotIn(self.SECRET, stderr)     # the token is not

    def test_network_error_returns_false(self):
        buf = io.StringIO()
        with mock.patch.object(notify.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("no route")):
            with redirect_stderr(buf):
                self.assertFalse(notify.send_message(self.SECRET, "42", "hi"))
        self.assertNotIn(self.SECRET, buf.getvalue())


class TestEnvParser(unittest.TestCase):
    def test_canonical_format_parses(self):
        """The one documented env format round-trips through load_env."""
        import tempfile
        content = (
            "# a comment\n"
            "; another comment style\n"
            "\n"
            "TELEGRAM_BOT_TOKEN=123:ABC\n"
            "TELEGRAM_CHAT_ID=42\n"
            "SSH_USER=ghost\n"
            "TMUX_SESSION=main\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            cfg = notify.load_env(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg["TELEGRAM_BOT_TOKEN"], "123:ABC")
        self.assertEqual(cfg["TELEGRAM_CHAT_ID"], "42")
        self.assertEqual(cfg["SSH_USER"], "ghost")
        self.assertEqual(cfg["TMUX_SESSION"], "main")
        self.assertNotIn("# a comment", cfg)

    def test_missing_file_returns_empty(self):
        self.assertEqual(notify.load_env("/no/such/file.env"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
