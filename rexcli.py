"""Locating the Rex pane a Hermes session lives in, and calling the ``rex`` CLI for it.

Hermes runs every session in one gateway process, so the shell's ``REX_*`` vars are not in
``os.environ``. Inside a turn they are the session's bound client env; hooks that fire outside a
turn (slash commands, session teardown, the kanban dispatcher) look the target up in
:class:`Registry`, which turn hooks keep current.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

REX_VARS = ("REX_SESSION", "REX_BLOCK", "REX_SERVER", "REX_BIN_DIR")
# Local interactive surfaces only: "local" is a CLI/TUI session served by the gateway.
LOCAL_PLATFORMS = frozenset({"", "cli", "tui", "local"})
TIMEOUT_S = 3


def _bound_env() -> Optional[Mapping[str, str]]:
    try:
        from agent.session_client_env import bound_client_env
        return bound_client_env()
    except Exception:
        return None


def rex_env() -> dict[str, str]:
    """REX_* of the session executing here: its shell client's env when the gateway bound one,
    else this process's own (in-process CLI / older Hermes)."""
    bound = _bound_env()
    src: Mapping[str, str] = bound if bound is not None else os.environ
    return {k: src[k] for k in REX_VARS if src.get(k)}


def _rex_bin(env: Mapping[str, str], path: Optional[str]) -> Optional[str]:
    found = shutil.which("rex", path=path) or shutil.which("rex")
    if found:
        return found
    bin_dir = env.get("REX_BIN_DIR")
    cand = Path(bin_dir) / "rex" if bin_dir else None
    return str(cand) if cand and cand.is_file() and os.access(cand, os.X_OK) else None


@dataclass(frozen=True)
class Target:
    """One Rex pane (block) plus what is needed to run ``rex`` against its server."""

    env: dict[str, str]
    rex: str
    path: Optional[str] = None

    @property
    def session(self) -> str:
        return self.env.get("REX_SESSION", "")

    @property
    def block(self) -> str:
        return self.env.get("REX_BLOCK", "")

    def to_json(self) -> dict[str, Any]:
        return {"env": dict(self.env), "rex": self.rex, "path": self.path}

    @classmethod
    def from_json(cls, data: Any) -> Optional["Target"]:
        if not isinstance(data, dict) or not isinstance(data.get("env"), dict) or not data.get("rex"):
            return None
        env = {k: str(v) for k, v in data["env"].items() if k in REX_VARS and v}
        return cls(env, str(data["rex"]), data.get("path")) if env.get("REX_SESSION") else None


def current_target() -> Optional[Target]:
    """The Rex pane of the session executing here, or None outside Rex."""
    env = rex_env()
    if not env.get("REX_SESSION"):
        return None
    bound = _bound_env()
    path = (bound or {}).get("PATH") or None
    rex = _rex_bin(env, path)
    return Target(env, rex, path) if rex else None


def child_env(target: Target) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("REX_")}
    if target.path:
        base["PATH"] = target.path
    return base | dict(target.env)


def run(target: Target, *args: str, timeout: float = TIMEOUT_S) -> tuple[int, str]:
    """Run ``rex ARGS`` for *target*; ``(returncode, stdout)``, ``(-1, '')`` on failure."""
    try:
        proc = subprocess.run(
            [target.rex, "--autostart=false", *args], capture_output=True, text=True,
            timeout=timeout, check=False, stdin=subprocess.DEVNULL, env=child_env(target))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("rex %s failed: %s", args[:2], exc)
        return -1, ""
    if proc.returncode != 0:
        logger.debug("rex %s exited %s: %s", args[:2], proc.returncode, proc.stderr.strip()[:200])
    return proc.returncode, proc.stdout


def run_json(target: Target, *args: str) -> Optional[dict[str, Any]]:
    rc, out = run(target, *args)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Worker:
    """One daemon thread running queued jobs in order, so hooks never wait on ``rex`` and a
    title change can never overtake the one before it."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=256)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def submit(self, job: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="rex-plugin", daemon=True)
                self._thread.start()
        try:
            self._q.put_nowait(job)
        except queue.Full:
            logger.debug("rex plugin queue full; dropping job")

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            try:
                job()
            except Exception:
                logger.debug("rex plugin job failed", exc_info=True)

    def drain(self, timeout: float = 10.0) -> None:
        """Test helper: wait until queued jobs have run."""
        done = threading.Event()
        self.submit(done.set)
        done.wait(timeout)


@dataclass
class Registry:
    """Session id / session key -> Target, so hooks outside a turn can find the pane."""

    by_session_id: dict[str, Target] = field(default_factory=dict)
    by_session_key: dict[str, Target] = field(default_factory=dict)
    child_sessions: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def remember(self, target: Target, *, session_id: str = "", session_key: str = "") -> None:
        with self._lock:
            if session_id:
                self.by_session_id[session_id] = target
            if session_key:
                self.by_session_key[session_key] = target

    def lookup(self, *, session_id: str = "", session_key: str = "") -> Optional[Target]:
        with self._lock:
            return (self.by_session_id.get(session_id or "")
                    or self.by_session_key.get(session_key or ""))

    def forget(self, session_id: str) -> None:
        with self._lock:
            self.by_session_id.pop(session_id, None)

    def has_rex_session(self, rex_session: str) -> bool:
        """True while any remembered Hermes session still lives in Rex session *rex_session*."""
        with self._lock:
            return any(t.session == rex_session for t in self.by_session_id.values())


def current_session_key() -> str:
    try:
        from tools import approval_context
        return approval_context.get_current_session_key(default="") or ""
    except Exception:
        return ""
