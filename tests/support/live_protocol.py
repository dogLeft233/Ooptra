"""通过真实供应商 reader 的公开 LiveSession 端口驱动合成 WebSocket。"""
import asyncio

from channels.models import RoomTarget, SessionRef
from context.models import ContextRef, ContextRevision, PreparedContext
from dialogue import create_gemini_backend
from dialogue.models import LiveSessionRequest
from operations.models import VoiceAgentSettings
from tests.dialogue.test_live_permissions import Socket


async def open_control_session(settings=None):
    socket = Socket()
    async def connector(*args, **kwargs):
        return socket
    settings = settings or VoiceAgentSettings(gemini_api_key='synthetic')
    backend = create_gemini_backend(settings, connector=connector)
    session = SessionRef('run', 'binding', 'session', RoomTarget('area', 'room'))
    prepared = PreparedContext(ContextRef(session, 0, 'local'), ContextRevision(0), '', ())
    live = await backend.open_live(LiveSessionRequest(session, prepared))
    return socket, live, prepared


def control_packet(call_id='volume', args=None, name='control_voice_output'):
    return {'toolCall': {'functionCalls': [dict(id=call_id, name=name, args=args)]}}


async def wait_responses(socket, count=1):
    async def wait():
        while True:
            replies = [row for packet in socket.sent for row in
                       packet.get('toolResponse', {}).get('functionResponses', [])]
            if len(replies) >= count:
                return replies
            await asyncio.sleep(0)
    return await asyncio.wait_for(wait(), 1)
