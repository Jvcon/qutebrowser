# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the vertical panel's TabTree (Phase 2, slice 1)."""

import uuid

import pytest

from qutebrowser.qt.core import (
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    pyqtSignal,
)
from qutebrowser.qt.gui import (
    QHelpEvent,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from qutebrowser.qt.widgets import (
    QStyle,
    QStyleOptionViewItem,
    QTreeView,
)

from qutebrowser.mainwindow import tabtree
from qutebrowser.workspaces.model import Group, Workspace, WorkspaceTab


class MockManager(QObject):

    """Minimal stand-in for ``WorkspaceManager`` in TabTree tests."""

    changed = pyqtSignal()

    def __init__(self, workspace=None):
        super().__init__()
        self._workspace = workspace

    def get_active(self):
        return self._workspace

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
        monkeypatch.setattr(tabtree, 'QTimer', _FakeQTimer())
        model.set_highlighted_tab_id(20)
        index = model.index(2, 0, QModelIndex())  # standalone tab id 20
        assert index.data(tabtree.HighlightRole) is True
        assert model.index(3, 0, QModelIndex()).data(
            tabtree.HighlightRole) is False

    def test_highlight_resets_after_timeout(self, model, monkeypatch):
        """The scheduled callback clears the highlight."""
        calls = []

        class FakeQTimer:
            @staticmethod
            def singleShot(ms, callback):  # noqa: N802 - Qt naming
                calls.append((ms, callback))

        monkeypatch.setattr(tabtree, 'QTimer', FakeQTimer)
        model.set_highlighted_tab_id(20, timeout_ms=1234)

        assert len(calls) == 1
        assert calls[0][0] == 1234
        assert model.index(2, 0, QModelIndex()).data(
            tabtree.HighlightRole) is True

        calls[0][1]()  # fire the scheduled reset

        assert model.index(2, 0, QModelIndex()).data(
            tabtree.HighlightRole) is False

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


class _FakeQTimer:

    """No-op QTimer stand-in so scheduled resets never fire in tests."""

    @staticmethod
    def singleShot(ms, callback):  # noqa: N802 - Qt naming
        return None


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
        monkeypatch.setattr(tabtree, 'QTimer', _FakeQTimer())
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
        assert after.row() == 1
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

        tree._view = FakeView()  # type: ignore[assignment]

        highlighted = []
        tree.model().highlight_tab = (
            lambda tab_id, **kwargs: highlighted.append(tab_id))

        tree.focus_group_and_tab(group_id, 30)

        assert len(scrolled) == 2
        assert highlighted == [30]
        assert tree.model().active_group_id() == group_id
