import asyncio
import base64
import io
import wave

import pytest
from test_voice_operations import make_agent


def test_preview_is_independent_wav_and_releases_backend(tmp_path):
    import json

    from aiohttp import web

    from dialogue import create_preview_service
    from operations.models import VoiceAgentSettings

    async def run():
        requests = []
        async def model(request):
            body = await request.json()
            requests.append(body)
            if body.get("audio"):
                audio = base64.b64encode(b"\x00\x20" * 240).decode()
                return web.Response(text='data: '+json.dumps({"choices":[{"delta":{
                    "audio":{"data":audio}}}]})+'\n\ndata: [DONE]\n\n')
            return web.json_response({"choices":[{"message":{"content":"你好呀"}}]})
        app = web.Application()
        app.router.add_post('/chat/completions', model)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        settings = VoiceAgentSettings(backend="mimo_cascade", mimo_api_key="synthetic",
            mimo_base_url=f"http://127.0.0.1:{port}")
        try:
            result = await create_preview_service().generate(settings,"enter","打招呼","测试音色")
            with wave.open(io.BytesIO(base64.b64decode(result["wav_base64"]))) as wav:
                assert wav.getframerate() == 24000
                assert wav.getnchannels() == 1
            assert result["text"] == "你好呀"
            assert requests[1]["audio"]["voice"] == "测试音色"
            assert settings.mimo_tts_voice != "测试音色"
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_live_preview_collects_temp_audio_and_text(tmp_path):
    from support.live_socket import Socket

    from dialogue import create_gemini_backend, create_preview_service
    from operations.models import VoiceAgentSettings
    async def run():
        class PreviewSocket(Socket):
            async def send(self, text):
                await super().send(text)
                if "realtimeInput" in self.sent[-1]:
                    await self.incoming.put({"serverContent": {"modelTurn": {"parts": [{
                        "inlineData": {"data": base64.b64encode(b"\x00\x20"*240).decode(),
                                       "mimeType":"audio/pcm;rate=24000"}}]},
                        "outputTranscription":{"text":"拜拜啦"},"turnComplete":True}})
        socket = PreviewSocket()
        async def connect(*args, **kwargs):
            return socket
        service = create_preview_service(backend_factory=lambda settings:
            create_gemini_backend(settings, connector=connect))
        result = await service.generate(VoiceAgentSettings(gemini_api_key="synthetic"),"leave","告别")
        assert result["text"] == "拜拜啦" and socket.closed
        setup = socket.sent[0]["setup"]
        assert "tools" not in setup
    asyncio.run(run())


def test_preview_cancellation_closes_and_allows_retry(tmp_path):
    from support.live_socket import Socket

    from dialogue import create_gemini_backend, create_preview_service
    from operations.models import VoiceAgentSettings
    async def run():
        socket = Socket()
        async def connect(*args, **kwargs):
            return socket
        service = create_preview_service(backend_factory=lambda settings:
            create_gemini_backend(settings, connector=connect))
        task = asyncio.create_task(service.generate(VoiceAgentSettings(gemini_api_key="synthetic"),"voice","你好"))
        for _ in range(100):
            if len(socket.sent) > 1:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert socket.closed and not service.busy
    asyncio.run(run())


def test_local_diagnostics_do_not_touch_active_backend(tmp_path):
    from app.diagnostics import diagnose

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.mimo_api_key = ""
        result = await diagnose(agent, network=False)
        checks = {item["id"]: item for item in result["checks"]}
        assert checks["credentials"]["state"] == "fail"
        assert checks["network"]["state"] == "skipped"
        assert not agent.status()["joined"]
        assert "sample_rate" in checks
    asyncio.run(run())


def test_saved_preview_prompts_respect_area_overrides_and_silence():
    from behavior import parse_auto_visit_config, saved_prompts

    config = parse_auto_visit_config({"defaults": {"enter_prompts": ["你好", "在玩什么？"]}, "areas": {"a": {"overrides": {"enter_prompts": ["自定义"]}}, "b": {"overrides": {"leave_prompts": []}}}})
    assert saved_prompts(config, "enter") == ["你好", "在玩什么？"]
    assert saved_prompts(config, "enter", "a") == ["自定义"]
    assert saved_prompts(config, "leave", "b") == []
