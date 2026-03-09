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

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application

from ..config import config
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
from .callback_data import CB_CRASH_NEW, CB_CRASH_RESUME
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
_seen_alive: set[str] = set()  # window_ids where Claude was seen running


async def send_restart_browser(
    bot: Bot,
    application: Application,  # type: ignore[type-arg]
    chat_id: int,
    thread_id: int,
    notification: str = "⚠️ Claude Code process has exited.\nSelect a directory to start a new session.",
    last_cwd: str | None = None,
    user_id: int | None = None,
) -> None:
    """Send directory browser to a topic after process death or /restart.

    Sets user_data state so the directory browser callback handlers work correctly.
    Uses last_cwd (the session's working directory) if available, otherwise falls
    back to the bot's cwd.

    user_id is needed to set user_data browsing state for the callback handler.
    If not provided, user_data state is not set (callbacks from any user will work
    if they have user_data initialized).
    """
    fallback = config.root_dir if config.root_dir else Path.cwd()
    default_path = last_cwd if last_cwd and Path(last_cwd).is_dir() else str(fallback)
    msg_text, keyboard, dirs = build_directory_browser(default_path)
    full_text = f"{notification}\n\n{msg_text}"

    # Set browsing state BEFORE sending — ensures callback handlers have
    # the state ready even if send takes time or triggers a callback race.
    # application.user_data is MappingProxyType wrapping defaultdict(dict);
    # __getitem__ triggers auto-creation via defaultdict.__missing__.
    if user_id is not None:
        user_data = application.user_data[user_id]  # type: ignore[index]
        user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        user_data[BROWSE_PATH_KEY] = default_path
        user_data[BROWSE_PAGE_KEY] = 0
        user_data[BROWSE_DIRS_KEY] = dirs
        user_data["_pending_thread_id"] = thread_id

    await safe_send(
        bot, chat_id, full_text, message_thread_id=thread_id, reply_markup=keyboard
    )


async def send_crash_menu(
    bot: Bot,
    chat_id: int,
    thread_id: int,
) -> None:
    """Show Resume/New session menu after crash detection."""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Resume",
                    callback_data=f"{CB_CRASH_RESUME}{thread_id}",
                ),
                InlineKeyboardButton(
                    "🆕 New session",
                    callback_data=f"{CB_CRASH_NEW}{thread_id}",
                ),
            ]
        ]
    )
    await safe_send(
        bot,
        chat_id,
        "⚠️ Claude Code process has exited.\nChoose an action:",
        message_thread_id=thread_id,
        reply_markup=keyboard,
    )


async def update_status_message(
    bot: Bot,
    chat_id: int,
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
                bot, chat_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    interactive_window = get_interactive_window(chat_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # Interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(chat_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # Interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(chat_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (chat_id=%d, window=%s, thread=%s)",
            chat_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, chat_id, window_id, thread_id)
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(
            bot,
            chat_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


async def status_poll_loop(application: Application) -> None:  # type: ignore[type-arg]
    """Background task to poll terminal status for all thread-bound windows."""
    from ..outbox import process_outbox

    bot = application.bot
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    while True:
        try:
            # Process outbox file-send requests
            await process_outbox(bot)

            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for thread_id, wid in list(session_manager.iter_thread_bindings()):
                    chat_id = session_manager.resolve_chat_id(thread_id)
                    try:
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if "Topic_id_invalid" in str(e):
                            # Topic deleted — kill window, unbind, and clean up state
                            w = await tmux_manager.find_window_by_id(wid)
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            session_manager.unbind_thread(thread_id)
                            await clear_topic_state(chat_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d",
                                wid,
                                thread_id,
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

            for thread_id, wid in list(session_manager.iter_thread_bindings()):
                chat_id = session_manager.resolve_chat_id(thread_id)
                try:
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        ws = session_manager.window_states.get(wid)
                        has_session_info = ws is not None and bool(
                            ws.session_id and (ws.host_cwd or ws.cwd)
                        )
                        _dead_process_counts.pop(wid, None)
                        _seen_alive.discard(wid)
                        session_manager.unbind_thread(thread_id)
                        await clear_topic_state(chat_id, thread_id, bot)
                        logger.info(
                            "Cleaned up stale binding: thread=%d window_id=%s",
                            thread_id,
                            wid,
                        )
                        if has_session_info:
                            await send_crash_menu(bot, chat_id, thread_id)
                        else:
                            await send_restart_browser(
                                bot, application, chat_id, thread_id
                            )
                        continue

                    # Dead process detection: shell as foreground = Claude exited
                    # Only trigger for windows where Claude was seen running at
                    # least once (avoids false positives during window creation).
                    cmd = w.pane_current_command
                    if cmd and cmd in SHELL_COMMANDS:
                        if wid in _seen_alive:
                            _dead_process_counts[wid] = (
                                _dead_process_counts.get(wid, 0) + 1
                            )
                            if _dead_process_counts[wid] >= DEAD_PROCESS_THRESHOLD:
                                logger.info(
                                    "Dead process detected: window=%s cmd=%s "
                                    "(thread=%d)",
                                    wid,
                                    cmd,
                                    thread_id,
                                )
                                ws = session_manager.window_states.get(wid)
                                has_session_info = ws is not None and bool(
                                    ws.session_id and (ws.host_cwd or ws.cwd)
                                )
                                # Capture host-side cwd before killing
                                if ws and not ws.host_cwd and w.cwd:
                                    ws.host_cwd = w.cwd
                                await tmux_manager.kill_window(w.window_id)
                                session_manager.unbind_thread(thread_id)
                                await clear_topic_state(chat_id, thread_id, bot)
                                _seen_alive.discard(wid)
                                _dead_process_counts.pop(wid, None)
                                if has_session_info:
                                    await send_crash_menu(bot, chat_id, thread_id)
                                else:
                                    await send_restart_browser(
                                        bot,
                                        application,
                                        chat_id,
                                        thread_id,
                                    )
                                continue
                    else:
                        # Non-shell command running → Claude is alive
                        _seen_alive.add(wid)
                        _dead_process_counts.pop(wid, None)

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(chat_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        chat_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(f"Status update error for thread {thread_id}: {e}")
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
