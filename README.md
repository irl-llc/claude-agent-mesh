# claude-agent-mesh

A flat, human-orchestrated mesh for **N independent Claude Code sessions in VS Code
tabs**. Each tab stays a peer driven directly by you — but the tabs can now see
each other (a live roster) and message each other (hand off a task, ask a
question, announce "I touched `X`, rebase"), across **every project on the
machine**.

No lead process, no daemon, no MCP server, no tmux. One moving part: a
stdlib-Python **mesh wrapper** installed as the VS Code
`claudeCode.claudeProcessWrapper`, which transparently proxies the
extension↔engine stream-json pipe and adds:

- **Presence** — every session writes `~/.claude/agent-mesh/presence/<sid>.json`
  (title, cwd, model, heartbeat). `mesh peers` shows the machine-wide roster.
- **Messaging** — peers send markdown messages (YAML front-matter) via the
  `mesh send` CLI into per-session Maildir inboxes; the recipient's wrapper
  splices each one into the live session as an ordinary user message —
  **including mid-task** — and it renders in the tab's UI.
- **Compaction survival** — after `/compact`, the wrapper re-seeds the roster
  and mesh protocol reminder, so team awareness survives context compaction.

If any mesh feature fails, the wrapper degrades to a pure byte-exact proxy — a
mesh bug never takes down a session. See [DESIGN.md](DESIGN.md) for the full
architecture, wire-protocol findings, and decision log.

## Install

Two steps: install the plugin (skill + CLI), then point one VS Code setting at
the wrapper.

### 1. Install the plugin

In any Claude Code session:

```
/plugin marketplace add irl-llc/claude-agent-mesh
/plugin install agent-mesh@claude-agent-mesh
```

This ships the `agent-messaging` skill (the protocol contract agents follow)
and the `mesh` CLI.

### 2. Install the wrapper at a stable path

The VS Code setting must point at a **stable path** — not the auto-updating
plugin directory. Clone this repo somewhere durable (or install via your
package manager of choice):

```sh
git clone https://github.com/irl-llc/claude-agent-mesh ~/.local/share/claude-agent-mesh
```

Then add to your **user** VS Code `settings.json`:

```json
{
  "claudeCode.claudeProcessWrapper": "~/.local/share/claude-agent-mesh/mesh_wrapper.py"
}
```

VS Code invokes the wrapper as `<wrapper> <real-claude> <args…>`; the wrapper
execs the real engine for anything that isn't an interactive stream-json
session (one-shot subcommands like `claude auth status` pass through inert).

### Optional: PATH-shim mode (terminal sessions)

The wrapper can also be invoked *as* `claude` (a PATH shim or symlink). In that
mode there is no real-binary first argument, so set the engine's absolute path
in `~/.claude/agent-mesh/config.json`:

```json
{ "claude_binary": "/absolute/path/to/real/claude" }
```

A recursion-guard environment variable plus the explicit path guarantees a
shadowing shim can never make the wrapper spawn itself. Terminal (PTY) sessions
fail the activation gate by design and pass through inert — mesh activates only
for stream-json sessions.

## Use

Inside any wrapped session, agents discover and message peers via the
`agent-messaging` skill:

```sh
mesh peers                      # machine-wide roster: title, sid, cwd, last seen
mesh send --to <session-id> --subject "rebase needed" --body "I touched src/wire.py on main — rebase before you continue."
```

Messages are markdown files with YAML front-matter (`message-id`, `from`, `to`,
`subject`, `date`, optional `thread-id`/`in-reply-to`), delivered at-least-once
into the recipient's context and visible in its tab UI. Sends are
back-pressured by a token bucket: an over-rate send **fails visibly** with
`rate limited — retry after ~Ns`, and the etiquette is to coalesce and wait.

## Configuration

All tunables live in `~/.claude/agent-mesh/config.json`, hot-reloaded on
change (no session restart). Defaults: heartbeat 60 s, presence staleness
5 min, GC tick ~5 min ± 60 s jitter, orphan-inbox retention 7 days, spliced
body cap 8 KiB. Malformed config fails open to defaults.

## Uninstall / kill switches

- **Disable instantly:** set `CLAUDE_MESH_DISABLE=1` in the environment, or
  remove the `claudeCode.claudeProcessWrapper` setting (one line). Either
  fully restores stock behavior.
- **Remove:** uninstall the plugin, delete the clone, and delete
  `~/.claude/agent-mesh/` (pure runtime state — nothing is committed into any
  of your repos).

## License

[GPL-3.0](LICENSE).
