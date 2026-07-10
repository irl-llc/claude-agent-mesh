# Wire fixture

Pinned shapes for the extension↔engine stream-json seam, replayed by the
contract tests in `tests/`. The fixture is a **wrapper-regression test only**
(D7): it cannot detect an extension upgrade — runtime strict frame validation
in `wire.py` does that.

## Provenance

Reconstructed 2026-07-10 from the T0 capture findings recorded in
[DESIGN.md §Wire-protocol findings](../DESIGN.md) (instrumented-wrapper live
capture against extension `anthropic.claude-code` v2.1.201: init → turn →
mid-turn steer → `/compact` → post-compact turn). The raw capture logs were
session-scratchpad throwaways and are not committed; when a fresh live capture
is taken, regenerate these files from it and keep the redaction contract
below holding.

## Redaction contract (enforced, not asserted)

Nothing in this directory may contain: OAuth/access tokens or anything
secret-shaped, account/org identifiers, email addresses, hooks-config values,
absolute home paths (macOS or Linux user directories), or real message text.
Placeholders only (`PLACEHOLDER`, `REDACTED`, RFC-2606 domains, all-1s/2s
UUIDs). `tests/test_fixture_redaction.py` scans every file here for
secret-shaped patterns and fails CI on a hit — extend its patterns when new
fixture material lands.

## Files

- `spawn_argv.json` — the captured spawn argv contract (finding 1), plus
  one-shot spawn shapes the activation gate must pass through.
- `stdin_frames.jsonl` — extension→engine frames: control-plane initialize,
  a user turn, a mid-turn user message (finding 3).
- `stdout_frames.jsonl` — engine→extension frames: `system/init`, streaming
  and final assistant output, `--replay-user-messages` echo, result, the
  compaction sequence (`status: compacting` → `compact_boundary` → summary
  user message, finding 4), and a title-bearing control response.
