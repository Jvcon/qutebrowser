# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistence helpers for workspaces and pinned tabs.

Workspace data lives in ``<data>/workspaces/<id>.yml`` (one file per
workspace), while pinned tabs are profile-scoped and live in
``<data>/profile/pinned.yml``.  Both use qutebrowser's YAML wrappers and
:func:`qutebrowser.utils.qtutils.savefile_open` for atomic writes.

To keep startup cheap, the id of the active workspace is stored separately in
``<data>/workspaces/_state.yml``::

    active_id: <uuid>

This lets :class:`~qutebrowser.workspaces.manager.WorkspaceManager` load just
the one workspace the user was last in, instead of globbing and parsing every
workspace file.  The ``_state.yml`` file is skipped by
:meth:`WorkspacePersistence.load_all`.
"""

import pathlib
import uuid
from typing import Optional

import yaml

from qutebrowser.utils import log, qtutils, standarddir, utils
from qutebrowser.workspaces.model import PinnedTab, Workspace


#: Name of the active-workspace state file inside ``base_path``.
STATE_FILENAME = '_state.yml'


class WorkspacePersistence:

    """Read/write ``<data>/workspaces/<id>.yml`` files.

    Attributes:
        base_path: Directory containing the per-workspace YAML files.
    """

    def __init__(self, base_path: Optional[pathlib.Path] = None) -> None:
        if base_path is None:
            base_path = pathlib.Path(standarddir.data()) / 'workspaces'
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _path(self, workspace_id: uuid.UUID | str) -> pathlib.Path:
        """Return the YAML path for a given workspace id."""
        return self.base_path / f'{workspace_id}.yml'

    @property
    def state_path(self) -> pathlib.Path:
        """Return the path of the active-workspace state file."""
        return self.base_path / STATE_FILENAME

    def save(self, ws: Workspace) -> None:
        """Atomically save a single workspace."""
        with qtutils.savefile_open(str(self._path(ws.id))) as f:
            utils.yaml_dump(ws.to_dict(), f)

    def load(self, workspace_id: uuid.UUID | str) -> Workspace:
        """Load a single workspace.

        Raises if the file is missing or malformed; callers that want to
        tolerate bad data should catch ``FileNotFoundError``, ``KeyError``,
        ``ValueError`` and ``yaml.YAMLError``.
        """
        path = self._path(workspace_id)
        with path.open('r', encoding='utf-8') as f:
            data = utils.yaml_load(f)
        if data is None:
            raise FileNotFoundError(f'{path} is empty')
        return Workspace.from_dict(data)

    def load_all(self) -> list[Workspace]:
        """Load every workspace found in ``base_path`` (sorted by id).

        Corrupt or unreadable workspace files are logged and skipped rather
        than aborting the whole load.
        """
        workspaces = []
        for path in sorted(self.base_path.glob('*.yml')):
            if path.name == STATE_FILENAME:
                continue
            try:
                workspaces.append(self.load(path.stem))
            except (yaml.YAMLError, KeyError, ValueError, OSError) as e:
                log.message.warning(
                    f"Ignoring corrupt workspace file {path}: {e}")
        workspaces.sort(key=lambda ws: str(ws.id))
        return workspaces

    def delete(self, workspace_id: uuid.UUID | str) -> None:
        """Delete a workspace file, if it exists."""
        self._path(workspace_id).unlink(missing_ok=True)

    def save_state(self, active_id: Optional[uuid.UUID]) -> None:
        """Atomically persist the id of the active workspace (or None)."""
        data = {'active_id': str(active_id) if active_id is not None else None}
        with qtutils.savefile_open(str(self.state_path)) as f:
            utils.yaml_dump(data, f)

    def load_state(self) -> Optional[uuid.UUID]:
        """Load the persisted active workspace id, if any.

        Returns None when the state file is missing, empty, or holds an
        invalid id.
        """
        if not self.state_path.exists():
            return None
        with self.state_path.open('r', encoding='utf-8') as f:
            data = utils.yaml_load(f)
        if not data:
            return None
        raw = data.get('active_id')
        if raw is None:
            return None
        try:
            return uuid.UUID(str(raw))
        except ValueError:
            log.message.warning(
                f"Ignoring invalid active workspace id {raw!r}")
            return None


class PinnedPersistence:

    """Read/write ``<data>/profile/pinned.yml``.

    Attributes:
        path: Path to the pinned-tabs YAML file.
    """

    def __init__(self, path: Optional[pathlib.Path] = None) -> None:
        if path is None:
            self.path = (pathlib.Path(standarddir.data()) /
                         'profile' / 'pinned.yml')
        else:
            self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, pinned: list[PinnedTab]) -> None:
        """Atomically save the pinned tab list."""
        data = {'pinned': [tab.to_dict() for tab in pinned]}
        with qtutils.savefile_open(str(self.path)) as f:
            utils.yaml_dump(data, f)

    def load(self) -> list[PinnedTab]:
        """Load the pinned tab list (empty if no file exists yet)."""
        if not self.path.exists():
            return []
        with self.path.open('r', encoding='utf-8') as f:
            data = utils.yaml_load(f)
        if data is None:
            return []
        return [PinnedTab.from_dict(d) for d in data.get('pinned', [])]
