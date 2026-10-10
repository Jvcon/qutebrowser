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
* Favicons are not forwarded yet (the placeholder pixmap is used).
* Drag-and-drop reparenting, the search overlay and the real audio toggle API
  are out of scope for this slice.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Optional, cast

from qutebrowser.qt import sip
from qutebrowser.qt.core import QModelIndex, QObject, QTimer, QUrl, Qt
from qutebrowser.qt.widgets import QWidget

from qutebrowser.mainwindow.tabtree import Status, TabTreeModel
from qutebrowser.utils import objreg
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import WorkspaceTab

if TYPE_CHECKING:
    from qutebrowser.browser.browsertab import AbstractTab
    from qutebrowser.mainwindow.tabbedbrowser import TabbedBrowser
    from qutebrowser.mainwindow.tabtree import TabTree


@dataclasses.dataclass
class _TabState:

    """Runtime display/status state for a single bridged tab."""

    title: str = ''
    url: str = ''
    loading: bool = False
    recently_audible: bool = False
    muted: bool = False
    pinned: bool = False

    def status(self) -> Status:
        """Combine the tracked booleans into a :class:`Status` flag set."""
        status = Status.NONE
        if self.loading:
            status |= Status.LOADING
        if self.recently_audible and not self.muted:
            status |= Status.AUDIBLE
        if self.pinned:
            status |= Status.PINNED
        return status


class WorkspaceBridge(QObject):

    """Syncs ``TabbedBrowser`` tab lifecycle into the workspace catalog.

    Attributes:
        _win_id: The window id used to resolve the per-window tab registry.
        _browser: The tabbed browser whose tabs are mirrored.
        _manager: The workspace manager to keep in sync, or None.
        _model: The tab tree model to feed display data, or None.
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
    ) -> None:
        super().__init__(parent)
        self._win_id = win_id
        self._browser = tabbed_browser
        self._manager = manager
        self._model = model
        self._tab_index: dict[int, int] = {}
        self._tab_state: dict[int, _TabState] = {}
        self._attached: set[int] = set()

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
        except KeyError:
            return {}

    # --- TabTree -> TabbedBrowser direction ---

    def _on_current_tab_changed(self, tab: AbstractTab) -> None:
        """Reflect the browser's ``current_tab_changed`` into the model."""
        if self._model is None or sip.isdeleted(self._model):
            return
        new_id = tab.tab_id
        if new_id == self._model.active_tab_id():
            # Guard against the feedback loop from a tree-driven switch.
            return
        self._model.set_active_tab_id(new_id)

    def _on_tree_activated(self, idx: QModelIndex) -> None:
        """Switch to the tab behind a clicked/selected tree row."""
        if self._model is None:
            return
        tab_id = self._model.tab_id_for_index(idx)
        if tab_id is None:
            return
        widget = self._tab_registry().get(tab_id)
        if widget is None:
            return
        self._browser.widget.setCurrentWidget(widget)
        widget.setFocus(Qt.FocusReason.TabFocusReason)

    def _on_tree_keyboard_activated(self, idx: QModelIndex) -> None:
        """Switch tabs from keyboard nav; keep focus in the tree for J/K."""
        if self._model is None:
            return
        tab_id = self._model.tab_id_for_index(idx)
        if tab_id is None:
            return
        widget = self._tab_registry().get(tab_id)
        if widget is None:
            return
        self._browser.widget.setCurrentWidget(widget)

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
            self._push(tab_id)
            return

        workspace_id = self._ensure_active_workspace()
        if workspace_id is None:
            return
        workspace = self._manager.get_active()  # type: ignore[union-attr]
        assert workspace is not None
        if not any(existing.tab_id == tab_id for existing in workspace.tabs):
            self._manager.add_tab(  # type: ignore[union-attr]
                workspace_id, WorkspaceTab(tab_id=tab_id))

        # A tab inserted at *idx* shifts every later index up by one.
        self._tab_index = {
            key + 1 if key >= idx else key: value
            for key, value in self._tab_index.items()
        }
        self._tab_index[idx] = tab_id

        state = self._tab_state.setdefault(tab_id, _TabState())
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

    def _on_title_changed(self, tab_id: int, title: str) -> None:
        self._state_for(tab_id).title = title
        self._push(tab_id)

    def _on_url_changed(self, tab_id: int, url: QUrl) -> None:
        self._state_for(tab_id).url = url.toString()
        self._push(tab_id)

    def _on_load_started(self, tab_id: int) -> None:
        self._state_for(tab_id).loading = True
        self._push(tab_id)

    def _on_load_finished(self, tab_id: int, ok: bool) -> None:
        self._state_for(tab_id).loading = False
        self._push(tab_id)

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

    def _on_tab_shutting_down(self, tab: AbstractTab) -> None:
        """Handle a tab's ``shutting_down`` signal (real removal hook)."""
        tab_id = tab.tab_id
        self._tab_index = {
            key: value
            for key, value in self._tab_index.items()
            if value != tab_id
        }
        self._remove_tab_id(tab_id)

    def _remove_tab_id(self, tab_id: int) -> None:
        """Drop *tab_id* from the model cache and the active workspace."""
        self._tab_state.pop(tab_id, None)
        self._attached.discard(tab_id)
        if self._model is not None and not sip.isdeleted(self._model):
            self._model.clear_tab_info(tab_id)
            # Clear the active highlight if the removed tab was the
            # currently focused one. The browser will emit a fresh
            # ``current_tab_changed`` for the replacement tab if any;
            # otherwise the highlight stays cleared until the next open.
            if self._model.active_tab_id() == tab_id:
                self._model.set_active_tab_id(None)
        if self._manager is None:
            return
        workspace = self._manager.get_active()
        if workspace is not None:
            self._manager.remove_tab(workspace.id, tab_id)
