# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistence helpers for workspaces and pinned tabs.

All workspace data lives under a per-profile directory::

    <data>/profiles/<name>/workspaces/
        <workspace-uuid>.yml      # per-workspace state
        _state.yml                # active_id (skipped by load_all)
        pinned.yml                # app-wide pinned tabs within this profile

The two persistence classes (:class:`WorkspacePersistence`,
:class:`PinnedPersistence`) both take a ``profile_name`` (default
``'default'``) and route their reads/writes to the matching subdir.
The layout is designed to align with upstream qutebrowser's planned
``#5935`` refactor (``ProfileFactory`` + per-name ``QWebEngineProfile``)
— ``<data>/profiles/<name>/`` is the same root the upstream profile
factory is expected to use, with our workspace state living in a
sibling ``workspaces/`` subdir so it doesn't collide with the
upstream-managed cookie/history/cache storage.

Legacy migration
================

Pre-Profile data was stored at the basedir root::

    <data>/workspaces/<uuid>.yml
    <data>/workspaces/_state.yml
    <data>/profile/pinned.yml

On the first launch after upgrading, :meth:`WorkspacePersistence.__init__`
runs :meth:`WorkspacePersistence._migrate_legacy` (delegated to
:meth:`_migrate_legacy_files`) which moves any data found at the
old locations into the new ``profiles/default/workspaces/`` tree
and removes the now-empty legacy directories. The migration is
idempotent and emits a single info log line on first run.
"""

import pathlib
import shutil
import uuid
from typing import Optional

import yaml

from qutebrowser.utils import log, qtutils, standarddir, utils
from qutebrowser.workspaces.model import PinnedTab, Profile, Workspace


#: Name of the active-workspace state file inside the per-profile dir.
STATE_FILENAME = '_state.yml'

#: Name of the default profile (used when no config override).
DEFAULT_PROFILE_NAME = 'default'


def _legacy_paths(data_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """Return the pre-Profile path locations under *data_dir*.

    These exist only for users who upgraded from a qutebrowser build
    that pre-dates the profiles/<name>/ layout. New installs never see
    them.
    """
    return {
        'workspaces_dir': data_dir / 'workspaces',
        'pinned_file': data_dir / 'profile' / 'pinned.yml',
    }


def _migrate_legacy_files(
    src: pathlib.Path,
    dst_dir: pathlib.Path,
    *,
    new_already_exists: bool = False,
) -> bool:
    """Move *src* into *dst_dir* (preserving its filename) if it exists.

    Returns True when a file was actually moved. When the new layout
    already has a file with the same name, the legacy file is removed
    silently — the new file is considered authoritative (this matches
    the behavior we want if a user has been running two builds side
    by side during the migration window).
    """
    if not src.exists():
        return False
    if not src.is_file():
        return False
    dst = dst_dir / src.name
    if dst.exists():
        src.unlink()
        return True
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return True


class WorkspacePersistence:

    """Read/write the per-profile workspace YAML files.

    Attributes:
        profile_name: The name of the profile this instance serves.
        base_path: The per-profile directory (``<data>/profiles/<name>/workspaces/``).
    """

    def __init__(
        self,
        profile_name: str = DEFAULT_PROFILE_NAME,
        base_path: Optional[pathlib.Path] = None,
    ) -> None:
        self.profile_name = profile_name
        # Track whether the caller passed an explicit base_path. When
        # True we treat this instance as a test / caller-controlled
        # context and skip the legacy migration — the migration only
        # makes sense against the real ``standarddir.data()`` basedir.
        self._is_default_path = base_path is None
        if base_path is None:
            base_path = (
                Profile.for_name(profile_name).basedir / 'workspaces'
            )
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        """One-shot migration from the pre-Profile layout.

        Moves any data found at the old locations into
        ``profiles/<self.profile_name>/workspaces/``. Idempotent: a
        second run is a no-op because the legacy files are already
        gone. Emits one info log line on the migration that actually
        moves something; silent thereafter.
        """
        if not self._is_default_path:
            # Caller provided an explicit base_path (typical for tests
            # or a manual override). We do not touch the real basedir.
            return
        if self.profile_name != DEFAULT_PROFILE_NAME:
            # We only auto-migrate into the default profile. Other
            # profiles start empty; if a user wants their legacy data
            # under a non-default profile, they can move it manually
            # after first launch. This avoids silently swallowing data
            # into a profile the user did not choose.
            return
        legacy = _legacy_paths(pathlib.Path(standarddir.data()))
        moved_workspace = False
        workspaces_legacy = legacy['workspaces_dir']
        if workspaces_legacy.is_dir():
            self.base_path.mkdir(parents=True, exist_ok=True)
            for src in sorted(workspaces_legacy.glob('*.yml')):
                if _migrate_legacy_files(src, self.base_path):
                    moved_workspace = True
            # Remove the now-empty legacy dir; ignore non-empty
            # (shouldn't happen since we just moved every .yml out).
            try:
                if not any(workspaces_legacy.iterdir()):
                    workspaces_legacy.rmdir()
            except OSError:
                pass
        moved_pinned = _migrate_legacy_files(
            legacy['pinned_file'], self.base_path
        )
        if moved_workspace or moved_pinned:
            log.message.info(
                f"Migrated workspace data to "
                f"profiles/{self.profile_name}/workspaces/"
            )

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

    """Read/write the per-profile pinned-tabs YAML file.

    Attributes:
        profile_name: The name of the profile this instance serves.
        path: Path to the pinned-tabs YAML file
            (``<data>/profiles/<name>/workspaces/pinned.yml``).
    """

    def __init__(
        self,
        profile_name: str = DEFAULT_PROFILE_NAME,
        path: Optional[pathlib.Path] = None,
    ) -> None:
        self.profile_name = profile_name
        # See WorkspacePersistence.__init__ for the rationale.
        self._is_default_path = path is None
        if path is None:
            path = (
                Profile.for_name(profile_name).basedir
                / 'workspaces' / 'pinned.yml'
            )
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        """One-shot migration of ``<data>/profile/pinned.yml``.

        Only runs when the path is the default one (no caller override)
        and the profile is the default. See
        :meth:`WorkspacePersistence._migrate_legacy` for the no-clobber
        policy.
        """
        if not self._is_default_path:
            return
        if self.profile_name != DEFAULT_PROFILE_NAME:
            return
        legacy = _legacy_paths(pathlib.Path(standarddir.data()))
        if _migrate_legacy_files(legacy['pinned_file'], self.path.parent):
            log.message.info(
                f"Migrated pinned tabs to "
                f"profiles/{self.profile_name}/workspaces/pinned.yml"
            )

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
