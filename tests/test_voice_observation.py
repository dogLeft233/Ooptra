"""流程诊断来自真实决策与生成事实，容量有界且不保留会话正文。"""
import asyncio
import json

import pytest
from support.channel_fixtures import make_voice_agent
from support.expression_runtime import ScriptedExpression
from support.live_runtime import audio_message, make_live_agent

from dialogue.models import Recognition


class Model(ScriptedExpression):
    name = "external-model"
    def __init__(self, *, fail=""):
        self.fail = fail
    async def recognize(self, audio, permit):
        return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key,
            "failed" if self.fail == "asr" else "recognized", "private question", elapsed_ms=12)
    async def tts(self, text):
        if self.fail == "tts":
            raise RuntimeError("secret-model-key")
        return await super().tts(text)


def test_observations_are_bounded_detached_and_content_free(tmp_path):
    async def scenario():
        agent = make_voice_agent(tmp_path)
        agent.backend = Model()
        agent.settings.reply_probability_percent = 0
        await agent.join("a", "b")
        try:
            for _ in range(45):
                await agent._handle_utterance("u", b"\0\1" * 16000, 16000)
            payload = agent.status()["voice_observation"]
            assert len(payload["recent_decisions"]) == 40
            row = payload["recent_decisions"][-1]
            assert row["reason"] == "policy_silence"
            assert row["timings_ms"]["asr"] == 12
            assert row["timings_ms"]["tts"] is None
            assert row["elapsed_ms"] >= 0
            assert "private question" not in json.dumps(payload)
            payload["recent_decisions"].clear()
            assert len(agent.status()["voice_observation"]["recent_decisions"]) == 40
        finally:
            await agent.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["asr", "tts"])
def test_flow_failure_diagnostic_does_not_include_provider_exception(tmp_path, failure):
    async def scenario():
        agent = make_voice_agent(tmp_path)
        agent.backend = Model(fail=failure)
        agent.settings.min_utterance_ms = 0
        await agent.join("a", "b")
        try:
            await agent._handle_utterance("u", b"\0\1"*16000, 16000)
            payload = agent.status()["voice_observation"]
            assert payload["recent_decisions"]
            assert "secret-model-key" not in json.dumps(payload)
            assert payload["recent_decisions"][-1]["outcome"] != "completed"
        finally:
            await agent.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("percent", [0,100])
def test_live_trace_keeps_overlap_explicit_without_inventing_asr_timing(tmp_path, percent):
    async def scenario():
        agent, _, sockets = await make_live_agent(tmp_path, reply_probability_percent=percent)
        try:
            await sockets[0].deliver(audio_message(user_text="private input",text="private reply",done=True))
            assert await agent.wait_for_reply_end(1)
            rows = agent.status()["voice_observation"]["recent_decisions"]
            assert rows
            assert rows[-1]["timing_mode"] == "live_overlap"
            assert rows[-1]["timings_ms"]["asr"] is None
            assert "private input" not in json.dumps(rows)
            assert "private reply" not in json.dumps(rows)
        finally:
            await agent.stop()
    asyncio.run(scenario())
