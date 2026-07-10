"""Contract tests: replay the pinned fixture through the wire adapter.

These are wrapper-regression tests (D7): they prove the adapter still
understands the shapes it was written against, not that the live extension
still emits them — runtime strict validation covers that.
"""

import json
import os
import unittest

from tests import REPO_ROOT
import wire

TESTDATA = os.path.join(REPO_ROOT, "testdata")


def fixture_lines(name):
    with open(os.path.join(TESTDATA, name), "rb") as f:
        return [line for line in f.read().splitlines(True) if line.strip()]


class FixtureReplayTest(unittest.TestCase):
    def test_every_fixture_frame_parses(self):
        for name in ("stdin_frames.jsonl", "stdout_frames.jsonl"):
            for line in fixture_lines(name):
                frame = wire.parse_frame(line)
                self.assertIsInstance(frame, dict, "blank frame in fixture %s" % name)
                self.assertTrue(wire.is_known_type(frame), frame["type"])

    def test_init_info_extracted_from_fixture(self):
        infos = [
            wire.init_info(wire.parse_frame(line))
            for line in fixture_lines("stdout_frames.jsonl")
        ]
        infos = [i for i in infos if i]
        self.assertEqual(len(infos), 1)
        info = infos[0]
        self.assertEqual(info["session_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(info["cwd"], "/PLACEHOLDER/workspace/project")
        self.assertEqual(info["model"], "claude-opus-4-8")
        self.assertEqual(info["version"], wire.FIXTURE_ENGINE_VERSION)

    def test_compact_boundary_detected_after_compacting_status(self):
        frames = [wire.parse_frame(l) for l in fixture_lines("stdout_frames.jsonl")]
        boundaries = [i for i, f in enumerate(frames) if wire.is_compact_boundary(f)]
        self.assertEqual(len(boundaries), 1)
        # The captured ordering: status compacting immediately precedes the boundary.
        before = frames[boundaries[0] - 1]
        self.assertEqual(before.get("subtype"), "status")
        self.assertEqual(before.get("status"), "compacting")

    def test_title_rides_a_control_response(self):
        titles = [
            wire.control_title(wire.parse_frame(l))
            for l in fixture_lines("stdout_frames.jsonl")
        ]
        titles = [t for t in titles if t]
        self.assertEqual(titles, ["PLACEHOLDER Session Title"])

    def test_title_never_read_from_non_control_frames(self):
        frame = wire.parse_frame(
            b'{"type":"user","uuid":"u","session_id":"","parent_tool_use_id":null,'
            b'"message":{"role":"user","content":[{"type":"text","text":"x"}]},"title":"smuggled"}\n'
        )
        self.assertIsNone(wire.control_title(frame))


class FrameSplitterTest(unittest.TestCase):
    def test_odd_chunking_reassembles_fixture_exactly(self):
        raw = b"".join(fixture_lines("stdout_frames.jsonl"))
        for chunk_size in (1, 7, 64, 4096):
            splitter = wire.FrameSplitter()
            out = []
            for i in range(0, len(raw), chunk_size):
                out.extend(splitter.feed(raw[i : i + chunk_size]))
            self.assertEqual(b"".join(out), raw, "chunk_size=%d" % chunk_size)
            self.assertTrue(splitter.at_boundary)

    def test_partial_tail_held_until_newline(self):
        splitter = wire.FrameSplitter()
        self.assertEqual(splitter.feed(b'{"type":"user"'), [])
        self.assertFalse(splitter.at_boundary)
        self.assertEqual(splitter.pending, b'{"type":"user"')
        self.assertEqual(splitter.feed(b"}\n"), [b'{"type":"user"}\n'])
        self.assertTrue(splitter.at_boundary)

    def test_multiple_frames_in_one_chunk(self):
        splitter = wire.FrameSplitter()
        lines = splitter.feed(b"{}\n{}\n{}")
        self.assertEqual(lines, [b"{}\n", b"{}\n"])
        self.assertEqual(splitter.pending, b"{}")


class ParseStrictnessTest(unittest.TestCase):
    def test_blank_line_tolerated(self):
        self.assertIsNone(wire.parse_frame(b"\n"))
        self.assertIsNone(wire.parse_frame(b"   \n"))

    def test_non_json_is_unrecognized(self):
        with self.assertRaises(wire.UnrecognizedFrame):
            wire.parse_frame(b"debug: something leaked onto stdout\n")

    def test_non_object_json_is_unrecognized(self):
        with self.assertRaises(wire.UnrecognizedFrame):
            wire.parse_frame(b'["not", "an", "object"]\n')

    def test_unknown_type_parses_as_drift_not_error(self):
        frame = wire.parse_frame(b'{"type":"telepathy"}\n')
        self.assertEqual(frame["type"], "telepathy")
        self.assertFalse(wire.is_known_type(frame))

    def test_missing_or_nonstring_type_is_unrecognized(self):
        with self.assertRaises(wire.UnrecognizedFrame):
            wire.parse_frame(b'{"no_type":"here"}\n')
        with self.assertRaises(wire.UnrecognizedFrame):
            wire.parse_frame(b'{"type":42}\n')

    def test_live_2_1_206_types_are_known(self):
        for ftype in ("command_lifecycle", "rate_limit_event", "auth_status"):
            self.assertTrue(wire.is_known_type({"type": ftype}), ftype)

    def test_unrecognized_sample_is_capped(self):
        try:
            wire.parse_frame(b"x" * 10000 + b"\n")
        except wire.UnrecognizedFrame as e:
            self.assertLessEqual(len(e.sample), 256)
        else:
            self.fail("expected UnrecognizedFrame")


class BuildUserFrameTest(unittest.TestCase):
    def test_shape_matches_captured_envelope(self):
        fixture_user = None
        for line in fixture_lines("stdin_frames.jsonl"):
            frame = wire.parse_frame(line)
            if frame["type"] == "user":
                fixture_user = frame
                break
        self.assertIsNotNone(fixture_user)

        built_line = wire.build_user_frame("hello mesh")
        self.assertTrue(built_line.endswith(b"\n"))
        built = json.loads(built_line)
        # Key set must match the captured envelope exactly (finding 2).
        self.assertEqual(set(built.keys()), set(fixture_user.keys()))
        self.assertEqual(built["type"], "user")
        self.assertEqual(built["session_id"], "")  # engine assigns it
        self.assertIsNone(built["parent_tool_use_id"])
        self.assertEqual(built["message"]["role"], "user")
        self.assertEqual(
            built["message"]["content"], [{"type": "text", "text": "hello mesh"}]
        )

    def test_frames_get_unique_uuids(self):
        a = json.loads(wire.build_user_frame("a"))
        b = json.loads(wire.build_user_frame("b"))
        self.assertNotEqual(a["uuid"], b["uuid"])

    def test_newlines_in_text_never_break_framing(self):
        line = wire.build_user_frame("line one\nline two\n")
        # Exactly one wire newline: the terminator.
        self.assertEqual(line.count(b"\n"), 1)
        self.assertEqual(
            json.loads(line)["message"]["content"][0]["text"], "line one\nline two\n"
        )


class ActivationArgvTest(unittest.TestCase):
    def _fixture_argv(self, key):
        with open(os.path.join(TESTDATA, "spawn_argv.json")) as f:
            return json.load(f)[key]

    def test_captured_session_argv_activates(self):
        self.assertTrue(
            wire.is_stream_json_session_argv(self._fixture_argv("session_argv"))
        )

    def test_one_shot_spawns_do_not_activate(self):
        for argv in self._fixture_argv("one_shot_argv_examples"):
            self.assertFalse(wire.is_stream_json_session_argv(argv), argv)

    def test_equals_form_activates(self):
        self.assertTrue(wire.is_stream_json_session_argv(["--input-format=stream-json"]))

    def test_dangling_flag_does_not_activate(self):
        self.assertFalse(wire.is_stream_json_session_argv(["--input-format"]))
        self.assertFalse(wire.is_stream_json_session_argv(["--input-format", "text"]))
