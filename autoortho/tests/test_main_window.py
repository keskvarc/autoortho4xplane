import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ui.main_window import ApplicationShell
from ui.runtime_state import RuntimeState
from ui.task_models import TaskType


def test_shell_builds_default_pages_and_updates_header(qt_app):
    shell = ApplicationShell(app_name="AutoOrtho", version_text="1.0.0")

    assert shell.navigation.destination_keys() == [
        "home",
        "scenery-library",
        "flight-plan-map",
        "settings",
        "diagnostics",
    ]
    assert shell.current_page() is shell.page("home")
    assert shell.header.start_stop_button.text() == "Start"

    shell.set_runtime_state(
        RuntimeState.RUNNING, message="Scenery mounts are ready"
    )
    shell.set_xplane_state(True)
    shell.task_manager.create_task(
        "catalog", TaskType.CATALOG, "Refresh scenery catalog"
    )
    shell.set_update_available("2.0.0")

    assert shell.header.start_stop_button.text() == "Stop"
    assert shell.header.start_stop_button.toolTip() == "Scenery mounts are ready"
    assert shell.header.xplane_chip.text() == "✓ X-Plane connected"
    assert shell.header.findChildren(QLabel, "chipLabel") == [
        shell.header.xplane_chip,
    ]
    assert not shell.task_panel.isHidden()
    assert "catalog" in shell.task_panel.rows
    assert shell.header.update_banner.isHidden() is False

    shell.clear_update_available()
    assert shell.header.update_banner.isHidden() is True


def test_shell_page_switching_and_action_signals(qt_app):
    shell = ApplicationShell()
    signals = {
        "start": [],
        "stop": [],
        "setup": [],
        "docs": [],
        "about": [],
        "quit": [],
    }
    shell.startRequested.connect(lambda: signals["start"].append(True))
    shell.stopRequested.connect(lambda: signals["stop"].append(True))
    shell.setupWizardRequested.connect(lambda: signals["setup"].append(True))
    shell.docsRequested.connect(lambda: signals["docs"].append(True))
    shell.aboutRequested.connect(lambda: signals["about"].append(True))
    shell.quitRequested.connect(lambda: signals["quit"].append(True))

    extra = QWidget()
    extra.setObjectName("extras")
    shell.add_page(extra, key="extras", title="Extras")
    shell.set_page("extras")
    qt_app.processEvents()

    assert shell.current_page() is extra
    assert shell.page("extras") is extra

    shell.header.start_stop_button.click()
    shell.header.setup_wizard_action.trigger()
    shell.header.docs_action.trigger()
    shell.header.about_action.trigger()
    shell.header.quit_action.trigger()
    for state in (RuntimeState.STARTING, RuntimeState.STOPPING):
        shell.header.set_runtime_state(state)
        shell.header.start_stop_button.click()
    shell.header.set_runtime_state(RuntimeState.RUNNING)
    shell.header.start_stop_button.click()

    qt_app.processEvents()

    assert signals["start"] == [True]
    assert signals["stop"] == [True]
    assert signals["setup"] == [True]
    assert signals["docs"] == [True]
    assert signals["about"] == [True]
    assert signals["quit"] == [True]


def test_streaming_button_keeps_its_width_across_runtime_states(qt_app):
    shell = ApplicationShell()
    shell.show()
    qt_app.processEvents()
    button = shell.header.start_stop_button
    width = button.width()

    for state, label, enabled in (
        (RuntimeState.STOPPED, "Start", True),
        (RuntimeState.STARTING, "Starting…", False),
        (RuntimeState.RUNNING, "Stop", True),
        (RuntimeState.STOPPING, "Stopping…", False),
        (RuntimeState.STOPPED, "Start", True),
        (RuntimeState.ERROR, "Start", True),
    ):
        shell.set_runtime_state(state)
        qt_app.processEvents()
        assert button.text() == label
        assert button.accessibleName() == (
            f"{label.removesuffix('…')} scenery streaming"
        )
        assert button.isEnabled() == enabled
        assert button.width() == width
        assert button.width() >= button.sizeHint().width()

    shell.close()


def test_overflow_menu_groups_setup_help_and_exit_actions(qt_app):
    shell = ApplicationShell()
    labels = [
        "|" if action.isSeparator() else action.text()
        for action in shell.header.overflow_menu.actions()
    ]

    assert labels == [
        "Setup Wizard",
        "|",
        "Help and Documentation",
        "Check for Updates",
        "About",
        "|",
        "Quit",
    ]
