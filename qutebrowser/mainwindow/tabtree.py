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
import contextlib
import enum
import functools
import types
import uuid
import weakref
from collections.abc import Callable, Iterable, Iterator
from typing import Any, NamedTuple, Optional

from qutebrowser.qt.core import (
    QAbstractItemModel,
    QEvent,
    QMimeData,
    QObject,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from qutebrowser.qt.gui import (
    QColor,
    QContextMenuEvent,
    QFocusEvent,
    QFontMetrics,
    QHelpEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from qutebrowser.qt.widgets import (
    QAbstractItemView,
    QFrame,
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

from qutebrowser.mainwindow import panelglyphs
from qutebrowser.mainwindow import panelstyle
from qutebrowser.mainwindow.panelstyle import (Palette, best_black_or_white,
                                               blend)
from qutebrowser.keyinput import modeman
from qutebrowser.mainwindow.pinpanel import PINNED_TAB_MIME
from qutebrowser.utils import message, objreg, usertypes
from qutebrowser.utils.qtutils import weak_slot
from qutebrowser.workspaces import search
from qutebrowser.workspaces.manager import WorkspaceManager, WorkspaceMoveError
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
ReadingStatusRole = Qt.ItemDataRole.UserRole + 15
SearchTermsRole = Qt.ItemDataRole.UserRole + 16

#: MIME type of a tab node dragged out of the TabTree (ticket #6).
#:
#: The payload is ``"<workspace-uuid>:<tab-id>:<kind>"`` where ``<kind>`` is
#: ``branch`` (the node has descendants, which move with it) or ``leaf``.
#: See :func:`encode_tab_node` / :func:`decode_tab_node`.
TAB_NODE_MIME = 'application/x-qutebrowser-tabtree-node'

_NODE_KIND_BRANCH = 'branch'
_NODE_KIND_LEAF = 'leaf'


class TabNodeRef(NamedTuple):

    """A decoded :data:`TAB_NODE_MIME` payload."""

    workspace_id: uuid.UUID
    tab_id: int
    branch: bool


def encode_tab_node(workspace_id: uuid.UUID, tab_id: int, *,
                    branch: bool) -> bytes:
    """Return the :data:`TAB_NODE_MIME` payload for a dragged tab node."""
    kind = _NODE_KIND_BRANCH if branch else _NODE_KIND_LEAF
    return f'{workspace_id}:{tab_id}:{kind}'.encode('ascii')


def decode_tab_node(mime_data: QMimeData) -> Optional[TabNodeRef]:
    """Decode a :data:`TAB_NODE_MIME` payload, or return ``None``.

    Anything malformed (missing format, wrong field count, a bad UUID, a
    non-numeric tab id or an unknown kind) yields ``None`` rather than an
    exception, so a stray drag can never crash a drop handler.
    """
    if not mime_data.hasFormat(TAB_NODE_MIME):
        return None
    raw = mime_data.data(TAB_NODE_MIME).data().decode('ascii', 'replace')
    fields = raw.split(':')
    if len(fields) != 3:
        return None
    workspace_str, tab_str, kind = fields
    if not tab_str.isdecimal() or kind not in (_NODE_KIND_BRANCH,
                                               _NODE_KIND_LEAF):
        return None
    try:
        workspace_id = uuid.UUID(workspace_str)
    except ValueError:
        return None
    return TabNodeRef(workspace_id, int(tab_str), kind == _NODE_KIND_BRANCH)


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


def _free_tab_id(occupied: set[int], preferred: int) -> int:
    """Return *preferred* when free, else the smallest unused positive id."""
    if preferred not in occupied:
        return preferred
    candidate = 1
    while candidate in occupied:
        candidate += 1
    return candidate


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

#: Tab roles that resolve to text (title, URL, icon, reading, a11y, open).
_TAB_TEXT_ROLES = frozenset({
    OpenStatusRole,
    ReadingStatusRole,
    SearchTermsRole,
    Qt.ItemDataRole.DisplayRole,
    Qt.ItemDataRole.ToolTipRole,
    Qt.ItemDataRole.DecorationRole,
    Qt.ItemDataRole.AccessibleTextRole,
    Qt.ItemDataRole.AccessibleDescriptionRole,
    UrlRole,
})


#: Reading-status glyph per ``WorkspaceTab.reading_status`` (tab rows).
_READING_GLYPHS = {
    'FRESH': panelglyphs.Glyph.FRESH,
    'ACTIVE': panelglyphs.Glyph.ACTIVE,
    'DONE': panelglyphs.Glyph.DONE,
}

#: Resolved group-state glyph (``GroupStateRole``) on group rows.
_GROUP_GLYPHS = {
    'OPEN': panelglyphs.Glyph.OPEN,
    'SUSPENDED': panelglyphs.Glyph.SUSPENDED,
    'ARCHIVED': panelglyphs.Glyph.ARCHIVED,
}


def _reading_color(palette: Palette, glyph: panelglyphs.Glyph) -> QColor:
    """Return the palette colour of a reading-status *glyph*."""
    return {
        panelglyphs.Glyph.FRESH: palette.fresh,
        panelglyphs.Glyph.ACTIVE: palette.active,
        panelglyphs.Glyph.DONE: palette.done,
    }[glyph]


def _group_color(palette: Palette, glyph: panelglyphs.Glyph) -> QColor:
    """Return the palette colour of a group-state *glyph*."""
    return {
        panelglyphs.Glyph.OPEN: palette.open,
        panelglyphs.Glyph.SUSPENDED: palette.suspended,
        panelglyphs.Glyph.ARCHIVED: palette.archived,
    }[glyph]


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
        ReadingStatusRole: Role id for a tab's ``reading_status`` (str):
            ``'FRESH'``, ``'ACTIVE'`` or ``'DONE'``.  Group rows report
            ``None``.

    Accessibility (ADR 0008 § "Screen reader contract"): every row sets
    ``AccessibleTextRole`` (the spoken label) and
    ``AccessibleDescriptionRole`` (the state), and ``ToolTipRole`` stays as a
    fallback for older ATs.  See :meth:`_group_accessible_description` and
    :meth:`_tab_accessible_description`.
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
    ReadingStatusRole = ReadingStatusRole
    SearchTermsRole = SearchTermsRole

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
        # Lower-cased query terms, underlined in the title (ADR § Search).
        self._search_terms: list[str] = []
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

    def set_search_terms(self, terms: list[str]) -> None:
        """Set the query terms whose matches the delegate underlines."""
        self._search_terms = list(terms)
        self._emit_rows_changed()

    def _emit_rows_changed(self) -> None:
        """Repaint every visible row after a view-only change."""
        if self.rowCount(QModelIndex()):
            self.dataChanged.emit(
                self.index(0, 0, QModelIndex()),
                self.index(self.rowCount(QModelIndex()) - 1, 0,
                           QModelIndex()))

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
            timer.timeout.connect(
                weak_slot(self._clear_highlight, tab_id))
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
        flags = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                 | Qt.ItemFlag.ItemIsDropEnabled)
        if self._workspace_tab_for_index(index) is not None:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        return flags

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

    @staticmethod
    def _has_live_tab(url: str) -> bool:
        """Return whether a live browser tab currently shows *url*.

        Reuses the bridge's window-registry scan so a pinned drop agrees with
        what :func:`qutebrowser.mainwindow.workspacebridge.reopen_tab` treats
        as live.  The import is local because ``workspacebridge`` imports
        this module at load time.
        """
        if not url:
            return False
        from qutebrowser.mainwindow.workspacebridge import (
            _find_live_tab_by_url)
        return _find_live_tab_by_url(url) is not None

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

    # --- tab-node drag & drop (ticket #6) ---

    def mimeTypes(self) -> list[str]:  # noqa: N802 - Qt naming
        return [PINNED_TAB_MIME, TAB_NODE_MIME]

    def supportedDragActions(self) -> Qt.DropAction:  # noqa: N802
        return Qt.DropAction.MoveAction

    def supportedDropActions(self) -> Qt.DropAction:  # noqa: N802
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

    def mimeData(  # noqa: N802 - Qt naming
            self,
            indexes: Iterable[QModelIndex],
    ) -> Optional[QMimeData]:
        """Describe a single dragged tab row as a :data:`TAB_NODE_MIME` payload.

        Group rows (and multi-row drags) return ``None`` so Qt starts no drag.
        """
        indexes = list(indexes)
        if len(indexes) != 1 or self._workspace is None:
            return None
        tab = self._workspace_tab_for_index(indexes[0])
        if tab is None:
            return None
        mime = QMimeData()
        # Branch-ness is read from the current model state: a node with
        # descendants moves as a branch, a leaf moves alone.
        branch = any(child.parent_id == tab.tab_id
                     for child in self._workspace.tabs)
        mime.setData(TAB_NODE_MIME, encode_tab_node(
            self._workspace.id, tab.tab_id, branch=branch))
        return mime

    @staticmethod
    def _node_for_mime(mime_data: QMimeData) -> Optional[tuple[uuid.UUID, int]]:
        """Decode ``(workspace id, tab id)`` from a tab-node drag, if any."""
        ref = decode_tab_node(mime_data)
        if ref is None:
            return None
        return ref.workspace_id, ref.tab_id

    def _node_drop_target(
            self,
            index: QModelIndex,
    ) -> Optional[tuple[Optional[uuid.UUID], Optional[int]]]:
        """Return ``(group_id, parent_tab_id)`` for a tab-node drop on *index*.

        A group row targets that group at top level.  A tab row targets the
        tab's own group and *parent* (the dropped node becomes its sibling).
        The root (no row) targets a standalone top-level position.
        """
        group = self.group_for_index(index)
        if group is not None:
            return (group.group_id, None)
        tab = self._workspace_tab_for_index(index)
        if tab is not None:
            return (tab.group_id, tab.parent_id)
        return (None, None)

    def _node_drop_valid(
            self,
            data: QMimeData,
            action: Qt.DropAction,
            parent: QModelIndex,
    ) -> Optional[tuple[uuid.UUID, int, Optional[uuid.UUID], Optional[int]]]:
        """Validate a tab-node drop; return the move arguments or ``None``.

        The result is ``(source workspace id, tab id, group id, parent id)``.
        A drop is refused when the source is the active workspace (the tree
        only reorganises a workspace through the existing commands), when
        the node no longer exists, or when the target group is archived.
        """
        if action != Qt.DropAction.MoveAction or self._workspace is None:
            return None
        decoded = self._node_for_mime(data)
        if decoded is None:
            return None
        source_id, tab_id = decoded
        if source_id == self._workspace.id:
            return None
        if self._manager.find_tab_by_id(source_id, tab_id) is None:
            return None
        target = self._node_drop_target(parent)
        if target is None:
            return None
        group_id, parent_id = target
        if group_id is not None:
            group = next((g for g in self._workspace.groups
                          if g.group_id == group_id), None)
            if group is not None and group.archived:
                return None
        return source_id, tab_id, group_id, parent_id

    def _drop_node(
            self,
            data: QMimeData,
            action: Qt.DropAction,
            parent: QModelIndex,
    ) -> bool:
        """Move the dragged tab node (and its branch) onto *parent*."""
        valid = self._node_drop_valid(data, action, parent)
        if valid is None:
            return False
        source_id, tab_id, group_id, parent_id = valid
        assert self._workspace is not None
        source = self._manager.get_workspace(source_id)
        # A node with descendants moves as a branch (ADR 0008 ticket #6).
        branch = source is not None and any(
            tab.parent_id == tab_id for tab in source.tabs)
        try:
            self._manager.move_tab_to_workspace(
                source_id, tab_id, self._workspace.id,
                branch=branch, parent_id=parent_id, group_id=group_id)
        except WorkspaceMoveError as e:
            message.error(str(e))
            return False
        self._drop_target = None
        return True

    def canDropMimeData(  # noqa: N802 - Qt naming
            self,
            data,
            action: Qt.DropAction,
            row: int,
            column: int,
            parent: QModelIndex,
    ) -> bool:
        del row, column  # the hovered row identifies the target
        if self._workspace is None:
            return False
        if data.hasFormat(TAB_NODE_MIME):
            return self._node_drop_valid(data, action, parent) is not None
        if action != Qt.DropAction.CopyAction:
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
        if data.hasFormat(TAB_NODE_MIME):
            return self._drop_node(data, action, parent)
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
        # A pinned entry can outlive its browser tab (for example after a
        # restart, when only ``pinned.yml`` remembers it).  Never fabricate
        # an OPEN row: with no live tab the entry is stored SUSPENDED and
        # later reopens through the regular D1 path (``_activate_tab_id`` ->
        # ``workspacebridge.reopen_tab``).  A live tab, on the other hand,
        # means the dropped entry really is OPEN.
        open_status = 'OPEN' if self._has_live_tab(pinned.url) else 'SUSPENDED'
        # A pinned runtime id can collide with an existing workspace entry
        # (e.g. after an import).  ``add_tab`` does not reject duplicates, so
        # pick a genuinely free id rather than putting two rows under one id.
        occupied = {tab.tab_id for tab in workspace.tabs}
        tab_id = _free_tab_id(occupied, pinned.tab_id)
        entry = WorkspaceTab(
            tab_id=tab_id,
            parent_id=parent_tab_id,
            group_id=group_id,
            title=pinned.title,
            url=pinned.url,
            source=QUrl(pinned.url).host().lower(),
            favicon=favicon,
            open_status=open_status,
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
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return group.name
        if role == Qt.ItemDataRole.AccessibleDescriptionRole:
            return self._group_accessible_description(group)
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

    def _group_accessible_description(self, group: Group) -> str:
        """Return the spoken state of *group*: fold, ``open/total``, state.

        E.g. ``"group, 2/5 open, collapsed, suspended"``.
        """
        fold = 'collapsed' if self._group_folded(group) else 'expanded'
        state = self._group_state(group).lower()
        return f"group, {self._group_badge(group)} open, {fold}, {state}"

    def _group_folded(self, group: Group) -> bool:
        """Return the effective folded state of *group* (search forces open)."""
        return False if self.search_active else group.folded

    def _group_name_for(self, group_id: Optional[uuid.UUID]) -> str:
        """Return the name of the group *group_id*, or ``''`` if none."""
        if group_id is None or self._workspace is None:
            return ''
        for group in self._workspace.groups:
            if group.group_id == group_id:
                return group.name
        return ''

    def _tab_accessible_data(
            self,
            tab: WorkspaceTab,
            info: _TabInfo,
            kind: Kind,
            role: int,
    ) -> str:
        """Return the spoken label (text role) or state (description role)."""
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return info.title or info.url or '?'
        return self._tab_accessible_description(tab, info, kind)

    def _tab_accessible_description(
            self,
            tab: WorkspaceTab,
            info: _TabInfo,
            kind: Kind,
    ) -> str:
        """Return the spoken state of a tab row (ADR 0008 § Screen reader).

        E.g. ``"tab, github.com/issues/128, group client-A, reading done,
        open, loading, audio playing"``.
        """
        parts = ['tab']
        if info.url:
            parts.append(info.url)
        if kind == Kind.ParentTab:
            parts.append('parent')
        group_name = self._group_name_for(tab.group_id)
        if group_name:
            parts.append(f'group {group_name}')
        reading = getattr(tab, 'reading_status', 'FRESH') or 'FRESH'
        parts.append(f'reading {reading.lower()}')
        suspended = getattr(tab, 'open_status', 'SUSPENDED') == 'SUSPENDED'
        parts.append('suspended' if suspended else 'open')
        if info.status & Status.LOADING:
            parts.append('loading')
        if info.status & Status.AUDIBLE:
            parts.append('audio playing')
        if kind == Kind.ParentTab:
            parts.append(
                'collapsed' if self._folded_state.get(tab.tab_id, False)
                else 'expanded')
        return ', '.join(parts)

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

    def _tab_data(self, tab: WorkspaceTab, role: int, kind: Kind) -> Any:
        info = self._tab_info.get(tab.tab_id, _TabInfo())
        if role == KindRole:
            return kind
        if role == ActiveRole:
            return self._active_tab_id == tab.tab_id
        if role in _TAB_TEXT_ROLES:
            return self._tab_text_data(tab, info, kind, role)
        if role == FoldedRole:
            return self._tab_folded(tab, kind)
        if role in (StatusRole, PinStateRole):
            return self._tab_status_data(tab, info, role)
        if role == SelectionRole:
            return tab.tab_id in self._selected
        if role == HighlightRole:
            return self._highlighted_tab_id == tab.tab_id
        if role in (GroupIdRole, DropTargetRole):
            return self._tab_relation_role(tab, role)
        return _TAB_STATIC_ROLES.get(role)

    def _tab_text_data(self, tab: WorkspaceTab, info: _TabInfo, kind: Kind,
                       role: int) -> Any:
        """Return the text-like roles: title/URL/icon, reading and a11y."""
        if role == OpenStatusRole:
            return getattr(tab, 'open_status', 'SUSPENDED')
        if role == SearchTermsRole:
            return list(self._search_terms)
        if role == ReadingStatusRole:
            return getattr(tab, 'reading_status', 'FRESH') or 'FRESH'
        if role in (Qt.ItemDataRole.AccessibleTextRole,
                    Qt.ItemDataRole.AccessibleDescriptionRole):
            return self._tab_accessible_data(tab, info, kind, role)
        return self._tab_visible_data(info, role)

    @staticmethod
    def _tab_status_data(tab: WorkspaceTab, info: _TabInfo, role: int) -> Any:
        """Return the status flags (StatusRole) or pin state (PinStateRole)."""
        if role == PinStateRole:
            return info.status & Status.PINNED != 0
        status = info.status
        if getattr(tab, 'open_status', 'SUSPENDED') == 'SUSPENDED':
            status |= Status.SUSPENDED
        return status

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
    #: Width of the hover bar on the left edge (ADR § Hover).
    HOVER_MARKER_WIDTH = 3
    #: Edge of the multi-select checkbox drawn in the left margin.
    SELECTION_INDICATOR_WIDTH = 14

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
        # Whether the tree has *keyboard* focus (ADR § Focus ring).  Set by
        # the host from the focus reason; mouse focus never shows the ring.
        self._keyboard_focus = False
        # The tree's current (cursor) row, kept persistent across resets.
        self._current = QPersistentModelIndex()

    def set_keyboard_focus(self, keyboard: bool) -> None:
        """Record whether the tree currently has keyboard focus."""
        self._keyboard_focus = keyboard

    def set_current_index(self, index: QModelIndex) -> None:
        """Record *index* as the tree's current (cursor) row."""
        self._current = QPersistentModelIndex(index)

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
        palette = Palette.from_config()
        rect = option.rect

        painter.save()

        # Row fill (ADR § Color scheme): selected > hover > plain row.  The
        # fill starts at the viewport edge so the indentation gutter of a
        # nested row is tinted too, not left in the palette base colour.
        if option.state & QStyle.StateFlag.State_Selected:
            fill = palette.selected_fill
        elif option.state & QStyle.StateFlag.State_MouseOver:
            fill = palette.hover_fill
        else:
            fill = palette.row_fill
        painter.fillRect(
            QRect(0, rect.y(), rect.right() + 1, rect.height()), fill)

        # Transient auto-locate / drop-target washes sit above the fill.
        if (bool(index.data(HighlightRole))
                or bool(index.data(DropTargetRole))):
            self._draw_transient_highlight(painter, option, index, palette)

        # Group running state (VP-D2): suspended and archived groups use the
        # muted text colour; ARCHIVED also carries its own glyph.
        text_color = palette.text
        if kind == Kind.Group and index.data(GroupStateRole) in (
                'SUSPENDED', 'ARCHIVED'):
            text_color = palette.muted_text
        painter.setPen(text_color)

        x = rect.x()

        # Chevron.
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

        # Favicon (tabs only), then the reading-status glyph (tabs only).
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

            glyph = _READING_GLYPHS.get(index.data(ReadingStatusRole))
            if glyph is not None:
                glyph_rect = QRect(x, rect.y(), panelglyphs.GLYPH_SIZE,
                                   rect.height())
                panelglyphs.draw_glyph(
                    painter, glyph, glyph_rect,
                    _reading_color(palette, glyph))
                x += panelglyphs.GLYPH_SIZE + self.GAP_ICON_TEXT

        # Title, trailing status/badge, hover close glyph and the active
        # accent are all delegated so the paint method stays small.
        self._draw_row_content(
            painter, option, index, kind, rect, x, text_color, palette)

        # Hover marker (shape, not colour alone): a 3 px bar on the left edge.
        # It is drawn after the content so the favicon cannot cover it.  The
        # active-tab / active-group accent takes over the same edge instead.
        if (option.state & QStyle.StateFlag.State_MouseOver
                and not index.data(ActiveRole)):
            painter.fillRect(
                QRect(rect.x(), rect.y(), self.HOVER_MARKER_WIDTH,
                      rect.height()),
                palette.hover_marker,
            )

        # Multi-select checkmark (VP-F4): drawn as an overlay in the left
        # margin so the title / favicon / badge layout is untouched.
        if bool(index.data(SelectionRole)):
            self._draw_selection_indicator(painter, rect, palette)

        self._draw_cursor(painter, option, index, palette)

        painter.restore()

    def _draw_cursor(
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
            palette: Palette,
    ) -> None:
        """Mark the cursor row by shape: focus ring, or a dotted outline.

        The ring (ADR § Focus ring) is shown only on keyboard focus, so a
        mouse click gives the tree focus without it.  A current row without
        keyboard focus gets a dotted outline instead, so it stays identifiable.
        """
        rect = option.rect
        if (self._keyboard_focus
                and option.state & QStyle.StateFlag.State_HasFocus):
            self._draw_focus_ring(painter, rect, palette)
        elif self._is_current(index):
            pen = QPen(palette.text)
            pen.setStyle(Qt.PenStyle.DotLine)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))

    def _is_current(self, index: QModelIndex) -> bool:
        """Return whether *index* is the tree's current (cursor) row."""
        return self._current.isValid() and QModelIndex(self._current) == index

    def _draw_focus_ring(
            self,
            painter: QPainter,
            rect: QRect,
            palette: Palette,
    ) -> None:
        """Draw the two-stroke focus ring: 2 px accent outside, 1 px fg inside.

        Radius is 2 px to match the rounded workspace monograms.
        """
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        outer = QRectF(rect).adjusted(1, 1, -2, -2)
        pen = QPen(palette.focus_outer)
        pen.setWidthF(2)
        painter.setPen(pen)
        painter.drawRoundedRect(outer, 2, 2)
        inner = QRectF(rect).adjusted(3, 3, -4, -4)
        pen = QPen(palette.focus_inner)
        pen.setWidthF(1)
        painter.setPen(pen)
        painter.drawRoundedRect(inner, 1, 1)
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

        # Audio icon hit rect (only for audible tabs).  The strip is right
        # aligned; the audio glyph follows the loading glyph, if shown.
        status = Status(int(index.data(StatusRole) or 0))
        if status & Status.AUDIBLE:
            fm: QFontMetrics = option.fontMetrics
            audio_text = _Icon.AUDIBLE.value
            audio_width = fm.horizontalAdvance(audio_text)
            status_text = self._status_text(status)
            full_width = fm.horizontalAdvance(status_text)
            strip_x = (
                rect.right() - self.RIGHT_MARGIN - close_reserve
                - pin_reserve - full_width
            )
            if status & Status.LOADING:
                strip_x += fm.horizontalAdvance(_Icon.LOADING.value)
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
            palette: Palette,
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
        group_glyph = _GROUP_GLYPHS.get(index.data(GroupStateRole)) \
            if kind == Kind.Group else None

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
        glyph_width = (
            panelglyphs.GLYPH_SIZE + self.GAP_STRIP
            if group_glyph is not None
            else 0
        )
        trailing_width = (
            max(strip_width, badge_width + glyph_width)
            + close_reserve + pin_reserve
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
        painter.setPen(text_color)
        painter.drawText(
            x,
            rect.y(),
            text_width,
            rect.height(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided,
        )
        if kind in (Kind.Tab, Kind.ParentTab):
            self._draw_search_underlines(
                painter, fm, x, rect, elided, index, palette)

        right_edge = (
            rect.right() - self.RIGHT_MARGIN - close_reserve - pin_reserve)
        # Tab status strip (loading / audio / suspended), right aligned.
        if status_text:
            self._draw_status_strip(
                painter, fm, rect, right_edge - fm.horizontalAdvance(
                    status_text), status, palette)
        # Group badge (dimmed, contrast-checked) with the state glyph to
        # its left.
        if badge_text:
            drawn_width = fm.horizontalAdvance(badge_text)
            text_x = right_edge - drawn_width
            painter.setPen(palette.muted_text)
            painter.drawText(
                text_x,
                rect.y(),
                drawn_width,
                rect.height(),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                badge_text,
            )
            if group_glyph is not None:
                glyph_rect = QRect(
                    text_x - self.GAP_STRIP - panelglyphs.GLYPH_SIZE,
                    rect.y(),
                    panelglyphs.GLYPH_SIZE,
                    rect.height(),
                )
                panelglyphs.draw_glyph(
                    painter, group_glyph, glyph_rect,
                    _group_color(palette, group_glyph))

        self._finish_row(
            painter, option, index, rect, is_tab, kind, close_rect, pin_rect,
            is_pinned, palette,
        )

    def _draw_status_strip(
            self,
            painter: QPainter,
            fm: QFontMetrics,
            rect: QRect,
            x: int,
            status: Status,
            palette: Palette,
    ) -> None:
        """Draw the status glyphs left to right, each in its own colour."""
        for glyph, colour_key in self._status_segments(status):
            painter.setPen(getattr(palette, colour_key))
            width = fm.horizontalAdvance(glyph)
            painter.drawText(
                x,
                rect.y(),
                width,
                rect.height(),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                glyph,
            )
            x += width

    def _draw_search_underlines(
            self,
            painter: QPainter,
            fm: QFontMetrics,
            x: int,
            rect: QRect,
            shown: str,
            index: QModelIndex,
            palette: Palette,
    ) -> None:
        """Underline the search-query matches in the drawn title (ADR § 2).

        *shown* is the elided title actually drawn at *x*; only matches in it
        are underlined.  The underline uses the accent colour.
        """
        terms = index.data(SearchTermsRole) or []
        if not terms or not shown:
            return
        lowered = shown.lower()
        baseline = rect.y() + rect.height() - 4
        painter.save()
        painter.setPen(palette.accent)
        for term in terms:
            start = lowered.find(term)
            while term and start != -1:
                left = x + fm.horizontalAdvance(shown[:start])
                width = fm.horizontalAdvance(shown[start:start + len(term)])
                painter.drawLine(left, baseline, left + width - 1, baseline)
                start = lowered.find(term, start + len(term))
        painter.restore()

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
            palette: Palette,
    ) -> None:
        """Draw the close/pin glyphs and the active-tab/group left accent."""
        # Hover close glyph and its hit-rect cache.
        if close_rect is not None:
            self._draw_close(painter, close_rect, palette.muted_text)
            self._remember_close_rect(index, close_rect)
        else:
            self._last_close_rect.pop(index, None)

        # Pin glyph: accent when pinned, muted otherwise (the latter only
        # happens on hover).
        if pin_rect is not None:
            pin_color = palette.pin if is_pinned else palette.muted_text
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
            painter.fillRect(accent_rect, palette.accent)
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
            palette: Palette,
    ) -> None:
        """Draw the multi-select checkbox, with a stroked check, in the margin.

        The check is a 2 px stroke rather than a text glyph: a small text
        check can read as a solid box at this size.
        """
        size = self.SELECTION_INDICATOR_WIDTH
        y = rect.y() + (rect.height() - size) // 2
        box = QRect(
            rect.x() + self.ACTIVE_ACCENT_WIDTH + 2, y, size, size)
        painter.save()
        painter.fillRect(box, palette.accent)
        pen = QPen(best_black_or_white([palette.accent]))
        pen.setWidthF(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        left, top = box.x(), box.y()
        check = QPainterPath()
        check.moveTo(left + size * 0.25, top + size * 0.52)
        check.lineTo(left + size * 0.43, top + size * 0.70)
        check.lineTo(left + size * 0.76, top + size * 0.32)
        painter.drawPath(check)
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
            palette: Palette,
    ) -> None:
        """Paint the auto-locate / drop-target row washes."""
        if bool(index.data(HighlightRole)):
            painter.fillRect(option.rect, self._highlight_color(palette))
        if bool(index.data(DropTargetRole)):
            target = QColor(palette.accent)
            target.setAlpha(70)
            painter.fillRect(option.rect, target)

    @staticmethod
    def _highlight_color(palette: Palette) -> QColor:
        """Return the faint auto-locate wash (base blended toward accent)."""
        return blend(palette.base, palette.accent, 0.18)

    @staticmethod
    def _status_segments(status: Status) -> list[tuple[str, str]]:
        """Return ``(glyph, colour key)`` for each status-strip item.

        Colour keys name a :class:`Palette` attribute: ``'loading'`` (40 %),
        ``'text'`` (audio) and ``'suspended'`` (50 %), per ADR 0008 § "Color
        scheme".  PINNED is its own affordance (the P glyph), not in the strip.
        """
        segments = []
        if status & Status.LOADING:
            segments.append((_Icon.LOADING.value, 'loading'))
        if status & Status.AUDIBLE:
            segments.append((_Icon.AUDIBLE.value, 'text'))
        if status & Status.SUSPENDED:
            segments.append((_Icon.SUSPENDED.value, 'suspended'))
        return segments

    @classmethod
    def _status_text(cls, status: Status) -> str:
        return ''.join(glyph for glyph, _ in cls._status_segments(status))


class _TabTreeView(QTreeView):

    """QTreeView which accepts pinned-entry and tab-node drags.

    Native branch arrows are suppressed: the delegate draws the custom 9 px
    fold chevron (ADR 0008 § "Fold indicator").

    Pinned entries from the PIN panel drop with ``CopyAction``; tab nodes
    dragged out of this tree (ticket #6) drop with ``MoveAction``.  The view
    drives the model's ``canDropMimeData`` / ``dropMimeData`` itself so it
    can reject invalid rows *before* Qt paints a drop indicator, and so the
    hovered row can carry :data:`DropTargetRole` while the drag is in
    progress (accept = highlighted, reject = no-drop cursor and no
    highlight).
    """

    def drawBranches(  # noqa: N802 - Qt naming  # type: ignore[override]
            self,
            painter: QPainter,
            rect: QRect,
            index: QModelIndex,
    ) -> None:
        """Draw no native branch indicator (see the class docstring)."""

    @staticmethod
    def _event_pos(event) -> QPoint:
        """Return the event position as a QPoint (Qt5/Qt6 compatible)."""
        try:
            return event.position().toPoint()
        except AttributeError:  # Qt5
            return event.pos()

    @staticmethod
    def _drop_action_for(mime_data: QMimeData) -> Optional[Qt.DropAction]:
        """Return the drop action our model expects for *mime_data*, if any."""
        if mime_data.hasFormat(TAB_NODE_MIME):
            return Qt.DropAction.MoveAction
        if mime_data.hasFormat(PINNED_TAB_MIME):
            return Qt.DropAction.CopyAction
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        action = self._drop_action_for(event.mimeData())
        if action is None:
            super().dragEnterEvent(event)
            return
        event.setDropAction(action)
        event.accept()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        action = self._drop_action_for(event.mimeData())
        if action is None:
            super().dragMoveEvent(event)
            return
        index = self.indexAt(self._event_pos(event))
        model = self.model()
        if model.canDropMimeData(
                event.mimeData(), action, -1, -1, index):
            model.set_drop_target(index)
            event.setDropAction(action)
            event.accept()
        else:
            model.set_drop_target(QModelIndex())
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        model = self.model()
        if model is not None:
            model.set_drop_target(QModelIndex())
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        action = self._drop_action_for(event.mimeData())
        if action is None:
            super().dropEvent(event)
            return
        index = self.indexAt(self._event_pos(event))
        model = self.model()
        dropped = model.dropMimeData(
            event.mimeData(), action, -1, -1, index)
        model.set_drop_target(QModelIndex())
        if dropped:
            event.setDropAction(action)
            event.accept()
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
        # ADR 0008 § "Screen reader contract".
        self.setAccessibleName('Tabs')
        self.setAccessibleDescription(
            'Tabs of the active workspace, grouped; use arrow keys to move, '
            'Right and Left to expand and collapse.')

        self._model = TabTreeModel(manager, parent=self)
        self._delegate = TabTreeItemDelegate(self)
        self._view = _TabTreeView(self)
        self._view.setObjectName('tab-tree-view')
        self._view.setAccessibleName('Tabs')
        self._view.setAccessibleDescription(self.accessibleDescription())
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        # Pinned-tab drops (ADR 0008 § "Pinned-tab drop target") and tab-node
        # moves (ticket #6) both go through Qt's built-in drag and drop.
        self._view.setAcceptDrops(True)
        self._view.setDragDropMode(
            QAbstractItemView.DragDropMode.DragDrop)
        self._view.setDragEnabled(True)
        self._view.setDropIndicatorShown(False)

        self._view.setHeaderHidden(True)
        self._view.setIndentation(18)
        # No branch column at the root: top-level rows (standalone tabs and
        # group headers) start at the left edge, as in ADR 0008's wireframes.
        self._view.setRootIsDecorated(False)
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
        self._view.setAlternatingRowColors(False)
        self._apply_view_palette()
        panelstyle.connect_colors_changed(self._on_colors_changed)
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
        self.search_input.setAccessibleName('Search tabs')
        self.search_input.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_input)

        # ADR 0008 § Keyboard navigation: the Shift+F2 close-branch hint.
        # Hidden until opened; sits between the search field and the tree.
        self._hint_open = False
        self._hint_tab_id: Optional[int] = None
        self._close_hint = QFrame(self)
        self._close_hint.setObjectName('tab-tree-close-hint')
        self._close_hint.setFrameShape(QFrame.Shape.StyledPanel)
        hint_layout = QVBoxLayout(self._close_hint)
        hint_layout.setContentsMargins(6, 4, 6, 4)
        hint_layout.setSpacing(2)
        self._hint_workspace = QLabel(self._close_hint)
        self._hint_tab = QLabel(self._close_hint)
        for label in (self._hint_workspace, self._hint_tab):
            label.setTextFormat(Qt.TextFormat.PlainText)
            hint_layout.addWidget(label)
        self._close_hint.hide()
        layout.insertWidget(1, self._close_hint)
        self._apply_hint_style()

        layout.addWidget(self._stack)
        # Tab cycles search -> tree in order (ADR § "Panel focus").
        self.setTabOrder(self.search_input, self._view)

        # Keyboard focus ring (ADR § "Focus ring") for the field and the tree.
        panelstyle.install_focus_ring(self.search_input, field=True)
        panelstyle.install_focus_ring(self._view)

        self._refresh_empty_state()

        # Delegate interactions.
        self._delegate.fold_toggled.connect(self._model.toggle_folded)
        # After the model has flipped the fold, mirror it on the view so the
        # children actually show or hide (ADR § "Fold indicator").
        self._delegate.fold_toggled.connect(self._on_fold_toggled)
        # Expansion follows the folded flags: rebuilds and searches re-apply
        # it, so group members are visible unless the user folded them.
        self._model.modelReset.connect(self._sync_expansion)
        selection = self._view.selectionModel()
        if selection is not None:
            selection.currentChanged.connect(self._on_current_changed)
        # Keyboard navigation and focus tracking for the tree (ADR §
        # "Keyboard navigation"); see :meth:`eventFilter`.
        self._view.installEventFilter(self)
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
                weak_slot(self._move_selection, delta))
            self._shortcuts.append(shortcut)

        # The model was populated before the reset hook was connected, so
        # apply the initial expansion explicitly.
        self._sync_expansion()

        # Refresh when the manager signals a mutation.
        # TODO(v0.2): Replace wholesale rebuild with fine-grained inserts/removes.
        self._manager.changed.connect(self._on_manager_changed)

    def _apply_view_palette(self) -> None:
        """Paint the tree's bare background with the panel base colour."""
        palette = Palette.from_config()
        view_palette = QPalette(self._view.palette())
        for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Window):
            view_palette.setColor(role, palette.base)
        view_palette.setColor(QPalette.ColorRole.Text, palette.text)
        self._view.setPalette(view_palette)
        self._view.viewport().setPalette(view_palette)

    def _on_colors_changed(self, option: str) -> None:
        """Re-apply the panel colours after a ``colors.*`` change."""
        if option.startswith('colors.'):
            self._apply_view_palette()
            self._apply_hint_style()
            self._view.viewport().update()

    def _apply_hint_style(self) -> None:
        """Colour the close-branch hint from the panel palette (ADR § Color)."""
        palette = Palette.from_config()
        self._close_hint.setStyleSheet(
            f'QFrame#tab-tree-close-hint {{'
            f' background: {palette.search_fill.name()};'
            f' border: 1px solid {palette.focus_outer.name()}; }}'
            f' QFrame#tab-tree-close-hint QLabel {{'
            f' color: {palette.text.name()}; background: transparent; }}')

    # --- expansion, current row and keyboard focus ---

    def _sync_expansion(self) -> None:
        """Expand every group / parent row whose folded flag is clear."""
        def walk(parent: QModelIndex) -> None:
            for row in range(self._model.rowCount(parent)):
                child = self._model.index(row, 0, parent)
                if self._model.rowCount(child):
                    folded = bool(child.data(FoldedRole))
                    self._view.setExpanded(child, not folded)
                    walk(child)

        walk(QModelIndex())

    def _on_fold_toggled(self, index: QModelIndex) -> None:
        """Mirror a chevron / keyboard fold change onto the view."""
        if index.isValid() and self._model.rowCount(index):
            self._view.setExpanded(index, not bool(index.data(FoldedRole)))

    def _on_current_changed(
            self,
            current: QModelIndex,
            _previous: QModelIndex,
    ) -> None:
        """Keep the delegate's cursor-row marker in step with the view."""
        self._delegate.set_current_index(current)
        self._view.viewport().update()

    def _set_folded(self, index: QModelIndex, folded: bool) -> None:
        """Fold or unfold the group / parent row *index* from the keyboard."""
        if self._model.search_active or not self._model.rowCount(index):
            return
        if bool(index.data(FoldedRole)) != folded:
            self._model.toggle_folded(index)
        self._view.setExpanded(index, not folded)

    def _keyboard_navigate(self, key: int) -> bool:
        """Handle Left / Right / Return / Space on the tree.

        Returns ``True`` when the key was consumed.  Right expands a
        collapsed group or parent (or steps into its first child); Left
        collapses an expanded one (or steps to the parent).  Return and Space
        activate a tab row; a group header is a no-op (ADR § "Keyboard
        navigation").
        """
        index = self._view.currentIndex()
        if not index.isValid():
            return False
        has_children = self._model.rowCount(index) > 0
        if key == Qt.Key.Key_Right:
            if has_children and not self._view.isExpanded(index):
                self._set_folded(index, False)
            elif has_children:
                self._view.setCurrentIndex(self._model.index(0, 0, index))
            return True
        if key == Qt.Key.Key_Left:
            if has_children and self._view.isExpanded(index):
                self._set_folded(index, True)
            elif index.parent().isValid():
                self._view.setCurrentIndex(index.parent())
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if index.data(KindRole) != Kind.Group:
                self.activated.emit(index)
            return True
        return False

    def eventFilter(  # noqa: N802 - Qt naming
            self,
            obj: Optional[QObject],
            event: Optional[QEvent],
    ) -> bool:
        """Route the tree's keyboard, focus and keyboard-menu events."""
        if obj is not self._view or event is None:
            return super().eventFilter(obj, event)
        etype = event.type()
        if self._hint_open:
            # The hint owns the keyboard: nothing reaches the tree's rows.
            if etype == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            if etype == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
                self._on_close_hint_key(event)
                return True
        if isinstance(event, QFocusEvent):
            keyboard = (
                etype == QEvent.Type.FocusIn
                and event.reason() != Qt.FocusReason.MouseFocusReason
            )
            if etype in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
                self._delegate.set_keyboard_focus(keyboard)
                self._view.viewport().update()
        elif etype == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                if self._keyboard_navigate(event.key()):
                    return True
        elif (etype == QEvent.Type.ContextMenu
              and isinstance(event, QContextMenuEvent)
              and event.reason() == QContextMenuEvent.Reason.Keyboard):
            # The Menu key has no mouse position; open the menu on the
            # cursor row so the affinity toggle is reachable without a mouse.
            index = self._view.currentIndex()
            if index.isValid():
                rect = self._view.visualRect(index)
                self._on_context_menu_requested(rect.center())
                return True
        return super().eventFilter(obj, event)

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
        self._model.set_search_terms(self._search_terms)
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

    @contextlib.contextmanager
    def bulk_rebuild(self) -> Iterator[None]:
        """Suppress animation and keep the selection across a bulk rebuild.

        Used while recovering a crash journal (#7): many manager changes
        collapse into one model reset, which would otherwise animate every
        expand/collapse and stutter.  Inside the block the view is not
        animated and does not repaint; on exit the previous animation setting
        is restored and the current row is put back if it is still valid (by
        tab id, then group id, then row).
        """
        current = self._view.currentIndex()
        tab_id = self._model.tab_id_for_index(current)
        group = self._model.group_for_index(current)
        group_id = group.group_id if group is not None else None
        row = current.row() if current.isValid() else -1
        animated = self._view.isAnimated()
        self._view.setAnimated(False)
        self._view.setUpdatesEnabled(False)
        try:
            yield
        finally:
            self._view.setUpdatesEnabled(True)
            self._view.setAnimated(animated)
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
        # triggered(bool) lands as the trailing ``enabled`` argument.
        action.triggered.connect(
            weak_slot(self._set_group_affinity, group))
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
        self._sync_expansion()
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
        # Keep the current row on the row the user just acted on, matching
        # plain clicks and J/K range extension.
        self._view.setCurrentIndex(index)
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

    # --- group jump (ADR § Keyboard navigation, ``gh``) ---

    def focus_next_group(self) -> bool:
        """Move the current row to the next group header and focus the tree.

        "Next" is taken in display order from the top-level row holding the
        current row (so a tab inside group A moves to the group after A).
        Past the last group it wraps to the first; with no current row it
        starts at the top. Returns False when no group header is visible.
        """
        model = self._model
        root = QModelIndex()
        groups = [
            row for row in range(model.rowCount(root))
            if model.index(row, 0, root).data(KindRole) == Kind.Group
        ]
        if not groups:
            return False
        current = self._view.currentIndex()
        top = current
        while top.isValid() and top.parent().isValid():
            top = top.parent()
        start = top.row() if top.isValid() else -1
        later = [row for row in groups if row > start]
        target = model.index(later[0] if later else groups[0], 0, root)
        self._view.setCurrentIndex(target)
        self._view.scrollTo(
            target, QAbstractItemView.ScrollHint.EnsureVisible)
        self._view.setFocus(Qt.FocusReason.ShortcutFocusReason)
        return True

    # --- close-branch hint (ADR § Keyboard navigation, Shift+F2) ---

    @property
    def close_hint_open(self) -> bool:
        """Whether the Shift+F2 close-branch hint is showing."""
        return self._hint_open

    def show_close_hint(self) -> bool:
        """Show the close-branch hint for the current row.

        Returns False (and shows nothing) when the tree has no current row.
        Focus moves to the tree so the hint can own the keyboard.
        """
        index = self._view.currentIndex()
        if not index.isValid():
            return False
        workspace = self._manager.get_active()
        name = workspace.name if workspace is not None else ''
        title = index.data(Qt.ItemDataRole.DisplayRole) or ''
        # A group header has no tab to close; ``t`` is then a no-op.
        self._hint_tab_id = (
            None if index.data(KindRole) == Kind.Group
            else self._model.tab_id_for_index(index))
        self._hint_workspace.setText(f'[w] close workspace  {name}')
        self._hint_tab.setText(
            f'[t] close tab  {title}' if self._hint_tab_id is not None
            else '[t] close tab  (select a tab row)')
        self._hint_open = True
        self._set_hint_capture(True)
        self._close_hint.show()
        self._view.setFocus(Qt.FocusReason.ShortcutFocusReason)
        return True

    def hide_close_hint(self) -> None:
        """Dismiss the close-branch hint and hand keys back to the tree."""
        self._hint_open = False
        self._hint_tab_id = None
        self._close_hint.hide()
        self._set_hint_capture(False)

    def _set_hint_capture(self, active: bool) -> None:
        """Stop the workspace_panel parser binding keys while the hint is up.

        Without this, bound keys (``d``, ``r``, ``/``, ...) and unbound letters
        (``w``, ``t``) would be consumed by the mode before reaching the hint.
        The parser is optional so tests can build a tree without a mode manager.
        """
        try:
            mode_manager = modeman.instance(self._win_id)
        except (modeman.UnavailableError, objreg.RegistryUnavailableError):
            return
        parser = mode_manager.parsers.get(usertypes.KeyMode.workspace_panel)
        setter = getattr(parser, 'set_keys_captured', None)
        if setter is not None:
            setter(active)

    def _on_close_hint_key(self, event: QKeyEvent) -> None:
        """Run the hint action for ``w`` / ``t``; any other key dismisses."""
        key = event.key()
        tab_id = self._hint_tab_id
        plain = event.modifiers() == Qt.KeyboardModifier.NoModifier
        self.hide_close_hint()
        if not plain:
            return
        if key == Qt.Key.Key_W:
            self._close_workspace_tabs()
        elif key == Qt.Key.Key_T and tab_id is not None:
            self._close_tab(tab_id)

    def _close_tab(self, tab_id: int) -> None:
        """Close one tab through the same path as the row's close glyph."""
        index = self._model.index_for_tab_id(tab_id)
        if index.isValid():
            self.close_requested.emit(index)

    def _close_workspace_tabs(self) -> None:
        """Close every OPEN tab of the active workspace, after confirmation.

        Each tab goes through :attr:`close_requested`, the path the row's close
        glyph uses, so entries flip to SUSPENDED and stay in the workspace.
        """
        workspace = self._manager.get_active()
        if workspace is None:
            return
        tab_ids = [
            tab.tab_id for tab in workspace.tabs
            if tab.open_status == 'OPEN'
            and self._model.index_for_tab_id(tab.tab_id).isValid()
        ]
        if not tab_ids:
            message.info(f"Workspace '{workspace.name}' has no open tabs.")
            return
        message.confirm_async(
            title=f"Close workspace '{workspace.name}'?",
            text=(f"This closes {len(tab_ids)} open tab(s); their entries "
                  "stay in the workspace as suspended."),
            default=False,
            yes_action=weak_slot(self._close_tabs_confirmed, tab_ids,
                                 accepts=0),
        )

    def _close_tabs_confirmed(self, tab_ids: list[int]) -> None:
        """Close each of *tab_ids* that still has a row (confirmed by user)."""
        for tab_id in tab_ids:
            self._close_tab(tab_id)

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
