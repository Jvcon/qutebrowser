# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the crash journal (issue #7).

A SIGKILL is modelled in-process: a manager writes its journal and is then
simply abandoned (no ``shutdown()``), and a fresh manager built over the same
directory plays the role of the next launch.  No real processes are spawned.
"""

import json
import logging
import uuid

import pytest
import yaml

from qutebrowser.workspaces import manager as manager_module
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import PinnedTab, WorkspaceTab
from qutebrowser.workspaces.persistence import (JOURNAL_FILENAME,
                                                JournalError,
                                                JournalPersistence,
                                                JournalSnapshot,
                                                PinnedPersistence,
                                                WorkspacePersistence,
                                                _checksum, decode_journal,
                                                encode_journal)


class CountingJournal(JournalPersistence):

    """A journal that records how many times it was written."""

    def __init__(self, base_path):
        super().__init__(base_path)
        self.writes = 0

    def write(self, snapshot):
        self.writes += 1
        super().write(snapshot)


class Harness:

    """One "process": a manager over the shared on-disk directory."""

    def __init__(self, root):
        self.root = root
        self.base = root / 'workspaces'
        self.pinned_path = root / 'profile' / 'pinned.yml'
        self.journal = CountingJournal(self.base)

    def manager(self):
        """Build and load a manager, as the app does at startup."""
        manager = WorkspaceManager(
            WorkspacePersistence(base_path=self.base),
            PinnedPersistence(path=self.pinned_path),
            journal=self.journal,
        )
        manager.load()
        return manager


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def journal_path(harness):
    return harness.base / JOURNAL_FILENAME


@pytest.fixture(autouse=True)
def allow_warnings(caplog):
    """Let the expected warnings through the log guard.

    A SIGKILL before the first YAML save leaves ``_state.yml`` naming a
    workspace that only the journal knows about, so "could not load active
    workspace" is the correct, expected outcome in these tests.
    """
    for name in ('message', 'workspaces'):
        caplog.set_level(logging.WARNING, logger=name)


def _populate(manager):
    """Build a parent/child + grouped topology and return its ids."""
    ws = manager.add_workspace('work')
    group = manager.create_group(ws.id, 'research')
    manager.add_tab(ws.id, WorkspaceTab(
        tab_id=1, url='https://a.example/', title='A', open_status='OPEN'))
    manager.add_tab(ws.id, WorkspaceTab(
        tab_id=2, url='https://b.example/', title='B', open_status='OPEN'))
    manager.add_tab(ws.id, WorkspaceTab(
        tab_id=3, url='https://c.example/', title='C', open_status='OPEN'))
    manager.set_parent(ws.id, 2, 1)
    manager.set_parent(ws.id, 3, 2)
    manager.move_tab_to_group(ws.id, 2, group.group_id)
    manager.add_pinned(PinnedTab(tab_id=9, title='P', url='https://p.example/'))
    manager.record_active_tab(3, ws.id)
    return ws.id, group.group_id


class TestJournalRoundTrip:

    def test_crash_then_recover_restores_topology(self, harness, qtbot):
        first = harness.manager()
        ws_id, group_id = _populate(first)
        assert first.flush_journal(force=True)
        # SIGKILL: the first manager is abandoned without shutdown().

        second = harness.manager()
        snapshot = second.pending_recovery
        assert isinstance(snapshot, JournalSnapshot)
        assert snapshot.tab_count == 3

        summary = second.apply_recovery(snapshot)
        assert (summary.workspaces, summary.tabs) == (1, 3)

        ws = second.get_workspace(ws_id)
        by_url = {t.url: t for t in ws.tabs}
        a = by_url['https://a.example/']
        b = by_url['https://b.example/']
        c = by_url['https://c.example/']
        assert b.parent_id == a.tab_id
        assert c.parent_id == b.tab_id
        assert b.group_id == group_id
        assert second.get_active().id == ws_id
        assert second.tab_id_for_workspace(ws_id) == c.tab_id
        assert [p.tab_id for p in second.list_pinned()] == [9]

    def test_recovered_entries_are_suspended_with_unique_negative_ids(
            self, harness, qtbot):
        first = harness.manager()
        ws_id, _ = _populate(first)
        first.flush_journal(force=True)

        second = harness.manager()
        second.apply_recovery(second.pending_recovery)
        ws = second.get_workspace(ws_id)
        ids = [t.tab_id for t in ws.tabs]
        assert all(i < 0 for i in ids)
        assert len(set(ids)) == len(ids)
        assert {t.open_status for t in ws.tabs} == {'SUSPENDED'}

    def test_journal_is_checksummed_yaml_envelope(self, harness, qtbot,
                                                  journal_path):
        manager = harness.manager()
        _populate(manager)
        manager.flush_journal(force=True)

        data = yaml.safe_load(journal_path.read_text(encoding='utf-8'))
        assert set(data) == {'checksum', 'payload'}
        assert data['payload']['format'] == 1
        assert len(data['payload']['workspaces']) == 1

    def test_round_trip_via_encode_decode(self):
        ws_id = uuid.uuid4()
        snapshot = JournalSnapshot(
            written_at='2026-01-01T00:00:00+00:00',
            active_id=ws_id,
            last_active_tabs={ws_id: 4},
            workspaces=[],
            pinned=[PinnedTab(tab_id=1, title='t', url='u')],
        )
        decoded = decode_journal(json.loads(json.dumps(encode_journal(snapshot))))
        assert decoded.active_id == ws_id
        assert decoded.last_active_tabs == {ws_id: 4}
        assert decoded.pinned == snapshot.pinned


class TestTornWrite:

    def _write_valid(self, harness):
        manager = harness.manager()
        _populate(manager)
        manager.flush_journal(force=True)

    def test_truncated_journal_is_ignored_and_kept(self, harness, qtbot,
                                                   journal_path, caplog):
        self._write_valid(harness)
        raw = journal_path.read_bytes()
        journal_path.write_bytes(raw[:len(raw) // 2])

        manager = harness.manager()

        assert manager.pending_recovery is None
        assert not journal_path.exists()
        kept = list(harness.base.glob('_journal.corrupt-*.yml'))
        assert len(kept) == 1
        assert any('crash journal' in r.getMessage() for r in caplog.records)

    def test_edited_payload_fails_checksum(self, harness, qtbot, journal_path):
        self._write_valid(harness)
        data = yaml.safe_load(journal_path.read_text(encoding='utf-8'))
        data['payload']['workspaces'][0]['name'] = 'tampered'
        journal_path.write_text(yaml.safe_dump(data), encoding='utf-8')

        assert harness.manager().pending_recovery is None

    @pytest.mark.parametrize('content', ['', '- 1\n- 2\n', 'checksum: 1\n',
                                         '{{{ not yaml'])
    def test_malformed_journal_never_bricks_startup(
            self, harness, qtbot, journal_path, content):
        harness.base.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(content, encoding='utf-8')

        manager = harness.manager()
        assert manager.pending_recovery is None

    def test_unknown_format_is_rejected(self):
        snapshot = JournalSnapshot(
            written_at='', active_id=None, last_active_tabs={},
            workspaces=[], pinned=[])
        envelope = encode_journal(snapshot)
        envelope['payload']['format'] = 99
        # Re-checksum so only the format check can reject it.
        envelope['checksum'] = _checksum(envelope['payload'])
        with pytest.raises(JournalError, match='format'):
            decode_journal(envelope)


class TestBatching:

    def test_burst_of_changes_collapses_to_one_write(self, harness, qtbot):
        manager = harness.manager()
        for i in range(50):
            manager.add_workspace(f'ws{i}')
        # Nothing is written inside the batch window.
        assert harness.journal.writes == 0

        qtbot.waitUntil(lambda: harness.journal.writes >= 1, timeout=5000)
        qtbot.wait(manager_module.JOURNAL_BATCH_MS + 200)
        assert harness.journal.writes == 1

    def test_explicit_flush_writes_once_for_many_changes(self, harness, qtbot):
        manager = harness.manager()
        for i in range(20):
            manager.add_workspace(f'ws{i}')
        assert manager.flush_journal() is True
        assert manager.flush_journal() is False
        assert harness.journal.writes == 1

    def test_saveable_cycle_flushes_journal(self, harness, qtbot):
        manager = harness.manager()
        manager.add_workspace('w')
        manager.save()
        assert harness.journal.writes == 1
        assert manager.flush_journal() is False

    def test_no_changes_no_write(self, harness, qtbot):
        manager = harness.manager()
        assert manager.flush_journal() is False
        assert harness.journal.writes == 0

    def test_manager_without_journal_never_writes(self, harness, qtbot):
        manager = WorkspaceManager(
            WorkspacePersistence(base_path=harness.base),
            PinnedPersistence(path=harness.pinned_path))
        manager.add_workspace('w')
        assert manager.flush_journal(force=True) is False
        assert manager.pending_recovery is None


class TestDeclinePath:

    def test_decline_keeps_yaml_and_preserves_journal(self, harness, qtbot,
                                                      journal_path):
        first = harness.manager()
        ws_id, _ = _populate(first)
        first.save()                       # YAML on disk: 3 tabs
        first.add_tab(ws_id, WorkspaceTab(  # journal-only change: 4th tab
            tab_id=4, url='https://d.example/', open_status='OPEN'))
        first.flush_journal(force=True)    # SIGKILL here

        second = harness.manager()
        assert second.pending_recovery is not None
        second.decline_recovery()

        assert second.pending_recovery is None
        preserved = list(harness.base.glob('_journal.declined-*.yml'))
        assert len(preserved) == 1
        assert not any(t.url == 'https://d.example/'
                       for t in harness.manager().get_workspace(ws_id).tabs)
        # The on-disk workspace YAML still holds the saved 3 tabs.
        yaml_data = yaml.safe_load(
            (harness.base / f'{ws_id}.yml').read_text(encoding='utf-8'))
        assert len(yaml_data['tabs']) == 3

    def test_decline_writes_fresh_journal_for_next_crash(
            self, harness, qtbot, journal_path):
        first = harness.manager()
        _populate(first)
        first.flush_journal(force=True)

        second = harness.manager()
        second.decline_recovery()
        qtbot.waitUntil(journal_path.exists, timeout=5000)

        third = harness.manager()
        assert third.pending_recovery is not None   # the new run's state

    def test_pending_recovery_blocks_journal_overwrite(
            self, harness, qtbot, journal_path):
        first = harness.manager()
        _populate(first)
        first.flush_journal(force=True)
        before = journal_path.read_bytes()

        second = harness.manager()
        second.add_workspace('new work while the prompt is open')
        assert second.flush_journal(force=True) is False
        assert journal_path.read_bytes() == before

    def test_offer_is_handed_out_once(self, harness, qtbot):
        first = harness.manager()
        _populate(first)
        first.flush_journal(force=True)

        second = harness.manager()
        assert second.take_recovery_offer() is not None
        assert second.take_recovery_offer() is None
        assert second.pending_recovery is not None

    def test_no_journal_means_no_offer(self, harness, qtbot):
        assert harness.manager().take_recovery_offer() is None


class TestRecoveryApply:

    def test_apply_emits_changed_once(self, harness, qtbot):
        first = harness.manager()
        _populate(first)
        first.flush_journal(force=True)

        second = harness.manager()
        emitted = []
        second.changed.connect(lambda: emitted.append(True))
        second.apply_recovery(second.pending_recovery)
        assert emitted == [True]

    def test_apply_persists_and_clears_journal_pending_state(
            self, harness, qtbot):
        first = harness.manager()
        ws_id, _ = _populate(first)
        first.flush_journal(force=True)

        second = harness.manager()
        second.apply_recovery(second.pending_recovery)
        assert (harness.base / f'{ws_id}.yml').exists()

        third = harness.manager()
        assert third.pending_recovery is not None  # rewritten by the apply
        assert third.pending_recovery.tab_count == 3

    def test_clean_shutdown_removes_journal(self, harness, qtbot, journal_path):
        manager = harness.manager()
        _populate(manager)
        manager.flush_journal(force=True)
        assert journal_path.exists()

        manager.shutdown()
        assert not journal_path.exists()
        assert harness.manager().pending_recovery is None

    def test_changes_after_shutdown_do_not_recreate_journal(
            self, harness, qtbot, journal_path):
        manager = harness.manager()
        _populate(manager)
        manager.shutdown()

        manager.add_workspace('late')    # e.g. tabs closing during quit
        qtbot.wait(manager_module.JOURNAL_BATCH_MS + 200)
        assert not journal_path.exists()

    def test_failed_save_on_shutdown_keeps_journal(
            self, harness, qtbot, journal_path, monkeypatch, caplog):
        manager = harness.manager()
        _populate(manager)
        manager.flush_journal(force=True)

        def boom(_workspace):
            raise OSError('disk full')

        monkeypatch.setattr(manager._persistence, 'save', boom)
        with caplog.at_level(logging.ERROR, 'workspaces'):
            manager.shutdown()
        assert journal_path.exists()


class TestCorruptInputDoesNotBrick:

    def test_corrupt_state_file_starts_normally(self, harness, qtbot):
        harness.base.mkdir(parents=True)
        (harness.base / '_state.yml').write_text('{{{: [', encoding='utf-8')

        manager = harness.manager()
        assert manager.get_active() is None
        assert manager.list_workspaces() == []

    def test_corrupt_workspace_file_is_skipped(self, harness, qtbot):
        harness.base.mkdir(parents=True)
        (harness.base / f'{uuid.uuid4()}.yml').write_text(
            '{{{: [', encoding='utf-8')
        (harness.base / f'{uuid.uuid4()}.yml').write_text(
            '- 1\n- 2\n', encoding='utf-8')
        manager = harness.manager()
        assert manager.list_all_workspaces() == []

    def test_corrupt_pinned_file_starts_normally(self, harness, qtbot):
        harness.pinned_path.parent.mkdir(parents=True)
        harness.pinned_path.write_text('{{{: [', encoding='utf-8')
        manager = harness.manager()
        assert manager.list_pinned() == []

    def test_recovery_repairs_corrupt_workspace_yaml(self, harness, qtbot):
        first = harness.manager()
        ws_id, _ = _populate(first)
        first.flush_journal(force=True)
        (harness.base / f'{ws_id}.yml').write_text('{{{: [', encoding='utf-8')

        second = harness.manager()
        second.apply_recovery(second.pending_recovery)
        restored = harness.manager().get_workspace(ws_id)
        assert restored is not None
        assert len(restored.tabs) == 3

    def test_journal_file_is_not_loaded_as_a_workspace(self, harness, qtbot):
        first = harness.manager()
        _populate(first)
        first.save()                      # one workspace YAML on disk
        first.flush_journal(force=True)   # plus the journal beside it
        assert (harness.base / JOURNAL_FILENAME).exists()

        loaded = WorkspacePersistence(base_path=harness.base).load_all()
        assert [ws.name for ws in loaded] == ['work']
