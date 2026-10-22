# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The vertical panel's workspace-switching section (Phase 2).

A :class:`WorkspaceIndicator` renders one checkable ``QToolButton`` per
non-archived workspace at the bottom of the
:class:`~qutebrowser.mainwindow.verticalpanel.VerticalPanel`. Clicking a
button makes that workspace active (which, via the manager's ``changed``
signal, drives the TabTree to redraw). Right-clicking opens a context menu
with rename / archive / delete.

Workspace switching happens **only** through this widget (ADR 0008 § 4);
the active workspace is preserved across restarts via the ``last_active``
field stamped by :meth:`~qutebrowser.workspaces.manager.WorkspaceManager.
set_active`.
"""

import hashlib
import uuid
from typing import Any, Optional

from qutebrowser.qt.core import QPoint, QSize, Qt
from qutebrowser.qt.gui import QAction, QColor, QIcon, QPainter, QPixmap
from qutebrowser.qt.widgets import QMenu, QToolBar, QToolButton, QWidget

from qutebrowser.config import config
from qutebrowser.mainwindow import panelstyle
from qutebrowser.mainwindow.panelstyle import Palette
from qutebrowser.mainwindow.tabtree import decode_tab_node
from qutebrowser.utils import message, usertypes
from qutebrowser.utils.qtutils import weak_slot
from qutebrowser.workspaces.manager import WorkspaceMoveError
from qutebrowser.workspaces.model import Workspace


def monogram_pixmap(name: str, color: Optional[str] = None) -> QPixmap:
    """Render a 16x16 rounded-square monogram for a workspace name.

    The glyph is the first Unicode scalar of *name* centered on a rounded
    square. The fill is *color* when given, otherwise a deterministic muted
    HSL color derived from a stable hash of *name* (H = hash % 360,
    S = 25 %, L = 45 %) so the same workspace renders the same way across
    restarts.

    WCAG (ADR § "Workspace monogram"): the fill is shifted toward the panel
    foreground until it reaches 3:1 against the panel base, and the letter is
    black or white, whichever contrasts more with the fill.
    """
    palette = Palette.from_config()
    if color:
        fill = QColor(color)
    else:
        digest = hashlib.md5(name.encode('utf-8')).hexdigest()
        hue = int(digest[:8], 16) % 360
        fill = QColor.fromHslF(hue / 360.0, 0.25, 0.45)
    fill, letter_color = panelstyle.monogram_colors(
        fill, palette.base, palette.text)

    letter = name[0].upper() if name else '?'

    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(0, 0, 16, 16, 4, 4)
    painter.setPen(letter_color)
    font = painter.font()
    font.setPixelSize(10)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return pixmap


class WorkspaceIndicator(QToolBar):

    """The vertical panel's workspace switcher (ADR 0008 § 4).

    Attributes:
        _win_id: The window id this indicator belongs to.
        _manager: The app-wide ``WorkspaceManager``.
        _buttons: Mapping of workspace id -> ``QToolButton``. Kept in sync
            in place by :meth:`_sync`; surviving buttons are never recreated.
        _actions: Mapping of workspace id -> the toolbar ``QAction`` that
            hosts the button (as returned by ``addWidget``/``insertWidget``).
        _drop_target: Workspace id currently highlighted as a tab-node drop
            target, or ``None``.  Mirrors the tree's ``DropTargetRole``.
    """

    def __init__(self, win_id: int, manager,
                 parent: QWidget | None = None) -> None:
        super().__init__('Workspaces', parent)
        self._win_id = win_id
        self._manager = manager
        self._buttons: dict[uuid.UUID, QToolButton] = {}
        self._actions: dict[uuid.UUID, QAction] = {}
        self._drop_target: Optional[uuid.UUID] = None

        self.setObjectName('workspace-indicator')
        # ADR 0008 § "Screen reader contract".
        self.setAccessibleName('Workspaces')
        self.setAccessibleDescription('Workspace switcher')
        self.setMovable(False)
        self.setFixedHeight(28)
        self.setIconSize(QSize(24, 24))
        # Tab nodes dragged out of the TabTree move to the workspace whose
        # button they are dropped on (ticket #6).
        self.setAcceptDrops(True)

        self._sync()
        self._manager.changed.connect(self._sync)
        # Accent / drop-wash colours follow the ``colors`` config.
        panelstyle.connect_colors_changed(self._on_config_changed)

    def _on_config_changed(self, option: str) -> None:
        """Restyle the buttons and monograms after a colour change."""
        if option.startswith('colors.'):
            self._sync()

    # --- sync with manager (in-place diff) ---

    def _sync(self) -> None:
        """Bring the buttons in line with the manager's workspace list.

        Archived workspaces are hidden (ADR § 4). Buttons are keyed by
        workspace id: vanished workspaces have their button removed, new
        ones get a button inserted at their position, and surviving buttons
        are only re-labelled and re-checked, so they are never destroyed and
        recreated on a change.
        """
        active = self._manager.get_active()
        visible = [ws for ws in self._manager.list_all_workspaces()
                   if not ws.archived]
        visible_ids = {ws.id for ws in visible}
        count = len(visible)
        active_name = active.name if active is not None else 'none'
        self.setAccessibleDescription(
            f"Workspace switcher, {count} workspace"
            f"{'' if count == 1 else 's'}, active: {active_name}")

        for wid in list(self._buttons):
            if wid not in visible_ids:
                self._remove(wid)

        for index, workspace in enumerate(visible):
            is_active = active is not None and workspace.id == active.id
            button = self._buttons.get(workspace.id)
            if button is None:
                # Insert before the next already-present workspace so the
                # toolbar order follows the manager's list.
                before = next(
                    (self._actions[ws.id] for ws in visible[index + 1:]
                     if ws.id in self._actions),
                    None)
                button = self._add(workspace, before)
            else:
                self._update(button, workspace)
            button.setChecked(is_active)
            self._apply_style(workspace.id, is_active)

    def _add(self, workspace: Workspace,
             before: QAction | None) -> QToolButton:
        """Create the button for *workspace* and place it in the toolbar."""
        button = QToolButton(self)
        button.setCheckable(True)
        button.setFixedSize(QSize(24, 24))
        button.clicked.connect(
            weak_slot(self._on_clicked, workspace.id, accepts=0))
        button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        button.customContextMenuRequested.connect(
            weak_slot(self._on_context_menu, workspace.id))
        # The focus ring rule is part of this button's stylesheet, which
        # _apply_style owns, so only track keyboard focus here.
        panelstyle.install_focus_ring(button, stylesheet=False)
        self._update(button, workspace)

        if before is None:
            action = self.addWidget(button)
        else:
            action = self.insertWidget(before, button)
        self._buttons[workspace.id] = button
        self._actions[workspace.id] = action
        return button

    def _update(self, button: QToolButton, workspace: Workspace) -> None:
        """Refresh the label, tooltip and monogram colour of *button*."""
        button.setToolTip(workspace.name)
        # ADR 0008 § "Screen reader contract": the spoken name of a button.
        button.setAccessibleName(workspace.name)
        button.setIcon(QIcon(monogram_pixmap(
            workspace.name, getattr(workspace, 'color', None))))

    def _remove(self, workspace_id: uuid.UUID) -> None:
        """Remove the button and toolbar action for *workspace_id*."""
        self._buttons.pop(workspace_id).hide()
        # The toolbar's QWidgetAction owns the button, so deleting the action
        # also deletes the button.
        action = self._actions.pop(workspace_id)
        self.removeAction(action)
        action.deleteLater()
        if self._drop_target == workspace_id:
            self._drop_target = None

    def _apply_style(self, workspace_id: uuid.UUID, active: bool) -> None:
        """Style the button for *workspace_id*: active accent and drop wash.

        The active button gets an accent border (ADR § 4).  A button that is
        the current drop target gets the same translucent highlight-palette
        wash the TabTree paints for its ``DropTargetRole`` row (alpha 70).
        """
        palette = Palette.from_config()
        button = self._buttons[workspace_id]
        button.setAccessibleDescription(
            'active workspace' if active else 'workspace')
        rules = [panelstyle.focus_ring_rule('QToolButton')]
        if active:
            accent = palette.accent.name()
            rules.append(f'QToolButton {{ border: 2px solid {accent}; }}')
        if self._drop_target == workspace_id:
            wash = QColor(palette.accent)
            wash.setAlpha(70)
            rules.append(
                f'QToolButton {{ background-color: rgba({wash.red()}, '
                f'{wash.green()}, {wash.blue()}, {wash.alpha()}); }}')
        button.setStyleSheet('\n'.join(rules))

    # --- interaction ---

    def _on_clicked(self, workspace_id: uuid.UUID) -> None:
        """Make *workspace_id* the active workspace."""
        active = self._manager.get_active()
        if active is None or active.id != workspace_id:
            self._manager.set_active(workspace_id)

    # --- drag & drop of tab nodes (ticket #6) ---

    @staticmethod
    def _event_pos(event: Any) -> QPoint:
        try:
            return event.position().toPoint()
        except AttributeError:  # Qt5
            return event.pos()

    def _button_id_at(self, pos: QPoint) -> Optional[uuid.UUID]:
        """Return the id of the workspace button containing *pos*, if any."""
        for workspace_id, button in self._buttons.items():
            if button.geometry().contains(pos):
                return workspace_id
        return None

    def _is_active(self, workspace_id: uuid.UUID) -> bool:
        active = self._manager.get_active()
        return active is not None and active.id == workspace_id

    def _set_drop_target(self, workspace_id: Optional[uuid.UUID]) -> None:
        """Highlight *workspace_id* as the drop target (``None`` clears)."""
        if workspace_id == self._drop_target:
            return
        previous = self._drop_target
        self._drop_target = workspace_id
        for wid in (previous, workspace_id):
            if wid is not None and wid in self._buttons:
                self._apply_style(wid, self._is_active(wid))

    def _drag_move(self, event: Any) -> None:
        """Accept a tab-node drag over a non-active workspace button only."""
        if decode_tab_node(event.mimeData()) is None:
            self._set_drop_target(None)
            event.ignore()
            return
        target = self._button_id_at(self._event_pos(event))
        if target is None or self._is_active(target):
            self._set_drop_target(None)
            event.ignore()
            return
        self._set_drop_target(target)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802
        self._drag_move(event)

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802
        self._drag_move(event)

    def dragLeaveEvent(self, event: Any) -> None:  # noqa: N802
        self._set_drop_target(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: Any) -> None:  # noqa: N802
        """Move the dragged tab node into the workspace under the cursor."""
        self._set_drop_target(None)
        ref = decode_tab_node(event.mimeData())
        target = self._button_id_at(self._event_pos(event))
        if ref is None or target is None:
            event.ignore()
            return

        workspaces = {ws.id: ws for ws in self._manager.list_all_workspaces()}
        target_ws = workspaces.get(target)
        target_name = target_ws.name if target_ws is not None else '?'
        if self._is_active(target):
            message.error(
                f"Can't move a tab into the active workspace "
                f"'{target_name}'; switch workspaces first.")
            event.ignore()
            return
        if ref.workspace_id == target:
            message.error(f"Tab is already in workspace '{target_name}'.")
            event.ignore()
            return

        try:
            self._manager.move_tab_to_workspace(
                ref.workspace_id, ref.tab_id, target, branch=ref.branch)
        except WorkspaceMoveError as e:
            message.error(str(e))
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def _build_context_menu(self, workspace_id: uuid.UUID) -> QMenu:
        """Return the right-click menu for *workspace_id* (for testing)."""
        menu = QMenu(self)
        rename_action = menu.addAction('Rename')
        archive_action = menu.addAction('Archive')
        delete_action = menu.addAction('Delete')

        workspace = next(
            (ws for ws in self._manager.list_all_workspaces()
             if ws.id == workspace_id),
            None,
        )
        if workspace is None:
            return menu

        rename_action.triggered.connect(
            weak_slot(self._rename, workspace, accepts=0))
        archive_action.triggered.connect(
            weak_slot(self._archive, workspace, accepts=0))
        delete_action.triggered.connect(
            weak_slot(self._delete, workspace, accepts=0))
        return menu

    def _on_context_menu(self, pos: QPoint, workspace_id: uuid.UUID) -> None:
        menu = self._build_context_menu(workspace_id)
        menu.exec(self.mapToGlobal(pos))

    def _rename(self, workspace: Workspace) -> None:
        """Prompt for a new name and rename *workspace*."""

        def apply(new_name: str) -> None:
            if new_name:
                self._manager.rename_workspace(workspace.id, new_name)

        message.ask_async(
            f"Rename workspace '{workspace.name}' to:",
            usertypes.PromptMode.text,
            apply,
            default=workspace.name,
        )

    def _archive(self, workspace: Workspace) -> None:
        """Archive *workspace* (keeps its data on disk)."""
        if workspace.archived:
            return
        self._manager.archive_workspace(workspace.id)

    def _delete(self, workspace: Workspace) -> None:
        """Confirm then delete *workspace*."""
        message.confirm_async(
            title=f"Delete workspace '{workspace.name}'?",
            default=False,
            yes_action=lambda: self._manager.delete_workspace(workspace.id),
        )

    # --- public API used by the host (VerticalPanel) ---

    def buttons(self) -> dict[uuid.UUID, QToolButton]:
        """Return the current workspace-id -> button mapping."""
        return dict(self._buttons)
