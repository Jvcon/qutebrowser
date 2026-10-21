# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the default ``bindings.default.workspace_panel`` entries."""

import shlex

import pytest

# To run cmdutils.register decorators
from qutebrowser import app  # pylint: disable=unused-import
from qutebrowser.config import configdata
from qutebrowser.misc import objects


def _panel_bindings(config_stub):
    return configdata.DATA['bindings.default'].default['workspace_panel']


def _bound_command_name(command: str) -> str:
    """Get the command a binding ultimately invokes.

    A ``cmd-set-text`` binding pre-fills the command line with the real
    command after the leading colon.
    """
    parts = shlex.split(command)
    if parts[0] == 'cmd-set-text':
        for part in parts[1:]:
            if part.startswith(':'):
                return shlex.split(part[1:])[0]
    return parts[0]


@pytest.mark.parametrize('key, expected', [
    ('gS', 'pintab-style-toggle'),
    ('gwc', 'cmd-set-text -s :workspace-create'),
    ('gwr', 'cmd-set-text -s :workspace-rename'),
    ('gwd', 'cmd-set-text -s :workspace-delete'),
    ('gwa', 'cmd-set-text -s :workspace-archive'),
    ('gwu', 'cmd-set-text -s :workspace-unarchive'),
    ('gws', 'cmd-set-text -s :workspace-switch'),
    ('ggl', 'group-list'),
    ('ggn', 'cmd-set-text -s :vp-group-new'),
    ('ggr', 'cmd-set-text -s :vp-group-rename'),
    ('ggp', 'cmd-set-text -s :vp-group-pin'),
    ('ggP', 'cmd-set-text -s :vp-group-unpin'),
    ('gga', 'cmd-set-text -s :vp-group-archive'),
    ('ggR', 'cmd-set-text -s :vp-group-restore'),
    ('ggd', 'cmd-set-text -s :vp-group-delete'),
    ('ggc', 'cmd-set-text -s :vp-group-close'),
    ('ggo', 'cmd-set-text -s :vp-group-reopen'),
    ('gX', 'vp-cleanup-done'),
    ('gbm', 'cmd-set-text -s :vp-batch-move'),
    ('gbs', 'cmd-set-text -s :vp-batch-status'),
    ('/', 'panel-filter'),
])
def test_panel_binding_resolves(config_stub, key, expected):
    """Each new panel binding points at a real, registered command."""
    bindings = _panel_bindings(config_stub)
    assert bindings[key] == expected
    assert _bound_command_name(bindings[key]) in objects.commands


def test_panel_filter_binding(config_stub):
    """``/`` opens the transient filter row (ADR 0008 § 2)."""
    assert _panel_bindings(config_stub)['/'] == 'panel-filter'
    assert 'panel-filter' in objects.commands


def test_all_panel_bindings_resolve(config_stub):
    """Every default workspace_panel binding invokes a registered command."""
    for key, command in _panel_bindings(config_stub).items():
        name = _bound_command_name(command)
        assert name in objects.commands, f'{key} -> {command}'
