import os
from pathlib import Path
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from ui.map_tiles import MapBridge, ReadOnlyTileCache


JPEG = b"\xff\xd8\xff" + b"cached image fixture" + b"\xff\xd9"


def test_cache_observation_does_not_create_files_or_touch_retention_timestamps(tmp_path):
    root = tmp_path / "missing-cache"
    cache = ReadOnlyTileCache(root, "BI")
    assert cache.read_tile(16, 14884, 27057) is None
    assert not root.exists()
    root.mkdir()
    path = root / "14884_27057_16_BI.jpg"
    path.write_bytes(JPEG)
    os.utime(path, (1700000000, 1700000000))
    before = path.stat().st_mtime_ns
    cache.MEMORY_TTL = 0
    tile = cache.read_tile(16, 14884, 27057)
    assert tile.data == JPEG
    assert tile.zoom == 16
    assert path.stat().st_mtime_ns == before
    assert list(root.iterdir()) == [path]


def test_missing_detail_uses_the_matching_parent_not_a_neighbour(tmp_path):
    (tmp_path / "14884_27057_16_BI.jpg").write_bytes(JPEG)
    (tmp_path / "14885_27057_16_BI.jpg").write_bytes(JPEG + b"other")
    cache = ReadOnlyTileCache(tmp_path, "BI")
    tile = cache.read_tile(18, 14884 * 4 + 3, 27057 * 4 + 2)
    assert tile.zoom == 16
    assert tile.data == JPEG
    assert cache.read_tile(18, 14884 * 4, 27058 * 4) is None


def test_concurrent_writes_and_eviction_are_rechecked_without_repairing_cache(tmp_path):
    path = tmp_path / "14884_27057_16_BI.jpg"
    path.write_bytes(JPEG[:-2])
    cache = ReadOnlyTileCache(tmp_path, "BI")
    cache.MEMORY_TTL = 0
    assert cache.read_tile(16, 14884, 27057) is None
    assert path.read_bytes() == JPEG[:-2]
    path.write_bytes(JPEG)
    assert cache.read_tile(16, 14884, 27057).data == JPEG
    path.unlink()
    assert cache.read_tile(16, 14884, 27057) is None


@pytest.mark.parametrize("coordinates", [
    (-1, 0, 0), (23, 0, 0), (16, -1, 0), (16, 0, 65536), (1, True, 0),
])
def test_invalid_coordinates_and_provider_cannot_form_cache_paths(tmp_path, coordinates):
    cache = ReadOnlyTileCache(tmp_path, "BI")
    with pytest.raises(ValueError, match="coordinates"):
        cache.read_tile(*coordinates)
    with pytest.raises(ValueError, match="source"):
        ReadOnlyTileCache(tmp_path, "../private")


def test_large_reads_and_in_memory_cache_are_bounded(tmp_path):
    cache = ReadOnlyTileCache(tmp_path, "BI")
    cache.MAX_TILE_BYTES = 64
    cache.MEMORY_ENTRIES = 2
    for x in range(3):
        (tmp_path / f"{x}_0_2_BI.jpg").write_bytes(JPEG)
        assert cache.read_tile(2, x, 0) is not None
    assert len(cache._entries) <= 2
    (tmp_path / "3_0_2_BI.jpg").write_bytes(b"x" * 65)
    with pytest.raises(ValueError, match="size limit"):
        cache.read_tile(2, 3, 0)


def test_tile_jobs_are_bounded_and_old_provider_results_are_discarded(
    qt_app, monkeypatch, tmp_path,
):
    bridge = MapBridge()
    bridge.MAX_PENDING = 3
    bridge.configure(str(tmp_path), "BI")
    gate = threading.Event()
    results = []
    bridge.tile_ready.connect(lambda request_id, result: results.append(request_id))

    def blocked(*coordinates):
        assert gate.wait(10)
        return None

    monkeypatch.setattr(bridge.cache, "read_tile", blocked)
    try:
        for request_id in range(10):
            bridge.requestTile(request_id, 16, 14884, 27057)
        assert len(bridge._active) == 2
        assert len(bridge._pending) == 3
        bridge.cancelTile(2)
        assert 2 not in bridge._pending
        bridge.configure(str(tmp_path), "ARC")
        bridge.requestTile(20, 16, 14884, 27057)
    finally:
        gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and 20 not in results:
        qt_app.processEvents()
        time.sleep(0.01)
    assert 20 in results
    assert 0 not in results and 1 not in results
    assert not bridge._active and not bridge._pending
    bridge.shutdown()
