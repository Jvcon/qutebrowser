# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Import and export workspaces as a versioned JSON interchange document.

The interchange format is deliberately separate from the private on-disk
YAML persistence format (ADR 0009 § 1).  It carries only state which is
meaningful on another machine and is written as stable, diff-friendly JSON.
"""

import base64
import contextlib
import dataclasses
import datetime
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from qutebrowser import __version__ as qutebrowser_version
from qutebrowser.qt.core import QUrl
from qutebrowser.utils import objreg
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)

#: Version of the interchange document written by :func:`export_to_path`.
#: Importers run a document forward through a migration chain to this version
#: (ADR 0009 § 1).
SCHEMA_VERSION = 1

#: Every interchange schema version this build can read.  A document newer
#: than ``max(SUPPORTED_VERSIONS)`` is rejected before anything is applied.
SUPPORTED_VERSIONS = (1,)

#: A migration takes a document at version ``N`` and returns it ready for
#: version ``N + 1``.  Version ``1 -> 1`` is the identity self-edge which keeps
#: the chain machinery exercised from day one (ADR 0009 § 1 / issue #42).
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]
MIGRATIONS: dict[int, MigrationFn] = {
    1: lambda doc: doc,
}

#: File name used when the user supplies a directory (or no path at all).
DEFAULT_EXPORT_FILENAME = 'workspaces-export.json'


class PortabilityError(Exception):

    """Raised when a portability document cannot be read or applied."""


@dataclasses.dataclass
class ImportReport:

    """Counters describing what :func:`import_from_path` did."""

    added_workspaces: int = 0
    added_entries: int = 0
    #: Document entry whose normalized URL already existed in the target.
    skipped_entries: int = 0
    #: Document workspace whose ``id`` was already present locally.
    skipped_workspaces: int = 0
    #: Child whose ``parent_id`` could not be resolved locally.
    orphan_promotions: int = 0


def _manager() -> Any:
    """Return the app-wide WorkspaceManager.

    Kept as a module-level indirection so unit tests can substitute a stub
    without reaching into the object registry.
    """
    return objreg.get('workspace-manager')


def _normalize_url(url: str) -> str:
    """Return *url* in the canonical form used as the cross-machine key.

    ``WorkspaceTab.url`` is stored as ``QUrl.toString()`` by the browser
    layer; exporting that same representation keeps case and percent-encoding
    differences from producing different keys on import.  Leading/trailing
    whitespace is stripped first because a stray space makes ``QUrl`` invalid.
    """
    cleaned = url.strip()
    if not cleaned:
        return ''
    # Fall back to the stripped original if QUrl rejects the value, so a
    # malformed but non-empty URL is not silently dropped from the export.
    return QUrl(cleaned).toString() or cleaned


def _tab_to_dict(tab: WorkspaceTab) -> dict[str, Any]:
    """Serialize one :class:`WorkspaceTab` for the interchange document.

    Machine-local and derived fields listed in ADR 0009 § 3 are omitted:
    ``tab_id``, ``open_status``, ``last_fetched``, ``fetch_attempts`` and
    ``last_error``.  ``parent_id`` stays a raw local ``tab_id``; the importer
    remaps it through the normalized URL key.

    ``favicon`` is carried (base64 ASCII already, per
    :class:`~qutebrowser.workspaces.model.WorkspaceTab`) so an imported
    document does not have to re-fetch every site icon just to render the
    panel.
    """
    return {
        'title': tab.title,
        'url': _normalize_url(tab.url),
        'description': tab.description,
        'source': tab.source,
        'favicon': tab.favicon,
        'reading_status': tab.reading_status,
        'done_at': tab.done_at,
        'created_at': tab.created_at,
        'parent_id': tab.parent_id,
        'group_id': str(tab.group_id) if tab.group_id is not None else None,
    }


def _group_to_dict(group: Group, tabs: list[WorkspaceTab]) -> dict[str, Any]:
    """Serialize a group together with its member entries.

    ``window_affinity`` and ``affinity_enabled`` are excluded (ADR 0009 § 3):
    a ``win_id`` is meaningless on another machine.
    """
    return {
        'group_id': str(group.group_id),
        'name': group.name,
        'folded': group.folded,
        'pinned': group.pinned,
        'archived': group.archived,
        'created_at': group.created_at,
        'entries': [
            _tab_to_dict(tab) for tab in tabs
            if tab.group_id == group.group_id
        ],
    }


def _pinned_to_dict(tab: PinnedTab) -> dict[str, Any]:
    """Serialize a pinned tab, dropping its local ``tab_id``.

    Pinned tabs are re-addressed by URL on import, exactly like entries
    (ADR 0009 § 3).  ``last_active`` is machine-local and also omitted.
    """
    data = tab.to_dict()
    data.pop('tab_id', None)
    data.pop('last_active', None)
    return data


def _workspace_to_dict(workspace: Workspace) -> dict[str, Any]:
    """Serialize one workspace and the groups/tabs it owns.

    ``last_active`` is machine-local (which workspace *this* machine last
    showed) and is excluded (ADR 0009 § 3).  The model has no
    ``Workspace.created_at`` field yet, so the key is emitted as ``None``
    (tracked as a known limitation, see issue #41).

    Pinned tabs are deliberately **not** here: they are profile-scoped, so
    they sit at the document root where exactly one copy round-trips
    through import.

    Standalone tabs (``group_id is None``) are kept in a workspace-level
    ``entries`` list; grouped tabs live under their group's ``entries``.
    The original issue table only describes per-group ``entries``; this
    extra key is the only non-lossy representation and is noted in ADR 0009
    § Implementation status.
    """
    standalone = [
        _tab_to_dict(tab) for tab in workspace.tabs if tab.group_id is None
    ]
    return {
        'id': str(workspace.id),
        'name': workspace.name,
        'archived': workspace.archived,
        'created_at': None,
        'groups': [
            _group_to_dict(group, workspace.tabs)
            for group in workspace.groups
        ],
        'entries': standalone,
    }


def export_to_path(path: Path) -> Path:
    """Serialize all workspaces to a versioned JSON document at *path*.

    The parent directory is created when missing.  On Unix the file is
    chmodded to ``0o600`` because it contains browsing history metadata;
    ``PermissionError`` is swallowed on Windows where the call may raise but
    the bits are ignored.

    Returns:
        The absolute resolved path which was written.
    """
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    manager = _manager()
    pinned = list(manager.list_pinned())
    workspaces = manager.list_all_workspaces()

    # `tabs_index` is a structural field at the document root that maps each
    # source ``tab_id`` (process-local counter, meaningless on another
    # machine) to its normalized URL. ADR 0009 § 2 promises that
    # ``parent_id`` chains round-trip via the URL key, but the per-entry
    # ``tab_id`` exclusion (§ 3) makes that impossible without this side
    # channel. The importer uses ``tabs_index`` as the source of truth and
    # promotes to top-level only when a referenced ``tab_id`` is genuinely
    # absent from the document.
    tabs_index: dict[str, str] = {}
    for workspace in workspaces:
        for tab in workspace.tabs:
            if tab.tab_id is not None:
                tabs_index[str(tab.tab_id)] = _normalize_url(tab.url)

    document = {
        'schema_version': SCHEMA_VERSION,
        'exported_at': datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        'qutebrowser_version': qutebrowser_version,
        # Pinned tabs are profile-scoped, so they sit at the document root
        # rather than inside any one workspace. A per-workspace copy would
        # duplicate the list N times and force the importer to URL-dedup
        # just to undo that duplication. See issue 27 for the discussion.
        'pinned': [_pinned_to_dict(tab) for tab in pinned],
        'tabs_index': tabs_index,
        'workspaces': [
            _workspace_to_dict(workspace)
            for workspace in workspaces
        ],
    }

    with resolved.open('w', encoding='utf-8') as f:
        json.dump(document, f, indent=2, sort_keys=False, ensure_ascii=False)

    try:
        os.chmod(resolved, 0o600)
    except PermissionError:
        # Windows ignores the permission bits but may still raise.
        pass

    return resolved


def _read_document(path: Path) -> dict[str, Any]:
    """Read *path* as JSON without ever including its contents in errors."""
    try:
        with Path(path).expanduser().open('r', encoding='utf-8') as f:
            document = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise PortabilityError(f"could not read import document: {e}") from e
    if not isinstance(document, dict):
        raise PortabilityError("import document is not a JSON object")
    return document


def _migrate_document(document: dict[str, Any]) -> dict[str, Any]:
    """Run *document* forward through :data:`MIGRATIONS` to this build.

    The version check happens before any migration (and therefore before any
    state is touched), so a newer document is rejected atomically.
    """
    version = document.get('schema_version')
    if isinstance(version, bool) or not isinstance(version, int):
        raise PortabilityError("schema_version is missing or not an integer")

    latest = max(SUPPORTED_VERSIONS)
    if version > latest:
        raise PortabilityError(
            f"unsupported schema_version={version}; "
            f"this build only knows up to {latest}")

    current = version
    while current <= latest:
        migration = MIGRATIONS.get(current)
        if migration is None:
            raise PortabilityError(
                f"no migration path from schema_version={version}")
        document = migration(document)
        if not isinstance(document, dict):
            raise PortabilityError(
                f"migration from schema_version={current} did not return "
                "a JSON object")
        current += 1

    return document


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortabilityError(f"invalid {field} in import document")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise PortabilityError(f"invalid {field} in import document")
    return value


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as e:
        raise PortabilityError(f"invalid {field} in import document") from e


def _entry_url(entry: dict[str, Any]) -> str:
    raw = entry.get('url')
    return _normalize_url(raw if isinstance(raw, str) else '')


def _entry_source_id(entry: dict[str, Any]) -> Optional[int]:
    """Return the source ``tab_id`` when the entry carries it inline.

    Round-tripped documents emit ``tabs_index`` at the document root, not
    inline ``tab_id``, so this helper is a fallback for hand-written or
    legacy documents only.
    """
    value = entry.get('tab_id')
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _find_url_entry(
        workspace: Workspace,
        group_id: Optional[uuid.UUID],
        url: str,
) -> Optional[WorkspaceTab]:
    """Return a local entry in *group_id* with normalized *url*, if any."""
    if not url:
        return None
    for tab in workspace.tabs:
        if tab.group_id == group_id and _normalize_url(tab.url) == url:
            return tab
    return None


def _find_url_in_workspace(
        manager: Any,
        workspace_id: uuid.UUID,
        url: str,
) -> Optional[WorkspaceTab]:
    if not url:
        return None
    workspace = manager.get_workspace(workspace_id)
    if workspace is None:
        return None
    for tab in workspace.tabs:
        if _normalize_url(tab.url) == url:
            return tab
    return None


def _collect_source_urls_from_index(document: dict[str, Any]) -> dict[int, str]:
    """Read the document-level ``tabs_index`` written by the exporter.

    Each entry maps a source ``tab_id`` to a normalized URL. The exporter
    emits this for round-trip integrity of ``parent_id`` chains (ADR 0009
    § 2).
    """
    result: dict[int, str] = {}
    raw_index = document.get('tabs_index')
    if not isinstance(raw_index, dict):
        return result
    for source_id, url in raw_index.items():
        if not isinstance(source_id, str) or not isinstance(url, str):
            continue
        try:
            key = int(source_id)
        except ValueError:
            continue
        url_value = _normalize_url(url)
        if url_value:
            result[key] = url_value
    return result


def _collect_source_urls_from_entries(
        document: dict[str, Any]) -> dict[int, str]:
    """Read inline ``tab_id`` on entries as a fallback source map.

    Used for hand-written or older documents; the exporter never emits
    ``tab_id`` inline because ADR 0009 § 3 rules out machine-local fields
    on each entry.
    """
    result: dict[int, str] = {}

    def _walk_container(container) -> None:
        for entry in _require_list(container.get('entries', []), 'entries'):
            entry = _require_dict(entry, 'entry')
            source_id = _entry_source_id(entry)
            if source_id is None:
                continue
            url_value = _entry_url(entry)
            if url_value and source_id not in result:
                result[source_id] = url_value

    for workspace_doc in _require_list(
            document.get('workspaces', []), 'workspaces'):
        workspace_doc = _require_dict(workspace_doc, 'workspace')
        for group_doc in _require_list(
                workspace_doc.get('groups', []), 'groups'):
            _walk_container(_require_dict(group_doc, 'group'))
        _walk_container(workspace_doc)
    return result


def _collect_source_urls(document: dict[str, Any]) -> dict[int, str]:
    """Map source ``tab_id`` to its normalized URL for parent remapping.

    The exporter writes the mapping at the document root in ``tabs_index``
    (primary source of truth, ADR 0009 § 2). Per-entry ``tab_id`` is a
    fallback for hand-written or older documents. When neither is present
    for a referenced ``parent_id``, the child is promoted to top-level per
    the ADR 0009 § 2 orphan rule.
    """
    result = _collect_source_urls_from_index(document)
    fallback = _collect_source_urls_from_entries(document)
    for key, url in fallback.items():
        result.setdefault(key, url)
    return result


def _next_tab_id(workspaces: list[Workspace]) -> int:
    ids = [tab.tab_id for workspace in workspaces for tab in workspace.tabs]
    return max(ids, default=-1) + 1


def _next_pinned_id(pinned: list[PinnedTab]) -> int:
    ids = [tab.tab_id for tab in pinned]
    return max(ids, default=-1) + 1


def _decode_favicon(value: Any) -> Optional[bytes]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value)
    except ValueError as e:
        raise PortabilityError("invalid pinned favicon in import document") from e


def _rollback(
        manager: Any,
        added_workspace_ids: list[uuid.UUID],
        added_group_keys: list[tuple[uuid.UUID, uuid.UUID]],
        added_tab_keys: list[tuple[uuid.UUID, int]],
        added_pinned_ids: list[int],
) -> None:
    """Best-effort revert of a partially applied import.

    Uses only public manager APIs, in reverse dependency order.  A failure
    during rollback is suppressed because the original exception is more
    useful; the caller is told the state may be partial.
    """
    for workspace_id, tab_id in reversed(added_tab_keys):
        with contextlib.suppress(Exception):
            manager.remove_tab(workspace_id, tab_id)
    for workspace_id, group_id in reversed(added_group_keys):
        with contextlib.suppress(Exception):
            manager.delete_group(workspace_id, group_id)
    for workspace_id in reversed(added_workspace_ids):
        with contextlib.suppress(Exception):
            manager.delete_workspace(workspace_id)
    for pinned_id in reversed(added_pinned_ids):
        with contextlib.suppress(Exception):
            manager.remove_pinned(pinned_id)


def _import_entry(
        manager: Any,
        workspace: Workspace,
        group_id: Optional[uuid.UUID],
        entry_doc: dict[str, Any],
        *,
        report: ImportReport,
        added_tab_keys: list[tuple[uuid.UUID, int]],
        added_source_tabs: dict[int, tuple[uuid.UUID, int]],
        pending_parents: list[
            tuple[uuid.UUID, WorkspaceTab, Any, Optional[int]]],
        next_tab_id: int,
) -> int:
    """Add one document entry, or count it as a URL collision."""
    url = _entry_url(entry_doc)
    source_id = _entry_source_id(entry_doc)
    existing = _find_url_entry(workspace, group_id, url)
    if existing is not None:
        report.skipped_entries += 1
        if source_id is not None:
            added_source_tabs[source_id] = (workspace.id, existing.tab_id)
        return next_tab_id

    tab = WorkspaceTab(
        tab_id=next_tab_id,
        group_id=group_id,
        title=str(entry_doc.get('title') or ''),
        url=url,
        description=str(entry_doc.get('description') or ''),
        source=str(entry_doc.get('source') or ''),
        favicon=entry_doc.get('favicon'),
        reading_status=str(entry_doc.get('reading_status') or 'FRESH'),
        created_at=entry_doc.get('created_at'),
        done_at=entry_doc.get('done_at'),
    )
    manager.add_tab(workspace.id, tab)
    added_tab_keys.append((workspace.id, tab.tab_id))
    report.added_entries += 1

    if source_id is not None:
        added_source_tabs[source_id] = (workspace.id, tab.tab_id)
    parent_id = entry_doc.get('parent_id')
    if parent_id is not None:
        pending_parents.append((workspace.id, tab, parent_id, source_id))
    return next_tab_id + 1


def _resolve_pending_parents(
        manager: Any,
        pending_parents: list[
            tuple[uuid.UUID, WorkspaceTab, Any, Optional[int]]],
        added_source_tabs: dict[int, tuple[uuid.UUID, int]],
        source_urls: dict[int, str],
        report: ImportReport,
) -> None:
    """Translate document ``parent_id`` values into local tab ids."""
    for workspace_id, tab, parent_source_id, _own_source_id in pending_parents:
        resolved: Optional[int] = None
        if isinstance(parent_source_id, int) and not isinstance(
                parent_source_id, bool):
            mapped = added_source_tabs.get(parent_source_id)
            if mapped is not None and mapped[0] == workspace_id:
                resolved = mapped[1]
            else:
                parent_url = source_urls.get(parent_source_id)
                if parent_url:
                    local_parent = _find_url_in_workspace(
                        manager, workspace_id, parent_url)
                    if local_parent is not None:
                        resolved = local_parent.tab_id

        if (resolved is None or resolved == tab.tab_id
                or not _tab_exists(manager, workspace_id, resolved)):
            report.orphan_promotions += 1
            continue
        manager.set_parent(workspace_id, tab.tab_id, resolved)


def _import_pinned_entries(
        manager: Any,
        pinned_doc: list[Any],
        *,
        added_pinned_ids: list[int],
        next_pinned_id: int,
) -> int:
    """Add document pinned tabs, silently skipping URL collisions."""
    for pinned_entry in pinned_doc:
        pinned_entry = _require_dict(pinned_entry, 'pinned entry')
        url = _entry_url(pinned_entry)
        if url and any(
                _normalize_url(tab.url) == url
                for tab in manager.list_pinned()):
            # Pinned tabs are profile-scoped; dedup is silent.
            continue
        pinned = PinnedTab(
            tab_id=next_pinned_id,
            title=str(pinned_entry.get('title') or ''),
            url=url,
            favicon=_decode_favicon(pinned_entry.get('favicon')),
            last_active=None,
        )
        manager.add_pinned(pinned)
        added_pinned_ids.append(pinned.tab_id)
        next_pinned_id += 1
    return next_pinned_id


def import_from_path(path: Path) -> ImportReport:
    """Read a versioned JSON document at *path* and apply it additively.

    Nothing in the document is ever logged.  The returned
    :class:`ImportReport` is the only output; the caller decides how to
    present it.

    Raises:
        PortabilityError: if the document is malformed, its
            ``schema_version`` is newer than this build understands, or an
            apply error occurred (in which case a best-effort rollback has
            already run).
    """
    document = _migrate_document(_read_document(Path(path)))
    workspaces_doc = _require_list(
        document.get('workspaces', []), 'workspaces')
    pinned_doc = _require_list(document.get('pinned', []), 'pinned')

    source_urls = _collect_source_urls(document)

    manager = _manager()
    local_workspaces = manager.list_all_workspaces()
    local_workspace_ids = {workspace.id for workspace in local_workspaces}
    groups_by_id: dict[uuid.UUID, tuple[Workspace, Group]] = {
        group.group_id: (workspace, group)
        for workspace in local_workspaces
        for group in workspace.groups
    }

    next_tab_id = _next_tab_id(local_workspaces)
    next_pinned_id = _next_pinned_id(manager.list_pinned())

    report = ImportReport()
    added_workspace_ids: list[uuid.UUID] = []
    added_group_keys: list[tuple[uuid.UUID, uuid.UUID]] = []
    added_tab_keys: list[tuple[uuid.UUID, int]] = []
    added_pinned_ids: list[int] = []
    # source tab_id -> (workspace containing the new local tab, local id)
    added_source_tabs: dict[int, tuple[uuid.UUID, int]] = {}
    # (target workspace, added tab, document parent_id, source tab_id)
    pending_parents: list[
        tuple[uuid.UUID, WorkspaceTab, Any, Optional[int]]
    ] = []

    try:
        for workspace_doc in workspaces_doc:
            workspace_doc = _require_dict(workspace_doc, 'workspace')
            workspace_id = _parse_uuid(workspace_doc.get('id'), 'workspace id')

            if workspace_id in local_workspace_ids:
                report.skipped_workspaces += 1
                continue

            name = str(workspace_doc.get('name') or '')
            workspace = manager.add_workspace_with_id(
                workspace_id,
                name,
                archived=bool(workspace_doc.get('archived', False)),
            )
            local_workspace_ids.add(workspace_id)
            added_workspace_ids.append(workspace_id)
            report.added_workspaces += 1

            for group_doc in _require_list(
                    workspace_doc.get('groups', []), 'groups'):
                group_doc = _require_dict(group_doc, 'group')
                group_id = _parse_uuid(
                    group_doc.get('group_id'), 'group id')
                existing_group = groups_by_id.get(group_id)
                if existing_group is None:
                    group = manager.create_group(
                        workspace.id, str(group_doc.get('name') or ''))
                    # create_group generates a fresh uuid; the document id is
                    # globally unique and must be preserved (ADR 0009 § 2).
                    group.group_id = group_id
                    group.folded = bool(group_doc.get('folded', False))
                    group.pinned = bool(group_doc.get('pinned', False))
                    group.archived = bool(group_doc.get('archived', False))
                    created_at = group_doc.get('created_at')
                    if isinstance(created_at, str) and created_at:
                        group.created_at = created_at
                    groups_by_id[group_id] = (workspace, group)
                    added_group_keys.append((workspace.id, group_id))
                    target_workspace, target_group = workspace, group
                else:
                    target_workspace, target_group = existing_group

                for entry_doc in _require_list(
                        group_doc.get('entries', []), 'entries'):
                    next_tab_id = _import_entry(
                        manager,
                        target_workspace,
                        target_group.group_id,
                        _require_dict(entry_doc, 'entry'),
                        report=report,
                        added_tab_keys=added_tab_keys,
                        added_source_tabs=added_source_tabs,
                        pending_parents=pending_parents,
                        next_tab_id=next_tab_id,
                    )

            for entry_doc in _require_list(
                    workspace_doc.get('entries', []), 'entries'):
                next_tab_id = _import_entry(
                    manager,
                    workspace,
                    None,
                    _require_dict(entry_doc, 'entry'),
                    report=report,
                    added_tab_keys=added_tab_keys,
                    added_source_tabs=added_source_tabs,
                    pending_parents=pending_parents,
                    next_tab_id=next_tab_id,
                )

        _resolve_pending_parents(
            manager, pending_parents, added_source_tabs, source_urls, report)
        _import_pinned_entries(
            manager,
            pinned_doc,
            added_pinned_ids=added_pinned_ids,
            next_pinned_id=next_pinned_id,
        )
    except Exception as e:
        _rollback(
            manager, added_workspace_ids, added_group_keys,
            added_tab_keys, added_pinned_ids)
        if isinstance(e, PortabilityError):
            raise
        raise PortabilityError(
            "import failed; partial changes were rolled back where possible"
        ) from e

    return report


def _tab_exists(
        manager: Any,
        workspace_id: uuid.UUID,
        tab_id: int,
) -> bool:
    workspace = manager.get_workspace(workspace_id)
    if workspace is None:
        return False
    return any(tab.tab_id == tab_id for tab in workspace.tabs)
