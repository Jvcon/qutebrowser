# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.persistence."""

import uuid

import pytest

from qutebrowser.workspaces import model
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)


@pytest.fixture
def ws_persistence(tmp_path):
    """A WorkspacePersistence rooted in a temporary directory."""
    return WorkspacePersistence(base_path=tmp_path / 'workspaces')


@pytest.fixture
def pinned_persistence(tmp_path):
    """A PinnedPersistence rooted in a temporary directory."""
    return PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml')


class TestWorkspacePersistence:

    def test_save_creates_file_at_id_path(self, ws_persistence):
        workspace = model.Workspace(id=uuid.uuid4(), name='w')
        ws_persistence.save(workspace)

        path = ws_persistence.base_path / f'{workspace.id}.yml'
        assert path.exists()
        assert ws_persistence.load(workspace.id) == workspace

    def test_load_missing_raises_file_not_found(self, ws_persistence):
        with pytest.raises(FileNotFoundError):
            ws_persistence.load(uuid.uuid4())

    def test_load_all(self, ws_persistence):
        first = model.Workspace(id=uuid.uuid4(), name='a')
        second = model.Workspace(id=uuid.uuid4(), name='b')
        ws_persistence.save(first)
        ws_persistence.save(second)

        loaded = ws_persistence.load_all()
        assert {workspace.id for workspace in loaded} == {first.id, second.id}
        assert [str(ws.id) for ws in loaded] == sorted(
            [str(first.id), str(second.id)])

    def test_delete_removes_file(self, ws_persistence):
        workspace = model.Workspace(id=uuid.uuid4(), name='w')
        ws_persistence.save(workspace)
        ws_persistence.delete(workspace.id)
        assert not (ws_persistence.base_path / f'{workspace.id}.yml').exists()

    def test_delete_missing_is_noop(self, ws_persistence):
        ws_persistence.delete(uuid.uuid4())

    def test_base_path_created(self, tmp_path):
        persistence = WorkspacePersistence(base_path=tmp_path / 'new')
        assert persistence.base_path.exists()


class TestPinnedPersistence:

    def test_save_load_roundtrip(self, pinned_persistence):
        pinned = [
            model.PinnedTab(tab_id=1, title='a', url='https://a.example',
                            favicon=b'abc',
                            last_active='2026-01-01T00:00:00+00:00'),
            model.PinnedTab(tab_id=2, title='b', url='https://b.example'),
        ]
        pinned_persistence.save(pinned)
        assert pinned_persistence.load() == pinned

    def test_missing_file_returns_empty(self, pinned_persistence):
        assert pinned_persistence.load() == []

    def test_parent_dir_auto_created(self, tmp_path):
        path = tmp_path / 'profile' / 'nested' / 'pinned.yml'
        PinnedPersistence(path=path)
        assert path.parent.exists()

    def test_empty_list_roundtrip(self, pinned_persistence):
        pinned_persistence.save([])
        assert pinned_persistence.load() == []
