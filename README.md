# hermes-rex-plugin

Rex integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

`rex skill setup` wires Rex into Claude Code and Codex with a skill and a
SessionStart hook. Hermes isn't one of its targets, so this plugin does the
same job the Hermes way, then adds what only a plugin inside Hermes can do.

For Hermes sessions started (CLI or TUI) inside a Rex pane:

- **Session context.** Each new session's system prompt gets Rex's own
  SessionStart text (`rex __internal session-start`): your `REX_SESSION` /
  `REX_BLOCK`, and the instruction to name the Rex session, and the window,
  after the task. It is rendered once per session and frozen, so prompt
  caching is unaffected.
- **Skill.** `rex:rex`, a stub that defers to `rex __internal skill`, so the
  guide always matches your installed Rex.
- **Status in the window title.** `● fix-login` while Hermes works,
  `? fix-login` while it waits for you, then `✓` done, `■` stopped or
  `✗` failed. Only the mark belongs to the plugin; the label stays whatever
  you or the model set.
- **Alerts.** A macOS notification when Hermes waits for a command approval,
  a clarify answer or a sudo password and its pane isn't in view (Rex isn't in
  front, or another pane is focused).
- **Label sync.** `/title <text>` and `/resume <session>` relabel the window.
  When the session ends, the window gets back the label it had before.
- **Visible commands.** The agent is told to run dev servers, watchers, log
  tails and long builds in a Rex pane beside its own (`rex run`) instead of a
  hidden background process, and to ask before touching panes it didn't open.
- **Subagent panes.** Each `delegate_task` child gets a pane in a
  `subagents` window that follows its live transcript.
- **Kanban panes.** Each kanban worker gets a pane in a `kanban` window that
  follows the worker's log.

New panes never take focus. A finished pane closes after
`pane_linger_seconds`; press Enter in it to keep it.

Gateway chats (Telegram, Slack, …), cron and Desktop are never affected.
Everything fails open: no `rex`, not in Rex, or a slow or broken `rex` means
nothing happens, never an error.

## Install

```sh
hermes plugins install kshitijk4poor/hermes-rex-plugin --enable
hermes config set terminal.client_env_passthrough '["REX_SESSION","REX_BLOCK","REX_SERVER","REX_BIN_DIR"]'
```

The second line is required. Hermes runs every session in one gateway process,
not in your shell, so the shell's `REX_*` variables only reach a session (and
its terminal-tool commands) when they're on the client-env allowlist. The plugin
logs a warning at load if they're missing. If you already have entries in
`client_env_passthrough`, add these four to your list instead of replacing it.

Sessions that already exist keep the system prompt they started with; start a
new one to get the session context.

## Settings

All optional, under `plugins.entries.rex.settings` in `config.yaml`:

| Setting | Default | |
|---|---|---|
| `session_naming` | `true` | Rex's "name the session after the task", plus the same label on the window |
| `status_titles` | `true` | ● ? ✓ ■ ✗ mark on the window label |
| `alerts` | `unfocused` | `unfocused`, `always` or `off` |
| `visible_commands` | `true` | Tell the agent to run long-lived commands in Rex panes |
| `subagent_panes` | `true` | A pane per subagent |
| `kanban_panes` | `true` | A pane per kanban worker |
| `pane_linger_seconds` | `300` | How long a finished pane stays open |

For example: `hermes config set plugins.entries.rex.settings.alerts always`.

## Check it

From a Rex pane:

```sh
hermes chat -q 'Without tools: quote REX_SESSION from your context. Then run: echo $REX_SESSION'
```

Both values should match. Outside Rex the model should report no Rex context.

## Tests

```sh
uv run --no-project --with pytest python -m pytest -c tests/pytest.ini tests
```

The tests drive the hooks against a fake `rex`; no Hermes install is needed.

## Notes

- The session ID is captured when a session starts. A resumed session keeps the
  pane it was created in.
- `--agent opencode` is used for Rex's agent-neutral wording; the plugin appends
  the Hermes skill name (`rex:rex`).
- Kanban workers are started by the gateway's dispatcher, which has no pane of
  its own, so their panes go to the Rex session of the most recent Hermes turn
  (within 12 hours).
- `rex skill status` won't list Hermes. Native support belongs in
  `rex skill setup`.

## License

MIT
