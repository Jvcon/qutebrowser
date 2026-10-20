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

import base64
import uuid
from collections.abc import Callable
from typing import Any, Optional

from qutebrowser.api import cmdutils
from qutebrowser.config import config
from qutebrowser.utils import message, objreg
from qutebrowser.utils.utils import format_size
from qutebrowser.workspaces import iconbytes, tabentry_fetch
from qutebrowser.workspaces.manager import (READING_STATUSES, TabMetadata,
                                            WorkspaceManager)
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)

#: Name of the default group collect commands land in when none is given.
INBOX_GROUP_NAME = 'Inbox'


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


def _tab_tree(win_id: int) -> Any:
    """Return the window's TabTree, raising a CommandError if unavailable."""
    try:
        return objreg.get('tab-tree', scope='window', window=win_id)
    except (KeyError, objreg.RegistryUnavailableError) as e:
        raise cmdutils.CommandError(
            "The tab tree is not available in this window.") from e


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
        group_id: Group name or UUID. When omitted, the tab is collected
            into the workspace's Inbox group (created on demand).
    """
    manager = _manager()
    workspace = _active_workspace(manager)
    if group_id:
        group = _resolve_group(manager, workspace, group_id)
        target_name = group_id
    else:
        inbox = manager.get_or_create_inbox(workspace.id)
        group = inbox.group_id
        target_name = inbox.name or INBOX_GROUP_NAME

    metadata = _metadata_from_tab(tab)

    # VP-F5: an explicit group collects into that group; no group collects
    # into the workspace Inbox.  VP-D1: the entry may already exist for this
    # URL (created by the bridge when the tab opened); ``collect_tab`` upserts
    # by URL so no duplicate is created, and the entry is rebound to the
    # live tab's id below so the bridge tracks this tab for the same-URL row.
    collected = manager.collect_tab(
        workspace.id, tab.tab_id, group, metadata=metadata)
    if collected.tab_id != tab.tab_id:
        manager.update_tab_metadata(
            workspace.id, collected.tab_id, new_tab_id=tab.tab_id)
    _persist_description(manager, workspace.id, tab.tab_id, tab)

    if group_id:
        message.info(f"Collected tab into group '{target_name}'.")
    else:
        message.info(f"Collected tab into '{target_name}'.")


@cmdutils.register(name='vp-collect-all')
@cmdutils.argument('group_id', metavar='group')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_collect_all(group_id: str = '', win_id: int = 0) -> None:
    """Collect every collectable tab in the current window into a group.

    Browser-internal pages (``qute://*``, ``about:blank``, ``chrome://*``,
    ``view-source:*``, ...) are skipped silently.

    Args:
        group_id: Group name or UUID. When omitted, the tabs are collected
            into the workspace's Inbox group (created on demand).
    """
    manager = _manager()
    workspace = _active_workspace(manager)
    if group_id:
        group = _resolve_group(manager, workspace, group_id)
        target_name = group_id
    else:
        inbox = manager.get_or_create_inbox(workspace.id)
        group = inbox.group_id
        target_name = inbox.name or INBOX_GROUP_NAME

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

    if group_id:
        message.info(
            f"Collected {len(collected)} tab(s) into group '{target_name}'.")
    else:
        message.info(
            f"Collected {len(collected)} tab(s) into '{target_name}'.")


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

    Non-empty groups require confirmation; empty groups are deleted
    immediately (ADR 0008 § Decisions). Unlike ``:vp-group-archive``, this
    cannot be undone.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    if manager.group_total_count(workspace.id, group.group_id) == 0:
        # ADR 0008 § Decisions: empty groups are deleted without a prompt.
        _make_group_delete_action(manager, workspace, group)()
        return
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


@cmdutils.register(name='vp-group-close')
@cmdutils.argument('target', metavar='target')
@cmdutils.argument('force', flag='!')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_group_close(target: str, force: bool = False, win_id: int = 0) -> None:
    """Close every open tab in the group, leaving entries as SUSPENDED.

    Asks for confirmation (including an estimate of the memory which will be
    freed) unless ``-!`` is given. Cancelling leaves the group untouched.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    group_id = group.group_id
    name = group.name

    state = manager.group_state(workspace.id, group_id)
    if state == 'ARCHIVED':
        message.info(f"Group '{name}' is archived.")
        return
    if manager.group_total_count(workspace.id, group_id) == 0:
        message.info(f"Group '{name}' has no open tabs.")
        return
    if state == 'SUSPENDED':
        message.info(f"Group '{name}' is already suspended.")
        return

    bytes_estimate = manager.estimate_group_memory_bytes(
        workspace.id, group_id)
    fmt = format_size(bytes_estimate, suffix='B')
    if force:
        _close_group_and_message(manager, workspace, group, fmt, win_id)
        return
    message.confirm_async(
        title=f"Close group '{name}'?",
        text=(f"This will close {manager.group_open_count(workspace.id, group_id)} "
              f"tab(s), freeing ~{fmt}."),
        default=False,
        yes_action=lambda: _close_group_and_message(
            manager, workspace, group, fmt, win_id),
        no_action=lambda: message.info("Cancelled."),
    )


def _close_group_and_message(
    manager: WorkspaceManager,
    workspace: Workspace,
    group: Group,
    fmt: str,
    win_id: int,
) -> None:
    """Close *group* and report how many tabs were closed / skipped."""
    closed, skipped = manager.close_group(
        workspace.id, group.group_id, win_id=win_id)
    message.info(
        f"Closed {closed} tab(s) in '{group.name}', freed ~{fmt}; "
        f"skipped {skipped}.")


@cmdutils.register(name='vp-group-reopen')
@cmdutils.argument('target', metavar='target')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_group_reopen(target: str, win_id: int = 0) -> None:
    """Reopen every suspended tab in the group, reusing live tabs by URL.

    Already-open tabs are left untouched. An OPEN group is a no-op, and an
    archived group must be restored first.

    Args:
        target: The name or UUID of the group.
    """
    manager = _manager()
    workspace, group = _resolve_group_target(manager, target)
    state = manager.group_state(workspace.id, group.group_id)
    if state == 'OPEN':
        message.info(f"Group '{group.name}' is already open.")
        return
    if state == 'ARCHIVED':
        message.info(f"Group '{group.name}' is archived; restore it first.")
        return
    total = sum(1 for tab in workspace.tabs
                if tab.group_id == group.group_id
                and tab.open_status == 'SUSPENDED')
    if total == 0:
        message.info(f"Group '{group.name}' has no suspended tabs.")
        return

    def _progress(done: int, total_count: int, skipped: int) -> None:
        message.info(
            f"恢复中 {done}/{total_count} ({group.name})", replace=True)

    opened, reused, skipped = manager.reopen_group(
        workspace.id, group.group_id, win_id=win_id,
        on_progress=_progress)
    if opened == 0 and reused == 0 and skipped == 0:
        message.info(f"Group '{group.name}' has no suspended tabs.")
        return
    message.info(
        f"Reopened group '{group.name}': opened {opened}, reused {reused}, "
        f"skipped {skipped}.")


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
    if removed > 0:
        message.info(f"Removed {removed} done tab(s).")


@cmdutils.register(name='vp-suspend-stale')
@cmdutils.argument('days', metavar='days')
def vp_suspend_stale(days: int | None = None) -> None:
    """Suspend ``DONE`` tabs which have gone untouched for a while.

    Only open tabs marked ``DONE`` older than the threshold are flipped to
    ``SUSPENDED``; the live browser tab is left alone. When *days* is omitted
    the ``content.vertical_panel.auto_suspend_done_days`` setting is used. A
    *days* value outside ``[30, 180]`` is ignored in favour of the configured
    default.

    Args:
        days: Optional retention window in days (30-180).
    """
    manager = _manager()
    workspace = _active_workspace(manager)

    configured = config.val.content.vertical_panel.auto_suspend_done_days
    if days is None:
        effective = configured
    elif _CLEANUP_MIN_DAYS <= days <= _CLEANUP_MAX_DAYS:
        effective = days
    else:
        message.info(
            f"Days {days} out of range "
            f"({_CLEANUP_MIN_DAYS}-{_CLEANUP_MAX_DAYS}); using {configured}.")
        effective = configured

    suspended = manager.auto_suspend_due_tabs(workspace.id, effective)
    if suspended:
        message.info(f"Suspended {suspended} stale tab(s).")


@cmdutils.register(name='vp-suggestion-stale')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_suggestion_stale(win_id: int = 0) -> None:
    """Print the current smart-suggestion list (debug/manual trigger).

    Groups already inside the ignore cooldown are omitted.
    """
    manager = _manager()
    workspace = _active_workspace(manager)
    ignored = manager.get_ignored_suggestion_groups(workspace.id)
    groups = [group for group in manager.suggest_stale_groups(workspace.id)
              if str(group.group_id) not in ignored]
    if not groups:
        message.info("No stale groups.")
        return
    names = ', '.join(group.name for group in groups)
    message.info(f"{len(groups)} stale group(s): {names}")


@cmdutils.register(name='vp-suggestion-ignore')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_suggestion_ignore(win_id: int = 0) -> None:
    """Mark all currently-suggested Groups as ignored (manual)."""
    manager = _manager()
    workspace = _active_workspace(manager)
    ignored = manager.get_ignored_suggestion_groups(workspace.id)
    group_ids = [
        str(group.group_id)
        for group in manager.suggest_stale_groups(workspace.id)
        if str(group.group_id) not in ignored
    ]
    if not group_ids:
        message.info("No stale groups to ignore.")
        return
    added = manager.ignore_suggestion_groups(workspace.id, group_ids)
    message.info(f"Ignored {added} group(s) for 7 days.")

# VP-F4 (vp/16): multi-select + batch operations. The selection itself lives
# in the window's TabTree model; these commands drive it and hand the selected
# tab ids to the batched WorkspaceManager APIs.


@cmdutils.register(name='vp-select-mode')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_select_mode(win_id: int = 0) -> None:
    """Toggle multi-select mode in the vertical panel's tab tree."""
    tree = _tab_tree(win_id)
    enabled = not tree.selection_mode()
    tree.set_selection_mode(enabled)
    if enabled:
        message.info("Selection mode enabled.")
    else:
        message.info("Selection mode disabled.")


@cmdutils.register(name='vp-select-toggle')
@cmdutils.argument('target', metavar='tab-id')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_select_toggle(target: int, win_id: int = 0) -> None:
    """Toggle the selection of the tab with the given id.

    Args:
        target: The integer id of the tab to (de)select.
    """
    tree = _tab_tree(win_id)
    model = tree.model()
    if not tree.selection_mode():
        tree.set_selection_mode(True)
    selected = model.selected_tab_ids()
    newly_selected = target not in selected
    model.set_selected(target, newly_selected)
    if newly_selected:
        message.info(f"Selected tab {target}.")
    else:
        message.info(f"Deselected tab {target}.")


@cmdutils.register(name='vp-select-all')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_select_all(win_id: int = 0) -> None:
    """Select every tab in the active workspace."""
    tree = _tab_tree(win_id)
    model = tree.model()
    if not tree.selection_mode():
        tree.set_selection_mode(True)
    model.select_all(True)
    message.info(f"Selected {len(model.selected_tab_ids())} tab(s).")


@cmdutils.register(name='vp-select-none')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_select_none(win_id: int = 0) -> None:
    """Clear the tab selection."""
    tree = _tab_tree(win_id)
    tree.model().select_all(False)
    message.info("Cleared tab selection.")


def _report_ignored(selected: set[int], handled: int) -> None:
    """Report how many selected ids the manager did not recognise."""
    ignored = len(selected) - handled
    if ignored > 0:
        message.info(f"Ignored {ignored} unknown id(s).")


@cmdutils.register(name='vp-batch-move')
@cmdutils.argument('group_ref', metavar='group')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_batch_move(group_ref: str, win_id: int = 0) -> None:
    """Move the selected tabs into a group.

    Args:
        group_ref: Group name or UUID to move the tabs into. Required.
    """
    manager = _manager()
    tree = _tab_tree(win_id)
    selected = tree.model().selected_tab_ids()
    if not selected:
        raise cmdutils.CommandError("No tabs selected.")
    if not group_ref:
        raise cmdutils.CommandError(
            "A group name or UUID is required for :vp-batch-move.")
    workspace, group = _resolve_group_target(manager, group_ref)
    moved = manager.move_tabs(workspace.id, selected, group.group_id)
    message.info(f"Moved {moved} tab(s) into '{group.name}'.")
    _report_ignored(selected, moved)


@cmdutils.register(name='vp-batch-status')
@cmdutils.argument('status', metavar='status')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_batch_status(status: str, win_id: int = 0) -> None:
    """Set the reading status of the selected tabs.

    Args:
        status: One of ``FRESH``, ``ACTIVE`` or ``DONE``
            (case-insensitive).
    """
    manager = _manager()
    tree = _tab_tree(win_id)
    selected = tree.model().selected_tab_ids()
    if not selected:
        raise cmdutils.CommandError("No tabs selected.")
    workspace = _active_workspace(manager)
    normalized = status.upper()
    try:
        count = manager.set_reading_status(workspace.id, selected, normalized)
    except ValueError as e:
        raise cmdutils.CommandError(str(e)) from e
    message.info(f"Marked {count} tab(s) as {normalized}.")
    _report_ignored(selected, count)


@cmdutils.register(name='vp-batch-delete')
@cmdutils.argument('force', flag='!')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_batch_delete(force: bool = False, win_id: int = 0) -> None:
    """Delete the selected tabs, after confirmation.

    Pass ``-!`` to skip the confirmation prompt.
    """
    manager = _manager()
    tree = _tab_tree(win_id)
    model = tree.model()
    selected = model.selected_tab_ids()
    if not selected:
        raise cmdutils.CommandError("No tabs selected.")
    workspace = _active_workspace(manager)
    if force:
        count = manager.delete_tabs(workspace.id, selected, confirm=False)
        model.select_all(False)
        message.info(f"Deleted {count} tab(s).")
        _report_ignored(selected, count)
        return
    message.confirm_async(
        title=f"Delete {len(selected)} selected tab(s)?",
        default=False,
        yes_action=_make_batch_delete_action(
            manager, workspace, selected, model),
        cancel_action=_make_batch_delete_cancel(),
    )


def _make_batch_delete_cancel() -> Callable[[], None]:
    """Return the callback emitted when the delete prompt is cancelled."""

    def cancel() -> None:
        message.info("Cancelled.")

    return cancel


def _make_batch_delete_action(
    manager: WorkspaceManager,
    workspace: Workspace,
    tab_ids: set[int],
    model: Any,
) -> Callable[[], None]:
    """Return the callback which deletes *tab_ids* after confirmation."""
    ids = set(tab_ids)

    def delete() -> None:
        count = manager.delete_tabs(workspace.id, ids, confirm=False)
        model.select_all(False)
        message.info(f"Deleted {count} tab(s).")
        _report_ignored(ids, count)

    return delete


@cmdutils.register(name='vp-batch-open-window')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_batch_open_window(win_id: int = 0) -> None:
    """Open the selected tabs in a new window, reusing live tabs."""
    manager = _manager()
    tree = _tab_tree(win_id)
    selected = tree.model().selected_tab_ids()
    if not selected:
        # Spec: silent no-op (unlike the other batch commands).
        return
    workspace = _active_workspace(manager)
    opened, reused = manager.open_tabs_in_new_window(
        workspace.id, selected, win_id=win_id)
    message.info(
        f"Opened {opened} tab(s) in a new window; reused {reused}.")
    _report_ignored(selected, opened + reused)


@cmdutils.register(name='vp-batch-open-tabs')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def vp_batch_open_tabs(win_id: int = 0) -> None:
    """Open the selected tabs as new tabs, reusing live tabs."""
    manager = _manager()
    tree = _tab_tree(win_id)
    selected = tree.model().selected_tab_ids()
    if not selected:
        raise cmdutils.CommandError("No tabs selected.")
    workspace = _active_workspace(manager)
    opened, reused = manager.open_tabs_as_new_tabs(
        workspace.id, selected, win_id=win_id)
    message.info(f"Opened {opened} tab(s); reused {reused}.")
    _report_ignored(selected, opened + reused)
# Workspace switching (ADR 0008 § 4: switching happens via the
# WorkspaceIndicator; these commands are the keyboard/command equivalents,
# registered in `bindings.default.workspace_panel:`).


@cmdutils.register(name='workspace-switch')
def workspace_switch(name: str) -> None:
    """Switch the active workspace to the given name.

    Args:
        name: The name of the workspace to switch to.
    """
    manager = _manager()
    workspace = _lookup(name, manager)
    active = manager.get_active()
    if active is not None and active.id == workspace.id:
        message.info(f"Already on workspace '{workspace.name}'.")
        return
    manager.set_active(workspace.id)
    message.info(f"Switched to workspace '{workspace.name}'.")


@cmdutils.register(name='workspace-next')
def workspace_next() -> None:
    """Switch to the next non-archived workspace (wrapping around)."""
    _cycle_workspace(step=1)


@cmdutils.register(name='workspace-prev')
def workspace_prev() -> None:
    """Switch to the previous non-archived workspace (wrapping around)."""
    _cycle_workspace(step=-1)


def _cycle_workspace(step: int) -> None:
    """Switch to the workspace *step* positions away from the active one.

    Archived workspaces are skipped. With fewer than two candidates this is
    a no-op.
    """
    manager = _manager()
    workspaces = [ws for ws in manager.list_all_workspaces()
                  if not ws.archived]
    if len(workspaces) < 2:
        message.info("No other workspace to switch to.")
        return

    active = manager.get_active()
    if active is None:
        manager.set_active(workspaces[0].id)
        message.info(f"Switched to workspace '{workspaces[0].name}'.")
        return

    index = next(
        (i for i, ws in enumerate(workspaces) if ws.id == active.id),
        0,
    )
    target = workspaces[(index + step) % len(workspaces)]
    manager.set_active(target.id)
    message.info(f"Switched to workspace '{target.name}'.")


# Group management / tab placement (ADR 0008 § "Commands" and § 5).


@cmdutils.register(name='group-list')
def group_list() -> None:
    """List all groups in the active workspace."""
    manager = _manager()
    workspace = _active_workspace(manager)
    groups = manager.list_groups(workspace.id)
    if not groups:
        message.info('No groups in this workspace.')
        return
    lines = []
    for group in groups:
        markers = []
        if group.pinned:
            markers.append('pinned')
        if group.archived:
            markers.append('archived')
        suffix = f" ({', '.join(markers)})" if markers else ''
        lines.append(f"{group.name}{suffix}")
    message.info('\n'.join(lines))


@cmdutils.register(name='tab-move-to-group')
@cmdutils.argument('group_name', metavar='group')
def tab_move_to_group(tab_id: int, group_name: str = 'root') -> None:
    """Move a tab entry into a group, or back to the root level.

    Args:
        tab_id: The workspace tab's integer id to move.
        group_name: Target group name or UUID, or ``root`` for standalone.
    """
    manager = _manager()
    workspace = _active_workspace(manager)
    if group_name == 'root' or not group_name:
        group_id: uuid.UUID | None = None
    else:
        group_id = _resolve_group(manager, workspace, group_name)
    manager.move_tab_to_group(workspace.id, tab_id, group_id)
    target = group_name if group_id is not None else 'root'
    message.info(f"Moved tab {tab_id} to {target}.")


def _live_tab(win_id: int, tab_id: int) -> Any:
    """Return the live tab for *tab_id* in *win_id*, or ``None``."""
    try:
        registry = objreg.get(
            'tab-registry', scope='window', window=win_id)
    except (KeyError, objreg.RegistryUnavailableError):
        return None
    return registry.get(tab_id)


def _resolve_pinned(manager: WorkspaceManager, target: str) -> PinnedTab:
    """Resolve *target* (a tab id or URL) to a pinned tab entry."""
    try:
        tab_id: Optional[int] = int(target)
    except ValueError:
        tab_id = None

    pinned = manager.list_pinned()
    if tab_id is not None:
        for entry in pinned:
            if entry.tab_id == tab_id:
                return entry
    for entry in pinned:
        if entry.url == target:
            return entry
    raise cmdutils.CommandError(f"No pinned tab matching '{target}'.")


def _resolve_pinned_position(
        pinned: list[PinnedTab],
        target: str,
        *,
        exclude_tab_id: int,
) -> int:
    """Resolve *target* to an index in the pinned list.

    A tab id or URL selects the index of that entry.  A bare integer which
    is not a pinned tab id is treated as a final position (0-based), which
    makes ``:pintab-move 3 0`` move tab 3 to the top.
    """
    try:
        value: Optional[int] = int(target)
    except ValueError:
        value = None

    if value is not None:
        for index, entry in enumerate(pinned):
            if entry.tab_id == value and entry.tab_id != exclude_tab_id:
                return index
        if 0 <= value < len(pinned):
            return value

    for index, entry in enumerate(pinned):
        if entry.url == target and entry.tab_id != exclude_tab_id:
            return index
    raise cmdutils.CommandError(f"No pinned position matching '{target}'.")


def _pinned_from_entry(
        manager: WorkspaceManager,
        workspace: Workspace,
        entry: WorkspaceTab,
        win_id: int,
) -> PinnedTab:
    """Build a :class:`PinnedTab` from a workspace entry and its live tab."""
    title = entry.title
    url = entry.url
    favicon_b64 = entry.favicon
    live = _live_tab(win_id, entry.tab_id)
    if live is not None:
        metadata = _metadata_from_tab(live)
        title = metadata.title or title
        url = metadata.url or url
        favicon_b64 = metadata.favicon or favicon_b64
    favicon = (
        base64.b64decode(favicon_b64) if favicon_b64 else None
    )
    return PinnedTab(
        tab_id=entry.tab_id,
        title=title,
        url=url,
        favicon=favicon,
    )


@cmdutils.register(name='pintab-add')
@cmdutils.argument('target', metavar='target')
@cmdutils.argument('win_id', value=cmdutils.Value.win_id)
def pintab_add(target: str, win_id: int = 0) -> None:
    """Pin a tab entry, shown in the vertical panel's PIN section.

    Args:
        target: Tab id or URL of the regular tab to pin.
    """
    manager = _manager()
    workspace = _active_workspace(manager)
    entry = _resolve_status_target(manager, workspace, target)
    pinned = _pinned_from_entry(manager, workspace, entry, win_id)
    manager.add_pinned(pinned)
    message.info(f"Pinned tab {entry.tab_id}.")


@cmdutils.register(name='pintab-remove')
@cmdutils.argument('target', metavar='target')
def pintab_remove(target: str) -> None:
    """Remove a tab from the PIN section without closing the tab.

    Args:
        target: Tab id or URL of the pinned entry to unpin.
    """
    manager = _manager()
    pinned = _resolve_pinned(manager, target)
    manager.remove_pinned(pinned.tab_id)
    message.info(f"Unpinned tab {pinned.tab_id}.")


@cmdutils.register(name='pintab-move')
@cmdutils.argument('from_target', metavar='from')
@cmdutils.argument('to_target', metavar='to')
def pintab_move(from_target: str, to_target: str) -> None:
    """Reorder a pinned entry within the PIN section.

    Args:
        from_target: Tab id or URL of the pinned entry to move.
        to_target: Target tab id, URL, or 0-based final position.
    """
    manager = _manager()
    pinned = manager.list_pinned()
    source = _resolve_pinned(manager, from_target)
    new_index = _resolve_pinned_position(
        pinned, to_target, exclude_tab_id=source.tab_id)
    if not manager.move_pinned(source.tab_id, new_index):
        message.info(f"Pinned tab {source.tab_id} is already in that position.")
        return
    message.info(
        f"Moved pinned tab {source.tab_id} to position {new_index}.")


@cmdutils.register(name='pintab-style-toggle')
def pintab_style_toggle() -> None:
    """Toggle the pinned-tab panel between ``icon`` and ``line`` style."""
    new = 'line' if config.val.pinned_tabs.style == 'icon' else 'icon'
    config.instance.set_obj('pinned_tabs.style', new)
    message.info(f"Pinned-tab style: {new}.")


@cmdutils.register(name='workspace-unarchive')
def workspace_unarchive(name: str) -> None:
    """Restore an archived workspace to normal use.

    Args:
        name: The name of the workspace to unarchive.
    """
    manager = _manager()
    workspace = _lookup(name, manager)
    if not workspace.archived:
        message.info(f"Workspace '{name}' is not archived.")
        return
    manager.unarchive_workspace(workspace.id)
    message.info(f"Unarchived workspace '{name}'.")
