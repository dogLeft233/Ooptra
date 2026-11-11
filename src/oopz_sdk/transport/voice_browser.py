from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from oopz_sdk.config.settings import OopzConfig
from oopz_sdk.exceptions import OopzConnectionError
from oopz_sdk.transport.proxy import build_aiohttp_proxy

logger = logging.getLogger(__name__)

# Observed from real Oopz/Agora Web SDK join_v3 frames.
# The voice API response may return appId=0; do not pass that 0 to AgoraRTC.join().
DEFAULT_AGORA_APP_ID = "358eebceadb94c2a9fd91ecd7b341602"

_DEFAULT_BROWSER_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]


class BrowserVoiceTransport:
    def __init__(self, config: OopzConfig):
        self.config = config

        # Agora RTC numeric uid. Do not generate a random uid here.
        # It must be the numeric uid used by the Oopz voice join result / Agora join.
        self._agora_uid: str | None = None

        self._available = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        self._init_done = threading.Event()
        self._init_error: str | None = None

        self._page = None
        self._browser = None
        self._playwright = None

        self._joined_room: str | None = None
        self._joined_uid: str | None = None

        # Oopz internal string uid, e.g. 6ad0261bc4eb41e882692531981a0972.
        # This is the data_stream payload field `uid`.
        self._oopz_uid: str | None = None

        # Duplex: JS -> Python remote PCM callback
        self._remote_pcm_callback = None

        # 房间内各成员的实时静音状态：oopz uid -> {m, hm, cid, ts}
        # 来源是 Agora stream message（播放器里 client.on("stream-message")）。
        # Oopz 的 REST（membersByChannels）与网关事件都不提供这个状态，
        # 这里是唯一能拿到「谁闭了麦」的地方。
        self._voice_states: dict[str, dict[str, Any]] = {}
        self._voice_state_rx = 0

    # ------------------------------------------------------------------
    # 成员实时静音状态（Agora stream message 接收侧）
    # ------------------------------------------------------------------

    def _remember_voice_state(self, uid: str, payload: Any) -> bool:
        """记下一条成员静音状态。返回是否采纳（供探针/测试判断）。

        payload 是播放器发来的 JSON 串：``{"m":0|1,"hm":0|1,"cid":<agora uid>,"ts":...}``
        （``m``=1 闭麦，``hm``=1 闭听）。字段缺失或非法一律丢弃，不猜。
        """
        import json

        uid = str(uid or "").strip()
        if not uid:
            return False

        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except Exception:
                logger.debug("voice state payload is not json")
                return False
        elif isinstance(payload, dict):
            data = payload
        else:
            return False
        if not isinstance(data, dict):
            return False

        def _flag(key: str) -> int | None:
            value = data.get(key)
            if value is None:
                return None
            try:
                return 1 if int(value) else 0
            except (TypeError, ValueError):
                return None

        m = _flag("m")
        hm = _flag("hm")
        if m is None and hm is None:
            return False

        try:
            cid = int(data.get("cid"))
        except (TypeError, ValueError):
            cid = None
        try:
            ts = float(data.get("ts")) / 1000.0
        except (TypeError, ValueError):
            ts = time.time()

        self._voice_states[uid] = {"m": m, "hm": hm, "cid": cid, "ts": ts}
        self._voice_state_rx += 1
        if self._voice_state_rx == 1:
            # 只报一次：Oopz 客户端到底发不发这条广播，是这套方案的前提
            logger.info("[VOICE] 收到房间静音状态广播（后续按 debug 级别记录）")
        else:
            logger.debug("[VOICE] 静音状态更新 uid=%s m=%s hm=%s", uid, m, hm)
        return True

    def voice_states(self) -> dict[str, dict[str, Any]]:
        """成员实时静音状态快照（可能为空：没人广播过就没有数据）。"""
        return {uid: dict(row) for uid, row in self._voice_states.items()}

    @property
    def voice_state_received(self) -> int:
        """累计收到的广播条数。

        WebUI 用它区分「真的没人闭麦」和「压根没收到过广播」——
        后者说明 Oopz 客户端不发这条消息，界面应继续显示「未知」。
        """
        return self._voice_state_rx

    @property
    def available(self) -> bool:
        return self._available

    async def start(self) -> None:
        if self._started:
            return

        self._init_error = None
        self._init_done.clear()

        self._started = True
        self._thread = threading.Thread(
            target=self._thread_main,
            name="oopz-voice-browser",
            daemon=True,
        )
        self._thread.start()

        ok = await asyncio.to_thread(
            self._init_done.wait,
            self.config.agora_init_timeout,
        )
        if not ok:
            self._started = False
            raise RuntimeError("voice browser init timeout")
        if self._init_error:
            self._started = False
            raise RuntimeError(f"voice browser init failed: {self._init_error}")

        self._available = True

    async def close(self) -> None:
        if not self._started:
            return

        try:
            await self.leave()
        except Exception:
            logger.debug("voice leave failed during close", exc_info=True)

        loop = self._thread_loop
        if loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown_browser(), loop)
            try:
                await asyncio.wrap_future(fut)
            except Exception:
                logger.debug("voice browser shutdown coroutine failed", exc_info=True)
            loop.call_soon_threadsafe(loop.stop)

        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)

        self._available = False
        self._started = False
        self._thread = None
        self._thread_loop = None
        self._joined_room = None
        self._joined_uid = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._thread_loop = loop
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._init_browser())
            loop.run_forever()
        except Exception as exc:
            self._init_error = str(exc)
            self._init_done.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            loop.close()

    async def _init_browser(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:
            self._init_error = (
                "playwright is required for voice browser backend. "
                "Install with: pip install playwright && playwright install chromium"
            )
            self._init_done.set()
            raise exc

        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": self.config.voice_browser_headless,
            "args": list(_DEFAULT_BROWSER_ARGS),
        }

        if self.config.voice_browser_executable_path:
            launch_kwargs["executable_path"] = self.config.voice_browser_executable_path
        else:
            launch_kwargs["channel"] = "chromium"

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        page = await self._browser.new_page()
        page.set_default_timeout(60_000)

        await page.add_init_script(
            f"window.AGORA_SDK_URL = {self.config.voice_agora_sdk_url!r};"
        )

        async def _on_remote_pcm(uid: str, b64: str, sample_rate: int) -> None:
            callback = self._remote_pcm_callback
            if callback is None:
                return
            try:
                import base64 as _b64

                pcm = _b64.b64decode(b64 or "")
            except Exception:
                logger.debug("decode remote pcm failed", exc_info=True)
                return
            try:
                result = callback(str(uid or ""), pcm, int(sample_rate or 16000))
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("remote pcm callback failed", exc_info=True)

        try:
            await page.expose_function("oopzPushRemotePcm", _on_remote_pcm)
        except Exception:
            logger.debug("expose oopzPushRemotePcm failed", exc_info=True)

        async def _on_voice_state(uid: str, payload: str) -> None:
            self._remember_voice_state(str(uid or ""), payload)

        try:
            await page.expose_function("oopzPushVoiceState", _on_voice_state)
        except Exception:
            logger.debug("expose oopzPushVoiceState failed", exc_info=True)

        html_path = Path(__file__).resolve().parent.parent / "assets" / "voice" / "agora_player.html"
        await page.goto(html_path.as_uri())

        self._page = page
        self._init_done.set()

    async def _shutdown_browser(self) -> None:
        try:
            if self._page is not None:
                await self._page.close()
        finally:
            self._page = None

        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = None

        try:
            if self._playwright is not None:
                await self._playwright.stop()
        finally:
            self._playwright = None

    async def _run_on_browser(self, method: str, *args: Any) -> Any:
        await self.start()

        if self._thread_loop is None or self._page is None:
            raise RuntimeError("voice browser is not ready")

        async def _invoke() -> Any:
            return await self._page.evaluate(
                """
                async ({method, args}) => {
                  const fn = window[method];
                  if (typeof fn !== 'function') {
                    throw new Error(`browser function not found: ${method}`);
                  }
                  return await fn(...args);
                }
                """,
                {"method": method, "args": list(args)},
            )

        fut = asyncio.run_coroutine_threadsafe(_invoke(), self._thread_loop)
        return await asyncio.wrap_future(fut)

    @staticmethod
    def _normalize_app_id(app_id: str | int | None) -> str:
        value = "" if app_id is None else str(app_id).strip()
        if not value or value == "0":
            return DEFAULT_AGORA_APP_ID
        return value

    async def join(
            self,
            *,
            app_id: str | int | None,
            token: str,
            room_id: str,
            uid: str | int,
            oopz_uid: str | None = None,
    ) -> bool:
        """
        Join Agora voice room.

        Mapping from the Oopz voice API response:
            app_id   -> usually use DEFAULT_AGORA_APP_ID, because API data.appId may be 0
            token    -> data.supplierSign
            room_id  -> data.roomId
            uid      -> numeric RTC uid, e.g. 374010679
            oopz_uid -> internal Oopz string uid, e.g. 6ad0261bc4eb41e882692531981a0972
        """
        if not token:
            raise ValueError("token / supplierSign is required for voice join")
        if not room_id:
            raise ValueError("room_id / roomId is required for voice join")
        if not uid:
            raise ValueError("agora uid (oopz pid) is required for voice join")
        rtc_uid = int(uid)
        real_app_id = self._normalize_app_id(app_id)

        result = await self._run_on_browser(
            "agoraJoin",
            real_app_id,
            token,
            room_id,
            rtc_uid,
        )

        ok = bool(result and result.get("ok"))
        if not ok:
            logger.debug(
                "[VOICE] join failed app_id=%s room=%s rtc_uid=%s result=%s",
                real_app_id,
                room_id,
                rtc_uid,
                result,
            )
            return False

        returned_uid = result.get("uid", rtc_uid)
        if returned_uid is not None and int(returned_uid) != rtc_uid:
            logger.warning(
                "[VOICE] Agora returned uid differs from enter_channel uid: "
                "enter_uid=%s returned_uid=%s",
                rtc_uid,
                returned_uid,
            )

        self._agora_uid = str(rtc_uid)
        self._joined_room = room_id
        self._joined_uid = str(rtc_uid)

        # 新房间：清掉上一次遗留的状态（含计数，探针要按房间算）
        self._voice_states.clear()
        self._voice_state_rx = 0

        if oopz_uid is None:
            oopz_uid = getattr(self.config, "person_uid", None)

        if oopz_uid:
            self._oopz_uid = str(oopz_uid)

        logger.debug(
            "[VOICE] joined app_id=%s room=%s agora_uid=%s oopz_uid=%s result=%s",
            real_app_id,
            room_id,
            rtc_uid,
            self._oopz_uid,
            result,
        )

        # Idle state after join: mic normal, speaker normal.
        if self._oopz_uid:
            try:
                await self.send_identity(self._oopz_uid, rtc_uid)
                await self.set_voice_state(mic_muted=False, speaker_muted=False)
            except Exception:
                logger.debug("send voice identity after join failed", exc_info=True)

        return True

    async def leave(self) -> None:
        if not self._started:
            return

        if self._oopz_uid and self._agora_uid:
            try:
                await self.set_voice_state(mic_muted=True, speaker_muted=False)
            except Exception:
                logger.debug("set voice state before leave failed", exc_info=True)

        result = await self._run_on_browser("agoraLeave")
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError((result or {}).get("error") or "browser voice leave failed")
        self._agora_uid = None
        self._joined_room = None
        self._joined_uid = None
        self._voice_states.clear()

    async def stop_audio(self) -> None:
        if not self._started:
            return

        await self._run_on_browser("agoraStopAudio")

    async def play_url(self, url: str) -> dict[str, Any]:
        result = await self._prepare_and_play("agoraPlayAudio", url)
        if result and result.get("ok"):
            return result

        logger.debug("remote audio play failed, fallback to local bytes: %s", result)

        try:
            await self.stop_audio()
        except Exception:
            logger.debug("stop audio before fallback failed", exc_info=True)

        data, mime_type = await self._download_audio(url)
        return await self.play_bytes(data, mime_type=mime_type)

    async def play_file(
            self,
            file_path: str,
            *,
            mime_type: str | None = None,
    ) -> dict[str, Any]:
        data = await asyncio.to_thread(Path(file_path).read_bytes)
        return await self.play_bytes(
            data,
            mime_type=mime_type or self._guess_mime_from_path(file_path),
        )

    async def play_bytes(
            self,
            data: bytes,
            *,
            mime_type: str | None = None,
    ) -> dict[str, Any]:
        payload = base64.b64encode(data).decode("ascii")
        return await self._prepare_and_play(
            "agoraPlayLocal",
            payload,
            mime_type or "audio/mpeg",
        )

    async def _prepare_and_play(self, method: str, *args: Any) -> dict[str, Any]:
        """
        Python 端只确保 uid/cid 绑定已经传给 HTML。
        """
        if self._oopz_uid and self._agora_uid:
            try:
                await self.send_identity(self._oopz_uid, self._agora_uid)
            except Exception:
                logger.debug("prepare voice identity before play failed", exc_info=True)

        result = await self._run_on_browser(method, *args)

        if result and result.get("ok"):
            browser_uid = result.get("agoraUid")
            if browser_uid is not None and self._agora_uid is not None:
                if int(browser_uid) != int(self._agora_uid):
                    logger.warning(
                        "[VOICE] browser agora uid differs: expected=%s browser=%s",
                        self._agora_uid,
                        browser_uid,
                    )

            return result

        # 播放失败
        return result or {"ok": False, "error": "empty browser result"}

    async def send_identity(
            self,
            oopz_uid: str,
            agora_uid: str | int | None = None,
    ) -> bool:
        """
        Send the Oopz uid -> Agora numeric uid bridge.

        data_stream payload:
            uid = Oopz internal string uid
            cid = Agora numeric uid
        """
        self._oopz_uid = str(oopz_uid)

        if agora_uid is None:
            agora_uid = self._agora_uid

        if agora_uid is None:
            logger.debug("[VOICE] skip identity: agora uid is not ready")
            return False

        result = await self._run_on_browser(
            "agoraSetVoiceIdentity",
            str(oopz_uid),
            int(agora_uid),
        )

        ok = bool(result and result.get("ok"))

        logger.debug(
            "[VOICE] send identity oopz_uid=%s agora_uid=%s result=%s",
            oopz_uid,
            agora_uid,
            result,
        )

        return ok

    async def set_voice_state(
            self,
            *,
            mic_muted: bool,
            speaker_muted: bool = False,
    ) -> bool:
        result = await self._run_on_browser(
            "agoraSetVoiceState",
            bool(mic_muted),
            bool(speaker_muted),
        )

        logger.debug(
            "[VOICE] set voice state mic_muted=%s speaker_muted=%s result=%s",
            mic_muted,
            speaker_muted,
            result,
        )

        return bool(result and result.get("ok"))

    def set_remote_pcm_callback(self, callback) -> None:
        self._remote_pcm_callback = callback

    async def enable_listen(self, enabled: bool = True) -> dict[str, Any]:
        result = await self._run_on_browser("agoraSetListen", bool(enabled))
        return result if isinstance(result, dict) else {"ok": bool(result)}

    async def push_tts_pcm(
        self,
        pcm16: bytes,
        sample_rate: int,
        *,
        finish: bool = False,
        volume: int = 50,
    ) -> dict[str, Any]:
        import base64 as _b64

        payload = _b64.b64encode(pcm16 or b"").decode("ascii")
        result = await self._run_on_browser(
            "agoraPushTtsPcm",
            payload,
            int(sample_rate),
            bool(finish),
            int(volume),
        )
        return result if isinstance(result, dict) else {"ok": bool(result)}

    async def set_tts_volume(self, volume: int) -> dict[str, Any]:
        result = await self._run_on_browser("agoraSetTtsVolume", int(volume))
        return result if isinstance(result, dict) else {"ok": bool(result)}

    async def stop_tts(self) -> dict[str, Any]:
        result = await self._run_on_browser("agoraStopTts")
        return result if isinstance(result, dict) else {"ok": bool(result)}

    async def wait_tts_complete(self, timeout: float = 30.0) -> dict[str, Any]:
        """Wait for actual AudioContext playback, including scheduled tail sources."""
        async def wait() -> dict[str, Any]:
            while True:
                status = await self._run_on_browser("agoraTtsStatus")
                if not isinstance(status, dict) or not status.get("ok"):
                    return {"ok": False, "error": (status or {}).get("error", "TTS status unavailable")}
                if status.get("pending_sources", 0) == 0 and status.get("remaining_seconds", 0) <= 0:
                    return {"ok": True}
                await asyncio.sleep(0.02)

        try:
            return await asyncio.wait_for(wait(), max(0.0, timeout))
        except asyncio.TimeoutError:
            return {"ok": False, "error": "TTS playback timed out"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def set_volume(self, volume: int) -> bool:
        result = await self._run_on_browser("agoraSetVolume", int(volume))
        return bool(result and result.get("ok"))

    async def pause(self) -> bool:
        result = await self._run_on_browser("agoraPause")
        return bool(result and result.get("ok"))

    async def resume(self) -> bool:
        result = await self._run_on_browser("agoraResume")
        return bool(result and result.get("ok"))

    async def seek(self, seconds: float) -> bool:
        result = await self._run_on_browser("agoraSeek", float(seconds))
        return bool(result and result.get("ok"))

    async def get_state(self) -> str:
        if not self._started:
            return "idle"

        result = await self._run_on_browser("agoraState")
        return str(result or "idle")

    async def capture_screen(self, watch=None) -> dict[str, Any]:
        """按需截取当前RTC单一视频发布者，不保存图片。"""
        if not self._started:
            return {"status": "unavailable"}
        if watch is not None:
            joined = await self._run_on_browser("agoraWatchScreen", watch)
            if not isinstance(joined, dict) or not joined.get("ok"):
                return {"status": "unavailable"}
        result = await self._run_on_browser("agoraCaptureScreen")
        return result if isinstance(result, dict) else {"status": "unavailable"}

    async def close_screen(self) -> None:
        if self._started:
            result = await self._run_on_browser("agoraCloseScreen")
            if not isinstance(result, dict) or not result.get("ok"):
                raise RuntimeError("screen viewer cleanup unconfirmed")

    async def get_status(self) -> dict[str, Any]:
        if not self._started:
            return {
                "state": "idle",
                "hasClient": False,
                "currentOopzUid": self._oopz_uid,
                "currentAgoraUid": self._agora_uid,
            }

        result = await self._run_on_browser("agoraGetStatus")
        if isinstance(result, dict):
            return result

        return {
            "state": str(result or "unknown"),
            "currentOopzUid": self._oopz_uid,
            "currentAgoraUid": self._agora_uid,
        }

    async def get_current_time(self) -> float:
        if not self._started:
            return 0.0

        result = await self._run_on_browser("agoraGetCurrentTime")
        try:
            return float(result or 0)
        except (TypeError, ValueError):
            return 0.0

    async def _download_audio(self, url: str) -> tuple[bytes, str]:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
        proxy = build_aiohttp_proxy(url, self.config.proxy)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, proxy=proxy) as resp:
                    if resp.status >= 400:
                        raise OopzConnectionError(
                            f"audio download failed: HTTP {resp.status}"
                        )

                    data = await resp.read()
                    mime_type = (
                            resp.headers.get("Content-Type", "audio/mpeg")
                            .split(";")[0]
                            .strip()
                            or "audio/mpeg"
                    )
                    return data, mime_type

        except asyncio.TimeoutError as exc:
            raise OopzConnectionError("audio download timeout") from exc
        except aiohttp.ClientError as exc:
            raise OopzConnectionError(f"audio download failed: {exc}") from exc

    @staticmethod
    def _guess_mime_from_path(file_path: str) -> str:
        mime_type, _ = mimetypes.guess_type(file_path)
        return mime_type or "audio/mpeg"

    @staticmethod
    def guess_extension_from_url(url: str) -> str:
        path = urlparse(url).path
        ext = Path(path).suffix
        if ext:
            return ext
        return ".mp3"
