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
from typing import Any, Callable, Optional

import yaml

from qutebrowser.qt.core import QObject, pyqtSignal

from qutebrowser.utils import log
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)


def _now() -> str:
    """Return the current time as an ISO 8601 UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class WorkspaceManager(QObject):

    """In-memory store of workspaces + pinned tabs; persists via Saveable.

    Only the active workspace is loaded eagerly (see :meth:`load`); other
    workspaces are loaded on demand through :meth:`get_workspace`.

    Signals:
        changed: Emitted whenever managed state changes.
        pinned_changed: Emitted whenever the pinned-tab list changes
            (add / remove / initial load). UI consumers (e.g. the
            VerticalPanel's PINPanel) listen to this to refresh
            without having to poll.
    """

    changed = pyqtSignal()
    pinned_changed = pyqtSignal()

    def __init__(self, persistence: WorkspacePersistence,
                 pinned_persistence: PinnedPersistence) -> None:
        super().__init__()
        self._persistence = persistence
        self._pinned_persistence = pinned_persistence
        self.profile_name: str = persistence.profile_name
        self._workspaces: dict[uuid.UUID, Workspace] = {}
        self._active_id: Optional[uuid.UUID] = None
        self._pinned: list[PinnedTab] = []

    # --- load/save ---

    def load(self) -> None:
        """Load the active workspace and pinned tabs from disk.

        Only the workspace whose id is recorded in the state file is loaded
        eagerly; all others are loaded on demand via :meth:`get_workspace`.
        """
        self._workspaces = {}
        self._active_id = None

        active_id = self._persistence.load_state()
        if active_id is not None:
            try:
                workspace = self._persistence.load(active_id)
            except (yaml.YAMLError, KeyError, ValueError, OSError) as e:
                log.message.warning(
                    f"Could not load active workspace {active_id}: {e}")
            else:
                self._workspaces[workspace.id] = workspace
                self._active_id = workspace.id

        self._pinned = self._pinned_persistence.load()
        # Signal consumers that the initial pinned list is now in
        # memory. Late-constructed consumers (e.g. a PINPanel that is
        # built after the manager) should also call list_pinned()
        # once to pick up the initial state.
        self.pinned_changed.emit()

    def save(self) -> None:
        """Persist all loaded workspaces, the active id, and pinned tabs."""
        for workspace in self._workspaces.values():
            self._persistence.save(workspace)
        self._persistence.save_state(self._active_id)
        self._pinned_persistence.save(self._pinned)

    # --- workspace CRUD ---

    def add_workspace(self, name: str) -> Workspace:
        """Create, store, and return a new workspace.

        The new workspace is stamped with ``last_active`` so a first launch
        followed by a save can restore it on the next start.
        """
        workspace = Workspace(id=uuid.uuid4(), name=name, last_active=_now())
        self._workspaces[workspace.id] = workspace
        if self._active_id is None:
            self._active_id = workspace.id
            self._persistence.save_state(self._active_id)
        self.changed.emit()
        return workspace

    def delete_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace from memory and disk."""
        del self._workspaces[workspace_id]
        self._persistence.delete(workspace_id)
        if self._active_id == workspace_id:
            self._active_id = None
        self._persistence.save_state(self._active_id)
        self.changed.emit()

    def rename_workspace(self, workspace_id: uuid.UUID, new_name: str) -> None:
        """Rename an existing workspace."""
        self._workspaces[workspace_id].name = new_name
        self.changed.emit()

    def list_workspaces(self) -> list[Workspace]:
        """Return all loaded workspaces."""
        return list(self._workspaces.values())

    def get_workspace(self, workspace_id: uuid.UUID) -> Optional[Workspace]:
        """Return a workspace by id, loading it from disk on demand."""
        if workspace_id in self._workspaces:
            return self._workspaces[workspace_id]
        try:
            workspace = self._persistence.load(workspace_id)
        except (yaml.YAMLError, KeyError, ValueError, OSError):
            return None
        self._workspaces[workspace.id] = workspace
        return workspace

    def set_active(self, workspace_id: uuid.UUID) -> None:
        """Mark a workspace as active, stamping ``last_active`` and state."""
        workspace = self._workspaces[workspace_id]
        workspace.last_active = _now()
        self._active_id = workspace_id
        self._persistence.save_state(self._active_id)
        self.changed.emit()

    def get_active(self) -> Optional[Workspace]:
        """Return the active workspace, or None if there is none."""
        if self._active_id is None:
            return None
        return self._workspaces.get(self._active_id)

    def get_workspace_by_name(self, name: str) -> Optional[Workspace]:
        """Return the workspace with the given name (case-insensitive), if any.

        Loaded workspaces are checked first. If none matches, the on-disk
        workspaces are consulted so that workspaces which are not currently
        loaded (e.g. after a cold start, when only the active workspace is
        loaded eagerly) are still found. A match loaded from disk is cached
        in memory so the mutation APIs (rename/archive/delete) work on it.
        """
        name_lower = name.lower()
        for workspace in self._workspaces.values():
            if workspace.name.lower() == name_lower:
                return workspace
        for workspace in self._persistence.load_all():
            if workspace.name.lower() == name_lower:
                self._workspaces[workspace.id] = workspace
                return workspace
        return None

    def list_all_workspaces(self) -> list[Workspace]:
        """Return every workspace (loaded ones plus on-disk ones).

        Used by the ``:workspace-list`` command so that a cold-started
        qutebrowser (which loads only the active workspace eagerly) still
        lists all workspaces.
        """
        workspaces = self.list_workspaces()
        loaded_ids = {workspace.id for workspace in workspaces}
        for workspace in self._persistence.load_all():
            if workspace.id not in loaded_ids:
                workspaces.append(workspace)
        return workspaces

    def archive_workspace(self, workspace_id: uuid.UUID) -> None:
        """Mark a workspace as archived without deleting it."""
        self._workspaces[workspace_id].archived = True
        self.changed.emit()

    # --- groups ---

    def create_group(self, workspace_id: uuid.UUID, name: str) -> Group:
        """Create and return a new group in a workspace."""
        group = Group(group_id=uuid.uuid4(), name=name)
        self._workspaces[workspace_id].add_group(group)
        self.changed.emit()
        return group

    def _mutate_group_or_tab(
            self,
            workspace_id: uuid.UUID,
            target_id: uuid.UUID | int,
            mutator: Callable[[Any], None],
    ) -> None:
        """Apply *mutator* to the group or tab identified by *target_id*.

        Groups are searched first, then tabs.  :attr:`changed` is emitted
        once if a matching item was found.
        """
        workspace = self._workspaces[workspace_id]
        for group in workspace.groups:
            if group.group_id == target_id:
                mutator(group)
                self.changed.emit()
                return
        for tab in workspace.tabs:
            if tab.tab_id == target_id:
                mutator(tab)
                self.changed.emit()
                return

    def rename_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID,
                     new_name: str) -> None:
        """Rename a group within a workspace."""
        self._mutate_group_or_tab(
            workspace_id, group_id, lambda group: group.rename(new_name))

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
                tab.clear_group()
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
                tab.clear_parent()
        self.changed.emit()

    def move_tab_to_group(self, workspace_id: uuid.UUID, tab_id: int,
                          group_id: Optional[uuid.UUID]) -> None:
        """Move a tab into a group (or to standalone when group_id is None)."""
        self._mutate_group_or_tab(
            workspace_id, tab_id, lambda tab: tab.assign_group(group_id))

    # --- pinned tabs ---

    def add_pinned(self, tab: PinnedTab) -> None:
        """Add a pinned tab, replacing any existing entry with its tab_id."""
        self._pinned = [pinned for pinned in self._pinned
                        if pinned.tab_id != tab.tab_id]
        self._pinned.append(tab)
        self.changed.emit()
        self.pinned_changed.emit()

    def remove_pinned(self, tab_id: int) -> None:
        """Remove a pinned tab by tab_id."""
        self._pinned = [pinned for pinned in self._pinned
                        if pinned.tab_id != tab_id]
        self.changed.emit()
        self.pinned_changed.emit()

    def list_pinned(self) -> list[PinnedTab]:
        """Return all pinned tabs."""
        return list(self._pinned)
