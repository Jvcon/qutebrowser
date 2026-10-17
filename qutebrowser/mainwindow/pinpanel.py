# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The vertical panel's pinned-tab section (Phase 2).

Renders the profile-scoped pinned-tab list as either an icon grid
(2×3 cells of 24×24 favicons) or a line list (16×16 favicon + elided
title per row), depending on the ``pinned_tabs.style`` config key.
See ADR 0008 § "1. PIN panel" for the visual contract.

The panel is composed into the :class:`qutebrowser.mainwindow.verticalpanel.VerticalPanel`
at the top of its QVBoxLayout. When the pinned list is empty the
panel collapses to 0 px (the inner QListView is hidden and the
QVBoxLayout gives it no vertical space).

The panel reacts to :attr:`qutebrowser.workspaces.manager.WorkspaceManager.pinned_changed`
to refresh whenever the manager's pinned list is mutated by
``add_pinned`` / ``remove_pinned`` or by the initial ``load()``.

Interaction
===========

- Left click: emit :attr:`activated` with the clicked PinnedTab so
  the host can open the URL in the current workspace.
- Middle click: emit :attr:`unpinned` with the tab_id so the host
  can remove it from the manager.
- Right click: show a context menu with "Unpin" (Phase 2 first
  slice; "Move to workspace" lands in a later sub-slice).

The middle / right paths ride on a viewport event filter because
``QListView`` does not carry the originating button on its public
``clicked`` signal. The left-click path uses the public signal as
intended.
"""

from qutebrowser.qt.core import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QSize,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from qutebrowser.qt.gui import (
    QMouseEvent,
    QPainter,
    QPixmap,
    QFontMetrics,
    QColor,
)
from qutebrowser.qt.widgets import (
    QAbstractItemView,
    QListView,
    QMenu,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from qutebrowser.config import config
from qutebrowser.workspaces.model import PinnedTab


# Class-level role ids so the delegate and the model share them
# without circular imports.
TitleRole = Qt.ItemDataRole.UserRole + 1
UrlRole = Qt.ItemDataRole.UserRole + 2
FaviconRole = Qt.ItemDataRole.UserRole + 3


def _pinned_tab_data(tab: PinnedTab, role: int):
    """Shared role -> value lookup for the model and the line-mode delegate.

    Keeping the role table in one place avoids the "two callers
    disagree on a role" footgun.
    """
    if role == TitleRole:
        return tab.title
    if role == UrlRole:
        return tab.url
    if role == FaviconRole:
        return tab.favicon
    if role == Qt.ItemDataRole.ToolTipRole:
        return f"{tab.title}\n{tab.url}"
    if role == Qt.ItemDataRole.DisplayRole:
        return tab.title
    if role == Qt.ItemDataRole.DecorationRole:
        return tab.favicon
    return None


class PinnedTabModel(QAbstractItemModel):

    """Backing model for the PIN panel's QListView.

    The model exposes ``title`` (TitleRole), ``url`` (UrlRole) and
    ``favicon`` bytes (FaviconRole). Other roles are unused because
    the panel never queries the model for anything else.
    """

    TitleRole = TitleRole
    UrlRole = UrlRole
    FaviconRole = FaviconRole

    def __init__(
            self,
            pinned: list[PinnedTab] | None = None,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pinned: list[PinnedTab] = list(pinned) if pinned else []

    def set_pinned(self, pinned: list[PinnedTab]) -> None:
        """Replace the model contents with *pinned* and reset views."""
        self.beginResetModel()
        self._pinned = list(pinned)
        self.endResetModel()

    def pinned_at(self, row: int) -> PinnedTab | None:
        """Return the PinnedTab at *row*, or None if out of range."""
        if 0 <= row < len(self._pinned):
            return self._pinned[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            # We are a list, not a tree.
            return 0
        return len(self._pinned)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def index(  # noqa: N802 - Qt naming
            self,
            row: int,
            column: int,
            parent: QModelIndex = QModelIndex(),
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        return self.createIndex(row, column, self._pinned[row])

    def parent(  # noqa: N802 - Qt naming
            self,
            child: QModelIndex = QModelIndex(),
    ) -> QModelIndex:  # type: ignore[override]
        # Flat list — every (valid) index has the implicit root as
        # its parent.
        return QModelIndex()

    def data(  # noqa: N802 - Qt naming
            self,
            index: QModelIndex,
            role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid():
            return None
        tab = self._pinned[index.row()]
        return _pinned_tab_data(tab, role)


class PinnedTabDelegate(QStyledItemDelegate):

    """Custom delegate for the line-mode PIN panel.

    Renders a 16×16 favicon followed by the elided title in a 22-px
    row. The hover-only close `×` from ADR 0008 § "1. PIN panel"
    (Line mode) lands in a later sub-slice; this delegate ships the
    base rendering only so the panel is functional end-to-end.
    """

    ROW_HEIGHT = 22
    ICON_SIZE = 16
    ICON_TEXT_GAP = 6

    def sizeHint(  # noqa: N802 - Qt naming
            self,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> QSize:
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(  # noqa: N802 - Qt naming
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> None:
        painter.save()
        if option.state & QStyleOptionViewItem.State.State_Selected:
            painter.fillRect(
                option.rect,
                option.palette.highlight(),
            )
            text_color: QColor = option.palette.highlightedText().color()
        else:
            text_color = option.palette.text().color()

        rect = option.rect
        icon_x = rect.x() + 3
        icon_y = rect.y() + (rect.height() - self.ICON_SIZE) // 2
        favicon = index.data(PinnedTabModel.FaviconRole)
        if favicon:
            pixmap = QPixmap()
            pixmap.loadFromData(favicon)
            painter.drawPixmap(
                icon_x, icon_y, self.ICON_SIZE, self.ICON_SIZE, pixmap,
            )

        text_x = icon_x + self.ICON_SIZE + self.ICON_TEXT_GAP
        text_width = max(rect.width() - (text_x - rect.x()) - 4, 0)
        title = index.data(PinnedTabModel.TitleRole) or ''
        fm: QFontMetrics = option.fontMetrics
        elided = fm.elidedText(
            title,
            Qt.TextElideMode.ElideRight,
            text_width,
        )
        painter.setPen(text_color)
        painter.drawText(
            text_x,
            rect.y(),
            text_width,
            rect.height(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided,
        )
        painter.restore()


class _PinListView(QListView):

    """QListView subclass that records the originating button on release.

    Qt's public ``clicked`` signal carries only the QModelIndex, not the
    mouse button. The host can ask ``last_button_for(index)`` after
    ``clicked`` fires to know which button triggered the
    activation. Middle and right clicks are *also* routed through
    the event filter, so this is mostly belt-and-suspenders for the
    left-click path.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last_button: Qt.MouseButton = Qt.MouseButton.NoButton

    def last_button_for(self, _index: QModelIndex) -> Qt.MouseButton:
        return self._last_button

    def mouseReleaseEvent(  # noqa: N802 - Qt naming
            self, event: QMouseEvent,
    ) -> None:
        self._last_button = event.button()
        super().mouseReleaseEvent(event)


class PINPanel(QWidget):

    """The vertical panel's pinned-tab section (Phase 2).

    Attributes:
        activated: Emitted when a pinned tab is left-clicked.
            Argument: the :class:`PinnedTab` that was activated.
        unpinned: Emitted when a pinned tab is removed (middle
            click or context menu). Argument: the ``tab_id`` of the
            removed pinned tab.
    """

    activated = pyqtSignal(PinnedTab)
    unpinned = pyqtSignal(int)

    def __init__(
            self,
            win_id: int,
            manager,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._win_id = win_id
        self._manager = manager

        self.setObjectName('pin-panel')

        self._model = PinnedTabModel(parent=self)
        self._list = _PinListView(self)
        self._list.setObjectName('pin-panel-list')
        self._list.setModel(self._model)
        self._list.setItemDelegate(PinnedTabDelegate(self._list))
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setMovement(QListView.Movement.Static)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setUniformItemSizes(True)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._list)

        # Apply the current style + contents. Order matters: style
        # first (sets the section height), then contents (decides
        # visibility / collapse).
        self._apply_style()
        self._refresh_from_manager()

        # Left click is the standard QListView.clicked signal.
        self._list.clicked.connect(self._on_clicked)
        # Right click is the standard customContextMenuRequested
        # signal (the QListView emits it for the right button).
        self._list.customContextMenuRequested.connect(
            self._on_context_menu_requested)
        # Middle click is NOT surfaced via either of the above; we
        # install an event filter on the viewport for it.
        self._list.viewport().installEventFilter(self)

        # Wire config + manager signals.
        config.instance.changed.connect(self._on_config_changed)
        self._manager.pinned_changed.connect(self._refresh_from_manager)

    # --- style ---

    def _apply_style(self) -> None:
        style = config.val.pinned_tabs.style
        if style == 'icon':
            self._list.setViewMode(QListView.ViewMode.IconMode)
            self._list.setFlow(QListView.Flow.LeftToRight)
            self._list.setIconSize(QSize(24, 24))
            self._list.setSpacing(4)
            # Icon mode: fixed ~54 px section per ADR. Allow scroll
            # for the rare 7+ tab case via the QListView's own
            # vertical scrollbar.
            self._list.setMaximumHeight(54 + 4)
            self._list.setMinimumHeight(54)
        else:  # 'line'
            self._list.setViewMode(QListView.ViewMode.ListMode)
            self._list.setFlow(QListView.Flow.TopToBottom)
            self._list.setIconSize(QSize(16, 16))
            self._list.setSpacing(0)
            # Line mode: 22 px × row, capped at 8 rows (~176 px).
            self._list.setMaximumHeight(22 * 8)
            self._list.setMinimumHeight(0)

    def _on_config_changed(self, option: str) -> None:
        if option == 'pinned_tabs.style':
            self._apply_style()

    # --- data flow ---

    def _refresh_from_manager(self) -> None:
        pinned = self._manager.list_pinned()
        self._model.set_pinned(pinned)
        # Empty pinned list -> collapse to 0 px (ADR § "1. PIN
        # panel"). The QVBoxLayout above this list view will give
        # the list zero vertical space when it's hidden, which is
        # what we want.
        self._list.setVisible(len(pinned) > 0)

    # --- interaction ---

    @pyqtSlot(QModelIndex)
    def _on_clicked(self, index: QModelIndex) -> None:
        if self._list.last_button_for(index) != Qt.MouseButton.LeftButton:
            # Middle / right clicks land here too on some Qt
            # versions; let the event filter / context menu handler
            # take them.
            return
        tab = self._model.pinned_at(index.row())
        if tab is None:
            return
        self.activated.emit(tab)

    def eventFilter(  # noqa: N802 - Qt naming
            self, obj: QObject, event: QEvent,
    ) -> bool:
        if (
                obj is self._list.viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.MiddleButton
        ):
            assert isinstance(event, QMouseEvent)
            index = self._list.indexAt(event.pos())
            if index.isValid():
                tab = self._model.pinned_at(index.row())
                if tab is not None:
                    self.unpinned.emit(tab.tab_id)
                    return True
        return super().eventFilter(obj, event)

    def _on_context_menu_requested(self, pos: QPoint) -> None:
        index = self._list.indexAt(pos)
        if not index.isValid():
            return
        tab = self._model.pinned_at(index.row())
        if tab is None:
            return
        menu = QMenu(self._list)
        unpin_action = menu.addAction('Unpin')
        unpin_action.triggered.connect(
            lambda: self.unpinned.emit(tab.tab_id))
        menu.exec(self._list.viewport().mapToGlobal(pos))

    # --- public API used by the host (VerticalPanel) ---

    def list_widget(self) -> QListView:
        """Return the inner QListView for direct focus / event routing."""
        return self._list
