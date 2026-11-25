#!/usr/bin/env python3
"""Fake `agy` for unit tests. Behaviour via FAKE_AGY_MODE:
  ok        emit init + a tool step + result SUCCESS; write files from FAKE_AGY_WRITE ("path=content;path2=content")
  fail      emit init then exit 1 with an AGY_ERROR line
  sleep     emit init then sleep FAKE_AGY_SLEEP seconds (default 30) silently
  timeout   emit init, AGY_ERROR print_timeout, exit 3
  denied    result SUCCESS with empty response and denied_actions
  swarm     like ok, plus coordinator swarm/read_board tool steps for workers FAKE_AGY_WORKERS ("a,b")
If --log-file is given, a line like agy's real `Resolved active tags: [jetski_mode_auto ...]` is written there.
Every invocation's argv is appended to $FAKE_AGY_ARGV_LOG (one JSON line) if set.
"""
import json
import os
import sys
import time
import uuid

argv = sys.argv[1:]
if os.environ.get("FAKE_AGY_ARGV_LOG"):
    with open(os.environ["FAKE_AGY_ARGV_LOG"], "a") as fh:
        fh.write(json.dumps(argv) + "\n")
mode = os.environ.get("FAKE_AGY_MODE", "ok")
cid = argv[argv.index("--conversation") + 1] if "--conversation" in argv else str(uuid.uuid4())


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if "--log-file" in argv:
    with open(argv[argv.index("--log-file") + 1], "a") as fh:
        m = argv[argv.index("--mode") + 1] if "--mode" in argv else "auto"
        fh.write("I1125 monitoring.go:411] [Sonar] Resolved active tags: [jetski_mode_%s policy_guardian]\n"
                 % ("default" if m == "accept-edits" else "auto"))
emit({"event": "init", "conversation_id": cid, "init": {"model": "fake", "cwd": os.getcwd(), "permission_mode": "request-review"}})
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_AGY_SLEEP", "30")))
    sys.exit(0)
if mode == "fail":
    sys.stderr.write('AGY_ERROR: {"short_error":"model exploded","status":"INTERNAL","reason":"x"}\n')
    sys.exit(1)
if mode == "timeout":
    sys.stderr.write('AGY_ERROR: {"short_error":"print timeout after 5s","status":"DEADLINE_EXCEEDED","reason":"print_timeout"}\n')
    emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR", "response": "partial"}})
    sys.exit(3)
for spec in filter(None, os.environ.get("FAKE_AGY_WRITE", "").split(";")):
    path, content = spec.split("=", 1)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content.replace("\\n", "\n"))
emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "tool",
      "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "python3 -m unittest"}, "output": "OK"}}})
emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 3, "state": "ERROR", "step_type": "tool",
      "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "curl x"},
      "error": {"type": "TOOL_ERROR", "message": "Permission denied for command(curl x). Matches user-configured deny rule."}}}})
if mode == "swarm":
    workers = [w for w in os.environ.get("FAKE_AGY_WORKERS", "a,b").split(",") if w]
    i = 10
    for w in workers + ["verifier"]:
        out = {"status": "added", "name": w, "model": "fake-model", "conversation_id": "conv-" + w}
        emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": i, "state": "DONE",
              "step_type": "tool", "tool_name": "swarm", "tool_info": {"name": "swarm", "parameters": {"action": "add", "name": w},
              "output": "Created At: x\nCompleted At: y\n" + json.dumps(out)}}})
        i += 1
    msgs = [{"seq": n + 1, "agent": w, "text": "@coordinator [done] %s finished" % w} for n, w in enumerate(workers)]
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": i, "state": "DONE", "step_type": "tool",
          "tool_name": "read_board", "tool_info": {"name": "read_board", "parameters": {"since": 0},
          "output": "Created At: x\n" + json.dumps({"messages": msgs, "head": len(msgs)})}}})
    status = {"title": "fake swarm", "members": [{"name": "coordinator", "role": "coordinator", "state": "working"}] +
              [{"name": w, "role": "worker", "model": "fake-model", "state": "waiting", "killed": False} for w in workers] +
              [{"name": "verifier", "role": "worker", "model": "fake-model", "state": "working", "killed": False}]}
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": i + 1, "state": "DONE", "step_type": "tool",
          "tool_name": "swarm", "tool_info": {"name": "swarm", "parameters": {"action": "status"}, "output": json.dumps(status)}}})
if mode == "denied":
    emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "",
          "denied_actions": [{"action": "command", "display_name": "Bash"}]}})
    sys.exit(0)
emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 4, "state": "DONE", "step_type": "agent_response", "text_delta": "All done."}})
emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "All done.", "usage": {"total_tokens": 10}}})
