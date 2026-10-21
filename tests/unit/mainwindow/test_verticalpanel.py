# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the vertical workspace panel (Phase 2)."""

from types import SimpleNamespace

import pytest

from qutebrowser.qt.core import Qt
from qutebrowser.qt.widgets import QApplication, QWidget

from qutebrowser.mainwindow import mainwindow, verticalpanel
from qutebrowser.utils import usertypes


class TestVerticalPanel:

    """Tests for :class:`verticalpanel.VerticalPanel`."""

    @pytest.fixture
    def modeman_mock(self, monkeypatch):
        """Stub the modeman.enter / modeman.leave functions."""
        calls = []

        def fake_enter(win_id, mode, reason=None, only_if_normal=False):
            calls.append(('enter', win_id, mode, reason))

        def fake_leave(win_id, mode, reason=None, maybe=False):
            calls.append(('leave', win_id, mode, reason))

        monkeypatch.setattr(verticalpanel.modeman, 'enter', fake_enter)
        monkeypatch.setattr(verticalpanel.modeman, 'leave', fake_leave)
        return calls

    @pytest.fixture
    def panel(self, qtbot, config_stub, modeman_mock):
        """Create a VerticalPanel registered with qtbot for cleanup."""
        config_stub.val.verticalpanel.width = 220
        config_stub.val.verticalpanel.visible = True
        panel = verticalpanel.VerticalPanel(win_id=0)
        qtbot.add_widget(panel)
        # The panel is focusable and ``qtbot.add_widget`` shows it,
        # which queues a real focus-in event in the Qt event loop.
        # That event fires AFTER this fixture body returns, so a
        # naive reset here would be undone by the queued event before
        # the test body runs (the test would see ``_in_mode = True``
        # and the manual ``_on_focus_changed`` call would skip the
        # enter branch). Flush the event loop first so the queued
        # focus event fires NOW; then reset state and the mock so
        # each test starts from a clean focus baseline.
        QApplication.processEvents()
        panel._in_mode = False
        panel._last_focused = None
        modeman_mock.clear()
        return panel

    def test_focus_policy_strong_focus(self, panel):
        """Test that the empty panel is focusable."""
        assert panel.focusPolicy() == Qt.FocusPolicy.StrongFocus

    def test_visible_by_default(self, panel):
        """Test that the panel is not hidden by default."""
        assert not panel.isHidden()

    def test_hidden_when_config_says_so(self, qtbot, config_stub, modeman_mock):
        """Test that verticalpanel.visible=false hides the panel at init."""
        config_stub.val.verticalpanel.width = 220
        config_stub.val.verticalpanel.visible = False
        panel = verticalpanel.VerticalPanel(win_id=0)
        qtbot.add_widget(panel)
        try:
            assert panel.isHidden()
        finally:
            panel.show()  # avoid warnings on teardown

    def test_applies_minimum_width(self, panel):
        """Test that the panel's minimum width tracks verticalpanel.width."""
        assert panel.minimumWidth() == 220

    def test_integer_width_is_pixels(self, panel):
        """Regression guard: an integer width stays an absolute pixel value."""
        assert panel.pixel_width() == 220
        assert panel.minimumWidth() == 220
        assert panel.maximumWidth() == 880

    def test_percentage_width_does_not_raise(
            self, qtbot, config_stub, modeman_mock):
        """Regression: percentages must be resolved before reaching Qt.

        Previously the raw ``'25%'`` string was passed to
        ``setMinimumWidth``/``setMaximumWidth``, which raised a TypeError
        and prevented startup.
        """
        config_stub.val.verticalpanel.width = '25%'
        config_stub.val.verticalpanel.visible = True
        panel = verticalpanel.VerticalPanel(win_id=0)
        qtbot.add_widget(panel)
        assert isinstance(panel.minimumWidth(), int)

    def test_percentage_width_resolves_to_pixels(self, panel, config_stub):
        """A percentage resolves against the window width (like tabs.width)."""
        # The integer fixture width caps the panel at 880px, so lift the
        # cap before resizing to a known reference width.
        panel.setMaximumWidth(16777215)
        panel.resize(1000, 600)
        QApplication.processEvents()
        assert panel.width() == 1000

        config_stub.val.verticalpanel.width = '25%'
        assert panel.pixel_width() == 250
        assert panel.minimumWidth() == 250
        assert panel.maximumWidth() == 1000

    def test_width_change_at_runtime(self, panel, config_stub):
        """A runtime width change updates the panel without a restart."""
        received = []
        panel.width_changed.connect(received.append)

        config_stub.val.verticalpanel.width = 100
        assert panel.minimumWidth() == 100
        assert panel.maximumWidth() == 400
        assert received[-1] == 100

        config_stub.val.verticalpanel.width = '50%'
        assert panel.minimumWidth() == panel.pixel_width()
        assert received[-1] == panel.pixel_width()

    def test_width_change_resizes_splitter(self, panel, config_stub):
        """A runtime change reaches MainWindow's splitter resize handler."""
        sizes = []
        fake_window = SimpleNamespace(
            _central_splitter=SimpleNamespace(setSizes=sizes.append),
            width=lambda: 1000,
        )
        panel.width_changed.connect(
            lambda width: mainwindow.MainWindow._on_panel_width_changed(
                fake_window, width))

        config_stub.val.verticalpanel.width = 120
        assert sizes[-1] == [120, 880]

    def test_toggle_hides(self, panel):
        """Test that toggle() hides a visible panel."""
        assert not panel.isHidden()
        panel.toggle()
        assert panel.isHidden()

    def test_toggle_shows(self, qtbot, config_stub, modeman_mock):
        """Test that toggle() shows a hidden panel."""
        config_stub.val.verticalpanel.width = 220
        config_stub.val.verticalpanel.visible = False
        panel = verticalpanel.VerticalPanel(win_id=0)
        qtbot.add_widget(panel)
        try:
            assert panel.isHidden()
            panel.toggle()
            assert not panel.isHidden()
        finally:
            panel.hide()

    def test_focus_in_enters_workspace_panel(self, panel, modeman_mock):
        """Test that focusing the panel enters KeyMode.workspace_panel."""
        panel._on_focus_changed(None, panel)
        assert modeman_mock == [
            ('enter', 0, usertypes.KeyMode.workspace_panel,
             'vertical-panel focus-in'),
        ]

    def test_focus_in_descendant_enters_workspace_panel(
            self, panel, modeman_mock):
        """Test that focusing a child of the panel enters the mode."""
        child = QWidget(panel)
        panel._on_focus_changed(None, child)
        assert modeman_mock == [
            ('enter', 0, usertypes.KeyMode.workspace_panel,
             'vertical-panel focus-in'),
        ]
        # And the child is remembered for the next toggle show.
        assert panel._last_focused is child

    def test_focus_out_leaves_workspace_panel(self, panel, modeman_mock):
        """Test that focus moving away from the panel leaves the mode."""
        # First enter the mode by focusing the panel.
        panel._on_focus_changed(None, panel)
        modeman_mock.clear()
        # Then simulate focus moving to some other widget.
        other = QWidget()
        try:
            panel._on_focus_changed(panel, other)
            assert modeman_mock == [
                ('leave', 0, usertypes.KeyMode.workspace_panel,
                 'vertical-panel focus-out'),
            ]
        finally:
            other.deleteLater()

    def test_focus_within_panel_does_not_emit_leave(
            self, panel, modeman_mock):
        """Test that focus moving within the panel does not emit events."""
        child_a = QWidget(panel)
        child_b = QWidget(panel)
        try:
            panel._on_focus_changed(None, child_a)
            modeman_mock.clear()
            panel._on_focus_changed(child_a, child_b)
            # No leave event was emitted because focus stayed inside
            # the panel subtree.
            assert modeman_mock == []
        finally:
            child_a.deleteLater()
            child_b.deleteLater()

    def test_is_panel_descendant_self(self, panel):
        """Test that _is_panel_descendant handles the panel itself."""
        assert panel._is_panel_descendant(panel) is True

    def test_is_panel_descendant_child(self, panel):
        """Test that _is_panel_descendant finds a direct child."""
        child = QWidget(panel)
        try:
            assert panel._is_panel_descendant(child) is True
        finally:
            child.deleteLater()

    def test_is_panel_descendant_grandchild(self, panel):
        """Test that _is_panel_descendant finds a transitive descendant."""
        child = QWidget(panel)
        grandchild = QWidget(child)
        try:
            assert panel._is_panel_descendant(grandchild) is True
        finally:
            child.deleteLater()

    def test_is_panel_descendant_sibling(self, qtbot, panel, modeman_mock):
        """Test that _is_panel_descendant rejects widgets outside the panel."""
        sibling = QWidget()
        qtbot.add_widget(sibling)
        try:
            assert panel._is_panel_descendant(sibling) is False
        finally:
            sibling.deleteLater()

    def test_is_panel_descendant_none(self, panel):
        """Test that _is_panel_descendant handles None gracefully."""
        assert panel._is_panel_descendant(None) is False

    def test_filter_row_toggles_parser_passthrough(
            self, panel, monkeypatch):
        """Opening the filter row puts the panel parser in passthrough.

        This is what lets the QLineEdit receive its text and the Escape
        key while leaving ``KeyMode.workspace_panel`` active.
        """
        calls = []

        class FakeParser:

            def set_filter_active(self, active):
                calls.append(active)

        mode_manager = SimpleNamespace(
            parsers={usertypes.KeyMode.workspace_panel: FakeParser()})
        monkeypatch.setattr(
            verticalpanel.modeman, 'instance',
            lambda win_id: mode_manager)

        panel.open_filter()
        assert calls == [True]
        assert panel.filter_row.isVisible()

        panel.filter_row.close_filter()
        assert calls == [True, False]
        assert panel.filter_row.isHidden()
