"""config.py 里 ``proxy`` 字段的解析。

支持四种写法：

- ``""`` / 未设置：系统代理。aiohttp 会读 ``HTTP_PROXY`` / ``HTTPS_PROXY`` /
  ``ALL_PROXY`` / ``NO_PROXY`` 环境变量；
- ``False`` 或 ``"direct"`` / ``"off"`` 等：直连，忽略系统代理；
- 别名 ``"clash"`` / ``"mihomo"`` 等：按 ``PROXY_ALIAS_CONFIG`` 拼出本机代理地址；
- 显式地址 ``http://host:port`` / ``socks5://host:port``，可带用户名密码。

这里只负责解析，真正装配到 SDK 传输层的是 ``src/oopz/proxy_transport.py``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

_DIRECT_VALUES = {"0", "false", "no", "none", "off", "direct"}

# 本地代理客户端（Clash / mihomo）的默认监听端口与回环地址。
# 集中定义，避免在多个别名里重复写死同一组端口；可被 config.PROXY_ALIAS_CONFIG 覆盖。
_DEFAULT_LOCAL_PROXY_HOST = "127.0.0.1"
_DEFAULT_CLASH_HTTP_PORT = 7890
_DEFAULT_CLASH_SOCKS_PORT = 7891


def _build_proxy_aliases() -> dict[str, str]:
    """构建 clash/mihomo 等别名 → 代理 URL 的映射。

    端口与回环地址默认沿用 Clash 习惯值，可通过 ``config.PROXY_ALIAS_CONFIG``
    覆盖（改了本地代理监听端口时无需改代码）。config 不可用时回退默认值。
    """
    host = _DEFAULT_LOCAL_PROXY_HOST
    http_port = _DEFAULT_CLASH_HTTP_PORT
    socks_port = _DEFAULT_CLASH_SOCKS_PORT
    try:
        from config import PROXY_ALIAS_CONFIG

        host = str(PROXY_ALIAS_CONFIG.get("host") or host)
        http_port = int(PROXY_ALIAS_CONFIG.get("http_port") or http_port)
        socks_port = int(PROXY_ALIAS_CONFIG.get("socks_port") or socks_port)
    except Exception:
        pass

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    http = f"http://{host}:{http_port}"
    socks = f"socks5://{host}:{socks_port}"
    return {
        "clash": http,
        "clash-http": http,
        "clash-mixed": http,
        "clash-socks": socks,
        "mihomo": http,
        "mihomo-socks": socks,
    }


_PROXY_ALIASES = _build_proxy_aliases()

_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "socks4": 1080,
    "socks4a": 1080,
    "socks5": 1080,
    "socks5h": 1080,
}
_SUPPORTED_SCHEMES = set(_DEFAULT_PORTS)
_PROXY_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)


@dataclass(frozen=True)
class ProxySettings:
    mode: str
    raw: str = ""
    server: str | None = None
    scheme: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode == "explicit" and bool(self.server)

    @property
    def is_socks(self) -> bool:
        return self.enabled and str(self.scheme or "").startswith("socks")


def _config_proxy_value():
    try:
        from config import OOPZ_CONFIG
    except Exception:
        return ""
    return OOPZ_CONFIG.get("proxy", "")


def _normalize_proxy_value(proxy_value=None):
    value = _config_proxy_value() if proxy_value is None else proxy_value
    if value is False:
        return False
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return ""
    alias = _PROXY_ALIASES.get(value.lower())
    return alias or value


def _parse_proxy_url(proxy_url: str) -> ProxySettings:
    candidate = proxy_url.strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise ValueError(f"unsupported proxy scheme: {scheme}")
    if not parsed.hostname:
        raise ValueError("proxy host is required")

    port = parsed.port or _DEFAULT_PORTS[scheme]
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    server = f"{scheme}://{host}:{port}"
    if username:
        auth = quote(username, safe="")
        if password is not None:
            auth = f"{auth}:{quote(password, safe='')}"
        server = f"{scheme}://{auth}@{host}:{port}"

    return ProxySettings(
        mode="explicit",
        raw=proxy_url,
        server=server,
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=username,
        password=password,
    )


def resolve_proxy_settings(proxy_value=None) -> ProxySettings:
    value = _normalize_proxy_value(proxy_value)
    if value is False:
        return ProxySettings(mode="direct", raw="direct")
    if not value:
        return ProxySettings(mode="system")
    if value.lower() in _DIRECT_VALUES:
        return ProxySettings(mode="direct", raw=value)
    return _parse_proxy_url(value)


def resolve_proxy_settings_with_env(proxy_value=None) -> ProxySettings:
    settings = resolve_proxy_settings(proxy_value)
    if settings.mode != "system":
        return settings
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return resolve_proxy_settings(value)
    return settings


__all__ = ["ProxySettings", "resolve_proxy_settings", "resolve_proxy_settings_with_env"]
