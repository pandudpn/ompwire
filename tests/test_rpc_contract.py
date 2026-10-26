"""Contract tests against the live `omp --mode rpc-ui` agent.

The frame shapes ompwire relies on are omp's, not ours, so these run the real binary and are
never mocked: the point is to catch upstream drift. They skip when `omp` is absent (CI without
the agent). The turn tests need a configured model and take up to a few minutes.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import pytest

from ompwire.rpc import AgentClient

pytestmark = pytest.mark.skipif(shutil.which("omp") is None, reason="omp not installed")

TURN_TIMEOUT = 300.0


@pytest.fixture()
def workdir(tmp_path):
    return tmp_path


def client_for(workdir, *extra, on_dialog=None):
    return AgentClient(args=("--mode", "rpc-ui", *extra), cwd=str(workdir), on_dialog=on_dialog)


def test_ready_and_protocol_v2_are_negotiated(workdir):
    """start() waits for `ready`, then asks for v2 (chunked frames), the ask dialog and the
    subagent frames; each must be accepted, or a chat silently loses that feature."""
    with client_for(workdir, "--no-session") as client:
        client.start()
        assert client.request("negotiate_protocol", {"protocolVersion": 2}) == {"protocolVersion": 2}
        assert client.subagent_frames, "set_subagent_subscription was refused"
        client.request("set_ask_dialog", {"enabled": True})  # raises if omp refuses it


def test_resuming_an_unknown_session_exits_nonzero(workdir):
    done = subprocess.run(["omp", "--mode", "rpc-ui", "--resume",
                           "00000000-dead-beef-0000-000000000000"],
                          cwd=workdir, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode != 0
    assert "not found" in (done.stderr + done.stdout).lower()


def test_a_changing_command_asks_with_the_options_ompwire_maps(workdir):
    """Approvals arrive as `select` with omp's labels; the chat's Allow / Allow for this session /
    Deny buttons map onto exactly these three, and Deny must stop the command."""
    seen: list[dict] = []

    def on_dialog(frame):
        if frame.get("method") == "select":
            seen.append(frame)
            return {"value": "Deny"}
        return None

    with client_for(workdir, "--no-session", "--approval-mode=write", on_dialog=on_dialog) as client:
        client.prompt("Run exactly this with the bash tool once: touch probe.txt . Do nothing else.",
                      timeout=TURN_TIMEOUT)
    assert seen, "a command that changes files must ask under --approval-mode=write"
    assert seen[0]["options"] == ["Approve", "Approve bash for this session", "Deny"]
    assert "Allow tool: bash" in seen[0]["title"]
    assert not (workdir / "probe.txt").exists(), "a denied command must not run"


def test_steer_is_accepted_mid_turn_and_abort_ends_it_as_aborted(workdir):
    with client_for(workdir, "--no-session", "--approval-mode=yolo") as client:
        client.start()
        outcome: dict = {}

        def run():
            try:
                outcome["result"] = client.prompt(
                    "Run `sleep 60` with the bash tool, then reply with the word done.",
                    timeout=TURN_TIMEOUT)
            except Exception as exc:  # the assertions below name what went wrong
                outcome["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + TURN_TIMEOUT
        while client._turn is None and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(3)
        steered, reason = client.steer("Also say the word steered at the end.")
        assert steered == "injected", reason
        client.abort()
        worker.join(TURN_TIMEOUT)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"].stop_reason == "cancelled"
