# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Issue #52: runtime reconciliation of a moved tab entry with the bridge.

A tab entry moved by ``WorkspaceManager.move_tab_to_workspace`` keeps its
runtime tab id, but the live browser tab object stays where it was.  These
tests check that the bridge keeps the TabTree, the manager entries and the
live tabs consistent across a move followed by a workspace switch.

Fixtures and mock widgets are reused from ``test_workspacebridge``.
"""

from unittest import mock

import pytest

from qutebrowser.qt.core import QObject, Qt, QUrl
from qutebrowser.qt.gui import QIcon, QPixmap

from qutebrowser.mainwindow import tabtree, workspacebridge
from qutebrowser.utils.qtutils import weak_slot
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import WorkspaceTab

# Same mock tab / browser doubles as test_workspacebridge.py.
from tests.unit.mainwindow.test_workspacebridge import (
    MockTab, MockTabbedBrowser)


@pytest.fixture
def browser(qapp) -> MockTabbedBrowser:
    return MockTabbedBrowser()


@pytest.fixture
def manager(qapp) -> WorkspaceManager:
    """A real manager backed by mock persistence (no file I/O)."""
    persistence = mock.Mock()
    persistence.profile_name = 'default'
    pinned_persistence = mock.Mock()
    return WorkspaceManager(persistence, pinned_persistence)


@pytest.fixture
def model(manager: WorkspaceManager) -> tabtree.TabTreeModel:
    return tabtree.TabTreeModel(manager)


@pytest.fixture
def bridge(
        browser: MockTabbedBrowser,
        manager: WorkspaceManager,
        model: tabtree.TabTreeModel,
) -> workspacebridge.WorkspaceBridge:
    return workspacebridge.WorkspaceBridge(browser, manager, model)


class _ModelRebuilder(QObject):

    """Mirrors the TabTree view: rebuild the model on every manager change."""

    def __init__(
            self,
            manager: WorkspaceManager,
            model: tabtree.TabTreeModel,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._model = model

    def rebuild(self) -> None:
        self._model.set_workspace(self._manager.get_active())


@pytest.fixture
def reconciled(manager, model, bridge):
    """The bridge plus a model that follows manager ``changed`` like the view."""
    rebuilder = _ModelRebuilder(manager, model)
    manager.changed.connect(weak_slot(rebuilder.rebuild, accepts=0))
    model.set_workspace(manager.get_active())
    return rebuilder


def _attach_live(browser, bridge, tab_id, *, title, url):
    """Create a live tab, insert it into the browser and register it."""
    tab = MockTab(tab_id, title=title, url=QUrl(url))
    idx = browser.widget.insert(tab)
    bridge._register_tab(tab, idx)
    return tab


def _depth(model: tabtree.TabTreeModel, index) -> int:
    depth = 0
    while index.isValid():
        depth += 1
        index = index.parent()
    return depth


def _icon() -> QIcon:
    pixmap = QPixmap(8, 8)
    pixmap.fill(Qt.GlobalColor.red)
    return QIcon(pixmap)


class TestOpenEntryMove:

    """An OPEN entry moved to another workspace, with and without a switch."""

    @staticmethod
    def _setup(manager, browser, bridge):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/one',
            open_status='OPEN'))
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=2, title='two', url='https://src.example/two',
            open_status='OPEN', parent_id=1))
        tab1 = _attach_live(
            browser, bridge, 1, title='one', url='https://src.example/one')
        tab2 = _attach_live(
            browser, bridge, 2, title='two', url='https://src.example/two')
        return src, dst, tab1, tab2

    def test_move_without_switch_drops_entry_from_source_tree(
            self, browser, manager, model, bridge, reconciled):
        src, dst, tab1, _tab2 = self._setup(manager, browser, bridge)
        assert model.index_for_tab_id(1).isValid()

        manager.move_tab_to_workspace(src.id, 1, dst.id)

        assert manager.find_tab_by_id(src.id, 1) is None
        assert manager.find_tab_by_id(dst.id, 1) is not None
        assert not model.index_for_tab_id(1).isValid()
        # The live tab is untouched: same object, same id, still in the
        # browser and still bridged.
        assert browser.widget.indexOf(tab1) >= 0
        assert tab1.tab_id == 1
        assert 1 in bridge._attached

    def test_live_tab_stays_interactive_after_move(
            self, browser, manager, model, bridge, reconciled):
        src, dst, tab1, _tab2 = self._setup(manager, browser, bridge)
        manager.move_tab_to_workspace(src.id, 1, dst.id)

        # Signals from the still-live tab must be delivered without error and
        # the browser must still be able to focus it.
        tab1.load_started.emit()
        tab1.load_finished.emit(True)
        browser.widget.setCurrentWidget(tab1)

        assert browser.widget.currentWidget() is tab1

    def test_switch_shows_entry_matching_live_state(
            self, browser, manager, model, bridge, reconciled):
        src, dst, tab1, _tab2 = self._setup(manager, browser, bridge)
        tab1.icon_changed.emit(_icon())
        manager.move_tab_to_workspace(src.id, 1, dst.id)

        manager.set_active(dst.id)

        idx = model.index_for_tab_id(1)
        assert idx.isValid()
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == 'one'
        assert model.data(idx, tabtree.UrlRole) == 'https://src.example/one'
        entry = manager.find_tab_by_id(dst.id, 1)
        assert entry.title == 'one'
        assert entry.url == 'https://src.example/one'
        assert entry.favicon
        assert browser.widget.currentWidget() is tab1
        assert model.active_tab_id() == 1

    def test_live_state_changed_after_move_reaches_target_entry(
            self, browser, manager, model, bridge, reconciled):
        """Metadata changes of a moved live tab must follow it to the target.

        While the user is still on the source workspace the live tab reports
        a new title / URL / favicon.  The target entry has to end up with
        them, otherwise the entry shown after the switch is stale.
        """
        src, dst, tab1, _tab2 = self._setup(manager, browser, bridge)
        manager.move_tab_to_workspace(src.id, 1, dst.id)

        tab1.title_changed.emit('one renamed')
        tab1.url_changed.emit(QUrl('https://src.example/renamed'))
        tab1.icon_changed.emit(_icon())
        manager.set_active(dst.id)

        entry = manager.find_tab_by_id(dst.id, 1)
        assert entry.title == 'one renamed'
        assert entry.url == 'https://src.example/renamed'
        assert entry.favicon
        idx = model.index_for_tab_id(1)
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == 'one renamed'

    def test_closing_moved_live_tab_suspends_target_entry(
            self, browser, manager, model, bridge, reconciled):
        """Closing a moved live tab must suspend the entry that owns it.

        Otherwise the target workspace keeps an OPEN entry with no live tab
        behind it, and later suspend / close actions hit nothing.
        """
        src, dst, tab1, _tab2 = self._setup(manager, browser, bridge)
        manager.move_tab_to_workspace(src.id, 1, dst.id)

        tab1.shutting_down.emit()

        entry = manager.find_tab_by_id(dst.id, 1)
        assert entry is not None
        assert entry.open_status == 'SUSPENDED'
        assert manager.find_tab_by_id(src.id, 1) is None


class TestSuspendedEntryMove:

    """A SUSPENDED entry moved must not resume or destroy anything."""

    def test_move_does_not_resume_or_destroy(
            self, browser, manager, model, bridge, reconciled):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=9, title='gone', url='https://gone.example/',
            open_status='SUSPENDED'))
        count_before = browser.widget.count()

        manager.move_tab_to_workspace(src.id, 9, dst.id)

        assert browser.widget.count() == count_before
        assert 9 not in bridge._attached
        assert manager.find_tab_by_id(src.id, 9) is None
        entry = manager.find_tab_by_id(dst.id, 9)
        assert entry is not None
        assert entry.open_status == 'SUSPENDED'
        assert entry.url == 'https://gone.example/'

    def test_switch_keeps_suspended_entry_suspended(
            self, browser, manager, model, bridge, reconciled):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=9, title='gone', url='https://gone.example/',
            open_status='SUSPENDED'))
        manager.move_tab_to_workspace(src.id, 9, dst.id)
        count_before = browser.widget.count()

        manager.set_active(dst.id)

        assert browser.widget.count() == count_before
        assert manager.find_tab_by_id(dst.id, 9).open_status == 'SUSPENDED'
        idx = model.index_for_tab_id(9)
        assert idx.isValid()
        assert model.data(idx, tabtree.OpenStatusRole) == 'SUSPENDED'
        assert manager.find_tab_by_id(dst.id, 9).title == 'gone'


class TestLastActiveOnMove:

    """Moving the source's last-active tab must not leave a dangling id."""

    def test_moving_last_active_root_clears_source_last_active(
            self, browser, manager, model, bridge, reconciled):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/one',
            open_status='OPEN'))
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=2, title='two', url='https://src.example/two',
            open_status='OPEN', parent_id=1))
        tab1 = _attach_live(
            browser, bridge, 1, title='one', url='https://src.example/one')
        _attach_live(
            browser, bridge, 2, title='two', url='https://src.example/two')
        bridge._on_current_tab_changed(tab1)
        assert manager.tab_id_for_workspace(src.id) == 1

        manager.move_tab_to_workspace(src.id, 1, dst.id)

        assert manager.tab_id_for_workspace(src.id) is None
        # The promoted child is the only one left; the source must not point
        # at the moved entry anymore.
        assert manager.find_tab_by_id(src.id, 2).parent_id is None

    def test_moving_last_active_keeps_old_parent_as_fallback(
            self, browser, manager, model, bridge, reconciled):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=5, title='parent', url='https://src.example/p',
            open_status='OPEN'))
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/one',
            open_status='OPEN', parent_id=5))
        _attach_live(browser, bridge, 5, title='parent',
                     url='https://src.example/p')
        tab1 = _attach_live(
            browser, bridge, 1, title='one', url='https://src.example/one')
        bridge._on_current_tab_changed(tab1)

        manager.move_tab_to_workspace(src.id, 1, dst.id)

        assert manager.tab_id_for_workspace(src.id) == 5
        assert manager.find_tab_by_id(src.id, 5) is not None

    def test_switch_back_to_source_focuses_surviving_tab(
            self, browser, manager, model, bridge, reconciled):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/one',
            open_status='OPEN'))
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=2, title='two', url='https://src.example/two',
            open_status='OPEN', parent_id=1))
        tab1 = _attach_live(
            browser, bridge, 1, title='one', url='https://src.example/one')
        tab2 = _attach_live(
            browser, bridge, 2, title='two', url='https://src.example/two')
        bridge._on_current_tab_changed(tab1)
        manager.move_tab_to_workspace(src.id, 1, dst.id)

        manager.set_active(dst.id)
        manager.set_active(src.id)

        # No last-active is recorded for src any more, so the first live OPEN
        # entry (the promoted child) is focused -- never the moved tab.
        assert browser.widget.currentWidget() is tab2
        assert model.active_tab_id() == 2


class TestSwitchAfterMove:

    """Moving an entry and then switching to the target workspace."""

    def test_last_active_points_at_moved_entry_after_switch(
            self, browser, manager, model, bridge, reconciled):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/one',
            open_status='OPEN'))
        tab1 = _attach_live(
            browser, bridge, 1, title='one', url='https://src.example/one')
        bridge._on_current_tab_changed(tab1)

        manager.move_tab_to_workspace(src.id, 1, dst.id)
        assert manager.tab_id_for_workspace(dst.id) == 1
        manager.set_active(dst.id)

        assert manager.tab_id_for_workspace(dst.id) == 1
        assert browser.widget.currentWidget() is tab1
        assert model.active_tab_id() == 1

    def test_branch_move_keeps_parent_links_and_depth(
            self, browser, manager, model, bridge, reconciled):
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/1',
            open_status='OPEN'))
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=2, title='two', url='https://src.example/2',
            open_status='OPEN', parent_id=1))
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=4, title='four', url='https://src.example/4',
            open_status='OPEN', parent_id=2))
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=3, title='three', url='https://src.example/3',
            open_status='OPEN'))
        live = {
            tab_id: _attach_live(browser, bridge, tab_id, title=f't{tab_id}',
                                 url=f'https://src.example/{tab_id}')
            for tab_id in (1, 2, 4, 3)
        }
        tab4 = live[4]
        bridge._on_current_tab_changed(tab4)
        assert manager.tab_id_for_workspace(src.id) == 4

        manager.move_tab_to_workspace(src.id, 1, dst.id, branch=True)
        manager.set_active(dst.id)

        assert manager.find_tab_by_id(dst.id, 1).parent_id is None
        assert manager.find_tab_by_id(dst.id, 2).parent_id == 1
        assert manager.find_tab_by_id(dst.id, 4).parent_id == 2
        # The unrelated root stays behind in the source.
        assert manager.find_tab_by_id(src.id, 3) is not None
        assert manager.find_tab_by_id(src.id, 1) is None

        assert _depth(model, model.index_for_tab_id(1)) == 1
        assert _depth(model, model.index_for_tab_id(2)) == 2
        assert _depth(model, model.index_for_tab_id(4)) == 3
        assert model.index_for_tab_id(4).parent() == model.index_for_tab_id(2)

        # The moved descendant was the source's last-active tab; it is the
        # target's last-active now and is focused on the switch.
        assert manager.tab_id_for_workspace(dst.id) == 4
        assert browser.widget.currentWidget() is tab4
        assert model.active_tab_id() == 4

    def test_refocus_of_moved_tab_in_source_does_not_dangle(
            self, browser, manager, model, bridge, reconciled):
        """A moved tab focused again while still shown must not re-enter src.

        The live tab is still in the source window, so the browser can focus
        it.  That focus is recorded for the workspace which owns the entry,
        never as a dangling last-active of the source.
        """
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/one',
            open_status='OPEN'))
        tab1 = _attach_live(
            browser, bridge, 1, title='one', url='https://src.example/one')
        bridge._on_current_tab_changed(tab1)
        manager.move_tab_to_workspace(src.id, 1, dst.id)

        bridge._on_current_tab_changed(tab1)

        assert manager.tab_id_for_workspace(src.id) is None
        assert manager.tab_id_for_workspace(dst.id) == 1


class TestAutoLocateAfterMove:

    """VP-D5 must not look for a row of the wrong workspace."""

    def test_refocus_of_moved_tab_does_not_request_group_focus(
            self, browser, manager, qtbot):
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        bridge = workspacebridge.WorkspaceBridge(
            win_id=0,
            tabbed_browser=browser,
            manager=manager,
            model=tree._model,
            tab_tree=tree,
        )
        src = manager.add_workspace('src')
        dst = manager.add_workspace('dst')
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=1, title='one', url='https://src.example/one',
            open_status='OPEN'))
        # A different entry in the source with the same URL: the URL fallback
        # must not pick it for the moved tab.
        manager.add_tab(src.id, WorkspaceTab(
            tab_id=7, title='again', url='https://src.example/one',
            open_status='SUSPENDED'))
        tab1 = _attach_live(
            browser, bridge, 1, title='one', url='https://src.example/one')
        emitted = []

        def record(gid, tid):
            emitted.append((gid, tid))

        tree.group_focus_requested.connect(record)
        manager.move_tab_to_workspace(src.id, 1, dst.id)

        bridge._on_current_tab_changed(tab1)

        assert emitted == []
