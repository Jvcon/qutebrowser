# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the default ``bindings.default.workspace_panel`` entries."""

import shlex

import pytest

# To run cmdutils.register decorators (including component commands like
# :reload, which are not loaded by importing qutebrowser.app alone).
from qutebrowser import app  # pylint: disable=unused-import
from qutebrowser.components import misccommands  # noqa: F401
from qutebrowser.config import configdata
from qutebrowser.misc import objects


def _panel_bindings(config_stub):
    return configdata.DATA['bindings.default'].default['workspace_panel']


def _mode_command_names(config_stub, mode: str) -> set[str]:
    bindings = configdata.DATA['bindings.default'].default[mode]
    names = {_bound_command_name(command) for command in bindings.values()}
    return {name for name in names if name}


def _bound_command_name(command: str) -> str:
    """Get the command a binding ultimately invokes.

    A ``cmd-set-text`` binding pre-fills the command line with the real
    command after the leading colon. Bindings which merely open the command
    line (``cmd-set-text :``) have no command name and return an empty
    string.
    """
    parts = shlex.split(command)
    if parts[0] == 'cmd-set-text':
        for part in parts[1:]:
            if part.startswith(':'):
                cmd = shlex.split(part[1:])
                return cmd[0] if cmd else ''
    return parts[0]


# qutebrowser spells the ADR's ``tab-reload`` command ``reload``.
ACTIVE_TAB_COMMANDS = [
    'tab-close',
    'tab-only',
    'tab-clone',
    'tab-move',
    'reload',
    'tab-move-to-group',
    'tab-collect',
    'vp-status-set',
    'vp-status-cycle',
    'vp-select-toggle',
]


@pytest.mark.parametrize('key, expected', [
    ('d', 'tab-close'),
    ('<Ctrl-W>', 'tab-close'),
    ('co', 'tab-only'),
    ('gc', 'cmd-set-text -s :tab-collect'),
    ('gG', 'cmd-set-text -s :tab-move-to-group'),
    ('gC', 'tab-clone'),
    ('gm', 'tab-move'),
    ('gs', 'cmd-set-text -s :vp-status-set'),
    ('gv', 'cmd-set-text -s :vp-status-cycle'),
    ('gz', 'cmd-set-text -s :vp-select-toggle'),
    ('r', 'reload'),
    ('gS', 'pintab-style-toggle'),
    ('gwc', 'cmd-set-text -s :workspace-create'),
    ('gwr', 'cmd-set-text -s :workspace-rename'),
    ('gwd', 'cmd-set-text -s :workspace-delete'),
    ('gwa', 'cmd-set-text -s :workspace-archive'),
    ('gwu', 'cmd-set-text -s :workspace-unarchive'),
    ('gws', 'cmd-set-text -s :workspace-switch'),
    ('gwi', 'cmd-set-text -s :vp-workspace-import'),
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


@pytest.mark.parametrize('command', ACTIVE_TAB_COMMANDS)
def test_active_tab_command_is_dual_registered(config_stub, command):
    """Each active-tab command is bound in both normal and workspace_panel."""
    assert command in _mode_command_names(config_stub, 'normal')
    assert command in _mode_command_names(config_stub, 'workspace_panel')


def test_panel_filter_binding(config_stub):
    """``/`` opens the transient filter row (ADR 0008 § 2)."""
    assert _panel_bindings(config_stub)['/'] == 'panel-filter'
    assert 'panel-filter' in objects.commands


def test_all_panel_bindings_resolve(config_stub):
    """Every default workspace_panel binding invokes a registered command."""
    for key, command in _panel_bindings(config_stub).items():
        name = _bound_command_name(command)
        assert name in objects.commands, f'{key} -> {command}'
