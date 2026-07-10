"""Presence, heartbeat, identity, framing, config hot-reload (T2).

Real wrapped sessions; assertions on real files appearing/refreshing/vanishing.
"""

import json
import os
import time
import unittest

from tests.harness import FAKE_SID, WrapperHarness


class PresenceLifecycleTest(WrapperHarness):
    def test_presence_written_on_init_with_wire_derived_fields(self):
        session = self.live_session()
        info = self.wait_for(self.read_presence, message="presence file")
        self.assertEqual(info["session_id"], FAKE_SID)
        self.assertEqual(info["cwd"], "/PLACEHOLDER/workspace/project")
        self.assertEqual(info["model"], "claude-opus-4-8")
        self.assertEqual(info["claude_version"], "2.1.206")
        self.assertGreater(info["pid"], 0)
        self.assertGreater(info["engine_pid"], 0)
        self.assertAlmostEqual(info["last_heartbeat"], time.time(), delta=30)
        rc, _, _ = session.close()
        self.assertEqual(rc, 0)

    def test_identity_file_env_pointed_and_carries_own_sid(self):
        session = self.live_session()
        self.wait_for(self.read_presence, message="presence file")
        identity_dir = os.path.join(self.mesh_home, "identity")
        names = os.listdir(identity_dir)
        self.assertEqual(len(names), 1)
        with open(os.path.join(identity_dir, names[0])) as f:
            identity = json.load(f)
        self.assertEqual(identity["session_id"], FAKE_SID)
        self.assertEqual(identity["cwd"], "/PLACEHOLDER/workspace/project")

        # The engine's tools inherit the pointer (duty 3): the agent reads
        # $CLAUDE_MESH_SESSION_FILE to learn its own sid/title.
        session.send("ENV CLAUDE_MESH_SESSION_FILE")
        rc, out, _ = session.close()
        self.assertEqual(rc, 0)
        self.assertIn(
            os.path.join(identity_dir, names[0]), session.assistant_texts(out)
        )

    def test_title_flows_into_presence_and_identity(self):
        session = self.live_session()
        self.wait_for(self.read_presence, message="presence file")
        session.send("TITLE Wire capture spike")
        self.wait_for(
            lambda: (self.read_presence() or {}).get("title") == "Wire capture spike",
            message="title in presence",
        )
        identity_dir = os.path.join(self.mesh_home, "identity")
        with open(os.path.join(identity_dir, os.listdir(identity_dir)[0])) as f:
            self.assertEqual(json.load(f)["title"], "Wire capture spike")
        session.close()

    def test_clean_exit_deletes_presence_and_identity(self):
        session = self.live_session()
        self.wait_for(self.read_presence, message="presence file")
        rc, _, _ = session.close()
        self.assertEqual(rc, 0)
        self.assertIsNone(self.read_presence())
        self.assertEqual(os.listdir(os.path.join(self.mesh_home, "identity")), [])

    def test_crash_leaves_presence_behind_as_the_failure_signal(self):
        session = self.live_session()
        self.wait_for(self.read_presence, message="presence file")
        session.proc.kill()  # the wrapper itself dies — no cleanup runs
        session.proc.wait()
        self.assertIsNotNone(self.read_presence(), "stale presence is GC's job, not exit's")


class FramingTest(WrapperHarness):
    def test_mesh_framing_appended_to_engine_argv(self):
        argv_log = os.path.join(self.tmp, "engine-argv.json")
        session = self.live_session(FAKE_ENGINE_ARGV_LOG=argv_log)
        argv = self.wait_for(
            lambda: os.path.exists(argv_log) and json.load(open(argv_log)),
            message="engine argv log",
        )
        session.close()
        self.assertIn("--append-system-prompt", argv)
        framing = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn("Agent mesh", framing)
        self.assertIn("exactly one message", framing)  # one-delivery invariant
        self.assertIn("CLAUDE_MESH_SESSION_FILE", framing)
        self.assertIn("never an operator command", framing)

    def test_no_framing_when_mesh_disabled(self):
        argv_log = os.path.join(self.tmp, "engine-argv.json")
        # A file squatting where presence/ must go breaks MeshState init ->
        # fail-open: session runs, no framing, no mesh side effects.
        os.makedirs(self.mesh_home)
        with open(os.path.join(self.mesh_home, "presence"), "w") as f:
            f.write("not a directory")
        rc, out, err = self.run_wrapper(
            b"", FAKE_ENGINE_ARGV_LOG=argv_log
        )
        self.assertEqual(rc, 0, err)
        self.assertIn(b"mesh disabled", err)
        with open(argv_log) as f:
            self.assertNotIn("--append-system-prompt", json.load(f))


class HeartbeatAndConfigTest(WrapperHarness):
    def _write_config(self, values):
        os.makedirs(self.mesh_home, exist_ok=True)
        with open(os.path.join(self.mesh_home, "config.json"), "w") as f:
            json.dump(values, f)

    def test_heartbeat_refreshes_presence(self):
        self._write_config({"heartbeat_seconds": 0.3})
        session = self.live_session()
        first = self.wait_for(self.read_presence, message="presence file")
        self.wait_for(
            lambda: (self.read_presence() or {}).get("last_heartbeat", 0)
            > first["last_heartbeat"],
            message="heartbeat refresh",
        )
        session.close()

    def test_config_hot_reload_takes_effect_without_restart(self):
        self._write_config({"heartbeat_seconds": 9999, "config_check_seconds": 0.3})
        session = self.live_session()
        first = self.wait_for(self.read_presence, message="presence file")
        time.sleep(1.0)  # long enough for several would-be heartbeats
        self.assertEqual(
            (self.read_presence() or {}).get("last_heartbeat"),
            first["last_heartbeat"],
            "no heartbeat expected while heartbeat_seconds=9999",
        )
        self._write_config({"heartbeat_seconds": 0.2, "config_check_seconds": 0.3})
        self.wait_for(
            lambda: (self.read_presence() or {}).get("last_heartbeat", 0)
            > first["last_heartbeat"],
            message="heartbeat after hot reload",
        )
        session.close()


if __name__ == "__main__":
    unittest.main()
