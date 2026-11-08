"""Live 端点 URL 拼接必须是幂等的：绝不能出现两个 ``?key=``。

这个文件存在的理由（2026-09-29 实测）：
``start_session`` 原来无条件写 ``url = f"{endpoint}?key={key}"``。用户把 key 直接
写在 ``gemini.base_url`` 里（Google 文档给的就是 ``...BidiGenerateContent?key=...``
这种形态，完全合法），URL 就变成了 ``...?key=<k>?key=<k>``。服务端解析出的 key
长度翻倍后**不返回 error 帧、直接关 socket** —— 现象是「bot 进房了但一句话不说」，
日志里连 ``Gemini Live session ready`` 都不出现，也没有任何报错指向 URL。

真实握手 A/B 实测：双 key → 收不到任何响应；单 key（同一个 key、同一个模型
``gemini-3.8-live``）→ ``setupComplete``。所以坏的是拼接，不是 key、也不是模型名。
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

from dialogue import (
    build_live_url,
    live_url_has_key,
)
from operations.models import VoiceAgentSettings

DEFAULT_LIVE_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
ENDPOINT = DEFAULT_LIVE_WS
WITH_KEY = f"{ENDPOINT}?key=old-key"


def _key_of(url: str) -> str:
    return (parse_qs(urlsplit(url).query).get("key") or [""])[0]


# ----------------------------------------------------------------------
# 纯函数：build_live_url
# ----------------------------------------------------------------------


def test_plain_endpoint_gets_one_key() -> None:
    url = build_live_url(ENDPOINT, "k1")
    assert url == f"{ENDPOINT}?key=k1"
    assert url.count("?") == 1
    assert _key_of(url) == "k1"


def test_endpoint_already_carrying_key_is_not_doubled() -> None:
    """回归锁：base_url 自带 key 时不能再拼一次。"""
    url = build_live_url(WITH_KEY, "k2")
    assert url.count("?") == 1, url
    assert url.count("key=") == 1, url
    assert _key_of(url) == "k2"  # 显式 api_key 优先
    assert "old-key" not in url


def test_explicit_key_overrides_blank_key_in_endpoint() -> None:
    url = build_live_url(f"{ENDPOINT}?key=", "k3")
    assert url.count("?") == 1
    assert _key_of(url) == "k3"


def test_existing_key_is_kept_when_api_key_empty() -> None:
    """只有 URL 里带 key 也要能用（api_key 留空是合法配置）。"""
    assert build_live_url(WITH_KEY, "") == WITH_KEY
    assert live_url_has_key(build_live_url(WITH_KEY, "")) is True


def test_other_query_params_are_preserved() -> None:
    url = build_live_url(f"{ENDPOINT}?alt=json", "k4")
    assert url.count("?") == 1
    assert "alt=json" in url
    assert _key_of(url) == "k4"


def test_key_is_replaced_among_other_params() -> None:
    url = build_live_url(f"{ENDPOINT}?alt=json&key=old", "k5")
    assert url.count("key=") == 1
    assert _key_of(url) == "k5"
    assert "alt=json" in url
    assert "old" not in url


def test_key_with_special_chars_is_percent_encoded() -> None:
    url = build_live_url(ENDPOINT, "a b&c=d")
    assert url.count("key=") == 1
    assert _key_of(url) == "a b&c=d"  # 解析回来是原值


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_endpoint_falls_back_to_default(blank: Any) -> None:
    url = build_live_url(blank, "k6")
    assert url.startswith(DEFAULT_LIVE_WS)
    assert _key_of(url) == "k6"


def test_no_key_anywhere_is_reported() -> None:
    assert live_url_has_key(ENDPOINT) is False
    assert live_url_has_key(f"{ENDPOINT}?key=") is False
    assert live_url_has_key("") is False


# ----------------------------------------------------------------------
# start_session：真正拿去建连的 URL 只能有一个 key
# ----------------------------------------------------------------------


class FakeMemory:
    def append(self, role: str, content: str, **kwargs: Any) -> dict:
        return {"role": role, "content": content}

    def set_persona(self, persona: str) -> None:
        pass


def _settings(**overrides: Any) -> VoiceAgentSettings:
    base = {
        "enabled": True,
        "gemini_api_key": "",
        "gemini_base_url": ENDPOINT,
        "gemini_model": "gemini-3.8-live",
        "gemini_voice": "Kore",
        "persona": "测试",
        "proxy": "",
    }
    base.update(overrides)
    return VoiceAgentSettings(**base)


def _capture_open(monkeypatch: pytest.MonkeyPatch, urls: list[str]) -> None:
    """把 open_live_ws 换成「只记录 URL 就炸」——足以验证拼接，不必真建会话。"""

    class Stop(Exception):
        pass

    async def fake_open(url: str, *, proxy: str | None = None, **kwargs: Any) -> Any:
        urls.append(url)
        raise Stop("captured")

    from support import dialogue_fixtures as transport

    monkeypatch.setattr(transport, "live_connector", fake_open)


def test_start_session_sends_single_key_when_base_url_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    _capture_open(monkeypatch, urls)
    backend = GeminiLiveBackend(
        _settings(gemini_api_key="cfg-key", gemini_base_url=f"{ENDPOINT}?key=cfg-key"),
        FakeMemory(),
    )
    with pytest.raises(Exception, match="captured"):
        asyncio.run(backend.start_session())

    assert len(urls) == 1
    assert urls[0].count("?") == 1, urls[0]
    assert _key_of(urls[0]) == "cfg-key"


def test_start_session_uses_key_from_url_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    _capture_open(monkeypatch, urls)
    backend = GeminiLiveBackend(
        _settings(gemini_api_key="", gemini_base_url=f"{ENDPOINT}?key=url-only-key"),
        FakeMemory(),
    )
    with pytest.raises(Exception, match="captured"):
        asyncio.run(backend.start_session())

    assert len(urls) == 1
    assert _key_of(urls[0]) == "url-only-key"


def test_start_session_without_any_key_raises_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []
    _capture_open(monkeypatch, urls)
    backend = GeminiLiveBackend(_settings(gemini_api_key=""), FakeMemory())
    with pytest.raises(RuntimeError, match="未配置"):
        asyncio.run(backend.start_session())
    assert urls == []


def test_default_endpoint_constant_is_the_one_settings_ships() -> None:
    """两处默认值不能漂移：settings 的默认 base_url 必须就是本模块的端点常量。"""
    assert VoiceAgentSettings().gemini_base_url == build_live_url("", "")
