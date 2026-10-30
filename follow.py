"""Follow a subagent transcript or a kanban worker log inside a Rex pane.

Run by the rex plugin as ``rex run ... -- python follow.py ...``; stdlib only. Streams the file
as it grows, stops when the run is over (manifest status for subagents, worker pid for kanban),
then keeps the pane for ``--linger`` seconds unless you press Enter to keep it.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import sys
import time


def _manifest_done(path: str, index: int) -> str:
    """'' while the task runs, else its final status."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    for task in data.get("tasks") or []:
        if task.get("index") == index and task.get("status") not in (None, "running"):
            return str(task.get("status"))
    return "finished" if data.get("completed") else ""


def _pid_done(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "exited"
    except PermissionError:
        return ""
    return ""


def _wait_for(path: str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.5)
    return os.path.exists(path)


def follow(args: argparse.Namespace) -> int:
    if args.title:
        print(f"\033[1m{args.title}\033[0m", flush=True)
    if not _wait_for(args.log, 60):
        print(f"(no log at {args.log})", flush=True)
        return 1

    def finished() -> str:
        if args.manifest:
            return _manifest_done(args.manifest, args.index)
        if args.pid:
            return _pid_done(args.pid)
        return ""

    status = ""
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        while True:
            chunk = fh.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                continue
            if status:
                break
            status = finished()  # one more read after it ends, to flush the tail
            if not status:
                time.sleep(0.5)

    print(f"\n\033[1m── {status} ──\033[0m", flush=True)
    if args.linger <= 0:
        return 0
    print(f"This pane closes in {args.linger}s. Press Enter to keep it open.", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], args.linger)
        if ready:
            sys.stdin.readline()
            print("Kept open. Press Enter again to close.", flush=True)
            sys.stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--log", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--manifest", default="")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--pid", type=int, default=0)
    p.add_argument("--linger", type=int, default=300)
    try:
        return follow(p.parse_args(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
