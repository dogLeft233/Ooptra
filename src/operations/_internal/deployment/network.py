"""Server-side Git update transport; mirrors never choose the installed commit.

The official lookup runs outside the checkout with global/system Git configuration
disabled, so url.*.insteadOf cannot silently replace the trust source. Proxy URLs
are passed in child environment configuration, never command arguments or errors.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from operations.models import NetworkSettings, validate_network_field

OFFICIAL_URL = "https://github.com/Eason4869/Ooptra.git"
_CHANNELS = {"main", "beta", "dev"}
_PROXY_ENV = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}



class UpdateTransport:
    def __init__(self, *, command_runner, clock=None, api_lookup=None):
        self.command_runner = command_runner
        self.clock = clock or time
        if api_lookup is not None:
            self._api_sha = api_lookup

    def load_network_settings(self):
        """Read the synchronized runtime dictionary for each new operation."""
        from config import WEBUI_CONFIG

        return NetworkSettings(WEBUI_CONFIG.get("update_proxy", ""), WEBUI_CONFIG.get("update_mirror", ""))


    def test_mirror_connection(self, mirror, channel, run_command=None, *, proxy=None):
        """Probe Git protocol on the deployment host, without saving or fetching code."""
        run_command = run_command or self.command_runner
        self._validate_channel(channel)
        url = validate_network_field("update_mirror", mirror) or OFFICIAL_URL
        proxy = self.load_network_settings().proxy if proxy is None else validate_network_field("update_proxy", proxy)
        started = self.clock.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="ooptra-network-test-") as directory:
                env = self._git_environment(proxy, url, isolated=True)
                # Some accelerators redirect Git's initial discovery request.
                for index in range(int(env["GIT_CONFIG_COUNT"])):
                    if env[f"GIT_CONFIG_KEY_{index}"].endswith("followRedirects"):
                        env[f"GIT_CONFIG_VALUE_{index}"] = "initial"
                output = run_command(Path(directory), ["git", "ls-remote", "--", url, f"refs/heads/{channel}"], 15, env=env)
            rows = [row.split() for row in output.splitlines()]
            if not any(len(row) == 2 and self._valid_sha(row[0]) and row[1] == f"refs/heads/{channel}" for row in rows):
                raise ValueError("加速地址未返回该频道的有效 Git 分支；可能仅支持文件下载")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Git 连通性测试失败或超过 15 秒，请更换地址后重试") from exc
        except ValueError:
            raise ValueError("Git 连通性测试失败：未读取到有效分支，请检查地址或切换节点") from None
        return {"ok": True, "elapsed_ms": round((self.clock.monotonic() - started) * 1000),
                "detail": "Git 分支读取成功；安装时仍核对官方提交，连通不代表下载全程可用"}


    def _git_environment(self, proxy, url=OFFICIAL_URL, *, isolated=False):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("GIT_CONFIG_", "GIT_TRACE")) or key in {"GIT_CONFIG", "GIT_SSL_NO_VERIFY", "GIT_CURL_VERBOSE"}:
                env.pop(key, None)
            if proxy and key.lower() in _PROXY_ENV:
                env.pop(key, None)
        pairs = []
        if proxy:
            pairs.append(("http.proxy", "" if proxy == "direct" else proxy))
            pairs.append((f"http.{url}.proxy", "" if proxy == "direct" else proxy))
        redirects = "false" if isolated else "initial"
        pairs.extend([("http.sslVerify", "true"), (f"http.{url}.sslVerify", "true"),
                      ("http.followRedirects", redirects), (f"http.{url}.followRedirects", redirects)])
        env["GIT_CONFIG_COUNT"] = str(len(pairs))
        for index, (key, value) in enumerate(pairs):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_OPTIONAL_LOCKS="0")
        if isolated:
            env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
                env.pop(key, None)
        return env


    def _timeout(self, deadline, attempts_left=1, maximum=15):
        budget = deadline - self.clock.monotonic()
        if budget <= 0:
            raise ValueError("更新网络操作总耗时已超时，请检查部署服务器网络后重试")
        return min(maximum, budget / attempts_left)


    def _valid_sha(self, value):
        return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


    def _validate_channel(self, channel):
        if not isinstance(channel, str) or channel not in _CHANNELS:
            raise ValueError("仅支持 main、beta 或 dev 更新源")


    def _api_sha(self, channel, timeout, proxy):
        """Official API fallback, with redirects disabled and TLS always verified."""
        url = f"https://api.github.com/repos/Eason4869/Ooptra/commits/heads/{channel}"
        with requests.Session() as session:
            session.trust_env = not proxy
            if proxy and proxy != "direct":
                session.proxies.update(http=proxy, https=proxy)
            with session.get(url, headers={"Accept": "application/vnd.github.sha", "User-Agent": "Ooptra-Updater"},
                             timeout=timeout, allow_redirects=False, stream=True, verify=True) as response:
                if response.status_code != 200:
                    raise ValueError("官方 GitHub API 查询失败")
                body = bytearray()
                read_deadline = self.clock.monotonic() + timeout
                for chunk in response.iter_content(chunk_size=128):
                    if self.clock.monotonic() >= read_deadline or len(body) + len(chunk) > 128:
                        raise ValueError("官方 GitHub API 响应超时或无效")
                    body.extend(chunk)
                sha = body.decode("ascii").strip()
        if not self._valid_sha(sha):
            raise ValueError("官方 GitHub API 返回的提交无效")
        return sha


    def lookup_official_sha(self, root, channel, deadline, run_command=None, *, settings=None, progress=None):
        """Resolve the channel only from verified official HTTPS; never from mirrors."""
        run_command = run_command or self.command_runner
        self._validate_channel(channel)
        settings = self.load_network_settings() if settings is None else settings
        git_proxies = [settings.proxy] if settings.proxy == "direct" else [settings.proxy, "direct"]
        api_proxies = [settings.proxy] if settings.proxy == "direct" else [settings.proxy, "direct"]
        # requests' optional SOCKS extra is not part of the deployment contract.
        api_proxies = [proxy for proxy in api_proxies if not proxy.startswith("socks")]
        attempts = [("git", proxy) for proxy in git_proxies] + [("api", proxy) for proxy in api_proxies]
        for index, (kind, proxy) in enumerate(attempts):
            timeout = self._timeout(deadline, len(attempts) - index)
            label = "官方 GitHub API" if kind == "api" else "官方 GitHub"
            if progress:
                progress(f"查询{label}（{'直连' if proxy == 'direct' else '服务器网络出口'}），尝试 {index + 1}/{len(attempts)}")
            try:
                if kind == "api":
                    sha = self._api_sha(channel, timeout, proxy)
                else:
                    with tempfile.TemporaryDirectory(prefix="ooptra-update-source-") as directory:
                        env = self._git_environment(proxy, isolated=True)
                        env["GIT_CEILING_DIRECTORIES"] = str(Path(directory).parent)
                        output = run_command(Path(directory), ["git", "ls-remote", OFFICIAL_URL, f"refs/heads/{channel}"],
                                             timeout, env=env)
                    rows = [row.split() for row in output.splitlines()]
                    sha = next((row[0] for row in rows if len(row) == 2 and row[1] == f"refs/heads/{channel}"), "")
                if self._valid_sha(sha):
                    self._timeout(deadline)
                    return sha
            except (OSError, ValueError, subprocess.TimeoutExpired, requests.RequestException, UnicodeError):
                pass
        raise ValueError("无法核对官方更新版本；已尝试服务器网络出口、直连及官方 API。请配置部署服务器更新代理后重试；镜像不能替代官方版本校验")


    def fetch_verified(self, root, channel, trusted_sha, deadline, run_command=None, *, settings=None, progress=None):
        """Try official transports, then the configured mirror, pinning every fetch."""
        run_command = run_command or self.command_runner
        self._validate_channel(channel)
        if not self._valid_sha(trusted_sha):
            raise ValueError("更新目标提交无效，请先重新检查官方版本")
        settings = self.load_network_settings() if settings is None else settings
        sources = [(OFFICIAL_URL, settings.proxy, "官方 GitHub")]
        if settings.proxy != "direct":
            sources.append((OFFICIAL_URL, "direct", "官方 GitHub 直连"))
        if settings.mirror:
            sources.append((settings.mirror, settings.proxy, "配置镜像（已核对官方提交）"))
            if settings.proxy != "direct":
                sources.append((settings.mirror, "direct", "配置镜像直连（已核对官方提交）"))
        for index, (url, proxy, label) in enumerate(sources):
            timeout = self._timeout(deadline, len(sources) - index, 60)
            if progress:
                progress(f"下载更新：{label.split('（')[0]}，尝试 {index + 1}/{len(sources)}")
            try:
                env = self._git_environment(proxy, url)
                run_command(root, ["git", "fetch", "--no-tags", "--", url, f"refs/heads/{channel}"], timeout, env=env)
                sha = run_command(root, ["git", "rev-parse", "FETCH_HEAD"], self._timeout(deadline, maximum=3), env=env)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
            if sha != trusted_sha:
                raise ValueError("下载提交与已核对的官方版本不一致，已停止更新；渠道或镜像可能已变化，请重新检查官方版本后重试")
            self._timeout(deadline)
            return label
        raise ValueError("更新下载失败或超时；已尝试官方源及配置的备用源，请检查部署服务器代理与 HTTPS Git 镜像地址后重试")
