"""真实 Live Hold 到期证据必须结束没有播放任务的输入诊断。"""
import asyncio

from support.live_runtime import Socket, audio_message

from app.live import LiveFlow
from app.observation import FlowObservations
from behavior import create_behavior
from behavior.models import ReplySettings
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomTarget
from context import create_context_service
from context.models import VoiceTraceQuery
from dialogue import create_gemini_backend
from operations.models import VoiceAgentSettings


def test_held_live_turn_expiry_finishes_its_persistent_diagnostic(tmp_path):
    async def run():
        finished = asyncio.Event()
        class Observations(FlowObservations):
            def trace(self, *args, **kwargs):
                super().trace(*args, **kwargs)
                if args[3] == 'finished':
                    finished.set()
        class Transport:
            async def join(self, target): return 'rtc'
            async def leave(self): pass
            async def retire_output(self): pass
            async def push_audio(self, pcm, rate, *, finish=False): return AudioPushResult(True)
            async def wait_playback(self, timeout): return AudioPushResult(True)
        socket = Socket()
        async def connect(*args, **kwargs): return socket
        context = create_context_service(str(tmp_path / 'memory.jsonl'))
        backend = create_gemini_backend(VoiceAgentSettings(gemini_api_key='synthetic'), connector=connect)
        channel = create_channel(Transport())
        session = (await channel.execute(JoinRoom('join', RoomTarget('a', 'b'), 0))).session
        flow = LiveFlow(backend=backend, context=context, behavior=create_behavior(), channel=channel,
            session=session, current=lambda: True, settings=lambda: ReplySettings(probability=0, keywords=('机器人',)),
            on_state=lambda *args: None, on_leave=lambda **kwargs: False,
            output_state=lambda: {'volume': 50, 'muted': False}, observations=Observations(journal=context))
        try:
            await flow.start()
            await socket.deliver(audio_message(user_text='我在自己推导公式', text='候选回答', done=True))
            await asyncio.wait_for(finished.wait(), 3)
            await context.flush_voice_traces()
            row = context.voice_traces(VoiceTraceQuery()).rows[0].trace
            assert (row.stage, row.reason, row.outcome) == ('finished', 'expired', 'not_executed')
            assert row.text == '我在自己推导公式'
            assert not flow.busy
            assert not any(item['role'] == 'assistant' for item in context.history.recent())
        finally:
            await flow.close()
            await context.close()
    asyncio.run(run())
