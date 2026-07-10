"""`claude-agent-mesh` send/peers CLI tests (T3/D10): real subprocess, real files."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests import REPO_ROOT
from tests.harness import MESH_CLI

SENDER_SID = "11111111-1111-4111-8111-111111111111"
PEER_SID = "22222222-2222-4222-8222-222222222222"


class CliHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mesh-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mesh_home = os.path.join(self.tmp, "agent-mesh")
        os.makedirs(self.mesh_home)
        self.identity_file = os.path.join(self.tmp, "identity.json")
        with open(self.identity_file, "w") as f:
            json.dump(
                {"session_id": SENDER_SID, "title": "Sender tab", "cwd": "/PLACEHOLDER"}, f
            )
        self.plant_presence(SENDER_SID, title="Sender tab")
        self.plant_presence(PEER_SID, title="Peer tab")

    def plant_presence(self, sid, title="untitled"):
        d = os.path.join(self.mesh_home, "presence")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".json"), "w") as f:
            json.dump(
                {
                    "session_id": sid,
                    "title": title,
                    "cwd": "/PLACEHOLDER/repo",
                    "model": "claude-opus-4-8",
                    "last_heartbeat": time.time(),
                },
                f,
            )

    def remove_presence(self, sid):
        os.unlink(os.path.join(self.mesh_home, "presence", sid + ".json"))

    def write_config(self, values):
        with open(os.path.join(self.mesh_home, "config.json"), "w") as f:
            json.dump(values, f)

    def mesh(self, *argv, stdin=None, identity=True):
        env = os.environ.copy()
        env["CLAUDE_MESH_HOME"] = self.mesh_home
        if identity:
            env["CLAUDE_MESH_SESSION_FILE"] = self.identity_file
        else:
            env.pop("CLAUDE_MESH_SESSION_FILE", None)
        p = subprocess.run(
            [sys.executable, MESH_CLI] + list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return p.returncode, p.stdout, p.stderr

    def inbox_new(self, sid=PEER_SID):
        d = os.path.join(self.mesh_home, "inbox", sid, "new")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def read_new_message(self, sid=PEER_SID):
        names = self.inbox_new(sid)
        self.assertEqual(len(names), 1)
        with open(os.path.join(self.mesh_home, "inbox", sid, "new", names[0])) as f:
            return names[0], f.read()


class SendTest(CliHarness):
    def test_send_stamps_validates_and_lands_atomically_in_new(self):
        rc, out, err = self.mesh(
            "send", "--to", PEER_SID, "--subject", "rebase needed",
            "--body", "I touched wire.py on main — rebase.",
        )
        self.assertEqual(rc, 0, err)
        message_id = out.strip()
        name, text = self.read_new_message()
        self.assertIn(message_id, name)

        sys.path.insert(0, REPO_ROOT)
        import mesh_runtime as rt

        headers, body = rt.parse_message(text)
        self.assertEqual(headers["message-id"], message_id)
        self.assertEqual(headers["from"], "Sender tab <<%s>>" % SENDER_SID)
        self.assertEqual(headers["to"], "Peer tab <<%s>>" % PEER_SID)
        self.assertEqual(headers["subject"], "rebase needed")
        self.assertIn("rebase", body)
        time.strptime(headers["date"][:19], "%Y-%m-%dT%H:%M:%S")  # stamped, parseable
        # Maildir discipline: nothing left in tmp/.
        self.assertEqual(os.listdir(os.path.join(self.mesh_home, "inbox", PEER_SID, "tmp")), [])

    def test_body_from_stdin(self):
        rc, _, err = self.mesh(
            "send", "--to", PEER_SID, "--subject", "s", stdin="piped body\n"
        )
        self.assertEqual(rc, 0, err)
        _, text = self.read_new_message()
        self.assertIn("piped body", text)

    def test_thread_and_reply_headers(self):
        rc, _, _ = self.mesh(
            "send", "--to", PEER_SID, "--subject", "s", "--body", "b",
            "--thread-id", "PROJ-42", "--in-reply-to", "abc-123",
        )
        self.assertEqual(rc, 0)
        _, text = self.read_new_message()
        self.assertIn("thread-id: PROJ-42", text)
        self.assertIn("in-reply-to: abc-123", text)

    def test_newline_in_header_value_rejected_before_any_write(self):
        rc, _, err = self.mesh(
            "send", "--to", PEER_SID, "--subject", "sneaky\nfrom: forged", "--body", "b"
        )
        self.assertEqual(rc, 2)
        self.assertIn("invalid message", err)
        self.assertEqual(self.inbox_new(), [])

    def test_oversized_body_rejected(self):
        self.write_config({"send_body_cap_bytes": 64})
        rc, _, err = self.mesh("send", "--to", PEER_SID, "--subject", "s", "--body", "x" * 65)
        self.assertEqual(rc, 2)
        self.assertEqual(self.inbox_new(), [])

    def test_dead_peer_fails_visibly(self):
        self.remove_presence(PEER_SID)
        rc, _, err = self.mesh("send", "--to", PEER_SID, "--subject", "s", "--body", "b")
        self.assertEqual(rc, 3)
        self.assertIn("gone", err)
        self.assertIn("claude-agent-mesh peers", err)
        self.assertEqual(self.inbox_new(), [])

    def test_invalid_recipient_id_rejected(self):
        rc, _, _ = self.mesh("send", "--to", "../escape", "--subject", "s", "--body", "b")
        self.assertEqual(rc, 2)

    def test_no_identity_is_actionable(self):
        rc, _, err = self.mesh(
            "send", "--to", PEER_SID, "--subject", "s", "--body", "b", identity=False
        )
        self.assertEqual(rc, 5)
        self.assertIn("CLAUDE_MESH_SESSION_FILE", err)

    def test_rate_limit_refuses_with_retry_hint(self):
        self.write_config({"send_bucket_capacity": 2, "send_bucket_refill_seconds": 60})
        for i in range(2):
            rc, _, err = self.mesh(
                "send", "--to", PEER_SID, "--subject", "s%d" % i, "--body", "b"
            )
            self.assertEqual(rc, 0, err)
        rc, _, err = self.mesh("send", "--to", PEER_SID, "--subject", "s3", "--body", "b")
        self.assertEqual(rc, 4)
        self.assertIn("rate limited", err)
        self.assertIn("retry after", err)
        self.assertIn("coalesce", err)
        self.assertEqual(len(self.inbox_new()), 2, "refused send must write nothing")


class PeersTest(CliHarness):
    def test_roster_lists_peers_and_marks_self(self):
        rc, out, err = self.mesh("peers")
        self.assertEqual(rc, 0, err)
        self.assertIn(PEER_SID, out)
        self.assertIn("Peer tab", out)
        self.assertIn(SENDER_SID + " (you)", out)

    def test_json_roster(self):
        rc, out, _ = self.mesh("peers", "--json")
        self.assertEqual(rc, 0)
        sids = {i["session_id"] for i in json.loads(out)}
        self.assertEqual(sids, {SENDER_SID, PEER_SID})

    def test_empty_roster(self):
        self.remove_presence(SENDER_SID)
        self.remove_presence(PEER_SID)
        rc, out, _ = self.mesh("peers")
        self.assertEqual(rc, 0)
        self.assertIn("no live peers", out)


if __name__ == "__main__":
    unittest.main()
