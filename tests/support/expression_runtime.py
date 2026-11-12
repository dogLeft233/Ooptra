"""流程测试的外部生成端口；使用真实ExpressionJob与渠道帧，而非复制播放器。"""
from dialogue import create_expression_job
from dialogue.models import DialogueCapabilities


class ScriptedExpression:
    def capabilities(self):
        return DialogueCapabilities()

    async def rewrite_announcement(self, kind, text):
        return text

    async def tts(self, text):
        return b"\x00\x01" * 320, 24000

    def generate(self, intent, prepared, *, tools=None):
        async def produce(emit):
            text = (await self.rewrite_announcement(intent.announcement_kind, intent.text)
                    if intent.kind == "announcement" else intent.text)
            if not text:
                raise ValueError("empty output")
            await emit("text_final", text=text)
            pcm, rate = await self.tts(text)
            await emit("audio", pcm16=pcm, sample_rate=rate)
        return create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)

    def bind_history(self, reader):
        pass

    async def aclose(self):
        pass
