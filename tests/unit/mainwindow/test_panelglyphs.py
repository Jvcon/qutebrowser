# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the inline SVG state glyphs (issue #37, ADR 0008)."""

import pytest

from qutebrowser.qt.core import QRect
from qutebrowser.qt.gui import QColor, QImage, QPainter, QPixmap

from qutebrowser.mainwindow import panelglyphs
from qutebrowser.mainwindow.panelglyphs import GLYPH_SIZE, Glyph


@pytest.fixture(autouse=True)
def _gui_app(qapp):
    """QPixmap and QImage rendering need a QGuiApplication."""
    assert qapp is not None


ALL_GLYPHS = [Glyph.OPEN, Glyph.SUSPENDED, Glyph.ARCHIVED,
              Glyph.FRESH, Glyph.ACTIVE, Glyph.DONE]


def _image_bits(pixmap):
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    return bytes(image.constBits().asstring(image.sizeInBytes()))


def test_there_are_exactly_six_glyphs():
    assert {g.value for g in Glyph} == {
        'OPEN', 'SUSPENDED', 'ARCHIVED', 'FRESH', 'ACTIVE', 'DONE'}


@pytest.mark.parametrize('glyph', ALL_GLYPHS, ids=lambda g: g.value)
def test_markup_is_pure_vector(glyph):
    """Inline SVG only: no embedded bitmap, no font glyph."""
    markup = panelglyphs.svg_markup(glyph, QColor('#123456'), 14).decode()
    assert markup.startswith('<svg ')
    assert '<image' not in markup
    assert 'data:image' not in markup
    assert '{color}' not in markup
    assert '#123456' in markup


@pytest.mark.parametrize('glyph', ALL_GLYPHS, ids=lambda g: g.value)
def test_renders_non_null(glyph):
    pixmap = panelglyphs.glyph_pixmap(glyph, QColor('#ff0000'))
    assert not pixmap.isNull()
    assert pixmap.width() == GLYPH_SIZE
    assert pixmap.height() == GLYPH_SIZE


def test_glyph_is_crisp_at_2x_device_pixel_ratio():
    """At 2x the raster is 28 px but the logical size stays 14 px."""
    pixmap = panelglyphs.glyph_pixmap(Glyph.DONE, QColor('#000000'), dpr=2.0)
    assert pixmap.devicePixelRatio() == pytest.approx(2.0)
    assert pixmap.width() == GLYPH_SIZE * 2
    assert pixmap.width() / pixmap.devicePixelRatio() == GLYPH_SIZE


@pytest.mark.parametrize('glyph', ALL_GLYPHS, ids=lambda g: g.value)
def test_glyph_takes_requested_colour(glyph):
    """The palette colour is substituted into the vector shape."""
    pixmap = panelglyphs.glyph_pixmap(glyph, QColor('#00ff00'))
    image = pixmap.toImage()
    # Anti-aliased edges are partly transparent, so match on RGB of any
    # painted pixel rather than on fully opaque ones.
    rgb = {
        (c.red(), c.green(), c.blue())
        for c in (image.pixelColor(x, y)
                  for x in range(image.width())
                  for y in range(image.height()))
        if c.alpha() > 0
    }
    assert rgb == {(0, 255, 0)}


def test_shapes_are_distinct():
    """Each state has its own silhouette, so colour is not the only cue."""
    alpha_masks = {}
    for glyph in ALL_GLYPHS:
        image = panelglyphs.glyph_pixmap(glyph, QColor('#000000')).toImage()
        alpha_masks[glyph] = tuple(
            image.pixelColor(x, y).alpha() > 127
            for y in range(image.height()) for x in range(image.width())
        )
    assert len(set(alpha_masks.values())) == len(ALL_GLYPHS)


def test_draw_glyph_paints_inside_rect():
    target = QPixmap(40, 40)
    target.fill(QColor('#ffffff'))
    painter = QPainter(target)
    try:
        panelglyphs.draw_glyph(
            painter, Glyph.OPEN, QRect(10, 10, 20, 20), QColor('#000000'))
    finally:
        painter.end()
    image = target.toImage()
    assert image.pixelColor(20, 20).name() == '#000000'
    assert image.pixelColor(2, 2).name() == '#ffffff'
