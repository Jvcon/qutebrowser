# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""In-memory workspace / pinned-tab state and its persistence orchestration.

:class:`WorkspaceManager` is the central state holder used by the widget layer
(Phase 2).  It owns the loaded :class:`~qutebrowser.workspaces.model.Workspace`
objects and pinned tabs, exposes CRUD operations over them, and emits
:attr:`changed` whenever state mutates so it can be registered as a
``Saveable``.
"""

import datetime
import uuid
from typing import Optional

from qutebrowser.qt.core import QObject, pyqtSignal

from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)


class WorkspaceManager(QObject):

    """In-memory store of workspaces + pinned tabs; persists via Saveable.

    Attributes:
        changed: Emitted whenever managed state changes.
    """

    changed = pyqtSignal()

    def __init__(self, persistence: WorkspacePersistence,
                 pinned: PinnedPersistence) -> None:
        super().__init__()
        self._persistence = persistence
        self._pinned_persistence = pinned
        self._workspaces: dict[uuid.UUID, Workspace] = {}
        self._active_id: Optional[uuid.UUID] = None
        self._pinned: list[PinnedTab] = []

    # --- load/save ---

    def load(self) -> None:
        """Load all workspaces and pinned tabs from disk.

        The active workspace is restored from the most recent ``last_active``
        timestamp found on disk, if any.
        """
        self._workspaces = {
            workspace.id: workspace
            for workspace in self._persistence.load_all()
        }
        self._pinned = self._pinned_persistence.load()
        self._active_id = None
        candidates = [ws for ws in self._workspaces.values() if ws.last_active]
        if candidates:
            active = max(candidates, key=lambda ws: ws.last_active or '')
            self._active_id = active.id

    def save(self) -> None:
        """Persist all workspaces and pinned tabs."""
        for workspace in self._workspaces.values():
            self._persistence.save(workspace)
        self._pinned_persistence.save(self._pinned)

    # --- workspace CRUD ---

    def add_workspace(self, name: str) -> Workspace:
        """Create, store, and return a new workspace."""
        workspace = Workspace(id=uuid.uuid4(), name=name)
        self._workspaces[workspace.id] = workspace
        if self._active_id is None:
            self._active_id = workspace.id
        self.changed.emit()
        return workspace

    def delete_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace from memory and disk."""
        del self._workspaces[workspace_id]
        self._persistence.delete(workspace_id)
        if self._active_id == workspace_id:
            self._active_id = None
        self.changed.emit()

    def rename_workspace(self, workspace_id: uuid.UUID, new_name: str) -> None:
        """Rename an existing workspace."""
        self._workspaces[workspace_id].name = new_name
        self.changed.emit()

    def list_workspaces(self) -> list[Workspace]:
        """Return all known workspaces."""
        return list(self._workspaces.values())

    def set_active(self, workspace_id: uuid.UUID) -> None:
        """Mark a workspace as active, updating its ``last_active`` stamp."""
        workspace = self._workspaces[workspace_id]
        workspace.last_active = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        self._active_id = workspace_id
        self.changed.emit()

    def get_active(self) -> Optional[Workspace]:
        """Return the active workspace, or None if there is none."""
        if self._active_id is None:
            return None
        return self._workspaces.get(self._active_id)

    # --- groups ---

    def create_group(self, workspace_id: uuid.UUID, name: str) -> Group:
        """Create and return a new group in a workspace."""
        group = Group(group_id=uuid.uuid4(), name=name)
        self._workspaces[workspace_id].groups.append(group)
        self.changed.emit()
        return group

    def rename_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID,
                     new_name: str) -> None:
        """Rename a group within a workspace."""
        for group in self._workspaces[workspace_id].groups:
            if group.group_id == group_id:
                group.name = new_name
                self.changed.emit()
                return

    def delete_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID) -> None:
        """Delete a group and make its members standalone.

        Empty groups are deleted without confirmation; prompting for
        non-empty groups is the caller's responsibility.
        """
        workspace = self._workspaces[workspace_id]
        workspace.groups = [group for group in workspace.groups
                            if group.group_id != group_id]
        for tab in workspace.tabs:
            if tab.group_id == group_id:
                tab.group_id = None
        self.changed.emit()

    def list_groups(self, workspace_id: uuid.UUID) -> list[Group]:
        """Return all groups in a workspace."""
        return list(self._workspaces[workspace_id].groups)

    # --- tab membership ---

    def add_tab(self, workspace_id: uuid.UUID, tab: WorkspaceTab) -> None:
        """Add a tab entry to a workspace."""
        self._workspaces[workspace_id].tabs.append(tab)
        self.changed.emit()

    def remove_tab(self, workspace_id: uuid.UUID, tab_id: int) -> None:
        """Remove a tab entry from a workspace.

        Any child tab pointing at the removed tab is detached (its
        ``parent_id`` is cleared).
        """
        workspace = self._workspaces[workspace_id]
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.tab_id != tab_id]
        for tab in workspace.tabs:
            if tab.parent_id == tab_id:
                tab.parent_id = None
        self.changed.emit()

    def move_tab_to_group(self, workspace_id: uuid.UUID, tab_id: int,
                          group_id: Optional[uuid.UUID]) -> None:
        """Move a tab into a group (or to standalone when group_id is None)."""
        workspace = self._workspaces[workspace_id]
        for tab in workspace.tabs:
            if tab.tab_id == tab_id:
                tab.group_id = group_id
                self.changed.emit()
                return

    # --- pinned tabs ---

    def add_pinned(self, tab: PinnedTab) -> None:
        """Add a pinned tab, replacing any existing entry with its tab_id."""
        self._pinned = [pinned for pinned in self._pinned
                        if pinned.tab_id != tab.tab_id]
        self._pinned.append(tab)
        self.changed.emit()

    def remove_pinned(self, tab_id: int) -> None:
        """Remove a pinned tab by tab_id."""
        self._pinned = [pinned for pinned in self._pinned
                        if pinned.tab_id != tab_id]
        self.changed.emit()

    def list_pinned(self) -> list[PinnedTab]:
        """Return all pinned tabs."""
        return list(self._pinned)
