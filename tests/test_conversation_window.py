import asyncio

from test_voice_operations import make_agent


def test_window_is_per_member_extends_expires_and_clears():
    from behavior import create_reply_policy

    now = [100.0]
    window = create_reply_policy(clock=lambda: now[0])
    assert window.observe("ＯＯＰＴＲＡ，好呀", ["ooptra"], "a", 30)  # noqa: RUF001 -- ASR normalization
    assert not window.observe("继续", [], "b", 30)
    now[0] = 129
    assert window.observe("继续", [], "a", 30)
    now[0] = 159
    assert not window.observe("继续", [], "a", 30)
    assert window.observe("ooptra", ["ooptra"], "a", 30)
    window.clear()
    assert not window.observe("继续", [], "a", 30)


def test_unknown_member_never_opens_window_and_disabled_clears():
    from behavior import create_reply_policy

    window = create_reply_policy()
    assert window.observe("ooptra", ["ooptra"], "", 30)
    assert not window.observe("继续", [], "", 30)
    assert window.observe("ooptra", ["ooptra"], "a", 30)
    assert not window.observe("继续", [], "a", 0)
    assert not window.observe("继续", [], "a", 30)


def test_mimo_followups_bypass_zero_probability_until_room_changes(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        agent.settings.conversation_window_enabled = True
        agent.settings.conversation_window_seconds = 30
        await agent.join("a", "c")
        text = ["ooptra，你好"]
        chats = []
        async def asr(*args):
            return text[0]
        async def chat(value, **kwargs):
            chats.append(value)
            return "好呀"
        async def tts(*args):
            return b"", 24000
        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "chat", chat)
        monkeypatch.setattr(agent.backend, "tts", tts)
        async def say(user):
            return await agent.backend.handle_utterance(
                b"\x00\x20" * 16000, sample_rate=16000, user_key=user, channel_key="a/c"
            )
        await say("a")
        text[0] = "然后呢"
        await say("a")
        await say("b")
        assert chats == ["ooptra，你好", "然后呢"]
        await agent.leave()
        await agent.join("a", "d")
        await say("a")
        assert len(chats) == 2
        await agent.leave()
    asyncio.run(run())


def test_live_window_requires_unambiguous_input_speaker(tmp_path):
    from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        audio = []
        backend.set_audio_out(lambda pcm, rate: audio.append(pcm))
        async def turn(text, speakers):
            for speaker in speakers:
                backend.note_input_speaker(speaker)
            await backend._handle_server_msg({"serverContent": {
                "inputTranscription": {"text": text},
                "modelTurn": {"parts": [{"inlineData": {"data": "eA=="}}]},
                "turnComplete": True,
            }})
        await turn("ooptra", ["a"])
        await turn("继续", ["b"])
        await turn("继续", ["a"])
        assert len(audio) == 2
        await turn("继续", ["a", "b"])
        assert len(audio) == 2
        await backend.aclose()
    asyncio.run(run())


def test_live_reconnect_retries_are_bounded_and_close_stops_them(tmp_path, monkeypatch):
    from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

    async def run():
        agent = make_agent(tmp_path)
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        backend.reconnect_delay = 0
        attempts = []
        async def failed():
            attempts.append(True)
            raise OSError("network unavailable")
        monkeypatch.setattr(backend, "start_session", failed)
        await backend._reconnect()
        assert len(attempts) == 3
        assert backend.connection_status()["state"] == "failed"
        backend._schedule_reconnect()
        await asyncio.sleep(0)
        assert len(attempts) == 3
        await backend.aclose()
        await backend._reconnect()
        assert len(attempts) == 3
    asyncio.run(run())


def test_live_mixed_speaker_late_keyword_never_opens_member_window(tmp_path):
    from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        backend.note_input_speaker("alice")
        await backend._handle_server_msg({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": "eA=="}}]}}})
        backend.note_input_speaker("bob")
        await backend._handle_server_msg({"serverContent": {"turnComplete": True}})
        await backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "ooptra"}}})
        assert not backend._conversation.observe("继续", [], "alice", 30)
        await backend.aclose()
    asyncio.run(run())


def test_live_failed_connection_audio_does_not_restart_for_every_frame(tmp_path, monkeypatch):
    from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

    async def run():
        agent = make_agent(tmp_path)
        backend = GeminiLiveBackend(agent.settings, agent.memory)
        backend._connection_state = "failed"
        attempts = []
        async def start():
            attempts.append(True)
        monkeypatch.setattr(backend, "start_session", start)
        for _ in range(10):
            await backend.push_audio(b"\x00\x20" * 160)
        assert not attempts
        await backend.aclose()
    asyncio.run(run())
