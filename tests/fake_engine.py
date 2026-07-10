#!/usr/bin/env python3
"""A deterministic stand-in engine speaking fixture-shaped stream-json.

Not a mock: the integration tests spawn the real wrapper as a subprocess
around this real child process over real pipes. Every output is a pure
function of the input script, so a wrapper run can be diffed byte-for-byte
against a direct run.

Commands (the text of an incoming user frame):
  ECHO <text>    -> one assistant frame carrying <text>
  TITLE <text>   -> a control_response carrying a session title
  COMPACT        -> status compacting + compact_boundary + summary user frame
  STDERR <text>  -> <text> on stderr
  BADFRAME       -> an unknown-type line on stdout (vocabulary drift)
  GARBAGE        -> a non-JSON line on stdout (structural drift)
  SLOWFRAME      -> an assistant frame dribbled out in two flushes
  SIGSELF        -> SIGKILL own process (crash simulation)
  EXIT <n>       -> clean exit with code n

  ENV <name>     -> one assistant frame carrying that environment variable
  SLEEP <s>      -> busy for <s> seconds (not reading stdin), then "slept"
  PERMISSION     -> emit a stdio permission control_request and block until
                    the matching control_response arrives, then "granted"

Environment:
  FAKE_ENGINE_STDIN_LOG  append every raw stdin line here (delivery evidence)
  FAKE_ENGINE_SID        session id for the init frame (default: fixture sid)
  FAKE_ENGINE_ARGV_LOG   write own argv as JSON here at session start
"""

import json
import os
import signal
import sys
import time

SID = os.environ.get("FAKE_ENGINE_SID", "11111111-1111-4111-8111-111111111111")
STDIN_LOG = os.environ.get("FAKE_ENGINE_STDIN_LOG")

_counter = [0]


def _uuid():
    _counter[0] += 1
    return "eeeeeeee-0000-4000-8000-%012d" % _counter[0]


def emit(frame):
    sys.stdout.buffer.write(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def emit_assistant(text):
    emit(
        {
            "type": "assistant",
            "message": {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            "parent_tool_use_id": None,
            "session_id": SID,
            "uuid": _uuid(),
        }
    )


def log_stdin(raw):
    if STDIN_LOG:
        with open(STDIN_LOG, "ab") as f:
            f.write(raw)
            f.flush()


def one_shot():
    sys.stdout.write('{"ok": true}\n')
    sys.stdout.flush()
    return 0


def session():
    argv_log = os.environ.get("FAKE_ENGINE_ARGV_LOG")
    if argv_log:
        with open(argv_log, "w") as f:
            json.dump(sys.argv, f)
    emit(
        {
            "type": "system",
            "subtype": "init",
            "cwd": "/PLACEHOLDER/workspace/project",
            "session_id": SID,
            "tools": ["Bash"],
            "mcp_servers": [],
            "model": "claude-opus-4-8",
            "permissionMode": "default",
            "apiKeySource": "none",
            "slash_commands": [],
            "version": "2.1.206",
            "uuid": _uuid(),
        }
    )
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            return 0
        log_stdin(raw)
        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        if frame.get("type") == "control_request":
            emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": frame.get("request_id"),
                        "response": {},
                    },
                }
            )
            continue
        if frame.get("type") != "user":
            continue
        content = frame.get("message", {}).get("content") or [{}]
        text = content[0].get("text", "")
        if text.startswith("ECHO "):
            emit_assistant(text[5:])
        elif text.startswith("ENV "):
            emit_assistant(os.environ.get(text[4:], "<unset>"))
        elif text.startswith("SLEEP "):
            time.sleep(float(text[6:]))
            emit_assistant("slept")
        elif text == "PERMISSION":
            emit(
                {
                    "type": "control_request",
                    "request_id": "perm_1",
                    "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
                }
            )
            while True:
                raw2 = sys.stdin.buffer.readline()
                if not raw2:
                    return 0
                log_stdin(raw2)
                try:
                    mid = json.loads(raw2)
                except ValueError:
                    continue
                if mid.get("type") == "control_response":
                    break
            emit_assistant("granted")
        elif text.startswith("TITLE "):
            emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": "req_title",
                        "response": {"title": text[6:]},
                    },
                }
            )
        elif text == "COMPACT":
            emit({"type": "system", "subtype": "status", "status": "compacting", "session_id": SID, "uuid": _uuid()})
            emit(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"trigger": "manual", "pre_tokens": 1},
                    "session_id": SID,
                    "uuid": _uuid(),
                }
            )
            emit(
                {
                    "type": "user",
                    "uuid": _uuid(),
                    "session_id": SID,
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": [{"type": "text", "text": "summary"}]},
                    "isCompactSummary": True,
                }
            )
        elif text.startswith("STDERR "):
            sys.stderr.write(text[7:] + "\n")
            sys.stderr.flush()
        elif text == "BADFRAME":
            sys.stdout.buffer.write(b'{"type":"telepathy","payload":"drift"}\n')
            sys.stdout.buffer.flush()
        elif text == "GARBAGE":
            sys.stdout.buffer.write(b"this is not stream-json\n")
            sys.stdout.buffer.flush()
        elif text == "SLOWFRAME":
            half = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "slow"}]}, "session_id": SID, "uuid": _uuid()}).encode("utf-8")
            sys.stdout.buffer.write(half[: len(half) // 2])
            sys.stdout.buffer.flush()
            time.sleep(0.4)
            sys.stdout.buffer.write(half[len(half) // 2 :] + b"\n")
            sys.stdout.buffer.flush()
        elif text == "SIGSELF":
            os.kill(os.getpid(), signal.SIGKILL)
        elif text.startswith("EXIT "):
            return int(text[5:])
        else:
            emit_assistant("ok")


if __name__ == "__main__":
    if "--input-format" in sys.argv:
        sys.exit(session())
    sys.exit(one_shot())
