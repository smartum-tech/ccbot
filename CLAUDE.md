# CLAUDE.md

ccmux — Telegram bot that bridges Telegram Forum topics to Claude Code sessions via tmux windows. Each topic is bound to one tmux window running one Claude Code instance.

Tech stack: Python, python-telegram-bot, tmux, uv.

## Common Commands

```bash
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/ccbot/             # Type check — MUST be 0 errors before committing
./scripts/restart.sh                  # Restart the ccbot service after code changes
ccbot hook --install                  # Auto-install Claude Code SessionStart hook
ccbot hook --install-docker           # Install standalone scripts for Docker
```

## Agent Commands (for Claude Code sessions)

These commands let a Claude Code agent interact with the Telegram topic it is bound to.
On host (tmux): use `ccbot` directly. In Docker: use standalone scripts from `~/.ccbot/`.

### Send file to Telegram

Send any file (image, PDF, archive, etc.) to the user in the bound Telegram topic.
The file appears as a Telegram document. Max size: 50 MB.

```bash
# Host
ccbot send-file <path> [--caption "text"]

# Docker
~/.ccbot/ccbot-send-file.sh <path> [--caption "text"]
```

Examples:
```bash
ccbot send-file ./report.pdf --caption "Weekly report"
ccbot send-file /tmp/chart.png
```

### Schedule a task

Create a delayed or repeating task. The bot will send the prompt to this session
at the scheduled time — even if Claude Code has exited (auto-resume).

```bash
# Host
ccbot schedule --in <time> --prompt "text" [--description "label"]
ccbot schedule --at "HH:MM" --prompt "text"
ccbot schedule --every <interval> --prompt "text"
ccbot schedule --list
ccbot schedule --cancel <id>

# Docker
~/.ccbot/ccbot-schedule.sh --in <time> --prompt "text" [--description "label"]
~/.ccbot/ccbot-schedule.sh --at "HH:MM" --prompt "text"
~/.ccbot/ccbot-schedule.sh --every <interval> --prompt "text"
```

Time formats: `30m`, `1h`, `2d`, `daily`. Absolute: `"14:00"` (local timezone).

Examples:
```bash
ccbot schedule --in 2h --prompt "Run tests and report results"
ccbot schedule --every 1h --prompt "Check service health" --description "Health check"
ccbot schedule --at "09:00" --prompt "Good morning! Summarize overnight changes"
ccbot schedule --list
ccbot schedule --cancel abc12345
```

## Core Design Constraints

- **1 Topic = 1 Window = 1 Session** — all internal routing keyed by tmux window ID (`@0`, `@12`), not window name. Window names kept as display names. Same directory can have multiple windows.
- **Topic-only** — no backward-compat for non-topic mode. No `active_sessions`, no `/list`, no General topic routing.
- **No message truncation** at parse layer — splitting only at send layer (`split_message`, 4096 char limit).
- **MarkdownV2 only** — use `safe_reply`/`safe_edit`/`safe_send` helpers (auto fallback to plain text). Internal queue/UI code calls bot API directly with its own fallback.
- **Hook-based session tracking** — `SessionStart` hook writes `session_map.json`; monitor polls it to detect session changes.
- **Message queue per user** — FIFO ordering, message merging (3800 char limit), tool_use/tool_result pairing.
- **Rate limiting** — `AIORateLimiter(max_retries=5)` on the Application (30/s global). On restart, the global bucket is pre-filled to avoid burst against Telegram's server-side counter.

## Code Conventions

- Every `.py` file starts with a module-level docstring: purpose clear within 10 lines, one-sentence summary first line, then core responsibilities and key components.
- Telegram interaction: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates; keep callback data under 64 bytes; use `answer_callback_query` for instant feedback.

## Configuration

- Config directory: `~/.ccbot/` by default, override with `CCBOT_DIR` env var.
- `.env` loading priority: local `.env` > config dir `.env`.
- State files: `state.json` (thread bindings), `session_map.json` (hook-generated), `monitor_state.json` (byte offsets).
- Service commands: `commands.json` — JSON-configured shell commands that work in General topic and child topics without a Claude Code session. See README for format.

## Hook Configuration

Auto-install: `ccbot hook --install`

Docker: `ccbot hook --install-docker` — installs standalone scripts (`docker_hook.sh`, `ccbot-send-file.sh`, `ccbot-schedule.sh`) that work without ccbot in the container.

Or manually in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }]
      }
    ]
  }
}
```

## Docker Setup

When Claude Code runs inside Docker, the `claude_command` should include placeholders:
```bash
# Example CLAUDE_COMMAND env var:
docker run -it -e CCBOT_WINDOW_KEY={CCBOT_WINDOW_KEY} -e CCBOT_MAP_FILE={CCBOT_MAP_FILE} -e CCBOT_THREAD_ID={CCBOT_THREAD_ID} -v ~/.ccbot:/root/.ccbot ... claude
```

Placeholders substituted by the bot at window creation:
- `{CCBOT_WINDOW_KEY}` — tmux session:window_id:window_name (for hook)
- `{CCBOT_MAP_FILE}` — path to session_map.json (for hook)
- `{CCBOT_THREAD_ID}` — Telegram topic ID (for schedule/send-file)

Inside Docker, Claude Code uses standalone scripts instead of `ccbot`:
- `ccbot-send-file.sh <path> [--caption "text"]` — send file to topic
- `ccbot-schedule.sh --in 10m --prompt "text"` — schedule a task

## Architecture Details

See @.claude/rules/architecture.md for full system diagram and module inventory.
See @.claude/rules/topic-architecture.md for topic→window→session mapping details.
See @.claude/rules/message-handling.md for message queue, merging, and rate limiting.
