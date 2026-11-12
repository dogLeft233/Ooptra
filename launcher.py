"""Managed launcher for WebUI upgrades and rollback; python main.py remains usable."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from operations import create_supervisor

if __name__ == "__main__":
    raise SystemExit(create_supervisor().supervise(Path(__file__).resolve().parent))
