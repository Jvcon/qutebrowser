# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.manager."""

import uuid

import pytest

from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import PinnedTab, WorkspaceTab
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)


@pytest.fixture
def pers(tmp_path):
    """Shared persistence rooted in a temporary directory."""
    return (
        WorkspacePersistence(base_path=tmp_path / 'workspaces'),
        PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml'),
    )


@pytest.fixture
def manager(pers, qapp):
    """An empty WorkspaceManager backed by temporary persistence."""
    return WorkspaceManager(*pers)


class TestWorkspaceCrud:

    def test_add_workspace(self, manager):
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        workspace = manager.add_workspace('product-rd')

        assert manager.list_workspaces() == [workspace]
        assert changes

    def test_add_workspace_becomes_active_when_none(self, manager):
        workspace = manager.add_workspace('first')
        assert manager.get_active() == workspace

    def test_delete_workspace(self, manager):
        workspace = manager.add_workspace('w')
        manager.delete_workspace(workspace.id)
        assert manager.list_workspaces() == []
        assert manager.get_active() is None

    def test_rename_workspace(self, manager):
        workspace = manager.add_workspace('old')
        manager.rename_workspace(workspace.id, 'new')
        assert manager.list_workspaces()[0].name == 'new'

    def test_set_active_updates_get_active(self, manager):
        first = manager.add_workspace('a')
        second = manager.add_workspace('b')
        manager.set_active(second.id)
        assert manager.get_active() == second
        assert manager.get_active() != first

    def test_get_active_none_when_empty(self, manager):
        assert manager.get_active() is None


class TestGroups:

    def test_create_and_list_group(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'github')
        assert manager.list_groups(workspace.id) == [group]

    def test_rename_group(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'old')
        manager.rename_group(workspace.id, group.group_id, 'new')
        assert manager.list_groups(workspace.id)[0].name == 'new'

    def test_delete_empty_group_removes_it(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.delete_group(workspace.id, group.group_id)
        assert manager.list_groups(workspace.id) == []
        assert manager.list_workspaces()[0].groups == []

    def test_delete_group_nulls_members(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, group_id=group.group_id))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=2))

        manager.delete_group(workspace.id, group.group_id)

        assert manager.list_groups(workspace.id) == []
        assert workspace.tabs[0].group_id is None
        assert workspace.tabs[1].group_id is None


class TestTabMembership:

    def test_add_and_remove_tab(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=2))
        manager.remove_tab(workspace.id, 1)
        assert [tab.tab_id for tab in workspace.tabs] == [2]

    def test_remove_tab_detaches_children(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=2, parent_id=1))
        manager.remove_tab(workspace.id, 1)
        assert workspace.tabs[0].parent_id is None

    def test_move_tab_to_group(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        changes = []
        manager.changed.connect(lambda: changes.append(True))
        manager.move_tab_to_group(workspace.id, 1, group.group_id)

        assert workspace.tabs[0].group_id == group.group_id
        assert changes

    def test_move_tab_to_standalone(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, group_id=group.group_id))
        manager.move_tab_to_group(workspace.id, 1, None)
        assert workspace.tabs[0].group_id is None


class TestPinned:

    def test_add_and_remove_pinned(self, manager):
        pinned = PinnedTab(tab_id=1, title='t', url='https://example.com')
        manager.add_pinned(pinned)

        changes = []
        manager.changed.connect(lambda: changes.append(True))
        assert manager.list_pinned() == [pinned]

        manager.remove_pinned(1)
        assert manager.list_pinned() == []
        assert changes

    def test_add_pinned_replaces_same_tab_id(self, manager):
        manager.add_pinned(PinnedTab(tab_id=1, title='old', url='u'))
        manager.add_pinned(PinnedTab(tab_id=1, title='new', url='u'))
        assert [p.title for p in manager.list_pinned()] == ['new']


class TestSaveLoad:

    def test_state_restored_on_new_manager(self, pers, qapp):
        first = WorkspaceManager(*pers)
        workspace = first.add_workspace('w')
        group = first.create_group(workspace.id, 'g')
        first.add_tab(workspace.id,
                      WorkspaceTab(tab_id=1, group_id=group.group_id))
        first.add_pinned(PinnedTab(tab_id=1, title='t', url='u'))
        first.set_active(workspace.id)
        first.save()

        second = WorkspaceManager(*pers)
        second.load()

        assert [ws.id for ws in second.list_workspaces()] == [workspace.id]
        active = second.get_active()
        assert active is not None
        assert active.id == workspace.id
        assert second.list_groups(workspace.id)[0].name == 'g'
        assert second.list_pinned()[0].title == 't'

    def test_load_empty_is_clean(self, pers, qapp):
        manager = WorkspaceManager(*pers)
        manager.load()
        assert manager.list_workspaces() == []
        assert manager.list_pinned() == []
        assert manager.get_active() is None
