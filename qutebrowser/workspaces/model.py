# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Data model for workspaces, groups, workspace tabs, and pinned tabs.

The dataclasses here are intentionally serialization-agnostic: each provides a
manual ``to_dict``/``from_dict`` pair that produces and consumes only primitive
types (``str``, ``int``, ``bool``, ``None``) so the result can be handed
straight to :func:`qutebrowser.utils.utils.yaml_dump`.  ``uuid.UUID`` values are
stored as strings and ``bytes`` favicons as base64 ASCII strings.
"""

import base64
import dataclasses
import uuid
from typing import Any, Optional


@dataclasses.dataclass
class Group:

    """A named, foldable group of tabs within a workspace.

    Attributes:
        group_id: Unique identifier of this group.
        name: User-visible group name.
        folded: Whether the group is collapsed in the TabTree.
    """

    group_id: uuid.UUID
    name: str
    folded: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict."""
        return {
            'group_id': str(self.group_id),
            'name': self.name,
            'folded': self.folded,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'Group':
        """Deserialize from a primitive-only dict."""
        return cls(
            group_id=uuid.UUID(d['group_id']),
            name=d['name'],
            folded=bool(d.get('folded', False)),
        )


@dataclasses.dataclass
class WorkspaceTab:

    """A single tab entry inside a workspace.

    Attributes:
        tab_id: The tab's integer id (matches the runtime tab registry).
        parent_id: The ``tab_id`` of the parent tab, if any.
        group_id: The ``group_id`` of the group this tab belongs to, if any.
    """

    tab_id: int
    parent_id: Optional[int] = None
    group_id: Optional[uuid.UUID] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict."""
        return {
            'tab_id': self.tab_id,
            'parent_id': self.parent_id,
            'group_id': (
                str(self.group_id) if self.group_id is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'WorkspaceTab':
        """Deserialize from a primitive-only dict."""
        raw_group_id = d.get('group_id')
        group_id = (
            uuid.UUID(raw_group_id) if raw_group_id is not None else None
        )
        return cls(
            tab_id=int(d['tab_id']),
            parent_id=d.get('parent_id'),
            group_id=group_id,
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
    """

    id: uuid.UUID
    name: str
    groups: list[Group] = dataclasses.field(default_factory=list)
    tabs: list[WorkspaceTab] = dataclasses.field(default_factory=list)
    last_active: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict."""
        return {
            'id': str(self.id),
            'name': self.name,
            'groups': [group.to_dict() for group in self.groups],
            'tabs': [tab.to_dict() for tab in self.tabs],
            'last_active': self.last_active,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'Workspace':
        """Deserialize from a primitive-only dict."""
        return cls(
            id=uuid.UUID(d['id']),
            name=d['name'],
            groups=[Group.from_dict(g) for g in d.get('groups', [])],
            tabs=[WorkspaceTab.from_dict(t) for t in d.get('tabs', [])],
            last_active=d.get('last_active'),
        )


@dataclasses.dataclass
class PinnedTab:

    """A profile-scoped pinned tab.

    Attributes:
        tab_id: The tab's integer id.
        title: The tab's last known title.
        url: The tab's last known URL.
        favicon: Raw favicon bytes (an embedded PNG), if available.
        last_active: ISO 8601 timestamp of when the tab was last active.
    """

    tab_id: int
    title: str
    url: str
    favicon: Optional[bytes] = None
    last_active: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a primitive-only dict (favicon as base64 ASCII)."""
        if self.favicon is None:
            encoded: Optional[str] = None
        else:
            encoded = base64.b64encode(self.favicon).decode('ascii')
        return {
            'tab_id': self.tab_id,
            'title': self.title,
            'url': self.url,
            'favicon': encoded,
            'last_active': self.last_active,
        }

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
            last_active=d.get('last_active', ''),
        )
