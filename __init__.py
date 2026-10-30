"""Rex <-> Hermes integration.

Mirrors what ``rex skill setup`` installs for Claude Code / Codex:

* **Session context** — Claude/Codex get a SessionStart hook running
  ``rex __internal session-start``. Hermes' equivalent is a plugin system-prompt
  section: rendered once when a session's prompt is built and frozen for the
  session (prompt-cache safe). Emitted only for sessions whose client (CLI / TUI)
  was launched inside a Rex pane — never for gateway chats, cron, or Desktop.
* **Skill** — ``rex:rex``, a stub that defers to ``rex __internal skill`` so the
  guide always matches the installed Rex version.

Hermes runs every session in one gateway process, so the shell's ``REX_*`` vars
are NOT in ``os.environ`` here. They travel with the session as its client env,
which requires (config.yaml)::

    terminal:
      client_env_passthrough: [REX_SESSION, REX_BLOCK, REX_SERVER, REX_BIN_DIR]

That same allowlist also hands them to the session's terminal-tool subprocesses,
so ``rex -s "$REX_SESSION" -b "$REX_BLOCK"`` works from the agent's shell.

The context text always comes from the installed ``rex`` CLI (``--agent opencode``
is Rex's agent-neutral wording), so upgrading Rex updates what Hermes is told.
Everything fails open: no rex binary, no Rex env, slow/broken rex => no section.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_TIMEOUT_S = 3
_REX_VARS = ("REX_SESSION", "REX_BLOCK", "REX_SERVER", "REX_BIN_DIR")
# Local interactive surfaces only: "local" is a CLI/TUI session served by the gateway.
_LOCAL_PLATFORMS = frozenset({"", "cli", "tui", "local"})
_SKILL_PATH = Path(__file__).parent / "skills" / "rex" / "SKILL.md"


def _rex_env() -> dict[str, str]:
    """REX_* of the session executing here: its shell client's env when the gateway
    bound one, else this process's own (in-process CLI / older Hermes)."""
    src: Mapping[str, str] = os.environ
    try:
        from agent.session_client_env import bound_client_env
        bound = bound_client_env()
        if bound is not None:
            src = bound
    except Exception:
        pass
    return {k: src[k] for k in _REX_VARS if src.get(k)}


def _client_path() -> Optional[str]:
    """The session client's PATH (the gateway's own PATH may lack Rex's bin dir)."""
    try:
        from agent.session_client_env import bound_client_env
        bound = bound_client_env()
        return (bound or {}).get("PATH") or None
    except Exception:
        return None


def _rex_bin(env: Mapping[str, str]) -> Optional[str]:
    found = shutil.which("rex", path=_client_path()) or shutil.which("rex")
    if found:
        return found
    bin_dir = env.get("REX_BIN_DIR")
    cand = Path(bin_dir) / "rex" if bin_dir else None
    return str(cand) if cand and cand.is_file() and os.access(cand, os.X_OK) else None


def _session_start_text(env: Mapping[str, str]) -> str:
    """Rex's own SessionStart context, or '' outside Rex / on any failure."""
    if not env.get("REX_SESSION"):
        return ""
    rex = _rex_bin(env)
    if not rex:
        return ""
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("REX_")} | dict(env)
    try:
        proc = subprocess.run(
            [rex, "--autostart=false", "__internal", "session-start", "--agent", "opencode"],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL, env=child_env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = proc.stdout.strip() if proc.returncode == 0 else ""
    if not text:
        return ""
    # Rex's text names its skill by a bare name; Hermes loads plugin skills namespaced.
    return text + '\n\nIn Hermes, load the Rex guide with skill_view(name="rex:rex").'


def _prompt_section(info: Mapping[str, Any]) -> str:
    if str(info.get("platform") or "") not in _LOCAL_PLATFORMS:
        return ""
    return _session_start_text(_rex_env())


def _warn_if_passthrough_missing() -> None:
    """Without the client-env allowlist the gateway never sees REX_* and the plugin is inert."""
    try:
        from hermes_cli.config import load_config_readonly
        terminal = (load_config_readonly() or {}).get("terminal") or {}
        listed = set(terminal.get("client_env_passthrough") or [])
    except Exception:
        return
    missing = [v for v in _REX_VARS if v not in listed]
    if missing:
        logger.warning(
            "rex plugin: terminal.client_env_passthrough is missing %s — Hermes sessions won't see "
            "which Rex pane they run in. Run: hermes config set terminal.client_env_passthrough "
            "'[\"REX_SESSION\",\"REX_BLOCK\",\"REX_SERVER\",\"REX_BIN_DIR\"]'", ", ".join(missing))


def register(ctx) -> None:
    _warn_if_passthrough_missing()
    ctx.register_system_prompt_section("rex.session", _prompt_section)
    if _SKILL_PATH.exists():
        ctx.register_skill("rex", _SKILL_PATH, description=(
            "Use the rex CLI to manage Rex sessions, open tabs, arrange panes, run commands, "
            "inspect terminal output, and control connected Rex apps."))
