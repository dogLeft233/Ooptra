"""自动测试发现不能执行数据目录中的手工联调脚本。"""

import subprocess
import sys
from pathlib import Path


def test_default_discovery_does_not_import_manual_acceptance_scripts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_safe.py").write_text("def test_safe(): pass\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "test_manual.py").write_text(
        "raise RuntimeError('manual acceptance was imported')\n", encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_safe.py::test_safe" in result.stdout
    assert "manual acceptance was imported" not in result.stdout
