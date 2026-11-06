"""Program Status reports (OSC 7501) for the Rex pane a Hermes session runs in.

Rex shows each pane's reported status in its tab bar and session picker, and sends its own Done
notification (https://www.superlogical.com/rex/docs/build/program-status). The plugin runs in the
gateway, which has no terminal, so a report is written straight to the pane's tty: Rex names the
pane's process (``rex block call process``) and ``ps`` gives that process's tty.

The root record is the Hermes session itself. Each running subagent gets a child record
(``sub/<id>``), so a blocked or busy child shows under a parent that is still working.

Protocol rules this module keeps: every report repeats ``app`` (a report replaces its record
whole), ``msg``/``title`` are base64 of a single line with no control characters, and nothing over
the size caps is sent. ``working``/``blocked`` records drop when the pane's process exits;
``done``/``error`` stay until the user looks.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import threading
from typing import Optional

from . import rexcli
from .rexcli import Target

logger = logging.getLogger(__name__)

APP = "hermes"
_MSG_CHARS = 160      # the protocol allows 2048 decoded bytes; Rex shows one short line
_TITLE_CHARS = 64     # the protocol allows 192
_MAX_REPORT = 4096
_ID_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.+-]")
STATES = frozenset({"idle", "working", "done", "blocked", "error", "clear"})
# Hermes human-input kinds -> the protocol's blocked kinds.
BLOCKED_KIND = {"approval": "permission", "clarify": "question", "sudo": "auth"}


def one_line(text: object, limit: int = _MSG_CHARS) -> str:
    """The first non-empty line of *text* with control characters removed, cut to *limit*."""
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    line = "".join(ch for ch in line if ch.isprintable())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def record_id(*parts: str) -> str:
    """A valid record id from *parts* (each segment 1-32 of ``[A-Za-z0-9_.+-]``; the tail is kept,
    since session ids differ at the end)."""
    segments = [_ID_SEGMENT_RE.sub("-", p)[-32:] for p in parts if p]
    return "/".join(s for s in segments if s)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def encode(state: str, *, kind: str = "", msg: object = "", rid: str = "", title: object = "",
           app: str = APP) -> bytes:
    """One OSC 7501 report, or b"" when the state is unknown or the report is over the cap."""
    if state not in STATES:
        return b""
    pairs = [f"state={state}"]
    if state != "clear":
        pairs.append(f"app={app}")
    if rid:
        pairs.append(f"id={rid}")
    if state == "blocked" and kind:
        pairs.append(f"kind={kind}")
    title_text = one_line(title, _TITLE_CHARS) if state != "clear" else ""
    if title_text:
        pairs.append(f"title={_b64(title_text)}")
    msg_text = one_line(msg) if state != "clear" else ""
    if msg_text:
        pairs.append(f"msg={_b64(msg_text)}")
    data = ("\x1b]7501;" + ":".join(pairs) + "\x1b\\").encode("ascii")
    return data if len(data) <= _MAX_REPORT else b""


def _tty_for_pid(pid: int) -> Optional[str]:
    try:
        name = subprocess.run(["ps", "-o", "tty=", "-p", str(int(pid))], capture_output=True,
                              text=True, timeout=2, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not name or name.startswith("?"):
        return None
    path = "/dev/" + name
    return path if os.path.exists(path) else None


class Reporter:
    """Writes reports to the tty of each target's pane. A pane's pty never changes, so the tty is
    looked up once per block; a failed lookup is retried on the next report."""

    def __init__(self) -> None:
        self._tty: dict[str, str] = {}
        self._lock = threading.Lock()

    def tty(self, target: Target) -> Optional[str]:
        block = target.block
        if not block:
            return None
        with self._lock:
            known = self._tty.get(block)
        if known:
            return known
        info = rexcli.run_json(target, "block", "call", "-b", block, "process") or {}
        found = None
        for key in ("foreground", "child"):  # every process in the pane shares its tty
            pid = (info.get(key) or {}).get("pid")
            found = _tty_for_pid(pid) if pid else None
            if found:
                break
        if found:
            with self._lock:
                self._tty[block] = found
        return found

    def send(self, target: Target, state: str, **fields: object) -> bool:
        data = encode(state, **fields)  # type: ignore[arg-type]
        path = self.tty(target) if data else None
        if not path:
            return False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_APPEND | os.O_NONBLOCK)
        except OSError as exc:
            logger.debug("rex plugin: cannot open %s: %s", path, exc)
            with self._lock:
                self._tty.pop(target.block, None)
            return False
        try:
            os.write(fd, data)
            return True
        except OSError as exc:
            logger.debug("rex plugin: status write to %s failed: %s", path, exc)
            return False
        finally:
            os.close(fd)
