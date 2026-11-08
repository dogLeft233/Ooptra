"""自动串门通过公开生命周期保留人工房间的冷却记录。"""
import asyncio
from types import SimpleNamespace

import pytest

from behavior import create_auto_visits, parse_auto_visit_config


def test_manual_leave_cooldown_survives_service_recreation(tmp_path):
    async def scenario():
        class Host:
            def status(self):
                return {"joined": False}

        path = tmp_path / "visits.json"
        service = create_auto_visits(
            Host(), parse_auto_visit_config({"areas": {"area": {"enabled": True}}}), path=path, enabled=lambda: True,
        )
        await service.on_operation(SimpleNamespace(
            kind="leave", source="manual", area="area", channel="room", visit_id="",
        ))
        await service.stop()
        restored = create_auto_visits(
            Host(), parse_auto_visit_config({"areas": {"area": {"enabled": True}}}), path=path, enabled=lambda: True,
        )
        assert restored.status()["areas"]["area"]["manual_cooldown_until"] is not None
        assert restored.status()["daily_count"] == 0
        await restored.stop()

    asyncio.run(scenario())


def test_parsed_visit_configuration_is_recursively_immutable(tmp_path):
    import pytest

    from behavior import effective_area

    config = parse_auto_visit_config({"areas": {"area": {"enabled": True}}})
    with pytest.raises(TypeError):
        config.areas["area"]["enabled"] = False
    with pytest.raises(TypeError):
        config.defaults["enter_prompts"].append("injected")
    detached = effective_area(config, "area")
    detached["enter_prompts"].append("allowed local edit")
    assert effective_area(config, "area")["enter_prompts"] == ["在玩什么游戏？"]


@pytest.mark.parametrize("failure", ["uncertain", "retired_transport", "missing_confirmation"])
def test_uncertain_join_retains_reservation_and_blocks_automatic_retry(tmp_path, failure):
    from datetime import datetime, timedelta, timezone

    from behavior import create_visit_store
    from channels.models import (
        AreaMembership,
        JoinedAreas,
        PlatformMember,
        RoomMembership,
        RoomTarget,
    )

    async def run():
        class Clock:
            value = 0.0

            def monotonic(self):
                return self.value

            def utcnow(self):
                return datetime(2026, 11, 8, tzinfo=timezone.utc) + timedelta(seconds=self.value)

            async def sleep(self, seconds):
                if not self.value:
                    self.value += seconds
                    await asyncio.sleep(0)
                else:
                    await asyncio.Event().wait()

        class Rng:
            def uniform(self, low, high):
                return low

            def random(self):
                return 0

            def choice(self, values):
                return values[0]

        class Directory:
            async def joined_areas(self):
                return JoinedAreas("confirmed", ("area",))

            async def voice_members(self, area):
                return AreaMembership(area, "confirmed", (
                    RoomMembership(RoomTarget(area, "room"), (PlatformMember("human", False, False),)),
                ))

        class Host:
            directory = Directory()
            operation_epoch = 0
            attempts = 0

            def status(self):
                if failure == "missing_confirmation":
                    return {"joined": False}
                return {"joined": False,
                        "room_state": "uncertain" if self.attempts and failure == "uncertain" else "idle",
                        "room_cleanup_pending": self.attempts > 0 and failure == "retired_transport"}

            async def join(self, *args, **kwargs):
                self.attempts += 1
                raise OSError("network timeout; platform admission is unknown")

        host, clock = Host(), Clock()
        store = create_visit_store(tmp_path / "visits.json")
        service = create_auto_visits(host, parse_auto_visit_config({
            "check_interval_minutes": [1, 1], "areas": {"area": {"enabled": True}},
        }), store, enabled=lambda: True, clock=clock, rng=Rng())
        service.set_bot_ready(True)
        await service.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while "本轮未能" not in service.status()["last_action"]:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(.001)
            assert store.snapshot(clock.utcnow())["pending"], "uncertain admission must retain quota reservation"
            assert service.status()["paused"] is True
            assert host.attempts == 1
        finally:
            await service.stop()
    asyncio.run(run())
