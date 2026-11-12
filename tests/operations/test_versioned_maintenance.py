"""配置 CAS、幂等和跨进程交接的公开行为。"""
from types import SimpleNamespace

import pytest

from operations import create_configuration, deployment_access
from operations.models import ConfigurationChange, ConfigurationRequest, RevisionConflict


def test_config_request_preserves_version_and_retries_without_reapplying(tmp_path):
    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = {"output_volume": 50}\n')
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"output_volume": 50})
    config = create_configuration(path=path, runtime_config=lambda: runtime)
    initial = config.revision()
    change = ConfigurationChange.from_normalized(
        {'voice': {'output_volume': 70}}, sources={'voice': 'VOICE_AGENT_CONFIG'})
    request = ConfigurationRequest('request-1', initial, change)
    receipt = config.apply(request)
    assert receipt.revision != initial
    assert receipt.state == 'saved'
    assert config.apply(request) == receipt
    assert config.result('request-1') == receipt
    with pytest.raises(RevisionConflict):
        config.apply(ConfigurationRequest('request-2', initial, change))
    assert runtime.VOICE_AGENT_CONFIG['output_volume'] == 70


def test_candidate_and_corrupt_record_deny_configuration_mutation(tmp_path):
    import json
    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = {"output_volume": 50}\n')
    job = tmp_path / 'data/maintenance/job.json'
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps({'id': 'candidate', 'phase': 'checking'}))
    config = create_configuration(path=path, runtime_config=SimpleNamespace)
    assert deployment_access(tmp_path).allowed is False
    change = ConfigurationChange.from_normalized(
        {'voice': {'output_volume': 70}}, sources={'voice': 'VOICE_AGENT_CONFIG'})
    with pytest.raises(ValueError, match='维护'):
        config.save_fields(change)
    job.write_text('{invalid')
    assert deployment_access(tmp_path).allowed is False
    with pytest.raises(ValueError, match='维护'):
        config.save_fields(change)
    assert '50' in path.read_text()


def test_quiesce_failure_blocks_exit_and_retains_partial_resources(tmp_path):
    import asyncio

    async def scenario():
        from operations import create_maintenance
        from operations.models import QuiesceResult

        class Runtime:
            def __init__(self):
                self.exits = []
            async def quiesce(self, job_id, deadline):
                return QuiesceResult(('generation',), ('channel',), ('close_failed',))
            async def request_exit(self, ref):
                self.exits.append(ref)
                raise AssertionError('partial quiesce must not request exit')

        runtime = Runtime()
        service = create_maintenance(tmp_path, managed=True, runtime_control=runtime)
        # A real archive is validated and prepared through the public restore request.
        (tmp_path / 'config.py').write_text('WEBUI_CONFIG = {"enabled": True, "port": 3090}\n')
        backup = await service.create_backup()
        job = await service.start_restore(backup['id'])
        await service.close()
        status = service.job(__import__('operations.models', fromlist=['JobRef']).JobRef(job['id']))
        assert status.phase == 'blocked_degraded'
        assert status.closed == ('generation',)
        assert status.pending == ('channel',)
        assert runtime.exits == []
        assert deployment_access(tmp_path).allowed is False

    asyncio.run(scenario())


def test_quiesced_handoff_is_durable_and_repeated_plan_returns_same_job(tmp_path):
    import asyncio
    import json

    from operations import create_maintenance
    from operations.models import ExitResult, MaintenanceRequest, QuiesceResult

    class Runtime:
        def __init__(self):
            self.handoffs = []
        async def quiesce(self, job_id, deadline):
            return QuiesceResult(('generation', 'channel', 'context'))
        async def request_exit(self, ref):
            raw = json.loads((tmp_path / 'data/maintenance/job.json').read_text(encoding='utf-8'))
            assert raw['phase'] == 'awaiting_exit'
            assert raw['revision'] == ref.revision
            assert raw['handoff_token'] == ref.token
            self.handoffs.append(ref)
            return ExitResult(True)

    async def scenario():
        (tmp_path / 'config.py').write_text('WEBUI_CONFIG = {"enabled": True}\n')
        runtime = Runtime()
        service = create_maintenance(tmp_path, managed=True, runtime_control=runtime)
        backup = await service.create_backup()
        plan = await service.plan(MaintenanceRequest('restore', backup_id=backup['id']))
        first = await service.execute(plan)
        assert await service.execute(plan) == first
        await service.close()
        assert service.job(first).phase == 'awaiting_exit'
        assert len(runtime.handoffs) == 1
        assert not deployment_access(tmp_path).allowed
    asyncio.run(scenario())


def test_changed_config_invalidates_prepared_maintenance_plan(tmp_path):
    import asyncio

    from operations import create_maintenance
    from operations.models import MaintenanceRequest

    async def scenario():
        path = tmp_path / 'config.py'
        path.write_text('WEBUI_CONFIG = {"enabled": True}\n')
        service = create_maintenance(tmp_path, managed=True, runtime_control=SimpleNamespace())
        backup = await service.create_backup()
        plan = await service.plan(MaintenanceRequest('restore', backup_id=backup['id']))
        path.write_text('WEBUI_CONFIG = {"enabled": True, "port": 3095}\n')
        with pytest.raises(RevisionConflict):
            await service.execute(plan)
        assert service.snapshot().job is None
        await service.close()
    asyncio.run(scenario())


def test_supervisor_requires_owned_stopped_process_and_matching_handoff():
    from operations import create_supervisor
    supervisor = create_supervisor()
    process = SimpleNamespace(pid=25, ooptra_start_marker='actual', poll=lambda: None)
    job = dict(id='job', phase='awaiting_exit', service_pid=25,
               service_start_marker='actual', handoff_token='handoff')
    with pytest.raises(ValueError, match='尚未退出'):
        supervisor.confirm_stopped(process, job)
    process.poll = lambda: 0
    process.ooptra_start_marker = 'reused-pid'
    with pytest.raises(ValueError, match='身份'):
        supervisor.confirm_stopped(process, job)
    process.ooptra_start_marker = 'actual'
    receipt = supervisor.confirm_stopped(process, job)
    assert receipt.pid == 25 and receipt.handoff_token == 'handoff'


def test_saved_configuration_records_partial_application_and_rejects_old_retry(tmp_path):
    from operations.models import RuntimeApplyReport
    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = {"output_volume": 50}\n')
    config = create_configuration(path=path, runtime_config=lambda: SimpleNamespace())
    change = ConfigurationChange.from_normalized(
        {'voice': {'output_volume': 60}}, sources={'voice': 'VOICE_AGENT_CONFIG'})
    config.apply(ConfigurationRequest('partial', config.revision(), change))
    result = config.record_application('partial', RuntimeApplyReport(('configuration',), ('voice',)))
    assert result.state == 'partial' and result.failed == ('voice',)
    assert create_configuration(path=path).result('partial') == result
    path.write_text('VOICE_AGENT_CONFIG = {"output_volume": 70}\n')
    with pytest.raises(RevisionConflict):
        config.record_application('partial', RuntimeApplyReport(('voice',)))


def test_restore_keeps_validated_reference_and_refuses_replaced_archive(tmp_path):
    from operations import create_backup_store
    from operations.models import StoppedDeployment
    config = tmp_path / 'config.py'
    config.write_text('VALUE = 1\n')
    store = create_backup_store(tmp_path)
    first = store.create()
    verified = store.inspect(first['id'])
    config.write_text('VALUE = 2\n')
    second = store.create()
    store.path(first['id']).write_bytes(store.path(second['id']).read_bytes())
    with pytest.raises(ValueError, match='变化'):
        store.restore(verified, StoppedDeployment('isolated', 1, 'owned', 'token'))
    assert config.read_text() == 'VALUE = 2\n'


def test_configuration_application_holds_maintenance_gate_until_runtime_finishes(tmp_path):
    import asyncio

    from operations import configuration_transaction, create_maintenance

    async def scenario():
        saved, finish = asyncio.Event(), asyncio.Event()
        service = create_maintenance(tmp_path)
        async def operation():
            saved.set()
            await finish.wait()
            return 'applied'
        application = asyncio.create_task(configuration_transaction(tmp_path, operation))
        await saved.wait()
        with pytest.raises(ValueError, match='进行中'):
            await service.create_backup()
        application.cancel()
        await asyncio.sleep(0)
        assert not application.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await application
        assert (await service.create_backup())['id']
        await service.close()
    asyncio.run(scenario())


def test_saved_file_is_reconciled_after_receipt_publication_failure(tmp_path, monkeypatch):
    import os
    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = {"output_volume": 50}\n')
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={"output_volume": 50})
    config = create_configuration(path=path, runtime_config=lambda: runtime)
    change = ConfigurationChange.from_normalized(
        {'voice': {'output_volume': 70}}, sources={'voice': 'VOICE_AGENT_CONFIG'})
    request = ConfigurationRequest('crash-window', config.revision(), change)
    replace = os.replace
    publications = 0
    def failing_publication(source, target):
        nonlocal publications
        if 'config_requests' in str(target):
            publications += 1
            if publications == 2:
                raise OSError('synthetic receipt publication failure')
        return replace(source, target)
    monkeypatch.setattr(os, 'replace', failing_publication)
    with pytest.raises(OSError):
        config.apply(request)
    assert config.result(request.request_id).state == 'saved'
    monkeypatch.setattr(os, 'replace', replace)
    assert config.apply(request).state == 'saved'
    assert runtime.VOICE_AGENT_CONFIG['output_volume'] == 70


def test_credential_save_serializes_validated_rsa_key_instead_of_raw_pem_tail(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from operations import create_credential_store
    path = tmp_path / 'config.py'
    path.write_text('OOPZ_CONFIG = ' + repr(dict(app_version='old', device_id='old',
                    person_uid='old', jwt_token='old')) + '\n')
    private = tmp_path / 'private_key.py'
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    payload = dict(app_version='new', device_id='new', person_uid='new', jwt_token='new',
                   private_key_pem=pem + '\n"""\nraise RuntimeError("must_not_execute")\n#')
    store = create_credential_store(config_path=path, private_key_path=private,
                                    runtime_config=lambda: SimpleNamespace(OOPZ_CONFIG={}))
    store.save(payload)
    assert 'must_not_execute' not in private.read_text(encoding='utf-8')
    assert store.load_private_key().private_numbers() == key.private_numbers()


def test_credential_symlink_target_cannot_bypass_logical_deployment_gate(tmp_path):
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from operations import create_credential_store
    root, external = tmp_path / 'deployment', tmp_path / 'mounted'
    root.mkdir()
    external.mkdir()
    real_config = external / 'config.py'
    original = 'OOPZ_CONFIG = ' + repr(dict(app_version='old', device_id='old',
                    person_uid='old', jwt_token='old')) + '\n'
    real_config.write_text(original)
    logical_config = root / 'config.py'
    try:
        logical_config.symlink_to(real_config)
    except OSError:
        pytest.skip('当前系统未提供创建符号链接的权限')
    job = root / 'data/maintenance/job.json'
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps(dict(id='checking', phase='checking')))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = dict(app_version='new', device_id='new', person_uid='new', jwt_token='new',
        private_key_pem=key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    store = create_credential_store(config_path=logical_config, private_key_path=root / 'private_key.py')
    with pytest.raises(ValueError, match='维护'):
        store.save(payload)
    assert real_config.read_text() == original and logical_config.is_symlink()
    assert not (root / 'private_key.py').exists()


def test_backup_is_visible_only_after_complete_archive_is_published(tmp_path, monkeypatch):
    import asyncio
    import threading
    import zipfile

    from operations import create_backup_store
    path = tmp_path / 'config.py'
    path.write_text('VALUE = 1\n')
    store = create_backup_store(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = zipfile.ZipFile.writestr
    def pending(archive, name, data, *args, **kwargs):
        if name == 'config.py':
            entered.set()
            assert release.wait(2)
        return original(archive, name, data, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'writestr', pending)
    async def scenario():
        task = asyncio.create_task(asyncio.to_thread(store.create))
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            assert store.list() == []
        finally:
            release.set()
            receipt = await task
        assert [row['id'] for row in store.list()] == [receipt['id']]
        assert store.inspect(receipt['id']).backup_id == receipt['id']
    asyncio.run(scenario())


def test_duplicate_saved_request_does_not_execute_trusted_config_again(tmp_path):
    from operations.models import ConfigurationChange, ConfigurationRequest
    marker = tmp_path / 'executions'
    path = tmp_path / 'config.py'
    path.write_text('from pathlib import Path\n'
        + f'counter = Path({str(marker)!r})\n'
        + 'counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")\n'
        + 'VOICE_AGENT_CONFIG = {"output_volume": 50}\n')
    runtime = SimpleNamespace(VOICE_AGENT_CONFIG={'output_volume':50})
    store = create_configuration(path=path, runtime_config=lambda:runtime)
    request = ConfigurationRequest('repeat', store.revision(), ConfigurationChange.from_normalized(
        {'voice':{'output_volume':70}}, sources={'voice':'VOICE_AGENT_CONFIG'}))
    store.apply(request)
    executed = marker.read_text()
    assert int(executed) > 0
    store.apply(request)
    assert marker.read_text() == executed
