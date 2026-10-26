#!/usr/bin/env python3
"""A stand-in for `claude` used by the tests.

Reads the --settings hooks the wrapper passes, runs SessionStart, writes a transcript, and when the
account it runs on is listed in MOCK_LIMITED ends the turn with the usage-limit error (running the
StopFailure hook, then waiting to be stopped unless --print); a run with `--model MOCK_FREE_MODEL`
is never limited. Every invocation is appended to MOCK_LOG as one JSON line.
"""
import json
import os
import fcntl
import signal
import struct
import subprocess
import sys
import time
import termios
import uuid
from datetime import datetime, timezone
from pathlib import Path


def hooks(settings, event):
    for group in (settings.get("hooks") or {}).get(event, []):
        for h in group.get("hooks", []):
            yield h["command"]


def run_hooks(settings, event, payload):
    for cmd in hooks(settings, event):
        subprocess.run(cmd, shell=True, input=json.dumps(payload), text=True, check=False)


def main(argv):
    settings, resume, printing, model = {}, None, False, None
    i = 0
    while i < len(argv):
        if argv[i] == "--settings":
            settings = json.loads(argv[i + 1]); i += 2; continue
        if argv[i] == "--resume":
            resume = argv[i + 1]; i += 2; continue
        if argv[i] == "--model":
            model = argv[i + 1]; i += 2; continue
        if argv[i] in ("-p", "--print"):
            printing = True
        if argv[i] == "--":
            break
        i += 1
    account = os.environ.get("CLAUDE_ROTATE_ACCOUNT", "no-wrapper")
    with open(os.environ["MOCK_LOG"], "a") as f:
        f.write(json.dumps({"argv": argv, "account": account, "cwd": os.getcwd(),
                            "run": os.environ.get("CLAUDE_ROTATE_RUN"),
                            "tty": sys.stdin.isatty(),
                            "size": list(fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
                            if sys.stdin.isatty() else None}) + "\n")

    session_id = resume or str(uuid.uuid4())
    transcript = Path(os.environ["MOCK_TRANSCRIPTS"]) / f"{session_id}.jsonl"
    run_hooks(settings, "SessionStart", {
        "hook_event_name": "SessionStart", "session_id": session_id,
        "transcript_path": str(transcript), "cwd": os.getcwd(),
    })
    limited = (account in os.environ.get("MOCK_LIMITED", "").split(",")
               and not (model and model == os.environ.get("MOCK_FREE_MODEL")))
    with open(transcript, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        if os.environ.get("MOCK_QUOTE_BANNER"):
            # A reply that quotes the banner: the terminal shows it, the transcript records it.
            f.write(json.dumps({
                "type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": "assistant", "model": "claude-opus-5-5",
                            "content": [{"type": "text", "text": os.environ["MOCK_BANNER"]}]},
            }, ensure_ascii=False) + "\n")
        if os.environ.get("MOCK_REPLY_MODEL"):
            f.write(json.dumps({
                "type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": "assistant", "model": os.environ["MOCK_REPLY_MODEL"],
                            "content": [{"type": "text", "text": "earlier reply"}]},
            }) + "\n")
        if limited and not os.environ.get("MOCK_NO_TRANSCRIPT_ERROR"):
            f.write(json.dumps({
                "type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": os.environ["MOCK_LIMIT_TEXT"]}]},
            }) + "\n")
        else:
            f.write(json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "done"}]}}) + "\n")
    if limited:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
        if os.environ.get("MOCK_REPORT_RESIZE"):
            def resized(*_):
                rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
                print(f"RESIZED {rows}x{cols}", flush=True)
            signal.signal(signal.SIGWINCH, resized)
            print("mock ready", flush=True)
        if os.environ.get("MOCK_SUBAGENT_LIMIT"):
            agent = transcript.with_name("agent.jsonl")
            agent.write_text(json.dumps({
                "type": "assistant", "isSidechain": True, "isApiErrorMessage": True,
                "error": "rate_limit", "message": {"content": [
                    {"type": "text", "text": os.environ["MOCK_LIMIT_TEXT"]}]},
            }) + "\n")
            run_hooks(settings, "SubagentStop", {
                "hook_event_name": "SubagentStop", "session_id": session_id,
                "agent_id": "agent", "agent_transcript_path": str(agent),
            })
        elif not os.environ.get("MOCK_NO_STOP_HOOK"):
            run_hooks(settings, "StopFailure", {
                "hook_event_name": "StopFailure", "error": "rate_limit", "session_id": session_id,
                "transcript_path": str(transcript), "last_assistant_message": os.environ["MOCK_LIMIT_TEXT"],
                "error_details": "429 Too Many Requests",
            })
        if os.environ.get("MOCK_BANNER"):
            data = os.environ["MOCK_BANNER"].encode()
            if os.environ.get("MOCK_FRAGMENT_BANNER"):
                for byte in data:
                    os.write(1, bytes([byte]))
                    time.sleep(0.001)
            else:
                os.write(1, data)
        if os.environ.get("MOCK_SUSPEND"):
            os.kill(os.getpid(), signal.SIGSTOP)
            print("mock resumed", flush=True)
        if not printing:
            # An interactive session keeps running until the wrapper stops it.
            deadline = time.time() + float(os.environ.get("MOCK_WAIT", "20"))
            while time.time() < deadline:
                time.sleep(0.1)
            return 3
    print("mock finished", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
