"""真实HTTP生成请求不能重复追加已落盘的当前输入；相同旧文本必须保留。"""
import asyncio
import base64
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from app.flows import run_cascade
from behavior import create_behavior
from behavior.models import ReplySettings
from channels import create_channel
from channels.models import JoinRoom, RoomTarget
from context import create_context_service
from context.models import Observation
from dialogue import create_mimo_backend
from operations.models import VoiceAgentSettings
from tests.app.test_subtitle_outputs import Transport


def test_real_cascade_http_prompt_contains_old_equal_input_and_current_input_once_each(tmp_path):
    async def run():
        requests = []
        async def model(request):
            body = await request.json()
            if body["model"] == "asr":
                return web.json_response({"choices": [{"message": {"content": "你好"}}]})
            if body["model"] == "llm":
                requests.append(body)
                return web.json_response({"choices": [{"message": {"content": "你好呀"}}]})
            event = {"choices": [{"delta": {"audio": {"data": base64.b64encode(b"\0\x40" * 320).decode()}}}]}
            return web.Response(text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n", content_type="text/event-stream")
        app = web.Application()
        app.router.add_post("/chat/completions", model)
        server = TestServer(app)
        await server.start_server()
        settings = VoiceAgentSettings(backend="mimo_cascade", mimo_api_key="synthetic",
            mimo_base_url=str(server.make_url("/")).rstrip("/"), mimo_asr_model="asr",
            mimo_llm_model="llm", mimo_tts_model="tts", proxy="direct")
        backend = create_mimo_backend(settings)
        context = create_context_service(str(tmp_path / "memory.jsonl"))
        channel = create_channel(Transport(True))
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
        context.observe(Observation("old", session, "old-capture", 1, "recognized", "你好", "u", "old"))
        behavior = create_behavior()
        behavior.start_session(session)
        try:
            result = await run_cascade(backend=backend, context=context, behavior=behavior, channel=channel,
                session=session, trigger_id="current", user_key="u", pcm=b"\0\x40"*16000,
                sample_rate=16000, limit=0, settings=ReplySettings(), interpret_controls=False,
                current=lambda:True, on_playing=lambda _:None)
            assert result.outcome.status == "completed"
            assert [[message["content"] for message in body["messages"] if message["role"] == "user"] for body in requests] == [["你好", "你好"]]
            assert [row["content"] for row in context.history.recent()] == ["你好", "你好", "你好呀"]
        finally:
            await backend.aclose()
            await context.close()
            await server.close()
    asyncio.run(run())
