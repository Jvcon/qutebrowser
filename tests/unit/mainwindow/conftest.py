# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test setup for the mainwindow package."""

import logging

import pytest


@pytest.fixture(autouse=True)
def _config_isolation(config_stub):
    """Give every mainwindow test its own config instance and cache.

    The vertical panel widgets resolve their palette through
    ``config.cache`` at construction time, exactly like
    ``qutebrowser/browser/hints.py`` does.  ``config_stub`` is what supplies
    a real ``config.instance`` and a fresh ``ConfigCache``; without it a test
    that happens to run after a ``config_stub`` test sees ``config.instance``
    reset to ``None`` by monkeypatch teardown and dies with
    ``'NoneType' object has no attribute 'get_opt'`` -- or, when
    ``tests/unit/config`` runs first, with ``KeyError: 'colors.webpage.bg'``.
    Both make the panel tests order-dependent, which is how the whole suite
    went red while ``pytest tests/unit/mainwindow`` alone stayed green.
    """


@pytest.fixture(autouse=True)
def _expect_palette_shift_warnings(caplog):
    """Allow the vertical panel's colour-shift warnings in mainwindow tests.

    :mod:`qutebrowser.mainwindow.panelstyle` logs a warning on the ``misc``
    logger when a configured accent or text colour fails WCAG on the panel.
    The shipped default (white text on a white base) does, and most panel
    tests build that palette, so the warning is expected here.  Its wording is
    asserted by ``TestShiftWarnings`` in ``test_panelstyle.py``.
    """
    caplog.set_level(logging.WARNING, logger='misc')
