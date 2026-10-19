# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.manager."""

import uuid

import pytest

from qutebrowser.workspaces.manager import TabMetadata, WorkspaceManager
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

    def test_add_workspace_stamps_last_active(self, manager):
        workspace = manager.add_workspace('first')
        assert workspace.last_active is not None

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


class TestGroupLifecycle:

    def test_pin_and_unpin_flip_flag_and_emit_changed(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.pin_group(workspace.id, group.group_id)
        assert group.pinned is True
        assert changes

        changes.clear()
        manager.unpin_group(workspace.id, group.group_id)
        assert group.pinned is False
        assert changes

    def test_pinning_does_not_reorder_groups(self, manager):
        workspace = manager.add_workspace('w')
        first = manager.create_group(workspace.id, 'first')
        second = manager.create_group(workspace.id, 'second')

        manager.pin_group(workspace.id, second.group_id)

        assert manager.list_groups(workspace.id) == [first, second]

    def test_archive_and_restore(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.archive_group(workspace.id, group.group_id)
        assert group.archived is True
        assert manager.list_archived_groups(workspace.id) == [group]
        assert changes

        changes.clear()
        manager.restore_group(workspace.id, group.group_id)
        assert group.archived is False
        assert manager.list_archived_groups(workspace.id) == []
        assert changes

    def test_move_group_reorders(self, manager):
        workspace = manager.add_workspace('w')
        a = manager.create_group(workspace.id, 'a')
        b = manager.create_group(workspace.id, 'b')
        c = manager.create_group(workspace.id, 'c')

        manager.move_group(workspace.id, c.group_id, 0)
        assert manager.list_groups(workspace.id) == [c, a, b]

        manager.move_group(workspace.id, a.group_id, 2)
        assert manager.list_groups(workspace.id) == [c, b, a]

    def test_move_group_out_of_range_raises(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')

        with pytest.raises((ValueError, IndexError)):
            manager.move_group(workspace.id, group.group_id, 5)
        with pytest.raises((ValueError, IndexError)):
            manager.move_group(workspace.id, group.group_id, -1)

    def test_move_group_unknown_id_raises(self, manager):
        workspace = manager.add_workspace('w')
        manager.create_group(workspace.id, 'g')
        with pytest.raises(KeyError):
            manager.move_group(workspace.id, uuid.uuid4(), 0)

    def test_purge_group_removes_group_and_members(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        other = manager.create_group(workspace.id, 'other')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, group_id=group.group_id))
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=2, group_id=group.group_id))
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=3, group_id=other.group_id))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=4))

        changes = []
        manager.changed.connect(lambda: changes.append(True))
        removed = manager.purge_group(workspace.id, group.group_id)

        assert removed == 2
        assert manager.list_groups(workspace.id) == [other]
        assert [tab.tab_id for tab in workspace.tabs] == [3, 4]
        assert changes

    def test_delete_group_nulls_members_still_works(self, manager):
        """The legacy delete_group semantics are unchanged by purge_group."""
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, group_id=group.group_id))

        manager.delete_group(workspace.id, group.group_id)

        assert manager.list_groups(workspace.id) == []
        assert workspace.tabs[0].group_id is None


class TestInbox:
    """VP-F5: the on-demand Inbox group used as the default collect target."""

    def test_get_or_create_inbox_creates_named_group(self, manager):
        workspace = manager.add_workspace('w')

        inbox = manager.get_or_create_inbox(workspace.id)

        assert inbox.name == 'Inbox'
        assert manager.list_groups(workspace.id) == [inbox]

    def test_get_or_create_inbox_reuses_existing(self, manager):
        workspace = manager.add_workspace('w')

        first = manager.get_or_create_inbox(workspace.id)
        second = manager.get_or_create_inbox(workspace.id)

        assert second is first
        assert manager.list_groups(workspace.id) == [first]

    def test_renamed_inbox_gets_recreated(self, manager):
        workspace = manager.add_workspace('w')
        inbox = manager.get_or_create_inbox(workspace.id)
        manager.rename_group(workspace.id, inbox.group_id, 'Other')

        fresh = manager.get_or_create_inbox(workspace.id)

        assert fresh is not inbox
        assert fresh.name == 'Inbox'
        assert len(manager.list_groups(workspace.id)) == 2

    def test_lookup_is_case_insensitive(self, manager):
        workspace = manager.add_workspace('w')
        existing = manager.create_group(workspace.id, 'inbox')

        assert manager.get_or_create_inbox(workspace.id) is existing
        assert manager.list_groups(workspace.id) == [existing]

    def test_delete_then_recreate(self, manager):
        workspace = manager.add_workspace('w')
        inbox = manager.get_or_create_inbox(workspace.id)
        manager.delete_group(workspace.id, inbox.group_id)

        fresh = manager.get_or_create_inbox(workspace.id)

        assert fresh is not inbox
        assert manager.list_groups(workspace.id) == [fresh]


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


class TestCollect:

    def test_collect_fresh_url_captures_metadata(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'github')

        tab = manager.collect_tab(
            workspace.id, 1, group.group_id,
            metadata=TabMetadata(
                url='https://github.com/qutebrowser',
                title='qutebrowser',
                description='A keyboard-driven browser',
            ))

        assert [t.tab_id for t in workspace.tabs] == [1]
        assert tab.title == 'qutebrowser'
        assert tab.url == 'https://github.com/qutebrowser'
        assert tab.source == 'github.com'
        assert tab.description == 'A keyboard-driven browser'
        assert tab.group_id == group.group_id
        assert tab.fetch_attempts == 1
        assert tab.last_fetched is not None
        assert tab.last_error is None

    def test_collect_same_url_reuses_entry_and_backfills(self, manager):
        workspace = manager.add_workspace('w')

        first = manager.collect_tab(
            workspace.id, 1,
            metadata=TabMetadata(url='https://example.com/page'))
        assert first.title == ''

        second = manager.collect_tab(
            workspace.id, 2,
            metadata=TabMetadata(
                url='https://example.com/page', title='Example'))

        assert second is first
        assert [t.tab_id for t in workspace.tabs] == [1]
        assert first.title == 'Example'
        assert first.fetch_attempts == 2

    def test_collect_never_overwrites_user_edited_field(self, manager):
        workspace = manager.add_workspace('w')
        tab = manager.collect_tab(
            workspace.id, 1,
            metadata=TabMetadata(
                url='https://example.com/', title='Auto title'))
        manager.set_user_edited(workspace.id, tab.tab_id, 'title')

        manager.collect_tab(
            workspace.id, 1,
            metadata=TabMetadata(
                url='https://example.com/', title='New auto title'))

        assert tab.title == 'Auto title'

    def test_collect_all_skips_internal_pages(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        metadata = {
            1: TabMetadata(url='https://example.com/'),
            2: TabMetadata(url='qute://version'),
            3: TabMetadata(url='about:blank'),
        }

        collected, skipped = manager.collect_all_in_window(
            workspace.id, group.group_id, [1, 2, 3],
            tab_metadata=metadata)

        assert collected == [1]
        assert skipped == [2, 3]
        assert [t.tab_id for t in workspace.tabs] == [1]
        assert workspace.tabs[0].group_id == group.group_id

    def test_collect_all_all_internal_returns_zero_and_count(self, manager):
        workspace = manager.add_workspace('w')
        metadata = {
            1: TabMetadata(url='qute://version'),
            2: TabMetadata(url='chrome://settings'),
        }

        collected, skipped = manager.collect_all_in_window(
            workspace.id, None, [1, 2], tab_metadata=metadata)

        assert collected == []
        assert skipped == [1, 2]
        assert workspace.tabs == []


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

    def test_active_id_restored_after_add_and_save(self, pers, qapp):
        first = WorkspaceManager(*pers)
        workspace = first.add_workspace('first')
        assert workspace.last_active is not None
        first.save()

        second = WorkspaceManager(*pers)
        second.load()

        restored = second.get_active()
        assert restored is not None
        assert restored.id == workspace.id
        assert [ws.id for ws in second.list_workspaces()] == [workspace.id]

    def test_load_only_loads_active_workspace(self, pers, qapp):
        first = WorkspaceManager(*pers)
        active = first.add_workspace('active')
        inactive = first.add_workspace('inactive')
        first.set_active(active.id)
        first.save()

        second = WorkspaceManager(*pers)
        second.load()

        assert [ws.id for ws in second.list_workspaces()] == [active.id]

        loaded = second.get_workspace(inactive.id)
        assert loaded is not None
        assert loaded.name == 'inactive'
        assert [ws.id for ws in second.list_workspaces()] == [active.id,
                                                              inactive.id]

    def test_get_workspace_missing_is_none(self, manager, qapp):
        assert manager.get_workspace(uuid.uuid4()) is None

    def test_delete_active_clears_state(self, pers, qapp):
        first = WorkspaceManager(*pers)
        workspace = first.add_workspace('w')
        first.delete_workspace(workspace.id)

        second = WorkspaceManager(*pers)
        second.load()
        assert second.get_active() is None
        assert second.list_workspaces() == []
