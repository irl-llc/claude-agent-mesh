"""GC leadership + sweeps (T2/duty 4): real wrappers, real flock, real kill -9."""

import json
import os
import time
import unittest

from tests.harness import WrapperHarness

OTHER_SID = "22222222-2222-4222-8222-222222222222"
ORPHAN_SID = "99999999-9999-4999-8999-999999999999"


class GcHarness(WrapperHarness):
    def write_config(self, values):
        os.makedirs(self.mesh_home, exist_ok=True)
        with open(os.path.join(self.mesh_home, "config.json"), "w") as f:
            json.dump(values, f)

    def plant_presence(self, sid, last_heartbeat):
        d = os.path.join(self.mesh_home, "presence")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".json"), "w") as f:
            json.dump({"session_id": sid, "last_heartbeat": last_heartbeat}, f)

    def plant_inbox(self, sid, age_seconds):
        base = os.path.join(self.mesh_home, "inbox", sid)
        for sub in ("tmp", "new", "cur"):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        msg = os.path.join(base, "new", "0001.msg.md")
        with open(msg, "w") as f:
            f.write("---\nsubject: old\n---\n\nbody\n")
        old = time.time() - age_seconds
        for dirpath, dirnames, filenames in os.walk(base, topdown=False):
            for entry in [dirpath] + [os.path.join(dirpath, n) for n in filenames]:
                os.utime(entry, (old, old))
        os.utime(base, (old, old))
        return base


class SweepTest(GcHarness):
    def test_stale_presence_swept_fresh_and_own_kept(self):
        self.write_config({"gc_tick_seconds": 0.3, "gc_jitter_seconds": 0.1,
                           "presence_stale_seconds": 3600})
        now = time.time()
        self.plant_presence("stale-peer", now - 9999)
        self.plant_presence("fresh-peer", now + 3600)  # deterministically fresh
        session = self.live_session()
        self.wait_for(self.read_presence, message="own presence")
        self.wait_for(
            lambda: not os.path.exists(self.presence_path("stale-peer")),
            message="stale presence sweep",
        )
        self.assertTrue(os.path.exists(self.presence_path("fresh-peer")))
        self.assertIsNotNone(self.read_presence(), "own presence must survive sweeps")
        session.close()

    def test_orphan_inbox_reaped_only_after_retention(self):
        self.write_config({"gc_tick_seconds": 0.3, "gc_jitter_seconds": 0.1,
                           "orphan_retention_seconds": 3600})
        expired = self.plant_inbox(ORPHAN_SID, age_seconds=7200)
        recent = self.plant_inbox("88888888-8888-4888-8888-888888888888", age_seconds=10)
        session = self.live_session()
        self.wait_for(lambda: not os.path.exists(expired), message="orphan inbox reap")
        self.assertTrue(os.path.exists(recent), "resumable inbox must be retained")
        session.close()

    def test_inbox_with_presence_is_not_an_orphan(self):
        self.write_config({"gc_tick_seconds": 0.3, "gc_jitter_seconds": 0.1,
                           "orphan_retention_seconds": 1})
        inbox = self.plant_inbox(OTHER_SID, age_seconds=7200)
        self.plant_presence(OTHER_SID, time.time() + 3600)
        session = self.live_session()
        self.wait_for(self.read_presence, message="own presence")
        time.sleep(1.0)  # several sweep cycles
        self.assertTrue(os.path.exists(inbox), "presence shields an inbox from reaping")
        session.close()

    def test_old_logs_pruned(self):
        self.write_config({"gc_tick_seconds": 0.3, "gc_jitter_seconds": 0.1,
                           "log_retention_seconds": 3600})
        log_dir = os.path.join(self.mesh_home, "log")
        os.makedirs(log_dir)
        old_log = os.path.join(log_dir, "wrapper-old.log")
        with open(old_log, "w") as f:
            f.write("ancient\n")
        ancient = time.time() - 7200
        os.utime(old_log, (ancient, ancient))
        session = self.live_session()
        self.wait_for(lambda: not os.path.exists(old_log), message="log prune")
        session.close()


class LeaderFailoverTest(GcHarness):
    def test_kernel_released_lock_fails_over_and_survivor_sweeps(self):
        self.write_config({"gc_tick_seconds": 0.3, "gc_jitter_seconds": 0.1,
                           "presence_stale_seconds": 3600})
        # A spawns first and takes leadership at startup.
        a = self.live_session()
        self.wait_for(
            lambda: "leadership acquired" in self.wrapper_log_text(),
            message="A's leadership",
        )
        b = self.live_session(FAKE_ENGINE_SID=OTHER_SID,
                              FAKE_ENGINE_STDIN_LOG=os.path.join(self.tmp, "b-stdin.log"))
        self.wait_for(lambda: self.read_presence(OTHER_SID), message="B's presence")

        a.proc.kill()  # kill -9 the leader: the kernel releases the flock
        a.proc.wait()
        self.plant_presence("stale-peer", time.time() - 9999)
        self.wait_for(
            lambda: not os.path.exists(self.presence_path("stale-peer")),
            message="survivor's sweep after failover",
        )
        self.assertGreaterEqual(self.wrapper_log_text().count("leadership acquired"), 2)
        # A crashed, so its presence lingers until staleness — honesty over tidiness.
        self.assertIsNotNone(self.read_presence())
        b.close()


if __name__ == "__main__":
    unittest.main()
