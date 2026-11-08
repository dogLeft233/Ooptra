"""共享画面只能交付给捕获时仍有效的原房间会话。"""
import asyncio

from test_room_contract import Transport

from channels import create_channel
from channels.models import JoinRoom, LeaveRoom, RoomTarget


def test_frame_from_a_room_left_during_capture_is_rejected():
    class CapturingTransport(Transport):
        async def capture_screen(self, session):
            import time

            from channels.models import ScreenCapture, ScreenFrame
            await gate.wait()
            return ScreenCapture('captured', ScreenFrame(session, 'publisher',
                b'\xff\xd8\xff\xd9', 2, 2, time.monotonic()))

    async def run():
        nonlocal gate
        gate = asyncio.Event()
        channel = create_channel(CapturingTransport())
        joined = await channel.execute(JoinRoom('join', RoomTarget('a', 'c'), 0))
        task = asyncio.create_task(channel.capture_screen(joined.session))
        await asyncio.sleep(0)
        await channel.execute(LeaveRoom('leave', joined.session))
        gate.set()
        result = await task
        assert result.status == 'stale_session' and result.frame is None
    gate = None
    asyncio.run(run())


def test_oopz_screen_viewer_normalizes_publisher_pid_and_captures_only_selected_share():
    import base64
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from channels import create_voice_binding

    async def run():
        async def frame(watch):
            # Agora numeric ids do not retain the leading zero in Oopz profiles.
            if watch['publisher'] != '84626071':
                return {'status': 'no_video'}
            return {'status': 'captured', 'publisher_id': '84626071',
                    'width': 2, 'height': 2, 'jpeg_base64': base64.b64encode(b'\xff\xd8\xff\xd9').decode()}
        sdk = SimpleNamespace(config=SimpleNamespace(agora_app_id='app'),
            capture_screen=frame, close_screen=AsyncMock())
        bot = SimpleNamespace(
            voice=SimpleNamespace(backend=sdk,
                join=AsyncMock(return_value=SimpleNamespace(rtc_channel_name='voice-room'))),
            channels=SimpleNamespace(get_voice_channel_members=AsyncMock(return_value=SimpleNamespace(
                channel_members={'c': [SimpleNamespace(uid='sharing-user', screen_sharing_state='OPEN')]}))),
            person=SimpleNamespace(get_self_detail=AsyncMock(return_value=SimpleNamespace(pid='589843154')),
                get_person_detail_full=AsyncMock(return_value=SimpleNamespace(pid='084626071'))),
            rest=SimpleNamespace(transport=SimpleNamespace(request_data=AsyncMock(return_value={
                'roomId': 'screen-room', 'signPid': 'synthetic-screen-token'}))))
        binding = create_voice_binding(bot, on_audio=lambda *_: None, controls=lambda: None)
        channel = binding.channel
        joined = await channel.execute(JoinRoom('join', RoomTarget('a', 'c'), 0))
        result = await channel.capture_screen(joined.session)
        assert result.status == 'captured'
        assert result.frame.session == joined.session
        assert result.frame.publisher_id == '84626071'
    asyncio.run(run())
