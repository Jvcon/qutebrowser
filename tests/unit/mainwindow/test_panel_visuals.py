# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Focus ring, keyboard navigation and screen-reader contract (issue #37).

These tests drive the panel through real Qt events (focus, key and context
menu events), so they cover the event routing without a physical keyboard.
What they cannot cover is AT-SPI delivery to a screen reader; that needs a
desktop session (see the report for issue #37).
"""

import uuid

import pytest

from qutebrowser.qt.core import QEvent, QModelIndex, QObject, QPoint, Qt, pyqtSignal
from qutebrowser.qt.gui import (
    QContextMenuEvent,
    QFocusEvent,
    QKeyEvent,
    QPainter,
    QPixmap,
)
from qutebrowser.qt.widgets import QApplication, QStyle, QStyleOptionViewItem, QTreeView

from qutebrowser.mainwindow import panelstyle, tabtree
from qutebrowser.mainwindow.filterrow import FilterRow
from qutebrowser.mainwindow.panelstyle import Palette
from qutebrowser.mainwindow.pinpanel import PINPanel
from qutebrowser.mainwindow.workspaceindicator import WorkspaceIndicator
from qutebrowser.workspaces.model import Group, PinnedTab, Workspace, WorkspaceTab


class _Manager(QObject):

    """Minimal manager covering what TabTree / PINPanel / indicator call."""

    changed = pyqtSignal()
    pinned_changed = pyqtSignal()

    def __init__(self, workspace=None, pinned=(), workspaces=None):
        super().__init__()
        self._workspace = workspace
        self._pinned = list(pinned)
        self._workspaces = list(workspaces) if workspaces else []

    def get_active(self):
        return self._workspace

    def list_all_workspaces(self):
        return list(self._workspaces)

    def set_active(self, workspace_id):
        pass

    def list_pinned(self):
        return list(self._pinned)

    def group_open_count(self, workspace_id, group_id):
        return sum(1 for t in self._workspace.tabs
                   if t.group_id == group_id and t.open_status == 'OPEN')

    def group_total_count(self, workspace_id, group_id):
        return sum(1 for t in self._workspace.tabs if t.group_id == group_id)

    def group_state(self, workspace_id, group_id):
        group = next(g for g in self._workspace.groups
                     if g.group_id == group_id)
        if group.archived:
            return 'ARCHIVED'
        if self.group_open_count(workspace_id, group_id):
            return 'OPEN'
        return 'SUSPENDED'


def _workspace():
    """One group (3 members, all OPEN), a parent with a child, 2 standalone."""
    group = Group(group_id=uuid.uuid4(), name='client-A docs', folded=False)
    tabs = [
        WorkspaceTab(tab_id=10, open_status='OPEN'),
        WorkspaceTab(tab_id=11, parent_id=10, open_status='OPEN'),
        WorkspaceTab(tab_id=20, open_status='OPEN'),
        WorkspaceTab(tab_id=21, open_status='OPEN'),
        WorkspaceTab(tab_id=30, group_id=group.group_id, open_status='OPEN'),
        WorkspaceTab(tab_id=31, group_id=group.group_id, open_status='OPEN'),
        WorkspaceTab(tab_id=32, group_id=group.group_id, open_status='OPEN'),
    ]
    return Workspace(id=uuid.uuid4(), name='test', groups=[group], tabs=tabs)


@pytest.fixture
def workspace():
    return _workspace()


@pytest.fixture
def tree(qtbot, workspace):
    manager = _Manager(workspace)
    widget = tabtree.TabTree(win_id=0, manager=manager)
    qtbot.add_widget(widget)
    widget.resize(260, 400)
    widget.show()
    return widget


def _find(model, kind, parent=QModelIndex()):
    """Return the first index of *kind* under *parent*, depth-first."""
    for row in range(model.rowCount(parent)):
        index = model.index(row, 0, parent)
        if index.data(tabtree.KindRole) == kind:
            return index
        found = _find(model, kind, index)
        if found is not None and found.isValid():
            return found
    return None


def _tab(model, tab_id):
    return model.index_for_tab_id(tab_id)


def _group(model):
    return _find(model, tabtree.Kind.Group)


def _send_key(widget, key, modifiers=Qt.KeyboardModifier.NoModifier):
    QApplication.sendEvent(
        widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def _focus(widget, reason):
    QApplication.sendEvent(
        widget, QFocusEvent(QEvent.Type.FocusIn, reason))


def _option(width=220, height=22):
    option = QStyleOptionViewItem()
    option.rect = QPixmap(width, height).rect()
    return option


def _paint(delegate, index, option):
    pixmap = QPixmap(option.rect.width(), option.rect.height())
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        delegate.paint(painter, option, index)
    finally:
        painter.end()
    return pixmap.toImage()


def _rgb(image, x, y):
    colour = image.pixelColor(x, y)
    return (colour.red(), colour.green(), colour.blue())


class TestAccessibleNames:

    def test_tree_and_view_are_named_tabs(self, tree):
        assert tree.accessibleName() == 'Tabs'
        assert tree.view().accessibleName() == 'Tabs'
        # Qt's AT-SPI role for a QTreeView is "tree"; the ADR asks for Tree.
        assert isinstance(tree.view(), QTreeView)

    def test_tree_description_explains_keys(self, tree):
        assert 'arrow' in tree.accessibleDescription().lower()

    def test_pin_panel_names(self, qtbot, config_stub):
        panel = PINPanel(win_id=0, manager=_Manager())
        qtbot.add_widget(panel)
        assert panel.accessibleName() == 'Pinned tabs'
        assert panel.list_widget().accessibleName() == 'Pinned tabs'

    def test_filter_row_name(self, qtbot):
        row = FilterRow()
        qtbot.add_widget(row)
        assert row.accessibleName() == 'Filter tabs'
        assert 'Escape' in row.accessibleDescription()

    def test_workspace_indicator_name_and_buttons(self, qtbot, config_stub):
        alpha = Workspace(id=uuid.uuid4(), name='alpha')
        beta = Workspace(id=uuid.uuid4(), name='beta')
        manager = _Manager(alpha, workspaces=[alpha, beta])
        indicator = WorkspaceIndicator(0, manager)
        qtbot.add_widget(indicator)
        assert indicator.accessibleName() == 'Workspaces'
        buttons = indicator.buttons()
        assert buttons[alpha.id].accessibleName() == 'alpha'
        assert buttons[alpha.id].accessibleDescription() == (
            'active workspace')
        assert buttons[beta.id].accessibleDescription() == 'workspace'


class TestRowAnnouncements:

    def test_tab_row_text_and_state(self, tree, workspace):
        model = tree.model()
        model.set_tab_info(
            20, title='GitHub Issue #128',
            url='github.com/issues/128',
            status=tabtree.Status.LOADING | tabtree.Status.AUDIBLE)
        index = _tab(model, 20)
        assert index.data(Qt.ItemDataRole.AccessibleTextRole) == (
            'GitHub Issue #128')
        description = index.data(Qt.ItemDataRole.AccessibleDescriptionRole)
        assert description == (
            'tab, github.com/issues/128, reading fresh, open, loading, '
            'audio playing')

    def test_tab_row_announces_group_and_reading_status(self, tree):
        model = tree.model()
        model.set_tab_info(30, title='Doc', url='docs.example/a')
        # Group membership comes from the workspace model, not tab info.
        tab = next(t for t in tree.model()._workspace.tabs
                   if t.tab_id == 30)
        tab.reading_status = 'DONE'
        description = _tab(model, 30).data(
            Qt.ItemDataRole.AccessibleDescriptionRole)
        assert 'group client-A docs' in description
        assert 'reading done' in description

    def test_suspended_tab_is_announced(self, tree):
        model = tree.model()
        model.set_tab_info(21, title='Idle', url='idle.example')
        tab = next(t for t in model._workspace.tabs if t.tab_id == 21)
        tab.open_status = 'SUSPENDED'
        description = _tab(model, 21).data(
            Qt.ItemDataRole.AccessibleDescriptionRole)
        assert 'suspended' in description.split(', ')
        assert 'open' not in description.split(', ')

    def test_parent_row_announces_expansion(self, tree):
        model = tree.model()
        model.set_tab_info(10, title='Dashboard', url='host.com/dashboard')
        description = _tab(model, 10).data(
            Qt.ItemDataRole.AccessibleDescriptionRole)
        assert description == (
            'tab, host.com/dashboard, parent, reading fresh, open, expanded')

    def test_group_row_announces_fold_and_count(self, tree):
        model = tree.model()
        group = _group(model)
        assert group.data(Qt.ItemDataRole.AccessibleTextRole) == (
            'client-A docs')
        assert group.data(Qt.ItemDataRole.AccessibleDescriptionRole) == (
            'group, 3/3 open, expanded, open')
        model.toggle_folded(group)
        assert group.data(Qt.ItemDataRole.AccessibleDescriptionRole) == (
            'group, 3/3 open, collapsed, open')

    def test_group_row_tooltip_fallback_kept(self, tree):
        group = _group(tree.model())
        assert 'client-A docs' in group.data(
            Qt.ItemDataRole.ToolTipRole)


class TestExpansion:

    def test_group_members_are_visible_by_default(self, tree):
        """Regression: members were hidden because the view never expanded."""
        model = tree.model()
        group = _group(model)
        view = tree.view()
        assert view.isExpanded(group)
        member = model.index(0, 0, group)
        assert member.isValid()
        assert view.visualRect(member).height() > 0

    def test_folded_group_hides_members(self, tree):
        model = tree.model()
        group = _group(model)
        model.toggle_folded(group)
        tree._sync_expansion()
        assert not tree.view().isExpanded(group)

    def test_left_collapses_and_right_expands_group(self, tree):
        model = tree.model()
        view = tree.view()
        group = _group(model)
        view.setCurrentIndex(group)
        _send_key(view, Qt.Key.Key_Left)
        assert not view.isExpanded(group)
        assert group.data(tabtree.FoldedRole) is True
        _send_key(view, Qt.Key.Key_Right)
        assert view.isExpanded(group)
        assert group.data(tabtree.FoldedRole) is False

    def test_right_on_expanded_parent_steps_into_child(self, tree):
        model = tree.model()
        view = tree.view()
        parent = _tab(model, 10)
        view.setCurrentIndex(parent)
        _send_key(view, Qt.Key.Key_Right)
        assert view.currentIndex().data(tabtree.KindRole) != (
            tabtree.Kind.Group)
        assert model.tab_id_for_index(view.currentIndex()) == 11

    def test_left_on_child_steps_to_parent(self, tree):
        model = tree.model()
        view = tree.view()
        child = _tab(model, 11)
        view.setCurrentIndex(child)
        _send_key(view, Qt.Key.Key_Left)
        assert model.tab_id_for_index(view.currentIndex()) == 10

    def test_left_collapses_parent_tab(self, tree):
        model = tree.model()
        view = tree.view()
        parent = _tab(model, 10)
        view.setCurrentIndex(parent)
        _send_key(view, Qt.Key.Key_Left)
        assert not view.isExpanded(parent)

    def test_arrow_down_moves_to_next_visible_row(self, tree):
        model = tree.model()
        view = tree.view()
        view.setCurrentIndex(_tab(model, 10))
        _send_key(view, Qt.Key.Key_Down)
        assert model.tab_id_for_index(view.currentIndex()) == 11


class TestKeyboardActivation:

    def _record_activated(self, tree):
        seen = []
        tree.activated.connect(seen.append)
        return seen

    def test_return_activates_tab_row(self, tree):
        model = tree.model()
        seen = self._record_activated(tree)
        tree.view().setCurrentIndex(_tab(model, 20))
        _send_key(tree.view(), Qt.Key.Key_Return)
        assert [model.tab_id_for_index(i) for i in seen] == [20]

    def test_space_activates_tab_row(self, tree):
        model = tree.model()
        seen = self._record_activated(tree)
        tree.view().setCurrentIndex(_tab(model, 20))
        _send_key(tree.view(), Qt.Key.Key_Space)
        assert [model.tab_id_for_index(i) for i in seen] == [20]

    def test_return_on_group_is_a_no_op(self, tree):
        seen = self._record_activated(tree)
        tree.view().setCurrentIndex(_group(tree.model()))
        _send_key(tree.view(), Qt.Key.Key_Return)
        assert seen == []

    def test_keyboard_context_menu_opens_on_cursor_row(self, tree,
                                                       monkeypatch):
        model = tree.model()
        view = tree.view()
        view.setCurrentIndex(_group(model))
        positions = []
        monkeypatch.setattr(
            tree, '_on_context_menu_requested', positions.append)
        QApplication.sendEvent(view, QContextMenuEvent(
            QContextMenuEvent.Reason.Keyboard, QPoint(0, 0)))
        assert len(positions) == 1
        row = view.visualRect(_group(model))
        assert row.contains(positions[0])

    def test_mouse_context_menu_is_left_to_qt(self, tree, monkeypatch):
        positions = []
        monkeypatch.setattr(
            tree, '_on_context_menu_requested', positions.append)
        QApplication.sendEvent(tree.view(), QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(5, 5)))
        assert positions == []


class TestPinPanelKeyboard:

    @pytest.fixture
    def panel(self, qtbot, config_stub):
        pinned = [PinnedTab(tab_id=1, title='One', url='one.example'),
                  PinnedTab(tab_id=2, title='Two', url='two.example')]
        manager = _Manager(pinned=pinned)
        widget = PINPanel(win_id=0, manager=manager)
        qtbot.add_widget(widget)
        widget.resize(240, 80)
        widget.show()
        return widget

    def test_return_activates_pinned_tab(self, panel):
        seen = []
        panel.activated.connect(seen.append)
        panel.list_widget().setCurrentIndex(
            panel.list_widget().model().index(1, 0))
        _send_key(panel.list_widget(), Qt.Key.Key_Return)
        assert [tab.tab_id for tab in seen] == [2]

    def test_delete_unpins_current_tab(self, panel):
        removed = []
        panel.unpinned.connect(removed.append)
        panel.list_widget().setCurrentIndex(
            panel.list_widget().model().index(0, 0))
        _send_key(panel.list_widget(), Qt.Key.Key_Delete)
        assert removed == [1]

    def test_menu_key_opens_context_menu_on_current_row(
            self, panel, monkeypatch):
        opened = []
        monkeypatch.setattr(
            panel, '_open_menu', lambda index, pos: opened.append(index))
        panel.list_widget().setCurrentIndex(
            panel.list_widget().model().index(1, 0))
        QApplication.sendEvent(panel.list_widget(), QContextMenuEvent(
            QContextMenuEvent.Reason.Keyboard, QPoint(0, 0)))
        assert [index.row() for index in opened] == [1]

    def test_mouse_context_menu_is_left_to_qt(self, panel, monkeypatch):
        opened = []
        monkeypatch.setattr(
            panel, '_open_menu', lambda index, pos: opened.append(index))
        QApplication.sendEvent(panel.list_widget(), QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(5, 5)))
        assert opened == []

    def test_row_is_announced_with_title_and_url(self, panel):
        index = panel.list_widget().model().index(0, 0)
        assert index.data(Qt.ItemDataRole.AccessibleTextRole) == 'One'
        assert index.data(Qt.ItemDataRole.AccessibleDescriptionRole) == (
            'pinned tab, one.example')
        assert panel.accessibleDescription() == 'Pinned tabs, 2 items'


class TestFocusRing:

    def test_keyboard_focus_sets_delegate_flag(self, tree):
        _focus(tree.view(), Qt.FocusReason.TabFocusReason)
        assert tree._delegate._keyboard_focus is True

    def test_mouse_focus_suppresses_ring(self, tree):
        _focus(tree.view(), Qt.FocusReason.MouseFocusReason)
        assert tree._delegate._keyboard_focus is False

    def test_focus_out_clears_flag(self, tree):
        _focus(tree.view(), Qt.FocusReason.TabFocusReason)
        QApplication.sendEvent(tree.view(), QFocusEvent(
            QEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason))
        assert tree._delegate._keyboard_focus is False

    def test_keyboard_focus_paints_accent_ring(self, tree):
        palette = Palette.from_config()
        model = tree.model()
        index = _tab(model, 20)
        delegate = tabtree.TabTreeItemDelegate()
        delegate.set_keyboard_focus(True)
        option = _option()
        option.state = QStyle.StateFlag.State_HasFocus
        image = _paint(delegate, index, option)
        # Outer 2 px ring sits on the left edge (x 0..1) of the row.
        assert _rgb(image, 1, 11) == _rgb_of(palette.focus_outer)

    def test_mouse_focus_paints_no_ring(self, tree):
        palette = Palette.from_config()
        index = _tab(tree.model(), 20)
        delegate = tabtree.TabTreeItemDelegate()
        delegate.set_keyboard_focus(False)
        option = _option()
        option.state = QStyle.StateFlag.State_HasFocus
        image = _paint(delegate, index, option)
        assert _rgb(image, 1, 11) != _rgb_of(palette.focus_outer)

    def test_current_row_is_tracked_from_view(self, tree):
        model = tree.model()
        tree.view().setCurrentIndex(_tab(model, 21))
        assert tree._delegate._is_current(_tab(model, 21))
        assert not tree._delegate._is_current(_tab(model, 20))

    def test_hover_marker_is_a_left_bar(self, tree):
        palette = Palette.from_config()
        index = _tab(tree.model(), 20)
        delegate = tabtree.TabTreeItemDelegate()
        option = _option()
        option.state = QStyle.StateFlag.State_MouseOver
        image = _paint(delegate, index, option)
        marker = _rgb_of(palette.hover_marker)
        # The bar is 3 px wide: x 0..2 carry it, x 3 falls back to the fill.
        for x in range(tabtree.TabTreeItemDelegate.HOVER_MARKER_WIDTH):
            assert _rgb(image, x, 11) == marker, x
        assert _rgb(image, 3, 11) != marker

    def test_hover_marker_is_not_covered_by_favicon(self, tree):
        """Regression: the favicon used to be painted over the hover bar."""
        palette = Palette.from_config()
        model = tree.model()
        model.set_tab_info(20, title='Fav')
        index = _tab(model, 20)
        delegate = tabtree.TabTreeItemDelegate()
        option = _option()
        option.state = QStyle.StateFlag.State_MouseOver
        image = _paint(delegate, index, option)
        assert _rgb(image, 0, 11) == _rgb_of(palette.hover_marker)

    def test_checkbox_is_14_px_with_a_drawn_check(self, tree):
        palette = Palette.from_config()
        tree.model().set_selected(20, True)
        index = _tab(tree.model(), 20)
        delegate = tabtree.TabTreeItemDelegate()
        option = _option()
        option.state = QStyle.StateFlag.State_Selected
        image = _paint(delegate, index, option)
        assert tabtree.TabTreeItemDelegate.SELECTION_INDICATOR_WIDTH == 14
        accent = _rgb_of(palette.accent)
        box_x = tabtree.TabTreeItemDelegate.ACTIVE_ACCENT_WIDTH + 2
        box_pixels = [
            _rgb(image, x, y) == accent
            for x in range(box_x, box_x + 14) for y in range(4, 18)]
        assert sum(box_pixels) > 14 * 14 * 0.4
        # The check is stroked, so some pixels inside the box are not accent.
        assert sum(box_pixels) < 14 * 14

    def test_selected_row_fill_differs_from_plain_row(self, tree):
        index = _tab(tree.model(), 20)
        delegate = tabtree.TabTreeItemDelegate()
        plain = _paint(delegate, index, _option())
        option = _option()
        option.state = QStyle.StateFlag.State_Selected
        selected = _paint(delegate, index, option)
        assert _rgb(plain, 200, 3) != _rgb(selected, 200, 3)

    def test_filter_row_gets_keyboard_focus_ring_rule(self, qtbot):
        row = FilterRow()
        qtbot.add_widget(row)
        assert 'keyboardFocus' in row.styleSheet()
        assert '#0000aa' in row.styleSheet()
        _focus(row, Qt.FocusReason.TabFocusReason)
        assert row.property('keyboardFocus') is True
        _focus(row, Qt.FocusReason.MouseFocusReason)
        assert row.property('keyboardFocus') is False

    def test_search_field_focus_is_keyboard_only(self, tree):
        field = tree.search_input
        _focus(field, Qt.FocusReason.MouseFocusReason)
        assert field.property('keyboardFocus') is False
        _focus(field, Qt.FocusReason.TabFocusReason)
        assert field.property('keyboardFocus') is True

    def test_workspace_button_keeps_focus_rule_on_restyle(self, qtbot,
                                                          config_stub):
        alpha = Workspace(id=uuid.uuid4(), name='alpha')
        manager = _Manager(alpha, workspaces=[alpha])
        indicator = WorkspaceIndicator(0, manager)
        qtbot.add_widget(indicator)
        button = indicator.buttons()[alpha.id]
        assert 'keyboardFocus' in button.styleSheet()
        assert '2px solid' in button.styleSheet()


def _rgb_of(colour):
    return (colour.red(), colour.green(), colour.blue())


class TestSearchAndStatusVisuals:

    def test_search_terms_reach_the_model_rows(self, tree):
        tree.model().set_search_terms(['docs'])
        index = _tab(tree.model(), 30)
        assert index.data(tabtree.SearchTermsRole) == ['docs']

    def test_search_match_is_underlined_in_accent(self, tree):
        palette = Palette.from_config()
        model = tree.model()
        model.set_tab_info(21, title='Plain standalone', url='example.org')
        index = _tab(model, 21)
        delegate = tabtree.TabTreeItemDelegate()

        model.set_search_terms([])
        plain = _paint(delegate, index, _option())
        model.set_search_terms(['standalone'])
        underlined = _paint(delegate, index, _option())

        def accent_pixels(image):
            return sum(
                1 for x in range(image.width())
                for y in range(image.height())
                if _rgb(image, x, y) == _rgb_of(palette.accent))

        assert accent_pixels(plain) == 0
        assert accent_pixels(underlined) > 0

    def test_loading_and_audio_glyphs_use_their_own_colours(self):
        segments = tabtree.TabTreeItemDelegate._status_segments(
            tabtree.Status.LOADING | tabtree.Status.AUDIBLE
            | tabtree.Status.SUSPENDED)
        assert [key for _glyph, key in segments] == [
            'loading', 'text', 'suspended']
        assert tabtree.TabTreeItemDelegate._status_text(
            tabtree.Status.LOADING | tabtree.Status.AUDIBLE) == '↻♪'

    def test_palette_strip_colours_follow_adr_opacity(self):
        """Loading 40 % and suspended 50 % of the foreground (ADR § Colour)."""
        from qutebrowser.mainwindow.panelstyle import blend
        palette = Palette.from_config()
        loading = blend(palette.base, palette.text, 0.40)
        suspended = blend(palette.base, palette.text, 0.50)
        # ensure_contrast may shift a failing candidate toward the text, so
        # the palette colour is at least as strong as the nominal blend.
        assert palette.loading.name() == loading.name() or (
            panelstyle.contrast_ratio(palette.loading, palette.base)
            >= panelstyle.contrast_ratio(loading, palette.base) - 1e-6)
        assert palette.suspended.name() == suspended.name() or (
            panelstyle.contrast_ratio(palette.suspended, palette.base)
            >= panelstyle.contrast_ratio(suspended, palette.base) - 1e-6)
