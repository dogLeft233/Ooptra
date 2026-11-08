"""Ooptra 入口：Oopz <-> OneBot v11 反向 WebSocket 桥接 + 本地 Web 控制台。"""

import asyncio
import contextlib
import os
import signal
import sys
import threading

if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from app.management import RuntimeManagement
from channels import create_platform_gateway as BridgeController
from channels import create_platform_state as BridgeState
from core.logger_config import setup_logger
from core.version import __version__
from operations import apply_runtime_overrides, deployment_access, validate_runtime_config
from webui.server import WebUIConsole

logger = setup_logger("Main")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(_sig: signal.Signals) -> None:
        stop_event.set()

    signals = [signal.SIGINT, signal.SIGTERM]
    # Windows 控制台没有 SIGTERM；Ctrl+Break（含 taskkill 的软关闭）走 SIGBREAK。
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)

    for sig in signals:
        try:
            loop.add_signal_handler(sig, request_stop, sig)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(stop_event.set))


def _install_managed_control(stop_event: asyncio.Event) -> None:
    if os.environ.get("OOPTRA_MANAGED") != "1":
        return
    loop = asyncio.get_running_loop()

    def listen():
        for line in sys.stdin:
            if line.strip() == "OOPTRA_STOP":
                loop.call_soon_threadsafe(stop_event.set)
                return

    threading.Thread(target=listen, name="managed-stop", daemon=True).start()


async def run() -> None:
    from config import WEBUI_CONFIG

    state = BridgeState()
    voice_runtime = None
    try:
        from app.assembly import get_voice_runtime

        voice_runtime = get_voice_runtime()
    except Exception:
        voice_runtime = None
    from operations import create_platform_configuration

    controller = BridgeController(state, configuration=create_platform_configuration(), voice=voice_runtime)
    stop_event = asyncio.Event()
    console = WebUIConsole(state, controller, config=WEBUI_CONFIG, voice_runtime=voice_runtime,
                           shutdown=stop_event.set)
    management = RuntimeManagement(voice_runtime, controller, stop_event.set)
    console.set_runtime_control(management)
    _install_signal_handlers(stop_event)
    _install_managed_control(stop_event)

    logger.info("=" * 56)
    logger.info("Ooptra v%s · Oopz <-> OneBot v11 反向 WebSocket 桥接", __version__)
    logger.info("=" * 56)

    # 先起控制台：即使凭据失效或对端还没起来，也能在网页上查看状态并登录。
    await console.start()
    # 让 VOICE_API 知道控制台的真实监听地址，避免两者抢同一个端口（见 api.py 的说明）。
    bound = console.bind_address
    if voice_runtime is not None and bound is not None:
        voice_runtime.api.set_webui_endpoint(bound[0], bound[1])
    # 候选版本父提交之前只开放查询，不能先连接渠道或启动自动行为。
    deployment_blocked = not deployment_access(_ROOT).allowed
    while not deployment_access(_ROOT).allowed and not stop_event.is_set():
        console.set_bootstrap_ready(True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), 0.25)
    if deployment_blocked and stop_event.is_set():
        await console.stop()
        return
    await controller.start()

    try:
        if voice_runtime is not None:
            await voice_runtime.start()
            if voice_runtime.enabled:
                logger.info("语音 Agent 已启动（API 走 WebUI /api/voice/*）")
            else:
                logger.info("语音对话未启用；可在 WebUI 开启，调度器已就绪")
        else:
            logger.info("语音 Agent 未启用（VOICE_AGENT_CONFIG.enabled=False）")
    except Exception:
        logger.exception("语音 Agent 启动失败，不影响文字桥接")

    # WebUI starts early for recovery; a response alone does not mean bootstrap finished.
    console.set_bootstrap_ready(True)
    try:
        await stop_event.wait()
    finally:
        console.set_bootstrap_ready(False)
        # 先拒绝新配置并收完已提交保存/热应用，再释放它们可能访问的业务资源。
        failures = []
        for name, resource in [('console', console), ('voice', voice_runtime), ('channels', controller)]:
            if resource is None:
                continue
            try:
                await resource.stop()
            except Exception:
                failures.append(name)
                logger.exception('event=application.stop_failed resource=%s', name)
        if failures:
            raise RuntimeError('应用资源停止未完成：' + ','.join(failures))
    logger.info("桥接已停止。")


def main() -> None:
    apply_runtime_overrides()
    validate_runtime_config()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
