import json
import tracemalloc

from context import create_memory as MemoryStore


def test_long_history_recent_has_bounded_peak_allocation(tmp_path):
    path = tmp_path / "memory.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for index in range(10000):
            stream.write(json.dumps({"role": "user", "content": str(index) + "x" * 500}) + "\n")
    memory = MemoryStore(str(path), 20)
    tracemalloc.start()
    try:
        rows = memory.recent(limit=5)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(rows) == 5
    assert rows[-1]["content"].startswith("9999")
    assert peak < 512 * 1024
