# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bridge between the live :class:`TabbedBrowser` and the workspace catalog.

``TabbedBrowser`` emits tab lifecycle signals (``new_tab``) and each
:class:`~qutebrowser.browser.browsertab.AbstractTab` emits per-tab metadata
signals (title, URL, load state, audio, pinned state).  Nothing on the
workspace side listens to them, so ``WorkspaceManager`` stayed empty and the
vertical panel's :class:`~qutebrowser.mainwindow.tabtree.TabTree` showed the
"No tabs in this workspace" placeholder even with real tabs open.

:class:`WorkspaceBridge` closes that loop:

* :meth:`WorkspaceBridge._on_new_tab` ensures an active workspace exists,
  registers a ``WorkspaceTab`` for the new tab, caches the widget index and
  attaches per-tab signal handlers.
* :meth:`WorkspaceBridge._on_tab_removed` (and the per-tab
  ``shutting_down`` fallback, see below) removes the tab from the manager and
  clears its display data from the model.
* The per-tab handlers translate titles / URLs / load / audio / pinned state
  into :meth:`TabTreeModel.set_tab_info` calls so rows render real data and
  status flags.
* The TabTree-as-superset-of-TabBar slice wires the tree into the browser:
  ``current_tab_changed`` is reflected into the model's ``ActiveRole``, and
  the tree's ``activated`` / ``close_requested`` / ``audio_toggled`` /
  ``pin_toggled`` signals drive the real ``QTabWidget`` and mute/pin APIs.
  ``TabbedBrowser`` remains the source of truth; the bridge merely forwards
  the tree's *requests* and reflects the results back into the highlight.

Removal detection
=================

``QTabWidget.tabRemoved`` is a *protected virtual method*, not a signal, so it
cannot be connected to on the real widget.  The bridge therefore:

* connects to ``widget.tabRemoved`` **only when it is actually a signal**
  (e.g. the test double, or a future Qt/backend that exposes one), and
* always connects each tab's ``shutting_down`` signal, which is the real
  removal hook used by :class:`TabbedBrowser`.

Both paths funnel into :meth:`_remove_tab_id`, which is idempotent.

Known limitations (tracked for the next sub-slice)
==================================================

* Tabs inserted directly through the ``QTabWidget`` API (bypassing
  ``TabbedBrowser.tabopen``) are only picked up by the construction-time
  ``_sync_existing_tabs`` pass, not while running.
* Drag-and-drop reparenting, the search overlay and the real audio toggle API
  are out of scope for this slice.
* Favicons / titles / URLs are captured into the :class:`WorkspaceTab`
  metadata (VP-F1); the tree's placeholder pixmap is still used for display.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional, cast

from qutebrowser.qt import sip
from qutebrowser.qt.core import QModelIndex, QObject, QTimer, QUrl, Qt
from qutebrowser.qt.widgets import QWidget

from qutebrowser.config import config
from qutebrowser.mainwindow.tabtree import Status, TabTreeModel
from qutebrowser.utils import log, objreg, qtutils
from qutebrowser.workspaces import iconbytes, tabentry_fetch
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import Workspace, WorkspaceTab

if TYPE_CHECKING:
    from qutebrowser.browser.browsertab import AbstractTab
    from qutebrowser.mainwindow.tabbedbrowser import TabbedBrowser
    from qutebrowser.mainwindow.tabtree import TabTree
    from qutebrowser.qt.gui import QIcon


def _panel_setting(name: str, default: object) -> object:
    """Read a ``content.vertical_panel`` setting safely.

    Returns *default* when the config has not been initialized yet (for
    example in headless unit tests), so callers can stay inert.
    """
    panel = getattr(
        getattr(config.val, 'content', None), 'vertical_panel', None)
    if panel is None:
        return default
    return getattr(panel, name, default)


#: URL schemes which never map to a workspace entry; a switch to one of
#: these must not trigger the VP-D5 auto-locate (internal pages).
_INTERNAL_SCHEMES = frozenset({'qute', 'about', 'chrome', 'view-source'})


# --- Search result click-to-jump (VP-P5) ---

def _get_manager() -> Optional[WorkspaceManager]:
    """Return the app-wide workspace manager, or ``None`` if unavailable."""
    try:
        return objreg.get('workspace-manager')
    except (KeyError, objreg.RegistryUnavailableError):
        return None


def _registry_for_window(win_id: int) -> dict[int, 'AbstractTab']:
    """Return the ``tab_id -> tab`` registry for *win_id* (empty if absent)."""
    try:
        registry = objreg.get(
            'tab-registry', scope='window', window=win_id)
    except (KeyError, objreg.RegistryUnavailableError):
        return {}
    return dict(registry or {})


def _iter_window_resources():
    """Yield ``(win_id, registry, browser)`` for every usable window."""
    win_ids = getattr(objreg.window_registry, 'window_ids', None)
    if win_ids is None:
        try:
            win_ids = list(objreg.window_registry)
        except TypeError:
            win_ids = []
    for win_id in list(win_ids) or [0]:
        try:
            registry = objreg.get(
                'tab-registry', scope='window', window=win_id)
            browser = objreg.get(
                'tabbed-browser', scope='window', window=win_id)
        except (KeyError, objreg.RegistryUnavailableError):
            continue
        if registry is None or browser is None:
            continue
        yield win_id, registry, browser


def _find_live_tab_by_url(url: str):
    """Return ``(win_id, browser, live_tab)`` for the first matching URL."""
    if not url:
        return None
    for win_id, registry, browser in _iter_window_resources():
        for live in registry.values():
            try:
                live_url = live.url().toString()
            except (AttributeError, NotImplementedError):
                continue
            if live_url == url:
                return win_id, browser, live
    return None


def _workspace_for_entry(
        manager: Optional[WorkspaceManager],
        entry: WorkspaceTab,
) -> Optional[Workspace]:
    """Find the workspace containing *entry* (by identity, then by tab id)."""
    if manager is None:
        return None
    for workspace in manager.list_workspaces():
        for candidate in workspace.tabs:
            if candidate is entry:
                return workspace
    for workspace in manager.list_workspaces():
        for candidate in workspace.tabs:
            if candidate.tab_id == entry.tab_id:
                return workspace
    return None


def _mark_entry_open(
        manager: Optional[WorkspaceManager],
        workspace: Optional[Workspace],
        entry: WorkspaceTab,
) -> None:
    """Mark *entry* OPEN when a manager/workspace pair is available."""
    if manager is not None and workspace is not None:
        manager.set_open_status(workspace.id, entry.tab_id, 'OPEN')


def _browser_for_window(win_id: int):
    """Return the tabbed browser for *win_id*, or ``None``."""
    for res_win_id, _registry, browser in _iter_window_resources():
        if res_win_id == win_id:
            return browser
    return None


def _reuse_live_tab(
        hit,
        manager: Optional[WorkspaceManager],
        workspace: Optional[Workspace],
        entry: WorkspaceTab,
):
    """Focus the live tab from *hit* and mark *entry* OPEN."""
    hit_win_id, browser, live = hit
    try:
        browser.widget.setCurrentWidget(live)
    except Exception as e:  # noqa: BLE001 - reopening is best-effort
        log.workspaces.debug(
            f"reopen_tab: setCurrentWidget failed for {entry.tab_id}: {e}")
        return None
    _mark_entry_open(manager, workspace, entry)
    _raise_window(hit_win_id)
    return live


def _open_new_tab(
        browser,
        manager: Optional[WorkspaceManager],
        workspace: Optional[Workspace],
        entry: WorkspaceTab,
        win_id: int,
):
    """Open *entry*'s URL in *browser* and return the resulting live tab."""
    try:
        qurl = QUrl(entry.url)
        qtutils.ensure_valid(qurl)
        browser.tabopen(qurl, background=False)
    except Exception as e:  # noqa: BLE001 - reopening is best-effort
        log.workspaces.debug(
            f"reopen_tab: tabopen failed for url={entry.url!r}: {e}")
        return None

    _mark_entry_open(manager, workspace, entry)

    live = _registry_for_window(win_id).get(entry.tab_id)
    if live is None:
        hit = _find_live_tab_by_url(entry.url)
        if hit is not None:
            live = hit[2]
    if live is None:
        try:
            live = browser.widget.currentWidget()
        except Exception:  # noqa: BLE001 - best-effort fallback
            live = None
    return live


def _reopen_single_tab(
        workspace: Optional[Workspace],
        entry: WorkspaceTab,
        *,
        win_id: int = 0,
):
    """Reopen one SUSPENDED *entry* and return the live tab, or ``None``.

    This is the single-entry equivalent of
    :meth:`WorkspaceManager.reopen_group` (VP-D4): a live tab with the same
    URL in *any* window is reused, otherwise the URL is opened in *win_id*.
    """
    if not entry.url:
        log.workspaces.debug(
            f"reopen_tab: skipping tab {entry.tab_id} with an empty URL")
        return None

    manager = _get_manager()
    hit = _find_live_tab_by_url(entry.url)
    if hit is not None:
        return _reuse_live_tab(hit, manager, workspace, entry)

    browser = _browser_for_window(win_id)
    if browser is None:
        log.workspaces.debug(
            f"reopen_tab: no browser for window {win_id}; "
            f"cannot reopen {entry.tab_id}")
        return None
    return _open_new_tab(browser, manager, workspace, entry, win_id)


def _raise_window(win_id: int) -> None:
    """Raise / activate the main window for *win_id* (best-effort)."""
    try:
        window = objreg.get(
            'main-window', scope='window', window=win_id)
    except (KeyError, objreg.RegistryUnavailableError):
        return
    try:
        window.raise_()
        window.activateWindow()
    except (AttributeError, RuntimeError):
        pass


def reopen_tab(entry: WorkspaceTab, *, win_id: int = 0):
    """Reopen a single SUSPENDED *entry* and return the live tab widget."""
    manager = _get_manager()
    workspace = _workspace_for_entry(manager, entry)
    return _reopen_single_tab(workspace, entry, win_id=win_id)


def _browser_for_live(live, win_id: int):
    """Return the browser owning *live*, falling back to *win_id*'s browser."""
    for _win_id, _registry, browser in _iter_window_resources():
        try:
            if browser.widget.indexOf(live) >= 0:
                return browser
        except Exception:  # noqa: BLE001 - mock / incomplete tab widgets
            continue
    for res_win_id, _registry, browser in _iter_window_resources():
        if res_win_id == win_id:
            return browser
    return None


def jump_to_entry(entry: WorkspaceTab, win_id: int = 0) -> None:
    """Switch to the live tab behind *entry*, reopening it when suspended.

    OPEN entries are looked up in the window's ``tab-registry``; an orphaned
    entry is logged and skipped.  SUSPENDED entries go through
    :func:`reopen_tab` first and the returned live tab is then focused.
    """
    if entry.open_status == 'OPEN':
        live = _registry_for_window(win_id).get(entry.tab_id)
        if live is None:
            log.workspaces.debug(
                f"jump_to_entry: OPEN tab {entry.tab_id} not in registry; "
                "skipping")
            return
    else:
        live = reopen_tab(entry, win_id=win_id)
        if live is None:
            log.workspaces.debug(
                f"jump_to_entry: could not reopen tab {entry.tab_id}")
            return

    browser = _browser_for_live(live, win_id)
    if browser is None:
        log.workspaces.debug(
            f"jump_to_entry: no browser for window {win_id}; "
            f"cannot switch to {entry.tab_id}")
        return
    try:
        browser.widget.setCurrentWidget(live)
        live.setFocus(Qt.FocusReason.TabFocusReason)
    except Exception as e:  # noqa: BLE001 - switching is best-effort
        log.workspaces.debug(
            f"jump_to_entry: setCurrentWidget failed for {entry.tab_id}: {e}")


@dataclasses.dataclass
class _TabState:

    """Runtime display/status state for a single bridged tab."""

    title: str = ''
    url: str = ''
    loading: bool = False
    recently_audible: bool = False
    muted: bool = False
    pinned: bool = False
    suspended: bool = False

    def status(self) -> Status:
        """Combine the tracked booleans into a :class:`Status` flag set."""
        status = Status.NONE
        if self.loading:
            status |= Status.LOADING
        if self.recently_audible and not self.muted:
            status |= Status.AUDIBLE
        if self.pinned:
            status |= Status.PINNED
        if self.suspended:
            status |= Status.SUSPENDED
        return status


class WorkspaceBridge(QObject):

    """Syncs ``TabbedBrowser`` tab lifecycle into the workspace catalog.

    Attributes:
        _win_id: The window id used to resolve the per-window tab registry.
        _browser: The tabbed browser whose tabs are mirrored.
        _manager: The workspace manager to keep in sync, or None.
        _model: The tab tree model to feed display data, or None.
        _tab_tree: The TabTree host used for auto-locate on tab switch, or
            None when the bridge was built without one.
        _tab_index: ``QTabWidget`` index -> ``tab.tab_id`` cache.
        _tab_state: ``tab.tab_id`` -> :class:`_TabState`.
        _attached: ``tab_id`` set of tabs whose signals are already wired.
    """

    def __init__(
            self,
            tabbed_browser: TabbedBrowser,
            manager: Optional[WorkspaceManager],
            model: Optional[TabTreeModel],
            *,
            tab_tree: Optional[TabTree] = None,
            win_id: int = 0,
            parent: Optional[QWidget] = None,
            description_fetcher: Callable[..., None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._win_id = win_id
        self._browser = tabbed_browser
        self._manager = manager
        self._model = model
        self._tab_tree = tab_tree
        self._tab_index: dict[int, int] = {}
        self._tab_state: dict[int, _TabState] = {}
        self._attached: set[int] = set()
        self._user_initiated_close_flags: dict[int, bool] = {}
        self._description_fetcher = (
            description_fetcher or tabentry_fetch.fetch_description)

        if manager is None or model is None:
            # Tolerate headless / no-workspace startup: stay inert so the
            # rest of window construction keeps working.
            return

        tabbed_browser.new_tab.connect(self._on_new_tab)
        tabbed_browser.current_tab_changed.connect(
            self._on_current_tab_changed)
        removed = getattr(tabbed_browser.widget, 'tabRemoved', None)
        if removed is not None and hasattr(removed, 'connect'):
            # Only true signals can be connected; the real QTabWidget's
            # tabRemoved is a virtual method (see module docstring).
            removed.connect(self._on_tab_removed)

        if tab_tree is not None:
            tab_tree.activated.connect(self._on_tree_activated)
            tab_tree.keyboard_activated.connect(
                self._on_tree_keyboard_activated)
            tab_tree.close_requested.connect(self._on_tree_close_requested)
            tab_tree.audio_toggled.connect(self._on_tree_audio_toggled)
            tab_tree.pin_toggled.connect(self._on_tree_pin_toggled)

        # Pick up tabs that already exist (e.g. the initial home tab created
        # before this bridge was constructed), and re-check once the event
        # loop runs to catch a tab opened during window construction.
        self._sync_existing_tabs()
        self._sync_active_tab()
        QTimer.singleShot(0, self._sync_existing_tabs)

    # --- registration ---

    def _ensure_active_workspace(self) -> Optional[uuid.UUID]:
        """Return the active workspace id, creating a default one if needed."""
        workspace = self._manager.get_active()  # type: ignore[union-attr]
        if workspace is not None:
            return workspace.id
        workspace = self._manager.add_workspace('default')  # type: ignore[union-attr]
        return workspace.id

    def _sync_existing_tabs(self) -> None:
        """Register any tabs already present in the ``QTabWidget``."""
        if self._manager is None or self._model is None:
            return
        if sip.isdeleted(self._model):
            # The deferred re-sync can outlive the model during teardown.
            return
        widget = self._browser.widget
        for idx in range(widget.count()):
            tab = widget.widget(idx)
            if tab is None:
                continue
            self._register_tab(cast('AbstractTab', tab), idx)

    def _on_new_tab(self, tab: AbstractTab, idx: int) -> None:
        """Register a tab announced through ``TabbedBrowser.new_tab``."""
        self._register_tab(tab, idx)

    def _sync_active_tab(self) -> None:
        """Reflect the browser's current tab as the active tree row."""
        widget = self._browser.widget
        current = widget.currentWidget()
        if current is None:
            return
        tab_id = getattr(current, 'tab_id', None)
        if tab_id is not None:
            self._model.set_active_tab_id(tab_id)  # type: ignore[union-attr]

    def _tab_registry(self) -> dict[int, AbstractTab]:
        """Return the per-window ``tab_id -> AbstractTab`` registry."""
        try:
            return objreg.get(
                'tab-registry', scope='window', window=self._win_id)
        except (KeyError, objreg.RegistryUnavailableError):
            return {}

    @staticmethod
    def _live_url(tab: AbstractTab) -> str:
        """Return *tab*'s current URL as a string ('' when unavailable)."""
        try:
            return tab.url().toString()
        except (AttributeError, NotImplementedError):
            return ''

    def _ensure_entry(
            self,
            workspace_id: uuid.UUID,
            workspace: Workspace,
            tab: AbstractTab,
            tab_id: int,
    ) -> None:
        """Ensure *tab_id* has exactly one entry, reusing by id or URL.

        VP-D1: a *new* tab id may still belong to an entry retained for the
        same URL (e.g. a suspended tab re-opened from history). Match by URL
        instead of creating a duplicate entry, and rebind the entry's
        ``tab_id`` so it tracks the live tab from here on.
        """
        tabs = workspace.tabs
        existing = next(
            (entry for entry in tabs if entry.tab_id == tab_id), None)
        if existing is not None:
            # Re-using a retained entry (e.g. after a suspend): mark it OPEN
            # again and clear the SUSPENDED status bit before pushing.
            self._manager.set_open_status(  # type: ignore[union-attr]
                workspace_id, tab_id, 'OPEN')
            return
        live_url = self._live_url(tab)
        if live_url:
            existing = next(
                (entry for entry in tabs if entry.url == live_url), None)
        if existing is None:
            self._manager.add_tab(  # type: ignore[union-attr]
                workspace_id,
                WorkspaceTab(tab_id=tab_id, open_status='OPEN'),
            )
            return
        old_id = existing.tab_id
        self._manager.set_open_status(  # type: ignore[union-attr]
            workspace_id, old_id, 'OPEN')
        if old_id != tab_id:
            self._manager.update_tab_metadata(  # type: ignore[union-attr]
                workspace_id, old_id, new_tab_id=tab_id)

    # --- TabTree -> TabbedBrowser direction ---

    def _on_current_tab_changed(self, tab: AbstractTab) -> None:
        """Reflect the browser's ``current_tab_changed`` into the model.

        Besides updating the active row it also asks the tree to auto-locate
        the newly focused tab (VP-D5): expand and scroll its group, then
        briefly highlight the tab.
        """
        if self._model is None or sip.isdeleted(self._model):
            return
        new_id = tab.tab_id
        if new_id == self._model.active_tab_id():
            # Guard against the feedback loop from a tree-driven switch.
            return
        self._model.set_active_tab_id(new_id)
        self._auto_locate(tab)

    def _auto_locate(self, tab: AbstractTab) -> None:
        """Emit ``group_focus_requested`` for the newly focused *tab*.

        Skips internal pages (``qute://`` / ``about:`` / ``chrome://`` /
        ``view-source:``), empty URLs and tabs which are not tracked live
        (i.e. shutting down).  When the tab has no workspace entry the
        browser switch is simply ignored -- auto-locate never creates state.
        """
        if self._manager is None or self._tab_tree is None:
            return
        # A tab which is shutting down has already been detached; never
        # auto-locate a dying tab.
        if tab.tab_id not in self._attached:
            return
        url = self._live_url(tab)
        if not url:
            return
        qurl = QUrl(url)
        try:
            qtutils.ensure_valid(qurl)
        except qtutils.QtValueError:
            return  # invalid URL — treat as no-op
        if qurl.scheme() in _INTERNAL_SCHEMES:
            return
        workspace_id = self._active_workspace_id()
        if workspace_id is None:
            return
        entry = self._manager.find_tab_by_id(workspace_id, tab.tab_id)
        if entry is None:
            entry = self._manager.find_tab_by_url(workspace_id, url)
        if entry is None:
            return
        self._tab_tree.group_focus_requested.emit(
            entry.group_id, entry.tab_id)

    def _activate_tab_id(self, tab_id: int, *, focus: bool) -> None:
        """Switch to *tab_id*, reopening a SUSPENDED entry first (VP-P5).

        Live tabs take the fast path through the per-window registry.  When
        the row points at a SUSPENDED workspace entry (e.g. a search result),
        the single-entry D4 reopen path produces a live tab which is then
        focused.
        """
        widget = self._tab_registry().get(tab_id)
        if widget is not None:
            self._browser.widget.setCurrentWidget(widget)
            if focus:
                widget.setFocus(Qt.FocusReason.TabFocusReason)
            return

        workspace_id = self._active_workspace_id()
        entry = None
        if workspace_id is not None and self._manager is not None:
            entry = self._manager.find_tab_by_id(workspace_id, tab_id)
        if entry is None or entry.open_status != 'SUSPENDED':
            log.workspaces.debug(
                f"_activate_tab_id: tab {tab_id} not live; skipping")
            return
        live = reopen_tab(entry, win_id=self._win_id)
        if live is None:
            return
        self._browser.widget.setCurrentWidget(live)
        if focus:
            live.setFocus(Qt.FocusReason.TabFocusReason)

    def _on_tree_activated(self, idx: QModelIndex) -> None:
        """Switch to the tab behind a clicked/selected tree row."""
        if self._model is None:
            return
        tab_id = self._model.tab_id_for_index(idx)
        if tab_id is None:
            return
        self._activate_tab_id(tab_id, focus=True)

    def _on_tree_keyboard_activated(self, idx: QModelIndex) -> None:
        """Switch tabs from keyboard nav; keep focus in the tree for J/K."""
        if self._model is None:
            return
        tab_id = self._model.tab_id_for_index(idx)
        if tab_id is None:
            return
        self._activate_tab_id(tab_id, focus=False)

    def _on_tree_close_requested(self, idx: QModelIndex) -> None:
        """Close the tab behind a tree row by emitting ``tabCloseRequested``."""
        if self._model is None:
            return
        tab_id = self._model.tab_id_for_index(idx)
        if tab_id is None:
            return
        widget = self._tab_registry().get(tab_id)
        if widget is None:
            return
        widget_idx = self._browser.widget.indexOf(widget)
        if widget_idx >= 0:
            self._browser.widget.tabCloseRequested.emit(widget_idx)

    def _on_tree_audio_toggled(self, idx: QModelIndex) -> None:
        """Flip the mute state of the tab behind a tree row."""
        if self._model is None:
            return
        tab_id = self._model.tab_id_for_index(idx)
        if tab_id is None:
            return
        tab = self._tab_registry().get(tab_id)
        if tab is None or not hasattr(tab, 'audio'):
            return
        tab.audio.set_muted(not tab.audio.is_muted())

    def _on_tree_pin_toggled(self, idx: QModelIndex) -> None:
        """Flip the pinned state of the tab behind a tree row."""
        if self._model is None:
            return
        tab_id = self._model.tab_id_for_index(idx)
        if tab_id is None:
            return
        tab = self._tab_registry().get(tab_id)
        if tab is None or not hasattr(tab, 'data'):
            return
        new_state = not bool(getattr(tab.data, 'pinned', False))
        tab.set_pinned(new_state)
        # ``set_pinned`` emits ``pinned_changed`` synchronously, which the
        # per-tab handler in :meth:`_attach_tab` already translates into a
        # full ``set_tab_info`` push.  We additionally flip the derived
        # ``PinStateRole`` bit directly so the row repaints even if this tab
        # is not (yet) attached to the per-tab signal pipeline.
        self._model.set_tab_pinned(tab_id, new_state)

    def _register_tab(self, tab: AbstractTab, idx: int) -> None:
        """Add *tab* to the active workspace and wire its signals."""
        tab_id = tab.tab_id
        if tab_id in self._tab_state:
            # Already bridged (e.g. the single-shot re-sync); just refresh
            # the index cache without shifting it again.
            self._tab_index[idx] = tab_id
            workspace_id = self._active_workspace_id()
            if workspace_id is not None:
                self._manager.set_open_status(  # type: ignore[union-attr]
                    workspace_id, tab_id, 'OPEN')
            self._tab_state[tab_id].suspended = False
            self._push(tab_id)
            return

        workspace_id = self._ensure_active_workspace()
        if workspace_id is None:
            return
        workspace = self._manager.get_active()  # type: ignore[union-attr]
        assert workspace is not None
        self._ensure_entry(workspace_id, workspace, tab, tab_id)

        # A tab inserted at *idx* shifts every later index up by one.
        self._tab_index = {
            key + 1 if key >= idx else key: value
            for key, value in self._tab_index.items()
        }
        self._tab_index[idx] = tab_id

        state = self._tab_state.setdefault(tab_id, _TabState())
        state.suspended = False
        data = getattr(tab, 'data', None)
        state.pinned = bool(getattr(data, 'pinned', False))
        try:
            state.title = tab.title()
        except (AttributeError, NotImplementedError):
            pass
        try:
            state.url = tab.url().toString()
        except (AttributeError, NotImplementedError):
            pass

        # Persist the live title/url/source (and any already-available
        # favicon) onto the WorkspaceTab, not just the ephemeral state.
        favicon = ''
        try:
            favicon = iconbytes.icon_to_png_base64(tab.icon())
        except (AttributeError, NotImplementedError):
            pass
        self._manager.update_tab_metadata(  # type: ignore[union-attr]
            workspace_id,
            tab_id,
            url=state.url or None,
            title=state.title or None,
            favicon=favicon or None,
            fetched=bool(favicon),
        )

        self._attach_tab(tab)
        self._push(tab_id)

    def _attach_tab(self, tab: AbstractTab) -> None:
        """Connect a tab's metadata signals to the bridge handlers."""
        tab_id = tab.tab_id
        if tab_id in self._attached:
            return
        self._attached.add(tab_id)

        tab.title_changed.connect(
            lambda title, tid=tab_id: self._on_title_changed(tid, title))
        tab.url_changed.connect(
            lambda url, tid=tab_id: self._on_url_changed(tid, url))
        tab.icon_changed.connect(
            lambda icon, tid=tab_id: self._on_icon_changed(tid, icon))
        tab.load_started.connect(
            lambda tid=tab_id: self._on_load_started(tid))
        tab.load_finished.connect(
            lambda ok, tid=tab_id: self._on_load_finished(tid, ok))
        tab.pinned_changed.connect(
            lambda pinned, tid=tab_id: self._on_pinned_changed(tid, pinned))
        tab.shutting_down.connect(
            lambda tab=tab: self._on_tab_shutting_down(tab))

        audio = getattr(tab, 'audio', None)
        if audio is not None:
            audio.muted_changed.connect(
                lambda muted, tid=tab_id: self._on_muted_changed(tid, muted))
            audio.recently_audible_changed.connect(
                lambda audible, tid=tab_id:
                    self._on_recently_audible_changed(tid, audible))

    # --- per-tab handlers ---

    def _state_for(self, tab_id: int) -> _TabState:
        return self._tab_state.setdefault(tab_id, _TabState())

    def _push(self, tab_id: int) -> None:
        """Publish the current display/status state to the model."""
        if self._model is None or sip.isdeleted(self._model):
            return
        state = self._state_for(tab_id)
        self._model.set_tab_info(
            tab_id,
            title=state.title,
            url=state.url,
            status=state.status(),
        )

    def _active_workspace_id(self) -> Optional[uuid.UUID]:
        """Return the active workspace id, or None when unavailable."""
        if self._manager is None:
            return None
        workspace = self._manager.get_active()
        return None if workspace is None else workspace.id

    def _on_title_changed(self, tab_id: int, title: str) -> None:
        self._state_for(tab_id).title = title
        workspace_id = self._active_workspace_id()
        if workspace_id is not None:
            # The manager applies the user-edited guard for us.
            self._manager.update_tab_metadata(  # type: ignore[union-attr]
                workspace_id, tab_id, title=title)
        self._push(tab_id)

    def _on_url_changed(self, tab_id: int, url: QUrl) -> None:
        url_str = url.toString()
        self._state_for(tab_id).url = url_str
        workspace_id = self._active_workspace_id()
        if workspace_id is not None:
            source = url.host().lower()
            self._manager.update_tab_metadata(  # type: ignore[union-attr]
                workspace_id, tab_id, url=url_str or None,
                source=source or None)
        self._push(tab_id)

    def _on_icon_changed(self, tab_id: int, icon: QIcon) -> None:
        """Persist a live favicon as base64 PNG onto the workspace tab."""
        encoded = iconbytes.icon_to_png_base64(icon)
        workspace_id = self._active_workspace_id()
        if workspace_id is None or not encoded:
            return
        self._manager.update_tab_metadata(  # type: ignore[union-attr]
            workspace_id, tab_id, favicon=encoded, fetched=True)

    def _on_load_started(self, tab_id: int) -> None:
        self._state_for(tab_id).loading = True
        self._push(tab_id)

    def _on_load_finished(self, tab_id: int, ok: bool) -> None:
        self._state_for(tab_id).loading = False
        self._push(tab_id)
        if ok:
            tab = self._tab_registry().get(tab_id)
            if tab is not None:
                self._fetch_description(tab_id, tab)

    def _fetch_description(self, tab_id: int, tab: AbstractTab) -> None:
        """Best-effort description capture, persisted when it resolves."""
        workspace_id = self._active_workspace_id()
        manager = self._manager
        if workspace_id is None or manager is None:
            return

        def update(description: str) -> None:
            manager.update_tab_metadata(
                workspace_id, tab_id, description=description)

        def on_error(error: str) -> None:
            manager.update_tab_metadata(
                workspace_id, tab_id, description='', error=error)

        try:
            self._description_fetcher(tab, update, on_error=on_error)
        except TypeError:
            # Injected test doubles may only accept (tab, callback).
            self._description_fetcher(tab, update)
        except Exception as e:  # noqa: BLE001 - metadata is best-effort
            on_error(str(e)[:200])

    def _on_pinned_changed(self, tab_id: int, pinned: bool) -> None:
        self._state_for(tab_id).pinned = pinned
        self._push(tab_id)

    def _on_muted_changed(self, tab_id: int, muted: bool) -> None:
        self._state_for(tab_id).muted = muted
        self._push(tab_id)

    def _on_recently_audible_changed(self, tab_id: int, audible: bool) -> None:
        self._state_for(tab_id).recently_audible = audible
        self._push(tab_id)

    # --- removal ---

    def _on_tab_removed(self, idx: int) -> None:
        """Handle a ``QTabWidget``-style ``tabRemoved`` signal."""
        tab_id = self._tab_index.pop(idx, None)
        if tab_id is None:
            return
        # A tab removed at *idx* shifts every later index down by one.
        self._tab_index = {
            key - 1 if key > idx else key: value
            for key, value in self._tab_index.items()
        }
        self._remove_tab_id(tab_id)
        self._schedule_snapshot_check()

    def _on_tab_shutting_down(self, tab: AbstractTab) -> None:
        """Handle a tab's ``shutting_down`` signal (real removal hook)."""
        tab_id = tab.tab_id
        self._tab_index = {
            key: value
            for key, value in self._tab_index.items()
            if value != tab_id
        }
        self._remove_tab_id(tab_id)
        self._schedule_snapshot_check()

    def _schedule_snapshot_check(self) -> None:
        """Schedule a delayed window-cleared snapshot check (VP-P4)."""
        delay = _panel_setting('snapshot_delay_seconds', None)
        if delay is None:
            # No initialized config (headless tests): stay inert.
            return
        QTimer.singleShot(
            int(delay) * 1000,
            lambda wid=self._win_id: self._maybe_snapshot_window(wid),
        )

    def _window_registry(self, win_id: int) -> dict[int, AbstractTab]:
        """Return the live tab registry for *win_id* (empty on failure)."""
        try:
            return objreg.get(
                'tab-registry', scope='window', window=win_id)
        except (KeyError, objreg.RegistryUnavailableError):
            return {}

    @staticmethod
    def _collect_snapshots(
            workspace: Workspace,
            registry: dict[int, AbstractTab],
    ) -> list[dict]:
        """Turn still-OPEN workspace entries into snapshot dicts."""
        snapshots = []
        for tab in workspace.tabs:
            if tab.open_status != 'OPEN':
                continue
            live = registry.get(tab.tab_id)
            if live is None:
                continue
            snapshots.append({
                'url': WorkspaceBridge._live_url(live),
                'title': live.title() or tab.title,
            })
        return snapshots

    def _maybe_snapshot_window(self, win_id: int) -> None:
        """Snapshot OPEN workspace tabs when *win_id* has no tabs left.

        A ``:tab-close --window`` marks the window as user-initiated via
        :attr:`_user_initiated_close_flags`; in that case no snapshot is
        taken and the flag is cleared for the next close.
        """
        if self._user_initiated_close_flags.get(win_id, False):
            self._user_initiated_close_flags[win_id] = False
            return
        if self._manager is None:
            return
        try:
            tabbed_browser = objreg.get(
                'tabbed-browser', scope='window', window=win_id)
        except (KeyError, objreg.RegistryUnavailableError):
            return
        if tabbed_browser.widget.count() > 0:
            return
        workspace_id = self._active_workspace_id()
        if workspace_id is None:
            return
        workspace = self._manager.get_workspace(workspace_id)
        if workspace is None:
            return
        snapshots = self._collect_snapshots(
            workspace, self._window_registry(win_id))
        self._manager.create_window_snapshot(
            workspace_id, snapshots,
            min_tabs=int(_panel_setting('snapshot_min_tabs', 1)),
        )

    def _push_suspended(self, tab_id: int) -> None:
        """Mark *tab_id* as SUSPENDED in the model so the badge paints."""
        state = self._state_for(tab_id)
        state.suspended = True
        self._push(tab_id)

    def _remove_tab_id(self, tab_id: int) -> None:
        """Suspend *tab_id*, keeping its last known display data visible.

        VP-D1 requires the retained entry to stay on screen: the workspace
        entry is only flipped to ``SUSPENDED`` (never removed), so the tree
        row must keep rendering its last known title/URL with the SUSPENDED
        badge instead of going blank.
        """
        self._attached.discard(tab_id)
        if self._model is not None and not sip.isdeleted(self._model):
            # Clear the active highlight if the removed tab was the
            # currently focused one. The browser will emit a fresh
            # ``current_tab_changed`` for the replacement tab if any;
            # otherwise the highlight stays cleared until the next open.
            if self._model.active_tab_id() == tab_id:
                self._model.set_active_tab_id(None)
        if self._manager is None:
            return
        workspace_id = self._active_workspace_id()
        if workspace_id is None:
            return
        # D1: closing a tab keeps its entry in the workspace but flips it to
        # SUSPENDED, so it can be resurrected later instead of being lost.
        self._manager.set_open_status(
            workspace_id, tab_id, 'SUSPENDED')
        self._push_suspended(tab_id)
