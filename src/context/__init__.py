"""上下文管理工厂；应用在此装配，模块外不导入 _internal。"""

from pathlib import Path

from context.api import ConversationHistory, SourceBridge, TurnSubmission


def create_memory(path: str, max_turns: int = 30) -> ConversationHistory:
    from context._internal.memory.jsonl import MemoryStore

    return MemoryStore(path, max_turns)


def create_context_bridge(*, timeout: float = 4.0, outbox_path: Path | None = None) -> SourceBridge:
    from context._internal.sources.bridge import ContextBridge

    return ContextBridge(timeout=timeout, outbox_path=outbox_path)


def create_turn_submission(history: ConversationHistory | None, bridge: SourceBridge, *,
                           speaker: str) -> TurnSubmission:
    """固定本轮来源与存储，返回拥有受理、线程收尾与单次结果的提交能力。"""
    from context._internal.memory.submission import TurnSubmission as Submission
    return Submission(history, bridge.bind_turn(speaker=speaker))
