# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.manager."""

import datetime
import uuid
from types import SimpleNamespace

import pytest

from qutebrowser.qt.core import QUrl

from qutebrowser.workspaces import manager as manager_module
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

    def test_archive_and_unarchive_workspace(self, manager):
        workspace = manager.add_workspace('w')

        manager.archive_workspace(workspace.id)
        assert workspace.archived is True

        manager.unarchive_workspace(workspace.id)
        assert workspace.archived is False

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


class TestCreateWindowSnapshot:

    """VP-P4: unexpected window close snapshots into the Inbox."""

    @staticmethod
    def _snap(**overrides):
        data = {
            'url': 'https://example.com/',
            'title': 'Example',
        }
        data.update(overrides)
        return data

    def test_empty_returns_none_without_changed(self, manager):
        workspace = manager.add_workspace('w')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.create_window_snapshot(
            workspace.id, [], min_tabs=1)

        assert result is None
        assert changes == []

    def test_single_tab_with_min_one_creates_subgroup(self, manager):
        workspace = manager.add_workspace('w')

        sub = manager.create_window_snapshot(
            workspace.id, [self._snap()], min_tabs=1)

        assert sub is not None
        assert sub in manager.list_groups(workspace.id)
        assert sub.name.startswith("快照退出 — ")
        inbox = manager.get_or_create_inbox(workspace.id)
        assert sub.snapshot_parent_id == inbox.group_id

    def test_below_min_returns_none(self, manager):
        workspace = manager.add_workspace('w')
        snapshots = [
            self._snap(url=f'https://e{i}.example/') for i in range(3)
        ]

        assert manager.create_window_snapshot(
            workspace.id, snapshots, min_tabs=5) is None

    def test_unknown_workspace_returns_none(self, manager):
        assert manager.create_window_snapshot(
            uuid.uuid4(), [self._snap()], min_tabs=1) is None

    def test_snapshot_tab_fields(self, manager):
        workspace = manager.add_workspace('w')

        sub = manager.create_window_snapshot(workspace.id, [self._snap(
            description='A description',
            source='example.com',
        )], min_tabs=1)

        assert len(workspace.tabs) == 1
        tab = workspace.tabs[0]
        assert tab.url == 'https://example.com/'
        assert tab.title == 'Example'
        assert tab.description == 'A description'
        assert tab.source == 'example.com'
        assert tab.group_id == sub.group_id
        assert tab.open_status == 'SUSPENDED'
        assert tab.reading_status == 'FRESH'
        assert tab.tab_id < 0
        assert tab.created_at is not None

    def test_changed_emitted_once(self, manager):
        workspace = manager.add_workspace('w')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.create_window_snapshot(
            workspace.id, [self._snap()], min_tabs=1)

        assert changes == [True]

    def test_inbox_is_reused_across_snapshots(self, manager):
        workspace = manager.add_workspace('w')
        inbox = manager.get_or_create_inbox(workspace.id)

        manager.create_window_snapshot(
            workspace.id, [self._snap()], min_tabs=1)
        manager.create_window_snapshot(
            workspace.id, [self._snap()], min_tabs=1)

        inboxes = [
            group for group in manager.list_groups(workspace.id)
            if group.name.lower() == 'inbox'
        ]
        assert inboxes == [inbox]
        assert len(manager.list_groups(workspace.id)) == 3


class TestSnapshotSubGroupName:

    def test_contains_label_and_timestamp(self, manager):
        workspace = manager.add_workspace('w')

        sub = manager.create_window_snapshot(
            workspace.id, [{'url': 'https://example.com/'}], min_tabs=1)

        assert "快照退出" in sub.name
        assert datetime.datetime.now().strftime('%Y-%m-%d') in sub.name
        assert sub.name.split("— ")[-1].count(':') == 1


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


class TestOpenStatus:

    def test_set_open_status_updates_single_entry(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, open_status='SUSPENDED'))
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=2, open_status='SUSPENDED'))

        result = manager.set_open_status(workspace.id, 1, 'OPEN')

        assert result == 1
        assert workspace.tabs[0].open_status == 'OPEN'
        assert workspace.tabs[1].open_status == 'SUSPENDED'

    def test_set_open_status_invalid_raises(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        with pytest.raises(ValueError):
            manager.set_open_status(workspace.id, 1, 'CLOSED')

    def test_set_open_status_emits_changed_on_change(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, open_status='SUSPENDED'))
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.set_open_status(workspace.id, 1, 'OPEN')

        assert changes == [True]

    def test_set_open_status_noop_does_not_emit(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, open_status='OPEN'))
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.set_open_status(workspace.id, 1, 'OPEN')

        assert result == 1
        assert changes == []

    def test_set_open_status_unknown_tab_returns_zero(self, manager):
        workspace = manager.add_workspace('w')
        assert manager.set_open_status(workspace.id, 99, 'OPEN') == 0

    def test_open_and_suspended_queries(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, open_status='OPEN'))
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=2, open_status='SUSPENDED'))
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=3, open_status='OPEN'))

        assert [tab.tab_id for tab in manager.open_tabs(workspace.id)] == [
            1, 3]
        assert [tab.tab_id
                for tab in manager.suspended_tabs(workspace.id)] == [2]


class TestGroupState:

    """Group running-state queries (VP-D2)."""

    def _group_with_tabs(self, manager, statuses):
        """Create a group with one tab per entry in *statuses*."""
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        for index, status in enumerate(statuses, start=1):
            manager.add_tab(workspace.id, WorkspaceTab(
                tab_id=index, group_id=group.group_id, open_status=status))
        return workspace, group

    def test_empty_group_is_suspended(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        assert manager.group_state(workspace.id, group.group_id) == 'SUSPENDED'

    def test_all_suspended_is_suspended(self, manager):
        workspace, group = self._group_with_tabs(
            manager, ['SUSPENDED', 'SUSPENDED'])
        assert manager.group_state(workspace.id, group.group_id) == 'SUSPENDED'

    def test_any_open_is_open(self, manager):
        workspace, group = self._group_with_tabs(
            manager, ['SUSPENDED', 'OPEN', 'SUSPENDED'])
        assert manager.group_state(workspace.id, group.group_id) == 'OPEN'

    def test_archived_wins_over_open(self, manager):
        workspace, group = self._group_with_tabs(manager, ['OPEN'])
        manager.archive_group(workspace.id, group.group_id)
        assert manager.group_state(workspace.id, group.group_id) == 'ARCHIVED'

    def test_open_and_total_count(self, manager):
        workspace, group = self._group_with_tabs(
            manager, ['OPEN', 'SUSPENDED', 'OPEN'])
        # Standalone and other-group tabs do not count.
        manager.add_tab(workspace.id, WorkspaceTab(
            tab_id=90, open_status='OPEN'))
        other = manager.create_group(workspace.id, 'other')
        manager.add_tab(workspace.id, WorkspaceTab(
            tab_id=91, group_id=other.group_id, open_status='OPEN'))

        assert manager.group_open_count(workspace.id, group.group_id) == 2
        assert manager.group_total_count(workspace.id, group.group_id) == 3
        assert manager.group_open_count(workspace.id, other.group_id) == 1
        assert manager.group_total_count(workspace.id, other.group_id) == 1

    def test_empty_group_counts_are_zero(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        assert manager.group_open_count(workspace.id, group.group_id) == 0
        assert manager.group_total_count(workspace.id, group.group_id) == 0

    def test_unknown_group_raises_key_error(self, manager):
        workspace = manager.add_workspace('w')
        with pytest.raises(KeyError):
            manager.group_state(workspace.id, uuid.uuid4())

    def test_queries_do_not_emit_changed(self, manager):
        workspace, group = self._group_with_tabs(manager, ['OPEN'])
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.group_open_count(workspace.id, group.group_id)
        manager.group_total_count(workspace.id, group.group_id)
        manager.group_state(workspace.id, group.group_id)

        assert changes == []


class TestEstimateGroupMemoryBytes:

    """Memory estimate for a group's OPEN tabs (VP-P1)."""

    def _group_with_statuses(self, manager, statuses):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        for index, status in enumerate(statuses, start=1):
            manager.add_tab(workspace.id, WorkspaceTab(
                tab_id=index, group_id=group.group_id, open_status=status))
        return workspace, group

    def test_empty_group_estimates_zero(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        assert manager.estimate_group_memory_bytes(
            workspace.id, group.group_id) == 0

    def test_open_tabs_use_constant_per_tab(self, manager):
        workspace, group = self._group_with_statuses(
            manager, ['OPEN', 'SUSPENDED', 'OPEN', 'OPEN'])
        assert manager.estimate_group_memory_bytes(
            workspace.id, group.group_id) == 3 * manager_module.BYTES_PER_TAB

    def test_query_does_not_emit_changed(self, manager):
        workspace, group = self._group_with_statuses(manager, ['OPEN'])
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.estimate_group_memory_bytes(workspace.id, group.group_id)

        assert changes == []


class TestFindTabByUrl:

    """Pure URL lookup used by the VP-D5 auto-locate (no create, no emit)."""

    def _workspace_with_tabs(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(
            tab_id=1, url='https://a.example/'))
        manager.add_tab(workspace.id, WorkspaceTab(
            tab_id=2, url='https://b.example/'))
        return workspace

    def test_hit_returns_matching_entry(self, manager):
        workspace = self._workspace_with_tabs(manager)
        found = manager.find_tab_by_url(workspace.id, 'https://b.example/')
        assert found is not None
        assert found.tab_id == 2

    def test_miss_returns_none(self, manager):
        workspace = self._workspace_with_tabs(manager)
        assert manager.find_tab_by_url(
            workspace.id, 'https://missing.example/') is None

    def test_empty_url_returns_none(self, manager):
        workspace = self._workspace_with_tabs(manager)
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=3, url=''))
        assert manager.find_tab_by_url(workspace.id, '') is None

    def test_unknown_workspace_returns_none(self, manager):
        self._workspace_with_tabs(manager)
        assert manager.find_tab_by_url(uuid.uuid4(), 'https://a.example/') is None

    def test_lookup_does_not_create_or_emit(self, manager):
        workspace = self._workspace_with_tabs(manager)
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        assert manager.find_tab_by_url(
            workspace.id, 'https://missing.example/') is None
        assert len(workspace.tabs) == 2
        assert changes == []


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


def _old_timestamp(days: int = 3650) -> str:
    """Return an ISO 8601 UTC timestamp *days* days in the past."""
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)).isoformat()


def _recent_timestamp(days: int = 1) -> str:
    """Return an ISO 8601 UTC timestamp *days* days in the past."""
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)).isoformat()


class TestReadingStatus:

    def test_set_single(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        count = manager.set_reading_status(workspace.id, [1], 'ACTIVE')

        assert count == 1
        assert workspace.tabs[0].reading_status == 'ACTIVE'

    def test_set_multiple_only_matching(self, manager):
        workspace = manager.add_workspace('w')
        for tab_id in (1, 2, 3):
            manager.add_tab(workspace.id, WorkspaceTab(tab_id=tab_id))

        count = manager.set_reading_status(workspace.id, [1, 3], 'DONE')

        assert count == 2
        assert [tab.reading_status for tab in workspace.tabs] == [
            'DONE', 'FRESH', 'DONE']

    def test_set_unknown_ids_returns_zero_without_emitting(self, manager):
        workspace = manager.add_workspace('w')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        count = manager.set_reading_status(workspace.id, [99], 'DONE')

        assert count == 0
        assert changes == []

    @pytest.mark.parametrize('status', ['FRESH', 'ACTIVE', 'DONE'])
    def test_set_all_valid_statuses(self, manager, status):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        manager.set_reading_status(workspace.id, [1], status)
        assert workspace.tabs[0].reading_status == status

    def test_invalid_status_raises(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        with pytest.raises(ValueError):
            manager.set_reading_status(workspace.id, [1], 'BOGUS')

    def test_emits_changed_exactly_once_for_batch(self, manager):
        workspace = manager.add_workspace('w')
        for tab_id in (1, 2):
            manager.add_tab(workspace.id, WorkspaceTab(tab_id=tab_id))
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.set_reading_status(workspace.id, [1, 2], 'DONE')

        assert len(changes) == 1

    def test_tabs_by_status_returns_only_matches(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, reading_status='DONE'))
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=2, reading_status='ACTIVE'))
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=3, reading_status='DONE'))

        result = manager.tabs_by_status(workspace.id, 'DONE')

        assert [tab.tab_id for tab in result] == [1, 3]

    def test_tabs_by_status_emits_nothing(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id,
                        WorkspaceTab(tab_id=1, reading_status='DONE'))
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.tabs_by_status(workspace.id, 'DONE')

        assert changes == []

    def test_default_status_is_fresh(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        assert workspace.tabs[0].reading_status == 'FRESH'
        assert manager.tabs_by_status(workspace.id, 'FRESH')


def _patch_auto_suspend_config(monkeypatch, *, enabled=True, days=30):
    """Patch the manager module's config accessor for auto-suspend tests."""
    panel = SimpleNamespace(
        auto_suspend_enabled=enabled,
        auto_suspend_done_days=days,
        suggestion_enabled=True,
        suggestion_min_open_tabs=3,
        suggestion_idle_days=7,
    )
    monkeypatch.setattr(
        manager_module, 'config',
        SimpleNamespace(
            val=SimpleNamespace(
                content=SimpleNamespace(vertical_panel=panel))))


class TestDoneAt:

    def test_done_stamps_done_at(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        manager.set_reading_status(workspace.id, [1], 'DONE')

        done_at = workspace.tabs[0].done_at
        assert done_at is not None
        assert done_at.endswith('+00:00')

    def test_done_to_active_clears_done_at(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        manager.set_reading_status(workspace.id, [1], 'DONE')

        manager.set_reading_status(workspace.id, [1], 'ACTIVE')

        assert workspace.tabs[0].done_at is None

    def test_active_to_done_stamps_fresh_done_at(self, manager):
        workspace = manager.add_workspace('w')
        tab = WorkspaceTab(tab_id=1, done_at='2000-01-01T00:00:00+00:00')
        manager.add_tab(workspace.id, tab)
        manager.set_reading_status(workspace.id, [1], 'ACTIVE')

        manager.set_reading_status(workspace.id, [1], 'DONE')

        assert tab.done_at is not None
        assert tab.done_at != '2000-01-01T00:00:00+00:00'

    @pytest.mark.parametrize('status', ['FRESH', 'ACTIVE'])
    def test_non_done_clears_done_at(self, manager, status):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        manager.set_reading_status(workspace.id, [1], status)

        assert workspace.tabs[0].done_at is None


class TestAutoSuspendDueTabs:

    def _add(self, manager, workspace, tab_id, *, open_status='OPEN',
             reading_status='DONE', done_at='2000-01-01T00:00:00+00:00',
             group_id=None):
        tab = WorkspaceTab(
            tab_id=tab_id,
            open_status=open_status,
            reading_status=reading_status,
            done_at=done_at,
        )
        if group_id is not None:
            tab.assign_group(group_id)
        manager.add_tab(workspace.id, tab)
        return tab

    def test_suspends_all_open_done_stale(self, manager):
        workspace = manager.add_workspace('w')
        for tab_id in (1, 2, 3):
            self._add(manager, workspace, tab_id)

        suspended = manager.auto_suspend_due_tabs(workspace.id, 30)

        assert suspended == 3
        assert [tab.open_status for tab in workspace.tabs] == [
            'SUSPENDED', 'SUSPENDED', 'SUSPENDED']

    def test_emits_changed_once_for_batch(self, manager):
        workspace = manager.add_workspace('w')
        for tab_id in (1, 2, 3):
            self._add(manager, workspace, tab_id)
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.auto_suspend_due_tabs(workspace.id, 30)

        assert len(changes) == 1

    def test_skips_fresh_and_suspended(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, reading_status='FRESH')
        self._add(manager, workspace, 2, open_status='SUSPENDED')

        assert manager.auto_suspend_due_tabs(workspace.id, 30) == 0
        assert [tab.open_status for tab in workspace.tabs] == [
            'OPEN', 'SUSPENDED']

    def test_skips_missing_done_at(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, done_at=None)

        assert manager.auto_suspend_due_tabs(workspace.id, 30) == 0
        assert workspace.tabs[0].open_status == 'OPEN'

    def test_skips_archived_group_members(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'archived')
        manager.archive_group(workspace.id, group.group_id)
        self._add(manager, workspace, 1, group_id=group.group_id)

        assert manager.auto_suspend_due_tabs(workspace.id, 30) == 0
        assert workspace.tabs[0].open_status == 'OPEN'

    def test_threshold_boundary(self, manager, monkeypatch):
        fixed_now = datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0)
        frozen = SimpleNamespace(
            datetime=type(
                'FrozenDatetime', (datetime.datetime,),
                {'now': classmethod(lambda cls, tz=None: fixed_now)}),
            timedelta=datetime.timedelta,
            timezone=datetime.timezone,
        )
        monkeypatch.setattr(manager_module, 'datetime', frozen)
        workspace = manager.add_workspace('w')
        exactly_30 = (fixed_now - datetime.timedelta(days=30)).isoformat()
        older_31 = (fixed_now - datetime.timedelta(days=31)).isoformat()
        self._add(manager, workspace, 1, done_at=exactly_30)
        self._add(manager, workspace, 2, done_at=older_31)

        suspended = manager.auto_suspend_due_tabs(workspace.id, 30)

        assert suspended == 1
        assert workspace.tabs[0].open_status == 'OPEN'
        assert workspace.tabs[1].open_status == 'SUSPENDED'

    def test_uses_configured_days_when_omitted(self, manager, monkeypatch):
        _patch_auto_suspend_config(monkeypatch, days=30)
        workspace = manager.add_workspace('w')
        self._add(
            manager, workspace, 1,
            done_at=(datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=40)).isoformat())

        assert manager.auto_suspend_due_tabs(workspace.id) == 1

    def test_scan_skips_when_disabled(self, manager, monkeypatch):
        _patch_auto_suspend_config(monkeypatch, enabled=False)
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1)
        scans = []
        monkeypatch.setattr(
            manager, 'auto_suspend_due_tabs',
            lambda workspace_id: scans.append(workspace_id) or 0)

        assert manager._scan_all_workspaces_for_stale() == 0
        assert scans == []

    def test_scan_suspends_across_workspaces(self, manager, monkeypatch):
        _patch_auto_suspend_config(monkeypatch, enabled=True)
        first = manager.add_workspace('a')
        second = manager.add_workspace('b')
        self._add(manager, first, 1)
        self._add(manager, second, 2)

        assert manager._scan_all_workspaces_for_stale() == 2

    def test_start_scheduler_arms_repeating_timer(self, manager, monkeypatch):
        scans = []
        monkeypatch.setattr(
            manager, '_scan_all_workspaces_for_stale',
            lambda: scans.append(True) or 0)
        seen = {}

        class FakeSignal:

            def connect(self, callback):
                seen['timeout'] = callback

        class FakeTimer:

            def __init__(self, parent=None):
                self.parent = parent
                self.interval = None
                self.started = False
                self.timeout = FakeSignal()

            def setInterval(self, interval):
                self.interval = interval

            def start(self):
                self.started = True

        class FakeQTimer:

            def __new__(cls, parent=None):
                return FakeTimer(parent)

            @staticmethod
            def singleShot(msec, callback):
                seen['single_shot'] = (msec, callback)

        monkeypatch.setattr(manager_module, 'QTimer', FakeQTimer)

        manager.start_auto_suspend_scheduler(parent=object())

        msec, deferred = seen['single_shot']
        assert msec == 0
        deferred()
        assert scans == [True]
        timer = manager._auto_suspend_timer
        assert timer.interval == 12 * 3600 * 1000
        assert timer.started
        timer.timeout.connect  # noqa: B018 - connected in the scheduler
        assert callable(seen['timeout'])


def _freeze_now(monkeypatch, fixed_now):
    """Freeze ``manager_module.datetime`` at *fixed_now*."""
    frozen = SimpleNamespace(
        datetime=type(
            'FrozenDatetime', (datetime.datetime,),
            {'now': classmethod(lambda cls, tz=None: fixed_now)}),
        timedelta=datetime.timedelta,
        timezone=datetime.timezone,
    )
    monkeypatch.setattr(manager_module, 'datetime', frozen)


class TestSuggestStaleGroups:

    def _add_group(
            self,
            manager,
            workspace,
            name,
            *,
            tab_count=3,
            days_ago=10,
            open_status='OPEN',
            done_at=True,
            reading_status='DONE',
    ):
        group = manager.create_group(workspace.id, name)
        stamp = None
        if done_at:
            stamp = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=days_ago)).isoformat()
        next_id = 1 + max(
            (tab.tab_id for tab in workspace.tabs), default=0)
        for offset in range(tab_count):
            tab_id = next_id + offset
            tab = WorkspaceTab(
                tab_id=tab_id,
                open_status=open_status,
                reading_status=reading_status,
                done_at=stamp,
            )
            tab.assign_group(group.group_id)
            manager.add_tab(workspace.id, tab)
        return group

    def test_returns_only_stale_open_groups(self, manager):
        workspace = manager.add_workspace('w')
        stale = self._add_group(manager, workspace, 'stale')
        self._add_group(manager, workspace, 'fresh', days_ago=1)
        self._add_group(
            manager, workspace, 'suspended', open_status='SUSPENDED')
        archived = self._add_group(manager, workspace, 'archived')
        manager.archive_group(workspace.id, archived.group_id)

        result = manager.suggest_stale_groups(workspace.id)

        assert result == [stale]

    def test_min_open_tabs_threshold(self, manager):
        workspace = manager.add_workspace('w')
        self._add_group(manager, workspace, 'stale', tab_count=3)

        assert manager.suggest_stale_groups(
            workspace.id, min_open_tabs=5) == []

    def test_idle_days_override(self, manager):
        workspace = manager.add_workspace('w')
        self._add_group(manager, workspace, 'ten-days', days_ago=10)

        assert manager.suggest_stale_groups(
            workspace.id, idle_days=7)
        assert manager.suggest_stale_groups(
            workspace.id, idle_days=30) == []

    def test_uses_configured_defaults(self, manager, monkeypatch):
        _patch_auto_suspend_config(monkeypatch)
        workspace = manager.add_workspace('w')
        self._add_group(manager, workspace, 'ten-days', days_ago=10)

        assert len(manager.suggest_stale_groups(workspace.id)) == 1

    def test_falls_back_to_created_at(self, manager):
        workspace = manager.add_workspace('w')
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=10)).isoformat()
        group = manager.create_group(workspace.id, 'old-created')
        for tab_id in (1, 2, 3):
            tab = WorkspaceTab(
                tab_id=tab_id, open_status='OPEN',
                reading_status='ACTIVE', created_at=old)
            tab.assign_group(group.group_id)
            manager.add_tab(workspace.id, tab)

        assert manager.suggest_stale_groups(workspace.id) == [group]

    def test_pure_query_emits_nothing(self, manager):
        workspace = manager.add_workspace('w')
        self._add_group(manager, workspace, 'stale')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.suggest_stale_groups(workspace.id)

        assert changes == []

    def test_scan_emits_suggestions(self, manager, monkeypatch):
        _patch_auto_suspend_config(monkeypatch)
        workspace = manager.add_workspace('w')
        stale = self._add_group(manager, workspace, 'stale')
        received = []
        manager.suggestions_changed.connect(
            lambda groups: received.append(list(groups)))

        manager._scan_all_workspaces_for_stale()

        assert received == [[stale]]


class TestSuggestionIgnoreList:

    def test_get_is_empty_initially(self, manager):
        workspace = manager.add_workspace('w')
        assert manager.get_ignored_suggestion_groups(workspace.id) == set()

    def test_ignore_adds_group(self, manager):
        workspace = manager.add_workspace('w')
        group_id = str(uuid.uuid4())

        added = manager.ignore_suggestion_groups(workspace.id, [group_id])

        assert added == 1
        assert manager.get_ignored_suggestion_groups(workspace.id) == {
            group_id}
        assert group_id in workspace.suggestion_ignored

    def test_ignore_is_idempotent(self, manager):
        workspace = manager.add_workspace('w')
        group_id = str(uuid.uuid4())
        manager.ignore_suggestion_groups(workspace.id, [group_id])

        assert manager.ignore_suggestion_groups(
            workspace.id, [group_id]) == 0

    def test_cooldown_expires_after_seven_days(self, manager, monkeypatch):
        fixed_now = datetime.datetime.now(datetime.timezone.utc)
        _freeze_now(monkeypatch, fixed_now)
        workspace = manager.add_workspace('w')
        group_id = str(uuid.uuid4())
        manager.ignore_suggestion_groups(workspace.id, [group_id])

        _freeze_now(monkeypatch, fixed_now + datetime.timedelta(days=8))

        assert manager.get_ignored_suggestion_groups(workspace.id) == set()
        assert manager.ignore_suggestion_groups(
            workspace.id, [group_id]) == 1

    def test_persists_through_workspace_save(self, manager, pers):
        workspace = manager.add_workspace('w')
        group_id = str(uuid.uuid4())
        manager.ignore_suggestion_groups(workspace.id, [group_id])
        manager.save()

        reloaded = WorkspaceManager(*pers)
        reloaded.load()

        assert reloaded.get_ignored_suggestion_groups(workspace.id) == {
            group_id}


class TestCleanupOldDone:

    def _add(self, manager, workspace, tab_id, *, status='DONE',
             open_status='SUSPENDED', last_fetched=None):
        tab = WorkspaceTab(tab_id=tab_id, reading_status=status,
                           last_fetched=last_fetched)
        if open_status is not None:
            # ``open_status`` is added by VP-D1; until then it lives as a
            # plain instance attribute.
            tab.open_status = open_status  # type: ignore[attr-defined]
        manager.add_tab(workspace.id, tab)
        return tab

    def test_removes_old_done_suspended(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, last_fetched=_old_timestamp())
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 1
        assert workspace.tabs == []
        assert changes

    def test_keeps_open_tab(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, open_status='OPEN',
                  last_fetched=_old_timestamp())

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 0
        assert [tab.tab_id for tab in workspace.tabs] == [1]

    def test_keeps_default_suspended_fallback_missing_field(self, manager):
        """Without an ``open_status`` attribute the tab counts as SUSPENDED."""
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, open_status=None,
                  last_fetched=_old_timestamp())

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 1

    @pytest.mark.parametrize('status', ['FRESH', 'ACTIVE'])
    def test_keeps_not_done(self, manager, status):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, status=status,
                  last_fetched=_old_timestamp())

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 0
        assert [tab.tab_id for tab in workspace.tabs] == [1]

    def test_keeps_recent_done(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, last_fetched=_recent_timestamp())

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 0
        assert [tab.tab_id for tab in workspace.tabs] == [1]

    def test_uses_workspace_last_active_when_no_last_fetched(self, manager):
        workspace = manager.add_workspace('w')
        workspace.last_active = _old_timestamp()
        self._add(manager, workspace, 1, last_fetched=None)

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 1

    def test_falls_back_to_created_at_when_no_dates(self, manager):
        workspace = manager.add_workspace('w')
        workspace.last_active = None
        tab = self._add(manager, workspace, 1, last_fetched=None)
        tab.created_at = _old_timestamp()

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 1
        assert workspace.tabs == []

    def test_keeps_tab_with_no_dates_at_all(self, manager):
        workspace = manager.add_workspace('w')
        workspace.last_active = None
        tab = self._add(manager, workspace, 1, last_fetched=None)
        tab.created_at = None

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 0
        assert [t.tab_id for t in workspace.tabs] == [1]

    def test_detaches_children_of_removed_parent(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, last_fetched=_old_timestamp())
        child = WorkspaceTab(tab_id=2, parent_id=1, reading_status='ACTIVE')
        manager.add_tab(workspace.id, child)

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 1
        assert [t.tab_id for t in workspace.tabs] == [2]
        assert child.parent_id is None

    def test_removes_only_matching_tabs(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, last_fetched=_old_timestamp())
        self._add(manager, workspace, 2, status='ACTIVE',
                  last_fetched=_old_timestamp())
        self._add(manager, workspace, 3, open_status='OPEN',
                  last_fetched=_old_timestamp())
        self._add(manager, workspace, 4, last_fetched=_recent_timestamp())

        removed = manager.cleanup_old_done(workspace.id, 30)

        assert removed == 1
        assert [tab.tab_id for tab in workspace.tabs] == [2, 3, 4]

    def test_no_emit_when_nothing_removed(self, manager):
        workspace = manager.add_workspace('w')
        self._add(manager, workspace, 1, last_fetched=_recent_timestamp())
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.cleanup_old_done(workspace.id, 30)

        assert changes == []


class TestOpenStatusStub:

    def test_sets_status_and_emits(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.set_open_status(workspace.id, 1, 'OPEN')

        assert getattr(workspace.tabs[0], 'open_status', None) == 'OPEN'
        assert result == 1
        assert changes

    def test_invalid_status_raises(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        with pytest.raises(ValueError):
            manager.set_open_status(workspace.id, 1, 'BOGUS')

    def test_unknown_tab_returns_zero(self, manager):
        workspace = manager.add_workspace('w')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.set_open_status(workspace.id, 99, 'OPEN')

        assert result == 0
        assert changes == []


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


class _FakeLiveTab:

    """Minimal stand-in for a live tab exposing ``url()``."""

    def __init__(self, url: str) -> None:
        self._url = QUrl(url)

    def url(self) -> QUrl:
        return self._url


class _FakeWidget:

    """Stand-in for the browser's QTabWidget."""

    def __init__(self) -> None:
        self.set_calls: list[object] = []

    def setCurrentWidget(self, widget: object) -> None:  # noqa: N802
        self.set_calls.append(widget)


class _FakeBrowser:

    """Stand-in for TabbedBrowser recording ``tabopen`` calls."""

    def __init__(self) -> None:
        self.widget = _FakeWidget()
        # ``(url, background)`` in call order, for quick assertions.
        self.opened: list[tuple[str, object]] = []
        # Full ``tabopen`` kwargs per call, so tests can assert background.
        self.tabopen_calls: list[dict[str, object]] = []
        # VP-F4's new-window flow constructs a fresh ``MainWindow`` and
        # reads the source browser's ``is_private`` flag to mirror it.
        self.is_private = False

    def tabopen(self, url: QUrl, background: object = None, **kwargs) -> None:
        self.opened.append((url.toString(), background))
        self.tabopen_calls.append(
            {'url': url.toString(), 'background': background, **kwargs})


class TestBatchOperations:

    """Tests for the VP-F4 batched manager APIs."""

    def test_move_tabs_into_group(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=2))

        changes = []
        manager.changed.connect(lambda: changes.append(True))
        moved = manager.move_tabs(workspace.id, [1, 2], group.group_id)

        assert moved == 2
        assert all(tab.group_id == group.group_id for tab in workspace.tabs)
        assert changes

    def test_move_tabs_to_standalone(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.add_tab(
            workspace.id, WorkspaceTab(tab_id=1, group_id=group.group_id))

        moved = manager.move_tabs(workspace.id, [1], None)

        assert moved == 1
        assert workspace.tabs[0].group_id is None

    def test_move_tabs_skips_unknown(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        assert manager.move_tabs(workspace.id, [1, 99], None) == 1

    def test_move_tabs_noop_emits_nothing(self, manager):
        workspace = manager.add_workspace('w')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        assert manager.move_tabs(workspace.id, [99], None) == 0
        assert changes == []

    def test_set_reading_status_valid(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=2))

        changes = []
        manager.changed.connect(lambda: changes.append(True))
        count = manager.set_reading_status(workspace.id, [1, 2], 'done')

        assert count == 2
        assert getattr(workspace.tabs[0], 'reading_status') == 'DONE'
        assert getattr(workspace.tabs[1], 'reading_status') == 'DONE'
        assert changes

    def test_set_reading_status_invalid_raises(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        with pytest.raises(ValueError):
            manager.set_reading_status(workspace.id, [1], 'BOGUS')

    def test_set_reading_status_skips_unknown(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        assert manager.set_reading_status(workspace.id, [1, 99], 'FRESH') == 1

    def test_delete_tabs_removes_and_detaches(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=2, parent_id=1))
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=3))

        changes = []
        manager.changed.connect(lambda: changes.append(True))
        removed = manager.delete_tabs(workspace.id, [1, 2], confirm=False)

        assert removed == 2
        assert [tab.tab_id for tab in workspace.tabs] == [3]
        assert changes

    def test_delete_tabs_default_confirm_is_noop(self, manager):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1))

        assert manager.delete_tabs(workspace.id, [1]) == 0
        assert [tab.tab_id for tab in workspace.tabs] == [1]

    def test_delete_tabs_unknown_is_idempotent(self, manager):
        workspace = manager.add_workspace('w')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        assert manager.delete_tabs(workspace.id, [99], confirm=False) == 0
        assert changes == []

    def test_open_tabs_in_new_window_reuses_and_opens(
            self, manager, monkeypatch):
        workspace = manager.add_workspace('w')
        manager.add_tab(
            workspace.id, WorkspaceTab(tab_id=1, url='https://a.example/'))
        manager.add_tab(
            workspace.id, WorkspaceTab(tab_id=2, url='https://b.example/'))

        live = _FakeLiveTab('https://a.example/')
        browser = _FakeBrowser()
        registry = {5: live}

        def fake_get(name, **_kwargs):
            if name == 'tab-registry':
                return registry
            if name == 'tabbed-browser':
                return browser
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

        # The new-window path constructs a fresh ``MainWindow`` (we cannot
        # build a real one without a fully initialised Qt/config env) and
        # uses *its* tabbed browser to open the URL.
        new_browser = _FakeBrowser()
        new_browser.is_private = False

        class _FakeMainWindow:

            def __init__(self, *, private: bool) -> None:
                self.private = private
                self.tabbed_browser = new_browser
                self.shown = False

            def show(self) -> None:
                self.shown = True

        from qutebrowser.mainwindow import mainwindow as _mw
        monkeypatch.setattr(_mw, 'MainWindow', _FakeMainWindow)

        opened, reused = manager.open_tabs_in_new_window(
            workspace.id, [1, 2])

        assert (opened, reused) == (1, 1)
        assert browser.widget.set_calls == [live]
        assert new_browser.opened == [('https://b.example/', None)]

    def test_open_tabs_as_new_tabs_reuses_and_opens(
            self, manager, monkeypatch):
        workspace = manager.add_workspace('w')
        manager.add_tab(
            workspace.id, WorkspaceTab(tab_id=1, url='https://a.example/'))
        manager.add_tab(
            workspace.id, WorkspaceTab(tab_id=2, url='https://b.example/'))

        browser = _FakeBrowser()
        registry: dict[int, object] = {}

        def fake_get(name, **_kwargs):
            if name == 'tab-registry':
                return registry
            if name == 'tabbed-browser':
                return browser
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

        opened, reused = manager.open_tabs_as_new_tabs(workspace.id, [1, 2])

        assert (opened, reused) == (2, 0)
        assert browser.opened == [
            ('https://a.example/', None),
            ('https://b.example/', None),
        ]

    def test_open_tabs_skips_unknown_and_missing_url(
            self, manager, monkeypatch):
        workspace = manager.add_workspace('w')
        manager.add_tab(workspace.id, WorkspaceTab(tab_id=1, url=''))

        browser = _FakeBrowser()

        def fake_get(name, **_kwargs):
            if name == 'tab-registry':
                return {}
            if name == 'tabbed-browser':
                return browser
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

        assert manager.open_tabs_as_new_tabs(workspace.id, [1, 99]) == (0, 0)
        assert browser.opened == []


class _PinData:

    """Minimal ``data`` stand-in carrying a ``pinned`` flag."""

    def __init__(self, pinned: bool = False) -> None:
        self.pinned = pinned


class _LiveWithPin:

    """Live-tab stand-in exposing ``data.pinned`` for close_group tests."""

    def __init__(self, pinned: bool = False) -> None:
        self.data = _PinData(pinned)


class TestCloseGroup:

    """Tests for ``WorkspaceManager.close_group`` (VP-D3)."""

    class _CloseSignal:

        def __init__(self, fail=()):
            self.calls = []
            self.fail = set(fail)

        def emit(self, idx):
            if idx in self.fail:
                raise RuntimeError('close handler failed')
            self.calls.append(idx)

    class _CloseWidget:

        def __init__(self, live_tabs, fail=()):
            self._live = list(live_tabs)
            self.tabCloseRequested = TestCloseGroup._CloseSignal(fail)

        def indexOf(self, widget):  # noqa: N802 - Qt naming
            try:
                return self._live.index(widget)
            except ValueError:
                return -1

    class _CloseBrowser:

        def __init__(self, live_tabs, fail=()):
            self.widget = TestCloseGroup._CloseWidget(live_tabs, fail)

    def _group(self, manager, statuses):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        for index, status in enumerate(statuses, start=1):
            manager.add_tab(workspace.id, WorkspaceTab(
                tab_id=index, group_id=group.group_id,
                open_status=status))
        return workspace, group

    def _install(self, monkeypatch, browser, registry):
        def fake_get(name, **_kwargs):
            if name == 'tab-registry':
                return registry
            if name == 'tabbed-browser':
                return browser
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

    def test_closes_only_open_members_and_emits_once(
            self, manager, monkeypatch):
        workspace, group = self._group(
            manager, ['OPEN', 'SUSPENDED', 'OPEN'])
        live = [object(), object(), object()]
        registry = {1: live[0], 2: live[1], 3: live[2]}
        browser = self._CloseBrowser(live)
        self._install(monkeypatch, browser, registry)
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.close_group(workspace.id, group.group_id)

        assert result == (2, 0)
        # Only the OPEN tabs (indices 0 and 2) were closed.
        assert browser.widget.tabCloseRequested.calls == [0, 2]
        assert changes == [True]

    def test_all_suspended_is_noop(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, ['SUSPENDED', 'SUSPENDED'])
        live = [object(), object()]
        browser = self._CloseBrowser(live)
        self._install(monkeypatch, browser, {1: live[0], 2: live[1]})
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.close_group(workspace.id, group.group_id)

        assert result == (0, 0)
        assert browser.widget.tabCloseRequested.calls == []
        assert changes == []

    def test_open_without_live_match_is_skipped(self, manager, monkeypatch):
        workspace, group = self._group(manager, ['OPEN', 'OPEN'])
        live = object()
        browser = self._CloseBrowser([live])
        self._install(monkeypatch, browser, {2: live})

        result = manager.close_group(workspace.id, group.group_id)

        assert result == (1, 1)
        assert browser.widget.tabCloseRequested.calls == [0]

    def test_handler_exception_skips_and_continues(
            self, manager, monkeypatch):
        workspace, group = self._group(manager, ['OPEN', 'OPEN'])
        live = [object(), object()]
        browser = self._CloseBrowser(live, fail={0})
        self._install(monkeypatch, browser, {1: live[0], 2: live[1]})

        result = manager.close_group(workspace.id, group.group_id)

        assert result == (1, 1)
        # The first close raised; the second one still ran.
        assert browser.widget.tabCloseRequested.calls == [1]

    def test_empty_group_is_noop(self, manager, monkeypatch):
        workspace, group = self._group(manager, [])
        browser = self._CloseBrowser([])
        self._install(monkeypatch, browser, {})

        result = manager.close_group(workspace.id, group.group_id)

        assert result == (0, 0)
        assert browser.widget.tabCloseRequested.calls == []

    def test_unknown_group_raises(self, manager):
        workspace = manager.add_workspace('w')
        with pytest.raises(KeyError):
            manager.close_group(workspace.id, uuid.uuid4())

    def test_unknown_workspace_is_noop(self, manager):
        assert manager.close_group(uuid.uuid4(), uuid.uuid4()) == (0, 0)

    def test_pinned_tab_is_skipped(self, manager, monkeypatch):
        workspace, group = self._group(manager, ['OPEN'])
        pinned = _LiveWithPin(pinned=True)
        browser = self._CloseBrowser([pinned])
        self._install(monkeypatch, browser, {1: pinned})
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.close_group(workspace.id, group.group_id)

        assert result == (0, 1)
        assert browser.widget.tabCloseRequested.calls == []
        assert changes == []

    def test_archived_group_is_noop(self, manager, monkeypatch):
        workspace, group = self._group(manager, ['OPEN'])
        manager.archive_group(workspace.id, group.group_id)
        live = object()
        browser = self._CloseBrowser([live])
        self._install(monkeypatch, browser, {1: live})
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.close_group(workspace.id, group.group_id)

        assert result == (0, 0)
        assert browser.widget.tabCloseRequested.calls == []
        assert changes == []

    def test_win_id_used_for_objreg_lookups(self, manager, monkeypatch):
        workspace, group = self._group(manager, ['OPEN'])
        live = object()
        browser = self._CloseBrowser([live])
        seen = []

        def fake_get(name, **kwargs):
            seen.append((name, kwargs.get('window')))
            if name == 'tab-registry':
                return {1: live}
            if name == 'tabbed-browser':
                return browser
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

        result = manager.close_group(workspace.id, group.group_id, win_id=1)

        assert result == (1, 0)
        assert seen == [
            ('tab-registry', 1),
            ('tabbed-browser', 1),
        ]
        assert browser.widget.tabCloseRequested.calls == [0]


class TestReopenGroup:

    """Tests for ``WorkspaceManager.reopen_group`` (VP-D4)."""

    class _RaisingBrowser(_FakeBrowser):

        """A browser whose ``tabopen`` raises for the given URLs."""

        def __init__(self, fail_urls=()) -> None:
            super().__init__()
            self._fail = set(fail_urls)

        def tabopen(self, url, background=None, **kwargs) -> None:
            if url.toString() in self._fail:
                raise RuntimeError('tabopen failed')
            super().tabopen(url, background, **kwargs)

    class _RaisingWidget(_FakeWidget):

        """A QTabWidget whose ``setCurrentWidget`` always raises."""

        def setCurrentWidget(self, widget: object) -> None:  # noqa: N802
            raise RuntimeError('setCurrentWidget failed')

    class _RaisingBrowserWidget(_FakeBrowser):

        """A browser whose widget raises on ``setCurrentWidget``."""

        def __init__(self) -> None:
            super().__init__()
            self.widget = TestReopenGroup._RaisingWidget()

    def _group(self, manager, tabs):
        """Create a workspace + group with ``(status, url)`` tab entries."""
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        for index, (status, url) in enumerate(tabs, start=1):
            manager.add_tab(workspace.id, WorkspaceTab(
                tab_id=index, group_id=group.group_id,
                open_status=status, url=url))
        return workspace, group

    def _install(self, monkeypatch, resources):
        """Install a fake objreg exposing ``win_id -> (registry, browser)``."""
        monkeypatch.setattr(
            manager_module.objreg, 'window_registry',
            {win_id: object() for win_id in resources})

        def fake_get(name, **_kwargs):
            win_id = _kwargs.get('window')
            if name == 'tab-registry':
                return resources[win_id][0]
            if name == 'tabbed-browser':
                return resources[win_id][1]
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

    def test_opens_all_suspended_when_no_live_match(
            self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/'),
                      ('SUSPENDED', 'https://b.example/')])
        browser = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser)})
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (2, 0, 0)
        assert [url for url, _ in browser.opened] == [
            'https://a.example/', 'https://b.example/']
        # The reopened miss path must force foreground (background=False),
        # not defer to the tabs.background setting.
        assert browser.tabopen_calls == [
            {'url': 'https://a.example/', 'background': False},
            {'url': 'https://b.example/', 'background': False},
        ]
        # Only the single final batch emit fires when nothing was reused.
        assert changes == [True]

    def test_reuses_live_tab_in_other_window(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/'),
                      ('SUSPENDED', 'https://b.example/')])
        live = _FakeLiveTab('https://a.example/')
        browser0 = _FakeBrowser()
        browser1 = _FakeBrowser()
        self._install(monkeypatch, {
            0: ({}, browser0),
            1: ({7: live}, browser1),
        })

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (1, 1, 0)
        # The hit is focused in the window that owns it...
        assert browser1.widget.set_calls == [live]
        # ...and the miss is opened in the current (win_id 0) window.
        assert [url for url, _ in browser0.opened] == ['https://b.example/']
        # The reused entry was flipped to OPEN.
        assert workspace.tabs[0].open_status == 'OPEN'

    def test_cross_window_reuse_raises_owning_window(
            self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/')])
        live = _FakeLiveTab('https://a.example/')
        browser0 = _FakeBrowser()
        browser1 = _FakeBrowser()
        resources = {0: ({}, browser0), 1: ({7: live}, browser1)}

        class _FakeWindow:

            def __init__(self) -> None:
                self.raised = False
                self.activated = False

            def raise_(self) -> None:  # noqa: N802 - Qt naming
                self.raised = True

            def activateWindow(self) -> None:  # noqa: N802 - Qt naming
                self.activated = True

        window1 = _FakeWindow()
        monkeypatch.setattr(
            manager_module.objreg, 'window_registry',
            {win_id: object() for win_id in resources})

        def fake_get(name, **_kwargs):
            win_id = _kwargs.get('window')
            if name == 'main-window':
                return window1 if win_id == 1 else None
            if name == 'tab-registry':
                return resources[win_id][0]
            if name == 'tabbed-browser':
                return resources[win_id][1]
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (0, 1, 0)
        assert browser1.widget.set_calls == [live]
        assert window1.raised is True
        assert window1.activated is True

    def test_only_suspended_members_processed(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('OPEN', 'https://open.example/'),
                      ('SUSPENDED', 'https://susp.example/')])
        browser = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser)})

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (1, 0, 0)
        assert [url for url, _ in browser.opened] == ['https://susp.example/']

    def test_empty_url_is_skipped(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', ''),
                      ('SUSPENDED', 'https://b.example/')])
        browser = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser)})

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (1, 0, 1)
        assert [url for url, _ in browser.opened] == ['https://b.example/']

    def test_tabopen_exception_skips_and_continues(
            self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://bad.example/'),
                      ('SUSPENDED', 'https://good.example/')])
        browser = self._RaisingBrowser(fail_urls={'https://bad.example/'})
        self._install(monkeypatch, {0: ({}, browser)})

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (1, 0, 1)
        assert [url for url, _ in browser.opened] == ['https://good.example/']

    def test_set_current_widget_exception_skips(
            self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/')])
        live = _FakeLiveTab('https://a.example/')
        browser = self._RaisingBrowserWidget()
        self._install(monkeypatch, {0: ({1: live}, browser)})

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (0, 0, 1)
        assert browser.opened == []
        assert workspace.tabs[0].open_status == 'SUSPENDED'

    def test_no_suspended_tabs_emits_nothing(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('OPEN', 'https://a.example/')])
        browser = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser)})
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (0, 0, 0)
        assert changes == []

    def test_no_window_browser_skips(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/')])
        monkeypatch.setattr(
            manager_module.objreg, 'window_registry', {})

        result = manager.reopen_group(workspace.id, group.group_id)

        assert result == (0, 0, 1)

    def test_unknown_group_raises(self, manager, monkeypatch):
        workspace = manager.add_workspace('w')
        self._install(monkeypatch, {0: ({}, _FakeBrowser())})
        with pytest.raises(KeyError):
            manager.reopen_group(workspace.id, uuid.uuid4())

    def test_unknown_workspace_is_noop(self, manager, monkeypatch):
        self._install(monkeypatch, {0: ({}, _FakeBrowser())})
        assert manager.reopen_group(uuid.uuid4(), uuid.uuid4()) == (0, 0, 0)

    def test_progress_reported_for_opens(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/'),
                      ('SUSPENDED', 'https://b.example/'),
                      ('SUSPENDED', 'https://c.example/')])
        browser = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser)})
        events = []

        result = manager.reopen_group(
            workspace.id, group.group_id,
            on_progress=lambda *args: events.append(args))

        assert result == (3, 0, 0)
        assert events == [(1, 3, 0), (2, 3, 0), (3, 3, 0)]

    def test_progress_reported_skip_interleaved(
            self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/'),
                      ('SUSPENDED', ''),
                      ('SUSPENDED', 'https://b.example/')])
        browser = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser)})
        events = []

        result = manager.reopen_group(
            workspace.id, group.group_id,
            on_progress=lambda *args: events.append(args))

        assert result == (2, 0, 1)
        assert events == [(1, 3, 0), (1, 3, 1), (2, 3, 1)]

    def test_progress_exception_is_swallowed(self, manager, monkeypatch):
        workspace, group = self._group(
            manager, [('SUSPENDED', 'https://a.example/'),
                      ('SUSPENDED', 'https://b.example/'),
                      ('SUSPENDED', 'https://c.example/')])
        browser = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser)})

        def fail(_done, _total, _skipped):
            raise RuntimeError('progress failed')

        result = manager.reopen_group(
            workspace.id, group.group_id, on_progress=fail)

        assert result == (3, 0, 0)
        assert [url for url, _ in browser.opened] == [
            'https://a.example/', 'https://b.example/', 'https://c.example/']


def _patch_affinity_config(monkeypatch, *, enabled=True):
    """Patch the manager module's global window-affinity kill switch."""
    panel = SimpleNamespace(window_affinity_enabled=enabled)
    monkeypatch.setattr(
        manager_module, 'config',
        SimpleNamespace(
            val=SimpleNamespace(
                content=SimpleNamespace(vertical_panel=panel))))


class TestCloseGroupAffinity:

    """VP-A1: closing a group records its window affinity."""

    def _group(self, manager, *, statuses=('OPEN',)):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        live_tabs = []
        registry = {}
        for index, status in enumerate(statuses, start=1):
            manager.add_tab(workspace.id, WorkspaceTab(
                tab_id=index, group_id=group.group_id,
                open_status=status))
            live = object()
            live_tabs.append(live)
            registry[index] = live
        browser = TestCloseGroup._CloseBrowser(live_tabs)
        return workspace, group, browser, registry

    @staticmethod
    def _install(monkeypatch, browser, registry):
        def fake_get(name, **_kwargs):
            if name == 'tab-registry':
                return registry
            if name == 'tabbed-browser':
                return browser
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

    def test_sets_affinity_when_missing(self, manager, monkeypatch):
        workspace, group, browser, registry = self._group(manager)
        self._install(monkeypatch, browser, registry)

        manager.close_group(workspace.id, group.group_id, win_id=2)

        assert group.window_affinity == 2

    def test_does_not_overwrite_existing_affinity(
            self, manager, monkeypatch):
        workspace, group, browser, registry = self._group(manager)
        group.window_affinity = 1
        self._install(monkeypatch, browser, registry)

        manager.close_group(workspace.id, group.group_id, win_id=2)

        assert group.window_affinity == 1

    def test_affinity_disabled_does_not_set(self, manager, monkeypatch):
        workspace, group, browser, registry = self._group(manager)
        group.affinity_enabled = False
        self._install(monkeypatch, browser, registry)

        manager.close_group(workspace.id, group.group_id, win_id=2)

        assert group.window_affinity is None

    def test_archived_group_does_not_set(self, manager, monkeypatch):
        workspace, group, browser, registry = self._group(manager)
        manager.archive_group(workspace.id, group.group_id)
        self._install(monkeypatch, browser, registry)

        manager.close_group(workspace.id, group.group_id, win_id=2)

        assert group.window_affinity is None

    def test_global_kill_switch_does_not_set(self, manager, monkeypatch):
        workspace, group, browser, registry = self._group(manager)
        self._install(monkeypatch, browser, registry)
        _patch_affinity_config(monkeypatch, enabled=False)

        manager.close_group(workspace.id, group.group_id, win_id=2)

        assert group.window_affinity is None

    def test_set_group_affinity_enabled(self, manager):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        changes = []
        manager.changed.connect(lambda: changes.append(True))

        manager.set_group_affinity_enabled(
            workspace.id, group.group_id, False)

        assert group.affinity_enabled is False
        assert changes == [True]


class TestReopenGroupAffinity:

    """VP-A1: Reopen Group routes restored tabs to the affinity window."""

    def _group(self, manager, url='https://a.example/'):
        workspace = manager.add_workspace('w')
        group = manager.create_group(workspace.id, 'g')
        manager.add_tab(workspace.id, WorkspaceTab(
            tab_id=1, group_id=group.group_id, open_status='SUSPENDED',
            url=url))
        return workspace, group

    @staticmethod
    def _install(monkeypatch, resources):
        """Install a fake objreg exposing ``win_id -> (registry, browser)``."""
        monkeypatch.setattr(
            manager_module.objreg, 'window_registry',
            {win_id: object() for win_id in resources})

        def fake_get(name, **_kwargs):
            win_id = _kwargs.get('window')
            if name == 'tab-registry':
                return resources[win_id][0]
            if name == 'tabbed-browser':
                return resources[win_id][1]
            raise KeyError(name)

        monkeypatch.setattr(manager_module.objreg, 'get', fake_get)

    def test_live_affinity_routes_to_that_window(
            self, manager, monkeypatch):
        workspace, group = self._group(manager)
        group.window_affinity = 1
        browser0 = _FakeBrowser()
        browser1 = _FakeBrowser()
        self._install(monkeypatch, {
            0: ({}, browser0),
            1: ({}, browser1),
        })

        result = manager.reopen_group(
            workspace.id, group.group_id, win_id=0)

        assert result == (1, 0, 0)
        assert [url for url, _ in browser1.opened] == [
            'https://a.example/']
        assert browser0.opened == []
        assert group.window_affinity == 1

    def test_dead_affinity_uses_current_and_updates(
            self, manager, monkeypatch):
        workspace, group = self._group(manager)
        group.window_affinity = 9
        browser0 = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser0)})

        result = manager.reopen_group(
            workspace.id, group.group_id, win_id=0)

        assert result == (1, 0, 0)
        assert [url for url, _ in browser0.opened] == [
            'https://a.example/']
        assert group.window_affinity == 0

    def test_no_affinity_uses_current_and_does_not_set(
            self, manager, monkeypatch):
        workspace, group = self._group(manager)
        browser0 = _FakeBrowser()
        self._install(monkeypatch, {0: ({}, browser0)})

        result = manager.reopen_group(
            workspace.id, group.group_id, win_id=0)

        assert result == (1, 0, 0)
        assert browser0.opened == [('https://a.example/', False)]
        assert group.window_affinity is None

    def test_affinity_disabled_uses_current_and_does_not_set(
            self, manager, monkeypatch):
        workspace, group = self._group(manager)
        group.window_affinity = 1
        group.affinity_enabled = False
        browser0 = _FakeBrowser()
        browser1 = _FakeBrowser()
        self._install(monkeypatch, {
            0: ({}, browser0),
            1: ({}, browser1),
        })

        result = manager.reopen_group(
            workspace.id, group.group_id, win_id=0)

        assert result == (1, 0, 0)
        assert browser0.opened == [('https://a.example/', False)]
        assert browser1.opened == []
        # Disabled affinity never writes to the stored value.
        assert group.window_affinity == 1

    def test_global_kill_switch_uses_current_for_every_group(
            self, manager, monkeypatch):
        workspace, group = self._group(manager)
        group.window_affinity = 1
        browser0 = _FakeBrowser()
        browser1 = _FakeBrowser()
        self._install(monkeypatch, {
            0: ({}, browser0),
            1: ({}, browser1),
        })
        _patch_affinity_config(monkeypatch, enabled=False)

        result = manager.reopen_group(
            workspace.id, group.group_id, win_id=0)

        assert result == (1, 0, 0)
        assert browser0.opened == [('https://a.example/', False)]
        assert browser1.opened == []
        assert group.window_affinity == 1


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
