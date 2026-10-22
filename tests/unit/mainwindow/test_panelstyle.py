# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""WCAG contrast checks for the vertical panel palette (ADR 0008 § Color)."""

import pytest

from qutebrowser.qt.gui import QColor

from qutebrowser.mainwindow import panelstyle
from qutebrowser.mainwindow.panelstyle import (GRAPHIC_MIN_RATIO,
                                               TEXT_MIN_RATIO, Palette)

#: (name, colors.webpage.bg, colors.tabs.odd.fg, colors.tabs.indicator.start)
THEMES = [
    # The shipped defaults: fg and bg are both ``white`` out of the box.
    ('default', '#ffffff', '#ffffff', '#0000aa'),
    ('dark', '#1e1e1e', '#d4d4d4', '#0000aa'),
    ('pure-dark', '#000000', '#ffffff', '#0000aa'),
    ('light', '#ffffff', '#222222', '#0000aa'),
    ('mid-grey', '#808080', '#777777', '#4444aa'),
    ('dark-blue-accent', '#101820', '#c8d0d8', '#1e3a8a'),
]


def _set_theme(config_stub, base, fg, accent):
    config_stub.set_obj('colors.webpage.bg', base)
    config_stub.set_obj('colors.tabs.odd.fg', fg)
    config_stub.set_obj('colors.tabs.indicator.start', accent)


def _text_ratios(palette):
    """Return {element: min ratio} for every text element of *palette*."""
    return {
        'text': panelstyle.min_contrast(palette.text, palette.fills),
        'muted_text': panelstyle.min_contrast(
            palette.muted_text, palette.fills),
    }


def _icon_ratios(palette):
    """Return {element: min ratio} for every icon / graphic element."""
    names = ('accent', 'loading', 'suspended', 'open', 'archived', 'fresh',
             'active', 'done', 'pin', 'focus_outer', 'focus_inner')
    return {
        name: panelstyle.min_contrast(getattr(palette, name), palette.fills)
        for name in names
    }


class TestContrastPrimitives:

    def test_black_on_white_is_21(self):
        ratio = panelstyle.contrast_ratio(QColor('black'), QColor('white'))
        assert ratio == pytest.approx(21.0, abs=0.01)

    def test_same_colour_is_1(self):
        ratio = panelstyle.contrast_ratio(QColor('#777777'),
                                          QColor('#777777'))
        assert ratio == pytest.approx(1.0)

    def test_contrast_is_symmetric(self):
        a, b = QColor('#336699'), QColor('#f0e0d0')
        assert (panelstyle.contrast_ratio(a, b)
                == pytest.approx(panelstyle.contrast_ratio(b, a)))

    def test_blend_is_linear_rgb(self):
        base, overlay = QColor(0, 0, 0), QColor(200, 100, 50)
        mixed = panelstyle.blend(base, overlay, 0.5)
        assert (mixed.red(), mixed.green(), mixed.blue()) == (100, 50, 25)

    def test_blend_endpoints(self):
        base, overlay = QColor('#102030'), QColor('#f0e0d0')
        assert panelstyle.blend(base, overlay, 0).name() == base.name()
        assert panelstyle.blend(base, overlay, 1).name() == overlay.name()

    def test_ensure_contrast_keeps_passing_colour(self):
        color = QColor('#000000')
        result = panelstyle.ensure_contrast(
            color, [QColor('white')], TEXT_MIN_RATIO, QColor('white'))
        assert result.name() == color.name()

    def test_ensure_contrast_shifts_failing_colour(self):
        grey = QColor('#8a8a8a')
        bg = [QColor('white')]
        assert panelstyle.min_contrast(grey, bg) < TEXT_MIN_RATIO
        result = panelstyle.ensure_contrast(
            grey, bg, TEXT_MIN_RATIO, QColor('black'))
        assert panelstyle.min_contrast(result, bg) >= TEXT_MIN_RATIO

    def test_best_black_or_white(self):
        assert panelstyle.best_black_or_white(
            [QColor('#101010')]).name() == '#ffffff'
        assert panelstyle.best_black_or_white(
            [QColor('#f0f0f0')]).name() == '#000000'


class TestPaletteContrast:

    @pytest.mark.parametrize(
        'name,base,fg,accent', THEMES, ids=[t[0] for t in THEMES])
    def test_text_meets_aa(self, config_stub, name, base, fg, accent):
        """Body and muted text reach 4.5:1 on every fill (ADR § Contrast)."""
        _set_theme(config_stub, base, fg, accent)
        palette = Palette.from_config()
        for element, ratio in _text_ratios(palette).items():
            assert ratio >= TEXT_MIN_RATIO, (name, element, ratio)

    @pytest.mark.parametrize(
        'name,base,fg,accent', THEMES, ids=[t[0] for t in THEMES])
    def test_icons_and_focus_ring_meet_3_to_1(
            self, config_stub, name, base, fg, accent):
        """Icons, glyphs, pin and focus ring reach 3:1 on every fill."""
        _set_theme(config_stub, base, fg, accent)
        palette = Palette.from_config()
        for element, ratio in _icon_ratios(palette).items():
            assert ratio >= GRAPHIC_MIN_RATIO, (name, element, ratio)

    def test_default_text_pair_falls_back_to_black(self, config_stub):
        """White-on-white defaults are not legible, so text falls back."""
        _set_theme(config_stub, '#ffffff', '#ffffff', '#0000aa')
        palette = Palette.from_config()
        assert palette.text.name() == '#000000'

    def test_configured_text_is_kept_when_legible(self, config_stub):
        _set_theme(config_stub, '#1e1e1e', '#d4d4d4', '#0000aa')
        palette = Palette.from_config()
        assert palette.text.name() == '#d4d4d4'

    def test_palette_follows_base_config(self, config_stub):
        """The fills derive from ``colors.webpage.bg``, not a constant."""
        _set_theme(config_stub, '#1e1e1e', '#d4d4d4', '#0000aa')
        dark = Palette.from_config()
        _set_theme(config_stub, '#fafafa', '#222222', '#0000aa')
        light = Palette.from_config()
        assert dark.base.name() == '#1e1e1e'
        assert light.base.name() == '#fafafa'
        assert dark.row_fill.name() != light.row_fill.name()

    def test_fill_steps_follow_adr(self, config_stub):
        """Row / hover / selected fills are blends at 5 / 8 / 12 %."""
        _set_theme(config_stub, '#000000', '#ffffff', '#0000aa')
        palette = Palette.from_config()
        assert palette.row_fill.name() == '#0d0d0d'      # 255 * 0.05 = 12.75
        assert palette.hover_fill.name() == '#141414'    # 255 * 0.08 = 20.4
        assert palette.selected_fill.name() == '#1f1f1f'  # 255 * 0.12 = 30.6

    @pytest.mark.parametrize(
        'name,base,fg,accent', THEMES, ids=[t[0] for t in THEMES])
    def test_hover_fill_keeps_full_hover_step(self, config_stub, name, base,
                                              fg, accent):
        """The 8 % hover step is applied unclamped on every theme.

        ``_capped_blend`` would silently shrink it if text failed 4.5:1, so
        compare against the raw blend.
        """
        _set_theme(config_stub, base, fg, accent)
        palette = Palette.from_config()
        raw = panelstyle.blend(palette.base, palette.text,
                               panelstyle.HOVER_AMOUNT)
        assert palette.hover_fill.name() == raw.name(), name

    @pytest.mark.parametrize(
        'name,base,fg,accent', THEMES, ids=[t[0] for t in THEMES])
    def test_hover_marker_reaches_3_to_1_on_both_fills(
            self, config_stub, name, base, fg, accent):
        """The hover bar contrasts with the plain and the hovered row."""
        _set_theme(config_stub, base, fg, accent)
        palette = Palette.from_config()
        for fill in (palette.row_fill, palette.hover_fill):
            ratio = panelstyle.contrast_ratio(palette.hover_marker, fill)
            assert ratio >= GRAPHIC_MIN_RATIO, (name, fill.name(), ratio)

    def test_palette_is_cached_per_colours(self, config_stub):
        _set_theme(config_stub, '#1e1e1e', '#d4d4d4', '#0000aa')
        assert Palette.from_config() is Palette.from_config()


class TestShiftWarnings:

    @pytest.fixture(autouse=True)
    def _fresh_palettes(self, monkeypatch):
        """Start each test with an empty palette cache and no logged shifts."""
        panelstyle._palette_for.cache_clear()
        monkeypatch.setattr(panelstyle, '_logged_shifts', set())
        yield
        panelstyle._palette_for.cache_clear()

    def _warnings(self, caplog):
        return [r.getMessage() for r in caplog.records
                if r.levelname == 'WARNING']

    def test_accent_shift_is_logged_with_both_values(
            self, config_stub, caplog):
        _set_theme(config_stub, '#1e1e1e', '#d4d4d4', '#0000aa')
        with caplog.at_level('WARNING'):
            palette = Palette.from_config()
        shown = palette.accent.name()
        assert shown != '#0000aa'
        messages = self._warnings(caplog)
        assert len(messages) == 1
        assert '#0000aa' in messages[0]
        assert shown in messages[0]
        assert 'colors.tabs.indicator.start' in messages[0]

    def test_no_warning_when_accent_passes(self, config_stub, caplog):
        _set_theme(config_stub, '#ffffff', '#222222', '#0000aa')
        with caplog.at_level('WARNING'):
            Palette.from_config()
        assert self._warnings(caplog) == []

    def test_default_text_fallback_is_logged(self, config_stub, caplog):
        _set_theme(config_stub, '#ffffff', '#ffffff', '#0000aa')
        with caplog.at_level('WARNING'):
            Palette.from_config()
        messages = self._warnings(caplog)
        assert any('colors.tabs.odd.fg' in m and '#ffffff' in m
                   and '#000000' in m for m in messages), messages
        assert any('are equal' in m for m in messages), messages

    def test_repeated_repaints_log_once(self, config_stub, caplog):
        _set_theme(config_stub, '#1e1e1e', '#d4d4d4', '#0000aa')
        with caplog.at_level('WARNING'):
            for _ in range(50):
                Palette.from_config()
        assert len(self._warnings(caplog)) == 1

    def test_log_survives_palette_cache_eviction(self, config_stub, caplog,
                                                 monkeypatch):
        """A rebuilt palette does not repeat a message already logged."""
        _set_theme(config_stub, '#1e1e1e', '#d4d4d4', '#0000aa')
        with caplog.at_level('WARNING'):
            Palette.from_config()
            panelstyle._palette_for.cache_clear()
            Palette.from_config()
        assert len(self._warnings(caplog)) == 1

    def test_shipped_default_colour_is_reported_below_warning(
            self, config_stub, caplog, monkeypatch):
        """A displaced colour the user never chose is not a warning.

        Out of the box ``colors.tabs.odd.fg`` and ``colors.webpage.bg`` are
        both ``white``, so the text fallback fires on every clean install.
        Warning about it would be unactionable noise that trains users to
        ignore warnings -- the same message is logged at debug instead.
        """
        monkeypatch.setattr(
            panelstyle, '_holds_shipped_default',
            lambda option: True,
        )
        with caplog.at_level('DEBUG', logger='misc'):
            _set_theme(config_stub, '#ffffff', '#ffffff', '#0000aa')
            Palette.from_config()
        debug_messages = [r.getMessage() for r in caplog.records
                          if r.levelname == 'DEBUG']
        assert any('colors.tabs.odd.fg' in m for m in debug_messages), \
            debug_messages
        assert self._warnings(caplog) == []

    def test_user_configured_colour_keeps_warning_level(
            self, config_stub, caplog, monkeypatch):
        """A colour the user actually set, and we overrode, still warns."""
        monkeypatch.setattr(
            panelstyle, '_holds_shipped_default',
            lambda option: False,
        )
        with caplog.at_level('WARNING'):
            _set_theme(config_stub, '#1e1e1e', '#d4d4d4', '#0000aa')
            Palette.from_config()
        messages = self._warnings(caplog)
        assert any('colors.tabs.indicator.start' in m for m in messages), \
            messages


class TestMonogramContrast:

    @pytest.mark.parametrize('fill', ['#ffffff', '#fafa00', '#333333',
                                      '#0000aa'])
    def test_fill_reaches_3_to_1_against_base(self, fill):
        base = QColor('#1e1e1e')
        checked, _letter = panelstyle.monogram_colors(
            QColor(fill), base, QColor('#d4d4d4'))
        assert panelstyle.contrast_ratio(
            checked, base) >= GRAPHIC_MIN_RATIO

    def test_letter_reaches_4_5_to_1_against_fill(self):
        fill, letter = panelstyle.monogram_colors(
            QColor('#808080'), QColor('#ffffff'), QColor('#000000'))
        assert panelstyle.contrast_ratio(letter, fill) >= TEXT_MIN_RATIO
