import os
from pathlib import Path
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from datareftrack import DatarefTracker


def test_packaged_startup_shares_telemetry_with_the_ui(tmp_path):
    project = Path(__file__).resolve().parents[2]
    script = """
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from autoortho import autoortho as application
from autoortho.datareftrack import dt
from autoortho.ui.main_window import ConfigUI
from autoortho.ui.runtime_state import RuntimeState

calls = []
dt.start = lambda: calls.append("start")
dt.stop = lambda: calls.append("stop")

def unexpected_legacy_tracker():
    raise AssertionError("Startup used a separate legacy telemetry instance")

sys.modules["datareftrack"] = SimpleNamespace(
    dt=SimpleNamespace(
        start=unexpected_legacy_tracker,
        stop=unexpected_legacy_tracker,
    ),
)
sys.modules["flighttrack"] = SimpleNamespace(
    run=lambda: None,
    ft=SimpleNamespace(stop=lambda: None),
)
sys.modules["autoortho.__main__"] = SimpleNamespace(
    _global_shutdown=lambda: None,
)
application.threading = SimpleNamespace(
    Thread=lambda **kwargs: SimpleNamespace(start=lambda: None),
)
application.AOMount = lambda cfg: SimpleNamespace(mount_sceneries=lambda: None)
sys.argv = ["autoortho", "--headless"]
application.main()
assert calls == ["start", "stop"], calls

dt.connected = True
dt.data_valid = True
dt.lat = 30.201738357543945
dt.lon = -97.66309356689453
dt.alt = 0.08577370643615723
ui = Mock(
    phase3_active=True,
    runtime_state=RuntimeState.RUNNING,
    cfg=application.aoconfig.CFG,
    simbrief_flight_data=None,
    current_readiness=None,
)
ui._has_active_ui_jobs.return_value = False
ConfigUI._update_shell_status(ui)
ui.shell.set_xplane_state.assert_called_once_with(True)
flight = ui.home_page.set_flight_data.call_args.args[0]
assert flight["lat"] == dt.lat
assert flight["lon"] == dt.lon

from autoortho import autoortho_fuse

autoortho_fuse.CFG.autoortho.tile_time_budget = 20
autoortho_fuse.CFG.autoortho.suspend_maxwait = True
autoortho_fuse.CFG.autoortho.fallback_extends_budget = False
dt.has_ever_connected = True
filesystem = object.__new__(autoortho_fuse.AutoOrtho)
assert filesystem._calculate_build_timeout() == 35
autoortho_fuse.getortho.shutdown()
dt.sock.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("true_heading", [12.0, None, 400.0])
def test_optional_true_heading_does_not_change_magnetic_heading_or_legacy_packets(
    monkeypatch, true_heading,
):
    tracker = DatarefTracker()
    tracker.sock.close()
    values = [47.45, -122.31, 1000, 359, 80, 12000, 3000, 20]
    if true_heading is not None:
        values.append(true_heading)
    packet = b"RREF\x00" + b"".join(
        struct.pack("<if", index, value) for index, value in enumerate(values)
    )

    class Socket:
        def recvfrom(self, size):
            tracker.running = False
            return packet, ("127.0.0.1", 49000)

    tracker.sock = Socket()
    monkeypatch.setattr(
        DatarefTracker, "_request_datarefs", lambda self, **kwargs: None,
    )
    tracker.running = True
    try:
        tracker._dataref_tracker_udp_listen()
    finally:
        tracker.sock = None
    data = tracker.get_flight_data()
    assert data is not None
    assert data["hdg"] == 359
    assert data["true_hdg"] == (
        true_heading if true_heading is not None and true_heading <= 360 else None
    )
