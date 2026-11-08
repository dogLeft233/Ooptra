"""级联自动接话预算在排队、失败和重新进房时保持有界。"""
import asyncio

from test_voice_operations import make_agent


def test_budget_blocks_concurrent_and_failed_rounds_until_rejoin(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.cascade_turn_limit = 3
        calls = []

        async def fail(*args, **kwargs):
            calls.append(1)
            await asyncio.sleep(0)
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(agent.backend, "handle_utterance", fail)
        try:
            await agent.join("a", "c")
            await asyncio.gather(*(
                agent._handle_utterance_reply("peer", b"\x01\x00", 16000)
                for _ in range(8)
            ))
            assert len(calls) == 3
            assert agent._cascade_limit_reached()
            # No audio/VAD/network work after exhaustion.
            monkeypatch.setattr(agent, "_vad_for", lambda uid: (_ for _ in ()).throw(
                AssertionError("exhausted budget reached VAD")))
            await agent._on_remote_pcm("peer", b"\x01\x00", 16000)
            await agent.leave()
            await agent.join("a", "c")
            assert agent.status()["cascade_attempts"] == 0
            await agent._handle_utterance_reply("peer", b"\x01\x00", 16000)
            assert len(calls) == 4
        finally:
            await agent.stop()

    asyncio.run(run())


def test_peer_filter_resolves_agora_id_using_received_identity(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.listen_only_uids = ["peer-oopz"]
        agent._bot.voice.voice_states = lambda: {"peer-oopz": {"cid": 12345}}
        received = []

        class Vad:
            def feed(self, pcm):
                received.append(pcm)
                return None

        monkeypatch.setattr(agent, "_vad_for", lambda uid: (
            Vad() if uid == "peer-oopz" else (_ for _ in ()).throw(AssertionError(uid))))
        try:
            await agent._on_remote_pcm("12345", b"\x01\x00", 16000)
            assert received == [b"\x01\x00"]
            await agent._on_remote_pcm("99999", b"\x01\x00", 16000)
            assert len(received) == 1
        finally:
            await agent.stop()

    asyncio.run(run())
