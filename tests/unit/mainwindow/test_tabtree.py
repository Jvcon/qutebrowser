# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the vertical panel's TabTree (Phase 2, slice 1)."""

import gc
import uuid
import weakref
from types import SimpleNamespace

import pytest

from qutebrowser.qt.core import (
    QEvent,
    QMimeData,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    pyqtSignal,
)
from qutebrowser.qt.gui import (
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QHelpEvent,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from qutebrowser.qt.widgets import (
    QApplication,
    QMenu,
    QStyle,
    QStyleOptionViewItem,
    QTreeView,
)

from qutebrowser.mainwindow import tabtree, workspacebridge
from qutebrowser.mainwindow.pinpanel import PINNED_TAB_MIME
from qutebrowser.utils.qtutils import weak_slot
from qutebrowser.workspaces.manager import WorkspaceMoveError
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)


class MockManager(QObject):

    """Minimal stand-in for ``WorkspaceManager`` in TabTree tests."""

    changed = pyqtSignal()

    def __init__(self, workspace=None):
        super().__init__()
        self._workspace = workspace
        self._pinned = []

    def get_active(self):
        return self._workspace

    def add_tab(self, workspace_id, tab):
        self._workspace.tabs.append(tab)

    def add_pinned(self, tab):
        self._pinned.append(tab)

    def remove_pinned(self, tab_id):
        self._pinned = [tab for tab in self._pinned
                        if tab.tab_id != tab_id]

    def list_pinned(self):
        return list(self._pinned)

    def group_open_count(self, workspace_id, group_id):
        return sum(
            1 for tab in self._workspace.tabs
            if tab.group_id == group_id and tab.open_status == 'OPEN'
        )

    def group_total_count(self, workspace_id, group_id):
        return sum(
            1 for tab in self._workspace.tabs
            if tab.group_id == group_id
        )

    def group_state(self, workspace_id, group_id):
        group = next(
            group for group in self._workspace.groups
            if group.group_id == group_id
        )
        if group.archived:
            return 'ARCHIVED'
        if self.group_open_count(workspace_id, group_id) > 0:
            return 'OPEN'
        return 'SUSPENDED'


def _make_workspace():
    """Return a workspace with one group, two standalones and one parent tab."""
    group = Group(group_id=uuid.uuid4(), name='client-A docs', folded=False)
    parent_tab = WorkspaceTab(tab_id=10, parent_id=None, group_id=None)
    child_a = WorkspaceTab(tab_id=11, parent_id=10, group_id=None)
    child_b = WorkspaceTab(tab_id=12, parent_id=10, group_id=None)
    standalone_a = WorkspaceTab(tab_id=20, parent_id=None, group_id=None)
    standalone_b = WorkspaceTab(tab_id=21, parent_id=None, group_id=None)
    group_tab_a = WorkspaceTab(
        tab_id=30, parent_id=None, group_id=group.group_id)
    group_tab_b = WorkspaceTab(
        tab_id=31, parent_id=None, group_id=group.group_id)
    group_tab_c = WorkspaceTab(
        tab_id=32, parent_id=None, group_id=group.group_id)

    workspace = Workspace(
        id=uuid.uuid4(),
        name='test',
        groups=[group],
        tabs=[
            parent_tab, child_a, child_b,
            standalone_a, standalone_b,
            group_tab_a, group_tab_b, group_tab_c,
        ],
    )
    # These fixtures represent live entries; older files default to SUSPENDED.
    for tab in workspace.tabs:
        tab.open_status = 'OPEN'
    return workspace


class TestTabTreeModel:

    """Tests for :class:`tabtree.TabTreeModel`."""

    @pytest.fixture
    def model(self, qtbot):
        """Model backed by a populated workspace."""
        manager = MockManager(_make_workspace())
        m = tabtree.TabTreeModel(manager, parent=None)
        # Populate display data so role tests have non-empty values.
        m.set_tab_info(10, title='Parent', url='qute://parent',
                       status=tabtree.Status.AUDIBLE)
        m.set_tab_info(11, title='Child A', url='qute://child-a')
        m.set_tab_info(12, title='Child B', url='qute://child-b')
        m.set_tab_info(20, title='Standalone A', url='qute://standalone-a')
        m.set_tab_info(21, title='Standalone B', url='qute://standalone-b')
        m.set_tab_info(30, title='Group Tab A', url='qute://group-a')
        m.set_tab_info(31, title='Group Tab B', url='qute://group-b')
        m.set_tab_info(32, title='Group Tab C', url='qute://group-c')
        return m

    def test_topology(self, model):
        """The model exposes groups first, then root-level tabs."""
        root = QModelIndex()
        assert model.rowCount(root) == 4  # 1 group + 2 standalones + 1 parent

        group_index = model.index(0, 0, root)
        assert group_index.data(tabtree.KindRole) == tabtree.Kind.Group
        assert model.rowCount(group_index) == 3
        for row in range(3):
            child = model.index(row, 0, group_index)
            assert child.data(tabtree.KindRole) in (
                tabtree.Kind.Tab, tabtree.Kind.ParentTab)
            assert child.parent() == group_index

        # Collect the three root-level tabs by title.
        root_tabs: dict[str, QModelIndex] = {}
        for row in range(1, model.rowCount(root)):
            idx = model.index(row, 0, root)
            root_tabs[idx.data(Qt.ItemDataRole.DisplayRole)] = idx

        assert sorted(root_tabs) == ['Parent', 'Standalone A', 'Standalone B']

        parent_index = root_tabs['Parent']
        assert parent_index.data(tabtree.KindRole) == tabtree.Kind.ParentTab
        assert model.rowCount(parent_index) == 2
        for row in range(2):
            child = model.index(row, 0, parent_index)
            assert child.data(tabtree.KindRole) == tabtree.Kind.Tab
            assert child.parent() == parent_index

        for title in ('Standalone A', 'Standalone B'):
            idx = root_tabs[title]
            assert idx.data(tabtree.KindRole) == tabtree.Kind.Tab
            assert idx.parent() == root

    def test_size_hint_group_vs_tab(self, model):
        """Group headers are 24 px high; tab rows are 22 px high."""
        delegate = tabtree.TabTreeItemDelegate()
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 220, 24)

        group_index = model.index(0, 0, QModelIndex())
        tab_index = model.index(1, 0, QModelIndex())

        assert delegate.sizeHint(option, group_index).height() == 24
        assert delegate.sizeHint(option, tab_index).height() == 22

    def test_data_roles(self, model):
        """Custom roles return the expected values."""
        root = QModelIndex()
        group_index = model.index(0, 0, root)
        assert group_index.data(Qt.ItemDataRole.DisplayRole) == 'client-A docs'
        assert group_index.data(tabtree.KindRole) == tabtree.Kind.Group
        assert group_index.data(tabtree.FoldedRole) is False
        assert group_index.data(tabtree.BadgeRole) == '3/3'
        assert group_index.data(tabtree.StatusRole) == tabtree.Status.NONE

        root_tabs = {
            model.index(row, 0, root).data(Qt.ItemDataRole.DisplayRole):
                model.index(row, 0, root)
            for row in range(1, model.rowCount(root))
        }
        tab_index = root_tabs['Standalone A']
        assert tab_index.data(tabtree.KindRole) == tabtree.Kind.Tab
        assert tab_index.data(tabtree.UrlRole) == 'qute://standalone-a'
        assert tab_index.data(tabtree.FoldedRole) is False

        parent_index = root_tabs['Parent']
        assert parent_index.data(tabtree.KindRole) == tabtree.Kind.ParentTab
        assert parent_index.data(tabtree.StatusRole) == tabtree.Status.AUDIBLE

    def test_p_role_exposed_via_data(self, model):
        """PinStateRole is derived from the PINNED status flag."""
        root = QModelIndex()
        standalone = model.index(2, 0, root)  # tab id 20
        assert standalone.data(tabtree.PinStateRole) is False

        model.set_tab_info(20, status=tabtree.Status.PINNED)
        assert standalone.data(tabtree.PinStateRole) is True

        model.set_tab_info(20, status=tabtree.Status.NONE)
        assert standalone.data(tabtree.PinStateRole) is False

    def test_set_tab_pinned_emits_data_changed(self, model):
        """set_tab_pinned flips the bit and emits PinStateRole."""
        root = QModelIndex()
        standalone = model.index(2, 0, root)
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))

        model.set_tab_pinned(20, True)
        assert standalone.data(tabtree.PinStateRole) is True
        assert changes[-1][2] == [tabtree.PinStateRole]

        model.set_tab_pinned(20, False)
        assert standalone.data(tabtree.PinStateRole) is False

    def test_open_status_role_and_suspension(self, model):
        """OpenStatusRole exposes the entry state and drives the zZ badge."""
        root = QModelIndex()
        standalone = model.index(2, 0, root)  # tab id 20

        assert standalone.data(tabtree.OpenStatusRole) == 'OPEN'
        assert not (standalone.data(tabtree.StatusRole)
                    & tabtree.Status.SUSPENDED)

        for tab in model._workspace.tabs:
            if tab.tab_id == 20:
                tab.open_status = 'SUSPENDED'
        model.set_workspace(model._workspace)
        standalone = model.index(2, 0, root)

        assert standalone.data(tabtree.OpenStatusRole) == 'SUSPENDED'
        assert standalone.data(tabtree.StatusRole) & tabtree.Status.SUSPENDED

    def test_empty_group_badge_zero(self):
        """Empty groups report a ``"0/0"`` badge."""
        group = Group(group_id=uuid.uuid4(), name='empty', folded=False)
        workspace = Workspace(
            id=uuid.uuid4(), name='test', groups=[group], tabs=[])
        model = tabtree.TabTreeModel(MockManager(workspace))
        group_index = model.index(0, 0, QModelIndex())
        assert group_index.data(tabtree.BadgeRole) == '0/0'

    def test_active_tab_id_is_exposed_via_data(self, model):
        """ActiveRole is True only on the row for the active tab id."""
        model.set_active_tab_id(20)
        root = QModelIndex()
        group_index = model.index(0, 0, root)     # group
        parent_index = model.index(1, 0, root)    # parent tab, id 10
        active_index = model.index(2, 0, root)    # standalone tab, id 20

        assert active_index.data(tabtree.ActiveRole) is True
        assert parent_index.data(tabtree.ActiveRole) is False
        assert group_index.data(tabtree.ActiveRole) is False

        model.set_active_tab_id(None)
        assert active_index.data(tabtree.ActiveRole) is False

    def test_set_active_tab_id_emits_data_changed(self, model):
        """Changing the active tab emits dataChanged carrying ActiveRole."""
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))
        model.set_active_tab_id(20)
        assert len(changes) == 1
        top_left = changes[0][0]
        assert top_left.isValid()
        assert top_left.data(tabtree.KindRole) == tabtree.Kind.Tab
        assert changes[0][2] == [tabtree.ActiveRole]

    def test_set_active_tab_id_is_idempotent(self, model):
        """Setting the same active id again emits nothing."""
        model.set_active_tab_id(20)
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))
        model.set_active_tab_id(20)
        assert changes == []

    def test_tab_id_for_index(self, model):
        """tab_id_for_index resolves tab rows and rejects group rows."""
        root = QModelIndex()
        assert model.tab_id_for_index(QModelIndex()) is None
        assert model.tab_id_for_index(model.index(0, 0, root)) is None
        assert model.tab_id_for_index(model.index(1, 0, root)) == 10
        assert model.tab_id_for_index(model.index(2, 0, root)) == 20

    def test_pinned_groups_sort_first_and_stable(self):
        """Pinned groups come first; insertion order is stable per band."""
        groups = [
            Group(group_id=uuid.uuid4(), name='unpinned-1'),
            Group(group_id=uuid.uuid4(), name='pinned-1', pinned=True),
            Group(group_id=uuid.uuid4(), name='unpinned-2'),
            Group(group_id=uuid.uuid4(), name='pinned-2', pinned=True),
        ]
        workspace = Workspace(
            id=uuid.uuid4(), name='test', groups=groups, tabs=[])
        model = tabtree.TabTreeModel(MockManager(workspace))

        root = QModelIndex()
        names = [
            model.index(row, 0, root).data(Qt.ItemDataRole.DisplayRole)
            for row in range(model.rowCount(root))
        ]
        assert names == [
            'pinned-1', 'pinned-2', 'unpinned-1', 'unpinned-2']

    def test_archived_groups_hidden_by_default(self):
        """Archived groups are filtered out until set_show_archived(True)."""
        visible = Group(group_id=uuid.uuid4(), name='visible')
        archived = Group(
            group_id=uuid.uuid4(), name='archived', archived=True)
        workspace = Workspace(
            id=uuid.uuid4(), name='test',
            groups=[visible, archived], tabs=[])
        model = tabtree.TabTreeModel(MockManager(workspace))
        root = QModelIndex()

        def names():
            return [
                model.index(row, 0, root).data(
                    Qt.ItemDataRole.DisplayRole)
                for row in range(model.rowCount(root))
            ]

        assert names() == ['visible']

        model.set_show_archived(True)
        # Both groups are non-OPEN, so they share the suspended band and
        # sort by name ('archived' < 'visible').
        assert names() == ['archived', 'visible']

        model.set_show_archived(False)
        assert names() == ['visible']

    def test_badge_open_over_total(self):
        """BadgeRole reports the OPEN/total tab counts as a string."""
        group = Group(group_id=uuid.uuid4(), name='mix')
        tabs = [
            WorkspaceTab(tab_id=1, group_id=group.group_id,
                         open_status='OPEN'),
            WorkspaceTab(tab_id=2, group_id=group.group_id,
                         open_status='SUSPENDED'),
            WorkspaceTab(tab_id=3, group_id=group.group_id,
                         open_status='OPEN'),
        ]
        workspace = Workspace(
            id=uuid.uuid4(), name='test', groups=[group], tabs=tabs)
        model = tabtree.TabTreeModel(MockManager(workspace))

        group_index = model.index(0, 0, QModelIndex())
        assert group_index.data(tabtree.BadgeRole) == '2/3'

    def test_group_state_role(self):
        """GroupStateRole resolves empty / suspended / open groups."""
        empty = Group(group_id=uuid.uuid4(), name='empty')
        suspended = Group(group_id=uuid.uuid4(), name='suspended')
        opened = Group(group_id=uuid.uuid4(), name='opened')
        tabs = [
            WorkspaceTab(tab_id=1, group_id=suspended.group_id,
                         open_status='SUSPENDED'),
            WorkspaceTab(tab_id=2, group_id=opened.group_id,
                         open_status='OPEN'),
        ]
        workspace = Workspace(
            id=uuid.uuid4(), name='test',
            groups=[empty, suspended, opened], tabs=tabs)
        model = tabtree.TabTreeModel(MockManager(workspace))
        root = QModelIndex()

        states = {
            model.index(row, 0, root).data(Qt.ItemDataRole.DisplayRole):
                model.index(row, 0, root).data(tabtree.GroupStateRole)
            for row in range(model.rowCount(root))
        }
        assert states == {
            'empty': 'SUSPENDED',
            'suspended': 'SUSPENDED',
            'opened': 'OPEN',
        }

    def test_group_state_role_archived(self):
        """Archived groups report ARCHIVED when shown."""
        group = Group(group_id=uuid.uuid4(), name='archived', archived=True)
        workspace = Workspace(
            id=uuid.uuid4(), name='test', groups=[group], tabs=[
                WorkspaceTab(tab_id=1, group_id=group.group_id,
                             open_status='OPEN')])
        model = tabtree.TabTreeModel(MockManager(workspace))
        model.set_show_archived(True)

        group_index = model.index(0, 0, QModelIndex())
        assert group_index.data(tabtree.GroupStateRole) == 'ARCHIVED'

    def test_sort_open_groups_first(self, config_stub):
        """OPEN groups sort before suspended ones; pinned still come first."""
        config_stub.val.content.vertical_panel.sort_open_groups_first = True
        suspended = Group(group_id=uuid.uuid4(), name='suspended')
        opened = Group(group_id=uuid.uuid4(), name='opened')
        pinned = Group(group_id=uuid.uuid4(), name='pinned', pinned=True)
        tabs = [
            WorkspaceTab(tab_id=1, group_id=suspended.group_id,
                         open_status='SUSPENDED'),
            WorkspaceTab(tab_id=2, group_id=opened.group_id,
                         open_status='OPEN'),
        ]
        workspace = Workspace(
            id=uuid.uuid4(), name='test',
            groups=[suspended, opened, pinned], tabs=tabs)
        model = tabtree.TabTreeModel(MockManager(workspace))
        root = QModelIndex()

        names = [
            model.index(row, 0, root).data(Qt.ItemDataRole.DisplayRole)
            for row in range(model.rowCount(root))
        ]
        assert names == ['pinned', 'opened', 'suspended']

    def test_sort_config_disabled_preserves_order(self, config_stub):
        """With the setting off, groups keep their original sort order."""
        config_stub.val.content.vertical_panel.sort_open_groups_first = False
        suspended = Group(group_id=uuid.uuid4(), name='suspended')
        opened = Group(group_id=uuid.uuid4(), name='opened')
        tabs = [
            WorkspaceTab(tab_id=1, group_id=suspended.group_id,
                         open_status='SUSPENDED'),
            WorkspaceTab(tab_id=2, group_id=opened.group_id,
                         open_status='OPEN'),
        ]
        workspace = Workspace(
            id=uuid.uuid4(), name='test',
            groups=[suspended, opened], tabs=tabs)
        model = tabtree.TabTreeModel(MockManager(workspace))
        root = QModelIndex()

        names = [
            model.index(row, 0, root).data(Qt.ItemDataRole.DisplayRole)
            for row in range(model.rowCount(root))
        ]
        assert names == ['suspended', 'opened']

    def test_archived_sorts_with_suspended_band(self, config_stub):
        """Archived groups sort after OPEN ones, alongside suspended ones."""
        config_stub.val.content.vertical_panel.sort_open_groups_first = True
        suspended = Group(group_id=uuid.uuid4(), name='suspended')
        opened = Group(group_id=uuid.uuid4(), name='opened')
        archived = Group(
            group_id=uuid.uuid4(), name='archived', archived=True)
        tabs = [
            WorkspaceTab(tab_id=1, group_id=suspended.group_id,
                         open_status='SUSPENDED'),
            WorkspaceTab(tab_id=2, group_id=opened.group_id,
                         open_status='OPEN'),
        ]
        workspace = Workspace(
            id=uuid.uuid4(), name='test',
            groups=[suspended, opened, archived], tabs=tabs)
        model = tabtree.TabTreeModel(MockManager(workspace))
        model.set_show_archived(True)
        root = QModelIndex()

        names = [
            model.index(row, 0, root).data(Qt.ItemDataRole.DisplayRole)
            for row in range(model.rowCount(root))
        ]
        assert names == ['opened', 'archived', 'suspended']


class TestAutoLocate:

    """VP-D5: active-group accent, transient tab highlight and focus."""

    @pytest.fixture
    def model(self, qtbot):
        manager = MockManager(_make_workspace())
        return tabtree.TabTreeModel(manager)

    @pytest.fixture
    def group_id(self, model):
        return model._workspace.groups[0].group_id

    def test_active_group_role_and_data_changed(self, model, group_id):
        """set_active_group_id flips ActiveGroupRole and emits dataChanged."""
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))

        model.set_active_group_id(group_id)

        assert model.active_group_id() == group_id
        group_index = model.index(0, 0, QModelIndex())
        assert group_index.data(tabtree.ActiveGroupRole) is True
        assert changes[0][2] == [tabtree.ActiveGroupRole]
        # Tab rows never claim the active-group role.
        assert model.index(2, 0, QModelIndex()).data(
            tabtree.ActiveGroupRole) is False

    def test_set_active_group_id_is_idempotent(self, model, group_id):
        """Setting the same active group again emits nothing."""
        model.set_active_group_id(group_id)
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))
        model.set_active_group_id(group_id)
        assert changes == []

    def test_clear_active_group_id(self, model, group_id):
        """Passing None clears the marker."""
        model.set_active_group_id(group_id)
        model.set_active_group_id(None)
        assert model.active_group_id() is None
        assert model.index(0, 0, QModelIndex()).data(
            tabtree.ActiveGroupRole) is False

    def test_highlight_role_reflects(self, model, monkeypatch):
        """set_highlighted_tab_id flips HighlightRole for the tab row."""
        monkeypatch.setattr(tabtree, 'QTimer', _FakeQTimer)
        model.set_highlighted_tab_id(20)
        index = model.index(2, 0, QModelIndex())  # standalone tab id 20
        assert index.data(tabtree.HighlightRole) is True
        assert model.index(3, 0, QModelIndex()).data(
            tabtree.HighlightRole) is False

    def test_highlight_resets_after_timeout(self, model, monkeypatch):
        """The scheduled callback clears the highlight."""
        monkeypatch.setattr(tabtree, 'QTimer', _FakeQTimer)
        model.set_highlighted_tab_id(20, timeout_ms=1234)

        timer = model._highlight_timer
        assert timer is not None
        assert timer.single_shot is True
        assert timer.interval == 1234
        assert model.index(2, 0, QModelIndex()).data(
            tabtree.HighlightRole) is True

        timer.timeout.emit()  # fire the scheduled reset

        assert model.index(2, 0, QModelIndex()).data(
            tabtree.HighlightRole) is False

    def test_stale_timer_cannot_clear_newer_highlight(
            self, model, monkeypatch):
        """A -> B -> A keeps only the latest timer, even for the same id.

        The old implementation used independent ``QTimer.singleShot``
        callbacks; the first A callback could still be pending and would
        clear the freshly re-highlighted A prematurely.  The instance timer
        cancels/replaces the previous one so only the latest stays live.
        """
        monkeypatch.setattr(tabtree, 'QTimer', _FakeQTimer)

        model.set_highlighted_tab_id(20)
        first = model._highlight_timer
        model.set_highlighted_tab_id(21)
        second = model._highlight_timer
        model.set_highlighted_tab_id(20)
        latest = model._highlight_timer

        assert first is not None
        assert second is not None
        assert latest is not None
        assert first is not second
        assert second is not latest
        # The superseded timers were cancelled and scheduled for deletion.
        assert first.active is False
        assert first.deleted is True
        assert second.active is False
        assert second.deleted is True
        assert latest.active is True
        assert model._highlighted_tab_id == 20

        # A cancelled timer never fires, so it cannot wipe the newer A.
        first.timeout.emit()
        assert model._highlighted_tab_id == 20

        # Advancing the live timer clears the latest A once.
        latest.timeout.emit()
        assert model._highlighted_tab_id is None

    def test_highlight_callbacks_capture_each_tab_id(
            self, model, monkeypatch):
        """Each timer callback carries its own tab id, not a shared cell."""
        monkeypatch.setattr(tabtree, 'QTimer', _FakeQTimer)
        cleared = []
        monkeypatch.setattr(model, '_clear_highlight', cleared.append)

        model.set_highlighted_tab_id(20)
        first = model._highlight_timer
        model.set_highlighted_tab_id(21)
        second = model._highlight_timer

        assert first is not None
        assert second is not None
        # Starting the second highlight cancelled the first timer; force
        # both live so we can observe the captured ids directly.
        first.active = True
        second.active = True
        first.timeout.emit()
        second.timeout.emit()

        assert cleared == [20, 21]

    def test_focus_group_and_tab_expands_and_returns_index(self, model, group_id):
        """focus_group_and_tab expands the group and returns the tab index."""
        model._workspace.groups[0].folded = True

        index = model.focus_group_and_tab(group_id, 30)

        assert model._workspace.groups[0].folded is False
        assert model.active_group_id() == group_id
        assert index.isValid()
        assert index.data(tabtree.KindRole) == tabtree.Kind.Tab

    def test_focus_group_and_tab_standalone(self, model):
        """A None group id focuses a standalone tab without an accent."""
        index = model.focus_group_and_tab(None, 20)
        assert index.isValid()
        assert model.active_group_id() is None

    def test_focus_group_and_tab_unknown_tab(self, model, group_id):
        """An unknown tab returns an invalid index."""
        index = model.focus_group_and_tab(group_id, 999)
        assert not index.isValid()


class _FakeTimeout:

    """Minimal ``QTimer.timeout`` signal stand-in.

    ``emit`` only fires while the owning timer is active, mirroring Qt's
    behaviour for a stopped / deleted timer.
    """

    def __init__(self, owner: '_FakeQTimer') -> None:
        self._owner = owner
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self) -> None:
        if not self._owner.active:
            return
        for callback in list(self._callbacks):
            callback()


class _FakeQTimer:

    """``QTimer`` stand-in exposing start/stop and a fireable timeout.

    The model now keeps a per-highlight ``QTimer`` instance, so this fake
    supports instantiation (unlike the previous no-op stand-in) and lets
    tests fire the timeout synchronously.
    """

    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.timeout = _FakeTimeout(self)
        self.interval = None
        self.single_shot = False
        self.active = False
        self.deleted = False

    def setSingleShot(self, value) -> None:  # noqa: N802 - Qt naming
        self.single_shot = value

    def start(self, ms) -> None:  # noqa: N802 - Qt naming
        self.interval = ms
        self.active = True

    def stop(self) -> None:  # noqa: N802 - Qt naming
        self.active = False

    def deleteLater(self) -> None:  # noqa: N802 - Qt naming
        self.deleted = True


class TestTabTreeItemDelegate:

    """Tests for :class:`tabtree.TabTreeItemDelegate`."""

    @pytest.fixture
    def delegate(self):
        """Fresh delegate with captured signals."""
        d = tabtree.TabTreeItemDelegate()
        d._captured = []
        d.fold_toggled.connect(lambda idx: d._captured.append(('fold', idx)))
        d.audio_toggled.connect(lambda idx: d._captured.append(('audio', idx)))
        d.activated.connect(lambda idx: d._captured.append(('activated', idx)))
        return d

    @pytest.fixture
    def model(self, qtbot):
        """Populated model with one audible tab."""
        workspace = _make_workspace()
        manager = MockManager(workspace)
        m = tabtree.TabTreeModel(manager)
        m.set_tab_info(10, title='Parent', status=tabtree.Status.AUDIBLE)
        return m

    @pytest.fixture
    def option(self):
        """Style option covering the full 220 px row."""
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 220, 22)
        opt.fontMetrics = opt.fontMetrics
        return opt

    def _press(self, delegate, model, option, index, pos):
        """Send a left-button press event to the delegate at *pos*."""
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(pos),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        return delegate.editorEvent(event, model, option, index)

    def test_chevron_click_toggles_fold(self, delegate, model, option):
        """Clicking the chevron emits fold_toggled."""
        group_index = model.index(0, 0, QModelIndex())
        # The chevron occupies the leftmost CHEVRON_WIDTH pixels.
        pos = QPoint(3, option.rect.height() // 2)
        assert self._press(delegate, model, option, group_index, pos) is True
        assert delegate._captured == [('fold', group_index)]

    def test_audio_click_toggles_audio(self, delegate, model, option):
        """Clicking the audio icon emits audio_toggled."""
        root = QModelIndex()
        parent_index = None
        for row in range(model.rowCount(root)):
            idx = model.index(row, 0, root)
            if idx.data(tabtree.KindRole) == tabtree.Kind.ParentTab:
                parent_index = idx
                break
        assert parent_index is not None

        # Status strip is right-aligned; audio is its leftmost glyph.
        fm = option.fontMetrics
        status_text = '♪'  # audible only
        text_width = fm.horizontalAdvance(status_text)
        pos = QPoint(option.rect.right() - 6 - text_width + 2,
                     option.rect.height() // 2)
        assert self._press(delegate, model, option, parent_index, pos) is True
        assert delegate._captured == [('audio', parent_index)]

    def test_body_click_activates(self, delegate, model, option):
        """Clicking the row body emits activated."""
        root = QModelIndex()
        tab_index = None
        for row in range(model.rowCount(root)):
            idx = model.index(row, 0, root)
            if idx.data(tabtree.KindRole) == tabtree.Kind.Tab:
                tab_index = idx
                break
        assert tab_index is not None

        # Click in the middle of the row, well right of any icon.
        pos = QPoint(60, option.rect.height() // 2)
        assert self._press(delegate, model, option, tab_index, pos) is True
        assert delegate._captured == [('activated', tab_index)]

    def test_help_event_tooltip(self, delegate, model, option, qtbot):
        """helpEvent returns a tooltip for group and tab rows."""
        view = QTreeView()
        qtbot.add_widget(view)

        group_index = model.index(0, 0, QModelIndex())
        event = QHelpEvent(
            QEvent.Type.ToolTip,
            QPoint(10, 10),
            QPoint(10, 10),
        )
        assert delegate.helpEvent(event, view, option, group_index) is True

        tab_index = model.index(1, 0, QModelIndex())
        assert delegate.helpEvent(event, view, option, tab_index) is True

    @staticmethod
    def _paint_row(delegate, model, option, index):
        """Paint *index* through *delegate* with a real QPainter."""
        pixmap = QPixmap(option.rect.width(), option.rect.height())
        painter = QPainter(pixmap)
        try:
            delegate.paint(painter, option, index)
        finally:
            painter.end()
        return pixmap

    def test_paint_includes_close_x_when_hovered(self, model, option):
        """Hovered tab rows record a close-glyph hit rect while painting."""
        delegate = tabtree.TabTreeItemDelegate()
        tab_index = model.index(1, 0, QModelIndex())
        option.state = QStyle.StateFlag.State_MouseOver
        self._paint_row(delegate, model, option, tab_index)

        close_rect = delegate._last_close_rect.get(tab_index)
        assert close_rect is not None
        assert close_rect.width() > 0
        assert close_rect.height() > 0
        assert option.rect.contains(close_rect)

    def test_paint_omits_close_x_when_not_hovered(self, model, option):
        """Non-hovered tab rows record no close-glyph hit rect."""
        delegate = tabtree.TabTreeItemDelegate()
        tab_index = model.index(1, 0, QModelIndex())
        self._paint_row(delegate, model, option, tab_index)

        assert delegate._last_close_rect.get(tab_index) is None

    def test_editor_event_close_x_emits_signal(self, model, option):
        """Pressing inside the close glyph emits close_requested."""
        delegate = tabtree.TabTreeItemDelegate()
        tab_index = model.index(1, 0, QModelIndex())
        option.state = QStyle.StateFlag.State_MouseOver
        self._paint_row(delegate, model, option, tab_index)

        close_rect = delegate._last_close_rect[tab_index]
        emitted = []
        delegate.close_requested.connect(
            lambda idx: emitted.append(idx))

        pos = close_rect.center()
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(pos),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        assert delegate.editorEvent(event, model, option, tab_index) is True
        assert emitted == [tab_index]

    def test_pin_glyph_drawn_for_pinned_tab(self, model, option):
        """A pinned tab records a pin-glyph hit rect while painting."""
        delegate = tabtree.TabTreeItemDelegate()
        model.set_tab_info(10, status=tabtree.Status.PINNED)
        tab_index = model.index(1, 0, QModelIndex())
        self._paint_row(delegate, model, option, tab_index)

        pin_rect = delegate._last_pin_rect.get(tab_index)
        assert pin_rect is not None
        assert pin_rect.width() > 0
        assert pin_rect.height() > 0
        assert option.rect.contains(pin_rect)

    def test_pin_glyph_drawn_on_hover_when_unpinned(self, model, option):
        """An unpinned tab shows the pin glyph only while hovered."""
        delegate = tabtree.TabTreeItemDelegate()
        tab_index = model.index(1, 0, QModelIndex())
        option.state = QStyle.StateFlag.State_MouseOver
        self._paint_row(delegate, model, option, tab_index)

        pin_rect = delegate._last_pin_rect.get(tab_index)
        assert pin_rect is not None
        assert option.rect.contains(pin_rect)

    def test_pin_glyph_omitted_when_unpinned_not_hovered(self, model, option):
        """An unpinned, non-hovered tab records no pin-glyph hit rect."""
        delegate = tabtree.TabTreeItemDelegate()
        tab_index = model.index(1, 0, QModelIndex())
        self._paint_row(delegate, model, option, tab_index)

        assert delegate._last_pin_rect.get(tab_index) is None

    def test_pin_toggle_emits_on_p_click(self, model, option):
        """Pressing inside the pin glyph emits pin_toggled."""
        delegate = tabtree.TabTreeItemDelegate()
        model.set_tab_info(10, status=tabtree.Status.PINNED)
        tab_index = model.index(1, 0, QModelIndex())
        self._paint_row(delegate, model, option, tab_index)

        pin_rect = delegate._last_pin_rect[tab_index]
        emitted = []
        delegate.pin_toggled.connect(lambda idx: emitted.append(idx))

        pos = pin_rect.center()
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(pos),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        assert delegate.editorEvent(event, model, option, tab_index) is True
        assert emitted == [tab_index]

    def test_active_tab_highlight_drawn_when_set(self, model, option):
        """The active-tab left accent is drawn once the model marks it."""
        model.set_active_tab_id(10)
        delegate = tabtree.TabTreeItemDelegate()
        active_index = model.index(1, 0, QModelIndex())
        other_index = model.index(2, 0, QModelIndex())

        self._paint_row(delegate, model, option, active_index)
        assert delegate._last_active_accent is not None
        assert delegate._last_active_accent.width() == 2

        self._paint_row(delegate, model, option, other_index)
        assert delegate._last_active_accent is None

    def test_paint_highlight_and_active_group(self, model, option, monkeypatch):
        """HighlightRole and ActiveGroupRole paint without raising."""
        monkeypatch.setattr(tabtree, 'QTimer', _FakeQTimer)
        group_id = model._workspace.groups[0].group_id
        model.set_active_group_id(group_id)
        model.set_highlighted_tab_id(20)
        delegate = tabtree.TabTreeItemDelegate()

        group_index = model.index(0, 0, QModelIndex())
        tab_index = model.index(2, 0, QModelIndex())
        self._paint_row(delegate, model, option, group_index)
        self._paint_row(delegate, model, option, tab_index)


class TestSelectionMode:

    """Tests for the VP-F4 multi-select API."""

    @pytest.fixture
    def model(self, qtbot):
        """Model backed by a populated workspace with per-tab titles."""
        manager = MockManager(_make_workspace())
        m = tabtree.TabTreeModel(manager, parent=None)
        for tab_id in (10, 11, 12, 20, 21, 30, 31, 32):
            m.set_tab_info(tab_id, title=str(tab_id))
        return m

    def test_selection_role_defaults_false(self, model):
        """SelectionRole is False for tabs and groups until set."""
        root = QModelIndex()
        assert model.index(2, 0, root).data(tabtree.SelectionRole) is False
        assert model.index(0, 0, root).data(tabtree.SelectionRole) is False

    def test_set_selected_updates_role_and_emits(self, model):
        """set_selected flips the role and emits SelectionRole."""
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))

        model.set_selected(20, True)

        assert model.selected_tab_ids() == {20}
        standalone = model.index(2, 0, QModelIndex())
        assert standalone.data(tabtree.SelectionRole) is True
        assert changes[-1][2] == [tabtree.SelectionRole]

        model.set_selected(20, False)
        assert standalone.data(tabtree.SelectionRole) is False

    def test_select_range_adds_and_removes(self, model):
        """select_range is a bulk version of set_selected."""
        model.select_range([10, 11, 20])
        assert model.selected_tab_ids() == {10, 11, 20}

        model.select_range([11], selected=False)
        assert model.selected_tab_ids() == {10, 20}

    def test_select_all_and_none(self, model):
        """select_all covers every tab in the workspace, then clears."""
        model.select_all(True)
        assert model.selected_tab_ids() == {10, 11, 12, 20, 21, 30, 31, 32}

        model.select_all(False)
        assert model.selected_tab_ids() == set()

    def test_selection_mode_toggle_and_clear(self, model):
        """Leaving selection mode clears the selection."""
        assert model.selection_mode() is False

        model.select_range([20, 21])
        model.set_selection_mode(True)
        assert model.selection_mode() is True
        assert model.selected_tab_ids() == {20, 21}

        model.set_selection_mode(False)
        assert model.selection_mode() is False
        assert model.selected_tab_ids() == set()

    def test_disable_emits_data_changed(self, model):
        """Disabling selection mode repaints rows carrying SelectionRole."""
        model.set_selection_mode(True)
        model.set_selected(20, True)

        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))
        model.set_selection_mode(False)

        assert any(args[2] == [tabtree.SelectionRole] for args in changes)

    def test_paint_selected_row_does_not_raise(self, model):
        """Painting a selected row runs cleanly and draws the checkmark."""
        model.set_selected(20, True)
        delegate = tabtree.TabTreeItemDelegate()
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 220, 22)
        index = model.index(2, 0, QModelIndex())

        image = QImage(220, 22, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            delegate.paint(painter, option, index)
        finally:
            painter.end()

    def test_delegate_consumes_click_in_selection_mode(self, model):
        """A click on a tab row in selection mode emits selection_toggled."""
        model.set_selection_mode(True)
        delegate = tabtree.TabTreeItemDelegate()
        captured = []
        activated = []
        delegate.selection_toggled.connect(
            lambda idx: captured.append(idx))
        delegate.activated.connect(lambda idx: activated.append(idx))

        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 220, 22)
        index = model.index(2, 0, QModelIndex())
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(QPoint(60, 11)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        assert delegate.editorEvent(event, model, option, index) is True
        assert captured == [index]
        assert activated == []

    def test_delegate_shift_click_requests_range(self, model):
        """Shift+click in selection mode requests a range extension."""
        model.set_selection_mode(True)
        delegate = tabtree.TabTreeItemDelegate()
        ranges = []
        toggled = []
        delegate.selection_range_requested.connect(
            lambda idx: ranges.append(idx))
        delegate.selection_toggled.connect(lambda idx: toggled.append(idx))

        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 220, 22)
        index = model.index(2, 0, QModelIndex())
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(QPoint(60, 11)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
        )
        assert delegate.editorEvent(event, model, option, index) is True
        assert ranges == [index]
        assert toggled == []


class TestTabTree:

    """Tests for the host :class:`tabtree.TabTree` widget."""

    def test_instantiates_and_shows(self, qtbot):
        """The host widget can be shown without raising."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.show()
        assert tree.view() is not None
        assert tree.view().model() is not None

    def test_empty_workspace_shows_placeholder(self, qtbot):
        """An empty workspace displays the placeholder instead of the tree."""
        workspace = Workspace(id=uuid.uuid4(), name='empty')
        manager = MockManager(workspace)
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.show()
        # Stack page 0 is the tree, page 1 is the placeholder.
        assert tree._stack.currentIndex() == 1
        assert tree.view().model().rowCount(QModelIndex()) == 0

    def test_manager_changed_rebuilds(self, qtbot):
        """Emitting manager.changed refreshes the model."""
        workspace = Workspace(id=uuid.uuid4(), name='initial')
        manager = MockManager(workspace)
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        assert tree.view().model().rowCount(QModelIndex()) == 0

        manager._workspace = _make_workspace()
        manager.changed.emit()
        assert tree.view().model().rowCount(QModelIndex()) == 4

    def test_jk_shortcut_moves_selection(self, qtbot):
        """The J shortcut moves the selection and emits keyboard_activated."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.show()

        tree.view().setCurrentIndex(tree._model.index(0, 0))
        before = tree.view().currentIndex()
        assert before.row() == 0

        activated = []
        keyboard_activated = []
        tree.activated.connect(lambda idx: activated.append(idx))
        tree.keyboard_activated.connect(
            lambda idx: keyboard_activated.append(idx))

        j_shortcut = next(
            shortcut for shortcut in tree._shortcuts
            if shortcut.key() == QKeySequence('j'))
        j_shortcut.activated.emit()

        after = tree.view().currentIndex()
        assert after.isValid()
        # The group header is expanded, so the next visible row is its first
        # member (group members are visible; they were hidden before the
        # expansion sync, which is why this used to be row 1).
        assert after.row() == 0
        assert after.parent() == tree._model.index(0, 0)
        # J/K uses the dedicated signal so the bridge can keep focus in
        # the tree for iterative navigation; mouse clicks still hit
        # ``activated``.
        assert activated == []
        assert keyboard_activated == [after]

    def test_tree_emits_close_requested(self, qtbot):
        """The host forwards the delegate's close_requested signal."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)

        emitted = []
        tree.close_requested.connect(lambda idx: emitted.append(idx))
        idx = tree._model.index(1, 0)
        tree._delegate.close_requested.emit(idx)
        assert emitted == [idx]

    def test_tree_emits_pin_toggled(self, qtbot):
        """The host forwards the delegate's pin_toggled signal."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)

        emitted = []
        tree.pin_toggled.connect(lambda idx: emitted.append(idx))
        idx = tree._model.index(1, 0)
        tree._delegate.pin_toggled.emit(idx)
        assert emitted == [idx]

    def test_host_selection_mode_toggle(self, qtbot):
        """The host exposes set_selection_mode / selection_mode."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)

        assert tree.selection_mode() is False
        tree.set_selection_mode(True)
        assert tree.selection_mode() is True
        tree.set_selection_mode(False)
        assert tree.selection_mode() is False

    def test_host_selection_toggled_toggles_model(self, qtbot):
        """A delegate selection_toggled toggles the model and re-emits."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.set_selection_mode(True)

        emitted = []
        tree.selection_toggled.connect(lambda idx: emitted.append(idx))
        idx = tree._model.index(2, 0)  # standalone tab id 20

        tree._delegate.selection_toggled.emit(idx)
        assert tree.model().selected_tab_ids() == {20}
        assert emitted == [idx]

        tree._delegate.selection_toggled.emit(idx)
        assert tree.model().selected_tab_ids() == set()

    def test_selection_toggle_moves_current_row(self, qtbot):
        """Ctrl-click moves the current row without changing what is selected."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.set_selection_mode(True)

        # Start on a row the user did not click.
        tree.view().setCurrentIndex(tree._model.index(1, 0))
        target = tree._model.index(3, 0)  # standalone tab id 21

        tree._delegate.selection_toggled.emit(target)

        assert tree.view().currentIndex() == target
        assert tree.model().selected_tab_ids() == {21}

    def test_selection_toggle_anchor_supports_shift_range(self, qtbot):
        """A Ctrl-click anchor still drives a following Shift-click range."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.set_selection_mode(True)

        anchor = tree._model.index(2, 0)  # standalone tab id 20
        target = tree._model.index(3, 0)  # standalone tab id 21

        tree._delegate.selection_toggled.emit(anchor)
        tree._delegate.selection_range_requested.emit(target)

        assert tree.view().currentIndex() == target
        assert tree.model().selected_tab_ids() == {20, 21}

    def test_focus_group_and_tab_scrolls_and_highlights(self, qtbot):
        """The host scrolls to group + tab and highlights once."""
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        group_id = tree.model()._workspace.groups[0].group_id

        scrolled = []

        class FakeView:

            def scrollTo(self, index, hint=None):  # noqa: N802 - Qt naming
                scrolled.append((index, hint))

            def setExpanded(self, index, expanded):  # noqa: N802
                # Expansion sync (fold state -> view) is part of focusing.
                pass

        tree._view = FakeView()  # type: ignore[assignment]

        highlighted = []
        tree.model().highlight_tab = (
            lambda tab_id, **kwargs: highlighted.append(tab_id))

        tree.focus_group_and_tab(group_id, 30)

        assert len(scrolled) == 2
        assert highlighted == [30]
        assert tree.model().active_group_id() == group_id


class TestPinnedDrop:

    """Pinned-entry drag-and-drop into the tab tree (issue #34)."""

    @pytest.fixture
    def drop_setup(self, qtbot):
        workspace = _make_workspace()
        manager = MockManager(workspace)
        manager.add_pinned(PinnedTab(
            tab_id=99, title='Pinned', url='https://example.com/'))
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        return tree, manager, workspace

    @staticmethod
    def _mime(tab_id=99):
        mime = QMimeData()
        mime.setData(PINNED_TAB_MIME, str(tab_id).encode('ascii'))
        return mime

    def test_drop_into_group_unpins_and_sets_group(self, drop_setup):
        tree, manager, workspace = drop_setup
        group_index = tree.model().index(0, 0)

        assert tree.model().canDropMimeData(
            self._mime(), Qt.DropAction.CopyAction, -1, -1, group_index)
        assert tree.model().dropMimeData(
            self._mime(), Qt.DropAction.CopyAction, -1, -1, group_index)

        entry = next(tab for tab in workspace.tabs if tab.tab_id == 99)
        assert entry.group_id == workspace.groups[0].group_id
        assert entry.parent_id is None
        assert manager.list_pinned() == []

    def test_drop_on_group_tab_sets_parent(self, drop_setup):
        tree, _manager, workspace = drop_setup
        model = tree.model()
        # Index of group tab 30 (row 0 under the first group).
        group_index = model.index(0, 0)
        tab_index = model.index(0, 0, group_index)

        assert model.dropMimeData(
            self._mime(), Qt.DropAction.CopyAction, -1, -1, tab_index)

        entry = next(tab for tab in workspace.tabs if tab.tab_id == 99)
        assert entry.group_id == workspace.groups[0].group_id
        assert entry.parent_id == 30

    def test_drop_on_standalone_tab_is_top_level(self, drop_setup):
        tree, _manager, workspace = drop_setup
        model = tree.model()
        # Standalone tabs follow the parent chain at the root.
        standalone = next(
            model.index(row, 0) for row in range(model.rowCount())
            if model.tab_id_for_index(model.index(row, 0)) == 20
        )

        assert model.dropMimeData(
            self._mime(), Qt.DropAction.CopyAction, -1, -1, standalone)

        entry = next(tab for tab in workspace.tabs if tab.tab_id == 99)
        assert entry.group_id is None
        assert entry.parent_id is None

    def test_foreign_mime_is_rejected(self, drop_setup):
        tree, _manager, _workspace = drop_setup
        mime = QMimeData()
        mime.setText('nope')

        assert not tree.model().canDropMimeData(
            mime, Qt.DropAction.CopyAction, -1, -1, QModelIndex())

    def test_drop_without_live_tab_is_suspended(
            self, drop_setup, monkeypatch):
        """A pinned entry with no live tab must not become a fake OPEN row."""
        tree, _manager, workspace = drop_setup
        monkeypatch.setattr(
            workspacebridge, '_find_live_tab_by_url', lambda url: None)

        assert tree.model().dropMimeData(
            self._mime(), Qt.DropAction.CopyAction, -1, -1, QModelIndex())

        entry = next(tab for tab in workspace.tabs if tab.tab_id == 99)
        assert entry.open_status == 'SUSPENDED'

    def test_drop_with_live_tab_is_open(self, drop_setup, monkeypatch):
        """A pinned entry backed by a live tab stays OPEN after the drop."""
        tree, _manager, workspace = drop_setup
        monkeypatch.setattr(
            workspacebridge, '_find_live_tab_by_url',
            lambda url: (0, object(), object()))

        assert tree.model().dropMimeData(
            self._mime(), Qt.DropAction.CopyAction, -1, -1, QModelIndex())

        entry = next(tab for tab in workspace.tabs if tab.tab_id == 99)
        assert entry.open_status == 'OPEN'

    def test_drop_with_colliding_tab_id_uses_free_id(
            self, drop_setup, monkeypatch):
        """A drop must not put two workspace rows under the same tab id."""
        tree, _manager, workspace = drop_setup
        workspace.tabs.append(WorkspaceTab(
            tab_id=99, url='https://other.example/', open_status='OPEN'))
        monkeypatch.setattr(
            workspacebridge, '_find_live_tab_by_url', lambda url: None)

        assert tree.model().dropMimeData(
            self._mime(), Qt.DropAction.CopyAction, -1, -1, QModelIndex())

        tab_ids = [tab.tab_id for tab in workspace.tabs]
        assert len(tab_ids) == len(set(tab_ids))
        dropped = next(
            tab for tab in workspace.tabs
            if tab.url == 'https://example.com/')
        assert dropped.tab_id != 99

    def test_drop_target_role_highlights_row(self, drop_setup):
        tree, _manager, _workspace = drop_setup
        model = tree.model()
        group_index = model.index(0, 0)

        model.set_drop_target(group_index)

        assert model.data(group_index, model.DropTargetRole) is True
        model.set_drop_target(QModelIndex())
        assert model.data(group_index, model.DropTargetRole) is False


class TestSelectionPreservation:

    def test_current_tab_survives_manager_rebuild(self, qtbot):
        workspace = _make_workspace()
        manager = MockManager(workspace)
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.show()

        child_index = tree.model().index_for_tab_id(11)
        tree.view().setCurrentIndex(child_index)

        # A workspace switch rebuilds the model (manager.changed).  Tab 11
        # still exists in the new workspace, so the selection is valid.
        manager._workspace = _make_workspace()
        manager.changed.emit()

        current = tree.view().currentIndex()
        assert tree.model().tab_id_for_index(current) == 11


class TestTabTreeSearch:

    """Live in-panel search (VP-P5)."""

    def _workspace(self):
        group = Group(group_id=uuid.uuid4(), name='Research')
        other = Group(group_id=uuid.uuid4(), name='Misc')
        return Workspace(
            id=uuid.uuid4(),
            name='search',
            groups=[group, other],
            tabs=[
                WorkspaceTab(
                    tab_id=1, group_id=group.group_id,
                    title='Alpha docs', open_status='OPEN'),
                WorkspaceTab(
                    tab_id=2, group_id=group.group_id, title='Beta'),
                WorkspaceTab(
                    tab_id=3, group_id=other.group_id,
                    title='Alpha notes', open_status='SUSPENDED'),
                WorkspaceTab(tab_id=4, title='Standalone Alpha'),
            ],
        )

    def _tree(self, qtbot):
        tree = tabtree.TabTree(win_id=0, manager=MockManager(self._workspace()))
        qtbot.add_widget(tree)
        return tree

    def test_search_input_is_first_child(self, qtbot):
        tree = self._tree(qtbot)
        assert tree.layout().indexOf(tree.search_input) == 0

    def test_search_filters_live_and_hides_empty_groups(self, qtbot):
        tree = self._tree(qtbot)
        tree.search_input.setText('alpha')
        model = tree.model()
        assert model.search_active

        root = QModelIndex()
        root_kinds = [
            model.index(row, 0).data(tabtree.KindRole)
            for row in range(model.rowCount(root))
        ]
        assert root_kinds == [
            tabtree.Kind.Group, tabtree.Kind.Group, tabtree.Kind.Tab]
        grouped_ids = {
            model.tab_id_for_index(model.index(row, 0, model.index(0, 0)))
            for row in range(model.rowCount(model.index(0, 0)))
        }
        assert grouped_ids == {3}
        research_ids = {
            model.tab_id_for_index(model.index(row, 0, model.index(1, 0)))
            for row in range(model.rowCount(model.index(1, 0)))
        }
        assert research_ids == {1}

    def test_clearing_search_restores_default_view(self, qtbot):
        tree = self._tree(qtbot)
        before = tree.model().rowCount(QModelIndex())
        tree.search_input.setText('alpha')
        tree.search_input.setText('')
        assert not tree.model().search_active
        assert tree.model().rowCount(QModelIndex()) == before

    def test_no_matches_shows_placeholder(self, qtbot):
        tree = self._tree(qtbot)
        tree.search_input.setText('does-not-exist')
        assert tree._stack.currentIndex() == 1
        assert tree._placeholder.text() == 'No matching tabs'

    def test_search_result_keeps_open_status_role(self, qtbot):
        tree = self._tree(qtbot)
        tree.search_input.setText('notes')
        model = tree.model()
        group_index = model.index(0, 0)
        tab_index = model.index(0, 0, group_index)
        assert tab_index.data(tabtree.OpenStatusRole) == 'SUSPENDED'


class _SignalSource(QObject):

    """Emits one zero-argument, one one-argument and one two-argument signal."""

    zero = pyqtSignal()
    one = pyqtSignal(bool)
    two = pyqtSignal(str, str)


class _PlainOwner:

    """Plain (non-QObject) owner, so tests can drop its last reference."""

    def __init__(self) -> None:
        self.calls = []

    def record(self, *args) -> None:
        self.calls.append(args)


class _TriggeringMenu(QMenu):

    """QMenu stand-in whose ``exec`` triggers every checkable action."""

    def exec(self, *args, **kwargs):  # noqa: A003 - Qt naming
        for action in self.actions():
            if action.isCheckable():
                action.trigger()
        return 0


class TestWeakSlot:

    """Regression for #46: per-object signal bindings must never raise.

    ``lambda tid=...`` / ``functools.partial`` bindings closed a reference
    cycle through the Qt-held connection; the cycle GC then cleared the
    callable, and the next delivery raised ``TypeError: () missing 1
    required positional argument: 'tid'`` (or segfaulted for partials).
    """

    def test_zero_arg_signal_calls_method_with_bound_args(self, qtbot):
        """A zero-argument signal delivers only the bound arguments."""
        source = _SignalSource()
        owner = _PlainOwner()
        source.zero.connect(weak_slot(owner.record, 7))
        with qtbot.captureExceptions() as excs:
            source.zero.emit()
        assert excs == []
        assert owner.calls == [(7,)]

    def test_one_arg_signal_appends_signal_argument(self, qtbot):
        """The signal's own argument is appended after the bound ones."""
        source = _SignalSource()
        owner = _PlainOwner()
        source.one.connect(weak_slot(owner.record, 7))
        with qtbot.captureExceptions() as excs:
            source.one.emit(True)
        assert excs == []
        assert owner.calls == [(7, True)]

    def test_accepts_caps_forwarded_signal_arguments(self, qtbot):
        """``accepts`` drops the arguments a handler does not want.

        Qt truncates a signal's arguments to the connected callable's arity.
        The returned slot is variadic, so Qt cannot introspect it and forwards
        everything -- a handler that ignores the signal's arguments (every
        ``QAction.triggered`` handler, since it emits a ``bool``) has to say so
        with ``accepts=0``, or it is handed the ``bool`` it never declared.
        """
        source = _SignalSource()
        owner = _PlainOwner()
        source.one.connect(weak_slot(owner.record, 7, accepts=0))
        with qtbot.captureExceptions() as excs:
            source.one.emit(True)
        assert excs == []
        assert owner.calls == [(7,)]

    def test_accepts_forwards_only_the_first_n_arguments(self, qtbot):
        source = _SignalSource()
        owner = _PlainOwner()
        source.two.connect(weak_slot(owner.record, 7, accepts=1))
        with qtbot.captureExceptions() as excs:
            source.two.emit('a', 'b')
        assert excs == []
        assert owner.calls == [(7, 'a')]

    def test_dead_owner_is_a_noop(self, qtbot):
        """Once the owner is gone the still-connected slot does nothing."""
        source = _SignalSource()
        owner = _PlainOwner()
        source.zero.connect(weak_slot(owner.record, 7))
        del owner
        gc.collect()
        with qtbot.captureExceptions() as excs:
            source.zero.emit()
        assert excs == []

    def test_non_method_callable_is_bound_with_partial(self, qtbot):
        """Non-method callables (e.g. test doubles) still get bound args."""
        source = _SignalSource()
        received = []
        source.zero.connect(weak_slot(received.append, 'x'))
        with qtbot.captureExceptions() as excs:
            source.zero.emit()
        assert excs == []
        assert received == ['x']

    def test_slot_does_not_keep_owner_alive(self, qtbot):
        """A Qt-held slot must not pin its owner (the root of #46).

        A strong ``lambda``/``partial`` made the connection a reference path
        to the owner, closing a cycle the GC could clear under Qt.  Holding
        the owner weakly keeps the owner collectable.
        """
        source = _SignalSource()
        owner = _PlainOwner()
        owner_ref = weakref.ref(owner)
        source.zero.connect(weak_slot(owner.record, 7))
        del owner
        gc.collect()
        assert owner_ref() is None

    def test_highlight_timer_clears_highlight(self, qtbot):
        """The zero-argument timeout clears the highlight without raising."""
        manager = MockManager(_make_workspace())
        model = tabtree.TabTreeModel(manager, parent=None)
        with qtbot.captureExceptions() as excs:
            model.set_highlighted_tab_id(20, timeout_ms=1)
            qtbot.waitUntil(lambda: model._highlighted_tab_id is None)
        assert excs == []

    def test_shortcut_activation_moves_selection(self, qtbot, monkeypatch):
        """The zero-argument ``j``/``k`` shortcuts call _move_selection."""
        deltas = []
        monkeypatch.setattr(
            tabtree.TabTree, '_move_selection',
            lambda self, delta: deltas.append(delta))
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        with qtbot.captureExceptions() as excs:
            tree._shortcuts[0].activated.emit()
            tree._shortcuts[1].activated.emit()
        assert excs == []
        assert deltas == [1, -1]

    def test_affinity_menu_action_passes_checked_state(
            self, qtbot, monkeypatch):
        """``triggered(bool)`` reaches _set_group_affinity as ``enabled``."""
        calls = []
        monkeypatch.setattr(
            tabtree.TabTree, '_set_group_affinity',
            lambda self, group, enabled: calls.append((group, enabled)))
        monkeypatch.setattr(tabtree, 'QMenu', _TriggeringMenu)
        manager = MockManager(_make_workspace())
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        group = tree.model()._workspace.groups[0]
        monkeypatch.setattr(
            tree.model(), 'group_for_index', lambda index: group)
        with qtbot.captureExceptions() as excs:
            tree._on_context_menu_requested(QPoint(0, 0))
        assert excs == []
        assert calls == [(group, not group.affinity_enabled)]


class TestTabNodeDrag:

    """Dragging a tab node out of one workspace onto another (ticket #6).

    The tree shows the active workspace, so these tests make the *active*
    workspace the drop target and drag a node that lives in the other one.
    """

    @pytest.fixture
    def setup(self, qtbot, tmp_path, monkeypatch):
        from qutebrowser.workspaces.manager import WorkspaceManager
        from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                        WorkspacePersistence)
        manager = WorkspaceManager(
            WorkspacePersistence(base_path=tmp_path / 'ws'),
            PinnedPersistence(path=tmp_path / 'pinned.yml'))
        source = manager.add_workspace('source')
        target = manager.add_workspace('target')
        manager.set_active(target.id)
        group = manager.create_group(target.id, 'G')
        # target: 10 (root) -> 11 child; 20 standalone; 30 in group G.
        manager.add_tab(target.id, WorkspaceTab(tab_id=10, open_status='OPEN'))
        manager.add_tab(target.id, WorkspaceTab(
            tab_id=11, parent_id=10, open_status='OPEN'))
        manager.add_tab(target.id, WorkspaceTab(
            tab_id=20, open_status='OPEN'))
        manager.add_tab(target.id, WorkspaceTab(
            tab_id=30, group_id=group.group_id, open_status='OPEN'))
        # source: 1 (root) -> 2 child, 3 standalone.
        manager.add_tab(source.id, WorkspaceTab(tab_id=1, open_status='OPEN'))
        manager.add_tab(source.id, WorkspaceTab(
            tab_id=2, parent_id=1, open_status='OPEN'))
        manager.add_tab(source.id, WorkspaceTab(
            tab_id=3, open_status='OPEN'))
        tree = tabtree.TabTree(win_id=0, manager=manager)
        qtbot.add_widget(tree)
        tree.resize(300, 600)
        tree.show()
        # Child rows only have a geometry (and so a hit area) when expanded.
        tree.view().expandAll()
        messages = []
        monkeypatch.setattr(
            tabtree.message, 'error', lambda text, **_kw: messages.append(text))
        return SimpleNamespace(
            tree=tree, manager=manager, source=source, target=target,
            group=group, messages=messages)

    @staticmethod
    def _node_mime(workspace_id, tab_id, *, branch=False):
        mime = QMimeData()
        mime.setData(tabtree.TAB_NODE_MIME, tabtree.encode_tab_node(
            workspace_id, tab_id, branch=branch))
        return mime

    @staticmethod
    def _index_for(model, tab_id=None, group_id=None):
        """Return the root-level or group-child index for an id."""
        if tab_id is not None:
            index = model.index_for_tab_id(tab_id)
            assert index.isValid(), tab_id
            return index
        for row in range(model.rowCount()):
            idx = model.index(row, 0)
            group = model.group_for_index(idx)
            if group is not None and group.group_id == group_id:
                return idx
        raise AssertionError('group row not found')

    @staticmethod
    def _tab_ids(workspace):
        return {tab.tab_id: tab.parent_id for tab in workspace.tabs}

    # --- model: mime, flags, drag source ---

    def test_tab_rows_are_drag_sources_and_group_rows_are_not(self, setup):
        model = setup.tree.model()
        assert tabtree.TAB_NODE_MIME in model.mimeTypes()
        tab_index = model.index_for_tab_id(20)
        assert model.flags(tab_index) & Qt.ItemFlag.ItemIsDragEnabled
        group_index = self._index_for(model, group_id=setup.group.group_id)
        assert not (model.flags(group_index)
                    & Qt.ItemFlag.ItemIsDragEnabled)

    def test_mime_data_encodes_workspace_and_tab(self, setup):
        model = setup.tree.model()
        mime = model.mimeData([model.index_for_tab_id(20)])
        assert mime.hasFormat(tabtree.TAB_NODE_MIME)
        raw = bytes(mime.data(tabtree.TAB_NODE_MIME)).decode('ascii')
        assert raw == f'{setup.target.id}:20:leaf'

    def test_mime_data_encodes_branch_for_node_with_descendants(self, setup):
        model = setup.tree.model()
        # target: 10 has child 11, so 10 is a branch; 20 is a leaf.
        branch = model.mimeData([model.index_for_tab_id(10)])
        leaf = model.mimeData([model.index_for_tab_id(20)])
        assert bytes(branch.data(tabtree.TAB_NODE_MIME)).decode('ascii') == (
            f'{setup.target.id}:10:branch')
        assert bytes(leaf.data(tabtree.TAB_NODE_MIME)).decode('ascii') == (
            f'{setup.target.id}:20:leaf')

    @pytest.mark.parametrize('branch', [True, False])
    def test_payload_round_trips_through_mime(self, branch):
        ws_id = uuid.uuid4()
        mime = QMimeData()
        mime.setData(tabtree.TAB_NODE_MIME, tabtree.encode_tab_node(
            ws_id, 42, branch=branch))
        ref = tabtree.decode_tab_node(mime)
        assert ref == tabtree.TabNodeRef(ws_id, 42, branch)

    @pytest.mark.parametrize('payload', [
        b'',
        b'not-a-payload',
        b'%s:7' % str(uuid.uuid4()).encode('ascii'),          # no kind
        b'%s:7:sideways' % str(uuid.uuid4()).encode('ascii'),  # bad kind
        b'%s:-1:leaf' % str(uuid.uuid4()).encode('ascii'),     # negative id
        b'%s:x:leaf' % str(uuid.uuid4()).encode('ascii'),      # non-numeric
        b'nope:7:leaf',                                        # bad uuid
        b'%s:7:leaf:extra' % str(uuid.uuid4()).encode('ascii'),
        b'\xff\xfe:7:leaf',                                    # not ascii
    ])
    def test_decode_rejects_malformed_payload(self, payload):
        mime = QMimeData()
        mime.setData(tabtree.TAB_NODE_MIME, payload)
        assert tabtree.decode_tab_node(mime) is None

    def test_decode_ignores_other_mime(self):
        mime = QMimeData()
        mime.setData(PINNED_TAB_MIME, b'1')
        assert tabtree.decode_tab_node(mime) is None

    def test_mime_data_refuses_group_row_and_multi_select(self, setup):
        model = setup.tree.model()
        group_index = self._index_for(model, group_id=setup.group.group_id)
        assert model.mimeData([group_index]) is None
        tab_index = model.index_for_tab_id(20)
        assert model.mimeData([tab_index, model.index_for_tab_id(30)]) is None

    # --- model: drop targets ---

    def test_drop_on_group_row_moves_into_group(self, setup):
        model = setup.tree.model()
        group_index = self._index_for(model, group_id=setup.group.group_id)
        mime = self._node_mime(setup.source.id, 3)

        assert model.canDropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, group_index)
        assert model.dropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, group_index)

        assert 3 not in self._tab_ids(setup.source)
        moved = setup.manager.find_tab_by_id(setup.target.id, 3)
        assert moved.group_id == setup.group.group_id
        assert moved.parent_id is None

    def test_drop_on_tab_row_targets_that_tabs_parent(self, setup):
        """A tab-row drop becomes a sibling: same parent and group as the row."""
        model = setup.tree.model()
        child_row = model.index_for_tab_id(11)  # child of 10, standalone group
        mime = self._node_mime(setup.source.id, 3)

        assert model.dropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, child_row)

        moved = setup.manager.find_tab_by_id(setup.target.id, 3)
        assert moved.parent_id == 10
        assert moved.group_id is None

    def test_drop_on_grouped_tab_row_inherits_group(self, setup):
        model = setup.tree.model()
        grouped_row = model.index_for_tab_id(30)
        mime = self._node_mime(setup.source.id, 3)

        assert model.dropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, grouped_row)

        moved = setup.manager.find_tab_by_id(setup.target.id, 3)
        assert moved.group_id == setup.group.group_id
        assert moved.parent_id is None

    def test_drop_on_root_is_top_level(self, setup):
        model = setup.tree.model()
        mime = self._node_mime(setup.source.id, 3)

        assert model.dropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, QModelIndex())

        moved = setup.manager.find_tab_by_id(setup.target.id, 3)
        assert moved.parent_id is None
        assert moved.group_id is None

    def test_dropping_a_parent_moves_its_branch(self, setup):
        model = setup.tree.model()
        mime = self._node_mime(setup.source.id, 1, branch=True)  # 1 -> 2

        assert model.dropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, QModelIndex())

        assert self._tab_ids(setup.source) == {3: None}
        assert self._tab_ids(setup.target) == {
            10: None, 11: 10, 20: None, 30: None, 1: None, 2: 1}

    def test_same_workspace_drop_is_rejected_silently(self, setup):
        model = setup.tree.model()
        own = self._node_mime(setup.target.id, 20)
        before = self._tab_ids(setup.target)
        group_index = self._index_for(model, group_id=setup.group.group_id)

        assert not model.canDropMimeData(
            own, Qt.DropAction.MoveAction, -1, -1, group_index)
        assert not model.dropMimeData(
            own, Qt.DropAction.MoveAction, -1, -1, group_index)
        assert self._tab_ids(setup.target) == before
        assert setup.messages == []

    def test_copy_action_is_rejected_for_node_drags(self, setup):
        model = setup.tree.model()
        mime = self._node_mime(setup.source.id, 3)
        assert not model.canDropMimeData(
            mime, Qt.DropAction.CopyAction, -1, -1, QModelIndex())

    def test_unknown_source_tab_is_rejected(self, setup):
        model = setup.tree.model()
        mime = self._node_mime(setup.source.id, 999)
        assert not model.canDropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, QModelIndex())

    def test_malformed_payload_is_rejected(self, setup):
        model = setup.tree.model()
        mime = QMimeData()
        mime.setData(tabtree.TAB_NODE_MIME, b'not-a-payload')
        assert not model.canDropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, QModelIndex())

    def test_archived_target_group_is_rejected(self, setup):
        model = setup.tree.model()
        setup.group.archived = True
        group_index = self._index_for(model, group_id=setup.group.group_id)
        mime = self._node_mime(setup.source.id, 3)
        assert not model.canDropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, group_index)

    def test_manager_refusal_is_reported_and_not_accepted(self, setup):
        model = setup.tree.model()
        mime = self._node_mime(setup.source.id, 3)

        def refuse(*_args, **_kwargs):
            raise WorkspaceMoveError('refused in test')
        # The cheap checks pass; the manager has the final say and refuses.
        setup.manager.move_tab_to_workspace = refuse

        assert not model.dropMimeData(
            mime, Qt.DropAction.MoveAction, -1, -1, QModelIndex())
        assert setup.messages == ['refused in test']

    # --- view: hover accept/reject and the real Qt drop path ---

    @classmethod
    def _hover(cls, view, pos, mime, action):
        """Send a realistic drag sequence: enter, then move over *pos*."""
        cls._send(view, QDragEnterEvent, pos, mime, action)
        return cls._send(view, QDragMoveEvent, pos, mime, action)

    @staticmethod
    def _send(view, event_cls, pos, mime, action):
        # PyQt6 takes a QPoint for the enter/move events, a QPointF for drops.
        where = QPointF(pos) if event_cls is QDropEvent else QPoint(pos)
        event = event_cls(
            where, action, mime, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), event)
        return event

    def test_hover_accepts_group_and_highlights(self, setup, qtbot):
        view = setup.tree.view()
        model = setup.tree.model()
        group_index = self._index_for(model, group_id=setup.group.group_id)
        pos = view.visualRect(group_index).center()
        mime = self._node_mime(setup.source.id, 3)

        event = self._hover(view, pos, mime, Qt.DropAction.MoveAction)

        assert event.isAccepted()
        assert model.data(group_index, model.DropTargetRole) is True

    def test_hover_rejects_own_workspace_without_highlight(self, setup, qtbot):
        view = setup.tree.view()
        model = setup.tree.model()
        row = model.index_for_tab_id(20)
        pos = view.visualRect(row).center()
        own = self._node_mime(setup.target.id, 20)

        event = self._hover(view, pos, own, Qt.DropAction.MoveAction)

        assert not event.isAccepted()
        assert model.data(row, model.DropTargetRole) is False

    def test_real_drop_event_moves_the_node(self, setup, qtbot):
        view = setup.tree.view()
        model = setup.tree.model()
        row = model.index_for_tab_id(11)
        pos = view.visualRect(row).center()
        mime = self._node_mime(setup.source.id, 3)

        self._send(view, QDragEnterEvent, pos, mime, Qt.DropAction.MoveAction)
        event = self._send(view, QDropEvent, pos, mime,
                           Qt.DropAction.MoveAction)

        assert event.isAccepted()
        assert 3 not in self._tab_ids(setup.source)
        moved = setup.manager.find_tab_by_id(setup.target.id, 3)
        assert moved.parent_id == 10
        # The highlight is cleared once the drop is handled.
        assert model.data(row, model.DropTargetRole) is False

    def test_drag_enter_accepts_node_mime(self, setup, qtbot):
        view = setup.tree.view()
        pos = view.visualRect(setup.tree.model().index_for_tab_id(20)).center()
        mime = self._node_mime(setup.source.id, 3)
        event = self._send(view, QDragEnterEvent, pos, mime,
                           Qt.DropAction.MoveAction)
        assert event.isAccepted()
