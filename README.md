# hermes-rex-plugin

Rex integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

`rex skill setup` wires Rex into Claude Code and Codex with a skill and a
SessionStart hook. Hermes isn't one of its targets, so this plugin does the
same job the Hermes way, then adds what only a plugin inside Hermes can do.

For Hermes sessions started (CLI or TUI) inside a Rex pane:

- **Session context.** Each new session's system prompt gets Rex's own
  SessionStart text (`rex __internal session-start`): your `REX_SESSION` /
  `REX_BLOCK`, and the instruction to name the Rex session, and the window,
  after the task. If the Rex session already has other windows, Hermes is told
  to name only its own window, because the session label is shared. It is
  rendered once per session and frozen, so prompt caching is unaffected.
- **Skill.** `rex:rex`, a stub that defers to `rex __internal skill`, so the
  guide always matches your installed Rex.
- **Program status.** Hermes reports its state to Rex with the
  [Program Status Protocol](https://www.superlogical.com/rex/docs/build/program-status)
  (OSC 7501): working (with the running tool), blocked (approval, question or
  sudo, with the reason), done (the reply's first line) and error. Rex shows
  it as a spinner on unfocused tabs and a mark in the session picker, and
  sends its own Done notification. Each running subagent gets its own record,
  titled with its goal, under the session's: its current tool, or what it is
  waiting for. A turn whose background subagents are still running reports
  "waiting for N subagents" instead of done.
- **Status in the window title.** `● fix-login` while Hermes works,
  `? fix-login` while it waits for you, then `✓` done, `■` stopped or
  `✗` failed. Only the mark belongs to the plugin; the label stays whatever
  you or the model set. A turn that leaves background subagents running stays
  `●` until the last of them stops.
- **Alerts.** A macOS notification when Hermes waits for a command approval,
  a clarify answer or a sudo password and its pane isn't in view (Rex isn't in
  front, or another pane is focused).
- **Phone push (Pushover).** The same "waiting for you" events, pushed to your
  phone while the pane is out of view. Set `push: done` to get a push only when
  a turn finishes or fails (no approval/question pushes), or `push: all` for
  both. Put `PUSHOVER_USER_KEY` and `PUSHOVER_APP_TOKEN`
  in `~/.hermes/.env`. Without both keys nothing is sent.
- **Label sync.** `/title <text>` and `/resume <session>` relabel the window.
  When the session ends, the window gets back the label it had before, and when
  the last Hermes chat in a Rex session ends, so does the session.
- **Visible commands.** The agent is told to run dev servers, watchers, log
  tails and long builds in a Rex pane beside its own (`rex run`) instead of a
  hidden background process, and to ask before touching panes it didn't open.
- **Subagent panes.** Each `delegate_task` child gets a pane in a
  `subagents` window that follows its live transcript.
- **Kanban panes.** Each kanban worker gets a pane in a `kanban` window that
  follows the worker's log.

- **Chat restore.** With [rex-agent-restore](https://github.com/pattont/RexAgentRestore)
  installed, a Hermes chat cut off by a Rex server restart comes back: on the
  first prompt of the restored pane it runs `hermes --resume <session>` in the
  same folder. The plugin saves each pane's session where rex-agent-restore
  looks, since its own hooks can't see Hermes (sessions run in the gateway,
  not in the pane). A chat you quit normally is forgotten. Panes without
  rex-agent-restore's shell integration get nothing. Add flags with
  `REX_AGENT_RESTORE_HERMES_ARGS` (e.g. `--tui`).

New panes never take focus. A finished pane closes after
`pane_linger_seconds`; press Enter in it to keep it.

Gateway chats (Telegram, Slack, …), cron and Desktop are never affected.
Everything fails open: no `rex`, not in Rex, or a slow or broken `rex` means
nothing happens, never an error.

## Install

```sh
hermes plugins install kshitijk4poor/hermes-rex-plugin --enable
hermes rex doctor --fix
```

The second line is required: it adds `REX_SESSION`, `REX_BLOCK`, `REX_SERVER`
and `REX_BIN_DIR` to `terminal.client_env_passthrough`, keeping any entries
already there. Hermes runs every session in one gateway process,
not in your shell, so the shell's `REX_*` variables only reach a session (and
its terminal-tool commands) when they're on the client-env allowlist. The plugin
logs a warning at load if they're missing.

Sessions that already exist keep the system prompt they started with; start a
new one to get the session context.

## Settings

All optional, under `plugins.entries.rex.settings` in `config.yaml`:

| Setting | Default | |
|---|---|---|
| `session_naming` | `true` | Rex's "name the session after the task", plus the same label on the window |
| `program_status` | `true` | OSC 7501 status reports to the pane |
| `status_titles` | `true` | ● ? ✓ ■ ✗ mark on the window label |
| `alerts` | `unfocused` | `unfocused`, `always` or `off` |
| `push` | `waiting` | Pushover phone push: `waiting` (approvals, questions, sudo), `done` (only finished/failed turns), `all` (both) or `off` |
| `visible_commands` | `true` | Tell the agent to run long-lived commands in Rex panes |
| `subagent_panes` | `true` | A pane per subagent |
| `kanban_panes` | `true` | A pane per kanban worker |
| `restore_chats` | `true` | Save each pane's session for rex-agent-restore |
| `pane_linger_seconds` | `300` | How long a finished pane stays open |
| `debug` | `false` | Log every hook call (ids only, no message text) to `trace.jsonl` in the plugin's data dir |

For example: `hermes config set plugins.entries.rex.settings.alerts always`.

## Check it

From a Rex pane:

```sh
hermes rex doctor
```

It checks that you're in a Rex pane, that `rex` and the Rex server answer, and
that the passthrough setting is in place. Then:

```sh
hermes chat -q 'Without tools: quote REX_SESSION from your context. Then run: echo $REX_SESSION'
```

Both values should match. Outside Rex the model should report no Rex context.

## Tests

```sh
uv run --no-project --with pytest python -m pytest -c tests/pytest.ini tests
```

The tests drive the hooks against a fake `rex`; no Hermes install is needed.

## What it runs

- Status reports are written straight to the pane's tty, found once per pane
  with `rex block call process` and `ps -o tty=`. Nothing goes to stdout.
- The `rex` CLI, with `--autostart=false` so it never starts a Rex server,
  and a 3-second timeout per call. It talks only to your local Rex server.
- `lsappinfo` and `osascript` (macOS only) for "is Rex in front?" and the
  alert notification.
- `follow.py` (stdlib only), inside each subagent or kanban pane, which reads
  the Hermes transcript or worker log it follows. Panes close after
  `pane_linger_seconds`.

- With both Pushover keys set, one HTTPS POST to `api.pushover.net` per push
  (5-second timeout). Without them, nothing.

No other network calls, no telemetry. It writes only its own state
(`last_target`, plus `trace.jsonl` when `debug` is on) in its plugin data dir.
`hermes rex doctor --fix` writes `terminal.client_env_passthrough` through
Hermes's config writer. Requires Hermes 0.22.3 or newer (`terminal.client_env_passthrough`).

## Notes

- The session ID is captured when a session starts. A resumed session keeps the
  pane it was created in.
- `--agent opencode` is used for Rex's agent-neutral wording; the plugin appends
  the Hermes skill name (`rex:rex`).
- Kanban workers are started by the gateway's dispatcher, which has no pane of
  its own, so their panes go to the Rex session of the most recent Hermes turn
  (within 12 hours).
- `rex skill status` won't list Hermes, and `rex skill setup` / `remove`
  don't touch it: install, update and remove the plugin with
  `hermes plugins`. Rex's session-naming choice from `rex skill setup` doesn't
  apply either; Hermes uses the `session_naming` setting above.
- The `rex:rex` skill reads Rex's guide with naming off, so it never
  contradicts the session context (which can tell Hermes to name only its
  window, or nothing).
- Rex prints "the Rex setup for this agent is out of date" when its naming
  flag differs from the choice `rex skill setup` recorded for other agents.
  That notice can't apply to Hermes, so the plugin drops it.

## License

MIT
