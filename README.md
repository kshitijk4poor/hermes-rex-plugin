# hermes-rex-plugin

Rex integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

`rex skill setup` wires Rex into Claude Code and Codex with a skill and a
SessionStart hook. Hermes isn't one of its targets, so this plugin does the
same job the Hermes way:

- **Session context** — when Hermes (CLI or TUI) is started inside a Rex pane,
  each new session's system prompt gets Rex's own SessionStart text
  (`rex __internal session-start`): your `REX_SESSION` / `REX_BLOCK`, and the
  instruction to name the Rex session after the task. Rendered once per session
  and frozen, so prompt caching is unaffected. Gateway chats (Telegram, Slack,
  …), cron and Desktop never get it.
- **Skill** — `rex:rex`, a stub that defers to `rex __internal skill`, so the
  guide always matches your installed Rex.

The context text comes from the installed `rex` CLI every time, so upgrading Rex
updates what Hermes is told. Everything fails open: no `rex`, not in Rex, or a
slow/broken `rex` means no section, never an error.

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

## Check it

From a Rex pane:

```sh
hermes chat -q 'Without tools: quote REX_SESSION from your context. Then run: echo $REX_SESSION'
```

Both values should match. Outside Rex the model should report no Rex context.

## Notes

- The session ID is captured when a session starts. A resumed session keeps the
  pane it was created in.
- `--agent opencode` is used for Rex's agent-neutral wording; the plugin appends
  the Hermes skill name (`rex:rex`).
- `rex skill status` won't list Hermes. Native support belongs in
  `rex skill setup`.

## License

MIT
