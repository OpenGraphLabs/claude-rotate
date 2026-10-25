"""Unit tests for the pure helpers plus end-to-end runs against tests/mock_claude.py.

Every test uses temporary directories through CLAUDE_ROTATE_HOME / CLAUDE_ROTATE_SHARED_DIR, so the
real ~/.claude and ~/.claude-accounts are never read or written.
"""
import importlib.util
import json
import os
import errno
import fcntl
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "claude-rotate"
MOCK = ROOT / "tests" / "mock_claude.py"
LIMIT_TEXT = "You've hit your weekly limit · resets Oct 6 at 2am (Asia/Seoul)"
FABLE_TEXT = "You've reached your Fable limit. Run /usage-credits to continue or switch models with /model."
BANNER = "\r\n ⚠ \x1b[33mUsage limit reached\x1b[0m · limit resets 3:50pm · " \
         "\x1b]8;;https://clau.de/wrap-up\x1b\\clau.de/wrap-up\x1b]8;;\x1b\\\r\n"


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
        at = datetime(2026, 10, 25, 5, 40, tzinfo=ZoneInfo("UTC")).timestamp()
        self.assertEqual(self.m.scan_models(t, 0), ([], {"opus": at}))

    def test_resume_parts_switches_model(self):
        parts = self.m.split_args(["--model=fable", "-p", "hello"])
        out = self.m.resume_parts(parts, "sid", "go on", "claude-opus-5-5")
        self.assertEqual(out, [("-p", ["-p"]), ("--model", ["--model", "claude-opus-5-5"]),
                              ("--resume", ["--resume", "sid"]), ("--", ["--", "go on"])])

    def test_switch_for_uses_fallback_where_it_is_free(self):
        cfg = {"order": ["default", "a"], "model_fallbacks": {"fable": "claude-opus-5-5"}}
        (self.m.ACCOUNTS / "a").mkdir(parents=True, exist_ok=True)
        fable, opus = {"fable": {"limited_until": 10**12}}, {"opus": {"limited_until": 10**12}}
        state = {"default": {"models": fable}}
        self.assertEqual(self.m.switch_for(cfg, state, "default", 0, "fable"),
                         ("default", "claude-opus-5-5"))
        state = {"default": {"models": {**fable, **opus}}}
        self.assertEqual(self.m.switch_for(cfg, state, "default", 0, "fable"), ("a", "claude-opus-5-5"))
        state["a"] = {"models": opus}
        self.assertIsNone(self.m.switch_for(cfg, state, "default", 0, "fable"))
        self.assertEqual(self.m.switch_for(cfg, {}, "default", 0, "sonnet"), ("a", None))

    def test_scan_models_reads_subagent_transcripts(self):
        t = Path(self.tmp.name) / "parent.jsonl"
        agents = t.with_suffix("") / "subagents"
        agents.mkdir(parents=True)
        t.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-25T05:00:00Z",
                                 "message": {"model": "claude-opus-5-5", "content": []}}) + "\n")
        (agents / "agent-a.jsonl").write_text(json.dumps({
            "type": "assistant", "isSidechain": True, "isApiErrorMessage": True, "error": "rate_limit",
            "timestamp": "2026-10-25T05:01:00Z",
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": FABLE_TEXT}]},
        }) + "\n")
        limits, replied = self.m.scan_models(t, 0)
        self.assertEqual([(model, text) for model, text, _ in limits], [("fable", FABLE_TEXT)])
        self.assertEqual(list(replied), ["opus"])

    def test_note_models_clears_limit_after_a_later_reply(self):
        t = Path(self.tmp.name) / "later.jsonl"
        t.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-25T06:00:00Z",
                                 "message": {"model": "claude-fable-5-1", "content": []}}) + "\n")
        reply = datetime(2026, 10, 25, 6, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        state = {"default": {"models": {"fable": {"limited_until": 10**12, "recorded_at": reply - 60}}}}
        self.m.note_models(state, "default", t, 0, {"model_cooldown_hours": 24})
        self.assertEqual(state["default"]["models"], {})

    def test_model_limits_steer_account_choice(self):
        cfg = {"order": ["default", "a", "b"]}
        for name in ("a", "b"):
            (self.m.ACCOUNTS / name).mkdir(parents=True, exist_ok=True)
        fable = {"models": {"fable": {"limited_until": 10**12}}}
        state = {"default": fable, "a": fable}
        self.assertEqual(self.m.pick_next(cfg, state, "default", 0, "fable"), "b")
        self.assertEqual(self.m.pick_next(cfg, state, "default", 0), "a")
        self.assertEqual(self.m.pick_first(cfg, state, 0), "b")

    def test_pick_next_skips_limited(self):
        cfg = {"order": ["default", "a", "b"]}
        (self.m.ACCOUNTS / "a").mkdir(parents=True, exist_ok=True)
        (self.m.ACCOUNTS / "b").mkdir(parents=True, exist_ok=True)
        state = {"a": {"limited_until": 10**12}}
        self.assertEqual(self.m.pick_next(cfg, state, "default", 0), "b")
        self.assertEqual(self.m.pick_next(cfg, state, "b", 0), "default")
        self.assertIsNone(self.m.pick_next(cfg, {"default": {"limited_until": 10**12}, **state}, "b", 0))

    def test_banner_across_utf8_and_ansi_boundaries(self):
        detector = self.m.TerminalLimitDetector()
        found = [message for byte in BANNER.encode()
                 if (message := detector.feed(bytes([byte])))]
        self.assertEqual(found, ["Usage limit reached · resets 3:50pm"])
        self.assertIsNone(detector.feed(BANNER.encode()))

    def test_banner_ignores_conversation_running_and_paid_usage(self):
        for text in [
            "Usage limit reached",
            "The error was: Usage limit reached · limit resets 3:50pm · clau.de/wrap-up",
            "⚠ Usage limit reached · wrapping up · limit resets 3:50pm · clau.de/wrap-up",
            "⚠ Usage limit reached · brief included wrap-up, then usage credits · clau.de/wrap-up",
            "⚠ Usage limit approaching · limit resets 3:50pm · clau.de/wrap-up",
            "> ⚠ Usage limit reached · limit resets 3:50pm · clau.de/wrap-up",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(self.m.TerminalLimitDetector().feed(text.encode()))

    def test_banner_cursor_redraw_and_long_output(self):
        detector = self.m.TerminalLimitDetector()
        detector.feed(b"ordinary output" * 1000)
        self.assertLessEqual(len(detector.text), 8192)
        text = "\x1b[2K\x1b[1G⚠ Usage limit reached · limit resets 3:50pm · clau.de/wrap-up"
        self.assertIn("resets 3:50pm", detector.feed(text.encode()))


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

    def rotate_tty(self, *, callback=None, **extra):
        master, slave = pty.openpty()
        size = struct.pack("HHHH", 42, 120, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        original = termios.tcgetattr(slave)
        proc = subprocess.Popen([sys.executable, str(SCRIPT)], env={**self.env, **extra},
                                cwd=self.cwd, stdin=slave, stdout=slave, stderr=slave)
        output = b""
        deadline = time.monotonic() + 15
        try:
            while proc.poll() is None:
                self.assertLess(time.monotonic(), deadline, output.decode(errors="replace"))
                if callback:
                    callback(proc, output, master, slave)
                if select.select([master], [], [], 0.05)[0]:
                    output += os.read(master, 65536)
            while select.select([master], [], [], 0)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                output += data
            restored = termios.tcgetattr(slave)
            # Darwin can set the kernel-owned "retype pending input" bit on tcsetattr.
            restored[3] &= ~getattr(termios, "PENDIN", 0)
            original[3] &= ~getattr(termios, "PENDIN", 0)
            self.assertEqual(restored, original, "terminal settings not restored")
            return proc.returncode, output.decode(errors="replace")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
            os.close(slave)

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

    def test_hook_evidence_survives_missing_transcript_error(self):
        r = self.rotate([], MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = self.invocations()
        self.assertEqual([i["account"] for i in inv], ["default", "dev"])
        self.assertNotEqual(inv[0]["run"], inv[1]["run"])
        events = [json.loads(line) for line in (self.home / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["source"] for e in events if e["event"] == "limit_detected"],
                         ["StopFailure"])

    def test_subagent_limit_resumes_parent_without_main_error(self):
        r = self.rotate([], MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1",
                        MOCK_SUBAGENT_LIMIT="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = self.invocations()
        self.assertEqual([i["account"] for i in inv], ["default", "dev"])
        self.assertNotIn("agent", inv[1]["argv"][inv[1]["argv"].index("--resume") + 1])

    def test_rendered_hook_message_preserves_model_scope(self):
        r = self.rotate([], MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1",
                        MOCK_LIMIT_TEXT="You've reached your Sonnet limit")
        self.assertEqual(r.returncode, 0, r.stderr)
        entry = self.state()["default"]
        self.assertNotIn("limited_until", entry)
        self.assertIn("sonnet", entry["models"])
        # Sonnet has no fallback model, so the session moves to an account where Sonnet is free.
        self.assertEqual([i["account"] for i in self.invocations()], ["default", "dev"])

    def test_fable_limit_resumes_same_account_on_opus(self):
        r = self.rotate(["--model", "fable"], MOCK_LIMITED="default", MOCK_LIMIT_TEXT=FABLE_TEXT,
                        MOCK_FREE_MODEL="claude-opus-5-5")
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = self.invocations()
        self.assertEqual([i["account"] for i in inv], ["default", "default"])
        second = inv[1]["argv"]
        self.assertEqual(second.count("--model"), 1)
        self.assertEqual(second[second.index("--model") + 1], "claude-opus-5-5")
        transcripts = Path(self.tmp.name) / "transcripts"
        sid = second[second.index("--resume") + 1]
        self.assertEqual(list(transcripts.glob("*.jsonl")), [transcripts / f"{sid}.jsonl"])
        prompt = second[second.index("--") + 1]
        self.assertIn("Fable usage limit", prompt)
        self.assertIn("resumed on claude-opus-5-5", prompt)
        self.assertIn("with claude-opus-5-5", r.stderr)
        self.assertIn("fable", self.state()["default"]["models"])

    def test_subagent_fable_limit_leaves_parent_running(self):
        r = self.rotate([], MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1",
                        MOCK_SUBAGENT_LIMIT="1", MOCK_LIMIT_TEXT=FABLE_TEXT, MOCK_WAIT="0.5")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual([i["account"] for i in self.invocations()], ["default"])
        self.assertIn("fable", self.state()["default"]["models"])
        events = [json.loads(line) for line in (self.home / "events.jsonl").read_text().splitlines()]
        self.assertIn("subagent_model_limit",
                      [e.get("reason") for e in events if e["event"] == "rotation_skipped"])

    def test_hook_without_other_account_still_records_limit(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["order"] = ["default"]
        (self.home / "config.json").write_text(json.dumps(cfg))
        r = self.rotate([], MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1", MOCK_WAIT="0.2")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("limited_until", self.state()["default"])
        self.assertIn("no other account is free", r.stderr)

    def test_terminal_only_banner_rotates_with_same_parent_session(self):
        code, output = self.rotate_tty(MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1",
                                      MOCK_NO_STOP_HOOK="1", MOCK_BANNER=BANNER,
                                      MOCK_FRAGMENT_BANNER="1")
        self.assertEqual(code, 0, output)
        inv = self.invocations()
        self.assertEqual([i["account"] for i in inv], ["default", "dev"])
        self.assertTrue(all(i["tty"] for i in inv))
        self.assertEqual(inv[0]["size"], list(struct.pack("HHHH", 42, 120, 0, 0)))
        self.assertIn("mock finished", output)
        events = [json.loads(line) for line in (self.home / "events.jsonl").read_text().splitlines()]
        detected = next(e for e in events if e["event"] == "limit_detected")
        self.assertEqual(detected["source"], "terminal_banner")
        second = inv[1]["argv"]
        self.assertEqual(second[second.index("--resume") + 1], detected["session_id"])
        self.assertFalse(list((self.home / "runs").iterdir()))

    def test_terminal_normal_text_does_not_rotate(self):
        code, output = self.rotate_tty(MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1",
                                      MOCK_NO_STOP_HOOK="1",
                                      MOCK_BANNER="Usage limit reached\n", MOCK_WAIT="0.2")
        self.assertEqual(code, 3, output)
        self.assertEqual([i["account"] for i in self.invocations()], ["default"])
        self.assertNotIn("default", self.state())

    def test_terminal_banner_quoted_in_conversation_does_not_rotate(self):
        quoted = "\r\n  ⚠ Usage limit reached · limit resets 3:50pm · clau.de/wrap-up\r\n"
        code, output = self.rotate_tty(MOCK_LIMITED="default", MOCK_NO_TRANSCRIPT_ERROR="1",
                                      MOCK_NO_STOP_HOOK="1", MOCK_BANNER=quoted,
                                      MOCK_QUOTE_BANNER="1", MOCK_WAIT="3")
        self.assertEqual(code, 3, output)
        self.assertEqual([i["account"] for i in self.invocations()], ["default"])
        self.assertNotIn("default", self.state())
        events = [json.loads(line) for line in (self.home / "events.jsonl").read_text().splitlines()]
        self.assertIn("banner_ignored", [e["event"] for e in events])

    def test_terminal_external_termination_does_not_resume(self):
        sent = False

        def terminate(proc, output, master, slave):
            nonlocal sent
            if not sent and b"clau.de/wrap-up" in output:
                sent = True
                proc.send_signal(signal.SIGTERM)

        code, output = self.rotate_tty(callback=terminate, MOCK_LIMITED="default",
                                      MOCK_NO_TRANSCRIPT_ERROR="1", MOCK_NO_STOP_HOOK="1",
                                      MOCK_BANNER=BANNER)
        self.assertTrue(sent, output)
        self.assertEqual([i["account"] for i in self.invocations()], ["default"])

    def test_terminal_resize_and_keyboard_interrupt(self):
        resized = interrupted = False

        def control(proc, output, master, slave):
            nonlocal resized, interrupted
            if not resized and b"mock ready" in output:
                resized = True
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 99, 0, 0))
                proc.send_signal(signal.SIGWINCH)
            if not interrupted and b"RESIZED 33x99" in output:
                interrupted = True
                os.write(master, b"\x03")

        code, output = self.rotate_tty(callback=control, MOCK_LIMITED="default",
                                      MOCK_NO_TRANSCRIPT_ERROR="1", MOCK_NO_STOP_HOOK="1",
                                      MOCK_REPORT_RESIZE="1")
        self.assertTrue(interrupted, output)
        self.assertEqual(code, 130, output)
        self.assertEqual([i["account"] for i in self.invocations()], ["default"])

    def test_terminal_suspend_and_continue(self):
        resumed = False

        def control(proc, output, master, slave):
            nonlocal resumed
            if not resumed:
                pid, status = os.waitpid(proc.pid, os.WNOHANG | os.WUNTRACED)
                if pid:
                    self.assertTrue(os.WIFSTOPPED(status))
                    # The outer terminal is usable while the wrapper is suspended.
                    self.assertTrue(termios.tcgetattr(slave)[3] & termios.ICANON)
                    resumed = True
                    proc.send_signal(signal.SIGCONT)

        code, output = self.rotate_tty(callback=control, MOCK_LIMITED="default",
                                      MOCK_NO_TRANSCRIPT_ERROR="1", MOCK_NO_STOP_HOOK="1",
                                      MOCK_SUSPEND="1", MOCK_WAIT="0.2")
        self.assertTrue(resumed, output)
        self.assertEqual(code, 3, output)
        self.assertIn("mock resumed", output)

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
