"""CLI entry point for `ccbot send-file` — send files to Telegram via outbox.

Writes a JSON request to ~/.ccbot/outbox/<uuid>.json which the bot picks up
and sends to the bound Telegram topic as a document.
"""

import argparse
import os
import sys
import uuid

# Telegram bot API file size limit
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def send_file_cli_main() -> None:
    """CLI entry point for `ccbot send-file <path> [--caption text]`."""
    parser = argparse.ArgumentParser(
        prog="ccbot send-file",
        description="Send a file to the bound Telegram topic",
    )
    parser.add_argument("path", help="Path to the file to send")
    parser.add_argument("--caption", help="Optional caption for the file", default="")

    args = parser.parse_args(sys.argv[2:])

    # Validate file
    file_path = os.path.abspath(args.path)
    if not os.path.isfile(file_path):
        print(f"Error: File not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    file_size = os.path.getsize(file_path)
    if file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        print(
            f"Error: File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve tmux context to get thread_id
    from .scheduler import _resolve_tmux_context

    ctx = _resolve_tmux_context()
    if not ctx:
        sys.exit(1)

    _tmux_session, _window_id, _window_name, _session_window_key, thread_id = ctx

    # Write outbox request
    import time

    from .utils import atomic_write_json, ccbot_dir

    outbox_dir = ccbot_dir() / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)

    request = {
        "thread_id": thread_id,
        "file_path": file_path,
        "caption": args.caption,
        "created_at": time.time(),
    }

    request_file = outbox_dir / f"{uuid.uuid4()}.json"
    atomic_write_json(request_file, request)

    filename = os.path.basename(file_path)
    print(f"Queued: {filename} → Telegram topic")
