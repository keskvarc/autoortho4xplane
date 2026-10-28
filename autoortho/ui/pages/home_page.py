"""Offline moving map backed by shared telemetry and read-only cached JPEGs."""

from __future__ import annotations

import logging
import math
import os
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineDownloadRequest,
    QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..map_tiles import MapBridge
from ..theme import _asset_path

log = logging.getLogger(__name__)


class _LocalAssetsOnly(QWebEngineUrlRequestInterceptor):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.root = os.path.abspath(_asset_path("map"))

    def interceptRequest(self, info) -> None:
        url = info.requestUrl()
        if url.scheme() == "file":
            path = os.path.abspath(url.toLocalFile())
            try:
                allowed = os.path.commonpath((self.root, path)) == self.root
            except ValueError:
                allowed = False
        else:
            allowed = url.scheme() in {"qrc", "data", "about"}
        if not allowed:
            log.warning("Blocked non-local Home map request: %s", url.toString())
            info.block(True)


class _MapPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source) -> None:
        if level == self.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            log.error("Home map JavaScript (%s:%s): %s", source, line, message)
        elif level == self.JavaScriptConsoleMessageLevel.WarningMessageLevel:
            log.warning("Home map JavaScript (%s:%s): %s", source, line, message)


class HomePage(QWidget):
    """Map-first Home; only a visible page starts an embedded renderer."""

    STALE_SECONDS = 5.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("homePage")
        self.setAccessibleName("Live aircraft map")
        self.setAccessibleDescription(
            "Offline world map with cached satellite imagery. "
            "Drag or use arrow keys to pan; use plus and minus to zoom."
        )
        self.bridge = MapBridge(self)
        self.view: QWebEngineView | None = None
        self._ready = False
        self._flight_state = {"connected": False}
        self._last_update = 0.0
        self._provider = "BI"
        self._configuration: tuple[str, str] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(10, 4, 10, 6)
        self.follow_button = QPushButton("Following")
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(True)
        self.follow_button.setFixedWidth(130)
        self.follow_button.setAccessibleName("Follow aircraft")
        self.follow_button.setToolTip(
            "Keep the aircraft centred. Dragging the map pauses following."
        )
        self.recenter_button = QPushButton("Recenter")
        self.recenter_button.setEnabled(False)
        self.recenter_button.setAccessibleName("Recenter on aircraft")
        self.recenter_button.setToolTip(
            "Centre the map on the last known aircraft position."
        )
        self.streaming_counts_label = QLabel("Streamed: 0  |  Cache hits: 0")
        self.streaming_counts_label.setAccessibleName("Streaming session counts")
        self.streaming_counts_label.setToolTip(
            "Successful imagery downloads and memory/disk cache hits "
            "for this streaming session."
        )
        toolbar.addWidget(self.follow_button)
        toolbar.addWidget(self.recenter_button)
        toolbar.addStretch()
        toolbar.addWidget(self.streaming_counts_label)
        outer.addLayout(toolbar)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setProperty("validationState", "error")
        self.error_label.setContentsMargins(10, 4, 10, 6)
        self.error_label.hide()
        outer.addWidget(self.error_label)

        self.loading_label = QLabel("Loading offline map...")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setWordWrap(True)
        outer.addWidget(self.loading_label, 1)

        self.cache_label = QLabel("Cached JPEG imagery only. No scenery downloads.")
        self.cache_label.setProperty("textRole", "secondary")
        self.cache_label.setWordWrap(True)
        self.cache_label.setContentsMargins(10, 4, 10, 6)
        self.cache_label.setToolTip(
            "Missing JPEGs use a cached lower-resolution parent if available. "
            "DDS-only scenery is not decoded for this preview. "
            "Overview: Natural Earth (public domain). Map: Leaflet."
        )
        outer.addWidget(self.cache_label)

        self.follow_button.toggled.connect(self._set_following)
        self.recenter_button.clicked.connect(self._recenter)
        self.bridge.follow_paused.connect(self._pause_follow)
        self.bridge.ready_received.connect(self._map_ready)
        self.bridge.coverage_changed.connect(self._set_coverage)
        self.bridge.error_received.connect(self._map_error)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self._refresh_imagery)
        self.stale_timer = QTimer(self)
        self.stale_timer.setInterval(1000)
        self.stale_timer.timeout.connect(self._check_stale)
        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.setInterval(15000)
        self.startup_timer.timeout.connect(self._startup_timed_out)

    def set_streaming_counts(
        self, streamed: int | None, cache_hits: int | None,
    ) -> None:
        if streamed is None or cache_hits is None:
            self.streaming_counts_label.setText("Streamed: --  |  Cache hits: --")
        else:
            self.streaming_counts_label.setText(
                f"Streamed: {streamed:,}  |  Cache hits: {cache_hits:,}"
            )

    def configure_cache(self, directory: str, provider: str) -> None:
        if self._configuration == (directory, provider):
            return
        self._configuration = (directory, provider)
        self._provider = "BI" if provider == "Use tile default" else provider
        try:
            self.bridge.configure(directory, self._provider)
        except ValueError as error:
            self._map_error(str(error))
        else:
            self.error_label.hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.bridge.set_active(True)
        self._check_stale()
        if self.view is None:
            self._create_map()
        else:
            self.view.page().setLifecycleState(
                QWebEnginePage.LifecycleState.Active
            )
            if self.isVisible():
                self.bridge.flight_changed.emit(self._flight_state)
            self.bridge.cache_changed.emit()
            self.bridge.refresh_requested.emit()
        self.refresh_timer.start()
        self.stale_timer.start()

    def hideEvent(self, event) -> None:
        self.refresh_timer.stop()
        self.stale_timer.stop()
        self.bridge.set_active(False)
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    @Slot()
    def shutdown(self) -> None:
        self.refresh_timer.stop()
        self.stale_timer.stop()
        self.startup_timer.stop()
        self.bridge.shutdown()
        if self.view is not None:
            # Release WebEngine while the application's render context still exists.
            QObject.disconnect(self._quit_connection)
            self.view.stop()
            self.view.deleteLater()
            self.profile.deleteLater()
            self.view = None
            self._ready = False

    def _create_map(self) -> None:
        self._quit_connection = QApplication.instance().aboutToQuit.connect(
            self.shutdown
        )
        self.view = QWebEngineView(self)
        self.view.setObjectName("aircraftMap")
        self.view.setAccessibleName("Interactive aircraft map")
        # The profile must outlive the view and its page.
        self.profile = QWebEngineProfile(self)
        self.interceptor = _LocalAssetsOnly(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.profile.downloadRequested.connect(self._cancel_download)
        page = _MapPage(self.profile, self.view)
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        self.channel = QWebChannel(page)
        self.channel.registerObject("backend", self.bridge)
        page.setWebChannel(self.channel)
        self.view.setPage(page)
        self.view.loadFinished.connect(self._loaded)
        self.view.renderProcessTerminated.connect(self._renderer_terminated)
        self.layout().insertWidget(2, self.view, 1)
        self.startup_timer.start()
        self.view.load(QUrl.fromLocalFile(_asset_path("map/index.html")))

    def _loaded(self, ok: bool) -> None:
        if not ok:
            self._map_error("Cannot load the bundled offline map.")

    @Slot()
    def _pause_follow(self) -> None:
        self.follow_button.setChecked(False)

    @Slot()
    def _recenter(self) -> None:
        self.bridge.recenter_requested.emit()

    @Slot()
    def _refresh_imagery(self) -> None:
        self.bridge.refresh_requested.emit()

    @Slot()
    def _startup_timed_out(self) -> None:
        self._map_error("Offline map startup timed out. See Diagnostics.")

    @Slot(QWebEngineDownloadRequest)
    def _cancel_download(self, download: QWebEngineDownloadRequest) -> None:
        download.cancel()

    @Slot(QWebEnginePage.RenderProcessTerminationStatus, int)
    def _renderer_terminated(self, status, code: int) -> None:
        self._map_error(
            f"Map renderer stopped ({status.name}, exit {code}). Relaunch the app."
        )

    @Slot()
    def _map_ready(self) -> None:
        self._ready = True
        self.startup_timer.stop()
        self.loading_label.hide()
        self.bridge.follow_changed.emit(self.follow_button.isChecked())
        self.bridge.flight_changed.emit(self._flight_state)

    @Slot(bool)
    def _set_following(self, following: bool) -> None:
        self.follow_button.setText("Following" if following else "Follow aircraft")
        self.bridge.follow_changed.emit(following)

    @Slot(int, int, int)
    def _set_coverage(self, cached: int, total: int, lower_zoom: int) -> None:
        coverage = f"{cached}/{total} tiles" if cached else "No cached imagery here"
        fallback = f" ({lower_zoom} lower-resolution)" if lower_zoom else ""
        self.cache_label.setText(
            f"{self._provider} JPEG cache: {coverage}{fallback}. "
            "Offline overview: Natural Earth."
        )

    @Slot(str)
    def _map_error(self, message: str) -> None:
        log.error("Home map: %s", message)
        self.error_label.setText(f"Map error: {message}")
        self.error_label.show()
        if not self._ready:
            self.loading_label.setText(f"Map unavailable: {message}")

    def set_flight_data(self, data: dict | None) -> None:
        state = {"connected": False}
        if data is not None:
            try:
                lat, lon = float(data["lat"]), float(data["lon"])
                stamp = float(data["timestamp"])
                if (
                    not data["connected"] or not data["data_valid"]
                    or not math.isfinite(lat) or not -90 <= lat <= 90
                    or not math.isfinite(lon) or not -180 <= lon <= 180
                    or not math.isfinite(stamp)
                    or not -1 <= time.time() - stamp <= self.STALE_SECONDS
                ):
                    raise ValueError("Invalid or stale aircraft position")
                heading = data.get("true_hdg")
                if heading is not None:
                    heading = float(heading)
                    if not math.isfinite(heading) or not 0 <= heading <= 360:
                        heading = None
                state = {
                    "connected": True, "lat": lat, "lon": lon,
                    "heading": heading,
                }
            except (KeyError, TypeError, ValueError) as error:
                log.warning("Home map rejected telemetry: %s", error)
        self._flight_state = state
        if state["connected"]:
            self._last_update = time.monotonic()
            self.recenter_button.setEnabled(True)
        if self._ready and self.isVisible():
            self.bridge.flight_changed.emit(state)

    def _check_stale(self) -> None:
        if (
            self._flight_state["connected"]
            and time.monotonic() - self._last_update > self.STALE_SECONDS
        ):
            self.set_flight_data(None)


__all__ = ["HomePage"]
