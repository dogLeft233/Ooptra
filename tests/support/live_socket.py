"""外部WebSocket替身；供应商reader与session闸门保持真实。"""
import asyncio
import json


class Socket:
    closed = False
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
    def __aiter__(self):
        return self
    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)
    async def send(self, text):
        self.sent.append(json.loads(text))
        if "setup" in self.sent[-1]:
            await self.incoming.put({"setupComplete": {}})
    async def close(self):
        self.closed = True
        await self.incoming.put(None)
