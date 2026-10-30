"""Rex panes that show work Hermes does out of sight: subagents and kanban workers.

Each Hermes session gets one ``subagents`` window in its Rex session, and kanban workers share one
``kanban`` window. Every child or worker is a pane in that window running ``follow.py`` on its
transcript or log. Panes open with ``--focus=false``, so the user's focus never moves.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import rexcli
from .rexcli import Target

logger = logging.getLogger(__name__)

_FOLLOW = Path(__file__).with_name("follow.py")
# A manifest older than this cannot belong to the subagent starting now.
_MANIFEST_MAX_AGE_S = 300


def _short(text: str, n: int = 40) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class PaneGroup:
    """A window of follower panes, created on first use and re-created if the user closed it."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.window_id = ""
        self.last_block = ""
        self.count = 0

    def _alive(self, target: Target) -> bool:
        if not self.window_id:
            return False
        data = rexcli.run_json(target, "window", "list", "-s", target.session, "--json") or {}
        for win in data.get("windows") or []:
            if win.get("window_id") == self.window_id:
                blocks = [b.get("block_id") for layer in win.get("layers") or []
                          for b in layer.get("blocks") or []]
                if self.last_block not in blocks:
                    self.last_block = blocks[-1] if blocks else ""
                return bool(self.last_block)
        return False

    def open(self, target: Target, label: str, command: list[str]) -> Optional[str]:
        """Start *command* in a new pane of this group's window; return its block id."""
        common = ["--focus=false", "--shell=none", "--label", label, "--json", "--", *command]
        if self._alive(target):
            # Alternate the split direction so panes tile instead of thinning into columns.
            direction = "right" if self.count % 2 else "below"
            data = rexcli.run_json(target, "run", "-s", target.session, "-b", self.last_block,
                                   f"--split={direction}", *common)
        else:
            data = rexcli.run_json(target, "run", "-s", target.session, *common)
            if data and data.get("window_id"):
                self.window_id = data["window_id"]
                self.count = 0
                rexcli.run(target, "window", "rename", "-s", target.session, self.window_id, self.label)
        blocks = (data or {}).get("block_ids") or []
        if not blocks:
            return None
        self.last_block = blocks[0]
        self.count += 1
        return blocks[0]


def follow_command(log: str, *, title: str, linger: int, manifest: str = "", index: int = 0,
                   pid: int = 0) -> list[str]:
    cmd = [sys.executable, str(_FOLLOW), "--log", log, "--title", title, "--linger", str(linger)]
    if manifest:
        cmd += ["--manifest", manifest, "--index", str(index)]
    if pid:
        cmd += ["--pid", str(pid)]
    return cmd


# --- subagents ----------------------------------------------------------------------------------

def _redact(text: str) -> str:
    try:
        from tools.delegation_live_log import _redact as redact
        return redact(text)
    except Exception:
        return text


def live_root() -> Optional[Path]:
    """Hermes' live delegation transcript dir (resolve on the hook thread: it is profile-scoped)."""
    try:
        from tools.delegation_live_log import live_transcript_root
        return live_transcript_root()
    except Exception:
        return None


class SubagentPanes:
    def __init__(self) -> None:
        self._groups: dict[str, PaneGroup] = {}  # parent session id -> its subagents window
        self._claimed: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def find_transcript(self, root: Path, goal: str) -> Optional[tuple[str, str, int]]:
        """``(log, manifest, index)`` of the newest unclaimed task whose goal matches *goal*.

        Delegation writes every task's transcript and manifest.json before the children are
        built, and subagent_start fires as each child is built, so the manifest is already there.
        """
        want = _redact(str(goal or "")[:500])
        now = time.time()
        try:
            dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                          key=lambda d: d.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for d in dirs[:20]:
            manifest = d / "manifest.json"
            try:
                if now - manifest.stat().st_mtime > _MANIFEST_MAX_AGE_S:
                    continue
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            for task in data.get("tasks") or []:
                key = (d.name, int(task.get("index", -1)))
                if task.get("log") and task.get("goal") == want:
                    with self._lock:
                        if key in self._claimed:
                            continue
                        self._claimed.add(key)
                    return task["log"], str(manifest), key[1]
        return None

    def open(self, target: Target, parent_session_id: str, goal: str, root: Optional[Path],
             linger: int) -> None:
        found = self.find_transcript(root, goal) if root else None
        if not found:
            logger.debug("rex plugin: no live transcript for subagent %r", _short(goal))
            return
        log, manifest, index = found
        with self._lock:
            group = self._groups.setdefault(parent_session_id or target.block, PaneGroup("subagents"))
        title = f"subagent {index + 1}: {_short(goal, 80)}"
        group.open(target, f"sub {index + 1}: {_short(goal, 24)}",
                   follow_command(log, title=title, linger=linger, manifest=manifest, index=index))


# --- kanban -------------------------------------------------------------------------------------

class KanbanPanes:
    def __init__(self) -> None:
        self._groups: dict[str, PaneGroup] = {}  # rex session -> its kanban window

    def open(self, target: Target, *, task_id: str, board: Optional[str], assignee: str,
             worker_pid: int, linger: int) -> None:
        try:
            from hermes_cli.kanban_db import worker_log_path
            log = str(worker_log_path(task_id, board=board))
        except Exception:
            logger.debug("rex plugin: kanban log path unavailable", exc_info=True)
            return
        group = self._groups.setdefault(target.session, PaneGroup("kanban"))
        title = f"kanban {task_id} ({assignee or 'unassigned'})"
        group.open(target, f"{task_id} {assignee}".strip(),
                   follow_command(log, title=title, linger=linger, pid=worker_pid))
