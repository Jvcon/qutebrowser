# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistence helpers for workspaces and pinned tabs.

Workspace data lives in ``<data>/workspaces/<id>.yml`` (one file per
workspace), while pinned tabs are profile-scoped and live in
``<data>/profile/pinned.yml``.  Both use qutebrowser's YAML wrappers and
:func:`qutebrowser.utils.qtutils.savefile_open` for atomic writes.
"""

import pathlib
import uuid
from typing import Optional

from qutebrowser.utils import qtutils, standarddir, utils
from qutebrowser.workspaces.model import PinnedTab, Workspace


class WorkspacePersistence:

    """Read/write ``<data>/workspaces/<id>.yml`` files.

    Attributes:
        base_path: Directory containing the per-workspace YAML files.
    """

    def __init__(self, base_path: Optional[pathlib.Path] = None) -> None:
        if base_path is None:
            base_path = pathlib.Path(standarddir.data()) / 'workspaces'
        self.base_path = base_path
        self.base_path.mkdir(exist_ok=True)

    def _path(self, workspace_id: uuid.UUID | str) -> pathlib.Path:
        """Return the YAML path for a given workspace id."""
        return self.base_path / f'{workspace_id}.yml'

    def save(self, ws: Workspace) -> None:
        """Atomically save a single workspace."""
        with qtutils.savefile_open(str(self._path(ws.id))) as f:
            utils.yaml_dump(ws.to_dict(), f)

    def load(self, workspace_id: uuid.UUID | str) -> Workspace:
        """Load a single workspace.

        Raises:
            FileNotFoundError: If the workspace file does not exist.
        """
        path = self._path(workspace_id)
        with path.open('r', encoding='utf-8') as f:
            data = utils.yaml_load(f)
        if data is None:
            raise FileNotFoundError(f'{path} is empty')
        return Workspace.from_dict(data)

    def load_all(self) -> list[Workspace]:
        """Load every workspace found in ``base_path`` (sorted by id)."""
        workspaces = [self.load(path.stem)
                      for path in self.base_path.glob('*.yml')]
        workspaces.sort(key=lambda ws: str(ws.id))
        return workspaces

    def delete(self, workspace_id: uuid.UUID | str) -> None:
        """Delete a workspace file, if it exists."""
        self._path(workspace_id).unlink(missing_ok=True)


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
