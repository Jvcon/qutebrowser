# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the import half of qutebrowser.workspaces.portability."""

import datetime
import json
import uuid

import pytest

from qutebrowser.workspaces import portability
from qutebrowser.workspaces.model import WorkspaceTab

# Reuse the existing command-test stub, like the export-half tests do.
from test_commands import FakeManager


def _wire(monkeypatch: pytest.MonkeyPatch, manager: FakeManager) -> None:
    monkeypatch.setattr(portability, '_manager', lambda: manager)


def _write(tmp_path, document: dict) -> object:
    path = tmp_path / 'import.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def _document(*, workspaces=None, pinned=None, schema_version=1) -> dict:
    return {
        'schema_version': schema_version,
        'pinned': [] if pinned is None else pinned,
        'workspaces': [] if workspaces is None else workspaces,
    }


def _workspace_doc(*, workspace_id=None, name='imported', groups=None,
                   entries=None, created_at=None, archived=False) -> dict:
    return {
        'id': str(workspace_id or uuid.uuid4()),
        'name': name,
        'archived': archived,
        'created_at': created_at,
        'groups': [] if groups is None else groups,
        'entries': [] if entries is None else entries,
    }


def _group_doc(*, group_id=None, name='group', entries=None,
               created_at='2026-01-01T00:00:00+00:00') -> dict:
    return {
        'group_id': str(group_id or uuid.uuid4()),
        'name': name,
        'folded': False,
        'pinned': False,
        'archived': False,
        'created_at': created_at,
        'entries': [] if entries is None else entries,
    }


def _entry_doc(url='https://example.com/a', *, parent_id=None,
               title='Entry', tab_id=None) -> dict:
    entry = {
        'title': title,
        'url': url,
        'description': '',
        'source': '',
        'favicon': None,
        'reading_status': 'FRESH',
        'done_at': None,
        'created_at': '2026-01-01T00:00:00+00:00',
        'parent_id': parent_id,
        'group_id': None,
    }
    if tab_id is not None:
        entry['tab_id'] = tab_id
    return entry


def _all_groups(manager: FakeManager):
    return [
        group
        for workspace in manager.list_all_workspaces()
        for group in workspace.groups
    ]


def test_import_rejects_newer_schema_version(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    path = _write(tmp_path, _document(
        schema_version=2,
        workspaces=[_workspace_doc()],
    ))

    with pytest.raises(
            portability.PortabilityError,
            match=r'unsupported schema_version=2; this build only knows up to 1'):
        portability.import_from_path(path)

    assert manager.list_all_workspaces() == []


def test_import_skips_known_workspace_id(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    group = _group_doc(entries=[_entry_doc()])
    document = _document(workspaces=[_workspace_doc(groups=[group])])
    path = _write(tmp_path, document)

    first = portability.import_from_path(path)
    second = portability.import_from_path(path)

    assert first.added_workspaces == 1
    assert first.added_entries == 1
    assert second.added_workspaces == 0
    assert second.added_entries == 0
    assert second.skipped_workspaces == 1
    assert len(manager.list_all_workspaces()) == 1


def test_import_dedupes_entry_by_url(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    group = _group_doc(entries=[
        _entry_doc('  HTTP://Example.COM/Path  '),
        _entry_doc('http://example.com/Path'),
    ])
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[group])]))

    report = portability.import_from_path(path)

    workspace = manager.list_all_workspaces()[0]
    assert report.added_entries == 1
    # The duplicate came from the same document, so it is a merge, not a
    # skip of a pre-existing local entry (issue #49).
    assert report.merged_entries == 1
    assert report.skipped_entries == 0
    assert [tab.url for tab in workspace.tabs] == ['http://example.com/Path']
    assert workspace.tabs[0].group_id == uuid.UUID(group['group_id'])


def test_import_merges_same_url_across_groups(tmp_path, monkeypatch) -> None:
    """URL de-duplication is workspace-scoped, not group-scoped (#49)."""
    manager = FakeManager()
    _wire(monkeypatch, manager)
    first_group = _group_doc(name='first', entries=[
        _entry_doc('https://example.com/shared', title='First'),
    ])
    second_group = _group_doc(name='second', entries=[
        _entry_doc('https://example.com/shared', title='Second'),
    ])
    path = _write(tmp_path, _document(workspaces=[_workspace_doc(
        groups=[first_group, second_group])]))

    report = portability.import_from_path(path)

    workspace = manager.list_all_workspaces()[0]
    assert [tab.url for tab in workspace.tabs] == [
        'https://example.com/shared']
    # First occurrence wins: the entry keeps the first group it appeared in.
    assert workspace.tabs[0].group_id == uuid.UUID(first_group['group_id'])
    assert workspace.tabs[0].title == 'First'
    assert report.added_entries == 1
    assert report.merged_entries == 1
    assert report.skipped_entries == 0


def test_import_merges_group_entry_into_standalone_entry(
        tmp_path, monkeypatch) -> None:
    """Groups are imported before standalone entries, so the group wins."""
    manager = FakeManager()
    _wire(monkeypatch, manager)
    group = _group_doc(entries=[
        _entry_doc('https://example.com/shared', title='Grouped'),
    ])
    path = _write(tmp_path, _document(workspaces=[_workspace_doc(
        groups=[group],
        entries=[_entry_doc('https://example.com/shared', title='Standalone')],
    )]))

    report = portability.import_from_path(path)

    workspace = manager.list_all_workspaces()[0]
    assert len(workspace.tabs) == 1
    assert workspace.tabs[0].group_id == uuid.UUID(group['group_id'])
    assert report.merged_entries == 1


def test_import_skips_local_url_from_another_group(
        tmp_path, monkeypatch) -> None:
    """A local URL in a different group must be skipped, not duplicated."""
    manager = FakeManager()
    _wire(monkeypatch, manager)
    local_workspace = manager.add_workspace('local')
    local_group_a = manager.create_group(local_workspace.id, 'a')
    local_group_b = manager.create_group(local_workspace.id, 'b')
    manager.add_tab(local_workspace.id, WorkspaceTab(
        tab_id=1,
        group_id=local_group_a.group_id,
        url='https://example.com/shared',
        title='Local',
    ))
    # The document's group reuses the *other* local group, so the entry
    # targets the local workspace and collides with group a's entry.
    imported_group = _group_doc(
        group_id=local_group_b.group_id,
        name='b',
        entries=[_entry_doc('https://example.com/shared', title='Imported')],
    )
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[imported_group])]))

    report = portability.import_from_path(path)

    matching = [tab for tab in local_workspace.tabs
                if tab.url == 'https://example.com/shared']
    assert len(matching) == 1
    assert matching[0].tab_id == 1
    assert matching[0].group_id == local_group_a.group_id
    assert matching[0].title == 'Local'
    assert report.added_entries == 0
    assert report.merged_entries == 0
    assert report.skipped_entries == 1


def test_import_report_separates_merge_from_local_skip(
        tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    local_workspace = manager.add_workspace('local')
    local_group = manager.create_group(local_workspace.id, 'local')
    manager.add_tab(local_workspace.id, WorkspaceTab(
        tab_id=1,
        group_id=local_group.group_id,
        url='https://example.com/local',
        title='Local',
    ))
    # Group id reuses the local group so the entries target the local
    # workspace: one local collision, one new entry and two in-document
    # duplicates.
    imported_group = _group_doc(
        group_id=local_group.group_id,
        name='local',
        entries=[
            _entry_doc('https://example.com/local'),
            _entry_doc('https://example.com/new'),
            _entry_doc('https://example.com/new'),
            _entry_doc('https://example.com/new'),
        ],
    )
    path = _write(tmp_path, _document(workspaces=[_workspace_doc(
        groups=[imported_group])]))

    report = portability.import_from_path(path)

    assert report.added_entries == 1
    assert report.skipped_entries == 1
    assert report.merged_entries == 2
    assert report.total_collapsed_entries == 3
    assert sorted(tab.url for tab in local_workspace.tabs) == [
        'https://example.com/local', 'https://example.com/new']


def test_import_keeps_same_url_in_different_workspaces(
        tmp_path, monkeypatch) -> None:
    """Workspace-level dedup must not merge across document workspaces."""
    manager = FakeManager()
    _wire(monkeypatch, manager)
    first = _workspace_doc(name='first', groups=[_group_doc(
        entries=[_entry_doc('https://example.com/shared')])])
    second = _workspace_doc(name='second', groups=[_group_doc(
        entries=[_entry_doc('https://example.com/shared')])])
    path = _write(tmp_path, _document(workspaces=[first, second]))

    report = portability.import_from_path(path)

    assert report.added_workspaces == 2
    assert report.added_entries == 2
    assert report.merged_entries == 0
    assert report.skipped_entries == 0


def test_import_merged_entry_is_parent_target(tmp_path, monkeypatch) -> None:
    """``parent_id`` of a merged duplicate resolves to the surviving entry."""
    manager = FakeManager()
    _wire(monkeypatch, manager)
    parent_url = 'https://example.com/parent'
    first_group = _group_doc(name='first', entries=[
        _entry_doc(parent_url, tab_id=1, title='Parent'),
    ])
    second_group = _group_doc(name='second', entries=[
        _entry_doc(parent_url, tab_id=2, title='Duplicate parent'),
        _entry_doc('https://example.com/child', tab_id=3, parent_id=2),
    ])
    path = _write(tmp_path, _document(workspaces=[_workspace_doc(
        groups=[first_group, second_group])]))

    report = portability.import_from_path(path)

    workspace = manager.list_all_workspaces()[0]
    parents = [tab for tab in workspace.tabs if tab.url == parent_url]
    children = [tab for tab in workspace.tabs
                if tab.url == 'https://example.com/child']
    assert len(parents) == 1
    assert len(children) == 1
    assert children[0].parent_id == parents[0].tab_id
    assert report.added_entries == 2
    assert report.merged_entries == 1
    assert report.orphan_promotions == 0


def test_import_pinned_report_counts_added_and_skipped(
        tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    pinned = [
        {'title': 'Pinned', 'url': 'https://example.com/pinned'},
        {'title': 'Pinned duplicate',
         'url': '  HTTPS://Example.com/pinned'},
    ]
    path = _write(tmp_path, _document(pinned=pinned))

    first = portability.import_from_path(path)
    second = portability.import_from_path(path)

    assert first.added_pinned == 1
    assert first.skipped_pinned == 1
    assert second.added_pinned == 0
    assert second.skipped_pinned == 2
    assert len(manager.list_pinned()) == 1


def test_import_promotes_orphan_parent_to_top_level(
        tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    group = _group_doc(entries=[
        _entry_doc('https://example.com/child', parent_id=999,
                   tab_id=2),
    ])
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[group])]))

    report = portability.import_from_path(path)

    workspace = manager.list_all_workspaces()[0]
    child = workspace.tabs[0]
    assert child.parent_id is None
    assert report.orphan_promotions == 1


def test_import_remaps_parent_through_source_tab_id(
        tmp_path, monkeypatch) -> None:
    """When a document carries source tab ids, the child nests under the new id."""
    manager = FakeManager()
    _wire(monkeypatch, manager)
    group = _group_doc(entries=[
        _entry_doc('https://example.com/parent', tab_id=1),
        _entry_doc('https://example.com/child', parent_id=1, tab_id=2),
    ])
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[group])]))

    report = portability.import_from_path(path)

    workspace = manager.list_all_workspaces()[0]
    by_url = {tab.url: tab for tab in workspace.tabs}
    parent = by_url['https://example.com/parent']
    child = by_url['https://example.com/child']
    assert child.parent_id == parent.tab_id
    assert report.orphan_promotions == 0


def test_import_reuses_existing_group(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    local_workspace = manager.add_workspace('local')
    local_group = manager.create_group(local_workspace.id, 'shared')
    group = _group_doc(
        group_id=local_group.group_id,
        name='shared',
        entries=[_entry_doc('https://example.com/new')],
    )
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[group])]))

    report = portability.import_from_path(path)

    groups = [entry for entry in _all_groups(manager)
              if entry.group_id == local_group.group_id]
    assert len(groups) == 1
    assert groups[0].name == 'shared'
    assert any(tab.url == 'https://example.com/new'
               for tab in local_workspace.tabs)
    assert report.added_workspaces == 1
    assert report.added_entries == 1


def test_import_is_purely_additive(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    local_workspace = manager.add_workspace('local')
    local_group = manager.create_group(local_workspace.id, 'kept')
    manager.add_tab(local_workspace.id, WorkspaceTab(
        tab_id=1,
        group_id=local_group.group_id,
        url='https://example.com/local',
        title='Local',
    ))
    before = local_workspace.to_dict()

    group = _group_doc(entries=[_entry_doc('https://example.com/new')])
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[group])]))

    portability.import_from_path(path)

    assert local_workspace.to_dict() == before
    assert len(manager.list_all_workspaces()) == 2


def test_import_tolerates_null_created_at(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    group = _group_doc(created_at=None, entries=[
        _entry_doc('https://example.com/null-created'),
    ])
    group['entries'][0]['created_at'] = None
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[group], created_at=None)]))

    report = portability.import_from_path(path)

    workspace = manager.list_all_workspaces()[0]
    assert report.added_workspaces == 1
    assert report.added_entries == 1
    assert workspace.tabs[0].created_at is not None


def test_import_pinned_round_trips(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    pinned = [{
        'title': 'Pinned',
        'url': 'https://example.com/pinned',
        'favicon': 'aGVsbG8=',
    }]
    path = _write(tmp_path, _document(pinned=pinned))

    portability.import_from_path(path)
    portability.import_from_path(path)

    assert len(manager.list_pinned()) == 1
    imported = manager.list_pinned()[0]
    assert imported.url == 'https://example.com/pinned'
    assert imported.favicon == b'hello'


def test_import_applies_migration_chain(tmp_path, monkeypatch) -> None:
    manager = FakeManager()
    _wire(monkeypatch, manager)
    calls = []

    def migration(document):
        calls.append(document['schema_version'])
        return document

    monkeypatch.setitem(portability.MIGRATIONS, 1, migration)
    group = _group_doc(entries=[_entry_doc()])
    path = _write(tmp_path, _document(
        workspaces=[_workspace_doc(groups=[group])]))

    report = portability.import_from_path(path)

    assert calls == [1]
    assert report.added_workspaces == 1
    assert report.added_entries == 1


def test_import_round_trip_preserves_parent_tree(
        tmp_path, monkeypatch) -> None:
    """A round-tripped document must preserve parent_id chains.

    The exporter puts source-tab-id → URL at the document root in
    tabs_index so the importer can rebuild the tree. Without tabs_index
    every child with a parent_id would silently become an orphan.
    """
    src = FakeManager()
    _wire(monkeypatch, src)
    workspace = src.add_workspace('research')
    group = src.create_group(workspace.id, 'papers')
    parent_tab = WorkspaceTab(
        tab_id=1,
        url='https://example.com/parent',
        title='Parent',
        group_id=group.group_id,
    )
    child_tab = WorkspaceTab(
        tab_id=2,
        url='https://example.com/child',
        title='Child',
        group_id=group.group_id,
        parent_id=1,
    )
    src.add_tab(workspace.id, parent_tab)
    src.add_tab(workspace.id, child_tab)

    written_path = portability.export_to_path(tmp_path / 'rt.json')

    # The exporter's emitted document carries tabs_index, not inline tab_id.
    document = json.loads(written_path.read_text(encoding='utf-8'))
    assert document['tabs_index'] == {
        '1': 'https://example.com/parent',
        '2': 'https://example.com/child',
    }
    for entry in document['workspaces'][0]['groups'][0]['entries']:
        assert 'tab_id' not in entry

    # Import into a fresh manager and assert the tree shape survived.
    dst = FakeManager()
    _wire(monkeypatch, dst)
    report = portability.import_from_path(written_path)

    assert report.added_workspaces == 1
    assert report.added_entries == 2
    assert report.orphan_promotions == 0

    tabs = dst.list_all_workspaces()[0].tabs
    by_url = {t.url: t for t in tabs}
    assert by_url['https://example.com/child'].parent_id \
        == by_url['https://example.com/parent'].tab_id


def test_import_round_trip_preserves_workspace_created_at(
        tmp_path, monkeypatch) -> None:
    """A round-tripped workspace must keep its original creation time.

    Before issue #40 the field was a structural null; after #40 a real
    timestamp is set on the model and forwarded by the exporter.
    """
    src = FakeManager()
    _wire(monkeypatch, src)
    src.add_workspace('legacy')
    workspace = src.list_all_workspaces()[0]
    workspace.created_at = '2026-02-03T04:05:06+00:00'

    written_path = portability.export_to_path(tmp_path / 'rt-created.json')

    document = json.loads(written_path.read_text(encoding='utf-8'))
    assert document['workspaces'][0]['created_at'] == (
        '2026-02-03T04:05:06+00:00'
    )

    dst = FakeManager()
    _wire(monkeypatch, dst)
    portability.import_from_path(written_path)
    loaded = dst.list_all_workspaces()[0]
    assert loaded.created_at == '2026-02-03T04:05:06+00:00'


def test_import_null_workspace_created_at_stamps_now(
        tmp_path, monkeypatch) -> None:
    """Older documents with ``created_at: null`` import without crashing.

    The freshly-imported workspace is stamped with a now-UTC timestamp so
    the field is never silently broken-looking in the manager. Issue #40
    made this path reachable in practice (legacy exports before the
    field existed).
    """
    manager = FakeManager()
    _wire(monkeypatch, manager)
    group = _group_doc()
    workspace_doc = _workspace_doc(groups=[group], created_at=None)
    path = _write(tmp_path, _document(workspaces=[workspace_doc]))

    portability.import_from_path(path)
    loaded = manager.list_all_workspaces()[0]
    assert loaded.created_at is not None
    datetime.datetime.fromisoformat(loaded.created_at)


class TestRealManager:
    """End-to-end import against the real manager, not the FakeManager.

    Every other test in this module drives ``test_commands.FakeManager``,
    which mirrors the manager API but does not run ``add_tab`` /
    ``create_group`` / ``set_parent`` for real and never touches YAML.
    These tests close that gap for the workspace-scoped URL de-duplication
    introduced by issue #49: the merged state must survive
    ``save()``/``load()`` through the real persistence layer.
    """

    @staticmethod
    def _manager(tmp_path):
        from qutebrowser.workspaces.manager import WorkspaceManager
        from qutebrowser.workspaces.persistence import (
            PinnedPersistence, WorkspacePersistence)

        manager = WorkspaceManager(
            WorkspacePersistence(base_path=tmp_path / 'workspaces'),
            PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml'))
        manager.load()
        return manager

    @staticmethod
    def _reloaded(tmp_path):
        """Re-read the workspace state from disk, like a restart would."""
        from qutebrowser.workspaces.manager import WorkspaceManager
        from qutebrowser.workspaces.persistence import (
            PinnedPersistence, WorkspacePersistence)

        manager = WorkspaceManager(
            WorkspacePersistence(base_path=tmp_path / 'workspaces'),
            PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml'))
        manager.load()
        return manager

    def test_cross_group_dedup_survives_a_restart(
            self, tmp_path, monkeypatch) -> None:
        manager = self._manager(tmp_path)
        monkeypatch.setattr(portability, '_manager', lambda: manager)
        first = _group_doc(name='first', entries=[
            _entry_doc('https://example.com/shared', title='First')])
        second = _group_doc(name='second', entries=[
            _entry_doc('https://example.com/shared', title='Second')])
        path = _write(tmp_path, _document(workspaces=[_workspace_doc(
            groups=[first, second])]))

        report = portability.import_from_path(path)
        manager.save()

        assert report.added_entries == 1
        assert report.merged_entries == 1
        assert report.skipped_entries == 0

        workspace = manager.list_all_workspaces()[0]
        assert [tab.url for tab in workspace.tabs] == [
            'https://example.com/shared']
        assert workspace.tabs[0].group_id == uuid.UUID(first['group_id'])

        # The de-duplicated state must be what a restart reads back, not
        # just what the in-memory model holds.
        restarted = self._reloaded(tmp_path)
        reloaded = restarted.list_all_workspaces()[0]
        assert [tab.url for tab in reloaded.tabs] == [
            'https://example.com/shared']
        assert reloaded.tabs[0].group_id == uuid.UUID(first['group_id'])
        assert reloaded.tabs[0].title == 'First'

    def test_local_entry_in_a_reused_group_is_skipped(
            self, tmp_path, monkeypatch) -> None:
        """A pre-existing local entry wins, even when it is in another group.

        ``groups_by_id`` is pre-populated from every local group
        (``portability.py:661-665``), so a document which reuses a
        ``group_id`` that already exists locally adds its entries into that
        local group. That is the one production path where a local URL
        collision is reachable at all -- a fresh workspace has no local
        tabs, and a local workspace id makes the whole workspace skipped.
        """
        manager = self._manager(tmp_path)
        local = manager.add_workspace('local')
        group = manager.create_group(local.id, 'reused')
        manager.add_tab(local.id, WorkspaceTab(
            tab_id=7, url='https://example.com/shared', title='Local'))
        manager.move_tab_to_group(local.id, 7, group.group_id)
        monkeypatch.setattr(portability, '_manager', lambda: manager)

        # New workspace id, but the document reuses the local group id.
        group_doc = _group_doc(group_id=group.group_id, name='reused',
                               entries=[
                                   _entry_doc(
                                       'https://example.com/shared',
                                       title='FromDocument')])
        path = _write(tmp_path, _document(workspaces=[_workspace_doc(
            groups=[group_doc])]))

        report = portability.import_from_path(path)

        assert report.added_workspaces == 1
        assert report.added_entries == 0
        assert report.merged_entries == 0
        # The URL was already present locally, so it is a skip, not a merge.
        assert report.skipped_entries == 1

        local_workspace = manager.get_workspace(local.id)
        assert [tab.title for tab in local_workspace.tabs] == ['Local']
