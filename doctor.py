"""``hermes rex doctor [--fix]``: check that Hermes sessions can see and report to their Rex pane.

Run it from a Rex pane. ``--fix`` adds the Rex ids to ``terminal.client_env_passthrough`` through
Hermes's own config writer (existing entries are kept).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Callable

from .rexcli import REX_VARS


def setup(parser) -> None:
    sub = parser.add_subparsers(dest="rex_command")
    check = sub.add_parser("doctor", help="Check the Hermes <-> Rex wiring from a Rex pane")
    check.add_argument("--fix", action="store_true",
                       help="add the Rex ids to terminal.client_env_passthrough in config.yaml")
    parser.set_defaults(rex_command="doctor", fix=False)


def _passthrough() -> list[str]:
    try:
        from hermes_cli.config import load_config_readonly
        terminal = (load_config_readonly() or {}).get("terminal") or {}
        listed = terminal.get("client_env_passthrough") or []
    except Exception:
        return []
    return [str(v) for v in listed] if isinstance(listed, list) else []


def _fix_passthrough(listed: list[str]) -> str:
    """Write the merged allowlist with ``hermes config set``'s writer; '' on success, else why not."""
    merged = listed + [v for v in REX_VARS if v not in listed]
    try:
        from hermes_cli.config import set_config_value
        set_config_value("terminal.client_env_passthrough", json.dumps(merged))
    except SystemExit as exc:  # the writer exits on a refused value (managed install, bad key)
        return f"config writer refused the change (exit {exc.code})"
    except Exception as exc:
        return f"could not update config.yaml: {exc}"
    return ""


def _rex_whoami(rex: str) -> dict[str, Any]:
    try:
        out = subprocess.run([rex, "--autostart=false", "whoami", "--json"], capture_output=True,
                             text=True, timeout=5, check=False, stdin=subprocess.DEVNULL).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def checks(fix: bool = False, *, env: dict[str, str] | None = None,
           whoami: Callable[[str], dict[str, Any]] = _rex_whoami) -> list[tuple[bool, str, str]]:
    """``(ok, what, how to fix)`` rows. Pure apart from the rex call and an opt-in config write."""
    env = dict(os.environ) if env is None else env
    rows: list[tuple[bool, str, str]] = []
    in_rex = bool(env.get("REX_SESSION") and env.get("REX_BLOCK"))
    rows.append((in_rex, "running inside a Rex pane (REX_SESSION, REX_BLOCK set)",
                 "run `hermes rex doctor` from a Rex terminal"))
    rex = shutil.which("rex", path=env.get("PATH"))
    if not rex and env.get("REX_BIN_DIR"):
        cand = os.path.join(env["REX_BIN_DIR"], "rex")
        rex = cand if os.access(cand, os.X_OK) else None
    rows.append((bool(rex), f"rex CLI found ({rex})" if rex else "rex CLI found",
                 "open a Rex terminal, or put Rex's bin directory on PATH"))
    if rex:
        who = whoami(rex)
        rows.append((bool(who.get("rex_version")),
                     f"Rex server answers (rex {who.get('rex_version')})" if who else "Rex server answers",
                     "is Rex running? `rex whoami` should print this pane's ids"))
        if in_rex and who.get("block_id"):
            same = who.get("block_id") == env.get("REX_BLOCK")
            rows.append((same, "REX_BLOCK matches the pane Rex reports",
                         "REX_BLOCK is stale (inherited from another pane?); open a new pane"))
    listed = _passthrough()
    missing = [v for v in REX_VARS if v not in listed]
    note = ""
    if missing and fix:
        note = _fix_passthrough(listed)
        listed = _passthrough()
        missing = [v for v in REX_VARS if v not in listed]
    rows.append((not missing, "terminal.client_env_passthrough forwards " + ", ".join(REX_VARS),
                 note or ("missing " + ", ".join(missing) + "; run `hermes rex doctor --fix`")))
    return rows


def run(args=None) -> int:
    fix = bool(getattr(args, "fix", False))
    before = _passthrough()
    rows = checks(fix)
    print("Rex integration")
    for ok, what, how in rows:
        print(f"  {'ok ' if ok else 'FAIL'} {what}")
        if not ok:
            print(f"       -> {how}")
    if fix and _passthrough() != before:
        print("\nUpdated config.yaml. Start a new Hermes session for it to take effect.")
    failed = [r for r in rows if not r[0]]
    print("\nAll good." if not failed else f"\n{len(failed)} check(s) failed.")
    return 1 if failed else 0
