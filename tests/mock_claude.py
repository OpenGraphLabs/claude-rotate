#!/usr/bin/env python3
"""A stand-in for `claude` used by the tests.

Reads the --settings hooks the wrapper passes, runs SessionStart, writes a transcript, and when the
account it runs on is listed in MOCK_LIMITED ends the turn with the usage-limit error (running the
StopFailure hook, then waiting to be stopped unless --print). Every invocation is appended to
MOCK_LOG as one JSON line.
"""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path


def hooks(settings, event):
    for group in (settings.get("hooks") or {}).get(event, []):
        for h in group.get("hooks", []):
            yield h["command"]


def run_hooks(settings, event, payload):
    for cmd in hooks(settings, event):
        subprocess.run(cmd, shell=True, input=json.dumps(payload), text=True, check=False)


def main(argv):
    settings, resume, printing = {}, None, False
    i = 0
    while i < len(argv):
        if argv[i] == "--settings":
            settings = json.loads(argv[i + 1]); i += 2; continue
        if argv[i] == "--resume":
            resume = argv[i + 1]; i += 2; continue
        if argv[i] in ("-p", "--print"):
            printing = True
        if argv[i] == "--":
            break
        i += 1
    account = os.environ["CLAUDE_ROTATE_ACCOUNT"]
    with open(os.environ["MOCK_LOG"], "a") as f:
        f.write(json.dumps({"argv": argv, "account": account, "cwd": os.getcwd()}) + "\n")

    session_id = resume or str(uuid.uuid4())
    transcript = Path(os.environ["MOCK_TRANSCRIPTS"]) / f"{session_id}.jsonl"
    run_hooks(settings, "SessionStart", {
        "hook_event_name": "SessionStart", "session_id": session_id,
        "transcript_path": str(transcript), "cwd": os.getcwd(),
    })
    limited = account in os.environ.get("MOCK_LIMITED", "").split(",")
    with open(transcript, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        if limited:
            f.write(json.dumps({
                "type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": os.environ["MOCK_LIMIT_TEXT"]}]},
            }) + "\n")
        else:
            f.write(json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "done"}]}}) + "\n")
    if limited:
        if not os.environ.get("MOCK_NO_STOP_HOOK"):
            run_hooks(settings, "StopFailure", {
                "hook_event_name": "StopFailure", "error": "rate_limit", "session_id": session_id,
                "transcript_path": str(transcript), "last_assistant_message": os.environ["MOCK_LIMIT_TEXT"],
            })
        if not printing:
            # An interactive session keeps running until the wrapper stops it.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            deadline = time.time() + float(os.environ.get("MOCK_WAIT", "20"))
            while time.time() < deadline:
                time.sleep(0.1)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
