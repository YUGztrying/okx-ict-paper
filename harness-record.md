# Harness Setup Record

- **Harness:** Claude Code
- **Version:** 2.1.257 (Claude Code)
- **Date Configured:** 2026-08-15
- **Official Docs:** https://code.claude.com/docs/en/getting-started

## Boundary Rules
- **Filesystem Read:** Restricted to repository tree (`/home/user/okx-ict-paper`).
- **Filesystem Write:** Strictly repository tree only. Absolute refusal on outside paths.
- **Protected Paths:** `journal/runs.jsonl`, `config.toml`, live trading logic.
- **Network & Sandbox:** Runs inside container VM. External API calls require confirmation.
- **Session Resume:** Default Claude Code session recovery via `/resume` or standard state reload.
