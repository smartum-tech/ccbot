"""Custom and service shell command handlers.

Custom commands: defined via CUSTOM_CMD_* env vars, run in bound session cwd.
Service commands: defined via commands.json, run anywhere (including General topic),
with CCBOT_* env vars and user arguments appended.

Key functions: make_handler(), make_service_handler().
"""

import asyncio
import logging
import os
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
    extra_env: dict[str, str] | None = None,
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

    env = None
    if extra_env:
        env = {**os.environ, **extra_env}

    try:
        proc = await asyncio.create_subprocess_shell(
            shell_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env=env,
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


def make_service_handler(cmd_name: str, shell_command: str) -> _Handler:
    """Factory: return an async handler for a service command from commands.json.

    Unlike custom commands, service commands:
    - Work in General topic (no _get_thread_id gate)
    - Append user arguments to the shell command
    - Pass CCBOT_* env vars to the subprocess
    """

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not config.is_user_allowed(user.id):
            return
        if not update.message:
            return

        # Extract user arguments: "/cmd arg1 arg2" → "arg1 arg2"
        text = update.message.text or ""
        parts = text.split(maxsplit=1)
        user_args = parts[1] if len(parts) > 1 else ""

        # Build full command with appended args
        full_command = f"{shell_command} {user_args}" if user_args else shell_command

        # Resolve session info (works in both General and child topics)
        msg = update.message
        tid = getattr(msg, "message_thread_id", None)
        # For child topics, resolve window/cwd
        thread_id = tid if tid and tid != 1 else None
        wid = session_manager.resolve_window_for_thread(thread_id)
        session_cwd = ""
        window_id = ""
        if wid:
            window_id = wid
            cwd_str = session_manager.get_window_cwd(wid)
            if cwd_str:
                session_cwd = cwd_str
            else:
                w = await tmux_manager.find_window_by_id(wid)
                if w and w.cwd:
                    session_cwd = w.cwd

        extra_env: dict[str, str] = {
            "CCBOT_ARGS": user_args,
            "CCBOT_CHAT_ID": str(msg.chat_id),
            "CCBOT_THREAD_ID": str(tid) if tid and tid != 1 else "",
            "CCBOT_USER_ID": str(user.id),
            "CCBOT_USER_NAME": user.first_name or "",
            "CCBOT_SESSION_CWD": session_cwd,
            "CCBOT_WINDOW_ID": window_id,
        }

        await _execute_custom_command(
            update, context, cmd_name, full_command, extra_env=extra_env
        )

    return handler
