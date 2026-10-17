# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.mainwindow.workspacebridge."""

from unittest import mock

import pytest

from qutebrowser.qt.core import QModelIndex, QObject, Qt, QUrl, pyqtSignal
from qutebrowser.qt.gui import QIcon, QPixmap

from qutebrowser.mainwindow import tabtree, workspacebridge
from qutebrowser.workspaces.manager import WorkspaceManager


class MockAudio(QObject):

    """Minimal ``AbstractAudio`` stand-in for the per-tab audio signals."""

    muted_changed = pyqtSignal(bool)
    recently_audible_changed = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.muted_calls: list[bool] = []
        self._muted = False

    def is_muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool, override: bool = False) -> None:
        self._muted = muted
        self.muted_calls.append(muted)
        self.muted_changed.emit(muted)


class MockTabData:

    """Minimal ``TabData`` stand-in carrying the pinned flag."""

    def __init__(self, pinned: bool = False) -> None:
        self.pinned = pinned


class MockTab(QObject):

    """Minimal ``AbstractTab`` stand-in exposing the signals the bridge uses."""

    title_changed = pyqtSignal(str)
    url_changed = pyqtSignal(QUrl)
    icon_changed = pyqtSignal(QIcon)
    load_started = pyqtSignal()
    load_finished = pyqtSignal(bool)
    load_status_changed = pyqtSignal(object)
    pinned_changed = pyqtSignal(bool)
    shutting_down = pyqtSignal()

    def __init__(
            self,
            tab_id: int,
            title: str = '',
            url: QUrl | None = None,
            pinned: bool = False,
    ) -> None:
        super().__init__()
        self.tab_id = tab_id
        self._title = title
        self._url = url if url is not None else QUrl()
        self.data = MockTabData(pinned=pinned)
        self.audio = MockAudio()
        self._focused = False

    def title(self) -> str:
        return self._title

    def url(self) -> QUrl:
        return self._url

    def icon(self) -> QIcon:
        return QIcon()

    def set_pinned(self, pinned: bool) -> None:
        self.data.pinned = pinned
        self.pinned_changed.emit(pinned)

    def setFocus(self, reason=None) -> None:  # noqa: N802 - Qt naming
        self._focused = True


class MockWidget(QObject):

    """Stand-in for ``tabwidget.TabWidget``.

    ``tabRemoved`` is a real signal here (the production ``QTabWidget``
    exposes it as a virtual method instead), which lets the tests drive the
    bridge's index-based removal path directly.
    """

    tabRemoved = pyqtSignal(int)  # noqa: N815 - mimics production name
    tabCloseRequested = pyqtSignal(int)  # noqa: N815 - mimics production name

    def __init__(self) -> None:
        super().__init__()
        self._tabs: list[MockTab] = []
        self._current: MockTab | None = None

    def count(self) -> int:
        return len(self._tabs)

    def widget(self, idx: int) -> MockTab | None:
        if 0 <= idx < len(self._tabs):
            return self._tabs[idx]
        return None

    def currentWidget(self) -> MockTab | None:  # noqa: N802 - Qt naming
        return self._current

    def currentIndex(self) -> int:  # noqa: N802 - Qt naming
        if self._current is None:
            return -1
        return self._tabs.index(self._current)

    def insert(self, tab: MockTab, idx: int | None = None) -> int:
        if idx is None:
            idx = len(self._tabs)
        self._tabs.insert(idx, tab)
        if self._current is None:
            self._current = tab
        return idx

    def remove(self, idx: int) -> None:
        if self._current is self._tabs[idx]:
            self._current = None
        del self._tabs[idx]
        self.tabRemoved.emit(idx)

    def indexOf(self, widget: MockTab) -> int:  # noqa: N802 - Qt naming
        if widget in self._tabs:
            return self._tabs.index(widget)
        return -1

    def setCurrentWidget(self, widget: MockTab) -> None:  # noqa: N802 - Qt naming
        self._current = widget


class MockTabbedBrowser(QObject):

    """Minimal ``TabbedBrowser`` stand-in with ``new_tab`` + ``widget``."""

    new_tab = pyqtSignal(object, int)
    current_tab_changed = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.widget = MockWidget()


def _open_tab(
        browser: MockTabbedBrowser,
        tab: MockTab,
        idx: int | None = None,
) -> MockTab:
    """Insert *tab* into the mock widget and emit ``new_tab``."""
    if idx is None:
        idx = browser.widget.count()
    browser.widget.insert(tab, idx)
    browser.new_tab.emit(tab, idx)
    return tab


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


class TestWorkspaceBridge:

    """Tests for :class:`workspacebridge.WorkspaceBridge`."""

    def test_new_tab_adds_to_active_workspace(self, browser, manager, bridge):
        _open_tab(browser, MockTab(1, title='One'))
        workspace = manager.get_active()
        assert workspace is not None
        assert [t.tab_id for t in workspace.tabs] == [1]

    def test_first_new_tab_creates_default_workspace(
            self, browser, manager, bridge):
        assert manager.get_active() is None
        _open_tab(browser, MockTab(7))
        workspace = manager.get_active()
        assert workspace is not None
        assert workspace.name == 'default'
        assert [t.tab_id for t in workspace.tabs] == [7]

    def test_tab_removed_drops_from_manager(self, browser, manager, bridge):
        _open_tab(browser, MockTab(1))
        _open_tab(browser, MockTab(2))
        assert [t.tab_id for t in manager.get_active().tabs] == [1, 2]

        browser.widget.remove(0)
        assert [t.tab_id for t in manager.get_active().tabs] == [2]

    def test_shutting_down_drops_from_manager(
            self, browser, manager, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.shutting_down.emit()
        assert manager.get_active().tabs == []

    def test_title_changed_updates_model(self, browser, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.title_changed.emit('New title')
        assert model._tab_info[1].title == 'New title'

    def test_url_changed_updates_model(self, browser, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.url_changed.emit(QUrl('https://example.com/'))
        assert model._tab_info[1].url == 'https://example.com/'

    def test_loading_flag_flips(self, browser, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        assert model._tab_info[1].status == tabtree.Status.NONE

        tab.load_started.emit()
        assert model._tab_info[1].status & tabtree.Status.LOADING

        tab.load_finished.emit(True)
        assert not (model._tab_info[1].status & tabtree.Status.LOADING)

    def test_recently_audible_sets_audible(self, browser, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.audio.recently_audible_changed.emit(True)
        assert model._tab_info[1].status & tabtree.Status.AUDIBLE

    def test_muted_suppresses_audible(self, browser, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.audio.recently_audible_changed.emit(True)
        tab.audio.muted_changed.emit(True)
        assert not (model._tab_info[1].status & tabtree.Status.AUDIBLE)

    def test_pinned_changed_sets_pinned(self, browser, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.pinned_changed.emit(True)
        assert model._tab_info[1].status & tabtree.Status.PINNED

    def test_tree_rebuilds_after_new_tab(self, browser, manager, qtbot):
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        bridge = workspacebridge.WorkspaceBridge(browser, manager, tree._model)
        assert bridge is not None
        assert tree._model.rowCount(QModelIndex()) == 0

        _open_tab(browser, MockTab(1, title='One'))
        # The manager emitted ``changed`` and the tree rebuilt itself,
        # so the new tab now occupies a row.
        assert tree._model.rowCount(QModelIndex()) == 1

    def test_clear_tab_info_on_removal(self, browser, manager, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.title_changed.emit('Gone')
        assert 1 in model._tab_info
        tab.shutting_down.emit()
        assert 1 not in model._tab_info

    def test_noop_when_manager_none(self, browser, model):
        bridge = workspacebridge.WorkspaceBridge(browser, None, model)
        assert bridge is not None
        _open_tab(browser, MockTab(1))
        assert model.rowCount(QModelIndex()) == 0

    def test_noop_when_model_none(self, browser, manager):
        bridge = workspacebridge.WorkspaceBridge(browser, manager, None)
        assert bridge is not None
        _open_tab(browser, MockTab(1))
        assert manager.get_active() is None

    def test_existing_tabs_synced_at_construction(
            self, browser, manager, model):
        # A tab that exists before the bridge is built must still be picked
        # up by the construction-time sync pass.
        browser.widget.insert(MockTab(42, title='Pre-existing'))
        bridge = workspacebridge.WorkspaceBridge(browser, manager, model)
        assert bridge is not None
        assert [t.tab_id for t in manager.get_active().tabs] == [42]
        assert model._tab_info[42].title == 'Pre-existing'

    def test_current_tab_changed_updates_model_active_id(
            self, browser, manager, model, bridge):
        """``current_tab_changed`` is reflected into the model's ActiveRole."""
        browser.current_tab_changed.emit(MockTab(5))
        assert model.active_tab_id() == 5

    def test_active_tab_change_no_feedback_loop(
            self, browser, manager, model, bridge):
        """A redundant ``current_tab_changed`` is a no-op (no re-emit)."""
        model.set_active_tab_id(1)
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))
        browser.current_tab_changed.emit(MockTab(1))
        assert model.active_tab_id() == 1
        assert changes == []

    def _tree_and_bridge(self, browser, manager, qtbot):
        """Build a tree whose own model backs a bridge wired to it.

        ``WorkspaceBridge`` must operate on the *same* model instance the
        tree renders (matching ``MainWindow.__init__``), otherwise the
        indexes emitted by the tree would point into a different model.
        """
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        model = tree._model
        bridge = workspacebridge.WorkspaceBridge(
            win_id=0,
            tabbed_browser=browser,
            manager=manager,
            model=model,
            tab_tree=tree,
        )
        return tree, model, bridge

    def test_tree_activated_switches_browser(
            self, browser, manager, tab_registry, qtbot):
        """``tree.activated`` drives ``QTabWidget.setCurrentWidget``."""
        tab_a = _open_tab(browser, MockTab(1, title='A'))
        tab_b = _open_tab(browser, MockTab(2, title='B'))
        tab_registry[1] = tab_a
        tab_registry[2] = tab_b
        tree, model, bridge = self._tree_and_bridge(browser, manager, qtbot)
        assert model.rowCount(QModelIndex()) == 2
        browser.widget.setCurrentWidget(tab_a)

        tree.activated.emit(model.index(1, 0))
        assert browser.widget.currentWidget() is tab_b

    def test_tree_close_requested_emits_tab_close_requested(
            self, browser, manager, tab_registry, qtbot):
        """``tree.close_requested`` forwards to ``tabCloseRequested``."""
        tab_a = _open_tab(browser, MockTab(1, title='A'))
        tab_b = _open_tab(browser, MockTab(2, title='B'))
        tab_registry[1] = tab_a
        tab_registry[2] = tab_b
        tree, model, bridge = self._tree_and_bridge(browser, manager, qtbot)
        calls = []
        browser.widget.tabCloseRequested.connect(calls.append)

        tree.close_requested.emit(model.index(1, 0))
        assert calls == [1]

    def test_tree_audio_toggled_flips_mute(
            self, browser, manager, tab_registry, qtbot):
        """``tree.audio_toggled`` flips the tab's mute state."""
        tab = _open_tab(browser, MockTab(1, title='A'))
        tab_registry[1] = tab
        tree, model, bridge = self._tree_and_bridge(browser, manager, qtbot)

        tree.audio_toggled.emit(model.index(0, 0))
        assert tab.audio.is_muted() is True
        assert tab.audio.muted_calls == [True]

        tree.audio_toggled.emit(model.index(0, 0))
        assert tab.audio.is_muted() is False
        assert tab.audio.muted_calls == [True, False]

    def test_tree_pin_toggled_calls_set_pinned(
            self, browser, manager, tab_registry, qtbot):
        """``tree.pin_toggled`` flips the tab's pinned state."""
        tab = _open_tab(browser, MockTab(1, title='A'))
        tab_registry[1] = tab
        tree, model, bridge = self._tree_and_bridge(browser, manager, qtbot)
        assert tab.data.pinned is False

        tree.pin_toggled.emit(model.index(0, 0))
        assert tab.data.pinned is True
        assert model._tab_info[1].status & tabtree.Status.PINNED

        tree.pin_toggled.emit(model.index(0, 0))
        assert tab.data.pinned is False
        assert not (model._tab_info[1].status & tabtree.Status.PINNED)

    def test_pinned_status_propagates_into_model(
            self, browser, model, bridge):
        """A tab's ``pinned_changed`` signal updates the model status."""
        tab = _open_tab(browser, MockTab(1))
        tab.pinned_changed.emit(True)
        assert model._tab_info[1].status & tabtree.Status.PINNED

    def test_icon_changed_writes_favicon_to_workspace_tab(
            self, browser, manager, bridge):
        tab = _open_tab(browser, MockTab(1))
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.red)

        tab.icon_changed.emit(QIcon(pixmap))

        workspace_tab = manager.get_active().tabs[0]
        assert workspace_tab.favicon
        assert workspace_tab.fetch_attempts == 1
        assert workspace_tab.last_fetched is not None

    def test_title_changed_persists_into_workspace_tab(
            self, browser, manager, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.title_changed.emit('Persisted title')
        assert manager.get_active().tabs[0].title == 'Persisted title'

    def test_url_changed_persists_source_into_workspace_tab(
            self, browser, manager, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.url_changed.emit(QUrl('https://Example.COM/path'))
        workspace_tab = manager.get_active().tabs[0]
        assert workspace_tab.url == 'https://example.com/path'
        assert workspace_tab.source == 'example.com'

    def test_user_edited_title_blocks_overwrite(
            self, browser, manager, bridge):
        tab = _open_tab(browser, MockTab(1, title='Original'))
        workspace_tab = manager.get_active().tabs[0]
        workspace_tab.user_edited['title'] = True

        tab.title_changed.emit('Auto title')

        assert workspace_tab.title == 'Original'
