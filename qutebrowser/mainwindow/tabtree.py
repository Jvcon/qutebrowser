# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The vertical panel's active-workspace tab tree (Phase 2, slice 1).

Renders the active workspace's tabs as a compact, nestable ``QTreeView``.
Groups appear as foldable section headers; standalone tabs form a flat list
at the root. Parent-child tab chains are nested under their parent tab, but
only within the same group (or both standalone) so the group container and
the parent-child nesting are displayed simultaneously.

The module follows the same composition pattern as the PIN panel:
:class:`TabTreeModel` backs a ``QTreeView`` and :class:`TabTreeItemDelegate`
draws the rows. The host :class:`TabTree` widget wires them together and
refreshes when the workspace manager emits ``changed``.

Out of scope for this slice (see ADR 0008 § "3. TabTree"):

- Drag-and-drop reparenting.
- Active-workspace switch animation.
- Search & filter overlay.
- Live favicon updates.
- Right-click context menus.

Tab activation, the J/K keyboard selection and the hover close/audio
affordances are wired through :class:`~qutebrowser.mainwindow.workspacebridge.WorkspaceBridge`
(see the "TabTree as a superset of TabBar" slice). ``TabbedBrowser`` remains
the source of truth: the tree only ever *requests* a switch and reflects the
resulting ``current_tab_changed`` back through the bridge.

These remaining items are tracked for the next sub-slice and marked with
``TODO(v0.2)`` comments where appropriate.
"""

from __future__ import annotations

import base64
import enum
import uuid
from collections.abc import Iterable
from typing import Optional

from qutebrowser.qt.core import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from qutebrowser.qt.gui import (
    QColor,
    QFontMetrics,
    QHelpEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from qutebrowser.qt.widgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QMenu,
    QStackedWidget,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from qutebrowser.mainwindow.pinpanel import PINNED_TAB_MIME
from qutebrowser.workspaces import search
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)


# Role ids shared between model and delegate.
KindRole = Qt.ItemDataRole.UserRole + 1
FoldedRole = Qt.ItemDataRole.UserRole + 2
StatusRole = Qt.ItemDataRole.UserRole + 3
BadgeRole = Qt.ItemDataRole.UserRole + 4
UrlRole = Qt.ItemDataRole.UserRole + 5
ActiveRole = Qt.ItemDataRole.UserRole + 6
PinStateRole = Qt.ItemDataRole.UserRole + 7
OpenStatusRole = Qt.ItemDataRole.UserRole + 8
SelectionRole = Qt.ItemDataRole.UserRole + 9
GroupStateRole = Qt.ItemDataRole.UserRole + 10
ActiveGroupRole = Qt.ItemDataRole.UserRole + 11
HighlightRole = Qt.ItemDataRole.UserRole + 12
GroupIdRole = Qt.ItemDataRole.UserRole + 13
DropTargetRole = Qt.ItemDataRole.UserRole + 14


class Kind(enum.IntEnum):
    """Row kind exposed via :attr:`KindRole`."""

    Group = 1
    Tab = 2
    ParentTab = 3


class Status(enum.IntFlag):
    """Tab status flags exposed via :attr:`StatusRole`."""

    NONE = 0
    LOADING = 1
    AUDIBLE = 2
    PINNED = 4
    SUSPENDED = 8


#: Constant group-row role values (everything not derived from live state).
_GROUP_STATIC_ROLES = {
    KindRole: Kind.Group,
    StatusRole: Status.NONE,
    OpenStatusRole: None,
    ActiveRole: False,
    SelectionRole: False,
    HighlightRole: False,
}

#: Constant tab-row role values (everything not derived from live state).
_TAB_STATIC_ROLES = {
    BadgeRole: None,
    ActiveGroupRole: False,
}


@enum.unique
class _Icon(enum.Enum):
    """Status-strip glyphs."""

    LOADING = '↻'
    AUDIBLE = '♪'
    PINNED = 'P'
    SUSPENDED = 'zZ'


class _TabInfo:

    """Runtime display data for a workspace tab.

    ``WorkspaceTab`` only stores structural ids; titles, URLs, favicons and
    status flags are supplied out-of-band until the next slice integrates
    with ``TabbedBrowser``.
    """

    def __init__(self) -> None:
        self.title: str = ''
        self.url: str = ''
        self.favicon: Optional[bytes] = None
        self.status: Status = Status.NONE


class _Node:

    """Internal tree node used as a ``QModelIndex`` internal pointer."""

    def __init__(
            self,
            kind: Kind,
            obj: Group | WorkspaceTab | None,
            parent: Optional[_Node] = None,
    ) -> None:
        self.kind = kind
        self.obj = obj
        self.parent = parent
        self.children: list[_Node] = []
        self.row = 0


class TabTreeModel(QAbstractItemModel):

    """Tree model for the active workspace's groups and tabs.

    The model flattens a workspace into a two-level tree: group headers and
    standalone tabs sit at the root; group members and parent-tab children are
    nested one level below (with further parent-child nesting allowed within
    the same container).

    Attributes:
        KindRole: Role id for the row :class:`Kind`.
        FoldedRole: Role id for the collapsed state (bool).
        StatusRole: Role id for tab :class:`Status` flags.
        BadgeRole: Role id for the group ``"open/total"`` tab-count badge
            (str), e.g. ``"2/5"``.  ``"0/0"`` for an empty group.
        UrlRole: Role id for the tab URL (str).
        ActiveRole: Role id for whether the row is the active tab (bool).
        PinStateRole: Role id for whether the tab row is pinned (bool).
        OpenStatusRole: Role id for the entry's OPEN/SUSPENDED state (str).
        SelectionRole: Role id for whether the tab row is multi-selected
            (bool).  Group rows always report ``False``.
        GroupStateRole: Role id for the resolved group running state, one
            of ``'OPEN'``, ``'SUSPENDED'`` or ``'ARCHIVED'`` (str).  Group
            rows only; tab rows report ``None``.
        ActiveGroupRole: Role id for whether the row belongs to the
            auto-located group (bool).  Only the matching group header row
            reports ``True``; every other row reports ``False``.
        HighlightRole: Role id for the transient auto-locate highlight
            (bool).  ``True`` on the single tab row highlighted after a
            browser tab switch; every other row reports ``False``.
        GroupIdRole: Role id for the row's owning group id (``uuid.UUID``
            or ``None``).  Used by the pinned-tab drop handler.
        DropTargetRole: Role id for the row currently highlighted as a
            valid pinned-tab drop target (bool).
    """

    KindRole = KindRole
    FoldedRole = FoldedRole
    StatusRole = StatusRole
    BadgeRole = BadgeRole
    UrlRole = UrlRole
    ActiveRole = ActiveRole
    PinStateRole = PinStateRole
    OpenStatusRole = OpenStatusRole
    SelectionRole = SelectionRole
    GroupStateRole = GroupStateRole
    ActiveGroupRole = ActiveGroupRole
    HighlightRole = HighlightRole
    GroupIdRole = GroupIdRole
    DropTargetRole = DropTargetRole

    def __init__(
            self,
            manager: WorkspaceManager,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._workspace: Optional[Workspace] = None
        self._root = _Node(Kind.Group, None)
        self._tab_nodes: dict[int, _Node] = {}
        self._tab_info: dict[int, _TabInfo] = {}
        self._folded_state: dict[int, bool] = {}
        self._active_tab_id: Optional[int] = None
        # Auto-locate (VP-D5): the group header currently accented and the
        # tab row carrying the transient highlight.
        self._active_group_id: Optional[uuid.UUID] = None
        self._highlighted_tab_id: Optional[int] = None
        self._highlight_timer: Optional[QTimer] = None
        self._show_archived = False
# VP-P5: when ``_search_hits`` is not None the tree renders only the
        # matching entries, aggregated by group.  ``None`` means the normal,
        # unfiltered view.
        self._search_hits: Optional[list[search.SearchHit]] = None
        # Multi-select (VP-F4).  The ids survive ``set_workspace`` rebuilds
        # so a batch command that emits ``changed`` does not wipe the
        # selection mid-operation.
        self._selected: set[int] = set()
        self._selection_mode: bool = False
        self._filter_text = ''
        # Pinned-tab drag-and-drop: ``(kind, key)`` of the highlighted row,
        # where key is a tab id or group id.  ``None`` when no valid target
        # is under the cursor.
        self._drop_target: Optional[tuple[str, object]] = None
        self._placeholder_pixmap = self._create_placeholder_pixmap()
        self.set_workspace(manager.get_active())

    def set_workspace(self, workspace: Workspace | None) -> None:
        """Replace the model contents with *workspace* and reset views."""
        self.beginResetModel()
        self._workspace = workspace
        self._search_hits = None
        self._drop_target = None
        self._root.children.clear()
        self._tab_nodes.clear()
        if workspace is not None:
            self._build_tree(workspace)
        self.endResetModel()

    def set_search_hits(
            self,
            hits: Optional[list[search.SearchHit]],
    ) -> None:
        """Render only *hits*, or the normal view when *hits* is ``None``.

        Hits are expected to be pre-sorted by relevance (see
        :func:`qutebrowser.workspaces.search.search_workspace`); the model
        keeps that order inside each group while creating one group header
        per group that contains at least one hit.
        """
        self.beginResetModel()
        self._search_hits = None if hits is None else list(hits)
        self._drop_target = None
        self._root.children.clear()
        self._tab_nodes.clear()
        if self._workspace is not None:
            if self._search_hits is None:
                self._build_tree(self._workspace)
            else:
                self._build_search_tree(self._search_hits)
        self.endResetModel()

    @property
    def search_active(self) -> bool:
        """Return whether the model is showing filtered search results."""
        return self._search_hits is not None

    def set_show_archived(self, show: bool) -> None:
        """Show or hide archived groups in the tree.

        Archived groups are hidden by default. The archive view flips this
        on via :meth:`set_show_archived` when the user opens it.
        """
        if show == self._show_archived:
            return
        self._show_archived = show
        self.set_workspace(self._workspace)

    def set_filter(self, text: str) -> None:
        """Filter rows to those whose title/URL contains *text*.

        The filter is a case-insensitive substring match on the tab's
        persisted and runtime title/URL. Matching tabs and their ancestor
        chain (parent tabs, groups) stay visible so matches remain
        reachable. An empty *text* clears the filter.
        """
        text = text.strip()
        if text == self._filter_text:
            return
        self._filter_text = text
        self.set_workspace(self._workspace)

    def set_tab_info(
            self,
            tab_id: int,
            title: Optional[str] = None,
            url: Optional[str] = None,
            favicon: Optional[bytes] = None,
            status: Optional[Status] = None,
    ) -> None:
        """Set display data for *tab_id* and refresh its row.

        This is a first-slice helper for tests and for the future integration
        with ``TabbedBrowser``.
        """
        info = self._tab_info.setdefault(tab_id, _TabInfo())
        if title is not None:
            info.title = title
        if url is not None:
            info.url = url
        if favicon is not None:
            info.favicon = favicon
        if status is not None:
            info.status = status

        node = self._tab_nodes.get(tab_id)
        if node is not None:
            idx = self._index_for_node(node)
            if idx.isValid():
                self.dataChanged.emit(idx, idx)

    def clear_tab_info(self, tab_id: int) -> None:
        """Drop any cached display data for *tab_id* (e.g. after removal).

        This is the inverse of :meth:`set_tab_info`.  It is safe to call for
        an unknown *tab_id*.
        """
        self._tab_info.pop(tab_id, None)

    def set_tab_pinned(self, tab_id: int, pinned: bool) -> None:
        """Update the PINNED status flag for *tab_id* and refresh its row.

        :data:`PinStateRole` is derived from :class:`Status.PINNED`, so the
        model only has to flip that bit and emit a targeted ``dataChanged``
        carrying the role.  Safe to call for an unknown *tab_id*.
        """
        info = self._tab_info.setdefault(tab_id, _TabInfo())
        if pinned:
            info.status |= Status.PINNED
        else:
            info.status &= ~Status.PINNED

        node = self._tab_nodes.get(tab_id)
        if node is None:
            return
        idx = self._index_for_node(node)
        if idx.isValid():
            self.dataChanged.emit(idx, idx, [PinStateRole])

    def set_active_tab_id(self, tab_id: Optional[int]) -> None:
        """Mark the row for *tab_id* as the currently focused tab.

        Passing ``None`` clears the marker.  The call is idempotent: when
        *tab_id* is unchanged nothing happens.  When the marker moves, a
        ``dataChanged`` carrying :data:`ActiveRole` is emitted for both the
        previously active row and the new one (when they are present in the
        current workspace).
        """
        if tab_id == self._active_tab_id:
            return
        previous = self._active_tab_id
        self._active_tab_id = tab_id
        for changed_id in (previous, tab_id):
            if changed_id is None:
                continue
            node = self._tab_nodes.get(changed_id)
            if node is None:
                continue
            idx = self._index_for_node(node)
            if idx.isValid():
                self.dataChanged.emit(idx, idx, [ActiveRole])

    def active_tab_id(self) -> Optional[int]:
        """Return the tab id currently marked active, or ``None``."""
        return self._active_tab_id

    # --- auto-locate on tab switch (VP-D5) ---

    def set_active_group_id(
            self, group_id: Optional[uuid.UUID]) -> None:
        """Mark the group header for *group_id* as the active group.

        Passing ``None`` clears the marker.  The call is idempotent: when
        *group_id* is unchanged nothing happens.  When the marker moves, a
        ``dataChanged`` carrying :data:`ActiveGroupRole` is emitted for both
        the previously active group row and the new one (when present in the
        current workspace).
        """
        if group_id == self._active_group_id:
            return
        previous = self._active_group_id
        self._active_group_id = group_id
        for changed_id in (previous, group_id):
            if changed_id is None:
                continue
            idx = self._index_for_group_id(changed_id)
            if idx.isValid():
                self.dataChanged.emit(idx, idx, [ActiveGroupRole])

    def active_group_id(self) -> Optional[uuid.UUID]:
        """Return the group id currently marked active, or ``None``."""
        return self._active_group_id

    def set_highlighted_tab_id(
            self,
            tab_id: Optional[int],
            *,
            timeout_ms: int = 2500,
    ) -> None:
        """Transiently highlight the tab row for *tab_id*.

        Emits a ``dataChanged`` carrying :data:`HighlightRole` for the row
        and schedules an automatic clear after *timeout_ms* milliseconds
        (skipped when *tab_id* is ``None`` or *timeout_ms* is non-positive).

        A single instance timer is kept: starting a new highlight cancels
        any pending one first, so an older timer can never clear a newer
        highlight -- even when the same tab is highlighted again
        (A -> B -> A).  ``_clear_highlight`` additionally re-checks the id
        before clearing.
        """
        if tab_id == self._highlighted_tab_id:
            return
        if self._highlight_timer is not None:
            self._highlight_timer.stop()
            self._highlight_timer.deleteLater()
            self._highlight_timer = None
        previous = self._highlighted_tab_id
        self._highlighted_tab_id = tab_id
        for changed_id in (previous, tab_id):
            if changed_id is None:
                continue
            node = self._tab_nodes.get(changed_id)
            if node is None:
                continue
            idx = self._index_for_node(node)
            if idx.isValid():
                self.dataChanged.emit(idx, idx, [HighlightRole])
        if timeout_ms > 0 and tab_id is not None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._clear_highlight(tab_id))
            timer.start(timeout_ms)
            self._highlight_timer = timer

    def _clear_highlight(self, tab_id: int) -> None:
        """Clear the highlight created for *tab_id* if still current."""
        if self._highlighted_tab_id == tab_id:
            self.set_highlighted_tab_id(None)

    def highlight_tab(self, tab_id: int, *, timeout_ms: int = 2500) -> None:
        """Highlight the tab row for *tab_id* for *timeout_ms* milliseconds."""
        self.set_highlighted_tab_id(tab_id, timeout_ms=timeout_ms)

    def focus_group_and_tab(
            self,
            group_id: Optional[uuid.UUID],
            tab_id: int,
    ) -> QModelIndex:
        """Mark *group_id* active, expand it and return the tab's index.

        The returned :class:`QModelIndex` is the row for *tab_id* (invalid
        when the tab is not in the current workspace) so the host can scroll
        to it after bringing the group header into view.
        """
        self.set_active_group_id(group_id)
        if group_id is not None:
            for node in self._root.children:
                if (node.kind == Kind.Group
                        and isinstance(node.obj, Group)
                        and node.obj.group_id == group_id):
                    node.obj.folded = False
                    idx = self._index_for_node(node)
                    if idx.isValid():
                        self.dataChanged.emit(idx, idx, [FoldedRole])
                    break
        node = self._tab_nodes.get(tab_id)
        if node is None:
            return QModelIndex()
        return self._index_for_node(node)

    def _index_for_group_id(
            self, group_id: uuid.UUID) -> QModelIndex:
        """Return the index for the group header with *group_id*, or invalid."""
        for node in self._root.children:
            if (node.kind == Kind.Group
                    and isinstance(node.obj, Group)
                    and node.obj.group_id == group_id):
                return self._index_for_node(node)
        return QModelIndex()

    # --- multi-select (VP-F4) ---

    def set_selection_mode(self, enabled: bool) -> None:
        """Enable or disable multi-select mode.

        Disabling the mode clears the current selection and repaints every
        visible tab row.  Enabling it deliberately does *not* select
        anything: the caller (a command or shortcut) drives the selection.
        """
        if enabled == self._selection_mode:
            return
        self._selection_mode = enabled
        if not enabled and self._selected:
            self._selected.clear()
            for node in self._tab_nodes.values():
                self._emit_selection_changed(node)

    def selection_mode(self) -> bool:
        """Return whether multi-select mode is currently active."""
        return self._selection_mode

    def selected_tab_ids(self) -> set[int]:
        """Return a copy of the currently selected tab ids."""
        return set(self._selected)

    def set_selected(self, tab_id: int, selected: bool) -> None:
        """Add or remove *tab_id* from the selection.

        Emits a targeted ``dataChanged`` carrying :data:`SelectionRole` for
        the tab's row when it is present in the current workspace.
        """
        if selected:
            self._selected.add(tab_id)
        else:
            self._selected.discard(tab_id)
        node = self._tab_nodes.get(tab_id)
        if node is not None:
            self._emit_selection_changed(node)

    def select_range(
            self,
            tab_ids: Iterable[int],
            selected: bool = True,
    ) -> None:
        """Bulk add/remove every id in *tab_ids* and repaint the rows."""
        changed_nodes: list[_Node] = []
        for tab_id in tab_ids:
            if selected:
                if tab_id in self._selected:
                    continue
                self._selected.add(tab_id)
            else:
                if tab_id not in self._selected:
                    continue
                self._selected.discard(tab_id)
            node = self._tab_nodes.get(tab_id)
            if node is not None:
                changed_nodes.append(node)
        for node in changed_nodes:
            self._emit_selection_changed(node)

    def select_all(self, selected: bool = True) -> None:
        """Select (or deselect) every tab in the current workspace."""
        self.select_range(list(self._tab_nodes), selected)

    def _emit_selection_changed(self, node: _Node) -> None:
        """Emit ``dataChanged`` (SelectionRole) for *node*."""
        idx = self._index_for_node(node)
        if idx.isValid():
            self.dataChanged.emit(idx, idx, [SelectionRole])

    def tab_id_for_index(self, index: QModelIndex) -> Optional[int]:
        """Return the tab id behind *index*, or ``None`` for groups/invalid."""
        if not index.isValid():
            return None
        node = index.internalPointer()
        if node.kind == Kind.Group:
            return None
        tab = node.obj
        if not isinstance(tab, WorkspaceTab):
            return None
        return tab.tab_id

    def index_for_tab_id(self, tab_id: int) -> QModelIndex:
        """Return the index for *tab_id*, or an invalid index when absent."""
        node = self._tab_nodes.get(tab_id)
        if node is None:
            return QModelIndex()
        return self._index_for_node(node)

    def group_for_index(self, index: QModelIndex) -> Optional[Group]:
        """Return the group behind *index*, or ``None`` for non-group rows."""
        if not index.isValid():
            return None
        node = index.internalPointer()
        if node.kind != Kind.Group:
            return None
        group = node.obj
        return group if isinstance(group, Group) else None

    def toggle_folded(self, index: QModelIndex) -> None:
        """Toggle the folded state of a group or parent-tab row."""
        if not index.isValid():
            return
        node = index.internalPointer()
        if node.kind == Kind.Group:
            group = node.obj
            assert isinstance(group, Group)
            group.folded = not group.folded
            self.dataChanged.emit(index, index)
        elif node.kind == Kind.ParentTab:
            tab = node.obj
            assert isinstance(tab, WorkspaceTab)
            new_state = not self._folded_state.get(tab.tab_id, False)
            self._folded_state[tab.tab_id] = new_state
            self.dataChanged.emit(index, index)

    # --- QAbstractItemModel implementation ---

    def rowCount(  # noqa: N802 - Qt naming
            self,
            parent: QModelIndex = QModelIndex(),
    ) -> int:
        if not parent.isValid():
            return len(self._root.children)
        node = parent.internalPointer()
        return len(node.children)

    def columnCount(  # noqa: N802 - Qt naming
            self,
            parent: QModelIndex = QModelIndex(),
    ) -> int:
        return 1

    def index(  # noqa: N802 - Qt naming
            self,
            row: int,
            column: int,
            parent: QModelIndex = QModelIndex(),
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_node = parent.internalPointer() if parent.isValid() else self._root
        return self.createIndex(row, column, parent_node.children[row])

    def parent(  # noqa: N802 - Qt naming  # type: ignore[override]
            self,
            child: QModelIndex = QModelIndex(),
    ) -> QModelIndex:
        if not child.isValid():
            return QModelIndex()
        node = child.internalPointer()
        if node.parent is None or node.parent is self._root:
            return QModelIndex()
        return self._index_for_node(node.parent)

    def data(  # noqa: N802 - Qt naming
            self,
            index: QModelIndex,
            role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if node.kind == Kind.Group:
            return self._group_data(node.obj, role)
        return self._tab_data(node.obj, role, node.kind)

    def flags(  # noqa: N802 - Qt naming
            self,
            index: QModelIndex,
    ) -> Qt.ItemFlag:
        if not index.isValid():
            # The root must accept drops so a pinned entry can land as a
            # top-level standalone row.
            return Qt.ItemFlag.ItemIsDropEnabled
        return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDropEnabled)

    # --- pinned-tab drag & drop ---

    def set_drop_target(self, index: QModelIndex) -> None:
        """Highlight *index* as the pinned-tab drop target.

        Passing an invalid index clears the highlight.  Only the row itself
        is repainted, so dragging over a long tree stays cheap.
        """
        target = self._drop_target_for_index(index)
        if target == self._drop_target:
            return
        previous = self._drop_target
        self._drop_target = target
        for key in (previous, target):
            if key is None:
                continue
            idx = self._index_for_drop_key(key)
            if idx.isValid():
                self.dataChanged.emit(idx, idx, [DropTargetRole])

    def _drop_target_for_index(
            self,
            index: QModelIndex,
    ) -> Optional[tuple[str, object]]:
        if not index.isValid():
            return None
        node = index.internalPointer()
        if node.kind == Kind.Group:
            group = node.obj
            assert isinstance(group, Group)
            return ('group', group.group_id)
        tab = node.obj
        if not isinstance(tab, WorkspaceTab):
            return None
        return ('tab', tab.tab_id)

    def _index_for_drop_key(self, key: tuple[str, object]) -> QModelIndex:
        kind, value = key
        if kind == 'group':
            assert isinstance(value, uuid.UUID)
            return self._index_for_group_id(value)
        assert isinstance(value, int)
        return self.index_for_tab_id(value)

    def _pinned_for_mime(self, mime_data) -> Optional[PinnedTab]:
        """Decode the pinned entry carried by *mime_data*."""
        if not mime_data.hasFormat(PINNED_TAB_MIME):
            return None
        raw = bytes(mime_data.data(PINNED_TAB_MIME))
        try:
            tab_id = int(raw.decode('ascii'))
        except ValueError:
            return None
        return next(
            (entry for entry in self._manager.list_pinned()
             if entry.tab_id == tab_id),
            None,
        )

    def _drop_target_group(
            self,
            index: QModelIndex,
    ) -> tuple[Optional[uuid.UUID], Optional[int]]:
        """Return ``(group_id, parent_tab_id)`` for a drop on *index*.

        A group row yields its group and no parent.  A tab row inside a
        group yields that group and the tab as parent (ADR 0008 § "Pinned-
        tab drop target").  A standalone tab row or the root yields a
        top-level standalone entry.
        """
        group = self.group_for_index(index)
        if group is not None:
            return (group.group_id, None)
        tab = self._workspace_tab_for_index(index)
        if tab is None:
            return (None, None)
        if tab.group_id is None:
            return (None, None)
        return (tab.group_id, tab.tab_id)

    def _workspace_tab_for_index(
            self,
            index: QModelIndex,
    ) -> Optional[WorkspaceTab]:
        if not index.isValid():
            return None
        node = index.internalPointer()
        if node.kind == Kind.Group:
            return None
        tab = node.obj
        return tab if isinstance(tab, WorkspaceTab) else None

    def mimeTypes(self) -> list[str]:  # noqa: N802 - Qt naming
        return [PINNED_TAB_MIME]

    def supportedDropActions(self) -> Qt.DropAction:  # noqa: N802
        return Qt.DropAction.CopyAction

    def canDropMimeData(  # noqa: N802 - Qt naming
            self,
            data,
            action: Qt.DropAction,
            row: int,
            column: int,
            parent: QModelIndex,
    ) -> bool:
        del row, column  # the hovered row identifies the target
        if action != Qt.DropAction.CopyAction:
            return False
        if self._workspace is None:
            return False
        pinned = self._pinned_for_mime(data)
        if pinned is None:
            return False

        group = self.group_for_index(parent)
        if group is not None:
            return not group.archived

        tab = self._workspace_tab_for_index(parent)
        if tab is not None and tab.group_id is not None:
            for candidate in self._workspace.groups:
                if candidate.group_id == tab.group_id:
                    return not candidate.archived
        # Root / standalone tab: a top-level drop is always valid.
        return True

    def dropMimeData(  # noqa: N802 - Qt naming
            self,
            data,
            action: Qt.DropAction,
            row: int,
            column: int,
            parent: QModelIndex,
    ) -> bool:
        del row, column
        if not self.canDropMimeData(data, action, -1, -1, parent):
            return False
        pinned = self._pinned_for_mime(data)
        assert pinned is not None
        assert self._workspace is not None

        group_id, parent_tab_id = self._drop_target_group(parent)
        workspace = self._workspace
        favicon = (
            base64.b64encode(pinned.favicon).decode('ascii')
            if pinned.favicon else None
        )
        entry = WorkspaceTab(
            tab_id=pinned.tab_id,
            parent_id=parent_tab_id,
            group_id=group_id,
            title=pinned.title,
            url=pinned.url,
            source=QUrl(pinned.url).host().lower(),
            favicon=favicon,
            open_status='OPEN',
        )
        # Unpin first, then add the regular entry.  Both calls emit
        # ``changed``; the target ids above survive the intermediate reset.
        self._manager.remove_pinned(pinned.tab_id)
        self._manager.add_tab(workspace.id, entry)
        self._drop_target = None
        return True

    # --- private helpers ---

    def _build_tree(self, workspace: Workspace) -> None:
        tabs = list(workspace.tabs)

        if self._filter_text:
            keep_ids = self._filter_keep_ids(tabs)
            tabs = [t for t in tabs if t.tab_id in keep_ids]
        else:
            keep_ids = None

        parent_ids = {t.parent_id for t in tabs if t.parent_id is not None}

        groups = [
            group for group in workspace.groups
            if self._show_archived or not group.archived
        ]
        if keep_ids is not None:
            needle = self._filter_text.lower()
            groups = [
                group for group in groups
                if needle in group.name.lower()
                or any(t.tab_id in keep_ids
                       for t in workspace.tabs if t.group_id == group.group_id)
            ]
        # Pinned groups sort first. Without the open-first setting,
        # insertion order is preserved within each band (``sorted`` is
        # stable); pinning itself never changes list order.
        if self._sort_open_groups_first():
            # Pinned first, then OPEN groups, then SUSPENDED/ARCHIVED (the
            # archived band sorts with the suspended one; the archive UI
            # already separates archived groups visually).
            groups = sorted(
                groups,
                key=lambda group: (
                    not group.pinned,
                    self._manager.group_state(
                        workspace.id, group.group_id) != 'OPEN',
                    group.name,
                ),
            )
        else:
            groups = sorted(groups, key=lambda group: not group.pinned)

        group_nodes: dict[uuid.UUID, _Node] = {}
        for group in groups:
            node = _Node(Kind.Group, group, self._root)
            group_nodes[group.group_id] = node
            node.row = len(self._root.children)
            self._root.children.append(node)

        def container(tab: WorkspaceTab) -> _Node:
            if tab.group_id is not None and tab.group_id in group_nodes:
                return group_nodes[tab.group_id]
            return self._root

        tab_nodes: dict[int, _Node] = {}
        for tab in tabs:
            kind = Kind.ParentTab if tab.tab_id in parent_ids else Kind.Tab
            node = _Node(kind, tab, None)
            tab_nodes[tab.tab_id] = node
            self._tab_nodes[tab.tab_id] = node

        for tab in tabs:
            node = tab_nodes[tab.tab_id]
            target = container(tab)
            if tab.parent_id is not None:
                parent_node = tab_nodes.get(tab.parent_id)
                if parent_node is not None:
                    assert isinstance(parent_node.obj, WorkspaceTab)
                    if container(parent_node.obj) is target:
                        target = parent_node
            node.parent = target
            node.row = len(target.children)
            target.children.append(node)

    def _build_search_tree(self, hits: list[search.SearchHit]) -> None:
        """Build a flat-by-group tree from pre-sorted *hits*.

        Group headers are created on first hit, so groups without matches are
        omitted naturally.  Tabs are always plain ``Kind.Tab`` rows: nesting
        a hit under a non-hit parent would hide it, and the search result
        list is sorted globally by relevance rather than tree topology.
        """
        group_nodes: dict[uuid.UUID, _Node] = {}
        for hit in hits:
            group = hit.group
            if group is not None:
                target = group_nodes.get(group.group_id)
                if target is None:
                    target = _Node(Kind.Group, group, self._root)
                    target.row = len(self._root.children)
                    self._root.children.append(target)
                    group_nodes[group.group_id] = target
            else:
                target = self._root
            node = _Node(Kind.Tab, hit.entry, target)
            node.row = len(target.children)
            target.children.append(node)
            self._tab_nodes[hit.entry.tab_id] = node

    def _filter_keep_ids(self, tabs: list[WorkspaceTab]) -> set[int]:
        """Return the tab ids to keep under the active filter.

        A tab is kept when its title or URL (persisted or runtime) contains
        the filter text; its ancestor chain via ``parent_id`` is also kept so
        matches remain reachable in the tree.
        """
        needle = self._filter_text.lower()
        matched: set[int] = set()
        for tab in tabs:
            info = self._tab_info.get(tab.tab_id, _TabInfo())
            haystack = ' '.join(
                [tab.title, tab.url, info.title, info.url]).lower()
            if needle in haystack:
                matched.add(tab.tab_id)

        parent_of = {
            t.tab_id: t.parent_id for t in tabs if t.parent_id is not None
        }
        keep = set(matched)
        changed = True
        while changed:
            changed = False
            for tab_id in list(keep):
                parent_id = parent_of.get(tab_id)
                if parent_id is not None and parent_id not in keep:
                    keep.add(parent_id)
                    changed = True
        return keep

    def _group_tab_count(self, group: Group) -> int:
        if self._workspace is None:
            return 0
        return sum(
            1 for t in self._workspace.tabs
            if t.group_id == group.group_id
        )

    def _group_data(self, group: Group, role: int):
        count = self._group_tab_count(group)
        if role == Qt.ItemDataRole.DisplayRole:
            return group.name
        if role == Qt.ItemDataRole.ToolTipRole:
            plural = 's' if count != 1 else ''
            return f"{group.name}\n{count} tab{plural}"
        if role == FoldedRole:
            # Search results are always expanded: folding a hit group would
            # hide the very rows the user asked for.
            return False if self.search_active else group.folded
        if role == BadgeRole:
            return self._group_badge(group)
        if role == GroupStateRole:
            return self._group_state(group)
        if role == ActiveGroupRole:
            return group.group_id == self._active_group_id
        if role == GroupIdRole:
            return group.group_id
        if role == DropTargetRole:
            return self._drop_target == ('group', group.group_id)
        return _GROUP_STATIC_ROLES.get(role)

    def _group_badge(self, group: Group) -> str:
        """Return the ``"open/total"`` badge text for *group*."""
        if self._workspace is None:
            return '0/0'
        open_count = self._manager.group_open_count(
            self._workspace.id, group.group_id)
        total_count = self._manager.group_total_count(
            self._workspace.id, group.group_id)
        return f"{open_count}/{total_count}"

    def _group_state(self, group: Group) -> str:
        """Return the resolved running state for *group*."""
        if self._workspace is None:
            return 'SUSPENDED'
        return self._manager.group_state(
            self._workspace.id, group.group_id)

    def _sort_open_groups_first(self) -> bool:
        """Return whether OPEN groups should sort before suspended ones."""
        # Imported lazily so importing this module never depends on the
        # config being initialised (several unit tests build a model
        # without a config stub).
        from qutebrowser.config import config
        try:
            return bool(
                config.val.content.vertical_panel.sort_open_groups_first)
        except AttributeError:
            return True

    def _tab_data(self, tab: WorkspaceTab, role: int, kind: Kind):
        info = self._tab_info.get(tab.tab_id, _TabInfo())
        if role == KindRole:
            return kind
        if role == ActiveRole:
            return self._active_tab_id == tab.tab_id
        if role == OpenStatusRole:
            return getattr(tab, 'open_status', 'SUSPENDED')
        if role in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
            Qt.ItemDataRole.DecorationRole,
            UrlRole,
        ):
            return self._tab_visible_data(info, role)
        if role == FoldedRole:
            return self._tab_folded(tab, kind)
        if role == StatusRole:
            status = info.status
            if getattr(tab, 'open_status', 'SUSPENDED') == 'SUSPENDED':
                status |= Status.SUSPENDED
            return status
        if role == PinStateRole:
            return info.status & Status.PINNED != 0
        if role == SelectionRole:
            return tab.tab_id in self._selected
        if role == HighlightRole:
            return self._highlighted_tab_id == tab.tab_id
        if role in (GroupIdRole, DropTargetRole):
            return self._tab_relation_role(tab, role)
        return _TAB_STATIC_ROLES.get(role)

    def _tab_relation_role(self, tab: WorkspaceTab, role: int):
        """Return the group id / drop-target roles for *tab*."""
        if role == GroupIdRole:
            return tab.group_id
        return self._drop_target == ('tab', tab.tab_id)

    def _tab_visible_data(self, info: _TabInfo, role: int):
        if role == Qt.ItemDataRole.DisplayRole:
            return info.title
        if role == Qt.ItemDataRole.ToolTipRole:
            if info.url:
                return f"{info.title}\n{info.url}"
            return info.title
        if role == Qt.ItemDataRole.DecorationRole:
            if info.favicon:
                pixmap = QPixmap()
                if pixmap.loadFromData(info.favicon):
                    return pixmap
            return self._placeholder_pixmap
        if role == UrlRole:
            return info.url
        return None

    def _tab_folded(self, tab: WorkspaceTab, kind: Kind) -> bool:
        if kind == Kind.ParentTab:
            return self._folded_state.get(tab.tab_id, False)
        return False

    def _index_for_node(self, node: _Node) -> QModelIndex:
        if node is self._root:
            return QModelIndex()
        return self.createIndex(node.row, 0, node)

    @staticmethod
    def _create_placeholder_pixmap() -> QPixmap:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        return pixmap


class TabTreeItemDelegate(QStyledItemDelegate):

    """Custom delegate rendering the TabTree rows.

    Draws the chevron, favicon, elided title and trailing badge or status
    strip. Mouse interactions on the chevron, audio icon, close glyph, pin
    glyph and row body are surfaced as signals so the host can act on them.

    Attributes:
        fold_toggled: Emitted when the chevron of a group or parent tab is
            clicked.
        audio_toggled: Emitted when the audio icon of an audible tab is
            clicked.
        close_requested: Emitted when the hover close glyph of a tab is
            clicked.
        pin_toggled: Emitted when the pin glyph of a tab is clicked.
        activated: Emitted when the body of a row is clicked.
        selection_toggled: Emitted when a tab row is clicked while the model
            is in multi-select mode.  The click is consumed instead of
            triggering :attr:`activated`; the host decides whether to add,
            remove or extend the selection.
        selection_range_requested: Emitted when a tab row is Shift-clicked
            while the model is in multi-select mode.  The host extends the
            selection from its current anchor to the clicked row.
    """

    fold_toggled = pyqtSignal(QModelIndex)
    audio_toggled = pyqtSignal(QModelIndex)
    close_requested = pyqtSignal(QModelIndex)
    pin_toggled = pyqtSignal(QModelIndex)
    activated = pyqtSignal(QModelIndex)
    selection_toggled = pyqtSignal(QModelIndex)
    selection_range_requested = pyqtSignal(QModelIndex)

    ROW_HEIGHT_GROUP = 24
    ROW_HEIGHT_TAB = 22
    ICON_SIZE = 16
    CHEVRON_WIDTH = 12
    GAP_CHEVRON_ICON = 4
    GAP_ICON_TEXT = 6
    GAP_STRIP = 6
    RIGHT_MARGIN = 6
    CLOSE_WIDTH = 14
    GAP_BEFORE_CLOSE = 6
    PIN_WIDTH = 14
    GAP_BEFORE_PIN = 6
    ACTIVE_ACCENT_WIDTH = 2
    #: Width of the multi-select checkmark drawn in the left margin.
    SELECTION_INDICATOR_WIDTH = 12

    #: Upper bound for the per-index close-glyph hit-rect cache.
    CLOSE_RECT_CACHE_SIZE = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Close-glyph hit rects recorded during paint, keyed by index.
        self._last_close_rect: dict[QModelIndex, QRect] = {}
        # Pin-glyph hit rects recorded during paint, keyed by index.
        self._last_pin_rect: dict[QModelIndex, QRect] = {}
        # Left-edge accent rect recorded during paint for the active tab.
        self._last_active_accent: Optional[QRect] = None

    def sizeHint(  # noqa: N802 - Qt naming
            self,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> QSize:
        kind = index.data(KindRole)
        height = (
            self.ROW_HEIGHT_GROUP
            if kind == Kind.Group
            else self.ROW_HEIGHT_TAB
        )
        return QSize(option.rect.width(), height)

    def paint(  # noqa: N802 - Qt naming  # type: ignore[override]
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> None:
        self.initStyleOption(option, index)
        kind = index.data(KindRole)
        folded = bool(index.data(FoldedRole))

        painter.save()

        # Transient auto-locate / drop-target washes are painted beneath
        # the selection / hover pass so those still win when both apply.
        if (bool(index.data(HighlightRole))
                or bool(index.data(DropTargetRole))):
            self._draw_transient_highlight(painter, option, index)

        # Background / selection.
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, option.palette.alternateBase())

        text_color = (
            option.palette.highlightedText().color()
            if option.state & QStyle.StateFlag.State_Selected
            else option.palette.text().color()
        )
        # Group running state (VP-D2): dim suspended groups and give
        # archived groups a distinct muted hint.  OPEN groups are unchanged.
        if kind == Kind.Group:
            group_state = index.data(GroupStateRole)
            if group_state == 'SUSPENDED':
                dimmed = QColor(text_color)
                dimmed.setAlpha(140)
                text_color = dimmed
            elif group_state == 'ARCHIVED':
                archived = QColor(text_color)
                archived.setAlpha(100)
                text_color = archived
                hint = QColor(option.palette.mid().color())
                hint.setAlpha(40)
                painter.fillRect(option.rect, hint)
        painter.setPen(text_color)

        rect = option.rect
        x = rect.x()

        # Chevron.
        chevron_rect: Optional[QRect] = None  # type: ignore[name-defined]
        if kind in (Kind.Group, Kind.ParentTab):
            chevron_rect = QRect(
                x,
                rect.y(),
                self.CHEVRON_WIDTH,
                rect.height(),
            )
            empty_group = (
                kind == Kind.Group
                and index.data(BadgeRole) in (None, '', '0/0')
            )
            self._draw_chevron(
                painter, chevron_rect, not folded, empty_group, text_color,
            )
            x += self.CHEVRON_WIDTH + self.GAP_CHEVRON_ICON

        # Favicon (tabs only).
        if kind in (Kind.Tab, Kind.ParentTab):
            icon_y = rect.y() + (rect.height() - self.ICON_SIZE) // 2
            icon_rect = QRect(x, icon_y, self.ICON_SIZE, self.ICON_SIZE)
            favicon = index.data(Qt.ItemDataRole.DecorationRole)
            if isinstance(favicon, QPixmap) and not favicon.isNull():
                painter.drawPixmap(
                    icon_rect.x(), icon_rect.y(),
                    self.ICON_SIZE, self.ICON_SIZE,
                    favicon,
                )
            else:
                painter.fillRect(icon_rect, option.palette.mid())
            x += self.ICON_SIZE + self.GAP_ICON_TEXT

        # Title, trailing status/badge, hover close glyph and the active
        # accent are all delegated so the paint method stays small.
        self._draw_row_content(
            painter, option, index, kind, rect, x, text_color)

        # Multi-select checkmark (VP-F4): drawn as an overlay in the left
        # margin so the title / favicon / badge layout is untouched.
        if bool(index.data(SelectionRole)):
            self._draw_selection_indicator(painter, rect, option)

        # Focus ring (keyboard focus only; skip hover-only focus).
        if (
                option.state & QStyle.StateFlag.State_HasFocus
                and not (option.state & QStyle.StateFlag.State_MouseOver)
        ):
            painter.setPen(text_color)
            painter.drawRect(option.rect.adjusted(1, 1, -1, -1))

        painter.restore()

    def editorEvent(  # noqa: N802 - Qt naming  # type: ignore[override]
            self,
            event: QEvent,
            model: QAbstractItemModel,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> bool:
        if (
                event.type() != QEvent.Type.MouseButtonPress
                or not isinstance(event, QMouseEvent)
                or event.button() != Qt.MouseButton.LeftButton
        ):
            return super().editorEvent(event, model, option, index)

        if not index.isValid():
            return super().editorEvent(event, model, option, index)

        kind = index.data(KindRole)

        # Multi-select mode (VP-F4): a click on a tab row toggles its
        # selection and is consumed so it never activates the tab.  The
        # delegate only reports the hit; the host (which owns both the
        # model and the selection-mode state) decides add/remove/extend.
        #
        # Modifier choice (documented per review): Shift extends the range
        # from the current anchor; Ctrl/Meta toggles a single row; a plain
        # click is treated as a single toggle too, matching the discoverable
        # click-to-select behaviour.
        selection_mode = getattr(model, 'selection_mode', None)
        if (callable(selection_mode) and selection_mode()
                and kind in (Kind.Tab, Kind.ParentTab)):
            modifiers = event.modifiers()
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self.selection_range_requested.emit(index)
            else:
                self.selection_toggled.emit(index)
            return True

        rect = option.rect
        x = rect.x()

        # Chevron hit rect.
        if kind in (Kind.Group, Kind.ParentTab):
            chevron_rect = QRect(
                x, rect.y(), self.CHEVRON_WIDTH, rect.height(),
            )
            if chevron_rect.contains(event.pos()):
                self.fold_toggled.emit(index)
                return True
            x += self.CHEVRON_WIDTH + self.GAP_CHEVRON_ICON

        # Close ×, pin glyph and audio hit rects (close/pin require a prior
        # paint of this row to leave cached rects).
        if kind in (Kind.Tab, Kind.ParentTab):
            if self._hit_test_trailing_glyphs(
                    event, option, index, rect):
                return True
            x += self.ICON_SIZE + self.GAP_ICON_TEXT

        # Body click.
        body_rect = QRect(
            x,
            rect.y(),
            max(rect.right() - x - self.RIGHT_MARGIN, 0),
            rect.height(),
        )
        if body_rect.contains(event.pos()):
            self.activated.emit(index)
            return True

        return super().editorEvent(event, model, option, index)

    def helpEvent(  # noqa: N802 - Qt naming  # type: ignore[override]
            self,
            event: QHelpEvent,
            view: QAbstractItemView,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> bool:
        if event.type() != QEvent.Type.ToolTip:
            return super().helpEvent(event, view, option, index)

        tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
        if tooltip:
            QToolTip.showText(event.globalPos(), str(tooltip), view)
            return True
        return super().helpEvent(event, view, option, index)

    # --- private helpers ---

    def _hit_test_trailing_glyphs(
            self,
            event: QMouseEvent,
            option: QStyleOptionViewItem,
            index: QModelIndex,
            rect: QRect,
    ) -> bool:
        """Handle clicks on the close, pin or audio glyphs.

        Returns ``True`` when the click was consumed by one of the trailing
        glyphs.  The close and pin hit rects come from the last paint pass;
        the audio rect is computed from the status strip layout.
        """
        close_rect = self._last_close_rect.get(index)
        close_reserve = 0
        if close_rect is not None:
            close_reserve = self.CLOSE_WIDTH + self.GAP_BEFORE_CLOSE
            if close_rect.contains(event.pos()):
                self.close_requested.emit(index)
                return True

        pin_rect = self._last_pin_rect.get(index)
        pin_reserve = 0
        if pin_rect is not None:
            pin_reserve = self.PIN_WIDTH + self.GAP_BEFORE_PIN
            if pin_rect.contains(event.pos()):
                self.pin_toggled.emit(index)
                return True

        # Audio icon hit rect (only for audible tabs).
        status = Status(int(index.data(StatusRole) or 0))
        if status & Status.AUDIBLE:
            fm: QFontMetrics = option.fontMetrics
            audio_text = _Icon.AUDIBLE.value
            audio_width = fm.horizontalAdvance(audio_text)
            status_text = self._status_text(status)
            full_width = fm.horizontalAdvance(status_text)
            # Audio is the leftmost glyph in the status strip.
            strip_x = (
                rect.right() - self.RIGHT_MARGIN - close_reserve
                - pin_reserve - full_width
            )
            audio_rect = QRect(
                strip_x,
                rect.y(),
                audio_width,
                rect.height(),
            )
            if audio_rect.contains(event.pos()):
                self.audio_toggled.emit(index)
                return True
        return False

    def _draw_row_content(
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
            kind: Kind,
            rect: QRect,
            x: int,
            text_color: QColor,
    ) -> None:
        """Draw the title, trailing strip, close/pin glyphs and active accent."""
        fm: QFontMetrics = option.fontMetrics
        is_tab = kind in (Kind.Tab, Kind.ParentTab)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        close_rect: Optional[QRect] = None
        if is_tab and hovered:
            close_rect = self._close_rect(rect)
        close_reserve = (
            self.CLOSE_WIDTH + self.GAP_BEFORE_CLOSE
            if close_rect is not None
            else 0
        )

        # The pin glyph sits left of the close glyph. It is always drawn for
        # a pinned tab, and on hover otherwise (mirroring the close ×).
        is_pinned = is_tab and bool(index.data(PinStateRole))
        pin_rect: Optional[QRect] = None
        if is_tab and (is_pinned or hovered):
            pin_rect = self._pin_rect(rect, close_reserve)
        pin_reserve = (
            self.PIN_WIDTH + self.GAP_BEFORE_PIN
            if pin_rect is not None
            else 0
        )

        status_text = ''
        if is_tab:
            status = Status(int(index.data(StatusRole) or 0))
            status_text = self._status_text(status)
        badge_text = ''
        if kind == Kind.Group:
            badge = index.data(BadgeRole)
            if badge is not None:
                badge_text = str(badge)

        strip_width = (
            fm.horizontalAdvance(status_text) + self.GAP_STRIP
            if status_text
            else 0
        )
        badge_width = (
            fm.horizontalAdvance(badge_text) + self.GAP_STRIP
            if badge_text
            else 0
        )
        trailing_width = (
            max(strip_width, badge_width) + close_reserve + pin_reserve
        )
        text_width = max(
            rect.width() - (x - rect.x()) - trailing_width - self.RIGHT_MARGIN,
            0,
        )

        title = index.data(Qt.ItemDataRole.DisplayRole) or ''
        elided = fm.elidedText(
            title,
            Qt.TextElideMode.ElideRight,
            text_width,
        )
        painter.drawText(
            x,
            rect.y(),
            text_width,
            rect.height(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided,
        )

        # Status strip or badge, right aligned (shifted left of the pin and
        # close glyphs).
        if status_text or badge_text:
            draw_text = status_text or badge_text
            drawn_width = fm.horizontalAdvance(draw_text)
            text_x = (
                rect.right() - self.RIGHT_MARGIN - close_reserve
                - pin_reserve - drawn_width
            )
            if kind == Kind.Group:
                # Dimmed badge.
                dim_color = QColor(text_color)
                dim_color.setAlpha(140)
                painter.setPen(dim_color)
            painter.drawText(
                text_x,
                rect.y(),
                drawn_width,
                rect.height(),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                draw_text,
            )

        self._finish_row(
            painter, option, index, rect, is_tab, kind, close_rect, pin_rect,
            is_pinned,
        )

    def _finish_row(
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
            rect: QRect,
            is_tab: bool,
            kind: Kind,
            close_rect: Optional[QRect],
            pin_rect: Optional[QRect],
            is_pinned: bool,
    ) -> None:
        """Draw the close/pin glyphs and the active-tab/group left accent."""
        # Hover close glyph and its hit-rect cache.
        if close_rect is not None:
            self._draw_close(painter, close_rect, option.palette.mid().color())
            self._remember_close_rect(index, close_rect)
        else:
            self._last_close_rect.pop(index, None)

        # Pin glyph: full-opacity accent when pinned, half-opacity muted
        # otherwise (the latter only happens on hover).
        if pin_rect is not None:
            if is_pinned:
                pin_color = option.palette.highlight().color()
            else:
                pin_color = QColor(option.palette.mid().color())
                pin_color.setAlpha(128)
            self._draw_pin(painter, pin_rect, pin_color)
            self._remember_pin_rect(index, pin_rect)
        else:
            self._last_pin_rect.pop(index, None)

        # Active-tab / auto-located group left-edge accent (visible without
        # hover).
        active_tab = is_tab and bool(index.data(ActiveRole))
        active_group = (
            kind == Kind.Group and bool(index.data(ActiveGroupRole)))
        if active_tab or active_group:
            accent_rect = QRect(
                rect.x(), rect.y(), self.ACTIVE_ACCENT_WIDTH, rect.height())
            painter.fillRect(accent_rect, option.palette.highlight().color())
            self._last_active_accent = accent_rect
        else:
            self._last_active_accent = None

    def _close_rect(self, rect: QRect) -> QRect:
        """Return the close-glyph rect at the right edge of *rect*."""
        y = rect.y() + (rect.height() - self.CLOSE_WIDTH) // 2
        x = rect.right() - self.RIGHT_MARGIN - self.CLOSE_WIDTH + 1
        return QRect(x, y, self.CLOSE_WIDTH, self.CLOSE_WIDTH)

    def _draw_close(
            self,
            painter: QPainter,
            rect: QRect,
            color: QColor,
    ) -> None:
        """Draw a small × glyph inside *rect* using *color*."""
        pen = QPen(color)
        pen.setWidth(2)
        painter.save()
        painter.setPen(pen)
        inset = 4
        line_rect = rect.adjusted(inset, inset, -inset, -inset)
        painter.drawLine(line_rect.topLeft(), line_rect.bottomRight())
        painter.drawLine(line_rect.topRight(), line_rect.bottomLeft())
        painter.restore()

    def _draw_selection_indicator(
            self,
            painter: QPainter,
            rect: QRect,
            option: QStyleOptionViewItem,
    ) -> None:
        """Draw the multi-select checkmark in the row's left margin."""
        size = self.SELECTION_INDICATOR_WIDTH
        y = rect.y() + (rect.height() - size) // 2
        box = QRect(
            rect.x() + self.ACTIVE_ACCENT_WIDTH + 2, y, size, size)
        painter.save()
        painter.fillRect(box, option.palette.highlight().color())
        painter.setPen(option.palette.highlightedText().color())
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, '✓')
        painter.restore()

    def _remember_close_rect(self, index: QModelIndex, rect: QRect) -> None:
        """Cache the close-glyph hit rect for *index*, bounding the cache."""
        self._last_close_rect[index] = rect
        while len(self._last_close_rect) > self.CLOSE_RECT_CACHE_SIZE:
            oldest = next(iter(self._last_close_rect))
            del self._last_close_rect[oldest]

    def _pin_rect(self, rect: QRect, close_reserve: int) -> QRect:
        """Return the pin-glyph rect to the left of the close glyph.

        *close_reserve* is the space already reserved on the right for the
        close glyph (``CLOSE_WIDTH + GAP_BEFORE_CLOSE``) or ``0`` when the
        close glyph is not visible.
        """
        y = rect.y() + (rect.height() - self.PIN_WIDTH) // 2
        x = (
            rect.right() - self.RIGHT_MARGIN - close_reserve
            - self.GAP_BEFORE_PIN - self.PIN_WIDTH + 1
        )
        return QRect(x, y, self.PIN_WIDTH, self.PIN_WIDTH)

    def _draw_pin(
            self,
            painter: QPainter,
            rect: QRect,
            color: QColor,
    ) -> None:
        """Draw a bold ``P`` glyph centered in *rect* using *color*."""
        painter.save()
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignCenter,
            _Icon.PINNED.value,
        )
        painter.restore()

    def _remember_pin_rect(self, index: QModelIndex, rect: QRect) -> None:
        """Cache the pin-glyph hit rect for *index*, bounding the cache."""
        self._last_pin_rect[index] = rect
        while len(self._last_pin_rect) > self.CLOSE_RECT_CACHE_SIZE:
            oldest = next(iter(self._last_pin_rect))
            del self._last_pin_rect[oldest]

    def _draw_chevron(
            self,
            painter: QPainter,
            rect: QRect,
            expanded: bool,
            empty: bool,
            color: QColor,
    ) -> None:
        cx = rect.x() + rect.width() / 2
        cy = rect.y() + rect.height() / 2
        size = 4.5

        path = QPainterPath()
        path.moveTo(cx - size, cy - size)
        path.lineTo(cx + size, cy)
        path.lineTo(cx - size, cy + size)
        path.closeSubpath()

        painter.save()
        painter.translate(cx, cy)
        if expanded:
            painter.rotate(90)
        painter.translate(-cx, -cy)

        if empty:
            painter.setPen(color)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        else:
            painter.fillPath(path, color)
        painter.restore()

    def _draw_transient_highlight(
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> None:
        """Paint the auto-locate / drop-target row washes."""
        if bool(index.data(HighlightRole)):
            painter.fillRect(option.rect, self._highlight_color(option))
        if bool(index.data(DropTargetRole)):
            target = QColor(option.palette.highlight().color())
            target.setAlpha(70)
            painter.fillRect(option.rect, target)

    @staticmethod
    def _highlight_color(option: QStyleOptionViewItem) -> QColor:
        """Return the faint auto-locate wash for *option*'s palette."""
        base = QColor(option.palette.base().color())
        accent = QColor(option.palette.highlight().color())
        ratio = 0.18
        return QColor(
            round(base.red() + (accent.red() - base.red()) * ratio),
            round(base.green() + (accent.green() - base.green()) * ratio),
            round(base.blue() + (accent.blue() - base.blue()) * ratio),
        )

    @staticmethod
    def _status_text(status: Status) -> str:
        parts = []
        if status & Status.LOADING:
            parts.append(_Icon.LOADING.value)
        if status & Status.AUDIBLE:
            parts.append(_Icon.AUDIBLE.value)
        if status & Status.SUSPENDED:
            parts.append(_Icon.SUSPENDED.value)
        # PINNED is rendered as its own affordance column (the P glyph),
        # not as part of the status strip.
        return ''.join(parts)


class _TabTreeView(QTreeView):

    """QTreeView which accepts pinned-entry drags from the PIN panel.

    The view drives the model's ``canDropMimeData`` / ``dropMimeData``
    itself so it can reject invalid rows *before* Qt paints a drop
    indicator, and so the hovered row can carry :data:`DropTargetRole`
    while the drag is in progress.
    """

    @staticmethod
    def _event_pos(event) -> QPoint:
        """Return the event position as a QPoint (Qt5/Qt6 compatible)."""
        try:
            return event.position().toPoint()
        except AttributeError:  # Qt5
            return event.pos()

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasFormat(PINNED_TAB_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not event.mimeData().hasFormat(PINNED_TAB_MIME):
            super().dragMoveEvent(event)
            return
        index = self.indexAt(self._event_pos(event))
        model = self.model()
        if model.canDropMimeData(
                event.mimeData(), Qt.DropAction.CopyAction,
                -1, -1, index):
            model.set_drop_target(index)
            event.acceptProposedAction()
        else:
            model.set_drop_target(QModelIndex())
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        model = self.model()
        if model is not None:
            model.set_drop_target(QModelIndex())
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not event.mimeData().hasFormat(PINNED_TAB_MIME):
            super().dropEvent(event)
            return
        index = self.indexAt(self._event_pos(event))
        model = self.model()
        dropped = model.dropMimeData(
            event.mimeData(), Qt.DropAction.CopyAction, -1, -1, index)
        model.set_drop_target(QModelIndex())
        if dropped:
            event.acceptProposedAction()
        else:
            event.ignore()


class TabTree(QWidget):

    """Host widget for the active-workspace TabTree.

    Owns the ``QTreeView``, delegate and model. Shows a centered placeholder
    when the active workspace has no tabs or groups.

    Attributes:
        activated: Emitted when a tab row body is clicked.
            Argument: the clicked ``QModelIndex``.
        fold_toggled: Forwarded from the delegate when a chevron is clicked.
        audio_toggled: Forwarded from the delegate when an audio icon is
            clicked.
        close_requested: Forwarded from the delegate when the hover close
            glyph is clicked.
        pin_toggled: Forwarded from the delegate when the pin glyph is
            clicked.
        selection_toggled: Emitted when a tab row is clicked while
            multi-select mode is active.  Argument: the clicked
            ``QModelIndex``.
        selection_range_requested: Emitted when a tab row is Shift-clicked
            while multi-select mode is active.  Argument: the clicked
            ``QModelIndex``.
        group_focus_requested: Emitted when the browser switches to a tab
            the tree should auto-locate (VP-D5).  Arguments: the owning
            group id (or ``None`` for a standalone tab) and the tab id.  The
            host connects this to :meth:`focus_group_and_tab`.
    """

    activated = pyqtSignal(QModelIndex)
    keyboard_activated = pyqtSignal(QModelIndex)
    fold_toggled = pyqtSignal(QModelIndex)
    audio_toggled = pyqtSignal(QModelIndex)
    close_requested = pyqtSignal(QModelIndex)
    pin_toggled = pyqtSignal(QModelIndex)
    selection_toggled = pyqtSignal(QModelIndex)
    selection_range_requested = pyqtSignal(QModelIndex)
    group_focus_requested = pyqtSignal(object, int)

    def __init__(
            self,
            win_id: int,
            manager: WorkspaceManager,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._win_id = win_id
        self._manager = manager
        # Anchor for shift+J/K range extension in multi-select mode.
        self._selection_anchor: Optional[QModelIndex] = None
        # VP-P5: parsed query terms of the active search (empty = default
        # view).  Kept on the host so a manager rebuild can re-apply it.
        self._search_terms: list[str] = []

        self.setObjectName('tab-tree')

        self._model = TabTreeModel(manager, parent=self)
        self._delegate = TabTreeItemDelegate(self)
        self._view = _TabTreeView(self)
        self._view.setObjectName('tab-tree-view')
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        # Pinned-tab drops (ADR 0008 § "Pinned-tab drop target").
        self._view.setAcceptDrops(True)
        self._view.setDragDropMode(
            QAbstractItemView.DragDropMode.DropOnly)
        self._view.setDropIndicatorShown(False)

        self._view.setHeaderHidden(True)
        self._view.setIndentation(18)
        self._view.setUniformRowHeights(False)
        self._view.setAnimated(True)
        self._view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._view.setAlternatingRowColors(True)
        # Group rows expose a minimal context menu (VP-A1 affinity toggle).
        self._view.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(
            self._on_context_menu_requested)

        self._placeholder = QLabel("No tabs in this workspace", self)
        self._placeholder.setObjectName('tab-tree-placeholder')
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._view)
        self._stack.addWidget(self._placeholder)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # VP-P5: in-panel keyword search.  The line edit sits above the
        # group/tab list and filters live on every keystroke.
        self.search_input = QLineEdit(self)
        self.search_input.setObjectName('tab-tree-search')
        self.search_input.setPlaceholderText("Search tabs…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_input)

        layout.addWidget(self._stack)

        self._refresh_empty_state()

        # Delegate interactions.
        self._delegate.fold_toggled.connect(self._model.toggle_folded)
        self._delegate.fold_toggled.connect(self.fold_toggled)
        self._delegate.audio_toggled.connect(self.audio_toggled)
        self._delegate.close_requested.connect(self.close_requested)
        self._delegate.pin_toggled.connect(self.pin_toggled)
        self._delegate.activated.connect(self.activated)
        self._delegate.selection_toggled.connect(self._on_selection_toggled)
        self._delegate.selection_range_requested.connect(
            self._on_selection_range_requested)
        # Auto-locate (VP-D5): the bridge emits ``group_focus_requested`` and
        # the host owns the actual scrolling / highlighting.
        self.group_focus_requested.connect(self.focus_group_and_tab)

        # Keyboard selection (J/K).  These are a *secondary* affordance: the
        # host only emits ``activated``; the bridge turns that into a
        # ``QTabWidget`` switch and the resulting ``current_tab_changed``
        # flows back to update the highlight.
        self._shortcuts: list[QShortcut] = []
        for keyseq, delta in (('j', +1), ('k', -1)):
            shortcut = QShortcut(QKeySequence(keyseq), self._view)
            shortcut.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(
                lambda d=delta: self._move_selection(d))
            self._shortcuts.append(shortcut)

        # Refresh when the manager signals a mutation.
        # TODO(v0.2): Replace wholesale rebuild with fine-grained inserts/removes.
        self._manager.changed.connect(self._on_manager_changed)

    def _on_search_changed(self, text: str) -> None:
        """Filter the tree live as the user types (VP-P5)."""
        self._search_terms = search.parse_query(text)
        self._apply_search()

    def _apply_search(self) -> None:
        """Push the current query into the model and refresh the stack.

        An empty query restores the normal grouped view through the regular
        rebuild path; a non-empty query renders only the hits.
        """
        workspace = self._manager.get_active()
        if not self._search_terms or workspace is None:
            self._model.set_search_hits(None)
        else:
            hits = search.search_workspace(workspace, self._search_terms)
            self._model.set_search_hits(hits)
        self._refresh_empty_state()

    def _on_manager_changed(self) -> None:
        """Rebuild the tree from the active workspace, keeping selection.

        A model reset invalidates every QModelIndex, so the current row is
        captured by id (tab first, then group) with its row as a fallback.
        The selection is restored only when the rebuilt tree still has the
        row, so switching to an unrelated workspace never points at stale
        data.
        """
        current = self._view.currentIndex()
        tab_id = self._model.tab_id_for_index(current)
        group = self._model.group_for_index(current)
        group_id = group.group_id if group is not None else None
        row = current.row() if current.isValid() else -1

        self._model.set_workspace(self._manager.get_active())
        if self._search_terms:
            self._apply_search()
        else:
            self._refresh_empty_state()

        self._restore_current_selection(tab_id, group_id, row)

    def _restore_current_selection(
            self,
            tab_id: Optional[int],
            group_id: Optional[uuid.UUID],
            row: int,
    ) -> None:
        """Restore the tree's current row after a model reset."""
        index = QModelIndex()
        if tab_id is not None:
            index = self._model.index_for_tab_id(tab_id)
        if not index.isValid() and group_id is not None:
            index = self._model._index_for_group_id(group_id)
        if (not index.isValid() and row >= 0
                and row < self._model.rowCount(QModelIndex())):
            index = self._model.index(row, 0)
        if index.isValid():
            self._view.setCurrentIndex(index)

    # --- group context menu (VP-A1) ---

    def _on_context_menu_requested(self, pos: QPoint) -> None:
        """Show the Group context menu for the row under *pos*."""
        index = self._view.indexAt(pos)
        group = self._model.group_for_index(index)
        if group is None:
            return
        menu = QMenu(self._view)
        label = (
            '窗口亲和：已启用'
            if group.affinity_enabled
            else '窗口亲和：已禁用'
        )
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(group.affinity_enabled)
        action.triggered.connect(
            lambda checked=False, g=group: self._set_group_affinity(
                g, checked))
        menu.exec(self._view.viewport().mapToGlobal(pos))

    def _set_group_affinity(self, group: Group, enabled: bool) -> None:
        """Persist the per-group affinity toggle for *group*."""
        workspace = self._manager.get_active()
        if workspace is None:
            return
        self._manager.set_group_affinity_enabled(
            workspace.id, group.group_id, enabled)

    def focus_group_and_tab(
            self,
            group_id: Optional[uuid.UUID],
            tab_id: int,
            *,
            highlight_timeout_ms: int = 2500,
    ) -> None:
        """Scroll to *group_id* / *tab_id* and highlight the tab (VP-D5).

        The group header is expanded and brought into view first, then the
        tab row, before the transient highlight is applied.  A ``None``
        *group_id* handles a standalone tab.
        """
        tab_index = self._model.focus_group_and_tab(group_id, tab_id)
        if group_id is not None:
            group_index = self._model._index_for_group_id(group_id)
            if group_index.isValid():
                self._view.scrollTo(
                    group_index,
                    QAbstractItemView.ScrollHint.EnsureVisible,
                )
        if not tab_index.isValid():
            return
        self._view.scrollTo(
            tab_index, QAbstractItemView.ScrollHint.EnsureVisible)
        self._model.highlight_tab(tab_id, timeout_ms=highlight_timeout_ms)

    def _move_selection(self, delta: int) -> None:
        """Move the tree selection by *delta* rows and activate the result."""
        direction = (
            QAbstractItemView.CursorAction.MoveDown
            if delta > 0
            else QAbstractItemView.CursorAction.MoveUp
        )
        # ``moveCursor`` only *computes* the target index, so we have to
        # apply it explicitly before treating it as the selection.
        index = self._view.moveCursor(
            direction, Qt.KeyboardModifier.NoModifier)
        if not index.isValid():
            return
        self._view.setCurrentIndex(index)
        self._view.scrollTo(index)
        # Use a dedicated "keyboard" signal so the bridge can keep focus
        # in the tree for iterative J/K navigation instead of yanking
        # it back to the webview on every keypress.
        self.keyboard_activated.emit(index)

    # --- multi-select (VP-F4) ---

    def model(self) -> TabTreeModel:
        """Return the backing model (used by the ``:vp-select-*`` commands)."""
        return self._model

    def set_selection_mode(self, enabled: bool) -> None:
        """Enable or disable the model's multi-select mode."""
        self._model.set_selection_mode(enabled)
        if not enabled:
            self._selection_anchor = None

    def selection_mode(self) -> bool:
        """Return whether multi-select mode is currently active."""
        return self._model.selection_mode()

    def _toggle_selection_mode(self) -> None:
        """Flip multi-select mode (Ctrl+Space shortcut)."""
        self.set_selection_mode(not self._model.selection_mode())

    def _on_selection_toggled(self, index: QModelIndex) -> None:
        """Toggle the model selection for the tab behind a clicked row."""
        tab_id = self._model.tab_id_for_index(index)
        if tab_id is None:
            return
        selected = tab_id in self._model.selected_tab_ids()
        self._model.set_selected(tab_id, not selected)
        # Remember the last plain toggle as the anchor for a later
        # Shift-click range extension.
        self._selection_anchor = index
        self.selection_toggled.emit(index)

    def _on_selection_range_requested(self, index: QModelIndex) -> None:
        """Extend the selection from the anchor to a Shift-clicked row."""
        anchor = self._selection_anchor
        if anchor is None or not anchor.isValid():
            anchor = self._view.currentIndex()
        if not anchor.isValid():
            self._on_selection_toggled(index)
            return
        self._view.setCurrentIndex(index)
        self._model.select_range(self._tab_ids_between(anchor, index), True)
        self.selection_toggled.emit(index)

    def _extend_selection(self, delta: int) -> None:
        """Move the cursor by *delta* and extend the selection to it.

        Outside multi-select mode this falls back to the plain J/K tab
        switch so the shortcut keeps working.
        """
        if not self._model.selection_mode():
            self._move_selection(delta)
            return
        direction = (
            QAbstractItemView.CursorAction.MoveDown
            if delta > 0
            else QAbstractItemView.CursorAction.MoveUp
        )
        previous = self._view.currentIndex()
        index = self._view.moveCursor(
            direction, Qt.KeyboardModifier.NoModifier)
        if not index.isValid():
            return
        self._view.setCurrentIndex(index)
        self._view.scrollTo(index)
        anchor = (
            self._selection_anchor
            if self._selection_anchor is not None
            else previous
        )
        self._selection_anchor = anchor
        self._model.select_range(
            self._tab_ids_between(anchor, index), True)

    def _tab_ids_between(
            self,
            first: QModelIndex,
            last: QModelIndex,
    ) -> list[int]:
        """Return the tab ids in display order between two indices."""
        order = self._visible_tab_ids()
        first_id = self._model.tab_id_for_index(first)
        last_id = self._model.tab_id_for_index(last)
        if first_id is None or last_id is None:
            return [last_id] if last_id is not None else []
        if first_id not in order or last_id not in order:
            return [last_id]
        start = order.index(first_id)
        end = order.index(last_id)
        low, high = sorted((start, end))
        return order[low:high + 1]

    def _visible_tab_ids(self) -> list[int]:
        """Return every tab id in the model, in display order."""
        ids: list[int] = []

        def walk(parent: QModelIndex) -> None:
            for row in range(self._model.rowCount(parent)):
                child = self._model.index(row, 0, parent)
                tab_id = self._model.tab_id_for_index(child)
                if tab_id is not None:
                    ids.append(tab_id)
                walk(child)

        walk(QModelIndex())
        return ids

    def _return_focus_to_webview(self) -> None:
        """Focus the currently selected tab's webview, if reachable.

        The tree is a downstream view and never imports ``TabbedBrowser``; we
        walk the parent chain looking for a window exposing
        ``tabbed_browser`` (the ``MainWindow``), mirroring the bridge flow.
        """
        parent = self.parent()
        while parent is not None:
            tabbed_browser = getattr(parent, 'tabbed_browser', None)
            if tabbed_browser is not None:
                widget = tabbed_browser.widget.currentWidget()
                if widget is not None:
                    widget.setFocus(Qt.FocusReason.TabFocusReason)
                return
            parent = parent.parent()

    def _refresh_empty_state(self) -> None:
        """Show the placeholder when the active workspace is empty."""
        workspace = self._manager.get_active()
        empty = workspace is None or (
            not workspace.groups and not workspace.tabs)
        if self._search_terms:
            empty = self._model.rowCount(QModelIndex()) == 0
            self._placeholder.setText(
                "No matching tabs" if empty else "")
        else:
            self._placeholder.setText("No tabs in this workspace")
        self._stack.setCurrentIndex(1 if empty else 0)

    def set_filter(self, text: str) -> None:
        """Apply a title/URL filter to the tree (delegates to the model)."""
        self._model.set_filter(text)

    def view(self) -> QTreeView:
        """Return the inner ``QTreeView`` for focus routing."""
        return self._view
