# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the vertical workspace panel (Phase 2)."""

import pytest

from qutebrowser.qt.core import Qt
from qutebrowser.qt.widgets import QApplication, QWidget

from qutebrowser.mainwindow import verticalpanel
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
