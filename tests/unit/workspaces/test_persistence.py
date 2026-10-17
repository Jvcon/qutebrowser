# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.persistence."""

import logging
import uuid
from typing import Iterator

import pytest

from qutebrowser.utils import standarddir
from qutebrowser.workspaces import model
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence,
                                                DEFAULT_PROFILE_NAME)


@pytest.fixture
def ws_persistence(tmp_path):
    """A WorkspacePersistence rooted in a temporary directory."""
    return WorkspacePersistence(base_path=tmp_path / 'workspaces')


@pytest.fixture
def pinned_persistence(tmp_path):
    """A PinnedPersistence rooted in a temporary directory."""
    return PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml')


@pytest.fixture
def fake_data_dir(tmp_path, monkeypatch):
    """Point standarddir.data() at *tmp_path* and return that path.

    The legacy migration code reads from
    ``pathlib.Path(standarddir.data()) / 'workspaces'`` and
    ``... / 'profile' / 'pinned.yml'``; with this fixture in place,
    the tests can stage legacy files in *tmp_path* and watch the
    migration move them.
    """
    monkeypatch.setattr(standarddir, 'data', lambda: tmp_path)
    return tmp_path


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

    def test_base_path_creates_missing_parents(self, tmp_path):
        persistence = WorkspacePersistence(
            base_path=tmp_path / 'deep' / 'nested' / 'workspaces')
        assert persistence.base_path.exists()

    def test_load_all_skips_corrupt_files(self, ws_persistence, caplog):
        first = model.Workspace(id=uuid.uuid4(), name='a')
        second = model.Workspace(id=uuid.uuid4(), name='b')
        ws_persistence.save(first)
        ws_persistence.save(second)
        (ws_persistence.base_path / 'broken.yml').write_text(
            'id: [unclosed\n', encoding='utf-8')

        with caplog.at_level(logging.WARNING, 'message'):
            loaded = ws_persistence.load_all()
        assert {workspace.id for workspace in loaded} == {first.id, second.id}

    def test_load_all_skips_missing_id_file(self, ws_persistence, caplog):
        workspace = model.Workspace(id=uuid.uuid4(), name='a')
        ws_persistence.save(workspace)
        (ws_persistence.base_path / 'nodata.yml').write_text(
            'name: no id here\n', encoding='utf-8')

        with caplog.at_level(logging.WARNING, 'message'):
            loaded = ws_persistence.load_all()
        assert [ws.id for ws in loaded] == [workspace.id]

    def test_state_roundtrip(self, ws_persistence):
        workspace = model.Workspace(id=uuid.uuid4(), name='a')
        assert ws_persistence.load_state() is None
        ws_persistence.save_state(workspace.id)
        assert ws_persistence.load_state() == workspace.id
        ws_persistence.save_state(None)
        assert ws_persistence.load_state() is None

    def test_load_all_ignores_state_file(self, ws_persistence):
        workspace = model.Workspace(id=uuid.uuid4(), name='a')
        ws_persistence.save(workspace)
        ws_persistence.save_state(workspace.id)

        loaded = ws_persistence.load_all()
        assert [ws.id for ws in loaded] == [workspace.id]

    def test_load_state_invalid_id_is_none(self, ws_persistence, caplog):
        ws_persistence.save_state(uuid.uuid4())
        ws_persistence.state_path.write_text(
            'active_id: not-a-uuid\n', encoding='utf-8')
        with caplog.at_level(logging.WARNING, 'message'):
            assert ws_persistence.load_state() is None

    def test_default_profile_name(self, fake_data_dir):
        persistence = WorkspacePersistence()
        assert persistence.profile_name == DEFAULT_PROFILE_NAME
        assert persistence.base_path == (
            fake_data_dir / 'profiles' / DEFAULT_PROFILE_NAME / 'workspaces'
        )

    def test_named_profile_uses_profiles_subdir(self, tmp_path):
        # Override basedir so the test doesn't touch the real data dir.
        persistence = WorkspacePersistence(
            profile_name='work',
            base_path=tmp_path / 'profiles' / 'work' / 'workspaces',
        )
        assert persistence.profile_name == 'work'
        assert persistence.base_path == (
            tmp_path / 'profiles' / 'work' / 'workspaces'
        )


class TestPinnedPersistence:

    def test_save_load_roundtrip(self, pinned_persistence):
        pinned = [
            model.PinnedTab(
                tab_id=1, title='a', url='https://a.example',
                favicon=b'abc',
                last_active='2026-01-01T00:00:00+00:00'),
            model.PinnedTab(tab_id=2, title='b', url='https://b.example'),
        ]
        pinned_persistence.save(pinned)
        assert pinned_persistence.load() == pinned

    def test_missing_file_returns_empty(self, pinned_persistence):
        assert pinned_persistence.load() == []

    def test_parent_dir_auto_created(self, tmp_path):
        path = tmp_path / 'pinned' / 'nested' / 'pinned.yml'
        PinnedPersistence(path=path)
        assert path.parent.exists()

    def test_empty_list_roundtrip(self, pinned_persistence):
        pinned_persistence.save([])
        assert pinned_persistence.load() == []

    def test_default_path_under_profiles(self, tmp_path):
        # Don't actually call without an override — that would touch the
        # real data dir. Instead, simulate it by constructing a
        # PinnedPersistence against a fake data dir.
        from qutebrowser.workspaces import persistence
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(
                persistence.standarddir, 'data', lambda: tmp_path)
            p = PinnedPersistence()
        finally:
            monkeypatch.undo()
        assert p.path == (
            tmp_path / 'profiles' / DEFAULT_PROFILE_NAME
            / 'workspaces' / 'pinned.yml'
        )


class TestLegacyMigration:

    """One-shot migration from the pre-Profile layout.

    The legacy layout was::

        <data>/workspaces/<uuid>.yml
        <data>/workspaces/_state.yml
        <data>/profile/pinned.yml

    After this commit, both persistence classes route to
    ``<data>/profiles/default/workspaces/``; on first run they move any
    data found at the legacy locations into the new tree.
    """

    def test_workspaces_migrated_to_default_profile(
            self, fake_data_dir, caplog):
        legacy_dir = fake_data_dir / 'workspaces'
        legacy_dir.mkdir(parents=True)
        # Read each legacy file's content into a string before the
        # migration moves it out from under us.
        original_contents = {}
        for i, name in enumerate(['alpha', 'beta', 'gamma']):
            original_contents[name] = (
                f'id: 00000000-0000-0000-0000-00000000000{i}\n'
                f'name: {name}\n'
            )
            (legacy_dir / f'ws-{i}.yml').write_text(
                original_contents[name], encoding='utf-8',
            )
        original_state = (
            'active_id: 00000000-0000-0000-0000-000000000001\n'
        )
        (legacy_dir / '_state.yml').write_text(
            original_state, encoding='utf-8',
        )

        with caplog.at_level(logging.INFO, 'message'):
            WorkspacePersistence()

        new_dir = (
            fake_data_dir / 'profiles' / DEFAULT_PROFILE_NAME / 'workspaces'
        )
        assert (new_dir / 'ws-0.yml').read_text(
            encoding='utf-8') == original_contents['alpha']
        assert (new_dir / 'ws-1.yml').read_text(
            encoding='utf-8') == original_contents['beta']
        assert (new_dir / 'ws-2.yml').read_text(
            encoding='utf-8') == original_contents['gamma']
        assert (new_dir / '_state.yml').read_text(
            encoding='utf-8') == original_state
        assert not legacy_dir.exists()
        assert any('Migrated workspace data' in r.message
                   for r in caplog.records)

    def test_pinned_migrated_to_default_profile(
            self, fake_data_dir, caplog):
        legacy_pinned = fake_data_dir / 'profile' / 'pinned.yml'
        legacy_pinned.parent.mkdir(parents=True)
        legacy_pinned.write_text(
            'pinned:\n'
            '  - tab_id: 1\n'
            '    title: legacy\n'
            '    url: https://legacy.example\n',
            encoding='utf-8',
        )

        with caplog.at_level(logging.INFO, 'message'):
            persistence = PinnedPersistence()
            loaded = persistence.load()

        assert loaded == [
            model.PinnedTab(tab_id=1, title='legacy',
                            url='https://legacy.example'),
        ]
        assert not legacy_pinned.exists()
        assert persistence.path == (
            fake_data_dir / 'profiles' / DEFAULT_PROFILE_NAME
            / 'workspaces' / 'pinned.yml'
        )
        assert any('Migrated pinned tabs' in r.message
                   for r in caplog.records)

    def test_migration_is_idempotent(self, fake_data_dir):
        legacy_dir = fake_data_dir / 'workspaces'
        legacy_dir.mkdir(parents=True)
        (legacy_dir / 'ws-0.yml').write_text(
            'id: 00000000-0000-0000-0000-000000000000\n'
            'name: alpha\n',
            encoding='utf-8',
        )
        legacy_pinned = fake_data_dir / 'profile' / 'pinned.yml'
        legacy_pinned.parent.mkdir(parents=True)
        legacy_pinned.write_text(
            'pinned:\n  - {tab_id: 1, title: t, url: u}\n',
            encoding='utf-8',
        )

        # First construction: runs the migration.
        WorkspacePersistence()
        # Second construction with no legacy data: must be a clean no-op
        # (no exceptions, legacy dirs already gone, files unchanged).
        WorkspacePersistence()
        third = PinnedPersistence()

        new_dir = (
            fake_data_dir / 'profiles' / DEFAULT_PROFILE_NAME / 'workspaces'
        )
        assert (new_dir / 'ws-0.yml').exists()
        assert not legacy_dir.exists()
        assert not legacy_pinned.exists()
        # The new persistence should see the migrated data.
        assert third.load() == [
            model.PinnedTab(tab_id=1, title='t', url='u'),
        ]

    def test_migration_does_not_clobber_existing_new_file(
            self, fake_data_dir):
        # Stage a "new" file in the new layout first.
        new_dir = (
            fake_data_dir / 'profiles' / DEFAULT_PROFILE_NAME / 'workspaces'
        )
        new_dir.mkdir(parents=True)
        winner = new_dir / 'ws-0.yml'
        winner.write_text(
            'id: 00000000-0000-0000-0000-000000000000\n'
            'name: new-wins\n',
            encoding='utf-8',
        )
        # And a legacy file with the same name.
        legacy_dir = fake_data_dir / 'workspaces'
        legacy_dir.mkdir(parents=True)
        loser = legacy_dir / 'ws-0.yml'
        loser.write_text(
            'id: 00000000-0000-0000-0000-000000000000\n'
            'name: legacy-loses\n',
            encoding='utf-8',
        )

        WorkspacePersistence()

        # The new file is untouched; the legacy file is dropped silently.
        assert winner.read_text(encoding='utf-8') == (
            'id: 00000000-0000-0000-0000-000000000000\n'
            'name: new-wins\n'
        )
        assert not loser.exists()

    def test_non_default_profile_does_not_auto_migrate(
            self, fake_data_dir, tmp_path):
        # Stage legacy data.
        legacy_dir = fake_data_dir / 'workspaces'
        legacy_dir.mkdir(parents=True)
        (legacy_dir / 'ws-0.yml').write_text(
            'id: 00000000-0000-0000-0000-000000000000\n'
            'name: alpha\n',
            encoding='utf-8',
        )
        # Construct a non-default profile: it must NOT auto-migrate.
        persistence = WorkspacePersistence(
            profile_name='work',
            base_path=tmp_path / 'profiles' / 'work' / 'workspaces',
        )

        # Legacy data is still in place; the new profile's dir is empty.
        assert (legacy_dir / 'ws-0.yml').exists()
        assert persistence.base_path == (
            tmp_path / 'profiles' / 'work' / 'workspaces'
        )
        assert persistence.load_all() == []

    def test_legacy_pinned_dropped_when_new_already_exists(
            self, fake_data_dir):
        new_dir = (
            fake_data_dir / 'profiles' / DEFAULT_PROFILE_NAME / 'workspaces'
        )
        new_dir.mkdir(parents=True)
        winner = new_dir / 'pinned.yml'
        winner.write_text(
            'pinned:\n  - {tab_id: 9, title: new, url: u}\n',
            encoding='utf-8',
        )
        legacy_pinned = fake_data_dir / 'profile' / 'pinned.yml'
        legacy_pinned.parent.mkdir(parents=True)
        legacy_pinned.write_text(
            'pinned:\n  - {tab_id: 1, title: legacy, url: u}\n',
            encoding='utf-8',
        )

        PinnedPersistence()

        # New data wins, legacy file is dropped silently.
        assert PinnedPersistence().load() == [
            model.PinnedTab(tab_id=9, title='new', url='u'),
        ]
        assert not legacy_pinned.exists()
