"""The RPC client against a stand-in omp that replays the frame order measured on omp 18.12.6."""

from __future__ import annotations

import sys
import textwrap

from ompwire.rpc import AgentClient

# A local command (/compact) is acknowledged with agentInvoked:false, and its answer arrives
# afterwards as command_output. The pause is what made the answer land in the next turn.
FAKE_OMP = textwrap.dedent("""
    import json, sys, time
    def send(frame):
        sys.stdout.write(json.dumps(frame) + "\\n"); sys.stdout.flush()
    send({"type": "ready"})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") != "prompt":
            send({"type": "response", "id": msg.get("id"), "command": msg.get("type"),
                  "success": True, "data": {}})
            continue
        if msg["message"] == "/compact":
            send({"type": "response", "id": msg["id"], "command": "prompt", "success": True,
                  "data": {"agentInvoked": False}})
            time.sleep(0.5)
            send({"type": "command_output", "text": "Compaction complete."})
        else:
            send({"type": "response", "id": msg["id"], "command": "prompt", "success": True,
                  "data": {}})
            send({"type": "message_update", "assistantMessageEvent":
                  {"type": "text_delta", "delta": "hello"}})
            send({"type": "prompt_result", "id": msg["id"], "status": "completed"})
""")


def test_a_local_command_answers_in_its_own_turn(tmp_path):
    script = tmp_path / "omp.py"
    script.write_text(FAKE_OMP)
    client = AgentClient(command=sys.executable, args=[str(script)], startup_timeout=10)
    try:
        compact = client.prompt("/compact", timeout=10)
        after = client.prompt("hi", timeout=10)
    finally:
        client.close()
    assert compact.text == "Compaction complete."
    assert "Compaction" not in after.text
