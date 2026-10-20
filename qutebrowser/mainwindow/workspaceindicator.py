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
from typing import Optional

from qutebrowser.qt.core import QPoint, QSize, Qt
from qutebrowser.qt.gui import QAction, QColor, QIcon, QPainter, QPixmap
from qutebrowser.qt.widgets import QMenu, QToolBar, QToolButton, QWidget

from qutebrowser.config import config
from qutebrowser.utils import message, usertypes
from qutebrowser.workspaces.model import Workspace


def monogram_pixmap(name: str, color: Optional[str] = None) -> QPixmap:
    """Render a 16x16 rounded-square monogram for a workspace name.

    The glyph is the first Unicode scalar of *name* centered on a rounded
    square. The fill is *color* when given, otherwise a deterministic muted
    HSL color derived from a stable hash of *name* (H = hash % 360,
    S = 25 %, L = 45 %) so the same workspace renders the same way across
    restarts.

    Note: WCAG contrast adjustment (ADR § "Workspace monogram") is
    intentionally simplified here to a light-on-dark letter; a full
    luminance-shift pass is a later polish slice.
    """
    if color:
        fill = QColor(color)
    else:
        digest = hashlib.md5(name.encode('utf-8')).hexdigest()
        hue = int(digest[:8], 16) % 360
        fill = QColor.fromHslF(hue / 360.0, 0.25, 0.45)

    letter = name[0].upper() if name else '?'

    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(0, 0, 16, 16, 4, 4)
    painter.setPen(Qt.GlobalColor.white)
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
        _buttons: Mapping of workspace id -> ``QToolButton``, rebuilt
            whenever the manager's ``changed`` signal fires.
    """

    def __init__(self, win_id: int, manager,
                 parent: QWidget | None = None) -> None:
        super().__init__('Workspaces', parent)
        self._win_id = win_id
        self._manager = manager
        self._buttons: dict[uuid.UUID, QToolButton] = {}

        self.setObjectName('workspace-indicator')
        self.setAccessibleName('Workspaces')
        self.setMovable(False)
        self.setFixedHeight(28)
        self.setIconSize(QSize(24, 24))

        self._rebuild()
        self._manager.changed.connect(self._rebuild)

    # --- (re)build ---

    def _rebuild(self) -> None:
        """Rebuild the buttons from the manager's workspace list.

        Archived workspaces are hidden (ADR § 4). A full rebuild on every
        change is acceptable for the small number of workspaces expected
        here; a fine-grained diff is a later polish slice.
        """
        self.clear()
        for button in self._buttons.values():
            button.deleteLater()
        self._buttons.clear()

        active = self._manager.get_active()
        for workspace in self._manager.list_all_workspaces():
            if workspace.archived:
                continue

            button = QToolButton(self)
            button.setCheckable(True)
            button.setFixedSize(QSize(24, 24))
            button.setToolTip(workspace.name)
            button.setIcon(
                QIcon(monogram_pixmap(
                    workspace.name, getattr(workspace, 'color', None))))

            is_active = active is not None and workspace.id == active.id
            button.setChecked(is_active)
            self._apply_active_style(button, is_active)

            button.clicked.connect(
                lambda _checked, wid=workspace.id: self._on_clicked(wid))
            button.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, wid=workspace.id: self._on_context_menu(
                    pos, wid))

            self.addWidget(button)
            self._buttons[workspace.id] = button

    def _apply_active_style(self, button: QToolButton, active: bool) -> None:
        """Give the active button an accent border (ADR § 4)."""
        if active:
            accent = QColor(
                config.cache['colors.tabs.indicator.start']).name()
            button.setStyleSheet(
                f'QToolButton {{ border: 2px solid {accent}; }}')
        else:
            button.setStyleSheet('')

    # --- interaction ---

    def _on_clicked(self, workspace_id: uuid.UUID) -> None:
        """Make *workspace_id* the active workspace."""
        active = self._manager.get_active()
        if active is None or active.id != workspace_id:
            self._manager.set_active(workspace_id)

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
            lambda: self._rename(workspace))
        archive_action.triggered.connect(
            lambda: self._archive(workspace))
        delete_action.triggered.connect(
            lambda: self._delete(workspace))
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
