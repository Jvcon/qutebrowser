# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test setup for the workspaces package."""

import logging

import pytest


@pytest.fixture(autouse=True)
def _expect_palette_shift_warnings(caplog):
    """Allow the vertical panel's colour-shift warnings in workspace tests.

    Tests that build a TabTree hit the shipped default palette (white text on
    a white base), which logs a warning on the ``misc`` logger by design.  See
    ``tests/unit/mainwindow/conftest.py``.
    """
    caplog.set_level(logging.WARNING, logger='misc')
