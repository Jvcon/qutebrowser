# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Workspace switching and last-active-tab restore (issue #5)."""

import time
import uuid
from unittest import mock

import pytest

from qutebrowser.utils import utils
from qutebrowser.workspaces import commands
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import WorkspaceTab
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)


@pytest.fixture
def pers(tmp_path):
    """Shared real persistence rooted in a temporary directory."""
    return (
        WorkspacePersistence(base_path=tmp_path / 'workspaces'),
        PinnedPersistence(path=tmp_path / 'profile' / 'pinned.yml'),
    )


@pytest.fixture
def manager(pers, qapp):
    """An empty WorkspaceManager backed by temporary persistence."""
    return WorkspaceManager(*pers)


class MemoryPersistence:

    """In-memory stand-in for :class:`WorkspacePersistence`.

    Used by the hot-switch benchmark so the measured operation contains no
    disk I/O (the budget in issue #5 is defined for the in-memory path).
    """

    profile_name = 'default'

    def __init__(self) -> None:
        self.state: tuple = (None, {})

    def load(self, workspace_id):
        raise FileNotFoundError(workspace_id)

    def load_all(self):
        return []

    def save(self, workspace):
        pass

    def delete(self, workspace_id):
        pass

    def load_state(self):
        return self.state[0]

    def load_state_data(self):
        return self.state

    def save_state(self, active_id, last_active_tabs=None):
        self.state = (active_id, dict(last_active_tabs or {}))


class TestSetActive:

    def test_set_active_stashes_previous_workspace_tab(self, manager):
        """A -> B -> A restores both workspaces' last-active tab ids."""
        first = manager.add_workspace('a')
        second = manager.add_workspace('b')
        manager.add_tab(first.id, WorkspaceTab(tab_id=1))
        manager.add_tab(second.id, WorkspaceTab(tab_id=2))

        emitted = []
        manager.active_workspace_changed.connect(
            lambda workspace_id, tab_id: emitted.append(
                (workspace_id, tab_id)))

        # Focus tab 1 in A, then switch away. B has no recorded tab yet.
        manager.record_active_tab(1, first.id)
        manager.set_active(second.id)
        assert emitted[-1] == (second.id, None)

        # Focus tab 2 in B, then switch back to A.
        manager.record_active_tab(2, second.id)
        manager.set_active(first.id)

        assert emitted[-1] == (first.id, 1)
        assert manager.tab_id_for_workspace(first.id) == 1
        assert manager.tab_id_for_workspace(second.id) == 2

    def test_set_active_same_workspace_is_noop(self, manager):
        workspace = manager.add_workspace('a')
        emitted = []
        changed = []
        manager.active_workspace_changed.connect(
            lambda *args: emitted.append(args))
        manager.changed.connect(lambda: changed.append(True))
        last_active = workspace.last_active

        manager.set_active(workspace.id)

        assert emitted == []
        assert changed == []
        assert workspace.last_active == last_active

    def test_set_active_loads_cold_workspace(self, pers, qapp):
        """Switching to an on-disk-only workspace loads it first."""
        first = WorkspaceManager(*pers)
        first.add_workspace('active')
        cold = first.add_workspace('cold')
        first.add_tab(cold.id, WorkspaceTab(tab_id=9))
        first.save()

        manager = WorkspaceManager(*pers)
        manager.load()
        active = manager.get_active()
        assert active is not None
        assert active.id != cold.id

        manager.set_active(cold.id)

        active = manager.get_active()
        assert active is not None
        assert active.id == cold.id
        assert manager.tab_id_for_workspace(cold.id) is None

    def test_record_active_tab_does_not_emit(self, manager):
        workspace = manager.add_workspace('a')
        changed = []
        manager.changed.connect(lambda: changed.append(True))

        manager.record_active_tab(4, workspace.id)

        assert changed == []
        assert manager.tab_id_for_workspace(workspace.id) == 4


class TestStatePersistence:

    def test_load_state_tolerates_legacy_active_id_only(
            self, pers, qapp):
        """A pre-change _state.yml (active_id only) loads cleanly."""
        manager = WorkspaceManager(*pers)
        workspace = manager.add_workspace('legacy')
        manager.save()

        # Rewrite the state file in the legacy one-key shape.
        pers[0].state_path.write_text(
            f'active_id: {workspace.id}\n', encoding='utf-8')

        reloaded = WorkspaceManager(*pers)
        reloaded.load()

        active = reloaded.get_active()
        assert active is not None
        assert active.id == workspace.id
        assert reloaded.tab_id_for_workspace(workspace.id) is None

    def test_save_state_round_trip_with_last_active_tabs(
            self, pers, qapp):
        manager = WorkspaceManager(*pers)
        first = manager.add_workspace('a')
        second = manager.add_workspace('b')
        manager.record_active_tab(11, first.id)
        manager.record_active_tab(22, second.id)
        manager.set_active(second.id)
        manager.save()

        with pers[0].state_path.open('r', encoding='utf-8') as f:
            raw = utils.yaml_load(f)
        assert raw['active_id'] == str(second.id)
        assert raw['last_active_tabs'] == {
            str(first.id): 11,
            str(second.id): 22,
        }

        reloaded = WorkspaceManager(*pers)
        reloaded.load()
        active = reloaded.get_active()
        assert active is not None
        assert active.id == second.id
        assert reloaded.tab_id_for_workspace(first.id) == 11
        assert reloaded.tab_id_for_workspace(second.id) == 22

    def test_save_state_accepts_explicit_tab_map(self, pers):
        persistence = pers[0]
        first = uuid.uuid4()
        second = uuid.uuid4()

        persistence.save_state(
            first, {first: 3, second: None})

        active_id, tabs = persistence.load_state_data()
        assert active_id == first
        assert tabs == {first: 3, second: None}
        # The single-value accessor remains compatible.
        assert persistence.load_state() == first

    def test_load_state_ignores_malformed_tab_entries(self, pers, caplog):
        persistence = pers[0]
        workspace = uuid.uuid4()
        bad_value = uuid.uuid4()
        persistence.state_path.write_text(
            f'active_id: {workspace}\n'
            'last_active_tabs:\n'
            '  not-a-uuid: 1\n'
            f'  {bad_value}: not-an-int\n'
            f'  {workspace}: 5\n',
            encoding='utf-8',
        )

        with caplog.at_level('WARNING', 'message'):
            active_id, tabs = persistence.load_state_data()

        assert active_id == workspace
        assert tabs == {workspace: 5}


class TestWorkspaceSwitchCommand:

    def test_workspace_switch_command_short_circuits_same_workspace(
            self, manager, monkeypatch):
        first = manager.add_workspace('a')
        manager.add_workspace('b')
        manager.set_active(first.id)
        messages = []
        monkeypatch.setattr(commands, '_manager', lambda: manager)
        monkeypatch.setattr(
            commands.message, 'info',
            lambda text, **_kwargs: messages.append(text))
        emitted = []
        manager.active_workspace_changed.connect(
            lambda *args: emitted.append(args))
        last_active = first.last_active

        commands.workspace_switch('a')

        assert messages == ["Already on workspace 'a'."]
        assert emitted == []
        assert first.last_active == last_active


class TestColdRestart:

    def test_cold_restart_restores_active_workspace(self, pers, qapp):
        first = WorkspaceManager(*pers)
        first.add_workspace('a')
        target = first.add_workspace('b')
        first.add_tab(target.id, WorkspaceTab(tab_id=7))
        first.set_active(target.id)
        first.record_active_tab(7, target.id)
        first.save()

        # Simulate a new process: a fresh manager reads _state.yml.
        reloaded = WorkspaceManager(*pers)
        reloaded.load()

        active = reloaded.get_active()
        assert active is not None
        assert active.id == target.id
        assert reloaded.tab_id_for_workspace(target.id) == 7


class TestSwitchPerformance:

    def test_hot_switch_under_50ms(self, qapp):
        """The already-loaded switch path stays under 50 ms."""
        persistence = MemoryPersistence()
        pinned = mock.Mock()
        pinned.profile_name = 'default'
        manager = WorkspaceManager(
            persistence, pinned)  # type: ignore[arg-type]
        first = manager.add_workspace('a')
        second = manager.add_workspace('b')
        manager.add_tab(first.id, WorkspaceTab(tab_id=1))
        manager.add_tab(second.id, WorkspaceTab(tab_id=2))
        manager.record_active_tab(1, first.id)
        manager.record_active_tab(2, second.id)
        manager.set_active(second.id)
        manager.set_active(first.id)

        start = time.perf_counter()
        manager.set_active(second.id)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.050, f"hot switch took {elapsed * 1000:.1f} ms"

    def test_cold_switch_under_200ms(self, pers, qapp):
        """A switch which lazy-loads the target workspace stays < 200 ms."""
        first = WorkspaceManager(*pers)
        first.add_workspace('a')
        target = first.add_workspace('b')
        first.add_tab(target.id, WorkspaceTab(tab_id=2))
        first.save()

        manager = WorkspaceManager(*pers)
        manager.load()
        active = manager.get_active()
        assert active is not None
        assert active.id != target.id

        start = time.perf_counter()
        manager.set_active(target.id)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.200, f"cold switch took {elapsed * 1000:.1f} ms"
