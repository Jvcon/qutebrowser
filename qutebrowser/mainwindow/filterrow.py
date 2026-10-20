# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The vertical panel's transient search/filter row (ADR 0008 § 2).

A :class:`FilterRow` is a 28 px-high ``QLineEdit`` inserted above the
TabTree. It is hidden until opened (the ``/`` key in ``workspace_panel``
mode); it closes on ``Esc`` or when its text is cleared. Each text change
is re-emitted as :attr:`filter_changed` so the host (VerticalPanel) can
push the filter into the PIN and TabTree models.
"""

from qutebrowser.qt.core import Qt, pyqtSignal
from qutebrowser.qt.widgets import QLineEdit


class FilterRow(QLineEdit):

    """The transient filter input above the TabTree.

    Attributes:
        filter_changed: Emitted with the current filter text whenever it
            changes (suppressed while the row is closing itself).
        closed: Emitted when the row hides (``Esc`` or cleared field).
    """

    filter_changed = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._closing = False
        self.setObjectName('filter-row')
        self.setPlaceholderText('/ search...')
        self.setFixedHeight(28)
        self.hide()
        self.textChanged.connect(self._on_text_changed)

    def open(self) -> None:
        """Clear, show, and focus the row."""
        self._closing = True
        try:
            self.clear()
        finally:
            self._closing = False
        self.show()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_filter(self) -> None:
        """Clear and hide the row, emitting :attr:`closed`."""
        if self._closing:
            return
        self._closing = True
        try:
            self.clear()
            self.hide()
        finally:
            self._closing = False
        self.closed.emit()

    def _on_text_changed(self, text: str) -> None:
        if self._closing:
            return
        self.filter_changed.emit(text)
        if not text.strip():
            self.close_filter()

    def keyPressEvent(  # noqa: N802 - Qt naming
            self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_filter()
            event.accept()
            return
        super().keyPressEvent(event)
