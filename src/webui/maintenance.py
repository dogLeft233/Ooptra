"""维护 HTTP 接入：解析请求、调用公开维护能力并转换响应。"""

from __future__ import annotations

import asyncio
import subprocess

from operations.api import Maintenance


def mount_maintenance_routes(app, service: Maintenance):
    from urllib.parse import urlsplit

    from aiohttp import web

    async def body(request):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise ValueError("跨站维护请求被拒绝")
        if request.content_type != "application/json":
            raise ValueError("维护请求必须使用 JSON")
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("请求格式无效")
        return value

    async def status(request):
        try:
            page = int(request.query.get("page", "1"))
            page_size = int(request.query.get("page_size", "50"))
            return web.json_response(await asyncio.to_thread(service.status, page, page_size))
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def preflight_status(request):
        return web.json_response(await service.query("preflight"))

    async def storage_status(request):
        return web.json_response(await service.query("storage"))

    async def download(request):
        try:
            path = service.backups.path(request.match_info["id"])
            if not path.is_file():
                raise ValueError("备份不存在")
            return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="ooptra-{path.stem}.zip"'})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    def handler(action):
        async def invoke(request):
            try:
                value = await body(request)
                if action == "check":
                    return web.json_response(await service.check_update(value.get("channel")))
                if action == "cleanup_preview":
                    result = await asyncio.to_thread(service.preview_cleanup)
                elif action == "cleanup_apply":
                    result = await service.apply_cleanup(value.get("token", ""))
                elif action == "backup":
                    result = await service.create_backup()
                elif action == "delete_backup":
                    result = await service.delete_backup(request.match_info["id"])
                elif action == "network_test":
                    options = {"proxy": value["proxy"]} if "proxy" in value else {}
                    result = await asyncio.to_thread(service.probe_network, value.get("mirror", ""), value.get("channel", "main"), **options)
                elif action == "restore":
                    result = await service.start_restore(value.get("backup_id", ""))
                else:
                    result = await service.start_update(value.get("channel"), value.get("target_sha"),
                                                        confirm_channel_switch=value.get("confirm_channel_switch", False))
                return web.json_response({"ok": True, **result})
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                return web.json_response({"ok": False, "error": str(exc) if isinstance(exc, ValueError) else "维护操作失败，请检查部署环境和网络"}, status=400)
        return invoke

    app.add_routes([
        web.get("/api/maintenance", status), web.get("/api/maintenance/backups/{id}", download),
        web.get("/api/maintenance/preflight", preflight_status),
        web.get("/api/maintenance/storage", storage_status),
        web.post("/api/maintenance/check", handler("check")),
        web.post("/api/maintenance/backups", handler("backup")),
        web.delete("/api/maintenance/backups/{id}", handler("delete_backup")),
        web.post("/api/maintenance/network/test", handler("network_test")),
        web.post("/api/maintenance/restore", handler("restore")),
        web.post("/api/maintenance/update", handler("update")),
        web.post("/api/maintenance/cleanup/preview", handler("cleanup_preview")),
        web.post("/api/maintenance/cleanup/apply", handler("cleanup_apply")),
    ])
