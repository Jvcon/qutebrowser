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

import dataclasses
import datetime
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Callable, Optional

import yaml

from qutebrowser.qt.core import QObject, QTimer, QUrl, pyqtSignal

from qutebrowser.config import config
from qutebrowser.utils import log, objreg, qtutils
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)


def _now() -> str:
    """Return the current time as an ISO 8601 UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _now_seconds() -> str:
    """Return the current UTC timestamp truncated to whole seconds."""
    return datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec='seconds')


def _host(url: str) -> str:
    """Return the lowercase host component of *url* ('' when unavailable)."""
    if not url:
        return ''
    return QUrl(url).host().lower()


# The three reading states of a WorkspaceTab (issue #15).
READING_STATUSES = ('FRESH', 'ACTIVE', 'DONE')

#: Rough memory estimate per OPEN tab (VP-P1).
# TODO(vp-p1-followup): switch to chrome.processes for real values.
BYTES_PER_TAB = 50 * 1024 * 1024  # 50 MB heuristic; MVP estimate.


def _parse_timestamp(value: str) -> Optional[datetime.datetime]:
    """Parse an ISO 8601 *value* into an aware UTC datetime, or None.

    Naive timestamps are assumed to be UTC so comparisons against the
    timezone-aware cutoff never raise.
    """
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


@dataclasses.dataclass
class TabMetadata:

    """Metadata captured from a live tab for collection/backfill.

    This intentionally does not depend on the browser layer so the manager
    can be unit-tested with plain strings.
    """

    url: str = ''
    title: str = ''
    description: str = ''
    source: str = ''
    favicon: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.source:
            self.source = _host(self.url)


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
        self._auto_suspend_timer: Optional[QTimer] = None

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

    # --- group running state (VP-D2) ---

    def group_open_count(self, workspace_id: uuid.UUID,
                         group_id: uuid.UUID) -> int:
        """Return the number of OPEN tabs belonging to *group_id*.

        This is a pure query: no :attr:`changed` is emitted.
        """
        workspace = self._workspaces[workspace_id]
        return sum(
            1 for tab in workspace.tabs
            if tab.group_id == group_id and tab.open_status == 'OPEN'
        )

    def group_total_count(self, workspace_id: uuid.UUID,
                          group_id: uuid.UUID) -> int:
        """Return the total number of tabs belonging to *group_id*.

        This is a pure query: no :attr:`changed` is emitted.
        """
        workspace = self._workspaces[workspace_id]
        return sum(
            1 for tab in workspace.tabs if tab.group_id == group_id
        )

    def estimate_group_memory_bytes(self, workspace_id: uuid.UUID,
                                    group_id: uuid.UUID) -> int:
        """Return a rough byte estimate for the group's OPEN tabs.

        The MVP heuristic assumes :data:`BYTES_PER_TAB` per OPEN tab;
        SUSPENDED tabs are already released. This is a pure query: no
        :attr:`changed` is emitted.
        """
        return (self.group_open_count(workspace_id, group_id)
                * BYTES_PER_TAB)

    def group_state(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> str:
        """Return the derived running state of *group_id*.

        The result is one of:

        * ``'ARCHIVED'`` — the group's archive flag is set (it wins over
          the runtime state of its members);
        * ``'OPEN'`` — at least one member tab is OPEN;
        * ``'SUSPENDED'`` — the group is empty or every member is
          SUSPENDED.

        This is a pure query: no :attr:`changed` is emitted.

        Downstream contract (VP-D3 / VP-D4 / VP-D5)::

            WorkspaceManager.group_state(workspace_id, group_id) -> str
            WorkspaceManager.group_open_count(workspace_id, group_id) -> int
            WorkspaceManager.group_total_count(workspace_id, group_id) -> int

        ``Group.state`` is the local view (``'ARCHIVED' | 'UNKNOWN'``); the
        model layer calls this method to resolve ``'UNKNOWN'`` into
        ``'OPEN'`` / ``'SUSPENDED'``.

        Raises:
            KeyError: When *group_id* does not exist in *workspace_id*.
        """
        group = self._find_group(workspace_id, group_id)
        if group.archived:
            return 'ARCHIVED'
        if self.group_open_count(workspace_id, group_id) > 0:
            return 'OPEN'
        return 'SUSPENDED'

    def get_or_create_inbox(self, workspace_id: uuid.UUID) -> Group:
        """Return the Inbox group for *workspace_id*, creating it if needed.

        Lookup is case-insensitive on the name ``'Inbox'``. The Inbox is a
        regular group; users may rename or delete it freely. A subsequent
        collect after a rename / delete creates a fresh group.
        """
        workspace = self._workspaces[workspace_id]
        for group in workspace.groups:
            if group.name.lower() == 'inbox':
                return group
        group = self.create_group(workspace_id, 'Inbox')
        log.workspaces.debug(
            "get_or_create_inbox: workspace %s created Inbox (1 group)",
            workspace_id)
        return group

    # --- group lifecycle (pin / archive / reorder / purge) ---

    def _find_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> Group:
        """Return the group identified by *group_id*.

        Raises ``KeyError`` when no such group exists.
        """
        for group in self._workspaces[workspace_id].groups:
            if group.group_id == group_id:
                return group
        raise KeyError(f"No group with id {group_id}")

    def pin_group(self, workspace_id: uuid.UUID,
                  group_id: uuid.UUID) -> None:
        """Mark a group as pinned (pinned groups sort first)."""
        group = self._find_group(workspace_id, group_id)
        group.pinned = True
        self.changed.emit()

    def unpin_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> None:
        """Clear a group's pinned flag."""
        group = self._find_group(workspace_id, group_id)
        group.pinned = False
        self.changed.emit()

    def archive_group(self, workspace_id: uuid.UUID,
                      group_id: uuid.UUID) -> None:
        """Mark a group as archived without deleting its data."""
        group = self._find_group(workspace_id, group_id)
        group.archived = True
        self.changed.emit()

    def restore_group(self, workspace_id: uuid.UUID,
                      group_id: uuid.UUID) -> None:
        """Clear a group's archived flag."""
        group = self._find_group(workspace_id, group_id)
        group.archived = False
        self.changed.emit()

    def list_archived_groups(self, workspace_id: uuid.UUID) -> list[Group]:
        """Return only the archived groups in a workspace."""
        return [group for group in self._workspaces[workspace_id].groups
                if group.archived]

    def move_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID,
                   new_index: int) -> None:
        """Move *group_id* to *new_index* in ``workspace.groups``.

        Reordering is independent of the pin flag: pinning only changes the
        display sort order, not the underlying list order.
        """
        workspace = self._workspaces[workspace_id]
        group = self._find_group(workspace_id, group_id)
        if not 0 <= new_index < len(workspace.groups):
            raise ValueError(
                f"Group index {new_index} out of range "
                f"(0..{len(workspace.groups) - 1}).")
        current_index = workspace.groups.index(group)
        workspace.groups.pop(current_index)
        workspace.groups.insert(new_index, group)
        self.changed.emit()

    def purge_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> int:
        """Permanently delete a group and every tab belonging to it.

        Unlike :meth:`delete_group` (which makes members standalone), this
        cascades: every :class:`~qutebrowser.workspaces.model.WorkspaceTab`
        whose ``group_id`` matches is removed as well.

        Returns:
            The number of tabs that were removed.
        """
        workspace = self._workspaces[workspace_id]
        removed_tabs = [tab for tab in workspace.tabs
                        if tab.group_id == group_id]
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.group_id != group_id]
        workspace.groups = [group for group in workspace.groups
                            if group.group_id != group_id]
        self.changed.emit()
        return len(removed_tabs)

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

    # --- batched operations (VP-F4) ---

    def move_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            group_id: Optional[uuid.UUID],
    ) -> int:
        """Move every known tab in *tab_ids* into *group_id*.

        ``group_id`` of ``None`` detaches the tabs (makes them standalone).
        Unknown tab ids are skipped silently.  :attr:`changed` is emitted
        once if anything moved.

        Returns:
            The number of tabs that were moved.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return 0
        id_set = set(tab_ids)
        moved = 0
        for tab in workspace.tabs:
            if tab.tab_id in id_set:
                tab.assign_group(group_id)
                moved += 1
        if moved:
            log.workspaces.debug(
                "move_tabs: workspace %s moved %d tab(s)", workspace_id, moved)
            self.changed.emit()
        return moved

    def delete_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            confirm: bool = True,
    ) -> int:
        """Remove every known tab in *tab_ids* from *workspace_id*.

        Children of a removed tab are detached (their ``parent_id`` is
        cleared).  Unknown tab ids are skipped silently.  :attr:`changed`
        is emitted once if anything was removed.

        Args:
            workspace_id: Workspace owning the tabs.
            tab_ids: Runtime ids of the tabs to remove.
            confirm: When True (the default) the call is a no-op returning
                ``0`` so the command layer can ask the user first; pass
                ``False`` (or use ``-!``) to delete immediately.

        Returns:
            The number of tabs that were removed.
        """
        if confirm:
            # The command layer confirms via ``message.confirm_async`` and
            # calls back with ``confirm=False`` from the yes_action.
            return 0
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return 0
        id_set = set(tab_ids)
        removed = [tab for tab in workspace.tabs if tab.tab_id in id_set]
        if not removed:
            return 0
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.tab_id not in id_set]
        for tab in workspace.tabs:
            if tab.parent_id in id_set:
                tab.clear_parent()
        log.workspaces.debug(
            "delete_tabs: workspace %s removed %d tab(s)",
            workspace_id, len(removed))
        self.changed.emit()
        return len(removed)

    def close_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID, *,
                    win_id: int = 0) -> tuple[int, int]:
        """Close every OPEN tab in *group_id*; return ``(closed, skipped)``.

        Each close goes through the browser's ``tabCloseRequested`` signal
        so the bridge's existing ``_on_tree_close_requested`` path flips the
        entry to SUSPENDED and emits :attr:`changed`. Tabs that are already
        SUSPENDED or pinned are skipped silently. Tabs that have no live
        match in the *win_id* window's ``tab-registry``, or whose
        ``tabCloseRequested`` handler raises, count toward *skipped* and do
        not abort the batch.

        Archived groups never participate in the running state, so closing
        their tabs is wrong: an archived *group_id* is a no-op returning
        ``(0, 0)``.

        The group itself (and its tab entries) is preserved so it can be
        reopened later; it is never deleted.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return (0, 0)
        # Verify the group exists before touching any tab.
        group = self._find_group(workspace_id, group_id)
        if group.archived:
            return (0, 0)

        registry, browser = self._close_resources(win_id)
        closed = skipped = 0
        for tab in workspace.tabs:
            if tab.group_id != group_id or tab.open_status != 'OPEN':
                continue
            live = registry.get(tab.tab_id)
            if live is None or browser is None:
                skipped += 1
                continue
            if getattr(getattr(live, 'data', None), 'pinned', False):
                # Pinned tabs would prompt the modal
                # ``tab_close_prompt_if_pinned`` dialog; skip them.
                skipped += 1
                continue
            if self._request_close(browser, live, tab.tab_id):
                closed += 1
            else:
                skipped += 1
        if closed:
            self.changed.emit()
        log.workspaces.debug(
            f"close_group: group={group_id} closed={closed} skipped={skipped}")
        return (closed, skipped)

    @staticmethod
    def _close_resources(win_id: int) -> tuple[Mapping[int, Any], Any]:
        """Return ``(tab-registry, tabbed-browser)`` for *win_id*.

        Falls back to an empty registry and ``None`` browser when the window
        has no registered resources.
        """
        try:
            registry = objreg.get(
                'tab-registry', scope='window', window=win_id) or {}
            browser = objreg.get(
                'tabbed-browser', scope='window', window=win_id)
        except (KeyError, objreg.RegistryUnavailableError):
            return {}, None
        return registry, browser

    @staticmethod
    def _request_close(browser: Any, live: Any, tab_id: int) -> bool:
        """Emit ``tabCloseRequested`` for *live*; return True on success."""
        try:
            widget_idx = browser.widget.indexOf(live)
            if widget_idx < 0:
                return False
            browser.widget.tabCloseRequested.emit(widget_idx)
            return True
        except Exception as e:  # noqa: BLE001 — closing is best-effort
            log.workspaces.debug(
                f"close_group: tabCloseRequested raised for {tab_id}: {e}")
            return False

    def _all_window_resources(self) -> list[tuple[int, Mapping[int, Any], Any]]:
        """Return ``(win_id, registry, browser)`` for every open window.

        Used by :meth:`reopen_group` to support cross-window URL reuse. Any
        window whose ``tab-registry`` / ``tabbed-browser`` is unavailable is
        skipped. When no window is registered at all, a single safe default
        ``(0, {}, None)`` is returned so callers can rely on ``[0]`` existing.
        """
        win_ids = getattr(objreg.window_registry, 'window_ids', None)
        if win_ids is None:
            try:
                win_ids = list(objreg.window_registry)
            except TypeError:  # pragma: no cover - defensive
                win_ids = []
        win_ids = list(win_ids) or [0]

        out: list[tuple[int, Mapping[int, Any], Any]] = []
        for win_id in win_ids:
            try:
                registry = objreg.get(
                    'tab-registry', scope='window', window=win_id)
                browser = objreg.get(
                    'tabbed-browser', scope='window', window=win_id)
            except (KeyError, objreg.RegistryUnavailableError):
                continue
            if registry is None or browser is None:
                continue
            out.append((win_id, registry, browser))
        return out or [(0, {}, None)]

    def _find_live_tab_in_windows(
            self,
            windows: Iterable[tuple[int, Mapping[int, Any], Any]],
            urlstr: str,
    ) -> Optional[tuple[Any, Any, int]]:
        """Return ``(browser, live_tab, win_id)`` for the first URL match.

        This method takes a URL **string**, not a `QUrl`. For `QUrl` input,
        read `.toString()` first.
        """
        for win_id, registry, browser in windows:
            live = self._live_tab_for_url(registry, urlstr)
            if live is not None:
                return browser, live, win_id
        return None

    def _focus_live_tab(
            self,
            workspace_id: uuid.UUID,
            tab: WorkspaceTab,
            browser: Any,
            live: Any,
            win_id: int,
    ) -> bool:
        """Focus *live*, raise its window and flip *tab* to OPEN.

        Returns True on success.  The window identified by *win_id* (the one
        owning the reused tab) is raised / activated so a cross-window reuse
        brings the hit into view.
        """
        try:
            browser.widget.setCurrentWidget(live)
            self.set_open_status(workspace_id, tab.tab_id, 'OPEN')
        except Exception as e:  # noqa: BLE001 - reopening is best-effort
            log.workspaces.debug(
                f"reopen_group: setCurrentWidget failed for {tab.tab_id}: {e}")
            return False
        try:
            window = objreg.get(
                'main-window', scope='window', window=win_id)
            if window is not None:
                window.raise_()
                window.activateWindow()
        except (KeyError, objreg.RegistryUnavailableError):
            pass
        return True

    @staticmethod
    def _open_url_in_browser(browser: Any, urlstr: str) -> bool:
        """Open *urlstr* via ``tabopen``; return True on success.

        This method takes a URL **string**, not a `QUrl`. For `QUrl` input,
        read `.toString()` first.
        """
        try:
            qurl = QUrl(urlstr)
            qtutils.ensure_valid(qurl)
            browser.tabopen(qurl, background=False)
            return True
        except Exception as e:  # noqa: BLE001 - reopening is best-effort
            log.workspaces.debug(
                f"reopen_group: tabopen failed for url={urlstr!r}: {e}")
            return False

    @staticmethod
    def _notify_reopen_progress(
            on_progress: Optional[Callable[[int, int, int], None]],
            done: int,
            total: int,
            skipped: int,
    ) -> None:
        """Call *on_progress*, swallowing any exception it raises."""
        if on_progress is None:
            return
        try:
            on_progress(done, total, skipped)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass

    def reopen_group(
            self,
            workspace_id: uuid.UUID,
            group_id: uuid.UUID,
            *,
            win_id: int = 0,
            on_progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> tuple[int, int, int]:
        """Reopen every SUSPENDED tab in *group_id*.

        For each SUSPENDED entry:

        * A live tab in *any* window whose URL matches is focused via
          ``setCurrentWidget`` and the entry is flipped to OPEN through
          :meth:`set_open_status`; the owning window is raised. This is
          counted as *reused*.
        * Otherwise the URL is opened via ``tabopen`` in the *win_id* window;
          the bridge's ``_register_tab`` (VP-D1) auto-rebinds the entry and
          flips it to OPEN. This is counted as *opened*.
        * Empty URLs, missing browsers and any exception raised by
          ``setCurrentWidget`` / ``tabopen`` are counted as *skipped* and do
          not abort the batch.

        :attr:`changed` is emitted when at least one tab was opened or reused.

        *on_progress*, when given, is called after every processed
        SUSPENDED entry with ``(done, total, skipped)``, where *done* is the
        number of tabs reused or opened so far and *total* is the number of
        SUSPENDED members. Exceptions raised by the callback are swallowed.

        Returns:
            ``(opened, reused, skipped)``.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return (0, 0, 0)
        # Verify the group exists before touching any tab.
        self._find_group(workspace_id, group_id)

        total = sum(1 for tab in workspace.tabs
                    if tab.group_id == group_id
                    and tab.open_status == 'SUSPENDED')

        windows = self._all_window_resources()
        by_win = {w_win_id: w_browser
                  for w_win_id, _registry, w_browser in windows}
        cur_browser = by_win.get(win_id)

        opened = reused = skipped = 0
        for tab in list(workspace.tabs):
            if tab.group_id != group_id or tab.open_status != 'SUSPENDED':
                continue
            if not tab.url:
                skipped += 1
            else:
                hit = self._find_live_tab_in_windows(windows, tab.url)
                if hit is not None:
                    browser, live, hit_win_id = hit
                    if self._focus_live_tab(
                            workspace_id, tab, browser, live, hit_win_id):
                        reused += 1
                    else:
                        skipped += 1
                elif cur_browser is None:
                    skipped += 1
                elif self._open_url_in_browser(cur_browser, tab.url):
                    # The bridge auto-flips the entry to OPEN via _register_tab.
                    opened += 1
                else:
                    skipped += 1
            self._notify_reopen_progress(
                on_progress, opened + reused, total, skipped)

        if opened + reused:
            self.changed.emit()
        log.workspaces.debug(
            f"reopen_group: group={group_id} opened={opened} reused={reused} "
            f"skipped={skipped}")
        return (opened, reused, skipped)

    @staticmethod
    def _window_resources(win_id: int) -> tuple[Any, Mapping[int, Any]]:
        """Return ``(tabbed_browser, tab_registry)`` for *win_id*."""
        browser = objreg.get('tabbed-browser', scope='window', window=win_id)
        registry = objreg.get('tab-registry', scope='window', window=win_id)
        return browser, registry

    def _open_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            newwindow: bool,
            win_id: int,
    ) -> tuple[int, int]:
        """Open the URLs of *tab_ids*, reusing live tabs when possible.

        A live tab whose ``url().toString()`` matches a requested URL is
        focused via ``setCurrentWidget`` (counted as *reused*); otherwise
        the URL is opened through ``tabopen`` (counted as *opened*).

        Returns:
            ``(opened, reused)``.
        """
        workspace = self._workspaces.get(workspace_id)
        browser, registry = self._window_resources(win_id)

        urls: list[str] = []
        if workspace is not None:
            by_id = {tab.tab_id: tab for tab in workspace.tabs}
            for tab_id in tab_ids:
                tab = by_id.get(tab_id)
                if tab is not None and tab.url:
                    urls.append(tab.url)

        opened = 0
        reused = 0
        to_open: list[str] = []
        for url in urls:
            match = self._live_tab_for_url(registry, url)
            if match is not None:
                browser.widget.setCurrentWidget(match)
                reused += 1
            else:
                to_open.append(url)

        if not to_open:
            return 0, reused
        if newwindow:
            # The real ``TabbedBrowser.tabopen`` has no ``newwindow`` kwarg;
            # the new-window path constructs a *single* fresh ``MainWindow``
            # holding every non-reused URL, mirroring the
            # ``tabs.tabs_are_windows`` branch in
            # :meth:`TabbedBrowser.tabopen`.  The first ``tabopen`` creates
            # the window's initial tab; the rest land in the same window.
            from qutebrowser.mainwindow import mainwindow as _mw
            window = _mw.MainWindow(private=browser.is_private)
            for url in to_open:
                qurl = QUrl(url)
                qtutils.ensure_valid(qurl)
                window.tabbed_browser.tabopen(qurl)
            window.show()
        else:
            for url in to_open:
                qurl = QUrl(url)
                qtutils.ensure_valid(qurl)
                browser.tabopen(qurl)
        opened = len(to_open)
        return opened, reused

    @staticmethod
    def _live_tab_for_url(registry: Mapping[int, Any], url: str) -> Any:
        """Return the live tab in *registry* whose URL matches *url*."""
        for live in registry.values():
            try:
                if live.url().toString() == url:
                    return live
            except (AttributeError, NotImplementedError):
                continue
        return None

    def open_tabs_in_new_window(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            win_id: int = 0,
    ) -> tuple[int, int]:
        """Open *tab_ids* in a new window, reusing matching live tabs.

        Returns:
            ``(opened, reused)``.
        """
        result = self._open_tabs(
            workspace_id, tab_ids, newwindow=True, win_id=win_id)
        log.workspaces.debug(
            "open_tabs_in_new_window: workspace %s opened %d tab(s)",
            workspace_id, result[0])
        return result

    def open_tabs_as_new_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            win_id: int = 0,
    ) -> tuple[int, int]:
        """Open *tab_ids* as new tabs, reusing matching live tabs.

        Returns:
            ``(opened, reused)``.
        """
        result = self._open_tabs(
            workspace_id, tab_ids, newwindow=False, win_id=win_id)
        log.workspaces.debug(
            "open_tabs_as_new_tabs: workspace %s opened %d tab(s)",
            workspace_id, result[0])
        return result

    # --- metadata capture / collection (VP-F1) ---

    def find_tab_by_id(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
    ) -> Optional[WorkspaceTab]:
        """Return the workspace tab with *tab_id*, if present."""
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return None
        for tab in workspace.tabs:
            if tab.tab_id == tab_id:
                return tab
        return None

    def find_tab_by_url(
            self,
            workspace_id: uuid.UUID,
            urlstr: str,
    ) -> Optional[WorkspaceTab]:
        """Return the first tab in *workspace_id* with matching *urlstr*.

        This method takes a URL **string**, not a `QUrl`. For `QUrl` input,
        read `.toString()` first.

        Pure query: unlike :meth:`find_or_create_tab_by_url` it never
        creates an entry, so it is safe to call on tab switches.  An empty
        *urlstr* never matches (it cannot identify an entry).
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None or not urlstr:
            return None
        for tab in workspace.tabs:
            if tab.url == urlstr:
                return tab
        return None

    def set_open_status(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            status: str,
    ) -> int:
        """Set the OPEN/SUSPENDED state of a single tab entry.

        Valid *status* values are ``'OPEN'`` and ``'SUSPENDED'``.  A
        suspended entry is retained in the workspace (so it can be restored)
        instead of being removed.

        Args:
            workspace_id: Workspace owning the tab.
            tab_id: Runtime id of the tab entry to update.
            status: Either ``'OPEN'`` or ``'SUSPENDED'``.

        Returns:
            1 if the entry was found and updated, 0 otherwise.
        """
        if status not in ('OPEN', 'SUSPENDED'):
            raise ValueError(
                f"Invalid open status {status!r}; expected 'OPEN' or "
                "'SUSPENDED'.")
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return 0
        if tab.open_status != status:
            tab.open_status = status
            log.workspaces.debug(
                "set_open_status: workspace %s tab %s -> %s (1)",
                workspace_id, tab_id, status)
            self.changed.emit()
        return 1

    def open_tabs(self, workspace_id: uuid.UUID) -> list[WorkspaceTab]:
        """Return the tab entries in *workspace_id* which are OPEN."""
        workspace = self._workspaces[workspace_id]
        return [tab for tab in workspace.tabs if tab.open_status == 'OPEN']

    def suspended_tabs(self, workspace_id: uuid.UUID) -> list[WorkspaceTab]:
        """Return the tab entries in *workspace_id* which are SUSPENDED."""
        workspace = self._workspaces[workspace_id]
        return [tab for tab in workspace.tabs
                if tab.open_status == 'SUSPENDED']

    def find_or_create_tab_by_url(
            self,
            workspace_id: uuid.UUID,
            url: str,
            *,
            tab_id: Optional[int] = None,
    ) -> WorkspaceTab:
        """Return the tab entry matching *url*, creating one when missing.

        A non-empty URL identifies the entry: re-collecting the same URL
        must not create a duplicate. Empty URLs always create a fresh entry
        because they cannot be matched meaningfully.

        Args:
            workspace_id: Workspace to search / add to.
            url: The URL to match.
            tab_id: Tab id to use for a newly created entry. Defaults to
                ``-1`` (a placeholder for callers which only need the entry).
        """
        workspace = self._workspaces[workspace_id]
        if url:
            for tab in workspace.tabs:
                if tab.url == url:
                    return tab

        tab = WorkspaceTab(
            tab_id=tab_id if tab_id is not None else -1,
            url=url,
            source=_host(url),
            open_status='OPEN',
        )
        self.add_tab(workspace_id, tab)
        return tab

    def _rebind_tab_id(
            self,
            workspace_id: uuid.UUID,
            tab: WorkspaceTab,
            new_tab_id: int,
    ) -> bool:
        """Rebind *tab* to *new_tab_id*, clearing colliding children links.

        Returns True when the id actually changed.  A collision with another
        entry's id is ignored (the entry keeps its current id) so the caller
        never ends up with two entries sharing one runtime id.
        """
        if new_tab_id == tab.tab_id:
            return False
        workspace = self._workspaces[workspace_id]
        collision = any(
            other.tab_id == new_tab_id
            for other in workspace.tabs if other is not tab
        )
        if collision:
            return False
        old_id = tab.tab_id
        tab.tab_id = new_tab_id
        for child in workspace.tabs:
            if child.parent_id == old_id:
                child.parent_id = new_tab_id
        return True

    def update_tab_metadata(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            *,
            url: Optional[str] = None,
            title: Optional[str] = None,
            description: Optional[str] = None,
            source: Optional[str] = None,
            favicon: Optional[str] = None,
            fetched: bool = False,
            error: Optional[str] = None,
            new_tab_id: Optional[int] = None,
    ) -> Optional[WorkspaceTab]:
        """Update a tab entry's metadata, respecting the user-edited guard.

        ``None`` means "leave unchanged". Fields with a truthy
        :attr:`WorkspaceTab.user_edited` flag are never overwritten (except
        ``url``, which is the entry's identity rather than editable content).

        Args:
            workspace_id: Workspace owning the tab.
            tab_id: Runtime id of the tab entry to update.
            url: New page URL; when given, ``source`` is re-derived unless
                explicitly supplied.
            title: New page title.
            description: New page description.
            source: New site host.
            favicon: New base64-encoded PNG favicon.
            fetched: Whether this call represents a successful fetch; when
                True, ``fetch_attempts`` is incremented, ``last_fetched`` is
                stamped and ``last_error`` is cleared.
            error: Error string to record on failure.
            new_tab_id: When given, rebind the entry to this runtime tab id
                (used by VP-D1's URL-match reuse so the entry tracks the live
                tab). A colliding id is ignored.

        Returns:
            The updated tab, or None when the workspace/tab is unknown.
        """
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return None

        changed = False
        if new_tab_id is not None:
            changed = self._rebind_tab_id(
                workspace_id, tab, new_tab_id) or changed
        if url is not None and url != tab.url:
            tab.url = url
            changed = True
            if source is None:
                source = _host(url)

        updates = (
            ('title', title),
            ('description', description),
            ('source', source),
            ('favicon', favicon),
        )
        for field, value in updates:
            if value is None:
                continue
            if tab.user_edited.get(field, False):
                continue
            if getattr(tab, field) != value:
                setattr(tab, field, value)
                changed = True

        if fetched:
            tab.fetch_attempts += 1
            tab.last_fetched = _now_seconds()
            tab.last_error = None
            changed = True
        if error is not None:
            tab.last_error = error
            changed = True

        if changed:
            self.changed.emit()
        return tab

    def set_user_edited(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            field: str,
            value: bool = True,
    ) -> None:
        """Mark *field* as user-edited so auto-fetch won't overwrite it."""
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return
        tab.user_edited[field] = value
        self.changed.emit()

    def collect_tab(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            group_id: Optional[uuid.UUID] = None,
            *,
            metadata: Optional[TabMetadata] = None,
    ) -> WorkspaceTab:
        """Collect a tab, upserting by URL and backfilling metadata.

        Re-collecting an already-known URL reuses the existing entry instead
        of appending a duplicate. Fields the user edited manually are never
        overwritten.
        """
        meta = metadata or TabMetadata()
        tab = self.find_or_create_tab_by_url(
            workspace_id, meta.url, tab_id=tab_id)
        # Collecting associates the entry with a live tab.
        tab.open_status = 'OPEN'

        if meta.url:
            tab.url = meta.url
        if meta.title and not tab.user_edited.get('title', False):
            tab.title = meta.title
        if meta.description and not tab.user_edited.get(
                'description', False):
            tab.description = meta.description
        if meta.source and not tab.user_edited.get('source', False):
            tab.source = meta.source
        if meta.favicon and not tab.user_edited.get('favicon', False):
            tab.favicon = meta.favicon

        if group_id is not None:
            tab.assign_group(group_id)

        tab.fetch_attempts += 1
        tab.last_fetched = _now_seconds()
        tab.last_error = None
        self.changed.emit()
        return tab

    def collect_all_in_window(
            self,
            workspace_id: uuid.UUID,
            group_id: Optional[uuid.UUID],
            tab_ids: Iterable[int],
            *,
            tab_metadata: Optional[Mapping[int, TabMetadata]] = None,
    ) -> tuple[list[int], list[int]]:
        """Collect every collectable tab in *tab_ids* into *group_id*.

        A tab is collectable when its URL uses ``http``/``https`` and has a
        non-empty host. Internal pages (``qute://*``, ``about:blank``,
        ``chrome://*``, ``view-source:*``, ...) are skipped silently.

        Returns:
            ``(collected, skipped)`` lists of tab ids.
        """
        metadata_by_id = tab_metadata or {}
        collected: list[int] = []
        skipped: list[int] = []
        for tab_id in tab_ids:
            meta = metadata_by_id.get(tab_id, TabMetadata())
            parsed = QUrl(meta.url)
            scheme = parsed.scheme().lower()
            if scheme not in ('http', 'https') or not parsed.host():
                skipped.append(tab_id)
                continue
            self.collect_tab(
                workspace_id, tab_id, group_id, metadata=meta)
            collected.append(tab_id)
        return collected, skipped

    # --- reading status / auto-cleanup (VP-F3) ---

    def _record_done_at(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            status: str,
    ) -> None:
        """Stamp ``done_at`` when *status* is ``DONE``, else clear it."""
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return
        tab.done_at = _now_seconds() if status == 'DONE' else None

    def set_reading_status(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            status: str,
    ) -> int:
        """Set the reading status of the given tabs in one batch.

        Valid *status* values are ``'FRESH'``, ``'ACTIVE'`` and ``'DONE'``.
        :attr:`changed` is emitted at most once, and only when at least one
        tab was matched (so a no-op batch stays silent).

        Args:
            workspace_id: Workspace owning the tabs.
            tab_ids: Runtime ids of the tabs to update. Unknown ids are
                ignored.
            status: One of the :data:`READING_STATUSES`.

        Returns:
            The number of tabs that were matched (and assigned *status*).
        """
        # Normalise lowercase callers (e.g. ``'done'``) before validation so
        # both the :vp-status-set / :vp-batch-status commands and direct
        # callers can use the same case-insensitive API surface.
        status = status.upper()
        if status not in READING_STATUSES:
            raise ValueError(
                f"Invalid reading status {status!r}; expected one of "
                f"{', '.join(READING_STATUSES)}.")
        wanted = set(tab_ids)
        workspace = self._workspaces[workspace_id]
        count = 0
        for tab in workspace.tabs:
            if tab.tab_id in wanted:
                tab.reading_status = status
                self._record_done_at(workspace_id, tab.tab_id, status)
                count += 1
        if count > 0:
            log.workspaces.debug(
                "set_reading_status: workspace %s -> %s (%d)",
                workspace_id, status, count)
            self.changed.emit()
        return count

    def auto_suspend_due_tabs(
            self,
            workspace_id: uuid.UUID,
            days: Optional[int] = None,
    ) -> int:
        """Suspend ``OPEN`` ``DONE`` tab entries older than *days*.

        A tab is suspended only when *all* of the following hold:

        * its ``open_status`` is ``'OPEN'``;
        * its ``reading_status`` is ``'DONE'``;
        * it has a ``done_at`` timestamp older than the cutoff;
        * its group (if any) is not archived.

        Only the workspace entry's ``open_status`` is flipped; live browser
        tabs are never closed here. :attr:`changed` is emitted once when at
        least one entry was suspended.

        Args:
            workspace_id: Workspace whose tabs are scanned.
            days: Age in days; defaults to
                ``content.vertical_panel.auto_suspend_done_days``.

        Returns:
            The number of tab entries that were suspended.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return 0
        if days is None:
            days = config.val.content.vertical_panel.auto_suspend_done_days
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days))
        archived_groups = {group.group_id for group in workspace.groups
                           if group.archived}

        due: list[int] = []
        for tab in workspace.tabs:
            if tab.open_status != 'OPEN' or tab.reading_status != 'DONE':
                continue
            if tab.done_at is None:
                continue
            if tab.group_id is not None and tab.group_id in archived_groups:
                continue
            parsed = _parse_timestamp(tab.done_at)
            if parsed is not None and parsed < cutoff:
                due.append(tab.tab_id)

        if not due:
            return 0

        # ``set_open_status`` emits ``changed`` per mutation; suppress those
        # and emit a single signal for the whole batch.
        was_blocked = self.blockSignals(True)
        try:
            for tab_id in due:
                self.set_open_status(workspace_id, tab_id, 'SUSPENDED')
        finally:
            self.blockSignals(was_blocked)
        self.changed.emit()
        log.workspaces.debug(
            "auto_suspend_due_tabs: workspace %s suspended %d tab(s)",
            workspace_id, len(due))
        return len(due)

    def start_auto_suspend_scheduler(self, parent: QObject) -> None:
        """Run a stale-tab scan now (deferred) and every 12 hours thereafter.

        The periodic timer stays active even when
        ``auto_suspend_enabled`` is false; each scan checks the setting and
        becomes a no-op, so re-enabling it takes effect without restarting
        qutebrowser.
        """
        QTimer.singleShot(0, self._scan_all_workspaces_for_stale)
        try:
            timer = QTimer(parent)
        except TypeError:
            # Test doubles (and non-QObject parents) cannot own the timer;
            # keep it alive through ``self._auto_suspend_timer`` instead.
            timer = QTimer()
        timer.setInterval(12 * 3600 * 1000)
        timer.timeout.connect(self._scan_all_workspaces_for_stale)
        timer.start()
        self._auto_suspend_timer = timer

    def _scan_all_workspaces_for_stale(self) -> int:
        """Suspend stale ``DONE`` tabs across every loaded workspace."""
        value = config.val
        if value is None or not value.content.vertical_panel.auto_suspend_enabled:
            return 0
        total = 0
        for workspace_id in list(self._workspaces.keys()):
            total += self.auto_suspend_due_tabs(workspace_id)
        return total

    def tabs_by_status(
            self,
            workspace_id: uuid.UUID,
            status: str,
    ) -> list[WorkspaceTab]:
        """Return the tabs in *workspace_id* whose reading status matches.

        This is a pure query: no :attr:`changed` is emitted.
        """
        workspace = self._workspaces[workspace_id]
        return [tab for tab in workspace.tabs
                if tab.reading_status == status]

    def cleanup_old_done(
            self,
            workspace_id: uuid.UUID,
            days: int,
    ) -> int:
        """Remove old ``'DONE'`` suspended tabs from a workspace.

        A tab is removed only when *all* of the following hold:

        * its ``reading_status`` is ``'DONE'``;
        * its ``open_status`` is ``'SUSPENDED'`` (read via ``getattr`` with a
          ``'SUSPENDED'`` fallback so this works before VP-D1 adds the
          field);
        * its effective timestamp — ``last_fetched``, else the workspace's
          ``last_active``, else the entry's ``created_at`` — is older than
          *days*.  Entries with no usable timestamp are treated as recent and
          kept.

        Groups are never touched. :attr:`changed` is emitted once if (and
        only if) at least one tab was removed.

        Returns:
            The number of tabs that were removed.
        """
        workspace = self._workspaces[workspace_id]
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days))
        removed_ids: set[int] = set()
        kept: list[WorkspaceTab] = []
        for tab in workspace.tabs:
            if tab.reading_status != 'DONE':
                kept.append(tab)
                continue
            if getattr(tab, 'open_status', 'SUSPENDED') != 'SUSPENDED':
                kept.append(tab)
                continue
            timestamp = (tab.last_fetched or workspace.last_active
                         or tab.created_at)
            parsed = _parse_timestamp(timestamp) if timestamp else None
            if parsed is not None and parsed < cutoff:
                removed_ids.add(tab.tab_id)
            else:
                kept.append(tab)
        if removed_ids:
            workspace.tabs = kept
            # Detach any surviving children of the removed tabs so they do
            # not point at a non-existent parent (mirrors :meth:`remove_tab`).
            for tab in workspace.tabs:
                if tab.parent_id in removed_ids:
                    tab.clear_parent()
            log.workspaces.debug(
                "cleanup_old_done: workspace %s removed %d tab(s)",
                workspace_id, len(removed_ids))
            self.changed.emit()
        return len(removed_ids)

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
