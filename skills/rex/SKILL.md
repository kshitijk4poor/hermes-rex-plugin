---
name: rex
description: Use the rex CLI to manage Rex sessions, open tabs, arrange panes, run commands, inspect terminal output, and control connected Rex apps.
---

Run `rex __internal skill` and follow the guide it prints. The guide comes from
the installed Rex CLI, so it describes the commands available in that version.

If the command fails, the installed Rex version predates the guide. Start with
`rex whoami --json`, then use `rex --help` and `rex COMMAND --help`.

Hermes notes:
- `REX_SESSION` / `REX_BLOCK` are inherited by your terminal tool when Hermes was
  launched inside a Rex pane. If they are unset, Hermes is not running in Rex;
  use `rex whoami --json` and `rex ls --json` to find a target.
- Run `rex` through the terminal tool. Don't kill Rex processes or restart the
  Rex server unless the user asks.
