# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the vertical panel's WorkspaceIndicator (Phase 2)."""

import uuid

import pytest

from qutebrowser.qt.core import (QMimeData, QObject, QPoint, QPointF, Qt,
                                 pyqtSignal)
from qutebrowser.qt.gui import (QDragEnterEvent, QDragLeaveEvent,
                                QDragMoveEvent, QDropEvent)
from qutebrowser.qt.widgets import QApplication

from qutebrowser.mainwindow import tabtree, workspaceindicator
from qutebrowser.mainwindow.pinpanel import PINNED_TAB_MIME
from qutebrowser.workspaces.manager import WorkspaceMoveError
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
        self.moves: list[tuple[uuid.UUID, int, uuid.UUID, bool]] = []
        self.move_error: Exception | None = None

    def get_active(self) -> Workspace | None:
        return self._workspaces.get(self._active_id)

    def list_all_workspaces(self) -> list[Workspace]:
        return list(self._workspaces.values())

    def set_active(self, workspace_id: uuid.UUID) -> None:
        self._active_id = workspace_id
        self.set_active_calls.append(workspace_id)
        self.changed.emit()

    def add_workspace(self, workspace: Workspace) -> None:
        self._workspaces[workspace.id] = workspace
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

    def move_tab_to_workspace(self, source_id: uuid.UUID, tab_id: int,
                              target_id: uuid.UUID, *, branch: bool = False,
                              parent_id=None, group_id=None) -> int:
        if self.move_error is not None:
            raise self.move_error
        self.moves.append((source_id, tab_id, target_id, branch))
        return tab_id


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


class TestWorkspaceIndicatorInPlaceSync:

    """The indicator must update in place, never destroy surviving buttons."""

    @staticmethod
    def _by_name(manager: FakeManager, name: str) -> Workspace:
        return next(ws for ws in manager.list_all_workspaces()
                    if ws.name == name)

    def test_switch_keeps_button_identity_and_checked_state(
            self, indicator, manager) -> None:
        alpha = self._by_name(manager, 'alpha')
        beta = self._by_name(manager, 'beta')
        before = dict(indicator.buttons())
        assert len(before) == 2

        manager.set_active(beta.id)
        after = indicator.buttons()

        assert len(after) == 2
        assert set(after) == set(before)
        for wid, button in after.items():
            assert button is before[wid]
        assert not after[alpha.id].isChecked()
        assert after[beta.id].isChecked()

        manager.set_active(alpha.id)
        again = indicator.buttons()
        assert again[alpha.id] is before[alpha.id]
        assert again[beta.id] is before[beta.id]
        assert again[alpha.id].isChecked()
        assert not again[beta.id].isChecked()

    def test_rename_updates_label_in_place(self, indicator, manager) -> None:
        alpha = self._by_name(manager, 'alpha')
        before = dict(indicator.buttons())

        manager.rename_workspace(alpha.id, 'delta')

        after = indicator.buttons()
        assert set(after) == set(before)
        assert after[alpha.id] is before[alpha.id]
        assert after[alpha.id].toolTip() == 'delta'
        assert after[alpha.id].isChecked()

    def test_add_inserts_only_new_button(self, indicator, manager) -> None:
        alpha = self._by_name(manager, 'alpha')
        beta = self._by_name(manager, 'beta')
        before = dict(indicator.buttons())
        new = _make('zeta')

        manager.add_workspace(new)

        after = indicator.buttons()
        assert len(after) == 3
        assert new.id in after
        assert after[alpha.id] is before[alpha.id]
        assert after[beta.id] is before[beta.id]
        assert not after[new.id].isChecked()

    def test_delete_removes_only_that_button(self, indicator, manager) -> None:
        alpha = self._by_name(manager, 'alpha')
        beta = self._by_name(manager, 'beta')
        before = dict(indicator.buttons())
        manager.set_active(beta.id)

        manager.delete_workspace(alpha.id)

        after = indicator.buttons()
        assert alpha.id not in after
        assert set(after) == {beta.id}
        assert after[beta.id] is before[beta.id]
        assert after[beta.id].isChecked()

    def test_archive_removes_only_that_button(
            self, indicator, manager) -> None:
        alpha = self._by_name(manager, 'alpha')
        beta = self._by_name(manager, 'beta')
        before = dict(indicator.buttons())

        manager.archive_workspace(alpha.id)

        after = indicator.buttons()
        assert set(after) == {beta.id}
        assert after[beta.id] is before[beta.id]


def _node_mime(workspace_id: uuid.UUID, tab_id: int, *,
               branch: bool = False) -> QMimeData:
    mime = QMimeData()
    mime.setData(tabtree.TAB_NODE_MIME, tabtree.encode_tab_node(
        workspace_id, tab_id, branch=branch))
    return mime


class TestTabNodeDrop:

    """Dropping a TabTree node onto a workspace button (ticket #6)."""

    @pytest.fixture
    def shown(self, indicator, manager, monkeypatch):
        indicator.resize(300, 28)
        indicator.show()
        QApplication.processEvents()
        errors: list[str] = []
        monkeypatch.setattr(
            workspaceindicator.message, 'error',
            lambda text, **_kw: errors.append(text))
        active = manager.get_active()
        assert active is not None
        other = next(ws for ws in manager.list_all_workspaces()
                     if not ws.archived and ws.id != active.id)
        return indicator, manager, active, other, errors

    @staticmethod
    def _center(indicator, workspace_id: uuid.UUID) -> QPoint:
        return indicator.buttons()[workspace_id].geometry().center()

    @staticmethod
    def _send(indicator, event_cls, pos, mime,
              action=Qt.DropAction.MoveAction):
        where = QPointF(pos) if event_cls is QDropEvent else QPoint(pos)
        event = event_cls(where, action, mime, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(indicator, event)
        return event

    def _hover(self, indicator, pos, mime):
        self._send(indicator, QDragEnterEvent, pos, mime)
        return self._send(indicator, QDragMoveEvent, pos, mime)

    def test_accepts_node_over_non_active_button_and_highlights(
            self, shown):
        indicator, manager, active, other, _errors = shown
        pos = self._center(indicator, other.id)
        event = self._hover(indicator, pos, _node_mime(active.id, 7))

        assert event.isAccepted()
        assert indicator._drop_target == other.id
        assert 'background-color' in indicator.buttons()[other.id].styleSheet()

    def test_hover_over_active_button_is_rejected_without_highlight(
            self, shown):
        indicator, manager, active, other, _errors = shown
        pos = self._center(indicator, active.id)
        event = self._hover(indicator, pos, _node_mime(other.id, 7))

        assert not event.isAccepted()
        assert indicator._drop_target is None

    def test_ignores_pinned_mime(self, shown):
        indicator, manager, active, other, _errors = shown
        mime = QMimeData()
        mime.setData(PINNED_TAB_MIME, b'7')
        pos = self._center(indicator, other.id)
        event = self._hover(indicator, pos, mime)

        assert not event.isAccepted()
        assert indicator._drop_target is None

    def test_ignores_unrelated_mime(self, shown):
        indicator, manager, active, other, _errors = shown
        mime = QMimeData()
        mime.setText('just text')
        pos = self._center(indicator, other.id)
        event = self._hover(indicator, pos, mime)
        assert not event.isAccepted()

    def test_drop_moves_leaf_into_target(self, shown):
        indicator, manager, active, other, errors = shown
        pos = self._center(indicator, other.id)
        mime = _node_mime(active.id, 7, branch=False)
        self._hover(indicator, pos, mime)

        event = self._send(indicator, QDropEvent, pos, mime)

        assert event.isAccepted()
        assert manager.moves == [(active.id, 7, other.id, False)]
        assert errors == []
        assert indicator._drop_target is None

    def test_drop_moves_branch_when_payload_says_branch(self, shown):
        indicator, manager, active, other, _errors = shown
        pos = self._center(indicator, other.id)
        mime = _node_mime(active.id, 7, branch=True)
        self._hover(indicator, pos, mime)

        self._send(indicator, QDropEvent, pos, mime)

        assert manager.moves == [(active.id, 7, other.id, True)]

    def test_drop_on_active_workspace_is_rejected_with_message(self, shown):
        indicator, manager, active, other, errors = shown
        pos = self._center(indicator, active.id)
        mime = _node_mime(other.id, 7)
        # Qt never delivers a drop after a rejected enter/move, so call the
        # handler directly to exercise the defensive guard.
        event = QDropEvent(QPointF(pos), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        indicator.dropEvent(event)

        assert not event.isAccepted()
        assert manager.moves == []
        assert len(errors) == 1
        assert active.name in errors[0]

    def test_drop_into_own_workspace_is_rejected_with_message(self, shown):
        indicator, manager, active, other, errors = shown
        pos = self._center(indicator, other.id)
        mime = _node_mime(other.id, 7)
        self._send(indicator, QDragEnterEvent, pos, mime)

        event = self._send(indicator, QDropEvent, pos, mime)

        assert not event.isAccepted()
        assert manager.moves == []
        assert len(errors) == 1

    def test_malformed_payload_is_rejected(self, shown):
        indicator, manager, active, other, errors = shown
        mime = QMimeData()
        mime.setData(tabtree.TAB_NODE_MIME, b'garbage')
        pos = self._center(indicator, other.id)

        assert not self._hover(indicator, pos, mime).isAccepted()
        event = self._send(indicator, QDropEvent, pos, mime)

        assert not event.isAccepted()
        assert manager.moves == []
        assert errors == []

    def test_non_node_drop_is_ignored(self, shown):
        indicator, manager, active, other, errors = shown
        mime = QMimeData()
        mime.setData(PINNED_TAB_MIME, b'7')
        pos = self._center(indicator, other.id)

        event = self._send(indicator, QDropEvent, pos, mime)

        assert not event.isAccepted()
        assert manager.moves == []
        assert errors == []

    def test_move_error_surfaces_as_message_not_exception(self, shown):
        indicator, manager, active, other, errors = shown
        manager.move_error = WorkspaceMoveError('refused in test')
        pos = self._center(indicator, other.id)
        mime = _node_mime(active.id, 7)
        self._hover(indicator, pos, mime)

        event = self._send(indicator, QDropEvent, pos, mime)

        assert not event.isAccepted()
        assert errors == ['refused in test']
        assert indicator._drop_target is None

    def test_leave_clears_highlight(self, shown):
        indicator, manager, active, other, _errors = shown
        pos = self._center(indicator, other.id)
        self._hover(indicator, pos, _node_mime(active.id, 7))
        assert indicator._drop_target == other.id

        QApplication.sendEvent(indicator, QDragLeaveEvent())

        assert indicator._drop_target is None
        assert 'background-color' not in (
            indicator.buttons()[other.id].styleSheet())

    def test_moving_off_button_clears_highlight(self, shown):
        indicator, manager, active, other, _errors = shown
        pos = self._center(indicator, other.id)
        mime = _node_mime(active.id, 7)
        self._hover(indicator, pos, mime)

        self._send(indicator, QDragMoveEvent, QPoint(-50, -50), mime)

        assert indicator._drop_target is None

    def test_active_style_survives_drop_highlight(self, shown):
        indicator, manager, active, other, _errors = shown
        # Highlight the non-active button, then clear it: the active accent
        # border on the active button must survive the restyle.
        self._hover(indicator, self._center(indicator, other.id),
                    _node_mime(active.id, 7))
        QApplication.sendEvent(indicator, QDragLeaveEvent())

        assert 'border' in indicator.buttons()[active.id].styleSheet()
