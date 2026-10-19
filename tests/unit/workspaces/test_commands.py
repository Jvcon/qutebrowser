# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.commands."""

import pathlib
import uuid
from collections.abc import Callable
from typing import Optional

import pytest

from qutebrowser.api import cmdutils
from qutebrowser.workspaces import commands
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import Group, Workspace, WorkspaceTab
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
        self.purged: list[uuid.UUID] = []
        self.moved: list[tuple[set[int], Optional[uuid.UUID]]] = []
        self.statuses: list[tuple[set[int], str]] = []
        self.deleted_tabs: list[int] = []
        self.opened_windows: list[tuple[uuid.UUID, set[int]]] = []
        self.opened_tabs: list[tuple[uuid.UUID, set[int]]] = []

    def add_workspace(self, name: str) -> Workspace:
        workspace = Workspace(id=uuid.uuid4(), name=name,
                              last_active='2026-01-01T00:00:00+00:00')
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

    def purge_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> int:
        workspace = self._workspaces[workspace_id]
        removed = [tab for tab in workspace.tabs
                   if tab.group_id == group_id]
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.group_id != group_id]
        workspace.groups = [group for group in workspace.groups
                            if group.group_id != group_id]
        self.purged.append(group_id)
        return len(removed)

    # --- batched operations (VP-F4) ---

    def add_tab(self, workspace_id: uuid.UUID, tab: WorkspaceTab) -> None:
        self._workspaces[workspace_id].tabs.append(tab)

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

    def set_reading_status(self, workspace_id: uuid.UUID, tab_ids: set[int],
                           status: str) -> int:
        if status not in ('FRESH', 'ACTIVE', 'DONE'):
            raise ValueError(status)
        id_set = set(tab_ids)
        count = 0
        for tab in self._workspaces[workspace_id].tabs:
            if tab.tab_id in id_set:
                setattr(tab, 'reading_status', status)
                count += 1
        self.statuses.append((id_set, status))
        return count

    def delete_tabs(self, workspace_id: uuid.UUID,
                    tab_ids: set[int]) -> int:
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

    def test_delete_confirms_then_purges(
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

        assert fake_manager.purged == [group.group_id]
        assert workspace.groups == []
        assert [tab.tab_id for tab in workspace.tabs] == [2]
        assert messages == ["Deleted group 'g' (1 tabs)."]

    def test_delete_missing_raises(self, fake_manager: FakeManager) -> None:
        fake_manager.add_workspace('w')
        with pytest.raises(cmdutils.CommandError, match='No group'):
            commands.vp_group_delete('nope')


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

    def test_vp_batch_delete_empty_selection_noop(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            confirmations: list[Callable[[], None]],
            messages: list[str]) -> None:
        commands.vp_batch_delete(win_id=0)
        assert confirmations == []
        assert messages == ["No tabs selected."]

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

    def test_vp_batch_open_empty_selection_noop(
            self, fake_manager: FakeManager, fake_tree: FakeTree,
            messages: list[str]) -> None:
        commands.vp_batch_open_window(win_id=0)
        assert messages == ["No tabs selected."]


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
