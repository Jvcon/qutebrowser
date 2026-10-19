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
from qutebrowser.config import config
from qutebrowser.utils import message, objreg
from qutebrowser.workspaces import iconbytes, tabentry_fetch
from qutebrowser.workspaces.manager import (READING_STATUSES, TabMetadata,
                                            WorkspaceManager)
from qutebrowser.workspaces.model import Group, Workspace, WorkspaceTab


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


# VP-F1 (vp/13): tab collection commands. Helper `_resolve_group` is defined
# earlier in this module and resolves within the *given* workspace, returning
# a group UUID or None for "standalone".
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


# VP-F2 (vp/14): group management commands. The resolver is renamed to
# `_resolve_group_target` so it can coexist with the in-workspace
# `_resolve_group` defined above (different signatures, different scopes).
def _resolve_group_target(
    manager: WorkspaceManager,
    target: str,
) -> tuple[Workspace, Group]:
    """Resolve *target* (a group name or UUID) to ``(workspace, group)``.

    Group names are matched case-insensitively across every workspace.
    Archived groups are included so they can be restored or permanently
    deleted.

    Raises:
        cmdutils.CommandError: If no group matches, or if a name matches
            groups in more than one workspace.
    """
    target_lower = target.lower()
    try:
        target_uuid: uuid.UUID | None = uuid.UUID(target)
    except ValueError:
        target_uuid = None

    name_matches: list[tuple[Workspace, Group]] = []
    id_matches: list[tuple[Workspace, Group]] = []
    for candidate in manager.list_all_workspaces():
        # ``list_all_workspaces`` may return workspaces that are still only
        # on disk; make sure the manager caches the instance before we hand
        # it back for mutation.
        workspace = manager.get_workspace(candidate.id)
        if workspace is None:
            continue
        for group in workspace.groups:
            if target_uuid is not None and group.group_id == target_uuid:
                id_matches.append((workspace, group))
            if group.name.lower() == target_lower:
                name_matches.append((workspace, group))

    matches = id_matches or name_matches
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise cmdutils.CommandError(
            f"Group name '{target}' is ambiguous; use its UUID instead.")
    raise cmdutils.CommandError(f"No group '{target}' in any workspace.")


# TODO(vp-followup): add completion for group names + UUIDs once the group
# picker/completion model lands (mirrors the TODO in the #13 worktree).


@cmdutils.register(name='vp-group-new')
@cmdutils.argument('name', metavar='name')
def vp_group_new(name: str) -> None:
    """Create a new Group with the given name.

    The group is added to the active workspace. Group names must be unique
    across all workspaces (compared case-insensitively) so that they can be
    resolved unambiguously by the other ``:vp-group-*`` commands.

    Args:
        name: The name for the new group.
    """
    manager = _manager()
    workspace = manager.get_active()
    if workspace is None:
        raise cmdutils.CommandError(
            "No active workspace; create or switch to one first.")

    name_lower = name.lower()
    for candidate in manager.list_all_workspaces():
        for group in candidate.groups:
            if group.name.lower() == name_lower:
                raise cmdutils.CommandError(
                    f"A group with the name '{name}' already exists!")

    group = manager.create_group(workspace.id, name)
    message.info(f"Created group '{group.name}' (id {group.group_id}).")


@cmdutils.register(name='vp-group-rename')
@cmdutils.argument('target', metavar='target')
@cmdutils.argument('new_name', metavar='new-name')
def vp_group_rename(target: str, new_name: str) -> None:
    """Rename a Group. ``target`` may be a name or UUID.

    Args:
        target: The current name or UUID of the group.
        new_name: The new name for the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)

    name_lower = new_name.lower()
    for candidate in manager.list_all_workspaces():
        for other in candidate.groups:
            if other.group_id == group.group_id:
                continue
            if other.name.lower() == name_lower:
                raise cmdutils.CommandError(
                    f"A group with the name '{new_name}' already exists!")

    old_name = group.name
    manager.rename_group(workspace.id, group.group_id, new_name)
    message.info(f"Renamed group '{old_name}' to '{new_name}'.")


@cmdutils.register(name='vp-group-pin')
@cmdutils.argument('target', metavar='target')
def vp_group_pin(target: str) -> None:
    """Pin a Group so it appears first in the list.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    manager.pin_group(workspace.id, group.group_id)
    message.info(f"Pinned group '{group.name}'.")


@cmdutils.register(name='vp-group-unpin')
@cmdutils.argument('target', metavar='target')
def vp_group_unpin(target: str) -> None:
    """Unpin a Group.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    manager.unpin_group(workspace.id, group.group_id)
    message.info(f"Unpinned group '{group.name}'.")


@cmdutils.register(name='vp-group-archive')
@cmdutils.argument('target', metavar='target')
def vp_group_archive(target: str) -> None:
    """Archive a Group, hiding it without deleting its data.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    if group.archived:
        message.info(f"Group '{group.name}' is already archived.")
        return
    manager.archive_group(workspace.id, group.group_id)
    message.info(f"Archived group '{group.name}'.")


@cmdutils.register(name='vp-group-restore')
@cmdutils.argument('target', metavar='target')
def vp_group_restore(target: str) -> None:
    """Restore an archived Group.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    if not group.archived:
        message.info(f"Group '{group.name}' is not archived.")
        return
    manager.restore_group(workspace.id, group.group_id)
    message.info(f"Restored group '{group.name}'.")


@cmdutils.register(name='vp-group-delete')
@cmdutils.argument('target', metavar='target')
def vp_group_delete(target: str) -> None:
    """Permanently delete a Group and all its TabEntries.

    Requires confirmation. Unlike ``:vp-group-archive``, this cannot be
    undone.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    message.confirm_async(
        title=(f"Delete group '{group.name}' and all its tabs? "
               "This cannot be undone."),
        default=False,
        yes_action=_make_group_delete_action(manager, workspace, group),
    )


def _make_group_delete_action(
    manager: WorkspaceManager,
    workspace: Workspace,
    group: Group,
) -> Callable[[], None]:
    """Return the callback which cascade-deletes *group* after confirmation."""
    name = group.name
    group_id = group.group_id

    def delete() -> None:
        removed = manager.purge_group(workspace.id, group_id)
        message.info(f"Deleted group '{name}' ({removed} tabs).")

    return delete


# VP-F3 (vp/15): three-state reading status + auto-cleanup. The status
# commands target a single tab within the active workspace; VP-F4 reuses
# ``WorkspaceManager.set_reading_status`` for its multi-select UI.
_CYCLE_ORDER = {
    'FRESH': 'ACTIVE',
    'ACTIVE': 'DONE',
    'DONE': 'FRESH',
}

_CLEANUP_MIN_DAYS = 30
_CLEANUP_MAX_DAYS = 180


def _resolve_status_target(
    manager: WorkspaceManager,
    workspace: Workspace,
    target: str,
) -> WorkspaceTab:
    """Resolve *target* (an int tab id or a URL) to a workspace tab.

    An integer is looked up by ``tab_id``; anything else is treated as a URL
    and matched against the first tab whose ``url`` equals *target*.

    Raises:
        cmdutils.CommandError: If no tab matches.
    """
    try:
        tab_id = int(target)
    except ValueError:
        tab_id = None

    if tab_id is not None:
        tab = manager.find_tab_by_id(workspace.id, tab_id)
        if tab is None:
            raise cmdutils.CommandError(f"No tab with id {tab_id}.")
        return tab

    for tab in workspace.tabs:
        if tab.url == target:
            return tab
    raise cmdutils.CommandError(f"No tab matching URL '{target}'.")


@cmdutils.register(name='vp-status-set')
@cmdutils.argument('target', metavar='target')
@cmdutils.argument('status', metavar='status')
def vp_status_set(target: str, status: str) -> None:
    """Set the reading status of a tab.

    *target* is either a tab id (integer) or a URL. *status* is
    case-insensitive and must be one of ``fresh``, ``active`` or ``done``.

    Args:
        target: Tab id or URL of the tab to update.
        status: New reading status.
    """
    manager = _manager()
    workspace = _active_workspace(manager)

    normalized = status.upper()
    if normalized not in READING_STATUSES:
        raise cmdutils.CommandError(
            f"Unknown reading status '{status}'; expected one of "
            f"{', '.join(READING_STATUSES)}.")

    tab = _resolve_status_target(manager, workspace, target)
    manager.set_reading_status(workspace.id, [tab.tab_id], normalized)
    message.info(f"Set tab {tab.tab_id} status to {normalized}.")


@cmdutils.register(name='vp-status-cycle')
@cmdutils.argument('target', metavar='target')
def vp_status_cycle(target: str) -> None:
    """Cycle a tab's reading status FRESH -> ACTIVE -> DONE -> FRESH.

    Args:
        target: Tab id or URL of the tab to update.
    """
    manager = _manager()
    workspace = _active_workspace(manager)

    tab = _resolve_status_target(manager, workspace, target)
    current = getattr(tab, 'reading_status', 'FRESH')
    next_status = _CYCLE_ORDER.get(current, 'FRESH')
    manager.set_reading_status(workspace.id, [tab.tab_id], next_status)
    message.info(f"Cycled tab {tab.tab_id} status to {next_status}.")


@cmdutils.register(name='vp-cleanup-done')
@cmdutils.argument('days', metavar='days')
def vp_cleanup_done(days: int | None = None) -> None:
    """Remove ``DONE`` tabs which have gone untouched for a while.

    Only suspended tabs marked ``DONE`` older than the threshold are
    removed; open tabs and groups are left alone. When *days* is omitted the
    ``content.vertical_panel.auto_cleanup_days`` setting is used. A *days*
    value outside ``[30, 180]`` is ignored in favour of the configured
    default.

    Args:
        days: Optional retention window in days (30-180).
    """
    manager = _manager()
    workspace = _active_workspace(manager)

    configured = config.val.content.vertical_panel.auto_cleanup_days
    if days is None:
        effective = configured
    elif _CLEANUP_MIN_DAYS <= days <= _CLEANUP_MAX_DAYS:
        effective = days
    else:
        message.info(
            f"Days {days} out of range "
            f"({_CLEANUP_MIN_DAYS}-{_CLEANUP_MAX_DAYS}); using {configured}.")
        effective = configured

    removed = manager.cleanup_old_done(workspace.id, effective)
    message.info(f"Removed {removed} done tab(s).")
