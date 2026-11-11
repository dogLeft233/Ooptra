from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from behavior.models import AttemptPermit, SpeechIntent
from channels.models import OutputFrame, SessionRef
from context.models import PreparedContext, RecallRequest, RememberRequest
from dialogue.models import (
    Allow,
    AudioInput,
    ControlInput,
    ControlInterpretation,
    ControlResolution,
    DiagnosticRequest,
    DiagnosticResult,
    DialogueCapabilities,
    GenerationEvent,
    GenerationEvidence,
    Hold,
    LiveEvent,
    LiveSessionRequest,
    LiveSnapshot,
    PreviewArtifact,
    PreviewFailure,
    PreviewRequest,
    ProposalRef,
    Recognition,
    Reject,
    ResolveReceipt,
    ToolRef,
)


class DialogueSettings(Protocol):
    """生成读取的配置视图；由装配传入，不依赖配置加载器或配置文件。"""

    @property
    def aliyun_asr_model(self) -> str: ...

    @property
    def aliyun_tts_model(self) -> str: ...

    @property
    def aliyun_tts_voice(self) -> str: ...

    @property
    def backend(self) -> str: ...

    @property
    def dashscope_api_key(self) -> str: ...

    @property
    def dashscope_ws_url(self) -> str: ...

    @property
    def deepseek_api_key(self) -> str: ...

    @property
    def deepseek_base_url(self) -> str: ...

    @property
    def deepseek_model(self) -> str: ...

    @property
    def gemini_api_key(self) -> str: ...

    @property
    def gemini_base_url(self) -> str: ...

    @property
    def gemini_model(self) -> str: ...

    @property
    def gemini_voice(self) -> str: ...

    @property
    def mimo_api_key(self) -> str: ...

    @property
    def mimo_asr_model(self) -> str: ...

    @property
    def mimo_base_url(self) -> str: ...

    @property
    def mimo_llm_model(self) -> str: ...

    @property
    def mimo_tts_model(self) -> str: ...

    @property
    def mimo_tts_voice(self) -> str: ...

    @property
    def persona(self) -> str: ...

    @property
    def proxy(self) -> str: ...

    @property
    def sample_rate_out(self) -> int: ...

    @property
    def voice_leave_enabled(self) -> bool: ...

    @property
    def voice_volume_enabled(self) -> bool: ...


class ContextToolPort(Protocol):
    """应用注入的共享上下文读取与授权工具端口；不暴露 outbox/pending。"""

    @property
    def active(self) -> bool: ...
    @property
    def persona(self) -> str: ...
    @property
    def context(self) -> dict[str, Any]: ...
    @property
    def history(self) -> list[dict[str, str]]: ...
    @property
    def session_id(self) -> str: ...
    async def recall(self, request: RecallRequest) -> dict[str, Any]: ...
    async def remember(self, request: RememberRequest) -> dict[str, Any]: ...



@runtime_checkable
class ConnectionReporter(Protocol):
    def connection_status(self) -> dict[str, object]: ...



class ExpressionJob(Protocol):
    """独占生成任务；events单订阅、有界背压，aclose只关闭本任务。"""
    generation_id: str
    action_id: str
    session: SessionRef
    def events(self) -> AsyncIterator[GenerationEvent]: ...
    def snapshot(self) -> GenerationEvidence: ...
    async def cancel(self) -> None: ...
    async def aclose(self) -> None: ...


class GeneratedAudio(Protocol):
    """上游事件的唯一消费者；失败转OutputSourceError，证据不表示已播放。"""
    def frames(self) -> AsyncIterator[OutputFrame]: ...
    def evidence(self) -> GenerationEvidence: ...
    async def aclose(self) -> None: ...


class LiveSession(Protocol):
    """持续输入/提议/许可通道；events单订阅，输出任务独立交渠道执行。

    当前连接失败关闭旧绑定；调用方从snapshot观察失败后，用新open_live重建。
    Allow相同action幂等；旧提议/来源返回stale，Hold不能延长提议硬截止。
    update_context只允许同一来源，已批准轮次保留其原上下文/工具授权。
    """
    live_session_id: str
    async def send_input(self, input: AudioInput) -> ResolveReceipt: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...
    async def resolve_turn(self, ref: ProposalRef, permission: Allow | Hold | Reject) -> ResolveReceipt: ...
    def claim_control(self, ref: ToolRef, action_id: str) -> ResolveReceipt: ...
    async def resolve_control(self, ref: ToolRef, result: ControlResolution) -> ResolveReceipt: ...
    async def update_context(self, prepared: PreparedContext, *, tools: ContextToolPort | None = None) -> ResolveReceipt: ...
    async def authorize_input(self, ref: ProposalRef, prepared: PreparedContext | None, *, tools: ContextToolPort | None = None) -> ResolveReceipt:
        """仅接受同轮定稿输入的原来源授权；不重写已许可表达的历史快照。"""
        ...
    async def speak(self, intent: SpeechIntent) -> ResolveReceipt: ...
    async def interrupt(self) -> None: ...
    def snapshot(self) -> LiveSnapshot: ...
    async def aclose(self) -> None: ...


class PreviewService(Protocol):
    @property
    def busy(self) -> bool: ...
    async def preview(self, settings: DialogueSettings, request: PreviewRequest) -> PreviewArtifact | PreviewFailure: ...
    async def generate(self, settings: DialogueSettings, kind: str, text: str, voice: str = "") -> dict: ...


class VoiceBackend:
    """语音大脑：输入一段 utterance PCM，输出文字 + 回话 PCM。"""

    name: str = "base"
    settings: DialogueSettings

    def capabilities(self) -> DialogueCapabilities:
        """声明可调用能力，不由调用方猜测供应商名。"""
        return DialogueCapabilities()

    async def tts(self, text: str) -> tuple[bytes, int]:
        """级联的固定文字合成；Live 不承诺逐字合成，应使用其表达能力。"""
        raise NotImplementedError("Fixed speech is unsupported by this backend")

    async def recognize(self, input: AudioInput, permit: AttemptPermit) -> Recognition:
        """级联识别能力；Live 不支持逐输入识别。"""
        raise NotImplementedError("Recognition is unsupported by this backend")

    async def interpret_control(self, input: ControlInput) -> ControlInterpretation:
        """返回解析后的候选或明确非法/不可用，不执行控制。"""
        raise NotImplementedError("Control interpretation is unsupported")

    def generate(self, intent: SpeechIntent, context: PreparedContext, *, tools: ContextToolPort | None = None) -> ExpressionJob:
        """启动已批准意图的独占表达任务，返回ExpressionJob。"""
        raise NotImplementedError("Cascade generation is unsupported")

    async def open_live(self, request: LiveSessionRequest, *, tools: ContextToolPort | None = None) -> LiveSession:
        """开启固定上下文来源的持续生成会话。"""
        raise NotImplementedError("Live generation is unsupported")

    async def probe(self, request: DiagnosticRequest) -> DiagnosticResult:
        """独占自检后端，禁止对活跃房间后端调用；外层负责aclose。"""
        raise NotImplementedError("Diagnostic probes are unsupported")

    async def aclose(self) -> None:
        """停止本实例的生成与供应商资源；不触碰渠道房间或写历史。"""
        return None
