"""验收控制器通过真实 HTTP 检查外部实例，测试不连接 Oopz 或模型。"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from scripts.run_bot_pair_acceptance import Instance, run_acceptance


@pytest.mark.parametrize("produce_audio,join_failure,cancel_run", [(True, False, False), (False, False, False), (False, True, False), (False, False, True)])
def test_pair_requires_completed_audio_and_always_leaves(tmp_path, produce_audio, join_failure, cancel_run):
    async def run():
        servers = []
        instances = []
        rooms = [False, False]
        opening = False
        joining = asyncio.Event()
        for i in range(2):
            root = tmp_path / str(i)
            (root / "logs").mkdir(parents=True)
            log = root / "logs/oopz_bot.log"
            log.write_text("old 流式语音回合完成：已输出音频=True\n" * 8, encoding="utf-8")

            async def handle(request, index=i, logfile=log):
                nonlocal opening
                assert request.headers["Authorization"] == "Bearer synthetic"
                if request.path == "/voice/join":
                    if join_failure and index == 0:
                        await joining.wait()
                        return web.json_response({"ok": False}, status=503)
                    if join_failure:
                        joining.set()
                        await asyncio.sleep(.02)
                    rooms[index] = True
                    return web.json_response({"ok": True})
                if request.path == "/voice/leave":
                    rooms[index] = False
                    return web.json_response({"ok": True})
                if request.path == "/voice/members":
                    return web.json_response({"ok": True, "members": [{"uid": f"peer-{1-index}", "live": True}]})
                if request.path == "/voice/speak":
                    opening = True
                    for instance in instances:
                        with (instance.root / "logs/oopz_bot.log").open("a", encoding="utf-8") as stream:
                            stream.write(("流式语音回合完成：已输出音频=True\n" if produce_audio else "流式语音回合完成：已输出音频=False\n") * 3)
                    return web.json_response({"ok": True, "pcm_bytes": 400})
                status = {"joined": rooms[index], "speaking": False, "cascade_turn_limit": 3,
                          "cascade_attempts": 3 if opening else 0, "turns": (4-index) if opening else 0,
                          "last_user_text": "对方真实输入", "last_reply": "自然接话"}
                return web.json_response({"ok": True, "status": status, **status})

            app = web.Application()
            app.router.add_route("*", "/{path:.*}", handle)
            server = TestServer(app)
            await server.start_server()
            servers.append(server)
            instances.append(Instance(root, str(server.make_url("")).rstrip("/"), "synthetic", f"peer-{i}"))
        try:
            report_path = tmp_path / "report.json"
            task = asyncio.create_task(run_acceptance(
                tuple(instances), area="a", channel="c", timeout=.2, poll_interval=.001,
                on_report=lambda report: report_path.write_text(json.dumps(report), encoding="utf-8"),
            ))
            if cancel_run:
                while not opening:
                    await asyncio.sleep(.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                report = json.loads(report_path.read_text(encoding="utf-8"))
            else:
                report = await task
            assert report["outcome"] == ("cancelled" if cancel_run else "RuntimeError" if join_failure else "completed" if produce_audio else "TimeoutError")
            assert report["completed_replies"] == ([3, 3] if produce_audio else [0, 0])
            assert report["left_confirmed"] == [True, True]
            await asyncio.sleep(.03)
            assert rooms == [False, False]
        finally:
            await asyncio.gather(*(server.close() for server in servers))

    asyncio.run(run())
