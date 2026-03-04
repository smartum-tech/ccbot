"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Detects dead Claude Code processes (OOM/crash/exit) via pane_current_command
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - DEAD_PROCESS_THRESHOLD: Consecutive polls before dead process triggers (3)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
  - send_restart_browser: Send directory browser after process death/restart
"""

import asyncio
import logging
import time
from pathlib import Path

from telegram import Bot
from telegram.error import BadRequest
from telegram.ext import Application

from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import tmux_manager
from .cleanup import clear_topic_state
from .directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    build_directory_browser,
)
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .message_queue import enqueue_status_update, get_message_queue
from .message_sender import safe_send

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds

# Dead process detection
DEAD_PROCESS_THRESHOLD = 3  # consecutive polls (~3s) before triggering
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish", "dash"}
_dead_process_counts: dict[str, int] = {}  # window_id → consecutive dead count


async def send_restart_browser(
    bot: Bot,
    application: Application,  # type: ignore[type-arg]
    user_id: int,
    thread_id: int,
    notification: str = "⚠️ Claude Code process has exited.\nSelect a directory to start a new session.",
    last_cwd: str | None = None,
) -> None:
    """Send directory browser to a topic after process death or /restart.

    Sets user_data state so the directory browser callback handlers work correctly.
    Uses last_cwd (the session's working directory) if available, otherwise falls
    back to the bot's cwd.
    """
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    default_path = last_cwd if last_cwd and Path(last_cwd).is_dir() else str(Path.cwd())
    msg_text, keyboard, dirs = build_directory_browser(default_path)
    full_text = f"{notification}\n\n{msg_text}"

    await safe_send(
        bot, chat_id, full_text, message_thread_id=thread_id, reply_markup=keyboard
    )

    # Set browsing state so callback handlers work
    # user_data is dict at runtime; Mapping type annotation is overly strict
    all_user_data: dict = application.user_data  # type: ignore[assignment]
    user_data = all_user_data.setdefault(user_id, {})
    user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
    user_data[BROWSE_PATH_KEY] = default_path
    user_data[BROWSE_PAGE_KEY] = 0
    user_data[BROWSE_DIRS_KEY] = dirs
    user_data["_pending_thread_id"] = thread_id


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


async def status_poll_loop(application: Application) -> None:  # type: ignore[type-arg]
    """Background task to poll terminal status for all thread-bound windows."""
    bot = application.bot
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    while True:
        try:
            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for user_id, thread_id, wid in list(
                    session_manager.iter_thread_bindings()
                ):
                    try:
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=session_manager.resolve_chat_id(user_id, thread_id),
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if "Topic_id_invalid" in str(e):
                            # Topic deleted — kill window, unbind, and clean up state
                            w = await tmux_manager.find_window_by_id(wid)
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d for user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        _dead_process_counts.pop(wid, None)
                        session_manager.unbind_thread(user_id, thread_id)
                        await clear_topic_state(user_id, thread_id, bot)
                        logger.info(
                            "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                            user_id,
                            thread_id,
                            wid,
                        )
                        continue

                    # Dead process detection: shell as foreground = Claude exited
                    cmd = w.pane_current_command
                    if cmd and cmd in SHELL_COMMANDS:
                        _dead_process_counts[wid] = _dead_process_counts.get(wid, 0) + 1
                        if _dead_process_counts[wid] >= DEAD_PROCESS_THRESHOLD:
                            logger.info(
                                "Dead process detected: window=%s cmd=%s "
                                "(user=%d, thread=%d)",
                                wid,
                                cmd,
                                user_id,
                                thread_id,
                            )
                            last_cwd = session_manager.get_window_cwd(wid)
                            await tmux_manager.kill_window(w.window_id)
                            session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            await send_restart_browser(
                                bot,
                                application,
                                user_id,
                                thread_id,
                                last_cwd=last_cwd,
                            )
                            _dead_process_counts.pop(wid, None)
                            continue
                    else:
                        _dead_process_counts.pop(wid, None)

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
