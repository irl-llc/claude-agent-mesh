"""Shared harness: run the real wrapper around the real fake engine."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests import REPO_ROOT

WRAPPER = os.path.join(REPO_ROOT, "mesh_wrapper.py")
FAKE_ENGINE = os.path.join(REPO_ROOT, "tests", "fake_engine.py")
MESH_CLI = os.path.join(REPO_ROOT, "claude_agent_mesh.py")

with open(os.path.join(REPO_ROOT, "testdata", "spawn_argv.json")) as _f:
    SESSION_ARGV = json.load(_f)["session_argv"]

FAKE_SID = "11111111-1111-4111-8111-111111111111"


def user_frame(text):
    frame = {
        "type": "user",
        "uuid": "44444444-4444-4444-8444-444444444444",
        "session_id": "",
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(frame).encode("utf-8") + b"\n"


class WrapperHarness(unittest.TestCase):
    """A temp mesh-home plus helpers to spawn wrapper/engine subprocesses."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mesh-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mesh_home = os.path.join(self.tmp, "agent-mesh")
        self.stdin_log = os.path.join(self.tmp, "engine-stdin.log")
        self._procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()
                p.wait()

    def env(self, **extra):
        env = os.environ.copy()
        env.pop("CLAUDE_MESH_WRAPPED", None)
        env.pop("CLAUDE_MESH_DISABLE", None)
        env["CLAUDE_MESH_HOME"] = self.mesh_home
        env["FAKE_ENGINE_STDIN_LOG"] = self.stdin_log
        env["PYTHONUNBUFFERED"] = "1"
        env.update(extra)
        return env

    def spawn_wrapper(self, engine_args=None, wrapper_argv0=None, **envkw):
        """Wrapper in prefix mode around the fake engine; returns Popen."""
        if engine_args is None:
            engine_args = list(SESSION_ARGV)
        argv = [sys.executable, WRAPPER, sys.executable, FAKE_ENGINE] + engine_args
        p = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(**envkw),
        )
        self._procs.append(p)
        return p

    def run_wrapper(self, input_bytes, engine_args=None, timeout=30, **envkw):
        p = self.spawn_wrapper(engine_args=engine_args, **envkw)
        out, err = p.communicate(input_bytes, timeout=timeout)
        return p.returncode, out, err

    def run_engine_directly(self, input_bytes, engine_args=None, timeout=30):
        if engine_args is None:
            engine_args = list(SESSION_ARGV)
        p = subprocess.Popen(
            [sys.executable, FAKE_ENGINE] + engine_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(FAKE_ENGINE_STDIN_LOG=""),  # don't double-log
        )
        self._procs.append(p)
        out, err = p.communicate(input_bytes, timeout=timeout)
        return p.returncode, out, err

    def wait_for(self, predicate, timeout=15, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % message)

    SENDER_SID = "33333333-3333-4333-8333-333333333333"

    def mesh_send(self, subject, body, to=FAKE_SID, expect_rc=0, **flags):
        """Run the real `claude-agent-mesh send` CLI as a peer session would."""
        identity_file = os.path.join(self.tmp, "sender-identity.json")
        if not os.path.exists(identity_file):
            with open(identity_file, "w") as f:
                json.dump(
                    {"session_id": self.SENDER_SID, "title": "Sender tab", "cwd": "/PLACEHOLDER"},
                    f,
                )
        argv = [sys.executable, MESH_CLI, "send", "--to", to, "--subject", subject, "--body", body]
        for key, value in flags.items():
            argv += ["--" + key.replace("_", "-"), value]
        p = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=self.env(CLAUDE_MESH_SESSION_FILE=identity_file),
            timeout=30,
        )
        if expect_rc is not None:
            assert p.returncode == expect_rc, (p.returncode, p.stderr)
        return p

    def live_session(self, engine_args=None, **envkw):
        """Spawn a wrapped session and keep stdin open for interaction."""
        return _LiveSession(self, self.spawn_wrapper(engine_args=engine_args, **envkw))

    def presence_path(self, sid=FAKE_SID):
        return os.path.join(self.mesh_home, "presence", sid + ".json")

    def read_presence(self, sid=FAKE_SID):
        try:
            with open(self.presence_path(sid)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def stdin_log_lines(self):
        try:
            with open(self.stdin_log, "rb") as f:
                return [json.loads(l) for l in f.read().splitlines() if l.strip()]
        except (OSError, ValueError):
            return []

    def wrapper_log_text(self):
        log_dir = os.path.join(self.mesh_home, "log")
        chunks = []
        if os.path.isdir(log_dir):
            for name in sorted(os.listdir(log_dir)):
                with open(os.path.join(log_dir, name), "r", encoding="utf-8") as f:
                    chunks.append(f.read())
        return "\n".join(chunks)


class _LiveSession:
    def __init__(self, harness, proc):
        self._harness = harness
        self.proc = proc
        self._closed = False

    def send(self, text):
        self.proc.stdin.write(user_frame(text))
        self.proc.stdin.flush()

    def close(self, timeout=30):
        """Close stdin, wait for exit; returns (returncode, stdout, stderr)."""
        self._closed = True
        out, err = self.proc.communicate(timeout=timeout)
        return self.proc.returncode, out, err

    def assistant_texts(self, stdout_bytes):
        texts = []
        for line in stdout_bytes.splitlines():
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("type") == "assistant":
                for block in frame["message"]["content"]:
                    if block.get("type") == "text":
                        texts.append(block["text"])
        return texts
