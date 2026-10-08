# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The vertical workspace panel (Phase 2).

Sits to the left of the browser area, joined by a ``QSplitter``. The
panel is composed (top to bottom) of:

- PINPanel            (added in a later sub-slice)
- TabTree             (added in a later sub-slice)
- WorkspaceIndicator  (added in a later sub-slice)

This commit ships the empty shell plus the focus event filter and the
visibility toggle. The shell is focusable on purpose so the focus
event filter can already drive the ``KeyMode.workspace_panel``
entry/exit contract from ADR 0008 § "Panel focus & mode model" before
any child widget is added; the children land in later sub-slices
without changing the focus contract.

Focus protocol
=============

- Focus enters any direct or transitive descendant of this widget
  (or the widget itself) -> ``modeman.enter(KeyMode.workspace_panel)``.
- Focus leaves the subtree back to the webview -> ``modeman.leave(
  KeyMode.workspace_panel)``.
- ``toggle()`` shows/hides the panel and moves focus accordingly:
  on show, focus moves to the last-focused descendant (or the panel
  itself on the first show); on hide, focus stays where it is so a
  pending webview state is preserved.
"""

from qutebrowser.qt.core import Qt, pyqtSlot
from qutebrowser.qt.widgets import QApplication, QVBoxLayout, QWidget

from qutebrowser.config import config
from qutebrowser.keyinput import modeman
from qutebrowser.utils import usertypes


class VerticalPanel(QWidget):

    """The left-side vertical workspace panel (Phase 2).

    Attributes:
        _win_id: The window id this panel belongs to.
        _last_focused: The most recently focused descendant of this
            panel; restored on the next ``toggle()`` show.
    """

    def __init__(self, win_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._win_id = win_id
        self._last_focused: QWidget | None = None
        # Tracks whether we already drove the mode into
        # KeyMode.workspace_panel via the most recent focus transition.
        # Focus changes WITHIN the panel subtree must not re-emit
        # enter / leave; only transitions across the panel boundary
        # count.
        self._in_mode = False

        self.setObjectName('vertical-panel')
        # The empty panel is focusable so the focus event filter can
        # already drive KeyMode.workspace_panel entry/exit before any
        # child widgets are added.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(
            Qt.WidgetAttribute.WA_MouseTracking, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._layout = layout

        self._apply_width()
        if config.val.verticalpanel.visible:
            self.show()
        else:
            self.hide()

        # Track every focus change in the application and decide
        # whether the new / old focus widget is inside this panel.
        # The signal lives on QApplication, not the panel, so we get
        # notifications about focus moving between the webview and
        # any child of the panel without per-child event filters.
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        config.instance.changed.connect(self._on_config_changed)

    def _on_config_changed(self, option: str) -> None:
        if option == 'verticalpanel.width':
            self._apply_width()
        elif option == 'verticalpanel.visible':
            if config.val.verticalpanel.visible:
                self.show()
            else:
                self.hide()

    def _apply_width(self) -> None:
        width = config.val.verticalpanel.width
        # The minimum is the user-configured value (so the panel can
        # never collapse below it). The maximum is generous so the
        # user can drag-resize well past the default if they want;
        # this matches the spirit of `tabs.width` (PercOrInt with
        # min/max bounds).
        self.setMinimumWidth(width)
        if width > 0:
            self.setMaximumWidth(width * 4)
        else:
            self.setMaximumWidth(16777215)  # QWIDGETSIZE_MAX

    @pyqtSlot()
    def toggle(self) -> None:
        """Toggle visibility and focus state per ADR 0008 § Panel focus."""
        if self.isVisible():
            self.hide()
            # Do not steal focus: the user toggled the panel off, so
            # they want to be back in the webview. The next focus
            # event the user produces (e.g. mouse click) will return
            # focus there.
        else:
            self.show()
            target = (
                self._last_focused
                if self._last_focused is not None
                else self
            )
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_focus_changed(
            self,
            old: QWidget | None,
            new: QWidget | None,
    ) -> None:
        """Drive KeyMode.workspace_panel from Qt focus events.

        Fires on every focus change in the application. The check
        ``_is_panel_descendant`` is O(depth) and runs at most once per
        real focus change, so the cost is negligible. Only the
        transition across the panel boundary emits a modeman call;
        focus changes that stay inside the panel subtree are
        suppressed via the ``_in_mode`` flag.
        """
        new_in_panel = self._is_panel_descendant(new) or new is self
        old_in_panel = self._is_panel_descendant(old) or old is self

        if new_in_panel and not self._in_mode:
            self._in_mode = True
            if new is not None and new is not self:
                self._last_focused = new
            modeman.enter(
                self._win_id,
                usertypes.KeyMode.workspace_panel,
                reason='vertical-panel focus-in',
            )
        elif not new_in_panel and old_in_panel and self._in_mode:
            self._in_mode = False
            modeman.leave(
                self._win_id,
                usertypes.KeyMode.workspace_panel,
                reason='vertical-panel focus-out',
            )

    def _is_panel_descendant(self, widget: QWidget | None) -> bool:
        """Return True if *widget* is a descendant of this panel."""
        if widget is None:
            return False
        if widget is self:
            return True
        candidate: QWidget | None = widget.parentWidget()
        while candidate is not None:
            if candidate is self:
                return True
            candidate = candidate.parentWidget()
        return False
