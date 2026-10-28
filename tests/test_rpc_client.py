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


# Measured on omp 18.12.6: after a prompt_result whose session has not settled, background work
# can wake the agent again. A plain prompt then fails with "Agent is already processing"; one
# sent with streamingBehavior "followUp" is queued behind the run and still gets its result.
BUSY_OMP = textwrap.dedent("""
    import json, sys
    def send(frame):
        sys.stdout.write(json.dumps(frame) + "\\n"); sys.stdout.flush()
    send({"type": "ready"})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") != "prompt":
            send({"type": "response", "id": msg.get("id"), "command": msg.get("type"),
                  "success": True, "data": {}})
            continue
        if msg.get("streamingBehavior") != "followUp":
            send({"type": "response", "id": msg["id"], "command": "prompt", "success": False,
                  "error": "Agent is already processing. Use steer() or followUp() to queue "
                           "messages, or wait for completion."})
            continue
        send({"type": "response", "id": msg["id"], "command": "prompt", "success": True})
        send({"type": "message_update", "assistantMessageEvent":
              {"type": "text_delta", "delta": "queued and answered"}})
        send({"type": "prompt_result", "id": msg["id"], "status": "completed"})
""")


def test_a_prompt_while_omp_is_still_busy_waits_its_turn(tmp_path):
    """Seen live: "Agent error: prompt failed: Agent is already processing" on a plain message,
    because a background job had woken the agent after the last turn ended."""
    script = tmp_path / "omp.py"
    script.write_text(BUSY_OMP)
    client = AgentClient(command=sys.executable, args=[str(script)], startup_timeout=10)
    try:
        result = client.prompt("continue", timeout=10)
    finally:
        client.close()
    assert result.text == "queued and answered"


def test_an_advisor_card_becomes_an_advisor_update():
    """omp 18.12.6 injects the advisor's review as its own record: role custom, customType
    advisor, the notes in details (read from the bundled source and a session transcript)."""
    seen = []
    client = AgentClient(on_update=seen.append)
    client._dispatch({"type": "message_end", "messageId": "m7", "message": {
        "role": "custom", "customType": "advisor", "display": True, "content": "<advisory>…",
        "details": {"notes": [{"note": "check the exit code", "severity": "concern"},
                              {"severity": "nit"}]}}})
    client._dispatch({"type": "message_end", "message": {
        "role": "custom", "customType": "irc:incoming", "details": {}}})
    assert [(update.kind, update.raw["notes"]) for update in seen] == [
        ("advisor", [{"note": "check the exit code", "severity": "concern"}])]


def test_a_failing_update_callback_is_logged_not_swallowed(caplog):
    """The reader thread must survive a broken callback, and the failure must be visible."""
    def broken(update):
        raise ValueError("renderer blew up")

    client = AgentClient(on_update=broken)
    with caplog.at_level("ERROR", logger="ompwire.rpc"):
        client._dispatch({"type": "message_update", "messageId": "m1",
                          "assistantMessageEvent": {"type": "text_delta", "delta": "hi"}})
    assert any("update callback failed" in record.getMessage() and record.exc_info
               for record in caplog.records)
