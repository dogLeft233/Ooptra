"""公开凭据存储保存与读取当前源，永不复用旧Python模块缓存。"""
import importlib.util
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from operations import create_credential_store, create_platform_configuration


def test_private_key_replacement_ignores_import_cache(tmp_path, monkeypatch):
    config_path, key_path = tmp_path / "config.py", tmp_path / "private_key.py"
    config_path.write_text('OOPZ_CONFIG = {"device_id":"old","person_uid":"old","jwt_token":"old","app_version":"old"}')
    store = create_credential_store(config_path=config_path, private_key_path=key_path,
                                    runtime_config=lambda:SimpleNamespace())
    keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]
    for index, key in enumerate(keys):
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        store.save(dict(private_key_pem=pem, device_id="new", person_uid="new", jwt_token="new", app_version="new"))
        if index == 0:
            spec = importlib.util.spec_from_file_location("private_key", key_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            monkeypatch.setitem(sys.modules, "private_key", module)
    platform = create_platform_configuration(config_path=config_path, private_key_path=key_path)
    assert platform.load_private_key().public_key().public_numbers() == keys[1].public_key().public_numbers()


def test_same_size_source_replacement_is_read_from_disk(tmp_path):
    key_path = tmp_path / "private_key.py"
    platform = create_platform_configuration(config_path=tmp_path/"config.py", private_key_path=key_path)
    key_path.write_text('def get_private_key():\n    return "old"\n')
    assert platform.load_private_key() == "old"
    key_path.write_text('def get_private_key():\n    return "new"\n')
    assert platform.load_private_key() == "new"
