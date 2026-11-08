import asyncio
import base64
import json
import time
from types import SimpleNamespace

from channels.models import RoomTarget, SessionRef
from context.models import ContextRef, ContextRevision, PreparedContext
from dialogue import audio_output, create_gemini_backend
from dialogue.models import Allow, LiveSessionRequest, Reject
from operations.models import VoiceAgentSettings


class Socket:
    closed = False
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
    def __aiter__(self):
        return self
    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)
    async def send(self, text):
        self.sent.append(json.loads(text))
        if "setup" in self.sent[-1]:
            await self.incoming.put({"setupComplete": {}})
    async def close(self):
        self.closed = True
        await self.incoming.put(None)


def test_live_provider_waits_permission_then_releases_only_once():
    async def scenario():
        socket = Socket()
        async def connect(*args, **kwargs):
            return socket
        settings = VoiceAgentSettings()
        settings.gemini_api_key = "synthetic"
        backend = create_gemini_backend(settings, connector=connect)
        session = SessionRef("run", "bind", "session", RoomTarget("a", "c"))
        prepared = PreparedContext(ContextRef(session, 0, "local"), ContextRevision(0),
                                   "说中文", ())
        live = await backend.open_live(LiveSessionRequest(session, prepared))
        events = live.events()
        pcm = b"\1\0" * 100
        await socket.incoming.put({"serverContent": {"modelTurn": {"parts": [
            {"inlineData": {"data": base64.b64encode(pcm).decode(),
                            "mimeType": "audio/pcm;rate=24000"}}]}}})
        proposed = await asyncio.wait_for(anext(events), 1)
        assert proposed.kind == "proposed"
        assert live.snapshot().action_id == ""
        intent = SimpleNamespace(action_id="action", session=session,
            context_version=prepared.version, deadline=time.monotonic() + 5)
        receipt = await live.resolve_turn(proposed.proposal, Allow(intent))
        assert receipt.status == "applied"
        output = await asyncio.wait_for(anext(events), 1)
        assert output.kind == "output_ready"
        assert (await live.resolve_turn(proposed.proposal, Allow(intent))).status == "duplicate"
        await socket.incoming.put({"serverContent": {
            "outputTranscription": {"text": "你好"}, "turnComplete": True}})
        audio = audio_output(output.job)
        frames = [frame async for frame in audio.frames()]
        assert b"".join(frame.pcm16 for frame in frames) == pcm
        assert audio.evidence().text == "你好"
        await events.aclose()
        await live.aclose()
        assert socket.closed
    asyncio.run(scenario())


def test_rejected_live_turn_cannot_be_resurrected_by_late_allow():
    async def scenario():
        socket = Socket()
        async def connect(*args, **kwargs):
            return socket
        settings = VoiceAgentSettings()
        settings.gemini_api_key = "synthetic"
        backend = create_gemini_backend(settings, connector=connect)
        session = SessionRef("run", "bind", "session", RoomTarget("a", "c"))
        prepared = PreparedContext(ContextRef(session, 0, "local"), ContextRevision(0), "", ())
        live = await backend.open_live(LiveSessionRequest(session, prepared))
        events = live.events()
        await socket.incoming.put({"serverContent": {"outputTranscription": {"text": "草稿"}}})
        proposed = await asyncio.wait_for(anext(events), 1)
        assert (await live.resolve_turn(proposed.proposal, Reject("policy"))).status == "rejected"
        intent = SimpleNamespace(action_id="action", session=session,
            context_version=prepared.version, deadline=time.monotonic() + 5)
        assert (await live.resolve_turn(proposed.proposal, Allow(intent))).status == "stale"
        assert live.snapshot().action_id == ""
        await events.aclose()
        await live.aclose()
    asyncio.run(scenario())


async def open_session(*, limits=None):
    socket = Socket()
    async def connect(*args, **kwargs):
        return socket
    settings = VoiceAgentSettings(gemini_api_key="synthetic")
    backend = create_gemini_backend(settings, connector=connect)
    session = SessionRef("run", "bind", "session", RoomTarget("a", "c"))
    prepared = PreparedContext(ContextRef(session, 0, "local"), ContextRevision(0), "", ())
    request = LiveSessionRequest(session, prepared, limits) if limits else LiveSessionRequest(session, prepared)
    return socket, await backend.open_live(request), prepared


def audio_message(pcm=b"\1\0"):
    return {"serverContent": {"modelTurn": {"parts": [{"inlineData": {
        "data": base64.b64encode(pcm).decode(), "mimeType": "audio/pcm;rate=24000"}}]}}}


def test_live_hold_completed_audio_can_be_allowed_after_late_transcription():
    from dialogue.models import Hold
    async def scenario():
        socket, live, prepared = await open_session()
        events = live.events()
        await socket.incoming.put(audio_message())
        first = await anext(events)
        assert (await live.resolve_turn(first.proposal, Hold(time.monotonic() + 1))).status == "held"
        await socket.incoming.put({"serverContent": {"turnComplete": True}})
        await anext(events)
        await socket.incoming.put({"serverContent": {"inputTranscription": {"text": "小蓝你好"}}})
        updated = await anext(events)
        assert updated.kind == "updated" and updated.text == "小蓝你好"
        assert updated.proposal.turn_id == first.proposal.turn_id
        intent = SimpleNamespace(action_id="late-action", session=prepared.context_ref.session,
            context_version=prepared.version, deadline=time.monotonic() + 5)
        assert (await live.resolve_turn(first.proposal, Allow(intent))).status == "stale"
        assert (await live.resolve_turn(updated.proposal, Allow(intent))).status == "applied"
        output = await anext(events)
        frames = [frame async for frame in audio_output(output.job).frames()]
        assert b"".join(frame.pcm16 for frame in frames) == b"\1\0"
        await events.aclose()
        await live.aclose()
    asyncio.run(scenario())


def test_live_client_interrupt_discards_late_audio_and_keeps_next_turn_ready():
    async def scenario():
        socket, live, prepared = await open_session()
        events = live.events()
        await socket.incoming.put(audio_message())
        first = await anext(events)
        intent = SimpleNamespace(action_id="action", session=prepared.context_ref.session,
            context_version=prepared.version, deadline=time.monotonic() + 5)
        await live.resolve_turn(first.proposal, Allow(intent))
        output = await anext(events)
        await live.interrupt()
        interrupted = await anext(events)
        assert interrupted.reason == "interrupted"
        assert output.job.snapshot().status == "cancelled"
        await socket.incoming.put(audio_message(b"\2\0"))
        await socket.incoming.put({"serverContent": {"turnComplete": True}})
        await socket.incoming.put(audio_message(b"\3\0"))
        second = await asyncio.wait_for(anext(events), 1)
        assert second.kind == "proposed"
        assert second.proposal.turn_id != first.proposal.turn_id
        assert live.snapshot().state == "ready"
        await events.aclose()
        await live.aclose()
    asyncio.run(scenario())


def test_live_control_queue_overflow_is_queryable_and_closes_transport():
    async def scenario():
        socket, live, _ = await open_session()
        for _ in range(140):
            await socket.incoming.put({"serverContent": {"inputTranscription": {"text": "词"}}})
        for _ in range(500):
            if live.snapshot().state == "failed" and socket.closed:
                break
            await asyncio.sleep(0)
        assert live.snapshot().state == "failed"
        assert live.snapshot().reason == "event_delivery_overflow"
        assert socket.closed
        await live.aclose()
    asyncio.run(scenario())


def test_unresolved_live_turn_expires_without_leaking_or_reviving_audio():
    from dialogue.models import LiveLimits
    async def scenario():
        socket, live, prepared = await open_session(limits=LiveLimits(late_transcription_seconds=.01))
        events = live.events()
        await socket.incoming.put(audio_message())
        proposed = await anext(events)
        await asyncio.sleep(.02)
        assert live.snapshot().state == "failed"
        intent = SimpleNamespace(action_id="action", session=prepared.context_ref.session,
            context_version=prepared.version, deadline=time.monotonic() + 5)
        assert (await live.resolve_turn(proposed.proposal, Allow(intent))).status == "stale"
        await events.aclose()
        await live.aclose()
    asyncio.run(scenario())
