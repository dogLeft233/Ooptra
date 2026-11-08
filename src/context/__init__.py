"""上下文管理工厂；应用在此装配，模块外不导入 _internal。"""

from pathlib import Path

from context.api import ConversationHistory, SourceBridge


def create_memory(path: str, max_turns: int = 30) -> ConversationHistory:
    from context._internal.memory.jsonl import MemoryStore

    return MemoryStore(path, max_turns)


def create_context_bridge(*, timeout: float = 4.0, outbox_path: Path | None = None) -> SourceBridge:
    from context._internal.sources.bridge import ContextBridge

    return ContextBridge(timeout=timeout, outbox_path=outbox_path)
