"""The single wire-format adapter for the extension<->engine stream-json seam.

Every byte of knowledge about the Claude Code wire protocol lives here (D7):
frame splitting, parsing, strict shape validation, and the one frame shape the
mesh is allowed to fabricate — the ``{"type": "user", ...}`` envelope.

The seam is consumed as a monitored de-facto-stable dependency: the fixture
under ``testdata/`` pins the shapes this module was written against, the
contract tests replay it, and at runtime any frame this module refuses to
recognize must make the caller fail open (disable mesh, keep proxying).
"""

from __future__ import annotations

import json
import os
import re
import uuid

# Engine version the testdata/ fixture shapes were last validated against
# (T0 capture 2026-07-09/10 on extension v2.1.201; re-captured live
# 2026-07-10 against engine 2.1.206). Compared informationally against
# system/init's version field; skew is logged, never fatal (D7).
FIXTURE_ENGINE_VERSION = "2.1.206"

# Top-level frame types observed on the wire (T0 capture + the 2.1.206
# live re-capture) plus the control-plane types documented in the SDK. The
# mesh interprets only a tiny subset (system/init, compact_boundary,
# control titles); a type outside this set is DRIFT TELEMETRY, not an
# error — the caller logs it and passes the bytes through, because a frame
# the mesh never interprets cannot be misinterpreted. Only structural
# unparseability (non-JSON, not an object, no type) fails mesh open.
KNOWN_FRAME_TYPES = frozenset(
    {
        "user",
        "assistant",
        "system",
        "result",
        "stream_event",
        "control_request",
        "control_response",
        "control_cancel_request",
        "command_lifecycle",  # command queue lifecycle (queued/started/completed)
        "rate_limit_event",
        "auth_status",  # --enable-auth-status rides the captured argv
    }
)


class UnrecognizedFrame(Exception):
    """A line on the wire this adapter does not recognize.

    Carries enough context to log the drift; the caller's contract is to
    disable mesh for the process and keep proxying (D2/D7).
    """

    def __init__(self, reason: str, line: bytes):
        super().__init__(reason)
        self.reason = reason
        # Cap what we retain: this may be logged, and frames can be huge.
        self.sample = line[:256]


class FrameSplitter:
    """Incremental newline-delimited frame splitter.

    Bytes go in as they arrive off the pipe; complete newline-terminated
    lines come out. The partial tail is held until its newline arrives.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        self._buf.extend(data)
        lines = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            lines.append(bytes(self._buf[: idx + 1]))
            del self._buf[: idx + 1]
        return lines

    @property
    def pending(self) -> bytes:
        """The partial frame tail not yet terminated by a newline."""
        return bytes(self._buf)

    @property
    def at_boundary(self) -> bool:
        """True when nothing is mid-frame — a safe point to splice a frame."""
        return not self._buf


def parse_frame(line: bytes):
    """Parse one wire line into a frame dict, strictly.

    Returns ``None`` for blank lines (tolerated as keepalive noise).
    Raises :class:`UnrecognizedFrame` for anything that is not a JSON object
    carrying a string ``type`` — structural drift the caller must fail open
    on. An *unknown* type still parses: gate interpretation on
    :func:`is_known_type` and log unknowns as pass-through drift.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        frame = json.loads(stripped)
    except ValueError:
        raise UnrecognizedFrame("not JSON", line)
    if not isinstance(frame, dict):
        raise UnrecognizedFrame("frame is not an object", line)
    ftype = frame.get("type")
    if not isinstance(ftype, str) or not ftype:
        raise UnrecognizedFrame("missing frame type", line)
    return frame


def is_known_type(frame) -> bool:
    """True when the frame's type is in the pinned vocabulary. False is
    drift to log-and-pass-through, never a reason to disable mesh."""
    return frame.get("type") in KNOWN_FRAME_TYPES


def build_user_frame(text: str, session_id: str = "") -> bytes:
    """Build the one frame shape the mesh may fabricate.

    Matches the captured extension->engine user-message envelope byte-for-shape
    (finding 2): ``session_id`` is sent empty by the extension; the engine
    assigns it. Returns a complete newline-terminated line ready to splice at
    a frame boundary.
    """
    frame = {
        "type": "user",
        "uuid": str(uuid.uuid4()),
        "session_id": session_id,
        "parent_tool_use_id": None,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    return json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n"


def init_info(frame):
    """Extract session identity from a ``system/init`` frame, else ``None``.

    Tolerant of extra fields: only the fields the mesh needs are read.
    """
    if frame.get("type") != "system" or frame.get("subtype") != "init":
        return None
    return {
        "session_id": frame.get("session_id"),
        "cwd": frame.get("cwd"),
        "model": frame.get("model"),
        "version": frame.get("version"),
    }


def is_compact_boundary(frame) -> bool:
    return frame.get("type") == "system" and frame.get("subtype") == "compact_boundary"


def control_title(frame):
    """Pull a session title out of a control frame if one is riding past.

    The ``generate_session_title`` control response carries the title; the
    exact nesting is not load-bearing, so search shallow dict levels for a
    string-valued ``title`` key rather than pinning a deep path.
    """
    if frame.get("type") not in ("control_request", "control_response"):
        return None
    stack = [frame]
    depth = 0
    while stack and depth < 64:  # bounded walk; frames are small dicts
        node = stack.pop()
        depth += 1
        if isinstance(node, dict):
            title = node.get("title")
            if isinstance(title, str) and title:
                return title
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def is_stream_json_session_argv(args) -> bool:
    """The argv half of the activation gate (duty 2).

    True when the spawn args declare ``--input-format stream-json`` — the
    signature of a real interactive session per the captured argv contract.
    One-shot subcommands (``auth status --json``) never carry it.
    """
    for i, arg in enumerate(args):
        if arg == "--input-format":
            if i + 1 < len(args) and args[i + 1] == "stream-json":
                return True
        elif arg == "--input-format=stream-json":
            return True
    return False


# -- transcript title records (session-store seam, found live 2026-07-10) ----
#
# A UI rename never crosses the stdio pipe: the webview sends the extension a
# ``rename_session`` request and the extension appends a record to the
# session's transcript jsonl under ``<config-root>/projects/``. Generated
# titles are persisted the same way. These accessors are the only place that
# knows those record shapes; consumed as a monitored dependency exactly like
# the wire — but drift here is FAIL-SOFT (a stale title), never a disable.

# Precedence ranks: a user rename outranks a generated title (mirrors the
# extension's own onlyIfNoCustomTitle rule); within a rank, latest wins.
TITLE_RANK_GENERATED = 1  # {"type":"ai-title","sessionId":…,"aiTitle":…}
TITLE_RANK_CUSTOM = 2  # {"type":"custom-title","sessionId":…,"customTitle":…}

# Fast pre-filter so callers can skip json-parsing transcript lines (which
# carry whole conversation payloads) that cannot be title records.
TITLE_RECORD_MARKERS = (b'"custom-title"', b'"ai-title"')


def transcript_path(config_root: str, cwd: str, session_id: str) -> str:
    """Where the session's transcript jsonl lives for a given cwd.

    The projects-dir slug replaces every non-alphanumeric character of the
    absolute cwd with ``-`` (observed: ``/``, ``.``, and ``-`` all map to
    ``-``). Callers must treat a miss as possible slug drift and fall back
    to globbing ``projects/*/<session_id>.jsonl``.
    """
    slug = _SLUG_RE.sub("-", cwd)
    return os.path.join(config_root, "projects", slug, session_id + ".jsonl")


_SLUG_RE = re.compile(r"[^A-Za-z0-9]")


def title_record(record, session_id: str):
    """``(rank, title)`` from a transcript title record for this session.

    Returns ``None`` for anything else — including malformed title records
    and records for other sessions. Never raises: title staleness is the
    worst permitted failure mode on this seam.
    """
    if not isinstance(record, dict) or record.get("sessionId") != session_id:
        return None
    rtype = record.get("type")
    if rtype == "custom-title":
        title = record.get("customTitle")
        rank = TITLE_RANK_CUSTOM
    elif rtype == "ai-title":
        title = record.get("aiTitle")
        rank = TITLE_RANK_GENERATED
    else:
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    return (rank, title.strip())
