#!/usr/bin/env python3
"""The mesh wrapper: a transparent extension<->engine proxy that adds the mesh.

Installed as VS Code's ``claudeCode.claudeProcessWrapper`` (prefix semantics:
``<wrapper> <real-claude> <args...>``) or invoked *as* ``claude`` via a PATH
shim (Q5; engine resolved from config.json's ``claude_binary``).

The reliability contract (D2): the byte-exact proxy is the foundation; every
mesh feature is additive, and any exception in mesh logic disables mesh for
the rest of the process while the proxy keeps proxying. Spawn shapes that are
not a real stream-json session (one-shot subcommands, PTY terminals) exec the
real engine directly and get no mesh side effects at all.

Stdlib only (D8); Python >= 3.9.
"""

from __future__ import annotations

import errno
import os
import random
import select
import signal
import subprocess
import sys
import time
import uuid

# Resolve colocated modules through symlinks (PATH-shim mode installs a
# symlink; sys.path[0] would be the symlink's directory).
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import mesh_runtime as rt  # noqa: E402
import wire  # noqa: E402

_HIGH_WATER = 1 << 20  # stop reading a side whose write buffer is this deep
_TICK_SECONDS = 0.25


def _err(message: str):
    sys.stderr.write("mesh-wrapper: %s\n" % message)
    sys.stderr.flush()


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _stdin_is_pipe() -> bool:
    import stat as _stat

    try:
        return _stat.S_ISFIFO(os.fstat(0).st_mode)
    except OSError:
        return False


def _set_nonblocking(fd: int):
    os.set_blocking(fd, False)


class Proxy:
    """Byte-exact stdin/stdout plumbing with frame-boundary injection.

    Pure mechanism: forwards ``in_fd`` -> child stdin and child stdout ->
    ``out_fd`` verbatim, with backpressure, and flushes injected frames into
    the child's stdin only between the extension's own newline-terminated
    frames. Interpretation (the mesh) hangs off the ``on_lines_*``/``on_tick``
    hooks, which must never raise (the mesh guards itself; a hook exception
    here would violate D2).
    """

    def __init__(
        self,
        child,
        in_fd: int = 0,
        out_fd: int = 1,
        tick: float = _TICK_SECONDS,
        on_lines_in=None,
        on_lines_out=None,
        on_tick=None,
    ):
        self._child = child
        self._in_fd = in_fd
        self._out_fd = out_fd
        self._tick = tick
        self._on_lines_in = on_lines_in
        self._on_lines_out = on_lines_out
        self._on_tick = on_tick

        self._child_stdin_fd = child.stdin.fileno()
        self._child_stdout_fd = child.stdout.fileno()

        self._to_child = bytearray()
        self._to_out = bytearray()
        self._stdin_splitter = wire.FrameSplitter()  # boundary tracking only
        self._out_splitter = wire.FrameSplitter()
        self._inject_queue = []  # list of (frame_bytes, on_flushed or None)
        self._flush_marks = []  # list of (threshold, on_flushed)
        self._enqueued_to_child = 0
        self._written_to_child = 0

        self._stdin_eof = False
        self._child_stdout_eof = False
        self._child_stdin_closed = False
        self._out_broken = False

        for fd in (self._in_fd, self._out_fd, self._child_stdin_fd, self._child_stdout_fd):
            _set_nonblocking(fd)

    # -- injection ----------------------------------------------------------

    def inject(self, frame_bytes: bytes, on_flushed=None):
        """Queue one complete newline-terminated frame for boundary splice."""
        self._inject_queue.append((frame_bytes, on_flushed))
        self._flush_injections()

    def clear_injections(self):
        self._inject_queue = []

    @property
    def inject_pending(self) -> bool:
        return bool(self._inject_queue) or bool(self._flush_marks)

    def _flush_injections(self):
        if self._child_stdin_closed or not self._stdin_splitter.at_boundary:
            return
        while self._inject_queue:
            frame_bytes, on_flushed = self._inject_queue.pop(0)
            self._append_to_child(frame_bytes)
            if on_flushed is not None:
                self._flush_marks.append((self._enqueued_to_child, on_flushed))

    def _append_to_child(self, data: bytes):
        self._to_child.extend(data)
        self._enqueued_to_child += len(data)

    def _fire_flush_marks(self):
        while self._flush_marks and self._flush_marks[0][0] <= self._written_to_child:
            _, on_flushed = self._flush_marks.pop(0)
            on_flushed()

    # -- the loop ------------------------------------------------------------

    def run(self) -> int:
        while True:
            rlist, wlist = [], []
            if not self._stdin_eof and len(self._to_child) < _HIGH_WATER:
                rlist.append(self._in_fd)
            if not self._child_stdout_eof and len(self._to_out) < _HIGH_WATER:
                rlist.append(self._child_stdout_fd)
            if self._to_child and not self._child_stdin_closed:
                wlist.append(self._child_stdin_fd)
            if self._to_out and not self._out_broken:
                wlist.append(self._out_fd)

            try:
                readable, writable, _ = select.select(rlist, wlist, [], self._tick)
            except (OSError, select.error) as e:  # EINTR on pre-3.5 semantics, etc.
                if getattr(e, "errno", None) == errno.EINTR:
                    continue
                raise

            if self._in_fd in readable:
                self._read_stdin()
            if self._child_stdout_fd in readable:
                self._read_child_stdout()
            if self._child_stdin_fd in writable:
                self._write_child_stdin()
            if self._out_fd in writable:
                self._write_out()

            self._flush_injections()

            # Extension closed stdin and everything owed the child is flushed:
            # pass the EOF along so the engine can exit.
            if (
                self._stdin_eof
                and not self._to_child
                and not self._inject_queue
                and not self._child_stdin_closed
            ):
                self._close_child_stdin()

            if self._on_tick is not None:
                self._on_tick()

            exited = self._child.poll() is not None
            if exited and self._child_stdout_eof and (not self._to_out or self._out_broken):
                break

        code = self._child.returncode
        if code is not None and code < 0:
            return 128 - code  # died on signal N -> conventional 128+N
        return code if code is not None else 1

    # -- reads ----------------------------------------------------------------

    def _read_stdin(self):
        try:
            chunk = os.read(self._in_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._stdin_eof = True
            return
        lines = self._stdin_splitter.feed(chunk)
        self._append_to_child(chunk)
        if lines and self._on_lines_in is not None:
            self._on_lines_in(lines)

    def _read_child_stdout(self):
        try:
            chunk = os.read(self._child_stdout_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._child_stdout_eof = True
            return
        self._to_out.extend(chunk)
        lines = self._out_splitter.feed(chunk)
        if lines and self._on_lines_out is not None:
            self._on_lines_out(lines)

    # -- writes ---------------------------------------------------------------

    def _write_child_stdin(self):
        try:
            n = os.write(self._child_stdin_fd, self._to_child)
        except BlockingIOError:
            return
        except OSError:
            # Child stdin gone (child dying): drop what we owed it.
            self._to_child.clear()
            self._inject_queue = []
            self._flush_marks = []
            self._close_child_stdin()
            return
        del self._to_child[:n]
        self._written_to_child += n
        self._fire_flush_marks()

    def _write_out(self):
        try:
            n = os.write(self._out_fd, self._to_out)
        except BlockingIOError:
            return
        except OSError:
            # The extension went away; nothing left to forward to. Ask the
            # child to wind down rather than filling buffers forever.
            self._out_broken = True
            self._to_out.clear()
            try:
                self._child.terminate()
            except OSError:
                pass
            return
        del self._to_out[:n]

    def _close_child_stdin(self):
        if not self._child_stdin_closed:
            self._child_stdin_closed = True
            try:
                self._child.stdin.close()
            except OSError:
                pass


class MeshState:
    """The mesh's interpretive state machine, fed by proxy hooks.

    Every hook is guarded: the first exception (or unrecognized wire frame,
    D7) disables mesh for the rest of the process and the proxy keeps
    proxying (D2).
    """

    def __init__(self, paths: rt.Paths):
        self.enabled = False
        self.disabled_reason = None
        self.paths = paths
        self.proxy = None
        self.session_id = None
        self.title = None
        self.cwd = None
        self.model = None
        self.engine_version = None
        self.mesh_id = uuid.uuid4().hex
        self.log = None
        try:
            paths.ensure_tree()
            self.config = rt.Config.load(paths.config_path)
            self.log = rt.MeshLog(
                paths, "wrapper-%s-%d.log" % (time.strftime("%Y%m%d"), os.getpid())
            )
            self.identity_file = paths.identity_path(self.mesh_id)
            self.enabled = True
            if self.config.load_error:
                self.log.line("config: %s" % self.config.load_error)
            self.log.line("mesh activated pid=%d" % os.getpid())
        except Exception as e:  # fail-open: no tree, no mesh — proxy continues
            self.disabled_reason = "init failed: %r" % e
            _err("mesh disabled (%s); proxying only" % self.disabled_reason)

    def attach(self, proxy: Proxy):
        self.proxy = proxy

    def disable(self, reason: str):
        if not self.enabled:
            return
        self.enabled = False
        self.disabled_reason = reason
        if self.proxy is not None:
            self.proxy.clear_injections()
        if self.log is not None:
            self.log.line(
                "mesh disabled: %s (engine_version=%s, fixture=%s)"
                % (reason, self.engine_version, wire.FIXTURE_EXTENSION_VERSION)
            )

    def _guarded(self, fn, *args):
        if not self.enabled:
            return
        try:
            fn(*args)
        except wire.UnrecognizedFrame as e:
            self.disable("unrecognized frame (%s): %r" % (e.reason, e.sample))
        except Exception as e:
            self.disable("mesh error: %r" % e)

    # -- proxy hooks (never raise) -------------------------------------------

    def on_lines_in(self, lines):
        self._guarded(self._handle_lines_in, lines)

    def on_lines_out(self, lines):
        self._guarded(self._handle_lines_out, lines)

    def on_tick(self):
        self._guarded(self._handle_tick, time.time())

    def on_exit(self):
        try:
            self._cleanup()
        except Exception:
            pass

    # -- interpretation --------------------------------------------------------

    def _handle_lines_in(self, lines):
        for line in lines:
            frame = wire.parse_frame(line)  # strict: drift disables mesh (D7)
            if frame is None:
                continue
            title = wire.control_title(frame)
            if title:
                self._set_title(title)

    def _handle_lines_out(self, lines):
        for line in lines:
            frame = wire.parse_frame(line)
            if frame is None:
                continue
            info = wire.init_info(frame)
            if info:
                self._on_init(info)
                continue
            if wire.is_compact_boundary(frame):
                self._on_compact_boundary()
                continue
            title = wire.control_title(frame)
            if title:
                self._set_title(title)

    def _handle_tick(self, now: float):
        pass  # presence heartbeat / inbox poll / GC arrive with T2/T3

    def _on_init(self, info):
        self.session_id = info["session_id"]
        self.cwd = info["cwd"]
        self.model = info["model"]
        self.engine_version = info["version"]
        if self.engine_version != wire.FIXTURE_EXTENSION_VERSION:
            self.log.line(
                "version skew: engine=%s fixture=%s (informational, D7)"
                % (self.engine_version, wire.FIXTURE_EXTENSION_VERSION)
            )

    def _on_compact_boundary(self):
        pass  # roster re-seed arrives with T3

    def _set_title(self, title: str):
        self.title = title

    def _cleanup(self):
        pass  # presence/identity deletion arrives with T2


def _passthrough_exec(real: str, engine_args):
    try:
        os.execv(real, [real] + engine_args)
    except OSError as e:
        _err("cannot exec %s: %s" % (real, e))
        sys.exit(127)


def run_session(real: str, engine_args) -> int:
    mesh = MeshState(rt.Paths())
    child_env = os.environ.copy()
    child_env[rt.ENV_RECURSION_GUARD] = "1"
    argv = [real] + list(engine_args)
    if mesh.enabled:
        child_env[rt.ENV_IDENTITY_FILE] = mesh.identity_file

    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # engine stderr flows to the extension untouched
            bufsize=0,
            env=child_env,
        )
    except OSError as e:
        _err("cannot spawn %s: %s" % (real, e))
        return 127

    proxy = Proxy(
        child,
        on_lines_in=mesh.on_lines_in,
        on_lines_out=mesh.on_lines_out,
        on_tick=mesh.on_tick,
    )
    mesh.attach(proxy)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(signum, forward)
        except (OSError, ValueError):
            pass

    try:
        code = proxy.run()
    finally:
        mesh.on_exit()
    return code


_DEPTH_ENV = "CLAUDE_MESH_EXEC_DEPTH"
_MAX_EXEC_DEPTH = 5


def main(argv) -> int:
    self_path = os.path.realpath(__file__)

    # Exec-depth guard: the realpath self-check below cannot see a generated
    # console-script shim (uv/brew installs) that re-enters the wrapper, so
    # cap re-entry depth outright. Legitimate nesting (a claude spawned from
    # inside a wrapped session) sits at depth 2-3; a loop hits 5 instantly.
    try:
        depth = int(os.environ.get(_DEPTH_ENV, "0"))
    except ValueError:
        depth = 0
    if depth >= _MAX_EXEC_DEPTH:
        _err("refusing recursion: wrapper re-entered %d times (is claude_binary pointing back at the wrapper?)" % depth)
        return 125
    os.environ[_DEPTH_ENV] = str(depth + 1)

    if len(argv) > 1 and _is_executable_file(argv[1]):
        # Prefix-wrapper mode: the extension hands us the real engine path.
        real, engine_args = argv[1], list(argv[2:])
    else:
        # PATH-shim mode (Q5): the engine comes from config.json, explicitly —
        # never from a PATH walk a shadowing shim could poison.
        engine_args = list(argv[1:])
        config = rt.Config.load(rt.Paths().config_path)
        real = config["claude_binary"]
        if not real:
            _err(
                "invoked as a shim but claude_binary is not set in "
                "~/.claude/agent-mesh/config.json; cannot resolve the engine"
            )
            return 127

    if os.path.realpath(real) == self_path:
        _err("refusing recursion: the engine path resolves to the mesh wrapper itself")
        return 125

    if os.environ.get(rt.ENV_RECURSION_GUARD) or os.environ.get(rt.ENV_DISABLE):
        # Nested spawn from inside a wrapped session, or the kill switch:
        # pure passthrough, no mesh side effects.
        _passthrough_exec(real, engine_args)

    if not (wire.is_stream_json_session_argv(engine_args) and _stdin_is_pipe()):
        # Activation gate (duty 2): one-shots, PTY terminals, unknown shapes.
        _passthrough_exec(real, engine_args)

    return run_session(real, engine_args)


def cli_main():
    """Console-script entry point (uv/Homebrew installs)."""
    sys.exit(main(sys.argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
