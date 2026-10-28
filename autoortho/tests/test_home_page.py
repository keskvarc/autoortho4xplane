import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, QUrl
from PySide6.QtGui import QColor, QImage, QPainter

from ui.pages.home_page import HomePage, _LocalAssetsOnly


def _wait(app, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for the Home map")


def _js(page, expression):
    result = []
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    page.view.page().runJavaScript(
        f"JSON.stringify({expression})",
        lambda value: (result.append(value), loop.quit()),
    )
    timer.start(2000)
    loop.exec()
    timer.stop()
    assert result and result[0], f"Map JavaScript did not complete: {expression}"
    return json.loads(result[0])


def _flight(lat=47.45, lon=-122.31, **overrides):
    return {
        "lat": lat, "lon": lon, "true_hdg": 270.0,
        "connected": True, "data_valid": True, "timestamp": time.time(),
        **overrides,
    }


@pytest.fixture
def map_page(qt_app, tmp_path):
    page = HomePage()
    page.configure_cache(str(tmp_path / "cache"), "Use tile default")
    page.resize(800, 700)
    page.show()
    _wait(qt_app, lambda: page._ready)
    yield page
    page.close()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


def test_offline_map_tracks_aircraft_and_pauses_follow_when_panning(
    qt_app, map_page, tmp_path,
):
    page = map_page
    assert _js(page, "!!document.querySelector('.leaflet-overlay-pane path')")
    assert _js(page, "window.autoorthoMap.marker === null")
    assert not (tmp_path / "cache").exists()
    page.set_flight_data(_flight())
    _wait(qt_app, lambda: _js(page, "window.autoorthoMap.marker !== null"))
    assert _js(page, "window.autoorthoMap.map.getZoom()") >= 12
    assert _js(page, "window.autoorthoMap.marker.getLatLng().lat") == 47.45

    _js(page, "(window.autoorthoMap.map.fire('dragstart'), window.autoorthoMap.map.panTo([48, -123], {animate:false}), true)")
    _wait(qt_app, lambda: not page.follow_button.isChecked())
    page.set_flight_data(_flight(47.46, -122.32))
    _wait(qt_app, lambda: _js(page, "window.autoorthoMap.marker.getLatLng().lat") == 47.46)
    assert _js(page, "window.autoorthoMap.map.getCenter().lat") == pytest.approx(48, abs=0.001)
    page.follow_button.click()
    _wait(qt_app, lambda: abs(_js(page, "window.autoorthoMap.map.getCenter().lat") - 47.46) < 0.001)

    page.set_flight_data(None)
    _wait(qt_app, lambda: _js(page, "window.autoorthoMap.marker.getElement().classList.contains('aircraft-stale')"))
    assert "last known position" in _js(page, "document.getElementById('position').textContent")
    assert page.recenter_button.isEnabled()


def test_cached_parent_is_cropped_into_the_correct_zoomed_map_tile(
    qt_app, map_page, tmp_path,
):
    # Coordinates captured from the real BI cache on onix.
    parent_x, parent_y, parent_zoom = 14884, 27057, 16
    cache = tmp_path / "cache"
    cache.mkdir()
    image = QImage(256, 256, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    for x, y, colour in (
        (0, 0, "#c81414"), (128, 0, "#14c814"),
        (0, 128, "#1414c8"), (128, 128, "#c8c814"),
    ):
        painter.fillRect(x, y, 128, 128, QColor(colour))
    painter.end()
    assert image.save(str(cache / f"{parent_x}_{parent_y}_{parent_zoom}_BI.jpg"), "JPEG", 95)

    x, y, zoom = parent_x * 4 + 3, parent_y * 4 + 2, 18
    lon = (x + 0.5) / (1 << zoom) * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / (1 << zoom)))))
    page = map_page
    page.set_flight_data(_flight(lat, lon))
    _wait(qt_app, lambda: _js(page, "window.autoorthoMap.marker !== null"))
    _js(page, f"(window.autoorthoMap.map.setView([{lat}, {lon}], {zoom}, {{animate:false}}), true)")
    expression = (
        "(() => {"
        f"const t = Object.values(window.autoorthoMap.imagery._tiles).find(t => t.coords.x === {x} && t.coords.y === {y} && t.coords.z === {zoom});"
        "return t?.el.dataset.cached === 'true'"
        " ? Array.from(t.el.getContext('2d').getImageData(128,128,1,1).data) : null;"
        "})()"
    )
    _wait(qt_app, lambda: _js(page, expression) is not None)
    pixel = _js(page, expression)
    assert pixel[:3] == pytest.approx([200, 200, 20], abs=4)
    assert pixel[3] == 255
    point = _js(page, (
        "(() => {"
        f"const t = Object.values(window.autoorthoMap.imagery._tiles).find(t => t.coords.x === {x} && t.coords.y === {y} && t.coords.z === {zoom});"
        "const r = t.el.getBoundingClientRect(); return [r.left + 32, r.top + 32];"
        "})()"
    ))

    def composite_is_imagery():
        rendered = page.view.grab().toImage()
        ratio = rendered.devicePixelRatio()
        colour = rendered.pixelColor(round(point[0] * ratio), round(point[1] * ratio))
        return all(
            abs(actual - expected) <= 4
            for actual, expected in zip(colour.getRgb(), [200, 200, 20, 255])
        )

    _wait(qt_app, composite_is_imagery)
    _wait(qt_app, lambda: "lower-resolution" in page.cache_label.text())
    assert sorted(p.name for p in cache.iterdir()) == [
        f"{parent_x}_{parent_y}_{parent_zoom}_BI.jpg",
    ]


def test_stale_and_invalid_telemetry_never_remains_live(qt_app, caplog):
    page = HomePage()
    page.set_flight_data(_flight())
    assert page._flight_state["connected"]
    page._last_update -= page.STALE_SECONDS + 1
    page._check_stale()
    assert not page._flight_state["connected"]
    for invalid in (
        _flight(lat=float("nan")), _flight(lon=181),
        _flight(timestamp=time.time() - 10),
    ):
        page.set_flight_data(invalid)
        assert not page._flight_state["connected"]
    assert "rejected telemetry" in caplog.text
    page.close()


def test_streaming_counts_fit_beside_map_controls(qt_app, map_page):
    page = map_page
    page.set_streaming_counts(1234567, 9876543)
    qt_app.processEvents()
    label = page.streaming_counts_label
    assert label.text() == "Streamed: 1,234,567  |  Cache hits: 9,876,543"
    assert label.geometry().left() > page.recenter_button.geometry().right()
    assert label.geometry().right() <= page.width()
    assert page.minimumSizeHint().width() <= 800
    page.set_flight_data(_flight())
    page.set_flight_data(None)
    assert label.text() == "Streamed: 1,234,567  |  Cache hits: 9,876,543"


def test_map_requests_cannot_access_network_or_arbitrary_local_files(qt_app):
    class Request:
        def __init__(self, url):
            self.url = QUrl(url)
            self.blocked = False

        def requestUrl(self):
            return self.url

        def block(self, value):
            self.blocked = value

    interceptor = _LocalAssetsOnly()
    for url in ("https://example.com/tile.jpg", "file:///etc/passwd"):
        request = Request(url)
        interceptor.interceptRequest(request)
        assert request.blocked
    request = Request(QUrl.fromLocalFile(str(Path(interceptor.root) / "map.js")).toString())
    interceptor.interceptRequest(request)
    assert not request.blocked


def test_loaded_map_shuts_down_cleanly_with_the_application():
    root = Path(__file__).resolve().parents[2]
    script = """
import sys
sys.path.insert(0, 'autoortho')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ui.pages.home_page import HomePage
app = QApplication([])
page = HomePage()
page.resize(800, 700)
page.bridge.ready_received.connect(lambda: QTimer.singleShot(100, page.close))
QTimer.singleShot(15000, lambda: app.exit(2))
page.show()
assert app.exec() == 0
assert page.view is None
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
