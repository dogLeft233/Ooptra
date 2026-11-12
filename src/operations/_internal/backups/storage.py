"""Bounded, verified backups of local configuration and user data (stdlib only)."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 1024


def restored_endpoint(path: Path) -> dict | None:
    """Read only a literal WebUI dictionary; never execute a restored config."""
    if not path.is_file():
        return None
    for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "WEBUI_CONFIG" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
                if not isinstance(value, dict) or not value.get("enabled", True):
                    raise ValueError("恢复后 WebUI 必须启用，才能验证恢复结果")
                host = str(os.environ.get("BOT_WEBUI_HOST") or value.get("host") or "127.0.0.1").strip() or "127.0.0.1"
                host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else "::1" if host == "::" else host
                token = str(value.get("token") or "").strip() or os.environ.get("BOT_WEBUI_TOKEN", "").strip()
                return {"health_host": host, "health_port": int(os.environ.get("BOT_WEBUI_PORT") or value.get("port") or 3090), "health_token": token}
            except (TypeError, ValueError) as exc:
                raise ValueError("无法安全检查备份中的 WebUI 配置，请手动恢复") from exc
    return None


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".job-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        for attempt in range(5):
            try:
                os.replace(name, path)
                break
            except PermissionError as exc:
                # Windows readers/antivirus can briefly deny rename. Keep the
                # old complete file until replacement; real permission failures
                # still propagate after at most 200 ms of retries.
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        Path(name).unlink(missing_ok=True)


class BackupStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.directory = self.root / "data/maintenance/backups"
        if self.directory.is_symlink() or not self.directory.resolve().is_relative_to(self.root):
            raise ValueError("维护目录不能指向项目外部")
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, backup_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", str(backup_id)):
            raise ValueError("备份 ID 无效")
        target = self.directory / f"{backup_id}.zip"
        if target.is_symlink() or not target.resolve().is_relative_to(self.root):
            raise ValueError("备份路径无效")
        return target

    def delete(self, backup_id: str) -> None:
        """Only unlink a regular backup file inside the original, unlinked directory."""
        target = self.path(backup_id)
        for path in (target, *target.parents):
            if path == self.root:
                break
            try:
                info = path.lstat()
            except FileNotFoundError:
                raise ValueError("备份不存在") from None
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("备份路径不能包含链接或联接目录")
        if not stat.S_ISREG(target.lstat().st_mode):
            raise ValueError("备份不存在或不是普通文件")
        target.unlink()

    def _target(self, name: str) -> Path:
        part = PurePosixPath(name)
        if (not name or "\\" in name or ":" in name or part.is_absolute()
                or any(p in {"..", "."} for p in part.parts)
                or str(part) != name):
            raise ValueError("备份包含不安全路径")
        if name not in {"config.py", "private_key.py"} and (len(part.parts) < 2 or part.parts[0] != "data" or part.parts[1] in {"maintenance", "cache", "caches", "logs"}):
            raise ValueError("备份包含范围外的文件")
        target = self.root.joinpath(*part.parts)
        if not target.resolve().is_relative_to(self.root) or target.is_symlink():
            raise ValueError("备份路径指向项目外部")
        # Reject links even when they resolve to another location inside the project.
        if any(p.is_symlink() for p in target.parents if p != self.root and p.is_relative_to(self.root)):
            raise ValueError("备份不支持符号链接目录")
        return target

    def create(self) -> dict:
        backup_id = uuid.uuid4().hex
        entries = []
        total = 0
        created = time.time()
        target = self.path(backup_id)
        sources = [self.root / "config.py", self.root / "private_key.py"]
        data = self.root / "data"
        if data.exists():
            for directory, dirs, files in os.walk(data, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {"maintenance", "cache", "caches", "logs", "__pycache__"}]
                if any((Path(directory) / name).is_symlink() for name in dirs):
                    raise ValueError("用户数据包含符号链接目录，请先手动备份")
                sources.extend(Path(directory) / name for name in files)
        try:
            with tempfile.TemporaryDirectory(dir=self.directory) as stage, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for source in sources:
                    if not source.exists() or source.name.endswith(("-wal", "-shm", "-journal")):
                        continue
                    name = source.relative_to(self.root).as_posix()
                    self._target(name)
                    if not source.is_file() or len(entries) >= MAX_FILES or source.stat().st_size + total > MAX_BYTES:
                        raise ValueError("备份超出容量限制或包含非普通文件")
                    with source.open("rb") as stream:
                        sqlite = stream.read(16) == b"SQLite format 3\x00"
                    copy = source
                    if sqlite:
                        copy = Path(stage) / f"{len(entries)}.sqlite"
                        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as db, closing(sqlite3.connect(copy)) as dest:
                            db.backup(dest)
                    content = copy.read_bytes()
                    total += len(content)
                    if total > MAX_BYTES:
                        raise ValueError("备份超出容量限制")
                    entries.append({"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
                    archive.writestr(name, content)
                archive.writestr("manifest.json", json.dumps({"format": 1, "created_at": created, "files": entries}))
            return {"id": backup_id, "created_at": created, "size": target.stat().st_size, "file_count": len(entries)}
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def list(self, limit: int | None = 50) -> list[dict]:
        result = []
        for path in sorted(self.directory.glob("*.zip"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem) or path.is_symlink():
                continue
            result.append({"id": path.stem, "created_at": path.stat().st_mtime, "size": path.stat().st_size})
        return result if limit is None else result[:limit]

    def validate(self, backup_id: str, stage: Path) -> list[str]:
        target = self.path(backup_id)
        if not target.is_file() or target.stat().st_size > MAX_BYTES * 2:
            raise ValueError("备份不存在或过大")
        try:
            with zipfile.ZipFile(target) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES + 1 or len({i.filename for i in infos}) != len(infos):
                    raise ValueError("备份文件列表无效")
                manifest_info = archive.getinfo("manifest.json")
                if manifest_info.file_size > 1024 * 1024:
                    raise ValueError("备份清单过大")
                manifest = json.loads(archive.read("manifest.json"))
                rows = manifest.get("files", [])
                if manifest.get("format") != 1 or not isinstance(rows, list) or len(rows) > MAX_FILES:
                    raise ValueError("不支持的备份格式")
                names = [r["name"] for r in rows]
                if len(set(names)) != len(names) or set(names) | {"manifest.json"} != {i.filename for i in infos}:
                    raise ValueError("备份内容与清单不一致")
                total = 0
                for row in rows:
                    name = row["name"]
                    self._target(name)
                    info = archive.getinfo(name)
                    total += info.file_size
                    mode = info.external_attr >> 16
                    if total > MAX_BYTES or info.file_size != row["size"] or stat.S_ISLNK(mode) or info.is_dir():
                        raise ValueError("备份内容无效或过大")
                    content = archive.read(name)
                    if hashlib.sha256(content).hexdigest() != row["sha256"]:
                        raise ValueError("备份校验失败")
                    output = stage / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(content)
                return names
        except (KeyError, TypeError, json.JSONDecodeError, zipfile.BadZipFile, OSError) as exc:
            raise ValueError("备份损坏或无法读取") from exc

    def restore(self, backup_id: str) -> None:
        """Call only after the application has shut down its database connections."""
        with tempfile.TemporaryDirectory(dir=self.directory) as folder:
            stage = Path(folder)
            names = self.validate(backup_id, stage)
            previous = stage / "previous"
            previous.mkdir()
            replaced = []
            try:
                for index, name in enumerate(names):
                    dest = self._target(name)
                    existed = dest.exists()
                    if existed:
                        shutil.copy2(dest, previous / str(index))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # A stopped SQLite database must not replay stale WAL data.
                    with (stage / name).open("rb") as stream:
                        is_sqlite = stream.read(16) == b"SQLite format 3\x00"
                    if is_sqlite:
                        for suffix in ("-wal", "-shm", "-journal"):
                            sidecar = self._target(name + suffix)
                            sidecar.unlink(missing_ok=True)
                    os.replace(stage / name, dest)
                    replaced.append((index, dest, existed))
            except BaseException:
                for index, dest, existed in reversed(replaced):
                    if existed:
                        os.replace(previous / str(index), dest)
                    else:
                        dest.unlink(missing_ok=True)
                raise
