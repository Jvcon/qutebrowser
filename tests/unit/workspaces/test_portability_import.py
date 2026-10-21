# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the import half of qutebrowser.workspaces.portability."""

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
    assert report.skipped_entries == 1
    assert [tab.url for tab in workspace.tabs] == ['http://example.com/Path']
    assert workspace.tabs[0].group_id == uuid.UUID(group['group_id'])


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
