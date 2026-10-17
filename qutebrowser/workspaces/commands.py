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

import uuid
from collections.abc import Callable
from typing import Any, Optional

from qutebrowser.api import cmdutils
from qutebrowser.utils import message, objreg
from qutebrowser.workspaces import iconbytes, tabentry_fetch
from qutebrowser.workspaces.manager import TabMetadata, WorkspaceManager
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


def _active_workspace(manager: WorkspaceManager) -> Workspace:
    """Return the active workspace, raising a CommandError if there is none."""
    workspace = manager.get_active()
    if workspace is None:
        raise cmdutils.CommandError(
            "No active workspace. Create one with :workspace-create first.")
    return workspace


def _resolve_group(
    manager: WorkspaceManager,
    workspace: Workspace,
    group_ref: str,
) -> Optional[uuid.UUID]:
    """Resolve *group_ref* (UUID string or group name) to a group id.

    An empty reference means "standalone" and returns None.
    """
    if not group_ref:
        return None

    groups = manager.list_groups(workspace.id)
    try:
        candidate = uuid.UUID(group_ref)
    except ValueError:
        candidate = None
    if candidate is not None:
        for group in groups:
            if group.group_id == candidate:
                return group.group_id

    wanted = group_ref.lower()
    for group in groups:
        if group.name.lower() == wanted:
            return group.group_id
    raise cmdutils.CommandError(f"No group named '{group_ref}'")


def _metadata_from_tab(tab: Any) -> TabMetadata:
    """Capture URL/title/source/favicon from a live tab, best-effort."""
    try:
        url = tab.url().toString()
    except (AttributeError, NotImplementedError):
        url = ''
    try:
        title = tab.title()
    except (AttributeError, NotImplementedError):
        title = ''
    try:
        favicon = iconbytes.icon_to_png_base64(tab.icon())
    except (AttributeError, NotImplementedError):
        favicon = ''
    return TabMetadata(
        url=url,
        title=title,
        favicon=favicon or None,
    )


def _persist_description(
    manager: WorkspaceManager,
    workspace_id: uuid.UUID,
    tab_id: int,
    tab: Any,
) -> None:
    """Kick off an async description fetch and persist the result."""

    def update(description: str) -> None:
        manager.update_tab_metadata(
            workspace_id, tab_id, description=description)

    def on_error(error: str) -> None:
        manager.update_tab_metadata(
            workspace_id, tab_id, description='', error=error)

    try:
        tabentry_fetch.fetch_description(tab, update, on_error=on_error)
    except Exception as e:  # noqa: BLE001 - metadata is best-effort
        on_error(str(e)[:200])


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


# TODO(vp14): add completion listing group names + UUIDs once the group
# picker/completion model lands.
@cmdutils.register(name='tab-collect')
@cmdutils.argument('tab', value=cmdutils.Value.cur_tab)
@cmdutils.argument('group_id', metavar='group')
def tab_collect(tab: Any, group_id: str = '') -> None:
    """Collect the current tab into a group, capturing its metadata.

    Args:
        group_id: Group name or UUID. When omitted, the tab is collected as
            a standalone entry.
    """
    manager = _manager()
    workspace = _active_workspace(manager)
    group = _resolve_group(manager, workspace, group_id)

    metadata = _metadata_from_tab(tab)
    collected = manager.collect_tab(
        workspace.id, tab.tab_id, group, metadata=metadata)
    _persist_description(manager, workspace.id, collected.tab_id, tab)

    if group_id:
        message.info(f"Collected tab into group '{group_id}'.")
    else:
        message.info("Collected tab.")


@cmdutils.register(name='vp-collect-all')
@cmdutils.argument('group_id', metavar='group')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_collect_all(group_id: str, win_id: int = 0) -> None:
    """Collect every collectable tab in the current window into a group.

    Browser-internal pages (``qute://*``, ``about:blank``, ``chrome://*``,
    ``view-source:*``, ...) are skipped silently.

    Args:
        group_id: Group name or UUID to collect the tabs into.
    """
    manager = _manager()
    workspace = _active_workspace(manager)
    group = _resolve_group(manager, workspace, group_id)

    try:
        registry = objreg.get(
            'tab-registry', scope='window', window=win_id)
    except KeyError:
        registry = {}

    tabs = list(registry.values())
    metadata = {tab.tab_id: _metadata_from_tab(tab) for tab in tabs}
    collected, skipped = manager.collect_all_in_window(
        workspace.id,
        group,
        [tab.tab_id for tab in tabs],
        tab_metadata=metadata,
    )

    if not collected and not skipped:
        message.info("No collectable tabs in this window.")
        return

    for tab in tabs:
        if tab.tab_id in collected:
            _persist_description(manager, workspace.id, tab.tab_id, tab)

    message.info(
        f"Collected {len(collected)} tab(s) into '{group_id}'; "
        f"skipped {len(skipped)}.")
