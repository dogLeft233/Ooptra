import asyncio
import base64
import io
import wave

import pytest
from test_voice_operations import make_agent

from dialogue.models import DialogueCapabilities


def test_preview_is_independent_wav_and_releases_backend(tmp_path, monkeypatch):
    from voice_agent import preview

    async def run():
        agent = make_agent(tmp_path)
        calls = []
        class Backend:
            def capabilities(self):
                return DialogueCapabilities()
            async def rewrite_announcement(self, kind, text):
                return "你好呀"
            async def tts(self, text):
                calls.append(text)
                return b"\x00\x20" * 240, 24000
            async def aclose(self):
                calls.append("closed")
        monkeypatch.setattr(preview, "create_backend", lambda settings, memory, **kwargs: Backend())
        result = await preview.PreviewService().generate(agent.settings, "enter", "打招呼", "测试音色")
        with wave.open(io.BytesIO(base64.b64decode(result["wav_base64"]))) as wav:
            assert wav.getframerate() == 24000
            assert wav.getnchannels() == 1
        assert result["text"] == "你好呀"
        assert calls == ["你好呀", "closed"]
        assert not agent.status()["joined"] and not agent.memory.recent()
        assert agent.settings.mimo_tts_voice != "测试音色"
    asyncio.run(run())


def test_live_preview_collects_temp_audio_and_text(tmp_path, monkeypatch):
    from voice_agent import preview

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.backend = "gemini_live"
        class Backend:
            def capabilities(self):
                return DialogueCapabilities(live=True)
            closed = False
            def set_audio_out(self, fn):
                self.audio = fn
            def set_text_out(self, fn):
                self.text = fn
            def set_turn_out(self, fn):
                self.turn = fn
            async def speak_announcement(self, kind, text):
                self.audio(b"\x00\x20" * 240, 24000)
                self.text("拜拜啦")
                self.turn(False)
            async def aclose(self):
                self.closed = True
        backend = Backend()
        monkeypatch.setattr(preview, "create_backend", lambda settings, memory, **kwargs: backend)
        result = await preview.PreviewService().generate(agent.settings, "leave", "告别")
        assert result["text"] == "拜拜啦" and backend.closed
        assert not agent.memory.recent()
    asyncio.run(run())


def test_preview_cancellation_closes_and_allows_retry(tmp_path, monkeypatch):
    from voice_agent import preview

    async def run():
        agent = make_agent(tmp_path)
        started = asyncio.Event()
        class Backend:
            def capabilities(self):
                return DialogueCapabilities()
            closed = False
            async def tts(self, text):
                started.set()
                await asyncio.Event().wait()
            async def aclose(self):
                self.closed = True
        backend = Backend()
        monkeypatch.setattr(preview, "create_backend", lambda settings, memory, **kwargs: backend)
        service = preview.PreviewService()
        task = asyncio.create_task(service.generate(agent.settings, "voice", "你好"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.closed and not service.busy
    asyncio.run(run())


def test_local_diagnostics_do_not_touch_active_backend(tmp_path):
    from voice_agent.diagnostics import diagnose

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
    from behavior import parse_auto_visit_config
    from voice_agent.preview import saved_prompts

    config = parse_auto_visit_config({"defaults": {"enter_prompts": ["你好", "在玩什么？"]}, "areas": {"a": {"overrides": {"enter_prompts": ["自定义"]}}, "b": {"overrides": {"leave_prompts": []}}}})
    assert saved_prompts(config, "enter") == ["你好", "在玩什么？"]
    assert saved_prompts(config, "enter", "a") == ["自定义"]
    assert saved_prompts(config, "leave", "b") == []
