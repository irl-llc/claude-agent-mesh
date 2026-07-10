# claude-agent-mesh — contributor/agent guidance

Read [DESIGN.md](DESIGN.md) first. It is the source of truth for architecture,
the wire-protocol findings the code rests on, and the decision log (D1–D10).
Do not contradict a SETTLED/OPERATOR-DIRECTED decision without flagging it.

## Map

| Module | Role |
|---|---|
| `mesh_wrapper.py` | the wrapper: transparent proxy + activation gate + presence/heartbeat + GC leadership + inbox→splice delivery + compact re-seed. Spawned by VS Code; must stay stdlib-only and runnable standalone. |
| `wire.py` | the **single** wire-format adapter: stream-json frame split/parse/validate/build. All knowledge of the extension↔engine seam lives here and nowhere else (D7). |
| `mesh_runtime.py` | shared substrate: runtime-tree paths, config load/hot-reload, front-matter parse/serialize + validation, atomic file ops, presence I/O, token bucket. Imported by both the wrapper and the CLI. |
| `claude_agent_mesh.py` | agent-facing CLI (`claude-agent-mesh send`/`peers`): stamp/validate/liveness/rate-limit/atomic write. |
| `pyproject.toml`, `Formula/` | distribution: uv console scripts + head-only Homebrew formula (the repo doubles as a tap). |
| `skills/agent-messaging/` | the protocol contract agents follow (ships in the plugin). |
| `testdata/` | redacted wire fixture — governed by the redaction contract below. |

## Invariants (violating any of these is a bug, not a style choice)

1. **Fail-open (D2).** Any exception in mesh logic disables mesh for the
   process lifetime and the proxy keeps proxying bytes. Nothing in the mesh
   path may ever raise out of the proxy loop.
2. **The wrapper only ever adds `{"type":"user",…}` frames**, only at frame
   boundaries. It never edits, reorders, or fabricates control-plane frames.
3. **Python stdlib only** (D8). The wrapper runs outside any build system.
   Keep syntax ≥3.9-compatible (macOS system Python).
4. **Wire knowledge stays in `wire.py`** (D7). If you need a new frame shape,
   add it to the adapter and its contract test, not inline.
5. **Send rules are code, not prose** (D10). Anything the skill "asks" agents
   to do that matters mechanically (validation, rate, liveness, atomicity)
   must be enforced in `claude_agent_mesh.py`.
6. **All sweeps idempotent and race-tolerant.** GC leadership via `flock` is
   contention control, never a correctness dependency.

## Testing

`python3 -m unittest discover -s tests` — CI runs exactly this. Philosophy:
**no mocks**. Integration tests run the real wrapper as a subprocess around a
real fake-engine child process speaking fixture-derived stream-json over real
pipes; unit tests exercise real files in temp runtime trees
(`CLAUDE_MESH_HOME` overrides `~/.claude/agent-mesh` for tests). The fixture
replay is a **wrapper-regression test only** — extension drift is caught at
runtime by strict frame validation, not in CI (D7).

## Fixture redaction contract (T1)

Nothing under `testdata/` may contain: OAuth/access tokens, account/org ids,
emails, hooks-config values, absolute home paths, or real message text —
placeholders only. `tests/test_fixture_redaction.py` enforces this by
scanning for secret-shaped patterns; keep it passing and extend the patterns
when new fixture material lands.

## Git

Use git-spice for commits. Domain-oriented commits: each commit is one
coherent capability, telling the story of the design's tranches.
