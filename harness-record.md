# Harness Record

- **Harness:** Claude Code 2.1.257 (`claude --version`, run in-session)
- **Verified:** 2026-09-01, remote container session, repo `YUGztrying/okx-ict-paper`
- **Container:** Linux 6.18.44-fc-v22, process runs as `root` (uid 0)

Every line below was tested in the session that wrote this file. Anything
that could not be tested is either marked as such or left out.

## What is enforced

- **Auto-mode classifier on Bash.** A classifier blocked two commands that
  read files under `/root/.claude/` (a settings file and hook scripts). This
  is the only boundary that actually stopped an action this session. The
  session later left auto mode; behaviour under other permission modes was
  not exercised.
- **Outbound network goes through an agent proxy.** `HTTPS_PROXY`,
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS` are set
  and point at `/root/.ccr/ca-bundle.crt`. `git push` to github.com
  succeeded with no confirmation prompt. External calls are governed by the
  environment's network policy, not per-call approval.
- **Git identity.** `user.name`/`user.email` are preset to
  `Claude <noreply@anthropic.com>`; commits made here carry that author.
- **GitHub access is scoped to this repository** by the harness. Every
  GitHub tool call made this session was in scope; out-of-scope calls were
  not attempted, so the denial itself is asserted by the harness, not observed.

## How it was verified

| Claim | Test | Result |
|---|---|---|
| No repo-level permission rules or hooks | `ls .claude`, `ls .git/hooks` (non-sample), `ls CODEOWNERS .github/CODEOWNERS` | none exist |
| Reads outside the repo tree are allowed | `ls /root/.claude`, `ls -l /root/.ccr/ca-bundle.crt` | succeeded |
| Reads outside the repo tree are partly gated | `head` on `/root/.claude/*hook*.{sh,py}` | blocked by classifier |
| Writes outside the repo tree are possible | `test -w` on `/tmp`, `/root`, `/etc`, `/home/user` (no write performed) | all writable as root |
| `config.toml` and `journal/runs.jsonl` are not protected by the repo | absence of settings, hooks, CODEOWNERS above | nothing would stop an edit; not tested by editing |
| Network needs no per-call confirmation | `git push -u origin claude/repo-overview-qpsbjv` | pushed without prompt |
| CLI version | `claude --version` | `2.1.257 (Claude Code)` |

Hook scripts exist at the harness level (`/root/.claude/stop-hook-git-check.sh`,
`stop-hook-reply-gate.py`, `user-prompt-submit-reply-reminder.py`,
`session-start-git-identity.sh`). Their contents could not be read, so what
they enforce is unknown beyond the git identity observed above. None blocked
the README edit, commit or push made this session.

## What is instruction-only

These are rules the model is told to follow. Nothing in the environment
checks them; the tests above found nothing that would stop the corresponding
action.

- `CLAUDE.md`: never touch live trading logic without asking; never write
  outside this repo without asking; never modify API keys or config with
  secrets without asking.
- Session prompt: develop only on `claude/repo-overview-qpsbjv`, never push
  to another branch, open a draft PR after pushing.
- "Protected paths" in the previous version of this file. No mechanism
  backs them.

## Removed from the previous version

- "Date Configured: 2026-08-15": no source for it.
- "Official Docs" URL: not fetched.
- "Session Resume" line: not testable from inside a session.
- "Filesystem Read/Write restricted to repository tree": false, see table.
- "External API calls require confirmation": false, see table.
