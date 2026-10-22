# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Keyboard-only navigation of the vertical panel (ADR 0008, issue #55).

Keys are resolved through the real ``bindings.default.workspace_panel``
table and the commands they name are run by the command machinery. Tests
assert on the resulting state (manager, model, widgets), not on key
sequences, and never send mouse events.
"""

import shlex
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest

# Importing the app registers every ``@cmdutils.register`` command.
from qutebrowser import app  # pylint: disable=unused-import
from qutebrowser.components import misccommands
from qutebrowser.config import configdata
from qutebrowser.mainwindow import tabtree, verticalpanel
from qutebrowser.misc import objects
from qutebrowser.qt.core import Qt
from qutebrowser.qt.widgets import QApplication, QListView, QToolButton, QWidget
from qutebrowser.utils import objreg, usertypes
from qutebrowser.workspaces import commands as ws_commands
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import PinnedTab, WorkspaceTab


class _StatusCommand:

    """Records the text the ``cmd-set-text`` path would pre-fill."""

    def __init__(self):
        self.texts = []

    def cmd_set_text_command(self, text, space=False, **_kwargs):
        self.texts.append(text + (' ' if space else ''))


@pytest.fixture
def manager(qapp):
    """A real manager backed by mock persistence (no file I/O)."""
    persistence = mock.Mock()
    persistence.profile_name = 'default'
    persistence.load_all.return_value = []
    return WorkspaceManager(persistence, mock.Mock())


@pytest.fixture
def status():
    return _StatusCommand()


@pytest.fixture
def panel(qtbot, config_stub, manager, monkeypatch, status, win_registry):
    """A real VerticalPanel wired to *manager* and registered for commands."""
    config_stub.val.verticalpanel.width = 220
    config_stub.val.verticalpanel.visible = True
    monkeypatch.setattr(verticalpanel.modeman, 'enter', lambda *a, **k: None)
    monkeypatch.setattr(verticalpanel.modeman, 'leave', lambda *a, **k: None)
    monkeypatch.setattr(ws_commands, '_manager', lambda: manager)
    panel = verticalpanel.VerticalPanel(win_id=0, manager=manager)
    qtbot.add_widget(panel)
    objreg.register('vertical-panel', panel, scope='window', window=0)
    objreg.register('status-command', status, scope='window', window=0)
    # ``panel-filter`` is an instance command: the real method runs against
    # this stand-in and reads its ``_win_id``.
    dispatcher = SimpleNamespace(_win_id=0)
    objreg.register('command-dispatcher', dispatcher, scope='window',
                    window=0)
    # Command.run checks the current mode; the panel is the active mode.
    mode_manager = SimpleNamespace(
        mode=usertypes.KeyMode.workspace_panel, parsers={})
    objreg.register('mode-manager', mode_manager, scope='window', window=0)
    # The ``vp-select-*`` commands find the tree through the registry.
    objreg.register('tab-tree', panel.tab_tree, scope='window', window=0)
    yield panel
    objreg.delete('tab-tree', scope='window', window=0)
    objreg.delete('mode-manager', scope='window', window=0)
    objreg.delete('command-dispatcher', scope='window', window=0)
    objreg.delete('status-command', scope='window', window=0)
    objreg.delete('vertical-panel', scope='window', window=0)


def _binding(key: str) -> str:
    """Return the command the default workspace_panel binding *key* runs."""
    bindings = configdata.DATA['bindings.default'].default['workspace_panel']
    return bindings[key]


class _Keyboard:

    """Presses keys by resolving them through the default panel bindings.

    ``cmd-set-text`` bindings only pre-fill the command line, so the user's
    typing is modelled by the extra *args* passed to :meth:`press`.
    """

    def press(self, key: str, *args: str) -> None:
        parts = shlex.split(_binding(key))
        if parts[0] == 'cmd-set-text':
            # ``cmd-set-text -s :name`` pre-fills ``:name `` for the user.
            name = parts[-1].lstrip(':')
            words = [name, *args]
        else:
            words = [*parts, *args]
        command = objects.commands[words[0]]
        command.run(win_id=0, args=words[1:])


@pytest.fixture
def kb():
    return _Keyboard()


class TestTabChain:

    """Tab / Shift+Tab walks PIN list -> FilterRow -> TabTree -> indicator."""

    @pytest.fixture
    def seeded(self, panel, manager):
        ws = manager.add_workspace('alpha')
        manager.add_workspace('beta')
        manager.add_tab(ws.id, WorkspaceTab(tab_id=1, title='one',
                                            url='https://a.example/',
                                            open_status='OPEN'))
        manager.add_tab(ws.id, WorkspaceTab(tab_id=2, title='two',
                                            url='https://b.example/',
                                            open_status='OPEN'))
        # The PIN panel hides itself while empty, so pin one tab to put
        # it in the chain.
        manager.add_pinned(PinnedTab(tab_id=9, title='Pin',
                                     url='https://p.example/'))
        panel.show()
        panel.activateWindow()
        QApplication.processEvents()
        return panel

    def _walk(self, qtbot, start: QWidget, steps: int) -> list[QWidget]:
        """Press Tab *steps* times from *start*, recording each focus stop."""
        start.setFocus(Qt.FocusReason.OtherFocusReason)
        QApplication.processEvents()
        seen = [QApplication.focusWidget()]
        for _ in range(steps):
            qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
            seen.append(QApplication.focusWidget())
        return seen

    def test_tab_cycles_pin_search_tree_indicator(self, qtbot, seeded):
        """With the FilterRow hidden, Tab skips it and reaches every stop."""
        assert seeded.filter_row.isHidden()
        pin_list = seeded.pin_panel.findChild(QListView)
        assert pin_list is not None
        seen = self._walk(qtbot, pin_list, steps=6)
        # PIN list -> (FilterRow skipped) -> search -> tree -> buttons.
        assert seen[0] is pin_list
        assert seen[1] is seeded.tab_tree.search_input
        assert seen[2] is seeded.tab_tree.view()
        buttons = list(seen[3:5])
        assert all(isinstance(b, QToolButton) for b in buttons)
        assert len(buttons) == 2
        # Tab then reaches the VerticalPanel container itself, which is a
        # pre-existing StrongFocus stop (the empty-panel focus design, see
        # test_verticalpanel), before wrapping to the PIN list.
        assert seen[5] is seeded
        assert seen[6] is pin_list

    def test_filter_row_joins_chain_when_open_and_leaves_when_closed(
            self, qtbot, seeded):
        """Opening the FilterRow inserts it; closing it restores the order."""
        pin_list = seeded.pin_panel.findChild(QListView)
        seeded.open_filter()
        QApplication.processEvents()
        assert seeded.filter_row.isVisible()
        seen = self._walk(qtbot, pin_list, steps=2)
        assert seen[1] is seeded.filter_row

        seeded.filter_row.close_filter()
        QApplication.processEvents()
        seen = self._walk(qtbot, pin_list, steps=2)
        assert seen[1] is seeded.tab_tree.search_input
        assert seen[2] is seeded.tab_tree.view()

    def test_chain_survives_workspace_button_rebuild(self, qtbot, seeded,
                                                     manager):
        """A new workspace button is reached by Tab after a manager change."""
        manager.add_workspace('gamma')
        QApplication.processEvents()
        buttons = [
            seeded.workspace_indicator.widgetForAction(action)
            for action in seeded.workspace_indicator.actions()
        ]
        buttons = [b for b in buttons if isinstance(b, QToolButton)]
        assert len(buttons) == 3
        seeded.tab_tree.view().setFocus(Qt.FocusReason.OtherFocusReason)
        seen = self._walk(qtbot, seeded.tab_tree.view(), steps=3)
        assert seen[1:] == buttons


class TestGroupJump:

    """``gh``: move the TabTree selection to the next group header."""

    @pytest.fixture
    def tree(self, panel, manager):
        ws = manager.add_workspace('alpha')
        first = manager.create_group(ws.id, 'First')
        second = manager.create_group(ws.id, 'Second')
        manager.add_tab(ws.id, WorkspaceTab(
            tab_id=1, title='one', group_id=first.group_id,
            open_status='OPEN'))
        manager.add_tab(ws.id, WorkspaceTab(
            tab_id=2, title='two', group_id=second.group_id,
            open_status='OPEN'))
        manager.add_tab(ws.id, WorkspaceTab(
            tab_id=3, title='loose', open_status='OPEN'))
        QApplication.processEvents()
        return panel.tab_tree

    def _group_names_in_order(self, tree):
        model = tree.model()
        names = []
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if index.data(tabtree.KindRole) == tabtree.Kind.Group:
                names.append(index.data(Qt.ItemDataRole.DisplayRole))
        return names

    def test_from_standalone_tab_goes_to_next_group(self, tree):
        """From a root tab, the next header in display order is selected."""
        first_root = tree.model().index_for_tab_id(3)
        tree.view().setCurrentIndex(first_root)
        assert tree.focus_next_group()
        current = tree.view().currentIndex()
        assert current.data(tabtree.KindRole) == tabtree.Kind.Group
        assert tree.view().hasFocus()

    def test_from_tab_inside_group_moves_past_that_group(self, tree):
        """A tab inside group A moves to group B, not to A's own header."""
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(1))
        assert tree.focus_next_group()
        current = tree.view().currentIndex()
        assert current.data(tabtree.KindRole) == tabtree.Kind.Group
        assert current.data(Qt.ItemDataRole.DisplayRole) == 'Second'

    def test_wraps_to_first_group_past_the_last(self, tree):
        """Past the last group the jump wraps to the first."""
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(2))
        assert tree.focus_next_group()
        current = tree.view().currentIndex()
        assert current.data(Qt.ItemDataRole.DisplayRole) == 'First'

    def test_no_groups_prefills_vp_group_new(self, panel, manager, status,
                                             kb):
        """With no group header, ``gh`` pre-fills the existing vp-group-new."""
        ws = manager.add_workspace('alpha')
        manager.add_tab(ws.id, WorkspaceTab(
            tab_id=1, title='one', open_status='OPEN'))
        QApplication.processEvents()
        assert not panel.tab_tree.focus_next_group()
        kb.press('gh')
        assert status.texts == [':vp-group-new ']


class TestCloseBranchHint:

    """``Shift+F2``: the close-branch hint overlay."""

    @pytest.fixture
    def tree(self, panel, manager):
        ws = manager.add_workspace('customer-A')
        group = manager.create_group(ws.id, 'Client docs')
        manager.add_tab(ws.id, WorkspaceTab(
            tab_id=1, title='Ticket #42', group_id=group.group_id,
            open_status='OPEN'))
        manager.add_tab(ws.id, WorkspaceTab(
            tab_id=2, title='Loose', open_status='OPEN'))
        QApplication.processEvents()
        # The row text is the live title the bridge reports (set_tab_info).
        panel.tab_tree.model().set_tab_info(1, title='Ticket #42')
        panel.tab_tree.model().set_tab_info(2, title='Loose')
        return panel.tab_tree

    @pytest.fixture
    def received(self, tree):
        closed = []
        tree.close_requested.connect(closed.append)
        return closed

    @pytest.fixture
    def capture(self, monkeypatch):
        """Record set_keys_captured calls on the workspace_panel parser."""
        calls = []
        parser = mock.Mock()
        parser.set_keys_captured.side_effect = calls.append
        mode_manager = SimpleNamespace(
            mode=usertypes.KeyMode.workspace_panel,
            parsers={usertypes.KeyMode.workspace_panel: parser})
        monkeypatch.setattr(tabtree.modeman, 'instance',
                            lambda win_id: mode_manager)
        return calls

    def test_overlay_names_workspace_and_row(self, tree, kb):
        """The overlay shows the active workspace and the selected row."""
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(1))
        kb.press('<Shift-F2>')
        assert tree.close_hint_open
        assert not tree._close_hint.isHidden()
        assert tree._hint_workspace.text() == '[w] close workspace  customer-A'
        assert tree._hint_tab.text() == '[t] close tab  Ticket #42'

    def test_overlay_is_plain_text_and_palette_coloured(self, tree):
        """Titles can't inject markup and the colours come from panelstyle."""
        from qutebrowser.mainwindow import panelstyle
        assert tree._hint_tab.textFormat().name == 'PlainText'
        palette = panelstyle.Palette.from_config()
        sheet = tree._close_hint.styleSheet()
        assert palette.focus_outer.name() in sheet
        assert palette.search_fill.name() in sheet
        assert palette.text.name() in sheet

    def test_t_closes_selected_tab(self, tree, kb, received, qtbot):
        """``t`` closes the selected tab through the row's close path."""
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(2))
        kb.press('<Shift-F2>')
        qtbot.keyClick(tree.view(), Qt.Key.Key_T)
        assert [tree.model().tab_id_for_index(i) for i in received] == [2]
        assert not tree.close_hint_open

    def test_escape_dismisses_without_action(self, tree, kb, received, qtbot):
        """Escape dismisses the overlay and closes nothing."""
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(2))
        kb.press('<Shift-F2>')
        qtbot.keyClick(tree.view(), Qt.Key.Key_Escape)
        assert not tree.close_hint_open
        assert tree._close_hint.isHidden()
        assert received == []

    def test_other_key_dismisses_and_is_not_passed_on(
            self, tree, kb, received, qtbot):
        """Any other key dismisses; it does not reach the tree's rows."""
        group_index = tree.model().index(0, 0)
        tree.view().setCurrentIndex(group_index)
        folded_before = bool(group_index.data(tabtree.FoldedRole))
        kb.press('<Shift-F2>')
        qtbot.keyClick(tree.view(), Qt.Key.Key_Left)
        assert not tree.close_hint_open
        assert bool(group_index.data(tabtree.FoldedRole)) == folded_before
        assert received == []

    def test_w_runs_workspace_close_after_confirmation(
            self, tree, kb, received, qtbot, monkeypatch):
        """``w`` asks first, then closes every open tab of the workspace."""
        prompts = []
        monkeypatch.setattr(tabtree.message, 'confirm_async',
                            lambda **kw: prompts.append(kw))
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(2))
        kb.press('<Shift-F2>')
        qtbot.keyClick(tree.view(), Qt.Key.Key_W)
        assert len(prompts) == 1
        assert received == []  # nothing closes before confirmation
        prompts[0]['yes_action']()
        closed = sorted(tree.model().tab_id_for_index(i) for i in received)
        assert closed == [1, 2]

    def test_hint_capture_toggles_parser_passthrough(
            self, tree, kb, capture, qtbot):
        """While open, the workspace_panel parser lets keys through."""
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(2))
        kb.press('<Shift-F2>')
        assert capture == [True]
        qtbot.keyClick(tree.view(), Qt.Key.Key_Escape)
        assert capture == [True, False]

    def test_shortcut_override_is_swallowed_while_open(self, tree, kb, qtbot):
        """j/k shortcuts on the tree must not fire while the hint is up."""
        tree.view().setCurrentIndex(tree.model().index_for_tab_id(2))
        kb.press('<Shift-F2>')
        before = tree.view().currentIndex()
        qtbot.keyClick(tree.view(), Qt.Key.Key_J)
        assert tree.view().currentIndex() == before
        assert not tree.close_hint_open


class TestFullPanelKeyboard:

    """Every panel capability, completed from the keyboard alone."""

    def test_complete_panel_workflow_by_keyboard(
            self, panel, manager, kb, qtbot, monkeypatch):
        tree = panel.tab_tree
        view = tree.view()
        alpha = manager.add_workspace('alpha')
        beta = manager.add_workspace('beta')
        manager.add_tab(alpha.id, WorkspaceTab(
            tab_id=1, title='one', url='https://a.example/',
            open_status='OPEN'))
        manager.add_tab(alpha.id, WorkspaceTab(
            tab_id=2, title='two', url='https://b.example/',
            open_status='OPEN'))
        panel.show()
        QApplication.processEvents()
        tree.model().set_tab_info(1, title='one')
        tree.model().set_tab_info(2, title='two')
        view.setFocus(Qt.FocusReason.OtherFocusReason)

        def tabs(workspace_id):
            return {t.tab_id: t for t in manager.get_workspace(
                workspace_id).tabs}

        # 1. Create a group (``ggn`` pre-fills, the user types the name).
        kb.press('ggn', 'Work')
        assert [g.name for g in manager.get_workspace(alpha.id).groups
                if g.name == 'Work'] == ['Work']
        group_id = next(g.group_id for g in manager.get_workspace(
            alpha.id).groups if g.name == 'Work')

        # 2. Move a tab into the group.
        kb.press('gG', '1', 'Work')
        assert tabs(alpha.id)[1].group_id == group_id

        # 3. Jump to the group header, then fold and unfold it.
        kb.press('gh')
        header = view.currentIndex()
        assert header.data(tabtree.KindRole) == tabtree.Kind.Group
        qtbot.keyClick(view, Qt.Key.Key_Left)
        assert header.data(tabtree.FoldedRole)
        qtbot.keyClick(view, Qt.Key.Key_Right)
        assert not header.data(tabtree.FoldedRole)

        # 4. Pin a tab (it leaves the tree for the PIN panel), then unpin it.
        kb.press('gp', '2')
        assert [p.tab_id for p in manager.list_pinned()] == [2]
        assert 2 not in tabs(alpha.id)
        kb.press('gP', '2')
        assert manager.list_pinned() == []

        # A third tab gives the filter, multi-select and move steps something
        # to act on.
        manager.add_tab(alpha.id, WorkspaceTab(
            tab_id=3, title='two', url='https://c.example/',
            open_status='OPEN'))
        QApplication.processEvents()
        tree.model().set_tab_info(3, title='two')

        # 5. Filter the tree; Escape closes the row again.
        kb.press('/')
        assert panel.filter_row.isVisible()
        qtbot.keyClicks(panel.filter_row, 'two')
        assert tree.model().index_for_tab_id(1).isValid() is False
        assert tree.model().index_for_tab_id(3).isValid()
        qtbot.keyClick(panel.filter_row, Qt.Key.Key_Escape)
        assert panel.filter_row.isHidden()
        assert tree.model().index_for_tab_id(1).isValid()

        # 6. Multi-select every tab, then leave selection mode.
        kb.press('<Ctrl-Space>')
        kb.press('<Ctrl-A>')
        assert tree.model().selected_tab_ids() == {1, 3}
        kb.press('<Ctrl-Space>')

        # 7. Move a tab to another workspace (``tabtree-move``).
        kb.press('gT', '3', 'beta')
        assert 3 in tabs(beta.id)
        assert 3 not in tabs(alpha.id)

        # 8. Switch workspace and back.
        kb.press('gws', 'beta')
        assert manager.get_active().id == beta.id
        kb.press('gws', 'alpha')
        assert manager.get_active().id == alpha.id

        # 9. Mute: the binding is an active-tab operation in the panel. The
        # mute state lives on the webview, so the tab object stands in for it.
        assert _binding('<Alt-m>') == 'tab-mute'
        fake_tab = mock.Mock()
        fake_tab.audio.is_muted.return_value = False
        misccommands.tab_mute(fake_tab)
        fake_tab.audio.set_muted.assert_called_once_with(True, override=True)

        # 10. Close a branch: point at tab 1, open the hint, close with ``t``.
        closed = []
        tree.close_requested.connect(closed.append)
        view.setCurrentIndex(tree.model().index_for_tab_id(1))
        kb.press('<Shift-F2>')
        assert tree.close_hint_open
        qtbot.keyClick(view, Qt.Key.Key_T)
        assert [tree.model().tab_id_for_index(i) for i in closed] == [1]
        assert not tree.close_hint_open
