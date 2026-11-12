"""Source validation protects both filtered and Python 3.10 tar extraction."""

import io
import tarfile

import pytest

from tests.support.maintenance_harness import maintenance


@pytest.mark.parametrize("modern_filters", [True, False])
@pytest.mark.parametrize("unsafe_member", [None, "../escaped.py", "symlink", "hardlink"])
def test_source_preparation_preserves_archive_boundary(tmp_path, monkeypatch, modern_filters, unsafe_member):
    if not modern_filters:
        # 旧版默认不使用过滤器；3.14 的默认值却会引用下面要移除的 data_filter。
        if hasattr(tarfile.TarFile, "extraction_filter") and hasattr(tarfile, "fully_trusted_filter"):
            monkeypatch.setattr(tarfile.TarFile, "extraction_filter",
                                staticmethod(tarfile.fully_trusted_filter))
        monkeypatch.delattr(tarfile, "data_filter", raising=False)

    def run(root, args, timeout=30, env=None):
        if args[:2] == ["git", "archive"]:
            output = next(value.removeprefix("--output=") for value in args if value.startswith("--output="))
            with tarfile.open(output, "w") as archive:
                for name, content in {"launcher.py": b"pass", "src/webui/server.py": b"OOPTRA_UPDATE_ID bootstrap_ready", "src/operations/models.py": b"DEPLOYMENT_CONTRACT_VERSION = 2\nCONTEXT_SCHEMA_MAX = 1"}.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                if unsafe_member:
                    info = tarfile.TarInfo(unsafe_member)
                    if unsafe_member in {"symlink", "hardlink"}:
                        info.type = tarfile.SYMTYPE if unsafe_member == "symlink" else tarfile.LNKTYPE
                        info.linkname = "launcher.py"
                    archive.addfile(info)
        elif "venv" in args:
            # Reaching this point proves real extraction preserved readiness files.
            raise RuntimeError("reached environment creation")
        return ""

    monkeypatch.setattr(maintenance, "run_command", run)
    monkeypatch.setattr(maintenance, "fetch_verified", lambda *args, **kwargs: None)
    service = maintenance.MaintenanceService(tmp_path)
    expected_error = ValueError if unsafe_member else RuntimeError
    expected_detail = "不安全路径" if unsafe_member else "reached environment creation"

    with pytest.raises(expected_error, match=expected_detail):
        service._prepare_environment({"id": "b" * 32, "channel": "dev", "target_sha": "a" * 40})
    assert not list((tmp_path / "data/maintenance/staging").glob("*/escaped.py"))
