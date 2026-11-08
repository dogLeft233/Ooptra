"""Strict semantic decisions and actual backend control dispatch, without model keys."""

import asyncio
import json

import pytest

from behavior import models as voice_control


@pytest.mark.parametrize("raw", [
    {}, [], "mute", {"action": "unknown"}, {"action": "set", "value": True},
    {"action": "set", "value": 9}, {"action": "set", "value": 101},
    {"action": "set", "value": "50"}, {"action": "set", "value": 50.0},
    {"action": "increase", "value": 10}, {"action": "mute", "value": None},
    {"action": "restore", "extra": True},
])
def test_volume_parser_rejects_invalid_commands(raw):
    with pytest.raises(ValueError):
        voice_control.parse_volume_command(raw)


@pytest.mark.parametrize("action,value", [
    ("increase", None), ("decrease", None), ("mute", None), ("restore", None),
    ("set", 10), ("set", 50), ("set", 100),
])
def test_volume_parser_accepts_only_valid_command_shapes(action, value):
    raw = {"action": action}
    if value is not None:
        raw["value"] = value
    assert voice_control.parse_volume_command(raw) == voice_control.VolumeCommand(action, value)
    assert voice_control.parse_volume_command(None) is None


@pytest.mark.parametrize("raw", [
    None, [], {}, {"leave": "false"}, {"leave": 0}, {"leave": True, "volume": {"action": "mute"}},
    {"leave": False, "volume": {"action": "set", "value": 0}},
    {"leave": False, "unexpected": True},
])
def test_control_parser_rejects_ambiguous_or_malformed_decisions(raw):
    with pytest.raises(ValueError):
        voice_control.parse_control_decision(raw)


def test_control_parser_preserves_leave_and_volume_independently():
    assert voice_control.parse_control_decision({"leave": True}) == voice_control.VoiceControlDecision(leave=True)
    assert voice_control.parse_control_decision({"leave": False, "volume": None}) == voice_control.VoiceControlDecision()
    assert voice_control.parse_control_decision({"leave": False, "volume": {"action": "mute"}}).volume.action == "mute"



@pytest.mark.parametrize('raw,valid', [
    ({'leave': False, 'volume': {'action': 'increase'}}, True),
    ({'leave': True, 'volume': {'action': 'mute'}}, False),
    ({'leave': 'true'}, False),
])
def test_mimo_interprets_control_through_isolated_http_without_writing_memory(raw, valid):

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from channels.models import RoomTarget, SessionRef
    from dialogue import create_mimo_backend
    from dialogue.models import ControlInput
    from operations.models import VoiceAgentSettings

    async def scenario():
        received = []
        async def classify(request):
            received.append(await request.json())
            return web.json_response({'choices': [{'message': {'content': json.dumps(raw)}}]})
        app = web.Application()
        app.router.add_post('/v1/chat/completions', classify)
        async with TestServer(app) as server:
            settings = VoiceAgentSettings(mimo_api_key='synthetic', mimo_base_url=str(server.make_url('/v1')))
            backend = create_mimo_backend(settings)
            session = SessionRef('run', 'binding', 'session', RoomTarget('area', 'room'))
            try:
                result = await backend.interpret_control(ControlInput('机器人，请调高音量', 'input', session))
                assert result.status == ('candidate' if valid else 'invalid')
                if valid:
                    assert result.candidate.volume == voice_control.VolumeCommand('increase')
                # Classifying controls is an independent request, without conversation history.
                assert [message['role'] for message in received[0]['messages']] == ['system', 'user']
                assert received[0]['messages'][-1]['content'] == '机器人，请调高音量'
            finally:
                await backend.aclose()
    asyncio.run(scenario())


def test_live_control_requires_actual_channel_result_and_duplicate_permission_is_idempotent():
    from support.live_protocol import control_packet, open_control_session, wait_responses

    from channels.models import ActionOutcome
    from dialogue.models import ControlResolution

    async def scenario():
        socket, live, _ = await open_control_session()
        events = live.events()
        try:
            await socket.incoming.put(control_packet(args={'action': 'increase'}))
            event = await asyncio.wait_for(anext(events), 1)
            assert event.kind == 'control_proposed'
            assert event.candidate.volume == voice_control.VolumeCommand('increase')
            assert not [packet for packet in socket.sent if 'toolResponse' in packet]
            actual = ActionOutcome('action', 'completed', session=event.session)
            result = ControlResolution('executed', actual, volume=60, muted=False)
            first, second = await asyncio.gather(live.resolve_control(event.tool_ref, result),
                                                 live.resolve_control(event.tool_ref, result))
            assert {first.status, second.status} == {'applied', 'duplicate'}
            reply = (await wait_responses(socket))[0]['response']
            assert reply['ok'] is True and reply['volume'] == 60 and reply['muted'] is False
            await socket.incoming.put(control_packet(args={'action': 'increase'}))
            duplicate = (await wait_responses(socket, 2))[1]['response']
            assert duplicate == reply
        finally:
            await events.aclose()
            await live.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('args', [None, {}, {'action': 'unknown'}, {'action': 'set', 'value': True},
    {'action': 'set', 'value': 9}, {'action': 'set', 'value': 101},
    {'action': 'set', 'value': '50'}, {'action': 'set', 'value': 50.0},
    {'action': 'mute', 'value': 10}])
def test_live_invalid_tool_arguments_receive_rejection_without_an_action(args):
    from support.live_protocol import control_packet, open_control_session, wait_responses
    async def scenario():
        socket, live, _ = await open_control_session()
        try:
            await socket.incoming.put(control_packet(args=args))
            reply = (await wait_responses(socket))[0]['response']
            assert reply['ok'] is False and reply['status'] == 'rejected'
            assert live.snapshot().action_id == ''
        finally:
            await live.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('status,outcome', [('not_executed', None), ('executed', 'failed'), ('unknown', 'unknown')])
def test_live_failed_or_unconfirmed_action_never_reports_success(status, outcome):
    from support.live_protocol import control_packet, open_control_session, wait_responses

    from channels.models import ActionOutcome
    from dialogue.models import ControlResolution
    async def scenario():
        socket, live, _ = await open_control_session()
        events = live.events()
        try:
            await socket.incoming.put(control_packet(args={'action': 'mute'}))
            event = await asyncio.wait_for(anext(events), 1)
            actual = ActionOutcome('action', outcome, session=event.session) if outcome else None
            receipt = await live.resolve_control(event.tool_ref, ControlResolution(status, actual, reason='failed'))
            assert receipt.status == 'applied'
            reply = (await wait_responses(socket))[0]['response']
            assert reply['ok'] is False
            assert 'volume' not in reply and 'muted' not in reply
        finally:
            await events.aclose()
            await live.aclose()
    asyncio.run(scenario())


def test_live_control_cancellation_revokes_old_candidate_before_late_result():
    from support.live_protocol import control_packet, open_control_session

    from dialogue.models import ControlResolution
    async def scenario():
        socket, live, _ = await open_control_session()
        events = live.events()
        try:
            await socket.incoming.put(control_packet(args={'action': 'mute'}))
            candidate = await asyncio.wait_for(anext(events), 1)
            await socket.incoming.put({'toolCallCancellation': {'ids': ['volume']}})
            cancelled = await asyncio.wait_for(anext(events), 1)
            assert cancelled.kind == 'control_cancelled' and cancelled.tool_ref == candidate.tool_ref
            assert (await live.resolve_control(candidate.tool_ref,
                ControlResolution('not_executed', reason='cancelled'))).status == 'stale'
        finally:
            await events.aclose()
            await live.aclose()
    asyncio.run(scenario())


def test_live_mixed_leave_and_volume_packet_is_rejected_and_cannot_be_partially_replayed():
    from support.live_protocol import control_packet, open_control_session, wait_responses
    async def scenario():
        socket, live, _ = await open_control_session()
        try:
            calls = [dict(id='leave', name='leave_voice_room', args={}),
                     dict(id='volume', name='control_voice_output', args={'action': 'increase'})]
            await socket.incoming.put({'toolCall': {'functionCalls': calls}})
            replies = await wait_responses(socket, 2)
            assert all(reply['response']['ok'] is False for reply in replies)
            await socket.incoming.put(control_packet(args={'action': 'increase'}))
            repeated = await wait_responses(socket, 3)
            assert repeated[-1]['response'] == replies[1]['response']
        finally:
            await live.aclose()
    asyncio.run(scenario())
