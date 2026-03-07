"""List available commands and Claude Code skills for the current topic.

Scans the session's working directory for project-level .claude/commands/
and .claude/skills/, parses YAML frontmatter for descriptions, and combines
with bot built-in and custom commands.

Key function: tools_command() — /tools handler.
"""

import json
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from ..config import config
from ..session import session_manager
from ..tmux_manager import tmux_manager
from .message_sender import safe_reply

logger = logging.getLogger(__name__)

# Simple YAML frontmatter parser: extract key-value pairs between --- fences
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract simple key: value pairs from YAML frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip("\"'")
            if key and val:
                result[key] = val
    return result


def _scan_claude_commands(cwd: Path) -> list[tuple[str, str]]:
    """Scan .claude/commands/ for slash command .md files.

    Returns list of (command_name, description).
    """
    commands_dir = cwd / ".claude" / "commands"
    if not commands_dir.is_dir():
        return []

    results: list[tuple[str, str]] = []
    for md_file in sorted(commands_dir.glob("*.md")):
        name = md_file.stem  # filename without .md
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        desc = fm.get("description", "")
        results.append((name, desc))
    return results


def _scan_claude_skills(cwd: Path) -> list[tuple[str, str]]:
    """Scan .claude/skills/*/SKILL.md for skill definitions.

    Returns list of (skill_name, description).
    """
    skills_dir = cwd / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []

    results: list[tuple[str, str]] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            # Also check for lowercase
            skill_file = skill_dir / "skill.md"
            if not skill_file.is_file():
                continue
        try:
            text = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = fm.get("name", skill_dir.name)
        desc = fm.get("description", "")
        results.append((name, desc))
    return results


def _scan_global_plugin_commands() -> list[tuple[str, str, str]]:
    """Scan ~/.claude/plugins/ for plugin commands.

    Returns list of (command_name, description, plugin_name).
    """
    plugins_dir = Path.home() / ".claude" / "plugins" / "marketplaces"
    if not plugins_dir.is_dir():
        return []

    results: list[tuple[str, str, str]] = []
    for marketplace in plugins_dir.iterdir():
        if not marketplace.is_dir():
            continue
        for category in ("plugins", "external_plugins"):
            cat_dir = marketplace / category
            if not cat_dir.is_dir():
                continue
            for plugin_dir in sorted(cat_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                commands_dir = plugin_dir / "commands"
                if not commands_dir.is_dir():
                    continue
                for md_file in sorted(commands_dir.glob("*.md")):
                    name = md_file.stem
                    try:
                        text = md_file.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    fm = _parse_frontmatter(text)
                    desc = fm.get("description", "")
                    results.append((name, desc, plugin_dir.name))
    return results


def _scan_mcp_tools(cwd: Path) -> list[str]:
    """Scan .mcp.json for configured MCP server names.

    Returns list of server names (tools are dynamic, so we just list servers).
    """
    mcp_file = cwd / ".mcp.json"
    if not mcp_file.is_file():
        return []
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return sorted(data.keys())
    except (OSError, json.JSONDecodeError):
        pass
    return []


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


async def tools_command(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List available commands and skills for the current topic's session."""
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(thread_id)

    # Determine cwd from the bound session
    cwd: Path | None = None
    if wid:
        w = await tmux_manager.find_window_by_id(wid)
        if w and w.cwd:
            cwd = Path(w.cwd)
        if not cwd:
            cwd_str = session_manager.get_window_cwd(wid)
            if cwd_str:
                cwd = Path(cwd_str)

    sections: list[str] = []

    # 1. Bot built-in commands
    builtin_lines = [
        "/start — Start the bot",
        "/history — View message history",
        "/screenshot — Capture terminal screenshot",
        "/esc — Send Escape key",
        "/kill — Kill the session",
        "/unbind — Unbind topic from session",
        "/restart — Restart session",
        "/usage — Show token usage",
        "/tools — This command",
    ]
    sections.append("🤖 *Bot commands*\n" + "\n".join(builtin_lines))

    # 2. Custom shell commands (CUSTOM_CMD_*)
    if config.custom_commands:
        lines = [f"/{name}" for name in sorted(config.custom_commands)]
        sections.append("🔧 *Custom commands*\n" + "\n".join(lines))

    # 3. Service commands (commands.json)
    if config.service_commands:
        lines = [
            f"/{name} — {sc.description}"
            for name, sc in sorted(config.service_commands.items())
        ]
        sections.append("🛠 *Service commands*\n" + "\n".join(lines))

    # 4. CC skill commands (CC_CMD_*)
    if config.cc_skill_commands:
        lines = []
        for tg_name, desc in sorted(config.cc_skill_commands.items()):
            lines.append(f"/{tg_name} — {desc}")
        sections.append("⚡ *Claude Code shortcuts*\n" + "\n".join(lines))

    # 4. Claude Code built-in commands (forwarded via tmux)
    cc_lines = [
        "/clear — Clear conversation history",
        "/compact — Compact conversation context",
        "/cost — Show token/cost usage",
        "/help — Show Claude Code help",
        "/memory — Edit CLAUDE.md",
        "/model — Switch AI model",
    ]
    sections.append("💬 *Claude Code commands*\n" + "\n".join(cc_lines))

    # 5. Global plugin commands
    plugin_commands = _scan_global_plugin_commands()
    if plugin_commands:
        lines = []
        for name, desc, plugin in plugin_commands:
            if desc:
                lines.append(f"/{name} — {desc} ({plugin})")
            else:
                lines.append(f"/{name} ({plugin})")
        sections.append("🧩 *Plugin commands*\n" + "\n".join(lines))

    # 6. Project-level commands and skills (from .claude/ in session cwd)
    if cwd and cwd.is_dir():
        project_commands = _scan_claude_commands(cwd)
        if project_commands:
            lines = []
            for name, desc in project_commands:
                if desc:
                    lines.append(f"/{name} — {desc}")
                else:
                    lines.append(f"/{name}")
            sections.append(
                f"📂 *Project commands* (`{cwd.name}/.claude/commands/`)\n"
                + "\n".join(lines)
            )

        project_skills = _scan_claude_skills(cwd)
        if project_skills:
            lines = []
            for name, desc in project_skills:
                if desc:
                    lines.append(f"/{name} — {desc}")
                else:
                    lines.append(f"/{name}")
            sections.append(
                f"🎯 *Project skills* (`{cwd.name}/.claude/skills/`)\n"
                + "\n".join(lines)
            )

        mcp_servers = _scan_mcp_tools(cwd)
        if mcp_servers:
            lines = [f"• {s}" for s in mcp_servers]
            sections.append("🔌 *MCP servers* (`.mcp.json`)\n" + "\n".join(lines))
    elif not wid:
        sections.append("_No session bound to this topic._")

    text = "\n\n".join(sections)
    await safe_reply(update.message, text)
