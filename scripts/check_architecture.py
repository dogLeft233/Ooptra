"""检查五模块目录和静态导入契约，不导入应用或读取运行配置。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MODULES = frozenset({"channels", "context", "behavior", "dialogue", "operations"})
ADAPTERS = frozenset({"oopz_sdk", "webui"})
RETIRED_NAMESPACES = frozenset({"bridge", "onebot_v11", "oopz", "voice_agent"})
SUPPORT = frozenset({"app", "core"})
PUBLIC = frozenset({"api", "models"})
DEPENDENCIES = {
    "channels": frozenset(),
    "context": frozenset({"channels"}),
    "behavior": frozenset({"channels", "context"}),
    "dialogue": frozenset({"channels", "context", "behavior"}),
    "operations": frozenset(),
    "core": frozenset(),
}
IGNORED = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})


# 已退休的实现和命名空间不提供迁移豁免。
RETIRED_PATHS = {
    "voice_agent": "app（装配）及五业务模块",
    "bridge": "channels/_internal/adapters",
    "oopz": "channels/_internal/adapters 与 operations 的配置能力",
    "onebot_v11": "channels/_internal/adapters",
    "webui/oopz_login.py": "channels 的平台登录能力",
    "channels/_internal/legacy.py": "channels/_internal/adapters/voice.py",
    "voice_agent/backends": "dialogue",
    **{f"voice_agent/{name}.py": module for module, names in {
        "context": ("memory", "context_bridge"),
        "behavior": ("auto_visit", "auto_visit_settings", "auto_visit_state", "voice_control"),
        "channels": ("duplex", "output_controls", "vad", "operations"),
        "dialogue": ("ws_transport", "observation"),
        "operations": ("settings",),
    }.items() for name in names},
    **{f"webui/maintenance_{name}.py": "operations"
       for name in ("storage", "cleanup", "network", "worker")},
}


def imports(tree: ast.AST, package: tuple[str, ...]) -> list[tuple[int, tuple[str, ...]]]:
    """展开相对导入和 from-import 的名称，使私有导入不能藏在别名中。"""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, tuple(alias.name.split("."))) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[: len(package) - node.level + 1] if node.level else ()
            base = prefix + tuple(node.module.split(".")) if node.module else prefix
            found.append((node.lineno, base))
            found.extend((node.lineno, (*base, alias.name)) for alias in node.names)
    return found


def check(root: Path) -> list[str]:
    errors: list[str] = []
    source = root / "src"
    for retired, destination in RETIRED_PATHS.items():
        previous = source / retired
        if previous.is_file() or (previous.is_dir() and any(previous.rglob("*.py"))):
            errors.append(f"src/{retired}: 实现已迁移至 {destination}，请通过公开接口扩展")
    for entry in sorted(source.iterdir()):
        if entry.name in IGNORED or entry.name.startswith("."):
            continue
        if entry.is_dir() and entry.name not in MODULES | ADAPTERS | SUPPORT:
            errors.append(f"src/{entry.name}: 未登记的一级目录，请按五模块归属组织")
        elif entry.is_file() and entry.suffix in {".py", ".pyi"} and entry.name != "__init__.py":
            errors.append(f"src/{entry.name}: 业务代码应放入所属模块")

    for module in sorted(MODULES):
        directory = source / module
        if not directory.exists():
            errors.append(f"src/{module}: 缺少模块导航目录")
            continue
        if not (directory / "README.md").is_file():
            errors.append(f"src/{module}: 缺少模块 README.md")
        files = [p for p in directory.rglob("*") if p.suffix in {".py", ".pyi"}]
        if files:
            for required in ("__init__.py", "api.py"):
                if not (directory / required).is_file():
                    errors.append(f"src/{module}: 迁入代码时需要 {required}")
        for path in files:
            relative = path.relative_to(directory)
            if len(relative.parts) == 1:
                if path.stem not in {"__init__", "api", "models"}:
                    errors.append(f"{path.relative_to(root)}: 实现应放入 _internal/")
            elif relative.parts[0] != "_internal":
                errors.append(f"{path.relative_to(root)}: 实现子目录应放入 _internal/")

    paths = [*source.rglob("*.py"), *source.rglob("*.pyi")]
    if (root / "tests").exists():
        paths.extend((root / "tests").rglob("*.py"))
    paths.extend(p for p in root.glob("*.py") if p.name in {"main.py", "launcher.py"})
    for path in sorted(paths):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.parts[:2] == ("src", "oopz_sdk"):
            continue
        owner = relative.parts[1] if len(relative.parts) > 2 else ""
        in_source = relative.parts[0] == "src"
        module_owner = owner if owner in MODULES else ""
        package = relative.with_suffix("").parts[1:-1] if in_source else ()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(relative))
        except (SyntaxError, UnicodeError) as exc:
            errors.append(f"{relative}: 无法解析 Python: {exc}")
            continue
        for lineno, target in imports(tree, package):
            if not target:
                continue
            # src 不是运行时包前缀；同时识别它，防止通过另一种写法越过接口。
            if target[0] == "src":
                target = target[1:]
            if not target:
                continue
            destination = target[0]
            location = f"{relative}:{lineno}"
            if destination in RETIRED_NAMESPACES:
                errors.append(f"{location}: {destination} 已删除；通过 app 装配或五模块公开接口访问")
            if in_source and destination == "oopz_sdk" and not (
                owner == "channels" and relative.parts[2:4] == ("_internal", "adapters")
            ):
                errors.append(f"{location}: SDK 访问只允许渠道内部适配；通过 channels 公开接口访问")
            if destination in MODULES and destination != module_owner:
                if any(part.startswith("_") or part == "*" for part in target[1:]):
                    errors.append(f"{location}: 跨模块通过 {destination}.api 或 models 访问")
                elif len(target) > 2 and target[1] not in PUBLIC:
                    errors.append(f"{location}: 跨模块访问了非公开路径 {'.'.join(target)}")
            if (in_source and owner in DEPENDENCIES and destination in MODULES and destination != owner
                    and (destination not in DEPENDENCIES[owner] or (len(target) > 1 and target[1] != "models"))):
                errors.append(f"{location}: 禁止的依赖方向 {owner} → {'.'.join(target)}；按根架构仅引用允许的 models")
            if in_source and owner in MODULES and destination in SUPPORT and destination != "core":
                errors.append(f"{location}: 禁止的依赖方向 {owner} → {destination}；由 app 装配注入")
            if not in_source or not module_owner:
                continue
            contract = len(relative.parts) == 3 and path.stem in PUBLIC
            if contract and destination == module_owner and "_internal" in target[1:]:
                errors.append(f"{location}: api.py/models.py 应独立于内部实现")
            if destination == "webui":
                errors.append(f"{location}: 业务模块不能依赖 HTTP 适配；由 app 装配公开端口")
    return sorted(set(errors))


def main() -> int:
    errors = check(Path(__file__).resolve().parents[1])
    if errors:
        print("\n".join(errors))
        return 1
    print("五模块目录、公开接口、依赖方向与退休路径检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
