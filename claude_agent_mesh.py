#!/usr/bin/env python3
"""claude-agent-mesh — the agent-facing CLI: ``send`` and ``peers`` (D10).

Sends go through this CLI, never through hand-written inbox files: every
previously prose-enforced send rule is code here — stamping, mechanical
front-matter validation, the recipient-liveness re-check, token-bucket
back-pressure, and the atomic Maildir tmp/ -> new/ rename. A refused send
fails visibly (nonzero exit + actionable stderr) so the agent backs off.

Exit codes: 0 sent · 2 validation/usage · 3 peer gone · 4 rate limited ·
5 no mesh identity.

Stdlib only (D8); Python >= 3.9.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import mesh_runtime as rt  # noqa: E402

EXIT_VALIDATION = 2
EXIT_PEER_GONE = 3
EXIT_RATE_LIMITED = 4
EXIT_NO_IDENTITY = 5


class CliError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _load_identity():
    """The sender's own sid/title, from the wrapper's env-pointed file (duty 3)."""
    path = os.environ.get(rt.ENV_IDENTITY_FILE)
    if not path:
        raise CliError(
            EXIT_NO_IDENTITY,
            "%s is not set — this does not look like a mesh session "
            "(the mesh wrapper sets it; see the agent-messaging skill)" % rt.ENV_IDENTITY_FILE,
        )
    identity = rt.read_json(path)
    if not isinstance(identity, dict) or not rt.valid_sid(identity.get("session_id") or ""):
        raise CliError(
            EXIT_NO_IDENTITY,
            "identity file %s is missing or unreadable — it appears shortly "
            "after session start; retry in a moment" % path,
        )
    return identity


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file is not None:
        try:
            with open(args.body_file, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise CliError(EXIT_VALIDATION, "cannot read --body-file: %s" % e)
    if sys.stdin.isatty():
        raise CliError(EXIT_VALIDATION, "no body: pass --body, --body-file, or pipe stdin")
    return sys.stdin.read()


def cmd_send(args) -> int:
    paths = rt.Paths()
    paths.ensure_tree()
    config = rt.Config.load(paths.config_path)
    identity = _load_identity()
    sender_sid = identity["session_id"]

    if not rt.valid_sid(args.to):
        raise CliError(EXIT_VALIDATION, "--to %r is not a valid session id" % args.to)

    recipient = rt.read_presence(paths, args.to)
    if recipient is None:
        raise CliError(
            EXIT_PEER_GONE,
            "peer %s is gone (no live presence) — run `claude-agent-mesh peers` for the roster" % args.to,
        )

    headers = {
        "message-id": str(uuid.uuid4()),
        "from": rt.format_from(identity.get("title"), sender_sid),
        "to": rt.format_from(recipient.get("title"), args.to),
        "subject": args.subject,
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if args.thread_id:
        headers["thread-id"] = args.thread_id
    if args.in_reply_to:
        headers["in-reply-to"] = args.in_reply_to

    try:
        rendered = rt.serialize_message(
            headers,
            _read_body(args),
            config["header_value_max_len"],
            config["send_body_cap_bytes"],
        )
    except rt.MessageError as e:
        raise CliError(EXIT_VALIDATION, "invalid message: %s" % e)

    ok, retry_after = rt.take_send_token(paths, sender_sid, config)
    if not ok:
        raise CliError(
            EXIT_RATE_LIMITED,
            "rate limited — retry after ~%ds; coalesce pending updates into "
            "one message rather than retrying immediately" % retry_after,
        )

    paths.ensure_inbox(args.to)
    tmp_dir, new_dir, _cur = paths.inbox_subdirs(args.to)
    filename = "%d.%s.md" % (time.time_ns(), headers["message-id"])
    tmp_path = os.path.join(tmp_dir, filename)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(rendered)
        f.flush()
        os.fsync(f.fileno())

    # Liveness re-check immediately before the rename (D10 #3): never a
    # silent write into a readerless mailbox.
    if rt.read_presence(paths, args.to) is None:
        rt.unlink_quiet(tmp_path)
        raise CliError(
            EXIT_PEER_GONE,
            "peer %s went away mid-send — run `claude-agent-mesh peers` for the roster" % args.to,
        )
    os.rename(tmp_path, os.path.join(new_dir, filename))
    print(headers["message-id"])
    return 0


def _age(seconds: float) -> str:
    if seconds < 90:
        return "%ds ago" % max(0, int(seconds))
    if seconds < 5400:
        return "%dm ago" % int(seconds / 60)
    return "%dh ago" % int(seconds / 3600)


def cmd_peers(args) -> int:
    paths = rt.Paths()
    roster = rt.read_roster(paths)
    own_sid = None
    identity_path = os.environ.get(rt.ENV_IDENTITY_FILE)
    if identity_path:
        identity = rt.read_json(identity_path)
        if isinstance(identity, dict):
            own_sid = identity.get("session_id")

    if args.json:
        print(json.dumps(roster, indent=1))
        return 0
    if not roster:
        print("no live peers")
        return 0
    now = time.time()
    rows = [("SESSION-ID", "TITLE", "CWD", "MODEL", "LAST-SEEN")]
    for info in roster:
        marker = " (you)" if info.get("session_id") == own_sid else ""
        rows.append(
            (
                str(info.get("session_id", "?")) + marker,
                str(info.get("title") or "untitled"),
                str(info.get("cwd") or "?"),
                str(info.get("model") or "?"),
                _age(now - (info.get("last_heartbeat") or now)),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return 0


def main(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-agent-mesh", description="peer messaging for the claude-agent-mesh"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="send a message to a live peer")
    p_send.add_argument("--to", required=True, help="recipient session id (see `claude-agent-mesh peers`)")
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--thread-id", help="ticket/epic id when the exchange is about one")
    p_send.add_argument("--in-reply-to", help="message-id being replied to")
    body = p_send.add_mutually_exclusive_group()
    body.add_argument("--body", help="message body (markdown)")
    body.add_argument("--body-file", help="read the body from a file")
    p_send.set_defaults(func=cmd_send)

    p_peers = sub.add_parser("peers", help="the machine-wide roster of live sessions")
    p_peers.add_argument("--json", action="store_true")
    p_peers.set_defaults(func=cmd_peers)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        sys.stderr.write("claude-agent-mesh: %s\n" % e)
        return e.code


def cli_main():
    """Console-script entry point (uv/Homebrew installs)."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
