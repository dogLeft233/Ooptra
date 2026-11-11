"""上下文管理工厂；应用在此装配，模块外不导入 _internal。"""

from pathlib import Path

from context.api import ContextService, ConversationHistory, SourceBridge


def create_memory(path: str, max_turns: int = 30) -> ConversationHistory:
    from context._internal.memory.ledger import ContextLedger
    from core.paths import project_path

    legacy = Path(path)
    if not legacy.is_absolute():
        legacy = Path(project_path(path))
    return ContextLedger(legacy.with_name(legacy.stem + "_context.sqlite3"),
                         legacy_history=legacy, max_turns=max_turns, strict_import=False)


def create_context_bridge(*, timeout: float = 4.0, outbox_path: Path | None = None) -> SourceBridge:
    from context._internal.memory.ledger import ContextLedger
    from context._internal.sources.bridge import ContextBridge

    ledger = ContextLedger(outbox_path.with_name(outbox_path.stem + "_context.sqlite3") if outbox_path else ":memory:",
                           legacy_outbox=outbox_path)
    return ContextBridge(timeout=timeout, ledger=ledger, owns_ledger=True)


def create_context_service(history_path: str, *, max_turns: int = 30, timeout: float = 4.0,
                           outbox_path: Path | None = None) -> ContextService:
    """共享 SQLite 台账；旧 JSONL/outbox 只导入一次，原文件保留。"""
    from context._internal.service import ContextService as Service

    return Service(history_path, max_turns=max_turns, timeout=timeout, outbox_path=outbox_path)


def storage_configuration_changed(changed: set[str]) -> bool:
    """存储布局或容量变化要求关闭原上下文后重新装配。"""
    return bool({"memory_path", "memory_max_turns"} & changed)


def create_voice_context(history_path: str, *, max_turns: int = 30) -> ContextService:
    """语音上下文存储与旧outbox导入约定；调用者不拼装存储路径。"""
    return create_context_service(history_path, max_turns=max_turns,
        outbox_path=Path(history_path).with_name("astrbot_voice_outbox.json"))
