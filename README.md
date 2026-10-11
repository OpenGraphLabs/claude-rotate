# claude-rotate

Run [Claude Code](https://docs.claude.com/en/docs/claude-code) on several Claude accounts and move
the running session to the next account when the current one hits its usage limit.

Claude Code signs in to one account at a time and stops with
`You've hit your weekly limit · resets Oct 6 at 2am (Asia/Seoul)` when that account's quota is
used up. There is no built-in way to switch accounts from the command line, let alone to carry
the conversation over. `claude-rotate` is a single-file Python wrapper that:

- keeps each extra account in its own `CLAUDE_CONFIG_DIR` while sharing settings, `CLAUDE.md`,
  skills, plugins, memory and session transcripts with your main login;
- starts `claude` on the first account that is not limited;
- notices the usage-limit error (through a `StopFailure` hook and the session transcript),
  records when that account resets, and relaunches `claude --resume <session>` on the next free
  account with a short "continue where you left off" prompt;
- does the same for `claude -p` (print mode) runs after they exit.

Works on macOS and Linux with Python 3.9+ and Claude Code 2.1.x. Only use accounts you are
entitled to use under Anthropic's terms.

## Install

```sh
curl -fsSLo ~/.local/bin/claude-rotate \
  https://raw.githubusercontent.com/sungman-cho/claude-rotate/main/claude-rotate
chmod +x ~/.local/bin/claude-rotate
```

`~/.local/bin` must be on your `PATH` and `claude` must already be installed and signed in to
your main account (that login is the account named `default`).

Most people want every `claude` invocation to go through the wrapper:

```sh
# ~/.zshrc or ~/.bashrc
alias claude=claude-rotate
```

The wrapper finds the real binary as the first `claude` on `PATH` that is not itself, so the
alias does not loop. Set `CLAUDE_ROTATE_CLAUDE_BIN` to point at a specific binary instead.

## Add accounts

```sh
claude-rotate account add work2          # creates the account dir, then runs `claude auth login`
claude-rotate account list
```

```
default          you@example.com                      available
work2            you+2@example.com                    limited until 10-06 02:00
```

`account add` opens the normal Claude Code login flow for the new account. If your browser is
already signed in to another claude.ai account, open the login link in a private window.
Accounts rotate in the order they were added (`order` in `~/.claude-accounts/config.json`).

Other commands:

```
claude-rotate account login NAME [--email E]   sign in (again) on an existing account
claude-rotate account reset [NAME]             forget recorded limits
claude-rotate account help
```

## Use

```sh
claude-rotate                       # interactive session, like `claude`
claude-rotate -p "summarise this"   # print mode
claude-rotate --resume <id>         # any claude flag passes through
```

What you see when a limit hits:

```
[claude-rotate] account default (you@example.com)
...
[claude-rotate] default hit its usage limit (free again around 10-06 02:00)
[claude-rotate] resuming session 1f3c...-... on work2
[claude-rotate] account work2 (you+2@example.com)
```

The resumed session starts with the prompt from `continue_prompt` in `config.json`, so Claude
picks up the turn that was cut off. If you quit an interactive session yourself after the limit
message, the wrapper does not relaunch; it just remembers the limit for next time.

## How it works

**Accounts.** `default` is your normal login with no `CLAUDE_CONFIG_DIR`. Every other account is
a directory `~/.claude-accounts/accounts/<name>` used as `CLAUDE_CONFIG_DIR`. Only the
account-private entries live there (`.claude.json` with the sign-in reference, backups, caches,
telemetry); everything else is a symlink into `~/.claude`, so sessions, memory, settings and
skills are one set. Claude Code stores the OAuth token in the OS keychain under a name derived
from the config dir, so each directory gets its own login. On every launch the wrapper copies
MCP servers and per-project trust from the main login into the account's `.claude.json`, so a
resumed session does not stop at the trust dialog.

**Detection.** Two hooks are passed with `--settings` (your own `--settings` is merged, your
`settings.json` is untouched): `SessionStart` records the session id and transcript path, and
`StopFailure` with matcher `rate_limit` asks the wrapper to stop `claude` once another account is
free. After `claude` exits, the wrapper reads the transcript tail; if the last message is the
usage-limit error (`isApiErrorMessage` with `error: "rate_limit"`), it parses the reset time from
the text (`resets 3pm (Asia/Seoul)`, `resets Oct 6 at 2am (Asia/Seoul)`, or an ISO time) and
falls back to `cooldown_minutes` (default 60) when it cannot.

**Relaunch.** The original arguments are kept except anything that chooses a conversation
(`-c`, `--resume`, `--session-id`, `--worktree`, positional prompt, ...), then
`--resume <session> -- <continue_prompt>` is appended.

Files:

```
~/.claude-accounts/
  config.json     order, cooldown_minutes, continue_prompt
  state.json      per-account limited_until + the limit message
  accounts/<n>/   CLAUDE_CONFIG_DIR of each extra account
  runs/           per-run session info while a wrapper is running
```

Environment variables: `CLAUDE_ROTATE_HOME` (default `~/.claude-accounts`),
`CLAUDE_ROTATE_SHARED_DIR` (default `~/.claude`), `CLAUDE_ROTATE_CLAUDE_BIN`.

## Limits

- `claude --bare` skips `--settings` hooks, so an interactive session started that way is not
  moved while it runs; print-mode runs are still moved after they exit.
- Rotation is per wrapper process. Several sessions running at once each move on their own; the
  recorded limit in `state.json` is shared so later launches start on a free account.
- Ctrl-C, Ctrl-\ and Ctrl-Z go to `claude`; the wrapper survives them.
- Not tested on Windows (uses `fcntl` and POSIX signals).

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests run the wrapper against `tests/mock_claude.py` in temporary directories and never touch
`~/.claude` or `~/.claude-accounts`.

## License

MIT
