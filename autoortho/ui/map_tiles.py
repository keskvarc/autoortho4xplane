"""Bounded, read-only access to the imagery cache for the Home map."""

from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedMapTile:
    data: bytes
    zoom: int


class ReadOnlyTileCache:
    """Observe JPEGs without invoking fetchers, FUSE, or cache maintenance."""

    MAX_ZOOM = 22
    MAX_TILE_BYTES = 2 * 1024 * 1024
    MEMORY_BYTES = 8 * 1024 * 1024
    MEMORY_ENTRIES = 512
    MEMORY_TTL = 2.0

    def __init__(self, directory: str | Path, provider: str) -> None:
        if not str(directory).strip():
            raise ValueError("The map cache directory must not be empty")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", provider):
            raise ValueError(f"Invalid map imagery source: {provider!r}")
        self.directory = Path(directory).expanduser()
        self.provider = provider
        self._lock = threading.Lock()
        self._entries: OrderedDict[
            Path, tuple[float, bytes | None]
        ] = OrderedDict()
        self._memory_bytes = 0

    def read_tile(self, zoom: int, x: int, y: int) -> CachedMapTile | None:
        if (
            not all(type(value) is int for value in (zoom, x, y))
            or not 0 <= zoom <= self.MAX_ZOOM
            or not 0 <= x < (1 << zoom)
            or not 0 <= y < (1 << zoom)
        ):
            raise ValueError(f"Invalid map tile coordinates: {(zoom, x, y)}")
        for source_zoom in range(zoom, -1, -1):
            shift = zoom - source_zoom
            path = self.directory / (
                f"{x >> shift}_{y >> shift}_{source_zoom}_{self.provider}.jpg"
            )
            data = self._read_jpeg(path)
            if data is not None:
                return CachedMapTile(data, source_zoom)
        return None

    def _read_jpeg(self, path: Path) -> bytes | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.pop(path, None)
            if entry is not None:
                if now - entry[0] < self.MEMORY_TTL:
                    self._entries[path] = entry
                    return entry[1]
                self._memory_bytes -= len(entry[1] or b"")
        try:
            with path.open("rb") as stream:
                data = stream.read(self.MAX_TILE_BYTES + 1)
        except FileNotFoundError:
            data = None
        if data is not None:
            if len(data) > self.MAX_TILE_BYTES:
                raise ValueError(f"Cached map JPEG exceeds the size limit: {path}")
            # A writer may still be completing a JPEG; retry it on a later refresh.
            if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
                log.warning("Incomplete or invalid cached map JPEG: %s", path)
                data = None
        with self._lock:
            previous = self._entries.pop(path, None)
            if previous is not None:
                self._memory_bytes -= len(previous[1] or b"")
            self._entries[path] = (now, data)
            self._memory_bytes += len(data or b"")
            while (
                len(self._entries) > self.MEMORY_ENTRIES
                or self._memory_bytes > self.MEMORY_BYTES
            ):
                _, (_, evicted) = self._entries.popitem(last=False)
                self._memory_bytes -= len(evicted or b"")
        return data


class MapBridge(QObject):
    """App-local WebChannel transport with two reads and a bounded backlog."""

    MAX_ACTIVE = 2
    MAX_PENDING = 64

    tile_ready = Signal(int, dict)
    flight_changed = Signal(dict)
    cache_changed = Signal()
    refresh_requested = Signal()
    follow_changed = Signal(bool)
    recenter_requested = Signal()
    ready_received = Signal()
    follow_paused = Signal()
    coverage_changed = Signal(int, int, int)
    error_received = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cache: ReadOnlyTileCache | None = None
        self._enabled = True
        self._generation = 0
        self._pending: OrderedDict[int, tuple[int, int, int]] = OrderedDict()
        self._active: dict[
            int, tuple[int, Future[CachedMapTile | None]]
        ] = {}
        self._cancelled: set[int] = set()
        self._executor: ThreadPoolExecutor | None = None
        self._completion_timer = QTimer(self)
        self._completion_timer.setInterval(16)
        self._completion_timer.timeout.connect(self._drain_completed)

    def configure(self, directory: str, provider: str) -> None:
        if (
            self.cache is not None
            and self.cache.directory == Path(directory).expanduser()
            and self.cache.provider == provider
        ):
            return
        self._generation += 1
        self._pending.clear()
        self.cache = None
        try:
            self.cache = ReadOnlyTileCache(directory, provider)
        finally:
            self.cache_changed.emit()

    def set_active(self, active: bool) -> None:
        self._enabled = active
        if not active:
            self._cancelled.update(self._active)
            self._pending.clear()

    def shutdown(self) -> None:
        self.set_active(False)
        self._completion_timer.stop()
        for _, future in self._active.values():
            future.cancel()
        self._active.clear()
        self._cancelled.clear()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    @Slot()
    def ready(self) -> None:
        self.ready_received.emit()

    @Slot()
    def pauseFollow(self) -> None:
        self.follow_paused.emit()

    @Slot(int, int, int)
    def reportCoverage(self, cached: int, total: int, lower_zoom: int) -> None:
        self.coverage_changed.emit(cached, total, lower_zoom)

    @Slot(str)
    def reportError(self, message: str) -> None:
        log.error("Home map: %s", message)
        self.error_received.emit(message)

    @Slot(int, int, int, int)
    def requestTile(self, request_id: int, zoom: int, x: int, y: int) -> None:
        if not self._enabled or self.cache is None:
            self.tile_ready.emit(request_id, {})
            return
        if request_id in self._pending or request_id in self._active:
            self.reportError("Duplicate map tile request")
            self.tile_ready.emit(request_id, {})
            return
        if len(self._pending) >= self.MAX_PENDING:
            log.warning("Home map tile backlog is full")
            self.tile_ready.emit(request_id, {"error": "Map tile backlog is full"})
            return
        self._pending[request_id] = (zoom, x, y)
        self._pump()

    @Slot(int)
    def cancelTile(self, request_id: int) -> None:
        self._pending.pop(request_id, None)
        if request_id in self._active:
            self._cancelled.add(request_id)

    def _pump(self) -> None:
        while (
            self._enabled
            and self.cache is not None
            and self._pending
            and len(self._active) < self.MAX_ACTIVE
        ):
            request_id, coordinates = self._pending.popitem(last=False)
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.MAX_ACTIVE, thread_name_prefix="HomeMap"
                )
            future = self._executor.submit(self.cache.read_tile, *coordinates)
            self._active[request_id] = (self._generation, future)
            self._completion_timer.start()

    @Slot()
    def _drain_completed(self) -> None:
        for request_id, (_, future) in list(self._active.items()):
            if not future.done():
                continue
            try:
                tile = future.result()
            except (OSError, ValueError) as error:
                log.warning("Cannot read Home map imagery: %s", error)
                self._finish(request_id, None, str(error))
            else:
                self._finish(request_id, tile, "")
        self._pump()
        if not self._active:
            self._completion_timer.stop()

    def _finish(
        self, request_id: int, tile: CachedMapTile | None, error: str,
    ) -> None:
        generation, _ = self._active.pop(request_id)
        cancelled = request_id in self._cancelled
        self._cancelled.discard(request_id)
        if not cancelled and self._enabled and generation == self._generation:
            result = {}
            if error:
                result["error"] = error
                self.error_received.emit(error)
            elif tile is not None:
                result = {
                    "image": base64.b64encode(tile.data).decode("ascii"),
                    "zoom": tile.zoom,
                }
            self.tile_ready.emit(request_id, result)
