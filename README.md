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
- notices usage limits through main/subagent error hooks and the session transcript,
  records when that account resets, and relaunches `claude --resume <session>` on the next free
  account with a short "continue where you left off" prompt;
- does the same for `claude -p` (print mode) runs after they exit.

Works on macOS and Linux with Python 3.9+ and Claude Code 2.1.x. Only use accounts you are
entitled to use under Anthropic's terms.

## Quick start

Five steps, about two minutes. Assumes `claude` is installed and signed in to your main account.

1. **Install the wrapper.**

   ```sh
   mkdir -p ~/.local/bin
   curl -fsSLo ~/.local/bin/claude-rotate \
     https://raw.githubusercontent.com/OpenGraphLabs/claude-rotate/main/claude-rotate
   chmod +x ~/.local/bin/claude-rotate
   claude-rotate account list       # should print your main login as "default"
   ```

   If the last command says `command not found`, add `export PATH="$HOME/.local/bin:$PATH"` to
   your shell rc file and open a new terminal.

2. **Add a second account.** Pick any short name; it only labels the account locally.

   ```sh
   claude-rotate account add personal
   ```

   This creates `~/.claude-accounts/accounts/personal` and runs `claude auth login` for it. Your
   browser opens the claude.ai login page. Sign in as the *second* account. If the browser is
   already signed in to the first one, copy the login link into a private window instead.

3. **Check both accounts are listed.**

   ```sh
   claude-rotate account list
   ```

   ```
   default          you@company.com                      available
   personal         you@gmail.com                        available
   ```

4. **Make `claude` use the wrapper.** Add to `~/.zshrc` or `~/.bashrc` and open a new terminal:

   ```sh
   alias claude=claude-rotate
   ```

5. **Work as usual.** Run `claude` in any project. The first line of output names the account in
   use. When that account hits its limit, the session is stopped and resumed on the next one:

   ```
   [claude-rotate] account default (you@company.com)
   ...
   [claude-rotate] default hit its usage limit (free again around 10-06 02:00)
   [claude-rotate] resuming session 1f3c6d2a-... on personal
   [claude-rotate] account personal (you@gmail.com)
   ```

   Claude continues the turn it was in the middle of. Nothing else changes: same project, same
   settings, same memory, same transcript.

### Day to day

- **Which account am I on?** The `[claude-rotate] account ...` line at the top of every session,
  or `claude-rotate account list` from another terminal.
- **An account is marked limited but I know it has reset.** `claude-rotate account reset NAME`
  clears the record. The wrapper only trusts the reset time printed in the limit message; when
  it cannot parse one it waits `cooldown_minutes` (default 60) before trying that account again.
- **Add a third account.** `claude-rotate account add NAME`. Accounts rotate in the order added;
  edit `order` in `~/.claude-accounts/config.json` to change it.
- **Sign in again** (token expired, switched email): `claude-rotate account login NAME`.
- **Run plain `claude` without rotation.** `command claude ...` bypasses the alias.
- **Resume a session that was moved.** `claude --resume <id>` works on every account because
  transcripts are shared. The wrapper starts it on whichever account is free.
- **`claude auth`, `claude mcp`, `claude doctor`, ...** Subcommands are not sessions; the wrapper
  runs them on your main login exactly as typed. Add MCP servers there and every account gets
  them. To run one against another account: `CLAUDE_CONFIG_DIR=~/.claude-accounts/accounts/NAME
  command claude auth status`.
- **Remove an account.** Sign it out first if you want the keychain entry gone
  (`CLAUDE_CONFIG_DIR=~/.claude-accounts/accounts/NAME command claude auth logout`), then delete
  `~/.claude-accounts/accounts/NAME` and remove it from `order` in `config.json`.

## Command reference

```
claude-rotate [claude args...]                  run claude on the first account that is not limited
claude-rotate account add NAME [--email E] [--no-login]
claude-rotate account login NAME [--email E]    sign in (again) on an existing account
claude-rotate account list
claude-rotate account reset [NAME]              forget recorded limits
claude-rotate account help
```

Every `claude` flag passes through (`-p`, `--model`, `--resume`, `--dangerously-skip-permissions`,
...). The wrapper finds the real binary as the first `claude` on `PATH` that is not itself, so the
alias does not loop; set `CLAUDE_ROTATE_CLAUDE_BIN` to use a specific binary. If you quit an
interactive session yourself after the limit message, the wrapper does not relaunch; it just
remembers the limit for next time.

## How it works

**Accounts.** `default` is your normal login with no `CLAUDE_CONFIG_DIR`. Every other account is
a directory `~/.claude-accounts/accounts/<name>` used as `CLAUDE_CONFIG_DIR`. Only the
account-private entries live there (`.claude.json` with the sign-in reference, backups, caches,
telemetry); everything else is a symlink into `~/.claude`, so sessions, memory, settings and
skills are one set. Claude Code stores the OAuth token in the OS keychain under a name derived
from the config dir, so each directory gets its own login. On every launch the wrapper copies
MCP servers and per-project trust from the main login into the account's `.claude.json`, so a
resumed session does not stop at the trust dialog.

**Detection.** Three hooks are passed with `--settings` (your own `--settings` is merged, your
`settings.json` is untouched). `SessionStart` records the parent session id and transcript path.
`StopFailure` with matcher `rate_limit`, or a `SubagentStop` whose transcript ends in a rate-limit
error, saves the error before asking the wrapper to stop Claude when another account is free.
Saved hook evidence is sufficient to rotate even when the main transcript has no error.
After exit, the last transcript message remains a secondary source of evidence for print runs
whose hooks did not finish. The reset time is parsed from the message; when absent,
`cooldown_minutes` (default 60) applies.

**Relaunch.** The original arguments are kept except anything that chooses a conversation
(`-c`, `--resume`, `--session-id`, `--worktree`, positional prompt, ...), then
`--resume <session> -- <continue_prompt>` is appended.
The resumed session is the parent, not the subagent that hit a limit. Interrupted background
work may need to be restarted by that parent; rotation does not preserve live child processes.
Every account launch gets a different run id so a late hook cannot stop the new account's run.
If no alternative is free, the limit is still recorded and Claude is left running.

Files:

```
~/.claude-accounts/
  config.json     order, cooldown_minutes, continue_prompt
  state.json      per-account limited_until + the limit message
  accounts/<n>/   CLAUDE_CONFIG_DIR of each extra account
  runs/           per-launch session and limit evidence while a wrapper is running
  events.jsonl    session start, limit source, selection and resume/skip diagnostics
  hook-errors.log hook exceptions, if any
```

Environment variables: `CLAUDE_ROTATE_HOME` (default `~/.claude-accounts`),
`CLAUDE_ROTATE_SHARED_DIR` (default `~/.claude`), `CLAUDE_ROTATE_CLAUDE_BIN`.

## Limits

- `claude --bare` skips `--settings` hooks, so an interactive session started that way is not
  moved while it runs; print-mode runs are still moved after they exit.
- Rotation is per wrapper process. Several sessions running at once each move on their own; the
  recorded limit in `state.json` is shared so later launches start on a free account.
- Ctrl-C, Ctrl-\ and Ctrl-Z go to `claude`; the wrapper survives them.
- An external SIGTERM/SIGHUP is forwarded and never causes an automatic relaunch.
- Not tested on Windows (uses `fcntl` and POSIX signals).

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests run the wrapper against `tests/mock_claude.py` in temporary directories and never touch
`~/.claude` or `~/.claude-accounts`.

## License

MIT
