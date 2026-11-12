"""配置保存的一次读改写事务，统一文件提交与所保存字段的运行同步。"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.config_file_store import config_file_write_lock, replace_text_files_atomically
from operations.api import ConfigurationGroupMerger
from operations.models import (
    ConfigurationChange,
    ConfigurationSaveReceipt,
    OutputVolume,
    VolumeSaveReceipt,
)

from .document import patch_config_text, validate_config_text

logger = logging.getLogger(__name__)


def _nested_get(value, path):
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class FileConfiguration:
    def __init__(self, path: Path, runtime_config: Callable[[], object], *,
                 group_mergers: Mapping[str, ConfigurationGroupMerger] | None = None) -> None:
        self._path = path
        self._runtime_config = runtime_config
        # Only pure domain merge rules are injected; file/runtime I/O remains here.
        self._group_mergers = dict(group_mergers or {})

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
        if not isinstance(volume, OutputVolume):
            raise TypeError("volume 必须是已解析的 OutputVolume")
        change = ConfigurationChange.from_normalized(
            {"voice": {"output_volume": volume.value}}, sources={"voice": "VOICE_AGENT_CONFIG"})
        self._save_fields(change, required_runtime=frozenset({"voice"}))
        logger.info("event=config.volume.saved field=voice.output_volume")
        return VolumeSaveReceipt(volume.value)

    def save_fields(self, change: ConfigurationChange) -> ConfigurationSaveReceipt:
        return self._save_fields(change)

    def _save_fields(self, change: ConfigurationChange, *, required_runtime=frozenset()):
        if not isinstance(change, ConfigurationChange):
            raise TypeError("change 必须是已解析的 ConfigurationChange")
        normalized = change.to_updates()
        sources = change.source_names()
        with config_file_write_lock():
            with self._path.open(encoding="utf-8", newline="") as source:
                text = source.read()
            to_write = copy.deepcopy(normalized)
            merged_groups = set(normalized) & self._group_mergers.keys()
            if merged_groups:
                # Reject dynamic target bindings before executing trusted config
                # to read current values. Domain merges run inside the same lock.
                patch_config_text(text, normalized, sources=sources, filename=str(self._path))
                current: dict[str, Any] = {}
                exec(compile(text, str(self._path), "exec"), current)
                for group in merged_groups:
                    merged = self._group_mergers[group](current.get(sources[group]), normalized[group])
                    to_write[group] = {name: merged[name] for name in normalized[group]}
            patched = patch_config_text(text, to_write, sources=sources, filename=str(self._path))
            validate_config_text(patched, filename=str(self._path))
            namespace: dict[str, Any] = {}
            exec(compile(patched, str(self._path), "exec"), namespace)
            runtime = self._runtime_config()
            for group in required_runtime:
                if hasattr(runtime, sources[group]) and not isinstance(getattr(runtime, sources[group]), dict):
                    raise RuntimeError("运行语音配置不是字典，已取消保存")
            replace_text_files_atomically(((self._path, patched),))
            self._sync_runtime(runtime, namespace, normalized, sources)
        return ConfigurationSaveReceipt(tuple((group, tuple(sorted(fields)))
                                              for group, fields in normalized.items()))

    def _sync_runtime(self, runtime, namespace, updates, sources):
        # Update only saved paths, preserving unrelated environment overrides.
        for group, fields in updates.items():
            source = sources[group]
            fresh = namespace.get(source)
            if not isinstance(fresh, dict):
                continue
            target = getattr(runtime, source, None)
            if not isinstance(target, dict):
                setattr(runtime, source, copy.deepcopy(fresh))
                continue
            for name in fields:
                path = name.split(".")
                node = target
                for part in path[:-1]:
                    if not isinstance(node.get(part), dict):
                        node[part] = {}
                    node = node[part]
                node[path[-1]] = copy.deepcopy(_nested_get(fresh, path))
