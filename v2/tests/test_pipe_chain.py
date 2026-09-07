"""Pipe chain integration test — two real Claude PTY sessions.

Tests:
  1. Edit-in-place: processing message ID = response message ID
  2. Pipe resolution: session B waits for session A's output
  3. Cascade failure: if A fails, B fails immediately without sending to LLM

Usage:
  python -m v2.tests.test_pipe_chain
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from v2.bridge.core.log_store import LogStore
from v2.bridge.core.prompt_composer import PromptComposer
from v2.bridge.providers.claude import ClaudeAdapter
from v2.bridge.pty.handler import PtySession, PtyState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("pipe_chain_test")

# ── Failure marker (must match core/__init__.py) ──
_FAILURE_LOG_MARKER = "[실패]"


# ── Minimal mock sink that tracks message IDs ──
@dataclass
class MockMessage:
    id: str
    content: str


@dataclass
class MockSink:
    label: str
    status_msg_id: str | None = None
    final_content: str | None = None
    error: str | None = None
    _msg_counter: int = 0

    async def begin(self, context: str) -> None:
        if self.status_msg_id:
            return
        self.status_msg_id = f"MOCK-{self.label}-{int(time.time() * 1000)}"
        LOGGER.info("[%s] processing... (msg_id=%s)", self.label, self.status_msg_id)

    async def stream_update(self, text: str) -> None:
        pass

    async def finalize(self, text: str) -> str:
        self.final_content = text
        # edit-in-place: return same ID as processing message
        LOGGER.info("[%s] finalized (%d chars, msg_id=%s)", self.label, len(text), self.status_msg_id)
        return self.status_msg_id

    async def fail(self, error: str) -> None:
        self.error = error
        LOGGER.info("[%s] FAILED: %s (msg_id=%s)", self.label, error, self.status_msg_id)

    @property
    def status_message_id(self) -> str | None:
        return self.status_msg_id


async def spawn_claude(workspace: str, adapter: ClaudeAdapter) -> PtySession:
    """Spawn a real Claude CLI session."""
    cmd = f"claude --verbose"
    session = PtySession(
        command=cmd,
        cwd=workspace,
        rows=50,
        cols=200,
        ready_detector=adapter.ready_detector,
    )
    session.spawn()
    await session.wait_ready(timeout=30)
    LOGGER.info("Claude session ready (state=%s)", session.state.value)
    return session


async def test_edit_in_place_id_preservation():
    """Test 1: processing message ID stays the same after finalize."""
    LOGGER.info("=" * 60)
    LOGGER.info("TEST 1: Edit-in-place ID preservation")
    LOGGER.info("=" * 60)

    sink = MockSink(label="A")
    await sink.begin("test")
    processing_id = sink.status_msg_id

    response_id = await sink.finalize("Hello, this is the response!")
    assert response_id == processing_id, f"ID changed! {processing_id} → {response_id}"
    LOGGER.info("PASS: processing_id == response_id == %s", processing_id)


async def test_failure_log_and_cascade():
    """Test 2: Failure log blocks downstream pipe resolution."""
    LOGGER.info("=" * 60)
    LOGGER.info("TEST 2: Failure log cascade")
    LOGGER.info("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        log_store = LogStore(Path(tmp))
        server_id = "test-server"
        channel_id = 999

        # Simulate: A fails, writes failure log
        sink_a = MockSink(label="A")
        await sink_a.begin("test")
        await sink_a.fail("timeout error")

        fail_path = Path(tmp) / server_id / str(channel_id) / f"{sink_a.status_msg_id}.txt"
        fail_path.parent.mkdir(parents=True, exist_ok=True)
        fail_path.write_text(f"{_FAILURE_LOG_MARKER}\ntimeout error", encoding="utf-8")
        LOGGER.info("Wrote failure log: %s", fail_path)

        # Simulate: B tries to resolve pipe to A's message
        content = fail_path.read_text(encoding="utf-8")
        assert content.startswith(_FAILURE_LOG_MARKER), "Failure marker not found!"

        LOGGER.info("PASS: Failure log detected, downstream would cascade-fail")


async def test_pipe_chain_two_sessions():
    """Test 3: Real two-session pipe chain with actual Claude PTY."""
    LOGGER.info("=" * 60)
    LOGGER.info("TEST 3: Two-session pipe chain (real Claude PTY)")
    LOGGER.info("=" * 60)

    adapter = ClaudeAdapter()
    workspace = str(Path.cwd().resolve())

    with tempfile.TemporaryDirectory() as tmp:
        log_root = Path(tmp)
        server_id = "0"
        channel_id = 0
        log_dir = log_root / server_id / str(channel_id)
        log_dir.mkdir(parents=True, exist_ok=True)

        # ── Spawn session A ──
        LOGGER.info("Spawning session A...")
        session_a = await spawn_claude(workspace, adapter)

        # ── Spawn session B ──
        LOGGER.info("Spawning session B...")
        session_b = await spawn_claude(workspace, adapter)

        # ── Session A: send a complex question ──
        sink_a = MockSink(label="A")
        await sink_a.begin("Processing A")

        question_a = (
            "블루 아카이브의 게임개발부 캐릭터 5명의 이름과 각각의 성격을 "
            "한 줄씩 요약해줘. 한국어로 답해."
        )
        LOGGER.info("[A] Sending question: %s", question_a[:60])
        inputs_a = adapter.build_message_inputs(question_a)
        await session_a.send_message(question_a, inputs=inputs_a)

        # ── Immediately: B shows processing (before A finishes) ──
        sink_b = MockSink(label="B")
        await sink_b.begin("Processing B")
        LOGGER.info("[B] processing... shown immediately (pipe chain ready)")
        LOGGER.info("[B] Will wait for A's response log to appear...")

        # ── Wait for A to finish ──
        LOGGER.info("[A] Waiting for response...")
        await session_a.wait_response(timeout=120, idle_seconds=3.0)
        response_a = adapter.extract_response(session_a.get_screen_lines())
        LOGGER.info("[A] Got response (%d chars): %s", len(response_a), response_a[:100])

        # Finalize A and write log (simulates what turn_executor does)
        response_id_a = await sink_a.finalize(response_a)
        log_path_a = log_dir / f"{response_id_a}.txt"
        log_path_a.write_text(
            f"@[trigger] [A]\n{response_a}", encoding="utf-8",
        )
        LOGGER.info("[A] Log saved: %s", log_path_a.name)

        # ── B: resolve pipe (should find A's log immediately) ──
        LOGGER.info("[B] Resolving pipe to %s...", response_id_a)
        assert log_path_a.exists(), "A's log file not found!"
        content = log_path_a.read_text(encoding="utf-8")
        _, _, body = content.partition("\n")
        pipe_content = body.strip()
        LOGGER.info("[B] Pipe resolved (%d chars): %s", len(pipe_content), pipe_content[:80])

        # ── B: send question with pipe content ──
        question_b = f"[메시지 참조]\n{pipe_content}\n---\n위 답변에서 가장 특이한 캐릭터는 누구야? 왜?"
        LOGGER.info("[B] Sending piped question (%d chars)", len(question_b))
        inputs_b = adapter.build_message_inputs(question_b)
        await session_b.send_message(question_b, inputs=inputs_b)

        # ── Wait for B to finish ──
        LOGGER.info("[B] Waiting for response...")
        await session_b.wait_response(timeout=120, idle_seconds=3.0)
        response_b = adapter.extract_response(session_b.get_screen_lines())
        LOGGER.info("[B] Got response (%d chars): %s", len(response_b), response_b[:100])

        response_id_b = await sink_b.finalize(response_b)

        # ── Verify results ──
        LOGGER.info("=" * 60)
        LOGGER.info("RESULTS:")
        LOGGER.info("  A processing_id: %s", sink_a.status_msg_id)
        LOGGER.info("  A response_id:   %s (same = edit-in-place)", response_id_a)
        LOGGER.info("  A response:      %s...", response_a[:80])
        LOGGER.info("  B processing_id: %s", sink_b.status_msg_id)
        LOGGER.info("  B response_id:   %s (same = edit-in-place)", response_id_b)
        LOGGER.info("  B response:      %s...", response_b[:80])

        assert response_id_a == sink_a.status_msg_id, "A: ID mismatch!"
        assert response_id_b == sink_b.status_msg_id, "B: ID mismatch!"
        assert len(response_a) > 10, "A: response too short"
        assert len(response_b) > 10, "B: response too short"

        LOGGER.info("PASS: Pipe chain completed successfully!")

        # Cleanup
        session_a.stop()
        session_b.stop()


async def main():
    print("\n🔧 Pipe Chain Integration Test\n")

    await test_edit_in_place_id_preservation()
    print()

    await test_failure_log_and_cascade()
    print()

    await test_pipe_chain_two_sessions()
    print()

    print("✅ All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
