"""Shared substrate for the mesh: the user-global runtime tree and its formats.

Everything both the wrapper and the ``mesh`` CLI need lives here: runtime-tree
paths, config load + mtime hot-reload, the front-matter message grammar with
its mechanical send-path validation (D6/D10), atomic file operations, presence
I/O, and the send-rate token bucket.

Stdlib only (D8); Python >= 3.9.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import tempfile
import time

ENV_HOME = "CLAUDE_MESH_HOME"  # test/override hook for ~/.claude/agent-mesh
ENV_IDENTITY_FILE = "CLAUDE_MESH_SESSION_FILE"
ENV_DISABLE = "CLAUDE_MESH_DISABLE"
ENV_RECURSION_GUARD = "CLAUDE_MESH_WRAPPED"

# All tunables (Q2). config.json overrides individual keys; anything absent,
# extra, or malformed fails open to these values.
DEFAULT_CONFIG = {
    "heartbeat_seconds": 60,
    "presence_stale_seconds": 300,
    "gc_tick_seconds": 300,
    "gc_jitter_seconds": 60,
    "orphan_retention_seconds": 7 * 24 * 3600,
    "splice_body_cap_bytes": 8192,
    "send_body_cap_bytes": 262144,
    "header_value_max_len": 512,
    "send_bucket_capacity": 8,
    "send_bucket_refill_seconds": 30,
    "inbox_poll_seconds": 1.0,
    "config_check_seconds": 60,
    "title_poll_seconds": 5.0,
    "title_backfill_max_bytes": 4 * 1024 * 1024,
    "log_max_bytes": 1048576,
    "log_retention_seconds": 7 * 24 * 3600,
    "claude_binary": None,  # shim-mode engine path (Q5); unused in prefix mode
}

# Engine-assigned session ids are UUIDs, but ``mesh send --to`` builds paths
# from its argument, so the id grammar is enforced defensively everywhere.
_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")

_HEADER_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# Canonical header order for serialization; parse tolerates any order and
# unknown keys (forward compatibility).
HEADER_ORDER = ("message-id", "thread-id", "in-reply-to", "from", "to", "subject", "date")


class MeshError(Exception):
    """Base for mesh-runtime failures with a human-actionable message."""


class MessageError(MeshError):
    """A message violates the front-matter contract."""


def valid_sid(sid) -> bool:
    return isinstance(sid, str) and bool(_SID_RE.match(sid))


class Paths:
    """The ``~/.claude/agent-mesh/`` runtime tree (D4). 0700 throughout."""

    def __init__(self, root=None):
        if root is None:
            root = os.environ.get(ENV_HOME) or os.path.join(
                os.path.expanduser("~"), ".claude", "agent-mesh"
            )
        self.root = root
        self.presence_dir = os.path.join(root, "presence")
        self.identity_dir = os.path.join(root, "identity")
        self.inbox_root = os.path.join(root, "inbox")
        self.log_dir = os.path.join(root, "log")
        self.send_rate_dir = os.path.join(root, "send_rate")
        self.leader_lock = os.path.join(root, "leader.lock")
        self.config_path = os.path.join(root, "config.json")

    def ensure_tree(self):
        for d in (
            self.root,
            self.presence_dir,
            self.identity_dir,
            self.inbox_root,
            self.log_dir,
            self.send_rate_dir,
        ):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)  # makedirs mode is umask-subject; 0700 is the posture

    def presence_path(self, sid: str) -> str:
        assert valid_sid(sid)
        return os.path.join(self.presence_dir, sid + ".json")

    def identity_path(self, mesh_id: str) -> str:
        assert valid_sid(mesh_id)
        return os.path.join(self.identity_dir, mesh_id + ".json")

    def inbox_dir(self, sid: str) -> str:
        assert valid_sid(sid)
        return os.path.join(self.inbox_root, sid)

    def inbox_subdirs(self, sid: str):
        base = self.inbox_dir(sid)
        return (
            os.path.join(base, "tmp"),
            os.path.join(base, "new"),
            os.path.join(base, "cur"),
        )

    def ensure_inbox(self, sid: str):
        for d in self.inbox_subdirs(sid):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)

    def send_rate_path(self, sid: str) -> str:
        assert valid_sid(sid)
        return os.path.join(self.send_rate_dir, sid + ".json")


# ---------------------------------------------------------------------------
# Atomic file operations (tmp + rename in the same directory — no torn reads)


def atomic_write_bytes(path: str, data: bytes):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj):
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path: str):
    """Read a JSON file; returns None on missing/malformed (callers fail open)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def unlink_quiet(path: str):
    """Idempotent delete: ENOENT is a success (GC race tolerance, duty 4)."""
    try:
        os.unlink(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


# ---------------------------------------------------------------------------
# Config: load at spawn, hot-reload on mtime change (duty 8, Q2)


class Config:
    def __init__(self, path=None):
        self._path = path
        self._mtime = None
        self.values = dict(DEFAULT_CONFIG)
        self.load_error = None

    @classmethod
    def load(cls, path: str) -> "Config":
        cfg = cls(path)
        cfg._read()
        return cfg

    def _read(self):
        self.values = dict(DEFAULT_CONFIG)
        self.load_error = None
        try:
            st = os.stat(self._path)
        except OSError:
            self._mtime = None
            return
        self._mtime = st.st_mtime
        raw = read_json(self._path)
        if not isinstance(raw, dict):
            self.load_error = "malformed config.json — using defaults"
            return
        for key, default in DEFAULT_CONFIG.items():
            if key not in raw:
                continue
            val = raw[key]
            if key == "claude_binary":
                if val is None or isinstance(val, str):
                    self.values[key] = val
                else:
                    self.load_error = "claude_binary must be a string — using default"
            elif isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                self.values[key] = val
            else:
                self.load_error = "invalid value for %s — using default" % key

    def maybe_reload(self) -> bool:
        """Reload if config.json's mtime changed. Returns True on reload."""
        try:
            st = os.stat(self._path)
            mtime = st.st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._read()
            return True
        return False

    def __getitem__(self, key):
        return self.values[key]


# ---------------------------------------------------------------------------
# Front-matter message grammar (D6)
#
# On disk, in the spliced context, in the UI, in a grep: one format —
#
#   ---
#   message-id: <uuid>
#   from: <title> <<session_id>>
#   ...
#   ---
#
#   body markdown
#
# The parser is delimiter-safe by construction (header mode ends at the second
# ``---`` line; body bytes can never retroactively become headers) *provided*
# header values are single-line — which the send path enforces mechanically.


def validate_header_value(key: str, value: str, max_len: int):
    """Return an error string if the value violates the contract, else None."""
    if not isinstance(value, str) or not value:
        return "header %r must be a non-empty string" % key
    if len(value) > max_len:
        return "header %r exceeds %d characters" % (key, max_len)
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return "header %r contains a control character (newlines in header values are rejected by contract)" % key
    return None


def serialize_message(headers: dict, body: str, header_value_max_len: int, body_cap: int) -> str:
    """Render a message to its on-disk form, validating mechanically (D10 #2).

    Raises :class:`MessageError` before anything is written if any header
    value is multi-line/control-laden/oversized or the body exceeds its cap.
    """
    ordered = [k for k in HEADER_ORDER if k in headers]
    extra = sorted(k for k in headers if k not in HEADER_ORDER)
    lines = ["---"]
    for key in ordered + extra:
        if not _HEADER_KEY_RE.match(key):
            raise MessageError("invalid header key %r" % key)
        value = headers[key]
        err = validate_header_value(key, value, header_value_max_len)
        if err:
            raise MessageError(err)
        lines.append("%s: %s" % (key, value))
    lines.append("---")
    if not isinstance(body, str):
        raise MessageError("body must be a string")
    if len(body.encode("utf-8")) > body_cap:
        raise MessageError("body exceeds the %d-byte cap" % body_cap)
    return "\n".join(lines) + "\n\n" + body.rstrip("\n") + "\n"


def parse_message(text: str):
    """Parse the on-disk form into ``(headers, body)``.

    Header mode exits at the second ``---`` unconditionally; a body that
    quotes a whole message (front-matter and all) stays body content.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise MessageError("missing front-matter opening delimiter")
    headers = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            body = "\n".join(lines[i + 1 :])
            if body.startswith("\n"):
                body = body[1:]
            return headers, body
        if ":" not in line:
            raise MessageError("malformed header line %d" % (i + 1))
        key, _, value = line.partition(":")
        key = key.strip()
        if not _HEADER_KEY_RE.match(key):
            raise MessageError("invalid header key %r" % key)
        headers[key] = value.strip()
        i += 1
    raise MessageError("missing front-matter closing delimiter")


def format_from(title, session_id: str) -> str:
    """The ``from``/``to`` grammar: ``<title> <<session_id>>`` (D6)."""
    title = (title or "untitled").replace("\n", " ").strip()
    return "%s <<%s>>" % (title, session_id)


# ---------------------------------------------------------------------------
# Presence (duty 3)


def write_presence(paths: Paths, info: dict):
    atomic_write_json(paths.presence_path(info["session_id"]), info)


def read_presence(paths: Paths, sid: str):
    if not valid_sid(sid):
        return None
    info = read_json(paths.presence_path(sid))
    if isinstance(info, dict) and info.get("session_id") == sid:
        return info
    return None


def read_roster(paths: Paths):
    """All presence records, malformed entries skipped, newest-first."""
    roster = []
    try:
        names = os.listdir(paths.presence_dir)
    except OSError:
        return roster
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        info = read_presence(paths, name[: -len(".json")])
        if info:
            roster.append(info)
    roster.sort(key=lambda i: i.get("last_heartbeat") or 0, reverse=True)
    return roster


# ---------------------------------------------------------------------------
# Send-rate token bucket (D10 #4) — flock-guarded read-modify-write


def take_send_token(paths: Paths, sender_sid: str, config: Config, now=None):
    """Try to take one token. Returns ``(True, 0)`` or ``(False, retry_after_s)``.

    State rides ``send_rate/<sid>.json``; the read-modify-write is guarded by
    an flock on the state file. On platforms/filesystems without flock the
    guard degrades to best-effort (worst case: a rare extra send slips
    through — rate limiting is back-pressure, not a correctness dependency).
    """
    if now is None:
        now = time.time()
    capacity = float(config["send_bucket_capacity"])
    refill_seconds = float(config["send_bucket_refill_seconds"])
    path = paths.send_rate_path(sender_sid)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        raw = os.read(fd, 65536)
        try:
            state = json.loads(raw.decode("utf-8"))
            tokens = float(state["tokens"])
            updated = float(state["updated"])
        except (ValueError, KeyError, TypeError):
            tokens, updated = capacity, now
        elapsed = max(0.0, now - updated)
        tokens = min(capacity, tokens + elapsed / refill_seconds)
        if tokens >= 1.0:
            tokens -= 1.0
            ok, retry_after = True, 0
        else:
            ok, retry_after = False, int((1.0 - tokens) * refill_seconds) + 1
        payload = json.dumps({"tokens": tokens, "updated": now}).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        return ok, retry_after
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Wrapper log (duty 4 rotates it)


class MeshLog:
    """Append-only event log under ``log/``; never raises to its caller."""

    def __init__(self, paths: Paths, name: str):
        self._path = os.path.join(paths.log_dir, name)

    @property
    def path(self):
        return self._path

    def line(self, message: str):
        try:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            with open(self._path, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (ts, message.replace("\n", " ")))
        except OSError:
            pass


def tree_mode_ok(path: str) -> bool:
    """True if the path exists with owner-only permissions (0700 posture)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_IMODE(st.st_mode) & 0o077 == 0
