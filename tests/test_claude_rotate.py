"""Unit tests for the pure helpers plus end-to-end runs against tests/mock_claude.py.

Every test uses temporary directories through CLAUDE_ROTATE_HOME / CLAUDE_ROTATE_SHARED_DIR, so the
real ~/.claude and ~/.claude-accounts are never read or written.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "claude-rotate"
MOCK = ROOT / "tests" / "mock_claude.py"
LIMIT_TEXT = "You've hit your weekly limit · resets Oct 6 at 2am (Asia/Seoul)"


def load_module():
    spec = importlib.util.spec_from_loader(
        "claude_rotate", importlib.machinery.SourceFileLoader("claude_rotate", str(SCRIPT)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Helpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_ROTATE_HOME"] = f"{cls.tmp.name}/home"
        os.environ["CLAUDE_ROTATE_SHARED_DIR"] = f"{cls.tmp.name}/shared"
        cls.m = load_module()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_parse_reset_real_weekly_message(self):
        seoul = ZoneInfo("Asia/Seoul")
        now = datetime(2026, 10, 4, 16, 41, tzinfo=seoul).timestamp()
        ts = self.m.parse_reset(LIMIT_TEXT, now)
        self.assertEqual(datetime.fromtimestamp(ts, seoul), datetime(2026, 10, 6, 2, 0, tzinfo=seoul))

    def test_parse_reset_clock_only_rolls_to_tomorrow(self):
        ny = ZoneInfo("America/New_York")
        now = datetime(2026, 3, 1, 16, 0, tzinfo=ny).timestamp()
        ts = self.m.parse_reset("You've hit your limit · resets 3pm (America/New_York)", now)
        self.assertEqual(datetime.fromtimestamp(ts, ny), datetime(2026, 3, 2, 15, 0, tzinfo=ny))

    def test_parse_reset_iso_utc(self):
        ts = self.m.parse_reset("resets 2026-10-06 17:00 UTC", 0)
        self.assertEqual(datetime.fromtimestamp(ts, ZoneInfo("UTC")),
                         datetime(2026, 10, 6, 17, 0, tzinfo=ZoneInfo("UTC")))

    def test_parse_reset_unknown(self):
        self.assertIsNone(self.m.parse_reset("You've hit your limit", 0))
        self.assertIsNone(self.m.parse_reset("resets 5", 0))  # no am/pm and no minutes: ambiguous

    def test_split_args(self):
        parts = self.m.split_args(["-p", "--model", "opus", "--add-dir", "a", "b", "--model=x",
                                   "--resume", "--", "tail", "x"])
        self.assertEqual(parts, [
            ("-p", ["-p"]), ("--model", ["--model", "opus"]), ("--add-dir", ["--add-dir", "a", "b"]),
            ("--model", ["--model=x"]), ("--resume", ["--resume"]), ("--", ["--", "tail", "x"]),
        ])

    def test_resume_parts_drops_session_and_positionals(self):
        parts = self.m.split_args(["-c", "--model", "opus", "-p", "hello", "--", "old"])
        out = self.m.resume_parts(parts, "sid", "go on")
        self.assertEqual(out, [("--model", ["--model", "opus"]), ("-p", ["-p"]),
                              ("--resume", ["--resume", "sid"]), ("--", ["--", "go on"])])

    def test_hook_settings_merges_user_settings(self):
        parts = self.m.split_args(["--settings", '{"theme": "dark", "hooks": {"Stop": [{"hooks": []}]}}'])
        s = json.loads(self.m.hook_settings(parts))
        self.assertEqual(s["theme"], "dark")
        self.assertIn("Stop", s["hooks"])
        self.assertEqual(s["hooks"]["StopFailure"][0]["matcher"], "rate_limit")
        self.assertTrue(s["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("__hook"))
        cmd = self.m.command_line(parts, "{}")
        self.assertEqual(cmd, ["--settings", "{}"])  # the user's own --settings token is replaced

    def test_limit_message(self):
        t = Path(self.tmp.name) / "t.jsonl"
        err = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
               "message": {"content": [{"type": "text", "text": LIMIT_TEXT}]}}
        ok = {"type": "assistant", "message": {"content": [{"type": "text", "text": "fine"}]}}
        side = {**err, "isSidechain": True}
        t.write_text("\n".join(json.dumps(e) for e in [ok, err, {"type": "summary"}]) + "\n")
        self.assertEqual(self.m.limit_message(t), LIMIT_TEXT)
        t.write_text("\n".join(json.dumps(e) for e in [err, ok, side]) + "\n")
        self.assertIsNone(self.m.limit_message(t))
        self.assertIsNone(self.m.limit_message(t.with_name("missing.jsonl")))

    def test_transcript_lines_keep_unicode_line_separators(self):
        # JSON leaves U+2028 unescaped and tool output contains it. str.splitlines() cut lines there
        # and a fragment such as "1" parsed as an int: 'int' object has no attribute 'get'.
        t = Path(self.tmp.name) / "sep.jsonl"
        reply = {"type": "assistant", "timestamp": "2026-10-25T05:40:00Z",
                 "message": {"model": "claude-opus-5-5",
                             "content": [{"type": "text", "text": "a\u20280\u2028b"}]}}
        err = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
               "message": {"content": [{"type": "text", "text": "x\u20281\u2028" + LIMIT_TEXT}]}}
        t.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [reply, err]) + "\n")
        self.assertEqual(self.m.limit_message(t), "x\u20281\u2028" + LIMIT_TEXT)

    def test_pick_next_skips_limited(self):
        cfg = {"order": ["default", "a", "b"]}
        (self.m.ACCOUNTS / "a").mkdir(parents=True, exist_ok=True)
        (self.m.ACCOUNTS / "b").mkdir(parents=True, exist_ok=True)
        state = {"a": {"limited_until": 10**12}}
        self.assertEqual(self.m.pick_next(cfg, state, "default", 0), "b")
        self.assertEqual(self.m.pick_next(cfg, state, "b", 0), "default")
        self.assertIsNone(self.m.pick_next(cfg, {"default": {"limited_until": 10**12}, **state}, "b", 0))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.shared, self.cwd = root / "home", root / "shared", root / "work"
        for d in (self.shared, self.cwd, root / "transcripts"):
            d.mkdir(parents=True)
        (self.shared / "settings.json").write_text("{}")
        (self.shared / "CLAUDE.md").write_text("shared instructions\n")
        (self.shared / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "main@example.com"}, "theme": "dark",
            "hasCompletedOnboarding": True,
            "projects": {str(self.cwd): {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]}},
            "mcpServers": {"x": {"command": "x"}},
        }))
        self.log = root / "log.jsonl"
        # A real message names the day without a year; use tomorrow so the parser's year roll-over
        # (a past date means next year) does not apply.
        seoul = ZoneInfo("Asia/Seoul")
        self.reset = (datetime.now(seoul) + timedelta(days=1)).replace(hour=2, minute=0, second=0,
                                                                        microsecond=0)
        limit_text = f"You've hit your weekly limit · resets {self.reset:%b} {self.reset.day} at 2am (Asia/Seoul)"
        self.env = {
            **os.environ,
            "CLAUDE_ROTATE_HOME": str(self.home), "CLAUDE_ROTATE_SHARED_DIR": str(self.shared),
            "CLAUDE_ROTATE_CLAUDE_BIN": str(MOCK), "MOCK_LOG": str(self.log),
            "MOCK_TRANSCRIPTS": str(root / "transcripts"), "MOCK_LIMIT_TEXT": limit_text,
        }
        for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_ROTATE_ACCOUNT", "CLAUDE_ROTATE_RUN"):
            self.env.pop(k, None)  # the tests may themselves run inside a wrapper session
        self.rotate(["account", "add", "dev", "--no-login"])

    def tearDown(self):
        self.tmp.cleanup()

    def rotate(self, args, **extra):
        env = {**self.env, **extra}
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, cwd=self.cwd,
                              capture_output=True, text=True, timeout=60)

    def invocations(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def state(self):
        return json.loads((self.home / "state.json").read_text())

    def test_account_add_links_shared_and_seeds_config(self):
        d = self.home / "accounts" / "dev"
        self.assertTrue((d / "settings.json").is_symlink())
        self.assertTrue((d / "projects").is_symlink())
        self.assertEqual((d / "CLAUDE.md").read_text(), "shared instructions\n")
        cfg = json.loads((d / ".claude.json").read_text())
        self.assertEqual(cfg["theme"], "dark")
        self.assertNotIn("oauthAccount", cfg)
        self.assertEqual(cfg["mcpServers"], {"x": {"command": "x"}})
        self.assertTrue(cfg["projects"][str(self.cwd)]["hasTrustDialogAccepted"])
        self.assertEqual(json.loads((self.home / "config.json").read_text())["order"], ["default", "dev"])
        out = self.rotate(["account", "list"]).stdout
        self.assertIn("main@example.com", out)
        self.assertIn("not signed in", out)

    def test_print_run_moves_to_next_account(self):
        r = self.rotate(["-p", "hello", "--model", "opus"], MOCK_LIMITED="default")
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = self.invocations()
        self.assertEqual([i["account"] for i in inv], ["default", "dev"])
        self.assertIn("hello", inv[0]["argv"])
        second = inv[1]["argv"]
        self.assertNotIn("hello", second)
        self.assertIn("-p", second)
        self.assertEqual(second[second.index("--model") + 1], "opus")
        sid = second[second.index("--resume") + 1]
        self.assertTrue((Path(self.tmp.name) / "transcripts" / f"{sid}.jsonl").exists())
        self.assertIn("Continue where you left off", second[second.index("--") + 1])
        self.assertEqual(datetime.fromtimestamp(self.state()["default"]["limited_until"], ZoneInfo("Asia/Seoul")),
                         self.reset)
        self.assertIn("hit its usage limit", r.stderr)
        self.assertFalse(list((self.home / "runs").iterdir()))  # run files are cleaned up

    def test_interactive_run_is_stopped_by_hook_and_resumed(self):
        r = self.rotate([], MOCK_LIMITED="default")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([i["account"] for i in self.invocations()], ["default", "dev"])
        self.assertIn("resuming session", r.stderr)

    def test_interactive_quit_without_hook_stays_quit(self):
        r = self.rotate([], MOCK_LIMITED="default", MOCK_NO_STOP_HOOK="1", MOCK_WAIT="2")
        # the mock waits for SIGTERM that never comes and exits 3 after its own timeout
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual([i["account"] for i in self.invocations()], ["default"])
        self.assertIn("default", self.state())  # the limit is still recorded for next time

    def test_subcommands_run_on_main_login_without_hooks(self):
        r = self.rotate(["auth", "status"], CLAUDE_CONFIG_DIR="/should/be/dropped")
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = self.invocations()
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0]["account"], "no-wrapper")
        self.assertEqual(inv[0]["argv"], ["auth", "status"])
        self.assertEqual(r.stderr, "")

    def test_starts_on_free_account_and_stops_when_all_limited(self):
        r = self.rotate(["-p", "x"], MOCK_LIMITED="default,dev")
        self.assertEqual([i["account"] for i in self.invocations()], ["default", "dev"])
        self.assertIn("no other account is free", r.stderr)
        self.log.unlink()
        r = self.rotate(["-p", "x"])
        self.assertIn("every account is limited", r.stderr)
        self.rotate(["account", "reset", "default"])
        self.log.unlink()
        self.rotate(["-p", "x"])
        self.assertEqual([i["account"] for i in self.invocations()], ["default"])


if __name__ == "__main__":
    unittest.main()
