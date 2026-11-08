import asyncio

import pytest

from app.voice import VoiceCoordinator
from channels.models import RoomTarget, SessionRef
from context.models import ContextRequest
from operations.models import VoiceAgentSettings, VoiceApiSettings


def test_full_close_retires_context_but_restart_creates_a_fresh_service(tmp_path):
    async def scenario():
        agent = VoiceCoordinator(VoiceAgentSettings(enabled=False, backend="mimo_cascade",
                    memory_path=str(tmp_path/"history.jsonl")), VoiceApiSettings())
        original = agent.context_service
        request = ContextRequest(SessionRef("run", "binding", "session", RoomTarget("a", "b")))
        await agent.close()
        with pytest.raises(RuntimeError, match="关闭"):
            await original.prepare_context(request)
        agent.settings.enabled = True
        await agent.start()
        try:
            assert agent.context_service is not original
            await agent.context_service.prepare_context(request)
        finally:
            await agent.close()
    asyncio.run(scenario())
