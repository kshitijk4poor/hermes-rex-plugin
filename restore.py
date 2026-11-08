"""Save each pane's Hermes chat for rex-agent-restore, so it resumes after a Rex server restart.

Rex restores panes, tabs and scrollback after its server restarts, but each pane gets a fresh
shell. rex-agent-restore (https://github.com/pattont/RexAgentRestore) resumes the chat that ran
there: its shell integration reads a per-pane record on the first prompt of a restored pane and
runs the agent's resume command (``hermes --resume <id>`` for agent ``hermes``).

Its own hooks find an agent by walking up the process tree from the hook, which can't work for
Hermes: sessions run in the gateway, not in the pane. This module writes the same record from
here instead, with the session id and the pane (``REX_BLOCK``) the session's client env carries.

The record must carry the pane shell's ``REX_AGENT_RESTORE_SHELL_TOKEN``: when Hermes exits
normally, the shell sees its own token in the record and deletes it, so only chats cut off by a
restart come back. That name can't ride ``client_env_passthrough`` (Hermes drops names containing
TOKEN), so it is read from the environment of the pane's foreground process, the Hermes client,
the same way rex-agent-restore's ``capture`` does. No token means the shell integration isn't
loaded in that pane, and nothing is written.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import rexcli
from .rexcli import Target

logger = logging.getLogger(__name__)

AGENT = "hermes"
TOKEN_VAR = "REX_AGENT_RESTORE_SHELL_TOKEN"
# Names read from the client's process env (where the record goes, and whether it is wanted).
_ENV_NAMES = (TOKEN_VAR, "REX_AGENT_RESTORE_STATE_DIR", "REX_AGENT_RESTORE_DISABLE", "XDG_STATE_HOME", "HOME")


def _process_env(pid: int) -> dict[str, str]:
    """The ``_ENV_NAMES`` values of process *pid* (same user only); {} when unreadable."""
    proc = Path(f"/proc/{pid}/environ")
    try:
        if proc.exists():
            pairs = proc.read_bytes().decode("utf-8", "replace").split("\0")
        else:  # macOS: env follows the command, space-separated (values with spaces are cut)
            out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(int(pid))], capture_output=True,
                                 text=True, timeout=2, check=False, stdin=subprocess.DEVNULL).stdout
            pairs = out.split()
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    env: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if sep and name in _ENV_NAMES and name not in env:
            env[name] = value
    return env


def _block_key(block: str) -> Optional[str]:
    key = block.removeprefix("block:")
    return None if not key or "/" in key or ".." in key else key


def _state_dir(env: dict[str, str]) -> Path:
    if env.get("REX_AGENT_RESTORE_STATE_DIR"):
        return Path(env["REX_AGENT_RESTORE_STATE_DIR"])
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or str(Path.home()), ".local", "state")
    return Path(base) / "rex-agent-restore"


def _client(target: Target) -> tuple[int, str, dict[str, str]]:
    """``(pid, cwd, env)`` of the pane's foreground process; pid 0 when Rex can't say."""
    info = rexcli.run_json(target, "block", "call", "-b", target.block, "process") or {}
    fg = info.get("foreground") or {}
    try:
        pid = int(fg.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return pid, str(fg.get("cwd") or ""), (_process_env(pid) if pid else {})


def save(target: Target, session_id: str, *, cwd: str = "") -> bool:
    """Write (or refresh) the pane's record for *session_id*. True when a record was written."""
    key = _block_key(target.block)
    if key is None or not session_id:
        return False
    pid, fg_cwd, env = _client(target)
    token = env.get(TOKEN_VAR, "")
    if not token or env.get("REX_AGENT_RESTORE_DISABLE") == "1":
        return False  # no shell integration in this pane (or turned off): nothing would resume it
    panes = _state_dir(env) / "panes"
    path = panes / f"{key}.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if isinstance(old, dict) and old.get("agent") == AGENT and old.get("chat_id") == session_id \
            and old.get("shell_token") == token:
        return False  # already current
    record = {"version": 1, "block_id": target.block, "rex_session": target.session,
              "rex_server": target.env.get("REX_SERVER", ""), "agent": AGENT, "chat_id": session_id,
              "cwd": cwd or fg_cwd, "source": "hermes-rex-plugin", "shell_token": token,
              "agent_pid": pid, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        panes.mkdir(parents=True, exist_ok=True)
        for d in (panes.parent, panes):
            os.chmod(d, 0o700)
        tmp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("rex plugin: could not save the chat record %s: %s", path, exc)
        tmp.unlink(missing_ok=True)
        return False
    return True
