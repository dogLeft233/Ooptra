"""Uploaded short WAV diagnostics use disposable resources and finite stages."""

import asyncio
import io
import wave

import pytest
from support.channel_fixtures import establish_room
from test_voice_operations import make_agent


def sample_wav(seconds=.5):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x20" * int(seconds * 16000))
    return output.getvalue()


@pytest.mark.parametrize("failure", [None, "asr", "intent", "tts"])
def test_sample_probe_is_independent_and_reports_individual_stages(tmp_path, monkeypatch, failure):
    from voice_agent import diagnostics
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.mimo_api_key = "test"
        await establish_room(agent)
        agent.memory.append("user", "existing room memory", user_key="member")
        before = agent.memory.recent()
        calls = []
        class Backend:
            async def asr(self, pcm, rate):
                calls.append("asr")
                assert len(pcm) == 16000 and rate == 16000
                if failure == "asr":
                    raise RuntimeError("credential-secret")
                return "你好，不要离开"
            async def detect_control_intent(self, text):
                calls.append("intent")
                if failure == "intent":
                    raise RuntimeError("credential-secret")
                return False
            async def tts(self, text):
                calls.append("tts")
                if failure == "tts":
                    raise RuntimeError("credential-secret")
                return b"\x00\x20" * 240, 24000
            async def aclose(self):
                calls.append("closed")
        def factory(settings, memory, **kwargs):
            assert settings is not agent.settings and memory is not agent.memory
            return Backend()
        monkeypatch.setattr(diagnostics, "create_backend", factory)
        result = await diagnostics.diagnose(agent, audio_wav=sample_wav())
        checks = {item["id"]: item for item in result["checks"]}
        for stage in ("asr", "intent", "tts"):
            assert checks[stage]["state"] == ("fail" if stage == failure else "skipped" if failure == "asr" and stage == "intent" else "pass")
            if checks[stage]["state"] != "skipped":
                assert checks[stage]["elapsed_ms"] >= 0
        assert calls[-1] == "closed"
        assert agent.status()["joined"] and agent.memory.recent() == before
        assert "credential-secret" not in str(result)
    asyncio.run(run())


def test_live_sample_probe_exposes_protocol_limit_and_never_calls_active_room(tmp_path, monkeypatch):
    from voice_agent import diagnostics
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "test"
        calls = []
        class Backend:
            _last_user_text = ""
            def set_audio_out(self, fn):
                self.audio = fn
            def set_turn_out(self, fn):
                self.turn = fn
            def set_voice_leave_out(self, fn):
                self.leave = fn
            async def start_session(self):
                calls.append("start")
            async def push_audio(self, pcm, rate):
                calls.append("input")
            async def _send(self, payload):
                assert payload == {"realtimeInput": {"audioStreamEnd": True}}
                self._last_user_text = "你好"
                self.turn(False)
            async def speak_text(self, text):
                calls.append("tts")
                self.audio(b"\x00\x20" * 240, 24000)
                self.turn(False)
            async def aclose(self):
                calls.append("closed")
        monkeypatch.setattr(diagnostics, "create_backend", lambda settings, memory, **kwargs: Backend())
        result = await diagnostics.diagnose(agent, audio_wav=sample_wav())
        checks = {item["id"]: item for item in result["checks"]}
        assert checks["asr"]["state"] == checks["tts"]["state"] == "pass"
        assert checks["intent"]["state"] == "skipped"
        assert calls[:3] == ["start", "input", "closed"]
        assert calls[3] == "tts"
        assert calls.count("closed") >= 2
        assert not agent.memory.recent() and not agent.status()["joined"]
    asyncio.run(run())


@pytest.mark.parametrize("early_leave", [False, True], ids=["asr-timeout", "early-leave-tool"])
def test_late_live_sample_audio_never_passes_a_silent_tts_probe(tmp_path, monkeypatch, early_leave):
    from voice_agent import diagnostics
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "test"
        original_wait = asyncio.wait_for
        async def bounded_wait(awaitable, timeout):
            return await original_wait(awaitable, .1 if timeout == 20 else timeout)
        monkeypatch.setattr(diagnostics.asyncio, "wait_for", bounded_wait)
        backends, late_tasks = [], []
        class Backend:
            _last_user_text = "机器人你先离开这里吧"
            closed = False
            def set_audio_out(self, fn):
                self.audio = fn
            def set_turn_out(self, fn):
                self.turn = fn
            def set_voice_leave_out(self, fn):
                self.leave = fn
            async def start_session(self):
                pass
            async def push_audio(self, pcm, rate):
                pass
            async def _send(self, payload):
                old_audio, old_turn = self.audio, self.turn
                async def delayed_sample_response():
                    await asyncio.sleep(.04 if early_leave else .14)
                    old_audio(b"\x00\x20" * 240, 24000)
                    old_turn(False)
                late_tasks.append(asyncio.create_task(delayed_sample_response()))
                if early_leave:
                    self.leave()
            async def speak_text(self, text):
                # The explicit request generates no PCM. Its own turn completion
                # follows the late sample response, which must belong only to ASR.
                await asyncio.sleep(.06)
                self.turn(False)
            async def aclose(self):
                self.closed = True
        def factory(settings, memory, **kwargs):
            backend = Backend()
            backends.append(backend)
            return backend
        monkeypatch.setattr(diagnostics, "create_backend", factory)
        result = await diagnostics.diagnose(agent, audio_wav=sample_wav())
        await asyncio.gather(*late_tasks)
        checks = {item["id"]: item for item in result["checks"]}
        assert checks["tts"]["state"] == "fail"
        assert len(backends) == 2 and all(backend.closed for backend in backends)
        assert not agent.status()["joined"] and not agent.memory.recent()
    asyncio.run(run())


def test_cancelling_live_tts_probe_closes_both_disposable_backends(tmp_path, monkeypatch):
    from voice_agent import diagnostics
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "test"
        started = asyncio.Event()
        backends = []
        class Backend:
            _last_user_text = "你好"
            closed = False
            def set_audio_out(self, fn):
                self.audio = fn
            def set_turn_out(self, fn):
                self.turn = fn
            def set_voice_leave_out(self, fn):
                self.leave = fn
            async def start_session(self):
                pass
            async def push_audio(self, pcm, rate):
                pass
            async def _send(self, payload):
                self.turn(False)
            async def speak_text(self, text):
                assert backends[0].closed
                started.set()
                await asyncio.Event().wait()
            async def aclose(self):
                await asyncio.sleep(0)
                self.closed = True
        def factory(settings, memory, **kwargs):
            backend = Backend()
            backends.append(backend)
            return backend
        monkeypatch.setattr(diagnostics, "create_backend", factory)
        task = asyncio.create_task(diagnostics.diagnose(agent, audio_wav=sample_wav()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(backends) == 2 and all(backend.closed for backend in backends)
    asyncio.run(run())


def test_sample_cancellation_waits_for_disposable_cleanup(tmp_path, monkeypatch):
    from voice_agent import diagnostics
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.mimo_api_key = "test"
        started = asyncio.Event()
        closed = []
        class Backend:
            async def asr(self, *args):
                started.set()
                await asyncio.Event().wait()
            async def aclose(self):
                await asyncio.sleep(0)
                closed.append(True)
        monkeypatch.setattr(diagnostics, "create_backend", lambda *args, **kwargs: Backend())
        task = asyncio.create_task(diagnostics.diagnose(agent, audio_wav=sample_wav()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
    asyncio.run(run())


@pytest.mark.parametrize("data", [b"garbage", sample_wav(10.1), b"x" * (2 * 1024 * 1024 + 1)], ids=["invalid", "too-long", "too-large"])
def test_invalid_or_long_audio_never_constructs_network_backend(tmp_path, monkeypatch, data):
    from voice_agent import diagnostics
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.mimo_api_key = "test"
        monkeypatch.setattr(diagnostics, "create_backend", lambda *args, **kwargs: pytest.fail("invalid audio sent to model"))
        with pytest.raises(ValueError):
            await diagnostics.diagnose(agent, audio_wav=data)
    asyncio.run(run())
