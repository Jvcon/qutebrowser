# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Colour scheme, WCAG contrast and focus-ring helpers for the vertical panel.

Implements ADR 0008 § "Color scheme", § "Focus ring" and § "Workspace
monogram".  Every colour is derived from existing ``colors.*`` config keys so
the panel follows the user's theme:

- panel base:  ``colors.webpage.bg``
- foreground:  ``colors.tabs.odd.fg`` (``colors.webpage.fg`` does not exist)
- accent:      ``colors.tabs.indicator.start``

Blending is linear RGB: ``blend(base, overlay, a) = base*(1-a) + overlay*a``.

Contrast is WCAG 2.x relative luminance.  Text must reach 4.5:1 and icons,
graphics and the focus ring 3:1 against *every* background the element can
sit on (base, search field, row, hover and selected fills).  Colours that
fail are nudged toward the foreground until they pass.
"""

import functools
from collections.abc import Callable
from typing import Any, Optional

from qutebrowser.qt.core import QEvent, QObject, Qt
from qutebrowser.qt.gui import QColor, QFocusEvent
from qutebrowser.qt.widgets import QWidget

from qutebrowser.config import config
from qutebrowser.utils import log

#: WCAG AA minimum for body text.
TEXT_MIN_RATIO = 4.5
#: WCAG AA minimum for icons, graphics and the focus ring.
GRAPHIC_MIN_RATIO = 3.0

#: Blend amounts from ADR 0008 § "Color scheme".
ROW_AMOUNT = 0.05
#: ADR 0008 says 7 %.  Raised to 8 %, the largest whole step at which text
#: still reaches 4.5:1 on every palette in the tests (mid-grey fails at 9 %).
HOVER_AMOUNT = 0.08
SELECTED_AMOUNT = 0.12
SEARCH_AMOUNT = 0.03
#: Opacity amounts for the icons (ADR § "Status indicators" / § "Color scheme").
LOADING_AMOUNT = 0.40
SUSPENDED_AMOUNT = 0.50
#: Dimmed text (suspended / archived group labels, badges).
MUTED_AMOUNT = 0.60


def blend(base: QColor, overlay: QColor, amount: float) -> QColor:
    """Return ``base * (1 - amount) + overlay * amount`` per RGB channel."""
    return QColor(
        round(base.red() * (1 - amount) + overlay.red() * amount),
        round(base.green() * (1 - amount) + overlay.green() * amount),
        round(base.blue() * (1 - amount) + overlay.blue() * amount),
    )


def _channel_luminance(value: int) -> float:
    c = value / 255
    if c <= 0.03928:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: QColor) -> float:
    """Return the WCAG relative luminance of *color* (0 = black, 1 = white)."""
    return (
        0.2126 * _channel_luminance(color.red())
        + 0.7152 * _channel_luminance(color.green())
        + 0.0722 * _channel_luminance(color.blue())
    )


def contrast_ratio(first: QColor, second: QColor) -> float:
    """Return the WCAG contrast ratio between two colours (1 .. 21)."""
    lighter, darker = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def min_contrast(color: QColor, backgrounds: list[QColor]) -> float:
    """Return the lowest contrast ratio of *color* over *backgrounds*."""
    return min(contrast_ratio(color, bg) for bg in backgrounds)


def best_black_or_white(backgrounds: list[QColor]) -> QColor:
    """Return pure black or white, whichever has the higher worst-case ratio."""
    black = QColor(0, 0, 0)
    white = QColor(255, 255, 255)
    if min_contrast(black, backgrounds) >= min_contrast(white, backgrounds):
        return black
    return white


def ensure_contrast(
        color: QColor,
        backgrounds: list[QColor],
        minimum: float,
        toward: QColor,
) -> QColor:
    """Return *color*, shifted toward *toward* until it meets *minimum*.

    Shifting is done in 5 % steps.  If even the full shift to *toward* does
    not pass, *toward* is returned as the best available option.
    """
    if min_contrast(color, backgrounds) >= minimum:
        return color
    for step in range(1, 21):
        candidate = blend(color, toward, step * 0.05)
        if min_contrast(candidate, backgrounds) >= minimum:
            return candidate
    return toward


class Palette:

    """The resolved, contrast-checked colours of the vertical panel.

    Attributes:
        base: Panel / PIN background (``colors.webpage.bg``).
        text: Body text colour, checked against every fill.
        accent: Accent colour (active border, focus ring, search underline).
        search_fill: Search field background.
        row_fill: TabTree row background.
        hover_fill: Hovered row background.
        selected_fill: Selected row background.
        hover_marker: Hover bar colour.  It is the text colour, which already
            reaches 4.5:1 on every fill and so clears the 3:1 graphic minimum
            against both ``row_fill`` and ``hover_fill``.
        muted_text: Dimmed text (badges, suspended / archived labels).
        loading: Loading icon colour (text at 40 % opacity, icon contrast).
        suspended: Suspended icon colour (text at 50 % opacity, icon contrast).
        open: OPEN state glyph colour.
        archived: ARCHIVED state glyph colour.
        fresh: FRESH reading-status glyph colour.
        active: ACTIVE reading-status glyph colour (accent based).
        done: DONE reading-status glyph colour.
        pin: Pin badge colour (accent based).
        focus_outer: Outer focus-ring colour (accent).
        focus_inner: Inner focus-ring colour (text).
        fills: Every fill a foreground element may be drawn on.
    """

    def __init__(
            self,
            base: QColor,
            text: QColor,
            accent: QColor,
    ) -> None:
        self.base = base
        self.text = self._resolve_text(text, base)
        self.fills = self._fills(base, self.text)
        # The configured foreground may still fail against the fills (they
        # are blended toward it); if so, re-resolve against the fills.
        if min_contrast(self.text, self.fills) < TEXT_MIN_RATIO:
            self.text = best_black_or_white(self.fills)
            self.fills = self._fills(base, self.text)
        self.search_fill, self.row_fill, self.hover_fill, self.selected_fill = (
            self.fills[1:])
        toward = self.text
        # Text passes 4.5:1 on every fill, so it also passes the 3:1 graphic
        # minimum for the hover bar.
        self.hover_marker = self.text
        # The accent paints graphics (active bar, indicator border, checkbox,
        # drop wash), so it must reach 3:1 like every other icon colour.
        self.accent = ensure_contrast(
            QColor(accent), self.fills, GRAPHIC_MIN_RATIO, toward)

        self.muted_text = ensure_contrast(
            blend(self.base, self.text, MUTED_AMOUNT),
            self.fills, TEXT_MIN_RATIO, toward)

        def icon(color: QColor) -> QColor:
            return ensure_contrast(
                color, self.fills, GRAPHIC_MIN_RATIO, toward)

        self.loading = icon(blend(self.base, self.text, LOADING_AMOUNT))
        self.suspended = icon(blend(self.base, self.text, SUSPENDED_AMOUNT))
        self.open = icon(self.text)
        self.archived = icon(blend(self.base, self.text, MUTED_AMOUNT))
        self.fresh = icon(blend(self.base, self.text, 0.8))
        self.active = icon(self.accent)
        self.done = icon(blend(self.base, self.text, 0.45))
        self.pin = icon(self.accent)
        self.focus_outer = icon(self.accent)
        self.focus_inner = self.text

    @staticmethod
    def _resolve_text(text: QColor, base: QColor) -> QColor:
        """Return the configured foreground, or black / white if illegible.

        The ADR's default pair (``colors.tabs.odd.fg`` and
        ``colors.webpage.bg`` both ``white`` out of the box) fails WCAG, so
        the legible black or white is used instead.
        """
        if min_contrast(text, [base]) >= TEXT_MIN_RATIO:
            return QColor(text)
        return best_black_or_white([base])

    @staticmethod
    def _fills(base: QColor, text: QColor) -> list[QColor]:
        """Return ``[base, search, row, hover, selected]`` (ADR § Colour).

        Each fill is the ADR blend.  A fill may never push *text* below 4.5:1,
        so on a mid-tone base the blend is capped at the largest amount that
        still passes (WCAG wins over the 12 % selected step).
        """
        return [
            QColor(base),
            _capped_blend(base, text, SEARCH_AMOUNT),
            _capped_blend(base, text, ROW_AMOUNT),
            _capped_blend(base, text, HOVER_AMOUNT),
            _capped_blend(base, text, SELECTED_AMOUNT),
        ]

    @classmethod
    def from_config(cls) -> 'Palette':
        """Return the palette for the live ``colors.*`` config values.

        Results are cached per set of colour values, so repainting a long
        tree does not redo the contrast search for every row.  A config change
        yields new strings and therefore a fresh palette.
        """
        return _palette_for(
            _config_color('colors.webpage.bg', _DEFAULT_BASE).rgba(),
            _config_color('colors.tabs.odd.fg', _DEFAULT_TEXT).rgba(),
            _config_color('colors.tabs.indicator.start',
                          _DEFAULT_ACCENT).rgba(),
        )


#: Fallbacks used before the config is initialised (e.g. in unit tests that
#: build a panel widget without a config stub).  They match the shipped
#: defaults of the three keys.
_DEFAULT_BASE = QColor('white')
_DEFAULT_TEXT = QColor('white')
_DEFAULT_ACCENT = QColor('#0000aa')


def _config_color(name: str, fallback: QColor) -> QColor:
    """Return the colour *name* from the config cache, or *fallback*."""
    # ``config.cache`` is ``None`` until the config is initialised.
    cache: Any = config.cache
    if cache is None:
        return fallback
    return QColor(cache[name])


def _capped_blend(base: QColor, text: QColor, amount: float) -> QColor:
    """Return ``blend(base, text, amount)``, reduced until *text* passes.

    Steps the amount down in 1 % steps.  Returns *base* when no non-zero
    amount keeps the text legible (the fill then carries no tint).
    """
    steps = round(amount * 100)
    for percent in range(steps, 0, -1):
        candidate = blend(base, text, percent / 100)
        if contrast_ratio(text, candidate) >= TEXT_MIN_RATIO:
            return candidate
    return QColor(base)


#: Shift messages already logged this session.  Keyed by message text, so a
#: given configured/resolved pair is logged once even if the palette cache
#: evicts it and rebuilds it later.
_logged_shifts: set[str] = set()


def _holds_shipped_default(option: str) -> bool:
    """True when *option* still holds the value configdata ships.

    A displaced colour the user never chose is not their mistake and must not
    be reported at warning level -- out of the box ``colors.tabs.odd.fg`` and
    ``colors.webpage.bg`` are both ``white``, so a warning on every clean
    install would train users to ignore warnings.  A colour the user *did*
    configure and the panel then overrode is worth a warning.
    """
    try:
        current = QColor(config.val[option])
        default = QColor(config.get_opt(option).default)
    except Exception:  # pragma: no cover - stubbed config, unset / null value
        return False
    if not default.isValid():
        return False
    return current.name() == default.name()


def _log_shift_once(message: str, *, user_configured: bool) -> None:
    """Log *message* once, at warning level only for a user's own colour."""
    if message in _logged_shifts:
        return
    _logged_shifts.add(message)
    if user_configured:
        log.misc.warning(message)
    else:
        log.misc.debug(
            '%s (this is the shipped default; not changed by the user)',
            message)


def _report_shifts(
        configured_text: QColor,
        text: QColor,
        configured_accent: QColor,
        accent: QColor,
) -> None:
    """Report every configured colour the palette had to replace."""
    if accent.name() != configured_accent.name():
        _log_shift_once(
            f"The configured accent {configured_accent.name()} does not meet "
            "contrast requirements on the vertical panel; it is being "
            f"displayed as {accent.name()}. Consider changing "
            "colors.tabs.indicator.start or colors.webpage.bg.",
            user_configured=not _holds_shipped_default(
                'colors.tabs.indicator.start'),
        )
    if text.name() != configured_text.name():
        _log_shift_once(
            "The configured panel text colour "
            f"{configured_text.name()} (colors.tabs.odd.fg) is not legible on "
            "the vertical panel background (colors.webpage.bg), so the panel "
            f"draws its text as {text.name()} instead. This is expected when "
            "colors.tabs.odd.fg and colors.webpage.bg are equal, for example "
            "at their defaults; set them to contrasting colours to use your "
            "own text colour.",
            user_configured=not _holds_shipped_default('colors.tabs.odd.fg'),
        )


@functools.lru_cache(maxsize=16)
def _palette_for(base: int, text: int, accent: int) -> Palette:
    configured_text = QColor.fromRgba(text)
    configured_accent = QColor.fromRgba(accent)
    palette = Palette(QColor.fromRgba(base), configured_text,
                      configured_accent)
    # Runs once per cached configuration, never on the paint path.
    _report_shifts(configured_text, palette.text,
                   configured_accent, palette.accent)
    return palette


def monogram_colors(
        fill: QColor,
        base: QColor,
        text: QColor,
) -> tuple[QColor, QColor]:
    """Return a contrast-checked ``(fill, letter)`` pair for a monogram.

    The fill must reach 3:1 against the panel *base* (ADR § "Workspace
    monogram"); the letter must reach 4.5:1 against the fill.
    """
    checked_fill = ensure_contrast(fill, [base], GRAPHIC_MIN_RATIO, text)
    letter = best_black_or_white([checked_fill])
    return checked_fill, letter


# --- focus ring (ADR § "Focus ring") ---

#: Dynamic property set on a widget while it has *keyboard* focus.
KEYBOARD_FOCUS_PROPERTY = 'keyboardFocus'


def focus_ring_rule(widget_class: str) -> str:
    """Return the QSS rule for the keyboard focus ring of *widget_class*.

    The rule draws a 2 px solid accent border only while the widget has
    keyboard focus.  Callers that own a widget stylesheet (e.g. the workspace
    buttons) include this in it, since a later ``setStyleSheet`` replaces the
    whole sheet.
    """
    accent = Palette.from_config().focus_outer
    return (
        f'{widget_class}[{KEYBOARD_FOCUS_PROPERTY}="true"]:focus '
        f'{{ border: 2px solid {accent.name()}; }}')


class _KeyboardFocusTracker(QObject):

    """Mirrors "focused by the keyboard" into a dynamic widget property.

    The property :data:`KEYBOARD_FOCUS_PROPERTY` is ``True`` only when the
    widget gained focus for a non-mouse reason (Tab, Backtab, shortcut, or a
    programmatic ``setFocus``).  A mouse click therefore never shows a ring.
    """

    def __init__(self, widget: QWidget, *, stylesheet: bool,
                 field: bool) -> None:
        super().__init__(widget)
        self._widget = widget
        self._field = field
        widget.installEventFilter(self)
        widget.setProperty(KEYBOARD_FOCUS_PROPERTY, False)
        if stylesheet:
            self._apply_stylesheet()
            connect_colors_changed(self._on_config_changed)

    def _apply_stylesheet(self) -> None:
        widget_class = type(self._widget).__name__
        rules = [focus_ring_rule(widget_class)]
        if self._field:
            fill = Palette.from_config().search_fill
            rules.append(
                f'{widget_class} {{ background-color: {fill.name()}; }}')
        self._widget.setStyleSheet('\n'.join(rules))

    def _on_config_changed(self, option: str) -> None:
        if option.startswith('colors.'):
            self._apply_stylesheet()

    def _set_keyboard_focus(self, value: bool) -> None:
        self._widget.setProperty(KEYBOARD_FOCUS_PROPERTY, value)
        style = self._widget.style()
        if style is not None:
            style.unpolish(self._widget)
            style.polish(self._widget)
        self._widget.update()

    def eventFilter(  # noqa: N802 - Qt naming
            self, obj: Optional[QObject], event: Optional[QEvent]) -> bool:
        if obj is self._widget and isinstance(event, QFocusEvent):
            if event.type() == QEvent.Type.FocusIn:
                self._set_keyboard_focus(
                    event.reason() != Qt.FocusReason.MouseFocusReason)
            elif event.type() == QEvent.Type.FocusOut:
                self._set_keyboard_focus(False)
        return False


def connect_colors_changed(slot: Callable[[str], None]) -> None:
    """Call *slot* with the option name whenever a ``colors.*`` key changes.

    A no-op when the config is not initialised (unit tests that build panel
    widgets without a config stub); such widgets simply keep the colours they
    were styled with.
    """
    if config.instance is not None:
        config.instance.changed.connect(slot)


def install_focus_ring(
        widget: QWidget,
        *,
        stylesheet: bool = True,
        field: bool = False,
) -> None:
    """Show a visible focus ring on *widget* for keyboard focus only.

    With ``stylesheet=True`` (default) the widget gets its own ring rule,
    rebuilt when the ``colors`` config changes.  With ``stylesheet=False`` only
    the keyboard-focus property is tracked, and the caller includes
    :func:`focus_ring_rule` in the stylesheet it owns.  ``field=True`` also
    gives a text field the ADR search-field fill (ADR § "Color scheme").
    """
    _KeyboardFocusTracker(widget, stylesheet=stylesheet, field=field)
