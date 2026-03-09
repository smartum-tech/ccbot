"""Bot-side outbox processor — sends queued files to Telegram topics.

Called from status_poll_loop each cycle. Reads JSON request files from
~/.ccbot/outbox/, sends each file as a Telegram document, then deletes
the request file.
"""

import json
import logging
import os
import time

from telegram import Bot

from .config import config
from .session import session_manager

logger = logging.getLogger(__name__)

# Stale request threshold (seconds)
STALE_THRESHOLD = 300.0  # 5 minutes


async def process_outbox(bot: Bot) -> None:
    """Process pending file-send requests from the outbox directory."""
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

        thread_id: int = raw.get("thread_id", 0)
        file_path: str = raw.get("file_path", "")
        caption: str = raw.get("caption", "")

        if not thread_id or not file_path:
            logger.warning("Outbox: invalid request %s", entry)
            _safe_delete(request_path)
            continue

        # Resolve chat_id
        try:
            chat_id = session_manager.resolve_chat_id(thread_id)
        except Exception as e:
            logger.warning(
                "Outbox: cannot resolve chat for thread %d: %s", thread_id, e
            )
            _safe_delete(request_path)
            continue

        # Validate file still exists
        if not os.path.isfile(file_path):
            logger.warning("Outbox: file no longer exists: %s", file_path)
            _safe_delete(request_path)
            continue

        # Send document
        try:
            filename = os.path.basename(file_path)
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
            logger.error("Outbox: failed to send %s: %s", file_path, e)

        _safe_delete(request_path)


def _safe_delete(path: os.PathLike[str] | str) -> None:
    """Delete a file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass
