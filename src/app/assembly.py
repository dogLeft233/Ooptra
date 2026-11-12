"""语音 Agent 装配入口。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import config as runtime_config
from app.http import VoiceApiServer
from app.voice import VoiceCoordinator
from behavior import create_auto_visits as AutoVisitController
from behavior import create_visit_store as AutoVisitStateStore
from behavior import departure_template, parse_auto_visit_config
from channels.models import RoomTarget
from core.paths import project_path
from operations import apply_settings, create_configuration, load_voice_agent_settings
from operations.api import Configuration
from operations.models import OutputVolume

logger = logging.getLogger(__name__)

# 独立 VOICE_API 端口这些字段改了必须重启进程才能生效（socket 已经绑好了）
_API_RESTART_KEYS = ("host", "port")


def _configured_subtitle_target() -> RoomTarget | None:
    config = getattr(runtime_config, 'OOPZ_CONFIG', {})
    area = str(config.get('default_area') or '').strip()
    channel = str(config.get('default_channel') or '').strip()
    return RoomTarget(area, channel) if area and channel else None


class VoiceRuntime:
    def __init__(
        self, *, state_path: str | Path | None = None, configuration: Configuration | None = None,
    ) -> None:
        self._configuration = configuration if configuration is not None else create_configuration()
        self.agent_settings, self.api_settings = load_voice_agent_settings()
        self.agent = VoiceCoordinator(self.agent_settings, self.api_settings,
            default_target=lambda:(str(getattr(runtime_config, "OOPZ_CONFIG", {}).get("default_area") or ""),
                                   str(getattr(runtime_config, "OOPZ_CONFIG", {}).get("default_channel") or "")),
            departure_text=lambda area:departure_template(self.auto_visit.config, area),
            subtitle_target=_configured_subtitle_target)
        self.agent.set_output_volume_saver(self._save_output_volume)
        self.api = VoiceApiServer(self, self.api_settings)
        self.auto_visit = AutoVisitController(
            self.agent, parse_auto_visit_config(getattr(runtime_config, "VOICE_AUTO_VISIT_CONFIG", None)),
            AutoVisitStateStore(state_path or project_path("data", "voice_auto_visit.json")),
            enabled=lambda: self.agent_settings.enabled)
        self.agent.set_operation_callback(self.auto_visit.on_operation)
        self._bound_bot: Any = None
        self._started = False
        self._api_started = False
        self._stop_task: asyncio.Task | None = None
        self._retry_voice_fields: set[str] = set()
        self._retry_voice_toggle = False
        self._retry_api_toggle = False
        self._retry_visit = False

    @property
    def enabled(self) -> bool:
        return bool(self.agent_settings.enabled or self.api_settings.enabled)

    async def _save_output_volume(self, volume: int) -> None:
        parsed = OutputVolume(volume)
        await self._configuration.save_output_volume(parsed)

    def bind_bot(self, bot: Any) -> None:
        self.agent.bind_bot(bot)
        if bot is not self._bound_bot:
            self._bound_bot = bot
            self.auto_visit.set_bot_ready(False)
            self.agent.subscribe_presence(self.auto_visit.notify_presence)

    async def set_bot_ready(self, ready: bool) -> None:
        self.agent.set_connection_ready(ready)
        self.auto_visit.set_bot_ready(ready)
        if not ready:
            try:
                result = await self.agent.leave(source="system")
                if isinstance(result, dict) and not result.get("ok", True):
                    raise RuntimeError(result.get("error") or "无法确认语音房间已退出")
            except Exception:
                logger.exception("voice cleanup after Oopz disconnect failed")
                await self.auto_visit.pause()

    async def unbind_bot(self, bot: Any) -> None:
        if bot is self._bound_bot:
            await self.set_bot_ready(False)
            self._bound_bot = None
            self.agent.bind_bot(None)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_task = None
        # 独立 VOICE_API 端口默认关闭；语音接口已合并到 WebUI /api/*
        if self.api_settings.enabled and self.api_settings.port not in (0,):
            try:
                await self.api.start()
                self._api_started = True
            except Exception:
                logger.exception("VOICE_API start failed")
        try:
            await self.agent.start()
        except Exception:
            logger.exception("voice agent start failed")
        await self.auto_visit.start()

    async def stop(self) -> None:
        """尝试全部资源收尾；有失败就报告不完整，取消等待者仍观察收尾。"""
        task = self._stop_task
        if task is None or (task.done() and task.exception() is not None):
            task = asyncio.create_task(self._stop_components(), name='voice-runtime-stop')
            self._stop_task = task
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break
        if cancelled is not None:
            try:
                task.result()
            except Exception:
                logger.exception('取消等待后语音运行资源仍有未完成收尾')
            raise cancelled
        task.result()

    async def _stop_components(self) -> None:
        failures = []
        for name, stop in [('auto_visit', self.auto_visit.stop),
                           ('agent', self.agent.close), ('api', self.api.stop)]:
            try:
                await stop()
            except Exception:
                failures.append(name)
                logger.exception('event=voice.runtime.stop_failed resource=%s', name)
            else:
                if name == 'api':
                    self._api_started = False
        if failures:
            raise RuntimeError('语音运行资源停止未完成：' + ','.join(failures))
        self._started = False

    # ------------------------------------------------------------------
    # 热重载
    # ------------------------------------------------------------------

    async def reload_settings(self) -> dict[str, Any]:
        async with self.agent.output_controls.configuration_update():
            return await self._reload_settings_locked()

    async def _reload_settings_locked(self) -> dict[str, Any]:
        """重新读 config.py，把变化**真正应用**到运行中的对象上。

        过去 WebUI 保存只改了 config 模块的 dict，VoiceRuntime 里的 dataclass
        快照（agent.settings / backend.settings 与它是同一个对象）永不刷新，
        于是「保存成功但没生效、必须重启」。这里就地写回并重建派生对象。

        返回 ``{"changed", "api_changed", "notes", "restart_keys"}``。
        """
        fresh_agent, fresh_api = load_voice_agent_settings()
        fresh_visit = parse_auto_visit_config(getattr(runtime_config, "VOICE_AUTO_VISIT_CONFIG", None))
        visit_changed = fresh_visit != self.auto_visit.config
        applied_components = []
        failed_components = []
        notes: list[str] = []
        if visit_changed or self._retry_visit:
            try:
                await self.auto_visit.reload(fresh_visit)
            except Exception:
                self._retry_visit = True
                failed_components.append('auto_visit')
                notes.append('自动串门配置应用失败')
                logger.exception('event=config.apply.failed component=auto_visit')
            else:
                self._retry_visit = False
                applied_components.append('auto_visit')

        # 就地写回：runtime.agent_settings / agent.settings / backend.settings 同一对象
        changed = apply_settings(self.agent_settings, fresh_agent)

        # 独立端口的 host/port 不能热改（socket 已绑定），只同步其余字段
        api_changed: list[str] = []
        for key in ("enabled", "token"):
            new = getattr(fresh_api, key)
            if getattr(self.api_settings, key) != new:
                setattr(self.api_settings, key, new)
                api_changed.append(key)
        restart_keys = [
            key for key in _API_RESTART_KEYS
            if getattr(self.api_settings, key) != getattr(fresh_api, key)
        ]

        hot_reload_failed = False
        refreshing = list(dict.fromkeys([*changed, *sorted(self._retry_voice_fields)]))
        if refreshing:
            try:
                refreshed = await self.agent.refresh(refreshing)
                notes.extend(refreshed)
                if any('失败' in note for note in refreshed):
                    raise RuntimeError('语音组件未完成配置应用')
            except Exception as exc:
                logger.exception('voice agent refresh failed')
                self._retry_voice_fields.update(refreshing)
                failed_components.append('voice')
                hot_reload_failed = True
                notes.append(f'语音配置应用失败：{type(exc).__name__}')
            else:
                self._retry_voice_fields.difference_update(refreshing)
                applied_components.append('voice')

        # WebUI can enable voice even if the process booted with both switches off.
        if self.enabled and not self._started:
            await self.start()

        # 语音总开关的热切换
        if 'enabled' in changed or self._retry_voice_toggle:
            try:
                if self.agent_settings.enabled:
                    if not self.agent.running:
                        await self.agent.start()
                    notes.append("语音对话已启用")
                elif not self.agent_settings.enabled and self.agent.running:
                    await self.agent.stop()
                    notes.append("语音对话已停用")
            except Exception as exc:
                logger.exception('voice agent toggle failed')
                self._retry_voice_toggle = True
                failed_components.append('voice_lifecycle')
                hot_reload_failed = True
                notes.append(f'语音总开关切换失败：{type(exc).__name__}')
            else:
                self._retry_voice_toggle = False
                applied_components.append('voice_lifecycle')

        # 独立端口开关的热切换
        if ('enabled' in api_changed or self._retry_api_toggle) and self._started:
            try:
                if self.api_settings.enabled and not self._api_started:
                    await self.api.start()
                    self._api_started = True
                    notes.append("独立语音端口已启动")
                elif not self.api_settings.enabled and self._api_started:
                    await self.api.stop()
                    self._api_started = False
                    notes.append("独立语音端口已停止")
            except Exception as exc:
                logger.exception('VOICE_API toggle failed')
                self._retry_api_toggle = True
                failed_components.append('voice_api')
                hot_reload_failed = True
                notes.append(f'独立语音端口切换失败：{type(exc).__name__}')
            else:
                self._retry_api_toggle = False
                applied_components.append('voice_api')

        if restart_keys:
            notes.append("独立语音端口的监听地址/端口需重启才能生效")

        return {
            "changed": [*changed, *(["auto_visit"] if visit_changed else [])],
            "api_changed": api_changed,
            "notes": notes,
            "restart_keys": restart_keys,
            'hot_reload_failed': bool(failed_components) or hot_reload_failed,
            'applied_components': applied_components,
            'failed_components': failed_components,
        }


_runtime: VoiceRuntime | None = None


def get_voice_runtime() -> VoiceRuntime:
    global _runtime
    if _runtime is None:
        _runtime = VoiceRuntime()
    return _runtime
