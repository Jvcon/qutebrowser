# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Inline SVG state glyphs for the vertical panel (ADR 0008; issue #37).

Six glyphs mark a row's state:

- :attr:`Glyph.OPEN` / :attr:`Glyph.SUSPENDED` / :attr:`Glyph.ARCHIVED` --
  the resolved group state (``Group.state``) on group header rows.
- :attr:`Glyph.FRESH` / :attr:`Glyph.ACTIVE` / :attr:`Glyph.DONE` -- the
  reading status (``WorkspaceTab.reading_status``) on tab rows.

Each glyph is stored as SVG markup in this module (not a font icon and not a
bitmap) and rendered at the paint size times the device pixel ratio, so it
stays crisp at any scale.  The markup is loaded through the same
``QImage.fromData(..., 'SVG')`` path the repo already uses for SVG icons
(see ``qutebrowser/browser/webengine/notification.py``).  Colours are
substituted into the markup at render time, so the glyphs follow the
palette in :mod:`qutebrowser.mainwindow.panelstyle`.

Every glyph is distinguished by *shape*, not colour alone:

========== ==========================================
OPEN       filled disc
SUSPENDED  hollow ring
ARCHIVED   box with a lid
FRESH      four-point sparkle
ACTIVE     half-filled disc
DONE       check mark
========== ==========================================
"""

import enum
import functools

from qutebrowser.qt.gui import QColor, QImage, QPainter, QPixmap
from qutebrowser.qt.core import QRect

#: Logical size of a glyph in device-independent pixels.
GLYPH_SIZE = 14

_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
    'viewBox="0 0 16 16">{body}</svg>'
)

_BODIES = {
    'OPEN': (
        '<circle cx="8" cy="8" r="4.5" fill="{color}"/>'
    ),
    'SUSPENDED': (
        '<circle cx="8" cy="8" r="3.8" fill="none" stroke="{color}" '
        'stroke-width="1.6"/>'
    ),
    'ARCHIVED': (
        '<rect x="2" y="3" width="12" height="3" rx="0.6" fill="{color}"/>'
        '<path d="M3.2 6.2h9.6v6.8H3.2z" fill="none" stroke="{color}" '
        'stroke-width="1.4" stroke-linejoin="round"/>'
        '<path d="M6.5 9h3" stroke="{color}" stroke-width="1.4" '
        'stroke-linecap="round"/>'
    ),
    'FRESH': (
        # Inner vertices sit 1.2 (was 1.7) from centre: fatter arms, smaller
        # void, so it reads as a star rather than a diamond at 14 px.
        '<path d="M8 1.5L9.2 6.8L14.5 8L9.2 9.2L8 14.5L6.8 9.2L1.5 8L6.8 6.8Z" '
        'fill="{color}"/>'
    ),
    'ACTIVE': (
        '<circle cx="8" cy="8" r="4.2" fill="none" stroke="{color}" '
        'stroke-width="1.4"/>'
        '<path d="M8 3.8A4.2 4.2 0 0 1 8 12.2Z" fill="{color}"/>'
    ),
    'DONE': (
        '<path d="M3.2 8.6L6.6 11.8L12.8 4.6" fill="none" stroke="{color}" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
    ),
}


@enum.unique
class Glyph(enum.Enum):

    """The six state glyphs.  The value is the key into ``_BODIES``."""

    OPEN = 'OPEN'
    SUSPENDED = 'SUSPENDED'
    ARCHIVED = 'ARCHIVED'
    FRESH = 'FRESH'
    ACTIVE = 'ACTIVE'
    DONE = 'DONE'


def svg_markup(glyph: Glyph, color: QColor, px: int) -> bytes:
    """Return the SVG document for *glyph* in *color*, *px* device pixels."""
    body = _BODIES[glyph.value].replace('{color}', color.name())
    return _SVG_TEMPLATE.format(px=px, body=body).encode('utf-8')


@functools.lru_cache(maxsize=256)
def _rendered(glyph: Glyph, rgba: int, px: int) -> QImage:
    color = QColor.fromRgba(rgba)
    image = QImage.fromData(svg_markup(glyph, color, px), 'SVG')
    if image.isNull():  # pragma: no cover - the markup is static and valid
        raise RuntimeError(f"Could not render glyph {glyph.value}")
    return image


def glyph_pixmap(glyph: Glyph, color: QColor, dpr: float = 1.0) -> QPixmap:
    """Return a :data:`GLYPH_SIZE` glyph rendered for device ratio *dpr*.

    The SVG is rasterised at the device size, so a HiDPI screen gets a
    sharp bitmap at the native resolution (the logical size stays
    :data:`GLYPH_SIZE`).
    """
    px = max(1, round(GLYPH_SIZE * dpr))
    image = _rendered(glyph, color.rgba(), px)
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def draw_glyph(
        painter: QPainter,
        glyph: Glyph,
        rect: QRect,
        color: QColor,
) -> None:
    """Draw *glyph* centred inside *rect* at the painter's device ratio."""
    device = painter.device()
    dpr = device.devicePixelRatioF() if device is not None else 1.0
    pixmap = glyph_pixmap(glyph, color, dpr)
    x = rect.x() + (rect.width() - GLYPH_SIZE) // 2
    y = rect.y() + (rect.height() - GLYPH_SIZE) // 2
    painter.drawPixmap(x, y, pixmap)
