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
        self.reading_status_sets: list[tuple[list[int], str]] = []
        self.cleanups: list[tuple[uuid.UUID, int]] = []
        self.cleanup_result = 0

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

    # --- reading status / cleanup ---

    def find_tab_by_id(self, workspace_id: uuid.UUID,
                       tab_id: int) -> Optional[WorkspaceTab]:
        for tab in self._workspaces[workspace_id].tabs:
            if tab.tab_id == tab_id:
                return tab
        return None

    def set_reading_status(self, workspace_id: uuid.UUID,
                           tab_ids: list[int], status: str) -> int:
        wanted = list(tab_ids)
        count = 0
        for tab in self._workspaces[workspace_id].tabs:
            if tab.tab_id in wanted:
                tab.reading_status = status
                count += 1
        self.reading_status_sets.append((wanted, status))
        return count

    def cleanup_old_done(self, workspace_id: uuid.UUID, days: int) -> int:
        self.cleanups.append((workspace_id, days))
        return self.cleanup_result


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

    def test_requires_active_workspace(
            self,
            fake_manager: FakeManager,
            auto_cleanup_days: list[int],
    ) -> None:
        auto_cleanup_days[0] = 30
        with pytest.raises(cmdutils.CommandError, match='No active workspace'):
            commands.vp_cleanup_done()


@pytest.fixture
def pers(tmp_path: pathlib.Path) -> tuple[WorkspacePersistence,
                                         PinnedPersistence]:
    """Shared persistence rooted in a temporary directory."""
    return (
        WorkspacePersistence(base_path=tmp_path / 'workspaces'),
        PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml'),
    )


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
