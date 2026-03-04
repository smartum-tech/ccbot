"""Custom shell command handlers defined via CUSTOM_CMD_* env vars.

Executes shell commands directly via subprocess, bypassing Claude Code.
Each command runs in the working directory of the bound session (or $HOME).

Key function: make_handler() — factory that returns an async handler for a given command.
"""

import asyncio
import logging
from pathlib import Path

from collections.abc import Callable, Coroutine
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from ..config import config
from ..session import session_manager
from ..tmux_manager import tmux_manager
from .message_sender import safe_reply

logger = logging.getLogger(__name__)

# Maximum output length sent back to Telegram (leave room for formatting)
_MAX_OUTPUT_LEN = 3900
_TIMEOUT_SECONDS = 120


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


async def _execute_custom_command(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
    cmd_name: str,
    shell_command: str,
) -> None:
    """Execute a custom shell command and reply with its output."""
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)

    # Determine working directory from live tmux pane (host path).
    # session_map.json stores the container path which may differ in Docker setups.
    cwd = Path.home()
    wid = session_manager.resolve_window_for_thread(thread_id)
    if wid:
        w = await tmux_manager.find_window_by_id(wid)
        if w and w.cwd:
            cwd = Path(w.cwd)

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        proc = await asyncio.create_subprocess_shell(
            shell_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SECONDS)
        output = stdout.decode("utf-8", errors="replace").rstrip()
        exit_code = proc.returncode or 0

        if len(output) > _MAX_OUTPUT_LEN:
            output = output[:_MAX_OUTPUT_LEN] + "\n… (truncated)"

        if output:
            text = f"`/{cmd_name}` (exit {exit_code})\n```\n{output}\n```"
        else:
            text = f"`/{cmd_name}` (exit {exit_code}) — no output"

        await safe_reply(update.message, text)

    except TimeoutError:
        proc.kill()  # type: ignore[possibly-undefined]
        await safe_reply(
            update.message, f"`/{cmd_name}` — timed out after {_TIMEOUT_SECONDS}s"
        )
    except Exception:
        logger.exception("Error executing custom command /%s", cmd_name)
        await safe_reply(update.message, f"`/{cmd_name}` — execution error")


_Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


def make_handler(cmd_name: str, shell_command: str) -> _Handler:
    """Factory: return an async handler for a custom shell command."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _execute_custom_command(update, context, cmd_name, shell_command)

    return handler
