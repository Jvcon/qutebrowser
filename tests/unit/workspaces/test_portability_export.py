# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.portability (export half of #27)."""

import datetime
import json
import stat
import sys

import pytest

from qutebrowser.workspaces import portability
from qutebrowser.workspaces.model import PinnedTab, WorkspaceTab

# Reuse the existing command-test stub so both halves of the workspace
# command surface are exercised with the same real model objects.
from test_commands import FakeManager


def _manager() -> FakeManager:
    """Return a manager with one group, a grouped tab and a standalone tab."""
    manager = FakeManager()
    workspace = manager.add_workspace('research')
    group = manager.create_group(workspace.id, 'papers')
    workspace.tabs.append(WorkspaceTab(
        tab_id=1,
        url='https://example.com/paper',
        title='Paper',
        group_id=group.group_id,
    ))
    workspace.tabs.append(WorkspaceTab(
        tab_id=2,
        url='https://example.com/standalone',
        title='Standalone',
    ))
    return manager


def _wire(monkeypatch: pytest.MonkeyPatch, manager: FakeManager) -> None:
    """Point the module at *manager* instead of the object registry."""
    monkeypatch.setattr(portability, '_manager', lambda: manager)


def _iter_dicts(value):
    """Yield every dict nested inside *value*."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def test_export_writes_valid_json(tmp_path, monkeypatch) -> None:
    manager = _manager()
    _wire(monkeypatch, manager)

    out = portability.export_to_path(tmp_path / 'export.json')

    assert out == (tmp_path / 'export.json').resolve()
    document = json.loads(out.read_text(encoding='utf-8'))
    assert document['schema_version'] == 1
    assert datetime.datetime.fromisoformat(document['exported_at'])
    assert {
        'schema_version',
        'exported_at',
        'qutebrowser_version',
        'workspaces',
    } <= set(document)
    assert [ws['name'] for ws in document['workspaces']] == ['research']


def test_export_excludes_machine_local_fields(tmp_path, monkeypatch) -> None:
    manager = _manager()
    _wire(monkeypatch, manager)
    workspace = manager.list_all_workspaces()[0]
    group = workspace.groups[0]
    tab = workspace.tabs[0]

    workspace.last_active = '2026-01-01T00:00:00+00:00'
    group.window_affinity = 3
    group.affinity_enabled = False
    tab.open_status = 'OPEN'
    tab.last_fetched = '2026-01-01T00:00:00+00:00'
    tab.fetch_attempts = 4
    tab.last_error = 'boom'
    manager.pinned.append(PinnedTab(
        tab_id=9,
        title='Pinned',
        url='https://example.com/pinned',
        last_active='2026-01-01T00:00:00+00:00',
    ))

    out = portability.export_to_path(tmp_path / 'export.json')
    document = json.loads(out.read_text(encoding='utf-8'))

    excluded = {
        'last_active',
        'window_affinity',
        'affinity_enabled',
        'tab_id',
        'open_status',
        'last_fetched',
        'fetch_attempts',
        'last_error',
    }
    for obj in _iter_dicts(document):
        assert not (excluded & set(obj))


def test_export_carries_user_judgment_fields(tmp_path, monkeypatch) -> None:
    manager = _manager()
    _wire(monkeypatch, manager)
    workspace = manager.list_all_workspaces()[0]
    tab = workspace.tabs[0]
    tab.reading_status = 'DONE'
    tab.done_at = '2026-02-03T04:05:06+00:00'

    out = portability.export_to_path(tmp_path / 'export.json')
    document = json.loads(out.read_text(encoding='utf-8'))

    entry = document['workspaces'][0]['groups'][0]['entries'][0]
    assert entry['reading_status'] == 'DONE'
    assert entry['done_at'] == '2026-02-03T04:05:06+00:00'


def test_export_normalizes_url(tmp_path, monkeypatch) -> None:
    manager = _manager()
    _wire(monkeypatch, manager)
    workspace = manager.list_all_workspaces()[0]
    group = workspace.groups[0]
    workspace.tabs = [
        WorkspaceTab(
            tab_id=1,
            url='  HTTP://Example.COM/Path  ',
            group_id=group.group_id,
        ),
        WorkspaceTab(
            tab_id=2,
            url='http://example.com/Path',
            group_id=group.group_id,
        ),
    ]

    out = portability.export_to_path(tmp_path / 'export.json')
    document = json.loads(out.read_text(encoding='utf-8'))

    urls = [
        entry['url']
        for entry in document['workspaces'][0]['groups'][0]['entries']
    ]
    assert urls == ['http://example.com/Path', 'http://example.com/Path']


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits')
def test_export_file_mode_0600_on_unix(tmp_path, monkeypatch) -> None:
    _wire(monkeypatch, _manager())

    out = portability.export_to_path(tmp_path / 'export.json')

    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_export_creates_parent_dir(tmp_path, monkeypatch) -> None:
    _wire(monkeypatch, _manager())
    target = tmp_path / 'deep' / 'nested' / 'dir' / 'out.json'

    out = portability.export_to_path(target)

    assert out == target.resolve()
    assert out.exists()


def test_export_carries_pinned_tabs_at_document_root(
        tmp_path, monkeypatch) -> None:
    """PinnedTab is profile-scoped, so the document carries exactly one copy."""
    manager = _manager()
    _wire(monkeypatch, manager)
    manager.pinned.append(PinnedTab(
        tab_id=9,
        title='Pinned',
        url='https://example.com/pinned',
    ))

    out = portability.export_to_path(tmp_path / 'export.json')
    document = json.loads(out.read_text(encoding='utf-8'))

    assert document['pinned'] == [{
        'title': 'Pinned',
        'url': 'https://example.com/pinned',
        'favicon': None,
    }]
    # And no workspace dict should expose the duplicated list.
    for ws in document['workspaces']:
        assert 'pinned' not in ws


def test_export_carries_tab_favicon(tmp_path, monkeypatch) -> None:
    """WorkspaceTab.favicon is a base64 string and must round-trip."""
    manager = _manager()
    _wire(monkeypatch, manager)
    workspace = manager.list_all_workspaces()[0]
    workspace.tabs[0].favicon = 'aGVsbG8='  # arbitrary base64 ASCII bytes

    out = portability.export_to_path(tmp_path / 'export.json')
    document = json.loads(out.read_text(encoding='utf-8'))

    entry = document['workspaces'][0]['groups'][0]['entries'][0]
    assert entry['favicon'] == 'aGVsbG8='


def test_export_emits_tabs_index(tmp_path, monkeypatch) -> None:
    """Each source tab_id must be reachable from tabs_index by URL so that
    the importer can rebuild parent_id chains (ADR 0009 § 2)."""
    manager = _manager()
    _wire(monkeypatch, manager)

    out = portability.export_to_path(tmp_path / 'export.json')
    document = json.loads(out.read_text(encoding='utf-8'))

    # Two tabs in the stub manager: grouped tab_id=1, standalone tab_id=2.
    assert document['tabs_index'] == {
        '1': 'https://example.com/paper',
        '2': 'https://example.com/standalone',
    }
