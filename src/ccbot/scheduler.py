"""Scheduled tasks (cron jobs) for ccbot.

Allows Claude Code to create delayed or repeating tasks via `ccbot schedule` CLI.
The bot executes tasks at the scheduled time — wakes sessions and sends prompts.

Key components:
  - ScheduledTask: dataclass representing a single scheduled task.
  - TaskScheduler: singleton managing task persistence and CRUD.
  - scheduler_loop: async background task (15s poll) that executes due tasks.
  - schedule_cli_main: CLI entry point for `ccbot schedule` subcommand.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Shell commands indicating Claude Code has exited (reused from status_polling)
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish", "dash"}

# Scheduler poll interval
SCHEDULER_POLL_INTERVAL = 15.0  # seconds

# Max resume attempts before marking task as failed
MAX_RESUME_ATTEMPTS = 2

# Interval name patterns
_INTERVAL_RE = re.compile(r"^(\d+)(m|h|d)$")


def parse_interval(spec: str) -> int | None:
    """Parse an interval spec like '30m', '1h', '2d', 'daily' into seconds.

    Returns None if the spec is invalid.
    """
    if spec == "daily":
        return 86400

    m = _INTERVAL_RE.match(spec)
    if not m:
        return None

    value = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return value * 60
    elif unit == "h":
        return value * 3600
    elif unit == "d":
        return value * 86400
    return None


def parse_at_time(time_str: str) -> float | None:
    """Parse an absolute time like '14:00' into a UTC unix timestamp.

    If the time is in the past today, schedules for tomorrow.
    Uses local timezone for interpretation.
    """
    try:
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
    except (ValueError, AttributeError):
        return None

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        # Schedule for tomorrow
        from datetime import timedelta

        target = target + timedelta(days=1)

    # Convert local time to UTC timestamp
    return target.timestamp()


@dataclass
class ScheduledTask:
    """A single scheduled task."""

    task_id: str
    scheduled_time: float  # Unix timestamp (UTC), next execution time
    prompt: str
    thread_id: int
    window_id: str  # tmux window_id at creation time (fallback)
    cwd: str
    session_id: str
    repeat: str | None  # None (one-shot) or interval: "30m", "1h", "daily"
    created_at: float
    last_executed: float | None
    status: str  # "pending", "running", "completed", "failed"
    description: str
    execution_count: int = 0
    resume_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "scheduled_time": self.scheduled_time,
            "prompt": self.prompt,
            "thread_id": self.thread_id,
            "window_id": self.window_id,
            "cwd": self.cwd,
            "session_id": self.session_id,
            "repeat": self.repeat,
            "created_at": self.created_at,
            "last_executed": self.last_executed,
            "status": self.status,
            "description": self.description,
            "execution_count": self.execution_count,
            "resume_attempts": self.resume_attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        return cls(
            task_id=data["task_id"],
            scheduled_time=data["scheduled_time"],
            prompt=data["prompt"],
            thread_id=data["thread_id"],
            window_id=data["window_id"],
            cwd=data["cwd"],
            session_id=data["session_id"],
            repeat=data.get("repeat"),
            created_at=data["created_at"],
            last_executed=data.get("last_executed"),
            status=data.get("status", "pending"),
            description=data.get("description", ""),
            execution_count=data.get("execution_count", 0),
            resume_attempts=data.get("resume_attempts", 0),
        )

    @property
    def short_id(self) -> str:
        """First 8 chars of task_id for display."""
        return self.task_id[:8]

    def time_until(self) -> str:
        """Human-readable time until execution."""
        delta = self.scheduled_time - time.time()
        if delta <= 0:
            return "now"
        if delta < 60:
            return f"{int(delta)}s"
        if delta < 3600:
            return f"{int(delta / 60)}m"
        if delta < 86400:
            hours = int(delta / 3600)
            mins = int((delta % 3600) / 60)
            return f"{hours}h{mins}m" if mins else f"{hours}h"
        days = int(delta / 86400)
        return f"{days}d"


class TaskScheduler:
    """Manages scheduled task persistence and CRUD.

    Uses fcntl.flock for concurrent access safety (CLI + scheduler_loop).
    Tracks file mtime to avoid unnecessary re-reads.
    """

    def __init__(self, tasks_file: Path) -> None:
        self._tasks_file = tasks_file
        self._tasks: dict[str, ScheduledTask] = {}
        self._last_mtime: float = 0.0

    def _read_locked(self) -> dict[str, ScheduledTask]:
        """Read tasks from file with file locking."""
        if not self._tasks_file.exists():
            return {}

        lock_path = self._tasks_file.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_SH)
                try:
                    raw = json.loads(self._tasks_file.read_text())
                    return {
                        tid: ScheduledTask.from_dict(data) for tid, data in raw.items()
                    }
                except (json.JSONDecodeError, OSError, KeyError) as e:
                    logger.warning("Failed to read scheduled tasks: %s", e)
                    return {}
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except OSError as e:
            logger.error("Failed to acquire lock for reading: %s", e)
            return {}

    def _write_locked(self, tasks: dict[str, ScheduledTask]) -> None:
        """Write tasks to file with file locking."""
        from .utils import atomic_write_json

        self._tasks_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._tasks_file.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    data = {tid: t.to_dict() for tid, t in tasks.items()}
                    atomic_write_json(self._tasks_file, data)
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except OSError as e:
            logger.error("Failed to write scheduled tasks: %s", e)

    def reload_if_changed(self) -> bool:
        """Reload tasks from file if mtime changed. Returns True if reloaded."""
        try:
            mtime = self._tasks_file.stat().st_mtime
        except OSError:
            if self._tasks:
                self._tasks = {}
                self._last_mtime = 0.0
                return True
            return False

        if mtime != self._last_mtime:
            self._tasks = self._read_locked()
            self._last_mtime = mtime
            return True
        return False

    def load_tasks(self) -> None:
        """Force reload tasks from file."""
        self._tasks = self._read_locked()
        try:
            self._last_mtime = self._tasks_file.stat().st_mtime
        except OSError:
            self._last_mtime = 0.0

    def save_tasks(self) -> None:
        """Save current tasks to file."""
        self._write_locked(self._tasks)
        try:
            self._last_mtime = self._tasks_file.stat().st_mtime
        except OSError:
            pass

    def add_task(self, task: ScheduledTask) -> None:
        """Add a task and save."""
        self._tasks[task.task_id] = task
        self.save_tasks()

    def cancel_task(self, task_id: str) -> ScheduledTask | None:
        """Cancel a task by full or prefix ID. Returns the task if found."""
        # Try exact match first
        if task_id in self._tasks:
            task = self._tasks.pop(task_id)
            self.save_tasks()
            return task

        # Try prefix match
        matches = [tid for tid in self._tasks if tid.startswith(task_id)]
        if len(matches) == 1:
            task = self._tasks.pop(matches[0])
            self.save_tasks()
            return task

        return None

    def list_tasks(self) -> list[ScheduledTask]:
        """Return all tasks sorted by scheduled_time."""
        return sorted(self._tasks.values(), key=lambda t: t.scheduled_time)

    def get_tasks_for_thread(self, thread_id: int) -> list[ScheduledTask]:
        """Return pending/running tasks for a specific thread."""
        return sorted(
            [
                t
                for t in self._tasks.values()
                if t.thread_id == thread_id and t.status in ("pending", "running")
            ],
            key=lambda t: t.scheduled_time,
        )

    def get_due_tasks(self) -> list[ScheduledTask]:
        """Return tasks that are due for execution."""
        now = time.time()
        return [
            t
            for t in self._tasks.values()
            if t.scheduled_time <= now and t.status == "pending"
        ]

    def update_task(self, task: ScheduledTask) -> None:
        """Update a task in memory (call save_tasks() to persist)."""
        self._tasks[task.task_id] = task

    def cancel_pending_for_thread(self, thread_id: int) -> int:
        """Cancel all pending tasks for a thread. Returns count cancelled."""
        count = 0
        to_remove = []
        for tid, task in self._tasks.items():
            if task.thread_id == thread_id and task.status == "pending":
                to_remove.append(tid)
                count += 1
        for tid in to_remove:
            del self._tasks[tid]
        if count:
            self.save_tasks()
        return count


# Module-level singleton (initialized lazily)
_task_scheduler: TaskScheduler | None = None


def get_task_scheduler() -> TaskScheduler:
    """Get or create the module-level TaskScheduler singleton."""
    global _task_scheduler
    if _task_scheduler is None:
        from .config import config

        _task_scheduler = TaskScheduler(config.scheduled_tasks_file)
        _task_scheduler.load_tasks()
    return _task_scheduler


async def _execute_task(
    task: ScheduledTask,
    scheduler: TaskScheduler,
    bot: Any,
) -> None:
    """Execute a single scheduled task.

    Resolves the target window, sends the prompt, handles auto-resume if needed.
    """
    from .session import session_manager
    from .tmux_manager import tmux_manager
    from .handlers.message_sender import safe_send

    thread_id = task.thread_id
    chat_id = session_manager.resolve_chat_id(thread_id)

    # Check thread still bound
    wid = session_manager.get_window_for_thread(thread_id)

    if wid:
        # Window bound — check if alive
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            # Window alive — send prompt directly
            await tmux_manager.send_keys(w.window_id, task.prompt)
            task.last_executed = time.time()
            task.execution_count += 1
            await safe_send(
                bot,
                chat_id,
                f"⏰ Scheduled: '{task.description}' — prompt sent.",
                message_thread_id=thread_id,
            )
            _finish_task(task, scheduler)
            return

    # Window dead or thread unbound — try auto-resume
    await _auto_resume_and_send(task, scheduler, bot, chat_id)


async def _auto_resume_and_send(
    task: ScheduledTask,
    scheduler: TaskScheduler,
    bot: Any,
    chat_id: int,
) -> None:
    """Auto-resume a dead session and send the scheduled prompt."""
    import asyncio

    from .session import session_manager
    from .tmux_manager import tmux_manager
    from .handlers.message_sender import safe_send

    thread_id = task.thread_id

    if task.resume_attempts >= MAX_RESUME_ATTEMPTS:
        task.status = "failed"
        scheduler.update_task(task)
        scheduler.save_tasks()
        await safe_send(
            bot,
            chat_id,
            f"❌ Scheduled task '{task.description}' failed — "
            f"could not resume session after {MAX_RESUME_ATTEMPTS} attempts.",
            message_thread_id=thread_id,
        )
        return

    # Validate cwd; fall back to home dir for resume
    resume_cwd = task.cwd
    if not resume_cwd or not Path(resume_cwd).is_dir():
        logger.warning(
            "Task %s cwd '%s' does not exist, falling back to home dir",
            task.short_id,
            resume_cwd,
        )
        resume_cwd = str(Path.home())

    # Create window with resume
    success, message, created_wname, created_wid = await tmux_manager.create_window(
        resume_cwd, resume_session_id=task.session_id
    )
    if not success:
        task.resume_attempts += 1
        scheduler.update_task(task)
        scheduler.save_tasks()
        logger.warning(
            "Resume failed for task %s: %s (attempt %d)",
            task.short_id,
            message,
            task.resume_attempts,
        )
        return

    # Wait for hook
    hook_ok = await session_manager.wait_for_session_map_entry(
        created_wid, timeout=15.0
    )

    # Override session_id for resume (same pattern as bot.py)
    ws = session_manager.get_window_state(created_wid)
    if not hook_ok:
        logger.warning(
            "Hook timed out for scheduled resume window %s, "
            "manually setting session_id=%s",
            created_wid,
            task.session_id,
        )
        ws.session_id = task.session_id
        ws.cwd = task.cwd
        ws.window_name = created_wname
        session_manager._save_state()
    elif ws.session_id != task.session_id:
        ws.session_id = task.session_id
        session_manager._save_state()

    # Bind thread to new window
    session_manager.bind_thread(thread_id, created_wid, window_name=created_wname)

    # Wait and check if Claude Code actually started
    await asyncio.sleep(3.0)
    w = await tmux_manager.find_window_by_id(created_wid)
    if w and w.pane_current_command in SHELL_COMMANDS:
        # Claude Code exited immediately — likely auth failure
        logger.warning(
            "Claude Code exited immediately after resume for task %s (cmd=%s)",
            task.short_id,
            w.pane_current_command,
        )
        await tmux_manager.kill_window(created_wid)
        session_manager.unbind_thread(thread_id)
        task.status = "failed"
        scheduler.update_task(task)

        # Cancel all pending tasks for this thread
        cancelled = scheduler.cancel_pending_for_thread(thread_id)
        scheduler.save_tasks()

        extra = ""
        if cancelled:
            extra = (
                f"\n{cancelled} other pending task(s) for this topic also cancelled."
            )
        await safe_send(
            bot,
            chat_id,
            f"⚠️ Scheduled task '{task.description}' failed — "
            f"Claude Code session could not start. "
            f"Please check authentication and try /restart.{extra}",
            message_thread_id=thread_id,
        )
        return

    # Success — send prompt
    await tmux_manager.send_keys(created_wid, task.prompt)
    task.last_executed = time.time()
    task.execution_count += 1
    await safe_send(
        bot,
        chat_id,
        f"⏰ Scheduled: '{task.description}' — session resumed, prompt sent.",
        message_thread_id=thread_id,
    )
    _finish_task(task, scheduler)


def _finish_task(task: ScheduledTask, scheduler: TaskScheduler) -> None:
    """Mark task as completed or advance to next repeat cycle."""
    if task.repeat:
        interval = parse_interval(task.repeat)
        if interval:
            # Advance scheduled_time, skip past missed cycles
            now = time.time()
            next_time = task.scheduled_time + interval
            while next_time <= now:
                next_time += interval
            task.scheduled_time = next_time
            task.status = "pending"
            task.resume_attempts = 0
            scheduler.update_task(task)
            scheduler.save_tasks()
            return

    task.status = "completed"
    scheduler.update_task(task)
    scheduler.save_tasks()


async def scheduler_loop(application: Any) -> None:
    """Background task that polls for and executes due scheduled tasks."""
    import asyncio

    from .session import session_manager

    bot = application.bot
    scheduler = get_task_scheduler()
    logger.info("Scheduler loop started (interval: %ss)", SCHEDULER_POLL_INTERVAL)

    while True:
        try:
            # Reload if file changed (CLI may have added/cancelled tasks)
            scheduler.reload_if_changed()

            due_tasks = scheduler.get_due_tasks()
            if due_tasks:
                logger.info("Found %d due task(s)", len(due_tasks))

            for task in due_tasks:
                task.status = "running"
                scheduler.update_task(task)
                scheduler.save_tasks()

                # Verify thread still exists in bindings or has chat_id
                try:
                    session_manager.resolve_chat_id(task.thread_id)
                except Exception:
                    logger.warning(
                        "Cannot resolve chat for task %s thread %d, marking failed",
                        task.short_id,
                        task.thread_id,
                    )
                    task.status = "failed"
                    scheduler.update_task(task)
                    scheduler.save_tasks()
                    continue

                try:
                    await _execute_task(task, scheduler, bot)
                except Exception as e:
                    logger.error("Error executing task %s: %s", task.short_id, e)
                    task.status = "failed"
                    scheduler.update_task(task)
                    scheduler.save_tasks()

        except Exception as e:
            logger.error("Scheduler loop error: %s", e)

        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


# --- CLI ---


def _resolve_tmux_context() -> tuple[str, str, str, str, int] | None:
    """Resolve tmux context from TMUX_PANE env var.

    Returns (tmux_session, window_id, window_name, session_window_key, thread_id)
    or None if resolution fails.
    """
    from .utils import ccbot_dir

    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        print(
            "Error: TMUX_PANE not set. Run this from inside a tmux pane.",
            file=sys.stderr,
        )
        return None

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        print(f"Error: Failed to parse tmux context: {raw_output}", file=sys.stderr)
        return None

    tmux_session_name, window_id, window_name = parts
    session_window_key = f"{tmux_session_name}:{window_id}"

    # Resolve thread_id from state.json
    state_file = ccbot_dir() / "state.json"
    if not state_file.exists():
        print("Error: state.json not found. Is the bot running?", file=sys.stderr)
        return None

    try:
        state = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading state.json: {e}", file=sys.stderr)
        return None

    # Reverse lookup: find thread_id for this window_id
    bindings = state.get("thread_bindings", {})
    thread_id = None
    for tid_str, wid in bindings.items():
        if wid == window_id:
            thread_id = int(tid_str)
            break

    if thread_id is None:
        print(
            f"Error: No topic bound to window {window_id}. "
            "This window must be bound to a Telegram topic.",
            file=sys.stderr,
        )
        return None

    return tmux_session_name, window_id, window_name, session_window_key, thread_id


def _resolve_session_info(
    session_window_key: str,
) -> tuple[str, str] | None:
    """Resolve session_id and cwd from session_map.json.

    Returns (session_id, cwd) or None.
    """
    from .utils import ccbot_dir

    map_file = ccbot_dir() / "session_map.json"
    if not map_file.exists():
        print("Error: session_map.json not found.", file=sys.stderr)
        return None

    try:
        session_map = json.loads(map_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading session_map.json: {e}", file=sys.stderr)
        return None

    entry = session_map.get(session_window_key)
    if not entry:
        print(
            f"Error: No session mapping for {session_window_key}.",
            file=sys.stderr,
        )
        return None

    return entry.get("session_id", ""), entry.get("cwd", "")


def schedule_cli_main() -> None:
    """CLI entry point for `ccbot schedule` subcommand."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.WARNING,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="ccbot schedule",
        description="Schedule tasks for Claude Code execution",
    )
    parser.add_argument(
        "--in",
        dest="in_time",
        help="Relative time: 30m, 1h, 2h, 1d",
    )
    parser.add_argument(
        "--at",
        dest="at_time",
        help='Absolute time: "14:00" (local timezone)',
    )
    parser.add_argument(
        "--every",
        help="Repeat interval: 30m, 1h, daily",
    )
    parser.add_argument(
        "--prompt",
        help="Text to send to Claude Code",
    )
    parser.add_argument(
        "--description",
        help="Human-readable label for the task",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_tasks",
        help="List all scheduled tasks",
    )
    parser.add_argument(
        "--cancel",
        help="Cancel a task by ID prefix",
    )

    args = parser.parse_args(sys.argv[2:])

    from .utils import ccbot_dir

    tasks_file = ccbot_dir() / "scheduled_tasks.json"
    scheduler = TaskScheduler(tasks_file)
    scheduler.load_tasks()

    # --list
    if args.list_tasks:
        tasks = scheduler.list_tasks()
        if not tasks:
            print("No scheduled tasks.")
            return
        for t in tasks:
            repeat_str = f" (every {t.repeat})" if t.repeat else ""
            ts = datetime.fromtimestamp(t.scheduled_time).strftime("%Y-%m-%d %H:%M")
            print(
                f"  {t.short_id}  {t.status:<9}  {ts}  "
                f"in {t.time_until()}{repeat_str}  "
                f"{t.description or t.prompt[:40]}"
            )
        return

    # --cancel
    if args.cancel:
        cancelled = scheduler.cancel_task(args.cancel)
        if cancelled:
            print(
                f"Cancelled: {cancelled.short_id} — {cancelled.description or cancelled.prompt[:40]}"
            )
        else:
            print(f"No task found matching '{args.cancel}'")
            sys.exit(1)
        return

    # Schedule a new task — requires --prompt and time spec
    if not args.prompt:
        print("Error: --prompt is required when scheduling a task.", file=sys.stderr)
        sys.exit(1)

    # Resolve time
    scheduled_time: float | None = None
    if args.in_time:
        seconds = parse_interval(args.in_time)
        if seconds is None:
            print(
                f"Error: Invalid time format '{args.in_time}'. Use: 30m, 1h, 2d",
                file=sys.stderr,
            )
            sys.exit(1)
        scheduled_time = time.time() + seconds
    elif args.at_time:
        scheduled_time = parse_at_time(args.at_time)
        if scheduled_time is None:
            print(
                f"Error: Invalid time format '{args.at_time}'. Use: HH:MM (e.g. 14:00)",
                file=sys.stderr,
            )
            sys.exit(1)
    elif args.every:
        # For --every without --in or --at, first execution is after one interval
        seconds = parse_interval(args.every)
        if seconds is None:
            print(
                f"Error: Invalid interval '{args.every}'. Use: 30m, 1h, daily",
                file=sys.stderr,
            )
            sys.exit(1)
        scheduled_time = time.time() + seconds
    else:
        print("Error: Specify --in, --at, or --every for timing.", file=sys.stderr)
        sys.exit(1)

    # Validate repeat interval if specified
    repeat: str | None = args.every
    if repeat and parse_interval(repeat) is None:
        print(f"Error: Invalid repeat interval '{repeat}'.", file=sys.stderr)
        sys.exit(1)

    # Resolve tmux context
    ctx = _resolve_tmux_context()
    if not ctx:
        sys.exit(1)

    tmux_session_name, window_id, window_name, session_window_key, thread_id = ctx

    # Resolve session info
    session_info = _resolve_session_info(session_window_key)
    if not session_info:
        sys.exit(1)

    session_id, cwd = session_info

    description = args.description or args.prompt[:60]

    task = ScheduledTask(
        task_id=str(uuid.uuid4()),
        scheduled_time=scheduled_time,
        prompt=args.prompt,
        thread_id=thread_id,
        window_id=window_id,
        cwd=cwd,
        session_id=session_id,
        repeat=repeat,
        created_at=time.time(),
        last_executed=None,
        status="pending",
        description=description,
    )

    scheduler.add_task(task)

    ts = datetime.fromtimestamp(scheduled_time).strftime("%H:%M:%S")
    repeat_str = f" (repeating every {repeat})" if repeat else ""
    print(f"Scheduled [{task.short_id}]: '{description}' at {ts}{repeat_str}")
