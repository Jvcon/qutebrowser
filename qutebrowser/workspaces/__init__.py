# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Workspace, tab-group, and pinned-tab management."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qutebrowser.misc import savemanager


def init(
    save_manager: 'savemanager.SaveManager',
    profile_name: str = 'default',
) -> None:
    """Create the WorkspaceManager and register it as a Saveable.

    Called once during startup, after the QApplication exists, so that the
    manager can be loaded and registered with the ``SaveManager`` (which
    drives auto-saving via :attr:`WorkspaceManager.changed`).

    Args:
        save_manager: The application's save manager.
        profile_name: The name of the storage profile to back this
            WorkspaceManager with. Defaults to ``'default'``; future
            phases may pass a config-driven value once a
            ``profile-switch`` command lands (see
            ``docs/agents/upstream-tracking.md`` for the upstream #5935
            alignment plan).
    """
    from qutebrowser.utils import objreg
    from qutebrowser.workspaces.manager import WorkspaceManager
    from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                    WorkspacePersistence)

    persistence = WorkspacePersistence(profile_name=profile_name)
    pinned_persistence = PinnedPersistence(profile_name=profile_name)
    manager = WorkspaceManager(persistence, pinned_persistence)
    manager.load()
    save_manager.add_saveable('workspace-manager', manager.save,
                              manager.changed)
    objreg.register('workspace-manager', manager, update=True)
