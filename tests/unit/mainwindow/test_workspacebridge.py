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
from qutebrowser.workspaces.model import WorkspaceTab


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

    def test_first_register_creates_open_entry(
            self, browser, manager, bridge):
        _open_tab(browser, MockTab(1))
        workspace = manager.get_active()
        assert workspace is not None
        assert workspace.tabs[0].open_status == 'OPEN'
        assert manager.open_tabs(workspace.id) == workspace.tabs

    def test_reregister_keeps_entry_open(self, browser, manager, bridge):
        tab = _open_tab(browser, MockTab(1))
        manager.set_open_status(manager.get_active().id, 1, 'SUSPENDED')

        bridge._register_tab(tab, 0)

        assert manager.get_active().tabs[0].open_status == 'OPEN'

    def test_tab_removed_suspends_entry(self, browser, manager, bridge):
        _open_tab(browser, MockTab(1))
        _open_tab(browser, MockTab(2))
        assert [t.tab_id for t in manager.get_active().tabs] == [1, 2]

        browser.widget.remove(0)
        tabs = manager.get_active().tabs
        assert [t.tab_id for t in tabs] == [1, 2]
        assert tabs[0].open_status == 'SUSPENDED'
        assert tabs[1].open_status == 'OPEN'

    def test_tab_close_request_suspends_entry_and_keeps_children(
            self, browser, manager, bridge):
        """VP-D3: closing through ``tabCloseRequested`` suspends the entry.

        D1 keeps the entry in the workspace instead of deleting it, so a
        child tab remains linked to its now-suspended parent; the link is
        only cleared when the entry is explicitly removed.
        """
        _open_tab(browser, MockTab(1, title='Parent'))
        _open_tab(browser, MockTab(2, title='Child'))
        workspace = manager.get_active()
        workspace.tabs[1].parent_id = 1

        def close(idx):
            browser.widget.remove(idx)

        browser.widget.tabCloseRequested.connect(close)

        browser.widget.tabCloseRequested.emit(0)

        tabs = workspace.tabs
        assert [t.tab_id for t in tabs] == [1, 2]
        assert tabs[0].open_status == 'SUSPENDED'
        assert tabs[1].open_status == 'OPEN'
        assert tabs[1].parent_id == 1

    def test_shutting_down_suspends_entry(self, browser, manager, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.shutting_down.emit()
        tabs = manager.get_active().tabs
        assert [t.tab_id for t in tabs] == [1]
        assert tabs[0].open_status == 'SUSPENDED'

    def test_shutting_down_pushes_suspended_status(
            self, browser, manager, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.title_changed.emit('Gone')

        tab.shutting_down.emit()

        assert model._tab_info[1].status & tabtree.Status.SUSPENDED
        # VP-D1: the retained entry keeps its last known display data so the
        # suspended row is still readable instead of rendering blank.
        assert model._tab_info[1].title == 'Gone'
        assert model.active_tab_id() is None

    def test_reused_url_entry_is_rebound_to_new_tab(
            self, browser, manager, model, bridge):
        """VP-D1: a new tab id matching a retained URL reuses its entry."""
        first = _open_tab(browser, MockTab(1, url=QUrl('https://example.com/')))
        first.shutting_down.emit()
        assert [t.tab_id for t in manager.get_active().tabs] == [1]

        _open_tab(browser, MockTab(5, url=QUrl('https://example.com/')))

        tabs = manager.get_active().tabs
        assert [t.tab_id for t in tabs] == [5]
        assert tabs[0].open_status == 'OPEN'
        assert model._tab_info[5].url == 'https://example.com/'

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

    def test_clear_tab_info_on_suspend(self, browser, manager, model, bridge):
        tab = _open_tab(browser, MockTab(1))
        tab.title_changed.emit('Gone')
        assert model._tab_info[1].title == 'Gone'
        tab.shutting_down.emit()
        # VP-D1: the entry is retained, so the row keeps its last known
        # metadata while the SUSPENDED status bit paints the "zZ" badge.
        assert model._tab_info[1].title == 'Gone'
        assert model._tab_info[1].status & tabtree.Status.SUSPENDED

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


class TestAutoLocate:

    """VP-D5: tab switches emit ``group_focus_requested``."""

    def _tree_and_bridge(self, browser, manager, qtbot):
        """Build a tree and a bridge wired to the same model."""
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        bridge = workspacebridge.WorkspaceBridge(
            win_id=0,
            tabbed_browser=browser,
            manager=manager,
            model=tree._model,
            tab_tree=tree,
        )
        return tree, bridge

    @staticmethod
    def _collect(tree):
        emitted = []
        tree.group_focus_requested.connect(
            lambda gid, tid: emitted.append((gid, tid)))
        return emitted

    def test_known_tab_emits_group_focus(self, browser, manager, qtbot):
        """A switch to a grouped tab emits its group + tab id."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2, url=QUrl('https://b.example/')))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        workspace = manager.get_active()
        group = manager.create_group(workspace.id, 'g')
        manager.move_tab_to_group(workspace.id, 2, group.group_id)
        emitted = self._collect(tree)

        browser.current_tab_changed.emit(tab_b)

        assert emitted == [(group.group_id, 2)]
        assert tree.model().active_group_id() == group.group_id

    def test_standalone_tab_emits_none_group(self, browser, manager, qtbot):
        """A standalone tab emits ``None`` as the group id."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2, url=QUrl('https://b.example/')))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        emitted = self._collect(tree)

        browser.current_tab_changed.emit(tab_b)

        assert emitted == [(None, 2)]

    def test_url_fallback_emits(
            self, browser, manager, qtbot, monkeypatch):
        """When the id lookup misses, the URL match is used instead."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2, url=QUrl('https://b.example/')))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        workspace = manager.get_active()
        entry = manager.find_tab_by_id(workspace.id, 2)
        assert entry is not None
        monkeypatch.setattr(manager, 'find_tab_by_id', lambda *a, **k: None)
        monkeypatch.setattr(
            manager, 'find_tab_by_url', lambda wid, url: entry)
        emitted = self._collect(tree)

        browser.current_tab_changed.emit(tab_b)

        assert emitted == [(entry.group_id, entry.tab_id)]

    def test_internal_page_skipped(self, browser, manager, qtbot):
        """A switch to a ``qute://`` page does not emit."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2, url=QUrl('qute://settings')))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        emitted = self._collect(tree)

        browser.current_tab_changed.emit(tab_b)

        assert emitted == []

    def test_invalid_url_skipped_via_ensure_valid(
            self, browser, manager, qtbot, monkeypatch):
        """``_auto_locate`` validates the URL and bails on an invalid one."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2, url=QUrl('https://b.example/')))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        emitted = self._collect(tree)

        def _raise(_qurl):
            raise workspacebridge.qtutils.QtValueError(QUrl())

        monkeypatch.setattr(workspacebridge.qtutils, 'ensure_valid', _raise)

        browser.current_tab_changed.emit(tab_b)

        assert emitted == []

    def test_empty_url_skipped(self, browser, manager, qtbot):
        """A switch to a tab without a URL does not emit."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        emitted = self._collect(tree)

        browser.current_tab_changed.emit(tab_b)

        assert emitted == []

    def test_shutting_down_tab_skipped(self, browser, manager, qtbot):
        """A tab which is shutting down does not trigger auto-locate."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2, url=QUrl('https://b.example/')))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        emitted = self._collect(tree)

        tab_b.shutting_down.emit()
        browser.current_tab_changed.emit(tab_b)

        assert emitted == []

    def test_unknown_tab_does_not_emit(self, browser, manager, qtbot):
        """A switch to a tab with no workspace entry does not emit."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tree, bridge = self._tree_and_bridge(browser, manager, qtbot)
        emitted = self._collect(tree)

        # A brand-new tab id never registered with the bridge.
        browser.current_tab_changed.emit(MockTab(99, url=QUrl('https://z/')))

        assert emitted == []

    def test_no_tab_tree_is_inert(self, browser, manager, model, bridge):
        """A bridge built without a TabTree tolerates tab switches."""
        _open_tab(browser, MockTab(1, url=QUrl('https://a.example/')))
        tab_b = _open_tab(browser, MockTab(2, url=QUrl('https://b.example/')))
        browser.current_tab_changed.emit(tab_b)
        assert model.active_tab_id() == 2


class TestMaybeSnapshotWindow:

    """VP-P4: snapshot OPEN workspace tabs once a window is empty."""

    @staticmethod
    def _install_objreg(monkeypatch, browser, registry):
        def fake_get(name, **_kwargs):
            if name == 'tabbed-browser':
                return browser
            if name == 'tab-registry':
                return registry
            raise KeyError(name)

        monkeypatch.setattr(workspacebridge.objreg, 'get', fake_get)

    @staticmethod
    def _workspace_with_open_tab(manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(
            tab_id=1,
            url='https://a.example/',
            title='From entry',
            open_status='OPEN',
        ))
        return workspace

    def test_nonempty_window_skips(
            self, browser, manager, bridge, monkeypatch, config_stub):
        config_stub.val.content.vertical_panel.snapshot_min_tabs = 1
        self._workspace_with_open_tab(manager)
        self._install_objreg(
            monkeypatch, browser,
            {1: MockTab(1, title='Live', url=QUrl('https://a.example/'))})
        browser.widget.insert(MockTab(9), 0)
        calls = []
        monkeypatch.setattr(
            manager, 'create_window_snapshot',
            lambda *a, **k: calls.append((a, k)))

        bridge._maybe_snapshot_window(0)

        assert calls == []

    def test_empty_window_creates_snapshot(
            self, browser, manager, bridge, monkeypatch, config_stub):
        config_stub.val.content.vertical_panel.snapshot_min_tabs = 1
        workspace = self._workspace_with_open_tab(manager)
        self._install_objreg(
            monkeypatch, browser,
            {1: MockTab(1, title='Live', url=QUrl('https://a.example/'))})
        calls = []

        def fake_create(workspace_id, snapshots, *, min_tabs=1):
            calls.append((workspace_id, snapshots, min_tabs))
            return object()

        monkeypatch.setattr(manager, 'create_window_snapshot', fake_create)

        bridge._maybe_snapshot_window(0)

        assert calls == [(
            workspace.id,
            [{'url': 'https://a.example/', 'title': 'Live'}],
            1,
        )]

    def test_user_initiated_close_skips_and_clears_flag(
            self, browser, manager, bridge, monkeypatch, config_stub):
        config_stub.val.content.vertical_panel.snapshot_min_tabs = 1
        self._workspace_with_open_tab(manager)
        self._install_objreg(
            monkeypatch, browser,
            {1: MockTab(1, title='Live', url=QUrl('https://a.example/'))})
        calls = []
        monkeypatch.setattr(
            manager, 'create_window_snapshot',
            lambda *a, **k: calls.append((a, k)))
        bridge._user_initiated_close_flags[0] = True

        bridge._maybe_snapshot_window(0)

        assert calls == []
        assert bridge._user_initiated_close_flags[0] is False

    def test_flag_cleared_snapshots_on_next_call(
            self, browser, manager, bridge, monkeypatch, config_stub):
        config_stub.val.content.vertical_panel.snapshot_min_tabs = 1
        self._workspace_with_open_tab(manager)
        self._install_objreg(
            monkeypatch, browser,
            {1: MockTab(1, title='Live', url=QUrl('https://a.example/'))})
        calls = []
        monkeypatch.setattr(
            manager, 'create_window_snapshot',
            lambda *a, **k: calls.append((a, k)))
        bridge._user_initiated_close_flags[0] = True

        bridge._maybe_snapshot_window(0)
        bridge._maybe_snapshot_window(0)

        assert len(calls) == 1

    def test_min_tabs_from_config_is_forwarded(
            self, browser, manager, bridge, monkeypatch, config_stub):
        config_stub.val.content.vertical_panel.snapshot_min_tabs = 4
        self._workspace_with_open_tab(manager)
        self._install_objreg(
            monkeypatch, browser,
            {1: MockTab(1, title='Live', url=QUrl('https://a.example/'))})
        calls = []
        monkeypatch.setattr(
            manager, 'create_window_snapshot',
            lambda *a, **k: calls.append((a, k)))

        bridge._maybe_snapshot_window(0)

        assert calls[0][1]['min_tabs'] == 4


class TestDelayedHook:

    """VP-P4: tab removal schedules the delayed snapshot check."""

    def test_tab_shutdown_schedules_check(
            self, browser, manager, bridge, monkeypatch, config_stub):
        config_stub.val.content.vertical_panel.snapshot_delay_seconds = 7
        tab = _open_tab(browser, MockTab(1))
        seen = []

        def fake_single_shot(msec, callback):
            seen.append(msec)
            callback()

        monkeypatch.setattr(
            workspacebridge.QTimer, 'singleShot', fake_single_shot)
        checks = []
        monkeypatch.setattr(
            bridge, '_maybe_snapshot_window',
            lambda wid: checks.append(wid))

        tab.shutting_down.emit()

        assert seen == [7000]
        assert checks == [0]

    def test_tab_removed_schedules_check(
            self, browser, manager, bridge, monkeypatch, config_stub):
        config_stub.val.content.vertical_panel.snapshot_delay_seconds = 5
        _open_tab(browser, MockTab(1))
        seen = []

        def fake_single_shot(msec, callback):
            seen.append(msec)
            callback()

        monkeypatch.setattr(
            workspacebridge.QTimer, 'singleShot', fake_single_shot)
        checks = []
        monkeypatch.setattr(
            bridge, '_maybe_snapshot_window',
            lambda wid: checks.append(wid))

        browser.widget.remove(0)

        assert seen == [5000]
        assert checks == [0]


class TestJumpToEntry:

    """Click-to-jump from a search result (VP-P5)."""

    def _resources(self, monkeypatch, browser, registry=None):
        registry = registry or {}
        monkeypatch.setattr(
            workspacebridge,
            '_iter_window_resources',
            lambda: iter([(0, registry, browser)]),
        )

    def test_open_entry_switches_live_tab(
            self, browser, tab_registry, monkeypatch):
        tab = MockTab(1, title='A')
        browser.widget.insert(tab)
        tab_registry[1] = tab
        self._resources(monkeypatch, browser, tab_registry)
        entry = WorkspaceTab(tab_id=1, title='A', open_status='OPEN')

        workspacebridge.jump_to_entry(entry)

        assert browser.widget.currentWidget() is tab
        assert tab._focused is True

    def test_open_entry_not_in_registry_is_skipped(
            self, browser, tab_registry, monkeypatch):
        tab = MockTab(1, title='A')
        browser.widget.insert(tab)
        browser.widget.setCurrentWidget(tab)
        tab_registry[1] = tab
        self._resources(monkeypatch, browser, tab_registry)
        entry = WorkspaceTab(tab_id=9, title='orphan', open_status='OPEN')

        workspacebridge.jump_to_entry(entry)

        assert browser.widget.currentWidget() is tab

    def test_suspended_entry_reopens_then_switches(
            self, browser, monkeypatch):
        live = MockTab(5, title='Reopened')
        browser.widget.insert(live)
        other = MockTab(6, title='Other')
        browser.widget.insert(other)
        browser.widget.setCurrentWidget(other)
        self._resources(monkeypatch, browser, {5: live})
        calls = []

        def fake_reopen(entry, *, win_id=0):
            calls.append((entry, win_id))
            return live

        monkeypatch.setattr(workspacebridge, 'reopen_tab', fake_reopen)
        entry = WorkspaceTab(
            tab_id=5, url='https://example.com',
            open_status='SUSPENDED')

        workspacebridge.jump_to_entry(entry)

        assert calls == [(entry, 0)]
        assert browser.widget.currentWidget() is live

    def test_reopen_tab_reuses_live_url(
            self, browser, manager, tab_registry, monkeypatch):
        """The single-entry D4 path still reuses a live same-URL tab."""
        workspace = manager.add_workspace('ws')
        entry = WorkspaceTab(
            tab_id=10, url='https://example.com',
            open_status='SUSPENDED')
        manager.add_tab(workspace.id, entry)

        live = MockTab(3, url=QUrl('https://example.com'))
        browser.widget.insert(live)
        tab_registry[3] = live
        self._resources(monkeypatch, browser, tab_registry)
        monkeypatch.setattr(
            workspacebridge, '_get_manager', lambda: manager)

        result = workspacebridge.reopen_tab(entry)

        assert result is live
        assert entry.open_status == 'OPEN'
        assert browser.widget.currentWidget() is live


class TestBridgeSuspendedActivation:

    """Tree-row activation of a SUSPENDED search result (VP-P5)."""

    def test_activate_reopens_then_focuses(
            self, browser, manager, model, bridge, monkeypatch):
        workspace = manager.add_workspace('ws')
        entry = WorkspaceTab(
            tab_id=10, url='https://example.com',
            open_status='SUSPENDED')
        manager.add_tab(workspace.id, entry)

        live = MockTab(10, url=QUrl('https://example.com'))
        browser.widget.insert(live)
        calls = []

        def fake_reopen(tab_entry, *, win_id=0):
            calls.append((tab_entry, win_id))
            return live

        monkeypatch.setattr(workspacebridge, 'reopen_tab', fake_reopen)

        bridge._activate_tab_id(10, focus=True)

        assert calls == [(entry, 0)]
        assert browser.widget.currentWidget() is live
        assert live._focused is True
