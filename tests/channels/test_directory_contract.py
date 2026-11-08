"""平台目录公开边界：原始 SDK 响应不会逃出渠道模块。"""

import asyncio
from types import SimpleNamespace

import pytest

from channels import create_oopz_directory


def test_directory_normalizes_members_and_preserves_unknown_failure():
    async def run():
        async def joined():
            return [SimpleNamespace(area_id="a")]

        async def members(*, area):
            assert area == "a"
            return {"channelMembers": {"room": [
                {"uid": "self"}, {"uid": "human", "isBot": False},
                {"uid": "robot", "is_bot": "true"},
            ]}}

        bot = SimpleNamespace(
            config=SimpleNamespace(person_uid="self"),
            areas=SimpleNamespace(get_joined_areas=joined),
            channels=SimpleNamespace(get_voice_channel_members=members),
        )
        directory = create_oopz_directory(bot)
        areas = await directory.joined_areas()
        assert areas.status == "confirmed" and areas.area_ids == ("a",)
        result = await directory.voice_members("a")
        assert result.status == "confirmed"
        room = result.rooms[0]
        assert room.target.channel == "room"
        assert [(m.uid, m.is_self, m.is_bot) for m in room.members] == [
            ("self", True, False), ("human", False, False), ("robot", False, True),
        ]

        async def unavailable(*, area):
            raise OSError("private SDK response")

        bot.channels.get_voice_channel_members = unavailable
        failure = await directory.voice_members("a")
        assert failure.status == "unavailable" and failure.rooms == ()
        assert "private" not in repr(failure)

    asyncio.run(run())


def test_malformed_member_payload_is_unknown_not_an_empty_room():
    async def run():
        async def members(*, area):
            return {"channelMembers": {"room": ["malformed member"]}}

        directory = create_oopz_directory(SimpleNamespace(
            config=SimpleNamespace(person_uid="self"),
            channels=SimpleNamespace(get_voice_channel_members=members),
        ))
        result = await directory.voice_members("a")
        assert result.status == "invalid_response" and result.rooms == ()

    asyncio.run(run())


def test_directory_cancellation_reaches_external_query():
    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def members(*, area):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        directory = create_oopz_directory(SimpleNamespace(
            channels=SimpleNamespace(get_voice_channel_members=members),
        ))
        task = asyncio.create_task(directory.voice_members("a"))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("查询取消不能变成成功或空房间")
        assert stopped.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {},
    {"channelMembers": "invalid"},
    {"channelMembers": {"room": "invalid"}},
    {"channelMembers": {"room": [{"uid": "other", "isBot": "invalid"}]}},
    {"channelMembers": {"room": [{"uid": {"id": "other"}}]}},
    {"channelMembers": {False: []}},
])
def test_sdk_normalization_cannot_turn_invalid_source_into_confirmed_presence(payload):
    from oopz_sdk.models.channel import VoiceChannelMembersResult

    async def run():
        async def members(*, area):
            return VoiceChannelMembersResult.from_api(payload)

        directory = create_oopz_directory(SimpleNamespace(
            config=SimpleNamespace(person_uid="self"),
            channels=SimpleNamespace(get_voice_channel_members=members),
        ))
        observed = await directory.voice_members("a")
        assert observed.status == "invalid_response" and observed.rooms == ()

    asyncio.run(run())
