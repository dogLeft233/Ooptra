"""真实应用按渠道播放事实提交事务历史；生成草稿不能冒充已输出。"""
import asyncio
import base64

from aiohttp import web
from aiohttp.test_utils import TestServer
from support.channel_fixtures import make_voice_agent
from support.live_runtime import Playback, audio_message, make_live_agent, settle, wait_until


def test_client_interruption_never_records_the_full_generated_reply(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=0)
        try:
            await sockets[0].deliver(audio_message(user_text="question", text="complete generated answer"))
            await wait_until(lambda: bool(playback.chunks))
            assert playback.chunks
            await playback.capture("u", b"\x00\x40" * 320)
            assert playback.stops == 1
            await sockets[0].deliver({"serverContent": {"turnComplete": True}})
            await agent.wait_for_reply_end(timeout=1)
            assert agent.memory.as_messages() == [{"role": "user", "content": "question"}]
        finally:
            await agent.leave()
    asyncio.run(run())


def test_same_room_context_refresh_rejects_late_cascade_result(tmp_path):
    async def run():
        entered, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def model(request):
            body = await request.json()
            calls.append(body["model"])
            if body["model"] == "asr":
                entered.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    await released.wait()
                finished.set()
                return web.json_response({"choices": [{"message": {"content": "old source question"}}]})
            if body.get("stream"):
                encoded = base64.b64encode(b"\0\x40" * 100).decode()
                return web.Response(text='data: {"choices":[{"delta":{"audio":{"data":"' + encoded + '"}}}]}\n\n' + 'data: [DONE]\n\n', content_type="text/event-stream")
            return web.json_response({"choices": [{"message": {"content": "stale reply"}}]})
        app = web.Application()
        app.router.add_post("/chat/completions", model)
        server = TestServer(app)
        await server.start_server()
        agent = make_voice_agent(tmp_path)
        playback = Playback()
        agent.settings.mimo_api_key = "synthetic"
        agent.settings.mimo_base_url = str(server.make_url("/")).rstrip("/")
        agent.settings.mimo_asr_model = "asr"
        agent.settings.voice_leave_enabled = False
        agent.settings.voice_volume_enabled = False
        agent.bind_duplex(playback)
        try:
            await agent.join("area", "room")
            original_session = agent.channels.snapshot().session
            # Real channel capture -> energy VAD -> application input task -> local HTTP ASR.
            for _ in range(40):
                await playback.capture("u", b"\0\x40" * 480)
            for _ in range(45):
                await playback.capture("u", bytes(960))
            await asyncio.wait_for(entered.wait(), 1)
            await agent.refresh_astrbot_context()
            released.set()
            await asyncio.wait_for(finished.wait(), 1)
            await settle()
            assert agent.channels.snapshot().session == original_session
            assert playback.chunks == []
            assert agent.memory.as_messages() == []
            assert calls == ["asr"], "stale ASR must never start expression generation"
        finally:
            released.set()
            await agent.leave()
            await server.close()
    asyncio.run(run())
