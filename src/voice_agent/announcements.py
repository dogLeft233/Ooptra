"""Internal event intentions are model instructions, never human chat history."""
import asyncio
import contextlib
from typing import Any

from dialogue import clean_announcement


async def run_announcement(agent: Any, kind: str, template: str, visit_id: str | None, epoch: int,
                            timeout: float) -> dict[str, Any]:
    session = agent.channels.snapshot().session
    agent._announcement_active = True
    agent._announcement_collecting = False
    agent._announcement_done.clear()
    agent._announcement_audio = 0
    agent._announcement_text = ""
    agent._announcement_error = ""
    agent.reply_scheduler.reset()

    def check_current() -> None:
        if (epoch != agent._operation_epoch or not agent._joined
                or (visit_id is not None and not agent.is_auto_visit_current(visit_id))):
            raise RuntimeError("stale visit")

    async def generate_and_play() -> dict[str, Any]:
        check_current()
        if agent.duplex is None:
            raise RuntimeError("voice playback unavailable")
        if not await agent.wait_for_reply_end(timeout):
            raise RuntimeError("previous reply did not complete")
        check_current()
        agent._announcement_collecting = True
        if agent.live_mode:
            agent._reply_generating = True
            await agent.backend.speak_announcement(kind, template)
            await agent._announcement_done.wait()
            text = clean_announcement(agent._announcement_text)
            if agent._announcement_error:
                raise RuntimeError(agent._announcement_error)
            if not agent._announcement_audio:
                raise RuntimeError("announcement produced no audio")
            if not text:
                raise RuntimeError("announcement produced empty text")
        else:
            agent._reply_generating = True
            text = clean_announcement(await agent.backend.rewrite_announcement(kind, template))
            check_current()
            if not text:
                raise RuntimeError("announcement produced empty text")
            pcm, rate = await agent.backend.tts(text)
            check_current()
            if not pcm:
                raise RuntimeError("announcement produced no audio")
            agent._speaking = True
            chunk = max(2, int(agent.settings.sample_rate_out * 0.04) * 2)
            for offset in range(0, len(pcm), chunk):
                check_current()
                async with agent._live_output_lock:
                    result = await agent._push_room_pcm(session, pcm[offset:offset + chunk], rate)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "audio push failed")
                await asyncio.sleep(0.02)
            result = await agent._push_room_pcm(session, b"", rate, finish=True)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "audio finish failed")
            agent._reply_generating = False
        check_current()
        drain = await agent.duplex.wait_tts_complete(timeout)
        check_current()
        if not drain.get("ok"):
            raise RuntimeError(drain.get("error") or "playback did not complete")
        agent.last_reply = text
        await agent._record_output("", text, "announcement", session, "completed")
        if not agent.live_mode:
            agent.turns += 1
        return {"ok": True, "text": text, "error": ""}

    succeeded = False
    try:
        result = await asyncio.wait_for(generate_and_play(), max(0.0, timeout))
        succeeded = True
        return result
    except asyncio.TimeoutError:
        return {"ok": False, "text": "", "error": "announcement timed out"}
    except Exception as exc:
        return {"ok": False, "text": "", "error": str(exc)}
    finally:
        try:
            if not succeeded and epoch == agent._operation_epoch:
                agent._discard_live_audio = True
                disposed = not agent.live_mode
                if agent.live_mode:
                    # Cancel the session reader first: it may currently hold the
                    # output lock inside a pending browser push.
                    agent._operation_epoch += 1
                    try:
                        reset = getattr(agent.backend, "reset_reply_session", agent.backend.aclose)
                        await asyncio.wait_for(reset(), 3.0)
                    except Exception:
                        pass
                    else:
                        disposed = True
                async with agent._live_output_lock:
                    if agent.duplex is not None:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(agent.duplex.stop_tts(), 1.0)
                if agent.live_mode and disposed:
                    await agent._wire_live()
        finally:
            agent._announcement_active = False
            agent._announcement_collecting = False
            agent._reply_generating = False
            agent._speaking = False
            agent._user_buffers.clear()
            agent._vad.reset()
