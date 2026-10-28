import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QSizePolicy

from ui.navigation import NavigationDestination, NavigationRail
from ui.theme import apply_theme


def test_navigation_rail_exposes_five_destinations(qt_app):
    rail = NavigationRail()

    assert rail.destination_keys() == [
        "home",
        "scenery-library",
        "flight-plan-map",
        "settings",
        "diagnostics",
    ]
    assert rail.current_destination() == "home"
    for destination in rail.destinations():
        button = rail.button_for(destination.key)
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        assert not button.icon().pixmap(button.iconSize()).isNull()
        assert button.toolTip() == destination.title
        assert button.accessibleName() == f"Open {destination.title}"
        assert button.minimumHeight() >= 42
    assert (
        rail.sizePolicy().horizontalPolicy()
        == QSizePolicy.Policy.Fixed
    )


def test_icon_rail_stays_narrow_with_long_titles_and_theme(qt_app):
    apply_theme(qt_app)
    title = "Scenery Library With A Much Longer Localized Name"
    rail = NavigationRail([
        NavigationDestination("custom", title, tooltip="Manage scenery packs"),
    ])
    rail.show()
    qt_app.processEvents()

    button = rail.button_for("custom")
    assert rail.width() <= 64
    assert button.toolTip() == "Manage scenery packs"
    assert button.accessibleName() == f"Open {title}"
    assert not button.icon().isNull()
    assert button.width() >= button.sizeHint().width()
    assert button.height() >= button.sizeHint().height()
    assert rail.contentsRect().contains(button.geometry())
    rail.close()


def test_navigation_rail_emits_selection_changes(qt_app):
    rail = NavigationRail()
    seen = []
    rail.destinationChanged.connect(seen.append)

    rail.set_current_destination("diagnostics")
    qt_app.processEvents()

    assert rail.current_destination() == "diagnostics"
    assert seen == ["diagnostics"]

    rail.button_for("settings").click()
    assert rail.current_destination() == "settings"
    assert seen == ["diagnostics", "settings"]
    assert [
        key for key in rail.destination_keys()
        if rail.button_for(key).isChecked()
    ] == ["settings"]
