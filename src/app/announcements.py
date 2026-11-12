"""Internal event intentions are model instructions, never human chat history."""
import asyncio
import contextlib
from typing import Any, Literal

from dialogue import clean_announcement


async def run_announcement(agent: Any, kind: Literal["enter", "leave"], template: str, visit_id: str | None, epoch: int,
                            timeout: float) -> dict[str, Any]:
    session = agent.channels.snapshot().session
    agent._announcement_active = True
    agent._announcement_collecting = False
    agent.reply_scheduler.reset()

    def check_current() -> None:
        if (epoch != agent._operation_epoch or not agent._joined
                or (visit_id is not None and not agent.is_auto_visit_current(visit_id))):
            raise RuntimeError("stale visit")

    async def generate_and_play() -> dict[str, Any]:
        from app.flows import run_expression
        from dialogue import announcement_instruction

        check_current()
        if agent.duplex is None:
            raise RuntimeError("voice playback unavailable")
        if not await agent.wait_for_reply_end(timeout):
            raise RuntimeError("previous reply did not complete")
        check_current()
        agent._announcement_collecting = True
        agent._reply_generating = True
        if agent.live_mode:
            flow = getattr(agent, "_live_flow", None)
            if flow is None:
                raise RuntimeError("Live session unavailable")
            result = await flow.speak(announcement_instruction(kind, template), timeout=timeout, wait=True)
            if not result["ok"]:
                raise RuntimeError(result.get("error") or "announcement failed")
            text = clean_announcement(result["text"])
        else:
            result = await run_expression(backend=agent.backend, context=agent.context_service,
                channel=agent.channels, session=session, text=template, kind="announcement",
                user_key="announcement", current=lambda: epoch == agent._operation_epoch and agent._joined,
                timeout=timeout, announcement_kind=kind, reply_text_to_channel=agent.settings.reply_text_to_channel,
                subtitle_target=agent.subtitle_target)
            if result.outcome is None or result.outcome.status != "completed":
                raise RuntimeError("announcement playback failed")
            text = clean_announcement(result.reply_text)
            agent.turns += 1
        check_current()
        agent.last_reply = text
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
                disposed = not agent.live_mode
                if agent.live_mode:
                    # Cancel the session reader first: it may currently hold the
                    # output lock inside a pending browser push.
                    agent._operation_epoch += 1
                    try:
                        await asyncio.wait_for(agent._close_live_flow(), 3.0)
                    except Exception:
                        pass
                    else:
                        disposed = True
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
            agent.ingress.reset()
