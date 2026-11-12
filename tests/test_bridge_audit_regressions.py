import ast
import asyncio
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oopz_sdk.adapters.onebot.v11.adapter import OneBotV11Adapter
from oopz_sdk.adapters.onebot.v11.event import to_v11_event
from oopz_sdk.adapters.onebot.v11.types import IdStore, make_user_source
from oopz_sdk.adapters.onebot.v12.adapter import OneBotV12Adapter
from oopz_sdk.exceptions import (
    OopzApiError,
    OopzAuthError,
    OopzConnectionError,
)
from oopz_sdk.models.event import MessageEvent
from oopz_sdk.models.message import Message as MessageModel
from oopz_sdk.services.message import Message
from operations import create_credential_store, patch_credentials
from operations.models import CredentialsError

CREDENTIAL_FIELDS = ("app_version", "device_id", "person_uid", "jwt_token")

def test_credentials_only_patch_target_dict_and_mixed_quotes(monkeypatch):
    text = 'OTHER = {"jwt_token": "untouched"}\n# 中文\nOOPZ_CONFIG = {\n'
    text += ',\n'.join(f"'{key}': 'old'" for key in CREDENTIAL_FIELDS) + '\n}\n'
    payload = {key: '新"value' for key in CREDENTIAL_FIELDS}
    result = patch_credentials(text, payload)
    nodes = ast.parse(result).body
    assert ast.literal_eval(nodes[0].value) == {'jwt_token': 'untouched'}
    assert ast.literal_eval(nodes[1].value) == payload
    assert '# 中文' in result


@pytest.mark.parametrize('config', [
    'OOPZ_CONFIG = {"device_id": "old"}',
    'OOPZ_CONFIG = dict(device_id="old")',
    'OOPZ_CONFIG = {"device_id": "old", "person_uid": "old", "jwt_token": unsafe(), "app_version": "old"}',
    'OOPZ_CONFIG = {"device_id": "old", "person_uid": "old", "jwt_token": "old", "app_version": "old", **extra}',
])
def test_invalid_credentials_config_preserves_both_files(tmp_path, monkeypatch, config):
    path = tmp_path / 'config.py'
    private = tmp_path / 'private_key.py'
    path.write_text(config, encoding='utf-8')
    private.write_text('old-key', encoding='utf-8')
    store = create_credential_store(config_path=path, private_key_path=private, runtime_config=lambda:SimpleNamespace())
    payload = {key: 'new' for key in CREDENTIAL_FIELDS}
    payload['private_key_pem'] = 'synthetic-key'
    with pytest.raises(CredentialsError):
        store.save(payload)
    assert path.read_text(encoding='utf-8') == config
    assert private.read_text(encoding='utf-8') == 'old-key'


@pytest.mark.parametrize('mutation', [
    "OOPZ_CONFIG |= {'jwt_token': 'stale'}",
    "OOPZ_CONFIG['jwt_token'] = 'stale'",
    "OOPZ_CONFIG.update(jwt_token='stale')",
    "alias = OOPZ_CONFIG\nalias['jwt_token'] = 'stale'",
    "if True:\n    OOPZ_CONFIG['jwt_token'] = 'stale'",
    "OOPZ_CONFIG, other = {'jwt_token': 'stale'}, 1",
    "del OOPZ_CONFIG['jwt_token']",
    "def change():\n    OOPZ_CONFIG.update(jwt_token='stale')\nchange()",
    "from settings import OOPZ_CONFIG",
    "from settings import *",
    "import settings as OOPZ_CONFIG",
    "import OOPZ_CONFIG.settings",
    "def OOPZ_CONFIG():\n    pass",
    "async def OOPZ_CONFIG():\n    pass",
    "class OOPZ_CONFIG:\n    pass",
    "def change(OOPZ_CONFIG):\n    pass",
    "try:\n    pass\nexcept Exception as OOPZ_CONFIG:\n    pass",
    "def change():\n    global OOPZ_CONFIG",
    "match value:\n    case OOPZ_CONFIG:\n        pass",
    "match value:\n    case [*OOPZ_CONFIG]:\n        pass",
    "match value:\n    case {'value': other, **OOPZ_CONFIG}:\n        pass",
])
def test_credentials_reject_dynamic_dict_changes(tmp_path, monkeypatch, mutation):
    original = 'OOPZ_CONFIG = ' + repr({key: 'old' for key in CREDENTIAL_FIELDS})
    test_invalid_credentials_config_preserves_both_files(tmp_path, monkeypatch, original + '\n' + mutation)


def test_credentials_reject_chained_alias_definition(tmp_path, monkeypatch):
    original = 'alias = OOPZ_CONFIG = ' + repr({key: 'old' for key in CREDENTIAL_FIELDS})
    test_invalid_credentials_config_preserves_both_files(tmp_path, monkeypatch, original)


@pytest.mark.parametrize('version,cq', [(11, False), (11, True), (12, False)])
def test_private_mentions_reach_real_service(tmp_path, version, cq):
    async def run():
        service = object.__new__(Message)
        bot = SimpleNamespace(messages=service)
        service._bot = bot
        service._config = SimpleNamespace(use_announcement_style=False)
        service.signer = SimpleNamespace(client_message_id=lambda: 'client', timestamp_us=lambda: '123')
        service.open_private_session = AsyncMock(return_value=SimpleNamespace(session_id='session'))
        service._request_data = AsyncMock(return_value={'messageId': 'sent', 'timestamp': '123'})
        if version == 11:
            adapter = OneBotV11Adapter(bot, 'self', db_path=tmp_path / 'ids.sqlite3')
            uid = adapter.ids.createId(make_user_source('person')).number
            message = f'[CQ:at,qq={uid}]' if cq else [{'type': 'at', 'data': {'qq': str(uid)}}]
            await adapter.send_private_msg({'user_id': uid, 'message': message})
        else:
            adapter = OneBotV12Adapter(bot, 'self', db_path=tmp_path / 'ids.sqlite3')
            await adapter.send_message({'detail_type': 'private', 'user_id': 'person',
                                        'message': [{'type': 'mention', 'data': {'user_id': 'person'}}]})
        service._request_data.assert_awaited_once()
        body = service._request_data.call_args.kwargs['body']['message']
        assert body['mentionList'][0]['person'] == 'person'
    asyncio.run(run())


@pytest.mark.parametrize('private', [True, False])
@pytest.mark.parametrize('failure', [OopzConnectionError('offline'), OopzApiError('busy', status_code=503), None])
def test_optional_profile_failure_keeps_message(tmp_path, private, failure):
    lookup = AsyncMock(side_effect=failure, return_value=None)
    event = MessageEvent.model_construct(is_private=private, message=MessageModel.model_construct(
        sender_id='sender', target='target', area='area', channel='channel', message_id='message',
        timestamp='123', text='hello', plain_text='hello', content='hello', segments=[],
    ))
    result = asyncio.run(to_v11_event(event, self_id='self', ids=IdStore(tmp_path / 'ids.db'),
                                    bot=SimpleNamespace(person=SimpleNamespace(get_person_info=lookup))))
    assert result['post_type'] == 'message'
    assert result['sender']['nickname'] == ''
    lookup.assert_awaited_once()


def test_profile_authentication_failure_is_not_hidden(tmp_path):
    event = MessageEvent.model_construct(is_private=True, message=MessageModel.model_construct(
        sender_id='sender', target='target', area='', channel='', message_id='message',
    ))
    lookup = AsyncMock(side_effect=OopzAuthError('expired'))
    with pytest.raises(OopzAuthError):
        asyncio.run(to_v11_event(event, self_id='self', ids=IdStore(tmp_path / 'ids.db'),
                                bot=SimpleNamespace(person=SimpleNamespace(get_person_info=lookup))))


def test_id_store_serializes_independent_instances(tmp_path, monkeypatch):
    stores = [IdStore(tmp_path / 'ids.db') for _ in range(12)]
    original = IdStore._new_unique_number

    def delayed(self, conn):
        time.sleep(0.02)
        return original(self, conn)

    monkeypatch.setattr(IdStore, '_new_unique_number', delayed)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda store: store.create_id('same'), stores))
    assert len({result.number for result in results}) == 1


def test_id_store_collision_retry_is_bounded(tmp_path, monkeypatch):
    store = IdStore(tmp_path / 'ids.db')
    monkeypatch.setattr('oopz_sdk.adapters.onebot.v11.types.random.randint', lambda *_: 10000000)
    assert store.create_id('one').number == 10000000
    with pytest.raises(RuntimeError, match='allocate'):
        store.create_id('two')
    assert store.create_id('one').number == 10000000


def test_id_store_threads_retry_numeric_collisions(tmp_path, monkeypatch):
    stores = [IdStore(tmp_path / 'ids.db') for _ in range(4)]
    choices = iter([10000000, 10000000, 10000001, 10000001, 10000002, 10000002, 10000003])
    monkeypatch.setattr('oopz_sdk.adapters.onebot.v11.types.random.randint', lambda *_: next(choices))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: stores[index].create_id(str(index)), range(4)))
    assert len({result.number for result in results}) == 4
    for index, result in enumerate(results):
        assert stores[index].resolve_id(result.number).source == str(index)


@pytest.mark.parametrize('failure', [OopzApiError('denied', status_code=401), OopzApiError('bad', status_code=400)])
def test_profile_nontransient_api_failure_propagates(tmp_path, failure):
    event = MessageEvent.model_construct(is_private=False, message=MessageModel.model_construct(
        sender_id='sender', target='target', area='area', channel='channel', message_id='message',
    ))
    lookup = AsyncMock(side_effect=failure)
    with pytest.raises(OopzApiError):
        asyncio.run(to_v11_event(event, self_id='self', ids=IdStore(tmp_path / 'ids.db'),
                                bot=SimpleNamespace(person=SimpleNamespace(get_person_info=lookup))))


def test_credentials_save_never_imports_user_configuration(tmp_path, monkeypatch):
    marker = tmp_path / 'executed'
    config = tmp_path / 'config.py'
    config.write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).touch()\nOOPZ_CONFIG = '
        + repr({key: 'old' for key in CREDENTIAL_FIELDS}), encoding='utf-8',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, 'config', raising=False)
    store = create_credential_store(config_path=config, private_key_path=tmp_path / 'private_key.py')
    payload = {key: 'new' for key in CREDENTIAL_FIELDS}
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    payload['private_key_pem'] = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    store.save(payload)
    assert not marker.exists()
