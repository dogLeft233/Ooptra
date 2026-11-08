"""版本化维护记录；应用和父监督通过同一 CAS 入口交接单写者。"""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path

from operations._internal.backups.storage import atomic_json
from operations._internal.mutation import mutation_lock
from operations.models import RevisionConflict


def transition(root: Path, value: dict, *, role: str, now: float, locked=False):
    if not isinstance(value.get('id'), str) or not value['id']:
        raise ValueError('维护任务缺少标识')
    path = root / 'data/maintenance/job.json'
    with nullcontext() if locked else mutation_lock(root):
        try:
            current = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            current = None
        except (OSError, ValueError) as exc:
            raise ValueError('维护记录损坏，禁止覆盖') from exc
        if current is not None and not isinstance(current, dict):
            raise ValueError('维护记录结构无效')
        previous_revision = 0
        if current is not None and current.get('id') == value.get('id'):
            previous_revision = current.get('revision', 0)
            if value.get('revision', 0) != previous_revision:
                raise RevisionConflict('维护任务版本已变化，拒绝迟到阶段')
            owner = current.get('owner', 'application')
            if role != owner and not (role == 'supervisor' and current.get('phase') == 'awaiting_exit'):
                raise RevisionConflict('维护任务写入角色不匹配')
        value.update(schema=2, revision=previous_revision + 1, owner=role, updated_at=now)
        atomic_json(path, value)
        # Current record is the mutation barrier; history retains queries after later jobs.
        history = root / 'data/maintenance/jobs' / (hashlib.sha256(value['id'].encode()).hexdigest() + '.json')
        atomic_json(history, value)
