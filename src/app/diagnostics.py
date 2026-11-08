"""Actionable bounded checks, using disposable resources for network probes."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import io
import sys
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit

from dialogue import create_backend, wav_to_pcm16
from dialogue.models import DiagnosticRequest
from operations import resolve_agent_proxy_url


def _short_wav(data: bytes) -> tuple[bytes, int]:
    if not isinstance(data, bytes) or not data or len(data) > 2 * 1024 * 1024:
        raise ValueError("自检 WAV 文件需小于 2 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate, frames = wav.getframerate(), wav.getnframes()
            expected = frames * wav.getnchannels() * wav.getsampwidth()
            if (not 8000 <= rate <= 96000 or wav.getnchannels() not in {1, 2}
                    or wav.getsampwidth() not in {1, 2, 4} or not 0 < frames / rate <= 10):
                raise ValueError("自检需要 10 秒以内、8~96 kHz 的单声道或双声道 WAV")
            if len(wav.readframes(frames)) != expected:
                raise ValueError("自检 WAV 文件不完整")
        return wav_to_pcm16(data)
    except (wave.Error, EOFError) as exc:
        raise ValueError("自检文件必须是有效的 PCM WAV") from exc


async def _close_backend(backend):
    cleanup = asyncio.create_task(backend.aclose())
    try:
        await asyncio.wait_for(asyncio.shield(cleanup), 3)
    except asyncio.TimeoutError:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
    except asyncio.CancelledError:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        raise
    except Exception:
        pass


async def diagnose(agent, *, network: bool = False, audio_wav: bytes | None = None) -> dict:
    sample = _short_wav(audio_wav) if audio_wav is not None else None
    network = network or sample is not None
    checks = []
    def add(key, title, state, detail):
        checks.append({"id": key, "title": title, "state": state, "detail": detail})
    elapsed = {}
    settings = agent.settings
    aliyun = settings.backend == "aliyun_deepseek"
    mimo = aliyun or (settings.backend or "").strip().lower() in {"mimo", "mimo_cascade", "cascade", "cascade_mimo"}
    add("python", "Python 环境", "pass" if sys.version_info >= (3, 10) else "fail",
        ".".join(map(str, sys.version_info[:3])))
    available = importlib.util.find_spec("playwright") is not None
    add("playwright", "语音浏览器依赖", "pass" if available else "fail",
        "已安装" if available else "请安装 requirements-optional.txt，再安装 Chromium")
    if available:
        try:
            # This dependency is optional and may be absent in headless installs.
            async_playwright = importlib.import_module("playwright.async_api").async_playwright
            async with async_playwright() as manager:
                installed = Path(manager.chromium.executable_path).is_file()
            add("chromium", "Chromium", "pass" if installed else "fail",
                "内核已安装" if installed else "运行 python -m playwright install chromium")
        except Exception:
            add("chromium", "Chromium", "fail", "浏览器驱动检查失败，请重新安装可选依赖与 Chromium")
    else:
        add("chromium", "Chromium", "skipped", "先安装 Playwright")
    connected = bool(agent.platform_bound and getattr(agent, "_connection_ready", True))
    add("oopz", "Oopz 连接", "pass" if connected else "fail",
        "已连接" if connected else "请先在账号页登录，并确认桥接连接状态")
    key = settings.mimo_api_key if mimo else settings.gemini_api_key
    from dialogue import live_url_has_key
    configured = (bool(settings.dashscope_api_key and settings.deepseek_api_key) if aliyun
                  else bool(key or (not mimo and live_url_has_key(settings.gemini_base_url))))
    add("credentials", "模型凭据", "pass" if configured else "fail",
        "已配置；有效性由网络检查确认" if configured else "请在语音模型配置中填写密钥")
    model = settings.deepseek_model if aliyun else settings.mimo_llm_model if mimo else settings.gemini_model
    add("model", "模型配置", "pass" if model else "fail", model or "请选择模型")
    valid_rate = 8000 <= settings.sample_rate_in <= 96000 and 8000 <= settings.sample_rate_out <= 96000
    add("sample_rate", "音频采样率", "pass" if valid_rate else "fail",
        f"输入 {settings.sample_rate_in} Hz / 输出 {settings.sample_rate_out} Hz")
    try:
        proxy = resolve_agent_proxy_url(settings.proxy)
        if proxy and urlsplit(proxy).scheme not in {"http", "https"}:
            raise ValueError("unsupported proxy")
        add("proxy", "模型网络出口", "pass", "已配置代理" if proxy else "直连/系统出口")
    except Exception:
        add("proxy", "模型网络出口", "fail", "代理地址无效，请检查协议、地址与端口")
    if not network or not configured:
        add("network", "模型连接", "skipped", "点击网络检查验证连接" if configured else "先配置模型密钥")
    else:
        started = time.monotonic()
        resources = []
        try:
            fresh = dataclasses.replace(settings)
            if sample is not None:
                fresh.reply_probability_percent = 100
                fresh.voice_leave_enabled = True
            def fresh_live():
                value = create_backend(dataclasses.replace(fresh))
                resources.append(value)
                return value
            async def retire(value):
                try:
                    await _close_backend(value)
                finally:
                    resources.remove(value)
            backend = fresh_live()
            async def probe():
                result = await backend.probe(DiagnosticRequest(*sample) if sample is not None else DiagnosticRequest())
                titles = {"asr": "短句语音识别", "intent": "语音控制语义判断", "tts": "独立语音合成"}
                for item in result.stages:
                    if item.name == "network":
                        if item.state == "fail":
                            raise RuntimeError("Model connection unavailable")
                        continue
                    add(item.name, titles[item.name], item.state,
                        "独立自检；未进入语音房" if not item.reason else item.reason)
                    elapsed[item.name] = item.elapsed_ms
            await asyncio.wait_for(probe(), 65 if sample is not None else 20)
            if sample is not None:
                failed = any(item["state"] == "fail" for item in checks if item["id"] in {"asr", "intent", "tts"})
                add("network", "音频模型自检", "fail" if failed else "pass",
                    "部分音频阶段失败，请查看分阶段结果" if failed else "独立短音频自检完成；未进入语音房")
            else:
                add("network", "模型连接", "pass", "级联对话模型请求成功；ASR 和 TTS 可用性需实际语音或试听确认" if mimo else "Live 会话建立成功；未加入语音房")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            add("network", "模型连接", "fail",
                f"{type(exc).__name__}：连接失败，请核对密钥、模型权限和网络出口")
        finally:
            for value in reversed(resources):
                await _close_backend(value)
        checks[-1]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    for check in checks:
        if check["id"] in elapsed:
            check["elapsed_ms"] = elapsed[check["id"]]
    return {"checks": checks, "passed": all(item["state"] != "fail" for item in checks),
            "network_checked": network and configured, "audio_checked": sample is not None and configured}
