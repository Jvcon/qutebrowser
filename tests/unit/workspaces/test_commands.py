# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.commands."""

import pathlib
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Optional

import pytest

from qutebrowser.api import cmdutils
from qutebrowser.mainwindow import tabtree
from qutebrowser.mainwindow.pinpanel import PINNED_TAB_MIME
from qutebrowser.qt.core import QMimeData, QModelIndex, Qt, QUrl
from qutebrowser.workspaces import commands, portability
from qutebrowser.workspaces.manager import BYTES_PER_TAB, WorkspaceManager
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)


class FakeManager:

    """A minimal WorkspaceManager stand-in for command unit tests.

    Uses real :class:`~qutebrowser.workspaces.model.Workspace` objects so
    the commands exercise real model logic, and records mutations so tests
    can assert the right manager APIs were called.
    """

    def __init__(self) -> None:
        self._workspaces: dict[uuid.UUID, Workspace] = {}
        self._active_id: Optional[uuid.UUID] = None
        self.added: list[Workspace] = []
        self.renamed: list[tuple[uuid.UUID, str]] = []
        self.archived: list[uuid.UUID] = []
        self.deleted: list[uuid.UUID] = []
        self.deleted_groups: list[uuid.UUID] = []
        self.reading_status_sets: list[tuple[set[int], str]] = []
        self.cleanups: list[tuple[uuid.UUID, int]] = []
        self.cleanup_result = 0
        self.auto_suspends: list[tuple[uuid.UUID, Optional[int]]] = []
        self.auto_suspend_result = 0
        self.suggested: list[Group] = []
        self.ignored_suggestions: list[tuple[uuid.UUID, list[str]]] = []
        self.moved: list[tuple[set[int], Optional[uuid.UUID]]] = []
        self.statuses: list[tuple[set[int], str]] = []
        self.deleted_tabs: list[int] = []
        self.opened_windows: list[tuple[uuid.UUID, set[int]]] = []
        self.opened_tabs: list[tuple[uuid.UUID, set[int]]] = []
        self.closed_groups: list[tuple[uuid.UUID, uuid.UUID, int]] = []
        self.close_group_result: tuple[int, int] = (0, 0)
        self.reopened_groups: list[tuple[uuid.UUID, uuid.UUID, int]] = []
        self.reopen_group_result: tuple[int, int, int] = (0, 0, 0)
        self.reapplied: list[uuid.UUID] = []
        self.reapply_result = 0
        self.pinned: list = []

    def add_workspace(self, name: str) -> Workspace:
        workspace = Workspace(id=uuid.uuid4(), name=name,
                              last_active='2026-01-01T00:00:00+00:00')
        self._workspaces[workspace.id] = workspace
        if self._active_id is None:
            self._active_id = workspace.id
        self.added.append(workspace)
        return workspace

    def add_workspace_with_id(
            self,
            workspace_id: uuid.UUID,
            name: str,
            *,
            archived: bool = False,
            last_active: Optional[str] = None,
            created_at: Optional[str] = None,
    ) -> Workspace:
        workspace = Workspace(
            id=workspace_id,
            name=name,
            last_active=last_active or '2026-01-01T00:00:00+00:00',
            archived=archived,
            created_at=created_at or '2026-01-01T00:00:00+00:00',
        )
        self._workspaces[workspace.id] = workspace
        if self._active_id is None:
            self._active_id = workspace.id
        self.added.append(workspace)
        return workspace

    def get_workspace_by_name(self, name: str) -> Optional[Workspace]:
        name_lower = name.lower()
        for workspace in self._workspaces.values():
            if workspace.name.lower() == name_lower:
                return workspace
        return None

    def list_all_workspaces(self) -> list[Workspace]:
        return list(self._workspaces.values())

    def get_active(self) -> Optional[Workspace]:
        if self._active_id is None:
            return None
        return self._workspaces.get(self._active_id)

    def set_active(self, workspace_id: uuid.UUID) -> None:
        self._active_id = workspace_id

    def rename_workspace(self, workspace_id: uuid.UUID,
                         new_name: str) -> None:
        self._workspaces[workspace_id].name = new_name
        self.renamed.append((workspace_id, new_name))

    def archive_workspace(self, workspace_id: uuid.UUID) -> None:
        self._workspaces[workspace_id].archived = True
        self.archived.append(workspace_id)

    def unarchive_workspace(self, workspace_id: uuid.UUID) -> None:
        self._workspaces[workspace_id].archived = False

    def delete_workspace(self, workspace_id: uuid.UUID) -> None:
        del self._workspaces[workspace_id]
        self.deleted.append(workspace_id)

    def get_workspace(self, workspace_id: uuid.UUID) -> Optional[Workspace]:
        return self._workspaces.get(workspace_id)

    # --- groups ---

    def create_group(self, workspace_id: uuid.UUID, name: str) -> Group:
        group = Group(group_id=uuid.uuid4(), name=name)
        self._workspaces[workspace_id].add_group(group)
        return group

    def _find_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> Group:
        for group in self._workspaces[workspace_id].groups:
            if group.group_id == group_id:
                return group
        raise KeyError(group_id)

    def list_groups(self, workspace_id: uuid.UUID) -> list[Group]:
        return list(self._workspaces[workspace_id].groups)

    def move_tab_to_group(self, workspace_id: uuid.UUID, tab_id: int,
                          group_id: Optional[uuid.UUID]) -> None:
        for tab in self._workspaces[workspace_id].tabs:
            if tab.tab_id == tab_id:
                tab.group_id = group_id
                return

    def set_parent(self, workspace_id: uuid.UUID, tab_id: int,
                   parent_tab_id: Optional[int]) -> None:
        workspace = self._workspaces[workspace_id]
        tab = next(
            (entry for entry in workspace.tabs if entry.tab_id == tab_id),
            None)
        if tab is None:
            return
        if parent_tab_id is None:
            tab.clear_parent()
            return
        if parent_tab_id == tab_id:
            return
        if not any(entry.tab_id == parent_tab_id for entry in workspace.tabs):
            return
        tab.parent_id = parent_tab_id

    def reapply_auto_group_rules(self, workspace_id: uuid.UUID) -> int:
        self.reapplied.append(workspace_id)
        return self.reapply_result

    def rename_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID,
                     new_name: str) -> None:
        self._find_group(workspace_id, group_id).rename(new_name)

    def pin_group(self, workspace_id: uuid.UUID,
                  group_id: uuid.UUID) -> None:
        self._find_group(workspace_id, group_id).pinned = True

    def unpin_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> None:
        self._find_group(workspace_id, group_id).pinned = False

    def archive_group(self, workspace_id: uuid.UUID,
                      group_id: uuid.UUID) -> None:
        self._find_group(workspace_id, group_id).archived = True

    def restore_group(self, workspace_id: uuid.UUID,
                      group_id: uuid.UUID) -> None:
        self._find_group(workspace_id, group_id).archived = False

    def list_archived_groups(self, workspace_id: uuid.UUID) -> list[Group]:
        return [group for group in self._workspaces[workspace_id].groups
                if group.archived]

    def delete_group(self, workspace_id: uuid.UUID,
                     group_id: uuid.UUID) -> None:
        workspace = self._workspaces[workspace_id]
        workspace.groups = [group for group in workspace.groups
                            if group.group_id != group_id]
        for tab in workspace.tabs:
            if tab.group_id == group_id:
                tab.clear_group()
        self.deleted_groups.append(group_id)

    def group_total_count(self, workspace_id: uuid.UUID,
                          group_id: uuid.UUID) -> int:
        return sum(1 for tab in self._workspaces[workspace_id].tabs
                   if tab.group_id == group_id)

    def group_open_count(self, workspace_id: uuid.UUID,
                         group_id: uuid.UUID) -> int:
        return sum(1 for tab in self._workspaces[workspace_id].tabs
                   if tab.group_id == group_id
                   and tab.open_status == 'OPEN')

    def estimate_group_memory_bytes(self, workspace_id: uuid.UUID,
                                    group_id: uuid.UUID) -> int:
        return self.group_open_count(
            workspace_id, group_id) * BYTES_PER_TAB

    def group_state(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> str:
        group = self._find_group(workspace_id, group_id)
        if group.archived:
            return 'ARCHIVED'
        if any(tab.group_id == group_id and tab.open_status == 'OPEN'
               for tab in self._workspaces[workspace_id].tabs):
            return 'OPEN'
        return 'SUSPENDED'

    def close_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID, *,
                    win_id: int = 0) -> tuple[int, int]:
        self.closed_groups.append((workspace_id, group_id, win_id))
        return self.close_group_result

    def reopen_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID,
                     *, win_id: int = 0,
                     on_progress: Optional[
                         Callable[[int, int, int], None]] = None,
                     ) -> tuple[int, int, int]:
        self.reopened_groups.append((workspace_id, group_id, win_id))
        if on_progress is not None:
            total = sum(
                1 for tab in self._workspaces[workspace_id].tabs
                if tab.group_id == group_id
                and tab.open_status == 'SUSPENDED')
            for done in range(1, total + 1):
                on_progress(done, total, 0)
        return self.reopen_group_result

    def get_or_create_inbox(self, workspace_id: uuid.UUID) -> Group:
        for group in self._workspaces[workspace_id].groups:
            if group.name.lower() == 'inbox':
                return group
        return self.create_group(workspace_id, 'Inbox')

    # --- reading status / cleanup ---

    def find_tab_by_id(self, workspace_id: uuid.UUID,
                       tab_id: int) -> Optional[WorkspaceTab]:
        for tab in self._workspaces[workspace_id].tabs:
            if tab.tab_id == tab_id:
                return tab
        return None

    def set_reading_status(self, workspace_id: uuid.UUID,
                           tab_ids: 'set[int] | list[int]', status: str) -> int:
        if status not in ('FRESH', 'ACTIVE', 'DONE'):
            raise ValueError(status)
        id_set = set(tab_ids)
        id_list = list(tab_ids)
        count = 0
        for tab in self._workspaces[workspace_id].tabs:
            if tab.tab_id in id_set:
                tab.reading_status = status
                count += 1
        # F3 tests assert on reading_status_sets with list[int] tuples;
        # F4 tests assert on statuses with set[int] tuples.  Write both so
        # neither side regresses after the merge.
        self.reading_status_sets.append((id_list, status))
        self.statuses.append((id_set, status))
        return count

    def cleanup_old_done(self, workspace_id: uuid.UUID, days: int) -> int:
        self.cleanups.append((workspace_id, days))
        return self.cleanup_result

    def auto_suspend_due_tabs(
            self, workspace_id: uuid.UUID, days: Optional[int] = None) -> int:
        self.auto_suspends.append((workspace_id, days))
        return self.auto_suspend_result

    def suggest_stale_groups(self, workspace_id: uuid.UUID,
                             **_kwargs: object) -> list[Group]:
        return list(self.suggested)

    def get_ignored_suggestion_groups(
            self, workspace_id: uuid.UUID) -> set[str]:
        return set()

    def ignore_suggestion_groups(
            self, workspace_id: uuid.UUID,
            group_ids: 'list[str] | set[str]') -> int:
        ids = [str(group_id) for group_id in group_ids]
        self.ignored_suggestions.append((workspace_id, ids))
        return len(ids)

    # --- batched operations (VP-F4) ---

    def add_tab(self, workspace_id: uuid.UUID, tab: WorkspaceTab) -> None:
        self._workspaces[workspace_id].tabs.append(tab)

    def remove_tab(self, workspace_id: uuid.UUID, tab_id: int) -> None:
        workspace = self._workspaces[workspace_id]
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.tab_id != tab_id]
        for tab in workspace.tabs:
            if tab.parent_id == tab_id:
                tab.clear_parent()

    # --- pinned tabs ---

    def add_pinned(self, tab) -> None:
        self.pinned = [entry for entry in self.pinned
                       if entry.tab_id != tab.tab_id]
        self.pinned.append(tab)

    def remove_pinned(self, tab_id: int) -> None:
        self.pinned = [entry for entry in self.pinned
                       if entry.tab_id != tab_id]

    def list_pinned(self):
        return list(self.pinned)

    def move_pinned(self, tab_id: int, new_index: int) -> bool:
        entry = next(
            (item for item in self.pinned if item.tab_id == tab_id), None)
        if entry is None or not 0 <= new_index < len(self.pinned):
            return False
        self.pinned.remove(entry)
        self.pinned.insert(new_index, entry)
        return True

    def move_tabs(self, workspace_id: uuid.UUID, tab_ids: set[int],
                  group_id: Optional[uuid.UUID]) -> int:
        id_set = set(tab_ids)
        count = 0
        for tab in self._workspaces[workspace_id].tabs:
            if tab.tab_id in id_set:
                tab.assign_group(group_id)
                count += 1
        self.moved.append((id_set, group_id))
        return count

    def delete_tabs(self, workspace_id: uuid.UUID,
                    tab_ids: set[int], *,
                    confirm: bool = True) -> int:
        if confirm:
            return 0
        workspace = self._workspaces[workspace_id]
        id_set = set(tab_ids)
        removed = [tab for tab in workspace.tabs if tab.tab_id in id_set]
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.tab_id not in id_set]
        self.deleted_tabs.extend(tab.tab_id for tab in removed)
        return len(removed)

    def open_tabs_in_new_window(
            self, workspace_id: uuid.UUID, tab_ids: set[int],
            *, win_id: int = 0) -> tuple[int, int]:
        self.opened_windows.append((workspace_id, set(tab_ids)))
        return (3, 1)

    def open_tabs_as_new_tabs(
            self, workspace_id: uuid.UUID, tab_ids: set[int],
            *, win_id: int = 0) -> tuple[int, int]:
        self.opened_tabs.append((workspace_id, set(tab_ids)))
        return (2, 0)


class FakeTab:

    """A minimal live-tab stand-in exposing the accessors ``_metadata_from_tab`` uses."""

    def __init__(self, tab_id: int, url: str = '', title: str = '') -> None:
        self.tab_id = tab_id
        self._url = url
        self._title = title

    def url(self) -> QUrl:
        return QUrl(self._url)

    def title(self) -> str:
        return self._title


class FakeModel:

    """Minimal TabTreeModel stand-in for selection commands."""

    def __init__(self) -> None:
        self.selected: set[int] = set()

    def selected_tab_ids(self) -> set[int]:
        return set(self.selected)

    def set_selected(self, tab_id: int, selected: bool) -> None:
        if selected:
            self.selected.add(tab_id)
        else:
            self.selected.discard(tab_id)

    def select_all(self, selected: bool = True) -> None:
        if not selected:
            self.selected.clear()


class FakeTree:

    """Minimal TabTree stand-in exposing the selection API."""

    def __init__(self) -> None:
        self._model = FakeModel()
        self._selection_mode = False

    def model(self) -> FakeModel:
        return self._model

    def selection_mode(self) -> bool:
        return self._selection_mode

    def set_selection_mode(self, enabled: bool) -> None:
        self._selection_mode = enabled


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    """A FakeManager wired into the commands module."""
    manager = FakeManager()
    monkeypatch.setattr(commands, '_manager', lambda: manager)
    return manager


@pytest.fixture
def auto_cleanup_days(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch ``commands.config`` with a mutable auto_cleanup_days value."""
    holder = [30]

    class _Val:

        @property
        def content(self) -> SimpleNamespace:
            return SimpleNamespace(
                vertical_panel=SimpleNamespace(
                    auto_cleanup_days=holder[0]))

    monkeypatch.setattr(commands, 'config', SimpleNamespace(val=_Val()))
    return holder


@pytest.fixture
def auto_suspend_days(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch ``commands.config`` with a mutable auto-suspend days value."""
    holder = [30]

    class _Val:

        @property
        def content(self) -> SimpleNamespace:
            return SimpleNamespace(
                vertical_panel=SimpleNamespace(
                    auto_suspend_done_days=holder[0],
                    auto_cleanup_days=30))

    monkeypatch.setattr(commands, 'config', SimpleNamespace(val=_Val()))
    return holder


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture message.info() calls made by the commands."""
    shown: list[str] = []

    def info(text: str, **_kwargs: object) -> None:
        shown.append(text)

    monkeypatch.setattr(commands.message, 'info', info)
    return shown


@pytest.fixture
def confirmations(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    """Capture confirm_async() calls, storing the pending yes_action."""
    pending: list[Callable[[], None]] = []

    def confirm_async(*, yes_action: Callable[[], None],
                      **_kwargs: object) -> None:
        pending.append(yes_action)

    monkeypatch.setattr(commands.message, 'confirm_async', confirm_async)
    return pending


@pytest.fixture
def confirm_cancels(
        monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    """Capture confirm_async() no_action callbacks, storing the pending one."""
    pending: list[Callable[[], None]] = []

    def confirm_async(*, yes_action: Callable[[], None],
                      no_action: Optional[Callable[[], None]] = None,
                      **_kwargs: object) -> None:
        if no_action is not None:
            pending.append(no_action)

    monkeypatch.setattr(commands.message, 'confirm_async', confirm_async)
    return pending


@pytest.fixture
def fake_tree(monkeypatch: pytest.MonkeyPatch) -> FakeTree:
    """A FakeTree wired into objreg so the :vp-select-* commands find it."""
    tree = FakeTree()

    def fake_get(name: str, **_kwargs: object) -> object:
        if name == 'tab-tree':
            return tree
        raise KeyError(name)

    monkeypatch.setattr(commands.objreg, 'get', fake_get)
    return tree


class TestWorkspaceCreate:

    def test_create_registers_workspace(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        commands.workspace_create('product-rd')

        assert [ws.name for ws in fake_manager.added] == ['product-rd']
        workspace = fake_manager.added[0]
        assert fake_manager.get_active() == workspace
        assert messages == [f"Created workspace 'product-rd' "
                            f"(id {workspace.id})."]

    def test_create_stamps_id_and_timestamps(
            self, fake_manager: FakeManager) -> None:
        commands.workspace_create('dev')
        workspace = fake_manager.added[0]
        assert workspace.id is not None
        assert workspace.last_active is not None

    def test_create_refuses_duplicate_case_insensitive(
            self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('Dev')
        with pytest.raises(cmdutils.CommandError):
            commands.workspace_create('dev')
        # No additional workspace must have been registered.
        assert [ws.name for ws in fake_manager.added] == ['Dev']


class TestWorkspaceList:

    def test_list_marks_active(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        fake_manager.add_workspace('a')
        second = fake_manager.add_workspace('b')
        fake_manager.set_active(second.id)

        commands.workspace_list()

        assert messages == ['  a\n* b']

    def test_list_flags_archived(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        archived = fake_manager.add_workspace('old')
        fake_manager.add_workspace('current')
        fake_manager.archive_workspace(archived.id)

        commands.workspace_list()

        assert messages == ['* old (archived)\n  current']

    def test_list_empty(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        commands.workspace_list()
        assert messages == ['No workspaces yet.']


class TestWorkspaceRename:

    def test_rename_updates_name(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('old')

        commands.workspace_rename('old', 'new')

        assert workspace.name == 'new'
        assert fake_manager.renamed == [(workspace.id, 'new')]
        assert messages == ["Renamed workspace 'old' to 'new'."]

    def test_rename_refuses_collision_case_insensitive(
            self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('taken')
        workspace = fake_manager.add_workspace('mine')

        with pytest.raises(cmdutils.CommandError):
            commands.workspace_rename('mine', 'TAKEN')
        assert workspace.name == 'mine'
        assert fake_manager.renamed == []

    def test_rename_to_same_name_is_noop(
            self, fake_manager: FakeManager) -> None:
        workspace = fake_manager.add_workspace('Foo')

        commands.workspace_rename('foo', 'Foo')

        assert fake_manager.renamed == []
        assert workspace.name == 'Foo'

    def test_rename_missing_raises(self, fake_manager: FakeManager) -> None:
        with pytest.raises(cmdutils.CommandError, match='No workspace'):
            commands.workspace_rename('nope', 'new')


class TestWorkspaceArchive:

    def test_archive_marks_archived(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('old')

        commands.workspace_archive('old')

        assert workspace.archived is True
        assert fake_manager.archived == [workspace.id]
        assert messages == ["Archived workspace 'old'."]

    def test_archive_already_archived_is_noop(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('old')
        fake_manager.archive_workspace(workspace.id)

        commands.workspace_archive('old')

        assert fake_manager.archived == [workspace.id]
        assert messages == ["Workspace 'old' is already archived."]

    def test_archive_missing_raises(self, fake_manager: FakeManager) -> None:
        with pytest.raises(cmdutils.CommandError, match='No workspace'):
            commands.workspace_archive('nope')


class TestWorkspaceDelete:

    def test_delete_confirms_then_deletes(
            self,
            fake_manager: FakeManager,
            confirmations: list[Callable[[], None]],
            messages: list[str],
    ) -> None:
        ft = fake_manager.add_workspace('other')
        fake_manager.set_active(ft.id)
        workspace = fake_manager.add_workspace('old')

        commands.workspace_delete('old')

        assert len(confirmations) == 1
        confirmations[0]()
        assert fake_manager.deleted == [workspace.id]
        assert messages == ["Deleted workspace 'old'."]

    def test_delete_refuses_active(
            self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('active')

        with pytest.raises(cmdutils.CommandError,
                           match="Can't delete the active workspace"):
            commands.workspace_delete('active')
        assert fake_manager.get_workspace_by_name('active') is not None

    def test_delete_missing_raises(self, fake_manager: FakeManager) -> None:
        with pytest.raises(cmdutils.CommandError, match='No workspace'):
            commands.workspace_delete('nope')


class TestWorkspaceSwitch:

    def test_switch_sets_active(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        fake_manager.add_workspace('first')
        second = fake_manager.add_workspace('second')

        commands.workspace_switch('second')

        assert fake_manager.get_active() is second
        assert messages == ["Switched to workspace 'second'."]

    def test_switch_to_same_is_noop(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        fake_manager.add_workspace('first')

        commands.workspace_switch('first')

        assert messages == ["Already on workspace 'first'."]

    def test_switch_missing_raises(self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('first')
        with pytest.raises(cmdutils.CommandError, match='No workspace'):
            commands.workspace_switch('nope')


class TestWorkspaceCycle:

    def test_next_wraps_to_first(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        first = fake_manager.add_workspace('first')
        fake_manager.add_workspace('second')
        third = fake_manager.add_workspace('third')
        fake_manager.set_active(third.id)

        commands.workspace_next()

        assert fake_manager.get_active() is first
        assert messages == ["Switched to workspace 'first'."]

    def test_prev_moves_back(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        first = fake_manager.add_workspace('first')
        second = fake_manager.add_workspace('second')
        fake_manager.set_active(second.id)

        commands.workspace_prev()

        assert fake_manager.get_active() is first
        assert messages == ["Switched to workspace 'first'."]

    def test_cycle_skips_archived(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        first = fake_manager.add_workspace('first')
        hidden = fake_manager.add_workspace('hidden')
        third = fake_manager.add_workspace('third')
        fake_manager.archive_workspace(hidden.id)
        fake_manager.set_active(first.id)

        commands.workspace_next()

        assert fake_manager.get_active() is third

    def test_cycle_noop_with_one_workspace(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        fake_manager.add_workspace('only')

        commands.workspace_next()

        assert messages == ["No other workspace to switch to."]


class TestWorkspaceUnarchive:

    def test_unarchive_restores(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('old')
        fake_manager.archive_workspace(workspace.id)

        commands.workspace_unarchive('old')

        assert workspace.archived is False
        assert messages == ["Unarchived workspace 'old'."]

    def test_unarchive_not_archived_is_noop(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        fake_manager.add_workspace('old')

        commands.workspace_unarchive('old')

        assert messages == ["Workspace 'old' is not archived."]

    def test_unarchive_missing_raises(self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('old')
        with pytest.raises(cmdutils.CommandError, match='No workspace'):
            commands.workspace_unarchive('nope')


class TestAutoGroupReapply:

    def test_reapplies_on_active_workspace(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        other = fake_manager.add_workspace('other')
        fake_manager.set_active(workspace.id)
        fake_manager.reapply_result = 2

        commands.auto_group_reapply()

        assert fake_manager.reapplied == [workspace.id]
        assert workspace.id != other.id
        assert messages == ['Re-applied auto-group rules: 2 tab(s) updated.']

    def test_no_active_workspace_raises(
            self, fake_manager: FakeManager) -> None:
        with pytest.raises(cmdutils.CommandError, match='No active workspace'):
            commands.auto_group_reapply()


class TestGroupList:

    def test_lists_groups(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.create_group(workspace.id, 'github')
        fake_manager.create_group(workspace.id, 'docs')

        commands.group_list()

        assert messages == ['github\ndocs']

    def test_no_groups(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        fake_manager.add_workspace('w')

        commands.group_list()

        assert messages == ['No groups in this workspace.']


class TestTabMoveToGroup:

    def test_move_into_group(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'github')
        tab = WorkspaceTab(tab_id=1, url='https://github.com/x')
        workspace.tabs.append(tab)

        commands.tab_move_to_group(1, 'github')

        assert tab.group_id == group.group_id
        assert messages == ["Moved tab 1 to github."]

    def test_move_to_root(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'github')
        tab = WorkspaceTab(tab_id=1, url='https://github.com/x',
                           group_id=group.group_id)
        workspace.tabs.append(tab)

        commands.tab_move_to_group(1, 'root')

        assert tab.group_id is None
        assert messages == ["Moved tab 1 to root."]

    def test_move_missing_group_raises(
            self, fake_manager: FakeManager) -> None:
        workspace = fake_manager.add_workspace('w')
        tab = WorkspaceTab(tab_id=1, url='https://github.com/x')
        workspace.tabs.append(tab)

        with pytest.raises(cmdutils.CommandError, match='No group'):
            commands.tab_move_to_group(1, 'nope')


class TestPintabStyleToggle:

    def test_toggle_icon_to_line(self, monkeypatch, messages) -> None:
        val = type('Val', (), {
            'pinned_tabs': type('Pinned', (), {'style': 'icon'})(),
        })()
        calls = []

        class FakeInstance:
            def set_obj(self, option: str, value: str) -> None:
                calls.append((option, value))

        monkeypatch.setattr(commands.config, 'val', val)
        monkeypatch.setattr(commands.config, 'instance', FakeInstance())

        commands.pintab_style_toggle()

        assert calls == [('pinned_tabs.style', 'line')]
        assert messages == ['Pinned-tab style: line.']


class TestPintabCommands:

    def _workspace_with_tab(self, fake_manager):
        workspace = fake_manager.add_workspace('w')
        workspace.tabs.append(WorkspaceTab(
            tab_id=1, title='One', url='https://example.com/',
            open_status='OPEN'))
        return workspace

    def test_add_pins_entry_and_removes_workspace_entry(
            self, fake_manager, messages):
        workspace = self._workspace_with_tab(fake_manager)

        commands.pintab_add('1')

        assert [entry.tab_id for entry in fake_manager.list_pinned()] == [1]
        assert workspace.tabs == []
        assert messages == ['Pinned tab 1.']

    def test_add_resolves_url(self, fake_manager, messages):
        workspace = self._workspace_with_tab(fake_manager)

        commands.pintab_add('https://example.com/')

        assert [entry.tab_id for entry in fake_manager.list_pinned()] == [1]
        assert workspace.tabs == []

    def test_add_then_drop_into_tree_has_single_entry(
            self, fake_manager, qtbot):
        """A pinned entry dropped back into the tree is not duplicated."""
        workspace = self._workspace_with_tab(fake_manager)
        commands.pintab_add('1')
        model = tabtree.TabTreeModel(fake_manager)
        assert model.rowCount(QModelIndex()) == 0

        mime = QMimeData()
        mime.setData(PINNED_TAB_MIME, b'1')
        assert model.dropMimeData(
            mime, Qt.DropAction.CopyAction, -1, -1, QModelIndex())

        entries = [tab for tab in workspace.tabs if tab.tab_id == 1]
        assert len(entries) == 1
        # No live browser tab backs this entry in this unit test, so the drop
        # must store it SUSPENDED rather than fabricate an OPEN row (issue
        # #47). The OPEN case is covered against a live tab in
        # tests/unit/mainwindow/test_tabtree.py.
        assert entries[0].open_status == 'SUSPENDED'
        assert fake_manager.list_pinned() == []

    def test_remove_unpins_without_restoring_workspace_entry(
            self, fake_manager, messages):
        workspace = self._workspace_with_tab(fake_manager)
        commands.pintab_add('1')

        commands.pintab_remove('1')

        assert fake_manager.list_pinned() == []
        assert workspace.tabs == []
        assert messages[-1] == 'Unpinned tab 1.'

    def test_remove_unknown_raises(self, fake_manager):
        with pytest.raises(cmdutils.CommandError, match='No pinned tab'):
            commands.pintab_remove('1')

    def test_move_reorders_by_position(self, fake_manager, messages):
        self._workspace_with_tab(fake_manager)
        for tab_id in (1, 2, 3):
            fake_manager.add_pinned(
                PinnedTab(tab_id=tab_id, title=str(tab_id), url='u'))

        commands.pintab_move('1', '0')

        assert [entry.tab_id for entry in fake_manager.list_pinned()] == [1, 2, 3]
        commands.pintab_move('1', '3')
        assert [entry.tab_id for entry in fake_manager.list_pinned()] == [2, 3, 1]
        assert messages[-1] == 'Moved pinned tab 1 to position 2.'


class TestGroupCommands:

    def test_new_creates_group(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')

        commands.vp_group_new('github')

        assert [group.name for group in workspace.groups] == ['github']
        assert messages[0].startswith("Created group 'github'")

    def test_new_refuses_duplicate_case_insensitive(
            self, fake_manager: FakeManager) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.create_group(workspace.id, 'Dev')

        with pytest.raises(cmdutils.CommandError):
            commands.vp_group_new('dev')
        assert [group.name for group in workspace.groups] == ['Dev']

    def test_new_requires_active_workspace(
            self, fake_manager: FakeManager) -> None:
        with pytest.raises(cmdutils.CommandError, match='No active workspace'):
            commands.vp_group_new('g')

    def test_rename_by_name(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'old')

        commands.vp_group_rename('old', 'new')

        assert group.name == 'new'
        assert messages == ["Renamed group 'old' to 'new'."]

    def test_rename_by_uuid(
            self, fake_manager: FakeManager) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'old')

        commands.vp_group_rename(str(group.group_id), 'new')

        assert group.name == 'new'

    def test_rename_refuses_collision(
            self, fake_manager: FakeManager) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.create_group(workspace.id, 'taken')
        group = fake_manager.create_group(workspace.id, 'mine')

        with pytest.raises(cmdutils.CommandError):
            commands.vp_group_rename('mine', 'TAKEN')
        assert group.name == 'mine'

    def test_rename_missing_raises(self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('w')
        with pytest.raises(cmdutils.CommandError, match='No group'):
            commands.vp_group_rename('nope', 'new')

    def test_pin_and_unpin(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')

        commands.vp_group_pin('g')
        assert group.pinned is True
        assert messages == ["Pinned group 'g'."]

        commands.vp_group_unpin('g')
        assert group.pinned is False
        assert messages[-1] == "Unpinned group 'g'."

    def test_archive_then_restore(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')

        commands.vp_group_archive('g')
        assert group.archived is True
        assert fake_manager.list_archived_groups(workspace.id) == [group]
        assert messages[-1] == "Archived group 'g'."

        commands.vp_group_restore('g')
        assert group.archived is False
        assert fake_manager.list_archived_groups(workspace.id) == []
        assert messages[-1] == "Restored group 'g'."

    def test_archive_already_archived_is_noop(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        group.archived = True

        commands.vp_group_archive('g')

        assert messages == ["Group 'g' is already archived."]

    def test_restore_not_archived_is_noop(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.create_group(workspace.id, 'g')

        commands.vp_group_restore('g')

        assert messages == ["Group 'g' is not archived."]

    def test_delete_confirms_then_makes_members_standalone(
            self,
            fake_manager: FakeManager,
            confirmations: list[Callable[[], None]],
            messages: list[str],
    ) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        workspace.tabs.append(
            WorkspaceTab(tab_id=1, group_id=group.group_id))
        workspace.tabs.append(WorkspaceTab(tab_id=2))

        commands.vp_group_delete('g')

        assert len(confirmations) == 1
        confirmations[0]()

        assert fake_manager.deleted_groups == [group.group_id]
        assert workspace.groups == []
        assert [tab.tab_id for tab in workspace.tabs] == [1, 2]
        assert workspace.tabs[0].group_id is None
        assert messages == [
            "Deleted group 'g'; 1 tab(s) became standalone."]

    def test_delete_empty_group_skips_confirmation(
            self,
            fake_manager: FakeManager,
            confirmations: list[Callable[[], None]],
            messages: list[str],
    ) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')

        commands.vp_group_delete('g')

        assert confirmations == []
        assert fake_manager.deleted_groups == [group.group_id]
        assert workspace.groups == []
        assert messages == [
            "Deleted group 'g'; 0 tab(s) became standalone."]

    def test_delete_confirm_says_tabs_are_kept(
            self,
            fake_manager: FakeManager,
            monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        workspace.tabs.append(
            WorkspaceTab(tab_id=1, group_id=group.group_id))
        workspace.tabs.append(
            WorkspaceTab(tab_id=2, group_id=group.group_id))
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            commands.message, 'confirm_async',
            lambda **kwargs: captured.update(kwargs))

        commands.vp_group_delete('g')

        text = str(captured['text'])
        assert "all its tabs" not in str(captured['title'])
        assert "cannot be undone" not in text
        assert "2 tab(s) as top-level tabs" in text
        assert captured['default'] is False
        assert callable(captured['yes_action'])

    def test_delete_missing_raises(self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('w')
        with pytest.raises(cmdutils.CommandError, match='No group'):
            commands.vp_group_delete('nope')


class TestGroupClose:

    """Tests for ``:vp-group-close`` (VP-D3 / VP-P1)."""

    def _open_group(self, fake_manager: FakeManager, count: int = 2):
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        for tab_id in range(1, count + 1):
            workspace.tabs.append(WorkspaceTab(
                tab_id=tab_id, group_id=group.group_id, open_status='OPEN'))
        return workspace, group

    def test_confirms_before_closing(
            self, fake_manager: FakeManager,
            confirmations: list[Callable[[], None]],
            messages: list[str]) -> None:
        workspace, group = self._open_group(fake_manager)
        fake_manager.close_group_result = (2, 0)

        commands.vp_group_close('g')

        # Nothing is closed until the user confirms.
        assert len(confirmations) == 1
        assert fake_manager.closed_groups == []
        assert messages == []

        confirmations[0]()

        assert fake_manager.closed_groups == [
            (workspace.id, group.group_id, 0)]
        assert messages == [
            "Closed 2 tab(s) in 'g', freed ~100.00MB; skipped 0."]

    def test_confirm_mentions_count_and_memory(
            self, fake_manager: FakeManager,
            monkeypatch: pytest.MonkeyPatch) -> None:
        self._open_group(fake_manager, count=3)
        captured: dict[str, object] = {}

        def confirm_async(**kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(commands.message, 'confirm_async', confirm_async)

        commands.vp_group_close('g')

        assert captured['title'] == "Close group 'g'?"
        assert captured['text'] == (
            "This will close 3 tab(s), freeing ~150.00MB.")
        assert captured['default'] is False

    def test_force_skips_confirmation(
            self, fake_manager: FakeManager,
            confirmations: list[Callable[[], None]],
            messages: list[str]) -> None:
        workspace, group = self._open_group(fake_manager)
        fake_manager.close_group_result = (2, 1)

        commands.vp_group_close('g', force=True)

        assert confirmations == []
        assert fake_manager.closed_groups == [
            (workspace.id, group.group_id, 0)]
        assert messages == [
            "Closed 2 tab(s) in 'g', freed ~100.00MB; skipped 1."]

    def test_cancel_leaves_group_untouched(
            self, fake_manager: FakeManager,
            confirm_cancels: list[Callable[[], None]],
            messages: list[str]) -> None:
        self._open_group(fake_manager)

        commands.vp_group_close('g')
        assert fake_manager.closed_groups == []
        assert len(confirm_cancels) == 1

        confirm_cancels[0]()

        assert fake_manager.closed_groups == []
        assert messages == ["Cancelled."]

    def test_passes_win_id_through(
            self, fake_manager: FakeManager) -> None:
        workspace, group = self._open_group(fake_manager, count=1)
        fake_manager.close_group_result = (1, 0)

        commands.vp_group_close('g', force=True, win_id=3)

        assert fake_manager.closed_groups == [
            (workspace.id, group.group_id, 3)]

    def test_already_suspended(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        workspace.tabs.append(WorkspaceTab(
            tab_id=1, group_id=group.group_id, open_status='SUSPENDED'))

        commands.vp_group_close('g')

        assert messages == ["Group 'g' is already suspended."]

    def test_empty_group(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.create_group(workspace.id, 'g')

        commands.vp_group_close('g')

        assert messages == ["Group 'g' has no open tabs."]

    def test_archived_group(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        group.archived = True

        commands.vp_group_close('g')

        assert messages == ["Group 'g' is archived."]

    def test_missing_group_raises(self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('w')
        with pytest.raises(cmdutils.CommandError, match='No group'):
            commands.vp_group_close('nope')


class TestGroupReopen:

    """Tests for ``:vp-group-reopen`` (VP-D4)."""

    def test_reports_counts(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        for tab_id in range(1, 4):
            workspace.tabs.append(WorkspaceTab(
                tab_id=tab_id, group_id=group.group_id,
                open_status='SUSPENDED'))
        fake_manager.reopen_group_result = (2, 1, 0)

        commands.vp_group_reopen('g')

        assert fake_manager.reopened_groups == [
            (workspace.id, group.group_id, 0)]
        assert messages == [
            "恢复中 1/3 (g)",
            "恢复中 2/3 (g)",
            "恢复中 3/3 (g)",
            "Reopened group 'g': opened 2, reused 1, skipped 0."]

    def test_passes_win_id_through(
            self, fake_manager: FakeManager) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        workspace.tabs.append(WorkspaceTab(
            tab_id=1, group_id=group.group_id, open_status='SUSPENDED'))
        fake_manager.reopen_group_result = (1, 0, 0)

        commands.vp_group_reopen('g', win_id=4)

        assert fake_manager.reopened_groups == [
            (workspace.id, group.group_id, 4)]

    def test_already_open(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        workspace.tabs.append(WorkspaceTab(
            tab_id=1, group_id=group.group_id, open_status='OPEN'))

        commands.vp_group_reopen('g')

        assert fake_manager.reopened_groups == []
        assert messages == ["Group 'g' is already open."]

    def test_archived(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        group.archived = True

        commands.vp_group_reopen('g')

        assert fake_manager.reopened_groups == []
        assert messages == ["Group 'g' is archived; restore it first."]

    def test_no_suspended_tabs(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.create_group(workspace.id, 'g')
        fake_manager.reopen_group_result = (0, 0, 0)

        commands.vp_group_reopen('g')

        assert messages == ["Group 'g' has no suspended tabs."]

    def test_missing_group_raises(self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('w')
        with pytest.raises(cmdutils.CommandError, match='No group'):
            commands.vp_group_reopen('nope')


class TestStatusCommands:

    def _seed(self, fake_manager: FakeManager) -> tuple[Workspace, WorkspaceTab]:
        workspace = fake_manager.add_workspace('w')
        tab = WorkspaceTab(tab_id=1, url='https://example.com/')
        workspace.tabs.append(tab)
        return workspace, tab

    def test_set_by_int_normalizes_case(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        _, tab = self._seed(fake_manager)

        commands.vp_status_set('1', 'active')

        assert tab.reading_status == 'ACTIVE'
        assert fake_manager.reading_status_sets == [([1], 'ACTIVE')]

    def test_set_by_url(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        _, tab = self._seed(fake_manager)

        commands.vp_status_set('https://example.com/', 'done')

        assert tab.reading_status == 'DONE'
        assert fake_manager.reading_status_sets == [([1], 'DONE')]

    def test_set_unknown_int_raises(self, fake_manager: FakeManager) -> None:
        self._seed(fake_manager)
        with pytest.raises(cmdutils.CommandError, match='No tab'):
            commands.vp_status_set('99', 'DONE')

    def test_set_unknown_url_raises(self, fake_manager: FakeManager) -> None:
        self._seed(fake_manager)
        with pytest.raises(cmdutils.CommandError, match='No tab'):
            commands.vp_status_set('https://nope.example/', 'DONE')

    def test_set_invalid_status_raises(
            self, fake_manager: FakeManager) -> None:
        self._seed(fake_manager)
        with pytest.raises(cmdutils.CommandError, match='status'):
            commands.vp_status_set('1', 'BOGUS')

    def test_set_requires_active_workspace(
            self, fake_manager: FakeManager) -> None:
        with pytest.raises(cmdutils.CommandError, match='No active workspace'):
            commands.vp_status_set('1', 'DONE')

    def test_cycle_wraps_around(
            self, fake_manager: FakeManager, messages: list[str]) -> None:
        _, tab = self._seed(fake_manager)

        commands.vp_status_cycle('1')
        assert tab.reading_status == 'ACTIVE'
        commands.vp_status_cycle('1')
        assert tab.reading_status == 'DONE'
        commands.vp_status_cycle('1')
        assert tab.reading_status == 'FRESH'

    def test_cycle_by_url(self, fake_manager: FakeManager) -> None:
        _, tab = self._seed(fake_manager)
        commands.vp_status_cycle('https://example.com/')
        assert tab.reading_status == 'ACTIVE'


class TestCleanupCommand:

    def test_uses_configured_days_when_omitted(
            self,
            fake_manager: FakeManager,
            auto_cleanup_days: list[int],
            messages: list[str],
    ) -> None:
        auto_cleanup_days[0] = 45
        workspace = fake_manager.add_workspace('w')
        fake_manager.cleanup_result = 2

        commands.vp_cleanup_done()

        assert fake_manager.cleanups == [(workspace.id, 45)]
        assert messages == ['Removed 2 done tab(s).']

    def test_explicit_days(
            self,
            fake_manager: FakeManager,
            auto_cleanup_days: list[int],
    ) -> None:
        auto_cleanup_days[0] = 30
        workspace = fake_manager.add_workspace('w')

        commands.vp_cleanup_done(60)

        assert fake_manager.cleanups == [(workspace.id, 60)]

    def test_out_of_range_falls_back_to_config_default(
            self,
            fake_manager: FakeManager,
            auto_cleanup_days: list[int],
            messages: list[str],
    ) -> None:
        auto_cleanup_days[0] = 50
        workspace = fake_manager.add_workspace('w')

        commands.vp_cleanup_done(999)

        assert fake_manager.cleanups == [(workspace.id, 50)]
        assert any('range' in text for text in messages)

    def test_silent_when_nothing_removed(
            self,
            fake_manager: FakeManager,
            auto_cleanup_days: list[int],
            messages: list[str],
    ) -> None:
        auto_cleanup_days[0] = 30
        fake_manager.add_workspace('w')
        fake_manager.cleanup_result = 0

        commands.vp_cleanup_done()

        assert messages == []

    def test_requires_active_workspace(
            self,
            fake_manager: FakeManager,
            auto_cleanup_days: list[int],
    ) -> None:
        auto_cleanup_days[0] = 30
        with pytest.raises(cmdutils.CommandError, match='No active workspace'):
            commands.vp_cleanup_done()


class TestSuspendStaleCommand:

    def test_uses_configured_days_when_omitted(
            self,
            fake_manager: FakeManager,
            auto_suspend_days: list[int],
            messages: list[str],
    ) -> None:
        auto_suspend_days[0] = 45
        workspace = fake_manager.add_workspace('w')
        fake_manager.auto_suspend_result = 2

        commands.vp_suspend_stale()

        assert fake_manager.auto_suspends == [(workspace.id, 45)]
        assert messages == ['Suspended 2 stale tab(s).']

    def test_explicit_days(
            self,
            fake_manager: FakeManager,
            auto_suspend_days: list[int],
    ) -> None:
        auto_suspend_days[0] = 30
        workspace = fake_manager.add_workspace('w')

        commands.vp_suspend_stale(60)

        assert fake_manager.auto_suspends == [(workspace.id, 60)]

    def test_out_of_range_falls_back_to_config_default(
            self,
            fake_manager: FakeManager,
            auto_suspend_days: list[int],
            messages: list[str],
    ) -> None:
        auto_suspend_days[0] = 50
        workspace = fake_manager.add_workspace('w')

        commands.vp_suspend_stale(999)

        assert fake_manager.auto_suspends == [(workspace.id, 50)]
        assert any('range' in text for text in messages)

    def test_silent_when_nothing_suspended(
            self,
            fake_manager: FakeManager,
            auto_suspend_days: list[int],
            messages: list[str],
    ) -> None:
        auto_suspend_days[0] = 30
        fake_manager.add_workspace('w')
        fake_manager.auto_suspend_result = 0

        commands.vp_suspend_stale()

        assert messages == []

    def test_requires_active_workspace(
            self,
            fake_manager: FakeManager,
            auto_suspend_days: list[int],
    ) -> None:
        auto_suspend_days[0] = 30
        with pytest.raises(cmdutils.CommandError, match='No active workspace'):
            commands.vp_suspend_stale()


class TestSuggestionCommands:

    def test_stale_prints_count_and_names(
            self,
            fake_manager: FakeManager,
            messages: list[str],
    ) -> None:
        workspace = fake_manager.add_workspace('w')
        first = fake_manager.create_group(workspace.id, 'alpha')
        second = fake_manager.create_group(workspace.id, 'beta')
        fake_manager.suggested = [first, second]

        commands.vp_suggestion_stale()

        assert messages == ['2 stale group(s): alpha, beta']

    def test_stale_silent_when_empty(
            self,
            fake_manager: FakeManager,
            messages: list[str],
    ) -> None:
        fake_manager.add_workspace('w')

        commands.vp_suggestion_stale()

        assert messages == ['No stale groups.']

    def test_ignore_adds_to_list(
            self,
            fake_manager: FakeManager,
            messages: list[str],
    ) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'old')
        fake_manager.suggested = [group]

        commands.vp_suggestion_ignore()

        assert fake_manager.ignored_suggestions == [
            (workspace.id, [str(group.group_id)])]
        assert messages == ['Ignored 1 group(s) for 7 days.']


@pytest.fixture
def pers(tmp_path: pathlib.Path) -> tuple[WorkspacePersistence,
                                         PinnedPersistence]:
    """Shared persistence rooted in a temporary directory."""
    return (
        WorkspacePersistence(base_path=tmp_path / 'workspaces'),
        PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml'),
    )


class TestBatchCommands:

    """Tests for the VP-F4 :vp-select-* / :vp-batch-* commands."""

    def test_vp_select_mode_toggles(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        assert fake_tree.selection_mode() is False

        commands.vp_select_mode(win_id=0)
        assert fake_tree.selection_mode() is True
        assert messages == ["Selection mode enabled."]

        commands.vp_select_mode(win_id=0)
        assert fake_tree.selection_mode() is False
        assert messages[-1] == "Selection mode disabled."

    def test_vp_select_toggle_adds_and_removes(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        commands.vp_select_toggle(20, win_id=0)
        assert fake_tree.model().selected_tab_ids() == {20}

        commands.vp_select_toggle(20, win_id=0)
        assert fake_tree.model().selected_tab_ids() == set()

    def test_vp_select_all_and_none(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        # select_all on the fake is a no-op for True; just check it enables
        # selection mode and clears on none.
        commands.vp_select_all(win_id=0)
        assert fake_tree.selection_mode() is True

        fake_tree.model().selected.add(1)
        commands.vp_select_none(win_id=0)
        assert fake_tree.model().selected_tab_ids() == set()

    def test_vp_batch_move_resolves_group(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        group = fake_manager.create_group(workspace.id, 'g')
        fake_manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        fake_manager.add_tab(workspace.id, WorkspaceTab(tab_id=2))
        fake_tree.model().selected.update({1, 2})

        commands.vp_batch_move('g', win_id=0)

        assert workspace.tabs[0].group_id == group.group_id
        assert workspace.tabs[1].group_id == group.group_id
        assert messages == ["Moved 2 tab(s) into 'g'."]

    def test_vp_batch_move_empty_selection_raises(
            self, fake_manager: FakeManager, fake_tree: FakeTree) -> None:
        fake_manager.add_workspace('w')
        with pytest.raises(cmdutils.CommandError, match='No tabs selected'):
            commands.vp_batch_move('g', win_id=0)

    def test_vp_batch_status_updates(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        fake_tree.model().selected.add(1)

        commands.vp_batch_status('done', win_id=0)

        assert fake_manager.statuses == [({1}, 'DONE')]
        assert messages == ["Marked 1 tab(s) as DONE."]

    def test_vp_batch_status_invalid_raises(
            self, fake_manager: FakeManager, fake_tree: FakeTree) -> None:
        fake_manager.add_workspace('w')
        fake_tree.model().selected.add(1)
        with pytest.raises(cmdutils.CommandError):
            commands.vp_batch_status('bogus', win_id=0)

    def test_vp_batch_delete_confirms(
            self,
            fake_manager: FakeManager,
            fake_tree: FakeTree,
            confirmations: list[Callable[[], None]],
            messages: list[str],
    ) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        fake_manager.add_tab(workspace.id, WorkspaceTab(tab_id=2))
        fake_tree.model().selected.update({1, 2})

        commands.vp_batch_delete(win_id=0)

        assert len(confirmations) == 1
        assert workspace.tabs  # nothing deleted before confirmation
        confirmations[0]()
        assert sorted(fake_manager.deleted_tabs) == [1, 2]
        assert workspace.tabs == []
        assert messages == ["Deleted 2 tab(s)."]

    def test_vp_batch_delete_force_skips_confirmation(
            self,
            fake_manager: FakeManager,
            fake_tree: FakeTree,
            confirmations: list[Callable[[], None]],
            messages: list[str],
    ) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        fake_tree.model().selected.add(1)

        commands.vp_batch_delete(force=True, win_id=0)

        assert confirmations == []
        assert fake_manager.deleted_tabs == [1]
        assert workspace.tabs == []

    def test_vp_batch_delete_empty_selection_raises(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            confirmations: list[Callable[[], None]],
            messages: list[str]) -> None:
        with pytest.raises(cmdutils.CommandError, match='No tabs selected'):
            commands.vp_batch_delete(win_id=0)
        assert confirmations == []
        assert messages == []

    def test_vp_batch_open_window_dispatches(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_tree.model().selected.add(1)

        commands.vp_batch_open_window(win_id=0)

        assert fake_manager.opened_windows == [(workspace.id, {1})]
        assert messages == [
            "Opened 3 tab(s) in a new window; reused 1."]

    def test_vp_batch_open_tabs_dispatches(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_tree.model().selected.add(1)

        commands.vp_batch_open_tabs(win_id=0)

        assert fake_manager.opened_tabs == [(workspace.id, {1})]
        assert messages == ["Opened 2 tab(s); reused 0."]

    def test_vp_batch_open_window_empty_selection_silent(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        commands.vp_batch_open_window(win_id=0)
        assert messages == []

    def test_vp_batch_move_reports_ignored(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        workspace = fake_manager.add_workspace('w')
        fake_manager.create_group(workspace.id, 'g')
        fake_manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        fake_tree.model().selected.update({1, 99})

        commands.vp_batch_move('g', win_id=0)

        assert messages == [
            "Moved 1 tab(s) into 'g'.", "Ignored 1 unknown id(s)."]


class TestPersistence:

    """End-to-end persistence checks behind the commands (issue #2)."""

    def test_yaml_roundtrip_without_field_loss(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
    ) -> None:
        manager = WorkspaceManager(*pers)
        workspace = manager.add_workspace('product-rd')
        group = Group(group_id=uuid.uuid4(), name='github', folded=True)
        workspace.add_group(group)
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, group_id=group.group_id))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=2, parent_id=1))
        manager.archive_workspace(workspace.id)
        manager.set_active(workspace.id)
        manager.save()

        loaded = WorkspacePersistence(
            base_path=pers[0].base_path).load(workspace.id)
        assert loaded == workspace
        # Everything which was written must come back: id, name, groups
        # (incl. folded state), tabs (incl. group/parent linkage),
        # last_active and archived.
        assert loaded.id == workspace.id
        assert loaded.name == 'product-rd'
        assert loaded.archived is True
        assert loaded.last_active == workspace.last_active
        assert loaded.groups == workspace.groups
        assert loaded.tabs == workspace.tabs

    def test_cold_restart_list_is_identical(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
    ) -> None:
        first = WorkspaceManager(*pers)
        active = first.add_workspace('work')
        first.add_workspace('play')
        first.set_active(active.id)
        first.save()

        second = WorkspaceManager(*pers)
        second.load()

        assert {ws.name for ws in second.list_all_workspaces()} == {
            'work', 'play'}

    def test_command_session_survives_cold_restart(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = WorkspaceManager(*pers)
        workspace = first.add_workspace('work')
        first.set_active(workspace.id)
        first.save()

        second = WorkspaceManager(*pers)
        second.load()
        monkeypatch.setattr(commands, '_manager', lambda: second)

        commands.workspace_create('play')
        second.save()

        third = WorkspaceManager(*pers)
        third.load()
        assert {ws.name for ws in third.list_all_workspaces()} == {
            'work', 'play'}


class TestCollectAllCommand:

    def test_no_collectable_tabs_emits_message(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            messages: list[str],
            monkeypatch: pytest.MonkeyPatch,
            qapp,
    ) -> None:
        manager = WorkspaceManager(*pers)
        workspace = manager.add_workspace('w')
        manager.create_group(workspace.id, 'g')
        manager.set_active(workspace.id)
        monkeypatch.setattr(commands, '_manager', lambda: manager)
        monkeypatch.setattr(commands.objreg, 'get', lambda *_a, **_k: {})

        commands.vp_collect_all('g', win_id=0)

        assert messages == ['No collectable tabs in this window.']

    def test_unknown_group_raises(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            monkeypatch: pytest.MonkeyPatch,
            qapp,
    ) -> None:
        manager = WorkspaceManager(*pers)
        workspace = manager.add_workspace('w')
        manager.set_active(workspace.id)
        monkeypatch.setattr(commands, '_manager', lambda: manager)

        with pytest.raises(cmdutils.CommandError, match='No group named'):
            commands.vp_collect_all('missing', win_id=0)


def _wire_manager(
    pers: tuple[WorkspacePersistence, PinnedPersistence],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkspaceManager, Workspace]:
    """Wire a real manager into the commands module for collect tests."""
    manager = WorkspaceManager(*pers)
    workspace = manager.add_workspace('w')
    manager.set_active(workspace.id)
    monkeypatch.setattr(commands, '_manager', lambda: manager)
    monkeypatch.setattr(commands, '_persist_description', lambda *_a, **_k: None)
    return manager, workspace


class TestTabCollectCommand:
    """VP-F5: ``:tab-collect`` defaults to the Inbox group."""

    def test_no_group_collects_into_inbox(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            messages: list[str],
            monkeypatch: pytest.MonkeyPatch,
            qapp,
    ) -> None:
        manager, workspace = _wire_manager(pers, monkeypatch)
        tab = FakeTab(1, url='https://example.com/')

        commands.tab_collect(tab)

        groups = manager.list_groups(workspace.id)
        assert [group.name for group in groups] == ['Inbox']
        assert workspace.tabs[0].group_id == groups[0].group_id
        assert messages == ["Collected tab into 'Inbox'."]

    def test_explicit_group_is_used(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            messages: list[str],
            monkeypatch: pytest.MonkeyPatch,
            qapp,
    ) -> None:
        manager, workspace = _wire_manager(pers, monkeypatch)
        group = manager.create_group(workspace.id, 'g')
        tab = FakeTab(1, url='https://example.com/')

        commands.tab_collect(tab, 'g')

        assert workspace.tabs[0].group_id == group.group_id
        assert manager.list_groups(workspace.id) == [group]
        assert messages == ["Collected tab into group 'g'."]

    def test_same_url_entry_is_reused_and_rebound(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            messages: list[str],
            monkeypatch: pytest.MonkeyPatch,
            qapp,
    ) -> None:
        """VP-D1: a same-URL entry is reused and rebound to the live tab."""
        manager, workspace = _wire_manager(pers, monkeypatch)
        workspace.tabs.append(
            WorkspaceTab(tab_id=1, url='https://example.com/'))
        tab = FakeTab(9, url='https://example.com/')

        commands.tab_collect(tab)

        assert [t.tab_id for t in workspace.tabs] == [9]
        groups = manager.list_groups(workspace.id)
        assert workspace.tabs[0].group_id == groups[0].group_id
        assert messages == ["Collected tab into 'Inbox'."]


class TestCollectAllInbox:
    """VP-F5: ``:vp-collect-all`` defaults to the Inbox group."""

    def test_no_group_fills_inbox(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            messages: list[str],
            monkeypatch: pytest.MonkeyPatch,
            qapp,
    ) -> None:
        manager, workspace = _wire_manager(pers, monkeypatch)
        tab = FakeTab(1, url='https://example.com/')
        monkeypatch.setattr(commands.objreg, 'get', lambda *_a, **_k: {1: tab})

        commands.vp_collect_all()

        groups = manager.list_groups(workspace.id)
        assert [group.name for group in groups] == ['Inbox']
        assert workspace.tabs[0].group_id == groups[0].group_id
        assert messages == ["Collected 1 tab(s) into 'Inbox'."]

    def test_empty_window_without_group_is_message(
            self,
            pers: tuple[WorkspacePersistence, PinnedPersistence],
            messages: list[str],
            monkeypatch: pytest.MonkeyPatch,
            qapp,
    ) -> None:
        _wire_manager(pers, monkeypatch)
        monkeypatch.setattr(commands.objreg, 'get', lambda *_a, **_k: {})

        commands.vp_collect_all()

        assert messages == ['No collectable tabs in this window.']


class TestWorkspaceExport:

    """VP-A3 export half: ``:vp-workspace-export``."""

    def test_vp_workspace_export_default_path(
            self,
            tmp_path: pathlib.Path,
            fake_manager: FakeManager,
            messages: list[str],
            monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_manager.add_workspace('research')
        monkeypatch.setattr(commands.standarddir, 'data', lambda: str(tmp_path))
        monkeypatch.setattr(portability, '_manager', lambda: fake_manager)

        commands.vp_workspace_export()

        expected = (tmp_path / 'workspaces-export.json').resolve()
        assert expected.exists()
        assert messages == [f"Exported workspaces to {expected}"]


class TestTabTreeMoveCommands:

    """``:tabtree-move`` and ``:tabtree-move-branch`` (ticket #6)."""

    @pytest.fixture
    def real_manager(self, pers, qapp, monkeypatch):
        manager = WorkspaceManager(*pers)
        monkeypatch.setattr(commands, '_manager', lambda: manager)
        src = manager.add_workspace('src')
        manager.add_workspace('dst')
        for tab_id, parent in ((10, None), (11, 10), (12, 11), (20, None)):
            manager.add_tab(src.id, WorkspaceTab(tab_id=tab_id,
                                                 parent_id=parent))
        return manager

    @staticmethod
    def _ids(manager, name):
        ws = manager.get_workspace_by_name(name)
        return {tab.tab_id: tab.parent_id for tab in ws.tabs}

    def test_registered(self):
        from qutebrowser.misc import objects
        from qutebrowser import app  # pylint: disable=unused-import
        assert 'tabtree-move' in objects.commands
        assert 'tabtree-move-branch' in objects.commands

    def test_move_single_tab(self, real_manager, messages):
        commands.tabtree_move(20, 'dst')

        assert self._ids(real_manager, 'src') == {10: None, 11: 10, 12: 11}
        assert self._ids(real_manager, 'dst') == {20: None}
        assert "Moved tab 20 to workspace 'dst'" in messages[-1]
        assert real_manager.get_active().name == 'src'

    def test_move_single_tab_promotes_children(self, real_manager, messages):
        commands.tabtree_move(11, 'dst')

        assert self._ids(real_manager, 'src') == {10: None, 12: 10, 20: None}
        assert self._ids(real_manager, 'dst') == {11: None}

    def test_move_branch_keeps_subtree(self, real_manager, messages):
        commands.tabtree_move_branch(10, 'dst')

        assert self._ids(real_manager, 'src') == {20: None}
        assert self._ids(real_manager, 'dst') == {10: None, 11: 10, 12: 11}
        assert "branch from tab 10" in messages[-1]

    def test_move_single_does_not_move_branch(self, real_manager, messages):
        commands.tabtree_move(10, 'dst')
        assert self._ids(real_manager, 'dst') == {10: None}
        assert self._ids(real_manager, 'src') == {11: None, 12: 11, 20: None}

    def test_unknown_workspace_is_command_error(self, real_manager, messages):
        with pytest.raises(cmdutils.CommandError,
                           match="No workspace with the name 'nope'"):
            commands.tabtree_move(20, 'nope')
        assert 20 in self._ids(real_manager, 'src')

    def test_unknown_tab_is_command_error(self, real_manager, messages):
        with pytest.raises(cmdutils.CommandError, match='No tab with id 999'):
            commands.tabtree_move(999, 'dst')
        with pytest.raises(cmdutils.CommandError, match='No tab with id 999'):
            commands.tabtree_move_branch(999, 'dst')

    def test_move_into_own_workspace_is_command_error(
            self, real_manager, messages):
        with pytest.raises(cmdutils.CommandError, match='already in'):
            commands.tabtree_move(20, 'src')
        assert 20 in self._ids(real_manager, 'src')

    def test_archived_target_is_command_error(self, real_manager, messages):
        real_manager.archive_workspace(
            real_manager.get_workspace_by_name('dst').id)
        with pytest.raises(cmdutils.CommandError, match='archived'):
            commands.tabtree_move(20, 'dst')
        assert 20 in self._ids(real_manager, 'src')

    def test_no_active_workspace_is_command_error(
            self, real_manager, messages, monkeypatch):
        monkeypatch.setattr(real_manager, 'get_active', lambda: None)
        with pytest.raises(cmdutils.CommandError, match='No active workspace'):
            commands.tabtree_move(20, 'dst')

    def test_name_lookup_is_case_insensitive(self, real_manager, messages):
        commands.tabtree_move(20, 'DST')
        assert self._ids(real_manager, 'dst') == {20: None}
