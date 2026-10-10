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

import enum
import uuid
from typing import Optional

from qutebrowser.qt.core import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QRect,
    QSize,
    Qt,
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
    QStackedWidget,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import Group, Workspace, WorkspaceTab


# Role ids shared between model and delegate.
KindRole = Qt.ItemDataRole.UserRole + 1
FoldedRole = Qt.ItemDataRole.UserRole + 2
StatusRole = Qt.ItemDataRole.UserRole + 3
BadgeRole = Qt.ItemDataRole.UserRole + 4
UrlRole = Qt.ItemDataRole.UserRole + 5
ActiveRole = Qt.ItemDataRole.UserRole + 6
PinStateRole = Qt.ItemDataRole.UserRole + 7


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
        BadgeRole: Role id for the group tab-count badge (int).
        UrlRole: Role id for the tab URL (str).
        ActiveRole: Role id for whether the row is the active tab (bool).
        PinStateRole: Role id for whether the tab row is pinned (bool).
    """

    KindRole = KindRole
    FoldedRole = FoldedRole
    StatusRole = StatusRole
    BadgeRole = BadgeRole
    UrlRole = UrlRole
    ActiveRole = ActiveRole
    PinStateRole = PinStateRole

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
        self._placeholder_pixmap = self._create_placeholder_pixmap()
        self.set_workspace(manager.get_active())

    def set_workspace(self, workspace: Workspace | None) -> None:
        """Replace the model contents with *workspace* and reset views."""
        self.beginResetModel()
        self._workspace = workspace
        self._root.children.clear()
        self._tab_nodes.clear()
        if workspace is not None:
            self._build_tree(workspace)
        self.endResetModel()

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
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # --- private helpers ---

    def _build_tree(self, workspace: Workspace) -> None:
        tabs = list(workspace.tabs)
        parent_ids = {t.parent_id for t in tabs if t.parent_id is not None}

        group_nodes: dict[uuid.UUID, _Node] = {}
        for group in workspace.groups:
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
        if role == KindRole:
            return Kind.Group
        if role == FoldedRole:
            return group.folded
        if role == BadgeRole:
            return count
        if role == StatusRole:
            return Status.NONE
        if role == ActiveRole:
            return False
        return None

    def _tab_data(self, tab: WorkspaceTab, role: int, kind: Kind):
        info = self._tab_info.get(tab.tab_id, _TabInfo())
        if role == KindRole:
            return kind
        if role == ActiveRole:
            return self._active_tab_id == tab.tab_id
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
            return info.status
        if role == PinStateRole:
            return info.status & Status.PINNED != 0
        if role == BadgeRole:
            return None
        return None

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
    """

    fold_toggled = pyqtSignal(QModelIndex)
    audio_toggled = pyqtSignal(QModelIndex)
    close_requested = pyqtSignal(QModelIndex)
    pin_toggled = pyqtSignal(QModelIndex)
    activated = pyqtSignal(QModelIndex)

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
                and int(index.data(BadgeRole) or 0) == 0
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
                badge_text = f"+{int(badge)}"

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
            painter, option, index, rect, is_tab, close_rect, pin_rect,
            is_pinned,
        )

    def _finish_row(
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
            rect: QRect,
            is_tab: bool,
            close_rect: Optional[QRect],
            pin_rect: Optional[QRect],
            is_pinned: bool,
    ) -> None:
        """Draw the close/pin glyphs and the active-tab left accent."""
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

        # Active-tab left-edge accent (visible without hover).
        active = bool(index.data(ActiveRole)) and is_tab
        if active:
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
    """

    activated = pyqtSignal(QModelIndex)
    keyboard_activated = pyqtSignal(QModelIndex)
    fold_toggled = pyqtSignal(QModelIndex)
    audio_toggled = pyqtSignal(QModelIndex)
    close_requested = pyqtSignal(QModelIndex)
    pin_toggled = pyqtSignal(QModelIndex)

    def __init__(
            self,
            win_id: int,
            manager: WorkspaceManager,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._win_id = win_id
        self._manager = manager

        self.setObjectName('tab-tree')

        self._model = TabTreeModel(manager, parent=self)
        self._delegate = TabTreeItemDelegate(self)
        self._view = QTreeView(self)
        self._view.setObjectName('tab-tree-view')
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)

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

        self._placeholder = QLabel("No tabs in this workspace", self)
        self._placeholder.setObjectName('tab-tree-placeholder')
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._view)
        self._stack.addWidget(self._placeholder)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._stack)

        self._refresh_empty_state()

        # Delegate interactions.
        self._delegate.fold_toggled.connect(self._model.toggle_folded)
        self._delegate.fold_toggled.connect(self.fold_toggled)
        self._delegate.audio_toggled.connect(self.audio_toggled)
        self._delegate.close_requested.connect(self.close_requested)
        self._delegate.pin_toggled.connect(self.pin_toggled)
        self._delegate.activated.connect(self.activated)

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

    def _on_manager_changed(self) -> None:
        """Rebuild the tree from the active workspace."""
        self._model.set_workspace(self._manager.get_active())
        self._refresh_empty_state()

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
        empty = workspace is None or (not workspace.groups and not workspace.tabs)
        self._stack.setCurrentIndex(1 if empty else 0)

    def view(self) -> QTreeView:
        """Return the inner ``QTreeView`` for focus routing."""
        return self._view
