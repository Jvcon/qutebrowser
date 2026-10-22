# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""In-memory workspace / pinned-tab state and its persistence orchestration.

:class:`WorkspaceManager` is the central state holder used by the widget layer
(Phase 2).  It owns the loaded :class:`~qutebrowser.workspaces.model.Workspace`
objects and pinned tabs, exposes CRUD operations over them, and emits
:attr:`changed` whenever state mutates so it can be registered as a
``Saveable``.
"""

import dataclasses
import datetime
import itertools
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Callable, Optional

from qutebrowser.qt.core import QObject, QTimer, QUrl, pyqtSignal

from qutebrowser.config import config, configexc
from qutebrowser.utils.qtutils import weak_slot
from qutebrowser.utils import log, message, objreg, qtutils
from qutebrowser.workspaces.model import (Group, PinnedTab, Workspace,
                                          WorkspaceTab)
from qutebrowser.workspaces.persistence import (CORRUPT_DATA_ERRORS,
                                                JournalPersistence,
                                                JournalSnapshot,
                                                PinnedPersistence,
                                                WorkspacePersistence)

#: Batch window (ms) for crash-journal writes.  The first change opens the
#: window; every change inside it is coalesced into one disk write when it
#: closes.  A window (rather than a debounce) bounds the latency under a
#: constant stream of changes.
JOURNAL_BATCH_MS = 500

#: Tab entries resident at which ADR 0008's memory budget was measured
#: (``tracemalloc``, 326 KiB against a 512 KiB budget).  Used as the runtime
#: proxy for that budget -- see :meth:`WorkspaceManager._report_resident_size`.
MEMORY_BUDGET_ENTRIES = 500


@dataclasses.dataclass(frozen=True)
class RecoverySummary:

    """What a crash recovery restored.

    Attributes:
        workspaces: Number of workspaces restored from the journal.
        tabs: Total number of tab entries restored across those workspaces.
    """

    workspaces: int
    tabs: int


def _now() -> str:
    """Return the current time as an ISO 8601 UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _now_seconds() -> str:
    """Return the current UTC timestamp truncated to whole seconds."""
    return datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec='seconds')


def _host(url: str) -> str:
    """Return the lowercase host component of *url* ('' when unavailable)."""
    if not url:
        return ''
    return QUrl(url).host().lower()


def _snapshot_group_name() -> str:
    """Return the base name for a window-close snapshot group."""
    return "快照退出"


def _window_affinity_globally_enabled() -> bool:
    """Return the global window-affinity kill switch (default: on).

    ``config.val`` is ``None`` in bare unit tests, in which case affinity is
    treated as enabled (the production default). Missing attributes are also
    treated as enabled so the feature degrades gracefully.
    """
    try:
        return bool(
            config.val.content.vertical_panel.window_affinity_enabled)
    except AttributeError:
        return True


# The three reading states of a WorkspaceTab (issue #15).
READING_STATUSES = ('FRESH', 'ACTIVE', 'DONE')

#: Rough memory estimate per OPEN tab (VP-P1).
# TODO(vp-p1-followup): switch to chrome.processes for real values.
BYTES_PER_TAB = 50 * 1024 * 1024  # 50 MB heuristic; MVP estimate.

#: Default cooldown for ignored smart suggestions (VP-A2).
SUGGESTION_IGNORE_DAYS = 7


def _vertical_panel_setting(name: str, default: Any) -> Any:
    """Return a ``content.vertical_panel`` setting or *default*.

    Headless unit tests often install a minimal ``config.val`` stub (or no
    config at all), so missing settings fall back to their documented
    defaults instead of raising.
    """
    try:
        panel = config.val.content.vertical_panel
    except AttributeError:
        return default
    return getattr(panel, name, default)


def _auto_group_rules() -> list[Mapping[str, Any]]:
    """Return the configured ``auto_group_rules`` entries.

    Returns an empty list when the config is unavailable (headless tests,
    startup before config init) so rule evaluation never breaks tab
    registration.
    """
    try:
        rules = config.instance.get('auto_group_rules')
    except (AttributeError, configexc.NoOptionError):
        # ``config.instance`` is None before config init (headless tests) or
        # the option might be missing in a stubbed config module.
        return []
    return [rule for rule in (rules or []) if isinstance(rule, Mapping)]


def _matching_auto_group_rule(url: str) -> Optional[str]:
    """Return the first matching rule's group name for *url*, if any.

    Rules are ordered by ``priority`` descending; equal priorities keep
    their declaration order.  Invalid regexes are logged and skipped so a
    bad rule can never break the tab-open flow (ADR 0008 § 5).
    """
    if not url:
        return None

    ordered: list[tuple[int, int, str, str]] = []
    for index, rule in enumerate(_auto_group_rules()):
        pattern = rule.get('pattern')
        group = rule.get('group')
        priority = rule.get('priority', 0)
        if not isinstance(pattern, str) or not pattern:
            log.workspaces.warning(
                "auto_group_rules: skipping rule without a pattern: %r",
                rule)
            continue
        if not isinstance(group, str) or not group:
            log.workspaces.warning(
                "auto_group_rules: skipping rule without a group: %r", rule)
            continue
        if isinstance(priority, bool) or not isinstance(priority, int):
            log.workspaces.warning(
                "auto_group_rules: skipping rule with invalid priority: %r",
                rule)
            continue
        ordered.append((priority, index, pattern, group))

    ordered.sort(key=lambda item: (-item[0], item[1]))
    for _priority, _index, pattern, group in ordered:
        try:
            if re.search(pattern, url):
                return group
        except re.error as e:
            log.workspaces.warning(
                "auto_group_rules: skipping invalid regex %r: %s", pattern, e)
    return None


def _latest_group_activity(
        workspace: Workspace,
        group: Group,
) -> Optional[datetime.datetime]:
    """Return the newest member timestamp for *group* (VP-A2).

    Mirrors VP-P3's access semantics: ``done_at`` when set, otherwise the
    member's ``created_at``. Falls back to the Group's ``created_at`` when
    no member has a usable timestamp (or the Group has no members).
    """
    latest: Optional[datetime.datetime] = None
    for tab in workspace.tabs:
        if tab.group_id != group.group_id:
            continue
        stamp = tab.done_at or tab.created_at
        parsed = _parse_timestamp(stamp) if stamp else None
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    if latest is None and group.created_at:
        latest = _parse_timestamp(group.created_at)
    return latest


def _parse_timestamp(value: str) -> Optional[datetime.datetime]:
    """Parse an ISO 8601 *value* into an aware UTC datetime, or None.

    Naive timestamps are assumed to be UTC so comparisons against the
    timezone-aware cutoff never raise.
    """
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


class WorkspaceMoveError(ValueError):

    """A cross-workspace tab move was refused.

    ``str(error)`` is a user-facing message suitable for ``message.error``.
    """


def _free_id(occupied: set[int], preferred: int) -> int:
    """Return *preferred* when unused, else the smallest unused positive id."""
    if preferred not in occupied:
        return preferred
    candidate = 1
    while candidate in occupied:
        candidate += 1
    return candidate


def _descendant_ids(workspace: Workspace, tab_id: int) -> set[int]:
    """Return the ids of every tab nested (at any depth) under *tab_id*.

    *tab_id* itself is not included.  Visited ids are tracked so a corrupt
    parent cycle in the data cannot loop forever.
    """
    found: set[int] = set()
    frontier = [tab_id]
    while frontier:
        current = frontier.pop()
        for tab in workspace.tabs:
            if (tab.parent_id == current and tab.tab_id != tab_id
                    and tab.tab_id not in found):
                found.add(tab.tab_id)
                frontier.append(tab.tab_id)
    return found


@dataclasses.dataclass
class TabMetadata:

    """Metadata captured from a live tab for collection/backfill.

    This intentionally does not depend on the browser layer so the manager
    can be unit-tested with plain strings.
    """

    url: str = ''
    title: str = ''
    description: str = ''
    source: str = ''
    favicon: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.source:
            self.source = _host(self.url)


class WorkspaceManager(QObject):

    """In-memory store of workspaces + pinned tabs; persists via Saveable.

    Only the active workspace is loaded eagerly (see :meth:`load`); other
    workspaces are loaded on demand through :meth:`get_workspace`.

    Signals:
        changed: Emitted whenever managed state changes.
        pinned_changed: Emitted whenever the pinned-tab list changes
            (add / remove / initial load). UI consumers (e.g. the
            VerticalPanel's PINPanel) listen to this to refresh
            without having to poll.
        active_workspace_changed: Emitted after a successful workspace
            switch with ``(workspace_id, restored_tab_id)``. The widget
            layer (WorkspaceBridge) uses this to filter/rebuild the tab
            surface and restore focus to the workspace's last-active tab.
            ``restored_tab_id`` is ``None`` when no tab was recorded yet.
    """

    changed = pyqtSignal()
    pinned_changed = pyqtSignal()
    active_workspace_changed = pyqtSignal(object, object)
    #: Emitted after a periodic/startup scan with the current stale
    #: Group suggestions (``list[Group]``), so the panel can refresh its
    #: non-modal suggestion card (VP-A2).
    suggestions_changed = pyqtSignal(list)

    def __init__(self, persistence: WorkspacePersistence,
                 pinned_persistence: PinnedPersistence,
                 journal: Optional[JournalPersistence] = None) -> None:
        super().__init__()
        self._persistence = persistence
        self._pinned_persistence = pinned_persistence
        # Crash journal (#7).  ``None`` disables journaling entirely, which is
        # what in-memory test doubles get.
        self._journal = journal
        self._journal_dirty = False
        self._journal_closing = False
        # A journal left by a previous run that did not exit cleanly.  It is
        # never applied implicitly: the user accepts or declines it first.
        self._pending_recovery: Optional[JournalSnapshot] = None
        self._recovery_offered = False
        self._journal_timer = QTimer(self)
        self._journal_timer.setSingleShot(True)
        self._journal_timer.setInterval(JOURNAL_BATCH_MS)
        self._journal_timer.timeout.connect(weak_slot(self.flush_journal))
        self.changed.connect(weak_slot(self._mark_journal_dirty))
        self.profile_name: str = persistence.profile_name
        self._workspaces: dict[uuid.UUID, Workspace] = {}
        self._active_id: Optional[uuid.UUID] = None
        # workspace id -> last runtime tab id focused in that workspace.
        # ``None`` means "switched to this workspace but no tab was recorded
        # yet"; the bridge then falls back to the first OPEN tab.
        self._last_active_tabs: dict[uuid.UUID, Optional[int]] = {}
        self._pinned: list[PinnedTab] = []
        self._auto_suspend_timer: Optional[QTimer] = None
        # Tabs whose group was placed manually via :tab-move-to-group.  A
        # marker is dropped when the tab's URL changes, at which point the
        # auto-group rules may apply again (ADR 0008 § 5 "Override").
        self._auto_group_overrides: set[tuple[uuid.UUID, int]] = set()

    # --- load/save ---

    def load(self) -> None:
        """Load the active workspace and pinned tabs from disk.

        Only the workspace whose id is recorded in the state file is loaded
        eagerly; all others are loaded on demand via :meth:`get_workspace`.
        """
        self._workspaces = {}
        self._active_id = None
        self._last_active_tabs = {}

        loader = getattr(self._persistence, 'load_state_data', None)
        if loader is not None:
            active_id, last_active_tabs = loader()
        else:  # pragma: no cover - compatibility with older test doubles
            active_id = self._persistence.load_state()
            last_active_tabs = {}
        self._last_active_tabs = dict(last_active_tabs)

        if active_id is not None:
            try:
                workspace = self._persistence.load(active_id)
            except CORRUPT_DATA_ERRORS as e:
                log.message.warning(
                    f"Could not load active workspace {active_id}: {e}")
            else:
                self._workspaces[workspace.id] = workspace
                self._active_id = workspace.id

        self._pinned = self._pinned_persistence.load()

        # A surviving journal means the previous run died without a clean
        # shutdown.  Keep it pending until the user accepts or declines.
        self._pending_recovery = None
        self._recovery_offered = False
        if self._journal is not None:
            self._pending_recovery = self._journal.read()

        # Signal consumers that the initial pinned list is now in
        # memory. Late-constructed consumers (e.g. a PINPanel that is
        # built after the manager) should also call list_pinned()
        # once to pick up the initial state.
        self.pinned_changed.emit()
        self._report_resident_size()

    def _report_resident_size(self) -> None:
        """Log the resident tab-entry count against the measured budget.

        Issue #38 asked for a warning when the manager exceeds its memory
        budget.  The budget itself is enforced by
        ``tests/unit/workspaces/test_benchmarks.py``, which measures with
        ``tracemalloc`` -- turning that on in production to raise a warning
        would tax every later allocation at startup, which is a bad trade for
        a diagnostic.  So the runtime check is on the *count*, which is what
        the byte budget was measured at, and it is reported at debug level
        so ``--debug`` shows the real number without nagging a normal user.

        See ADR 0008 § "Performance baselines" for the measured figures.
        """
        resident = sum(len(ws.tabs) for ws in self._workspaces.values())
        if resident > MEMORY_BUDGET_ENTRIES:
            log.misc.warning(
                f"{resident} tab entries resident, "
                f"{resident / MEMORY_BUDGET_ENTRIES:.1f}x the "
                f"{MEMORY_BUDGET_ENTRIES} entries ADR 0008's memory "
                f"budget was measured at; the manager is now above its "
                f"documented budget.")
        log.misc.debug(
            "workspace memory: %d tab entries resident across %d "
            "workspace(s), budget measured at %d entries",
            resident, len(self._workspaces), MEMORY_BUDGET_ENTRIES)

    def save(self) -> None:
        """Persist all loaded workspaces, the active id, and pinned tabs.

        This is the SaveManager cycle's handler, so the crash journal is
        snapshotted here as well.
        """
        for workspace in self._workspaces.values():
            self._persistence.save(workspace)
        self._save_state()
        self._pinned_persistence.save(self._pinned)
        self.flush_journal(force=True)

    def shutdown(self) -> None:
        """Clean exit: persist everything, then remove the crash journal.

        The journal is kept when the save fails (it may hold the only copy of
        recent changes) or when a recovery is still unresolved.
        """
        self._journal_timer.stop()
        try:
            self.save()
        except OSError as e:
            log.workspaces.error(f"Could not save workspaces on exit: {e}")
            return
        self._journal_closing = True
        if self._journal is not None and self._pending_recovery is None:
            self._journal.discard()

    # --- crash journal (#7) ---

    def _mark_journal_dirty(self) -> None:
        """Note a change and open the batch window if none is open."""
        if self._journal is None or self._journal_closing:
            return
        self._journal_dirty = True
        if not self._journal_timer.isActive():
            self._journal_timer.start()

    def _snapshot(self) -> JournalSnapshot:
        """Return the journal snapshot of the current in-memory state."""
        return JournalSnapshot(
            written_at=_now(),
            active_id=self._active_id,
            last_active_tabs=dict(self._last_active_tabs),
            workspaces=list(self._workspaces.values()),
            pinned=list(self._pinned),
        )

    def flush_journal(self, *, force: bool = False) -> bool:
        """Write the crash journal if the state changed (or when *force*).

        Returns True when a write happened.  A failed write stays dirty and is
        retried by the next flush (the SaveManager cycle or the next batch).
        """
        self._journal_timer.stop()
        if self._journal is None or self._journal_closing:
            return False
        if self._pending_recovery is not None:
            # The unresolved crash journal is the only copy of that data.
            return False
        if not (self._journal_dirty or force):
            return False
        try:
            self._journal.write(self._snapshot())
        except OSError as e:
            log.workspaces.warning(f"Could not write crash journal: {e}")
            self._journal_dirty = True
            return False
        self._journal_dirty = False
        return True

    @property
    def pending_recovery(self) -> Optional[JournalSnapshot]:
        """Return the unresolved journal from a previous unclean exit, if any."""
        return self._pending_recovery

    def take_recovery_offer(self) -> Optional[JournalSnapshot]:
        """Return the pending journal the first time only.

        Several windows may be built at startup, but only one should ask the
        user.  Later callers get None until the recovery is resolved.
        """
        if self._pending_recovery is None or self._recovery_offered:
            return None
        self._recovery_offered = True
        return self._pending_recovery

    def apply_recovery(self, snapshot: JournalSnapshot) -> RecoverySummary:
        """Restore the workspaces, tabs and topology held by *snapshot*.

        Every tab that was OPEN at the crash becomes SUSPENDED: its browser
        page is gone, and it reopens on demand (as any suspended entry does).
        Tab ids are re-keyed to fresh negative ids so a recovered entry can
        never be mistaken for a new runtime tab that reuses the same id;
        ``parent_id`` and the last-active map are remapped with them.  The
        whole restore is one ``changed`` emission, so the tree rebuilds once.
        """
        self._pending_recovery = None
        counter = itertools.count(-1, -1)
        for workspace in snapshot.workspaces:
            mapping: dict[int, int] = {}
            for tab in workspace.tabs:
                new_id = next(counter)
                mapping.setdefault(tab.tab_id, new_id)
                tab.tab_id = new_id
            for tab in workspace.tabs:
                if tab.parent_id is not None:
                    tab.parent_id = mapping.get(tab.parent_id)
                if tab.open_status == 'OPEN':
                    tab.open_status = 'SUSPENDED'
            self._workspaces[workspace.id] = workspace
            last = snapshot.last_active_tabs.get(workspace.id)
            self._last_active_tabs[workspace.id] = (
                None if last is None else mapping.get(last))
        if snapshot.active_id in self._workspaces:
            self._active_id = snapshot.active_id
        self._pinned = list(snapshot.pinned)
        summary = RecoverySummary(
            workspaces=len(snapshot.workspaces), tabs=snapshot.tab_count)
        log.workspaces.info(
            f"Recovered {summary.tabs} tabs in {summary.workspaces} "
            "workspaces from the crash journal")
        try:
            self.save()
        except OSError as e:
            log.workspaces.error(f"Could not save recovered workspaces: {e}")
            message.error(f"Recovered workspaces could not be saved: {e}")
        self.pinned_changed.emit()
        self.changed.emit()
        return summary

    def decline_recovery(self) -> None:
        """Start without the journal's state, keeping the journal on disk.

        The workspace YAML files are untouched.  The journal is moved to a
        timestamped ``_journal.declined-*.yml`` so its data is preserved, and a
        fresh journal is written from the state that was loaded.
        """
        self._pending_recovery = None
        if self._journal is not None:
            moved = self._journal.preserve('declined')
            if moved is not None:
                log.workspaces.info(f"Crash journal kept at {moved}")
        self._mark_journal_dirty()

    def _save_state(self) -> None:
        """Persist the active id plus the per-workspace tab map atomically."""
        self._persistence.save_state(
            self._active_id, self._last_active_tabs)

    # --- workspace CRUD ---

    def add_workspace(self, name: str) -> Workspace:
        """Create, store, and return a new workspace.

        The new workspace is stamped with ``last_active`` so a first launch
        followed by a save can restore it on the next start, and with
        ``created_at`` so the portability exchange format can carry the
        user's actual creation time instead of ``null`` (issue #40).
        """
        now = _now()
        workspace = Workspace(
            id=uuid.uuid4(),
            name=name,
            last_active=now,
            created_at=now,
        )
        self._workspaces[workspace.id] = workspace
        if self._active_id is None:
            self._active_id = workspace.id
            self._save_state()
        self.changed.emit()
        return workspace

    def add_workspace_with_id(
            self,
            workspace_id: uuid.UUID,
            name: str,
            *,
            archived: bool = False,
            last_active: Optional[str] = None,
            created_at: Optional[str] = None,
    ) -> Workspace:
        """Create, store, and return a workspace with a caller-supplied id.

        Import is the only caller: it must preserve the ``Workspace.id``
        from the interchange document so an entry's ``group_id`` keeps its
        referential integrity (ADR 0009 § 2). When the document carries a
        real ``created_at`` it is preserved; otherwise we stamp it now so
        newly-imported workspaces still get a meaningful timestamp.
        """
        now = _now()
        workspace = Workspace(
            id=workspace_id,
            name=name,
            last_active=last_active or now,
            archived=archived,
            created_at=created_at or now,
        )
        self._workspaces[workspace.id] = workspace
        if self._active_id is None:
            self._active_id = workspace.id
            self._save_state()
        self.changed.emit()
        return workspace

    def delete_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace from memory and disk."""
        del self._workspaces[workspace_id]
        self._persistence.delete(workspace_id)
        self._last_active_tabs.pop(workspace_id, None)
        if self._active_id == workspace_id:
            self._active_id = None
        self._save_state()
        self.changed.emit()

    def rename_workspace(self, workspace_id: uuid.UUID, new_name: str) -> None:
        """Rename an existing workspace."""
        self._workspaces[workspace_id].name = new_name
        self.changed.emit()

    def list_workspaces(self) -> list[Workspace]:
        """Return all loaded workspaces."""
        return list(self._workspaces.values())

    def get_workspace(self, workspace_id: uuid.UUID) -> Optional[Workspace]:
        """Return a workspace by id, loading it from disk on demand."""
        if workspace_id in self._workspaces:
            return self._workspaces[workspace_id]
        try:
            workspace = self._persistence.load(workspace_id)
        except CORRUPT_DATA_ERRORS:
            return None
        self._workspaces[workspace.id] = workspace
        return workspace

    def record_active_tab(
            self,
            tab_id: Optional[int],
            workspace_id: Optional[uuid.UUID] = None,
    ) -> None:
        """Remember *tab_id* as the last-active tab of *workspace_id*.

        Called by the widget layer (``WorkspaceBridge``) on every tab switch.
        This is deliberately *not* a state mutation on its own: no
        :attr:`changed` is emitted and nothing is written to disk here. The
        map is flushed whenever the active workspace changes or the manager
        is saved, keeping the hot tab-switch path I/O free.

        Args:
            tab_id: Runtime id of the focused tab, or ``None``.
            workspace_id: Workspace owning the tab. Defaults to the active
                workspace. Unknown/unset workspaces are ignored.
        """
        if workspace_id is None:
            workspace_id = self._active_id
        if workspace_id is None:
            return
        if self._last_active_tabs.get(workspace_id) == tab_id:
            return
        self._last_active_tabs[workspace_id] = tab_id
        # Not a ``changed`` (no Saveable mark) but still worth journaling; the
        # batch window keeps a burst of tab switches to one write.
        self._mark_journal_dirty()

    def tab_id_for_workspace(
            self,
            workspace_id: uuid.UUID,
    ) -> Optional[int]:
        """Return the recorded last-active tab id for *workspace_id*.

        Returns ``None`` when no tab was ever recorded (or the workspace is
        unknown). The value is read from the in-memory cache populated by
        :meth:`load` / :meth:`record_active_tab`, so this is a pure query.
        """
        return self._last_active_tabs.get(workspace_id)

    def set_active(self, workspace_id: uuid.UUID) -> None:
        """Switch the active workspace atomically.

        The switch performs only in-memory bookkeeping plus one state-file
        write (shared with :meth:`_save_state`): stamp ``last_active``, set
        the active id, persist, then notify consumers. A dedicated
        :attr:`active_workspace_changed` signal carries
        ``(workspace_id, restored_tab_id)`` so the widget layer can rebuild
        its tab surface and restore focus without the manager importing any
        Qt widget code.

        Switching to the already-active workspace is a cheap no-op: it emits
        no signal and does not touch the disk, which keeps repeated
        ``:workspace-switch`` calls fast.
        """
        if workspace_id == self._active_id:
            log.workspaces.debug(
                "set_active: workspace %s is already active", workspace_id)
            return

        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        workspace.last_active = _now()
        self._active_id = workspace.id
        self._save_state()
        restored_tab_id = self._last_active_tabs.get(workspace.id)
        self.changed.emit()
        self.active_workspace_changed.emit(workspace.id, restored_tab_id)

    def get_active(self) -> Optional[Workspace]:
        """Return the active workspace, or None if there is none."""
        if self._active_id is None:
            return None
        return self._workspaces.get(self._active_id)

    def get_workspace_by_name(self, name: str) -> Optional[Workspace]:
        """Return the workspace with the given name (case-insensitive), if any.

        Loaded workspaces are checked first. If none matches, the on-disk
        workspaces are consulted so that workspaces which are not currently
        loaded (e.g. after a cold start, when only the active workspace is
        loaded eagerly) are still found. A match loaded from disk is cached
        in memory so the mutation APIs (rename/archive/delete) work on it.
        """
        name_lower = name.lower()
        for workspace in self._workspaces.values():
            if workspace.name.lower() == name_lower:
                return workspace
        for workspace in self._persistence.load_all():
            if workspace.name.lower() == name_lower:
                self._workspaces[workspace.id] = workspace
                return workspace
        return None

    def list_all_workspaces(self) -> list[Workspace]:
        """Return every workspace (loaded ones plus on-disk ones).

        Used by the ``:workspace-list`` command so that a cold-started
        qutebrowser (which loads only the active workspace eagerly) still
        lists all workspaces.
        """
        workspaces = self.list_workspaces()
        loaded_ids = {workspace.id for workspace in workspaces}
        for workspace in self._persistence.load_all():
            if workspace.id not in loaded_ids:
                workspaces.append(workspace)
        return workspaces

    def archive_workspace(self, workspace_id: uuid.UUID) -> None:
        """Mark a workspace as archived without deleting it."""
        self._workspaces[workspace_id].archived = True
        self.changed.emit()

    def unarchive_workspace(self, workspace_id: uuid.UUID) -> None:
        """Clear a workspace's archived flag, restoring it to normal use."""
        self._workspaces[workspace_id].archived = False
        self.changed.emit()

    # --- groups ---

    def create_group(self, workspace_id: uuid.UUID, name: str) -> Group:
        """Create and return a new group in a workspace."""
        group = Group(group_id=uuid.uuid4(), name=name)
        self._workspaces[workspace_id].add_group(group)
        self.changed.emit()
        return group

    def _mutate_group_or_tab(
            self,
            workspace_id: uuid.UUID,
            target_id: uuid.UUID | int,
            mutator: Callable[[Any], None],
    ) -> None:
        """Apply *mutator* to the group or tab identified by *target_id*.

        Groups are searched first, then tabs.  :attr:`changed` is emitted
        once if a matching item was found.
        """
        workspace = self._workspaces[workspace_id]
        for group in workspace.groups:
            if group.group_id == target_id:
                mutator(group)
                self.changed.emit()
                return
        for tab in workspace.tabs:
            if tab.tab_id == target_id:
                mutator(tab)
                self.changed.emit()
                return

    def rename_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID,
                     new_name: str) -> None:
        """Rename a group within a workspace."""
        self._mutate_group_or_tab(
            workspace_id, group_id, lambda group: group.rename(new_name))

    def delete_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID) -> None:
        """Delete a group and make its members standalone.

        Empty groups are deleted without confirmation; prompting for
        non-empty groups is the caller's responsibility.
        """
        workspace = self._workspaces[workspace_id]
        workspace.groups = [group for group in workspace.groups
                            if group.group_id != group_id]
        for tab in workspace.tabs:
            if tab.group_id == group_id:
                tab.clear_group()
        self.changed.emit()

    def list_groups(self, workspace_id: uuid.UUID) -> list[Group]:
        """Return all groups in a workspace."""
        return list(self._workspaces[workspace_id].groups)

    # --- auto-group rules (ADR 0008 § 5) ---

    @staticmethod
    def _rule_group(workspace: Workspace, name: str) -> Group:
        """Return the existing group *name* or create an unfolded one.

        Lookup is case-insensitive so a rule naming ``github`` reuses a
        manually created ``GitHub`` instead of creating a duplicate.
        """
        for group in workspace.groups:
            if group.name.lower() == name.lower():
                return group
        group = Group(group_id=uuid.uuid4(), name=name, folded=False)
        workspace.groups.append(group)
        log.workspaces.debug(
            "auto_group_rules: created group %r in workspace %s",
            name, workspace.id)
        return group

    def _apply_auto_group_rule(
            self,
            workspace: Workspace,
            tab: WorkspaceTab,
    ) -> bool:
        """Apply the first matching rule to *tab*; return whether it moved.

        Only ``group_id`` is touched, so an existing ``parent_id`` is
        preserved.  A tab whose URL matches no rule is left exactly as it
        was (the user's manual placement is never stripped).
        """
        group_name = _matching_auto_group_rule(tab.url)
        if group_name is None:
            return False
        group = self._rule_group(workspace, group_name)
        if tab.group_id == group.group_id:
            return False
        tab.assign_group(group.group_id)
        return True

    def apply_auto_group_rules(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            *,
            url_changed: bool = False,
    ) -> bool:
        """Evaluate ``auto_group_rules`` for a single tab.

        Called by the bridge when a tab opens and whenever its URL changes.
        When *url_changed* is True a manual placement made through
        :meth:`move_tab_to_group` is forgotten, so the rules may apply again
        (ADR 0008 § 5 "Override").

        Returns:
            True if the tab was assigned to a (possibly newly created)
            group, False when nothing changed or a manual override is still
            in effect.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return False
        tab = next(
            (entry for entry in workspace.tabs if entry.tab_id == tab_id),
            None,
        )
        if tab is None:
            return False

        key = (workspace_id, tab_id)
        if url_changed:
            self._auto_group_overrides.discard(key)
        if key in self._auto_group_overrides:
            return False

        if not self._apply_auto_group_rule(workspace, tab):
            return False
        self.changed.emit()
        return True

    def reapply_auto_group_rules(self, workspace_id: uuid.UUID) -> int:
        """Re-run ``auto_group_rules`` over every tab in *workspace_id*.

        This is the ``:auto-group-reapply`` backend, useful after editing
        the rule list.  Tabs manually placed with
        :meth:`move_tab_to_group` keep their override, and tabs which match
        no rule keep their current placement.

        Returns:
            The number of tabs whose group membership changed.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return 0

        changed_count = 0
        self.blockSignals(True)
        try:
            for tab in list(workspace.tabs):
                if (workspace_id, tab.tab_id) in self._auto_group_overrides:
                    continue
                if self._apply_auto_group_rule(workspace, tab):
                    changed_count += 1
        finally:
            self.blockSignals(False)
        if changed_count:
            self.changed.emit()
        return changed_count

    # --- group running state (VP-D2) ---

    def group_open_count(self, workspace_id: uuid.UUID,
                         group_id: uuid.UUID) -> int:
        """Return the number of OPEN tabs belonging to *group_id*.

        This is a pure query: no :attr:`changed` is emitted.
        """
        workspace = self._workspaces[workspace_id]
        return sum(
            1 for tab in workspace.tabs
            if tab.group_id == group_id and tab.open_status == 'OPEN'
        )

    def group_total_count(self, workspace_id: uuid.UUID,
                          group_id: uuid.UUID) -> int:
        """Return the total number of tabs belonging to *group_id*.

        This is a pure query: no :attr:`changed` is emitted.
        """
        workspace = self._workspaces[workspace_id]
        return sum(
            1 for tab in workspace.tabs if tab.group_id == group_id
        )

    def estimate_group_memory_bytes(self, workspace_id: uuid.UUID,
                                    group_id: uuid.UUID) -> int:
        """Return a rough byte estimate for the group's OPEN tabs.

        The MVP heuristic assumes :data:`BYTES_PER_TAB` per OPEN tab;
        SUSPENDED tabs are already released. This is a pure query: no
        :attr:`changed` is emitted.
        """
        return (self.group_open_count(workspace_id, group_id)
                * BYTES_PER_TAB)

    def group_state(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> str:
        """Return the derived running state of *group_id*.

        The result is one of:

        * ``'ARCHIVED'`` — the group's archive flag is set (it wins over
          the runtime state of its members);
        * ``'OPEN'`` — at least one member tab is OPEN;
        * ``'SUSPENDED'`` — the group is empty or every member is
          SUSPENDED.

        This is a pure query: no :attr:`changed` is emitted.

        Downstream contract (VP-D3 / VP-D4 / VP-D5)::

            WorkspaceManager.group_state(workspace_id, group_id) -> str
            WorkspaceManager.group_open_count(workspace_id, group_id) -> int
            WorkspaceManager.group_total_count(workspace_id, group_id) -> int

        ``Group.state`` is the local view (``'ARCHIVED' | 'UNKNOWN'``); the
        model layer calls this method to resolve ``'UNKNOWN'`` into
        ``'OPEN'`` / ``'SUSPENDED'``.

        Raises:
            KeyError: When *group_id* does not exist in *workspace_id*.
        """
        group = self._find_group(workspace_id, group_id)
        if group.archived:
            return 'ARCHIVED'
        if self.group_open_count(workspace_id, group_id) > 0:
            return 'OPEN'
        return 'SUSPENDED'

    def get_or_create_inbox(self, workspace_id: uuid.UUID) -> Group:
        """Return the Inbox group for *workspace_id*, creating it if needed.

        Lookup is case-insensitive on the name ``'Inbox'``. The Inbox is a
        regular group; users may rename or delete it freely. A subsequent
        collect after a rename / delete creates a fresh group.
        """
        workspace = self._workspaces[workspace_id]
        for group in workspace.groups:
            if group.name.lower() == 'inbox':
                return group
        group = self.create_group(workspace_id, 'Inbox')
        log.workspaces.debug(
            "get_or_create_inbox: workspace %s created Inbox (1 group)",
            workspace_id)
        return group

    def create_window_snapshot(
            self, workspace_id: uuid.UUID, tab_snapshots: list[dict],
            *, min_tabs: int = 1,
    ) -> Optional[Group]:
        """Store *tab_snapshots* as a sub-group of the Inbox (VP-P4).

        Returns ``None`` when fewer than *min_tabs* snapshots were captured
        or the workspace is unknown.  The whole operation emits
        :attr:`changed` exactly once.
        """
        if len(tab_snapshots) < min_tabs:
            log.workspaces.info(
                "快照跳过:少于 %d 条 (%d)", min_tabs, len(tab_snapshots))
            return None
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return None

        # Suppress the intermediate emits from get_or_create_inbox /
        # create_group / add_tab so consumers see a single change.
        self.blockSignals(True)
        try:
            inbox = self.get_or_create_inbox(workspace_id)
            sub = self.create_group(workspace_id, _snapshot_group_name())
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            sub.name = f"快照退出 — {timestamp}"
            sub.snapshot_parent_id = inbox.group_id  # type: ignore[attr-defined]
            base_id = -int(time.time() * 1000)
            for i, snap in enumerate(tab_snapshots):
                tab = WorkspaceTab(
                    tab_id=base_id - i,
                    url=snap.get('url', ''),
                    title=snap.get('title', ''),
                    description=snap.get('description', ''),
                    source=snap.get('source', ''),
                    favicon=snap.get('favicon'),
                    group_id=sub.group_id,
                    open_status='SUSPENDED',
                    reading_status='FRESH',
                )
                self.add_tab(workspace_id, tab)
        finally:
            self.blockSignals(False)
        self.changed.emit()
        return sub

    # --- group lifecycle (pin / archive / reorder / purge) ---

    def _find_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> Group:
        """Return the group identified by *group_id*.

        Raises ``KeyError`` when no such group exists.
        """
        for group in self._workspaces[workspace_id].groups:
            if group.group_id == group_id:
                return group
        raise KeyError(f"No group with id {group_id}")

    def pin_group(self, workspace_id: uuid.UUID,
                  group_id: uuid.UUID) -> None:
        """Mark a group as pinned (pinned groups sort first)."""
        group = self._find_group(workspace_id, group_id)
        group.pinned = True
        self.changed.emit()

    def unpin_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID) -> None:
        """Clear a group's pinned flag."""
        group = self._find_group(workspace_id, group_id)
        group.pinned = False
        self.changed.emit()

    def archive_group(self, workspace_id: uuid.UUID,
                      group_id: uuid.UUID) -> None:
        """Mark a group as archived without deleting its data."""
        group = self._find_group(workspace_id, group_id)
        group.archived = True
        self.changed.emit()

    def restore_group(self, workspace_id: uuid.UUID,
                      group_id: uuid.UUID) -> None:
        """Clear a group's archived flag."""
        group = self._find_group(workspace_id, group_id)
        group.archived = False
        self.changed.emit()

    def set_group_affinity_enabled(self, workspace_id: uuid.UUID,
                                   group_id: uuid.UUID,
                                   enabled: bool) -> None:
        """Enable or disable window affinity for a single group (VP-A1).

        Disabling affinity makes the group behave as if it had no
        :attr:`~qutebrowser.workspaces.model.Group.window_affinity`; the
        stored affinity is left untouched so re-enabling restores the old
        routing within the same browser session.
        """
        group = self._find_group(workspace_id, group_id)
        group.affinity_enabled = bool(enabled)
        self.changed.emit()

    def list_archived_groups(self, workspace_id: uuid.UUID) -> list[Group]:
        """Return only the archived groups in a workspace."""
        return [group for group in self._workspaces[workspace_id].groups
                if group.archived]

    def move_group(self, workspace_id: uuid.UUID, group_id: uuid.UUID,
                   new_index: int) -> None:
        """Move *group_id* to *new_index* in ``workspace.groups``.

        Reordering is independent of the pin flag: pinning only changes the
        display sort order, not the underlying list order.
        """
        workspace = self._workspaces[workspace_id]
        group = self._find_group(workspace_id, group_id)
        if not 0 <= new_index < len(workspace.groups):
            raise ValueError(
                f"Group index {new_index} out of range "
                f"(0..{len(workspace.groups) - 1}).")
        current_index = workspace.groups.index(group)
        workspace.groups.pop(current_index)
        workspace.groups.insert(new_index, group)
        self.changed.emit()

    # --- tab membership ---

    def add_tab(self, workspace_id: uuid.UUID, tab: WorkspaceTab) -> None:
        """Add a tab entry to a workspace."""
        # A recycled runtime tab id must not inherit an old manual-override
        # marker from the tab that previously used it.
        self._auto_group_overrides.discard((workspace_id, tab.tab_id))
        self._workspaces[workspace_id].tabs.append(tab)
        self.changed.emit()

    def remove_tab(self, workspace_id: uuid.UUID, tab_id: int) -> None:
        """Remove a tab entry from a workspace.

        Any child tab pointing at the removed tab is detached (its
        ``parent_id`` is cleared).
        """
        workspace = self._workspaces[workspace_id]
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.tab_id != tab_id]
        self._auto_group_overrides.discard((workspace_id, tab_id))
        for tab in workspace.tabs:
            if tab.parent_id == tab_id:
                tab.clear_parent()
        self.changed.emit()

    def move_tab_to_group(self, workspace_id: uuid.UUID, tab_id: int,
                          group_id: Optional[uuid.UUID]) -> None:
        """Move a tab into a group (or to standalone when group_id is None).

        A manual move overrides ``auto_group_rules`` for that tab until its
        URL changes again (ADR 0008 § 5 "Override").
        """
        workspace = self._workspaces.get(workspace_id)
        if (workspace is not None
                and any(tab.tab_id == tab_id for tab in workspace.tabs)):
            self._auto_group_overrides.add((workspace_id, tab_id))
        self._mutate_group_or_tab(
            workspace_id, tab_id, lambda tab: tab.assign_group(group_id))

    def set_parent(self, workspace_id: uuid.UUID, tab_id: int,
                   parent_tab_id: Optional[int]) -> None:
        """Set (or clear) a tab entry's parent tab.

        Both *tab_id* and *parent_tab_id* are resolved against the same
        workspace.  Unknown ids are logged and ignored so a stale bridge
        signal can never corrupt the tree.  ``tab_id == parent_tab_id`` (and
        longer ancestor cycles) are rejected for the same reason.

        Only ``parent_id`` is touched: a tab's ``group_id`` is an orthogonal
        relation and is preserved (ADR 0008 § 3/§ "auto_group_rules").
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            log.workspaces.debug(
                "set_parent: unknown workspace %s", workspace_id)
            return

        tab = next((t for t in workspace.tabs if t.tab_id == tab_id), None)
        if tab is None:
            log.workspaces.debug(
                "set_parent: unknown tab %s in workspace %s",
                tab_id, workspace_id)
            return

        if parent_tab_id is None:
            if tab.parent_id is not None:
                tab.clear_parent()
                self.changed.emit()
            return

        if parent_tab_id == tab_id:
            log.workspaces.debug(
                "set_parent: refusing to make tab %s its own parent", tab_id)
            return

        parent = next(
            (t for t in workspace.tabs if t.tab_id == parent_tab_id), None)
        if parent is None:
            log.workspaces.debug(
                "set_parent: unknown parent tab %s in workspace %s",
                parent_tab_id, workspace_id)
            return

        # Walk up from the proposed parent; reaching *tab* would create a
        # cycle (A -> B -> A).
        seen: set[int] = set()
        cursor: Optional[WorkspaceTab] = parent
        while cursor is not None and cursor.tab_id not in seen:
            if cursor.tab_id == tab_id:
                log.workspaces.debug(
                    "set_parent: refusing cyclic parent %s -> %s",
                    tab_id, parent_tab_id)
                return
            seen.add(cursor.tab_id)
            cursor = next(
                (t for t in workspace.tabs
                 if t.tab_id == cursor.parent_id), None)

        if tab.parent_id != parent_tab_id:
            tab.parent_id = parent_tab_id
            self.changed.emit()

    # --- cross-workspace move (ticket #6) ---

    def move_tab_to_workspace(
            self,
            source_id: uuid.UUID,
            tab_id: int,
            target_id: uuid.UUID,
            *,
            branch: bool = False,
            parent_id: Optional[int] = None,
            group_id: Optional[uuid.UUID] = None,
    ) -> int:
        """Move a tab entry (optionally with its branch) to another workspace.

        Args:
            source_id: Workspace currently owning *tab_id*.
            tab_id: Runtime id of the tab entry to move.
            target_id: Workspace to move the entry into.
            branch: When True, every descendant of *tab_id* moves too and
                parent-child links inside the moved set are preserved.  When
                False, only *tab_id* moves; its direct children are
                re-attached to its old parent in the source workspace so
                nothing is left pointing at a removed entry.
            parent_id: Tab id (in the *target* workspace) to become the
                moved root's parent, or ``None`` for a top-level entry.
            group_id: Group (in the *target* workspace) to place the moved
                entries in, or ``None`` for standalone.  Group ids are
                workspace-scoped, so the group membership of moved entries is
                always replaced by this value.

        Source and target are written to disk, target first.  A crash between
        the two writes leaves the entry in *both* files (never in neither).

        Returns:
            The moved root's tab id in the target workspace (it may differ
            from *tab_id* when the id was already taken there).

        Raises:
            WorkspaceMoveError: the move is refused.  ``str()`` of the
                exception is a user-facing message; nothing is changed.
        """
        source = self.get_workspace(source_id)
        target = self.get_workspace(target_id)
        if source is None or target is None:
            raise WorkspaceMoveError("Unknown workspace.")
        root = self.find_tab_by_id(source_id, tab_id)
        if root is None:
            raise WorkspaceMoveError(
                f"No tab with id {tab_id} in workspace '{source.name}'.")

        descendants = _descendant_ids(source, tab_id)
        self._check_move(
            source, target, tab_id, descendants,
            parent_id=parent_id, group_id=group_id)

        moving_ids = {tab_id} | (descendants if branch else set())
        moved = [tab for tab in source.tabs if tab.tab_id in moving_ids]

        occupied = {tab.tab_id for tab in target.tabs}
        mapping: dict[int, int] = {}
        for tab in moved:
            new_id = _free_id(occupied, tab.tab_id)
            occupied.add(new_id)
            mapping[tab.tab_id] = new_id

        # Detach from the source.  Direct children left behind by a single
        # move are promoted to the moved tab's old parent.
        old_parent = root.parent_id
        source.tabs = [tab for tab in source.tabs
                       if tab.tab_id not in moving_ids]
        for child in source.tabs:
            if child.parent_id == tab_id:
                child.parent_id = old_parent

        # Re-key the moved entries for the target.  Parent links are read
        # from the old ids (``mapping`` is keyed by them) before re-keying.
        for tab in moved:
            old_id = tab.tab_id
            if old_id == tab_id:
                new_parent = parent_id
            elif tab.parent_id in mapping:
                # A descendant's parent is always moved along with it.
                new_parent = mapping[tab.parent_id]
            else:
                new_parent = None
            tab.tab_id = mapping[old_id]
            tab.parent_id = new_parent
            tab.assign_group(group_id)
            self._auto_group_overrides.discard((source_id, old_id))
            if group_id is not None:
                self._auto_group_overrides.add((target_id, tab.tab_id))
            else:
                self._auto_group_overrides.discard((target_id, tab.tab_id))
        target.tabs.extend(moved)

        # Last-active bookkeeping: the source must not keep pointing at a tab
        # it no longer owns; the target inherits the moved tab as its last
        # active one.
        src_last = self._last_active_tabs.get(source_id)
        if src_last is not None and src_last in mapping:
            still_there = old_parent is not None and any(
                tab.tab_id == old_parent for tab in source.tabs)
            self._last_active_tabs[source_id] = (
                old_parent if still_there else None)
            self._last_active_tabs[target_id] = mapping[src_last]

        # Target first: a crash in between duplicates the entry, never loses it.
        self._persistence.save(target)
        self._persistence.save(source)
        self._save_state()
        log.workspaces.debug(
            "move_tab_to_workspace: %s -> %s, tab %s (branch=%s, moved %d)",
            source_id, target_id, tab_id, branch, len(moved))
        self.changed.emit()
        return mapping[tab_id]

    @staticmethod
    def _check_move(
            source: Workspace,
            target: Workspace,
            tab_id: int,
            descendants: set[int],
            *,
            parent_id: Optional[int],
            group_id: Optional[uuid.UUID],
    ) -> None:
        """Raise :class:`WorkspaceMoveError` when a move is not allowed."""
        if source.id == target.id:
            if parent_id is not None and (
                    parent_id == tab_id or parent_id in descendants):
                raise WorkspaceMoveError(
                    f"Cannot move tab {tab_id} under its own branch.")
            raise WorkspaceMoveError(
                f"Tab {tab_id} is already in workspace '{target.name}'.")
        if parent_id is not None and not any(
                tab.tab_id == parent_id for tab in target.tabs):
            raise WorkspaceMoveError(
                f"Parent tab {parent_id} is not in workspace "
                f"'{target.name}'.")
        if group_id is None:
            return
        group = next((g for g in target.groups
                      if g.group_id == group_id), None)
        if group is None:
            raise WorkspaceMoveError(
                f"Group {group_id} is not in workspace '{target.name}'.")
        if group.archived:
            raise WorkspaceMoveError(f"Group '{group.name}' is archived.")

    # --- batched operations (VP-F4) ---

    def move_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            group_id: Optional[uuid.UUID],
    ) -> int:
        """Move every known tab in *tab_ids* into *group_id*.

        ``group_id`` of ``None`` detaches the tabs (makes them standalone).
        Unknown tab ids are skipped silently.  :attr:`changed` is emitted
        once if anything moved.

        Returns:
            The number of tabs that were moved.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return 0
        id_set = set(tab_ids)
        moved = 0
        for tab in workspace.tabs:
            if tab.tab_id in id_set:
                tab.assign_group(group_id)
                self._auto_group_overrides.add(
                    (workspace_id, tab.tab_id))
                moved += 1
        if moved:
            log.workspaces.debug(
                "move_tabs: workspace %s moved %d tab(s)", workspace_id, moved)
            self.changed.emit()
        return moved

    def delete_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            confirm: bool = True,
    ) -> int:
        """Remove every known tab in *tab_ids* from *workspace_id*.

        Children of a removed tab are detached (their ``parent_id`` is
        cleared).  Unknown tab ids are skipped silently.  :attr:`changed`
        is emitted once if anything was removed.

        Args:
            workspace_id: Workspace owning the tabs.
            tab_ids: Runtime ids of the tabs to remove.
            confirm: When True (the default) the call is a no-op returning
                ``0`` so the command layer can ask the user first; pass
                ``False`` (or use ``-!``) to delete immediately.

        Returns:
            The number of tabs that were removed.
        """
        if confirm:
            # The command layer confirms via ``message.confirm_async`` and
            # calls back with ``confirm=False`` from the yes_action.
            return 0
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return 0
        id_set = set(tab_ids)
        removed = [tab for tab in workspace.tabs if tab.tab_id in id_set]
        if not removed:
            return 0
        workspace.tabs = [tab for tab in workspace.tabs
                          if tab.tab_id not in id_set]
        for tab_id in id_set:
            self._auto_group_overrides.discard((workspace_id, tab_id))
        for tab in workspace.tabs:
            if tab.parent_id in id_set:
                tab.clear_parent()
        log.workspaces.debug(
            "delete_tabs: workspace %s removed %d tab(s)",
            workspace_id, len(removed))
        self.changed.emit()
        return len(removed)

    def close_group(self, workspace_id: uuid.UUID,
                    group_id: uuid.UUID, *,
                    win_id: int = 0) -> tuple[int, int]:
        """Close every OPEN tab in *group_id*; return ``(closed, skipped)``.

        Each close goes through the browser's ``tabCloseRequested`` signal
        so the bridge's existing ``_on_tree_close_requested`` path flips the
        entry to SUSPENDED and emits :attr:`changed`. Tabs that are already
        SUSPENDED or pinned are skipped silently. Tabs that have no live
        match in the *win_id* window's ``tab-registry``, or whose
        ``tabCloseRequested`` handler raises, count toward *skipped* and do
        not abort the batch.

        Archived groups never participate in the running state, so closing
        their tabs is wrong: an archived *group_id* is a no-op returning
        ``(0, 0)``.

        The group itself (and its tab entries) is preserved so it can be
        reopened later; it is never deleted.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return (0, 0)
        # Verify the group exists before touching any tab.
        group = self._find_group(workspace_id, group_id)
        if group.archived:
            return (0, 0)

        registry, browser = self._close_resources(win_id)
        closed = skipped = 0
        for tab in workspace.tabs:
            if tab.group_id != group_id or tab.open_status != 'OPEN':
                continue
            live = registry.get(tab.tab_id)
            if live is None or browser is None:
                skipped += 1
                continue
            if getattr(getattr(live, 'data', None), 'pinned', False):
                # Pinned tabs would prompt the modal
                # ``tab_close_prompt_if_pinned`` dialog; skip them.
                skipped += 1
                continue
            if self._request_close(browser, live, tab.tab_id):
                closed += 1
            else:
                skipped += 1
        # VP-A1: remember the window this group was closed from so Reopen
        # Group can route its tabs back. Never overwrite an existing
        # affinity, and honour both the per-group and global switches.
        if (group.window_affinity is None
                and group.affinity_enabled
                and not group.archived
                and _window_affinity_globally_enabled()):
            group.window_affinity = win_id
        if closed:
            self.changed.emit()
        log.workspaces.debug(
            f"close_group: group={group_id} closed={closed} skipped={skipped}")
        return (closed, skipped)

    @staticmethod
    def _close_resources(win_id: int) -> tuple[Mapping[int, Any], Any]:
        """Return ``(tab-registry, tabbed-browser)`` for *win_id*.

        Falls back to an empty registry and ``None`` browser when the window
        has no registered resources.
        """
        try:
            registry = objreg.get(
                'tab-registry', scope='window', window=win_id) or {}
            browser = objreg.get(
                'tabbed-browser', scope='window', window=win_id)
        except (KeyError, objreg.RegistryUnavailableError):
            return {}, None
        return registry, browser

    @staticmethod
    def _request_close(browser: Any, live: Any, tab_id: int) -> bool:
        """Emit ``tabCloseRequested`` for *live*; return True on success."""
        try:
            widget_idx = browser.widget.indexOf(live)
            if widget_idx < 0:
                return False
            browser.widget.tabCloseRequested.emit(widget_idx)
            return True
        except Exception as e:  # noqa: BLE001 — closing is best-effort
            log.workspaces.debug(
                f"close_group: tabCloseRequested raised for {tab_id}: {e}")
            return False

    def _all_window_resources(self) -> list[tuple[int, Mapping[int, Any], Any]]:
        """Return ``(win_id, registry, browser)`` for every open window.

        Used by :meth:`reopen_group` to support cross-window URL reuse. Any
        window whose ``tab-registry`` / ``tabbed-browser`` is unavailable is
        skipped. When no window is registered at all, a single safe default
        ``(0, {}, None)`` is returned so callers can rely on ``[0]`` existing.
        """
        win_ids = getattr(objreg.window_registry, 'window_ids', None)
        if win_ids is None:
            try:
                win_ids = list(objreg.window_registry)
            except TypeError:  # pragma: no cover - defensive
                win_ids = []
        win_ids = list(win_ids) or [0]

        out: list[tuple[int, Mapping[int, Any], Any]] = []
        for win_id in win_ids:
            try:
                registry = objreg.get(
                    'tab-registry', scope='window', window=win_id)
                browser = objreg.get(
                    'tabbed-browser', scope='window', window=win_id)
            except (KeyError, objreg.RegistryUnavailableError):
                continue
            if registry is None or browser is None:
                continue
            out.append((win_id, registry, browser))
        return out or [(0, {}, None)]

    @staticmethod
    def _window_alive(win_id: int) -> bool:
        """Return whether the window *win_id* is still registered.

        Probes the per-window ``tabbed-browser`` object. Registry errors are
        treated as "dead" so Reopen Group falls back to the current window.
        """
        try:
            browser = objreg.get(
                'tabbed-browser', scope='window', window=win_id, default=None)
        except (KeyError, TypeError, objreg.RegistryUnavailableError):
            return False
        return browser is not None

    def _find_live_tab_in_windows(
            self,
            windows: Iterable[tuple[int, Mapping[int, Any], Any]],
            urlstr: str,
    ) -> Optional[tuple[Any, Any, int]]:
        """Return ``(browser, live_tab, win_id)`` for the first URL match.

        This method takes a URL **string**, not a `QUrl`. For `QUrl` input,
        read `.toString()` first.
        """
        for win_id, registry, browser in windows:
            live = self._live_tab_for_url(registry, urlstr)
            if live is not None:
                return browser, live, win_id
        return None

    def _focus_live_tab(
            self,
            workspace_id: uuid.UUID,
            tab: WorkspaceTab,
            browser: Any,
            live: Any,
            win_id: int,
    ) -> bool:
        """Focus *live*, raise its window and flip *tab* to OPEN.

        Returns True on success.  The window identified by *win_id* (the one
        owning the reused tab) is raised / activated so a cross-window reuse
        brings the hit into view.
        """
        try:
            browser.widget.setCurrentWidget(live)
            self.set_open_status(workspace_id, tab.tab_id, 'OPEN')
        except Exception as e:  # noqa: BLE001 - reopening is best-effort
            log.workspaces.debug(
                f"reopen_group: setCurrentWidget failed for {tab.tab_id}: {e}")
            return False
        try:
            window = objreg.get(
                'main-window', scope='window', window=win_id)
            if window is not None:
                window.raise_()
                window.activateWindow()
        except (KeyError, objreg.RegistryUnavailableError):
            pass
        return True

    @staticmethod
    def _open_url_in_browser(browser: Any, urlstr: str) -> bool:
        """Open *urlstr* via ``tabopen``; return True on success.

        This method takes a URL **string**, not a `QUrl`. For `QUrl` input,
        read `.toString()` first.
        """
        try:
            qurl = QUrl(urlstr)
            qtutils.ensure_valid(qurl)
            browser.tabopen(qurl, background=False)
            return True
        except Exception as e:  # noqa: BLE001 - reopening is best-effort
            log.workspaces.debug(
                f"reopen_group: tabopen failed for url={urlstr!r}: {e}")
            return False

    @staticmethod
    def _notify_reopen_progress(
            on_progress: Optional[Callable[[int, int, int], None]],
            done: int,
            total: int,
            skipped: int,
    ) -> None:
        """Call *on_progress*, swallowing any exception it raises."""
        if on_progress is None:
            return
        try:
            on_progress(done, total, skipped)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass

    def reopen_group(
            self,
            workspace_id: uuid.UUID,
            group_id: uuid.UUID,
            *,
            win_id: int = 0,
            on_progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> tuple[int, int, int]:
        """Reopen every SUSPENDED tab in *group_id*.

        For each SUSPENDED entry:

        * A live tab in *any* window whose URL matches is focused via
          ``setCurrentWidget`` and the entry is flipped to OPEN through
          :meth:`set_open_status`; the owning window is raised. This is
          counted as *reused*.
        * Otherwise the URL is opened via ``tabopen`` in the *win_id* window;
          the bridge's ``_register_tab`` (VP-D1) auto-rebinds the entry and
          flips it to OPEN. This is counted as *opened*.
        * Empty URLs, missing browsers and any exception raised by
          ``setCurrentWidget`` / ``tabopen`` are counted as *skipped* and do
          not abort the batch.

        :attr:`changed` is emitted when at least one tab was opened or reused.

        *on_progress*, when given, is called after every processed
        SUSPENDED entry with ``(done, total, skipped)``, where *done* is the
        number of tabs reused or opened so far and *total* is the number of
        SUSPENDED members. Exceptions raised by the callback are swallowed.

        Returns:
            ``(opened, reused, skipped)``.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return (0, 0, 0)
        # Verify the group exists before touching any tab.
        group = self._find_group(workspace_id, group_id)

        # VP-A1: route the restore to the window this group was closed from
        # when that window is still alive. A dead affinity is replaced with
        # the current window; a disabled affinity behaves like ``None``.
        target_win_id = win_id
        if (group.affinity_enabled
                and _window_affinity_globally_enabled()
                and group.window_affinity is not None):
            if self._window_alive(group.window_affinity):
                target_win_id = group.window_affinity
            else:
                group.window_affinity = win_id
                target_win_id = win_id

        total = sum(1 for tab in workspace.tabs
                    if tab.group_id == group_id
                    and tab.open_status == 'SUSPENDED')

        windows = self._all_window_resources()
        by_win = {w_win_id: w_browser
                  for w_win_id, _registry, w_browser in windows}
        cur_browser = by_win.get(target_win_id)

        opened = reused = skipped = 0
        for tab in list(workspace.tabs):
            if tab.group_id != group_id or tab.open_status != 'SUSPENDED':
                continue
            if not tab.url:
                skipped += 1
            else:
                hit = self._find_live_tab_in_windows(windows, tab.url)
                if hit is not None:
                    browser, live, hit_win_id = hit
                    if self._focus_live_tab(
                            workspace_id, tab, browser, live, hit_win_id):
                        reused += 1
                    else:
                        skipped += 1
                elif cur_browser is None:
                    skipped += 1
                elif self._open_url_in_browser(cur_browser, tab.url):
                    # The bridge auto-flips the entry to OPEN via _register_tab.
                    opened += 1
                else:
                    skipped += 1
            self._notify_reopen_progress(
                on_progress, opened + reused, total, skipped)

        if opened + reused:
            self.changed.emit()
        log.workspaces.debug(
            f"reopen_group: group={group_id} opened={opened} reused={reused} "
            f"skipped={skipped}")
        return (opened, reused, skipped)

    @staticmethod
    def _window_resources(win_id: int) -> tuple[Any, Mapping[int, Any]]:
        """Return ``(tabbed_browser, tab_registry)`` for *win_id*."""
        browser = objreg.get('tabbed-browser', scope='window', window=win_id)
        registry = objreg.get('tab-registry', scope='window', window=win_id)
        return browser, registry

    def _open_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            newwindow: bool,
            win_id: int,
    ) -> tuple[int, int]:
        """Open the URLs of *tab_ids*, reusing live tabs when possible.

        A live tab whose ``url().toString()`` matches a requested URL is
        focused via ``setCurrentWidget`` (counted as *reused*); otherwise
        the URL is opened through ``tabopen`` (counted as *opened*).

        Returns:
            ``(opened, reused)``.
        """
        workspace = self._workspaces.get(workspace_id)
        browser, registry = self._window_resources(win_id)

        urls: list[str] = []
        if workspace is not None:
            by_id = {tab.tab_id: tab for tab in workspace.tabs}
            for tab_id in tab_ids:
                tab = by_id.get(tab_id)
                if tab is not None and tab.url:
                    urls.append(tab.url)

        opened = 0
        reused = 0
        to_open: list[str] = []
        for url in urls:
            match = self._live_tab_for_url(registry, url)
            if match is not None:
                browser.widget.setCurrentWidget(match)
                reused += 1
            else:
                to_open.append(url)

        if not to_open:
            return 0, reused
        if newwindow:
            # The real ``TabbedBrowser.tabopen`` has no ``newwindow`` kwarg;
            # the new-window path constructs a *single* fresh ``MainWindow``
            # holding every non-reused URL, mirroring the
            # ``tabs.tabs_are_windows`` branch in
            # :meth:`TabbedBrowser.tabopen`.  The first ``tabopen`` creates
            # the window's initial tab; the rest land in the same window.
            from qutebrowser.mainwindow import mainwindow as _mw
            window = _mw.MainWindow(private=browser.is_private)
            for url in to_open:
                qurl = QUrl(url)
                qtutils.ensure_valid(qurl)
                window.tabbed_browser.tabopen(qurl)
            window.show()
        else:
            for url in to_open:
                qurl = QUrl(url)
                qtutils.ensure_valid(qurl)
                browser.tabopen(qurl)
        opened = len(to_open)
        return opened, reused

    @staticmethod
    def _live_tab_for_url(registry: Mapping[int, Any], url: str) -> Any:
        """Return the live tab in *registry* whose URL matches *url*."""
        for live in registry.values():
            try:
                if live.url().toString() == url:
                    return live
            except (AttributeError, NotImplementedError):
                continue
        return None

    def open_tabs_in_new_window(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            win_id: int = 0,
    ) -> tuple[int, int]:
        """Open *tab_ids* in a new window, reusing matching live tabs.

        Returns:
            ``(opened, reused)``.
        """
        result = self._open_tabs(
            workspace_id, tab_ids, newwindow=True, win_id=win_id)
        log.workspaces.debug(
            "open_tabs_in_new_window: workspace %s opened %d tab(s)",
            workspace_id, result[0])
        return result

    def open_tabs_as_new_tabs(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            *,
            win_id: int = 0,
    ) -> tuple[int, int]:
        """Open *tab_ids* as new tabs, reusing matching live tabs.

        Returns:
            ``(opened, reused)``.
        """
        result = self._open_tabs(
            workspace_id, tab_ids, newwindow=False, win_id=win_id)
        log.workspaces.debug(
            "open_tabs_as_new_tabs: workspace %s opened %d tab(s)",
            workspace_id, result[0])
        return result

    # --- metadata capture / collection (VP-F1) ---

    def find_tab_by_id(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
    ) -> Optional[WorkspaceTab]:
        """Return the workspace tab with *tab_id*, if present."""
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return None
        for tab in workspace.tabs:
            if tab.tab_id == tab_id:
                return tab
        return None

    def find_tab_by_url(
            self,
            workspace_id: uuid.UUID,
            urlstr: str,
    ) -> Optional[WorkspaceTab]:
        """Return the first tab in *workspace_id* with matching *urlstr*.

        This method takes a URL **string**, not a `QUrl`. For `QUrl` input,
        read `.toString()` first.

        Pure query: unlike :meth:`find_or_create_tab_by_url` it never
        creates an entry, so it is safe to call on tab switches.  An empty
        *urlstr* never matches (it cannot identify an entry).
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None or not urlstr:
            return None
        for tab in workspace.tabs:
            if tab.url == urlstr:
                return tab
        return None

    def set_open_status(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            status: str,
    ) -> int:
        """Set the OPEN/SUSPENDED state of a single tab entry.

        Valid *status* values are ``'OPEN'`` and ``'SUSPENDED'``.  A
        suspended entry is retained in the workspace (so it can be restored)
        instead of being removed.

        Args:
            workspace_id: Workspace owning the tab.
            tab_id: Runtime id of the tab entry to update.
            status: Either ``'OPEN'`` or ``'SUSPENDED'``.

        Returns:
            1 if the entry was found and updated, 0 otherwise.
        """
        if status not in ('OPEN', 'SUSPENDED'):
            raise ValueError(
                f"Invalid open status {status!r}; expected 'OPEN' or "
                "'SUSPENDED'.")
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return 0
        if tab.open_status != status:
            tab.open_status = status
            log.workspaces.debug(
                "set_open_status: workspace %s tab %s -> %s (1)",
                workspace_id, tab_id, status)
            self.changed.emit()
        return 1

    def open_tabs(self, workspace_id: uuid.UUID) -> list[WorkspaceTab]:
        """Return the tab entries in *workspace_id* which are OPEN."""
        workspace = self._workspaces[workspace_id]
        return [tab for tab in workspace.tabs if tab.open_status == 'OPEN']

    def suspended_tabs(self, workspace_id: uuid.UUID) -> list[WorkspaceTab]:
        """Return the tab entries in *workspace_id* which are SUSPENDED."""
        workspace = self._workspaces[workspace_id]
        return [tab for tab in workspace.tabs
                if tab.open_status == 'SUSPENDED']

    def find_or_create_tab_by_url(
            self,
            workspace_id: uuid.UUID,
            url: str,
            *,
            tab_id: Optional[int] = None,
    ) -> WorkspaceTab:
        """Return the tab entry matching *url*, creating one when missing.

        A non-empty URL identifies the entry: re-collecting the same URL
        must not create a duplicate. Empty URLs always create a fresh entry
        because they cannot be matched meaningfully.

        Args:
            workspace_id: Workspace to search / add to.
            url: The URL to match.
            tab_id: Tab id to use for a newly created entry. Defaults to
                ``-1`` (a placeholder for callers which only need the entry).
        """
        workspace = self._workspaces[workspace_id]
        if url:
            for tab in workspace.tabs:
                if tab.url == url:
                    return tab

        tab = WorkspaceTab(
            tab_id=tab_id if tab_id is not None else -1,
            url=url,
            source=_host(url),
            open_status='OPEN',
        )
        self.add_tab(workspace_id, tab)
        return tab

    def _rebind_tab_id(
            self,
            workspace_id: uuid.UUID,
            tab: WorkspaceTab,
            new_tab_id: int,
    ) -> bool:
        """Rebind *tab* to *new_tab_id*, clearing colliding children links.

        Returns True when the id actually changed.  A collision with another
        entry's id is ignored (the entry keeps its current id) so the caller
        never ends up with two entries sharing one runtime id.
        """
        if new_tab_id == tab.tab_id:
            return False
        workspace = self._workspaces[workspace_id]
        collision = any(
            other.tab_id == new_tab_id
            for other in workspace.tabs if other is not tab
        )
        if collision:
            return False
        old_id = tab.tab_id
        tab.tab_id = new_tab_id
        override_key = (workspace_id, old_id)
        if override_key in self._auto_group_overrides:
            self._auto_group_overrides.discard(override_key)
            self._auto_group_overrides.add((workspace_id, new_tab_id))
        for child in workspace.tabs:
            if child.parent_id == old_id:
                child.parent_id = new_tab_id
        return True

    def update_tab_metadata(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            *,
            url: Optional[str] = None,
            title: Optional[str] = None,
            description: Optional[str] = None,
            source: Optional[str] = None,
            favicon: Optional[str] = None,
            fetched: bool = False,
            error: Optional[str] = None,
            new_tab_id: Optional[int] = None,
    ) -> Optional[WorkspaceTab]:
        """Update a tab entry's metadata, respecting the user-edited guard.

        ``None`` means "leave unchanged". Fields with a truthy
        :attr:`WorkspaceTab.user_edited` flag are never overwritten (except
        ``url``, which is the entry's identity rather than editable content).

        Args:
            workspace_id: Workspace owning the tab.
            tab_id: Runtime id of the tab entry to update.
            url: New page URL; when given, ``source`` is re-derived unless
                explicitly supplied.
            title: New page title.
            description: New page description.
            source: New site host.
            favicon: New base64-encoded PNG favicon.
            fetched: Whether this call represents a successful fetch; when
                True, ``fetch_attempts`` is incremented, ``last_fetched`` is
                stamped and ``last_error`` is cleared.
            error: Error string to record on failure.
            new_tab_id: When given, rebind the entry to this runtime tab id
                (used by VP-D1's URL-match reuse so the entry tracks the live
                tab). A colliding id is ignored.

        Returns:
            The updated tab, or None when the workspace/tab is unknown.
        """
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return None

        changed = False
        if new_tab_id is not None:
            changed = self._rebind_tab_id(
                workspace_id, tab, new_tab_id) or changed
        if url is not None and url != tab.url:
            tab.url = url
            changed = True
            if source is None:
                source = _host(url)

        updates = (
            ('title', title),
            ('description', description),
            ('source', source),
            ('favicon', favicon),
        )
        for field, value in updates:
            if value is None:
                continue
            if tab.user_edited.get(field, False):
                continue
            if getattr(tab, field) != value:
                setattr(tab, field, value)
                changed = True

        if fetched:
            tab.fetch_attempts += 1
            tab.last_fetched = _now_seconds()
            tab.last_error = None
            changed = True
        if error is not None:
            tab.last_error = error
            changed = True

        if changed:
            self.changed.emit()
        return tab

    def set_user_edited(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            field: str,
            value: bool = True,
    ) -> None:
        """Mark *field* as user-edited so auto-fetch won't overwrite it."""
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return
        tab.user_edited[field] = value
        self.changed.emit()

    def collect_tab(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            group_id: Optional[uuid.UUID] = None,
            *,
            metadata: Optional[TabMetadata] = None,
    ) -> WorkspaceTab:
        """Collect a tab, upserting by URL and backfilling metadata.

        Re-collecting an already-known URL reuses the existing entry instead
        of appending a duplicate. Fields the user edited manually are never
        overwritten.
        """
        meta = metadata or TabMetadata()
        tab = self.find_or_create_tab_by_url(
            workspace_id, meta.url, tab_id=tab_id)
        # Collecting associates the entry with a live tab.
        tab.open_status = 'OPEN'

        if meta.url:
            tab.url = meta.url
        if meta.title and not tab.user_edited.get('title', False):
            tab.title = meta.title
        if meta.description and not tab.user_edited.get(
                'description', False):
            tab.description = meta.description
        if meta.source and not tab.user_edited.get('source', False):
            tab.source = meta.source
        if meta.favicon and not tab.user_edited.get('favicon', False):
            tab.favicon = meta.favicon

        if group_id is not None:
            tab.assign_group(group_id)

        tab.fetch_attempts += 1
        tab.last_fetched = _now_seconds()
        tab.last_error = None
        self.changed.emit()
        return tab

    def collect_all_in_window(
            self,
            workspace_id: uuid.UUID,
            group_id: Optional[uuid.UUID],
            tab_ids: Iterable[int],
            *,
            tab_metadata: Optional[Mapping[int, TabMetadata]] = None,
    ) -> tuple[list[int], list[int]]:
        """Collect every collectable tab in *tab_ids* into *group_id*.

        A tab is collectable when its URL uses ``http``/``https`` and has a
        non-empty host. Internal pages (``qute://*``, ``about:blank``,
        ``chrome://*``, ``view-source:*``, ...) are skipped silently.

        Returns:
            ``(collected, skipped)`` lists of tab ids.
        """
        metadata_by_id = tab_metadata or {}
        collected: list[int] = []
        skipped: list[int] = []
        for tab_id in tab_ids:
            meta = metadata_by_id.get(tab_id, TabMetadata())
            parsed = QUrl(meta.url)
            scheme = parsed.scheme().lower()
            if scheme not in ('http', 'https') or not parsed.host():
                skipped.append(tab_id)
                continue
            self.collect_tab(
                workspace_id, tab_id, group_id, metadata=meta)
            collected.append(tab_id)
        return collected, skipped

    # --- reading status / auto-cleanup (VP-F3) ---

    def _record_done_at(
            self,
            workspace_id: uuid.UUID,
            tab_id: int,
            status: str,
    ) -> None:
        """Stamp ``done_at`` when *status* is ``DONE``, else clear it."""
        tab = self.find_tab_by_id(workspace_id, tab_id)
        if tab is None:
            return
        tab.done_at = _now_seconds() if status == 'DONE' else None

    def set_reading_status(
            self,
            workspace_id: uuid.UUID,
            tab_ids: Iterable[int],
            status: str,
    ) -> int:
        """Set the reading status of the given tabs in one batch.

        Valid *status* values are ``'FRESH'``, ``'ACTIVE'`` and ``'DONE'``.
        :attr:`changed` is emitted at most once, and only when at least one
        tab was matched (so a no-op batch stays silent).

        Args:
            workspace_id: Workspace owning the tabs.
            tab_ids: Runtime ids of the tabs to update. Unknown ids are
                ignored.
            status: One of the :data:`READING_STATUSES`.

        Returns:
            The number of tabs that were matched (and assigned *status*).
        """
        # Normalise lowercase callers (e.g. ``'done'``) before validation so
        # both the :vp-status-set / :vp-batch-status commands and direct
        # callers can use the same case-insensitive API surface.
        status = status.upper()
        if status not in READING_STATUSES:
            raise ValueError(
                f"Invalid reading status {status!r}; expected one of "
                f"{', '.join(READING_STATUSES)}.")
        wanted = set(tab_ids)
        workspace = self._workspaces[workspace_id]
        count = 0
        for tab in workspace.tabs:
            if tab.tab_id in wanted:
                tab.reading_status = status
                self._record_done_at(workspace_id, tab.tab_id, status)
                count += 1
        if count > 0:
            log.workspaces.debug(
                "set_reading_status: workspace %s -> %s (%d)",
                workspace_id, status, count)
            self.changed.emit()
        return count

    def auto_suspend_due_tabs(
            self,
            workspace_id: uuid.UUID,
            days: Optional[int] = None,
    ) -> int:
        """Suspend ``OPEN`` ``DONE`` tab entries older than *days*.

        A tab is suspended only when *all* of the following hold:

        * its ``open_status`` is ``'OPEN'``;
        * its ``reading_status`` is ``'DONE'``;
        * it has a ``done_at`` timestamp older than the cutoff;
        * its group (if any) is not archived.

        Only the workspace entry's ``open_status`` is flipped; live browser
        tabs are never closed here. :attr:`changed` is emitted once when at
        least one entry was suspended.

        Args:
            workspace_id: Workspace whose tabs are scanned.
            days: Age in days; defaults to
                ``content.vertical_panel.auto_suspend_done_days``.

        Returns:
            The number of tab entries that were suspended.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            return 0
        if days is None:
            days = config.val.content.vertical_panel.auto_suspend_done_days
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days))
        archived_groups = {group.group_id for group in workspace.groups
                           if group.archived}

        due: list[int] = []
        for tab in workspace.tabs:
            if tab.open_status != 'OPEN' or tab.reading_status != 'DONE':
                continue
            if tab.done_at is None:
                continue
            if tab.group_id is not None and tab.group_id in archived_groups:
                continue
            parsed = _parse_timestamp(tab.done_at)
            if parsed is not None and parsed < cutoff:
                due.append(tab.tab_id)

        if not due:
            return 0

        # ``set_open_status`` emits ``changed`` per mutation; suppress those
        # and emit a single signal for the whole batch.
        was_blocked = self.blockSignals(True)
        try:
            for tab_id in due:
                self.set_open_status(workspace_id, tab_id, 'SUSPENDED')
        finally:
            self.blockSignals(was_blocked)
        self.changed.emit()
        log.workspaces.debug(
            "auto_suspend_due_tabs: workspace %s suspended %d tab(s)",
            workspace_id, len(due))
        return len(due)

    # --- smart suggestions (VP-A2) ---

    def suggest_stale_groups(
            self,
            workspace_id: uuid.UUID,
            *,
            min_open_tabs: Optional[int] = None,
            idle_days: Optional[int] = None,
    ) -> list[Group]:
        """Return Groups that look stale and worth suggesting to the user.

        A Group is suggested when it is not archived, has at least
        *min_open_tabs* OPEN tab entries, and its latest member activity is
        at least *idle_days* old. Member activity uses the same semantics
        as VP-P3: ``done_at`` when present, else ``created_at``; when no
        member has a usable timestamp, the Group's ``created_at`` is used.

        This is a pure query: no ``changed`` is emitted. The caller is
        responsible for de-duping against the ignore list and rendering
        the UI.

        Args:
            workspace_id: Workspace whose Groups are inspected.
            min_open_tabs: Minimum OPEN-tab count; defaults to
                ``content.vertical_panel.suggestion_min_open_tabs``.
            idle_days: Minimum untouched age in days; defaults to
                ``content.vertical_panel.suggestion_idle_days``.
        """
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            workspace = self.get_workspace(workspace_id)
        if workspace is None:
            return []
        if min_open_tabs is None:
            min_open_tabs = int(_vertical_panel_setting(
                'suggestion_min_open_tabs', 3))
        if idle_days is None:
            idle_days = int(_vertical_panel_setting(
                'suggestion_idle_days', 7))
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=idle_days))

        suggestions: list[Group] = []
        for group in workspace.groups:
            if group.archived:
                continue
            if self.group_open_count(
                    workspace_id, group.group_id) < min_open_tabs:
                continue
            latest = _latest_group_activity(workspace, group)
            if latest is not None and latest <= cutoff:
                suggestions.append(group)
        return suggestions

    def get_ignored_suggestion_groups(
            self, workspace_id: uuid.UUID) -> set[str]:
        """Return Group ids currently in the 7-day cooldown window.

        Expired entries are filtered out but left in persisted state; the
        next :meth:`ignore_suggestion_groups` call can safely overwrite
        them.
        """
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            return set()
        now = datetime.datetime.now(datetime.timezone.utc)
        ignored: set[str] = set()
        for group_id, until in (workspace.suggestion_ignored or {}).items():
            parsed = _parse_timestamp(until) if until else None
            if parsed is not None and parsed > now:
                ignored.add(str(group_id))
        return ignored

    def ignore_suggestion_groups(
            self,
            workspace_id: uuid.UUID,
            group_ids: Iterable[str],
    ) -> int:
        """Mark *group_ids* as ignored until now + 7 days.

        Persists through the normal workspace save path (the mapping lives
        on :attr:`Workspace.suggestion_ignored`) and emits :attr:`changed`
        at most once. Idempotent for Groups already inside the cooldown
        window.

        Returns:
            The number of ids newly added (or refreshed after expiry).
        """
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            return 0
        now = datetime.datetime.now(datetime.timezone.utc)
        until = (now + datetime.timedelta(
            days=SUGGESTION_IGNORE_DAYS)).isoformat(timespec='seconds')
        ignored = dict(workspace.suggestion_ignored or {})
        count = 0
        for group_id in group_ids:
            key = str(group_id)
            existing = _parse_timestamp(ignored[key]) if key in ignored else None
            if existing is not None and existing > now:
                continue
            ignored[key] = until
            count += 1
        if count:
            workspace.suggestion_ignored = ignored
            log.workspaces.debug(
                "ignore_suggestion_groups: workspace %s ignored %d group(s)",
                workspace_id, count)
            self.changed.emit()
        return count

    def start_auto_suspend_scheduler(self, parent: QObject) -> None:
        """Run a stale-tab scan now (deferred) and every 12 hours thereafter.

        The periodic timer stays active even when
        ``auto_suspend_enabled`` is false; each scan checks the setting and
        becomes a no-op, so re-enabling it takes effect without restarting
        qutebrowser.
        """
        QTimer.singleShot(0, self._scan_all_workspaces_for_stale)
        try:
            timer = QTimer(parent)
        except TypeError:
            # Test doubles (and non-QObject parents) cannot own the timer;
            # keep it alive through ``self._auto_suspend_timer`` instead.
            timer = QTimer()
        timer.setInterval(12 * 3600 * 1000)
        timer.timeout.connect(self._scan_all_workspaces_for_stale)
        timer.start()
        self._auto_suspend_timer = timer

    def _scan_all_workspaces_for_stale(self) -> int:
        """Run the periodic stale scan.

        Suspends stale ``DONE`` tabs (when auto-suspend is enabled), then
        recomputes smart-suggestion candidates for every loaded workspace
        and emits :attr:`suggestions_changed`. The suggestion scan still runs
        when auto-suspend is disabled; the UI decides whether the card is
        shown via ``content.vertical_panel.suggestion_enabled``.
        """
        value = config.val
        panel = getattr(getattr(value, 'content', None),
                        'vertical_panel', None)
        total = 0
        if panel is not None and getattr(
                panel, 'auto_suspend_enabled', False):
            for workspace_id in list(self._workspaces.keys()):
                total += self.auto_suspend_due_tabs(workspace_id)

        suggestions: list[Group] = []
        for workspace in list(self._workspaces.values()):
            suggestions.extend(self.suggest_stale_groups(workspace.id))
        self.suggestions_changed.emit(suggestions)
        log.workspaces.debug(
            "scan_all_workspaces_for_stale: %d suggestion(s)", len(suggestions))
        return total

    def tabs_by_status(
            self,
            workspace_id: uuid.UUID,
            status: str,
    ) -> list[WorkspaceTab]:
        """Return the tabs in *workspace_id* whose reading status matches.

        This is a pure query: no :attr:`changed` is emitted.
        """
        workspace = self._workspaces[workspace_id]
        return [tab for tab in workspace.tabs
                if tab.reading_status == status]

    def cleanup_old_done(
            self,
            workspace_id: uuid.UUID,
            days: int,
    ) -> int:
        """Remove old ``'DONE'`` suspended tabs from a workspace.

        A tab is removed only when *all* of the following hold:

        * its ``reading_status`` is ``'DONE'``;
        * its ``open_status`` is ``'SUSPENDED'`` (read via ``getattr`` with a
          ``'SUSPENDED'`` fallback so this works before VP-D1 adds the
          field);
        * its effective timestamp — ``last_fetched``, else the workspace's
          ``last_active``, else the entry's ``created_at`` — is older than
          *days*.  Entries with no usable timestamp are treated as recent and
          kept.

        Groups are never touched. :attr:`changed` is emitted once if (and
        only if) at least one tab was removed.

        Returns:
            The number of tabs that were removed.
        """
        workspace = self._workspaces[workspace_id]
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days))
        removed_ids: set[int] = set()
        kept: list[WorkspaceTab] = []
        for tab in workspace.tabs:
            if tab.reading_status != 'DONE':
                kept.append(tab)
                continue
            if getattr(tab, 'open_status', 'SUSPENDED') != 'SUSPENDED':
                kept.append(tab)
                continue
            timestamp = (tab.last_fetched or workspace.last_active
                         or tab.created_at)
            parsed = _parse_timestamp(timestamp) if timestamp else None
            if parsed is not None and parsed < cutoff:
                removed_ids.add(tab.tab_id)
            else:
                kept.append(tab)
        if removed_ids:
            workspace.tabs = kept
            # Detach any surviving children of the removed tabs so they do
            # not point at a non-existent parent (mirrors :meth:`remove_tab`).
            for tab in workspace.tabs:
                if tab.parent_id in removed_ids:
                    tab.clear_parent()
            log.workspaces.debug(
                "cleanup_old_done: workspace %s removed %d tab(s)",
                workspace_id, len(removed_ids))
            self.changed.emit()
        return len(removed_ids)

    # --- pinned tabs ---

    def add_pinned(self, tab: PinnedTab) -> None:
        """Add a pinned tab, replacing any existing entry with its tab_id."""
        self._pinned = [pinned for pinned in self._pinned
                        if pinned.tab_id != tab.tab_id]
        self._pinned.append(tab)
        self.changed.emit()
        self.pinned_changed.emit()

    def remove_pinned(self, tab_id: int) -> None:
        """Remove a pinned tab by tab_id."""
        self._pinned = [pinned for pinned in self._pinned
                        if pinned.tab_id != tab_id]
        self.changed.emit()
        self.pinned_changed.emit()

    def move_pinned(self, tab_id: int, new_index: int) -> bool:
        """Move the pinned tab *tab_id* to *new_index*.

        Returns True when the list changed.  Unknown ids and out-of-range
        indices are ignored (logged) so callers can resolve user input
        without a second lookup.
        """
        entry = next(
            (pinned for pinned in self._pinned if pinned.tab_id == tab_id),
            None)
        if entry is None:
            log.workspaces.debug(
                "move_pinned: unknown pinned tab %s", tab_id)
            return False
        if not 0 <= new_index < len(self._pinned):
            log.workspaces.debug(
                "move_pinned: index %s out of range (0..%s)",
                new_index, len(self._pinned) - 1)
            return False
        old_index = self._pinned.index(entry)
        if old_index == new_index:
            return False
        self._pinned.pop(old_index)
        self._pinned.insert(new_index, entry)
        self.changed.emit()
        self.pinned_changed.emit()
        return True

    def list_pinned(self) -> list[PinnedTab]:
        """Return all pinned tabs."""
        return list(self._pinned)
