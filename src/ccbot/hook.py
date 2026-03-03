"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a window↔session
mapping in <CCBOT_DIR>/session_map.json. Also provides `--install` to
auto-configure the hook in Claude's settings.json.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.ccbot_dir() (shared with config.py).

Key functions: hook_main() (CLI entry), _install_hook().
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _claude_settings_file(claude_config_dir: str | None = None) -> Path:
    """Resolve Claude settings.json path.

    Priority: --claude-config-dir arg > CLAUDE_CONFIG_DIR env > ~/.claude
    """
    if claude_config_dir:
        return Path(claude_config_dir) / "settings.json"
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir) / "settings.json"
    return Path.home() / ".claude" / "settings.json"


# The hook command suffix for detection
_HOOK_COMMAND_SUFFIX = "ccbot hook"

# Name of the hook script file written to ccbot config dir
_DOCKER_HOOK_SCRIPT_NAME = "docker_hook.sh"

# The hook script content. Uses node (guaranteed in Claude Code images)
# for JSON parsing. Reads stdin, checks env vars, updates session_map.json.
_DOCKER_HOOK_SCRIPT_CONTENT = """\
#!/bin/sh
# CCBOT Docker hook — updates session_map.json on SessionStart.
# Env vars: CCBOT_WINDOW_KEY (session:@id:name), CCBOT_MAP_FILE (path).
# Uses node for JSON (guaranteed in Claude Code images).

[ -z "$CCBOT_WINDOW_KEY" ] || [ -z "$CCBOT_MAP_FILE" ] && exit 0

node -e '
const fs = require("fs");
const path = require("path");

let input = "";
process.stdin.on("data", c => input += c);
process.stdin.on("end", () => {
  const payload = JSON.parse(input);
  if (payload.hook_event_name !== "SessionStart") process.exit(0);
  const sid = payload.session_id;
  const cwd = payload.cwd || "";
  if (!sid) process.exit(0);

  const wk = process.env.CCBOT_WINDOW_KEY;
  const mf = process.env.CCBOT_MAP_FILE;
  const parts = wk.split(":");
  const key = parts.slice(0, 2).join(":");
  const wname = parts.slice(2).join(":");

  fs.mkdirSync(path.dirname(mf), { recursive: true });
  let data = {};
  try { data = JSON.parse(fs.readFileSync(mf, "utf8")); } catch {}
  data[key] = { session_id: sid, cwd: cwd, window_name: wname };
  fs.writeFileSync(mf, JSON.stringify(data, null, 2) + "\\n");
});
'
"""


def _is_docker_hook_installed(settings: dict) -> bool:
    """Check if the Docker hook is already installed."""
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])
    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if _DOCKER_HOOK_SCRIPT_NAME in cmd:
                return True
    return False


def _install_docker_hook(claude_config_dir: str | None = None) -> int:
    """Install a Docker-compatible hook into Claude's settings.json.

    Writes a standalone script (docker_hook.sh) to the ccbot config dir and
    registers a short command in settings.json that invokes it via the
    CCBOT_MAP_FILE env var path. The script uses node for JSON manipulation.

    Returns 0 on success, 1 on error.
    """
    from .utils import ccbot_dir

    # Write the hook script to ccbot config dir
    script_path = ccbot_dir() / _DOCKER_HOOK_SCRIPT_NAME
    script_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        script_path.write_text(_DOCKER_HOOK_SCRIPT_CONTENT)
        script_path.chmod(0o755)
    except OSError as e:
        logger.error("Error writing %s: %s", script_path, e)
        print(f"Error writing {script_path}: {e}", file=sys.stderr)
        return 1
    logger.info("Wrote hook script to %s", script_path)
    print(f"Wrote hook script to {script_path}")

    # Install the settings.json entry
    settings_file = _claude_settings_file(claude_config_dir)
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    if _is_docker_hook_installed(settings):
        logger.info("Docker hook already installed in %s", settings_file)
        print(f"Docker hook already installed in {settings_file}")
        return 0

    # The command derives the script path from CCBOT_MAP_FILE at runtime.
    # CCBOT_MAP_FILE is set via Docker -e flag, so dirname gives the ccbot dir
    # where docker_hook.sh lives (mounted from the host).
    hook_command = f'"$(dirname "$CCBOT_MAP_FILE")/{_DOCKER_HOOK_SCRIPT_NAME}"'
    hook_config = {"type": "command", "command": hook_command, "timeout": 10}
    logger.info("Installing Docker hook into %s", settings_file)

    if "hooks" not in settings:
        settings["hooks"] = {}
    if "SessionStart" not in settings["hooks"]:
        settings["hooks"]["SessionStart"] = []

    settings["hooks"]["SessionStart"].append({"hooks": [hook_config]})

    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Docker hook installed successfully in %s", settings_file)
    print(f"Docker hook installed successfully in {settings_file}")
    return 0


def _find_ccbot_path() -> str:
    """Find the full path to the ccbot executable.

    Priority:
    1. shutil.which("ccbot") - if ccbot is in PATH
    2. Same directory as the Python interpreter (for venv installs)
    """
    # Try PATH first
    ccbot_path = shutil.which("ccbot")
    if ccbot_path:
        return ccbot_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where ccbot is installed in a venv
    python_dir = Path(sys.executable).parent
    ccbot_in_venv = python_dir / "ccbot"
    if ccbot_in_venv.exists():
        return str(ccbot_in_venv)

    # Last resort: assume it will be in PATH
    return "ccbot"


def _is_hook_installed(settings: dict) -> bool:
    """Check if ccbot hook is already installed in the settings.

    Detects both 'ccbot hook' and full paths like '/path/to/ccbot hook'.
    """
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            # Match 'ccbot hook' or paths ending with 'ccbot hook'
            if cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX):
                return True
    return False


def _install_hook(claude_config_dir: str | None = None) -> int:
    """Install the ccbot hook into Claude's settings.json.

    Returns 0 on success, 1 on error.
    """
    settings_file = _claude_settings_file(claude_config_dir)
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    # Check if already installed
    if _is_hook_installed(settings):
        logger.info("Hook already installed in %s", settings_file)
        print(f"Hook already installed in {settings_file}")
        return 0

    # Find the full path to ccbot
    ccbot_path = _find_ccbot_path()
    hook_command = f"{ccbot_path} hook"
    hook_config = {"type": "command", "command": hook_command, "timeout": 5}
    logger.info("Installing hook command: %s", hook_command)

    # Install the hook
    if "hooks" not in settings:
        settings["hooks"] = {}
    if "SessionStart" not in settings["hooks"]:
        settings["hooks"]["SessionStart"] = []

    settings["hooks"]["SessionStart"].append({"hooks": [hook_config]})

    # Write back
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Hook installed successfully in %s", settings_file)
    print(f"Hook installed successfully in {settings_file}")
    return 0


def hook_main() -> None:
    """Process a Claude Code hook event from stdin, or install the hook."""
    # Configure logging for the hook subprocess (main.py logging doesn't apply here)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="ccbot hook",
        description="Claude Code session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into Claude's settings.json",
    )
    parser.add_argument(
        "--install-docker",
        action="store_true",
        help="Install Docker-compatible inline hook into Claude's settings.json",
    )
    parser.add_argument(
        "--claude-config-dir",
        help="Path to Claude config directory (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook(args.claude_config_dir))

    if args.install_docker:
        logger.info("Docker hook install requested")
        sys.exit(_install_docker_hook(args.claude_config_dir))

    # Normal hook processing: read JSON from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    # Validate session_id format
    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

    # Get tmux session:window key for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.warning("TMUX_PANE not set, cannot determine window")
        return

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
    # Expected format: "session_name:@id:window_name"
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return
    tmux_session_name, window_id, window_name = parts
    # Key uses window_id for uniqueness
    session_window_key = f"{tmux_session_name}:{window_id}"

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    # Read-modify-write with file locking to prevent concurrent hook races
    from .utils import ccbot_dir

    map_file = ccbot_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": window_name,
                }

                # Clean up old-format key ("session:window_name") if it exists.
                # Previous versions keyed by window_name instead of window_id.
                old_key = f"{tmux_session_name}:{window_name}"
                if old_key != session_window_key and old_key in session_map:
                    del session_map[old_key]
                    logger.info("Removed old-format session_map key: %s", old_key)

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)
