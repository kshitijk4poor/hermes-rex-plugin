"""Window titles that show what each Hermes session is doing, plus a desktop alert when it waits.

The status is a one-glyph prefix on the window label the model (or the user) chose:
``● fix-login`` busy, ``? fix-login`` waiting for you, ``✓ fix-login`` done. Only the prefix is
ours; the rest of the label is read back from Rex each time, so a rename by anyone is kept.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Optional

from . import rexcli
from .rexcli import Target

logger = logging.getLogger(__name__)

GLYPHS = {"busy": "●", "waiting": "?", "done": "✓", "stopped": "■", "failed": "✗"}
_PREFIX_RE = re.compile(r"^[●?✓■✗] ")


def strip_status(label: str) -> str:
    return _PREFIX_RE.sub("", label or "", count=1)


def slug(text: str, *, max_words: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words[:max_words])


class Titles:
    def __init__(self) -> None:
        # window id -> the label it had before we first touched it, restored when the session ends.
        self._original: dict[str, str] = {}
        # rex session id -> its label before any Hermes session in it ran, restored when the last one ends.
        self._session_original: dict[str, str] = {}
        self._lock = threading.Lock()

    def _window(self, target: Target) -> Optional[dict]:
        """The window holding the target's block (``rex window inspect`` resolves it from REX_BLOCK)."""
        return rexcli.run_json(target, "window", "inspect", "--json")

    def _rename(self, target: Target, window_id: str, label: str) -> None:
        rexcli.run(target, "window", "rename", "-s", target.session, window_id, label)

    def set_status(self, target: Target, state: Optional[str]) -> None:
        """Prefix the window label with *state*'s glyph (None clears it)."""
        win = self._window(target)
        if not win or not win.get("window_id"):
            return
        current = str(win.get("label") or "")
        base = strip_status(current)
        with self._lock:
            self._original.setdefault(win["window_id"], base)
        glyph = GLYPHS.get(state or "")
        new = f"{glyph} {base}" if glyph else base
        if new != current:
            self._rename(target, win["window_id"], new)

    def set_base(self, target: Target, base: str) -> None:
        """Replace the label but keep the current status glyph."""
        base = base.strip()
        win = self._window(target)
        if not base or not win or not win.get("window_id"):
            return
        current = str(win.get("label") or "")
        with self._lock:
            self._original.setdefault(win["window_id"], strip_status(current))
        m = _PREFIX_RE.match(current)
        new = (m.group(0) if m else "") + base
        if new != current:
            self._rename(target, win["window_id"], new)

    def restore(self, target: Target) -> None:
        """Put back the label the window had before this session touched it."""
        win = self._window(target)
        if not win or not win.get("window_id"):
            return
        with self._lock:
            original = self._original.pop(win["window_id"], None)
        current = str(win.get("label") or "")
        new = original if original is not None else strip_status(current)
        if new and new != current:
            self._rename(target, win["window_id"], new)

    # --- the Rex session label (shared by every window in the session) ---

    def session_label(self, target: Target) -> Optional[str]:
        data = rexcli.run_json(target, "ls", "--json") or {}
        for sess in data.get("sessions") or []:
            if sess.get("session_id") == target.session:
                return str(sess.get("label") or "")
        return None

    def note_session_label(self, target: Target) -> None:
        """Remember the Rex session's label the first time a Hermes session in it is seen."""
        with self._lock:
            if target.session in self._session_original:
                return
        label = self.session_label(target)
        if label is not None:
            with self._lock:
                self._session_original.setdefault(target.session, label)

    def restore_session_label(self, target: Target) -> None:
        """Put back the Rex session label noted before any Hermes session in it renamed it."""
        with self._lock:
            original = self._session_original.pop(target.session, None)
        if not original:
            return
        if self.session_label(target) not in (None, original):
            rexcli.run(target, "session", "rename", target.session, original)


# --- alerts -------------------------------------------------------------------------------------

def _front_app() -> str:
    """Name of the frontmost macOS app ('' when unknown). lsappinfo needs no TCC permission."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=2).stdout.strip()
        if not asn:
            return ""
        out = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r'="([^"]*)"', out)
    return m.group(1) if m else ""


def pane_in_view(target: Target) -> bool:
    """True when Rex is the frontmost app and the target's pane is the focused one."""
    if "rex" not in _front_app().lower():
        return False
    windows = rexcli.run_json(target, "window", "list", "-s", target.session, "--json") or {}
    active = windows.get("active_window_id")
    for win in windows.get("windows") or []:
        if win.get("window_id") == active:
            return win.get("focused_block_id") == target.block
    return False


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, subtitle: str, body: str) -> None:
    script = (f"display notification {_applescript_str(body[:200])} with title {_applescript_str(title)}"
              f" subtitle {_applescript_str(subtitle)} sound name \"Glass\"")
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        logger.debug("rex plugin: notification failed", exc_info=True)


_KIND_TEXT = {"approval": "wants approval to run a command", "clarify": "has a question for you",
              "sudo": "needs your sudo password"}


def alert(target: Target, mode: str, kind: str, prompt: str) -> None:
    if mode == "off" or (mode != "always" and pane_in_view(target)):
        return
    win = rexcli.run_json(target, "window", "inspect", "--json") or {}
    label = strip_status(str(win.get("label") or "")) or "Hermes"
    first = next((line.strip() for line in (prompt or "").splitlines() if line.strip()), "")
    notify(f"Hermes {_KIND_TEXT.get(kind, 'needs you')}", label, first or "Switch to its Rex pane.")
