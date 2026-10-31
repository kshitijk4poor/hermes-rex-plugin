"""Rex <-> Hermes integration.

Mirrors what ``rex skill setup`` installs for Claude Code / Codex, then adds what only a plugin
inside Hermes can do:

* **Session context** — a plugin system-prompt section carrying Rex's own SessionStart text
  (``rex __internal session-start``), rendered once per session and frozen (prompt-cache safe).
  Only for sessions whose CLI/TUI client was launched inside a Rex pane.
* **Skill** — ``rex:rex``, a stub that defers to ``rex __internal skill``.
* **Status titles** — the session's window label gets a status mark: ● busy, ? waiting for you,
  ✓ done, ■ stopped, ✗ failed.
* **Alerts** — a macOS notification when Hermes waits for approval / a clarify answer / sudo
  and its pane isn't in view.
* **Label sync** — ``/title`` and ``/resume`` relabel the window; when the session ends the
  window gets back the label it had before.
* **Visible panes** — subagents and kanban workers each get a pane following their transcript.

Hermes runs every session in one gateway process, so the shell's ``REX_*`` vars are NOT in
``os.environ`` here. They travel with the session as its client env, which requires::

    terminal:
      client_env_passthrough: [REX_SESSION, REX_BLOCK, REX_SERVER, REX_BIN_DIR]

Settings live under ``plugins.entries.rex.settings`` (see ``DEFAULTS``). Everything fails open:
no rex binary, no Rex env, slow/broken rex => nothing happens, never an error.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from . import rexcli
from .panes import KanbanPanes, SubagentPanes, live_root
from .rexcli import LOCAL_PLATFORMS, REX_VARS, Registry, Target, Worker, current_target
from .titles import Titles, alert, slug

logger = logging.getLogger(__name__)

_SKILL_PATH = Path(__file__).parent / "skills" / "rex" / "SKILL.md"

DEFAULTS: dict[str, Any] = {
    "session_naming": True,      # Rex's "name the session after the task" (+ the window, same label)
    "status_titles": True,       # ● ? ✓ ■ ✗ prefix on the session's window label
    "alerts": "unfocused",       # unfocused | always | off
    "visible_commands": True,    # tell the agent to run servers/watchers/long jobs in Rex panes
    "subagent_panes": True,      # a pane per delegate_task child, following its transcript
    "kanban_panes": True,        # a pane per kanban worker, following its log
    "pane_linger_seconds": 300,  # how long a finished follower pane stays before it closes
}
# A kanban worker opens its pane in the Rex session Hermes was last active in, if this recent.
_LAST_TARGET_MAX_AGE_S = 12 * 3600
# A child whose subagent_stop never arrives stops holding its parent's ● after this long.
_CHILD_MAX_AGE_S = 2 * 3600
_STATUS_FOR_END = (("completed", "done"), ("interrupted", "stopped"), ("failed", "failed"))

_ctx: Any = None
_worker = Worker()
_registry = Registry()
_titles = Titles()
_subagents = SubagentPanes()
_kanban = KanbanPanes()
_last_saved: dict[str, Any] = {"key": None, "at": 0.0}
_last_saved_lock = threading.Lock()


def setting(key: str) -> Any:
    default = DEFAULTS[key]
    if _ctx is None:
        return default
    try:
        value = _ctx.get_config(key, default)
    except Exception:
        return default
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return str(value)


# --- system prompt ------------------------------------------------------------------------------

def _session_start_text(target: Target, *, naming: bool) -> str:
    """Rex's own SessionStart context, or '' on any failure."""
    rc, out = rexcli.run(target, "__internal", "session-start", "--agent", "opencode",
                         f"--feature-session-naming={'true' if naming else 'false'}")
    return out.strip() if rc == 0 else ""


def _shares_rex_session(target: Target) -> bool:
    """True when the Rex session has windows besides this one, so its label isn't ours to set."""
    data = rexcli.run_json(target, "window", "list", "-s", target.session, "--json") or {}
    return len(data.get("windows") or []) > 1


def _hermes_notes(naming: bool, *, window_only: bool = False) -> list[str]:
    notes = []
    if window_only:
        notes.append(
            "Name your window after the task as part of handling the first request that identifies "
            "the work: run `rex window rename <label>` without waiting for the user to ask. Use two to "
            "four lowercase words joined by hyphens, such as `fix-login-redirect`. Don't rename the "
            "Rex session: other windows share it. Without a window argument the command renames the "
            "window holding REX_BLOCK, so other windows are left alone.")
    elif naming:
        notes.append(
            "When you rename the session, also rename your own window with "
            "`rex window rename <label>`, using the same label. Without a window argument it "
            "renames the window holding REX_BLOCK, so other windows are left alone.")
    if setting("status_titles"):
        notes.append(
            "Your window label may start with a status mark (● ? ✓ ■ ✗). Hermes manages it, so "
            "leave it out of labels you set.")
    if setting("visible_commands"):
        notes.append(
            "Run dev servers, watchers, log tails and long builds or test runs the user may want "
            "to watch in a Rex pane beside yours, not as a hidden background process: "
            "`rex run -b \"$REX_BLOCK\" --split=below --ratio 35 --focus=false --keep-open "
            "--label <name> -- <command>`. Read it with `rex capture -b <block> --trim`, and close "
            "panes you opened once you no longer need them. Ask before typing into or closing "
            "panes you didn't open.")
    # Rex's text names its skill by a bare name; Hermes loads plugin skills namespaced.
    notes.append('In Hermes, load the Rex guide with skill_view(name="rex:rex").')
    return notes


def _prompt_section(info: Mapping[str, Any]) -> str:
    if str(info.get("platform") or "") not in LOCAL_PLATFORMS:
        return ""
    target = current_target()
    if target is None:
        return ""
    naming = setting("session_naming")
    # Several windows share one Rex session label: naming it after one chat's task would relabel
    # everyone's session, so a chat that isn't alone names only its window.
    window_only = naming and _shares_rex_session(target)
    text = _session_start_text(target, naming=naming and not window_only)
    if not text:
        return ""
    return "\n\n".join([text, *_hermes_notes(naming, window_only=window_only)])


# --- target bookkeeping -------------------------------------------------------------------------

def _remember(target: Target, session_id: str) -> None:
    _registry.remember(target, session_id=session_id, session_key=rexcli.current_session_key())
    if setting("session_naming"):
        _worker.submit(lambda: _titles.note_session_label(target))
    key = (target.session, target.block)
    now = time.time()
    with _last_saved_lock:
        if _last_saved["key"] == key and now - _last_saved["at"] < 60:
            return
        _last_saved.update(key=key, at=now)
    if _ctx is not None:
        try:
            _ctx.state.set("last_target", {**target.to_json(), "ts": now})
        except Exception:
            logger.debug("rex plugin: could not save last target", exc_info=True)


def _last_target() -> Optional[Target]:
    if _ctx is None:
        return None
    try:
        data = _ctx.state.get("last_target")
    except Exception:
        return None
    if not isinstance(data, dict) or time.time() - float(data.get("ts") or 0) > _LAST_TARGET_MAX_AGE_S:
        return None
    return Target.from_json(data)


def _target_for(*, session_id: str = "", session_key: str = "", platform: Any = "") -> Optional[Target]:
    """The pane of the session a hook is about: the bound env inside a turn, else the registry."""
    if str(platform or "") not in LOCAL_PLATFORMS:
        return None
    return current_target() or _registry.lookup(session_id=session_id, session_key=session_key)


def _status(target: Optional[Target], state: Optional[str]) -> None:
    if target is not None and setting("status_titles"):
        _worker.submit(lambda: _titles.set_status(target, state))


# --- subagents still running after their parent's turn ended ------------------------------------
# A background delegate_task lets the parent's turn finish while its children work. The parent's
# end-of-turn mark waits until the last child's subagent_stop, so ✓ never shows over live work.

_children: dict[str, dict[str, float]] = {}   # parent session id -> {child session id: started at}
_pending_end: dict[str, str] = {}             # parent session id -> end-of-turn state on hold
_children_lock = threading.Lock()


def _running_children(parent: str) -> int:
    with _children_lock:
        kids = _children.get(parent) or {}
        cutoff = time.time() - _CHILD_MAX_AGE_S
        for child in [c for c, started in kids.items() if started < cutoff]:
            kids.pop(child)  # a child whose subagent_stop never came must not pin ● forever
        return len(kids)


def _child_started(parent: str, child: str) -> None:
    with _children_lock:
        _children.setdefault(parent, {})[child] = time.time()


def _child_stopped(parent: str, child: str) -> Optional[str]:
    """Drop *child*; the held end state once its parent has no running children left."""
    with _children_lock:
        kids = _children.get(parent) or {}
        kids.pop(child, None)
        if kids:
            return None
        _children.pop(parent, None)
        return _pending_end.pop(parent, None)


# --- hooks --------------------------------------------------------------------------------------

def _on_session_start(session_id: str = "", platform: str = "", **_: Any) -> None:
    target = _target_for(platform=platform)
    if target is not None:
        _remember(target, session_id)


def _pre_llm_call(session_id: str = "", platform: str = "", parent_session_id: str = "", **_: Any) -> None:
    if parent_session_id:  # a subagent's turn runs in its parent's client env; not our pane
        _registry.child_sessions.add(session_id)
        return None
    target = _target_for(platform=platform)
    if target is not None:
        with _children_lock:
            _pending_end.pop(session_id, None)  # a new turn supersedes a held end state
        _remember(target, session_id)
        _status(target, "busy")
    return None  # a pre_llm_call return value is injected into the turn; we add nothing


def _on_session_end(session_id: str = "", platform: str = "", **kw: Any) -> None:
    """Fires once per turn (not per session) with completed / interrupted / failed."""
    if session_id in _registry.child_sessions:
        return
    state = next((s for flag, s in _STATUS_FOR_END if kw.get(flag)), "done")
    if state == "done" and _running_children(session_id):
        with _children_lock:
            _pending_end[session_id] = state  # stays ● until the last subagent stops
        return
    _status(_target_for(session_id=session_id, platform=platform), state)


def _on_human_input_request(kind: str = "", session_id: str = "", session_key: str = "",
                            platform: str = "", prompt: str = "", **_: Any) -> None:
    target = _target_for(session_id=session_id, session_key=session_key, platform=platform)
    if target is None:
        return
    _status(target, "waiting")
    mode = setting("alerts")
    if mode != "off":
        _worker.submit(lambda: alert(target, mode, kind, prompt))


def _on_human_input_resolved(session_id: str = "", session_key: str = "", platform: str = "",
                             **_: Any) -> None:
    _status(_target_for(session_id=session_id, session_key=session_key, platform=platform), "busy")


def _post_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> None:
    """The model renaming its window drops the status mark; put it back."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return
    command = str(args.get("command") or "")
    if "rex" in command and "rename" in command and session_id not in _registry.child_sessions:
        _status(_target_for(session_id=session_id), "busy")


def _session_title(ref: str) -> str:
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        sid = db.resolve_session_id(ref) or db.resolve_session_by_title(ref)
        return (db.get_session_title(sid) or "") if sid else ""
    except Exception:
        logger.debug("rex plugin: session title lookup failed", exc_info=True)
        return ""


def _pre_command(command: str = "", args_raw: str = "", session_key: str = "", platform: str = "",
                 **_: Any) -> None:
    if command not in ("title", "resume") or not (args_raw or "").strip():
        return
    target = _target_for(session_key=session_key, platform=platform)
    if target is None:
        return
    ref = args_raw.strip()
    if command == "title":
        _worker.submit(lambda: _titles.set_base(target, slug(ref, max_words=6)))
    else:
        _worker.submit(lambda: _titles.set_base(target, slug(_session_title(ref), max_words=6)))


def _on_session_finalize(session_id: str = "", platform: str = "", **_: Any) -> None:
    target = _target_for(session_id=session_id, platform=platform)
    if target is None:
        return
    _registry.forget(session_id)
    with _children_lock:
        _children.pop(session_id, None)
        _pending_end.pop(session_id, None)
    _worker.submit(lambda: _titles.restore(target))
    # The Rex session label is shared: put it back only when the last Hermes chat in it ends.
    if setting("session_naming") and not _registry.has_rex_session(target.session):
        _worker.submit(lambda: _titles.restore_session_label(target))


def _subagent_start(parent_session_id: str = "", child_session_id: str = "", child_goal: str = "",
                    **_: Any) -> None:
    if child_session_id:
        _registry.child_sessions.add(child_session_id)
        if parent_session_id:
            _child_started(parent_session_id, child_session_id)
    if not setting("subagent_panes"):
        return
    target = current_target() or _registry.lookup(session_id=parent_session_id or "")
    if target is None:
        return
    root = live_root()  # profile-scoped: resolve here, on the hook's thread
    linger = setting("pane_linger_seconds")
    _worker.submit(lambda: _subagents.open(target, parent_session_id or "", child_goal, root, linger))


def _subagent_stop(parent_session_id: str = "", child_session_id: str = "", **_: Any) -> None:
    if not parent_session_id or not child_session_id:
        return
    state = _child_stopped(parent_session_id, child_session_id)
    if state:
        _status(current_target() or _registry.lookup(session_id=parent_session_id), state)


def _on_kanban_worker_spawned(task_id: str = "", board: Optional[str] = None, assignee: str = "",
                              worker_pid: int = 0, **_: Any) -> None:
    if not setting("kanban_panes") or not task_id:
        return
    target = _last_target()
    if target is None:
        return
    linger = setting("pane_linger_seconds")
    _worker.submit(lambda: _kanban.open(target, task_id=task_id, board=board, assignee=assignee or "",
                                        worker_pid=int(worker_pid or 0), linger=linger))


HOOKS = {
    "on_session_start": _on_session_start,
    "pre_llm_call": _pre_llm_call,
    "on_session_end": _on_session_end,
    "on_human_input_request": _on_human_input_request,
    "on_human_input_resolved": _on_human_input_resolved,
    "post_tool_call": _post_tool_call,
    "pre_command": _pre_command,
    "on_session_finalize": _on_session_finalize,
    "subagent_start": _subagent_start,
    "subagent_stop": _subagent_stop,
    "on_kanban_worker_spawned": _on_kanban_worker_spawned,
}


def _warn_if_passthrough_missing() -> None:
    """Without the client-env allowlist the gateway never sees REX_* and the plugin is inert."""
    try:
        from hermes_cli.config import load_config_readonly
        terminal = (load_config_readonly() or {}).get("terminal") or {}
        listed = set(terminal.get("client_env_passthrough") or [])
    except Exception:
        return
    missing = [v for v in REX_VARS if v not in listed]
    if missing:
        logger.warning(
            "rex plugin: terminal.client_env_passthrough is missing %s — Hermes sessions won't see "
            "which Rex pane they run in. Run: hermes config set terminal.client_env_passthrough "
            "'[\"REX_SESSION\",\"REX_BLOCK\",\"REX_SERVER\",\"REX_BIN_DIR\"]'", ", ".join(missing))


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    _warn_if_passthrough_missing()
    ctx.register_system_prompt_section("rex.session", _prompt_section)
    for name, callback in HOOKS.items():
        ctx.register_hook(name, callback)
    if _SKILL_PATH.exists():
        ctx.register_skill("rex", _SKILL_PATH, description=(
            "Use the rex CLI to manage Rex sessions, open tabs, arrange panes, run commands, "
            "inspect terminal output, and control connected Rex apps."))
