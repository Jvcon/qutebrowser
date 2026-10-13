# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""User-facing :workspace-* commands.

The commands in this module drive the app-wide
:class:`~qutebrowser.workspaces.manager.WorkspaceManager` (registered as
``'workspace-manager'`` in the object registry by
:func:`qutebrowser.workspaces.init`). The module is imported for its side
effects — the ``@cmdutils.register`` decorators run at import time — from
:mod:`qutebrowser.workspaces`.
"""

from collections.abc import Callable

from qutebrowser.api import cmdutils
from qutebrowser.utils import message, objreg
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import Workspace


def _manager() -> WorkspaceManager:
    """Return the app-wide WorkspaceManager."""
    return objreg.get('workspace-manager')


def _lookup(name: str, manager: WorkspaceManager) -> Workspace:
    """Resolve *name* to a workspace, raising a CommandError if unknown."""
    workspace = manager.get_workspace_by_name(name)
    if workspace is None:
        raise cmdutils.CommandError(f"No workspace with the name '{name}'!")
    return workspace


@cmdutils.register(name='workspace-create')
def workspace_create(name: str) -> None:
    """Create a new workspace with the given name.

    The workspace is registered with a generated UUID and the current
    ISO 8601 timestamp. It becomes the active workspace when no other
    workspace exists yet. Creating a workspace whose name already
    exists (compared case-insensitively) is refused.

    Args:
        name: The name for the new workspace.
    """
    manager = _manager()
    if manager.get_workspace_by_name(name) is not None:
        raise cmdutils.CommandError(
            f"A workspace with the name '{name}' already exists!")
    workspace = manager.add_workspace(name)
    message.info(f"Created workspace '{workspace.name}' (id {workspace.id}).")


@cmdutils.register(name='workspace-list')
def workspace_list() -> None:
    """Show all workspaces, with the active workspace marked.

    Workspaces are listed one per line: the active workspace is prefixed
    with ``*``, archived workspaces carry a trailing ``(archived)``
    marker.
    """
    manager = _manager()
    active = manager.get_active()
    lines = []
    for workspace in manager.list_all_workspaces():
        marker = '*' if (active is not None
                         and workspace.id == active.id) else ' '
        suffix = ' (archived)' if workspace.archived else ''
        lines.append(f"{marker} {workspace.name}{suffix}")
    if lines:
        message.info('\n'.join(lines))
    else:
        message.info('No workspaces yet.')


@cmdutils.register(name='workspace-rename')
def workspace_rename(old_name: str, new_name: str) -> None:
    """Rename an existing workspace.

    Renaming a workspace to a name which is already taken by a different
    workspace (compared case-insensitively) is refused.

    Args:
        old_name: The current name of the workspace to rename.
        new_name: The new name for the workspace.
    """
    manager = _manager()
    workspace = _lookup(old_name, manager)
    collision = manager.get_workspace_by_name(new_name)
    if collision is not None and collision.id != workspace.id:
        raise cmdutils.CommandError(
            f"A workspace with the name '{new_name}' already exists!")
    if collision is not None and collision.id == workspace.id:
        # Renaming to the same name (possibly different case) is a no-op.
        return
    manager.rename_workspace(workspace.id, new_name)
    message.info(f"Renamed workspace '{old_name}' to '{new_name}'.")


@cmdutils.register(name='workspace-archive')
def workspace_archive(name: str) -> None:
    """Mark a workspace as archived without deleting it.

    Args:
        name: The name of the workspace to archive.
    """
    manager = _manager()
    workspace = _lookup(name, manager)
    if workspace.archived:
        message.info(f"Workspace '{name}' is already archived.")
        return
    manager.archive_workspace(workspace.id)
    message.info(f"Archived workspace '{name}'.")


@cmdutils.register(name='workspace-delete')
def workspace_delete(name: str) -> None:
    """Delete a workspace, after confirmation.

    Deleting the active workspace is refused.

    Args:
        name: The name of the workspace to delete.
    """
    manager = _manager()
    workspace = _lookup(name, manager)
    active = manager.get_active()
    if active is not None and active.id == workspace.id:
        raise cmdutils.CommandError(
            f"Can't delete the active workspace '{name}'!")
    message.confirm_async(
        title=f"Delete workspace '{name}'?",
        default=False,
        yes_action=_make_delete_action(manager, workspace, name),
    )


def _make_delete_action(
    manager: WorkspaceManager,
    workspace: Workspace,
    name: str,
) -> Callable[[], None]:
    """Return the callback which deletes *workspace* after confirmation."""

    def delete() -> None:
        manager.delete_workspace(workspace.id)
        message.info(f"Deleted workspace '{name}'.")

    return delete
