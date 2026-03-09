"""CLI entry point for `ccbot send-file` — send files to Telegram via outbox.

Copies the file into ~/.ccbot/outbox/ and writes a JSON manifest alongside it.
The bot picks up the manifest, sends the file to Telegram, and deletes both.
File is copied (not referenced by path) so it works across Docker boundaries.
"""

import argparse
import os
import shutil
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

    # Copy file into outbox and write manifest
    import time

    from .utils import atomic_write_json, ccbot_dir

    outbox_dir = ccbot_dir() / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())
    original_name = os.path.basename(file_path)
    # Preserve extension for Telegram MIME detection
    _, ext = os.path.splitext(original_name)
    staged_name = f"{request_id}{ext}"
    staged_path = outbox_dir / staged_name

    shutil.copy2(file_path, staged_path)

    request = {
        "thread_id": thread_id,
        "staged_file": staged_name,
        "original_name": original_name,
        "caption": args.caption,
        "created_at": time.time(),
    }

    manifest_file = outbox_dir / f"{request_id}.json"
    atomic_write_json(manifest_file, request)

    print(f"Queued: {original_name} → Telegram topic")
