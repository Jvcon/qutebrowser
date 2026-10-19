# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.manager."""

import datetime
import uuid

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
        self.opened: list[tuple[str, bool]] = []
        # VP-F4's new-window flow constructs a fresh ``MainWindow`` and
        # reads the source browser's ``is_private`` flag to mirror it.
        self.is_private = False

    def tabopen(self, url: QUrl, newwindow: bool = False) -> None:
        self.opened.append((url.toString(), newwindow))


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
        assert new_browser.opened == [('https://b.example/', False)]

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
            ('https://a.example/', False),
            ('https://b.example/', False),
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


class TestReopenGroup:

    """Tests for ``WorkspaceManager.reopen_group`` (VP-D4)."""

    class _RaisingBrowser(_FakeBrowser):

        """A browser whose ``tabopen`` raises for the given URLs."""

        def __init__(self, fail_urls=()) -> None:
            super().__init__()
            self._fail = set(fail_urls)

        def tabopen(self, url, newwindow: bool = False) -> None:
            if url.toString() in self._fail:
                raise RuntimeError('tabopen failed')
            super().tabopen(url, newwindow)

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
