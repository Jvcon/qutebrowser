# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the vertical panel's PIN section (issue #34)."""

import pytest

from qutebrowser.qt.core import QObject, Qt, pyqtSignal

from qutebrowser.mainwindow import pinpanel
from qutebrowser.workspaces.model import PinnedTab


class MockPinManager(QObject):

    """Minimal manager stand-in exposing only the pinned-tab API."""

    pinned_changed = pyqtSignal()

    def __init__(self, pinned=None):
        super().__init__()
        self._pinned = list(pinned or [])

    def list_pinned(self):
        return list(self._pinned)

    def set_pinned(self, pinned):
        self._pinned = list(pinned)
        self.pinned_changed.emit()


@pytest.fixture
def panel(qtbot, config_stub):
    config_stub.val.pinned_tabs.style = 'line'
    manager = MockPinManager()
    widget = pinpanel.PINPanel(win_id=0, manager=manager)
    qtbot.add_widget(widget)
    return widget, manager


class TestPinPanelEmptyState:

    def test_empty_list_collapses_section(self, panel):
        widget, _manager = panel

        assert widget.isHidden()
        assert widget.maximumHeight() == 0

    def test_pinned_entry_expands_section(self, panel):
        widget, manager = panel

        manager.set_pinned([
            PinnedTab(tab_id=1, title='One', url='https://example.com/')])

        assert not widget.isHidden()
        assert widget.maximumHeight() > 0


class TestPinPanelDrag:

    def test_model_exposes_drag_payload(self, panel):
        widget, manager = panel
        manager.set_pinned([
            PinnedTab(tab_id=7, title='Seven', url='https://example.com/')])
        model = widget._model
        index = model.index(0, 0)

        assert Qt.ItemFlag.ItemIsDragEnabled & model.flags(index)
        mime = model.mimeData([index])
        assert mime.hasFormat(pinpanel.PINNED_TAB_MIME)
        assert bytes(mime.data(pinpanel.PINNED_TAB_MIME)) == b'7'
        assert model.supportedDragActions() == Qt.DropAction.CopyAction
