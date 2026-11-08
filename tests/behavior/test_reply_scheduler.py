"""级联输入的替换、冷却与代次隔离通过公开能力验证。"""
import asyncio

from behavior import create_reply_scheduler


def test_waiting_reply_uses_latest_input_and_reset_invalidates_old_completion():
    async def scenario():
        now = [10.0]
        sleeping = asyncio.Event()
        resume = asyncio.Event()
        async def sleep(seconds):
            sleeping.set()
            await resume.wait()
            now[0] += seconds

        scheduler = create_reply_scheduler(clock=lambda: now[0], sleep=sleep)
        scheduler.offer("user", b"first", 16000)
        first = await scheduler.next(2)
        assert first.pcm == b"first"
        scheduler.finish(first, cooldown=2)
        scheduler.offer("user", b"old", 16000)
        waiting = asyncio.create_task(scheduler.next(2))
        await sleeping.wait()
        scheduler.offer("user", b"new", 16000)
        resume.set()
        current = await waiting
        assert current.pcm == b"new"
        scheduler.reset()
        scheduler.finish(current, cooldown=99)
        scheduler.offer("user", b"another-session", 16000)
        assert (await scheduler.next(0)).pcm == b"another-session"

    asyncio.run(scenario())


def test_old_cooldown_waiter_does_not_consume_new_generation_input():
    async def run():
        now = [10.0]
        sleeping, release = asyncio.Event(), asyncio.Event()

        async def sleep(_seconds):
            sleeping.set()
            await release.wait()

        scheduler = create_reply_scheduler(clock=lambda: now[0], sleep=sleep)
        scheduler.offer("user", b"first", 16000)
        first = await scheduler.next(5)
        scheduler.finish(first, cooldown=5)
        scheduler.offer("user", b"old-room", 16000)
        waiting = asyncio.create_task(scheduler.next(5))
        await sleeping.wait()
        scheduler.reset()
        scheduler.offer("user", b"new-room", 16000)
        release.set()
        current = await asyncio.wait_for(waiting, .1)
        assert current.pcm == b"new-room"
    asyncio.run(run())


def test_reset_releases_old_cooldown_without_waiting_for_deadline():
    async def run():
        sleeping = asyncio.Event()

        async def sleep(_seconds):
            sleeping.set()
            await asyncio.Event().wait()

        scheduler = create_reply_scheduler(clock=lambda: 10.0, sleep=sleep)
        scheduler.offer("user", b"first", 16000)
        scheduler.finish(await scheduler.next(30), cooldown=30)
        scheduler.offer("user", b"old-room", 16000)
        waiting = asyncio.create_task(scheduler.next(30))
        await sleeping.wait()
        scheduler.reset()
        scheduler.offer("user", b"new-room", 16000)
        assert (await asyncio.wait_for(waiting, .1)).pcm == b"new-room"
    asyncio.run(run())


def test_cancelled_waiter_releases_timer_and_keeps_later_input():
    async def run():
        import contextlib

        now = [10.0]
        sleeping, stopped = asyncio.Event(), asyncio.Event()

        async def sleep(_seconds):
            sleeping.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        scheduler = create_reply_scheduler(clock=lambda: now[0], sleep=sleep)
        scheduler.offer("user", b"first", 16000)
        scheduler.finish(await scheduler.next(1), cooldown=1)
        scheduler.offer("user", b"cancelled", 16000)
        waiting = asyncio.create_task(scheduler.next(1))
        await sleeping.wait()
        scheduler.offer("user", b"latest", 16000)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        assert stopped.is_set()
        now[0] = 11.0
        assert (await asyncio.wait_for(scheduler.next(1), .1)).pcm == b"latest"
    asyncio.run(run())
