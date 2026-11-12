"""公开音频生命周期不让旧采集进入重启后的会话。"""
import asyncio

from channels import create_duplex


def test_restart_discards_queued_audio_and_old_capture_callback():
    async def scenario():
        class Transport:
            callback = None
            async def start(self):
                pass
            async def close(self):
                pass
            def set_remote_pcm_callback(self, callback):
                self.callback = callback

        transport = Transport()
        duplex = create_duplex(transport)
        entered = asyncio.Event()
        delivered = []
        async def receive(uid, pcm, rate):
            delivered.append(pcm)
            entered.set()
            if pcm == b"busy":
                await asyncio.Event().wait()

        duplex.set_remote_pcm_handler(receive)
        await duplex.start()
        old_callback = transport.callback
        old_callback("user", b"busy", 16000)
        await asyncio.wait_for(entered.wait(), 1)
        old_callback("user", b"queued-old", 16000)
        await asyncio.sleep(0)
        await duplex.close()
        await duplex.start()
        old_callback("user", b"late-old", 16000)
        transport.callback("user", b"new!", 16000)
        for _ in range(20):
            if b"new!" in delivered:
                break
            await asyncio.sleep(0)
        await duplex.close()
        assert delivered == [b"busy", b"new!"]

    asyncio.run(scenario())
