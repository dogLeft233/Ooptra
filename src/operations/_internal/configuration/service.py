"""音量配置的一次读改写事务，复用共享文件锁和文档补丁。"""

from __future__ import annotations

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

    def save_output_volume(self, volume: OutputVolume) -> VolumeSaveReceipt:
        if not isinstance(volume, OutputVolume):
            raise TypeError("volume 必须是已解析的 OutputVolume")
        with config_file_write_lock():
            with self._path.open(encoding="utf-8", newline="") as source:
                text = source.read()
            patched = patch_config_text(
                text, {"voice": {"output_volume": volume.value}},
                sources={"voice": "VOICE_AGENT_CONFIG"}, filename=str(self._path),
            )
            validate_config_text(patched, filename=str(self._path))
            runtime = self._runtime_config()
            target = getattr(runtime, "VOICE_AGENT_CONFIG", None)
            if not isinstance(target, dict):
                raise RuntimeError("运行语音配置不是字典，已取消保存")
            replace_text_files_atomically(((self._path, patched),))
            target["output_volume"] = volume.value
        logger.info("event=config.volume.saved field=voice.output_volume")
        return VolumeSaveReceipt(volume.value)
