"""目录和接口规则的负例，防止检查只在空的目标模块上通过。"""

from pathlib import Path

import pytest

from scripts.check_architecture import MODULES, check


def repository(tmp_path: Path) -> Path:
    for module in MODULES:
        directory = tmp_path / "src" / module
        directory.mkdir(parents=True)
        (directory / "README.md").write_text("# 模块导航\n", encoding="utf-8")
    return tmp_path


def write(root: Path, path: str, content: str = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    parts = Path(path).parts
    if parts[0] == "src" and parts[1] in MODULES:
        for name in ("__init__.py", "api.py"):
            required = root / "src" / parts[1] / name
            if not required.exists():
                required.touch()


@pytest.mark.parametrize("statement", [
    "import channels._internal.player",
    "from channels import _internal",
    "from channels._internal import player as play",
    "from src.channels._internal import player",
    "from channels.models import _PrivateState",
    "from channels import *",
])
def test_external_private_import_is_rejected(tmp_path: Path, statement: str) -> None:
    root = repository(tmp_path)
    write(root, "src/behavior/_internal/policy.py", statement)
    assert any("跨模块" in error for error in check(root))


def test_public_contracts_and_own_internal_imports_are_allowed(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root, "src/behavior/_internal/policy.py", "from channels.models import SessionRef\n")
    write(root, "src/behavior/__init__.py", "from ._internal.policy import Policy\n")
    write(root, "tests/behavior/test_policy.py", "from behavior._internal import policy\n")
    assert check(root) == []


def test_relative_implementation_import_in_contract_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root, "src/behavior/api.py", "from ._internal.policy import Policy\n")
    assert any("独立于内部实现" in error for error in check(root))


def test_new_source_root_and_loose_implementation_are_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root, "src/services/manager.py")
    write(root, "src/behavior/policy.py")
    errors = check(root)
    assert any("未登记的一级目录" in error for error in errors)
    assert any("实现应放入" in error for error in errors)


def test_migrated_module_requires_public_entry_and_navigation(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "src/behavior/README.md").unlink()
    write(root, "src/behavior/_internal/policy.py")
    (root / "src/behavior/api.py").unlink()
    errors = check(root)
    assert any("README.md" in error for error in errors)
    assert any("需要 api.py" in error for error in errors)


def test_legacy_dependencies_are_localized(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root, "src/behavior/_internal/policy.py", "from voice_agent import agent\n")
    assert any("旧模块依赖" in error for error in check(root))
    write(root, "src/behavior/_internal/policy.py")
    write(root, "src/behavior/_internal/legacy.py", "from voice_agent import agent\n")
    write(root, "src/channels/_internal/adapters/oopz.py", "from oopz_sdk import OopzBot\n")
    assert check(root) == []


def test_invalid_python_cannot_silently_skip_import_checks(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root, "src/behavior/api.py", "def broken(:\n")
    assert any("无法解析 Python" in error for error in check(root))


@pytest.mark.parametrize("owner, statement", [
    ("channels", "from behavior.models import SpeechIntent"),
    ("context", "from channels.api import Channel"),
    ("behavior", "from dialogue.models import Recognition"),
    ("operations", "from context.models import ContextRef"),
    ("core", "import channels.models"),
])
def test_forbidden_dependency_direction_is_rejected(tmp_path: Path, owner: str, statement: str) -> None:
    root = repository(tmp_path)
    write(root, f"src/{owner}/_internal/dependency.py", statement)
    assert any("依赖方向" in error for error in check(root))


@pytest.mark.parametrize("old_path", [
    "src/voice_agent/memory.py", "src/voice_agent/settings.py",
    "src/voice_agent/backends/mimo_cascade.py", "src/webui/maintenance_worker.py",
])
def test_migrated_implementation_cannot_return_to_legacy_directory(tmp_path, old_path):
    root = repository(tmp_path)
    write(root, old_path)
    assert any("已迁移" in error for error in check(root))
