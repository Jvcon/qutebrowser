# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Export workspaces to a versioned JSON interchange document.

The interchange format is deliberately separate from the private on-disk
YAML persistence format (ADR 0009 § 1).  It carries only state which is
meaningful on another machine and is written as stable, diff-friendly JSON.
"""

import datetime
import json
import os
from pathlib import Path
from typing import Any

from qutebrowser import __version__ as qutebrowser_version
from qutebrowser.qt.core import QUrl
from qutebrowser.utils import objreg
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)

#: Version of the interchange document written by :func:`export_to_path`.
#: Importers run a document forward through a migration chain to this version
#: (ADR 0009 § 1); no migration table exists yet because export is the first
#: half of the feature.
SCHEMA_VERSION = 1

#: File name used when the user supplies a directory (or no path at all).
DEFAULT_EXPORT_FILENAME = 'workspaces-export.json'


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
