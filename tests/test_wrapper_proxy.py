"""Proxy foundation + activation gate integration tests (T1).

Real wrapper subprocess, real fake-engine child, real pipes — no mocks.
"""

import json
import os
import stat
import subprocess
import sys
import unittest

from tests.harness import (
    FAKE_ENGINE,
    SESSION_ARGV,
    WRAPPER,
    WrapperHarness,
    user_frame,
)


class ByteExactProxyTest(WrapperHarness):
    def test_wrapper_output_is_byte_identical_to_direct_engine_run(self):
        script = (
            user_frame("ECHO one")
            + user_frame("SLOWFRAME")
            + user_frame("ECHO two")
            + user_frame("COMPACT")
        )
        rc_direct, out_direct, _ = self.run_engine_directly(script)
        rc_wrapped, out_wrapped, err = self.run_wrapper(script)
        self.assertEqual(rc_direct, 0)
        self.assertEqual(rc_wrapped, 0, err)
        self.assertEqual(out_wrapped, out_direct)

    def test_stderr_passes_through(self):
        rc, _, err = self.run_wrapper(user_frame("STDERR boo"))
        self.assertEqual(rc, 0)
        self.assertIn(b"boo", err)

    def test_clean_exit_code_preserved(self):
        rc, _, _ = self.run_wrapper(user_frame("EXIT 7"))
        self.assertEqual(rc, 7)

    def test_signal_death_maps_to_128_plus_signum(self):
        rc, _, _ = self.run_wrapper(user_frame("SIGSELF"))
        self.assertEqual(rc, 137)  # SIGKILL


class FailOpenTest(WrapperHarness):
    def test_unknown_frame_type_is_drift_telemetry_mesh_stays_up(self):
        # Vocabulary drift (engine grew a new frame type, cf. the live
        # 2.1.206 command_lifecycle finding): logged once, passed through,
        # mesh keeps running.
        script = user_frame("BADFRAME") + user_frame("BADFRAME") + user_frame("ECHO after")
        rc, out, _ = self.run_wrapper(script)
        self.assertEqual(rc, 0)
        self.assertIn(b'"telepathy"', out)  # forwarded verbatim
        self.assertIn(b'"after"', out)
        log = self.wrapper_log_text()
        self.assertIn("wire drift", log)
        self.assertIn("telepathy", log)
        self.assertEqual(log.count("wire drift"), 1, "once per type, not per frame")
        self.assertNotIn("mesh disabled", log)

    def test_structural_garbage_disables_mesh_but_proxying_continues(self):
        script = user_frame("GARBAGE") + user_frame("ECHO after")
        rc, out, _ = self.run_wrapper(script)
        self.assertEqual(rc, 0)
        self.assertIn(b"this is not stream-json", out)  # still forwarded verbatim
        self.assertIn(b'"after"', out)  # session kept working
        self.assertIn("mesh disabled", self.wrapper_log_text())


class SocketpairSpawnTest(WrapperHarness):
    """The real extension spawn shape: Node/libuv child stdio is a Unix-domain
    socketpair, not a pipe — S_ISFIFO is false on it. The gate must still
    activate (this is the live 2026-07-10 VS Code finding)."""

    def test_socketpair_stdio_activates_mesh_with_framing(self):
        argv_log = os.path.join(self.tmp, "engine-argv.json")
        rc, out, err = self.run_wrapper_socketpair(
            user_frame("ECHO hi"), FAKE_ENGINE_ARGV_LOG=argv_log
        )
        self.assertEqual(rc, 0, err)
        self.assertIn(b'"hi"', out)
        self.assertIn("mesh activated", self.wrapper_log_text())
        with open(argv_log) as f:
            self.assertIn("--append-system-prompt", json.load(f))

    def test_socketpair_output_matches_direct_engine_run(self):
        script = user_frame("ECHO one") + user_frame("SLOWFRAME") + user_frame("COMPACT")
        rc_direct, out_direct, _ = self.run_engine_directly(script)
        rc_sock, out_sock, err = self.run_wrapper_socketpair(script)
        self.assertEqual(rc_direct, 0)
        self.assertEqual(rc_sock, 0, err)
        self.assertEqual(out_sock, out_direct)


class ActivationGateTest(WrapperHarness):
    def test_one_shot_spawn_passes_through_with_no_mesh_side_effects(self):
        p = subprocess.Popen(
            [sys.executable, WRAPPER, sys.executable, FAKE_ENGINE, "auth", "status", "--json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(json.loads(out), {"ok": True})
        self.assertFalse(os.path.exists(self.mesh_home), "gate must leave no trace")

    def test_kill_switch_env_passes_through(self):
        rc, out, _ = self.run_wrapper(user_frame("ECHO hi"), CLAUDE_MESH_DISABLE="1")
        self.assertEqual(rc, 0)
        self.assertIn(b'"hi"', out)
        self.assertFalse(os.path.exists(self.mesh_home))

    def test_nested_spawn_recursion_guard_passes_through(self):
        rc, out, _ = self.run_wrapper(user_frame("ECHO hi"), CLAUDE_MESH_WRAPPED="1")
        self.assertEqual(rc, 0)
        self.assertIn(b'"hi"', out)
        self.assertFalse(os.path.exists(self.mesh_home))


class EngineResolutionTest(WrapperHarness):
    def _fake_claude_script(self):
        path = os.path.join(self.tmp, "fake-claude")
        with open(path, "w") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_ENGINE))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_shim_mode_resolves_engine_from_config(self):
        os.makedirs(self.mesh_home)
        with open(os.path.join(self.mesh_home, "config.json"), "w") as f:
            json.dump({"claude_binary": self._fake_claude_script()}, f)
        p = subprocess.Popen(
            [sys.executable, WRAPPER, "auth", "status", "--json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(json.loads(out), {"ok": True})

    def test_shim_mode_without_claude_binary_errors_actionably(self):
        p = subprocess.Popen(
            [sys.executable, WRAPPER, "auth", "status"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 127)
        self.assertIn(b"claude_binary", err)

    def test_shim_reentry_loop_is_cut_by_depth_guard(self):
        # A generated console-script shim (uv/brew) re-entering the wrapper
        # defeats the realpath self-check; the exec-depth guard must cut the
        # loop instead of exec-looping forever.
        shim = os.path.join(self.tmp, "claude-agent-mesh-wrapper")
        with open(shim, "w") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, WRAPPER))
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IXUSR)
        os.makedirs(self.mesh_home)
        with open(os.path.join(self.mesh_home, "config.json"), "w") as f:
            json.dump({"claude_binary": shim}, f)
        p = subprocess.Popen(
            [shim, "auth", "status"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 125)
        self.assertIn(b"recursion", err)

    def test_double_wrap_is_warned_but_spawn_unaltered(self):
        # claudeProcessWrapper pointed at the terminal PATH shim stacks two
        # engines: the outer wrapper sees an executable with the engine's own
        # basename where the argv contract says the first arg is a flag.
        # Warn only — the spawn must still go through unaltered.
        outer = self._fake_claude_script()
        stray_dir = os.path.join(self.tmp, "native-binary")
        os.makedirs(stray_dir)
        stray = os.path.join(stray_dir, os.path.basename(outer))
        with open(stray, "w") as f:
            f.write("#!/bin/sh\nexit 99\n")
        os.chmod(stray, os.stat(stray).st_mode | stat.S_IXUSR)
        p = subprocess.Popen(
            [sys.executable, WRAPPER, outer, stray, "auth", "status", "--json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(json.loads(out), {"ok": True})
        self.assertIn(b"double-wrapped", err)

    def test_self_shadowing_engine_path_is_refused(self):
        os.makedirs(self.mesh_home)
        with open(os.path.join(self.mesh_home, "config.json"), "w") as f:
            json.dump({"claude_binary": WRAPPER}, f)
        p = subprocess.Popen(
            [sys.executable, WRAPPER, "auth", "status"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 125)
        self.assertIn(b"recursion", err)


if __name__ == "__main__":
    unittest.main()
