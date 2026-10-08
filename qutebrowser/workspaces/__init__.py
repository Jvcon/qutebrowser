# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Workspace, tab-group, and pinned-tab management."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qutebrowser.misc import savemanager


def init(save_manager: 'savemanager.SaveManager') -> None:
    """Create the WorkspaceManager and register it as a Saveable.

    Called once during startup, after the QApplication exists, so that the
    manager can be loaded and registered with the ``SaveManager`` (which
    drives auto-saving via :attr:`WorkspaceManager.changed`).
    """
    from qutebrowser.utils import objreg
    from qutebrowser.workspaces.manager import WorkspaceManager
    from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                    WorkspacePersistence)

    persistence = WorkspacePersistence()
    pinned_persistence = PinnedPersistence()
    manager = WorkspaceManager(persistence, pinned_persistence)
    manager.load()
    save_manager.add_saveable('workspace-manager', manager.save,
                              manager.changed)
    objreg.register('workspace-manager', manager, update=True)
