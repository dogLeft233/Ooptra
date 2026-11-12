import asyncio
import locale
import shutil
import subprocess
from pathlib import Path

import pytest

from channels import create_duplex as VoiceDuplex
from oopz_sdk.transport.voice_browser import BrowserVoiceTransport


def test_completion_waits_for_source_and_audio_clock_tail():
    async def run():
        transport = BrowserVoiceTransport.__new__(BrowserVoiceTransport)
        statuses = iter([
            {"ok": True, "pending_sources": 1, "remaining_seconds": 0.02},
            {"ok": True, "pending_sources": 0, "remaining_seconds": 0.01},
            {"ok": True, "pending_sources": 0, "remaining_seconds": 0},
        ])
        calls = []

        async def browser(method):
            calls.append(method)
            return next(statuses)

        transport._run_on_browser = browser
        result = await VoiceDuplex(transport).wait_tts_complete(timeout=1)
        assert result["ok"]
        assert calls == ["agoraTtsStatus"] * 3
    asyncio.run(run())


def test_suspended_tail_times_out_instead_of_claiming_complete():
    async def run():
        transport = BrowserVoiceTransport.__new__(BrowserVoiceTransport)

        async def browser(method):
            return {"ok": True, "pending_sources": 1, "remaining_seconds": 5}

        transport._run_on_browser = browser
        assert not (await transport.wait_tts_complete(timeout=0.01))["ok"]
    asyncio.run(run())


def test_browser_leave_failure_is_propagated_and_room_preserved():
    async def run():
        transport = BrowserVoiceTransport.__new__(BrowserVoiceTransport)
        transport._started = True
        transport._oopz_uid = None
        transport._agora_uid = "1"
        transport._joined_room = "room"
        transport._joined_uid = "1"
        transport._voice_states = {}

        async def browser(method):
            return {"ok": False, "error": "network disconnected"}

        transport._run_on_browser = browser
        try:
            await transport.leave()
        except RuntimeError:
            pass
        else:
            raise AssertionError("failed browser leave reported as success")
        assert transport._joined_room == "room"
    asyncio.run(run())


def test_real_player_finish_keeps_scheduled_tail_until_clock_and_sources_end(monkeypatch, tmp_path):
    # English Windows runners default to cp1252; the real player contains Chinese comments.
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252", raising=False)
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *args: "cp1252")
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the actual player functions")
    source = (Path(__file__).resolve().parents[1] / "src/oopz_sdk/assets/voice/agora_player.html").read_text(encoding="utf-8")
    functions = source[source.index("window.agoraPushTtsPcm ="):source.index("// join 成功后挂上 duplex 钩子")]
    script = """
const assert = require('node:assert/strict');
const window = {};
let client = {};
let ttsGeneration = 0;
let ttsNextTime = 0;
let ttsPlaying = false;
const ttsSources = new Set();
const TTS_LEAD = 0.03;
const ttsDest = {};
const ttsGain = {};
let ttsVolume = 50;
window.agoraSetTtsVolume = () => ({ok: true});
const ttsCtx = {
  currentTime: 0, state: 'running',
  createBuffer: (_, length, rate) => ({ duration: length / rate, getChannelData: () => new Float32Array(length) }),
  createBufferSource: () => ({ connect() {}, start() {} }),
};
const ensureTtsTrack = async () => {};
const base64ToInt16 = () => new Int16Array(24000);
""" + functions + """
(async () => {
  await window.agoraPushTtsPcm('audio', 24000, false);
  const finished = await window.agoraPushTtsPcm('', 24000, true);
  assert.equal(finished.playing, true);
  assert.equal(window.agoraTtsStatus().pending_sources, 1);
  assert(window.agoraTtsStatus().remaining_seconds > 1);
  ttsCtx.currentTime = 2;
  assert.equal(window.agoraTtsStatus().pending_sources, 1);
  [...ttsSources][0].onended();
  assert.equal(window.agoraTtsStatus().pending_sources, 0);
  assert.equal(window.agoraTtsStatus().remaining_seconds, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    # A file avoids Python/Node stdin EOF timing differences on Windows runners.
    script_path = tmp_path / "player_completion.cjs"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(script_path)], text=True, encoding="utf-8",
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
