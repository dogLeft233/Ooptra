"""配置保存的一次读改写事务，统一文件提交与所保存字段的运行同步。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Any

from core.config_file_store import config_file_write_lock, replace_text_files_atomically
from operations._internal.backups.storage import atomic_json
from operations._internal.mutation import deployment_access, mutation_lock, owns_mutation
from operations.api import ConfigurationGroupMerger
from operations.models import (
    ConfigurationChange,
    ConfigurationRequest,
    ConfigurationResult,
    ConfigurationSaveReceipt,
    OutputVolume,
    RevisionConflict,
    RuntimeApplyReport,
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

    def revision(self) -> str:
        return hashlib.sha256(self._path.read_bytes()).hexdigest()

    def _request_path(self, request_id):
        if not isinstance(request_id, str) or not request_id or any(
                char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-'
                for char in request_id) or len(request_id) > 128:
            raise ValueError('配置请求标识无效')
        # IDs are hashed for cross-platform filenames (Windows forbids colon).
        return self._path.parent / 'data/maintenance/config_requests' / (hashlib.sha256(request_id.encode()).hexdigest() + '.json')

    def _read_result(self, request_id):
        path = self._request_path(request_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding='utf-8'))
        if raw['state'] == 'pending':
            # File rename is the commit point. Reconcile a crash before receipt publication.
            raw['state'] = 'saved' if self.revision() == raw['revision'] else 'unknown'
            raw['_recovered_pending'] = raw['state'] == 'saved'
        return raw

    @staticmethod
    def _result(raw):
        return ConfigurationResult(raw['request_id'], raw['revision'], raw['state'],
            tuple((group, tuple(fields)) for group, fields in raw.get('changed', [])),
            tuple(raw.get('applied', [])), tuple(raw.get('failed', [])),
            tuple(raw.get('restart_required', [])))

    def result(self, request_id: str) -> ConfigurationResult | None:
        with nullcontext() if owns_mutation(self._path.parent) else mutation_lock(self._path.parent):
            raw = self._read_result(request_id)
            return self._result(raw) if raw else None

    def record_application(self, request_id: str, report: RuntimeApplyReport) -> ConfigurationResult:
        with nullcontext() if owns_mutation(self._path.parent) else mutation_lock(self._path.parent):
            raw = self._read_result(request_id)
            if raw is None or raw['state'] not in {'saved', 'applied', 'partial'}:
                raise ValueError('配置请求未确认保存，不能记录组件应用')
            if raw['revision'] != self.revision():
                raise RevisionConflict('配置已被新版本替代，禁止重放旧版本热应用')
            raw.pop('_recovered_pending', None)
            raw.update(asdict(report), state='partial' if report.failed else 'applied')
            atomic_json(self._request_path(request_id), raw)
            return self._result(raw)

    def apply_latest(self, request_id: str, change: ConfigurationChange) -> ConfigurationResult:
        """内部稀疏命令针对锁内最新配置合并；外部编辑器应显式传预期版本。"""
        request = ConfigurationRequest(request_id, '0' * 64, change)
        self._save_fields(change, request=request, latest=True)
        result = self.result(request_id)
        if result is None:
            raise RuntimeError('配置保存缺少回执')
        return result

    def apply(self, request: ConfigurationRequest) -> ConfigurationResult:
        if not isinstance(request, ConfigurationRequest):
            raise TypeError('需要已解析配置请求')
        self._save_fields(request.change, request=request)
        result = self.result(request.request_id)
        if result is None:
            raise RuntimeError('配置保存缺少回执')
        return result

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
        self._save_fields(change, required_runtime=frozenset({"voice"}), fail_if_busy=True)
        logger.info("event=config.volume.saved field=voice.output_volume")
        return VolumeSaveReceipt(volume.value)

    def save_fields(self, change: ConfigurationChange) -> ConfigurationSaveReceipt:
        return self._save_fields(change)

    def _save_fields(self, change: ConfigurationChange, *, required_runtime=frozenset(), request=None, latest=False, fail_if_busy=False):
        if not isinstance(change, ConfigurationChange):
            raise TypeError("change 必须是已解析的 ConfigurationChange")
        normalized = change.to_updates()
        sources = change.source_names()
        fingerprint = ""
        raw: dict[str, Any] = {}
        with (nullcontext() if owns_mutation(self._path.parent) else mutation_lock(self._path.parent, blocking=not fail_if_busy)), config_file_write_lock():
            access = deployment_access(self._path.parent)
            if not access.allowed:
                raise ValueError(access.reason)
            if request is not None:
                existing = self._read_result(request.request_id)
                # change repr deliberately hides values; include canonical frozen snapshot only in digest.
                fingerprint = hashlib.sha256(json.dumps(
                    [None if latest else request.expected_revision, normalized, sources], sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest()
                if existing is not None:
                    if existing.get('fingerprint') != fingerprint:
                        raise ValueError('配置请求 ID 已绑定其他变更')
                    if existing['state'] == 'unknown':
                        raise RevisionConflict('配置提交状态不确定，需读取当前版本后新建请求')
                    if existing.get('_recovered_pending') and self.revision() == existing['revision']:
                        namespace = {}
                        text = self._path.read_text(encoding='utf-8')
                        exec(compile(text, str(self._path), 'exec'), namespace)
                        self._sync_runtime(self._runtime_config(), namespace, normalized, sources)
                    return ConfigurationSaveReceipt(tuple((g, tuple(f)) for g, f in existing['changed']))
                if not latest and request.expected_revision != self.revision():
                    raise RevisionConflict('配置版本已变化，请重新读取后保存')
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
            if request is not None:
                raw = dict(request_id=request.request_id, fingerprint=fingerprint,
                    revision=hashlib.sha256(patched.encode('utf-8')).hexdigest(), state='pending',
                    changed=[(group, sorted(fields)) for group, fields in normalized.items()])
                atomic_json(self._request_path(request.request_id), raw)
            replace_text_files_atomically(((self._path, patched),))
            self._sync_runtime(runtime, namespace, normalized, sources)
            if request is not None:
                raw['state'] = 'saved'
                atomic_json(self._request_path(request.request_id), raw)
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
