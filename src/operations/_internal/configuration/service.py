"""音量配置的一次读改写事务，复用共享文件锁和文档补丁。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from core.config_file_store import config_file_write_lock, replace_text_files_atomically
from operations.models import OutputVolume, VolumeSaveReceipt

from .document import patch_config_text, validate_config_text

logger = logging.getLogger(__name__)


class FileConfiguration:
    def __init__(self, path: Path, runtime_config: Callable[[], object]) -> None:
        self._path = path
        self._runtime_config = runtime_config

    async def save_output_volume(self, volume: OutputVolume) -> VolumeSaveReceipt:
        if not isinstance(volume, OutputVolume):
            raise TypeError("volume 必须是已解析的 OutputVolume")
        work = asyncio.create_task(asyncio.to_thread(self._save_output_volume, volume))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(work)
                break
            except asyncio.CancelledError:
                if work.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _save_output_volume(self, volume: OutputVolume) -> VolumeSaveReceipt:
        source_name = "VOICE_AGENT_CONFIG"
        with config_file_write_lock():
            with self._path.open(encoding="utf-8", newline="") as source:
                text = source.read()
            patched = patch_config_text(
                text, {"voice": {"output_volume": volume.value}},
                sources={"voice": source_name}, filename=str(self._path),
            )
            validate_config_text(patched, filename=str(self._path))
            runtime = self._runtime_config()
            missing = not hasattr(runtime, source_name)
            target = getattr(runtime, source_name, None)
            if not missing and not isinstance(target, dict):
                raise RuntimeError("运行语音配置不是字典，已取消保存")
            replace_text_files_atomically(((self._path, patched),))
            if missing:
                setattr(runtime, source_name, {"output_volume": volume.value})
            else:
                assert isinstance(target, dict)
                target["output_volume"] = volume.value
        logger.info("event=config.volume.saved field=voice.output_volume")
        return VolumeSaveReceipt(volume.value)
