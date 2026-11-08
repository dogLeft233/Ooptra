"""关键词覆盖和有限Live等待由行为接口验证，模型流仍经过真实应用/渠道。"""
import asyncio

import pytest
from support.live_runtime import audio_message, make_live_agent, settle

from behavior import create_behavior
from behavior.models import AudioReady, RecognitionObserved, ReplySettings, Speak
from channels.models import RoomTarget, SessionRef
from context.models import ContextRevision
from operations import load_voice_agent_settings
from webui import config_editor


def test_force_keywords_load_and_are_editable_in_basic_config(monkeypatch):
    import config

    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"force_reply_keywords": [" Ooptra ", "机器人", ""]})
    settings, _ = load_voice_agent_settings()
    assert getattr(settings, "force_reply_keywords", None) == ["Ooptra", "机器人"]
    assert config_editor.schema_payload()["voice"]["fields"].get("force_reply_keywords", {}).get("tier") == "basic"
    assert config_editor._normalize_updates({"voice": {"force_reply_keywords": "Ooptra, 机器人"}}) == {
        "voice": {"force_reply_keywords": ["Ooptra", "机器人"]},
    }


@pytest.mark.parametrize("text,keywords,forced", [
    ("请问 oOpTrA 怎么办", ["OOPTRA"], True),
    ("Ｏｏｐｔｒａ，请帮忙", ["ooptra"], True),  # noqa: RUF001 - intentional full-width text
    ("Oop tra，你好", ["ooptra"], True),
    ("OOP-TRA，你好", ["ooptra"], True),
    ("机器 人，帮一下", ["机器人"], True),
    ("普通聊天", ["Ooptra", "机器人"], False),
    ("普通聊天", [" ", "..."], False),
])
def test_keyword_overrides_zero_probability_after_recognition(text, keywords, forced):
    behavior = create_behavior(random_value=lambda: pytest.fail("zero probability needs no draw"))
    session = SessionRef("r", "b", "s", RoomTarget("a", "b"))
    behavior.start_session(session)
    trigger = AudioReady("input", session, "u", ContextRevision())
    permit = behavior.admit(trigger, 3)
    result = behavior.decide(RecognitionObserved(trigger, permit, text, ContextRevision()),
                             ReplySettings(probability=0, keywords=tuple(keywords)))
    assert isinstance(result, Speak) is forced


@pytest.mark.parametrize("sample,expected", [(0.2, True), (0.8, False)])
def test_nonmatching_keyword_draws_once_for_the_same_trigger(sample, expected):
    draws = []
    def draw():
        draws.append(sample)
        return sample
    behavior = create_behavior(random_value=draw)
    session = SessionRef("r", "b", "s", RoomTarget("a", "b"))
    behavior.start_session(session)
    trigger = AudioReady("input", session, "u", ContextRevision())
    permit = behavior.admit(trigger, 3)
    observed = RecognitionObserved(trigger, permit, "ordinary question", ContextRevision())
    settings = ReplySettings(probability=30, keywords=("nickname",))
    result = behavior.decide(observed, settings)
    assert behavior.decide(observed, settings) == result
    assert isinstance(result, Speak) is expected
    assert draws == [sample]


def test_live_late_keyword_releases_all_buffered_frames_once(tmp_path):
    async def scenario():
        agent, playback, sockets = await make_live_agent(tmp_path, reply_probability_percent=0,
                                                       force_reply_keywords=["ooptra"])
        try:
            await sockets[0].deliver(audio_message(b"\1\0"*240, text="answer"))
            assert not playback.chunks
            await sockets[0].deliver({"serverContent": {"inputTranscription": {"text": "ＯＯＰ－ＴＲＡ，帮忙"}}})  # noqa: RUF001 - deliberate normalization input
            await sockets[0].deliver(audio_message(b"\2\0"*240, done=True))
            assert await agent.wait_for_reply_end(1)
            assert playback.chunks == [b"\1\0"*240, b"\2\0"*240]
        finally:
            await agent.stop()
    asyncio.run(scenario())


def test_rejected_live_turn_cannot_be_revived_by_editing_keywords(tmp_path):
    async def scenario():
        agent, playback, sockets = await make_live_agent(tmp_path, reply_probability_percent=0)
        try:
            await sockets[0].deliver(audio_message(text="discarded"))
            agent.settings.force_reply_keywords = ["ooptra"]
            await sockets[0].deliver({"serverContent": {"inputTranscription": {"text": "ooptra"}}})
            await sockets[0].deliver({"serverContent": {"turnComplete": True}})
            await settle()
            assert playback.chunks == []
            await sockets[0].deliver(audio_message(user_text="ooptra", text="next", done=True))
            assert await agent.wait_for_reply_end(1)
            assert playback.chunks
        finally:
            await agent.stop()
    asyncio.run(scenario())
