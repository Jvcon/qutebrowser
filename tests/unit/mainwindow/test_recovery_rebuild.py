# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the crash-recovery UI path (issue #7).

Covers the tree's bulk-rebuild (no animation, selection kept) and the bridge's
recovery prompt: offered once per launch, accept applies the journal, decline
keeps it on disk.  No real browser or process is involved.
"""

from unittest import mock

import logging

import pytest

from qutebrowser.mainwindow import tabtree, workspacebridge
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import WorkspaceTab
from qutebrowser.workspaces.persistence import (JOURNAL_FILENAME,
                                                JournalPersistence,
                                                PinnedPersistence,
                                                WorkspacePersistence)


def _manager(root, *, journal=True):
    return WorkspaceManager(
        WorkspacePersistence(base_path=root / 'workspaces'),
        PinnedPersistence(path=root / 'profile' / 'pinned.yml'),
        journal=(JournalPersistence(root / 'workspaces') if journal else None),
    )


def _seed(manager):
    ws = manager.add_workspace('work')
    for tab_id, url in ((1, 'https://a.example/'), (2, 'https://b.example/'),
                        (3, 'https://c.example/')):
        manager.add_tab(ws.id, WorkspaceTab(
            tab_id=tab_id, url=url, open_status='OPEN'))
    manager.set_parent(ws.id, 2, 1)
    return ws.id


@pytest.fixture
def tree_manager(tmp_path):
    manager = _manager(tmp_path, journal=False)
    manager.load()
    return manager


@pytest.fixture(autouse=True)
def _allow_recovery_warnings(caplog):
    """Expected warnings in these tests go through the log guard."""
    for name in ('message', 'workspaces'):
        caplog.set_level(logging.WARNING, logger=name)


class TestBulkRebuild:

    def test_animation_is_suppressed_and_restored(self, qtbot, tree_manager):
        _seed(tree_manager)
        tree = tabtree.TabTree(win_id=0, manager=tree_manager)
        qtbot.add_widget(tree)
        view = tree.view()
        assert view.isAnimated()

        with tree.bulk_rebuild():
            assert not view.isAnimated()
            tree_manager.add_tab(tree_manager.get_active().id, WorkspaceTab(
                tab_id=4, url='https://d.example/', open_status='OPEN'))

        assert view.isAnimated()

    def test_selection_survives_rebuild_inside_block(self, qtbot, tree_manager):
        ws_id = _seed(tree_manager)
        tree = tabtree.TabTree(win_id=0, manager=tree_manager)
        qtbot.add_widget(tree)
        model = tree.model()
        tree.view().setCurrentIndex(model.index_for_tab_id(3))

        with tree.bulk_rebuild():
            tree_manager.add_tab(ws_id, WorkspaceTab(
                tab_id=4, url='https://d.example/', open_status='OPEN'))

        current = tree.view().currentIndex()
        assert model.tab_id_for_index(current) == 3

    def test_selection_falls_back_to_valid_row_when_tab_is_gone(
            self, qtbot, tree_manager):
        ws_id = _seed(tree_manager)
        # Rows are [1 (with child 2), 3, 4]; the selection sits on tab 3.
        tree_manager.add_tab(ws_id, WorkspaceTab(
            tab_id=4, url='https://d.example/', open_status='OPEN'))
        tree = tabtree.TabTree(win_id=0, manager=tree_manager)
        qtbot.add_widget(tree)
        model = tree.model()
        tree.view().setCurrentIndex(model.index_for_tab_id(3))

        with tree.bulk_rebuild():
            tree_manager.remove_tab(ws_id, 3)

        # Tab 3 is gone, but its row index is still valid and now holds tab 4.
        current = tree.view().currentIndex()
        assert model.tab_id_for_index(current) == 4

    def test_no_selection_stays_empty(self, qtbot, tree_manager):
        _seed(tree_manager)
        tree = tabtree.TabTree(win_id=0, manager=tree_manager)
        qtbot.add_widget(tree)
        tree.view().setCurrentIndex(tabtree.QModelIndex())

        with tree.bulk_rebuild():
            pass

        assert not tree.view().currentIndex().isValid()


class TestRecoveryPrompt:

    @pytest.fixture
    def crashed_root(self, tmp_path):
        """A journal left behind by a manager that was never shut down."""
        first = _manager(tmp_path)
        first.load()
        ws_id = _seed(first)
        first.flush_journal(force=True)
        return tmp_path, ws_id

    @pytest.fixture
    def capture(self, monkeypatch):
        """Record the prompt and info messages instead of showing them."""
        calls = {'confirm': [], 'info': []}

        def fake_confirm(**kwargs):
            calls['confirm'].append(kwargs)

        monkeypatch.setattr(workspacebridge.message, 'confirm_async',
                            fake_confirm)
        monkeypatch.setattr(workspacebridge.message, 'info',
                            calls['info'].append)
        return calls

    @staticmethod
    def _bridge(manager):
        browser = mock.Mock()
        browser.widget.count.return_value = 0
        model = tabtree.TabTreeModel(manager)
        return workspacebridge.WorkspaceBridge(browser, manager, model)

    def test_accept_applies_journal_and_announces(
            self, qtbot, crashed_root, capture):
        root, ws_id = crashed_root
        manager = _manager(root)
        manager.load()
        bridge = self._bridge(manager)

        bridge._offer_recovery()
        assert len(capture['confirm']) == 1
        capture['confirm'][0]['yes_action']()

        assert manager.pending_recovery is None
        ws = manager.get_workspace(ws_id)
        assert len(ws.tabs) == 3
        assert {t.open_status for t in ws.tabs} == {'SUSPENDED'}
        assert len(capture['info']) == 1
        assert 'Recovered 3 tab(s)' in capture['info'][0]

    def test_decline_keeps_journal_and_announces(
            self, qtbot, crashed_root, capture):
        root, ws_id = crashed_root
        manager = _manager(root)
        manager.load()
        bridge = self._bridge(manager)

        bridge._offer_recovery()
        capture['confirm'][0]['no_action']()

        assert manager.pending_recovery is None
        assert list((root / 'workspaces').glob('_journal.declined-*.yml'))
        assert 'skipped' in capture['info'][0]

    def test_cancel_counts_as_decline(self, qtbot, crashed_root, capture):
        root, _ws_id = crashed_root
        manager = _manager(root)
        manager.load()
        bridge = self._bridge(manager)

        bridge._offer_recovery()
        capture['confirm'][0]['cancel_action']()

        assert manager.pending_recovery is None
        assert not (root / 'workspaces' / JOURNAL_FILENAME).exists()

    def test_prompt_is_offered_once_per_launch(
            self, qtbot, crashed_root, capture):
        root, _ws_id = crashed_root
        manager = _manager(root)
        manager.load()
        first = self._bridge(manager)
        second = self._bridge(manager)

        first._offer_recovery()
        second._offer_recovery()

        assert len(capture['confirm']) == 1

    def test_no_journal_no_prompt(self, qtbot, tmp_path, capture):
        manager = _manager(tmp_path)
        manager.load()
        bridge = self._bridge(manager)

        bridge._offer_recovery()
        assert capture['confirm'] == []

    def test_inert_bridge_never_prompts(self, qtbot, crashed_root, capture):
        root, _ws_id = crashed_root
        manager = _manager(root)
        manager.load()
        browser = mock.Mock()
        bridge = workspacebridge.WorkspaceBridge(browser, None, None)

        bridge._offer_recovery()
        assert capture['confirm'] == []
        assert manager.pending_recovery is not None
