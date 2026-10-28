import os
import sys
from dataclasses import FrozenInstanceError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from ui.runtime_state import RuntimeState
from ui.shell import CompactHeader
from ui.theme import (
    THEME,
    apply_theme,
    build_stylesheet,
    contrast_ratio,
    repolish,
    validate_theme_tokens,
)


def test_theme_tokens_are_immutable_and_accessible():
    assert THEME.colors.window.startswith("#")
    assert THEME.spacing.md > 0
    assert THEME.radius.pill > 0
    assert THEME.typography.title > THEME.typography.base
    assert THEME.control_heights.md > 0

    with pytest.raises(FrozenInstanceError):
        THEME.colors.window = "#000000"


def test_contrast_helper_and_theme_validation():
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0)
    assert contrast_ratio(THEME.colors.text_primary, THEME.colors.window) >= 4.5
    assert contrast_ratio(THEME.colors.border, THEME.colors.surface) >= 3.0
    assert validate_theme_tokens(THEME) == []


def test_stylesheet_contains_required_selectors_and_states():
    sheet = build_stylesheet(THEME)

    required = [
        'QPushButton[role="primary"]',
        'QPushButton[role="primary"]:disabled',
        'QPushButton[role="secondary"]:disabled',
        'QPushButton[role="quiet"]:disabled',
        'QPushButton[role="primary"]:focus',
        'QPushButton[role="secondary"]:focus',
        'QPushButton[role="destructive"]:focus',
        'QPushButton[role="secondary"]',
        'QPushButton[role="destructive"]',
        'QPushButton[role="quiet"]',
        'QPushButton#startStopButton',
        'QCheckBox::indicator:checked',
        'QCheckBox:focus',
        'QCheckBox::indicator:indeterminate',
        'QLineEdit[validationError="true"]',
        'QLineEdit:focus',
        'QHeaderView::section',
        'QTableView',
        'QListView',
        'QMenu::item:selected',
        'QToolTip',
        'QStatusBar',
        'QSlider::groove:horizontal',
        'QSlider::handle:vertical',
        'QSpinBox::up-button',
        'QProgressBar::chunk',
        'QFrame#navigationRail',
        'QLabel#chipLabel',
        'QFrame#updateBanner',
    ]
    for selector in required:
        assert selector in sheet

    assert "font-family" not in sheet.lower()
    assert "checkmark.svg" in sheet
    assert sheet.rfind('QPushButton[role="primary"]:focus') > sheet.find(
        'QPushButton[role="primary"]'
    )
    assert sheet.rfind('QPushButton[role="secondary"]:disabled') > sheet.find(
        'QPushButton[role="secondary"]'
    )


def test_apply_theme_uses_general_font_and_fusion_style(qt_app):
    apply_theme(qt_app, THEME)

    general_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    assert qt_app.font().family() == general_font.family()
    assert qt_app.property("autoorthoBaseStyle") == "Fusion"
    assert qt_app.styleSheet() == build_stylesheet(THEME)


def test_repolish_handles_dynamic_properties(qt_app):
    button = QPushButton("Primary")
    button.setProperty("role", "primary")
    repolish(button)
    assert button.property("role") == "primary"


def test_streaming_button_renders_green_only_when_running(qt_app):
    apply_theme(qt_app)
    header = CompactHeader()
    header.show()
    button = header.start_stop_button
    QTest.mouseMove(header, QPoint(1, 1))

    def background():
        qt_app.processEvents()
        return button.grab().toImage().pixelColor(8, button.height() // 2)

    header.set_runtime_state(RuntimeState.RUNNING)
    assert background() == QColor(THEME.colors.success)
    assert contrast_ratio(
        button.palette().buttonText().color(), background()
    ) >= 4.5

    QTest.mouseMove(button, QPoint(8, button.height() // 2))
    color = background()
    assert color.green() > max(color.red(), color.blue())
    QTest.mousePress(button, Qt.MouseButton.LeftButton)
    color = background()
    assert color.green() > max(color.red(), color.blue())
    QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
    QTest.mouseMove(header, QPoint(1, 1))

    header.set_runtime_state(RuntimeState.RUNNING, action_enabled=False)
    assert background() == QColor(THEME.colors.window)
    for state in (RuntimeState.STARTING, RuntimeState.STOPPING):
        header.set_runtime_state(state)
        assert background() == QColor(THEME.colors.window)
    for state in (RuntimeState.STOPPED, RuntimeState.ERROR):
        header.set_runtime_state(state)
        assert background() == QColor(THEME.colors.primary)

    header.close()
