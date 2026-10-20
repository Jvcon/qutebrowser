# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the vertical panel's WorkspaceIndicator (Phase 2)."""

import uuid

import pytest

from qutebrowser.qt.core import QObject, pyqtSignal

from qutebrowser.mainwindow import workspaceindicator
from qutebrowser.workspaces.model import Workspace


class FakeManager(QObject):

    """A minimal QObject manager stand-in with the API the widget needs."""

    changed = pyqtSignal()

    def __init__(self, workspaces: list[Workspace]) -> None:
        super().__init__()
        self._workspaces = {ws.id: ws for ws in workspaces}
        self._active_id = next(
            (ws.id for ws in workspaces if not ws.archived), None)
        self.set_active_calls: list[uuid.UUID] = []
        self.archived: list[uuid.UUID] = []
        self.renamed: list[tuple[uuid.UUID, str]] = []
        self.deleted: list[uuid.UUID] = []

    def get_active(self) -> Workspace | None:
        return self._workspaces.get(self._active_id)

    def list_all_workspaces(self) -> list[Workspace]:
        return list(self._workspaces.values())

    def set_active(self, workspace_id: uuid.UUID) -> None:
        self._active_id = workspace_id
        self.set_active_calls.append(workspace_id)
        self.changed.emit()

    def archive_workspace(self, workspace_id: uuid.UUID) -> None:
        self._workspaces[workspace_id].archived = True
        self.archived.append(workspace_id)
        self.changed.emit()

    def rename_workspace(self, workspace_id: uuid.UUID,
                         new_name: str) -> None:
        self._workspaces[workspace_id].name = new_name
        self.renamed.append((workspace_id, new_name))
        self.changed.emit()

    def delete_workspace(self, workspace_id: uuid.UUID) -> None:
        del self._workspaces[workspace_id]
        self.deleted.append(workspace_id)
        self.changed.emit()


def _make(name: str, archived: bool = False) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, archived=archived)


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager([_make('alpha'), _make('beta'), _make('gamma', True)])


@pytest.fixture
def indicator(qtbot, config_stub, manager: FakeManager) -> (
        workspaceindicator.WorkspaceIndicator):
    widget = workspaceindicator.WorkspaceIndicator(win_id=0, manager=manager)
    qtbot.add_widget(widget)
    return widget


class TestMonogram:

    def test_valid_pixmap(self, qtbot) -> None:
        pixmap = workspaceindicator.monogram_pixmap('alpha')
        assert not pixmap.isNull()
        assert pixmap.width() == 16
        assert pixmap.height() == 16

    def test_deterministic_for_same_name(self, qtbot) -> None:
        assert (workspaceindicator.monogram_pixmap('alpha').toImage()
                == workspaceindicator.monogram_pixmap('alpha').toImage())

    def test_different_names_differ(self, qtbot) -> None:
        assert (workspaceindicator.monogram_pixmap('alpha').toImage()
                != workspaceindicator.monogram_pixmap('beta').toImage())

    def test_color_override_accepted(self, qtbot) -> None:
        assert not workspaceindicator.monogram_pixmap(
            'alpha', color='#ff0000').isNull()


class TestWorkspaceIndicator:

    def test_button_per_non_archived_workspace(self, indicator) -> None:
        # gamma is archived and must be excluded.
        assert len(indicator.buttons()) == 2

    def test_active_button_checked(self, indicator, manager) -> None:
        active = manager.get_active()
        assert active is not None
        assert indicator.buttons()[active.id].isChecked()

    def test_inactive_button_not_checked(self, indicator, manager) -> None:
        beta = next(ws for ws in manager.list_all_workspaces()
                    if ws.name == 'beta')
        assert not indicator.buttons()[beta.id].isChecked()

    def test_click_switches_workspace(self, indicator, manager) -> None:
        beta = next(ws for ws in manager.list_all_workspaces()
                    if ws.name == 'beta')
        indicator.buttons()[beta.id].click()
        assert beta.id in manager.set_active_calls

    def test_click_on_active_is_noop(self, indicator, manager) -> None:
        active = manager.get_active()
        assert active is not None
        manager.set_active_calls.clear()
        indicator.buttons()[active.id].click()
        assert manager.set_active_calls == []

    def test_context_menu_has_three_actions(self, indicator, manager) -> None:
        ws = next(ws for ws in manager.list_all_workspaces()
                  if ws.name == 'alpha')
        menu = indicator._build_context_menu(ws.id)
        assert [action.text() for action in menu.actions()] == [
            'Rename', 'Archive', 'Delete']

    def test_archive_action(self, indicator, manager) -> None:
        ws = next(ws for ws in manager.list_all_workspaces()
                  if ws.name == 'alpha')
        menu = indicator._build_context_menu(ws.id)
        next(action for action in menu.actions()
             if action.text() == 'Archive').trigger()
        assert ws.id in manager.archived

    def test_rename_action_prompts(self, indicator, manager, monkeypatch) -> None:
        ws = next(ws for ws in manager.list_all_workspaces()
                  if ws.name == 'alpha')
        captured = {}
        monkeypatch.setattr(
            workspaceindicator.message, 'ask_async',
            lambda _title, _mode, callback, **_kw: captured.setdefault(
                'cb', callback))
        menu = indicator._build_context_menu(ws.id)
        next(action for action in menu.actions()
             if action.text() == 'Rename').trigger()
        captured['cb']('renamed')
        assert ws.id in [wid for wid, _name in manager.renamed]

    def test_delete_action_confirms(self, indicator, manager, monkeypatch) -> None:
        ws = next(ws for ws in manager.list_all_workspaces()
                  if ws.name == 'alpha')
        captured = {}
        monkeypatch.setattr(
            workspaceindicator.message, 'confirm_async',
            lambda **kwargs: captured.update(kwargs))
        menu = indicator._build_context_menu(ws.id)
        next(action for action in menu.actions()
             if action.text() == 'Delete').trigger()
        assert 'yes_action' in captured
        captured['yes_action']()
        assert ws.id in manager.deleted
