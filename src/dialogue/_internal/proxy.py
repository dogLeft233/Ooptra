def resolve_agent_proxy_url(proxy: str) -> str | None:
    """把 VOICE_AGENT_CONFIG.proxy 解析成 aiohttp/websockets 可用的代理 URL。

    返回 ``None`` 表示不指定代理（交给系统/库默认行为）。
    """
    from core.proxy_utils import resolve_proxy_settings

    settings = resolve_proxy_settings(proxy)
    if settings.mode != "explicit" or not settings.server:
        return None
    return str(settings.server)
