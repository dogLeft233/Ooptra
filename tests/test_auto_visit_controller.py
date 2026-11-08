from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from behavior import create_auto_visits as AutoVisitController
from behavior import create_visit_store as AutoVisitStateStore
from behavior import parse_auto_visit_config
from channels import create_oopz_directory


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.origin = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)
        self.sleepers = []

    def monotonic(self):
        return self.value

    def utcnow(self):
        return self.origin + timedelta(seconds=self.value)

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.value + seconds, future))
        await future

    def advance(self, seconds):
        self.value += seconds
        for at, future in self.sleepers:
            if at <= self.value and not future.done():
                future.set_result(None)


class FakeRandom:
    def __init__(self, chance=0.1):
        self.chance = chance
        self.draws = 0
        self.choices = []

    def uniform(self, low, high):
        return low

    def random(self):
        self.draws += 1
        return self.chance

    def choice(self, values):
        self.choices.append(list(values))
        return values[0]


class FakeAgent:
    def __init__(self):
        self.settings = SimpleNamespace(enabled=True)
        self.rooms = {"a": {"one": [{"uid": "human"}]},
                      "b": {"two": [{"uid": "human"}], "three": [{"uid": "human"}]}}
        self.calls = []
        self.joined = False
        self.visit = ""
        self.area = ""
        self.channel = ""
        self.source = ""
        self.operation_epoch = 0
        self.fail_queries = set()
        self.fail_join = False
        self.fail_leave = False
        self.callback = None
        self._bot = SimpleNamespace(
            config=SimpleNamespace(person_uid="bot"),
            channels=SimpleNamespace(get_voice_channel_members=self.members),
            areas=SimpleNamespace(get_joined_areas=self.joined_areas),
        )
        self.directory = create_oopz_directory(self._bot)

    async def joined_areas(self):
        return [SimpleNamespace(area_id=area) for area in self.rooms]

    async def members(self, area):
        if area in self.fail_queries:
            raise OSError("unknown membership")
        return {"channelMembers": self.rooms[area]}

    def status(self):
        return {"joined": self.joined, "area": self.area, "channel": self.channel,
                "join_source": self.source, "room_state": "joined" if self.joined else "idle",
                "room_cleanup_pending": False}

    def is_auto_visit_current(self, visit_id):
        return self.joined and self.source == "auto" and self.visit == visit_id

    def begin_auto_retirement(self, visit_id):
        return self.is_auto_visit_current(visit_id)

    async def join(self, area="", channel="", *, source="manual", visit_id="", expected_operation_epoch=None):
        if source == "auto" and (self.joined or expected_operation_epoch != self.operation_epoch):
            raise RuntimeError("stale auto admission")
        self.operation_epoch += 1
        self.calls.append(("join", area, channel, source))
        if self.fail_join:
            raise OSError("join failed")
        self.joined, self.area, self.channel = True, area, channel
        self.source, self.visit = source, visit_id
        if self.callback:
            await self.callback(SimpleNamespace(kind="join", source=source, area=area,
                                                channel=channel, visit_id=visit_id))
        return {"ok": True, "area": area, "channel": channel}

    async def leave(self, *, source="manual", expected_visit_id=None, expected_operation_epoch=None):
        if expected_operation_epoch is not None and expected_operation_epoch != self.operation_epoch:
            return {"ok": False, "error": "stale operation"}
        if expected_visit_id and not self.is_auto_visit_current(expected_visit_id):
            return {"ok": False, "stale": True}
        if self.fail_leave:
            raise OSError("leave failed")
        if not self.joined:
            return {"ok": True}
        event = SimpleNamespace(kind="leave", source=source, area=self.area,
                                channel=self.channel, visit_id=self.visit)
        self.calls.append(("leave", source))
        self.joined = False
        self.operation_epoch += 1
        if self.callback:
            await self.callback(event)
        return {"ok": True}

    async def wait_for_reply_end(self, timeout=30):
        self.calls.append(("wait", timeout))
        return True

    async def stop_current_reply(self):
        self.calls.append(("stop_reply",))

    async def speak_announcement(self, kind, template, *, expected_visit_id, timeout=20):
        self.calls.append(("speech", kind, template))
        return {"ok": True}


def scenario(tmp_path, *, raw=None, chance=0.1):
    config = parse_auto_visit_config(raw or {"areas": {"a": {"enabled": True},
                                                       "b": {"enabled": True}}})
    clock, rng, agent = FakeClock(), FakeRandom(chance), FakeAgent()
    store = AutoVisitStateStore(tmp_path / "visit.json")
    store.load()
    ctrl = AutoVisitController(agent, config, store, enabled=lambda: agent.settings.enabled, clock=clock, rng=rng)
    agent.callback = ctrl.on_operation
    ctrl.set_bot_ready(True)
    return ctrl, agent, clock, rng, store


async def turns():
    for _ in range(12):
        await asyncio.sleep(0)


def test_start_waits_full_interval_without_immediate_join(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await ctrl.start()
        await turns()
        assert ctrl.status()["next_check_at"] is not None
        assert not agent.calls
        clock.advance(599)
        await turns()
        assert not agent.calls
        await ctrl.stop()
    asyncio.run(run())


def test_equal_domain_selection_and_only_one_probability_draw(tmp_path):
    async def run():
        ctrl, agent, _, rng, _ = scenario(tmp_path, chance=0.99)
        await ctrl._check_once()
        assert rng.choices[0] == ["a", "b"]
        assert rng.draws == 1
        assert not agent.joined
        await ctrl.stop()
    asyncio.run(run())


def test_success_counts_once_and_announces_then_leaves(tmp_path):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        await ctrl._check_once()
        assert store.snapshot(clock.utcnow())["daily_count"] == 1
        await turns()
        assert ("speech", "enter", "在玩什么游戏？") in agent.calls
        await ctrl._finish_visit("scheduled")
        assert not agent.joined
        assert ("speech", "leave", "拜拜，我下了") in agent.calls
        assert store.snapshot(clock.utcnow())["global_cooldown_until"] is not None
        await ctrl.stop()
    asyncio.run(run())


def test_failed_join_rolls_back_reservation_without_trying_second_room(tmp_path):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        agent.fail_join = True
        await ctrl._check_once()
        snapshot = store.snapshot(clock.utcnow())
        assert snapshot["daily_count"] == 0
        assert not snapshot["pending"]
        assert len([call for call in agent.calls if call[0] == "join"]) == 1
        await ctrl.stop()
    asyncio.run(run())


def test_manual_leave_cools_only_departed_domain(tmp_path):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        await agent.join("a", "one")
        await agent.leave()
        snapshot = store.snapshot(clock.utcnow())
        assert snapshot["global_cooldown_until"] is None
        assert snapshot["area_cooldowns"]["a"] is not None
        await ctrl._check_once()
        assert agent.area == "b" and agent.joined
        assert snapshot["daily_count"] == 0
        await ctrl.stop()
    asyncio.run(run())


def test_daily_cap_and_global_cooldown_block_before_member_requests(tmp_path):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        store.set_global_cooldown(clock.utcnow() + timedelta(minutes=30))
        await ctrl._check_once()
        assert not agent.calls
        assert ctrl.status()["global_cooldown_until"] is not None
        await ctrl.stop()
    asyncio.run(run())


def test_query_failure_is_not_confirmed_empty_room(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await ctrl._check_once()
        agent.fail_queries.add("a")
        clock.advance(180)
        assert await ctrl._room_empty_confirmed() is False
        assert agent.joined
        await ctrl.stop()
    asyncio.run(run())


def test_only_self_or_bot_rooms_are_not_candidates(tmp_path):
    async def run():
        ctrl, agent, _, _, _ = scenario(tmp_path)
        agent.rooms = {"a": {"one": [{"uid": "bot"}, {"uid": "other", "isBot": True}]}}
        await ctrl._check_once()
        assert not agent.calls
        await ctrl.stop()
    asyncio.run(run())


def test_manual_takeover_invalidates_previous_auto_exit(tmp_path):
    async def run():
        ctrl, agent, _, _, _ = scenario(tmp_path)
        await ctrl._check_once()
        await agent.join("b", "two")
        await ctrl._finish_visit("scheduled")
        assert agent.joined and agent.area == "b"
        assert ("speech", "leave", "拜拜，我下了") not in agent.calls
        await ctrl.stop()
    asyncio.run(run())


@pytest.mark.parametrize("source", ["auto", "manual"])
def test_only_self_for_thirty_seconds_leaves_silently_with_original_cooldown(tmp_path, source):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        if source == "auto":
            await ctrl._check_once()
        else:
            await agent.join("a", "one")
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        await ctrl._check_empty_room()
        clock.advance(29)
        await ctrl._check_empty_room()
        assert agent.joined
        clock.advance(1)
        await ctrl._check_empty_room()
        assert not agent.joined
        assert ("speech", "leave", "拜拜，我下了") not in agent.calls
        snapshot = store.snapshot(clock.utcnow())
        assert snapshot["daily_count"] == (1 if source == "auto" else 0)
        if source == "auto":
            assert snapshot["global_cooldown_until"] is not None
        else:
            assert snapshot["global_cooldown_until"] is None
            assert snapshot["area_cooldowns"]["a"] is not None
        await ctrl.stop()
    asyncio.run(run())


def test_greeting_exception_keeps_bounded_stay(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)

        async def broken(*args, **kwargs):
            raise OSError("speech unavailable")

        agent.speak_announcement = broken
        await ctrl._check_once()
        await turns()
        assert agent.joined
        assert ctrl._leave_at == clock.monotonic() + 600
        await ctrl._finish_visit("scheduled")
        assert not agent.joined
        await ctrl.stop()
    asyncio.run(run())


def test_confirmation_io_failure_still_owns_bounded_visit(tmp_path, monkeypatch):
    async def run():
        ctrl, agent, _, _, store = scenario(tmp_path)

        def broken(*args):
            raise OSError("disk full")

        monkeypatch.setattr(store, "confirm", broken)
        await ctrl._check_once()
        assert agent.joined
        assert ctrl.status()["paused"]
        assert ctrl._leave_at is not None
        assert ctrl._visit_task is not None
        assert store.snapshot(ctrl.clock.utcnow())["pending"]
        await ctrl._finish_visit("scheduled")
        assert not agent.joined
        await ctrl.stop()
    asyncio.run(run())


def test_pause_preserves_current_visit_and_disabled_area_retires_it(tmp_path):
    async def run():
        ctrl, agent, _, _, _ = scenario(tmp_path)
        await ctrl._check_once()
        await turns()
        await ctrl.pause()
        assert agent.joined
        await ctrl.reload(parse_auto_visit_config({"areas": {"a": {"enabled": False}}}))
        for _ in range(100):
            await asyncio.sleep(0.002)
            if not agent.joined:
                break
        assert not agent.joined
        assert ("speech", "leave", "拜拜，我下了") in agent.calls
        assert ctrl.status()["paused"]
        await ctrl.stop()
    asyncio.run(run())


def test_stay_expiry_waits_no_more_than_30_seconds_then_farewell(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)

        async def busy(timeout):
            agent.calls.append(("wait", timeout))
            return False

        agent.wait_for_reply_end = busy
        await ctrl._check_once()
        await turns()
        clock.advance(600)
        for _ in range(100):
            await asyncio.sleep(0.002)
            if not agent.joined:
                break
        assert not agent.joined
        assert agent.calls.index(("wait", 30)) < agent.calls.index(("stop_reply",))
        assert agent.calls.index(("stop_reply",)) < agent.calls.index(("speech", "leave", "拜拜，我下了"))
        await ctrl.stop()
    asyncio.run(run())


def test_daily_limit_persists_and_manual_joins_are_not_blocked(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path, raw={
            "daily_limit": 1, "areas": {"a": {"enabled": True}}})
        await ctrl._check_once()
        await ctrl._finish_visit("scheduled")
        clock.advance(3600)
        before = len(agent.calls)
        await ctrl._check_once()
        assert len(agent.calls) == before
        reloaded = AutoVisitStateStore(tmp_path / "visit.json")
        reloaded.load()
        assert not reloaded.can_join("a", clock.utcnow(), 1, 0)
        await agent.join("a", "one")
        assert agent.joined and agent.source == "manual"
        assert reloaded.snapshot(clock.utcnow())["daily_count"] == 1
        await ctrl.stop()
    asyncio.run(run())


def test_manual_room_is_not_retired_when_area_disabled(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await agent.join("a", "one")
        await ctrl.reload(parse_auto_visit_config({"areas": {"a": {"enabled": False}}}))
        clock.advance(2000)
        await turns()
        await ctrl._check_once()
        assert agent.joined and agent.source == "manual"
        assert ctrl._leave_at is None
        await ctrl.stop()
    asyncio.run(run())


def test_unknown_membership_resets_empty_confirmation(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await ctrl._check_once()
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        assert not await ctrl._room_empty_confirmed()
        clock.advance(100)
        agent.fail_queries.add("a")
        assert not await ctrl._room_empty_confirmed()
        agent.fail_queries.clear()
        clock.advance(30)
        assert not await ctrl._room_empty_confirmed()
        clock.advance(30)
        assert await ctrl._room_empty_confirmed()
        await ctrl.stop()
    asyncio.run(run())


def test_returning_member_resets_empty_countdown(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await agent.join("a", "one")
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        await ctrl._check_empty_room()
        clock.advance(29)
        agent.rooms["a"]["one"].append({"uid": "human"})
        await ctrl._check_empty_room()
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        clock.advance(1)
        await ctrl._check_empty_room()
        clock.advance(29)
        await ctrl._check_empty_room()
        assert agent.joined
        clock.advance(1)
        await ctrl._check_empty_room()
        assert not agent.joined
        await ctrl.stop()
    asyncio.run(run())


@pytest.mark.parametrize("members", [
    [{"uid": "bot"}, {"uid": "other-bot", "isBot": True}],
    [{"uid": "bot"}, {}],
    [],
    [{"uid": "human"}],
])
def test_other_bots_or_unconfirmed_self_never_count_as_only_self(tmp_path, members):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await agent.join("a", "one")
        agent.rooms["a"]["one"] = members
        await ctrl._check_empty_room()
        clock.advance(60)
        await ctrl._check_empty_room()
        assert agent.joined
        await ctrl.stop()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["switch", "return", "event", "failure"])
def test_empty_exit_rechecks_room_and_ownership_before_leaving(tmp_path, change):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await agent.join("a", "one")
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        await ctrl._check_empty_room()
        clock.advance(30)
        original = agent.members
        queries = 0

        async def changing_members(area):
            nonlocal queries
            queries += 1
            if queries == 2:
                if change == "switch":
                    await agent.join("b", "two")
                elif change == "return":
                    agent.rooms["a"]["one"].append({"uid": "human"})
                elif change == "event":
                    ctrl.notify_presence("a", "one")
                else:
                    raise OSError("unknown membership")
            return await original(area)

        agent._bot.channels.get_voice_channel_members = changing_members
        await ctrl._check_empty_room()
        assert agent.joined
        assert not any(call[0] == "leave" for call in agent.calls)
        await ctrl.stop()
    asyncio.run(run())


def test_manual_empty_monitor_runs_when_auto_visits_are_off_and_paused(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path, raw={"areas": {}})
        agent.settings.enabled = False
        await ctrl.pause()
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        await agent.join("a", "one")
        await turns()
        for _ in range(6):
            clock.advance(5)
            # Disk persistence runs in a thread at retirement; yield real time too.
            for _ in range(20):
                await asyncio.sleep(0.001)
        assert not agent.joined
        assert ctrl.status()["paused"]
        await ctrl.stop()
    asyncio.run(run())


def test_stop_during_completed_member_query_does_not_restart_monitor_wait(tmp_path):
    async def run():
        ctrl, agent, _, _, _ = scenario(tmp_path)
        entered = asyncio.Event()
        tasks = {}

        async def members(area):
            if not tasks:
                tasks["loop"] = ctrl._loop
                # Stop is scheduled immediately before wait_for observes the completed query.
                tasks["stop"] = asyncio.create_task(ctrl.stop())
                entered.set()
            return {"channelMembers": {"one": [{"uid": "bot"}, {"uid": "human"}]}}

        agent._bot.channels.get_voice_channel_members = members
        await agent.join("a", "one")
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            done, _ = await asyncio.wait([tasks["stop"]], timeout=0.2)
            assert done, "stopped monitor must not enter another wait after query completion"
        finally:
            tasks["loop"].cancel()
            await asyncio.gather(tasks["loop"], tasks["stop"], return_exceptions=True)
    asyncio.run(run())


def test_same_room_rejoin_starts_a_fresh_empty_countdown(tmp_path):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await agent.join("a", "one")
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        await ctrl._check_empty_room()
        clock.advance(29)
        await agent.join("a", "one")
        await ctrl._check_empty_room()
        clock.advance(1)
        await ctrl._check_empty_room()
        assert agent.joined
        clock.advance(29)
        await ctrl._check_empty_room()
        assert not agent.joined
        await ctrl.stop()
    asyncio.run(run())


@pytest.mark.parametrize("after_failure", ["member_returns", "query_unknown"])
def test_failed_auto_empty_exit_does_not_retry_when_presence_changes(tmp_path, after_failure):
    async def run():
        ctrl, agent, clock, _, _ = scenario(tmp_path)
        await ctrl._check_once()
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        await ctrl._check_empty_room()
        original = agent.leave
        attempts = 0

        async def fails_once(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("leave failed")
            return await original(**kwargs)

        agent.leave = fails_once
        clock.advance(30)
        departure = asyncio.create_task(ctrl._check_empty_room())
        await turns()
        assert attempts == 1
        if after_failure == "member_returns":
            agent.rooms["a"]["one"].append({"uid": "human"})
            ctrl.notify_presence("a", "one")
        else:
            agent.fail_queries.add("a")
        clock.advance(2)
        await turns()
        await asyncio.wait_for(departure, timeout=1)
        assert agent.joined
        await ctrl._check_empty_room()
        clock.advance(60)
        await ctrl._check_empty_room()
        assert agent.joined and attempts == 1
        agent.fail_queries.clear()
        agent.rooms["a"]["one"] = [{"uid": "bot"}]
        await ctrl._check_empty_room()
        clock.advance(30)
        await ctrl._check_empty_room()
        assert not agent.joined and attempts == 2
        await ctrl.stop()
    asyncio.run(run())


def test_reply_stop_failure_does_not_prevent_retirement(tmp_path):
    async def run():
        ctrl, agent, _, _, _ = scenario(tmp_path)

        async def busy(timeout):
            return False

        async def broken():
            raise OSError("Live session unavailable")

        agent.wait_for_reply_end = busy
        agent.stop_current_reply = broken
        await ctrl._check_once()
        await ctrl._finish_visit("scheduled")
        assert not agent.joined
        assert ("speech", "leave", "拜拜，我下了") not in agent.calls
        await ctrl.stop()
    asyncio.run(run())


def test_manual_takeover_during_reservation_prevents_auto_join(tmp_path, monkeypatch):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        original = ctrl._write

        async def gated(method, *args):
            await original(method, *args)
            if method == "reserve":
                await agent.join("b", "two")

        monkeypatch.setattr(ctrl, "_write", gated)
        await ctrl._check_once()
        assert agent.joined and agent.area == "b" and agent.source == "manual"
        assert not store.snapshot(clock.utcnow())["pending"]
        assert store.snapshot(clock.utcnow())["daily_count"] == 0
        assert ctrl._leave_at is None
        await ctrl.stop()
    asyncio.run(run())


def test_pause_during_sdk_join_keeps_successful_visit_bounded(tmp_path, monkeypatch):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        original = agent.join

        async def join(*args, **kwargs):
            result = await original(*args, **kwargs)
            await ctrl.pause()
            return result

        monkeypatch.setattr(agent, "join", join)
        await ctrl._check_once()
        assert agent.joined
        assert ctrl._visit_task is not None and ctrl._leave_at == clock.monotonic() + 600
        assert ctrl.status()["paused"]
        assert store.snapshot(clock.utcnow())["daily_count"] == 1
        await ctrl._finish_visit("scheduled")
        assert not agent.joined
        await ctrl.stop()
    asyncio.run(run())


def test_wall_clock_forward_does_not_bypass_running_cooldown(tmp_path):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path)
        store.set_global_cooldown(clock.utcnow() + timedelta(minutes=30))
        await ctrl._check_once()
        clock.origin += timedelta(hours=1)
        await ctrl._check_once()
        assert not agent.joined
        clock.advance(1800)
        await ctrl._check_once()
        assert agent.joined
        await ctrl.stop()
    asyncio.run(run())


def test_wall_clock_rollback_does_not_extend_domain_cooldown(tmp_path):
    async def run():
        ctrl, agent, clock, _, store = scenario(tmp_path, raw={"areas": {"a": {"enabled": True}}})
        store.set_area_cooldown("a", clock.utcnow() + timedelta(minutes=30))
        await ctrl._check_once()
        clock.origin -= timedelta(hours=1)
        clock.advance(1800)
        await ctrl._check_once()
        assert agent.joined
        await ctrl.stop()
    asyncio.run(run())


def test_rebound_directory_cannot_admit_from_old_account_observation(tmp_path):
    async def run():
        ctrl, agent, _, _, _ = scenario(tmp_path)
        old = agent._bot

        async def joined():
            # A binding replacement may occur while a network request is pending.
            # New account is no longer a member of any configured area.
            replacement = SimpleNamespace(
                config=SimpleNamespace(person_uid="replacement"),
                channels=old.channels,
                areas=SimpleNamespace(get_joined_areas=lambda: None),
            )
            agent.directory = create_oopz_directory(replacement)
            return [SimpleNamespace(area_id="a")]

        old.areas.get_joined_areas = joined
        await ctrl._check_once()
        assert not agent.joined
        assert not any(call[0] == "join" for call in agent.calls)
        await ctrl.stop()

    asyncio.run(run())
