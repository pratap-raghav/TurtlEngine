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
        # The command lives in an inline code span and stays literal (no escapes).
        self.assertIn("`ssh ghost@192.168.1.42 -t tmux new-session -A -s main`", msg)

    def test_commands_are_copy_blocks(self):
        """Commands are inline code spans (tap-to-copy on mobile, BotFather-style)."""
        msg = notify.build_message(base_info())  # both LAN + Tailscale present
        # No fenced blocks -- mobile clients don't reliably copy those on tap.
        self.assertNotIn("```", msg)
        # Each reconnect command is an inline code span (single backticks)...
        self.assertIn("`ssh ", msg)
        # ...one per reachable address.
        self.assertEqual(msg.count("-t tmux new-session -A -s"), 2)

    def test_custom_session_name_flows_through(self):
        msg = notify.build_message(base_info(tmux_session="work"))
        self.assertIn("-s work", msg)        # in the command
        self.assertIn("`work`", msg)         # in the tmux-status line

    def test_system_info_rendered_when_present(self):
        msg = notify.build_message(base_info(
            cpu_model="Intel(R) Core(TM) i3", cpu_count=4,
            mem_used_gib=2.1, mem_total_gib=5.8,
            disk_used_gib=45, disk_total_gib=440, disk_pct=10,
            loadavg=(0.12, 0.08, 0.05), kernel="7.0.0-38-generic"))
        self.assertIn("4 cores", msg)
        self.assertIn("RAM:", msg)
        self.assertIn("Disk /:", msg)
        self.assertIn("Load:", msg)
        self.assertIn("`7.0.0-38-generic`", msg)          # kernel literal in code
        self.assertIn(r"Intel\(R\) Core\(TM\) i3", msg)   # parens escaped in prose

    def test_system_info_omitted_when_absent(self):
        msg = notify.build_message(base_info())  # no cpu/mem/disk/load/kernel keys
        for token in ("CPU:", "RAM:", "Disk /:", "Load:", "Kernel:"):
            self.assertNotIn(token, msg)

    def test_gpu_rendered_with_name_and_vram(self):
        msg = notify.build_message(base_info(gpus=[
            {"name": "NVIDIA GeForce RTX 3090", "mem_total_mib": 24576,
             "mem_used_mib": 1024, "util_pct": 0, "temp_c": 40}]))
        self.assertIn("GPU:", msg)
        self.assertIn("NVIDIA GeForce RTX 3090", msg)
        self.assertIn("24 GiB", msg)

    def test_gpu_omitted_when_none(self):
        self.assertNotIn("GPU:", notify.build_message(base_info()))
        self.assertNotIn("GPU:", notify.build_message(base_info(gpus=[])))


class TestGetGpus(unittest.TestCase):
    def test_parses_nvidia_smi_csv(self):
        out = ("NVIDIA GeForce RTX 3090, 24576, 1536, 12, 45\n"
               "NVIDIA GeForce RTX 3090, 24576, 0, 0, 38\n")
        fake = mock.Mock(returncode=0, stdout=out)
        with mock.patch.object(notify.subprocess, "run", return_value=fake):
            gpus = notify._nvidia_gpus()
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce RTX 3090")
        self.assertEqual(gpus[0]["mem_total_mib"], 24576)
        self.assertEqual(gpus[0]["util_pct"], 12)
        self.assertEqual(gpus[0]["temp_c"], 45)

    def test_nvidia_absent_returns_empty(self):
        with mock.patch.object(notify.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(notify._nvidia_gpus(), [])

    def test_falls_back_to_lspci_name_only(self):
        lspci = ("00:02.0 VGA compatible controller: Intel UHD Graphics 770\n"
                 "01:00.0 Audio device: NVIDIA Corporation\n")
        fake = mock.Mock(returncode=0, stdout=lspci)
        with mock.patch.object(notify, "_nvidia_gpus", return_value=[]), \
             mock.patch.object(notify.subprocess, "run", return_value=fake):
            gpus = notify.get_gpus()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "Intel UHD Graphics 770")
        self.assertIsNone(gpus[0]["mem_total_mib"])

    def test_welcome_message_is_celebratory_and_names_host(self):
        msg = notify.build_welcome_message(base_info(hostname="Ghost-Screen"))
        self.assertIn("TurtlEngine is live", msg)
        self.assertIn("`Ghost-Screen`", msg)   # host in a literal code span
        self.assertIn("🎉", msg)


class TestBanner(unittest.TestCase):
    def test_banner_prints_tagline(self):
        """The notifier prints the TurtlEngine banner on every run."""
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            notify.print_banner()
        out = buf.getvalue()
        self.assertIn("T u r t l E n g i n e", out)
        self.assertIn("slow is smooth, smooth is fast", out)


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
