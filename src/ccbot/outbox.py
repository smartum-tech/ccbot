"""Bot-side outbox processor — handles queued CLI requests from Docker/host.

Called from status_poll_loop each cycle. Reads JSON request files from
~/.ccbot/outbox/, dispatches by type (send-file, schedule), then deletes
the request file. Supports both ccbot CLI and standalone Docker scripts.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from telegram import Bot

from .config import config
from .session import session_manager

logger = logging.getLogger(__name__)

# Stale request threshold (seconds)
STALE_THRESHOLD = 300.0  # 5 minutes


async def process_outbox(bot: Bot) -> None:
    """Process pending outbox requests (send-file, schedule, etc.)."""
    outbox_dir = config.outbox_dir
    if not outbox_dir.is_dir():
        return

    try:
        entries = os.listdir(outbox_dir)
    except OSError:
        return

    for entry in entries:
        if not entry.endswith(".json"):
            continue

        request_path = outbox_dir / entry
        try:
            raw = json.loads(request_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Outbox: failed to read %s: %s", entry, e)
            _safe_delete(request_path)
            continue

        created_at = raw.get("created_at", 0.0)
        if time.time() - created_at > STALE_THRESHOLD:
            logger.info("Outbox: deleting stale request %s", entry)
            _safe_delete(request_path)
            continue

        request_type = raw.get("type", "send-file")

        try:
            if request_type == "schedule":
                await _process_schedule(bot, raw, entry)
            else:
                await _process_send_file(bot, raw, entry)
        except Exception as e:
            logger.error("Outbox: error processing %s: %s", entry, e)

        _safe_delete(request_path)


async def _process_send_file(bot: Bot, raw: dict[str, Any], entry: str) -> None:
    """Process a send-file outbox request.

    Supports two formats:
    - New (staged): file copied into outbox dir, referenced by staged_file key
    - Legacy (path): file referenced by absolute file_path (same-host only)
    """
    thread_id: int = raw.get("thread_id", 0)
    caption: str = raw.get("caption", "")

    # Resolve file location (staged in outbox dir, or legacy absolute path)
    staged_file: str = raw.get("staged_file", "")
    original_name: str = raw.get("original_name", "")
    legacy_path: str = raw.get("file_path", "")

    if staged_file:
        file_path = str(config.outbox_dir / staged_file)
        filename = original_name or staged_file
    elif legacy_path:
        file_path = legacy_path
        filename = os.path.basename(legacy_path)
    else:
        logger.warning("Outbox: no file in send-file request %s", entry)
        return

    if not thread_id:
        logger.warning("Outbox: invalid send-file request %s", entry)
        _safe_delete_if_staged(file_path, staged_file)
        return

    chat_id = session_manager.resolve_chat_id(thread_id)

    if not os.path.isfile(file_path):
        logger.warning("Outbox: file not found: %s", file_path)
        return

    try:
        with open(file_path, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filename,
                message_thread_id=thread_id,
                caption=caption or None,
            )
        logger.info("Outbox: sent %s to thread %d", filename, thread_id)
    except Exception as e:
        logger.error("Outbox: failed to send %s: %s", filename, e)
    finally:
        # Clean up staged file (copied into outbox by CLI)
        if staged_file:
            _safe_delete(file_path)


async def _process_schedule(bot: Bot, raw: dict[str, Any], entry: str) -> None:
    """Process a schedule outbox request (from Docker script)."""
    from .handlers.message_sender import safe_send
    from .scheduler import (
        ScheduledTask,
        _resolve_session_info,
        get_task_scheduler,
        parse_at_time,
        parse_interval,
    )

    thread_id: int = raw.get("thread_id", 0)
    args: list[str] = raw.get("args", [])

    if not thread_id or not args:
        logger.warning("Outbox: invalid schedule request %s", entry)
        return

    chat_id = session_manager.resolve_chat_id(thread_id)

    # Parse args (same as schedule_cli_main but without argparse exit-on-error)
    import argparse
    import uuid

    parser = argparse.ArgumentParser(exit_on_error=False)
    parser.add_argument("--in", dest="in_time")
    parser.add_argument("--at", dest="at_time")
    parser.add_argument("--every")
    parser.add_argument("--prompt")
    parser.add_argument("--description")

    try:
        parsed = parser.parse_args(args)
    except (SystemExit, argparse.ArgumentError) as e:
        await safe_send(
            bot, chat_id, f"❌ Schedule error: {e}", message_thread_id=thread_id
        )
        return

    if not parsed.prompt:
        await safe_send(
            bot,
            chat_id,
            "❌ Schedule error: --prompt is required.",
            message_thread_id=thread_id,
        )
        return

    # Resolve time
    scheduled_time: float | None = None
    if parsed.in_time:
        seconds = parse_interval(parsed.in_time)
        if seconds is None:
            await safe_send(
                bot,
                chat_id,
                f"❌ Invalid time: '{parsed.in_time}'. Use: 30m, 1h, 2d",
                message_thread_id=thread_id,
            )
            return
        scheduled_time = time.time() + seconds
    elif parsed.at_time:
        scheduled_time = parse_at_time(parsed.at_time)
        if scheduled_time is None:
            await safe_send(
                bot,
                chat_id,
                f"❌ Invalid time: '{parsed.at_time}'. Use: HH:MM",
                message_thread_id=thread_id,
            )
            return
    elif parsed.every:
        seconds = parse_interval(parsed.every)
        if seconds is None:
            await safe_send(
                bot,
                chat_id,
                f"❌ Invalid interval: '{parsed.every}'. Use: 30m, 1h, daily",
                message_thread_id=thread_id,
            )
            return
        scheduled_time = time.time() + seconds
    else:
        await safe_send(
            bot,
            chat_id,
            "❌ Schedule error: specify --in, --at, or --every.",
            message_thread_id=thread_id,
        )
        return

    repeat: str | None = parsed.every
    if repeat and parse_interval(repeat) is None:
        await safe_send(
            bot,
            chat_id,
            f"❌ Invalid repeat interval: '{repeat}'.",
            message_thread_id=thread_id,
        )
        return

    # Resolve window_id and session info from state
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_send(
            bot,
            chat_id,
            "❌ Schedule error: no window bound to this topic.",
            message_thread_id=thread_id,
        )
        return

    ws = session_manager.window_states.get(wid)
    session_id = ws.session_id if ws else ""
    cwd = ws.cwd if ws else ""

    # Fall back to session_map.json if window_states is incomplete
    if not session_id:
        session_window_key = f"{config.tmux_session_name}:{wid}"
        info = _resolve_session_info(session_window_key)
        if info:
            session_id, cwd = info

    description = parsed.description or parsed.prompt[:60]

    task = ScheduledTask(
        task_id=str(uuid.uuid4()),
        scheduled_time=scheduled_time,
        prompt=parsed.prompt,
        thread_id=thread_id,
        window_id=wid,
        cwd=cwd,
        session_id=session_id,
        repeat=repeat,
        created_at=time.time(),
        last_executed=None,
        status="pending",
        description=description,
    )

    scheduler = get_task_scheduler()
    scheduler.add_task(task)

    ts = datetime.fromtimestamp(scheduled_time).strftime("%H:%M:%S")
    repeat_str = f" (repeating every {repeat})" if repeat else ""
    await safe_send(
        bot,
        chat_id,
        f"⏰ Scheduled [{task.short_id}]: '{description}' at {ts}{repeat_str}",
        message_thread_id=thread_id,
    )
    logger.info("Outbox: scheduled task %s for thread %d", task.short_id, thread_id)


def _safe_delete(path: os.PathLike[str] | str) -> None:
    """Delete a file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _safe_delete_if_staged(file_path: str, staged_file: str) -> None:
    """Delete a staged file if it exists in the outbox."""
    if staged_file:
        _safe_delete(file_path)
