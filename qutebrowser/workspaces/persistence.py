# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistence helpers for workspaces and pinned tabs.

All workspace data lives under a per-profile directory::

    <data>/profiles/<name>/workspaces/
        <workspace-uuid>.yml      # per-workspace state
        _state.yml                # active_id (skipped by load_all)
        _journal.yml              # crash journal, present only while running
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

import dataclasses
import datetime
import hashlib
import json
import pathlib
import shutil
import uuid
from typing import Any, Optional

import yaml

from qutebrowser.utils import log, qtutils, standarddir, utils
from qutebrowser.workspaces.model import PinnedTab, Profile, Workspace


#: Name of the active-workspace state file inside the per-profile dir.
STATE_FILENAME = '_state.yml'

#: Name of the crash journal inside the per-profile dir (issue #7).
JOURNAL_FILENAME = '_journal.yml'

#: Schema version of the crash journal envelope.
JOURNAL_FORMAT = 1

#: Name of the default profile (used when no config override).
DEFAULT_PROFILE_NAME = 'default'

#: Exceptions which mean "this file is unreadable or malformed".  Corrupt
#: workspace data must degrade to a warning, never abort startup.
CORRUPT_DATA_ERRORS = (
    yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError, OSError)


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
            if not _is_workspace_file(path):
                continue
            try:
                workspaces.append(self.load(path.stem))
            except CORRUPT_DATA_ERRORS as e:
                log.message.warning(
                    f"Ignoring corrupt workspace file {path}: {e}")
        workspaces.sort(key=lambda ws: str(ws.id))
        return workspaces

    def delete(self, workspace_id: uuid.UUID | str) -> None:
        """Delete a workspace file, if it exists."""
        self._path(workspace_id).unlink(missing_ok=True)

    def save_state(
            self,
            active_id: Optional[uuid.UUID],
            last_active_tabs: Optional[
                dict[uuid.UUID, Optional[int]]] = None,
    ) -> None:
        """Atomically persist the whole active-workspace state.

        The state file carries the active workspace id plus the per-workspace
        last-active tab ids::

            active_id: <uuid>
            last_active_tabs:
              <uuid>: <int>
              <uuid>: null

        Both keys are written in a single ``QSaveFile`` commit so readers
        never observe a half-written state. ``last_active_tabs`` defaults to
        an empty map so legacy callers which only know the active id keep
        working (and drop the map from the file, which is harmless: a missing
        map is treated as empty on load).
        """
        last_active_tabs = last_active_tabs or {}
        data = {
            'active_id': str(active_id) if active_id is not None else None,
            'last_active_tabs': {
                str(workspace_id): tab_id
                for workspace_id, tab_id in last_active_tabs.items()
            },
        }
        with qtutils.savefile_open(str(self.state_path)) as f:
            utils.yaml_dump(data, f)

    def load_state_data(
            self,
    ) -> tuple[Optional[uuid.UUID],
               dict[uuid.UUID, Optional[int]]]:
        """Load the persisted active id plus the last-active tab map.

        Legacy state files written before per-workspace tabs were tracked
        only contain ``active_id``; they load with an empty map. Invalid ids
        (and malformed map entries) are ignored rather than aborting the
        whole load.
        """
        empty: dict[uuid.UUID, Optional[int]] = {}
        if not self.state_path.exists():
            return None, empty
        try:
            with self.state_path.open('r', encoding='utf-8') as f:
                data = utils.yaml_load(f)
        except CORRUPT_DATA_ERRORS as e:
            log.message.warning(
                f"Ignoring corrupt workspace state {self.state_path}: {e}")
            return None, empty
        if not isinstance(data, dict):
            return None, empty

        active_id: Optional[uuid.UUID] = None
        raw = data.get('active_id')
        if raw is not None:
            try:
                active_id = uuid.UUID(str(raw))
            except ValueError:
                log.message.warning(
                    f"Ignoring invalid active workspace id {raw!r}")

        last_active_tabs = _parse_last_active_tabs(data.get('last_active_tabs'))
        return active_id, last_active_tabs

    def load_state(self) -> Optional[uuid.UUID]:
        """Load the persisted active workspace id, if any.

        Returns None when the state file is missing, empty, or holds an
        invalid id. Per-workspace tab ids are ignored by this accessor; use
        :meth:`load_state_data` to read both.
        """
        active_id, _last_active_tabs = self.load_state_data()
        return active_id


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
        """Load the pinned tab list (empty if no file exists yet).

        A corrupt file or malformed entry is logged and skipped so a bad
        pinned.yml never blocks startup.
        """
        if not self.path.exists():
            return []
        try:
            with self.path.open('r', encoding='utf-8') as f:
                data = utils.yaml_load(f)
        except CORRUPT_DATA_ERRORS as e:
            log.message.warning(f"Ignoring corrupt pinned tabs {self.path}: {e}")
            return []
        if not isinstance(data, dict):
            return []
        pinned = []
        for raw in data.get('pinned') or []:
            try:
                pinned.append(PinnedTab.from_dict(raw))
            except CORRUPT_DATA_ERRORS as e:
                log.message.warning(f"Ignoring malformed pinned tab {raw!r}: {e}")
        return pinned


def _parse_last_active_tabs(
        raw_tabs: Any) -> dict[uuid.UUID, Optional[int]]:
    """Parse the ``last_active_tabs`` map from a state file, skipping bad rows."""
    last_active_tabs: dict[uuid.UUID, Optional[int]] = {}
    if not isinstance(raw_tabs, dict):
        return last_active_tabs
    for raw_id, raw_tab_id in raw_tabs.items():
        try:
            workspace_id = uuid.UUID(str(raw_id))
        except ValueError:
            log.message.warning(
                f"Ignoring invalid workspace id {raw_id!r} in "
                "last_active_tabs")
            continue
        if raw_tab_id is not None and not isinstance(raw_tab_id, int):
            log.message.warning(
                f"Ignoring invalid last-active tab id {raw_tab_id!r} "
                f"for workspace {workspace_id}")
            continue
        last_active_tabs[workspace_id] = raw_tab_id
    return last_active_tabs


def _is_workspace_file(path: pathlib.Path) -> bool:
    """Return True if *path* holds a workspace (not an aux/reserved file).

    Underscore-prefixed files (``_state.yml``, ``_journal.yml`` and the
    quarantined journal copies) are bookkeeping, never workspaces.  Workspace
    ids are UUIDs and never start with an underscore.  ``pinned.yml`` lives in
    the same directory and is skipped too.
    """
    return not path.name.startswith('_') and path.name != 'pinned.yml'


@dataclasses.dataclass
class JournalSnapshot:

    """A decoded crash journal: the in-memory state at the last flush.

    Attributes:
        written_at: ISO 8601 UTC timestamp of the flush.
        active_id: The active workspace id, if any.
        last_active_tabs: Per-workspace last-active runtime tab ids.
        workspaces: The workspaces that were loaded at the last flush.
        pinned: The app-wide pinned tabs at the last flush.
    """

    written_at: str
    active_id: Optional[uuid.UUID]
    last_active_tabs: dict[uuid.UUID, Optional[int]]
    workspaces: list[Workspace]
    pinned: list[PinnedTab]

    @property
    def tab_count(self) -> int:
        """Return the number of tab entries across all journaled workspaces."""
        return sum(len(workspace.tabs) for workspace in self.workspaces)


class JournalError(ValueError):

    """The crash journal is torn, truncated, edited, or of an unknown format."""


def _checksum(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 digest of a journal *payload*."""
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def encode_journal(snapshot: JournalSnapshot) -> dict[str, Any]:
    """Return the checksummed on-disk envelope for *snapshot*."""
    payload: dict[str, Any] = {
        'format': JOURNAL_FORMAT,
        'written_at': snapshot.written_at,
        'active_id': (
            str(snapshot.active_id) if snapshot.active_id is not None else None),
        'last_active_tabs': {
            str(workspace_id): tab_id
            for workspace_id, tab_id in snapshot.last_active_tabs.items()
        },
        'workspaces': [ws.to_dict() for ws in snapshot.workspaces],
        'pinned': [tab.to_dict() for tab in snapshot.pinned],
    }
    return {'checksum': _checksum(payload), 'payload': payload}


def _decode_workspaces(raw_list: Any) -> list[Workspace]:
    """Decode journaled workspaces, skipping any that are unreadable."""
    workspaces = []
    for raw in raw_list or []:
        try:
            workspaces.append(Workspace.from_dict(raw))
        except CORRUPT_DATA_ERRORS as e:
            log.workspaces.warning(f"Skipping unreadable journal workspace: {e}")
    return workspaces


def _decode_pinned(raw_list: Any) -> list[PinnedTab]:
    """Decode journaled pinned tabs, skipping any that are unreadable."""
    pinned = []
    for raw in raw_list or []:
        try:
            pinned.append(PinnedTab.from_dict(raw))
        except CORRUPT_DATA_ERRORS as e:
            log.workspaces.warning(f"Skipping unreadable journal pinned tab: {e}")
    return pinned


def _decode_active_id(raw: Any) -> Optional[uuid.UUID]:
    """Decode the journaled active workspace id, or None if absent/invalid."""
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        log.workspaces.warning("Ignoring invalid active id in crash journal")
        return None


def decode_journal(data: Any) -> JournalSnapshot:
    """Validate and decode a journal envelope.

    Raises:
        JournalError: when the envelope is not a mapping, the checksum does not
            match (a torn or hand-edited write), or the format is unknown.
    """
    if not isinstance(data, dict):
        raise JournalError('journal is not a mapping')
    payload = data.get('payload')
    checksum = data.get('checksum')
    if not isinstance(payload, dict) or not isinstance(checksum, str):
        raise JournalError('journal has no payload or checksum')
    if _checksum(payload) != checksum:
        raise JournalError('checksum mismatch (torn or edited write)')
    if payload.get('format') != JOURNAL_FORMAT:
        raise JournalError(f"unsupported journal format {payload.get('format')!r}")

    last_active_tabs = _parse_last_active_tabs(payload.get('last_active_tabs'))
    return JournalSnapshot(
        written_at=str(payload.get('written_at') or ''),
        active_id=_decode_active_id(payload.get('active_id')),
        last_active_tabs=last_active_tabs,
        workspaces=_decode_workspaces(payload.get('workspaces')),
        pinned=_decode_pinned(payload.get('pinned')),
    )


class JournalPersistence:

    """Read/write the crash journal (``_journal.yml``) for one profile.

    The journal is a full snapshot of the manager's loaded state, rewritten
    atomically (``QSaveFile``) and checksummed so a torn write is detected on
    the next launch and ignored instead of bricking startup.  It exists only
    while qutebrowser is running: a clean shutdown deletes it, so finding one
    at startup means the previous run did not exit cleanly.

    Attributes:
        base_path: The per-profile workspaces directory.
        path: The journal file path.
    """

    def __init__(self, base_path: pathlib.Path) -> None:
        self.base_path = base_path
        self.path = base_path / JOURNAL_FILENAME

    def write(self, snapshot: JournalSnapshot) -> None:
        """Atomically replace the journal with *snapshot*."""
        envelope = encode_journal(snapshot)
        with qtutils.savefile_open(str(self.path)) as f:
            utils.yaml_dump(envelope, f)

    def read(self) -> Optional[JournalSnapshot]:
        """Return the journal from the last run, or None.

        A missing journal returns None.  An unreadable, torn, or edited journal
        is logged, moved aside (kept on disk, never loaded again) and treated
        as absent, so it can never block startup.
        """
        if not self.path.exists():
            return None
        try:
            with self.path.open('r', encoding='utf-8') as f:
                data = utils.yaml_load(f)
            return decode_journal(data)
        except CORRUPT_DATA_ERRORS as e:
            log.workspaces.warning(
                f"Ignoring unreadable crash journal {self.path}: {e}")
            self.preserve('corrupt')
            return None

    def discard(self) -> None:
        """Delete the journal (clean shutdown)."""
        self.path.unlink(missing_ok=True)

    def preserve(self, reason: str) -> Optional[pathlib.Path]:
        """Move the journal aside under a unique name and return the new path.

        Used for declined recovery and corrupt journals so the data stays on
        disk.  The timestamped name never overwrites an earlier copy.
        """
        if not self.path.exists():
            return None
        stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
        dst = self.base_path / f'_journal.{reason}-{stamp}.yml'
        try:
            self.path.replace(dst)
        except OSError as e:
            log.workspaces.warning(f"Could not preserve crash journal: {e}")
            return None
        return dst
