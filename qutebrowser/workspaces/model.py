# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Data model for workspaces, groups, workspace tabs, and pinned tabs.

The dataclasses here are intentionally serialization-agnostic: each provides a
manual ``to_dict``/``from_dict`` pair that produces and consumes only primitive
types (``str``, ``int``, ``bool``, ``None``) so the result can be handed
straight to :func:`qutebrowser.utils.utils.yaml_dump``.  ``uuid.UUID`` values are
stored as strings and ``bytes`` favicons as base64 ASCII strings.
"""

import base64
import dataclasses
import datetime
import pathlib
import uuid
from typing import Any, Optional


@dataclasses.dataclass
class Profile:

    """A named storage profile (Phase X, not implemented yet).

    Placeholder for alignment with upstream qutebrowser's planned
    ``#5935`` refactor (The-Compiler's ``ProfileFactory(QObject)`` with a
    ``created = pyqtSignal(QWebEngineProfile)``). When upstream lands,
    this dataclass becomes a thin facade over the upstream
    ``QWebEngineProfile`` plus a ``name -> profile-id`` map; until then
    the only thing we use is the storage-root path (see
    :attr:`basedir`).

    The default profile is named ``'default'``; switching profiles
    means pointing the :class:`~qutebrowser.workspaces.manager.WorkspaceManager`
    at a different ``basedir``. Multiple workspaces may share one
    profile — the fork's design is more flexible than upstream's
    "1 profile = 1 window" model.

    Attributes:
        name: Human-readable profile identifier (also the directory
            component under ``<data>/profiles/``).
        basedir: Absolute path to this profile's root directory.
    """

    name: str
    basedir: pathlib.Path

    @classmethod
    def default(cls) -> 'Profile':
        """Return the default profile (named 'default')."""
        from qutebrowser.utils import standarddir
        return cls(
            name='default',
            basedir=pathlib.Path(standarddir.data()) / 'profiles' / 'default',
        )

    @classmethod
    def for_name(cls, name: str) -> 'Profile':
        """Return the profile for *name* under the standard data dir."""
        from qutebrowser.utils import standarddir
        return cls(
            name=name,
            basedir=pathlib.Path(standarddir.data()) / 'profiles' / name,
        )


@dataclasses.dataclass
class Group:

    """A named, foldable group of tabs within a workspace.

    Attributes:
        group_id: Unique identifier of this group.
        name: User-visible group name.
        folded: Whether the group is collapsed in the TabTree.
        pinned: Whether the group is pinned (pinned groups sort first).
        archived: Whether the group is archived (hidden from the normal
            list, but retained and restorable).
        created_at: ISO 8601 UTC timestamp of when the group was created.
            Populated automatically when left as ``None``.
        window_affinity: The ``win_id`` of the window this group was last
            closed from, if any. Used by Reopen Group to route restored tabs
            back to that window (VP-A1). Only valid for the current browser
            session; window ids are not persisted across restarts.
        affinity_enabled: Per-group kill switch for window affinity. When
            ``False`` the group behaves as if :attr:`window_affinity` were
            ``None``.
    """

    group_id: uuid.UUID
    name: str
    folded: bool = False
    pinned: bool = False
    archived: bool = False
    created_at: Optional[str] = None
    window_affinity: Optional[int] = None
    affinity_enabled: bool = True

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.datetime.now(
                datetime.timezone.utc).isoformat()

    @property
    def state(self) -> str:
        """Return ``'ARCHIVED'`` if archived, else ``'UNKNOWN'``.

        The OPEN vs SUSPENDED distinction is resolved by
        :meth:`WorkspaceManager.group_state`, which has access to the
        workspace's tab list.  This property is the local view: archived
        wins, everything else is ``'UNKNOWN'`` until the manager
        classifies it (``'UNKNOWN'`` means "the model layer should call
        ``manager.group_state`` to resolve").
        """
        return 'ARCHIVED' if self.archived else 'UNKNOWN'

    def rename(self, new_name: str) -> None:
        """Rename this group."""
        self.name = new_name

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict."""
        return {
            'group_id': str(self.group_id),
            'name': self.name,
            'folded': self.folded,
            'pinned': self.pinned,
            'archived': self.archived,
            'created_at': self.created_at,
            'window_affinity': self.window_affinity,
            'affinity_enabled': self.affinity_enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'Group':
        """Deserialize from a primitive-only dict.

        Fields added after the original format (``pinned``, ``archived``,
        ``created_at``, ``window_affinity``, ``affinity_enabled``) default
        gracefully so older YAML files still load.
        """
        return cls(
            group_id=uuid.UUID(d['group_id']),
            name=d['name'],
            folded=bool(d.get('folded', False)),
            pinned=bool(d.get('pinned', False)),
            archived=bool(d.get('archived', False)),
            created_at=d.get('created_at'),
            window_affinity=d.get('window_affinity'),
            affinity_enabled=bool(d.get('affinity_enabled', True)),
        )


@dataclasses.dataclass
class WorkspaceTab:

    """A single tab entry (a.k.a. ``TabEntry``) inside a workspace.

    Attributes:
        tab_id: The tab's integer id (matches the runtime tab registry).
        parent_id: The ``tab_id`` of the parent tab, if any.
        group_id: The ``group_id`` of the group this tab belongs to, if any.
        title: Last captured page title.
        url: Last captured page URL.
        description: Best-effort page description (``meta[name=description]``).
        source: Site host derived from :attr:`url` (e.g. ``github.com``).
        favicon: Base64-encoded PNG bytes of the tab's favicon, if captured.
        user_edited: Per-field flags marking metadata the user edited
            manually. Keys are ``title``, ``description`` and ``source``;
            all three are always present (defaulting to ``False``).
        last_fetched: ISO 8601 UTC timestamp of the last successful metadata
            fetch, if any.
        fetch_attempts: Number of metadata fetch attempts made so far.
        last_error: Short error string from the last failed fetch, if any.
        open_status: Whether the tab is currently open in the browser
            (``'OPEN'``) or was closed/suspended but kept in the workspace
            (``'SUSPENDED'``).  These are the only valid values.
        reading_status: Three-state reading marker, one of ``'FRESH'`` (just
            collected, not yet looked at), ``'ACTIVE'`` (currently being
            read) or ``'DONE'`` (finished, eligible for auto-cleanup).
        created_at: ISO 8601 UTC timestamp of when the entry was created.
            Populated automatically when left as ``None``; used as the final
            fallback by the auto-cleanup age heuristic.
        done_at: ISO 8601 UTC timestamp of when the entry was marked
            ``'DONE'``, or ``None`` when it is not done. Used by the
            auto-suspend heuristic to age completed tabs.
    """

    tab_id: int
    parent_id: Optional[int] = None
    group_id: Optional[uuid.UUID] = None
    title: str = ''
    url: str = ''
    description: str = ''
    source: str = ''
    favicon: Optional[str] = None
    user_edited: dict[str, bool] = dataclasses.field(default_factory=dict)
    last_fetched: Optional[str] = None
    fetch_attempts: int = 0
    last_error: Optional[str] = None
    open_status: str = 'SUSPENDED'
    reading_status: str = 'FRESH'
    created_at: Optional[str] = None
    done_at: Optional[str] = None

    def __post_init__(self) -> None:
        """Ensure every user-editable field has an explicit flag."""
        edited = {
            key: bool(value)
            for key, value in (self.user_edited or {}).items()
        }
        for key in ('title', 'description', 'source'):
            edited.setdefault(key, False)
        self.user_edited = edited
        if self.created_at is None:
            self.created_at = datetime.datetime.now(
                datetime.timezone.utc).isoformat()

    def assign_group(self, group_id: Optional[uuid.UUID]) -> None:
        """Move this tab into *group_id* (or to standalone when None)."""
        self.group_id = group_id

    def clear_group(self) -> None:
        """Make this tab standalone by clearing its group membership."""
        self.group_id = None

    def clear_parent(self) -> None:
        """Detach this tab from its parent tab."""
        self.parent_id = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict."""
        return {
            'tab_id': self.tab_id,
            'parent_id': self.parent_id,
            'group_id': (
                str(self.group_id) if self.group_id is not None else None
            ),
            'title': self.title,
            'url': self.url,
            'description': self.description,
            'source': self.source,
            'favicon': self.favicon,
            'user_edited': dict(self.user_edited),
            'last_fetched': self.last_fetched,
            'fetch_attempts': self.fetch_attempts,
            'last_error': self.last_error,
            'open_status': self.open_status,
            'reading_status': self.reading_status,
            'created_at': self.created_at,
            'done_at': self.done_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'WorkspaceTab':
        """Deserialize from a primitive-only dict.

        Older YAML files lack the metadata fields added for issue #13; all
        of them are read with ``.get()`` defaults so those files load
        without migration.
        """
        raw_group_id = d.get('group_id')
        group_id = (
            uuid.UUID(raw_group_id) if raw_group_id is not None else None
        )
        raw_user_edited = d.get('user_edited') or {}
        return cls(
            tab_id=int(d['tab_id']),
            parent_id=d.get('parent_id'),
            group_id=group_id,
            title=d.get('title', '') or '',
            url=d.get('url', '') or '',
            description=d.get('description', '') or '',
            source=d.get('source', '') or '',
            favicon=d.get('favicon'),
            user_edited={
                key: bool(value)
                for key, value in raw_user_edited.items()
            },
            last_fetched=d.get('last_fetched'),
            fetch_attempts=int(d.get('fetch_attempts', 0) or 0),
            last_error=d.get('last_error'),
            open_status=d.get('open_status', 'SUSPENDED') or 'SUSPENDED',
            reading_status=d.get('reading_status', 'FRESH') or 'FRESH',
            created_at=d.get('created_at'),
            done_at=d.get('done_at', None),
        )


@dataclasses.dataclass
class Workspace:

    """A named workspace holding groups and tabs.

    Attributes:
        id: Unique identifier of this workspace.
        name: User-visible workspace name.
        groups: Groups defined in this workspace (opt-in, may be empty).
        tabs: Tab entries belonging to this workspace.
        last_active: ISO 8601 timestamp of when this workspace was last active.
        archived: Whether this workspace is archived (hidden from normal
            use, but not deleted).
        suggestion_ignored: Group ids the user has ignored in the smart
            suggestion card, mapped to the ISO 8601 timestamp until which
            they stay suppressed (VP-A2). Older state files without this
            key load with an empty mapping.
    """

    id: uuid.UUID
    name: str
    groups: list[Group] = dataclasses.field(default_factory=list)
    tabs: list[WorkspaceTab] = dataclasses.field(default_factory=list)
    last_active: Optional[str] = None
    archived: bool = False
    suggestion_ignored: dict[str, str] = dataclasses.field(
        default_factory=dict)

    def add_group(self, group: Group) -> None:
        """Append *group* to this workspace's group list."""
        self.groups.append(group)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict."""
        return {
            'id': str(self.id),
            'name': self.name,
            'groups': [group.to_dict() for group in self.groups],
            'tabs': [tab.to_dict() for tab in self.tabs],
            'last_active': self.last_active,
            'archived': self.archived,
            '_suggestion_ignored': dict(self.suggestion_ignored),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'Workspace':
        """Deserialize from a primitive-only dict."""
        ignored = {
            str(group_id): str(until)
            for group_id, until in (d.get('_suggestion_ignored') or {}).items()
        }
        return cls(
            id=uuid.UUID(d['id']),
            name=d['name'],
            groups=[Group.from_dict(g) for g in d.get('groups', [])],
            tabs=[WorkspaceTab.from_dict(t) for t in d.get('tabs', [])],
            last_active=d.get('last_active'),
            archived=bool(d.get('archived', False)),
            suggestion_ignored=ignored,
        )


@dataclasses.dataclass
class PinnedTab:

    """A profile-scoped pinned tab.

    Attributes:
        tab_id: The tab's integer id.
        title: The tab's last known title.
        url: The tab's last known URL.
        favicon: Raw favicon bytes (an embedded PNG), if available.
        last_active: ISO 8601 timestamp of when the tab was last active, or
            None if it has never been active.
    """

    tab_id: int
    title: str
    url: str
    favicon: Optional[bytes] = None
    last_active: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict (favicon as base64 ASCII)."""
        if self.favicon is None:
            encoded: Optional[str] = None
        else:
            encoded = base64.b64encode(self.favicon).decode('ascii')
        data: dict[str, Any] = {
            'tab_id': self.tab_id,
            'title': self.title,
            'url': self.url,
            'favicon': encoded,
        }
        if self.last_active is not None:
            data['last_active'] = self.last_active
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'PinnedTab':
        """Deserialize from a primitive-only dict (base64 favicon)."""
        raw_favicon = d.get('favicon')
        favicon = (
            None if raw_favicon is None else base64.b64decode(raw_favicon)
        )
        return cls(
            tab_id=int(d['tab_id']),
            title=d['title'],
            url=d['url'],
            favicon=favicon,
            last_active=d.get('last_active'),
        )
