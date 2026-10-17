# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for workspace Saveable registration in qutebrowser.workspaces."""

import unittest.mock

import pytest

from qutebrowser.misc import savemanager
from qutebrowser.utils import objreg
from qutebrowser import workspaces


@pytest.fixture(autouse=True)
def cleanup_workspace_manager():
    """Make sure the global workspace-manager registration is cleaned up."""
    yield
    try:
        objreg.delete('workspace-manager')
    except KeyError:
        pass


def test_init_registers_saveable(fake_save_manager, data_tmpdir, qapp):
    workspaces.init(fake_save_manager)

    fake_save_manager.add_saveable.assert_called_with(
        'workspace-manager', unittest.mock.ANY, unittest.mock.ANY)
    assert objreg.get('workspace-manager') is not None


def test_changed_signal_is_wired_to_saveable(fake_save_manager, data_tmpdir,
                                             qapp):
    workspaces.init(fake_save_manager)
    manager = objreg.get('workspace-manager')

    name, save, changed = fake_save_manager.add_saveable.call_args[0]
    assert name == 'workspace-manager'
    assert save == manager.save

    # PyQt creates a new bound-signal wrapper per attribute access, so verify
    # identity behaviorally: a slot connected to the registered signal fires
    # when the manager emits.
    spy = unittest.mock.Mock()
    changed.connect(spy)
    manager.changed.emit()
    spy.assert_called_once()


def test_changed_emission_triggers_saveable_save(fake_save_manager, data_tmpdir,
                                                 qapp):
    workspaces.init(fake_save_manager)
    manager = objreg.get('workspace-manager')
    _, _, changed = fake_save_manager.add_saveable.call_args[0]

    # Reproduce the Saveable contract: the signal marks the saveable dirty,
    # and the next save() call invokes our handler.
    save_spy = unittest.mock.Mock()
    saveable = savemanager.Saveable('workspace-manager', save_spy, changed)
    manager.changed.emit()
    saveable.save()
    save_spy.assert_called_once()


def test_configinit_no_longer_wires_workspaces():
    from qutebrowser.config import configinit
    assert not hasattr(configinit, '_init_workspace_saveable')
